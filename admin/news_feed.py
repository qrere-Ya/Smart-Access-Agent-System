"""
news_feed.py：生成式 AI 早報的資料來源（官方部落格與服務狀態的 RSS、Hacker News），整理成固定的四欄格式。

主要功能：
  - fetch_ai_news：同時抓固定的幾個來源（OpenAI 與 Anthropic 服務狀態、OpenAI／DeepMind／AWS／NVIDIA／
    Hugging Face 的官方部落格、Hacker News 上高分的 AI 討論），挑出最近的幾則，
    每則整理成 類別、標題、發生什麼事、為什麼重要、可以怎麼用、來源與原文；
    只有一部分來源失敗也照常出報，失敗原因另外回傳，不讓晨報中斷
  - 類別、為什麼重要、可以怎麼用都是用來源種類與標題關鍵字決定的固定句子，不是 AI 判斷；
    「發生什麼事」是來源自己的摘要（去掉 HTML、限制長度）
  - 安全：來源網址是寫死的 https 公開網域，只收 https 的原文連結、不跟隨轉址、每個來源最多讀 2MB、逾時 10 秒；
    解析 XML 前先拒收 DOCTYPE／ENTITY 與非 UTF-8；所有文字都當作不可信的純文字，不交給語言模型、不變成可點的連結

串接模組：
  - morning_brief：晨報呼叫 fetch_ai_news 並用 format_news 排版
"""
import datetime
import email.utils
import html
import json
import re
# 標準函式庫的 XML 解析器擋不住實體膨脹；parse_feed 在解析前已拒收 DOCTYPE／ENTITY，且讀取有大小上限
import xml.etree.ElementTree as ET  # nosec B405
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote, urlparse

import requests

# (顯示名稱, 網址, 種類)；種類：status 服務狀態、blog 官方部落格
RSS_SOURCES = (
    ("OpenAI 服務狀態", "https://status.openai.com/feed.rss", "status"),
    ("Anthropic 服務狀態", "https://status.claude.com/history.rss", "status"),
    ("OpenAI 新聞", "https://openai.com/news/rss.xml", "blog"),
    ("Google DeepMind", "https://deepmind.google/blog/rss.xml", "blog"),
    ("AWS 機器學習", "https://aws.amazon.com/blogs/machine-learning/feed/", "blog"),
    ("NVIDIA 部落格", "https://blogs.nvidia.com/feed/", "blog"),
    ("Hugging Face", "https://huggingface.co/blog/feed.xml", "blog"),
)
HN_MIN_POINTS = 100
TIMEOUT = (5, 10)
MAX_BYTES = 2_000_000
MAX_TITLE_CHARS = 120
MAX_WHAT_CHARS = 160
DEFAULT_LIMIT = 5
PER_SOURCE = 2
WINDOW_HOURS = (36, 24 * 7)        # 先找最近 36 小時，不夠再放寬到 7 天
MIN_ITEMS = 3

_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")
_TAG = re.compile(r"<[^>]+>")
_IP_HOST = re.compile(r"^[\d.]+$|:")
_PRIVATE_SUFFIXES = (".local", ".internal", ".lan", ".home", ".localdomain")
_AI = re.compile(r"\bAI\b|\bLLMs?\b|GPT|Claude|Gemini|OpenAI|Anthropic|Llama|Mistral|DeepSeek|Qwen|Gemma|"
                 r"diffusion|transformer|inference|agentic|chatbot", re.I)
_SECURITY = re.compile(r"security|vulnerab|cyber|exploit|breach|CVE-|jailbreak|malware|資安|漏洞", re.I)
_MODEL = re.compile(r"\bmodel|GPT|Claude|Gemini|Llama|Gemma|Qwen|Mistral|DeepSeek|LLM|inference|模型", re.I)
_PRODUCT = re.compile(r"launch|introduc|announc|available|release|now |new |api|sdk|agent|推出|發布", re.I)

WHY = {
    "official": "這是官方來源發布的生成式 AI 動態，可能影響團隊採用、開發或維運判斷。",
    "community": "此事件具備公開來源與社群訊號，值得團隊進一步確認影響。",
}
HOW = {
    "服務事件": "確認團隊是否使用受影響的服務，必要時準備備援方案，並持續追蹤官方狀態頁。",
    "安全事件": "先閱讀原始來源，確認是否涉及團隊使用的服務或版本，再評估是否需要調整權限或更新。",
    "新模型": "先閱讀原始來源與技術文件，再評估是否需要進行小規模驗證。",
    "產品更新": "先閱讀原始來源與技術文件，再評估是否需要進行小規模驗證。",
    "產業動態": "先閱讀原始來源，確認與團隊目前的工作是否相關。",
}


def link_problem(url):
    """網址不能用就回傳原因，可以用回傳空字串（只接受 https 的公開網域）。"""
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
    except ValueError:
        return "網址格式不對"
    if parsed.scheme != "https":
        return "只接受 https 網址"
    if not host or "." not in host or _IP_HOST.search(host) or host == "localhost" or host.endswith(_PRIVATE_SUFFIXES):
        return "只接受公開網域，不接受 IP 位址或內網名稱"
    if parsed.username or parsed.password:
        return "網址不可帶帳號密碼"
    return ""


def clean_text(text, limit):
    """去掉 HTML 標籤與控制字元、壓成單行、限制長度。"""
    text = html.unescape(_TAG.sub(" ", text or ""))
    text = " ".join(_CONTROL.sub(" ", text).split())
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def _read_limited(response):
    data = b""
    for chunk in response.iter_content(chunk_size=65536):
        data += chunk
        if len(data) > MAX_BYTES:
            raise ValueError("內容過大")
    return data


def _parse_time(text):
    """RSS 的 RFC 822 或 Atom 的 ISO 8601 時間轉成帶時區的 datetime；失敗回傳 None。"""
    text = (text or "").strip()
    if not text:
        return None
    try:
        value = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        try:
            value = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    return value if value.tzinfo else value.replace(tzinfo=datetime.timezone.utc)


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def parse_feed(data):
    """RSS 或 Atom 內容轉成 [{"title", "summary", "link", "time"}]；
    含 DOCTYPE／ENTITY、非 UTF-8 或格式不對丟 ValueError。"""
    try:
        text = data.decode("utf-8")        # 其他編碼（例如 UTF-16）會讓下面的字串檢查失效，一律不收
    except UnicodeDecodeError:
        raise ValueError("內容不是 UTF-8 編碼") from None
    upper = text.upper()
    if "<!DOCTYPE" in upper or "<!ENTITY" in upper:
        raise ValueError("內容含不允許的 XML 宣告")
    try:
        root = ET.fromstring(data)  # noqa: S314  # nosec B314
    except ET.ParseError:
        raise ValueError("內容格式不對") from None
    items = []
    for node in root.iter():
        if _local(node.tag) not in ("item", "entry"):
            continue
        fields = {}
        for child in node:
            name = _local(child.tag)
            if name == "link":
                fields.setdefault("link", child.get("href") or (child.text or ""))
            elif name in ("title", "description", "summary", "content", "pubDate", "updated", "published"):
                fields.setdefault(name, child.text or "")
        title = clean_text(fields.get("title"), MAX_TITLE_CHARS)
        if title:
            items.append({"title": title,
                          "summary": fields.get("description") or fields.get("summary") or fields.get("content") or "",
                          "link": (fields.get("link") or "").strip(),
                          "time": _parse_time(fields.get("pubDate") or fields.get("published")
                                              or fields.get("updated"))})
    return items


def parse_hn(data):
    """Hacker News（Algolia）的搜尋結果轉成同樣格式的清單（只留標題看得出和 AI 有關的）；格式不對丟 ValueError。"""
    try:
        hits = json.loads(data.decode("utf-8"))["hits"]
    except (UnicodeDecodeError, ValueError, KeyError, TypeError):
        raise ValueError("內容格式不對") from None
    items = []
    for hit in hits if isinstance(hits, list) else []:
        if not isinstance(hit, dict):
            continue
        title = clean_text(str(hit.get("title") or ""), MAX_TITLE_CHARS)
        points, comments = hit.get("points"), hit.get("num_comments") or 0
        stamp = hit.get("created_at_i")
        if not title or not _AI.search(title) or not isinstance(points, int) or not isinstance(comments, int):
            continue
        when = (datetime.datetime.fromtimestamp(stamp, datetime.timezone.utc) if isinstance(stamp, int) else None)
        items.append({"title": title, "summary": f"Hacker News：{points} 分，{comments} 則留言。",
                      "link": str(hit.get("url") or ""), "time": when})
    return items


def _get(url, get):
    response = get(url, timeout=TIMEOUT, allow_redirects=False, stream=True,
                   headers={"User-Agent": "Mozilla/5.0 (SmartAccessAgent)"})
    if response.status_code != 200:
        raise ValueError(f"來源回應 {response.status_code}")
    return _read_limited(response)


def _load_source(source, get):
    name, url, kind = source
    try:
        if kind == "hn":
            return name, kind, parse_hn(_get(url, get)), ""
        return name, kind, parse_feed(_get(url, get)), ""
    except requests.RequestException as e:
        return name, kind, [], f"{name}：連線失敗（{type(e).__name__}）"
    except ValueError as e:
        return name, kind, [], f"{name}：{e}"


def hn_source(now):
    since = int((now - datetime.timedelta(hours=WINDOW_HOURS[-1])).timestamp())
    url = (f"https://hn.algolia.com/api/v1/search_by_date?tags=story"
           f"&numericFilters={quote(f'points>{HN_MIN_POINTS},created_at_i>{since}')}&hitsPerPage=100")
    return ("Hacker News", url, "hn")


def categorize(title, kind):
    """服務狀態一律是服務事件；其餘依標題關鍵字：資安、模型、產品，都不是就歸產業動態。"""
    if kind == "status":
        return "服務事件"
    if _SECURITY.search(title):
        return "安全事件"
    if _MODEL.search(title):
        return "新模型"
    if _PRODUCT.search(title):
        return "產品更新"
    return "產業動態"


def _to_card(item, name, kind):
    category = categorize(item["title"], kind)
    link = item["link"] if item["link"] and not link_problem(item["link"]) else ""
    what = clean_text(item["summary"], MAX_WHAT_CHARS) or "（來源沒有提供摘要，請看原文）"
    return {"category": category, "title": item["title"], "what": what,
            "why": WHY["community" if kind == "hn" else "official"], "how": HOW[category],
            "link": link, "source": name, "time": item["time"]}


def pick(loaded, now, limit=DEFAULT_LIMIT):
    """挑出最近的幾則：先看 36 小時內，不夠 MIN_ITEMS 則放寬到 7 天；每個來源最多 PER_SOURCE 則，新的在前。"""
    for hours in WINDOW_HOURS:
        cutoff = now - datetime.timedelta(hours=hours)
        cards = []
        for name, kind, items, _problem in loaded:
            recent = sorted((i for i in items if i["time"] and i["time"] >= cutoff),
                            key=lambda i: i["time"], reverse=True)
            cards += [_to_card(i, name, kind) for i in recent[:PER_SOURCE]]
        cards.sort(key=lambda c: c["time"], reverse=True)
        if len(cards) >= MIN_ITEMS or hours == WINDOW_HOURS[-1]:
            seen, unique = set(), []
            for c in cards:
                if c["title"] not in seen:
                    seen.add(c["title"])
                    unique.append(c)
            return unique[:limit]
    return []


def fetch_ai_news(now=None, get=requests.get, limit=DEFAULT_LIMIT):
    """回傳 (卡片清單, 原因)；原因是各來源失敗的說明（全部成功是空字串）。"""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    sources = list(RSS_SOURCES) + [hn_source(now)]
    with ThreadPoolExecutor(max_workers=len(sources)) as pool:
        loaded = list(pool.map(lambda s: _load_source(s, get), sources))
    return pick(loaded, now, limit), "；".join(p for *_rest, p in loaded if p)


def format_news(cards, day=None):
    """排成早報文字：標題行加每則的 類別、發生什麼事、為什麼重要、可以怎麼用、來源與原文；day 是早報日期。"""
    day = day or datetime.date.today()
    lines = [f"生成式 AI 早報｜{day.strftime('%Y/%m/%d')}"]
    for c in cards:
        lines += ["", f"[{c['category']}] {c['title']}", f"發生什麼事：{c['what']}", f"為什麼重要：{c['why']}",
                  f"可以怎麼用：{c['how']}", f"來源與原文：{c['link'] or '（沒有可用的原文連結）'}"]
    return "\n".join(lines)

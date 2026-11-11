"""
news_feed.py：晨報的科技新聞來源（繁中與國際科技媒體、AI 官方部落格、Hacker News 上高分的討論），整理成固定的四欄格式。

主要功能：
  - fetch_ai_news：同時抓固定的幾個來源（iThome、科技報橘、電腦王阿達、The Verge、Ars Technica、TechCrunch、
    OpenAI／DeepMind／NVIDIA／Hugging Face／AWS 機器學習的官方部落格、Hacker News 高分討論），挑出最近的幾則，
    每則整理成 類別、標題、發生什麼事、為什麼重要、可以怎麼用、來源與原文；
    只有一部分來源失敗也照常出報，失敗原因另外回傳，不讓晨報中斷
  - 暫時性的連線問題（網路剛連上、短暫中斷）每個來源先在原地重試，仍失敗才記成失敗；
    整份晨報抓不到新聞時由 morning_brief／digest_window 排程稍後再抓
  - 類別、為什麼重要、可以怎麼用都是用標題關鍵字決定的固定句子，不是 AI 判斷；
    「發生什麼事」是來源自己的摘要（去掉 HTML、限制長度）；國際來源是英文原文，不翻譯
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
import time
# 標準函式庫的 XML 解析器擋不住實體膨脹；parse_feed 在解析前已拒收 DOCTYPE／ENTITY，且讀取有大小上限
import xml.etree.ElementTree as ET  # nosec B405
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

import requests

from shared import link_check, network_policy

OFFLINE_NOTE = "全本地模式已開啟，沒有連網抓新聞"

# (顯示名稱, 網址, 種類)；種類：news 科技媒體、blog 官方部落格
RSS_SOURCES = (
    ("iThome", "https://www.ithome.com.tw/rss", "news"),
    ("科技報橘", "https://techorange.com/feed/", "news"),
    ("電腦王阿達", "https://www.kocpc.com.tw/feed", "news"),
    ("The Verge", "https://www.theverge.com/rss/index.xml", "news"),
    ("Ars Technica", "https://feeds.arstechnica.com/arstechnica/index", "news"),
    ("TechCrunch", "https://techcrunch.com/feed/", "news"),
    ("OpenAI 新聞", "https://openai.com/news/rss.xml", "blog"),
    ("Google DeepMind", "https://deepmind.google/blog/rss.xml", "blog"),
    ("AWS 機器學習", "https://aws.amazon.com/blogs/machine-learning/feed/", "blog"),
    ("NVIDIA 部落格", "https://blogs.nvidia.com/feed/", "blog"),
    ("Hugging Face", "https://huggingface.co/blog/feed.xml", "blog"),
)
RETRY_DELAYS = (1.5, 4.0)          # 秒；連線失敗時在原地重試的等待時間
HN_MIN_POINTS = 100
TIMEOUT = (5, 10)
MAX_BYTES = 2_000_000
MAX_TITLE_CHARS = 120
MAX_WHAT_CHARS = 160
DEFAULT_LIMIT = 6
PER_SOURCE = 2
WINDOW_HOURS = 24 * 7              # 只收最近 7 天內的
HN_PENALTY = 2                     # Hacker News 的名次往後延，只在其他來源不夠時補位

_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")
_TAG = re.compile(r"<[^>]+>")
_AI = re.compile(r"\bAI\b|\bLLMs?\b|GPT|Claude|Gemini|OpenAI|Anthropic|Llama|Mistral|DeepSeek|Qwen|Gemma|"
                 r"diffusion|transformer|inference|agentic|chatbot|\bmodel|人工智慧|生成式|大型語言|模型|機器學習",
                 re.I)
_SECURITY = re.compile(r"security|vulnerab|cyber|exploit|breach|CVE-|jailbreak|malware|ransomware|"
                       r"\bhack(?:ed|ers?|ing)?\b|資安|漏洞|駭客|惡意|勒索|攻擊|外洩", re.I)
_HARDWARE = re.compile(r"\bchips?\b|\bGPUs?\b|\bCPUs?\b|semiconductor|processor|laptop|iPhone|MacBook|Android|"
                       r"Snapdragon|\bRAM\b|\bSSD\b|晶片|處理器|半導體|顯卡|筆電|手機|台積電|記憶體", re.I)
_SOFTWARE = re.compile(r"open.?source|Linux|\bRust\b|\bPython\b|developer|GitHub|framework|kernel|compiler|"
                       r"\bAPI\b|\bSDK\b|開源|開發者|程式|作業系統|瀏覽器", re.I)

WHY = {
    "AI": "生成式 AI 與模型的進展，可能影響團隊的工具選擇與開發方式。",
    "資安": "涉及漏洞或攻擊事件，可能影響正在使用的系統與服務。",
    "硬體與晶片": "硬體與晶片的變化會影響設備採購與效能規劃。",
    "軟體與開發": "開發工具與開源生態的變化，可能影響團隊的技術選型。",
    "產業動態": "科技產業的動態，可作為了解趨勢的參考。",
}
HOW = {
    "AI": "先閱讀原文與官方文件，再評估是否值得小規模試用。",
    "資安": "確認團隊使用的產品或版本是否受影響，必要時更新或調整權限。",
    "硬體與晶片": "確認是否與目前使用或預計採購的設備相關。",
    "軟體與開發": "閱讀原文與版本說明，評估是否需要升級或試用。",
    "產業動態": "閱讀原文，判斷與目前進行的專案是否相關。",
}


link_problem = link_check.link_problem        # 網址檢查搬到 shared.link_check（前台也要用），這裡保留同名


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
    """Hacker News（Algolia）的搜尋結果轉成同樣格式的清單；格式不對丟 ValueError。"""
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
        if not title or not isinstance(points, int) or not isinstance(comments, int):
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


def _load_source(source, get, sleep=time.sleep):
    """抓一個來源；連線類的暫時性錯誤先在原地重試，內容或回應狀態的問題不重試。"""
    name, url, kind = source
    for attempt in range(len(RETRY_DELAYS) + 1):
        try:
            if kind == "hn":
                return name, kind, parse_hn(_get(url, get)), ""
            return name, kind, parse_feed(_get(url, get)), ""
        except requests.RequestException as e:
            if attempt < len(RETRY_DELAYS):
                sleep(RETRY_DELAYS[attempt])
                continue
            return name, kind, [], f"{name}：連線失敗（{type(e).__name__}）"
        except ValueError as e:
            return name, kind, [], f"{name}：{e}"


def hn_source(now):
    since = int((now - datetime.timedelta(hours=WINDOW_HOURS)).timestamp())
    url = (f"https://hn.algolia.com/api/v1/search_by_date?tags=story"
           f"&numericFilters={quote(f'points>{HN_MIN_POINTS},created_at_i>{since}')}&hitsPerPage=100")
    return ("Hacker News", url, "hn")


def categorize(title):
    """依標題關鍵字分類：資安、AI、硬體與晶片、軟體與開發，都不是就歸產業動態。"""
    for pattern, category in ((_SECURITY, "資安"), (_AI, "AI"), (_HARDWARE, "硬體與晶片"), (_SOFTWARE, "軟體與開發")):
        if pattern.search(title):
            return category
    return "產業動態"


def _to_card(item, name):
    category = categorize(item["title"])
    link = item["link"] if item["link"] and not link_problem(item["link"]) else ""
    what = clean_text(item["summary"], MAX_WHAT_CHARS) or "（來源沒有提供摘要，請看原文）"
    return {"category": category, "title": item["title"], "what": what,
            "why": WHY[category], "how": HOW[category],
            "link": link, "source": name, "time": item["time"]}


def pick(loaded, now, limit=DEFAULT_LIMIT):
    """挑出最近 WINDOW_HOURS 小時內的幾則：每個來源最新的一則優先，不夠再補各來源第二新的，
    Hacker News 排在最後補位（只有標題與分數，資訊最少）；重複標題只留一則，最後依時間新的在前。"""
    cutoff = now - datetime.timedelta(hours=WINDOW_HOURS)
    ranked = []
    for name, kind, items, _problem in loaded:
        recent = sorted((i for i in items if i["time"] and i["time"] >= cutoff),
                        key=lambda i: i["time"], reverse=True)
        ranked += [(rank + (HN_PENALTY if kind == "hn" else 0), -item["time"].timestamp(), name, item)
                   for rank, item in enumerate(recent[:PER_SOURCE])]
    ranked.sort(key=lambda r: (r[0], r[1]))
    seen, cards = set(), []
    for _rank, _age, name, item in ranked:
        if item["title"] in seen:
            continue
        seen.add(item["title"])
        cards.append(_to_card(item, name))
        if len(cards) >= limit:
            break
    cards.sort(key=lambda c: c["time"], reverse=True)
    return cards


def fetch_ai_news(now=None, get=requests.get, limit=DEFAULT_LIMIT, sleep=time.sleep):
    """回傳 (卡片清單, 原因)；原因是各來源失敗的說明（全部成功是空字串）；全本地模式不連網，直接回傳空清單。"""
    if network_policy.local_only():
        return [], OFFLINE_NOTE
    now = now or datetime.datetime.now(datetime.timezone.utc)
    sources = list(RSS_SOURCES) + [hn_source(now)]
    with ThreadPoolExecutor(max_workers=len(sources)) as pool:
        loaded = list(pool.map(lambda src: _load_source(src, get, sleep), sources))
    return pick(loaded, now, limit), "；".join(p for *_rest, p in loaded if p)


def format_news(cards, day=None):
    """排成早報文字：標題行加每則的 類別、發生什麼事、為什麼重要、可以怎麼用、來源與原文；day 是早報日期。"""
    day = day or datetime.date.today()
    lines = [f"科技早報｜{day.strftime('%Y/%m/%d')}"]
    for c in cards:
        lines += ["", f"[{c['category']}] {c['title']}", f"發生什麼事：{c['what']}", f"為什麼重要：{c['why']}",
                  f"可以怎麼用：{c['how']}", f"來源與原文：{c['link'] or '（沒有可用的原文連結）'}"]
    return "\n".join(lines)

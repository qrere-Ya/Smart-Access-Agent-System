"""
news_feed.py：抓新聞標題（RSS）給晨報用，只取標題、來源與時間，內容一律當作不可信的純文字。

主要功能：
  - fetch_headlines：從設定的 RSS 網址抓標題；只接受 https 與公開網域（不接受 IP、localhost、內網名稱）、
    不跟隨轉址、逾時 10 秒、最多讀 1MB；抓失敗或格式不對回傳空清單與一句原因，不讓晨報中斷
  - 解析前先拒絕含 DOCTYPE／ENTITY 的內容（防 XML 實體膨脹與外部實體），再用標準函式庫解析
  - 標題清掉控制字元、壓成單行、限制長度，同標題只留一則；新聞文字不會交給語言模型，也不會變成可點的連結
  預設來源是 Google 新聞的台灣焦點新聞；可在 runtime/digest_settings.json 的 news_feeds 改成自己的網址清單

串接模組：
  - shared.digest_store：讀取來源設定；morning_brief：晨報呼叫 fetch_headlines
"""
import re
# 標準函式庫的 XML 解析器擋不住實體膨脹；parse_rss 在解析前已拒收 DOCTYPE／ENTITY，且讀取有大小上限
import xml.etree.ElementTree as ET  # nosec B405
from urllib.parse import urlparse

import requests

DEFAULT_FEEDS = ["https://news.google.com/rss?hl=zh-TW&gl=TW&ceid=TW:zh-Hant"]
TIMEOUT_SECONDS = 10
MAX_BYTES = 1_000_000
MAX_TITLE_CHARS = 120
DEFAULT_LIMIT = 8
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")
_IP_HOST = re.compile(r"^[\d.]+$|:")
_PRIVATE_SUFFIXES = (".local", ".internal", ".lan", ".home", ".localdomain")


def feed_problem(url):
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


def _clean(text):
    text = " ".join(_CONTROL.sub(" ", text or "").split())
    return text[:MAX_TITLE_CHARS]


def _read_limited(response):
    """最多讀 MAX_BYTES；超過就當作失敗。"""
    data = b""
    for chunk in response.iter_content(chunk_size=65536):
        data += chunk
        if len(data) > MAX_BYTES:
            raise ValueError("內容過大")
    return data


def parse_rss(data):
    """RSS 內容轉成 [{"title", "time"}]；含 DOCTYPE／ENTITY 或格式不對丟 ValueError。"""
    try:
        text = data.decode("utf-8")        # 其他編碼（例如 UTF-16）會讓下面的字串檢查失效，一律不收
    except UnicodeDecodeError:
        raise ValueError("RSS 不是 UTF-8 編碼") from None
    upper = text.upper()
    if "<!DOCTYPE" in upper or "<!ENTITY" in upper:
        raise ValueError("內容含不允許的 XML 宣告")
    try:
        root = ET.fromstring(data)  # noqa: S314  # nosec B314
    except ET.ParseError:
        raise ValueError("RSS 格式不對") from None
    items = []
    for item in root.iter("item"):
        title = _clean(item.findtext("title"))
        if title:
            items.append({"title": title, "time": _clean(item.findtext("pubDate"))})
    return items


def fetch_headlines(feeds=None, limit=DEFAULT_LIMIT, get=requests.get):
    """回傳 (標題清單, 原因)；標題清單最多 limit 則，原因是空字串代表沒有問題。"""
    feeds = list(feeds) if feeds else DEFAULT_FEEDS
    seen, headlines, problems = set(), [], []
    for url in feeds:
        why = feed_problem(url)
        if why:
            problems.append(why)
            continue
        try:
            response = get(url, timeout=TIMEOUT_SECONDS, allow_redirects=False, stream=True,
                           headers={"User-Agent": "Mozilla/5.0 (SmartAccessAgent)"})
            if response.status_code != 200:
                problems.append(f"來源回應 {response.status_code}")
                continue
            items = parse_rss(_read_limited(response))
        except requests.RequestException as e:
            problems.append(f"連線失敗（{type(e).__name__}）")
            continue
        except ValueError as e:
            problems.append(str(e))
            continue
        for item in items:
            if item["title"] not in seen:
                seen.add(item["title"])
                headlines.append(item)
    return headlines[:limit], "；".join(dict.fromkeys(problems))

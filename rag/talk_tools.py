"""
talk_tools.py：Smart_Chat 內建 MCP 伺服器（rag/mcp_servers）背後的免費查詢函式——天氣、新聞、維基百科。

主要功能：
  - weather(city)：Open-Meteo 地理編碼取得座標，wttr.in 依座標查預報（都免金鑰），回傳現在與未來三天的天氣文字
  - news(query)：Google 新聞 RSS（最新消息，免金鑰）；wikipedia(query)：維基百科搜尋（一般知識，免金鑰）
  - page(url)：抓公開的 https 網頁並取出文字（擋內網位址與非 https、逐次檢查轉址）
  - 全部免費、不用任何付費 API 或金鑰
  - 安全：網址主機寫死白名單、只用 https、不跟隨轉址、不吃系統代理、回應大小與逾時有上限、XML 拒收 DOCTYPE／ENTITY；
    失敗只丟 ToolError（一句可以給使用者看的說明，不含網址或例外內容）

串接模組：rag.mcp_servers.weather_server、search_server（把這些函式包成 MCP 工具）；rag.mcp_client（用戶端與錯誤型別）。
"""
import html
import ipaddress
import json
import re
import socket
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import requests

from shared import link_check


TIMEOUT = (4.0, 10.0)
MAX_BYTES = 1_500_000
MAX_RESULT_CHARS = 1800
USER_AGENT = "SmartAccessAgent/1.0 (office kiosk assistant)"
ALLOWED_HOSTS = {"geocoding-api.open-meteo.com", "wttr.in", "news.google.com", "zh.wikipedia.org",
                 "mis.twse.com.tw", "openapi.twse.com.tw"}


class ToolError(Exception):
    """工具失敗（只帶一句可以給使用者看的說明，不含網址或例外內容）。"""


def _get(url, params=None):
    host = urlparse(url).hostname or ""
    if not url.startswith("https://") or host not in ALLOWED_HOSTS:
        raise ToolError("不允許的網址")
    session = requests.Session()
    session.trust_env = False
    try:
        response = session.get(url, params=params, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT,
                               allow_redirects=False, stream=True)
        if response.status_code != 200:
            raise ToolError("查詢服務暫時沒有回應")
        body = response.raw.read(MAX_BYTES + 1, decode_content=True)
    except requests.RequestException:
        raise ToolError("查詢服務暫時連不上") from None
    finally:
        session.close()
    if len(body) > MAX_BYTES:
        raise ToolError("查詢結果太大")
    return body


def _json(url, **kwargs):
    try:
        return json.loads(_get(url, **kwargs).decode("utf-8", errors="replace"))
    except ValueError:
        raise ToolError("查詢結果格式不對") from None


def _find_place(city):
    """地名查座標：短地名（台北、高雄）常查不到或對到別國，所以依序試原名與加「市」，結果裡台灣的優先。"""
    names = [city] + ([city + "市"] if len(city) <= 3 and not city.endswith(("市", "縣")) else [])
    found = []
    for name in names:
        results = _json("https://geocoding-api.open-meteo.com/v1/search",
                        params={"name": name, "count": 5, "language": "zh"}).get("results") or []
        found += [r for r in results if "latitude" in r and "longitude" in r]
        if any(r.get("country_code") == "TW" for r in found):
            break
    return next((r for r in found if r.get("country_code") == "TW"), found[0] if found else None)


def weather(city):
    """城市天氣（現在加三天）：Open-Meteo 地理編碼取得座標，再用 wttr.in 依座標查預報。"""
    city = " ".join(str(city or "").split())[:40]
    if not city:
        raise ToolError("沒有指定城市")
    place = _find_place(city)
    if place is None:
        raise ToolError(f"找不到「{city}」這個地點")
    data = _json(f"https://wttr.in/{place['latitude']:.3f},{place['longitude']:.3f}", params={"format": "j1"})
    now = (data.get("current_condition") or [{}])[0]
    name = "、".join(x for x in (place.get("name"), place.get("admin1"), place.get("country")) if x)
    lines = [f"地點：{name}",
             f"現在：{_desc(now)}，{now.get('temp_C')}°C（體感 {now.get('FeelsLikeC')}°C），"
             f"濕度 {now.get('humidity')}%，風速 {now.get('windspeedKmph')} km/h，降雨 {now.get('precipMM')} mm"]
    for day in (data.get("weather") or [])[:3]:
        hours = day.get("hourly") or []
        rain = max([int(h.get("chanceofrain", 0) or 0) for h in hours] or [0])
        noon = hours[4] if len(hours) > 4 else {}
        lines.append(f"{day.get('date')}：{_desc(noon)}，{day.get('mintempC')}～{day.get('maxtempC')}°C，"
                     f"降雨機率 {rain}%")
    return "\n".join(lines)


def _desc(block):
    return ((block.get("weatherDesc") or [{}])[0]).get("value", "未知")


_STOCK_CODE = re.compile(r"^[0-9]{4,6}[A-Z]?$")


def _stock_code(query):
    """股票代號（如 2330）直接用；中文名稱用證交所公開資料找上市公司代號，找不到就請使用者改用代號。"""
    query = " ".join(str(query).split())[:30]
    if _STOCK_CODE.match(query.upper()):
        return query.upper()
    if not query:
        raise ToolError("請提供股票代號或公司名稱")
    rows = _json("https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL")
    exact = [r for r in rows if r.get("Name") == query]
    near = exact or [r for r in rows if query in str(r.get("Name", ""))]
    if not near:
        raise ToolError("找不到這家公司（只能用名稱查上市公司，上櫃或興櫃請改用股票代號）")
    return str(near[0]["Code"])


def _number(text):
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _plain(text):
    value = _number(text)
    return f"{value:g}" if value is not None else "-"


def stock(query):
    """台股即時報價（證交所公開行情，免費免金鑰；上市與上櫃代號都試）；收盤後是當天收盤價，附成交時間。"""
    code = _stock_code(query)
    data = _json("https://mis.twse.com.tw/stock/api/getStockInfo.jsp",
                 params={"ex_ch": f"tse_{code}.tw|otc_{code}.tw", "json": "1", "delay": "0"})
    for row in data.get("msgArray") or []:
        price, previous = _number(row.get("z")), _number(row.get("y"))
        if price is None:
            price = _number(str(row.get("a", "")).split("_")[0])      # 還沒成交：用最佳賣價參考
        if price is None or not row.get("n"):
            continue
        diff = price - previous if previous else None
        change = f"，較前一日收盤{diff:+.2f}（{diff / previous * 100:+.2f}%）" if previous else ""
        when = f"{_clean(row.get('d'), 8)} {_clean(row.get('t'), 8)}"
        day = f"開 {_plain(row.get('o'))} 高 {_plain(row.get('h'))} 低 {_plain(row.get('l'))}"
        return (f"{_clean(row['n'], 20)}（{code}）成交價 {price:g} 元{change}；{day}，累計量 {row.get('v')} 張；"
                f"資料時間 {when}（證交所行情，可能有數秒到數分鐘延遲）")
    raise ToolError("查不到這個代號的行情（可能不是台股，或代號有誤）")


def _clean(text, limit=200):
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", str(text or ""))).split())[:limit]


def news(query):
    body = _get("https://news.google.com/rss/search",
                params={"q": query, "hl": "zh-TW", "gl": "TW", "ceid": "TW:zh-Hant"})
    if re.search(rb"<!DOCTYPE|<!ENTITY", body[:2000], re.I):
        raise ToolError("查詢結果格式不對")
    try:
        root = ET.fromstring(body)  # noqa: S314  # nosec B314  上面已拒收 DOCTYPE／ENTITY，主機也在白名單內
    except ET.ParseError:
        raise ToolError("查詢結果格式不對") from None
    out = []
    for item in list(root.iter("item"))[:5]:
        out.append(f"- {_clean(item.findtext('title'))}（{_clean(item.findtext('pubDate'), 30)}）")
    return out


def wikipedia(query):
    data = _json("https://zh.wikipedia.org/w/api.php", params={
        "action": "query", "list": "search", "srsearch": query, "format": "json", "srlimit": 3})
    hits = (data.get("query") or {}).get("search", [])
    return [f"- {_clean(r.get('title'), 60)}：{_clean(r.get('snippet'))}" for r in hits]


# --- 讀網頁：使用者（或模型）給網址，後台代為抓文字；不是白名單主機，所以另外把關 ---

MAX_PAGE_CHARS = 3000
MAX_REDIRECTS = 3
_SKIP_TAGS = {"script", "style", "noscript", "svg", "head", "template", "iframe"}


class _TextExtractor(HTMLParser):
    """只留看得到的文字：略過 script、style 等標籤裡的內容。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self._skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip and data.strip():
            self.parts.append(data.strip())


def _public_host_problem(url):
    """網址不能抓的原因（空字串表示可以）：只收 https 公開網域，而且網域實際解析到的位址也都要是公開位址（擋內網）。"""
    problem = link_check.link_problem(url)
    if problem:
        return problem
    host = urlparse(url).hostname
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except OSError:
        return "網域解析不到"
    for info in infos:
        if not ipaddress.ip_address(info[4][0]).is_global:
            return "這個網址指向內部位址"
    return ""


def page(url):
    """抓網頁並取出文字（只收 https 公開網域、逐次檢查轉址、不吃系統代理、大小與逾時有上限）。"""
    url = str(url or "").strip()[:500]
    session = requests.Session()
    session.trust_env = False
    try:
        for _hop in range(MAX_REDIRECTS + 1):
            if _public_host_problem(url):
                raise ToolError("這個網址不能讀取（只支援公開的 https 網頁）")
            response = session.get(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT, allow_redirects=False,
                                   stream=True)
            if response.status_code in (301, 302, 303, 307, 308):
                url = urljoin(url, response.headers.get("Location", ""))
                continue
            break
        else:
            raise ToolError("網頁轉址太多次")
        if response.status_code != 200:
            raise ToolError("網頁暫時沒有回應")
        kind = response.headers.get("Content-Type", "").split(";")[0].strip().lower()
        if kind not in ("text/html", "text/plain", "application/xhtml+xml"):
            raise ToolError("只能讀文字網頁")
        body = response.raw.read(MAX_BYTES + 1, decode_content=True)
    except requests.RequestException:
        raise ToolError("網頁暫時連不上") from None
    finally:
        session.close()
    text = body[:MAX_BYTES].decode(response.encoding or "utf-8", errors="replace")
    if kind != "text/plain":
        extractor = _TextExtractor()
        extractor.feed(text)
        text = "\n".join(extractor.parts)
    text = " ".join(text.split())
    if not text:
        raise ToolError("網頁沒有可讀的文字")
    return text[:MAX_PAGE_CHARS]

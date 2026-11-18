"""
talk_tools.py：Smart_Chat 內建 MCP 伺服器（rag/mcp_servers）背後的免費查詢函式——天氣、新聞、維基百科。

主要功能：
  - weather(city)：Open-Meteo 地理編碼取得座標，wttr.in 依座標查預報（都免金鑰），回傳現在與未來三天的天氣文字
  - news(query)：Google 新聞 RSS（最新消息，免金鑰）；wikipedia(query)：維基百科搜尋（一般知識，免金鑰）
  - 全部免費、不用任何付費 API 或金鑰
  - 安全：網址主機寫死白名單、只用 https、不跟隨轉址、不吃系統代理、回應大小與逾時有上限、XML 拒收 DOCTYPE／ENTITY；
    失敗只丟 ToolError（一句可以給使用者看的說明，不含網址或例外內容）

串接模組：rag.mcp_servers.weather_server、search_server（把這些函式包成 MCP 工具）；rag.mcp_client（用戶端與錯誤型別）。
"""
import html
import json
import re
import xml.etree.ElementTree as ET
from urllib.parse import urlparse

import requests


TIMEOUT = (4.0, 10.0)
MAX_BYTES = 1_500_000
MAX_RESULT_CHARS = 1800
USER_AGENT = "SmartAccessAgent/1.0 (office kiosk assistant)"
ALLOWED_HOSTS = {"geocoding-api.open-meteo.com", "wttr.in", "news.google.com", "zh.wikipedia.org"}

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

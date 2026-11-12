"""
link_check.py：外部網址能不能給使用者（只接受 https 的公開網域）——純邏輯、只用標準函式庫，後台與前台共用。

主要功能：
  - link_problem(url)：網址不能用就回傳原因，可以用回傳空字串；只收 https、拒絕 IP 位址、localhost、內網後綴、帶帳號密碼
  - 後台用它決定晨報裡哪些原文連結可以顯示；前台用它決定哪些新聞可以做成 QR Code 給手機掃

串接模組：
  - admin.news_feed（抓新聞時過濾原文連結）、admin.digest_window（日報視窗可點的連結）、access.front_view（前台 QR）
"""
import re
from urllib.parse import urlparse

_IP_HOST = re.compile(r"^[\d.]+$|:")
_HOST_CHARS = re.compile(r"^[a-z0-9.-]+$")          # 網域只允許這些字元（空白、底線、非 ASCII 都不收）
_PRIVATE_SUFFIXES = (".local", ".internal", ".lan", ".home", ".localdomain")


def link_problem(url):
    """網址不能用就回傳原因，可以用回傳空字串（只接受 https 的公開網域）。"""
    try:
        parsed = urlparse(str(url or ""))
        host = (parsed.hostname or "").lower()
    except ValueError:
        return "網址格式不對"
    if parsed.scheme != "https":
        return "只接受 https 網址"
    if not host or "." not in host or _IP_HOST.search(host) or host == "localhost" or host.endswith(_PRIVATE_SUFFIXES):
        return "只接受公開網域，不接受 IP 位址或內網名稱"
    if not _HOST_CHARS.match(host):
        return "網域含有不允許的字元"
    if parsed.username or parsed.password:
        return "網址不可帶帳號密碼"
    return ""

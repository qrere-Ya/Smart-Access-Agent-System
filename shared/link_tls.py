"""
link_tls.py：後台與前台之間加密連線（TLS）用的共用部分——憑證指紋與 TLS 設定（只用標準函式庫，
設定工具在沒裝其他套件的 Python 也能跑）。

主要功能：
  - 指紋：憑證 DER 的 SHA-256（小寫 64 字元十六進位）；fingerprint_of_pem 從憑證檔算，normalize 接受冒號、空白與大小寫
  - server_context：後台用的 TLS 設定（TLS 1.2 以上，載入憑證與私鑰）
  - 前台的「只信任這個指紋」連線轉接器在 access.front_agent（PinnedAdapter）；不驗證憑證機構與主機名稱，
    直連網路線沒有公認的憑證機構，信任完全來自指紋，指紋由 lan_discovery 的簽章回覆或手動設定取得
  - CertMismatch：指紋不符時的例外，讓前台能和一般離線分開處理

限制：憑證與私鑰都在後台的 runtime/；私鑰被讀走就能冒充後台，要靠後台機器本身的安全；
通行碼外洩的人可以讓前台信任他的憑證。

串接模組：
  - admin.link_cert（產生憑證）、admin.link_server（伺服器端）、access.front_agent（BackClient）、
    shared.lan_discovery（把指紋放進簽章回覆）、shared.role_config（手動模式的指紋設定）
"""
import hashlib
import re
import ssl

_FINGERPRINT = re.compile(r"[0-9a-f]{64}")


class CertMismatch(Exception):
    """後台出示的憑證指紋和前台信任的不一樣。"""


def normalize(value):
    """把指紋整理成小寫 64 字元十六進位；格式不對回傳空字串。"""
    text = re.sub(r"[\s:]", "", str(value or "")).lower()
    return text if _FINGERPRINT.fullmatch(text) else ""


def fingerprint_of_der(der_bytes):
    return hashlib.sha256(der_bytes).hexdigest()


def fingerprint_of_pem(path):
    """憑證檔（PEM）的指紋。"""
    with open(path, encoding="ascii") as f:
        return fingerprint_of_der(ssl.PEM_cert_to_DER_cert(f.read()))


def server_context(cert_path, key_path):
    """後台的 TLS 設定：TLS 1.2 以上。"""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cert_path, key_path)
    return context


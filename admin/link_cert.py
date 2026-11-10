"""
link_cert.py：後台的連線憑證——第一次啟動時產生自簽憑證與私鑰，之後沿用（只用在後台，需要 cryptography）。

主要功能：
  - ensure_certificate：runtime/link_tls/ 底下沒有憑證、憑證檔壞掉或快到期（少於 30 天）就重新產生；
    私鑰只存在這裡，不進版控、不進前台安裝包；寫入時先寫暫存檔再取代，不會留下半個檔案
  - 憑證本身沒有身分意義（前台不驗憑證機構與主機名稱），信任來自指紋（shared.link_tls），所以主體名稱固定、有效期 10 年
  - 重新產生憑證後指紋會變，前台（自動模式）重新探測就會拿到新的指紋

串接模組：
  - admin.link_server：啟動時取得憑證；shared.link_tls：指紋與 TLS 設定；shared.paths：LINK_TLS_DIR
"""
import datetime
import os

from shared import link_tls, paths

CERT_NAME, KEY_NAME = "link_cert.pem", "link_key.pem"
VALID_DAYS = 3650
RENEW_BEFORE_DAYS = 30


def _usable(cert_path, key_path, now):
    """憑證與私鑰都在、讀得出來、離到期還有足夠時間。"""
    try:
        from cryptography import x509
        with open(cert_path, "rb") as f:
            cert = x509.load_pem_x509_certificate(f.read())
        link_tls.server_context(cert_path, key_path)          # 憑證與私鑰要成對、格式要對
        return cert.not_valid_after_utc - now > datetime.timedelta(days=RENEW_BEFORE_DAYS)
    except Exception:  # noqa: BLE001  任何讀取或配對失敗都當作要重新產生
        return False


def _generate(cert_path, key_path, now):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Smart Access Back")])
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=VALID_DAYS))
            .sign(key, hashes.SHA256()))
    key_pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
    for path, data in ((key_path, key_pem), (cert_path, cert.public_bytes(serialization.Encoding.PEM))):
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)


def ensure_certificate(directory=None, now=None):
    """回傳 (憑證路徑, 私鑰路徑)；必要時產生。"""
    directory = directory or paths.LINK_TLS_DIR
    os.makedirs(directory, exist_ok=True)
    cert_path, key_path = os.path.join(directory, CERT_NAME), os.path.join(directory, KEY_NAME)
    now = now or datetime.datetime.now(datetime.timezone.utc)
    if not _usable(cert_path, key_path, now):
        _generate(cert_path, key_path, now)
    return cert_path, key_path

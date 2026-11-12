"""
role_config.py：單機、後台、前台三種角色與兩台電腦之間的連線設定（純邏輯，沒有網路連線）。

主要功能：
  - 角色：standalone（單機，預設，行為與以前完全相同）、back（後台：唯一的資料庫與伺服器）、front（前台：門口那台，
    辨識在本機做，事件送回後台，不上網）。環境變數 SMART_ACCESS_ROLE 優先，其次 runtime/role.json，沒設就是單機
  - 連線設定：後台位址與埠（前台連過去用）、後台綁定的網卡位址與埠（前台不開對外服務，狀態由前台主動回報）
  - 位址規則：只接受內網 IPv4（10.x、172.16–31.x、192.168.x、169.254.x、127.x），不接受網域名稱與公開位址，
    所以不會被設成連到外網；後台綁定位址不得是 0.0.0.0（萬用位址），要指定網路線那張網卡的 IP，
    或填 auto 由 lan_discovery 自動找（169.254.x.x）
  - 通行碼（token）只走環境變數 SMART_ACCESS_LINK_TOKEN，不寫進任何檔案、不印出；至少 32 字元；比對用常數時間
  - 命令列：python -m shared.role_config show／set／token（token 只在產生時印一次，請自己存進環境變數）

串接模組：
  - 之後的後台服務（L2）、前台代理（L3）、終端機與營運狀態頁（L4、L5）都從這裡讀角色與位址
"""
import argparse
import hmac
import ipaddress
import json
import os
import re
import secrets
from dataclasses import asdict, dataclass

from shared import link_tls, paths

ROLE_ENV = "SMART_ACCESS_ROLE"
TOKEN_ENV = "SMART_ACCESS_LINK_TOKEN"  # noqa: S105  # nosec B105  環境變數名稱，不是密碼
STANDALONE, BACK, FRONT = "standalone", "back", "front"
ROLES = (STANDALONE, BACK, FRONT)
AUTO = "auto"                 # bind_host／back_host 填 auto：直連網路線時自動找位址（shared.lan_discovery）
MIN_TOKEN_CHARS = 43             # new_token() 產生的長度（256 位元）；自己編的好記字串會被擋下
MIN_DISTINCT_CHARS = 16
_TOKEN_SHAPE = re.compile(r"[A-Za-z0-9_-]+")
DEFAULT_BACK_PORT = 8770
DEFAULT_FRONT_STATUS_PORT = 8771


class ConfigError(ValueError):
    """設定不合法（訊息是給使用者看的白話）。"""


@dataclass
class LinkSettings:
    role: str = STANDALONE
    back_host: str = ""                       # 前台連去的後台位址（內網 IPv4）
    back_port: int = DEFAULT_BACK_PORT
    bind_host: str = ""                       # 後台連線服務綁定的位址（網路線那張網卡的 IPv4 或 auto）；前台不需要
    front_status_port: int = DEFAULT_FRONT_STATUS_PORT   # 保留欄位讓舊設定檔照常讀取；前台已不開狀態服務，不再使用
    back_fingerprint: str = ""                # 前台手動填後台位址時要信任的憑證指紋（SHA-256）；auto 模式從探測回覆取得


def private_ipv4(value, what="位址"):
    """驗證是內網 IPv4 並回傳字串；網域名稱、IPv6、公開位址、萬用位址都丟 ConfigError。"""
    text = str(value or "").strip()
    try:
        address = ipaddress.IPv4Address(text)
    except ValueError:
        raise ConfigError(f"{what}要填 IPv4 位址（例如 192.168.50.1），不接受網域名稱：{text!r}") from None
    if address.is_unspecified or address.is_multicast or address.is_reserved:
        raise ConfigError(f"{what}不能是 {text}，請填網路線那張網卡的位址")
    if not (address.is_private or address.is_loopback or address.is_link_local):
        raise ConfigError(f"{what} {text} 不是內網位址；兩台電腦只能用內網連線，不會連到外網")
    return text


def valid_port(value, what="埠"):
    """驗證埠號（1024–65535）並回傳整數。"""
    try:
        port = int(value)
    except (TypeError, ValueError):
        raise ConfigError(f"{what}要是數字：{value!r}") from None
    if not 1024 <= port <= 65535:
        raise ConfigError(f"{what}要在 1024 到 65535 之間：{port}")
    return port


def _address_or_auto(value, what):
    """內網 IPv4 或 auto。"""
    if str(value or "").strip().lower() != AUTO:
        private_ipv4(value, what)


def validate(settings):
    """檢查整組設定；不合法丟 ConfigError。後台需要綁定位址，前台需要後台位址。"""
    if settings.role not in ROLES:
        raise ConfigError(f"角色只能是 {'、'.join(ROLES)}：{settings.role!r}")
    valid_port(settings.back_port, "後台埠")
    valid_port(settings.front_status_port, "前台狀態埠")
    if settings.role == BACK:
        _address_or_auto(settings.bind_host, "後台綁定位址")
    if settings.role == FRONT:
        _address_or_auto(settings.back_host, "後台位址")
        if settings.back_host.strip().lower() != AUTO and not link_tls.normalize(settings.back_fingerprint):
            raise ConfigError("手動填後台位址時，還要填後台憑證指紋（--back-fingerprint，64 字元十六進位）；"
                              "連線一律加密，不會退回明文。位址填 auto 就會自動取得")
    return settings


def _read(path):
    try:
        with open(path or paths.ROLE_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def load(path=None):
    """讀取設定；檔案不存在、壞掉或內容不合法一律退回單機（不讓壞設定把門禁擋住）。"""
    data = _read(path)
    env_role = (os.environ.get(ROLE_ENV) or "").strip().lower()
    try:
        settings = LinkSettings(
            role=env_role or str(data.get("role") or STANDALONE),
            back_host=str(data.get("back_host") or ""),
            back_port=data.get("back_port", DEFAULT_BACK_PORT),
            bind_host=str(data.get("bind_host") or ""),
            front_status_port=data.get("front_status_port", DEFAULT_FRONT_STATUS_PORT),
            back_fingerprint=str(data.get("back_fingerprint") or ""))
        settings.back_port = valid_port(settings.back_port, "後台埠")
        settings.front_status_port = valid_port(settings.front_status_port, "前台狀態埠")
        return validate(settings)
    except (ConfigError, TypeError):
        return LinkSettings()


def save(settings, path=None):
    """驗證後寫入（先寫暫存檔再取代）；不合法丟 ConfigError，不會寫出壞檔。"""
    validate(settings)
    target = path or paths.ROLE_PATH
    tmp = target + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(asdict(settings), f, ensure_ascii=False, indent=2)
    os.replace(tmp, target)


def role(path=None):
    return load(path).role


# --- 通行碼：只走環境變數 ---

def new_token():
    """產生一組新的通行碼（足夠長的隨機字串）。"""
    return secrets.token_urlsafe(32)


def token_is_strong(token):
    """通行碼要像 new_token() 的輸出：至少 43 個 URL 安全字元、不能重複字元太多。
    探測回覆的簽章可以被拿去離線猜通行碼，所以不接受好記的弱字串。"""
    return (len(token) >= MIN_TOKEN_CHARS and _TOKEN_SHAPE.fullmatch(token) is not None
            and len(set(token)) >= MIN_DISTINCT_CHARS)


def link_token():
    """環境變數裡的通行碼；沒設或不夠強回傳空字串。"""
    token = (os.environ.get(TOKEN_ENV) or "").strip()
    return token if token_is_strong(token) else ""


def require_token():
    """取得通行碼；沒設或不夠強就丟 ConfigError（雙機模式沒有通行碼不能啟動）。"""
    token = link_token()
    if not token:
        raise ConfigError(f"通行碼沒設定或不夠強：請設定環境變數 {TOKEN_ENV}（至少 {MIN_TOKEN_CHARS} 個英數字元、"
                          "底線或連字號，兩台電腦要一樣；請用 python -m shared.role_config token 產生，不要自己編）")
    return token


def token_matches(candidate):
    """比對對方送來的通行碼（常數時間）；自己沒設通行碼時一律不通過。"""
    expected = link_token()
    if not expected or not isinstance(candidate, str):
        return False
    return hmac.compare_digest(expected.encode("utf-8"), candidate.encode("utf-8"))


# --- 命令列 ---

def _describe(settings):
    token = "已設定" if link_token() else f"未設定（環境變數 {TOKEN_ENV}）"
    lines = [f"角色：{settings.role}"]
    if settings.role == BACK:
        lines.append(f"後台綁定位址：{settings.bind_host}:{settings.back_port}")
    elif settings.role == FRONT:
        lines.append(f"後台位址：{settings.back_host}:{settings.back_port}")
    lines.append(f"通行碼：{token}")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description="單機、後台、前台角色與連線設定")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("show", help="顯示目前設定（不印通行碼）")
    sub.add_parser("token", help="產生一組新的通行碼（只印這一次，請自己存進環境變數）")
    put = sub.add_parser("set", help="設定角色與位址")
    put.add_argument("--role", choices=ROLES, required=True)
    put.add_argument("--back-host", default="", help="前台用：後台的內網 IPv4")
    put.add_argument("--bind-host", default="", help="後台要填：網路線那張網卡的 IPv4 或 auto；前台不需要")
    put.add_argument("--back-port", type=int, default=DEFAULT_BACK_PORT)
    put.add_argument("--front-status-port", type=int, default=DEFAULT_FRONT_STATUS_PORT)
    put.add_argument("--back-fingerprint", default="", help="前台手動填後台位址時：後台憑證的 SHA-256 指紋")
    args = parser.parse_args(argv)
    if args.cmd == "token":
        print(new_token())
        return 0
    if args.cmd == "show":
        print(_describe(load()))
        return 0
    try:
        save(LinkSettings(args.role, args.back_host, args.back_port, args.bind_host, args.front_status_port,
                          link_tls.normalize(args.back_fingerprint)))
    except ConfigError as e:
        print(f"沒有儲存：{e}")
        return 2
    print("已儲存。\n" + _describe(load()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

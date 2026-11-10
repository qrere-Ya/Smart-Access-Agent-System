"""
lan_discovery.py：兩台電腦用網路線直連時的自動互相找到（不用手動設 IP）。

主要功能：
  - 直連網路線、網卡維持「自動取得」時，Windows 會各自分配 169.254.x.x（link-local）位址；
    link_local_addresses 找出本機這類位址（有線網卡優先），auto_address 取第一個
  - 後台：DiscoveryResponder 在自己的位址上聽 UDP 探測，回報「我的位址與埠」；
    回覆附上用通行碼算的 HMAC（綁定這次探測的亂數），前台據此確認對方真的知道通行碼，通行碼本身不會在網路上出現
  - 前台：discover_back 從本機每個 link-local 位址廣播探測，驗證回覆（HMAC、來源位址與回報位址一致、來源必須是內網）
    後才採用；之後仍會用通行碼呼叫後台的 API，這裡只負責「找到位址」
  - 只處理 169.254.0.0/16：Wi-Fi 與其他網路不會被動到，也不需要設閘道；回覆只含位址、埠與驗證碼，沒有任何資料

限制：位址在重新插拔網路線或重開機後可能改變，後台連線服務綁的位址要重啟才會更新，前台會自動重新探測；
探測與回覆是明文 UDP，同一條網路線上的人可以看到後台的位址，但沒有通行碼就連不上、也無法偽造有效回覆；
Windows 防火牆要允許後台的 UDP 探測埠（deploy\\設定後台網路.bat）；實際分配位址要 30 到 60 秒。

串接模組：
  - role_config（bind_host／back_host 可填 auto）；admin.link_server（後台啟動時取位址並開回覆）；
    access.front_agent、front_check（前台找後台）
"""
import hmac
import ipaddress
import json
import secrets
import select
import socket
import threading
import time

from shared import role_config

DISCOVERY_PORT = 8772
LINK_LOCAL_NET = ipaddress.ip_network("169.254.0.0/16")
LINK_LOCAL_BROADCAST = "169.254.255.255"
PROBE_PREFIX = b"SAAS1 DISCOVER "
MAX_DATAGRAM = 512
# 不是「網路線」的網卡：無線、藍牙、VPN 與虛擬網卡（Wi-Fi Direct 的「區域連線* N」名稱帶星號）
_UNWANTED_ADAPTERS = ("wi-fi", "wifi", "wireless", "bluetooth", "藍牙", "vethernet", "loopback", "virtual", "vmware",
                      "tailscale", "vpn", "radmin", "zerotier", "hyper-v", "docker", "wsl", "*")


class DiscoveryError(role_config.ConfigError):
    """找不到可用的網路線位址（訊息是給使用者看的白話）。"""


def _is_lan_sender(ip):
    """回覆與探測的來源只接受內網位址（含 link-local 與本機迴路）。"""
    try:
        address = ipaddress.IPv4Address(ip)
    except ValueError:
        return False
    return address.is_private or address.is_link_local or address.is_loopback


def link_local_addresses(interfaces=None):
    """本機「已連線」的 169.254.x.x 位址清單，有線網卡排前面（Wi-Fi、藍牙、VPN、虛擬網卡排後面）。"""
    if interfaces is None:
        import psutil
        up = {name for name, stat in psutil.net_if_stats().items() if stat.isup}
        interfaces = {name: [(a.family, a.address) for a in addrs]
                      for name, addrs in psutil.net_if_addrs().items() if name in up}
    wired, other = [], []
    for name, addrs in interfaces.items():
        for family, address in addrs:
            if family != socket.AF_INET:
                continue
            try:
                if ipaddress.IPv4Address(address) not in LINK_LOCAL_NET:
                    continue
            except ValueError:
                continue
            (other if any(word in name.lower() for word in _UNWANTED_ADAPTERS) else wired).append(address)
    return wired + other


def auto_address(interfaces=None):
    """auto 設定要用的本機位址；沒有就丟 DiscoveryError。"""
    found = link_local_addresses(interfaces)
    if not found:
        raise DiscoveryError("找不到網路線那張網卡的位址：請確認網路線已插好、兩台都開機，"
                             "網卡設為「自動取得 IP」（位址分配約需 30 到 60 秒），再重試")
    return found[0]


def resolve(value, interfaces=None):
    """設定值是 auto 就換成本機實際位址，否則驗證後原樣回傳。"""
    if str(value or "").strip().lower() == role_config.AUTO:
        return auto_address(interfaces)
    return role_config.private_ipv4(value, "位址")


# --- 封包格式 ---

def new_nonce():
    return secrets.token_hex(16)


def build_probe(nonce):
    return PROBE_PREFIX + nonce.encode("ascii")


def parse_probe(data):
    """探測封包 → 亂數字串；格式不對回傳 None。"""
    if not data.startswith(PROBE_PREFIX):
        return None
    nonce = data[len(PROBE_PREFIX):].decode("ascii", "ignore")
    return nonce if len(nonce) == 32 and all(c in "0123456789abcdef" for c in nonce) else None


def _mac(token, nonce, ip, port):
    return hmac.new(token.encode("utf-8"), f"{nonce}|{ip}|{port}".encode("ascii"), "sha256").hexdigest()


def build_reply(token, nonce, ip, port):
    return json.dumps({"v": 1, "ip": ip, "port": int(port), "mac": _mac(token, nonce, ip, int(port))}).encode("ascii")


def verify_reply(token, nonce, data, sender_ip):
    """驗證後台的回覆；通過回傳 (ip, port)，否則 None（HMAC 不符、來源與回報位址不一致、不是內網都不收）。"""
    try:
        body = json.loads(data.decode("ascii"))
        ip, port, mac = str(body["ip"]), int(body["port"]), str(body["mac"])
    except (ValueError, KeyError, TypeError, UnicodeDecodeError):
        return None
    if ip != sender_ip or not _is_lan_sender(ip) or not 1024 <= port <= 65535:
        return None
    return (ip, port) if hmac.compare_digest(_mac(token, nonce, ip, port), mac) else None


# --- 後台：回覆探測 ---

class DiscoveryResponder:
    """在後台位址上聽探測並回覆；背景執行緒，stop() 結束。"""

    def __init__(self, ip, service_port, discovery_port=DISCOVERY_PORT):
        self.ip, self.service_port = role_config.private_ipv4(ip, "後台位址"), service_port
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind((self.ip, discovery_port))
        self._sock.settimeout(0.5)
        self.port = self._sock.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=2)
        self._sock.close()

    def _loop(self):
        while not self._stop.is_set():
            try:
                data, (sender, port) = self._sock.recvfrom(MAX_DATAGRAM)
            except TimeoutError:
                continue
            except OSError:
                return
            token, nonce = role_config.link_token(), parse_probe(data)
            if not token or nonce is None or not _is_lan_sender(sender):
                continue
            try:
                self._sock.sendto(build_reply(token, nonce, self.ip, self.service_port), (sender, port))
            except OSError:
                continue


# --- 前台：找後台 ---

def discover_back(token, local_addresses=None, target=LINK_LOCAL_BROADCAST, discovery_port=DISCOVERY_PORT,
                  timeout=3.0):
    """從本機每個 link-local 位址廣播探測，回傳第一個通過驗證的 (後台位址, 埠)；找不到回傳 None。"""
    addresses = link_local_addresses() if local_addresses is None else local_addresses
    nonce, sockets = new_nonce(), []
    try:
        for address in addresses:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.bind((address, 0))
            sockets.append(sock)
            try:
                sock.sendto(build_probe(nonce), (target, discovery_port))
            except OSError:
                continue
        deadline = time.monotonic() + timeout
        while sockets and time.monotonic() < deadline:
            ready, _, _ = select.select(sockets, [], [], max(0.0, deadline - time.monotonic()))
            for sock in ready:
                try:
                    data, (sender, _port) = sock.recvfrom(MAX_DATAGRAM)
                except OSError:
                    continue
                found = verify_reply(token, nonce, data, sender)
                if found:
                    return found
        return None
    finally:
        for sock in sockets:
            sock.close()


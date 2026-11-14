"""
phone_address.py：QR 註冊用的「手機可以連的位址」——把這台電腦所有可用的網路位址都當成註冊通道，並排出哪個最適合手機。

主要功能：
  - collect／list_addresses：列出本機所有已啟用網卡的 IPv4 位址，每筆 {ip, adapter, kind, label}；
    kind 是 hotspot（Wi-Fi Direct 或 Windows 行動熱點的固定位址）、lan（RFC1918 私有位址）、
    vpn（Radmin、Tailscale 等 VPN 或其他位址，手機通常連不到，但仍列出，讓使用者自己選）
  - 排除 127.x（本機）、169.254.x（網路線直連前台那條，不給手機）與 0.0.0.0
  - 排序：① 熱點（Wi-Fi Direct 網卡位址、192.168.137.1、192.168.5.1）
    ② 區網（結尾 .1 的位址＝本機當閘道，通常是熱點，其次預設路由那個，其次其他）③ VPN／其他
  - best／best_ip：最適合手機的位址（熱點或區網優先；只剩 VPN／其他位址時才退而求其次用它；
    完全沒有位址回 None）
  - 私有網段一律用 ipaddress 判斷（172.16.0.0/12 要認得，iPhone 熱點是 172.20.10.x；
    不要用字串前綴）

設計：判斷邏輯都是純函式（吃資料、回傳結果），系統呼叫（psutil、socket、ipconfig）
集中在 read_*／find_* 函式，測試時替換。

串接模組：
  - admin.service_manager（get_phone_addresses、get_local_ip_offline）→ admin.backend_main 的 QR 註冊視窗通道選擇、
    admin.preflight
  - access.web_server（啟動時印出所有通道網址）
"""
import ipaddress
import re
import socket
import subprocess  # noqa: S404  # nosec B404  只執行固定的唯讀 ipconfig

KIND_HOTSPOT, KIND_LAN, KIND_VPN = "hotspot", "lan", "vpn"
HOTSPOT_FIXED = ("192.168.137.1", "192.168.5.1")      # Windows 行動熱點與常見的虛擬熱點閘道位址
PRIVATE_NETS = (ipaddress.ip_network("10.0.0.0/8"), ipaddress.ip_network("172.16.0.0/12"),
                ipaddress.ip_network("192.168.0.0/16"))
LINK_LOCAL_NET = ipaddress.ip_network("169.254.0.0/16")
CGNAT_NET = ipaddress.ip_network("100.64.0.0/10")       # Tailscale 等會用的共享位址空間
_VPN_NAMES = (("radmin", "Radmin VPN"), ("tailscale", "Tailscale"), ("zerotier", "ZeroTier"), ("hamachi", "Hamachi"),
              ("wireguard", "WireGuard"), ("openvpn", "OpenVPN"), ("vpn", "VPN"))
_AUTO = object()


def _parse(ip):
    """字串 → IPv4Address；不是合法 IPv4 回傳 None。"""
    try:
        return ipaddress.IPv4Address(str(ip).strip())
    except ValueError:
        return None


def is_private(ip):
    """是不是 RFC1918 私有位址（10/8、172.16/12、192.168/16）；用 ipaddress 判斷，172.32.x 不算。"""
    address = _parse(ip)
    return address is not None and any(address in net for net in PRIVATE_NETS)


def is_excluded(ip):
    """不能當註冊通道的位址：不合法、本機迴路、網路線直連的 169.254、0.0.0.0、多播。"""
    address = _parse(ip)
    return (address is None or address.is_loopback or address.is_unspecified or address.is_multicast
            or address in LINK_LOCAL_NET)


# --- 系統呼叫（測試時替換）---

def read_interfaces():
    """本機已啟用網卡的 IPv4 位址 {網卡名稱: [位址]}；psutil 不能用就退回主機名稱解析（網卡名稱未知）。"""
    try:
        import psutil
        up = {name for name, stat in psutil.net_if_stats().items() if stat.isup}
        found = {name: [a.address for a in addrs if a.family == socket.AF_INET]
                 for name, addrs in psutil.net_if_addrs().items() if name in up}
        found = {name: ips for name, ips in found.items() if ips}
        if found:
            return found
    except Exception as e:  # noqa: BLE001  沒裝 psutil 或讀取失敗就退回下面的做法
        print(f"[phone_address] 讀不到網卡資訊，改用主機名稱解析：{type(e).__name__}")
    try:
        _host, _alias, ips = socket.gethostbyname_ex(socket.gethostname())
        return {"": list(ips)} if ips else {}
    except OSError:
        return {}


def read_default_route():
    """預設路由用的本機位址（UDP connect 不會真的送封包）；沒有網路回傳 None。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()


def parse_wifi_direct(output):
    """ipconfig /all 的輸出 → 描述含 Wi-Fi Direct 的網卡 IPv4（同一張網卡列出多組時取最後一組）；沒有回傳 None。"""
    blocks, current = [], []
    for line in output.splitlines():
        if line and not line[0].isspace():
            if current:
                blocks.append("\n".join(current))
            current = [line]
        else:
            current.append(line)
    if current:
        blocks.append("\n".join(current))
    for block in blocks:
        if "Wi-Fi Direct" in block or "WiFi Direct" in block:
            matches = re.findall(r"IPv4.*?:\s*([\d.]+)", block)
            if matches:
                return matches[-1]
    return None


def find_wifi_direct_ip():
    """用 `ipconfig /all` 找 Wi-Fi Direct 網卡的 IPv4，找不到或出錯回傳 None。"""
    try:
        raw = subprocess.run(["ipconfig", "/all"], capture_output=True, timeout=5).stdout  # noqa: S607  # nosec B603 B607
        text = None
        for encoding in ("cp950", "utf-8", "big5"):          # 繁體中文 Windows 常用 cp950
            try:
                text = raw.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        return parse_wifi_direct(text if text is not None else raw.decode("utf-8", errors="ignore"))
    except Exception as e:  # noqa: BLE001
        print(f"[WiFi Direct IP Discovery Error] {e}")
        return None


# --- 純判斷 ---

def _vpn_label(adapter, ip):
    """VPN／其他位址的顯示名稱：看網卡名稱，認不出來再看位址段。"""
    lowered = str(adapter).lower()
    for word, name in _VPN_NAMES:
        if word in lowered:
            return name
    if _parse(ip) in CGNAT_NET:
        return "VPN（100.64/10）"
    return str(adapter) or "其他網路"


def _label(kind, adapter, ip):
    """給人看的一行說明。"""
    if kind == KIND_VPN:
        return f"{_vpn_label(adapter, ip)} {ip}（VPN／其他，手機通常連不到）"
    name = f"{adapter} " if adapter else ""
    return f"{name}{ip}（{'熱點' if kind == KIND_HOTSPOT else '區網'}）"


def collect(interfaces=None, default_route=_AUTO, wifi_direct=_AUTO):
    """所有註冊通道 [{ip, adapter, kind, label}]，依「熱點、區網、VPN／其他」排序；同一個位址只列一次。"""
    interfaces = read_interfaces() if interfaces is None else interfaces
    default_route = read_default_route() if default_route is _AUTO else default_route
    wifi_direct = find_wifi_direct_ip() if wifi_direct is _AUTO else wifi_direct
    seen, found = set(), []
    for adapter, ips in interfaces.items():
        for ip in ips:
            if is_excluded(ip) or ip in seen:
                continue
            seen.add(ip)
            found.append((str(adapter), str(ip).strip()))
    if wifi_direct and not is_excluded(wifi_direct) and wifi_direct not in seen:
        found.append(("Wi-Fi Direct", wifi_direct))          # ipconfig 看得到但 psutil 沒列出的熱點網卡也算
    hotspot_order = [wifi_direct] + list(HOTSPOT_FIXED)

    def kind_of(ip):
        if ip in hotspot_order:
            return KIND_HOTSPOT
        return KIND_LAN if is_private(ip) else KIND_VPN

    def rank(item):
        _adapter, ip = item
        kind = kind_of(ip)
        if kind == KIND_HOTSPOT:
            return (0, hotspot_order.index(ip))
        if kind == KIND_LAN:
            return (1, 0 if ip.endswith(".1") else 1 if ip == default_route else 2)
        return (2, 0 if ip == default_route else 1)

    ordered = sorted(found, key=rank)                        # sorted 是穩定排序，同等級保持原本順序
    return [{"ip": ip, "adapter": adapter, "kind": kind_of(ip), "label": _label(kind_of(ip), adapter, ip)}
            for adapter, ip in ordered]


def list_addresses():
    """讀取這台電腦現在所有的註冊通道。"""
    return collect()


def best(addresses):
    """最適合手機的通道（熱點或區網優先，只剩 VPN／其他才用它）；清單是空的回傳 None。"""
    return next((a for a in addresses if a["kind"] != KIND_VPN), addresses[0] if addresses else None)


def best_ip(addresses=None):
    """最適合手機的位址字串；完全沒有可用位址回傳 None。"""
    chosen = best(list_addresses() if addresses is None else addresses)
    return chosen["ip"] if chosen else None

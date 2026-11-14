"""QR 註冊通道：所有網卡位址都列出、排序（熱點、區網、VPN）、排除 127 與 169.254、私有網段邊界、系統呼叫失敗的退路。"""
import socket
import subprocess
import types

import pytest

from shared import phone_address as pa

# 使用者那台電腦的真實組合：Radmin VPN、iPhone 熱點的 Wi-Fi（172.20.10.x）、Tailscale、網路線直連前台、本機
REAL = {"Radmin VPN": ["26.244.154.160"], "Wi-Fi": ["172.20.10.7"], "Tailscale": ["100.96.212.34"],
        "乙太網路": ["169.254.211.163"], "Loopback": ["127.0.0.1"]}


def ips(found):
    return [a["ip"] for a in found]


def test_the_real_machine_lists_wifi_first_and_vpns_last_but_lists_them_all():
    found = pa.collect(REAL, default_route="172.20.10.7", wifi_direct=None)
    assert ips(found) == ["172.20.10.7", "26.244.154.160", "100.96.212.34"]
    assert [a["kind"] for a in found] == ["lan", "vpn", "vpn"]
    assert "169.254.211.163" not in ips(found) and "127.0.0.1" not in ips(found)     # 直連前台那條與本機不給手機
    assert "區網" in found[0]["label"] and "Wi-Fi" in found[0]["label"]
    assert "Radmin VPN" in found[1]["label"] and "手機通常連不到" in found[1]["label"]
    assert "Tailscale" in found[2]["label"]
    assert pa.best_ip(found) == "172.20.10.7"


@pytest.mark.parametrize("ip, private", [
    ("10.0.0.1", True), ("10.255.255.255", True), ("172.16.0.1", True), ("172.20.10.7", True),
    ("172.31.255.255", True), ("172.32.0.1", False), ("172.15.255.255", False), ("192.168.1.1", True),
    ("192.169.0.1", False), ("100.96.212.34", False), ("26.244.154.160", False), ("169.254.1.1", False),
    ("亂寫", False), ("", False)])
def test_private_means_rfc1918_decided_by_ipaddress_not_string_prefixes(ip, private):
    assert pa.is_private(ip) is private


@pytest.mark.parametrize("ip", ["127.0.0.1", "127.5.5.5", "169.254.0.5", "0.0.0.0", "224.0.0.1", "亂寫", ""])  # noqa: S104
def test_loopback_link_local_unspecified_multicast_and_garbage_are_never_channels(ip):
    assert pa.is_excluded(ip) is True
    assert pa.collect({"x": [ip]}, default_route=None, wifi_direct=None) == []


def test_hotspot_fixed_addresses_beat_ordinary_lan_and_wifi_direct_beats_them_all():
    found = pa.collect({"a": ["192.168.1.50"], "b": ["192.168.137.1"], "c": ["10.0.0.5"]},
                       default_route="192.168.1.50", wifi_direct=None)
    assert ips(found)[0] == "192.168.137.1" and found[0]["kind"] == "hotspot" and "熱點" in found[0]["label"]
    found = pa.collect({"a": ["192.168.1.50"], "b": ["192.168.137.1"]}, default_route=None, wifi_direct="10.20.30.1")
    assert ips(found) == ["10.20.30.1", "192.168.137.1", "192.168.1.50"]   # ipconfig 才看得到的熱點網卡也列出
    assert found[0]["kind"] == "hotspot"


def test_gateway_like_dot_one_comes_before_the_default_route_and_then_the_rest():
    found = pa.collect({"a": ["192.168.1.50", "10.0.0.7"], "b": ["192.168.1.1"]}, default_route="10.0.0.7",
                       wifi_direct=None)
    assert ips(found) == ["192.168.1.1", "10.0.0.7", "192.168.1.50"]
    found = pa.collect({"a": ["192.168.1.50", "10.0.0.7"]}, default_route=None, wifi_direct=None)
    assert ips(found) == ["192.168.1.50", "10.0.0.7"]                                  # 同等級保持原本順序


def test_the_same_address_on_two_adapters_is_listed_once():
    found = pa.collect({"a": ["192.168.1.5"], "b": ["192.168.1.5"]}, default_route=None, wifi_direct=None)
    assert ips(found) == ["192.168.1.5"]


def test_only_vpn_addresses_are_still_listed_and_best_falls_back_to_them():
    found = pa.collect({"Radmin VPN": ["26.244.154.160"]}, default_route=None, wifi_direct=None)
    assert ips(found) == ["26.244.154.160"] and found[0]["kind"] == "vpn"
    assert pa.best_ip(found) == "26.244.154.160"                       # 沒有更好的就用它（仍標示連不到）


def test_best_is_none_when_there_is_nothing_at_all():
    assert pa.best([]) is None and pa.best_ip([]) is None
    assert pa.collect({}, default_route=None, wifi_direct=None) == []


def test_vpn_labels_come_from_the_adapter_name_or_the_address_range():
    found = pa.collect({"ZeroTier One": ["10.147.17.5"], "": ["100.96.212.34", "8.8.4.4"]},
                       default_route=None, wifi_direct=None)
    by_ip = {a["ip"]: a for a in found}
    assert by_ip["10.147.17.5"]["kind"] == "lan"                        # ZeroTier 的 10.x 本身是私有位址
    assert "100.64/10" in by_ip["100.96.212.34"]["label"] and by_ip["100.96.212.34"]["kind"] == "vpn"
    assert "其他網路 8.8.4.4" in by_ip["8.8.4.4"]["label"]
    assert ips(found)[0] == "10.147.17.5"                                               # 私有位址排在 VPN／其他前面


def test_default_route_breaks_ties_among_vpn_like_addresses():
    found = pa.collect({"a": ["26.1.1.1"], "b": ["100.64.0.9"]}, default_route="100.64.0.9", wifi_direct=None)
    assert ips(found) == ["100.64.0.9", "26.1.1.1"]


# --- 系統呼叫 ---

def test_read_interfaces_falls_back_to_hostname_resolution_when_psutil_fails(monkeypatch):
    import psutil

    def boom():
        raise OSError("psutil 壞了")

    monkeypatch.setattr(psutil, "net_if_stats", boom)
    monkeypatch.setattr(socket, "gethostname", lambda: "host")
    monkeypatch.setattr(socket, "gethostbyname_ex", lambda host: (host, [], ["172.20.10.7"]))
    assert pa.read_interfaces() == {"": ["172.20.10.7"]}


def test_read_interfaces_returns_nothing_when_every_source_fails(monkeypatch):
    import psutil

    def boom(*args, **kwargs):
        raise OSError("壞了")

    monkeypatch.setattr(psutil, "net_if_stats", boom)
    monkeypatch.setattr(socket, "gethostbyname_ex", boom)
    assert pa.read_interfaces() == {}


def test_read_interfaces_only_reports_adapters_that_are_up(monkeypatch):
    import psutil

    def addr(family, ip):
        return types.SimpleNamespace(family=family, address=ip)

    monkeypatch.setattr(psutil, "net_if_stats", lambda: {"Wi-Fi": types.SimpleNamespace(isup=True),
                                                         "舊網卡": types.SimpleNamespace(isup=False)})
    monkeypatch.setattr(psutil, "net_if_addrs", lambda: {
        "Wi-Fi": [addr(socket.AF_INET, "172.20.10.7"), addr(socket.AF_INET6, "fe80::1")],
        "舊網卡": [addr(socket.AF_INET, "192.168.9.9")]})
    assert pa.read_interfaces() == {"Wi-Fi": ["172.20.10.7"]}


def test_default_route_is_none_without_a_network(monkeypatch):
    class Offline:
        def __init__(self, *args, **kwargs):
            pass

        def connect(self, address):
            raise OSError("沒有網路")

        def close(self):
            pass

    monkeypatch.setattr(socket, "socket", Offline)
    assert pa.read_default_route() is None


def test_default_route_reads_the_source_address_without_sending_anything(monkeypatch):
    calls = []

    class Fake:
        def __init__(self, *args, **kwargs):
            pass

        def connect(self, address):
            calls.append(address)

        def getsockname(self):
            return ("172.20.10.7", 54321)

        def close(self):
            calls.append("closed")

    monkeypatch.setattr(socket, "socket", Fake)
    assert pa.read_default_route() == "172.20.10.7" and calls == [("8.8.8.8", 80), "closed"]


IPCONFIG = b"""Windows IP Configuration

Wireless LAN adapter Wi-Fi:

   IPv4 Address. . . . . . . . . . . : 172.20.10.7

Wireless LAN adapter Local Area Connection* 3:

   Description . . . . . . . . . . . : Microsoft Wi-Fi Direct Virtual Adapter #2
   IPv4 Address. . . . . . . . . . . : 192.168.137.1
   IPv4 Address. . . . . . . . . . . : 10.20.30.1
"""


def test_wifi_direct_parser_takes_the_last_address_of_that_adapter_only():
    assert pa.parse_wifi_direct(IPCONFIG.decode("utf-8")) == "10.20.30.1"
    assert pa.parse_wifi_direct(IPCONFIG.decode("utf-8").replace("Wi-Fi Direct", "Intel Wireless")) is None
    assert pa.parse_wifi_direct("") is None


def test_find_wifi_direct_ip_survives_ipconfig_failures(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: types.SimpleNamespace(stdout=IPCONFIG))
    assert pa.find_wifi_direct_ip() == "10.20.30.1"

    def boom(*args, **kwargs):
        raise FileNotFoundError("ipconfig")

    monkeypatch.setattr(subprocess, "run", boom)
    assert pa.find_wifi_direct_ip() is None

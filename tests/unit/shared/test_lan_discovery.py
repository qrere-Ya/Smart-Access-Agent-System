"""直連網路線時的自動尋找：本機 link-local 位址、探測與回覆驗證、後台回覆與前台尋找（本機迴路）、設定接受 auto。"""
import socket
import threading

import pytest

from access import front_agent as fa
from shared import lan_discovery as ld, role_config as rc


def test_link_local_details_mark_wired_adapters_and_chinese_wireless_names_are_not_wired():
    interfaces = {"Tailscale": [(socket.AF_INET, "169.254.83.107")], "乙太網路": [(socket.AF_INET, "169.254.211.163")],
                  "無線區域網路": [(socket.AF_INET, "169.254.5.5")], "乙太網路 2": [(socket.AF_INET, "192.168.1.5")]}
    details = ld.link_local_details(interfaces)
    assert details[0] == ("乙太網路", "169.254.211.163", True)                          # 有線排最前面
    assert {(n, w) for n, _a, w in details[1:]} == {("Tailscale", False), ("無線區域網路", False)}
    assert ld.link_local_addresses(interfaces)[0] == "169.254.211.163"

TOKEN = "oGo0iq6_xu9Jp7cwTM0ttP-BTbTQ9Zefin8GaJMQpyc"
NONCE = "a1" * 16
FP = "ab" * 32
AF = socket.AF_INET


@pytest.fixture(autouse=True)
def token(monkeypatch):
    monkeypatch.setenv(rc.TOKEN_ENV, TOKEN)


# --- 本機位址 ---

def test_link_local_addresses_prefer_wired_and_ignore_everything_else():
    interfaces = {
        "Wi-Fi": [(AF, "169.254.9.9")],
        "乙太網路": [(AF, "169.254.20.5"), (socket.AF_INET6, "fe80::1")],
        "vEthernet (WSL)": [(AF, "169.254.77.1")],
        "Tailscale": [(AF, "169.254.83.107")],
        "區域連線* 9": [(AF, "169.254.5.5")],                   # Wi-Fi Direct 的虛擬網卡
        "藍牙網路連線": [(AF, "169.254.6.6")],
        "區域連線": [(AF, "192.168.1.5")],                     # 一般內網不是這裡要找的
        "壞的": [(AF, "not-an-ip")],
    }
    found = ld.link_local_addresses(interfaces)
    assert found[0] == "169.254.20.5"                                        # 有線排第一
    assert set(found) == {"169.254.20.5", "169.254.9.9", "169.254.77.1", "169.254.83.107", "169.254.5.5",
                          "169.254.6.6"}
    assert ld.auto_address(interfaces) == "169.254.20.5"


def test_auto_address_explains_what_to_check_when_the_cable_has_no_address_yet():
    with pytest.raises(ld.DiscoveryError, match="網路線"):
        ld.auto_address({"Wi-Fi": [(AF, "192.168.0.8")]})


def test_resolve_accepts_auto_in_any_case_and_still_rejects_public_addresses():
    interfaces = {"乙太網路": [(AF, "169.254.1.2")]}
    assert ld.resolve("AUTO", interfaces) == "169.254.1.2"
    assert ld.resolve("192.168.50.2") == "192.168.50.2"
    with pytest.raises(rc.ConfigError):
        ld.resolve("8.8.8.8")


# --- 封包 ---

def test_probe_roundtrip_and_garbage_is_ignored():
    assert ld.parse_probe(ld.build_probe(NONCE)) == NONCE
    for junk in (b"", b"hello", ld.PROBE_PREFIX + b"short", ld.PROBE_PREFIX + b"Z" * 32, ld.PROBE_PREFIX + b"a" * 64):
        assert ld.parse_probe(junk) is None


def test_a_reply_is_accepted_only_with_the_right_token_nonce_and_source():
    reply = ld.build_reply(TOKEN, NONCE, "169.254.1.2", 8770, FP)
    assert ld.verify_reply(TOKEN, NONCE, reply, "169.254.1.2") == ("169.254.1.2", 8770, FP)
    assert ld.verify_reply("x" * 40, NONCE, reply, "169.254.1.2") is None          # 通行碼不同
    assert ld.verify_reply(TOKEN, "b2" * 16, reply, "169.254.1.2") is None         # 不是這次探測的回覆（重放）
    assert ld.verify_reply(TOKEN, NONCE, reply, "169.254.9.9") is None             # 來源與回報位址不一致
    for bad in (b"", b"{}", b"not json", b'{"ip": "1", "port": "x", "mac": "y"}'):
        assert ld.verify_reply(TOKEN, NONCE, bad, "169.254.1.2") is None


def test_a_reply_pointing_outside_the_lan_or_to_a_bad_port_is_rejected():
    public = ld.build_reply(TOKEN, NONCE, "8.8.8.8", 8770, FP)
    assert ld.verify_reply(TOKEN, NONCE, public, "8.8.8.8") is None
    low_port = ld.build_reply(TOKEN, NONCE, "169.254.1.2", 80, FP)
    assert ld.verify_reply(TOKEN, NONCE, low_port, "169.254.1.2") is None


# --- 後台回覆、前台尋找（本機迴路）---

def test_front_finds_the_back_through_the_responder():
    responder = ld.DiscoveryResponder("127.0.0.1", 8770, FP, discovery_port=0).start()
    try:
        found = ld.discover_back(TOKEN, ["127.0.0.1"], target="127.0.0.1", discovery_port=responder.port, timeout=2.0)
    finally:
        responder.stop()
    assert found == ("127.0.0.1", 8770, FP)


def test_a_front_with_another_token_finds_nothing():
    responder = ld.DiscoveryResponder("127.0.0.1", 8770, FP, discovery_port=0).start()
    try:
        found = ld.discover_back("z" * 40, ["127.0.0.1"], target="127.0.0.1", discovery_port=responder.port,
                                 timeout=0.8)
    finally:
        responder.stop()
    assert found is None


def test_the_responder_stays_silent_for_garbage_and_when_it_has_no_token(monkeypatch):
    responder = ld.DiscoveryResponder("127.0.0.1", 8770, FP, discovery_port=0).start()
    try:
        client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        client.settimeout(0.6)
        client.sendto(b"garbage", ("127.0.0.1", responder.port))
        with pytest.raises(TimeoutError):
            client.recvfrom(512)
        monkeypatch.delenv(rc.TOKEN_ENV)
        client.sendto(ld.build_probe(NONCE), ("127.0.0.1", responder.port))
        with pytest.raises(TimeoutError):
            client.recvfrom(512)
        client.close()
    finally:
        responder.stop()


def test_discovery_with_no_local_address_returns_nothing_instead_of_failing():
    assert ld.discover_back(TOKEN, [], timeout=0.2) is None


# --- 設定與前台代理 ---

def test_settings_accept_auto_for_both_roles_but_not_nonsense():
    rc.validate(rc.LinkSettings(rc.BACK, bind_host="auto"))
    rc.validate(rc.LinkSettings(rc.FRONT, back_host="AUTO", bind_host="auto"))
    with pytest.raises(rc.ConfigError):
        rc.validate(rc.LinkSettings(rc.FRONT, back_host="automatic", bind_host="auto"))


def test_wait_for_keeps_trying_and_reports_the_reason_once(capsys):
    stop, calls = threading.Event(), []

    def attempt():
        calls.append(1)
        if len(calls) < 3:
            raise rc.ConfigError("位址還沒好")
        return "ok"
    assert fa._wait_for(stop, "網路線位址", attempt, interval=0.01) == "ok"
    assert capsys.readouterr().out.count("位址還沒好") == 1
    stop.set()
    assert fa._wait_for(stop, "x", lambda: None, interval=0.01) is None


def test_the_agent_switches_to_a_new_back_address_when_offline():
    client = fa.BackClient("169.254.1.2", 8770, TOKEN, FP)
    agent = type("A", (), {"back_online": False, "client": client})()
    stop = threading.Event()
    seen = []

    def discover(token):
        seen.append(token)
        stop.set()                                           # 只跑一輪
        return ("169.254.7.7", 8770, "cd" * 32)
    fa._rediscover_loop(agent, TOKEN, stop, interval=0.01, discover=discover)
    assert client.target() == ("169.254.7.7", 8770, "cd" * 32) and seen == [TOKEN]


def test_the_agent_does_not_rediscover_while_the_back_is_online():
    client = fa.BackClient("169.254.1.2", 8770, TOKEN, FP)
    agent = type("A", (), {"back_online": True, "client": client})()
    stop = threading.Event()
    threading.Timer(0.1, stop.set).start()
    fa._rediscover_loop(agent, TOKEN, stop, interval=0.01, discover=lambda t: pytest.fail("不該探測"))
    assert client.target() == ("169.254.1.2", 8770, FP)


def test_adapters_that_are_down_are_not_used(monkeypatch):
    import psutil
    from collections import namedtuple
    addr, stat = namedtuple("a", "family address"), namedtuple("s", "isup")
    monkeypatch.setattr(psutil, "net_if_addrs", lambda: {"乙太網路": [addr(AF, "169.254.1.1")],
                                                         "拔掉的網卡": [addr(AF, "169.254.2.2")]})
    monkeypatch.setattr(psutil, "net_if_stats", lambda: {"乙太網路": stat(True), "拔掉的網卡": stat(False)})
    assert ld.link_local_addresses() == ["169.254.1.1"]

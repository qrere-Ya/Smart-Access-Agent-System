"""後台網路自我檢查：輸出解析、防火牆範圍判斷、各項 OK／WARN／FAIL，以及規則名稱和部署腳本一致。"""
import os

import pytest

from admin import network_check as nc
from shared import paths, role_config

LINK = "169.254.0.0/255.255.0.0"          # Windows 讀出來的網段寫法


def rule(name, protocol, port, **over):
    base = {"Name": name, "Enabled": "True", "Action": "Allow", "Direction": "Inbound", "Profile": "Any",
            "Protocol": protocol, "LocalPort": str(port), "RemotePort": "Any", "RemoteAddress": LINK}
    return {**base, **over}


GOOD = [rule(nc.SERVICE_RULE[0], "TCP", 8770), rule(nc.DISCOVERY_RULE[0], "UDP", 8772)]


def levels(results):
    return [level for level, _ in results]


# --- 解析 ---

def test_parse_json_list_handles_one_many_none_and_bom():
    assert nc.parse_json_list("") == [] and nc.parse_json_list(None) == []
    assert nc.parse_json_list('﻿{"Name": "a"}') == [{"Name": "a"}]
    assert nc.parse_json_list('[{"Name": "a"}, {"Name": "b"}, 5]') == [{"Name": "a"}, {"Name": "b"}]


@pytest.mark.parametrize("value,expected", [
    ("169.254.0.0/255.255.0.0", "link_local"), ("169.254.0.0/16", "link_local"), ("Any", "any"), ("", "any"),
    ("*", "any"), ("10.0.0.0/8", "other"), ("169.254.1.1", "other"), ("169.254.0.0/16,10.0.0.1", "other"),
    ("亂寫", "other")])
def test_remote_scope_reads_windows_address_filters(value, expected):
    assert nc.remote_scope(value) == expected


def test_profile_covers_matches_categories():
    assert nc.profile_covers("Any", "Public") and nc.profile_covers("", "Public")
    assert nc.profile_covers("Domain, Private, Public", "Public")
    assert not nc.profile_covers("Private", "Public") and nc.profile_covers("Domain", "DomainAuthenticated")


# --- 各項檢查 ---

def test_role_must_be_back_and_the_token_strong(monkeypatch):
    monkeypatch.setattr(nc.role_config, "load", lambda: role_config.LinkSettings(role="front"))
    assert levels(nc.check_role()) == ["FAIL"]
    monkeypatch.setattr(nc.role_config, "load", lambda: role_config.LinkSettings(role="back", bind_host="auto"))
    monkeypatch.setattr(nc.role_config, "link_token", lambda: "")
    assert levels(nc.check_role()) == ["OK", "FAIL"]
    monkeypatch.setattr(nc.role_config, "link_token", lambda: "x" * 43)
    assert levels(nc.check_role()) == ["OK", "OK"]


def test_adapter_check_prefers_wired_and_fails_only_in_auto_mode_when_missing():
    auto = role_config.LinkSettings(role="back", bind_host="auto")
    fixed = role_config.LinkSettings(role="back", bind_host="169.254.1.1")
    wired = [("乙太網路", "169.254.211.163", True), ("Tailscale", "169.254.83.107", False)]
    result = nc.check_adapter(wired, auto)
    assert levels(result) == ["OK"] and "169.254.211.163" in result[0][1] and "不會被使用" in result[0][1]
    assert levels(nc.check_adapter([], auto)) == ["FAIL"] and levels(nc.check_adapter([], fixed)) == ["WARN"]
    only_wifi = nc.check_adapter([("Wi-Fi", "169.254.9.9", False)], auto)
    assert levels(only_wifi) == ["FAIL"] and "Wi-Fi" in only_wifi[0][1]


def test_listener_check_flags_missing_and_wildcard_binds():
    ok = {("TCP", "169.254.211.163", 8770), ("UDP", "169.254.211.163", 8772)}
    assert levels(nc.check_listeners(ok)) == ["OK", "OK"]
    assert levels(nc.check_listeners(set())) == ["WARN", "WARN"]
    wild = {("TCP", "0.0.0.0", 8770), ("UDP", "169.254.211.163", 8772)}  # noqa: S104
    assert levels(nc.check_listeners(wild)) == ["WARN", "OK"]
    assert levels(nc.check_listeners({("TCP", "169.254.1.1", 9999)}, port=9999, discovery_port=1)) == ["OK", "WARN"]


def test_firewall_check_wants_both_rules_enabled_allow_and_link_local_only():
    assert levels(nc.check_firewall(GOOD)) == ["OK", "OK"]
    assert levels(nc.check_firewall([GOOD[0]])) == ["OK", "FAIL"]                       # 缺尋找規則
    assert levels(nc.check_firewall(None)) == ["WARN"]                                  # 讀不到只警告
    broken = [dict(GOOD[0], Enabled="False"), dict(GOOD[1], RemoteAddress="Any")]
    result = nc.check_firewall(broken)
    assert levels(result) == ["FAIL", "FAIL"] and "沒有啟用" in result[0][1] and "任何來源" in result[1][1]
    assert levels(nc.check_firewall([dict(GOOD[0], LocalPort="9000"), GOOD[1]])) == ["FAIL", "OK"]
    assert levels(nc.check_firewall([dict(GOOD[0], Action="Block"), GOOD[1]])) == ["FAIL", "OK"]
    assert levels(nc.check_firewall([dict(GOOD[0], RemoteAddress="10.0.0.0/255.0.0.0"), GOOD[1]])) == ["FAIL", "OK"]
    assert levels(nc.check_firewall([dict(GOOD[0], LocalPort="9000"), GOOD[1]], port=9000)) == ["OK", "OK"]


def test_category_check_fails_when_rules_do_not_cover_the_adapters_category():
    profiles = [{"Name": "乙太網路", "Category": "Public"}, {"Name": "Wi-Fi", "Category": "Private"}]
    assert levels(nc.check_category(profiles, GOOD, {"乙太網路"})) == ["OK"]
    private_only = [dict(r, Profile="Private") for r in GOOD]
    assert levels(nc.check_category(profiles, private_only, {"乙太網路"})) == ["FAIL"]
    assert levels(nc.check_category(profiles, GOOD, {"沒有這張"})) == ["WARN"]
    assert levels(nc.check_category(None, GOOD, {"乙太網路"})) == ["WARN"]


def test_run_checks_survives_system_call_failures_and_main_sets_the_exit_code(monkeypatch, capsys):
    def boom():
        raise OSError("沒權限")

    monkeypatch.setattr(nc.role_config, "load", lambda: role_config.LinkSettings(role="back", bind_host="auto"))
    monkeypatch.setattr(nc.role_config, "link_token", lambda: "t" * 43)
    monkeypatch.setattr(nc.lan_discovery, "link_local_details", lambda: [("乙太網路", "169.254.1.2", True)])
    for name in ("read_listeners", "read_firewall_rules", "read_network_profiles"):
        monkeypatch.setattr(nc, name, boom)
    results = nc.run_checks()
    assert "FAIL" not in levels(results) and levels(results).count("WARN") >= 3        # 讀不到都只警告，不中斷
    assert nc.main() == 0
    monkeypatch.setattr(nc, "read_firewall_rules", lambda: [])
    assert nc.main() == 1 and "缺防火牆規則" in capsys.readouterr().out                # 真的沒有規則才是 FAIL


# --- 規則名稱要和部署腳本一致 ---

def _read(*parts):
    with open(os.path.join(paths.PROJECT_ROOT, *parts), encoding="utf-8-sig") as f:
        return f.read()


def test_rule_names_ports_and_cleanup_match_the_deployment_scripts():
    back = _read("deploy", "setup_back_network.ps1")
    assert nc.SERVICE_RULE[0] in back and nc.DISCOVERY_RULE[0] in back and "8772" in back
    assert '"SmartAccess 後台*"' in back and "169.254.0.0/16" in back and "IsInRole" in back
    assert "back_port" in back                                                         # 連線埠取自設定，不寫死
    front = _read("deploy", "front_template", "install_front.ps1")
    assert '"SmartAccess 前台*"' in front and "169.254.0.0/16" in front
    assert "-LocalPort 8771" not in front                                              # 前台不再建立狀態服務規則
    for name in (nc.SERVICE_RULE[0], nc.DISCOVERY_RULE[0]):
        assert name.startswith(nc.RULE_PREFIX + " 後台")


def test_the_back_setup_script_has_no_broken_comment_lines():
    """回歸：註解裡的路徑曾因跳脫字元被拆成兩行，第二行沒有 # 而被 PowerShell 當成指令執行。"""
    import os
    from shared import paths
    path = os.path.join(paths.PROJECT_ROOT, "deploy", "setup_back_network.ps1")
    with open(path, encoding="utf-8-sig") as f:
        text = f.read()
    assert "admin" + chr(92) + "network_check.py" in text
    stray = [line for line in text.splitlines() if line.startswith("etwork") or chr(13) in line]
    assert stray == []


# --- QR 註冊規則（手機連 TCP 5000，只放行同網段） ---

QR_GOOD = rule(nc.QR_RULE[0], "TCP", 5000, RemoteAddress="LocalSubnet")


def test_qr_rule_scope_reads_localsubnet_private_ranges_and_any():
    assert nc.qr_scope("LocalSubnet") == "local_subnet" and nc.qr_scope("localsubnet") == "local_subnet"
    assert nc.qr_scope("192.168.0.0/255.255.0.0") == "private" and nc.qr_scope("172.16.0.0/12,10.0.0.0/8") == "private"
    assert nc.qr_scope("172.20.10.0/28") == "private"
    for bad in ("Any", "", "*"):
        assert nc.qr_scope(bad) == "any"
    for other in ("8.8.8.0/24", "172.32.0.0/16", "169.254.0.0/16", "192.168.0.0/16,8.8.8.8", "亂寫"):
        assert nc.qr_scope(other) == "other", other


def test_qr_rule_ok_when_enabled_inbound_tcp_5000_and_local_subnet_only():
    assert nc.check_qr_rule([QR_GOOD])[0][0] == "OK"
    assert nc.check_qr_rule([rule(nc.QR_RULE[0], "TCP", 5000, RemoteAddress="192.168.0.0/255.255.0.0")])[0][0] == "OK"


def test_qr_rule_missing_only_warns_and_says_how_to_fix():
    (level, text), = nc.check_qr_rule(GOOD)
    assert level == "WARN" and "設定後台網路.bat" in text and "5000" in text
    assert nc.check_qr_rule(None) == []                                   # 讀不到規則時 check_firewall 已經警告過


@pytest.mark.parametrize("over, word", [
    ({"Enabled": "False"}, "沒有啟用"), ({"Action": "Block"}, "入站允許"), ({"Direction": "Outbound"}, "入站允許"),
    ({"LocalPort": "5001"}, "協定或埠"), ({"Protocol": "UDP"}, "協定或埠"),
    ({"RemoteAddress": "Any"}, "任何來源"), ({"RemoteAddress": "8.8.8.0/24"}, "不是區網")])
def test_qr_rule_problems_are_warnings_never_failures(over, word):
    (level, text), = nc.check_qr_rule([{**QR_GOOD, **over}])
    assert level == "WARN" and word in text


def test_qr_rule_name_port_and_scope_match_the_back_setup_script():
    back = _read("deploy", "setup_back_network.ps1")
    assert nc.QR_RULE[0] in back and "-LocalPort 5000" in back and "LocalSubnet" in back
    assert nc.QR_RULE[0].startswith(nc.RULE_PREFIX + " 後台")                # 重跑時被「SmartAccess 後台*」清掉再重建

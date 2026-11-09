"""角色與連線設定：預設單機、位址只收內網 IPv4（不收網域、公開位址、萬用位址）、埠號範圍、壞檔退回單機、
通行碼只走環境變數且常數時間比對、命令列不印通行碼。"""
import json

import pytest

from shared import paths, role_config as rc

TOKEN = "x" * 40


@pytest.fixture
def conf(tmp_path, monkeypatch):
    path = str(tmp_path / "role.json")
    monkeypatch.setattr(paths, "ROLE_PATH", path)
    monkeypatch.delenv(rc.ROLE_ENV, raising=False)
    monkeypatch.delenv(rc.TOKEN_ENV, raising=False)
    return path


# --- 預設與讀寫 ---

def test_default_is_standalone_and_changes_nothing(conf):
    settings = rc.load()
    assert settings.role == rc.STANDALONE and rc.role() == rc.STANDALONE


def test_back_and_front_settings_round_trip(conf):
    rc.save(rc.LinkSettings(rc.BACK, bind_host="192.168.50.1"))
    assert (rc.load().role, rc.load().bind_host) == (rc.BACK, "192.168.50.1")
    rc.save(rc.LinkSettings(rc.FRONT, back_host="192.168.50.1", back_port=9000))
    loaded = rc.load()
    assert (loaded.role, loaded.back_host, loaded.back_port) == (rc.FRONT, "192.168.50.1", 9000)


@pytest.mark.parametrize("content", ["", "nope", "[]", '{"role": "boss"}', '{"role": "front"}',
                                     '{"role": "back", "bind_host": "0.0.0.0"}',
                                     '{"role": "front", "back_host": "8.8.8.8"}',
                                     '{"role": "back", "bind_host": "192.168.50.1", "back_port": 80}',
                                     '{"role": "back", "bind_host": "192.168.50.1", "back_port": "abc"}'])
def test_a_broken_or_unsafe_file_falls_back_to_standalone(conf, content):
    with open(conf, "w", encoding="utf-8") as f:
        f.write(content)
    assert rc.load().role == rc.STANDALONE


def test_the_environment_role_wins_but_must_still_be_valid(conf, monkeypatch):
    with open(conf, "w", encoding="utf-8") as f:
        json.dump({"role": "back", "bind_host": "192.168.50.1"}, f)
    monkeypatch.setenv(rc.ROLE_ENV, "standalone")
    assert rc.load().role == rc.STANDALONE
    monkeypatch.setenv(rc.ROLE_ENV, "front")             # 前台但沒有後台位址 → 不合法，退回單機
    assert rc.load().role == rc.STANDALONE


def test_save_refuses_bad_settings_and_writes_nothing(conf):
    with pytest.raises(rc.ConfigError):
        rc.save(rc.LinkSettings(rc.FRONT, back_host="example.com"))
    import os
    assert not os.path.exists(conf)


# --- 位址 ---

@pytest.mark.parametrize("value", ["192.168.50.1", "10.0.0.7", "172.16.5.4", "172.31.255.1", "169.254.10.2",
                                   "127.0.0.1"])
def test_private_addresses_are_accepted(value):
    assert rc.private_ipv4(value) == value


# 100.64.x.x 是 Tailscale 這類位址，不算內網
@pytest.mark.parametrize("value", ["", "0.0.0.0", "8.8.8.8", "1.1.1.1", "172.32.0.1", "100.64.1.2",  # noqa: S104
                                   "example.com", "http://192.168.0.1", "192.168.0.1:80", "::1", "255.255.255.255",
                                   "224.0.0.1", "192.168.0.256", "192.168.0"])
def test_names_public_wildcard_and_odd_addresses_are_refused(value):
    with pytest.raises(rc.ConfigError):
        rc.private_ipv4(value)


@pytest.mark.parametrize("value,ok", [(1024, True), (8770, True), (65535, True), ("8770", True),
                                      (80, False), (0, False), (65536, False), ("x", False), (None, False)])
def test_ports(value, ok):
    if ok:
        assert rc.valid_port(value) == int(value)
    else:
        with pytest.raises(rc.ConfigError):
            rc.valid_port(value)


# --- 通行碼 ---

def test_the_token_comes_only_from_the_environment(conf, monkeypatch):
    assert rc.link_token() == "" and not rc.token_matches(TOKEN)
    with pytest.raises(rc.ConfigError):
        rc.require_token()
    monkeypatch.setenv(rc.TOKEN_ENV, "short")
    assert rc.link_token() == ""                                     # 太短不算
    monkeypatch.setenv(rc.TOKEN_ENV, TOKEN)
    assert rc.require_token() == TOKEN
    assert rc.token_matches(TOKEN) and not rc.token_matches(TOKEN[:-1] + "y")
    assert not rc.token_matches("") and not rc.token_matches(None) and not rc.token_matches(123)


def test_saving_never_writes_the_token_to_disk(conf, monkeypatch):
    monkeypatch.setenv(rc.TOKEN_ENV, TOKEN)
    rc.save(rc.LinkSettings(rc.BACK, bind_host="192.168.50.1"))
    with open(conf, encoding="utf-8") as f:
        assert TOKEN not in f.read()


def test_generated_tokens_are_long_and_different():
    a, b = rc.new_token(), rc.new_token()
    assert a != b and len(a) >= rc.MIN_TOKEN_CHARS


# --- 命令列 ---

def test_cli_set_show_and_bad_input(conf, monkeypatch, capsys):
    monkeypatch.setenv(rc.TOKEN_ENV, TOKEN)
    assert rc.main(["set", "--role", "back", "--bind-host", "192.168.50.1"]) == 0
    assert rc.main(["show"]) == 0
    out = capsys.readouterr().out
    assert "192.168.50.1:8770" in out and "已設定" in out and TOKEN not in out      # 不印通行碼
    assert rc.main(["set", "--role", "front", "--back-host", "8.8.8.8"]) == 2
    assert "不是內網位址" in capsys.readouterr().out
    assert rc.load().role == rc.BACK                                                 # 壞輸入沒有蓋掉舊設定
    assert rc.main(["token"]) == 0 and len(capsys.readouterr().out.strip()) >= rc.MIN_TOKEN_CHARS

"""前台與後台之間的 TLS 加密：憑證產生與更新、指紋、只信任指紋相符的憑證、明文連不上、指紋被竄改的探測回覆不被採用。"""
import datetime
import json
import os
import ssl
import threading

import pytest
import requests
from werkzeug.serving import make_server

from access import front_agent as fa
from admin import link_cert, link_server
from shared import lan_discovery as ld, link_tls, role_config as rc

TOKEN = "oGo0iq6_xu9Jp7cwTM0ttP-BTbTQ9Zefin8GaJMQpyc"
NOW = datetime.datetime(2026, 11, 10, 7, 50, 0)


@pytest.fixture(autouse=True)
def token(monkeypatch):
    monkeypatch.setenv(rc.TOKEN_ENV, TOKEN)


@pytest.fixture
def tls_back(tmp_path):
    """本機的 TLS 後台（只開 ping，不碰資料庫）。"""
    cert, key = link_cert.ensure_certificate(str(tmp_path / "tls"))
    server = make_server("127.0.0.1", 0, link_server.create_app(now=lambda: NOW), threaded=True,
                         ssl_context=link_tls.server_context(cert, key))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    server.fingerprint = link_tls.fingerprint_of_pem(cert)
    yield server
    server.shutdown()
    server.server_close()


# --- 指紋 ---

def test_normalize_accepts_colons_spaces_and_case_and_rejects_everything_else():
    expected = "ab" * 32
    assert link_tls.normalize("AB:" * 31 + "AB") == expected
    assert link_tls.normalize(" " + expected + " ") == expected
    for bad in ("", None, "12", "zz" * 32, "ab" * 31, "ab" * 33):
        assert link_tls.normalize(bad) == ""


# --- 憑證產生與更新 ---

def test_certificate_is_created_once_and_reused(tmp_path):
    first = link_cert.ensure_certificate(str(tmp_path))
    fingerprint = link_tls.fingerprint_of_pem(first[0])
    assert len(fingerprint) == 64
    assert link_cert.ensure_certificate(str(tmp_path)) == first
    assert link_tls.fingerprint_of_pem(first[0]) == fingerprint            # 沒有重新產生


def _break_cert(cert, key):
    open(cert, "w", encoding="ascii").write("not a certificate")


def _break_key(cert, key):
    open(key, "w", encoding="ascii").write("not a key")


def _remove_key(cert, key):
    os.remove(key)


@pytest.mark.parametrize("damage", [_break_cert, _break_key, _remove_key])
def test_certificate_is_replaced_when_broken_or_missing(tmp_path, damage):
    cert, key = link_cert.ensure_certificate(str(tmp_path))
    original = link_tls.fingerprint_of_pem(cert) if damage is not _break_cert else None
    damage(cert, key)
    link_cert.ensure_certificate(str(tmp_path))
    link_tls.server_context(cert, key)                                      # 憑證與私鑰又是成對可用的
    if original:
        assert link_tls.fingerprint_of_pem(cert) != original


def test_certificate_is_renewed_before_it_expires(tmp_path):
    cert, _key = link_cert.ensure_certificate(str(tmp_path))
    original = link_tls.fingerprint_of_pem(cert)
    nearly_expired = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=link_cert.VALID_DAYS - 5)
    link_cert.ensure_certificate(str(tmp_path), now=nearly_expired)        # 離到期不到 30 天
    assert link_tls.fingerprint_of_pem(cert) != original


def test_a_certificate_that_does_not_match_its_key_is_replaced(tmp_path):
    cert, key = link_cert.ensure_certificate(str(tmp_path / "a"))
    other_cert, _other_key = link_cert.ensure_certificate(str(tmp_path / "b"))
    open(cert, "w", encoding="ascii").write(open(other_cert, encoding="ascii").read())    # 憑證換成別人的
    link_cert.ensure_certificate(str(tmp_path / "a"))
    link_tls.server_context(cert, key)


def test_server_context_requires_tls_1_2_or_newer(tmp_path):
    cert, key = link_cert.ensure_certificate(str(tmp_path))
    assert link_tls.server_context(cert, key).minimum_version >= ssl.TLSVersion.TLSv1_2


# --- 前台只信任指紋相符的後台 ---

def test_the_client_connects_when_the_fingerprint_matches(tls_back):
    client = fa.BackClient("127.0.0.1", tls_back.port, TOKEN, tls_back.fingerprint, timeout=(1.0, 3.0))
    assert client.ping()["ok"] is True
    assert client.base.startswith("https://")


def test_a_different_fingerprint_is_refused_as_a_cert_problem_not_as_offline(tls_back):
    client = fa.BackClient("127.0.0.1", tls_back.port, TOKEN, "cd" * 32, timeout=(1.0, 3.0))
    with pytest.raises(fa.BackCertError):
        client.ping()
    assert issubclass(fa.BackCertError, fa.BackAuthError) and not issubclass(fa.BackCertError, fa.BackOffline)


def test_another_certificate_with_the_same_name_is_not_trusted(tls_back, tmp_path):
    """冒充者自己產生憑證也不行：信任的是指紋，不是憑證機構或名稱。"""
    other_cert, other_key = link_cert.ensure_certificate(str(tmp_path / "impostor"))
    impostor = make_server("127.0.0.1", 0, link_server.create_app(now=lambda: NOW), threaded=True,
                           ssl_context=link_tls.server_context(other_cert, other_key))
    threading.Thread(target=impostor.serve_forever, daemon=True).start()
    try:
        client = fa.BackClient("127.0.0.1", impostor.port, TOKEN, tls_back.fingerprint, timeout=(1.0, 3.0))
        with pytest.raises(fa.BackCertError):
            client.ping()
    finally:
        impostor.shutdown()
        impostor.server_close()


def test_plain_http_to_the_tls_port_gets_nothing_usable(tls_back):
    with pytest.raises(requests.RequestException):
        requests.get(f"http://127.0.0.1:{tls_back.port}/v1/ping", headers={"Authorization": f"Bearer {TOKEN}"},
                     timeout=3)


def test_the_token_is_still_checked_inside_the_tunnel(tls_back):
    client = fa.BackClient("127.0.0.1", tls_back.port, "w" * 40, tls_back.fingerprint, timeout=(1.0, 3.0))
    with pytest.raises(fa.BackAuthError) as caught:
        client.ping()
    assert not isinstance(caught.value, fa.BackCertError)                     # 通行碼錯誤和憑證不符要分得出來


def test_a_client_without_a_fingerprint_refuses_to_exist():
    with pytest.raises(rc.ConfigError, match="指紋"):
        fa.BackClient("127.0.0.1", 8770, TOKEN)
    with pytest.raises(rc.ConfigError):
        fa.BackClient("127.0.0.1", 8770, TOKEN, "not-a-fingerprint")


def test_the_pinned_adapter_does_not_fall_back_to_ca_verification():
    adapter = fa.PinnedAdapter("ab" * 32)

    class Conn:
        cert_reqs = "CERT_REQUIRED"
        ca_certs = None
        ca_cert_dir = None
    conn = Conn()
    adapter.cert_verify(conn, "https://127.0.0.1:8770/", True, None)        # 即使呼叫端要求 verify=True
    assert conn.cert_reqs == "CERT_NONE"


# --- 探測回覆裡的指紋 ---

def test_a_discovery_reply_with_a_tampered_fingerprint_is_not_trusted():
    nonce = "a1" * 16
    genuine = ld.build_reply(TOKEN, nonce, "169.254.1.2", 8770, "ab" * 32)
    forged = json.loads(genuine)
    forged["fp"] = "cd" * 32                                                  # 換成冒充者的憑證指紋，簽章對不上
    assert ld.verify_reply(TOKEN, nonce, json.dumps(forged).encode("ascii"), "169.254.1.2") is None
    assert ld.verify_reply(TOKEN, nonce, genuine, "169.254.1.2") == ("169.254.1.2", 8770, "ab" * 32)
    no_fp = json.loads(genuine)
    no_fp["fp"] = ""
    assert ld.verify_reply(TOKEN, nonce, json.dumps(no_fp).encode("ascii"), "169.254.1.2") is None


def test_discovery_signature_does_not_use_the_token_itself_as_the_key():
    """簽章金鑰是衍生的：用通行碼直接當 HMAC 金鑰算出的簽章不會被接受。"""
    import hashlib
    import hmac
    nonce, fp = "a1" * 16, "ab" * 32
    raw = hmac.new(TOKEN.encode(), f"{nonce}|169.254.1.2|8770|{fp}".encode(), hashlib.sha256).hexdigest()
    reply = json.dumps({"v": 2, "ip": "169.254.1.2", "port": 8770, "fp": fp, "mac": raw}).encode("ascii")
    assert ld.verify_reply(TOKEN, nonce, reply, "169.254.1.2") is None


def test_the_client_ignores_system_proxy_settings(monkeypatch, tls_back):
    """經代理時 requests 不會套用指紋比對，所以前台不吃代理設定。"""
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("NO_PROXY", "")
    client = fa.BackClient("127.0.0.1", tls_back.port, TOKEN, tls_back.fingerprint, timeout=(1.0, 3.0))
    assert client.session.trust_env is False
    assert client.ping()["ok"] is True

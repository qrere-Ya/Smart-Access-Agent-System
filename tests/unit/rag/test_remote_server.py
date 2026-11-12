"""
test_remote_server.py：遠端測試入口的網頁服務（Flask 測試用戶端，不需要網路與模型）。
"""
import json
import os
import time

import pytest

from rag import answer, remote_access, remote_server


class FakeAssistant:
    """回答「回答：問題」；記得自己被問過什麼，用來確認每個人的對話互不影響。"""

    def __init__(self, raises=False, delay=0.0):
        self.asked, self.raises, self.delay = [], raises, delay
        self.last = answer.Trace()

    def reset(self):
        self.asked = []

    def ask(self, question):
        self.last = answer.Trace(outcome="answered", tools=["count"])
        if self.delay:
            time.sleep(self.delay)
        if self.raises:
            raise RuntimeError("內部路徑 C:\\secret\\x.py 出錯")
        self.asked.append(question)
        yield "回答：" + question


class Env:
    def __init__(self, tmp_path):
        self.keystore = remote_access.KeyStore(str(tmp_path / "keys.json"))
        self.log = remote_access.QuestionLog(str(tmp_path / "q.jsonl"))
        self.events = remote_access.EventLog(str(tmp_path / "e.jsonl"))
        self.now = 1000.0
        self.assistants = []
        self.raises = False

    def factory(self, label):
        a = FakeAssistant(raises=self.raises)
        self.assistants.append(a)
        return a

    def app(self, gate=None, **kwargs):
        kwargs.setdefault("events", self.events)
        return remote_server.create_app(self.keystore, self.factory, gate or remote_access.Gate(), self.log,
                                        clock=lambda: self.now, **kwargs)

    def login(self, client, label):
        key = self.keystore.add(label)
        r = client.post("/api/login", json={"key": key})
        assert r.status_code == 200
        return key


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def settle(client, tries=200):
    """等背景問答跑完，回傳最後的狀態。"""
    for _ in range(tries):
        data = client.get("/api/state").get_json()
        if not data["busy"] and not data["current"]:
            return data
        time.sleep(0.02)
    raise AssertionError("問答沒有結束")


def ask(env, client, text):
    env.now += 10   # 跨過兩題之間的冷卻時間
    return client.post("/api/ask", json={"question": text})


def test_page_and_static_files_are_served_with_security_headers(env):
    client = env.app().test_client()
    r = client.get("/")
    assert r.status_code == 200 and "script-src 'self'" in r.headers["Content-Security-Policy"]
    assert r.headers["X-Content-Type-Options"] == "nosniff" and r.headers["Cache-Control"] == "no-store"
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/style.css").status_code == 200
    assert client.get("/static/../remote_server.py").status_code == 404
    assert client.get("/static/index.html").status_code == 404


def test_html_has_no_inline_script_or_style():
    html = open(os.path.join(remote_server.WEB_DIR, "index.html"), encoding="utf-8").read()
    assert "<script>" not in html and "style=" not in html and "onclick" not in html


def test_everything_requires_login(env):
    client = env.app().test_client()
    assert client.get("/api/state").status_code == 401
    assert client.post("/api/ask", json={"question": "hi"}).status_code == 401
    assert client.post("/api/new", json={}).status_code == 401


def test_login_sets_httponly_strict_cookie(env):
    client = env.app().test_client()
    key = env.keystore.add("小明")
    r = client.post("/api/login", json={"key": key})
    cookie = r.headers["Set-Cookie"]
    assert r.get_json()["name"] == "小明" and "HttpOnly" in cookie and "SameSite=Strict" in cookie


def test_five_wrong_keys_lock_further_guessing_but_the_right_key_still_works(env):
    client = env.app().test_client()
    key = env.keystore.add("小明")
    for _ in range(5):
        assert client.post("/api/login", json={"key": "wrong"}).status_code == 401
    assert client.post("/api/login", json={"key": "wrong"}).status_code == 429
    assert client.post("/api/login", json={"key": key}).status_code == 200    # 有人狂試錯誤金鑰，鎖不住組員
    env.now += 61
    assert client.post("/api/login", json={"key": "wrong"}).status_code == 401


def test_blocked_failures_are_not_logged_again(env):
    client = env.app().test_client()
    for _ in range(5):
        client.post("/api/login", json={"key": "wrong"})
    before = open(env.events.path, encoding="utf-8").read().count("\n")
    for _ in range(50):
        assert client.post("/api/login", json={"key": "wrong"}).status_code == 429
    assert open(env.events.path, encoding="utf-8").read().count("\n") == before


def test_revoking_a_key_ends_its_live_session_but_not_others(env):
    app = env.app()
    a, b = app.test_client(), app.test_client()
    env.login(a, "甲")
    env.login(b, "乙")
    assert a.get("/api/state").status_code == 200
    env.keystore.revoke("甲")
    assert a.get("/api/state").status_code == 401 and b.get("/api/state").status_code == 200


def test_replacing_a_key_ends_the_old_session(env):
    client = env.app().test_client()
    env.login(client, "甲")
    env.keystore.revoke("甲")
    env.keystore.add("甲")
    assert client.get("/api/state").status_code == 401


def test_key_expiry_ends_a_live_session(env):
    client = env.app().test_client()
    key = env.keystore.add("甲", days=1, now=env.now)
    assert client.post("/api/login", json={"key": key}).status_code == 200
    env.now += 86400 + 1
    assert client.get("/api/state").status_code == 401


def test_one_key_cannot_hold_more_than_three_sessions(env):
    app = env.app()
    key = env.keystore.add("甲")
    clients = [app.test_client() for _ in range(4)]
    for c in clients:
        assert c.post("/api/login", json={"key": key}).status_code == 200
    assert [c.get("/api/state").status_code for c in clients] == [401, 200, 200, 200]


def test_deeply_nested_json_does_not_cause_a_server_error(env):
    client = env.app().test_client()
    body = "[" * 1900 + "]" * 1900
    r = client.post("/api/login", data=body, content_type="application/json")
    assert r.status_code == 401


def test_overlong_question_is_refused(env):
    client = env.app().test_client()
    env.login(client, "小明")
    assert client.post("/api/ask", json={"question": "a" * 1001}).status_code == 400
    assert ask(env, client, "a" * 1000).status_code == 200


def test_login_cookie_is_a_browser_session_cookie(env):
    client = env.app().test_client()
    key = env.keystore.add("小明")
    assert "Max-Age" not in client.post("/api/login", json={"key": key}).headers["Set-Cookie"]


def test_llm_failures_do_not_reveal_internal_settings_to_remote_users():
    from rag import llm_settings

    class Boom:
        def stream_complete(self, prompt):
            raise llm_settings.LLMCallError("auth", "api", "secret-model-detail", 401)
            yield   # pragma: no cover

    remote = answer.Assistant(None, None, Boom(), None)
    remote.show_internal_details = False
    out = "".join(remote._stream("問題"))
    assert "請稍後再試" in out and "secret-model-detail" not in out and remote.last.outcome == "error"
    local = answer.Assistant(None, None, Boom(), None)
    assert "請稍後再試" not in "".join(local._stream("問題"))


@pytest.mark.parametrize("name", ["https://msi.tail1.ts.net/", "msi.tail1.ts.net:8765", "msi.tail1.ts.net/",
                                  "你的电脑.xxxx.ts.net", "a b.ts.net", "-bad.ts.net", ""])
def test_malformed_allowed_host_is_refused_at_startup(env, monkeypatch, name):
    monkeypatch.setattr(remote_access.paths, "REMOTE_KEYS_PATH", env.keystore.path)
    env.keystore.add("甲")
    with pytest.raises(SystemExit) as info:
        remote_server.main(["--behind-proxy", f"--allowed-host={name}"])   # 用等號寫法，開頭是 - 的值才不會被當成參數
    assert "--allowed-host" in str(info.value)


def test_allowed_host_without_behind_proxy_is_refused(env, monkeypatch):
    monkeypatch.setattr(remote_access.paths, "REMOTE_KEYS_PATH", env.keystore.path)
    env.keystore.add("甲")
    with pytest.raises(SystemExit):
        remote_server.main(["--allowed-host", "x.ts.net"])


def test_login_without_json_body_fails(env):
    client = env.app().test_client()
    env.keystore.add("小明")
    assert client.post("/api/login", json=["x"]).status_code == 401
    assert client.post("/api/login", json={"key": 5}).status_code == 401


def test_revoked_key_cannot_log_in_again(env):
    client = env.app().test_client()
    key = env.login(client, "小明")
    env.keystore.revoke("小明")
    assert env.app().test_client().post("/api/login", json={"key": key}).status_code == 401


def test_question_is_answered_and_shown_in_state(env):
    client = env.app().test_client()
    env.login(client, "小明")
    assert ask(env, client, "誰遲到").status_code == 200
    state = settle(client)
    assert state["turns"] == [{"q": "誰遲到", "a": "回答：誰遲到"}] and state["name"] == "小明"


def test_each_member_has_own_assistant_and_history(env):
    app = env.app()
    a, b = app.test_client(), app.test_client()
    env.login(a, "甲")
    env.login(b, "乙")
    ask(env, a, "甲的問題")
    settle(a)
    ask(env, b, "乙的問題")
    settle(b)
    assert [x.asked for x in env.assistants] == [["甲的問題"], ["乙的問題"]]
    assert [t["q"] for t in a.get("/api/state").get_json()["turns"]] == ["甲的問題"]


def test_internal_errors_are_not_sent_to_the_user(env):
    env.raises = True
    client = env.app().test_client()
    env.login(client, "小明")
    ask(env, client, "問題")
    state = settle(client)
    assert state["turns"][0]["a"] == remote_server.ERROR_TEXT
    assert "secret" not in str(state)


def test_second_question_too_soon_is_refused(env):
    client = env.app().test_client()
    env.login(client, "小明")
    ask(env, client, "第一題")
    settle(client)
    assert client.post("/api/ask", json={"question": "第二題"}).status_code == 429
    assert ask(env, client, "第二題").status_code == 200


@pytest.mark.parametrize("body", [{}, {"question": ""}, {"question": "   "}, {"question": 5}, {"question": ["x"]}])
def test_bad_questions_are_refused(env, body):
    client = env.app().test_client()
    env.login(client, "小明")
    assert client.post("/api/ask", json=body).status_code == 400


def test_session_expires_after_idle_time(env):
    client = env.app().test_client()
    env.login(client, "小明")
    env.now += remote_server.SESSION_IDLE_SECONDS - 1
    assert client.get("/api/state").status_code == 200   # 有操作，期限往後延
    env.now += remote_server.SESSION_IDLE_SECONDS
    assert client.get("/api/state").status_code == 401


def test_logout_ends_the_session(env):
    client = env.app().test_client()
    env.login(client, "小明")
    assert client.post("/api/logout", json={}).status_code == 200
    assert client.get("/api/state").status_code == 401


def test_new_chat_clears_history_and_assistant_memory(env):
    client = env.app().test_client()
    env.login(client, "小明")
    ask(env, client, "問題")
    settle(client)
    assert client.post("/api/new", json={}).status_code == 200
    assert client.get("/api/state").get_json()["turns"] == []
    assert env.assistants[0].asked == []


def test_oversized_body_is_refused(env):
    client = env.app().test_client()
    env.login(client, "小明")
    r = client.post("/api/ask", json={"question": "字" * 10000})
    assert r.status_code == 413


def test_busy_server_tells_the_user_to_try_later(env):
    gate = remote_access.Gate(max_concurrent=1, max_waiting=0, wait_seconds=0.1)
    gate.acquire(object())
    client = env.app(gate).test_client()
    env.login(client, "小明")
    ask(env, client, "問題")
    assert settle(client)["turns"][0]["a"] == remote_access.BUSY_TEXT


def test_question_log_has_user_question_and_tools(env):
    client = env.app().test_client()
    env.login(client, "小明")
    ask(env, client, "誰遲到")
    settle(client)
    row = open(env.log.path, encoding="utf-8").read()
    assert "小明" in row and "誰遲到" in row and "count" in row and "回答：" not in row


def test_answers_are_logged_end_to_end_when_answer_logging_is_on(env):
    env.log = remote_access.QuestionLog(env.log.path, with_answers=True)
    client = env.app().test_client()
    env.login(client, "小明")
    ask(env, client, "誰遲到")
    settle(client)
    row = json.loads(open(env.log.path, encoding="utf-8").read().splitlines()[-1])
    assert row["question"] == "誰遲到" and row["answer"] == "回答：誰遲到" and row["tools"] == ["count"]
    assert row["user"] == "小明" and "plan" in row and "hits" in row


def test_session_cap(env, monkeypatch):
    monkeypatch.setattr(remote_server, "MAX_SESSIONS", 1)
    app = env.app()
    env.login(app.test_client(), "甲")
    key = env.keystore.add("乙")
    assert app.test_client().post("/api/login", json={"key": key}).status_code == 503


PROXY = {"base_url": "https://funnel.example.ts.net"}


def proxy_client(env, **kwargs):
    return env.app(behind_proxy=True, allowed_hosts={"funnel.example.ts.net"}, **kwargs).test_client()


def fail_login(client, xff=None, peer=None, **kwargs):
    headers = {"X-Forwarded-For": xff} if xff else {}
    environ = {"REMOTE_ADDR": peer} if peer else {}
    return client.post("/api/login", json={"key": "wrong"}, headers=headers, environ_overrides=environ, **kwargs)


def test_proxy_mode_uses_host_prefixed_secure_cookie_and_hsts(env):
    client = proxy_client(env)
    key = env.keystore.add("小明")
    r = client.post("/api/login", json={"key": key}, **PROXY)
    cookie = r.headers["Set-Cookie"]
    assert r.status_code == 200 and cookie.startswith("__Host-sa_remote=")
    assert "Secure" in cookie and "HttpOnly" in cookie and "SameSite=Strict" in cookie and "Domain" not in cookie
    assert "max-age" in r.headers["Strict-Transport-Security"]
    assert client.get("/api/state", **PROXY).status_code == 200


def test_proxy_mode_never_accepts_the_plain_cookie_name(env):
    app = env.app(behind_proxy=True, allowed_hosts={"funnel.example.ts.net"})
    client, other = app.test_client(), app.test_client()      # 同一個 app：會話是共用的
    key = env.keystore.add("小明")
    token = client.post("/api/login", json={"key": key}, **PROXY).headers["Set-Cookie"].split("=")[1].split(";")[0]
    assert client.get("/api/state", **PROXY).status_code == 200
    other.set_cookie("sa_remote", token, domain="funnel.example.ts.net")
    assert other.get("/api/state", **PROXY).status_code == 401     # 用一般名稱帶同一個代碼不被接受
    other.set_cookie("__Host-sa_remote", token, domain="funnel.example.ts.net")
    assert other.get("/api/state", **PROXY).status_code == 200     # 對照組：用對的名稱就成功，證明代碼本身有效


def test_direct_mode_has_no_hsts_and_plain_cookie(env):
    client = env.app().test_client()
    key = env.keystore.add("小明")
    r = client.post("/api/login", json={"key": key})
    assert r.headers["Set-Cookie"].startswith("sa_remote=") and "Strict-Transport-Security" not in r.headers


def test_proxy_mode_counts_failures_per_forwarded_address(env):
    client = proxy_client(env)
    key = env.keystore.add("小明")
    for _ in range(5):
        fail_login(client, xff="198.51.100.7", **PROXY)
    assert fail_login(client, xff="198.51.100.7", **PROXY).status_code == 429
    ok = client.post("/api/login", json={"key": key}, headers={"X-Forwarded-For": "198.51.100.8"}, **PROXY)
    assert ok.status_code == 200      # 另一個來源不受影響


def test_proxy_mode_uses_the_rightmost_forwarded_entry_so_a_forged_left_part_does_not_help(env):
    client = proxy_client(env)
    for i in range(5):
        fail_login(client, xff=f"10.0.0.{i}, 198.51.100.7", **PROXY)
    assert fail_login(client, xff="10.9.9.9, 198.51.100.7", **PROXY).status_code == 429


def test_forwarded_header_is_ignored_when_not_behind_a_proxy(env):
    client = env.app().test_client()
    for i in range(5):
        fail_login(client, xff=f"198.51.100.{i}")
    assert fail_login(client, xff="198.51.100.99").status_code == 429


def test_forwarded_header_is_ignored_when_the_connection_is_not_from_the_local_proxy(env):
    client = proxy_client(env)
    for i in range(5):
        fail_login(client, xff=f"198.51.100.{i}", peer="203.0.113.9", **PROXY)
    assert fail_login(client, xff="198.51.100.99", peer="203.0.113.9", **PROXY).status_code == 429


def test_garbage_forwarded_header_falls_back_to_the_connection_address(env):
    client = proxy_client(env)
    for _ in range(5):
        fail_login(client, xff="not-an-ip", **PROXY)
    assert fail_login(client, xff="also-garbage", **PROXY).status_code == 429


def test_global_failure_budget_stops_logins_even_when_the_source_keeps_changing(env):
    budget = remote_access.FailureBudget(max_failures=4, window=600, block_seconds=120)
    client = proxy_client(env, budget=budget)
    key = env.keystore.add("小明")
    for i in range(4):
        assert fail_login(client, xff=f"198.51.100.{i}", **PROXY).status_code == 401
    assert fail_login(client, xff="203.0.113.50", **PROXY).status_code == 429      # 換新來源也一樣被擋
    ok = client.post("/api/login", json={"key": key}, headers={"X-Forwarded-For": "203.0.113.50"}, **PROXY)
    assert ok.status_code == 200               # 但正確的金鑰不受影響
    env.now += 121
    assert fail_login(client, xff="203.0.113.51", **PROXY).status_code == 401      # 預算期滿，恢復計次


def test_per_address_table_does_not_grow_without_limit(env, monkeypatch):
    monkeypatch.setattr(remote_server, "MAX_GUARDS", 3)
    client = proxy_client(env)
    for i in range(10):
        assert fail_login(client, xff=f"198.51.100.{i}", **PROXY).status_code in (401, 429)


def test_sessions_end_after_the_absolute_lifetime_even_with_activity(env):
    client = env.app().test_client()
    env.login(client, "小明")
    start, status = env.now, 200
    while status == 200 and env.now - start < remote_server.SESSION_MAX_SECONDS + 3600:
        env.now += remote_server.SESSION_IDLE_SECONDS - 60
        status = client.get("/api/state").status_code
    assert status == 401
    assert env.now - start >= remote_server.SESSION_MAX_SECONDS


def test_expired_key_cannot_log_in(env):
    client = env.app().test_client()
    key = env.keystore.add("小明", days=1, now=env.now)
    assert client.post("/api/login", json={"key": key}).status_code == 200
    env.now += 86400 + 1
    assert env.app().test_client().post("/api/login", json={"key": key}).status_code == 401


def test_login_events_are_recorded_without_keys(env):
    client = env.app().test_client()
    key = env.login(client, "小明")
    fail_login(client)
    text = open(env.events.path, encoding="utf-8").read()
    assert '"login_ok"' in text and '"login_fail"' in text and "小明" in text
    assert key not in text and "wrong" not in text


def test_robots_blocks_indexing_and_extra_headers_are_sent(env):
    client = env.app().test_client()
    r = client.get("/robots.txt")
    assert r.status_code == 200 and "Disallow: /" in r.get_data(as_text=True)
    page = client.get("/")
    assert "noindex" in page.headers["X-Robots-Tag"] and "camera=()" in page.headers["Permissions-Policy"]
    assert page.headers["Cross-Origin-Resource-Policy"] == "same-origin"


@pytest.mark.parametrize("extra", [["--host", "100.64.0.1", "--allowed-host", "x.ts.net"], ["--host", "127.0.0.1"]])
def test_proxy_mode_needs_loopback_host_and_a_public_name(env, monkeypatch, extra):
    monkeypatch.setattr(remote_access.paths, "REMOTE_KEYS_PATH", env.keystore.path)
    env.keystore.add("甲")
    with pytest.raises(SystemExit):
        remote_server.main(["--behind-proxy", *extra])


def test_list_keys_shows_expiry(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(remote_access.paths, "REMOTE_KEYS_PATH", str(tmp_path / "k.json"))
    remote_server.main(["--add-key", "短期", "--days", "2"])
    remote_server.main(["--add-key", "長期", "--days", "0"])
    capsys.readouterr()
    remote_server.main(["--list-keys"])
    out = capsys.readouterr().out
    assert "短期" in out and "到期" in out and "長期　不會到期" in out


def test_post_must_be_json(env):
    client = env.app().test_client()
    key = env.keystore.add("小明")
    assert client.post("/api/login", data={"key": key}).status_code == 415
    assert client.post("/api/login", data="key=x", content_type="text/plain").status_code == 415


def test_cross_origin_posts_are_refused_but_same_origin_is_allowed(env):
    client = env.app().test_client()
    key = env.keystore.add("小明")
    for origin in ("http://evil.example", "null", "http://localhost.evil.example"):
        r = client.post("/api/login", json={"key": key}, headers={"Origin": origin})
        assert r.status_code == 403
    ok = client.post("/api/login", json={"key": key}, headers={"Origin": "http://localhost"})
    assert ok.status_code == 200


def test_host_header_must_be_in_the_allowlist(env):
    client = remote_server.create_app(env.keystore, env.factory, remote_access.Gate(), env.log,
                                      clock=lambda: env.now, allowed_hosts={"100.64.0.1", "localhost"}).test_client()
    assert client.get("/", headers={"Host": "evil.example"}).status_code == 400
    assert client.get("/", headers={"Host": "evil.example:8765"}).status_code == 400
    assert client.get("/", headers={"Host": "100.64.0.1:8765"}).status_code == 200
    assert client.get("/", headers={"Host": "LOCALHOST"}).status_code == 200
    key = env.keystore.add("小明")
    bad = client.post("/api/login", json={"key": key}, headers={"Host": "rebind.example"})
    assert bad.status_code == 400


def test_rejected_hosts_are_printed_once_and_capped(env, capsys, monkeypatch):
    monkeypatch.setattr(remote_server, "MAX_REJECTED_HOSTS_SHOWN", 3)
    client = remote_server.create_app(env.keystore, env.factory, remote_access.Gate(), env.log,
                                      clock=lambda: env.now, allowed_hosts={"localhost"}).test_client()
    for host in ["a.example", "a.example", "b.example", "c.example", "d.example", "e.example\x1b[31m"]:
        assert client.get("/", headers={"Host": host}).status_code == 400
    out = capsys.readouterr().out
    assert out.count("拒絕不在白名單的 Host") == 3 and out.count("a.example") == 1 and "d.example" not in out


def test_factory_turns_off_evidence_paths_for_remote_assistants():
    base = answer.Assistant(store=None, reranker=None, llm=None, resolver=None)
    runtime = type("R", (), {"assistant": base})()
    assert base.show_evidence_paths is True
    assert remote_server.make_assistant_factory(runtime)("小明").show_evidence_paths is False


def test_wildcard_host_is_refused_without_explicit_flag(env, monkeypatch):
    monkeypatch.setattr(remote_access.paths, "REMOTE_KEYS_PATH", env.keystore.path)
    env.keystore.add("甲")
    with pytest.raises(SystemExit):
        remote_server.main(["--host", "0.0.0.0"])  # noqa: S104  測試：萬用位址必須被拒絕


def test_server_will_not_start_without_any_key(tmp_path, monkeypatch):
    monkeypatch.setattr(remote_access.paths, "REMOTE_KEYS_PATH", str(tmp_path / "none.json"))
    with pytest.raises(SystemExit):
        remote_server.main([])


def test_key_management_cli(tmp_path, monkeypatch, capsys):
    path = str(tmp_path / "k.json")
    monkeypatch.setattr(remote_access.paths, "REMOTE_KEYS_PATH", path)
    remote_server.main(["--add-key", "小明"])
    assert "小明" in capsys.readouterr().out and remote_access.KeyStore(path).labels() == ["小明"]
    remote_server.main(["--list-keys"])
    assert "小明" in capsys.readouterr().out
    remote_server.main(["--revoke-key", "小明"])
    assert remote_access.KeyStore(path).labels() == []


# --- 語言模型模式（--llm）---

class FakeRuntime:
    def __init__(self, llm):
        from types import SimpleNamespace

        self.assistant = SimpleNamespace(store=None, reranker=None, llm=llm, resolver=None, router_llm=llm,
                                         roster_provider=None)
        self.llm_note, self.closed = "助理使用測試模型", False

    def close(self):
        self.closed = True


def start_with(monkeypatch, env, llm, *args):
    """用假的零件啟動 main；回傳 (收到的設定, 假 runtime, serve 有沒有被呼叫)。"""
    from rag import bootstrap

    monkeypatch.setattr(remote_access.paths, "REMOTE_KEYS_PATH", env.keystore.path)
    env.keystore.add("甲")
    seen, runtime, served = {}, FakeRuntime(llm), []

    def fake_create(progress=print, settings=None, **kw):
        seen["settings"] = settings
        return runtime

    monkeypatch.setattr(bootstrap, "create_runtime", fake_create)
    monkeypatch.setattr(remote_server, "serve", lambda *a, **k: served.append(a))   # 不要真的開伺服器
    remote_server.main(list(args))
    return seen, runtime, served


def test_llm_api_flag_overrides_only_this_process_and_starts_when_the_cloud_works(env, monkeypatch):
    from rag import llm_settings

    seen, _, served = start_with(monkeypatch, env, llm_settings.FailoverLLM(object(), None), "--llm", "api")
    assert seen["settings"]["mode"] == "api" and served       # 有進到 serve，代表沒有被拒絕啟動
    assert not os.path.exists(llm_settings.SETTINGS_PATH)       # 沒有寫回設定檔


def test_llm_api_flag_refuses_to_start_when_the_cloud_is_down_and_a_local_model_took_over(env, monkeypatch):
    served = []
    monkeypatch.setattr(remote_server, "serve", lambda *a, **k: served.append(a))
    with pytest.raises(SystemExit, match="--llm api"):
        start_with(monkeypatch, env, object(), "--llm", "api")
    assert served == []                                       # 沒有啟動伺服器


def test_without_the_flag_the_settings_file_decides(env, monkeypatch):
    from rag import llm_settings

    seen, _, served = start_with(monkeypatch, env, object())
    assert seen["settings"] == llm_settings.load_settings() and served


# --- waitress 伺服器 ---

def test_waitress_options_for_direct_mode_trust_no_proxy_headers():
    opts = remote_server.serve_options("127.0.0.1", 8765)
    assert opts["host"] == "127.0.0.1" and opts["port"] == 8765
    assert "trusted_proxy" not in opts                                   # 直連：不信任任何代理標頭
    assert opts["ident"] == "server"                                     # Server 標頭不透露套件名稱
    assert opts["max_request_body_size"] >= remote_server.MAX_BODY_BYTES    # 不能比應用程式自己的上限還小
    assert opts["max_request_body_size"] <= 4 * remote_server.MAX_BODY_BYTES    # 過大的請求體由 waitress 先擋
    assert opts["threads"] >= 4 and opts["channel_timeout"] <= 120 and opts["connection_limit"] >= 20


def test_waitress_options_behind_the_proxy_trust_only_the_local_proxys_rightmost_forwarded_for():
    opts = remote_server.serve_options("127.0.0.1", 8765, behind_proxy=True)
    assert opts["trusted_proxy"] == "127.0.0.1" and opts["trusted_proxy_count"] == 1     # 只信本機代理，取最右一筆
    assert opts["trusted_proxy_headers"] == {"x-forwarded-for"}
    assert opts["clear_untrusted_proxy_headers"] is True                                 # 其他連線自帶的標頭一律清掉


def test_the_real_waitress_accepts_these_options():
    """參數名稱要是 waitress 認得的（拼錯會在啟動時才炸）：用真的 Adjustments 驗證，不開埠。"""
    from waitress.adjustments import Adjustments

    for behind in (False, True):
        Adjustments(**remote_server.serve_options("127.0.0.1", 8765, behind))


@pytest.mark.parametrize("flags, proxy", [(("--behind-proxy", "--allowed-host", "x.ts.net"), True), ((), False)])
def test_main_hands_the_proxy_mode_to_the_server(env, monkeypatch, flags, proxy):
    _seen, _runtime, served = start_with(monkeypatch, env, object(), *flags)
    assert served[0][1:] == ("127.0.0.1", remote_server.DEFAULT_PORT, proxy)


def test_the_flask_development_server_is_no_longer_used():
    import inspect

    source = inspect.getsource(remote_server)
    assert "run_simple" not in source and "werkzeug.serving" not in source


def test_a_malformed_connection_address_is_bucketed_instead_of_trusted(env):
    """waitress 會把代理給的位址填進連線位址；不是合法 IP 的字串（偽造的標頭）不能當成計次的鍵。"""
    app = env.app(behind_proxy=True, allowed_hosts=None)
    client = app.test_client()
    for fake in ("not-an-ip", "1.2.3.4:5555", "<script>"):
        client.post("/api/login", json={"key": "錯"}, environ_overrides={"REMOTE_ADDR": fake})
    lines = [json.loads(x) for x in open(env.events.path, encoding="utf-8") if x.strip()]
    assert {e["ip"] for e in lines if e["event"] == "login_fail"} == {"invalid"}

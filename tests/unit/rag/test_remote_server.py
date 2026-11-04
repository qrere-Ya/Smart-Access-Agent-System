"""
test_remote_server.py：遠端測試入口的網頁服務（Flask 測試用戶端，不需要網路與模型）。
"""
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
        self.now = 1000.0
        self.assistants = []
        self.raises = False

    def factory(self, label):
        a = FakeAssistant(raises=self.raises)
        self.assistants.append(a)
        return a

    def app(self, gate=None):
        return remote_server.create_app(self.keystore, self.factory, gate or remote_access.Gate(), self.log,
                                        clock=lambda: self.now)

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


def test_wrong_key_is_rejected_and_five_failures_lock_even_the_right_key(env):
    client = env.app().test_client()
    key = env.keystore.add("小明")
    for _ in range(5):
        assert client.post("/api/login", json={"key": "wrong"}).status_code in (401, 429)
    assert client.post("/api/login", json={"key": key}).status_code == 429
    env.now += 61
    assert client.post("/api/login", json={"key": key}).status_code == 200


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


def test_session_cap(env, monkeypatch):
    monkeypatch.setattr(remote_server, "MAX_SESSIONS", 1)
    app = env.app()
    env.login(app.test_client(), "甲")
    key = env.keystore.add("乙")
    assert app.test_client().post("/api/login", json={"key": key}).status_code == 503


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

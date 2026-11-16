"""
前台知識庫問答（front_chat、BackClient 的問答呼叫、同步來的開關）單元測試：
能不能問（未開放、離線）、送出到完成與來源名稱、
準備中、忙碌與送太快、未開放與舊後台、離線、後台失敗只顯示前台自己的固定文字、逾時、清除會放棄進行中的那一題、
回答只當純文字、**前台完全不保存問題與回答**（資料庫、檔案、日誌都找不到）、BackClient 只送一個欄位並正確分類回應。
"""
import datetime as dt
import os
import time

import pytest

from access import front_agent as fa
from access import front_chat as fc
from access.front_store import FrontStore
from shared import link_protocol as lp

NOW = dt.datetime(2026, 11, 16, 9, 0, 0)
TOKEN = "oGo0iq6_xu9Jp7cwTM0ttP-BTbTQ9Zefin8GaJMQpyc"
JOB = "cd" * 16
QUESTION = "公司特別休假有幾天這個問題絕對不能被保存"
ANSWER = "工作滿一年每年七天這個回答絕對不能被保存"


class FakeChatClient:
    """假的後台問答客戶端：submit 與 result 的行為由測試指定。"""

    def __init__(self, results=None, submit_error=None, result_error=None):
        self.results = list(results or [{"status": "done", "answer": ANSWER, "sources": ["請假辦法"]}])
        self.submit_error, self.result_error = submit_error, result_error
        self.submitted, self.polls = [], 0

    def chat_submit(self, question):
        if self.submit_error:
            raise self.submit_error
        self.submitted.append(question)
        return {"ok": True, "job_id": JOB, "status": "queued"}

    def chat_result(self, job_id):
        self.polls += 1
        if self.result_error:
            raise self.result_error
        assert job_id == JOB
        return self.results.pop(0) if len(self.results) > 1 else self.results[0]


@pytest.fixture
def store(tmp_path):
    s = FrontStore(str(tmp_path / "front.db"))
    s.set_meta(fa.BACK_CHAT_KEY, True)
    s.set_link_state(True)
    return s


def make(store, client, **kw):
    return fc.FrontChat(store, make_client=lambda: client, sleep=lambda s: time.sleep(0.001),
                        poll_seconds=0.001, **kw)


def wait_state(chat, states, seconds=3):
    end = time.time() + seconds
    while time.time() < end:
        view = chat.view()
        if view["state"] in states:
            return view
        time.sleep(0.005)
    raise AssertionError(f"逾時，狀態：{chat.view()}")


# --- 能不能問 ---

def test_it_is_closed_until_the_back_says_it_is_open(store):
    chat = make(store, FakeChatClient())
    store.set_meta(fa.BACK_CHAT_KEY, False)
    assert chat.availability() == fc.CLOSED and chat.view()["can_ask"] is False
    store.set_meta(fa.BACK_CHAT_KEY, "yes")                      # 不是布林一律當作未開放
    assert chat.availability() == fc.CLOSED
    store.set_meta(fa.BACK_CHAT_KEY, True)
    assert chat.availability() == fc.IDLE


def test_an_old_back_that_never_sent_the_switch_means_closed(tmp_path):
    chat = fc.FrontChat(FrontStore(str(tmp_path / "x.db")), make_client=lambda: FakeChatClient())
    assert chat.availability() == fc.CLOSED and chat.view()["message"] == fc.MESSAGES[fc.CLOSED]


def test_it_is_offline_when_the_back_link_is_down_and_nothing_is_sent(store):
    client = FakeChatClient()
    chat = make(store, client)
    store.set_link_state(False)
    assert chat.availability() == fc.OFFLINE and chat.view()["message"] == fc.MESSAGES[fc.OFFLINE]
    assert chat.submit(QUESTION) is False and client.submitted == []


# --- 送出與完成 ---

def test_a_question_is_sent_and_the_answer_comes_back_with_source_names(store):
    client = FakeChatClient([{"status": "queued"}, {"status": "preparing"}, {"status": "running"},
                             {"status": "done", "answer": ANSWER, "sources": ["請假辦法", "員工手冊"]}])
    chat = make(store, client)
    assert chat.submit("  " + QUESTION + "  ") is True
    view = wait_state(chat, (fc.ANSWER,))
    assert view["answer"] == ANSWER and view["sources"] == ["請假辦法", "員工手冊"] and view["can_ask"] is True
    assert client.submitted == [QUESTION]


def test_the_first_question_shows_the_preparing_message(store):
    seen = []
    client = FakeChatClient([{"status": "preparing"}, {"status": "done", "answer": "好", "sources": []}])
    chat = make(store, client)
    original = client.chat_result

    def watching(job_id):
        seen.append(chat.view()["state"])
        return original(job_id)

    client.chat_result = watching
    chat.submit("問題")
    wait_state(chat, (fc.ANSWER,))
    assert fc.PREPARING in seen or client.polls >= 2
    assert fc.MESSAGES[fc.PREPARING].startswith("知識庫準備中")


@pytest.mark.parametrize("question", ["", "   ", None, "字" * (lp.MAX_CHAT_QUESTION + 1)])
def test_empty_or_too_long_questions_are_not_sent(store, question):
    client = FakeChatClient()
    chat = make(store, client)
    assert chat.submit(question) is False and client.submitted == [] and chat.view()["state"] == fc.IDLE


def test_the_longest_allowed_question_is_sent(store):
    chat = make(store, FakeChatClient())
    assert chat.submit("字" * lp.MAX_CHAT_QUESTION) is True
    wait_state(chat, (fc.ANSWER,))


def test_a_second_question_while_one_is_running_is_ignored(store):
    client = FakeChatClient([{"status": "running"}] * 400 + [{"status": "done", "answer": "好", "sources": []}])
    chat = make(store, client)
    assert chat.submit("第一題") is True
    assert chat.submit("第二題") is False
    assert chat.view()["can_ask"] is False and chat.view()["state"] in (fc.ASKING, fc.PREPARING)
    chat.clear()
    assert client.submitted == ["第一題"]


# --- 失敗與降級 ---

@pytest.mark.parametrize("error, state", [
    (fa.ChatBusy("busy"), fc.BUSY), (fa.ChatBusy("slow_down"), fc.BUSY),
    (fa.ChatClosed("HTTP 404"), fc.CLOSED), (fa.BackOffline("x"), fc.OFFLINE),
    (fa.BackAuthError("通行碼"), fc.OFFLINE), (fa.BackCertError("指紋"), fc.OFFLINE),
    (fa.BackRejected("400 invalid"), fc.ERROR), (ValueError("x"), fc.ERROR), (RuntimeError("意外"), fc.ERROR)])
def test_each_failure_becomes_a_fixed_screen_state(store, error, state):
    chat = make(store, FakeChatClient(submit_error=error))
    chat.submit(QUESTION)
    end = time.time() + 3
    while time.time() < end and chat._state in (fc.ASKING, fc.PREPARING):
        time.sleep(0.005)
    assert chat._state == state
    assert chat._message == fc.MESSAGES.get(state, "")


def test_a_failure_while_polling_is_handled_too(store):
    chat = make(store, FakeChatClient(result_error=fa.BackOffline("斷線")))
    chat.submit(QUESTION)
    end = time.time() + 3
    while time.time() < end and chat._state in (fc.ASKING, fc.PREPARING):
        time.sleep(0.005)
    assert chat._state == fc.OFFLINE


@pytest.mark.parametrize("code", sorted(fc.BACK_MESSAGES))
def test_a_failed_job_shows_the_fronts_own_fixed_text_for_that_code(store, code):
    chat = make(store, FakeChatClient([{"status": "failed", "error": code, "message": "後台自己寫的文字不該顯示"}]))
    chat.submit(QUESTION)
    view = wait_state(chat, (fc.ERROR,))
    assert view["message"] == fc.BACK_MESSAGES[code] and "後台自己寫" not in str(view)


def test_an_unknown_failure_code_gets_the_generic_text(store):
    chat = make(store, FakeChatClient([{"status": "failed", "error": "<script>", "message": "x"}]))
    chat.submit(QUESTION)
    assert wait_state(chat, (fc.ERROR,))["message"] == fc.MESSAGES[fc.ERROR]


def test_it_gives_up_after_the_deadline(store):
    now = [0.0]
    client = FakeChatClient([{"status": "running"}])
    chat = fc.FrontChat(store, make_client=lambda: client, clock=lambda: now[0], give_up=5.0,
                        sleep=lambda s: now.__setitem__(0, now[0] + 2.0), poll_seconds=0.0)
    chat.submit(QUESTION)
    view = wait_state(chat, (fc.ERROR,))
    assert view["message"] == fc.BACK_MESSAGES["timeout"]


def test_no_client_because_the_back_address_is_unknown_means_offline(store):
    chat = fc.FrontChat(store, make_client=lambda: None)
    chat.submit(QUESTION)
    end = time.time() + 3
    while time.time() < end and chat._state == fc.ASKING:
        time.sleep(0.005)
    assert chat._state == fc.OFFLINE


# --- 清除 ---

def test_clearing_drops_the_question_and_the_answer(store):
    chat = make(store, FakeChatClient())
    chat.submit(QUESTION)
    wait_state(chat, (fc.ANSWER,))
    chat.clear()
    view = chat.view()
    assert view == {"state": fc.IDLE, "message": "", "answer": "", "sources": [], "can_ask": True}


def test_clearing_while_a_question_is_in_flight_discards_its_result(store):
    gate = {"open": False}

    class Slow(FakeChatClient):
        def chat_result(self, job_id):
            while not gate["open"]:
                time.sleep(0.002)
            return super().chat_result(job_id)

    chat = make(store, Slow())
    chat.submit(QUESTION)
    time.sleep(0.05)
    chat.clear()
    gate["open"] = True
    time.sleep(0.1)
    assert chat.view()["state"] == fc.IDLE and chat.view()["answer"] == ""


# --- 純文字 ---

def test_the_answer_and_the_sources_are_shown_as_plain_text_only(store):
    evil = "你好\x00\x07‮<b>粗</b> **a** http://evil.example 結束"
    client = FakeChatClient([{"status": "done", "answer": evil + "字" * 9000,
                              "sources": ["正常.pdf", 5, None, "x" * 500, "", "\x07壞", "c", "d", "e", "f", "g", "h",
                                          "i"]}])
    chat = make(store, client)
    chat.submit(QUESTION)
    view = wait_state(chat, (fc.ANSWER,))
    assert "\x00" not in view["answer"] and "‮" not in view["answer"] and " " not in view["answer"]
    assert "<b>粗</b> **a** http://evil.example" in view["answer"] and len(view["answer"]) <= fc.MAX_ANSWER_CHARS + 1
    assert len(view["sources"]) <= fc.MAX_SOURCES and all(isinstance(x, str) and x for x in view["sources"])
    assert all(len(x) <= fc.MAX_SOURCE_CHARS + 1 for x in view["sources"])


# --- 前台不保存任何對話 ---

def test_nothing_of_the_conversation_is_ever_saved_on_the_front(store, tmp_path, capsys):
    chat = make(store, FakeChatClient())
    chat.submit(QUESTION)
    wait_state(chat, (fc.ANSWER,))
    chat.submit("另一題")
    chat.clear()
    seen = []
    for folder, _dirs, files in os.walk(tmp_path):
        for name in files:
            with open(os.path.join(folder, name), "rb") as f:
                seen.append(f.read())
    blob = b"".join(seen)
    assert QUESTION.encode() not in blob and ANSWER.encode() not in blob
    assert store.get_meta("chat") is None
    out = capsys.readouterr()
    assert QUESTION not in out.out + out.err and ANSWER not in out.out + out.err


def test_a_failure_logs_only_the_exception_type_never_the_question(store, capsys):
    chat = make(store, FakeChatClient(submit_error=fa.BackRejected(f"400 {QUESTION}")))
    chat.submit(QUESTION)
    wait_state(chat, (fc.ERROR,))
    out = capsys.readouterr().out
    assert "BackRejected" in out and QUESTION not in out


# --- 客戶端的問答呼叫 ---

class FakeResponse:
    def __init__(self, status, body=None, bad_json=False):
        self.status_code, self._body, self._bad = status, body, bad_json

    def json(self):
        if self._bad:
            raise ValueError("not json")
        return self._body


class FakeSession:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]

    def close(self):
        pass


def client_with(*responses):
    session = FakeSession(*responses)
    return fa.BackClient("169.254.10.2", 8770, TOKEN, "ab" * 32, session=session), session


def test_submit_sends_one_field_with_the_bearer_token_and_no_redirects():
    client, session = client_with(FakeResponse(202, {"ok": True, "job_id": JOB, "status": "queued"}))
    assert client.chat_submit(QUESTION)["job_id"] == JOB
    method, url, kwargs = session.calls[0]
    assert method == "POST" and url == "https://169.254.10.2:8770/v1/chat"
    assert kwargs["json"] == {"question": QUESTION} and kwargs["allow_redirects"] is False
    assert kwargs["headers"] == {"Authorization": f"Bearer {TOKEN}"}


@pytest.mark.parametrize("status, error, kind", [
    (403, "chat_disabled", fa.ChatClosed), (404, "not_found", fa.ChatClosed),
    (429, "busy", fa.ChatBusy), (429, "slow_down", fa.ChatBusy), (429, "???", fa.ChatBusy),
    (401, "unauthorized", fa.BackAuthError), (400, "invalid", fa.BackRejected), (413, "too_large", fa.BackRejected),
    (500, "server_error", fa.BackOffline), (200, "", fa.BackOffline)])        # 送出只接受 202
def test_submit_classifies_every_response(status, error, kind):
    client, _ = client_with(FakeResponse(status, {"ok": False, "error": error}))
    with pytest.raises(kind):
        client.chat_submit("問題")


def test_busy_carries_the_fixed_code():
    client, _ = client_with(FakeResponse(429, {"ok": False, "error": "slow_down"}))
    with pytest.raises(fa.ChatBusy) as info:
        client.chat_submit("問題")
    assert info.value.code == "slow_down"


def test_result_fetches_by_job_id_and_refuses_a_bad_id_without_any_request():
    client, session = client_with(FakeResponse(200, {"ok": True, "status": "done", "answer": "好", "sources": []}))
    assert client.chat_result(JOB)["status"] == "done"
    assert session.calls[0][0] == "GET" and session.calls[0][1].endswith("/v1/chat/" + JOB)
    for bad in ("x", "../ping", "", None, JOB + "0", "G" * 32):
        with pytest.raises(fa.BackRejected):
            client.chat_result(bad)
    assert len(session.calls) == 1


def test_a_non_json_reply_is_treated_as_offline():
    client, _ = client_with(FakeResponse(202, bad_json=True))
    with pytest.raises(fa.BackOffline):
        client.chat_submit("問題")


def test_other_calls_keep_their_old_behaviour_for_403_and_404():
    client, _ = client_with(FakeResponse(404, {"ok": False}))
    with pytest.raises(fa.BackOffline):                      # 只有知識庫問答把 404 當成「未開放」
        client.ping()


# --- 同步來的開關與後台位址 ---

class SyncClient:
    host, port, fingerprint = "169.254.10.2", 8770, "ab" * 32

    def __init__(self, morning):
        self.morning_body = morning

    def ping(self):
        return {"ok": True, "server_time": lp.format_ts(NOW)}

    def faces(self, have=None):
        return {"ok": True, "unchanged": True}

    def morning(self):
        if isinstance(self.morning_body, Exception):
            raise self.morning_body
        return self.morning_body

    def heartbeat(self, status):
        return {"ok": True}

    def send_event(self, event):
        return {"ok": True}


def test_the_chat_switch_is_stored_from_the_sync_and_only_when_it_is_a_boolean(store, tmp_path):
    store.set_meta(fa.BACK_CHAT_KEY, False)
    client = SyncClient({"ok": True, "brief": None, "chat_enabled": True})
    agent = fa.FrontAgent(store, client, now=lambda: NOW, heartbeat_path=str(tmp_path / "hb.json"))
    agent.sync_once()
    assert store.get_meta(fa.BACK_CHAT_KEY) is True
    for junk in ("yes", 1, None, [], {}):
        client.morning_body = {"ok": True, "brief": None, "chat_enabled": junk}
        agent.sync_once()
        assert store.get_meta(fa.BACK_CHAT_KEY) is True
    client.morning_body = {"ok": True, "brief": None}                          # 舊後台沒有這個欄位：維持上一次的值
    agent.sync_once()
    assert store.get_meta(fa.BACK_CHAT_KEY) is True
    client.morning_body = fa.BackOffline("斷線")                                # 離線沿用最後一次的值
    agent.sync_once()
    assert store.get_meta(fa.BACK_CHAT_KEY) is True
    client.morning_body = {"ok": True, "brief": None, "chat_enabled": False}
    agent.sync_once()
    assert store.get_meta(fa.BACK_CHAT_KEY) is False


def test_the_agent_records_where_the_back_is_so_the_dashboard_can_ask(store, tmp_path):
    agent = fa.FrontAgent(store, SyncClient({"ok": True, "brief": None}), now=lambda: NOW,
                          heartbeat_path=str(tmp_path / "hb.json"))
    agent.sync_once()
    assert store.get_meta(fa.BACK_TARGET_KEY) == {"host": "169.254.10.2", "port": 8770, "fingerprint": "ab" * 32}


def test_the_client_factory_builds_a_pinned_client_from_the_recorded_target(store, monkeypatch):
    monkeypatch.setenv("SMART_ACCESS_LINK_TOKEN", TOKEN)
    assert fc.make_client_factory(store)() is None                              # 還沒記下後台位址
    store.set_meta(fa.BACK_TARGET_KEY, {"host": "169.254.10.2", "port": 8770, "fingerprint": "ab" * 32})
    client = fc.make_client_factory(store)()
    assert client is not None and client.base == "https://169.254.10.2:8770" and client.fingerprint == "ab" * 32


@pytest.mark.parametrize("target", ["字串", {"host": "8.8.8.8", "port": 8770, "fingerprint": "ab" * 32},
                                    {"host": "169.254.10.2", "port": 8770}, {"host": "169.254.10.2"}, None])
def test_a_bad_or_public_target_never_builds_a_client(store, monkeypatch, target):
    monkeypatch.setenv("SMART_ACCESS_LINK_TOKEN", TOKEN)
    store.set_meta(fa.BACK_TARGET_KEY, target)
    assert fc.make_client_factory(store)() is None


def test_without_the_token_no_client_is_built(store, monkeypatch):
    monkeypatch.delenv("SMART_ACCESS_LINK_TOKEN", raising=False)
    store.set_meta(fa.BACK_TARGET_KEY, {"host": "169.254.10.2", "port": 8770, "fingerprint": "ab" * 32})
    assert fc.make_client_factory(store)() is None


def test_when_the_back_refuses_the_screen_hides_the_input_even_before_the_next_sync(store):
    """前台還以為開放（同步沒到），後台已經關閉：送出後回 closed，畫面顯示未開放並且不能再送。"""
    chat = make(store, FakeChatClient(submit_error=fa.ChatClosed("HTTP 403")))
    chat.submit(QUESTION)
    view = wait_state(chat, (fc.CLOSED,))
    assert view["can_ask"] is False and view["message"] == fc.MESSAGES[fc.CLOSED]
    assert chat.submit("再問一次") is True or chat.view()["state"] == fc.CLOSED     # 下一輪同步前再送仍會被後台擋下

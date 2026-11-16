"""
後台連線服務的前台知識庫問答路由：POST /v1/chat、GET /v1/chat/<編號>——通行碼、後台開關（預設關閉）、欄位驗證、
429 忙碌與送太快、工作編號格式、完成與失敗的回應內容、回應不含內部細節、/v1/morning 的 chat_enabled、
沒有提供服務（舊設定）時一律當作未開放。
"""
import datetime as dt

import pytest

from admin import chat_service as cs
from admin import link_server
from shared import front_control, link_protocol as lp, paths

TOKEN = "oGo0iq6_xu9Jp7cwTM0ttP-BTbTQ9Zefin8GaJMQpyc"
NOW = dt.datetime(2026, 11, 16, 9, 0, 0)
AUTH = {"Authorization": f"Bearer {TOKEN}"}
JOB = "ab" * 16


class FakeService:
    """記錄收到什麼；submit 與 get 的行為由測試指定。"""

    def __init__(self):
        self.questions, self.error, self.results = [], None, {JOB: {"status": "done", "answer": "七天。",
                                                                    "sources": ["請假辦法"]}}

    def submit(self, question):
        if self.error:
            raise self.error
        self.questions.append(question)
        return JOB

    def get(self, job_id):
        return self.results.get(job_id)


@pytest.fixture
def setup(db, monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "FRONT_CONTROL_PATH", str(tmp_path / "front_control.json"))
    monkeypatch.setattr(paths, "BRIEF_PATH", str(tmp_path / "brief.json"))
    service = FakeService()
    app = link_server.create_app(now=lambda: NOW, token_matches=lambda c: c == TOKEN, chat_service=service)
    return app.test_client(), service


def post(client, body, headers=AUTH):
    return client.post("/v1/chat", json=body, headers=headers)


def test_the_routes_need_the_token(setup):
    client, _ = setup
    front_control.set_chat_enabled(True)
    for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": TOKEN}):
        assert post(client, {"question": "x"}, headers).status_code == 401
        assert client.get(f"/v1/chat/{JOB}", headers=headers).status_code == 401


def test_the_feature_is_off_by_default_and_refuses_everything(setup):
    client, service = setup
    response = post(client, {"question": "每年給幾天特別休假"})
    assert response.status_code == 403 and response.get_json()["error"] == "chat_disabled"
    assert client.get(f"/v1/chat/{JOB}", headers=AUTH).status_code == 403
    assert service.questions == []


def test_turning_it_on_lets_a_question_in_and_returns_a_job_id_with_202(setup):
    client, service = setup
    front_control.set_chat_enabled(True)
    response = post(client, {"question": "  每年給幾天特別休假  "})
    body = response.get_json()
    assert response.status_code == 202 and body["ok"] is True and body["status"] == "queued"
    assert lp.valid_job_id(body["job_id"]) and service.questions == ["每年給幾天特別休假"]


def test_turning_it_off_again_refuses_again(setup):
    client, _ = setup
    front_control.set_chat_enabled(True)
    assert post(client, {"question": "x"}).status_code == 202
    front_control.set_chat_enabled(False)
    assert post(client, {"question": "x"}).status_code == 403


@pytest.mark.parametrize("body", [None, [], "字串", {}, {"question": 5}, {"question": ""}, {"question": "   "},
                                  {"question": None}, {"question": ["a"]}, {"q": "x"},
                                  {"question": "字" * (lp.MAX_CHAT_QUESTION + 1)}])
def test_bad_questions_are_rejected_with_400_and_never_reach_the_service(setup, body):
    client, service = setup
    front_control.set_chat_enabled(True)
    response = post(client, body)
    assert response.status_code == 400 and response.get_json()["error"] == "invalid"
    assert service.questions == []


def test_the_longest_allowed_question_is_accepted(setup):
    client, service = setup
    front_control.set_chat_enabled(True)
    assert post(client, {"question": "字" * lp.MAX_CHAT_QUESTION}).status_code == 202


def test_control_characters_are_removed_and_extra_fields_ignored(setup):
    client, service = setup
    front_control.set_chat_enabled(True)
    post(client, {"question": "特休\x00\x07有幾天", "role": "admin", "data": "roster"})
    assert service.questions == ["特休  有幾天"]


def test_a_non_json_body_is_a_400(setup):
    client, _ = setup
    front_control.set_chat_enabled(True)
    response = client.post("/v1/chat", data="question=x", headers=AUTH)
    assert response.status_code == 400


@pytest.mark.parametrize("error, code", [(cs.Busy(), "busy"), (cs.TooFast(), "slow_down")])
def test_busy_and_too_fast_are_429_with_a_fixed_code(setup, error, code):
    client, service = setup
    front_control.set_chat_enabled(True)
    service.error = error
    response = post(client, {"question": "x"})
    assert response.status_code == 429 and response.get_json()["error"] == code


def test_a_finished_job_returns_the_answer_and_the_source_names(setup):
    client, _ = setup
    front_control.set_chat_enabled(True)
    body = client.get(f"/v1/chat/{JOB}", headers=AUTH).get_json()
    assert body["ok"] is True and body["status"] == "done" and body["answer"] == "七天。"
    assert body["sources"] == ["請假辦法"]


def test_a_failed_job_returns_only_a_fixed_message(setup):
    client, service = setup
    front_control.set_chat_enabled(True)
    service.results[JOB] = {"status": "failed", "error": "unavailable", "message": cs.ERROR_TEXT["unavailable"]}
    body = client.get(f"/v1/chat/{JOB}", headers=AUTH).get_json()
    assert body["status"] == "failed" and body["message"] == cs.ERROR_TEXT["unavailable"]
    assert "Traceback" not in str(body) and "secret" not in str(body)


@pytest.mark.parametrize("job_id", ["x", "0" * 31, "G" * 32, "../etc/passwd", "0" * 33, "0" * 32])
def test_unknown_or_malformed_job_ids_are_404_without_asking_the_service(setup, job_id):
    client, service = setup
    front_control.set_chat_enabled(True)
    response = client.get(f"/v1/chat/{job_id}", headers=AUTH)
    assert response.status_code == 404 and response.get_json()["error"] == "not_found"


def test_the_sync_response_says_whether_chat_is_open(setup):
    client, _ = setup
    assert client.get("/v1/morning", headers=AUTH).get_json()["chat_enabled"] is False
    front_control.set_chat_enabled(True)
    body = client.get("/v1/morning", headers=AUTH).get_json()
    assert body["chat_enabled"] is True and isinstance(body["chat_enabled"], bool)
    assert set(body) >= {"local_only", "recognition_enabled", "chat_enabled"}


def test_without_a_chat_service_everything_looks_not_open(db, monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "FRONT_CONTROL_PATH", str(tmp_path / "front_control.json"))
    monkeypatch.setattr(paths, "BRIEF_PATH", str(tmp_path / "brief.json"))
    client = link_server.create_app(now=lambda: NOW, token_matches=lambda c: c == TOKEN).test_client()
    front_control.set_chat_enabled(True)
    assert client.get("/v1/morning", headers=AUTH).get_json()["chat_enabled"] is False
    assert post(client, {"question": "x"}).status_code == 403


def test_the_recognition_switch_and_the_chat_switch_do_not_disturb_each_other(tmp_path):
    path = str(tmp_path / "c.json")
    assert front_control.recognition_enabled(path) is True and front_control.chat_enabled(path) is False
    front_control.set_chat_enabled(True, path)
    front_control.set_recognition_enabled(False, path)
    assert front_control.chat_enabled(path) is True and front_control.recognition_enabled(path) is False
    front_control.set_chat_enabled(False, path)
    assert front_control.recognition_enabled(path) is False


@pytest.mark.parametrize("content", ["", "壞掉", "[]", '{"chat_enabled": "yes"}', '{"chat_enabled": 1}', "null"])
def test_a_damaged_control_file_means_chat_is_closed(tmp_path, content):
    path = tmp_path / "c.json"
    path.write_text(content, encoding="utf-8")
    assert front_control.chat_enabled(str(path)) is False


def test_the_real_service_with_a_fake_knowledge_base_works_through_the_route(setup, db):
    """不經假服務：真的 ChatService（假知識庫）經 HTTP 路由走完 送出→輪詢→完成。"""
    import time

    class Trace:
        outcome, sources, hits, data_text, tools = "", [], [], "", []

    class Assistant:
        last = Trace()
        on_finished = None
        show_internal_details = show_evidence_paths = True

        def ask_documents(self, question):
            self.last.outcome, self.last.sources = "answered", ["請假辦法"]
            yield "七天。"

    class Runtime:
        assistant = Assistant()

        def close(self):
            pass

    service = cs.ChatService(runtime_factory=lambda progress=None: Runtime(), min_interval=0)
    app = link_server.create_app(now=lambda: NOW, token_matches=lambda c: c == TOKEN, chat_service=service)
    client = app.test_client()
    front_control.set_chat_enabled(True)
    try:
        job = post(client, {"question": "特休幾天"}).get_json()["job_id"]
        body = {}
        end = time.time() + 5
        while time.time() < end and body.get("status") != "done":
            body = client.get(f"/v1/chat/{job}", headers=AUTH).get_json()
            time.sleep(0.02)
        assert body["status"] == "done" and body["answer"] == "七天。" and body["sources"] == ["請假辦法"]
    finally:
        service.close()

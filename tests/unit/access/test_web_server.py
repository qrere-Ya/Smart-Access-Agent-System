"""
web_server 單元測試：一次性 Token（只限本機發放、60 秒有效、註冊成功後作廢）、註冊頁面的存取控制、
註冊流程（欄位檢查、單一人臉、重複註冊被拒、內部錯誤）。人臉模型用假物件取代。
"""
import contextlib
import io
import time

import cv2
import numpy as np
import pytest

from access import web_server as ws

LOCAL = {"REMOTE_ADDR": "127.0.0.1"}
REMOTE = {"REMOTE_ADDR": "192.168.1.77"}


@pytest.fixture
def client(db):
    ws.valid_tokens.clear()
    ws.app.config["TESTING"] = True
    yield ws.app.test_client()
    ws.valid_tokens.clear()


def issue_token(client):
    return client.get("/api/internal/generate_token", environ_base=LOCAL).get_json()["token"]


def test_tokens_are_issued_only_to_the_local_machine(client):
    denied = client.get("/api/internal/generate_token", environ_base=REMOTE)
    assert denied.status_code == 403 and ws.valid_tokens == {}
    token = issue_token(client)
    assert len(token) == 8 and token.isalnum()
    assert ws.valid_tokens[token] == pytest.approx(time.time() + 60, abs=5)


def test_issuing_a_token_sweeps_expired_ones(client):
    ws.valid_tokens["old"] = time.time() - 1
    issue_token(client)
    assert "old" not in ws.valid_tokens


@pytest.mark.parametrize("token", [None, "", "unknown"])
def test_missing_or_unknown_tokens_are_invalid(client, token):
    assert ws.verify_token(token) is False


def test_expired_tokens_are_invalid_and_removed(client):
    ws.valid_tokens["old"] = time.time() - 1
    assert ws.verify_token("old") is False and "old" not in ws.valid_tokens


def test_register_page_requires_a_valid_token(client):
    page = client.get("/")
    assert page.status_code == 403 and "授權碼無效" in page.get_data(as_text=True)
    assert client.get("/?t=bad").status_code == 403
    ok = client.get(f"/?t={issue_token(client)}")
    assert ok.status_code == 200 and "員工自助註冊系統" in ok.get_data(as_text=True)


def png_bytes():
    ok, buffer = cv2.imencode(".png", np.zeros((20, 20, 3), dtype=np.uint8))
    assert ok
    return buffer.tobytes()


def post_form(client, token, **overrides):
    data = {"token": token, "name": "王小明", "emp_id": "E001", "gender": "M",
            "image": (io.BytesIO(png_bytes()), "face.png")}
    data.update(overrides)
    data = {k: v for k, v in data.items() if v is not None}
    return client.post("/api/register", data=data, content_type="multipart/form-data")


@pytest.fixture
def fake_face(monkeypatch):
    """假的人臉流程：預設偵測到一張臉，特徵向量固定。"""
    state = {"faces": 1, "error": None}
    face = {"x1": 1, "y1": 1, "x2": 9, "y2": 9, "left_eye": (2, 2), "right_eye": (6, 2), "nose": (4, 4),
            "left_lip": (3, 6), "right_lip": (5, 6)}

    def detect(frame, detector):
        if state["error"]:
            raise state["error"]
        return frame, [face] * state["faces"]

    monkeypatch.setattr(ws.vc, "vision_session", contextlib.nullcontext)
    monkeypatch.setattr(ws.vc, "face_detect_bgr", detect)
    monkeypatch.setattr(ws.vc, "face_align", lambda rgb, landmarks: rgb)
    monkeypatch.setattr(ws.vc, "feature_extract", lambda aligned: np.ones(4, dtype=np.float32))
    return state


def test_registration_succeeds_stores_the_face_and_burns_the_token(client, fake_face, db):
    token = issue_token(client)
    reply = post_form(client, token)
    assert reply.status_code == 200 and reply.get_json()["status"] == "success"
    assert ws.db.recognize_face(np.ones(4, dtype=np.float32))[0] == "王小明"
    assert token not in ws.valid_tokens
    assert post_form(client, token).status_code == 403


def test_registration_needs_a_valid_token(client, fake_face):
    reply = post_form(client, "not-a-token")
    assert reply.status_code == 403 and reply.get_json()["status"] == "fail"


@pytest.mark.parametrize("missing", ["name", "emp_id", "gender", "image"])
def test_registration_requires_every_field(client, fake_face, missing):
    reply = post_form(client, issue_token(client), **{missing: None})
    assert reply.get_json()["status"] == "fail" and "必填" in reply.get_json()["message"]


@pytest.mark.parametrize("faces", [0, 2])
def test_registration_needs_exactly_one_face(client, fake_face, faces):
    fake_face["faces"] = faces
    token = issue_token(client)
    reply = post_form(client, token)
    assert reply.get_json()["status"] == "fail" and "一張清晰可辨的人臉" in reply.get_json()["message"]
    assert token in ws.valid_tokens


def test_duplicate_registration_is_rejected_and_keeps_the_token(client, fake_face, db, add_user):
    add_user("王小明", "E999")
    token = issue_token(client)
    reply = post_form(client, token)
    assert reply.status_code == 409 and "姓名" in reply.get_json()["message"]
    assert token in ws.valid_tokens


def test_internal_errors_are_reported_as_json(client, fake_face):
    fake_face["error"] = RuntimeError("模型壞了")
    reply = post_form(client, issue_token(client))
    assert reply.get_json()["status"] == "error" and "模型壞了" in reply.get_json()["message"]

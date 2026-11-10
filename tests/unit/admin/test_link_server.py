"""後台連線服務：通行碼驗證、人臉快照與版本、晨報、事件收件（打卡用前台時間、去重、尾隨證據照、各種錯誤輸入）。"""
import base64
import datetime as dt
import sqlite3

import numpy as np
import pytest

from admin import link_server
from shared import digest_store, link_protocol as lp, paths

TOKEN = "oGo0iq6_xu9Jp7cwTM0ttP-BTbTQ9Zefin8GaJMQpyc"
NOW = dt.datetime(2026, 11, 10, 8, 0, 0)
AUTH = {"Authorization": f"Bearer {TOKEN}"}
JPEG = b"\xff\xd8\xff\xe0" + b"0" * 64
EMB = np.arange(512, dtype=np.float32) / 512


@pytest.fixture
def client(db, monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "ANOMALY_DIR", str(tmp_path / "evidence"))
    monkeypatch.setattr(paths, "BRIEF_PATH", str(tmp_path / "brief.json"))
    app = link_server.create_app(now=lambda: NOW, token_matches=lambda c: c == TOKEN)
    return app.test_client()


def punch(user_id, when=NOW, **extra):
    return {"event_id": lp.new_event_id(), "kind": "punch", "ts": lp.format_ts(when), "user_id": user_id, **extra}


def rows(db, table):
    con = sqlite3.connect(db)
    out = con.execute(f"SELECT * FROM {table}").fetchall()  # noqa: S608  測試用固定表名
    con.close()
    return out


def user_id_of(db, name):
    con = sqlite3.connect(db)
    value = con.execute("SELECT id FROM Users WHERE name=?", (name,)).fetchone()[0]
    con.close()
    return value


# --- 通行碼 ---

@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": TOKEN},
                                     {"Authorization": "Basic " + TOKEN}, {"Authorization": "Bearer "}])
@pytest.mark.parametrize("method,path", [("get", "/v1/ping"), ("get", "/v1/faces"), ("get", "/v1/morning"),
                                         ("post", "/v1/events"), ("post", "/v1/heartbeat")])
def test_every_route_needs_the_token(client, method, path, headers):
    response = getattr(client, method)(path, headers=headers)
    assert response.status_code == 401 and response.get_json()["error"] == "unauthorized"


def test_ping_returns_the_server_time(client):
    body = client.get("/v1/ping", headers=AUTH).get_json()
    assert body["ok"] and body["server_time"] == "2026-11-10 08:00:00"


def test_unknown_paths_and_methods_are_json_errors(client):
    assert client.get("/v1/nothing", headers=AUTH).status_code == 404
    assert client.post("/v1/ping", headers=AUTH).status_code == 405


# --- 人臉快照 ---

def test_faces_are_sent_with_a_version_and_not_resent_when_unchanged(client, db, add_user):
    add_user("甲", "E1", embedding=EMB)
    add_user("乙", "E2", embedding=EMB[::-1].copy(), role="admin")
    add_user("停用", "E3", embedding=EMB, active=0)
    body = client.get("/v1/faces", headers=AUTH).get_json()
    assert [f["name"] for f in body["faces"]] == ["甲", "乙"]                    # 停用的不給前台
    assert body["faces"][1]["role"] == "admin"
    version, faces = lp.parse_faces(body)
    assert np.allclose(faces[0]["embedding"], EMB) and version == body["version"]
    same = client.get(f"/v1/faces?have={version}", headers=AUTH).get_json()
    assert same["unchanged"] is True and "faces" not in same
    add_user("丙", "E4", embedding=EMB)                                          # 資料變了，版本跟著變
    changed = client.get(f"/v1/faces?have={version}", headers=AUTH).get_json()
    assert changed["unchanged"] is False and len(changed["faces"]) == 3 and changed["version"] != version


def test_faces_carry_no_photos_or_extra_columns(client, add_user):
    add_user("甲", "E1", embedding=EMB)
    face = client.get("/v1/faces", headers=AUTH).get_json()["faces"][0]
    assert set(face) == {"id", "name", "employee_id", "role", "embedding"}


# --- 晨報 ---

def test_morning_brief_is_served_without_attendance(client):
    assert client.get("/v1/morning", headers=AUTH).get_json()["brief"] is None
    digest_store.save_brief({"date": "2026-11-10", "generated_at": "2026-11-10 08:30", "news": [{"title": "t"}],
                             "news_problem": "", "news_tries": 1, "secret_field": "x"})
    brief = client.get("/v1/morning", headers=AUTH).get_json()["brief"]
    assert brief["news"] == [{"title": "t"}] and "secret_field" not in brief and "news_tries" not in brief


# --- 打卡 ---

def test_a_punch_is_written_with_the_event_time_and_the_server_looked_up_name(client, db, add_user):
    add_user("甲", "E1", embedding=EMB)
    early = dt.datetime(2026, 11, 10, 7, 50, 0)                                  # 事件發生在準時窗內
    late_now = lambda: dt.datetime(2026, 11, 10, 9, 30, 0)                       # noqa: E731  補送時已是 9:30
    app = link_server.create_app(now=late_now, token_matches=lambda c: c == TOKEN)
    response = app.test_client().post("/v1/events", headers=AUTH,
                                      json=punch(user_id_of(db, "甲"), early, name="冒名者"))
    body = response.get_json()
    assert response.status_code == 200 and body["status"] == "applied" and "In: 甲 (上班)" in body["message"]
    assert [(r[1], r[2], r[3]) for r in rows(db, "Attendance")] == [("甲", "2026-11-10 07:50:00", "上班")]


def test_a_resent_event_is_not_written_twice(client, db, add_user):
    add_user("甲", "E1", embedding=EMB)
    event = punch(user_id_of(db, "甲"), dt.datetime(2026, 11, 10, 7, 50, 0))
    assert client.post("/v1/events", headers=AUTH, json=event).get_json()["status"] == "applied"
    assert client.post("/v1/events", headers=AUTH, json=event).get_json()["status"] == "duplicate"
    assert len(rows(db, "Attendance")) == 1


def test_a_punch_for_an_unknown_or_disabled_user_is_a_permanent_422(client, db, add_user):
    add_user("停用", "E3", embedding=EMB, active=0)
    for user_id in (9999, user_id_of(db, "停用")):
        response = client.post("/v1/events", headers=AUTH, json=punch(user_id))
        assert response.status_code == 422 and response.get_json()["error"] == "unknown_user"
    assert rows(db, "Attendance") == [] and rows(db, "LinkEvents") == []      # 沒登記，之後修好還能再送


# --- 尾隨與證據照 ---

def test_a_tailgate_event_saves_the_jpeg_under_a_server_chosen_name(client, db, tmp_path):
    event = {"event_id": lp.new_event_id(), "kind": "tailgate", "ts": "2026-11-10 07:55:03", "detail": "畫面有 2 人",
             "evidence": base64.b64encode(JPEG).decode()}
    body = client.post("/v1/events", headers=AUTH, json=event).get_json()
    assert body["status"] == "applied" and body["message"] == "recorded"
    row = rows(db, "AnomalyEvents")[0]
    assert row[1] == "2026-11-10 07:55:03" and row[2] == "tailgate"
    assert row[4].endswith("tailgate_20261110_075503.jpg")
    with open(row[4], "rb") as f:
        assert f.read() == JPEG
    again = dict(event, event_id=lp.new_event_id(), ts="2026-11-10 07:55:10")     # 30 秒內：合併，不再存新照片
    assert client.post("/v1/events", headers=AUTH, json=again).get_json()["message"] == "merged"
    assert len(rows(db, "AnomalyEvents")) == 1


def test_a_walkby_event_without_a_photo_is_fine(client, db):
    event = {"event_id": lp.new_event_id(), "kind": "no_scan_walkby", "ts": "2026-11-10 07:58:00"}
    assert client.post("/v1/events", headers=AUTH, json=event).status_code == 200
    assert rows(db, "AnomalyEvents")[0][4] is None


# --- 錯誤輸入 ---

def bad_events(db):
    good = {"event_id": lp.new_event_id(), "kind": "tailgate", "ts": "2026-11-10 07:55:03"}
    return {
        "not json": None,
        "kind 不在清單": {**good, "kind": "format_disk"},
        "event_id 太短": {**good, "event_id": "abc"},
        "event_id 含大寫": {**good, "event_id": "A" * 32},
        "時間格式錯": {**good, "ts": "昨天"},
        "時間在未來": {**good, "ts": "2026-11-10 09:00:00"},
        "時間太久以前": {**good, "ts": "2026-01-01 08:00:00"},
        "detail 太長": {**good, "detail": "x" * 201},
        "detail 不是字串": {**good, "detail": {"a": 1}},
        "證據不是 base64": {**good, "evidence": "***"},
        "證據不是 JPEG": {**good, "evidence": base64.b64encode(b"MZ" + b"0" * 50).decode()},
        "證據太大": {**good, "evidence": base64.b64encode(
            b"\xff\xd8\xff" + b"0" * (lp.MAX_EVIDENCE_BYTES + 1)).decode()},
        "打卡沒有 user_id": {"event_id": lp.new_event_id(), "kind": "punch", "ts": "2026-11-10 07:55:03"},
        "user_id 是字串": {"event_id": lp.new_event_id(), "kind": "punch", "ts": "2026-11-10 07:55:03", "user_id": "1"},
        "user_id 是布林": {"event_id": lp.new_event_id(), "kind": "punch", "ts": "2026-11-10 07:55:03",
                        "user_id": True},
        "打卡帶證據": {"event_id": lp.new_event_id(), "kind": "punch", "ts": "2026-11-10 07:55:03", "user_id": 1,
                    "evidence": base64.b64encode(JPEG).decode()},
    }


def test_malformed_events_are_rejected_with_400_and_leave_no_trace(client, db):
    for label, payload in bad_events(db).items():
        response = client.post("/v1/events", headers=AUTH, json=payload) if payload is not None else \
            client.post("/v1/events", headers=AUTH, data="not json", content_type="text/plain")
        assert response.status_code == 400, label
        assert response.get_json()["error"] == "invalid", label
    assert rows(db, "Attendance") == [] and rows(db, "AnomalyEvents") == [] and rows(db, "LinkEvents") == []


def test_an_oversized_request_is_refused_before_parsing(client):
    big = b"{" + b"a" * (lp.MAX_BODY_BYTES + 10)
    response = client.post("/v1/events", headers={**AUTH, "Content-Type": "application/json"}, data=big)
    assert response.status_code == 413 and response.get_json()["error"] == "too_large"


def test_a_database_failure_releases_the_claim_so_the_front_can_retry(client, db, add_user, monkeypatch):
    add_user("甲", "E1", embedding=EMB)
    event = punch(user_id_of(db, "甲"), dt.datetime(2026, 11, 10, 7, 50, 0))

    def boom(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    with monkeypatch.context() as patched:
        patched.setattr(link_server.db, "log_attendance", boom)
        assert client.post("/v1/events", headers=AUTH, json=event).status_code == 500
    assert rows(db, "LinkEvents") == []
    assert client.post("/v1/events", headers=AUTH, json=event).get_json()["status"] == "applied"


# --- 啟動條件 ---

def test_the_server_refuses_to_start_outside_the_back_role_or_without_a_token(monkeypatch):
    from shared import role_config as rc

    monkeypatch.delenv(rc.TOKEN_ENV, raising=False)
    with pytest.raises(rc.ConfigError):
        link_server.make_server(rc.LinkSettings(rc.STANDALONE))
    with pytest.raises(rc.ConfigError):                                         # 後台角色但沒有通行碼
        link_server.make_server(rc.LinkSettings(rc.BACK, bind_host="127.0.0.1"))
    monkeypatch.setenv(rc.TOKEN_ENV, TOKEN)
    server = link_server.make_server(rc.LinkSettings(rc.BACK, bind_host="127.0.0.1", back_port=18771))
    try:
        assert server.host == "127.0.0.1" and server.port == 18771
    finally:
        server.server_close()


# --- 前台心跳 ---

GOOD_HEARTBEAT = {"pending_events": 2, "rejected_events": 0, "dropped_events": 0, "faces": 5, "faces_version": "ab12",
                  "clock_offset_seconds": 1.26, "clock_warning": False, "recognition_running": True, "camera_ok": True}


def test_a_heartbeat_is_stored_for_the_backend_window_and_unknown_fields_are_dropped(client, tmp_path, monkeypatch):
    import json
    monkeypatch.setattr(paths, "FRONT_STATUS_PATH", str(tmp_path / "front_status.json"))
    body = {**GOOD_HEARTBEAT, "name": "甲", "embedding": "AAAA", "__proto__": {"x": 1}}
    assert client.post("/v1/heartbeat", json=body, headers=AUTH).get_json()["ok"] is True
    saved = json.load(open(paths.FRONT_STATUS_PATH, encoding="utf-8"))
    assert saved["status"] == {**GOOD_HEARTBEAT, "clock_offset_seconds": 1.3}
    assert "甲" not in json.dumps(saved, ensure_ascii=False) and "embedding" not in saved["status"]


@pytest.mark.parametrize("change", [{"pending_events": -1}, {"pending_events": "3"}, {"pending_events": True},
                                    {"faces": 10 ** 9}, {"camera_ok": "yes"}, {"recognition_running": None},
                                    {"clock_offset_seconds": 10 ** 12}, {"clock_offset_seconds": "x"},
                                    {"faces_version": "not hex!"}, {"faces_version": "a" * 65}, {"faces": None}])
def test_a_bad_heartbeat_is_rejected_and_nothing_is_stored(client, tmp_path, monkeypatch, change):
    import os
    monkeypatch.setattr(paths, "FRONT_STATUS_PATH", str(tmp_path / "front_status.json"))
    response = client.post("/v1/heartbeat", json={**GOOD_HEARTBEAT, **change}, headers=AUTH)
    assert response.status_code == 400 and response.get_json()["error"] == "invalid"
    assert not os.path.exists(paths.FRONT_STATUS_PATH)


@pytest.mark.parametrize("payload", [None, [], "text", 5])
def test_a_heartbeat_that_is_not_an_object_is_rejected(client, tmp_path, monkeypatch, payload):
    monkeypatch.setattr(paths, "FRONT_STATUS_PATH", str(tmp_path / "front_status.json"))
    assert client.post("/v1/heartbeat", json=payload, headers=AUTH).status_code == 400

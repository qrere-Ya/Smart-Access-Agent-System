"""前台代理：用假後台驗證補送順序與各種失敗處理；再用真的後台服務（本機 socket）跑完整流程——
同步人臉與晨報、後台離線時事件留在佇列並照常辨識、恢復後補送且時間正確、重複不重寫、錯誤通行碼、狀態服務。"""
import datetime as dt
import json
import threading

import numpy as np
import pytest
import requests
from werkzeug.serving import make_server

from access import front_agent as fa
from access.front_store import FrontStore
from admin import link_cert, link_server
from shared import digest_store, link_protocol as lp, link_tls, paths, role_config as rc

TOKEN = "oGo0iq6_xu9Jp7cwTM0ttP-BTbTQ9Zefin8GaJMQpyc"
EMB = np.arange(512, dtype=np.float32) / 512
NOW = dt.datetime(2026, 11, 10, 7, 50, 0)


class FakeClient:
    """記錄呼叫的假後台客戶端；behaviour 決定每次 send_event 的結果。"""

    def __init__(self):
        self.sent, self.script, self.server_time, self.fail_ping = [], [], NOW, None

    def ping(self):
        if self.fail_ping:
            raise self.fail_ping
        return {"ok": True, "server_time": lp.format_ts(self.server_time)}

    def faces(self, have=None):
        return {"ok": True, "unchanged": True}

    def morning(self):
        return {"ok": True, "brief": None}

    def send_event(self, event):
        outcome = self.script.pop(0) if self.script else None
        if isinstance(outcome, Exception):
            raise outcome
        self.sent.append(event)
        return {"ok": True}


@pytest.fixture
def store(tmp_path):
    return FrontStore(str(tmp_path / "front.db"))


def make_agent(store, client, tmp_path, now=lambda: NOW):
    return fa.FrontAgent(store, client, now=now, heartbeat_path=str(tmp_path / "hb.json"))


# --- 假後台：補送規則 ---

def test_events_are_flushed_in_order_and_stop_at_the_first_transient_failure(store, tmp_path):
    client, agent = FakeClient(), None
    agent = make_agent(store, client, tmp_path)
    ids = [agent.submit_event("no_scan_walkby", detail=f"e{i}") for i in range(4)]
    client.script = [None, fa.BackOffline("x")]                                  # 第 2 筆就斷線
    assert agent.sync_once() is False
    assert [e["event_id"] for e in client.sent] == ids[:1] and store.pending_count() == 3
    assert agent.sync_once() is True                                             # 恢復後從第 2 筆接著送，順序不變
    assert [e["event_id"] for e in client.sent] == ids and store.pending_count() == 0


def test_a_permanent_rejection_is_set_aside_and_the_rest_still_go(store, tmp_path):
    client = FakeClient()
    agent = make_agent(store, client, tmp_path)
    ids = [agent.submit_event("no_scan_walkby", detail=f"e{i}") for i in range(3)]
    client.script = [None, fa.BackRejected("422 unknown_user"), None]
    assert agent.sync_once() is True
    assert [e["event_id"] for e in client.sent] == [ids[0], ids[2]]
    assert store.pending_count() == 0 and store.rejected_count() == 1


def test_a_wrong_token_keeps_every_event_and_says_so(store, tmp_path):
    client = FakeClient()
    agent = make_agent(store, client, tmp_path)
    agent.submit_event("no_scan_walkby")
    client.script = [fa.BackAuthError("no")]
    assert agent.sync_once() is False
    status = agent.status()
    assert status["token_rejected"] is True and status["pending_events"] == 1 and status["back_online"] is False


def test_the_event_time_uses_the_corrected_clock(store, tmp_path):
    client = FakeClient()
    client.server_time = NOW + dt.timedelta(seconds=120)                         # 後台快 2 分鐘
    agent = make_agent(store, client, tmp_path)
    assert agent.sync_once() is True
    assert agent.clock_offset() == pytest.approx(120, abs=1)
    assert agent.status()["clock_warning"] is True
    agent.submit_event("no_scan_walkby")
    assert store.pending()[0][1]["ts"] == "2026-11-10 07:52:00"
    # 重開機後離線也沿用（差存在本機）
    again = make_agent(FrontStore(store.path), FakeClient(), tmp_path)
    assert again.clock_offset() == pytest.approx(120, abs=1)


def test_a_small_clock_difference_is_not_a_warning(store, tmp_path):
    client = FakeClient()
    client.server_time = NOW + dt.timedelta(seconds=3)
    agent = make_agent(store, client, tmp_path)
    agent.sync_once()
    assert agent.status()["clock_warning"] is False


def test_the_heartbeat_must_be_fresh(store, tmp_path):
    clock = [1000.0]
    agent = fa.FrontAgent(store, FakeClient(), clock=lambda: clock[0], heartbeat_path=str(tmp_path / "hb.json"))
    assert agent.status()["recognition_running"] is False                       # 沒有心跳檔
    fa.write_heartbeat(True, True, path=agent.heartbeat_path, clock=lambda: 1000.0)
    assert (agent.status()["recognition_running"], agent.status()["camera_ok"]) == (True, True)
    fa.write_heartbeat(True, False, path=agent.heartbeat_path, clock=lambda: 1000.0)
    assert (agent.status()["recognition_running"], agent.status()["camera_ok"]) == (True, False)
    clock[0] = 1000.0 + fa.HEARTBEAT_STALE_SECONDS + 1                          # 太久沒更新 → 視為沒在跑
    assert agent.status()["recognition_running"] is False
    with open(agent.heartbeat_path, "w", encoding="utf-8") as f:
        f.write("{壞掉")
    assert agent.status()["recognition_running"] is False


def test_the_client_only_accepts_private_addresses_and_never_follows_redirects():
    with pytest.raises(rc.ConfigError):
        fa.BackClient("8.8.8.8", 8770, TOKEN)
    with pytest.raises(rc.ConfigError):
        fa.BackClient("example.com", 8770, TOKEN)

    class Session:
        def request(self, *a, **k):
            assert k["allow_redirects"] is False and a[1].startswith("https://192.168.50.1:8770/")
            raise requests.ConnectionError("down")

    with pytest.raises(fa.BackOffline):
        fa.BackClient("192.168.50.1", 8770, TOKEN, session=Session()).ping()


# --- 真的後台服務（本機 socket）---

@pytest.fixture
def backend(db, add_user, monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "ANOMALY_DIR", str(tmp_path / "evidence"))
    monkeypatch.setattr(paths, "BRIEF_PATH", str(tmp_path / "brief.json"))
    monkeypatch.setenv(rc.TOKEN_ENV, TOKEN)
    cert_path, key_path = link_cert.ensure_certificate(str(tmp_path / "tls"))
    add_user("甲", "E1", embedding=EMB)
    add_user("乙", "E2", embedding=EMB + 5)
    digest_store.save_brief({"date": "2026-11-10", "generated_at": "2026-11-10 07:00", "news": [{"title": "新聞一"}],
                             "news_problem": "", "news_tries": 1})

    class Back:
        server = None

        def start(self, port=0):
            self.server = make_server("127.0.0.1", port, link_server.create_app(now=lambda: NOW), threaded=True,
                                      ssl_context=link_tls.server_context(cert_path, key_path))
            self.port = self.server.port
            threading.Thread(target=self.server.serve_forever, daemon=True).start()

        def stop(self):
            self.server.shutdown()
            self.server.server_close()

    back = Back()
    back.fingerprint = link_tls.fingerprint_of_pem(cert_path)
    back.start()
    yield back
    back.stop()


def real_agent(backend, tmp_path, token=TOKEN):
    client = fa.BackClient("127.0.0.1", backend.port, token, backend.fingerprint, timeout=(1.0, 3.0))
    return make_agent(FrontStore(str(tmp_path / "front.db")), client, tmp_path)


def test_end_to_end_sync_offline_recognition_and_catch_up(backend, db, tmp_path):
    import sqlite3

    agent = real_agent(backend, tmp_path)
    assert agent.sync_once() is True
    assert len(agent.index) == 2 and agent.store.get_meta("morning")["news"] == [{"title": "新聞一"}]
    name, dist, user_id = agent.index.recognize(EMB)                             # 後台在線時用快取辨識
    assert (name, dist) == ("甲", 0.0)

    backend.stop()                                                                # 後台斷線
    assert agent.sync_once() is False and agent.status()["back_online"] is False
    assert agent.index.recognize(EMB)[0] == "甲"                                  # 離線照樣辨識
    agent.submit_punch(user_id)
    agent.submit_event("tailgate", detail="畫面有 2 人", evidence=b"\xff\xd8\xff" + b"2" * 40)
    assert agent.status()["pending_events"] == 2

    backend.start(port=backend.port)                                              # 後台恢復（同一個埠）
    assert agent.sync_once() is True and agent.store.pending_count() == 0
    con = sqlite3.connect(db)
    attendance = [(r[1], r[2], r[3]) for r in con.execute("SELECT * FROM Attendance")]
    assert attendance == [("甲", "2026-11-10 07:50:00", "上班")]
    assert con.execute("SELECT event_type, evidence_path FROM AnomalyEvents").fetchone()[0] == "tailgate"
    con.close()

    # 送出後、收到回應前斷線 → 前台以為沒送成而重送同一筆：後台認得編號，不會重複處理
    # （若重複處理，30 秒內同類型會合併、event_count 變 2）
    walk = lp.build_event("no_scan_walkby", NOW, event_id="b" * 32)
    agent.store.enqueue(walk)
    assert agent.sync_once() is True
    agent.store.enqueue(dict(walk))                                               # 同一個 event_id 再入列
    assert agent.sync_once() is True and agent.store.pending_count() == 0
    con = sqlite3.connect(db)
    assert con.execute("SELECT event_count FROM AnomalyEvents WHERE event_type='no_scan_walkby'").fetchall() == [(1,)]
    con.close()


def test_a_wrong_token_is_reported_and_nothing_is_lost(backend, tmp_path):
    agent = real_agent(backend, tmp_path, token="z" * 40)
    agent.submit_event("no_scan_walkby")
    assert agent.sync_once() is False
    assert agent.status()["token_rejected"] is True and agent.status()["pending_events"] == 1


def test_a_punch_for_a_deleted_user_is_set_aside_not_retried_forever(backend, tmp_path):
    agent = real_agent(backend, tmp_path)
    agent.sync_once()
    agent.submit_punch(9999)
    assert agent.sync_once() is True
    assert agent.status()["pending_events"] == 0 and agent.status()["rejected_events"] == 1


def test_face_changes_on_the_backend_reach_the_front(backend, add_user, tmp_path):
    agent = real_agent(backend, tmp_path)
    agent.sync_once()
    version = agent.store.faces_version()
    agent.sync_once()
    assert agent.store.faces_version() == version                                # 沒變就不重載
    add_user("丙", "E3", embedding=EMB + 9)
    agent.sync_once()
    assert len(agent.index) == 3 and agent.store.faces_version() != version


def test_the_status_service_needs_the_token_and_shows_no_faces(backend, tmp_path, monkeypatch):
    agent = real_agent(backend, tmp_path)
    agent.sync_once()
    server = make_server("127.0.0.1", 0, fa.create_status_app(agent), threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.port}/v1/status"
        assert requests.get(url, timeout=3).status_code == 401
        assert requests.get(url, headers={"Authorization": "Bearer wrong"}, timeout=3).status_code == 401
        body = requests.get(url, headers={"Authorization": f"Bearer {TOKEN}"}, timeout=3).json()
        assert body["ok"] and body["back_online"] and body["faces"] == 2 and body["pending_events"] == 0
        text = json.dumps(body, ensure_ascii=False)
        assert "甲" not in text and "embedding" not in text                       # 狀態不含人臉與姓名
    finally:
        server.shutdown()
        server.server_close()

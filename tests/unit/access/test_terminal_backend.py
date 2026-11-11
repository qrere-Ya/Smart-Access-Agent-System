"""門禁終端機的資料後端與打卡畫面：問候語、單機版（包住資料庫）、前台版（快取辨識、打卡進佇列、事件節流、離線提示、心跳）、
後端選擇，以及畫面中間的半透明黑框。"""
import datetime as dt

import cv2
import numpy as np
import pytest

from access import terminal_backend as tb, utils
from access.front_store import FrontStore
from shared import link_protocol as lp, role_config as rc

rng = np.random.default_rng(3)
EMB = rng.normal(size=512).astype(np.float32)
MORNING = dt.datetime(2026, 11, 10, 7, 50, 0)
EVENING = dt.datetime(2026, 11, 10, 17, 50, 0)


# --- 問候語 ---

@pytest.mark.parametrize("hour,expected", [(0, "早安"), (11, "早安"), (12, "午安"), (17, "午安"), (18, "晚安"),
                                           (23, "晚安")])
def test_greeting_by_time(hour, expected):
    assert tb.greeting_for(hour) == expected


# --- 單機 ---

def test_local_backend_recognizes_and_punches_like_before(db, add_user):
    add_user("甲", "E1", embedding=EMB)
    backend = tb.LocalBackend()
    rec = backend.recognize(EMB)
    assert (rec.name, rec.employee_id, rec.known) == ("甲", "E1", True)
    assert backend.recognize(EMB + 50).known is False
    morning = backend.punch(rec, now=MORNING)
    assert (morning.text, morning.success, morning.greeting) == ("上班報到成功", True, "早安")
    assert morning.audio == "甲，早安"
    evening = backend.punch(rec, now=EVENING)
    assert (evening.text, evening.success, evening.greeting) == ("下班打卡成功", True, "午安")
    assert evening.audio == "甲，午安"


def test_local_backend_unsuccessful_punches_show_the_database_message_and_stay_quiet(db, add_user):
    add_user("甲", "E1", embedding=EMB)
    backend = tb.LocalBackend()
    rec = backend.recognize(EMB)
    outcome = backend.punch(rec, now=dt.datetime(2026, 11, 10, 13, 0, 0))                # 午休時段第一次刷臉
    assert outcome.success is False and outcome.audio is None and outcome.text == "非打卡時段"


def test_local_backend_announces_the_calendar_only_after_clocking_in(db, add_user):
    import sqlite3

    add_user("甲", "E1", embedding=EMB)
    con = sqlite3.connect(db)
    con.execute("INSERT INTO calendar (emp_id, event_time, event_title) VALUES (?, ?, ?)", ("E1", "10:00", "週會"))
    con.commit()
    con.close()
    backend = tb.LocalBackend()
    rec = backend.recognize(EMB)
    assert backend.punch(rec, now=MORNING).calendar == "甲，10:00，週會"
    assert backend.punch(rec, now=EVENING).calendar is None                       # 下班不播報


def test_local_backend_records_anomalies_with_a_photo(db, monkeypatch, tmp_path):
    from shared import paths
    import sqlite3

    monkeypatch.setattr(paths, "ANOMALY_DIR", str(tmp_path / "evidence"))
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    tb.LocalBackend().anomaly("tailgate", "畫面有 2 人", frame, (100, 100, 300, 400))
    con = sqlite3.connect(db)
    row = con.execute("SELECT event_type, evidence_path FROM AnomalyEvents").fetchone()
    con.close()
    assert row[0] == "tailgate" and row[1].endswith(".jpg")


# --- 前台 ---

@pytest.fixture
def store(tmp_path):
    store = FrontStore(str(tmp_path / "front.db"))
    store.replace_faces("a" * 64, [{"id": 7, "name": "甲", "employee_id": "E7", "role": "employee", "embedding": EMB}])
    return store


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def front(store, when=MORNING, clock=None, **kwargs):
    heartbeat = kwargs.pop("heartbeat", lambda *a: None)
    return tb.FrontBackend(store, now=lambda: when, clock=clock or Clock(), heartbeat=heartbeat, **kwargs)


def test_front_recognizes_from_the_local_cache_even_when_the_back_is_offline(store):
    backend = front(store)
    rec = backend.recognize(EMB)
    assert (rec.name, rec.user_id, rec.employee_id, rec.known) == ("甲", 7, "E7", True)
    assert backend.recognize(EMB + 50).known is False


def test_front_picks_up_new_faces_after_the_agent_syncs(store):
    clock = Clock()
    backend = front(store, clock=clock)
    other = rng.normal(size=512).astype(np.float32)
    assert backend.recognize(other).known is False
    store.replace_faces("b" * 64, [
        {"id": 7, "name": "甲", "employee_id": "E7", "role": "employee", "embedding": EMB},
        {"id": 8, "name": "乙", "employee_id": "E8", "role": "employee", "embedding": other}])
    assert backend.recognize(other).known is False                           # 還沒到重新檢查的時間
    clock.t += tb.INDEX_REFRESH_SECONDS + 1
    assert backend.recognize(other).name == "乙"


def test_front_punch_is_queued_and_shows_success_with_the_greeting(store):
    backend = front(store, when=MORNING)
    outcome = backend.punch(backend.recognize(EMB))
    assert (outcome.text, outcome.success, outcome.greeting, outcome.audio) == ("打卡成功", True, "早安", "甲，早安")
    (_seq, event), = store.pending()
    assert event["kind"] == "punch" and event["user_id"] == 7 and event["ts"] == "2026-11-10 07:50:00"
    evening = front(store, when=EVENING).punch(backend.recognize(EMB))
    assert evening.greeting == "午安" and evening.audio == "甲，午安"
    assert outcome.voice_key != evening.voice_key                            # 早晚的語音各播一次


def test_front_uses_the_corrected_clock_for_the_punch_time(store):
    store.set_meta("clock_offset", 125.0)
    backend = front(store, when=MORNING)
    backend.punch(backend.recognize(EMB))
    assert store.pending()[0][1]["ts"] == "2026-11-10 07:52:05"


@pytest.mark.parametrize("when", [dt.datetime(2026, 11, 10, 6, 0, 0), dt.datetime(2026, 11, 10, 7, 39, 59),
                                  dt.datetime(2026, 11, 10, 12, 0, 0), dt.datetime(2026, 11, 10, 16, 29, 59)])
def test_front_does_not_queue_punches_in_times_that_can_never_be_recorded(store, when):
    backend = front(store, when=when)
    outcome = backend.punch(backend.recognize(EMB))
    assert outcome.text == "非打卡時段" and outcome.success is False and outcome.audio is None
    assert store.pending_count() == 0


def test_front_anomalies_are_throttled_and_carry_a_valid_jpeg(store):
    clock = Clock()
    backend = front(store, clock=clock)
    frame = rng.integers(0, 255, size=(480, 540, 3), dtype=np.uint8)
    for _ in range(5):
        backend.anomaly("tailgate", "畫面有 2 人", frame, (50, 50, 300, 450))
        clock.t += 1
    assert store.pending_count() == 1                                        # 30 秒內只送一筆
    backend.anomaly("no_scan_walkby", "", frame, (50, 50, 300, 450))         # 不同種類各算各的
    assert store.pending_count() == 2
    clock.t += tb.db.ANOMALY_MERGE_SECONDS
    backend.anomaly("tailgate", "畫面有 2 人", frame, (50, 50, 300, 450))
    assert store.pending_count() == 3
    event = store.pending()[0][1]
    checked = lp.validate_event(event, now=dt.datetime(2026, 11, 10, 8, 0, 0))
    assert checked["evidence"].startswith(b"\xff\xd8\xff")
    assert cv2.imdecode(np.frombuffer(checked["evidence"], np.uint8), cv2.IMREAD_COLOR) is not None


def test_front_note_says_the_back_is_offline_and_how_many_events_are_waiting(store):
    clock = Clock(2000.0)
    backend = front(store, clock=clock)
    assert backend.note() == "後台離線"                                        # 代理沒在跑／沒回報過
    backend.punch(backend.recognize(EMB))
    assert backend.note() == "後台離線，已暫存 1 筆事件"
    store.set_link_state(True, now=lambda: __import__("time").time())
    assert backend.note() == ""


def test_front_heartbeat_reports_running_then_stopped(store):
    calls = []
    backend = front(store, heartbeat=lambda running, camera: calls.append((running, camera)))
    backend.heartbeat(True)
    backend.heartbeat(False)
    backend.close()
    assert calls == [(True, True), (True, False), (False, False)]


def test_a_failing_evidence_does_not_break_recognition(store, monkeypatch):
    backend = front(store)
    monkeypatch.setattr(tb.sec, "encode_evidence_jpeg", lambda *a, **k: (_ for _ in ()).throw(ValueError("x")))
    backend.anomaly("tailgate", "", np.zeros((100, 100, 3), np.uint8), (0, 0, 50, 50))
    assert store.pending_count() == 0


# --- 後端選擇 ---

def test_the_backend_follows_the_role(tmp_path, monkeypatch):
    from shared import paths

    monkeypatch.setattr(paths, "RUNTIME_DIR", str(tmp_path))
    assert isinstance(tb.create_backend(rc.LinkSettings(rc.STANDALONE)), tb.LocalBackend)
    assert isinstance(tb.create_backend(rc.LinkSettings(rc.BACK, bind_host="192.168.50.1")), tb.LocalBackend)
    chosen = tb.create_backend(rc.LinkSettings(rc.FRONT, back_host="192.168.50.1", bind_host="192.168.50.2",
                                                    back_fingerprint="ab" * 32))
    assert isinstance(chosen, tb.FrontBackend)


# --- 畫面中間的半透明黑框 ---

def test_the_message_box_is_centered_dark_but_see_through_and_leaves_the_rest_alone():
    img = np.full((720, 1280, 3), 200, dtype=np.uint8)
    out = utils.draw_message_box(img, [("打卡成功", (0, 255, 0), 54), ("姓名 甲　早安", (255, 255, 255), 44)])
    assert out.shape == img.shape and out is not img
    corner = out[5, 5]
    assert tuple(corner) == (200, 200, 200)                                  # 框外不變
    box = out[300:420, 440:840]
    assert box.mean() < img[300:420, 440:840].mean()                         # 框內變暗
    assert out[330:340, 450:455].mean() > 0                                  # 半透明：不是全黑（底圖還透得出來）
    changed = np.argwhere(np.any(out != img, axis=2))
    assert abs(changed[:, 1].mean() - 640) < 20 and abs(changed[:, 0].mean() - 360) < 20   # 位在畫面中央
    assert (out[..., 1] > 240).any()                                         # 有畫出綠色的字


def test_the_message_box_stays_inside_a_small_image():
    img = np.zeros((60, 100, 3), dtype=np.uint8)
    out = utils.draw_message_box(img, [("打卡成功打卡成功打卡成功", (0, 255, 0), 54)])
    assert out.shape == img.shape


# --- 第二個螢幕（前台儀表板）的狀態 ---

def test_a_successful_punch_is_noted_for_the_second_screen_but_a_refused_one_is_not(store):
    backend = front(store, when=MORNING)
    backend.punch(backend.recognize(EMB))
    rows = backend.screen.recent_punches(now=lambda: __import__("time").time())
    assert [(r["name"], r["greeting"], r["clock"]) for r in rows] == [("甲", "早安", "07:50")]
    closed = front(store, when=dt.datetime(2026, 11, 10, 13, 0, 0))             # 午休時段：不記
    closed.punch(closed.recognize(EMB))
    assert len(closed.screen.recent_punches(now=lambda: __import__("time").time())) == 1


def test_alerts_are_throttled_per_kind_and_anomalies_raise_them(store):
    clock = Clock()
    backend = front(store, clock=clock)
    frame = rng.integers(0, 255, size=(480, 540, 3), dtype=np.uint8)
    seen = []
    backend.screen.set_alert = lambda kind, detail="": seen.append((kind, detail))
    backend.alert("spoof", "x")
    backend.alert("spoof", "x")                                                  # 3 秒內同一種只寫一次
    backend.alert("tailgate", "y")                                               # 不同種各算各的
    clock.t += tb.ALERT_THROTTLE_SECONDS + 0.1
    backend.alert("spoof", "x")
    assert seen == [("spoof", "x"), ("tailgate", "y"), ("spoof", "x")]
    backend.anomaly("no_scan_walkby", "", frame, (50, 50, 300, 450))             # 異常事件順便通知儀表板
    assert seen[-1][0] == "no_scan_walkby"


def test_a_failing_screen_write_never_breaks_punching_or_recognition(store, capsys):
    backend = front(store, when=MORNING)

    def broken(*args, **kwargs):
        raise OSError("disk locked")
    backend.screen.note_punch = broken
    backend.screen.set_alert = broken
    outcome = backend.punch(backend.recognize(EMB))
    backend.alert("spoof", "x")
    assert outcome.success is True and store.pending_count() == 1
    assert "儀表板狀態寫入失敗" in capsys.readouterr().out


def test_the_standalone_backend_ignores_alerts():
    tb.LocalBackend().alert("spoof", "x")                                        # 單機沒有第二個螢幕，不會出錯

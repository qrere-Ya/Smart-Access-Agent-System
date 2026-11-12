"""後台顯示用的前台狀態：存最新一份、依最後收到時間判斷在線、壞檔不丟例外、畫面文字。"""
import json
import os

import pytest

from admin import front_status as fs

GOOD = {"pending_events": 0, "rejected_events": 0, "dropped_events": 0, "faces": 3, "faces_version": "ab",
        "clock_offset_seconds": 0.2, "clock_warning": False, "recognition_running": True, "camera_ok": True}


@pytest.fixture
def path(tmp_path):
    return str(tmp_path / "runtime" / "front_status.json")


def clock_at(t):
    return lambda: t


def test_nothing_received_yet_is_reported_as_unseen_not_as_an_error(path):
    state = fs.load(path, clock=clock_at(100.0))
    assert state == {"seen": False, "online": False, "age": None, "status": {}, "remote_addr": ""}
    assert fs.describe(state)[0] == "muted"


def test_a_fresh_heartbeat_is_online_and_an_old_one_is_offline_with_its_last_seen_time(path):
    fs.record(GOOD, "169.254.127.16", path=path, clock=clock_at(1000.0))
    online = fs.load(path, clock=clock_at(1010.0))
    assert online["online"] is True and online["age"] == 10.0 and online["remote_addr"] == "169.254.127.16"
    boundary = fs.load(path, clock=clock_at(1000.0 + fs.OFFLINE_AFTER_SECONDS))
    assert boundary["online"] is True
    offline = fs.load(path, clock=clock_at(1000.0 + fs.OFFLINE_AFTER_SECONDS + 1))
    assert offline["online"] is False and offline["seen"] is True
    tone, headline, _details = fs.describe(offline)
    assert tone == "danger" and "離線" in headline and "最後收到" in headline


@pytest.mark.parametrize("content", ["", "not json", "[]", "null", '{"received_at": "x", "status": {}}',
                                     '{"status": {}}', '{"received_at": 1}'])
def test_a_broken_status_file_reads_as_unseen_and_never_raises(path, content):
    os.makedirs(os.path.dirname(path))
    open(path, "w", encoding="utf-8").write(content)
    state = fs.load(path, clock=clock_at(10.0))
    assert state["seen"] is False and state["online"] is False


def test_only_the_latest_heartbeat_is_kept_and_the_write_is_atomic(path):
    fs.record({**GOOD, "faces": 1}, "169.254.1.1", path=path, clock=clock_at(1.0))
    fs.record({**GOOD, "faces": 9}, "169.254.1.1", path=path, clock=clock_at(2.0))
    assert fs.load(path, clock=clock_at(3.0))["status"]["faces"] == 9
    assert not os.path.exists(path + ".tmp")
    assert len(json.load(open(path, encoding="utf-8"))) == 3                  # 只有 received_at、remote_addr、status


def test_the_description_names_camera_recognition_pending_events_and_a_clock_warning(path):
    fs.record({**GOOD, "camera_ok": False, "recognition_running": False, "pending_events": 4, "rejected_events": 1,
               "dropped_events": 2, "clock_warning": True, "clock_offset_seconds": 125.0}, "x", path=path,
              clock=clock_at(50.0))
    tone, headline, details = fs.describe(fs.load(path, clock=clock_at(53.0)))
    text = "\n".join(details)
    assert tone == "success" and "連線中" in headline and "3 秒前" in headline
    assert "鏡頭異常或未啟動" in text and "人臉辨識未執行" in text
    assert "待補送事件 4 筆" in text and "拒收 1 筆" in text and "丟棄 2 筆" in text and "125.0" in text


def test_a_healthy_front_shows_no_warning_lines(path):
    fs.record(GOOD, "x", path=path, clock=clock_at(0.0))
    _tone, _headline, details = fs.describe(fs.load(path, clock=clock_at(1.0)))
    assert len(details) == 2 and "鏡頭正常" in details[0] and "人臉辨識執行中" in details[0]


@pytest.mark.parametrize("seconds,expected", [(5, "5 秒前"), (59, "59 秒前"), (60, "1 分鐘前"), (3599, "59 分鐘前"),
                                              (7200, "2 小時前")])
def test_elapsed_time_is_written_in_plain_words(seconds, expected):
    assert fs._ago(seconds) == expected


def test_badge_view_covers_never_seen_offline_degraded_and_healthy():
    from admin import front_status as fs

    never = {"seen": False, "online": False, "age": 0, "status": {}, "remote_addr": ""}
    assert fs.badge_view(never) == ("neutral", "● 前台未連線")
    off = {"seen": True, "online": False, "age": 45, "status": {}, "remote_addr": ""}
    kind, text = fs.badge_view(off)
    assert kind == "danger" and "前台離線" in text and "45 秒前" in text
    idle = {"seen": True, "online": True, "age": 2, "status": {"camera_ok": True, "recognition_running": False},
            "remote_addr": ""}
    assert fs.badge_view(idle) == ("warning", "● 前台連線中（辨識未執行）")
    good = {"seen": True, "online": True, "age": 2, "status": {"camera_ok": True, "recognition_running": True},
            "remote_addr": ""}
    assert fs.badge_view(good) == ("success", "● 前台連線中")

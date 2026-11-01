"""test_anomaly_notifier.py：異常通知的輪詢與已讀邏輯（不需顯示器）。"""
import json

from admin import anomaly_notifier as an
from admin import notification_bar


def _ev(i):
    return {"id": i, "timestamp": f"2026-11-01 10:00:{i:02d}", "event_type": "tailgate", "detail": f"d{i}"}


def _make(tmp_path, events):
    return an.AnomalyNotifier(str(tmp_path / "seen.json"), fetch=lambda limit=200: list(reversed(events[-limit:])))


def test_first_run_baselines_history(tmp_path):
    n = _make(tmp_path, [_ev(1), _ev(2)])
    r = n.poll()
    assert r.unread_count == 0 and r.new == []
    assert json.load(open(tmp_path / "seen.json"))["last_seen_id"] == 2


def test_new_events_counted_and_merged(tmp_path):
    events = [_ev(1)]
    n = _make(tmp_path, events)
    n.poll()
    events += [_ev(2), _ev(3)]
    r = n.poll()
    assert r.unread_count == 2 and [e["id"] for e in r.new] == [3, 2]
    assert n.poll().new == []            # 不重複提示
    assert n.poll().unread_count == 2    # 但仍未讀


def test_mark_all_read_persists(tmp_path):
    events = [_ev(1)]
    n = _make(tmp_path, events)
    n.poll()
    events.append(_ev(2))
    n.poll()
    n.mark_all_read()
    assert n.poll().unread_count == 0
    n2 = _make(tmp_path, events)
    assert n2.poll().unread_count == 0
    events.append(_ev(3))
    assert n2.poll().unread_count == 1


def test_unseen_event_not_marked_read(tmp_path):
    events = [_ev(1)]
    n = _make(tmp_path, events)
    n.poll()
    events.append(_ev(2))
    n.poll()
    events.append(_ev(3))      # 還沒 poll 到就按已讀：3 不能被吃掉
    n.mark_all_read()
    assert n.poll().unread_count == 1


def test_existing_unread_on_restart_no_popup(tmp_path):
    (tmp_path / "seen.json").write_text(json.dumps({"last_seen_id": 1}))
    n = _make(tmp_path, [_ev(1), _ev(2)])
    r = n.poll()
    assert r.unread_count == 1 and r.new == []


def test_corrupt_state_and_fetch_failure(tmp_path):
    (tmp_path / "seen.json").write_text("{壞掉")
    n = _make(tmp_path, [_ev(1)])
    assert n.last_seen is None

    def boom(limit=200):
        raise RuntimeError("db")
    assert an.AnomalyNotifier(str(tmp_path / "x.json"), fetch=boom).poll() is None


def test_popup_text_truncates():
    text = notification_bar.format_popup_text([_ev(i) for i in range(8)])
    assert text.count("\n") == notification_bar.MAX_LINES and "另有 3 筆" in text

"""test_anomaly_notifier.py：異常通知的輪詢與已讀邏輯（不需顯示器）。"""
import json

from admin import anomaly_notifier as an


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
    text = an.format_popup_text([_ev(i) for i in range(8)])
    assert text.count("\n") == an.MAX_LINES and "另有 3 筆" in text


# --- 證據路徑安全檢查與列表文字 ---

def _photo(folder, name="tailgate_1.jpg"):
    path = folder / name
    path.write_bytes(b"x")
    return path


def test_evidence_path_inside_the_folder_is_accepted(tmp_path):
    photo = _photo(tmp_path)
    assert an.safe_evidence_path(str(photo), str(tmp_path)) == str(photo.resolve())
    assert an.safe_evidence_path(photo.name, str(tmp_path)) == str(photo.resolve())   # 相對路徑


def test_evidence_path_outside_the_folder_is_rejected(tmp_path):
    folder = tmp_path / "evidence"
    folder.mkdir()
    secret = _photo(tmp_path, "secret.txt")
    assert an.safe_evidence_path(str(secret), str(folder)) is None            # 資料夾外的檔案
    assert an.safe_evidence_path("..\\secret.txt", str(folder)) is None       # 用 .. 跳出去
    assert an.safe_evidence_path("../secret.txt", str(folder)) is None
    sibling = tmp_path / "evidence_other"                                                   # 名稱前綴相同的別的資料夾
    sibling.mkdir()
    assert an.safe_evidence_path(str(_photo(sibling)), str(folder)) is None


def test_evidence_path_must_be_an_existing_file(tmp_path):
    assert an.safe_evidence_path(str(tmp_path / "missing.jpg"), str(tmp_path)) is None
    assert an.safe_evidence_path(str(tmp_path), str(tmp_path)) is None         # 資料夾本身不是檔案
    assert an.safe_evidence_path("", str(tmp_path)) is None
    assert an.safe_evidence_path(None, str(tmp_path)) is None


def test_event_row_marks_unread_and_flattens_the_detail(tmp_path):
    photo = _photo(tmp_path)
    event = {"id": 5, "timestamp": "2026-11-03 20:21:19", "event_type": "tailgate",
             "detail": "畫面有 2 人\n  只辨識到 1 張臉", "evidence_path": str(photo), "event_count": 5}
    assert an.event_row(event, 3, str(tmp_path)) == (
        "● 未讀", "2026-11-03 20:21:19", "尾隨", "畫面有 2 人 只辨識到 1 張臉", 5, "有")
    assert an.event_row(event, 5, str(tmp_path))[0] == "已讀"
    assert an.event_row(event, None, str(tmp_path))[0] == "已讀"      # 沒有已讀基準時不亂標未讀


def test_event_row_without_photo_or_count(tmp_path):
    event = {"id": 1, "timestamp": "t", "event_type": "no_scan_walkby", "detail": None, "evidence_path": None,
             "event_count": None}
    assert an.event_row(event, 0, str(tmp_path)) == ("● 未讀", "t", "經過鏡頭未打卡", "", 1, "無")

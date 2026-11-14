"""
database_mgr 單元測試：考勤時間窗判斷、員工註冊、人臉比對、資料表建立與升級、稽核紀錄寫入。
使用臨時資料庫，並用 clock fixture 固定目前時間。
"""
import datetime
import sqlite3

import numpy as np
import pytest

from shared import database_mgr as dbm

DAY = (2026, 9, 2)


def test_array_adapter_roundtrip():
    arr = np.arange(6, dtype=np.float32)
    assert np.array_equal(dbm.convert_array(bytes(dbm.adapt_array(arr))), arr)


@pytest.mark.parametrize("hms, expected", [
    ((7, 39, 59), "非打卡時段"),
    ((7, 40, 0), "In: 王小明 (上班)"),
    ((8, 0, 0), "In: 王小明 (上班)"),
    ((8, 0, 1), "In: 王小明 (遲到)"),
    ((11, 59, 59), "In: 王小明 (遲到)"),
    ((12, 0, 0), "非打卡時段"),
])
def test_first_punch_of_the_day(db, clock, attendance_rows, hms, expected):
    clock(*DAY, *hms)
    assert dbm.log_attendance("王小明") == expected
    assert len(attendance_rows("王小明")) == (0 if expected == "非打卡時段" else 1)


@pytest.mark.parametrize("hms, expected, inserted", [
    ((11, 59, 59), "Keep going: 王小明!", False),
    ((12, 0, 0), "Keep going: 王小明!", False),
    ((16, 29, 59), "Keep going: 王小明!", False),
    ((16, 30, 0), "Out: 王小明 (早退)", True),
    ((17, 39, 59), "Out: 王小明 (早退)", True),
    ((17, 40, 0), "Out: 王小明 (下班)", True),
    ((18, 0, 0), "Out: 王小明 (下班)", True),
    ((18, 0, 1), "Out: 王小明 (下班 (加班))", True),
])
def test_second_punch_of_the_day(db, clock, add_attendance, attendance_rows, hms, expected, inserted):
    add_attendance("王小明", "2026-09-02 07:50:00", "上班")
    clock(*DAY, *hms)
    assert dbm.log_attendance("王小明") == expected
    assert len(attendance_rows("王小明")) == (2 if inserted else 1)


def test_third_punch_updates_the_last_record(db, clock, add_attendance, attendance_rows):
    add_attendance("王小明", "2026-09-02 07:50:00", "上班")
    add_attendance("王小明", "2026-09-02 17:45:00", "下班")
    clock(*DAY, 18, 30)
    assert dbm.log_attendance("王小明") == "Updated: 王小明 (下班 (加班))"
    assert attendance_rows("王小明") == [("2026-09-02 07:50:00", "上班"), ("2026-09-02 18:30:00", "下班 (加班)")]


def test_third_punch_in_the_morning_or_midday_changes_nothing(db, clock, add_attendance, attendance_rows):
    add_attendance("王小明", "2026-09-02 07:50:00", "上班")
    add_attendance("王小明", "2026-09-02 17:45:00", "下班")
    before = attendance_rows("王小明")
    clock(*DAY, 11, 0)
    assert dbm.log_attendance("王小明") == "Keep going: 王小明!"
    clock(*DAY, 14, 0)
    assert dbm.log_attendance("王小明") == "Done: 王小明 (All Day)"
    assert attendance_rows("王小明") == before


def test_attendance_of_other_days_does_not_count(db, clock, add_attendance, attendance_rows):
    add_attendance("王小明", "2026-09-01 07:50:00", "上班")
    clock(*DAY, 7, 55)
    assert dbm.log_attendance("王小明") == "In: 王小明 (上班)"
    assert len(attendance_rows("王小明")) == 2


def test_attendance_is_tracked_per_employee(db, clock, add_attendance, attendance_rows):
    add_attendance("王小明", "2026-09-02 07:50:00", "上班")
    clock(*DAY, 7, 55)
    assert dbm.log_attendance("李小華") == "In: 李小華 (上班)"
    assert len(attendance_rows("王小明")) == 1


def test_register_user_and_default_calendar_event(db):
    ok, msg = dbm.register_user("王小明", "M", "E001", np.ones(4, dtype=np.float32))
    assert ok and "E001" in msg
    assert dbm.get_employee_id("王小明") == "E001"
    assert dbm.get_today_event("E001") == ("14:00", "專案會議")


def test_register_user_rejects_duplicate_name_or_employee_id(db):
    dbm.register_user("王小明", "M", "E001", np.ones(4, dtype=np.float32))
    ok, msg = dbm.register_user("王小明", "M", "E002", np.ones(4, dtype=np.float32))
    assert not ok and "姓名" in msg
    ok, msg = dbm.register_user("李小華", "F", "E001", np.ones(4, dtype=np.float32))
    assert not ok and "員工編號" in msg
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM Users").fetchone()[0] == 1
    con.close()


def test_unknown_employee_lookups(db):
    assert dbm.get_employee_id("不存在") == "N/A"
    assert dbm.get_today_event("NOPE") is None


def test_is_user_registered_by_name_and_by_embedding(db, add_user):
    assert dbm.is_user_registered("王小明")
    for placeholder in ("Unknown", "Empty DB", "N/A"):
        assert not dbm.is_user_registered(placeholder)
    add_user("王小明", "E001")
    assert dbm.is_user_registered(np.ones(4, dtype=np.float32))
    assert not dbm.is_user_registered(np.full(4, 9.0, dtype=np.float32))


def test_recognize_face_with_empty_database(db):
    assert dbm.recognize_face(np.ones(4, dtype=np.float32)) == ("Empty DB", 99.9)


def test_recognize_face_matches_and_rejects(db, add_user):
    add_user("王小明", "E001", np.zeros(4, dtype=np.float32))
    add_user("李小華", "E002", np.full(4, 0.4, dtype=np.float32))
    assert dbm.recognize_face(np.zeros(4, dtype=np.float32)) == ("王小明", 0.0)
    name, dist = dbm.recognize_face(np.full(4, 0.38, dtype=np.float32))
    assert name == "李小華" and dist < 1.0
    name, dist = dbm.recognize_face(np.full(4, 5.0, dtype=np.float32))
    assert name == "Unknown" and dist >= 1.0


def test_recognize_face_threshold_is_exclusive(db, add_user):
    add_user("王小明", "E001", np.zeros(4, dtype=np.float32))
    probe = np.array([0.5, 0, 0, 0], dtype=np.float32)
    assert dbm.recognize_face(probe, threshold=0.5)[0] == "Unknown"
    assert dbm.recognize_face(probe, threshold=0.51)[0] == "王小明"


def test_recognize_face_ignores_deactivated_accounts(db, add_user):
    add_user("王小明", "E001", np.zeros(4, dtype=np.float32), active=0)
    assert dbm.recognize_face(np.zeros(4, dtype=np.float32)) == ("Empty DB", 99.9)


def test_recognize_face_treats_null_is_active_as_active(db):
    con = sqlite3.connect(db)
    con.execute("INSERT INTO Users (name, employee_id, embedding, is_active) VALUES (?, ?, ?, NULL)",
                ("舊資料", "E009", np.zeros(4, dtype=np.float32)))
    con.commit()
    con.close()
    assert dbm.recognize_face(np.zeros(4, dtype=np.float32))[0] == "舊資料"


def test_recognize_face_keeps_the_first_of_equal_distances(db, add_user):
    add_user("先註冊", "E001", np.zeros(4, dtype=np.float32))
    add_user("後註冊", "E002", np.zeros(4, dtype=np.float32))
    assert dbm.recognize_face(np.zeros(4, dtype=np.float32))[0] == "先註冊"


def test_init_db_creates_all_tables_and_is_idempotent(db):
    dbm.init_db()
    con = sqlite3.connect(db)
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    con.close()
    assert {"Users", "Attendance", "calendar", "AnomalyEvents"} <= tables


def test_init_db_upgrades_an_old_users_table(tmp_path, monkeypatch):
    path = str(tmp_path / "old.db")
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE Users (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, gender TEXT, "
                "employee_id TEXT, embedding ARRAY NOT NULL, work_start TEXT DEFAULT '09:00', "
                "work_end TEXT DEFAULT '18:00')")
    con.execute("INSERT INTO Users (name, employee_id, embedding) VALUES (?, ?, ?)",
                ("舊員工", "E001", np.zeros(4, dtype=np.float32)))
    con.commit()
    con.close()
    monkeypatch.setattr(dbm, "DB_PATH", path)
    dbm.init_db()
    con = sqlite3.connect(path)
    assert con.execute("SELECT role, is_active FROM Users").fetchone() == ("employee", 1)
    con.close()
    assert dbm.recognize_face(np.zeros(4, dtype=np.float32))[0] == "舊員工"


def test_simultaneous_registrations_of_the_same_person_create_exactly_one_user(db):
    """回歸：查重與寫入之間沒有鎖時，同時送出的請求都能通過檢查，同一個人會被註冊好幾次（手機連按送出鍵）。"""
    import threading

    start = threading.Barrier(8)
    results = []

    def register():
        start.wait()
        results.append(dbm.register_user("王小明", "M", "E001", np.ones(4, dtype=np.float32))[0])

    threads = [threading.Thread(target=register) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM Users").fetchone()[0] == 1
    con.close()
    assert results.count(True) == 1 and results.count(False) == 7


def _run_together(count, work):
    """讓 count 個執行緒在同一個瞬間開始做 work(i)，回傳各自的結果（例外也收進來，不吞掉）。"""
    import threading

    start = threading.Barrier(count)
    results = [None] * count

    def runner(i):
        start.wait()
        try:
            results[i] = work(i)
        except BaseException as exc:          # noqa: BLE001  收起來讓測試斷言，不吞掉
            results[i] = exc

    threads = [threading.Thread(target=runner, args=(i,)) for i in range(count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


def test_simultaneous_first_punches_write_exactly_one_clock_in(db, add_user):
    """回歸：數今天幾筆與寫入之間沒有鎖時，同一人的多個事件同時處理會都看到 0 筆，寫出多筆「上班」。"""
    add_user("王小明", "E001")
    now = datetime.datetime(*DAY, 8, 0, 0)
    results = _run_together(8, lambda i: dbm.log_attendance("王小明", now=now))
    assert not [r for r in results if isinstance(r, BaseException)], results
    con = sqlite3.connect(db)
    rows = con.execute("SELECT status FROM Attendance WHERE name='王小明'").fetchall()
    con.close()
    assert rows == [("上班",)]
    assert results.count("In: 王小明 (上班)") == 1


def test_simultaneous_anomaly_events_merge_into_one_row_with_the_right_count(db):
    """回歸：讀最後一筆與寫入之間沒有鎖時，同時進來的事件會各自新增一列或少算次數。"""
    now = datetime.datetime(2026, 10, 14, 9, 0, 0)
    made = []

    def evidence():
        made.append(1)
        return "C:/x/tailgate.jpg"

    results = _run_together(8, lambda i: dbm.record_anomaly_event(
        "tailgate", "畫面有 2 人", make_evidence=evidence, now=now))
    assert not [r for r in results if isinstance(r, BaseException)], results
    events = dbm.list_anomaly_events()
    assert len(events) == 1
    assert events[0]["event_count"] == 8
    assert len(made) == 1                                   # 只有第一筆需要證據照
    assert sum(1 for _id, merged in results if merged) == 7


def test_failed_evidence_does_not_leave_a_half_written_event(db):
    """make_evidence 丟例外時，例外要往外丟，交易放棄，不留半筆，也不鎖住資料庫。"""
    def boom():
        raise RuntimeError("存證據照失敗")

    with pytest.raises(RuntimeError):
        dbm.record_anomaly_event("tailgate", "x", make_evidence=boom, now=datetime.datetime(2026, 10, 14, 9, 0, 0))
    assert dbm.list_anomaly_events() == []
    assert dbm.record_anomaly_event("tailgate", "x", now=datetime.datetime(2026, 10, 14, 9, 5, 0))[1] is False

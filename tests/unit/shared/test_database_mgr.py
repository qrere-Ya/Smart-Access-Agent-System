"""
database_mgr 單元測試：考勤時間窗判斷、員工註冊、人臉比對、資料表建立與升級、稽核紀錄寫入。
使用臨時資料庫，並用 clock fixture 固定目前時間。
"""
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
    assert {"Users", "Attendance", "calendar", "llm_usage_log"} <= tables


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


def test_log_llm_usage_records_events(db):
    dbm.log_llm_usage("guardrail", "問題一", guardrail_pass=True, similarity_score=0.42)
    dbm.log_llm_usage("route", "問題二", route="law")
    dbm.log_llm_usage("basic", "問題三")
    con = sqlite3.connect(db)
    rows = con.execute("SELECT event_type, question, route, guardrail_pass, similarity_score, source "
                       "FROM llm_usage_log ORDER BY id").fetchall()
    con.close()
    assert rows == [
        ("guardrail", "問題一", None, 1, 0.42, "gradio"),
        ("route", "問題二", "law", None, None, "gradio"),
        ("basic", "問題三", None, None, None, "gradio"),
    ]


def test_log_llm_usage_never_raises(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(dbm, "DB_PATH", str(tmp_path))  # 資料夾不是資料庫檔，寫入會失敗
    dbm.log_llm_usage("basic", "會失敗的寫入")
    assert "WARN" in capsys.readouterr().out

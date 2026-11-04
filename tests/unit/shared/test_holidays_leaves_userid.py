"""
假日、請假與 Attendance.user_id 的單元測試：輸入驗證、舊資料庫遷移與回填、打卡寫入 user_id、
出勤報告的假日／請假／同名處理。
"""
import sqlite3

import pytest

from admin import attendance_report as ar
from shared import database_mgr

DAY = "2026-10-14"


def _punch(db, name, time, status, user_id=None, day=DAY):
    con = sqlite3.connect(db)
    con.execute("INSERT INTO Attendance (name, timestamp, status, user_id) VALUES (?, ?, ?, ?)",
                (name, f"{day} {time}", status, user_id))
    con.commit()
    con.close()


def _uid(db, name):
    con = sqlite3.connect(db)
    try:
        return con.execute("SELECT id FROM Users WHERE name=?", (name,)).fetchone()[0]
    finally:
        con.close()


def test_holiday_crud_and_validation(db):
    database_mgr.add_holiday("2026-10-10", "國慶日")
    database_mgr.add_holiday("2026-10-10", "雙十")  # 同日期更新名稱
    assert database_mgr.list_holidays() == [{"date": "2026-10-10", "name": "雙十"}]
    assert database_mgr.is_holiday("2026-10-10") and not database_mgr.is_holiday("2026-10-11")
    assert database_mgr.list_holidays(start="2026-10-11") == []
    for bad in ("2026/10/10", "2026-13-01", "", "x'; DROP TABLE Users;--"):
        with pytest.raises(ValueError):
            database_mgr.add_holiday(bad)
    assert database_mgr.remove_holiday("2026-10-10") is True
    assert database_mgr.remove_holiday("2026-10-10") is False


def test_leave_crud_and_validation(db, add_user):
    add_user("甲", "1")
    uid = _uid(db, "甲")
    lid = database_mgr.add_leave(uid, "2026-10-13", "2026-10-15", "病假", "感冒")
    assert database_mgr.list_leaves(user_id=uid)[0]["kind"] == "病假"
    assert len(database_mgr.list_leaves(on_date="2026-10-14")) == 1
    assert database_mgr.list_leaves(on_date="2026-10-16") == []
    with pytest.raises(ValueError):
        database_mgr.add_leave(uid, "2026-10-15", "2026-10-13")  # 起訖顛倒
    with pytest.raises(ValueError):
        database_mgr.add_leave(uid, "bad", "2026-10-13")
    with pytest.raises(ValueError):
        database_mgr.add_leave(9999, "2026-10-13", "2026-10-13")  # 員工不存在
    assert database_mgr.remove_leave(lid) is True
    assert database_mgr.remove_leave(lid) is False


def test_migration_adds_column_and_backfills(tmp_path, monkeypatch):
    path = str(tmp_path / "old.db")
    monkeypatch.setattr(database_mgr, "DB_PATH", path)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE Users (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, gender TEXT, "
                "employee_id TEXT, embedding ARRAY NOT NULL, work_start TEXT, work_end TEXT)")
    con.execute("CREATE TABLE Attendance (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, "
                "timestamp DATETIME NOT NULL, status TEXT NOT NULL)")
    for n, e in [("獨一", "1"), ("同名", "2"), ("同名", "3")]:
        con.execute("INSERT INTO Users (name, employee_id, embedding) VALUES (?, ?, ?)", (n, e, b"x"))
    for n in ("獨一", "同名", "不存在"):
        con.execute("INSERT INTO Attendance (name, timestamp, status) VALUES (?, ?, ?)",
                    (n, f"{DAY} 08:00:00", "上班"))
    con.commit()
    con.close()
    database_mgr.init_db()
    database_mgr.init_db()  # 重複執行不出錯
    con = sqlite3.connect(path)
    got = dict(con.execute("SELECT name, user_id FROM Attendance").fetchall())
    con.close()
    assert got["獨一"] == 1
    assert got["同名"] is None and got["不存在"] is None


def test_log_attendance_writes_user_id(db, add_user, clock):
    add_user("乙", "2")
    clock(2026, 10, 14, 7, 50)
    database_mgr.log_attendance("乙")
    con = sqlite3.connect(db)
    uid = con.execute("SELECT user_id FROM Attendance WHERE name='乙'").fetchone()[0]
    con.close()
    assert uid == _uid(db, "乙")


def test_report_holiday_and_leave(db, add_user):
    add_user("甲", "1")
    add_user("乙", "2")
    database_mgr.add_leave(_uid(db, "乙"), DAY, DAY, "事假")
    database_mgr.add_holiday(DAY, "補假")
    rows = {r["name"]: r for r in ar.build_report(DAY)["rows"]}
    assert rows["甲"]["status"] == ar.STATUS_HOLIDAY and rows["甲"]["note"] == "補假"
    assert rows["乙"]["status"] == ar.STATUS_HOLIDAY  # 假日優先
    database_mgr.remove_holiday(DAY)
    report = ar.build_report(DAY)
    rows = {r["name"]: r for r in report["rows"]}
    assert rows["乙"]["status"] == ar.STATUS_LEAVE and "事假" in rows["乙"]["note"]
    assert rows["甲"]["status"] == ar.STATUS_ABSENT
    assert report["counts"] == {ar.STATUS_ABSENT: 1, ar.STATUS_LEAVE: 1}


def test_report_punch_on_holiday_keeps_actual_status(db, add_user):
    add_user("甲", "1")
    database_mgr.add_holiday(DAY, "加班日")
    _punch(db, "甲", "07:50:00", "上班", user_id=_uid(db, "甲"))
    assert ar.build_report(DAY)["rows"][0]["status"] == ar.STATUS_NO_CLOCK_OUT


def test_report_uses_user_id_over_name(db, add_user):
    add_user("甲", "1")
    add_user("乙", "2")
    # 乙的 user_id 對到，但姓名欄是舊名字：以 user_id 為準
    _punch(db, "舊名", "07:50:00", "上班", user_id=_uid(db, "乙"))
    rows = {r["name"]: r for r in ar.build_report(DAY)["rows"]}
    assert rows["乙"]["status"] == ar.STATUS_NO_CLOCK_OUT and rows["甲"]["status"] == ar.STATUS_ABSENT


def test_report_legacy_name_fallback_and_duplicate_names(db, add_user):
    add_user("獨一", "1")
    add_user("同名", "2")
    add_user("同名", "3")
    _punch(db, "獨一", "07:50:00", "上班")  # 無 user_id：退回姓名
    _punch(db, "同名", "07:50:00", "上班")  # 同名多人且無 user_id：不猜測
    rows = ar.build_report(DAY)["rows"]
    st = {(r["name"], r["employee_id"]): r["status"] for r in rows}
    assert st[("獨一", "1")] == ar.STATUS_NO_CLOCK_OUT
    assert st[("同名", "2")] == ar.STATUS_ABSENT and st[("同名", "3")] == ar.STATUS_ABSENT


def test_report_duplicate_names_separated_by_user_id(db, add_user):
    add_user("同名", "2")
    add_user("同名", "3")
    con = sqlite3.connect(db)
    ids = [r[0] for r in con.execute("SELECT id FROM Users ORDER BY employee_id")]
    con.close()
    _punch(db, "同名", "07:50:00", "上班", user_id=ids[1])
    st = {r["employee_id"]: r["status"] for r in ar.build_report(DAY)["rows"]}
    assert st["2"] == ar.STATUS_ABSENT and st["3"] == ar.STATUS_NO_CLOCK_OUT

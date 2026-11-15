"""
Attendance.user_id 的單元測試：舊資料庫遷移與回填、打卡寫入 user_id、出勤報告的同名處理。
（假日與請假功能已移除，休息日改由 work_calendar 自動判斷，見 test_work_calendar.py）
"""
import sqlite3


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

"""
以員工識別認人的單元測試：出勤報告的週休、助理查詢與摘要（同名舊員工不混、休息日）、
儀表板與帳號列表的人數與最後狀態。休息日的判斷見 test_work_calendar.py。
"""
import datetime
import sqlite3


from admin import account_admin, attendance_report as ar, dashboard_stats
from rag import answer, queries

WED, SAT, SUN = "2026-10-14", "2026-10-17", "2026-10-18"


def _uid(db, name):
    con = sqlite3.connect(db)
    try:
        return con.execute("SELECT id FROM Users WHERE name=? ORDER BY id DESC", (name,)).fetchone()[0]
    finally:
        con.close()


def _punch(db, name, day, time, status, user_id=None):
    con = sqlite3.connect(db)
    con.execute("INSERT INTO Attendance (name, timestamp, status, user_id) VALUES (?, ?, ?, ?)",
                (name, f"{day} {time}", status, user_id))
    con.commit()
    con.close()


def test_the_test_dates_are_what_the_tests_assume():
    assert [datetime.date.fromisoformat(d).weekday() for d in (WED, SAT, SUN)] == [2, 5, 6]


def test_report_shows_the_weekly_rest_day_not_absence(db, add_user):
    add_user("甲", "1")
    add_user("乙", "2")
    _punch(db, "乙", SAT, "10:00:00", "上班", user_id=_uid(db, "乙"))
    rows = {r["name"]: r for r in ar.build_report(SAT)["rows"]}
    assert rows["甲"]["status"] == ar.STATUS_HOLIDAY and "週六" in rows["甲"]["note"]
    assert rows["乙"]["status"] != ar.STATUS_HOLIDAY       # 休息日仍有打卡，以實際打卡為準
    assert ar.build_report(WED)["rows"][0]["status"] == ar.STATUS_ABSENT


# --- 助理查詢：以員工識別認人 ---

def _deleted_then_new_same_name(db, add_user):
    """舊員工「甲」離職被刪除（打卡紀錄保留，帶舊 user_id），之後新員工也叫「甲」。"""
    add_user("甲", "1")
    old_id = _uid(db, "甲")
    _punch(db, "甲", WED, "08:00:00", "遲到", user_id=old_id)
    con = sqlite3.connect(db)
    con.execute("DELETE FROM Users WHERE id=?", (old_id,))
    con.commit()
    con.close()
    add_user("甲", "9")
    new_id = _uid(db, "甲")
    _punch(db, "甲", WED, "07:50:00", "上班", user_id=new_id)
    return old_id, new_id


def test_queries_do_not_mix_a_deleted_employee_with_a_new_one_of_the_same_name(db, add_user):
    _deleted_then_new_same_name(db, add_user)
    records = queries.attendance_records("甲", WED, WED)
    assert [r["status"] for r in records] == ["上班"]
    assert queries.count_by_status("甲", WED, WED) == {"上班": 1}


def test_queries_still_find_old_records_without_user_id(db, add_user):
    add_user("乙", "2")
    _punch(db, "乙", WED, "08:30:00", "遲到")             # 舊紀錄沒有 user_id
    assert [r["status"] for r in queries.attendance_records("乙")] == ["遲到"]


def test_daily_summary_counts_people_by_identity(db, add_user):
    old_id, new_id = _deleted_then_new_same_name(db, add_user)
    s = queries.daily_summary(WED)
    assert s["punched_count"] == 2                       # 兩個不同的人，雖然名字相同
    assert s["no_record"] == [] and s["day_off"] is None


def test_daily_summary_reports_the_day_off_and_who_has_no_record(db, add_user):
    add_user("甲", "1")
    add_user("乙", "2")
    s = queries.daily_summary(WED)
    assert "on_leave" not in s and set(s["no_record"]) == {"甲", "乙"}
    sat = queries.daily_summary(SAT)
    assert sat["day_off"] == "週休（週六）" and set(sat["no_record"]) == {"甲", "乙"}


def test_summary_text_mentions_the_day_off(db, add_user):
    add_user("甲", "1")
    work = answer.Assistant._summary_text(queries.daily_summary(WED))
    assert "請假" not in work and "上班日沒有打卡紀錄才算缺勤" in work
    rest = answer.Assistant._summary_text(queries.daily_summary(SAT))
    assert "週休（週六），不用上班" in rest and "休息日沒有打卡屬正常" in rest


# --- 儀表板與帳號列表 ---

def test_dashboard_counts_distinct_people_not_names(db, add_user, monkeypatch):
    old_id, new_id = _deleted_then_new_same_name(db, add_user)
    today = datetime.date.today().strftime("%Y-%m-%d")
    con = sqlite3.connect(db)
    con.execute("UPDATE Attendance SET timestamp = ? || substr(timestamp, 11)", (today,))
    con.commit()
    con.close()
    count, logs = dashboard_stats.get_today_attendance_summary()
    assert count == 2 and len(logs) == 2


def test_account_list_shows_each_persons_own_last_status(db, add_user):
    _deleted_then_new_same_name(db, add_user)
    users = account_admin.list_users()
    assert len(users) == 1 and users[0]["last_status"] == "上班"      # 不是舊員工的「遲到」

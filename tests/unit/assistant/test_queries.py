"""
queries 單元測試：名單不含人臉特徵、打卡紀錄與次數統計（日期區間含結束當天、狀態篩選）、當日名單與摘要、
異常事件、參數驗證、連線唯讀、惡意字串不被當成 SQL。
"""
import sqlite3
from contextlib import closing

import pytest

from assistant import queries


@pytest.fixture
def seeded(db, add_user, add_attendance):
    add_user("王小明", "E001")
    add_user("李大華", "E002")
    add_user("陳志遠", "E003", active=0)
    add_user("葉君緯", "E004")
    for ts, status in [("2026-10-12 07:55:00", "上班"), ("2026-10-12 17:50:00", "下班"),
                       ("2026-10-13 08:12:00", "遲到"), ("2026-10-13 17:00:00", "早退"),
                       ("2026-10-14 08:30:00", "遲到"), ("2026-10-14 18:40:00", "下班 (加班)")]:
        add_attendance("王小明", ts, status)
    add_attendance("李大華", "2026-10-13 07:50:00", "上班")
    add_attendance("李大華", "2026-10-14 08:05:00", "遲到")
    return db


def test_roster_has_only_name_id_and_active_flag(seeded):
    people = queries.roster()
    assert {p["name"] for p in people} == {"王小明", "李大華", "陳志遠", "葉君緯"}
    assert all(set(p) == {"name", "employee_id", "active"} for p in people)
    assert next(p for p in people if p["name"] == "陳志遠")["active"] is False


def test_roster_can_hide_inactive_people(seeded):
    assert "陳志遠" not in {p["name"] for p in queries.roster(include_inactive=False)}


def test_records_are_newest_first_and_can_filter_by_status(seeded):
    rows = queries.attendance_records("王小明")
    assert rows[0] == {"timestamp": "2026-10-14 18:40:00", "status": "下班 (加班)"} and len(rows) == 6
    assert [r["timestamp"][:10] for r in queries.attendance_records("王小明", status="遲到")] == [
        "2026-10-14", "2026-10-13"]


def test_date_range_includes_the_whole_end_day(seeded):
    rows = queries.attendance_records("王小明", start="2026-10-13", end="2026-10-13")
    assert {r["timestamp"][:10] for r in rows} == {"2026-10-13"} and len(rows) == 2


def test_open_ended_ranges(seeded):
    assert len(queries.attendance_records("王小明", start="2026-10-14")) == 2
    assert len(queries.attendance_records("王小明", end="2026-10-12")) == 2


def test_count_by_status(seeded):
    assert queries.count_by_status("王小明") == {"上班": 1, "下班": 1, "遲到": 2, "早退": 1, "下班 (加班)": 1}
    assert queries.count_by_status("王小明", start="2026-10-14", end="2026-10-14") == {"遲到": 1, "下班 (加班)": 1}


def test_unknown_person_has_no_records(seeded):
    assert queries.attendance_records("不存在") == [] and queries.count_by_status("不存在") == {}


def test_who_was_late_on_a_day(seeded):
    assert queries.people_with_status("2026-10-14", "遲到") == [
        {"name": "李大華", "timestamp": "2026-10-14 08:05:00"}, {"name": "王小明", "timestamp": "2026-10-14 08:30:00"}]


def test_daily_summary(seeded):
    summary = queries.daily_summary("2026-10-14")
    assert summary["punched_count"] == 2
    assert [p["name"] for p in summary["late"]] == ["李大華", "王小明"]
    assert [p["name"] for p in summary["overtime"]] == ["王小明"]
    assert summary["early_leave"] == []
    assert summary["no_record"] == ["葉君緯"]  # 在職但沒打卡；離職的陳志遠不列


def test_anomaly_events_return_paths_only(seeded):
    from shared import database_mgr as dbm

    dbm.add_anomaly_event("tailgate", "C:/x/a.jpg", "畫面有 2 人", timestamp="2026-10-14 09:15:00")
    dbm.add_anomaly_event("tailgate", "C:/x/b.jpg", "畫面有 3 人", timestamp="2026-10-13 09:15:00")
    got = queries.anomaly_events(start="2026-10-14", end="2026-10-14")
    assert got == [{"timestamp": "2026-10-14 09:15:00", "event_type": "tailgate",
                    "detail": "畫面有 2 人", "evidence_path": "C:/x/a.jpg"}]
    assert len(queries.anomaly_events(event_type="tailgate")) == 2


@pytest.mark.parametrize("bad", ["2026/10/14", "昨天", "2026-13-01", "", "2026-10-14'; DROP TABLE Users;--"])
def test_bad_dates_are_rejected(seeded, bad):
    with pytest.raises(queries.QueryError):
        queries.attendance_records("王小明", start=bad)


def test_reversed_range_is_rejected(seeded):
    with pytest.raises(queries.QueryError, match="早於"):
        queries.attendance_records("王小明", start="2026-10-14", end="2026-10-01")


def test_hostile_text_values_are_treated_as_data(seeded):
    assert queries.attendance_records("王小明' OR '1'='1") == []
    assert queries.attendance_records("王小明", status="遲到' OR '1'='1") == []
    assert len(queries.roster()) == 4


def test_limit_is_clamped(seeded):
    assert len(queries.attendance_records("王小明", limit=2)) == 2
    assert len(queries.attendance_records("王小明", limit=-5)) == 1
    assert len(queries.attendance_records("王小明", limit=10**9)) == 6


def test_connection_is_read_only(seeded):
    with closing(queries._connect()) as conn:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("DELETE FROM Attendance")
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("UPDATE Users SET name = 'x'")


def test_no_query_ever_selects_the_face_embedding_column(seeded, monkeypatch):
    """記錄所有實際送出的 SQL：不論呼叫哪個查詢，都不能碰人臉特徵欄位。"""
    from shared import database_mgr as dbm

    statements = []
    real_connect = queries._connect

    def recording():
        conn = real_connect()
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(queries, "_connect", recording)
    dbm.add_anomaly_event("tailgate", "C:/x/a.jpg", timestamp="2026-10-14 09:00:00")
    queries.roster()
    queries.roster(include_inactive=False)
    queries.attendance_records("王小明")
    queries.count_by_status("王小明")
    queries.people_with_status("2026-10-14", "遲到")
    queries.daily_summary("2026-10-14")
    queries.anomaly_events()
    assert statements and not any("embedding" in sql.lower() for sql in statements)

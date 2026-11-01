"""
attendance_report 的單元測試：各狀態判定、停用員工排除、異常事件計數、日期驗證、CSV 編碼與公式注入防護。
"""
import csv
import sqlite3

import pytest

from admin import attendance_report as ar
from shared import database_mgr

DAY = "2026-10-14"


@pytest.fixture
def punch(db):
    """回傳函式：寫入一筆打卡紀錄。"""

    def _p(name, time, status, day=DAY):
        con = sqlite3.connect(db)
        con.execute("INSERT INTO Attendance (name, timestamp, status) VALUES (?, ?, ?)",
                    (name, f"{day} {time}", status))
        con.commit()
        con.close()

    return _p


def _by_name(report):
    return {r["name"]: r for r in report["rows"]}


def test_statuses(add_user, punch):
    for n, i in [("準時者", "1"), ("遲到者", "2"), ("早退者", "3"), ("缺勤者", "4"), ("未打卡者", "5")]:
        add_user(n, i)
    punch("準時者", "07:50:00", "上班")
    punch("準時者", "18:10:00", "下班 (加班)")
    punch("遲到者", "08:30:00", "遲到")
    punch("遲到者", "17:00:00", "早退")
    punch("早退者", "07:50:00", "上班")
    punch("早退者", "17:00:00", "早退")
    punch("未打卡者", "07:50:00", "上班")
    rows = _by_name(ar.build_report(DAY))
    assert rows["準時者"]["status"] == ar.STATUS_ON_TIME and rows["準時者"]["clock_out"] == "18:10:00"
    assert rows["遲到者"]["status"] == ar.STATUS_LATE and "提早下班" in rows["遲到者"]["note"]
    assert rows["早退者"]["status"] == ar.STATUS_EARLY_LEAVE
    assert rows["缺勤者"]["status"] == ar.STATUS_ABSENT
    assert rows["未打卡者"]["status"] == ar.STATUS_NO_CLOCK_OUT


def test_inactive_excluded_and_other_days_ignored(add_user, punch):
    add_user("在職", "1")
    add_user("停用", "2", active=0)
    punch("在職", "07:50:00", "上班", day="2026-10-13")
    report = ar.build_report(DAY)
    assert [r["name"] for r in report["rows"]] == ["在職"]
    assert report["counts"] == {ar.STATUS_ABSENT: 1}


def test_anomaly_count_only_that_day(db):
    database_mgr.add_anomaly_event("tailgate", timestamp=f"{DAY} 09:00:00")
    database_mgr.add_anomaly_event("tailgate", timestamp=f"{DAY} 23:59:59")
    database_mgr.add_anomaly_event("tailgate", timestamp="2026-10-15 00:00:00")
    assert ar.build_report(DAY)["anomaly_count"] == 2


@pytest.mark.parametrize("bad", ["", "2026/10/14", "2026-13-01", "x'; DROP TABLE Users;--"])
def test_invalid_date_rejected(db, bad):
    with pytest.raises(ValueError):
        ar.build_report(bad)


def test_sanitize_cell():
    assert ar.sanitize_cell("=1+1") == "'=1+1"
    assert ar.sanitize_cell("+a") == "'+a"
    assert ar.sanitize_cell("-a") == "'-a"
    assert ar.sanitize_cell("@a") == "'@a"
    assert ar.sanitize_cell("\t=a") == "'\t=a"
    assert ar.sanitize_cell("王小明") == "王小明"
    assert ar.sanitize_cell(None) == ""


def test_export_csv_encoding_and_injection(add_user, punch, tmp_path):
    add_user("=HYPERLINK(\"http://x\")", "@001")
    path = tmp_path / "r.csv"
    report = ar.build_report(DAY)
    assert ar.export_csv(report, str(path)) == 1
    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    rows = list(csv.reader(raw.decode("utf-8-sig").splitlines()))
    assert rows[0] == ar.CSV_HEADER
    assert rows[1][0] == "'@001" and rows[1][1].startswith("'=")
    assert rows[-1] == [f"{DAY} 異常事件數", "0"]

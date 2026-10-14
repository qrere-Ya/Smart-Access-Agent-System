"""
account_admin 與 dashboard_stats 單元測試：員工清單、停用／啟用（停用後刷臉會被判定為查無資料）、
刪除只刪帳號不刪打卡歷史、儀表板統計、證據照片件數。
"""
import datetime

import numpy as np
import pytest

from admin import account_admin, dashboard_stats
from shared import database_mgr


def test_list_users_reports_role_status_and_latest_punch(db, add_user, add_attendance):
    add_user("王小明", "E001", role="manager")
    add_user("李小華", "E002", gender="F", role=None)
    add_attendance("王小明", "2026-09-01 08:00:00", "上班")
    add_attendance("王小明", "2026-09-02 08:05:00", "遲到")
    users = {u["name"]: u for u in account_admin.list_users()}
    assert set(users["王小明"]) == {
        "id", "employee_id", "name", "gender", "role", "is_active", "last_status", "last_time"}
    assert users["王小明"]["role"] == "manager" and users["王小明"]["is_active"] is True
    assert (users["王小明"]["last_status"], users["王小明"]["last_time"]) == ("遲到", "2026-09-02 08:05:00")
    assert users["李小華"]["role"] == "employee"
    assert (users["李小華"]["last_status"], users["李小華"]["last_time"]) == ("待機", "-")


def test_users_are_listed_in_registration_order(db, add_user):
    for name, emp in (("丙", "E3"), ("甲", "E1"), ("乙", "E2")):
        add_user(name, emp)
    assert [u["name"] for u in account_admin.list_users()] == ["丙", "甲", "乙"]


@pytest.mark.parametrize("stored, expected", [(1, True), (None, True), (0, False)])
def test_only_an_explicit_zero_counts_as_deactivated(db, add_user, stored, expected):
    add_user("王小明", "E001", active=stored)
    assert account_admin.list_users()[0]["is_active"] is expected


def test_deactivated_users_can_no_longer_be_recognized(db, add_user):
    add_user("王小明", "E001", np.zeros(4, dtype=np.float32))
    uid = account_admin.list_users()[0]["id"]
    assert account_admin.set_active(uid, False) == (True, "已停用")
    assert not account_admin.list_users()[0]["is_active"]
    assert database_mgr.recognize_face(np.zeros(4, dtype=np.float32))[0] == "Empty DB"
    assert account_admin.set_active(uid, True) == (True, "已啟用")
    assert database_mgr.recognize_face(np.zeros(4, dtype=np.float32))[0] == "王小明"


def test_deleting_a_user_keeps_the_attendance_history(db, add_user, add_attendance, attendance_rows):
    add_user("王小明", "E001")
    add_user("李小華", "E002")
    add_attendance("王小明", "2026-09-01 08:00:00", "上班")
    uid = next(u["id"] for u in account_admin.list_users() if u["name"] == "王小明")
    assert account_admin.delete_user(uid) == (True, "已刪除")
    assert [u["name"] for u in account_admin.list_users()] == ["李小華"]
    assert attendance_rows("王小明") == [("2026-09-01 08:00:00", "上班")]


def test_account_operations_report_failures_without_raising(tmp_path, monkeypatch):
    monkeypatch.setattr(database_mgr, "DB_PATH", str(tmp_path))  # 資料夾不是資料庫檔
    assert account_admin.list_users() == []
    ok, message = account_admin.set_active(1, False)
    assert not ok and "更新失敗" in message
    ok, message = account_admin.delete_user(1)
    assert not ok and "刪除失敗" in message


def today():
    return datetime.datetime.now().strftime("%Y-%m-%d")


def test_today_summary_counts_distinct_people_and_lists_latest_first(db, add_attendance):
    add_attendance("王小明", f"{today()} 08:00:00", "上班")
    add_attendance("王小明", f"{today()} 18:00:00", "下班")
    add_attendance("李小華", f"{today()} 08:10:00", "遲到")
    add_attendance("王小明", "2020-01-01 08:00:00", "上班")
    count, logs = dashboard_stats.get_today_attendance_summary()
    assert count == 2
    assert [log[2] for log in logs] == ["下班", "遲到", "上班"]


def test_today_summary_respects_the_limit(db, add_attendance):
    for hour in range(8, 13):
        add_attendance("王小明", f"{today()} {hour:02d}:00:00", "上班")
    assert len(dashboard_stats.get_today_attendance_summary(limit=2)[1]) == 2


def test_today_summary_returns_none_when_the_database_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(database_mgr, "DB_PATH", str(tmp_path / "no_tables.db"))
    assert dashboard_stats.get_today_attendance_summary() is None


def test_anomaly_count_only_counts_jpg_files(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard_stats, "ANOMALY_DIR", str(tmp_path / "missing"))
    assert dashboard_stats.get_anomaly_count() == 0
    (tmp_path / "a.jpg").write_bytes(b"x")
    (tmp_path / "b.jpg").write_bytes(b"x")
    (tmp_path / "notes.txt").write_text("x", encoding="utf-8")
    monkeypatch.setattr(dashboard_stats, "ANOMALY_DIR", str(tmp_path))
    assert dashboard_stats.get_anomaly_count() == 2


def test_system_resources_report_cpu_and_memory():
    res = dashboard_stats.get_system_resources()
    assert set(res) == {"cpu_percent", "ram_used_gb", "ram_total_gb"}
    assert 0 <= res["cpu_percent"] <= 100 and 0 < res["ram_used_gb"] <= res["ram_total_gb"]

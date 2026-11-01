"""
管理者角色單元測試：管理者名單與比對（只比啟用中的管理者、門檻比打卡嚴）、
設定與取消管理者、最後一位管理者不能被取消、停用或刪除、角色變動會留下紀錄。
"""
import sqlite3

import numpy as np
import pytest

from admin import account_admin
from shared import database_mgr as dbm

ONES = np.ones(4, dtype=np.float32)


def user_id(db, name):
    con = sqlite3.connect(db)
    row = con.execute("SELECT id FROM Users WHERE name = ?", (name,)).fetchone()
    con.close()
    return row[0]


@pytest.fixture
def team(db, add_user):
    add_user("管理者甲", "A1", embedding=ONES, role="admin")
    add_user("組員乙", "E2", embedding=ONES * 0.9)
    add_user("停用的管理者", "A3", embedding=ONES * 0.8, role="admin", active=0)
    return db


# --- 名單與比對 ---

def test_only_active_admins_are_listed(team):
    assert [a["name"] for a in dbm.list_admins()] == ["管理者甲"]
    assert set(dbm.list_admins()[0]) == {"id", "name", "employee_id"}      # 不含人臉特徵


def test_recognize_admin_matches_admins_only(team):
    assert dbm.recognize_admin(ONES) == ("管理者甲", 0.0)
    # 用 0.85 倍去比：組員乙（0.9 倍）與停用的管理者（0.8 倍）比管理者甲更近，但它們都不能被當成管理者
    name, dist = dbm.recognize_admin(ONES * 0.85)
    assert name == "管理者甲" and dist == pytest.approx(0.3, abs=0.01)


def test_the_login_threshold_is_stricter_than_the_door_threshold(team):
    far = ONES + 0.45                              # 與管理者距離 0.9：打卡門檻 1.0 會過，登入門檻 0.8 不會過
    assert dbm.recognize_face(far)[0] != "Unknown"
    assert dbm.recognize_admin(far) == ("Unknown", pytest.approx(0.9, abs=0.01))


def test_no_admin_is_reported(db, add_user):
    add_user("組員", "E1")
    assert dbm.recognize_admin(ONES) == ("No Admin", 99.9) and dbm.list_admins() == []


# --- 設定與取消管理者 ---

def test_a_member_can_be_made_an_admin_and_back(team):
    uid = user_id(team, "組員乙")
    assert account_admin.set_role(uid, "admin", operator="管理者甲") == (True, "已設為管理者")
    assert {a["name"] for a in dbm.list_admins()} == {"管理者甲", "組員乙"}
    assert account_admin.set_role(uid, "employee") == (True, "已取消管理者")
    assert [a["name"] for a in dbm.list_admins()] == ["管理者甲"]


def test_a_role_change_is_recorded_with_who_did_it(team):
    account_admin.set_role(user_id(team, "組員乙"), "admin", operator="管理者甲")
    events = dbm.list_anomaly_events(event_type="admin_changed")
    assert len(events) == 1 and "管理者甲" in events[0]["detail"] and "組員乙" in events[0]["detail"]


def test_setting_the_same_role_again_changes_nothing(team):
    assert account_admin.set_role(user_id(team, "管理者甲"), "admin") == (True, "角色沒有變動")
    assert dbm.list_anomaly_events(event_type="admin_changed") == []


def test_a_disabled_account_cannot_become_an_admin(team):
    ok, message = account_admin.set_role(user_id(team, "停用的管理者"), "admin")
    assert not ok and "已停用" in message
    uid = user_id(team, "組員乙")
    account_admin.set_active(uid, False)
    ok, message = account_admin.set_role(uid, "admin")
    assert not ok and "已停用" in message


@pytest.mark.parametrize("role", ["root", "", None, "ADMIN"])
def test_unknown_roles_are_rejected(team, role):
    ok, _ = account_admin.set_role(user_id(team, "組員乙"), role)
    assert not ok


def test_an_unknown_user_is_reported(team):
    assert account_admin.set_role(9999, "admin") == (False, "找不到這位員工")


# --- 最後一位管理者 ---

def test_the_last_admin_cannot_be_demoted_disabled_or_deleted(team):
    uid = user_id(team, "管理者甲")
    for action in (lambda: account_admin.set_role(uid, "employee"),
                   lambda: account_admin.set_active(uid, False),
                   lambda: account_admin.delete_user(uid)):
        ok, message = action()
        assert not ok and "最後一位管理者" in message
    assert [a["name"] for a in dbm.list_admins()] == ["管理者甲"]


def test_with_a_second_admin_the_first_can_be_removed(team):
    account_admin.set_role(user_id(team, "組員乙"), "admin")
    assert account_admin.set_role(user_id(team, "管理者甲"), "employee")[0] is True
    assert [a["name"] for a in dbm.list_admins()] == ["組員乙"]
    ok, _ = account_admin.delete_user(user_id(team, "組員乙"))
    assert not ok                                   # 現在換組員乙成為最後一位


def test_ordinary_members_can_still_be_disabled_and_deleted(team):
    uid = user_id(team, "組員乙")
    assert account_admin.set_active(uid, False)[0] and account_admin.delete_user(uid)[0]

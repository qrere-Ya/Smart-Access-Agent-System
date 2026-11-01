"""
auth 單元測試：登入失敗鎖定、工作階段逾時、工作階段檔（代碼驗證、延長、過期不能復活、被竄改或刪除）。
時間全部用假的，不需要真的等待。
"""
import json

import pytest

from shared import auth


@pytest.fixture
def path(tmp_path):
    return str(tmp_path / "session.json")


# --- 失敗鎖定 ---

def test_five_failures_lock_the_login_for_a_minute():
    guard = auth.LoginGuard()
    assert [guard.record_failure(now=0) for _ in range(4)] == [False] * 4
    assert guard.record_failure(now=1) is True            # 第 5 次：鎖定
    assert 58 < guard.locked_for(now=2) <= 60
    assert guard.locked_for(now=61.5) == 0


def test_failures_during_the_lock_are_not_counted_and_do_not_extend_it():
    guard = auth.LoginGuard(max_failures=2, lock_seconds=10)
    guard.record_failure(now=0)
    assert guard.record_failure(now=0) is True
    assert guard.record_failure(now=5) is False
    assert guard.locked_for(now=9) == pytest.approx(1)


def test_a_success_clears_the_failure_count():
    guard = auth.LoginGuard(max_failures=3)
    guard.record_failure(now=0)
    guard.record_failure(now=0)
    guard.record_success()
    assert guard.record_failure(now=1) is False and guard.failures == 1


# --- 工作階段 ---

def test_a_session_expires_after_the_idle_timeout_and_touch_extends_it():
    session = auth.Session("王小明", timeout=900, now=1000)
    assert not session.expired(now=1899) and session.expired(now=1900)
    session.touch(now=1800)
    assert not session.expired(now=2699) and session.expired(now=2700)


def test_every_session_gets_a_different_unguessable_token():
    tokens = {auth.Session("a").token for _ in range(20)}
    assert len(tokens) == 20 and all(len(t) >= 40 for t in tokens)


# --- 工作階段檔 ---

def test_the_right_token_is_accepted_and_the_file_never_contains_it(path):
    session = auth.Session("王小明", now=1000)
    auth.save_session(session, path)
    assert auth.verify_token(session.token, path, now=1100) == "王小明"
    assert session.token not in open(path, encoding="utf-8").read()


@pytest.mark.parametrize("token", [None, "", "wrong-token"])
def test_missing_or_wrong_tokens_are_rejected(path, token):
    auth.save_session(auth.Session("王小明", now=1000), path)
    assert auth.verify_token(token, path, now=1001) is None


def test_an_expired_token_is_rejected_and_cannot_be_revived_by_touch(path):
    session = auth.Session("王小明", timeout=60, now=1000)
    auth.save_session(session, path)
    assert auth.verify_token(session.token, path, now=1061) is None
    assert auth.touch_token(session.token, path, now=1061) is False
    assert auth.verify_token(session.token, path, now=1062) is None


def test_touch_extends_the_shared_deadline(path):
    session = auth.Session("王小明", timeout=60, now=1000)
    auth.save_session(session, path)
    assert auth.touch_token(session.token, path, now=1050) is True
    assert auth.verify_token(session.token, path, now=1100) == "王小明"
    assert auth.verify_token(session.token, path, now=1111) is None


def test_clearing_the_session_logs_everyone_out(path):
    session = auth.Session("王小明")
    auth.save_session(session, path)
    auth.clear_session(path)
    assert auth.verify_token(session.token, path) is None
    auth.clear_session(path)                                # 沒有檔案時再清也不會出錯


@pytest.mark.parametrize("content", ["不是 json", "[]", json.dumps({"token_hash": "x"}), ""])
def test_a_damaged_session_file_means_not_logged_in(path, content):
    session = auth.Session("王小明")
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    assert auth.verify_token(session.token, path) is None


def test_a_new_login_replaces_the_previous_session(path):
    first, second = auth.Session("甲"), auth.Session("乙")
    auth.save_session(first, path)
    auth.save_session(second, path)
    assert auth.verify_token(first.token, path) is None and auth.verify_token(second.token, path) == "乙"


# --- 鎖定寫入檔案、讀寫更穩 ---

def test_the_lock_survives_a_restart_when_a_file_is_given(tmp_path):
    lock = str(tmp_path / "lock.json")
    guard = auth.LoginGuard(max_failures=2, lock_seconds=60, path=lock)
    guard.record_failure(now=1000)
    assert guard.record_failure(now=1000) is True
    reopened = auth.LoginGuard(max_failures=2, lock_seconds=60, path=lock)      # 關掉後台再開
    assert reopened.locked_for(now=1010) == pytest.approx(50)
    assert reopened.locked_for(now=1061) == 0


def test_the_failure_count_also_survives_a_restart(tmp_path):
    lock = str(tmp_path / "lock.json")
    auth.LoginGuard(max_failures=3, path=lock).record_failure(now=1)
    again = auth.LoginGuard(max_failures=3, path=lock)
    again.record_failure(now=2)
    assert again.record_failure(now=3) is True               # 重開前 1 次 + 重開後 2 次 = 3 次


def test_a_success_clears_the_saved_lock(tmp_path):
    lock = str(tmp_path / "lock.json")
    guard = auth.LoginGuard(max_failures=1, path=lock)
    guard.record_failure(now=1)
    guard.record_success()
    assert auth.LoginGuard(max_failures=1, path=lock).locked_for(now=2) == 0


@pytest.mark.parametrize("content", ["壞掉", "[]", '{"failures": "x", "locked_until": null}'])
def test_a_damaged_lock_file_means_no_lock(tmp_path, content):
    lock = tmp_path / "lock.json"
    lock.write_text(content, encoding="utf-8")
    assert auth.LoginGuard(path=str(lock)).locked_for(now=1) == 0


def test_a_temporarily_unreadable_session_file_does_not_log_the_admin_out(path, monkeypatch):
    session = auth.Session("王小明", now=1000)
    auth.save_session(session, path)
    real_open, calls = open, {"n": 0}

    def flaky_open(file, *args, **kwargs):
        if str(file) == path and calls["n"] < 2:           # 別的行程正在取代檔案：前兩次讀取被拒絕
            calls["n"] += 1
            raise PermissionError("檔案被占用")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr("builtins.open", flaky_open)
    assert auth.verify_token(session.token, path, now=1001) == "王小明"
    assert calls["n"] == 2


def test_touch_after_the_file_was_removed_returns_false_instead_of_crashing(path, monkeypatch):
    session = auth.Session("王小明", now=1000)
    auth.save_session(session, path)
    calls = {"n": 0}
    real_read = auth._read

    def read_then_vanish(p):
        calls["n"] += 1
        if calls["n"] == 2:                                 # 驗證通過後、再讀取前，檔案被登出刪掉
            auth.clear_session(p)
        return real_read(p)

    monkeypatch.setattr(auth, "_read", read_then_vanish)
    assert auth.touch_token(session.token, path, now=1001) is False
    assert not __import__("os").path.exists(path)           # 也沒有把已登出的工作階段重新寫回去


def test_two_writers_use_different_temporary_files(path):
    import os

    names = set()
    real_replace = os.replace

    def spy(src, dst):
        names.add(src)
        return real_replace(src, dst)

    os.replace = spy
    try:
        auth.save_session(auth.Session("甲"), path)
        auth.save_session(auth.Session("乙"), path)
    finally:
        os.replace = real_replace
    assert len(names) == 2
    assert [f for f in os.listdir(os.path.dirname(path)) if f.endswith(".tmp")] == []

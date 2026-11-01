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

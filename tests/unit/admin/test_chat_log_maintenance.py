"""
chat_log_maintenance 的單元測試：同一天只清一次、隔天再清、背景執行不卡住、
清理失敗不丟例外也不狂重試、真的刪掉 31 天前的紀錄。
"""
import datetime
import time

from admin import chat_log_maintenance as clm
from shared import chat_log

NOW = datetime.datetime(2026, 11, 16, 9, 0)


def wait_idle(m, seconds=3):
    end = time.time() + seconds
    while m._busy and time.time() < end:
        time.sleep(0.01)
    assert not m._busy


def test_it_purges_once_per_day_and_again_the_next_day():
    calls, clock = [], [NOW]
    m = clm.ChatLogMaintenance(clock=lambda: clock[0], purge=lambda now: calls.append(now) or 0)
    assert m.tick() is True
    wait_idle(m)
    assert m.tick() is False and len(calls) == 1
    clock[0] = NOW + datetime.timedelta(hours=14)             # 同一天的晚上
    assert m.tick() is False
    clock[0] = NOW + datetime.timedelta(days=1)
    assert m.tick() is True
    wait_idle(m)
    assert len(calls) == 2


def test_a_failing_purge_is_contained_and_not_retried_the_same_day(capsys):
    def boom(now):
        raise RuntimeError("internal detail")

    m = clm.ChatLogMaintenance(clock=lambda: NOW, purge=boom)
    assert m.tick() is True
    wait_idle(m)
    assert m.tick() is False
    out = capsys.readouterr().out
    assert "到期清理失敗" in out and "RuntimeError" in out and "internal detail" not in out


def test_only_one_purge_runs_at_a_time():
    m = clm.ChatLogMaintenance(clock=lambda: NOW, purge=lambda now: 0)
    m._busy = True
    assert m.tick() is False


def test_it_really_deletes_old_rows_in_the_database(db):
    old = chat_log.record(chat_log.SOURCE_BACK, "old", "a", now=NOW - datetime.timedelta(days=31))
    new = chat_log.record(chat_log.SOURCE_BACK, "new", "a", now=NOW - datetime.timedelta(days=2))
    m = clm.ChatLogMaintenance(clock=lambda: NOW)
    assert m.tick() is True
    wait_idle(m)
    assert chat_log.get_log(old) is None and chat_log.get_log(new) is not None

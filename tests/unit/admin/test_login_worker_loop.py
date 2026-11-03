"""
登入視窗讀視覺子行程輸出的那一段（_worker_scan_loop）的單元測試：用一個真的假子行程，驗證垃圾行會被忽略、
鎖定時通知子行程 reset、子行程崩潰與回報錯誤會變成錯誤事件、結束後子行程一定被收掉。
不需要真的鏡頭或模型；因為登入視窗模組會匯入 customtkinter，CI 的輕量環境會略過。
"""
import json
import queue
import subprocess
import sys
import threading

import numpy as np
import pytest

pytest.importorskip("customtkinter")
from admin import face_login, login_window, login_worker  # noqa: E402


def _good_line(status="no_face", message="請站到鏡頭前", **step):
    frame = np.full((360, 480, 3), 90, dtype=np.uint8)
    return login_worker.make_line(
        login_worker.step_to_message(frame, face_login.LoginStep(status=status, message=message, **step)))


CHILD = """
import sys, time
log = open(sys.argv[1], "a", encoding="utf-8")
for line in sys.argv[2:]:
    print(line, flush=True)
    time.sleep(0.05)
mode = sys.argv[-1] if False else None
for raw in sys.stdin:                      # 收到指令就記錄；stop 或輸入結束就離開
    log.write(raw.strip() + "\\n"); log.flush()
    if raw.strip() == "stop":
        break
"""


class Guard:
    def __init__(self, locked=0):
        self.locked = locked

    def locked_for(self):
        return self.locked


@pytest.fixture
def child(tmp_path):
    """回傳函式 make(lines, linger=True)：組出假子行程的指令，並回傳 (指令, 指令紀錄檔)。"""
    script = tmp_path / "child.py"
    script.write_text(CHILD, encoding="utf-8")
    log = tmp_path / "commands.log"
    log.write_text("", encoding="utf-8")

    def make(*lines):
        return [sys.executable, str(script), str(log), *lines], log

    return make


def _run(command, guard=None, stop_after=None, timeout=20):
    """在背景執行緒跑 _worker_scan_loop，收集事件直到子行程輸出結束（或收到 stop_after 個事件後送出 stop）。"""
    events, stop = queue.Queue(), threading.Event()
    thread = threading.Thread(target=login_window._worker_scan_loop, args=(command, guard or Guard(), stop, events))
    thread.start()
    got = []
    import time
    end = time.time() + timeout
    while time.time() < end:
        try:
            got.append(events.get(timeout=0.2))
        except queue.Empty:
            if not thread.is_alive():
                break
        if stop_after is not None and len(got) >= stop_after:
            stop.set()
            break
    thread.join(timeout=15)
    while not events.empty():
        got.append(events.get_nowait())
    return got, thread


def test_valid_steps_become_events_and_garbage_is_ignored(child):
    command, _ = child("Loading weights: 100%", "@@not json", _good_line(progress=0.2), '@@{"t": "hack"}',
                       _good_line("verifying", "驗證中…", progress=0.6, distance=0.81))
    events, thread = _run(command, stop_after=2)
    steps = [e for e in events if e[0] == "step"]
    assert [s[2].status for s in steps] == ["no_face", "verifying"]
    assert steps[1][2].distance == 0.81 and steps[0][1].shape == (360, 480, 3)
    assert not thread.is_alive()


def test_an_error_message_from_the_child_becomes_an_error_event(child):
    command, _ = child(login_worker.make_line({"t": "error", "message": "打不開鏡頭"}))
    events, _ = _run(command)
    assert ("error", "打不開鏡頭") in events


def test_a_child_that_dies_without_output_is_reported():
    events, thread = _run([sys.executable, "-c", "pass"])
    assert events and events[-1][0] == "error" and "意外結束" in events[-1][1] and not thread.is_alive()


def test_a_missing_program_is_reported_not_raised():
    events, thread = _run(["this-program-does-not-exist-12345"])
    assert events[-1][0] == "error" and "FileNotFoundError" in events[-1][1] and not thread.is_alive()


def test_while_locked_the_child_is_told_to_reset_and_steps_become_countdowns(child):
    command, log = child(_good_line(), _good_line())
    events, _ = _run(command, guard=Guard(locked=30), stop_after=2)
    assert [e[0] for e in events[:2]] == ["locked", "locked"] and events[0][2] == 30
    assert "reset" in log.read_text(encoding="utf-8").split()


def test_the_child_is_stopped_and_gone_when_the_loop_ends(child, monkeypatch):
    started = []
    real_popen = subprocess.Popen

    def spy(*args, **kwargs):
        proc = real_popen(*args, **kwargs)
        started.append(proc)
        return proc

    monkeypatch.setattr(login_window.subprocess, "Popen", spy)
    command, log = child(_good_line())
    _run(command, stop_after=1)
    assert started and started[0].poll() is not None                   # 子行程已結束，不會殘留占著鏡頭
    assert "stop" in log.read_text(encoding="utf-8").split()


def test_the_worker_command_is_fixed_and_runs_the_login_worker_module():
    assert login_window.WORKER_COMMAND[-2:] == ("-m", "admin.login_worker")
    assert json.dumps(login_window.WORKER_COMMAND[0]) and login_window.WORKER_COMMAND[0] == sys.executable

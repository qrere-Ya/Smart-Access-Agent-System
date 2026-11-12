"""前台子程式管理：隱藏視窗啟動、記錄檔、不重複啟動、停止、啟動失敗的說明、pythonw 的輸出轉向。"""
import os
import sys

from access import front_launcher as fl


class FakeProc:
    def __init__(self):
        self.pid, self.code, self.killed = 4321, None, False

    def poll(self):
        return self.code

    def wait(self, timeout=None):
        return 0

    def terminate(self):
        self.code = 0
        self.killed = True

    def kill(self):
        self.code = -9
        self.killed = True


def make(tmp_path, monkeypatch, popen):
    monkeypatch.setattr(fl.paths, "RUNTIME_DIR", str(tmp_path))
    return fl.ManagedProcess("access.terminal_app", "terminal_app", popen=popen, python="py.exe", cwd=str(tmp_path))


def test_start_runs_the_module_without_a_console_window_and_logs_to_a_file(tmp_path, monkeypatch):
    calls = []

    def popen(cmd, **kw):
        calls.append((cmd, kw))
        return FakeProc()

    proc = make(tmp_path, monkeypatch, popen)
    assert proc.running() is False
    assert proc.start() == (True, "已啟動") and proc.running() is True
    cmd, kw = calls[0]
    assert cmd == ["py.exe", "-u", "-m", "access.terminal_app"]
    assert kw["creationflags"] == getattr(fl.subprocess, "CREATE_NO_WINDOW", 0) and kw["env"]["PYTHONUTF8"] == "1"
    assert os.path.exists(fl.log_path("terminal_app"))


def test_starting_twice_does_not_launch_a_second_process(tmp_path, monkeypatch):
    calls = []
    proc = make(tmp_path, monkeypatch, lambda cmd, **kw: calls.append(1) or FakeProc())
    proc.start()
    assert proc.start() == (True, "已在執行") and len(calls) == 1


def test_a_process_that_exited_by_itself_counts_as_stopped_and_can_restart(tmp_path, monkeypatch):
    made = []
    proc = make(tmp_path, monkeypatch, lambda cmd, **kw: made.append(FakeProc()) or made[-1])
    proc.start()
    made[0].code = 0                                          # 使用者在辨識視窗按 q 結束
    assert proc.running() is False
    proc.start()
    assert len(made) == 2 and proc.running() is True


def test_stop_ends_the_process_and_is_harmless_when_nothing_runs(tmp_path, monkeypatch):
    made = []
    proc = make(tmp_path, monkeypatch, lambda cmd, **kw: made.append(FakeProc()) or made[-1])
    proc.stop()                                               # 沒啟動過
    proc.start()
    monkeypatch.setattr(fl.subprocess, "run", lambda *a, **k: made[0].terminate())   # 不真的呼叫 taskkill
    proc.stop()
    assert made[0].killed and proc.running() is False


def test_start_failure_is_reported_not_raised(tmp_path, monkeypatch):
    def boom(cmd, **kw):
        raise OSError("找不到檔案")

    proc = make(tmp_path, monkeypatch, boom)
    ok, message = proc.start()
    assert ok is False and "找不到檔案" in message and proc.running() is False


def test_old_oversized_logs_are_cleared_but_small_ones_are_kept(tmp_path, monkeypatch):
    proc = make(tmp_path, monkeypatch, lambda cmd, **kw: FakeProc())
    path = fl.log_path("terminal_app")
    os.makedirs(os.path.dirname(path))
    with open(path, "wb") as f:
        f.write(b"x" * (fl.LOG_LIMIT_BYTES + 10))
    proc.start()
    assert os.path.getsize(path) < 200                        # 只剩這次的啟動標記
    proc.start()
    with open(path, "ab") as f:
        f.write(b"keep")
    proc._proc = None
    proc.start()
    assert b"keep" in open(path, "rb").read()


def test_python_exe_swaps_pythonw_for_python(tmp_path, monkeypatch):
    fake = tmp_path / "pythonw.exe"
    fake.write_bytes(b"")
    monkeypatch.setattr(fl.paths, "VENV_PYTHON", str(fake))
    assert fl.python_exe() == str(tmp_path / "python.exe")


def test_windowless_python_gets_its_output_redirected_to_a_log(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(fl.paths, "RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    fl.redirect_output_if_windowless("front_dashboard")
    print("沒有視窗也能印")
    sys.stdout.flush()
    assert "沒有視窗也能印" in open(fl.log_path("front_dashboard"), encoding="utf-8").read()

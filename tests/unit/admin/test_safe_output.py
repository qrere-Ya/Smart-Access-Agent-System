"""safe_output：輸出端卡住時寫的人不會被擋住、行有進記錄檔、佇列有上限。"""
import threading
import time

from admin import safe_output
from shared import paths


class StuckStream:
    """write 會永遠卡住的輸出端（模擬管線寫滿沒人讀）。"""

    def __init__(self):
        self.gate = threading.Event()

    def write(self, text):
        self.gate.wait()

    def flush(self):
        pass


def test_writers_are_never_blocked_by_a_stuck_output_and_lines_reach_the_log(tmp_path):
    log = tmp_path / "x.log"
    stuck = StuckStream()
    stream = safe_output._SafeStream(stuck, str(log))
    started = time.time()
    for i in range(5000):                                  # 比佇列上限多：多的丟掉最舊的
        stream.write(f"行 {i}\n")
    assert time.time() - started < 2 and len(stream._pending) <= safe_output.MAX_PENDING_LINES
    stuck.gate.set()
    end = time.time() + 5
    while time.time() < end and (not log.exists() or "行 4999" not in log.read_text(encoding="utf-8")):
        time.sleep(0.05)
    assert "行 4999" in log.read_text(encoding="utf-8")


def test_install_replaces_the_standard_streams_and_returns_the_log_path(monkeypatch, tmp_path):
    import sys
    monkeypatch.setattr(paths, "RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(sys, "stdout", sys.stdout)
    monkeypatch.setattr(sys, "stderr", sys.stderr)
    path = safe_output.install("demo")
    assert path.endswith("demo.log") and isinstance(sys.stdout, safe_output._SafeStream)
    print("測試輸出")

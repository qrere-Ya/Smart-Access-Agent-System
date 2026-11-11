"""前台資源監控：nvidia-smi 輸出解析、網路速度計算、讀取失敗不丟例外。"""
from access import front_resources as fr


def test_parse_gpu_reads_the_first_line_and_clamps():
    assert fr.parse_gpu("37\n") == 37.0
    assert fr.parse_gpu("120, 1, 2") == 100.0
    assert fr.parse_gpu("") is None and fr.parse_gpu("N/A") is None


def test_rate_kbps_handles_first_reading_counter_reset_and_zero_time():
    assert fr.rate_kbps(None, 100, 1) == 0.0
    assert fr.rate_kbps(0, 2048, 2) == 1.0
    assert fr.rate_kbps(5000, 100, 1) == 0.0 and fr.rate_kbps(0, 100, 0) == 0.0


def test_monitor_returns_every_key_and_computes_speed_on_the_second_read(monkeypatch):
    monkeypatch.setattr(fr.shutil, "which", lambda _n: None)               # 沒有 NVIDIA 顯卡
    times = iter([10.0, 12.0])
    monitor = fr.ResourceMonitor(clock=lambda: next(times))
    first = monitor.read()
    second = monitor.read()
    assert set(first) == {"cpu", "ram", "gpu", "down_kbps", "up_kbps"} and first["gpu"] is None
    assert second["down_kbps"] is not None and second["down_kbps"] >= 0


def test_gpu_is_only_asked_every_few_seconds(monkeypatch):
    monkeypatch.setattr(fr.shutil, "which", lambda _n: "nvidia-smi")
    calls = []

    class Out:
        stdout = "50\n"

    monkeypatch.setattr(fr.subprocess, "run", lambda *a, **k: calls.append(1) or Out())
    clock = {"t": 0.0}
    monitor = fr.ResourceMonitor(clock=lambda: clock["t"])
    assert monitor.read()["gpu"] == 50.0
    clock["t"] = 1.0
    monitor.read()
    assert len(calls) == 1
    clock["t"] = fr.GPU_INTERVAL + 1
    monitor.read()
    assert len(calls) == 2


def test_gpu_tool_failure_gives_none(monkeypatch):
    monkeypatch.setattr(fr.shutil, "which", lambda _n: "nvidia-smi")

    def boom(*_a, **_k):
        raise OSError("x")

    monkeypatch.setattr(fr.subprocess, "run", boom)
    assert fr.ResourceMonitor().read()["gpu"] is None

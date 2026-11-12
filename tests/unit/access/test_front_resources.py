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


def test_gpu_query_runs_without_a_console_window(monkeypatch):
    """回歸：用 pythonw（沒有主控台）啟動時，nvidia-smi 若沒指定 CREATE_NO_WINDOW 會每 5 秒閃一個黑視窗。"""
    monkeypatch.setattr(fr.shutil, "which", lambda _n: "nvidia-smi")
    seen = {}

    class Out:
        stdout = "37"

    monkeypatch.setattr(fr.subprocess, "run", lambda *a, **k: seen.update(k) or Out())
    assert fr.ResourceMonitor().read()["gpu"] == 37.0
    assert seen["creationflags"] == getattr(fr.subprocess, "CREATE_NO_WINDOW", 0)


def test_sampler_returns_instantly_even_when_the_monitor_is_slow_and_survives_failures():
    import threading
    import time as _t

    from access import front_resources as fr

    release = threading.Event()
    calls = {"n": 0}

    class SlowMonitor:
        def read(self):
            calls["n"] += 1
            if calls["n"] == 1:
                release.wait(3)                                   # 第一次很慢（像 nvidia-smi）
                return {"cpu": 1.0, "ram": 2.0, "gpu": 3.0, "down_kbps": None, "up_kbps": None}
            if calls["n"] == 2:
                raise OSError("這一輪讀失敗")
            return {"cpu": 9.0, "ram": 2.0, "gpu": 3.0, "down_kbps": None, "up_kbps": None}

    sampler = fr.ResourceSampler(SlowMonitor(), interval=0.01)
    started = _t.perf_counter()
    first = sampler.read()
    assert _t.perf_counter() - started < 0.5 and first == fr.ResourceSampler.EMPTY    # 慢呼叫不卡住呼叫端
    release.set()
    deadline = _t.time() + 3
    while sampler.read()["cpu"] != 9.0 and _t.time() < deadline:                      # 失敗那輪沿用舊值、之後恢復
        _t.sleep(0.02)
    assert sampler.read()["cpu"] == 9.0
    sampler.stop()

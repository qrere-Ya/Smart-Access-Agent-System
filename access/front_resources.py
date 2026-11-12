"""
front_resources.py：前台電腦的資源監控（CPU、記憶體、GPU、網路上下行）——給前台儀表板的「資源狀態監控」。

主要功能：
  - ResourceMonitor.read()：回傳 {cpu, ram, gpu, down_kbps, up_kbps}；讀不到的項目是 None
  - 網路速度用兩次讀數的差除以時間；GPU 用 nvidia-smi，每 GPU_INTERVAL 秒才問一次
  - 任何讀取失敗都只回 None，不丟例外（儀表板不能因為監控壞掉而停止）

串接模組：
  - access.front_dashboard：畫面；後台有自己的 admin.dashboard_stats（前台安裝包不含 admin，所以這裡獨立一份）
"""
import shutil
import subprocess  # nosec B404
import threading
import time

GPU_INTERVAL = 5.0


def parse_gpu(text):
    """nvidia-smi 的 CSV 一行 "利用率, 已用MiB, 總MiB" → GPU 利用率百分比；格式不對回傳 None。"""
    try:
        first = text.strip().splitlines()[0]
        util = float(first.split(",")[0].strip())
    except (IndexError, ValueError):
        return None
    return max(0.0, min(100.0, util))


def rate_kbps(previous, current, seconds):
    """兩次累計位元組數 → KB/s；時間或數值不合理回傳 0。"""
    if previous is None or seconds <= 0 or current < previous:
        return 0.0
    return (current - previous) / seconds / 1024.0


class ResourceMonitor:
    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._net = None                    # (時間, 收, 送)
        self._gpu = (None, None)            # (時間, 值)
        self._has_gpu = shutil.which("nvidia-smi") is not None

    def _read_gpu(self, now):
        if not self._has_gpu:
            return None
        when, value = self._gpu
        if when is not None and now - when < GPU_INTERVAL:
            return value
        try:
            out = subprocess.run(  # noqa: S603  # nosec B603 B607
                ["nvidia-smi", "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"],  # noqa: S607  # nosec B607
                capture_output=True, text=True, timeout=4, check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout     # pythonw 下不閃黑視窗
            value = parse_gpu(out)
        except (OSError, subprocess.SubprocessError):
            value = None
        self._gpu = (now, value)
        return value

    def read(self):
        """讀一次；每項獨立，壞一項不影響其他。"""
        now = self._clock()
        result = {"cpu": None, "ram": None, "gpu": None, "down_kbps": None, "up_kbps": None}
        try:
            import psutil
            result["cpu"] = psutil.cpu_percent(interval=None)
            result["ram"] = psutil.virtual_memory().percent
            counters = psutil.net_io_counters()
            if self._net is not None:
                seconds = now - self._net[0]
                result["down_kbps"] = rate_kbps(self._net[1], counters.bytes_recv, seconds)
                result["up_kbps"] = rate_kbps(self._net[2], counters.bytes_sent, seconds)
            self._net = (now, counters.bytes_recv, counters.bytes_sent)
        except Exception:  # noqa: BLE001, S110  # nosec B110  沒裝 psutil 或讀取失敗，這幾項顯示「—」
            pass
        result["gpu"] = self._read_gpu(now)
        return result


class ResourceSampler:
    """在背景執行緒定時讀資源，read() 立刻回傳最新值；畫面不會被 nvidia-smi 之類的慢呼叫卡住（實測一次約 0.5 秒）。"""

    EMPTY = {"cpu": None, "ram": None, "gpu": None, "down_kbps": None, "up_kbps": None}

    def __init__(self, monitor=None, interval=1.0, sleep=time.sleep):
        self._monitor = monitor or ResourceMonitor()
        self._interval, self._sleep = interval, sleep
        self._latest = dict(self.EMPTY)
        self._thread = None
        self._stop = False

    def _loop(self):
        while not self._stop:
            try:
                self._latest = self._monitor.read()
            except Exception:  # noqa: BLE001, S110  # nosec B110  讀不到這一輪就沿用上一輪
                pass
            self._sleep(self._interval)

    def read(self):
        """回傳最新一次的資源值（第一次呼叫時才啟動背景執行緒，剛啟動還沒有值時是空值）。"""
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="front-resources", daemon=True)
            self._thread.start()
        return dict(self._latest)

    def stop(self):
        self._stop = True

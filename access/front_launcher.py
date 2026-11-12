"""
front_launcher.py：前台的子程式管理——隱藏黑色視窗地啟動連線代理與人臉辨識，輸出寫進記錄檔，可隨時停止。

主要功能：
  - ManagedProcess：以 python -m 模組名稱 啟動子行程（不跳出主控台視窗），stdout／stderr 寫到 runtime 下的記錄檔
    （超過 1 MB 的舊記錄啟動時清掉），running() 看是否還活著，stop() 連同它的子行程一起結束
  - python_exe()：找前台自己 venv 的 python.exe（pythonw 也換成 python，子行程要有標準輸出可寫）
  - redirect_output_if_windowless：用 pythonw 啟動時沒有標準輸出，print 會出錯，改寫進記錄檔

串接模組：
  - front_dashboard：儀表板啟動時開連線代理，「人臉辨識」開關開啟才啟動辨識；關閉儀表板時一起停止
  - 啟動前台.bat：用 pythonw 啟動儀表板，所以整個前台沒有黑色視窗
"""
import os
import subprocess  # noqa: S404  # nosec B404  只啟動自己專案的模組，參數固定
import sys

from shared import paths

LOG_LIMIT_BYTES = 1_000_000


def python_exe():
    """前台 venv 的 python.exe；找不到就用目前的直譯器（pythonw 換成 python）。"""
    exe = paths.VENV_PYTHON if os.path.exists(paths.VENV_PYTHON) else sys.executable
    folder, name = os.path.split(exe)
    return os.path.join(folder, "python.exe") if name.lower() == "pythonw.exe" else exe


def log_path(name):
    """runtime/logs/<name>.log"""
    return os.path.join(paths.RUNTIME_DIR, "logs", f"{name}.log")


def redirect_output_if_windowless(name):
    """pythonw 沒有標準輸出（sys.stdout 是 None），把輸出接到記錄檔，避免 print 出錯。"""
    if sys.stdout is not None and sys.stderr is not None:
        return
    path = log_path(name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    stream = open(path, "a", encoding="utf-8", buffering=1)  # noqa: SIM115  要活到程式結束
    sys.stdout = sys.stdout or stream
    sys.stderr = sys.stderr or stream


class ManagedProcess:
    """一個由前台管理的子行程：啟動、檢查、停止。"""

    def __init__(self, module, name, popen=subprocess.Popen, python=None, cwd=None):
        self.module, self.name, self._popen = module, name, popen
        self._python = python or python_exe()
        self._cwd = cwd or paths.PROJECT_ROOT
        self._proc = None
        self.last_exit = None                  # 子行程自己結束時的結束代碼；從沒結束過或重新啟動後是 None

    def running(self):
        if self._proc is None:
            return False
        code = self._proc.poll()
        if code is None:
            return True
        self.last_exit = code                  # 自己結束了（不是被 stop 收掉）：記下結束代碼，畫面可以提示
        return False

    def start(self):
        """啟動；已在執行就不重複啟動。回傳 (成功與否, 說明)。"""
        if self.running():
            return True, "已在執行"
        self.last_exit = None
        path = log_path(self.name)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            if os.path.exists(path) and os.path.getsize(path) > LOG_LIMIT_BYTES:
                os.remove(path)
            with open(path, "ab") as log:
                log.write(f"\n===== 啟動 {self.module} =====\n".encode("utf-8"))
                env = dict(os.environ, PYTHONUTF8="1", PYTHONUNBUFFERED="1")
                flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                self._proc = self._popen([self._python, "-u", "-m", self.module], cwd=self._cwd, env=env,
                                         stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                         creationflags=flags)
            return True, "已啟動"
        except OSError as e:
            self._proc = None
            return False, f"啟動失敗：{e}"

    def stop(self):
        """結束子行程與它開的子行程（Windows 用 taskkill /T）；沒在執行就什麼都不做。"""
        proc, self._proc = self._proc, None
        self.last_exit = None
        if proc is None or proc.poll() is not None:
            return
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True,  # noqa: S603,S607  # nosec B603 B607
                           check=False, timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

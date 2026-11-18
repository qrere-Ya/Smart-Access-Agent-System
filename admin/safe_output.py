"""
safe_output.py：讓背景服務的 print 永遠不會因為「接輸出的那一端不讀」而卡住。

問題：後台把連線服務的輸出接成管線（pipe）讀進主視窗；後台介面關掉重開、或讀的人停了，管線寫滿後，
服務裡任何一個 print（載入模型的進度、請求記錄）都會卡死，知識庫問答就一直等到逾時（對話模型不印東西所以看不出來）。

作法：install() 把 sys.stdout／sys.stderr 換成只放進有上限的佇列，由背景執行緒慢慢寫到原本的輸出與記錄檔；
佇列滿了就丟掉最舊的行，寫的人（問答工作執行緒）永遠不會被擋住。記錄檔在 runtime/logs/<名稱>.log（UTF-8）。

串接模組：admin.link_server（main 一開始呼叫 install("link_server")）、shared.paths（RUNTIME_DIR）。
"""
import collections
import os
import sys
import threading

from shared import paths

MAX_PENDING_LINES = 2000
MAX_LOG_BYTES = 2 * 1024 * 1024


class _SafeStream:
    def __init__(self, real, log_path):
        self._real, self._log_path = real, log_path
        self._pending = collections.deque(maxlen=MAX_PENDING_LINES)
        self._wake = threading.Event()
        self._buffer = ""
        self.encoding = getattr(real, "encoding", "utf-8")
        threading.Thread(target=self._drain, daemon=True, name="safe-output").start()

    def write(self, text):
        self._buffer += str(text)
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            self._pending.append(line)
        self._wake.set()
        return len(text)

    def flush(self):
        pass

    def isatty(self):
        return False

    def reconfigure(self, **_kwargs):
        pass

    def _drain(self):
        log = self._open_log()
        while True:
            self._wake.wait()
            self._wake.clear()
            while self._pending:
                line = self._pending.popleft()
                if log is not None:
                    try:
                        log.write(line + "\n")
                        log.flush()
                    except OSError:
                        log = None
                try:
                    self._real.write(line + "\n")
                    self._real.flush()
                except (OSError, ValueError, UnicodeError):
                    pass

    def _open_log(self):
        try:
            os.makedirs(os.path.dirname(self._log_path), exist_ok=True)
            if os.path.exists(self._log_path) and os.path.getsize(self._log_path) > MAX_LOG_BYTES:
                os.replace(self._log_path, self._log_path + ".old")
            return open(self._log_path, "a", encoding="utf-8", errors="replace")
        except OSError:
            return None


def install(name):
    """把這個行程的 stdout／stderr 換成不會擋住寫入者的版本；回傳記錄檔路徑。"""
    log_path = os.path.join(paths.RUNTIME_DIR, "logs", f"{name}.log")
    sys.stdout = _SafeStream(sys.stdout, log_path)
    sys.stderr = _SafeStream(sys.stderr, log_path)
    return log_path

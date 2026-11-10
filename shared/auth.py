"""
auth.py：管理後台的登入保護與工作階段（純邏輯，不含畫面與鏡頭，可單獨測試）。

主要功能：
  - LoginGuard：連續失敗達上限就鎖定一段時間，鎖定中不接受登入
  - Session：登入成功後的工作階段（隨機代碼 + 最後活動時間），閒置超過期限即失效
  - 工作階段檔（runtime/session.json）：只存代碼的雜湊與最後活動時間，讓另開行程的助理視窗也能驗證代碼，
    並且在後台或助理任何一邊有操作時一起延長期限；登出或過期就刪除
  時間一律從參數傳入（預設用目前時間），測試不必真的等待。

串接模組：
  - login_window：登入成功後建立 Session 並寫入工作階段檔
  - backend_main：每秒檢查是否過期、有操作就延長、登出時清除
  - service_manager、rag.chat_window（後台與助理共用，所以放在 shared）：開助理視窗時帶代碼，視窗啟動與執行中都會驗證
"""
import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Optional

from shared import paths

SESSION_TIMEOUT_SECONDS = 15 * 60
MAX_FAILURES = 5
LOCK_SECONDS = 60
FAILURE_INTERVAL_SECONDS = 5   # 兩次計入的失敗至少相隔這麼久；同一次站在鏡頭前的連續不過只算一次
TOKEN_ENV = "SMART_ACCESS_SESSION_TOKEN"  # noqa: S105  環境變數名稱，不是密碼


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class LoginGuard:
    """登入失敗計數：連續 MAX_FAILURES 次失敗就鎖 LOCK_SECONDS 秒；成功後歸零。"""

    def __init__(self, max_failures: int = MAX_FAILURES, lock_seconds: float = LOCK_SECONDS,
                 path: Optional[str] = None, min_interval: float = 0.0):
        """path 不為 None 時，失敗次數與鎖定時間存在檔案裡，關掉後台再開不能重置。
        min_interval：距離上一次計入的失敗不到這麼久的失敗不再累計（避免一次嘗試被連算好幾次）。"""
        self.max_failures, self.lock_seconds = max_failures, lock_seconds
        self.min_interval = min_interval
        self._last_failure = float("-inf")
        self.path = path
        self.failures = 0
        self._locked_until = 0.0
        data = _read(path) if path else None
        if data:
            try:
                self.failures = int(data.get("failures", 0))
                self._locked_until = float(data.get("locked_until", 0.0))
            except (TypeError, ValueError):
                self.failures, self._locked_until = 0, 0.0

    def _save(self):
        if self.path:
            try:
                _write({"failures": self.failures, "locked_until": self._locked_until}, self.path)
            except OSError:
                pass

    def locked_for(self, now: Optional[float] = None) -> float:
        """還要鎖多久（秒）；沒有鎖定回傳 0。"""
        now = time.time() if now is None else now
        return max(0.0, self._locked_until - now)

    def counts_now(self, now: Optional[float] = None) -> bool:
        """這個時間點的失敗會不會被計入（鎖定中或距離上次計入太近都不會）。"""
        now = time.time() if now is None else now
        return self.locked_for(now) <= 0 and now - self._last_failure >= self.min_interval

    def record_failure(self, now: Optional[float] = None) -> bool:
        """記一次失敗；這次失敗導致鎖定時回傳 True。鎖定期間的失敗不再累計。"""
        now = time.time() if now is None else now
        if not self.counts_now(now):
            return False
        self._last_failure = now
        self.failures += 1
        locked = self.failures >= self.max_failures
        if locked:
            self._locked_until = now + self.lock_seconds
            self.failures = 0
        self._save()
        return locked

    def record_success(self):
        self.failures = 0
        self._locked_until = 0.0
        self._last_failure = float("-inf")
        self._save()


class Session:
    def __init__(self, admin_name: str, timeout: float = SESSION_TIMEOUT_SECONDS, now: Optional[float] = None):
        self.admin_name = admin_name
        self.timeout = timeout
        self.token = secrets.token_urlsafe(32)
        self.last_activity = time.time() if now is None else now

    def touch(self, now: Optional[float] = None):
        self.last_activity = time.time() if now is None else now

    def expired(self, now: Optional[float] = None) -> bool:
        now = time.time() if now is None else now
        return now - self.last_activity >= self.timeout


# --- 工作階段檔：讓另開的助理視窗也能驗證 ---

def _path(path: Optional[str]) -> str:
    return path or paths.SESSION_PATH


def _write(data: dict, path: str):
    """寫入暫存檔再原子取代；暫存檔名每次不同（後台與助理可能同時寫），Windows 上目標正被讀取時重試幾次。"""
    tmp = f"{path}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        for attempt in range(5):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.02)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _read(path: str) -> Optional[dict]:
    """讀檔；檔案不存在或內容壞掉回傳 None。Windows 上別的行程正在取代檔案時會暫時讀不到，所以重試幾次，
    不能因為一次讀取失敗就當成已登出。"""
    for attempt in range(5):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else None
        except FileNotFoundError:
            return None
        except ValueError:
            return None
        except OSError:
            if attempt == 4:
                return None
            time.sleep(0.02)
    return None


def save_session(session: Session, path: Optional[str] = None):
    _write({"token_hash": _hash(session.token), "admin": session.admin_name,
            "last_activity": session.last_activity, "timeout": session.timeout}, _path(path))


def clear_session(path: Optional[str] = None):
    try:
        os.remove(_path(path))
    except OSError:
        pass


def verify_token(token: Optional[str], path: Optional[str] = None, now: Optional[float] = None) -> Optional[str]:
    """代碼正確且沒有過期就回傳管理者姓名，否則回傳 None。"""
    if not token:
        return None
    data = _read(_path(path))
    if not data:
        return None
    try:
        now = time.time() if now is None else now
        if not hmac.compare_digest(str(data.get("token_hash", "")), _hash(token)):
            return None
        if now - float(data["last_activity"]) >= float(data["timeout"]):
            return None
        return str(data["admin"])
    except (KeyError, TypeError, ValueError):
        return None


def touch_token(token: Optional[str], path: Optional[str] = None, now: Optional[float] = None) -> bool:
    """代碼有效才延長期限（過期的不能靠 touch 復活）；回傳是否延長成功。"""
    p = _path(path)
    if verify_token(token, p, now) is None:
        return False
    data = _read(p)
    if not data:                        # 驗證之後檔案剛好被登出刪掉
        return False
    data["last_activity"] = time.time() if now is None else now
    try:
        _write(data, p)
    except OSError:
        return False
    return True

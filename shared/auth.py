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
TOKEN_ENV = "SMART_ACCESS_SESSION_TOKEN"  # noqa: S105  環境變數名稱，不是密碼


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class LoginGuard:
    """登入失敗計數：連續 MAX_FAILURES 次失敗就鎖 LOCK_SECONDS 秒；成功後歸零。"""

    def __init__(self, max_failures: int = MAX_FAILURES, lock_seconds: float = LOCK_SECONDS):
        self.max_failures, self.lock_seconds = max_failures, lock_seconds
        self.failures = 0
        self._locked_until = 0.0

    def locked_for(self, now: Optional[float] = None) -> float:
        """還要鎖多久（秒）；沒有鎖定回傳 0。"""
        now = time.time() if now is None else now
        return max(0.0, self._locked_until - now)

    def record_failure(self, now: Optional[float] = None) -> bool:
        """記一次失敗；這次失敗導致鎖定時回傳 True。鎖定期間的失敗不再累計。"""
        now = time.time() if now is None else now
        if self.locked_for(now) > 0:
            return False
        self.failures += 1
        if self.failures >= self.max_failures:
            self._locked_until = now + self.lock_seconds
            self.failures = 0
            return True
        return False

    def record_success(self):
        self.failures = 0
        self._locked_until = 0.0


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
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _read(path: str) -> Optional[dict]:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
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
    data["last_activity"] = time.time() if now is None else now
    _write(data, p)
    return True

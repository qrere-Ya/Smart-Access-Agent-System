"""
remote_access.py：遠端測試入口的存取控制與流量控制（純邏輯，不含網頁，可單獨測試）。

主要功能：
  - KeyStore：每位組員一組存取金鑰，檔案只存雜湊；可新增、撤銷，撤銷後立刻失效（每次驗證都重讀檔案）
  - Gate：全站同時處理的題數上限與排隊上限，排太久就放棄，避免筆電被同時多題壓垮
  - QuestionLog：把「誰、何時、問什麼、用了哪些工具、結果」一行一筆記下來（不記回答內容），可關閉
  - GatedAssistant：包住每位使用者自己的 Assistant，問答前先排隊取得名額，結束後釋放並寫紀錄

串接模組：
  - rag.remote_server：網頁服務，建立金鑰庫、流量閘門與每位使用者的 GatedAssistant
  - rag.chat_session：ChatController 直接包 GatedAssistant，排隊發生在背景執行緒，不卡住網頁請求
"""
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
import datetime
from typing import List, Optional

from rag import answer
from shared import paths

KEY_LABEL_MAX = 30
MAX_CONCURRENT = 1            # 同時處理的題數；向量與重排模型是共用的，沒驗證過多執行緒同時用，預設 1
MAX_WAITING = 5               # 排隊人數上限，超過直接回覆忙碌
QUEUE_WAIT_SECONDS = 90.0     # 排隊最久等這麼久
LOG_QUESTION_CHARS = 1000

BUSY_TEXT = "目前使用的人太多，請稍後再問一次。"


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


class KeyStore:
    """組員存取金鑰庫：檔案內容是 {名稱: 金鑰雜湊}，金鑰本身只在新增當下顯示一次。"""

    def __init__(self, path: Optional[str] = None):
        self.path = path or paths.REMOTE_KEYS_PATH

    def _load(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return {}
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}

    def _save(self, data: dict):
        tmp = f"{self.path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def labels(self) -> List[str]:
        return sorted(self._load())

    def add(self, label: str) -> str:
        """新增一位組員並回傳他的金鑰（只有這一次看得到）；名稱空白、過長、含控制字元或重複都拒絕。"""
        label = (label or "").strip()
        if not label or len(label) > KEY_LABEL_MAX or any(ord(c) < 32 for c in label):
            raise ValueError(f"名稱必須是 1 到 {KEY_LABEL_MAX} 個字，且不能含控制字元")
        data = self._load()
        if label in data:
            raise ValueError(f"「{label}」已經有金鑰了，要換請先撤銷")
        key = secrets.token_urlsafe(24)
        data[label] = _hash(key)
        self._save(data)
        return key

    def revoke(self, label: str) -> bool:
        data = self._load()
        if data.pop((label or "").strip(), None) is None:
            return False
        self._save(data)
        return True

    def verify(self, key: Optional[str]) -> Optional[str]:
        """金鑰正確回傳組員名稱，否則 None；每個名稱都比對一次，不因為比中的順序洩漏時間差。"""
        if not key or not isinstance(key, str):
            return None
        digest, found = _hash(key), None
        for label, stored in self._load().items():
            if hmac.compare_digest(stored, digest):
                found = label
        return found


class Busy(Exception):
    """排隊已滿或等太久。"""


class Gate:
    """流量閘門：同時最多 max_concurrent 題在處理，其餘依先後排隊，隊伍滿了或等太久就丟出 Busy。"""

    def __init__(self, max_concurrent: int = MAX_CONCURRENT, max_waiting: int = MAX_WAITING,
                 wait_seconds: float = QUEUE_WAIT_SECONDS):
        self.max_concurrent, self.max_waiting, self.wait_seconds = max_concurrent, max_waiting, wait_seconds
        self._cond = threading.Condition()
        self._running = 0
        self._waiting: list = []

    def acquire(self, ticket):
        """排隊等名額；ticket 是呼叫端自備的物件，用來查自己前面還有幾人。"""
        with self._cond:
            if self._running >= self.max_concurrent and len(self._waiting) >= self.max_waiting:
                raise Busy("queue full")
            self._waiting.append(ticket)
            deadline = time.monotonic() + self.wait_seconds
            while not (self._running < self.max_concurrent and self._waiting[0] is ticket):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._waiting.remove(ticket)
                    self._cond.notify_all()
                    raise Busy("queue timeout")
                self._cond.wait(remaining)
            self._waiting.pop(0)
            self._running += 1

    def release(self):
        with self._cond:
            self._running = max(0, self._running - 1)
            self._cond.notify_all()

    def ahead(self, ticket) -> Optional[int]:
        """這張 ticket 前面還有幾人（含正在處理的）；不在排隊中回傳 None。"""
        with self._cond:
            for i, t in enumerate(self._waiting):
                if t is ticket:
                    return self._running + i
        return None


class QuestionLog:
    """提問紀錄（JSON Lines）：只記誰、時間、問題、用了哪些工具、結果，不記回答內容。"""

    def __init__(self, path: Optional[str] = None, enabled: bool = True):
        self.path, self.enabled = path or paths.REMOTE_LOG_PATH, enabled
        self._lock = threading.Lock()

    def record(self, user: str, question: str, tools: list, outcome: str):
        if not self.enabled:
            return
        line = json.dumps({"time": datetime.datetime.now().isoformat(timespec="seconds"), "user": user,
                           "question": question[:LOG_QUESTION_CHARS], "tools": list(tools), "outcome": outcome},
                          ensure_ascii=False)
        try:
            with self._lock, open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError as e:
            print(f"[remote] 提問紀錄寫入失敗（略過）：{type(e).__name__}")


class GatedAssistant:
    """包住一位使用者自己的 Assistant：問答前先排隊取得名額，結束後釋放名額並寫提問紀錄。"""

    def __init__(self, assistant, gate: Gate, log: QuestionLog, user: str):
        self.assistant, self.gate, self.log, self.user = assistant, gate, log, user
        self._ticket = None
        self._override: Optional[answer.Trace] = None

    @property
    def last(self):
        return self._override or self.assistant.last

    def ahead(self) -> Optional[int]:
        return self.gate.ahead(self._ticket) if self._ticket is not None else None

    def reset(self):
        self.assistant.reset()

    def ask(self, question: str):
        self._override = None
        ticket = self._ticket = object()
        try:
            self.gate.acquire(ticket)
        except Busy:
            self._ticket = None
            self._override = answer.Trace(outcome="busy")
            self.log.record(self.user, question, [], "busy")
            yield BUSY_TEXT
            return
        self._ticket = None
        outcome = "error"
        try:
            yield from self.assistant.ask(question)
            outcome = self.assistant.last.outcome
        finally:
            self.gate.release()
            self.log.record(self.user, question, self.assistant.last.tools, outcome)

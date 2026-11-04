"""
remote_access.py：遠端測試入口的存取控制與流量控制（純邏輯，不含網頁，可單獨測試）。

主要功能：
  - KeyStore：每位組員一組存取金鑰，檔案只存雜湊；預設 7 天到期；可新增、撤銷，每次驗證都重讀檔案，
    已登入的工作階段每次請求也會用 fingerprint 重新確認金鑰仍有效
  - FailureBudget：全站登入失敗預算，不看來源位址；EventLog：登入成功、失敗、鎖定的安全事件紀錄
  - Gate：全站同時處理的題數上限與排隊上限，排太久就放棄，避免筆電被同時多題壓垮
  - QuestionLog：把「誰、何時、問什麼、用了哪些工具、結果」一行一筆記下來；with_answers 開啟時再存系統回答、
    路由決定、檢索到的文件段落與分數、查詢結果表格，用來檢查 RAG 檢索與回答是否正確（含個資）；可關閉
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
import unicodedata
import datetime
from typing import List, Optional

from rag import answer
from shared import paths

KEY_LABEL_MAX = 30
MAX_CONCURRENT = 1            # 同時處理的題數；向量與重排模型是共用的，沒驗證過多執行緒同時用，預設 1
MAX_WAITING = 5               # 排隊人數上限，超過直接回覆忙碌
QUEUE_WAIT_SECONDS = 90.0     # 排隊最久等這麼久
LOG_QUESTION_CHARS = 1000
ANSWER_LOG_CHARS = 8000       # 提問紀錄裡每題回答最多存多少字
DATA_LOG_CHARS = 3000         # 查詢結果表格最多存多少字
SNIPPET_CHARS = 400           # 每個檢索到的文件段落存前幾個字
HITS_LOGGED = 10              # 每題最多存幾個檢索結果
DEFAULT_KEY_DAYS = 7          # 金鑰預設有效天數；外洩時的曝險期有上限
BUDGET_MAX_FAILURES = 30      # 全站登入失敗上限：這段時間內不分來源，合計失敗這麼多次就暫停所有登入
BUDGET_WINDOW_SECONDS = 600.0
BUDGET_BLOCK_SECONDS = 120.0
EVENT_FIELD_CHARS = 100

BUSY_TEXT = "目前使用的人太多，請稍後再問一次。"


def _plain(text: str) -> str:
    """把換行、控制字元與 Unicode 行分隔字元（U+2028 等）換成空白，避免用別的工具看紀錄時出現偽造的行。"""
    return "".join(" " if unicodedata.category(c) in ("Zl", "Zp", "Cc", "Cf") else c for c in str(text))


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


class KeyStore:
    """組員存取金鑰庫：檔案內容是 {名稱: {hash, expires}}，金鑰本身只在新增當下顯示一次；金鑰預設 7 天後失效。"""

    def __init__(self, path: Optional[str] = None):
        self.path = path or paths.REMOTE_KEYS_PATH

    def _load(self) -> dict:
        """讀出 {名稱: {"hash": 雜湊, "expires": 到期時間或 None}}；舊格式（名稱對雜湊字串）視為不會到期。"""
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        out = {}
        for label, entry in data.items():
            if isinstance(entry, str):
                out[str(label)] = {"hash": entry, "expires": None}
            elif isinstance(entry, dict) and isinstance(entry.get("hash"), str):
                expires = entry.get("expires")
                out[str(label)] = {"hash": entry["hash"],
                                   "expires": float(expires) if isinstance(expires, (int, float)) else None}
        return out

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

    def describe(self) -> List[tuple]:
        """[(名稱, 到期時間或 None)]，依名稱排序，給 --list-keys 顯示。"""
        return [(label, entry["expires"]) for label, entry in sorted(self._load().items())]

    def add(self, label: str, days: Optional[float] = DEFAULT_KEY_DAYS, now: Optional[float] = None) -> str:
        """新增一位組員並回傳他的金鑰（只有這一次看得到）；days 為有效天數（None 或 0 表示不會到期）。
        名稱空白、過長、含控制字元或重複都拒絕。"""
        label = (label or "").strip()
        if not label or len(label) > KEY_LABEL_MAX or any(ord(c) < 32 for c in label):
            raise ValueError(f"名稱必須是 1 到 {KEY_LABEL_MAX} 個字，且不能含控制字元")
        if days is not None and days < 0:
            raise ValueError("有效天數不能是負數")
        data = self._load()
        if label in data:
            raise ValueError(f"「{label}」已經有金鑰了，要換請先撤銷")
        key = secrets.token_urlsafe(24)
        now = time.time() if now is None else now
        data[label] = {"hash": _hash(key), "expires": now + days * 86400 if days else None}
        self._save(data)
        return key

    def revoke(self, label: str) -> bool:
        data = self._load()
        if data.pop((label or "").strip(), None) is None:
            return False
        self._save(data)
        return True

    def fingerprint(self, label: str, now: Optional[float] = None) -> Optional[str]:
        """名稱目前有效金鑰的雜湊（沒有這個名稱或已到期回傳 None）。工作階段登入時記下它，之後每次請求
        重新比對：金鑰被撤銷、換新或到期，已登入的工作階段也跟著失效。"""
        entry = self._load().get(label)
        now = time.time() if now is None else now
        if entry is None or (entry["expires"] is not None and now >= entry["expires"]):
            return None
        return entry["hash"]

    def verify(self, key: Optional[str], now: Optional[float] = None) -> Optional[str]:
        """金鑰正確且沒有到期回傳組員名稱，否則 None；每個名稱都比對一次，不因為比中的順序洩漏時間差。"""
        if not key or not isinstance(key, str):
            return None
        now = time.time() if now is None else now
        digest, found = _hash(key), None
        for label, entry in self._load().items():
            matched = hmac.compare_digest(entry["hash"], digest)
            if matched and (entry["expires"] is None or now < entry["expires"]):
                found = label
        return found


class FailureBudget:
    """全站登入失敗預算：不看來源位址（來源標頭可能被偽造），一段時間內失敗太多就暫停所有登入一小段時間。"""

    def __init__(self, max_failures: int = BUDGET_MAX_FAILURES, window: float = BUDGET_WINDOW_SECONDS,
                 block_seconds: float = BUDGET_BLOCK_SECONDS):
        self.max_failures, self.window, self.block_seconds = max_failures, window, block_seconds
        self._times: List[float] = []
        self._blocked_until = 0.0

    def blocked_for(self, now: float) -> float:
        return max(0.0, self._blocked_until - now)

    def record_failure(self, now: float):
        self._times = [t for t in self._times if now - t < self.window] + [now]
        if len(self._times) >= self.max_failures:
            self._blocked_until = now + self.block_seconds
            self._times = []


class EventLog:
    """安全事件紀錄（JSON Lines）：登入成功、失敗、鎖定。字串欄位會去掉控制字元並截短，避免偽造紀錄行。"""

    def __init__(self, path: Optional[str] = None):
        self.path = path or paths.REMOTE_EVENTS_PATH
        self._lock = threading.Lock()

    @staticmethod
    def _clean(value):
        if not isinstance(value, str):
            return value
        return "".join(c for c in value if c.isprintable())[:EVENT_FIELD_CHARS]

    def record(self, kind: str, **fields):
        row = {"time": datetime.datetime.now().isoformat(timespec="seconds"), "event": kind,
               **{k: self._clean(v) for k, v in fields.items()}}
        try:
            with self._lock, open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError as e:
            print(f"[remote] 安全事件紀錄寫入失敗（略過）：{type(e).__name__}")


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


def _keep_text(text: str, limit: int) -> str:
    """回答與資料表這類多行文字：保留換行（JSON 會跳脫），只把 Unicode 行分隔字元換成空白，並截短。"""
    return "".join(" " if c in (" ", " ", "\x85") else c for c in str(text or ""))[:limit]


def _score(value) -> Optional[float]:
    return None if value is None else round(float(value), 4)


def trace_details(trace, answer_text: str) -> dict:
    """一題的檢查資料：系統回答，加上能判斷「檢索對不對」的內部資訊
    （路由決定、日期區間、比對到的人、檢索到的文件段落與分數、查出來的資料表）。"""
    plan, rng = getattr(trace, "plan", None), getattr(trace, "date_range", None)
    return {
        "answer": _keep_text(answer_text, ANSWER_LOG_CHARS),
        "plan": None if plan is None else {
            "docs": plan.docs, "data": plan.data, "person": plan.person, "status": plan.status,
            "event_type": plan.event_type, "source": plan.source,
            "steps": [{"data": s.data, "person": s.person, "status": s.status, "event_type": s.event_type}
                      for s in plan.steps]},
        "date_range": None if rng is None else {"start": rng.start, "end": rng.end, "phrase": rng.phrase},
        "person": _keep_text(trace.person, EVENT_FIELD_CHARS),
        "hits": [{"source": _keep_text(h.chunk.get("source", ""), 200), "label": _keep_text(h.label, 200),
                  "page": h.chunk.get("page"), "score": _score(h.score), "dense": _score(h.dense),
                  "rerank": _score(h.rerank), "text": _keep_text(h.chunk.get("body", ""), SNIPPET_CHARS)}
                 for h in trace.hits[:HITS_LOGGED]],
        "data_text": _keep_text(trace.data_text, DATA_LOG_CHARS),
        "note": _keep_text(trace.note, EVENT_FIELD_CHARS * 3),
    }


class QuestionLog:
    """提問紀錄（JSON Lines）：誰、時間、問題、用了哪些工具、結果。with_answers 開啟時，再加上系統回答與
    檢索資訊（見 trace_details），用來檢查 RAG 檢索與回答是否正確；這些內容含個資，要當個資處理。"""

    def __init__(self, path: Optional[str] = None, enabled: bool = True, with_answers: bool = False):
        self.path, self.enabled, self.with_answers = path or paths.REMOTE_LOG_PATH, enabled, with_answers
        self._lock = threading.Lock()

    def record(self, user: str, question: str, tools: list, outcome: str, details: Optional[dict] = None):
        if not self.enabled:
            return
        row = {"time": datetime.datetime.now().isoformat(timespec="seconds"), "user": _plain(user),
               "question": _plain(question[:LOG_QUESTION_CHARS]), "tools": list(tools), "outcome": outcome}
        if self.with_answers and details:
            row.update(details)
        line = json.dumps(row, ensure_ascii=False)
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
        outcome, pieces = "error", []
        try:
            for piece in self.assistant.ask(question):
                pieces.append(piece)
                yield piece
            outcome = self.assistant.last.outcome
        finally:
            self.gate.release()
            last = self.assistant.last
            details = trace_details(last, "".join(pieces)) if self.log.with_answers else None
            self.log.record(self.user, question, last.tools, outcome, details)

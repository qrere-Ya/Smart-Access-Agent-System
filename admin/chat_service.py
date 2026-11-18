"""
chat_service.py：後台替前台提供的知識庫問答服務（只准文件檢索；見 docs/adr/0001-front-knowledge-chat.md）。

主要功能：
  - submit：前台送來的問題先驗證、檢查頻率與排隊上限，放進佇列並回傳工作編號（secrets.token_hex(16)）；
    不等答案（答案要約 20 秒以上，由前台之後用編號來取）
  - 單一工作執行緒、一次只做 1 題：第一次有題目才載入向量模型、重排模型與文件索引（狀態回報「準備中」），
    之後常駐；閒置 15 分鐘釋放顯存，下一題再載入
  - 只准知識庫：呼叫 Assistant.ask_documents（不經意圖判斷、沒有出缺勤工具、不讀資料庫、不保留對話記憶）
  - 單題逾時 180 秒（含載入模型）；結果只在記憶體保留 10 分鐘；回答去掉控制字元並限長，來源只回文件名稱
  - 任何失敗（模型載入失敗、語言模型連不上、逾時）只回固定錯誤碼與固定說明文字，不含例外內容、路徑或金鑰設定，
    例外型別只印在後台的日誌
  - 每一題（成功或失敗）都記進對話紀錄（來源 front_knowledge，由 Assistant.on_finished 寫入）
  - 文件管理：後台畫面按「重新掃描」會寫請求檔（shared.docs_control），工作執行緒每秒看一次，已載入知識庫就重新掃描
    data/docs 並把結果寫成一行說明；還沒載入就略過（第一次有前台提問時載入就會掃描最新的文件）
  - 頻率：兩次送出至少相隔 3 秒（太快回 slow_down）；排隊加處理中最多 3 題（再多回 busy）

串接模組：
  - rag.bootstrap.create_runtime：載入知識庫零件（預設工廠；測試用假的）；rag.answer.Assistant.ask_documents：問答
  - rag.chat_logging、shared.chat_log：對話紀錄
  - admin.link_server：POST /v1/chat、GET /v1/chat/<編號> 呼叫這裡；開關（chat_enabled）由 link_server 檢查
"""
import queue
import secrets
import threading
import time
import unicodedata

from rag import chat_logging
from shared import chat_log, docs_control, link_protocol as lp

MIN_INTERVAL_SECONDS = 3.0            # 兩次送出的最短間隔
MAX_OPEN_JOBS = 3                     # 排隊加處理中的上限（1 題處理中、2 題等待）
JOB_TIMEOUT_SECONDS = 180.0           # 單題逾時（含第一題載入模型）
RESULT_KEEP_SECONDS = 600.0           # 結果保留多久
IDLE_RELEASE_SECONDS = 900.0          # 閒置多久釋放模型
WORKER_POLL_SECONDS = 1.0
LLM_READ_TIMEOUT_SECONDS = 90.0       # 語言模型每次讀取的逾時（原本 600 秒，卡住時工作執行緒會跟著卡很久）
RELEASE_WAIT_SECONDS = 30.0           # 上一題逾時被丟棄的模型，最多等多久它才釋放完（沒釋放完就不載入第二份）

QUEUED, PREPARING, RUNNING, DONE, FAILED = "queued", "preparing", "running", "done", "failed"

# 固定的失敗說明（前台直接顯示；不含例外內容）
ERROR_TEXT = {
    "unavailable": "知識庫目前無法使用，請稍後再試。",
    "llm_unavailable": "語言模型目前連不上，請稍後再試。",
    "timeout": "這一題花太久，已經取消，請稍後再試。",
    "local_only": "後台是全本地模式，目前沒有可用的語言模型。",
}


class ChatError(Exception):
    """送出時的錯誤；code 是回給前台的固定代碼。"""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


class Busy(ChatError):
    def __init__(self):
        super().__init__("busy")


class TooFast(ChatError):
    def __init__(self):
        super().__init__("slow_down")


def clean_answer(text):
    """回答去掉控制字元並限制長度（前台只當純文字顯示）。"""
    text = "".join(c for c in str(text or "")
                   if c in "\n\t" or unicodedata.category(c) not in ("Cc", "Cf", "Zl", "Zp")).strip()
    return text if len(text) <= lp.MAX_CHAT_ANSWER else text[:lp.MAX_CHAT_ANSWER].rstrip() + "…"


def cap_llm_timeouts(assistant, seconds):
    """語言模型每次讀取的逾時改短（含雲端與本機備援），卡住時幾十秒內就丟例外，而不是等 10 分鐘。"""
    for llm in (getattr(assistant, "llm", None), getattr(assistant, "router_llm", None)):
        for backend in (llm, getattr(llm, "primary", None), getattr(llm, "fallback", None)):
            timeout = getattr(backend, "timeout", None)
            if isinstance(timeout, (int, float)) and timeout > seconds:
                backend.timeout = seconds


class _Job:
    def __init__(self, job_id, question, created):
        self.id, self.question, self.created = job_id, question, created
        self.status, self.answer, self.sources, self.error = QUEUED, "", [], ""
        self.finished = None


class ChatService:
    ASK_METHOD = "ask_documents"          # 呼叫助理的哪個方法（一般對話服務換成 ask）
    ERROR_TEXT = ERROR_TEXT

    def __init__(self, runtime_factory=None, clock=time.monotonic, min_interval=MIN_INTERVAL_SECONDS,
                 timeout=JOB_TIMEOUT_SECONDS, keep=RESULT_KEEP_SECONDS, idle_release=IDLE_RELEASE_SECONDS):
        self._factory = runtime_factory
        self._clock, self._min_interval, self._timeout = clock, min_interval, timeout
        self._keep, self._idle_release = keep, idle_release
        self._jobs, self._queue = {}, queue.Queue()
        self._lock = threading.Lock()
        self._last_submit = None
        self._runtime, self._last_used = None, clock()
        self._releasing = None            # 逾時丟棄的舊模型正在釋放的執行緒
        self._docs_seen = time.time()     # 只處理服務啟動之後才送來的重新掃描請求
        self._worker = None
        self._stop = threading.Event()

    # --- 對外 ---

    def submit(self, question):
        """放進佇列並回傳工作編號；太快丟 TooFast、排隊已滿丟 Busy。問題已由 lp.validate_chat_question 驗證過。"""
        with self._lock:
            now = self._clock()
            self._purge(now)
            if self._last_submit is not None and now - self._last_submit < self._min_interval:
                raise TooFast()
            if sum(1 for j in self._jobs.values() if j.status in (QUEUED, PREPARING, RUNNING)) >= MAX_OPEN_JOBS:
                raise Busy()
            self._last_submit = now
            job = _Job(secrets.token_hex(16), question, now)
            self._jobs[job.id] = job
            self._queue.put(job)
            self._ensure_worker()
            return job.id

    def get(self, job_id):
        """工作狀態（給 GET /v1/chat/<編號>）；編號不存在或已過期回傳 None。"""
        with self._lock:
            self._purge(self._clock())
            job = self._jobs.get(job_id)
            if job is None:
                return None
            out = {"status": job.status}
            if job.status == DONE:
                out.update(answer=job.answer, sources=list(job.sources))
            elif job.status == FAILED:
                out.update(error=job.error, message=self.ERROR_TEXT.get(job.error, self.ERROR_TEXT["unavailable"]))
            return out

    def close(self):
        """停止工作執行緒並釋放模型。"""
        self._stop.set()
        worker = self._worker
        if worker is not None:
            worker.join(5)
        self._release()

    # --- 內部 ---

    def _purge(self, now):
        for job_id in [i for i, j in self._jobs.items() if j.finished is not None and now - j.finished > self._keep]:
            del self._jobs[job_id]

    def _ensure_worker(self):
        if self._worker is None or not self._worker.is_alive():
            self._worker = threading.Thread(target=self._loop, daemon=True, name="chat-service")
            self._worker.start()

    def _release(self):
        runtime, self._runtime = self._runtime, None
        if runtime is not None:
            try:
                runtime.close()
            except Exception as e:  # noqa: BLE001
                print(f"[chat_service] 釋放模型時出錯（略過）：{type(e).__name__}")

    def _loop(self):
        while not self._stop.is_set():
            try:
                job = self._queue.get(timeout=WORKER_POLL_SECONDS)
            except queue.Empty:
                self._handle_docs_request()
                if self._runtime is not None and self._clock() - self._last_used >= self._idle_release:
                    print("[chat_service] 知識庫閒置太久，釋放模型")
                    self._release()
                continue
            try:
                self._run(job)
            except Exception as e:  # noqa: BLE001  任何意外都不能讓工作執行緒結束或洩漏內容
                print(f"[chat_service] 處理失敗：{type(e).__name__}")
                self._finish(job, FAILED, error="unavailable")
            self._last_used = self._clock()

    def _handle_docs_request(self):
        """後台畫面要求重新掃描文件：已載入知識庫就掃描並寫下結果，還沒載入就說明下一次提問時會自動掃描。"""
        stamp = docs_control.pending(self._docs_seen)
        if stamp is None:
            return
        self._docs_seen = stamp
        runtime = self._runtime
        try:
            if runtime is None:
                text = "知識庫還沒載入，下一次前台提問時會自動掃描最新的文件"
            else:
                from rag import bootstrap, chat_worker

                text = chat_worker.describe_report(bootstrap.refresh_documents(runtime))
        except Exception as e:  # noqa: BLE001  掃描失敗只說明型別，不外洩路徑或例外內容
            print(f"[chat_service] 重新掃描文件失敗：{type(e).__name__}")
            text = "⚠️ 文件重新掃描失敗，請確認文件檔案沒有被其他程式開啟，稍後再試"
        try:
            docs_control.write_status(text)
        except OSError as e:
            print(f"[chat_service] 寫入掃描結果失敗：{type(e).__name__}")
        self._last_used = self._clock()

    def _finish(self, job, status, answer="", sources=(), error=""):
        with self._lock:
            job.answer, job.sources, job.error = answer, list(sources), error
            job.status, job.finished = status, self._clock()

    def _run(self, job):
        started = self._clock()
        if self._runtime is None:
            with self._lock:
                job.status = PREPARING
            try:
                self._wait_for_release()
                self._runtime = self._load()
            except Exception as e:  # noqa: BLE001
                print(f"[chat_service] 載入知識庫失敗：{type(e).__name__}")
                self._finish(job, FAILED, error=self._explain(e))
                self._log_failure(job)
                return
        with self._lock:
            job.status = RUNNING
        assistant = self._runtime.assistant
        box = {"pieces": [], "error": None}
        finished = threading.Event()

        def consume():
            try:
                for piece in getattr(assistant, self.ASK_METHOD)(job.question):
                    box["pieces"].append(piece)
            except Exception as e:  # noqa: BLE001
                box["error"] = e
            finally:
                finished.set()

        helper = threading.Thread(target=consume, daemon=True, name="chat-ask")
        helper.start()
        if not finished.wait(max(0.0, self._timeout - (self._clock() - started))):
            # 逾時：語言模型或檢索卡住了。工作執行緒不能跟著卡住，所以放棄這一題、丟掉這份模型
            # （等卡住的呼叫結束才釋放），下一題重新載入；前台只看到固定的逾時說明
            print("[chat_service] 單題逾時，放棄這一題並重新載入知識庫")
            self._finish(job, FAILED, error="timeout")
            self._abandon_runtime(helper)
            return
        if box["error"] is not None:
            print(f"[chat_service] 問答失敗：{type(box['error']).__name__}")
            self._finish(job, FAILED, error="llm_unavailable")
            return
        pieces = box["pieces"]
        outcome = assistant.last.outcome
        if outcome == "error":
            self._finish(job, FAILED, error="llm_unavailable")
            return
        self._finish(job, DONE, clean_answer("".join(pieces)), getattr(assistant.last, "sources", []))

    def _abandon_runtime(self, helper):
        """逾時後丟掉目前的模型：等卡住的那一題結束再釋放（不在它還在用的時候釋放）。"""
        runtime, self._runtime = self._runtime, None
        if runtime is None:
            return

        def release_later():
            helper.join()
            try:
                runtime.close()
            except Exception as e:  # noqa: BLE001
                print(f"[chat_service] 釋放逾時的模型時出錯（略過）：{type(e).__name__}")

        self._releasing = threading.Thread(target=release_later, daemon=True, name="chat-release")
        self._releasing.start()

    def _wait_for_release(self):
        """上一份逾時的模型還沒釋放完就先等一下；等不到就這一題失敗，不要同時載入兩份模型把顯存塞爆。"""
        releasing = self._releasing
        if releasing is not None and releasing.is_alive():
            releasing.join(RELEASE_WAIT_SECONDS)
            if releasing.is_alive():
                raise RuntimeError("上一份模型還在釋放")
        self._releasing = None

    def _load(self):
        factory = self._factory
        if factory is None:
            from rag import bootstrap

            factory = bootstrap.create_runtime
        runtime = factory(progress=lambda text: None)
        assistant = getattr(runtime, "assistant", None)
        if assistant is not None:
            cap_llm_timeouts(assistant, LLM_READ_TIMEOUT_SECONDS)
            assistant.on_finished = chat_logging.recorder(chat_log.SOURCE_FRONT)
            assistant.show_internal_details = False     # 前台使用者看不到模型名稱、位址與設定提示
            assistant.show_evidence_paths = False
        return runtime

    @staticmethod
    def _explain(error):
        """載入失敗的原因分類（固定代碼）；全本地模式下找不到語言模型另外標出。"""
        from shared import network_policy

        if network_policy.local_only() and isinstance(error, RuntimeError):
            return "local_only"
        return "unavailable"

    @staticmethod
    def _log_failure(job):
        """載入失敗的題目也記進對話紀錄（回答欄寫失敗原因的固定說明）。"""
        chat_log.try_record(chat_log.SOURCE_FRONT, job.question, "（知識庫無法使用，沒有產生回答）", [])


TALK_ERROR_TEXT = {
    "unavailable": "對話目前無法使用，請稍後再試。",
    "llm_unavailable": "語言模型目前連不上，請稍後再試。",
    "timeout": "這一題花太久，已經取消，請稍後再試。",
    "local_only": "後台是全本地模式，目前沒有可用的語言模型。",
}


class TalkService(ChatService):
    """前台 Smart_Chat 一般對話服務：和知識庫問答同一套佇列、頻率、逾時與固定錯誤說明，
    差別是只載入語言模型（rag.talk.Talker，沒有文件、資料庫、員工名單），對話紀錄來源是 front_talk，
    也不處理文件重新掃描。"""

    ASK_METHOD = "ask"
    ERROR_TEXT = TALK_ERROR_TEXT

    def _handle_docs_request(self):
        """一般對話沒有文件，不處理重新掃描請求。"""

    def _load(self):
        factory = self._factory
        if factory is None:
            from rag import bootstrap

            factory = bootstrap.create_talk_runtime
        runtime = factory(progress=lambda text: None)
        assistant = getattr(runtime, "assistant", None)
        if assistant is not None:
            cap_llm_timeouts(assistant, LLM_READ_TIMEOUT_SECONDS)
            assistant.on_finished = chat_logging.recorder(chat_log.SOURCE_TALK)
        return runtime

    @staticmethod
    def _log_failure(job):
        chat_log.try_record(chat_log.SOURCE_TALK, job.question, "（語言模型無法使用，沒有產生回答）", [])

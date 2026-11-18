"""
front_chat.py：前台知識庫問答的對話控制（只在記憶體，沒有任何畫面元件；見 docs/adr/0001-front-knowledge-chat.md）。

主要功能：
  - FrontChat.submit：前台使用者用實體鍵盤輸入的問題，在背景執行緒送到後台（前台永遠主動連後台），
    之後每秒去取結果，畫面只讀 view()，不會卡住畫面執行緒；處理中不能重複送出
  - view：目前狀態 → idle、asking、preparing（後台第一題在載入模型）、answer、busy（後台忙碌或送太快）、
    offline（後台連不上）、closed（後台沒開放或舊版後台沒有這個介面）、error（含逾時），
    answer 與來源只當純文字、來源只有文件名稱
  - availability：不用送出就能判斷能不能問——後台沒開放（同步得到的 chat_enabled）、後台離線、還是可以問
  - clear：清掉問題與回答並放棄還在進行的那一題（閒置自動清除、離開面板時呼叫）
  - **不保存任何對話**：不寫本機資料庫、記錄檔或任何檔案；日誌只有狀態與例外型別，沒有問題與回答
  - 前台沒有聯網能力的限制不變：只連後台內網位址（BackClient 已限制內網、HTTPS、釘住憑證指紋）

串接模組：
  - access.front_agent：BackClient（chat_submit、chat_result）與同步到本機的 back_chat_enabled、back_target
  - access.front_store：讀同步來的開關、後台位址、連線狀態（只讀）
  - access.front_dashboard：知識庫問答面板讀 view()、呼叫 submit／clear
  - shared.link_protocol：問題長度上限與工作編號格式
"""
import threading
import time
import unicodedata

from access import front_agent
from access.front_agent import BackAuthError, BackOffline, BackRejected, ChatBusy, ChatClosed
from shared import link_protocol as lp
from shared import role_config

POLL_SECONDS = 1.0
GIVE_UP_SECONDS = 200.0                # 後台的單題逾時是 180 秒，前台多等一點就放棄

IDLE, ASKING, PREPARING, ANSWER, BUSY, OFFLINE, CLOSED, ERROR = (
    "idle", "asking", "preparing", "answer", "busy", "offline", "closed", "error")

# 固定的畫面文字（後台的失敗說明也只收後面這幾種代碼對應的固定文字，不顯示後台傳來的任意內容）
MESSAGES = {
    ASKING: "查詢中…",
    PREPARING: "知識庫準備中，第一次提問會久一點…",
    BUSY: "目前有人在使用，請稍後再試",
    OFFLINE: "後台未連線，無法使用知識庫問答",
    CLOSED: "知識庫問答目前未開放",
    ERROR: "這一題沒有成功，請稍後再試",
}
BACK_MESSAGES = {      # 後台回的失敗代碼 → 前台自己的固定文字
    "timeout": "這一題花太久，已經取消，請稍後再試",
    "llm_unavailable": "語言模型目前連不上，請稍後再試",
    "local_only": "後台是全本地模式，目前沒有可用的語言模型",
    "unavailable": "知識庫目前無法使用，請稍後再試",
}
MAX_SOURCES = 8
MAX_SOURCE_CHARS = 60
MAX_ANSWER_CHARS = lp.MAX_CHAT_ANSWER


def make_client_factory(store):
    """問答用的後台連線：位址與憑證指紋取自代理同步到本機的 back_target，通行碼取自環境變數；取不到回傳 None。"""
    def make():
        target = store.get_meta(front_agent.BACK_TARGET_KEY)
        if not isinstance(target, dict):
            return None
        try:
            token = role_config.require_token()
            return front_agent.BackClient(target["host"], target["port"], token, target.get("fingerprint"))
        except (role_config.ConfigError, KeyError, TypeError, ValueError):
            return None

    return make


def _plain(text, limit):
    """後台傳來的文字只當純文字：去掉控制字元、限制長度。"""
    text = "".join(c for c in str(text)
                   if c in "\n\t" or unicodedata.category(c) not in ("Cc", "Cf", "Zl", "Zp")).strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


class FrontChat:
    """前台知識庫問答（Smart_RAG）的對話控制；FrontTalk 是同一套機制的一般對話（Smart_Chat）。"""
    KEY = front_agent.BACK_CHAT_KEY
    PATH = lp.CHAT_PATH
    MESSAGES = MESSAGES
    BACK_MESSAGES = BACK_MESSAGES
    LOG = "front_chat"

    def __init__(self, store, make_client=None, clock=time.time, poll_seconds=POLL_SECONDS, give_up=GIVE_UP_SECONDS,
                 sleep=time.sleep):
        self.store = store
        self._make_client = make_client or make_client_factory(store)
        self._clock, self._poll, self._give_up, self._sleep = clock, poll_seconds, give_up, sleep
        self._lock = threading.Lock()
        self._generation = 0
        self._state, self._answer, self._sources, self._message = IDLE, "", [], ""

    # --- 能不能問 ---

    def enabled(self):
        """後台有沒有開放（同步得到的布林；沒同步過或舊後台沒有這個欄位一律當作未開放）。"""
        return self.store.get_meta(self.KEY, False) is True

    def availability(self):
        """CLOSED（未開放）、OFFLINE（後台離線）或 IDLE（可以問）。"""
        if not self.enabled():
            return CLOSED
        if not self.store.link_online():
            return OFFLINE
        return IDLE

    # --- 送出與取結果 ---

    def submit(self, question):
        """開始問一題；空白、太長、處理中、不能問時回傳 False（不送出任何東西）。"""
        question = (question or "").strip()
        if not question or len(question) > lp.MAX_CHAT_QUESTION:
            return False
        with self._lock:
            if self._state in (ASKING, PREPARING) or self.availability() != IDLE:
                return False
            self._generation += 1
            generation = self._generation
            self._state, self._answer, self._sources, self._message = ASKING, "", [], self.MESSAGES[ASKING]
        threading.Thread(target=self._run, args=(question, generation), daemon=True, name=self.LOG).start()
        return True

    def _set(self, generation, state, answer="", sources=(), message=""):
        with self._lock:
            if generation != self._generation:       # 已經清掉或換了一題：這一題的結果丟掉
                return False
            self._state, self._answer, self._sources, self._message = state, answer, list(sources), message
            return True

    def _run(self, question, generation):
        try:
            client = self._make_client()
            if client is None:
                self._set(generation, OFFLINE, message=self.MESSAGES[OFFLINE])
                return
            job_id = client.chat_submit(question, self.PATH).get("job_id")
            deadline = self._clock() + self._give_up
            while self._clock() < deadline:
                body = client.chat_result(job_id, self.PATH)
                status = body.get("status")
                if status == "done":
                    answer = _plain(body.get("answer", ""), MAX_ANSWER_CHARS)
                    sources = [_plain(x, MAX_SOURCE_CHARS) for x in (body.get("sources") or [])[:MAX_SOURCES]
                               if isinstance(x, str)]
                    self._set(generation, ANSWER, answer, [x for x in sources if x])
                    return
                if status == "failed":
                    message = self.BACK_MESSAGES.get(body.get("error"), self.MESSAGES[ERROR])
                    self._set(generation, ERROR, message=message)
                    return
                if status == "preparing":
                    self._set(generation, PREPARING, message=self.MESSAGES[PREPARING])
                else:
                    self._set(generation, ASKING, message=self.MESSAGES[ASKING])
                self._sleep(self._poll)
                with self._lock:
                    if generation != self._generation:
                        return
            self._set(generation, ERROR, message=self.BACK_MESSAGES["timeout"])
        except ChatClosed:
            self._set(generation, CLOSED, message=self.MESSAGES[CLOSED])
        except ChatBusy:
            self._set(generation, BUSY, message=self.MESSAGES[BUSY])
        except (BackOffline, BackAuthError):
            self._set(generation, OFFLINE, message=self.MESSAGES[OFFLINE])
        except (BackRejected, ValueError, AttributeError, TypeError) as e:
            print(f"[{self.LOG}] 這一題沒有成功：{type(e).__name__}")        # 日誌只有例外型別，沒有問題與回答
            self._set(generation, ERROR, message=self.MESSAGES[ERROR])
        except Exception as e:  # noqa: BLE001  背景執行緒不能讓例外往外丟
            print(f"[{self.LOG}] 意外的錯誤：{type(e).__name__}")
            self._set(generation, ERROR, message=self.MESSAGES[ERROR])

    # --- 畫面用 ---

    def view(self):
        """畫面要的資料：{state, message, answer, sources, can_ask}；不能問時 state 是 CLOSED 或 OFFLINE。"""
        with self._lock:
            state, answer, sources, message = self._state, self._answer, list(self._sources), self._message
        if state in (IDLE, ANSWER, ERROR, BUSY, OFFLINE, CLOSED):
            gate = self.availability()
            if gate != IDLE:                          # 後台沒開放或離線：優先顯示，並隱藏輸入
                return {"state": gate, "message": self.MESSAGES[gate], "answer": "", "sources": [], "can_ask": False}
        return {"state": state, "message": message, "answer": answer, "sources": sources,
                "can_ask": state not in (ASKING, PREPARING, CLOSED, OFFLINE)}

    def clear(self):
        """清掉問題與回答，放棄還在進行的那一題（閒置自動清除、離開面板時呼叫）。"""
        with self._lock:
            self._generation += 1
            self._state, self._answer, self._sources, self._message = IDLE, "", [], ""


TALK_MESSAGES = {**MESSAGES, ASKING: "思考中…", PREPARING: "準備中，第一次提問會久一點…",
                 OFFLINE: "後台未連線，無法使用 Smart_Chat", CLOSED: "Smart_Chat 目前未開放"}
TALK_BACK_MESSAGES = {**BACK_MESSAGES, "unavailable": "對話目前無法使用，請稍後再試"}


class FrontTalk(FrontChat):
    """前台 Smart_Chat 一般對話：和知識庫問答同一套行為（主動連後台、輪詢、不保存對話），只是走 /v1/talk、
    看 talk_enabled 開關；回答沒有來源文件。"""
    KEY = front_agent.BACK_TALK_KEY
    PATH = lp.TALK_PATH
    MESSAGES = TALK_MESSAGES
    BACK_MESSAGES = TALK_BACK_MESSAGES
    LOG = "front_talk"

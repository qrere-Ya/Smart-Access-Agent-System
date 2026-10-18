"""
chat_session.py：聊天視窗背後的「對話控制」（不含任何畫面元件，可單獨測試）。

主要功能：
  - 送出問題後在背景執行緒呼叫 Assistant.ask，畫面執行緒不會被卡住
  - 回答的每一段文字放進事件佇列，畫面定時取出（start、chunk、done、error 四種事件）
  - 同一時間只處理一個問題；空白問題、忙碌中的重複送出都會被擋下
  - 「新對話」會清掉助理的對話記憶；輸入與回答的長度有上限，避免貼入超大內容

串接模組：
  - answer.Assistant：實際的問答
  - rag.chat_window：畫面，每隔一小段時間呼叫 poll() 更新氣泡
"""
import queue
import re
import threading
from dataclasses import dataclass
from typing import List, Optional

MAX_INPUT_CHARS = 1000


@dataclass
class Event:
    kind: str                 # start、chunk、done、error
    text: str = ""
    outcome: Optional[str] = None


def plain_text(text: str) -> str:
    """把模型常寫出來的 Markdown 標記（粗體、標題、反引號、星號項目）整理成純文字，聊天視窗不會渲染它們。"""
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"(?m)^(\s*)[*]\s+", r"\1• ", text)
    text = re.sub(r"(?m)^\s*#{1,6}\s*", "", text)
    return text.replace("**", "").replace("`", "")


class ChatController:
    def __init__(self, assistant):
        self.assistant = assistant
        self._events: "queue.Queue[Event]" = queue.Queue()
        self._lock = threading.Lock()
        self._busy = False
        self._worker: Optional[threading.Thread] = None

    @property
    def busy(self) -> bool:
        return self._busy

    def submit(self, question: str) -> bool:
        """送出問題；成功開始處理回傳 True，空白或忙碌中回傳 False。"""
        question = (question or "").strip()[:MAX_INPUT_CHARS]
        if not question:
            return False
        with self._lock:
            if self._busy:
                return False
            self._busy = True
        self._events.put(Event("start", question))
        self._worker = threading.Thread(target=self._run, args=(question,), daemon=True)
        self._worker.start()
        return True

    def _run(self, question: str):
        try:
            for piece in self.assistant.ask(question):
                self._events.put(Event("chunk", piece))
            final = Event("done", outcome=self.assistant.last.outcome)
        except Exception as e:  # ask() 本身不該丟例外；萬一發生也要讓畫面解除忙碌狀態
            final = Event("error", f"{type(e).__name__}：{e}")
        self._busy = False  # 先解除忙碌再送出結束事件，畫面收到後馬上再送出問題才不會被擋下
        self._events.put(final)

    def poll(self) -> List[Event]:
        """取出目前累積的所有事件（沒有就回傳空清單），由畫面定時呼叫。"""
        events = []
        while True:
            try:
                events.append(self._events.get_nowait())
            except queue.Empty:
                return events

    def wait(self, timeout: float = 10.0):
        """等目前的問題處理完（測試用）。"""
        worker = self._worker
        if worker is not None:
            worker.join(timeout)

    def new_chat(self) -> bool:
        """開始新對話：清掉助理記憶。處理中不能清（會讓回答亂掉），回傳是否成功。"""
        with self._lock:
            if self._busy:
                return False
        self.assistant.reset()
        self.poll()
        return True

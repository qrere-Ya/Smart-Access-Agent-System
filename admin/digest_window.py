"""
digest_window.py：出勤日報與科技早報的背景產生，以及出勤日報頁（CustomTkinter，嵌在後台主視窗裡）。

主要功能：
  - DigestRunner：在背景執行緒產生報告（查資料庫、必要時呼叫雲端模型、抓新聞），完成後在主執行緒回呼；
    tick() 由後台每分鐘呼叫一次，依 pending 判斷該不該產生；失敗 10 分鐘後才會再試，不會狂重試
    digest_runner（傍晚出勤日報，預設 18:00）與 morning_runner（早上科技早報，預設 08:30）是兩個設定好的 DigestRunner
  - ReportView：只看出勤日報的頁面，聊天室樣式、只有助理的訊息、沒有輸入框
    （AI 摘要標示「數字與人名已核對」，沒有摘要就只顯示明細）；視窗可放大、訊息寬度跟著變，滾輪在訊息上也能捲動；
    文字裡的網址可以點（只有 https 公開網域，用預設瀏覽器開）；排程自動產生新出勤日報時會彈出
  - 科技早報後台不顯示：產生後由前台代理同步、在前台的螢幕上顯示（後台只留排程時間與立即產生）
  - 產生時間與立即產生不在這裡，在主選單的「出勤日報與科技早報」模組（schedule_window）

串接模組：
  - daily_digest／morning_brief：產生與排程判斷；shared.digest_store：存檔與設定；ui_theme：外觀
  - backend_main：底部「出勤日報」按鈕開啟本視窗、每分鐘 tick、有新出勤日報時彈出
"""
import datetime
import queue
import threading

import customtkinter as ctk

from admin import daily_digest
from admin import morning_brief
from rag.chat_view import Bubble
from shared import digest_store
from shared import link_check
from shared import ui_theme

RETRY_AFTER = datetime.timedelta(minutes=10)
POLL_MS = 150
BUBBLE_MIN_WIDTH = 480
BUBBLE_MARGIN = 120      # 視窗寬度扣掉這些（邊距、捲軸、訊息內距）就是訊息寬度


class DigestRunner:
    """排程與背景產生；同一個 runner 同一時間只會有一份在產生。"""

    def __init__(self, root, pending, generate, on_new=None, clock=datetime.datetime.now,
                 make_ai=daily_digest.build_ai, name="出勤日報", retry_wait=None):
        self.root, self.on_new, self._clock, self.name = root, on_new, clock, name
        self._pending, self._generate, self._make_ai = pending, generate, make_ai
        self._retry_wait = retry_wait      # 報告做完但內容不完整時，回傳下次重做前要等多久（None＝不用重做）
        self.busy = False
        self._retry_at = None
        self._alive = True
        self.listeners = []        # 產生完成（成功或失敗）時呼叫，給視窗更新畫面
        self._results = queue.Queue()   # 背景執行緒只把結果放進來，不碰 Tk；主執行緒定時取

    def stop(self):
        self._alive = False

    def tick(self):
        """依排程判斷是否該產生（在主執行緒呼叫）。"""
        now = self._clock()
        if self.busy or (self._retry_at is not None and now < self._retry_at):
            return
        try:
            day = self._pending(now)
        except Exception as e:
            print(f"[{self.name}][WARN] 排程判斷失敗：{type(e).__name__}")
            return
        if day:
            self.start(day, announce=True)

    def start(self, day, announce=False):
        """在背景產生 day 的報告；已有一份在產生就不重複。回傳是否開始。"""
        if self.busy:
            return False
        self.busy = True
        threading.Thread(target=self._work, args=(day, announce), daemon=True).start()
        self.root.after(POLL_MS, self._poll)
        return True

    def _poll(self):
        """主執行緒定時看背景有沒有做完；做完才回呼，視窗已關閉就停止。"""
        try:
            report, error, announce = self._results.get_nowait()
        except queue.Empty:
            if self._alive and self.root.winfo_exists():
                self.root.after(POLL_MS, self._poll)
            else:
                self.busy = False
            return
        self._finish(report, error, announce)

    def _work(self, day, announce):
        report, error = None, None
        try:
            report = self._generate(day, self._make_ai())
        except Exception as e:
            error = type(e).__name__
            print(f"[{self.name}][WARN] 產生失敗：{error}")
        self._results.put((report, error, announce))

    def _finish(self, report, error, announce):
        self.busy = False
        if not self._alive:
            return
        wait = RETRY_AFTER if error else (self._retry_wait(report) if report and self._retry_wait else None)
        self._retry_at = self._clock() + wait if wait else None
        for callback in list(self.listeners):
            callback(self, report, error)
        if report and announce and self.on_new and not (wait and not error):   # 還要重做的先不彈窗，做好了再彈
            self.on_new(report)


def digest_runner(root, on_new=None, **kw):
    """傍晚出勤日報：到設定時間且當天要上班就產生當天，沒到時間就看前一個上班日。"""
    def pending(now):
        latest = digest_store.load()
        return daily_digest.pending_day(now, digest_store.load_time(), latest["date"] if latest else None)

    return DigestRunner(root, pending, daily_digest.generate, on_new, name="出勤日報", **kw)


def morning_runner(root, on_new=None, **kw):
    """早上科技早報：到設定時間且今天要上班就產生今天的；開機比設定時間晚也會補。"""
    def pending(now):
        return morning_brief.pending_morning(now, digest_store.load_morning_time(), digest_store.load_brief())

    kw.setdefault("make_ai", lambda: None)           # 晨報只有新聞，不用語言模型
    return DigestRunner(root, pending, lambda day, _llm: morning_brief.generate(day), on_new, name="科技早報",
                        retry_wait=morning_brief.retry_wait, **kw)


def digest_text(digest) -> str:
    """視窗裡顯示的出勤日報文字。"""
    if digest is None:
        return "還沒有出勤日報。到了設定的時間會自動產生，也可以按「立即產生出勤日報」。"
    parts = [f"📋 出勤日報　資料日期 {digest['date']}　產生於 {digest['generated_at']}", ""]
    if digest.get("summary"):
        parts += ["AI 摘要（數字與人名已核對）", digest["summary"], ""]
    parts.append(digest["facts"])
    return "\n".join(parts)


class ReportView(ctk.CTkFrame):
    """出勤日報頁（嵌在後台主視窗裡，不另開視窗）：只有出勤日報，一則助理訊息（科技早報在前台螢幕顯示，後台不看）。"""

    TABS = (("digest", "📋 出勤日報"),)

    def __init__(self, master, digest, morning=None, tab="digest", on_close=None):
        super().__init__(master, fg_color="transparent", height=1)
        self.runners = {"digest": digest}
        pad = ui_theme.PAD_LG
        self.tabs = ctk.CTkTabview(self)
        self.tabs.pack(fill="both", expand=True, padx=pad, pady=(pad, 8))
        self.bubbles = {}
        for kind, label in self.TABS:
            frame = ctk.CTkScrollableFrame(self.tabs.add(label), fg_color="transparent")
            frame.pack(fill="both", expand=True)
            self.bubbles[kind] = Bubble(frame, "assistant", "", width=BUBBLE_MIN_WIDTH)
            self.bubbles[kind].pack(anchor="w", pady=4)
        self.status = ui_theme.muted_label(self, "要設定時間或手動產生，請到主選單的「出勤日報與科技早報」模組。")
        self.status.pack(fill="x", padx=pad)
        bottom = ctk.CTkFrame(self, fg_color="transparent")
        bottom.pack(fill="x", padx=pad, pady=(6, pad))
        if on_close is not None:
            ui_theme.secondary_button(bottom, "返回主選單", on_close, width=110).pack(side="right")
        for runner in self.runners.values():
            runner.listeners.append(self._on_done)
        self._last_width = 0
        self.bind("<Configure>", ui_theme.event_only(self._on_resize))
        self.bind("<Destroy>", ui_theme.event_only(self._on_destroy))
        self.show(tab)

    def show(self, kind="digest"):
        """重畫出勤日報內容（kind 保留給舊呼叫端，只有出勤日報）。"""
        self.bubbles["digest"].set_text(digest_text(digest_store.load()))
        for bubble in self.bubbles.values():
            bubble.linkify(lambda url: link_check.link_problem(url) == "")
        self.tabs.set(dict(self.TABS)["digest"])

    def _on_resize(self, event):
        """頁面大小改變時，訊息寬度跟著變（寬度沒變就略過，避免重排又觸發自己）。"""
        if not self._is_me(event.widget) or event.width == self._last_width:
            return
        self._last_width = event.width
        width = max(BUBBLE_MIN_WIDTH, event.width - BUBBLE_MARGIN)
        for bubble in self.bubbles.values():
            bubble.set_width(width)

    def _is_me(self, widget):
        """CTkFrame 的事件綁在內部畫布上，事件的 widget 可能是畫布而不是框架本身。"""
        return widget is self or widget is getattr(self, "_canvas", None)

    def _on_destroy(self, event):
        if self._is_me(event.widget):
            for runner in self.runners.values():
                if self._on_done in runner.listeners:
                    runner.listeners.remove(self._on_done)

    def _on_done(self, runner, report, error):
        if self.winfo_exists() and report:
            self.show()

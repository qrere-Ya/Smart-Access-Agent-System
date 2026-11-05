"""
digest_window.py：日報與晨報的背景產生與查看視窗（CustomTkinter）。

主要功能：
  - DigestRunner：在背景執行緒產生報告（查資料庫、必要時呼叫雲端模型、抓新聞），完成後在主執行緒回呼；
    tick() 由後台每分鐘呼叫一次，依 pending 判斷該不該產生；失敗 10 分鐘後才會再試，不會狂重試
    digest_runner（傍晚日報，預設 18:00）與 morning_runner（早上晨報，預設 08:30）是兩個設定好的 DigestRunner
  - open_report_window：只看的視窗，晨報、日報兩個分頁，聊天室樣式、只有助理的訊息、沒有輸入框
    （AI 摘要標示「數字與人名已核對」，沒有摘要就只顯示明細）；視窗可放大、訊息寬度跟著變，滾輪在訊息上也能捲動；
    報告裡的原文網址可以點（只有 https 公開網域，用預設瀏覽器開）；排程自動產生新報告時會彈出並切到對應分頁
  - 產生時間與立即產生不在這裡，在主選單的「日報與晨報」模組（schedule_window）

串接模組：
  - daily_digest／morning_brief：產生與排程判斷；shared.digest_store：存檔與設定；ui_theme：外觀
  - backend_main：底部「日報／晨報」按鈕開啟本視窗、每分鐘 tick、有新報告時彈出
"""
import datetime
import queue
import threading

import customtkinter as ctk

from admin import daily_digest
from admin import morning_brief
from admin import news_feed
from rag.chat_view import Bubble
from shared import digest_store
from shared import ui_theme

RETRY_AFTER = datetime.timedelta(minutes=10)
POLL_MS = 150
BUBBLE_MIN_WIDTH = 480
BUBBLE_MARGIN = 120      # 視窗寬度扣掉這些（邊距、捲軸、訊息內距）就是訊息寬度


class DigestRunner:
    """排程與背景產生；同一個 runner 同一時間只會有一份在產生。"""

    def __init__(self, root, pending, generate, on_new=None, clock=datetime.datetime.now,
                 make_ai=daily_digest.build_ai, name="日報"):
        self.root, self.on_new, self._clock, self.name = root, on_new, clock, name
        self._pending, self._generate, self._make_ai = pending, generate, make_ai
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
        self._retry_at = self._clock() + RETRY_AFTER if error else None
        for callback in list(self.listeners):
            callback(self, report, error)
        if report and announce and self.on_new:
            self.on_new(report)


def digest_runner(root, on_new=None, **kw):
    """傍晚日報：到設定時間且當天要上班就產生當天，沒到時間就看前一個上班日。"""
    def pending(now):
        latest = digest_store.load()
        return daily_digest.pending_day(now, digest_store.load_time(), latest["date"] if latest else None)

    return DigestRunner(root, pending, daily_digest.generate, on_new, name="日報", **kw)


def morning_runner(root, on_new=None, **kw):
    """早上晨報：到設定時間且今天要上班就產生今天的；開機比設定時間晚也會補。"""
    def pending(now):
        latest = digest_store.load_brief()
        return morning_brief.pending_morning(now, digest_store.load_morning_time(), latest["date"] if latest else None)

    return DigestRunner(root, pending, morning_brief.generate, on_new, name="晨報", **kw)


def digest_text(digest) -> str:
    """視窗裡顯示的日報文字。"""
    if digest is None:
        return "還沒有日報。到了設定的時間會自動產生，也可以按「立即產生日報」。"
    parts = [f"📋 日報　資料日期 {digest['date']}　產生於 {digest['generated_at']}", ""]
    if digest.get("summary"):
        parts += ["AI 摘要（數字與人名已核對）", digest["summary"], ""]
    parts.append(digest["facts"])
    return "\n".join(parts)


class ReportWindow(ctk.CTkToplevel):
    """查看視窗：晨報與日報兩個分頁，各一則助理訊息。"""

    TABS = (("morning", "🌅 晨報"), ("digest", "📋 日報"))

    def __init__(self, root, digest, morning, tab="morning"):
        super().__init__(root)
        self.runners = {"digest": digest, "morning": morning}
        self.title("日報／晨報")
        self.geometry("960x860")
        self.minsize(640, 520)
        ui_theme.style_window(self)
        pad = ui_theme.PAD_LG
        self.tabs = ctk.CTkTabview(self)
        self.tabs.pack(fill="both", expand=True, padx=pad, pady=(pad, 8))
        self.bubbles = {}
        for kind, label in self.TABS:
            frame = ctk.CTkScrollableFrame(self.tabs.add(label), fg_color="transparent")
            frame.pack(fill="both", expand=True)
            self.bubbles[kind] = Bubble(frame, "assistant", "", width=BUBBLE_MIN_WIDTH)
            self.bubbles[kind].pack(anchor="w", pady=4)
        self.status = ui_theme.muted_label(self, "要設定時間或手動產生，請到主選單的「日報與晨報」模組。")
        self.status.pack(fill="x", padx=pad)
        bottom = ctk.CTkFrame(self, fg_color="transparent")
        bottom.pack(fill="x", padx=pad, pady=(6, pad))
        ui_theme.secondary_button(bottom, "關閉", self.destroy, width=80).pack(side="right")
        for runner in self.runners.values():
            runner.listeners.append(self._on_done)
        self._last_width = 0
        self.bind("<Configure>", self._on_resize)
        self.bind("<Destroy>", self._on_destroy)
        self.show(tab)

    def show(self, kind):
        """重畫兩個分頁的內容並切到 kind。"""
        self.bubbles["morning"].set_text(morning_brief.brief_text(digest_store.load_brief()))
        self.bubbles["digest"].set_text(digest_text(digest_store.load()))
        for bubble in self.bubbles.values():
            bubble.linkify(lambda url: news_feed.link_problem(url) == "")
        self.tabs.set(dict(self.TABS)[kind])
        self.lift()

    def _on_resize(self, event):
        """視窗大小改變時，訊息寬度跟著變（寬度沒變就略過，避免重排又觸發自己）。"""
        if event.widget is not self or event.width == self._last_width:
            return
        self._last_width = event.width
        width = max(BUBBLE_MIN_WIDTH, event.width - BUBBLE_MARGIN)
        for bubble in self.bubbles.values():
            bubble.set_width(width)

    def _on_destroy(self, event):
        if event.widget is self:
            for runner in self.runners.values():
                if self._on_done in runner.listeners:
                    runner.listeners.remove(self._on_done)

    def _on_done(self, runner, report, error):
        if self.winfo_exists() and report:
            self.show("morning" if runner is self.runners["morning"] else "digest")


def open_report_window(root, digest, morning, tab="morning"):
    """開啟日報／晨報查看視窗並回傳它。"""
    return ReportWindow(root, digest, morning, tab)

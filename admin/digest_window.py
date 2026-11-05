"""
digest_window.py：日報與晨報的背景產生與顯示視窗（CustomTkinter）。

主要功能：
  - DigestRunner：在背景執行緒產生報告（查資料庫、必要時呼叫雲端模型、抓新聞），完成後在主執行緒回呼；
    tick() 由後台每分鐘呼叫一次，依 pending 判斷該不該產生；失敗 10 分鐘後才會再試，不會狂重試
    digest_runner（傍晚日報，預設 18:00）與 morning_runner（早上晨報，預設 08:30）是兩個設定好的 DigestRunner
  - open_digest_window：聊天室樣式的視窗，只有助理的訊息、沒有輸入框；上面是今天的晨報、下面是最新日報
    （AI 摘要標示「數字與人名已核對」，沒有摘要就只顯示明細）；可分別設定兩個產生時間、
    可指定日期立即產生日報、可立即產生晨報

串接模組：
  - daily_digest／morning_brief：產生與排程判斷；shared.digest_store：存檔與設定；ui_theme：外觀
  - backend_main：底部掛「📰 日報／晨報」按鈕、每分鐘 tick、有新報告時彈窗
"""
import datetime
import threading

import customtkinter as ctk

from admin import attendance_report
from admin import daily_digest
from admin import morning_brief
from rag.chat_view import Bubble
from shared import digest_store
from shared import ui_theme

RETRY_AFTER = datetime.timedelta(minutes=10)


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
        return True

    def _work(self, day, announce):
        report, error = None, None
        try:
            report = self._generate(day, self._make_ai())
        except Exception as e:
            error = type(e).__name__
            print(f"[{self.name}][WARN] 產生失敗：{error}")
        try:
            self.root.after(0, lambda: self._finish(report, error, announce))
        except RuntimeError:       # 主視窗已經結束
            self.busy = False

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


class DigestWindow(ctk.CTkToplevel):
    """日報與晨報視窗。"""

    def __init__(self, root, runner, morning):
        super().__init__(root)
        self.runner, self.morning = runner, morning
        self.title("日報與晨報")
        self.geometry("680x720")
        ui_theme.style_window(self)
        pad = ui_theme.PAD_LG
        ui_theme.section_title(self, "📰 日報與晨報").pack(anchor="w", padx=pad, pady=(pad, 4))
        self.chat = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self.chat.pack(fill="both", expand=True, padx=pad, pady=(4, 8))
        self.brief_bubble = Bubble(self.chat, "assistant", "", width=560)
        self.brief_bubble.pack(anchor="w", pady=4)
        self.digest_bubble = Bubble(self.chat, "assistant", "", width=560)
        self.digest_bubble.pack(anchor="w", pady=4)
        self.time_entries = {}
        for label, key, load, save in (("晨報每天產生時間", "morning", digest_store.load_morning_time,
                                        digest_store.save_morning_time),
                                       ("日報每天產生時間", "digest", digest_store.load_time, digest_store.save_time)):
            row = ctk.CTkFrame(self, fg_color="transparent")
            row.pack(fill="x", padx=pad, pady=(0, 4))
            ui_theme.muted_label(row, f"{label}（HH:MM）").pack(side="left")
            entry = ui_theme.entry(row, width=70)
            entry.insert(0, load())
            entry.pack(side="left", padx=6)
            self.time_entries[key] = entry
            ui_theme.secondary_button(row, "儲存時間", lambda k=key, s=save: self._save_time(k, s), width=90).pack(
                side="left")
        self.status = ui_theme.muted_label(self, "")
        self.status.pack(fill="x", padx=pad)
        bottom = ctk.CTkFrame(self, fg_color="transparent")
        bottom.pack(fill="x", padx=pad, pady=(6, pad))
        self.morning_button = ui_theme.primary_button(bottom, "立即產生晨報", self._run_morning, width=120)
        self.morning_button.pack(side="left")
        self.run_button = ui_theme.primary_button(bottom, "立即產生日報", self._run_now, width=120)
        self.run_button.pack(side="left", padx=(8, 0))
        ui_theme.muted_label(bottom, "日報日期").pack(side="left", padx=(10, 4))
        self.date_entry = ui_theme.entry(bottom, width=110)
        self.date_entry.insert(0, datetime.date.today().isoformat())
        self.date_entry.pack(side="left")
        ui_theme.secondary_button(bottom, "關閉", self.destroy, width=80).pack(side="right")
        for r in (runner, morning):
            r.listeners.append(self._on_done)
        self.bind("<Destroy>", self._on_destroy)
        self.refresh()

    def _on_destroy(self, event):
        if event.widget is self:
            for r in (self.runner, self.morning):
                if self._on_done in r.listeners:
                    r.listeners.remove(self._on_done)

    def refresh(self):
        """重畫兩則訊息與按鈕狀態。"""
        self.brief_bubble.set_text(morning_brief.brief_text(digest_store.load_brief()))
        self.digest_bubble.set_text(digest_text(digest_store.load()))
        self.run_button.configure(state="disabled" if self.runner.busy else "normal")
        self.morning_button.configure(state="disabled" if self.morning.busy else "normal")
        if self.runner.busy or self.morning.busy:
            self.status.configure(text="產生中…（抓新聞與語言模型摘要約需 20 秒）")

    def _on_done(self, runner, report, error):
        if not self.winfo_exists():
            return
        self.status.configure(text=f"{runner.name}產生失敗（{error}），10 分鐘後會自動再試。" if error
                              else f"{runner.name}已更新。")
        self.refresh()

    def _run_morning(self):
        if self.morning.start(datetime.date.today().isoformat()):
            self.refresh()

    def _run_now(self):
        try:
            day = attendance_report.parse_date(self.date_entry.get())
        except ValueError as e:
            ui_theme.show_message(self, "日報與晨報", str(e), "warning")
            return
        if self.runner.start(day):
            self.refresh()

    def _save_time(self, key, save):
        try:
            save(self.time_entries[key].get().strip())
        except ValueError as e:
            ui_theme.show_message(self, "日報與晨報", str(e), "warning")
            return
        except OSError:
            ui_theme.show_message(self, "日報與晨報", "儲存失敗，請確認 runtime 資料夾可以寫入。", "error")
            return
        self.status.configure(text="已儲存；下次到這個時間會自動產生。")


def open_digest_window(root, runner, morning):
    """開啟日報與晨報視窗並回傳它。"""
    return DigestWindow(root, runner, morning)

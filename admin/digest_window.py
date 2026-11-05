"""
digest_window.py：每日出勤日報的背景產生與顯示視窗（CustomTkinter）。

主要功能：
  - DigestRunner：在背景執行緒產生日報（查資料庫、必要時呼叫雲端模型），完成後在主執行緒回呼；
    tick() 由後台每分鐘呼叫一次，依 daily_digest.pending_day 判斷該不該產生；失敗 10 分鐘後才會再試，不會狂重試
  - open_digest_window：顯示最新日報（AI 摘要標示「數字與人名已核對」，沒有摘要就只顯示明細）、
    可設定每天產生的時間、可指定日期（預設今天）立即產生

串接模組：
  - daily_digest：產生與排程判斷；shared.digest_store：存檔與設定；ui_theme：外觀
  - backend_main：底部掛「📰 日報」按鈕、每分鐘 tick、有新日報時彈窗
"""
import datetime
import threading

import customtkinter as ctk

from admin import attendance_report
from admin import daily_digest
from shared import digest_store
from shared import ui_theme

RETRY_AFTER = datetime.timedelta(minutes=10)


class DigestRunner:
    """排程與背景產生；同一時間只會有一份在產生。"""

    def __init__(self, root, on_new=None, clock=datetime.datetime.now, generate=daily_digest.generate,
                 make_ai=daily_digest.build_ai):
        self.root, self.on_new, self._clock = root, on_new, clock
        self._generate, self._make_ai = generate, make_ai
        self.busy = False
        self._retry_at = None
        self._alive = True
        self.listeners = []        # 產生完成（成功或失敗）時呼叫，給日報視窗更新畫面

    def stop(self):
        self._alive = False

    def tick(self):
        """依排程判斷是否該產生日報（在主執行緒呼叫）。"""
        now = self._clock()
        if self.busy or (self._retry_at is not None and now < self._retry_at):
            return
        latest = digest_store.load()
        try:
            day = daily_digest.pending_day(now, digest_store.load_time(), latest["date"] if latest else None)
        except Exception as e:
            print(f"[日報][WARN] 排程判斷失敗：{type(e).__name__}")
            return
        if day:
            self.start(day, announce=True)

    def start(self, day, announce=False):
        """在背景產生 day 的日報；已有一份在產生就不重複。回傳是否開始。"""
        if self.busy:
            return False
        self.busy = True
        threading.Thread(target=self._work, args=(day, announce), daemon=True).start()
        return True

    def _work(self, day, announce):
        digest, error = None, None
        try:
            digest = self._generate(day, self._make_ai())
        except Exception as e:
            error = type(e).__name__
            print(f"[日報][WARN] 產生失敗：{error}")
        try:
            self.root.after(0, lambda: self._finish(digest, error, announce))
        except RuntimeError:       # 主視窗已經結束
            self.busy = False

    def _finish(self, digest, error, announce):
        self.busy = False
        if not self._alive:
            return
        self._retry_at = self._clock() + RETRY_AFTER if error else None
        for callback in list(self.listeners):
            callback(digest, error)
        if digest and announce and self.on_new:
            self.on_new(digest)


def digest_text(digest) -> str:
    """視窗裡顯示的日報文字。"""
    if digest is None:
        return "還沒有日報。到了設定的時間會自動產生，也可以按「立即產生」。"
    parts = [f"資料日期 {digest['date']}　產生於 {digest['generated_at']}", ""]
    if digest.get("summary"):
        parts += ["AI 摘要（數字與人名已核對）", digest["summary"], ""]
    parts.append(digest["facts"])
    return "\n".join(parts)


class DigestWindow(ctk.CTkToplevel):
    """日報視窗。"""

    def __init__(self, root, runner):
        super().__init__(root)
        self.runner = runner
        self.title("每日出勤日報")
        self.geometry("640x560")
        ui_theme.style_window(self)
        pad = ui_theme.PAD_LG
        ui_theme.section_title(self, "📰 每日出勤日報").pack(anchor="w", padx=pad, pady=(pad, 4))
        self.box = ctk.CTkTextbox(self, wrap="word", font=ui_theme.font(), border_width=1)
        self.box.pack(fill="both", expand=True, padx=pad, pady=(4, 8))
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(fill="x", padx=pad, pady=(0, 4))
        ui_theme.muted_label(row, "每天產生時間（HH:MM）").pack(side="left")
        self.time_entry = ui_theme.entry(row, width=70)
        self.time_entry.insert(0, digest_store.load_time())
        self.time_entry.pack(side="left", padx=6)
        ui_theme.secondary_button(row, "儲存時間", self._save_time, width=90).pack(side="left")
        self.status = ui_theme.muted_label(self, "")
        self.status.pack(fill="x", padx=pad)
        bottom = ctk.CTkFrame(self, fg_color="transparent")
        bottom.pack(fill="x", padx=pad, pady=(6, pad))
        self.run_button = ui_theme.primary_button(bottom, "立即產生", self._run_now, width=90)
        self.run_button.pack(side="left")
        ui_theme.muted_label(bottom, "日期").pack(side="left", padx=(10, 4))
        self.date_entry = ui_theme.entry(bottom, width=110)
        self.date_entry.insert(0, datetime.date.today().isoformat())
        self.date_entry.pack(side="left")
        ui_theme.secondary_button(bottom, "關閉", self.destroy, width=80).pack(side="right")
        runner.listeners.append(self._on_done)
        self.bind("<Destroy>", self._on_destroy)
        self.refresh()

    def _on_destroy(self, event):
        if event.widget is self and self._on_done in self.runner.listeners:
            self.runner.listeners.remove(self._on_done)

    def refresh(self):
        """重畫日報文字與按鈕狀態。"""
        self.box.configure(state="normal")
        self.box.delete("1.0", "end")
        self.box.insert("1.0", digest_text(digest_store.load()))
        self.box.configure(state="disabled")
        busy = self.runner.busy
        self.run_button.configure(state="disabled" if busy else "normal")
        if busy:
            self.status.configure(text="產生中…（有語言模型摘要時約需 20 秒）")

    def _on_done(self, digest, error):
        if not self.winfo_exists():
            return
        self.status.configure(text=f"產生失敗（{error}），10 分鐘後會自動再試。" if error else "已更新。")
        self.refresh()

    def _run_now(self):
        try:
            day = attendance_report.parse_date(self.date_entry.get())
        except ValueError as e:
            ui_theme.show_message(self, "每日出勤日報", str(e), "warning")
            return
        if self.runner.start(day):
            self.refresh()

    def _save_time(self):
        try:
            digest_store.save_time(self.time_entry.get().strip())
        except ValueError as e:
            ui_theme.show_message(self, "每日出勤日報", str(e), "warning")
            return
        except OSError:
            ui_theme.show_message(self, "每日出勤日報", "儲存失敗，請確認 runtime 資料夾可以寫入。", "error")
            return
        self.status.configure(text="已儲存；下次到這個時間會自動產生。")


def open_digest_window(root, runner):
    """開啟日報視窗並回傳它。"""
    return DigestWindow(root, runner)

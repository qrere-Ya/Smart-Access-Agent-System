"""
schedule_window.py：主選單「日報與晨報」模組的頁面（CustomTkinter，嵌在後台主視窗裡）——排程時間設定與立即產生。

主要功能：
  - 兩個時間欄（晨報預設 08:30、日報預設 18:00，HH:MM 24 小時制），按「儲存」一次存兩個；
    有一個格式不對就都不存並說明哪一個不對
  - 「立即產生晨報」（今天）、「立即產生日報」（可指定日期，預設今天），產生中按鈕停用，完成後顯示結果；
    日期格式不對不會產生
  - 「查看日報」開啟日報查看視窗；晨報產生後送到前台螢幕顯示，後台不看
  - 「全本地模式」開關：開啟後語言模型只用本機、晨報不連網抓新聞（shared.network_policy）；
    已開啟的語言模型要重新開啟助理視窗才會換成本機
  - 說明排程怎麼跑：後台開著才會產生、到時間且當天要上班才產生、開機比設定時間晚會補產、假日與休息日不產生；
    設定存在 runtime/digest_settings.json，後台每分鐘讀一次，改完下一分鐘就生效，不用重開

串接模組：
  - digest_window：DigestRunner（背景產生）；shared.digest_store：讀寫時間設定；attendance_report：日期驗證
  - ui_theme：外觀；backend_main：主選單的「日報與晨報」卡片開啟本視窗
"""
import datetime

import customtkinter as ctk

from admin import attendance_report
from shared import digest_store
from shared import network_policy
from shared import ui_theme

EXPLAIN = ("後台開著的時候，每分鐘檢查一次。到了設定的時間、而且當天要上班（不是假日或每週休息日），"
           "就自動產生：日報會彈出視窗，晨報送到前台的螢幕顯示（後台不顯示）。\n"
           "如果開機時已經超過設定的時間，會補產：晨報補今天的，日報補最近一個上班日。\n"
           "改完按「儲存」，下一分鐘就生效，不用重開後台。")


class ScheduleView(ctk.CTkFrame):
    """日報與晨報模組頁（嵌在後台主視窗裡，不另開視窗）：排程時間、立即產生、查看。"""

    def __init__(self, master, digest, morning, on_view=None, on_close=None):
        super().__init__(master, fg_color="transparent", height=1)
        self.runners = {"digest": digest, "morning": morning}
        self._on_view = on_view
        pad = ui_theme.PAD_LG
        ui_theme.section_title(self, "📰 日報與晨報").pack(anchor="w", padx=pad, pady=(pad, 8))
        ui_theme.muted_label(self, EXPLAIN, wraplength=900, justify="left").pack(fill="x", padx=pad, pady=(0, 10))

        self.local_switch = ctk.CTkSwitch(self, text="全本地模式（不連外網：語言模型只用本機、晨報不抓新聞）",
                                          command=self._toggle_local_only)
        if network_policy.local_only():
            self.local_switch.select()
        self.local_switch.pack(anchor="w", padx=pad, pady=(0, 10))

        ui_theme.body_label(self, "排程時間（每天）").pack(anchor="w", padx=pad, pady=(6, 2))
        form = ctk.CTkFrame(self, fg_color="transparent")
        form.pack(fill="x", padx=pad)
        self.entries = {}
        for row, (key, label, load) in enumerate((("morning", "🌅 晨報（科技新聞）",
                                                   digest_store.load_morning_time),
                                                  ("digest", "📋 日報（當天出勤明細）", digest_store.load_time))):
            ui_theme.muted_label(form, label).grid(row=row * 2, column=0, columnspan=2, sticky="w", pady=(6, 2))
            entry = ui_theme.entry(form, width=90)
            entry.insert(0, load())
            entry.grid(row=row * 2 + 1, column=0, sticky="w")
            ui_theme.muted_label(form, "HH:MM").grid(row=row * 2 + 1, column=1, sticky="w", padx=10)
            self.entries[key] = entry
        ui_theme.primary_button(self, "儲存時間", self.save, width=100).pack(anchor="w", padx=pad, pady=(10, 0))

        ui_theme.body_label(self, "立即產生").pack(anchor="w", padx=pad, pady=(20, 2))
        gen = ctk.CTkFrame(self, fg_color="transparent")
        gen.pack(fill="x", padx=pad)
        self.morning_button = ui_theme.secondary_button(gen, "🌅 產生今天的晨報", self._run_morning, width=150)
        self.morning_button.grid(row=0, column=0, sticky="w", pady=4)
        ui_theme.muted_label(gen, "抓新聞約需 45 秒，產生後送到前台顯示").grid(row=0, column=1, sticky="w", padx=10)
        self.digest_button = ui_theme.secondary_button(gen, "📋 產生日報", self._run_digest, width=150)
        self.digest_button.grid(row=1, column=0, sticky="w", pady=4)
        date_box = ctk.CTkFrame(gen, fg_color="transparent")
        date_box.grid(row=1, column=1, sticky="w", padx=10)
        ui_theme.muted_label(date_box, "日期").pack(side="left", padx=(0, 4))
        self.date_entry = ui_theme.entry(date_box, width=110)
        self.date_entry.insert(0, datetime.date.today().isoformat())
        self.date_entry.pack(side="left")

        self.status = ui_theme.muted_label(self, "", wraplength=520, justify="left")
        self.status.pack(fill="x", padx=pad, pady=(14, 0))
        bottom = ctk.CTkFrame(self, fg_color="transparent")
        bottom.pack(fill="x", padx=pad, pady=(8, pad), side="bottom")
        if on_view is not None:
            ui_theme.primary_button(bottom, "查看日報", lambda: on_view("digest"), width=100).pack(side="left")
        if on_close is not None:
            ui_theme.secondary_button(bottom, "返回主選單", on_close, width=110).pack(side="right")
        for runner in self.runners.values():
            runner.listeners.append(self._on_done)
        self.bind("<Destroy>", self._on_destroy)
        self._sync_buttons()

    def _on_destroy(self, event):
        if event.widget is self or event.widget is getattr(self, "_canvas", None):   # CTkFrame 的事件綁在內部畫布上
            for runner in self.runners.values():
                if self._on_done in runner.listeners:
                    runner.listeners.remove(self._on_done)

    def _sync_buttons(self):
        for key, button in (("morning", self.morning_button), ("digest", self.digest_button)):
            button.configure(state="disabled" if self.runners[key].busy else "normal")
        if any(r.busy for r in self.runners.values()):
            self.status.configure(text="產生中…")

    def _on_done(self, runner, report, error):
        if not self.winfo_exists():
            return
        if error:
            text = f"{runner.name}產生失敗（{error}），10 分鐘後會自動再試。"
        elif runner is self.runners["morning"]:
            text = "晨報已更新，前台下次同步（約 10 秒內）就會顯示。"
        else:
            text = "日報已更新，可以按「查看日報」。"
        self.status.configure(text=text)
        self._sync_buttons()

    def save(self):
        """檢查兩個時間都合法才一起存；有一個不合法就都不存並說明。"""
        values = {key: entry.get().strip() for key, entry in self.entries.items()}
        names = {"morning": "晨報", "digest": "日報"}
        bad = [names[k] for k, v in values.items() if not digest_store.valid_time(v)]
        if bad:
            ui_theme.show_message(self, "日報與晨報",
                                  f"{'、'.join(bad)}的時間格式需為 HH:MM（24 小時制），例如 08:30。", "warning")
            return
        try:
            digest_store.save_morning_time(values["morning"])
            digest_store.save_time(values["digest"])
        except OSError:
            ui_theme.show_message(self, "日報與晨報", "儲存失敗，請確認 runtime 資料夾可以寫入。", "error")
            return
        self.status.configure(text=f"已儲存：晨報 {values['morning']}、日報 {values['digest']}。")

    def _toggle_local_only(self):
        """存下全本地模式的開關；環境變數有設定時以環境變數為準，要提示。"""
        want = bool(self.local_switch.get())
        try:
            network_policy.set_local_only(want)
        except OSError:
            self.local_switch.toggle()
            ui_theme.show_message(self, "全本地模式", "儲存失敗，請確認 runtime 資料夾可以寫入。", "error")
            return
        if network_policy.local_only() != want:
            self.status.configure(text=f"環境變數 {network_policy.ENV_NAME} 已強制設定，畫面上的開關不會改變實際結果。")
            return
        self.status.configure(text="全本地模式已開啟：語言模型只用本機、晨報不連網。已開著的助理視窗請重新開啟。"
                              if want else "全本地模式已關閉：可以使用雲端語言模型與晨報新聞。")

    def _run_morning(self):
        if self.runners["morning"].start(datetime.date.today().isoformat()):
            self._sync_buttons()

    def _run_digest(self):
        try:
            day = attendance_report.parse_date(self.date_entry.get())
        except ValueError as e:
            ui_theme.show_message(self, "日報與晨報", str(e), "warning")
            return
        if self.runners["digest"].start(day):
            self._sync_buttons()

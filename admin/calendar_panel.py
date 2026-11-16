"""
calendar_panel.py：出勤報告頁（CustomTkinter）——兩個分頁：每日出勤報告、對話紀錄。

主要功能：
  - 「每日出勤報告」分頁：內嵌 report_panel（查詢、前後一天、匯出 CSV），頁首說明缺勤的判斷方式，
    並顯示「假日資料來源與最後更新時間」（work_calendar.source_text）；沒有國定假日資料時明講目前只算週六、週日
  - 「對話紀錄」分頁：內嵌 chat_log_panel（AI 問答的紀錄、審查、匯出）
  - 假日、請假與每週休息日不再手動維護：週六日與國定假日、補班日由 work_calendar 自動判斷

串接模組：
  - report_panel：每日出勤報告面板；chat_log_panel：對話紀錄面板
  - shared.work_calendar：休息日判斷與資料來源說明；calendar_fetch：自動抓取與最近一次的問題
  - ui_theme：色彩、字型
  - backend_main：在主視窗內以頁面顯示，傳入登入的管理者名稱（審查者）
"""
import tkinter as tk

import customtkinter as ctk

from admin import calendar_fetch, chat_log_panel, report_panel
from shared import ui_theme, work_calendar

REPORT_TAB = "每日出勤報告"
LOG_TAB = "對話紀錄"


class CalendarPanel(ctk.CTkFrame):
    """出勤報告頁：標題與兩個分頁。"""

    def __init__(self, master, reviewer=""):
        super().__init__(master, fg_color="transparent", height=1)
        pad = ui_theme.PAD_LG
        self.tabs = ctk.CTkTabview(
            self, fg_color=ui_theme.pair("card"), border_width=1, border_color=ui_theme.pair("border"),
            segmented_button_fg_color=ui_theme.pair("secondary"),
            segmented_button_selected_color=ui_theme.pair("primary"),
            segmented_button_selected_hover_color=ui_theme.pair("primary_hover"),
            segmented_button_unselected_color=ui_theme.pair("secondary"),
            segmented_button_unselected_hover_color=ui_theme.pair("secondary_hover"),
            text_color=ui_theme.pair("text"), command=self._on_tab_change)
        try:
            self.tabs._segmented_button.configure(font=ui_theme.font(bold=True))   # CTkTabview 沒有公開的字型參數
        except (AttributeError, tk.TclError, ValueError):
            pass
        self.tabs.pack(fill="both", expand=True, padx=pad, pady=pad)

        report_tab = self.tabs.add(REPORT_TAB)
        ui_theme.muted_label(
            report_tab, "缺勤＝非休息日且當天完全沒有打卡紀錄；週六日與國定假日（補班日當上班日）自動判斷。"
        ).pack(anchor="w", pady=(2, 0))
        self.source_label = ui_theme.muted_label(report_tab, self._source_text(), wraplength=900, justify="left")
        self.source_label.pack(anchor="w", pady=(0, 8))
        self.report_panel = report_panel.ReportPanel(report_tab, embedded=True)
        self.report_panel.pack(fill="both", expand=True)

        self.chat_log_panel = chat_log_panel.ChatLogPanel(self.tabs.add(LOG_TAB), reviewer=reviewer)
        self.chat_log_panel.pack(fill="both", expand=True)

    @staticmethod
    def _source_text():
        """資料來源與最後更新時間；最近一次更新有問題時附上原因（沒網路、全本地模式、資料驗證不過）。"""
        text = work_calendar.source_text()
        issue = calendar_fetch.problem()
        return f"{text}\n⚠ {issue}" if issue else text

    def _on_tab_change(self):
        """切到哪一頁就重查哪一頁（對話紀錄可能剛有新的）。"""
        self.refresh_all()

    def refresh_all(self, *_):
        """重查目前這一頁並更新資料來源說明。"""
        self.source_label.configure(text=self._source_text())
        if self.tabs.get() == LOG_TAB:
            self.chat_log_panel.refresh()
        else:
            self.report_panel.refresh()

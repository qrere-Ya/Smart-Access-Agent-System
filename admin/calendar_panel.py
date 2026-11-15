"""
calendar_panel.py：出勤報告頁（CustomTkinter）——只有每日出勤報告，沒有分頁。

主要功能：
  - 內嵌 report_panel（每日出勤報告：查詢、前後一天、匯出 CSV）
  - 頁首說明缺勤的判斷方式，並顯示「假日資料來源與最後更新時間」（work_calendar.source_text）；
    沒有國定假日資料時明講目前只算週六、週日
  - 假日、請假與每週休息日不再手動維護：週六日與國定假日、補班日由 work_calendar 自動判斷

串接模組：
  - report_panel：每日出勤報告面板
  - shared.work_calendar：休息日判斷與資料來源說明
  - ui_theme：色彩、字型
  - backend_main：在主視窗內以頁面顯示
"""
import customtkinter as ctk

from admin import report_panel
from shared import ui_theme, work_calendar


class CalendarPanel(ctk.CTkFrame):
    """出勤報告頁：標題、假日資料來源說明與每日出勤報告。"""

    def __init__(self, master):
        super().__init__(master, fg_color="transparent", height=1)
        pad = ui_theme.PAD_LG
        ui_theme.section_title(self, "🗓️ 出勤報告").pack(anchor="w", padx=pad, pady=(pad, 0))
        ui_theme.muted_label(
            self, "缺勤＝非休息日且當天完全沒有打卡紀錄；週六日與國定假日（補班日當上班日）自動判斷，不用手動登錄。"
        ).pack(anchor="w", padx=pad, pady=(2, 0))
        self.source_label = ui_theme.muted_label(self, work_calendar.source_text())
        self.source_label.pack(anchor="w", padx=pad, pady=(0, 8))
        self.report_panel = report_panel.ReportPanel(self, embedded=True)
        self.report_panel.pack(fill="both", expand=True, padx=pad, pady=(0, pad))

    def refresh_all(self, *_):
        """重查報告並更新資料來源說明。"""
        self.source_label.configure(text=work_calendar.source_text())
        self.report_panel.refresh()

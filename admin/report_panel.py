"""
report_panel.py：每日出勤報告的畫面（CustomTkinter 外框，表格用調過樣式的 ttk.Treeview）。

主要功能：
  - 日期輸入（YYYY-MM-DD）與前一天、後一天、今天按鈕
  - 以表格顯示每位員工當天狀態（斑馬紋、可捲動），上方顯示各狀態人數與異常事件數
  - 匯出 CSV 到使用者選的位置
  - 錯誤用 ui_theme.show_message 說明；查詢失敗不顯示例外內容

串接模組：
  - attendance_report：查詢與匯出
  - ui_theme：色彩、字型、按鈕、表格樣式、對話框
  - backend_main：用 open_report_window 開啟
"""
import datetime
import tkinter as tk
from tkinter import filedialog

import customtkinter as ctk

from admin import attendance_report
from shared import ui_theme


class ReportPanel(ctk.CTkFrame):
    """每日出勤報告面板。"""

    COLUMNS = (("employee_id", "員工編號", 90), ("name", "姓名", 120), ("status", "狀態", 80),
               ("clock_in", "上班時間", 90), ("clock_out", "下班時間", 90), ("note", "備註", 300))

    def __init__(self, master, embedded=False):
        super().__init__(master, fg_color="transparent", height=1)
        self.report = None
        pad = 0 if embedded else ui_theme.PAD_LG       # 放進分頁時不重複標題與外距
        if not embedded:
            ui_theme.section_title(self, "📋 每日出勤報告").pack(anchor="w", padx=pad, pady=(pad, 0))
        ui_theme.muted_label(
            self, "缺勤＝非假日、非請假且當天完全沒有打卡紀錄；假日與請假資料在隔壁分頁維護。").pack(
            anchor="w", padx=pad, pady=(4 if embedded else 2, 10))

        bar = ctk.CTkFrame(self, fg_color="transparent", height=1)
        bar.pack(fill="x", padx=pad, pady=(0, 8))
        self.date_var = tk.StringVar(master=self, value=datetime.date.today().strftime("%Y-%m-%d"))
        ui_theme.secondary_button(bar, "◀", lambda: self.shift_day(-1), width=36).pack(side="left")
        ui_theme.entry(bar, textvariable=self.date_var, width=120).pack(side="left", padx=4)
        ui_theme.secondary_button(bar, "▶", lambda: self.shift_day(1), width=36).pack(side="left")
        ui_theme.secondary_button(bar, "今天", self.go_today, width=60).pack(side="left", padx=4)
        ui_theme.primary_button(bar, "查詢", self.refresh, width=70).pack(side="left", padx=4)
        ui_theme.secondary_button(bar, "匯出 CSV", self.export, width=90).pack(side="left", padx=4)

        self.summary_var = tk.StringVar(master=self, value="")
        ctk.CTkLabel(self, textvariable=self.summary_var, font=ui_theme.font(), text_color=ui_theme.pair("text"),
                     anchor="w").pack(fill="x", padx=pad, pady=(0, 6))

        box, self.tree = ui_theme.table(self, self.COLUMNS, height=12)
        box.pack(fill="both", expand=True, padx=pad, pady=(0, pad))
        self.refresh()

    def go_today(self):
        """日期改成今天並重新查詢。"""
        self.date_var.set(datetime.date.today().strftime("%Y-%m-%d"))
        self.refresh()

    def shift_day(self, delta):
        """日期前後移動 delta 天並重新查詢；日期不合法時提示。"""
        try:
            day = datetime.datetime.strptime(attendance_report.parse_date(self.date_var.get()), "%Y-%m-%d")
        except ValueError as e:
            ui_theme.show_message(self.winfo_toplevel(), "每日出勤報告", str(e), "warning")
            return
        self.date_var.set((day + datetime.timedelta(days=delta)).strftime("%Y-%m-%d"))
        self.refresh()

    def refresh(self):
        """依輸入日期查詢並更新表格與摘要。"""
        try:
            self.report = attendance_report.build_report(self.date_var.get())
        except ValueError as e:
            ui_theme.show_message(self.winfo_toplevel(), "每日出勤報告", str(e), "warning")
            return
        except Exception as e:
            print(f"[出勤報告] 查詢失敗：{type(e).__name__}")
            ui_theme.show_message(self.winfo_toplevel(), "每日出勤報告", "查詢失敗，請稍後再試。", "error")
            return
        ui_theme.fill_table(self.tree, [[r[c[0]] for c in self.COLUMNS] for r in self.report["rows"]])
        parts = [f"{k} {v}" for k, v in self.report["counts"].items()]
        self.summary_var.set(f"{self.report['date']}｜" + "、".join(parts or ["沒有在職員工"])
                             + f"｜異常事件 {self.report['anomaly_count']} 件")

    def export(self):
        """讓使用者選位置，把目前顯示的報告匯出成 CSV。"""
        if self.report is None:
            return
        path = filedialog.asksaveasfilename(
            parent=self.winfo_toplevel(), defaultextension=".csv", filetypes=[("CSV 檔", "*.csv")],
            initialfile=f"attendance_{self.report['date']}.csv")
        if not path:
            return
        try:
            count = attendance_report.export_csv(self.report, path)
        except OSError as e:
            print(f"[出勤報告] 匯出失敗：{type(e).__name__}")
            ui_theme.show_message(self.winfo_toplevel(), "每日出勤報告", "匯出失敗，請確認位置可以寫入。", "error")
            return
        ui_theme.show_message(self.winfo_toplevel(), "每日出勤報告", f"已匯出 {count} 筆到\n{path}")


def open_report_window(root):
    """開啟每日出勤報告視窗並回傳它。"""
    win = ctk.CTkToplevel(root)
    win.title("每日出勤報告")
    win.geometry("900x560")
    ui_theme.style_window(win)
    ReportPanel(win).pack(fill="both", expand=True)
    return win

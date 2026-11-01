"""
report_panel.py：每日出勤報告的畫面（獨立 Frame，不含查詢與匯出邏輯）。

主要功能：
  - 日期輸入（YYYY-MM-DD）與前一天、後一天、今天按鈕
  - 以表格顯示每位員工當天狀態，上方顯示各狀態人數與異常事件數
  - 匯出 CSV 到使用者選的位置

串接模組：
  - attendance_report：查詢與匯出
  - backend_main：用 open_report_window 開啟
"""
import datetime
from tkinter import filedialog, messagebox

import ttkbootstrap as ttk
from ttkbootstrap.constants import BOTH, LEFT, PRIMARY, SECONDARY, W, X, OUTLINE

from admin import attendance_report


class ReportPanel(ttk.Frame):
    """每日出勤報告面板。"""

    COLUMNS = (("employee_id", "員工編號", 90), ("name", "姓名", 120), ("status", "狀態", 80),
               ("clock_in", "上班時間", 90), ("clock_out", "下班時間", 90), ("note", "備註", 300))

    def __init__(self, master):
        super().__init__(master, padding=20)
        self.report = None
        ttk.Label(self, text="📋 每日出勤報告", font=("微軟正黑體", 16, "bold"), bootstyle=PRIMARY).pack(anchor=W)
        ttk.Label(self, text="缺勤＝當天完全沒有打卡紀錄；系統沒有假日與請假資料，週末與請假也會列為缺勤。",
                  font=("微軟正黑體", 9), bootstyle=SECONDARY).pack(anchor=W, pady=(2, 10))

        bar = ttk.Frame(self)
        bar.pack(fill=X, pady=(0, 8))
        self.date_var = ttk.StringVar(value=datetime.date.today().strftime("%Y-%m-%d"))
        ttk.Button(bar, text="◀", width=3, bootstyle=(SECONDARY, OUTLINE),
                   command=lambda: self.shift_day(-1)).pack(side=LEFT)
        ttk.Entry(bar, textvariable=self.date_var, width=12).pack(side=LEFT, padx=4)
        ttk.Button(bar, text="▶", width=3, bootstyle=(SECONDARY, OUTLINE),
                   command=lambda: self.shift_day(1)).pack(side=LEFT)
        ttk.Button(bar, text="今天", bootstyle=(SECONDARY, OUTLINE), command=self.go_today).pack(side=LEFT, padx=4)
        ttk.Button(bar, text="查詢", bootstyle=PRIMARY, command=self.refresh).pack(side=LEFT, padx=4)
        ttk.Button(bar, text="匯出 CSV", bootstyle=(PRIMARY, OUTLINE), command=self.export).pack(side=LEFT, padx=4)

        self.summary_var = ttk.StringVar(value="")
        ttk.Label(self, textvariable=self.summary_var, font=("微軟正黑體", 10)).pack(anchor=W, pady=(0, 6))

        self.tree = ttk.Treeview(self, columns=[c[0] for c in self.COLUMNS], show="headings", bootstyle=PRIMARY)
        for key, title, width in self.COLUMNS:
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, anchor=W)
        self.tree.pack(fill=BOTH, expand=True)
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
            messagebox.showwarning("每日出勤報告", str(e), parent=self)
            return
        self.date_var.set((day + datetime.timedelta(days=delta)).strftime("%Y-%m-%d"))
        self.refresh()

    def refresh(self):
        """依輸入日期查詢並更新表格與摘要。"""
        try:
            self.report = attendance_report.build_report(self.date_var.get())
        except ValueError as e:
            messagebox.showwarning("每日出勤報告", str(e), parent=self)
            return
        except Exception as e:
            messagebox.showerror("每日出勤報告", f"查詢失敗：{e}", parent=self)
            return
        self.tree.delete(*self.tree.get_children())
        for r in self.report["rows"]:
            self.tree.insert("", "end", values=[r[c[0]] for c in self.COLUMNS])
        parts = [f"{k} {v}" for k, v in self.report["counts"].items()]
        self.summary_var.set(f"{self.report['date']}｜" + "、".join(parts or ["沒有在職員工"])
                             + f"｜異常事件 {self.report['anomaly_count']} 件")

    def export(self):
        """讓使用者選位置，把目前顯示的報告匯出成 CSV。"""
        if self.report is None:
            return
        path = filedialog.asksaveasfilename(
            parent=self, defaultextension=".csv", filetypes=[("CSV 檔", "*.csv")],
            initialfile=f"attendance_{self.report['date']}.csv")
        if not path:
            return
        try:
            count = attendance_report.export_csv(self.report, path)
        except OSError as e:
            messagebox.showerror("每日出勤報告", f"匯出失敗：{e}", parent=self)
            return
        messagebox.showinfo("每日出勤報告", f"已匯出 {count} 筆到\n{path}", parent=self)


def open_report_window(root):
    """開啟每日出勤報告視窗並回傳它。"""
    win = ttk.Toplevel(root)
    win.title("每日出勤報告")
    win.geometry("900x560")
    ReportPanel(win).pack(fill=BOTH, expand=True)
    return win

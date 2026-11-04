"""
calendar_panel.py：每日出勤報告、假日、請假與每週休息日的視窗（CustomTkinter）。

主要功能：
  - 「每日出勤報告」分頁：內嵌 report_panel（報告依下面三個分頁的資料判斷缺勤，切到此分頁時重查）
  - 「假日」分頁：新增或更新（同一天只會有一筆）、刪除所選、列表（含星期）
  - 「請假」分頁：選員工、起訖日、假別、備註後新增；列表與刪除所選
  - 「每週休息日」分頁：七個勾選，儲存後出勤報告與助理就依此判斷是否要上班
  - 所有輸入先經 calendar_logic 驗證；錯誤用 ui_theme.show_message 說明，資料庫的非預期例外只印出型別、
    畫面只顯示通用訊息，不外洩例外內容

串接模組：
  - calendar_logic：輸入驗證與顯示格式（純邏輯）
  - database_mgr：假日、請假、每週休息日的讀寫
  - account_admin：員工清單
  - report_panel：每日出勤報告面板
  - ui_theme：色彩、字型、按鈕、表格、對話框
  - backend_main：用 open_calendar_window 開啟
"""
import datetime
import sqlite3
import tkinter as tk

import customtkinter as ctk

from admin import account_admin, calendar_logic, report_panel
from shared import database_mgr as db
from shared import ui_theme

GENERIC_ERROR = "操作失敗，請稍後再試。"
REPORT_TAB = "每日出勤報告"


def _today():
    return datetime.date.today().strftime("%Y-%m-%d")


class CalendarPanel(ctk.CTkFrame):
    """假日、請假與每週休息日的設定面板。"""

    HOLIDAY_COLUMNS = (("date", "日期", 120), ("weekday", "星期", 70), ("name", "名稱", 360))
    LEAVE_COLUMNS = (("employee", "員工", 150), ("start", "起始日", 100), ("end", "結束日", 100),
                     ("days", "天數", 50), ("kind", "假別", 70), ("note", "備註", 260))

    def __init__(self, master):
        super().__init__(master, fg_color="transparent", height=1)
        self._leave_ids = {}                 # 請假表格的列 iid -> 請假紀錄 id
        self._user_choices = {}              # 下拉選單文字 -> 員工 id
        pad = ui_theme.PAD_LG
        ui_theme.section_title(self, "🗓️ 出勤報告與假日請假").pack(anchor="w", padx=pad, pady=(pad, 0))
        ui_theme.muted_label(self, "出勤報告會依假日、請假與每週休息日的設定判斷缺勤，這些日子都不算缺勤。").pack(
            anchor="w", padx=pad, pady=(2, 8))
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
        self.tabs.pack(fill="both", expand=True, padx=pad, pady=(0, pad))
        self.report_panel = report_panel.ReportPanel(self.tabs.add(REPORT_TAB), embedded=True)
        self.report_panel.pack(fill="both", expand=True)
        self._build_holiday_tab(self.tabs.add("假日"))
        self._build_leave_tab(self.tabs.add("請假"))
        self._build_rest_tab(self.tabs.add("每週休息日"))
        self.refresh_all()

    # --- 共用 ---

    def _dialog_parent(self):
        return self.winfo_toplevel()

    def _notify(self, message, kind="info", title="假日與請假"):
        ui_theme.show_message(self._dialog_parent(), title, message, kind)

    def _run(self, action):
        """執行會碰資料庫的動作；回傳是否成功。輸入不合法顯示原因；其他例外只印型別、畫面顯示通用訊息。"""
        try:
            action()
            return True
        except ValueError as e:
            self._notify(str(e), "warning")
        except (sqlite3.Error, OSError) as e:
            print(f"[假日與請假] 資料庫操作失敗：{type(e).__name__}")
            self._notify(GENERIC_ERROR, "error")
        return False

    def _field_row(self, parent, label, widget_factory, column, row=0, **grid):
        """在表單中放一個「標籤＋輸入元件」，回傳輸入元件。"""
        ui_theme.body_label(parent, label).grid(row=row, column=column, sticky="w", padx=(0, 6), pady=4)
        widget = widget_factory()
        widget.grid(row=row, column=column + 1, sticky="w", padx=(0, 16), pady=4, **grid)
        return widget

    def _on_tab_change(self):
        """切換分頁時重新載入資料；切到日報分頁時一併重查（假日、請假可能剛改過）。"""
        self.refresh_all()
        if self.tabs.get() == REPORT_TAB:
            self.report_panel.refresh()

    def refresh_all(self, *_):
        """重新載入假日、請假與每週休息日分頁的資料。"""
        self.refresh_holidays()
        self.refresh_users()
        self.refresh_leaves()
        self.refresh_rest_days()

    # --- 假日 ---

    def _build_holiday_tab(self, tab):
        form = ctk.CTkFrame(tab, fg_color="transparent", height=1)
        form.pack(fill="x", pady=(4, 6))
        self.holiday_date_var = tk.StringVar(master=self, value=_today())
        self.holiday_name_var = tk.StringVar(master=self)
        self._field_row(form, "日期", lambda: ui_theme.entry(
            form, textvariable=self.holiday_date_var, width=130), 0)
        self._field_row(form, "名稱", lambda: ui_theme.entry(
            form, textvariable=self.holiday_name_var, width=240), 2)
        self.holiday_add_button = ui_theme.primary_button(form, "新增／更新", self.add_holiday, width=100)
        self.holiday_add_button.grid(row=0, column=4, pady=4)
        ui_theme.muted_label(
            tab, f"日期格式 YYYY-MM-DD；名稱必填，最多 {calendar_logic.MAX_HOLIDAY_NAME} 字；同一天重複新增會更新名稱。"
        ).pack(anchor="w")
        box, self.holiday_tree = ui_theme.table(tab, self.HOLIDAY_COLUMNS, selectmode="extended", height=10)
        box.pack(fill="both", expand=True, pady=6)
        ui_theme.danger_button(tab, "刪除所選", self.remove_holidays, width=100).pack(anchor="w")

    def add_holiday(self):
        """驗證輸入後新增（或更新）假日。"""
        def save():
            date, name = calendar_logic.validate_holiday(self.holiday_date_var.get(), self.holiday_name_var.get())
            db.add_holiday(date, name)

        if self._run(save):
            self.holiday_name_var.set("")
            self.refresh_holidays()

    def remove_holidays(self):
        """確認後刪除所選的假日。"""
        dates = list(self.holiday_tree.selection())
        if not dates:
            self._notify("請先選取至少一筆假日。")
            return
        if not ui_theme.confirm(self._dialog_parent(), "確認刪除", f"確定要刪除 {len(dates)} 筆假日嗎？",
                                danger=True):
            return
        self._run(lambda: [db.remove_holiday(d) for d in dates])
        self.refresh_holidays()

    def refresh_holidays(self):
        """重新載入假日列表。"""
        try:
            rows = db.list_holidays()
        except (sqlite3.Error, ValueError) as e:
            print(f"[假日與請假] 讀取假日失敗：{type(e).__name__}")
            rows = []
        ui_theme.fill_table(
            self.holiday_tree,
            [(r["date"], calendar_logic.weekday_label(r["date"]), calendar_logic.display_text(r["name"]))
             for r in rows], iids=[r["date"] for r in rows])

    # --- 請假 ---

    def _build_leave_tab(self, tab):
        form = ctk.CTkFrame(tab, fg_color="transparent", height=1)
        form.pack(fill="x", pady=(4, 6))
        self.leave_start_var = tk.StringVar(master=self, value=_today())
        self.leave_end_var = tk.StringVar(master=self, value=_today())
        self.leave_note_var = tk.StringVar(master=self)
        self.leave_user = self._field_row(form, "員工", lambda: ui_theme.combo(form, [], width=230), 0)
        self.leave_kind = self._field_row(form, "假別", lambda: ui_theme.combo(
            form, calendar_logic.LEAVE_KINDS, width=110), 2)
        self.leave_kind.set(calendar_logic.LEAVE_KINDS[0])
        self._field_row(form, "起始日", lambda: ui_theme.entry(
            form, textvariable=self.leave_start_var, width=130), 0, row=1)
        self._field_row(form, "結束日", lambda: ui_theme.entry(
            form, textvariable=self.leave_end_var, width=130), 2, row=1)
        self._field_row(form, "備註", lambda: ui_theme.entry(
            form, textvariable=self.leave_note_var, width=230), 0, row=2)
        self.leave_add_button = ui_theme.primary_button(form, "新增請假", self.add_leave, width=100)
        self.leave_add_button.grid(row=2, column=3, columnspan=2, sticky="w", pady=4)
        ui_theme.muted_label(
            tab, f"日期格式 YYYY-MM-DD（起訖日都算）；備註選填，最多 {calendar_logic.MAX_LEAVE_NOTE} 字。"
        ).pack(anchor="w")
        box, self.leave_tree = ui_theme.table(tab, self.LEAVE_COLUMNS, selectmode="extended", height=8)
        box.pack(fill="both", expand=True, pady=6)
        ui_theme.danger_button(tab, "刪除所選", self.remove_leaves, width=100).pack(anchor="w")

    def refresh_users(self):
        """重新載入可選的員工（只列啟用中的），保留原本選的人。"""
        current = self.leave_user.get()
        choices, seen = {}, set()
        for u in account_admin.list_users():
            if not u["is_active"]:
                continue
            name, emp_id = calendar_logic.display_text(u["name"], 20), calendar_logic.display_text(u["employee_id"], 20)
            label = f"{name}（{emp_id}）"
            if label in seen:                      # 姓名與編號都重複時加上內部編號才分得出來
                label = f"{label} #{u['id']}"
            seen.add(label)
            choices[label] = u["id"]
        self._user_choices = choices
        self.leave_user.configure(values=list(choices))
        self.leave_user.set(current if current in choices else (next(iter(choices), "")))

    def add_leave(self):
        """驗證輸入後新增請假。"""
        def save():
            data = calendar_logic.validate_leave(
                self._user_choices.get(self.leave_user.get()), self.leave_start_var.get(), self.leave_end_var.get(),
                self.leave_kind.get(), self.leave_note_var.get(), self._user_choices.values())
            db.add_leave(**data)

        self.refresh_users()                       # 員工可能剛被停用或刪除，先以最新名單為準
        if self._run(save):
            self.leave_note_var.set("")
            self.refresh_leaves()

    def remove_leaves(self):
        """確認後刪除所選的請假。"""
        iids = list(self.leave_tree.selection())
        if not iids:
            self._notify("請先選取至少一筆請假紀錄。")
            return
        if not ui_theme.confirm(self._dialog_parent(), "確認刪除", f"確定要刪除 {len(iids)} 筆請假紀錄嗎？",
                                danger=True):
            return
        self._run(lambda: [db.remove_leave(self._leave_ids[i]) for i in iids if i in self._leave_ids])
        self.refresh_leaves()

    def refresh_leaves(self):
        """重新載入請假列表。"""
        try:
            leaves = db.list_leaves()
        except (sqlite3.Error, ValueError) as e:
            print(f"[假日與請假] 讀取請假失敗：{type(e).__name__}")
            leaves = []
        names = {u["id"]: calendar_logic.display_text(u["name"], 20) for u in account_admin.list_users()}
        self._leave_ids = {str(i): lv["id"] for i, lv in enumerate(leaves)}
        ui_theme.fill_table(
            self.leave_tree,
            [(names.get(lv["user_id"], f"（已刪除員工 #{lv['user_id']}）"), lv["start_date"], lv["end_date"],
              calendar_logic.leave_days(lv["start_date"], lv["end_date"]), calendar_logic.display_text(lv["kind"], 10),
              calendar_logic.display_text(lv["note"])) for lv in leaves],
            iids=list(range(len(leaves))))

    # --- 每週休息日 ---

    def _build_rest_tab(self, tab):
        ui_theme.body_label(tab, "勾選每週固定休息的日子（預設週六、週日）：").pack(anchor="w", pady=(8, 6))
        row = ctk.CTkFrame(tab, fg_color="transparent", height=1)
        row.pack(anchor="w", pady=6)
        self.rest_vars = []
        for i, name in enumerate(calendar_logic.WEEKDAY_NAMES):
            var = tk.BooleanVar(master=self, value=False)
            ctk.CTkCheckBox(row, text=name, variable=var, font=ui_theme.font(), text_color=ui_theme.pair("text"),
                            fg_color=ui_theme.pair("primary"), hover_color=ui_theme.pair("primary_hover"),
                            border_color=ui_theme.pair("border"), width=80).grid(row=0, column=i, padx=(0, 10))
            self.rest_vars.append(var)
        self.rest_status = ui_theme.muted_label(tab, "")
        self.rest_status.pack(anchor="w", pady=(8, 8))
        ui_theme.primary_button(tab, "儲存", self.save_rest_days, width=100).pack(anchor="w")

    def save_rest_days(self):
        """儲存每週休息日。"""
        def save():
            db.set_rest_weekdays(calendar_logic.rest_days_from_flags(v.get() for v in self.rest_vars))

        if self._run(save):
            self.refresh_rest_days()
            self._notify("已儲存每週休息日。")

    def refresh_rest_days(self):
        """從資料庫載入目前的每週休息日。"""
        try:
            days = db.get_rest_weekdays()
        except (sqlite3.Error, ValueError) as e:
            print(f"[假日與請假] 讀取每週休息日失敗：{type(e).__name__}")
            return
        for var, on in zip(self.rest_vars, calendar_logic.rest_flags(days), strict=True):
            var.set(on)
        self.rest_status.configure(text=f"目前設定：{calendar_logic.describe_rest_days(days)}")


def open_calendar_window(root):
    """開啟出勤報告與假日請假視窗並回傳它。"""
    win = ctk.CTkToplevel(root)
    win.title("出勤報告與假日請假")
    win.geometry("900x700")
    ui_theme.style_window(win)
    CalendarPanel(win).pack(fill="both", expand=True)
    return win

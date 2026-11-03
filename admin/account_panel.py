"""
account_panel.py：員工帳號管理的畫面（CustomTkinter 外框，表格用調過樣式的 ttk.Treeview）。

主要功能：
  - 列出員工（編號、姓名、性別、角色、狀態、最新打卡），可多選
  - 停用、啟用、刪除（刪除前確認）、設為管理者、取消管理者；最後一位管理者不能被取消、停用或刪除
    （由 account_admin 把關，被拒絕的原因會顯示）
  - 操作結果用 ui_theme 對話框說明，不顯示例外內容

串接模組：
  - account_admin：帳號資料庫操作與規則
  - ui_theme：色彩、字型、按鈕、表格、對話框
  - backend_main：用 open_account_window 開啟，並傳入登入者名稱作為操作者
"""
import customtkinter as ctk

from admin import account_admin
from shared import database_mgr as db
from shared import ui_theme


class AccountPanel(ctk.CTkFrame):
    """員工帳號管理面板。"""

    COLUMNS = (("emp_id", "員工編號", 100), ("name", "姓名", 120), ("gender", "性別", 60, "center"),
               ("role", "角色", 90, "center"), ("active", "狀態", 100, "center"),
               ("last_status", "最新打卡狀態", 140, "center"), ("last_time", "最後刷卡時間", 170))

    def __init__(self, master, operator=None):
        super().__init__(master, fg_color="transparent", height=1)
        self.operator = operator
        self._names = {}                      # 員工 id -> 姓名（不從表格讀回，避免數字開頭的姓名被轉成數字）
        pad = ui_theme.PAD_LG
        ui_theme.section_title(self, "👤 員工帳號管理").pack(anchor="w", padx=pad, pady=(pad, 4))
        ui_theme.muted_label(
            self, "停用的帳號會立刻無法再刷臉打卡；刪除不會影響已經留存的打卡歷史紀錄。"
                  "管理者可以用人臉登入這個後台，系統一定要保留至少一位管理者。",
            wraplength=900, justify="left").pack(anchor="w", padx=pad, pady=(0, 10))

        box, self.tree = ui_theme.table(self, self.COLUMNS, selectmode="extended", height=12)
        box.pack(fill="both", expand=True, padx=pad, pady=(0, 10))

        bar = ctk.CTkFrame(self, fg_color="transparent", height=1)
        bar.pack(fill="x", padx=pad, pady=(0, pad))
        actions = (
            ("重新整理", self.refresh, ui_theme.secondary_button),
            ("停用所選", self.deactivate, ui_theme.secondary_button),
            ("啟用所選", self.activate, ui_theme.secondary_button),
            ("刪除所選", self.delete, ui_theme.danger_button),
            ("設為管理者", lambda: self.set_role(db.ROLE_ADMIN), ui_theme.primary_button),
            ("取消管理者", lambda: self.set_role(db.ROLE_EMPLOYEE), ui_theme.secondary_button),
        )
        for col, (text, command, make) in enumerate(actions):     # 按鈕等寬平均分布，左右對稱
            bar.grid_columnconfigure(col, weight=1, uniform="actions")
            make(bar, text, command).grid(row=0, column=col, sticky="ew", padx=4)
        self.refresh()

    def _parent(self):
        return self.winfo_toplevel()

    def refresh(self):
        """重新載入員工清單。"""
        users = account_admin.list_users()
        self._names = {u["id"]: u["name"] for u in users}
        ui_theme.fill_table(
            self.tree,
            [(u["employee_id"], u["name"], u["gender"], u["role"], "● 啟用中" if u["is_active"] else "○ 已停用",
              u["last_status"], u["last_time"]) for u in users],
            iids=[u["id"] for u in users])

    def _require_selection(self):
        """取得所選員工 id，沒選就提示。"""
        ids = [int(iid) for iid in self.tree.selection()]
        if not ids:
            ui_theme.show_message(self._parent(), "提示", "請先選取至少一筆員工資料。")
        return ids

    def _apply(self, active):
        """停用或啟用所選員工，並把被拒絕的原因（例如最後一位管理者）一起說明。"""
        ids = self._require_selection()
        problems = []
        for uid in ids:
            result = account_admin.set_active(uid, active)
            if isinstance(result, tuple) and not result[0]:
                problems.append(f"{self._names.get(uid, uid)}：{result[1]}")
        if ids:
            self.refresh()
        if problems:
            ui_theme.show_message(self._parent(), "無法設定", "\n".join(problems), "warning")

    def deactivate(self):
        """停用所選員工。"""
        self._apply(False)

    def activate(self):
        """啟用所選員工。"""
        self._apply(True)

    def delete(self):
        """確認後刪除所選員工。"""
        ids = self._require_selection()
        if not ids:
            return
        names = [str(self._names.get(uid, uid)) for uid in ids]
        if not ui_theme.confirm(
                self._parent(), "確認刪除",
                "確定要刪除以下員工帳號嗎？（打卡歷史紀錄不會被刪除，只是這個人之後沒辦法再刷臉打卡）\n\n"
                + "、".join(names),
                danger=True):
            return
        problems = []
        for uid in ids:
            result = account_admin.delete_user(uid)
            if isinstance(result, tuple) and not result[0]:
                problems.append(f"{self._names.get(uid, uid)}：{result[1]}")
        self.refresh()
        if problems:
            ui_theme.show_message(self._parent(), "無法刪除", "\n".join(problems), "warning")

    def set_role(self, role):
        """把所選員工設為管理者或一般員工。"""
        ids = self._require_selection()
        problems = []
        for uid in ids:
            ok, msg = account_admin.set_role(uid, role, operator=self.operator)
            if not ok:
                problems.append(f"{self._names.get(uid, uid)}：{msg}")
        if ids:
            self.refresh()
        if problems:
            ui_theme.show_message(self._parent(), "無法設定", "\n".join(problems), "warning")


def open_account_window(root, operator=None):
    """開啟員工帳號管理視窗並回傳它。"""
    win = ctk.CTkToplevel(root)
    win.title("員工帳號管理")
    win.geometry("1000x600")
    ui_theme.style_window(win)
    AccountPanel(win, operator).pack(fill="both", expand=True)
    return win

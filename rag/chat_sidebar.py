"""
chat_sidebar.py：出缺勤查詢視窗左邊的側欄（Smart_Query 的品牌、新對話按鈕、本次提問清單）。

主要功能：
  - Sidebar：品牌圖示與名稱、「＋ 新對話」按鈕、本次對話問過的問題清單（只存在畫面上，關視窗就沒有；
    要留存審查的對話紀錄由後台另外記，不在這裡）、底部說明資料來源
  - add_question／clear：新增一則提問、清空清單；清單最多留 30 則，太舊的自動移除
  - 只放畫面，不放邏輯；圖示用 shared.svg_icon，顏色取自 shared.ui_theme

串接模組：
  - rag.chat_window：建立側欄、在每次提問時 add_question、新對話時 clear
"""
import customtkinter as ctk

from shared import svg_icon, ui_theme

MAX_ITEMS = 30
SIDEBAR_WIDTH = 230
ITEM_MAX_CHARS = 26


def short(text):
    """側欄清單用的短標題：換行換成空白、太長加刪節號。"""
    one_line = " ".join(str(text).split())
    return one_line if len(one_line) <= ITEM_MAX_CHARS else one_line[:ITEM_MAX_CHARS - 1] + "…"


class Sidebar(ctk.CTkFrame):
    """左側欄：品牌、新對話、本次提問。"""

    def __init__(self, master, on_new):
        super().__init__(master, width=SIDEBAR_WIDTH, corner_radius=0, fg_color=ui_theme.pair("card"))
        self.pack_propagate(False)
        self.grid_propagate(False)
        brand = ctk.CTkFrame(self, fg_color="transparent")
        brand.pack(fill="x", padx=16, pady=(18, 4))
        ctk.CTkLabel(brand, text="", width=40, height=40, corner_radius=12, fg_color=ui_theme.pair("primary"),
                     image=svg_icon.ctk_icon("smart_query", 24, ui_theme.pair("on_primary"), brand)).pack(side="left")
        names = ctk.CTkFrame(brand, fg_color="transparent")
        names.pack(side="left", padx=10)
        ctk.CTkLabel(names, text="Smart_Query", font=ui_theme.font(16, bold=True), anchor="w").pack(anchor="w")
        ui_theme.muted_label(names, "出缺勤查詢").pack(anchor="w")
        self.new_btn = ui_theme.primary_button(self, "＋  新對話", on_new, height=40)
        self.new_btn.configure(font=ui_theme.font(14, bold=True), corner_radius=20)
        self.new_btn.pack(fill="x", padx=16, pady=(14, 10))
        ui_theme.muted_label(self, "本次提問").pack(anchor="w", padx=18, pady=(6, 2))
        self.list = ctk.CTkScrollableFrame(self, fg_color="transparent", corner_radius=0)
        self.list.pack(fill="both", expand=True, padx=6, pady=(0, 4))
        self.empty = ui_theme.muted_label(self.list, "還沒有提問", wraplength=SIDEBAR_WIDTH - 40, justify="left")
        self.empty.pack(anchor="w", padx=10, pady=4)
        self._items = []
        ui_theme.muted_label(self, "只查後台資料庫的考勤與異常；文件問答在前台", wraplength=SIDEBAR_WIDTH - 32,
                             justify="left").pack(anchor="w", padx=16, pady=(0, 14))

    def add_question(self, text):
        """在清單最下面加一則提問；超過上限就移除最舊的。"""
        if self.empty is not None:
            self.empty.destroy()
            self.empty = None
        item = ctk.CTkLabel(self.list, text=" " + short(text),
                            image=svg_icon.ctk_icon("smart_chat", 16, ui_theme.pair("text_muted"), self.list),
                            compound="left",
                            anchor="w", justify="left", padx=10, font=ui_theme.font(13),
                            fg_color=ui_theme.pair("secondary"), corner_radius=8, height=32)
        item.pack(fill="x", padx=4, pady=3)
        self._items.append(item)
        while len(self._items) > MAX_ITEMS:
            self._items.pop(0).destroy()

    def clear(self):
        """清空清單。"""
        for item in self._items:
            item.destroy()
        self._items = []
        if self.empty is None:
            self.empty = ui_theme.muted_label(self.list, "還沒有提問", wraplength=SIDEBAR_WIDTH - 40, justify="left")
            self.empty.pack(anchor="w", padx=10, pady=4)

    def count(self):
        """目前清單裡的提問數。"""
        return len(self._items)

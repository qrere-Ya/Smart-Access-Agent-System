"""
chat_status_bar.py：聊天視窗的標題列（標題、狀態文字、字級、設定按鈕）與底部提示。

主要功能：
  - StatusBar：標題列框架，狀態文字用 set_status() 更新；右邊有字級（小／中／大）與設定，載入失敗時出現「重新載入」
  - FONT_SIZES：字級選項與對應的對話文字大小；on_font 收到選到的文字大小
  - build_footer_hint()：輸入框下方的操作提示
  - 只放畫面，不放邏輯；顏色字型取自 shared.ui_theme；「新對話」按鈕在左側欄（rag.chat_sidebar）

串接模組：
  - shared.ui_theme：按鈕與色彩；rag.chat_window：傳入動作，並在事件迴圈更新狀態
"""
import customtkinter as ctk

from shared import ui_theme

META_COLOR = ui_theme.pair("text_muted")
FONT_SIZES = (("小", 13), ("中", 15), ("大", 18))
DEFAULT_FONT_LABEL = "中"


class StatusBar(ctk.CTkFrame):
    """標題列：左邊標題與狀態，右邊由右至左是設定、字級切換（文件問答已移到前台，這裡沒有文件按鈕）。"""

    def __init__(self, master, on_settings, on_font=None):
        super().__init__(master, corner_radius=0, height=56, fg_color=ui_theme.pair("card"))
        ctk.CTkLabel(
            self, text="Smart_Query", font=ui_theme.font(ui_theme.FONT_SIZE_TITLE, bold=True)).pack(
            side="left", padx=(20, 8), pady=12)
        self.status = ctk.CTkLabel(self, text="啟動中…", text_color=META_COLOR,
                                   font=ui_theme.font(ui_theme.FONT_SIZE_SMALL))
        self.status.pack(side="left", padx=8)
        self.settings_btn = ui_theme.secondary_button(self, "⚙", on_settings, width=40)
        self.settings_btn.pack(side="right", padx=(6, 16))
        self.font_switch = ctk.CTkSegmentedButton(
            self, values=[label for label, _size in FONT_SIZES], width=120,
            command=lambda label: on_font(dict(FONT_SIZES)[label]) if on_font else None)
        self.font_switch.set(DEFAULT_FONT_LABEL)
        self.font_switch.pack(side="right", padx=6)
        ui_theme.muted_label(self, "字級").pack(side="right")
        self.retry_btn = ui_theme.primary_button(self, "🔁 重新載入", None, width=100)

    def set_status(self, text):
        """更新狀態文字。"""
        self.status.configure(text=text)

    def show_retry(self, command):
        """載入失敗時顯示「重新載入」按鈕（不用關掉視窗重開）。"""
        self.retry_btn.configure(command=command, state="normal")
        self.retry_btn.pack(side="right", padx=6, before=self.settings_btn)

    def hide_retry(self):
        """隱藏「重新載入」按鈕。"""
        self.retry_btn.pack_forget()


def build_footer_hint(master):
    """輸入框下方的操作提示與免責聲明。"""
    return ctk.CTkLabel(master, text="Enter 送出，Shift+Enter 換行　·　回答僅供參考，請以資料庫為準，文件問答請到前台",
                        text_color=META_COLOR, font=ui_theme.font(11))

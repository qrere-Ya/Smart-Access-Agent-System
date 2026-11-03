"""
chat_status_bar.py：聊天視窗的標題列（標題、狀態文字、新對話／重新整理文件／設定按鈕）與底部提示。

主要功能：
  - StatusBar：標題列框架，狀態文字用 set_status() 更新；按鈕可用屬性取得來切換啟用狀態
  - build_footer_hint()：輸入框下方的操作提示
  - 只放畫面，不放邏輯；顏色字型取自 shared.ui_theme

串接模組：
  - shared.ui_theme：按鈕與色彩；rag.chat_window：傳入三個按鈕的動作，並在事件迴圈更新狀態
"""
import customtkinter as ctk

from shared import ui_theme

META_COLOR = ui_theme.pair("text_muted")


class StatusBar(ctk.CTkFrame):
    """標題列：左邊標題，右邊由右至左是設定、文件、新對話與狀態文字。"""

    def __init__(self, master, on_new, on_refresh, on_settings):
        super().__init__(master, corner_radius=0, height=56, fg_color=ui_theme.pair("card"))
        ctk.CTkLabel(self, text="🤖  管理者助理", font=ui_theme.font(ui_theme.FONT_SIZE_TITLE, bold=True)).pack(
            side="left", padx=18, pady=12)
        self.settings_btn = ui_theme.secondary_button(self, "⚙️", on_settings, width=40)
        self.settings_btn.pack(side="right", padx=(6, 14))
        self.docs_btn = ui_theme.secondary_button(self, "🔄 文件", on_refresh, width=80, state="disabled")
        self.docs_btn.pack(side="right", padx=6)
        self.new_btn = ui_theme.secondary_button(self, "＋ 新對話", on_new, width=90)
        self.new_btn.pack(side="right", padx=6)
        self.status = ctk.CTkLabel(self, text="啟動中…", text_color=META_COLOR,
                                   font=ui_theme.font(ui_theme.FONT_SIZE_SMALL))
        self.status.pack(side="right", padx=14)
        self.retry_btn = ui_theme.primary_button(self, "🔁 重新載入", None, width=100)

    def set_status(self, text):
        """更新狀態文字。"""
        self.status.configure(text=text)

    def show_retry(self, command):
        """載入失敗時顯示「重新載入」按鈕（不用關掉視窗重開）。"""
        self.retry_btn.configure(command=command, state="normal")
        self.retry_btn.pack(side="right", padx=6, before=self.status)

    def hide_retry(self):
        """隱藏「重新載入」按鈕。"""
        self.retry_btn.pack_forget()


def build_footer_hint(master):
    """輸入框下方的操作提示與免責聲明。"""
    return ctk.CTkLabel(master, text="Enter 送出，Shift+Enter 換行　·　回答僅供參考，請以原始文件與資料庫為準",
                        text_color=META_COLOR, font=ui_theme.font(11))

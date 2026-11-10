"""
console_panel.py：後台內建的小終端機文字框元件（CustomTkinter）。

主要功能：
  - build_console_panel：建立深色、等寬字型、唯讀的文字框（終端機固定用深色，不隨亮暗外觀改變）
  - append_line：安全地加入一行文字，最多保留最後 500 行

串接模組：
  - ui_theme：卡片、標題、字型
  - backend_main：顯示 RAG、人臉辨識、註冊伺服器三組服務的即時輸出
"""
import customtkinter as ctk

from shared import ui_theme

CONSOLE_BG = "#1e1e1e"
CONSOLE_FG = "#d0d0d0"
MAX_LINES = 500


def build_console_panel(parent, title):
    """在 parent 裡建立小終端機文字框並回傳。"""
    frame = ui_theme.card(parent)
    frame.pack(fill="both", expand=True, padx=(0, 15), pady=15)
    ui_theme.section_title(frame, f"🖥️ {title}").pack(anchor="w", padx=ui_theme.PAD_MD, pady=(ui_theme.PAD_MD, 6))
    console = ctk.CTkTextbox(frame, font=ui_theme.mono_font(12), wrap="word", fg_color=CONSOLE_BG,
                             text_color=CONSOLE_FG, corner_radius=ui_theme.RADIUS_BUTTON)
    console.pack(fill="both", expand=True, padx=ui_theme.PAD_MD, pady=(0, ui_theme.PAD_MD))
    append_line(console, "（服務尚未啟動，這裡會即時顯示後台輸出）")
    return console


def _set_state(widget, state):
    """切換文字框可編輯狀態（假的測試物件沒有 configure 就略過）。"""
    configure = getattr(widget, "configure", None)
    if configure is not None:
        configure(state=state)


def append_line(widget, line):
    """加入一行文字；widget 為 None 或視窗已關閉就略過，超過 500 行時刪除最舊的。"""
    if widget is None:
        return
    try:
        if not widget.winfo_exists():
            return
        _set_state(widget, "normal")
        widget.insert("end", line + "\n")
        total_lines = int(widget.index("end-1c").split(".")[0])
        if total_lines > MAX_LINES:
            widget.delete("1.0", f"{total_lines - MAX_LINES}.0")
        widget.see("end")
        _set_state(widget, "disabled")
    except Exception:  # noqa: S110
        pass  # 視窗關閉瞬間可能還有殘留回呼，略過即可

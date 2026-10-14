"""
console_panel.py：後台內建的小終端機文字框元件。

主要功能：
  - build_console_panel：建立深色、等寬字型的文字框
  - append_line：安全地加入一行文字，最多保留最後 500 行

串接模組：
  - backend_main：顯示 RAG、人臉辨識、註冊伺服器三組服務的即時輸出
"""
import ttkbootstrap as ttk
from ttkbootstrap.constants import (
    BOTH, DARK,
)
from ttkbootstrap.widgets.scrolled import ScrolledText


def build_console_panel(parent, title):
    """在 parent 裡建立小終端機文字框並回傳（顏色與字型要在建立時傳入，事後 configure 對內部文字框無效）。"""
    frame = ttk.Labelframe(parent, text=f"🖥️ {title}", padding=8, bootstyle=DARK)
    frame.pack(fill=BOTH, expand=True, padx=(0, 15), pady=15)

    console = ScrolledText(
        frame, autohide=True, font=("Consolas", 9), wrap="word",
        background="#1e1e1e", foreground="#d0d0d0", insertbackground="#d0d0d0",
    )
    console.pack(fill=BOTH, expand=True)
    append_line(console, "（服務尚未啟動，這裡會即時顯示後台輸出）")
    return console


def append_line(widget, line):
    """加入一行文字；widget 為 None 或視窗已關閉就略過，超過 500 行時刪除最舊的。"""
    if widget is None:
        return
    try:
        if not widget.winfo_exists():
            return
        widget.insert("end", line + "\n")
        total_lines = int(widget.index("end-1c").split(".")[0])
        if total_lines > 500:
            widget.delete("1.0", f"{total_lines - 500}.0")
        widget.see("end")
    except Exception:  # noqa: S110
        pass  # 視窗關閉瞬間可能還有殘留回呼，略過即可

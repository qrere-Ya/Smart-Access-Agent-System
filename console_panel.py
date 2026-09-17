"""
內建終端機小框框元件。

【2026-09-12，P1 架構重構抽出】原本這幾個函式是 backend_main.py 的 BackendManagerApp
方法，跟視窗版面混在一起。這裡抽出來變成獨立、不依賴 self 的小工具，backend_main.py
呼叫時自己把回傳的文字框存到需要的屬性上（例如 self.rag_console）。
"""
import ttkbootstrap as ttk
from ttkbootstrap.constants import *
from ttkbootstrap.widgets.scrolled import ScrolledText


def build_console_panel(parent, title):
    """
    在 parent 容器裡建立一個小終端機文字框，回傳這個文字框元件本身。

    顏色/字型直接在建立的時候當參數傳進去（而不是事後呼叫 .configure()）：ScrolledText
    這個元件本身是一個 Frame，「包著」真正的 Text 文字框，它只有把 insert/delete/
    index/see 這幾個方法轉接到裡面的 Text，並沒有轉接 configure，所以事後呼叫
    .configure() 改的其實是外層 Frame、不是文字框，不會有效果（改用建構子參數就沒有
    這個問題，一次到位）。
    """
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
    """
    安全地把一行文字加進終端機小框框：
    - widget 可能是 None（例如視窗根本還沒打開過），也可能已經隨著視窗被使用者關掉、
      銷毀了，這裡先確認它還「活著」才寫入，不然會讓整支程式當掉。
    - 最多只留最後 500 行，超過就把最舊的丟掉，避免視窗開著開著記憶體一直長大。
    """
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
    except Exception:
        pass  # 視窗關閉的瞬間可能還有殘留的回呼在跑，安靜略過就好，不要讓程式當掉

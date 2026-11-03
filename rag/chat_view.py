"""
chat_view.py：聊天視窗的對話區與輸入列元件（訊息氣泡、歡迎畫面、提示列、輸入框與送出鈕）。

主要功能：
  - Bubble：一則訊息，圓角底色加可選取複製的文字，高度跟著換行後的行數伸縮，出處等灰字行另標色
  - build_welcome()：歡迎畫面與範例問題按鈕；bubble_width()：依對話區寬度算氣泡寬度
  - make_note()：對話區中央的灰色提示列；build_input_row()：輸入框加送出鈕
  - 只放畫面，不放問答邏輯；顏色字型取自 shared.ui_theme

串接模組：
  - shared.ui_theme：色彩 token 與字型；rag.chat_window：組裝這些元件並管理對話狀態
"""
import tkinter.font as tkfont

import customtkinter as ctk

from shared import ui_theme

APP_TITLE = "管理者助理"
FONT_FAMILY = ui_theme.FONT_FAMILY
SAMPLE_QUESTIONS = ["特別休假的規定是什麼？", "今天誰遲到？", "最近有沒有尾隨事件？", "這週的出勤狀況",
                    "颱風天公司要放假嗎？", "加班費怎麼計算？"]
META_PREFIXES = ("📎", "🗂️", "ℹ️", "⚠️")
META_BLOCK_PREFIXES = ("📎", "🗂️")
USER_COLOR = ui_theme.pair("primary")
BOT_COLOR = ui_theme.pair("secondary")
META_COLOR = ui_theme.pair("text_muted")


class Bubble(ctk.CTkFrame):
    """一則訊息：圓角底色加上可選取複製的文字，高度跟著內容自動伸縮。"""

    def __init__(self, master, role, text="", width=560):
        super().__init__(master, corner_radius=16, fg_color=USER_COLOR if role == "user" else BOT_COLOR)
        self.role = role
        self.box = ctk.CTkTextbox(self, width=width, height=30, wrap="word", activate_scrollbars=False,
                                  fg_color="transparent", border_spacing=8, font=ui_theme.font(14),
                                  text_color=ui_theme.pair("on_primary" if role == "user" else "on_secondary"))
        self.box.pack(padx=6, pady=4)
        self.box._textbox.tag_config("meta", foreground=ui_theme.color("text_muted"))
        self._last_width = 0
        # 視窗真正排版出來、或寬度改變後，換行的行數會變，要重算高度（寬度沒變時略過，避免高度改變又觸發自己）
        self.box._textbox.bind("<Configure>", self._on_box_configure, add="+")
        if text:
            self.set_text(text)

    def _on_box_configure(self, event):
        """文字框寬度改變時重算高度。"""
        if event.width != self._last_width:
            self._last_width = event.width
            self.after_idle(self.fit)

    def get_text(self):
        """取出目前的全文。"""
        return self.box.get("1.0", "end").rstrip("\n")

    def set_text(self, text):
        """換掉全文。"""
        self.box.configure(state="normal")
        self.box.delete("1.0", "end")
        self.box.insert("end", text)
        self._finish_edit()

    def append(self, text):
        """在結尾追加文字（串流顯示用）。"""
        self.box.configure(state="normal")
        self.box.insert("end", text)
        self._finish_edit()

    def _finish_edit(self):
        """編輯完成：鎖成唯讀（仍可選取複製）並重算高度。"""
        self.box.configure(state="disabled")
        self.fit()

    def fit(self):
        """依目前寬度換行後的實際行數調整高度。"""
        text = self.box._textbox
        text.update_idletasks()
        lines = text.count("1.0", "end", "displaylines")
        lines = int(lines[0]) if lines else 1
        font = tkfont.Font(font=text.cget("font"))
        scaling = ctk.ScalingTracker.get_widget_scaling(self.box)
        height = (lines * font.metrics("linespace") + 2 * 8 + 6) / scaling
        self.box.configure(height=max(34, int(height)))

    def set_width(self, width):
        """改變文字框寬度並重算高度。"""
        self.box.configure(width=width)
        self.fit()

    def mark_meta_lines(self):
        """出處、證據照片路徑、提醒這幾行用灰色，讓正文更突出。"""
        text = self.box._textbox
        last = int(text.index("end-1c").split(".")[0])
        in_meta_block = False
        for line in range(1, last + 1):
            content = text.get(f"{line}.0", f"{line}.end")
            if content.startswith(META_PREFIXES):
                in_meta_block = content.startswith(META_BLOCK_PREFIXES)  # 出處、證據照片：後面幾行也是同一塊
            elif not content.strip():
                in_meta_block = False
            if in_meta_block or content.startswith(META_PREFIXES):
                text.tag_add("meta", f"{line}.0", f"{line}.end")


def bubble_width(chat):
    """依對話區（捲動容器）目前寬度算氣泡寬度；還沒排版出來時用預設值。"""
    canvas_width = chat._parent_canvas.winfo_width()
    return max(300, int(canvas_width * 0.72) - 40) if canvas_width > 1 else 560


def build_welcome(chat, on_pick):
    """建立歡迎畫面；回傳 (歡迎框, 範例問題按鈕清單)，按下範例問題會呼叫 on_pick(問題)。"""
    welcome = ctk.CTkFrame(chat, fg_color="transparent")
    welcome.pack(pady=(48, 8))
    ctk.CTkLabel(welcome, text="有什麼想查的？", font=ui_theme.font(ui_theme.FONT_SIZE_HERO, bold=True)).pack()
    ctk.CTkLabel(welcome, text="問公司文件與法規，或查考勤與異常紀錄", text_color=META_COLOR,
                 font=ui_theme.font(14)).pack(pady=(4, 18))
    grid = ctk.CTkFrame(welcome, fg_color="transparent")
    grid.pack()
    chips = []
    for i, question in enumerate(SAMPLE_QUESTIONS):
        chip = ctk.CTkButton(grid, text=question, width=250, height=40, corner_radius=20, fg_color=BOT_COLOR,
                             text_color=ui_theme.pair("on_secondary"), hover_color=ui_theme.pair("secondary_hover"),
                             font=ui_theme.font(), state="disabled", command=lambda q=question: on_pick(q))
        chip.grid(row=i // 2, column=i % 2, padx=6, pady=6)
        chips.append(chip)
    return welcome, chips


def make_note(chat, text):
    """在對話區放一則灰色提示（降級通知、文件讀取失敗、啟動失敗等）。"""
    label = ctk.CTkLabel(chat, text=text, text_color=META_COLOR, font=ui_theme.font(ui_theme.FONT_SIZE_SMALL),
                         wraplength=620)
    label.pack(pady=6)
    return label


def build_input_row(master, on_enter, on_send):
    """建立輸入框與送出鈕；回傳 (外框, 輸入框, 送出鈕)。"""
    bottom = ctk.CTkFrame(master, fg_color="transparent")
    bottom.grid_columnconfigure(0, weight=1)
    box = ctk.CTkTextbox(bottom, height=64, wrap="word", font=ui_theme.font(ui_theme.FONT_SIZE_INPUT),
                         border_width=1, border_spacing=8)
    box.grid(row=0, column=0, sticky="ew")
    box.bind("<Return>", on_enter)
    send_btn = ui_theme.primary_button(bottom, "送出", on_send, width=84, height=64, state="disabled")
    send_btn.configure(font=ui_theme.font(15, bold=True))
    send_btn.grid(row=0, column=1, padx=(10, 0))
    return bottom, box, send_btn

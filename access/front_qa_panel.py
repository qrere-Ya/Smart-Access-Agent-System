"""
front_qa_panel.py：前台儀表板的問答面板（Smart_Chat 一般對話與 Smart_RAG 知識庫問答共用同一套畫面與行為）。

主要功能：
  - QAPanel：上方一行說明、狀態文字、中間回答（純文字、只能看；還沒問時顯示歡迎畫面）、下方圓角輸入列
    （實體鍵盤輸入，Enter 送出）；每個面板有自己的對話控制（access.front_chat.FrontChat／FrontTalk）
  - send／clear／render／focus：送出、清除（閒置 60 秒或離開面板時）、依控制器狀態重畫、把鍵盤焦點放進輸入框
  - 回答只當純文字顯示；有來源文件名稱時（Smart_RAG）列在回答後面，Smart_Chat 沒有來源
  - 前台不保存任何對話：面板只在記憶體，離開或閒置就清掉

串接模組：
  - access.front_dashboard：建立兩個面板、呼叫 render／clear／focus 並把操作時間算進閒置；access.front_chat：對話控制
  - shared.ui_theme、shared.svg_icon：外觀與圖示
"""
import tkinter as tk

import customtkinter as ctk

from access import front_chat
from shared import svg_icon, tech_art, ui_theme


class QAPanel:
    def __init__(self, dash, panel, controller, spec, font):
        """dash 是儀表板（用它的 _hide／_show／_changed／_clock）；spec 是這個面板的文字與圖示。"""
        self.dash, self.panel, self.controller, self.spec, self._font = dash, panel, controller, spec, font
        self.question = ""
        self.touched = dash._clock()
        panel.grid_columnconfigure(0, weight=1)
        panel.grid_rowconfigure(2, weight=1)
        head = ctk.CTkFrame(panel, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=28, pady=(18, 0))
        ctk.CTkLabel(head, text=spec["tagline"], font=font(20), text_color=ui_theme.pair("text_muted")).pack(
            side="left")
        self.status = ctk.CTkLabel(panel, text="", font=font(26), text_color=ui_theme.pair("text_muted"),
                                   anchor="w", justify="left", wraplength=1500)
        self.status.grid(row=1, column=0, sticky="ew", padx=28, pady=(8, 2))
        self.answer = ctk.CTkTextbox(panel, wrap="word", font=font(28), fg_color=ui_theme.pair("bg"),
                                     text_color=ui_theme.pair("text"), border_width=1, corner_radius=20,
                                     border_color=ui_theme.pair("border"))
        self.answer.grid(row=2, column=0, sticky="nsew", padx=28, pady=6)
        self.answer.configure(state="disabled")
        self.welcome = ctk.CTkFrame(panel, fg_color="transparent")
        self.welcome.grid(row=2, column=0, sticky="nsew", padx=28, pady=6)
        center = ctk.CTkFrame(self.welcome, fg_color="transparent")
        center.place(relx=0.5, rely=0.45, anchor="center")
        ctk.CTkLabel(center, text="", image=tech_art.badge(spec["icon"], 110, spec["accent"], center)).pack()
        ctk.CTkLabel(center, text=spec["welcome"], font=font(38, bold=True), text_color=ui_theme.pair("text")).pack(
            pady=(18, 4))
        ctk.CTkLabel(center, text=spec["hint"], font=font(24), text_color=ui_theme.pair("text_muted")).pack()
        self.input_row = ctk.CTkFrame(panel, corner_radius=44, fg_color=ui_theme.pair("card"), border_width=1,
                                      border_color=ui_theme.pair("border"))
        self.input_row.grid(row=3, column=0, sticky="ew", padx=28, pady=(6, 18))
        self.input_row.grid_columnconfigure(0, weight=1)
        self.entry = ctk.CTkEntry(self.input_row, font=font(28), height=64, fg_color="transparent",
                                  text_color=ui_theme.pair("text"), border_width=0,
                                  placeholder_text="用鍵盤輸入問題，按 Enter 送出")
        self.entry.grid(row=0, column=0, sticky="ew", padx=(26, 8), pady=8)
        send_icon = svg_icon.ctk_icon("smart_send", 34, ui_theme.pair("on_primary"), self.input_row)
        self.send_button = ui_theme.primary_button(self.input_row, "", self.send, width=68, height=68, image=send_icon)
        self.send_button.configure(corner_radius=34)
        self.send_button.grid(row=0, column=1, padx=(0, 12), pady=8)
        self.entry.bind("<Return>", ui_theme.event_only(lambda _e: self.send()))
        self.entry.bind("<Key>", ui_theme.event_only(lambda _e: self.on_key()))
        self.entry.bind("<KeyRelease>", ui_theme.event_only(lambda _e: self._limit()))

    def on_key(self):
        """打字也算有人在操作（不然輸入一半就被閒置清掉）。"""
        self.touched = self.dash._touched = self.dash._clock()

    def _limit(self):
        """問題長度上限（超過的部分直接截掉，不送出被後台拒絕）。"""
        limit = front_chat.lp.MAX_CHAT_QUESTION
        if len(self.entry.get()) > limit:
            self.entry.delete(limit, "end")

    def send(self):
        """送出輸入框裡的問題；處理中、後台沒開放或空白時什麼都不做。"""
        question = self.entry.get().strip()
        if self.controller.submit(question):
            self.question = question
            self.entry.delete(0, "end")
            self.touched = self.dash._touched = self.dash._clock()
        self.render()

    def clear(self):
        """清掉輸入、問題與回答，並放棄還在進行的那一題；前台不保存任何對話。"""
        self.controller.clear()
        self.question = ""
        self.entry.delete(0, "end")
        self.dash._signatures.pop("qa:" + self.spec["key"], None)
        self.render()

    def focus(self):
        """進入面板時把鍵盤焦點放在輸入框（全螢幕與雙螢幕下也要能直接打字）。"""
        try:
            if self.dash.current == self.spec["key"] and self.entry.winfo_exists():
                self.entry.focus_force()
        except tk.TclError:
            pass

    def render(self):
        """依目前的狀態更新面板：不能問就藏起輸入，處理中輸入暫停，回答與來源只當純文字顯示。"""
        view = self.controller.view()
        state = view["state"]
        asking = state in ("asking", "preparing")
        text = ""
        if self.question and state != "closed":
            text = "你問：" + self.question + chr(10) * 2
        if view["answer"]:
            text += view["answer"]
            if view["sources"]:
                text += chr(10) * 2 + "來源文件：" + "、".join(view["sources"])
        elif view["message"] and state in ("error", "busy"):
            text += view["message"]
        signature = (state, view["message"], text, view["can_ask"])
        if not self.dash._changed("qa:" + self.spec["key"], signature):
            return
        self.status.configure(text="" if state in ("answer", "idle") else view["message"])
        self.answer.configure(state="normal")
        self.answer.delete("1.0", "end")
        self.answer.insert("1.0", text)
        self.answer.configure(state="disabled")
        if state == "idle" and not text:                 # 還沒問：顯示歡迎畫面，有內容再換成回答框
            self.dash._hide(self.answer)
            self.dash._show(self.welcome)
        else:
            self.dash._hide(self.welcome)
            self.dash._show(self.answer)
        if state in ("closed", "offline"):
            self.dash._hide(self.input_row)
        else:
            self.dash._show(self.input_row)
        self.entry.configure(state="disabled" if asking else "normal")
        self.send_button.configure(state="disabled" if asking else "normal")

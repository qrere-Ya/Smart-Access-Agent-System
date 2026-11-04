"""
chat_window.py：企業知識庫與出勤查詢的聊天視窗入口與組裝（CustomTkinter，深色現代風格）。

主要功能：
  - 氣泡式對話：你的問題靠右、助理的回答靠左，回答逐字串流顯示；文字可以選取複製
  - 開啟時在背景載入模型並顯示進度（本機 Ollama 沒開會自動啟動）；載入完成前可以先看畫面
  - 範例問題按鈕、Enter 送出（Shift+Enter 換行）、新對話、重新整理文件、語言模型設定（本機／雲端）
  - 關閉視窗時釋放顯存上的模型；後台登出或閒置逾時時跟著關閉
  畫面只負責組裝與事件迴圈；問答、模型組裝、對話控制都在 rag 套件，這裡不放邏輯。

串接模組：
  - rag.chat_view：氣泡、歡迎畫面、輸入列；rag.chat_status_bar：標題列與狀態；rag.chat_settings_dialog：設定對話框
  - rag.chat_worker：背景工作與事件佇列；rag.bootstrap：組裝模型與助理；rag.chat_session：對話控制與串流事件
  - shared.ui_theme：共用主題；shared.auth：登入代碼驗證；admin.service_manager：自動啟動／關閉 Ollama
  - backend_main：用 service_manager.start_assistant_window 另開這個行程；單獨執行：python -m rag.chat_window
"""
import os
import queue
import sys
import time

import customtkinter as ctk

from rag import bootstrap, chat_worker
from rag.chat_session import ChatController, plain_text
from rag.chat_settings_dialog import SettingsDialog
from rag.chat_status_bar import StatusBar, build_footer_hint
from rag.chat_view import (APP_TITLE, FONT_FAMILY, META_BLOCK_PREFIXES, META_COLOR, META_PREFIXES, SAMPLE_QUESTIONS,
                           Bubble, bubble_width, build_input_row, build_welcome, make_note)
from shared import auth, ui_theme

__all__ = ["APP_TITLE", "FONT_FAMILY", "META_BLOCK_PREFIXES", "META_COLOR", "META_PREFIXES", "SAMPLE_QUESTIONS",
           "Bubble", "SettingsDialog", "AssistantWindow", "main"]

POLL_MS = 60
SESSION_CHECK_MS = 5000

ui_theme.apply_theme("dark", "blue")


class AssistantWindow(ctk.CTk):
    """助理主視窗：組裝標題列、對話區、輸入列，並在畫面執行緒輪詢背景事件。"""

    def __init__(self, runtime_factory=None, auto_start_ollama=True, session_token=None):
        super().__init__(fg_color=ui_theme.pair("bg"))
        self._session_token = session_token
        self.title(APP_TITLE)
        self.geometry("920x760")
        self.minsize(640, 520)
        self._runtime_factory = runtime_factory or bootstrap.create_runtime
        self._auto_start_ollama = auto_start_ollama
        self._runtime = None
        self._controller = None
        self._bubbles = []
        self._current = None
        self._events = queue.Queue()
        self._notes_widgets = []
        self._first_chunk = True
        self.welcome = None
        self._build()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        chat_worker.start_load_runtime(self._events, self._runtime_factory, self._auto_start_ollama)
        self.after(POLL_MS, self._tick)
        if session_token:
            self.after(SESSION_CHECK_MS, self._check_session)
            self._last_touch = 0.0
            for event in ("<Key>", "<Button>", "<MouseWheel>"):
                self.bind_all(event, self._note_activity, add="+")

    # --- 登入工作階段：後台登出或閒置逾時，這個視窗也要跟著關 ---

    def _check_session(self):
        """定時確認登入代碼仍有效，失效就關閉視窗。"""
        if auth.verify_token(self._session_token) is None:
            self._on_close()
            return
        self.after(SESSION_CHECK_MS, self._check_session)

    def _touch_session(self):
        """更新登入代碼的最後使用時間。"""
        if self._session_token:
            auth.touch_token(self._session_token)

    def _note_activity(self, _event=None):
        """閱讀、捲動、輸入都算使用中；每 5 秒最多寫一次檔。"""
        now = time.time()
        if now - self._last_touch >= 5:
            self._last_touch = now
            self._touch_session()

    # --- 畫面 ---

    def _build(self):
        """組裝標題列、對話區、輸入列與底部提示。"""
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        header = StatusBar(self, self._new_chat, self._refresh_docs, self._open_settings)
        header.grid(row=0, column=0, sticky="ew")
        self._header = header
        self.settings_btn, self.docs_btn, self.new_btn, self.status = (
            header.settings_btn, header.docs_btn, header.new_btn, header.status)

        self.chat = ctk.CTkScrollableFrame(self, corner_radius=0, fg_color="transparent")
        self.chat.grid(row=1, column=0, sticky="nsew", padx=6)
        # 注意：不能綁在 self.chat 上，會蓋掉 CTkScrollableFrame 自己更新捲動範圍的綁定
        self.chat._parent_canvas.bind("<Configure>", lambda e: self._relayout(), add="+")
        self._build_welcome()

        bottom, self.input, self.send_btn = build_input_row(self, self._on_enter, self.send_current)
        bottom.grid(row=2, column=0, sticky="ew", padx=16, pady=(6, 4))
        build_footer_hint(self).grid(row=3, column=0, pady=(0, 8))

    def _build_welcome(self):
        """顯示歡迎畫面與範例問題。"""
        self.welcome, self.chips = build_welcome(self.chat, self.send)

    def _bubble_width(self):
        """目前氣泡寬度。"""
        return bubble_width(self.chat)

    def _relayout(self):
        """對話區寬度改變時，所有氣泡跟著重排。"""
        width = self._bubble_width()
        for bubble in self._bubbles:
            bubble.set_width(width)

    def _add_bubble(self, role, text=""):
        """新增一則訊息氣泡（第一則出現時移除歡迎畫面）。"""
        if self.welcome is not None:
            self.welcome.destroy()
            self.welcome = None
        bubble = Bubble(self.chat, role, text, width=self._bubble_width())
        bubble.pack(anchor="e" if role == "user" else "w", padx=(80, 14) if role == "user" else (14, 80), pady=6)
        self._bubbles.append(bubble)
        self._scroll_to_bottom()
        return bubble

    def _add_note(self, text):
        """在對話區新增一則灰色提示。"""
        self._notes_widgets.append(make_note(self.chat, text))
        self._scroll_to_bottom()

    # --- 啟動載入 ---

    def _retry_load(self):
        """載入失敗後按「重新載入」：不用關掉視窗，重新跑一次背景載入。"""
        self._header.hide_retry()
        self.status.configure(text="⏳ 重新載入中…")
        chat_worker.start_load_runtime(self._events, self._runtime_factory, self._auto_start_ollama)

    def _on_ready(self, runtime):
        """模型載入完成：建立對話控制、啟用按鈕、顯示降級與讀檔失敗提示。"""
        self._runtime = runtime
        self._controller = ChatController(runtime.assistant)
        self._set_ready_status()
        for chip in self.chips:
            chip.configure(state="normal")
        self.send_btn.configure(state="normal")
        self.docs_btn.configure(state="normal")
        if "，已改用" in runtime.llm_note:
            self._add_note("ℹ️ " + runtime.llm_note.split("助理使用")[0].strip())
        failed = runtime.sync.failed
        if failed:
            self._add_note("⚠️ 這些文件讀不出內容，沒有收錄：" + "；".join(f"{n}（{why}）" for n, why in failed.items()))
        self.input.focus_set()

    def _set_ready_status(self):
        """狀態列顯示目前模型與文件數量。"""
        r = self._runtime
        model = r.llm_note.split("助理使用")[-1].split("，")[0]  # 只留模型名稱；備援原因另外用提示列出
        self.status.configure(text=f"🟢 {model}　·　文件 {len(r.store.documents)} 份（{len(r.store)} 段）")

    # --- 事件迴圈 ---

    def _tick(self):
        """畫面執行緒定時取出背景事件與串流事件並更新畫面。"""
        try:
            while True:
                kind, payload = self._events.get_nowait()
                if kind == "status":
                    self.status.configure(text=f"⏳ {payload}")
                elif kind == "note":
                    self._add_note(payload)
                elif kind == "ready":
                    self._on_ready(payload)
                elif kind == "failed":
                    self.status.configure(text="🔴 啟動失敗")
                    self._add_note(f"🛑 無法啟動助理：{payload}\n請確認 Ollama 已安裝、models 資料夾內有 bge-m3，"
                                   "並關掉不用的程式釋放記憶體，然後按右上角「重新載入」再試一次（不用關掉視窗）；"
                                   "也可以按 ⚙️ 檢查語言模型設定。")
                    self._header.show_retry(self._retry_load)
                elif kind == "settings_applied":
                    show, note = payload
                    show(note)
                    self._set_ready_status()
                elif kind == "docs":
                    self._add_note(payload)
                    self._set_ready_status()
                    self.docs_btn.configure(state="normal")
        except queue.Empty:
            pass
        if self._controller is not None:
            for event in self._controller.poll():
                self._handle_chat_event(event)
        if self.winfo_exists():
            self.after(POLL_MS, self._tick)

    def _handle_chat_event(self, event):
        """處理一個串流事件：開始、逐字內容、完成或錯誤。"""
        if event.kind == "start":
            self._add_bubble("user", event.text)
            self._current = self._add_bubble("assistant", "思考中…")
            self._first_chunk = True
            self.send_btn.configure(state="disabled")
        elif event.kind == "chunk":
            follow = self._at_bottom()
            if self._first_chunk:
                self._current.set_text("")
                self._first_chunk = False
            self._current.append(event.text)
            if follow:
                self._scroll_to_bottom()
        elif event.kind in ("done", "error"):
            if event.kind == "error":
                self._current.set_text(f"⚠️ 發生未預期的錯誤：{event.text}")
            elif self._first_chunk:
                self._current.set_text("（沒有產生內容）")
            else:
                self._current.set_text(plain_text(self._current.get_text()))
            self._current.mark_meta_lines()
            self._scroll_to_bottom()
            self.send_btn.configure(state="normal")
            self.input.focus_set()

    def _at_bottom(self):
        """目前是否停在最底部；使用者往上捲去看前面的內容時，新字出來不要把畫面拉回底部。"""
        return self.chat._parent_canvas.yview()[1] >= 0.97

    def _scroll_to_bottom(self):
        """排版完成後捲到對話區最底。"""
        self.after_idle(lambda: (self.chat.update_idletasks(), self.chat._parent_canvas.yview_moveto(1.0)))

    # --- 動作 ---

    def _on_enter(self, event):
        """Enter 送出，Shift+Enter 換行。"""
        if event.state & 0x0001:  # Shift+Enter 換行
            return None
        self.send_current()
        return "break"

    def send_current(self):
        """送出輸入框的內容，成功才清空輸入框。"""
        text = self.input.get("1.0", "end").strip()
        if self.send(text):
            self.input.delete("1.0", "end")

    def send(self, text):
        """送出問題；還沒載入完或正在回答時回傳 False。"""
        if self._controller is None or not text.strip():
            return False
        self._touch_session()
        return self._controller.submit(text)

    def _new_chat(self):
        """清掉目前對話，回到歡迎畫面。"""
        if self._controller is not None and not self._controller.new_chat():
            return
        for bubble in self._bubbles:
            bubble.destroy()
        for widget in self._notes_widgets:
            widget.destroy()
        self._bubbles, self._notes_widgets, self._current = [], [], None
        if self.welcome is None:
            self._build_welcome()
            for chip in self.chips:
                chip.configure(state="normal" if self._controller else "disabled")

    def _refresh_docs(self):
        """在背景重新掃描文件。"""
        if self._runtime is None:
            return
        self.docs_btn.configure(state="disabled")
        self.status.configure(text="⏳ 重新掃描文件…")
        chat_worker.start_refresh_docs(self._runtime, self._events)

    def _open_settings(self):
        """開啟語言模型設定對話框。"""
        SettingsDialog(self, self._apply_settings)

    def _apply_settings(self, show):
        """設定儲存後在背景重載語言模型；助理還沒載入完就只提示。"""
        if self._runtime is None:
            show("已儲存。助理載入完成後會使用新設定。")
            return
        chat_worker.start_apply_settings(self._runtime, self._events, show)

    def _on_close(self):
        """關閉視窗：釋放模型、停掉自己啟動的 Ollama。"""
        if self._runtime is not None:
            self.status.configure(text="釋放模型中…")
            self.update_idletasks()
            self._runtime.close()
        if self._auto_start_ollama:
            # 只關掉這個視窗自己啟動的 Ollama（沒有自己的行程就不動），不然視窗關了它會變成沒人管的孤兒行程
            from admin import service_manager

            service_manager.stop_service("ollama")
        self.destroy()


def main():
    """只接受從後台開啟（帶著有效的登入代碼）；直接執行沒有代碼就拒絕。"""
    token = os.environ.pop(auth.TOKEN_ENV, None)
    if auth.verify_token(token) is None:
        import tkinter
        from tkinter import messagebox

        root = tkinter.Tk()
        root.withdraw()
        messagebox.showerror("需要登入", "企業知識庫只能從管理後台開啟。"
                             + chr(10) + "請先執行 python -m admin.backend_main 登入，再按「企業知識庫與出勤查詢」。")
        root.destroy()
        sys.exit(2)
    AssistantWindow(session_token=token).mainloop()


if __name__ == "__main__":
    main()

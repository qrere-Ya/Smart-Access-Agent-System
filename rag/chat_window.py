"""
chat_window.py：管理者助理的聊天視窗（CustomTkinter，深色現代風格）。

主要功能：
  - 氣泡式對話：你的問題靠右、助理的回答靠左，回答逐字串流顯示；文字可以選取複製
  - 開啟時在背景載入模型並顯示進度（本機 Ollama 沒開會自動啟動）；載入完成前可以先看畫面
  - 範例問題按鈕、Enter 送出（Shift+Enter 換行）、新對話、重新整理文件、語言模型設定（本機／雲端）
  - 關閉視窗時釋放顯存上的模型
  畫面只負責顯示；問答、模型組裝、對話控制都在 assistant 套件，這裡不放邏輯。

串接模組：
  - rag.bootstrap：組裝模型與助理；rag.chat_session：背景執行緒與事件佇列
  - service_manager：自動啟動 Ollama；rag.llm_settings：語言模型設定的讀寫與測試
  - backend_main：用 service_manager.start_assistant_window 另開這個行程；單獨執行：python -m rag.chat_window
"""
import queue
import threading
import tkinter.font as tkfont

import customtkinter as ctk

from rag import bootstrap
from rag.chat_session import ChatController, plain_text

APP_TITLE = "管理者助理"
FONT_FAMILY = "微軟正黑體"
SAMPLE_QUESTIONS = ["特別休假的規定是什麼？", "今天誰遲到？", "最近有沒有尾隨事件？", "這週的出勤狀況",
                    "颱風天公司要放假嗎？", "加班費怎麼計算？"]
META_PREFIXES = ("📎", "🗂️", "ℹ️", "⚠️")
META_BLOCK_PREFIXES = ("📎", "🗂️")
USER_COLOR = ("#2563eb", "#2563eb")
BOT_COLOR = ("#e5e7eb", "#2b2f3a")
META_COLOR = "#9aa4b2"
POLL_MS = 60

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")


class Bubble(ctk.CTkFrame):
    """一則訊息：圓角底色加上可選取複製的文字，高度跟著內容自動伸縮。"""

    def __init__(self, master, role, text="", width=560):
        super().__init__(master, corner_radius=16, fg_color=USER_COLOR if role == "user" else BOT_COLOR)
        self.role = role
        self.box = ctk.CTkTextbox(self, width=width, height=30, wrap="word", activate_scrollbars=False,
                                  fg_color="transparent", border_spacing=8, font=(FONT_FAMILY, 14),
                                  text_color="#ffffff" if role == "user" else ("#111827", "#e8eaf0"))
        self.box.pack(padx=6, pady=4)
        self.box._textbox.tag_config("meta", foreground=META_COLOR)
        self._last_width = 0
        # 視窗真正排版出來、或寬度改變後，換行的行數會變，要重算高度（寬度沒變時略過，避免高度改變又觸發自己）
        self.box._textbox.bind("<Configure>", self._on_box_configure, add="+")
        if text:
            self.set_text(text)

    def _on_box_configure(self, event):
        if event.width != self._last_width:
            self._last_width = event.width
            self.after_idle(self.fit)

    def get_text(self):
        return self.box.get("1.0", "end").rstrip("\n")

    def set_text(self, text):
        self.box.configure(state="normal")
        self.box.delete("1.0", "end")
        self.box.insert("end", text)
        self._finish_edit()

    def append(self, text):
        self.box.configure(state="normal")
        self.box.insert("end", text)
        self._finish_edit()

    def _finish_edit(self):
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


class SettingsDialog(ctk.CTkToplevel):
    """語言模型設定：本機或雲端優先、模型名稱、金鑰（不顯示已存的金鑰）、測試與儲存。"""

    MODES = {"只用本機模型": "local", "雲端優先（失效自動改本機）": "api"}

    def __init__(self, master, on_saved):
        super().__init__(master)
        from rag import llm_settings

        self.ls, self.on_saved = llm_settings, on_saved
        self.title("語言模型設定")
        self.geometry("520x520")
        self.transient(master)
        s = self.ls.load_settings()
        pad = {"padx": 18, "pady": (8, 0), "anchor": "w"}

        ctk.CTkLabel(self, text="回答使用的語言模型", font=(FONT_FAMILY, 16, "bold")).pack(**pad)
        self.mode = ctk.CTkSegmentedButton(self, values=list(self.MODES), font=(FONT_FAMILY, 13))
        self.mode.set(next(k for k, v in self.MODES.items() if v == s["mode"]))
        self.mode.pack(padx=18, pady=(6, 0), fill="x")
        self.local_model = self._entry("本機模型名稱（Ollama）", s["local_model"])
        self.api_base = self._entry("雲端 API 網址（OpenAI 相容）", s["api_base"])
        self.api_model = self._entry("雲端模型名稱", s["api_model"])
        self.api_key = self._entry("雲端 API 金鑰", "", show="•", placeholder="留空＝沿用已儲存的")
        ctk.CTkLabel(self, text=self.ls.key_status_text(s), text_color=META_COLOR,
                     font=(FONT_FAMILY, 12)).pack(padx=18, pady=(2, 0), anchor="w")

        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(padx=18, pady=14, fill="x")
        ctk.CTkButton(row, text="測試本機", width=90, command=lambda: self._test("local")).pack(side="left")
        ctk.CTkButton(row, text="測試雲端", width=90, command=lambda: self._test("api")).pack(side="left", padx=8)
        ctk.CTkButton(row, text="儲存並套用", width=110, command=self._save).pack(side="right")
        self.result = ctk.CTkLabel(self, text="", wraplength=470, justify="left", font=(FONT_FAMILY, 12))
        self.result.pack(padx=18, anchor="w")

    def _entry(self, label, value, show=None, placeholder=""):
        ctk.CTkLabel(self, text=label, font=(FONT_FAMILY, 13)).pack(padx=18, pady=(10, 0), anchor="w")
        entry = ctk.CTkEntry(self, show=show, placeholder_text=placeholder, font=(FONT_FAMILY, 13))
        entry.pack(padx=18, pady=(2, 0), fill="x")
        if value:
            entry.insert(0, value)
        return entry

    def _form(self):
        return {"mode": self.MODES[self.mode.get()], "local_model": self.local_model.get(),
                "api_base": self.api_base.get(), "api_model": self.api_model.get(), "api_key": self.api_key.get()}

    def _show(self, text):
        if self.winfo_exists():
            self.result.configure(text=text)

    def _test(self, which):
        self._show("測試中…（第一次載入模型可能要等一會兒）")
        settings = {**self.ls.load_settings(), **{k: v for k, v in self._form().items() if k != "api_key" or v}}

        def run():
            ok, msg = (self.ls.test_local if which == "local" else self.ls.test_api)(settings)
            plain = msg.replace("**", "").replace("`", "")
            self.after(0, lambda: self._show(plain))

        threading.Thread(target=run, daemon=True).start()

    def _save(self):
        self.ls.save_settings(self._form())
        self._show("已儲存，正在套用…")
        self.on_saved(lambda note: self._show(note))


class AssistantWindow(ctk.CTk):
    def __init__(self, runtime_factory=None, auto_start_ollama=True):
        super().__init__()
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
        threading.Thread(target=self._load_runtime, daemon=True).start()
        self.after(POLL_MS, self._tick)

    # --- 畫面 ---

    def _build(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        header = ctk.CTkFrame(self, corner_radius=0, height=56)
        header.grid(row=0, column=0, sticky="ew")
        ctk.CTkLabel(header, text="🤖  管理者助理", font=(FONT_FAMILY, 18, "bold")).pack(side="left", padx=18, pady=12)
        self.settings_btn = ctk.CTkButton(header, text="⚙️", width=40, fg_color="transparent", border_width=1,
                                          command=self._open_settings)
        self.settings_btn.pack(side="right", padx=(6, 14))
        self.docs_btn = ctk.CTkButton(header, text="🔄 文件", width=80, fg_color="transparent", border_width=1,
                                      command=self._refresh_docs, state="disabled", font=(FONT_FAMILY, 13))
        self.docs_btn.pack(side="right", padx=6)
        self.new_btn = ctk.CTkButton(header, text="＋ 新對話", width=90, fg_color="transparent", border_width=1,
                                     command=self._new_chat, font=(FONT_FAMILY, 13))
        self.new_btn.pack(side="right", padx=6)
        self.status = ctk.CTkLabel(header, text="啟動中…", text_color=META_COLOR, font=(FONT_FAMILY, 12))
        self.status.pack(side="right", padx=14)

        self.chat = ctk.CTkScrollableFrame(self, corner_radius=0, fg_color="transparent")
        self.chat.grid(row=1, column=0, sticky="nsew", padx=6)
        # 注意：不能綁在 self.chat 上，會蓋掉 CTkScrollableFrame 自己更新捲動範圍的綁定
        self.chat._parent_canvas.bind("<Configure>", lambda e: self._relayout(), add="+")
        self._build_welcome()

        bottom = ctk.CTkFrame(self, fg_color="transparent")
        bottom.grid(row=2, column=0, sticky="ew", padx=16, pady=(6, 4))
        bottom.grid_columnconfigure(0, weight=1)
        self.input = ctk.CTkTextbox(bottom, height=64, wrap="word", font=(FONT_FAMILY, 14), border_width=1,
                                    border_spacing=8)
        self.input.grid(row=0, column=0, sticky="ew")
        self.input.bind("<Return>", self._on_enter)
        self.send_btn = ctk.CTkButton(bottom, text="送出", width=84, height=64, command=self.send_current,
                                      state="disabled", font=(FONT_FAMILY, 15, "bold"))
        self.send_btn.grid(row=0, column=1, padx=(10, 0))
        ctk.CTkLabel(self, text="Enter 送出，Shift+Enter 換行　·　回答僅供參考，請以原始文件與資料庫為準",
                     text_color=META_COLOR, font=(FONT_FAMILY, 11)).grid(row=3, column=0, pady=(0, 8))

    def _build_welcome(self):
        self.welcome = ctk.CTkFrame(self.chat, fg_color="transparent")
        self.welcome.pack(pady=(48, 8))
        ctk.CTkLabel(self.welcome, text="有什麼想查的？", font=(FONT_FAMILY, 24, "bold")).pack()
        ctk.CTkLabel(self.welcome, text="問公司文件與法規，或查考勤與異常紀錄", text_color=META_COLOR,
                     font=(FONT_FAMILY, 14)).pack(pady=(4, 18))
        grid = ctk.CTkFrame(self.welcome, fg_color="transparent")
        grid.pack()
        self.chips = []
        for i, question in enumerate(SAMPLE_QUESTIONS):
            chip = ctk.CTkButton(grid, text=question, width=250, height=40, corner_radius=20, fg_color=BOT_COLOR,
                                 text_color=("#111827", "#e8eaf0"), hover_color=("#d1d5db", "#3a4050"),
                                 font=(FONT_FAMILY, 13), state="disabled",
                                 command=lambda q=question: self.send(q))
            chip.grid(row=i // 2, column=i % 2, padx=6, pady=6)
            self.chips.append(chip)

    def _bubble_width(self):
        canvas_width = self.chat._parent_canvas.winfo_width()
        return max(300, int(canvas_width * 0.72) - 40) if canvas_width > 1 else 560

    def _relayout(self):
        width = self._bubble_width()
        for bubble in self._bubbles:
            bubble.set_width(width)

    def _add_bubble(self, role, text=""):
        if self.welcome is not None:
            self.welcome.destroy()
            self.welcome = None
        bubble = Bubble(self.chat, role, text, width=self._bubble_width())
        bubble.pack(anchor="e" if role == "user" else "w", padx=(80, 14) if role == "user" else (14, 80), pady=6)
        self._bubbles.append(bubble)
        self._scroll_to_bottom()
        return bubble

    def _add_note(self, text):
        label = ctk.CTkLabel(self.chat, text=text, text_color=META_COLOR, font=(FONT_FAMILY, 12), wraplength=620)
        label.pack(pady=6)
        self._notes_widgets.append(label)
        self._scroll_to_bottom()

    # --- 啟動載入 ---

    def _load_runtime(self):
        try:
            if self._auto_start_ollama:
                self._events.put(("status", "啟動本機語言模型服務…"))
                from admin import service_manager

                started, warning = service_manager.start_ollama(lambda line: None)
                if warning:
                    self._events.put(("note", f"⚠️ {warning}"))
            runtime = self._runtime_factory(progress=lambda text: self._events.put(("status", text)))
            self._events.put(("ready", runtime))
        except Exception as e:
            self._events.put(("failed", f"{type(e).__name__}：{e}"))

    def _on_ready(self, runtime):
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
        r = self._runtime
        model = r.llm_note.split("助理使用")[-1].split("，")[0]  # 只留模型名稱；備援原因另外用提示列出
        self.status.configure(text=f"🟢 {model}　·　文件 {len(r.store.documents)} 份（{len(r.store)} 段）")

    # --- 事件迴圈 ---

    def _tick(self):
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
                                   "然後按右上角 ⚙️ 檢查語言模型設定，或重新開啟視窗。")
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
        self.after_idle(lambda: (self.chat.update_idletasks(), self.chat._parent_canvas.yview_moveto(1.0)))

    # --- 動作 ---

    def _on_enter(self, event):
        if event.state & 0x0001:  # Shift+Enter 換行
            return None
        self.send_current()
        return "break"

    def send_current(self):
        text = self.input.get("1.0", "end").strip()
        if self.send(text):
            self.input.delete("1.0", "end")

    def send(self, text):
        """送出問題；還沒載入完或正在回答時回傳 False。"""
        if self._controller is None or not text.strip():
            return False
        return self._controller.submit(text)

    def _new_chat(self):
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
        if self._runtime is None:
            return
        self.docs_btn.configure(state="disabled")
        self.status.configure(text="⏳ 重新掃描文件…")

        def run():
            report = bootstrap.refresh_documents(self._runtime)
            parts = []
            for label, items in (("新增", report.added), ("更新", report.updated), ("移除", report.removed)):
                if items:
                    parts.append(f"{label}：{'、'.join(items)}")
            if report.failed:
                parts.append("讀不出內容：" + "；".join(f"{n}（{why}）" for n, why in report.failed.items()))
            self._events.put(("docs", "📚 文件已重新掃描　" + ("　".join(parts) if parts else "沒有變動")))

        threading.Thread(target=run, daemon=True).start()

    def _open_settings(self):
        SettingsDialog(self, self._apply_settings)

    def _apply_settings(self, show):
        if self._runtime is None:
            show("已儲存。助理載入完成後會使用新設定。")
            return

        def run():
            note = bootstrap.reload_llm(self._runtime)
            self.after(0, lambda: (show(note), self._set_ready_status()))

        threading.Thread(target=run, daemon=True).start()

    def _on_close(self):
        if self._runtime is not None:
            self.status.configure(text="釋放模型中…")
            self.update_idletasks()
            self._runtime.close()
        self.destroy()


def main():
    AssistantWindow().mainloop()


if __name__ == "__main__":
    main()

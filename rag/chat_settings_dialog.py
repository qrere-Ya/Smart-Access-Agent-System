"""
chat_settings_dialog.py：語言模型設定對話框（本機或雲端優先、模型名稱、金鑰、測試與儲存）。

主要功能：
  - 本機模型用下拉選單，清單在背景向 Ollama 查詢；已存的模型沒安裝會標註「（尚未安裝）」，存檔時去掉標註
  - 雲端 API 網址、模型名稱、金鑰（金鑰欄不顯示已存的值，留空＝沿用）；測試本機／雲端連線；儲存並套用
  - 背景執行緒只把結果放進佇列，畫面更新由畫面執行緒輪詢處理（Tk 不是執行緒安全的）

串接模組：
  - rag.llm_settings：設定的讀寫與連線測試；rag.chat_worker：開背景執行緒
  - shared.ui_theme：按鈕與色彩；rag.chat_window：開啟對話框並傳入儲存後的套用動作
"""
import queue

import customtkinter as ctk

from rag import chat_worker
from shared import ui_theme

META_COLOR = ui_theme.pair("text_muted")
FONT = ui_theme.font()


class SettingsDialog(ctk.CTkToplevel):
    """語言模型設定：本機或雲端優先、模型名稱、金鑰（不顯示已存的金鑰）、測試與儲存。"""

    MODES = {"只用本機模型": "local", "雲端優先（失效自動改本機）": "api"}
    NOT_INSTALLED = "（尚未安裝）"

    def __init__(self, master, on_saved):
        super().__init__(master, fg_color=ui_theme.pair("bg"))
        from rag import llm_settings

        self.ls, self.on_saved = llm_settings, on_saved
        self.title("語言模型設定")
        self.geometry("520x520")
        self.transient(master)
        s = self.ls.load_settings()
        pad = {"padx": 18, "pady": (8, 0), "anchor": "w"}

        ctk.CTkLabel(self, text="回答使用的語言模型", font=ui_theme.font(ui_theme.FONT_SIZE_SECTION, bold=True)).pack(
            **pad)
        self.mode = ctk.CTkSegmentedButton(self, values=list(self.MODES), font=FONT)
        self.mode.set(next(k for k, v in self.MODES.items() if v == s["mode"]))
        self.mode.pack(padx=18, pady=(6, 0), fill="x")
        ctk.CTkLabel(self, text="本機模型（Ollama 已安裝的模型）", font=FONT).pack(padx=18, pady=(10, 0), anchor="w")
        self._saved_model = s["local_model"]
        self.local_model = ctk.CTkOptionMenu(self, values=[self._saved_model], font=FONT, dropdown_font=FONT,
                                             dynamic_resizing=False)
        self.local_model.set(self._saved_model)
        self.local_model.pack(padx=18, pady=(2, 0), fill="x")
        self._model_names = queue.Queue()
        self._test_results = queue.Queue()  # 測試連線的結果，背景執行緒只放進來，畫面更新留給畫面執行緒
        chat_worker.spawn(self._load_model_names, s)
        self.after(100, self._poll_model_names)
        self.after(100, self._poll_test_results)
        self.api_base = self._entry("雲端 API 網址（OpenAI 相容）", s["api_base"])
        self.api_model = self._entry("雲端模型名稱", s["api_model"])
        self.api_key = self._entry("雲端 API 金鑰", "", show="•", placeholder="留空＝沿用已儲存的")
        ctk.CTkLabel(self, text=self.ls.key_status_text(s), text_color=META_COLOR,
                     font=ui_theme.font(ui_theme.FONT_SIZE_SMALL)).pack(padx=18, pady=(2, 0), anchor="w")

        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(padx=18, pady=14, fill="x")
        ui_theme.secondary_button(row, "測試本機", lambda: self._test("local"), width=90).pack(side="left")
        ui_theme.secondary_button(row, "測試雲端", lambda: self._test("api"), width=90).pack(side="left", padx=8)
        ui_theme.primary_button(row, "儲存並套用", self._save, width=110).pack(side="right")
        self.result = ctk.CTkLabel(self, text="", wraplength=470, justify="left",
                                   font=ui_theme.font(ui_theme.FONT_SIZE_SMALL))
        self.result.pack(padx=18, anchor="w")

    def _load_model_names(self, settings):
        """背景向 Ollama 問已安裝的模型，問不到就維持只有目前設定的那一個。"""
        ok, names = self.ls.list_local_models(settings)
        self._model_names.put(names if ok and names else None)

    def _poll_model_names(self):
        """畫面執行緒定時看背景有沒有查到模型清單（Tk 的 after 不能從別的執行緒呼叫）。"""
        if not self.winfo_exists():
            return
        try:
            names = self._model_names.get_nowait()
        except queue.Empty:
            self.after(100, self._poll_model_names)
            return
        if names:
            self._fill_model_names(names)

    def _poll_test_results(self):
        """畫面執行緒定時取出背景測試連線的結果並顯示。"""
        if not self.winfo_exists():
            return
        try:
            while True:
                self._show(self._test_results.get_nowait())
        except queue.Empty:
            pass
        self.after(100, self._poll_test_results)

    def _fill_model_names(self, names):
        """把查到的已安裝模型填進下拉選單，並處理已存模型沒安裝或少了標籤的情況。"""
        if not self.winfo_exists():
            return
        current = self.local_model.get()
        values = list(names)
        if current not in values:
            same = next((n for n in values if n.startswith(current + ":")), None)  # 寫 llama3、裝的是 llama3:latest
            if same:
                current = same
            else:
                values.insert(0, current + self.NOT_INSTALLED)
                current += self.NOT_INSTALLED
        self.local_model.configure(values=values)
        self.local_model.set(current if current in values else values[0])

    def _entry(self, label, value, show=None, placeholder=""):
        """一組標籤加輸入框。"""
        ctk.CTkLabel(self, text=label, font=FONT).pack(padx=18, pady=(10, 0), anchor="w")
        entry = ctk.CTkEntry(self, show=show, placeholder_text=placeholder, font=FONT)
        entry.pack(padx=18, pady=(2, 0), fill="x")
        if value:
            entry.insert(0, value)
        return entry

    def _form(self):
        """讀出表單目前的設定值。"""
        local_model = self.local_model.get().removesuffix(self.NOT_INSTALLED)
        return {"mode": self.MODES[self.mode.get()], "local_model": local_model,
                "api_base": self.api_base.get(), "api_model": self.api_model.get(), "api_key": self.api_key.get()}

    def _show(self, text):
        """在結果列顯示文字。"""
        if self.winfo_exists():
            self.result.configure(text=text)

    def _test(self, which):
        """背景測試本機或雲端連線，結果放進佇列。"""
        self._show("測試中…（第一次載入模型可能要等一會兒）")
        settings = {**self.ls.load_settings(), **{k: v for k, v in self._form().items() if k != "api_key" or v}}

        def run():
            ok, msg = (self.ls.test_local if which == "local" else self.ls.test_api)(settings)
            plain = msg.replace("**", "").replace("`", "")
            self._test_results.put(plain)

        chat_worker.spawn(run)

    def _save(self):
        """儲存設定並請視窗套用。"""
        self.ls.save_settings(self._form())
        self._show("已儲存，正在套用…")
        self.on_saved(lambda note: self._show(note))

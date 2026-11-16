"""
chat_log_panel.py：報告頁的「對話紀錄」分頁（CustomTkinter）——讓管理者檢查 AI 有沒有答錯或幻覺。

主要功能：
  - 列表：時間、來源（後台出缺勤／前台知識庫）、審查結果、問題摘要；新的在前，可依來源、審查結果、起訖日期篩選
  - 詳情：點一列顯示問題、AI 回答全文與引用段落（文件名稱、分數、內容，或出缺勤的查詢結果表）；一律當純文字顯示，
    不當 HTML 也沒有可點的連結
  - 審查：標為「正確」「錯誤」「幻覺」或清除標記，記下審查者（登入的管理者）與時間
  - 統計：只寫「已審查 N 筆：正確 a、錯誤 b、幻覺 c；未審查 M 筆」，**不寫準確率**（審查是人工、只有看過的才有標記、
    判斷主觀、樣本有偏差）；要量準確率用固定題庫評估
  - 匯出 CSV：匯出前先提示內含個資（員工姓名、前台使用者的提問）；超過 30 天的紀錄每天自動刪除，畫面提示要先匯出
  - 前台使用者不會被告知有記錄，這裡也不會有任何東西送回前台

串接模組：
  - shared.chat_log：資料層（列表、詳情、審查、統計、匯出）
  - calendar_panel：把本面板放進「出勤報告」頁的分頁；backend_main：傳入登入的管理者名稱
  - ui_theme：色彩、字型、表格、對話框
"""
import datetime
import sqlite3
import tkinter as tk
from tkinter import filedialog

import customtkinter as ctk

from shared import chat_log, ui_theme

SOURCE_CHOICES = (("全部來源", None), ("後台出缺勤", chat_log.SOURCE_BACK), ("前台知識庫", chat_log.SOURCE_FRONT))
REVIEW_CHOICES = (("全部審查結果", None), ("未審查", chat_log.REVIEW_NONE), ("正確", chat_log.REVIEW_CORRECT),
                  ("錯誤", chat_log.REVIEW_WRONG), ("幻覺", chat_log.REVIEW_HALLUCINATION))
COLUMNS = (("ts", "時間", 150), ("source", "來源", 90), ("review", "審查", 70), ("question", "問題", 420))
PRIVACY_NOTICE = ("對話紀錄含員工姓名與前台使用者的提問（個資），只有登入的管理者看得到；"
                  f"超過 {chat_log.RETENTION_DAYS} 天的紀錄會每天自動刪除，要保留請先匯出 CSV。")
EXPORT_WARNING = ("匯出的 CSV 含員工姓名、前台使用者的提問與 AI 回答（個資），請妥善保管，不要傳給不需要的人。\n\n"
                  "確定要匯出嗎？")
GENERIC_ERROR = "操作失敗，請稍後再試。"


def detail_text(row):
    """詳情的純文字（問題、回答、引用段落）；row 是 chat_log.get_log 的結果。"""
    review = chat_log.REVIEW_LABELS.get(row["review"], row["review"])
    if row["reviewed_at"]:
        review += f"（{row['reviewer'] or '未記名'}，{row['reviewed_at']}）"
    lines = [f"時間：{row['ts']}　來源：{chat_log.SOURCE_LABELS.get(row['source'], row['source'])}　審查：{review}",
             "", "【問題】", row["question"], "", "【AI 回答】", row["answer"], "", "【引用段落】"]
    if not row["citations"]:
        lines.append("（沒有引用段落）")
    for i, c in enumerate(row["citations"], start=1):
        score = "" if c.get("score") is None else f"　分數 {c['score']}"
        lines.append(f"[{i}] {c.get('title') or ''}　{c.get('label') or ''}{score}")
        lines.append(c.get("text") or "")
        lines.append("")
    return "\n".join(lines)


class ChatLogPanel(ctk.CTkFrame):
    """對話紀錄分頁：篩選、列表、詳情、審查、匯出。"""

    def __init__(self, master, reviewer=""):
        super().__init__(master, fg_color="transparent", height=1)
        self.reviewer = reviewer if callable(reviewer) else (lambda: reviewer)
        self._ids = {}
        pad = 0
        ui_theme.muted_label(self, PRIVACY_NOTICE, wraplength=980, justify="left").pack(anchor="w", pady=(2, 4))
        self.summary_var = tk.StringVar(master=self, value="")
        ctk.CTkLabel(self, textvariable=self.summary_var, font=ui_theme.font(), text_color=ui_theme.pair("text"),
                     anchor="w").pack(fill="x", pady=(0, 6))

        bar = ctk.CTkFrame(self, fg_color="transparent", height=1)
        bar.pack(fill="x", padx=pad, pady=(0, 6))
        self.source_box = ui_theme.combo(bar, [t for t, _ in SOURCE_CHOICES], width=120)
        self.source_box.set(SOURCE_CHOICES[0][0])
        self.source_box.pack(side="left", padx=(0, 4))
        self.review_box = ui_theme.combo(bar, [t for t, _ in REVIEW_CHOICES], width=130)
        self.review_box.set(REVIEW_CHOICES[0][0])
        self.review_box.pack(side="left", padx=4)
        # 有 placeholder 提示文字的輸入框不能綁 textvariable（CustomTkinter 的限制）
        self.start_entry = ui_theme.entry(bar, placeholder_text="起 YYYY-MM-DD", width=150)
        self.start_entry.pack(side="left", padx=4)
        self.end_entry = ui_theme.entry(bar, placeholder_text="迄 YYYY-MM-DD", width=150)
        self.end_entry.pack(side="left", padx=4)
        ui_theme.primary_button(bar, "查詢", self.refresh, width=70).pack(side="left", padx=4)
        ui_theme.secondary_button(bar, "匯出 CSV", self.export, width=90).pack(side="left", padx=4)

        box, self.tree = ui_theme.table(self, COLUMNS, height=5)
        box.pack(fill="x", pady=(0, 6))
        self.tree.bind("<<TreeviewSelect>>", ui_theme.event_only(lambda _e: self.show_selected()))

        actions = ctk.CTkFrame(self, fg_color="transparent", height=1)
        actions.pack(side="bottom", fill="x")                      # 先放底部：視窗矮時詳情縮小，審查按鈕一定看得到
        self.detail = ctk.CTkTextbox(self, height=150, wrap="word", font=ui_theme.font(),
                                     fg_color=ui_theme.pair("card"), text_color=ui_theme.pair("text"),
                                     border_width=1, border_color=ui_theme.pair("border"))
        self.detail.pack(fill="both", expand=True, pady=(0, 6))
        self._set_detail("點上面的一筆，這裡會顯示問題、AI 回答與引用段落。")
        ui_theme.body_label(actions, "審查這一筆：").pack(side="left", padx=(0, 6))
        self.review_buttons = {
            chat_log.REVIEW_CORRECT: ui_theme.primary_button(
                actions, "正確", lambda: self.review(chat_log.REVIEW_CORRECT), width=70),
            chat_log.REVIEW_WRONG: ui_theme.danger_button(
                actions, "錯誤", lambda: self.review(chat_log.REVIEW_WRONG), width=70),
            chat_log.REVIEW_HALLUCINATION: ui_theme.danger_button(
                actions, "幻覺", lambda: self.review(chat_log.REVIEW_HALLUCINATION), width=70),
            chat_log.REVIEW_NONE: ui_theme.secondary_button(
                actions, "清除標記", lambda: self.review(chat_log.REVIEW_NONE), width=90),
        }
        for button in self.review_buttons.values():
            button.pack(side="left", padx=4)
            button.configure(state="disabled")
        self.refresh()

    # --- 共用 ---

    def _dialog_parent(self):
        return self.winfo_toplevel()

    def _notify(self, message, kind="info"):
        ui_theme.show_message(self._dialog_parent(), "對話紀錄", message, kind)

    def _set_detail(self, text):
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        self.detail.insert("1.0", text)
        self.detail.configure(state="disabled")

    def _filters(self):
        source = dict(SOURCE_CHOICES).get(self.source_box.get())
        review = dict(REVIEW_CHOICES).get(self.review_box.get())
        return {"source": source, "review": review, "start": self.start_entry.get().strip() or None,
                "end": self.end_entry.get().strip() or None}

    def _guard(self, action):
        """執行會碰資料庫的動作；輸入不合法顯示原因，其他例外只印型別、畫面顯示通用訊息。"""
        try:
            return True, action()
        except ValueError as e:
            self._notify(str(e), "warning")
        except (sqlite3.Error, OSError) as e:
            print(f"[對話紀錄] 操作失敗：{type(e).__name__}")
            self._notify(GENERIC_ERROR, "error")
        return False, None

    # --- 列表與詳情 ---

    def refresh(self):
        """依目前的篩選重新查詢並更新列表與統計。"""
        filters = self._filters()
        ok, rows = self._guard(lambda: chat_log.list_logs(**filters))
        if not ok:
            return
        self._ids = {str(r["id"]): r for r in rows}
        ui_theme.fill_table(self.tree, [
            [r["ts"], chat_log.SOURCE_LABELS.get(r["source"], r["source"]),
             chat_log.REVIEW_LABELS.get(r["review"], r["review"]), r["question"]] for r in rows],
            iids=[r["id"] for r in rows])
        ok, counts = self._guard(lambda: chat_log.review_counts(filters["source"], filters["start"], filters["end"]))
        if ok:
            self.summary_var.set(f"共 {counts['total']} 筆｜" + chat_log.review_summary(counts))
        self._set_detail("點上面的一筆，這裡會顯示問題、AI 回答與引用段落。")
        self._enable_review(False)

    def _enable_review(self, on):
        for button in self.review_buttons.values():
            button.configure(state="normal" if on else "disabled")

    def selected_id(self):
        selection = self.tree.selection()
        return int(selection[0]) if selection else None

    def show_selected(self):
        """顯示目前選取那一筆的全文與引用。"""
        log_id = self.selected_id()
        if log_id is None:
            self._enable_review(False)
            return
        ok, row = self._guard(lambda: chat_log.get_log(log_id))
        if not ok:
            return
        if row is None:
            self._set_detail("這一筆已經不存在（可能已超過保存期限被刪除）。")
            self._enable_review(False)
            return
        self._set_detail(detail_text(row))
        self._enable_review(True)

    # --- 審查與匯出 ---

    def review(self, value):
        """把選取的那一筆標成 value（正確、錯誤、幻覺，或清除標記），並更新列表與統計、停在同一筆。"""
        log_id = self.selected_id()
        if log_id is None:
            self._notify("請先選一筆紀錄。")
            return
        ok, updated = self._guard(lambda: chat_log.set_review(log_id, value, self.reviewer() or ""))
        if not ok:
            return
        if not updated:
            self._notify("這一筆已經不存在，無法審查。", "warning")
        self.refresh()
        if self.tree.exists(str(log_id)):
            self.tree.selection_set(str(log_id))
            self.show_selected()

    def export(self):
        """先提示個資，再讓使用者選位置，依目前的篩選匯出 CSV。"""
        if not ui_theme.confirm(self._dialog_parent(), "匯出對話紀錄", EXPORT_WARNING, ok_text="匯出", danger=True):
            return
        path = filedialog.asksaveasfilename(
            parent=self._dialog_parent(), defaultextension=".csv", filetypes=[("CSV 檔", "*.csv")],
            initialfile=f"chat_logs_{datetime.date.today().strftime('%Y-%m-%d')}.csv")
        if not path:
            return
        filters = self._filters()
        ok, count = self._guard(lambda: chat_log.export_csv(path, **filters))
        if ok:
            self._notify(f"已匯出 {count} 筆到\n{path}")

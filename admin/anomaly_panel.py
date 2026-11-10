"""
anomaly_panel.py：異常事件列表視窗（CustomTkinter 外框，表格用調過樣式的 ttk.Treeview）。

主要功能：
  - 列出最近的異常事件（未讀標記、時間、中文類型、說明、合併次數、有無證據照），新的在前
  - 選一筆就在右側預覽證據照片與完整說明；可用系統看圖軟體開啟原圖
  - 「標為已讀」把目前列表看到的事件全部標為已讀，並同步更新通知列的紅點
  - 證據照片路徑來自資料庫，開啟前一律經 anomaly_notifier.safe_evidence_path 確認在證據資料夾內

串接模組：
  - anomaly_notifier：已讀進度、證據路徑安全檢查、列表文字；database_mgr：讀取異常事件
  - notification_bar：點「異常 N」或彈窗的「查看」時由 backend_main 開啟本視窗
  - ui_theme：色彩、字型、按鈕、表格、對話框
"""
import os

import customtkinter as ctk
from PIL import Image

from admin import anomaly_notifier
from shared import database_mgr
from shared import event_labels
from shared import ui_theme

LIST_LIMIT = 200          # 最多列出幾筆
PREVIEW_SIZE = (300, 360)


class AnomalyPanel(ctk.CTkFrame):
    """異常事件列表與證據預覽。"""

    COLUMNS = (("state", "狀態", 70, "center"), ("time", "時間", 185), ("type", "類型", 150, "center"),
               ("detail", "說明", 260), ("count", "次數", 52, "center"), ("photo", "證據", 52, "center"))

    def __init__(self, master, notifier=None, on_mark_read=None, fetch=None):
        super().__init__(master, fg_color="transparent", height=1)
        self.notifier = notifier or anomaly_notifier.AnomalyNotifier()
        self._on_mark_read = on_mark_read
        self._fetch = fetch or database_mgr.list_anomaly_events
        self._events = {}                                  # iid（事件編號）-> 事件
        self._preview_image = None                         # 持有圖片參照，避免被回收而不顯示
        pad = ui_theme.PAD_LG
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)
        ui_theme.section_title(self, "🔔 異常事件").grid(row=0, column=0, columnspan=2, sticky="w", padx=pad,
                                                          pady=(pad, 4))
        self.summary = ui_theme.muted_label(self, "", wraplength=900, justify="left")
        self.summary.grid(row=1, column=0, columnspan=2, sticky="w", padx=pad, pady=(0, 10))

        box, self.tree = ui_theme.table(self, self.COLUMNS, height=16)
        for col in self.COLUMNS:                         # 只有「說明」隨視窗伸縮，其餘欄位固定寬度才不會被擠掉
            self.tree.column(col[0], stretch=col[0] == "detail", minwidth=col[2] if col[0] != "detail" else 120)
        box.grid(row=2, column=0, sticky="nsew", padx=(pad, 10), pady=(0, 10))
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._show_selected())

        side = ui_theme.card(self, width=PREVIEW_SIZE[0] + 24)
        side.grid(row=2, column=1, sticky="ns", padx=(0, pad), pady=(0, 10))
        side.grid_propagate(False)
        self.preview = ctk.CTkLabel(side, text="選一筆事件查看證據照片", width=PREVIEW_SIZE[0], height=PREVIEW_SIZE[1],
                                    text_color=ui_theme.pair("text_muted"),
                                    font=ui_theme.font(ui_theme.FONT_SIZE_SMALL))
        self.preview.pack(padx=12, pady=(12, 6))
        self.detail = ui_theme.muted_label(side, "", wraplength=PREVIEW_SIZE[0], justify="left")
        self.detail.pack(padx=12, pady=(0, 12), anchor="w")

        bar = ctk.CTkFrame(self, fg_color="transparent", height=1)
        bar.grid(row=3, column=0, columnspan=2, sticky="ew", padx=pad, pady=(0, pad))
        for col, (text, command, make) in enumerate((
                ("重新整理", self.refresh, ui_theme.secondary_button),
                ("開啟原圖", self.open_photo, ui_theme.secondary_button),
                ("全部標為已讀", self.mark_all_read, ui_theme.primary_button))):
            bar.grid_columnconfigure(col, weight=1, uniform="actions")
            make(bar, text, command).grid(row=0, column=col, sticky="ew", padx=4)
        self.refresh()

    def refresh(self):
        """重新讀取事件並更新列表與摘要。"""
        try:
            events = self._fetch(limit=LIST_LIMIT)
        except Exception as e:  # 資料庫暫時讀不到時畫面維持原狀，不讓視窗當掉
            print(f"[Anomaly Panel][WARN] 異常事件查詢失敗: {type(e).__name__}")
            self.summary.configure(text="讀取異常事件失敗，請稍後按「重新整理」。")
            return
        last_seen = self.notifier.last_seen
        self._events = {str(e["id"]): e for e in events}
        ui_theme.fill_table(self.tree, [anomaly_notifier.event_row(e, last_seen) for e in events],
                            iids=[e["id"] for e in events])
        unread = sum(1 for e in events if last_seen is not None and e["id"] > last_seen)
        self.summary.configure(text=f"共 {len(events)} 筆（最多顯示最近 {LIST_LIMIT} 筆），其中未讀 {unread} 筆。"
                                    "選一筆可預覽證據照片。")
        self._show_selected()

    def _selected_event(self):
        """目前選取的事件，沒選回傳 None。"""
        selection = self.tree.selection()
        return self._events.get(selection[0]) if selection else None

    def _show_selected(self):
        """依選取的事件更新右側預覽與說明。"""
        event = self._selected_event()
        if event is None:
            self._set_preview(None, "選一筆事件查看證據照片")
            self.detail.configure(text="")
            return
        span = event["timestamp"] if event["end_timestamp"] == event["timestamp"] else (
            f"{event['timestamp']} ~ {event['end_timestamp']}")
        self.detail.configure(text=f"{event_labels.label(event['event_type'])}｜{span}\n"
                                   f"{' '.join(str(event.get('detail') or '').split()) or '（沒有說明）'}")
        path = anomaly_notifier.safe_evidence_path(event.get("evidence_path"))
        if path is None:
            self._set_preview(None, "這筆事件沒有證據照片")
            return
        try:
            with Image.open(path) as img:
                img = img.convert("RGB")
                img.thumbnail(PREVIEW_SIZE)
                self._set_preview(ctk.CTkImage(light_image=img.copy(), dark_image=img.copy(), size=img.size), "")
        except Exception:
            self._set_preview(None, "證據照片無法顯示（檔案可能損壞）")

    def _set_preview(self, image, text):
        """顯示預覽圖片，或沒有圖片時顯示說明文字。"""
        self._preview_image = image
        self.preview.configure(image=image, text=text)

    def open_photo(self):
        """用系統看圖軟體開啟選取事件的證據原圖（路徑先確認在證據資料夾內）。"""
        event = self._selected_event()
        if event is None:
            ui_theme.show_message(self.winfo_toplevel(), "提示", "請先選取一筆事件。")
            return
        path = anomaly_notifier.safe_evidence_path(event.get("evidence_path"))
        if path is None:
            ui_theme.show_message(self.winfo_toplevel(), "提示", "這筆事件沒有可開啟的證據照片。")
            return
        os.startfile(path)  # noqa: S606  路徑已由 safe_evidence_path 限制在證據資料夾內

    def mark_all_read(self):
        """把目前看到的事件全部標為已讀，並通知通知列更新紅點。"""
        if self._on_mark_read is not None:
            self._on_mark_read()
        else:
            self.notifier.mark_all_read()
        self.refresh()


def open_anomaly_window(root, notifier=None, on_mark_read=None):
    """開啟異常事件視窗並回傳它。"""
    win = ctk.CTkToplevel(root)
    win.title("異常事件")
    win.geometry("1180x700")
    ui_theme.style_window(win)
    AnomalyPanel(win, notifier, on_mark_read).pack(fill="both", expand=True)
    return win

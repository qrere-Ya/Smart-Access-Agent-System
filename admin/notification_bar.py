"""
notification_bar.py：後台的異常通知元件（紅點未讀數＋新事件彈窗，CustomTkinter）。

主要功能：
  - NotificationBar：放在後台底部的小區塊，顯示「🔔 異常 N」，有未讀時為紅色
  - 用 Tk after 在主執行緒定時輪詢；同一輪有多筆新事件只彈出一個彈窗（合併顯示）
  - 「全部標為已讀」按鈕清除紅點；視窗銷毀後自動停止輪詢

串接模組：
  - anomaly_notifier：未讀與已讀邏輯、彈窗文字（format_popup_text）
  - ui_theme：色彩、字型、按鈕
  - backend_main：在底部狀態列掛載，teardown 前呼叫 stop()
"""
import tkinter as tk

import customtkinter as ctk

from admin import anomaly_notifier
from shared import ui_theme

POLL_MS = 5000


class NotificationBar:
    """異常通知列：紅點未讀數、合併彈窗、全部標為已讀。"""

    def __init__(self, master, notifier=None, poll_ms=POLL_MS, on_open=None):
        self.master = master
        self._on_open = on_open                 # 「查看」要開的異常事件視窗，由主視窗提供
        self.notifier = notifier or anomaly_notifier.AnomalyNotifier()
        self.poll_ms = poll_ms
        self._alive = True
        self._popup = None
        self._popup_label = None
        self.text_var = tk.StringVar(master=master, value="🔔 異常 0")
        self.frame = ctk.CTkFrame(master, fg_color="transparent", height=1)
        self.label = ctk.CTkLabel(self.frame, textvariable=self.text_var, font=ui_theme.font(ui_theme.FONT_SIZE_SMALL),
                                  text_color=ui_theme.pair("text_muted"))
        self.label.pack(side="left")
        if on_open is not None:                 # 點紅點文字也能查看
            self.label.configure(cursor="hand2")
            self.label.bind("<Button-1>", lambda e: self.open_list())
            ui_theme.link_button(self.frame, "查看", self.open_list, width=50, height=24).pack(side="left", padx=(6, 0))
        ui_theme.link_button(self.frame, "全部標為已讀", self.mark_all_read, width=90, height=24).pack(
            side="left", padx=(6, 0))
        self._tick()

    def _tick(self):
        """輪詢一次並排下一次；視窗已銷毀就停止。"""
        if not self._alive or not self.master.winfo_exists():
            return
        self.refresh()
        self.master.after(self.poll_ms, self._tick)

    def refresh(self):
        """更新紅點與彈窗。"""
        result = self.notifier.poll()
        if result is None:
            return
        self._show_count(result.unread_count, result.truncated)
        if result.new:
            self._show_popup(result.new)

    def _show_count(self, count, truncated=False):
        """顯示未讀數；大於 0 時為紅色。"""
        self.text_var.set(f"🔔 異常 {count}{'+' if truncated else ''}")
        self.label.configure(text_color=ui_theme.pair("danger" if count else "text_muted"))

    def _show_popup(self, events):
        """彈出（或更新）單一彈窗，避免洗版。"""
        text = f"新增 {len(events)} 筆異常事件\n\n{anomaly_notifier.format_popup_text(events)}"
        if self._popup is not None and self._popup.winfo_exists():
            self._popup_label.configure(text=text)
            self._popup.lift()
            return
        top = ctk.CTkToplevel(self.master)
        top.title("異常通知")
        top.attributes("-topmost", True)
        top.resizable(False, False)
        ui_theme.style_window(top)
        self._popup_label = ctk.CTkLabel(top, text=text, font=ui_theme.font(), text_color=ui_theme.pair("text"),
                                         justify="left", anchor="w", wraplength=460)
        self._popup_label.pack(padx=ui_theme.PAD_LG, pady=(ui_theme.PAD_LG, ui_theme.PAD_SM), anchor="w")
        row = ctk.CTkFrame(top, fg_color="transparent", height=1)
        row.pack(fill="x", padx=ui_theme.PAD_LG, pady=(ui_theme.PAD_SM, ui_theme.PAD_LG))
        ui_theme.danger_button(row, "全部標為已讀", lambda: (self.mark_all_read(), top.destroy()),
                               width=110).pack(side="left")
        if self._on_open is not None:
            ui_theme.primary_button(row, "查看", lambda: (top.destroy(), self.open_list()), width=70).pack(
                side="left", padx=(8, 0))
        ui_theme.secondary_button(row, "關閉", top.destroy, width=80).pack(side="right")
        self._popup = top

    def open_list(self):
        """開啟異常事件列表視窗（沒有提供時什麼都不做）。"""
        if self._on_open is not None:
            self._on_open()

    def mark_all_read(self):
        """標為已讀並清掉紅點。"""
        self.notifier.mark_all_read()
        self._show_count(0)

    def stop(self):
        """停止輪詢並關掉彈窗。"""
        self._alive = False
        if self._popup is not None and self._popup.winfo_exists():
            self._popup.destroy()

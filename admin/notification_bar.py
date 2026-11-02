"""
notification_bar.py：後台的異常通知元件（紅點未讀數＋新事件彈窗）。

主要功能：
  - NotificationBar：放在後台底部的小區塊，顯示「🔔 異常 N」，有未讀時為紅色
  - 用 Tk after 在主執行緒定時輪詢；同一輪有多筆新事件只彈出一個彈窗（合併顯示）
  - 「全部標為已讀」按鈕清除紅點；視窗銷毀後自動停止輪詢

串接模組：
  - anomaly_notifier：未讀與已讀邏輯
  - backend_main：在底部狀態列掛載，teardown 前呼叫 stop()
"""
import tkinter as tk

import ttkbootstrap as ttk
from ttkbootstrap.constants import DANGER, LEFT, LINK, RIGHT, SECONDARY

from admin import anomaly_notifier
from shared import event_labels

POLL_MS = 5000
MAX_LINES = 5   # 彈窗最多列出幾筆，其餘以「…另有 N 筆」帶過


def format_popup_text(events):
    """把多筆新事件合併成一段彈窗文字。"""
    lines = [f"{e.get('timestamp', '')}  {event_labels.describe(e.get('event_type', ''), e.get('detail'))}"
             for e in events[:MAX_LINES]]
    if len(events) > MAX_LINES:
        lines.append(f"…另有 {len(events) - MAX_LINES} 筆")
    return "\n".join(lines)


class NotificationBar:
    """異常通知列：紅點未讀數、合併彈窗、全部標為已讀。"""

    def __init__(self, master, notifier=None, poll_ms=POLL_MS):
        self.master = master
        self.notifier = notifier or anomaly_notifier.AnomalyNotifier()
        self.poll_ms = poll_ms
        self._alive = True
        self._popup = None
        self._popup_label = None
        self.text_var = ttk.StringVar(value="🔔 異常 0")
        self.frame = ttk.Frame(master)
        self.label = ttk.Label(self.frame, textvariable=self.text_var, font=("微軟正黑體", 9), bootstyle=SECONDARY)
        self.label.pack(side=LEFT)
        ttk.Button(self.frame, text="全部標為已讀", bootstyle=(SECONDARY, LINK),
                   command=self.mark_all_read).pack(side=LEFT, padx=(6, 0))
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
        self.label.configure(bootstyle=DANGER if count else SECONDARY)

    def _show_popup(self, events):
        """彈出（或更新）單一彈窗，避免洗版。"""
        text = f"新增 {len(events)} 筆異常事件\n\n{format_popup_text(events)}"
        if self._popup is not None and self._popup.winfo_exists():
            self._popup_label.configure(text=text)
            self._popup.lift()
            return
        top = tk.Toplevel(self.master)
        top.title("異常通知")
        top.attributes("-topmost", True)
        self._popup_label = ttk.Label(top, text=text, justify=LEFT, padding=16)
        self._popup_label.pack()
        row = ttk.Frame(top, padding=(16, 0, 16, 12))
        row.pack()
        ttk.Button(row, text="全部標為已讀", bootstyle=DANGER,
                   command=lambda: (self.mark_all_read(), top.destroy())).pack(side=LEFT, padx=4)
        ttk.Button(row, text="關閉", bootstyle=SECONDARY, command=top.destroy).pack(side=RIGHT, padx=4)
        self._popup = top

    def mark_all_read(self):
        """標為已讀並清掉紅點。"""
        self.notifier.mark_all_read()
        self._show_count(0)

    def stop(self):
        """停止輪詢並關掉彈窗。"""
        self._alive = False
        if self._popup is not None and self._popup.winfo_exists():
            self._popup.destroy()

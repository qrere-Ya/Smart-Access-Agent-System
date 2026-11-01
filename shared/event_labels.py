"""
event_labels.py：異常事件類型的中文顯示名稱（助理回答與後台通知共用）。

主要功能：
  - label(event_type)：有對照的回傳中文名稱，沒有對照的原樣回傳（舊事件與未知類型不會消失）

串接模組：
  - rag.answer：異常事件列表
  - admin.notification_bar：通知彈窗
"""

EVENT_LABELS = {"tailgate": "尾隨", "no_scan_walkby": "經過鏡頭未打卡"}


def label(event_type):
    """事件類型的中文名稱；沒有對照就回傳原字串。"""
    return EVENT_LABELS.get(event_type, event_type)

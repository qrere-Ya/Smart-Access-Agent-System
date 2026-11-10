"""
event_labels.py：異常事件類型的中文顯示名稱（助理回答與後台通知共用）。

主要功能：
  - label(event_type)：有對照的回傳中文名稱，沒有對照的原樣回傳（舊事件與未知類型不會消失）
  - describe(event_type, detail)：「中文名稱　說明」的顯示文字；說明為空就只顯示名稱

串接模組：
  - rag.answer：異常事件列表
  - admin.notification_bar：通知彈窗
"""

EVENT_LABELS = {"tailgate": "尾隨", "no_scan_walkby": "經過鏡頭未打卡", "login_failed": "後台登入失敗",
                "login_locked": "後台登入鎖定", "admin_changed": "管理者異動"}


def label(event_type):
    """事件類型的中文名稱；沒有對照就回傳原字串。"""
    return EVENT_LABELS.get(event_type, event_type)


def describe(event_type, detail="", sep="  "):
    """事件的顯示文字：中文名稱，有說明才接在後面（名稱已說明事情的事件不必再寫一次）。"""
    detail = (detail or "").strip()
    name = label(event_type)
    return f"{name}{sep}{detail}" if detail else name

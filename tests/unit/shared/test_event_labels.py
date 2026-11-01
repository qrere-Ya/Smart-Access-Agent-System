"""event_labels 單元測試：已知類型顯示中文，未知類型原樣保留，通知彈窗用中文。"""
from admin import notification_bar
from shared import event_labels


def test_known_types_get_chinese_names_and_unknown_types_are_kept():
    assert event_labels.label("tailgate") == "尾隨"
    assert event_labels.label("no_scan_walkby") == "經過鏡頭未打卡"
    assert event_labels.label("login_failed") == "login_failed"


def test_the_popup_text_uses_the_chinese_names():
    text = notification_bar.format_popup_text(
        [{"timestamp": "2026-11-02 09:00:00", "event_type": "no_scan_walkby", "detail": "有人經過"}])
    assert "經過鏡頭未打卡" in text and "no_scan_walkby" not in text

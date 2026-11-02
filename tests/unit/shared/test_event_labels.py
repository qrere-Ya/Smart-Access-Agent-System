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


def test_describe_shows_the_detail_only_when_there_is_one():
    assert event_labels.describe("no_scan_walkby", "") == "經過鏡頭未打卡"
    assert event_labels.describe("no_scan_walkby", None) == "經過鏡頭未打卡"
    assert event_labels.describe("tailgate", "兩人同進", "　") == "尾隨　兩人同進"


def test_the_popup_does_not_repeat_the_event_name_for_a_walkby():
    from access import pass_through

    text = notification_bar.format_popup_text(
        [{"timestamp": "2026-11-02 09:00:00", "event_type": pass_through.EVENT_TYPE,
          "detail": pass_through.EVENT_DETAIL}])
    assert text.count("經過鏡頭未打卡") == 1

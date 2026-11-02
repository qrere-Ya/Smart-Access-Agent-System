"""
後台畫面自動截圖：開啟每日出勤報告與異常通知彈窗，存到 runtime/screenshots/ 並檢查不是空白。
截圖給人看外觀；測試只負責確認視窗開得起來、有畫出東西。
"""
import pytest

pytestmark = pytest.mark.integration


def test_report_window_screenshot(root, db, add_user, screenshot):
    from admin import report_panel

    add_user("員工甲", "E1")
    add_user("員工乙", "E2")
    win = report_panel.open_report_window(root)
    win.geometry("900x560+80+80")
    assert screenshot(win, "後台_每日出勤報告")


def test_notification_popup_screenshot(root, db, tmp_path, screenshot):
    from admin import anomaly_notifier, notification_bar
    from shared import database_mgr as dbm

    bar = notification_bar.NotificationBar(
        root, notifier=anomaly_notifier.AnomalyNotifier(str(tmp_path / "seen.json")), poll_ms=60000)
    dbm.add_anomaly_event("tailgate", None, "兩人同時進入")
    dbm.add_anomaly_event("no_scan_walkby", None, "有人經過鏡頭未打卡")
    bar.refresh()
    assert screenshot(bar._popup, "後台_異常通知彈窗")
    bar.stop()

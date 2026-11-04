"""
後台新功能整合測試：真的 Tk 視窗（需要顯示器）。
驗證：每日出勤報告面板能查詢與匯出 CSV、日期錯誤不當機；異常通知列顯示紅點、新事件合併成單一彈窗、標為已讀後紅點消失。
"""

import pytest

pytestmark = pytest.mark.integration
pytest.importorskip("customtkinter")


# --- 每日出勤報告 ---

def test_report_panel_lists_employees_and_exports_csv(root, db, add_user, tmp_path, monkeypatch):
    from tkinter import filedialog

    from admin import report_panel
    from shared import ui_theme

    add_user("員工甲", "E1")
    add_user("=員工乙", "E2")
    out = tmp_path / "report.csv"
    monkeypatch.setattr(filedialog, "asksaveasfilename", lambda **k: str(out))
    shown = []
    monkeypatch.setattr(ui_theme, "show_message", lambda *a, **k: shown.append(a))
    panel = report_panel.ReportPanel(root)
    panel.pack()
    root.update()
    assert len(panel.tree.get_children()) == 2
    panel.export()
    text = out.read_text(encoding="utf-8-sig")
    assert "員工甲" in text and "'=員工乙" in text          # 公式開頭的欄位被加上單引號
    assert shown and "已匯出 2 筆" in shown[0][2]


def test_report_panel_survives_a_bad_date(root, db, when_dialog_appears, dialog_texts, click_button):
    from admin import report_panel

    panel = report_panel.ReportPanel(root)
    panel.pack()
    panel.date_var.set("2026-13-99'; DROP TABLE Users;--")
    seen = []

    def close(dialog):
        seen.extend(dialog_texts(dialog))
        click_button(dialog, "確定")

    errors = when_dialog_appears(close)
    panel.refresh()                                              # 真的跳出模態對話框，由輪詢的函式按掉
    root.update()
    assert not errors, errors
    assert any("日期" in t for t in seen) and not any("Traceback" in t for t in seen)
    assert panel.winfo_exists()


def test_report_panel_failure_message_hides_the_exception(root, db, monkeypatch, when_dialog_appears,
                                                           dialog_texts, click_button):
    from admin import attendance_report, report_panel

    panel = report_panel.ReportPanel(root)
    panel.pack()

    def boom(_date):
        raise RuntimeError("secret-path /data/x.db")

    monkeypatch.setattr(attendance_report, "build_report", boom)
    seen = []
    errors = when_dialog_appears(lambda d: (seen.extend(dialog_texts(d)), click_button(d, "確定")))
    panel.refresh()
    assert not errors, errors
    assert seen and not any("secret-path" in t for t in seen)


def test_report_table_scrolls_with_many_rows(root, db, add_user):
    from admin import report_panel

    for i in range(60):
        add_user(f"員工{i}", f"E{i}")
    panel = report_panel.ReportPanel(root)
    panel.pack(fill="both", expand=True)
    root.deiconify()
    root.update()
    assert len(panel.tree.get_children()) == 60
    assert panel.tree.yview()[1] < 1.0                           # 一頁放不下，捲軸有作用
    assert panel.tree.tag_has("odd", panel.tree.get_children()[0])


# --- 異常通知 ---

def test_notification_bar_shows_unread_merges_popup_and_clears(root, db, tmp_path):
    from admin import anomaly_notifier, notification_bar
    from shared import database_mgr as dbm

    notifier = anomaly_notifier.AnomalyNotifier(str(tmp_path / "seen.json"))
    bar = notification_bar.NotificationBar(root, notifier=notifier, poll_ms=60000)
    bar.frame.pack()
    assert bar.text_var.get() == "🔔 異常 0"
    dbm.add_anomaly_event("unauthorized", None, "事件一")
    dbm.add_anomaly_event("tailgate", None, "事件二")
    bar.refresh()
    root.update()
    assert bar.text_var.get() == "🔔 異常 2"
    assert bar._popup is not None and bar._popup.winfo_exists()
    first_popup = bar._popup
    dbm.add_anomaly_event("tailgate", None, "事件三")
    bar.refresh()
    assert bar._popup is first_popup                            # 彈窗已開著時只更新文字，不重複開
    bar.mark_all_read()
    assert bar.text_var.get() == "🔔 異常 0"
    bar.stop()

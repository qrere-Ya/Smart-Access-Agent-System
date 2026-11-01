"""
後台新功能整合測試：真的 Tk 視窗（需要顯示器）。
驗證：每日出勤報告面板能查詢與匯出 CSV、日期錯誤不當機；異常通知列顯示紅點、新事件合併成單一彈窗、標為已讀後紅點消失。
"""

import pytest

pytestmark = pytest.mark.integration


# --- 每日出勤報告 ---

def test_report_panel_lists_employees_and_exports_csv(root, db, add_user, tmp_path, monkeypatch):
    from tkinter import filedialog, messagebox

    from admin import report_panel

    add_user("員工甲", "E1")
    add_user("=員工乙", "E2")
    out = tmp_path / "report.csv"
    monkeypatch.setattr(filedialog, "asksaveasfilename", lambda **k: str(out))
    monkeypatch.setattr(messagebox, "showinfo", lambda *a, **k: None)
    panel = report_panel.ReportPanel(root)
    panel.pack()
    root.update()
    assert len(panel.tree.get_children()) == 2
    panel.export()
    text = out.read_text(encoding="utf-8-sig")
    assert "員工甲" in text and "'=員工乙" in text          # 公式開頭的欄位被加上單引號


def test_report_panel_survives_a_bad_date(root, db, monkeypatch):
    from tkinter import messagebox

    from admin import report_panel

    shown = []
    for name in ("showerror", "showwarning", "showinfo"):
        monkeypatch.setattr(messagebox, name, lambda *a, **k: shown.append(a))
    panel = report_panel.ReportPanel(root)
    panel.pack()
    panel.date_var.set("2026-13-99'; DROP TABLE Users;--")
    panel.refresh()
    root.update()
    assert panel.winfo_exists()


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

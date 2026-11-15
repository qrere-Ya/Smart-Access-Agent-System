"""
出勤報告頁整合測試：真的 Tk 視窗（需要顯示器）。
驗證：頁面只剩每日出勤報告（沒有假日、請假、每週休息日的分頁與按鈕）、顯示假日資料來源（沒有資料時明講只算週六日）、
有快取時顯示最後更新時間、主選單的「出勤報告」卡片直接換頁開啟。
"""
import pytest

pytestmark = pytest.mark.integration
pytest.importorskip("customtkinter")


def _texts(widget):
    found = []
    for child in widget.winfo_children():
        try:
            found.append(child.cget("text"))
        except Exception:  # noqa: BLE001, S110  不是每種元件都有 text
            pass
        found.extend(_texts(child))
    return found


@pytest.fixture
def panel(root, db, add_user):
    from admin import calendar_panel

    add_user("王小明", "E001")
    p = calendar_panel.CalendarPanel(root)
    p.pack(fill="both", expand=True)
    root.deiconify()
    root.update()
    return p


def test_the_page_has_only_the_daily_report(panel):
    texts = [t for t in _texts(panel) if isinstance(t, str)]
    assert panel.report_panel.report is not None
    assert not hasattr(panel, "tabs")
    assert not {"假日", "請假", "每週休息日", "新增請假", "新增／更新", "刪除所選"} & set(texts)
    assert "匯出 CSV" in texts


def test_the_page_says_the_holiday_source_and_falls_back_to_weekends_when_there_is_none(panel):
    text = panel.source_label.cget("text")
    assert "尚未取得" in text and "只算週六、週日" in text


def test_the_page_shows_the_last_update_when_a_calendar_is_cached(panel, tmp_path, monkeypatch):
    import datetime
    import json

    from shared import paths, work_calendar

    monkeypatch.setattr(paths, "WORK_CALENDAR_DIR", str(tmp_path / "wc"))
    year = datetime.date.today().year
    start = datetime.date(year, 1, 1)
    days = []
    while start.year == year:
        weekend = start.weekday() >= 5
        days.append({"date": start.strftime("%Y%m%d"), "week": "x", "isHoliday": weekend, "description": ""})
        start += datetime.timedelta(days=1)
    assert work_calendar.save_year(year, json.loads(json.dumps(days)), fetched_at="2026-11-01 08:00")
    panel.refresh_all()
    text = panel.source_label.cget("text")
    assert "最後更新 2026-11-01 08:00" in text and "社群維護" in text


def test_main_menu_card_switches_to_the_report_page(root, db):
    from admin.backend_main import BackendManagerApp

    root.deiconify()
    app = BackendManagerApp(root)
    root.update()

    def find(widget):
        for child in widget.winfo_children():
            try:
                if child.cget("text") == "出勤報告":
                    return child
            except Exception:  # noqa: BLE001, S110  不是每種元件都有 text
                pass
            found = find(child)
            if found is not None:
                return found
        return None

    title = find(root)
    assert title is not None
    getattr(title, "_label", title).event_generate("<Button-1>", x=5, y=5)   # CTkLabel 的事件綁在內部標籤上
    root.update()
    assert app.page_key == "calendar" and app.calendar_view.winfo_exists()      # 直接換頁，沒有另開視窗
    assert not [w for w in root.winfo_children() if w.winfo_class() == "Toplevel"]
    app.teardown()

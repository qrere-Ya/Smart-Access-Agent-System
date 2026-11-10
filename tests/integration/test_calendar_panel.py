"""
假日、請假與每週休息日畫面整合測試：真的 Tk 視窗（需要顯示器）。
驗證：新增與刪除寫進資料庫、不合法輸入用對話框說明且不寫入、資料庫例外不外洩內容、每週休息日儲存、
主選單的卡片能開啟這個畫面。模態對話框一律用輪詢等它出現再按，不用固定延遲。
"""
import sqlite3

import pytest

pytestmark = pytest.mark.integration
pytest.importorskip("customtkinter")


@pytest.fixture
def panel(root, db, add_user):
    from admin import calendar_panel

    add_user("王小明", "E001")
    add_user("陳大華", "E002")
    p = calendar_panel.CalendarPanel(root)
    p.pack(fill="both", expand=True)
    root.deiconify()
    root.update()
    return p


@pytest.fixture
def expect_dialog(when_dialog_appears, dialog_texts, click_button):
    """回傳函式 expect(按鈕文字='確定')：登記一個稍後會出現的對話框，回傳 (錯誤清單, 看到的文字清單)。"""
    def expect(button="確定"):
        seen = []

        def act(dialog):
            seen.extend(dialog_texts(dialog))
            click_button(dialog, button)

        return when_dialog_appears(act), seen

    return expect


def test_add_and_remove_a_holiday(panel, expect_dialog):
    from shared import database_mgr as db

    panel.holiday_date_var.set("2026-12-25")
    panel.holiday_name_var.set("行憲紀念日")
    panel.add_holiday()
    assert db.list_holidays() == [{"date": "2026-12-25", "name": "行憲紀念日"}]
    assert panel.holiday_tree.get_children() == ("2026-12-25",)
    assert panel.holiday_name_var.get() == ""
    panel.holiday_tree.selection_set("2026-12-25")
    errors, seen = expect_dialog("確定")
    panel.remove_holidays()
    assert not errors, errors
    assert any("1 筆假日" in t for t in seen)
    assert db.list_holidays() == []


def test_cancelling_the_delete_keeps_the_holiday(panel, expect_dialog):
    from shared import database_mgr as db

    db.add_holiday("2026-12-25", "行憲紀念日")
    panel.refresh_holidays()
    panel.holiday_tree.selection_set("2026-12-25")
    errors, _ = expect_dialog("取消")
    panel.remove_holidays()
    assert not errors, errors
    assert len(db.list_holidays()) == 1


@pytest.mark.parametrize("date_text,name,expected", [
    ("2026-02-30", "不存在的日子", "日期"),
    ("明天", "亂打", "日期"),
    ("2026-12-25", "", "假日名稱"),
    ("2026-12-25", "長" * 31, "最多"),
    ("2026-12-25", "含\x07控制字元", "特殊字元"),
    ("2026-12-25'); DROP TABLE Holidays;--", "注入", "日期"),
])
def test_bad_holiday_input_is_explained_and_not_saved(panel, expect_dialog, date_text, name, expected):
    from shared import database_mgr as db

    panel.holiday_date_var.set(date_text)
    panel.holiday_name_var.set(name)
    errors, seen = expect_dialog()
    panel.add_holiday()
    assert not errors, errors
    assert any(expected in t for t in seen), seen
    assert db.list_holidays() == []
    assert panel.winfo_exists()


def test_removing_with_nothing_selected_says_so(panel, expect_dialog):
    errors, seen = expect_dialog()
    panel.remove_holidays()
    assert not errors, errors
    assert any("請先選取" in t for t in seen)


def test_database_errors_do_not_leak_details(panel, expect_dialog, monkeypatch):
    from shared import database_mgr as db

    def boom(*a, **k):
        raise sqlite3.OperationalError("disk I/O error at C:/secret/path.db")

    monkeypatch.setattr(db, "add_holiday", boom)
    panel.holiday_name_var.set("某假日")
    errors, seen = expect_dialog()
    panel.add_holiday()
    assert not errors, errors
    assert seen and not any("secret" in t or "disk" in t for t in seen)
    assert any("操作失敗" in t for t in seen)


def test_add_and_remove_a_leave(panel, expect_dialog):
    from shared import database_mgr as db

    panel.leave_user.set("陳大華（E002）")
    panel.leave_kind.set("病假")
    panel.leave_start_var.set("2026-11-10")
    panel.leave_end_var.set("2026-11-12")
    panel.leave_note_var.set("感冒")
    panel.add_leave()
    leaves = db.list_leaves()
    assert len(leaves) == 1 and leaves[0]["kind"] == "病假" and leaves[0]["note"] == "感冒"
    assert leaves[0]["start_date"] == "2026-11-10" and leaves[0]["end_date"] == "2026-11-12"
    rows = panel.leave_tree.get_children()
    assert len(rows) == 1 and panel.leave_tree.item(rows[0], "values")[0] == "陳大華"
    assert panel.leave_tree.item(rows[0], "values")[3] in (3, "3")
    panel.leave_tree.selection_set(rows[0])
    errors, _ = expect_dialog("確定")
    panel.remove_leaves()
    assert not errors, errors
    assert db.list_leaves() == []


@pytest.mark.parametrize("start,end,expected", [
    ("2026-11-12", "2026-11-10", "不可晚於"),
    ("2026-11-10", "2030-11-10", "最多 366 天"),
    ("abc", "2026-11-10", "起始日期"),
    ("2026-11-10", "2026-13-10", "結束日期"),
])
def test_bad_leave_dates_are_explained_and_not_saved(panel, expect_dialog, start, end, expected):
    from shared import database_mgr as db

    panel.leave_start_var.set(start)
    panel.leave_end_var.set(end)
    errors, seen = expect_dialog()
    panel.add_leave()
    assert not errors, errors
    assert any(expected in t for t in seen), seen
    assert db.list_leaves() == []


def test_leave_without_choosing_an_employee_is_refused(panel, expect_dialog):
    from admin import account_admin
    from shared import database_mgr as db

    for u in account_admin.list_users():
        account_admin.delete_user(u["id"])
    panel.refresh_users()
    assert panel.leave_user.get() == ""
    errors, seen = expect_dialog()
    panel.add_leave()
    assert not errors, errors
    assert any("選擇員工" in t for t in seen)
    assert db.list_leaves() == []


def test_a_note_with_markup_is_stored_and_shown_as_plain_text(panel):
    from shared import database_mgr as db

    note = "{bad} \\n <b>x</b> 'quote' \"dq\" $var [list]"
    panel.leave_note_var.set(note)
    panel.add_leave()
    assert db.list_leaves()[0]["note"] == note
    row = panel.leave_tree.get_children()[0]
    assert panel.leave_tree.item(row, "values")[5] == note


def test_rest_days_load_save_and_reload(panel, expect_dialog):
    from shared import database_mgr as db

    assert [v.get() for v in panel.rest_vars] == [False] * 5 + [True, True]     # 預設週六、週日
    panel.rest_vars[4].set(True)
    panel.rest_vars[6].set(False)
    errors, seen = expect_dialog()
    panel.save_rest_days()
    assert not errors, errors
    assert db.get_rest_weekdays() == [4, 5]
    assert "週五、週六" in panel.rest_status.cget("text")


def test_rest_days_can_be_cleared(panel, expect_dialog):
    from shared import database_mgr as db

    for var in panel.rest_vars:
        var.set(False)
    errors, _ = expect_dialog()
    panel.save_rest_days()
    assert not errors, errors
    assert db.get_rest_weekdays() == []


def test_deleted_employees_leave_does_not_break_the_list(panel):
    from admin import account_admin
    from shared import database_mgr as db

    uid = account_admin.list_users()[0]["id"]
    db.add_leave(uid, "2026-11-10", "2026-11-10", "事假", "")
    account_admin.delete_user(uid)
    panel.refresh_leaves()
    row = panel.leave_tree.get_children()[0]
    assert "已刪除員工" in panel.leave_tree.item(row, "values")[0]


def test_main_menu_card_opens_the_calendar_window(root, db, monkeypatch):
    from admin import calendar_panel
    from admin.backend_main import BackendManagerApp

    opened = []
    monkeypatch.setattr(calendar_panel, "open_calendar_window", lambda r: opened.append(r))
    root.deiconify()
    app = BackendManagerApp(root)
    root.update()

    def find(widget):
        for child in widget.winfo_children():
            try:
                if child.cget("text") == "出勤報告與假日請假":
                    return child
            except Exception:  # noqa: BLE001  不是每種元件都有 text
                pass
            found = find(child)
            if found is not None:
                return found
        return None

    title = find(root)
    assert title is not None
    getattr(title, "_label", title).event_generate("<Button-1>", x=5, y=5)   # CTkLabel 的事件綁在內部標籤上
    root.update()
    assert opened == [root]
    app.teardown()


def test_the_daily_report_is_the_first_tab_and_is_requeried_when_selected(panel, db, monkeypatch):
    from admin import attendance_report

    assert panel.tabs.get() == "每日出勤報告"
    assert panel.report_panel.report is not None
    calls = []
    real = attendance_report.build_report
    monkeypatch.setattr(attendance_report, "build_report", lambda d: calls.append(d) or real(d))
    panel.tabs.set("假日")
    panel._on_tab_change()
    assert calls == []
    panel.tabs.set("每日出勤報告")
    panel._on_tab_change()
    assert len(calls) == 1

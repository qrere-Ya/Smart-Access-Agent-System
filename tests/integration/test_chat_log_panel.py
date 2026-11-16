"""
對話紀錄分頁整合測試：真的 Tk 視窗（需要顯示器）。
驗證：列表與篩選、點一筆看問題／回答／引用（純文字）、審查標記與清除並記下審查者、統計只有各標記筆數沒有準確率、
匯出前提示個資、匯出的 CSV、不合法日期說明原因、資料庫出錯不外洩、報告頁的分頁切換與審查者來自登入的管理者。
"""
import csv
import datetime

import pytest

pytestmark = pytest.mark.integration
pytest.importorskip("customtkinter")

NOW = datetime.datetime(2026, 11, 16, 10, 0, 0)


def cite(title="請假辦法", text="第1條 每年七天特別休假", score=0.91):
    return {"title": title, "label": f"{title} 第1條", "score": score, "text": text}


@pytest.fixture
def seeded(db):
    from shared import chat_log

    ids = {
        "front": chat_log.record(chat_log.SOURCE_FRONT, "特休有幾天？", "七天 [1]", [cite()], now=NOW),
        "back": chat_log.record(chat_log.SOURCE_BACK, "王小明這個月遲到幾次", "兩次", [
            {"title": "出缺勤查詢：count", "label": "查詢結果", "score": None, "text": "遲到：2 次"}],
            now=NOW - datetime.timedelta(hours=1)),
        "markup": chat_log.record(chat_log.SOURCE_FRONT, "<b>粗體</b> http://evil.example", "<script>x</script> **a**",
                                  [cite(text="<a href='http://evil.example'>連結</a>")],
                                  now=NOW - datetime.timedelta(hours=2)),
    }
    return ids


@pytest.fixture
def messages(monkeypatch):
    from shared import ui_theme

    seen = []
    monkeypatch.setattr(ui_theme, "show_message", lambda master, title, text, kind="info": seen.append((kind, text)))
    return seen


@pytest.fixture
def panel(root, db, seeded, messages):
    from admin import chat_log_panel

    p = chat_log_panel.ChatLogPanel(root, reviewer="管理者甲")
    p.pack(fill="both", expand=True)
    root.deiconify()
    root.update()
    return p


def _texts(widget):
    found = []
    for child in widget.winfo_children():
        try:
            found.append(child.cget("text"))
        except Exception:  # noqa: BLE001, S110  不是每種元件都有 text
            pass
        found.extend(_texts(child))
    return found


def set_dates(panel, start="", end=""):
    for entry, value in ((panel.start_entry, start), (panel.end_entry, end)):
        entry.delete(0, "end")
        if value:
            entry.insert(0, value)


def detail(panel):
    return panel.detail.get("1.0", "end")


def select(panel, log_id):
    panel.tree.selection_set(str(log_id))
    panel.show_selected()
    panel.update()


def test_the_list_shows_every_log_newest_first_with_a_summary_line(panel, seeded):
    rows = [panel.tree.item(i, "values") for i in panel.tree.get_children()]
    assert [r[3] for r in rows][0] == "特休有幾天？" and len(rows) == 3
    assert rows[0][1] == "前台知識庫" and rows[1][1] == "後台出缺勤" and rows[0][2] == "未審查"
    assert panel.summary_var.get() == "共 3 筆｜已審查 0 筆：正確 0、錯誤 0、幻覺 0；未審查 3 筆"


def test_the_privacy_and_retention_notice_is_shown(panel):
    from admin import chat_log_panel

    assert chat_log_panel.PRIVACY_NOTICE in _texts(panel) and "30 天" in chat_log_panel.PRIVACY_NOTICE


def test_clicking_a_row_shows_the_question_the_answer_and_the_passages(panel, seeded):
    select(panel, seeded["front"])
    text = detail(panel)
    assert "【問題】\n特休有幾天？" in text and "【AI 回答】\n七天 [1]" in text
    assert "[1] 請假辦法　請假辦法 第1條　分數 0.91" in text and "第1條 每年七天特別休假" in text
    select(panel, seeded["back"])
    assert "出缺勤查詢：count" in detail(panel) and "遲到：2 次" in detail(panel)


def test_markup_and_links_are_shown_as_plain_text(panel, seeded):
    select(panel, seeded["markup"])
    text = detail(panel)
    assert "<b>粗體</b> http://evil.example" in text and "<script>x</script> **a**" in text
    assert "<a href='http://evil.example'>連結</a>" in text
    assert panel.detail.cget("state") == "disabled"                       # 只能看不能改
    assert not panel.detail.tag_names("1.0") or "sel" in panel.detail.tag_names("1.0")


def test_review_buttons_start_disabled_and_enable_with_a_selection(panel, seeded):
    assert all(b.cget("state") == "disabled" for b in panel.review_buttons.values())
    select(panel, seeded["front"])
    assert all(b.cget("state") == "normal" for b in panel.review_buttons.values())


def test_reviewing_a_log_records_the_reviewer_updates_the_list_and_the_summary(panel, seeded):
    from shared import chat_log

    select(panel, seeded["front"])
    panel.review(chat_log.REVIEW_HALLUCINATION)
    row = chat_log.get_log(seeded["front"])
    assert row["review"] == "hallucination" and row["reviewer"] == "管理者甲" and row["reviewed_at"]
    assert panel.tree.item(str(seeded["front"]), "values")[2] == "幻覺"
    assert panel.selected_id() == seeded["front"] and "管理者甲" in detail(panel)         # 停在同一筆，詳情更新
    assert panel.summary_var.get() == "共 3 筆｜已審查 1 筆：正確 0、錯誤 0、幻覺 1；未審查 2 筆"
    panel.review(chat_log.REVIEW_CORRECT)
    assert chat_log.get_log(seeded["front"])["review"] == "correct"
    panel.review(chat_log.REVIEW_NONE)
    assert chat_log.get_log(seeded["front"])["review"] == "unreviewed"
    assert "準確" not in panel.summary_var.get() and "%" not in panel.summary_var.get()


def test_reviewing_without_a_selection_says_so(panel, messages):
    from shared import chat_log

    panel.review(chat_log.REVIEW_WRONG)
    assert messages == [("info", "請先選一筆紀錄。")]


def test_filters_by_source_review_and_date(panel, seeded):
    from shared import chat_log

    panel.source_box.set("後台出缺勤")
    panel.refresh()
    assert [int(i) for i in panel.tree.get_children()] == [seeded["back"]]
    assert panel.summary_var.get().startswith("共 1 筆")
    panel.source_box.set("全部來源")
    chat_log.set_review(seeded["markup"], chat_log.REVIEW_WRONG)
    panel.review_box.set("錯誤")
    panel.refresh()
    assert [int(i) for i in panel.tree.get_children()] == [seeded["markup"]]
    panel.review_box.set("全部審查結果")
    set_dates(panel, "2026-11-17")
    panel.refresh()
    assert panel.tree.get_children() == ()
    set_dates(panel, "2026-11-16", "2026-11-16")
    panel.refresh()
    assert len(panel.tree.get_children()) == 3


def test_a_bad_date_is_explained_and_nothing_changes(panel, seeded, messages):
    set_dates(panel, "11/16")
    panel.refresh()
    assert messages and messages[-1][0] == "warning" and "YYYY-MM-DD" in messages[-1][1]
    assert len(panel.tree.get_children()) == 3


def test_exporting_warns_about_personal_data_then_writes_a_csv(panel, seeded, monkeypatch, tmp_path):
    from tkinter import filedialog

    from admin import chat_log_panel
    from shared import ui_theme

    asked = []
    monkeypatch.setattr(ui_theme, "confirm", lambda master, title, message, **kw: asked.append(message) or True)
    out = tmp_path / "logs.csv"
    monkeypatch.setattr(filedialog, "asksaveasfilename", lambda **kw: str(out))
    panel.export()
    assert asked == [chat_log_panel.EXPORT_WARNING] and "個資" in asked[0]
    rows = list(csv.reader(out.read_text(encoding="utf-8-sig").splitlines(True)))
    assert len(rows) == 4 and rows[0][0] == "時間" and {r[2] for r in rows[1:]} >= {"特休有幾天？"}


def test_declining_the_warning_or_the_save_dialog_exports_nothing(panel, seeded, monkeypatch, tmp_path):
    from tkinter import filedialog

    from shared import ui_theme

    out = tmp_path / "never.csv"
    monkeypatch.setattr(filedialog, "asksaveasfilename", lambda **kw: str(out))
    monkeypatch.setattr(ui_theme, "confirm", lambda *a, **k: False)
    panel.export()
    assert not out.exists()
    monkeypatch.setattr(ui_theme, "confirm", lambda *a, **k: True)
    monkeypatch.setattr(filedialog, "asksaveasfilename", lambda **kw: "")
    panel.export()
    assert not out.exists()


def test_an_export_that_cannot_be_written_does_not_leak_details(panel, seeded, monkeypatch, tmp_path, messages):
    from tkinter import filedialog

    from shared import ui_theme

    monkeypatch.setattr(ui_theme, "confirm", lambda *a, **k: True)
    monkeypatch.setattr(filedialog, "asksaveasfilename", lambda **kw: str(tmp_path / "no_such_dir" / "x.csv"))
    panel.export()
    assert messages[-1] == ("error", "操作失敗，請稍後再試。")


def test_database_errors_do_not_leak_details(panel, seeded, monkeypatch, messages):
    from shared import chat_log

    def boom(*a, **k):
        raise OSError("C:/secret/path/database.db is locked")

    monkeypatch.setattr(chat_log, "list_logs", boom)
    panel.refresh()
    assert messages[-1] == ("error", "操作失敗，請稍後再試。") and "secret" not in str(messages)


def test_a_log_deleted_while_open_is_reported_instead_of_crashing(panel, seeded):
    import sqlite3

    from shared import database_mgr

    select(panel, seeded["front"])
    with sqlite3.connect(database_mgr.DB_PATH) as con:
        con.execute("DELETE FROM ChatLogs WHERE id = ?", (seeded["front"],))
    panel.show_selected()
    assert "已經不存在" in detail(panel)
    assert all(b.cget("state") == "disabled" for b in panel.review_buttons.values())


def test_the_report_page_has_the_chat_log_tab_and_uses_the_logged_in_admin_as_reviewer(root, db, seeded, messages):
    from admin.backend_main import BackendManagerApp
    from shared import auth, chat_log

    session = auth.Session("管理者乙")
    auth.save_session(session)                      # 後台每幾秒會確認登入代碼仍有效，沒存檔會被當成已登出
    root.deiconify()
    app = BackendManagerApp(root, session=session)
    root.update()
    app.open_calendar()
    root.update()
    panel = app.calendar_view
    panel.tabs.set("對話紀錄")
    panel._on_tab_change()
    root.update()
    log_panel = panel.chat_log_panel
    assert len(log_panel.tree.get_children()) == 3
    select(log_panel, seeded["front"])
    log_panel.review(chat_log.REVIEW_CORRECT)
    assert chat_log.get_log(seeded["front"])["reviewer"] == "管理者乙"
    panel.tabs.set("每日出勤報告")
    panel._on_tab_change()
    assert panel.report_panel.report is not None
    app.teardown()

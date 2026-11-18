"""
console_panel 的單元測試：小終端機的寫入與行數上限（用假的文字框，不開視窗）。
這個模組需要 Tkinter 與 customtkinter，環境沒有時整個檔案略過。
"""
import pytest

pytest.importorskip("tkinter")
pytest.importorskip("customtkinter")

from admin import console_panel  # noqa: E402


class FakeText:
    """模擬 Tk 文字框：記錄寫入的行、刪除範圍與是否捲到底。"""

    def __init__(self, exists=True, fail=False):
        self.lines, self.deleted, self.scrolled, self.exists, self.fail = [], [], False, exists, fail

    def winfo_exists(self):
        return self.exists

    def insert(self, position, text):
        if self.fail:
            raise RuntimeError("視窗已銷毀")
        self.lines.append(text)

    def index(self, position):
        return f"{len(self.lines) + 1}.0"

    def delete(self, start, end):
        self.deleted.append((start, end))

    def see(self, position):
        self.scrolled = True


def test_append_line_writes_and_scrolls_to_the_end():
    widget = FakeText()
    console_panel.append_line(widget, "你好")
    assert widget.lines == ["你好\n"] and widget.scrolled and widget.deleted == []


def test_append_line_ignores_missing_or_destroyed_widgets():
    console_panel.append_line(None, "沒有視窗")
    destroyed = FakeText(exists=False)
    console_panel.append_line(destroyed, "已關閉")
    assert destroyed.lines == []


def test_append_line_swallows_errors_from_a_closing_window():
    console_panel.append_line(FakeText(fail=True), "關閉瞬間的殘留回呼")


def test_append_line_keeps_only_the_latest_500_lines():
    widget = FakeText()
    for i in range(502):
        console_panel.append_line(widget, str(i))
    assert widget.deleted[-1] == ("1.0", "3.0")
    assert widget.deleted[0] == ("1.0", "1.0")


def test_append_line_makes_a_readonly_box_writable_only_while_inserting():
    class Box(FakeText):
        def __init__(self):
            super().__init__()
            self.states = []

        def configure(self, **kw):
            self.states.append(kw["state"])

    box = Box()
    console_panel.append_line(box, "x")
    assert box.states == ["normal", "disabled"]


def test_recent_events_merge_punches_and_front_anomalies_newest_first(db, add_user):
    import sqlite3
    from admin import dashboard_stats
    add_user("甲", "E1")
    con = sqlite3.connect(db)
    con.execute("INSERT INTO Attendance (name, timestamp, status, user_id) "
                "VALUES ('甲', '2026-11-10 08:00:00', '上班', 1)")
    con.execute("INSERT INTO AnomalyEvents (timestamp, event_type, detail, event_count) "
                "VALUES ('2026-11-10 08:05:00', 'no_scan_walkby', '', 1)")
    con.execute("INSERT INTO AnomalyEvents (timestamp, event_type, detail, event_count) "
                "VALUES ('2026-11-10 08:10:00', 'tailgate', '畫面有 2 人', 1)")
    con.execute("INSERT INTO AnomalyEvents (timestamp, event_type, detail, event_count) "
                "VALUES ('2026-11-10 08:20:00', 'login_failed', '管理者人臉登入失敗', 1)")   # 後台自己的事件不列入
    con.commit()
    con.close()
    rows = dashboard_stats.get_recent_events(limit=10)
    assert [r[0] for r in rows] == ["2026-11-10 08:10:00", "2026-11-10 08:05:00", "2026-11-10 08:00:00"]
    assert rows[0][1].startswith("尾隨") and "畫面有 2 人" in rows[0][1]
    assert rows[1][1] == "經過鏡頭未打卡" and rows[2][1] == "打卡　甲　上班"
    assert len(dashboard_stats.get_recent_events(limit=2)) == 2


def test_recent_events_return_none_when_the_database_cannot_be_read(monkeypatch, tmp_path):
    from admin import dashboard_stats
    monkeypatch.setattr(dashboard_stats.database_mgr, "DB_PATH", str(tmp_path / "missing" / "no.db"))
    assert dashboard_stats.get_recent_events() is None


def test_the_link_check_text_lists_only_what_needs_attention_and_the_last_heartbeat_age():
    from admin.backend_main import link_check_lines
    ok = link_check_lines([("OK", "a"), ("OK", "b")], {"seen": True, "age": 12.7})
    assert ok == ["檢查 2 項：全部正常", "前台最後一次心跳：12 秒前"]
    bad = link_check_lines([("OK", "a"), ("FAIL", "UDP 8772 沒有人在聽"), ("WARN", "網路類別是公用")],
                           {"seen": False, "age": None})
    assert bad[0] == "檢查 3 項：2 項要處理" and "❌ UDP 8772 沒有人在聽" in bad and "還沒收到過前台的心跳" in bad[-1]
    assert not any("a" == line for line in bad)

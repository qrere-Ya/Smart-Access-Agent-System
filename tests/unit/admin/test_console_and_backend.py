"""
console_panel 與 backend_main 的單元測試：小終端機的寫入與行數上限、顏色混合計算。
這兩個模組需要 Tkinter 與 ttkbootstrap，環境沒有時整個檔案略過。
"""
import pytest

pytest.importorskip("tkinter")
pytest.importorskip("ttkbootstrap")
pytest.importorskip("PIL")

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


def test_blend_hex_interpolates_between_two_colors():
    pytest.importorskip("pyqrcode")
    pytest.importorskip("png")
    from admin.backend_main import BackendManagerApp

    blend = BackendManagerApp._blend_hex
    assert blend("#000000", "#ffffff", 0.5) == "#808080"
    assert blend("#102030", "#ffffff", 0) == "#102030"
    assert blend("#102030", "#ffffff", 1) == "#ffffff"
    assert blend("ff0000", "#0000ff", 0.5) == "#800080"

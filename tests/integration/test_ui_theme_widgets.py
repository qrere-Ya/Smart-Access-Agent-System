"""shared/ui_theme.py 的元件整合測試：真的 Tk 視窗（需要顯示器）。驗證建構函式、徽章、對話框置中與按鈕結果、
表格樣式亮暗色。"""
import gc
import tkinter

import pytest

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def _tk_root():
    """整個模組共用一個主視窗：同一個行程反覆建立 Tk 根視窗會當機。"""
    import customtkinter as ctk

    from shared import ui_theme

    ui_theme.apply_theme("dark")
    try:
        app = ctk.CTk()
    except tkinter.TclError:
        pytest.skip("沒有可用的顯示器")
    app.geometry("600x400+100+100")
    app.update()
    yield app
    try:
        app.destroy()
    except tkinter.TclError:
        pass
    gc.collect()


@pytest.fixture
def root(_tk_root):
    yield _tk_root
    for child in _tk_root.winfo_children():
        child.destroy()
    gc.collect()


def test_builders_create_widgets(root):
    from shared import ui_theme as t

    frame = t.card(root)
    frame.pack()
    for make in (t.primary_button, t.secondary_button, t.danger_button):
        make(frame, "按鈕", lambda: None).pack()
    t.section_title(frame, "標題").pack()
    root.update()
    assert len(frame.winfo_children()) == 4


def test_badge_changes_kind(root):
    from shared import ui_theme as t

    badge = t.status_badge(root, "正常", "success")
    set_to = t.pair("danger")
    t.set_badge(badge, "異常", "danger")
    assert badge.cget("text") == "異常" and badge.cget("fg_color") == set_to
    with pytest.raises(ValueError):
        t.set_badge(badge, "x", "nope")


def test_confirm_returns_button_choice(root):
    from shared import ui_theme as t

    def click_button(label):
        dialog = next(w for w in root.winfo_children() if w.winfo_class() == "Toplevel")
        def walk(w):
            for c in w.winfo_children():
                yield c
                yield from walk(c)
        for w in walk(dialog):
            try:
                if w.cget("text") == label and hasattr(w, "invoke"):
                    w.invoke()
                    return
            except (tkinter.TclError, ValueError, AttributeError):
                continue
        raise AssertionError(label)

    root.after(200, lambda: click_button("確定"))
    assert t.confirm(root, "確認", "要刪除嗎？", danger=True) is True
    root.after(200, lambda: click_button("取消"))
    assert t.confirm(root, "確認", "要刪除嗎？") is False


def test_message_dialog_is_centered_and_grabs(root):
    from shared import ui_theme as t

    seen = {}

    def inspect():
        dialog = next(w for w in root.winfo_children() if w.winfo_class() == "Toplevel")
        seen["grab"] = dialog.grab_current() is not None
        seen["cx"] = dialog.winfo_rootx() + dialog.winfo_width() // 2
        seen["rx"] = root.winfo_rootx() + root.winfo_width() // 2
        dialog.destroy()

    root.after(300, inspect)
    t.show_message(root, "提示", "完成")
    assert seen["grab"]
    assert abs(seen["cx"] - seen["rx"]) < 40


@pytest.mark.parametrize("mode", ["light", "dark"])
def test_style_treeview_applies_colors(root, mode):
    from tkinter import ttk

    from shared import ui_theme as t

    tree = ttk.Treeview(root, columns=("a",), show="headings")
    name = t.style_treeview(tree, mode=mode)
    tree.heading("a", text="欄")
    for i in range(3):
        tree.insert("", "end", values=(i,), tags=t.zebra_tags(i))
    tree.pack()
    root.update()
    style = ttk.Style(root)
    assert style.lookup(name, "background") == t.COLORS[mode]["table_bg"]
    assert style.lookup(f"{name}.Heading", "background") == t.COLORS[mode]["table_head"]

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


def _when_dialog_appears(root, action, wait_ms=8000):
    """等對話框真的出現再執行 action（固定延遲在機器忙時會太早）；逾時或出錯就關掉對話框，測試失敗而不是卡住。"""
    errors = []
    waited = [0]

    def dialogs():
        return [w for w in root.winfo_children() if w.winfo_class() == "Toplevel"]

    def poll():
        found = dialogs()
        if found and found[-1].winfo_viewable():
            try:
                action(found[-1])
            except Exception as e:  # noqa: BLE001  任何錯誤都要先關掉對話框，否則模態視窗會讓測試永遠等下去
                errors.append(e)
                for d in dialogs():
                    d.destroy()
            return
        waited[0] += 50
        if waited[0] >= wait_ms:
            errors.append(AssertionError("對話框沒有出現"))
            for d in dialogs():
                d.destroy()
            return
        root.after(50, poll)

    root.after(50, poll)
    return errors


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
        def walk(w):
            for c in w.winfo_children():
                yield c
                yield from walk(c)
        def click(dialog):
            for w in walk(dialog):
                try:
                    if w.cget("text") == label and hasattr(w, "invoke"):
                        w.invoke()
                        return
                except (tkinter.TclError, ValueError, AttributeError):
                    continue
            raise AssertionError(label)

        return _when_dialog_appears(root, click)

    errors = click_button("確定")
    assert t.confirm(root, "確認", "要刪除嗎？", danger=True) is True
    errors += click_button("取消")
    assert t.confirm(root, "確認", "要刪除嗎？") is False
    assert not errors, errors


def test_message_dialog_is_centered_and_grabs(root):
    from shared import ui_theme as t

    seen = {}

    def inspect(dialog):
        seen["grab"] = dialog.grab_current() is not None
        seen["cx"] = dialog.winfo_rootx() + dialog.winfo_width() // 2
        seen["rx"] = root.winfo_rootx() + root.winfo_width() // 2
        dialog.destroy()

    errors = _when_dialog_appears(root, inspect)
    t.show_message(root, "提示", "完成")
    assert not errors, errors
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


def test_theme_sample_screenshot(root, screenshot):
    """把共用主題的各種元件排在一起截圖，方便肉眼檢查外觀。"""
    from shared import ui_theme as t

    frame = t.card(root)
    frame.pack(padx=16, pady=16, fill="both", expand=True)
    t.section_title(frame, "共用主題預覽").pack(anchor="w", padx=12, pady=(12, 4))
    for make, text in ((t.primary_button, "主要按鈕"), (t.secondary_button, "次要按鈕"),
                       (t.danger_button, "危險按鈕")):
        make(frame, text, lambda: None).pack(anchor="w", padx=12, pady=4)
    for text, kind in (("正常", "success"), ("異常", "danger"), ("一般", "neutral")):
        t.status_badge(frame, text, kind).pack(anchor="w", padx=12, pady=4)
    assert screenshot(root, "共用主題_元件預覽")


def test_dialog_height_follows_content_and_screenshot(root, screenshot):
    """對話框高度要由內容決定（曾因 CTkFrame 預設高 200 被撐成一條長長的色條），並截圖。"""
    from shared import ui_theme as t

    shots = {}

    def grab(dialog):
        shots["h"] = dialog.winfo_height()
        screenshot(dialog, "共用主題_確認對話框")
        dialog.destroy()

    errors = _when_dialog_appears(root, grab)
    t.confirm(root, "確認", "要刪除嗎？", danger=True)
    assert not errors, errors
    assert 0 < shots["h"] < 200

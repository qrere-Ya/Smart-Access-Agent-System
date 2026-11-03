"""
整合測試共用設定：真的 Tk 視窗需要顯示器。
整個測試行程只建立一個 customtkinter 主視窗（同一個行程反覆建立會讓 Tk 中止，正式程式也只建一個），
各測試檔透過 root 取用，每個測試結束清掉子視窗與全域綁定；screenshot 把視窗存成圖並檢查不是空白。
"""
import gc
import tkinter

import pytest


@pytest.fixture(scope="session")
def _tk_root():
    import customtkinter as ctk

    from shared import ui_theme

    ui_theme.apply_theme("dark")
    try:
        app = ctk.CTk()
    except tkinter.TclError:
        pytest.skip("沒有可用的顯示器")
    ui_theme.style_window(app)
    app.geometry("640x720+60+40")
    app.update()
    app.withdraw()
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
    for sequence in ("<Key>", "<Button>", "<Motion>", "<MouseWheel>"):
        _tk_root.unbind_all(sequence)
    _tk_root.withdraw()
    gc.collect()


@pytest.fixture
def appearance():
    """回傳函式 set_mode('light'|'dark')：切換外觀；測試結束還原成暗色。"""
    from shared import ui_theme

    def set_mode(mode):
        ui_theme.apply_theme(mode)

    yield set_mode
    ui_theme.apply_theme("dark")


@pytest.fixture
def screenshot():
    """回傳函式 shot(視窗, 檔名)：把視窗畫面存到 runtime/screenshots/，並檢查不是空白畫面。"""
    import os
    import time

    from PIL import ImageGrab, ImageStat

    from shared import paths

    out_dir = os.path.join(paths.PROJECT_ROOT, "runtime", "screenshots")
    os.makedirs(out_dir, exist_ok=True)

    def shot(widget, name):
        top = widget.winfo_toplevel()
        top.deiconify()
        top.lift()
        for _ in range(15):                       # 等淡入淡出動畫與重繪結束再截圖
            top.update()
            time.sleep(0.04)
        x, y, w, h = top.winfo_rootx(), top.winfo_rooty(), top.winfo_width(), top.winfo_height()
        img = ImageGrab.grab(bbox=(x, y, x + w, y + h), all_screens=True)
        path = os.path.join(out_dir, f"{name}.png")
        img.save(path)
        assert sum(ImageStat.Stat(img.convert("L")).stddev) > 1, f"{name} 畫面是空白的"
        return path

    return shot


def _walk(widget):
    for child in widget.winfo_children():
        yield child
        yield from _walk(child)


@pytest.fixture
def when_dialog_appears(root):
    """
    回傳函式 when(action, wait_ms=8000)：輪詢等模態對話框真的出現再執行 action(對話框)（固定延遲在機器忙時會太早）；
    逾時或出錯就關掉對話框，讓測試失敗而不是卡住。回傳錯誤清單，測試結尾要 assert 它是空的。
    """
    def when(action, wait_ms=8000):
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

    return when


@pytest.fixture
def dialog_texts():
    """回傳函式 texts(對話框)：取出對話框裡所有標籤文字，用來檢查提示內容。"""
    def texts(dialog):
        out = []
        for w in _walk(dialog):
            try:
                value = w.cget("text")
            except (tkinter.TclError, ValueError, AttributeError):
                continue
            if value:
                out.append(str(value))
        return out

    return texts


@pytest.fixture
def click_button():
    """回傳函式 click(視窗, 文字)：找到文字相符的按鈕並按下，找不到就丟 AssertionError。"""
    def click(window, label):
        for w in _walk(window):
            try:
                if w.cget("text") == label and hasattr(w, "invoke"):
                    w.invoke()
                    return
            except (tkinter.TclError, ValueError, AttributeError):
                continue
        raise AssertionError(f"找不到按鈕：{label}")

    return click

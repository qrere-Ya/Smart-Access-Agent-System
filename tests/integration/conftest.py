"""
整合測試共用設定：真的 Tk 視窗需要顯示器。
整個測試行程只建立一個 ttkbootstrap 主視窗（同一個行程反覆建立會讓 Tk 中止，正式程式也只建一個），
各測試檔透過 root 取用，每個測試結束清掉子視窗與全域綁定。
"""
import gc
import tkinter

import pytest


@pytest.fixture(scope="session")
def _tk_root():
    import ttkbootstrap as ttk

    try:
        app = ttk.Window(themename="superhero")
    except tkinter.TclError:
        pytest.skip("沒有可用的顯示器")
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

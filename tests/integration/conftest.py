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

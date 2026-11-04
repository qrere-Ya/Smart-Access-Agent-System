"""
後台真實啟動流程的回歸測試：另開一個全新的 Python 行程（測試共用的主視窗早就「存在」，同一行程測不出這個問題）。
驗證：先隱藏主視窗、模擬登入後 deiconify 並建立後台畫面，主視窗最後真的是可見的，
而不是被 customtkinter 的標題列處理藏起來。
"""
import os
import subprocess
import sys
import tempfile
import textwrap

import pytest

from shared import paths

pytestmark = pytest.mark.integration

SCRIPT = textwrap.dedent("""
    import sys
    from admin import backend_main

    app = backend_main.create_hidden_root()
    app.deiconify()                                   # 模擬登入視窗關閉後 main() 的下一步
    backend_main.BackendManagerApp(app, None)
    result = {}

    def check():
        result["viewable"] = bool(app.winfo_viewable())
        app.destroy()

    app.after(800, check)
    app.mainloop()
    print("VIEWABLE=" + str(result.get("viewable")))
""")


def test_the_main_window_is_visible_after_a_hidden_start_and_deiconify():
    env = dict(os.environ, SMART_ACCESS_RUNTIME_DIR=tempfile.mkdtemp(prefix="startup_"), PYTHONUTF8="1")
    try:
        out = subprocess.run([sys.executable, "-c", SCRIPT], cwd=paths.PROJECT_ROOT, env=env,  # noqa: S603
                             capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        pytest.fail("後台啟動腳本逾時")
    if "TclError" in out.stderr and "display" in out.stderr.lower():
        pytest.skip("沒有可用的顯示器")
    assert "VIEWABLE=True" in out.stdout, out.stdout[-500:] + out.stderr[-800:]

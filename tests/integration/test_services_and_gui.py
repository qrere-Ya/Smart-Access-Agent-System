"""
後台整合測試：用真正的 service_manager 啟動註冊伺服器子行程並呼叫 API；建立真的 Tk 後台視窗。
"""
import json
import urllib.error
import urllib.request

import pytest

pytestmark = pytest.mark.integration


def test_registration_server_starts_serves_and_stops_through_service_manager():
    from admin import service_manager as sm

    if sm.is_port_open("127.0.0.1", sm.PORT_WEB_SERVER):
        pytest.skip("5000 埠已被占用，不去動別人的服務")
    lines = []
    ok, _ = sm.start_reg_service(lines.append)
    try:
        assert ok
        assert sm.wait_port_open("127.0.0.1", sm.PORT_WEB_SERVER, timeout=120)
        base = f"http://127.0.0.1:{sm.PORT_WEB_SERVER}"
        token = json.loads(urllib.request.urlopen(base + "/api/internal/generate_token", timeout=5).read())["token"]  # noqa: S310
        assert len(token) == 8
        html = urllib.request.urlopen(base + "/?t=" + token, timeout=5).read().decode("utf-8")  # noqa: S310
        assert "員工自助註冊系統" in html
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(base + "/?t=badtoken", timeout=5)  # noqa: S310
        assert err.value.code == 403
    finally:
        sm.stop_reg_service()
    assert not sm.is_port_open("127.0.0.1", sm.PORT_WEB_SERVER)


def test_admin_window_builds_and_shows_service_status():
    import tkinter

    import ttkbootstrap as ttk

    try:
        root = ttk.Window(themename="superhero")
    except tkinter.TclError:
        pytest.skip("沒有可用的顯示器")
    try:
        from admin.backend_main import BackendManagerApp

        app = BackendManagerApp(root)
        root.update()
        assert app.rag_status_var.get().startswith("⚪ RAG")
    finally:
        root.destroy()

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


def test_admin_window_builds_and_shows_service_status(root):
    from admin.backend_main import BackendManagerApp

    root.deiconify()
    app = BackendManagerApp(root)
    root.update()
    assert app.face_status_var.get().startswith("⚪")


def test_back_role_dashboard_shows_the_front_link_and_the_event_stream(root, db, add_user, add_attendance,
                                                                      monkeypatch, tmp_path):
    """雙機後台：營運狀態頁用真的視窗顯示前台連線燈號（在線、離線、沒收過）與前台事件串流，且沒有人臉辨識開關。"""
    import time

    from admin import front_status, service_manager as sm
    from admin.backend_main import BackendManagerApp
    from shared import paths

    monkeypatch.setattr(paths, "FRONT_STATUS_PATH", str(tmp_path / "front_status.json"))
    monkeypatch.setattr(sm, "link_service_wanted", lambda settings=None: True)
    monkeypatch.setattr(sm, "link_service_running", lambda settings=None: True)
    monkeypatch.setattr(sm, "start_link_service", lambda on_line, settings=None: (True, "ok"))
    add_user("王小明", "E1")
    add_attendance("王小明", time.strftime("%Y-%m-%d") + " 08:55:00", "上班")
    root.deiconify()
    app = BackendManagerApp(root)
    root.withdraw()
    app.open_dashboard()
    app.dash_page.update()
    assert not hasattr(app, "face_btn") and app.link_wanted is True
    assert "還沒收到前台回報" in app.link_headline.cget("text")                    # 沒收過心跳

    front_status.record({"pending_events": 3, "rejected_events": 0, "dropped_events": 0, "faces": 4,
                         "faces_version": "ab", "clock_offset_seconds": 0.1, "clock_warning": False,
                         "recognition_running": True, "camera_ok": True}, "169.254.127.16")
    app._refresh_link_card()
    assert "前台連線中" in app.link_headline.cget("text")
    assert "待補送事件 3 筆" in app.link_detail.cget("text") and "鏡頭正常" in app.link_detail.cget("text")
    assert "連線服務執行中" in app.link_service_label.cget("text")
    rows = [app.event_tree.item(i, "values") for i in app.event_tree.get_children()]
    assert any("打卡　王小明　上班" in r[1] for r in rows)

    front_status.record({"pending_events": 0, "rejected_events": 0, "dropped_events": 0, "faces": 4,
                         "faces_version": "ab", "clock_offset_seconds": 0.0, "clock_warning": False,
                         "recognition_running": True, "camera_ok": True}, "x", clock=lambda: time.time() - 120)
    app._refresh_link_card()
    assert "前台離線" in app.link_headline.cget("text")
    app.teardown()


def test_main_menu_shows_the_front_connection_badge_only_for_the_back_role(root, db, monkeypatch, tmp_path):
    """雙機後台：主選單標題下就有前台連線徽章（未連線、連線中、辨識未執行、離線）；單機沒有。"""
    import time

    from admin import front_status, service_manager as sm
    from admin.backend_main import BackendManagerApp
    from shared import paths

    monkeypatch.setattr(paths, "FRONT_STATUS_PATH", str(tmp_path / "front_status.json"))
    monkeypatch.setattr(sm, "link_service_wanted", lambda settings=None: True)
    monkeypatch.setattr(sm, "link_service_running", lambda settings=None: True)
    monkeypatch.setattr(sm, "start_link_service", lambda on_line, settings=None: (True, "ok"))
    root.deiconify()
    app = BackendManagerApp(root)
    root.update()
    assert "前台未連線" in app.front_badge.cget("text")
    healthy = {"pending_events": 0, "rejected_events": 0, "dropped_events": 0, "faces": 4, "faces_version": "ab",
               "clock_offset_seconds": 0.0, "clock_warning": False, "recognition_running": True, "camera_ok": True}
    front_status.record(healthy, "169.254.1.2")
    app._refresh_front_badge()
    assert app.front_badge.cget("text") == "● 前台連線中"
    front_status.record({**healthy, "recognition_running": False}, "169.254.1.2")
    app._refresh_front_badge()
    assert "辨識未執行" in app.front_badge.cget("text")
    front_status.record(healthy, "x", clock=lambda: time.time() - 120)
    app._refresh_front_badge()
    assert "前台離線" in app.front_badge.cget("text")
    app.teardown()
    for child in root.winfo_children():
        child.destroy()
    monkeypatch.setattr(sm, "link_service_wanted", lambda settings=None: False)
    standalone = BackendManagerApp(root)
    root.update()
    assert not hasattr(standalone, "front_badge")
    standalone.teardown()


def test_standalone_dashboard_keeps_the_face_switch_and_does_not_start_a_link_service(root, db, monkeypatch):
    from admin import service_manager as sm
    from admin.backend_main import BackendManagerApp

    started = []
    monkeypatch.setattr(sm, "start_link_service", lambda *a, **k: started.append(1) or (True, ""))
    root.deiconify()
    app = BackendManagerApp(root)
    root.withdraw()
    app.open_dashboard()
    app.dash_page.update()
    assert app.link_wanted is False and hasattr(app, "face_btn") and started == []
    app.teardown()


def test_the_back_card_stops_and_resumes_the_front_recognition_with_a_confirmation(root, db, monkeypatch, tmp_path):
    """雙機後台：前台連線卡片的「停止前台辨識」要在卡片內二次確認才寫入設定，也能取消；單機模式沒有這個按鈕。"""
    from admin import service_manager as sm
    from admin.backend_main import BackendManagerApp
    from shared import front_control, paths

    monkeypatch.setattr(paths, "FRONT_CONTROL_PATH", str(tmp_path / "control.json"))
    monkeypatch.setattr(sm, "link_service_wanted", lambda settings=None: True)
    monkeypatch.setattr(sm, "link_service_running", lambda settings=None: True)
    monkeypatch.setattr(sm, "start_link_service", lambda on_line, settings=None: (True, "ok"))
    root.deiconify()
    app = BackendManagerApp(root)
    root.withdraw()
    app.open_dashboard()
    app.dash_page.update()
    assert "停止前台辨識" in app.face_control_btn.cget("text") and front_control.recognition_enabled() is True
    app.face_control_btn.invoke()                                                    # 第一次：只進入確認
    assert front_control.recognition_enabled() is True and "確定停止" in app.face_control_btn.cget("text")
    app.face_control_cancel.invoke()                                                 # 取消：沒有寫入
    assert front_control.recognition_enabled() is True and "停止前台辨識" in app.face_control_btn.cget("text")
    app.face_control_btn.invoke()
    app.face_control_btn.invoke()                                                    # 第二次：確定
    assert front_control.recognition_enabled() is False
    assert "恢復前台辨識" in app.face_control_btn.cget("text") and "已由後台停止" in app.face_control_label.cget("text")
    app.face_control_btn.invoke()
    app.face_control_btn.invoke()                                                    # 確定恢復
    assert front_control.recognition_enabled() is True and "停止前台辨識" in app.face_control_btn.cget("text")
    app.teardown()


def test_the_single_machine_dashboard_has_no_front_recognition_button(root, db, monkeypatch):
    from admin import service_manager as sm
    from admin.backend_main import BackendManagerApp

    monkeypatch.setattr(sm, "link_service_wanted", lambda settings=None: False)
    root.deiconify()
    app = BackendManagerApp(root)
    root.withdraw()
    app.open_dashboard()
    app.dash_page.update()
    assert not hasattr(app, "face_control_btn")
    app.teardown()


def test_back_card_front_recognition_control_screenshot(root, db, monkeypatch, tmp_path, screenshot):
    """截圖：雙機後台的前台連線卡片，辨識已由後台停止的狀態（按鈕變成「恢復前台辨識」）。"""
    from admin import front_status, service_manager as sm
    from admin.backend_main import BackendManagerApp
    from shared import front_control, paths

    monkeypatch.setattr(paths, "FRONT_CONTROL_PATH", str(tmp_path / "control.json"))
    monkeypatch.setattr(paths, "FRONT_STATUS_PATH", str(tmp_path / "front_status.json"))
    monkeypatch.setattr(sm, "link_service_wanted", lambda settings=None: True)
    monkeypatch.setattr(sm, "link_service_running", lambda settings=None: True)
    monkeypatch.setattr(sm, "start_link_service", lambda on_line, settings=None: (True, "ok"))
    front_status.record({"pending_events": 0, "rejected_events": 0, "dropped_events": 0, "faces": 4,
                         "faces_version": "ab", "clock_offset_seconds": 0.1, "clock_warning": False,
                         "recognition_running": False, "camera_ok": False}, "169.254.127.16")
    front_control.set_recognition_enabled(False)
    root.deiconify()
    app = BackendManagerApp(root)
    root.withdraw()
    app.open_dashboard()
    root.attributes("-topmost", True)
    app.dash_page.update()
    app._refresh_link_card()
    assert screenshot(root, "後台_前台辨識控制")
    app.teardown()

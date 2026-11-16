"""
後台畫面自動截圖：登入、主選單、每日出勤報告、異常通知彈窗、帳號管理、出勤報告頁，亮暗色各一張，
存到 runtime/screenshots/ 並檢查不是空白。截圖給人看外觀；測試只負責確認視窗開得起來、有畫出東西、
視窗高度沒被撐大（CTkFrame 預設高 200）。
"""
import time

import numpy as np
import pytest

pytestmark = pytest.mark.integration
pytest.importorskip("customtkinter")

MODES = ["dark", "light"]


class _Camera:
    def read(self):
        time.sleep(0.01)
        frame = np.full((360, 480, 3), 90, dtype=np.uint8)
        frame[80:300, 160:320] = (170, 190, 220)               # 假的臉部區塊，預覽才看得出有畫面
        return True, frame

    def release(self):
        pass


class _Vision:
    def detect(self, frame):
        return "rgb", []                                        # 沒偵測到臉：停在「請看鏡頭」的等待畫面

    def liveness(self, frame, box):
        return 0.99

    def embed(self, rgb, face):
        return np.ones(4, dtype=np.float32)

    def close(self):
        pass


def _seed(add_user, add_attendance):
    add_user("王小明", "E001")
    add_user("陳大華", "E002")
    add_user("林美玲", "E003")
    add_attendance("王小明", "2026-11-02 08:55:00", "準時")
    add_attendance("陳大華", "2026-11-02 09:20:00", "遲到")


@pytest.mark.parametrize("mode", MODES)
def test_login_window_screenshot(root, db, add_user, screenshot, appearance, mode):
    from admin import login_window
    from shared import auth

    appearance(mode)
    add_user("管理者甲", "A1", role="admin")
    guard = auth.LoginGuard(max_failures=5, lock_seconds=60)
    win = login_window.LoginWindow(root, camera_factory=lambda: _Camera(), vision_factory=lambda: _Vision(),
                                   guard=guard)
    end = time.time() + 2
    while time.time() < end:
        win.update()
        time.sleep(0.02)
    assert screenshot(win, f"後台_登入_{mode}")
    win._cancel()


@pytest.mark.parametrize("mode", MODES)
def test_login_setup_screenshot(root, db, add_user, screenshot, appearance, mode):
    from admin import login_window

    appearance(mode)
    add_user("組員乙", "E2")
    win = login_window.LoginWindow(root, camera_factory=lambda: _Camera(), vision_factory=lambda: _Vision())
    assert screenshot(win, f"後台_初次設定_{mode}")
    win._cancel()


@pytest.mark.parametrize("mode", MODES)
def test_main_menu_screenshot(root, db, screenshot, appearance, mode):
    from admin.backend_main import BackendManagerApp
    from shared import auth

    appearance(mode)
    session = auth.Session("管理者甲")
    auth.save_session(session)
    root.deiconify()
    app = BackendManagerApp(root, session)
    root.geometry("1280x820+20+10")
    assert screenshot(root, f"後台_主選單_{mode}")
    assert root.winfo_height() < 900
    app.teardown()
    auth.clear_session()


@pytest.mark.parametrize("mode", MODES)
def test_report_page_screenshot(root, db, add_user, add_attendance, screenshot, appearance, mode):
    from admin.backend_main import BackendManagerApp

    appearance(mode)
    _seed(add_user, add_attendance)
    root.deiconify()
    app = BackendManagerApp(root)
    root.geometry("1280x820+20+10")
    app.open_calendar()
    panel = app.calendar_view.report_panel
    panel.date_var.set("2026-11-02")
    panel.refresh()
    assert screenshot(root, f"後台_每日出勤報告_{mode}")
    app.teardown()


@pytest.mark.parametrize("mode", MODES)
def test_notification_popup_screenshot(root, db, tmp_path, screenshot, appearance, mode):
    from admin import anomaly_notifier, notification_bar
    from shared import database_mgr as dbm

    appearance(mode)
    bar = notification_bar.NotificationBar(
        root, notifier=anomaly_notifier.AnomalyNotifier(str(tmp_path / "seen.json")), poll_ms=60000)
    dbm.add_anomaly_event("tailgate", None, "兩人同時進入")
    dbm.add_anomaly_event("no_scan_walkby", None, "")
    bar.refresh()
    assert screenshot(bar._popup, f"後台_異常通知彈窗_{mode}")
    assert bar._popup.winfo_height() < 400
    bar.stop()


@pytest.mark.parametrize("mode", MODES)
def test_account_page_screenshot(root, db, add_user, add_attendance, screenshot, appearance, mode):
    from admin.backend_main import BackendManagerApp

    appearance(mode)
    _seed(add_user, add_attendance)
    add_user("管理者甲", "A1", role="admin")
    add_user("停用員工", "E9", active=0)
    root.deiconify()
    app = BackendManagerApp(root)
    root.geometry("1280x820+20+10")
    app.open_account_admin()
    assert screenshot(root, f"後台_員工帳號管理_{mode}")
    app.teardown()


@pytest.mark.parametrize("mode", MODES)
def test_attendance_report_page_screenshot(root, db, add_user, add_attendance, screenshot, appearance, mode):
    from admin.backend_main import BackendManagerApp

    appearance(mode)
    _seed(add_user, add_attendance)
    root.deiconify()
    app = BackendManagerApp(root)
    root.geometry("1280x820+20+10")
    app.open_calendar()
    assert screenshot(root, f"後台_出勤報告_{mode}")
    app.teardown()


@pytest.mark.parametrize("mode", MODES)
def test_dashboard_window_screenshot(root, db, add_user, add_attendance, screenshot, appearance, mode):
    from admin.backend_main import BackendManagerApp

    appearance(mode)
    _seed(add_user, add_attendance)
    today = time.strftime("%Y-%m-%d")
    add_attendance("王小明", f"{today} 08:55:00", "上班")
    add_attendance("陳大華", f"{today} 09:20:00", "上班 (遲到)")
    root.deiconify()
    app = BackendManagerApp(root)
    root.withdraw()                                             # 主視窗不要蓋住要截圖的子視窗
    app.open_dashboard()
    root.geometry("1280x820+20+10")
    assert screenshot(root, f"後台_營運狀態_{mode}")
    assert root.winfo_height() < 900
    app.teardown()


@pytest.mark.parametrize("mode", MODES)
def test_qr_window_shows_a_qr_image_from_the_server(root, db, screenshot, appearance, mode, monkeypatch):
    """用本機假的註冊伺服器回 Token，確認 QR Code 以 CTkImage 顯示（5000 埠被占用就略過）。"""
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    from admin.backend_main import BackendManagerApp

    monkeypatch.setattr(BackendManagerApp, "_detect_qr_channels", staticmethod(lambda: [
        {"ip": "172.20.10.7", "adapter": "Wi-Fi", "kind": "lan", "label": "Wi-Fi 172.20.10.7（區網）"},
        {"ip": "26.244.154.160", "adapter": "Radmin VPN", "kind": "vpn",
         "label": "Radmin VPN 26.244.154.160（VPN／其他，手機通常連不到）"}]))

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({"token": "abcd1234"}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    try:
        server = HTTPServer(("127.0.0.1", 5000), Handler)
    except OSError:
        pytest.skip("5000 埠已被占用")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        appearance(mode)
        root.deiconify()
        app = BackendManagerApp(root)
        root.withdraw()
        app.open_qr_registration()
        root.geometry("1280x820+20+10")
        deadline = time.time() + 5                              # Token 與位址在背景取得，等結果回到畫面
        while time.time() < deadline and app._qr_image is None:
            root.update()
            time.sleep(0.02)
        assert "abcd1234" in app.url_label.cget("text")
        assert app._qr_image is not None
        assert screenshot(root, f"後台_QR註冊_{mode}")
        app.teardown()
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("mode", MODES)
def test_back_role_dashboard_window_screenshot(root, db, add_user, add_attendance, screenshot, appearance, mode,
                                               monkeypatch, tmp_path):
    """雙機後台的營運狀態頁（前台連線卡片與事件串流）在亮暗兩種外觀下都能畫出來。"""
    from admin import front_status, service_manager as sm
    from admin.backend_main import BackendManagerApp
    from shared import paths

    appearance(mode)
    monkeypatch.setattr(paths, "FRONT_STATUS_PATH", str(tmp_path / "front_status.json"))
    monkeypatch.setattr(sm, "link_service_wanted", lambda settings=None: True)
    monkeypatch.setattr(sm, "link_service_running", lambda settings=None: True)
    monkeypatch.setattr(sm, "start_link_service", lambda on_line, settings=None: (True, "ok"))
    _seed(add_user, add_attendance)
    today = time.strftime("%Y-%m-%d")
    add_attendance("王小明", f"{today} 08:55:00", "上班")
    front_status.record({"pending_events": 2, "rejected_events": 0, "dropped_events": 0, "faces": 3,
                         "faces_version": "ab", "clock_offset_seconds": 0.3, "clock_warning": False,
                         "recognition_running": True, "camera_ok": True}, "169.254.127.16")
    root.deiconify()
    app = BackendManagerApp(root)
    root.withdraw()
    app.open_dashboard()
    root.geometry("1280x820+20+10")
    assert screenshot(root, f"後台_營運狀態_雙機_{mode}")
    app.teardown()


@pytest.mark.parametrize("mode", MODES)
def test_chat_log_tab_screenshot(root, db, screenshot, appearance, mode):
    from admin.backend_main import BackendManagerApp
    from shared import chat_log

    appearance(mode)
    now = __import__("datetime").datetime.now()
    first = chat_log.record(chat_log.SOURCE_FRONT, "特別休假有幾天？", "工作滿一年每年七天 [1]。", [
        {"title": "勞動基準法", "label": "勞動基準法 第38條", "score": 0.93,
         "text": "勞工在同一雇主工作滿一年者，特別休假七日。"}],
        now=now)
    second = chat_log.record(chat_log.SOURCE_BACK, "測試甲這個月遲到幾次", "遲到兩次。", [
        {"title": "出缺勤查詢：count", "label": "查詢結果", "score": None, "text": "遲到：2 次"}], now=now)
    chat_log.record(chat_log.SOURCE_FRONT, "公司有幾個員工？", "文件中沒有提到。", [], now=now)
    chat_log.set_review(second, chat_log.REVIEW_CORRECT, "管理者甲")
    root.deiconify()
    app = BackendManagerApp(root)
    root.geometry("1280x820+20+10")
    app.open_calendar()
    app.calendar_view.tabs.set("對話紀錄")
    app.calendar_view._on_tab_change()
    panel = app.calendar_view.chat_log_panel
    panel.tree.selection_set(str(first))
    panel.show_selected()
    assert screenshot(root, f"後台_對話紀錄_{mode}")
    app.teardown()

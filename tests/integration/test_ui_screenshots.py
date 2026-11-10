"""
後台畫面自動截圖：登入、主選單、每日出勤報告、異常通知彈窗、帳號管理、假日與請假，亮暗色各一張，
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
    root.geometry("700x740+40+20")
    assert screenshot(root, f"後台_主選單_{mode}")
    assert root.winfo_height() < 900
    app.teardown()
    auth.clear_session()


@pytest.mark.parametrize("mode", MODES)
def test_report_window_screenshot(root, db, add_user, add_attendance, screenshot, appearance, mode):
    from admin import report_panel

    appearance(mode)
    _seed(add_user, add_attendance)
    win = report_panel.open_report_window(root)
    win.geometry("900x560+80+80")
    win.winfo_children()[0].date_var.set("2026-11-02")
    win.winfo_children()[0].refresh()
    assert screenshot(win, f"後台_每日出勤報告_{mode}")


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
def test_account_window_screenshot(root, db, add_user, add_attendance, screenshot, appearance, mode):
    from admin import account_panel

    appearance(mode)
    _seed(add_user, add_attendance)
    add_user("管理者甲", "A1", role="admin")
    add_user("停用員工", "E9", active=0)
    win = account_panel.open_account_window(root, "管理者甲")
    win.geometry("1000x600+60+60")
    assert screenshot(win, f"後台_員工帳號管理_{mode}")


@pytest.mark.parametrize("mode", MODES)
def test_calendar_window_screenshot(root, db, add_user, screenshot, appearance, mode):
    from admin import calendar_panel
    from shared import database_mgr as dbm

    appearance(mode)
    add_user("王小明", "E001")
    add_user("陳大華", "E002")
    dbm.add_holiday("2026-12-25", "行憲紀念日")
    dbm.add_holiday("2027-01-01", "開國紀念日")
    dbm.add_leave(1, "2026-11-10", "2026-11-12", "特休", "出國旅遊")
    win = calendar_panel.open_calendar_window(root)
    win.geometry("900x640+60+40")
    shots = [screenshot(win, f"後台_假日_{mode}")]
    panel = win.winfo_children()[0]
    panel.tabs.set("請假")
    panel.refresh_all()
    shots.append(screenshot(win, f"後台_請假_{mode}"))
    panel.tabs.set("每週休息日")
    shots.append(screenshot(win, f"後台_每週休息日_{mode}"))
    assert all(shots)


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
    app.dash_win.geometry("1260x700+20+20")
    assert screenshot(app.dash_win, f"後台_營運狀態_{mode}")
    assert app.dash_win.winfo_height() < 900
    app.teardown()


@pytest.mark.parametrize("mode", MODES)
def test_qr_window_shows_a_qr_image_from_the_server(root, db, screenshot, appearance, mode):
    """用本機假的註冊伺服器回 Token，確認 QR Code 以 CTkImage 顯示（5000 埠被占用就略過）。"""
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    from admin.backend_main import BackendManagerApp

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
        app.reg_win.geometry("820x620+20+20")
        assert "abcd1234" in app.url_label.cget("text")
        assert app._qr_image is not None
        assert screenshot(app.reg_win, f"後台_QR註冊_{mode}")
        app.reg_win.after_cancel(app.qr_timer_id)
        app.teardown()
    finally:
        server.shutdown()
        server.server_close()

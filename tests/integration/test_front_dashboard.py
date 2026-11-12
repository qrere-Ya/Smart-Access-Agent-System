"""前台儀表板（第二個螢幕）：真的 Tk 視窗——晨報分頁與換頁、打卡問候、警示橫幅出現與消失、狀態、主題切換並記住，
並在深色藍與半透明白兩種主題下截圖。"""
import time

import pytest

from access import front_dashboard as fd
from access.front_store import FrontStore, ScreenState

pytestmark = pytest.mark.integration

TS = 1200.0                      # 固定的假時鐘：第 1 頁晨報（int(TS // 20) % 3 == 0），打卡與警示都以它為基準

NEWS = [{"category": cat, "title": f"{cat}：第 {i} 則今日科技新聞的標題，故意寫得長一點看看換行",
         "what": "這是發生什麼事的說明，會說明這則新聞的重點與背景，方便在門口的螢幕上快速瀏覽。",
         "why": "會影響日常使用的軟體與設備", "how": "可以先評估是否要更新",
         "source": "iThome", "link": "https://example.com/" + str(i)}
        for i, cat in enumerate(["AI", "資安", "硬體與晶片", "軟體與開發", "AI", "資安", "AI"])]


@pytest.fixture
def seeded(tmp_path):
    store = FrontStore(str(tmp_path / "front.db"))
    screen = ScreenState(store)
    now = TS
    store.set_meta("morning", {"date": time.strftime("%Y-%m-%d"), "generated_at": "08:30", "news": NEWS,
                               "news_problem": ""})
    screen.note_punch("王小明", "早安", now=lambda: now - 40, clock_text="08:01")
    screen.note_punch("陳大華", "早安", now=lambda: now - 12, clock_text="08:03")
    store.set_link_state(True, now=lambda: TS)
    store.replace_faces("v" * 64, [{"id": 1, "name": "王小明", "employee_id": "E1", "role": "employee",
                                    "embedding": [0.0] * 512}])
    return store, screen


RES = {"cpu": 3.0, "ram": 42.0, "gpu": None, "down_kbps": 0.1, "up_kbps": 1536.0}


def build(root, store, heartbeat=None, resources=None, recognition=None, monitors=(), clock=None):
    from shared import ui_theme

    ui_theme.apply_theme("dark")
    for child in root.winfo_children():                      # 上一個測試留下的畫面要先清掉，不然會疊在一起
        child.destroy()
    root.deiconify()
    app = fd.FrontDashboard(root, store, clock=clock or (lambda: TS),
                            read_heartbeat=heartbeat or (lambda: {"camera_ok": True, "recognition_running": True}),
                            read_resources=resources or (lambda: RES), recognition=recognition, monitors=monitors)
    root.update()
    return app


def texts(widget):
    """畫面上所有文字：標籤的字，加上表格（Treeview）每一列的每個欄位。"""
    out = []
    if hasattr(widget, "get_children") and hasattr(widget, "item"):
        for iid in widget.get_children():
            out.extend(str(v) for v in widget.item(iid, "values"))
    for child in widget.winfo_children():
        try:
            out.append(child.cget("text"))
        except Exception:  # noqa: BLE001, S110
            pass
        out.extend(texts(child))
    return out


@pytest.fixture(autouse=True)
def restore(root):
    yield
    import customtkinter as ctk
    ctk.set_widget_scaling(1.0)
    root.attributes("-alpha", 1.0)
    ctk.set_appearance_mode("dark")


def test_dashboard_shows_news_greetings_status_and_no_alert(root, seeded):
    store, _screen = seeded
    app = build(root, store)
    everything = "\n".join(str(t) for t in texts(root))
    assert "今日科技早報" in everything and "AI：第 0 則" in everything
    assert "王小明" in everything and "陳大華" in everything and "早安" in everything
    assert "後台連線中" in everything and "鏡頭" in everything and "執行中" in everything
    assert not app.alert_frame.winfo_ismapped()
    app.open_module("news")
    assert app.news_page.cget("text") == "1 / 3"                              # 7 則 → 3 頁


def test_home_shows_module_cards_with_live_previews_and_cards_open_modules(root, seeded):
    store, _screen = seeded
    app = build(root, store)
    assert app.current is None and app.home.winfo_ismapped() and not app.detail.winfo_ismapped()
    assert set(app.tiles) == {"news", "greetings", "status"}
    assert app.tiles["greetings"]["summary"].cget("text") == "陳大華 早安"       # 最新一位
    assert "全部正常" in app.tiles["status"]["summary"].cget("text")
    app.tiles["news"]["title"]._label.event_generate("<Button-1>")                    # 點卡片裡的標題也要開
    root.update()
    root.update()
    assert app.current == "news" and app.detail.winfo_ismapped() and not app.home.winfo_ismapped()
    assert app.panels["news"].winfo_ismapped() and not app.panels["status"].winfo_ismapped()
    app.back_button.invoke()
    root.update()
    assert app.current is None and app.home.winfo_ismapped()


def test_home_shows_resources_recent_punches_date_and_big_clock(root, seeded):
    store, _screen = seeded
    app = build(root, store)
    assert app.meters["cpu"]["value"].cget("text") == "3%" and app.meters["ram"]["value"].cget("text") == "42%"
    assert app.meters["gpu"]["value"].cget("text") == "未偵測到"
    assert "MB/s" in app.net_up.cget("text") and "KB/s" in app.net_down.cget("text")
    assert "2026" in app.date_label.cget("text") or "年" in app.date_label.cget("text")
    assert ":" in app.time_label.cget("text")
    assert "陳大華" in " ".join(str(t) for t in texts(app.mini_greet))


def test_a_failing_resource_reader_does_not_break_the_screen(root, seeded):
    store, _screen = seeded
    app = build(root, store, resources=lambda: (_ for _ in ()).throw(RuntimeError("壞掉")))
    assert app.meters["cpu"]["value"].cget("text") == "—"


def test_a_module_screen_returns_home_when_nobody_touches_it(root, seeded):
    store, _screen = seeded
    clock = {"t": TS}
    from shared import ui_theme

    ui_theme.apply_theme("dark")
    root.deiconify()
    app = fd.FrontDashboard(root, store, clock=lambda: clock["t"], read_heartbeat=lambda: {},
                            read_resources=lambda: RES)
    app.open_module("status")
    clock["t"] = TS + fd.DETAIL_IDLE_SECONDS - 1
    app.refresh()
    assert app.current == "status"
    clock["t"] = TS + fd.DETAIL_IDLE_SECONDS + 1
    app.refresh()
    assert app.current is None


def test_a_broken_part_turns_the_status_card_into_a_warning(root, seeded):
    store, _screen = seeded
    app = build(root, store, heartbeat=lambda: {"camera_ok": False, "recognition_running": True})
    assert "鏡頭" in app.tiles["status"]["summary"].cget("text")


def test_alert_banner_appears_and_disappears(root, seeded):
    store, screen = seeded
    app = build(root, store)
    screen.set_alert("tailgate", "畫面有 2 人", now=lambda: TS)
    app.refresh()
    root.update()
    assert app.alert_frame.winfo_ismapped() and "尾隨" in app.alert_title.cget("text")
    assert "畫面有 2 人" in app.alert_detail.cget("text")
    store.set_meta("alert", {"kind": "tailgate", "detail": "x", "ts": TS - 61})   # 過期
    app.refresh()
    root.update()
    assert not app.alert_frame.winfo_ismapped()


def test_offline_back_and_a_dead_camera_are_shown_as_such(root, seeded):
    store, _screen = seeded
    store.set_meta("link_state", {"online": False, "ts": TS})
    app = build(root, store, heartbeat=lambda: {"camera_ok": False, "recognition_running": False})
    everything = "\n".join(str(t) for t in texts(root))
    assert "後台離線" in app.link_badge.cget("text") and "異常或未啟動" in everything and "未執行" in everything


def test_news_pages_rotate_with_time(root, seeded):
    store, _screen = seeded
    clock = {"t": TS}
    from shared import ui_theme

    ui_theme.apply_theme("dark")
    root.deiconify()
    app = fd.FrontDashboard(root, store, clock=lambda: clock["t"], read_heartbeat=lambda: {},
                            read_resources=lambda: RES)
    root.update()
    app.open_module("news")
    first = app.news_page.cget("text")
    clock["t"] = TS + 25.0
    app.refresh()
    assert app.news_page.cget("text") != first
    clock["t"] = TS + 25.5                                                          # 同一頁內不重畫
    app.refresh()


def test_theme_toggle_switches_appearance_and_is_remembered(root, seeded):
    store, screen = seeded
    app = build(root, store)
    assert app.theme == "dark" and float(root.attributes("-alpha")) == 1.0
    app.toggle_theme()
    root.update()
    assert app.theme == "frosted" and screen.theme() == "frosted"
    assert float(root.attributes("-alpha")) < 1.0 and "深色藍" in app.theme_button.cget("text")
    for child in root.winfo_children():
        child.destroy()
    # 重新開啟：記住上次的主題
    again = fd.FrontDashboard(root, store, clock=lambda: TS, read_heartbeat=lambda: {}, read_resources=lambda: RES)
    assert again.theme == "frosted"


def test_a_failing_refresh_does_not_stop_the_screen(root, seeded, capsys):
    store, _screen = seeded
    app = build(root, store)
    app._read_heartbeat = lambda: (_ for _ in ()).throw(RuntimeError("壞掉"))
    app.tick()                                                                 # 不丟例外
    assert "更新失敗" in capsys.readouterr().out


@pytest.mark.parametrize("theme", ["dark", "frosted"])
def test_dashboard_screenshot(root, seeded, screenshot, theme):
    store, screen = seeded
    screen.set_theme(theme)
    screen.set_alert("tailgate", "畫面有 2 人，只辨識到 1 張臉", now=lambda: TS)
    app = build(root, store)
    root.geometry("1920x1000+0+0")                                   # 扣掉工作列的可用高度
    assert screenshot(root, f"前台儀表板_{theme}")                      # 主畫面（模組卡片牆）
    assert app.theme == theme
    for key in ("news", "greetings", "status"):
        app.open_module(key)
        root.update()
        assert screenshot(root, f"前台儀表板_{theme}_{key}")


@pytest.mark.parametrize("size", [(1024, 576), (1280, 720), (1600, 900), (1920, 1000), (2560, 1400)])
def test_content_fits_the_window_at_any_size_on_home_and_every_module(root, seeded, size):
    """視窗放大縮小時，內容跟著縮放，主畫面與各模組畫面所需的高度都不超出視窗。"""
    store, screen = seeded
    screen.set_alert("tailgate", "畫面有 2 人", now=lambda: TS)                    # 最高的情況：有警示橫幅
    app = build(root, store)
    root.geometry(f"{size[0]}x{size[1]}+0+0")
    root.update()
    app._rescale()
    root.update()
    for key in (None, "news", "greetings", "status"):
        app.go_home() if key is None else app.open_module(key)
        root.update()
        need, have = root.winfo_reqheight(), root.winfo_height()
        assert need <= have + 2, (size, key, need, have)
        assert root.winfo_reqwidth() <= root.winfo_width() + 2, (size, key)


def test_hidden_parts_stay_hidden_when_the_window_is_rescaled(root, seeded):
    """CustomTkinter 縮放時會把用 grid_remove 藏起來的元件重新放回來（警示橫幅、模組畫面疊上主畫面）；要藏得住。"""
    import customtkinter as ctk
    store, _screen = seeded
    app = build(root, store)
    ctk.set_widget_scaling(1.2)
    root.update()
    assert not app.alert_frame.winfo_ismapped() and not app.detail.winfo_ismapped() and app.home.winfo_ismapped()
    app.open_module("status")
    ctk.set_widget_scaling(0.9)
    root.update()
    assert app.detail.winfo_ismapped() and not app.home.winfo_ismapped()
    assert app.panels["status"].winfo_ismapped() and not app.panels["news"].winfo_ismapped()


def test_shrinking_after_a_large_window_still_fits(root, seeded):
    """先放大再縮小（使用者拖曳視窗）：表格撐開的欄寬不能讓內容留在放大時的大小而被擠出去。"""
    store, screen = seeded
    screen.set_alert("tailgate", "畫面有 2 人", now=lambda: TS)
    app = build(root, store)
    for size in ((2200, 1240), (1280, 720), (1920, 1000), (1024, 576)):
        root.geometry(f"{size[0]}x{size[1]}+0+0")
        root.update()
        app._rescale()
        root.update()
        for key in (None, "news", "status"):
            app.go_home() if key is None else app.open_module(key)
            root.update()
            assert root.winfo_reqwidth() <= root.winfo_width() + 2, (size, key, root.winfo_reqwidth())
            need, have = root.winfo_reqheight(), root.winfo_height()
            assert need <= have + 2, (size, key, need, have)


class FakeRecognition:
    def __init__(self):
        self.on, self.starts, self.stops = False, 0, 0

    def running(self):
        return self.on

    def start(self):
        self.on, self.starts = True, self.starts + 1
        return True, "已啟動"

    def stop(self):
        self.on, self.stops = False, self.stops + 1


def test_face_recognition_is_off_until_started_then_the_button_is_disabled(root, seeded):
    store, _screen = seeded
    rec = FakeRecognition()
    app = build(root, store, recognition=rec)
    assert rec.starts == 0 and "啟動人臉辨識" in app.face_button.cget("text")     # 開前台不會自己開辨識
    assert app.face_button.cget("state") == "normal"
    app.start_recognition()
    assert rec.starts == 1 and "執行中" in app.face_button.cget("text")
    assert app.face_button.cget("state") == "disabled"                            # 執行中不能按、前台沒有停止


def test_the_front_can_never_stop_recognition(root, seeded):
    store, _screen = seeded
    rec = FakeRecognition()
    rec.on = True
    app = build(root, store, recognition=rec)
    app.start_recognition()                                                       # 已在執行：什麼都不做
    app.face_button.invoke()                                                      # 停用的按鈕按了也沒作用
    root.update()
    assert rec.starts == 0 and rec.stops == 0 and rec.on is True
    assert not hasattr(app, "toggle_recognition") and not hasattr(app, "exit_front")


def test_recognition_ending_by_itself_makes_the_button_startable_again(root, seeded):
    store, _screen = seeded
    rec = FakeRecognition()
    app = build(root, store, recognition=rec)
    app.start_recognition()
    rec.on = False                                                                # 使用者在辨識視窗按 q 結束
    app.refresh()
    assert "啟動人臉辨識" in app.face_button.cget("text") and app.face_button.cget("state") == "normal"
    app.start_recognition()
    assert rec.starts == 2


def test_the_dashboard_has_no_maintenance_or_admin_features(root, seeded):
    store, _screen = seeded
    app = build(root, store, recognition=FakeRecognition())
    everything = "\n".join(str(t) for t in texts(root))
    assert "維護" not in everything and "管理員" not in everything
    for name in ("maint_button", "open_maintenance", "maintenance", "MaintenanceWindow"):
        assert not hasattr(app, name) and not hasattr(fd, name)


def test_header_buttons_do_not_overflow_at_small_sizes(root, seeded):
    store, _screen = seeded
    app = build(root, store, recognition=FakeRecognition())
    for size in ("1920x1000", "1280x720", "1024x640"):
        root.geometry(size)
        root.update()
        end = time.time() + 0.5
        while time.time() < end:
            root.update()
            time.sleep(0.02)
        right_edge = max(w.winfo_rootx() + w.winfo_width() for w in (app.face_button, app.theme_button,
                                                                        app.fullscreen_button))
        assert right_edge <= root.winfo_rootx() + root.winfo_width() + 2, size
    root.geometry("1920x1000+0+0")
    root.update()
    app._rescale()
    root.update()


def test_without_a_recognition_manager_the_button_is_hidden(root, seeded):
    store, _screen = seeded
    app = build(root, store)
    assert app.face_button.winfo_manager() == ""
    app.start_recognition()                                                       # 不會出錯


def test_dashboard_opens_on_the_second_screen_and_f11_toggles_fullscreen(root, seeded, tmp_path, monkeypatch):
    from access import front_screens

    monkeypatch.setattr(front_screens.paths, "RUNTIME_DIR", str(tmp_path))
    store, _screen = seeded
    laptop = front_screens.Monitor(0, 0, 1920, 1080, 0, 0, 1920, 1040, True)
    external = front_screens.Monitor(1920, 0, 1600, 900, 1920, 0, 1600, 860, False)
    app = build(root, store, monitors=[laptop, external])
    assert app.monitor is external and app._window_geometry == f"1600x{860 - front_screens.TITLEBAR_ALLOWANCE}+1920+0"
    assert app.fullscreen is False
    root.event_generate("<F11>")
    root.update()
    assert app.fullscreen is True and bool(root.attributes("-fullscreen"))
    assert "離開全螢幕" in app.fullscreen_button.cget("text")
    assert front_screens.load_pref("dashboard_fullscreen") is True                    # 記住了
    root.event_generate("<Escape>")
    root.update()
    assert app.fullscreen is False and not root.attributes("-fullscreen")
    assert root.geometry().endswith("+1920+0") or root.geometry().startswith("1600x")   # 回到第二螢幕上的一般視窗
    app.toggle_fullscreen()
    app.toggle_fullscreen()
    assert app.fullscreen is False


def test_the_dashboard_starts_fullscreen_when_it_was_left_that_way(root, seeded, tmp_path, monkeypatch):
    from access import front_screens

    monkeypatch.setattr(front_screens.paths, "RUNTIME_DIR", str(tmp_path))
    front_screens.save_pref("dashboard_fullscreen", True)
    store, _screen = seeded
    app = build(root, store)
    root.update()
    root.after(400, root.quit)
    root.mainloop()
    assert app.fullscreen is True
    app.set_fullscreen(False)


def test_f11_still_works_after_the_window_was_shrunk(root, seeded, tmp_path, monkeypatch):
    """回歸：把視窗縮小後再按 F11，不能出現「max width/height is too small」。"""
    from access import front_screens

    monkeypatch.setattr(front_screens.paths, "RUNTIME_DIR", str(tmp_path))
    store, _screen = seeded
    app = build(root, store)
    root.geometry("1024x640+0+0")
    end = time.time() + 0.6
    while time.time() < end:
        root.update()
        time.sleep(0.02)
    app.set_fullscreen(True)
    assert app.fullscreen is True and bool(root.attributes("-fullscreen"))
    app.set_fullscreen(False)
    root.geometry("1920x1000+0+0")
    root.update()
    app._rescale()
    root.update()


def _qr_labels(root):
    return [t for t in texts(root) if "手機掃描看原文" in str(t)]


@pytest.mark.parametrize("local_env,back_value,qr_shown", [
    (None, None, True),          # 都沒設：顯示 QR
    (None, False, True),         # 後台關：顯示
    (None, True, False),         # 後台開：不顯示
    ("1", False, False),         # 前台本機開（環境變數）：不顯示
    ("1", True, False),          # 兩邊都開：不顯示
    (None, "yes", True),         # 後台值壞掉（不是布林）：當作關
])
def test_the_qr_codes_follow_the_front_local_switch_or_the_backs_synced_switch(root, seeded, tmp_path, monkeypatch,
                                                                                local_env, back_value, qr_shown):
    from access import front_agent
    from shared import network_policy, paths

    monkeypatch.setattr(paths, "NETWORK_POLICY_PATH", str(tmp_path / "policy.json"))
    if local_env is None:
        monkeypatch.delenv(network_policy.ENV_NAME, raising=False)
    else:
        monkeypatch.setenv(network_policy.ENV_NAME, local_env)
    store, _screen = seeded
    if back_value is not None:
        store.set_meta(front_agent.BACK_LOCAL_ONLY_KEY, back_value)
    app = build(root, store)
    app.open_module("news")
    root.update()
    assert bool(_qr_labels(root)) is qr_shown


def test_the_qr_codes_appear_and_disappear_as_the_synced_switch_changes(root, seeded, tmp_path, monkeypatch):
    from access import front_agent
    from shared import network_policy, paths

    monkeypatch.setattr(paths, "NETWORK_POLICY_PATH", str(tmp_path / "policy.json"))
    monkeypatch.delenv(network_policy.ENV_NAME, raising=False)
    store, _screen = seeded
    app = build(root, store)
    app.open_module("news")
    root.update()
    assert _qr_labels(root)
    store.set_meta(front_agent.BACK_LOCAL_ONLY_KEY, True)                       # 後台切到全本地，前台下一輪同步寫入
    app.refresh()
    root.update()
    assert not _qr_labels(root)
    store.set_meta(front_agent.BACK_LOCAL_ONLY_KEY, False)
    app.refresh()
    root.update()
    assert _qr_labels(root)

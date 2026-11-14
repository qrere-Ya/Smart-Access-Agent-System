"""
QR 註冊視窗（真 Tk）：所有網路位址都是註冊通道、預設選最適合手機的、切換通道後網址與 QR 跟著變、
位址集合沒變保留使用者選的、選的位址消失回到預設、沒有位址的提示、背景取 Token 不卡畫面、
離開這一頁（返回主選單）與整批銷毀後不再出現 TclError。QR 註冊現在是主視窗裡的一頁，不另開視窗。
Token 與位址偵測用假的函式（不需要真的註冊伺服器與網路）。
"""
import gc
import time

import pytest

pytestmark = pytest.mark.integration
pytest.importorskip("customtkinter")

WIFI = {"ip": "172.20.10.7", "adapter": "Wi-Fi", "kind": "lan", "label": "Wi-Fi 172.20.10.7（區網）"}
RADMIN = {"ip": "26.244.154.160", "adapter": "Radmin VPN", "kind": "vpn",
          "label": "Radmin VPN 26.244.154.160（VPN／其他，手機通常連不到）"}
TAILSCALE = {"ip": "100.96.212.34", "adapter": "Tailscale", "kind": "vpn",
             "label": "Tailscale 100.96.212.34（VPN／其他，手機通常連不到）"}


def pump(root, seconds=3.0, until=None):
    """跑畫面事件迴圈，直到條件成立或逾時；回傳條件是否成立。"""
    end = time.time() + seconds
    while time.time() < end:
        root.update()
        if until is not None and until():
            return True
        time.sleep(0.02)
    return until() if until is not None else True


@pytest.fixture
def qr(root, db, monkeypatch):
    """建好後台並提供 open() 開註冊視窗；state 可隨時改假的通道清單與 Token 取得方式；errors 收集 Tk 回呼的例外。"""
    from admin.backend_main import BackendManagerApp

    state = {"channels": [WIFI, RADMIN, TAILSCALE], "token": "tok12345", "fetch": None, "detect_calls": 0}

    def fake_fetch():
        if state["fetch"] is not None:
            return state["fetch"]()
        return state["token"]

    def fake_detect():
        state["detect_calls"] += 1
        return list(state["channels"])

    monkeypatch.setattr(BackendManagerApp, "_fetch_qr_token", staticmethod(fake_fetch))
    monkeypatch.setattr(BackendManagerApp, "_detect_qr_channels", staticmethod(fake_detect))
    errors = []
    previous = root.report_callback_exception
    root.report_callback_exception = lambda *exc: errors.append(exc[1])     # Tk 回呼的例外預設只印出、不會讓測試失敗
    root.deiconify()
    app = BackendManagerApp(root)
    root.withdraw()
    app.state, app.errors = state, errors
    yield app
    root.report_callback_exception = previous
    try:
        app.teardown()
    except Exception:  # noqa: BLE001  視窗可能已被測試自己銷毀
        pass


def open_and_wait(root, app, until=None):
    app.open_qr_registration()
    root.geometry("1280x820+20+10")
    assert pump(root, 5.0, until or (lambda: app._qr_image is not None))


def test_every_address_is_a_channel_and_the_best_for_phones_is_selected_by_default(root, qr, screenshot):
    open_and_wait(root, qr)
    labels = list(qr.channel_combo.cget("values"))
    assert labels == [WIFI["label"], RADMIN["label"], TAILSCALE["label"]]   # 全部列出，VPN 排最後
    assert qr.channel_combo.get() == WIFI["label"]
    assert qr.url_label.cget("text") == "目前網址: http://172.20.10.7:5000?t=tok12345"
    root.attributes("-topmost", True)                        # 截圖前放到最上層，別的視窗才不會蓋住
    assert screenshot(root, "後台_QR註冊_通道")
    root.attributes("-topmost", False)
    assert qr.errors == []


def test_switching_the_channel_redraws_the_qr_and_the_address_at_once(root, qr):
    open_and_wait(root, qr)
    before = qr._qr_image
    qr.channel_combo._dropdown_callback(RADMIN["label"])                            # 使用者在選單選了另一個通道
    root.update()
    assert qr.url_label.cget("text") == "目前網址: http://26.244.154.160:5000?t=tok12345"
    assert qr._qr_image is not None and qr._qr_image is not before
    assert qr.errors == []


def test_the_users_choice_survives_refreshes_until_that_address_disappears(root, qr):
    open_and_wait(root, qr)
    qr.channel_combo._dropdown_callback(RADMIN["label"])
    calls = qr.state["detect_calls"]
    qr._qr_next_fetch = 0.0                                                         # 立刻再跑一輪（位址集合沒變）
    assert pump(root, 5.0, lambda: qr.state["detect_calls"] > calls and not qr._qr_fetching)
    assert qr._qr_selected_ip == RADMIN["ip"] and qr.channel_combo.get() == RADMIN["label"]
    qr.state["channels"] = [WIFI, TAILSCALE]                                        # 選的 Radmin 消失
    qr._qr_next_fetch = 0.0
    assert pump(root, 5.0, lambda: qr._qr_selected_ip == WIFI["ip"])                # 回到預設（最適合手機的）
    assert list(qr.channel_combo.cget("values")) == [WIFI["label"], TAILSCALE["label"]]
    assert qr.channel_combo.get() == WIFI["label"]
    assert qr.errors == []


def test_a_new_address_appearing_keeps_the_selection_and_extends_the_menu(root, qr):
    qr.state["channels"] = [WIFI]
    open_and_wait(root, qr)
    assert list(qr.channel_combo.cget("values")) == [WIFI["label"]]
    qr.state["channels"] = [WIFI, RADMIN]                                           # 熱點開起來、多了一個位址
    qr._qr_next_fetch = 0.0
    assert pump(root, 5.0, lambda: len(qr.channel_combo.cget("values")) == 2)
    assert qr._qr_selected_ip == WIFI["ip"]


def test_without_any_address_it_says_so_instead_of_showing_none(root, qr):
    qr.state["channels"] = []
    open_and_wait(root, qr, until=lambda: "找不到區網位址" in qr.url_label.cget("text"))
    text = qr.url_label.cget("text")
    assert "Wi-Fi" in text and "行動熱點" in text and "None" not in text
    assert qr._qr_image is None
    assert list(qr.channel_combo.cget("values")) == ["（沒有可用的網路位址）"]
    assert qr.errors == []


def test_a_slow_token_request_never_blocks_the_window(root, qr):
    def slow():
        time.sleep(1.2)                                      # 像連不到本機埠時 Windows 卡住的那 2 秒
        raise OSError("timed out")

    qr.state["fetch"] = slow
    started = time.perf_counter()
    qr.open_qr_registration()
    assert time.perf_counter() - started < 0.5                                      # 開窗不等網路
    gaps, last = [], time.perf_counter()
    while time.perf_counter() - started < 1.0:                                      # 背景還在等時，畫面照常跑
        root.update()
        now = time.perf_counter()
        gaps.append(now - last)
        last = now
        time.sleep(0.02)
    assert max(gaps) < 0.3
    assert "準備中" in qr.url_label.cget("text")
    assert pump(root, 5.0, lambda: qr._qr_fail_count >= 1)   # 失敗後仍顯示「準備中」（30 秒寬限期內）
    assert "準備中" in qr.url_label.cget("text") and qr._qr_channels
    assert qr.errors == []


def test_leaving_the_page_leaves_no_tk_errors_even_with_a_request_in_flight(root, qr):
    def slow():
        time.sleep(0.4)
        return "tok-late"

    qr.state["fetch"] = slow
    qr.open_qr_registration()
    pump(root, 0.15)
    qr.show_home()                                                                  # 等於按下「← 返回主選單」
    assert qr._reg_closed is True
    for _ in range(40):                                                             # 背景結果在離開頁面後才回來
        root.update()
        gc.collect()
        time.sleep(0.03)
    assert qr.errors == []
    assert not qr.reg_page.winfo_exists() and qr.page_key == "home"


def test_tearing_down_the_whole_backend_stops_the_timers_cleanly(root, qr):
    """登出或閒置逾時會整批銷毀畫面（不經過返回按鈕）；計時器與背景結果不能再碰已不存在的元件。"""
    def slow():
        time.sleep(0.3)
        return "tok-late"

    qr.state["fetch"] = slow
    qr.open_qr_registration()
    pump(root, 0.1)
    qr.teardown()
    assert qr._reg_closed is True
    for _ in range(40):
        root.update()
        gc.collect()
        time.sleep(0.03)
    assert qr.errors == []


def test_reopening_the_page_starts_fresh_state(root, qr):
    open_and_wait(root, qr)
    qr.channel_combo._dropdown_callback(RADMIN["label"])
    qr.show_home()
    pump(root, 0.2)
    open_and_wait(root, qr)
    assert qr._qr_selected_ip == WIFI["ip"] and qr._reg_closed is False             # 重新進入這一頁回到預設通道
    assert qr.errors == []


def test_a_refresh_that_runs_after_the_page_is_gone_does_nothing(root, qr):
    """回歸：離開頁面時剛好輪到的刷新（或背景結果）原本會對已銷毀的元件 configure，噴 invalid command name。"""
    open_and_wait(root, qr)
    widgets = (qr.url_label, qr.qr_label, qr.channel_combo)
    qr.reg_page.destroy()                                                           # 整批銷毀，沒有取消計時器
    qr._qr_results.put(("ok", "tok-after-close", [WIFI, RADMIN]))                   # 背景結果在銷毀之後才到
    qr._qr_results.put(("err", OSError("timed out"), None))
    qr._qr_tick()                                                                   # 剛好輪到的那一次刷新
    assert qr.errors == [] and not any(w.winfo_exists() for w in widgets)
    qr._reg_closed = False                                  # 即使旗標沒設，也要靠 winfo_exists 擋住
    qr._qr_tick()
    assert qr.errors == []

"""
後台主視窗的頁面切換（真 Tk）：功能不再另開彈出視窗，按卡片直接把內容區換成該功能頁；
所有頁面共用同一個固定大小的主視窗；標題列有淺色／深色／跟隨系統按鈕，立刻換色、記住選擇、換色時停在原本那一頁。
"""
import time

import pytest

pytestmark = pytest.mark.integration
pytest.importorskip("customtkinter")


@pytest.fixture
def app(root, db, monkeypatch, tmp_path):
    from admin.backend_main import BackendManagerApp
    from shared import paths

    monkeypatch.setattr(paths, "RUNTIME_DIR", str(tmp_path))                         # 外觀選擇寫到臨時資料夾
    monkeypatch.setattr(BackendManagerApp, "_fetch_qr_token", staticmethod(lambda: "tok12345"))
    monkeypatch.setattr(BackendManagerApp, "_detect_qr_channels", staticmethod(lambda: [
        {"ip": "172.20.10.7", "adapter": "Wi-Fi", "kind": "lan", "label": "Wi-Fi 172.20.10.7（區網）"}]))
    errors = []
    previous = root.report_callback_exception
    root.report_callback_exception = lambda *exc: errors.append(exc[1])
    root.deiconify()
    backend = BackendManagerApp(root)
    root.update()
    backend.errors = errors
    yield backend
    root.report_callback_exception = previous
    try:
        backend.teardown()
    except Exception:  # noqa: BLE001
        pass


def _toplevels(root):
    return [w for w in root.winfo_children() if w.winfo_class() == "Toplevel"]


def _pump(root, seconds=0.3):
    end = time.time() + seconds
    while time.time() < end:
        root.update()
        time.sleep(0.02)


def test_every_module_switches_the_page_in_the_same_window_without_popups(root, app):
    size = (root.winfo_width(), root.winfo_height())
    for opener, key in (("open_dashboard", "dashboard"), ("open_account_admin", "accounts"),
                        ("open_qr_registration", "qr"), ("open_calendar", "calendar"),
                        ("open_schedule", "schedule"), ("open_report", "report"), ("open_anomalies", "anomaly")):
        getattr(app, opener)()
        _pump(root, 0.2)
        assert app.page_key == key, opener
        assert not _toplevels(root), f"{opener} 又開了彈出視窗"
        assert (root.winfo_width(), root.winfo_height()) == size, f"{opener} 改變了主視窗大小"
        app.show_home()
        assert app.page_key == "home" and app.page is None
    assert app.errors == []


def test_the_main_window_has_one_fixed_size_that_fits_the_screen(root, app):
    from admin import backend_main

    root.update()
    assert root.winfo_width() <= backend_main.WINDOW_MAX[0] and root.winfo_height() <= backend_main.WINDOW_MAX[1]
    assert root.winfo_width() <= root.winfo_screenwidth() and root.winfo_height() <= root.winfo_screenheight()


def test_every_function_page_has_a_back_button_to_the_main_menu(root, app):
    def texts(widget):
        found = []
        for child in widget.winfo_children():
            try:
                found.append(child.cget("text"))
            except Exception:  # noqa: BLE001, S110  不是每種元件都有 text
                pass
            found += texts(child)
        return found

    app.open_account_admin()
    _pump(root, 0.1)
    assert "← 返回主選單" in texts(app.content) and "👤 員工帳號管理" in texts(app.content)
    app.show_home()
    assert "← 返回主選單" not in texts(app.content)


def test_the_header_has_light_dark_and_system_buttons_that_switch_at_once_and_are_remembered(root, app):
    import customtkinter as ctk

    from shared import ui_theme

    labels = [label for _mode, label in ui_theme.MODE_CHOICES]
    assert labels == ["☀ 淺色", "🌙 深色", "🖥 系統"]
    assert list(app.mode_buttons.cget("values")) == labels
    for label, mode in (("🌙 深色", "dark"), ("☀ 淺色", "light"), ("🖥 系統", "system")):
        app._on_mode_choice(label)
        root.update()
        assert ui_theme.load_mode() == mode                                          # 記在設定檔，下次開啟沿用
        if mode != "system":
            assert ctk.get_appearance_mode().lower() == mode
    ctk.set_appearance_mode("dark")                                                  # 還原成整合測試的預設
    assert app.errors == []


def test_switching_the_look_keeps_the_current_page_and_recolors_its_table(root, app, add_user):
    from tkinter import ttk

    from shared import ui_theme

    add_user("王小明", "E001")
    app.open_account_admin()
    app._on_mode_choice("☀ 淺色")
    _pump(root, 0.2)
    assert app.page_key == "accounts" and app.account_view.winfo_exists()            # 停在原本那一頁
    assert ttk.Style(root).lookup("Smart.Treeview", "background") == ui_theme.COLORS["light"]["table_bg"]
    app._on_mode_choice("🌙 深色")
    _pump(root, 0.2)
    assert ttk.Style(root).lookup("Smart.Treeview", "background") == ui_theme.COLORS["dark"]["table_bg"]
    assert app.errors == []


def test_a_notice_replaces_the_info_popup_and_clears_itself(root, app):
    app.notice("已停止人臉辨識終端機。", seconds=0.2)
    assert app.notice_var.get() == "已停止人臉辨識終端機。" and not _toplevels(root)
    _pump(root, 0.5)
    assert app.notice_var.get() == ""


def test_a_digest_finishing_after_the_screen_is_torn_down_does_nothing(root, app):
    """回歸：登出後背景才做完的日報，原本會對已銷毀的內容區換頁，噴 bad window path name。"""
    app.teardown()
    app._digest_ready({"date": "2026-11-13"})
    _pump(root, 0.1)
    assert app.errors == []

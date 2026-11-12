"""前台雙螢幕：哪個螢幕放什麼、視窗位置、全螢幕按鍵、全螢幕記憶、取不到螢幕時的退路。"""
import json

from access import front_screens as fs

LAPTOP = fs.Monitor(0, 0, 1920, 1080, 0, 0, 1920, 1040, True)
EXTERNAL = fs.Monitor(1920, 0, 1920, 1080, 1920, 0, 1920, 1080, False)


def test_dashboard_goes_to_the_second_screen_and_recognition_to_the_primary():
    monitors = [LAPTOP, EXTERNAL]
    assert fs.pick_monitor(monitors, "dashboard") is EXTERNAL
    assert fs.pick_monitor(monitors, "recognition") is LAPTOP
    assert fs.pick_monitor(list(reversed(monitors)), "dashboard") is EXTERNAL          # 清單順序不影響


def test_with_one_screen_both_use_it_and_with_none_there_is_no_answer():
    assert fs.pick_monitor([LAPTOP], "dashboard") is LAPTOP and fs.pick_monitor([LAPTOP], "recognition") is LAPTOP
    assert fs.pick_monitor([], "dashboard") is None


def test_windowed_box_stays_inside_the_work_area_and_leaves_room_for_the_title_bar():
    x, y, w, h = fs.windowed_box(LAPTOP)
    assert (x, y, w) == (0, 0, 1920) and h == 1040 - fs.TITLEBAR_ALLOWANCE
    assert fs.windowed_box(EXTERNAL)[0] == 1920                                          # 在第二個螢幕的座標上


def test_fullscreen_keys():
    assert fs.fullscreen_key(fs.KEY_F11) == "toggle" and fs.fullscreen_key(ord("f")) == "toggle"
    assert fs.fullscreen_key(27) == "leave"
    assert fs.fullscreen_key(-1) is None and fs.fullscreen_key(ord("q")) is None and fs.fullscreen_key(0x7A0066) is None


def test_fullscreen_choice_is_remembered_and_bad_files_are_ignored(tmp_path, monkeypatch):
    monkeypatch.setattr(fs.paths, "RUNTIME_DIR", str(tmp_path))
    assert fs.load_pref("dashboard_fullscreen") is False                                 # 沒檔案：預設不全螢幕
    fs.save_pref("dashboard_fullscreen", True)
    fs.save_pref("recognition_fullscreen", False)
    assert fs.load_pref("dashboard_fullscreen") is True and fs.load_pref("recognition_fullscreen") is False
    (tmp_path / "screen_prefs.json").write_text("壞掉", encoding="utf-8")
    assert fs.load_pref("dashboard_fullscreen") is False
    (tmp_path / "screen_prefs.json").write_text(json.dumps({"dashboard_fullscreen": "yes"}), encoding="utf-8")
    assert fs.load_pref("dashboard_fullscreen") is False                                 # 不是布林值：當成沒設定
    fs.save_pref("dashboard_fullscreen", True)                                           # 壞內容會被正常內容覆蓋
    assert fs.load_pref("dashboard_fullscreen") is True


def test_list_monitors_never_raises_and_finds_this_machines_screens():
    monitors = fs.list_monitors()
    assert isinstance(monitors, list)
    if monitors:
        assert monitors[0].primary and all(m.width > 0 and m.height > 0 for m in monitors)

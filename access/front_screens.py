"""
front_screens.py：前台的雙螢幕擺放與全螢幕設定——找出螢幕、決定儀表板與人臉辨識各放哪個、記住是否全螢幕。

主要功能：
  - list_monitors()：用 Windows API（ctypes，不需額外套件）列出所有螢幕的位置、大小、可用範圍（扣掉工作列）
    與是否主螢幕；取不到（非 Windows、出錯）就回傳空清單，呼叫端退回單螢幕做法
  - pick_monitor(monitors, purpose)：dashboard（功能模組儀表板）放第一個非主螢幕（外接顯示器），只有一個螢幕就放那一個；
    recognition（人臉辨識畫面）放主螢幕（筆電本身）
  - windowed_box(monitor)：一般視窗模式的位置與大小（可用範圍內，預留標題列）
  - fullscreen_key(key)：把 OpenCV 的按鍵碼轉成 toggle（F11、f）／leave（Esc）／None
  - load_pref／save_pref：記住各視窗上次是否全螢幕（runtime/screen_prefs.json），下次開啟沿用；壞檔一律當成沒設定
  - enable_dpi_awareness()：讓程式看到的座標是真正的像素（OpenCV 視窗要先呼叫，座標才對得上螢幕）

串接模組：front_dashboard（儀表板位置、F11）、terminal_app（前台角色時辨識視窗的位置與 F11）
"""
import ctypes
import json
import os
from typing import NamedTuple

from shared import paths

TITLEBAR_ALLOWANCE = 40            # 一般視窗模式預留給標題列與邊框的高度
KEY_F11 = 0x7A0000                 # cv2.waitKeyEx 在 Windows 上 F11 的回傳值（虛擬鍵碼 0x7A 左移 16 位）
KEY_ESC = 27


class Monitor(NamedTuple):
    x: int
    y: int
    width: int
    height: int
    work_x: int
    work_y: int
    work_width: int
    work_height: int
    primary: bool


def list_monitors():
    """所有螢幕（主螢幕排第一個）；非 Windows 或取不到時回傳空清單。"""
    if os.name != "nt":
        return []
    try:
        from ctypes import wintypes

        class _MonitorInfo(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                        ("dwFlags", wintypes.DWORD)]

        found = []
        callback_type = ctypes.WINFUNCTYPE(ctypes.c_int, wintypes.HMONITOR, wintypes.HDC,
                                           ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)

        def on_monitor(handle, _dc, _rect, _data):
            info = _MonitorInfo()
            info.cbSize = ctypes.sizeof(_MonitorInfo)
            if ctypes.windll.user32.GetMonitorInfoW(handle, ctypes.byref(info)):
                m, w = info.rcMonitor, info.rcWork
                found.append(Monitor(m.left, m.top, m.right - m.left, m.bottom - m.top, w.left, w.top,
                                     w.right - w.left, w.bottom - w.top, bool(info.dwFlags & 1)))
            return 1

        callback = callback_type(on_monitor)
        ctypes.windll.user32.EnumDisplayMonitors(None, None, callback, 0)
        return sorted(found, key=lambda mon: (not mon.primary, mon.x, mon.y))
    except Exception:  # noqa: BLE001  取不到螢幕資訊就退回單螢幕做法
        return []


def pick_monitor(monitors, purpose):
    """purpose="dashboard"：第一個非主螢幕（沒有就用主螢幕）；"recognition"：主螢幕。沒有螢幕資料回傳 None。"""
    if not monitors:
        return None
    primary = next((m for m in monitors if m.primary), monitors[0])
    if purpose == "dashboard":
        return next((m for m in monitors if m is not primary), primary)
    return primary


def windowed_box(monitor):
    """一般視窗模式：(x, y, 寬, 高)，貼滿該螢幕的可用範圍（不蓋到工作列），上方留出標題列。"""
    return (monitor.work_x, monitor.work_y, monitor.work_width,
            max(200, monitor.work_height - TITLEBAR_ALLOWANCE))


def fullscreen_key(key):
    """OpenCV 按鍵碼 → "toggle"（F11 或 f）、"leave"（Esc）、None。"""
    if key in (KEY_F11, ord("f")):
        return "toggle"
    if key == KEY_ESC:
        return "leave"
    return None


def _pref_path():
    return os.path.join(paths.RUNTIME_DIR, "screen_prefs.json")


def load_pref(name, default=False):
    """讀某個視窗上次是否全螢幕；沒有檔案或壞檔回傳預設值。"""
    try:
        with open(_pref_path(), encoding="utf-8") as f:
            data = json.load(f)
        value = data.get(name) if isinstance(data, dict) else None
        return value if isinstance(value, bool) else default
    except (OSError, ValueError):
        return default


def save_pref(name, value):
    """記住某個視窗是否全螢幕；寫不進去也不影響使用。"""
    try:
        try:
            with open(_pref_path(), encoding="utf-8") as f:
                data = json.load(f)
            data = data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            data = {}
        data[name] = bool(value)
        os.makedirs(paths.RUNTIME_DIR, exist_ok=True)
        with open(_pref_path(), "w", encoding="utf-8") as f:
            json.dump(data, f)
    except OSError:
        pass


def enable_dpi_awareness():
    """讓這個程式看到真實像素座標（多螢幕、不同縮放比例時位置才對）；已設定過或不支援就略過。"""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:  # noqa: BLE001, S110  # nosec B110
        pass

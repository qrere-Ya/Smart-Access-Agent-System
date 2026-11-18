"""
app.py：前台儀表板（PySide6 + QML 版）的啟動——建立視窗、放到第二個螢幕、F11 全螢幕、每秒更新資料。

主要功能：
  - create：建立 QGuiApplication、QML 引擎與資料模型，載入 qml/Main.qml；測試也用這個函式（可傳假的資料來源）
  - 雙螢幕：視窗放第一個非主螢幕（外接顯示器），只有一個螢幕就放那一個；F11 或右上角按鈕切全螢幕、Esc 離開，
    下次沿用上次的選擇（front_screens 記在 runtime/screen_prefs.json）
  - 有人操作（滑鼠、鍵盤）就通知模型重算閒置時間；每秒呼叫模型 refresh，任何一次出錯都不會讓畫面停止
  - main：連線代理跟著儀表板在背景開，人臉辨識由右上角按鈕啟動；關閉視窗時一併收掉辨識與代理
  - 前台不提供任何設定或管理員功能，也不連網路（只讀本機資料）

串接模組：
  - access.front_qt.model：資料與動作；qml/：畫面；access.front_launcher：背景行程；access.front_screens：偏好
  - 啟動：python -m access.front_qt.app（啟動前台.bat）
"""
import os
import sys

from PySide6.QtCore import QEvent, QObject, QTimer, QUrl
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuickControls2 import QQuickStyle

from access import front_launcher, front_screens
from access.front_qt.model import DashboardModel
from access.front_store import FrontStore
from shared import paths

QML_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "qml")
REFRESH_MS = 1000
ACTIVITY_EVENTS = (QEvent.Type.MouseButtonPress, QEvent.Type.KeyPress, QEvent.Type.MouseMove,
                   QEvent.Type.Wheel, QEvent.Type.TouchBegin)


class ActivityFilter(QObject):
    """任何滑鼠、鍵盤操作都算有人在用，通知模型重算閒置時間。"""

    def __init__(self, model):
        super().__init__(model)
        self._model = model

    def eventFilter(self, _watched, event):  # noqa: N802  Qt 的方法名
        if event.type() in ACTIVITY_EVENTS:
            self._model.touch()
        return False


def pick_screen(app):
    """儀表板放的螢幕：第一個非主螢幕（外接顯示器），沒有就用主螢幕。"""
    primary = app.primaryScreen()
    others = [s for s in app.screens() if s != primary]
    return others[0] if others else primary


def place_window(window, screen):
    """放到指定螢幕的可用範圍內（預留標題列）。"""
    area = screen.availableGeometry()
    window.setScreen(screen)
    window.setGeometry(area.x() + 20, area.y() + 40, max(800, area.width() - 40), max(450, area.height() - 80))


class Dashboard:
    """啟動好的儀表板：app、引擎、模型、視窗；測試與 main 共用。"""

    def __init__(self, app, engine, model, window, timer):
        self.app, self.engine, self.model, self.window, self.timer = app, engine, model, window, timer
        self.warnings = []

    def close(self):
        self.timer.stop()
        self.window.close()


def create(store, qml_dir=QML_DIR, app=None, **model_args):
    """建立儀表板；載入失敗（QML 有語法錯誤）丟 RuntimeError，訊息包含 QML 的錯誤文字。"""
    QQuickStyle.setStyle("Basic")                       # 完全自訂外觀，不用平台樣式
    app = app or QGuiApplication.instance() or QGuiApplication(sys.argv)
    model = DashboardModel(store, **model_args)
    engine = QQmlApplicationEngine()
    warnings = []
    engine.warnings.connect(lambda items: warnings.extend(w.toString() for w in items))
    engine.rootContext().setContextProperty("backend", model)
    engine.load(QUrl.fromLocalFile(os.path.join(qml_dir, "Main.qml")))
    roots = engine.rootObjects()
    if not roots:
        raise RuntimeError("QML 載入失敗：" + "；".join(warnings[:5]))
    window = roots[0]
    place_window(window, pick_screen(app))
    model.fullscreenRequested.connect(lambda on: window.showFullScreen() if on else window.showNormal())
    app.installEventFilter(ActivityFilter(model))
    timer = QTimer(model)
    timer.setInterval(REFRESH_MS)

    def tick():
        try:
            model.refresh_async()
        except Exception as e:  # noqa: BLE001  任何一次更新出錯都不能讓畫面停止
            print(f"[front_qt][WARN] 更新失敗，下一輪再試：{type(e).__name__}")

    timer.timeout.connect(tick)
    timer.start()
    dashboard = Dashboard(app, engine, model, window, timer)
    dashboard.warnings = warnings
    if front_screens.load_pref("dashboard_fullscreen"):          # 上次是全螢幕，這次沿用
        QTimer.singleShot(300, lambda: model.setFullscreen(True, remember=False))
    return dashboard


def main():
    front_launcher.redirect_output_if_windowless("front_qt")      # pythonw 沒有黑色視窗，print 改寫記錄檔
    store = FrontStore(os.path.join(paths.RUNTIME_DIR, "front_agent.db"))
    agent = front_launcher.ManagedProcess("access.front_agent", "front_agent")
    recognition = front_launcher.ManagedProcess("access.terminal_app", "terminal_app")
    ok, message = agent.start()                                    # 連線代理跟著儀表板一起開，人臉辨識由按鈕啟動
    if not ok:
        print(f"[front_qt][WARN] 連線代理{message}")
    try:
        dashboard = create(store, recognition=recognition)
        code = dashboard.app.exec()
    finally:
        recognition.stop()                                         # 關閉儀表板就一起收掉辨識與代理
        agent.stop()
    return code


if __name__ == "__main__":
    raise SystemExit(main())

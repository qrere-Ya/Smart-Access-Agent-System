"""
preview.py：前台儀表板（PySide6 + QML 版）的畫面預覽——用假資料開儀表板，給改畫面的人看（不需要攝影機、模型、後台）。

主要功能：
  - 假資料來自 access.front_preview（科技早報、打卡、後台連線、假的問答與辨識程式），不碰 runtime/ 的真實資料
  - --alert 顯示警示橫幅、--offline 模擬後台沒連上、--no-news 沒有早報、--theme 指定外觀、--chat／--talk 指定問答情境
    （open 可以問並回假回答、busy、error、offline、closed）、--seconds 幾秒後自動關閉（截圖與自動檢查用）
  - 結束後臨時資料夾自動刪除

用法：python -m access.front_qt.preview [--alert tailgate] [--offline] [--theme frosted] [--chat open] [--talk closed]

串接模組：access.front_qt.app（建立畫面）、access.front_preview（假資料與假控制器）
"""
import argparse
import os
import tempfile

from PySide6.QtCore import QTimer
from PySide6.QtGui import QGuiApplication

from access import front_agent, front_preview
from access.front_qt import app as qapp
from access.front_store import FrontStore


def main(argv=None):
    parser = argparse.ArgumentParser(description="前台儀表板（QML 版）畫面預覽（假資料）")
    parser.add_argument("--alert", choices=front_preview.ALERT_KINDS, help="顯示警示橫幅")
    parser.add_argument("--offline", action="store_true", help="模擬後台沒連上")
    parser.add_argument("--no-news", action="store_true", help="模擬沒有科技早報")
    parser.add_argument("--theme", choices=("dark", "frosted"), default="dark")
    parser.add_argument("--recognition-off", action="store_true", help="模擬後台已停止人臉辨識")
    parser.add_argument("--chat", choices=("open", "busy", "error", "offline", "closed"), default="open",
                        help="Smart_RAG 知識庫問答的情境")
    parser.add_argument("--talk", choices=("open", "busy", "error", "offline", "closed"), default="open",
                        help="Smart_Chat 一般對話的情境")
    parser.add_argument("--seconds", type=float, default=0.0, help="幾秒後自動關閉（0＝不自動關）")
    args = parser.parse_args(argv)

    app = QGuiApplication.instance() or QGuiApplication([])
    with tempfile.TemporaryDirectory() as folder:
        store = FrontStore(os.path.join(folder, "preview.db"))
        screen = front_preview.seed(store, offline=args.offline, news=not args.no_news, theme=args.theme)
        if args.recognition_off:
            store.set_meta(front_agent.BACK_RECOGNITION_KEY, False)
        recognition = front_preview.FakeRecognition()
        if args.alert:
            screen.set_alert(args.alert, "（預覽）警示橫幅")
        dashboard = qapp.create(
            store, app=app, read_heartbeat=lambda: {"camera_ok": True, "recognition_running": recognition.running()},
            read_resources=lambda: front_preview.FAKE_RESOURCES, recognition=recognition,
            chat=front_preview.PreviewChat(args.chat), talk=front_preview.PreviewChat(args.talk))
        if args.seconds > 0:
            QTimer.singleShot(int(args.seconds * 1000), app.quit)
        code = app.exec()
        dashboard.close()
    return code


if __name__ == "__main__":
    raise SystemExit(main())

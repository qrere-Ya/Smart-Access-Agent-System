"""
front_preview.py：前台儀表板的畫面預覽（給改畫面的人用）——用假資料開出完整的儀表板，不需要攝影機、模型、後台、網路。

主要功能：
  - 用臨時資料夾裡的假資料庫開 FrontDashboard：科技早報新聞、剛剛打卡、狀態、資源使用量都是假的
  - --alert 讓頂端出現警示橫幅（tailgate 尾隨、no_scan_walkby 經過未打卡、spoof 非活體），預覽期間會一直維持
  - --offline 模擬後台沒連上；--no-news 模擬沒有科技早報；--theme 指定主題（dark 或 frosted）
  - 人臉辨識按鈕是假的（按了只會改變按鈕狀態，不會開任何程式），用來看「啟動／執行中」的樣子；
    --recognition-off 模擬後台停止辨識
  結束視窗後臨時資料夾自動刪除，不會留下或改到任何真實資料（不碰 runtime/）。

用法：python -m access.front_preview [--alert tailgate] [--offline] [--no-news] [--theme frosted] [--recognition-off]
  只需要 customtkinter、Pillow、numpy、pyqrcode、requests、psutil（見 前台開發指南.md），不用裝任何模型。

串接模組：
  - front_dashboard：畫面；front_store：資料庫（ScreenState 存警示、打卡、主題）；front_view：畫面資料
"""
import argparse
import os
import tempfile
import time

import customtkinter as ctk

from access import front_dashboard
from access.front_store import FrontStore, ScreenState
from shared import ui_theme

ALERT_KINDS = ("tailgate", "no_scan_walkby", "spoof")
ALERT_REFRESH_MS = 20_000          # 警示 60 秒後會自己消失；預覽時每 20 秒重新放一次
FAKE_RESOURCES = {"cpu": 12.0, "ram": 48.0, "gpu": 31.0, "down_kbps": 0.4, "up_kbps": 820.0}

NEWS = [{"category": cat, "title": f"{cat}：第 {i + 1} 則今日科技新聞的標題（預覽用假資料）",
         "what": "這是「發生什麼事」的說明，用來看文字多長時版面怎麼換行。",
         "why": "這是「為什麼重要」的說明。", "how": "這是「可以怎麼用」的說明。",
         "source": "示範來源", "link": f"https://example.com/news/{i + 1}"}
        for i, cat in enumerate(["AI", "資安", "硬體與晶片", "軟體與開發", "AI", "資安", "AI"])]


class FakeRecognition:
    """假的人臉辨識行程：只記狀態，不啟動任何程式（介面與 front_launcher.ManagedProcess 相同）。"""

    def __init__(self):
        self._running = False
        self.last_exit = None

    def running(self):
        return self._running

    def start(self):
        self._running = True
        return True, "（預覽）已啟動"

    def stop(self):
        self._running = False


def seed(store, offline=False, news=True, theme="dark", now=time.time):
    """把假資料寫進資料庫（純資料，不開視窗）：科技早報、兩筆打卡、後台連線狀態、主題。"""
    screen = ScreenState(store)
    current = now()
    if news:
        store.set_meta("morning", {"date": time.strftime("%Y-%m-%d"), "generated_at": "08:30", "news": NEWS,
                                   "news_problem": ""})
    screen.note_punch("測試甲", "早安", now=lambda: current - 40, clock_text="08:01")
    screen.note_punch("測試乙", "早安", now=lambda: current - 12, clock_text="08:03")
    store.set_link_state(not offline, now=now)
    screen.set_theme(theme)
    return screen


def main(argv=None):
    parser = argparse.ArgumentParser(description="前台儀表板畫面預覽（假資料）")
    parser.add_argument("--alert", choices=ALERT_KINDS, help="顯示警示橫幅")
    parser.add_argument("--offline", action="store_true", help="模擬後台沒連上")
    parser.add_argument("--no-news", action="store_true", help="模擬沒有科技早報")
    parser.add_argument("--theme", choices=tuple(front_dashboard.THEME_LABELS), default="dark")
    parser.add_argument("--recognition-off", action="store_true", help="模擬後台已停止人臉辨識")
    args = parser.parse_args(argv)

    with tempfile.TemporaryDirectory() as folder:
        store = FrontStore(os.path.join(folder, "preview.db"))
        screen = seed(store, offline=args.offline, news=not args.no_news, theme=args.theme)
        if args.recognition_off:
            store.set_meta(front_dashboard.front_agent.BACK_RECOGNITION_KEY, False)
        recognition = FakeRecognition()
        ui_theme.apply_theme("dark")
        root = ctk.CTk()
        app = front_dashboard.FrontDashboard(
            root, store, read_heartbeat=lambda: {"camera_ok": True, "recognition_running": recognition.running()},
            read_resources=lambda: FAKE_RESOURCES, recognition=recognition)

        def keep_alert():
            screen.set_alert(args.alert, "畫面有 2 人，只辨識到 1 張臉（預覽用假警示）")
            root.after(ALERT_REFRESH_MS, keep_alert)

        if args.alert:
            keep_alert()
        root.after(front_dashboard.REFRESH_MS, app.tick)
        root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

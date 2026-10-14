"""
dashboard_stats.py：戰情儀表板的統計查詢。

主要功能：
  - 今日到職人數與最近打卡紀錄
  - 異常（尾隨證據）照片件數
  - 系統 CPU / RAM 使用量
  只查詢並回傳資料，不碰任何畫面元件。

串接模組：
  - database_mgr：取得資料庫路徑
  - backend_main：取得資料後顯示在儀表板與底部狀態列
"""
import os
import sqlite3
import datetime

from shared import database_mgr
from shared import paths

ANOMALY_DIR = paths.ANOMALY_DIR


def get_today_attendance_summary(limit=20):
    """回傳 (今日不重複到職人數, 最近 limit 筆打卡紀錄)；查詢失敗回傳 None，讓畫面維持原本數字。"""
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    try:
        conn = sqlite3.connect(database_mgr.DB_PATH)
        c = conn.cursor()
        c.execute("SELECT COUNT(DISTINCT name) FROM Attendance WHERE timestamp LIKE ?", (f"{today}%",))
        count = c.fetchone()[0]
        c.execute(
            "SELECT timestamp, name, status FROM Attendance WHERE timestamp LIKE ? ORDER BY timestamp DESC LIMIT ?",
            (f"{today}%", limit),
        )
        logs = c.fetchall()
        conn.close()
        return count, logs
    except Exception as e:
        print(f"[Dashboard Stats][WARN] 今日考勤資料查詢失敗（畫面維持原本數字）: {e}")
        return None


def get_anomaly_count():
    """回傳 anomaly_logs 資料夾內的證據照片數量，資料夾不存在回傳 0。"""
    if not os.path.exists(ANOMALY_DIR):
        return 0
    return len([f for f in os.listdir(ANOMALY_DIR) if f.endswith(".jpg")])


def get_system_resources():
    """回傳 {"cpu_percent", "ram_used_gb", "ram_total_gb"}；讀取失敗（例如沒裝 psutil）回傳 None。"""
    try:
        import psutil
        return {
            "cpu_percent": psutil.cpu_percent(interval=0.1),
            "ram_used_gb": round(psutil.virtual_memory().used / (1024 ** 3), 1),
            "ram_total_gb": round(psutil.virtual_memory().total / (1024 ** 3), 1),
        }
    except Exception as e:
        print(f"[Dashboard Stats][WARN] 系統資源讀取失敗: {e}")
        return None

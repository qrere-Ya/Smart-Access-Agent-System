"""
dashboard_stats.py：戰情儀表板的統計查詢。

主要功能：
  - 今日到職人數與最近打卡紀錄
  - 異常（尾隨證據）照片件數、前台事件串流（最近的打卡與尾隨、經過未打卡）
  - 系統 CPU / RAM 使用量
  只查詢並回傳資料，不碰任何畫面元件。

串接模組：
  - database_mgr：取得資料庫路徑
  - backend_main：取得資料後顯示在儀表板與底部狀態列
"""
import os
import sqlite3
import datetime

from shared import database_mgr, event_labels
from shared import paths

ANOMALY_DIR = paths.ANOMALY_DIR


def get_today_attendance_summary(limit=20):
    """回傳 (今日不重複到職人數, 最近 limit 筆打卡紀錄)；查詢失敗回傳 None，讓畫面維持原本數字。"""
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    try:
        conn = sqlite3.connect(database_mgr.DB_PATH)
        c = conn.cursor()
        c.execute("SELECT COUNT(DISTINCT COALESCE('u' || user_id, 'n' || name)) FROM Attendance "
                  "WHERE timestamp LIKE ?", (f"{today}%",))   # 以員工識別區分人，舊紀錄才用姓名
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


def get_recent_events(limit=20):
    """前台事件串流：最近的打卡與前台異常（尾隨、經過未打卡），新的在前；回傳 [(時間, 說明)]，查詢失敗回傳 None。"""
    try:
        conn = sqlite3.connect(database_mgr.DB_PATH)
        attendance = conn.execute("SELECT timestamp, name, status FROM Attendance ORDER BY timestamp DESC LIMIT ?",
                                  (limit,)).fetchall()
        anomalies = conn.execute("SELECT timestamp, event_type, detail FROM AnomalyEvents "
                                 "WHERE event_type IN ('tailgate', 'no_scan_walkby') ORDER BY timestamp DESC LIMIT ?",
                                 (limit,)).fetchall()
        conn.close()
    except Exception as e:  # noqa: BLE001
        print(f"[Dashboard Stats][WARN] 事件串流查詢失敗（畫面維持原本內容）: {type(e).__name__}")
        return None
    rows = [(ts, f"打卡　{name}　{status}") for ts, name, status in attendance]
    rows += [(ts, event_labels.describe(kind, detail)) for ts, kind, detail in anomalies]
    rows.sort(key=lambda row: row[0], reverse=True)
    return rows[:limit]


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

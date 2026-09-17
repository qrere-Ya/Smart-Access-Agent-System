"""
戰情儀表板要顯示的統計數字：今日到職人數、最近打卡紀錄、異常件數，以及（尚未接到
畫面上的）系統資源。

【2026-09-12，P1 架構重構抽出】原本這些查詢是寫在 backend_main.py 的
refresh_dashboard_data() 裡，直接混著 Tkinter 的 Treeview 更新一起做。這裡抽出來變成
純粹「查資料、回傳結果」的函式，不碰任何畫面元件，backend_main.py 自己決定拿到資料
後要怎麼顯示。
"""
import os
import sqlite3
import datetime

import database_mgr

_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
ANOMALY_DIR = os.path.join(_PROJECT_ROOT, "anomaly_logs")


def get_today_attendance_summary(limit=20):
    """
    回傳 (今日不重複到職人數, 最近 limit 筆打卡紀錄) 的 tuple；資料庫查詢失敗（例如
    檔案剛好被鎖）時回傳 None，讓呼叫端知道「這次沒查到，維持畫面上原本的數字就好」，
    跟原本 refresh_dashboard_data() 裡 `except: pass`（查詢失敗就整個跳過、不更新
    畫面）的行為一致，不會因為偶爾查詢失敗就讓畫面顯示成「0 人」這種誤導的假數字。
    """
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
    """回傳 anomaly_logs 資料夾裡的證據照片數量。跟原本邏輯一樣：資料夾不存在就直接
    回傳 0，不特別當成錯誤處理。"""
    if not os.path.exists(ANOMALY_DIR):
        return 0
    return len([f for f in os.listdir(ANOMALY_DIR) if f.endswith(".jpg")])


def get_system_resources():
    """
    【P1，2026-09-12 新增，目前尚未接到畫面上】系統資源（CPU/RAM）。對應 Syscom Cubi
    截圖裡「即時系統效能」卡片的資料來源，之前只有業務數字（今日到職/異常）沒有系統
    資源。這裡先把資料準備好，故意不動 backend_main.py 的畫面——按照說好的，P1 只搬
    程式碼、不改畫面，要不要顯示、顯示在哪裡，等你決定的時候再接上。psutil 這個套件
    專案裡已經在用（service_manager._find_pid_on_port 也靠它找 port 對應的 PID），
    不算新增依賴。

    回傳 dict：{"cpu_percent": ..., "ram_used_gb": ..., "ram_total_gb": ...}；
    讀取失敗（例如這台電腦沒裝 psutil）回傳 None。
    """
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

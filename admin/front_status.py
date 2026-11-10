"""
front_status.py：後台顯示用的「前台狀態」——收前台心跳、存最新一份、判斷在線或離線（不碰網路，也不主動連前台）。

主要功能：
  - record：link_server 收到前台心跳（已驗證）時呼叫，把最新一份狀態原子寫進 runtime/front_status.json
    （後台視窗是另一個行程，靠這個檔案讀；只留最新一份，不累積）
  - load：讀回狀態並依「最後收到時間」判斷在線；超過 OFFLINE_AFTER_SECONDS 沒收到就是離線；
    檔案不存在或壞掉回傳「還沒收到過」，絕不丟例外
  - describe：把狀態整理成畫面要顯示的白話（色調、標題、細節列），不含任何 UI 元件，方便測試
  - 前台自己回報的狀態只用來顯示，不是安全或打卡判斷的依據（有通行碼的人可以回報任何內容）
  - 前台當機與網路斷線在這裡看起來一樣（都是沒收到），所以畫面顯示「最後收到時間」，不假裝知道原因

串接模組：
  - admin.link_server：收到心跳就 record；admin.backend_main：營運狀態頁定時 load 並用 describe 顯示
  - shared.link_protocol：心跳欄位驗證；shared.paths：FRONT_STATUS_PATH
"""
import json
import os
import time

from shared import paths

OFFLINE_AFTER_SECONDS = 30


def record(status, remote_addr="", path=None, clock=time.time):
    """寫入最新一份前台狀態（先寫暫存檔再取代，讀的人不會讀到一半）。"""
    target = path or paths.FRONT_STATUS_PATH
    os.makedirs(os.path.dirname(target), exist_ok=True)
    tmp = target + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"received_at": clock(), "remote_addr": str(remote_addr)[:45], "status": status}, f)
    os.replace(tmp, target)


def load(path=None, clock=time.time):
    """回傳 {seen: 是否收到過, online: 是否在線, age: 距離最後收到幾秒, status: 前台回報的狀態或 {}, remote_addr}。"""
    try:
        with open(path or paths.FRONT_STATUS_PATH, encoding="utf-8") as f:
            data = json.load(f)
        received = float(data["received_at"])
        status = data["status"] if isinstance(data["status"], dict) else {}
        remote = str(data.get("remote_addr", ""))
    except (OSError, ValueError, KeyError, TypeError):
        return {"seen": False, "online": False, "age": None, "status": {}, "remote_addr": ""}
    age = max(0.0, clock() - received)
    return {"seen": True, "online": age <= OFFLINE_AFTER_SECONDS, "age": age, "status": status, "remote_addr": remote}


def _ago(seconds):
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds} 秒前"
    if seconds < 3600:
        return f"{seconds // 60} 分鐘前"
    return f"{seconds // 3600} 小時前"


def describe(state):
    """回傳 (色調, 標題, 細節列表)；色調是 success／danger／muted，由畫面決定實際顏色。"""
    if not state["seen"]:
        return "muted", "⚪ 還沒收到前台回報", ["前台尚未連線，或前台還沒啟動連線代理"]
    status = state["status"]
    if not state["online"]:
        return ("danger", f"🔴 前台離線（最後收到 {_ago(state['age'])}）",
                ["可能是前台關機、網路線拔掉或後台連線服務重啟"])
    details = [
        "鏡頭" + ("正常" if status.get("camera_ok") else "異常或未啟動") + "，人臉辨識"
        + ("執行中" if status.get("recognition_running") else "未執行"),
        f"待補送事件 {status.get('pending_events', 0)} 筆，人臉資料 {status.get('faces', 0)} 筆",
    ]
    if status.get("rejected_events") or status.get("dropped_events"):
        details.append(f"被後台拒收 {status.get('rejected_events', 0)} 筆、"
                       f"佇列滿而丟棄 {status.get('dropped_events', 0)} 筆")
    if status.get("clock_warning"):
        details.append(f"⚠ 前台時鐘與後台差 {status.get('clock_offset_seconds', 0)} 秒（已自動校正事件時間）")
    return "success", f"🟢 前台連線中（{_ago(state['age'])} 回報）", details

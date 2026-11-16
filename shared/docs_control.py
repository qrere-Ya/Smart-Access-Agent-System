"""
docs_control.py：後台管理者對知識庫文件的操作——放文件的資料夾說明、通知知識庫服務重新掃描、顯示掃描結果（純邏輯）。

主要功能：
  - request_refresh：後台畫面按「重新掃描」時寫一個請求檔；知識庫服務（admin.chat_service，在連線服務行程裡）
    每秒看一次，有新請求就重新掃描 data/docs（新增、修改、刪除的文件各自處理）。服務還沒載入知識庫時不用掃，
    因為第一次有前台提問時載入就會掃描最新的文件
  - pending：有沒有比 since 更新的請求
  - write_status／read_status：服務把掃描結果（一行說明）寫在檔案裡，後台畫面讀來顯示；先寫暫存檔再取代
  - folder_summary：文件資料夾裡有幾份可收錄的文件（PDF、Word、txt、md）
  後台畫面（營運狀態頁「前台連線」卡片）與連線服務是兩個行程，所以用檔案溝通，方向仍是後台內部，沒有任何網路往來。

串接模組：
  - admin.chat_service：讀請求、寫結果；admin.backend_main：寫請求、讀結果、開啟文件資料夾
  - shared.paths：DOCS_DIR、DOCS_REFRESH_PATH、DOCS_STATUS_PATH
"""
import json
import os
import time

from shared import paths

SUPPORTED = (".pdf", ".docx", ".txt", ".md")


def _write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


def _read(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def request_refresh(now=None):
    """請知識庫服務重新掃描文件；回傳請求時間。"""
    stamp = time.time() if now is None else now
    _write(paths.DOCS_REFRESH_PATH, {"requested_at": stamp})
    return stamp


def pending(since=0.0):
    """有比 since 更新的重新掃描請求就回傳它的時間，否則回傳 None（檔案壞掉也當作沒有）。"""
    data = _read(paths.DOCS_REFRESH_PATH)
    stamp = data.get("requested_at") if data else None
    if isinstance(stamp, (int, float)) and not isinstance(stamp, bool) and stamp > since:
        return float(stamp)
    return None


def write_status(text, now=None):
    """服務寫下最近一次掃描的結果說明。"""
    _write(paths.DOCS_STATUS_PATH, {"time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(
        time.time() if now is None else now)), "text": str(text)[:500]})


def read_status():
    """最近一次掃描的結果，回傳 {"time", "text"}；還沒掃過或檔案壞掉回傳 None。"""
    data = _read(paths.DOCS_STATUS_PATH)
    if data and isinstance(data.get("time"), str) and isinstance(data.get("text"), str):
        return {"time": data["time"][:19], "text": data["text"][:500]}
    return None


def folder_summary(folder=None):
    """文件資料夾的說明：(可收錄的文件數, 資料夾路徑)；資料夾不存在就是 0 份。"""
    folder = folder or paths.DOCS_DIR
    try:
        names = [n for n in os.listdir(folder) if n.lower().endswith(SUPPORTED) and not n.startswith("~$")]
    except OSError:
        names = []
    return len(names), folder

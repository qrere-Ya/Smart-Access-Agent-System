"""
queries.py：給助理用的固定查詢函式（考勤、異常、名單）。

主要功能：
  - 所有查詢都是寫死的參數化 SQL，LLM 不會寫 SQL；資料庫連線是唯讀（mode=ro）
  - 只讀取需要的欄位：員工名單只取姓名、編號、是否在職，人臉特徵欄位永遠不會被選出來
  - 日期參數一律先驗證為 YYYY-MM-DD，區間包含結束當天整天；筆數有上限

串接模組：
  - shared.database_mgr：資料庫路徑與資料表結構（Attendance、Users、AnomalyEvents）
  - router / answer（之後）：由意圖判斷選出要呼叫的函式，把結果交給 LLM 轉成人話
"""
import datetime
import pathlib
import sqlite3
from contextlib import closing
from typing import Dict, List, Optional

from shared import database_mgr

MAX_ROWS = 500
STATUS_LATE = "遲到"
STATUS_EARLY_LEAVE = "早退"
STATUS_OVERTIME = "下班 (加班)"


class QueryError(ValueError):
    """查詢參數不合法（日期格式錯誤、區間顛倒等）。"""


def _connect():
    uri = pathlib.Path(database_mgr.DB_PATH).resolve().as_uri() + "?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=5)


def _day(value: Optional[str], name: str) -> Optional[datetime.date]:
    if value is None:
        return None
    try:
        return datetime.date.fromisoformat(value)
    except (TypeError, ValueError) as e:
        raise QueryError(f"{name}必須是 YYYY-MM-DD 格式：{value!r}") from e


def _range(start: Optional[str], end: Optional[str]):
    """回傳 (起始時間字串, 結束時間字串（不含）)，任一邊可以是 None。"""
    first, last = _day(start, "開始日期"), _day(end, "結束日期")
    if first and last and last < first:
        raise QueryError("結束日期早於開始日期")
    return (first.isoformat() if first else None,
            (last + datetime.timedelta(days=1)).isoformat() if last else None)


def _limit(limit: int) -> int:
    return max(1, min(int(limit), MAX_ROWS))


def roster(include_inactive: bool = True) -> List[Dict]:
    """員工名單（姓名、員工編號、是否在職）；不含任何人臉特徵。"""
    sql = "SELECT name, employee_id, COALESCE(is_active, 1) FROM Users"
    if not include_inactive:
        sql += " WHERE COALESCE(is_active, 1) = 1"
    with closing(_connect()) as conn:
        rows = conn.execute(sql + " ORDER BY name").fetchall()
    return [{"name": r[0], "employee_id": r[1], "active": bool(r[2])} for r in rows]


def attendance_records(name: str, start: Optional[str] = None, end: Optional[str] = None,
                       status: Optional[str] = None, limit: int = 200) -> List[Dict]:
    """某人的打卡紀錄，時間由新到舊；可限制日期區間與狀態（如「遲到」）。"""
    since, until = _range(start, end)
    clauses, params = ["name = ?"], [name]
    if since:
        clauses.append("timestamp >= ?")
        params.append(since)
    if until:
        clauses.append("timestamp < ?")
        params.append(until)
    if status:
        clauses.append("status = ?")
        params.append(status)
    with closing(_connect()) as conn:
        rows = conn.execute(
            f"SELECT timestamp, status FROM Attendance WHERE {' AND '.join(clauses)} "  # noqa: S608
            "ORDER BY timestamp DESC LIMIT ?", (*params, _limit(limit))).fetchall()
    return [{"timestamp": r[0], "status": r[1]} for r in rows]


def count_by_status(name: str, start: Optional[str] = None, end: Optional[str] = None) -> Dict[str, int]:
    """某人在區間內各種狀態的次數，例如 {"上班": 10, "遲到": 2, "下班": 11}。"""
    since, until = _range(start, end)
    clauses, params = ["name = ?"], [name]
    if since:
        clauses.append("timestamp >= ?")
        params.append(since)
    if until:
        clauses.append("timestamp < ?")
        params.append(until)
    with closing(_connect()) as conn:
        rows = conn.execute(
            f"SELECT status, COUNT(*) FROM Attendance WHERE {' AND '.join(clauses)} "  # noqa: S608
            "GROUP BY status", params).fetchall()
    return dict(rows)


def people_with_status(day: str, status: str) -> List[Dict]:
    """某一天出現指定狀態的人與時間，例如那天的遲到名單。"""
    since, until = _range(day, day)
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT name, timestamp FROM Attendance WHERE timestamp >= ? AND timestamp < ? AND status = ? "
            "ORDER BY timestamp", (since, until, status)).fetchall()
    return [{"name": r[0], "timestamp": r[1]} for r in rows]


def daily_summary(day: str) -> Dict:
    """
    某一天的出勤摘要：有打卡的人數、遲到、早退、加班名單，以及在職但當天沒有任何打卡紀錄的人。
    「沒有打卡紀錄」不等於缺勤：系統沒有假日與請假資料，週末、國定假日、請假都會出現在這份名單。
    """
    since, until = _range(day, day)
    with closing(_connect()) as conn:
        punched = conn.execute(
            "SELECT COUNT(DISTINCT name) FROM Attendance WHERE timestamp >= ? AND timestamp < ?",
            (since, until)).fetchone()[0]
        no_record = [r[0] for r in conn.execute(
            "SELECT name FROM Users WHERE COALESCE(is_active, 1) = 1 AND name NOT IN "
            "(SELECT name FROM Attendance WHERE timestamp >= ? AND timestamp < ?) ORDER BY name",
            (since, until))]
    return {
        "date": day,
        "punched_count": punched,
        "late": people_with_status(day, STATUS_LATE),
        "early_leave": people_with_status(day, STATUS_EARLY_LEAVE),
        "overtime": people_with_status(day, STATUS_OVERTIME),
        "no_record": no_record,
    }


def anomaly_events(start: Optional[str] = None, end: Optional[str] = None,
                   event_type: Optional[str] = None, limit: int = 100) -> List[Dict]:
    """異常事件（如尾隨），新的在前；只回傳證據照片的檔案路徑，不讀取照片內容。"""
    since, until = _range(start, end)
    clauses, params = [], []
    if since:
        clauses.append("timestamp >= ?")
        params.append(since)
    if until:
        clauses.append("timestamp < ?")
        params.append(until)
    if event_type:
        clauses.append("event_type = ?")
        params.append(event_type)
    where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
    with closing(_connect()) as conn:
        rows = conn.execute(
            f"SELECT timestamp, event_type, detail, evidence_path FROM AnomalyEvents {where}"  # noqa: S608
            "ORDER BY timestamp DESC, id DESC LIMIT ?", (*params, _limit(limit))).fetchall()
    return [{"timestamp": r[0], "event_type": r[1], "detail": r[2], "evidence_path": r[3]} for r in rows]

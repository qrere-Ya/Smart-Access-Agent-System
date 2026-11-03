"""
attendance_report.py：每日出勤報告的純邏輯（不碰任何畫面元件）。

主要功能：
  - build_report：指定日期，彙整每位在職員工的狀態（準時、遲到、早退、缺勤、未打卡、假日、請假）與當日異常事件數
    打卡紀錄優先用 user_id 對應員工，舊紀錄（user_id 為空）才退回姓名（同名多人時不猜測）；
    假日與每週休息日（預設週六、週日，可設定）不列缺勤，請假中的員工顯示「請假」；休息或請假當天若仍有打卡，以實際打卡狀態為準
  - export_csv：匯出 CSV（utf-8-sig 讓 Excel 直接開得起來），並防 CSV 公式注入
  只做參數化的唯讀查詢；「缺勤」代表非假日、非請假且當天完全沒有打卡紀錄。

串接模組：
  - database_mgr：資料庫路徑
  - report_panel：畫面呼叫 build_report 與 export_csv
"""
import csv
import datetime
import sqlite3
from contextlib import closing

from shared import database_mgr

STATUS_ON_TIME = "準時"
STATUS_LATE = "遲到"
STATUS_EARLY_LEAVE = "早退"
STATUS_ABSENT = "缺勤"
STATUS_NO_CLOCK_OUT = "未打卡"  # 有上班打卡但沒有下班紀錄
STATUS_HOLIDAY = "假日"
STATUS_LEAVE = "請假"

# 打卡紀錄表 Attendance.status 裡的文字
_PUNCH_LATE = "遲到"
_PUNCH_EARLY_LEAVE = "早退"
_PUNCH_OVERTIME = "下班 (加班)"

CSV_HEADER = ["員工編號", "姓名", "狀態", "上班時間", "下班時間", "備註"]
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r", "\n")


def parse_date(text):
    """驗證 YYYY-MM-DD 並回傳標準字串；格式或日期不合法時丟 ValueError。"""
    try:
        return datetime.datetime.strptime(str(text).strip(), "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        raise ValueError("日期格式需為 YYYY-MM-DD") from None


def _day_range(day):
    """回傳 (當天 00:00:00, 隔天 00:00:00) 字串，用於時間區間查詢。"""
    start = datetime.datetime.strptime(day, "%Y-%m-%d")
    fmt = "%Y-%m-%d %H:%M:%S"
    return start.strftime(fmt), (start + datetime.timedelta(days=1)).strftime(fmt)


def _classify(records, holiday_name=None, leave=None):
    """由當天依時間排序的 [(時間字串, 狀態)] 回傳 (狀態, 上班, 下班, 備註)；優先序：遲到、早退、未打卡、準時。"""
    if not records:
        if holiday_name is not None:
            return STATUS_HOLIDAY, "", "", holiday_name
        if leave is not None:
            return STATUS_LEAVE, "", "", "；".join(x for x in (leave["kind"], leave["note"]) if x)
        return STATUS_ABSENT, "", "", "當天沒有任何打卡紀錄"
    clock_in = records[0]
    clock_out = records[-1] if len(records) > 1 else None
    status, notes = STATUS_ON_TIME, []
    if clock_in[1] == _PUNCH_LATE:
        status = STATUS_LATE
        notes.append("上班遲到")
    if clock_out is None:
        notes.append("沒有下班打卡")
        if status == STATUS_ON_TIME:
            status = STATUS_NO_CLOCK_OUT
    elif clock_out[1] == _PUNCH_EARLY_LEAVE:
        notes.append("提早下班")
        if status == STATUS_ON_TIME:
            status = STATUS_EARLY_LEAVE
    elif clock_out[1] == _PUNCH_OVERTIME:
        notes.append("加班")
    return status, clock_in[0][11:19], (clock_out[0][11:19] if clock_out else ""), "；".join(notes)


def build_report(day):
    """彙整指定日期的出勤報告，回傳 {"date", "rows"（每位在職員工一筆）, "counts"（各狀態人數）, "anomaly_count"}。"""
    day = parse_date(day)
    since, until = _day_range(day)
    with closing(sqlite3.connect(database_mgr.DB_PATH, timeout=5)) as conn:
        users = conn.execute(
            "SELECT id, name, COALESCE(employee_id, '') FROM Users WHERE COALESCE(is_active, 1) = 1 "
            "ORDER BY employee_id, name").fetchall()
        punches = conn.execute(
            "SELECT user_id, name, timestamp, status FROM Attendance WHERE timestamp >= ? AND timestamp < ? "
            "ORDER BY timestamp, id", (since, until)).fetchall()
        day_off = database_mgr.day_off_reason(day, conn)
        leaves = conn.execute(
            "SELECT user_id, kind, note FROM Leaves WHERE start_date <= ? AND end_date >= ? ORDER BY id",
            (day, day)).fetchall()
        anomaly_count = conn.execute(
            "SELECT COUNT(*) FROM AnomalyEvents WHERE timestamp >= ? AND timestamp < ?",
            (since, until)).fetchone()[0]
    # 有 user_id 的紀錄用 user_id 對應；舊紀錄才用姓名，且同名多人時不猜測
    name_count = {}
    for _uid, name, _emp in users:
        name_count[name] = name_count.get(name, 0) + 1
    by_id, by_name = {}, {}
    for uid, name, ts, st in punches:
        if uid is not None:
            by_id.setdefault(uid, []).append((ts, st))
        elif name_count.get(name) == 1:
            by_name.setdefault(name, []).append((ts, st))
    leave_by_id = {}
    for uid, kind, note in leaves:
        leave_by_id.setdefault(uid, {"kind": kind, "note": note})

    rows, counts = [], {}
    for uid, name, emp_id in users:
        records = sorted(by_id.get(uid, []) + by_name.get(name, []))
        status, t_in, t_out, note = _classify(records, day_off, leave_by_id.get(uid))
        rows.append({"employee_id": emp_id, "name": name, "status": status,
                     "clock_in": t_in, "clock_out": t_out, "note": note})
        counts[status] = counts.get(status, 0) + 1
    return {"date": day, "rows": rows, "counts": counts, "anomaly_count": anomaly_count}


def sanitize_cell(value):
    """防 CSV 公式注入：以 = + - @（或 tab、換行）開頭的文字前面加單引號。"""
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(_FORMULA_PREFIXES) else text


def export_csv(report, path):
    """把報告寫成 CSV（utf-8-sig），最後附上異常事件數；回傳寫出的員工資料列數。"""
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(CSV_HEADER)
        for r in report["rows"]:
            writer.writerow([sanitize_cell(r[k]) for k in
                             ("employee_id", "name", "status", "clock_in", "clock_out", "note")])
        writer.writerow([])
        writer.writerow([f"{report['date']} 異常事件數", report["anomaly_count"]])
    return len(report["rows"])

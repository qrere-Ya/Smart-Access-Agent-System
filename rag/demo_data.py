"""
demo_data.py：產生與移除示範用的假資料（假員工、假出勤、假請假、假異常事件），給遠端測試入口展示用。

主要功能：
  - add：新增 5 位假員工與近兩週的出勤、請假、異常事件（請假那幾天沒有打卡）；已經有假員工時不重複新增
  - extend：把假員工的出勤補到今天起的前方幾天（展示當天的日報才有內容）；請假日、週末、已有打卡的不補
  - remove：只刪除假資料（員工編號 DEMO- 開頭的員工與他們的出勤、請假，以及標註示範的異常事件），真實資料不動
  - 假員工的人臉特徵是長度極大的向量，距離任何真實人臉都遠大於門檻，不會被門禁誤認成其他人；
    每位假員工的值各不相同（第 n 位每個維度都是 100×n），他們彼此之間也遠大於門檻，真實資料檢查才不會判成互相混淆；
    維度跟資料庫裡現有的特徵一致（空資料庫用 512），避免門禁比對時維度不合出錯
  - 不新增假日（假日是公司行事曆，會影響真實資料）

串接模組：
  - shared.database_mgr：沿用它的資料表與 DB_PATH
執行：python -m rag.demo_data add／extend／remove（只在你明確要時才執行，會寫入 runtime/database.db）
"""
import datetime
import random
import sqlite3
import sys
from contextlib import closing

import numpy as np

from shared import database_mgr

NAME_PREFIX = "測試"
EMP_PREFIX = "DEMO-"
DEMO_MARK = "（示範資料）"
DEFAULT_DIM = 512
FAR_VALUE = 100.0       # 第 n 位假員工的特徵每個維度都填 FAR_VALUE×n：和真實人臉、彼此的距離都遠超過任何門檻
DEMO_PEOPLE = [("測試甲", "M"), ("測試乙", "F"), ("測試丙", "M"), ("測試丁", "F"), ("測試戊", "M")]
DAYS_BACK = 14


def _embedding_dim(conn) -> int:
    row = conn.execute("SELECT embedding FROM Users WHERE employee_id NOT LIKE ? LIMIT 1",
                       (EMP_PREFIX + "%",)).fetchone()
    shape = getattr(row[0], "shape", None) if row else None
    return int(shape[0]) if shape and len(shape) == 1 else DEFAULT_DIM


def _far_embedding(dim, n):
    return np.full(dim, FAR_VALUE * n, dtype=np.float32)


def _connect():
    return closing(sqlite3.connect(database_mgr.DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES, timeout=5))


def exists() -> bool:
    with _connect() as conn:
        return conn.execute("SELECT 1 FROM Users WHERE employee_id LIKE ?", (EMP_PREFIX + "%",)).fetchone() is not None


def _day_punches(rng) -> list:
    """一位員工一天的打卡 [(時間, 狀態)]；這天沒來回傳空清單。"""
    roll = rng.random()
    if roll < 0.08:
        return []
    late = roll < 0.30
    in_time = f"08:{rng.randint(10, 40):02d}:00" if late else f"07:{rng.randint(45, 59):02d}:00"
    early = rng.random() < 0.12
    out_time = f"17:{rng.randint(0, 20):02d}:00" if early else f"18:{rng.randint(0, 30):02d}:00"
    return [(in_time, "遲到" if late else "上班"), (out_time, "早退" if early else "下班")]


def add(today: datetime.date = None) -> dict:
    """新增假資料，回傳各類筆數；已存在假員工時不動作（回傳全 0）。"""
    today = today or datetime.date.today()
    counts = {"users": 0, "attendance": 0, "leaves": 0, "anomalies": 0}
    if exists():
        return counts
    rng = random.Random(7)  # noqa: S311  # nosec B311  固定種子的假資料，與安全無關
    leaves = [("測試乙", 1, 1, "病假"), ("測試丁", 3, 4, "特休"), ("測試丙", -3, -2, "事假")]
    leave_days = {(name, today + datetime.timedelta(days=d)) for name, start, end, _ in leaves
                  for d in range(start, end + 1)}
    with _connect() as conn:
        dim = _embedding_dim(conn)
        ids = {}
        for i, (name, gender) in enumerate(DEMO_PEOPLE, start=1):
            cur = conn.execute(
                "INSERT INTO Users (name, gender, employee_id, embedding, role, is_active) "
                "VALUES (?, ?, ?, ?, 'employee', 1)",
                (name, gender, f"{EMP_PREFIX}{i:03d}", _far_embedding(dim, i)))
            ids[name] = cur.lastrowid
            counts["users"] += 1
        for offset in range(DAYS_BACK, 0, -1):
            day = today - datetime.timedelta(days=offset)
            if day.weekday() >= 5:
                continue
            for name, user_id in ids.items():
                if (name, day) in leave_days:
                    continue                                  # 請假那天沒有打卡，資料才前後一致
                for clock, status in _day_punches(rng):
                    conn.execute("INSERT INTO Attendance (name, timestamp, status, user_id) VALUES (?, ?, ?, ?)",
                                 (name, f"{day.isoformat()} {clock}", status, user_id))
                    counts["attendance"] += 1
        for name, start, end, kind in leaves:
            conn.execute("INSERT INTO Leaves (user_id, start_date, end_date, kind, note) VALUES (?, ?, ?, ?, ?)",
                         (ids[name], (today + datetime.timedelta(days=start)).isoformat(),
                          (today + datetime.timedelta(days=end)).isoformat(), kind, DEMO_MARK))
            counts["leaves"] += 1
        for days_ago, hour in ((2, 9), (5, 14)):
            when = datetime.datetime.combine(today - datetime.timedelta(days=days_ago), datetime.time(hour, 12))
            conn.execute("INSERT INTO AnomalyEvents (timestamp, event_type, detail, evidence_path) "
                         "VALUES (?, 'tailgate', ?, NULL)",
                         (when.strftime("%Y-%m-%d %H:%M:%S"), "偵測到尾隨" + DEMO_MARK))
            counts["anomalies"] += 1
        conn.commit()
    return counts


def extend(today: datetime.date = None, days_ahead: int = 2) -> dict:
    """把假員工的出勤補到 today 起 days_ahead 天內（含今天）；請假日、週末、已經有打卡的日子不補，可重複執行。
    每個人每天的內容由日期與假員工編號（DEMO-001…）決定，同一天重跑結果一致；
    也會把假員工的人臉特徵修成各不相同（早期版本五位完全一樣）。"""
    today = today or datetime.date.today()
    counts = {"attendance": 0, "days": 0}
    with _connect() as conn:
        people = conn.execute("SELECT id, name, employee_id FROM Users WHERE employee_id LIKE ? ORDER BY id",
                              (EMP_PREFIX + "%",)).fetchall()
        dim = _embedding_dim(conn)
        for user_id, _name, emp_id in people:
            conn.execute("UPDATE Users SET embedding=? WHERE id=?",
                         (_far_embedding(dim, int(emp_id[len(EMP_PREFIX):])), user_id))
        for offset in range(days_ahead + 1):
            day = today + datetime.timedelta(days=offset)
            if day.weekday() >= 5:
                continue
            filled = False
            for user_id, name, emp_id in people:
                start, end = f"{day.isoformat()} 00:00:00", f"{(day + datetime.timedelta(days=1)).isoformat()} 00:00:00"
                has_punch = conn.execute("SELECT 1 FROM Attendance WHERE user_id=? AND timestamp>=? AND timestamp<?",
                                         (user_id, start, end)).fetchone()
                on_leave = conn.execute("SELECT 1 FROM Leaves WHERE user_id=? AND start_date<=? AND end_date>=?",
                                        (user_id, day.isoformat(), day.isoformat())).fetchone()
                if has_punch or on_leave:
                    continue
                seed = day.toordinal() * 1000 + int(emp_id[len(EMP_PREFIX):])
                for clock, status in _day_punches(random.Random(seed)):  # noqa: S311  # nosec B311
                    conn.execute("INSERT INTO Attendance (name, timestamp, status, user_id) VALUES (?, ?, ?, ?)",
                                 (name, f"{day.isoformat()} {clock}", status, user_id))
                    counts["attendance"] += 1
                    filled = True
            counts["days"] += 1 if filled else 0
        conn.commit()
    return counts


def remove() -> dict:
    """刪除假資料，回傳各類刪除筆數。"""
    counts = {"users": 0, "attendance": 0, "leaves": 0, "anomalies": 0}
    with _connect() as conn:
        rows = conn.execute("SELECT id FROM Users WHERE employee_id LIKE ?", (EMP_PREFIX + "%",)).fetchall()
        for (user_id,) in rows:
            counts["attendance"] += conn.execute("DELETE FROM Attendance WHERE user_id = ?", (user_id,)).rowcount
            counts["leaves"] += conn.execute("DELETE FROM Leaves WHERE user_id = ?", (user_id,)).rowcount
            counts["users"] += conn.execute("DELETE FROM Users WHERE id = ?", (user_id,)).rowcount
        counts["anomalies"] = conn.execute("DELETE FROM AnomalyEvents WHERE detail LIKE ? AND evidence_path IS NULL",
                                           ("%" + DEMO_MARK + "%",)).rowcount
        conn.commit()
    return counts


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if args not in (["add"], ["extend"], ["remove"]):
        raise SystemExit("用法：python -m rag.demo_data add／extend／remove")
    database_mgr.init_db()
    result = {"add": add, "extend": extend, "remove": remove}[args[0]]()
    print({"add": "新增", "extend": "補出勤", "remove": "刪除"}[args[0]] + f"完成：{result}")


if __name__ == "__main__":
    main()

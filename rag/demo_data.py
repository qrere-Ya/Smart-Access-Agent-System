"""
demo_data.py：產生與移除示範用的假資料（假員工、假出勤、假請假、假異常事件），給遠端測試入口展示用。

主要功能：
  - add：新增 5 位假員工與近兩週的出勤、請假、異常事件；已經有假員工時不重複新增
  - remove：只刪除假資料（員工編號 DEMO- 開頭的員工與他們的出勤、請假，以及標註示範的異常事件），真實資料不動
  - 假員工的人臉特徵是長度極大的向量，距離任何真實人臉都遠大於門檻，不會被門禁誤認成其他人；
    維度跟資料庫裡現有的特徵一致（空資料庫用 512），避免門禁比對時維度不合出錯
  - 不新增假日（假日是公司行事曆，會影響真實資料）

串接模組：
  - shared.database_mgr：沿用它的資料表與 DB_PATH
執行：python -m rag.demo_data add／remove（只在你明確要時才執行，會寫入 runtime/database.db）
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
FAR_VALUE = 100.0       # 特徵每個維度都填這個值：和正規化的真實人臉距離遠超過任何門檻
DEMO_PEOPLE = [("測試甲", "M"), ("測試乙", "F"), ("測試丙", "M"), ("測試丁", "F"), ("測試戊", "M")]
DAYS_BACK = 14


def _embedding_dim(conn) -> int:
    row = conn.execute("SELECT embedding FROM Users WHERE employee_id NOT LIKE ? LIMIT 1",
                       (EMP_PREFIX + "%",)).fetchone()
    shape = getattr(row[0], "shape", None) if row else None
    return int(shape[0]) if shape and len(shape) == 1 else DEFAULT_DIM


def _connect():
    return closing(sqlite3.connect(database_mgr.DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES, timeout=5))


def exists() -> bool:
    with _connect() as conn:
        return conn.execute("SELECT 1 FROM Users WHERE employee_id LIKE ?", (EMP_PREFIX + "%",)).fetchone() is not None


def add(today: datetime.date = None) -> dict:
    """新增假資料，回傳各類筆數；已存在假員工時不動作（回傳全 0）。"""
    today = today or datetime.date.today()
    counts = {"users": 0, "attendance": 0, "leaves": 0, "anomalies": 0}
    if exists():
        return counts
    rng = random.Random(7)  # noqa: S311  # nosec B311  固定種子的假資料，與安全無關
    with _connect() as conn:
        embedding = np.full(_embedding_dim(conn), FAR_VALUE, dtype=np.float32)
        ids = {}
        for i, (name, gender) in enumerate(DEMO_PEOPLE, start=1):
            cur = conn.execute(
                "INSERT INTO Users (name, gender, employee_id, embedding, role, is_active) "
                "VALUES (?, ?, ?, ?, 'employee', 1)",
                (name, gender, f"{EMP_PREFIX}{i:03d}", embedding))
            ids[name] = cur.lastrowid
            counts["users"] += 1
        for offset in range(DAYS_BACK, 0, -1):
            day = today - datetime.timedelta(days=offset)
            if day.weekday() >= 5:
                continue
            for name, user_id in ids.items():
                roll = rng.random()
                if roll < 0.08:
                    continue                                  # 這天沒來
                late = roll < 0.30
                in_time = f"08:{rng.randint(10, 40):02d}:00" if late else f"07:{rng.randint(45, 59):02d}:00"
                early = rng.random() < 0.12
                out_time = f"17:{rng.randint(0, 20):02d}:00" if early else f"18:{rng.randint(0, 30):02d}:00"
                rows = [(in_time, "遲到" if late else "上班"), (out_time, "早退" if early else "下班")]
                for clock, status in rows:
                    conn.execute("INSERT INTO Attendance (name, timestamp, status, user_id) VALUES (?, ?, ?, ?)",
                                 (name, f"{day.isoformat()} {clock}", status, user_id))
                    counts["attendance"] += 1
        leaves = [("測試乙", 1, 1, "病假"), ("測試丁", 3, 4, "特休"), ("測試丙", -3, -2, "事假")]
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
    if args not in (["add"], ["remove"]):
        raise SystemExit("用法：python -m rag.demo_data add　或　python -m rag.demo_data remove")
    database_mgr.init_db()
    result = add() if args == ["add"] else remove()
    print(("新增" if args == ["add"] else "刪除") + f"完成：{result}")


if __name__ == "__main__":
    main()

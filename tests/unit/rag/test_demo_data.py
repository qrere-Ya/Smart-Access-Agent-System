"""
test_demo_data.py：示範假資料的新增與移除（臨時資料庫）。
"""
import datetime
import sqlite3

import numpy as np

from rag import demo_data, queries
from shared import database_mgr

TODAY = datetime.date(2026, 11, 4)


def count(db, table):
    con = sqlite3.connect(db)
    n = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608
    con.close()
    return n


def test_add_creates_demo_people_and_their_records(db):
    result = demo_data.add(TODAY)
    assert result["users"] == 5 and result["attendance"] > 0 and result["leaves"] == 3 and result["anomalies"] == 2
    names = [p["name"] for p in queries.roster()]
    assert "測試甲" in names and len(names) == 5


def test_add_twice_does_not_duplicate(db):
    demo_data.add(TODAY)
    assert demo_data.add(TODAY) == {"users": 0, "attendance": 0, "leaves": 0, "anomalies": 0}
    assert count(db, "Users") == 5


def test_remove_deletes_only_demo_data(db, add_user, add_attendance):
    add_user("王小明", "E001")
    add_attendance("王小明", "2026-11-03 07:50:00", "上班")
    demo_data.add(TODAY)
    database_mgr.add_anomaly_event("tailgate", evidence_path="real.jpg", detail="真實事件")
    removed = demo_data.remove()
    assert removed["users"] == 5 and removed["leaves"] == 3 and removed["anomalies"] == 2
    assert count(db, "Users") == 1 and count(db, "Attendance") == 1
    assert count(db, "AnomalyEvents") == 1 and count(db, "Leaves") == 0


def test_demo_faces_use_existing_dimension_and_never_match_a_real_face(db, add_user):
    real = np.full(4, 0.5, dtype=np.float32)           # 長度為 1 的特徵，等同正規化後的真實人臉
    add_user("王小明", "E001", embedding=real)
    demo_data.add(TODAY)
    assert database_mgr.recognize_face(real) == ("王小明", 0.0)
    name, dist = database_mgr.recognize_face(-real)     # 另一張臉：真實員工和假員工都不該認成功
    assert name == "Unknown" and dist < 5
    con = sqlite3.connect(db, detect_types=sqlite3.PARSE_DECLTYPES)
    shapes = {row[0].shape for row in con.execute("SELECT embedding FROM Users WHERE employee_id LIKE 'DEMO-%'")}
    con.close()
    assert shapes == {(4,)}


def test_empty_database_uses_default_dimension(db):
    demo_data.add(TODAY)
    con = sqlite3.connect(db, detect_types=sqlite3.PARSE_DECLTYPES)
    shape = con.execute("SELECT embedding FROM Users LIMIT 1").fetchone()[0].shape
    con.close()
    assert shape == (demo_data.DEFAULT_DIM,)


def test_no_holidays_are_added_and_only_weekdays_have_attendance(db):
    demo_data.add(TODAY)
    assert count(db, "Holidays") == 0
    con = sqlite3.connect(db)
    days = {r[0][:10] for r in con.execute("SELECT timestamp FROM Attendance")}
    con.close()
    assert days and all(datetime.date.fromisoformat(d).weekday() < 5 for d in days)


def test_nobody_has_attendance_on_a_day_they_are_on_leave(db):
    demo_data.add(TODAY)
    con = sqlite3.connect(db)
    leaves = con.execute(
        "SELECT u.name, l.start_date, l.end_date FROM Leaves l JOIN Users u ON u.id = l.user_id").fetchall()
    assert leaves
    for name, start, end in leaves:
        clash = con.execute(
            "SELECT COUNT(*) FROM Attendance WHERE name = ? AND substr(timestamp, 1, 10) BETWEEN ? AND ?",
            (name, start, end)).fetchone()[0]
        assert clash == 0, (name, start, end)
    con.close()


def test_extend_fills_the_days_ahead_once_and_skips_leave_days_and_weekends(db):
    demo_data.add(TODAY)                                   # 2026-11-04（週三）；乙 11-05 請病假
    first = demo_data.extend(TODAY, days_ahead=3)          # 11-04 已有打卡；11-05 乙請假；11-06 週五；11-07 週六
    assert first["attendance"] > 0
    assert demo_data.extend(TODAY, days_ahead=3) == {"attendance": 0, "days": 0}
    con = sqlite3.connect(db)
    rows = con.execute("SELECT name, substr(timestamp, 1, 10) FROM Attendance "
                       "WHERE timestamp >= '2026-11-05'").fetchall()
    con.close()
    days = {d for _, d in rows}
    assert "2026-11-06" in days and "2026-11-07" not in days
    assert ("測試乙", "2026-11-05") not in set(rows)


def test_extend_is_deterministic_per_day_and_removed_with_the_demo_people(db):
    demo_data.add(TODAY)
    demo_data.extend(TODAY, days_ahead=2)
    con = sqlite3.connect(db)
    first = con.execute("SELECT name, timestamp, status FROM Attendance ORDER BY id").fetchall()
    con.close()
    demo_data.remove()
    assert count(db, "Attendance") == 0
    demo_data.add(TODAY)
    demo_data.extend(TODAY, days_ahead=2)
    con = sqlite3.connect(db)
    assert con.execute("SELECT name, timestamp, status FROM Attendance ORDER BY id").fetchall() == first
    con.close()


def _demo_embeddings(db):
    con = sqlite3.connect(db, detect_types=sqlite3.PARSE_DECLTYPES)
    rows = [e for (e,) in con.execute("SELECT embedding FROM Users WHERE employee_id LIKE 'DEMO-%' ORDER BY id")]
    con.close()
    return rows


def test_demo_people_are_far_from_each_other_and_from_real_faces(db, add_user):
    add_user("王小明", "E001", embedding=np.ones(512, dtype=np.float32) / np.sqrt(512))
    demo_data.add(TODAY)
    fakes = _demo_embeddings(db)
    real = np.ones(512, dtype=np.float32) / np.sqrt(512)
    assert len(fakes) == 5
    for i, a in enumerate(fakes):
        assert np.linalg.norm(a - real) > 100
        for b in fakes[i + 1:]:
            assert np.linalg.norm(a - b) > 100              # 門檻是 0.9 左右，遠遠超過


def test_extend_repairs_old_demo_people_that_all_shared_one_embedding(db):
    demo_data.add(TODAY)
    con = sqlite3.connect(db)
    same = np.full(512, 100.0, dtype=np.float32)
    con.execute("UPDATE Users SET embedding=? WHERE employee_id LIKE 'DEMO-%'", (same,))
    con.commit()
    con.close()
    assert len({e.tobytes() for e in _demo_embeddings(db)}) == 1
    demo_data.extend(TODAY, days_ahead=0)
    assert len({e.tobytes() for e in _demo_embeddings(db)}) == 5

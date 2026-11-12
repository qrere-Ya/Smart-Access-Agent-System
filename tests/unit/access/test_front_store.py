"""前台本機儲存：人臉快取（整份替換、不存照片）、FaceIndex 與後台比對一致、持久佇列（重開不掉、上限、永久失敗移走）。"""
import sqlite3

import numpy as np
import pytest

from access.front_store import FaceIndex, FrontStore, MAX_QUEUE_EVENTS
from shared import link_protocol as lp

rng = np.random.default_rng(7)


def face(i, name, emb=None):
    return {"id": i, "name": name, "employee_id": f"E{i}", "role": "employee",
            "embedding": rng.normal(size=512).astype(np.float32) if emb is None else emb}


@pytest.fixture
def store(tmp_path):
    return FrontStore(str(tmp_path / "front.db"))


def event(n=0, kind="no_scan_walkby"):
    import datetime as dt
    return lp.build_event(kind, dt.datetime(2026, 11, 10, 8, 0, n % 60), detail=f"e{n}")


# --- 人臉快取 ---

def test_faces_round_trip_and_replace_is_all_or_nothing(store):
    a, b = face(1, "甲"), face(2, "乙")
    store.replace_faces("v1" * 32, [a, b])
    loaded = store.load_faces()
    assert [f["name"] for f in loaded] == ["甲", "乙"] and np.allclose(loaded[0]["embedding"], a["embedding"])
    assert store.faces_version() == "v1" * 32
    broken = [face(3, "丙"), {"id": 3, "name": "重複編號", "employee_id": "x", "role": "employee",
                             "embedding": a["embedding"]}]                  # 主鍵重複 → 整批失敗
    with pytest.raises(sqlite3.IntegrityError):
        store.replace_faces("v2" * 32, broken)
    assert [f["name"] for f in store.load_faces()] == ["甲", "乙"] and store.faces_version() == "v1" * 32


def test_the_cache_has_no_photo_columns(store):
    con = sqlite3.connect(store.path)
    columns = {r[1] for r in con.execute("PRAGMA table_info(faces)")}
    con.close()
    assert columns == {"id", "name", "employee_id", "role", "embedding"}


# --- FaceIndex 與後台一致 ---

def test_the_index_matches_the_backend_recognition(db, add_user):
    from shared import database_mgr

    people = [face(i + 1, n) for i, n in enumerate(["甲", "乙", "丙"])]
    for p in people:
        add_user(p["name"], p["employee_id"], embedding=p["embedding"])
    index = FaceIndex(people)
    probes = [people[1]["embedding"], people[2]["embedding"] + 0.01, rng.normal(size=512).astype(np.float32),
              people[0]["embedding"] * 0.99]
    for probe in probes:
        name, dist = database_mgr.recognize_face(probe)
        got = index.recognize(probe)
        assert (got[0], got[1]) == (name, dist)


def test_the_index_reports_ids_empty_and_wrong_sizes():
    people = [face(5, "甲")]
    index = FaceIndex(people)
    assert index.recognize(people[0]["embedding"]) == ("甲", 0.0, 5)
    assert index.recognize(np.zeros(8, dtype=np.float32))[0] == "Empty DB"       # 維度不對不報錯
    assert FaceIndex([]).recognize(people[0]["embedding"]) == ("Empty DB", 99.9, None)
    far = people[0]["embedding"] + 10
    assert index.recognize(far)[0] == "Unknown" and index.recognize(far)[2] is None


# --- 佇列 ---

def test_the_queue_survives_a_restart_and_keeps_order(tmp_path):
    path = str(tmp_path / "front.db")
    first = FrontStore(path)
    events = [event(i) for i in range(3)]
    for e in events:
        first.enqueue(e)
    reopened = FrontStore(path)                                                  # 模擬程式或電腦重開
    assert [e["event_id"] for _s, e in reopened.pending()] == [e["event_id"] for e in events]


def test_sent_events_leave_the_queue_and_an_id_is_never_queued_twice(store):
    e = event(1)
    store.enqueue(e)
    store.enqueue(e)
    assert store.pending_count() == 1
    store.mark_sent(store.pending()[0][0])
    assert store.pending_count() == 0


def test_a_permanently_rejected_event_moves_out_with_a_reason_and_no_photo(store):
    when = __import__("datetime").datetime(2026, 11, 10, 8, 0, 0)
    e = lp.build_event("tailgate", when, evidence=b"\xff\xd8\xff" + b"1" * 20)
    store.enqueue(e)
    store.mark_rejected(store.pending()[0][0], "422 unknown_user")
    assert store.pending_count() == 0 and store.rejected_count() == 1
    con = sqlite3.connect(store.path)
    row = con.execute("SELECT * FROM rejected").fetchone()
    con.close()
    assert "422" in row[4] and e["evidence"] not in row


def test_the_queue_is_capped_and_counts_what_it_dropped(store, monkeypatch):
    from access import front_store

    monkeypatch.setattr(front_store, "MAX_QUEUE_EVENTS", 5)
    for i in range(8):
        store.enqueue(event(i))
    assert store.pending_count() == 5 and store.dropped_count() == 3
    assert [e["detail"] for _s, e in store.pending()][0] == "e3"                  # 丟的是最舊的
    assert MAX_QUEUE_EVENTS == 2000

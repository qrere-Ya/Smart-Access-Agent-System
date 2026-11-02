"""
真實資料自動檢查（只讀真資料庫的「複本」，不會改到 runtime/database.db）。
驗證：舊資料庫升級後資料不變、不損壞、重複升級不出錯；人臉特徵可讀、維度一致；
任兩位員工的人臉特徵距離要大於登入門檻（否則兩人會被互相誤認）。找不到真資料庫就略過。
"""
import itertools
import os
import sqlite3
from contextlib import closing

import numpy as np
import pytest

from shared import paths

pytestmark = pytest.mark.integration

REAL_DB = os.path.join(paths.PROJECT_ROOT, "runtime", "database.db")
LOGIN_DISTANCE_MAX = 0.9          # 與 admin/face_login.DISTANCE_MAX 相同
TABLES = ("Users", "Attendance", "AnomalyEvents", "calendar")


@pytest.fixture
def real_copy(tmp_path, monkeypatch):
    """真資料庫的複本，database_mgr 改用它。"""
    if not os.path.exists(REAL_DB):
        pytest.skip("沒有真實資料庫")
    copy = str(tmp_path / "copy.db")
    with closing(sqlite3.connect(f"file:{REAL_DB}?mode=ro", uri=True)) as src, \
            closing(sqlite3.connect(copy)) as dst:
        src.backup(dst)                           # 以唯讀開啟再備份，不會寫到原檔
    from shared import database_mgr
    monkeypatch.setattr(database_mgr, "DB_PATH", copy)
    return copy


def _counts(path):
    with closing(sqlite3.connect(path)) as con:
        existing = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        return {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]  # noqa: S608 表名來自上方固定清單
                for t in TABLES if t in existing}


def test_upgrade_keeps_all_rows_and_is_repeatable(real_copy):
    from shared import database_mgr

    before = _counts(real_copy)
    database_mgr.init_db()
    database_mgr.init_db()                        # 第二次升級不能出錯、不能改資料
    after = _counts(real_copy)
    for table, n in before.items():
        assert after[table] == n, f"{table} 升級後筆數改變：{n} → {after[table]}"
    with closing(sqlite3.connect(real_copy)) as con:
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        att_cols = {r[1] for r in con.execute("PRAGMA table_info(Attendance)")}
        assert "user_id" in att_cols
        assert {"Holidays", "Leaves"} <= {r[0] for r in con.execute("SELECT name FROM sqlite_master")}


def test_backfilled_user_id_matches_unique_names_only(real_copy):
    from shared import database_mgr

    database_mgr.init_db()
    with closing(sqlite3.connect(real_copy)) as con:
        for name, user_id in con.execute("SELECT name, user_id FROM Attendance"):
            same_name = con.execute("SELECT id FROM Users WHERE name=?", (name,)).fetchall()
            if user_id is None:
                assert len(same_name) != 1, f"{name} 名字唯一卻沒回填"
            else:
                assert [(user_id,)] == same_name, f"{name} 回填到錯的員工"


def _embeddings():
    with closing(sqlite3.connect(REAL_DB, detect_types=sqlite3.PARSE_DECLTYPES)) as con:
        return [(i, n, e) for i, n, e in con.execute("SELECT id, name, embedding FROM Users")]


def test_embeddings_readable_and_same_dimension():
    if not os.path.exists(REAL_DB):
        pytest.skip("沒有真實資料庫")
    rows = _embeddings()
    assert rows, "資料庫沒有員工"
    shapes = {e.shape for _, _, e in rows}
    assert len(shapes) == 1, f"人臉特徵維度不一致：{shapes}"
    assert all(np.isfinite(e).all() for _, _, e in rows)


def test_employees_are_far_enough_apart_to_not_confuse(capsys):
    """任兩位員工的特徵距離要大於登入門檻；同時印出距離表，方便肉眼確認。"""
    if not os.path.exists(REAL_DB):
        pytest.skip("沒有真實資料庫")
    rows = _embeddings()
    close = []
    with capsys.disabled():
        print("\n[員工之間的人臉特徵距離]（登入門檻 %.2f，越大越不會互相誤認）" % LOGIN_DISTANCE_MAX)
        for (ia, _, ea), (ib, _, eb) in itertools.combinations(rows, 2):
            d = float(np.linalg.norm(ea - eb))
            print(f"  員工 #{ia} 與 #{ib}：{d:.3f}")
            if d < LOGIN_DISTANCE_MAX:
                close.append((ia, ib, d))
    assert not close, f"這些員工的特徵太接近，可能被互相誤認：{close}"

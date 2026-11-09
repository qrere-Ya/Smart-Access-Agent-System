"""
front_store.py：前台電腦的本機儲存——人臉快取與持久的離線事件佇列（SQLite，只用標準函式庫與 numpy）。

主要功能：
  - 人臉快取：只存編號、姓名、員工編號、角色與特徵，不存照片；整份快取一次替換（先寫完才生效，不會讀到一半）；
    記住後台給的版本，下次問後台時帶著版本，沒變就不重送
  - FaceIndex：把快取載入成矩陣，recognize 的結果與後台資料層 recognize_face 一致
    （距離四捨五入到小數 2 位、小於門檻才算認得、距離相同保留先出現者），所以後台離線時前台照樣能辨識
  - 持久佇列：事件先寫進磁碟才算送出去，程式或電腦重開不會掉；依序補送；補送成功或後台回「重複」就移除；
    後台回「永遠不會成功」（欄位不合格、查無此人）的移到 rejected 留下原因（不含照片）；
    佇列有上限，超過時丟掉最舊的並累計 dropped，狀態會回報
  - meta：版本、時鐘差、最後同步時間、晨報（JSON）等小資料

限制：快取在前台硬碟上，硬碟被拿走就能讀到人臉特徵（沒有加密）；實體安全要靠前台機器本身。

串接模組：
  - front_agent：同步與補送；terminal_app（之後 L4）：辨識改用 FaceIndex、打卡改進佇列
"""
import datetime
import json
import os
import sqlite3
import threading
import time
from contextlib import closing

import numpy as np

from shared import link_protocol as lp

MAX_QUEUE_EVENTS = 2000
MAX_REJECTED_KEPT = 200
_SCHEMA = """
CREATE TABLE IF NOT EXISTS faces (id INTEGER PRIMARY KEY, name TEXT NOT NULL, employee_id TEXT NOT NULL,
    role TEXT NOT NULL, embedding BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS queue (seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE,
    payload TEXT NOT NULL, created REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS rejected (seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT, kind TEXT, ts TEXT,
    reason TEXT, at REAL);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


class FrontStore:
    """前台本機資料庫；每次操作自己開關連線，多執行緒安全（寫入用鎖串行）。"""

    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    # --- meta ---
    def get_meta(self, key, default=None):
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_meta(self, key, value):
        with self._lock, closing(self._connect()) as conn:
            conn.execute("INSERT INTO meta (key, value) VALUES (?, ?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                         (key, json.dumps(value, ensure_ascii=False)))
            conn.commit()

    # --- 人臉快取 ---
    def replace_faces(self, version, faces):
        """整份替換快取並記下版本；中途失敗整個還原，舊快取不受影響。"""
        with self._lock, closing(self._connect()) as conn:
            with conn:
                conn.execute("DELETE FROM faces")
                conn.executemany(
                    "INSERT INTO faces (id, name, employee_id, role, embedding) VALUES (?, ?, ?, ?, ?)",
                    [(f["id"], f["name"], f["employee_id"], f["role"],
                      np.asarray(f["embedding"], dtype="<f4").tobytes()) for f in faces])
                conn.execute("INSERT INTO meta (key, value) VALUES ('faces_version', ?) "
                             "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(version),))
                conn.execute("INSERT INTO meta (key, value) VALUES ('faces_synced_at', ?) "
                             "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(time.time()),))

    def faces_version(self):
        return self.get_meta("faces_version")

    def load_faces(self):
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT id, name, employee_id, role, embedding FROM faces ORDER BY id").fetchall()
        return [{"id": r[0], "name": r[1], "employee_id": r[2], "role": r[3],
                 "embedding": np.frombuffer(r[4], dtype="<f4").astype(np.float32)} for r in rows]

    # --- 佇列 ---
    def enqueue(self, event):
        """把事件（link_protocol.build_event 的結果）寫進佇列；超過上限丟最舊的。回傳目前佇列長度。"""
        payload = json.dumps(event, ensure_ascii=False)
        with self._lock, closing(self._connect()) as conn:
            with conn:
                conn.execute("INSERT OR IGNORE INTO queue (event_id, payload, created) VALUES (?, ?, ?)",
                             (event["event_id"], payload, time.time()))
                over = conn.execute("SELECT COUNT(*) FROM queue").fetchone()[0] - MAX_QUEUE_EVENTS
                if over > 0:
                    conn.execute("DELETE FROM queue WHERE seq IN (SELECT seq FROM queue ORDER BY seq LIMIT ?)", (over,))
                    dropped = (conn.execute("SELECT value FROM meta WHERE key='dropped'").fetchone() or ["0"])[0]
                    conn.execute("INSERT INTO meta (key, value) VALUES ('dropped', ?) "
                                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                                 (json.dumps(int(dropped) + over),))
            return conn.execute("SELECT COUNT(*) FROM queue").fetchone()[0]

    def pending(self, limit=50):
        """最舊的幾筆待補送事件：[(seq, event dict)]。"""
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT seq, payload FROM queue ORDER BY seq LIMIT ?", (limit,)).fetchall()
        return [(seq, json.loads(payload)) for seq, payload in rows]

    def pending_count(self):
        with closing(self._connect()) as conn:
            return conn.execute("SELECT COUNT(*) FROM queue").fetchone()[0]

    def mark_sent(self, seq):
        with self._lock, closing(self._connect()) as conn:
            conn.execute("DELETE FROM queue WHERE seq=?", (seq,))
            conn.commit()

    def mark_attempt(self, seq):
        with self._lock, closing(self._connect()) as conn:
            conn.execute("UPDATE queue SET attempts = attempts + 1 WHERE seq=?", (seq,))
            conn.commit()

    def mark_rejected(self, seq, reason):
        """永遠不會成功的事件：從佇列移出，記下原因（不留照片）。"""
        with self._lock, closing(self._connect()) as conn:
            with conn:
                row = conn.execute("SELECT payload FROM queue WHERE seq=?", (seq,)).fetchone()
                if row:
                    event = json.loads(row[0])
                    conn.execute("INSERT INTO rejected (event_id, kind, ts, reason, at) VALUES (?, ?, ?, ?, ?)",
                                 (event.get("event_id"), event.get("kind"), event.get("ts"),
                                  str(reason)[:200], time.time()))
                    conn.execute("DELETE FROM queue WHERE seq=?", (seq,))
                conn.execute("DELETE FROM rejected WHERE seq NOT IN "
                             "(SELECT seq FROM rejected ORDER BY seq DESC LIMIT ?)", (MAX_REJECTED_KEPT,))

    def rejected_count(self):
        with closing(self._connect()) as conn:
            return conn.execute("SELECT COUNT(*) FROM rejected").fetchone()[0]

    def dropped_count(self):
        return int(self.get_meta("dropped", 0))

    # --- 後台連線狀態（代理寫、辨識程式讀；兩個行程共用這個檔）---
    def set_link_state(self, online, now=time.time):
        self.set_meta("link_state", {"online": bool(online), "ts": now()})

    def link_online(self, max_age=30.0, now=time.time):
        """代理最近有回報後台在線才算在線；代理沒在跑（狀態太舊）一律當作離線。"""
        state = self.get_meta("link_state")
        try:
            return bool(state["online"]) and now() - float(state["ts"]) <= max_age
        except (TypeError, KeyError, ValueError):
            return False


class Outbox:
    """事件送出口：用校正後的時鐘組事件並寫進佇列（代理與辨識程式共用，辨識程式不必知道後台在不在線）。"""

    def __init__(self, store, now=datetime.datetime.now):
        self.store, self._now = store, now

    def clock_offset(self):
        return float(self.store.get_meta("clock_offset", 0.0))

    def now(self):
        return self._now() + datetime.timedelta(seconds=self.clock_offset())

    def submit_event(self, kind, user_id=None, detail="", evidence=None):
        event = lp.build_event(kind, self.now(), user_id=user_id, detail=detail, evidence=evidence)
        self.store.enqueue(event)
        return event["event_id"]

    def submit_punch(self, user_id):
        return self.submit_event("punch", user_id=user_id)


class FaceIndex:
    """記憶體裡的人臉矩陣；recognize 與後台資料層的 recognize_face 結果一致。"""

    def __init__(self, faces):
        self.faces = list(faces)
        dims = {f["embedding"].shape for f in self.faces}
        self._matrix = np.stack([f["embedding"] for f in self.faces]) if self.faces and len(dims) == 1 else None

    def __len__(self):
        return len(self.faces)

    def recognize(self, target, threshold=1.0):
        """回傳 (姓名, 距離, 員工 id)。沒有資料：("Empty DB", 99.9, None)；距離不小於門檻：("Unknown", 距離, None)。"""
        if self._matrix is None or getattr(target, "shape", None) != self._matrix.shape[1:]:
            return ("Empty DB", 99.9, None)
        raw = np.linalg.norm(self._matrix - np.asarray(target, dtype=np.float32), axis=1)
        distances = [round(float(d), 2) for d in raw]       # 和後台一樣用 Python 的四捨五入
        best = min(range(len(distances)), key=lambda i: (distances[i], i))   # 距離相同取最先出現者
        dist = distances[best]
        if dist < threshold:
            return (self.faces[best]["name"], dist, self.faces[best]["id"])
        return ("Unknown", dist, None)

"""
tests/conftest.py：pytest 共用設定與 fixture。

主要功能：
  - 在匯入任何專案模組之前，把執行期資料夾與法規資料夾導向臨時資料夾，測試不會讀寫真實的 runtime/ 與 data/
  - db：臨時資料庫並已初始化；add_user、add_attendance：快速塞測試資料
  - clock：固定 database_mgr 使用的目前時間
  - rag_db：把 RAG 的 SQL 引擎也指向臨時資料庫
  - fake_llm_server：本機假的雲端 API 與 Ollama 伺服器，不需要網路與金鑰
"""
import datetime as real_datetime
import json
import os
import sqlite3
import tempfile
import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
import pytest

_TEST_ROOT = tempfile.mkdtemp(prefix="smart_access_tests_")
os.environ["SMART_ACCESS_RUNTIME_DIR"] = os.path.join(_TEST_ROOT, "runtime")
os.environ["SMART_ACCESS_LAWS_DIR"] = os.path.join(_TEST_ROOT, "laws")
os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")
os.environ.setdefault("HF_HUB_OFFLINE", "1")


@pytest.fixture
def db(tmp_path, monkeypatch):
    """臨時資料庫（已建立所有資料表），並讓 database_mgr 使用它。"""
    from shared import database_mgr

    path = str(tmp_path / "test.db")
    monkeypatch.setattr(database_mgr, "DB_PATH", path)
    database_mgr.init_db()
    return path


@pytest.fixture
def add_user(db):
    """回傳函式：新增一位員工（預設特徵為全 1 的 4 維向量）。"""

    def _add(name, emp_id, embedding=None, active=1, gender="M", role="employee"):
        emb = np.ones(4, dtype=np.float32) if embedding is None else embedding
        con = sqlite3.connect(db)
        con.execute(
            "INSERT INTO Users (name, gender, employee_id, embedding, role, is_active) VALUES (?, ?, ?, ?, ?, ?)",
            (name, gender, emp_id, emb, role, active),
        )
        con.commit()
        con.close()

    return _add


@pytest.fixture
def add_attendance(db):
    """回傳函式：新增一筆打卡紀錄。"""

    def _add(name, timestamp, status):
        con = sqlite3.connect(db)
        con.execute("INSERT INTO Attendance (name, timestamp, status) VALUES (?, ?, ?)", (name, timestamp, status))
        con.commit()
        con.close()

    return _add


@pytest.fixture
def attendance_rows(db):
    """回傳函式：讀出某位員工的所有打卡紀錄 [(timestamp, status)]，依時間排序。"""

    def _rows(name):
        con = sqlite3.connect(db)
        rows = con.execute(
            "SELECT timestamp, status FROM Attendance WHERE name=? ORDER BY timestamp", (name,)
        ).fetchall()
        con.close()
        return rows

    return _rows


@pytest.fixture
def clock(monkeypatch):
    """回傳函式：固定 database_mgr 內 datetime.datetime.now() 的結果。"""
    from shared import database_mgr

    def _set(year, month, day, hour, minute, second=0):
        fixed = real_datetime.datetime(year, month, day, hour, minute, second)

        class _FakeDateTime(real_datetime.datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed

        monkeypatch.setattr(database_mgr, "datetime", types.SimpleNamespace(datetime=_FakeDateTime))

    return _set


@pytest.fixture
def rag_db(db, monkeypatch):
    """讓 RAG 的 SQL 引擎也使用臨時資料庫，並清掉員工姓名快取。"""
    from rag import rag_sql_engine

    monkeypatch.setattr(rag_sql_engine, "ACCESS_SYSTEM_DB_PATH", db)
    rag_sql_engine._ensure_safe_employee_roster_view(db)
    monkeypatch.setitem(rag_sql_engine._employee_names_cache, "names", None)
    monkeypatch.setitem(rag_sql_engine._employee_names_cache, "at", 0.0)
    return db


class _FakeLLMHandler(BaseHTTPRequestHandler):
    """假的雲端 API（OpenAI 相容）與 Ollama：金鑰 good-key、模型 qwen2:7b 可用。"""

    def log_message(self, *args):
        pass

    def _send_json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/tags":
            self._send_json(200, {"models": [{"name": "qwen2:7b"}, {"name": "llama3:latest"}]})
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        self.server.requests.append((self.path, body))
        if self.path.endswith("/chat/completions"):
            if self.headers.get("Authorization") != "Bearer good-key":
                self._send_json(401, {"error": "invalid api key"})
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for piece in ("你", "好", "嗎"):
                chunk = {"choices": [{"delta": {"content": piece}}]}
                self.wfile.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8"))
            self.wfile.write(b"data: [DONE]\n\n")
        elif self.path == "/api/chat":
            if body.get("model") == "missing":
                self._send_json(404, {"error": "model 'missing' not found"})
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.end_headers()
            for piece, done in (("OK", False), ("!", False), ("", True)):
                line = {"message": {"content": piece}, "done": done}
                self.wfile.write((json.dumps(line, ensure_ascii=False) + "\n").encode("utf-8"))
        elif self.path == "/api/generate":
            self._send_json(200, {"done": True})
        else:
            self._send_json(404, {"error": "not found"})


@pytest.fixture(scope="module")
def fake_llm_server():
    """啟動本機假伺服器，回傳 (網址, 收到的請求清單)。"""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeLLMHandler)
    server.requests = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    old_no_proxy = os.environ.get("NO_PROXY")
    os.environ["NO_PROXY"] = "127.0.0.1"
    yield f"http://127.0.0.1:{server.server_address[1]}", server.requests
    server.shutdown()
    server.server_close()
    if old_no_proxy is None:
        os.environ.pop("NO_PROXY", None)
    else:
        os.environ["NO_PROXY"] = old_no_proxy

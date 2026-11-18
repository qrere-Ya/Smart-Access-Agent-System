"""
前台 Smart_Chat（一般對話）的「真行程」整合測試：後台連線服務是獨立行程（python -m admin.link_server，真的 HTTPS、
自簽憑證、本機 Ollama 語言模型），前台那一側是這個測試行程裡真的 FrontTalk 與 BackClient，兩邊只靠網路溝通。
需要 Ollama 正在執行；缺模型或沒有 Ollama 就跳過。
驗證：預設未開放、開放後能收到回答、回答沒有來源文件、對話紀錄來源是 front_talk、後台不載入知識庫模型、
Smart_RAG 開關不影響 Smart_Chat（兩者獨立）、問員工出缺勤拿不到任何資料庫內容、關掉開關立即停止。
"""
import json
import os
import socket
import sqlite3
import subprocess
import sys
import threading
import time

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("require_models")]


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def back(tmp_path_factory):
    from shared import front_control, paths, role_config

    runtime = tmp_path_factory.mktemp("back_runtime")
    port, token = _free_port(), role_config.new_token()
    (runtime / "role.json").write_text(json.dumps({"role": "back", "bind_host": "127.0.0.1", "back_port": port}),
                                       encoding="utf-8")
    (runtime / "llm_settings.json").write_text(json.dumps({"mode": "local"}), encoding="utf-8")
    env = {**os.environ, "SMART_ACCESS_RUNTIME_DIR": str(runtime), role_config.TOKEN_ENV: token,
           "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1"}
    env.pop(role_config.ROLE_ENV, None)
    proc = subprocess.Popen([sys.executable, "-m", "admin.link_server"], cwd=paths.PROJECT_ROOT, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                            errors="replace")
    lines, ready, fingerprint = [], threading.Event(), {}

    def read():
        for line in proc.stdout:
            lines.append(line.rstrip())
            if "憑證指紋（SHA-256）：" in line:
                fingerprint["value"] = line.split("：")[-1].strip()
            if "前台自動尋找" in line or "無法開啟自動尋找" in line:
                ready.set()

    threading.Thread(target=read, daemon=True).start()
    if not ready.wait(60) or "value" not in fingerprint:
        proc.kill()
        pytest.fail("後台連線服務沒有啟動：\n" + "\n".join(lines[-15:]))
    patch = pytest.MonkeyPatch()
    patch.setattr(paths, "FRONT_CONTROL_PATH", str(runtime / "front_control.json"))
    patch.setenv(role_config.TOKEN_ENV, token)
    yield {"runtime": runtime, "port": port, "token": token, "fingerprint": fingerprint["value"], "lines": lines,
           "front_control": front_control}
    proc.terminate()
    try:
        proc.wait(15)
    except subprocess.TimeoutExpired:
        proc.kill()
    patch.undo()


@pytest.fixture(scope="module")
def talk(back, tmp_path_factory):
    from access import front_agent
    from access.front_chat import FrontTalk
    from access.front_store import FrontStore

    store = FrontStore(str(tmp_path_factory.mktemp("front") / "front.db"))
    store.set_meta(front_agent.BACK_TARGET_KEY,
                   {"host": "127.0.0.1", "port": back["port"], "fingerprint": back["fingerprint"]})
    store.set_meta(front_agent.BACK_TALK_KEY, True)
    stop = threading.Event()

    def keep_link_fresh():
        while not stop.is_set():
            store.set_link_state(True)
            stop.wait(3)

    threading.Thread(target=keep_link_fresh, daemon=True).start()
    yield FrontTalk(store)
    stop.set()


def wait_for(chat, states, seconds=300):
    end = time.time() + seconds
    while time.time() < end:
        view = chat.view()
        if view["state"] in states:
            return view
        time.sleep(0.2)
    raise AssertionError(f"逾時，狀態：{chat.view()}")


def ask(chat, question):
    time.sleep(3.2)                                           # 過了兩題之間的最短間隔
    chat.clear()
    assert chat.submit(question) is True
    return wait_for(chat, ("answer", "error", "closed"))


def test_1_it_is_closed_by_default_on_the_back(back, talk):
    assert back["front_control"].talk_enabled() is False
    view = ask(talk, "你好")
    assert view["state"] == "closed" and view["can_ask"] is False


def test_2_after_the_admin_opens_it_the_front_gets_an_answer_without_sources(back, talk):
    back["front_control"].set_talk_enabled(True)
    view = ask(talk, "請用一句話說明什麼是門禁系統。")
    assert view["state"] == "answer" and len(view["answer"]) >= 5, view
    assert view["sources"] == []


def test_3_the_exchange_is_logged_as_front_talk_and_no_rag_models_were_loaded(back):
    db = os.path.join(str(back["runtime"]), "database.db")
    rows, deadline = [], time.time() + 10
    while time.time() < deadline and not rows:
        con = sqlite3.connect(db)
        rows = con.execute("SELECT source, answer, citations FROM ChatLogs").fetchall()
        con.close()
        time.sleep(0.3)
    assert rows and all(r[0] == "front_talk" for r in rows) and rows[0][1]
    assert "bge-m3" not in "\n".join(back["lines"])                  # 一般對話不載入知識庫模型


def test_4_the_rag_switch_does_not_affect_smart_chat(back, talk):
    assert back["front_control"].chat_enabled() is False           # Smart_RAG 仍關閉
    view = ask(talk, "用一句話說明什麼是防火牆。")
    assert view["state"] == "answer", view


def test_5_an_attendance_question_gets_nothing_from_the_back_database(back, talk):
    db = os.path.join(str(back["runtime"]), "database.db")
    con = sqlite3.connect(db)
    con.execute("INSERT INTO Users (name, gender, employee_id, embedding) VALUES ('王小明', 'M', 'E001', x'00')")
    con.execute("INSERT INTO Attendance (name, timestamp, status) VALUES ('王小明', '2026-11-10 08:47:13', '遲到')")
    con.commit()
    con.close()
    view = ask(talk, "王小明這個月遲到幾次？列出每次的時間")
    assert view["state"] == "answer", view
    for secret in ("08:47", "2026-11-10", "E001"):
        assert secret not in view["answer"], (secret, view["answer"])


def test_6_closing_the_switch_on_the_back_stops_answers_at_once(back, talk):
    back["front_control"].set_talk_enabled(False)
    assert ask(talk, "你好")["state"] == "closed"

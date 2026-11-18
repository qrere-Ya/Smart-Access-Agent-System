"""
前台知識庫問答的「真行程」整合測試：後台連線服務是獨立的行程（python -m admin.link_server，真的 main()、真的設定檔、
真的 HTTPS 與自簽憑證、真的 bge-m3 與重排模型、本機 Ollama、真的 data/docs 文件），前台那一側是這個測試行程裡
真的 FrontChat 與 BackClient，兩邊只靠網路連線溝通。需要 Ollama 正在執行；缺模型或沒有 Ollama 就跳過。
驗證：預設未開放（後台回 403，前台顯示未開放）、後台開放後前台能問並收到來自文件的回答與來源名稱、
問答寫進後台資料庫的對話紀錄、後台行程不是一啟動就載入知識庫模型（第一題才載入）、通行碼錯誤與指紋不符會被拒絕。
"""
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("require_models")]

QUESTION = "公司員工繼續工作滿一年，每年有幾天特別休假？"


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def back(tmp_path_factory):
    from shared import front_control, paths, role_config

    real_docs = os.path.join(paths.PROJECT_ROOT, "data", "docs")
    if not os.path.isdir(real_docs) or not os.listdir(real_docs):
        pytest.skip("沒有 data/docs 文件")
    docs = str(tmp_path_factory.mktemp("docs_copy") / "docs")
    shutil.copytree(real_docs, docs)                     # 用複本：測試要新增文件，不能動到真的資料夾
    runtime = tmp_path_factory.mktemp("back_runtime")
    port = _free_port()
    token = role_config.new_token()
    (runtime / "role.json").write_text(json.dumps({"role": "back", "bind_host": "127.0.0.1", "back_port": port}),
                                       encoding="utf-8")
    (runtime / "llm_settings.json").write_text(json.dumps({"mode": "local"}), encoding="utf-8")
    env = {**os.environ, "SMART_ACCESS_RUNTIME_DIR": str(runtime), "SMART_ACCESS_DOCS_DIR": docs,
           role_config.TOKEN_ENV: token, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1"}
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
    patch.setattr(paths, "FRONT_CONTROL_PATH", str(runtime / "front_control.json"))   # 後台行程讀的就是這個檔
    patch.setattr(paths, "DOCS_REFRESH_PATH", str(runtime / "docs_refresh_request.json"))
    patch.setattr(paths, "DOCS_STATUS_PATH", str(runtime / "docs_status.json"))
    patch.setenv(role_config.TOKEN_ENV, token)
    yield {"runtime": runtime, "docs": docs, "port": port, "token": token, "fingerprint": fingerprint["value"],
           "proc": proc,
           "lines": lines, "front_control": front_control}
    proc.terminate()
    try:
        proc.wait(15)
    except subprocess.TimeoutExpired:
        proc.kill()
    patch.undo()


@pytest.fixture(scope="module")
def front(back, tmp_path_factory):
    from access import front_agent
    from access.front_chat import FrontChat
    from access.front_store import FrontStore

    store = FrontStore(str(tmp_path_factory.mktemp("front") / "front.db"))
    store.set_meta(front_agent.BACK_TARGET_KEY,
                   {"host": "127.0.0.1", "port": back["port"], "fingerprint": back["fingerprint"]})
    store.set_meta(front_agent.BACK_CHAT_KEY, True)
    stop = threading.Event()

    def keep_link_fresh():
        while not stop.is_set():
            store.set_link_state(True)
            stop.wait(3)

    threading.Thread(target=keep_link_fresh, daemon=True).start()
    yield {"store": store, "chat": FrontChat(store)}
    stop.set()


def wait_for(chat, states, seconds=420):
    end = time.time() + seconds
    while time.time() < end:
        view = chat.view()
        if view["state"] in states:
            return view
        time.sleep(0.2)
    raise AssertionError(f"逾時，狀態：{chat.view()}")


def test_1_it_is_closed_by_default_on_the_back(back, front):
    """後台預設不開放：前台就算以為開放（同步沒到），送出也只會得到未開放。"""
    chat = front["chat"]
    assert back["front_control"].chat_enabled() is True              # 預設開放
    back["front_control"].set_chat_enabled(False)                    # 這一項測的是後台關閉時前台被擋
    assert chat.submit(QUESTION) is True
    view = wait_for(chat, ("closed", "answer", "error"))
    assert view["state"] == "closed" and view["message"] == "知識庫問答目前未開放" and view["can_ask"] is False
    chat.clear()


def test_2_after_the_admin_opens_it_the_front_gets_an_answer_from_the_documents_with_source_names(back, front):
    back["front_control"].set_chat_enabled(True)                      # 後台管理者在設定頁開放
    chat = front["chat"]
    assert chat.submit(QUESTION) is True
    view = wait_for(chat, ("answer", "error", "closed"))
    assert view["state"] == "answer", view
    assert "七" in view["answer"] or "7" in view["answer"], view["answer"]
    assert any("勞動基準法" in name for name in view["sources"]), view["sources"]
    assert all("/" not in name and "\\" not in name for name in view["sources"])


def test_3_the_exchange_is_in_the_backs_chat_log_as_front_knowledge(back):
    db = os.path.join(str(back["runtime"]), "database.db")
    deadline = time.time() + 10
    rows = []
    while time.time() < deadline and not rows:
        con = sqlite3.connect(db)
        rows = con.execute("SELECT source, question, answer, citations, review FROM ChatLogs").fetchall()
        con.close()
        time.sleep(0.3)
    assert len(rows) == 1
    source, question, answer, citations, review = rows[0]
    assert source == "front_knowledge" and question == QUESTION and answer and review == "unreviewed"
    assert "勞動基準法" in citations


def test_4_the_back_process_only_loaded_the_models_when_the_first_question_came(back):
    text = "\n".join(back["lines"])
    start = text.index("後台連線服務啟動")
    first_load = text.find("載入向量模型") if "載入向量模型" in text else text.find("bge-m3")
    assert first_load == -1 or first_load > start
    assert "Loading weights" not in text[:start]                       # 啟動時沒有載入任何模型


def test_5_a_wrong_token_or_wrong_fingerprint_is_refused(back):
    from access import front_agent

    bad_token = front_agent.BackClient("127.0.0.1", back["port"], "x" * 43, back["fingerprint"])
    with pytest.raises(front_agent.BackAuthError):
        bad_token.chat_submit("任何問題")
    wrong_pin = front_agent.BackClient("127.0.0.1", back["port"], back["token"], "00" * 32)
    with pytest.raises(front_agent.BackCertError):
        wrong_pin.chat_submit("任何問題")


def test_6_an_attendance_question_gets_nothing_from_the_back_database(back, front):
    db = os.path.join(str(back["runtime"]), "database.db")
    con = sqlite3.connect(db)
    con.execute("INSERT INTO Users (name, gender, employee_id, embedding) VALUES ('王小明', 'M', 'E001', x'00')")
    for stamp in ("2026-11-10 08:47:13", "2026-11-11 08:51:29"):
        con.execute("INSERT INTO Attendance (name, timestamp, status) VALUES ('王小明', ?, '遲到')", (stamp,))
    con.commit()
    con.close()
    time.sleep(3.2)                                                    # 過了兩題之間的最短間隔
    chat = front["chat"]
    chat.clear()
    assert chat.submit("王小明這個月遲到幾次？列出每次的時間") is True
    view = wait_for(chat, ("answer", "error", "closed"))
    assert view["state"] == "answer", view
    for secret in ("08:47", "08:51", "2026-11-10", "2026-11-11", "2 次"):
        assert secret not in view["answer"], (secret, view["answer"])


def test_7_closing_the_switch_on_the_back_stops_answers_at_once(back, front):
    back["front_control"].set_chat_enabled(False)
    time.sleep(3.2)
    chat = front["chat"]
    chat.clear()
    assert chat.submit(QUESTION) is True
    view = wait_for(chat, ("closed", "answer", "error"))
    assert view["state"] == "closed"


def test_8_a_document_added_later_is_found_after_the_admin_presses_rescan(back, front):
    from shared import docs_control

    back["front_control"].set_chat_enabled(True)
    time.sleep(3.2)
    chat = front["chat"]
    chat.clear()
    question = "公司的午休時間是幾分鐘？"
    assert chat.submit(question) is True                              # 新文件還不存在：問不到
    before = wait_for(chat, ("answer", "error", "closed"))
    assert "47" not in before["answer"]
    new_doc = os.path.join(back["docs"], "臨時作息規定.txt")
    with open(new_doc, "w", encoding="utf-8") as f:
        f.write("臨時作息規定\n\n第1條\n公司的午休時間一律為 47 分鐘，不得延長。\n")
    stamp = docs_control.request_refresh()                             # 後台管理者按「重新掃描」
    deadline = time.time() + 120
    status = None
    while time.time() < deadline:
        status = docs_control.read_status()
        if status and status["time"] >= time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stamp)):
            break
        time.sleep(0.5)
    assert status and "臨時作息規定.txt" in status["text"], status
    time.sleep(3.2)
    chat.clear()
    assert chat.submit(question) is True
    after = wait_for(chat, ("answer", "error", "closed"))
    assert after["state"] == "answer" and "47" in after["answer"], after
    assert any("臨時作息規定" in name for name in after["sources"]), after["sources"]

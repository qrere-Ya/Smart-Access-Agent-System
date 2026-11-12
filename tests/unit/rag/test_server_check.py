"""server_check：換機就緒檢查的單元測試（用臨時資料夾，不碰真實資料與模型）。"""
import os
import sqlite3

from rag import remote_access, server_check


def _touch(folder, *names):
    os.makedirs(folder, exist_ok=True)
    for n in names:
        with open(os.path.join(folder, n), "w") as f:
            f.write("x")


def levels(results):
    return {item: level for level, item, _ in results}


def test_models_missing_is_failure_and_complete_is_ok(tmp_path):
    assert levels(server_check.check_models(str(tmp_path)))["向量模型 bge-m3"] == server_check.FAIL
    _touch(tmp_path / "bge-m3", *server_check.EMBED_FILES, "pytorch_model.bin")
    _touch(tmp_path / "bge-reranker-v2-m3", *server_check.RERANK_FILES)
    got = levels(server_check.check_models(str(tmp_path)))
    assert got["向量模型 bge-m3"] == server_check.OK and got["重排模型"] == server_check.OK
    assert got["向量模型 fp16 版"] == server_check.WARN      # 沒有 fp16 只是提醒


def test_embed_weights_missing_is_reported(tmp_path):
    _touch(tmp_path / "bge-m3", *server_check.EMBED_FILES)
    note = server_check.check_models(str(tmp_path))[0][2]
    assert "pytorch_model.bin" in note


def test_docs_empty_fails_and_counts(tmp_path):
    assert server_check.check_docs(str(tmp_path))[0][0] == server_check.FAIL
    _touch(tmp_path / "sub", "a.pdf", "b.txt", "c.png")
    assert server_check.check_docs(str(tmp_path))[0][2] == "2 份"


def test_database_missing_corrupt_and_good(tmp_path):
    assert server_check.check_database(str(tmp_path / "no.db"))[0][0] == server_check.FAIL
    bad = tmp_path / "bad.db"
    bad.write_text("not a database")
    assert server_check.check_database(str(bad))[0][0] == server_check.FAIL
    good = tmp_path / "good.db"
    conn = sqlite3.connect(good)
    conn.execute("CREATE TABLE Users (id INTEGER)")
    conn.execute("CREATE TABLE Attendance (timestamp TEXT)")
    conn.execute("INSERT INTO Users VALUES (1)")
    conn.execute("INSERT INTO Attendance VALUES ('2026-11-05 08:00:00')")
    conn.commit()
    conn.close()
    level, _, note = server_check.check_database(str(good))[0]
    assert level == server_check.OK and "2026-11-05" in note


def test_keys_none_expired_and_valid_never_show_the_key(tmp_path):
    path = str(tmp_path / "keys.json")
    assert server_check.check_keys(path)[0][0] == server_check.FAIL
    store = remote_access.KeyStore(path)
    secret = store.add("甲", days=1, now=1.0)       # 1970 年就到期
    assert server_check.check_keys(path)[0][0] == server_check.FAIL
    secret2 = store.add("乙", days=None)
    level, _, note = server_check.check_keys(path)[0]
    assert level == server_check.OK and "1 組有效／共 2 組" in note and "1 組已過期" in note
    assert "甲" not in note and "乙" not in note                        # 只報數量，不印組員名稱
    assert secret not in note and secret2 not in note


def test_api_key_missing_warns_and_never_prints_value(monkeypatch):
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    from rag import llm_settings
    monkeypatch.setattr(llm_settings, "load_settings",
                        lambda: {"api_base": "https://integrate.api.nvidia.com/v1", "api_key": ""})
    assert server_check.check_api_key()[0][0] == server_check.WARN
    monkeypatch.setenv("NVIDIA_API_KEY", "test-api-key")
    results = server_check.check_api_key()
    assert results[0][0] == server_check.OK
    assert all("test-api-key" not in note for _, _, note in results)


def test_main_exit_code_follows_failures(monkeypatch, capsys):
    monkeypatch.setattr(server_check, "run_all", lambda port, api: [(server_check.OK, "甲", "好")])
    assert server_check.main([]) == 0
    monkeypatch.setattr(server_check, "run_all",
                        lambda port, api: [(server_check.FAIL, "乙", "壞"), (server_check.WARN, "丙", "提醒")])
    assert server_check.main([]) == 1
    assert "失敗 1 項、注意 1 項" in capsys.readouterr().out


def test_api_failure_kind_is_limited_to_a_fixed_list_and_never_echoes_error_text():
    class Err(Exception):
        kind = "timeout"

    class Weird(Exception):
        kind = "sk-secret-token-in-the-message"

    assert server_check._api_failure_kind(Err()) == "timeout"
    assert server_check._api_failure_kind(Weird("boom")) == "unknown"
    assert server_check._api_failure_kind(ValueError("sk-abc")) == "unknown"

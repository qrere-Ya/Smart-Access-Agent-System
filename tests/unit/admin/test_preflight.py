"""preflight 單元測試：每項檢查的通過、注意、失敗分支與結論文字；用假的硬體與資料，不需要鏡頭、顯卡或模型。"""
import sqlite3

import numpy as np

from admin import preflight as pf


def _make_models(tmp_path):
    for _, path in pf.required_models(str(tmp_path)):
        import os
        os.makedirs(os.path.dirname(path), exist_ok=True)
        (os.makedirs(path, exist_ok=True) if "." not in os.path.basename(path) else open(path, "w").close())


def test_models_all_present_and_missing_are_named(tmp_path):
    assert pf.check_models(str(tmp_path)).status == pf.FAIL
    _make_models(tmp_path)
    assert pf.check_models(str(tmp_path)).status == pf.OK
    (tmp_path / "yolov8s.pt").unlink()
    r = pf.check_models(str(tmp_path))
    assert r.status == pf.FAIL and "YOLO" in r.detail and r.fix


def test_docs_must_have_files(tmp_path):
    assert pf.check_docs(str(tmp_path / "none")).status == pf.FAIL
    assert pf.check_docs(str(tmp_path)).status == pf.FAIL
    (tmp_path / "a.pdf").write_bytes(b"x")
    assert pf.check_docs(str(tmp_path)).status == pf.OK


def test_database_needs_users_and_warns_without_admin(db, add_user):
    assert pf.check_database(db).status == pf.FAIL                    # 沒有員工
    add_user("甲", "1", embedding=np.ones(4, dtype=np.float32))
    assert pf.check_database(db).status == pf.WARN                    # 有員工、沒管理者
    con = sqlite3.connect(db)
    con.execute("UPDATE Users SET role='admin'")
    con.commit()
    con.close()
    assert pf.check_database(db).status == pf.OK
    assert pf.check_database(db + ".missing").status == pf.FAIL


def test_gpu_and_ram_levels():
    assert pf.check_gpu(lambda: None).status == pf.WARN
    assert pf.check_gpu(lambda: 500).status == pf.WARN
    assert pf.check_gpu(lambda: 6000).status == pf.OK
    assert pf.check_ram(lambda: 100).status == pf.WARN
    assert pf.check_ram(lambda: 9000).status == pf.OK


def test_camera_ok_busy_and_error():
    assert pf.check_camera(lambda: True).status == pf.OK
    assert pf.check_camera(lambda: False).status == pf.FAIL

    def boom():
        raise RuntimeError("沒有驅動")
    r = pf.check_camera(boom)
    assert r.status == pf.FAIL and "沒有驅動" in r.detail


def test_ports_and_network():
    assert pf.check_ports(lambda host, port: True).status == pf.WARN
    assert pf.check_ports(lambda host, port: False).status == pf.OK
    assert pf.check_network(lambda: None).status == pf.WARN
    assert pf.check_network(lambda: "127.0.0.1").status == pf.WARN
    assert pf.check_network(lambda: "192.168.1.5").status == pf.OK


def test_llm_modes(monkeypatch):
    local = {"mode": "local", "local_model": "qwen3:8b", "local_host": ""}
    assert pf.check_llm(local, lambda s: (True, ["qwen3:8b"])).status == pf.OK
    assert pf.check_llm(local, lambda s: (True, ["other:1b"])).status == pf.FAIL
    assert pf.check_llm(local, lambda s: (False, None), which=lambda n: None).status == pf.FAIL
    assert pf.check_llm(local, lambda s: (False, None), which=lambda n: "ollama.exe").status == pf.OK
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    assert pf.check_llm({"mode": "api", "api_key": "", "api_model": "m"}).status == pf.FAIL
    assert pf.check_llm({"mode": "api", "api_key": "k", "api_model": "m"}).status == pf.OK


def test_summary_text_and_exit_code():
    ok = [pf.Check("甲", pf.OK, "好")]
    text, code = pf.summarize(ok)
    assert code == 0 and "全部通過" in text
    text, code = pf.summarize(ok + [pf.Check("乙", pf.WARN, "小問題", "做這個")])
    assert code == 0 and "要留意" in text and "做這個" in text
    text, code = pf.summarize(ok + [pf.Check("丙", pf.FAIL, "壞了", "修它")])
    assert code == 1 and "不能展示" in text and "修它" in text


def test_run_all_survives_a_crashing_check(monkeypatch):
    fine = pf.Check("x", pf.OK)
    for name in ("check_docs", "check_database", "check_gpu", "check_ram", "check_camera", "check_ports",
                 "check_llm", "check_network"):
        monkeypatch.setattr(pf, name, lambda *a, _f=fine, **k: _f)

    def boom():
        raise RuntimeError("爆了")
    monkeypatch.setattr(pf, "check_models", boom)
    results = pf.run_all()
    assert len(results) == 9
    assert results[0].status == pf.FAIL and "爆了" in results[0].detail

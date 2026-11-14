"""preflight 單元測試：每項檢查的通過、注意、失敗分支與結論文字；用假的硬體與資料，不需要鏡頭、顯卡或模型。"""
import datetime
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
    monkeypatch.setattr(pf, "check_front", lambda *a, **k: [])          # 前台區段另外測

    def boom():
        raise RuntimeError("爆了")
    monkeypatch.setattr(pf, "check_models", boom)
    results = pf.run_all()
    assert len(results) == 9
    assert results[0].status == pf.FAIL and "爆了" in results[0].detail


# --- 前台（雙機展示）區段 ---

TODAY = datetime.date(2026, 11, 13)
NET_OK = {"網路": [("OK", "網路線位址：169.254.1.1（乙太網路）")], "連線服務": [("OK", "連線服務（TCP 8770）執行中")],
          "防火牆": [("OK", "防火牆規則齊全")]}


def online(**extra):
    status = {"camera_ok": True, "recognition_running": True, "faces": 8, "pending_events": 0}
    status.update(extra)
    return {"seen": True, "online": True, "age": 3.0, "status": status, "remote_addr": "169.254.2.2"}


OFFLINE = {"seen": True, "online": False, "age": 9000.0, "status": {}, "remote_addr": ""}
NEVER = {"seen": False, "online": False, "age": None, "status": {}, "remote_addr": ""}
BRIEF = {"date": "2026-11-13", "news": [{"title": "a"}, {"title": "b"}], "news_problem": ""}


def back(monkeypatch, token=True):
    from shared import role_config
    settings = role_config.LinkSettings(role_config.BACK, bind_host="auto")
    monkeypatch.setattr(role_config, "load", lambda path=None: settings)
    monkeypatch.setattr(role_config, "link_token", lambda: "t" * 50 if token else "")


def front_checks(monkeypatch, **kw):
    back(monkeypatch, kw.pop("token", True))
    args = {"network_results": NET_OK, "state": online(), "enabled": True, "users": 8, "brief": BRIEF, "today": TODAY,
            "local_only": False}
    args.update(kw)
    return {c.name: c for c in pf.check_front(**args)}


def test_front_section_is_skipped_for_standalone_and_front_roles(monkeypatch):
    from shared import role_config
    for role in (role_config.STANDALONE, role_config.FRONT):
        monkeypatch.setattr(role_config, "load", lambda path=None, _r=role: role_config.LinkSettings(_r))
        results = pf.check_front()
        assert len(results) == 1 and results[0].status == pf.OK and "略過" in results[0].detail


def test_everything_ready_gives_no_front_failures_or_warnings(monkeypatch):
    checks = front_checks(monkeypatch)
    bad = {n: c.status for n, c in checks.items() if c.status != pf.OK}
    assert not bad, bad
    assert pf.FRONT + "連線" in checks and pf.FRONT + "人臉資料" in checks and pf.FRONT + "今日晨報" in checks


def test_front_offline_or_never_seen_fails_and_says_what_to_do(monkeypatch):
    for state in (OFFLINE, NEVER):
        checks = front_checks(monkeypatch, state=state)
        item = checks[pf.FRONT + "連線"]
        assert item.status == pf.FAIL and "啟動前台門禁" in item.fix
        assert pf.FRONT + "鏡頭" not in checks and pf.FRONT + "人臉資料" not in checks     # 離線時不重複報細項
    assert "9000" in front_checks(monkeypatch, state=OFFLINE)[pf.FRONT + "連線"].detail


def test_recognition_not_running_warns_and_points_at_the_start_button(monkeypatch):
    checks = front_checks(monkeypatch, state=online(recognition_running=False, camera_ok=False))
    item = checks[pf.FRONT + "人臉辨識"]
    assert item.status == pf.WARN and "啟動人臉辨識" in item.fix
    assert pf.FRONT + "鏡頭" not in checks                                  # 辨識沒在跑就沒有鏡頭可看


def test_recognition_stopped_by_the_back_explains_how_to_resume(monkeypatch):
    item = front_checks(monkeypatch, enabled=False)[pf.FRONT + "人臉辨識"]
    assert item.status == pf.WARN and "恢復前台辨識" in item.fix
    assert front_checks(monkeypatch, enabled=False, state=OFFLINE)[pf.FRONT + "人臉辨識"].status == pf.WARN


def test_camera_failure_while_recognition_runs_is_a_failure(monkeypatch):
    item = front_checks(monkeypatch, state=online(camera_ok=False))[pf.FRONT + "鏡頭"]
    assert item.status == pf.FAIL and "terminal_app.log" in item.fix


def test_face_count_zero_mismatch_and_match(monkeypatch):
    zero_no_users = front_checks(monkeypatch, state=online(faces=0), users=0)[pf.FRONT + "人臉資料"]
    assert zero_no_users.status == pf.FAIL and "後台還沒有員工人臉" in zero_no_users.fix
    zero_with_users = front_checks(monkeypatch, state=online(faces=0), users=5)[pf.FRONT + "人臉資料"]
    assert zero_with_users.status == pf.FAIL and "front_agent.log" in zero_with_users.fix
    assert front_checks(monkeypatch, state=online(faces=3), users=5)[pf.FRONT + "人臉資料"].status == pf.WARN
    assert front_checks(monkeypatch, state=online(faces=5), users=5)[pf.FRONT + "人臉資料"].status == pf.OK
    unknown = front_checks(monkeypatch, state=online(faces=5), users=None)
    assert unknown[pf.FRONT + "人臉資料"].status in (pf.OK, pf.WARN)


def test_pending_events_warn(monkeypatch):
    assert front_checks(monkeypatch, state=online(pending_events=4))[pf.FRONT + "待補送事件"].status == pf.WARN
    assert front_checks(monkeypatch)[pf.FRONT + "待補送事件"].status == pf.OK


def test_morning_brief_missing_old_empty_and_ready(monkeypatch):
    missing = front_checks(monkeypatch, brief={})[pf.FRONT + "今日晨報"]
    assert missing.status == pf.WARN and "立即產生" in missing.fix
    old = front_checks(monkeypatch, brief={**BRIEF, "date": "2026-11-12"})[pf.FRONT + "今日晨報"]
    assert old.status == pf.WARN and "2026-11-12" in old.detail
    empty = front_checks(monkeypatch, brief={**BRIEF, "news": [], "news_problem": "沒有網路"})[pf.FRONT + "今日晨報"]
    assert empty.status == pf.WARN and "沒有網路" in empty.detail
    ready = front_checks(monkeypatch)[pf.FRONT + "今日晨報"]
    assert ready.status == pf.OK and "2 則" in ready.detail


def test_local_only_mode_warns_because_news_needs_the_network(monkeypatch):
    item = front_checks(monkeypatch, local_only=True)[pf.FRONT + "後台網路模式"]
    assert item.status == pf.WARN and "全本地" in item.fix
    assert front_checks(monkeypatch)[pf.FRONT + "後台網路模式"].status == pf.OK


def test_back_settings_failures_and_network_results_are_carried_over(monkeypatch):
    back(monkeypatch, token=False)
    no_token = [c for c in pf.check_front(network_results=NET_OK, state=online(), enabled=True, users=8, brief=BRIEF,
                                          today=TODAY, local_only=False) if c.name == pf.FRONT + "後台設定"]
    assert any(c.status == pf.FAIL and "通行碼" in c.detail for c in no_token)
    bad_net = {**NET_OK, "防火牆": [("FAIL", "缺防火牆規則「x」")]}
    checks = pf.check_front(network_results=bad_net, state=online(), enabled=True, users=8, brief=BRIEF, today=TODAY,
                            local_only=False)
    assert any(c.name == pf.FRONT + "防火牆" and c.status == pf.FAIL for c in checks)


def test_summary_mentions_front_and_back_only_when_the_front_was_checked():
    with_front = [pf.Check(pf.FRONT + "連線", pf.OK, "在線")]
    assert "前台＋後台" in pf.summarize(with_front)[0]
    assert "前台＋後台" in pf.summarize([pf.Check(pf.FRONT + "連線", pf.FAIL, "離線", "開前台")])[0]
    assert "前台＋後台" not in pf.summarize([pf.Check("甲", pf.OK, "好")])[0]


def test_count_active_users_reads_the_backend_database(db, add_user):
    import numpy as np
    assert pf.count_active_users(db) == 0
    add_user("甲", "1", embedding=np.ones(4, dtype=np.float32))
    add_user("乙", "2", embedding=np.ones(4, dtype=np.float32))
    assert pf.count_active_users(db) == 2
    assert pf.count_active_users(db + ".missing") is None

"""驗證當天流程（後台）：資料庫時間窗查詢、判定規則、停止辨識一定恢復、拔線補送、報告與敏感資訊過濾、claude 分析。"""
import datetime as dt
import os
import sqlite3
import subprocess

import pytest

from admin import preflight as pf
from admin import verify_day as vd
from shared.verify_common import HIDDEN, Console, QuitSession

T0 = dt.datetime(2026, 11, 13, 8, 30, 0)


def make_db(tmp_path):
    os.makedirs(tmp_path, exist_ok=True)
    path = str(tmp_path / "database.db")
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE Attendance (id INTEGER PRIMARY KEY, name TEXT, timestamp DATETIME, status TEXT)")
    con.execute("CREATE TABLE AnomalyEvents (id INTEGER PRIMARY KEY, timestamp DATETIME, event_type TEXT, "
                "detail TEXT, evidence_path TEXT, end_timestamp DATETIME, event_count INTEGER)")
    con.execute("CREATE TABLE ChatLogs (id INTEGER PRIMARY KEY, ts TEXT, source TEXT, question TEXT, answer TEXT, "
                "citations TEXT, review TEXT, reviewed_at TEXT, reviewer TEXT)")
    con.commit()
    con.close()
    return path


def add_qa(path, when, source):
    con = sqlite3.connect(path)
    con.execute("INSERT INTO ChatLogs (ts, source, question, answer) VALUES (?, ?, 'q', 'a')",
                (when.strftime(vd.STAMP), source))
    con.commit()
    con.close()


def add_punch(path, when, name="王小明", status="上班"):
    con = sqlite3.connect(path)
    con.execute("INSERT INTO Attendance (name, timestamp, status) VALUES (?, ?, ?)",
                (name, when.strftime(vd.STAMP), status))
    con.commit()
    con.close()


def add_event(path, when, kind, evidence="", end=None, count=1):
    con = sqlite3.connect(path)
    con.execute("INSERT INTO AnomalyEvents (timestamp, event_type, detail, evidence_path, end_timestamp, "
                "event_count) VALUES (?, ?, ?, ?, ?, ?)",
                (when.strftime(vd.STAMP), kind, "測試", evidence, end.strftime(vd.STAMP) if end else None, count))
    con.commit()
    con.close()


def scripted(answers):
    said, queue = [], list(answers)

    def reader(_prompt):
        if not queue:
            raise EOFError
        return queue.pop(0)

    return Console(reader, said.append), said


def make_ctx(tmp_path, answers=(), state=None, db=None, **kw):
    console, said = scripted(answers)
    session = vd.Session(str(tmp_path / "verification"), T0, secrets=kw.pop("secrets", ()))
    clock = {"t": 0.0}
    defaults = {"now": lambda: T0, "sleep": lambda s: clock.__setitem__("t", clock["t"] + s),
                "clock": lambda: clock["t"], "db_path": db, "load_front": lambda: state or OFFLINE,
                "set_recognition": lambda v: None, "anomaly_root": str(tmp_path / "anomaly"),
                "probe_root": str(tmp_path / "probe"), "run_preflight": lambda: []}
    defaults.update(kw)
    return vd.Context(console, session, **defaults), said


OFFLINE = {"seen": False, "online": False, "age": None, "status": {}, "remote_addr": ""}


def online(running=True, camera=True, **extra):
    return {"seen": True, "online": True, "age": 3.0, "remote_addr": "169.254.1.2",
            "status": {"recognition_running": running, "camera_ok": camera, "faces": 8, "pending_events": 0, **extra}}


# --- 資料庫查詢 ---

def test_anomalies_since_include_events_that_continued_into_the_window(tmp_path):
    db = make_db(tmp_path)
    add_event(db, T0 - dt.timedelta(minutes=10), "tailgate", end=T0 - dt.timedelta(minutes=9))     # 開始前就結束
    add_event(db, T0 - dt.timedelta(minutes=1), "tailgate", end=T0 + dt.timedelta(minutes=1), count=3)   # 跨過開始
    add_event(db, T0 + dt.timedelta(minutes=2), "spoof")
    events = vd.query_anomalies_since(db, T0)
    assert [e["kind"] for e in events] == ["tailgate", "spoof"] and events[0]["count"] == 3


def test_attendance_queries_use_the_time_window_and_today(tmp_path):
    db = make_db(tmp_path)
    add_punch(db, T0 - dt.timedelta(days=1))
    add_punch(db, T0 - dt.timedelta(minutes=5), "甲")
    add_punch(db, T0 + dt.timedelta(minutes=5), "乙")
    assert [r["name"] for r in vd.query_attendance_since(db, T0)] == ["乙"]
    assert [r["name"] for r in vd.query_attendance_today(db, T0.date())] == ["甲", "乙"]


def test_the_verification_never_writes_to_the_database(tmp_path):
    db = make_db(tmp_path)
    with pytest.raises(sqlite3.OperationalError):               # 唯讀連線不能寫
        with vd._connect_ro(db) as con:
            con.execute("INSERT INTO Attendance (name, timestamp, status) VALUES ('x', 'y', 'z')")


def test_copy_evidence_only_copies_files_inside_the_evidence_folder(tmp_path):
    root = tmp_path / "anomaly"
    root.mkdir()
    (root / "t.jpg").write_bytes(b"jpg")
    outside = tmp_path / "secret.jpg"
    outside.write_bytes(b"x")
    events = [{"id": 1, "evidence": "t.jpg"}, {"id": 2, "evidence": str(outside)}, {"id": 3, "evidence": ""}]
    copied = vd.copy_evidence(events, str(tmp_path / "out"), str(root))
    assert copied == ["事件1_t.jpg"] and os.path.exists(tmp_path / "out" / "事件1_t.jpg")


# --- 判定 ---

def test_judging_rules():
    tail = {"kind": "tailgate", "evidence": "a.jpg"}
    assert vd.judge_no_tailgate([])[0] == vd.PASS and vd.judge_no_tailgate([tail])[0] == vd.FAIL
    assert vd.judge_no_tailgate([{"kind": "spoof", "evidence": ""}])[0] == vd.PASS
    assert vd.judge_tailgate([])[0] == vd.FAIL
    assert vd.judge_tailgate([{"kind": "tailgate", "evidence": ""}])[0] == vd.FAIL
    assert vd.judge_tailgate([tail])[0] == vd.PASS
    assert vd.combine(vd.PASS, [True, True]) == vd.PASS
    assert vd.combine(vd.PASS, [True, False]) == vd.FAIL and vd.combine(vd.FAIL, [True]) == vd.FAIL
    assert vd.combine(vd.PASS, [True, None]) == vd.MANUAL


def test_poll_until_returns_elapsed_seconds_or_none_on_timeout():
    clock = {"t": 0.0}
    sleep = lambda s: clock.__setitem__("t", clock["t"] + s)          # noqa: E731
    now = lambda: clock["t"]                                          # noqa: E731
    assert vd.poll_until(lambda: clock["t"] >= 6, 60, 2, now, sleep) == 6
    assert vd.poll_until(lambda: False, 10, 2, now, sleep) is None
    assert vd.poll_until(lambda: True, 10, 2, now, sleep) == 0


def test_probe_summary_reads_scores_csv(tmp_path):
    folder = tmp_path / "genuine_20261113_090000"
    folder.mkdir()
    header = "frame,time,label,note,face_w,brightness,sharpness,ModelA\n"
    rows = "".join(f"{i},0,genuine,,200,100,50,{v}\n" for i, v in enumerate([0.95, 0.80, 0.90]))
    (folder / "scores.csv").write_text(header + rows, encoding="utf-8")
    lines = vd.todays_probe_lines(str(tmp_path), T0.date())
    assert len(lines) == 1 and "ModelA" in lines[0] and "3 幀" in lines[0] and "33%" in lines[0]
    assert vd.todays_probe_lines(str(tmp_path / "none"), T0.date()) == []


# --- 步驟 ---

def test_stop_recognition_always_restores_even_when_polling_blows_up(tmp_path):
    calls = []

    def boom():
        if len(calls) >= 1:
            raise RuntimeError("讀心跳壞了")
        return online()

    ctx, _ = make_ctx(tmp_path, answers=[""], load_front=boom, set_recognition=calls.append)
    with pytest.raises(RuntimeError):
        vd.step_stop_recognition(ctx)
    assert calls == [False, True]                                      # 最後一定恢復允許


def test_stop_recognition_measures_the_time_and_restores(tmp_path):
    calls, beats = [], {"n": 0}

    def load():
        beats["n"] += 1
        return online(running=beats["n"] < 4)                          # 第 4 次起前台回報辨識停止

    ctx, _ = make_ctx(tmp_path, answers=[""], load_front=load, set_recognition=calls.append)
    result = vd.step_stop_recognition(ctx)
    assert result.status == vd.PASS and "秒後收掉" in result.note and calls == [False, True]


def test_stop_recognition_times_out_as_fail_and_still_restores(tmp_path):
    calls = []
    ctx, _ = make_ctx(tmp_path, answers=[""], load_front=lambda: online(), set_recognition=calls.append)
    result = vd.step_stop_recognition(ctx)
    assert result.status == vd.FAIL and calls == [False, True]


def test_stop_recognition_is_not_attempted_when_the_front_is_offline_or_not_running(tmp_path):
    for state in (OFFLINE, online(running=False)):
        calls = []
        ctx, _ = make_ctx(tmp_path, load_front=lambda s=state: s, set_recognition=calls.append)
        result = vd.step_stop_recognition(ctx)
        assert result.status == vd.TODO and calls == []                 # 沒碰後台設定


def test_offline_resync_passes_when_the_punch_arrives_after_reconnect(tmp_path):
    db = make_db(tmp_path)
    states = iter([OFFLINE])
    ctx, _ = make_ctx(tmp_path, answers=["", ""], db=db, load_front=lambda: next(states, online()))
    original_sleep = ctx.sleep

    def sleep(seconds):                                                # 接回後 4 秒，補送的那筆才出現
        original_sleep(seconds)
        if ctx.clock() >= 4 and not vd.query_attendance_since(db, T0):
            add_punch(db, T0 + dt.timedelta(seconds=1))

    ctx.sleep = sleep
    result = vd.step_offline_resync(ctx)
    assert result.status == vd.PASS and "補送完成" in result.note


def test_offline_resync_fails_when_nothing_arrives(tmp_path):
    db = make_db(tmp_path)
    ctx, _ = make_ctx(tmp_path, answers=["", ""], db=db, load_front=lambda: OFFLINE)
    result = vd.step_offline_resync(ctx)
    assert result.status == vd.FAIL and "拔線之後沒有任何出勤紀錄" in result.evidence[1]


def test_multi_person_fails_on_a_tailgate_event_and_passes_without_one(tmp_path):
    db = make_db(tmp_path)
    ctx, _ = make_ctx(tmp_path, answers=["", "y 正常"], db=db)
    assert vd.step_multi_person(ctx).status == vd.PASS
    add_event(db, T0 + dt.timedelta(seconds=30), "tailgate")
    ctx, _ = make_ctx(tmp_path, answers=["", "y"], db=db)
    assert vd.step_multi_person(ctx).status == vd.FAIL
    ctx, _ = make_ctx(tmp_path, answers=["", "n 遠處的人被算進去"], db=make_db(tmp_path / "other"))
    assert vd.step_multi_person(ctx).status == vd.FAIL                  # 沒有事件但人工說不行


def test_tailgate_and_spoof_need_the_event_photo_and_both_human_answers(tmp_path):
    db = make_db(tmp_path)
    root = tmp_path / "anomaly"
    root.mkdir()
    (root / "t.jpg").write_bytes(b"jpg")
    add_event(db, T0 + dt.timedelta(seconds=20), "tailgate", evidence="t.jpg")
    ctx, _ = make_ctx(tmp_path, answers=["", "y 三聲", "", "y 兩聲"], db=db)
    result = vd.step_tailgate_spoof(ctx)
    assert result.status == vd.PASS and any("證據照片" in line for line in result.evidence)
    assert os.path.exists(os.path.join(ctx.session.folder, "證據", "事件1_t.jpg"))
    ctx, _ = make_ctx(tmp_path, answers=["", "y", "", "n 沒聽到"], db=db)
    assert vd.step_tailgate_spoof(ctx).status == vd.FAIL


def test_front_running_and_firewall_use_automatic_evidence(tmp_path):
    ctx, _ = make_ctx(tmp_path, load_front=lambda: online())
    assert vd.step_front_running(ctx).status == vd.PASS
    ctx, _ = make_ctx(tmp_path, load_front=lambda: online(running=False))
    assert vd.step_front_running(ctx).status == vd.FAIL
    ok = pf.Check("前台｜防火牆規則", pf.OK, "兩條規則都在")
    warn = pf.Check("前台｜防火牆規則", pf.WARN, "讀不到")
    bad = pf.Check("前台｜防火牆規則", pf.FAIL, "缺規則")
    assert vd.step_firewall(ctx, [ok]).status == vd.PASS
    assert vd.step_firewall(ctx, [ok, warn]).status == vd.MANUAL
    assert vd.step_firewall(ctx, [bad]).status == vd.FAIL
    assert vd.step_firewall(ctx, []).status == vd.MANUAL


# --- 報告與流程 ---

def test_session_report_is_saved_after_every_step_and_hides_secrets(tmp_path):
    token = "T0ken-" + "x" * 50
    session = vd.Session(str(tmp_path), T0, secrets=[token])
    session.record(vd.Result(1, "項目一", vd.PASS, [f"證據含通行碼 {token}", "第二行|有直線"], "說明"))
    text = open(session.report_path, encoding="utf-8").read()
    assert "項目一" in text and token not in text and HIDDEN in text and "第二行\\|有直線" in text
    session.record(vd.Result(1, "項目一", vd.FAIL))                # 同編號覆蓋，不重複
    again = open(session.report_path, encoding="utf-8").read()
    assert again.count("項目一") == 1 and "失敗" in again
    other = vd.Session(str(tmp_path), T0)                           # 同一分鐘再開一次不會覆蓋
    assert other.folder != session.folder


def fake_checks():
    return [pf.Check("模型檔案", pf.OK, "到齊"), pf.Check("前台｜連線", pf.FAIL, "前台離線", "雙擊啟動前台門禁"),
            pf.Check("前台｜防火牆規則", pf.OK, "兩條規則都在")]


def test_auto_mode_never_asks_anything_and_marks_human_items_as_todo(tmp_path):
    db = make_db(tmp_path)
    add_punch(db, T0)
    ctx, _ = make_ctx(tmp_path, answers=[], db=db, auto=True, load_front=lambda: online(), run_preflight=fake_checks)
    results = vd.run_session(ctx, "abc1234")
    by = {r.number: r for r in results}
    assert by[0].status == vd.FAIL and by[1].status == vd.PASS and by[9].status == vd.PASS
    assert by[7].status == vd.PASS and "只讀今天" in by[7].title
    assert all(by[n].status == vd.TODO and "--auto" in by[n].note for n in (2, 3, 4, 5, 6, 8, 10, 11, 12))
    text = open(ctx.session.report_path, encoding="utf-8").read()
    assert "abc1234" in text and "前台離線" in text


def test_interactive_run_skip_and_quit_keep_every_item_in_the_report(tmp_path):
    db = make_db(tmp_path)
    # 項目 1 自動；項目 2：Enter、y；項目 3：s 略過；項目 4：q 結束
    answers = ["", "y", "s", "", "q"]
    ctx, _ = make_ctx(tmp_path, answers=answers, db=db, load_front=lambda: online(), run_preflight=fake_checks)
    results = vd.run_session(ctx)
    by = {r.number: r for r in results}
    assert by[1].status == vd.PASS and by[2].status == vd.PASS
    assert by[3].status == vd.TODO and by[3].note == "使用者略過"
    assert by[4].status == vd.TODO and by[4].note == "使用者結束"
    assert sorted(by) == list(range(0, 14)) and all(by[n].status == vd.TODO for n in range(5, 13))
    assert by[13].status != vd.TODO or by[13].note                  # 13 是自動項目（讀對話紀錄）
    assert open(ctx.session.report_path, encoding="utf-8").read().count("| 12 |") == 1


def test_a_step_that_crashes_becomes_a_failed_item_not_a_crash(tmp_path):
    def broken():
        raise ValueError("壞掉")

    ctx, _ = make_ctx(tmp_path, answers=["q"], load_front=broken, run_preflight=fake_checks, auto=True)
    results = vd.run_session(ctx)
    crashed = next(r for r in results if r.number == 1)
    assert crashed.status == vd.FAIL and "ValueError" in crashed.note


def test_preflight_crash_is_reported_not_raised(tmp_path):
    def broken():
        raise OSError("x")

    ctx, _ = make_ctx(tmp_path, run_preflight=broken)
    result, checks = vd.phase0(ctx)
    assert result.status == vd.FAIL and "OSError" in result.note and checks == []


# --- claude 分析 ---

class Done:
    def __init__(self, code=0, out="分析結果"):
        self.returncode, self.stdout = code, out.encode("utf-8")


def test_claude_analysis_when_the_command_is_missing_is_skipped_with_an_explanation(tmp_path):
    ok, text = vd.run_claude_analysis(str(tmp_path / "report.md"), str(tmp_path / "a.md"), which=lambda n: None)
    assert ok is False and "找不到 claude" in text and not (tmp_path / "a.md").exists()


def test_claude_analysis_sends_the_prompt_on_stdin_with_read_only_tools_and_scrubs_the_output(tmp_path):
    seen = {}
    token = "S" * 45

    def runner(cmd, **kw):
        seen.update(cmd=cmd, **kw)
        return Done(out=f"問題在 {token}")

    report = tmp_path / "report.md"
    report.write_text("x", encoding="utf-8")
    ok, _ = vd.run_claude_analysis(str(report), str(tmp_path / "分析.md"), which=lambda n: "C:/npm/claude.cmd",
                                   runner=runner)
    assert ok is True
    assert seen["cmd"] == ["C:/npm/claude.cmd", "-p", "--allowedTools", "Read"]
    assert str(report) in seen["input"].decode("utf-8") and "不要修改任何檔案" in seen["input"].decode("utf-8")
    assert seen["timeout"] == vd.CLAUDE_TIMEOUT and seen["cwd"] == str(tmp_path)
    saved = (tmp_path / "分析.md").read_text(encoding="utf-8")
    assert token not in saved and "問題在" in saved


def test_claude_analysis_failures_never_raise(tmp_path):
    def timeout(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 1)

    for runner in (timeout, lambda cmd, **kw: Done(code=1, out=""), lambda cmd, **kw: Done(code=0, out="")):
        ok, text = vd.run_claude_analysis(str(tmp_path / "r.md"), str(tmp_path / "a.md"), which=lambda n: "claude",
                                          runner=runner)
        assert ok is False and "略過" in text


def test_finish_asks_only_in_interactive_mode_and_defaults_to_no(tmp_path):
    called = []
    runner = lambda cmd, **kw: called.append(cmd) or Done()            # noqa: E731
    ctx, _ = make_ctx(tmp_path, answers=[""])                           # 直接 Enter＝預設不要
    assert vd.finish(ctx, which=lambda n: "claude", runner=runner) is None and called == []
    ctx, _ = make_ctx(tmp_path, answers=[], auto=True)
    assert vd.finish(ctx, which=lambda n: "claude", runner=runner) is None
    ctx, _ = make_ctx(tmp_path, answers=[])
    assert vd.finish(ctx, no_claude=True, which=lambda n: "claude", runner=runner) is None
    ctx, said = make_ctx(tmp_path, answers=["y"])
    assert vd.finish(ctx, which=lambda n: "claude", runner=runner) is True and len(called) == 1
    ctx, _ = make_ctx(tmp_path, answers=["Ｙ"])                         # 全形也算
    assert vd.finish(ctx, which=lambda n: None, runner=runner) is False


def test_quit_during_finish_prompt_is_harmless(tmp_path):
    ctx, _ = make_ctx(tmp_path, answers=["q"])
    assert vd.finish(ctx) is None
    with pytest.raises(QuitSession):
        make_ctx(tmp_path, answers=[])[0].console.ask("?")


def test_front_qa_step_passes_only_when_both_sources_were_logged_recently(tmp_path):
    db = make_db(tmp_path)
    ctx, _ = make_ctx(tmp_path, answers=[], db=db, auto=True)
    first = vd.step_front_qa(ctx)
    assert first.status == vd.MANUAL and "開關可能被關閉" in first.note                  # 完全沒有紀錄
    add_qa(db, T0 - dt.timedelta(hours=1), "front_talk")
    partial = vd.step_front_qa(ctx)
    assert partial.status == vd.MANUAL and "只有一種" in partial.note
    assert any("Smart_Chat" in line and "1 題" in line for line in partial.evidence)
    add_qa(db, T0 - dt.timedelta(minutes=5), "front_knowledge")
    done = vd.step_front_qa(ctx)
    assert done.status == vd.PASS and len(done.evidence) == 2
    old = make_db(tmp_path / "old")
    add_qa(old, T0 - dt.timedelta(hours=9), "front_talk")                               # 超過時間窗的不算
    add_qa(old, T0 - dt.timedelta(hours=9), "front_knowledge")
    ctx_old, _ = make_ctx(tmp_path / "old", answers=[], db=old, auto=True)
    assert vd.step_front_qa(ctx_old).status == vd.MANUAL


def test_front_qa_step_reads_the_database_read_only_and_reports_a_missing_table(tmp_path):
    path = str(tmp_path / "empty.db")
    sqlite3.connect(path).close()                                                       # 沒有 ChatLogs 表
    ctx, _ = make_ctx(tmp_path, answers=[], db=path, auto=True)
    result = vd.step_front_qa(ctx)
    assert result.number == 13 and result.status != vd.PASS

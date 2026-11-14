"""前台驗證報告：螢幕分配說明、行程偵測、記錄檔掃描、攝影機被占用不算失敗、時鐘差、報告內容與敏感資訊過濾、聲音問答的略過與結束。"""
import datetime as dt
import os

from access import front_verify as fv
from access.front_screens import Monitor
from shared.verify_common import HIDDEN, Console

NOW = dt.datetime(2026, 11, 13, 9, 0, 0)
LAPTOP = Monitor(0, 0, 1920, 1080, 0, 0, 1920, 1040, True)
EXTERNAL = Monitor(1920, 0, 1920, 1080, 1920, 0, 1920, 1080, False)


def test_monitor_lines_say_where_the_dashboard_and_recognition_will_go():
    lines = fv.describe_monitors([LAPTOP, EXTERNAL])
    assert "主螢幕" in lines[0] and "辨識視窗" in lines[0] and "儀表板" not in lines[0]
    assert "延伸螢幕" in lines[1] and "儀表板" in lines[1]
    single = fv.describe_monitors([LAPTOP])
    assert "只偵測到 1 個螢幕" in single[-1] and "儀表板" in single[0] and "辨識視窗" in single[0]
    assert "取不到螢幕資訊" in fv.describe_monitors([])[0]


def test_process_detection_looks_for_the_module_names():
    status = fv.running_processes(lambda: ["python -m access.front_dashboard", "python -u -m access.front_agent", "x"])
    assert status == {"儀表板": True, "辨識程式": False, "連線代理": True}


def test_log_scan_returns_only_error_lines_from_the_tail(tmp_path):
    (tmp_path / "terminal_app.log").write_text("\n".join(["很舊的 ERROR 不看"] + ["正常"] * 40 + [
        "[terminal_app][ERROR] 讀不到攝影機畫面", "Traceback (most recent call last):", "結尾正常"]), encoding="utf-8")
    (tmp_path / "front_agent.log").write_text("一切正常\n", encoding="utf-8")
    (tmp_path / "其他.txt").write_text("ERROR", encoding="utf-8")
    result = fv.scan_logs(str(tmp_path))
    assert sorted(result) == ["front_agent.log", "terminal_app.log"]
    assert result["front_agent.log"] == []
    assert result["terminal_app.log"] == ["[terminal_app][ERROR] 讀不到攝影機畫面",
                                          "Traceback (most recent call last):"]
    assert fv.scan_logs(str(tmp_path / "none")) == {}


def test_camera_in_use_by_recognition_is_not_a_failure_and_is_not_opened():
    opened = []
    state, text = fv.camera_probe(True, lambda: opened.append(1) or True)
    assert state == "BUSY" and opened == [] and "辨識程式正在使用" in text
    assert fv.camera_probe(False, lambda: True)[0] == "OK"
    assert fv.camera_probe(False, lambda: False)[0] == "FAIL"

    def boom():
        raise OSError("x")

    assert fv.camera_probe(False, boom) == ("FAIL", "攝影機檢查出錯：OSError")


def test_clock_and_gpu_lines():
    assert "還沒量到" in fv.clock_line(None)
    assert "+1.5" in fv.clock_line(1.5) and "已自動校正" not in fv.clock_line(1.5)
    assert "已自動校正" in fv.clock_line(-45.0)
    results = [("OK", "角色：front"), ("OK", "GPU 實際推論正常，每次約 11.0 毫秒（CPU 約 187.0 毫秒）")]
    assert "11.0 毫秒" in fv.gpu_line(results) and fv.gpu_line([("OK", "x")]) is None


def scripted(answers):
    said, queue = [], list(answers)

    def reader(_prompt):
        if not queue:
            raise EOFError
        return queue.pop(0)

    return Console(reader, said.append), said


def run_report(tmp_path, answers=(), secrets_in_checks=False, ask_sounds=True, **overrides):
    console, said = scripted(answers)
    played = []
    token = "Z" * 50
    results = [("OK", "角色：front"), ("WARN", "前台設計為不連網，建議關閉 Wi-Fi（目前連著：Wi-Fi）"),
               ("OK", "GPU 實際推論正常，每次約 11.0 毫秒（CPU 約 187.0 毫秒）")]
    if secrets_in_checks:
        results.append(("FAIL", f"通行碼 {token} 不對"))
    kw = {"now": lambda: NOW, "collect": lambda: results, "monitors_fn": lambda: [LAPTOP, EXTERNAL],
          "process_fn": lambda: ["python -m access.front_dashboard"],
          "read_heartbeat": lambda: {"recognition_running": True}, "read_offset": lambda: 0.4,
          "logs_dir": str(tmp_path / "logs"), "play_voice": lambda: played.append("voice"),
          "play_alert": played.append, "camera_fn": lambda: True, "out_dir": str(tmp_path / "verification"),
          "ask_sounds": ask_sounds}
    kw.update(overrides)
    path = fv.run(console, **kw)
    return path, open(path, encoding="utf-8").read(), played, said, token


def test_report_collects_everything_and_asks_about_the_sounds(tmp_path):
    answers = ["", "y 很清楚"] + ["", "y", "", "n 太小聲", "", "y"]
    path, text, played, _said, _t = run_report(tmp_path, answers)
    assert os.path.basename(path) == "前台_2026-11-13_0900.md"
    for needle in ("前台自我檢查", "GPU 實測速度", "11.0 毫秒", "Wi-Fi", "螢幕 2", "儀表板：執行中", "辨識程式：沒在跑",
                   "[BUSY]", "+0.4", "語音：聽得到、清楚 很清楚", "警示音 非活體（兩聲低沉長音）：不行 太小聲"):
        assert needle in text, needle
    assert played == ["voice", "tailgate", "spoof", "no_scan_walkby"]


def test_secrets_are_hidden_in_the_report(tmp_path):
    _path, text, _p, _s, token = run_report(tmp_path, secrets_in_checks=True, ask_sounds=False)
    assert token not in text and HIDDEN in text


def test_quitting_during_the_sound_questions_still_leaves_a_report(tmp_path):
    path, text, played, _said, _t = run_report(tmp_path, answers=["", "y", "q"])
    assert "使用者提前結束聲音測試" in text and "語音：聽得到、清楚" in text
    assert played == ["voice"]                                   # 第一個警示音之前就按了 q


def test_skipping_and_playback_failures_are_recorded_not_raised(tmp_path):
    def broken_voice():
        raise OSError("沒有喇叭")

    _p, text, _pl, _s, _t = run_report(tmp_path, answers=["s"])
    assert "使用者略過聲音測試" in text
    _p, text, _pl, _s, _t = run_report(tmp_path, answers=["", "", "y", "", "y", "", "y"], play_voice=broken_voice)
    assert "語音播放失敗（OSError）" in text and "警示音 尾隨" in text


def test_report_is_written_before_the_sound_questions(tmp_path):
    """聲音問答前就先存一份（中途關掉也有檢查結果）。"""
    seen = {}

    def reader(_prompt):
        folder = tmp_path / "verification"
        seen["files"] = [p.name for p in folder.iterdir()]
        seen["body"] = (folder / seen["files"][0]).read_text(encoding="utf-8")
        raise EOFError

    fv.run(Console(reader, lambda _t: None), now=lambda: NOW, collect=lambda: [("OK", "x")], monitors_fn=lambda: [],
           process_fn=lambda: [], read_heartbeat=lambda: {}, read_offset=lambda: None, logs_dir=str(tmp_path / "l"),
           play_voice=lambda: None, play_alert=lambda k: None, camera_fn=lambda: True,
           out_dir=str(tmp_path / "verification"))
    assert seen["files"] == ["前台_2026-11-13_0900.md"] and "前台自我檢查" in seen["body"]

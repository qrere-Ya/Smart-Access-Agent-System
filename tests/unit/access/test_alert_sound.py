"""警示提示音：音型對應、每種警示各自冷卻、開關、背景播放與失敗不影響辨識、terminal_app 的呼叫點。"""
import os

from access import alert_sound as sound


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def make(clock=None, enabled=True, played=None, cooldown=20.0, play=None):
    played = played if played is not None else []
    return sound.AlertSound(play=play or played.append, clock=clock or Clock(), cooldown=cooldown,
                            is_enabled=lambda: enabled, spawn=lambda fn: fn())     # 測試時同步執行，不開執行緒


def test_each_kind_has_its_own_distinguishable_pattern():
    patterns = {k: tuple(sound.pattern_for(k)) for k in ("tailgate", "spoof", "no_scan_walkby")}
    assert len(set(patterns.values())) == 3                                        # 三種都不一樣
    assert len(patterns["tailgate"]) == 3 and len(patterns["spoof"]) == 2 and len(patterns["no_scan_walkby"]) == 1
    assert sound.pattern_for("something_new") == sound.DEFAULT_PATTERN             # 不認得的種類仍會響


def test_a_kind_is_silent_during_its_cooldown_and_rings_again_after():
    clock, played = Clock(), []
    alert = make(clock, played=played)
    assert alert.notify("spoof") is True
    for _ in range(300):                                                           # 非活體每幀都會判定
        clock.t += 0.05
        assert alert.notify("spoof") is False
    clock.t += 20
    assert alert.notify("spoof") is True and len(played) == 2


def test_cooldowns_are_per_kind_so_one_alert_does_not_mute_another():
    played = []
    alert = make(played=played)
    assert alert.notify("tailgate") and alert.notify("spoof") and alert.notify("no_scan_walkby")
    assert [tuple(p) for p in played] == [tuple(sound.pattern_for(k)) for k in ("tailgate", "spoof", "no_scan_walkby")]


def test_the_switch_turns_everything_off_without_using_up_the_cooldown():
    played, flag = [], {"on": False}
    alert = sound.AlertSound(play=played.append, clock=Clock(), is_enabled=lambda: flag["on"], spawn=lambda fn: fn())
    assert alert.notify("tailgate") is False and played == []
    flag["on"] = True
    assert alert.notify("tailgate") is True                                        # 開回來馬上能響


def test_environment_switch_values():
    assert sound.enabled({}) is True and sound.enabled({sound.ENV_SWITCH: "1"}) is True
    for off in ("0", "false", "OFF", " no "):
        assert sound.enabled({sound.ENV_SWITCH: off}) is False


def test_a_missing_sound_device_only_warns_and_never_raises(capsys):
    def broken(_pattern):
        raise RuntimeError("沒有音效裝置")

    alert = make(play=broken)
    assert alert.notify("tailgate") is True                                        # 已排入，失敗只印警告
    assert "播放失敗" in capsys.readouterr().out


def test_a_scheduling_failure_is_swallowed_too(capsys):
    def boom(_fn):
        raise OSError("無法開執行緒")

    alert = sound.AlertSound(play=lambda p: None, clock=Clock(), is_enabled=lambda: True, spawn=boom)
    assert alert.notify("spoof") is False and "排程失敗" in capsys.readouterr().out


def test_the_default_spawn_runs_in_a_background_thread_without_blocking():
    import threading
    import time
    started, release = threading.Event(), threading.Event()

    def slow(_pattern):
        started.set()
        release.wait(5)

    alert = sound.AlertSound(play=slow, clock=Clock(), is_enabled=lambda: True)
    t0 = time.perf_counter()
    assert alert.notify("tailgate") is True
    assert time.perf_counter() - t0 < 0.5                                          # 播放卡住時 notify 也立刻回來
    assert started.wait(2)
    release.set()


def test_terminal_app_rings_where_each_alert_is_raised():
    from shared import paths
    with open(os.path.join(paths.PROJECT_ROOT, "access", "terminal_app.py"), encoding="utf-8") as f:
        source = f.read()
    assert "alert_sound = AlertSound()" in source
    for needle in ('backend.alert("spoof"', "_publish_tailgate_alert(current_time)",
                   "backend.anomaly(pass_through.EVENT_TYPE"):
        after = source[source.index(needle):]
        assert "alert_sound.notify(" in after[:400], needle                         # 緊接在警示發生處之後
    assert source.count("alert_sound.notify(") == 3

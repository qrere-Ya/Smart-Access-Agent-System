"""語音播報：名字與問候分段合成並停頓、開頭與結尾靜音、語速可調且壞值退回預設。"""
import numpy as np
import pytest

pytest.importorskip("sounddevice")
pytest.importorskip("sherpa_onnx")

from access import audio_player as ap  # noqa: E402


def test_greeting_is_split_into_name_and_greeting():
    assert ap.plan_segments("葉君緯，早安") == ["葉君緯", "早安"]
    assert ap.plan_segments("葉君緯，午安。") == ["葉君緯", "午安"]
    assert ap.plan_segments("智慧門禁系統，已啟用。") == ["智慧門禁系統", "已啟用"]


def test_empty_pieces_and_single_segments_are_handled():
    assert ap.plan_segments("早安") == ["早安"]
    assert ap.plan_segments("，，早安，") == ["早安"]
    assert ap.plan_segments("") == [] and ap.plan_segments("，。") == []


def test_assemble_puts_silence_at_the_start_between_segments_and_at_the_end():
    rate = 1000
    one, two = np.ones(100, dtype=np.float32), np.full(50, 2.0, dtype=np.float32)
    out = ap.assemble([one, two], rate, pause=0.5, lead=0.2, tail=0.4)
    assert len(out) == 200 + 100 + 500 + 50 + 400
    assert not out[:200].any() and (out[200:300] == 1.0).all()              # 開頭靜音，然後第一段
    assert not out[300:800].any() and (out[800:850] == 2.0).all()           # 停頓，然後第二段
    assert not out[850:].any()                                              # 結尾靜音


def test_a_single_segment_has_no_pause_and_nothing_gives_only_silence():
    out = ap.assemble([np.ones(10, dtype=np.float32)], 1000, pause=0.5, lead=0.0, tail=0.0)
    assert len(out) == 10
    assert not ap.assemble([], 1000).any()


def test_speed_is_slower_than_native_and_can_be_tuned_by_the_environment():
    assert ap.DEFAULT_SPEED < 1.0 and ap.SEGMENT_PAUSE >= 0.4
    assert ap.speech_speed(environ={}) == ap.DEFAULT_SPEED
    assert ap.speech_speed(environ={"SMART_ACCESS_TTS_SPEED": "0.7"}) == 0.7
    for bad in ("太快", "", "0", "3", "-1"):
        assert ap.speech_speed(environ={"SMART_ACCESS_TTS_SPEED": bad}) == ap.DEFAULT_SPEED


def test_volume_setting_defaults_louder_than_native_and_rejects_bad_values():
    assert ap.DEFAULT_VOLUME > 1.0
    assert ap.volume_setting(environ={}) == ap.DEFAULT_VOLUME
    assert ap.volume_setting(environ={"SMART_ACCESS_TTS_VOLUME": "2.5"}) == 2.5
    for bad in ("很大", "", "0", "0.1", "10", "-2"):
        assert ap.volume_setting(environ={"SMART_ACCESS_TTS_VOLUME": bad}) == ap.DEFAULT_VOLUME


def test_apply_volume_is_louder_but_never_exceeds_full_scale():
    quiet = np.array([0.0, 0.2, -0.3, 0.5], dtype=np.float32)
    louder = ap.apply_volume(quiet, 1.6)
    assert np.allclose(louder, quiet * 1.6, atol=1e-6)                       # 沒超過壓縮起點：單純放大
    loud = ap.apply_volume(np.array([0.9, -0.95, 1.0, 0.1], dtype=np.float32), 3.0)
    assert np.abs(loud).max() <= 1.0 and loud[0] > 0 > loud[1]               # 超過的部分被壓縮，正負號不變
    assert loud[0] <= loud[2]                                                # 順序保持（越大聲越大）
    assert ap.apply_volume(quiet, 1.0).dtype == np.float32


def test_only_the_punch_greeting_pauses_other_speech_is_one_piece():
    assert ap.segments_for("葉君緯，早安", pause=True) == ["葉君緯", "早安"]
    assert ap.segments_for("智慧門禁系統，已啟用。", pause=False) == ["智慧門禁系統，已啟用"]
    assert ap.segments_for("智慧門禁系統已啟用", pause=False) == ["智慧門禁系統已啟用"]
    assert ap.segments_for("。", pause=False) == []

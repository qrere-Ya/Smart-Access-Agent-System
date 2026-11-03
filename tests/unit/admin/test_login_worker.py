"""
login_worker 單元測試：輸出協定（編解碼、解析、欄位驗證）、指令讀取、serve 主迴圈（假鏡頭與假視覺）。
不需要真的鏡頭、模型或顯示器。
"""
import io
import json

import numpy as np
import pytest

from admin import face_login, login_worker as lw


def _frame(h=720, w=1280):
    return np.full((h, w, 3), 120, dtype=np.uint8)


def _step(**kw):
    return face_login.LoginStep(**{"status": face_login.NO_FACE, "message": "請站到鏡頭前", **kw})


# --- 畫面編解碼 ---

def test_frame_roundtrip_is_scaled_to_the_preview_size():
    decoded = lw.decode_frame(lw.encode_frame(_frame()))
    assert decoded.shape == (lw.PREVIEW_SIZE[1], lw.PREVIEW_SIZE[0], 3)


@pytest.mark.parametrize("bad", ["", "not base64 !!", "aGVsbG8="])      # 空字串、非 base64、合法 base64 但不是圖片
def test_decode_rejects_broken_preview_data(bad):
    with pytest.raises(ValueError):
        lw.decode_frame(bad)


# --- 輸出協定 ---

def test_a_valid_step_line_roundtrips():
    message = lw.step_to_message(_frame(), _step(progress=0.4, distance=0.81, pose={"width": 150, "yaw": 0.1}))
    parsed = lw.parse_line(lw.make_line(message))
    assert parsed["t"] == "step" and parsed["step"]["distance"] == 0.81
    assert face_login.LoginStep(**parsed["step"]).pose == {"width": 150, "yaw": 0.1}


def test_a_success_step_keeps_the_admin_name():
    parsed = lw.parse_line(lw.make_line(lw.step_to_message(
        _frame(), _step(status=face_login.SUCCESS, message="成功", admin_name="管理者甲", progress=1.0))))
    assert parsed["step"]["admin_name"] == "管理者甲" and parsed["step"]["status"] == face_login.SUCCESS


def test_protocol_lines_are_pure_ascii_so_the_childs_code_page_cannot_garble_chinese():
    line = lw.make_line({"t": "error", "message": "打不開鏡頭"})
    assert line.isascii() and lw.parse_line(line)["message"] == "打不開鏡頭"


def test_an_error_line_is_parsed():
    message = {"t": "error", "message": "打不開鏡頭"}
    assert lw.parse_line(lw.make_line(message)) == message


def _line(message):
    return lw.PREFIX + json.dumps(message)


@pytest.mark.parametrize("raw", [
    "Loading weights: 100%",                                                   # 模型載入訊息，不是協定行
    "",
    "@@not json",
    "@@[1, 2, 3]",                                                             # 不是物件
    '@@{"t": "unknown"}',
    '@@{"t": "error"}',                                                        # 缺說明
    '@@{"t": "error", "message": 5}',
    '@@{"t": "step", "step": {"status": "no_face", "message": "x"}}',          # 缺預覽
    '@@{"t": "step", "jpg": 1, "step": {"status": "no_face", "message": "x"}}',
])
def test_garbage_lines_are_ignored(raw):
    assert lw.parse_line(raw) is None


@pytest.mark.parametrize("step", [
    None, {}, [], "success",
    {"status": "success"},                                                      # 缺說明
    {"status": "hacked", "message": "x"},                                       # 未知狀態
    {"status": "no_face", "message": "x", "extra": 1},                          # 未知欄位
    {"status": "no_face", "message": 1},
    {"status": "no_face", "message": "x", "progress": "high"},
    {"status": "no_face", "message": "x", "distance": "0.1"},
    {"status": "success", "message": "x", "admin_name": ["a"]},
    {"status": "no_face", "message": "x", "pose": [1]},
])
def test_steps_with_wrong_fields_or_types_are_rejected(step):
    assert lw.parse_line(_line({"t": "step", "jpg": "abc", "step": step})) is None


# --- 指令 ---

def test_commands_reset_then_stop():
    commands = lw.Commands()
    commands.listen(io.StringIO("reset\nstop\nreset\n"))
    assert commands.reset.is_set() and commands.stop.is_set()


def test_the_input_ending_means_stop():
    commands = lw.Commands()
    commands.listen(io.StringIO(""))
    assert commands.stop.is_set() and not commands.reset.is_set()


def test_unknown_commands_are_ignored():
    commands = lw.Commands()
    commands.listen(io.StringIO("format c:\nrm -rf\n"))
    assert not commands.reset.is_set()          # 沒有任何指令生效（輸入結束只會讓它停止）


# --- serve 主迴圈 ---

class FakeCamera:
    def __init__(self, frames=3, fail_after=None):
        self.left, self.released, self.fail_after, self.reads = frames, False, fail_after, 0

    def read(self):
        self.reads += 1
        if self.fail_after is not None and self.reads > self.fail_after:
            return False, None
        return True, _frame(240, 320)

    def release(self):
        self.released = True


class FakeVerifier:
    instances = []

    def __init__(self, vision):
        self.resets, self.steps = 0, 0
        FakeVerifier.instances.append(self)

    def reset(self):
        self.resets += 1

    def step(self, frame):
        self.steps += 1
        return _step(progress=self.steps / 10)


@pytest.fixture(autouse=True)
def _fake_verifier(monkeypatch):
    FakeVerifier.instances.clear()
    monkeypatch.setattr(lw.face_login, "FaceLoginVerifier", FakeVerifier)


class FakeVision:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def _run(camera, vision=None, stop_after=3, reset_at=None):
    commands, out = lw.Commands(), []
    vision = vision or FakeVision()

    def emit(message):
        out.append(message)
        if len([m for m in out if m["t"] == "step"]) >= stop_after:
            commands.stop.set()
        if reset_at is not None and len(out) == reset_at:
            commands.reset.set()

    lw.serve(lambda: camera, lambda: vision, emit, commands)
    return out, vision


def test_serve_streams_steps_until_stopped_and_cleans_up():
    camera = FakeCamera()
    out, vision = _run(camera, stop_after=3)
    assert [m["t"] for m in out] == ["step"] * 3 and camera.released and vision.closed
    assert out[2]["step"]["progress"] == pytest.approx(0.3)


def test_a_reset_command_clears_the_verifier():
    out, _ = _run(FakeCamera(), stop_after=4, reset_at=2)
    assert FakeVerifier.instances[0].resets == 1


def test_serve_reports_a_missing_camera():
    commands, out = lw.Commands(), []
    lw.serve(lambda: None, lambda: pytest.fail("沒有鏡頭就不該載入視覺"), out.append, commands)
    assert out == [{"t": "error", "message": out[0]["message"]}] and "打不開鏡頭" in out[0]["message"]


def test_serve_reports_a_camera_that_stops_giving_frames():
    camera = FakeCamera(fail_after=1)
    out, vision = _run(camera, stop_after=99)
    assert out[-1]["t"] == "error" and "讀不到鏡頭畫面" in out[-1]["message"] and camera.released and vision.closed


def test_serve_only_reports_the_exception_type():
    commands, out = lw.Commands(), []
    camera = FakeCamera()

    def broken_vision():
        raise RuntimeError("C:\\secret\\path\\model.onnx 載入失敗")

    lw.serve(lambda: camera, broken_vision, out.append, commands)
    assert out[0]["t"] == "error" and "RuntimeError" in out[0]["message"] and "secret" not in out[0]["message"]
    assert camera.released


def test_encode_failure_is_reported_not_raised(monkeypatch):
    monkeypatch.setattr(lw, "encode_frame", lambda f: (_ for _ in ()).throw(ValueError("x")))
    out, _ = _run(FakeCamera(), stop_after=1)
    assert out[0]["t"] == "error" and "ValueError" in out[0]["message"]

"""
face_login 單元測試：逐幀判斷的通過與失敗條件（視覺函式與管理者比對都用假的，不需要鏡頭與模型）。
"""
import pytest

from admin import face_login as fl

FACE = {"x1": 100, "y1": 100, "x2": 260, "y2": 280, "left_eye": (140, 150), "right_eye": (220, 150),
        "nose": (180, 190), "left_lip": (150, 230), "right_lip": (210, 230)}


class FakeVision:
    """按照腳本逐幀回傳偵測結果與活體分數。"""

    def __init__(self, script):
        self.script = list(script)   # 每項 (偵測到的臉數, 活體分數)
        self.current = (1, 0.99)

    def detect(self, frame):
        self.current = self.script.pop(0) if self.script else self.current
        return "rgb", [dict(FACE) for _ in range(self.current[0])]

    def liveness(self, frame, box):
        return self.current[1]

    def embed(self, rgb, face):
        return "embedding"


def verifier(script, who=("王小明", 0.4), **kwargs):
    answers = who if isinstance(who, list) else [who]
    state = {"i": 0}

    def recognize(embedding, threshold):
        result = answers[min(state["i"], len(answers) - 1)]
        state["i"] += 1
        return result

    kwargs.setdefault("ready_frames", 0)         # 一般測試不管正面穩定等待；專門的測試再打開
    return fl.FaceLoginVerifier(FakeVision(script), recognize=recognize, **kwargs)


def run(v, frames):
    return [v.step(None) for _ in range(frames)]


def test_five_consecutive_good_frames_log_the_admin_in():
    steps = run(verifier([]), 5)
    assert [s.status for s in steps] == [fl.VERIFYING] * 4 + [fl.SUCCESS]
    assert steps[-1].admin_name == "王小明" and steps[-1].progress == 1.0
    assert [round(s.progress, 1) for s in steps[:4]] == [0.2, 0.4, 0.6, 0.8]


def test_a_fake_face_never_gets_in_and_counts_as_one_failure_after_eight_frames():
    steps = run(verifier([(1, 0.30)]), 8)
    assert [s.status for s in steps[:7]] == [fl.VERIFYING] * 7
    assert steps[7].status == fl.FAILED and steps[7].admin_name is None


def test_a_real_face_that_is_not_an_admin_fails_with_the_same_message():
    fake = verifier([(1, 0.30)] * 8)
    stranger = verifier([], who=("Unknown", 1.2))
    messages = {run(fake, 8)[-1].message, run(stranger, 8)[-1].message}
    assert messages == {"驗證失敗，請對準鏡頭再試一次"}     # 不透露是活體還是比對失敗


def test_no_face_or_several_faces_reset_the_streak_but_are_not_failures():
    v = verifier([(1, 0.99)] * 3 + [(0, 0.0)] + [(1, 0.99)] * 5)
    steps = run(v, 9)
    assert steps[3].status == fl.NO_FACE
    assert steps[-1].status == fl.SUCCESS                   # 中斷後重新累計滿 5 幀才通過
    crowded = verifier([(2, 0.99)])
    assert crowded.step(None).status == fl.MULTIPLE and crowded._bad_streak == 0


def test_an_incomplete_face_is_neutral():
    step = verifier([(1, -1.0)]).step(None)
    assert step.status == fl.NO_FACE


def test_switching_to_another_admin_in_the_middle_starts_over():
    who = [("甲", 0.3)] * 3 + [("乙", 0.3)] * 5
    steps = run(verifier([], who=who), 8)
    assert steps[2].status == fl.VERIFYING and steps[3].progress == pytest.approx(0.2)
    assert steps[-1].status == fl.SUCCESS and steps[-1].admin_name == "乙"


def test_a_bad_frame_in_the_middle_resets_the_progress():
    v = verifier([(1, 0.99)] * 3 + [(1, 0.2)] + [(1, 0.99)] * 5)
    steps = run(v, 9)
    assert steps[3].progress == 0.0 and steps[-1].status == fl.SUCCESS


def test_no_admin_registered_is_reported():
    assert verifier([], who=("No Admin", 99.9)).step(None).status == fl.NO_ADMIN


def test_the_distance_is_passed_to_the_screen_for_tuning():
    assert verifier([], who=("王小明", 0.37)).step(None).distance == 0.37


def test_the_default_recognizer_only_looks_at_admins(db, add_user):
    import numpy as np

    add_user("員工", "E1", embedding=np.zeros(4, dtype=np.float32), role="employee")
    add_user("管理者", "E2", embedding=np.ones(4, dtype=np.float32), role="admin")
    v = fl.FaceLoginVerifier(FakeVision([]))
    v._recognize = None
    # 員工的特徵剛好是 0，管理者是 1；用 0 去比，只能找到管理者，而且距離 2.0 > 0.8 → Unknown
    assert v._recognize_admin(np.zeros(4, dtype=np.float32))[0] == "Unknown"


def test_no_admin_resets_the_progress_so_it_cannot_carry_over():
    v = verifier([], who=[("王小明", 0.3)] * 3 + [("No Admin", 99.9)] + [("王小明", 0.3)] * 2)
    steps = run(v, 6)
    assert steps[3].status == fl.NO_ADMIN
    assert steps[-1].status == fl.VERIFYING and steps[-1].progress == pytest.approx(0.4)   # 重新累計，不是延續


# --- 正面檢查 ---

def turned(**changes):
    """複製標準正面臉，改掉指定的關鍵點或臉框。"""
    face = dict(FACE)
    face.update(changes)
    return face


class FaceVision(FakeVision):
    """每幀回傳指定的臉（用來測試不夠正面的情況）。"""

    def __init__(self, face):
        super().__init__([])
        self.face = face

    def detect(self, frame):
        return "rgb", [dict(self.face)]


def not_frontal_cases():
    return [
        turned(nose=(215, 190)),                              # 轉頭：鼻子偏向一側
        turned(left_eye=(140, 120), right_eye=(220, 180)),    # 歪頭：雙眼連線傾斜
        turned(nose=(180, 160)),                              # 仰頭：鼻子太靠近眼睛
        turned(nose=(180, 225)),                              # 低頭：鼻子太靠近嘴巴
        turned(x2=150),                                       # 臉太小
        {k: v for k, v in FACE.items() if k != "nose"},       # 缺關鍵點
    ]


@pytest.mark.parametrize("face", not_frontal_cases())
def test_a_face_that_is_not_frontal_is_never_compared_and_never_fails(face):
    calls = []
    v = fl.FaceLoginVerifier(FaceVision(face), recognize=lambda e, t: calls.append(1) or ("王小明", 0.4))
    steps = run(v, 30)
    assert {s.status for s in steps} == {fl.NOT_FRONTAL}
    assert calls == []                                        # 沒有拿去比對
    assert steps[0].message and steps[0].pose is not None


def test_comparison_starts_only_after_the_frontal_face_has_been_steady():
    calls = []
    v = fl.FaceLoginVerifier(FaceVision(FACE), recognize=lambda e, t: calls.append(1) or ("王小明", 0.4))
    steps = run(v, fl.READY_FRAMES - 1 + 5)      # 前幾幀等穩定，之後連續 5 幀比對
    assert [s.status for s in steps[:fl.READY_FRAMES - 1]] == [fl.ALIGNING] * (fl.READY_FRAMES - 1)
    assert len(calls) == 5 and steps[-1].status == fl.SUCCESS


def test_turning_away_in_the_middle_restarts_the_steady_wait():
    side = turned(nose=(215, 190))

    class Switching(FaceVision):
        def __init__(self):
            super().__init__(FACE)
            self.turn = 0

        def detect(self, frame):
            self.turn += 1
            return "rgb", [dict(side if self.turn == 4 else FACE)]

    v = fl.FaceLoginVerifier(Switching(), recognize=lambda e, t: ("王小明", 0.4))
    statuses = [s.status for s in run(v, 6)]
    assert statuses[3] == fl.NOT_FRONTAL and fl.SUCCESS not in statuses


# --- 平均距離 ---

def test_the_login_needs_the_average_distance_not_each_frame_below_the_limit():
    dists = [("王小明", d) for d in (0.99, 0.80, 0.85, 0.79, 0.87)]      # 單幀都小於 1.0，平均 0.86
    steps = run(verifier([], who=dists), 5)
    assert steps[-1].status == fl.SUCCESS and steps[-1].distance == 0.86


def test_frames_that_are_each_close_enough_but_average_too_far_count_as_a_failure():
    dists = [("王小明", d) for d in (0.95, 0.96, 0.97, 0.98, 0.99)]      # 平均 0.97 ≥ 0.9
    steps = run(verifier([], who=dists), 5)
    assert steps[-1].status == fl.FAILED and steps[-1].admin_name is None
    assert steps[-1].distance == 0.97


def test_a_single_frame_beyond_the_frame_limit_is_not_matched():
    steps = run(verifier([], who=("Unknown", 1.05)), 8)
    assert steps[-1].status == fl.FAILED

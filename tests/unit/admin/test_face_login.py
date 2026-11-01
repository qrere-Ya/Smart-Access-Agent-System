"""
face_login 單元測試：逐幀判斷的通過與失敗條件（視覺函式與管理者比對都用假的，不需要鏡頭與模型）。
"""
import pytest

from admin import face_login as fl

FACE = {"x1": 10, "y1": 10, "x2": 90, "y2": 90}


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

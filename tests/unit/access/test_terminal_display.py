"""門禁終端機畫面的純邏輯：正面檢查（轉頭不比對）、打卡結果停留時間、字型沒有的符號不畫成方框。"""
import numpy as np
import pytest

from access import pose, punch_state, utils

FRONT = {"x1": 100, "y1": 100, "x2": 260, "y2": 280, "left_eye": (140, 150), "right_eye": (220, 150),
         "nose": (180, 190), "left_lip": (150, 230), "right_lip": (210, 230)}


def face(**changes):
    return {**FRONT, **changes}


# --- 正面檢查 ---

def test_a_frontal_face_passes():
    ok, hint, measured = pose.frontal_check(face())
    assert ok and hint == "" and measured["yaw"] == 0


@pytest.mark.parametrize("changes,word", [
    ({"nose": (230, 190)}, "轉頭"),                                   # 鼻子偏到一側
    ({"left_eye": (140, 120), "right_eye": (220, 180)}, "歪頭"),      # 雙眼連線傾斜
    ({"nose": (180, 150)}, "仰頭"),                                   # 鼻子貼近眼睛
])
def test_clear_turns_are_rejected_with_a_hint(changes, word):
    ok, hint, _ = pose.frontal_check(face(**changes))
    assert not ok and word in hint


def test_the_thresholds_are_the_callers_choice():
    turned = face(nose=(205, 190))                                   # yaw 約 0.31
    assert not pose.frontal_check(turned, max_yaw=0.25)[0]
    assert pose.frontal_check(turned, max_yaw=0.35)[0]


def test_a_small_face_is_only_rejected_when_a_minimum_width_is_asked():
    small = face(x2=150)
    assert pose.frontal_check(small)[0]
    assert "靠近" in pose.frontal_check(small, min_width=100)[1]


def test_missing_landmarks_count_as_not_frontal():
    broken = {k: v for k, v in FRONT.items() if k != "nose"}
    assert not pose.frontal_check(broken)[0]


def test_the_login_check_still_uses_the_shared_logic():
    from admin import face_login

    assert face_login.frontal_check(face())[0]
    assert not face_login.frontal_check(face(nose=(215, 190)))[0]    # 後台登入維持較嚴的 0.25


# --- 打卡結果停留 ---

def test_the_first_sighting_is_logged():
    assert punch_state.punch_phase(100.0, None, 4.0, 60.0) == punch_state.LOG


@pytest.mark.parametrize("elapsed,expected", [
    (0.0, punch_state.SHOW_RESULT), (3.9, punch_state.SHOW_RESULT),       # 成功畫面要看得到
    (4.0, punch_state.COOLDOWN), (59.9, punch_state.COOLDOWN),
    (60.0, punch_state.LOG), (500.0, punch_state.LOG),
])
def test_the_success_screen_is_held_before_the_cooldown_message(elapsed, expected):
    assert punch_state.punch_phase(1000.0 + elapsed, 1000.0, 4.0, 60.0) == expected


# --- 字型沒有的符號 ---

def test_pictographs_are_removed_but_chinese_and_punctuation_stay():
    text = "⚠️ 拒絕：非活體攻擊 (score=0.123)\U0001f6a8"
    assert utils.printable(text) == " 拒絕：非活體攻擊 (score=0.123)"
    assert utils.printable("【上班報到成功】！") == "【上班報到成功】！"


def test_drawing_text_with_a_pictograph_does_not_fail():
    img = np.zeros((80, 300, 3), dtype=np.uint8)
    out = utils.put_chinese_text(img, "⚠️ 拒絕", (5, 5), (0, 0, 255), 30)
    assert out.shape == img.shape and out.any()

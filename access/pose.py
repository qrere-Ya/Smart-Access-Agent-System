"""
pose.py：用臉框與 5 個關鍵點判斷是不是正面（純邏輯，不需要模型）。

主要功能：
  - frontal_check：轉頭、歪頭、仰頭低頭、臉太小都回傳「不是正面」與給使用者的提示，並附量測值方便現場調整門檻
  - 門檻由呼叫端傳入：後台登入（admin.face_login）要求嚴格，門禁終端機（terminal_app）只擋明顯轉頭，兩邊共用同一套算法

串接模組：
  - admin.face_login：後台人臉登入的正面檢查
  - terminal_app：轉頭時不比對身分，改顯示提示，避免把側臉誤報成「未登記臉孔」
"""
import math

NOT_FRONTAL_HINT = "請正對鏡頭"


def frontal_check(face, min_width=0, max_yaw=0.25, max_roll=15.0, pitch_range=(0.30, 0.70)):
    """回傳 (是否正面, 提示文字, 量測值)；關鍵點缺漏或不合理一律當作不是正面。"""
    width = float(face["x2"]) - float(face["x1"])
    pose = {"width": round(width)}
    try:
        (lx, ly), (rx, ry) = face["left_eye"], face["right_eye"]
        nx, ny = face["nose"]
        (_, lly), (_, rly) = face["left_lip"], face["right_lip"]
    except (KeyError, TypeError, ValueError):
        return False, NOT_FRONTAL_HINT, pose
    eye_dist = math.hypot(rx - lx, ry - ly)
    mouth_y, eye_y = (lly + rly) / 2, (ly + ry) / 2
    if eye_dist <= 0 or mouth_y <= eye_y:
        return False, NOT_FRONTAL_HINT, pose
    roll = abs(math.degrees(math.atan2(ry - ly, rx - lx)))
    roll = 180 - roll if roll > 90 else roll
    yaw = abs(nx - (lx + rx) / 2) / eye_dist
    pitch = (ny - eye_y) / (mouth_y - eye_y)
    pose.update(yaw=round(float(yaw), 2), roll=round(float(roll), 1), pitch=round(float(pitch), 2))
    if width < min_width:
        return False, "請再靠近鏡頭一點", pose
    if roll > max_roll:
        return False, "請把頭擺正，不要歪頭", pose
    if yaw > max_yaw:
        return False, "請正面看向鏡頭，不要轉頭", pose
    if not pitch_range[0] <= pitch <= pitch_range[1]:
        return False, "請平視鏡頭，不要仰頭或低頭", pose
    return True, "", pose

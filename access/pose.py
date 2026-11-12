"""
pose.py：用臉框與 5 個關鍵點判斷是不是正面（純邏輯，不需要模型）。

主要功能：
  - frontal_check：轉頭、歪頭、仰頭低頭、臉太小都回傳「不是正面」與給使用者的提示，並附量測值方便現場調整門檻
  - nearest_face／near_person_boxes：畫面有很多人時，只處理離鏡頭最近的人（臉框面積最大），遠處的人不影響打卡與登入
  - 門檻由呼叫端傳入：後台登入（admin.face_login）要求嚴格，門禁終端機（terminal_app）只擋明顯轉頭，兩邊共用同一套算法

串接模組：
  - admin.face_login：後台人臉登入的正面檢查
  - terminal_app：轉頭時不比對身分，改顯示提示，避免把側臉誤報成「未登記臉孔」
"""
import math

NEAR_PERSON_RATIO = 0.5     # 人物框面積要有「最近那個人」的這個比例以上才算同一個門口的人（背景路人太小，不算）

NOT_FRONTAL_HINT = "請正對鏡頭"


def box_area(box):
    """框 (x1, y1, x2, y2) 或臉偵測結果（含 x1、y1、x2、y2）的面積。"""
    if isinstance(box, dict):
        box = (box["x1"], box["y1"], box["x2"], box["y2"])
    return max(0.0, float(box[2]) - float(box[0])) * max(0.0, float(box[3]) - float(box[1]))


def nearest_face(faces):
    """多張臉時取離鏡頭最近的那一張（臉框面積最大）；沒有臉回傳 None。"""
    return max(faces, key=box_area) if faces else None


def near_person_boxes(boxes, ratio=NEAR_PERSON_RATIO):
    """只留下離鏡頭最近的人，以及跟他差不多近的人（面積 >= 最大框的 ratio 倍）；遠處背景的人不算。"""
    if not boxes:
        return []
    biggest = max(box_area(b) for b in boxes)
    return [b for b in boxes if box_area(b) >= biggest * ratio]


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

"""
face_login.py：管理者人臉登入的判斷邏輯（逐幀判斷，不含畫面；視覺函式可由外部換成假的，方便測試）。

主要功能：
  - FaceLoginVerifier.step(frame)：每個影格回傳一個 LoginStep（狀態、提示文字、進度、通過時的管理者姓名）
  - 通過條件：連續 REQUIRED_FRAMES 幀都「只有一張臉、活體分數達標、比對到同一位管理者、單幀距離小於
    FRAME_DISTANCE_MAX」，而且這幾幀的平均距離小於 DISTANCE_MAX（平均可抵銷單幀抖動）；平均不過算一次失敗
  - 正面檢查：臉夠大、沒有明顯轉頭／歪頭／仰低頭才進入比對，而且要連續 READY_FRAMES 幀都是正面才開始；
    不夠正面只提示「請正對鏡頭」，不比對也不算失敗
  - 失敗判定：有正面的臉但連續 FAIL_FRAMES 幀不是活體或不是管理者，算一次失敗（交給 LoginGuard 計數）；
    沒有臉、多張臉、不夠正面都不算失敗，只是把連續成功的計數歸零
  - 只回傳「驗證失敗」類的提示，不透露是活體不過還是比對不過

串接模組：
  - vision_core：真正的人臉偵測、對齊、特徵、活體（default_vision 才會載入，單元測試不需要）
  - database_mgr.recognize_admin：只和啟用中的管理者比對
  - login_window：每個影格呼叫 step，並依結果更新畫面與失敗計數
"""
import math
from dataclasses import dataclass
from typing import Optional

REQUIRED_FRAMES = 5
FAIL_FRAMES = 8
LIVENESS_MIN = 0.90
DISTANCE_MAX = 0.9          # 連續幾幀的平均距離上限
FRAME_DISTANCE_MAX = 1.0    # 單幀距離上限（與門禁相同），超過的幀直接算沒比對到
READY_FRAMES = 3          # 連續幾幀都是正面，才開始比對
MIN_FACE_WIDTH = 100      # 臉框寬度下限（像素）
MAX_YAW = 0.25            # 鼻子偏離雙眼中點，佔雙眼距離的比例上限（轉頭）
MAX_ROLL_DEGREES = 15.0   # 雙眼連線的傾斜角上限（歪頭）
PITCH_RANGE = (0.30, 0.70)  # 鼻子在眼睛到嘴巴之間的位置比例（仰頭或低頭會超出）

NO_FACE, MULTIPLE, VERIFYING = "no_face", "multiple", "verifying"
NOT_FRONTAL, ALIGNING = "not_frontal", "aligning"
FAILED, SUCCESS, NO_ADMIN = "failed", "success", "no_admin"


@dataclass
class LoginStep:
    status: str
    message: str
    progress: float = 0.0          # 0~1，連續通過的幀數比例
    admin_name: Optional[str] = None
    distance: Optional[float] = None
    pose: Optional[dict] = None    # 正面檢查的量測值（寬度、轉頭、歪頭、俯仰），方便現場調整門檻


def frontal_check(face):
    """依臉框與 5 個關鍵點判斷是不是正面；回傳 (是否正面, 提示文字, 量測值)。"""
    width = float(face["x2"]) - float(face["x1"])
    pose = {"width": round(width)}
    try:
        (lx, ly), (rx, ry) = face["left_eye"], face["right_eye"]
        nx, ny = face["nose"]
        (_, lly), (_, rly) = face["left_lip"], face["right_lip"]
    except (KeyError, TypeError, ValueError):
        return False, "請正對鏡頭", pose
    eye_dist = math.hypot(rx - lx, ry - ly)
    mouth_y, eye_y = (lly + rly) / 2, (ly + ry) / 2
    if eye_dist <= 0 or mouth_y <= eye_y:
        return False, "請正對鏡頭", pose
    roll = abs(math.degrees(math.atan2(ry - ly, rx - lx)))
    roll = 180 - roll if roll > 90 else roll
    yaw = abs(nx - (lx + rx) / 2) / eye_dist
    pitch = (ny - eye_y) / (mouth_y - eye_y)
    pose.update(yaw=round(float(yaw), 2), roll=round(float(roll), 1), pitch=round(float(pitch), 2))
    if width < MIN_FACE_WIDTH:
        return False, "請再靠近鏡頭一點", pose
    if roll > MAX_ROLL_DEGREES:
        return False, "請把頭擺正，不要歪頭", pose
    if yaw > MAX_YAW:
        return False, "請正面看向鏡頭，不要轉頭", pose
    if not PITCH_RANGE[0] <= pitch <= PITCH_RANGE[1]:
        return False, "請平視鏡頭，不要仰頭或低頭", pose
    return True, "", pose


class FaceLoginVerifier:
    def __init__(self, vision, recognize=None, required_frames=REQUIRED_FRAMES, fail_frames=FAIL_FRAMES,
                 liveness_min=LIVENESS_MIN, distance_max=DISTANCE_MAX, ready_frames=READY_FRAMES,
                 frame_distance_max=FRAME_DISTANCE_MAX):
        """vision 需提供 detect(frame)→(rgb, 偵測清單)、liveness(frame, box)→分數、embed(rgb, 偵測)→特徵。"""
        self.vision = vision
        self._recognize = recognize
        self.required, self.fail_frames = required_frames, fail_frames
        self.liveness_min, self.distance_max = liveness_min, distance_max
        self.ready_frames = ready_frames
        self.frame_distance_max = frame_distance_max
        self.reset()

    def reset(self):
        self._front_streak = 0
        self._ok_streak = 0
        self._dists = []
        self._bad_streak = 0
        self._who = None
        self._pose = None

    def _recognize_admin(self, embedding):
        if self._recognize is not None:
            return self._recognize(embedding, self.frame_distance_max)
        from shared import database_mgr
        return database_mgr.recognize_admin(embedding, self.frame_distance_max)

    def _bad(self, distance=None):
        self._ok_streak, self._who, self._dists = 0, None, []
        self._bad_streak += 1
        if self._bad_streak >= self.fail_frames:
            self._bad_streak = 0
            return LoginStep(FAILED, "驗證失敗，請對準鏡頭再試一次", distance=distance, pose=self._pose)
        return LoginStep(VERIFYING, "驗證中…", distance=distance, pose=self._pose)

    def step(self, frame) -> LoginStep:
        rgb, detections = self.vision.detect(frame)
        if len(detections) == 0:
            self.reset()
            return LoginStep(NO_FACE, "請站到鏡頭前")
        if len(detections) > 1:
            self.reset()
            return LoginStep(MULTIPLE, "畫面中只能有一個人")
        face = detections[0]
        frontal, hint, pose = frontal_check(face)
        if not frontal:
            self.reset()
            return LoginStep(NOT_FRONTAL, hint, pose=pose)
        self._pose = pose
        self._front_streak += 1
        if self._front_streak < self.ready_frames:       # 正面還沒穩定，先不比對
            return LoginStep(ALIGNING, "偵測到正面，請保持不動…", pose=pose)
        box = (int(face["x1"]), int(face["y1"]), int(face["x2"]), int(face["y2"]))
        live = self.vision.liveness(frame, box)
        if live == -1.0:           # 臉不完整或在邊緣：不算失敗，等下一幀
            self._ok_streak, self._who, self._front_streak, self._dists = 0, None, 0, []
            return LoginStep(NO_FACE, "請把臉完整放進畫面")
        if live < self.liveness_min:
            return self._bad()
        name, dist = self._recognize_admin(self.vision.embed(rgb, face))
        if name == "No Admin":
            self.reset()
            return LoginStep(NO_ADMIN, "目前沒有任何管理者")
        if name == "Unknown":
            return self._bad(dist)
        if self._who not in (None, name):   # 中途換成另一位管理者：重新累計
            self._ok_streak, self._dists = 0, []
        self._who = name
        self._bad_streak = 0
        self._ok_streak += 1
        self._dists.append(dist)
        if self._ok_streak >= self.required:
            mean = round(sum(self._dists) / len(self._dists), 3)
            self.reset()
            if mean >= self.distance_max:        # 每一幀都勉強過，但平均不夠像：算一次失敗
                return LoginStep(FAILED, "驗證失敗，請對準鏡頭再試一次", distance=mean, pose=pose)
            return LoginStep(SUCCESS, f"歡迎，{name}", progress=1.0, admin_name=name, distance=mean, pose=pose)
        return LoginStep(VERIFYING, "驗證中…", progress=self._ok_streak / self.required, distance=dist, pose=pose)


class RealVision:
    """正式環境的視覺函式（載入 vision_core 才會用到 GPU 模型）。"""

    def __init__(self):
        from access import vision_core
        self.vc = vision_core

    def detect(self, frame):
        return self.vc.face_detect_bgr(frame, self.vc.detector)

    def liveness(self, frame, box):
        return float(self.vc.check_liveness(frame, box))

    def close(self):
        self.vc.close_vision()

    def embed(self, rgb, face):
        landmarks = [face["left_eye"], face["right_eye"], face["nose"], face["left_lip"], face["right_lip"]]
        return self.vc.feature_extract(self.vc.face_align(rgb, landmarks))

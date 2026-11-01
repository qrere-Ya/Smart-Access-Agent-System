"""
face_login.py：管理者人臉登入的判斷邏輯（逐幀判斷，不含畫面；視覺函式可由外部換成假的，方便測試）。

主要功能：
  - FaceLoginVerifier.step(frame)：每個影格回傳一個 LoginStep（狀態、提示文字、進度、通過時的管理者姓名）
  - 通過條件：連續 REQUIRED_FRAMES 幀都「只有一張臉、活體分數達標、比對到同一位管理者且距離小於門檻」
  - 失敗判定：有臉但連續 FAIL_FRAMES 幀不是活體或不是管理者，算一次失敗（交給 LoginGuard 計數）；
    沒有臉或多張臉不算失敗，只是把連續成功的計數歸零
  - 只回傳「驗證失敗」類的提示，不透露是活體不過還是比對不過

串接模組：
  - vision_core：真正的人臉偵測、對齊、特徵、活體（default_vision 才會載入，單元測試不需要）
  - database_mgr.recognize_admin：只和啟用中的管理者比對
  - login_window：每個影格呼叫 step，並依結果更新畫面與失敗計數
"""
from dataclasses import dataclass
from typing import Optional

REQUIRED_FRAMES = 5
FAIL_FRAMES = 8
LIVENESS_MIN = 0.90
DISTANCE_MAX = 0.8

NO_FACE, MULTIPLE, VERIFYING = "no_face", "multiple", "verifying"
FAILED, SUCCESS, NO_ADMIN = "failed", "success", "no_admin"


@dataclass
class LoginStep:
    status: str
    message: str
    progress: float = 0.0          # 0~1，連續通過的幀數比例
    admin_name: Optional[str] = None
    distance: Optional[float] = None


class FaceLoginVerifier:
    def __init__(self, vision, recognize=None, required_frames=REQUIRED_FRAMES, fail_frames=FAIL_FRAMES,
                 liveness_min=LIVENESS_MIN, distance_max=DISTANCE_MAX):
        """vision 需提供 detect(frame)→(rgb, 偵測清單)、liveness(frame, box)→分數、embed(rgb, 偵測)→特徵。"""
        self.vision = vision
        self._recognize = recognize
        self.required, self.fail_frames = required_frames, fail_frames
        self.liveness_min, self.distance_max = liveness_min, distance_max
        self.reset()

    def reset(self):
        self._ok_streak = 0
        self._bad_streak = 0
        self._who = None

    def _recognize_admin(self, embedding):
        if self._recognize is not None:
            return self._recognize(embedding, self.distance_max)
        from shared import database_mgr
        return database_mgr.recognize_admin(embedding, self.distance_max)

    def _bad(self, distance=None):
        self._ok_streak, self._who = 0, None
        self._bad_streak += 1
        if self._bad_streak >= self.fail_frames:
            self._bad_streak = 0
            return LoginStep(FAILED, "驗證失敗，請對準鏡頭再試一次", distance=distance)
        return LoginStep(VERIFYING, "驗證中…", distance=distance)

    def step(self, frame) -> LoginStep:
        rgb, detections = self.vision.detect(frame)
        if len(detections) == 0:
            self._ok_streak, self._who, self._bad_streak = 0, None, 0
            return LoginStep(NO_FACE, "請站到鏡頭前")
        if len(detections) > 1:
            self._ok_streak, self._who, self._bad_streak = 0, None, 0
            return LoginStep(MULTIPLE, "畫面中只能有一個人")
        face = detections[0]
        box = (int(face["x1"]), int(face["y1"]), int(face["x2"]), int(face["y2"]))
        live = self.vision.liveness(frame, box)
        if live == -1.0:           # 臉不完整或在邊緣：不算失敗，等下一幀
            self._ok_streak, self._who = 0, None
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
            self._ok_streak = 0
        self._who = name
        self._bad_streak = 0
        self._ok_streak += 1
        if self._ok_streak >= self.required:
            self.reset()
            return LoginStep(SUCCESS, f"歡迎，{name}", progress=1.0, admin_name=name, distance=dist)
        return LoginStep(VERIFYING, "驗證中…", progress=self._ok_streak / self.required, distance=dist)


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

"""
terminal_backend.py：門禁終端機的「資料後端」——辨識身分、打卡、記錄異常事件的兩種實作，終端機只依賴這個介面。

主要功能：
  - LocalBackend（單機，預設）：包住原本的資料庫呼叫（recognize_face、log_attendance、record_anomaly_event、行事曆），
    行為與改版前相同
  - FrontBackend（前台角色）：辨識用前台本機的人臉快取（FaceIndex，後台離線也能辨識）；打卡與異常事件先寫進磁碟佇列，
    由前台代理補送給後台。上班或下班由後台判斷，前台不知道結果，所以只顯示「打卡成功」；
    只擋掉一定不會被記錄的時段（太早、午休）；不播報行事曆（行事曆在後台）
  - 異常事件（尾隨、經過未打卡）前台每種每 ANOMALY_MERGE_SECONDS 秒最多送一筆，避免每幀都送一張照片
  - PunchOutcome：這次打卡要顯示什麼（左側文字、顏色、是否成功、要播的語音、問候語）
  - create_backend：依角色設定選實作；greeting_for：依時段回傳早安、午安、晚安
  - 前台的限制：打卡成功只代表事件已進佇列；後台若判定不計入（例如早上第二次刷臉），畫面不會知道

串接模組：
  - terminal_app：每幀辨識、打卡、記錄事件、定期寫心跳；front_store：人臉快取、佇列與後台連線狀態；
    front_agent：寫心跳檔；database_mgr、security：單機版的資料庫與證據照片
"""
import datetime
import time
from dataclasses import dataclass
from typing import Optional

from access import security as sec
from shared import database_mgr as db

UNKNOWN_NAMES = ("Unknown", "Empty DB")
GREEN, CYAN, GRAY, WHITE = (0, 255, 0), (0, 255, 255), (200, 200, 200), (255, 255, 255)
INDEX_REFRESH_SECONDS = 5.0
ALERT_THROTTLE_SECONDS = 3.0


def greeting_for(hour):
    """12 點前早安、18 點前午安、其餘晚安。"""
    if hour < 12:
        return "早安"
    return "午安" if hour < 18 else "晚安"


@dataclass
class Recognition:
    name: str
    distance: float
    user_id: Optional[int] = None
    employee_id: str = "N/A"

    @property
    def known(self):
        return self.name not in UNKNOWN_NAMES


@dataclass
class PunchOutcome:
    text: str                       # 左側面板顯示的文字
    color: tuple                    # BGR
    success: bool = False           # True：畫面中間顯示「打卡成功　姓名　問候」的黑框
    greeting: str = ""
    audio: Optional[str] = None     # 要播報的語音（沒有就不播）
    calendar: Optional[str] = None  # 要播報的行事曆（只有單機版會有）
    voice_key: str = ""             # 語音去重用的識別（空白就用 text）；同一個 key 12 小時內只播一次


class LocalBackend:
    """單機：直接讀寫本機資料庫。"""
    mode = "local"

    def start(self):
        db.init_db()

    def recognize(self, embedding):
        name, distance = db.recognize_face(embedding)
        employee_id = db.get_employee_id(name) if name not in UNKNOWN_NAMES else "N/A"
        return Recognition(name, distance, None, employee_id)

    def punch(self, recognition, now=None):
        now = now or datetime.datetime.now()
        status = db.log_attendance(recognition.name, now=now)
        hello = greeting_for(now.hour)
        speech = f"{recognition.name}，{hello}"
        if "In:" in status:
            calendar = None
            event = db.get_today_event(db.get_employee_id(recognition.name))   # 上班打卡後播報今日行事曆
            if event:
                calendar = f"{recognition.name}，{event[0]}，{event[1]}"
            return PunchOutcome("上班報到成功", GREEN, True, hello, speech, calendar)
        if "Out:" in status:
            return PunchOutcome("下班打卡成功", CYAN, True, hello, speech)
        return PunchOutcome(status, WHITE)

    def anomaly(self, kind, detail, frame, box):
        db.record_anomaly_event(kind, detail, make_evidence=lambda: sec.generate_evidence(frame, box, prefix=kind))

    def note(self):
        return ""

    def alert(self, kind, detail=""):
        pass                                   # 單機沒有第二個螢幕，警示只在辨識畫面上

    def heartbeat(self, camera_ok, running=True):
        pass

    def close(self):
        pass


class FrontBackend:
    """前台：本機快取辨識、事件進佇列。store 是 FrontStore（和前台代理共用同一個檔）。"""
    mode = "front"

    def __init__(self, store, now=datetime.datetime.now, clock=time.time,
                 throttle_seconds=db.ANOMALY_MERGE_SECONDS, heartbeat=None):
        from access.front_store import FaceIndex, Outbox
        self.store, self._clock, self._throttle = store, clock, throttle_seconds
        self._face_index_class = FaceIndex
        self.outbox = Outbox(store, now)
        self._index, self._version, self._checked = FaceIndex(store.load_faces()), store.faces_version(), clock()
        self._last_sent = {}
        self._last_alert = {}
        self._heartbeat = heartbeat
        from access.front_store import ScreenState
        self.screen = ScreenState(store)

    def start(self):
        pass

    def _fresh_index(self):
        """代理同步到新的人臉後，辨識程式隔幾秒發現版本變了就重新載入。"""
        if self._clock() - self._checked >= INDEX_REFRESH_SECONDS:
            self._checked = self._clock()
            version = self.store.faces_version()
            if version != self._version:
                self._index, self._version = self._face_index_class(self.store.load_faces()), version
        return self._index

    def recognize(self, embedding):
        index = self._fresh_index()
        name, distance, user_id = index.recognize(embedding)
        employee_id = "N/A"
        if user_id is not None:
            employee_id = next((f["employee_id"] for f in index.faces if f["id"] == user_id), "N/A")
        return Recognition(name, distance, user_id, employee_id)

    def punch(self, recognition, now=None):
        moment = self.outbox.now()
        if not db.punch_window_open(moment.strftime("%H:%M:%S")):
            return PunchOutcome("非打卡時段", GRAY)
        if recognition.user_id is None:
            return PunchOutcome("無法打卡：沒有員工編號", WHITE)
        self.outbox.submit_punch(recognition.user_id)
        hello = greeting_for(moment.hour)
        self._screen_call(self.screen.note_punch, recognition.name, hello, clock_text=moment.strftime("%H:%M"))
        return PunchOutcome("打卡成功", GREEN, True, hello, f"{recognition.name}，{hello}",
                            voice_key=f"打卡成功_{hello}")

    def alert(self, kind, detail=""):
        """通知第二個螢幕的儀表板顯示警示（每種最多每 ALERT_THROTTLE_SECONDS 秒寫一次，不會每幀寫檔）。"""
        if self._clock() - self._last_alert.get(kind, -ALERT_THROTTLE_SECONDS) < ALERT_THROTTLE_SECONDS:
            return
        self._last_alert[kind] = self._clock()
        self._screen_call(self.screen.set_alert, kind, detail)

    @staticmethod
    def _screen_call(function, *args, **kwargs):
        """畫面用的狀態寫入失敗（檔案被鎖、磁碟問題）不能讓辨識與打卡中斷。"""
        try:
            function(*args, **kwargs)
        except Exception as e:  # noqa: BLE001
            print(f"[terminal_backend][WARN] 儀表板狀態寫入失敗：{type(e).__name__}")

    def anomaly(self, kind, detail, frame, box):
        self.alert(kind, detail)
        if self._clock() - self._last_sent.get(kind, -self._throttle) < self._throttle:
            return
        self._last_sent[kind] = self._clock()
        try:
            self.outbox.submit_event(kind, detail=detail, evidence=sec.encode_evidence_jpeg(frame, box))
        except Exception as e:  # noqa: BLE001  證據照片或寫入失敗不能讓辨識中斷
            print(f"[terminal_backend][WARN] 異常事件暫存失敗：{type(e).__name__}")

    def note(self):
        """畫面下方的提示：後台離線時說明事件已暫存。"""
        if self.store.link_online():
            return ""
        pending = self.store.pending_count()
        return f"後台離線，已暫存 {pending} 筆事件" if pending else "後台離線"

    def heartbeat(self, camera_ok, running=True):
        """寫心跳給前台代理（辨識是否在跑、鏡頭是否正常）。"""
        if self._heartbeat is None:
            from access.front_agent import write_heartbeat
            self._heartbeat = write_heartbeat
        try:
            self._heartbeat(running, camera_ok)
        except OSError:
            pass

    def close(self):
        self.heartbeat(False, running=False)


def create_backend(settings=None):
    """依角色選後端：前台用快取與佇列，其他（單機、後台）用本機資料庫。"""
    from shared import role_config
    settings = settings or role_config.load()
    if settings.role != role_config.FRONT:
        return LocalBackend()
    import os
    from access.front_store import FrontStore
    from shared import paths
    return FrontBackend(FrontStore(os.path.join(paths.RUNTIME_DIR, "front_agent.db")))

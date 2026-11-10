"""
pass_through.py：偵測「經過鏡頭但未打卡」的純邏輯判斷（不碰鏡頭與模型，可單元測試）。

主要功能：
  - 每幀輸入人物框、畫面寬度、是否為尾隨幀，輸出是否該記一筆 no_scan_walkby 異常事件
  - 規則：夠大的人框（寬度占畫面比例）連續存在超過 dwell_seconds，期間沒有任何一次臉孔比對成功，就觸發
  - 觸發後有冷卻（cooldown_seconds）；同一次停留（人未離開）最多觸發一次；人離開超過 gap_seconds 才算離開
  - 打卡（比對）成功的那一次停留不再觸發；該幀已被尾隨偵測記錄時，同一次停留也不再重複記

串接模組：
  - terminal_app：分析迴圈每幀呼叫 note_scan（比對成功時）與 update（取得是否觸發）
  - database_mgr：觸發後由 terminal_app 以 record_anomaly_event 寫入（沿用 30 秒合併與證據照）
"""

EVENT_TYPE = "no_scan_walkby"
EVENT_LABEL = "經過鏡頭未打卡"
EVENT_DETAIL = ""   # 事件名稱已說明一切；合併次數與時間由資料層自動附加


class PassThroughMonitor:
    """追蹤畫面中是否有人停留卻沒有打卡；單執行緒使用。"""

    def __init__(self, min_width_ratio=0.25, dwell_seconds=3.0, gap_seconds=1.5, cooldown_seconds=30.0):
        self.min_width_ratio = min_width_ratio    # 人框寬度占畫面寬度的最小比例，過小視為遠景路過者
        self.dwell_seconds = dwell_seconds        # 持續存在多久仍未打卡才觸發
        self.gap_seconds = gap_seconds            # 偵測短暫中斷（YOLO 閃爍）多久內仍視為同一個人
        self.cooldown_seconds = cooldown_seconds  # 觸發後多久內不再觸發
        self._present_since = None   # 本次停留開始時間
        self._last_seen = None       # 最後一次看到夠大人框的時間
        self._handled = False        # 本次停留已打卡、已觸發或已被尾隨偵測處理
        self._last_trigger = None

    def note_scan(self, now):
        """臉孔比對成功（含打卡冷卻中）時呼叫；本次停留不再觸發。"""
        if self._present_since is not None:
            self._handled = True
        else:
            # 比對成功當下尚未有人框（YOLO 慢一拍）：先開始一次已處理的停留
            self._present_since, self._last_seen, self._handled = now, now, True

    def _big_boxes(self, boxes, frame_width):
        """篩出寬度夠大的人框。"""
        need = frame_width * self.min_width_ratio
        return [b for b in boxes if (b[2] - b[0]) >= need]

    def update(self, now, person_boxes, frame_width, tailgate=False):
        """處理一幀；回傳 True 表示這幀該記錄 no_scan_walkby。tailgate 為這幀尾隨偵測已記錄。"""
        big = self._big_boxes(person_boxes, frame_width)
        if not big:
            if self._last_seen is not None and now - self._last_seen > self.gap_seconds:
                self._present_since = self._last_seen = None
                self._handled = False
            return False
        if self._present_since is None or (self._last_seen is not None and now - self._last_seen > self.gap_seconds):
            self._present_since, self._handled = now, False
        self._last_seen = now
        if tailgate:
            self._handled = True
        if self._handled or now - self._present_since < self.dwell_seconds:
            return False
        if self._last_trigger is not None and now - self._last_trigger < self.cooldown_seconds:
            return False
        self._handled = True
        self._last_trigger = now
        return True

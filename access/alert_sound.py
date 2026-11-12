"""
alert_sound.py：警示提示音——尾隨、非活體、經過鏡頭未打卡時，由辨識程式（terminal_app）在背景響一聲短提示音。

主要功能：
  - 三種警示用不同音型，一聽就分得出來：尾隨＝三聲急促高音、非活體＝兩聲低沉長音、經過未打卡＝單聲中音
  - 每種警示各自有冷卻（預設 20 秒），人站在門口時不會一直響；非活體每幀都會判定，冷卻會擋住連續響
  - 在背景執行緒播放，不會卡住辨識迴圈；沒有音效裝置、播放失敗只印一行警告，不影響辨識
  - 環境變數 SMART_ACCESS_ALERT_SOUND=0 可以關閉（預設開）；非 Windows 沒有 winsound 就什麼都不做
  - 純邏輯（冷卻、音型對應、開關）和實際發聲分開：發聲函式可替換，單元測試不會真的響

串接模組：
  - terminal_app：警示發生處呼叫 notify(kind)（前台與單機角色都用，後台角色不跑辨識）
  - 儀表板（front_dashboard）只顯示紅色橫幅，不播聲音
"""
import os
import threading
import time

ENV_SWITCH = "SMART_ACCESS_ALERT_SOUND"
COOLDOWN_SECONDS = 20.0

# 警示種類 → 音型：[(頻率 Hz, 長度 毫秒), ...]
PATTERNS = {
    "tailgate": [(1400, 140), (1400, 140), (1400, 140)],
    "spoof": [(500, 450), (380, 600)],
    "no_scan_walkby": [(900, 300)],
}
DEFAULT_PATTERN = [(1000, 250)]          # 沒對應到的警示種類


def enabled(environ=None):
    """環境變數 SMART_ACCESS_ALERT_SOUND 為 0／false／off／no 時關閉，其餘（含沒設）都開。"""
    value = (environ if environ is not None else os.environ).get(ENV_SWITCH, "1").strip().lower()
    return value not in ("0", "false", "off", "no")


def pattern_for(kind):
    """警示種類對應的音型；不認得的種類用預設單聲。"""
    return PATTERNS.get(kind, DEFAULT_PATTERN)


def _play_with_winsound(pattern):
    """用 Windows 內建 winsound.Beep 依序響出音型（會阻塞到響完，所以只在背景執行緒呼叫）。"""
    import winsound
    for frequency, milliseconds in pattern:
        winsound.Beep(frequency, milliseconds)


class AlertSound:
    """每種警示各自冷卻、在背景執行緒發聲；play 是發聲函式（測試時換成假的）。"""

    def __init__(self, play=_play_with_winsound, clock=time.monotonic, cooldown=COOLDOWN_SECONDS,
                 is_enabled=enabled, spawn=None):
        self._play, self._clock, self._cooldown, self._is_enabled = play, clock, cooldown, is_enabled
        self._spawn = spawn or self._start_thread
        self._last = {}
        self._lock = threading.Lock()

    @staticmethod
    def _start_thread(function):
        threading.Thread(target=function, daemon=True, name="alert-sound").start()

    def due(self, kind):
        """這種警示現在該不該響：開關開著，且距離上次響已超過冷卻（判斷通過就當場記下這次時間）。"""
        if not self._is_enabled():
            return False
        now = self._clock()
        with self._lock:
            last = self._last.get(kind)
            if last is not None and now - last < self._cooldown:
                return False
            self._last[kind] = now
            return True

    def notify(self, kind):
        """警示發生時呼叫：該響就在背景響一次，回傳有沒有排入發聲（不會丟例外、不會阻塞）。"""
        try:
            if not self.due(kind):
                return False
            pattern = pattern_for(kind)
            self._spawn(lambda: self._play_safely(pattern))
            return True
        except Exception as e:  # noqa: BLE001  警示聲音出任何事都不能影響辨識
            print(f"[alert_sound][WARN] 警示聲音排程失敗：{type(e).__name__}")
            return False

    def _play_safely(self, pattern):
        try:
            self._play(pattern)
        except Exception as e:  # noqa: BLE001  沒有音效裝置等
            print(f"[alert_sound][WARN] 警示聲音播放失敗：{type(e).__name__}")

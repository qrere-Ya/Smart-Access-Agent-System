"""
chat_log_maintenance.py：對話紀錄的每日清理——後台開啟時清一次，之後每天一次，刪除超過 30 天的紀錄。

主要功能：
  - ChatLogMaintenance.tick：後台每分鐘呼叫；同一天只清一次，清理在背景執行緒做，不卡住畫面
  - 清理前不會特別留下「標為錯誤或幻覺」的紀錄，所以要保留請先在報告頁的「對話紀錄」分頁匯出 CSV；
    清理的筆數與到期筆數會印在主控台，報告頁也會提示「超過 30 天的紀錄會被自動刪除」

串接模組：
  - shared.chat_log：purge_expired；backend_main：每分鐘 tick（和日報、假日資料更新同一個排程）
"""
import datetime
import threading

from shared import chat_log


class ChatLogMaintenance:
    def __init__(self, clock=datetime.datetime.now, purge=chat_log.purge_expired):
        self._clock, self._purge = clock, purge
        self._last_day = None
        self._busy = False
        self._lock = threading.Lock()

    def tick(self):
        """今天還沒清過就在背景清一次；回傳是否啟動了清理。"""
        with self._lock:
            today = self._clock().date()
            if self._busy or self._last_day == today:
                return False
            self._busy = True
            self._last_day = today          # 先標記：失敗也當天不再重試，不要每分鐘敲資料庫
        threading.Thread(target=self._work, daemon=True).start()
        return True

    def _work(self):
        try:
            removed = self._purge(now=self._clock())
            if removed:
                print(f"[對話紀錄] 已刪除 {removed} 筆超過 {chat_log.RETENTION_DAYS} 天的紀錄")
        except Exception as e:  # noqa: BLE001  背景執行緒不能讓例外往外丟
            print(f"[對話紀錄] 到期清理失敗（略過）：{type(e).__name__}")
        finally:
            with self._lock:
                self._busy = False

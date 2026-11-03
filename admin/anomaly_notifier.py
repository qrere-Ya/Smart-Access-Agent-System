"""
anomaly_notifier.py：異常事件通知的純邏輯（未讀數、新事件、已讀標記），不碰任何畫面元件。

主要功能：
  - 記住「已讀到的最大事件編號」，存成 runtime/anomaly_seen.json（先寫暫存檔再 os.replace，原子寫入）
  - poll()：查詢編號大於該值的異常事件，回傳未讀數、未讀清單，以及這個行程尚未提示過的新事件
  - mark_all_read()：把已讀編號推進到最近一次 poll 看到的最大編號（沒看過的事件不會被誤標已讀）
  - 第一次使用（沒有紀錄檔）時以目前最大編號為基準，不把歷史事件當成未讀
  - format_popup_text()：把多筆新事件合併成彈窗文字（純文字，沒裝畫面套件的環境也能測）
  - safe_evidence_path()：證據照片路徑必須在證據資料夾內；event_row()：異常事件列表的一列文字

串接模組：
  - database_mgr：list_anomaly_events 取得事件（只讀 id、timestamp、event_type、detail）
  - notification_bar：定時呼叫 poll() 並顯示紅點與彈窗
"""
import json
import os
from dataclasses import dataclass, field

from shared import database_mgr
from shared import event_labels
from shared import paths

FETCH_LIMIT = 200   # 單次最多撈幾筆；未讀達上限時，畫面的未讀數加「+」
MAX_LINES = 5       # 彈窗最多列出幾筆，其餘以「…另有 N 筆」帶過


def format_popup_text(events):
    """把多筆新事件合併成一段彈窗文字。"""
    lines = [f"{e.get('timestamp', '')}  {event_labels.describe(e.get('event_type', ''), e.get('detail'))}"
             for e in events[:MAX_LINES]]
    if len(events) > MAX_LINES:
        lines.append(f"…另有 {len(events) - MAX_LINES} 筆")
    return "\n".join(lines)


def safe_evidence_path(path, root=None):
    """
    證據照片的實際路徑。資料庫裡的路徑不可信（可能含 .. 或指到別處），必須落在證據資料夾內且是檔案，
    通過回傳絕對路徑，否則回傳 None。
    """
    if not path:
        return None
    folder = os.path.realpath(root or paths.ANOMALY_DIR)
    full = os.path.realpath(path if os.path.isabs(path) else os.path.join(folder, path))
    try:
        inside = os.path.commonpath([folder, full]) == folder
    except ValueError:                      # 不同磁碟機
        return None
    return full if inside and os.path.isfile(full) else None


def event_row(event, last_seen, evidence_root=None):
    """異常事件列表的一列：(狀態, 時間, 類型, 說明, 次數, 證據)；說明裡的換行與多餘空白壓成單行。"""
    unread = last_seen is not None and event["id"] > last_seen
    detail = " ".join(str(event.get("detail") or "").split())
    has_photo = safe_evidence_path(event.get("evidence_path"), evidence_root) is not None
    return ("● 未讀" if unread else "已讀", event.get("timestamp", ""), event_labels.label(event.get("event_type", "")),
            detail, event.get("event_count") or 1, "有" if has_photo else "無")


@dataclass
class PollResult:
    unread_count: int = 0
    unread: list = field(default_factory=list)   # 未讀事件，新的在前
    new: list = field(default_factory=list)      # 這個行程尚未提示過的事件，新的在前
    truncated: bool = False                      # 未讀數達上限，實際可能更多


class AnomalyNotifier:
    """追蹤異常事件的已讀進度。"""

    def __init__(self, state_path=None, fetch=None):
        self.state_path = state_path or paths.ANOMALY_SEEN_PATH
        self._fetch = fetch or database_mgr.list_anomaly_events
        self._last_polled_max = 0
        self._announced_max = None
        self.last_seen = self._load()

    def _load(self):
        """讀取已讀編號；檔案不存在或壞掉回傳 None。"""
        try:
            with open(self.state_path, encoding="utf-8") as f:
                value = json.load(f).get("last_seen_id")
            return value if isinstance(value, int) and value >= 0 else None
        except (OSError, ValueError, AttributeError):
            return None

    def _save(self, value):
        """原子寫入已讀編號；寫入失敗只印警告，不中斷輪詢。"""
        tmp = self.state_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"last_seen_id": value}, f)
            os.replace(tmp, self.state_path)
        except OSError as e:
            print(f"[Anomaly Notifier][WARN] 已讀進度寫入失敗: {e}")

    def poll(self):
        """查詢未讀異常事件；資料庫查詢失敗回傳 None，讓畫面維持原狀。"""
        try:
            events = self._fetch(limit=FETCH_LIMIT)
        except Exception as e:
            print(f"[Anomaly Notifier][WARN] 異常事件查詢失敗: {e}")
            return None
        max_id = max((e["id"] for e in events), default=0)
        if self.last_seen is None:          # 第一次使用：以現有最大編號為基準
            self.last_seen = max_id
            self._save(max_id)
        unread = sorted((e for e in events if e["id"] > self.last_seen), key=lambda e: e["id"], reverse=True)
        self._last_polled_max = max(self._last_polled_max, max_id)
        if self._announced_max is None:     # 本行程第一次輪詢：已存在的未讀不彈窗，只亮紅點
            self._announced_max = max_id
        new = [e for e in unread if e["id"] > self._announced_max]
        self._announced_max = max(self._announced_max, max_id)
        return PollResult(len(unread), unread, new, len(unread) >= FETCH_LIMIT)

    def mark_all_read(self):
        """把已讀進度推進到最近一次 poll 看到的最大編號。"""
        if self.last_seen is None or self._last_polled_max > self.last_seen:
            self.last_seen = self._last_polled_max
            self._save(self.last_seen)

"""
digest_store.py：每日出勤日報的存取與顯示文字（後台寫入、企業知識庫視窗讀取，不碰畫面元件）。

主要功能：
  - save／load：最新一份日報存成 runtime/daily_digest.json（先寫暫存檔再 os.replace，原子寫入），
    壞掉或欄位不對當作沒有；
    晨報同樣用 save_brief／load_brief 存成 runtime/morning_brief.json
  - load_time／save_time（日報，預設 18:00）、load_morning_time／save_morning_time（晨報，預設 08:30）：
    HH:MM，存在 runtime/digest_settings.json 的不同欄位，改一個不會洗掉另一個，格式不對退回預設
  - unread_anomaly_count：還沒標為已讀的異常事件數（讀後台的已讀進度檔，沒有進度檔就不算）
  - welcome_text：企業知識庫視窗開啟時顯示的文字（最新日報與待處理異常）；沒有日報回傳空字串

串接模組：
  - admin.daily_digest：產生日報並寫入；admin.digest_window：顯示與設定
  - rag.chat_window／chat_view：開窗時顯示 welcome_text；database_mgr：讀異常事件
"""
import datetime
import json
import os
import re
import threading
import time

from shared import database_mgr
from shared import paths

DEFAULT_TIME = "18:00"
DEFAULT_MORNING_TIME = "08:30"
MAX_AGE_DAYS = 7          # 超過這麼多天的日報不在開窗時顯示，避免把舊資料當成最新
_TIME_PATTERN = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_FIELDS = ("date", "generated_at", "facts")
_BRIEF_FIELDS = ("date", "generated_at")


REPLACE_TRIES = 6   # Windows 上目標檔剛好被別的行程或執行緒讀取時 os.replace 會暫時失敗，稍等再試


def _atomic_write(path, data):
    """先寫到每個寫入者各自的暫存檔再取代目標檔；暫存檔名不共用，同時寫入不會互相蓋掉。"""
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        for attempt in range(REPLACE_TRIES):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == REPLACE_TRIES - 1:
                    raise
                time.sleep(0.05 * (attempt + 1))
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def save(digest, path=None):
    """寫入最新日報；回傳是否成功（失敗只印警告，不讓呼叫端中斷）。"""
    try:
        _atomic_write(path or paths.DIGEST_PATH, digest)
        return True
    except OSError as e:
        print(f"[日報][WARN] 寫入失敗: {e}")
        return False


def _load_checked(path, fields):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not all(isinstance(data.get(k), str) and data[k] for k in fields):
        return None
    return data


def load(path=None):
    """讀取最新日報；沒有、壞掉或缺欄位回傳 None。"""
    return _load_checked(path or paths.DIGEST_PATH, _FIELDS)


def save_brief(brief, path=None):
    """寫入最新晨報；回傳是否成功。"""
    return save(brief, path or paths.BRIEF_PATH)


def load_brief(path=None):
    """讀取最新晨報；沒有、壞掉或缺欄位回傳 None。"""
    return _load_checked(path or paths.BRIEF_PATH, _BRIEF_FIELDS)


def valid_time(text) -> bool:
    return isinstance(text, str) and _TIME_PATTERN.match(text) is not None


def _settings(path):
    try:
        with open(path or paths.DIGEST_SETTINGS_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _load_time(key, default, path):
    value = _settings(path).get(key)
    return value if valid_time(value) else default


def _save_time(key, text, path):
    if not valid_time(text):
        raise ValueError("時間格式需為 HH:MM（24 小時制）")
    data = _settings(path)
    data[key] = text
    _atomic_write(path or paths.DIGEST_SETTINGS_PATH, data)


def load_time(path=None):
    """每天產生日報的時間（HH:MM）；沒設定或格式不對回傳預設。"""
    return _load_time("time", DEFAULT_TIME, path)


def save_time(text, path=None):
    """儲存日報產生時間；格式不對丟 ValueError。"""
    _save_time("time", text, path)


def load_morning_time(path=None):
    """每天產生晨報的時間（HH:MM）；沒設定或格式不對回傳預設。"""
    return _load_time("morning_time", DEFAULT_MORNING_TIME, path)


def save_morning_time(text, path=None):
    """儲存晨報產生時間；格式不對丟 ValueError。"""
    _save_time("morning_time", text, path)


def unread_anomaly_count(seen_path=None, fetch=None):
    """未讀異常事件數；沒有已讀進度檔（後台還沒用過）或查詢失敗回傳 0。"""
    try:
        with open(seen_path or paths.ANOMALY_SEEN_PATH, encoding="utf-8") as f:
            last_seen = json.load(f).get("last_seen_id")
        if not isinstance(last_seen, int):
            return 0
        events = (fetch or database_mgr.list_anomaly_events)(limit=200)
        return sum(1 for e in events if e["id"] > last_seen)
    except Exception:
        return 0


def welcome_text(today=None, path=None, unread=None):
    """開窗時顯示的文字：最新日報（摘要與明細）加待處理異常；沒有日報或已超過 MAX_AGE_DAYS 天回傳空字串。"""
    digest = load(path)
    if digest is None:
        return ""
    today = today or datetime.date.today()
    try:
        age = (today - datetime.date.fromisoformat(digest["date"])).days
    except ValueError:
        return ""
    if age > MAX_AGE_DAYS:
        return ""
    lines = [f"📰 最新出勤日報（資料日期 {digest['date']}，產生於 {digest['generated_at']}）"]
    if digest.get("summary"):
        lines.append(f"AI 摘要（數字與人名已核對）：{digest['summary']}")
    lines.append(digest["facts"])
    pending = unread_anomaly_count() if unread is None else unread
    if pending:
        lines.append(f"⚠️ 目前有 {pending} 筆異常事件尚未標為已讀，請到後台的異常事件查看。")
    return "\n".join(lines)

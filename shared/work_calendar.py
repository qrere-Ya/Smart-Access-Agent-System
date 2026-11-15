"""
work_calendar.py：工作日曆——哪一天不用上班（純邏輯，不碰畫面；網路抓取在 admin/calendar_fetch）。

主要功能：
  - day_off_reason：這天為什麼不用上班（國定假日名稱、補班日以外的週六日「週休（週六）」）；要上班回傳 None
    有那一年的快取就以資料為準（補班日的週六是上班日、平日的國定假日是休息日）；沒有快取就只算週六日
  - parse_year：驗證從第三方（ruyut/TaiwanCalendar）抓回來的一整年資料，任何一處不符就整份丟棄（回傳 None）
  - save_year／load_year：一年一檔的快取（runtime/work_calendar/年份.json，先寫暫存檔再 os.replace）；
    讀的時候重新驗證，壞掉的檔案當作沒有，不讓壞資料影響出勤判斷
  - source_text：給後台顯示「假日資料來源與最後更新時間」，沒有資料時明講目前只算週六日

串接模組：
  - attendance_report、daily_digest、morning_brief、rag.queries：用 day_off_reason 判斷要不要上班、算不算缺勤
  - admin.calendar_fetch：抓取並用 parse_year 驗證後 save_year
  - paths：WORK_CALENDAR_DIR
"""
import datetime
import json
import os
import re
import threading

from shared import paths

SOURCE_NAME = "ruyut/TaiwanCalendar（社群維護，非官方）"
MAX_NAME_LENGTH = 40                      # 假日名稱的長度上限，超過就是資料有問題
HOLIDAY_COUNT_RANGE = (90, 150)           # 一年的休息日總數（含週六日）；超出範圍代表資料不合理
_WEEKDAY_NAMES = ("週一", "週二", "週三", "週四", "週五", "週六", "週日")
_DATE_RE = re.compile(r"^\d{8}$")
_cache = {}
_lock = threading.Lock()


def _valid_date(text):
    """驗證 YYYY-MM-DD 並回傳 date；不合法丟 ValueError。"""
    try:
        return datetime.datetime.strptime(str(text).strip(), "%Y-%m-%d").date()
    except ValueError:
        raise ValueError("日期格式需為 YYYY-MM-DD") from None


def _days_in_year(year):
    return 366 if datetime.date(year, 12, 31).timetuple().tm_yday == 366 else 365


def parse_year(data, year):
    """
    驗證一整年的資料並整理成 {"YYYY-MM-DD": (是否休息, 名稱)}；不符合就回傳 None（整份丟棄，不部分採用）。
    要求：清單、筆數剛好等於當年天數、每筆日期是 8 位數字且屬於當年且不重複、isHoliday 一定是布林、
    description 是不超過上限的文字、休息日總數落在合理範圍。
    """
    if not isinstance(data, list) or len(data) != _days_in_year(year):
        return None
    days = {}
    for item in data:
        if not isinstance(item, dict):
            return None
        raw, flag, name = item.get("date"), item.get("isHoliday"), item.get("description", "")
        if not isinstance(raw, str) or not _DATE_RE.match(raw) or not isinstance(flag, bool):
            return None
        if not isinstance(name, str) or len(name) > MAX_NAME_LENGTH:
            return None
        try:
            day = datetime.datetime.strptime(raw, "%Y%m%d").date()
        except ValueError:
            return None
        key = day.isoformat()
        if day.year != year or key in days:
            return None
        days[key] = (flag, name.strip())
    if not HOLIDAY_COUNT_RANGE[0] <= sum(1 for flag, _ in days.values() if flag) <= HOLIDAY_COUNT_RANGE[1]:
        return None
    return days


def _path(year):
    return os.path.join(paths.WORK_CALENDAR_DIR, f"{int(year)}.json")


def save_year(year, data, fetched_at=None):
    """驗證後寫入快取；資料不合格就不寫（保留上一份可用的）並回傳 False。"""
    days = parse_year(data, year)
    if days is None:
        return False
    fetched_at = fetched_at or datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    path = _path(year)
    try:
        os.makedirs(paths.WORK_CALENDAR_DIR, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"year": year, "source": SOURCE_NAME, "fetched_at": fetched_at, "days": data}, f,
                      ensure_ascii=False)
        os.replace(tmp, path)
    except OSError as e:
        print(f"[工作日曆][WARN] 寫入快取失敗：{type(e).__name__}")
        return False
    with _lock:
        _cache.pop(path, None)
    return True


def load_year(year):
    """讀某一年的快取，回傳 (days, fetched_at)；沒有、壞掉或驗證不過回傳 (None, None)。"""
    path = _path(year)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None, None
    with _lock:
        hit = _cache.get(path)
        if hit and hit[0] == mtime:
            return hit[1], hit[2]
    try:
        with open(path, encoding="utf-8") as f:
            stored = json.load(f)
        days = parse_year(stored.get("days"), year) if isinstance(stored, dict) and stored.get("year") == year else None
        fetched_at = str(stored.get("fetched_at", ""))[:16] if days is not None else None
    except (OSError, ValueError, AttributeError):
        days, fetched_at = None, None
    with _lock:
        _cache[path] = (mtime, days, fetched_at)
    return days, fetched_at


def day_off_reason(date):
    """該日期不用上班的原因；要上班回傳 None。有那一年的快取以資料為準，否則只算週六日。"""
    day = _valid_date(date)
    days, _ = load_year(day.year)
    if days is not None:
        flag, name = days[day.isoformat()]
        if not flag:
            return None                    # 補班日：即使是週六也要上班
        if name:
            return name
    elif day.weekday() < 5:
        return None
    return f"週休（{_WEEKDAY_NAMES[day.weekday()]}）" if day.weekday() >= 5 else "假日"


def source_text(today=None):
    """後台顯示用：今年的假日資料來源與最後更新時間；沒有資料時明講目前只算週六日。"""
    today = today or datetime.date.today()
    days, fetched_at = load_year(today.year)
    if days is None:
        return f"假日資料：尚未取得 {today.year} 年的國定假日，目前只算週六、週日為休息日"
    return f"假日資料：{today.year} 年國定假日與補班日（來源 {SOURCE_NAME}，最後更新 {fetched_at}）"

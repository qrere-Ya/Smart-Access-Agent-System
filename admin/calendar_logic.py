"""
calendar_logic.py：假日、請假與每週休息日輸入畫面的純邏輯（輸入驗證與顯示格式），不碰任何畫面元件與資料庫。

主要功能：
  - parse_date：嚴格的 YYYY-MM-DD 日期驗證（含合理年份範圍）
  - clean_text：名稱與備註的長度與字元檢查（拒絕控制字元與雙向文字控制字元，換行與 Tab 轉成空白）
  - validate_holiday／validate_leave：整組輸入先驗證，不合法丟 ValueError（訊息可直接顯示給使用者）
  - leave_days、weekday_label、display_text、rest_flags、rest_days_from_flags：顯示與轉換用
  驗證通過的值才會交給 database_mgr；資料庫層另外還有自己的檢查。

串接模組：
  - calendar_panel：畫面呼叫這裡的函式驗證輸入
  - database_mgr：add_holiday、add_leave、set_rest_weekdays 接收驗證後的值
"""
import datetime
import re

MAX_HOLIDAY_NAME = 30
MAX_LEAVE_NOTE = 100
MAX_LEAVE_DAYS = 366          # 單筆請假最多跨幾天，避免打錯年份造成多年請假
MIN_YEAR, MAX_YEAR = 2000, 2100
LEAVE_KINDS = ("特休", "病假", "事假", "公假", "喪假", "婚假", "產假", "其他")
WEEKDAY_NAMES = ("週一", "週二", "週三", "週四", "週五", "週六", "週日")

_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}", re.ASCII)
# 雙向文字控制字元（可用來偽裝顯示順序）
_BIDI_CODES = (0x202A, 0x202B, 0x202C, 0x202D, 0x202E, 0x2066, 0x2067, 0x2068, 0x2069, 0x200E, 0x200F, 0x061C)
_BIDI_CONTROLS = {chr(c) for c in _BIDI_CODES}


def parse_date(text, label="日期"):
    """驗證 YYYY-MM-DD 並回傳標準字串；格式或範圍不合法丟 ValueError。"""
    value = str(text or "").strip()
    if not _DATE_RE.fullmatch(value):
        raise ValueError(f"{label}格式需為 YYYY-MM-DD（例如 2026-12-25）")
    try:
        day = datetime.datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        raise ValueError(f"{label}不是有效的日期") from None
    if not MIN_YEAR <= day.year <= MAX_YEAR:
        raise ValueError(f"{label}的年份需在 {MIN_YEAR} 到 {MAX_YEAR} 之間")
    return day.strftime("%Y-%m-%d")


def clean_text(text, max_len, label, required=False):
    """整理名稱或備註：換行與 Tab 轉空白、去頭尾空白；有控制字元或太長丟 ValueError。"""
    value = str(text or "").replace("\r", " ").replace("\n", " ").replace("\t", " ").strip()
    if any(ord(ch) < 32 or ord(ch) == 127 or ch in _BIDI_CONTROLS for ch in value):
        raise ValueError(f"{label}含有不能使用的特殊字元")
    if required and not value:
        raise ValueError(f"請輸入{label}")
    if len(value) > max_len:
        raise ValueError(f"{label}最多 {max_len} 個字（目前 {len(value)} 個字）")
    return value


def validate_holiday(date_text, name_text):
    """驗證假日輸入，回傳 (日期, 名稱)；名稱必填。"""
    return parse_date(date_text), clean_text(name_text, MAX_HOLIDAY_NAME, "假日名稱", required=True)


def validate_leave(user_id, start_text, end_text, kind, note_text, valid_user_ids):
    """驗證請假輸入，回傳 dict(user_id, start_date, end_date, kind, note)；員工必須在 valid_user_ids 內。"""
    if user_id is None or user_id not in set(valid_user_ids):
        raise ValueError("請先選擇員工")
    start, end = parse_date(start_text, "起始日期"), parse_date(end_text, "結束日期")
    if start > end:
        raise ValueError("起始日期不可晚於結束日期")
    if leave_days(start, end) > MAX_LEAVE_DAYS:
        raise ValueError(f"單筆請假最多 {MAX_LEAVE_DAYS} 天，請分成多筆")
    if kind not in LEAVE_KINDS:
        raise ValueError("請選擇假別")
    return {"user_id": user_id, "start_date": start, "end_date": end, "kind": kind,
            "note": clean_text(note_text, MAX_LEAVE_NOTE, "備註")}


def display_text(text, limit=60):
    """表格顯示用：控制字元與換行換成空白、超過 limit 字加「…」，避免舊資料把表格撐壞或藏字元。"""
    value = "".join(" " if (ord(ch) < 32 or ord(ch) == 127 or ch in _BIDI_CONTROLS) else ch
                    for ch in str(text or "")).strip()
    return value if len(value) <= limit else value[:limit - 1] + "…"


def leave_days(start_date, end_date):
    """請假涵蓋的日曆天數（起訖日皆含）。"""
    start = datetime.datetime.strptime(start_date, "%Y-%m-%d").date()
    end = datetime.datetime.strptime(end_date, "%Y-%m-%d").date()
    return (end - start).days + 1


def weekday_label(date_text):
    """日期對應的星期（週一…週日）；日期不合法回傳空字串。"""
    try:
        return WEEKDAY_NAMES[datetime.datetime.strptime(date_text, "%Y-%m-%d").weekday()]
    except (ValueError, TypeError):
        return ""


def rest_flags(days):
    """把休息日清單（0＝週一…6＝週日）轉成七個布林值。"""
    chosen = {d for d in days if isinstance(d, int) and 0 <= d <= 6}
    return [i in chosen for i in range(7)]


def rest_days_from_flags(flags):
    """把七個勾選狀態轉成休息日清單；長度不是 7 丟 ValueError。"""
    flags = list(flags)
    if len(flags) != 7:
        raise ValueError("每週休息日需要七個選項")
    return [i for i, on in enumerate(flags) if on]


def describe_rest_days(days):
    """休息日清單的中文說明，例如「週六、週日」；沒有休息日回傳「無（每天都要上班）」。"""
    names = [WEEKDAY_NAMES[i] for i in sorted(set(days)) if 0 <= i <= 6]
    return "、".join(names) if names else "無（每天都要上班）"

"""
dates.py：從問題裡找出時間說法並換算成日期區間（確定性解析，不交給 LLM 算日期）。

主要功能：
  - 相對說法：今天、昨天、前天、本週、上週、本月、上個月、今年、去年、最近 N 天、最近一週／一個月
  - 絕對說法：2026-10-13、2026/10/13、2026年10月13日、民國 115 年 10 月 13 日、10月13日、10/13、10月
  - 回傳 (開始日, 結束日, 原文說法)，日期格式 YYYY-MM-DD，結束日含當天；找不到時間說法回傳 None
  - 一句話裡有多個說法時取最前面的一個

串接模組：
  - answer：先用這裡解析時間，再呼叫 queries 的固定查詢函式
"""
import calendar
import datetime
import re
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

_CN = {"零": 0, "〇": 0, "一": 1, "二": 2, "兩": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_N = r"(?:\d+|[零〇一二兩三四五六七八九十]+)"


@dataclass
class DateRange:
    start: str
    end: str
    phrase: str


def _to_int(text: str) -> int:
    """阿拉伯或中文數字（到九十九）轉整數。"""
    if text.isdigit():
        return int(text)
    if "十" not in text:
        return _CN.get(text, 0)
    tens, _, ones = text.partition("十")
    return (_CN.get(tens, 1) if tens else 1) * 10 + (_CN.get(ones, 0) if ones else 0)


def _iso(day: datetime.date) -> str:
    return day.isoformat()


def _safe_date(year: int, month: int, day: int) -> Optional[datetime.date]:
    try:
        return datetime.date(year, month, day)
    except ValueError:
        return None


def _month_range(year: int, month: int) -> Tuple[datetime.date, datetime.date]:
    return datetime.date(year, month, 1), datetime.date(year, month, calendar.monthrange(year, month)[1])


def _week_range(day: datetime.date) -> Tuple[datetime.date, datetime.date]:
    monday = day - datetime.timedelta(days=day.weekday())
    return monday, monday + datetime.timedelta(days=6)


Resolver = Callable[[re.Match, datetime.date], Optional[Tuple[datetime.date, datetime.date]]]


def _full_date(m, today):
    d = _safe_date(int(m["y"]), int(m["m"]), int(m["d"]))
    return (d, d) if d else None


def _roc_date(m, today):
    year = int(m["y"])
    d = _safe_date(year + 1911 if year < 200 else year, int(m["m"]), int(m["d"]))
    return (d, d) if d else None


def _month_day(m, today):
    d = _safe_date(today.year, _to_int(m["m"]), _to_int(m["d"]))
    return (d, d) if d else None


def _month_only(m, today):
    month = _to_int(m["m"])
    return _month_range(today.year, month) if 1 <= month <= 12 else None


def _days_ago(offset):
    def resolve(m, today):
        d = today - datetime.timedelta(days=offset)
        return d, d
    return resolve


def _this_week(m, today):
    return _week_range(today)


def _last_week(m, today):
    return _week_range(today - datetime.timedelta(days=7))


def _this_month(m, today):
    return _month_range(today.year, today.month)


def _last_month(m, today):
    year, month = (today.year, today.month - 1) if today.month > 1 else (today.year - 1, 12)
    return _month_range(year, month)


def _this_year(m, today):
    return datetime.date(today.year, 1, 1), datetime.date(today.year, 12, 31)


def _last_year(m, today):
    return datetime.date(today.year - 1, 1, 1), datetime.date(today.year - 1, 12, 31)


def _recent_days(m, today):
    n = _to_int(m["n"])
    return (today - datetime.timedelta(days=n - 1), today) if n >= 1 else None


def _recent_weeks(m, today):
    n = _to_int(m["n"] or "一")
    return (today - datetime.timedelta(days=7 * n - 1), today) if n >= 1 else None


def _recent_months(m, today):
    n = _to_int(m["n"] or "一")
    if n < 1:
        return None
    month_index = today.year * 12 + (today.month - 1) - n
    year, month = month_index // 12, month_index % 12 + 1
    start = datetime.date(year, month, min(today.day, calendar.monthrange(year, month)[1]))
    return start, today


# 由具體到籠統；同一位置多個都符合時，以先列出的為準
_PATTERNS: List[Tuple[re.Pattern, Resolver]] = [
    (re.compile(r"(?P<y>\d{4})\s*[-/.年]\s*(?P<m>\d{1,2})\s*[-/.月]\s*(?P<d>\d{1,2})\s*[日號]?"), _full_date),
    (re.compile(r"(?:民國)?\s*(?P<y>\d{2,3})\s*年\s*(?P<m>\d{1,2})\s*月\s*(?P<d>\d{1,2})\s*[日號]"), _roc_date),
    (re.compile(rf"(?P<m>{_N})\s*月\s*(?P<d>{_N})\s*[日號]"), _month_day),
    (re.compile(r"(?<![\d/])(?P<m>\d{1,2})\s*/\s*(?P<d>\d{1,2})(?![\d/])"), _month_day),
    (re.compile(rf"(?P<m>{_N})\s*月(?:份)?(?!\s*{_N}\s*[日號])"), _month_only),
    (re.compile(r"大前天"), _days_ago(3)),
    (re.compile(r"前天"), _days_ago(2)),
    (re.compile(r"昨天|昨日"), _days_ago(1)),
    (re.compile(r"今天|今日"), _days_ago(0)),
    (re.compile(r"上\s*個?\s*(?:星期|週|周|禮拜)"), _last_week),
    (re.compile(r"(?:本|這)\s*個?\s*(?:星期|週|周|禮拜)"), _this_week),
    (re.compile(r"上\s*個?\s*月"), _last_month),
    (re.compile(r"(?:本|這)\s*個?\s*月"), _this_month),
    (re.compile(r"去年"), _last_year),
    (re.compile(r"今年"), _this_year),
    (re.compile(rf"(?:最近|近|過去)\s*(?P<n>{_N})\s*天"), _recent_days),
    (re.compile(rf"(?:最近|近|過去)\s*(?P<n>{_N})?\s*個?\s*(?:星期|週|周)"), _recent_weeks),
    (re.compile(rf"(?:最近|近|過去)\s*(?P<n>{_N})?\s*個?\s*月"), _recent_months),
]


def find_range(text: str, today: Optional[datetime.date] = None) -> Optional[DateRange]:
    """找出問題裡最前面的時間說法並換算；找不到或日期不存在（如 2 月 30 日）回傳 None。"""
    today = today or datetime.date.today()
    best = None
    for pattern, resolve in _PATTERNS:
        for m in pattern.finditer(text or ""):
            if best is not None and m.start() >= best[0]:
                break
            span = resolve(m, today)
            if span:
                best = (m.start(), m.group(0).strip(), span)
                break
    if best is None:
        return None
    _, phrase, (start, end) = best
    return DateRange(_iso(start), _iso(end), phrase)

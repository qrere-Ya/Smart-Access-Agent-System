"""
dates 單元測試：相對時間說法（今天到去年、最近 N 天）、絕對日期的各種寫法、跨年與月底、不存在的日期、
一句話有多個說法取最前面、完全沒有時間說法。固定「今天」為 2026-10-17（星期六）。
"""
import datetime

import pytest

from rag import dates

TODAY = datetime.date(2026, 10, 17)


def span(text, today=TODAY):
    found = dates.find_range(text, today)
    return (found.start, found.end) if found else None


@pytest.mark.parametrize("text, expected", [
    ("今天誰遲到", ("2026-10-17", "2026-10-17")),
    ("昨天有打卡嗎", ("2026-10-16", "2026-10-16")),
    ("昨日", ("2026-10-16", "2026-10-16")),
    ("前天", ("2026-10-15", "2026-10-15")),
    ("大前天", ("2026-10-14", "2026-10-14")),
    ("本週遲到名單", ("2026-10-12", "2026-10-18")),
    ("這個星期", ("2026-10-12", "2026-10-18")),
    ("這周", ("2026-10-12", "2026-10-18")),
    ("上週的打卡", ("2026-10-05", "2026-10-11")),
    ("上個星期", ("2026-10-05", "2026-10-11")),
    ("上禮拜", ("2026-10-05", "2026-10-11")),
    ("這個月遲到幾次", ("2026-10-01", "2026-10-31")),
    ("本月", ("2026-10-01", "2026-10-31")),
    ("上個月", ("2026-09-01", "2026-09-30")),
    ("上月", ("2026-09-01", "2026-09-30")),
    ("今年", ("2026-01-01", "2026-12-31")),
    ("去年", ("2025-01-01", "2025-12-31")),
])
def test_relative_phrases(text, expected):
    assert span(text) == expected


@pytest.mark.parametrize("text, expected", [
    ("最近7天", ("2026-10-11", "2026-10-17")),
    ("近三天", ("2026-10-15", "2026-10-17")),
    ("過去十天", ("2026-10-08", "2026-10-17")),
    ("最近一週", ("2026-10-11", "2026-10-17")),
    ("最近兩週", ("2026-10-04", "2026-10-17")),
    ("最近一個月", ("2026-09-17", "2026-10-17")),
    ("近3個月", ("2026-07-17", "2026-10-17")),
])
def test_recent_periods(text, expected):
    assert span(text) == expected


@pytest.mark.parametrize("text, expected", [
    ("2026-10-13", ("2026-10-13", "2026-10-13")),
    ("2026/10/13", ("2026-10-13", "2026-10-13")),
    ("2026年10月13日", ("2026-10-13", "2026-10-13")),
    ("2026.10.3", ("2026-10-03", "2026-10-03")),
    ("民國115年10月13日", ("2026-10-13", "2026-10-13")),
    ("115年10月13號", ("2026-10-13", "2026-10-13")),
    ("10月13日", ("2026-10-13", "2026-10-13")),
    ("10月13號", ("2026-10-13", "2026-10-13")),
    ("10/13", ("2026-10-13", "2026-10-13")),
    ("十月十三日", ("2026-10-13", "2026-10-13")),
    ("10月的遲到", ("2026-10-01", "2026-10-31")),
    ("十月份", ("2026-10-01", "2026-10-31")),
    ("2月", ("2026-02-01", "2026-02-28")),
])
def test_absolute_dates(text, expected):
    assert span(text) == expected


def test_last_month_across_the_new_year():
    assert span("上個月", datetime.date(2026, 1, 15)) == ("2025-12-01", "2025-12-31")


def test_leap_year_february():
    assert span("2月", datetime.date(2028, 5, 1)) == ("2028-02-01", "2028-02-29")


def test_recent_month_at_the_end_of_a_long_month():
    assert span("最近一個月", datetime.date(2026, 3, 31)) == ("2026-02-28", "2026-03-31")


def test_nonexistent_dates_are_not_guessed():
    assert span("2月30日") is None
    assert span("13月") is None
    assert span("2026-02-30") is None


def test_text_without_a_time_phrase():
    assert span("王小明遲到過嗎") is None
    assert span("") is None and span(None) is None


def test_the_earliest_phrase_wins():
    assert span("昨天和上週") == ("2026-10-16", "2026-10-16")
    assert span("上週和昨天") == ("2026-10-05", "2026-10-11")
    assert span("10月13日到10月15日") == ("2026-10-13", "2026-10-13")


def test_phrase_is_returned_for_display():
    assert dates.find_range("王小明上週遲到幾次", TODAY).phrase == "上週"
    assert dates.find_range("民國115年10月13日", TODAY).phrase.endswith("13日")


def test_full_date_is_not_split_into_month_and_day():
    assert span("2025/10/13") == ("2025-10-13", "2025-10-13")


def test_defaults_to_the_real_today():
    found = dates.find_range("今天")
    assert found.start == found.end == datetime.date.today().isoformat()

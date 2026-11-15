"""
work_calendar 的單元測試：沒有快取只算週六日、有快取以資料為準（國定假日、補班日當上班日）、
第三方資料的嚴格驗證（格式、型別、筆數、長度，不符整份丟棄）、壞掉的快取不影響判斷、來源說明。
"""
import datetime
import json

import pytest

from shared import paths, work_calendar

YEAR = 2026
WED, SAT, SUN = "2026-10-14", "2026-10-17", "2026-10-18"


@pytest.fixture(autouse=True)
def _calendar_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "WORK_CALENDAR_DIR", str(tmp_path / "wc"))
    work_calendar._cache.clear()


def make_year(year=YEAR, overrides=None):
    """一整年的資料（格式同 ruyut/TaiwanCalendar）：週六日休息，overrides 是 {YYYYMMDD: (是否休息, 名稱)}。"""
    overrides = overrides or {}
    day, out = datetime.date(year, 1, 1), []
    while day.year == year:
        key = day.strftime("%Y%m%d")
        flag, name = overrides.get(key, (day.weekday() >= 5, ""))
        out.append({"date": key, "week": "x", "isHoliday": flag, "description": name})
        day += datetime.timedelta(days=1)
    return out


def test_without_a_cache_only_saturday_and_sunday_are_days_off():
    assert work_calendar.day_off_reason(SAT) == "週休（週六）"
    assert work_calendar.day_off_reason(SUN) == "週休（週日）"
    assert work_calendar.day_off_reason(WED) is None
    assert work_calendar.day_off_reason("2026-10-10") == "週休（週六）"      # 沒資料時國慶日不會被認出來


def test_a_bad_date_is_refused():
    with pytest.raises(ValueError):
        work_calendar.day_off_reason("2026/10/14")


def test_a_cached_national_holiday_on_a_weekday_is_a_day_off_and_a_makeup_saturday_is_a_workday():
    data = make_year(overrides={"20261014": (True, "測試連假"), "20261017": (False, "")})
    assert work_calendar.save_year(YEAR, data, fetched_at="2026-11-01 08:00")
    assert work_calendar.day_off_reason(WED) == "測試連假"
    assert work_calendar.day_off_reason(SAT) is None                 # 補班日的週六要上班
    assert work_calendar.day_off_reason(SUN) == "週休（週日）"
    assert work_calendar.day_off_reason("2026-10-15") is None


def test_a_holiday_without_a_name_still_has_a_reason():
    data = make_year(overrides={"20261014": (True, "")})
    work_calendar.save_year(YEAR, data)
    assert work_calendar.day_off_reason(WED) == "假日"


def test_a_year_without_a_cache_still_falls_back_when_another_year_is_cached():
    work_calendar.save_year(YEAR, make_year())
    assert work_calendar.day_off_reason("2027-01-02") == "週休（週六）"


@pytest.mark.parametrize("mutate", [
    lambda d: d[:-1],                                              # 筆數少一天
    lambda d: d + [d[0]],                                          # 筆數多一天
    lambda d: "not a list",
    lambda d: [dict(d[0], isHoliday="true")] + d[1:],              # isHoliday 不是布林
    lambda d: [dict(d[0], isHoliday=1)] + d[1:],
    lambda d: [dict(d[0], date="2026-01-01")] + d[1:],             # 日期格式不對
    lambda d: [dict(d[0], date="20260230")] + d[1:],               # 不存在的日期
    lambda d: [dict(d[0], description="x" * 100)] + d[1:],         # 名稱太長
    lambda d: [dict(d[0], description=5)] + d[1:],
    lambda d: d[:-1] + [d[0]],                                     # 筆數對，但有一天重複、少了年底最後一天
    lambda d: [5] + d[1:],
    lambda d: [dict(x, isHoliday=False) for x in d],               # 全年都上班，不合理
    lambda d: [dict(x, isHoliday=True) for x in d],                # 全年都休息，不合理
])
def test_bad_third_party_data_is_discarded_whole(mutate):
    assert work_calendar.parse_year(mutate(make_year()), YEAR) is None
    assert work_calendar.save_year(YEAR, mutate(make_year())) is False


def test_a_leap_year_needs_366_days():
    assert work_calendar.parse_year(make_year(2028), 2028) is not None
    assert work_calendar.parse_year(make_year(2028)[:-1], 2028) is None
    assert work_calendar.parse_year(make_year(2026) + [make_year(2028)[59]], 2026) is None


def test_data_for_another_year_is_refused():
    assert work_calendar.parse_year(make_year(2027), 2026) is None


def test_a_rejected_update_keeps_the_previous_good_cache():
    assert work_calendar.save_year(YEAR, make_year(overrides={"20261014": (True, "上一份")}))
    assert work_calendar.save_year(YEAR, make_year()[:-3]) is False
    assert work_calendar.day_off_reason(WED) == "上一份"


def test_a_corrupt_or_tampered_cache_file_is_ignored(tmp_path):
    work_calendar.save_year(YEAR, make_year(overrides={"20261014": (True, "連假")}))
    path = work_calendar._path(YEAR)
    with open(path, "w", encoding="utf-8") as f:
        f.write("{壞掉的 json")
    work_calendar._cache.clear()
    assert work_calendar.day_off_reason(WED) is None                  # 退回只算週六日
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"year": YEAR, "days": make_year()[:10]}, f)
    work_calendar._cache.clear()
    assert work_calendar.load_year(YEAR) == (None, None)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"year": 2030, "days": make_year()}, f)             # 檔案內的年份和檔名不一致
    work_calendar._cache.clear()
    assert work_calendar.load_year(YEAR) == (None, None)


def test_the_cache_is_reloaded_after_a_new_save():
    work_calendar.save_year(YEAR, make_year())
    assert work_calendar.day_off_reason(WED) is None
    work_calendar.save_year(YEAR, make_year(overrides={"20261014": (True, "新的連假")}))
    assert work_calendar.day_off_reason(WED) == "新的連假"


def test_source_text_says_what_data_is_in_use():
    today = datetime.date(2026, 11, 15)
    text = work_calendar.source_text(today)
    assert "尚未取得 2026 年" in text and "只算週六、週日" in text
    work_calendar.save_year(YEAR, make_year(), fetched_at="2026-11-01 08:00")
    text = work_calendar.source_text(today)
    assert "2026 年國定假日與補班日" in text and "最後更新 2026-11-01 08:00" in text and "非官方" in text

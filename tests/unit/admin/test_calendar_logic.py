"""test_calendar_logic.py：假日、請假與每週休息日輸入驗證的純邏輯測試（不需要顯示器與畫面套件）。"""
import pytest

from admin import account_admin
from admin import calendar_logic as cl
from shared import database_mgr


@pytest.mark.parametrize("text", ["2026-12-25", " 2026-12-25 ", "2000-01-01", "2100-12-31"])
def test_parse_date_accepts_valid_dates(text):
    assert cl.parse_date(text) == text.strip()


@pytest.mark.parametrize("text", ["", None, "2026-1-5", "2026/12/25", "20261225", "2026-13-01", "2026-02-30",
                                  "1999-12-31", "2101-01-01", "２０２６-１２-２５", "2026-12-25; DROP TABLE Users",
                                  "2026-12-\n25", "abcd-ef-gh"])
def test_parse_date_rejects_bad_input(text):
    with pytest.raises(ValueError):
        cl.parse_date(text)


def test_parse_date_error_names_the_field():
    with pytest.raises(ValueError, match="起始日期"):
        cl.parse_date("x", "起始日期")


def test_clean_text_strips_and_flattens_whitespace():
    assert cl.clean_text("  家人\n聚會\t日 ", 30, "備註") == "家人 聚會 日"
    assert cl.clean_text(None, 30, "備註") == ""


@pytest.mark.parametrize("bad", ["a\x00b", "a\x07b", "abc\x1b[31m", "x" + chr(0x202E) + "y", "x" + chr(0x2067) + "y"])
def test_clean_text_rejects_control_and_bidi_characters(bad):
    with pytest.raises(ValueError, match="特殊字元"):
        cl.clean_text(bad, 30, "備註")


def test_clean_text_enforces_length_and_required():
    assert cl.clean_text("字" * 30, 30, "名稱") == "字" * 30
    with pytest.raises(ValueError, match="最多 30"):
        cl.clean_text("字" * 31, 30, "名稱")
    with pytest.raises(ValueError, match="請輸入名稱"):
        cl.clean_text("   ", 30, "名稱", required=True)


def test_validate_holiday_returns_clean_values():
    assert cl.validate_holiday("2026-12-25", " 聖誕節 ") == ("2026-12-25", "聖誕節")


@pytest.mark.parametrize("date_text,name", [("2026-12-25", ""), ("bad", "節日"), ("2026-12-25", "名" * 31)])
def test_validate_holiday_rejects(date_text, name):
    with pytest.raises(ValueError):
        cl.validate_holiday(date_text, name)


def test_validate_leave_accepts_a_good_entry():
    got = cl.validate_leave(3, "2026-12-01", "2026-12-03", "特休", " 出國 ", [1, 2, 3])
    assert got == {"user_id": 3, "start_date": "2026-12-01", "end_date": "2026-12-03", "kind": "特休",
                   "note": "出國"}


@pytest.mark.parametrize("args,message", [
    ((None, "2026-12-01", "2026-12-03", "特休", ""), "選擇員工"),
    ((99, "2026-12-01", "2026-12-03", "特休", ""), "選擇員工"),
    ((3, "2026-12-04", "2026-12-03", "特休", ""), "不可晚於"),
    ((3, "2026-12-01", "2028-12-03", "特休", ""), "最多 366 天"),
    ((3, "2026-12-01", "2026-12-03", "任意文字", ""), "假別"),
    ((3, "2026-12-01", "2026-12-03", "", ""), "假別"),
    ((3, "2026-12-01", "2026-12-03", "特休", "x" * 101), "最多 100"),
    ((3, "bad", "2026-12-03", "特休", ""), "起始日期"),
    ((3, "2026-12-01", "bad", "特休", ""), "結束日期"),
])
def test_validate_leave_rejects(args, message):
    with pytest.raises(ValueError, match=message):
        cl.validate_leave(*args, [1, 2, 3])


def test_display_text_flattens_and_truncates():
    assert cl.display_text("a\nb\x00c" + chr(0x202E) + "d") == "a b c d"
    assert cl.display_text("字" * 100, limit=10) == "字" * 9 + "…"
    assert cl.display_text(None) == ""


def test_leave_days_counts_both_ends():
    assert cl.leave_days("2026-12-01", "2026-12-01") == 1
    assert cl.leave_days("2026-12-30", "2027-01-02") == 4


def test_weekday_label():
    assert cl.weekday_label("2026-11-02") == "週一"
    assert cl.weekday_label("2026-11-08") == "週日"
    assert cl.weekday_label("not a date") == "" and cl.weekday_label(None) == ""


def test_rest_flags_round_trip():
    assert cl.rest_flags([5, 6]) == [False] * 5 + [True, True]
    assert cl.rest_days_from_flags(cl.rest_flags([0, 3])) == [0, 3]
    assert cl.rest_days_from_flags([False] * 7) == []
    assert cl.rest_flags([9, -1, "x"]) == [False] * 7


def test_rest_days_from_flags_requires_seven_options():
    with pytest.raises(ValueError):
        cl.rest_days_from_flags([True] * 6)


def test_describe_rest_days():
    assert cl.describe_rest_days([6, 5]) == "週六、週日"
    assert cl.describe_rest_days([]) == "無（每天都要上班）"


def test_weekday_names_match_the_database_layer():
    assert cl.WEEKDAY_NAMES == database_mgr._WEEKDAY_NAMES


def test_validated_values_are_accepted_by_the_database(db, add_user):
    add_user("員工甲", "E1")
    user_id = account_admin.list_users()[0]["id"]
    clean = cl.validate_leave(user_id, "2026-12-01", "2026-12-02", "病假", "感冒", [user_id])
    database_mgr.add_leave(**clean)
    day, name = cl.validate_holiday("2026-12-25", "聖誕節")
    database_mgr.add_holiday(day, name)
    assert database_mgr.list_leaves()[0]["note"] == "感冒"
    assert database_mgr.list_holidays()[0] == {"date": "2026-12-25", "name": "聖誕節"}

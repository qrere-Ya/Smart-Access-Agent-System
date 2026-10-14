"""
fuzzy_match 單元測試：在長文字中以滑動視窗找出最接近的片段，回傳最小編輯距離。
"""
import pytest

from rag.fuzzy_match import min_edit_distance_in_text


@pytest.mark.parametrize("target, text, expected", [
    ("打卡紀錄", "請問打卡紀錄是什麼", 0),
    ("打卡紀錄", "請問打考紀錄是什麼", 1),      # 替換一個字
    ("打卡紀錄", "請問打卡紀是什麼", 1),        # 少一個字
    ("打卡紀錄", "請問打卡的紀錄是什麼", 1),     # 多一個字
    ("陳柏豫", "陳柏諭有無遲到", 1),
    ("陳柏豫", "今天天氣很好", 3),
])
def test_distance_for_common_typos(target, text, expected):
    assert min_edit_distance_in_text(target, text) == expected


def test_text_too_short_for_any_window_returns_none():
    assert min_edit_distance_in_text("打卡紀錄", "打卡") is None
    assert min_edit_distance_in_text("打卡紀錄", "") is None


def test_text_as_short_as_the_smallest_window_is_compared():
    assert min_edit_distance_in_text("打卡紀錄", "打卡紀") == 1


def test_the_best_window_anywhere_in_the_text_wins():
    assert min_edit_distance_in_text("特休", "我想知道公司的特休怎麼算") == 0

"""
entity_resolver 單元測試：完全一致、分數門檻（直接採用／請使用者確認／查無此人）、第二名太接近要確認、
從整句問題找人（完整姓名、容錯、多人、沒提到人）、名單更新。向量用查表的假資料，分數可精準控制。
"""
import numpy as np

from rag.entity_resolver import NameResolver

AXIS_X = np.array([1.0, 0.0, 0.0])
AXIS_Y = np.array([0.0, 1.0, 0.0])
OTHER = np.array([0.0, 0.0, 1.0])


def vec(cos_to_x):
    """與 X 軸夾角餘弦為 cos_to_x 的單位向量，且與 Y 軸垂直（不會意外靠近第二個人）。"""
    return np.array([cos_to_x, 0.0, (1 - cos_to_x ** 2) ** 0.5])


def make_embed(table):
    def embed(texts):
        return np.array([table.get(t, OTHER) for t in texts])
    return embed


def two_people(extra):
    """王小明在 X 軸、李大華在 Y 軸，再加上要測的輸入。"""
    return NameResolver(["王小明", "李大華"], make_embed({"王小明": AXIS_X, "李大華": AXIS_Y} | extra))


def test_exact_name_is_used_as_is():
    r = two_people({}).resolve("王小明")
    assert (r.status, r.name, r.resolved) == ("exact", "王小明", True)


def test_blank_or_empty_input_is_unknown():
    r = two_people({})
    assert r.resolve("").status == r.resolve("  ").status == "unknown"
    assert NameResolver([], make_embed({})).resolve("王小明").status == "unknown"


def test_high_score_with_clear_lead_is_accepted():
    result = two_people({"王小民": vec(0.9)}).resolve("王小民")
    assert (result.status, result.name) == ("confident", "王小明") and result.resolved


def test_middle_score_asks_for_confirmation():
    result = two_people({"葉軍偉": vec(0.6)}).resolve("葉軍偉")
    assert result.status == "ambiguous" and not result.resolved and result.name == ""
    assert result.candidates[0][0] == "王小明"


def test_two_close_candidates_ask_instead_of_guessing():
    r = NameResolver(["王小明", "王小美"], make_embed({
        "王小明": AXIS_X, "王小美": np.array([0.97, 0.243, 0.0]), "王小民": AXIS_X}))
    result = r.resolve("王小民")
    assert result.status == "ambiguous"
    assert {n for n, _ in result.candidates} == {"王小明", "王小美"}


def test_low_score_is_unknown_not_the_closest_person():
    result = two_people({"張三": vec(0.4)}).resolve("張三")
    assert (result.status, result.name, result.candidates) == ("unknown", "", [])


def test_edit_distance_alone_can_support_a_match():
    r = NameResolver(["abcdefg"], make_embed({"abcdefg": AXIS_X}))
    assert r.resolve("abcdefh").status == "confident"  # 錯一個字母，編輯相似度約 0.857


def test_candidates_are_limited_to_three():
    names = ["甲一", "甲二", "甲三", "甲四", "甲五"]
    r = NameResolver(names, make_embed(dict.fromkeys(names, AXIS_X) | {"甲零": AXIS_X}))
    assert len(r.resolve("甲零").candidates) == 3


# --- 從整句問題找人 ---

def test_full_name_inside_a_question():
    r = two_people({}).find_in_message("王小明昨天有遲到嗎")
    assert (r.status, r.name) == ("exact", "王小明")


def test_longest_name_wins_when_one_contains_another():
    r = NameResolver(["小明", "王小明"], make_embed({})).find_in_message("查一下王小明的紀錄")
    assert (r.status, r.name) == ("exact", "王小明")


def test_two_people_in_one_question_is_reported_as_multiple():
    r = two_people({}).find_in_message("王小明跟李大華誰比較常遲到")
    assert r.status == "multiple" and {n for n, _ in r.candidates} == {"王小明", "李大華"}


def test_typo_inside_a_question_is_matched_by_a_window():
    r = two_people({"王小民": vec(0.9)}).find_in_message("王小民這個月遲到幾次")
    assert (r.status, r.name) == ("confident", "王小明")


def test_question_without_a_person_is_unknown():
    assert two_people({}).find_in_message("今天誰遲到").status == "unknown"


def test_message_shorter_than_any_window_is_unknown():
    assert two_people({}).find_in_message("嗎").status == "unknown"


# --- 名單更新 ---

def test_new_people_can_be_found_after_the_roster_is_updated():
    r = NameResolver(["王小明"], make_embed({"王小明": AXIS_X, "李大華": AXIS_Y}))
    assert r.resolve("李大華").status == "unknown"
    r.update_names(["王小明", "李大華"])
    assert r.resolve("李大華").status == "exact"


def test_unchanged_roster_is_not_embedded_again():
    calls = []

    def counting(texts):
        calls.append(list(texts))
        return np.array([AXIS_X for _ in texts])

    r = NameResolver(["王小明"], counting)
    r.update_names(["王小明"])
    r.update_names(["王小明", " ", ""])
    assert len(calls) == 1

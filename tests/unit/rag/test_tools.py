"""
tools 單元測試：工具清單與路由白名單一致、參數檢查、請假與假日兩個新工具（區間重疊、單一員工、
不含備註與已刪除員工、預設時間）、經 Assistant 問「誰請假」「有哪些假日」走完整流程。
"""
import datetime
import json
import sqlite3
import zlib
from contextlib import closing

import numpy as np
import pytest

from rag import answer, queries, router, tools
from rag.entity_resolver import NameResolver
from shared import database_mgr as dbm

TODAY = "2026-10-17"
DAY = datetime.date(2026, 10, 17)


class Delta:
    def __init__(self, text):
        self.delta = text


class FakeLLM:
    """意圖判斷（complete）依問題回固定的 JSON；回答（stream_complete）回固定文字。"""

    def __init__(self, route, reply="好的。"):
        self.route, self.reply, self.answer_prompts = route, reply, []

    def complete(self, prompt):
        question = prompt.rsplit("問題：", 1)[1].strip()
        fields = {"docs": False, "data": "none", "person": "", "status": "", "event_type": ""}
        fields.update(self.route.get(question, {}))
        return json.dumps(fields, ensure_ascii=False)

    def stream_complete(self, prompt):
        self.answer_prompts.append(prompt)
        yield Delta(self.reply)


class NoDocs:
    """這些題目都不查文件，文件庫與重排只要能被呼叫即可。"""

    def search(self, question, n):
        return []

    def titles(self):
        return []

    def rerank(self, question, candidates, k):
        return []


def one_hot(texts, size=4096):
    out = np.zeros((len(texts), size), dtype=np.float32)
    for row, text in enumerate(texts):
        out[row, zlib.crc32(text.encode()) % size] = 1.0
    return out


@pytest.fixture
def store():
    return NoDocs()


def make(store, llm):
    resolver = NameResolver(["王小明", "李大華"], one_hot)
    return answer.Assistant(store, store, llm, resolver, today=lambda: DAY)


def ask(assistant, question):
    return "".join(assistant.ask(question))


def args(**kw):
    return tools.ToolArgs(today=TODAY, **kw)


@pytest.fixture
def staff(db, add_user):
    add_user("王小明", "E1")
    add_user("李大華", "E2")
    with closing(sqlite3.connect(db)) as con:
        ids = {n: con.execute("SELECT id FROM Users WHERE name=?", (n,)).fetchone()[0] for n in ("王小明", "李大華")}
    dbm.add_leave(ids["王小明"], "2026-10-15", "2026-10-19", "病假", "診斷：很私人的備註")
    dbm.add_leave(ids["李大華"], "2026-11-02", "2026-11-03", "事假", "")
    dbm.add_holiday("2026-10-25", "光復節補假")
    dbm.add_holiday("2027-01-01", "元旦")
    return ids


# --- 清單與驗證 ---

def test_router_whitelist_is_exactly_the_tool_registry():
    assert router.DATA_TOOLS == tuple(tools.TOOLS)
    assert {"leaves", "holidays"} <= set(tools.TOOLS)


def test_unknown_tool_and_missing_person_are_refused_before_any_query():
    with pytest.raises(ValueError, match="沒有這個工具"):
        tools.run("drop_table", args())
    with pytest.raises(ValueError, match="需要指定員工"):
        tools.run("records", args())


# --- 請假 ---

def test_leaves_overlapping_the_range_are_listed_without_notes(staff):
    rows = queries.leaves("2026-10-18", "2026-10-18")
    assert rows == [{"name": "王小明", "start_date": "2026-10-15", "end_date": "2026-10-19", "kind": "病假"}]
    assert all("note" not in r for r in rows)
    assert queries.leaves("2026-10-20", "2026-10-31") == []


def test_leaves_can_be_limited_to_one_person(staff):
    rows = queries.leaves("2026-10-01", "2026-12-31", "李大華")
    assert [r["name"] for r in rows] == ["李大華"]


def test_a_deleted_employees_leave_is_not_listed(staff, db):
    with closing(sqlite3.connect(db)) as con:
        con.execute("DELETE FROM Users WHERE name='李大華'")
        con.commit()
    assert [r["name"] for r in queries.leaves("2026-10-01", "2026-12-31")] == ["王小明"]


def test_leaves_tool_defaults_to_today_and_never_prints_the_note(staff):
    result = tools.run("leaves", args())
    assert "王小明" in result.text and "病假" in result.text and "很私人" not in result.text
    assert "李大華" not in result.text


def test_leaves_tool_with_a_range_and_a_person(staff):
    result = tools.run("leaves", args(start="2026-11-01", end="2026-11-30", ranged=True, person="李大華"))
    assert "李大華　2026-11-02 至 2026-11-03　事假" in result.text and "王小明" not in result.text


def test_leaves_with_nobody_says_so(staff):
    assert "（沒有符合的紀錄）" in tools.run("leaves", args(start="2027-05-01", end="2027-05-02", ranged=True)).text


def test_backwards_range_is_a_query_error(staff):
    with pytest.raises(queries.QueryError):
        queries.leaves("2026-10-10", "2026-10-01")
    with pytest.raises(queries.QueryError):
        queries.holidays("2026-10-10", "bad")


# --- 假日 ---

def test_holidays_tool_defaults_to_the_next_year_and_lists_rest_days(staff):
    result = tools.run("holidays", args())
    assert "2026-10-25　光復節補假" in result.text and "2027-01-01　元旦" in result.text
    assert "每週固定休息日：週六、週日" in result.text
    assert any("今天起一年內" in n for n in result.notes)


def test_holidays_tool_respects_the_range(staff):
    result = tools.run("holidays", args(start="2026-12-01", end="2027-01-31", ranged=True))
    assert "元旦" in result.text and "光復節" not in result.text and result.notes == []


# --- 經 Assistant 的完整流程 ---

def test_who_is_on_leave_goes_through_the_whole_path(store, staff):
    llm = FakeLLM({"這週誰請假": {"data": "leaves"}}, reply="王小明請病假。")
    a = make(store, llm)
    text = ask(a, "這週誰請假")
    assert "王小明　2026-10-15 至 2026-10-19　病假" in text and a.last.outcome == "answered"
    assert "很私人" not in llm.answer_prompts[0]


def test_a_named_person_narrows_the_leave_list(store, staff):
    llm = FakeLLM({"李大華11月請假了嗎": {"data": "leaves", "person": "李大華"}}, reply="有。")
    text = ask(make(store, llm), "李大華11月請假了嗎")
    assert "李大華　2026-11-02 至 2026-11-03" in text and "王小明" not in text


def test_unknown_person_on_leave_question_is_reported(store, staff):
    llm = FakeLLM({"張三請假了嗎": {"data": "leaves", "person": "張三"}})
    a = make(store, llm)
    assert "員工名單裡找不到「張三」" in ask(a, "張三請假了嗎")
    assert a.last.outcome == "clarify"


def test_holiday_question_goes_through_the_whole_path(store, staff):
    llm = FakeLLM({"今年有哪些假日": {"data": "holidays"}}, reply="有光復節補假。")
    text = ask(make(store, llm), "今年有哪些假日")
    assert "2026-10-25　光復節補假" in text and "每週固定休息日" in text


@pytest.mark.parametrize("question,tool", [("這週誰請假", "leaves"), ("請假名單給我", "leaves"),
                                           ("公司有哪些假日", "holidays"), ("假日清單", "holidays")])
def test_keyword_fallback_picks_the_new_tools(question, tool):
    assert router.heuristic_plan(question).data == tool


def test_leave_regulation_questions_still_go_to_documents():
    plan = router.heuristic_plan("請假要怎麼申請")
    assert plan.data is None and plan.docs is True


# --- 一題多個工具（router 的 also 欄位） ---

def two_calls(**extra):
    return {"data": "leaves", "also": [{"data": "roster", "person": "", "status": "", "event_type": ""}]} | extra


def test_two_tools_run_in_order_and_their_results_are_merged(store, staff):
    llm = FakeLLM({"誰請假，另外列出員工": two_calls()}, reply="王小明請病假。")
    a = make(store, llm)
    text = ask(a, "誰請假，另外列出員工")
    assert text.index("王小明　2026-10-15") < text.index("員工名單（共 2 人") and a.last.tools == ["leaves", "roster"]
    assert a.last.outcome == "answered"


def test_an_extra_step_that_needs_an_unknown_person_is_explained_and_the_main_result_still_shows(store, staff):
    also = [{"data": "records", "person": "張三", "status": "", "event_type": ""}]
    llm = FakeLLM({"誰請假，以及張三的打卡": {"data": "leaves", "also": also}})
    a = make(store, llm)
    text = ask(a, "誰請假，以及張三的打卡")
    assert "王小明　2026-10-15" in text and "員工名單裡找不到「張三」" in text and a.last.tools == ["leaves"]


@pytest.mark.parametrize("bad", [
    "roster", [{"data": "drop_table"}], [{"data": "roster"}, "x"],   # 不是清單、不在白名單、項目型別錯
    [{"data": "leaves"}],                                              # 重複主查詢
    [{"data": "roster"}, {"data": "holidays"}, {"data": "summary"}],   # 超過上限
    [{"data": "none"}], [],
])
def test_a_bad_also_list_is_ignored_and_the_main_query_still_runs(bad):
    plan = router.parse_plan(json.dumps({"docs": False, "data": "leaves", "also": bad}))
    assert plan is not None and plan.data == "leaves" and plan.steps == []


def test_a_valid_also_list_is_parsed_and_capped_at_three_tools_total():
    plan = router.parse_plan(json.dumps({"docs": False, "data": "leaves", "also": [
        {"data": "roster"}, {"data": "records", "person": "李大華", "status": "遲到"}]}))
    assert [(x.data, x.person, x.status) for x in plan.steps] == [("roster", "", None), ("records", "李大華", "遲到")]
    assert 1 + len(plan.steps) <= router.MAX_STEPS


def test_also_without_a_main_query_is_dropped():
    assert router.parse_plan(json.dumps({"docs": True, "data": "none", "also": [{"data": "roster"}]})).steps == []


def test_nested_json_is_parsed_whole_and_text_around_it_is_tolerated():
    text = "好的\n" + json.dumps({"docs": False, "data": "leaves", "also": [{"data": "roster"}]}) + "\n以上"
    assert router.parse_plan(text).steps[0].data == "roster"


def test_the_single_step_prompt_has_no_also_rule():
    import datetime as dt

    single = router.build_prompt("你好", "", dt.date(2026, 10, 17), multi=False)
    assert "also" not in single and "also" in router.build_prompt("你好", "", dt.date(2026, 10, 17))


# --- 追問沿用上一題的日期（same_period）---

def test_same_period_is_read_only_for_a_strict_true_and_only_with_a_data_query():
    base = {"docs": False, "data": "status_list", "status": "早退"}
    assert router.parse_plan(json.dumps({**base, "same_period": True})).same_period is True
    for bad in (False, "true", 1, None, "是"):
        assert router.parse_plan(json.dumps({**base, "same_period": bad})).same_period is False
    assert router.parse_plan(json.dumps({"docs": True, "data": "none", "same_period": True})).same_period is False
    assert router.parse_plan(json.dumps(base)).same_period is False


def test_prompt_explains_same_period_and_the_weekly_rest_days_tool():
    import datetime as dt

    prompt = router.build_prompt("那有人早退嗎", "使用者：這個月誰遲到", dt.date(2026, 10, 17))
    assert "same_period" in prompt and "週休" in prompt and "星期幾休息" in prompt


@pytest.mark.parametrize("question", ["公司的週休是星期幾", "我們的休息日是哪幾天", "週休二日嗎，星期幾休息"])
def test_keyword_fallback_sends_weekly_rest_day_questions_to_the_holidays_tool(question):
    assert router.heuristic_plan(question).data == "holidays"


def test_a_follow_up_without_its_own_time_reuses_the_previous_range_and_says_so(db):
    llm = FakeLLM({"這個月誰遲到": {"data": "status_list", "status": "遲到"},
                   "那有人早退嗎": {"data": "status_list", "status": "早退", "same_period": True}})
    assistant = make(NoDocs(), llm)
    ask(assistant, "這個月誰遲到")
    text = ask(assistant, "那有人早退嗎")
    assert (assistant.last.date_range.start, assistant.last.date_range.end) == ("2026-10-01", "2026-10-31")
    assert "沿用上一題的「這個月」（2026-10-01 至 2026-10-31）" in text
    assert "2026-10-01 至 2026-10-31　早退紀錄" in text


def test_without_the_same_period_flag_the_default_range_is_used(db):
    llm = FakeLLM({"這個月誰遲到": {"data": "status_list", "status": "遲到"},
                   "那有人早退嗎": {"data": "status_list", "status": "早退"}})
    assistant = make(NoDocs(), llm)
    ask(assistant, "這個月誰遲到")
    text = ask(assistant, "那有人早退嗎")
    assert assistant.last.date_range is None and "沿用上一題" not in text and "2026-10-17　早退紀錄" in text


def test_a_range_in_the_question_wins_over_the_previous_one(db):
    llm = FakeLLM({"這個月誰遲到": {"data": "status_list", "status": "遲到"},
                   "那昨天有人早退嗎": {"data": "status_list", "status": "早退", "same_period": True}})
    assistant = make(NoDocs(), llm)
    ask(assistant, "這個月誰遲到")
    text = ask(assistant, "那昨天有人早退嗎")
    assert assistant.last.date_range.start == "2026-10-16" and "沿用上一題" not in text


def test_only_the_immediately_previous_turn_counts(db):
    llm = FakeLLM({"這個月誰遲到": {"data": "status_list", "status": "遲到"},
                   "公司有哪些員工": {"data": "roster"},
                   "那有人早退嗎": {"data": "status_list", "status": "早退", "same_period": True}})
    assistant = make(NoDocs(), llm)
    ask(assistant, "這個月誰遲到")
    ask(assistant, "公司有哪些員工")
    text = ask(assistant, "那有人早退嗎")
    assert assistant.last.date_range is None and "沿用上一題" not in text


def test_a_follow_up_as_the_first_question_has_nothing_to_reuse(db):
    llm = FakeLLM({"那有人早退嗎": {"data": "status_list", "status": "早退", "same_period": True}})
    assistant = make(NoDocs(), llm)
    text = ask(assistant, "那有人早退嗎")
    assert assistant.last.date_range is None and "沿用上一題" not in text


def test_new_chat_forgets_the_previous_range(db):
    llm = FakeLLM({"這個月誰遲到": {"data": "status_list", "status": "遲到"},
                   "那有人早退嗎": {"data": "status_list", "status": "早退", "same_period": True}})
    assistant = make(NoDocs(), llm)
    ask(assistant, "這個月誰遲到")
    assistant.reset()
    ask(assistant, "那有人早退嗎")
    assert assistant.last.date_range is None


def test_the_roster_never_inherits_a_range(db):
    llm = FakeLLM({"這個月誰遲到": {"data": "status_list", "status": "遲到"},
                   "那員工有哪些": {"data": "roster", "same_period": True}})
    assistant = make(NoDocs(), llm)
    ask(assistant, "這個月誰遲到")
    ask(assistant, "那員工有哪些")
    assert assistant.last.date_range is None


# --- 說明的時間範圍不能超出實際查詢的範圍 ---

def test_a_narration_claiming_a_wider_period_than_queried_is_dropped_but_the_table_stays(db):
    llm = FakeLLM({"有人早退嗎": {"data": "status_list", "status": "早退"}}, reply="這個月沒有早退紀錄。")
    assistant = make(NoDocs(), llm)
    text = ask(assistant, "有人早退嗎")
    assert "2026-10-17　早退紀錄" in text and "（沒有符合的紀錄）" in text and "這個月沒有早退紀錄" not in text
    assert "超出實際查詢的範圍" in assistant.last.note


def test_a_narration_matching_the_queried_period_is_kept(db):
    llm = FakeLLM({"有人早退嗎": {"data": "status_list", "status": "早退"}}, reply="今天沒有早退紀錄。")
    assert "今天沒有早退紀錄" in ask(make(NoDocs(), llm), "有人早退嗎")


def test_a_narration_citing_a_day_inside_the_queried_range_is_kept(db, add_user, add_attendance):
    add_user("王小明", "E1")
    add_attendance("王小明", "2026-10-05 08:30:00", "遲到")
    llm = FakeLLM({"這個月誰遲到": {"data": "status_list", "status": "遲到"}}, reply="王小明 2026-10-05 遲到。")
    assistant = make(NoDocs(), llm)
    assert "王小明 2026-10-05 遲到。" in ask(assistant, "這個月誰遲到") and assistant.last.note == ""


def test_the_narration_prompt_forbids_borrowing_a_period_from_the_conversation(db):
    llm = FakeLLM({"有人早退嗎": {"data": "status_list", "status": "早退"}})
    ask(make(NoDocs(), llm), "有人早退嗎")
    assert "只能用查詢結果標題上的日期" in llm.answer_prompts[0]

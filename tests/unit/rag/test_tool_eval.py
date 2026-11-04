"""
tool_eval 單元測試：題庫完整性（欄位、分組、開發集與測試集不重疊、測試題沒有出現在提示詞範例裡）、
判定與統計邏輯（Wilson 區間、每次都對才算通過、樣本不足不下結論）、候選題整理、封存測試集的保護。
不需要語言模型。
"""
import datetime
import json

import pytest

from rag import router, tools
from rag.evaluation import mine_questions, stats, tool_eval

DAY = datetime.date(2026, 11, 4)


# --- 統計 ---

def test_wilson_known_values():
    assert stats.wilson(0, 0) == (0.0, 1.0)
    low, high = stats.wilson(10, 10)
    assert low == pytest.approx(0.722, abs=0.002) and high == 1.0
    low, high = stats.wilson(5, 10)
    assert (low, high) == (pytest.approx(0.237, abs=0.002), pytest.approx(0.763, abs=0.002))
    low, high = stats.wilson(0, 10)
    assert low == 0.0 and high == pytest.approx(0.278, abs=0.002)


def test_wilson_interval_narrows_with_more_questions():
    narrow = stats.wilson(95, 100)
    wide = stats.wilson(19, 20)
    assert narrow[1] - narrow[0] < wide[1] - wide[0]


# --- 題庫完整性 ---

BANK = tool_eval.load_questions()


def test_question_ids_are_unique_and_fields_are_valid():
    ids = [x["id"] for x in BANK]
    assert len(ids) == len(set(ids))
    for x in BANK:
        assert x["split"] in ("dev", "test") and x["group"] in tool_eval.GROUPS
        assert x["source"] in ("author", "teammate")
        assert x["q"].strip() and set(x["tools"]) <= set(tools.TOOLS)
        for alt in x.get("alt", []):
            assert set(alt) <= set(tools.TOOLS)
        for key, value in (x.get("params") or {}).items():
            assert key in ("person", "status", "event_type")
            if key == "status":
                assert value in router.STATUS_ALIASES
            if key == "event_type":
                assert value in router.EVENT_TYPES


def test_group_matches_the_expected_tools():
    for x in BANK:
        n = len(x["tools"])
        if x["group"] == "none":
            assert n == 0, x["id"]
        elif x["group"] == "many":
            assert n >= 2, x["id"]
        elif x["group"] == "one":
            assert n == 1, x["id"]
        elif x["group"] == "adversarial":
            assert n == 0 and not x.get("alt"), x["id"]


def test_follow_up_questions_carry_history_and_an_inherit_expectation():
    follow = [x for x in BANK if x["group"] == "followup"]
    assert len(follow) >= 6
    for x in follow:
        assert x["history"].strip(), x["id"]
        assert x.get("same_period") in (True, False, None), x["id"]
    flags = [x.get("same_period") for x in follow]
    assert flags.count(True) >= 3 and flags.count(False) >= 2   # 有沿用與不沿用的題目；自己有時間說法的不列入判定


def test_dev_and_test_splits_do_not_overlap():
    dev = {x["q"] for x in BANK if x["split"] == "dev"}
    test = {x["q"] for x in BANK if x["split"] == "test"}
    assert not dev & test


def test_no_test_question_appears_in_the_router_prompt_examples():
    prompt = router.build_prompt("", "", DAY)
    assert [x["id"] for x in BANK if x["split"] == "test" and x["q"] in prompt] == []


def test_test_split_is_large_enough_and_contains_real_teammate_questions():
    test = [x for x in BANK if x["split"] == "test"]
    assert len(test) >= 50 and sum(x["source"] == "teammate" for x in test) >= 15
    assert {x["group"] for x in test} == set(tool_eval.GROUPS)


# --- 判定 ---

def plan_of(**fields):
    base = {"docs": False, "data": "none"}
    return router.parse_plan(json.dumps({**base, **fields}, ensure_ascii=False))


def test_check_accepts_the_expected_set_or_an_alternative():
    item = {"id": "x", "q": "q", "tools": ["records"], "alt": [["count"]]}
    assert tool_eval.check(item, plan_of(data="records"))["tools_ok"]
    assert tool_eval.check(item, plan_of(data="count"))["tools_ok"]
    assert not tool_eval.check(item, plan_of(data="leaves"))["tools_ok"]
    assert not tool_eval.check(item, None)["tools_ok"]


def test_check_compares_the_main_and_extra_tools_as_a_set():
    item = {"id": "x", "q": "q", "tools": ["leaves", "roster"]}
    assert tool_eval.check(item, plan_of(data="roster", also=[{"data": "leaves"}]))["tools_ok"]
    assert not tool_eval.check(item, plan_of(data="roster"))["tools_ok"]


def test_params_are_checked_only_when_the_question_specifies_them():
    item = {"id": "x", "q": "q", "tools": ["count"], "params": {"person": "王小明", "status": "遲到"}}
    assert tool_eval.params_ok(item, plan_of(data="count", person="王小明", status="遲到")) is True
    assert tool_eval.params_ok(item, plan_of(data="count", person="王小明", status="早退")) is False
    assert tool_eval.params_ok(item, plan_of(data="count", person="李大華", status="遲到")) is False
    assert tool_eval.params_ok(item, None) is False
    assert tool_eval.params_ok({"id": "y", "q": "q", "tools": []}, plan_of()) is None


def test_overtime_alias_is_compared_after_normalisation():
    item = {"id": "x", "q": "q", "tools": ["status_list"], "params": {"status": "加班"}}
    assert tool_eval.params_ok(item, plan_of(data="status_list", status="加班")) is True


def test_follow_up_must_also_get_the_inherit_flag_right():
    item = {"id": "x", "q": "q", "tools": ["status_list"], "group": "followup", "same_period": True}
    right = tool_eval.check(item, plan_of(data="status_list", same_period=True))
    wrong = tool_eval.check(item, plan_of(data="status_list"))
    assert tool_eval.passed(item, right) and not tool_eval.passed(item, wrong)
    no_inherit = {**item, "same_period": False}
    still_right = tool_eval.check(no_inherit, plan_of(data="status_list"))
    flagged = tool_eval.check(no_inherit, plan_of(data="status_list", same_period=True))
    assert tool_eval.passed(no_inherit, still_right) and not tool_eval.passed(no_inherit, flagged)


# --- 評估與統計 ---

class Scripted:
    """依序回覆預先寫好的 JSON；耗盡後重複最後一個。"""

    def __init__(self, replies):
        self.replies, self.prompts = list(replies), []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, Exception):
            raise reply
        return reply


ITEM = {"id": "a", "q": "今天誰遲到", "tools": ["status_list"], "group": "one", "split": "dev", "source": "author"}
GOOD = json.dumps({"docs": False, "data": "status_list", "status": "遲到"}, ensure_ascii=False)
BAD = json.dumps({"docs": False, "data": "leaves"})


def test_a_question_must_be_right_on_every_run_to_pass():
    results = tool_eval.evaluate(Scripted([GOOD, BAD, GOOD]), [ITEM], runs=3)
    row = tool_eval.summarize(results)["one"]
    assert row["stable"] == 0 and row["trial_rate"] == pytest.approx(2 / 3)
    assert [f["wrong_runs"] for f in tool_eval.failures(results)] == [1]


def test_prompt_includes_history_and_follows_the_mode():
    item = {**ITEM, "history": "使用者：這個月誰遲到"}
    llm = Scripted([GOOD])
    tool_eval.evaluate(llm, [item], multi=False)
    assert "前面的對話：使用者：這個月誰遲到" in llm.prompts[0] and "also" not in llm.prompts[0]


def test_a_failing_call_counts_as_wrong_and_does_not_stop_the_evaluation():
    results = tool_eval.evaluate(Scripted([RuntimeError("boom")]), [ITEM, {**ITEM, "id": "b"}], runs=1)
    assert len(results) == 2 and not any(tool_eval.passed(r["item"], r["trials"][0]) for r in results)


def make_results(n, wrong, group="one"):
    items = [{**ITEM, "id": f"i{i}", "group": group} for i in range(n)]
    results = []
    for i, item in enumerate(items):
        plan = plan_of(data="leaves") if i < wrong else plan_of(data="status_list", status="遲到")
        results.append({"item": item, "trials": [tool_eval.check(item, plan)]})
    return results


def test_verdicts_cover_met_unmet_and_too_few_questions():
    assert tool_eval.summarize(make_results(12, 0))["one"]["verdict"] == "達標"
    assert tool_eval.summarize(make_results(12, 3))["one"]["verdict"] == "未達標"
    assert tool_eval.summarize(make_results(9, 0))["one"]["verdict"] == "樣本不足"


def test_a_perfect_score_on_too_few_questions_cannot_reach_the_interval_floor():
    # 12 題全對的下限約 0.76，剛好過；11 題全對的下限約 0.74，永遠到不了門檻，所以是樣本不足而不是未達標
    assert tool_eval.summarize(make_results(11, 0))["one"]["verdict"] == "樣本不足"
    assert tool_eval.summarize(make_results(12, 0))["one"]["verdict"] == "達標"


def test_params_are_summarised_separately():
    item = {**ITEM, "params": {"status": "遲到"}}
    results = [{"item": item, "trials": [tool_eval.check(item, plan_of(data="status_list", status="早退"))]}]
    summary = tool_eval.summarize(results)
    assert summary["params"]["stable"] == 0 and summary["one"]["stable"] == 1


# --- 候選題 ---

def test_mine_removes_known_and_duplicate_questions_and_hides_user_names(tmp_path):
    log = tmp_path / "q.jsonl"
    rows = [{"user": "小明", "question": "新題目", "tools": ["count"], "outcome": "answered"},
            {"user": "小華", "question": "新題目", "tools": ["count"], "outcome": "answered"},
            {"user": "小明", "question": "已知題目", "tools": [], "outcome": "help"},
            {"user": "小明", "question": "  ", "tools": [], "outcome": "help"}]
    log.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n{壞掉\n", encoding="utf-8")
    found = mine_questions.mine(str(log), known=["已知題目"])
    assert [(c["q"], c["count"], c["expected_tools"]) for c in found] == [("新題目", 2, None)]
    assert "小明" not in json.dumps(found, ensure_ascii=False)


def test_mine_without_a_log_returns_nothing(tmp_path):
    assert mine_questions.mine(str(tmp_path / "none.jsonl"), known=[]) == []


# --- 封存測試集 ---

def test_the_sealed_test_split_needs_an_explicit_confirmation():
    with pytest.raises(SystemExit, match="--confirm-test"):
        tool_eval.main(["--split", "test"])
    with pytest.raises(SystemExit, match="--confirm-test"):
        tool_eval.main(["--split", "all"])


def test_a_named_person_turns_summary_and_status_list_into_records_like_the_real_pipeline():
    item = {"id": "x", "q": "q", "tools": ["records"]}
    assert tool_eval.check(item, plan_of(data="summary", person="王小明"))["tools_ok"]
    assert tool_eval.check(item, plan_of(data="status_list", person="王小明", status="遲到"))["tools_ok"]
    summary_item = {"id": "y", "q": "q", "tools": ["summary"]}
    assert not tool_eval.check(summary_item, plan_of(data="summary", person="王小明"))["tools_ok"]
    assert tool_eval.check(summary_item, plan_of(data="summary"))["tools_ok"]


def test_follow_up_without_an_expectation_is_judged_on_tools_only():
    item = {"id": "x", "q": "那這週呢", "tools": ["count"], "group": "followup"}
    for flag in (True, False):
        trial = tool_eval.check(item, plan_of(data="count", same_period=flag))
        assert trial["period_ok"] is None and tool_eval.passed(item, trial)

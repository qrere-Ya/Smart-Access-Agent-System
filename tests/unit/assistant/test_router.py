"""
router 單元測試：LLM 輸出的驗證（格式、白名單、長度）、關鍵字備案、LLM 失敗時退回備案、提示詞內容。
"""
import datetime
import json

import pytest

from assistant import router

TODAY = datetime.date(2026, 10, 17)


def reply(**fields):
    base = {"docs": False, "data": "none", "person": "", "status": "", "event_type": ""}
    return json.dumps(base | fields, ensure_ascii=False)


# --- 驗證 LLM 輸出 ---

def test_valid_plan_is_parsed():
    plan = router.parse_plan(reply(data="count", person="王小明", status="遲到"))
    assert (plan.docs, plan.data, plan.person, plan.status, plan.source) == (False, "count", "王小明", "遲到", "llm")


def test_none_data_means_no_database_query():
    assert router.parse_plan(reply(docs=True)).data is None


def test_json_surrounded_by_other_text_is_still_found():
    assert router.parse_plan("好的，結果如下：\n```json\n" + reply(docs=True) + "\n```").docs is True


@pytest.mark.parametrize("text", [
    "", "不是 JSON", "{壞掉的 json}", "[]", reply(docs="yes"), reply(data="drop_table"), reply(status="翹班"),
    reply(event_type="fire"), reply(person="王" * 21), json.dumps({"data": "count"}),
])
def test_invalid_output_is_rejected(text):
    assert router.parse_plan(text) is None


def test_overtime_is_mapped_to_the_status_stored_in_the_database():
    assert router.parse_plan(reply(data="status_list", status="加班")).status == "下班 (加班)"


def test_hostile_person_text_is_kept_as_plain_text_only():
    plan = router.parse_plan(reply(data="records", person="王小明' OR 1=1--"))
    assert plan.person == "王小明' OR 1=1--"  # 不會被當 SQL；之後只拿去和名單比對
    assert router.parse_plan(reply(data="records", person="王小明'; DROP TABLE Users;--")) is None  # 太長直接拒絕


# --- 關鍵字備案 ---

@pytest.mark.parametrize("question, docs, data", [
    ("特別休假有幾天？", True, None),
    ("王小明這個月遲到幾次", False, "count"),
    ("今天誰遲到", False, "status_list"),
    ("王小明昨天的打卡紀錄", False, "records"),
    ("今天誰沒打卡", False, "summary"),
    ("最近有沒有尾隨事件", False, "anomalies"),
    ("公司有哪些員工", False, "roster"),
    ("王小明遲到太多次，依法規定公司可以怎麼處理", True, "records"),
    ("你好", True, None),
])
def test_heuristic_plan(question, docs, data):
    plan = router.heuristic_plan(question)
    assert (plan.docs, plan.data, plan.source) == (docs, data, "heuristic")


def test_heuristic_picks_up_the_status_and_event_type():
    assert router.heuristic_plan("今天誰加班").status == "下班 (加班)"
    assert router.heuristic_plan("有沒有異常").event_type == "tailgate"


# --- 先問 LLM，失敗退回備案 ---

class FakeLLM:
    def __init__(self, output=None, error=None):
        self.output, self.error, self.prompts = output, error, []

    def complete(self, prompt):
        self.prompts.append(prompt)
        if self.error:
            raise self.error
        return self.output


def test_llm_plan_is_used_when_valid():
    plan = router.make_plan(FakeLLM(reply(docs=True)), "特休幾天", "", TODAY)
    assert plan.source == "llm" and plan.docs


def test_invalid_llm_output_falls_back_to_keywords():
    plan = router.make_plan(FakeLLM("我不知道"), "今天誰遲到", "", TODAY)
    assert plan.source == "heuristic" and plan.data == "status_list"


def test_llm_error_falls_back_to_keywords():
    plan = router.make_plan(FakeLLM(error=RuntimeError("連不上")), "今天誰遲到", "", TODAY)
    assert plan.source == "heuristic"


def test_missing_llm_falls_back_to_keywords():
    assert router.make_plan(None, "特休幾天", "", TODAY).source == "heuristic"


def test_prompt_contains_today_history_and_question_and_asks_for_json_only():
    llm = FakeLLM(reply(docs=True))
    router.make_plan(llm, "那上週呢", "使用者：王小明遲到幾次\n助理：3 次", TODAY)
    prompt = llm.prompts[0]
    assert "2026-10-17（星期六）" in prompt and "王小明遲到幾次" in prompt and "問題：那上週呢" in prompt
    assert "只輸出一行 JSON" in prompt

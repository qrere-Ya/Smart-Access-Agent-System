"""
evaluate_rag 單元測試：出題官輸出的解析（容許各種格式）、重問機制、裁判官判定的解析、
各類別的統計、正負例測驗的流程。出題官與考生都用假物件取代。
"""
import types

import pytest

from rag.eval import evaluate_rag as er


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(er.time, "sleep", lambda seconds: None)


@pytest.mark.parametrize("text, expected", [
    ("問題: 特休假有幾天？\n答案: 七天", ("特休假有幾天？", "七天")),
    ("問題：特休假有幾天？\n答案：七天", ("特休假有幾天？", "七天")),                 # 全形冒號
    ("**問題**: 特休假有幾天？\n**答案**: 七天", ("特休假有幾天？", "七天")),          # 粗體
    ("Question: 特休假有幾天？\nAnswer: 七天", ("特休假有幾天？", "七天")),
    ("問題: [特休假有幾天？]\n答案: [七天]", ("特休假有幾天？", "七天")),            # 括號包住
    ("問題: 特休假有幾天？ 2. 標準答案: 七天", ("特休假有幾天？", "七天")),          # 殘留編號
    ("前言\n問題: 特休假有幾天？\n標準答案: 七天\n結尾", ("特休假有幾天？", "七天\n結尾")),
])
def test_parse_qa_accepts_many_formats(text, expected):
    assert er._parse_qa(text) == expected


@pytest.mark.parametrize("text", [None, "", "沒有任何標記", "問題: 短\n答案: 七天", "問題: 特休假有幾天？\n答案:   "])
def test_parse_qa_rejects_unusable_output(text):
    assert er._parse_qa(text) is None


@pytest.mark.parametrize("raw, expected", [
    ("[abc]", "abc"), ("「說明」", "說明"), ("【標題】", "標題"),
    ("  普通文字  ", "普通文字"), ("[未閉合", "[未閉合"), ("x", "x"),
])
def test_strip_wrapping(raw, expected):
    assert er._strip_wrapping(raw) == expected


def chunks(*texts):
    return [types.SimpleNamespace(delta=t) for t in texts]


class ScriptedJudge:
    """依序回傳預先寫好的輸出，用來控制出題官與裁判官的回答。"""

    def __init__(self, *outputs, error=None):
        self.outputs, self.error, self.calls = list(outputs), error, 0

    def stream_complete(self, prompt):
        self.calls += 1
        if self.error:
            raise self.error
        return chunks(*self.outputs.pop(0))


def test_generate_qa_retries_until_the_format_is_right():
    judge = ScriptedJudge(["亂寫"], ["還是亂寫"], ["問題: 特休假有幾天？\n", "答案: 七天"])
    assert er._generate_qa(judge, "prompt") == ("特休假有幾天？", "七天", "問題: 特休假有幾天？\n答案: 七天")
    assert judge.calls == 3


def test_generate_qa_gives_up_after_three_tries():
    judge = ScriptedJudge(["a"], ["b"], ["c"], ["問題: 不該用到的第四次輸出"])
    assert er._generate_qa(judge, "prompt") == (None, None, "c")
    assert judge.calls == 3


def drain(generator):
    """執行產生器並回傳它 return 的值。"""
    try:
        while True:
            next(generator)
    except StopIteration as stop:
        return stop.value


@pytest.mark.parametrize("verdict, expected", [
    ("[判定結果] [合格]\n[分類] 無\n[理由] 正確", (True, "無")),
    ("[判定結果] 合格", (True, "無")),                              # 沒有方括號也要認得
    ("[判定結果] [不合格]\n[分類] 矛盾", (False, "矛盾")),
    ("[判定結果] 不合格 [分類] 遺漏關鍵資訊", (False, "遺漏關鍵資訊")),
    ("[判定結果] 不合格 [分類] 幻覺瞎掰", (False, "幻覺瞎掰")),
    ("[判定結果] 不合格", (False, "未分類")),
    ("我看不懂這題", (False, "未分類")),                             # 格式亂掉時保守視為不合格
])
def test_judge_verdict_parsing(verdict, expected):
    history = []
    assert drain(er._judge_answer(history, ScriptedJudge([verdict]), "標準答案", "考生答案")) == expected
    assert "標準答案" in history[-1]["content"]


def test_judge_failure_still_reveals_the_standard_answer():
    history = []
    judge = ScriptedJudge(error=RuntimeError("API 壞了"))
    assert drain(er._judge_answer(history, judge, "七天", "考生答案")) == (False, "未知（API失敗）")
    assert "裁判官呼叫失敗" in history[-1]["content"] and "七天" in history[-1]["content"]


def test_examiner_and_student_messages_use_opposite_roles():
    history = []
    er._add_examiner(history, "出題")
    student = er._start_student_turn(history, "作答：")
    assert [h["role"] for h in history] == ["user", "assistant"]
    student["content"] += "更多"
    assert history[-1]["content"] == "作答：更多"


def test_category_statistics():
    assert er._cat_stats([]) is None
    stats = er._cat_stats([
        {"is_pass": True, "category": "無"}, {"is_pass": False, "category": "幻覺瞎掰"},
        {"is_pass": False, "category": "矛盾"}, {"is_pass": True, "category": "無"},
    ])
    assert stats == {"total": 4, "pass_count": 2, "pass_rate": 0.5,
                     "hallucination_count": 1, "hallucination_rate": 0.25}


def test_guardrail_category_scores_blocked_and_allowed_questions(monkeypatch):
    def fake_system(question, route=None, history=None):
        yield "🛑 攔截" if question in er.BAD_SAMPLE_QUESTIONS else "正常回答"

    monkeypatch.setattr(er, "stream_chat_response", fake_system)
    history = []
    results = drain(er._run_guardrail_category(history, 3))
    assert len(results) == 6 and all(r["correct"] for r in results)
    assert sum(r["expect_blocked"] for r in results) == 3


def test_guardrail_category_detects_a_failing_guardrail(monkeypatch):
    monkeypatch.setattr(er, "stream_chat_response", lambda q, route=None, history=None: iter(["一律放行"]))
    results = drain(er._run_guardrail_category([], 2))
    assert [r["correct"] for r in results if r["expect_blocked"]] == [False, False]
    assert [r["correct"] for r in results if not r["expect_blocked"]] == [True, True]


def test_question_pools_are_large_enough_for_the_default_sample_size():
    assert len(er.GOOD_SAMPLE_QUESTIONS) >= 20 and len(er.BAD_SAMPLE_QUESTIONS) >= 20
    assert not set(er.GOOD_SAMPLE_QUESTIONS) & set(er.BAD_SAMPLE_QUESTIONS)

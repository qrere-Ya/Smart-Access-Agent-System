"""
rerank 與檢索評估單元測試：重排依分數排序並保留來源資訊、評估指標（名次、hit@k、MRR、無依據題）、
題庫格式正確，且題庫標的條號在真實法規檔裡都存在（有 data/laws 時才檢查）。
"""
import os

import pytest

from assistant import ingest
from assistant.doc_store import Hit
from assistant.evaluation import retrieval_eval as ev
from assistant.rerank import Reranker
from shared import paths


def hit(article, body="內容", dense=0.5):
    return Hit({"title": "法", "section": "章", "article": article, "body": body, "source": "法.txt"}, 0.1, dense)


# --- 重排 ---

def test_rerank_orders_by_score_and_keeps_the_top_k():
    scores = {"甲": 0.1, "乙": 0.9, "丙": 0.5}
    reranker = Reranker(lambda pairs: [scores[doc.split("\n")[1]] for _, doc in pairs])
    got = reranker.rerank("問題", [hit("第1條", "甲"), hit("第2條", "乙"), hit("第3條", "丙")], top_k=2)
    assert [h.chunk["article"] for h in got] == ["第2條", "第3條"]
    assert [h.rerank for h in got] == [0.9, 0.5] and got[0].score == 0.9


def test_rerank_sends_question_and_labelled_text_to_the_model():
    seen = []
    Reranker(lambda pairs: seen.extend(pairs) or [0.0] * len(pairs)).rerank("特休幾天", [hit("第38條", "七日")])
    assert seen == [("特休幾天", "法 章 第38條\n七日")]


def test_rerank_keeps_the_original_similarity_and_does_not_modify_inputs():
    original = hit("第1條", dense=0.42)
    got = Reranker(lambda pairs: [0.8] * len(pairs)).rerank("問題", [original])
    assert got[0].dense == 0.42 and original.rerank is None and original.score == 0.1


def test_rerank_with_nothing_to_do_returns_empty():
    reranker = Reranker(lambda pairs: pytest.fail("不該呼叫模型"))
    assert reranker.rerank("問題", []) == [] and reranker.rerank("  ", [hit("第1條")]) == []


# --- 評估指標 ---

def ranking(*articles):
    return [(a, 0.5) for a in articles]


def test_base_article_strips_the_segment_suffix():
    assert ev.base_article("第56條（第 2/3 段）") == "第56條" and ev.base_article("第9-1條") == "第9-1條"


def test_first_hit_rank():
    assert ev.first_hit_rank(ranking("第1條", "第2條", "第3條"), ["第3條"]) == 3
    assert ev.first_hit_rank(ranking("第1條", "第2條"), ["第9條"]) is None
    assert ev.first_hit_rank(ranking("第1條", "第2條"), ["第2條", "第1條"]) == 1  # 任何一條命中即可


def test_segments_of_one_article_count_as_one_rank():
    assert ev.first_hit_rank(ranking("第56條（第 1/2 段）", "第56條（第 2/2 段）", "第9條"), ["第9條"]) == 2


QUESTIONS = [
    {"id": "a", "level": "詳細", "question": "甲", "gold": ["第1條"], "in_scope": True},
    {"id": "b", "level": "詳細", "question": "乙", "gold": ["第2條"], "in_scope": True},
    {"id": "c", "level": "刁鑽", "question": "丙", "gold": ["第3條"], "in_scope": True},
    {"id": "d", "level": "刁鑽", "question": "丁", "gold": [], "in_scope": False},
]


def test_summary_by_level_with_hit_at_k_and_mrr():
    rankings = {"a": ranking("第1條"), "b": ranking("第9條", "第8條", "第2條"), "c": ranking("第9條"),
                "d": [("第5條", 0.56), ("第6條", 0.1)]}
    summary = ev.summarize(QUESTIONS, rankings)
    assert summary["詳細"] == {"n": 2, "mrr": pytest.approx((1 + 1 / 3) / 2), "hit@1": 0.5, "hit@3": 1.0, "hit@5": 1.0}
    assert summary["刁鑽"]["n"] == 1 and summary["刁鑽"]["hit@5"] == 0.0 and summary["刁鑽"]["mrr"] == 0.0
    assert summary["全部"]["n"] == 3
    assert summary["無依據題最高分"] == [0.56]


def test_out_of_scope_questions_are_not_counted_as_hits_or_misses():
    summary = ev.summarize(QUESTIONS[3:], {"d": ranking("第1條")})
    assert "全部" not in summary and "刁鑽" not in summary


def test_evaluate_calls_the_search_for_each_question():
    asked = []

    def search(question):
        asked.append(question)
        return ranking("第1條")

    summary, rankings = ev.evaluate(search, QUESTIONS)
    assert asked == ["甲", "乙", "丙", "丁"] and set(rankings) == {"a", "b", "c", "d"}
    assert summary["全部"]["hit@1"] == pytest.approx(1 / 3)


def test_table_lists_every_level_present():
    summary = ev.summarize(QUESTIONS, {"a": ranking("第1條"), "b": ranking("第2條"), "c": ranking("第3條"),
                                       "d": ranking("第5條")})
    table = ev.format_table("測試", summary)
    assert "詳細" in table and "刁鑽" in table and "全部" in table and "100%" in table


# --- 題庫 ---

def test_question_bank_is_well_formed():
    questions = ev.load_questions()
    ids = [q["id"] for q in questions]
    assert len(ids) == len(set(ids)) and len(questions) >= 30
    assert {q["level"] for q in questions} == set(ev.LEVELS)
    for q in questions:
        assert q["question"].strip() and isinstance(q["gold"], list)
        assert bool(q["gold"]) == q["in_scope"], q["id"]


def test_every_level_has_questions():
    counts = {level: sum(1 for q in ev.load_questions() if q["level"] == level and q["in_scope"])
              for level in ev.LEVELS}
    assert all(n >= 5 for n in counts.values()), counts


def test_chunk_key_is_document_title_plus_article_without_the_segment_suffix():
    assert ev.chunk_key({"title": "勞動基準法", "article": "第56條（第 1/2 段）"}) == "勞動基準法 第56條"
    assert ev.chunk_key({"title": "聲明書", "article": ""}) == "聲明書"
    assert ev.chunk_key({"title": "聲明書", "article": "（第 2/2 段）"}) == "聲明書"


def test_gold_answers_exist_in_the_real_documents():
    folder = paths.LAWS_DIR if os.path.isdir(paths.LAWS_DIR) else None
    real = os.path.join(paths.PROJECT_ROOT, "data", "laws")
    folder = real if os.path.isdir(real) and os.listdir(real) else folder
    if not folder or not os.listdir(folder):
        pytest.skip("沒有 data/laws 文件")
    keys = set()
    for name in os.listdir(folder):
        if name.lower().endswith(ingest.SUPPORTED_EXTENSIONS):
            chunks = ingest.chunk_document(name, ingest.read_file(os.path.join(folder, name)))
            keys |= {ev.chunk_key(vars(c)) for c in chunks}
    missing = {(q["id"], g) for q in ev.load_questions() for g in q["gold"] if g not in keys}
    assert not missing, f"題庫標的在文件裡找不到（文件被換掉了嗎？）：{missing}"

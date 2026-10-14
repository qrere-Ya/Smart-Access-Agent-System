"""
rag_guardrail 單元測試：混合題與考勤狀態題的判斷、路由的優先順序與稽核紀錄、
上一句話的取得、純關鍵字防護欄（消融實驗對照組）、嵌入快取。
"""
import sqlite3

import pytest
from llama_index.core import Settings
from llama_index.core.embeddings import MockEmbedding

from rag import rag_guardrail as rg

NAMES = ["陳柏豫", "葉君緯"]


@pytest.fixture
def names(monkeypatch):
    monkeypatch.setattr(rg.rag_sql_engine, "_get_known_employee_names", lambda: list(NAMES))


@pytest.fixture
def mock_embed():
    old = Settings._embed_model
    Settings.embed_model = MockEmbedding(embed_dim=8)
    yield
    Settings._embed_model = old


def usage_rows(db):
    con = sqlite3.connect(db)
    rows = con.execute("SELECT event_type, route, guardrail_pass FROM llm_usage_log ORDER BY id").fetchall()
    con.close()
    return rows


@pytest.mark.parametrize("text, expected", [
    ("陳柏豫這個月遲到扣薪上限多少", True),
    ("葉君緯遲到被扣薪合法嗎", True),
    ("員工遲到是否可以扣除整天薪水", False),     # 沒有真實員工姓名，是純法規問題
    ("陳柏豫今天有打卡嗎", False),               # 沒有法規計算詞
    ("", False),
])
def test_detect_mixed_question(names, text, expected):
    assert rg._detect_mixed_question(text) is expected


@pytest.mark.parametrize("text, expected", [
    ("葉君緯有無遲到紀錄", True),
    ("葉君為有無遲到紀錄", True),                 # 姓名打錯一個字
    ("陳柏豫早退幾次", True),
    ("員工遲到可以扣薪嗎", False),
    ("陳柏豫今天打卡嗎", False),
    ("", False),
])
def test_detect_attendance_status_question(names, text, expected):
    assert rg._detect_attendance_status_question(text) is expected


def test_detectors_degrade_to_false_when_the_roster_is_unavailable(monkeypatch):
    def boom():
        raise RuntimeError("資料庫壞了")

    monkeypatch.setattr(rg.rag_sql_engine, "_get_known_employee_names", boom)
    assert rg._detect_mixed_question("陳柏豫扣薪上限") is False
    assert rg._detect_attendance_status_question("陳柏豫遲到") is False


def test_route_mixed_has_priority_and_is_logged(db, names):
    assert rg.check_query_route("陳柏豫這個月遲到扣薪上限多少") == "mixed"
    assert usage_rows(db) == [("route", "mixed", None)]


def test_route_name_plus_status_word_is_attendance(db, names):
    assert rg.check_query_route("葉君緯有無遲到紀錄") == "attendance"
    assert usage_rows(db) == [("route", "attendance", None)]


@pytest.mark.parametrize("query", ["請問打卡紀錄怎麼查", "請問打考紀錄怎麼查", "今天的行事曆有什麼", "會議行程是什麼"])
def test_route_keyword_fast_path_including_typos(db, names, query):
    assert rg.check_query_route(query) == "attendance"


def test_extract_last_user_context():
    history = [
        {"role": "user", "content": "員工目前幾人"},
        {"role": "assistant", "content": "目前員工有 3 人。"},
        {"role": "user", "content": "有名子嗎？"},
    ]
    assert rg.extract_last_user_context(history) == "員工目前幾人"


@pytest.mark.parametrize("history", [
    None, [], [{"role": "user", "content": "只有這一句"}],
    [{"role": "assistant", "content": "歡迎"}, {"role": "user", "content": "只有這一句"}],
    [{"role": "user", "content": ""}, {"role": "user", "content": "第二句"}],
])
def test_extract_last_user_context_without_a_previous_sentence(history):
    assert rg.extract_last_user_context(history) is None


def test_extract_last_user_context_handles_gradio_list_content():
    history = [
        {"role": "user", "content": [{"text": "員工目前幾人", "type": "text"}]},
        {"role": "user", "content": [{"text": "有名子嗎？", "type": "text"}]},
    ]
    assert rg.extract_last_user_context(history) == "員工目前幾人"


def test_semantic_guardrail_blocks_intent_words_without_embeddings(db, mock_embed):
    assert rg.check_semantic_guardrail("幫我寫一個 Python 腳本") is False
    assert usage_rows(db) == [("guardrail", None, 0)]


def test_keyword_only_guardrail_for_the_ablation_baseline(db, monkeypatch):
    monkeypatch.setattr(rg.ablation_config, "USE_SEMANTIC_GUARDRAIL", False)
    assert rg.check_semantic_guardrail("請問特休假的規定") is True
    assert rg.check_semantic_guardrail("幫我看看明天天氣") is False
    assert usage_rows(db) == [("guardrail", None, 1), ("guardrail", None, 0)]


class _CountingEmbed:
    def __init__(self):
        self.calls = []

    def get_text_embedding(self, text):
        self.calls.append(text)
        return [float(len(text))]

    def get_text_embedding_batch(self, texts):
        return [self.get_text_embedding(t) for t in texts]


def test_embedding_cache_reuses_results_and_evicts_the_oldest(monkeypatch):
    monkeypatch.setattr(rg, "_EMBED_CACHE_MAXSIZE", 2)
    monkeypatch.setattr(rg, "_embed_text_cache", {})
    monkeypatch.setattr(rg, "_embed_text_cache_order", [])
    model = _CountingEmbed()
    for text in ("甲", "甲", "乙乙", "丙丙丙", "甲"):
        rg._cached_get_text_embedding(model, text)
    assert model.calls == ["甲", "乙乙", "丙丙丙", "甲"]


def test_wrapped_embed_model_is_a_stable_singleton_per_model():
    model = _CountingEmbed()
    first = rg._get_cached_embed_model(model)
    assert rg._get_cached_embed_model(model) is first
    assert first.get_text_embedding("文字") == [2.0]
    assert first.get_text_embedding_batch(["a", "bb"]) == [[1.0], [2.0]]

"""
main_guardrail_rag 單元測試：問答流程的調度（防護欄 → 路由 → 考勤／法規／混合，查無資料時互相補位）、
混合題的整合與退路、斷句、本機 bge-m3 模型的判斷、初始化與釋放。
所有模型與資料庫都用假物件取代，不載入任何真實模型。
"""
import types

import pytest
from llama_index.core import Settings
from llama_index.llms.ollama import Ollama

from rag import main_guardrail_rag as m
from rag import rag_resources


class FakeResponse:
    def __init__(self, text, node_ids=(), tokens=None):
        self._text = text
        self.source_nodes = [types.SimpleNamespace(node=types.SimpleNamespace(node_id=i)) for i in node_ids]
        self.response_gen = iter([text] if tokens is None else tokens)

    def __str__(self):
        return self._text


class FakeChat:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.calls = response, error, []

    def chat(self, message):
        self.calls.append(message)
        if self.error:
            raise self.error
        return self.response

    def stream_chat(self, message):
        return self.chat(message)


class FakeLLM:
    def __init__(self, completion="整合後的答案", error=None, tokens=("甲", "乙")):
        self.completion, self.error, self.tokens, self.prompts = completion, error, tokens, []

    def complete(self, prompt):
        self.prompts.append(prompt)
        if self.error:
            raise self.error
        return self.completion

    def stream_complete(self, prompt):
        return [types.SimpleNamespace(delta=t) for t in self.tokens]


@pytest.fixture
def wired(monkeypatch):
    """把問答流程需要的全域物件換成假的，並讓防護欄、路由、考勤查詢可以由測試控制。"""
    state = types.SimpleNamespace(
        guard=True, route="law", attendance=(False, None), logged=[],
        chat=FakeChat(FakeResponse("法規說明", ["n1", "n2"])), llm=FakeLLM(), sql_engine=object(),
    )
    monkeypatch.setattr(m, "_GLOBAL_CHAT_ENGINE", state.chat)
    monkeypatch.setattr(m, "_GLOBAL_SQL_ENGINE", state.sql_engine)
    monkeypatch.setattr(m, "_GLOBAL_LLM", state.llm)
    monkeypatch.setattr(m, "_LAST_SOURCE_NODE_IDS", [])
    monkeypatch.setattr(m, "_LAST_QUERY_ROUTE", None)
    monkeypatch.setattr(m.rag_guardrail, "check_semantic_guardrail", lambda q, context=None: state.guard)
    monkeypatch.setattr(m.rag_guardrail, "check_query_route", lambda q, context=None: state.route)
    monkeypatch.setattr(m.rag_sql_engine, "try_attendance_answer", lambda engine, q: state.attendance)
    monkeypatch.setattr(m.database_mgr, "log_llm_usage", lambda **kw: state.logged.append(kw))

    def set_chat(chat):
        state.chat = chat
        monkeypatch.setattr(m, "_GLOBAL_CHAT_ENGINE", chat)

    def set_llm(llm):
        state.llm = llm
        monkeypatch.setattr(m, "_GLOBAL_LLM", llm)

    state.set_chat, state.set_llm = set_chat, set_llm
    return state


def stream(message="問題", route="RAG", history=None):
    return "".join(m._stream_chat_response_impl(message, route, history))


# ---------- 串流版本 ----------

def test_basic_route_asks_the_model_directly_and_logs_it(wired):
    assert stream(route="Basic") == "甲乙"
    assert m._LAST_QUERY_ROUTE == "basic" and wired.logged == [{"event_type": "basic", "question": "問題"}]


def test_unknown_route(wired):
    assert stream(route="???") == "Error: Invalid Route"


def test_guardrail_blocks_off_topic_questions(wired):
    wired.guard = False
    assert stream().startswith("🛑 [Guardrail 攔截]")
    assert m._LAST_QUERY_ROUTE == "blocked" and wired.chat.calls == []


def test_law_route_streams_the_answer_and_records_the_sources(wired):
    wired.set_chat(FakeChat(FakeResponse("特休", ["n1", "n2"], tokens=["特", "休"])))
    assert stream() == "特休"
    assert m._LAST_QUERY_ROUTE == "law" and m._LAST_SOURCE_NODE_IDS == ["n1", "n2"]


def test_law_route_explains_when_nothing_was_retrieved(wired):
    wired.set_chat(FakeChat(FakeResponse("", [])))
    assert "知識庫中找不到" in stream() and m._LAST_SOURCE_NODE_IDS == []


def test_law_route_explains_when_generation_produced_nothing(wired):
    wired.set_chat(FakeChat(FakeResponse("", ["n1"], tokens=[])))
    assert "生成回答時發生問題" in stream()


def test_law_route_survives_retrieval_errors(wired):
    wired.set_chat(FakeChat(error=RuntimeError("boom")))
    assert "在檢索相關法規時遇到問題" in stream()


def test_attendance_route_answers_from_the_database(wired):
    wired.route, wired.attendance = "attendance", (True, "陳柏豫共有 2 次遲到紀錄。")
    assert stream() == "陳柏豫共有 2 次遲到紀錄。"
    assert wired.chat.calls == [] and m._LAST_SOURCE_NODE_IDS == []


def test_attendance_route_without_a_database_connection(wired, monkeypatch):
    wired.route = "attendance"
    monkeypatch.setattr(m, "_GLOBAL_SQL_ENGINE", None)
    assert "尚未連線成功" in stream()


def test_attendance_with_no_data_falls_back_to_the_law_engine(wired):
    wired.route, wired.attendance = "attendance", (False, None)
    wired.set_chat(FakeChat(FakeResponse("遲到扣薪應按比例", ["n7"])))
    assert stream() == "遲到扣薪應按比例"
    assert m._LAST_QUERY_ROUTE == "attendance_fallback_law" and m._LAST_SOURCE_NODE_IDS == ["n7"]


@pytest.mark.parametrize("chat", [FakeChat(FakeResponse("無來源", [])), FakeChat(error=RuntimeError("x"))])
def test_attendance_fallback_without_law_data_says_nothing_was_found(wired, chat):
    wired.route = "attendance"
    wired.set_chat(chat)
    assert stream() == "查無符合條件的資料。"


def test_mixed_route_delegates_to_the_combined_answer(wired, monkeypatch):
    wired.route = "mixed"
    monkeypatch.setattr(m, "_answer_mixed_question", lambda message: f"整合：{message}")
    assert stream("某人遲到扣薪") == "整合：某人遲到扣薪"
    assert m._LAST_SOURCE_NODE_IDS == []


# ---------- 非串流版本 ----------

def answer(message="問題", route="RAG"):
    return m._get_chat_response_impl(message, route)


def test_non_streaming_flows(wired):
    assert answer(route="Basic") == "整合後的答案"
    assert answer(route="???") == "Error: Invalid Route"
    assert answer() == "法規說明" and m._LAST_SOURCE_NODE_IDS == ["n1", "n2"]
    wired.guard = False
    assert answer().startswith("🛑")
    wired.guard, wired.route, wired.attendance = True, "attendance", (True, "考勤答案")
    assert answer() == "考勤答案"


def test_non_streaming_attendance_fallback_and_empty_answers(wired):
    wired.route, wired.attendance = "attendance", (False, None)
    wired.set_chat(FakeChat(FakeResponse("法規補位", ["n9"])))
    assert answer() == "法規補位" and m._LAST_QUERY_ROUTE == "attendance_fallback_law"
    wired.set_chat(FakeChat(FakeResponse("", [])))
    assert answer() == "查無符合條件的資料。"
    wired.route = "law"
    assert "找不到與此問題高度相關" in answer()
    wired.set_chat(FakeChat(error=RuntimeError("x")))
    assert "在檢索相關法規時遇到問題" in answer()


# ---------- 混合題 ----------

def test_mixed_answer_combines_both_sides_through_the_model(wired):
    wired.attendance = (True, "陳柏豫這個月遲到 2 次")
    wired.set_chat(FakeChat(FakeResponse("遲到扣薪應按比例", ["n1"])))
    assert m._answer_mixed_question("陳柏豫遲到扣薪上限") == "整合後的答案"
    prompt = wired.llm.prompts[0]
    assert "陳柏豫這個月遲到 2 次" in prompt and "遲到扣薪應按比例" in prompt and "陳柏豫遲到扣薪上限" in prompt
    assert m._LAST_SOURCE_NODE_IDS == ["n1"]


@pytest.mark.parametrize("llm", [FakeLLM(error=RuntimeError("逾時")), FakeLLM(completion="   ")])
def test_mixed_answer_concatenates_when_the_model_fails(wired, llm):
    wired.attendance = (True, "考勤答案")
    wired.set_chat(FakeChat(FakeResponse("法規說明", ["n1"])))
    wired.set_llm(llm)
    assert m._answer_mixed_question("問題") == "【考勤資料】考勤答案\n【法規說明】法規說明"


def test_mixed_answer_uses_whichever_side_has_data(wired):
    wired.attendance = (True, "只有考勤")
    wired.set_chat(FakeChat(FakeResponse("", [])))
    assert m._answer_mixed_question("問題") == "只有考勤"
    wired.attendance = (False, None)
    wired.set_chat(FakeChat(FakeResponse("只有法規", ["n3"])))
    assert m._answer_mixed_question("問題") == "只有法規" and m._LAST_SOURCE_NODE_IDS == ["n3"]


def test_mixed_answer_when_nothing_is_found(wired, monkeypatch):
    wired.set_chat(FakeChat(error=RuntimeError("x")))
    monkeypatch.setattr(m, "_GLOBAL_SQL_ENGINE", None)
    assert m._answer_mixed_question("問題") == "查無符合條件的資料。"


def test_mixed_answer_treats_an_attendance_error_as_no_data(wired, monkeypatch):
    def boom(engine, question):
        raise RuntimeError("資料庫壞了")

    monkeypatch.setattr(m.rag_sql_engine, "try_attendance_answer", boom)
    wired.set_chat(FakeChat(FakeResponse("法規說明", ["n1"])))
    assert m._answer_mixed_question("問題") == "法規說明"


def test_retrieved_ids_are_returned_as_a_copy(wired, monkeypatch):
    monkeypatch.setattr(m, "_LAST_SOURCE_NODE_IDS", ["n1"])
    ids = m.get_last_retrieved_node_ids()
    ids.append("n2")
    assert m.get_last_retrieved_node_ids() == ["n1"]
    monkeypatch.setattr(m, "_LAST_QUERY_ROUTE", "law")
    assert m.get_last_query_route() == "law"


# ---------- 斷句、模型位置、初始化 ----------

@pytest.mark.parametrize("text, expected", [
    ("第一句。第二句！第三句？\n第四句；第五句", ["第一句。", "第二句！", "第三句？", "第四句；", "第五句"]),
    ("沒有標點的一整段", ["沒有標點的一整段"]),
    ("", []),
    ("。。。", ["。", "。", "。"]),
])
def test_chinese_sentence_splitting(text, expected):
    assert m._split_chinese_sentences(text) == expected


def test_local_bge_m3_is_used_only_when_complete(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(m, "_LOCAL_BGE_M3_DIR", str(tmp_path))
    assert m._resolve_bge_m3_model_source() == "BAAI/bge-m3"
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    assert m._resolve_bge_m3_model_source() == "BAAI/bge-m3"
    (tmp_path / "model.safetensors").write_bytes(b"x")
    assert m._resolve_bge_m3_model_source() == str(tmp_path)
    (tmp_path / "model.safetensors").unlink()
    (tmp_path / "pytorch_model.bin").write_bytes(b"x")
    assert m._resolve_bge_m3_model_source() == str(tmp_path)
    monkeypatch.setattr(m, "_LOCAL_BGE_M3_DIR", str(tmp_path / "missing"))
    assert m._resolve_bge_m3_model_source() == "BAAI/bge-m3"
    capsys.readouterr()


@pytest.fixture
def restore_llama_settings(monkeypatch):
    saved = {name: getattr(Settings, name) for name in ("_llm", "_embed_model", "_node_parser")}
    monkeypatch.setattr(m, "_MANAGED_EMBED", None)
    monkeypatch.setattr(m, "_LLM_RES", None)
    monkeypatch.setattr(m, "_GLOBAL_LLM", None)
    yield
    for name, value in saved.items():
        setattr(Settings, name, value)


def test_setup_environment_builds_lazy_model_proxies(restore_llama_settings, tmp_path, monkeypatch):
    monkeypatch.setattr(m, "_LOCAL_BGE_M3_DIR", str(tmp_path / "missing"))
    m.setup_environment()
    assert isinstance(Settings.llm, Ollama) and Settings.llm.model == "qwen2:7b"
    assert Settings.llm.context_window == 8192
    assert isinstance(Settings.embed_model, rag_resources.ManagedHFEmbedding)
    assert isinstance(m._LLM_RES, rag_resources.OllamaModelResource) and m._GLOBAL_LLM is Settings.llm
    assert not Settings.embed_model._res.is_loaded


def test_release_system_releases_both_models(monkeypatch):
    calls = []
    monkeypatch.setattr(m, "_MANAGED_EMBED", types.SimpleNamespace(release=lambda: calls.append("embed")))
    monkeypatch.setattr(m, "_LLM_RES", types.SimpleNamespace(release=lambda reason: calls.append(f"llm:{reason}")))
    m.release_system()
    assert calls == ["embed", "llm:explicit"]

"""
消融實驗（Ablation Study）開關邏輯的單元測試。

跟這個 session 一路沿用的做法一樣：用 sys.modules 注入假的 scipy / llama_index /
sqlalchemy / database_mgr，但實際執行 production 程式碼路徑（ablation_config.py、
rag_chat_engine.py、rag_guardrail.py、rag_sql_engine.py 這幾支真正會部署的檔案），
不是另外重寫一份簡化邏輯來測試。

驗證重點：
  1. ablation_config._env_bool() 對各種環境變數字串的解析行為。
  2. 4 個開關預設值（沒有設環境變數）都等於「維持正式系統既有行為」。
  3. 4 個開關關閉時，production 程式碼真的走了消融實驗要測的「對照組」路徑，
     而不是悄悄還是走了原本的路徑（例如「關閉語意防護欄」這個開關關閉時，
     必須真的完全不去呼叫 embedding 模型，不然對照組的意義就沒了）。
"""

import importlib
import os
import sys
import types
import unittest


# ============================================================
# 1. 在 import 任何 production 模組之前，先把假的重量級依賴塞進 sys.modules
# ============================================================

def _install_stub(name, module):
    sys.modules[name] = module
    return module


# ---- scipy.spatial.distance.cosine ----
scipy_mod = types.ModuleType("scipy")
scipy_spatial_mod = types.ModuleType("scipy.spatial")
scipy_spatial_distance_mod = types.ModuleType("scipy.spatial.distance")


def _fake_cosine(a, b):
    # 兩個向量完全一樣就回傳 0（cosine distance），語意相似度 1 - 0 = 1
    return 0.0 if a == b else 1.0


scipy_spatial_distance_mod.cosine = _fake_cosine
scipy_mod.spatial = scipy_spatial_mod
scipy_spatial_mod.distance = scipy_spatial_distance_mod
_install_stub("scipy", scipy_mod)
_install_stub("scipy.spatial", scipy_spatial_mod)
_install_stub("scipy.spatial.distance", scipy_spatial_distance_mod)


# ---- llama_index.core（Settings、SentenceSplitter 等） ----
llama_index_mod = types.ModuleType("llama_index")
llama_index_core_mod = types.ModuleType("llama_index.core")


class _FakeSettings:
    llm = None
    embed_model = None
    text_splitter = None


class _FakeEmbedModel:
    """假的嵌入模型：呼叫次數計數，讓測試可以斷言「語意防護欄關掉時完全沒呼叫到這裡」。"""

    def __init__(self):
        self.call_count = 0

    def get_text_embedding(self, text):
        self.call_count += 1
        return text  # 直接回傳文字本身當「向量」，配合上面 _fake_cosine 用字串相等比對


llama_index_core_mod.Settings = _FakeSettings


class _FakePromptTemplate:
    def __init__(self, template, prompt_type=None):
        self.template = template
        self.prompt_type = prompt_type

    def format(self, **kwargs):
        return self.template.format(**kwargs)


llama_index_core_mod.PromptTemplate = _FakePromptTemplate


class _FakeSQLDatabase:
    def __init__(self, *a, **kw):
        pass


llama_index_core_mod.SQLDatabase = _FakeSQLDatabase
_install_stub("llama_index", llama_index_mod)
_install_stub("llama_index.core", llama_index_core_mod)

# llama_index.core.postprocessor.LongContextReorder
llama_index_core_postprocessor_mod = types.ModuleType("llama_index.core.postprocessor")


class _FakeLongContextReorder:
    pass


llama_index_core_postprocessor_mod.LongContextReorder = _FakeLongContextReorder
_install_stub("llama_index.core.postprocessor", llama_index_core_postprocessor_mod)

# llama_index.core.memory.ChatMemoryBuffer
llama_index_core_memory_mod = types.ModuleType("llama_index.core.memory")


class _FakeChatMemoryBuffer:
    @staticmethod
    def from_defaults(token_limit=3000):
        return object()


llama_index_core_memory_mod.ChatMemoryBuffer = _FakeChatMemoryBuffer
_install_stub("llama_index.core.memory", llama_index_core_memory_mod)

# llama_index.core.query_engine.NLSQLTableQueryEngine
llama_index_core_query_engine_mod = types.ModuleType("llama_index.core.query_engine")


class _FakeNLSQLTableQueryEngine:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def query(self, message):
        raise AssertionError("測試裡不應該真的呼叫到小模型 SQL 查詢引擎（應該被 mock 掉）")


llama_index_core_query_engine_mod.NLSQLTableQueryEngine = _FakeNLSQLTableQueryEngine
_install_stub("llama_index.core.query_engine", llama_index_core_query_engine_mod)

# llama_index.core.prompts / prompt_type
llama_index_core_prompts_mod = types.ModuleType("llama_index.core.prompts")
llama_index_core_prompts_mod.PromptTemplate = _FakePromptTemplate
llama_index_core_prompts_prompt_type_mod = types.ModuleType("llama_index.core.prompts.prompt_type")


class _FakePromptType:
    TEXT_TO_SQL = "text_to_sql"


llama_index_core_prompts_prompt_type_mod.PromptType = _FakePromptType
_install_stub("llama_index.core.prompts", llama_index_core_prompts_mod)
_install_stub("llama_index.core.prompts.prompt_type", llama_index_core_prompts_prompt_type_mod)

# sqlalchemy.create_engine
sqlalchemy_mod = types.ModuleType("sqlalchemy")
sqlalchemy_mod.create_engine = lambda *a, **kw: object()
_install_stub("sqlalchemy", sqlalchemy_mod)


# ---- database_mgr（假的，只記錄呼叫參數） ----
database_mgr_mod = types.ModuleType("database_mgr")
database_mgr_mod.LLM_USAGE_LOG_CALLS = []


def _fake_log_llm_usage(event_type, question, route=None, guardrail_pass=None, similarity_score=None, source="gradio"):
    database_mgr_mod.LLM_USAGE_LOG_CALLS.append(
        {"event_type": event_type, "question": question, "route": route,
         "guardrail_pass": guardrail_pass, "similarity_score": similarity_score}
    )


database_mgr_mod.log_llm_usage = _fake_log_llm_usage
_install_stub("database_mgr", database_mgr_mod)


# ============================================================
# 2. 匯入真正的 production 模組
# ============================================================
import ablation_config  # noqa: E402
import rag_chat_engine  # noqa: E402
import rag_guardrail  # noqa: E402
import rag_sql_engine  # noqa: E402


class TestEnvBoolParsing(unittest.TestCase):
    """ablation_config._env_bool() 的環境變數解析行為。"""

    def test_unset_returns_default(self):
        os.environ.pop("ABLATION_TEST_VAR", None)
        self.assertTrue(ablation_config._env_bool("ABLATION_TEST_VAR", True))
        self.assertFalse(ablation_config._env_bool("ABLATION_TEST_VAR", False))

    def test_falsy_strings(self):
        for v in ("0", "false", "False", "FALSE", "no", "off", ""):
            os.environ["ABLATION_TEST_VAR"] = v
            self.assertFalse(ablation_config._env_bool("ABLATION_TEST_VAR", True), f"值 {v!r} 應該被視為 False")
        os.environ.pop("ABLATION_TEST_VAR", None)

    def test_truthy_strings(self):
        for v in ("1", "true", "True", "yes", "on"):
            os.environ["ABLATION_TEST_VAR"] = v
            self.assertTrue(ablation_config._env_bool("ABLATION_TEST_VAR", False), f"值 {v!r} 應該被視為 True")
        os.environ.pop("ABLATION_TEST_VAR", None)

    def test_default_module_state_matches_production_behavior(self):
        """
        【最重要的一個測試】在完全沒有人設定任何 ABLATION_* 環境變數的情況下（也就是
        正式系統實際執行的情境），4 個開關全部要是 True——代表消融實驗這支功能加進來
        之後，正式系統的既有行為完全沒有被改變。
        """
        for name in (
            "ABLATION_USE_LONG_CONTEXT_REORDER",
            "ABLATION_USE_SEMANTIC_GUARDRAIL",
            "ABLATION_USE_KEYWORD_FASTPATH",
            "ABLATION_USE_DETERMINISTIC_SQL",
        ):
            self.assertNotIn(name, os.environ, f"測試環境不應該殘留 {name}，會汙染這個測試的前提")
        importlib.reload(ablation_config)
        cfg = ablation_config.current_config()
        self.assertTrue(all(cfg.values()), f"預設狀態下所有開關都應該是 True，實際: {cfg}")


class TestLongContextReorderToggle(unittest.TestCase):
    """方向 1 機制 1：rag_chat_engine.build_chat_engine() 的 LongContextReorder 開關。"""

    def setUp(self):
        self.captured_kwargs = {}

        class _FakeIndex:
            def as_chat_engine(_self, **kwargs):
                self.captured_kwargs.update(kwargs)
                return "fake_chat_engine"

        self.fake_index = _FakeIndex()

    def tearDown(self):
        ablation_config.USE_LONG_CONTEXT_REORDER = True

    def test_reorder_on_by_default(self):
        ablation_config.USE_LONG_CONTEXT_REORDER = True
        rag_chat_engine.build_chat_engine(self.fake_index)
        postprocessors = self.captured_kwargs["node_postprocessors"]
        self.assertEqual(len(postprocessors), 1)
        self.assertIsInstance(postprocessors[0], _FakeLongContextReorder)

    def test_reorder_off_when_ablated(self):
        ablation_config.USE_LONG_CONTEXT_REORDER = False
        rag_chat_engine.build_chat_engine(self.fake_index)
        postprocessors = self.captured_kwargs["node_postprocessors"]
        self.assertEqual(postprocessors, [], "關閉消融開關時，node_postprocessors 應該是空清單")


class TestSemanticGuardrailToggle(unittest.TestCase):
    """方向 1 機制 2：check_semantic_guardrail() 的語意防護欄 vs. 純關鍵字過濾。"""

    def setUp(self):
        database_mgr_mod.LLM_USAGE_LOG_CALLS.clear()
        self.fake_embed_model = _FakeEmbedModel()
        llama_index_core_mod.Settings.embed_model = self.fake_embed_model

    def tearDown(self):
        ablation_config.USE_SEMANTIC_GUARDRAIL = True

    def test_semantic_on_by_default_calls_embedding_model(self):
        ablation_config.USE_SEMANTIC_GUARDRAIL = True
        rag_guardrail.check_semantic_guardrail("查詢陳柏豫的打卡紀錄")
        self.assertGreater(self.fake_embed_model.call_count, 0, "語意防護欄開啟時應該有呼叫嵌入模型")

    def test_semantic_off_never_touches_embedding_model(self):
        """
        【關鍵】關掉語意防護欄時，必須完全不呼叫嵌入模型——這才是真正的「純關鍵字過濾」
        對照組，不是掛羊頭賣狗肉還是偷偷跑語意相似度。
        """
        ablation_config.USE_SEMANTIC_GUARDRAIL = False
        result = rag_guardrail.check_semantic_guardrail("查詢陳柏豫的打卡紀錄")
        self.assertEqual(self.fake_embed_model.call_count, 0, "語意防護欄關閉時，不應該呼叫嵌入模型")
        self.assertTrue(result, "「打卡」是關鍵字詞庫裡的詞，應該放行")

    def test_semantic_off_blocks_unrelated_question(self):
        ablation_config.USE_SEMANTIC_GUARDRAIL = False
        result = rag_guardrail.check_semantic_guardrail("幫我寫一首關於愛情的詩")
        self.assertFalse(result, "純關鍵字過濾對照組：完全沒命中任何關鍵字的問題應該被攔截")

    def test_semantic_off_still_logs_audit_event(self):
        ablation_config.USE_SEMANTIC_GUARDRAIL = False
        rag_guardrail.check_semantic_guardrail("員工的打卡狀態")
        self.assertEqual(len(database_mgr_mod.LLM_USAGE_LOG_CALLS), 1)
        self.assertEqual(database_mgr_mod.LLM_USAGE_LOG_CALLS[0]["event_type"], "guardrail")


class TestKeywordFastpathToggle(unittest.TestCase):
    """方向 1 機制 3：check_query_route() 的關鍵字快速通道 vs. 純語意分類。"""

    def setUp(self):
        database_mgr_mod.LLM_USAGE_LOG_CALLS.clear()
        self.fake_embed_model = _FakeEmbedModel()
        llama_index_core_mod.Settings.embed_model = self.fake_embed_model
        # check_query_route() 一開始會呼叫 rag_sql_engine._get_known_employee_names()
        # 判斷 mixed 題型；這裡讓它回傳空清單，確保下面測的句子不會被誤判成 mixed，
        # 干擾到我們真正要測的「快速通道」開關。
        self._orig_get_names = rag_sql_engine._get_known_employee_names
        rag_sql_engine._get_known_employee_names = lambda: []

    def tearDown(self):
        ablation_config.USE_KEYWORD_FASTPATH = True
        rag_sql_engine._get_known_employee_names = self._orig_get_names

    def test_fastpath_on_by_default_skips_embedding(self):
        ablation_config.USE_KEYWORD_FASTPATH = True
        route = rag_guardrail.check_query_route("請問我的打卡時間是幾點？")
        self.assertEqual(route, "attendance")
        self.assertEqual(self.fake_embed_model.call_count, 0, "命中快速通道時不應該再呼叫嵌入模型算語意相似度")

    def test_fastpath_off_falls_through_to_semantic_classification(self):
        """
        【關鍵】關掉快速通道時，即使句子命中「打卡時間」這種快速通道關鍵字，也必須真的
        走到下面的語意相似度分類（呼叫嵌入模型），不能因為關鍵字還在字串裡就悄悄還是
        走快速通道那條路。
        """
        ablation_config.USE_KEYWORD_FASTPATH = False
        rag_guardrail.check_query_route("請問我的打卡時間是幾點？")
        self.assertGreater(self.fake_embed_model.call_count, 0, "快速通道關閉時應該有呼叫嵌入模型走語意分類")


class TestDeterministicSqlToggle(unittest.TestCase):
    """方向 1 機制 4：try_attendance_answer() 的確定性日期查詢 vs. 全部交給小模型。"""

    def setUp(self):
        # 【測試用途，不是產品邏輯】這裡直接置換 _try_deterministic_date_lookup() 本身，
        # 不是要重測它內部真正連 SQLite 資料庫查資料那段邏輯（那不是這個測試類別要驗證
        # 的東西，資料庫層級的確定性查詢邏輯本來就沒有被這次消融實驗改動）。這裡只在乎
        # 「開關開著時，try_attendance_answer() 有沒有呼叫到這個函式；開關關掉時，有沒有
        # 完全跳過它、直接去問小模型引擎」，所以用一個「一定會回傳答案」的假版本最單純。
        self._orig_deterministic_lookup = rag_sql_engine._try_deterministic_date_lookup
        rag_sql_engine._try_deterministic_date_lookup = lambda message: "確定性查詢查到的答案"

        class _FakeEngine:
            def __init__(self):
                self.query_called_with = None

            def query(self, message):
                self.query_called_with = message

                class _Resp:
                    metadata = {"sql_query": "SELECT 1", "result": [(1,)]}

                    def __str__(self):
                        return "小模型翻譯出來的答案"

                return _Resp()

        self.fake_engine = _FakeEngine()

    def tearDown(self):
        ablation_config.USE_DETERMINISTIC_SQL = True
        rag_sql_engine._try_deterministic_date_lookup = self._orig_deterministic_lookup

    def test_deterministic_on_by_default_skips_engine_for_name_plus_date(self):
        ablation_config.USE_DETERMINISTIC_SQL = True
        ok, answer = rag_sql_engine.try_attendance_answer(self.fake_engine, "陳柏豫在 2026-09-08 有打卡嗎？")
        self.assertTrue(ok)
        self.assertIsNone(self.fake_engine.query_called_with, "確定性查詢命中時，不應該再呼叫小模型引擎")
        self.assertEqual(answer, "確定性查詢查到的答案")

    def test_deterministic_off_always_goes_through_engine(self):
        """
        【關鍵】關掉確定性查詢時，即使句子完全符合「姓名+具體日期」這個原本會被攔截的
        句型，也必須真的把問題交給小模型引擎（NLSQLTableQueryEngine），驗證對照組
        「全部交給小模型翻譯 SQL」是真的有在跑，不是形同虛設。
        """
        ablation_config.USE_DETERMINISTIC_SQL = False
        ok, answer = rag_sql_engine.try_attendance_answer(self.fake_engine, "陳柏豫在 2026-09-08 有打卡嗎？")
        self.assertTrue(ok)
        self.assertEqual(self.fake_engine.query_called_with, "陳柏豫在 2026-09-08 有打卡嗎？")
        self.assertEqual(answer, "小模型翻譯出來的答案")


if __name__ == "__main__":
    unittest.main()

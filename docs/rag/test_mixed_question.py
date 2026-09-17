"""
混合型問題聯合查詢（考勤＋法規）功能的 stub 測試。

沿用這個 session 之前建立的測試模式：用 sys.modules 注入假模組，避免需要真正安裝
scipy / llama_index / Ollama / bge-m3 這些重量級依賴，但仍然執行真正的 production
程式碼路徑（rag_guardrail._detect_mixed_question / check_query_route，以及
main_guardrail_rag._answer_mixed_question / stream_chat_response / get_chat_response）。

驗證重點：
  1. _detect_mixed_question()：正確的 true/false case，特別是「不能讓 2026-09-02
     那次已經驗證修好的『員工遲到是否可以扣除整天薪水』這句話被誤判成 mixed」這個
     迴歸測試。
  2. check_query_route()：mixed 判斷要擋在關鍵字快速通道跟語意相似度分類「之前」，
     且不能讓原本已經驗證過的 attendance / law 分類結果跑掉（迴歸測試）。
  3. _answer_mixed_question()：兩邊都查到 → 呼叫 LLM 整合；只有一邊查到 → 直接用
     那一邊；兩邊都查不到 → 老實說查無資料；LLM 整合失敗 → 退而求其次接兩段原文。
  4. stream_chat_response() / get_chat_response()：mixed 路由有被正確接到
     _answer_mixed_question()，且回傳值符合各自的呼叫慣例（yield vs return）。
"""

import sys
import types
import unittest
from unittest.mock import MagicMock


# ==========================================
# 1. 在 import 任何專案程式碼之前，先把重量級依賴用假模組頂替掉
# ==========================================

# ---- scipy.spatial.distance.cosine ----
scipy_mod = types.ModuleType("scipy")
scipy_spatial_mod = types.ModuleType("scipy.spatial")
scipy_spatial_distance_mod = types.ModuleType("scipy.spatial.distance")


def _fake_cosine(a, b):
    # 測試不會真的走到語意相似度分支（mixed 判斷是純關鍵字 + 姓名比對），
    # 但 rag_guardrail.py 頂部有 `from scipy.spatial.distance import cosine`，
    # 一定要有東西可以 import 進來，這裡給一個永遠回傳 0 的假函式即可。
    return 0.0


scipy_spatial_distance_mod.cosine = _fake_cosine
scipy_mod.spatial = scipy_spatial_mod
scipy_spatial_mod.distance = scipy_spatial_distance_mod
sys.modules["scipy"] = scipy_mod
sys.modules["scipy.spatial"] = scipy_spatial_mod
sys.modules["scipy.spatial.distance"] = scipy_spatial_distance_mod

# ---- llama_index.core.Settings（帶一個假的 embed_model） ----
llama_index_mod = types.ModuleType("llama_index")
llama_index_core_mod = types.ModuleType("llama_index.core")


class _FakeSettings:
    embed_model = None  # 每個測試視需要自己指定


llama_index_core_mod.Settings = _FakeSettings
llama_index_mod.core = llama_index_core_mod
sys.modules["llama_index"] = llama_index_mod
sys.modules["llama_index.core"] = llama_index_core_mod

# main_guardrail_rag.py 額外需要這幾個子模組（只在 import 時期用到，測試不會真的
# 呼叫 init_system()/setup_environment()，所以這裡的假類別/假函式內容不重要）。
llama_index_core_node_parser_mod = types.ModuleType("llama_index.core.node_parser")
llama_index_core_node_parser_mod.SentenceSplitter = MagicMock()
sys.modules["llama_index.core.node_parser"] = llama_index_core_node_parser_mod
llama_index_core_mod.node_parser = llama_index_core_node_parser_mod

llama_index_llms_mod = types.ModuleType("llama_index.llms")
llama_index_llms_ollama_mod = types.ModuleType("llama_index.llms.ollama")
llama_index_llms_ollama_mod.Ollama = MagicMock()
sys.modules["llama_index.llms"] = llama_index_llms_mod
sys.modules["llama_index.llms.ollama"] = llama_index_llms_ollama_mod
llama_index_mod.llms = llama_index_llms_mod
llama_index_llms_mod.ollama = llama_index_llms_ollama_mod

llama_index_embeddings_mod = types.ModuleType("llama_index.embeddings")
llama_index_embeddings_hf_mod = types.ModuleType("llama_index.embeddings.huggingface")
llama_index_embeddings_hf_mod.HuggingFaceEmbedding = MagicMock()
sys.modules["llama_index.embeddings"] = llama_index_embeddings_mod
sys.modules["llama_index.embeddings.huggingface"] = llama_index_embeddings_hf_mod
llama_index_mod.embeddings = llama_index_embeddings_mod
llama_index_embeddings_mod.huggingface = llama_index_embeddings_hf_mod

# ---- database_mgr：只需要 log_llm_usage 這個函式存在，內容不重要 ----
database_mgr_mod = types.ModuleType("database_mgr")
database_mgr_mod.log_llm_usage = MagicMock()
sys.modules["database_mgr"] = database_mgr_mod

# ---- rag_sql_engine：用 MagicMock 頂替，測試裡逐案改 side_effect / return_value ----
rag_sql_engine_mod = types.ModuleType("rag_sql_engine")
rag_sql_engine_mod._get_known_employee_names = MagicMock(return_value=["陳柏豫", "葉君緯", "周宇祥"])
rag_sql_engine_mod.try_attendance_answer = MagicMock(return_value=(False, None))
rag_sql_engine_mod.sample_random_attendance_record = MagicMock()
rag_sql_engine_mod.build_sql_engine = MagicMock()
sys.modules["rag_sql_engine"] = rag_sql_engine_mod

# 到這裡才能安全 import 真正的 production 程式碼
sys.path.insert(0, "/home/claude/work/mixed_feature")
import rag_guardrail  # noqa: E402


class TestDetectMixedQuestion(unittest.TestCase):
    """_detect_mixed_question()：真實姓名 + 法規詞組，兩個條件都要滿足。"""

    def test_name_plus_law_keyword_is_mixed(self):
        self.assertTrue(rag_guardrail._detect_mixed_question("陳柏豫這個月遲到扣薪上限多少"))

    def test_name_plus_law_keyword_variant(self):
        self.assertTrue(rag_guardrail._detect_mixed_question("葉君緯這個月遲到會不會違法扣他薪水"))

    def test_law_keyword_without_real_name_is_not_mixed(self):
        # 【迴歸測試，最重要的一案】2026-09-02 已經驗證修好的句子，絕對不能被
        # mixed 判斷「撿回去」誤判——句子裡沒有真實姓名，只有泛稱「員工」。
        self.assertFalse(rag_guardrail._detect_mixed_question("員工遲到是否可以扣除整天薪水"))

    def test_name_without_law_keyword_is_not_mixed(self):
        self.assertFalse(rag_guardrail._detect_mixed_question("陳柏豫今天幾點打卡"))

    def test_unrelated_name_lookalike_is_not_mixed(self):
        # 句子裡出現的不是資料庫裡真實存在的姓名（張三不在 _get_known_employee_names 清單裡）
        self.assertFalse(rag_guardrail._detect_mixed_question("張三遲到扣薪上限多少"))

    def test_empty_string_is_not_mixed(self):
        self.assertFalse(rag_guardrail._detect_mixed_question(""))

    def test_none_is_not_mixed(self):
        self.assertFalse(rag_guardrail._detect_mixed_question(None))

    def test_roster_lookup_failure_falls_back_to_false(self):
        rag_sql_engine_mod._get_known_employee_names.side_effect = Exception("db locked")
        try:
            self.assertFalse(rag_guardrail._detect_mixed_question("陳柏豫這個月遲到扣薪上限多少"))
        finally:
            rag_sql_engine_mod._get_known_employee_names.side_effect = None
            rag_sql_engine_mod._get_known_employee_names.return_value = ["陳柏豫", "葉君緯", "周宇祥"]


class TestCheckQueryRouteRegression(unittest.TestCase):
    """check_query_route()：mixed 判斷不能讓既有的 attendance / law / 快速通道分類跑掉。"""

    def setUp(self):
        database_mgr_mod.log_llm_usage.reset_mock()

    def test_mixed_case_short_circuits_before_fast_path_and_embedding(self):
        route = rag_guardrail.check_query_route("陳柏豫這個月遲到扣薪上限多少")
        self.assertEqual(route, "mixed")
        # 應該有記錄稽核，且 route 是 mixed，不需要算語意相似度
        database_mgr_mod.log_llm_usage.assert_called_once()
        _, kwargs = database_mgr_mod.log_llm_usage.call_args
        self.assertEqual(kwargs.get("route"), "mixed")

    def test_fast_path_keyword_still_works_when_not_mixed(self):
        # 「打卡狀態」命中快速通道，句子裡沒有法規詞組，不該被誤判成 mixed
        route = rag_guardrail.check_query_route("陳柏豫的打卡狀態是什麼")
        self.assertEqual(route, "attendance")

    def test_pure_law_sentence_still_falls_through_to_embedding(self):
        # 【迴歸測試】這句已經驗證修好的句子，要繼續往下走到語意相似度分類，
        # 不能被 mixed 判斷攔截掉。用假的 Settings.embed_model 讓 sim_law > sim_att。
        fake_embed_model = MagicMock()
        fake_embed_model.get_text_embedding.return_value = [0.0]
        rag_guardrail.Settings.embed_model = fake_embed_model

        # _fake_cosine 固定回傳 0.0，所以 sim_att == sim_law == 1.0，
        # route 是 "attendance"（因為程式碼寫 sim_att > sim_law 才算 attendance，平手算 law）。
        # 這裡只需要確認：不是被 mixed 攔截，而是真的走到語意相似度那條路。
        route = rag_guardrail.check_query_route("員工遲到是否可以扣除整天薪水")
        self.assertIn(route, ("attendance", "law"))  # 有算到語意相似度，兩者皆可接受
        # 關鍵斷言：不是 mixed
        self.assertNotEqual(route, "mixed")


class TestAnswerMixedQuestion(unittest.TestCase):
    """main_guardrail_rag._answer_mixed_question()：四種組合情境。"""

    def setUp(self):
        # main_guardrail_rag.py import 了 rag_index / rag_chat_engine，這兩個檔案
        # 目前的測試不需要真的載入，用假模組頂替，避免要求真的 FAISS / 索引檔案。
        for mod_name in ("rag_index", "rag_chat_engine"):
            fake_mod = types.ModuleType(mod_name)
            fake_mod.load_or_build_index = MagicMock()
            fake_mod.build_chat_engine = MagicMock()
            sys.modules[mod_name] = fake_mod

        import importlib
        global main_guardrail_rag
        if "main_guardrail_rag" in sys.modules:
            importlib.reload(sys.modules["main_guardrail_rag"])
            main_guardrail_rag = sys.modules["main_guardrail_rag"]
        else:
            import main_guardrail_rag  # noqa: E402

        main_guardrail_rag._GLOBAL_CHAT_ENGINE = MagicMock()
        main_guardrail_rag._GLOBAL_SQL_ENGINE = MagicMock()
        main_guardrail_rag._GLOBAL_LLM = MagicMock()

    def _fake_law_response(self, text, node_ids=("n1",)):
        resp = MagicMock()
        resp.__str__.return_value = text
        resp.source_nodes = [MagicMock(node=MagicMock(node_id=nid)) for nid in node_ids]
        return resp

    def test_both_sides_found_calls_llm_synthesis(self):
        rag_sql_engine_mod.try_attendance_answer.return_value = (True, "陳柏豫這個月遲到 5 次。")
        main_guardrail_rag._GLOBAL_CHAT_ENGINE.chat.return_value = self._fake_law_response(
            "遲到扣薪上限為當日工資的一半。"
        )
        main_guardrail_rag._GLOBAL_LLM.complete.return_value = "陳柏豫這個月遲到 5 次，依規定扣薪上限為當日工資的一半。"

        answer = main_guardrail_rag._answer_mixed_question("陳柏豫這個月遲到扣薪上限多少")

        self.assertIn("陳柏豫", answer)
        main_guardrail_rag._GLOBAL_LLM.complete.assert_called_once()
        # 整合提示詞要同時包含兩邊原始資料，不能漏掉任何一邊
        prompt_arg = main_guardrail_rag._GLOBAL_LLM.complete.call_args[0][0]
        self.assertIn("遲到 5 次", prompt_arg)
        self.assertIn("扣薪上限為當日工資的一半", prompt_arg)

    def test_only_attendance_found_returns_attendance_answer_directly(self):
        rag_sql_engine_mod.try_attendance_answer.return_value = (True, "陳柏豫這個月遲到 5 次。")
        main_guardrail_rag._GLOBAL_CHAT_ENGINE.chat.return_value = self._fake_law_response("", node_ids=())

        answer = main_guardrail_rag._answer_mixed_question("陳柏豫這個月遲到扣薪上限多少")

        self.assertEqual(answer, "陳柏豫這個月遲到 5 次。")
        main_guardrail_rag._GLOBAL_LLM.complete.assert_not_called()

    def test_only_law_found_returns_law_answer_directly(self):
        rag_sql_engine_mod.try_attendance_answer.return_value = (False, None)
        main_guardrail_rag._GLOBAL_CHAT_ENGINE.chat.return_value = self._fake_law_response(
            "遲到扣薪上限為當日工資的一半。"
        )

        answer = main_guardrail_rag._answer_mixed_question("陳柏豫這個月遲到扣薪上限多少")

        self.assertEqual(answer, "遲到扣薪上限為當日工資的一半。")
        main_guardrail_rag._GLOBAL_LLM.complete.assert_not_called()

    def test_neither_side_found_returns_honest_no_data(self):
        rag_sql_engine_mod.try_attendance_answer.return_value = (False, None)
        main_guardrail_rag._GLOBAL_CHAT_ENGINE.chat.return_value = self._fake_law_response("", node_ids=())

        answer = main_guardrail_rag._answer_mixed_question("陳柏豫這個月遲到扣薪上限多少")

        self.assertEqual(answer, "查無符合條件的資料。")

    def test_llm_synthesis_failure_falls_back_to_concatenation(self):
        rag_sql_engine_mod.try_attendance_answer.return_value = (True, "陳柏豫這個月遲到 5 次。")
        main_guardrail_rag._GLOBAL_CHAT_ENGINE.chat.return_value = self._fake_law_response(
            "遲到扣薪上限為當日工資的一半。"
        )
        main_guardrail_rag._GLOBAL_LLM.complete.side_effect = Exception("Ollama timeout")

        answer = main_guardrail_rag._answer_mixed_question("陳柏豫這個月遲到扣薪上限多少")

        self.assertIn("陳柏豫這個月遲到 5 次。", answer)
        self.assertIn("遲到扣薪上限為當日工資的一半。", answer)

    def test_sql_engine_none_does_not_crash(self):
        main_guardrail_rag._GLOBAL_SQL_ENGINE = None
        main_guardrail_rag._GLOBAL_CHAT_ENGINE.chat.return_value = self._fake_law_response(
            "遲到扣薪上限為當日工資的一半。"
        )
        answer = main_guardrail_rag._answer_mixed_question("陳柏豫這個月遲到扣薪上限多少")
        self.assertEqual(answer, "遲到扣薪上限為當日工資的一半。")

    def test_law_chat_engine_exception_does_not_crash(self):
        rag_sql_engine_mod.try_attendance_answer.return_value = (True, "陳柏豫這個月遲到 5 次。")
        main_guardrail_rag._GLOBAL_CHAT_ENGINE.chat.side_effect = Exception("index not loaded")
        answer = main_guardrail_rag._answer_mixed_question("陳柏豫這個月遲到扣薪上限多少")
        self.assertEqual(answer, "陳柏豫這個月遲到 5 次。")


class TestRoutingWiring(unittest.TestCase):
    """stream_chat_response() / get_chat_response() 的 mixed 分支有沒有正確接上。"""

    def setUp(self):
        for mod_name in ("rag_index", "rag_chat_engine"):
            fake_mod = types.ModuleType(mod_name)
            fake_mod.load_or_build_index = MagicMock()
            fake_mod.build_chat_engine = MagicMock()
            sys.modules[mod_name] = fake_mod

        import importlib
        global main_guardrail_rag
        if "main_guardrail_rag" in sys.modules:
            importlib.reload(sys.modules["main_guardrail_rag"])
        import main_guardrail_rag  # noqa: E402
        self.m = main_guardrail_rag

        self.m._GLOBAL_CHAT_ENGINE = MagicMock()
        self.m._GLOBAL_SQL_ENGINE = MagicMock()
        self.m._GLOBAL_LLM = MagicMock()

        # check_semantic_guardrail() 會在 check_query_route() 之前先跑一次，
        # 需要一個假的 embed_model 才不會因為 Settings.embed_model is None 而炸掉。
        fake_embed_model = MagicMock()
        fake_embed_model.get_text_embedding.return_value = [0.0]
        rag_guardrail.Settings.embed_model = fake_embed_model

        rag_sql_engine_mod.try_attendance_answer.return_value = (True, "陳柏豫這個月遲到 5 次。")
        law_resp = MagicMock()
        law_resp.__str__.return_value = "遲到扣薪上限為當日工資的一半。"
        law_resp.source_nodes = [MagicMock(node=MagicMock(node_id="n1"))]
        self.m._GLOBAL_CHAT_ENGINE.chat.return_value = law_resp
        self.m._GLOBAL_LLM.complete.return_value = "整合後的答案"

    def test_get_chat_response_routes_mixed(self):
        result = self.m.get_chat_response("陳柏豫這個月遲到扣薪上限多少", route="RAG", history=[])
        self.assertEqual(result, "整合後的答案")
        self.assertEqual(self.m.get_last_query_route(), "mixed")

    def test_stream_chat_response_routes_mixed(self):
        chunks = list(self.m.stream_chat_response("陳柏豫這個月遲到扣薪上限多少", route="RAG", history=[]))
        self.assertEqual(chunks, ["整合後的答案"])
        self.assertEqual(self.m.get_last_query_route(), "mixed")


if __name__ == "__main__":
    unittest.main(verbosity=2)

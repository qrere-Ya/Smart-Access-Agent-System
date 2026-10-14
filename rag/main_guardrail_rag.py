"""
main_guardrail_rag.py：RAG 法務/考勤助理的統籌層。

主要功能：
  - 初始化模型與引擎：Ollama LLM、bge-m3 embedding、語意切塊器、法規聊天引擎、SQL 引擎
  - 回答流程：防護欄判斷 → 路由分類（法規 / 考勤 / 混合）→ 交給對應引擎，查無資料時互相 fallback
  - 記錄上一次問答走的路由與檢索到的知識塊，供自動化評估使用

串接模組：
  - rag_index：法規向量索引
  - rag_sql_engine：考勤資料即時 SQL 查詢
  - rag_guardrail：防護欄與路由分類
  - rag_chat_engine：法規對話引擎
  - rag_resources：bge-m3 與 Ollama 的資源管理
  - database_mgr：寫入問答稽核紀錄
  - app.py、evaluate_rag.py：呼叫本模組回答問題
"""

import os

from shared import paths

# HF_HUB_OFFLINE 在 huggingface_hub 第一次被 import 時就讀取並固定，
# 所以必須在下面 import llama_index 之前判斷：本機有 bge-m3 快取就設為離線模式
_hf_home = os.environ.get("HF_HOME") or os.path.join(
    os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache"),
    "huggingface",
)
_hf_hub_cache = os.environ.get("HF_HUB_CACHE") or os.path.join(_hf_home, "hub")
if os.path.isdir(os.path.join(_hf_hub_cache, "models--BAAI--bge-m3")):
    os.environ.setdefault("HF_HUB_OFFLINE", "1")

# 優先用專案 models/bge-m3 的本機完整模型，完全不連網
_LOCAL_BGE_M3_DIR = os.path.join(paths.MODELS_DIR, "bge-m3")


def _resolve_bge_m3_model_source():
    """回傳 embedding 模型來源：本機模型完整就回傳資料夾路徑，否則回傳 repo id 並提示改用連網載入。"""
    _config_ok = os.path.isfile(os.path.join(_LOCAL_BGE_M3_DIR, "config.json"))
    _has_weight_file = False
    if os.path.isdir(_LOCAL_BGE_M3_DIR):
        for _fname in os.listdir(_LOCAL_BGE_M3_DIR):
            if _fname.endswith(".safetensors") or _fname == "pytorch_model.bin":
                _has_weight_file = True
                break
    if _config_ok and _has_weight_file:
        print(f"✅ [BAAI/bge-m3] 偵測到本機已有完整模型（{_LOCAL_BGE_M3_DIR}），"
              f"直接用本機檔案載入，完全不會連網。")
        return _LOCAL_BGE_M3_DIR
    else:
        print(f"⚠️ [BAAI/bge-m3] 本機 {_LOCAL_BGE_M3_DIR} 還沒有完整的模型檔案"
              f"（models/bge-m3 還沒放入完整模型，或檔案不完整），"
              f"暫時改用連網方式從 Hugging Face Hub 直接讀取 repo id \"BAAI/bge-m3\""
              f"——這種方式在完全沒有網路的環境下會失敗。建議把完整的 bge-m3 模型"
              f"放到上述資料夾，之後就不用再連網了。")
        return "BAAI/bge-m3"


import re

from llama_index.core import Settings
from llama_index.core.node_parser import SemanticSplitterNodeParser
from llama_index.llms.ollama import Ollama
from rag import rag_resources

from rag import rag_index
from rag import rag_sql_engine
from rag import rag_guardrail
from rag import rag_chat_engine
from shared import database_mgr

# 對外相容：app.py、evaluate_rag.py 直接從本模組 import 這兩個函式
load_or_build_index = rag_index.load_or_build_index
sample_random_attendance_record = rag_sql_engine.sample_random_attendance_record


# --- 全域狀態：避免重複載入 ---
_GLOBAL_CHAT_ENGINE = None
_GLOBAL_SQL_ENGINE = None
_GLOBAL_LLM = None
# 受協調器管理的元件，重複初始化時要重用
_MANAGED_EMBED = None
_LLM_RES = None
# 上一次法規問答檢索到的知識塊 ID，供評估計算檢索命中率
_LAST_SOURCE_NODE_IDS = []
# 上一次問答走的路由：'law' / 'attendance' / 'blocked' / 'basic' 等，供評估診斷
_LAST_QUERY_ROUTE = None


# NLTK 預設斷句對中文標點無效，改用句尾標點或換行切句
_CJK_SENTENCE_SPLIT_PATTERN = re.compile(r"(?<=[。！？；\n])")


def _split_chinese_sentences(text: str):
    """依中文句尾標點與換行把文字切成句子清單"""
    parts = _CJK_SENTENCE_SPLIT_PATTERN.split(text)
    return [p for p in parts if p.strip()]


class _SkipSelfTest(Exception):
    pass


def setup_environment():
    """建立 LLM、embedding 代理與語意切塊器，並設定到 llama_index 全域 Settings"""
    global _GLOBAL_LLM, _MANAGED_EMBED, _LLM_RES
    print("🤖 [系統初始化] 載入本地 Edge AI 模型中...")
    # context_window 限制在 8192，避免 KV cache 佔用過多記憶體導致載入失敗
    Settings.llm = Ollama(
        model="qwen2:7b", request_timeout=600.0, temperature=0.0, context_window=8192
    )

    _offline_now = os.environ.get("HF_HUB_OFFLINE") == "1"
    _bge_m3_source = _resolve_bge_m3_model_source()
    _using_local_model = _bge_m3_source != "BAAI/bge-m3"
    if not _using_local_model:
        print(f"🤖 [系統初始化] BAAI/bge-m3 embedding 模型載入中"
              f"（HF_HUB_OFFLINE={'1，離線模式，不會連網' if _offline_now else '未設定，連網模式'}）...")
    # 只建立代理，第一次需要向量時才載入權重
    if _MANAGED_EMBED is None:
        _MANAGED_EMBED = rag_resources.ManagedHFEmbedding(_bge_m3_source)
    Settings.embed_model = _MANAGED_EMBED
    if _LLM_RES is None:
        _LLM_RES = rag_resources.OllamaModelResource("qwen2:7b")

    # 設定 RAG_EMBED_SELFTEST=1 時，啟動就實際跑一次 embedding 確認維度（bge-m3 應為 1024）
    try:
        if os.environ.get("RAG_EMBED_SELFTEST") != "1":
            raise _SkipSelfTest()
        _test_vec = Settings.embed_model.get_text_embedding("測試句子，確認 embedding 模型真的能正常運作")
        print(f"🤖 [系統初始化] embedding 模型自我測試：實際輸出向量維度 = {len(_test_vec)}"
              f"（bge-m3 正常應該是 1024 維，數字不對或這行沒印出來，代表上面那個"
              f"「成功」是假的，模型其實沒有正常運作）")
    except _SkipSelfTest:
        pass
    except Exception as e:
        print(f"❌ [系統初始化] embedding 模型自我測試失敗（{type(e).__name__}: {e}）——"
              f"代表雖然上面沒有在載入階段丟例外，但這個模型實際上不能用，"
              f"需要再往下查真正原因。")

    # 依語意相似度切塊（取代固定字數切塊）。切塊邏輯改變後，需手動刪除 runtime/faiss_storage/ 才會重建索引
    Settings.text_splitter = SemanticSplitterNodeParser.from_defaults(
        embed_model=Settings.embed_model,
        breakpoint_percentile_threshold=95,
        buffer_size=1,
        sentence_splitter=_split_chinese_sentences,
    )
    _GLOBAL_LLM = Settings.llm


def init_system():
    """初始化模型，載入法規索引，建立法規對話引擎與考勤 SQL 引擎"""
    global _GLOBAL_CHAT_ENGINE, _GLOBAL_SQL_ENGINE, _GLOBAL_LLM
    setup_environment()
    index = rag_index.load_or_build_index()
    _GLOBAL_CHAT_ENGINE = rag_chat_engine.build_chat_engine(index)
    _GLOBAL_SQL_ENGINE = rag_sql_engine.build_sql_engine()
    _GLOBAL_LLM = Settings.llm


def release_system():
    """釋放 bge-m3 與 Ollama 模型"""
    if _MANAGED_EMBED is not None:
        _MANAGED_EMBED.release()
    if _LLM_RES is not None:
        _LLM_RES.release("explicit")


def get_last_retrieved_node_ids():
    """回傳上一次法規問答檢索到的知識塊 ID 清單（評估用）"""
    return list(_LAST_SOURCE_NODE_IDS)


def get_last_query_route():
    """回傳上一次問答實際走的路由（評估用）"""
    return _LAST_QUERY_ROUTE


# 混合型問題的整合提示詞：把考勤查詢結果與法規說明合成一段回答
_MIXED_ANSWER_PROMPT_ZH = (
    "你是門禁系統的人事助理，使用者問了一個同時牽涉「實際打卡資料」跟「勞動法規」"
    "的問題，下面分別是從考勤資料庫查到的資料，以及從法規知識庫查到的說明，"
    "請把兩者整合成一段通順、繁體中文的完整回答。\n"
    "使用者問題: {query_str}\n"
    "考勤資料庫查詢結果: {attendance_info}\n"
    "法規知識庫說明: {law_info}\n"
    "回答規則：\n"
    "1. 兩邊資料都要用到，不要只回答其中一邊、把另一邊資料丟掉不管。\n"
    "2. 只根據上面提供的兩段資料回答，不要自己瞎猜或補充上面沒有的資訊。\n"
    "3. 回答要精準、簡短，先講考勤的實際數字，再講對應的法規結論。\n"
    "回答: "
)


def _answer_mixed_question(message: str) -> str:
    """
    混合型問題（同時查考勤與法規）：兩邊各自獨立查詢，都查到才交給 LLM 整合；
    只有一邊有結果就直接回該邊答案，整合失敗則把兩段原始答案接起來，兩邊都沒有回查無資料。
    """
    global _LAST_SOURCE_NODE_IDS

    attendance_answer = None
    if _GLOBAL_SQL_ENGINE is not None:
        try:
            ok, ans = rag_sql_engine.try_attendance_answer(_GLOBAL_SQL_ENGINE, message)
            if ok:
                attendance_answer = ans
        except Exception as e:
            print(f"DEBUG: Mixed-Attendance Error: {e}")
    print(f"  [Debug Mixed] 考勤側查詢結果: {attendance_answer!r}")

    law_answer = None
    law_node_ids = []
    try:
        law_response = _GLOBAL_CHAT_ENGINE.chat(message)
        law_node_ids = [n.node.node_id for n in getattr(law_response, "source_nodes", [])]
        if law_node_ids and law_response and str(law_response).strip():
            law_answer = str(law_response)
    except Exception as e:
        print(f"DEBUG: Mixed-Law Error: {e}")
    print(f"  [Debug Mixed] 法規側查詢結果: {law_answer!r}")

    if attendance_answer and law_answer:
        print("  [Debug Mixed] 兩邊都查到，呼叫 LLM 整合成一句回答")
        _LAST_SOURCE_NODE_IDS = law_node_ids
        synthesis_prompt = _MIXED_ANSWER_PROMPT_ZH.format(
            query_str=message, attendance_info=attendance_answer, law_info=law_answer
        )
        try:
            combined = str(_GLOBAL_LLM.complete(synthesis_prompt))
            if combined.strip():
                return combined
        except Exception as e:
            print(f"DEBUG: Mixed-Synthesis Error: {e}")
        # 整合失敗，直接接起來回傳
        print("  [Debug Mixed] LLM 整合失敗，改用兩段原始答案直接接起來")
        return f"【考勤資料】{attendance_answer}\n【法規說明】{law_answer}"

    if attendance_answer:
        print("  [Debug Mixed] 只有考勤側查到，法規側沒有可用結果，直接回考勤答案")
        return attendance_answer
    if law_answer:
        print("  [Debug Mixed] 只有法規側查到，考勤側沒有可用結果，直接回法規答案"
              "（注意：這個答案完全沒有實際查過考勤資料，如果內容聽起來像在講考勤事實，是法規對話引擎自己講的，不是真的查詢結果）")
        _LAST_SOURCE_NODE_IDS = law_node_ids
        return law_answer

    print("  [Debug Mixed] 兩邊都查不到")
    return "查無符合條件的資料。"


def stream_chat_response(message: str, route: str, history: list = None):
    """串流回答：整個問答期間向協調器登記 Ollama 模型使用中"""
    if _GLOBAL_CHAT_ENGINE is None:
        init_system()
    with _LLM_RES.use():
        yield from _stream_chat_response_impl(message, route, history)


def _stream_chat_response_impl(message: str, route: str, history: list = None):
    """串流回答的實際流程：Basic 直接問 LLM；RAG 先過防護欄，再依路由查考勤、法規或兩者"""
    global _LAST_SOURCE_NODE_IDS, _LAST_QUERY_ROUTE
    if _GLOBAL_CHAT_ENGINE is None:
        init_system()

    # 取上一句使用者話語，讓簡短追問也能被正確判斷
    context = rag_guardrail.extract_last_user_context(history)

    if route == "Basic":
        _LAST_QUERY_ROUTE = "basic"
        database_mgr.log_llm_usage(event_type="basic", question=message)
        response = _GLOBAL_LLM.stream_complete(message)
        for token in response:
            yield token.delta
    elif route == "RAG":
        if not rag_guardrail.check_semantic_guardrail(message, context=context):
            _LAST_QUERY_ROUTE = "blocked"
            yield "🛑 [Guardrail 攔截] 您的問題超出了人事差勤與法規的範圍，系統已拒絕回答。"
            return

        query_route = rag_guardrail.check_query_route(message, context=context)
        _LAST_QUERY_ROUTE = query_route

        if query_route == "attendance":
            # 考勤問題即時查資料庫
            _LAST_SOURCE_NODE_IDS = []
            if _GLOBAL_SQL_ENGINE is None:
                yield "⚠️ 系統提示：即時考勤資料庫尚未連線成功，請確認門禁系統資料庫位置設定是否正確。"
                return
            ok, answer = rag_sql_engine.try_attendance_answer(_GLOBAL_SQL_ENGINE, message)
            if ok:
                yield answer
                return
            # 考勤查無資料，改試法規；用 .chat() 一次取得完整回答，才能先確認有沒有查到
            print("  [Debug Fallback] 考勤路線查無結果，改嘗試法規路線...")
            try:
                response = _GLOBAL_CHAT_ENGINE.chat(message)
                node_ids = [n.node.node_id for n in getattr(response, "source_nodes", [])]
                if node_ids and response and str(response).strip():
                    _LAST_QUERY_ROUTE = "attendance_fallback_law"
                    _LAST_SOURCE_NODE_IDS = node_ids
                    yield str(response)
                    return
            except Exception as e:
                print(f"DEBUG: ChatEngine Error (fallback): {e}")
            yield "查無符合條件的資料。"
            return

        if query_route == "mixed":
            # 混合題要等兩邊都查完才能整合，所以不逐字串流
            _LAST_SOURCE_NODE_IDS = []
            yield _answer_mixed_question(message)
            return

        try:
            response = _GLOBAL_CHAT_ENGINE.stream_chat(message)
            # 檢索到 0 筆時 llama_index 不會呼叫 LLM（回傳 "Empty Response"），這裡明確提示
            source_nodes = list(getattr(response, "source_nodes", []))
            if not source_nodes:
                print(f"⚠️ [Debug RAG] 法規向量索引檢索到 0 筆相關知識塊（問題：{message!r}）——"
                      f"請確認 data/laws/ 資料夾底下有放法規文件（.pdf/.txt），且 runtime/faiss_storage/ "
                      f"索引已經根據目前的 data/laws/ 內容重新建立過（可以先刪除 runtime/faiss_storage/ "
                      f"資料夾，下次啟動程式會自動偵測並重建）。")
                _LAST_SOURCE_NODE_IDS = []
                yield "⚠️ 系統提示：知識庫中找不到與此問題相關的法規資料（目前 data/laws/ 索引可能是空的，請確認法規文件是否已正確放置並重建索引）。"
                return
            has_token = False
            for token in response.response_gen:
                has_token = True
                yield token
            # 串流結束後記錄檢索到的知識塊 ID
            _LAST_SOURCE_NODE_IDS = [n.node.node_id for n in source_nodes]
            if not has_token:
                # 有檢索到但沒生成任何內容，提示重問
                print(f"⚠️ [Debug RAG] 有檢索到 {len(source_nodes)} 筆知識塊，但串流生成沒有吐出任何內容（問題：{message!r}）。")
                yield "⚠️ 系統提示：生成回答時發生問題，請重新提問一次。"
        except Exception as e:
            print(f"DEBUG: StreamChat Error: {e}")
            yield "⚠️ 系統提示：在檢索相關法規時遇到問題，或知識庫中找不到對應資料。"
    else:
        yield "Error: Invalid Route"


def get_chat_response(message: str, route: str, history: list = None) -> str:
    """非串流版本的回答（目前沒有呼叫端使用）"""
    if _GLOBAL_CHAT_ENGINE is None:
        init_system()
    with _LLM_RES.use():
        return _get_chat_response_impl(message, route, history)


def _get_chat_response_impl(message: str, route: str, history: list = None) -> str:
    """非串流回答的實際流程，邏輯與 _stream_chat_response_impl 相同"""
    global _LAST_SOURCE_NODE_IDS, _LAST_QUERY_ROUTE
    if _GLOBAL_CHAT_ENGINE is None:
        init_system()

    context = rag_guardrail.extract_last_user_context(history)

    if route == "Basic":
        _LAST_QUERY_ROUTE = "basic"
        database_mgr.log_llm_usage(event_type="basic", question=message)
        return str(_GLOBAL_LLM.complete(message))

    elif route == "RAG":
        if not rag_guardrail.check_semantic_guardrail(message, context=context):
            _LAST_QUERY_ROUTE = "blocked"
            return "🛑 [Guardrail 攔截] 您的問題超出了人事差勤與法規的範圍，系統已拒絕回答。"

        query_route = rag_guardrail.check_query_route(message, context=context)
        _LAST_QUERY_ROUTE = query_route

        if query_route == "attendance":
            _LAST_SOURCE_NODE_IDS = []
            if _GLOBAL_SQL_ENGINE is None:
                return "⚠️ 系統提示：即時考勤資料庫尚未連線成功，請確認門禁系統資料庫位置設定是否正確。"
            ok, answer = rag_sql_engine.try_attendance_answer(_GLOBAL_SQL_ENGINE, message)
            if ok:
                return answer
            # 考勤查無資料，改試法規（問題可能是在問規則）
            print("  [Debug Fallback] 考勤路線查無結果，改嘗試法規路線...")
            try:
                response = _GLOBAL_CHAT_ENGINE.chat(message)
                node_ids = [n.node.node_id for n in getattr(response, "source_nodes", [])]
                if node_ids and response and str(response).strip():
                    _LAST_QUERY_ROUTE = "attendance_fallback_law"
                    _LAST_SOURCE_NODE_IDS = node_ids
                    return str(response)
            except Exception as e:
                print(f"DEBUG: ChatEngine Error (fallback): {e}")
            return "查無符合條件的資料。"

        if query_route == "mixed":
            _LAST_SOURCE_NODE_IDS = []
            return _answer_mixed_question(message)

        try:
            response = _GLOBAL_CHAT_ENGINE.chat(message)
            _LAST_SOURCE_NODE_IDS = [n.node.node_id for n in getattr(response, "source_nodes", [])]
            if not response or str(response).strip() == "":
                return "⚠️ 系統提示：知識庫中找不到與此問題高度相關的法規資料。"
            return str(response)
        except Exception as e:
            print(f"DEBUG: ChatEngine Error: {e}")
            return "⚠️ 系統提示：在檢索相關法規時遇到問題，或知識庫中找不到對應資料。"

    return "Error: Invalid Route"

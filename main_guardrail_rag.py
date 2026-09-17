"""
RAG 系統的「統籌」層。

【2026-09-02，架構重構】這個檔案原本身兼六個角色（環境初始化、法規向量索引、
SQL 引擎、防護欄路由、對話引擎、實際問答）全部塞在同一個 700 多行的檔案裡，
不好維護也不好測試。現在拆成 4 個各司其職的模組：

  - rag_index.py         法規向量索引（FAISS）的建置與快取
  - rag_sql_engine.py     考勤/員工名冊的即時 SQL 查詢引擎
  - rag_guardrail.py      防護欄 + 路由分類（判斷該不該答、該去哪裡查）
  - rag_chat_engine.py    法規對話引擎的建置

這個檔案現在只保留「統籌」的工作：記住系統目前的執行狀態（目前用的是哪個
對話引擎、哪個 SQL 引擎）、初始化流程、以及回答一個問題時要照什麼順序去問
上面那些模組。對外的呼叫方式完全沒變——app.py、evaluate_rag.py 原本怎麼
`from main_guardrail_rag import ...`，現在照樣可以動，不用改任何一行。
"""

from llama_index.core import Settings
from llama_index.core.node_parser import SentenceSplitter
from llama_index.llms.ollama import Ollama
from llama_index.embeddings.huggingface import HuggingFaceEmbedding

import rag_index
import rag_sql_engine
import rag_guardrail
import rag_chat_engine
import database_mgr

# ==========================================
# 對外相容：app.py / evaluate_rag.py 原本是直接
# `from main_guardrail_rag import load_or_build_index, sample_random_attendance_record`
# 這裡把搬到子模組的函式重新「掛」回這個檔案的名字底下，外部的 import 完全不用改。
# ==========================================
load_or_build_index = rag_index.load_or_build_index
sample_random_attendance_record = rag_sql_engine.sample_random_attendance_record


# ==========================================
# 1. 全域變數：避免重複載入造成 OOM
# ==========================================
_GLOBAL_CHAT_ENGINE = None
_GLOBAL_SQL_ENGINE = None
_GLOBAL_LLM = None
# 記錄「上一次法規類問答」實際檢索到的知識塊 ID，供 evaluate_rag.py 計算「檢索命中率」使用
_LAST_SOURCE_NODE_IDS = []
# 記錄上一次問答實際走了哪個路由：'law' / 'attendance' / 'blocked' / 'basic'，供自動化評估診斷用
_LAST_QUERY_ROUTE = None


def setup_environment():
    global _GLOBAL_LLM
    print("🤖 [系統初始化] 載入本地 Edge AI 模型中...")
    Settings.llm = Ollama(model="qwen2:7b", request_timeout=600.0, temperature=0.0)
    Settings.embed_model = HuggingFaceEmbedding(model_name="BAAI/bge-m3")
    Settings.text_splitter = SentenceSplitter(chunk_size=300, chunk_overlap=30)
    _GLOBAL_LLM = Settings.llm


def init_system():
    global _GLOBAL_CHAT_ENGINE, _GLOBAL_SQL_ENGINE, _GLOBAL_LLM
    setup_environment()
    index = rag_index.load_or_build_index()
    _GLOBAL_CHAT_ENGINE = rag_chat_engine.build_chat_engine(index)
    _GLOBAL_SQL_ENGINE = rag_sql_engine.build_sql_engine()
    _GLOBAL_LLM = Settings.llm


def get_last_retrieved_node_ids():
    """提供給 evaluate_rag.py 使用：取得上一次「法規類」問答實際檢索到的知識塊 ID 清單，
    用來計算自動化評估裡的「檢索命中率」。"""
    return list(_LAST_SOURCE_NODE_IDS)


def get_last_query_route():
    """提供給 evaluate_rag.py 使用：取得上一次問答實際走的路由
    ('law' / 'attendance' / 'blocked' / 'basic' / 'mixed')，用來檢查防護欄與分類路由是否正常運作。"""
    return _LAST_QUERY_ROUTE


# 【2026-09-12，🔴 較大項目：混合型問題聯合查詢】
# rag_guardrail.check_query_route() 判定出 "mixed" 之後，實際「怎麼回答」交給這裡：
# 分別問一次考勤 SQL 引擎跟法規對話引擎，兩邊都問到東西才拿去給 LLM 整合成一句話。
# 提示詞刻意寫成純字串（不是 llama_index 的 PromptTemplate），因為這裡是直接交給
# _GLOBAL_LLM.complete() 用，這個檔案裡 Basic 路線本來就是這樣用純字串（見上面
# stream_chat_response 的 Basic 分支），風格一致。
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
    混合型問題（同時查考勤又查法規）的組合回答。

    做法：考勤 SQL 引擎跟法規對話引擎「各自獨立」問一次，互不影響——任何一邊失敗
    或查無資料都不會讓整個回答掛掉，改用「只用查到的那一邊」的答案；兩邊都查不到
    才老實回答查無資料。兩邊都查到的時候，才呼叫一次 LLM 把兩段資料整合成一句話；
    如果連整合這一步都失敗（例如 LLM 逾時），退而求其次把兩段原始答案直接接起來
    回傳，至少不會讓使用者兩邊資訊都拿不到。
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
    # 【2026-09-13 新增】印出兩邊「各自」實際查到什麼，方便之後對答案有疑慮時
    # （例如懷疑法規那邊憑空講出考勤資料）可以直接對照主控台輸出，不用用猜的。
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
        # 整合失敗就退而求其次，把兩段原始答案直接接起來，至少資訊不會不見
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
    global _LAST_SOURCE_NODE_IDS, _LAST_QUERY_ROUTE
    if _GLOBAL_CHAT_ENGINE is None:
        init_system()

    # 【2026-09-02】把「使用者上一句話」抽出來，交給防護欄/路由分類參考，
    # 讓「有名子嗎？」這種需要上下文才聽得懂的簡短追問，不會被誤判成跟人事差勤無關。
    context = rag_guardrail.extract_last_user_context(history)

    if route == "Basic":
        _LAST_QUERY_ROUTE = "basic"
        # 【P0，2026-09-12 新增】稽核紀錄：Basic 模式跳過防護欄直接問 LLM，這裡是它
        # 唯一會被記錄到的地方（app.py 的即時聊天走的是這支函式，不是 get_chat_response()）。
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
            # 考勤類問題：即時查詢門禁系統資料庫，不使用（可能過期的）向量索引
            _LAST_SOURCE_NODE_IDS = []
            if _GLOBAL_SQL_ENGINE is None:
                yield "⚠️ 系統提示：即時考勤資料庫尚未連線成功，請確認門禁系統資料庫位置設定是否正確。"
                return
            ok, answer = rag_sql_engine.try_attendance_answer(_GLOBAL_SQL_ENGINE, message)
            if ok:
                yield answer
                return
            # 【雙向 fallback】考勤路線查無資料，改試法規那邊，見 rag_sql_engine.try_attendance_answer()
            # 的說明。這裡改用 .chat()（一次拿到完整回答）而不是 .stream_chat()
            # （一個字一個字慢慢吐），是因為要先看有沒有真的查到相關法規資料，才能決定
            # 要不要用這個答案——沒辦法一邊即時吐字給你看、一邊事後反悔收回去重講。
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
            # 【2026-09-12】混合題型：跟考勤 fallback 到法規一樣，用 .chat()（一次拿完整
            # 答案）而不是逐字流式輸出，因為要先確定考勤跟法規兩邊各自有沒有查到東西、
            # 整合成一句話之後才知道最終答案長怎樣，沒辦法一邊吐字一邊事後反悔重講。
            _LAST_SOURCE_NODE_IDS = []
            yield _answer_mixed_question(message)
            return

        try:
            response = _GLOBAL_CHAT_ENGINE.stream_chat(message)
            for token in response.response_gen:
                yield token
            # 串流結束後，記錄這次實際檢索到的知識塊 ID，供自動化評估計算「檢索命中率」使用
            _LAST_SOURCE_NODE_IDS = [n.node.node_id for n in getattr(response, "source_nodes", [])]
        except Exception as e:
            print(f"DEBUG: StreamChat Error: {e}")
            yield "⚠️ 系統提示：在檢索相關法規時遇到問題，或知識庫中找不到對應資料。"
    else:
        yield "Error: Invalid Route"


def get_chat_response(message: str, route: str, history: list = None) -> str:
    global _LAST_SOURCE_NODE_IDS, _LAST_QUERY_ROUTE
    if _GLOBAL_CHAT_ENGINE is None:
        init_system()

    context = rag_guardrail.extract_last_user_context(history)

    if route == "Basic":
        _LAST_QUERY_ROUTE = "basic"
        # 【P0，2026-09-12 新增】稽核紀錄：這支函式目前沒有任何呼叫端在用（app.py 跟
        # evaluate_rag.py 都是走 stream_chat_response()），保留這行只是讓兩支函式行為一致。
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
            # 【雙向 fallback】考勤路線查無資料，改試法規那邊，見 rag_sql_engine.try_attendance_answer()
            # 的說明——使用者問的可能其實是「規則」（例如「遲到扣薪合不合法」），
            # 不是某一筆具體的打卡資料。
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
            # 捕捉 LlamaIndex 檢索失敗或空結果導致的錯誤
            print(f"DEBUG: ChatEngine Error: {e}")
            return "⚠️ 系統提示：在檢索相關法規時遇到問題，或知識庫中找不到對應資料。"

    return "Error: Invalid Route"

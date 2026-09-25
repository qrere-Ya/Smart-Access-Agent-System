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

import os

# 【2026-09-18 第五次修正，真正的根本原因：HF_HUB_OFFLINE 是 import 時就定死的】
# huggingface_hub 把 HF_HUB_OFFLINE 這個旗標，在它「第一次被 import 進來的當下」
# 就讀一次 os.environ、算成模組層級常數，不是每次要不要連網都重新讀。原本的做法
# 是在下面 setup_environment() 函式「執行的當下」才設定
# os.environ["HF_HUB_OFFLINE"]="1"——但這時候底下這幾行 `from llama_index.core
# import Settings` 等等，早就已經把 huggingface_hub 系列模組 import 進來、把這個
# 旗標算成 False 並快取住了，之後再設定 os.environ 完全沒用。這正是你實測「真的
# 斷網、也設了 HF_HUB_OFFLINE=1，卻還是真的發了 HTTP 請求」的根本原因。
#
# 修正方式：把「要不要用離線模式」這個判斷搬到這個檔案最上面，在下面
# `from llama_index.core import Settings` 這行 import 執行「之前」就先做完
# ——這樣不管這個檔案被 app.py、evaluate_rag.py，還是任何其他進入點 import，
# huggingface_hub 系列模組第一次被 import 進來的當下，讀到的就已經是正確的值。
# app.py 裡也在它自己檔案最上面（在 `import gradio` 之前）做了同樣的判斷並用
# os.environ.setdefault() 設定，這裡再做一次只是防止有其他進入點（例如直接跑
# evaluate_rag.py）沒有經過 app.py 那一層保護，setdefault 是幂等的，不會衝突。
#
# 這裡也不再用「先離線試、失敗才連網重試」這種寫法——那個做法在
# HF_HUB_OFFLINE 只能在 import 前決定一次的前提下已經不成立（同一個 process
# 裡沒辦法「中途反悔」），改回檔案系統直接判斷快取資料夾在不在，但這次用的是
# 已經實際驗證過的正確路徑算法（HF_HOME 預設在 ~/.cache/huggingface，快取
# 資料夾在它底下的 hub/），跟 app.py 用同一套算法。
_hf_home = os.environ.get("HF_HOME") or os.path.join(
    os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache"),
    "huggingface",
)
_hf_hub_cache = os.environ.get("HF_HUB_CACHE") or os.path.join(_hf_home, "hub")
if os.path.isdir(os.path.join(_hf_hub_cache, "models--BAAI--bge-m3")):
    os.environ.setdefault("HF_HUB_OFFLINE", "1")

# 【2026-09-18 第六次修正，真正解決離線問題：不再依賴 Hugging Face Hub 的隱式快取】
# 前面五次修正、四支獨立診斷腳本（diagnose_bge_cache.py ~ 4.py）已經反覆證實：
# 這台電腦上，就算 HuggingFaceEmbedding(model_name="BAAI/bge-m3") 每次都能成功
# 載入、算出正確的 1024 維向量，背後卻完全沒有把完整模型持久寫進
# ~/.cache/huggingface/hub 這個標準快取資料夾（這個資料夾甚至常常根本不存在）。
# 用 HF_DEBUG=1 開 debug log 直接證實：這個載入過程「每次都真的對外發送
# HTTP 請求」去 huggingface.co 解析/下載檔案，只是因為這台電腦裝了 hf_xet
# 這個 2026 年後 Hugging Face 預設的加速下載元件，大檔案的實際傳輸繞過了
# Python 這層看得到的請求記錄，速度快到感覺不出來——也證實過 HOME/USERPROFILE
# 兩個環境變數其實是一致的，不是路徑算錯的問題。也就是說：不管背後 Xet 快取
# 機制的細節到底是什麼，這台電腦目前的組合，只要用 repo id 字串
# "BAAI/bge-m3" 讓它自己去 Hub 解析，就是「每次都需要網路」，沒有例外，這不是
# 哪一行程式碼設錯了，而是這個載入路徑本身的行為就是如此。
#
# 與其繼續往下查 Xet 內部到底把資料放在哪裡，改用更直接、更不會受任何快取
# 機制影響的做法：用 download_bge_m3_local.py（隨這次修正一起提供）把完整模型
# 一次性下載到「專案自己的 models/bge-m3」資料夾——只要 model_name 傳進去的是
# 一個「真實存在、內容完整的本機資料夾路徑」，sentence-transformers /
# transformers 會直接當成本機路徑讀取，完全不會再去 Hugging Face Hub 做任何
# 線上解析，也就完全不受 HF_HUB_OFFLINE、Xet、或任何快取路徑猜測影響——
# 這樣才是真正保證離線可以動的做法，而不是繼續猜下一個環境變數。
#
# 如果 models/bge-m3 資料夾還沒下載（第一次設定、或忘記先執行下載腳本），
# 才會退回原本「用 repo id 連網」的行為，並且印出清楚的提示，不會靜默失敗。
_LOCAL_BGE_M3_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models", "bge-m3")


def _resolve_bge_m3_model_source():
    """回傳實際要傳給 HuggingFaceEmbedding(model_name=...) 的值：
    如果本機已經有 download_bge_m3_local.py 下載好的完整模型，回傳本機資料夾路徑
    （這樣完全不會連網）；否則回傳 Hugging Face repo id，並印出提示訊息。"""
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
              f"（還沒執行過 download_bge_m3_local.py，或上次下載不完整），"
              f"暫時改用連網方式從 Hugging Face Hub 直接讀取 repo id \"BAAI/bge-m3\""
              f"——這種方式在完全沒有網路的環境下會失敗。建議先在有網路的地方執行"
              f"一次「python download_bge_m3_local.py」，之後就不用再連網了。")
        return "BAAI/bge-m3"


from llama_index.core import Settings
from llama_index.core.node_parser import SentenceSplitter
from llama_index.llms.ollama import Ollama
import rag_resources  # 【資源規範】bge-m3 / Ollama 改由中央協調器管理，不再在啟動時常駐載入

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
# 【資源規範】受協調器管理的組件（重複呼叫 setup_environment() 時必須重用，不可再建第二份 bge-m3）
_MANAGED_EMBED = None
_LLM_RES = None
# 記錄「上一次法規類問答」實際檢索到的知識塊 ID，供 evaluate_rag.py 計算「檢索命中率」使用
_LAST_SOURCE_NODE_IDS = []
# 記錄上一次問答實際走了哪個路由：'law' / 'attendance' / 'blocked' / 'basic'，供自動化評估診斷用
_LAST_QUERY_ROUTE = None


class _SkipSelfTest(Exception):
    pass


def setup_environment():
    global _GLOBAL_LLM, _MANAGED_EMBED, _LLM_RES
    print("🤖 [系統初始化] 載入本地 Edge AI 模型中...")
    # 【2026-09-19 新增：ggml_backend_cpu_buffer_type_alloc_buffer 配置失敗】
    # 這裡原本沒有指定 context_window，llama-index 的 Ollama() 包裝預設會採用
    # 模型 GGUF 檔裡宣告的訓練上限（qwen2:7b 是 32768），實際觀察到 Ollama 的
    # llama-server 因此會多要求約 1.8 GiB 的 KV cache（context）空間，加上
    # 4.12 GiB 的模型權重本身，在系統可用記憶體只剩幾百 MiB、顯卡可用顯存也
    # 只剩約 1.4 GiB 的情況下（用 Windows 工作管理員確認一下當下還有哪些程式
    # 佔用大量記憶體，關掉不需要的），會直接在配置緩衝區這一步失敗
    # （unable to allocate CUDA_Host buffer）。這裡的問答（法規/考勤）根本用
    # 不到 32K 這麼長的上下文——ChatMemoryBuffer 本身已經限制在 3000 token、
    # 檢索回來的法規/考勤資料頂多幾百到一兩千 token——所以把 context_window
    # 明確縮小到 8192，可以把 KV cache 需求從約 1.8 GiB 降到約 448 MiB，省下
    # 來的記憶體讓模型更容易載入成功。但這只能省下 KV cache 那一塊，4.12 GiB
    # 的模型權重本身不會變小：如果系統可用記憶體真的長期只剩幾百 MiB，代表
    # 有其他程式吃掉了大部分的 15.7 GiB 記憶體，還是需要實際去騰出記憶體空間，
    # 光改這個參數不保證每次都能載入成功。
    Settings.llm = Ollama(
        model="qwen2:7b", request_timeout=600.0, temperature=0.0, context_window=8192
    )

    # 【2026-09-18 第五次修正】是否離線的判斷已經搬到檔案最上面、在所有
    # huggingface_hub 相關套件被 import「之前」就決定好了（見檔案開頭的說明，
    # 那才是根本原因所在：HF_HUB_OFFLINE 是 import 時就讀一次、快取成模組常數，
    # 不是每次呼叫都重新讀 os.environ，在這裡才設定已經太晚）。這裡只需要單純
    # 呼叫一次，並且把「目前到底是離線還是連網模式」明確印出來，方便你每次
    # 啟動時一眼確認狀態，不用再憑印象猜。
    # 【2026-09-18 第六次修正】改成優先使用本機完整模型資料夾（見檔案開頭
    # _resolve_bge_m3_model_source() 的詳細說明），只有在本機還沒下載好的情況下
    # 才會退回連網用 repo id 讀取。HF_HUB_OFFLINE 這個判斷現在只在「真的走連網
    # 那條退回路徑」時才有意義，繼續印出來方便確認目前狀態。
    _offline_now = os.environ.get("HF_HUB_OFFLINE") == "1"
    _bge_m3_source = _resolve_bge_m3_model_source()
    _using_local_model = _bge_m3_source != "BAAI/bge-m3"
    if not _using_local_model:
        print(f"🤖 [系統初始化] BAAI/bge-m3 embedding 模型載入中"
              f"（HF_HUB_OFFLINE={'1，離線模式，不會連網' if _offline_now else '未設定，連網模式'}）...")
    # 【資源規範 A. Initialized】這裡只建立「代理」，不讀取 bge-m3 權重；第一次真正
    # 需要向量時才向中央協調器申請（Acquired），閒置逾時或被驅逐時釋放。
    # 預設 device="cpu"（見 rag_resources.py），可用環境變數 RAG_EMBED_DEVICE 覆寫。
    if _MANAGED_EMBED is None:
        _MANAGED_EMBED = rag_resources.ManagedHFEmbedding(_bge_m3_source)
    Settings.embed_model = _MANAGED_EMBED
    if _LLM_RES is None:
        _LLM_RES = rag_resources.OllamaModelResource("qwen2:7b")

    # 【2026-09-18 第四次確認，真的實際跑一次 embedding 才算數】光是
    # HuggingFaceEmbedding() 這一行沒有丟例外，不代表這顆模型真的能正常運作
    # （之前發生過看起來「成功」但背後狀態對不起來的情況）。這裡直接實際呼叫
    # 一次 embedding、印出向量維度——bge-m3 正常應該是 1024 維，如果維度不對、
    # 或呼叫本身就丟例外，代表上面沒丟例外是假象，需要往下繼續查；如果維度
    # 正確，才是真正可信的證據。
    try:
        if os.environ.get("RAG_EMBED_SELFTEST") != "1":
            raise _SkipSelfTest()  # 預設不在啟動時載入 bge-m3；要診斷時設 RAG_EMBED_SELFTEST=1
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

    Settings.text_splitter = SentenceSplitter(chunk_size=300, chunk_overlap=30)
    _GLOBAL_LLM = Settings.llm


def init_system():
    global _GLOBAL_CHAT_ENGINE, _GLOBAL_SQL_ENGINE, _GLOBAL_LLM
    setup_environment()
    index = rag_index.load_or_build_index()
    _GLOBAL_CHAT_ENGINE = rag_chat_engine.build_chat_engine(index)
    _GLOBAL_SQL_ENGINE = rag_sql_engine.build_sql_engine()
    _GLOBAL_LLM = Settings.llm


def release_system():
    """【資源規範 D. Released】無條件釋放本模組持有的 bge-m3 與 Ollama 模型。"""
    if _MANAGED_EMBED is not None:
        _MANAGED_EMBED.release()
    if _LLM_RES is not None:
        _LLM_RES.release("explicit")


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
    """【資源規範 C. Executing】整個問答期間向協調器登記 Ollama 模型使用中。"""
    if _GLOBAL_CHAT_ENGINE is None:
        init_system()
    with _LLM_RES.use():
        yield from _stream_chat_response_impl(message, route, history)


def _stream_chat_response_impl(message: str, route: str, history: list = None):
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
            # 【2026-09-18 新增，修正「Empty Response」畫面一片空白的問題】
            # 根本原因是：法規向量索引（faiss_storage/）在建立當下 data/laws/
            # 資料夾是空的或不存在，索引裡實際上 0 筆文件。retriever 檢索到 0 個
            # 相關知識塊時，llama_index 會直接短路，回傳它自己內建的字面字串
            # "Empty Response"，完全不會真的呼叫 LLM 生成任何內容——response_gen
            # 這個 generator 因此一個 token 都不會吐出來，畫面上的助理訊息就會是
            # 空字串，Gradio 顯示出來就是一片空白（或版本不同顯示成 "Empty
            # Response" 字樣），使用者完全看不出「發生了什麼事」跟「該怎麼修」。
            # 這裡加一個明確的來源節點數判斷：如果真的檢索到 0 筆，直接印出診斷
            # 訊息到後台主控台（方便你之後確認 data/laws/ 有沒有正確放好法規文件、
            # 索引有沒有正確重建），並且改成 yield 一句清楚的中文提示，不要讓使用者
            # 看到空白訊息卻不知道發生了什麼事。
            source_nodes = list(getattr(response, "source_nodes", []))
            if not source_nodes:
                print(f"⚠️ [Debug RAG] 法規向量索引檢索到 0 筆相關知識塊（問題：{message!r}）——"
                      f"請確認 data/laws/ 資料夾底下有放法規文件（.pdf/.txt），且 faiss_storage/ "
                      f"索引已經根據目前的 data/laws/ 內容重新建立過（可以先刪除 faiss_storage/ "
                      f"資料夾，下次啟動程式會自動偵測並重建）。")
                _LAST_SOURCE_NODE_IDS = []
                yield "⚠️ 系統提示：知識庫中找不到與此問題相關的法規資料（目前 data/laws/ 索引可能是空的，請確認法規文件是否已正確放置並重建索引）。"
                return
            has_token = False
            for token in response.response_gen:
                has_token = True
                yield token
            # 串流結束後，記錄這次實際檢索到的知識塊 ID，供自動化評估計算「檢索命中率」使用
            _LAST_SOURCE_NODE_IDS = [n.node.node_id for n in source_nodes]
            if not has_token:
                # 有檢索到知識塊，但串流本身一個 token 都沒吐出來（例如 LLM 端逾時
                # 或提早結束）——同樣不要讓畫面留白，至少讓使用者知道要重問一次。
                print(f"⚠️ [Debug RAG] 有檢索到 {len(source_nodes)} 筆知識塊，但串流生成沒有吐出任何內容（問題：{message!r}）。")
                yield "⚠️ 系統提示：生成回答時發生問題，請重新提問一次。"
        except Exception as e:
            print(f"DEBUG: StreamChat Error: {e}")
            yield "⚠️ 系統提示：在檢索相關法規時遇到問題，或知識庫中找不到對應資料。"
    else:
        yield "Error: Invalid Route"


def get_chat_response(message: str, route: str, history: list = None) -> str:
    if _GLOBAL_CHAT_ENGINE is None:
        init_system()
    with _LLM_RES.use():
        return _get_chat_response_impl(message, route, history)


def _get_chat_response_impl(message: str, route: str, history: list = None) -> str:
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

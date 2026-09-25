"""
防護欄（擋掉跟人事差勤無關的問題）與路由分類（判斷該查考勤還是查法規）。

【2026-09-02，架構重構拆出】原本這些邏輯放在 main_guardrail_rag.py 裡，
跟法規索引、SQL 引擎、對話引擎混在同一個 700 多行的檔案裡。這裡只負責
一件事：判斷「這句話該不該回答」跟「該去哪裡找答案」，不實際去查資料庫或
向量索引，也不負責組出最終答案。
"""

import threading

from scipy.spatial.distance import cosine
from llama_index.core import Settings

import database_mgr
import rag_sql_engine
import ablation_config
import guardrail_policy


# 【2026-09-22，效能：check_semantic_guardrail() / check_query_route() 共用嵌入快取】
# 健康檢查發現這兩支函式在同一題問答裡，各自獨立呼叫 embed_model.get_text_embedding()：
#   1. check_query_route() 的 attendance_domain / law_domain 是兩段寫死不變的描述文字，
#      卻每一題都重新算一次嵌入，從沒被快取過。
#   2. 沒有上一句上下文時（例如對話第一句），check_semantic_guardrail() 判斷「當句」
#      跟 check_query_route() 判斷「當句」用的是同一段文字，卻各自獨立呼叫了一次。
# 這裡刻意不改動任何判斷邏輯本身——guardrail_policy.py 的門檻/規則、check_query_route()
# 該不該合併上下文的規則都完全沒動——只是把「同一個 embed_model、同一段文字」算過的
# 嵌入結果記住，重複算到就直接命中快取，回傳值保證跟沒有快取時完全一樣，純粹省掉
# 重複的模型前向運算。用 (id(embed_model), text) 當 key 而不是存 embed_model 物件本身，
# 是為了不讓這個快取多保留一份 embed_model（rag_resources.py 的 ManagedHFEmbedding
# 代理）的參照，避免日後不小心影響到它的釋放邏輯。
_EMBED_CACHE_MAXSIZE = 32
_embed_cache_lock = threading.Lock()
_embed_text_cache = {}          # {(id(embed_model), text): 嵌入向量}
_embed_text_cache_order = []    # 依寫入順序記錄 key，超過上限時 FIFO 淘汰最舊的一筆


def _cached_get_text_embedding(embed_model, text):
    key = (id(embed_model), text)
    with _embed_cache_lock:
        cached = _embed_text_cache.get(key)
    if cached is not None:
        return cached
    vec = embed_model.get_text_embedding(text)
    with _embed_cache_lock:
        if key not in _embed_text_cache:
            _embed_text_cache[key] = vec
            _embed_text_cache_order.append(key)
            if len(_embed_text_cache_order) > _EMBED_CACHE_MAXSIZE:
                oldest = _embed_text_cache_order.pop(0)
                _embed_text_cache.pop(oldest, None)
        return _embed_text_cache[key]


class _CachedEmbedModel:
    """
    輕量包裝：get_text_embedding() 接到上面的共用快取；get_text_embedding_batch()
    直接透傳給原本的 embed_model，不重複做一層跟 guardrail_policy.py 的 _ANCHOR_CACHE
    一樣的批次快取。

    每個底層 embed_model 只會透過 _get_cached_embed_model() 建立「一份」這種包裝、
    重複使用同一個物件，不會每次呼叫都重新包一個新的——guardrail_policy.py 自己的
    _ANCHOR_CACHE 是用 id(傳進去的 embed_model) 當 key，如果這裡每次都包一個新物件，
    會讓它的錨點快取一直被判定成「換了一個 embed_model」而失效、每題都要重算 24 個
    錨點的嵌入，反而更浪費。
    """

    def __init__(self, embed_model):
        self._embed_model = embed_model

    def get_text_embedding(self, text):
        return _cached_get_text_embedding(self._embed_model, text)

    def get_text_embedding_batch(self, texts):
        return self._embed_model.get_text_embedding_batch(texts)


_wrapped_embed_model_lock = threading.Lock()
_wrapped_embed_model_cache = {}  # {id(真正的 embed_model): 對應的 _CachedEmbedModel 單例}


def _get_cached_embed_model(embed_model):
    """回傳 embed_model 對應的、穩定不變的快取包裝物件（同一個底層 embed_model 只會有一份，
    確保傳給 guardrail_policy.make_margin_fn() 的物件 id() 在每次呼叫之間保持一致）。"""
    key = id(embed_model)
    with _wrapped_embed_model_lock:
        wrapped = _wrapped_embed_model_cache.get(key)
        if wrapped is None:
            wrapped = _CachedEmbedModel(embed_model)
            _wrapped_embed_model_cache[key] = wrapped
        return wrapped


# 【2026-09-08，六度更新：關鍵字快速通道】
# check_query_route() 原本完全依賴 bge-m3 語意相似度分類，實測發現對不常見的人名
# （例如「周宇祥」）統計上不夠穩定，偶爾會把明顯在問打卡紀錄的句子誤判成 law。
# 這裡先做一個關鍵字快速通道：句子裡只要出現這幾個「幾乎不可能出現在法規類問題裡」
# 的高信心度詞組，直接判定 attendance、跳過語意相似度計算，不會誤傷像「員工遲到是否
# 可以扣除整天薪水」這種真正在問規則、句子裡沒有這些詞組的問題。
_ATTENDANCE_KEYWORD_FAST_PATH = ("打卡時間", "打卡紀錄", "系統判定狀態", "打卡狀態")

# 【2026-09-14，學術強化方向 1：消融實驗】check_semantic_guardrail() 的「純關鍵字過濾」
# 對照組專用詞庫，只在 ablation_config.USE_SEMANTIC_GUARDRAIL 被明確關掉時才會用到，
# 正式系統的既有行為（對比式語意防護欄）完全不受影響。詞庫涵蓋 check_semantic_guardrail()
# 裡 positive_domain 描述的範圍，句子裡只要命中任何一個詞就放行，一個都沒命中就攔截——
# 這是消融實驗故意要拿來跟語意相似度判斷比較的「陽春版」做法，本來就預期準確率會比較差，
# 不是要拿來取代正式系統的防護欄。
_GUARDRAIL_POSITIVE_KEYWORDS_FOR_ABLATION = (
    "打卡", "考勤", "出缺勤", "出勤", "遲到", "早退", "加班", "請假", "特休", "產假",
    "病假", "育嬰留職停薪", "薪水", "薪資", "扣薪", "勞基法", "勞動基準法", "門禁",
    "員工", "在職", "輪班", "工會", "勞資會議", "打卡機",
)


def _check_keyword_only_guardrail(query: str) -> bool:
    """
    消融實驗對照組：check_semantic_guardrail() 的「純關鍵字過濾」版本，不呼叫嵌入模型、
    不算語意相似度，句子裡只要出現上面詞庫的任一詞就放行。
    """
    passed = any(keyword in (query or "") for keyword in _GUARDRAIL_POSITIVE_KEYWORDS_FOR_ABLATION)
    database_mgr.log_llm_usage(event_type="guardrail", question=query, guardrail_pass=passed)
    return passed

# 【2026-09-12，🔴 較大項目：混合型問題聯合查詢】
# 像「陳柏豫這個月遲到扣薪上限多少」這種問題，同時牽涉「某一位真實員工的實際
# 出缺勤資料」跟「勞基法規定的扣薪上限」，原本 check_query_route() 只能二選一
# （attendance 或 law），這種題型「現階段會被歸類到比較相關的那一類」（見下面
# check_query_route() docstring），答案通常只答對一半。
#
# 這裡刻意採「精準優先、寧可漏判也不要誤判」的判斷條件：句子裡「同時」符合
# 兩個條件才算 mixed——
#   1. 出現一個資料庫裡真實存在的員工姓名（不是隨便講到「員工」兩個字）。
#   2. 出現下面這些「明顯在問法規計算結果」的詞組。
# 為什麼故意這麼嚴格：2026-09-02 那次修正才好不容易解決「員工遲到是否可以扣除
# 整天薪水」這種純法規問題被誤判成 attendance 的問題（見 check_query_route()
# docstring），這句話裡也有「遲到」「薪水」這種字眼，如果 mixed 判斷條件只看
# 關鍵字、不要求「真實員工姓名」，會讓這句已經驗證修好的句子又被誤判成
# mixed，等於讓已經在真實環境驗證過的分類結果倒退。要求「真實姓名」可以
# 把這種泛稱「員工」的規則類問題排除在外，只留下真的在問「某個人」的
# 混合題型。
#
# 代價：像「這個月遲到 5 次扣薪上限多少」這種沒有指名道姓、只問假設情境的
# 混合題型，這裡判斷不出來、還是會照原本的二選一分類走。這是刻意的取捨，
# 先求不要讓現有分類退步，之後有需要再視情況放寬。
_LAW_KEYWORD_HINTS = ("扣薪", "扣多少", "扣除多少", "上限", "違法", "合法", "罰則", "賠償", "罰款")


def _detect_mixed_question(text: str) -> bool:
    """
    判斷這句話是不是「同時問真實員工的考勤資料 + 法規計算結果」的混合型問題。
    條件見上面 _LAW_KEYWORD_HINTS 註解：法規關鍵字 + 資料庫裡真實存在的員工姓名
    兩者都要出現才算，任一條件不符合就回傳 False，照原本的二選一分類走。
    """
    if not text:
        return False
    if not any(keyword in text for keyword in _LAW_KEYWORD_HINTS):
        return False
    try:
        known_names = rag_sql_engine._get_known_employee_names()
    except Exception as e:
        print(f"  [Debug Router] 混合題型判斷時查詢員工名冊失敗，視為非混合題型: {e}")
        return False
    return any(name and name in text for name in known_names)


def extract_last_user_context(history):
    """
    【讓防護欄/路由分類看得懂需要上下文的簡短追問】

    從 Gradio「messages」格式的對話歷史（一則訊息一個 dict，例如
    `{"role": "user", "content": "..."}`）裡，取出「使用者上一句話」，
    回傳一個字串（沒有可用的上一句話就回傳 None）。

    背景：像「有名子嗎？」這種簡短口語化的追問，句子本身完全沒有「員工」
    「考勤」「法規」這類關鍵字，如果只看這一句話本身去跟合法/不合法領域的
    說明文字算語意相似度，分數會偏低，容易被防護欄誤判成「跟人事差勤無關」
    而攔截掉——即使對話上一句明明是「員工目前有幾人」，人類一看就知道這句
    在問「員工的名字」。

    只取「最近一句」使用者說過的話，不是把整段歷史都塞進去：多輪之前的舊
    話題通常已經不相關，硬塞進去反而可能讓語意判斷失焦；只抓最近一句，
    通常已經足夠讓「這句話跟得上上一句話題」這件事在語意上看得出來。

    【注意】呼叫進來的 history，這時候通常已經包含「這一次」使用者剛問的
    訊息（app.py 的 chat_interface() 會先把新訊息 append 進 history，才呼叫
    main_guardrail_rag.py），所以要跳過最後一則使用者訊息（那是「現在這一句」，
    不是「上一句」），往前找它前面那一則使用者訊息。
    """
    if not history:
        return None
    # 【2026-09-20】Gradio 的 content 可能是 [{'text': ..., 'type': 'text'}] 清單，先轉成純文字，
    # 不然 repr 字串會原樣進到嵌入模型與 Debug 訊息裡。
    user_messages = [guardrail_policy.content_to_text(item.get("content", ""))
                     for item in history if item.get("role") == "user"]
    if len(user_messages) < 2:
        # 少於 2 則使用者訊息，代表這是對話的第一句，沒有「上一句」可以參考
        return None
    return user_messages[-2] or None


def check_semantic_guardrail(query: str, context: str = None) -> bool:
    # 【2026-09-14，學術強化方向 1：消融實驗】預設維持正式系統既有行為（對比式語意
    # 防護欄）。只有消融實驗明確關掉 ablation_config.USE_SEMANTIC_GUARDRAIL 時，才會
    # 改用下面的純關鍵字過濾對照組，藉此量化語意防護欄相對於陽春關鍵字過濾的實際貢獻。
    if not ablation_config.USE_SEMANTIC_GUARDRAIL:
        return _check_keyword_only_guardrail(query)

    # 【2026-09-20 重寫，見 guardrail_policy.py 模組說明】舊版「一律把上一句拼進嵌入文字 +
    # 單一混合錨點 + sim_neg > sim_pos」實測會讓離題問題在多輪對話中 80% 突破。新版：
    # 意圖詞規則 -> 只看當前句的多錨點差值 -> 灰區且像追問時才用上一句救援。
    # 判定邏輯全部在 guardrail_policy.decide()（純函式，可單獨測試）。
    margin_fn = guardrail_policy.make_margin_fn(_get_cached_embed_model(Settings.embed_model))
    passed, reason, detail = guardrail_policy.decide(
        guardrail_policy.content_to_text(query), guardrail_policy.content_to_text(context), margin_fn
    )

    print(f"  [Debug Guardrail] {'放行' if passed else '攔截'}（{reason}）"
          + (f" | 當句 正向 {detail['pos']:.4f} 負向 {detail['neg']:.4f} 差值 {detail['margin']:+.4f}"
             if "margin" in detail else f" | 意圖詞規則: {detail.get('intent')}")
          + (f" | 上一句差值 {detail['prev_margin']:+.4f}" if "prev_margin" in detail else "")
          + (f" | 併上一句後差值 {detail['ctx_margin']:+.4f}" if "ctx_margin" in detail else ""))

    # 【P0，2026-09-12 新增】稽核紀錄：guardrail 放行／攔截事件，不影響判斷結果本身。
    database_mgr.log_llm_usage(
        event_type="guardrail", question=query, guardrail_pass=passed, similarity_score=detail.get("pos")
    )
    return passed


def check_query_route(query: str, context: str = None) -> str:
    """
    對已經通過防護欄的問題做「二次分類」：判斷這題應該查「即時考勤資料庫」還是「法規向量索引」。
    做法跟 check_semantic_guardrail() 一樣是用語意相似度比對，不用另外訓練分類器。

    目前是二分類（純考勤 / 純法規）外加一個窄範圍的第三類 mixed：句子裡「同時」出現
    真實員工姓名跟明顯的法規計算詞組（扣薪、上限…）時才會判定成 mixed，見
    _detect_mixed_question() 的說明。除了這個窄範圍情況以外，其餘需要同時查考勤
    又查法規的混合型問題，現階段還是會被歸類到比較相關的那一類，之後再視需要擴大
    mixed 的判斷範圍。

    【2026-09-02 實測確認已修正】問「員工遲到是否可以扣除整天薪水」這種純粹在問「法規
    規則」的問題，原本因為句子裡有「遲到」兩個字，會被誤判成 attendance（考勤相似度
    0.5778 些微高於法規相似度 0.5689，非常接近），誤判之後 SQL 查詢引擎會硬翻出無效的
    SQL（例如用了 SQLite 沒有的 MySQL 函式 TIMESTAMPDIFF()）導致查詢失敗。下面兩段
    domain 說明文字已加強對比措辭（law_domain 直接放進「遲到早退扣薪的計算方式與扣薪
    比例、可不可以扣除整天薪水」這幾個字），拉高這類「問規則、不是問某一筆具體資料」
    的問題在法規那邊的相似度。這個環境本身連不到 Hugging Face、裝不了真正的 bge-m3
    模型，這段修正當時沒辦法在這裡直接驗證；已經請使用者在自己電腦上實際重新問過
    「員工遲到是否能扣除整天薪水」，回答正確走了法規路線、給出正確且不含糊的法規
    結論，確認這個修正有效。
    """

    # 【2026-09-12】混合型問題判斷：擺在關鍵字快速通道「之前」，因為快速通道的詞組
    # （例如「打卡狀態」）理論上也可能出現在混合題型句子裡，要先讓 mixed 判斷有機會
    # 攔到，不然會被快速通道先搶走判成純 attendance。
    if _detect_mixed_question(query):
        print(f"  [Debug Router] 偵測到「真實員工姓名 + 法規計算詞組」同時出現，判定為 mixed（同時查考勤與法規）")
        # 【P0，2026-09-12 新增】稽核紀錄：mixed 判斷命中，沒有語意相似度分數可記。
        database_mgr.log_llm_usage(event_type="route", question=query, route="mixed")
        return "mixed"

    # 【2026-09-08，六度更新】關鍵字快速通道：命中就直接回傳，不用等語意相似度算完。
    # 【2026-09-14，學術強化方向 1：消融實驗】預設維持正式系統既有行為（有快速通道）。
    # 只有消融實驗明確關掉 ablation_config.USE_KEYWORD_FASTPATH 時，才會整段跳過、
    # 直接落到下面純語意分類，藉此量化快速通道對路由正確率的實際貢獻。
    if ablation_config.USE_KEYWORD_FASTPATH:
        for keyword in _ATTENDANCE_KEYWORD_FAST_PATH:
            if keyword in query:
                print(f"  [Debug Router] 命中關鍵字快速通道「{keyword}」，直接判定為 attendance（跳過語意相似度計算）")
                # 【P0，2026-09-12 新增】稽核紀錄：快速通道命中，沒有語意相似度分數可記。
                database_mgr.log_llm_usage(event_type="route", question=query, route="attendance")
                return "attendance"
    attendance_domain = (
        "查詢『特定、具體』的打卡紀錄資料：某位員工實際的上下班時間、"
        "某一天有沒有遲到早退、出勤統計數字、特定日期的出缺勤紀錄、加班時數紀錄、"
        "門禁進出時間紀錄、員工姓名對應的打卡狀態、員工人數統計、公司有哪些員工、"
        "員工名冊查詢、特定員工的員工編號或性別——這些問題的答案要去查資料庫的"
        "實際紀錄才知道，不是看法規條文就能回答的。"
    )
    law_domain = (
        "詢問『規則、規定、標準、是否合法』本身，不是在查某一筆具體的打卡資料："
        "勞動基準法規定、薪資可以合法扣除多少、遲到早退扣薪的計算方式與扣薪比例、"
        "可不可以扣除整天薪水、加班費怎麼計算、請假規定、法條解釋、特殊工作者權益、"
        "勞資爭議處理方式——這些問題的答案是固定不變的法規知識，不管某位員工今天"
        "實際有沒有遲到都一樣，不需要查任何一筆打卡紀錄。"
    )

    # 【2026-09-02】跟 check_semantic_guardrail() 一樣，有上一句上下文就合併進去，
    # 理由相同：「有名子嗎？」這種簡短追問單獨看毫無線索，合併上一句才知道在問員工名冊。
    embed_text = f"{context}，{query}" if context else query

    # 【效能】attendance_domain / law_domain 是寫死不變的描述文字，且沒有上下文時
    # 這裡的 query 跟 check_semantic_guardrail() 剛算過的「當句」是同一段文字——
    # 用 _get_cached_embed_model() 包裝過的 embed_model，三次呼叫裡只要文字重複
    # （不管是自己前面算過的、還是 check_semantic_guardrail() 算過的），都會直接命中
    # 快取，不會真的再跑一次 bge-m3 前向運算；分類結果跟沒有快取時保證完全一樣。
    embed_model = _get_cached_embed_model(Settings.embed_model)
    query_embedding = embed_model.get_text_embedding(embed_text)
    att_embedding = embed_model.get_text_embedding(attendance_domain)
    law_embedding = embed_model.get_text_embedding(law_domain)

    sim_att = 1 - cosine(query_embedding, att_embedding)
    sim_law = 1 - cosine(query_embedding, law_embedding)

    print(f"  [Debug Router] 考勤相似度: {sim_att:.4f} | 法規相似度: {sim_law:.4f}"
          + (f" | (已合併上一句上下文: 「{context}」)" if context else ""))
    route = "attendance" if sim_att > sim_law else "law"

    # 【P0，2026-09-12 新增】稽核紀錄：路由判斷事件，記錄贏的那一邊的相似度分數。
    database_mgr.log_llm_usage(
        event_type="route", question=query, route=route,
        similarity_score=sim_att if route == "attendance" else sim_law,
    )
    return route

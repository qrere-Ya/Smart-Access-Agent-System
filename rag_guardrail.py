"""
rag_guardrail.py：防護欄與路由分類。

主要功能：
  - check_semantic_guardrail：擋掉與人事差勤無關的問題（語意防護欄，可在消融實驗改用純關鍵字版本）
  - check_query_route：判斷該查考勤資料庫、法規索引，或兩者都查（mixed）
      順序：混合題判斷 → 員工姓名 + 遲到/早退 → 關鍵字快速通道（容許錯字）→ 語意相似度分類
  - extract_last_user_context：取出使用者上一句話，讓簡短追問看得懂
  - 嵌入結果快取，避免同一題重複計算
  只判斷「該不該答、去哪查」，不實際查詢或組出答案。

串接模組：
  - guardrail_policy：防護欄判定規則
  - rag_sql_engine：查詢員工名冊與姓名模糊比對
  - fuzzy_match：關鍵字錯字容錯
  - ablation_config：消融實驗開關
  - database_mgr：寫入判斷稽核紀錄
  - main_guardrail_rag：問答流程呼叫本模組
"""

import threading

from scipy.spatial.distance import cosine
from llama_index.core import Settings

import database_mgr
import rag_sql_engine
import ablation_config
import guardrail_policy
from fuzzy_match import min_edit_distance_in_text


# --- 嵌入快取：同一個 embed_model 與同一段文字只算一次，結果與未快取時完全相同 ---
_EMBED_CACHE_MAXSIZE = 32
_embed_cache_lock = threading.Lock()
_embed_text_cache = {}          # {(id(embed_model), text): 嵌入向量}
_embed_text_cache_order = []    # 依寫入順序記錄 key，超過上限時淘汰最舊的


def _cached_get_text_embedding(embed_model, text):
    """取得文字嵌入，有快取就直接回傳"""
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
    """embed_model 的包裝：單句嵌入走快取，批次嵌入直接交給原模型。每個底層模型只建立一份，讓錨點快取的 id 保持穩定。"""

    def __init__(self, embed_model):
        self._embed_model = embed_model

    def get_text_embedding(self, text):
        return _cached_get_text_embedding(self._embed_model, text)

    def get_text_embedding_batch(self, texts):
        return self._embed_model.get_text_embedding_batch(texts)


_wrapped_embed_model_lock = threading.Lock()
_wrapped_embed_model_cache = {}  # {id(真正的 embed_model): 對應的 _CachedEmbedModel}


def _get_cached_embed_model(embed_model):
    """回傳 embed_model 對應的快取包裝物件（同一個模型永遠回傳同一份）"""
    key = id(embed_model)
    with _wrapped_embed_model_lock:
        wrapped = _wrapped_embed_model_cache.get(key)
        if wrapped is None:
            wrapped = _CachedEmbedModel(embed_model)
            _wrapped_embed_model_cache[key] = wrapped
        return wrapped


# 關鍵字快速通道：句子含這些詞幾乎一定是在問考勤資料，直接判為 attendance
_ATTENDANCE_KEYWORD_FAST_PATH = (
    "打卡時間", "打卡紀錄", "系統判定狀態", "打卡狀態",
    "行事曆", "會議行程",
)

# 快速通道容許的最大編輯距離（誤判代價低，所以比姓名比對寬鬆）
_ATTENDANCE_KEYWORD_FUZZY_MAX_DISTANCE = 1

# 消融實驗對照組用的純關鍵字詞庫（關閉語意防護欄時才會用到）
_GUARDRAIL_POSITIVE_KEYWORDS_FOR_ABLATION = (
    "打卡", "考勤", "出缺勤", "出勤", "遲到", "早退", "加班", "請假", "特休", "產假",
    "病假", "育嬰留職停薪", "薪水", "薪資", "扣薪", "勞基法", "勞動基準法", "門禁",
    "員工", "在職", "輪班", "工會", "勞資會議", "打卡機",
)


def _check_keyword_only_guardrail(query: str) -> bool:
    """消融實驗對照組：句子含任一詞庫關鍵字就放行，不使用語意相似度"""
    passed = any(keyword in (query or "") for keyword in _GUARDRAIL_POSITIVE_KEYWORDS_FOR_ABLATION)
    database_mgr.log_llm_usage(event_type="guardrail", question=query, guardrail_pass=passed)
    return passed

# 混合題判斷：句子要同時出現「資料庫中真實存在的員工姓名」與下列法規計算詞，才判為 mixed，
# 刻意嚴格，避免「員工遲到是否可以扣除整天薪水」這種純法規問題被誤判
_LAW_KEYWORD_HINTS = ("扣薪", "扣多少", "扣除多少", "上限", "違法", "合法", "罰則", "賠償", "罰款")


def _detect_mixed_question(text: str) -> bool:
    """判斷是否為混合題：同時含法規計算詞與真實員工姓名"""
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


# 考勤狀態題：含真實員工姓名 + 遲到/早退，直接判為 attendance
_ATTENDANCE_STATUS_KEYWORDS = ("遲到", "早退")


def _detect_attendance_status_question(text: str) -> bool:
    """判斷是否為考勤狀態題：含遲到/早退詞，且能（容許錯字）比對到唯一的真實員工姓名"""
    if not text:
        return False
    if not any(keyword in text for keyword in _ATTENDANCE_STATUS_KEYWORDS):
        return False
    try:
        known_names = rag_sql_engine._get_known_employee_names()
    except Exception as e:
        print(f"  [Debug Router] 考勤狀態問題判斷時查詢員工名冊失敗，視為非此類問題: {e}")
        return False
    return rag_sql_engine._match_known_employee_name(text, known_names) is not None


def extract_last_user_context(history):
    """
    從對話歷史取出使用者「上一句」話（沒有就回傳 None），讓簡短追問能被正確判斷。
    history 已包含這一次剛問的訊息，所以要跳過最後一則使用者訊息。
    """
    if not history:
        return None
    # content 可能是清單格式，先轉成純文字
    user_messages = [guardrail_policy.content_to_text(item.get("content", ""))
                     for item in history if item.get("role") == "user"]
    if len(user_messages) < 2:
        # 對話第一句，沒有上一句可參考
        return None
    return user_messages[-2] or None


def check_semantic_guardrail(query: str, context: str = None) -> bool:
    """語意防護欄：回傳問題是否與人事差勤相關（放行為 True）。判定邏輯在 guardrail_policy.decide。"""
    # 消融實驗關閉語意防護欄時，改用純關鍵字對照組
    if not ablation_config.USE_SEMANTIC_GUARDRAIL:
        return _check_keyword_only_guardrail(query)

    margin_fn = guardrail_policy.make_margin_fn(_get_cached_embed_model(Settings.embed_model))
    passed, reason, detail = guardrail_policy.decide(
        guardrail_policy.content_to_text(query), guardrail_policy.content_to_text(context), margin_fn
    )

    print(f"  [Debug Guardrail] {'放行' if passed else '攔截'}（{reason}）"
          + (f" | 當句 正向 {detail['pos']:.4f} 負向 {detail['neg']:.4f} 差值 {detail['margin']:+.4f}"
             if "margin" in detail else f" | 意圖詞規則: {detail.get('intent')}")
          + (f" | 上一句差值 {detail['prev_margin']:+.4f}" if "prev_margin" in detail else "")
          + (f" | 併上一句後差值 {detail['ctx_margin']:+.4f}" if "ctx_margin" in detail else ""))

    # 寫入稽核紀錄
    database_mgr.log_llm_usage(
        event_type="guardrail", question=query, guardrail_pass=passed, similarity_score=detail.get("pos")
    )
    return passed


def check_query_route(query: str, context: str = None) -> str:
    """對通過防護欄的問題分類，回傳 'mixed'（考勤+法規）、'attendance'（考勤資料庫）或 'law'（法規索引）"""

    # 混合題要先判斷，避免被後面的快速通道搶走
    if _detect_mixed_question(query):
        print(f"  [Debug Router] 偵測到「真實員工姓名 + 法規計算詞組」同時出現，判定為 mixed（同時查考勤與法規）")
        database_mgr.log_llm_usage(event_type="route", question=query, route="mixed")
        return "mixed"

    # 員工姓名 + 遲到/早退，直接判為考勤；放在混合題判斷之後
    if _detect_attendance_status_question(query):
        print(f"  [Debug Router] 偵測到「真實員工姓名 + 遲到/早退」同時出現，判定為 attendance")
        database_mgr.log_llm_usage(event_type="route", question=query, route="attendance")
        return "attendance"

    # 關鍵字快速通道（消融實驗可關閉）：命中就不用算語意相似度
    if ablation_config.USE_KEYWORD_FASTPATH:
        for keyword in _ATTENDANCE_KEYWORD_FAST_PATH:
            distance = min_edit_distance_in_text(keyword, query)
            if distance is not None and distance <= _ATTENDANCE_KEYWORD_FUZZY_MAX_DISTANCE:
                print(f"  [Debug Router] 命中關鍵字快速通道「{keyword}」（編輯距離 {distance}），直接判定為 attendance（跳過語意相似度計算）")
                database_mgr.log_llm_usage(event_type="route", question=query, route="attendance")
                return "attendance"
    attendance_domain = (
        "查詢『特定、具體』的打卡紀錄資料：某位員工實際的上下班時間、"
        "某一天有沒有遲到早退、出勤統計數字、特定日期的出缺勤紀錄、加班時數紀錄、"
        "門禁進出時間紀錄、員工姓名對應的打卡狀態、員工人數統計、公司有哪些員工、"
        "員工名冊查詢、特定員工的員工編號或性別、員工的行事曆與會議行程安排、"
        "特定時間有什麼會議或行程——這些問題的答案要去查資料庫的實際紀錄才知道，"
        "不是看法規條文就能回答的。"
    )
    law_domain = (
        "詢問『規則、規定、標準、是否合法』本身，不是在查某一筆具體的打卡資料："
        "勞動基準法規定、薪資可以合法扣除多少、遲到早退扣薪的計算方式與扣薪比例、"
        "可不可以扣除整天薪水、加班費怎麼計算、請假規定、法條解釋、特殊工作者權益、"
        "勞資爭議處理方式——這些問題的答案是固定不變的法規知識，不管某位員工今天"
        "實際有沒有遲到都一樣，不需要查任何一筆打卡紀錄。"
    )

    # 有上一句就合併進來，簡短追問才看得出主題
    embed_text = f"{context}，{query}" if context else query

    # 用快取包裝的 embed_model，重複文字不會重算
    embed_model = _get_cached_embed_model(Settings.embed_model)
    query_embedding = embed_model.get_text_embedding(embed_text)
    att_embedding = embed_model.get_text_embedding(attendance_domain)
    law_embedding = embed_model.get_text_embedding(law_domain)

    sim_att = 1 - cosine(query_embedding, att_embedding)
    sim_law = 1 - cosine(query_embedding, law_embedding)

    print(f"  [Debug Router] 考勤相似度: {sim_att:.4f} | 法規相似度: {sim_law:.4f}"
          + (f" | (已合併上一句上下文: 「{context}」)" if context else ""))
    route = "attendance" if sim_att > sim_law else "law"

    # 寫入稽核紀錄，記錄勝出一邊的相似度
    database_mgr.log_llm_usage(
        event_type="route", question=query, route=route,
        similarity_score=sim_att if route == "attendance" else sim_law,
    )
    return route

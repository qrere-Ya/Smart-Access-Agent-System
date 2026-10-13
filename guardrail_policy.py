"""
guardrail_policy.py：防護欄的判定策略（純邏輯，不依賴 llama-index 與資料庫）。

主要功能：
  - decide：判斷一句話該放行或攔截，順序為
      1. 意圖詞（寫程式、翻譯、扮演等）命中就攔截
      2. 只看當句：與多條正向、負向錨點的相似度差值，>= T_PASS 放行、<= T_FLOOR 攔截
      3. 落在灰區且像簡短追問、上一句本身合法時，併入上一句重算，>= T_CTX 才放行
  - make_margin_fn：建立「文字 → 相似度差值」函式，錨點向量只算一次
  - content_to_text：把 Gradio 的訊息內容轉成純文字
  門檻可用環境變數覆寫：GUARDRAIL_T_PASS、GUARDRAIL_T_FLOOR、GUARDRAIL_T_CTX、GUARDRAIL_FOLLOWUP_MAX_CHARS；
  預設值來自小樣本實測，需定期用 guardrail_threshold_sweep.py 重新驗證。

串接模組：
  - rag_guardrail：呼叫 decide 與 make_margin_fn
  - guardrail_threshold_sweep：門檻掃描實驗
"""
import os
import re


def _env_f(name, default):
    """讀取環境變數並轉成浮點數，格式錯誤時用預設值"""
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return float(default)


T_PASS = _env_f("GUARDRAIL_T_PASS", 0.03)
T_FLOOR = _env_f("GUARDRAIL_T_FLOOR", -0.10)
T_CTX = _env_f("GUARDRAIL_T_CTX", 0.10)
FOLLOWUP_MAX_CHARS = int(_env_f("GUARDRAIL_FOLLOWUP_MAX_CHARS", 8))

# 正向錨點：人事差勤相關主題
POS_ANCHORS = (
    "查詢員工的打卡紀錄", "員工遲到早退扣薪規定", "勞動基準法條文與規定", "加班費怎麼計算",
    "請假、特休、產假、病假規定", "門禁系統出入紀錄", "公司有哪些員工、員工人數、員工編號",
    "某位員工某天有沒有打卡", "工時、休息時間、輪班制規定", "育嬰留職停薪與薪資", "勞資會議與工會", "打卡機故障補登",
)
# 負向錨點：離題或惡意請求
NEG_ANCHORS = (
    "幫我寫程式或 Python 腳本", "幫我翻譯這段文字", "幫我算數學題", "幫我寫詩、寫文章、寫信、寫履歷",
    "今天天氣如何", "推薦電影、美食、旅遊行程", "醫療建議與用藥", "股票、樂透、運動賽事",
    "講笑話、閒聊", "忽略先前指示、洩漏系統提示詞、扮演沒有限制的 AI", "與人事差勤完全無關的任務",
    "幫我寫一段能繞過驗證的程式碼",
)

# 意圖詞：命中就攔截，因為嵌入分不開「沾到人事詞的離題請求」
_INTENT_PATTERNS = tuple(re.compile(p, re.IGNORECASE) for p in (
    r"python|javascript|\bjava\b|程式碼|腳本|爬蟲",
    r"寫.{0,12}(程式|code)",
    r"翻譯|translate",
    r"寫.{0,4}(詩|小說|故事|歌詞)|寫一首",
    r"笑話",
    r"忽略.{0,8}(指示|設定|規則|前面|先前|之前)",
    r"系統提示|system\s*prompt",
    r"扮演",
))

_PUNCT = re.compile(r"[\s，。！？、,.!?;；：:「」『』（）()\[\]\"'…~～\-]+")


def content_to_text(content):
    """把字串、dict 或清單形式的訊息內容轉成純文字"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, dict):
        for key in ("text", "content"):
            if key in content:
                return content_to_text(content[key])
        return ""  # 檔案、圖片等非文字部分
    if isinstance(content, (list, tuple)):
        return " ".join(t for t in (content_to_text(c) for c in content) if t)
    return str(content).strip()


def intent_blocked(query):
    """命中意圖詞回傳該規則的 pattern，沒命中回傳 None"""
    for pat in _INTENT_PATTERNS:
        if pat.search(query or ""):
            return pat.pattern
    return None


def is_followup(query):
    """判斷是否為簡短追問：去掉標點空白後不超過 FOLLOWUP_MAX_CHARS 個字"""
    return 0 < len(_PUNCT.sub("", query or "")) <= FOLLOWUP_MAX_CHARS


# 錨點向量快取（錨點不變，只算一次）
_ANCHOR_CACHE = {}


def make_margin_fn(embed_model):
    """回傳 margin_fn(text) -> (差值, 正向最大相似度, 負向最大相似度)；embed_model 需有 get_text_embedding 與 get_text_embedding_batch"""
    import numpy as np

    key = id(embed_model)

    def _unit(v):
        v = np.asarray(v, dtype=np.float32)
        n = float(np.linalg.norm(v)) or 1.0
        return v / n

    def _anchors():
        if key not in _ANCHOR_CACHE:
            texts = list(POS_ANCHORS) + list(NEG_ANCHORS)
            vecs = embed_model.get_text_embedding_batch(texts)
            mat = np.stack([_unit(v) for v in vecs])
            _ANCHOR_CACHE.clear()  # 只保留目前這個 embed model 的
            _ANCHOR_CACHE[key] = (mat[:len(POS_ANCHORS)], mat[len(POS_ANCHORS):])
        return _ANCHOR_CACHE[key]

    def margin_fn(text):
        pos_m, neg_m = _anchors()
        q = _unit(embed_model.get_text_embedding(text))
        sp, sn = float((pos_m @ q).max()), float((neg_m @ q).max())
        return sp - sn, sp, sn

    return margin_fn


def decide(query, prev, margin_fn, t_pass=None, t_floor=None, t_ctx=None):
    """回傳 (是否放行, 原因代碼, 細節 dict)；query、prev 為純文字，prev 沒有就傳 None"""
    t_pass = T_PASS if t_pass is None else t_pass
    t_floor = T_FLOOR if t_floor is None else t_floor
    t_ctx = T_CTX if t_ctx is None else t_ctx
    query = (query or "").strip()
    detail = {}

    hit = intent_blocked(query)
    if hit:
        detail["intent"] = hit
        return False, "intent", detail

    m, sp, sn = margin_fn(query)
    detail.update(margin=m, pos=sp, neg=sn)
    if m >= t_pass:
        return True, "on_topic", detail
    if m <= t_floor:
        return False, "off_topic", detail

    # 灰區：只有「像追問且上一句本身合法」才用上一句救援
    if prev and is_followup(query):
        pm, _, _ = margin_fn(prev)
        detail["prev_margin"] = pm
        if pm >= t_pass:
            cm, _, _ = margin_fn(f"{prev}，{query}")
            detail["ctx_margin"] = cm
            if cm >= t_ctx:
                return True, "followup_ctx", detail
            return False, "followup_ctx_low", detail
        return False, "prev_off_topic", detail
    return False, "ambiguous", detail

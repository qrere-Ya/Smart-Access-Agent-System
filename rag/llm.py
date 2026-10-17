"""
llm.py：助理用的 LLM 取得（本機 Ollama 或雲端 API，雲端失效自動改本機）。

主要功能：
  - 依 runtime/llm_settings.json 的設定組出可串流呼叫的 LLM
  - 模式 api 且金鑰可用 → 用雲端 API，本機模型待命當備援；否則用本機 Ollama
  - 兩個都不能用時回傳 None 與白話說明，由呼叫端顯示

串接模組：
  - llm_settings：設定讀寫、兩種用戶端與失敗備援
  - answer：回答與意圖判斷各取一個 LLM
"""
from typing import Optional, Tuple

from rag import llm_settings as ls


def build_llm(settings: Optional[dict] = None, max_tokens: Optional[int] = None) -> Tuple[Optional[object], str]:
    """回傳 (llm 或 None, 給畫面看的一行說明)；max_tokens 限制單次輸出長度（意圖判斷用小一點）。"""
    s = settings or ls.load_settings()
    local_ok, local_err, _names, _text, _sec = ls.check_local(s)
    local = ls.OllamaChatLLM(ls.local_host(s), s["local_model"], max_tokens=max_tokens) if local_ok else None

    note = ""
    if s["mode"] == "api":
        ok, err, _text, _sec = ls.check_api(s, timeout=15.0)
        if ok:
            api = ls.OpenAICompatLLM(s["api_base"], ls.effective_api_key(s), s["api_model"], max_tokens=max_tokens)
            return ls.FailoverLLM(api, local), f"助理使用{api.label()}" + ("，失效時自動改用本機模型" if local else "")
        what, _ = ls.explain(err, s)
        note = f"雲端 API 目前無法使用（{what}），已改用本機模型。"

    if local is None:
        return None, "本機模型也無法使用：\n" + ls.format_error(local_err, s, markdown=False)
    return local, (note + " " if note else "") + f"助理使用{local.label()}"

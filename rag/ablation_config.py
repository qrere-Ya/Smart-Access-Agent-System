"""
ablation_config.py：消融實驗的機制開關。

主要功能：
  - 用環境變數控制 4 個機制的開關，預設全部開啟（維持正式系統的行為）
      ABLATION_USE_LONG_CONTEXT_REORDER：法規檢索結果重排（rag_chat_engine）
      ABLATION_USE_SEMANTIC_GUARDRAIL：語意防護欄，關閉後改用純關鍵字（rag_guardrail）
      ABLATION_USE_KEYWORD_FASTPATH：考勤關鍵字快速通道（rag_guardrail）
      ABLATION_USE_DETERMINISTIC_SQL：確定性 SQL 查詢，關閉後全交給 LLM 翻譯（rag_sql_engine）
  - current_config：回傳目前各開關狀態，供評估報告標註
  環境變數在模組載入時讀取一次，每個設定各開新的子程序執行。

串接模組：
  - rag_chat_engine、rag_guardrail、rag_sql_engine：依開關決定是否啟用機制
  - evaluate_rag：報告中標註本輪設定
  - run_ablation_suite：逐一關閉單一機制跑評估
"""

import os


def _env_bool(name: str, default: bool) -> bool:
    """讀取布林環境變數：0、false、no、off、空字串視為關閉"""
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() not in ("0", "false", "no", "off", "")


USE_LONG_CONTEXT_REORDER = _env_bool("ABLATION_USE_LONG_CONTEXT_REORDER", True)
USE_SEMANTIC_GUARDRAIL = _env_bool("ABLATION_USE_SEMANTIC_GUARDRAIL", True)
USE_KEYWORD_FASTPATH = _env_bool("ABLATION_USE_KEYWORD_FASTPATH", True)
USE_DETERMINISTIC_SQL = _env_bool("ABLATION_USE_DETERMINISTIC_SQL", True)


def current_config() -> dict:
    """回傳目前 4 個開關的狀態"""
    return {
        "use_long_context_reorder": USE_LONG_CONTEXT_REORDER,
        "use_semantic_guardrail": USE_SEMANTIC_GUARDRAIL,
        "use_keyword_fastpath": USE_KEYWORD_FASTPATH,
        "use_deterministic_sql": USE_DETERMINISTIC_SQL,
    }

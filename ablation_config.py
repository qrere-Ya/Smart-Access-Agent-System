"""
消融實驗（Ablation Study）開關設定。

【2026-09-14，學術強化方向 1 新增】讓 run_ablation_suite.py 可以系統性地「關掉」
某一個既有機制、重跑 evaluate_rag.py 的自動化評估，比較有無該機制時的答題合格率／
檢索命中率／幻覺率差異，產出消融實驗的比較表，直接放進論文的實驗章節。

對應 4 個機制（見 academic-enhancement-plan.md 方向 1）：
  1. USE_LONG_CONTEXT_REORDER  —— rag_chat_engine.py 的 LongContextReorder
  2. USE_SEMANTIC_GUARDRAIL    —— rag_guardrail.py 的對比式語意防護欄（關掉後改用
                                    單純關鍵字過濾）
  3. USE_KEYWORD_FASTPATH      —— rag_guardrail.py 考勤路由的關鍵字快速通道（關掉後
                                    純靠語意相似度分類）
  4. USE_DETERMINISTIC_SQL     —— rag_sql_engine.py 的「姓名+具體日期」確定性查詢
                                    （關掉後全部交給小模型翻譯 SQL）

【重要：不影響正式系統】下面 4 個開關預設值全部是 True，代表「維持目前正式系統的
既有行為」。沒有人手動設定對應的環境變數時，rag_guardrail.py／rag_chat_engine.py／
rag_sql_engine.py 的行為跟這個消融實驗功能加進來之前完全一樣，不會不小心影響到
已經在真實環境驗證過的正式問答流程（考勤路由、SQL 生成、混合型問題查詢等）。
只有在明確用 run_ablation_suite.py（或手動設定下面的環境變數）跑消融實驗時，
才會真的把某個開關關掉，而且每次只關一個，其餘 3 個維持預設開啟。
"""

import os


def _env_bool(name: str, default: bool) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() not in ("0", "false", "no", "off", "")


# 讀取時機：模組被 import 的當下就會讀一次環境變數，之後在同一個 Python 程序裡不會
# 再變動。這是刻意的設計，不是疏漏——消融實驗要「每個設定各跑一次」，run_ablation_suite.py
# 是用「每個設定各開一個全新的子程序」來跑（見該檔案說明），不是在同一個程序裡切來切去，
# 所以不需要支援程序執行中途動態切換，模組載入時讀一次環境變數就足夠、也最不容易出錯。
USE_LONG_CONTEXT_REORDER = _env_bool("ABLATION_USE_LONG_CONTEXT_REORDER", True)
USE_SEMANTIC_GUARDRAIL = _env_bool("ABLATION_USE_SEMANTIC_GUARDRAIL", True)
USE_KEYWORD_FASTPATH = _env_bool("ABLATION_USE_KEYWORD_FASTPATH", True)
USE_DETERMINISTIC_SQL = _env_bool("ABLATION_USE_DETERMINISTIC_SQL", True)


def current_config() -> dict:
    """回傳目前這個程序裡 4 個開關的實際狀態，供評估報告標註「這一輪跑的是哪個設定」用。"""
    return {
        "use_long_context_reorder": USE_LONG_CONTEXT_REORDER,
        "use_semantic_guardrail": USE_SEMANTIC_GUARDRAIL,
        "use_keyword_fastpath": USE_KEYWORD_FASTPATH,
        "use_deterministic_sql": USE_DETERMINISTIC_SQL,
    }

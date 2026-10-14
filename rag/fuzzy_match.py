"""
fuzzy_match.py：中文關鍵字、姓名的錯字容錯比對。

主要功能：
  - min_edit_distance_in_text：在長文字中找與目標字串最接近的片段，回傳最小編輯距離
    （容許少量字元增刪改；命中門檻由呼叫端決定）

串接模組：
  - rag_guardrail：考勤關鍵字快速通道
  - rag_sql_engine：員工姓名比對
  獨立成檔是為了避免 rag_guardrail 與 rag_sql_engine 互相 import 造成循環。
"""

from typing import Optional

from rapidfuzz.distance import Levenshtein


def min_edit_distance_in_text(target: str, text: str) -> Optional[int]:
    """
    比對 text 中長度為 len(target)-1、len(target)、len(target)+1 的所有連續片段，
    回傳與 target 的最小編輯距離；text 太短湊不出視窗時回傳 None（不應視為命中）。
    """
    best = None
    for window_len in (len(target) - 1, len(target), len(target) + 1):
        if window_len < 1 or window_len > len(text):
            continue
        for start in range(0, len(text) - window_len + 1):
            window = text[start:start + window_len]
            distance = Levenshtein.distance(target, window)
            if best is None or distance < best:
                best = distance
    return best

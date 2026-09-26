"""
中文姓名/關鍵字的錯字容錯比對。

【2026-09-26，新增】考勤查詢的路由判斷（rag_guardrail.py）與員工姓名比對
（rag_sql_engine.py）原本都是精確子字串比對，使用者打錯一個字（例如「考勤」
打成「考勸」）就完全查不到，直接被 fallback 到不穩定的小模型路線甚至查無
資料。這裡提供一個共用的「滑動視窗編輯距離」比對函式：允許目標字串（關鍵字
或姓名）出現在較長文字裡的任何位置，且容許少量字元的增刪改，取代原本的
精確比對。

放在獨立檔案，不是塞進 rag_guardrail.py 或 rag_sql_engine.py 裡，是因為
rag_guardrail.py 已經 import rag_sql_engine（用來查員工名冊判斷 mixed
題型），如果把這個函式放進其中一個檔案、讓另一個檔案 import，會變成循環
import。

【為什麼不用 rapidfuzz.fuzz.partial_ratio()】實測比較過：對「短關鍵字/姓名
+ 1 個字打錯」這種案例，partial_ratio 在字串所在的句子長度不同時，算出來
的分數不穩定（同樣打錯一個字，短句子算出 80 分、長句子只算出 75 分，同一個
門檻反而卡不住），改用「明確枚舉所有可能的比對視窗、算最小編輯距離」的作法，
行為更可預期、也更容易寫單元測試驗證邊界案例。
"""

from typing import Optional

from rapidfuzz.distance import Levenshtein


def min_edit_distance_in_text(target: str, text: str) -> Optional[int]:
    """
    在 text 裡找長度跟 target 相近（-1/0/+1，涵蓋增字/減字/替換三種打錯字模式）
    的所有連續子字串視窗，回傳跟 target 的最小編輯距離。

    如果 text 比 target 短到連 len(target)-1 的視窗都湊不出來，回傳 None
    （代表資訊量不足以判斷，呼叫端不應該當作命中）。

    這裡只負責「算距離」，命中門檻交給呼叫端自己決定——關鍵字快速通道跟
    員工姓名比對兩種情境的誤判代價不一樣（誤判關鍵字頂多多繞去語意分類，
    誤判姓名可能把 A 的打卡紀錄當成 B 的回答出去），門檻不該寫死在這裡。
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

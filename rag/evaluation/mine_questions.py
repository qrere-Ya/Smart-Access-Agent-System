"""
mine_questions.py：把組員在遠端測試入口真實問過的題目，整理成「待標註的候選題」。

主要功能：
  - 讀 runtime/remote_questions.jsonl，去掉已經在題庫裡的題目（完全相同的字串）與重複的題目
  - 每題附上系統當時實際選的工具與結果，只當作參考；標準答案要由人填（不能直接採用系統的選擇，
    否則評估只是在確認系統同意自己）
  - 輸出 runtime/tool_candidates.json（不進版控，內含真實姓名）；組員名稱一律換成 teammate，不輸出

串接模組：
  - tool_eval：標好的題目貼進 tool_questions.json 的 test 分組（source 填 teammate、reviewed 填 false）
執行：python -m rag.evaluation.mine_questions
"""
import json
import os
from typing import Dict, List

from rag.evaluation import tool_eval
from shared import paths

CANDIDATES_PATH = os.path.join(paths.RUNTIME_DIR, "tool_candidates.json")


def mine(log_path: str = paths.REMOTE_LOG_PATH, known: List[str] = None) -> List[Dict]:
    """回傳尚未在題庫裡的不重複題目 [{q, observed_tools, observed_outcome, count, expected_tools: None}]。"""
    known_set = set(known if known is not None else (x["q"] for x in tool_eval.load_questions()))
    seen: Dict[str, dict] = {}
    if not os.path.exists(log_path):
        return []
    with open(log_path, encoding="utf-8") as f:
        for line in f:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            question = str(row.get("question", "")).strip()
            if not question or question in known_set:
                continue
            entry = seen.setdefault(question, {"q": question, "observed_tools": row.get("tools", []),
                                               "observed_outcome": row.get("outcome", ""), "count": 0,
                                               "expected_tools": None, "source": "teammate"})
            entry["count"] += 1
    return list(seen.values())


def main():
    candidates = mine()
    with open(CANDIDATES_PATH, "w", encoding="utf-8") as f:
        json.dump(candidates, f, ensure_ascii=False, indent=1)
    print(f"找到 {len(candidates)} 題新的候選題，已存到 {CANDIDATES_PATH}")
    print("請人工填 expected_tools（工具清單，不查資料庫就是 []），再放進 tool_questions.json 的 test 分組。")


if __name__ == "__main__":
    main()

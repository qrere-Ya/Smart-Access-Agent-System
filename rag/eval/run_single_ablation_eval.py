"""
run_single_ablation_eval.py：消融實驗的單輪執行器（由 run_ablation_suite.py 以子程序呼叫）。

主要功能：
  - 依目前環境變數的開關設定，無介面地跑一次 evaluate_rag 的自動化評估
  - 把量化結果以 ABLATION_RESULT_JSON_START / END 標記包住的 JSON 印出，供父程序擷取
  手動測試單一設定：先設好環境變數再執行，例如
    ABLATION_USE_DETERMINISTIC_SQL=0 python -m rag.eval.run_single_ablation_eval --questions-per-category 5

串接模組：
  - evaluate_rag：run_dynamic_evaluation
  - run_ablation_suite：父程序，負責設定環境變數並解析輸出
"""

import argparse
import json
import random
import sys

# 被父程序用管線接走時，Windows 預設編碼印不出特殊符號會當掉，所以強制使用 UTF-8
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from rag.eval.evaluate_rag import run_dynamic_evaluation


def _drain(gen):
    """把 generator 跑到底並回傳它 return 的結果（忽略中途 yield 給畫面的內容）"""
    try:
        while True:
            next(gen)
    except StopIteration as e:
        return e.value or {}


def main():
    parser = argparse.ArgumentParser(description="消融實驗單輪執行器（由 run_ablation_suite.py 呼叫）")
    parser.add_argument("--questions-per-category", type=int, default=10, help="每一類（法規/考勤/正負例）跑幾題")
    parser.add_argument("--label", type=str, default="unnamed", help="這一輪的設定標籤，單純附加在輸出結果裡方便對照")
    parser.add_argument(
        "--seed", type=int, default=None,
        help="固定亂數種子（由 run_ablation_suite.py 統一帶入同一個值，讓 5 輪盡量抽到同一批法規題目、"
             "同一個正負例洗牌順序，減少「純粹因為這次抽到不同題目」造成的雜訊，讓比較更公平）。"
             "不指定就是不固定（每次都真隨機）。",
    )
    args = parser.parse_args()

    if args.seed is not None:
        random.seed(args.seed)

    print(f"▶ [單輪執行器] label={args.label!r}，每類 {args.questions_per_category} 題，"
          f"seed={args.seed}，開始執行...", flush=True)

    history = []
    gen = run_dynamic_evaluation(history, questions_per_category=args.questions_per_category)
    metrics = _drain(gen)

    if not metrics:
        print("⚠️ [單輪執行器] run_dynamic_evaluation() 沒有回傳任何量化結果（可能整個流程中途出錯），"
              "仍會輸出一份空結果，父程序那邊這一輪會被標記失敗。", flush=True)

    metrics["label"] = args.label
    metrics["questions_per_category"] = args.questions_per_category
    metrics["seed"] = args.seed

    # 用固定標記包住 JSON，父程序只擷取這一段，不受其他診斷訊息干擾
    print("ABLATION_RESULT_JSON_START", flush=True)
    print(json.dumps(metrics, ensure_ascii=False), flush=True)
    print("ABLATION_RESULT_JSON_END", flush=True)


if __name__ == "__main__":
    main()

"""
消融實驗（Ablation Study）單輪執行器 —— 學術強化方向 1。

【2026-09-14 新增】跑一次 evaluate_rag.py 的 run_dynamic_evaluation()（跟你平常在
Gradio 畫面上按「Execute Evaluate」是同一套評估邏輯，只是這裡改成純文字、非互動、
單一設定跑一次），跑完把量化結果印成一段用固定標記包住的 JSON，方便
run_ablation_suite.py（父程序）從子程序的 stdout 裡準確擷取，不用去解析畫面上那份
給人看的 Markdown 報告文字。

【不是拿來單獨執行的】這支腳本本身不切換任何消融實驗開關——開關是由
run_ablation_suite.py 在啟動這支腳本的「子程序」之前，透過環境變數（例如
ABLATION_USE_LONG_CONTEXT_REORDER=0）設定好的，這支腳本只負責「照目前的環境變數
設定跑一次、把結果印出來」。如果你想手動測試單一設定，可以自己先設好環境變數再
執行這支腳本，例如：
    ABLATION_USE_DETERMINISTIC_SQL=0 python run_single_ablation_eval.py --questions-per-category 5
"""

import argparse
import json
import random
import sys

# 【2026-09-14 修正，真實環境跑批發現】Windows 上這支腳本被 run_ablation_suite.py
# 當子程序啟動、stdout 被父程序用管線（pipe）接走時，Python 不會用你平常在終端機
# 互動執行時看到的那個編碼（通常是 UTF-8 或至少涵蓋常用中文字），而是退回作業系統
# 的「ANSI 語言代碼頁」（繁體中文 Windows 通常是 cp936/GBK），這個編碼碼表裡連
# 「▶」這種常見符號都沒有，一遇到就整個腳本 crash、UnicodeEncodeError。這裡強制把
# stdout/stderr 重新設成 UTF-8（errors="replace"：萬一還有什麼字元真的編碼不了，
# 印成問號也不會讓整個程序死掉），不管是被子程序管線接走、還是你自己在終端機直接
# 執行，這支腳本印的任何中文或符號都不會再讓程序當掉。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from evaluate_rag import run_dynamic_evaluation


def _drain(gen):
    """
    手動把 generator 跑到底，接住它最後 `return` 的值（PEP 380：generator 的
    `return value` 會變成 StopIteration 例外的 `.value`）。中間每個 yield 出來的
    history 這裡故意不理會——那是給 Gradio 畫面即時顯示用的，這裡是無介面執行，
    只在乎跑完之後的最終數字。
    """
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

    # 用固定的起訖標記包住 JSON，父程序 (run_ablation_suite.py) 用字串搜尋抓中間這一段，
    # 不受 evaluate_rag.py 過程中大量印出的 [Debug ...] 診斷訊息干擾。
    print("ABLATION_RESULT_JSON_START", flush=True)
    print(json.dumps(metrics, ensure_ascii=False), flush=True)
    print("ABLATION_RESULT_JSON_END", flush=True)


if __name__ == "__main__":
    main()

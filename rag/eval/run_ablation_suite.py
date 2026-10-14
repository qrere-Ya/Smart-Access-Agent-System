"""
run_ablation_suite.py：消融實驗跑批主程式。

主要功能：
  - 依序用 5 種設定各跑一次自動化評估：基準線（全開），以及分別關閉
    LongContextReorder、語意防護欄、關鍵字快速通道、確定性 SQL 查詢
  - 每種設定各開一個新的子程序（run_single_ablation_eval.py），避免全域狀態互相汙染
  - 5 輪使用同一個亂數種子，讓法規抽樣與正負例洗牌盡量一致（考勤抽樣不受種子影響）
  - 輸出帶時間戳記的 Markdown 比較表與 CSV
  執行前需啟動 Ollama（評估若用雲端 API，需先在設定面板填金鑰），且資料庫有打卡資料、data/laws/ 有法規文件。
  執行方式：python -m rag.eval.run_ablation_suite --questions-per-category 10

串接模組：
  - run_single_ablation_eval：單輪執行器
  - ablation_config：由環境變數控制機制開關
"""

import argparse
import csv
import json
import os
import subprocess
import sys
from datetime import datetime

from shared import paths

# 避免終端機或重導向的預設編碼印不出特殊符號而當掉
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

CONFIGS = [
    {
        "label": "baseline_full",
        "name_zh": "基準線（4 個機制全開＝目前正式系統）",
        "env": {},
    },
    {
        "label": "no_long_context_reorder",
        "name_zh": "關閉 LongContextReorder",
        "env": {"ABLATION_USE_LONG_CONTEXT_REORDER": "0"},
    },
    {
        "label": "no_semantic_guardrail",
        "name_zh": "防護欄改純關鍵字過濾",
        "env": {"ABLATION_USE_SEMANTIC_GUARDRAIL": "0"},
    },
    {
        "label": "no_keyword_fastpath",
        "name_zh": "考勤路由關閉關鍵字快速通道",
        "env": {"ABLATION_USE_KEYWORD_FASTPATH": "0"},
    },
    {
        "label": "no_deterministic_sql",
        "name_zh": "關閉確定性 SQL 查詢",
        "env": {"ABLATION_USE_DETERMINISTIC_SQL": "0"},
    },
]


def _run_one(config, questions_per_category, script_dir, seed):
    """用新的子程序跑一種設定，回傳解析後的量化結果；失敗時回傳帶 error 欄位的 dict"""
    env = os.environ.copy()
    env.update(config["env"])
    # 子程序輸出接管線時，Windows 會退回系統編碼，所以指定 UTF-8
    env["PYTHONIOENCODING"] = "utf-8"
    cmd = [
        sys.executable,
        "-m", "rag.eval.run_single_ablation_eval",
        "--questions-per-category", str(questions_per_category),
        "--label", config["label"],
    ]
    if seed is not None:
        cmd += ["--seed", str(seed)]
    print(f"\n{'=' * 70}\n▶ 開始跑：{config['name_zh']}（{config['label']}）\n{'=' * 70}", flush=True)

    result = subprocess.run(
        cmd, env=env, cwd=script_dir, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )

    # 只印輸出最後一段，避免洗版
    tail = result.stdout[-2000:] if result.stdout else ""
    print(tail, flush=True)

    if result.returncode != 0:
        print(f"⚠️ 子程序執行失敗（returncode={result.returncode}）：\n{(result.stderr or '')[-2000:]}", flush=True)
        return {"label": config["label"], "name_zh": config["name_zh"], "error": "subprocess_failed"}

    stdout = result.stdout or ""
    try:
        start = stdout.index("ABLATION_RESULT_JSON_START") + len("ABLATION_RESULT_JSON_START")
        end = stdout.index("ABLATION_RESULT_JSON_END")
        metrics = json.loads(stdout[start:end].strip())
    except (ValueError, json.JSONDecodeError) as e:
        print(f"⚠️ 解析子程序輸出失敗：{e}", flush=True)
        return {"label": config["label"], "name_zh": config["name_zh"], "error": "parse_failed"}

    metrics["name_zh"] = config["name_zh"]
    return metrics


def _fmt_pct(v):
    """格式化成百分比，None 顯示為破折號"""
    if v is None:
        return "—"
    return f"{v:.1%}"


def _write_markdown(results, path, n):
    """輸出 Markdown 比較表"""
    lines = [
        "# RAG 消融實驗（Ablation Study）比較表",
        "",
        f"每個設定各跑 {n} 題／類別（純法規／純考勤／正負例），跑法：依序切換單一機制的開／關，",
        "其餘 3 個機制維持正式系統預設值（開）。產生時間：" + datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "",
        "| 設定 | 法規合格率 | 法規檢索命中率 | 法規路由正確率 | 法規幻覺率 "
        "| 考勤合格率 | 考勤路由正確率 | 考勤幻覺率 | 防護欄準確率 | 綜合合格率 | 綜合幻覺率 |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        if r.get("error"):
            lines.append(f"| {r.get('name_zh', r.get('label'))} | ⚠️ {r['error']}（見主控台輸出） | | | | | | | | | |")
            continue
        lines.append(
            "| "
            + " | ".join(
                [
                    r.get("name_zh", r.get("label", "?")),
                    _fmt_pct(r.get("law_pass_rate")),
                    _fmt_pct(r.get("law_hit_rate")),
                    _fmt_pct(r.get("law_route_rate")),
                    _fmt_pct(r.get("law_hallucination_rate")),
                    _fmt_pct(r.get("att_pass_rate")),
                    _fmt_pct(r.get("att_route_rate")),
                    _fmt_pct(r.get("att_hallucination_rate")),
                    _fmt_pct(r.get("guardrail_accuracy")),
                    _fmt_pct(r.get("combined_pass_rate")),
                    _fmt_pct(r.get("combined_hallucination_rate")),
                ]
            )
            + " |"
        )
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def _write_csv(results, path):
    """輸出 CSV"""
    fieldnames = [
        "label", "name_zh", "law_pass_rate", "law_hit_rate", "law_route_rate",
        "law_hallucination_rate", "att_pass_rate", "att_route_rate",
        "att_hallucination_rate", "guardrail_good_accuracy", "guardrail_bad_accuracy",
        "guardrail_accuracy", "combined_pass_rate", "combined_hallucination_rate",
        "is_qualified", "seed", "questions_per_category", "error",
    ]
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for r in results:
            writer.writerow(r)


def main():
    """依序跑完 5 種設定並輸出比較表，最後統計成功與失敗的輪數"""
    parser = argparse.ArgumentParser(description="消融實驗跑批腳本（依序跑 5 種設定並產出比較表）")
    parser.add_argument("--questions-per-category", type=int, default=10)
    parser.add_argument("--output-prefix", type=str, default=None, help="輸出檔名前綴，預設用時間戳記自動產生")
    parser.add_argument(
        "--seed", type=int, default=42,
        help="固定亂數種子，5 輪都會帶同一個值（見檔案最上面的說明），讓法規抽樣、正負例洗牌"
             "順序盡量一致，比較更公平；設成負數（例如 -1）代表不固定種子，5 輪各自真隨機。",
    )
    args = parser.parse_args()
    seed = None if args.seed is not None and args.seed < 0 else args.seed

    script_dir = paths.PROJECT_ROOT
    all_results = []
    for config in CONFIGS:
        metrics = _run_one(config, args.questions_per_category, script_dir, seed)
        all_results.append(metrics)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    prefix = args.output_prefix or f"ablation_results_{timestamp}"
    os.makedirs(paths.EVAL_RESULTS_DIR, exist_ok=True)
    md_path = os.path.join(paths.EVAL_RESULTS_DIR, f"{prefix}.md")
    csv_path = os.path.join(paths.EVAL_RESULTS_DIR, f"{prefix}.csv")

    _write_markdown(all_results, md_path, args.questions_per_category)
    _write_csv(all_results, csv_path)

    # 如實回報成功與失敗的輪數
    failed = [r for r in all_results if r.get("error")]
    ok_count = len(all_results) - len(failed)
    if failed:
        print(
            f"\n⚠️ 消融實驗 5 輪跑完，但有 {len(failed)}/{len(all_results)} 輪失敗"
            f"（{', '.join(r['label'] for r in failed)}），比較表裡這幾輪會是空白，"
            f"請往上捲動看對應那一輪印出來的錯誤訊息：\n  - {md_path}\n  - {csv_path}",
            flush=True,
        )
    else:
        print(f"\n✅ 消融實驗 5 輪全部成功跑完，比較表已輸出：\n  - {md_path}\n  - {csv_path}", flush=True)


if __name__ == "__main__":
    main()

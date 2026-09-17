"""
消融實驗（Ablation Study）跑批腳本 —— 學術強化方向 1，主入口。

【2026-09-14 新增】依序用 5 種設定各跑一次 evaluate_rag.py 的自動化評估：
    1. baseline_full             —— 基準線，4 個機制全部開啟（= 目前正式系統的行為）
    2. no_long_context_reorder   —— 關掉 LongContextReorder
    3. no_semantic_guardrail     —— 防護欄改用純關鍵字過濾（關掉對比式語意防護欄）
    4. no_keyword_fastpath       —— 考勤路由關掉關鍵字快速通道（純語意分類）
    5. no_deterministic_sql      —— 關掉確定性 SQL 查詢（全部交給小模型翻譯）

每種設定都用「全新的 Python 子程序」執行 run_single_ablation_eval.py（透過環境變數
帶入對應的開關設定），不是在同一個程序裡切換——因為 Settings.llm、
_GLOBAL_CHAT_ENGINE、_GLOBAL_SQL_ENGINE 這些都是 main_guardrail_rag.py 裡的模組級
全域變數，混在同一個程序裡連續跑好幾輪，上一輪殘留的狀態可能汙染下一輪的結果；
用子程序保證每一輪都是完全乾淨的起點，這點犧牲一些執行時間換取結果可信度，划算。

【執行前置需求】
  - 本地 Ollama 要有啟動（考生模型 qwen2:7b）
  - LiteLLM Proxy 要有啟動（出題官／裁判官用的 API LLM），例如：litellm --config config.yaml
  - 門禁系統資料庫 database/database.db 裡要有打卡資料，法規知識庫 data/laws/ 要有文件

【執行方式】在 Smart_Access_Agent_System 資料夾底下：
    python run_ablation_suite.py --questions-per-category 10

預設每類 10 題只是為了讓 5 輪加起來不用等太久（本地小模型 + API 裁判官都要跑滿
30 題 x 5 輪，非常花時間）。正式要放進論文的版本，建議在時間允許的情況下調高到
20（跟 evaluate_rag.py 正式系統評估用的樣本數一致），跑之前預留足夠時間。

【產出】在同一個資料夾底下輸出一份 Markdown 比較表跟一份 CSV，檔名帶時間戳記，
可以直接放進論文的實驗章節，或用 Excel/其他工具重畫圖表。

【2026-09-14 新增，真實環境第一輪實測後發現的方法論問題】evaluate_rag.py 的
_run_law_category() 每次都是「即時隨機抽一段法規知識塊、現場請裁判官 LLM 出題」，
_run_guardrail_category() 也會每次重新洗牌正負例題庫——這代表如果 5 輪之間完全不
固定亂數種子，每一輪測到的實際題目都不一樣，數字差異可能只是「剛好抽到比較難/
比較簡單的題目」，不是真的反映被關掉的那個機制的影響，這樣消融實驗的比較就不夠
嚴謹。這裡預設所有 5 輪都帶入同一個 `--seed`（預設 42），讓法規抽樣、正負例洗牌
順序在 5 輪之間盡量一致，把「換了題目」這個干擾因素降到最低。

【殘留的限制，老實說】即時考勤類別（_run_attendance_category()）抽樣是用 SQL 的
`ORDER BY RANDOM() LIMIT 1`（SQLite 自己的亂數，不是 Python 的 random 模組），
這裡的 `--seed` 對它沒有作用，考勤題目每一輪還是會不一樣。如果要徹底控制這個變因，
需要另外修改 rag_sql_engine.sample_random_attendance_record() 讓它也能接受固定
種子，目前沒有做這一步（風險相對較低：考勤資料庫裡目前只有少數幾筆打卡紀錄，
真的抽到差異很大的題目機率不高），跑出來的比較表裡考勤那幾欄數字請保守解讀。
"""

import argparse
import csv
import json
import os
import subprocess
import sys
from datetime import datetime

# 【2026-09-14 修正，真實環境跑批發現】跟 run_single_ablation_eval.py 同一個問題：
# 這支腳本自己印的 emoji（✅⚠️▶）在某些 Windows 終端機／被重導向到檔案時，也可能
# 用到系統的 ANSI 語言代碼頁（cp936/GBK）而不是 UTF-8，一樣有 UnicodeEncodeError
# 當掉的風險，這裡一併防禦性修正。
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
    env = os.environ.copy()
    env.update(config["env"])
    # 【2026-09-14 修正，真實環境跑批發現】這是真正的根因修正：子程序的 stdout 被這裡
    # 用管線（pipe）接走時，Windows 上的 Python 不會沿用你平常在終端機互動執行時的
    # 編碼，而是退回作業系統的 ANSI 語言代碼頁（繁體中文 Windows 通常是 cp936/GBK）
    # ——這個碼表連「▶」這種常見符號都沒有，一印就整個子程序 UnicodeEncodeError 當掉
    # （實測過：5 輪全部這樣失敗，比較表整份都是空的）。明確設定 PYTHONIOENCODING=utf-8
    # 讓子程序不管跑在哪種語言代碼頁的 Windows 上，永遠用 UTF-8 印輸出，才是真正解決
    # 問題，不是只讓崩潰訊息看起來好看一點。
    env["PYTHONIOENCODING"] = "utf-8"
    cmd = [
        sys.executable,
        os.path.join(script_dir, "run_single_ablation_eval.py"),
        "--questions-per-category", str(questions_per_category),
        "--label", config["label"],
    ]
    if seed is not None:
        cmd += ["--seed", str(seed)]
    print(f"\n{'=' * 70}\n▶ 開始跑：{config['name_zh']}（{config['label']}）\n{'=' * 70}", flush=True)

    result = subprocess.run(
        cmd, env=env, cwd=script_dir, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )

    # 子程序過程中會印很多 [Debug ...] 訊息，這裡只印最後一段避免洗版，完整輸出
    # 如果之後要除錯，可以自己改成印 result.stdout 全文。
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
    if v is None:
        return "—"
    return f"{v:.1%}"


def _write_markdown(results, path, n):
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

    script_dir = os.path.dirname(os.path.abspath(__file__))
    all_results = []
    for config in CONFIGS:
        metrics = _run_one(config, args.questions_per_category, script_dir, seed)
        all_results.append(metrics)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    prefix = args.output_prefix or f"ablation_results_{timestamp}"
    md_path = os.path.join(script_dir, f"{prefix}.md")
    csv_path = os.path.join(script_dir, f"{prefix}.csv")

    _write_markdown(all_results, md_path, args.questions_per_category)
    _write_csv(all_results, csv_path)

    # 【2026-09-14 修正，真實環境跑批發現】原本不管每一輪實際成功還是失敗，最後都會
    # 印同一句「✅ 全部跑完」，實測時 5 輪全部因為編碼問題失敗、比較表整份是空的，
    # 畫面卻還是顯示「✅」，會讓人誤以為跑成功了，要點開檔案才發現整份都是
    # ⚠️ subprocess_failed。這裡改成老實統計成功/失敗數量，有任何一輪失敗就不能用 ✅。
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

"""
tool_eval.py：評估意圖判斷（router）選工具的表現，需要真的語言模型。

主要功能：
  - 題庫 tool_questions.json 分兩份：dev（開發集，調整提示詞時看）與 test（封存測試集，只用來報告，不能拿來調整）；
    每題有標準答案（工具集合，可有 alt 可接受的其他答案）、組別（one 一件事、many 多件事、none 不查資料庫、
    followup 追問、adversarial 惡意或越權）、來源（author 自己寫、teammate 組員真實問過），
    選填：params（人名、狀態、事件類型）、history（前面的對話）、same_period（追問是否沿用上一題日期）
  - 每題重複問 runs 次；「每次都對」才算通過，信賴區間（Wilson）以題數計，因為同一題重複問不是獨立樣本
  - 每組有事先訂好的目標與信賴區間下限，題數太少（少於 MIN_QUESTIONS）就不下結論，只顯示樣本不足
  - 另外統計參數（人名、狀態、事件類型）是否正確、追問是否正確判斷沿用日期
  - 跑測試集前要加 --confirm-test，並記錄第幾次跑測試集（跑越多次，測試集越不獨立）
  題庫是作者與我自己標的，標準答案沒有經過第二個人審查（reviewed 欄位）；本評估只看「選對工具與參數」，
  不評估最後的回答是否正確。

串接模組：
  - router：build_prompt、parse_plan（被評估的對象）
  - llm：build_llm 取得目前設定的語言模型（與正式環境相同）
  - stats：Wilson 信賴區間
  - mine_questions：把組員真實問過的題目匯出成待標註的候選題
執行：python -m rag.evaluation.tool_eval --split dev --runs 3
"""
import argparse
import datetime
import json
import os
from typing import Callable, Dict, List, Optional

from rag import router
from rag.evaluation import stats
from shared import paths

QUESTIONS_PATH = os.path.join(os.path.dirname(__file__), "tool_questions.json")
LAST_RUN_PATH = os.path.join(paths.RUNTIME_DIR, "tool_eval_last.json")
TEST_RUNS_PATH = os.path.join(paths.RUNTIME_DIR, "tool_eval_test_runs.jsonl")
EVAL_DAY = datetime.date(2026, 11, 4)

GROUPS = ("one", "many", "none", "followup", "adversarial")
GROUP_NAMES = {"one": "一件事", "many": "多件事", "none": "不查資料庫", "followup": "追問", "adversarial": "惡意越權"}
TARGETS = {"one": 0.95, "many": 0.85, "none": 0.95, "followup": 0.85, "adversarial": 0.95, "params": 0.90}
MIN_LOWER = 0.75        # 信賴區間下限至少要這麼高才算達標
MIN_QUESTIONS = 10      # 題數少於這個數字不下結論


def load_questions(path: str = QUESTIONS_PATH, split: str = "all") -> List[dict]:
    with open(path, encoding="utf-8") as f:
        items = json.load(f)
    return [x for x in items if split == "all" or x["split"] == split]


def chosen_tools(plan) -> Optional[set]:
    """計畫實際會執行的工具集合（主查詢加追加查詢）；格式不合為 None。
    問題指名某個人時，正式流程會把 summary、status_list 改成查那個人的 records（answer._answer），這裡照同樣規則。"""
    if plan is None:
        return None
    main = "records" if plan.person and plan.data in ("summary", "status_list") else plan.data
    return ({main} if main else set()) | {s.data for s in plan.steps}


def accepted(item: dict) -> List[set]:
    """這題可接受的工具集合：標準答案加上 alt。"""
    return [set(item["tools"])] + [set(a) for a in item.get("alt", [])]


def params_ok(item: dict, plan) -> Optional[bool]:
    """人名、狀態、事件類型是否和題目標的一致（只比主查詢）；題目沒有標 params 回傳 None。"""
    expected = item.get("params")
    if not expected:
        return None
    if plan is None:
        return False
    if "person" in expected and plan.person != expected["person"]:
        return False
    if "status" in expected and plan.status != router.STATUS_ALIASES.get(expected["status"], expected["status"]):
        return False
    return not ("event_type" in expected and plan.event_type != expected["event_type"])


def check(item: dict, plan) -> dict:
    """一次回答的判定：tools_ok（工具集合在可接受範圍）、params_ok、period_ok（追問是否正確判斷沿用日期）。"""
    got = chosen_tools(plan)
    period = None
    if "same_period" in item:
        period = plan is not None and plan.same_period == item["same_period"]
    return {"got": None if got is None else sorted(got), "tools_ok": got in accepted(item),
            "params_ok": params_ok(item, plan), "period_ok": period,
            "person": getattr(plan, "person", None), "status": getattr(plan, "status", None)}


def evaluate(complete: Callable[[str], str], questions: List[dict], multi: bool = True, runs: int = 1) -> List[dict]:
    """每題問 runs 次，回傳 [{item, trials: [check 的結果]}]；complete 是 prompt -> 文字的函式。"""
    results = []
    for item in questions:
        trials = []
        for _ in range(runs):
            prompt = router.build_prompt(item["q"], item.get("history", ""), EVAL_DAY, multi=multi)
            try:
                plan = router.parse_plan(complete(prompt))
            except Exception as e:   # 呼叫失敗算這次答錯，不讓整個評估中斷
                print(f"[tool_eval] {item['id']} 呼叫失敗：{type(e).__name__}")
                plan = None
            trials.append(check(item, plan))
        results.append({"item": item, "trials": trials})
    return results


def passed(item: dict, trial: dict) -> bool:
    """這次回答算不算通過：工具對；追問題還要沿用日期的判斷也對。"""
    return trial["tools_ok"] and trial["period_ok"] is not False


def _row(label: str, ok: int, n: int, trials_ok: int, trials: int, target: float) -> dict:
    low, high = stats.wilson(ok, n)
    if n < MIN_QUESTIONS or stats.wilson(n, n)[0] < MIN_LOWER:   # 就算全對，下限也到不了門檻：題數太少，不下結論
        verdict = "樣本不足"
    else:
        verdict = "達標" if ok / n >= target and low >= MIN_LOWER else "未達標"
    return {"label": label, "questions": n, "stable": ok, "stable_rate": ok / n if n else 0.0,
            "trial_rate": trials_ok / trials if trials else 0.0, "low": low, "high": high, "target": target,
            "verdict": verdict}


def summarize(results: List[dict]) -> Dict[str, dict]:
    """各組、參數、追問日期的統計；通過要求每次都對（以題為單位）。"""
    out = {}
    for group in GROUPS:
        rows = [r for r in results if r["item"]["group"] == group]
        if not rows:
            continue
        ok = sum(all(passed(r["item"], t) for t in r["trials"]) for r in rows)
        trials = [t for r in rows for t in r["trials"]]
        trials_ok = sum(passed(r["item"], t) for r in rows for t in r["trials"])
        out[group] = _row(GROUP_NAMES[group], ok, len(rows), trials_ok, len(trials), TARGETS[group])
    with_params = [r for r in results if r["item"].get("params")]
    if with_params:
        ok = sum(all(t["params_ok"] for t in r["trials"]) for r in with_params)
        trials_ok = sum(t["params_ok"] for r in with_params for t in r["trials"])
        out["params"] = _row("參數（人名、狀態、事件類型）", ok, len(with_params), trials_ok,
                             sum(len(r["trials"]) for r in with_params), TARGETS["params"])
    return out


def failures(results: List[dict]) -> List[dict]:
    """沒有每次都通過的題目（含工具、參數、追問日期任一項錯），附每次的結果。"""
    out = []
    for r in results:
        item, trials = r["item"], r["trials"]
        bad = [t for t in trials if not passed(item, t) or t["params_ok"] is False]
        if bad:
            out.append({"id": item["id"], "q": item["q"], "want": [sorted(s) for s in accepted(item)],
                        "same_period": item.get("same_period"), "wrong_runs": len(bad), "runs": len(trials),
                        "trials": trials})
    return out


def _print_summary(title: str, summary: Dict[str, dict], results: List[dict]):
    unreviewed = sum(1 for r in results if not r["item"].get("reviewed"))
    print(f"\n【{title}】題數 {len(results)}（標準答案未經第二人審查：{unreviewed} 題）")
    print(f"{'組別':<22}{'題數':>5}{'每次都對':>9}{'比例':>8}{'每次平均':>9}{'95%區間':>16}{'目標':>7}  判定")
    for row in summary.values():
        print(f"{row['label']:<20}{row['questions']:>5}{row['stable']:>9}{row['stable_rate']:>8.0%}{row['trial_rate']:>9.0%}"
              f"{row['low']:>9.0%}～{row['high']:<5.0%}{row['target']:>7.0%}  {row['verdict']}")


def _print_failures(results: List[dict]):
    for f in failures(results):
        got = ["格式不合" if t["got"] is None else "+".join(t["got"]) or "(無)" for t in f["trials"]]
        want = " 或 ".join("+".join(w) or "(無)" for w in f["want"])
        extra = f"（應沿用日期：{f['same_period']}）" if f["same_period"] is not None else ""
        print(f"  錯 {f['id']} {f['q']}　應為 {want}{extra}　各次實際 {got}")


def _record_test_run() -> int:
    """記錄這是第幾次跑測試集（回傳含這次的次數）。"""
    count = 1
    if os.path.exists(TEST_RUNS_PATH):
        with open(TEST_RUNS_PATH, encoding="utf-8") as f:
            count += sum(1 for line in f if line.strip())
    with open(TEST_RUNS_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps({"time": datetime.datetime.now().isoformat(timespec="seconds")}) + "\n")
    return count


def main(argv=None):
    parser = argparse.ArgumentParser(description="意圖判斷選工具的評估（需要真的語言模型）")
    parser.add_argument("--split", choices=("dev", "test", "all"), default="dev")
    parser.add_argument("--runs", type=int, default=3, help="每題問幾次")
    parser.add_argument("--mode", choices=("multi", "single", "both"), default="multi")
    parser.add_argument("--llm", choices=("settings", "api", "local"), default="settings",
                        help="語言模型：settings＝照 runtime/llm_settings.json；api／local＝只在這次評估改用該模式")
    parser.add_argument("--confirm-test", action="store_true", help="確認要跑封存測試集（調整完成後才跑）")
    parser.add_argument("--out", default=LAST_RUN_PATH, help="把完整結果存成 JSON")
    args = parser.parse_args(argv)
    if args.split != "dev" and not args.confirm_test:
        raise SystemExit("測試集是封存的，只用來報告、不能拿來調整提示詞。確定要跑請加 --confirm-test。")

    from rag import llm as rllm
    from rag import llm_settings

    model, label = rllm.build_llm(llm_settings.settings_for(args.llm), max_tokens=200)
    if model is None:
        raise SystemExit(f"沒有可用的語言模型：{label}")
    if args.llm == "api" and not isinstance(model, llm_settings.FailoverLLM):
        raise SystemExit(f"指定了 --llm api，但雲端目前無法使用，不評估（避免把本機模型的結果當成雲端的）：{label}")
    print(f"語言模型：{label}")
    if args.split != "dev":
        print(f"⚠️ 這是第 {_record_test_run()} 次跑測試集；跑越多次，測試集越不能代表「沒看過的題目」。")
    questions = load_questions(split=args.split)
    saved = {}
    for multi in ((True, False) if args.mode == "both" else (args.mode == "multi",)):
        results = evaluate(model.complete, questions, multi, args.runs)
        summary = summarize(results)
        name = "多步提示詞" if multi else "單步提示詞"
        _print_summary(f"{name}／{args.split}／每題 {args.runs} 次", summary, results)
        if getattr(model, "switched", False):
            print("⚠️ 評估途中雲端失敗，改用了備援的本機模型：這份結果混了兩個模型，不能當雲端的結果。")
        _print_failures(results)
        saved[name] = {"summary": summary, "failures": failures(results)}
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump({"time": datetime.datetime.now().isoformat(timespec="seconds"), "split": args.split,
                   "runs": args.runs, "model": label, "results": saved}, f, ensure_ascii=False, indent=1)
    print(f"\n完整結果：{args.out}")


if __name__ == "__main__":
    main()

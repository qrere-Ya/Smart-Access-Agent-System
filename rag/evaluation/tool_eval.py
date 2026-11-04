"""
tool_eval.py：評估意圖判斷選工具的正確率，並比較「單步」與「多步」兩種提示詞（需要真的語言模型）。

主要功能：
  - 讀 tool_questions.json（每題標準答案是應該用到的工具集合；只問文件的題目是空集合）
  - 每題各用單步提示詞與多步提示詞問一次 LLM，比較選出的工具集合（含主查詢與追加查詢）是否完全一致
  - 分「一件事」與「多件事」兩組統計，並列出答錯的題目；題目只用來判斷工具，不評估人名與日期

串接模組：
  - router：build_prompt、parse_plan
  - llm：build_llm 取得目前設定的語言模型
執行：python -m rag.evaluation.tool_eval
"""
import datetime
import json
import os

from rag import router

QUESTIONS_PATH = os.path.join(os.path.dirname(__file__), "tool_questions.json")
EVAL_DAY = datetime.date(2026, 11, 4)


def load_questions(path=QUESTIONS_PATH):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def chosen_tools(plan):
    """計畫實際會用到的工具集合（主查詢加追加查詢）。"""
    if plan is None:
        return None
    return ({plan.data} if plan.data else set()) | {s.data for s in plan.steps}


def evaluate(complete, multi, questions=None):
    """回傳每題 (題目, 標準答案集合, 選出集合或 None)；complete 是 prompt -> 文字的函式。"""
    results = []
    for item in questions or load_questions():
        plan = router.parse_plan(complete(router.build_prompt(item["q"], "", EVAL_DAY, multi=multi)))
        results.append((item["q"], set(item["tools"]), chosen_tools(plan)))
    return results


def summarize(results):
    """分組統計：one（標準答案 0 或 1 個工具）、many（2 個以上）；格式不合的回答算錯。"""
    out = {"one": [0, 0], "many": [0, 0]}
    for _, want, got in results:
        group = out["many" if len(want) >= 2 else "one"]
        group[1] += 1
        group[0] += int(got == want)
    return out


def main():
    from rag import llm as rllm

    model, label = rllm.build_llm()
    if model is None:
        raise SystemExit(f"沒有可用的語言模型：{label}")
    print(f"語言模型：{label}")
    questions = load_questions()
    for multi in (False, True):
        results = evaluate(model.complete, multi, questions)
        s = summarize(results)
        name = "多步" if multi else "單步"
        print(f"\n【{name}提示詞】一件事 {s['one'][0]}/{s['one'][1]}　多件事 {s['many'][0]}/{s['many'][1]}")
        for q, want, got in results:
            if got != want:
                print(f"  錯：{q}　應為 {sorted(want)}　實際 {sorted(got) if got is not None else '格式不合'}")


if __name__ == "__main__":
    main()

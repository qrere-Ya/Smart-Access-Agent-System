"""
retrieval_eval.py：檢索品質評估（不需要 LLM 當評審）。

主要功能：
  - 讀入題庫 questions.json（詳細、標準、攏統、刁鑽四種難度），每題標有「正確的文件與條號」，如「勞動基準法 第38條」
  - 對每題取檢索結果，算正確條文出現在前 1、3、5 名的比例（hit@k）與平均倒數名次（MRR），按難度分組
  - 法規裡沒有答案的題目（in_scope 為 false）不算命中，改記最高分數，用來決定「找不到依據」的門檻
  - 可比較「只用混合檢索」與「混合檢索加重排」兩種做法

執行：python -m assistant.evaluation.retrieval_eval（需要 models/ 內的 bge-m3 與重排模型）

串接模組：
  - doc_store、rerank：被評估的檢索流程
"""
import json
import os
import re
from collections import defaultdict
from typing import Callable, Dict, List, Optional, Sequence, Tuple

LEVELS = ("詳細", "標準", "攏統", "刁鑽")
KS = (1, 3, 5)
QUESTIONS_PATH = os.path.join(os.path.dirname(__file__), "questions.json")

# (「文件標題 條號」, 分數)，由前到後排序；分數用來觀察「沒有依據」的題目
Ranking = List[Tuple[str, Optional[float]]]


def load_questions(path: str = QUESTIONS_PATH) -> List[dict]:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)["questions"]


def chunk_key(chunk: dict) -> str:
    """一塊內容的比對鍵：「文件標題 條號」；沒有條號的文件（如聲明書）就只有標題。"""
    return f"{chunk['title']} {base_article(chunk['article'])}".strip()


def base_article(article: str) -> str:
    """去掉「（第 1/2 段）」之類的後綴，只留條號。"""
    return re.sub(r"（第 \d+/\d+ 段）$", "", article).strip()


def first_hit_rank(ranking: Ranking, gold: Sequence[str]) -> Optional[int]:
    """正確條號第一次出現的名次（從 1 開始），沒有出現回傳 None；同一條的多段只算一次名次。"""
    seen: List[str] = []
    for article, _ in ranking:
        base = base_article(article)
        if base not in seen:
            seen.append(base)
        if base in gold:
            return seen.index(base) + 1
    return None


def summarize(questions: Sequence[dict], rankings: Dict[str, Ranking]) -> Dict[str, dict]:
    """依難度與全體彙總；回傳 {難度: {n, hit@1, hit@3, hit@5, mrr}}，另有「全部」與「無依據題」。"""
    groups = defaultdict(list)
    out_of_scope_top: List[float] = []
    for q in questions:
        ranking = rankings[q["id"]]
        if not q["in_scope"]:
            out_of_scope_top.append(max((s for _, s in ranking if s is not None), default=0.0))
            continue
        groups[q["level"]].append(first_hit_rank(ranking, q["gold"]))
        groups["全部"].append(first_hit_rank(ranking, q["gold"]))
    result = {}
    for name, ranks in groups.items():
        result[name] = {"n": len(ranks), "mrr": sum(1 / r for r in ranks if r) / len(ranks)}
        for k in KS:
            result[name][f"hit@{k}"] = sum(1 for r in ranks if r and r <= k) / len(ranks)
    result["無依據題最高分"] = out_of_scope_top
    return result


def evaluate(search: Callable[[str], Ranking], questions: Optional[Sequence[dict]] = None):
    """對每題呼叫 search(問題)，回傳 (彙總, 每題名次)。"""
    questions = list(questions or load_questions())
    rankings = {q["id"]: search(q["question"]) for q in questions}
    return summarize(questions, rankings), rankings


def format_table(title: str, summary: Dict[str, dict]) -> str:
    lines = [f"【{title}】", f"{'難度':<6}{'題數':>4}  " + "  ".join(f"hit@{k:<2}" for k in KS) + "    MRR"]
    for name in (*LEVELS, "全部"):
        if name in summary:
            s = summary[name]
            lines.append(f"{name:<6}{s['n']:>4}  " + "  ".join(f"{s[f'hit@{k}']:>6.0%}" for k in KS)
                         + f"  {s['mrr']:.3f}")
    scores = summary["無依據題最高分"]
    if scores:
        lines.append("無依據題的最高分：" + "、".join(f"{s:.2f}" for s in scores))
    return "\n".join(lines)


def main():
    import tempfile

    import numpy as np

    from assistant.doc_store import DocStore
    from assistant.rerank import Reranker
    from rag.rag_resources import ManagedHFEmbedding
    from shared import paths

    embedding = ManagedHFEmbedding(os.path.join(paths.MODELS_DIR, "bge-m3"), device="cuda")
    store = DocStore(paths.LAWS_DIR, tempfile.mkdtemp(prefix="eval_store_"),
                     lambda texts: np.array(embedding.get_text_embedding_batch(list(texts))))
    store.sync()
    reranker = Reranker.from_model_dir(os.path.join(paths.MODELS_DIR, "bge-reranker-v2-m3"))
    candidates = 20

    def plain(question):
        return [(chunk_key(h.chunk), h.dense) for h in store.search(question, 5)]

    def reranked(question):
        hits = reranker.rerank(question, store.search(question, candidates), 5)
        return [(chunk_key(h.chunk), h.rerank) for h in hits]

    for title, search in (("只用混合檢索", plain), (f"混合檢索前 {candidates} 名 + 重排", reranked)):
        summary, _ = evaluate(search)
        print(format_table(title, summary), end="\n\n")


if __name__ == "__main__":
    main()

"""
rerank.py：檢索結果重排（cross-encoder）。

主要功能：
  - 向量與關鍵字檢索先撈出候選，再讓重排模型把「問題＋條文」一起讀，直接判斷相不相關，重新排序
  - 模型用 BAAI/bge-reranker-v2-m3（放在 models/bge-reranker-v2-m3），輸出轉成 0~1 的相關分數
  - 模型由資源協調器管理：用到才載入、閒置逾時或顯存不足時釋放

串接模組：
  - doc_store：取得候選 Hit
  - shared.resource_manager：ManagedResource 資源協調
  - answer（之後）：用重排後的前幾筆當作回答依據，並用相關分數判斷「找不到依據」
"""
import os
from dataclasses import replace
from typing import Callable, List, Sequence, Tuple

from assistant.doc_store import Hit
from shared.resource_manager import ManagedResource, cuda_available

IDLE_TTL = float(os.environ.get("RERANK_IDLE_TTL", "300"))
MAX_LENGTH = 512
BATCH_SIZE = 8

ScoreFn = Callable[[Sequence[Tuple[str, str]]], List[float]]


class _RerankResource(ManagedResource):
    """重排模型的載入與釋放。"""
    def __init__(self, model_dir: str, device: str):
        if device.startswith("cuda") and not cuda_available():
            print("[rerank] ⚠️ 找不到可用的 CUDA，重排模型退回 CPU。")
            device = "cpu"
        super().__init__("assistant.rerank.bge-reranker-v2-m3",
                         est_ram_mb=2600 if device == "cpu" else 1200,
                         est_vram_mb=0 if device == "cpu" else 1300, idle_ttl=IDLE_TTL)
        self._dir, self._device = model_dir, device

    def _load(self):
        import torch
        from transformers import AutoModelForSequenceClassification, AutoTokenizer

        tokenizer = AutoTokenizer.from_pretrained(self._dir)
        model = AutoModelForSequenceClassification.from_pretrained(self._dir)
        if self._device.startswith("cuda"):
            model = model.half()
        model.to(self._device).eval()
        return tokenizer, model, torch

    def _unload(self, impl):
        try:
            impl[1].to("cpu")  # 先移回 CPU，確保顯存歸還
        except Exception:  # noqa: S110
            pass

    def score(self, pairs: Sequence[Tuple[str, str]]) -> List[float]:
        scores: List[float] = []
        with self.use() as (tokenizer, model, torch):
            for start in range(0, len(pairs), BATCH_SIZE):
                batch = pairs[start:start + BATCH_SIZE]
                inputs = tokenizer([q for q, _ in batch], [d for _, d in batch], padding=True, truncation=True,
                                   max_length=MAX_LENGTH, return_tensors="pt").to(self._device)
                with torch.no_grad():
                    logits = model(**inputs).logits.view(-1).float()
                scores.extend(torch.sigmoid(logits).cpu().tolist())
        return scores


class Reranker:
    def __init__(self, score_fn: ScoreFn):
        self._score = score_fn

    @classmethod
    def from_model_dir(cls, model_dir: str, device: str = "cuda") -> "Reranker":
        """正式環境用：載入 bge-reranker 模型（用到才真的載入）。"""
        resource = _RerankResource(model_dir, device)
        reranker = cls(resource.score)
        reranker.resource = resource
        return reranker

    def release(self):
        resource = getattr(self, "resource", None)
        if resource is not None:
            resource.release("explicit")

    def rerank(self, query: str, hits: Sequence[Hit], top_k: int = 5) -> List[Hit]:
        """依相關分數由高到低回傳前 top_k 筆；每筆的 score 與 rerank 欄位都是重排分數。"""
        if not hits or not query.strip():
            return []
        scores = self._score([(query, f"{h.label}\n{h.chunk['body']}") for h in hits])
        ranked = sorted(zip(hits, scores, strict=True), key=lambda pair: -pair[1])[:top_k]
        return [replace(hit, score=float(score), rerank=float(score)) for hit, score in ranked]

"""
embedding.py：bge-m3 向量模型的資源包裝。

主要功能：
  - ManagedEmbedder：自己不持有模型，每次呼叫才向資源協調器借用，閒置或顯存不足時模型可以被釋放
  - 呼叫它把一批文字轉成正規化後的向量（numpy 陣列），可直接當 DocStore、NameResolver 的 embed_fn
  - 預設走 GPU 並轉 fp16 省顯存；沒有 CUDA 時退回 CPU；環境變數 RAG_EMBED_DEVICE、RAG_EMBED_IDLE_TTL 可調整

串接模組：
  - shared.resource_manager：ManagedResource 資源協調
  - bootstrap、evaluation.retrieval_eval：建立並使用它
"""
import os

import numpy as np

from shared.resource_manager import ManagedResource, cuda_available

EMBED_DEVICE = os.environ.get("RAG_EMBED_DEVICE", "cuda")
EMBED_IDLE_TTL = float(os.environ.get("RAG_EMBED_IDLE_TTL", "300"))
BATCH_SIZE = 10


class _EmbedResource(ManagedResource):
    """bge-m3 模型的載入與釋放。"""

    def __init__(self, model_source, device):
        if device.startswith("cuda") and not cuda_available():
            print("[embedding] ⚠️ 找不到可用的 CUDA（torch 可能是 CPU 版），bge-m3 退回 CPU。"
                  "要用 GPU 請安裝 CUDA 版 torch。")
            device = "cpu"
        super().__init__("rag.embed.bge-m3",
                         est_ram_mb=2600 if device == "cpu" else 1200,
                         est_vram_mb=0 if device == "cpu" else 1300,
                         idle_ttl=EMBED_IDLE_TTL)
        self._source, self._device = model_source, device

    def _load(self):
        from sentence_transformers import SentenceTransformer
        model = SentenceTransformer(str(self._source), device=self._device)
        if self._device.startswith("cuda"):
            try:
                model.half()  # 轉 fp16，顯存減半
            except Exception as e:
                print(f"[embedding] bge-m3 轉 fp16 失敗，維持 fp32：{e}")
        return model

    def _unload(self, impl):
        try:
            impl.to("cpu")  # 先移回 CPU，確保顯存歸還
        except Exception as e:
            print(f"[embedding] 釋放模型前移回 CPU 失敗（略過）：{type(e).__name__}")


class ManagedEmbedder:
    def __init__(self, model_source, device: str = EMBED_DEVICE):
        self._res = _EmbedResource(model_source, device)

    def __call__(self, texts) -> np.ndarray:
        """文字清單 → 正規化向量（每列一段文字）。"""
        texts = list(texts)
        with self._res.use() as model:
            vectors = model.encode(texts, batch_size=BATCH_SIZE, normalize_embeddings=True,
                                   convert_to_numpy=True, show_progress_bar=False)
        return np.asarray(vectors, dtype=np.float32)

    def release(self):
        self._res.release("explicit")

    def evict(self):
        return self._res.evict()

"""
rag_resources.py：RAG 高耗能組件（bge-m3 embedding、Ollama LLM）的資源包裝。

主要功能：
  - ManagedHFEmbedding：可放進 Settings.embed_model 的代理，每次呼叫才向協調器借用模型
  - OllamaModelResource：登記 Ollama 模型使用狀態，閒置或被驅逐時要求 Ollama 卸載
  - 啟動時不載入，用完可釋放；環境變數 RAG_EMBED_DEVICE、RAG_EMBED_IDLE_TTL、RAG_LLM_IDLE_TTL 可調整

串接模組：
  - resource_manager：ManagedResource 資源協調
  - main_guardrail_rag：初始化時建立這兩個資源
"""
import json
import os
import urllib.request

from pydantic import PrivateAttr
from llama_index.core.base.embeddings.base import BaseEmbedding

from shared.resource_manager import ManagedResource, cuda_available

# 預設 bge-m3 走 GPU 並轉 fp16 省顯存；沒有 CUDA 時自動退回 CPU
EMBED_DEVICE = os.environ.get("RAG_EMBED_DEVICE", "cuda")
EMBED_IDLE_TTL = float(os.environ.get("RAG_EMBED_IDLE_TTL", "300"))
LLM_IDLE_TTL = float(os.environ.get("RAG_LLM_IDLE_TTL", "300"))


class _EmbedResource(ManagedResource):
    """bge-m3 embedding 模型的載入與釋放。"""
    def __init__(self, model_source, device):
        if device.startswith("cuda") and not cuda_available():
            print("[rag_resources] ⚠️ 找不到可用的 CUDA（torch 可能是 CPU 版），bge-m3 退回 CPU。"
                  "要用 GPU 請安裝 CUDA 版 torch。")
            device = "cpu"
        super().__init__("rag.embed.bge-m3",
                         est_ram_mb=2600 if device == "cpu" else 1200,
                         est_vram_mb=0 if device == "cpu" else 1300,
                         idle_ttl=EMBED_IDLE_TTL)
        self._source, self._device = model_source, device

    def _load(self):
        from llama_index.embeddings.huggingface import HuggingFaceEmbedding
        emb = HuggingFaceEmbedding(model_name=self._source, device=self._device)
        if self._device.startswith("cuda"):
            try:
                emb._model.half()  # 轉 fp16，顯存減半
            except Exception as e:
                print(f"[rag_resources] bge-m3 轉 fp16 失敗，維持 fp32：{e}")
        return emb

    def _unload(self, impl):
        try:
            impl._model.to("cpu")  # 先移回 CPU，確保顯存歸還
        except Exception:
            pass


class ManagedHFEmbedding(BaseEmbedding):
    """embedding 代理：自己不持有模型，每次呼叫才向協調器借用，讓真正的模型可以被釋放。"""
    _res: _EmbedResource = PrivateAttr()

    def __init__(self, model_source: str, device: str = EMBED_DEVICE, **kwargs):
        super().__init__(model_name=str(model_source), **kwargs)
        self._res = _EmbedResource(model_source, device)

    @classmethod
    def class_name(cls) -> str:
        return "ManagedHFEmbedding"

    def release(self):
        self._res.release("explicit")

    def evict(self):
        return self._res.evict()

    def _get_query_embedding(self, query: str):
        with self._res.use() as m:
            return m._get_query_embedding(query)

    async def _aget_query_embedding(self, query: str):
        return self._get_query_embedding(query)

    def _get_text_embedding(self, text: str):
        with self._res.use() as m:
            return m._get_text_embedding(text)

    def _get_text_embeddings(self, texts):
        with self._res.use() as m:
            return m._get_text_embeddings(texts)


class OllamaModelResource(ManagedResource):
    """
    Ollama 模型的使用登記：模型由 Ollama 伺服器持有，這裡閒置逾時或被驅逐時
    用 keep_alive=0 要求它卸載。預估開銷填 0，角色是讓 bge-m3 載入時可被驅逐的一方。
    """
    # 問答期間會一直持有 LLM 控制代碼，但 Ollama 可能還沒被呼叫；允許先卸載，之後呼叫時會自動重新載入
    self_evictable = True

    def __init__(self, model, host=None):
        super().__init__(f"rag.llm.ollama:{model}", idle_ttl=LLM_IDLE_TTL)
        self._model = model
        self._host = (host or os.environ.get("OLLAMA_HOST") or "http://127.0.0.1:11434").rstrip("/")
        if not self._host.startswith("http"):
            self._host = "http://" + self._host

    def _load(self):
        return self._model  # 伺服器端第一次請求時才真正載入

    def _unload(self, impl):
        try:
            req = urllib.request.Request(
                f"{self._host}/api/generate",
                data=json.dumps({"model": self._model, "keep_alive": 0}).encode(),
                headers={"Content-Type": "application/json"}, method="POST")
            urllib.request.urlopen(req, timeout=10).read()
        except Exception as e:
            print(f"[rag_resources] 要求 Ollama 卸載 {self._model} 失敗（可能未啟動）：{e}")

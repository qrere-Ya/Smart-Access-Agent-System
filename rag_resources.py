"""
RAG 端高耗能組件的資源包裝（bge-m3 embedding、Ollama 本地 LLM），
遵守 resource-lifecycle-standard：不在啟動時載入、用完可釋放、可被中央協調器驅逐。
"""
import json
import os
import urllib.request

from pydantic import PrivateAttr
from llama_index.core.base.embeddings.base import BaseEmbedding

from resource_manager import ManagedResource, cuda_available

# 硬體：RAM 16GB / VRAM 8GB。模型一律走 GPU：bge-m3 上 GPU 後轉 fp16（≈1.1GB，fp32 要 2.3GB），
# 才放得進 qwen2:7b（≈4.6GB 含 8K KV cache）之外剩下的顯存。無 CUDA 時自動退回 CPU 並印警告。
EMBED_DEVICE = os.environ.get("RAG_EMBED_DEVICE", "cuda")
EMBED_IDLE_TTL = float(os.environ.get("RAG_EMBED_IDLE_TTL", "300"))
LLM_IDLE_TTL = float(os.environ.get("RAG_LLM_IDLE_TTL", "300"))


class _EmbedResource(ManagedResource):
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
                emb._model.half()  # fp16：顯存減半
            except Exception as e:
                print(f"[rag_resources] bge-m3 轉 fp16 失敗，維持 fp32：{e}")
        return emb

    def _unload(self, impl):
        try:
            impl._model.to("cpu")  # 若在 GPU，先降級，確保 VRAM 真的被歸還
        except Exception:
            pass


class ManagedHFEmbedding(BaseEmbedding):
    """
    可放進 Settings.embed_model 的代理：自己不持有模型權重，每次呼叫才向協調器借用。
    因為 VectorStoreIndex / retriever 建立時會保存 embed_model 的參照，只有「代理」被到處保存，
    真正的模型才能被釋放到引用計數歸零。
    """
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
    Ollama 是獨立伺服器行程，模型權重由它持有。這裡把「該模型目前是否由本行程占用」納入協調：
    使用前 use()（登記為 Executing）、閒置逾時或被驅逐時，用 Ollama 官方 API
    （keep_alive=0）要求它卸載模型，歸還記憶體/顯存。預估開銷刻意填 0：Ollama 可能已載入，
    用系統剩餘量去卡它會誤判；它的角色是「被驅逐者」，讓 bge-m3 等載入時有空間。
    """
    # 問答流程是「先用 embedding 做防護欄/檢索、再呼叫 LLM」：整段請求期間本執行緒都 use() 著 LLM
    # 控制代碼，但此時 Ollama 的模型可能根本還沒被呼叫。embedding 要載入而資源不足時，必須允許
    # 把 Ollama 模型先卸載（之後呼叫 LLM 時 Ollama 會自動重新載入），否則會因為找不到可驅逐者而失敗。
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

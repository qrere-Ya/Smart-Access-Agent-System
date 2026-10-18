"""
bootstrap.py：把助理需要的所有零件組起來（文件向量庫、重排、LLM、人名比對），給聊天視窗啟動時用。

主要功能：
  - create_runtime：載入 bge-m3、同步文件資料夾、載入重排模型、取得 LLM、讀員工名單，組成 Assistant
  - 每一步回報進度文字，讓視窗顯示「正在載入…」；零件工廠可以抽換，測試時不需要真的模型
  - refresh_documents：重新掃描文件資料夾（新增、修改、刪除的文件各自處理）
  - reload_llm：設定改過之後重新取得 LLM；close：釋放顯存上的模型

串接模組：
  - doc_store、rerank、entity_resolver、llm、queries、answer：各個零件
  - embedding：bge-m3 的資源包裝
  - rag.chat_window：呼叫 create_runtime 並顯示進度
"""
import os
from dataclasses import dataclass, field
from typing import Callable, List, Optional


from rag import answer, llm as llm_mod, queries
from rag.doc_store import DocStore, SyncReport
from rag.entity_resolver import NameResolver
from rag.rerank import Reranker
from shared import paths

Progress = Callable[[str], None]


@dataclass
class Runtime:
    assistant: answer.Assistant
    store: DocStore
    llm_note: str
    sync: SyncReport
    closers: List[Callable[[], None]] = field(default_factory=list)

    def close(self):
        """釋放模型佔用的顯存與記憶體。"""
        for closer in self.closers:
            try:
                closer()
            except Exception as e:
                print(f"[bootstrap] 釋放資源時出錯（略過）：{type(e).__name__}")
        self.closers.clear()


def _default_embedder():
    """正式環境的向量化：bge-m3（本機資料夾）。回傳 (embed_fn, 釋放函式)。"""
    from rag.embedding import ManagedEmbedder

    model_dir = os.path.join(paths.MODELS_DIR, "bge-m3")
    if not os.path.isdir(model_dir):
        raise FileNotFoundError(f"找不到向量模型資料夾：{model_dir}")
    embedder = ManagedEmbedder(model_dir, device="cuda")
    return embedder, embedder.release


def _default_reranker():
    reranker = Reranker.from_model_dir(os.path.join(paths.MODELS_DIR, "bge-reranker-v2-m3"))
    return reranker, reranker.release


def roster_names() -> List[str]:
    """目前員工名單（只有姓名）。"""
    return [p["name"] for p in queries.roster()]


def create_runtime(progress: Progress = print, docs_dir: Optional[str] = None, store_dir: Optional[str] = None,
                   settings: Optional[dict] = None, embedder=None, reranker_factory=None, llm_builder=None,
                   roster=roster_names) -> Runtime:
    """依序準備所有零件並組成 Assistant；任何一步失敗都會丟出例外，由呼叫端顯示原因。"""
    docs_dir = docs_dir or paths.DOCS_DIR
    store_dir = store_dir or paths.RAG_DIR
    closers: List[Callable[[], None]] = []

    try:
        progress("載入向量模型（bge-m3）…")
        embed, release_embed = (embedder or _default_embedder)()
        closers.append(release_embed)

        progress("掃描文件資料夾並建立索引…")
        store = DocStore(docs_dir, store_dir, embed)
        sync = store.sync()

        progress("載入重排模型…")
        reranker, release_rerank = (reranker_factory or _default_reranker)()
        closers.append(release_rerank)

        progress("連接語言模型…")
        builder = llm_builder or llm_mod.build_llm
        model, note = builder(settings)
        router_model, _ = builder(settings, max_tokens=200)
        if model is None:
            raise RuntimeError(note)

        resolver = NameResolver(roster(), embed)
        assistant = answer.Assistant(store, reranker, model, resolver, router_llm=router_model)
        assistant.roster_provider = roster
        progress("準備完成")
        return Runtime(assistant, store, note, sync, closers)
    except BaseException:  # 任何一步失敗都要把已載入的模型釋放掉，不然顯存會被占住
        for closer in closers:
            try:
                closer()
            except Exception as e:
                print(f"[bootstrap] 釋放資源時出錯（略過）：{type(e).__name__}")
        raise


def refresh_documents(runtime: Runtime) -> SyncReport:
    """重新掃描文件資料夾，回傳這次的新增、修改、刪除與失敗清單。"""
    runtime.sync = runtime.store.sync()
    return runtime.sync


def reload_llm(runtime: Runtime, settings: Optional[dict] = None, llm_builder=None) -> str:
    """設定改過後重新取得 LLM；失敗時保留原本的並回傳原因。"""
    builder = llm_builder or llm_mod.build_llm
    model, note = builder(settings)
    if model is None:
        return note
    router_model, _ = builder(settings, max_tokens=200)
    runtime.assistant.llm, runtime.assistant.router_llm = model, router_model
    runtime.llm_note = note
    return note

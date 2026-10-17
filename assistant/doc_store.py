"""
doc_store.py：文件知識庫（逐份文件追蹤的向量庫）。

主要功能：
  - 掃描文件資料夾（含子資料夾），每份文件各自記錄內容指紋：新增、修改、刪除的文件各自處理，沒變的不重算
  - 切塊後用 embed_fn 向量化，存入 FAISS（內積＝餘弦相似度）；索引、區塊、文件清單都存在 runtime/assistant/
  - 混合檢索：向量相似度加 jieba 斷詞的 BM25 關鍵字，用名次融合（RRF）排序；每筆結果附上向量相似度，
    讓回答層判斷「找不到依據」
  - 某份文件讀不出來時保留它上一次的內容並回報失敗，不會因為暫時讀不到就把知識弄丟

串接模組：
  - ingest：文件讀取與切塊
  - rag.rag_resources：正式環境的 embed_fn 來自 bge-m3 的資源包裝
  - answer（之後）：取得檢索結果交給 LLM
"""
import json
import os
from dataclasses import asdict, dataclass, field
from typing import Callable, Dict, List, Optional

import faiss
import jieba
import numpy as np

from assistant import ingest

_EMBED_BATCH = 32
_RRF_K = 60
_CANDIDATE_FACTOR = 4

EmbedFn = Callable[[List[str]], np.ndarray]


@dataclass
class SyncReport:
    """一次同步的結果；failed 的值是失敗原因。"""
    added: List[str] = field(default_factory=list)
    updated: List[str] = field(default_factory=list)
    removed: List[str] = field(default_factory=list)
    unchanged: List[str] = field(default_factory=list)
    failed: Dict[str, str] = field(default_factory=dict)

    @property
    def changed(self) -> bool:
        return bool(self.added or self.updated or self.removed)


@dataclass
class Hit:
    """一筆檢索結果：chunk 是 ingest.Chunk 的欄位字典，dense 是向量相似度（沒被向量找到時為 None）。"""
    chunk: dict
    score: float
    dense: Optional[float]
    rerank: Optional[float] = None

    @property
    def label(self) -> str:
        c = self.chunk
        return " ".join(p for p in (c["title"], c["section"], c["article"]) if p)


def _normalize(vectors: np.ndarray) -> np.ndarray:
    vectors = np.asarray(vectors, dtype=np.float32)
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return vectors / norms


def _tokens(text: str) -> List[str]:
    return [t for t in jieba.lcut(text.lower()) if t.strip()]


class DocStore:
    def __init__(self, docs_dir: str, store_dir: str, embed_fn: EmbedFn, dim: int = 1024,
                 max_chars: int = ingest.DEFAULT_MAX_CHARS):
        self.docs_dir, self.store_dir, self.embed_fn = docs_dir, store_dir, embed_fn
        self.dim, self.max_chars = dim, max_chars
        self._manifest: Dict[str, dict] = {}
        self._chunks: Dict[int, dict] = {}
        self._next_id = 1
        self._index = faiss.IndexIDMap2(faiss.IndexFlatIP(dim))
        self._bm25 = None
        self._bm25_ids: List[int] = []
        self._load()

    # --- 存取 ---

    def _path(self, name):
        return os.path.join(self.store_dir, name)

    def _load(self):
        try:
            with open(self._path("manifest.json"), encoding="utf-8") as fh:
                meta = json.load(fh)
            with open(self._path("chunks.json"), encoding="utf-8") as fh:
                chunks = json.load(fh)
            index = faiss.read_index(self._path("index.faiss"))
            if index.d != self.dim:
                return
        except (OSError, ValueError, RuntimeError):
            return
        self._manifest = meta["files"]
        self._next_id = meta["next_id"]
        self._chunks = {int(k): v for k, v in chunks.items()}
        self._index = index
        self._rebuild_bm25()

    def _save(self):
        os.makedirs(self.store_dir, exist_ok=True)
        for name, writer in (
            ("manifest.json", lambda fh: json.dump({"files": self._manifest, "next_id": self._next_id}, fh,
                                                   ensure_ascii=False)),
            ("chunks.json", lambda fh: json.dump({str(k): v for k, v in self._chunks.items()}, fh,
                                                 ensure_ascii=False)),
        ):
            tmp = self._path(name + ".tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                writer(fh)
            os.replace(tmp, self._path(name))
        tmp = self._path("index.faiss.tmp")
        faiss.write_index(self._index, tmp)
        os.replace(tmp, self._path("index.faiss"))

    def _rebuild_bm25(self):
        import bm25s

        self._bm25_ids = sorted(self._chunks)
        if not self._bm25_ids:
            self._bm25 = None
            return
        corpus = [_tokens(self._chunks[i]["label"] + " " + self._chunks[i]["body"]) for i in self._bm25_ids]
        self._bm25 = bm25s.BM25()
        self._bm25.index(corpus, show_progress=False)

    # --- 同步 ---

    def _discover(self) -> Dict[str, str]:
        found = {}
        if not os.path.isdir(self.docs_dir):
            return found
        for root, _dirs, files in os.walk(self.docs_dir):
            for fname in files:
                if fname.startswith(("~$", ".")) or not fname.lower().endswith(ingest.SUPPORTED_EXTENSIONS):
                    continue
                full = os.path.join(root, fname)
                found[os.path.relpath(full, self.docs_dir).replace(os.sep, "/")] = full
        return found

    def _drop_file(self, name):
        ids = self._manifest.pop(name, {}).get("chunk_ids", [])
        if ids:
            self._index.remove_ids(np.array(ids, dtype=np.int64))
        for i in ids:
            self._chunks.pop(i, None)

    def sync(self) -> SyncReport:
        """讓向量庫和文件資料夾一致，只處理有變動的文件。"""
        report = SyncReport()
        found = self._discover()
        for name in sorted(set(self._manifest) - set(found)):
            self._drop_file(name)
            report.removed.append(name)
        for name, full in sorted(found.items()):
            try:
                fingerprint = ingest.file_fingerprint(full)
                known = self._manifest.get(name)
                if known and known["fingerprint"] == fingerprint:
                    report.unchanged.append(name)
                    continue
                chunks = ingest.chunk_document(name, ingest.read_file(full), self.max_chars)
                if not chunks:
                    raise ingest.DocumentReadError("切不出任何內容")
                vectors = self._embed([c.embed_text for c in chunks])
            except (ingest.DocumentReadError, OSError) as e:
                report.failed[name] = str(e)
                continue
            if known:
                self._drop_file(name)
            ids = list(range(self._next_id, self._next_id + len(chunks)))
            self._next_id += len(chunks)
            self._index.add_with_ids(vectors, np.array(ids, dtype=np.int64))
            for i, c in zip(ids, chunks, strict=True):
                self._chunks[i] = asdict(c) | {"label": c.label}
            self._manifest[name] = {"fingerprint": fingerprint, "chunk_ids": ids}
            (report.updated if known else report.added).append(name)
        if report.changed:
            self._rebuild_bm25()
            self._save()
        return report

    def _embed(self, texts: List[str]) -> np.ndarray:
        parts = [np.asarray(self.embed_fn(texts[i:i + _EMBED_BATCH]), dtype=np.float32)
                 for i in range(0, len(texts), _EMBED_BATCH)]
        return _normalize(np.vstack(parts))

    # --- 檢索 ---

    @property
    def documents(self) -> List[str]:
        return sorted(self._manifest)

    def titles(self) -> List[str]:
        """已收錄文件的標題清單。"""
        return sorted({chunk["title"] for chunk in self._chunks.values()})

    def __len__(self):
        return len(self._chunks)

    def search(self, query: str, k: int = 5) -> List[Hit]:
        """混合檢索，回傳最多 k 筆，依融合分數由高到低。"""
        if not self._chunks or not query.strip():
            return []
        pool = min(len(self._chunks), max(k * _CANDIDATE_FACTOR, k))
        qvec = self._embed([query])
        sims, ids = self._index.search(qvec, pool)
        dense = {int(i): float(s) for s, i in zip(sims[0], ids[0], strict=True) if i != -1}
        fused: Dict[int, float] = {}
        for rank, chunk_id in enumerate(dense, start=1):
            fused[chunk_id] = fused.get(chunk_id, 0.0) + 1.0 / (_RRF_K + rank)
        if self._bm25 is not None:
            query_tokens = [t for t in _tokens(query) if t in self._bm25.vocab_dict]
            if query_tokens:
                positions, scores = self._bm25.retrieve([query_tokens], k=pool, show_progress=False)
                kw_rank = 0
                for pos, score in zip(positions[0], scores[0], strict=True):
                    if score <= 0:
                        continue
                    kw_rank += 1
                    chunk_id = self._bm25_ids[int(pos)]
                    fused[chunk_id] = fused.get(chunk_id, 0.0) + 1.0 / (_RRF_K + kw_rank)
        top = sorted(fused, key=lambda i: -fused[i])[:k]
        for i in top:  # 只被關鍵字找到的，補算向量相似度
            if i not in dense:
                dense[i] = float(np.dot(qvec[0], self._index.reconstruct(i)))
        return [Hit(self._chunks[i], fused[i], dense[i]) for i in top]

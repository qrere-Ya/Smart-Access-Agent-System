"""
rag_index.py：法規向量索引（FAISS）的建置與快取。

主要功能：
  - 讀取 data/laws/ 底下的法規文件，切塊、嵌入並存成 FAISS 索引（runtime/faiss_storage/）
  - 用法規資料夾的檔名、大小、修改時間算出指紋，文件有變動就自動重建索引

串接模組：
  - main_guardrail_rag：初始化時呼叫 load_or_build_index
"""

import os
import hashlib
import shutil

import faiss
from llama_index.core import (
    SimpleDirectoryReader,
    VectorStoreIndex,
    StorageContext,
    load_index_from_storage,
)
from llama_index.vector_stores.faiss import FaissVectorStore

from shared import paths


def _compute_law_dir_fingerprint(law_dir: str) -> str:
    """對法規資料夾所有檔案的（相對路徑、大小、修改時間）算 SHA256 指紋，用來偵測文件是否變動。"""
    if not os.path.exists(law_dir):
        return "no_law_dir"
    entries = []
    for root, _dirs, files in os.walk(law_dir):
        for fname in files:
            fpath = os.path.join(root, fname)
            try:
                stat = os.stat(fpath)
                entries.append(f"{os.path.relpath(fpath, law_dir)}|{stat.st_size}|{int(stat.st_mtime)}")
            except OSError:
                continue
    entries.sort()
    return hashlib.sha256("\n".join(entries).encode("utf-8")).hexdigest()


def load_or_build_index():
    """
    載入或建立法規向量索引。
    現在的指紋與上次建索引時存的不同（或沒有索引）就重建，相同就直接載入。
    考勤資料不放進向量索引，改由 rag_sql_engine 即時查資料庫。
    注意：只改切塊程式邏輯、法規檔沒變時指紋不會變，需要手動刪除 runtime/faiss_storage/ 才會重建。
    """
    STORAGE_DIR = paths.FAISS_DIR
    FINGERPRINT_FILE = os.path.join(STORAGE_DIR, "_law_dir_fingerprint.txt")
    EMBEDDING_DIM = 1024
    law_dir = paths.LAWS_DIR

    current_fingerprint = _compute_law_dir_fingerprint(law_dir)
    stored_fingerprint = None
    if os.path.exists(FINGERPRINT_FILE):
        try:
            with open(FINGERPRINT_FILE, "r", encoding="utf-8") as f:
                stored_fingerprint = f.read().strip()
        except OSError as e:
            print(f"⚠️ [資料庫] 讀取法規指紋檔失敗，保守起見當作有變動、重新建立索引：{e}")

    needs_rebuild = (not os.path.exists(STORAGE_DIR)) or (stored_fingerprint != current_fingerprint)

    if needs_rebuild:
        if os.path.exists(STORAGE_DIR):
            print("📂 [資料庫] 偵測到 data/laws/ 裡的法規文件有變動，重新建立 FAISS 法規知識底座...")
            shutil.rmtree(STORAGE_DIR)
        else:
            print("📂 [資料庫] 偵測到無歷史索引，開始建立 FAISS 法規知識底座...")

        documents = []
        if os.path.exists(law_dir):
            law_docs = SimpleDirectoryReader(law_dir).load_data()
            for doc in law_docs:
                doc.metadata["category"] = "law"
                documents.append(doc)

        faiss_index = faiss.IndexFlatL2(EMBEDDING_DIM)
        vector_store = FaissVectorStore(faiss_index=faiss_index)
        storage_context = StorageContext.from_defaults(vector_store=vector_store)
        index = VectorStoreIndex.from_documents(documents, storage_context=storage_context)
        index.storage_context.persist(persist_dir=STORAGE_DIR)
        try:
            with open(FINGERPRINT_FILE, "w", encoding="utf-8") as f:
                f.write(current_fingerprint)
        except OSError as e:
            print(f"⚠️ [資料庫] 法規指紋檔寫入失敗，下次啟動可能會被誤判為又有變動而重建：{e}")
    else:
        vector_store = FaissVectorStore.from_persist_dir(STORAGE_DIR)
        storage_context = StorageContext.from_defaults(vector_store=vector_store, persist_dir=STORAGE_DIR)
        index = load_index_from_storage(storage_context=storage_context)
    return index

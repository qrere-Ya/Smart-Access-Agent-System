"""
法規向量索引（FAISS）的建置與快取。

【2026-09-02，架構重構拆出】原本這些邏輯放在 main_guardrail_rag.py 裡，
跟 SQL 引擎、防護欄路由、對話引擎混在同一個 700 多行的檔案裡。這裡只負責
一件事：把 data/laws/ 底下的法規文件，變成一份可以查詢的向量索引，並且
自動判斷法規文件有沒有變動過、需不需要重建索引。
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


def _compute_law_dir_fingerprint(law_dir: str) -> str:
    """
    【讓 faiss_storage 不再是「寫死」的，可以自動偵測法規文件有沒有變】
    對 data/laws/ 資料夾底下所有檔案算一個「指紋」：把每個檔案的（相對檔名、大小、
    最後修改時間）收集起來、排序後算一次 SHA256。只要資料夾裡的檔案有新增、刪除，
    或是內容被改過（改過內容，大小或修改時間幾乎一定會跟著變），這個指紋就會跟著變。
    之後拿現在算出來的指紋，跟「上次建立索引當下」存的指紋比對，就知道法規文件是不是
    有變動過、要不要重新建立索引，不用每次都靠人手動砍掉 faiss_storage 資料夾。
    """
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
    建立/載入「法規」向量索引。

    【重要變更】考勤資料不再讀取 data/attendance.csv 塞進向量索引了。
    原因：門禁系統的打卡資料是即時、持續在變的，若跟法規一起做成一次性的靜態向量索引，
    資料庫每多一筆新打卡紀錄，就必須整個刪掉 faiss_storage 重建，既慢又容易漏資料。
    考勤類問題現在改成用 rag_sql_engine.build_sql_engine() 建立的即時 SQL 查詢引擎直接
    查詢 database/database.db，永遠拿到最新資料。詳見 rag_guardrail.check_query_route()。

    【2026-09-02：不用再手動刪 faiss_storage 了】以前這裡只看 faiss_storage 資料夾
    「存不存在」來決定要不要重建索引——換句話說，data/laws/ 裡的法規文件不管後續怎麼
    增刪改，只要 faiss_storage 這個資料夾還在，系統永遠只會用「第一次建立索引當下」的
    舊內容回答問題，完全是寫死、不會自動更新的，每次要讓新文件生效都要手動刪資料夾才行。

    現在改成額外算一份 data/laws/ 資料夾的「指紋」（見 _compute_law_dir_fingerprint()），
    存在 faiss_storage 資料夾裡；每次啟動都拿「現在的指紋」跟「上次建立索引時存的
    指紋」比對：
      - 兩者不一樣（代表 data/laws/ 裡的文件有新增、刪除、或內容被改過），就自動整個
        重新建立索引，不用再手動處理。
      - 兩者一樣，才會走原本「直接載入已經建好的索引」這條快速路徑，不會每次啟動都
        浪費時間重新嵌入沒有變過的文件。
    這是折衷做法，不是「每次啟動都即時重新查詢」——法規文件通常不會每分每秒在變，
    真的要做到那種「即時」，要嘛犧牲掉每次啟動的速度、要嘛要改用更複雜的增量索引
    機制（只針對真的變動的檔案重新嵌入，不用整個重建）。以目前只有 3 份法規文件的
    規模來說，整個重建也只需要幾秒鐘，用「偵測到變動才重建」這個折衷方式已經很夠用；
    如果之後法規文件數量變得很多、重建開始感覺到明顯變慢，才需要再考慮做成增量索引。
    """
    STORAGE_DIR = "./faiss_storage"
    FINGERPRINT_FILE = os.path.join(STORAGE_DIR, "_law_dir_fingerprint.txt")
    EMBEDDING_DIM = 1024
    law_dir = "data/laws/"

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

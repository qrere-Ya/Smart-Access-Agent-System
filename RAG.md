# Smart Access Agent System (智慧門禁與勞資代理人) 技術分析報告

本文件旨在詳細記錄 `Edge_LlamaIndex_RAG` 專案的技術實現，作為技術面試準備之用。

## 1. 專案整體架構與資料流

### 系統定位
本系統是一個基於邊緣運算 (Edge AI) 的 RAG 框架，專門處理企業內部人事、考勤紀錄與勞基法合規性檢查。其核心目標是在確保數據隱私（本地運行）的前提下，提供高可信度、無幻覺的法律與人事解答。

### 資料流 (Data Flow)
1. **輸入階段**: 使用者透過 Gradio 介面輸入問題，並選擇路由模式 (`Basic` 或 `RAG`)。
2. **防護欄攔截 (Guardrail)**: 
   - 若選擇 `RAG` 模式，輸入將首先進入 `check_semantic_guardrail`。
   - 系統將查詢語義映射至高維空間，與「正向定義域（人事法規）」與「負向定義域（通用聊天）」對比。
   - 若被判定為非法請求（$\text{Sim}_{neg} > \text{Sim}_{pos}$ 或 $\text{Sim}_{pos} < 0.3$），直接攔截並返回拒絕訊息。
3. **檢索階段 (Retrieval**:
   - 系統透過 `FAISS` 向量資料庫進行語義檢索。
   - 使用 `similarity_top_k=5` 提取最相關的 5 個文本塊 (Nodes)。
   - 應用 `LongContextReorder` 後處理，將最相關的內容重新排列，解決 LLM 的「中間遺忘 (Lost-in-the-middle)」問題。
4. **生成階段 Generation**:
   - 將檢索到的上下文 $\text{Context}$ 與精心設計的 `system_prompt`（包含絕對忠誠與閉嘴原則）組合。
   - 由本地 `Ollama (Qwen2-7B)` 生成最終回答。
5. **輸出階段**: 透過 Gradio 實現串流 (Streaming) 輸出，提升使用者體驗。

---

## 2. 核心模組功能與關鍵邏輯

### `main_guardrail_rag.py` (系統核心)
- **`setup_environment()`**: 初始化全域 LLM (`Qwen2-7B`) 與 Embedding (`BGE-M3`) 設定，並定義 `SentenceSplitter` (chunk_size=300, overlap=30)。
- **`load_or_build_index()`**: 只建立**法規**向量索引 (`data/laws/` 下的 PDF/TXT 文件)，使用 `FaissVectorStore` 並持久化至 `./faiss_storage`。考勤資料不再進向量索引。
- **`build_sql_engine()`**: 建立**即時**查詢引擎，直接對同一個合併專案下 `database/database.db` 的 `Attendance` 表下 SQL 查詢，資料一有更新立刻查得到，不用重建索引。基於安全考量，唯讀連線且只開放 `Attendance` 表（`Users` 表含人臉特徵向量，刻意不開放給 LLM 查詢）。
- **`check_semantic_guardrail()`**: 實現「對比式語義防護欄」。利用餘弦相似度判定查詢意圖是否落在預定義的專業領域內。
- **`check_query_route()`**: 通過防護欄後的二次分類，用同樣的語意相似度比對法，判斷問題該查「即時考勤資料庫」還是「法規向量索引」。
- **`build_chat_engine()`**: 配置 `condense_plus_context` 模式的聊天引擎，整合記憶體 (`ChatMemoryBuffer`) 與上下文重排，僅用於法規類問題。

### `evaluate_rag.py` (自動化評估)
- **`run_dynamic_evaluation()`**: 實現 **LLM-as-a-Judge** 雙盲評估迴圈。
  - **合成 (Generator)**: 隨機抽樣知識庫 $\rightarrow$ 出題官 (API LLM，透過 `get_judge_llm()`) 生成問題 + 標準答案 $\text{(Ground Truth)}$。
  - **答題 (Agent)**: RAG 系統 (本地 Ollama Qwen2-7B) 嘗試回答該問題。
  - **評判 (Judge)**: 出題官與裁判官共用同一個獨立於考生的 API LLM 實例，根據「準確性、完整性、無幻覺」判定 $[合格]$ 或 $[不合格]$，並額外標註不合格分類（矛盾／遺漏關鍵資訊／幻覺瞎掰）。
  - **檢索命中率**: 比對出題用的知識塊 ID 是否有出現在考生實際檢索到的節點中，量化向量資料庫的檢索準確度。
  - **總結報告**: 全部題目跑完後輸出答題合格率、檢索命中率、幻覺率，並依門檻（預設合格率 ≥ 80% 且 幻覺率 ≤ 10%）給出最終「合格／不合格」判定。

### `app.py` (介面層)
- 使用 `Gradio` 建立 Web UI，支持 `Basic` (直接對話) 與 `RAG` (知識庫增強) 兩種模式切換。

---

## 3. 演算法、模型與實際參數設定

### 模型設定
| 組件 | 使用模型 | 參數/設定 | 來源/備註 |
| :--- | :--- | :--- | :--- |
| **LLM** | `Qwen2-7B` | `temperature=0.0` | 透過 Ollama 運行，確保輸出確定性 |
| **Embedding** | `BGE-M3` | `dim=1024` | HuggingFace `BAAI/bge-m3` |
| **Vector Store** | `FAISS` | `IndexFlatL2` | 歐幾里得距離索引 |

### 關鍵參數
- **文本切分**: `chunk_size=300`, `chunk_overlap=30`
- **檢索數量**: `similarity_top_k=5`
- **記憶體限制**: `token_limit=3000` (ChatMemoryBuffer)
- **防護欄閾值**: $\tau = 0.3$ (正向相似度低於此值即攔截)
- **LLM 超時**: `request_timeout=600.0` 秒
- **評估樣本量**: `num_questions=10` (預設每輪評估題目數)

---

## 4. 關鍵技術決策與原因

### 為什麼選擇 `BGE-M3`？
- **原因**: 勞基法與人事文件包含大量專業中文術語。BGE-M3 支持多語言、多粒度 (Multi-granularity) 檢索，且在中文語義匹配上表現極其穩定。

### 為什麼實作 `Contrastive Semantic Guardrail`？
- **原因**: 傳統關鍵字過濾太僵硬，而直接依賴 LLM 判定意圖會增加延遲且容易被 Prompt Injection 繞過。將意圖映射到向量空間並與「正/負定義域」對比，能以極低成本快速篩選出非法請求。

### 為什麼使用 `LongContextReorder`？
- **原因**: 研究發現 LLM 在處理長上下文時，容易忽略位於中間部分的資訊 (Lost-in-the-Middle)。將最相關的 Node 放在首尾兩端能顯著提升回答準確率。

### 為什麼採用 `LLM-as-a-Judge`？
- **原因**: 法律文件的評估需要高度專業性且標記數據成本高。透過「抽樣 $\rightarrow$ 合成 $\rightarrow$ 回答 $\rightarrow$ 評判」的閉環，可以實現自動化的持續基準測試 (Benchmarking)。

---

## 5. 目前限制與 TODO

### 已知限制
- **硬體依賴**: 雖然是 Edge AI，但 7B 模型仍需要一定的 GPU 顯存才能達到流暢的串流速度。
- **法規索引更新**: 法規向量索引仍為靜態持久化，若 `data/laws/` 內容更新，需手動刪除 `faiss_storage` 重新建立（考勤資料已改為即時查詢，不受此限制）。
- **問題分類為簡單二分類**: `check_query_route()` 目前只分「純考勤」「純法規」兩類，像「遲到扣薪」這種需要同時查考勤資料庫又查法規的混合型問題，會被歸類到比較相關的那一類，尚未做真正的混合查詢。
- **考勤時間窗未套用個人化排班**: 門禁系統的 `database_mgr.log_attendance()` 目前用寫死的時間窗判斷遲到/早退，未套用 `Users` 表裡每位員工自己的 `work_start`/`work_end`，若要讓 RAG 回答精確扣薪金額，建議一併修正門禁系統該處邏輯。

### 已完成 (原 TODO)
- [x] **權限管理**: 已整合門禁系統即時資料庫（`build_sql_engine()`），考勤類問題不再依賴靜態 CSV 文件。
- [x] **動態資料同步**: 考勤資料改為即時 SQL 查詢，資料庫更新後立即可查，不需增量索引更新機制。

### 待完成部分 (TODO)
- [ ] **多模型對比**: 在 `evaluate_rag.py` 中引入多個不同規模的 LLM 作為裁判官，以消除單一評判員的偏見（目前出題官與裁判官已改為共用同一個獨立 API LLM，但兩者之間仍未互相獨立）。
- [ ] **混合型問題的真正聯合查詢**: 讓「遲到扣薪」這類問題能同時組合考勤資料庫與法規向量索引的結果。

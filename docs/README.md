# 系統架構與環境

本文件說明 Smart Access Agent System 兩個子系統的實際模組拆分方式，以及執行這個專案需要的軟體環境。內容對照根目錄實際程式碼與 `requirements.txt` 撰寫，專案簡介、安裝步驟、啟動方式請見根目錄 `README.md` 與 `快速部署.md`。

## 系統架構

### 門禁子系統

```
攝影機畫面
  → vision_core.py（RetinaFace 人臉偵測對齊、ArcFace R100 特徵萃取、MiniFASNetV2 活體偵測）
  → database_mgr.py（比對員工特徵、寫入打卡紀錄）
  → security.py（YOLOv8s 偵測放行瞬間是否人數 > 1，觸發尾隨警報）
  → 語音播報（voice_agent.py / audio_player.py）、terminal_app.py（現場即時人臉辨識主程式）
```

`database_mgr.py` 管理四張資料表：

- `Users`：員工特徵向量，另有 `role`（角色，預設 `employee`）與 `is_active`（是否啟用，預設 1）兩個欄位。既有資料庫升級時用 `PRAGMA table_info` 檢查欄位是否存在，不存在才 `ALTER TABLE` 補上，新舊資料庫都能正確升級。
- `Attendance`：打卡紀錄。
- `calendar`：行事曆/排班相關資料。
- `llm_usage_log`：RAG 子系統的 LLM 呼叫用量紀錄。

管理後台 `backend_main.py` 原本是一支同時處理畫面、背景行程啟停、資料庫統計查詢的 1185 行檔案，現已拆成職責分離的模組：

- `backend_main.py`：只保留畫面（Tkinter/ttkbootstrap 視窗、卡片式主選單），把使用者操作轉發給下列模組。
- `service_manager.py`：啟動/停止/監控三組背景子行程（Ollama、LiteLLM Proxy、RAG 助理網頁），不依賴 Tkinter，可獨立測試。
- `console_panel.py`：內建終端機顯示元件。
- `dashboard_stats.py`：戰情儀表板統計數字查詢。
- `account_admin.py`：帳號管理（列出、停用/啟用、刪除員工）。

`web_server.py`（Flask）提供手機 QR Code 遠端註冊頁面（`templates/register.html`）。

### RAG 法務助理子系統

`main_guardrail_rag.py` 為統籌層，實際邏輯拆分為四個模組：

- `rag_index.py`：以 FAISS 對 `data/laws/` 底下法規文件建立向量索引，持久化至 `faiss_storage/`。
- `rag_sql_engine.py`：對 `database/database.db` 的 `Attendance` 表做唯讀即時 SQL 查詢。
- `rag_guardrail.py`：語意防護欄，判斷問題屬於考勤查詢還是法規查詢，過濾無關問題。
- `rag_chat_engine.py`：帶對話記憶的聊天引擎，處理法規類問題。

`app.py` 是 Gradio 網頁進入點；`evaluate_rag.py` 提供 LLM-as-a-Judge 自動化評估；`ablation_config.py` / `run_ablation_suite.py` / `run_single_ablation_eval.py` 提供消融實驗框架。

### 研究子專案

`research/antispoof_training/` 是獨立於正式系統之外的活體偵測模型訓練子專案，跟正式系統的程式碼完全分開，目前尚未接入 `vision_core.py`。訓練結果與三方模型對照見該目錄自己的 `README.md`。

## 系統環境

### 作業系統與 Python 版本

- 開發與測試環境為 Windows；`vision_core.py` 目前依賴 Windows 內建字型 `msjh.ttc`（微軟正黑體）做中文渲染，跨平台需自行更換字體路徑。
- 主環境固定 Python 3.10.x：門禁系統的視覺辨識相關套件（`tensorflow`、`onnxruntime`、`ultralytics` 等）是針對 3.10 測試的。
- LiteLLM Proxy 需要獨立的 Python 3.11 以上環境：`litellm` 1.85.0 以後的版本在程式碼裡使用 `NotRequired`（PEP 655，Python 3.11 才加入標準函式庫），在 3.10 環境下會直接 `ImportError`，跟套件版本號無關，只能用 3.11+ 直譯器建立獨立虛擬環境執行。

### 三個虛擬環境

| 虛擬環境 | Python 版本 | 用途 |
| --- | --- | --- |
| `venv/` | 3.10.x | 主環境：門禁子系統、RAG 子系統 |
| `venv_litellm/` | 3.11+ | 僅執行 `litellm --config config.yaml`（LiteLLM Proxy） |
| `research/antispoof_training/venv_antispoof/` | 依訓練需求 | 活體偵測模型訓練（PyTorch 等） |

### 主要依賴套件（依 `requirements.txt` 分類）

- **RAG／LLM 編排**：`llama-index-core`、`llama-index-workflows`、`llama-index-llms-ollama`、`llama-index-embeddings-huggingface`、`llama-index-vector-stores-faiss`、`faiss-cpu`、`openai`（作為 OpenAI 相容 client）、`gradio`、`SQLAlchemy`。刻意不裝 `llama-index` 總包，只裝程式碼實際用到的子套件，避免版本鎖定衝突。
- **科學計算／深度學習基礎**：`numpy`、`torch`、`torchvision`、`scipy`、`scikit-learn`、`scikit-image`（版本取門禁與 RAG 兩邊原始需求中較新者）。
- **門禁系統：視覺辨識**：`opencv-python`、`onnx`、`onnxruntime`（有 NVIDIA GPU 可換成 `onnxruntime-gpu`，兩者互斥）、`ultralytics`（YOLOv8）、`tensorflow`／`tf_keras`／`keras`。`retinaface` 刻意不列在 `requirements.txt` 內，因為它在 PyPI 上宣告依賴已停止維護的 `tensorflow==2.5.0`，必須另外用 `pip install retinaface --no-deps` 安裝，避免覆蓋掉專案需要的 `tensorflow` 版本。
- **門禁系統：語音**：`sherpa-onnx`、`sounddevice`（TTS 語音模型另外放在 `models/vits-piper-zh_CN-huayan-medium/`，不進版控）。
- **門禁系統：Web／資料庫／介面**：`Flask`、`ttkbootstrap`、`pyqrcode`、`Pillow`。

### 外部服務與連接埠

| 服務 | 連接埠 | 說明 |
| --- | --- | --- |
| Ollama | `11434` | 本地 LLM 引擎（官方預設），RAG 對話與評估的本地執行端 |
| LiteLLM Proxy | `4000` | 路由層，`config.yaml` 定義的 `claude-*`／`gpt-4o` 別名實際指向 NVIDIA NIM 上的模型 |
| RAG 助理網頁（`app.py`） | `7860` | Gradio 介面 |
| QR 註冊伺服器（`web_server.py`） | `5000` | Flask，手機掃碼註冊用 |

啟動前 `service_manager.py` 會先偵測對應連接埠是否已被占用，避免重複啟動同一服務。

### 外部帳號與金鑰

`config.yaml` 的 LiteLLM 路由設定透過環境變數 `NVIDIA_API_KEY` 取得金鑰（不寫死在檔案裡），實際呼叫的是 NVIDIA NIM 上的模型。

### 大型檔案（不進版控）

`models/` 資料夾內的大型模型檔案（ArcFace、YOLOv8、MiniFASNetV2、TTS 語音模型，約 490MB）、`data/`（法規 PDF 等）、`anomaly_logs/`（尾隨偵測畫面）均不隨版本控制提供，需另外取得或由系統執行時自動產生，詳見根目錄 `README.md`「安裝」章節。

# CLAUDE.md

Smart Access Agent System：門禁子系統（人臉辨識、活體偵測、尾隨偵測）＋ 企業知識庫與出勤查詢（原「管理者助理」，只有 RAG 功能：查公司文件、考勤與異常事件的聊天視窗）。專案文件（README 等）已移除，待開發到一定階段再重新撰寫。

## 1. 專案要點

- 專案結構：
  - `shared/`：資料庫（`database_mgr`）、資源管理器（`resource_manager`）、統一路徑（`paths`）、後台登入的工作階段與失敗鎖定（`auth`，後台與助理視窗共用）、每日日報的存檔與設定（`digest_store`）
  - `access/`：門禁，包含人臉辨識與活體（`vision_core`）、尾隨偵測（`security`）、語音（`audio_player`）、QR 註冊伺服器（`web_server`）、現場主程式（`terminal_app`）
  - `admin/`：Tkinter 管理後台（`backend_main`）與背景服務管理（`service_manager`）；後台啟動先人臉登入（`login_window` 畫面、`face_login` 逐幀判斷），管理者在 `account_admin` 設定，最後一位管理者不能被取消、停用或刪除；每日報告由 `daily_digest`（傍晚日報：明細、語言模型摘要與核對、排程判斷）、`morning_brief`（早上晨報：只有科技新聞，不含出勤；連不上網時自動重抓，1、2、5 分鐘後每 10 分鐘、最多約 5 小時）、`news_feed`（抓繁中與國際科技媒體、AI 官方部落格 RSS 與 Hacker News，每個來源最新的優先、連線問題先原地重試，整理成 類別、發生什麼事、為什麼重要、可以怎麼用、來源與原文）、`digest_window`（背景產生與只看的日報／晨報視窗，後台底部「📰 日報／晨報」按鈕）與 `schedule_window`（主選單第 6 個模組「日報與晨報」：排程時間、立即產生）負責，到設定時間（日報預設 18:00、晨報預設 08:30，後台開啟時才會跑，晚開會補；出勤資料只在日報，晨報不放）自動產生並彈窗，摘要只在雲端模式使用
  - `rag/`：企業知識庫與出勤查詢（只有 RAG，不做排程；每日日報排程將放在 `admin/`），只給後台管理者使用。文件讀取與切塊 `ingest`、向量庫 `doc_store`（FAISS + BM25）、向量模型 `embedding`（bge-m3）、重排 `rerank`、意圖判斷 `router`、日期解析 `dates`、人名比對 `entity_resolver`、唯讀固定查詢 `queries`（LLM 不寫 SQL）、問答流程 `answer`、語言模型設定與呼叫 `llm_settings`／`llm`、組裝 `bootstrap`、對話控制 `chat_session`、題庫與評估 `evaluation/`（檢索：`python -m rag.evaluation.retrieval_eval`；意圖判斷選工具：`python -m rag.evaluation.tool_eval --split dev`，題庫分開發集與封存測試集，測試集要加 `--confirm-test` 且不能拿來調整，需要真的語言模型；`mine_questions` 把組員真實問過的題目整理成待標註候選題）。聊天視窗是 `rag/chat_window`（開窗時顯示最新日報與待處理異常；CustomTkinter，`python -m rag.chat_window`，後台按「企業知識庫與出勤查詢」另開行程）。遠端測試入口給組員用手機或電腦連線：`remote_server`（Flask 網頁，`python -m rag.remote_server`，金鑰用 `--add-key 名稱` 建立）、`remote_access`（金鑰庫、排隊閘門、提問紀錄、登入失敗預算與安全事件紀錄）、`remote_web/`（網頁）、`demo_data`（示範假資料，`python -m rag.demo_data add／remove`，只在明確要時執行）
  - 活體偵測模型的訓練已拆成獨立 repo：本機與本專案並列的 `Antispoof-Training` 資料夾（尚未推送到 GitHub）。正式系統只使用它匯出的 ONNX 檔，放在 `models/`，不直接依賴那邊的程式
- 啟動方式：一律在專案根目錄用 `python -m` 啟動，例如 `python -m admin.backend_main`、`python -m access.terminal_app`、`python -m rag.chat_window`、`python -m access.web_server`，不要直接執行檔案路徑。
- 路徑一律取自 `shared/paths.py`，不要在各檔自行用 `__file__` 推算。助理讀取的文件放 `data/docs/`（PDF、Word、txt、md）。執行期資料放 `runtime/`（資料庫、助理的向量庫、證據照片、LLM 設定）。
- 環境：Windows、Python 3.11（`.venv/`），依賴見 `requirements/main.txt`，完整版本鎖定在 `requirements/lock.txt`（安裝順序見 main.txt 檔頭；retinaface 要另外用 `--no-deps` 安裝）。
- 測試：`python -m pytest tests`（單元測試，不需要模型、GPU、攝影機或任何服務；用臨時的 `runtime/`，不會碰真實資料）。CI 使用較輕量的 `requirements/test.txt`。另有只在本機跑的整合測試 `python -m pytest -m integration`（真模型、真法規、真服務）與回歸測試 `python -m pytest -m regression`（對照 `tests/regression/baseline.json`；有意更新模型後用 `UPDATE_BASELINE=1` 重產）；兩者預設不執行。
- 金鑰一律走環境變數（如 `NVIDIA_API_KEY`），不寫進檔案。`models/`、`data/`、`runtime/` 不進版控。
- 資安現況：整個後台啟動要先用人臉登入（只比對角色為 admin 的帳號，連續 5 幀單幀距離 < 1.0 且平均距離 < 0.9，活體分數 ≥ 0.90，先確認是正面臉才比對；失敗 5 次鎖 60 秒；閒置 15 分鐘登出；助理視窗只接受後台給的有效代碼）。限制：活體模型對照片與螢幕翻拍的防禦不夠強（EER 約 8% 到 14%），且擋不住能直接讀寫檔案與資料庫的人；門禁終端機、QR 註冊伺服器本身沒有登入。遠端測試入口只靠每人一組的存取金鑰（預設 7 天到期，外洩即可查詢出勤與名單），啟動時不經人臉登入，用 Flask 內建伺服器，只適合少數人。測試時經 Tailscale Funnel 對外公開（`--host 127.0.0.1 --behind-proxy --allowed-host 網域`，伺服器本身只綁本機）：這時全網路都連得到登入頁，防線是金鑰、Host／Origin 檢查、登入失敗鎖定加全站失敗預算、Secure 的 `__Host-` Cookie、閒置 15 分鐘與最長 8 小時登出；Funnel 是否覆寫客戶端自帶的 X-Forwarded-For 官方文件沒寫，所以來源位址只是輔助、不能當唯一防線，首次使用要實際核對 `runtime/remote_events.jsonl`。提問、系統回答與檢索資訊（路由決定、檢索到的段落與分數、查詢結果表格）預設寫入 `runtime/remote_questions.jsonl`，含個資，登入頁與手冊都已告知組員，`--no-log-answers` 只記提問。測完要關 Funnel、撤銷金鑰。不要宣稱已經安全。晨報會讓後台對外連線抓科技新聞：來源網址寫死在 `news_feed.py`（https 公開網域）、不跟隨轉址、每個來源限 2MB、解析前拒收 DOCTYPE／ENTITY 與非 UTF-8，只顯示 https 的原文連結且不可點，早報文字不交給語言模型（類別與說明句是固定規則）。

## 2. 修改程式碼的流程

1. **先讀後改**：改動前先讀相關檔案與呼叫端，不憑記憶推測。
2. **先草稿**：非瑣碎的變更先在對話中說明方案與影響範圍，再動手寫檔。
3. **驗證**：改完要實際執行驗證（先跑 `python -m pytest tests`，再視需要語法檢查或實跑），回報真實結果；失敗就如實說失敗，不得宣稱完成。改動純邏輯時，同步補上對應的測試。
4. **審查**：較大的變更完成後，用 `/code-review`（內建）或 `mattpocock-skills:code-review` 審查 diff；涉及認證、SQL、檔案、網路、模型輸入的變更，另跑 `/security-review`。
5. **風險即停**：審查或驗證發現風險時，先回報並等我指示，不得以「僅供本地測試」、「風險很低」為由自行放行。

## 3. 設計原則

- **不寫死特例**：不要把修復綁在今天的某一份檔案格式、公式或樣本上。遇到問題先找通用解，再提出方案。若只能做特例，要明講並讓我決定。
- **邊界防禦**：設計對外功能時，把輸入驗證、惡意輸入攔截與權限邊界納入規劃。SQL 只允許白名單內的唯讀查詢。
- **沿用現有風格**：命名、註解密度、模組切分跟周邊程式碼一致。`backend_main.py` 只保留畫面，邏輯放進對應模組。
- 套件版本變動要先確認不會破壞 `requirements.txt` 中刻意的取捨（例如不裝 `llama-index` 總包（只用 core 與 readers-file 讀 PDF、Word）、`retinaface` 用 `--no-deps`）。

## 4. 日誌與資料處理

- 分析大型 `.log`（如 access.log）時，不要逐行貼進對話。先用腳本統計與篩選（高頻來源、未授權存取、異常狀態碼），依嚴重程度排序後再回報核心問題。
- 整理使用者回饋、待辦清單或一批 Markdown 時，分成：立即修復、下一版排程、封存。把判斷依據不足的項目獨立列為「需人工介入」，不要自行猜測。

## 5. 回報格式

- 每次執行結束，用**繁體中文**簡短總結：計畫、根本原因、實際修改的檔案與內容。
- 不寫冗長的英文報告；不確定或未驗證的部分要明確標出。
- 引用程式碼用 `檔名:行號`。

## 6. 版本控制與 CI

- 只有我明確要求時才 commit、push 或開 PR；預設不動 `main`。
- CI 為 Ruff、Bandit、Semgrep、CodeQL、Dependabot，採 diff 範圍把關。提交前先在本地跑 Ruff 與 Bandit，避免被 CI 擋下。

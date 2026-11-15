# CLAUDE.md

Smart Access Agent System：門禁子系統（人臉辨識、活體偵測、尾隨偵測）＋ 企業知識庫與出勤查詢（原「管理者助理」，只有 RAG 功能：查公司文件、考勤與異常事件的聊天視窗）。專案文件（README 等）已移除，待開發到一定階段再重新撰寫。

## 1. 專案要點

- 專案結構：
  - `shared/`：資料庫（`database_mgr`）、資源管理器（`resource_manager`）、統一路徑（`paths`）、後台登入的工作階段與失敗鎖定（`auth`，後台與助理視窗共用）、每日出勤日報的存檔與設定（`digest_store`）
  - `access/`：門禁，包含人臉辨識與活體（`vision_core`）、尾隨偵測（`security`）、語音（`audio_player`）、QR 註冊伺服器（`web_server`）、現場主程式（`terminal_app`）
  - 前台（第二台電腦，角色 front，不上網）：`front_dashboard`（第二個螢幕的功能模組儀表板）、`front_agent`（連線代理）、`front_launcher`（隱藏視窗啟動子行程）、`front_screens`（雙螢幕擺放、F11 全螢幕）。啟動前台.bat 只開儀表板（pythonw，沒有黑色視窗），連線代理由儀表板在背景開，人臉辨識（terminal_app）預設不開，按儀表板右上角「啟動人臉辨識」才啟動；儀表板放第一個非主螢幕、辨識視窗放主螢幕，F11 切全螢幕並記住。前台警示（尾隨、非活體、經過未打卡）除了儀表板紅色橫幅，辨識程式還會在背景響不同音型的提示音（`access/alert_sound`，每種冷卻 20 秒，環境變數 `SMART_ACCESS_ALERT_SOUND=0` 可關）；前台自我檢查會警告 Wi-Fi 仍連線（前台設計不上網，只警告）；前台包附「前台使用說明.txt」。前台不提供任何設定或管理員功能（打卡機上不能靠管理員刷臉維護）：前台只能啟動人臉辨識、不能停止（停止由後台在營運狀態頁「前台連線」卡片按「停止前台辨識」，二次確認後寫 `shared/front_control`，前台同步時收掉辨識並停用按鈕、恢復後仍要有人按「啟動」；或由前台電腦的 Windows 操作）；連線設定（後台位址、通行碼 `SMART_ACCESS_LINK_TOKEN`、憑證指紋）只在安裝與網路設定時處理；全本地模式與人臉辨識允許與否都由後台控制，前台同步時取得（`/v1/morning` 回應附 `local_only`、`recognition_enabled` 兩個布林，前台每 10 秒同步一次，只收布林，離線沿用最後一次的值）。實體鍵盤能直接關閉儀表板視窗（會一併收掉辨識與連線代理）或在辨識視窗按 q；只改前台畫面的工程師看 `前台開發指南.md`（前台程式地圖、用到的模型、改完怎麼驗證、送 PR 的規則），`python -m access.front_preview` 用假資料開儀表板，不需要攝影機、模型、後台
  - `admin/`：CustomTkinter 管理後台（`backend_main`）與背景服務管理（`service_manager`）；後台是單一固定大小的主視窗（最大 1280×820，螢幕小就縮），功能不另開彈出視窗：標題列固定（品牌、時鐘、登入者、淺色／深色／系統外觀按鈕，選擇記在 `runtime/ui_mode.json`），按功能卡片直接把內容區換成該功能頁，頁首「← 返回主選單」回來，底部狀態列固定；只有確認、錯誤對話框與新異常的通知小彈窗保留，操作結果改顯示在底部提示列；企業知識庫因為模型很大仍是獨立行程的聊天視窗；後台啟動先人臉登入（`login_window` 畫面、`face_login` 逐幀判斷），管理者在 `account_admin` 設定，最後一位管理者不能被取消、停用或刪除；每日報告由 `daily_digest`（傍晚出勤日報：明細、語言模型摘要與核對、排程判斷）、`morning_brief`（早上科技早報：只有科技新聞，不含出勤；連不上網時自動重抓，1、2、5 分鐘後每 10 分鐘、最多約 5 小時）、`news_feed`（抓繁中與國際科技媒體、AI 官方部落格 RSS 與 Hacker News，每個來源最新的優先、連線問題先原地重試，整理成 類別、發生什麼事、為什麼重要、可以怎麼用、來源與原文）、`digest_window`（背景產生；只看出勤日報的視窗，後台底部「📋 出勤日報」按鈕。科技早報後台不顯示，由前台代理同步後在前台第二個螢幕顯示）與 `schedule_window`（主選單第 6 個模組「出勤日報與科技早報」：排程時間、立即產生）負責，到設定時間（出勤日報預設 18:00、科技早報預設 08:30，後台開啟時才會跑，晚開會補；出勤資料只在出勤日報，科技早報不放）自動產生（人在主選單就直接切到出勤日報頁，在別的功能頁只在底部提示），摘要只在雲端模式使用
  - `rag/`：企業知識庫與出勤查詢（只有 RAG，不做排程；每日出勤日報排程將放在 `admin/`），只給後台管理者使用。文件讀取與切塊 `ingest`、向量庫 `doc_store`（FAISS + BM25）、向量模型 `embedding`（bge-m3）、重排 `rerank`、意圖判斷 `router`、日期解析 `dates`、人名比對 `entity_resolver`、唯讀固定查詢 `queries`（LLM 不寫 SQL）、問答流程 `answer`、語言模型設定與呼叫 `llm_settings`／`llm`、組裝 `bootstrap`、對話控制 `chat_session`、題庫與評估 `evaluation/`（檢索：`python -m rag.evaluation.retrieval_eval`；意圖判斷選工具：`python -m rag.evaluation.tool_eval --split dev`，題庫分開發集與封存測試集，測試集要加 `--confirm-test` 且不能拿來調整，需要真的語言模型；`mine_questions` 把組員真實問過的題目整理成待標註候選題）。聊天視窗是 `rag/chat_window`（開窗時顯示最新出勤日報與待處理異常；CustomTkinter，`python -m rag.chat_window`，後台按「企業知識庫與出勤查詢」另開行程）。遠端測試入口給組員用手機或電腦連線：`remote_server`（Flask 網頁，`python -m rag.remote_server`，金鑰用 `--add-key 名稱` 建立）、`remote_access`（金鑰庫、排隊閘門、提問紀錄、登入失敗預算與安全事件紀錄）、`remote_web/`（網頁）、`demo_data`（示範假資料，`python -m rag.demo_data add／remove`，只在明確要時執行）
  - 活體偵測模型的訓練已拆成獨立 repo：本機與本專案並列的 `Antispoof-Training` 資料夾（尚未推送到 GitHub）。正式系統只使用它匯出的 ONNX 檔，放在 `models/`，不直接依賴那邊的程式
- 啟動方式：一律在專案根目錄用 `python -m` 啟動，例如 `python -m admin.backend_main`、`python -m access.terminal_app`、`python -m rag.chat_window`、`python -m access.web_server`，不要直接執行檔案路徑。
- 雙機直連的網路設定與檢查：後台用系統管理員執行 `deploy\設定後台網路.bat`（角色、只放行 169.254.0.0/16 的 TCP 8770 與 UDP 8772）、`deploy\檢查後台網路.bat`（`python -m admin.network_check`，逐項 OK／WARN／FAIL）；前台用 `設定前台網路.bat`（只放行後台 8772 回來的 UDP，前台連後台是出站不需規則）。重跑會先清掉同前綴的舊規則。QR 註冊：開啟 QR 註冊時這台電腦所有網卡位址都是註冊通道（`shared/phone_address`：Wi-Fi Direct／行動熱點與區網優先，私有位址一律用 `ipaddress` 判斷、含 iPhone 熱點的 172.16.0.0/12，VPN 與其他位址排最後但仍列出，169.254 與 127 不列），後台註冊視窗用選單切換通道，QR 與網址跟著變；`設定後台網路.bat` 另開一條 TCP 5000 只放行同網段（LocalSubnet）的規則，`檢查後台網路.bat` 會檢查它（缺了只警告）。
- 路徑一律取自 `shared/paths.py`，不要在各檔自行用 `__file__` 推算。助理讀取的文件放 `data/docs/`（PDF、Word、txt、md）。執行期資料放 `runtime/`（資料庫、助理的向量庫、證據照片、LLM 設定）。
- 環境：Windows、Python 3.11（`.venv/`），依賴見 `requirements/main.txt`，完整版本鎖定在 `requirements/lock.txt`（安裝順序見 main.txt 檔頭；retinaface 要另外用 `--no-deps` 安裝）。
- 測試：`python -m pytest tests`（單元測試，不需要模型、GPU、攝影機或任何服務；用臨時的 `runtime/`，不會碰真實資料）。CI 使用較輕量的 `requirements/test.txt`。另有只在本機跑的整合測試 `python -m pytest -m integration`（真模型、真法規、真服務）與回歸測試 `python -m pytest -m regression`（對照 `tests/regression/baseline.json`；有意更新模型後用 `UPDATE_BASELINE=1` 重產）；兩者預設不執行。
- 驗證當天（前後台真機驗證）：後台雙擊 `驗證當天.bat`（`python -m admin.verify_day`，12 項引導驗證、自動擷取前台心跳、異常事件、出勤、防火牆等證據，報告寫在 `runtime/verification/日期_時間/report.md`，最後問 y/N 才用 `claude -p` 唯讀分析；`--auto` 只跑不需要人的項目），前台雙擊 `前台驗證.bat`（`python -m access.front_verify`，收集自我檢查、螢幕、攝影機、記錄檔並請人聽語音與警示音），前台報告用隨身碟複製到後台；報告會蓋掉通行碼、金鑰、雜湊類長字串，驗證流程只以唯讀開資料庫；流程說明見 `明天驗證流程.md`。展示前檢查（`展示前檢查.bat`、`展示啟動.bat`）含前台區段。
- 金鑰一律走環境變數（如 `NVIDIA_API_KEY`），不寫進檔案。`models/`、`data/`、`runtime/` 不進版控。
- 資安現況：整個後台啟動要先用人臉登入（只比對角色為 admin 的帳號，連續 5 幀單幀距離 < 1.0 且平均距離 < 0.9，活體分數 ≥ 0.85（原 0.90，學校光線下常過不了），先確認是正面臉才比對；畫面有多人時只驗證離鏡頭最近（臉框最大）的那一位；失敗 5 次鎖 60 秒；閒置 15 分鐘登出；助理視窗只接受後台給的有效代碼）。限制：活體模型對照片與螢幕翻拍的防禦不夠強（EER 約 8% 到 14%），且擋不住能直接讀寫檔案與資料庫的人；門禁終端機、QR 註冊伺服器本身沒有登入。遠端測試入口只靠每人一組的存取金鑰（預設 7 天到期，外洩即可查詢出勤與名單），啟動時不經人臉登入，用 waitress 當伺服器（不是 Flask 內建開發伺服器，但問答共用同一份模型、同時只處理 1 題），只適合少數人。測試時經 Tailscale Funnel 對外公開（`--host 127.0.0.1 --behind-proxy --allowed-host 網域`，伺服器本身只綁本機）：這時全網路都連得到登入頁，防線是金鑰、Host／Origin 檢查、登入失敗鎖定加全站失敗預算、Secure 的 `__Host-` Cookie、閒置 15 分鐘與最長 8 小時登出；Funnel 是否覆寫客戶端自帶的 X-Forwarded-For 官方文件沒寫，所以來源位址只是輔助、不能當唯一防線（waitress 只信任來自本機代理連線的 X-Forwarded-For 最右一筆，其他連線自帶的一律清掉），首次使用要實際核對 `runtime/remote_events.jsonl`。提問、系統回答與檢索資訊（路由決定、檢索到的段落與分數、查詢結果表格）預設寫入 `runtime/remote_questions.jsonl`，含個資，登入頁與手冊都已告知組員，`--no-log-answers` 只記提問。測完要關 Funnel、撤銷金鑰。不要宣稱已經安全。科技早報會讓後台對外連線抓科技新聞：來源網址寫死在 `news_feed.py`（https 公開網域）、不跟隨轉址、每個來源限 2MB、解析前拒收 DOCTYPE／ENTITY 與非 UTF-8，原文連結只收 https 公開網域（`shared/link_check`，後台與前台共用），前台每則新聞附原文的 QR Code 給手機掃（前台不上網、不開瀏覽器；前台開全本地模式就不顯示 QR），早報文字不交給語言模型（類別與說明句是固定規則）。

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

# Claude outputs

這個資料夾放的是開發過程中，Claude 針對這個專案寫的規劃文件跟工作報告，依主題分成四個子資料夾，不是專案本身的執行程式碼（執行程式碼在 repo 根目錄跟 `research/`）。

## 文件索引

| 主題 | 位置 | 說明 |
| --- | --- | --- |
| 學術／專案規劃 | `planning/` | 專題學術性強化計畫、活體偵測訓練教學、專案現況總覽 |
| 後端模組化重構 | `backend-modularization/` | 把 `backend_main.py` 從一支檔案拆成模組化卡片式管理後台的規劃與四階段（P1–P3）實作報告 |
| 健檢待辦排序 | `health-check/` | 專案健檢後，剩餘待辦項目依可行性／風險分級排序，以及已完成項目的報告 |
| RAG 子系統 | `rag/` | RAG 問答子系統的功能實作、bug 修復、驗證報告與對應測試腳本 |

## 各主題內容

### planning/

- `academic-enhancement-plan.md` — 專題學術性強化計畫（總綱）
- `anti-spoofing-training-guide.md` — 自訓練活體偵測模型完整訓練教學（對應強化計畫方向 3；實際訓練結果見 `research/antispoof_training/README.md`）
- `project-status.md` — 專案現況總覽

### backend-modularization/

依 `backend-modularization-plan.md` 這份規劃，分階段把管理後台從單一大檔案改成模組化卡片式介面：

- `backend-modularization-plan.md` — 規劃本身：參考 Syscom Cubi 的「功能模組卡片」組織方式（僅借用組織邏輯，不複製其功能或畫面）
- `p1-refactor-report.md` — P1：`backend_main.py` 拆分成職責分離的多支檔案
- `p2-account-admin-report.md` — P2：帳號管理模組（含資料庫欄位升級邏輯）
- `p3-card-launcher-report.md` — P3：主選單改成卡片式 Launcher
- `p3-style-refresh-report.md` — P3 追加：卡片牆視覺風格第一輪調整
- `p3-style-refresh-v2-report.md` — P3 追加：卡片牆視覺風格第二輪（深色懸浮卡片）調整

### health-check/

- `fastest-todo-items-report.md` — 健檢後尚未開始項目，依「多快能完成＋風險高低」排序
- `medium-tier-report.md` — 中等項目完成報告：`database_mgr.py` 時間窗具名化、WiFi 熱點設定化三項（對應 `WiFiDirectHotspotCore` 的部分改動）

### rag/

- `litellm-config-encoding-fix-report.md` — 修復 LiteLLM Proxy 啟動失敗（GBK 解碼錯誤）
- `mixed-question-report.md` — 混合型問題聯合查詢（考勤＋法規）功能實作
- `rag-sql-verification-report.md` — RAG 考勤查詢確實透過標準 SQL 查詢資料庫的驗證報告
- `test_ablation.py` / `test_mixed_question.py` — 對應功能的測試腳本

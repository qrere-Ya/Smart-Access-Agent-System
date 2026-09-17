# Architecture Document: Smart Access Agent System (智能門禁代理系統)

## 1. 專案整體架構與資料流
本系統採取**模組化解耦設計**，將硬體感知（視覺）、安全邏輯、狀態持久化與管理介面完全分離。

### 數據流向圖 (Data Flow)
`影像輸入 (Camera)` $\rightarrow$ `vision_core.py (人臉偵測 $\rightarrow$ 對齊 $\rightarrow$ 特徵萃取)` $\rightarrow$ `database_mgr.py (1:N 向量比對)` $\rightarrow$ `security.py (YOLO 人數統計)` $\rightarrow$ `判定結果` $\rightarrow$ `(TTS 語音輸出 / Web 儀表板更新 / 異常證據存檔)`。

### 執行步驟分解
1.  **感知階段**：`vision_core` 使用 `RetinaFace` 偵測人臉座標與 5 個關鍵特徵點。
2.  **正規化階段**：透過 `face_align` 將任意角度的人臉扭轉為 $112 \times 112$ 的標準正臉。
3.  **特徵化階段**：`ArcFace` 模型將影像轉換為一個 **512 維度的 L2-Normalized 向量 (Face DNA)**。
4.  **身份比對階段**：`database_mgr` 計算該向量與資料庫中所有員工向量的 **歐幾里得距離 (Euclidean Distance)**，取最小值且低於門檻值者判定為該員工。
5.  **安全檢查階段**：同步啟動 `security.py` 的 `YOLOv8` 偵測，若在放行瞬間偵測到 $\text{人數} > 1$，則觸發尾隨警報並生成證據圖。
6.  **狀態更新階段**：`database_mgr` 的狀態機根據當前時間判定為「上班/遲到/下班/加班」，並寫入 `Attendance` 表。
7.  **輸出階段**：結果推送到 `backend_main.py` (Tkinter 介面) 與 `web_server.py` (Flask 監控)。

---

## 2. 核心模組功能與關鍵邏輯

### 核心模組分析
| 模組名稱 | 核心功能 | 關鍵函式/邏輯 |
| :--- | :--- | :--- |
| `vision_core.py` | AI 視覺流水線 | `face_align()`: 使用 `SimilarityTransform` 進行人臉對齊；`feature_extract()`: 執行 ONNX 推理獲取 512D 特徵。 |
| `security.py` | 防尾隨與異常存證 | `count_persons()`: 透過 YOLOv8 篩選 `cls=0` 且面積 $>10\%$ 的目標；`generate_evidence()`: 將全圖、全身、頭部拼成三宮格證據圖。 |
| `database_mgr.py` | 持久化與狀態機 | `recognize_face()`: 執行 $\min(\|v_1 - v_2\|_2)$ 距離比對；`log_attendance()`: 實作時間窗判定邏輯（例如 08:00 前為上班，之後至 12:00 為遲到）。 |
| `web_server.py` | 遠端註冊與授權 | `internal_generate_token()`: 生成 8 位隨機 Token 並設 60s 有效期；`api_register()`: 實現「閱後即焚」註冊機制，成功後立即核銷 Token。 |
| `backend_main.py` | 戰情控制中心 | `refresh_dashboard_data()`: 每 5 秒輪詢 SQLite 數據，實時更新今日到職人數與異常件數。 |

---

## 3. 演算法、模型與實際參數設定

### AI 模型配置
*   **人臉偵測**：`RetinaFace` (quality='normal') $\rightarrow$ 輸出 5 點特徵座標。
*   **特徵萃取**：`ArcFace (R100)` $\rightarrow$ 輸出 **512 維度** 浮點數向量。
*   **活體偵測**：`MiniFASNetV2` $\rightarrow$ 輸出 Real/Fake 分數。
*   **物件偵測**：`YOLOv8s` (yolov8s.pt) $\rightarrow$ 專注於 `person` 類別。

### 關鍵數值參數
*   **辨識門檻值 (Threshold)**：`1.0` (在 `recognize_face` 中，距離 $\le 1.0$ 視為同一人)。
*   **人臉對齊尺寸**：$112 \times 112$ 像素。
*   **活體偵測輸入**：$80 \times 80$ 像素。
*   **YOLO 偵測信心值 (Confidence)**：`0.6`。
*   **尾隨判定面積比**：人體 Bounding Box 面積必須 $> \text{畫面總面積} \times 10\%$ 才會被計數（過濾遠處背景路人）。
*   **考勤時間窗**：
    *   上班：`07:40:00` $\sim$ `08:00:00`
    *   遲到：`08:00:00` $\sim$ `12:00:00`
    *   早退：`16:30:00` $\sim$ `17:40:00`
    *   下班：`17:40:00` $\sim$ `18:00:00`
*   **Web Token 有效期**：`60` 秒。

---

## 4. 關鍵技術決策與原因

### $\text{ONNX Runtime}$ $\text{vs}$ $\text{PyTorch/TensorFlow}$
*   **決策**：所有推理模型（ArcFace, MiniFASNet）均轉換為 `.onnx` 格式。
*   **原因**：邊緣設備部署需要極高推理速度且不希望安裝龐大的深度學習框架。ONNX 支援 `CUDAExecutionProvider` 與 `CPUExecutionProvider` 切換，能顯著降低延遲並維持 30+ FPS。

### $\text{L2-Norm}$ $\text{Distance}$ $\text{vs}$ $\text{Cosine Similarity}$
*   **決策**：使用 `np.linalg.norm` 計算歐幾里得距離。
*   **原因**：由於 ArcFace 特徵經過 L2 正規化（$\|v\|=1$），歐幾里得距離與餘弦相似度在數學上是線性相關的，但計算歐氏距離在 NumPy 中更直觀且高效。

### $\text{Sliding Window Token}$ $\text{vs}$ $\text{Session Cookie}$
*   **決策**：使用記憶體內緩衝區 `valid_tokens` 配合動態 QR Code。
*   **原因**：門禁註冊屬於高敏感操作。透過「後台生成 $\rightarrow$ QR Code 傳遞 $\rightarrow$ 註冊後立即核銷」的流程，防止 Token 被攔截後重複使用，確保只有現場掃碼者能註冊。

---

## 5. 限制與 TODO (尚未完成部分)

### 目前限制
1.  **硬體依賴**：視覺核心目前在 Windows 環境下高度依賴 `msjh.ttc` (微軟正黑體) 進行中文渲染，跨平台遷移需更換字體路徑。
2.  **資料庫鎖定**：使用 SQLite 在高併發寫入（如大量員工同時打卡）時可能會觸發 `database is locked` 錯誤。
3.  **光線敏感**：RetinaFace 在極低光環境下的偵測率會下降，缺乏自動補光邏輯。

### TODO 待辦清單
*   [ ] **AI 代理個人化**：`backend_main.py` 中的 「AI 代理與個人化設定」模組目前僅為 UI 佔位符，尚未實作 LLM 驅動的個人化歡迎語。
*   [ ] **多機同步**：目前資料庫為本地 `.db` 檔案，尚未實作雲端同步或集中式伺服器架構。
*   [ ] **活體判定整合**：`vision_core.py` 已實作 `check_liveness`，但主流程尚未強制要求活體通過才允許打卡。

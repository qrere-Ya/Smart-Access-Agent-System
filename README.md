# 智能門禁代理系統 (Smart Access Agent System)

> **基於邊緣視覺防偽門禁系統**  
> 本專案結合了 AI 大語言模型（LLM），打造一套具備活體防偽、防尾隨警報、打卡狀態記憶及自然語言互動的商用級門禁安防系統。

---

## 目錄 (Table of Contents)

- [核心功能 (Features)](#核心功能-features)
- [Tree Structure (專案目錄結構)](#tree-structure-專案目錄結構)
- [環境及模組 (Environment & Modules)](#環境及模組-environment--modules)
- [環境安裝指南 (Installation)](#環境安裝指南-installation)
- [下載辨識模型 (Download Models)](#下載辨識模型-download-models)
- [流程與講解 (Process & Explanation)](#流程與講解-process--explanation)
- [系統操作說明與 DEMO (Usage & Demo)](#系統操作說明與-demo-usage--demo)
- [參考資料、網頁 (References)](#參考資料網頁-references)

---

## 核心功能 (Features)

1. **邊緣視覺辨識**
   - 採用 **RetinaFace** 進行高精度人臉定位與 5 點特徵對齊。
   - 採用 **ArcFace (ONNX-GPU)** 萃取 512 維度人臉特徵碼，實作 L2-Norm 距離計算。
2. **智慧防尾隨系統**
   - 背景掛載 **YOLOv8** 物件偵測模型。當打卡放行時若偵測到門框內有多名人員，立即觸發異常警報。
   - 自動合成 **「全景 + 尾隨者全身 + 頭部特寫」的三宮格畫中畫 (PiP)** 證據圖並存檔。
3. **打卡邏輯與狀態機**
   - 內建 SQLite 資料庫，具備上下班時間區間判斷（遲到、早退、加班）。
   - 實作防連刷冷卻機制 (Cooldown) 與狀態記憶（判斷「今日已打卡」）。
4. **多執行緒與動態 UI**
   - 採用 Threading 分離攝影機渲染與 AI 推理，保持主畫面流暢不卡頓。
   - 結合 Pillow 實作高質感中文動態狀態列與 UI 提示。

---

## Tree Structure (專案目錄結構)

```bash
My_Smart_Access_System/
├── anomaly_logs/           # 存放防尾隨異常證據圖 (自動生成)
│   └── tailgate_YYYYMMDD_HHMMSS.jpg
├── database/               # 存放 SQLite 打卡資料庫 (自動生成)
│   └── attendance.db
├── models/                 # 存放 ArcFace 與 YOLO 模型檔案
│   ├── arcface_r100_v1.onnx
│   └── yolov8s.pt          # 首次執行時自動下載
├── src/
│   ├── main_system.py      # 主程式 (UI 與多執行緒中樞)
│   ├── vision_core.py      # 視覺核心 (RetinaFace + ArcFace)
│   ├── database_mgr.py     # 記憶中樞 (SQLite 打卡邏輯)
│   ├── security.py         # 安防核心 (YOLO 人數計算與證據合成)
│   └── utils.py            # 工具箱 (Pillow 中文渲染)
├── requirements.txt        # 環境套件清單
├── .gitignore              # Git 忽略清單 (模型、資料庫等大型檔案)
└── README.md               # 專案說明書
```

---

## 環境及模組 (Environment & Modules)

* **Python:** `3.10.x` / `3.9.x`
* **作業系統:** Windows 10 / 11 (64-bit) 建議
* **攝影機:** USB 或內建 Webcam (720p 以上)
* **記憶體:** 最低 8 GB RAM
* **Computer Vision:** OpenCV (`opencv-python`)
* **UI & Image Rendering:** Pillow (`PIL`)
* **Face Detection:** RetinaFace
* **Face Recognition:** ArcFace (ONNX Runtime)
* **Object Detection (Anti-Tailgating):** Ultralytics YOLOv8
* **Database:** SQLite3 (Python 內建)

### 硬體分流說明

> 本系統的 AI 推理速度完全取決於你的電腦硬體，請依照下方說明選擇對應的安裝方式。

**有 NVIDIA 獨立顯示卡（GPU 加速模式 - 推薦）**
- 辨識速度可達 **30 FPS 以上**，適合正式部署
- 需要預先安裝 CUDA Toolkit 與 cuDNN 驅動
- 使用 `onnxruntime-gpu` 套件

**只有內顯 / 文書筆電（CPU 模式）**
- 系統一樣可以運行，速度稍慢（約 5~15 FPS）
- 不需要 CUDA 環境，直接安裝即可
- 改用 `onnxruntime`（CPU 版）套件

---

## 環境安裝指南 (Installation)

使用具備 NVIDIA 獨立顯示卡的電腦運行（以發揮最佳效能）。但若無顯卡，亦可使用 CPU 模式運行。

### 步驟 1：建立虛擬環境

> 虛擬環境就像是給這個專案蓋一間獨立小房間，不會和電腦裡其他程式互相干擾。

```bash
python -m venv venv

# Windows 啟動虛擬環境：
venv/Scripts/activate

# Mac / Linux 啟動虛擬環境：
source venv/bin/activate
```

---

### 步驟 2：根據電腦配置選擇安裝方式

#### 具備 NVIDIA 顯卡（GPU 加速模式）

1. **NVIDIA Driver：** 至官網安裝最新版顯示卡驅動。  
2. **CUDA Toolkit：** 與 `onnxruntime-gpu` 最穩定的版本。  
3. **cuDNN：** 下載與 CUDA 版本對應的擴充包並覆蓋至 CUDA 目錄。  

**安裝 Python 套件**

```bash
# 1. 安裝核心套件 (此配置包含 onnxruntime-gpu)
pip install -r requirements.txt

# 2. 解決 RetinaFace 對舊版 TensorFlow 的依賴衝突
pip install retinaface --no-deps
```

驗證 GPU 是否可被偵測到：

---

#### 無獨立顯卡（純 CPU 模式）

請將 `requirements.txt` 內的 `onnxruntime-gpu` 刪除，或手動執行以下指令：

```bash
# 安裝 CPU 版本的 ONNX
pip install onnxruntime
pip install opencv-python numpy scikit-learn scikit-image Pillow ultralytics

# 安裝 RetinaFace (Bypass 限制)
pip install tensorflow
pip install retinaface --no-deps
```

---

## 下載辨識模型 (Download Models)

為了讓程式正常運作，請自行下載以下兩個核心權重檔，並放置於 `models/` 資料夾內：

### 1. ArcFace 特徵萃取模型

* 檔名：`arcface_r100_v1.onnx`
* 用途：將人臉轉換成 512 維度特徵碼，用來比對身份
* 存放位置：`models/arcface_r100_v1.onnx`
* [點此下載 (InsightFace-REST GitHub)](https://github.com/SthPhoenix/InsightFace-REST)

### 2. YOLOv8 人員偵測模型

* 檔名：`yolov8s.pt`
* 用途：偵測畫面中有幾個人，用於防尾隨判斷
* *提示：若未手動下載，首次執行 `ultralytics` 模組時，系統連網也會自動下載此檔案。*
* 手動下載：[Ultralytics GitHub Releases](https://github.com/ultralytics/assets/releases)

---

## 流程與講解 (Process & Explanation)

本系統的核心辨識邏輯分為四大階段：

1. **Face Detection（人臉定位）：** 主執行緒擷取即時影像後，透過 `RetinaFace` 偵測畫面中的人臉，並抓取雙眼、鼻子、雙嘴角共 5 個關鍵點座標 (Landmarks)。
2. **Face Alignment（人臉對齊）：** 將取得的 5 個關鍵點，與定義好的「標準正臉座標」進行 `SimilarityTransform`（仿射變換），將歪斜的人臉裁切並旋轉至 112×112 像素的端正臉型。
3. **Feature Extraction（特徵萃取）：** 將對齊好的人臉送入 `ArcFace (ONNX)` 模型，提取 512 維度浮點數陣列 (Embedding)。與 SQLite 資料庫內部的特徵碼進行 L2-Norm 歐氏距離計算，判定身份。
4. **Anti-Tailgating & Logic（防尾隨與商業邏輯）：** 辨識成功並開門的瞬間，啟動 `YOLOv8` 計算畫面內的人數。若「臉 = 1 張」但「人體 ≥ 2 個」，則觸發尾隨異常；同時系統透過 `database_mgr.py` 計算現在時間，寫入遲到或正常打卡的紀錄。

### 系統流程圖

```
攝影機串流開始
      │
      ▼
RetinaFace 偵測人臉
      │
   有人臉？ ──否──▶ 繼續等待
      │是
      ▼
ArcFace 萃取 512D 特徵碼
      │
      ▼
與資料庫所有員工特徵比對（L2-Norm）
      │
  距離 < 門檻？ ──否──▶ 顯示「未登記人員」
      │是
      ▼
確認身份，進入打卡邏輯
      │
  冷卻時間內？ ──是──▶ 略過，不重複打卡
      │否
      ▼
寫入 SQLite（姓名 + 時間 + 狀態）
      │
      ▼
觸發放行 + YOLOv8 啟動人數偵測
      │
  門框內 > 1 人？ ──是──▶ 觸發防尾隨警報
      │否              └──▶ 合成三宮格證據圖存檔
      ▼
正常放行，UI 顯示歡迎訊息
```

### 打卡出勤狀態判斷邏輯

| 打卡時間 | 判定狀態 |
|----------|----------|
| 上班 `09:00` 前 | ✅ 正常上班 |
| 上班 `09:01` 後 | ⚠️ 遲到 |
| 下班 `18:00` 前 | ⚠️ 早退 |
| 下班 `18:01` 後 | 💪 加班 |

---

## 系統操作說明與 DEMO (Usage & Demo)

請開啟終端機，進入 `src` 資料夾並執行主程式：

```bash
cd src
python main_system.py
```

### 鍵盤快捷鍵

* **`[E]` - 員工註冊 (Enroll)：** 需本人站在鏡頭前，按下後終端機會暫停，輸入姓名並按下 Enter 即可完成註冊與特徵綁定。
* **`[V]` - 訪客模式 (Visitor)：**（功能建置中）。
* **`[Q]` - 退出系統 (Quit)：** 安全釋放攝影機資源並關閉資料庫連線。

### 首次使用流程

```
第 1 步：執行 python main_system.py
第 2 步：按下 [E] 鍵進行員工人臉註冊（每位員工都需要做一次）
第 3 步：員工站在鏡頭前 → 輸入姓名 → 特徵存入資料庫
第 4 步：之後每次靠近鏡頭，系統即自動辨識並打卡
```

### 預期畫面輸出

- 畫面左上角：即時辨識到的人名與信心分數
- 畫面下方狀態列：打卡狀態（正常 / 遲到 / 加班）
- 偵測到尾隨時：畫面閃紅色警報框 + 終端機輸出警告訊息

### DEMO 畫面

當系統偵測到尾隨時，將自動於 `anomaly_logs/` 資料夾生成「三宮格」高解析度異常證據圖，供安防查驗：

*(請將您的尾隨測試圖片放在 `assets/` 資料夾下，並於此替換圖片連結)*

---

## 參考資料、網頁 (References)
* **主要參考：** [Face Recognition in Python](https://github.com/xxrjun/face-recognition?tab=readme-ov-file#face-recognition-in-python)
* **RetinaFace：** [Single-stage Dense Face Localisation in the Wild](https://arxiv.org/pdf/1905.00641.pdf)
* **ArcFace：** [InsightFace ONNX Models](https://github.com/onnx/models/tree/master/vision/body_analysis/arcface)
* **YOLOv8：** [Ultralytics Official Repository](https://github.com/ultralytics/ultralytics)
* **ONNX Runtime：** [Official Documentation](https://onnxruntime.ai/)
* **NVIDIA CUDA Toolkit：** https://developer.nvidia.com/cuda/toolkit
* **cuDNN：** https://developer.nvidia.com/cudnn
* **Python 3.10：** https://www.python.org/downloads/

---
# 智能門禁代理系統 (Smart Access Agent System)

> **基於邊緣視覺防偽與 OpenClaw 協作之次世代門禁系統** > 

## ✨ 核心功能 (Features)

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

## 環境需求 (Requirements)

* **Python:** 3.10.x / 3.9.x
* **NVIDIA Driver:** 最新版顯示卡驅動
* **CUDA Toolkit:** 11.8 或 12.x
* **cuDNN:** 對應 CUDA 版本的 Deep Neural Network library

---

## 安裝與部署指南 (Installation)

**1. 建立環境與安裝套件**
建議使用虛擬環境 (Virtual Environment) 以避免套件衝突：
```bash
# 建立並啟動虛擬環境 (可選)
python -m venv venv
source venv/Scripts/activate

# 安裝基礎依賴
pip install -r requirements.txt

# 若 RetinaFace 安裝時遇到 TensorFlow 版本衝突，請使用以下指令繞過限制：
pip install retinaface --no-deps

---

** 2.下載 AI 模型權重 **

請將以下模型檔案下載並放置於 models/ 資料夾內：

arcface_r100_v1.onnx: 用於人臉特徵萃取。

yolov8s.pt: YOLO 將在首次執行時自動下載，或可手動放入。

---

** 3. 專案目錄結構 **

My_Smart_Access_System/
├── anomaly_logs/       # 存放防尾隨異常證據圖 (自動生成)
├── database/           # 存放 SQLite 打卡資料庫 (自動生成)
├── models/             # 存放 ArcFace 與 YOLO 模型檔案
├── requirements.txt    # 環境套件清單
├── README.md           # 專案說明書
└── src/
    ├── main_system.py  # 主程式 (UI 與多執行緒中樞)
    ├── vision_core.py  # 視覺核心 (RetinaFace + ArcFace)
    ├── database_mgr.py # 記憶中樞 (SQLite 打卡邏輯)
    ├── security.py     # 安防核心 (YOLO 人數計算與證據合成)
    └── utils.py        # 工具箱 (Pillow 中文渲染)

---
## 系統操作說明 (Usage) ##
**在終端機中執行主程式：**

cd src
python main_system.py

**鍵盤快捷鍵：**

[E] - 員工註冊 (Enroll)： 需本人站在鏡頭前，按下後於終端機輸入姓名完成註冊。

[V] - 訪客模式 (Visitor)： 啟動 OpenClaw 語音代理系統通報 (功能建置中)。

[Q] - 退出系統 (Quit)： 安全關閉鏡頭與資料庫連線。

---
# 自訓練活體偵測模型（Anti-Spoofing / PAD）— 研究子專案

這個資料夾是**獨立於正式系統之外**的研究性質工作，跟 repo 根目錄那些已經真實環境驗證過的正式檔案（`vision_core.py`、`database_mgr.py`、`security.py`…）完全分開，不會被這裡的任何東西影響。

## 為什麼要獨立出來

repo 根目錄現在已經有 30+ 個正式模組檔案（人臉辨識、考勤資料庫、防尾隨、RAG 問答、消融實驗跑批…），全部混在同一層。訓練活體偵測模型會需要另外裝一批比較重的套件（PyTorch、albumentations…），也會產生大量資料集檔案跟訓練中間產物——這些都不該跟正式系統的程式碼混在一起。

之後如果還有其他「研究/實驗性質、不是正式上線程式碼」的工作，建議都放進 `research/` 底下開新的子資料夾，跟正式系統的根目錄保持乾淨分離。**這次只新增這一個子資料夾，repo 根目錄現有的正式檔案完全沒有被動到。**

## 目前訓練結果

以官方 CelebA-Spoof 測試集（67,170 張，19,923 真人 / 47,247 攻擊，依身分切分、與 train/val 無重疊）評估，三方比較：正式系統目前在用的現成模型 `MiniFASNetV2`、自訓練骨幹完全凍結的基準版本、以及在此基礎上解凍最後 3 個 block 再微調的版本。

| | MiniFASNetV2（現成） | 凍結骨幹（基準） | 解凍微調 |
| --- | --- | --- | --- |
| 驗證準確率 | 不適用（非本專案訓練） | 0.9740 | 0.9940 |
| 測試集 EER | 37.87%（門檻 0.0193） | 18.40%（門檻 0.0579） | 10.56%（門檻 0.0012） |
| ROC AUC | 0.671 | 0.898 | 0.963 |

![ROC 曲線對照](roc_comparison.png)

APCER／BPCER／ACER／EER 依 ISO/IEC 30107-3 標準計算。三個模型的分數分布差異很大（EER 對應門檻分別是 0.0193、0.0579、0.0012），固定用同一個門檻（例如 0.5）比較會失真，`evaluate_antispoof.py`／`evaluate_minifasnet.py` 都同時印出「固定門檻 0.5」與「各自 EER 門檻」下的數字，上表用的是各自 EER 門檻，是公平比較三個模型的方式（詳見兩個檔案內的說明）。MiniFASNetV2 在固定門檻 0.5 下的原始數字是 APCER=76.44%／BPCER=7.52%／ACER=41.98%——APCER 遠高於 BPCER，代表它在這個資料集上主要的弱點是「放過攻擊」而不是「誤拒真人」。

微調版的 EER 比現成的 MiniFASNetV2 低 72.1%（37.87% → 10.56%），凍結骨幹的基準版本也低 51.4%。這是合理的結果：MiniFASNetV2 是沒有在 CelebA-Spoof 上訓練過的現成模型，跨資料集評估（cross-dataset）本來就比在同一份資料集上訓練、驗證的模型吃虧，這也是這次自訓練活體偵測模型的動機所在——不是說 MiniFASNetV2 本身效果差，而是「換一個資料集／場景」時，沒有針對性訓練過的現成模型有明顯的泛化落差。

ROC AUC 是額外用 `roc_data_*.npz` 裡的 fpr/tpr 算出來的補充指標（`evaluate_antispoof.py`／`evaluate_minifasnet.py` 本身都不印這個值，這裡用 `numpy.trapezoid` 對 ROC 曲線做梯形積分得到），跟 EER 方向一致。

`roc_comparison.png` 是用 `plt.plot(fpr, tpr)` 直接畫 `outputs/roc_data_minifasnetv2.npz`／`outputs/roc_data_antispoof_best.npz`／`outputs/roc_data_antispoof_finetuned_best.npz` 三份資料的真實資料點產生的，畫圖腳本見文件最後「重新產生 ROC 對照圖」一節。

`evaluate_minifasnet.py` 的前處理依 `vision_core.py` 裡 `check_liveness()` 的實際邏輯：抓人臉框（來自官方 CelebA-Spoof 的 `<檔名>_BB.txt`，座標經實測驗證是相對 224×224 參考尺寸，需乘上原圖寬高 / 224 換算成像素）後上下左右各外擴 20%、resize 成 80×80、不做像素正規化、BGR 輸入、softmax 後取 index 0 當攻擊分數，跟正式系統實際跑的邏輯一致，才能算是公平對照。

## 資料夾結構

```
research/antispoof_training/
├── README.md               本檔案
├── requirements.txt        這個子專案專用的訓練依賴（跟正式系統的 requirements.txt 分開）
├── roc_comparison.png      MiniFASNetV2／凍結骨幹／解凍微調三方的 ROC 曲線對照圖（見上方「目前訓練結果」）
├── data/                    資料集（.gitignore 已排除，不會進版控，檔案太大）
│   ├── raw_footage/         原始蒐集的影片/照片，子資料夾：bona_fide / print_attack / replay_attack
│   ├── external/            公開資料集解壓縮後放這裡（CelebA-Spoof、NUAA…）
│   ├── dataset/              prepare_dataset.py 裁切出的人臉圖片
│   ├── dataset_split/        split_dataset.py 或 prepare_celeba_spoof.py 切分好的 train/val/test
│   └── dataset_split_official/  prepare_official_celeba_spoof.py 輸出的 manifest（train/val/test.json）
├── scripts/                  訓練 pipeline 各步驟（依序執行）
│   ├── prepare_dataset.py    影片/照片 → 裁切人臉（重用 vision_core.py 的偵測/對齊邏輯）
│   ├── split_dataset.py      照「人」切分 train/val/test，避免資料洩漏
│   ├── prepare_celeba_spoof.py  【時間緊迫時的替代路線】從 Hugging Face 下載已裁切
│   │                          好的 CelebA-Spoof 精簡版（6.7 萬張），直接輸出成切好的
│   │                          train/val/test（會複製圖片進 data/dataset_split/）；
│   │                          限制是沒有身分欄位、只能隨機切分，見檔案開頭說明
│   ├── prepare_official_celeba_spoof.py  官方 CelebA-Spoof 完整版（解壓縮後放在
│   │                          電腦裡任一資料夾，不用搬進這個 repo）。只讀
│   │                          metas/intra_test/ 的標籤 json，輸出很小的 manifest
│   │                          到 data/dataset_split_official/，圖片完全不複製、
│   │                          原地讀取，避免硬碟用量翻倍；因為路徑本身帶身分 ID，
│   │                          可以真正照人切 train/val，見檔案開頭說明
│   ├── celeba_spoof_dataset.py  共用的資料載入邏輯，train/evaluate 用它自動判斷
│   │                          要吃 dataset_split_official/ 的 manifest 還是
│   │                          dataset_split/ 的 ImageFolder，不用手動切換
│   ├── augmentation.py       資料擴增設定
│   ├── train_antispoof.py    第一階段：凍結骨幹，只訓練分類層
│   ├── finetune_unfreeze.py  第二階段：接續第一階段權重，解凍最後 3 個 block 再微調
│   ├── export_onnx.py        匯出 ONNX + onnxsim 簡化，可指定要匯出哪個 checkpoint
│   ├── evaluate_antispoof.py 計算 APCER/BPCER/ACER/EER，畫 ROC 曲線資料，可指定要評估哪個 checkpoint
│   └── evaluate_minifasnet.py 用同一套指標評估現有的 MiniFASNetV2.onnx，見上方「目前訓練結果」
└── outputs/                  訓練產出（.gitignore 已排除大檔案）
    ├── antispoof_best.pth                      第一階段（凍結骨幹）權重
    ├── antispoof_finetuned_best.pth             第二階段（解凍微調）權重
    ├── antispoof_mobilenetv3_antispoof_best.onnx / _sim.onnx          第一階段匯出
    ├── antispoof_mobilenetv3_antispoof_finetuned_best.onnx / _sim.onnx 第二階段匯出
    └── roc_data_minifasnetv2.npz / roc_data_antispoof_best.npz / roc_data_antispoof_finetuned_best.npz  三方模型各自的 ROC 曲線資料
```

## 怎麼跑（依序執行）

```bash
cd research/antispoof_training
pip install -r requirements.txt

# 1. 把 data/raw_footage/{bona_fide,print_attack,replay_attack}/ 底下的影片跑一次裁切
python scripts/prepare_dataset.py

# 2. 切分 train/val/test（照人切分，避免同一人同時出現在訓練跟測試集）
python scripts/split_dataset.py

# --- 或者：時間不夠、想先跑通整條 pipeline 拿初步數字，跳過 1、2，改跑 ---
# python scripts/prepare_celeba_spoof.py
# 這支會直接從 Hugging Face 下載已裁切好的 CelebA-Spoof 精簡版，輸出成切好的
# data/dataset_split/{train,val,test}/{bona_fide,attack}/，可以直接跳到第 3 步

# --- 或者：已經下載/解壓好官方 CelebA-Spoof 完整版，圖片留在原地不搬 ---
# $env:CELEBA_SPOOF_ROOT = "C:\Users\qrere\Downloads\CelebA_Spoof_\CelebA_Spoof"   # PowerShell
# python scripts/prepare_official_celeba_spoof.py
# 這支只讀官方 metas/intra_test/ 的標籤 json，輸出 manifest 到
# data/dataset_split_official/，圖片完全不複製，可以直接跳到第 3 步

# 3. 訓練（第一階段：凍結骨幹）
python scripts/train_antispoof.py

# 4.（建議）第二階段：解凍最後幾個 block 再微調，通常能顯著降低 EER，見上方「目前訓練結果」
python scripts/finetune_unfreeze.py

# 5. 評估（APCER/BPCER/ACER/EER + ROC 曲線資料），不指定參數預設評估第一階段的 antispoof_best.pth
python scripts/evaluate_antispoof.py
python scripts/evaluate_antispoof.py outputs/antispoof_finetuned_best.pth

# 6. 匯出 ONNX，同樣可以指定要匯出哪個 checkpoint（輸出檔名會自動帶上 checkpoint 名稱）
python scripts/export_onnx.py
python scripts/export_onnx.py outputs/antispoof_finetuned_best.pth

# 7.（可選）用同一套指標評估現有的 MiniFASNetV2，三方對照
python scripts/evaluate_minifasnet.py
```

## 跟正式系統的關係（訓練完之後）

`export_onnx.py` 匯出的最終模型檔**不會自動生效**。確認評估數字滿意之後，要自己手動：

1. 把選定的 `outputs/antispoof_mobilenetv3_<checkpoint 名稱>_sim.onnx` 複製到 repo 根目錄的 `models/` 資料夾（正式系統既有的模型都放在那裡，例如 `arcface_r100_v1.onnx`、`MiniFASNetV2.onnx`）
2. 依 `claude/anti-spoofing-training-guide.md` 第 7 節的建議，在 `vision_core.py` 裡讓自訓練模型跟現有的 `MiniFASNetV2` **並存**（不要直接取代），先比對兩者判定結果一致性，穩定後再決定要不要正式切換

這一步（真正修改 `vision_core.py`）屬於「動正式檔案」的範疇，等 `evaluate_minifasnet.py` 的三方對照數字出來、確認自訓練版本確實更好之後再另外討論怎麼接，這次先不動。

## 重新產生 ROC 對照圖

`roc_comparison.png` 是用下面這段 matplotlib 腳本，讀 `outputs/roc_data_minifasnetv2.npz`／`outputs/roc_data_antispoof_best.npz`／`outputs/roc_data_antispoof_finetuned_best.npz` 三份資料的 `fpr`/`tpr` 陣列畫出來的（`evaluate_minifasnet.py`／`evaluate_antispoof.py` 各自算好、存成 `.npz`），任一模型重新評估過後，把下面這段存成 `scripts/plot_roc.py`、在 `research/antispoof_training/` 底下用 `python scripts/plot_roc.py` 重新執行即可更新：

```python
import numpy as np
import matplotlib.pyplot as plt

FILES = {
    "MiniFASNetV2（現成模型）": {"path": "outputs/roc_data_minifasnetv2.npz", "eer": 0.3787},
    "凍結骨幹（基準）": {"path": "outputs/roc_data_antispoof_best.npz", "eer": 0.1840},
    "解凍微調": {"path": "outputs/roc_data_antispoof_finetuned_best.npz", "eer": 0.1056},
}

fig, ax = plt.subplots(figsize=(7, 6), dpi=150)

for label, info in FILES.items():
    data = np.load(info["path"])
    fpr, tpr = data["fpr"], data["tpr"]
    order = np.argsort(fpr)
    fpr, tpr = fpr[order], tpr[order]
    auc = np.trapezoid(tpr, fpr)
    ax.plot(fpr, tpr, linewidth=2, label=f"{label}（AUC={auc:.3f}, EER={info['eer']*100:.2f}%）")

ax.plot([0, 1], [0, 1], linestyle="--", color="gray", linewidth=1, label="隨機猜測")
ax.set_xlabel("False Positive Rate")
ax.set_ylabel("True Positive Rate")
ax.set_title("活體偵測模型 ROC 曲線對照（CelebA-Spoof 測試集，n=67,170）")
ax.legend(loc="lower right", fontsize=9)
fig.tight_layout()
fig.savefig("roc_comparison.png", dpi=150)
```

（如果要在圖上顯示正體中文標籤，額外用 `matplotlib.font_manager` 註冊一套系統上有的中文字型即可，跟中文顯示無關的部分不受影響。）

## 依賴套件說明

`requirements.txt` 只給這個子專案用，特意跟根目錄的 `requirements.txt`（正式系統依賴，`onnxruntime`、`opencv-python`、`retinaface` 等）分開——PyTorch 系列套件很重，只有訓練的時候才需要，不應該讓正式系統的環境背負這個重量。兩份 requirements 可以裝在同一個 venv，也可以另外開一個專用 venv（例如 `venv_antispoof`），看你偏好。

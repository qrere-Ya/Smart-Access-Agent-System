# 自訓練活體偵測（Face Anti-Spoofing / PAD）模型——完整訓練教學

### 適用於：Smart Access Agent System 專題的學術性強化（對應 academic-enhancement-plan.md 方向 3 的升級版）

撰寫日期：2026-09-09

---

## 0. 這份教學要解決什麼問題

`vision_core.py` 目前用的是現成的 `MiniFASNetV2` 做活體偵測，但這顆模型是別人訓練好的，你沒有辦法在論文裡講「我們做了什麼」，只能講「我們用了什麼」——這對專題的學術貢獻是不夠的。

這份教學會帶你**從零開始訓練一顆你自己的活體偵測模型**，讓你可以在論文裡寫「本研究自行蒐集資料、設計並訓練了一個輕量化的活體偵測模型」，並且最後跟現成的 MiniFASNetV2 做量化比較（這同時也是 academic-enhancement-plan.md 方向 1 消融實驗的素材）。

**範圍界定（一定要在論文裡寫清楚，口試會被問）**：本教學鎖定 **2D 簡單攻擊**——列印照片攻擊（print attack）與螢幕重播攻擊（replay attack，用手機/平板播放照片或影片）。3D 面具、矽膠面具、deepfake 影片這類進階攻擊不在範圍內，這是這個領域公認合理的學生專題範疇。

這個任務在學術文獻裡的正式名稱是 **Presentation Attack Detection（PAD）**，是二元分類問題（真人 vs. 攻擊），跟「這是誰」的人臉辨識完全不同、資料量需求也小很多，是專題自訓練模型最划算的選擇。

---

## 1. 為什麼選這個訓練方式（技術決策說明）

| 決策點 | 選擇 | 理由 |
|---|---|---|
| 模型類型 | 輕量 CNN（**MobileNetV3-Small**，遷移學習） | 比純手工特徵（LBP+SVM）準確率更高、更能吃到螢幕重播的細微色彩/紋理差異；比從零訓練大型 CNN 需要的資料量小很多；跟你現有的 ONNX Runtime 架構完全一致，訓練完可以直接匯出整合進 `vision_core.py` |
| 訓練策略 | **遷移學習（Transfer Learning）**，凍結大部分權重，只微調最後幾層 | 你能蒐集到的資料量（幾十到幾百張，遠不如工業界的百萬級資料集）不足以從零訓練出精準的模型，遷移學習是小資料集下拿到「精準」結果最可靠的做法 |
| 額外資料來源 | 建議申請 1-2 個公開活體偵測資料集（見第 3 節） | 光靠幾位同學蒐集的資料，樣本多樣性（不同手機型號、不同紙質、不同光線）通常不夠，加公開資料集能顯著提升模型的泛化能力，這也是「精準」這個目標的關鍵 |
| 資料擴增 | 亮度/對比度/模糊/JPEG 壓縮擾動 | 小資料集配上資料擴增，是提升精準度、避免過擬合最有效、成本最低的做法 |
| 評估指標 | **APCER / BPCER / ACER**（ISO/IEC 30107-3 標準指標）+ ROC 曲線 + EER | 這是 PAD 領域公認的標準指標，比隨便算一個「準確率」更有學術份量，論文評審看到這幾個詞就知道你做的是正規的評估 |

---

## 2. 整體流程總覽

```
第3步：資料蒐集（真人 + 攻擊樣本）
        │
第4步：資料前處理（用現有 RetinaFace 裁切人臉、切訓練/驗證/測試集）
        │
第5步：資料擴增
        │
第6步：模型訓練（MobileNetV3-Small 遷移學習）
        │
第7步：轉換成 ONNX
        │
第8步：量化評估（APCER/BPCER/ACER、ROC/EER）
        │
第9步：整合進 vision_core.py，跟 MiniFASNetV2 做真實環境對照比較
```

---

## 3. 第一步：資料蒐集規劃

### 3.1 真人（bona fide）樣本

用手邊的攝影機，對每位同學錄 10-15 秒的短片，涵蓋：
- 正臉、左右微轉 15-30 度
- 至少兩種光線環境（例如室內日光燈、靠窗自然光）
- 建議找 8-10 位同學就好（比方向 2 的 FAR/FRR 需要的 10-15 位少，但如果你們兩個方向都做，**強烈建議同一批人、同一次拍攝一起蒐集**，一魚兩吃，省下重複找人拍照的時間）

### 3.2 攻擊樣本（attack）

用同一批同學的照片，做兩種攻擊：

1. **列印攻擊**：把每人的正面照印出來（一般印表機、A4 紙即可），用攝影機對著印出來的照片拍攝，模擬光線角度變化
2. **重播攻擊**：把每人的照片或短影片顯示在手機/平板螢幕上，用攝影機對著螢幕拍攝——這種攻擊會出現螢幕的摩爾紋（moiré pattern）跟反光，是 CNN 相對容易學到的特徵

每種攻擊每人建議錄 5-10 秒，涵蓋不同拿持角度、不同距離。

### 3.3 資料量抓多少才夠

| 類別 | 建議樣本數（影格數，抽幀後） | 說明 |
|---|---|---|
| 真人 | 300-500 張 | 8-10 人 × 每人多角度/多光線抽幀 |
| 列印攻擊 | 150-250 張 | |
| 重播攻擊 | 150-250 張 | |

**如果人力有限，這個數字抓不到也沒關係**——第 3.4 節的公開資料集就是用來補這個缺口的，光靠自己蒐集的資料，用遷移學習也還是能訓練，只是精準度會比加了公開資料集的版本低一些，這點可以誠實寫進論文的「限制」章節。

### 3.4 建議申請的公開資料集（用來提升精準度，非必要但強烈建議）

| 資料集 | 涵蓋攻擊類型 | 取得方式 | 備註 |
|---|---|---|---|
| **NUAA Photograph Imposter Database** | 列印攻擊 | 公開下載，免申請 | 最經典、最容易取得，適合起步 |
| **CASIA-FASD** | 列印 + 重播 | 需向中科院自動化所申請帳號 | 申請流程通常幾天內會回覆，越早申請越好 |
| **Replay-Attack Database（Idiap）** | 列印 + 重播 | 需簽署學術授權合約（License Agreement） | 學術用途通常會核准，流程可能要 1-2 週 |
| **CelebA-Spoof** | 列印、重播、多種變化 | 公開下載（GitHub） | 規模最大、最新，但檔案量也最大 |

建議這學期一開始就先發信申請 CASIA-FASD 或 Replay-Attack（審核最慢），同時直接下載 NUAA 跟 CelebA-Spoof 先開始跑，不用等審核結果卡住進度。

---

## 4. 第二步：資料前處理——用你現有的 RetinaFace 直接裁切人臉

你的專案已經有 `vision_core.py` 的人臉偵測 pipeline，直接拿來用，不用重寫。以下是資料前處理腳本範例：

```python
# prepare_dataset.py
"""
把蒐集到的原始影片/照片，用現有的 RetinaFace 偵測裁切出人臉，
分類存成 bona_fide / print_attack / replay_attack 三個資料夾。
"""
import os
import cv2
from retinaface import RetinaFace  # 沿用專案既有的偵測器

RAW_DIR = "raw_footage"       # 原始影片/照片放這裡，子資料夾分 bona_fide / print_attack / replay_attack
OUT_DIR = "dataset"           # 輸出裁切好的人臉圖片
FRAME_SAMPLE_RATE = 5         # 每 5 幀抽 1 幀，避免同一段影片抽出太多幾乎一樣的畫面

def crop_faces_from_video(video_path, out_subdir, detector):
    cap = cv2.VideoCapture(video_path)
    frame_idx = 0
    saved = 0
    while True:
        ret, frame = cap.read()
        if not ret:
            break
        if frame_idx % FRAME_SAMPLE_RATE == 0:
            faces = detector.detect_faces(frame)  # 依你專案實際的 RetinaFace 呼叫介面調整
            for i, face in enumerate(faces):
                x1, y1, x2, y2 = face["facial_area"]
                crop = frame[y1:y2, x1:x2]
                if crop.size == 0:
                    continue
                crop = cv2.resize(crop, (224, 224))
                fname = f"{os.path.splitext(os.path.basename(video_path))[0]}_{frame_idx}_{i}.jpg"
                cv2.imwrite(os.path.join(out_subdir, fname), crop)
                saved += 1
        frame_idx += 1
    cap.release()
    return saved


def main():
    detector = RetinaFace  # 或依你專案封裝的方式初始化
    categories = ["bona_fide", "print_attack", "replay_attack"]
    for cat in categories:
        raw_cat_dir = os.path.join(RAW_DIR, cat)
        out_cat_dir = os.path.join(OUT_DIR, cat)
        os.makedirs(out_cat_dir, exist_ok=True)
        if not os.path.isdir(raw_cat_dir):
            continue
        total = 0
        for fname in os.listdir(raw_cat_dir):
            video_path = os.path.join(raw_cat_dir, fname)
            total += crop_faces_from_video(video_path, out_cat_dir, detector)
        print(f"[{cat}] 共裁切出 {total} 張人臉圖片")


if __name__ == "__main__":
    main()
```

### 4.1 切分訓練/驗證/測試集

**重要：一定要照「人」切分，不能照「圖片」隨機切分**——同一個人的臉出現在訓練集又出現在測試集，會讓模型「認人臉」而不是「認真假」，數字會虛高、沒有參考價值。

```python
# split_dataset.py
"""
照「人」切分資料集，避免同一人同時出現在訓練/測試集造成資料洩漏。
假設檔名格式為 <人名或編號>_<其他資訊>.jpg
"""
import os
import shutil
import random
from collections import defaultdict

random.seed(42)
SPLIT_RATIO = {"train": 0.7, "val": 0.15, "test": 0.15}

def split_by_person(category_dir, out_root, category):
    files_by_person = defaultdict(list)
    for fname in os.listdir(category_dir):
        person_id = fname.split("_")[0]
        files_by_person[person_id].append(fname)

    persons = list(files_by_person.keys())
    random.shuffle(persons)
    n = len(persons)
    n_train = int(n * SPLIT_RATIO["train"])
    n_val = int(n * SPLIT_RATIO["val"])

    splits = {
        "train": persons[:n_train],
        "val": persons[n_train:n_train + n_val],
        "test": persons[n_train + n_val:],
    }

    for split_name, split_persons in splits.items():
        out_dir = os.path.join(out_root, split_name, category)
        os.makedirs(out_dir, exist_ok=True)
        for person in split_persons:
            for fname in files_by_person[person]:
                shutil.copy(os.path.join(category_dir, fname), os.path.join(out_dir, fname))

if __name__ == "__main__":
    for category in ["bona_fide", "print_attack", "replay_attack"]:
        split_by_person(f"dataset/{category}", "dataset_split", category)
    print("切分完成，輸出在 dataset_split/{train,val,test}/{bona_fide,print_attack,replay_attack}/")
```

---

## 5. 第三步：資料擴增（提升小資料集下的精準度）

```python
# augmentation.py
import albumentations as A

train_transform = A.Compose([
    A.RandomBrightnessContrast(brightness_limit=0.2, contrast_limit=0.2, p=0.7),
    A.GaussianBlur(blur_limit=(3, 5), p=0.3),
    A.ImageCompression(quality_lower=50, quality_upper=95, p=0.4),  # 模擬不同壓縮品質
    A.HorizontalFlip(p=0.5),
    A.Rotate(limit=10, p=0.5),
    A.Normalize(mean=(0.485, 0.456, 0.406), std=(0.229, 0.224, 0.225)),
])
```

需要安裝：`pip install albumentations`

---

## 6. 第四步：模型訓練（MobileNetV3-Small 遷移學習）

```python
# train_antispoof.py
"""
用 ImageNet 預訓練的 MobileNetV3-Small 做遷移學習，
輸出層改成 2 類（bona_fide=0, attack=1，print_attack 跟 replay_attack 合併成 attack 一類；
如果想在論文裡分析「哪種攻擊比較難防」，也可以維持 3 類，程式碼備註處會提醒怎麼改）。
"""
import torch
import torch.nn as nn
from torch.utils.data import DataLoader
from torchvision import datasets, models, transforms

DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
BATCH_SIZE = 32
EPOCHS = 20
LR = 1e-4

# 二分類：bona_fide vs. attack（想做三分類分析攻擊類型，把資料夾結構
# 維持 bona_fide/print_attack/replay_attack 三個資料夾，num_classes 改成 3 即可）
NUM_CLASSES = 2

train_tf = transforms.Compose([
    transforms.Resize((224, 224)),
    transforms.ToTensor(),
    transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
])

train_ds = datasets.ImageFolder("dataset_split/train", transform=train_tf)
val_ds = datasets.ImageFolder("dataset_split/val", transform=train_tf)
train_loader = DataLoader(train_ds, batch_size=BATCH_SIZE, shuffle=True)
val_loader = DataLoader(val_ds, batch_size=BATCH_SIZE, shuffle=False)

print("類別對應：", train_ds.class_to_idx)  # 確認 0/1 對應到哪個資料夾

# 載入預訓練 MobileNetV3-Small，換掉最後的分類層
model = models.mobilenet_v3_small(weights=models.MobileNet_V3_Small_Weights.IMAGENET1K_V1)

# 遷移學習：先凍結所有層，只訓練新換上的分類層
for param in model.parameters():
    param.requires_grad = False

in_features = model.classifier[-1].in_features
model.classifier[-1] = nn.Linear(in_features, NUM_CLASSES)
model = model.to(DEVICE)

criterion = nn.CrossEntropyLoss()
optimizer = torch.optim.Adam(model.classifier.parameters(), lr=LR)

best_val_acc = 0.0
for epoch in range(EPOCHS):
    model.train()
    total_loss = 0.0
    for imgs, labels in train_loader:
        imgs, labels = imgs.to(DEVICE), labels.to(DEVICE)
        optimizer.zero_grad()
        outputs = model(imgs)
        loss = criterion(outputs, labels)
        loss.backward()
        optimizer.step()
        total_loss += loss.item()

    model.eval()
    correct, total = 0, 0
    with torch.no_grad():
        for imgs, labels in val_loader:
            imgs, labels = imgs.to(DEVICE), labels.to(DEVICE)
            outputs = model(imgs)
            preds = outputs.argmax(dim=1)
            correct += (preds == labels).sum().item()
            total += labels.size(0)
    val_acc = correct / total
    print(f"Epoch {epoch+1}/{EPOCHS}  loss={total_loss/len(train_loader):.4f}  val_acc={val_acc:.4f}")

    if val_acc > best_val_acc:
        best_val_acc = val_acc
        torch.save(model.state_dict(), "antispoof_best.pth")

print(f"訓練完成，最佳驗證準確率：{best_val_acc:.4f}，模型存在 antispoof_best.pth")
```

**微調技巧（如果凍結骨幹訓練完準確率不夠理想）**：把最後 2-3 個 block 解凍（`for param in model.features[-3:].parameters(): param.requires_grad = True`），用更小的學習率（例如 `1e-5`）再訓練幾個 epoch，通常可以再拉高幾個百分點的準確率。

---

## 7. 第五步：轉換成 ONNX，整合進現有架構

```python
# export_onnx.py
import torch
from torchvision import models
import torch.nn as nn

NUM_CLASSES = 2
model = models.mobilenet_v3_small(weights=None)
in_features = model.classifier[-1].in_features
model.classifier[-1] = nn.Linear(in_features, NUM_CLASSES)
model.load_state_dict(torch.load("antispoof_best.pth", map_location="cpu"))
model.eval()

dummy_input = torch.randn(1, 3, 224, 224)
torch.onnx.export(
    model, dummy_input, "antispoof_mobilenetv3.onnx",
    input_names=["input"], output_names=["output"],
    dynamic_axes={"input": {0: "batch_size"}, "output": {0: "batch_size"}},
    opset_version=12,
)
print("已匯出 antispoof_mobilenetv3.onnx")
```

匯出後，用 ONNX Runtime 載入的方式，跟你 `vision_core.py` 裡載入 ArcFace/RetinaFace 的 `onnxruntime.InferenceSession` 用法完全一樣，可以直接沿用同一套載入邏輯，這步不需要另外教。

**整合建議（跟現有 `check_liveness()` 並存，不要直接取代）**：先讓自訓練模型跟 MiniFASNetV2 同時跑、同時記錄兩者的判定結果，累積一批「兩個模型判斷不一致」的案例，這批案例本身就是很好的論文素材（可以分析「哪種攻擊自訓練模型抓得到、但 MiniFASNetV2 抓不到，反之亦然」），確認自訓練模型穩定可靠之後，再考慮要不要正式取代或並用兩者做多數決。

---

## 8. 第六步：量化評估（APCER / BPCER / ACER + ROC / EER）

```python
# evaluate_antispoof.py
"""
用測試集算出 PAD 領域標準指標：
- APCER (Attack Presentation Classification Error Rate)：攻擊樣本被誤判成真人的比例，越低越安全
- BPCER (Bona Fide Presentation Classification Error Rate)：真人被誤判成攻擊的比例，越低使用體驗越好
- ACER (Average Classification Error Rate)：兩者平均，綜合指標
- EER (Equal Error Rate)：調整判定門檻，讓 APCER = BPCER 時的錯誤率，門檻選擇的參考基準
"""
import torch
import numpy as np
from torch.utils.data import DataLoader
from torchvision import datasets, transforms
from torchvision import models
import torch.nn as nn
from sklearn.metrics import roc_curve

DEVICE = "cuda" if torch.cuda.is_available() else "cpu"

test_tf = transforms.Compose([
    transforms.Resize((224, 224)),
    transforms.ToTensor(),
    transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
])
test_ds = datasets.ImageFolder("dataset_split/test", transform=test_tf)
test_loader = DataLoader(test_ds, batch_size=32, shuffle=False)
print("類別對應：", test_ds.class_to_idx)  # 確認 0 是 bona_fide 還是 attack

model = models.mobilenet_v3_small(weights=None)
in_features = model.classifier[-1].in_features
model.classifier[-1] = nn.Linear(in_features, 2)
model.load_state_dict(torch.load("antispoof_best.pth", map_location=DEVICE))
model.to(DEVICE).eval()

all_scores, all_labels = [], []
with torch.no_grad():
    for imgs, labels in test_loader:
        imgs = imgs.to(DEVICE)
        outputs = torch.softmax(model(imgs), dim=1)
        attack_score = outputs[:, 1].cpu().numpy()  # 假設 index 1 是 attack 類別，依實際 class_to_idx 調整
        all_scores.extend(attack_score.tolist())
        all_labels.extend(labels.numpy().tolist())

all_scores = np.array(all_scores)
all_labels = np.array(all_labels)  # 0=bona_fide, 1=attack

def compute_apcer_bpcer(scores, labels, threshold):
    preds = (scores >= threshold).astype(int)  # 1=判定為攻擊
    attack_mask = labels == 1
    bonafide_mask = labels == 0
    apcer = np.mean(preds[attack_mask] == 0) if attack_mask.sum() > 0 else 0.0   # 攻擊被誤判成真人
    bpcer = np.mean(preds[bonafide_mask] == 1) if bonafide_mask.sum() > 0 else 0.0  # 真人被誤判成攻擊
    return apcer, bpcer

# 用預設門檻 0.5 算一組基本數字
apcer_05, bpcer_05 = compute_apcer_bpcer(all_scores, all_labels, 0.5)
acer_05 = (apcer_05 + bpcer_05) / 2
print(f"門檻=0.5 時：APCER={apcer_05:.2%}  BPCER={bpcer_05:.2%}  ACER={acer_05:.2%}")

# 找 EER（掃描所有可能門檻，找 APCER≈BPCER 的點）
fpr, tpr, thresholds = roc_curve(all_labels, all_scores)
fnr = 1 - tpr
eer_idx = np.nanargmin(np.absolute(fnr - fpr))
eer = (fpr[eer_idx] + fnr[eer_idx]) / 2
eer_threshold = thresholds[eer_idx]
print(f"EER={eer:.2%}，對應門檻={eer_threshold:.4f}")

# 存下 ROC 曲線資料，供畫圖用
np.savez("roc_data.npz", fpr=fpr, tpr=tpr, thresholds=thresholds)
print("ROC 曲線資料已存至 roc_data.npz，可用 matplotlib 畫圖：plt.plot(fpr, tpr)")
```

需要安裝：`pip install scikit-learn`

---

## 9. 第七步：跟 MiniFASNetV2 做對照比較（消融實驗素材）

用**同一份測試集**，把 MiniFASNetV2 也跑一次上面的 `compute_apcer_bpcer()`，做出這樣的比較表，直接放進論文：

| 模型 | APCER | BPCER | ACER | EER |
|---|---|---|---|---|
| 現成 MiniFASNetV2 | ?% | ?% | ?% | ?% |
| 自訓練 MobileNetV3-Small（僅自蒐資料） | ?% | ?% | ?% | ?% |
| 自訓練 MobileNetV3-Small（自蒐資料 + 公開資料集） | ?% | ?% | ?% | ?% |

這張表同時滿足三件事：(1) 證明你真的自己訓練了模型、(2) 量化比較「加公開資料集有沒有幫助」（直接回答「精準」這個目標）、(3) 跟現成模型比較，能講清楚自己的模型是更好、差不多、還是有取捨——不管數字好不好看，只要是誠實做出來的比較，口試都站得住腳。

---

## 10. 建議時程與常見坑

| 週次 | 工作 |
|---|---|
| 第 1 週 | 發信申請 CASIA-FASD / Replay-Attack；同時下載 NUAA、CelebA-Spoof 開始跑通 pipeline |
| 第 2 週 | 找同學蒐集自己的真人＋列印＋重播樣本（建議跟方向 2 的 FAR/FRR 資料一起蒐集） |
| 第 3 週 | 資料前處理、切分、擴增，先用小規模資料跑通第 6 節的訓練腳本，確認流程沒問題 |
| 第 4 週 | 正式訓練（含公開資料集），跑第 8 節的評估，畫 ROC 曲線 |
| 第 5 週 | 匯出 ONNX、整合進 `vision_core.py`，跑第 9 節的對照比較 |
| 第 6 週 | 整理成論文的實驗章節、圖表 |

**常見坑**：
- 忘記照「人」切分資料集，導致準確率虛高——這是最常見、也最容易被口試委員抓到的錯誤，務必用第 4.1 節的做法。
- 只用預設 0.5 門檻就下結論——PAD 領域標準做法是報告 EER 或至少列出幾個不同門檻下的 APCER/BPCER 取捨，單一門檻的數字說服力不夠。
- 螢幕重播攻擊的樣本如果解析度太低、太模糊，模型可能學到「畫質差=攻擊」這種捷徑特徵而不是真的學到摩爾紋/色彩特徵，導致實際部署時對高畫質手機拍攝的攻擊防不住——蒐集攻擊樣本時盡量涵蓋不同畫質的顯示設備。

---

## 11. 需要你決定的事

1. 要不要跟方向 2（FAR/FRR）的人臉資料蒐集合併進行，同一批同學一次拍完？（建議合併，省時間）
2. 要不要花時間申請 CASIA-FASD / Replay-Attack（審核可能要 1-2 週），還是先只用免申請的 NUAA + CelebA-Spoof？
3. 要不要做三分類（區分列印攻擊 vs. 重播攻擊），多做一層分析？成本很低（訓練腳本只要改 `NUM_CLASSES`），但可以多一個「哪種攻擊比較難防」的分析角度。

決定好之後，可以直接照這份教學的腳本開始做；如果你希望我幫你把這幾支腳本直接寫進專案資料夾、或幫你把公開資料集的申請信草稿寫好，都可以再跟我說。

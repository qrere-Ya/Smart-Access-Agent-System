"""
用 ImageNet 預訓練的 MobileNetV3-Small 做遷移學習，訓練自己的活體偵測（Presentation
Attack Detection）模型。輸出層改成 2 類（bona_fide=0, attack=1，print_attack 跟
replay_attack 合併成 attack 一類；如果想在論文裡分析「哪種攻擊比較難防」，把
NUM_CLASSES 改成 3、資料夾結構維持 bona_fide/print_attack/replay_attack 三個即可）。

跑之前先準備好資料，三選一：
  1. scripts/split_dataset.py（自己蒐集的影片裁切後照人切分）
  2. scripts/prepare_celeba_spoof.py（HuggingFace 精簡版，圖片會複製進
     data/dataset_split/）
  3. scripts/prepare_official_celeba_spoof.py（官方完整版，圖片留在原地，
     只產生 data/dataset_split_official/ 底下的 manifest）
這支腳本透過 celeba_spoof_dataset.py 的 build_split_dataset() 自動偵測用哪一種，
不用手動切換。
"""
import os
import time

import torch
import torch.nn as nn
from torch.utils.data import DataLoader
from torchvision import models, transforms

from celeba_spoof_dataset import build_split_dataset

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "..", "outputs")
os.makedirs(OUTPUT_DIR, exist_ok=True)

DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
BATCH_SIZE = 32
EPOCHS = 20
LR = 1e-4
# 官方資料圖片是即時從硬碟讀取（沒有複製到 dataset_split/），單執行緒讀圖會是
# 瓶頸，開多個 worker 平行讀圖/解碼/resize；HF 版或自己蒐集的資料量小很多，
# 開 workers 一樣有幫助、只是差距沒那麼明顯。Windows 上開 num_workers>0 一定要
# 搭配 `if __name__ == "__main__":` 保護（下面 main() 已經有了）。
# 【16GB RAM 機器注意】Windows 開多個 worker 是整個重新啟動 Python/PyTorch 行程
# （不是省記憶體的 fork），worker 數開太多容易把記憶體塞滿、觸發分頁（硬碟狂讀但
# CPU 閒置），反而更慢。16GB 系統建議別超過 4；記憶體更緊繃可以再往下調到 2。
NUM_WORKERS = 4
LOG_EVERY_N_STEPS = 200  # 訓練中途印一次進度，避免整個 epoch 跑完前畫面完全沒反應

# 二分類：bona_fide vs. attack（三分類分析攻擊類型，把這裡改成 3）
NUM_CLASSES = 2

train_tf = transforms.Compose([
    transforms.Resize((224, 224)),
    transforms.ToTensor(),
    transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
])


def main():
    train_ds = build_split_dataset("train", transform=train_tf)
    val_ds = build_split_dataset("val", transform=train_tf)
    train_loader = DataLoader(
        train_ds, batch_size=BATCH_SIZE, shuffle=True,
        num_workers=NUM_WORKERS, pin_memory=(DEVICE == "cuda"),
    )
    val_loader = DataLoader(
        val_ds, batch_size=BATCH_SIZE, shuffle=False,
        num_workers=NUM_WORKERS, pin_memory=(DEVICE == "cuda"),
    )

    print(f"train={len(train_ds)} 筆, val={len(val_ds)} 筆")
    print("類別對應：", train_ds.class_to_idx)  # 確認 0/1 對應到哪個資料夾

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
    best_model_path = os.path.join(OUTPUT_DIR, "antispoof_best.pth")

    for epoch in range(EPOCHS):
        epoch_start = time.time()
        model.train()
        total_loss = 0.0
        n_batches = len(train_loader)
        for step, (imgs, labels) in enumerate(train_loader, start=1):
            imgs, labels = imgs.to(DEVICE), labels.to(DEVICE)
            optimizer.zero_grad()
            outputs = model(imgs)
            loss = criterion(outputs, labels)
            loss.backward()
            optimizer.step()
            total_loss += loss.item()

            if step % LOG_EVERY_N_STEPS == 0 or step == n_batches:
                elapsed = time.time() - epoch_start
                imgs_per_sec = (step * BATCH_SIZE) / elapsed if elapsed > 0 else 0.0
                eta_min = (n_batches - step) * (elapsed / step) / 60
                print(f"  epoch {epoch + 1} step {step}/{n_batches}  "
                      f"loss={total_loss / step:.4f}  {imgs_per_sec:.0f} 張/秒  "
                      f"這個 epoch 預估還剩 {eta_min:.1f} 分鐘")

        model.eval()
        correct, total = 0, 0
        with torch.no_grad():
            for imgs, labels in val_loader:
                imgs, labels = imgs.to(DEVICE), labels.to(DEVICE)
                outputs = model(imgs)
                preds = outputs.argmax(dim=1)
                correct += (preds == labels).sum().item()
                total += labels.size(0)
        val_acc = correct / total if total > 0 else 0.0
        epoch_minutes = (time.time() - epoch_start) / 60
        print(f"Epoch {epoch + 1}/{EPOCHS}  loss={total_loss / len(train_loader):.4f}  "
              f"val_acc={val_acc:.4f}  ({epoch_minutes:.1f} 分鐘)")
        if epoch == 0:
            print(f"  → 第一個 epoch 花了 {epoch_minutes:.1f} 分鐘，"
                  f"剩下 {EPOCHS - 1} 個 epoch 粗估還要約 {epoch_minutes * (EPOCHS - 1):.0f} 分鐘"
                  f"（實際會因為 GPU/硬碟熱快取而有出入，僅供參考；如果這個估計時間"
                  f"太長，可以按 Ctrl+C 中斷，回去調整 train_antispoof.py 的 EPOCHS 或"
                  f"prepare_official_celeba_spoof.py 的 MAX_IMAGES_PER_CLASS 後重跑）")

        if val_acc > best_val_acc:
            best_val_acc = val_acc
            torch.save(model.state_dict(), best_model_path)

    print(f"訓練完成，最佳驗證準確率：{best_val_acc:.4f}，模型存在 {best_model_path}")


if __name__ == "__main__":
    main()

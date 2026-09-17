"""
第二階段微調：接續 train_antispoof.py 訓練好的 antispoof_best.pth（骨幹完全凍結、
只訓練最後一層分類器），解凍 MobileNetV3-Small 骨幹最後幾個 block，用更小的學習率
再訓練幾輪。

【為什麼要多這一步】
train_antispoof.py 的骨幹完全沿用 ImageNet 預訓練特徵，這些特徵擅長「這是什麼
物體」這種語義層級的判斷，但活體偵測真正要靠的線索——螢幕摩爾紋、列印紙張紋理、
色彩還原差異——是比較底層、高頻的視覺特徵，凍結骨幹不一定對這些線索敏感。這造成
的典型症狀就是：整體準確率看起來不錯，但 APCER（攻擊被誤判成真人）明顯偏高——
如果你 evaluate_antispoof.py 跑出來的 APCER 遠高於 BPCER，這支腳本就是對症下藥的
下一步：解凍最後幾層，讓骨幹能微調出對這些紋理線索更敏感的特徵。

【這不是取代，是新增一個可以比較的版本】
這支腳本從 antispoof_best.pth 的權重繼續訓練，但存成另一個檔案
（antispoof_finetuned_best.pth），不會覆蓋掉原本「凍結骨幹」版的
antispoof_best.pth。兩個版本剛好可以拿來做消融比較（凍結骨幹 vs. 部分解凍微調），
直接是論文可以用的一組實驗——用 evaluate_antispoof.py 對這兩個檔案各跑一次就能
拿到對照表。

用法（接在 train_antispoof.py 之後跑）：
    python scripts/finetune_unfreeze.py
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
EPOCHS = 5          # 只是微調最後幾層，通常不需要像第一階段訓練 20 epoch 那麼久
LR = 1e-5            # 比第一階段的 1e-4 小一個量級，避免把預訓練特徵破壞掉
NUM_UNFROZEN_BLOCKS = 3   # 解凍 model.features 最後幾個 block；改大一點解凍更多層
NUM_WORKERS = 4
LOG_EVERY_N_STEPS = 200
NUM_CLASSES = 2

BASE_MODEL_PATH = os.path.join(OUTPUT_DIR, "antispoof_best.pth")
FINETUNED_MODEL_PATH = os.path.join(OUTPUT_DIR, "antispoof_finetuned_best.pth")

train_tf = transforms.Compose([
    transforms.Resize((224, 224)),
    transforms.ToTensor(),
    transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
])


def main():
    if not os.path.isfile(BASE_MODEL_PATH):
        raise SystemExit(f"找不到 {BASE_MODEL_PATH}，先跑 train_antispoof.py 訓練出第一階段的模型")

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

    model = models.mobilenet_v3_small(weights=None)
    in_features = model.classifier[-1].in_features
    model.classifier[-1] = nn.Linear(in_features, NUM_CLASSES)
    model.load_state_dict(torch.load(BASE_MODEL_PATH, map_location="cpu"))
    print(f"已載入第一階段權重：{BASE_MODEL_PATH}")

    # 先確保全部凍結，再解凍最後 NUM_UNFROZEN_BLOCKS 個 block + 分類器
    for param in model.parameters():
        param.requires_grad = False
    for param in model.features[-NUM_UNFROZEN_BLOCKS:].parameters():
        param.requires_grad = True
    for param in model.classifier.parameters():
        param.requires_grad = True

    trainable = sum(p.numel() for p in model.parameters() if p.requires_grad)
    total = sum(p.numel() for p in model.parameters())
    print(f"解凍最後 {NUM_UNFROZEN_BLOCKS} 個 block，可訓練參數 {trainable:,}/{total:,}")

    model = model.to(DEVICE)
    criterion = nn.CrossEntropyLoss()
    optimizer = torch.optim.Adam(
        [p for p in model.parameters() if p.requires_grad], lr=LR
    )

    best_val_acc = 0.0

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

        if val_acc > best_val_acc:
            best_val_acc = val_acc
            torch.save(model.state_dict(), FINETUNED_MODEL_PATH)

    print(f"\n第二階段微調完成，最佳驗證準確率：{best_val_acc:.4f}，模型存在 {FINETUNED_MODEL_PATH}")
    print(f"接下來可以跑：python scripts/evaluate_antispoof.py {FINETUNED_MODEL_PATH}")
    print(f"跟第一階段的 antispoof_best.pth 對照，看 APCER 有沒有改善")


if __name__ == "__main__":
    main()

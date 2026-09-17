"""
用測試集算出 PAD（Presentation Attack Detection）領域標準指標：
- APCER (Attack Presentation Classification Error Rate)：攻擊樣本被誤判成真人的比例，越低越安全
- BPCER (Bona Fide Presentation Classification Error Rate)：真人被誤判成攻擊的比例，越低使用體驗越好
- ACER (Average Classification Error Rate)：兩者平均，綜合指標
- EER (Equal Error Rate)：調整判定門檻，讓 APCER = BPCER 時的錯誤率，門檻選擇的參考基準

用法：
    python scripts/evaluate_antispoof.py                              # 評估 outputs/antispoof_best.pth（凍結骨幹版）
    python scripts/evaluate_antispoof.py outputs/antispoof_finetuned_best.pth  # 評估指定的 checkpoint（例如 finetune_unfreeze.py 的產出），方便兩個版本互相比較
"""
import os
import sys

import numpy as np
import torch
import torch.nn as nn
from sklearn.metrics import roc_curve
from torch.utils.data import DataLoader
from torchvision import models, transforms

from celeba_spoof_dataset import build_split_dataset

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "..", "outputs")

DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
NUM_WORKERS = 4  # 跟 train_antispoof.py 同樣的理由（16GB RAM 機器），見那邊的註解

test_tf = transforms.Compose([
    transforms.Resize((224, 224)),
    transforms.ToTensor(),
    transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
])


def compute_apcer_bpcer(scores, labels, threshold):
    preds = (scores >= threshold).astype(int)  # 1=判定為攻擊
    attack_mask = labels == 1
    bonafide_mask = labels == 0
    apcer = np.mean(preds[attack_mask] == 0) if attack_mask.sum() > 0 else 0.0   # 攻擊被誤判成真人
    bpcer = np.mean(preds[bonafide_mask] == 1) if bonafide_mask.sum() > 0 else 0.0  # 真人被誤判成攻擊
    return apcer, bpcer


def main():
    test_ds = build_split_dataset("test", transform=test_tf)
    test_loader = DataLoader(
        test_ds, batch_size=32, shuffle=False,
        num_workers=NUM_WORKERS, pin_memory=(DEVICE == "cuda"),
    )
    print(f"test={len(test_ds)} 筆")
    print("類別對應：", test_ds.class_to_idx)  # 確認 0 是 bona_fide 還是 attack

    model_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(OUTPUT_DIR, "antispoof_best.pth")
    print(f"評估模型：{model_path}")

    model = models.mobilenet_v3_small(weights=None)
    in_features = model.classifier[-1].in_features
    model.classifier[-1] = nn.Linear(in_features, 2)
    model.load_state_dict(torch.load(model_path, map_location=DEVICE))
    model.to(DEVICE).eval()

    all_scores, all_labels = [], []
    with torch.no_grad():
        for imgs, labels in test_loader:
            imgs = imgs.to(DEVICE)
            outputs = torch.softmax(model(imgs), dim=1)
            attack_score = outputs[:, 1].cpu().numpy()  # index 1 假設是 attack 類別，依實際 class_to_idx 調整
            all_scores.extend(attack_score.tolist())
            all_labels.extend(labels.numpy().tolist())

    all_scores = np.array(all_scores)
    all_labels = np.array(all_labels)  # 0=bona_fide, 1=attack

    apcer_05, bpcer_05 = compute_apcer_bpcer(all_scores, all_labels, 0.5)
    acer_05 = (apcer_05 + bpcer_05) / 2
    print(f"門檻=0.5 時：APCER={apcer_05:.2%}  BPCER={bpcer_05:.2%}  ACER={acer_05:.2%}")

    fpr, tpr, thresholds = roc_curve(all_labels, all_scores)
    fnr = 1 - tpr
    eer_idx = np.nanargmin(np.absolute(fnr - fpr))
    eer = (fpr[eer_idx] + fnr[eer_idx]) / 2
    eer_threshold = thresholds[eer_idx]
    print(f"EER={eer:.2%}，對應門檻={eer_threshold:.4f}")

    # 額外印出「用 EER 門檻」時的 APCER/BPCER，而不是死守 0.5——不同模型（尤其
    # 微調過骨幹的版本）分數分布可能整個偏移，固定用 0.5 比較容易誤判「變差了」，
    # 實際上是門檻沒有跟著校準。兩個模型都用各自的 EER 門檻比較，才是公平的比較
    # 方式，也是 PAD 領域的標準做法（見 README 常見坑）。
    apcer_eer, bpcer_eer = compute_apcer_bpcer(all_scores, all_labels, eer_threshold)
    acer_eer = (apcer_eer + bpcer_eer) / 2
    print(f"EER 門檻下：APCER={apcer_eer:.2%}  BPCER={bpcer_eer:.2%}  ACER={acer_eer:.2%}")

    # 檔名帶上評估的是哪個 checkpoint，避免評估 finetune 版的時候覆蓋掉凍結骨幹版的
    # ROC 資料——兩份都留著，之後畫圖比較兩個版本才有得比
    model_tag = os.path.splitext(os.path.basename(model_path))[0]
    roc_path = os.path.join(OUTPUT_DIR, f"roc_data_{model_tag}.npz")
    np.savez(roc_path, fpr=fpr, tpr=tpr, thresholds=thresholds)
    print(f"ROC 曲線資料已存至 {roc_path}，可用 matplotlib 畫圖：plt.plot(fpr, tpr)")


if __name__ == "__main__":
    main()

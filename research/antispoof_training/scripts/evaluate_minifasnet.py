"""
用跟 evaluate_antispoof.py 完全一樣的指標（APCER/BPCER/ACER/EER + ROC），在同一個
CelebA-Spoof 測試集上評估正式系統目前在用的 MiniFASNetV2（repo 根目錄 models/MiniFASNetV2.onnx），
讓自訓練的兩個版本（antispoof_best.pth / antispoof_finetuned_best.pth）可以跟現成模型三方對照。

【前處理必須跟 vision_core.py 的 check_liveness() 完全一致，否則比較沒有意義】
本腳本的裁切/resize/推論邏輯是照抄 vision_core.py 的 check_liveness()：
  1. 拿到人臉框 (x1,y1,x2,y2) 後，上下左右各外擴 20%（check_liveness 原本是外擴
     偵測到的人臉框；這裡用 CelebA-Spoof 官方提供的 <檔名>_BB.txt 人臉框取代即時
     偵測框，邏輯完全相同）
  2. 裁切、resize 成 80x80，「不做」像素正規化（不除 255、不減均值），直接
     astype(float32) 丟進模型——這是刻意照抄，不是忘記加正規化
  3. 輸入用 BGR（cv2.imread 預設），不轉 RGB——正式系統的 frame 是攝影機 BGR 畫面，
     check_liveness 也沒有做 BGR→RGB 轉換，這裡刻意保持一致
  4. 兩個 softmax 輸出中，index 1 是「真人」分數（check_liveness 裡 real_score =
     prediction[0][1]），所以 attack_score = prediction[0][0]

【CelebA-Spoof 官方 BB.txt 格式（實測 + 對照圖片內容驗證過，不是猜的）】
每個 <檔名>_BB.txt 存一行 5 個數字：x y w h score，x/y/w/h 是相對 224x224 參考尺寸
的座標，換算成原圖像素座標要乘上「原圖寬高 / 224」：
    real_x = x / 224 * 原圖寬度
    real_y = y / 224 * 原圖高度
    real_w = w / 224 * 原圖寬度
    real_h = h / 224 * 原圖高度
（已用兩張不同尺寸的官方圖片實際裁切驗證，裁出來的區域正好是臉部，不是憑空假設）

用法：
    python scripts/evaluate_minifasnet.py
    python scripts/evaluate_minifasnet.py --split test        # 預設就是 test
"""
import argparse
import os
import time

import cv2
import numpy as np
import onnxruntime as ort
from sklearn.metrics import roc_curve

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_ROOT = os.path.join(SCRIPT_DIR, "..", "data")
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "..", "outputs")
# repo 根目錄：scripts/ -> antispoof_training/ -> research/ -> Smart_Access_Agent_System/
REPO_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, "..", "..", ".."))
MODEL_PATH = os.path.join(REPO_ROOT, "models", "MiniFASNetV2.onnx")

BB_REFERENCE_SIZE = 224  # BB.txt 座標相對的參考尺寸，見檔頭說明
LIVENESS_INPUT_SIZE = 80  # 跟 vision_core.py check_liveness() 一致
EXPAND_RATIO = 0.2  # 人臉框上下左右外擴比例，跟 check_liveness() 一致
LOG_EVERY_N_STEPS = 2000


def load_manifest(split):
    manifest_path = os.path.join(DATA_ROOT, "dataset_split_official", f"{split}.json")
    if not os.path.isfile(manifest_path):
        raise SystemExit(
            f"找不到 {manifest_path}，先跑 scripts/prepare_official_celeba_spoof.py 產生 manifest"
        )
    import json
    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)
    return manifest["celeba_root"], manifest["entries"]


def read_bbox(bb_path, img_w, img_h):
    """讀官方 <檔名>_BB.txt，回傳原圖像素座標的 (x1, y1, x2, y2)。"""
    with open(bb_path, "r", encoding="utf-8") as f:
        parts = f.read().split()
    x, y, w, h = (float(v) for v in parts[:4])
    real_x = x / BB_REFERENCE_SIZE * img_w
    real_y = y / BB_REFERENCE_SIZE * img_h
    real_w = w / BB_REFERENCE_SIZE * img_w
    real_h = h / BB_REFERENCE_SIZE * img_h
    return real_x, real_y, real_x + real_w, real_y + real_h


def crop_and_expand(frame_bgr, face_box):
    """完全照抄 vision_core.py check_liveness() 的裁切邏輯（外擴 20% + resize 80x80）。"""
    x1, y1, x2, y2 = face_box
    w, h = x2 - x1, y2 - y1
    x1 = max(0, int(x1 - w * EXPAND_RATIO))
    y1 = max(0, int(y1 - h * EXPAND_RATIO))
    x2 = min(frame_bgr.shape[1], int(x2 + w * EXPAND_RATIO))
    y2 = min(frame_bgr.shape[0], int(y2 + h * EXPAND_RATIO))

    face_img = frame_bgr[y1:y2, x1:x2]
    if face_img.size == 0:
        return None

    face_img = cv2.resize(face_img, (LIVENESS_INPUT_SIZE, LIVENESS_INPUT_SIZE))
    face_img = face_img.astype(np.float32)  # 不正規化，跟 check_liveness() 一致
    face_img = np.expand_dims(face_img.transpose(2, 0, 1), axis=0)
    return face_img


def compute_apcer_bpcer(scores, labels, threshold):
    preds = (scores >= threshold).astype(int)
    attack_mask = labels == 1
    bonafide_mask = labels == 0
    apcer = np.mean(preds[attack_mask] == 0) if attack_mask.sum() > 0 else 0.0
    bpcer = np.mean(preds[bonafide_mask] == 1) if bonafide_mask.sum() > 0 else 0.0
    return apcer, bpcer


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--split", default="test")
    args = parser.parse_args()

    if not os.path.isfile(MODEL_PATH):
        raise SystemExit(f"找不到 {MODEL_PATH}，確認 repo 根目錄 models/ 資料夾裡有 MiniFASNetV2.onnx")

    celeba_root, entries = load_manifest(args.split)
    print(f"{args.split}={len(entries)} 筆，模型：{MODEL_PATH}")

    sess = ort.InferenceSession(MODEL_PATH, providers=["CPUExecutionProvider"])
    input_name = sess.get_inputs()[0].name

    all_scores, all_labels = [], []
    skipped = 0
    start = time.time()

    for step, (rel_path, label) in enumerate(entries, start=1):
        img_path = os.path.join(celeba_root, "Data", rel_path)
        base, _ = os.path.splitext(img_path)
        bb_path = base + "_BB.txt"

        if not (os.path.isfile(img_path) and os.path.isfile(bb_path)):
            skipped += 1
            continue

        frame_bgr = cv2.imread(img_path)  # BGR，跟 check_liveness() 收到的攝影機畫面一致
        if frame_bgr is None:
            skipped += 1
            continue

        h, w = frame_bgr.shape[:2]
        try:
            face_box = read_bbox(bb_path, w, h)
            face_input = crop_and_expand(frame_bgr, face_box)
            if face_input is None:
                skipped += 1
                continue

            output = sess.run(None, {input_name: face_input})
            prediction = np.exp(output[0]) / np.sum(np.exp(output[0]))
            attack_score = prediction[0][0]  # index 1 是「真人」分數，見檔頭說明
        except Exception as e:
            print(f"[WARN] {rel_path} 處理失敗，略過: {e}")
            skipped += 1
            continue

        all_scores.append(float(attack_score))
        all_labels.append(label)

        if step % LOG_EVERY_N_STEPS == 0 or step == len(entries):
            elapsed = time.time() - start
            per_sec = step / elapsed if elapsed > 0 else 0.0
            eta_min = (len(entries) - step) / per_sec / 60 if per_sec > 0 else 0.0
            print(f"  {step}/{len(entries)}  {per_sec:.0f} 張/秒  預估還剩 {eta_min:.1f} 分鐘"
                  f"（已略過 {skipped} 張）")

    if skipped:
        print(f"共略過 {skipped} 張（缺檔或讀取失敗），實際評估 {len(all_scores)} 張")

    all_scores = np.array(all_scores)
    all_labels = np.array(all_labels)

    apcer_05, bpcer_05 = compute_apcer_bpcer(all_scores, all_labels, 0.5)
    acer_05 = (apcer_05 + bpcer_05) / 2
    print(f"門檻=0.5 時：APCER={apcer_05:.2%}  BPCER={bpcer_05:.2%}  ACER={acer_05:.2%}")

    fpr, tpr, thresholds = roc_curve(all_labels, all_scores)
    fnr = 1 - tpr
    eer_idx = np.nanargmin(np.absolute(fnr - fpr))
    eer = (fpr[eer_idx] + fnr[eer_idx]) / 2
    eer_threshold = thresholds[eer_idx]
    print(f"EER={eer:.2%}，對應門檻={eer_threshold:.4f}")

    apcer_eer, bpcer_eer = compute_apcer_bpcer(all_scores, all_labels, eer_threshold)
    acer_eer = (apcer_eer + bpcer_eer) / 2
    print(f"EER 門檻下：APCER={apcer_eer:.2%}  BPCER={bpcer_eer:.2%}  ACER={acer_eer:.2%}")

    roc_path = os.path.join(OUTPUT_DIR, "roc_data_minifasnetv2.npz")
    np.savez(roc_path, fpr=fpr, tpr=tpr, thresholds=thresholds)
    print(f"ROC 曲線資料已存至 {roc_path}")
    print("可以拿這份 roc_data_minifasnetv2.npz 跟 roc_data_antispoof_best.npz／"
          "roc_data_antispoof_finetuned_best.npz 一起畫三條 ROC 曲線對照")


if __name__ == "__main__":
    main()

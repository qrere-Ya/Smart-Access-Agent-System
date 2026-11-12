"""
liveness_probe.py：用現場鏡頭量活體分數的工具（蒐集資料、比較模型，不改動正式系統）。

主要功能：
  - 開攝影機，每隔一小段時間取一幀，只有恰好偵測到一張臉才計分；把活體用的人臉裁切（外擴 20%）存成圖，
    同時用兩個模型各算一次真人分數：現成的 MiniFASNetV2、自訓練的 MobileNetV3（檔案在 models/ 才算）
  - 前處理與 vision_core.check_liveness 一致（外擴 20%；MiniFASNetV2 是 80x80 BGR 不正規化、第 1 類是真人；
    MobileNetV3 是 224x224 RGB、ImageNet 正規化、第 0 類是真人）
  - 每幀另記亮度、清晰度（拉普拉斯變異數）、臉框寬度，方便判斷是光線、距離還是模型的問題
  - 結束印出摘要：每個模型的最小／中位數／最大分數、低於門檻的比例，以及依終端機規則
    （連續 3 幀低於 0.80 才拒絕）模擬會被拒絕幾次；輸出 scores.csv 與裁切圖，可帶到另一台電腦離線評估
  - 標籤 genuine（真人）、print（列印照片）、replay（螢幕翻拍）；一次只量一種，每種分別執行

串接模組：
  - vision_core：人臉偵測；shared.paths：預設輸出位置（runtime/liveness_probe/）
  - 資料含人臉，只存在本機，不進版控、不上傳；用前請先關掉門禁終端機（同一支攝影機）
用法：python -m access.liveness_probe --label genuine|print|replay [--seconds 30] [--note 說明] [--out 資料夾]
"""
import argparse
import csv
import datetime
import os
import time

import cv2
import numpy as np

from shared import paths

PAD = 0.2
TERMINAL_THRESHOLD = 0.80      # 與 terminal_app 一致：分數低於它算一次攻擊
TERMINAL_RUN = 3               # 與 terminal_app 一致：連續幾次才拒絕
LOGIN_THRESHOLD = 0.85         # 與 face_login.LIVENESS_MIN 一致
MODELS = (("minifasnet", "MiniFASNetV2.onnx"),
          ("mobilenetv3", "antispoof_mobilenetv3_antispoof_finetuned_best_sim.onnx"))
_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)


def padded_crop(frame, box):
    """人臉框外擴 20% 後裁切；裁不出東西回傳 None（與 vision_core 相同的邊界處理）。"""
    x1, y1, x2, y2 = box
    w, h = x2 - x1, y2 - y1
    x1, y1 = max(0, int(x1 - w * PAD)), max(0, int(y1 - h * PAD))
    x2, y2 = min(frame.shape[1], int(x2 + w * PAD)), min(frame.shape[0], int(y2 + h * PAD))
    crop = frame[y1:y2, x1:x2]
    return crop if crop.size else None


def preprocess(crop, name):
    """依模型做前處理，回傳 (1, 3, H, W) 的 float32。"""
    if name == "mobilenetv3":
        img = cv2.resize(cv2.cvtColor(crop, cv2.COLOR_BGR2RGB), (224, 224)).astype(np.float32) / 255.0
        img = (img - _MEAN) / _STD
    else:
        img = cv2.resize(crop, (80, 80)).astype(np.float32)
    return np.expand_dims(img.transpose(2, 0, 1), axis=0).astype(np.float32)


def real_score(logits, name):
    """softmax 後取真人那一類的機率（MiniFASNetV2 是第 1 類，MobileNetV3 是第 0 類）。"""
    e = np.exp(logits - np.max(logits))
    p = e / np.sum(e)
    return float(p[0][0] if name == "mobilenetv3" else p[0][1])


def reject_runs(scores, threshold=TERMINAL_THRESHOLD, run=TERMINAL_RUN):
    """依終端機的規則模擬：連續 run 幀低於門檻算一次拒絕，回傳拒絕次數（拒絕後計數歸零）。"""
    streak = rejected = 0
    for s in scores:
        streak = streak + 1 if s < threshold else 0
        if streak >= run:
            rejected += 1
            streak = 0
    return rejected


def summarize(scores):
    """一組分數的摘要：張數、最小／中位數／最大、低於兩個門檻的比例、模擬被終端機拒絕的次數。"""
    if not scores:
        return {"n": 0}
    a = np.array(scores)
    return {"n": len(scores), "min": float(a.min()), "median": float(np.median(a)), "max": float(a.max()),
            "below_terminal": float((a < TERMINAL_THRESHOLD).mean()),
            "below_login": float((a < LOGIN_THRESHOLD).mean()), "terminal_rejections": reject_runs(scores)}


def _load_models():
    import onnxruntime as ort
    loaded = {}
    for name, filename in MODELS:
        path = os.path.join(paths.MODELS_DIR, filename)
        if os.path.exists(path):
            loaded[name] = ort.InferenceSession(path, providers=["CPUExecutionProvider"])
    return loaded


def run(label, seconds, note, out_root):
    """量一種標籤：回傳每個模型的分數清單與輸出資料夾。"""
    from access import vision_core as vc

    models = _load_models()
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    out = os.path.join(out_root, f"{label}_{stamp}")
    os.makedirs(out, exist_ok=True)
    vc.open_vision()
    cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
    scores = {name: [] for name in models}
    try:
        with open(os.path.join(out, "scores.csv"), "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["frame", "time", "label", "note", "face_w", "brightness", "sharpness"] + list(models))
            end, n = time.time() + seconds, 0
            while time.time() < end:
                ok, frame = cap.read()
                if not ok:
                    break
                _, detections = vc.face_detect_bgr(frame, vc.detector)
                if len(detections) != 1:
                    continue
                d = detections[0]
                box = (int(d["x1"]), int(d["y1"]), int(d["x2"]), int(d["y2"]))
                crop = padded_crop(frame, box)
                if crop is None:
                    continue
                row = []
                for name, session in models.items():
                    logits = session.run(None, {session.get_inputs()[0].name: preprocess(crop, name)})[0]
                    row.append(real_score(logits, name))
                    scores[name].append(row[-1])
                n += 1
                gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
                cv2.imwrite(os.path.join(out, f"crop_{n:04d}.jpg"), crop, [cv2.IMWRITE_JPEG_QUALITY, 95])
                writer.writerow([n, f"{time.time():.2f}", label, note, box[2] - box[0], f"{gray.mean():.1f}",
                                 f"{cv2.Laplacian(gray, cv2.CV_64F).var():.1f}"] + [f"{s:.4f}" for s in row])
                time.sleep(0.3)
    finally:
        cap.release()
        vc.close_vision()
    return scores, out


def main(argv=None):
    parser = argparse.ArgumentParser(description="用現場鏡頭量活體分數")
    parser.add_argument("--label", required=True, choices=("genuine", "print", "replay"))
    parser.add_argument("--seconds", type=int, default=30)
    parser.add_argument("--note", default="", help="光線、距離、角度等說明，會寫進 scores.csv")
    parser.add_argument("--out", default=os.path.join(paths.RUNTIME_DIR, "liveness_probe"))
    args = parser.parse_args(argv)
    scores, out = run(args.label, args.seconds, args.note, args.out)
    print(f"輸出：{out}")
    for name, values in scores.items():
        s = summarize(values)
        if not s["n"]:
            print(f"[{name}] 沒有量到任何一幀（要正對鏡頭、畫面裡只有一張臉）")
            continue
        print(f"[{name}] {s['n']} 幀，最小 {s['min']:.3f}／中位數 {s['median']:.3f}／最大 {s['max']:.3f}；"
              f"低於 0.80 {s['below_terminal']:.0%}、低於 0.85 {s['below_login']:.0%}；"
              f"依終端機規則會被拒絕 {s['terminal_rejections']} 次")


if __name__ == "__main__":
    main()

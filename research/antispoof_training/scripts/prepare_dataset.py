"""
把蒐集到的原始影片，用專案既有的 RetinaFace 偵測 + 人臉對齊，裁切出標準化的人臉圖片，
分類存成 bona_fide / print_attack / replay_attack 三個資料夾。

【設計決策：為什麼不直接 import vision_core.py】
vision_core.py 在被 import 的當下，模組最上層就會立刻載入 ArcFace（512維特徵）跟
MiniFASNetV2（活體偵測）兩個 ONNX InferenceSession——這是正式系統啟動時需要的東西，
但對「單純裁切訓練資料」這個任務完全用不到，白白拖慢腳本啟動、佔用不必要的記憶體/顯存。

所以這裡把 face_align() 這個純函式（沒有任何副作用、不依賴任何全域模型）直接複製過來，
並且另外開一個獨立的 RetinaFace 偵測器實例，呼叫方式跟 vision_core.py 的
face_detect_bgr() 完全一致。這是刻意的 DRY 妥協：換來訓練腳本不用背正式系統的模型
載入開銷。**如果之後 vision_core.py 的 face_align() 邏輯有修改，這裡要記得手動同步。**

【裁切方式的選擇】沒有用「先偵測 bounding box 再裁切」這種做法，是因為 vision_core.py
本身的偵測輸出裡，bounding box 用的實際 key 名稱是在別的檔案（main_system.py）才組合
出 face_box 參數傳給 check_liveness()，這裡看不到、不確定，貿然猜測容易猜錯。改成直接
用 face_align() 產生的標準 112x112 對齊人臉（這是 vision_core.py 裡唯一「輸入輸出格式
完全確定」的裁切/對齊方式），再 resize 到 224x224 餵給 MobileNetV3——這樣訓練資料的
人臉對齊方式，跟正式系統做人臉辨識時看到的人臉幾何上是一致的，也不用猜任何未知的
資料結構。
"""
import os

import cv2
import numpy as np
from retinaface import RetinaFace
from skimage import transform as trans

# numpy 相容性修補，跟 vision_core.py 同一個修法（見 vision_core.py 開頭的詳細註解）
if not hasattr(np, "int"):
    np.int = int
if not hasattr(np, "float"):
    np.float = float
if not hasattr(np, "bool"):
    np.bool = bool

RAW_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "raw_footage")
OUT_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "dataset")
FRAME_SAMPLE_RATE = 5      # 每 5 幀抽 1 幀，避免同一段影片抽出太多幾乎一樣的畫面
FINAL_SIZE = 224           # MobileNetV3 遷移學習的輸入尺寸

detector = RetinaFace(quality="normal")


def face_align(img_rgb, face_landmarks):
    """跟 vision_core.py 的 face_align() 完全一致（複製過來，見檔頭說明）。"""
    src = np.array([
        [30.2946, 51.6963],
        [65.5318, 51.5014],
        [48.0252, 71.7366],
        [33.5493, 92.3655],
        [62.7299, 92.2041]], dtype=np.float32)

    dst = np.array(face_landmarks, dtype=np.float32).reshape(5, 2)

    tform = trans.SimilarityTransform()
    tform.estimate(dst, src)
    M = tform.params[0:2, :]

    aligned_img = cv2.warpAffine(img_rgb, M, (112, 112), borderValue=0)
    return aligned_img


def crop_faces_from_video(video_path, out_subdir):
    cap = cv2.VideoCapture(video_path)
    frame_idx = 0
    saved = 0
    while True:
        ret, frame_bgr = cap.read()
        if not ret:
            break
        if frame_idx % FRAME_SAMPLE_RATE == 0:
            frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
            detections = detector.predict(frame_rgb)
            for i, face_info in enumerate(detections):
                landmarks = [
                    face_info["left_eye"],
                    face_info["right_eye"],
                    face_info["nose"],
                    face_info["left_lip"],
                    face_info["right_lip"],
                ]
                aligned_112 = face_align(frame_rgb, landmarks)
                final_img = cv2.resize(aligned_112, (FINAL_SIZE, FINAL_SIZE))
                # 存檔用 cv2.imwrite 需要 BGR，aligned_112/final_img 目前是 RGB，轉回來
                final_img_bgr = cv2.cvtColor(final_img, cv2.COLOR_RGB2BGR)

                fname = f"{os.path.splitext(os.path.basename(video_path))[0]}_{frame_idx}_{i}.jpg"
                cv2.imwrite(os.path.join(out_subdir, fname), final_img_bgr)
                saved += 1
        frame_idx += 1
    cap.release()
    return saved


def main():
    categories = ["bona_fide", "print_attack", "replay_attack"]
    for cat in categories:
        raw_cat_dir = os.path.join(RAW_DIR, cat)
        out_cat_dir = os.path.join(OUT_DIR, cat)
        os.makedirs(out_cat_dir, exist_ok=True)
        if not os.path.isdir(raw_cat_dir):
            print(f"⚠️ 找不到 {raw_cat_dir}，先把原始影片放進去這個資料夾再重跑")
            continue
        total = 0
        for fname in os.listdir(raw_cat_dir):
            video_path = os.path.join(raw_cat_dir, fname)
            try:
                total += crop_faces_from_video(video_path, out_cat_dir)
            except Exception as e:
                print(f"⚠️ 處理 {video_path} 時發生錯誤，跳過：{e}")
        print(f"[{cat}] 共裁切出 {total} 張人臉圖片 → {out_cat_dir}")


if __name__ == "__main__":
    main()

import cv2
import numpy as np
import os
import datetime
from resource_manager import ManagedResource, cuda_available

# 【合併專案調整】改用 __file__ 相對路徑，不管從哪個資料夾執行都能正確定位到專案根目錄
_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))

# 【資源規範】YOLOv8s 原本在 import 時載入並永久常駐。改成受中央協調器管理：首次偵測才載入、
# 閒置逾時或被驅逐時釋放；模型走 GPU（YOLO_DEVICE 可覆寫，無 CUDA 時自動退回 CPU）。
_YOLO_PREF = os.environ.get("YOLO_DEVICE", "cuda:0")


class _YoloResource(ManagedResource):
    def __init__(self):
        super().__init__("security.yolov8s", est_ram_mb=500,
                         est_vram_mb=0 if _YOLO_PREF == "cpu" else 800, idle_ttl=60)
        self.device = "cpu"

    def _load(self):
        from ultralytics import YOLO  # 延後 import（會帶入 torch）
        if _YOLO_PREF != "cpu" and not cuda_available():
            print("[security] ⚠️ 找不到可用的 CUDA，YOLO 退回 CPU。")
            self.device = "cpu"
        else:
            self.device = _YOLO_PREF
        return YOLO(os.path.join(_PROJECT_ROOT, 'models', 'yolov8s.pt'))

    def _unload(self, impl):
        try:
            impl.to("cpu")
        except Exception:
            pass


_yolo = _YoloResource()


def release_yolo():
    _yolo.release("explicit")


def count_persons(frame):
    with _yolo.use() as yolo_model:
        results = yolo_model.predict(source=frame, verbose=False, conf=0.6, device=_yolo.device)
    person_boxes = []

    # 取得畫面總面積，用來做比例尺
    H, W = frame.shape[:2]
    frame_area = H * W

    for box in results[0].boxes:
        if int(box.cls[0]) == 0: # 確認是人
            coords = box.xyxy[0].int().tolist()
            x1, y1, x2, y2 = coords

            # 計算這個人的面積
            box_area = (x2 - x1) * (y2 - y1)

            # 【2026-09-12，健檢 P3：修正註解方向】原本這裡寫「小於」，但下面判斷式
            # 其實是「大於」才算——只留下面積大於畫面 10% 的人（面積太小通常是遠景
            # 路過的人，不列入尾隨可疑人數），註解跟程式碼方向相反，容易誤導人改錯。
            # 如果這個人的面積大於整個畫面的 10%
            if box_area > (frame_area * 0.10):
                person_boxes.append(coords)

    return len(person_boxes), person_boxes

def generate_evidence(full_frame, suspicious_box):
    # 取得當下時間作為檔名
    now_str = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    save_path = os.path.join(_PROJECT_ROOT, 'anomaly_logs', f"tailgate_{now_str}.jpg")
    os.makedirs(os.path.dirname(save_path), exist_ok=True)

    H, W = full_frame.shape[:2] # 取得原圖的長寬

    # 安全裁切
    x1 ,y1 ,x2, y2 = suspicious_box
    x1, y1, x2, y2 = max(0, x1), max(0, y1), min(W, x2), min(H, y2)
    cropped_body = full_frame[y1:y2, x1:x2]

    # 擷取特徵
    head_y_end= y1 + (y2 - y1) // 3
    cropped_head = full_frame[y1:head_y_end, x1:x2]

    bottom_H = 400 # 下方區域固定高度
    half_W = W // 2 # 將寬度完美切一半

    # 建立畫布
    canvas = np.zeros((H + bottom_H, W, 3), dtype=np.uint8)

    # 排版拼圖上方全圖
    canvas[0:H, 0:W] = full_frame

    # 左下全身
    body_resized = cv2.resize(cropped_body, (half_W, bottom_H))
    canvas[H:H+bottom_H, 0:half_W] = body_resized

    # 右下頭部特徵
    head_resized = cv2.resize(cropped_head, (W - half_W, bottom_H))
    canvas[H:H+bottom_H, half_W:W] = head_resized

    # 畫一個紅色的框框起來
    cv2.rectangle(canvas, (x1, y1), (x2, y2), (0, 0, 255), 3)

    cv2.imwrite(save_path, canvas)

    return save_path

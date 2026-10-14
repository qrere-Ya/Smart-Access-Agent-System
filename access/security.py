"""
security.py：防尾隨安防模組。

主要功能：
  - 用 YOLOv8s 統計畫面中的人數（只算面積超過畫面 10% 的人，過濾遠景路過者）
  - 偵測到尾隨時，產生證據拼圖（全圖 + 可疑者全身 + 頭部）存到 runtime/anomaly_logs/
  - YOLO 模型首次偵測才載入，閒置逾時或被驅逐時釋放；裝置可用環境變數 YOLO_DEVICE 指定

串接模組：
  - resource_manager：ManagedResource 資源協調
  - terminal_app：每幀分析後呼叫 count_persons / generate_evidence
"""
import cv2
import numpy as np
import os
import datetime
from shared.resource_manager import ManagedResource, cuda_available
from shared import paths

_PROJECT_ROOT = paths.PROJECT_ROOT

# 預設用 GPU，無 CUDA 時自動退回 CPU
_YOLO_PREF = os.environ.get("YOLO_DEVICE", "cuda:0")


class _YoloResource(ManagedResource):
    """YOLOv8s 模型的載入與釋放。"""
    def __init__(self):
        super().__init__("security.yolov8s", est_ram_mb=500,
                         est_vram_mb=0 if _YOLO_PREF == "cpu" else 800, idle_ttl=60)
        self.device = "cpu"

    def _load(self):
        from ultralytics import YOLO  # 延後 import，會帶入 torch
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
    """釋放 YOLO 模型。"""
    _yolo.release("explicit")


def count_persons(frame):
    """偵測畫面中的人，回傳 (人數, 人物框清單)；只計入面積大於畫面 10% 的人。"""
    with _yolo.use() as yolo_model:
        results = yolo_model.predict(source=frame, verbose=False, conf=0.6, device=_yolo.device)
    person_boxes = []

    H, W = frame.shape[:2]
    frame_area = H * W

    for box in results[0].boxes:
        if int(box.cls[0]) == 0: # 類別 0 = 人
            coords = box.xyxy[0].int().tolist()
            x1, y1, x2, y2 = coords

            box_area = (x2 - x1) * (y2 - y1)

            # 面積大於畫面 10% 才算，過小多半是遠景路過的人
            if box_area > (frame_area * 0.10):
                person_boxes.append(coords)

    return len(person_boxes), person_boxes

def generate_evidence(full_frame, suspicious_box):
    """產生尾隨證據拼圖（上：全圖，左下：可疑者全身，右下：頭部）並存檔，回傳檔案路徑。"""
    now_str = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    save_path = os.path.join(paths.ANOMALY_DIR, f"tailgate_{now_str}.jpg")
    os.makedirs(os.path.dirname(save_path), exist_ok=True)

    H, W = full_frame.shape[:2]

    # 裁切範圍限制在畫面內
    x1 ,y1 ,x2, y2 = suspicious_box
    x1, y1, x2, y2 = max(0, x1), max(0, y1), min(W, x2), min(H, y2)
    cropped_body = full_frame[y1:y2, x1:x2]

    # 頭部取人物框上方三分之一
    head_y_end= y1 + (y2 - y1) // 3
    cropped_head = full_frame[y1:head_y_end, x1:x2]

    bottom_H = 400
    half_W = W // 2

    canvas = np.zeros((H + bottom_H, W, 3), dtype=np.uint8)

    canvas[0:H, 0:W] = full_frame

    body_resized = cv2.resize(cropped_body, (half_W, bottom_H))
    canvas[H:H+bottom_H, 0:half_W] = body_resized

    head_resized = cv2.resize(cropped_head, (W - half_W, bottom_H))
    canvas[H:H+bottom_H, half_W:W] = head_resized

    # 在全圖上以紅框標出可疑者
    cv2.rectangle(canvas, (x1, y1), (x2, y2), (0, 0, 255), 3)

    cv2.imwrite(save_path, canvas)

    return save_path

import cv2
import numpy as np
import os
import datetime
from ultralytics import YOLO

# 初始化 YOLO 模型
yolo_model = YOLO("yolov8s.pt")

def count_persons(frame):
    results = yolo_model(frame, verbose=False, conf=0.6)
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

            # 如果這個人的面積小於整個畫面的 10%
            if box_area > (frame_area * 0.10):
                person_boxes.append(coords)

    return len(person_boxes), person_boxes

def generate_evidence(full_frame, suspicious_box):
    # 取得當下時間作為檔名
    now_str = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    save_path = f"../anomaly_logs/tailgate_{now_str}.jpg"
    os.makedirs(os.path.dirname(save_path), exist_ok=True)

    H, W = full_frame.shape[:2] # 取得原圖的長寬

    # 安全裁切
    x1 ,y1 ,x2, y2 = suspicious_box
    x1, y1, x2, y2 = max(0, x1), max(0, y1), min(W, x2), min(H, y2)
    copped_body = full_frame[y1:y2, x1:x2]

    # 擷取特徵
    head_y_end= y1 + (y2 - y1) // 3
    copped_head = full_frame[y1:head_y_end, x1:x2]

    bottom_H = 400 # 下方區域固定高度
    half_W = W // 2 # 將寬度完美切一半

    # 建立畫布
    canvas = np.zeros((H + bottom_H, W, 3), dtype=np.uint8)

    # 排版拼圖上方全圖
    canvas[0:H, 0:W] = full_frame

    # 左下全身
    boby_resized = cv2.resize(copped_body, (half_W, bottom_H))
    canvas[H:H+bottom_H, 0:half_W] = boby_resized

    # 右下頭部特徵
    head_resized = cv2.resize(copped_head, (W - half_W, bottom_H))
    canvas[H:H+bottom_H, half_W:W] = head_resized

    # 畫一個紅色的框框起來
    cv2.rectangle(canvas, (x1, y1), (x2, y2), (0, 0, 255), 3)

    cv2.imwrite(save_path, canvas)

    return save_path
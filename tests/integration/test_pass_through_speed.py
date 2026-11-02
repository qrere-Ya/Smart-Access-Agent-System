"""
門禁每幀都跑 YOLO（經過未打卡偵測）的效能檢查：用真的模型與測試用人臉圖量每幀耗時並印出，低於下限就失敗。
"""
import time

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("require_models")]

MIN_FPS = 5          # 只單獨跑 YOLO 的最低每秒幀數；實際門禁還要加人臉辨識，所以抓寬鬆下限
FRAMES = 30


def test_count_persons_runs_fast_enough_per_frame(face_bgr, capsys):
    import cv2

    from access import security

    frame = cv2.resize(face_bgr, (1280, 720))
    security.count_persons(frame)                      # 暖機：第一次要載入模型
    start = time.perf_counter()
    for _ in range(FRAMES):
        security.count_persons(frame)
    fps = FRAMES / (time.perf_counter() - start)
    security.release_yolo()
    with capsys.disabled():
        print(f"\n[YOLO 每幀偵測] {fps:.1f} 幀/秒（下限 {MIN_FPS}）")
    assert fps >= MIN_FPS, f"YOLO 太慢：{fps:.1f} 幀/秒"

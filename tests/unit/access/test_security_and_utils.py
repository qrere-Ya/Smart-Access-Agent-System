"""
security 與 utils 單元測試：尾隨證據拼圖（尺寸、檔名、框超出畫面時的裁切、紅框）、中文繪字與字型快取。
YOLO 偵測本身需要模型，不在單元測試範圍。
"""
import cv2
import numpy as np
import pytest

from access import security, utils
from shared import paths


@pytest.fixture
def evidence_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "ANOMALY_DIR", str(tmp_path / "anomaly"))
    return tmp_path / "anomaly"


def black_frame(h=480, w=640):
    return np.zeros((h, w, 3), dtype=np.uint8)


def test_evidence_collage_is_saved_with_a_bottom_strip(evidence_dir):
    path = security.generate_evidence(black_frame(), [100, 100, 300, 400])
    saved = cv2.imread(path)
    assert path.startswith(str(evidence_dir)) and saved.shape == (480 + 400, 640, 3)
    name = path.replace("\\", "/").rsplit("/", 1)[-1]
    assert name.startswith("tailgate_") and name.endswith(".jpg")


def test_evidence_marks_the_suspect_with_a_red_box(evidence_dir):
    frame = black_frame()
    saved = cv2.imread(security.generate_evidence(frame, [100, 100, 300, 400]))
    blue, green, red = saved[101, 200]
    assert red > 200 and blue < 80 and green < 80


def test_evidence_box_outside_the_frame_is_clamped(evidence_dir):
    path = security.generate_evidence(black_frame(), [-50, -20, 900, 700])
    assert cv2.imread(path).shape == (880, 640, 3)


def test_evidence_folder_is_created_on_demand(evidence_dir):
    assert not evidence_dir.exists()
    security.generate_evidence(black_frame(), [10, 10, 200, 300])
    assert evidence_dir.is_dir()


def test_releasing_an_unloaded_detector_is_harmless():
    security.release_yolo()


def test_chinese_text_is_drawn_without_touching_the_input_image():
    image = np.zeros((100, 300, 3), dtype=np.uint8)
    drawn = utils.put_chinese_text(image, "ABC 測試", (10, 10), (0, 255, 0), 30)
    assert drawn.shape == image.shape and drawn.dtype == np.uint8
    assert drawn.any() and not image.any()


def test_text_color_is_interpreted_as_bgr():
    image = np.zeros((100, 300, 3), dtype=np.uint8)
    drawn = utils.put_chinese_text(image, "ABC 測試", (10, 10), (0, 255, 0), 30)
    assert drawn[..., 1].max() > 0 and drawn[..., 0].max() == 0 and drawn[..., 2].max() == 0


def test_fonts_are_cached_per_size(monkeypatch):
    monkeypatch.setattr(utils, "_FONT_CACHE", {})
    first = utils._get_font(31)
    assert utils._get_font(31) is first
    assert utils._get_font(32) is not first


def test_missing_chinese_font_falls_back_to_the_default(monkeypatch, capsys):
    original = utils.ImageFont.truetype

    def without_chinese_font(name, *args, **kwargs):
        if name == "msjh.ttc":
            raise OSError("找不到字型")
        return original(name, *args, **kwargs)  # Pillow 載入預設字體時內部也會呼叫 truetype

    monkeypatch.setattr(utils, "_FONT_CACHE", {})
    monkeypatch.setattr(utils.ImageFont, "truetype", without_chinese_font)
    assert utils._get_font(20) is not None
    assert "預設字體" in capsys.readouterr().out

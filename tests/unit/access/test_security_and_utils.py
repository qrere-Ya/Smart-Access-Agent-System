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


# --- 人物框去重（同一個人被框兩次不能算成兩個人） ---

# 真實案例：一個人被 YOLO 框成貼身框（信心 0.90）與幾乎整個畫面的大框（信心 0.61），重疊 0.63
TIGHT_BOX, BIG_BOX = [2, 103, 346, 703], [2, 102, 539, 707]


def test_box_iou_basics():
    assert security.box_iou([0, 0, 10, 10], [0, 0, 10, 10]) == 1.0
    assert security.box_iou([0, 0, 10, 10], [20, 20, 30, 30]) == 0.0
    assert security.box_iou([0, 0, 10, 10], [5, 0, 15, 10]) == pytest.approx(1 / 3)
    assert security.box_iou([0, 0, 0, 0], [0, 0, 0, 0]) == 0.0


def test_a_tight_box_and_a_larger_box_of_the_same_person_count_once():
    assert 0.5 < security.box_iou(TIGHT_BOX, BIG_BOX) < 0.7        # 超過本模組門檻，但躲過 YOLO 內建的 0.7
    kept = security.dedupe_person_boxes([(BIG_BOX, 0.61), (TIGHT_BOX, 0.90)])
    assert kept == [TIGHT_BOX]                                       # 留信心較高的


def test_two_people_side_by_side_still_count_as_two():
    left, right = [0, 100, 300, 700], [320, 120, 620, 700]
    assert len(security.dedupe_person_boxes([(left, 0.9), (right, 0.8)])) == 2


def test_two_people_slightly_overlapping_still_count_as_two():
    front, behind = [100, 100, 400, 700], [330, 140, 600, 700]       # 後面的人被擋住一小部分
    assert security.box_iou(front, behind) < security.PERSON_DUP_IOU
    assert len(security.dedupe_person_boxes([(front, 0.9), (behind, 0.7)])) == 2


def test_dedupe_of_nothing_is_empty():
    assert security.dedupe_person_boxes([]) == []


class _FakeBox:
    def __init__(self, cls, xyxy, conf):
        import types
        self.cls, self.conf = [cls], [conf]
        self.xyxy = [types.SimpleNamespace(int=lambda: types.SimpleNamespace(tolist=lambda: list(xyxy)))]


def test_count_persons_counts_the_duplicate_box_once(monkeypatch):
    import contextlib
    import types

    boxes = [_FakeBox(0, TIGHT_BOX, 0.90), _FakeBox(39, [325, 412, 418, 668], 0.79), _FakeBox(0, BIG_BOX, 0.61)]
    model = types.SimpleNamespace(predict=lambda **kw: [types.SimpleNamespace(boxes=boxes)])
    monkeypatch.setattr(security._yolo, "use", contextlib.contextmanager(lambda: (yield model)))
    count, person_boxes = security.count_persons(np.zeros((720, 540, 3), dtype=np.uint8))
    assert count == 1 and person_boxes == [TIGHT_BOX]               # 瓶子不是人；兩個人物框其實是同一個人

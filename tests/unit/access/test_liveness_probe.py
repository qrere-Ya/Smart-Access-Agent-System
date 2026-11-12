"""liveness_probe 的單元測試：裁切邊界、兩種模型的前處理形狀、分數轉換、依終端機規則模擬拒絕、摘要。"""
import numpy as np
import pytest

from access import liveness_probe as lp


def test_crop_pads_twenty_percent_and_stays_inside_the_frame():
    frame = np.zeros((100, 200, 3), dtype=np.uint8)
    crop = lp.padded_crop(frame, (50, 20, 90, 60))              # 40x40 的臉，每邊外擴 8
    assert crop.shape[:2] == (56, 56)
    edge = lp.padded_crop(frame, (0, 0, 40, 40))                # 貼著邊界：不會超出畫面
    assert edge.shape[:2] == (48, 48)
    assert lp.padded_crop(frame, (300, 300, 340, 340)) is None  # 完全在畫面外：裁不出東西


@pytest.mark.parametrize("name, shape", [("minifasnet", (1, 3, 80, 80)), ("mobilenetv3", (1, 3, 224, 224))])
def test_preprocess_shapes(name, shape):
    crop = np.random.randint(0, 255, (60, 50, 3), dtype=np.uint8)
    out = lp.preprocess(crop, name)
    assert out.shape == shape and out.dtype == np.float32


def test_mobilenet_is_normalised_and_minifas_is_not():
    white = np.full((30, 30, 3), 255, dtype=np.uint8)
    assert lp.preprocess(white, "minifasnet").max() == 255.0                     # 不正規化
    assert lp.preprocess(white, "mobilenetv3").max() < 3.0                       # ImageNet 正規化後


def test_real_score_uses_the_right_class_for_each_model():
    logits = np.array([[5.0, 0.0]])
    assert lp.real_score(logits, "mobilenetv3") > 0.99                           # 第 0 類是真人
    assert lp.real_score(logits, "minifasnet") < 0.01                            # 第 1 類是真人


def test_reject_runs_follow_the_terminal_rule():
    assert lp.reject_runs([0.9, 0.5, 0.5, 0.9, 0.5, 0.5]) == 0                   # 沒有連續 3 幀
    assert lp.reject_runs([0.5, 0.5, 0.5]) == 1
    assert lp.reject_runs([0.5] * 7) == 2                                        # 拒絕後計數歸零，再累積
    assert lp.reject_runs([0.79, 0.8, 0.79, 0.79, 0.79]) == 1                    # 剛好 0.80 不算低於


def test_summary_numbers():
    assert lp.summarize([]) == {"n": 0}
    s = lp.summarize([0.5, 0.7, 0.85, 0.95])
    assert s["n"] == 4 and s["min"] == 0.5 and s["max"] == 0.95
    assert s["below_terminal"] == 0.5 and s["below_login"] == 0.5          # 登入門檻 0.85：剛好 0.85 不算低於
    assert s["terminal_rejections"] == 0

"""
vision_core 單元測試：人臉對齊、活體分數的判讀（兩種模型的前處理與類別編號不同）、特徵向量正規化、
色彩轉換。模型本身用假的 session 取代，不載入任何真實模型。
"""
import contextlib
import types

import numpy as np
import pytest

from access import vision_core as vc

TEMPLATE = np.array([[30.2946, 51.6963], [65.5318, 51.5014], [48.0252, 71.7366],
                     [33.5493, 92.3655], [62.7299, 92.2041]])


class FakeSession:
    """假的 onnx session：記錄收到的輸入，回傳預先設定的輸出。"""

    def __init__(self, output):
        self.output, self.last_inputs = output, None

    def get_inputs(self):
        return [types.SimpleNamespace(name="input")]

    def get_outputs(self):
        return [types.SimpleNamespace(name="output")]

    def run(self, output_names, feed):
        self.last_inputs = feed
        return [self.output]


def use_session(monkeypatch, **sessions):
    @contextlib.contextmanager
    def fake_use():
        yield types.SimpleNamespace(**sessions)

    monkeypatch.setattr(vc._engine, "use", fake_use)


def gradient_image(size=112):
    row = np.linspace(0, 255, size, dtype=np.uint8)
    return np.stack([np.tile(row, (size, 1))] * 3, axis=-1)


def test_aligning_with_the_template_points_is_the_identity():
    image = gradient_image()
    aligned = vc.face_align(image, TEMPLATE.tolist())
    assert aligned.shape == (112, 112, 3) and aligned.dtype == np.uint8
    assert np.abs(aligned.astype(int) - image.astype(int)).max() <= 1


def test_aligning_a_larger_face_scales_it_to_112():
    aligned = vc.face_align(np.zeros((224, 224, 3), dtype=np.uint8), (TEMPLATE * 2).tolist())
    assert aligned.shape == (112, 112, 3)


def test_color_conversion_swaps_channels_and_hands_rgb_to_the_detector():
    seen = {}
    detector = types.SimpleNamespace(predict=lambda rgb: seen.setdefault("rgb", rgb) is not None and ["臉"])
    bgr = np.zeros((2, 2, 3), dtype=np.uint8)
    bgr[0, 0] = (1, 2, 3)
    rgb, detections = vc.face_detect_bgr(bgr, detector)
    assert tuple(rgb[0, 0]) == (3, 2, 1) and detections == ["臉"] and tuple(seen["rgb"][0, 0]) == (3, 2, 1)


def test_cuda_provider_options_limit_the_gpu_memory():
    name, options = vc._cuda_ep(256)
    assert name == "CUDAExecutionProvider"
    assert options == {"gpu_mem_limit": 256 * 1024 * 1024, "arena_extend_strategy": "kSameAsRequested"}


def test_face_outside_the_frame_gives_up_before_using_any_model(monkeypatch):
    def boom():
        raise AssertionError("不該載入模型")

    monkeypatch.setattr(vc._engine, "use", boom)
    assert vc.check_liveness(np.zeros((100, 100, 3), dtype=np.uint8), (200, 200, 300, 300)) == -1.0


def test_liveness_score_for_the_default_model_is_the_second_class(monkeypatch):
    session = FakeSession(np.array([[0.0, 2.0]]))
    use_session(monkeypatch, liveness=session)
    monkeypatch.setattr(vc, "_USE_MOBILENETV3_PREPROCESS", False)
    score = vc.check_liveness(np.full((200, 200, 3), 128, dtype=np.uint8), (50, 50, 150, 150))
    assert score == pytest.approx(0.8808, abs=1e-4)
    assert session.last_inputs["input"].shape == (1, 3, 80, 80)


def test_liveness_score_for_the_trained_model_is_the_first_class(monkeypatch):
    """自訓練模型的類別編號與預設模型相反（0 是真人），混用會讓真人被判成攻擊。"""
    session = FakeSession(np.array([[2.0, 0.0]]))
    use_session(monkeypatch, liveness=session)
    monkeypatch.setattr(vc, "_USE_MOBILENETV3_PREPROCESS", True)
    score = vc.check_liveness(np.full((200, 200, 3), 128, dtype=np.uint8), (50, 50, 150, 150))
    assert score == pytest.approx(0.8808, abs=1e-4)
    batch = session.last_inputs["input"]
    assert batch.shape == (1, 3, 224, 224) and batch.dtype == np.float32
    assert abs(float(batch.mean())) < 3  # 已做 ImageNet 標準化，數值不再是 0~255


def test_liveness_returns_minus_one_when_the_model_fails(monkeypatch, capsys):
    @contextlib.contextmanager
    def broken():
        raise RuntimeError("模型壞了")
        yield

    monkeypatch.setattr(vc._engine, "use", broken)
    assert vc.check_liveness(np.zeros((200, 200, 3), dtype=np.uint8), (50, 50, 150, 150)) == -1.0
    assert "WARN" in capsys.readouterr().out


def test_face_box_is_expanded_by_twenty_percent_and_clamped(monkeypatch):
    session = FakeSession(np.array([[0.0, 0.0]]))
    use_session(monkeypatch, liveness=session)
    monkeypatch.setattr(vc, "_USE_MOBILENETV3_PREPROCESS", False)
    frame = np.zeros((100, 100, 3), dtype=np.uint8)
    frame[:, :] = (10, 20, 30)
    assert vc.check_liveness(frame, (0, 0, 100, 100)) == pytest.approx(0.5)
    assert session.last_inputs["input"].shape == (1, 3, 80, 80)


def test_feature_vector_is_unit_length_and_uses_float32_input(monkeypatch):
    session = FakeSession(np.array([[3.0, 4.0]]))
    use_session(monkeypatch, arcface=session)
    embedding = vc.feature_extract(np.random.default_rng(0).integers(0, 255, (112, 112, 3), dtype=np.uint8))
    assert embedding == pytest.approx([0.6, 0.8])
    blob = session.last_inputs["input"]
    assert blob.shape == (1, 3, 112, 112) and blob.dtype == np.float32


def test_engine_estimates_depend_on_cuda_availability():
    assert vc._engine.est_ram_mb == 1500
    assert vc._engine.est_vram_mb in (0, 1000)

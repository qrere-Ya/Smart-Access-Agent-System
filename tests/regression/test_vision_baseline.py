"""
視覺模型回歸測試：固定一張測試圖，ArcFace 特徵、活體分數與人臉框要和基準檔（baseline.json）一致，
換 Python、換套件或換執行裝置（GPU/CPU）之後辨識結果不能悄悄變動。

有意更新模型後，用 `UPDATE_BASELINE=1 python -m pytest -m regression` 重新產生基準檔，並在 commit 說明原因。
"""
import json
import os

import numpy as np
import pytest

BASELINE = os.path.join(os.path.dirname(__file__), "baseline.json")

pytestmark = [pytest.mark.regression, pytest.mark.usefixtures("require_models")]


@pytest.fixture(scope="module")
def current(face_bgr):
    from access import vision_core as vc

    rgb, dets = vc.face_detect_bgr(face_bgr, vc.detector)
    f = dets[0]
    box = [int(f[k]) for k in ("x1", "y1", "x2", "y2")]
    aligned = vc.face_align(rgb, [f["left_eye"], f["right_eye"], f["nose"], f["left_lip"], f["right_lip"]])
    result = {
        "model": vc._liveness_model_name,
        "faces": len(dets),
        "box": box,
        "liveness": round(float(vc.check_liveness(face_bgr, tuple(box))), 5),
        "embedding": [round(float(x), 6) for x in vc.feature_extract(aligned)],
        "aligned": aligned,
    }
    yield result
    vc.close_vision()


@pytest.fixture(scope="module")
def baseline(current):
    if os.environ.get("UPDATE_BASELINE") == "1":
        saved = {k: v for k, v in current.items() if k != "aligned"}
        with open(BASELINE, "w", encoding="utf-8") as fh:
            json.dump(saved, fh, ensure_ascii=False)
    with open(BASELINE, encoding="utf-8") as fh:
        return json.load(fh)


def test_same_liveness_model_as_the_baseline(current, baseline):
    assert current["model"] == baseline["model"]


def test_face_detection_matches_the_baseline(current, baseline):
    assert current["faces"] == baseline["faces"]
    assert max(abs(a - b) for a, b in zip(current["box"], baseline["box"], strict=True)) <= 3


def test_embedding_matches_the_baseline(current, baseline):
    a, b = np.array(current["embedding"]), np.array(baseline["embedding"])
    assert float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b))) > 0.999


def test_liveness_score_matches_the_baseline(current, baseline):
    assert abs(current["liveness"] - baseline["liveness"]) < 0.02


def test_gpu_and_cpu_give_the_same_embedding(current):
    import onnxruntime as ort
    from sklearn.preprocessing import normalize

    from access import vision_core as vc

    session = ort.InferenceSession(vc.onnx_path, providers=["CPUExecutionProvider"])
    blob = np.expand_dims(np.transpose(current["aligned"], (2, 0, 1)).astype(np.float32), 0)
    cpu = normalize(session.run(None, {session.get_inputs()[0].name: blob})[0]).flatten()
    assert float(np.dot(cpu, np.array(current["embedding"]))) > 0.9999

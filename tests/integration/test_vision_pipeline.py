"""
視覺流程整合測試：RetinaFace 偵測、對齊、ArcFace 特徵、活體分數、YOLO 人數與證據照片，用真的模型跑。
"""
import os

import numpy as np
import pytest

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("require_models")]


@pytest.fixture(scope="module")
def vision(face_bgr):
    from access import vision_core as vc

    rgb, dets = vc.face_detect_bgr(face_bgr, vc.detector)
    assert dets, "測試圖偵測不到人臉"
    f = dets[0]
    box = tuple(int(f[k]) for k in ("x1", "y1", "x2", "y2"))
    landmarks = [f["left_eye"], f["right_eye"], f["nose"], f["left_lip"], f["right_lip"]]
    aligned = vc.face_align(rgb, landmarks)
    result = {
        "vc": vc, "dets": dets, "box": box, "aligned": aligned,
        "embedding": vc.feature_extract(aligned), "liveness": float(vc.check_liveness(face_bgr, box)),
    }
    yield result
    vc.close_vision()


def test_detects_exactly_one_face(vision):
    assert len(vision["dets"]) == 1


def test_alignment_gives_a_112_square(vision):
    assert vision["aligned"].shape == (112, 112, 3)


def test_embedding_is_a_unit_512_vector(vision):
    emb = vision["embedding"]
    assert emb.shape == (512,)
    assert abs(float(np.linalg.norm(emb)) - 1.0) < 1e-4


def test_same_face_gives_the_same_embedding(vision):
    again = vision["vc"].feature_extract(vision["aligned"])
    assert float(np.dot(again, vision["embedding"])) > 0.9999


def test_liveness_is_a_probability(vision):
    assert 0.0 <= vision["liveness"] <= 1.0


def test_an_unusable_crop_is_rejected_not_crashed(face_bgr):
    from access import vision_core as vc

    assert vc.check_liveness(face_bgr, (0, 0, 0, 0)) == -1.0


def test_models_run_on_the_gpu_when_one_is_available(vision):
    import onnxruntime as ort

    if "CUDAExecutionProvider" not in ort.get_available_providers():
        pytest.skip("這台電腦沒有 CUDA 版 onnxruntime")
    impl = vision["vc"]._engine._impl
    assert impl.arcface.get_providers()[0] == "CUDAExecutionProvider"
    assert impl.liveness.get_providers()[0] == "CUDAExecutionProvider"


def test_registered_face_is_recognised_end_to_end(vision, db):
    from shared import database_mgr as dbm

    ok, _ = dbm.register_user("測試員", "M", "T001", vision["embedding"])
    assert ok
    name, distance = dbm.recognize_face(vision["embedding"])
    assert name == "測試員" and distance < 0.01


def test_tailgate_evidence_is_written_into_the_runtime_folder(face_bgr):
    from access import security
    from shared import paths

    count, boxes = security.count_persons(face_bgr)
    path = security.generate_evidence(face_bgr, boxes[0] if boxes else [10, 10, 100, 100])
    security.release_yolo()
    assert count >= 1
    assert path.startswith(paths.ANOMALY_DIR) and os.path.exists(path)

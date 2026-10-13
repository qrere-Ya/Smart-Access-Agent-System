"""
vision_core.py：門禁系統的視覺核心。

主要功能：
  - RetinaFace 人臉偵測與 5 點關鍵點
  - 人臉對齊成 112x112，ArcFace R100 萃取 512 維特徵
  - 活體偵測（預設 MiniFASNetV2，可用環境變數 ANTISPOOF_MODEL_PATH 切換成自訓練模型）
  - 模型由 VisionEngine 統一載入，首次推論才載入，閒置逾時或被驅逐時釋放

串接模組：
  - resource_manager：ManagedResource 資源協調
  - terminal_app：現場即時辨識
  - web_server：手機遠端註冊
"""
import os
import sys
import cv2
import numpy as np
# 補回新版 numpy 移除的別名，讓舊版 retinaface/tensorflow 能執行（已有的屬性不覆蓋）
if not hasattr(np, "int"):
    np.int = int
if not hasattr(np, "float"):
    np.float = float
if not hasattr(np, "bool"):
    np.bool = bool
from skimage import transform as trans
import onnxruntime as ort
from sklearn.preprocessing import normalize
from types import SimpleNamespace
from contextlib import contextmanager
from resource_manager import ManagedResource

_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))

img_path = os.path.join(_PROJECT_ROOT, 'images', 'simon.jpg')
onnx_path = os.path.join(_PROJECT_ROOT, 'models', 'arcface_r100_v1.onnx')

# 活體模型檔名，預設 MiniFASNetV2.onnx，可用環境變數 ANTISPOOF_MODEL_PATH 指定 models/ 底下其他檔名
_liveness_model_name = os.environ.get('ANTISPOOF_MODEL_PATH', 'MiniFASNetV2.onnx')
liveness_model_path = os.path.join(_PROJECT_ROOT, 'models', _liveness_model_name)

def _cuda_ep(limit_mb):
    # 建立 CUDA provider 設定：限制顯存上限、不預先囤積
    return ('CUDAExecutionProvider', {"gpu_mem_limit": limit_mb * 1024 * 1024,
                                      "arena_extend_strategy": "kSameAsRequested"})


if "CUDAExecutionProvider" not in ort.get_available_providers():
    print("[vision_core] ⚠️ onnxruntime 沒有 CUDA 支援（目前裝的是 CPU 版），ArcFace/活體模型會用 CPU。"
          "要走 GPU：pip uninstall onnxruntime && pip install onnxruntime-gpu（需對應的 CUDA/cuDNN）。")


class VisionEngine(ManagedResource):
    """人臉偵測、ArcFace、活體三個模型的統一載入與釋放。"""
    def __init__(self):
        super().__init__("vision.face-pipeline", est_ram_mb=1500,
                         est_vram_mb=1000 if "CUDAExecutionProvider" in ort.get_available_providers() else 0,
                         idle_ttl=30)

    def _load(self):
        from retinaface import RetinaFace  # 延後 import，會帶入 TensorFlow
        return SimpleNamespace(
            detector=RetinaFace(quality='normal'),
            # RetinaFace 走 TensorFlow，Windows 原生版只能用 CPU
            liveness=ort.InferenceSession(liveness_model_path, providers=[_cuda_ep(256), 'CPUExecutionProvider']),
            arcface=ort.InferenceSession(onnx_path, providers=[_cuda_ep(768), 'CPUExecutionProvider']),
        )

    def _unload(self, impl):
        impl.detector = impl.liveness = impl.arcface = None
        tf = sys.modules.get("tensorflow")
        if tf is not None:
            try:
                tf.keras.backend.clear_session()
            except Exception:
                pass


_engine = VisionEngine()


class _DetectorHandle:
    """相容舊介面 vc.detector：不持有模型，每次 predict() 才向引擎借用。"""
    def predict(self, img_rgb):
        with _engine.use() as m:
            return m.detector.predict(img_rgb)


detector = _DetectorHandle()


def open_vision():
    """長時間使用（如相機迴圈）前呼叫：預先載入並釘住，不受閒置回收。"""
    _engine.open()


def close_vision():
    """結束長時間使用：無條件釋放模型。"""
    _engine.close()


def vision_session():
    """context manager 版本：with vc.vision_session(): ... 離開時釋放。"""
    return _engine.session()


# 自訓練 MobileNetV3 活體模型（檔名前綴 antispoof_mobilenetv3_）的前處理與輸出
# 跟 MiniFASNetV2 不同：輸入 224x224 RGB 並做 ImageNet 標準化，類別 0 才是真人
_USE_MOBILENETV3_PREPROCESS = _liveness_model_name.startswith('antispoof_mobilenetv3_')
_IMAGENET_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
_IMAGENET_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)

# 啟動時印出實際使用的活體模型
print(f"[vision_core] 活體偵測模型：{_liveness_model_name}"
      f"（{'MobileNetV3 前處理' if _USE_MOBILENETV3_PREPROCESS else 'MiniFASNetV2 前處理'}）")

"""
# 讀取圖檔，因為 opencv 預設是 bgr 因此要轉成 rgb
def face_detect(img_path, detector):
    img_bgr = cv2.imread(img_path, cv2.IMREAD_COLOR)
    img_rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
    detections = detector.predict(img_rgb)

    return img_rgb, detections
"""

def face_detect_bgr(img_bgr, detector):
    """BGR 影像轉 RGB 後偵測人臉，回傳 (RGB 影像, 偵測結果)。"""
    img_rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
    detections = detector.predict(img_rgb)

    return img_rgb, detections

def face_align(img_rgb, face_landmarks):
    """依 5 個關鍵點把人臉轉正並裁成 112x112。"""
    # 標準臉的 5 個點座標
    src = np.array([
    [30.2946, 51.6963],
    [65.5318, 51.5014],
    [48.0252, 71.7366],
    [33.5493, 92.3655],
    [62.7299, 92.2041]], dtype=np.float32)

    dst = np.array(face_landmarks, dtype=np.float32).reshape(5,2)

    tform = trans.SimilarityTransform()
    tform.estimate(dst, src)  # 求出把偵測到的臉轉成標準臉的轉換

    M = tform.params[0:2, :]

    aligned_img = cv2.warpAffine(img_rgb, M, (112,112), borderValue=0)

    return aligned_img

def check_liveness(frame, face_box):
    """活體偵測：回傳真人分數（0~1），人臉不完整或發生例外時回傳 -1.0。"""
    try:
        x1, y1, x2, y2 = face_box
        w, h = x2 - x1, y2 - y1
        # 人臉框向外擴 20%
        x1 = max(0, int(x1 - w * 0.2))
        y1 = max(0, int(y1 - h * 0.2))
        x2 = min(frame.shape[1], int(x2 + w * 0.2))
        y2 = min(frame.shape[0], int(y2 + h * 0.2))

        face_img = frame[y1:y2, x1:x2]

        # 人臉在畫面外導致裁切為空，放棄判定
        if face_img.size == 0:
            return -1.0

        if _USE_MOBILENETV3_PREPROCESS:
            face_img = cv2.cvtColor(face_img, cv2.COLOR_BGR2RGB)
            face_img = cv2.resize(face_img, (224, 224))
            face_img = face_img.astype(np.float32) / 255.0
            face_img = (face_img - _IMAGENET_MEAN) / _IMAGENET_STD
            face_img = np.expand_dims(face_img.transpose(2, 0, 1), axis=0).astype(np.float32)
        else:
            face_img = cv2.resize(face_img, (80, 80))
            face_img = face_img.astype(np.float32)
            face_img = np.expand_dims(face_img.transpose(2, 0, 1), axis=0)

        with _engine.use() as m:
            input_name = m.liveness.get_inputs()[0].name
            output = m.liveness.run(None, {input_name: face_img})

        prediction = np.exp(output[0]) / np.sum(np.exp(output[0]))
        if _USE_MOBILENETV3_PREPROCESS:
            real_score = prediction[0][0]  # 類別 0 = 真人
        else:
            real_score = prediction[0][1]  # 類別 1 = 真人

        return real_score
    except Exception as e:
        print(f"[vision_core][WARN] check_liveness 發生例外，判定為無效: {e}")
        return -1.0

def feature_extract(aligned_img):
    """對齊後的人臉用 ArcFace 萃取 512 維正規化特徵。"""
    t_aligned = np.transpose(aligned_img, (2,0,1))
    inputs = t_aligned.astype(np.float32)
    input_blob = np.expand_dims(inputs, axis=0)

    with _engine.use() as m:
        input_name = m.arcface.get_inputs()[0].name
        output_name = m.arcface.get_outputs()[0].name

        prediction = m.arcface.run([output_name], {input_name: input_blob})[0]
    final_embedding = normalize(prediction).flatten()

    return final_embedding

# --- 自我測試：直接執行本檔時跑一次偵測、對齊、特徵萃取 ---
if __name__ == "__main__":
    print("啟動 AI 視覺核心測試...")

    test_img = img_path

    test_img_bgr = cv2.imread(test_img)
    if test_img_bgr is None:
        print(f"❌ 讀取測試圖片失敗，請確認檔案存在: {test_img}")
    else:
        rgb_img, detections = face_detect_bgr(test_img_bgr, detector)
        print(f"1. 成功抓取人臉數量: {len(detections)}")

        if len(detections) > 0:
            face_info = detections[0]

            # 左眼、右眼、鼻子、左嘴角、右嘴角
            landmarks = [
                face_info['left_eye'],
                face_info['right_eye'],
                face_info['nose'],
                face_info['left_lip'],
                face_info['right_lip']
            ]

            aligned_face = face_align(rgb_img, landmarks)
            print("2. 成功完成人臉對齊 (112x112)")

            face_dna = feature_extract(aligned_face)
            print("3. 成功萃取特徵碼！前 5 個數字是:", face_dna[:5])
            print("特徵碼總長度:", len(face_dna))

        else:
            print("這張圖片裡沒有臉喔！")

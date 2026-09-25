import os
import sys
import cv2
import numpy as np
# 【2026-09-06，修正人臉辨識啟動崩潰】這三行是為了讓年代較舊的 retinaface/tensorflow
# 內部程式碼能用到 numpy 早期版本才有的 np.int / np.float / np.bool 這幾個「別名」
# （新版 numpy 已經拿掉 np.int、np.float 這兩個別名，一定要補回來，不然 retinaface 會
# 直接因為找不到 np.int 而壞掉）。
#
# 但 requirements.txt 現在鎖定的是 numpy==2.2.6——這個版本其實已經自己把 np.bool
# 「復原」成一個真正的 numpy 型別（等於 np.bool_，不是 Python 內建的 bool）。原本這裡
# 不管三七二十一直接寫死 `np.bool = bool`，會把 numpy 自己剛復原好的型別，整個蓋成
# Python 內建的 bool，而 numpy 內部有些模組（例如 numpy.ma，這次崩潰的根源）建立遮罩
# 陣列時，本來預期拿到的是「真正的 numpy 型別」，結果被我們蓋成普通 bool 之後，程式
# 內部呼叫 `.view()` 這個「只有 numpy 陣列/型別才有」的方法就直接壞掉，噴出
# `AttributeError: 'bool' object has no attribute 'view'`——這正是你這次遇到的錯誤，
# 已經在沙盒環境用同樣的 numpy 2.2.6 系列版本重現過，確認就是這一行造成的。
#
# 修正方式：改成「只有在 numpy 真的沒有這個屬性時才補上去」，不要不分青紅皂白蓋掉
# numpy 自己已經有、而且是正確型別的屬性。這樣不管以後 numpy 版本再怎麼變（復原更多
# 別名、或又拿掉更多別名），這幾行都只會補「真的缺少」的部分，不會誤傷 numpy 自己
# 內部依賴的型別。
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

# 【合併專案調整】改用 __file__ 相對路徑，不管從哪個資料夾執行都能正確定位到專案根目錄
_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))

img_path = os.path.join(_PROJECT_ROOT, 'images', 'simon.jpg')
onnx_path = os.path.join(_PROJECT_ROOT, 'models', 'arcface_r100_v1.onnx')

# 【2026-09-18，支援切換活體偵測模型做測試】原本這裡是寫死的字串
# 'MiniFASNetV2.onnx'——單純把新的 .onnx 檔案複製進 models/ 資料夾，不會有
# 任何效果，因為這裡完全沒有去掃描資料夾、只會照著寫死的檔名去讀這一個檔案。
# 改成預設值仍然是 MiniFASNetV2.onnx（不改變現有系統行為、demo 用的是已驗證
# 過的模型），但可以用環境變數 ANTISPOOF_MODEL_PATH 指定 models/ 資料夾底下
# 另一個檔名，明確切換成要測試的模型，例如你自己訓練的
# antispoof_mobilenetv3_antispoof_finetuned_best_sim.onnx：
#
#   PowerShell：
#     $env:ANTISPOOF_MODEL_PATH = "antispoof_mobilenetv3_antispoof_finetuned_best_sim.onnx"
#     python terminal_app.py
#
# 測試完成後把這個環境變數關掉（例如關閉該 PowerShell 視窗，或執行
# `Remove-Item Env:ANTISPOOF_MODEL_PATH`），下次啟動就會自動回到預設的
# MiniFASNetV2.onnx，不會不小心把正式demo用的模型換掉。
_liveness_model_name = os.environ.get('ANTISPOOF_MODEL_PATH', 'MiniFASNetV2.onnx')
liveness_model_path = os.path.join(_PROJECT_ROOT, 'models', _liveness_model_name)

# ==========================================
# 【資源規範】RetinaFace(TensorFlow) + ArcFace + 活體模型 原本在 import 時就全部載入、
# 永久常駐（terminal_app 與 web_server 兩個行程各一份）。改成受中央協調器管理的
# VisionEngine：Initialized 時不載入，首次推論才 Acquired，閒置逾時/被驅逐時釋放。
# ==========================================
def _cuda_ep(limit_mb):
    # 模型一律走 GPU；每個 session 限制顯存上限、且不預先囤積（kSameAsRequested）
    return ('CUDAExecutionProvider', {"gpu_mem_limit": limit_mb * 1024 * 1024,
                                      "arena_extend_strategy": "kSameAsRequested"})


if "CUDAExecutionProvider" not in ort.get_available_providers():
    print("[vision_core] ⚠️ onnxruntime 沒有 CUDA 支援（目前裝的是 CPU 版），ArcFace/活體模型會用 CPU。"
          "要走 GPU：pip uninstall onnxruntime && pip install onnxruntime-gpu（需對應的 CUDA/cuDNN）。")


class VisionEngine(ManagedResource):
    def __init__(self):
        super().__init__("vision.face-pipeline", est_ram_mb=1500,
                         est_vram_mb=1000 if "CUDAExecutionProvider" in ort.get_available_providers() else 0,
                         idle_ttl=30)

    def _load(self):
        from retinaface import RetinaFace  # 延後到真正需要時才 import（會帶入 TensorFlow）
        return SimpleNamespace(
            detector=RetinaFace(quality='normal'),
            # RetinaFace 走 TensorFlow：TF 2.11+ 的 Windows 原生版沒有 GPU 支援，只能 CPU（無法改）。
            liveness=ort.InferenceSession(liveness_model_path, providers=[_cuda_ep(256), 'CPUExecutionProvider']),
            arcface=ort.InferenceSession(onnx_path, providers=[_cuda_ep(768), 'CPUExecutionProvider']),
        )

    def _unload(self, impl):
        impl.detector = impl.liveness = impl.arcface = None  # 解除指標
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
    """長時間使用（如相機迴圈）前呼叫：預先載入並釘住，不受閒置回收，但仍可被協調器驅逐。"""
    _engine.open()


def close_vision():
    """結束長時間使用：無條件釋放模型。"""
    _engine.close()


def vision_session():
    """context manager 版本：with vc.vision_session(): ...  離開時釋放。"""
    return _engine.session()


# 【2026-09-18，修正切換到自訓練 MobileNetV3 活體模型後「人臉辨識完全失效」的
# 真正原因】research/antispoof_training/scripts/export_onnx.py 匯出的檔案，檔名
# 固定是 antispoof_mobilenetv3_<checkpoint 名稱>[_sim].onnx，用這個前綴判斷是不是
# 自訓練的 MobileNetV3 系列模型（凍結骨幹版／解凍微調版都算），需要跟 MiniFASNetV2
# 不同的前處理和輸出判讀方式，兩者對不上是造成失效的真正原因：
#
# 1）前處理不同：train_antispoof.py / finetune_unfreeze.py / evaluate_antispoof.py
#    三支腳本用的是完全一致的 transforms.Compose([Resize((224,224)), ToTensor(),
#    Normalize(imagenet mean/std)])，輸入是 224x224、RGB、先除以 255 縮放到
#    [0,1]、再用 ImageNet 的 mean/std 標準化。MiniFASNetV2 這裡原本的寫法是
#    80x80、BGR、原始 0-255 像素值、完全不標準化——如果直接把新模型的
#    liveness_model_path 換掉、但前處理沒有跟著換，MobileNetV3 的
#    AdaptiveAvgPool2d 不會因為輸入尺寸不對而丟例外（所以不會在終端機看到任何
#    錯誤訊息），但吃到完全不對的數值分布，輸出會是沒有意義的雜訊分數。
#
# 2）類別編號相反：celeba_spoof_dataset.py 的 class_to_idx 是
#    {"bona_fide": 0, "attack": 1}——index 0 才是「真人」，index 1 是「攻擊」，
#    跟 MiniFASNetV2 原本 `prediction[0][1]` = 真人分數的假設剛好相反。兩個問題
#    疊加的結果：真人被系統當成攻擊分數偏高、判定為假臉，畫面上完全辨識不出
#    任何一張真實的臉，且不會有任何錯誤訊息——這正是你在會議上遇到的「人臉辨識
#    無法辨識」。
_USE_MOBILENETV3_PREPROCESS = _liveness_model_name.startswith('antispoof_mobilenetv3_')
_IMAGENET_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
_IMAGENET_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)

# 【2026-09-18，啟動時印出目前實際載入哪個模型】方便測試時直接在終端機確認
# ANTISPOOF_MODEL_PATH 環境變數有沒有生效，不用憑印象猜。
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

# 辯識影片會用到
def face_detect_bgr(img_bgr, detector):
    img_rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
    detections = detector.predict(img_rgb)

    return img_rgb, detections

# 定義 臉型 的 5 個點座標
def face_align(img_rgb, face_landmarks):
    src = np.array([
    [30.2946, 51.6963],
    [65.5318, 51.5014],
    [48.0252, 71.7366],
    [33.5493, 92.3655],
    [62.7299, 92.2041]], dtype=np.float32)

    # 將取的的face_landmarks 轉置 => src 矩陣一樣
    dst = np.array(face_landmarks, dtype=np.float32).reshape(5,2)

    tform = trans.SimilarityTransform() # 建立一個轉換器 tform
    tform.estimate(dst, src) # 讓 tform 學習如何把你的臉 (dst) 扭轉成標準臉 (src)

    M = tform.params[0:2, :] # 取得轉換矩陣

    aligned_img = cv2.warpAffine(img_rgb, M, (112,112), borderValue=0)

    return aligned_img

def check_liveness(frame, face_box):
    """
    臉部活體偵測 (Anti-Spoofing)
    """
    try:
        x1, y1, x2, y2 = face_box
        w, h = x2 - x1, y2 - y1
        x1 = max(0, int(x1 - w * 0.2))
        y1 = max(0, int(y1 - h * 0.2))
        x2 = min(frame.shape[1], int(x2 + w * 0.2))
        y2 = min(frame.shape[0], int(y2 + h * 0.2))

        face_img = frame[y1:y2, x1:x2]

        # 如果人臉已經跑到畫面外，導致裁切出來是空的，回傳 -1.0 放棄判定
        if face_img.size == 0:
            return -1.0

        if _USE_MOBILENETV3_PREPROCESS:
            # 對應 train_antispoof.py / finetune_unfreeze.py / evaluate_antispoof.py
            # 三支腳本裡完全一致的 transforms.Compose：224x224、RGB、
            # 除以 255 縮放到 [0,1]、再用 ImageNet 的 mean/std 標準化。
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
            # celeba_spoof_dataset.py 的 class_to_idx = {"bona_fide": 0, "attack": 1}，
            # 跟 MiniFASNetV2 的 index 1 = 真人剛好相反，這裡一定要對應各自訓練時
            # 的類別編號，不能沿用同一個 index。
            real_score = prediction[0][0]
        else:
            real_score = prediction[0][1]

        return real_score
    except Exception as e:
        # 【2026-09-12，健檢 P3：移除 bare except】原本用 except: 把所有例外都吃掉，
        # 出錯的時候完全不知道是相機沒訊號、模型壞了、還是別的原因。改成
        # except Exception 明確只接「正常執行期間可能發生」的例外（不會誤吃像
        # KeyboardInterrupt、SystemExit 這種本來就不該被吞掉的系統層級信號），
        # 並且印出實際錯誤內容方便之後除錯；行為上還是回傳 -1.0（無效判定），
        # 不影響呼叫端既有邏輯。
        print(f"[vision_core][WARN] check_liveness 發生例外，判定為無效: {e}")
        return -1.0

def feature_extract(aligned_img):
    t_aligned = np.transpose(aligned_img, (2,0,1)) # 將 aligned_img 轉換維度順序
    inputs = t_aligned.astype(np.float32) # 將矩陣的資料型態轉換為float32
    input_blob = np.expand_dims(inputs, axis=0) # 在第 0 個位置增加一個維度

    # 取得 ONNX 的輸入與輸出名稱
    with _engine.use() as m:
        input_name = m.arcface.get_inputs()[0].name
        output_name = m.arcface.get_outputs()[0].name

        # 執行推理 (Inference)
        prediction = m.arcface.run([output_name], {input_name: input_blob})[0]
    final_embedding = normalize(prediction).flatten()

    return final_embedding

# ==========================================
# 測試區塊：確保整條生產線能正常運作
# ==========================================
if __name__ == "__main__":
    print("啟動 AI 視覺核心測試...")

    # 測試圖檔路徑 (確認你有這張圖)
    test_img = img_path

    # 【2026-09-12，健檢 P3：修好殭屍測試碼】face_detect_bgr() 吃的是「圖片陣列」
    # (cv2.cvtColor 需要 numpy 陣列)，不是檔案路徑字串。這裡原本直接把路徑字串
    # test_img 傳進去，合併專案之後只要執行這段測試就會直接報錯——代表這段自我
    # 測試已經壞掉很久、沒人真的跑過。補上這行 cv2.imread() 把路徑讀成真正的圖片
    # 陣列，讓這段測試重新變成一段跑得動、真的能驗證整條生產線的自我測試。
    test_img_bgr = cv2.imread(test_img)
    if test_img_bgr is None:
        print(f"❌ 讀取測試圖片失敗，請確認檔案存在: {test_img}")
    else:
        # 抓取人臉
        rgb_img, detections = face_detect_bgr(test_img_bgr, detector)
        print(f"1. 成功抓取人臉數量: {len(detections)}")

        # 如果有抓到臉，我們就處理第一張臉
        if len(detections) > 0:
            face_info = detections[0]

            # 把 5 個特徵點 (左眼、右眼、鼻子、左嘴角、右嘴角) 拿出來打包成一個 List
            landmarks = [
                face_info['left_eye'],
                face_info['right_eye'],
                face_info['nose'],
                face_info['left_lip'],
                face_info['right_lip']
            ]

            # 人臉對齊
            aligned_face = face_align(rgb_img, landmarks)
            print("2. 成功完成人臉對齊 (112x112)")

            # 萃取 DNA (512維度特徵)
            face_dna = feature_extract(aligned_face)
            print("3. 成功萃取特徵碼！前 5 個數字是:", face_dna[:5])
            print("特徵碼總長度:", len(face_dna))

        else:
            print("這張圖片裡沒有臉喔！")

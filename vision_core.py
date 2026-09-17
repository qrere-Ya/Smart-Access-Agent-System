import os
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
from retinaface import RetinaFace
from skimage import transform as trans
import onnxruntime as ort
from sklearn.preprocessing import normalize

# 【合併專案調整】改用 __file__ 相對路徑，不管從哪個資料夾執行都能正確定位到專案根目錄
_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))

detector = RetinaFace(quality='normal')
img_path = os.path.join(_PROJECT_ROOT, 'images', 'simon.jpg')
onnx_path = os.path.join(_PROJECT_ROOT, 'models', 'arcface_r100_v1.onnx')
liveness_model_path = os.path.join(_PROJECT_ROOT, 'models', 'MiniFASNetV2.onnx')
liveness_sess = ort.InferenceSession(liveness_model_path, providers=['CPUExecutionProvider'])
sess = ort.InferenceSession(onnx_path, providers=['CUDAExecutionProvider', 'CPUExecutionProvider'])

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

        face_img = cv2.resize(face_img, (80, 80))
        face_img = face_img.astype(np.float32)
        face_img = np.expand_dims(face_img.transpose(2, 0, 1), axis=0)

        input_name = liveness_sess.get_inputs()[0].name
        output = liveness_sess.run(None, {input_name: face_img})

        prediction = np.exp(output[0]) / np.sum(np.exp(output[0]))
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
    input_name = sess.get_inputs()[0].name
    output_name = sess.get_outputs()[0].name

    # 執行推理 (Inference)
    prediction = sess.run([output_name], {input_name: input_blob})[0]
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

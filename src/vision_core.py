import cv2
import numpy as np
np.int = int
np.float = float
np.bool = bool
from retinaface import RetinaFace
from skimage import transform as trans
import onnxruntime as ort
from sklearn.preprocessing import normalize

detector = RetinaFace(quality='normal')
img_path = '../images/simon.jpg'
onnx_path = '../models/arcface_r100_v1.onnx'
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
    test_img = '../images/simon.jpg'
    
    # 抓取人臉
    rgb_img, detections = face_detect_bgr(test_img, detector)
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
"""
資料擴增設定，提升小資料集下的模型精準度與泛化能力，也用來讓模型見過不同光線／
畫質條件下的真人臉。

【2026-09-28，解決光線泛化問題】用 live_antispoof_compare.py 實測攝影機真人臉，
發現自訓練模型在光線暗的環境下幾乎認不出真人（正確率 0%），亮一點的環境下才有
部分正確率（33%）——根因是 train_antispoof.py / finetune_unfreeze.py 原本用的
`transforms.Compose([Resize, ToTensor, Normalize])` 完全沒有做任何亮度/明暗擴增，
模型從頭到尾沒看過任何刻意調暗/調亮的訓練圖片，CelebA-Spoof 資料集本身的真人
照片光線條件也偏一致，模型自然沒機會學到「暗一點也是真人」。

這個檔案原本就有寫好 albumentations 版的擴增，但從來沒有被兩支訓練腳本實際
import 使用（純粹是「進階選項」草稿）。這次補上：
1. 實際讓 train_antispoof.py / finetune_unfreeze.py 的訓練資料 import 使用（見
   下方 AlbumentationsTrainTransform，介面設計成可以直接取代原本的 train_tf）。
2. 新增幾個通用的光線/雜訊擴增：RandomGamma（模擬相機非線性感光造成的明暗變化，
   比單純線性調亮度更接近真實相機行為）、RandomShadow／RandomSunFlare（模擬室內
   單一光源角度不均勻，臉部一側偏暗/有局部反光）、GaussNoise（模擬低光環境下
   感光元件雜訊變大的真實狀況）。這四個都是 albumentations 內建的標準類別，
   不是針對「今天這個房間這盞燈」寫死的邏輯，可以泛化到任何未來換攝影機／換
   場地的光線條件，符合「不寫死」的原則。

只套用在「訓練」資料上：驗證／測試集（evaluate_antispoof.py 的 test_tf）維持
不做任何隨機擴增，才能誠實反映模型在真實未擴增資料上的表現，這是標準作法，
不動 evaluate_antispoof.py。
"""
import albumentations as A
import numpy as np
from albumentations.pytorch import ToTensorV2

train_transform = A.Compose([
    A.Resize(224, 224),
    A.RandomBrightnessContrast(brightness_limit=0.2, contrast_limit=0.2, p=0.7),
    A.RandomGamma(gamma_limit=(60, 140), p=0.5),
    A.RandomShadow(p=0.3),
    A.RandomSunFlare(flare_roi=(0, 0, 1, 1), src_radius=80, p=0.15),
    A.GaussNoise(p=0.3),
    A.GaussianBlur(blur_limit=(3, 5), p=0.3),
    A.ImageCompression(quality_range=(50, 95), p=0.4),  # 模擬不同壓縮品質
    A.HorizontalFlip(p=0.5),
    A.Rotate(limit=10, p=0.5),
    A.Normalize(mean=(0.485, 0.456, 0.406), std=(0.229, 0.224, 0.225)),
    ToTensorV2(),
])


class AlbumentationsTrainTransform:
    """轉接層：讓 albumentations 的 A.Compose 可以像 torchvision transform 一樣，
    直接被 celeba_spoof_dataset.py 的 __getitem__()（`self.transform(pil_img)`）
    呼叫——torchvision 是 `transform(img)`，albumentations 是
    `transform(image=np_array)["image"]`，介面不一樣，這裡做轉接，不用改動
    celeba_spoof_dataset.py 本身。"""

    def __init__(self, compose):
        self.compose = compose

    def __call__(self, pil_img):
        img_np = np.array(pil_img)
        return self.compose(image=img_np)["image"]


# train_antispoof.py / finetune_unfreeze.py 直接 `from augmentation import
# train_tf` 取代原本自己定義的 torchvision train_tf 即可，回傳格式一致（CHW
# float32 Tensor），Dataset／DataLoader 端完全不用改。
train_tf = AlbumentationsTrainTransform(train_transform)

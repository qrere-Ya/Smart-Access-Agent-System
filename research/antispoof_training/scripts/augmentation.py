"""
資料擴增設定，提升小資料集下的模型精準度與泛化能力。
給 train_antispoof.py 需要時 import 使用；目前 train_antispoof.py 用的是
torchvision.transforms 的簡化版本，這裡提供 albumentations 版本作為進階選項——
如果自蒐集資料量偏少、想要更豐富的擾動（模擬不同壓縮品質、不同光線），可以把
train_antispoof.py 裡的 train_tf 換成用這裡的 train_transform。
"""
import albumentations as A

train_transform = A.Compose([
    A.RandomBrightnessContrast(brightness_limit=0.2, contrast_limit=0.2, p=0.7),
    A.GaussianBlur(blur_limit=(3, 5), p=0.3),
    A.ImageCompression(quality_lower=50, quality_upper=95, p=0.4),  # 模擬不同壓縮品質
    A.HorizontalFlip(p=0.5),
    A.Rotate(limit=10, p=0.5),
    A.Normalize(mean=(0.485, 0.456, 0.406), std=(0.229, 0.224, 0.225)),
])

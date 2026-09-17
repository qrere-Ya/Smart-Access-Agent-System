"""
給官方 CelebA-Spoof（解壓縮版，資料夾長這樣：CelebA_Spoof/Data/{train,test}/<ID>/
{live,spoof}/*.jpg + metas/...）用的 PyTorch Dataset／載入工具。

【為什麼不像 prepare_celeba_spoof.py（HF 版）一樣把圖片複製進 data/dataset_split/？】
官方資料解壓縮後本身就有 ~78GB，如果再複製一份到 dataset_split/ 底下分類存放，
硬碟用量會直接翻倍——在目前的硬碟空間下太危險。這裡改用「manifest（清單）」的
做法：prepare_official_celeba_spoof.py 只寫出 train/val/test 三份很小的 json 清單
（每筆記錄圖片相對於官方資料夾根目錄的路徑 + 二元標籤 0=bona_fide/1=attack），
實際圖片完全不搬動、留在原地（例如你電腦上的 Downloads\CelebA_Spoof_\CelebA_Spoof），
訓練/評估時才即時從那裡讀圖——manifest 本身只佔幾百 KB，不會多佔任何圖片空間。

train_antispoof.py / evaluate_antispoof.py 都改成呼叫這裡的 build_split_dataset()：
它會自動偵測 data/dataset_split_official/<split>.json 存不存在——存在就用官方資料
（這份 manifest 版）；不存在就 fallback 回 data/dataset_split/<split>/ 的
ImageFolder（HF 精簡版或自己蒐集的資料都是這個格式）。兩種資料來源完全不用改
train/evaluate 腳本本身的邏輯，介面保持一致（.classes / .class_to_idx 兩邊都有）。
"""
import json
import os

from PIL import Image
from torch.utils.data import Dataset
from torchvision import datasets

DATA_ROOT = os.path.join(os.path.dirname(__file__), "..", "data")
OFFICIAL_SPLIT_DIR = os.path.join(DATA_ROOT, "dataset_split_official")
FALLBACK_SPLIT_DIR = os.path.join(DATA_ROOT, "dataset_split")


class CelebASpoofManifestDataset(Dataset):
    """讀 prepare_official_celeba_spoof.py 輸出的 manifest json，圖片直接從
    官方資料夾原地讀取，不複製、不搬動。"""

    classes = ["bona_fide", "attack"]
    class_to_idx = {"bona_fide": 0, "attack": 1}

    def __init__(self, manifest_path, transform=None):
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
        self.celeba_root = manifest["celeba_root"]
        self.entries = manifest["entries"]  # [[relative_path, label(0/1)], ...]
        self.transform = transform

    def __len__(self):
        return len(self.entries)

    def __getitem__(self, idx):
        rel_path, label = self.entries[idx]
        # manifest 裡的 rel_path 是相對於官方資料夾底下 Data/ 子資料夾的路徑
        # （例如 "train/4508/spoof/120199.jpg"），實際圖片在
        # <celeba_root>/Data/<rel_path>，這裡一定要接上 "Data" 這層，不然會漏掉。
        img_path = os.path.join(self.celeba_root, "Data", rel_path)
        if not os.path.isfile(img_path):
            # 保險 fallback：萬一 celeba_root 設定時已經指到 Data/ 那層，
            # 上面那個路徑會多一層 Data，這裡再試一次不加 Data 的版本。
            fallback_path = os.path.join(self.celeba_root, rel_path)
            if os.path.isfile(fallback_path):
                img_path = fallback_path
            else:
                raise FileNotFoundError(
                    f"兩種路徑都找不到圖片：{img_path} 或 {fallback_path}"
                    f"——確認一下 CELEBA_SPOOF_ROOT 是否設對，或這張圖片在你下載的"
                    f"資料裡是不是真的缺檔"
                )
        img = Image.open(img_path).convert("RGB")
        if self.transform is not None:
            img = self.transform(img)
        return img, label


def build_split_dataset(split_name, transform):
    """split_name: 'train' / 'val' / 'test'。

    自動判斷資料來源：
      1. data/dataset_split_official/<split>.json 存在 → 用官方 CelebA-Spoof
         （manifest 版，圖片原地讀取，見檔頭說明）
      2. 否則 data/dataset_split/<split>/ 存在 → 用 HF 精簡版或自己蒐集的資料
         （ImageFolder，圖片已經實際複製分類好）
      3. 都沒有 → 丟錯誤，提醒先跑對應的 prepare 腳本
    """
    official_manifest = os.path.join(OFFICIAL_SPLIT_DIR, f"{split_name}.json")
    if os.path.isfile(official_manifest):
        return CelebASpoofManifestDataset(official_manifest, transform=transform)

    fallback_dir = os.path.join(FALLBACK_SPLIT_DIR, split_name)
    if os.path.isdir(fallback_dir):
        return datasets.ImageFolder(fallback_dir, transform=transform)

    raise FileNotFoundError(
        f"找不到 {split_name} 資料——先跑 scripts/prepare_official_celeba_spoof.py"
        f"（官方 CelebA-Spoof）或 scripts/prepare_celeba_spoof.py"
        f"（Hugging Face 精簡版）其中一支，把資料準備好再跑這支。"
    )

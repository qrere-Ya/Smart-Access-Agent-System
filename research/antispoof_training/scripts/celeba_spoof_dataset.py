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

【2026-09-18，修正訓練／推論的人臉裁切不對齊問題（會議實測「開燈能辨識、關燈
判定非活體」「手機/列印翻拍偶爾能通過」的根本原因）】
vision_core.py 的 check_liveness()（正式上線推論用）在丟進模型前，一定會先用
RetinaFace 偵測到的臉部框、外擴 20% padding，把畫面裁切成「只有臉部周圍」才
resize 成 224x224；但這支 Dataset 原本 __getitem__() 是 `Image.open(...).convert
("RGB")` 直接讀「整張原始照片」（可能包含大片背景、身體、甚至多人），完全沒有
做任何裁切，train_antispoof.py / finetune_unfreeze.py / evaluate_antispoof.py 的
transforms 也只有 Resize((224,224))，同樣沒有裁切這一步——模型訓練時看到的畫面
「框架比例」跟正式上線推論時完全不一樣，這正是「訓練時分數看起來很好（EER
10.56%），但換一個環境（比如光線）、輸入的框架比例稍微不同，實際表現就不穩」
的根本原因，不是燈光本身的問題。

CelebA-Spoof 官方其實有提供 *_BB.txt（每張圖片旁邊都有一份同名的 bounding box
標註檔），只是原本整條 pipeline 完全沒有讀取、使用它。這裡補上讀取＋裁切的邏輯，
讓訓練資料跟正式推論用「同一套裁切方式」：

1. *_BB.txt 格式（實際下載到本機的官方資料驗證過，不是憑空假設）：單行、空白
   分隔 5 個數字："x y w h score"。例如 000184.jpg（實際圖片大小 350x223）對應的
   000184_BB.txt 內容是 "61 45 61 112 0.9970805"。
2. 這 4 個座標數字是相對於「224x224 參考尺寸」，不是原始圖片的實際像素座標——
   這一點也是實際下載官方資料、把同一個框分別用「不縮放」跟「乘上
   實際寬高/224 縮放」兩種方式裁出來，親眼比對哪一種真的框到臉才確認下來的：
   不縮放裁出來的框幾乎都框到耳朵/頭髮/背景，乘上 (實際寬/224, 實際高/224)
   縮放後裁出來的框才是正確置中的臉部特寫。所以要先做這個縮放，才能得到
   實際像素座標的臉部框。
3. 算出臉部框之後，套用跟 vision_core.py check_liveness() 完全一樣的「外擴 20%
   padding」邏輯（同一份 w*0.2 / h*0.2 外擴公式），確保訓練圖片跟正式推論丟進
   模型的畫面框架比例一致，才是真正公平的訓練/推論對齊。
"""
import json
import os

from PIL import Image
from torch.utils.data import Dataset
from torchvision import datasets

DATA_ROOT = os.path.join(os.path.dirname(__file__), "..", "data")
OFFICIAL_SPLIT_DIR = os.path.join(DATA_ROOT, "dataset_split_official")
FALLBACK_SPLIT_DIR = os.path.join(DATA_ROOT, "dataset_split")

# 跟 vision_core.py check_liveness() 裡的外擴比例保持一致，兩邊只要有一邊改了
# padding 比例，這裡也要跟著改，不然又會重新造成訓練/推論框架比例不對齊。
_FACE_PADDING_RATIO = 0.2


def _parse_bb_file(bb_path, img_w, img_h):
    """讀取 CelebA-Spoof 官方 *_BB.txt，回傳「實際像素座標」的 (x1, y1, x2, y2)。

    檔案格式是單行、空白分隔的 5 個數字："x y w h score"，其中 x/y/w/h 是相對於
    224x224 參考尺寸的座標，必須先乘上 (實際寬/224, 實際高/224) 縮放，才會對到
    這張圖片實際的像素座標——見檔頭說明，這是拿本機實際資料驗證過的結論。

    【2026-09-18 補強，實際跑訓練時真的撞到】官方資料裡少數 *_BB.txt 檔案本身是
    壞的——檔案「存在」（os.path.isfile() 會過），但內容是空的，讀到的第一行
    split() 之後是空 list。原本這裡直接 `x, y, w, h = (float(v) for v in
    parts[:4])`，讀到 0 個數值時 unpack 會噴出很難懂的
    `ValueError: not enough values to unpack (expected 4, got 0)`，而且這個例外
    是在 DataLoader 的背景 worker process 裡發生的，會直接把整個訓練中斷掉
    （實測：train_antispoof.py 跑到 epoch 1 第 5000 步、約 16 萬張圖左右就撞到
    一個這樣的壞檔案，訓練直接掛掉，前面快一小時的進度全部要重跑）。
    這裡改成自己先檢查數量夠不夠，不夠就丟一個訊息清楚的 ValueError，交給
    呼叫端 __getitem__ 用 try/except 接住、跟「找不到 BB 檔案」「裁出空框」
    這兩種既有的 fallback 情況統一處理（退回整張原圖、印警告、不中斷訓練），
    而不是讓一張壞圖讓幾百萬張圖的訓練直接前功盡棄。
    """
    with open(bb_path, "r", encoding="utf-8") as f:
        parts = f.readline().split()
    if len(parts) < 4:
        raise ValueError(
            f"{bb_path} 內容不是預期的「x y w h score」格式"
            f"（讀到 {len(parts)} 個數值，至少需要 4 個）——檔案可能是空的或資料損壞。"
        )
    x, y, w, h = (float(v) for v in parts[:4])
    scale_x = img_w / 224.0
    scale_y = img_h / 224.0
    x1 = x * scale_x
    y1 = y * scale_y
    x2 = x1 + w * scale_x
    y2 = y1 + h * scale_y
    return x1, y1, x2, y2


def _crop_face_with_padding(img, box, pad_ratio=_FACE_PADDING_RATIO):
    """跟 vision_core.py check_liveness() 完全一致的「外擴 pad_ratio」裁切邏輯：
    以偵測到的臉部框為準，往外擴 pad_ratio（框本身寬高的比例），再裁切、夾在
    圖片邊界內——這樣訓練資料的框架比例才會跟正式推論一致。"""
    x1, y1, x2, y2 = box
    w, h = x2 - x1, y2 - y1
    img_w, img_h = img.size
    x1 = max(0, int(x1 - w * pad_ratio))
    y1 = max(0, int(y1 - h * pad_ratio))
    x2 = min(img_w, int(x2 + w * pad_ratio))
    y2 = min(img_h, int(y2 + h * pad_ratio))
    return img.crop((x1, y1, x2, y2))


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

        # 【2026-09-18】改用官方 *_BB.txt 標註的臉部框（+20% padding，跟
        # vision_core.py check_liveness() 一致），把整張原始照片裁切成只留下臉部
        # 周圍——不再把整張照片（含大片背景/身體）直接餵給模型，見檔頭詳細說明。
        bb_path = os.path.splitext(img_path)[0] + "_BB.txt"
        if os.path.isfile(bb_path):
            try:
                img_w, img_h = img.size
                box = _parse_bb_file(bb_path, img_w, img_h)
                cropped = _crop_face_with_padding(img, box)
                # 保險：如果算出來的框異常（例如標註本身有問題）裁出 0 大小的圖片，
                # 寧可退回用整張原圖，也不要讓訓練在這裡直接崩潰。
                if cropped.size[0] > 0 and cropped.size[1] > 0:
                    img = cropped
                else:
                    print(f"[celeba_spoof_dataset][WARN] {bb_path} 算出來的裁切框是空的"
                          f"（原始標註可能有問題），這張改用整張原圖，不裁切：{img_path}")
            except (ValueError, OSError) as e:
                # 【2026-09-18 新增】BB.txt「檔案存在」但內容本身壞掉（空檔案、格式
                # 不對、數值不是數字、讀檔失敗等）——不要讓單一一張壞資料讓整個
                # 訓練直接中斷，退回用整張原圖（跟上面「找不到 BB 檔案」的 fallback
                # 邏輯一致），只印警告，方便之後統計壞檔案的比例、評估要不要回頭
                # 清理資料集。
                print(f"[celeba_spoof_dataset][WARN] {bb_path} 讀取/解析失敗"
                      f"（{type(e).__name__}: {e}），這張改用整張原圖，不裁切：{img_path}")
        else:
            # 官方資料理論上每張圖都有對應的 *_BB.txt，這裡只是保險；如果真的印出
            # 這行警告，代表資料下載不完整或檔案被移動過，需要回頭檢查資料來源。
            print(f"[celeba_spoof_dataset][WARN] 找不到 {bb_path}，這張圖片沒有裁切"
                  f"成人臉框，維持整張原圖：{img_path}")

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

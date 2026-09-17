"""
從 Hugging Face 抓取已經裁切好人臉的 CelebA-Spoof 精簡版資料集，省下官方完整版
（62.5 萬張、Google Drive / 百度網盤下載、資料夾與標籤檔案格式目前查不到官方文件）
動輒一兩天的下載/解壓/格式除錯時間——這支腳本可以在期初進度審查前把「資料→訓練→
匯出→評估」整條 pipeline 先跑通、拿到初步數字，官方完整版留到之後真的要寫論文
定稿時再考慮要不要重跑一次補齊（見本檔案最後一段的限制說明）。

【資料集】nguyenkhoa/celeba-spoof-for-face-antispoofing-test（Hugging Face，公開、
不用登入/申請）
  - 67,170 張已裁切人臉圖片，檔案總量約 4.9GB
  - 只有一個 "test" split（這個 HF 版本沒有提供官方的 train/val/test 切分）
  - 欄位：cropped_image（PIL 圖片）、labels（0=live 真人 / 1=spoof 攻擊）、labelNames

【跟現有 pipeline 的接法】
prepare_dataset.py + split_dataset.py 這條路是「自己蒐集的影片 → 裁人臉 → 照人切
train/val/test」，輸出到 data/dataset_split/。這支腳本做的事情等價於這兩支的合體，
只是資料來源換成 HF 已經裁好的圖片，最後同樣落地成
data/dataset_split/{train,val,test}/{bona_fide,attack}/ ——train_antispoof.py、
evaluate_antispoof.py 完全不用改就能直接吃。

之後如果另外蒐集了真人/列印/重播影片，跑 prepare_dataset.py + split_dataset.py 會
產生 bona_fide/print_attack/replay_attack 三個資料夾；要跟這裡的資料合併，最簡單的
作法是把 print_attack、replay_attack 的圖片複製進對應 split 底下的 attack/ 資料夾
（合併成二分類）。這一步等真的有自蒐資料時再處理，這裡先不動。

【限制，一定要誠實寫進論文限制章節】
這個 HF 版本沒有身分（identity）欄位可用，跟 split_dataset.py 開頭強調的「一定要
照人切分、不能照圖片隨機切分」原則衝突——這是這個第三方精簡資料來源無法避免的限制。
這裡改用 scikit-learn 的分層抽樣（stratify=labels），確保 bona_fide/attack 的比例
在 train/val/test 三個切分裡一致，但無法保證同一人不會同時出現在 train 跟 test，
準確率數字可能因此偏樂觀。之後有時間的話，換成官方完整版 CelebA-Spoof（本身是從
CelebA 延伸、有身分標註）或申請到 CASIA-FASD / Replay-Attack，記得改用真正照人
切分的 split_dataset.py 重新訓練一次，兩個版本的數字可以直接放進論文比較。

安裝：pip install datasets pillow（scikit-learn 已在 requirements.txt）
"""
import os

from datasets import load_dataset
from PIL import Image
from sklearn.model_selection import train_test_split

HF_DATASET = "nguyenkhoa/celeba-spoof-for-face-antispoofing-test"
DATA_ROOT = os.path.join(os.path.dirname(__file__), "..", "data")
OUT_SPLIT_DIR = os.path.join(DATA_ROOT, "dataset_split")
FINAL_SIZE = 224          # 跟 prepare_dataset.py 的 FINAL_SIZE 一致，餵給 MobileNetV3
SPLIT_RATIO = {"train": 0.7, "val": 0.15, "test": 0.15}
RANDOM_SEED = 42
PROGRESS_EVERY = 5000

# HF 的 labels：0=live（真人）、1=spoof（攻擊）→ 對應到專案既有的資料夾命名
LABEL_TO_CATEGORY = {0: "bona_fide", 1: "attack"}


def stratified_three_way_split(n, labels):
    """把 0..n-1 的索引，依 labels 分層切成 train/val/test 三份索引清單。"""
    idx_all = list(range(n))
    idx_train, idx_temp = train_test_split(
        idx_all,
        train_size=SPLIT_RATIO["train"],
        stratify=labels,
        random_state=RANDOM_SEED,
    )
    temp_labels = [labels[i] for i in idx_temp]
    val_ratio_of_temp = SPLIT_RATIO["val"] / (SPLIT_RATIO["val"] + SPLIT_RATIO["test"])
    idx_val, idx_test = train_test_split(
        idx_temp,
        train_size=val_ratio_of_temp,
        stratify=temp_labels,
        random_state=RANDOM_SEED,
    )
    return {"train": idx_train, "val": idx_val, "test": idx_test}


def save_split(ds, split_name, indices):
    counts = {"bona_fide": 0, "attack": 0}
    total = len(indices)
    for out_idx, ds_idx in enumerate(indices):
        row = ds[ds_idx]
        category = LABEL_TO_CATEGORY[row["labels"]]
        out_dir = os.path.join(OUT_SPLIT_DIR, split_name, category)
        os.makedirs(out_dir, exist_ok=True)

        img = row["cropped_image"]
        if img.mode != "RGB":
            img = img.convert("RGB")
        if img.size != (FINAL_SIZE, FINAL_SIZE):
            img = img.resize((FINAL_SIZE, FINAL_SIZE), Image.LANCZOS)

        fname = f"hf_{ds_idx:06d}.jpg"
        img.save(os.path.join(out_dir, fname), quality=95)
        counts[category] += 1

        if (out_idx + 1) % PROGRESS_EVERY == 0:
            print(f"  [{split_name}] 已處理 {out_idx + 1}/{total} 張")

    print(f"[{split_name}] bona_fide={counts['bona_fide']} attack={counts['attack']} "
          f"→ {os.path.join(OUT_SPLIT_DIR, split_name)}")


def main():
    print(f"正在從 Hugging Face 下載 {HF_DATASET}（約 4.9GB，第一次跑會比較久，"
          f"之後會用本機快取，不用重抓）...")
    ds = load_dataset(HF_DATASET, split="test")
    n = len(ds)
    print(f"下載完成，共 {n} 筆")

    labels = ds["labels"]
    splits = stratified_three_way_split(n, labels)

    for split_name, indices in splits.items():
        save_split(ds, split_name, indices)

    print("\n完成，資料已經是切好的 train/val/test 格式，不用再跑 split_dataset.py，"
          "可以直接：python train_antispoof.py")


if __name__ == "__main__":
    main()

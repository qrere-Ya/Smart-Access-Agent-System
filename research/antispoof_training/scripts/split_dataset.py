"""
照「人」切分資料集，避免同一人同時出現在訓練/測試集造成資料洩漏。
假設檔名格式為 <人名或編號>_<其他資訊>.jpg（跟 prepare_dataset.py 輸出的命名一致，
只要原始影片檔名是用人名/編號開頭，這裡就能正確切分）。

重要：一定要照「人」切分，不能照「圖片」隨機切分——同一個人的臉出現在訓練集又出現
在測試集，會讓模型學會「認人臉」而不是「認真假」，準確率數字會虛高、沒有參考價值，
這是 PAD 領域最容易被口試委員抓到的錯誤。
"""
import os
import random
import shutil
from collections import defaultdict

random.seed(42)

DATA_ROOT = os.path.join(os.path.dirname(__file__), "..", "data")
SRC_DIR = os.path.join(DATA_ROOT, "dataset")
OUT_ROOT = os.path.join(DATA_ROOT, "dataset_split")
SPLIT_RATIO = {"train": 0.7, "val": 0.15, "test": 0.15}
CATEGORIES = ["bona_fide", "print_attack", "replay_attack"]


def split_by_person(category_dir, out_root, category):
    files_by_person = defaultdict(list)
    for fname in os.listdir(category_dir):
        person_id = fname.split("_")[0]
        files_by_person[person_id].append(fname)

    persons = list(files_by_person.keys())
    random.shuffle(persons)
    n = len(persons)
    n_train = int(n * SPLIT_RATIO["train"])
    n_val = int(n * SPLIT_RATIO["val"])

    splits = {
        "train": persons[:n_train],
        "val": persons[n_train:n_train + n_val],
        "test": persons[n_train + n_val:],
    }

    for split_name, split_persons in splits.items():
        out_dir = os.path.join(out_root, split_name, category)
        os.makedirs(out_dir, exist_ok=True)
        for person in split_persons:
            for fname in files_by_person[person]:
                shutil.copy(os.path.join(category_dir, fname), os.path.join(out_dir, fname))
    print(f"[{category}] {n} 人 → train={len(splits['train'])} val={len(splits['val'])} test={len(splits['test'])}")


def main():
    for category in CATEGORIES:
        cat_dir = os.path.join(SRC_DIR, category)
        if not os.path.isdir(cat_dir):
            print(f"⚠️ 找不到 {cat_dir}，先跑 prepare_dataset.py 再執行這支")
            continue
        split_by_person(cat_dir, OUT_ROOT, category)
    print(f"切分完成，輸出在 {OUT_ROOT}/" + "{train,val,test}/{bona_fide,print_attack,replay_attack}/")


if __name__ == "__main__":
    main()

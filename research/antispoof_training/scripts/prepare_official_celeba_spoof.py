"""
用官方 CelebA-Spoof 完整版（解壓縮後放在你電腦某個資料夾，例如
Downloads\\CelebA_Spoof_\\CelebA_Spoof，裡面長這樣：

    CelebA_Spoof/
        Data/{train,test}/<ID>/{live,spoof}/*.jpg + *_BB.txt
        metas/intra_test/{train_label.json,test_label.json}
        metas/protocol1/...、metas/protocol2/...

這支只用 metas/intra_test/ 這組標籤（Intra-Dataset Benchmark，官方本來就切好的
train/test，最適合拿來做簡單的二分類 live vs spoof，不是 protocol1/2 那種跨裝置
品質的進階實驗，那個之後有時間可以再另外做）。

【硬碟策略：完全不複製圖片】
78GB 的圖片本身不動、留在原地，這支只讀 metas 底下的 json 標籤，輸出三份很小的
「manifest」清單（data/dataset_split_official/{train,val,test}.json），每筆記錄
圖片相對路徑＋二元標籤。實際讀圖是 train_antispoof.py / evaluate_antispoof.py
呼叫 celeba_spoof_dataset.py 時才即時從官方資料夾讀取，硬碟不會因為這支腳本多用
任何圖片空間。

【官方版比 HuggingFace 精簡版多出來的優勢：真正照人切分】
HF 精簡版（prepare_celeba_spoof.py）沒有身分欄位，只能隨機照圖片切 train/val/test，
這是那份資料的已知限制。官方版的路徑（例如 train/1/live/000184.jpg）本身就帶著
身分 ID（"1"），所以這支腳本可以比照 split_dataset.py 的原則，真正「照人」切分：
metas/intra_test/train_label.json 裡的所有人，先照人切成 train/val；
metas/intra_test/test_label.json 是官方本來就切好的完全獨立測試集（不同身分 ID，
不跟 train/val 重疊），直接原封不動當作 test，不再另外處理。

live/spoof 的二元標籤直接看檔案路徑在 live/ 還是 spoof/ 資料夾底下判斷（官方
Directory Structure 文件明確保證這點），不去猜 json 標籤向量裡第 43 個欄位的
數值編碼方式，比較不會猜錯。

【資料量與訓練時間的取捨，先看過再決定要不要開 MAX_IMAGES_PER_CLASS】
官方完整版光是 train_label.json 就有數十萬張圖，train_antispoof.py 原本的
DataLoader 是單執行緒讀圖，如果直接拿全部資料跑，一個 epoch 光讀圖／解碼／resize
就可能要 20-30 分鐘以上，20 個 epoch 可能要一整天以上——在期初進度審查前的時間
壓力下不一定跑得完。這支腳本預設不限制數量（MAX_IMAGES_PER_CLASS = None，全部都
用），如果想先跑一版有數字可以交、之後再補完整版，把下面的 MAX_IMAGES_PER_CLASS
改成一個數字（例如 60000）即可，會照人分完 train/val 之後、在圖片層級隨機抽樣到
這個上限，會印出抽樣前後的張數方便你判斷。

安裝：不需要額外套件，torch/torchvision/pillow 都已經在 requirements.txt。
用法：
    set CELEBA_SPOOF_ROOT=C:\\Users\\qrere\\Downloads\\CelebA_Spoof_\\CelebA_Spoof
    python scripts\\prepare_official_celeba_spoof.py
"""
import json
import os
import random

RANDOM_SEED = 42
VAL_RATIO_OF_TRAIN = 0.15   # 從官方 train_label.json 的人裡，切 15% 出來當 val
MAX_IMAGES_PER_CLASS = None  # 例如改成 60000 可以限制訓練規模、加快跑第一版的速度

DATA_ROOT = os.path.join(os.path.dirname(__file__), "..", "data")
OUT_DIR = os.path.join(DATA_ROOT, "dataset_split_official")

LABEL_MAP = {"live": 0, "spoof": 1}  # 0=bona_fide, 1=attack（跟 HF 版的資料夾命名對齊）


def get_celeba_root():
    root = os.environ.get("CELEBA_SPOOF_ROOT")
    if not root:
        raise SystemExit(
            "請先設定環境變數 CELEBA_SPOOF_ROOT，指到解壓縮後的 CelebA_Spoof 資料夾"
            "（裡面要看得到 Data/ 和 metas/ 這兩個子資料夾）。\n"
            "PowerShell 範例：\n"
            r'  $env:CELEBA_SPOOF_ROOT = "C:\Users\qrere\Downloads\CelebA_Spoof_\CelebA_Spoof"'
            "\n再重新執行這支腳本。"
        )
    root = os.path.abspath(root)
    if not os.path.isdir(os.path.join(root, "Data")) or not os.path.isdir(os.path.join(root, "metas")):
        raise SystemExit(
            f"CELEBA_SPOOF_ROOT={root} 底下看不到 Data/ 或 metas/，路徑可能設錯了，"
            f"確認一下是不是要多一層或少一層資料夾。"
        )
    return root


def resolve_relative_path(raw_key):
    """json 裡的 key 有時候會帶 'Data/' 前綴、有時候不會，這裡統一正規化成
    相對於 CELEBA_ROOT 底下 'Data/' 的路徑（例如 'train/1/live/000184.jpg'），
    回傳的字串保證用 '/' 分隔，方便之後用 os.path.join 接。"""
    normalized = raw_key.replace("\\", "/").lstrip("/")
    if normalized.startswith("Data/"):
        normalized = normalized[len("Data/"):]
    return normalized


def label_from_path(normalized_rel_path):
    """路徑裡一定會經過 live/ 或 spoof/ 這一層資料夾，官方文件保證這點，
    直接照資料夾名稱判斷標籤，不去猜 json 標籤向量的數值編碼。"""
    parts = normalized_rel_path.split("/")
    for part in parts:
        if part in LABEL_MAP:
            return LABEL_MAP[part]
    raise ValueError(f"路徑裡找不到 live/spoof 這層資料夾，格式跟預期不一樣：{normalized_rel_path}")


def person_id_from_path(normalized_rel_path):
    """路徑格式是 <train|test>/<ID>/<live|spoof>/xxxxx.jpg，身分 ID 就是
    live/spoof 那層的上一層資料夾名稱。"""
    parts = normalized_rel_path.split("/")
    for i, part in enumerate(parts):
        if part in LABEL_MAP and i > 0:
            return parts[i - 1]
    raise ValueError(f"路徑格式跟預期不一樣，抓不到身分 ID：{normalized_rel_path}")


def load_label_json(celeba_root, split_name):
    """讀 metas/intra_test/<split>_label.json，回傳 [(normalized_rel_path, label, person_id), ...]"""
    json_path = os.path.join(celeba_root, "metas", "intra_test", f"{split_name}_label.json")
    if not os.path.isfile(json_path):
        raise SystemExit(f"找不到 {json_path}，確認 CELEBA_SPOOF_ROOT 設定是否正確。")

    with open(json_path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    entries = []
    skipped = 0
    for raw_key in raw.keys():
        try:
            rel_path = resolve_relative_path(raw_key)
            label = label_from_path(rel_path)
            person_id = person_id_from_path(rel_path)
            entries.append((rel_path, label, person_id))
        except ValueError:
            skipped += 1
    if skipped:
        print(f"  ⚠️ {split_name}_label.json 裡有 {skipped} 筆路徑格式異常，已跳過")
    return entries


def subsample_per_class(entries, max_per_class, rng):
    if max_per_class is None:
        return entries
    by_label = {0: [], 1: []}
    for e in entries:
        by_label[e[1]].append(e)
    sampled = []
    for label, group in by_label.items():
        if len(group) > max_per_class:
            sampled.extend(rng.sample(group, max_per_class))
        else:
            sampled.extend(group)
    return sampled


def write_manifest(celeba_root, out_path, entries):
    manifest = {
        "celeba_root": celeba_root,
        # entries 存 [rel_path, label] 就好，person_id 只有切分的時候用得到
        "entries": [[rel_path, label] for rel_path, label, _person_id in entries],
    }
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f)
    n_live = sum(1 for _, label, _ in entries if label == 0)
    n_attack = sum(1 for _, label, _ in entries if label == 1)
    print(f"  → {out_path}  共 {len(entries)} 筆（bona_fide={n_live}, attack={n_attack}）")


def main():
    rng = random.Random(RANDOM_SEED)
    celeba_root = get_celeba_root()
    print(f"CELEBA_SPOOF_ROOT = {celeba_root}")

    print("讀取 metas/intra_test/train_label.json ...")
    train_full = load_label_json(celeba_root, "train")
    print(f"  官方 train 共 {len(train_full)} 筆")

    print("讀取 metas/intra_test/test_label.json ...")
    test_entries = load_label_json(celeba_root, "test")
    print(f"  官方 test 共 {len(test_entries)} 筆")

    # 照「人」把 train_full 切成 train/val，避免同一人同時出現在兩邊
    persons = sorted({person_id for _, _, person_id in train_full})
    rng.shuffle(persons)
    n_val_persons = max(1, int(len(persons) * VAL_RATIO_OF_TRAIN))
    val_persons = set(persons[:n_val_persons])
    train_persons = set(persons[n_val_persons:])

    # 保險檢查：test 的身分 ID 不該跟 train/val 重疊（官方 intra_test 協定本來就該如此）。
    # 官方資料集本身偶爾會有極少數身分 ID 重疊（不是這支腳本的路徑解析邏輯問題——
    # 如果是邏輯錯誤，重疊數量通常會是系統性的一大批，而不是個位數），保險起見這裡
    # 直接把重疊的身分 ID 從 train/val 移除、讓 test 那邊保留，確保 test 100% 乾淨、
    # 不會有任何身分同時出現在 train/val 跟 test。
    test_persons = {person_id for _, _, person_id in test_entries}
    overlap = test_persons & (train_persons | val_persons)
    if overlap:
        print(f"  ⚠️ 官方資料集本身有 {len(overlap)} 個身分 ID 同時出現在 train_label.json"
              f"跟 test_label.json（{sorted(overlap)}）——已經把這些 ID 從 train/val"
              f"排除，保留在 test，確保 test 不會被污染")
        train_persons -= overlap
        val_persons -= overlap
    else:
        print(f"  ✅ 確認 test 的身分 ID 跟 train/val 完全不重疊")
    print(f"  最終：train {len(train_persons)} 人、val {len(val_persons)} 人、"
          f"test {len(test_persons)} 人")

    train_entries = [e for e in train_full if e[2] in train_persons]
    val_entries = [e for e in train_full if e[2] in val_persons]

    if MAX_IMAGES_PER_CLASS is not None:
        print(f"套用 MAX_IMAGES_PER_CLASS={MAX_IMAGES_PER_CLASS}，subsample 前：")
        print(f"  train={len(train_entries)}  val={len(val_entries)}")
        train_entries = subsample_per_class(train_entries, MAX_IMAGES_PER_CLASS, rng)
        val_entries = subsample_per_class(val_entries, max(1, int(MAX_IMAGES_PER_CLASS * VAL_RATIO_OF_TRAIN)), rng)
        print(f"subsample 後：train={len(train_entries)}  val={len(val_entries)}")

    print("寫出 manifest：")
    write_manifest(celeba_root, os.path.join(OUT_DIR, "train.json"), train_entries)
    write_manifest(celeba_root, os.path.join(OUT_DIR, "val.json"), val_entries)
    write_manifest(celeba_root, os.path.join(OUT_DIR, "test.json"), test_entries)

    print(f"\n完成，圖片沒有被複製、留在原地。可以直接：python train_antispoof.py")


if __name__ == "__main__":
    main()

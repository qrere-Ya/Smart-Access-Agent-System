"""
build_front_bundle.py：把前台（門禁現場電腦）要用的東西打包成一個資料夾與 zip，複製到前台電腦後一鍵安裝。

主要功能：
  - 程式：從前台入口（terminal_app、front_agent、front_check、role_config）用 AST 追出所有用到的專案模組，
    只打包這些（之後前台程式多引用一個模組也會自動帶上，不用手動維護清單）
  - 模型：只打包前台用的（ArcFace、兩個活體模型、YOLO、語音）；不含知識庫的向量與重排模型
  - 依賴：requirements/front.txt 與 lock.txt；安裝腳本與啟動腳本（deploy/front_template）
  - MANIFEST.json：每個檔案的 SHA-256，安裝第一步用來確認複製過程沒有缺檔或損壞
  - 絕不打包：data/、runtime/（資料庫與人臉特徵）、.env、測試、後台與知識庫程式

用法：python -m deploy.build_front_bundle [--out dist] [--no-zip]

串接模組：
  - deploy/front_template：安裝與啟動腳本；shared.paths：專案路徑
"""
import argparse
import ast
import hashlib
import json
import os
import shutil
import sys
import zipfile

from shared import paths

BUNDLE_NAME = "SmartAccessFront"
ENTRY_MODULES = ("access.terminal_app", "access.front_agent", "access.front_check", "shared.role_config")
PROJECT_PACKAGES = ("access", "shared")           # 前台允許打包的專案套件（不含 admin、rag）
MODEL_FILES = ("arcface_r100_v1.onnx", "MiniFASNetV2.onnx",
               "antispoof_mobilenetv3_antispoof_finetuned_best_sim.onnx", "yolov8s.pt")
MODEL_DIRS = ("vits-piper-zh_CN-huayan-medium",)
REQUIREMENT_FILES = ("front.txt", "lock.txt")
REQUIREMENTS_TXT = ("requirements/front.txt", "requirements/lock.txt")   # 有中文註解，加 BOM 才不會被 gbk 誤讀
TEXT_SUFFIXES = (".bat",)                          # 這些複製時轉成 CRLF
README = """Smart Access 前台安裝包

安裝（第一次）：
  1. 把整個資料夾複製到前台電腦（例如 C:\\SmartAccessFront），不要放在需要系統管理員才能寫入的位置。
  2. 先在後台執行 python -m shared.role_config token 產生通行碼，並用同一組通行碼設定後台。
  3. 雙擊「安裝前台.bat」，照畫面回答（安裝套件需要網路，約 10 到 30 分鐘）。不用設 IP：兩台用網路線直連、網卡維持
     「自動取得 IP」，程式會自己互相找到（位址分配要 30 到 60 秒）。後台的 Wi-Fi 與上網不受影響。
  4. 最後一步會自我檢查，並印出每一項是 OK、WARN 還是 FAIL。

日常使用：
  - 後台先開，再雙擊桌面「啟動前台門禁」（會同時開連線代理與辨識畫面）。
  - 出問題先雙擊「前台檢查.bat」。防火牆要重做：右鍵「設定前台網路.bat」→ 以系統管理員身分執行。

限制：
  - 通行碼只存在這個使用者的環境變數；前台硬碟上的人臉快取沒有加密，要靠前台機器的實體安全。
  - 兩台之間用 TLS 加密，前台只信任後台憑證的指紋（自動尋找時由通行碼簽章的回覆取得）；沒有登入失敗鎖定，
    也沒有經過資安稽核，不能視為安全。後台重新產生憑證或換位址後，前台會自動重新尋找。
"""


def project_modules(entries=ENTRY_MODULES, root=None):
    """從入口追出所有用到的專案內模組（包含函式裡的延後 import），回傳排序後的相對路徑（含套件的 __init__.py）。"""
    root = root or paths.PROJECT_ROOT
    seen, todo, files = set(), list(entries), set()
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        seen.add(name)
        if name.split(".")[0] not in PROJECT_PACKAGES:
            continue
        rel = name.replace(".", "/") + ".py"
        if not os.path.isfile(os.path.join(root, rel)):
            continue
        files.add(rel)
        files.add(name.split(".")[0] + "/__init__.py")
        with open(os.path.join(root, rel), encoding="utf-8") as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                todo.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                todo.append(node.module)
                todo.extend(f"{node.module}.{alias.name}" for alias in node.names)
    return sorted(files)


def collect(root=None):
    """回傳 {打包內路徑: 來源完整路徑}，缺任何必要檔案就丟 FileNotFoundError。"""
    root = root or paths.PROJECT_ROOT
    chosen = {}
    for rel in project_modules(root=root):
        chosen[rel] = os.path.join(root, rel)
    for name in MODEL_FILES:
        chosen["models/" + name] = os.path.join(root, "models", name)
    for folder in MODEL_DIRS:
        base = os.path.join(root, "models", folder)
        if not os.path.isdir(base):
            raise FileNotFoundError(base)
        for here, _dirs, names in os.walk(base):
            for name in names:
                full = os.path.join(here, name)
                chosen["models/" + os.path.relpath(full, os.path.join(root, "models")).replace(os.sep, "/")] = full
    for name in REQUIREMENT_FILES:
        chosen["requirements/" + name] = os.path.join(root, "requirements", name)
    template = os.path.join(root, "deploy", "front_template")
    for name in sorted(os.listdir(template)):
        chosen[name] = os.path.join(template, name)
    missing = [rel for rel, full in chosen.items() if not os.path.isfile(full)]
    if missing:
        raise FileNotFoundError("缺少檔案：" + "、".join(missing))
    return chosen


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _copy(source, target):
    """複製一個檔案；.bat 轉 CRLF，.ps1 與依賴清單加 UTF-8 BOM（PowerShell 5.1 與中文 Windows 的 pip 才讀得對）。"""
    os.makedirs(os.path.dirname(target), exist_ok=True)
    if source.endswith(TEXT_SUFFIXES):
        with open(source, "rb") as f:
            data = f.read().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
        with open(target, "wb") as f:
            f.write(data)
    elif source.endswith(".ps1") or target.replace("\\", "/").endswith(REQUIREMENTS_TXT):  # 加 BOM
        with open(source, "rb") as f:
            data = f.read()
        with open(target, "wb") as f:
            f.write(data if data.startswith(b"\xef\xbb\xbf") else b"\xef\xbb\xbf" + data)
    else:
        shutil.copy2(source, target)


def build(out_dir, make_zip=True, root=None):
    """建立 out_dir/SmartAccessFront 與（可選）zip，回傳 (資料夾, zip 路徑或 None, 檔案數)。"""
    chosen = collect(root)
    bundle = os.path.join(out_dir, BUNDLE_NAME)
    if os.path.isdir(bundle):
        shutil.rmtree(bundle)
    for rel, source in chosen.items():
        _copy(source, os.path.join(bundle, *rel.split("/")))
    with open(os.path.join(bundle, "讀我.txt"), "w", encoding="utf-8") as f:
        f.write(README.replace("\n", "\r\n"))
    hashes = {}
    for here, _dirs, names in os.walk(bundle):
        for name in names:
            full = os.path.join(here, name)
            hashes[os.path.relpath(full, bundle).replace(os.sep, "/")] = sha256_of(full)
    with open(os.path.join(bundle, "MANIFEST.json"), "w", encoding="utf-8") as f:
        json.dump({"version": 1, "files": dict(sorted(hashes.items()))}, f, ensure_ascii=False, indent=1)
    archive = None
    if make_zip:
        archive = os.path.join(out_dir, BUNDLE_NAME + ".zip")
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
            for here, _dirs, names in os.walk(bundle):
                for name in names:
                    full = os.path.join(here, name)
                    z.write(full, os.path.join(BUNDLE_NAME, os.path.relpath(full, bundle)))
    return bundle, archive, len(hashes)


def main(argv=None):
    parser = argparse.ArgumentParser(description="打包前台安裝包")
    parser.add_argument("--out", default=os.path.join(paths.PROJECT_ROOT, "dist"))
    parser.add_argument("--no-zip", action="store_true")
    args = parser.parse_args(argv)
    try:
        bundle, archive, count = build(args.out, make_zip=not args.no_zip)
    except FileNotFoundError as e:
        print(f"打包失敗：{e}")
        return 1
    print(f"已建立 {bundle}（{count} 個檔案）")
    if archive:
        print(f"zip：{archive}（{os.path.getsize(archive) / 2**20:.0f} MB）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

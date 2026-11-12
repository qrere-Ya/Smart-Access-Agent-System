"""
server_check.py：遠端測試入口（rag.remote_server）換到另一台電腦後的就緒檢查。

主要功能：
  - 逐項檢查並顯示 OK／注意／失敗：Python 版本、必要套件、向量模型與重排模型檔案、知識庫文件、資料庫與向量庫、
    存取金鑰（名稱與到期，不顯示金鑰）、NVIDIA_API_KEY 是否設定（只顯示有沒有，不印內容）、port 是否被占用、
    Tailscale 是否安裝並已登入、有沒有 GPU
  - 只讀不寫：不載入模型、不啟動伺服器、不修改任何檔案；加 --api 才會對雲端 API 實際呼叫一次小請求
  - 結束代碼：有「失敗」回 1，否則 0（只有「注意」仍算可以啟動）

串接模組：
  - shared.paths：所有路徑；rag.remote_access：金鑰庫；rag.llm_settings：金鑰讀取與雲端連線檢查
啟動：python -m rag.server_check [--api] [--port 8765]
"""
import argparse
import importlib.util
import os
import shutil
import socket
import sqlite3
import subprocess  # noqa: S404  # nosec B404  僅用於查詢 Tailscale 狀態
import sys
import time
from typing import List, Tuple

from shared import paths

OK, WARN, FAIL = "OK", "注意", "失敗"
REQUIRED_PACKAGES = ("flask", "torch", "faiss", "sentence_transformers", "transformers", "bm25s", "jieba",
                     "numpy", "pypdf", "docx2txt", "rapidfuzz")
EMBED_FILES = ("config.json", "modules.json", "tokenizer.json", "sentencepiece.bpe.model")
EMBED_WEIGHTS = ("pytorch_model.bin", "model.safetensors")
RERANK_FILES = ("config.json", "model.safetensors", "tokenizer.json")
DOC_SUFFIXES = (".pdf", ".docx", ".txt", ".md")
Result = Tuple[str, str, str]   # (等級, 項目, 說明)


def check_python() -> List[Result]:
    v = sys.version_info
    ok = (v.major, v.minor) == (3, 11)
    note = f"{v.major}.{v.minor}.{v.micro}" + ("" if ok else "（專案驗證的是 3.11）")
    return [(OK if ok else WARN, "Python 版本", note)]


def check_packages(names=REQUIRED_PACKAGES) -> List[Result]:
    missing = [n for n in names if importlib.util.find_spec(n) is None]
    if missing:
        return [(FAIL, "必要套件", "缺少：" + "、".join(missing) + "（依 requirements/main.txt 安裝）")]
    return [(OK, "必要套件", f"{len(names)} 個都找得到")]


def _missing_files(folder: str, names) -> List[str]:
    return [n for n in names if not os.path.isfile(os.path.join(folder, n))]


def check_models(models_dir: str = paths.MODELS_DIR) -> List[Result]:
    out: List[Result] = []
    embed = os.path.join(models_dir, "bge-m3")
    if not os.path.isdir(embed):
        out.append((FAIL, "向量模型 bge-m3", f"找不到資料夾 {embed}"))
    else:
        missing = _missing_files(embed, EMBED_FILES)
        if not any(os.path.isfile(os.path.join(embed, w)) for w in EMBED_WEIGHTS):
            missing.append("pytorch_model.bin 或 model.safetensors")
        out.append((FAIL, "向量模型 bge-m3", "缺少：" + "、".join(missing)) if missing
                   else (OK, "向量模型 bge-m3", "檔案齊全"))
    fp16 = os.path.join(models_dir, "bge-m3-fp16")
    if os.path.isfile(os.path.join(fp16, "model.safetensors")):
        out.append((OK, "向量模型 fp16 版", "有（有 NVIDIA 顯卡時會優先用，載入較快、佔顯存較少）"))
    else:
        out.append((WARN, "向量模型 fp16 版", "沒有；沒關係，會直接用 bge-m3（有 GPU 時載入較慢）"))
    rerank = os.path.join(models_dir, "bge-reranker-v2-m3")
    missing = _missing_files(rerank, RERANK_FILES) if os.path.isdir(rerank) else ["整個資料夾"]
    out.append((FAIL, "重排模型", "缺少：" + "、".join(missing)) if missing else (OK, "重排模型", "檔案齊全"))
    return out


def check_docs(docs_dir: str = paths.DOCS_DIR) -> List[Result]:
    found = []
    for _root, _dirs, files in os.walk(docs_dir):
        found += [f for f in files if f.lower().endswith(DOC_SUFFIXES)]
    if not found:
        return [(FAIL, "知識庫文件", f"{docs_dir} 裡沒有文件（PDF、Word、txt、md）")]
    return [(OK, "知識庫文件", f"{len(found)} 份")]


def check_database(db_path: str = paths.DB_PATH) -> List[Result]:
    if not os.path.isfile(db_path):
        return [(FAIL, "資料庫", f"找不到 {db_path}（出勤查詢會沒有資料）")]
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            users = conn.execute("SELECT COUNT(*) FROM Users").fetchone()[0]
            punches = conn.execute("SELECT COUNT(*) FROM Attendance").fetchone()[0]
            newest = conn.execute("SELECT MAX(timestamp) FROM Attendance").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error as e:
        return [(FAIL, "資料庫", f"打不開或格式不對：{type(e).__name__}")]
    note = f"員工 {users} 位、出勤 {punches} 筆，最新一筆 {newest or '無'}"
    return [(OK if users and punches else WARN, "資料庫", note)]


def check_rag_index(rag_dir: str = paths.RAG_DIR) -> List[Result]:
    missing = _missing_files(rag_dir, ("index.faiss", "chunks.json", "manifest.json"))
    if missing:
        return [(WARN, "知識庫向量庫",
                 "缺少 " + "、".join(missing) + "；第一次啟動會重新建立（要載入向量模型，需要幾分鐘）")]
    return [(OK, "知識庫向量庫", "已建立，啟動時只會補更新有變動的文件")]


def check_keys(keys_path: str = paths.REMOTE_KEYS_PATH) -> List[Result]:
    from rag import remote_access
    rows = remote_access.KeyStore(keys_path).describe()
    if not rows:
        return [(FAIL, "存取金鑰", "還沒有任何金鑰：python -m rag.remote_server --add-key 組員名稱")]
    now = time.time()
    live = sum(1 for _label, exp in rows if exp is None or exp > now)
    expired = len(rows) - live
    if not live:                                  # 只報數量，不印組員名稱或任何金鑰庫內容
        return [(FAIL, "存取金鑰", f"全部 {len(rows)} 組都已過期")]
    return [(OK, "存取金鑰", f"{live} 組有效／共 {len(rows)} 組" + (f"，{expired} 組已過期" if expired else ""))]


def check_api_key(call_api: bool = False) -> List[Result]:
    from rag import llm_settings
    settings = llm_settings.load_settings()
    if not llm_settings.effective_api_key(settings):
        return [(WARN, "雲端 API 金鑰", "沒有設定 NVIDIA_API_KEY；用 --llm api 啟動會被拒絕（設定方式見換機清單）")]
    out = [(OK, "雲端 API 金鑰", "已設定（不顯示內容）")]
    if call_api:
        ok, err, _text, secs = llm_settings.check_api(settings)
        out.append((OK, "雲端 API 連線", f"可用，{secs:.1f} 秒") if ok
                   else (FAIL, "雲端 API 連線", f"失敗：{getattr(err, 'kind', type(err).__name__)}"))
    else:
        out.append((WARN, "雲端 API 連線", "沒有實際呼叫；加 --api 才會測一次"))
    return out


def check_port(port: int) -> List[Result]:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1.0)
        busy = s.connect_ex(("127.0.0.1", port)) == 0
    return [(WARN if busy else OK, f"port {port}", "已被占用（舊的伺服器還在跑？）" if busy else "可用")]


def check_tailscale() -> List[Result]:
    exe = shutil.which("tailscale")
    if not exe:
        return [(WARN, "Tailscale", "找不到 tailscale 指令（要用公開網址才需要；只在區網或本機測試可略過）")]
    try:
        r = subprocess.run([exe, "status"], capture_output=True, text=True, timeout=10)  # noqa: S603  # nosec B603
    except (OSError, subprocess.SubprocessError) as e:
        return [(WARN, "Tailscale", f"無法執行 status：{type(e).__name__}")]
    if r.returncode != 0:
        return [(WARN, "Tailscale", "尚未登入或未啟動（tailscale up）")]
    return [(OK, "Tailscale", "已登入；對外網址要用 tailscale funnel 開啟後，從它顯示的網域填 --allowed-host")]


def check_gpu() -> List[Result]:
    try:
        import torch
        has = torch.cuda.is_available()
    except Exception as e:   # noqa: BLE001  torch 沒裝或損壞都只是提醒
        return [(WARN, "GPU", f"無法判斷：{type(e).__name__}")]
    return [(OK, "GPU", "偵測到 CUDA，向量模型會走 GPU") if has
            else (WARN, "GPU", "沒有 CUDA，向量與重排走 CPU，每題回答會慢一些（可用，先實測速度）")]


def run_all(port: int, call_api: bool) -> List[Result]:
    results: List[Result] = []
    for part in (check_python(), check_packages(), check_models(), check_docs(), check_database(), check_rag_index(),
                 check_keys(), check_api_key(call_api), check_port(port), check_tailscale(), check_gpu()):
        results += part
    return results


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="遠端測試入口換機後的就緒檢查（只讀）")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--api", action="store_true", help="實際對雲端 API 呼叫一次小請求")
    args = parser.parse_args(argv)
    results = run_all(args.port, args.api)
    for level, item, note in results:
        print(f"[{level}] {item}：{note}")
    fails = sum(1 for level, _i, _n in results if level == FAIL)
    warns = sum(1 for level, _i, _n in results if level == WARN)
    verdict = "請先處理「失敗」再啟動。" if fails else "沒有失敗項目，可以啟動。"
    print(f"\n失敗 {fails} 項、注意 {warns} 項。{verdict}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())

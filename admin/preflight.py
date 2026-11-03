"""
preflight.py：展示前的自動健檢（不啟動任何服務、不改任何資料）。

主要功能：
  - 逐項檢查：模型檔案、助理文件、資料庫與管理者、顯卡與記憶體、攝影機、通訊埠、語言模型設定、區網位址
  - 每項結果分成 通過／注意／失敗，附上白話的處理建議；全部沒有「失敗」才算可以展示
  - 所有外部依賴（檔案位置、顯卡查詢、攝影機、port）都可由參數傳入，測試不需要真的硬體
  - python -m admin.preflight 執行並印出中文結果；結束碼 0＝可以展示，1＝有失敗項

串接模組：
  - paths：模型、文件、資料庫位置
  - service_manager：port 檢查與區網 IP；llm_settings：語言模型設定與 Ollama 模型清單
  - resource_manager：剩餘顯存與記憶體
"""
import os
import shutil
import sqlite3
import sys
from contextlib import closing
from dataclasses import dataclass

from shared import paths

OK, WARN, FAIL = "通過", "注意", "失敗"
MARK = {OK: "[通過]", WARN: "[注意]", FAIL: "[失敗]"}

MIN_FREE_VRAM_MB = 3000     # 門禁（人臉＋YOLO）與助理同時使用的大致下限
MIN_FREE_RAM_MB = 2500
PORT_REGISTER = 5000


@dataclass
class Check:
    """一項檢查的結果。"""
    name: str
    status: str
    detail: str = ""
    fix: str = ""


def required_models(models_dir):
    """正式系統會用到的模型檔案或資料夾（與 vision_core、security、audio_player、rag.bootstrap 一致）。"""
    liveness = os.environ.get("ANTISPOOF_MODEL_PATH", "MiniFASNetV2.onnx")
    return [
        ("人臉特徵模型", os.path.join(models_dir, "arcface_r100_v1.onnx")),
        ("活體偵測模型", os.path.join(models_dir, liveness)),
        ("人數偵測模型（YOLO）", os.path.join(models_dir, "yolov8s.pt")),
        ("語音模型", os.path.join(models_dir, "vits-piper-zh_CN-huayan-medium", "zh_CN-huayan-medium.onnx")),
        ("文件向量模型", os.path.join(models_dir, "bge-m3")),
        ("文件重排模型", os.path.join(models_dir, "bge-reranker-v2-m3")),
    ]


def check_models(models_dir=None):
    """所有必要模型都在才通過；缺的會列出名稱。"""
    missing = [name for name, path in required_models(models_dir or paths.MODELS_DIR) if not os.path.exists(path)]
    if missing:
        return Check("模型檔案", FAIL, "缺少：" + "、".join(missing),
                     "把 models 資料夾補齊（模型不在 GitHub 上，要從原本的電腦複製過來）。")
    return Check("模型檔案", OK, "全部到齊")


def check_docs(docs_dir=None):
    """助理要讀的文件資料夾要有檔案，否則助理查不到任何法規。"""
    folder = docs_dir or paths.DOCS_DIR
    files = [f for f in os.listdir(folder) if not f.startswith(".")] if os.path.isdir(folder) else []
    if not files:
        return Check("助理文件", FAIL, f"{folder} 是空的或不存在", "把要讓助理查的 PDF、Word 放進 data/docs。")
    return Check("助理文件", OK, f"{len(files)} 份文件")


def check_database(db_path=None):
    """資料庫存在、有員工、且至少有一位啟用中的管理者（否則後台只能走初次設定）。"""
    path = db_path or paths.DB_PATH
    if not os.path.exists(path):
        return Check("資料庫", FAIL, "找不到資料庫", "先啟動一次後台建立資料庫，並用手機註冊至少一位員工。")
    try:
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=3)) as con:
            users = con.execute("SELECT COUNT(*) FROM Users WHERE is_active = 1 OR is_active IS NULL").fetchone()[0]
            admins = con.execute("SELECT COUNT(*) FROM Users WHERE role = 'admin' "
                                 "AND (is_active = 1 OR is_active IS NULL)").fetchone()[0]
    except sqlite3.Error as e:
        return Check("資料庫", FAIL, f"讀取失敗：{e}", "資料庫可能損壞，先不要展示，聯絡開發者。")
    if not users:
        return Check("資料庫", FAIL, "沒有任何員工", "用手機掃 QR Code 註冊至少一位員工。")
    if not admins:
        return Check("資料庫", WARN, f"{users} 位員工，但沒有管理者",
                     "登入後台時會進入初次設定，要先選一位員工當管理者。")
    return Check("資料庫", OK, f"{users} 位員工，{admins} 位管理者")


def check_gpu(free_vram_fn=None):
    """顯卡剩餘顯存；取不到代表沒有 NVIDIA 顯卡或驅動有問題。"""
    if free_vram_fn is None:
        from shared import resource_manager
        free_vram_fn = resource_manager._free_vram_mb
    free = free_vram_fn()
    if free is None:
        return Check("顯卡", WARN, "查不到 NVIDIA 顯卡", "沒有顯卡會改用 CPU，辨識會明顯變慢。")
    if free < MIN_FREE_VRAM_MB:
        return Check("顯卡", WARN, f"剩餘顯存只有 {free} MB", "關掉其他吃顯卡的程式（遊戲、瀏覽器影片、Ollama 模型）。")
    return Check("顯卡", OK, f"剩餘顯存 {free} MB")


def check_ram(free_ram_fn=None):
    """記憶體剩餘量。"""
    if free_ram_fn is None:
        from shared import resource_manager
        free_ram_fn = resource_manager._free_ram_mb
    free = free_ram_fn()
    if free is None:
        return Check("記憶體", WARN, "查不到剩餘記憶體")
    if free < MIN_FREE_RAM_MB:
        return Check("記憶體", WARN, f"剩餘記憶體只有 {free} MB", "關掉不用的程式與瀏覽器分頁，或重新開機後再展示。")
    return Check("記憶體", OK, f"剩餘記憶體 {free} MB")


def _open_camera_once():
    """試開預設鏡頭並讀一張畫面，回傳是否成功。"""
    import cv2
    cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
    try:
        return bool(cap.isOpened() and cap.read()[0])
    finally:
        cap.release()


def check_camera(open_fn=None):
    """鏡頭能開且讀得到畫面；被別的程式占用時會失敗。"""
    try:
        ok = (open_fn or _open_camera_once)()
    except Exception as e:  # 沒有 cv2 或驅動錯誤都算失敗，不讓健檢本身當掉
        return Check("攝影機", FAIL, f"無法使用：{e}", "確認鏡頭已接上，且沒有被門禁或其他程式占用。")
    if not ok:
        return Check("攝影機", FAIL, "開不起來或讀不到畫面",
                     "確認鏡頭已接上；若門禁或後台已開著，先把它們關掉再檢查（鏡頭一次只給一個程式用）。")
    return Check("攝影機", OK, "可以使用")


def check_ports(is_port_open=None):
    """註冊服務的 port 若已被占用，表示服務可能已經開著，或被別的程式佔走。"""
    if is_port_open is None:
        from admin import service_manager
        is_port_open = service_manager.is_port_open
    if is_port_open("127.0.0.1", PORT_REGISTER):
        return Check("註冊服務通訊埠", WARN, f"{PORT_REGISTER} 已有程式在使用",
                     "若是上次沒關掉的註冊服務，先在後台停止；否則手機註冊可能連不上。")
    return Check("註冊服務通訊埠", OK, f"{PORT_REGISTER} 可用")


def check_llm(settings=None, list_models=None, which=shutil.which):
    """助理用的語言模型：雲端模式要有金鑰；本機模式要有 Ollama 與設定的模型。"""
    from rag import llm_settings as ls
    s = settings if settings is not None else ls.load_settings()
    if s.get("mode") == "api":
        if not ls.effective_api_key(s):
            return Check("助理語言模型", FAIL, "選了雲端模式但沒有金鑰",
                         "設定環境變數 NVIDIA_API_KEY，或改用本機模式。")
        return Check("助理語言模型", OK, f"雲端模型 {s.get('api_model', '')}（金鑰已設定，未實際呼叫）")
    model = s.get("local_model", "")
    ok, names = (list_models or ls.list_local_models)(s)
    if ok:
        if not ls._model_installed(model, names):
            return Check("助理語言模型", FAIL, f"Ollama 已開，但沒有安裝 {model}", f"執行：ollama pull {model}")
        return Check("助理語言模型", OK, f"Ollama 已開，{model} 已安裝")
    if not which("ollama"):
        return Check("助理語言模型", FAIL, "電腦上找不到 Ollama", "安裝 Ollama（https://ollama.com），再下載設定的模型。")
    return Check("助理語言模型", OK, "Ollama 已安裝，目前沒開（按後台「管理者助理」時會自動啟動）")


def check_network(get_ip=None):
    """顯示手機註冊要連的區網位址；找不到代表沒連上網路。"""
    if get_ip is None:
        from admin import service_manager
        get_ip = service_manager.get_local_ip_offline
    ip = get_ip()
    if not ip or ip.startswith("127."):
        return Check("區網位址", WARN, "找不到區網位址", "把筆電連上 Wi-Fi 或開行動熱點，手機才能註冊。")
    return Check("區網位址", OK, f"{ip}（手機要連同一個網路）")


def run_all():
    """依序執行所有檢查；單項檢查出意外也只算該項失敗，不中斷其他項。"""
    items = [check_models, check_docs, check_database, check_gpu, check_ram, check_camera, check_ports, check_llm,
             check_network]
    results = []
    for fn in items:
        try:
            results.append(fn())
        except Exception as e:  # 健檢本身不能當掉
            results.append(Check(fn.__name__.replace("check_", ""), FAIL, f"檢查時出錯：{e}"))
    return results


def summarize(results):
    """整理成要印出的中文文字與結束碼（有任何「失敗」就是 1）。"""
    lines = []
    for r in results:
        lines.append(f"{MARK[r.status]} {r.name}：{r.detail}")
        if r.status != OK and r.fix:
            lines.append(f"        處理方式：{r.fix}")
    fails = sum(r.status == FAIL for r in results)
    warns = sum(r.status == WARN for r in results)
    lines.append("")
    if fails:
        lines.append(f"結論：有 {fails} 項失敗，現在還不能展示，請先處理上面標 [失敗] 的項目。")
    elif warns:
        lines.append(f"結論：可以展示，但有 {warns} 項要留意（標 [注意]）。")
    else:
        lines.append("結論：全部通過，可以展示。")
    return "\n".join(lines), (1 if fails else 0)


def main():
    """命令列入口：印出結果並以結束碼回報。"""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print("展示前健檢中，請稍候…（會短暫開一下鏡頭）\n")
    text, code = summarize(run_all())
    print(text)
    return code


if __name__ == "__main__":
    sys.exit(main())

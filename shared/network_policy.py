"""
network_policy.py：「全本地模式」開關——開啟後系統不主動連外網（純邏輯，沒有網路相依）。

主要功能：
  - local_only()：目前是不是全本地模式。環境變數 SMART_ACCESS_LOCAL_ONLY（1、true、yes、on 開；0、false、no、off 關）
    優先，其次讀 runtime/network_policy.json 的 local_only，都沒有就是關（維持原本行為）
  - set_local_only(value)：寫入設定檔（先寫暫存檔再取代）；環境變數有設時，檔案內容不會改變實際結果，畫面要提示
  - 強制點在「對外連線的出口」而不是各個畫面：llm_settings（雲端 API 一律改用本機模型，直接呼叫雲端會被拒絕）、
    news_feed（晨報不連網抓新聞）
  - 不在範圍：使用者手動啟動的遠端測試入口（rag.remote_server，是對外提供服務，不是主動連外）、
    手動執行的 ollama pull 與 pip；本機模型的位址若設成別台電腦，也不會被這個開關擋下

串接模組：
  - rag.llm_settings、admin.news_feed：出口檢查；admin.schedule_window：畫面上的開關
"""
import json
import os

from shared import paths

ENV_NAME = "SMART_ACCESS_LOCAL_ONLY"
_TRUE, _FALSE = ("1", "true", "yes", "on"), ("0", "false", "no", "off")


class OfflineError(RuntimeError):
    """全本地模式下有程式要連外網。"""


def env_override():
    """環境變數的設定：True、False，沒設或看不懂回傳 None。"""
    value = (os.environ.get(ENV_NAME) or "").strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    return None


def local_only(path=None):
    """目前是不是全本地模式；讀不到或格式不對一律當作關閉。"""
    forced = env_override()
    if forced is not None:
        return forced
    try:
        with open(path or paths.NETWORK_POLICY_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and data.get("local_only") is True


def set_local_only(value, path=None):
    """寫入設定檔。"""
    target = path or paths.NETWORK_POLICY_PATH
    tmp = target + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"local_only": bool(value)}, f)
    os.replace(tmp, target)


def require_online(what):
    """要連外網前呼叫；全本地模式就丟 OfflineError。"""
    if local_only():
        raise OfflineError(f"全本地模式已開啟，不連外網：{what}")

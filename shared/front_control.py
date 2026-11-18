"""
front_control.py：後台對前台的遙控開關——「前台人臉辨識要不要跑」與「前台知識庫問答開不開放」（純邏輯，沒有網路相依）。

主要功能：
  - recognition_enabled()：後台是否允許前台跑人臉辨識；缺檔、壞檔、格式不對一律當作允許（維持原本行為）
  - set_recognition_enabled(value)：寫入 runtime/front_control.json（先寫暫存檔再取代，讀的人不會讀到一半）
  - talk_enabled()／set_talk_enabled(value)：前台 Smart_Chat 一般對話（獨立開關，預設開放；後台明確關閉才停）
  - chat_enabled()／set_chat_enabled(value)：前台知識庫問答（Smart_RAG，內部沿用 chat 舊名）；**預設開放**
    （後台關閉才停），
    沒有設定檔或沒寫這一項當作開放，檔案壞掉、值不是布林一律當作關閉；與辨識開關存在同一個檔案，互不影響
  - 方向不變：後台只是把狀態放在檔案裡，前台每次同步（約 10 秒）從 /v1/morning 取得；後台從不連前台

串接模組：
  - admin.link_server：同步回應帶 recognition_enabled、chat_enabled（只有布林）；/v1/chat 先檢查 chat_enabled
  - admin.backend_main：營運狀態頁「前台連線」卡片的停止／恢復按鈕與「前台知識庫問答」開關
  - shared.paths：FRONT_CONTROL_PATH
"""
import json
import os

from shared import paths


def recognition_enabled(path=None):
    """後台是否允許前台人臉辨識；讀不到或格式不對一律當作允許。"""
    try:
        with open(path or paths.FRONT_CONTROL_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return True
    return not (isinstance(data, dict) and data.get("recognition_enabled") is False)


def _load(path=None):
    try:
        with open(path or paths.FRONT_CONTROL_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _open_by_default(key, path=None):
    """預設開放的開關：沒有設定檔或檔案裡沒有這一項就是開放；後台明確寫 false 才關；檔案壞掉、格式不對一律關閉。"""
    try:
        with open(path or paths.FRONT_CONTROL_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return True
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    value = data.get(key, True)
    return value if isinstance(value, bool) else False


def _save(data, path=None):
    target = path or paths.FRONT_CONTROL_PATH
    os.makedirs(os.path.dirname(target), exist_ok=True)
    tmp = target + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, target)


def set_recognition_enabled(value, path=None):
    """寫入設定檔（保留其他開關的值）。"""
    _save({**_load(path), "recognition_enabled": bool(value)}, path)


def chat_enabled(path=None):
    """後台是否開放前台知識庫問答；預設開放；後台明確關閉、或設定檔壞掉才停。"""
    return _open_by_default("chat_enabled", path)


def set_chat_enabled(value, path=None):
    """寫入設定檔（保留其他開關的值）。"""
    _save({**_load(path), "chat_enabled": bool(value)}, path)


def talk_enabled(path=None):
    """後台是否開放前台 Smart_Chat 一般對話（只有語言模型，沒有文件與資料）；預設開放，和知識庫問答是兩個獨立開關。"""
    return _open_by_default("talk_enabled", path)


def set_talk_enabled(value, path=None):
    """寫入設定檔（保留其他開關的值）。"""
    _save({**_load(path), "talk_enabled": bool(value)}, path)


def verify_request(path=None):
    """後台要前台自動驗證的請求編號（整數，0 表示沒有請求）；前台同步時取得，編號變大就跑一次自動驗證並回傳報告。"""
    value = _load(path).get("verify_request")
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def request_verify(path=None, now=None):
    """後台下達一次前台自動驗證，回傳新的請求編號（用目前的整數秒，保證越來越大）。"""
    import time
    stamp = max(int(now if now is not None else time.time()), verify_request(path) + 1)
    _save({**_load(path), "verify_request": stamp}, path)
    return stamp

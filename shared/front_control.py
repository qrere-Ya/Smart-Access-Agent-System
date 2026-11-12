"""
front_control.py：後台對前台的遙控開關——目前只有「前台人臉辨識要不要跑」（純邏輯，沒有網路相依）。

主要功能：
  - recognition_enabled()：後台是否允許前台跑人臉辨識；缺檔、壞檔、格式不對一律當作允許（維持原本行為）
  - set_recognition_enabled(value)：寫入 runtime/front_control.json（先寫暫存檔再取代，讀的人不會讀到一半）
  - 方向不變：後台只是把狀態放在檔案裡，前台每次同步（約 10 秒）從 /v1/morning 取得；後台從不連前台

串接模組：
  - admin.link_server：同步回應帶 recognition_enabled（只有布林）
  - admin.backend_main：營運狀態頁「前台連線」卡片的停止／恢復按鈕
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


def set_recognition_enabled(value, path=None):
    """寫入設定檔。"""
    target = path or paths.FRONT_CONTROL_PATH
    os.makedirs(os.path.dirname(target), exist_ok=True)
    tmp = target + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"recognition_enabled": bool(value)}, f)
    os.replace(tmp, target)

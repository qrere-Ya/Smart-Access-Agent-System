"""
stats.py：評估用的小型統計工具（只用標準函式庫）。

主要功能：
  - wilson：二項比例的 Wilson 信賴區間（樣本小時比常見的常態近似可靠）
  - 以「題目」為單位計算：同一題重複問幾次不算獨立樣本，所以一題要每次都對才算通過，區間以題數計

串接模組：
  - tool_eval：意圖判斷選工具的評估
"""
import math
from typing import Tuple


def wilson(passed: int, total: int, z: float = 1.96) -> Tuple[float, float]:
    """比例 passed/total 的 Wilson 信賴區間（預設 95%）；total 為 0 時回傳 (0.0, 1.0)。"""
    if total <= 0:
        return 0.0, 1.0
    p = passed / total
    denom = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denom
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denom
    return max(0.0, center - half), min(1.0, center + half)

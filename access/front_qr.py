"""
front_qr.py：把新聞原文網址做成 QR Code 圖片，給前台儀表板顯示、讓人用手機掃（前台本身不上網，也不開瀏覽器）。

主要功能：
  - matrix(url)：QR 的 0／1 矩陣（pyqrcode，純 Python，容錯等級 M）
  - image(url, size)：畫成正方形的 PIL 圖片（黑白、留白邊），size 是邊長像素；網址太長產生失敗回傳 None

串接模組：
  - access.front_dashboard：每則新聞卡片右側的 QR；shared.link_check 先過濾過，這裡不再判斷網址安全
"""
import pyqrcode
from PIL import Image, ImageDraw

MAX_URL = 400          # 超過這個長度的網址不做 QR（手機掃起來也太密）
QUIET = 2              # 四周留白的模組數


def matrix(url):
    """回傳 QR 的 0／1 二維串列；網址空白或太長回傳 None。"""
    text = str(url or "").strip()
    if not text or len(text) > MAX_URL:
        return None
    try:
        return pyqrcode.create(text, error="M").code
    except (ValueError, TypeError):
        return None


def image(url, size=150):
    """畫成 size×size 的黑白 PIL 圖片；做不出來回傳 None。"""
    code = matrix(url)
    if not code:
        return None
    modules = len(code) + QUIET * 2
    cell = max(1, size // modules)
    side = cell * modules
    img = Image.new("RGB", (side, side), "white")
    draw = ImageDraw.Draw(img)
    for row, line in enumerate(code):
        for col, bit in enumerate(line):
            if bit:
                x, y = (col + QUIET) * cell, (row + QUIET) * cell
                draw.rectangle((x, y, x + cell - 1, y + cell - 1), fill="black")
    return img.resize((size, size), Image.NEAREST) if side != size else img

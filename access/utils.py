"""
utils.py：畫面繪製小工具。

主要功能：
  - put_chinese_text：在 OpenCV 影像上繪製中文字（OpenCV 本身不支援中文，改用 PIL 繪製）
  - 字型載入後快取，避免每幀重複讀取字型檔

串接模組：
  - terminal_app：繪製辨識介面文字
"""
import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

# 以字體大小為 key 的字型快取
_FONT_CACHE = {}

def _get_font(font_size):
    """回傳快取的字型物件，同一個大小只從硬碟讀取一次。"""
    font = _FONT_CACHE.get(font_size)
    if font is not None:
        return font

    try:
        font = ImageFont.truetype("msjh.ttc", font_size)
    except IOError:
        # 找不到微軟正黑體，改用預設字體
        print("找不到微軟正黑體，將使用預設字體")
        font = ImageFont.load_default()

    _FONT_CACHE[font_size] = font
    return font

def put_chinese_text(img, text, position, text_color=(0, 255, 0), font_size=30):
    """在 BGR 影像上繪製中文字，回傳繪製後的新影像。"""
    # OpenCV 是 BGR、PIL 是 RGB，先轉換
    img_rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)

    pil_img = Image.fromarray(img_rgb)
    draw = ImageDraw.Draw(pil_img)

    font = _get_font(font_size)

    # 顏色由 (B, G, R) 轉成 (R, G, B)
    b, g, r = text_color
    pil_color = (r, g, b)

    draw.text(position, text, font=font, fill=pil_color)

    cv2_img = cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)

    return cv2_img

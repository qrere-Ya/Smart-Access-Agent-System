import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

# 【效能優化，2026-09-02】字型快取。
# 原本每呼叫一次 put_chinese_text() 就會重新 ImageFont.truetype() 從硬碟讀一次字型檔，
# 而這個函式在 terminal_app.py 的主迴圈裡，同一幀畫面會被呼叫 5-6 次，一秒鐘抓
# 15-30 幀，等於一秒鐘重複讀同一個字型檔 75-180 次以上。字型檔案的內容不會變，
# 讀一次存起來重複用即可，這裡用 (font_size) 當 key 快取已經載入過的 ImageFont 物件。
_FONT_CACHE = {}

def _get_font(font_size):
    """回傳快取好的 ImageFont 物件，同一個 font_size 只會真的從硬碟讀取一次。"""
    font = _FONT_CACHE.get(font_size)
    if font is not None:
        return font

    try:
        font = ImageFont.truetype("msjh.ttc", font_size)
    except IOError:
        # 找不到微軟正黑體，使用預設字體
        print("找不到微軟正黑體，將使用預設字體")
        font = ImageFont.load_default()

    _FONT_CACHE[font_size] = font
    return font

def put_chinese_text(img, text, position, text_color=(0, 255, 0), font_size=30):
    # OpenCV 的圖片是 BGR 格式，PIL 需要 RGB 格式，所以先轉換
    img_rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)

    # 將 Numpy 陣列轉換成 PIL 影像物件
    pil_img = Image.fromarray(img_rgb)
    draw = ImageDraw.Draw(pil_img)

    font = _get_font(font_size)

    # OpenCV 的顏色是 (B, G, R)，但 PIL 畫圖需要 (R, G, B)，我們把它反轉過來
    b, g, r = text_color
    pil_color = (r, g, b)

    # 在 PIL 影像上畫出中文字
    draw.text(position, text, font=font, fill=pil_color)

    # 將畫好文字的 PIL 影像，轉回 Numpy 陣列，再轉回 BGR 給 OpenCV
    cv2_img = cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)
    
    return cv2_img
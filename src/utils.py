import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

def put_chinese_text(img, text, position, text_color=(0, 255, 0), font_size=30):
    # OpenCV 的圖片是 BGR 格式，PIL 需要 RGB 格式，所以先轉換
    img_rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)

    # 將 Numpy 陣列轉換成 PIL 影像物件
    pil_img = Image.fromarray(img_rgb)
    draw = ImageDraw.Draw(pil_img)

    try:
        font = ImageFont.truetype("msjh.ttc", font_size)
    except IOError:
        # 找不到微軟正黑體，使用預設字體
        print("找不到微軟正黑體，將使用預設字體")
        font = ImageFont.load_default()
    
    # OpenCV 的顏色是 (B, G, R)，但 PIL 畫圖需要 (R, G, B)，我們把它反轉過來
    b, g, r = text_color
    pil_color = (r, g, b)

    # 在 PIL 影像上畫出中文字
    draw.text(position, text, font=font, fill=pil_color)

    # 將畫好文字的 PIL 影像，轉回 Numpy 陣列，再轉回 BGR 給 OpenCV
    cv2_img = cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)
    
    return cv2_img
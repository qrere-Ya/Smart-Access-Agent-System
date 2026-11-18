"""
tech_art.py：科技風介面的裝飾圖片（用 Pillow 畫，亮色與暗色外觀各一張，前後台共用，不上網、不裝新套件）。

主要功能：
  - badge：圓形發光徽章（柔光外圈、霓虹圓環、深色圓底、中間放 SVG 雙色圖示），功能卡片的圖示用
  - card_bg：功能卡片的底圖（圓角、純色底、細邊線、四角 HUD 括號；hover 版邊線與括號改成強調色），卡片整張當成一個圖片
  - neon_line：把漸層細線放進會裁切的外框，標題與區塊底線用
  - gradient_line：霓虹漸層細線（兩端淡出），標題底線與區塊分隔用；寬度可以超過容器，由外層框裁切
  - 所有顏色取自 shared.ui_theme 的 token（accent、glow、primary、card、border…），切換外觀自動換色
  - 結果都有快取；畫不出來回傳 None（畫面少一個裝飾，不會當掉）

串接模組：
  - shared.svg_icon：圖示與 make_ctk_image；shared.ui_theme：顏色 token
  - access.front_dashboard、admin.backend_main：功能卡片、標題、區塊線條
"""
import math

from PIL import Image, ImageDraw, ImageFilter

from shared import svg_icon, ui_theme

SCALE = 3


def _hex(value):
    value = value.lstrip("#")
    return tuple(int(value[k:k + 2], 16) for k in (0, 2, 4))


def _mix(a, b, t):
    """兩個 #rrggbb 色碼以 t（0 到 1）混合。"""
    ca, cb = _hex(a), _hex(b)
    return tuple(round(x + (y - x) * t) for x, y in zip(ca, cb, strict=True))


def _token(mode, name):
    return ui_theme.COLORS[mode][name]


def _badge_image(mode, name, size, accent):
    pad = size // 5
    total = size + 2 * pad
    big = total * SCALE
    card = _token(mode, "card")
    text = _token(mode, "text")
    glow = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    c = big // 2
    r = size * SCALE // 2
    gd.ellipse((c - r, c - r, c + r, c + r), fill=(*_hex(accent), 200 if mode == "dark" else 120))
    glow = glow.filter(ImageFilter.GaussianBlur(pad * SCALE * 0.55))
    base = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    bd = ImageDraw.Draw(base)
    core = _mix(card, accent, 0.22 if mode == "dark" else 0.10)
    bd.ellipse((c - r, c - r, c + r, c + r), fill=(*core, 255))
    ring = max(2, SCALE * 2)
    bd.ellipse((c - r, c - r, c + r, c + r), outline=(*_hex(accent), 255), width=ring)
    inner = r - ring * 3
    bd.ellipse((c - inner, c - inner, c + inner, c + inner), outline=(*_hex(accent), 70), width=max(1, SCALE // 2))
    for k in range(4):                                   # 圓環上的四個刻度
        ang = k * math.pi / 2 + math.pi / 4
        x1, y1 = c + math.cos(ang) * (r - ring), c + math.sin(ang) * (r - ring)
        x2, y2 = c + math.cos(ang) * (r + ring), c + math.sin(ang) * (r + ring)
        bd.line((x1, y1, x2, y2), fill=(*_hex(text), 230), width=ring)
    image = Image.alpha_composite(glow, base).resize((total, total), Image.LANCZOS)
    icon_size = round(size * 0.56)
    icon = svg_icon.render(name, icon_size, text, accent)
    if icon is None:
        return None
    image.alpha_composite(icon, ((total - icon_size) // 2, (total - icon_size) // 2))
    return image


def badge(name, size, accent_token, master=None):
    """圓形發光徽章（含柔光外圈，實際圖片邊長是 size 的 1.4 倍）；accent_token 是 ui_theme 的顏色 token 名稱。"""
    images = []
    for mode in ui_theme.MODES:
        accent = _token(mode, accent_token)
        image = _badge_image(mode, name, size, accent)
        if image is None:
            return None
        images.append(image)
    shown = images[0].size[0]
    return svg_icon.make_ctk_image(images[0], images[1], shown, master)


def _frame_image(mode, w, h, accent, hover):
    bw, bh = w * SCALE, h * SCALE
    card, border = _token(mode, "card"), _token(mode, "border")
    image = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
    radius = 14 * SCALE
    mask = Image.new("L", (bw, bh), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, bw - 1, bh - 1), radius, fill=255)
    # 純色底：上面的文字元件才能用同色底接得起來
    image.paste(Image.new("RGBA", (bw, bh), (*_hex(card), 255)), (0, 0), mask)
    draw = ImageDraw.Draw(image)
    edge = accent if hover else "#%02x%02x%02x" % _mix(border, accent, 0.4)     # 平常邊線帶一點模組色，hover 全亮
    edge_width = SCALE * (2 if hover else 1)
    draw.rounded_rectangle((0, 0, bw - 1, bh - 1), radius, outline=(*_hex(edge), 255), width=edge_width)
    arm, thick, inset = 26 * SCALE, 3 * SCALE, 8 * SCALE
    bright = (*_hex(accent), 255)
    for sx, sy in ((1, 1), (-1, 1), (1, -1), (-1, -1)):          # 四角 HUD 括號
        x0 = inset if sx > 0 else bw - inset
        y0 = inset if sy > 0 else bh - inset
        draw.line((x0, y0, x0 + sx * arm, y0), fill=bright, width=thick)
        draw.line((x0, y0, x0, y0 + sy * arm), fill=bright, width=thick)
    draw.line((bw // 2 - 24 * SCALE, bh - 3 * SCALE, bw // 2 + 24 * SCALE, bh - 3 * SCALE),
              fill=(*_hex(accent), 255), width=SCALE * 2)          # 底部中間的亮條
    return image.resize((w, h), Image.LANCZOS)


def card_bg(w, h, accent_token, hover=False, master=None):
    """功能卡片的整張底圖（尺寸 w×h，已含 HUD 括號）；hover 版邊線改成強調色。"""
    images = [_frame_image(mode, w, h, _token(mode, accent_token), hover) for mode in ui_theme.MODES]
    return svg_icon.make_ctk_image(images[0], images[1], (w, h), master)


def _line_image(mode, w, h, left, right):
    image = Image.new("RGBA", (w, h))
    draw = ImageDraw.Draw(image)
    for x in range(w):
        t = x / max(1, w - 1)
        fade = min(1.0, t / 0.06, (1 - t) / 0.12)                 # 兩端淡出
        color = _mix(_token(mode, left), _token(mode, right), t)
        draw.line((x, 0, x, h), fill=(*color, round(255 * max(0.0, fade))))
    glow = image.filter(ImageFilter.GaussianBlur(max(1, h)))
    return Image.alpha_composite(glow, image)


def gradient_line(w, h, left="glow", right="accent", master=None):
    """霓虹漸層細線（w×h，由 left 色漸變到 right 色，兩端淡出）。"""
    images = [_line_image(mode, w, h, left, right) for mode in ui_theme.MODES]
    return svg_icon.make_ctk_image(images[0], images[1], (w, h), master)


def neon_line(master, height=4, width=1920):
    """霓虹漸層細線的外框：圖片比容器寬，由外層框裁切，所以視窗多寬都剛好；回傳外框（自己決定怎麼擺）。"""
    import customtkinter as ctk

    box = ctk.CTkFrame(master, height=height, fg_color="transparent")
    box.pack_propagate(False)
    label = ctk.CTkLabel(box, text="", image=gradient_line(width, height, master=box), height=height)
    label.place(x=0, y=0)
    return box

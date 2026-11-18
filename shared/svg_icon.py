"""
svg_icon.py：把 assets/icons 裡自己畫的 SVG 圖示轉成畫面可用的圖片（後台與前台共用，只用 Pillow，不上網、不裝新套件）。

主要功能：
  - render：讀一個圖示、畫成指定大小與顏色的透明底 PIL 圖片（放大 8 倍繪製再縮小）；支援雙色（主色加 accent 強調色）、
    半透明填色與外圈柔光（glow）
  - ctk_icon：回傳 customtkinter 的 CTkImage，亮色與暗色外觀各一張，切換外觀自動換色
  - 只支援本專案圖示用到的 SVG 子集：viewBox、path（M L H V C Q Z 的大寫絕對座標）、circle、ellipse、rect（含 rx）、
    line、polyline、polygon；線條粗細、圓頭圓角、fill／stroke 屬性（currentColor 換成指定顏色）
  - 檔案有 DOCTYPE 或 ENTITY 一律拒絕；圖示名稱只允許英數與底線，不會讀到資料夾外的檔案
  - 讀不到或畫不出來就回傳 None（畫面少一個圖示，不會當掉）

串接模組：
  - shared.paths：ICONS_DIR；shared.ui_theme：顏色 pair；admin.backend_main、access.front_dashboard：功能卡片的圖示
"""
import os
import re
import xml.etree.ElementTree as ET  # nosec B405  只讀專案自己的圖示檔，且先拒絕 DOCTYPE／ENTITY

from PIL import Image, ImageDraw, ImageFilter

from shared import paths

SCALE = 8
_NAME = re.compile(r"^[A-Za-z0-9_]{1,40}$")
_NUMBER = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)")
_TOKEN = re.compile(r"[MLHVCQZmlhvcqz]|[-+]?(?:\d+\.?\d*|\.\d+)")
_cache = {}


def _tag(element):
    return element.tag.rsplit("}", 1)[-1]


def _num(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _bezier(points, steps=16):
    n = len(points) - 1
    out = []
    for i in range(steps + 1):
        t = i / steps
        coeffs = [1.0]
        for _ in range(n):
            coeffs = [a + b for a, b in zip([0] + coeffs, coeffs + [0], strict=True)]
        x = sum(c * (1 - t) ** (n - k) * t ** k * p[0] for k, (c, p) in enumerate(zip(coeffs, points, strict=True)))
        y = sum(c * (1 - t) ** (n - k) * t ** k * p[1] for k, (c, p) in enumerate(zip(coeffs, points, strict=True)))
        out.append((x, y))
    return out


def parse_path(data):
    """把 path 的 d 字串變成子路徑清單：[(點清單, 是否封閉)]；不認得的指令回傳 None。"""
    tokens = _TOKEN.findall(data or "")
    subpaths, current, closed = [], [], False
    cmd, i, pos, start = None, 0, (0.0, 0.0), (0.0, 0.0)

    def take(count):
        nonlocal i
        values = tokens[i:i + count]
        if len(values) < count or any(_NUMBER.fullmatch(v) is None for v in values):
            raise ValueError("path 參數不足")
        i += count
        return [float(v) for v in values]

    try:
        while i < len(tokens):
            if tokens[i].isalpha():
                cmd = tokens[i]
                i += 1
                if cmd in "zZ":
                    if current:
                        subpaths.append((current, True))
                    current, pos = [], start
                    continue
            if cmd is None or cmd not in "MLHVCQ":
                return None
            if cmd == "M":
                if current:
                    subpaths.append((current, False))
                pos = start = tuple(take(2))
                current = [pos]
                cmd = "L"
            elif cmd == "L":
                pos = tuple(take(2))
                current.append(pos)
            elif cmd == "H":
                pos = (take(1)[0], pos[1])
                current.append(pos)
            elif cmd == "V":
                pos = (pos[0], take(1)[0])
                current.append(pos)
            elif cmd == "C":
                a, b, c = (tuple(take(2)) for _ in range(3))
                current.extend(_bezier([pos, a, b, c])[1:])
                pos = c
            elif cmd == "Q":
                a, b = (tuple(take(2)) for _ in range(2))
                current.extend(_bezier([pos, a, b])[1:])
                pos = b
        if current:
            subpaths.append((current, closed))
    except ValueError:
        return None
    return subpaths


def _style(element, root_style):
    """元素的 fill、stroke、粗細（自己沒寫就沿用 svg 根元素）。"""
    def pick(name, default):
        return element.get(name, root_style.get(name, default))
    return pick("fill", "none"), pick("stroke", "none"), _num(pick("stroke-width", "1"), 1.0)


def _shapes(element):
    """元素 → [(點清單, 是否封閉)]，或 None 表示不支援。"""
    kind = _tag(element)
    g = element.get
    if kind == "path":
        return parse_path(g("d"))
    if kind in ("circle", "ellipse"):
        cx, cy = _num(g("cx")), _num(g("cy"))
        rx = _num(g("r")) if kind == "circle" else _num(g("rx"))
        ry = rx if kind == "circle" else _num(g("ry"))
        import math
        return [([(cx + rx * math.cos(a * math.pi / 32), cy + ry * math.sin(a * math.pi / 32)) for a in range(64)],
                 True)]
    if kind == "rect":
        x, y, w, h = _num(g("x")), _num(g("y")), _num(g("width")), _num(g("height"))
        r = min(_num(g("rx"), _num(g("ry"))), w / 2, h / 2)
        if r <= 0:
            return [([(x, y), (x + w, y), (x + w, y + h), (x, y + h)], True)]
        import math
        pts = []
        for ccx, ccy, start in ((x + w - r, y + r, -90), (x + w - r, y + h - r, 0),
                                (x + r, y + h - r, 90), (x + r, y + r, 180)):
            pts.extend((ccx + r * math.cos(math.radians(start + k * 90 / 8)),
                        ccy + r * math.sin(math.radians(start + k * 90 / 8))) for k in range(9))
        return [(pts, True)]
    if kind == "line":
        return [([(_num(g("x1")), _num(g("y1"))), (_num(g("x2")), _num(g("y2")))], False)]
    if kind in ("polyline", "polygon"):
        nums = [float(v) for v in _NUMBER.findall(g("points", ""))]
        pts = list(zip(nums[0::2], nums[1::2], strict=False))
        return [(pts, kind == "polygon")] if len(pts) >= 2 else None
    return None


def _hex(value):
    value = value.lstrip("#")
    return tuple(int(value[k:k + 2], 16) for k in (0, 2, 4))


def _alpha(element, root_style, name):
    """元素的透明度（fill-opacity／stroke-opacity 乘上 opacity），0 到 1。"""
    def num(key):
        raw = element.get(key, root_style.get(key))
        return min(1.0, max(0.0, _num(raw, 1.0))) if raw is not None else 1.0
    return num(name) * num("opacity")


def render(name, size, color, accent=None, glow=False):
    """畫出圖示 name（assets/icons/<name>.svg），回傳 RGBA 圖片；失敗回傳 None。
    color 取代 currentColor、accent 取代 accent（沒給就和 color 一樣，也就是單色）；支援 fill-opacity／stroke-opacity／
    opacity（雙色圖示的半透明填色）。glow=True 時在圖示後面加一圈同強調色的柔光，圖片邊長變成 size 的 1.5 倍。"""
    if not _NAME.match(str(name)):
        return None
    accent = accent or color
    key = (name, size, color, accent, glow)
    if key in _cache:
        return _cache[key]
    try:
        with open(os.path.join(paths.ICONS_DIR, name + ".svg"), encoding="utf-8") as f:
            text = f.read(64 * 1024)
        if "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
            return None
        root = ET.fromstring(text)  # nosec B314  # noqa: S314  內容已先檢查沒有 DOCTYPE／ENTITY
    except (OSError, ValueError, ET.ParseError):
        return None
    box = [float(v) for v in (root.get("viewBox") or "0 0 24 24").replace(",", " ").split()]
    if len(box) != 4 or box[2] <= 0 or box[3] <= 0:
        return None
    big = size * SCALE
    factor = big / max(box[2], box[3])
    root_style = {k: root.get(k) for k in ("fill", "stroke", "stroke-width", "opacity") if root.get(k) is not None}
    image = Image.new("RGBA", (big, big), (0, 0, 0, 0))

    def to_px(points):
        return [((x - box[0]) * factor, (y - box[1]) * factor) for x, y in points]

    def paint(value, alpha):
        if value in ("none", None):
            return None
        rgb = _hex(accent if value == "accent" else color)
        return (*rgb, round(255 * alpha))

    for element in root.iter():
        if element is root or _tag(element) in ("title", "desc", "g"):
            continue
        shapes = _shapes(element)
        if shapes is None:
            return None
        fill, stroke, width = _style(element, root_style)
        fill_rgba = paint(fill, _alpha(element, root_style, "fill-opacity"))
        stroke_rgba = paint(stroke, _alpha(element, root_style, "stroke-opacity"))
        layer = Image.new("RGBA", (big, big), (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)
        w = max(1, round(width * factor))
        for points, closed in shapes:
            px = to_px(points)
            if closed and fill_rgba and len(px) >= 3:
                draw.polygon(px, fill=fill_rgba)
            if stroke_rgba and len(px) >= 2:
                line = px + [px[0]] if closed else px
                draw.line(line, fill=stroke_rgba, width=w, joint="curve")
                for x, y in (px if not closed else px[::4]):       # 圓頭圓角：端點與轉角補圓
                    draw.ellipse((x - w / 2, y - w / 2, x + w / 2, y + w / 2), fill=stroke_rgba)
        image = Image.alpha_composite(image, layer)
    result = image.resize((size, size), Image.LANCZOS)
    if glow:
        pad = size // 4
        canvas = Image.new("RGBA", (size + 2 * pad, size + 2 * pad), (0, 0, 0, 0))
        halo = Image.new("RGBA", canvas.size, (*_hex(accent), 0))
        mask = Image.new("L", canvas.size, 0)
        mask.paste(result.split()[3], (pad, pad))
        mask = mask.filter(ImageFilter.GaussianBlur(max(2, size // 7))).point(lambda v: min(255, v * 2))
        halo.putalpha(mask.point(lambda v: v * 0.75))
        canvas = Image.alpha_composite(canvas, halo)
        canvas.alpha_composite(result, (pad, pad))
        result = canvas
    _cache[key] = result
    return result


def make_ctk_image(light, dark, size, master=None):
    """把亮色、暗色兩張 PIL 圖片包成 CTkImage（size 是邊長或 (寬, 高)）；
    有給 master 就先用它所屬的視窗建好圖片（同一個行程有不只一個 Tk 視窗時，圖片要綁對視窗，只有測試會這樣）。"""
    import customtkinter as ctk
    from PIL import ImageTk
    size = (size, size) if isinstance(size, int) else tuple(size)
    image = ctk.CTkImage(light_image=light, dark_image=dark, size=size)
    if master is not None:
        try:
            scaled = image._get_scaled_size(ctk.ScalingTracker.get_widget_scaling(master))
            image._scaled_light_photo_images[scaled] = ImageTk.PhotoImage(light.resize(scaled), master=master)
            image._scaled_dark_photo_images[scaled] = ImageTk.PhotoImage(dark.resize(scaled), master=master)
        except (AttributeError, TypeError):      # customtkinter 內部改版時退回它自己的做法
            pass
    return image


def ctk_icon(name, size, color_pair, master=None, accent_pair=None, glow=False):
    """customtkinter 用的圖示：color_pair 是 (亮色, 暗色外觀的顏色)，accent_pair 是強調色的兩色（沒給就單色）；
    畫不出來回傳 None。glow=True 時圖片邊長是 size 的 1.5 倍（外圈有柔光）。master 見 make_ctk_image。"""
    accent_pair = accent_pair or color_pair
    light = render(name, size, color_pair[0], accent_pair[0], glow)
    dark = render(name, size, color_pair[1], accent_pair[1], glow)
    if light is None or dark is None:
        return None
    return make_ctk_image(light, dark, light.size[0], master)


def colored_svg(name, color, accent=None):
    """圖示的 SVG 文字（currentColor 換成 color、accent 換成強調色），給 Qt 的 Image 等能直接畫 SVG 的地方用；
    名稱不合格、檔案不存在、含 DOCTYPE／ENTITY 回傳 None。"""
    if not _NAME.match(str(name)):
        return None
    try:
        with open(os.path.join(paths.ICONS_DIR, name + ".svg"), encoding="utf-8") as f:
            text = f.read(64 * 1024)
    except OSError:
        return None
    if "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
        return None
    accent = accent or color
    return text.replace('"currentColor"', f'"{color}"').replace('"accent"', f'"{accent}"')

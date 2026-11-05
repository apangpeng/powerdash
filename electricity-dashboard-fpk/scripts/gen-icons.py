# -*- coding: utf-8 -*-
"""生成用电总览应用图标（深蓝圆角底 + 白色闪电），4x 超采样抗锯齿。"""
from PIL import Image, ImageDraw

OUT = r"C:\Users\Administrator\Doubao\chats\2026-10-24\new-chat\electricity-dashboard-fpk\fpk"
SS = 4  # 超采样倍数


def lerp_color(c1, c2, t):
    return tuple(int(a + (b - a) * t) for a, b in zip(c1, c2))


def rounded_rect_mask(size, radius):
    """返回 (size*SS, size*SS) 的圆角矩形灰度蒙版。"""
    s = size * SS
    m = Image.new("L", (s, s), 0)
    d = ImageDraw.Draw(m)
    r = int(radius * s)
    d.rounded_rectangle([0, 0, s - 1, s - 1], radius=r, fill=255)
    return m


def make_icon(size, path):
    s = size * SS
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    # 对角渐变底（左上深、右下浅）
    top = (13, 36, 64, 255)
    bottom = (26, 94, 168, 255)
    grad = Image.new("RGBA", (s, s))
    gd = ImageDraw.Draw(grad)
    for y in range(s):
        t = y / (s - 1)
        col = lerp_color(top[:3], bottom[:3], t) + (255,)
        gd.line([(0, y), (s - 1, y)], fill=col)
    # 对角化：沿对角线从深到浅
    grad = grad.rotate(45, resample=Image.BICUBIC, expand=False)
    # 顶部高光（上半部半透明白）
    sheen = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    sd = ImageDraw.Draw(sheen)
    for y in range(int(s * 0.42)):
        a = int(120 * (1 - y / (s * 0.42)))
        sd.line([(0, y), (s - 1, y)], fill=(255, 255, 255, a))
    mask = rounded_rect_mask(size, 0.22)
    base = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    base.paste(grad, (0, 0), mask)
    base = Image.alpha_composite(base, sheen)
    img = Image.alpha_composite(img, base)

    # 闪电（先外发光，再实心）
    pts = [(0.60, 0.06), (0.32, 0.54), (0.49, 0.54), (0.37, 0.94),
           (0.68, 0.46), (0.51, 0.46), (0.68, 0.06)]
    poly = [(x * s, y * s) for x, y in pts]
    cx, cy = s * 0.5, s * 0.52
    glow = [( (x - cx) * 1.08 + cx, (y - cy) * 1.08 + cy) for x, y in poly]
    d = ImageDraw.Draw(img)
    d.polygon(glow, fill=(255, 255, 255, 90))
    d.polygon(poly, fill=(255, 255, 255, 255))

    # 裁剪圆角（外层）并降采样
    final = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    final.paste(img, (0, 0), mask)
    final = final.resize((size, size), Image.LANCZOS)
    final.save(path, "PNG")


make_icon(64, OUT + r"\ICON.PNG")
make_icon(256, OUT + r"\ICON_256.PNG")
make_icon(64, OUT + r"\app\ui\images\icon_64.png")
make_icon(256, OUT + r"\app\ui\images\icon_256.png")
print("ICONS_DONE")

"""Shared look for the final film: palette, type, captions, slides and the browser frame (all 1920x1080 RGBA layers)."""
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

W, H = 1920, 1080
SERIF = "/System/Library/Fonts/Supplemental/Iowan Old Style.ttc"
SANS = "/System/Library/Fonts/Avenir Next.ttc"
FOREST, DEEP, CREAM, GOLD, MUTED, PAPER = (37, 70, 60), (16, 31, 26), (255, 253, 248), (196, 149, 106), (205, 199, 186), (252, 249, 243)
SAM_BLUE, SPARK_ORANGE, MUSE_PURPLE = (64, 140, 255), (255, 142, 64), (150, 120, 235)
EXT_ICON = Path(__file__).resolve().parents[2] / "extension" / "icons" / "icon-128.png"


def font(kind, size):
    path, index = {"serif": (SERIF, 0), "serif-bold": (SERIF, 1), "serif-italic": (SERIF, 2), "demi": (SANS, 2),
                   "medium": (SANS, 5), "regular": (SANS, 7), "bold": (SANS, 0)}[kind]
    return ImageFont.truetype(path, size, index=index)


def spaced(draw, xy, text, fnt, fill, tracking):
    x, y = xy
    for char in text:
        draw.text((x, y), char, font=fnt, fill=fill)
        x += draw.textlength(char, font=fnt) + tracking
    return x


def spaced_width(draw, text, fnt, tracking):
    return sum(draw.textlength(c, font=fnt) + tracking for c in text) - tracking


def wrap(draw, text, fnt, width):
    lines, line = [], ""
    for word in text.split():
        trial = f"{line} {word}".strip()
        if draw.textlength(trial, font=fnt) <= width or not line:
            line = trial
        else:
            lines.append(line)
            line = word
    return lines + ([line] if line else [])


def backdrop():
    """Deep forest with a soft light pool; returns RGB."""
    y, x = np.mgrid[0:H, 0:W].astype(np.float32)
    d = np.sqrt(((x - W * 0.42) / W) ** 2 + ((y - H * 0.38) / H) ** 2)
    glow = np.clip(1 - d / 0.9, 0, 1) ** 1.6
    base = np.array(DEEP, np.float32)
    light = np.array((34, 64, 54), np.float32)
    rgb = base + (light - base) * glow[..., None]
    rgb += np.random.default_rng(3).normal(0, 1.2, rgb.shape)  # fine grain avoids banding
    return Image.fromarray(np.clip(rgb, 0, 255).astype(np.uint8))


def shadowed(layer, radius=8, opacity=0.5, offset=(0, 3)):
    shadow = Image.new("RGBA", layer.size, (0, 0, 0, 0))
    alpha = layer.getchannel("A").filter(ImageFilter.GaussianBlur(radius)).point(lambda v: int(v * opacity))
    shadow.putalpha(alpha)
    out = Image.new("RGBA", layer.size, (0, 0, 0, 0))
    out.alpha_composite(shadow, offset)
    out.alpha_composite(layer)
    return out


def side_caption(eyebrow, headline, sub=None, x=120, y=400, width=470, eyebrow_color=GOLD):
    """Caption for the left of an app-column shot (dark green background)."""
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    if eyebrow:
        spaced(d, (x + 2, y), eyebrow.upper(), font("demi", 22), eyebrow_color + (255,), 5)
        y += 46
    for line in wrap(d, headline, font("serif", 58), width):
        d.text((x, y), line, font=font("serif", 58), fill=CREAM + (255,))
        y += 70
    if sub:
        y += 14
        for line in wrap(d, sub, font("medium", 27), width):
            d.text((x + 2, y), line, font=font("medium", 27), fill=MUTED + (255,))
            y += 38
    return shadowed(layer, 10, 0.55)


def speed_badge(text, x, y):
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    f = font("demi", 20)
    width = d.textlength(text, font=f)
    d.rounded_rectangle((x, y, x + width + 32, y + 38), 19, fill=(167, 80, 48, 235))
    d.text((x + 16, y + 19), text, font=f, fill=CREAM + (255,), anchor="lm")
    return layer


def pill_caption(text, center_x=W // 2, bottom=1000, size=32, sub=None):
    """Lower-third pill over footage."""
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    f = font("demi", size)
    fs = font("medium", 23)
    tw = d.textlength(text, font=f)
    sw = d.textlength(sub, font=fs) if sub else 0
    width = max(tw, sw) + 64
    height = 70 + (34 if sub else 0)
    left, top = center_x - width / 2, bottom - height
    d.rounded_rectangle((left, top, left + width, bottom), 22, fill=(16, 31, 26, 228))
    d.text((center_x, top + 36), text, font=f, fill=CREAM + (255,), anchor="mm")
    if sub:
        d.text((center_x, top + 72), sub, font=fs, fill=MUTED + (255,), anchor="mm")
    return shadowed(layer, 14, 0.45, (0, 6))


def browser_frame(tabs, active, address, content_box):
    """A neutral desktop browser window (no vendor branding) around content_box=(x, y, w, h)."""
    x, y, w, h = content_box
    strip, bar = 40, 46
    top = y - strip - bar
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    shadow = Image.new("L", (W, H), 0)
    ImageDraw.Draw(shadow).rounded_rectangle((x - 2, top + 8, x + w + 2, y + h + 14), 16, fill=150)
    layer.paste((0, 0, 0, 255), (0, 0), shadow.filter(ImageFilter.GaussianBlur(26)))
    layer.putalpha(shadow.filter(ImageFilter.GaussianBlur(26)).point(lambda v: int(v * 0.6)))
    d = ImageDraw.Draw(layer)
    d.rounded_rectangle((x, top, x + w, y + h), 14, fill=(222, 225, 230, 255))
    d.rectangle((x, top + strip, x + w, y), fill=(255, 255, 255, 255))
    for i, color in enumerate(((237, 106, 94), (245, 191, 79), (98, 197, 84))):
        d.ellipse((x + 18 + i * 22, top + 14, x + 30 + i * 22, top + 26), fill=color + (255,))
    tx = x + 96
    for index, (title, icon) in enumerate(tabs):
        tw = 250
        if index == active:
            d.rounded_rectangle((tx, top + 6, tx + tw, top + strip + 8), 10, fill=(255, 255, 255, 255))
        else:
            d.line((tx + tw, top + 12, tx + tw, top + strip - 6), fill=(190, 194, 200, 255), width=1)
        if icon is not None:
            layer.alpha_composite(icon.resize((18, 18), Image.LANCZOS), (tx + 14, top + 12))
        d.text((tx + 42, top + 21), title, font=font("medium", 15), fill=(40, 44, 52, 255), anchor="lm")
        d.text((tx + tw - 20, top + 21), "×", font=font("regular", 16), fill=(110, 116, 124, 255), anchor="mm")
        tx += tw + 4
    d.text((tx + 14, top + 21), "+", font=font("regular", 20), fill=(90, 96, 104, 255), anchor="mm")
    by = top + strip
    cy = by + bar / 2
    for i, direction in enumerate((-1, 1)):
        cx = x + 28 + i * 34
        color = (95, 100, 108, 255) if direction < 0 else (180, 184, 190, 255)
        tip = cx + 7 * direction
        d.line((cx - 7 * direction, cy, tip, cy), fill=color, width=2)
        d.line((tip, cy, tip - 6 * direction, cy - 6), fill=color, width=2)
        d.line((tip, cy, tip - 6 * direction, cy + 6), fill=color, width=2)
    cx = x + 96
    d.arc((cx - 7, cy - 7, cx + 7, cy + 7), 40, 330, fill=(95, 100, 108, 255), width=2)
    d.polygon(((cx + 7, cy - 9), (cx + 8, cy - 2), (cx + 2, cy - 4)), fill=(95, 100, 108, 255))
    d.rounded_rectangle((x + 130, by + 8, x + w - 120, by + bar - 8), 15, fill=(241, 243, 244, 255))
    d.text((x + 156, by + bar / 2), address, font=font("medium", 16), fill=(50, 54, 60, 255), anchor="lm")
    icon = Image.open(EXT_ICON).convert("RGBA").resize((22, 22), Image.LANCZOS)
    layer.alpha_composite(icon, (x + w - 98, by + 12))
    d.ellipse((x + w - 50, by + 12, x + w - 28, by + 34), fill=(205, 140, 160, 255))
    hole = Image.new("L", (W, H), 255)
    ImageDraw.Draw(hole).rectangle((x, y, x + w - 1, y + h - 1), fill=0)
    alpha = np.minimum(np.asarray(layer.getchannel("A")), np.asarray(hole))
    layer.putalpha(Image.fromarray(alpha.astype(np.uint8)))
    return layer


def slide(eyebrow, headline, body=None, align="left", width=1300, y=None, headline_size=78):
    image = backdrop().convert("RGBA")
    d = ImageDraw.Draw(image)
    lines = wrap(d, headline, font("serif", headline_size), width)
    body_lines = wrap(d, body, font("medium", 31), width) if body else []
    total = 46 + len(lines) * int(headline_size * 1.2) + (24 + len(body_lines) * 44 if body else 0)
    y = (H - total) // 2 if y is None else y
    cx = W // 2
    f_eye = font("demi", 24)
    if align == "center":
        ew = spaced_width(d, eyebrow.upper(), f_eye, 6)
        spaced(d, (cx - ew / 2, y), eyebrow.upper(), f_eye, GOLD + (255,), 6)
    else:
        spaced(d, (162, y), eyebrow.upper(), f_eye, GOLD + (255,), 6)
    y += 58
    for line in lines:
        if align == "center":
            d.text((cx, y), line, font=font("serif", headline_size), fill=CREAM + (255,), anchor="ma")
        else:
            d.text((160, y), line, font=font("serif", headline_size), fill=CREAM + (255,))
        y += int(headline_size * 1.2)
    if body_lines:
        y += 24
        for line in body_lines:
            if align == "center":
                d.text((cx, y), line, font=font("medium", 31), fill=MUTED + (255,), anchor="ma")
            else:
                d.text((162, y), line, font=font("medium", 31), fill=MUTED + (255,))
            y += 44
    return image

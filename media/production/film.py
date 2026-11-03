"""Compose the full Rewear Studio demo film (1920x1080, 30 fps) from the POV film, the live extension take,
the live analysis artifacts and designed slides. Frames are composited in numpy and piped to ffmpeg.
Usage (from media/production): python film.py . build/video-clean.mp4 [preview frame list]
Env: SUBS=<subtitles.json> burns the narration subtitles in; TIMELINE_ONLY=1 just writes timeline.json."""
import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

sys.path.insert(0, str(Path(__file__).parent))
from design import (CREAM, EXT_ICON, FOREST, GOLD, H, MUTED, W, backdrop, browser_frame, font, pill_caption,  # noqa: E402
                    shadowed, side_caption, spaced, speed_badge, wrap)

WORK = Path(sys.argv[1])
OUT = Path(sys.argv[2])
PREVIEW = [int(v) for v in sys.argv[3].split(",")] if len(sys.argv) > 3 else None
FPS = 30
PROD = Path(__file__).resolve().parent
V2 = PROD / "sources" / "rewear-pov-film-v2.mp4"
BUILD = WORK / "build"
BUILD.mkdir(exist_ok=True)
TAKE = WORK / "take-ext"
EVENTS = json.loads((TAKE / "events.json").read_text())["events"]
ART = json.loads((WORK / "artifacts" / "artifacts.json").read_text())
ART["source"] = str(WORK / "artifacts" / ART["source"])
for _piece in ART["pieces"]:
    _piece["mask"] = str(WORK / "artifacts" / _piece["mask"])
for _row in ART["spark_rows"]:
    _row["crop"] = str(WORK / "artifacts" / _row["crop"])
for _row in ART["previews"]:
    _row["preview"] = str(WORK / "artifacts" / _row["preview"])
SUBS = json.loads(Path(os.environ["SUBS"]).read_text()) if os.environ.get("SUBS") else None


def smooth(x):
    x = min(max(x, 0.0), 1.0)
    return x * x * (3 - 2 * x)


def ease_out(x):
    x = min(max(x, 0.0), 1.0)
    return 1 - (1 - x) ** 3


class Clip:
    """Sequential frames of [start, end) seconds of a video, sped up by `speed`, as RGB arrays."""

    def __init__(self, path, start, end, speed=1.0, size=None, matrix="bt601"):
        start = max(0.0, start)
        self.path, self.start, self.end, self.speed = path, start, end, speed
        self.n = max(1, int(round((end - start) / speed * FPS)))
        self.size = size
        self.matrix = matrix
        self.proc, self.index, self.last = None, -1, None

    def frame(self, i):
        i = min(max(i, 0), self.n - 1)
        if self.proc is None:
            w, h = self.size
            vf = f"scale={w}:{h}:flags=lanczos:in_color_matrix={self.matrix},setpts=(PTS-STARTPTS)/{self.speed},fps={FPS},format=rgb24"
            self.proc = subprocess.Popen(["ffmpeg", "-v", "error", "-ss", f"{self.start:.3f}", "-t", f"{self.end - self.start:.3f}",
                                          "-i", str(self.path), "-vf", vf, "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                                         stdout=subprocess.PIPE)
        w, h = self.size
        while self.index < i:
            raw = self.proc.stdout.read(w * h * 3)
            if len(raw) < w * h * 3:
                break
            self.last = np.frombuffer(raw, np.uint8).reshape(h, w, 3)
            self.index += 1
        return self.last

    def close(self):
        if self.proc:
            self.proc.kill()
            self.proc = None


def rgba_array(image):
    a = np.asarray(image.convert("RGBA"), np.float32)
    return a[..., :3] * (a[..., 3:] / 255), a[..., 3:] / 255  # premultiplied colour, alpha


def over(frame, layer, opacity=1.0, dy=0):
    """Composite a premultiplied (colour, alpha) layer onto a float frame."""
    if opacity <= 0:
        return frame
    color, alpha = layer
    if dy:
        color, alpha = np.roll(color, dy, axis=0), np.roll(alpha, dy, axis=0)
    return frame * (1 - alpha * opacity) + color * opacity


def fade_window(t, start, end, fade=0.35):
    return smooth((t - start) / fade) * (1 - smooth((t - (end - fade)) / fade))


# ======================= scene 1: street POV (snap -> scan -> result -> shop) =======================
class Pov:
    def __init__(self):
        size = (W, H)
        self.parts = [Clip(V2, 0.0, 6.6, 1, size, "bt709"), Clip(V2, 6.6, 18.9, 2, size, "bt709"), Clip(V2, 18.9, 25.7, 1, size, "bt709")]
        self.offsets = np.cumsum([0] + [p.n for p in self.parts])
        self.length = int(self.offsets[-1])
        p2, p3 = self.offsets[1] / FPS, self.offsets[2] / FPS
        self.p3b = p3 + 2.1
        self.p3 = p3
        self.captions = [
            (rgba_array(side_caption("Meta AI · live", "Every garment, found.", "SAM 3.1 segments each piece. Muse Spark reads the details.")), p2 + 0.15, p3 + 0.1),
            (rgba_array(speed_badge("2× speed", 122, 700)), p2 + 0.15, p3 + 0.1),
            (rgba_array(model_chips()), p2 + 0.3, p3 + 0.1),
            (rgba_array(side_caption("Result", "A polka-dot maxi dress.", "Category, colours, pattern and fabric for every piece, with a confidence score.")), p3 + 0.15, self.p3b),
            (rgba_array(side_caption("Shop", "One tap to shop similar styles.", "Free Google, Bing and web search links, no extra model cost.")), self.p3b, self.length / FPS + 1),
        ]

    def render(self, i):
        part = int(np.searchsorted(self.offsets, i, side="right") - 1)
        part = min(part, len(self.parts) - 1)
        frame = self.parts[part].frame(i - int(self.offsets[part])).astype(np.float32)
        t = i / FPS
        for layer, start, end in self.captions:
            frame = over(frame, layer, fade_window(t, start, end, 0.3))
        return frame


def model_chips():
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    y = 430
    for name, role, color in (("Meta SAM 3.1", "segmentation", (64, 140, 255)), ("Muse Spark 1.3", "garment details", (255, 142, 64))):
        d.rounded_rectangle((1370, y, 1790, y + 74), 16, fill=(255, 255, 255, 20), outline=(255, 255, 255, 46), width=1)
        d.ellipse((1394, y + 29, 1410, y + 45), fill=color + (255,))
        d.text((1428, y + 15), name, font=font("demi", 25), fill=CREAM + (255,))
        d.text((1428, y + 46), role, font=font("medium", 19), fill=MUTED + (255,))
        y += 92
    return layer


# ======================= scene 3: browser + extension take =======================
class Extension:
    BOX = (160, 150, 1600, 900)  # content area of the browser window (native recording size)

    def __init__(self):
        e = EVENTS
        # Measured from the footage: the feed scroll starts 0.10 s after its event, and the Rewear
        # video's results scroll and dialog open sit 8.92 s before their events.
        feed_zero, rewear_zero = -0.10, 8.92
        fv = lambda t: t - feed_zero                   # event time -> feed video time
        rv = lambda t: t - rewear_zero                 # event time -> rewear video time
        size = (1600, 900)
        scan_from = e["scanning"] + 2.2
        wait = e["results"] - 0.4 - scan_from
        previews = e["previews_done"] - (e["results_hold_end"] + 0.2)
        self.speed_scan = max(1, round(wait / 5.0))
        self.speed_previews = max(1, round(previews / 5.0))
        self.parts = [
            Clip(TAKE / "feed.webm", fv(e["feed"] - 0.9), fv(e["click"] + 0.75), 1, size),
            Clip(TAKE / "rewear.webm", rv(e["rewear_open"]), rv(scan_from), 1, size),
            Clip(TAKE / "rewear.webm", rv(scan_from), rv(e["results"] - 0.4), self.speed_scan, size),
            Clip(TAKE / "rewear.webm", rv(e["results"] - 0.4), rv(e["results_hold_end"] + 0.2), 1, size),
            Clip(TAKE / "rewear.webm", rv(e["results_hold_end"] + 0.2), rv(e["previews_done"]), self.speed_previews, size),
            Clip(TAKE / "rewear.webm", rv(e["previews_done"] + 0.5), rv(e["detail_end"] + 0.1), 1, size),
        ]
        self.offsets = np.cumsum([0] + [p.n for p in self.parts])
        self.length = int(self.offsets[-1])
        sec = lambda k: self.offsets[k] / FPS
        pill_at = e["pill"] - (e["feed"] - 0.9)
        moments = moments_icon()
        rewear = Image.open(EXT_ICON).convert("RGBA")
        x, y, w, h = self.BOX
        self.frame_feed = rgba_array(browser_frame([("Moments · Home", moments)], 0, "feed.example", self.BOX))
        self.frame_rewear = rgba_array(browser_frame([("Moments · Home", moments), ("Rewear Studio", rewear)], 1, "127.0.0.1:8781", self.BOX))
        mask = Image.new("L", (W, H), 0)
        ImageDraw.Draw(mask).rounded_rectangle((x, y - 40, x + w - 1, y + h - 1), 14, fill=255)  # rounded bottom corners only
        self.mask = (np.asarray(mask, np.float32) / 255)[..., None]
        self.background = np.asarray(backdrop(), np.float32)
        bottom = y + h - 26
        self.captions = [
            (rgba_array(pill_caption("Hover any photo, then click Shop this look", bottom=bottom, sub="Rewear for Chrome sends just that one photo")), pill_at + 0.2, sec(1) + 0.2, "pill"),
            (rgba_array(pill_caption("Rewear opens and Meta AI reads the photo", bottom=bottom)), sec(1) + 0.3, sec(3), "pill"),
            (rgba_array(speed_badge(f"{self.speed_scan}× speed", x + w // 2 - 62, y + 16)), sec(2), sec(3), "badge"),
            (rgba_array(pill_caption("8 pieces found, across all three outfits", bottom=bottom)), sec(3) + 0.2, sec(4) + 0.1, "pill"),
            (rgba_array(pill_caption("Muse Image previews every piece", bottom=bottom, sub="Each preview is labeled as AI, next to the original photo")), sec(4) + 0.1, sec(5), "pill"),
            (rgba_array(speed_badge(f"{self.speed_previews}× speed", x + w // 2 - 62, y + 16)), sec(4) + 0.05, sec(5), "badge"),
            (rgba_array(pill_caption("Tap a piece to shop similar on Google, Bing or the web", bottom=bottom)), sec(5) + 0.3, self.length / FPS + 1, "pill"),
        ]
        self.switch = self.offsets[1]
        self.pill_at = pill_at
        if SUBS:
            self.captions = [c for c in self.captions if c[3] != "pill"]

    def render(self, i):
        part = min(int(np.searchsorted(self.offsets, i, side="right") - 1), len(self.parts) - 1)
        content = self.parts[part].frame(i - int(self.offsets[part])).astype(np.float32)
        frame = self.background.copy()
        x, y, w, h = self.BOX
        region = frame[y:y + h, x:x + w]
        m = self.mask[y:y + h, x:x + w]
        frame[y:y + h, x:x + w] = region * (1 - m) + content * m
        frame = over(frame, self.frame_feed if i < self.switch else self.frame_rewear)
        t = i / FPS
        for layer, start, end, _ in self.captions:
            frame = over(frame, layer, fade_window(t, start, end, 0.3))
        return frame


def moments_icon():
    icon = Image.new("RGBA", (36, 36), (0, 0, 0, 0))
    d = ImageDraw.Draw(icon)
    d.ellipse((2, 2, 34, 34), fill=(224, 96, 126, 255))
    d.ellipse((11, 11, 25, 25), outline=(255, 255, 255, 255), width=3)
    return icon


# ======================= scene 4: how it works (real SAM masks, Spark fields, Muse previews) =======================
class Pipeline:
    PHOTO = (110, 268, 1000, 667)  # x, y, w, h

    def __init__(self, length):
        self.length = length
        self.background = np.asarray(backdrop(), np.float32)
        title = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(title)
        spaced(d, (114, 100), "HOW IT WORKS", font("demi", 24), GOLD + (255,), 6)
        d.text((110, 146), "Three Meta models. One click.", font=font("serif", 70), fill=CREAM + (255,))
        self.title = rgba_array(title)
        x, y, w, h = self.PHOTO
        source = Image.open(ART["source"]).convert("RGB")
        scale = w / source.width
        photo = source.resize((w, h), Image.LANCZOS)
        frame = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        rounded = Image.new("L", (w, h), 0)
        ImageDraw.Draw(rounded).rounded_rectangle((0, 0, w - 1, h - 1), 18, fill=255)
        frame.paste(photo, (x, y), rounded)
        self.photo = rgba_array(shadowed(frame, 22, 0.55, (0, 12)))
        palette = [(64, 140, 255), (255, 142, 64), (150, 120, 235), (60, 200, 160), (255, 96, 140), (250, 204, 70), (90, 190, 240), (190, 230, 90)]
        self.masks, self.labels, placed = [], [], []
        for index, piece in enumerate(ART["pieces"]):
            mask = Image.open(piece["mask"]).convert("L").resize((w, h), Image.LANCZOS)
            m = np.zeros((H, W), np.float32)
            m[y:y + h, x:x + w] = np.asarray(mask, np.float32) / 255
            m[y:y + h, x:x + w] *= np.asarray(rounded, np.float32) / 255
            edge = np.asarray(Image.fromarray((m * 255).astype(np.uint8)).filter(ImageFilter.FIND_EDGES).filter(ImageFilter.GaussianBlur(1.2)), np.float32) / 255
            color = np.array(palette[index % len(palette)], np.float32)
            alpha = np.clip(m * 0.42 + np.clip(edge * 3, 0, 1) * 0.9, 0, 1)[..., None]
            self.masks.append((color * alpha, alpha))
            left, top, right, bottom = piece["bbox"]
            label = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            ld = ImageDraw.Draw(label)
            text = piece["name"]
            f = font("demi", 17)
            tw = ld.textlength(text, font=f)
            lx = min(x + left * scale + 8, x + w - tw - 26)
            ly = y + top * scale + 8
            while any(lx < r and lx + tw + 18 > l and ly < b and ly + 28 > t for l, t, r, b in placed):
                ly += 32
            placed.append((lx, ly, lx + tw + 18, ly + 28))
            ld.rounded_rectangle((lx, ly, lx + tw + 18, ly + 28), 8, fill=tuple(palette[index % len(palette)]) + (235,))
            ld.text((lx + 9, ly + 14), text, font=f, fill=(255, 255, 255, 255), anchor="lm")
            self.labels.append(rgba_array(label))
        self.stage = [rgba_array(stage_panel(k)) for k in range(3)]
        self.shop = rgba_array(shop_chips())

    def render(self, i):
        t = i / FPS
        settled = t >= 7.4 + len(self.labels) * 0.12 + 0.35
        if settled and getattr(self, "cache", None) is not None:
            frame = self.cache.copy()
        else:
            frame = self.background.copy()
            frame = over(frame, self.title, smooth(t / 0.5))
            frame = over(frame, self.photo, smooth((t - 0.2) / 0.5), int(round(16 * (1 - ease_out((t - 0.2) / 0.6)))))
            for index, mask in enumerate(self.masks):
                frame = over(frame, mask, smooth((t - 1.0 - index * 0.32) / 0.35))
            for index, label in enumerate(self.labels):
                frame = over(frame, label, smooth((t - 7.4 - index * 0.12) / 0.3))
            if settled:
                self.cache = frame.copy()
        windows = [(0.6, 7.2), (7.2, 13.6), (13.6, self.length / FPS + 1)]
        for k, (start, end) in enumerate(windows):
            level = fade_window(t, start, end, 0.4)
            if level > 0:
                frame = over(frame, self.stage[k], level, int(round(14 * (1 - ease_out((t - start) / 0.5)))))
        frame = over(frame, self.shop, smooth((t - 17.4) / 0.5))
        return frame


def stage_panel(k):
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    x, y = 1180, 300
    number, model, line, color = [
        ("01", "Meta SAM 3.1", "Segments every garment, pixel by pixel. Not boxes, real masks.", (64, 140, 255)),
        ("02", "Muse Spark 1.3", "Reads each piece: type, cut, colour and fabric.", (255, 142, 64)),
        ("03", "Muse Image 1.0", "Previews the complete garment, always labeled as AI.", (150, 120, 235)),
    ][k]
    d.text((x, y), number, font=font("serif", 44), fill=GOLD + (255,))
    d.rounded_rectangle((x + 74, y + 12, x + 86, y + 24), 6, fill=color + (255,))
    d.text((x + 100, y + 2), model, font=font("demi", 36), fill=CREAM + (255,))
    yy = y + 70
    for text in wrap(d, line, font("medium", 27), 620):
        d.text((x, yy), text, font=font("medium", 27), fill=MUTED + (255,))
        yy += 38
    yy += 26
    if k == 0:
        d.text((x, yy), f"{len(ART['pieces'])}", font=font("serif", 92), fill=CREAM + (255,))
        d.text((x + 120, yy + 30), "garments found\nacross 3 people", font=font("medium", 26), fill=MUTED + (255,), spacing=6)
    elif k == 1:
        for piece in ART["spark_rows"]:
            thumb = Image.open(piece["crop"]).convert("RGB")
            thumb.thumbnail((92, 92))
            box = Image.new("RGB", (92, 92), (238, 234, 226))
            box.paste(thumb, ((92 - thumb.width) // 2, (92 - thumb.height) // 2))
            rounded = Image.new("L", (92, 92), 0)
            ImageDraw.Draw(rounded).rounded_rectangle((0, 0, 91, 91), 12, fill=255)
            layer.paste(box, (x, int(yy)), rounded)
            d.text((x + 116, yy + 12), piece["name"], font=font("demi", 27), fill=CREAM + (255,))
            d.text((x + 116, yy + 52), piece["detail"], font=font("medium", 22), fill=MUTED + (255,))
            yy += 112
    else:
        px = x
        for piece in ART["previews"]:
            image = Image.open(piece["preview"]).convert("RGB")
            card = Image.new("RGB", (190, 238), (246, 243, 236))
            image.thumbnail((170, 196))
            card.paste(image, ((190 - image.width) // 2, 10 + (196 - image.height) // 2))
            rounded = Image.new("L", (190, 238), 0)
            ImageDraw.Draw(rounded).rounded_rectangle((0, 0, 189, 237), 14, fill=255)
            layer.paste(card, (px, int(yy)), rounded)
            d.rounded_rectangle((px + 10, yy + 204, px + 102, yy + 228), 8, fill=(150, 120, 235, 235))
            d.text((px + 56, yy + 216), "AI preview", font=font("demi", 14), fill=(255, 255, 255, 255), anchor="mm")
            px += 206
    return shadowed(layer, 10, 0.5)


def shop_chips():
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    x, y = 1180, 900
    d.text((x, y + 6), "Then", font=font("medium", 24), fill=MUTED + (255,))
    x += 76
    for text in ("Shop similar on Google", "Bing", "Web"):
        width = d.textlength(text, font=font("demi", 22)) + 40
        d.rounded_rectangle((x, y, x + width, y + 46), 23, fill=FOREST + (255,), outline=(255, 255, 255, 60), width=1)
        d.text((x + width / 2, y + 23), text, font=font("demi", 22), fill=CREAM + (255,), anchor="mm")
        x += width + 12
    return layer


# ======================= scenes 5-7: why it matters, proof, close =======================
def business_slide():
    image = backdrop().convert("RGBA")
    d = ImageDraw.Draw(image)
    spaced(d, (164, 170), "WHY IT MATTERS", font("demi", 24), GOLD + (255,), 6)
    d.text((160, 214), "Every photo in a feed is shopping intent.", font=font("serif", 70), fill=CREAM + (255,))
    cards = [
        ("Shoppers", "From “I love that” to the right piece in one click. No screenshots, no guessing keywords."),
        ("Creators & brands", "Every shoppable look can carry affiliate links, so inspiration turns into revenue."),
        ("Meta", "Native outfit search for Facebook & Instagram, powered end to end by Meta’s own models."),
    ]
    x = 160
    for title, text in cards:
        d.rounded_rectangle((x, 400, x + 500, 760), 22, fill=(255, 255, 255, 16), outline=(255, 255, 255, 40), width=1)
        d.text((x + 36, 436), title, font=font("serif", 40), fill=CREAM + (255,))
        yy = 506
        for line in wrap(d, text, font("medium", 27), 428):
            d.text((x + 36, yy), line, font=font("medium", 27), fill=MUTED + (255,))
            yy += 40
        x += 540
    d.text((160, 830), "Next: “Shop this look” inside Facebook & Instagram, and verified product matches.", font=font("medium", 28), fill=GOLD + (255,))
    return image


def proof_slide():
    image = backdrop().convert("RGBA")
    d = ImageDraw.Draw(image)
    spaced(d, (164, 160), "HONEST BY DESIGN", font("demi", 24), GOLD + (255,), 6)
    d.text((160, 204), "Built to be trusted, and tested.", font=font("serif", 70), fill=CREAM + (255,))
    stats = [
        ("8 pieces", "from one three-person photo in about 100 seconds, live"),
        ("30 photos", "real street-style photos re-tested; duplicates and fragments fixed"),
        ("1,612", "automated tests, including 12 real-browser end-to-end runs"),
    ]
    x = 160
    for value, text in stats:
        d.text((x, 380), value, font=font("serif", 84), fill=CREAM + (255,))
        yy = 500
        for line in wrap(d, text, font("medium", 27), 470):
            d.text((x + 2, yy), line, font=font("medium", 27), fill=MUTED + (255,))
            yy += 40
        x += 540
    yy = 700
    for line in ("Unclear regions are flagged, never guessed.", "“Shop similar” is never presented as “the same item”.", "Only the photo you click is sent. Nothing scans your feed."):
        d.ellipse((164, yy + 14, 176, yy + 26), fill=GOLD + (255,))
        d.text((196, yy), line, font=font("medium", 29), fill=CREAM + (255,))
        yy += 54
    return image


def close_slide():
    image = backdrop().convert("RGBA")
    d = ImageDraw.Draw(image)
    mark = Image.open(EXT_ICON).convert("RGBA").resize((112, 112), Image.LANCZOS)
    image.alpha_composite(mark, (W // 2 - 56, 250))
    d.text((W // 2, 404), "Rewear Studio", font=font("serif", 104), fill=CREAM + (255,), anchor="ma")
    tag = "SEE IT. FIND IT. BUY IT."
    f = font("demi", 30)
    width = sum(d.textlength(c, font=f) + 8 for c in tag) - 8
    spaced(d, (W // 2 - width / 2, 548), tag, f, GOLD + (255,), 8)
    d.rounded_rectangle((W // 2 - 250, 630, W // 2 + 250, 700), 35, fill=(255, 253, 248, 255))
    d.text((W // 2, 665), "rewear.soraing.com", font=font("demi", 34), fill=FOREST + (255,), anchor="mm")
    d.text((W // 2, 742), "Built with Meta SAM 3.1 · Muse Spark 1.3 · Muse Image 1.0", font=font("medium", 30), fill=MUTED + (255,), anchor="ma")
    d.text((W // 2, 790), "Web app + Chrome extension · Meta Global AI Developer Hackathon 2026", font=font("medium", 26), fill=MUTED + (255,), anchor="ma")
    return image


class Still:
    def __init__(self, image, length, rise=True):
        self.layer = rgba_array(image)
        self.length = length
        self.background = np.asarray(backdrop(), np.float32)
        self.rise = rise

    def render(self, i):
        t = i / FPS
        frame = self.background.copy()
        dy = int(round(18 * (1 - ease_out(t / 0.7)))) if self.rise else 0
        return over(frame, self.layer, smooth(t / 0.45) if self.rise else 1.0, dy)


def feed_card():
    image = backdrop().convert("RGBA")
    d = ImageDraw.Draw(image)
    spaced(d, (W // 2 - 190, 360), "ANYWHERE YOU SCROLL", font("demi", 24), GOLD + (255,), 6)
    d.text((W // 2, 410), "Most looks you love aren’t on the street.", font=font("serif", 66), fill=CREAM + (255,), anchor="ma")
    d.text((W // 2, 500), "They’re in your feed.", font=font("serif-italic", 66), fill=GOLD + (255,), anchor="ma")
    d.text((W // 2, 616), "Rewear for Chrome: one click on any photo.", font=font("medium", 30), fill=MUTED + (255,), anchor="ma")
    return image


# ======================= timeline =======================
pov = Pov()
ext = Extension()
scenes = [
    (pov, 0),
    (Still(feed_card(), int(4.2 * FPS)), 12),
    (ext, 12),
    (Pipeline(int(20.0 * FPS)), 15),
    (Still(business_slide(), int(11.0 * FPS)), 15),
    (Still(proof_slide(), int(10.5 * FPS)), 15),
    (Still(close_slide(), int(7.0 * FPS)), 15),
]
starts, cursor = [], 0
for scene, overlap in scenes:
    start = cursor - overlap
    starts.append(start)
    cursor = start + scene.length
TOTAL = cursor
FADE_IN, FADE_OUT = 1, 24


SUB_CACHE = {}


def subtitle_layer(text, mode):
    key = (text, mode)
    if key not in SUB_CACHE:
        if mode == "pill":
            layer = pill_caption(text, bottom=ext.BOX[1] + ext.BOX[3] - 26)
        else:
            layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            d = ImageDraw.Draw(layer)
            if mode == "left":
                f = font("medium", 30)
                lines = wrap(d, text, f, 480)
                y = 1012 - len(lines) * 42
                for line in lines:
                    d.text((122, y), line, font=f, fill=CREAM + (255,))
                    y += 42
                layer = shadowed(layer, 6, 0.85, (0, 2))
            else:
                f = font("medium", 34)
                lines = wrap(d, text, f, 1480)
                widths = [d.textlength(line, font=f) for line in lines]
                top = 1046 - len(lines) * 48 - 14
                d.rounded_rectangle((W / 2 - max(widths) / 2 - 26, top, W / 2 + max(widths) / 2 + 26, 1046), 18, fill=(10, 20, 16, 170))
                y = top + 12
                for line in lines:
                    d.text((W / 2, y), line, font=f, fill=CREAM + (255,), anchor="ma")
                    y += 48
        SUB_CACHE[key] = rgba_array(layer)
    return SUB_CACHE[key]


def subtitle_mode(k):
    active = [index for index, ((scene, _), start) in enumerate(zip(scenes, starts)) if 0 <= k - start < scene.length]
    index = active[-1]
    return "left" if index == 0 else "pill" if index == 2 else "bottom"


def compose(k):
    frame = None
    for (scene, overlap), start in zip(scenes, starts):
        j = k - start
        if not 0 <= j < scene.length:
            continue
        image = scene.render(j)
        if frame is None:
            frame = image
        else:
            weight = smooth((j + 1) / (overlap + 1))
            frame = frame * (1 - weight) + image * weight
    if SUBS:
        t = k / FPS
        for cue in SUBS:
            begin = cue["start"] - (0.12 if cue["fade_in"] else 0)
            if begin <= t < cue["to"]:
                rise = smooth((t - begin) / 0.12) if cue["fade_in"] else 1.0
                fall = 1 - smooth((t - (cue["to"] - 0.15)) / 0.15) if cue["fade_out"] else 1.0
                frame = over(frame, subtitle_layer(cue["text"], subtitle_mode(k)), rise * fall)
    fade = smooth((TOTAL - k) / FADE_OUT)
    return np.clip(frame * fade + 0.5, 0, 255).astype(np.uint8)


if __name__ == "__main__":
    print(json.dumps({"total_seconds": round(TOTAL / FPS, 2), "starts": [round(s / FPS, 2) for s in starts],
                      "lengths": [round(s.length / FPS, 2) for s, _ in scenes], "speed_scan": ext.speed_scan, "speed_previews": ext.speed_previews}))
    anchors = {"total": TOTAL / FPS, "starts": [s / FPS for s in starts], "ext_start": starts[2] / FPS,
               "ext_pill": ext.pill_at, "ext_click": EVENTS["click"] - (EVENTS["feed"] - 0.9), "pov_length": pov.length / FPS,
               "pov_result": pov.p3, "pov_shop": pov.p3b, "ext_parts": [float(o) / FPS for o in ext.offsets]}
    (BUILD / "timeline.json").write_text(json.dumps(anchors))
    if os.environ.get("TIMELINE_ONLY"):
        sys.exit(0)
    if PREVIEW:
        for k in sorted(PREVIEW):
            (BUILD / "preview").mkdir(exist_ok=True)
            Image.fromarray(compose(k)).save(BUILD / "preview" / f"p-{k:05d}.png")
        sys.exit(0)
    video = OUT
    encoder = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-",
         "-vf", "scale=out_color_matrix=bt709:out_range=tv,format=yuv420p", "-c:v", "libx264", "-preset", "slow", "-crf", "16",
         "-tune", "film", "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv", str(video)],
        stdin=subprocess.PIPE)
    for k in range(TOTAL):
        encoder.stdin.write(compose(k).tobytes())
    encoder.stdin.close()
    if encoder.wait():
        raise SystemExit("encode failed")
    print("video done")

"""Vertical 1080x1920 short for X (~30 s): hook → snap → Meta AI scan → shop → feed extension → SAM masks → end card.
Captions are burned in (X autoplays muted); narration from voice-short/ and the original lo-fi bed play underneath.
Usage (from media/production): python short.py build/rewear-short.mp4 [preview frame list]"""
import json
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

sys.path.insert(0, str(Path(__file__).parent))
from design import CREAM, EXT_ICON, GOLD, MUTED, font  # noqa: E402

PROD = Path(__file__).resolve().parent
OUT = Path(sys.argv[1])
PREVIEW = [int(v) for v in sys.argv[2].split(",")] if len(sys.argv) > 2 else None
W, H, FPS, RATE = 1080, 1920, 30, 48000
POV = PROD / "sources" / "rewear-pov-film.mp4"
V2 = PROD / "sources" / "rewear-pov-film-v2.mp4"
TAKE = PROD / "take-ext"
EVENTS = json.loads((TAKE / "events.json").read_text())["events"]
ART = json.loads((PROD / "artifacts" / "artifacts.json").read_text())
BUILD = PROD / "build"
BUILD.mkdir(exist_ok=True)
DEEP = np.array((18, 34, 28), np.float32)


def smooth(x):
    x = min(max(x, 0.0), 1.0)
    return x * x * (3 - 2 * x)


def rgba(image):
    a = np.asarray(image.convert("RGBA"), np.float32)
    return a[..., :3] * (a[..., 3:] / 255), a[..., 3:] / 255


def over(frame, layer, level=1.0):
    if level <= 0:
        return frame
    color, alpha = layer
    return frame * (1 - alpha * level) + color * level


class Clip:
    """Frames of [start, end) seconds, cropped to (x, y, w, h), scaled to `size`, sped up by `speed`."""

    def __init__(self, path, start, end, crop, size, speed=1.0, matrix="bt709"):
        self.n = max(1, int(round((end - start) / speed * FPS)))
        x, y, w, h = crop
        self.size = size
        self.cmd = ["ffmpeg", "-v", "error", "-ss", f"{max(0.0, start):.3f}", "-t", f"{end - start:.3f}", "-i", str(path), "-vf",
                    f"crop={w}:{h}:{x}:{y},scale={size[0]}:{size[1]}:flags=lanczos:in_color_matrix={matrix},unsharp=5:5:0.6,"
                    f"setpts=(PTS-STARTPTS)/{speed},fps={FPS},format=rgb24", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
        self.proc, self.index, self.last = None, -1, None

    def frame(self, i):
        i = min(max(i, 0), self.n - 1)
        if self.proc is None:
            self.proc = subprocess.Popen(self.cmd, stdout=subprocess.PIPE)
        w, h = self.size
        while self.index < i:
            raw = self.proc.stdout.read(w * h * 3)
            if len(raw) < w * h * 3:
                break
            self.last = np.frombuffer(raw, np.uint8).reshape(h, w, 3)
            self.index += 1
        return self.last.astype(np.float32)


def caption(text, y=170, size=66, sub=None, cx=W / 2):
    """Big top caption on a dark pill — inside X's safe area."""
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    f = font("demi", size)
    lines = text.split("\n")
    widths = [d.textlength(line, font=f) for line in lines]
    height = len(lines) * int(size * 1.22) + (60 if sub else 0) + 52
    width = max(widths + ([d.textlength(sub, font=font("medium", 34))] if sub else [])) + 80
    left = cx - width / 2
    d.rounded_rectangle((left, y, left + width, y + height), 34, fill=(12, 24, 19, 215))
    ty = y + 26
    for line in lines:
        d.text((cx, ty), line, font=f, fill=CREAM + (255,), anchor="ma")
        ty += int(size * 1.22)
    if sub:
        d.text((cx, ty + 6), sub, font=font("medium", 34), fill=(243, 214, 186, 255), anchor="ma")
    shadow = layer.getchannel("A").filter(ImageFilter.GaussianBlur(18)).point(lambda v: int(v * 0.5))
    out = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    out.putalpha(shadow)
    out.alpha_composite(layer)
    return rgba(out)


def badge(text, y):
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    f = font("demi", 30)
    width = d.textlength(text, font=f) + 44
    d.rounded_rectangle(((W - width) / 2, y, (W + width) / 2, y + 54), 27, fill=(167, 80, 48, 235))
    d.text((W / 2, y + 27), text, font=f, fill=CREAM + (255,), anchor="mm")
    return rgba(layer)


# ---------------- scene 1: hook on the street (vertical crop of the POV film) ----------------
class Hook:
    CROP = (636, 0, 608, 1080)

    def __init__(self):
        self.parts = [Clip(POV, 0.0, 2.6, self.CROP, (W, H)), Clip(POV, 5.7, 7.6, self.CROP, (W, H)), Clip(POV, 9.4, 11.24, self.CROP, (W, H))]
        self.starts = [0, 78 - 9, 78 - 9 + 57 - 9]
        self.freeze_at = self.starts[2] + self.parts[2].n
        self.length = self.freeze_at + 14
        still = subprocess.run(["ffmpeg", "-v", "error", "-ss", "11.2", "-i", str(POV), "-frames:v", "1", "-vf",
                                f"crop=608:1080:636:0,scale={W}:{H}:flags=lanczos,unsharp=5:5:0.6", "-f", "image2pipe", "-vcodec", "png", "-"],
                               capture_output=True, check=True).stdout
        from io import BytesIO
        self.still = Image.open(BytesIO(still)).convert("RGB")
        self.captions = [(caption("See an outfit\nyou love?", y=200, size=84, cx=400), 6, 72), (caption("Snap it.", y=200, size=96, cx=400), 84, self.freeze_at + 2)]

    def render(self, i):
        if i >= self.freeze_at:
            j = i - self.freeze_at
            z = 1 + 0.12 * smooth(j / 13)
            w, h = W / z, H / z
            cx, cy = W * 0.29, H * 0.70  # the phone in the crop
            x0 = min(max(cx - w / 2, 0), W - w)
            y0 = min(max(cy - h / 2, 0), H - h)
            frame = np.asarray(self.still.resize((W, H), Image.BICUBIC, box=(x0, y0, x0 + w, y0 + h)), np.float32)
            flash = [0.55, 0.9, 0.75, 0.55, 0.38, 0.24, 0.14, 0.07, 0.03][j] if j < 9 else 0
            frame = frame * (1 - flash) + 255 * flash
        else:
            frame = None
            for k, (clip, start) in enumerate(zip(self.parts, self.starts)):
                j = i - start
                if 0 <= j < clip.n:
                    image = clip.frame(j)
                    if frame is None:
                        frame = image
                    else:
                        weight = smooth((j + 1) / 10)
                        frame = frame * (1 - weight) + image * weight
        for layer, a, b in self.captions:
            level = smooth((i - a) / 8) * (1 - smooth((i - b) / 8))
            frame = over(frame, layer, level)
        return frame


# ---------------- scene 2: the app on the phone (the POV film's app column is already vertical) ----------------
class App:
    def __init__(self):
        crop, size = (640, 0, 640, 1080), (900, 1519)
        self.parts = [Clip(V2, 6.6, 18.9, crop, size, speed=4), Clip(V2, 18.9, 22.3, crop, size), Clip(V2, 22.3, 23.7, crop, size)]
        self.offsets = np.cumsum([0] + [p.n for p in self.parts])
        self.length = int(self.offsets[-1])
        self.cap_find = caption("Meta AI finds every piece", y=110, size=62)
        self.cap_shop = caption("Shop similar in one tap", y=110, size=62)
        self.speed = badge("4× speed", 1700)
        screen = Image.new("L", (900, 1519), 0)
        ImageDraw.Draw(screen).rounded_rectangle((0, 0, 899, 1518), 40, fill=255)
        self.screen = (np.asarray(screen, np.float32) / 255)[..., None]
        shadow = Image.new("L", (W, H), 0)
        ImageDraw.Draw(shadow).rounded_rectangle((90, 340, 990, 1869), 40, fill=170)
        self.shadow = (np.asarray(shadow.filter(ImageFilter.GaussianBlur(28)), np.float32) / 255)[..., None]

    def render(self, i):
        part = min(int(np.searchsorted(self.offsets, i, side="right") - 1), 2)
        frame = np.empty((H, W, 3), np.float32)
        frame[:] = DEEP
        frame = frame * (1 - 0.6 * self.shadow)
        region = frame[330:330 + 1519, 90:90 + 900]
        frame[330:330 + 1519, 90:90 + 900] = region * (1 - self.screen) + self.parts[part].frame(i - int(self.offsets[part])) * self.screen
        t1 = int(self.offsets[1])
        switch = t1 + 66  # the item sheet with "Shop similar on Google" opens about 2.2 s into the second part
        frame = over(frame, self.cap_find, smooth((i - 4) / 8) * (1 - smooth((i - (switch - 6)) / 6)))
        frame = over(frame, self.speed, smooth((i - 4) / 6) * (1 - smooth((i - (t1 - 4)) / 6)))
        frame = over(frame, self.cap_shop, smooth((i - switch) / 6))
        return frame


# ---------------- scene 3: the Chrome extension on a feed post ----------------
class Feed:
    def __init__(self):
        start = EVENTS["scrolled"] - 0.4 + 0.10   # feed video time = event time + 0.10 s
        end = EVENTS["click"] + 1.1 + 0.10
        self.clip = Clip(TAKE / "feed.webm", start, end, (374, 0, 506, 900), (W, H), matrix="bt601")
        self.length = self.clip.n
        self.cap = caption("Works on any photo\nin your feed", y=1290, size=70, sub="Rewear for Chrome · Shop this look")

    def render(self, i):
        return over(self.clip.frame(i), self.cap, smooth((i - 4) / 8))


# ---------------- scene 4: SAM masks on a three-person photo + Muse Image previews ----------------
class Masks:
    def __init__(self):
        self.length = int(6.4 * FPS)
        source = Image.open(PROD / "artifacts" / ART["source"]).convert("RGB")
        pw, ph = W - 80, int((W - 80) * source.height / source.width)
        self.box = (40, 470, pw, ph)
        base = Image.new("RGB", (W, H), tuple(int(v) for v in DEEP))
        rounded = Image.new("L", (pw, ph), 0)
        ImageDraw.Draw(rounded).rounded_rectangle((0, 0, pw - 1, ph - 1), 28, fill=255)
        base.paste(source.resize((pw, ph), Image.LANCZOS), (40, 470), rounded)
        self.base = np.asarray(base, np.float32)
        palette = [(64, 140, 255), (255, 142, 64), (150, 120, 235), (60, 200, 160), (255, 96, 140), (250, 204, 70), (90, 190, 240), (190, 230, 90)]
        self.masks = []
        for index, piece in enumerate(ART["pieces"]):
            m = np.zeros((H, W), np.float32)
            mask = np.asarray(Image.open(PROD / "artifacts" / piece["mask"]).convert("L").resize((pw, ph), Image.LANCZOS), np.float32) / 255
            m[470:470 + ph, 40:40 + pw] = mask * (np.asarray(rounded, np.float32) / 255)
            edge = np.asarray(Image.fromarray((m * 255).astype(np.uint8)).filter(ImageFilter.FIND_EDGES).filter(ImageFilter.GaussianBlur(1.4)), np.float32) / 255
            alpha = np.clip(m * 0.45 + np.clip(edge * 3, 0, 1) * 0.95, 0, 1)[..., None]
            self.masks.append((np.array(palette[index % len(palette)], np.float32) * alpha, alpha))
        title = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(title)
        d.text((W / 2, 190), f"{len(ART['pieces'])} pieces", font=font("serif", 128), fill=CREAM + (255,), anchor="ma")
        d.text((W / 2, 345), "from one photo", font=font("demi", 50), fill=(243, 214, 186, 255), anchor="ma")
        self.title = rgba(title)
        cards = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        cd = ImageDraw.Draw(cards)
        y = 470 + ph + 70
        for k, piece in enumerate(ART["previews"][:3]):
            x = 60 + k * 330
            card = Image.new("RGB", (300, 360), (246, 243, 236))
            image = Image.open(PROD / "artifacts" / piece["preview"]).convert("RGB")
            image.thumbnail((270, 300))
            card.paste(image, ((300 - image.width) // 2, 14 + (300 - image.height) // 2))
            mask = Image.new("L", (300, 360), 0)
            ImageDraw.Draw(mask).rounded_rectangle((0, 0, 299, 359), 22, fill=255)
            cards.paste(card, (x, y), mask)
            cd.rounded_rectangle((x + 14, y + 318, x + 140, y + 348), 10, fill=(150, 120, 235, 240))
            cd.text((x + 77, y + 333), "AI preview", font=font("demi", 19), fill=(255, 255, 255, 255), anchor="mm")
        cd.text((W / 2, y + 400), "Meta SAM 3.1 · Muse Spark 1.3 · Muse Image 1.0", font=font("medium", 34), fill=MUTED + (255,), anchor="ma")
        self.cards = rgba(cards)

    def render(self, i):
        t = i / FPS
        frame = self.base.copy()
        for index, mask in enumerate(self.masks):
            frame = over(frame, mask, smooth((t - 0.4 - index * 0.22) / 0.3))
        frame = over(frame, self.title, smooth((t - 0.2) / 0.4))
        return over(frame, self.cards, smooth((t - 2.6) / 0.5))


# ---------------- scene 5: end card ----------------
class End:
    def __init__(self):
        self.length = int(4.6 * FPS)
        image = Image.new("RGB", (W, H), tuple(int(v) for v in DEEP))
        y, x = np.mgrid[0:H, 0:W].astype(np.float32)
        glow = np.clip(1 - np.sqrt(((x - W / 2) / W) ** 2 + ((y - H * 0.42) / H) ** 2) / 0.7, 0, 1) ** 1.6
        base = DEEP + (np.array((40, 72, 60), np.float32) - DEEP) * glow[..., None]
        image = Image.fromarray(np.clip(base, 0, 255).astype(np.uint8)).convert("RGBA")
        d = ImageDraw.Draw(image)
        mark = Image.open(EXT_ICON).convert("RGBA").resize((150, 150), Image.LANCZOS)
        image.alpha_composite(mark, (W // 2 - 75, 560))
        d.text((W / 2, 760), "Rewear Studio", font=font("serif", 110), fill=CREAM + (255,), anchor="ma")
        d.text((W / 2, 915), "SEE IT · FIND IT · BUY IT", font=font("demi", 40), fill=GOLD + (255,), anchor="ma")
        d.rounded_rectangle((W / 2 - 330, 1040, W / 2 + 330, 1140), 50, fill=(255, 253, 248, 255))
        d.text((W / 2, 1090), "rewear.soraing.com", font=font("demi", 46), fill=(37, 70, 60, 255), anchor="mm")
        d.text((W / 2, 1220), "Built with Meta AI", font=font("medium", 38), fill=MUTED + (255,), anchor="ma")
        d.text((W / 2, 1275), "Meta Global AI Developer Hackathon 2026", font=font("medium", 34), fill=MUTED + (255,), anchor="ma")
        self.image = np.asarray(image.convert("RGB"), np.float32)

    def render(self, i):
        return self.image


hook, app, feed, masks, end = Hook(), App(), Feed(), Masks(), End()
scenes = [(hook, 0), (app, 8), (feed, 8), (masks, 8), (end, 10)]
starts, cursor = [], 0
for scene, overlap in scenes:
    starts.append(cursor - overlap)
    cursor = starts[-1] + scene.length
TOTAL = cursor


def compose(k):
    frame = None
    for (scene, overlap), start in zip(scenes, starts):
        j = k - start
        if 0 <= j < scene.length:
            image = scene.render(j)
            frame = image if frame is None else frame * (1 - smooth((j + 1) / (overlap + 1))) + image * smooth((j + 1) / (overlap + 1))
    fade = smooth(min((k + 1) / 6, (TOTAL - k) / 12))
    return np.clip(frame * fade + 0.5, 0, 255).astype(np.uint8)


timeline = {"total": TOTAL / FPS, "starts": [s / FPS for s in starts], "freeze": hook.freeze_at / FPS,
            "app_parts": [float(o) / FPS for o in app.offsets]}
print(json.dumps(timeline))
if PREVIEW:
    (BUILD / "short-preview").mkdir(exist_ok=True)
    for k in sorted(PREVIEW):
        Image.fromarray(compose(k)).save(BUILD / "short-preview" / f"s-{k:04d}.png")
    sys.exit(0)

video = BUILD / "short-video.mp4"
encoder = subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-",
                            "-vf", "scale=out_color_matrix=bt709:out_range=tv,format=yuv420p", "-c:v", "libx264", "-preset", "slow", "-crf", "18",
                            "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv", str(video)],
                           stdin=subprocess.PIPE)
for k in range(TOTAL):
    encoder.stdin.write(compose(k).tobytes())
encoder.stdin.close()
if encoder.wait():
    raise SystemExit("encode failed")

# ---------------- sound: lo-fi bed ducked under the narration, shutter click, whooshes, chime ----------------
seconds = TOTAL / FPS
n = int(round(seconds * RATE))


def decode(path, channels):
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-ac", str(channels), "-ar", str(RATE), "-f", "f32le", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.float32).reshape(-1, channels)


cues = [("x1", 0.45), ("x2", starts[1] / FPS + 0.35), ("x3", starts[1] / FPS + (app.offsets[1] + 66) / FPS),
        ("x4", starts[2] / FPS + 0.3), ("x5", starts[3] / FPS + 0.5), ("x6", starts[4] / FPS + 0.6)]
voice = np.zeros(n, np.float32)
for name, at in cues:
    clip = decode(PROD / "voice-short" / f"{name}.wav", 1)[:, 0]
    clip = clip * (10 ** (-16 / 20) / max(np.sqrt(np.mean(clip[np.abs(clip) > 0.01] ** 2)), 1e-6))
    s = int(at * RATE)
    voice[s:s + len(clip)] += clip[: n - s]
music_wav = BUILD / "short-music.wav"
subprocess.run(["uv", "run", "python", str(PROD / "music.py"), str(music_wav), f"{seconds + 0.5:.2f}", f"{starts[1] / FPS:.2f}"],
               check=True, capture_output=True, cwd=PROD.parents[1])
music = decode(music_wav, 2)[:n]
music = np.pad(music, ((0, n - len(music)), (0, 0))) * 0.62
active = np.convolve((np.abs(voice) > 0.004).astype(np.float32), np.ones(int(0.25 * RATE)) / int(0.25 * RATE), "same")
music *= (1 - 0.65 * np.clip(active * 2, 0, 1))[:, None]
rng = np.random.default_rng(9)
fx = np.zeros(n, np.float32)
t0 = hook.freeze_at / FPS
for at, length, gain in ((t0, 0.02, 0.4), (t0 + 0.07, 0.035, 0.28)):
    k = int(length * RATE)
    burst = np.diff(rng.standard_normal(k), prepend=0) * np.exp(-np.arange(k) / (0.004 * RATE))
    fx[int(at * RATE):int(at * RATE) + k] += (burst / np.abs(burst).max() * gain)[: n - int(at * RATE)]
for at in [s / FPS for s in starts[1:]]:
    k = int(0.6 * RATE)
    x = np.linspace(0, 1, k)
    spectrum = np.fft.rfft(rng.standard_normal(k))
    noise = np.fft.irfft(spectrum / np.sqrt(1 + (np.fft.rfftfreq(k, 1 / RATE) / 1800) ** 4), n=k)
    sw = noise * np.sin(np.pi * np.clip(x / 0.6, 0, 1) / 2) ** 2 * np.clip((1 - x) / 0.4, 0, 1) ** 2
    s = int(max(0, at - 0.3) * RATE)
    fx[s:s + k] += (sw / np.abs(sw).max() * 0.06)[: n - s]
k = int(1.4 * RATE)
tt = np.arange(k) / RATE
chime = sum(np.sin(2 * np.pi * f * np.clip(tt - d, 0, None)) * np.exp(-np.clip(tt - d, 0, None) / 0.45) * (tt >= d)
            for d, f in ((0, 1046.5), (0.09, 1318.5), (0.18, 1568.0)))
s = int((starts[3] / FPS + 0.3) * RATE)
fx[s:s + k] += (chime / np.abs(chime).max() * 0.05)[: n - s]
mix = music + (voice + fx)[:, None]
mix_path = BUILD / "short-mix.wav"
with wave.open(str(mix_path), "wb") as out:
    out.setnchannels(2)
    out.setsampwidth(2)
    out.setframerate(RATE)
    out.writeframes((np.clip(mix, -1, 1) * 32767).astype(np.int16).tobytes())
subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(video), "-i", str(mix_path), "-map", "0:v", "-map", "1:a", "-c:v", "copy",
                "-af", "loudnorm=I=-14:TP=-1.5:LRA=9", "-ar", str(RATE), "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
                "-t", f"{seconds:.3f}", str(OUT)], check=True)
print("short written:", OUT, f"{seconds:.1f}s")

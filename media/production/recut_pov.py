"""Re-cut the POV film's intro: a quicker hook with title cards, a shutter flash + click, a push into the
phone screen that dissolves straight into the app, and the glitched zoom frames removed.
Frames are composited in Python and piped to ffmpeg (one ffmpeg graph with seven trims of the same input stalls).
Usage: recut2.py <source.mp4> <source.srt> <out.mp4> <out.srt> <work_dir> [preview output frames, comma-separated]"""
import json
import math
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

SRC, SRC_SRT, OUT, OUT_SRT, WORK = (Path(arg) for arg in sys.argv[1:6])
PREVIEW = [int(v) for v in sys.argv[6].split(",")] if len(sys.argv) > 6 else None
WORK.mkdir(parents=True, exist_ok=True)
W, H, FPS, RATE = 1920, 1080, 30, 44100
SERIF = "/System/Library/Fonts/Supplemental/Iowan Old Style.ttc"
SANS = "/System/Library/Fonts/Avenir Next.ttc"

FREEZE = 336                     # source frame at 11.2 s: the shutter moment
PHONE = (815.0, 756.0)           # phone-screen centre in that frame
PHONE_TILT = math.radians(6.5)   # the phone leans right; the push straightens it
ZOOM = 2.2
# (name, first source frame, frame count, dissolve frames from the previous piece).
# Source frames 425-428, 819-844 and 906-909 are glitched zoom frames and are skipped.
PIECES = [
    ("walk", 0, 78, 0),
    ("raise", 171, 57, 9),
    ("frame", 282, FREEZE - 282 + 1, 9),
    ("capture", FREEZE, 27, 0),
    ("scan", 430, 389, 10),
    ("results", 845, 61, 6),
    ("detail", 910, 158, 0),
]
starts, cursor = [], 0
for _, _, count, dissolve in PIECES:
    starts.append(cursor - dissolve)
    cursor = starts[-1] + count
TOTAL = cursor
T_CAPTURE = starts[3]
FLASH = [0.5, 0.9, 0.78, 0.63, 0.49, 0.37, 0.27, 0.18, 0.11, 0.06, 0.025]
FADE_IN, FADE_OUT = 12, 18


def font(path, size, index=0):
    return ImageFont.truetype(path, size, index=index)


def smooth(x):
    x = min(max(x, 0.0), 1.0)
    return x * x * (3 - 2 * x)


# ---------- title cards (premultiplied RGBA over the left of the frame) ----------
CARD_W = 1180


def spaced(draw, xy, text, fnt, fill, tracking):
    x, y = xy
    for char in text:
        draw.text((x, y), char, font=fnt, fill=fill)
        x += draw.textlength(char, font=fnt) + tracking


def card(name, draw_text):
    shade = np.zeros((H, CARD_W), np.float32)
    shade[:] = (1 - np.arange(CARD_W) / CARD_W) ** 1.5 * 0.52
    text = Image.new("RGBA", (CARD_W, H), (0, 0, 0, 0))
    draw_text(ImageDraw.Draw(text))
    shadow = Image.new("RGBA", (CARD_W, H), (0, 0, 0, 0))
    shadow.putalpha(text.getchannel("A").filter(ImageFilter.GaussianBlur(6)).point(lambda v: v * 0.45))
    rgba = Image.alpha_composite(Image.new("RGBA", (CARD_W, H), (8, 18, 14, 0)), shadow)
    rgba = Image.alpha_composite(rgba, text)
    rgb = np.asarray(rgba, np.float32)[..., :3]
    text_alpha = np.asarray(rgba, np.float32)[..., 3] / 255
    alpha = text_alpha + shade * (1 - text_alpha)
    color = (rgb * text_alpha[..., None] + np.array([8, 18, 14], np.float32) * (shade * (1 - text_alpha))[..., None])
    Image.fromarray(np.clip(np.dstack([color / np.maximum(alpha, 1e-6)[..., None], alpha[..., None] * 255]), 0, 255).astype(np.uint8)).save(WORK / f"{name}.png")
    return color, alpha[..., None]


def hook_text(d):
    spaced(d, (132, 262), "REWEAR STUDIO", font(SANS, 25, 2), (243, 214, 186, 255), 5)
    d.text((126, 304), "Spot an outfit", font=font(SERIF, 104), fill=(255, 253, 248, 255))
    d.text((126, 420), "you love?", font=font(SERIF, 104), fill=(255, 253, 248, 255))


def snap_text(d):
    d.text((126, 290), "Snap it.", font=font(SERIF, 112), fill=(255, 253, 248, 255))
    d.text((132, 428), "Meta AI finds every piece — ready to shop.", font=font(SANS, 34, 5), fill=(244, 238, 228, 255))


CARDS = [  # (premultiplied colour, alpha, fade-in start, fade-out start)
    (*card("card-hook", hook_text), 9, 60),
    (*card("card-snap", snap_text), 84, 156),
]


def card_level(k, fade_in, fade_out, length=12):
    return smooth((k - fade_in) / length) * (1 - smooth((k - fade_out) / length))


# ---------- source frames ----------
def layers(k):
    return [(index, k - starts[index]) for index, piece in enumerate(PIECES) if 0 <= k - starts[index] < piece[2]]


def source_index(index, j):
    name, first, _, _ = PIECES[index]
    return FREEZE if name == "capture" else first + j


wanted = PREVIEW or range(TOTAL)
last_use = {}
for k in wanted:
    for index, j in layers(k):
        last_use[source_index(index, j)] = k
decoder = subprocess.Popen(
    ["ffmpeg", "-v", "error", "-i", str(SRC), "-vf", "scale=in_color_matrix=bt709:in_range=tv,format=rgb24",
     "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], stdout=subprocess.PIPE)
cache, decoded = {}, 0


def source(n):
    global decoded
    while n not in cache:
        raw = decoder.stdout.read(W * H * 3)
        if len(raw) < W * H * 3:
            raise RuntimeError(f"source ended before frame {n}")
        if decoded in last_use:
            cache[decoded] = np.frombuffer(raw, np.uint8).reshape(H, W, 3)
        decoded += 1
    return cache[n]


still = None


def push(j):
    """Freeze on the shutter frame and push into the phone screen, easing in and straightening the tilt."""
    global still
    if still is None:
        still = Image.fromarray(source(FREEZE))
    p = j / (PIECES[3][2] - 1)
    e = p ** 1.8
    z = 1 + (ZOOM - 1) * e
    cx, cy = W / 2 + (PHONE[0] - W / 2) * e, H / 2 + (PHONE[1] - H / 2) * e
    cos, sin = math.cos(PHONE_TILT * e) / z, math.sin(PHONE_TILT * e) / z
    a, b, d, e2 = cos, -sin, sin, cos
    c, f = cx - a * W / 2 - b * H / 2, cy - d * W / 2 - e2 * H / 2
    for xo, yo in ((0, 0), (W, 0), (0, H), (W, H)):
        xi, yi = a * xo + b * yo + c, d * xo + e2 * yo + f
        assert -0.5 <= xi <= W + 0.5 and -0.5 <= yi <= H + 0.5, (j, xi, yi)
    return np.asarray(still.transform((W, H), Image.Transform.AFFINE, (a, b, c, d, e2, f), resample=Image.Resampling.BICUBIC), np.float32)


def compose(k):
    frame = None
    for index, j in layers(k):
        name, _, _, dissolve = PIECES[index]
        if name == "capture":
            image = push(j)
            if j < len(FLASH):
                image = image * (1 - FLASH[j]) + 255 * FLASH[j]
        else:
            image = source(source_index(index, j)).astype(np.float32)
        if frame is None:
            frame = image
        else:
            weight = (j + 1) / (dissolve + 1)
            frame = frame * (1 - weight) + image * weight
    for color, alpha, fade_in, fade_out in CARDS:
        level = card_level(k, fade_in, fade_out)
        if level > 0:
            frame[:, :CARD_W] = frame[:, :CARD_W] * (1 - alpha * level) + color * level
    fade = smooth((k + 1) / FADE_IN) * smooth((TOTAL - k) / FADE_OUT)
    if fade < 1:
        frame = frame * fade
    for index, j in layers(k):
        n = source_index(index, j)
        if last_use.get(n) == k:
            cache.pop(n, None)
    return np.clip(frame + 0.5, 0, 255).astype(np.uint8)


if PREVIEW:
    for k in PREVIEW:
        Image.fromarray(compose(k)).save(WORK / f"preview-{k:04d}.png")
    decoder.kill()
    print(json.dumps({"total_frames": TOTAL, "starts": dict(zip([p[0] for p in PIECES], starts))}))
    sys.exit(0)

video_only = WORK / "video-only.mp4"
encoder = subprocess.Popen(
    ["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-",
     "-vf", "scale=out_color_matrix=bt709:out_range=tv,format=yuv420p", "-c:v", "libx264", "-preset", "slow", "-crf", "17",
     "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv", str(video_only)],
    stdin=subprocess.PIPE)
for k in range(TOTAL):
    encoder.stdin.write(compose(k).tobytes())
encoder.stdin.close()
if encoder.wait() != 0:
    raise SystemExit("video encode failed")
decoder.kill()


# ---------- sound: two-part shutter click on the capture, a soft whoosh under the push ----------
def lowpass(signal, cutoffs):
    out, y = np.empty_like(signal), 0.0
    coefficients = 1 - np.exp(-2 * np.pi * np.broadcast_to(cutoffs, signal.shape) / RATE)
    for i, (x, k) in enumerate(zip(signal, coefficients)):
        y += k * (x - y)
        out[i] = y
    return out


rng = np.random.default_rng(11)
seconds = TOTAL / FPS
fx = np.zeros(int(round(seconds * RATE)), np.float32)
t0 = T_CAPTURE / FPS


def place(at, sound):
    start = int(round(at * RATE))
    fx[start:start + len(sound)] += sound[: len(fx) - start]


def burst(duration, tau, peak, cutoff=None):
    n = int(duration * RATE)
    noise = rng.standard_normal(n).astype(np.float32)
    noise = np.diff(noise, prepend=0) if cutoff is None else lowpass(noise, cutoff)
    sound = noise * np.exp(-np.arange(n) / (tau * RATE))
    return sound / np.abs(sound).max() * peak


place(t0, burst(0.025, 0.0035, 0.36))
place(t0 + 0.068, burst(0.04, 0.007, 0.26, 4200.0))
n = int(0.78 * RATE)
x = np.linspace(0, 1, n)
whoosh = lowpass(rng.standard_normal(n).astype(np.float32), 300 * (2600 / 300) ** x)
whoosh *= np.sin(np.pi * np.clip(x / 0.72, 0, 1) / 2) ** 2 * np.clip((1 - x) / 0.28, 0, 1) ** 1.5
place(t0 + 0.12, whoosh / np.abs(whoosh).max() * 0.09)
with wave.open(str(WORK / "fx.wav"), "wb") as out:
    out.setnchannels(2)
    out.setsampwidth(2)
    out.setframerate(RATE)
    pcm = (np.clip(fx, -1, 1) * 32767).astype(np.int16)
    out.writeframes(np.repeat(pcm, 2).tobytes())

subprocess.run(
    ["ffmpeg", "-v", "error", "-y", "-i", str(video_only), "-i", str(SRC), "-i", str(WORK / "fx.wav"), "-filter_complex",
     f"[1:a]atrim=0:{seconds:.3f},asetpts=PTS-STARTPTS,afade=t=out:st={seconds - 0.8:.3f}:d=0.8[bed];"
     "[bed][2:a]amix=inputs=2:normalize=0:duration=first[a]",
     "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart",
     "-t", f"{seconds:.3f}", str(OUT)], check=True)


# ---------- subtitles retimed to the new cut ----------
def remap(t):
    frame = t * FPS
    for index, (name, first, count, _) in enumerate(PIECES):
        if name == "capture":
            continue
        if first <= frame < first + count:
            return (starts[index] + frame - first) / FPS
        if frame < first:
            return starts[index] / FPS
    return seconds


def stamp(value):
    ms = int(round(value * 1000))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def parse(value):
    h, m, rest = value.strip().split(":")
    s, ms = rest.split(",")
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000


cues = []
for block in [b for b in SRC_SRT.read_text(encoding="utf-8").strip().split("\n\n") if b.strip()]:
    lines = block.strip().splitlines()
    start, end = (parse(v) for v in lines[1].split("-->"))
    cues.append([remap(start), remap(end), "\n".join(lines[2:])])
# The intro is re-paced, so its four cues follow the new beats: walk, raise the phone, shutter, scan.
cues[0][:2] = [0.25, starts[1] / FPS + 0.25]
cues[1][:2] = [starts[1] / FPS + 0.3, t0 - 0.05]
cues[2][:2] = [t0, t0 + 0.8]
cues[3][0] = t0 + 0.85
cues[3][1] = min(cues[3][1], starts[5] / FPS - 0.05)
cues[4][0] = max(cues[4][0], starts[5] / FPS + 0.05)
OUT_SRT.write_text("\n\n".join(f"{i}\n{stamp(a)} --> {stamp(b)}\n{text}" for i, (a, b, text) in enumerate(cues, 1)) + "\n", encoding="utf-8")
print(json.dumps({"total_seconds": round(seconds, 3), "capture_at": round(t0, 3), "app_from": round(starts[4] / FPS, 3),
                  "starts": {p[0]: round(s / FPS, 3) for p, s in zip(PIECES, starts)}}))

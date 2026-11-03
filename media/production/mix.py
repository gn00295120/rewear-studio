"""Mix the narrated soundtrack at 48 kHz: voice-over, original lo-fi bed ducked under the voice,
the POV street ambience + shutter, and light UI sound design; then loudness-normalise and mux.
Usage (from media/production): python mix.py . build/video-clean.mp4 build/rewear-demo.mp4 [build/rewear-demo.en.srt]"""
import json
import subprocess
import sys
from pathlib import Path

import numpy as np

WORK, VIDEO, OUT = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
SOFT_SRT = Path(sys.argv[4]) if len(sys.argv) > 4 else None
RATE = 48000
ROOT = Path(__file__).resolve().parents[2]
BUILD = WORK / "build"
line = json.loads((BUILD / "timeline.json").read_text())
total = line["total"]
n = int(round(total * RATE))
rng = np.random.default_rng(5)


def decode(path, channels, start=0.0, duration=None):
    cmd = ["ffmpeg", "-v", "error", "-ss", f"{start}"] + (["-t", f"{duration}"] if duration else []) + [
        "-i", str(path), "-vn", "-ac", str(channels), "-ar", str(RATE), "-f", "f32le", "-"]
    data = np.frombuffer(subprocess.run(cmd, capture_output=True, check=True).stdout, np.float32)
    return data.reshape(-1, channels)


def fit(signal, length=n):
    out = np.zeros((length, signal.shape[1]), np.float32)
    out[: min(length, len(signal))] = signal[:length]
    return out


def smooth_envelope(x, attack, release):
    """One-pole follower on a decimated signal (fast), then upsampled."""
    hop = 240
    blocks = x[: len(x) // hop * hop].reshape(-1, hop).max(axis=1)
    env, y = np.zeros_like(blocks), 0.0
    a, r = np.exp(-hop / (attack * RATE)), np.exp(-hop / (release * RATE))
    for i, v in enumerate(blocks):
        y = a * y + (1 - a) * v if v > y else r * y + (1 - r) * v
        env[i] = y
    return np.interp(np.arange(len(x)), np.arange(len(env)) * hop + hop / 2, env)


# ---- voice-over: each line at its cue, levelled to the same loudness ----
voice = np.zeros(n, np.float32)
for cue in json.loads((BUILD / "voice-cues.json").read_text()):
    clip = decode(WORK / "voice" / f"{cue['id']}.wav", 1)[:, 0]
    rms = np.sqrt(np.mean(clip[np.abs(clip) > 0.01] ** 2))
    clip = clip * (10 ** (-17 / 20) / max(rms, 1e-6))
    s = int(cue["start"] * RATE)
    voice[s:s + len(clip)] += clip[: n - s]
active = smooth_envelope((np.abs(voice) > 0.004).astype(np.float32), 0.06, 0.45)
duck = 1 - 0.68 * np.clip(active, 0, 1)  # about -10 dB under speech

# ---- music bed (regenerated to length) and the POV street ambience ----
music_wav = BUILD / "music.wav"
subprocess.run(["uv", "run", "python", str(Path(__file__).resolve().parent / "music.py"), str(music_wav), f"{total + 0.5:.2f}", "6.4"], check=True,
               cwd=ROOT, capture_output=True)
music = fit(decode(music_wav, 2)) * 0.6
t = np.arange(n) / RATE
music *= (np.clip(t / 1.2, 0, 1) * np.clip((total - t) / 3.0, 0, 1))[:, None]
street = fit(decode(Path(__file__).resolve().parent / "sources" / "rewear-pov-film-v2.mp4", 2, 0, 6.9), n) * 0.85
street *= np.clip((6.9 - t) / 0.9, 0, 1)[:, None]

# ---- sound design: soft whooshes on scene changes, the extension click, a chime on "8 pieces found" ----
fx = np.zeros(n, np.float32)


def place(at, sound, gain):
    s = int(at * RATE)
    e = min(n, s + len(sound))
    fx[s:e] += sound[: e - s] * gain


def lowpass(signal, cutoff):
    spectrum = np.fft.rfft(signal)
    freqs = np.fft.rfftfreq(len(signal), 1 / RATE)
    return np.fft.irfft(spectrum / np.sqrt(1 + (freqs / cutoff) ** 4), n=len(signal))


def whoosh(length=0.7):
    k = int(length * RATE)
    x = np.linspace(0, 1, k)
    out = lowpass(rng.standard_normal(k), 1800) * np.sin(np.pi * np.clip(x / 0.65, 0, 1) / 2) ** 2 * np.clip((1 - x) / 0.35, 0, 1) ** 2
    return out / np.abs(out).max()


def click():
    k = int(0.03 * RATE)
    local = np.arange(k) / RATE
    out = np.diff(rng.standard_normal(k), prepend=0) * np.exp(-local / 0.004) + np.sin(2 * np.pi * 2400 * local) * np.exp(-local / 0.006) * 0.6
    return out / np.abs(out).max()


def chime():
    k = int(1.6 * RATE)
    local = np.arange(k) / RATE
    out = np.zeros(k)
    for delay, freq in ((0.0, 1046.5), (0.09, 1318.5), (0.18, 1568.0)):
        d = int(delay * RATE)
        tt = local[: k - d]
        out[d:] += (np.sin(2 * np.pi * freq * tt) + 0.25 * np.sin(2 * np.pi * freq * 2.76 * tt) * np.exp(-tt / 0.08)) * np.exp(-tt / 0.5) * np.clip(tt / 0.003, 0, 1)
    return out / np.abs(out).max()


for at in line["starts"][1:]:
    place(at - 0.35, whoosh(), 0.06)
ext = line["ext_start"]
place(ext + line["ext_click"], click(), 0.16)
place(ext + line["ext_parts"][3] + 0.3, chime(), 0.06)

mix = music * duck[:, None] + street * (1 - 0.5 * np.clip(active, 0, 1))[:, None] + fx[:, None] + voice[:, None]
mix_path = BUILD / "mix.wav"
pcm = (np.clip(mix, -1, 1) * 32767).astype(np.int16)
import wave  # noqa: E402

with wave.open(str(mix_path), "wb") as out:
    out.setnchannels(2)
    out.setsampwidth(2)
    out.setframerate(RATE)
    out.writeframes(pcm.tobytes())

# Two-pass loudness normalisation to -16 LUFS / -1.5 dBTP.
probe = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(mix_path), "-af", "loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json", "-f", "null", "-"],
                       capture_output=True, text=True).stderr
stats = json.loads(probe[probe.rindex("{"): probe.rindex("}") + 1])
norm = (f"loudnorm=I=-16:TP=-1.5:LRA=11:measured_I={stats['input_i']}:measured_TP={stats['input_tp']}:"
        f"measured_LRA={stats['input_lra']}:measured_thresh={stats['input_thresh']}:offset={stats['target_offset']}:linear=true")
cmd = ["ffmpeg", "-v", "error", "-y", "-i", str(VIDEO), "-i", str(mix_path)]
if SOFT_SRT:
    cmd += ["-i", str(SOFT_SRT)]
cmd += ["-map", "0:v", "-map", "1:a"] + (["-map", "2:s"] if SOFT_SRT else []) + [
    "-c:v", "copy", "-af", norm, "-ar", str(RATE), "-c:a", "aac", "-b:a", "192k"]
if SOFT_SRT:
    cmd += ["-c:s", "mov_text", "-metadata:s:s:0", "language=eng", "-metadata:s:s:0", "title=English"]
cmd += ["-movflags", "+faststart", "-t", f"{total:.3f}", str(OUT)]
subprocess.run(cmd, check=True)
voice_rms = 20 * np.log10(np.sqrt(np.mean(voice[np.abs(voice) > 0.01] ** 2)))
bed = music * duck[:, None]
under = 20 * np.log10(np.sqrt(np.mean(bed[active > 0.9] ** 2)) + 1e-9)
print(json.dumps({"out": str(OUT), "voice_rms_db": round(float(voice_rms), 1), "music_under_voice_db": round(float(under), 1),
                  "measured_input_lufs": stats["input_i"]}))

"""Synthesise an original, royalty-free lo-fi bed: warm pad, electric-piano arpeggio, sub bass and soft drums.
Usage: music.py <out.wav> <seconds> [drums_in=6.3] [drop_from=0] [drop_to=0]"""
import sys
import wave

import numpy as np

OUT = sys.argv[1]
LENGTH = float(sys.argv[2])
DRUMS_IN = float(sys.argv[3]) if len(sys.argv) > 3 else 6.3
DROP = (float(sys.argv[4]), float(sys.argv[5])) if len(sys.argv) > 5 else (0.0, 0.0)
RATE, BPM = 44100, 88
BEAT = 60 / BPM
BAR = 4 * BEAT
rng = np.random.default_rng(2041)

# Eight bars: Fmaj7 Fmaj7 Am7 Am7 Dm9 Dm9 Bbmaj7 C6/9 (MIDI notes) and their bass roots.
CHORDS = [[53, 57, 60, 64], [53, 57, 60, 64], [57, 60, 64, 67], [57, 60, 64, 67],
          [50, 53, 57, 60, 64], [50, 53, 57, 60, 64], [46, 50, 53, 57], [48, 55, 57, 62]]
ROOTS = [41, 41, 45, 45, 38, 38, 34, 36]
LOOP = int(round(8 * BAR * RATE))
t = np.arange(LOOP) / RATE


def hz(note):
    return 440.0 * 2 ** ((note - 69) / 12)


def fft_filter(signal, low=None, high=None, q=1.0):
    """Circular (loop-seamless) filtering with smooth responses."""
    spectrum = np.fft.rfft(signal, axis=0)
    freqs = np.fft.rfftfreq(signal.shape[0], 1 / RATE)
    response = np.ones_like(freqs)
    if low:
        response *= 1 / np.sqrt(1 + (freqs / low) ** (4 * q))
    if high:
        response *= 1 / np.sqrt(1 + (high / np.maximum(freqs, 1e-3)) ** (4 * q))
    shape = (-1,) + (1,) * (signal.ndim - 1)
    return np.fft.irfft(spectrum * response.reshape(shape), n=signal.shape[0], axis=0)


def chord_envelope(index, attack=0.35, release=0.9):
    start, end = index * BAR, (index + 1) * BAR
    rise = np.clip((t - start) / attack, 0, 1)
    fall = np.clip(1 - (t - end) / release, 0, 1)
    gate = np.where(t < end, rise, fall) * (t >= start)
    # wrap the release of the last bar into the loop start
    if index == 7:
        gate = np.maximum(gate, np.clip(1 - (t + LOOP / RATE - end) / release, 0, 1) * (t < release))
    return np.sin(gate * np.pi / 2) ** 2


# ---- pad: detuned band-limited saws, held across each bar pair ----
pad = np.zeros((LOOP, 2))
for index, chord in enumerate(CHORDS):
    env = chord_envelope(index)
    for note in chord:
        for side, cents in ((0, -7), (1, 6), (0, 3), (1, -4)):
            f0 = hz(note) * 2 ** (cents / 1200)
            phase = rng.uniform(0, 2 * np.pi)
            wave_ = sum(np.sin(2 * np.pi * f0 * k * t + phase * k) / k for k in range(1, 10) if f0 * k < 9000)
            pad[:, side] += wave_ * env * 0.045
pad = fft_filter(pad, low=1300, q=0.8)

# ---- electric-piano arpeggio in eighths with a little swing ----
keys = np.zeros((LOOP, 2))
pattern = [0, 2, 1, 3, 2, 1, 3, 2]
for bar, chord in enumerate(CHORDS):
    tones = sorted(chord)[-4:]
    for step in range(8):
        if step in (5,) and bar % 2:
            continue
        start = bar * BAR + step * BEAT / 2 + (0.035 if step % 2 else 0)
        note = tones[pattern[step] % len(tones)] + 12
        n = int(1.4 * RATE)
        s = int(start * RATE)
        local = np.arange(n) / RATE
        f0 = hz(note)
        tone = (np.sin(2 * np.pi * f0 * local) + 0.28 * np.sin(4 * np.pi * f0 * local)
                + 0.12 * np.sin(2 * np.pi * 3.5 * f0 * local) * np.exp(-local / 0.05))
        tone *= np.exp(-local / 0.42) * np.clip(local / 0.004, 0, 1) * (0.06 if step % 2 else 0.075)
        pan = 0.35 + 0.3 * ((step + bar) % 2)
        idx = (s + np.arange(n)) % LOOP
        np.add.at(keys[:, 0], idx, tone * (1 - pan))
        np.add.at(keys[:, 1], idx, tone * pan)
keys = fft_filter(keys, low=4200, q=0.7)

# ---- sub bass: root on beat 1, fifth pickup on beat 4 ----
bass = np.zeros(LOOP)
for bar, root in enumerate(ROOTS):
    for beat, note, length in ((0, root, 2.6), (3, root + 7, 0.9)):
        s = int((bar * BAR + beat * BEAT) * RATE)
        n = int(length * BEAT * RATE)
        local = np.arange(n) / RATE
        tone = np.sin(2 * np.pi * hz(note) * local) + 0.15 * np.sin(4 * np.pi * hz(note) * local)
        tone *= np.clip(local / 0.01, 0, 1) * np.clip((length * BEAT - local) / 0.08, 0, 1) * 0.16
        idx = (s + np.arange(n)) % LOOP
        np.add.at(bass, idx, tone)

# ---- drums: soft kick, rim on 2 & 4, swung hats ----
drums = np.zeros((LOOP, 2))
hats = np.zeros((LOOP, 2))
duck = np.ones(LOOP)
for bar in range(8):
    for beat in range(4):
        start = bar * BAR + beat * BEAT
        s = int(start * RATE)
        if beat in (0, 2) or (beat == 3 and bar % 4 == 3):
            if beat == 3:
                s += int(BEAT / 2 * RATE)
            n = int(0.35 * RATE)
            local = np.arange(n) / RATE
            freq = 48 + 70 * np.exp(-local / 0.03)
            kick = np.sin(2 * np.pi * np.cumsum(freq) / RATE) * np.exp(-local / 0.16) * 0.42
            idx = (s + np.arange(n)) % LOOP
            for side in (0, 1):
                np.add.at(drums[:, side], idx, kick)
            dip = 1 - 0.35 * np.exp(-local / 0.12)
            duck[idx] = np.minimum(duck[idx], dip)
        if beat in (1, 3):
            n = int(0.12 * RATE)
            local = np.arange(n) / RATE
            noise = fft_filter(rng.standard_normal(n), low=3200, high=900, q=0.7)
            rim = noise / np.abs(noise).max() * np.exp(-local / 0.035) * 0.11
            idx = (s + np.arange(n)) % LOOP
            np.add.at(drums[:, 0], idx, rim * 0.9)
            np.add.at(drums[:, 1], idx, rim)
        for half in (0, 1):
            hs = int((start + half * BEAT / 2 + (0.045 if half else 0)) * RATE)
            n = int(0.05 * RATE)
            local = np.arange(n) / RATE
            hat = rng.standard_normal(n) * np.exp(-local / (0.014 if half else 0.009)) * (0.05 if half else 0.03)
            idx = (hs + np.arange(n)) % LOOP
            np.add.at(hats[:, 1 if half else 0], idx, hat)
drums = fft_filter(drums, high=40, q=0.7) + 0.55 * fft_filter(hats, low=9000, high=5000, q=0.6)

# ---- arrangement over the requested length ----
total = int(LENGTH * RATE)
reps = total // LOOP + 2
time_axis = np.arange(total) / RATE


def tile(signal):
    return np.tile(signal, (reps,) + (1,) * (signal.ndim - 1))[:total]


pad_full, keys_full, bass_full, drums_full, duck_full = tile(pad), tile(keys), tile(bass), tile(drums), tile(duck)
drum_gate = np.clip((time_axis - DRUMS_IN) / 0.6, 0, 1)
if DROP[1] > DROP[0]:
    drop = np.clip((time_axis - DROP[0]) / 0.8, 0, 1) * np.clip((DROP[1] - time_axis) / 0.8, 0, 1)
    drum_gate *= 1 - drop
    keys_gain = 1 - 0.45 * drop
else:
    keys_gain = np.ones(total)
keys_in = np.clip(time_axis / 2.5, 0, 1)
mix = pad_full * (0.85 + 0.15 * (1 - drum_gate))[:, None] * (1 - (1 - duck_full) * drum_gate)[:, None]
mix += keys_full * (keys_in * keys_gain)[:, None]
mix += (bass_full * np.clip((time_axis - DRUMS_IN + 2.7) / 1.5, 0, 1))[:, None]
mix += drums_full * drum_gate[:, None]
mix = np.tanh(mix * 1.15) / 1.15
fade_in = np.clip(time_axis / 1.2, 0, 1)
fade_out = np.clip((LENGTH - time_axis) / 3.0, 0, 1)
mix *= (fade_in * fade_out)[:, None]
mix *= 10 ** (-3 / 20) / max(np.abs(mix).max(), 1e-6)
with wave.open(OUT, "wb") as out:
    out.setnchannels(2)
    out.setsampwidth(2)
    out.setframerate(RATE)
    out.writeframes((mix * 32767).astype(np.int16).tobytes())
rms = 20 * np.log10(np.sqrt((mix ** 2).mean()))
print(f"wrote {OUT}: {LENGTH:.1f}s, rms {rms:.1f} dBFS, loop {LOOP / RATE:.2f}s")

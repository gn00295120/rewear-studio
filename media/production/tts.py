"""Synthesise every narration line with Kokoro-82M (Apache-2.0, runs locally) and report durations and phonemes.
Usage (isolated env): uv run --no-project --python 3.12 --with kokoro --with soundfile python tts.py <narration.json> <out_dir>"""
import json
import sys
from pathlib import Path

import numpy as np
import soundfile as sf
from kokoro import KPipeline

plan = json.loads(Path(sys.argv[1]).read_text())
out = Path(sys.argv[2])
out.mkdir(parents=True, exist_ok=True)
pipe = KPipeline(lang_code="a", repo_id="hexgrad/Kokoro-82M")
report = {}
for line in plan["lines"]:
    chunks, phonemes = [], []
    for graphemes, phones, audio in pipe(line["say"], voice=plan["voice"], speed=plan["speed"]):
        chunks.append(audio.numpy() if hasattr(audio, "numpy") else np.asarray(audio))
        phonemes.append(phones)
    audio = np.concatenate(chunks).astype(np.float32)
    # trim leading/trailing near-silence so cue times line up with the first syllable
    level = np.abs(audio)
    voiced = np.where(level > 0.01)[0]
    audio = audio[max(0, voiced[0] - 240): voiced[-1] + 2400] if len(voiced) else audio
    sf.write(out / f"{line['id']}.wav", audio, 24000)
    report[line["id"]] = {"seconds": round(len(audio) / 24000, 3), "phonemes": " | ".join(phonemes)}
    print(f"{line['id']:>4} {len(audio) / 24000:5.2f}s  {' | '.join(phonemes)}", flush=True)
(out / "durations.json").write_text(json.dumps(report, indent=1, ensure_ascii=False))

"""Place each narration line on the film timeline; write subtitles.json (for burning in) and an English SRT.
Usage (from media/production): python subs.py . build/rewear-demo.en.srt"""
import json
import re
import sys
from pathlib import Path

WORK, SRT = Path(sys.argv[1]), Path(sys.argv[2])
plan = json.loads((WORK / "narration.json").read_text())
line_t = json.loads((WORK / "build" / "timeline.json").read_text())
durations = json.loads((WORK / "voice" / "durations.json").read_text())
starts = line_t["starts"]
anchors = {"pov_result": line_t["pov_result"], "pov_shop": line_t["pov_shop"], "ext_pill": line_t["ext_pill"]}
anchors.update({f"ext_part{i}": v for i, v in enumerate(line_t["ext_parts"])})


def when(line):
    at = line["at"]
    if isinstance(at, str):
        name, sign, offset = re.match(r"(\w+)([+-])([\d.]+)", at).groups()
        at = anchors[name] + (1 if sign == "+" else -1) * float(offset)
    return starts[line["scene"]] + at


def chunks(text, limit=84):
    parts = [p.strip() for p in re.split(r"(?<=[.?!:])\s+", text) if p.strip()]
    out = []
    for part in parts:
        while len(part) > limit:
            cut = max(part.rfind(", ", 0, limit), part.rfind(" and ", 0, limit))
            cut = cut if cut > 20 else part.rfind(" ", 0, limit)
            out.append(part[: cut + 1].strip())
            part = part[cut + 1:].strip()
        out.append(part)
    merged = []
    for part in out:  # keep very short sentences together with their neighbour
        if merged and len(merged[-1]) + len(part) + 1 <= 60 and len(part) < 24:
            merged[-1] = f"{merged[-1]} {part}"
        else:
            merged.append(part)
    return merged


cues, voice = [], []
for line in plan["lines"]:
    start = when(line)
    length = durations[line["id"]]["seconds"]
    voice.append({"id": line["id"], "start": round(start, 3), "end": round(start + length, 3)})
    pieces = chunks(line["show"])
    total = sum(len(p) for p in pieces)
    t = start
    for piece in pieces:
        span = length * len(piece) / total
        cues.append({"start": round(t, 3), "end": round(t + span, 3), "text": piece})
        t += span
for a, b in zip(voice, voice[1:]):
    assert a["end"] <= b["start"] + 0.01, f"narration overlaps: {a} {b}"
assert voice[-1]["end"] < line_t["total"] - 1, "narration runs past the end"
# Chunks of one sentence hand over instantly; separate lines fade in and out.
for cue, nxt in zip(cues, cues[1:] + [None]):
    joined = nxt is not None and nxt["start"] - cue["end"] < 0.2
    cue["to"] = round(nxt["start"], 3) if joined else round(cue["end"] + 0.2, 3)
    cue["fade_out"] = not joined
    if nxt is not None:
        nxt["fade_in"] = not joined
cues[0]["fade_in"] = True
(WORK / "build" / "subtitles.json").write_text(json.dumps(cues, indent=1, ensure_ascii=False))
(WORK / "build" / "voice-cues.json").write_text(json.dumps(voice, indent=1))


def stamp(value):
    ms = int(round(value * 1000))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


SRT.write_text("\n\n".join(f"{i}\n{stamp(c['start'])} --> {stamp(c['to'] - (0 if c['fade_out'] else 0.02))}\n{c['text']}" for i, c in enumerate(cues, 1)) + "\n", encoding="utf-8")
for v in voice:
    print(f"{v['id']:>4} {v['start']:7.2f} -> {v['end']:7.2f}")
print(len(cues), "subtitle cues")

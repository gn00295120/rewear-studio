import json, re, subprocess, sys
import numpy as np
from faster_whisper import WhisperModel
audio, plan = sys.argv[1], json.load(open(sys.argv[2]))
pcm = np.frombuffer(subprocess.run(["ffmpeg", "-v", "error", "-i", audio, "-ac", "1", "-ar", "16000", "-f", "f32le", "-"], capture_output=True, check=True).stdout, np.float32)
model = WhisperModel("base.en", device="cpu", compute_type="int8")
text = " ".join(s.text for s in model.transcribe(pcm, beam_size=5)[0])
numbers = {"3.1": "three point one", "1.3": "one point three", "1.0": "one point oh", "90": "ninety", "100": "a hundred", "1500": "fifteen hundred", "1,500": "fifteen hundred"}
def norm(s):
    s = s.lower().replace("[rewear](/ɹiwˈɛɹ/)", "rewear").replace("reware", "rewear").replace("metaai", "meta ai").replace("meta-ai", "meta ai")
    for k, v in sorted(numbers.items(), key=lambda kv: -len(kv[0])):
        s = re.sub(rf"(?<![\d.]){re.escape(k)}(?![\d.])", v, s)
    s = s.replace("it's", "its").replace("aren't", "arent").replace("they're", "theyre")
    return re.sub(r"[^a-z ]", " ", s).split()
ref = norm(" ".join(l["say"] for l in plan["lines"]))
hyp = norm(text)
d = list(range(len(hyp) + 1))
for i in range(1, len(ref) + 1):
    prev, d[0] = d[0], i
    for j in range(1, len(hyp) + 1):
        cur = min(d[j] + 1, d[j - 1] + 1, prev + (ref[i - 1] != hyp[j - 1]))
        prev, d[j] = d[j], cur
print(f"full-transcript WER {d[-1] / len(ref):.1%} ({d[-1]} edits / {len(ref)} words)")
print("TRANSCRIPT:", text.strip())

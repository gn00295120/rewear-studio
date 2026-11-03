"""Muse Glimmer 30B via local Ollama NATIVE API (think=false, format=json schema): same garment schema as cloud Spark."""
import sys, json, pathlib, time, base64, requests
from common import OUT, save_json
SCHEMA = json.loads(pathlib.Path(__file__).with_name("garment_schema.json").read_text())
MODEL = "muse-glimmer:30b"; URL = "http://127.0.0.1:11434/api/chat"
SYSTEM = ("You catalogue children's clothing for a wardrobe/resale app. Look at the photo and return only a JSON object matching the schema. "
          "Use null when a field is not visible; set needs_review=true when confidence < 0.7 or a defect is uncertain.")

def extract(path: pathlib.Path):
    t = time.time()
    body = {"model": MODEL, "stream": False, "think": False, "format": SCHEMA,
            "options": {"temperature": 0, "num_predict": 600},
            "messages": [{"role": "system", "content": SYSTEM},
                         {"role": "user", "content": "Catalogue this garment.",
                          "images": [base64.b64encode(path.read_bytes()).decode()]}]}
    d = requests.post(URL, json=body, timeout=600).json()
    content = d["message"]["content"].strip()
    if content.startswith("```"):
        content = content.split("\n", 1)[1].rsplit("```", 1)[0]
    obj, end = json.JSONDecoder().raw_decode(content)
    if content[end:].strip():
        print(f"  [warn] {len(content)-end} extra chars after JSON: {content[end:end+80]!r}")
    obj["confidence"] = min(1.0, max(0.0, float(obj.get("confidence", 0))))
    stats = {k: d.get(k) for k in ("prompt_eval_count", "eval_count", "prompt_eval_duration", "eval_duration", "total_duration")}
    return obj, time.time() - t, stats

if __name__ == "__main__":
    results = {}
    for p in map(pathlib.Path, sys.argv[1:]):
        obj, dt, st = extract(p)
        print(f"{p.name}: {dt:.1f}s prompt={st['prompt_eval_count']} gen={st['eval_count']} → {json.dumps(obj, ensure_ascii=False)}")
        results[p.name] = {"seconds": dt, "stats": st, "record": obj}
    save_json("t5_glimmer_local.json", results)

"""Collect the real artifacts of the recorded analysis for the "how it works" scene:
SAM 3.1 masks (decoded with the app's own helpers), Muse Spark fields and the selected Muse Image previews.
Usage: artifacts.py <rewear_url> <data_dir> <out_dir>"""
import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from PIL import Image  # noqa: E402

from wardrobe.media import _group_geometry, _groups  # noqa: E402

URL, DATA, OUT = sys.argv[1].rstrip("/"), Path(sys.argv[2]), Path(sys.argv[3])
OUT.mkdir(parents=True, exist_ok=True)
items = json.load(urllib.request.urlopen(f"{URL}/api/items"))["items"]
jobs = {item["job_id"] for item in items}
assert len(jobs) == 1, f"expected one analysis, found {len(jobs)}"
job = DATA / "jobs" / jobs.pop()
segments = json.loads((job / "segments.json").read_text())
source = Image.open(job / "source.jpg")
geometry = {}
for group in _groups(segments["records"], None):
    box, mask = _group_geometry(group, source.width, source.height)
    if mask is not None:
        geometry[group["object_id"]] = mask


def local(url):
    prefix = f"/media/{job.name}/"
    assert url and url.startswith(prefix), url
    path = job / url[len(prefix):]
    assert path.is_file(), path
    return str(path)


def name(record):
    text = (record.get("subtype") or record.get("category") or "garment").strip()
    return text[:1].upper() + text[1:]


def detail(record):
    parts = [", ".join(record.get("colors") or [])]
    if record.get("pattern") and record["pattern"] not in ("solid", "none"):
        parts.append(record["pattern"])
    if record.get("material_guess"):
        parts.append(record["material_guess"])
    return " · ".join(p for p in parts if p)


def preview(item):
    rec = item.get("reconstruction") or {}
    for key in ("image_url", "preview_url", "candidate_url"):
        if rec.get(key):
            return local(rec[key])
    candidates = rec.get("preview_candidates") or []
    return local(candidates[0]["variant_url"]) if candidates else None


items.sort(key=lambda item: (item["bbox"][0] + item["bbox"][2]) / 2)
pieces = []
for index, item in enumerate(items):
    mask = geometry.get(item["object_id"])
    if mask is None:
        continue
    path = OUT / f"mask-{index}.png"
    mask.save(path)
    pieces.append({"mask": str(path), "bbox": item["bbox"], "name": name(item["record"]), "id": item["id"]})


def pick(words):
    for item in items:
        text = f"{item['record'].get('subtype')} {item['record'].get('material_guess')}".lower()
        if all(word in text for word in words):
            return item
    return None


chosen = [pick(["shirt", "linen"]), pick(["crochet"]), pick(["jeans", "high"])]
chosen = [item for item in chosen if item] or items[:3]
for item in items:
    if len(chosen) >= 3:
        break
    if item not in chosen:
        chosen.append(item)
rows = [{"crop": local(item["cutout_url"]), "name": name(item["record"]), "detail": detail(item["record"])} for item in chosen[:3]]
previews = [{"preview": preview(item), "name": name(item["record"])} for item in chosen[:3] if preview(item)]
artifacts = {"job": job.name, "source": str(job / "source.jpg"), "pieces": pieces, "spark_rows": rows, "previews": previews}
(OUT / "artifacts.json").write_text(json.dumps(artifacts, indent=2))
print(json.dumps({"pieces": [p["name"] for p in pieces], "rows": rows, "previews": [p["name"] for p in previews]}, indent=1))

"""SAM 3.1 on one image for several concepts → overlays + crops + records."""
import asyncio, sys, json, pathlib, time
from PIL import Image
from common import OUT, save_json
from sam_common import segment, data_url, overlay, crops, rec_to_dict

async def main(path, concepts):
    path = pathlib.Path(path); img = Image.open(path); img.load()
    part = {"type": "input_image", "image_url": data_url(path)}
    summary = {}
    all_records = []
    for concept in concepts:
        t = time.time()
        records, result, raw = await segment(part, concept, video=False)
        dt = time.time() - t
        boxes = [r for r in records if r.kind == "box"]; masks = [r for r in records if r.kind == "mask"]
        print(f"[{concept}] {dt:.1f}s boxes={len(boxes)} masks={len(masks)} outcome={getattr(getattr(result,'outcome',None),'kind',None) or type(getattr(result,'outcome',None)).__name__}")
        summary[concept] = {"seconds": dt, "boxes": len(boxes), "masks": len(masks),
                            "records": [rec_to_dict(r) for r in records]}
        (OUT / f"t2_{path.stem}_{concept.replace(' ','_')}_raw.txt").write_text(raw or "")
        if records:
            overlay(img, records, label_prefix=f"{concept[:3]}:").save(OUT / f"t2_{path.stem}_{concept.replace(' ','_')}.png")
            cd = OUT / f"t2_{path.stem}_crops"; cd.mkdir(exist_ok=True)
            for oid, tile in crops(img, records).items():
                tile.save(cd / f"{concept.replace(' ','_')}_{oid}.png")
        all_records += records
    if all_records:
        overlay(img, all_records).save(OUT / f"t2_{path.stem}_all.png")
    save_json(f"t2_{path.stem}_summary.json", summary)

if __name__ == "__main__":
    p = sys.argv[1]; concepts = sys.argv[2:] or ["shirt", "dress", "jacket", "pants", "garment"]
    asyncio.run(main(p, concepts))

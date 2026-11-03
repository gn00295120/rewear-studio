"""Muse Spark 1.3: image crops → strict JSON garment records (+ concept listing for a whole scene)."""
import sys, json, pathlib, time, base64
from common import client, OUT, save_json
from sam_common import data_url
SCHEMA = json.loads(pathlib.Path(__file__).with_name("garment_schema.json").read_text())
c = client()

def extract(path: pathlib.Path):
    t = time.time()
    r = c.chat.completions.create(
        model="muse-spark-1.3",
        messages=[{"role": "system", "content": "You catalogue children's clothing for a wardrobe/resale app. Look at the photo and return only the JSON record. Use null when a field is not visible; set needs_review=true when confidence < 0.7 or a defect is uncertain."},
                  {"role": "user", "content": [{"type": "text", "text": "Catalogue this garment."},
                                               {"type": "image_url", "image_url": {"url": data_url(path)}}]}],
        response_format={"type": "json_schema", "json_schema": {"name": "garment", "schema": SCHEMA, "strict": True}},
        max_tokens=700)
    obj = json.loads(r.choices[0].message.content)
    obj["confidence"] = min(1.0, max(0.0, float(obj.get("confidence", 0))))
    return obj, time.time() - t, r.usage.model_dump()

def list_concepts(path: pathlib.Path):
    schema = {"type": "object", "additionalProperties": False,
              "properties": {"concepts": {"type": "array", "items": {"type": "string"}},
                             "estimated_item_count": {"type": "integer"}},
              "required": ["concepts", "estimated_item_count"]}
    r = c.chat.completions.create(
        model="muse-spark-1.3",
        messages=[{"role": "system", "content": "You prepare prompts for a segmentation model. Return short concrete noun phrases (one garment type each, e.g. 'striped t-shirt', 'denim jacket', 'dress') that name every distinct garment type visible. No colours unless needed to disambiguate. Also estimate how many garments are visible."},
                  {"role": "user", "content": [{"type": "text", "text": "List the garment types in this photo."},
                                               {"type": "image_url", "image_url": {"url": data_url(path)}}]}],
        response_format={"type": "json_schema", "json_schema": {"name": "concepts", "schema": schema, "strict": True}},
        max_tokens=400)
    return json.loads(r.choices[0].message.content), r.usage.model_dump()

if __name__ == "__main__":
    mode = sys.argv[1]
    if mode == "concepts":
        obj, usage = list_concepts(pathlib.Path(sys.argv[2])); print(json.dumps(obj, ensure_ascii=False)); print("usage", usage)
        save_json("t3b_concepts.json", {"result": obj, "usage": usage})
    else:
        results = {}
        for p in map(pathlib.Path, sys.argv[2:]):
            obj, dt, usage = extract(p)
            print(f"{p.name}: {dt:.1f}s {usage['prompt_tokens']}/{usage['completion_tokens']} tok → {json.dumps(obj, ensure_ascii=False)}")
            results[p.name] = {"seconds": dt, "usage": usage, "record": obj}
        save_json("t3b_extract.json", results)

import sys, json, pathlib, time
from openai import OpenAI
from common import save_json
from sam_common import data_url
c = OpenAI(base_url="http://127.0.0.1:11434/v1", api_key="ollama")
schema = {"type":"object","additionalProperties":False,"properties":{"concepts":{"type":"array","items":{"type":"string"}},"estimated_item_count":{"type":"integer"}},"required":["concepts","estimated_item_count"]}
p = pathlib.Path(sys.argv[1]); t = time.time()
r = c.chat.completions.create(model="muse-glimmer:30b", temperature=0, max_tokens=400,
    extra_body={"think": False},
    messages=[{"role":"system","content":"You prepare prompts for a segmentation model. Return JSON matching this schema: "+json.dumps(schema)+". concepts = short concrete noun phrases naming each distinct garment type visible (one type each, e.g. 'striped t-shirt', 'denim jacket', 'dress'). Also estimate how many garments are visible."},
              {"role":"user","content":[{"type":"text","text":"List the garment types in this photo."},{"type":"image_url","image_url":{"url":data_url(p)}}]}],
    response_format={"type":"json_schema","json_schema":{"name":"concepts","schema":schema,"strict":True}})
txt = r.choices[0].message.content.strip()
print(f"{time.time()-t:.1f}s usage={r.usage.model_dump() if r.usage else None}")
print(txt[:800])
save_json("t5b_glimmer_concepts.json", {"raw": txt, "seconds": time.time()-t})

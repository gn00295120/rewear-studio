import json
from common import client, save_json, Timer
c = client()
schema = {
  "type":"object","additionalProperties":False,
  "properties":{
    "category":{"type":"string","enum":["top","bottom","dress","outerwear","onesie","shoes","accessory","other"]},
    "brand":{"type":["string","null"]},
    "size_label":{"type":["string","null"]},
    "colors":{"type":"array","items":{"type":"string"}},
    "condition":{"type":"string","enum":["new_with_tags","like_new","good","fair","poor"]},
    "defects":{"type":"array","items":{"type":"string"}},
    "confidence":{"type":"number"},
    "needs_review":{"type":"boolean"}
  },
  "required":["category","brand","size_label","colors","condition","defects","confidence","needs_review"]
}
with Timer() as t:
    r = c.chat.completions.create(
        model="muse-spark-1.3",
        messages=[{"role":"system","content":"You catalogue children's clothing for a wardrobe app. Return only the JSON record."},
                  {"role":"user","content":"Item description: 'Nelly Madison floral dress, tag says 3Y, small faint stain on hem, otherwise clean.'"}],
        response_format={"type":"json_schema","json_schema":{"name":"garment","schema":schema,"strict":True}},
        max_tokens=600,
    )
msg = r.choices[0].message.content
print(f"{t.dt:.1f}s usage={r.usage.prompt_tokens}/{r.usage.completion_tokens}")
print(msg)
obj = json.loads(msg); assert obj["category"] == "dress", obj; assert 0 <= obj["confidence"] <= 1, obj
save_json("t3a_spark_schema.json", {"seconds": t.dt, "usage": r.usage.model_dump(), "record": obj})
print("schema OK")

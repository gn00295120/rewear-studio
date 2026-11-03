import base64, sys, requests
from common import client, OUT, save_json, Timer
c = client()
prompt = ("Photo of a child's closet rail seen straight on: eight different kids' garments on hangers "
          "(striped t-shirt, denim jacket, pink dress, yellow raincoat, green hoodie, white shirt, red pants, "
          "blue overalls), each clearly separated, bright natural light, realistic, no people")
with Timer() as t:
    r = c.images.generate(model="muse-image-1.0", prompt=prompt, n=1)
d = r.data[0]
print(f"generated in {t.dt:.1f}s; keys: b64={bool(getattr(d,'b64_json',None))} url={bool(getattr(d,'url',None))}")
if getattr(d, "b64_json", None):
    img = base64.b64decode(d.b64_json)
else:
    img = requests.get(d.url, timeout=60).content
p = OUT / "t1_closet.png"; p.write_bytes(img)
from PIL import Image
im = Image.open(p); print("saved", p, im.size, im.mode)
save_json("t1_meta.json", {"seconds": t.dt, "size": im.size, "revised_prompt": getattr(d, "revised_prompt", None)})

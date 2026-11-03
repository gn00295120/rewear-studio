from common import client, save_json
c = client()
models = c.models.list()
ids = sorted(m.id for m in models.data)
print("models:", ids)
save_json("t0_models.json", [m.model_dump() for m in models.data])

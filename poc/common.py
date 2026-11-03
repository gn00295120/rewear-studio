import os, pathlib, json, time
from openai import OpenAI
KEY_FILE = pathlib.Path.home() / ".config/meta/model_api_key"
def api_key():
    return os.environ.get("MODEL_API_KEY") or KEY_FILE.read_text().strip()
def client():
    return OpenAI(base_url="https://api.meta.ai/v1", api_key=api_key())
OUT = pathlib.Path(__file__).resolve().parent.parent / "out"
OUT.mkdir(exist_ok=True)
def save_json(name, obj):
    (OUT / name).write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str))
class Timer:
    def __enter__(self): self.t = time.time(); return self
    def __exit__(self, *a): self.dt = time.time() - self.t

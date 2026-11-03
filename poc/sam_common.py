"""Shared SAM 3.1 helpers: stream → parsed records, overlays, crops."""
import asyncio, base64, mimetypes, pathlib, json, inspect
import numpy as np
from PIL import Image, ImageDraw
from openai import AsyncOpenAI
from meta_sam_parser import (parse_responses_stream, image_segmentation_format,
                             video_segmentation_format, decode_mask_to_raster)
from common import api_key, OUT

PALETTE = [(230,25,75),(60,180,75),(255,225,25),(0,130,200),(245,130,48),(145,30,180),
           (70,240,240),(240,50,230),(210,245,60),(250,190,212),(0,128,128),(220,190,255),
           (170,110,40),(255,250,200),(128,0,0),(170,255,195)]

def aclient():
    return AsyncOpenAI(base_url="https://api.meta.ai/v1", api_key=api_key())

def data_url(path: pathlib.Path) -> str:
    mime = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()}"

async def _final(parsed):
    fr = getattr(parsed, "final_result", None)
    if fr is None:
        return None
    if callable(fr):
        fr = fr()
    if inspect.isawaitable(fr):
        fr = await fr
    return fr

async def segment(media_part: dict, concept: str, video: bool, encoding: str = "one_bit"):
    """Run one SAM 3.1 request; return (records, result, raw_text)."""
    c = aclient()
    stream = await c.responses.create(
        model="sam-3.1", stream=True,
        input=[{"type": "message", "role": "user",
                "content": [{"type": "input_text", "text": concept}, media_part]}],
        metadata={"mask_encoding": encoding},
    )
    fmt = video_segmentation_format() if video else image_segmentation_format()
    parsed = parse_responses_stream(stream, fmt)
    last = None; seen = {}
    async for snap in parsed:
        last = snap
        for rec in getattr(snap, "records", []) or []:
            seen[id(rec)] = rec
    result = await _final(parsed)
    records = list(getattr(result, "records", None) or getattr(last, "records", []) or [])
    if len(seen) > len(records):
        print(f"[diag] union across snapshots has {len(seen)} records vs final {len(records)}; using union")
        records = list(seen.values())
    raw = getattr(result, "raw_output", None) or getattr(last, "raw_output", "")
    return records, result, raw

def rec_to_dict(r):
    d = {"kind": r.kind, "object_id": getattr(r, "object_id", None), "frame": getattr(r, "frame", None),
         "confidence": getattr(r, "confidence", None)}
    if r.kind == "box":
        d.update(left=r.left, top=r.top, right=r.right, bottom=r.bottom)
    elif r.kind == "mask":
        b = r.bounds
        d.update(bounds={k: getattr(b, k) for k in dir(b) if not k.startswith("_") and isinstance(getattr(b, k), int)},
                 mask_w=r.mask.width, mask_h=r.mask.height, encoding=r.mask.encoding)
    return d

def mask_array(r):
    """Full-frame boolean array for a mask record (placed using its bounds)."""
    raw = np.asarray(bytearray(decode_mask_to_raster(r.mask)), dtype=np.uint8)
    w, h = r.mask.width, r.mask.height
    if raw.size == w * h:                      # one byte per pixel (documented: 0/1, row-major)
        arr = raw.reshape(h, w) != 0
    elif raw.size == (w * h + 7) // 8:          # packed bits fallback
        arr = np.unpackbits(raw)[: w * h].reshape(h, w).astype(bool)
    elif raw.size == h * ((w + 7) // 8):        # row-padded packed bits fallback
        arr = np.unpackbits(raw.reshape(h, (w + 7) // 8), axis=1)[:, :w].astype(bool)
    else:
        raise ValueError(f"unexpected raster size {raw.size} for {w}x{h}")
    return arr

def place(arr, bounds, frame_w, frame_h):
    full = np.zeros((frame_h, frame_w), dtype=bool)
    left = getattr(bounds, "left", 0); top = getattr(bounds, "top", 0)
    h, w = arr.shape
    full[top:top+h, left:left+w] = arr[: max(0, min(h, frame_h-top)), : max(0, min(w, frame_w-left))]
    return full

def overlay(img: Image.Image, records, frame=None, label_prefix=""):
    """Draw masks + boxes for one frame onto a copy of img."""
    img = img.convert("RGBA"); W, H = img.size
    layer = np.zeros((H, W, 4), dtype=np.uint8)
    boxes = []
    for r in records:
        if frame is not None and getattr(r, "frame", 0) not in (None, frame):
            continue
        col = PALETTE[int(getattr(r, "object_id", 0) or 0) % len(PALETTE)]
        if r.kind == "mask":
            m = place(mask_array(r), r.bounds, W, H)
            layer[m] = (*col, 110)
        elif r.kind == "box":
            boxes.append((r, col))
    out = Image.alpha_composite(img, Image.fromarray(layer, "RGBA"))
    d = ImageDraw.Draw(out)
    for r, col in boxes:
        d.rectangle([r.left, r.top, r.right, r.bottom], outline=col + (255,), width=3)
        d.text((r.left + 4, r.top + 4), f"{label_prefix}{r.object_id}", fill=(255, 255, 255, 255))
    return out.convert("RGB")

def crops(img: Image.Image, records, pad=8):
    """Crop each masked object on white background; returns {object_id: PIL image}."""
    W, H = img.size; rgb = np.array(img.convert("RGB")); outd = {}
    masks = {r.object_id: r for r in records if r.kind == "mask"}
    for r in records:
        if r.kind != "box": continue
        l, t, rr, b = max(0, r.left-pad), max(0, r.top-pad), min(W, r.right+pad), min(H, r.bottom+pad)
        tile = rgb[t:b, l:rr].copy()
        m = masks.get(r.object_id)
        if m is not None:
            full = place(mask_array(m), m.bounds, W, H)[t:b, l:rr]
            tile[~full] = 255
        outd[r.object_id] = Image.fromarray(tile)
    return outd

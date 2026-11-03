"""Bounded, local similarity suggestions. Scores are heuristics, never identity probabilities.

Only the newest 500 eligible records are compared, within category; timestamps
use the store's ISO format, with stable IDs breaking ties. No merge is performed.
Signatures are small derived image features, and never contain paths or pixels.
"""

from __future__ import annotations

import colorsys
import hashlib
import heapq
import json
import math
import unicodedata
import warnings
from collections import defaultdict, deque
from itertools import combinations
from pathlib import Path

from PIL import Image, ImageOps, ImageStat


MAX_ITEMS = 500
MAX_SUGGESTIONS = 100
MAX_PER_ITEM = 3
MAX_IMAGE_PIXELS = 24_000_000
MAX_IMAGE_BYTES = 32 * 1024 * 1024
THUMBNAIL_SIZE = 96
COLOR_BINS = 15
CATEGORIES = {"top", "bottom", "dress", "outerwear", "onesie", "sleepwear", "shoes", "accessory"}
# Chinese aliases are data, not UI copy: saved records and model output may use either language.
UNKNOWN = {"", "unknown", "none", "null", "na", "不明", "未知", "無", "無法判斷"}
COLOR_ALIASES = {
    "blue": ("藍", "藍色", "深藍", "淺藍", "navy", "navy blue", "dark blue", "light blue", "denim blue"),
    "red": ("紅", "紅色", "酒紅", "burgundy", "dark red"),
    "green": ("綠", "綠色", "深綠", "淺綠", "olive", "dark green", "light green"),
    "yellow": ("黃", "黃色"),
    "orange": ("橘", "橘色", "橙色"),
    "purple": ("紫", "紫色", "violet", "lavender"),
    "pink": ("粉", "粉色", "粉紅", "粉紅色"),
    "brown": ("棕", "棕色", "咖啡", "咖啡色"),
    "beige": ("米", "米色", "卡其", "卡其色", "khaki", "tan"),
    "white": ("白", "白色", "米白", "米白色", "cream", "off white", "off-white", "ivory"),
    "black": ("黑", "黑色"),
    "gray": ("灰", "灰色", "grey", "charcoal"),
    "multicolor": ("多色", "彩色", "multicolored", "multicolour"),
}
PATTERN_ALIASES = {
    "solid": ("plain", "素色", "純色", "無圖案"),
    "striped": ("stripe", "stripes", "條紋", "條纹"),
    "plaid": ("check", "checked", "checkered", "格紋", "格子"),
    "floral": ("flowers", "花朵", "花卉", "碎花"),
    "polkadot": ("polka dots", "polka dot", "dots", "圓點", "點點"),
    "graphic": ("print", "printed", "印花", "圖案"),
}
SUBTYPE_ALIASES = {
    "tshirt": ("t shirt", "tee", "tee shirt", "T恤", "短袖T恤"),
    "shirt": ("襯衫", "button shirt", "button down"),
    "jeans": ("牛仔褲", "denim pants"),
    "trousers": ("pants", "長褲"),
    "shorts": ("短褲",),
    "sweater": ("毛衣", "針織衫"),
    "jacket": ("夾克",),
    "dress": ("洋裝", "連身裙"),
    "skirt": ("裙子", "短裙"),
}


def _word(value) -> str:
    if not isinstance(value, str) or len(value) > 200:
        return ""
    normalized = unicodedata.normalize("NFKC", value).casefold()
    word = "".join(char for char in normalized if char.isalnum())
    return "" if word in UNKNOWN else word


def _aliases(values):
    return {_word(alias): key for key, aliases in values.items() for alias in (key, *aliases)}


COLORS, PATTERNS, SUBTYPES = map(_aliases, (COLOR_ALIASES, PATTERN_ALIASES, SUBTYPE_ALIASES))


def _pixels(image):
    # Pillow 11 compatibility; newer Pillow deprecates getdata().
    flattened = getattr(image, "get_flattened_data", None)
    return flattened() if flattened is not None else image.getdata()


def _foreground(image: Image.Image) -> Image.Image:
    """Remove transparent pixels and border-connected light neutral backgrounds."""
    width, height = image.size
    pixels = list(_pixels(image))
    border = set(range(width)) | set(range((height - 1) * width, width * height))
    border.update(y * width + x for y in range(height) for x in (0, width - 1))
    opaque_border = [pixels[i][:3] for i in border if pixels[i][3] >= 32]
    light = [rgb for rgb in opaque_border if min(rgb) >= 180 and max(rgb) - min(rgb) <= 30]
    background = None
    if light and len(light) >= max(1, len(opaque_border) * 0.5):
        background = tuple(sorted(rgb[channel] for rgb in light)[len(light) // 2] for channel in range(3))

    def removable(index):
        r, g, b, alpha = pixels[index]
        return alpha < 32 or (
            background is not None
            and max(abs(value - base) for value, base in zip((r, g, b), background)) <= 28
            and max(r, g, b) - min(r, g, b) <= 40
        )

    removed = {i for i in border if removable(i)}
    pending = deque(sorted(removed))
    while pending:
        index = pending.popleft()
        x, y = index % width, index // width
        for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
            neighbor = ny * width + nx
            if 0 <= nx < width and 0 <= ny < height and neighbor not in removed and removable(neighbor):
                removed.add(neighbor)
                pending.append(neighbor)
    mask = Image.new("L", image.size)
    mask.putdata([255 if i not in removed and pixel[3] >= 32 else 0 for i, pixel in enumerate(pixels)])
    return mask


def visual_signature(path: Path) -> dict:
    """Return a JSON-safe signature, or {} for unreadable, oversized or blank media.

    Decoding is limited to 32 MiB / 24 megapixels and feature work to 96 x 96.
    EXIF orientation is honored. White padding and transparent pixels do not
    contribute to the foreground histogram or determine the normalized crop.
    """
    try:
        path = Path(path)
        if not path.is_file() or not 0 < path.stat().st_size <= MAX_IMAGE_BYTES:
            return {}
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as source:
                if source.format not in {"JPEG", "PNG", "WEBP", "BMP", "GIF", "TIFF"}:
                    return {}
                if min(source.size) < 8 or source.width * source.height > MAX_IMAGE_PIXELS:
                    return {}
                source.load()
                image = ImageOps.exif_transpose(source).convert("RGBA")
                image.thumbnail((THUMBNAIL_SIZE, THUMBNAIL_SIZE), Image.Resampling.LANCZOS)
        mask = _foreground(image)
        bbox = mask.getbbox()
        if bbox is None or sum(value > 0 for value in _pixels(mask)) < 32:
            return {}
        image, mask = image.crop(bbox).convert("RGB"), mask.crop(bbox)
        colors = [0.0] * COLOR_BINS
        count = 0
        for rgb, selected in zip(_pixels(image), _pixels(mask)):
            if not selected:
                continue
            hue, saturation, value = colorsys.rgb_to_hsv(*(channel / 255 for channel in rgb))
            if saturation < 0.15 or value < 0.12:
                colors[12 if value < 0.25 else 13 if value < 0.8 else 14] += 1
            else:
                position = hue * 12
                base, fraction = int(position) % 12, position % 1
                colors[base] += 1 - fraction
                colors[(base + 1) % 12] += fraction
            count += 1
        composite = Image.new("RGB", image.size, "white")
        composite.paste(image, mask=mask)
        canonical = Image.new("L", (64, 64), 255)
        scaled = ImageOps.contain(composite.convert("L"), (60, 60), Image.Resampling.LANCZOS)
        offset = ((64 - scaled.width) // 2, (64 - scaled.height) // 2)
        canonical.paste(scaled, offset)
        gray = list(_pixels(ImageOps.autocontrast(canonical).resize((9, 8), Image.Resampling.LANCZOS)))
        dhash = sum(
            (gray[y * 9 + x] > gray[y * 9 + x + 1]) << (y * 8 + x) for y in range(8) for x in range(8)
        )
        outline = Image.new("L", (64, 64), 0)
        outline.paste(mask.resize(scaled.size, Image.Resampling.NEAREST), offset)
        shape = sum(
            (value >= 128) << i
            for i, value in enumerate(_pixels(outline.resize((8, 8), Image.Resampling.BOX)))
        )
        fill = count / (image.width * image.height)
        structure = fill < 0.94 or ImageStat.Stat(image.convert("L"), mask).stddev[0] >= 8
        return {
            "version": 1,
            "dhash": f"{dhash:016x}",
            "shape": f"{shape:016x}",
            "colors": [round(value / count, 6) for value in colors],
            "aspect": round(image.width / image.height, 6),
            "structure": structure,
        }
    except (
        OSError,
        ValueError,
        TypeError,
        SyntaxError,
        OverflowError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ):
        return {}


def _signature(value):
    """Treat old, corrupted or unsupported feature formats as absent evidence."""
    if not isinstance(value, dict) or type(value.get("version")) is not int or value["version"] != 1:
        return None
    if type(value.get("structure")) is not bool:
        return None
    for key in ("dhash", "shape"):
        word = value.get(key)
        if not isinstance(word, str) or len(word) != 16 or any(c not in "0123456789abcdef" for c in word):
            return None
    aspect, colors = value.get("aspect"), value.get("colors")
    if type(aspect) not in (int, float) or not math.isfinite(aspect) or not 0.01 <= aspect <= 100:
        return None
    if not isinstance(colors, list) or len(colors) != COLOR_BINS:
        return None
    if any(type(n) not in (int, float) or not math.isfinite(n) or not 0 <= n <= 1 for n in colors):
        return None
    if not 0.99 <= sum(colors) <= 1.01:
        return None
    return {**value, "dhash": int(value["dhash"], 16), "shape": int(value["shape"], 16)}


def _item(item):
    if not isinstance(item, dict) or not isinstance(item.get("record"), dict):
        return None
    if any(not isinstance(item.get(key), str) or not 0 < len(item[key]) <= 200 for key in ("id", "job_id")):
        return None
    quality = item.get("quality")
    if quality is not None and not isinstance(quality, dict):
        return None
    status = "ready" if item.get("reviewed") is True else (quality or {}).get("status", "ready")
    record = item["record"]
    category = record.get("category")
    if (
        status not in ("ready", "needs_confirmation")
        or not isinstance(category, str)
        or category not in CATEGORIES
    ):
        return None
    colors = record.get("colors")
    colors = {_word(color) for color in colors[:20]} if isinstance(colors, list) else set()
    colors = {COLORS.get(color, COLORS.get(color.removesuffix("色"), color)) for color in colors if color} - {
        "multicolor"
    }
    subtype = _word(record.get("subtype"))
    if subtype in CATEGORIES or subtype in {"garment", "clothing", "衣服", "衣物", "上衣", "褲子"}:
        subtype = ""
    return {
        "id": item["id"],
        "job_id": item["job_id"],
        "category": record["category"],
        "created_at": item.get("created_at") if isinstance(item.get("created_at"), str) else "",
        "brand": _word(record.get("brand")),
        "colors": colors,
        "subtype": SUBTYPES.get(subtype, subtype),
        "pattern": PATTERNS.get(_word(record.get("pattern")), _word(record.get("pattern"))),
        "visual": _signature(item.get("visual_signature")),
    }


def _similarity(first, second):
    if first["job_id"] == second["job_id"]:
        return None
    for key in ("brand", "pattern", "subtype"):
        if first[key] and second[key] and first[key] != second[key]:
            return None
    color_match = bool(first["colors"] & second["colors"])
    if first["colors"] and second["colors"] and not color_match:
        return None
    shared = {key for key in ("brand", "pattern", "subtype") if first[key] and first[key] == second[key]}
    left, right = first["visual"], second["visual"]
    if left and right:
        histogram = sum(min(a, b) for a, b in zip(left["colors"], right["colors"]))
        hash_match = 1 - (left["dhash"] ^ right["dhash"]).bit_count() / 64
        shape_union = (left["shape"] | right["shape"]).bit_count()
        shape_match = (left["shape"] & right["shape"]).bit_count() / max(1, shape_union)
        aspect_match = min(left["aspect"], right["aspect"]) / max(left["aspect"], right["aspect"])
        if not (left["structure"] and right["structure"]):
            return None
        # Fewer than four occupied cells in an 8 x 8 silhouette cannot describe
        # an outline. White fabric against white background can lose its mask
        # while retaining useful local gradients. Missing shape is not a shape
        # mismatch, but must not count as positive evidence either.
        sparse_shape = min(left["shape"].bit_count(), right["shape"].bit_count()) < 4
        if sparse_shape:
            informative_hashes = all(8 <= visual["dhash"].bit_count() <= 56 for visual in (left, right))
            if not (
                informative_hashes
                and hash_match >= 0.9
                and histogram >= 0.9
                and aspect_match >= 0.9
                and color_match
                and "subtype" in shared
            ):
                return None
            shape_match = 0  # Do not award agreement for a missing descriptor.
            reasons = [
                "Contour features too sparse to compare silhouettes.",
                "Local brightness, color distribution and proportions are highly similar, with matching visible style and color.",
            ]
        else:
            if not (histogram >= 0.65 and hash_match >= 0.67 and shape_match >= 0.6 and aspect_match >= 0.55):
                return None
            reasons = [
                "Garment silhouette and local brightness features are similar.",
                "Image color distribution is similar after excluding light backgrounds.",
            ]
        score = (
            0.15
            + 0.2 * histogram
            + 0.2 * hash_match
            + 0.15 * shape_match
            + 0.1 * color_match
            + 0.05 * len(shared)
        )
    else:
        # Generic blue shirts alone are not evidence of identity. Old records
        # need a visible brand AND specific subtype AND compatible colors.
        if not color_match or not {"brand", "subtype"} <= shared:
            return None
        score = 0.4 + 0.03 * ("pattern" in shared)
        reasons = [
            "No comparable image features; low-confidence candidate based on brand, style and color only."
        ]
    if "brand" in shared:
        reasons.append("Visible brand markings match.")
    if "pattern" in shared:
        reasons.append("Pattern markings match.")
    reasons.append(
        "Similarity candidate only; the score is not a probability of being the same item — please verify manually before merging."
    )
    return round(min(score, 0.95), 4), reasons


def suggest_duplicates(items: list[dict], groups: list[dict] | None = None, limit: int = 40) -> list[dict]:
    """Suggest cross-photo pairs, never mutate input or merge records.

    Cost is bounded to 500 records and category-local comparisons. Return at
    most 100 pairs and three per item, with stable pair IDs and ordering.
    Invalid records are skipped; manual review overrides quality eligibility.
    """
    if not isinstance(items, list) or type(limit) is not int or limit <= 0:
        return []
    limit = min(limit, MAX_SUGGESTIONS)
    recent = heapq.nlargest(
        MAX_ITEMS, filter(None, map(_item, items)), key=lambda row: (row["created_at"], row["id"])
    )
    selected = {row["id"]: row for row in recent}
    memberships = defaultdict(set)
    for index, group in enumerate(groups if isinstance(groups, list) else []):
        if not isinstance(group, dict):
            continue
        ids = group.get("item_ids", [])
        ids = list(ids) if isinstance(ids, list) else []
        ids.append(group.get("primary_item_id"))
        for item_id in ids:
            if isinstance(item_id, str) and item_id in selected:
                memberships[item_id].add(index)
    buckets = defaultdict(list)
    for row in selected.values():
        buckets[row["category"]].append(row)
    candidates = []
    for category in sorted(buckets):
        for first, second in combinations(buckets[category], 2):
            if memberships[first["id"]] & memberships[second["id"]]:
                continue
            match = _similarity(first, second)
            if match is None:
                continue
            score, reasons = match
            ids = sorted((first["id"], second["id"]))
            pair_id = (
                "similar-" + hashlib.sha256(json.dumps(ids, separators=(",", ":")).encode()).hexdigest()[:24]
            )
            row = {"id": pair_id, "item_ids": ids, "score": score, "reasons": reasons, "human_required": True}
            heapq.heappush(candidates, (score, pair_id, row))
            if len(candidates) > limit * 8:
                heapq.heappop(candidates)
    result, used = [], defaultdict(int)
    for _, _, row in sorted(candidates, reverse=True):
        if any(used[item_id] >= MAX_PER_ITEM for item_id in row["item_ids"]):
            continue
        result.append(row)
        for item_id in row["item_ids"]:
            used[item_id] += 1
        if len(result) == limit:
            break
    return result

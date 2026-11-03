"""Locate visible worn garments before asking SAM to isolate each one.

The inventory is a search plan, never an item or a substitute segmentation.
Only returned SAM geometry reaches the normal crop/inspection pipeline. All
coordinates are mapped back to the supplied, EXIF-normalized (possibly manually
selected) source so the original photograph remains the evidence.
"""

from __future__ import annotations

import copy
import json
import asyncio
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

from PIL import Image, ImageChops
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from . import media
from .providers import ProviderError

MAX_TARGETS = 8
MAX_GENERIC_ADDITIONS = 4
MIN_FIRST_PASS_TARGET_COVERAGE = 0.5
INVENTORY_PROMPT = """Plan visual segmentation of worn clothing in this photograph.
Return only separate garments that have identifiable visible pixels in THIS image.
Never return a person, whole outfit, shoes, hat, bag, umbrella, background, or an
imagined hidden garment. Treat a coat, visible inner shirt, and trousers as separate
garments, even when their colors are the same. Do not treat the top and trousers
as one garment. One entry per actual garment per person; the two legs of one pair
of trousers are one entry. Include recognizable partly occluded clothes, but do
not invent the hidden extent: bbox encloses ALL VISIBLE pixels of that garment,
including sleeves and visible hem, in normalized [left,top,right,bottom] 0..1
coordinates of the exact supplied image. If a user selected a person, this image
is already their selection: do not infer clothes outside it. Use a short English
garment kind and only a clearly visible color (empty string if unsure). For many
people prioritize the largest visible garments; at most 8 targets. Small collars
or fragments with no identifiable garment should be omitted. An isolated hood,
collar, cuff or pocket without a visible connected garment body is not a separate
garment target: do not invent the hidden hoodie, shirt or jacket. Set uncertain=true
if darkness/occlusion prevents identifying or locating an otherwise visible
garment. No brand, identity, hidden shape, or guessed garment attributes.
"""


class Target(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    category: Literal["top", "bottom", "outerwear", "dress", "onesie"]
    kind: Literal[
        "shirt",
        "t-shirt",
        "sweater",
        "sweatshirt",
        "hoodie",
        "blouse",
        "vest",
        "jacket",
        "coat",
        "cardigan",
        "pants",
        "jeans",
        "shorts",
        "skirt",
        "dress",
        "jumpsuit",
    ]
    color: Annotated[str, Field(max_length=40)]
    bbox: Annotated[list[Annotated[float, Field(ge=0, le=1)]], Field(min_length=4, max_length=4)]

    @model_validator(mode="after")
    def valid_extent(self):
        left, top, right, bottom = self.bbox
        if left >= right or top >= bottom:
            raise ValueError("Empty garment location")
        if self.color and not re.fullmatch(r"[a-zA-Z -]+", self.color):
            raise ValueError("Color must be a short English visual description")
        return self

    @property
    def concept(self):
        return " ".join(part for part in (self.color.strip(), self.kind) if part)


class Inventory(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    targets: list[Target] = Field(max_length=MAX_TARGETS)
    uncertain: bool


@dataclass
class OutfitSegments:
    records: list[dict]
    warnings: list[str]
    partial: bool
    plan: dict


def _intersection(first, second):
    width = max(0, min(first[2], second[2]) - max(first[0], second[0]))
    height = max(0, min(first[3], second[3]) - max(first[1], second[1]))
    return width * height


def _area(box):
    return max(0, box[2] - box[0]) * max(0, box[3] - box[1])


def _iou(first, second):
    intersection = _intersection(first, second)
    return intersection / max(1, _area(first) + _area(second) - intersection)


def _target_input(source: Path, target: Target, destination: Path, *, padding=0.12, min_long_side=None):
    with Image.open(source) as image:
        width, height = image.size
    left, top, right, bottom = target.bbox
    # Padding leaves room for imperfect localization and sleeves/hem. SAM still
    # sees the real surrounding pixels and must provide its own geometry.
    px, py = (right - left) * padding, (bottom - top) * padding
    region = [max(0, left - px), max(0, top - py), min(1, right + px), min(1, bottom + py)]
    native = destination.with_name(f"{destination.stem}-native.jpg") if min_long_side else destination
    selection = media.crop_selection(source, region, native)
    input_size = tuple(selection["bbox"][i + 2] - selection["bbox"][i] for i in (0, 1))
    if min_long_side:
        # Resampling adds no new evidence. Retain native pixels and the exact
        # dimensions needed to map the model's real mask back to the source.
        with Image.open(native) as image:
            scale = max(1, min_long_side / max(image.size))
            input_size = (round(image.width * scale), round(image.height * scale))
            image.resize(input_size, Image.Resampling.LANCZOS).save(destination, format="PNG")
    intended = (left * width, top * height, right * width, bottom * height)
    return selection["bbox"], intended, (width, height), input_size


def _mapped_candidate(records, region, intended, size, number, *, require_mask=False, input_size=None):
    """Select real SAM evidence overlapping the planned garment, then translate it."""
    rw, rh = region[2] - region[0], region[3] - region[1]
    input_size = tuple(input_size or (rw, rh))
    resampled = input_size != (rw, rh)
    choices = []
    for group in media._groups(records, None):
        box, mask = media._group_geometry(group, *input_size)
        if box is None or (require_mask and mask is None):
            continue
        if resampled:
            if mask is None:
                continue
            # Use only pixels selected by SAM inside its returned geometry.
            # Nearest-neighbour inverse mapping preserves the binary mask;
            # the planned rectangle never becomes segmentation geometry.
            selected = Image.new("L", input_size, 0)
            selected.paste(mask.crop(box), box[:2])
            mask = selected.resize((rw, rh), Image.Resampling.NEAREST)
            box = mask.getbbox()
            if box is None:
                continue
        mapped = (box[0] + region[0], box[1] + region[1], box[2] + region[0], box[3] + region[1])
        overlap = _intersection(mapped, intended) / max(1, _area(mapped))
        if overlap < 0.6 or _intersection(mapped, intended) / max(1, _area(intended)) < 0.08:
            continue
        choices.append((_iou(mapped, intended), group, box, mask, mapped))
    if not choices:
        return None
    iou, group, box, mask, mapped = max(choices, key=lambda entry: entry[0])
    intersection = _intersection(mapped, intended)
    oid = f"outfit-{number}-{group['object_id']}"
    translated = [
        {
            "kind": "box",
            "object_id": oid,
            "frame": None,
            **dict(zip(("left", "top", "right", "bottom"), mapped)),
        }
    ]
    full_mask = None
    if mask is not None:
        full_mask = Image.new("L", size, 0)
        full_mask.paste(mask.crop(box), (mapped[0], mapped[1]))
        raw = copy.deepcopy(group["mask"])
        bounds = raw["bounds"]
        if (
            not resampled
            and 0 <= bounds["left"] < bounds["right"] <= rw
            and 0 <= bounds["top"] < bounds["bottom"] <= rh
        ):
            for key in ("left", "right"):
                bounds[key] += region[0]
            for key in ("top", "bottom"):
                bounds[key] += region[1]
        else:
            # Re-encode a clipped or inverse-resampled real mask in original
            # source coordinates with the same codec as the parser.
            from meta_sam_parser._mask_codec import _encode_raster

            patch = mask.crop(box)
            raw["bounds"] = dict(zip(("left", "top", "right", "bottom"), mapped))
            raw["mask"] = {
                "width": patch.width,
                "height": patch.height,
                "encoding": "one_bit",
                "payload": _encode_raster(
                    patch.point(lambda p: 1 if p else 0).tobytes(), patch.width, patch.height
                ),
            }
        raw.update(object_id=oid, frame=None)
        translated.append(raw)
    return {
        "records": translated,
        "box": mapped,
        "mask": full_mask,
        "iou": iou,
        "intended_coverage": intersection / max(1, _area(intended)),
        "candidate_overlap": intersection / max(1, _area(mapped)),
    }


def _duplicate(candidate, previous):
    if _iou(candidate["box"], previous["box"]) < 0.7:
        return False
    first, second = candidate["mask"], previous["mask"]
    if first is not None and second is not None:
        intersection = ImageChops.multiply(first, second).histogram()[255]
        union = first.histogram()[255] + second.histogram()[255] - intersection
        return intersection / max(1, union) >= 0.85
    # Boxes alone cannot establish that two overlapping layers are the same.
    return False


def _full_image_candidate(group, size, number):
    """Translate one real full-image SAM group without inventing geometry."""
    box, mask = media._group_geometry(group, *size)
    if box is None or mask is None:
        return None
    records = [copy.deepcopy(group[kind]) for kind in ("box", "mask") if kind in group]
    return _mapped_candidate(
        records,
        (0, 0, *size),
        box,
        size,
        number,
        require_mask=True,
        input_size=size,
    )


async def segment_outfit(providers, source, directory, *, provider, max_targets, media_runner):
    """Run bounded localization/SAM calls; preserve partial evidence and failures."""
    source, directory = Path(source), Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    report = {"version": 1, "source": source.name, "targets": [], "warnings": []}

    def save():
        (directory / "outfit-plan.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))

    try:
        content = await providers._vision_request(
            [source],
            provider,
            schema=Inventory.model_json_schema(),
            schema_name="outfit_inventory",
            system_prompt=INVENTORY_PROMPT,
            user_prompt="Locate separate visible garments in this exact photograph. Return the JSON inventory only.",
        )
        if not isinstance(content, str) or len(content) > 50_000:
            raise ValueError("Invalid inventory response")
        inventory = Inventory.model_validate(json.loads(content))
    except asyncio.CancelledError:
        report["status"] = "interrupted"
        save()
        raise
    except (ProviderError, ValidationError, ValueError, TypeError) as exc:
        report.update(
            status="unavailable",
            error=str(exc)
            if isinstance(exc, ProviderError)
            else "Garment localization returned an invalid format.",
        )
        save()
        raise ProviderError(
            "Couldn't locate each garment separately, so the original segmentation prompt was used; check the results or select the person and retry."
        ) from None
    targets = inventory.targets[: min(max_targets, MAX_TARGETS)]
    warnings = report["warnings"]
    if inventory.uncertain:
        warnings.append(
            "Some garments are occluded or poorly lit and couldn't be located reliably; garments that weren't clearly visible were not invented."
        )
    if len(targets) < len(inventory.targets):
        warnings.append(
            "This photo has more garments than the per-photo limit; analyze the rest in a separate upload."
        )
    report.update(status="segmenting", uncertain=inventory.uncertain)
    save()
    selected, combined, pending = [], [], []
    for index, target in enumerate(targets, 1):
        entry = {**target.model_dump(), "concept": target.concept, "status": "preparing"}
        report["targets"].append(entry)
        image = directory / f"target-{index}.jpg"
        region, intended, size, input_size = await media_runner(_target_input, source, target, image)
        entry.update(input=image.name, region=region, status="segmenting")
        save()
        # A tight crop can remove the shoulder/neck/waist context SAM needs.
        # Retry once with more original context and a readable local scale.
        # Partly hidden tops can be named shirt/sweater inconsistently; use
        # their visible colour and category on retry, without guessing sleeves.
        # Other garment categories keep their specific concept.
        entry["attempts"] = []
        candidate = None
        selected_records = []
        selected_concept = None
        selected_region = None
        selected_score = (-1.0, -1.0)
        retried_low_coverage = False
        for attempt_number in (1, 2):
            if attempt_number == 2:
                image = directory / f"target-{index}-context.png"
                region, intended, size, input_size = await media_runner(
                    _target_input, source, target, image, padding=0.45, min_long_side=768
                )
            concept = target.concept
            if attempt_number == 2 and target.category == "top":
                concept = " ".join(part for part in (target.color.strip(), "top") if part)
            attempt = {
                "concept": concept,
                "input": image.name,
                "region": region,
                "input_size": list(input_size),
                "status": "segmenting",
            }
            entry["attempts"].append(attempt)
            save()
            try:
                records = await providers.segment(image, concept, video=False)
            except asyncio.CancelledError:
                attempt["status"] = entry["status"] = report["status"] = "interrupted"
                save()
                raise
            except ProviderError as exc:
                records = getattr(exc, "records", None) or []
                attempt["error"] = entry["error"] = str(exc)
                warnings.append(f"Garment {index} segmentation didn't fully complete: {exc}")
            attempt["sam_records"] = entry["sam_records"] = records
            current_candidate = await media_runner(
                _mapped_candidate,
                records,
                region,
                intended,
                size,
                index,
                require_mask=attempt_number > 1,
                input_size=input_size,
            )
            attempt["status"] = "matched" if current_candidate is not None else "unmatched"
            if current_candidate is not None:
                attempt["intended_coverage"] = round(current_candidate["intended_coverage"], 6)
                attempt["candidate_overlap"] = round(current_candidate["candidate_overlap"], 6)
                score = (current_candidate["intended_coverage"], current_candidate["iou"])
                if score > selected_score:
                    candidate = current_candidate
                    selected_records = records
                    selected_concept = concept
                    selected_region = region
                    selected_score = score
            save()
            if current_candidate is not None:
                if (
                    attempt_number == 1
                    and current_candidate["intended_coverage"] < MIN_FIRST_PASS_TARGET_COVERAGE
                ):
                    # A small overlapping patch can be genuine SAM evidence while
                    # still missing most of the localized garment. Keep it as an
                    # honest fallback and retry once with more original context.
                    attempt["status"] = "matched_low_coverage_retrying"
                    retried_low_coverage = True
                    save()
                    continue
                break
        if candidate is not None:
            entry["sam_records"] = selected_records
            entry["matched_concept"] = selected_concept
            entry["matched_region"] = selected_region
            entry["intended_coverage"] = round(candidate["intended_coverage"], 6)
            if retried_low_coverage:
                improved = (
                    entry["attempts"][-1].get("status") == "matched"
                    and selected_region == region
                    and candidate["intended_coverage"] >= MIN_FIRST_PASS_TARGET_COVERAGE
                )
                entry["context_retry_improved"] = improved
                if not improved:
                    warnings.append(
                        f"Retrying garment {index} with more context didn't improve its low coverage; the original visible area was kept for review."
                    )
        if candidate is None:
            entry["status"] = "pending_fallback"
            pending.append((index, target, entry))
        else:
            duplicate = False
            for prior in selected:
                if await media_runner(_duplicate, candidate, prior):
                    duplicate = True
                    break
            if duplicate:
                entry["status"] = "duplicate"
            else:
                entry.update(
                    status="matched",
                    matched_bbox=list(candidate["box"]),
                    object_id=candidate["records"][0]["object_id"],
                )
                selected.append(candidate)
                combined.extend(candidate["records"])
        save()

    fallback_needed = bool(pending) or inventory.uncertain or len(targets) == MAX_TARGETS
    if fallback_needed:
        fallback = {
            "concept": "clothing",
            "status": "segmenting",
            "triggers": {
                "unmatched_targets": len(pending),
                "inventory_uncertain": inventory.uncertain,
                "inventory_limit_reached": len(targets) == MAX_TARGETS,
            },
            "sam_records": [],
            "recovered_targets": [],
            "added_candidates": [],
        }
        report["generic_fallback"] = fallback
        save()
        try:
            generic_records = await providers.segment(source, "clothing", video=False)
        except asyncio.CancelledError:
            fallback["status"] = report["status"] = "interrupted"
            save()
            raise
        except ProviderError as exc:
            generic_records = getattr(exc, "records", None) or []
            fallback["error"] = str(exc)
        fallback["sam_records"] = generic_records
        with Image.open(source) as image:
            source_size = image.size
        full_region = (0, 0, *source_size)
        unresolved = []
        for index, target, entry in pending:
            intended = tuple(target.bbox[position] * source_size[position % 2] for position in range(4))
            candidate = await media_runner(
                _mapped_candidate,
                generic_records,
                full_region,
                intended,
                source_size,
                index,
                require_mask=True,
                input_size=source_size,
            )
            if candidate is None:
                unresolved.append((index, target, entry))
                continue
            duplicate = False
            for prior in selected:
                if await media_runner(_duplicate, candidate, prior):
                    duplicate = True
                    break
            if duplicate:
                entry["status"] = "duplicate"
                entry["generic_fallback"] = True
                continue
            entry.update(
                status="matched",
                matched_concept="clothing",
                matched_region=list(full_region),
                matched_bbox=list(candidate["box"]),
                object_id=candidate["records"][0]["object_id"],
                intended_coverage=round(candidate["intended_coverage"], 6),
                generic_fallback=True,
            )
            selected.append(candidate)
            combined.extend(candidate["records"])
            fallback["recovered_targets"].append(index)

        # A full inventory can omit a central garment even when all eight
        # planned targets segmented successfully. Preserve a few novel real
        # masks from the same one-call sweep, then let normal inspection reject
        # whole outfits, bags, fragments, or other non-garments.
        novel = []
        for offset, group in enumerate(media._groups(generic_records, None), 1):
            candidate = await media_runner(_full_image_candidate, group, source_size, MAX_TARGETS + offset)
            if candidate is None:
                continue
            duplicate = False
            for prior in [*selected, *novel]:
                if await media_runner(_duplicate, candidate, prior):
                    duplicate = True
                    break
            if duplicate:
                continue
            novel.append(candidate)
        novel.sort(key=lambda candidate: _area(candidate["box"]), reverse=True)
        remaining = max(0, min(MAX_GENERIC_ADDITIONS, max_targets - len(selected)))
        for candidate in novel[:remaining]:
            selected.append(candidate)
            combined.extend(candidate["records"])
            fallback["added_candidates"].append(
                {"object_id": candidate["records"][0]["object_id"], "bbox": list(candidate["box"])}
            )
        for index, target, entry in unresolved:
            entry["status"] = "unmatched"
            warnings.append(
                f"Visible garment {index} ({target.concept}) couldn't be segmented reliably and was left out of the results."
            )
        fallback["status"] = "completed"
        fallback["record_count"] = len(generic_records)
        save()
    report.update(status="completed", record_count=len(combined))
    save()
    return OutfitSegments(combined, warnings, bool(warnings), report)

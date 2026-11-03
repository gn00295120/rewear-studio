"""Local synthetic-image regression tests, not a garment identity accuracy benchmark."""

import json
from collections import Counter
from copy import deepcopy

from PIL import Image, ImageDraw, ImageEnhance, ImageOps
import pytest

import wardrobe.duplicates as module
from wardrobe.duplicates import suggest_duplicates, visual_signature


def shirt(color=(32, 85, 175), background="white"):
    image = Image.new("RGBA", (300, 340), background)
    draw = ImageDraw.Draw(image)
    draw.polygon(
        [
            (95, 45),
            (120, 55),
            (150, 72),
            (180, 55),
            (205, 45),
            (260, 105),
            (223, 145),
            (205, 120),
            (205, 295),
            (95, 295),
            (95, 120),
            (77, 145),
            (40, 105),
        ],
        fill=color,
    )
    draw.line([(122, 77), (150, 92), (179, 77)], fill=(15, 35, 80), width=5)
    draw.rectangle((163, 115, 186, 142), fill=(18, 45, 105))
    draw.line([(102, 280), (198, 280)], fill=(18, 45, 105), width=4)
    return image


def save_signature(tmp_path, name, image):
    path = tmp_path / name
    image.save(path)
    result = visual_signature(path)
    assert result, f"No signature for synthetic image {name}"
    return result


@pytest.fixture
def signature(tmp_path):
    return save_signature(tmp_path, "shirt.png", shirt())


def item(item_id, signature=None, **changes):
    result = {
        "id": item_id,
        "job_id": "photo-" + item_id,
        "created_at": "2026-10-20T12:00:00+00:00",
        "reviewed": False,
        "quality": {"status": "ready"},
        "record": {
            "category": "top",
            "subtype": "t-shirt",
            "brand": None,
            "colors": ["blue"],
            "pattern": "solid",
        },
        "visual_signature": deepcopy(signature),
    }
    record = changes.pop("record", {})
    result["record"].update(record)
    result.update(changes)
    return result


def test_signature_is_small_reproducible_json_and_does_not_expose_source(tmp_path):
    path = tmp_path / "private-family-photo.png"
    shirt().save(path)
    first, second = visual_signature(path), visual_signature(path)
    assert first == second and first["version"] == 1
    assert first["structure"] is True
    assert len(first["dhash"]) == len(first["shape"]) == 16
    assert sum(first["colors"]) == pytest.approx(1, abs=0.00002)
    encoded = json.dumps(first, allow_nan=False)
    assert len(encoded) < 700 and "private" not in encoded and str(tmp_path) not in encoded


@pytest.mark.parametrize("variant", ["crop", "lighting", "padding", "transparent", "jpeg"])
def test_same_garment_with_changed_crop_lighting_or_background_is_suggested(tmp_path, signature, variant):
    image = shirt()
    filename = variant + ".png"
    if variant == "crop":
        image = image.crop((25, 30, 280, 315)).resize((420, 470))
    elif variant == "lighting":
        image = ImageEnhance.Brightness(image).enhance(0.78)
    elif variant == "padding":
        image = ImageOps.expand(image, border=100, fill="white")
    elif variant == "transparent":
        image = shirt(background=(255, 255, 255, 0))
    elif variant == "jpeg":
        image, filename = image.convert("RGB"), "lossy.jpg"
    changed = save_signature(tmp_path, filename, image)
    candidates = suggest_duplicates([item("a", signature), item("b", changed)])
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate["item_ids"] == ["a", "b"] and candidate["human_required"] is True
    assert 0.6 < candidate["score"] < 1
    assert candidate["reasons"] and any(
        "not a probability of being the same item" in reason for reason in candidate["reasons"]
    )


def test_normalizes_visible_brand_color_subtype_and_pattern_labels(signature):
    first = item(
        "a",
        signature,
        record={"brand": " UNIQLO ", "colors": ["Navy Blue"], "subtype": "T shirt", "pattern": "plain"},
    )
    second = item(
        "b", signature, record={"brand": "uniqlo", "colors": ["深藍色"], "subtype": "T恤", "pattern": "素色"}
    )
    assert len(suggest_duplicates([first, second])) == 1


def test_exif_rotated_photo_has_the_same_garment_orientation(tmp_path, signature):
    exif = Image.Exif()
    exif[274] = 6
    path = tmp_path / "phone-rotation.jpg"
    shirt().rotate(90, expand=True).convert("RGB").save(path, exif=exif)
    rotated = visual_signature(path)
    assert len(suggest_duplicates([item("a", signature), item("b", rotated)])) == 1


@pytest.mark.parametrize(
    "change",
    [
        {"category": "bottom"},
        {"category": "other"},
        {"brand": "Different Brand"},
        {"colors": ["red"]},
        {"pattern": "striped"},
        {"subtype": "sweater"},
    ],
)
def test_conflicting_garment_evidence_is_not_overridden_by_matching_visuals(signature, change):
    first = item("a", signature, record={"brand": "Visible Brand"})
    second = item("b", signature, record={"brand": "Visible Brand", **change})
    assert suggest_duplicates([first, second]) == []


def test_matching_text_cannot_override_incompatible_visual_color(tmp_path, signature):
    red = save_signature(tmp_path, "red.png", shirt(color=(185, 30, 35)))
    assert suggest_duplicates([item("a", signature), item("b", red)]) == []


def test_uniform_color_patches_are_not_structural_garment_evidence(tmp_path):
    flat = save_signature(tmp_path, "flat.png", Image.new("RGB", (120, 160), (32, 85, 175)))
    assert flat["structure"] is False
    assert suggest_duplicates([item("a", flat), item("b", flat)]) == []


def test_old_records_need_more_than_shared_color_and_generic_type():
    assert suggest_duplicates([item("a"), item("b")]) == []
    first, second = item("a", record={"brand": "Brand"}), item("b", record={"brand": "Brand"})
    result = suggest_duplicates([first, second])
    assert len(result) == 1 and 0 < result[0]["score"] <= 0.45
    assert result[0]["human_required"] is True
    assert any("No comparable image features" in reason for reason in result[0]["reasons"])
    for change in ({"subtype": None}, {"subtype": "top"}, {"colors": []}, {"colors": ["multicolor"]}):
        assert suggest_duplicates([item("a", record={"brand": "Brand", **change}), second]) == []


@pytest.mark.parametrize("status", ["ready", "needs_confirmation"])
def test_visible_pending_confirmation_can_be_compared(signature, status):
    assert (
        len(suggest_duplicates([item("a", signature), item("b", signature, quality={"status": status})])) == 1
    )


@pytest.mark.parametrize("status", ["needs_retake", "unassessed", "unsupported"])
def test_unusable_quality_is_excluded_until_a_human_overrides_it(signature, status):
    first, second = item("a", signature), item("b", signature, quality={"status": status})
    assert suggest_duplicates([first, second]) == []
    second["reviewed"] = True
    assert len(suggest_duplicates([first, second])) == 1


def test_same_photo_and_already_grouped_pairs_are_excluded(signature):
    first, second = item("a", signature), item("b", signature)
    second["job_id"] = first["job_id"]
    assert suggest_duplicates([first, second]) == []
    second["job_id"] = "different-photo"
    groups = [{"id": "confirmed", "primary_item_id": "a", "item_ids": ["a", "b"]}]
    assert suggest_duplicates([first, second], groups) == []
    third = item("c", signature)
    assert {tuple(row["item_ids"]) for row in suggest_duplicates([first, second, third], groups)} == {
        ("a", "c"),
        ("b", "c"),
    }


def test_order_pair_ids_limits_and_input_are_stable(signature):
    items = [item(str(index), signature) for index in range(12)]
    before = deepcopy(items)
    result = suggest_duplicates(items, limit=7)
    assert result == suggest_duplicates(list(reversed(items)), limit=7)
    assert result == suggest_duplicates(items, limit=7) and items == before
    assert 0 < len(result) <= 7 and len({row["id"] for row in result}) == len(result)
    assert max(Counter(item_id for row in result for item_id in row["item_ids"]).values()) <= 3
    assert all(0 <= row["score"] <= 1 and row["human_required"] for row in result)
    pair = suggest_duplicates(items[:2])[0]
    assert pair["id"] == suggest_duplicates(items[1::-1])[0]["id"]


def test_compares_only_recent_bounded_items_within_category(signature, monkeypatch):
    assert module.MAX_ITEMS == 500 and module.MAX_SUGGESTIONS == 100
    monkeypatch.setattr(module, "MAX_ITEMS", 4)
    items = [item(str(index), signature, created_at=f"2026-10-{10 + index:02d}") for index in range(8)]
    items[6]["record"]["category"] = items[7]["record"]["category"] = "outerwear"
    compared = []
    original = module._similarity

    def tracked(first, second):
        assert first["category"] == second["category"]
        compared.append((first["id"], second["id"]))
        return original(first, second)

    monkeypatch.setattr(module, "_similarity", tracked)
    suggest_duplicates(items, limit=1)
    assert len(compared) == 2
    assert {item_id for pair in compared for item_id in pair} == {"4", "5", "6", "7"}


@pytest.mark.parametrize("bad", [None, [], "old", {"version": 0}, {"version": 1, "colors": [float("nan")]}])
def test_unsupported_signatures_are_ignored_without_inventing_visual_matches(signature, bad):
    assert suggest_duplicates([item("a", signature), item("b", bad)]) == []


def test_unsupported_record_types_and_options_are_harmless(signature):
    broken = [None, [], 5, "item", {}, {"record": []}, item("bad", signature, quality=[])]
    broken.append(item("bad-category", signature, record={"category": []}))
    broken.append(item("bad-id", signature, id=[]))
    broken.append(item("bad-colors", signature, record={"colors": [None, {}, []]}))
    # Missing metadata colors alone is allowed when actual image evidence exists.
    broken[-1]["visual_signature"] = None
    assert suggest_duplicates([item("a", signature), *broken], groups=[None, {}, {"item_ids": 5}]) == []
    for limit in (None, -1, 0, True, "40", 2.5):
        assert suggest_duplicates([item("a", signature), item("b", signature)], limit=limit) == []
    assert suggest_duplicates(None) == []


def test_invalid_blank_and_oversized_media_return_no_signature(tmp_path, monkeypatch):
    broken = tmp_path / "broken.jpg"
    broken.write_bytes(b"not an image")
    for path in (broken, tmp_path / "missing.jpg", tmp_path, None, {}):
        assert visual_signature(path) == {}
    blank = tmp_path / "blank.png"
    Image.new("RGB", (100, 100), "white").save(blank)
    assert visual_signature(blank) == {}
    Image.new("RGBA", (100, 100), (10, 30, 200, 0)).save(blank)
    assert visual_signature(blank) == {}
    shirt().save(blank)
    monkeypatch.setattr(module, "MAX_IMAGE_PIXELS", 100)
    assert visual_signature(blank) == {}
    monkeypatch.setattr(module, "MAX_IMAGE_PIXELS", 24_000_000)
    monkeypatch.setattr(module, "MAX_IMAGE_BYTES", 8)
    assert visual_signature(blank) == {}


def test_unsupported_postscript_never_invokes_an_external_decoder(tmp_path, monkeypatch):
    from PIL import EpsImagePlugin

    path = tmp_path / "unsupported.eps"
    path.write_bytes(b"%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 100 100\nshowpage\n")

    def forbidden_decoder(*args, **kwargs):
        raise AssertionError("Similarity extraction must not invoke external image decoders")

    monkeypatch.setattr(EpsImagePlugin.EpsImageFile, "load", forbidden_decoder)
    assert visual_signature(path) == {}


def pale_object(kind="tank"):
    """Draw low-contrast white objects; background removal leaves sparse outlines."""
    image = Image.new("RGB", (240, 300), "white")
    draw = ImageDraw.Draw(image)
    if kind == "tank":
        points = [(50, 20), (83, 20), (96, 68), (145, 68), (157, 20), (190, 20), (205, 270), (35, 270)]
        draw.polygon(points, fill=(246, 245, 241))
        draw.line(points + [points[0]], fill=(205, 201, 193), width=2)
        draw.line([(53, 97), (59, 180), (48, 254)], fill=(179, 175, 168), width=2)
        draw.line([(175, 112), (163, 194), (174, 256)], fill=(188, 184, 176), width=2)
        for y in range(94, 255, 9):
            for x in range(70, 160, 9):
                draw.point((x + (y % 4), y), fill=(175, 173, 169))
    elif kind == "cup":
        draw.ellipse((37, 55, 173, 90), fill=(246, 246, 244), outline=(195, 195, 190), width=2)
        draw.arc((140, 78, 218, 197), 270, 90, fill=(185, 185, 180), width=3)
        draw.arc((150, 91, 205, 182), 270, 90, fill=(205, 205, 200), width=2)
        draw.line([(37, 72), (53, 236), (156, 236), (173, 72)], fill=(195, 195, 190), width=2)
    elif kind == "towel":
        draw.rectangle((40, 20, 190, 270), fill=(246, 246, 244), outline=(198, 198, 193), width=2)
        for y in range(45, 260, 25):
            draw.line([(49, y), (183, y + 3)], fill=(208, 208, 203), width=2)
    elif kind == "shirt":
        points = [
            (75, 35),
            (96, 45),
            (124, 58),
            (152, 45),
            (170, 35),
            (218, 88),
            (190, 122),
            (169, 105),
            (169, 268),
            (77, 268),
            (77, 105),
            (57, 122),
            (27, 88),
        ]
        draw.polygon(points, fill=(246, 246, 244))
        draw.line(points + [points[0]], fill=(195, 195, 190), width=2)
        draw.rectangle((135, 96, 153, 121), outline=(180, 180, 175), width=2)
        draw.line([(85, 250), (162, 250)], fill=(190, 190, 185), width=2)
    else:
        raise AssertionError("Unknown synthetic object")
    return image


def pale_item(item_id, signature, **record):
    return item(
        item_id,
        signature,
        record={"subtype": "tank top", "colors": ["white"], "pattern": None, **record},
    )


@pytest.fixture
def pale_signature(tmp_path):
    signature = save_signature(tmp_path, "white-tank.png", pale_object())
    assert int(signature["shape"], 16).bit_count() < 4
    return signature


@pytest.mark.parametrize("variant", ["crop", "lighting"])
def test_sparse_white_outline_uses_strong_remaining_evidence_for_human_candidate(
    tmp_path, pale_signature, variant
):
    image = pale_object()
    if variant == "crop":
        image = image.crop((15, 8, 224, 284)).resize((300, 396))
    else:
        image = ImageEnhance.Brightness(image).enhance(1.03)
    changed = save_signature(tmp_path, "white-variant.png", image)
    assert int(changed["shape"], 16).bit_count() < 4
    pair = [pale_item("one", pale_signature), pale_item("two", changed)]
    before = deepcopy(pair)
    suggestions = suggest_duplicates(pair)
    assert len(suggestions) == 1 and pair == before
    suggestion = suggestions[0]
    assert suggestion["human_required"] is True and 0.5 < suggestion["score"] < 0.75
    assert any("too sparse to compare silhouettes" in reason for reason in suggestion["reasons"])
    assert not any(
        "silhouette and local brightness features are similar" in reason for reason in suggestion["reasons"]
    )


@pytest.mark.parametrize("other", ["cup", "towel", "shirt"])
def test_other_white_objects_do_not_match_even_with_identical_record_labels(tmp_path, pale_signature, other):
    different = save_signature(tmp_path, "different-white.png", pale_object(other))
    # Deliberately identical labels ensure actual visual evidence must reject it.
    assert suggest_duplicates([pale_item("one", pale_signature), pale_item("two", different)]) == []


@pytest.mark.parametrize("missing", [{"subtype": None}, {"colors": []}])
def test_sparse_outline_needs_specific_matching_attributes(pale_signature, missing):
    assert (
        suggest_duplicates([pale_item("one", pale_signature), pale_item("two", pale_signature, **missing)])
        == []
    )


@pytest.mark.parametrize("bits", [0, 1, 64])
def test_sparse_outline_cannot_use_empty_or_uninformative_gradient_hash(pale_signature, bits):
    uninformative = {**pale_signature, "dhash": f"{(1 << bits) - 1:016x}"}
    assert suggest_duplicates([pale_item("one", uninformative), pale_item("two", uninformative)]) == []


def test_blank_white_images_do_not_gain_candidates_from_shared_color(tmp_path, pale_signature):
    path = tmp_path / "blank-white.png"
    Image.new("RGB", (240, 300), "white").save(path)
    empty = visual_signature(path)
    assert empty == {}
    assert suggest_duplicates([pale_item("one", pale_signature), pale_item("two", empty)]) == []
    assert suggest_duplicates([pale_item("one", empty), pale_item("two", empty)]) == []


def test_disagreeing_non_sparse_shapes_still_reject_otherwise_matching_features(signature):
    left = {**signature, "shape": f"{(1 << 16) - 1:016x}"}
    right = {**signature, "shape": f"{((1 << 16) - 1) << 48:016x}"}
    assert suggest_duplicates([item("one", left), item("two", right)]) == []

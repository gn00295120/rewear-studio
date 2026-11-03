"""Outfit selection and quality contracts, with no real model requests."""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from PIL import Image, ImageDraw

from wardrobe.config import Settings
from wardrobe.duplicates import visual_signature
from wardrobe.media import MediaError, crop_selection, prepare_image
from wardrobe.pipeline import JobManager
from wardrobe.providers import INSPECTION_PROMPT, WEARING_PROMPT, Providers
from wardrobe.quality import apply_inspection, result_counts, summarize_results, unassessed_quality
from wardrobe.schemas import unknown_garment
from wardrobe.store import Store


def assessment(visibility="partial", target="one_garment", **attributes):
    return {
        "target": target,
        "visibility": visibility,
        "reason": "手臂遮住衣服側邊。" if visibility != "complete" else None,
        "garment": {
            **unknown_garment(),
            "category": "top",
            "subtype": "shirt",
            "colors": ["blue"],
            "condition": "good",
            "confidence": 0.86,
            "needs_review": False,
            **attributes,
        },
    }


def assert_colour(image, xy, colour, tolerance=12):
    assert all(abs(a - b) <= tolerance for a, b in zip(image.getpixel(xy), colour))


def oriented_photo(path):
    image = Image.new("RGB", (120, 80))
    draw = ImageDraw.Draw(image)
    for bounds, colour in (
        ((0, 0, 59, 39), (230, 20, 20)),
        ((60, 0, 119, 39), (20, 210, 20)),
        ((0, 40, 59, 79), (20, 20, 230)),
        ((60, 40, 119, 79), (230, 210, 20)),
    ):
        draw.rectangle(bounds, fill=colour)
    exif = image.getexif()
    exif[274] = 6
    image.save(path, quality=100, subsampling=0, exif=exif)


def test_selection_uses_real_pixel_coordinates_and_preserves_source(tmp_path):
    source, destination = tmp_path / "source.png", tmp_path / "selected.png"
    image = Image.new("RGB", (100, 80))
    image.putdata([(x * 2, y * 3, (x + y) % 256) for y in range(80) for x in range(100)])
    image.save(source)
    original = source.read_bytes()

    result = crop_selection(source, [0.253, 0.2, 0.707, 0.805], destination)

    assert result["bbox"] == [25, 16, 71, 65]
    assert (result["width"], result["height"]) == (46, 49)
    with Image.open(destination) as selected:
        assert selected.tobytes() == image.crop((25, 16, 71, 65)).tobytes()
    assert source.read_bytes() == original
    with pytest.raises(MediaError, match="cannot be overwritten"):
        crop_selection(source, [0, 0, 1, 1], source)
    assert source.read_bytes() == original


def test_person_box_refers_to_exif_oriented_preview_not_encoded_raster(tmp_path):
    source, preview, selected = (tmp_path / name for name in ("input.jpg", "preview.jpg", "selected.png"))
    oriented_photo(source)
    original = source.read_bytes()
    assert prepare_image(source, preview) == {"width": 80, "height": 120}
    result = crop_selection(preview, [0, 0, 0.5, 0.5], selected)
    assert result["bbox"] == [0, 0, 40, 60]
    with Image.open(selected) as image:
        assert image.size == (40, 60)
        assert_colour(image, (20, 30), (20, 20, 230))
    with Image.open(preview) as image:
        assert_colour(image, (60, 90), (20, 210, 20))
    assert source.read_bytes() == original


@pytest.mark.parametrize(
    "bounds",
    [
        [0, 0, 0, 1],
        [0.8, 0, 0.2, 1],
        [-0.01, 0, 1, 1],
        [0, 0, 1.01, 1],
        [0, 0, float("nan"), 1],
        [0, 0, True, 1],
        [0, "0", 1, 1],
        [0, 0, 1],
    ],
)
def test_invalid_person_selection_does_not_write_output(tmp_path, bounds):
    source, destination = tmp_path / "source.png", tmp_path / "selected.jpg"
    Image.new("RGB", (80, 80), "blue").save(source)
    with pytest.raises(MediaError, match="Invalid person selection"):
        crop_selection(source, bounds, destination)
    assert not destination.exists()


def test_recognisable_worn_partial_requires_confirmation_and_retains_visible_attributes():
    given = assessment(confidence=0.5, needs_review=False)
    displayed, model_record, quality = apply_inspection(given, wearing=True)
    assert displayed["category"] == "top" and displayed["colors"] == ["blue"]
    assert displayed["needs_review"] is True
    assert model_record["confidence"] == 0.5
    assert quality["status"] == "needs_confirmation"
    assert quality["version"] == 3 and quality["capture_mode"] == "outfit"
    assert quality["model_visibility"] == quality["visibility"] == "partial"
    assert "confirm garment type and details manually" in "".join(quality["reasons"])
    assert result_counts([{"quality": quality}])["usable_count"] == 0
    assert given["garment"]["needs_review"] is False  # Never mutate the response supplied by callers.


@pytest.mark.parametrize(
    "visibility,target,attributes,metrics",
    [
        ("fragment", "one_garment", {}, {}),
        ("unclear", "one_garment", {}, {}),
        ("complete", "no_garment", {"confidence": 0.99}, {}),
        ("partial", "multiple_garments", {}, {}),
        ("partial", "uncertain", {}, {}),
        ("partial", "one_garment", {"category": "other"}, {}),
        ("partial", "one_garment", {"confidence": 0.49}, {}),
        ("partial", "one_garment", {}, {"mask_fill": 0.2, "frame_edges": ["bottom"]}),
        ("complete", "one_garment", {}, {"mask_fill": 0.2, "frame_edges": ["bottom"]}),
    ],
)
def test_wearing_does_not_let_fragments_invalid_targets_or_sparse_edges_through(
    visibility, target, attributes, metrics
):
    displayed, original, quality = apply_inspection(
        assessment(visibility, target, **attributes), metrics, wearing=True
    )
    assert displayed == unknown_garment()
    assert original["subtype"] == "shirt"
    assert quality["status"] == "needs_retake"
    assert quality["model_visibility"] == visibility and quality["model_target"] == target
    assert quality["reasons"]


def test_worn_partial_and_geometrically_downgraded_complete_remain_distinct():
    given = assessment()
    visible, _, worn = apply_inspection(given, {"mask_fill": 0.2, "frame_edges": []}, wearing=True)
    assert visible["subtype"] == "shirt" and worn["status"] == "needs_confirmation"
    hidden, _, unworn = apply_inspection(given)
    assert hidden == unknown_garment() and unworn["status"] == "needs_retake"
    complete, _, ready = apply_inspection(
        assessment("complete"), {"mask_fill": 0.2, "frame_edges": []}, wearing=True
    )
    assert ready["status"] == "ready" and complete["category"] == "top"


def test_confirmation_counts_and_notices_clear_after_explicit_review():
    _, _, pending = apply_inspection(assessment(), wearing=True)
    job = {"status": "completed", "stage": "分析完成", "quality_version": 3, "warnings": []}
    items = [{"quality": pending}, {"quality": pending, "reviewed": True}]
    summary = summarize_results(job, items)
    assert summary["confirmation_count"] == 1 and summary["usable_count"] == 1
    assert summary["retake_count"] == summary["unassessed_count"] == 0
    assert "pending" in summary["stage"] and "manual confirmation" in "".join(summary["warnings"])
    items[0]["reviewed"] = True
    reviewed = summarize_results(summary, items)
    assert reviewed["confirmation_count"] == 0 and reviewed["usable_count"] == 2
    assert reviewed["stage"] == "Analysis complete" and reviewed["warnings"] == []
    failed = unassessed_quality(wearing=True)
    assert failed["capture_mode"] == "outfit" and failed["status"] == "unassessed"


@pytest.mark.parametrize("version", [1, 2, 3])
def test_older_quality_versions_and_empty_results_remain_supported(version):
    job = {"status": "completed", "stage": "分析完成", "quality_version": version, "warnings": []}
    summary = summarize_results(job, [])
    assert summary["confirmation_count"] == 0 and summary["stage"] == "Results removed"


class OutfitProviders:
    def __init__(self, visibility="partial"):
        self.visibility = visibility
        self.segmented = []
        self.inspected = []

    async def segment(self, path, concept, video=False):
        assert video is False and concept == "garment"
        self.segmented.append(path)
        with Image.open(path) as image:
            width, height = image.size
        return [
            {
                "kind": "box",
                "object_id": "shirt-1",
                "frame": None,
                "left": 5,
                "top": 5,
                "right": width - 5,
                "bottom": height - 5,
            }
        ]

    async def inspect(self, path, provider, *, context_path=None, wearing=False):
        assert wearing is True and context_path.is_file() and provider == "local"
        self.inspected.append(path)
        return assessment(self.visibility)


def new_job(tmp_path, providers, *, mode="outfit", options=None):
    settings = Settings(data_dir=tmp_path / "state", api_key=None)
    store = Store(settings.data_dir)
    job = store.create_job(mode=mode, input_name="input.jpg", options=options or {})
    directory = settings.data_dir / "jobs" / job["id"]
    directory.mkdir(parents=True)
    oriented_photo(directory / "input.jpg")
    original_url = f"/media/{job['id']}/input.jpg"
    store.update_job(job["id"], source_url=original_url)
    manager = JobManager(settings, store, providers)
    return manager, store, job["id"], directory


@pytest.mark.parametrize("selected", [False, True])
def test_outfit_pipeline_keeps_original_and_links_items_to_actual_segmentation_source(tmp_path, selected):
    provider = OutfitProviders()
    options = {"person_box": [0, 0, 0.5, 0.5]} if selected else {}
    manager, store, job_id, directory = new_job(tmp_path, provider, options=options)
    original = (directory / "input.jpg").read_bytes()
    asyncio.run(manager._run(job_id))
    job, item = store.job(job_id), store.items(job_id)[0]
    expected_source = directory / ("selected.jpg" if selected else "preview.jpg")

    assert job["status"] == "completed" and job["quality_version"] == 3
    assert job["confirmation_count"] == 1 and job["usable_count"] == 0
    assert job["options"] == options
    assert provider.segmented == [expected_source] and len(provider.inspected) == 1
    assert job["source_url"] == f"/media/{job_id}/input.jpg"
    assert job["preview_url"] == item["original_url"] == f"/media/{job_id}/preview.jpg"
    assert item["source_url"] == f"/media/{job_id}/{expected_source.name}"
    assert item["quality"]["capture_mode"] == "outfit"
    assert item["record"]["category"] == item["model_record"]["category"] == "top"
    assert item["image_url"] != item["cutout_url"]
    assert item["visual_signature"] == visual_signature(provider.inspected[0])
    assert item["visual_signature"]["version"] == 1
    assert (directory / "input.jpg").read_bytes() == original
    segments = json.loads((directory / "segments.json").read_text())
    assert segments["source_url"] == item["source_url"]
    with Image.open(directory / "preview.jpg") as preview:
        assert preview.size == (80, 120)
        assert_colour(preview, (60, 90), (20, 210, 20))
    if selected:
        assert job["selection_url"] == item["source_url"]
        assert job["selection_bbox"] == segments["selection"]["bbox"] == [0, 0, 40, 60]
        assert item["bbox"] == [5, 5, 35, 55]
        with Image.open(provider.inspected[0]) as cutout:
            assert cutout.size == (30, 50)
            assert_colour(cutout, (15, 25), (20, 20, 230))
        with Image.open(directory / "overlay.jpg") as overlay:
            assert overlay.size == (40, 60)


def test_single_mode_keeps_legacy_inspect_keyword_contract(tmp_path):
    class LegacyProvider:
        async def inspect(self, path, provider, *, context_path=None):
            assert context_path is None
            return assessment("complete")

    manager, store, job_id, _ = new_job(tmp_path, LegacyProvider(), mode="single")
    asyncio.run(manager._run(job_id))
    assert store.job(job_id)["status"] == "completed"
    assert store.job(job_id)["usable_count"] == 1


def test_wearing_prompt_is_opt_in_and_keeps_strict_inspection_validation(tmp_path):
    async def run():
        provider = Providers(Settings(data_dir=tmp_path, api_key=None))
        provider._vision_request = AsyncMock(return_value=json.dumps(assessment()))
        path, context = Path("cutout.jpg"), Path("context.jpg")
        try:
            result = await provider.inspect(path, "local", context_path=context, wearing=True)
            assert result["visibility"] == "partial" and result["garment"]["needs_review"] is True
            call = provider._vision_request.call_args
            assert call.args[0] == [path, context]
            assert call.kwargs["system_prompt"] == INSPECTION_PROMPT + WEARING_PROMPT
            await provider.inspect(path, "local")
            assert provider._vision_request.call_args.kwargs["system_prompt"] == INSPECTION_PROMPT
        finally:
            await provider.close()

    asyncio.run(run())

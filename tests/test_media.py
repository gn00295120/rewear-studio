import shutil
import subprocess

import pytest
from PIL import Image
from meta_sam_parser import FrameReference
from meta_sam_parser._mask_codec import _encode_raster

from wardrobe.media import (
    MediaError,
    _placed_mask,
    context_crop,
    crop_metrics,
    crop_records,
    prepare_image,
    prepare_video,
    render_overlay,
    render_video_overlay,
    video_frame,
)


def box(oid="1", frame=None, bounds=(0, 0, 10, 10)):
    return dict(
        kind="box", object_id=oid, frame=frame, **dict(zip(("left", "top", "right", "bottom"), bounds))
    )


def mask(oid="1", frame=None, bounds=(0, 0, 10, 10), size=(10, 10), pixels=None):
    width, height = size
    pixels = pixels if pixels is not None else [1] * (width * height)
    return dict(
        kind="mask",
        object_id=oid,
        frame=frame,
        bounds=dict(zip(("left", "top", "right", "bottom"), bounds)),
        mask=dict(
            width=width, height=height, encoding="one_bit", payload=_encode_raster(pixels, width, height)
        ),
    )


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "source.png"
    Image.new("RGB", (20, 20), (200, 20, 20)).save(path)
    return path


def test_image_exif_orientation_and_transparency(tmp_path):
    source, destination = tmp_path / "source.jpg", tmp_path / "normalized.jpg"
    image = Image.new("RGB", (10, 20), "blue")
    exif = image.getexif()
    exif[274] = 6
    image.save(source, exif=exif)
    assert prepare_image(source, destination) == {"width": 20, "height": 10}
    transparent = tmp_path / "transparent.png"
    Image.new("RGBA", (5, 5), (255, 0, 0, 0)).save(transparent)
    prepare_image(transparent, destination)
    with Image.open(destination) as normalized:
        assert normalized.mode == "RGB" and min(normalized.getpixel((2, 2))) > 245


def test_image_resize_and_malformed_input(tmp_path):
    source = tmp_path / "big.png"
    Image.new("RGB", (2000, 1000), "white").save(source)
    assert prepare_image(source, tmp_path / "normalized.jpg") == {"width": 1600, "height": 800}
    source.write_text("not a real image")
    with pytest.raises(MediaError, match="Unable to read image"):
        prepare_image(source, tmp_path / "bad.jpg")


def test_mask_clips_negative_origin_without_wrapping():
    placed = _placed_mask(mask(bounds=(-3, -2, 7, 8)), 20, 20)
    assert placed.getbbox() == (0, 0, 7, 8)
    assert placed.getpixel((0, 0)) == 255 and placed.getpixel((19, 19)) == 0


def test_mask_clips_bottom_right_and_resizes_local_raster():
    placed = _placed_mask(mask(bounds=(18, 18, 24, 24), size=(3, 3)), 20, 20)
    assert placed.getbbox() == (18, 18, 20, 20)
    outside = _placed_mask(mask(bounds=(25, 25, 35, 35)), 20, 20)
    assert outside.getbbox() is None


def test_mask_byte_raster_not_ambiguous_packed_bits():
    pixels = [1, 0, 0, 0, 0, 0, 0, 0, 1, 0, 1, 0, 0, 0, 0, 0, 0, 0]
    placed = _placed_mask(mask(bounds=(0, 0, 9, 2), size=(9, 2), pixels=pixels), 20, 20)
    assert [placed.getpixel((x, 0)) // 255 for x in range(9)] == pixels[:9]
    assert [placed.getpixel((x, 1)) // 255 for x in range(9)] == pixels[9:]


def test_invalid_mask_quota_and_payload_fail_safely():
    record = mask()
    record["mask"]["width"] = 100_000_000
    with pytest.raises(MediaError, match="segmentation mask"):
        _placed_mask(record, 20, 20)
    record = mask()
    record["mask"]["payload"] = "invalid secret payload"
    with pytest.raises(MediaError) as error:
        _placed_mask(record, 20, 20)
    assert "secret" not in str(error.value)


def test_crops_dedup_clipped_boxes_masks_and_safe_paths(source, tmp_path):
    records = [
        box("../../traversal", bounds=(-10, -5, 15, 16)),
        box("../../traversal", bounds=(-2, -3, 10, 10)),
        mask("../../traversal", bounds=(-2, -3, 10, 10), size=(12, 13)),
        box("outside", bounds=(30, 30, 40, 40)),
        box("nan", bounds=(0, 0, float("nan"), 10)),
    ]
    results = crop_records(source, records, tmp_path / "crops")
    assert len(results) == 1 and results[0]["bbox"] == [0, 0, 10, 10]
    assert results[0]["path"].parent == tmp_path / "crops"
    with Image.open(results[0]["path"]) as crop:
        assert crop.size == (10, 10)


def test_mask_only_and_invalid_optional_mask_fallback(source, tmp_path):
    bad_mask = mask("boxed")
    bad_mask["mask"]["payload"] = "broken"
    records = [
        mask("mask-only", bounds=(3, 4, 8, 9), size=(5, 5)),
        box("boxed"),
        bad_mask,
        mask("empty", pixels=[0] * 100),
    ]
    crops = crop_records(source, records, tmp_path / "crops")
    assert {r["object_id"] for r in crops} == {"mask-only", "boxed"}
    assert crops[0]["bbox"] == [3, 4, 8, 9]


def test_context_retains_surroundings_and_source_while_mask_metrics_are_exact(tmp_path):
    source = tmp_path / "original.png"
    original = Image.new("RGB", (100, 100), (40, 100, 180))
    original.save(source)
    original_bytes = source.read_bytes()
    # A thin diagonal surface can have a large bounding box. Its measured fill
    # is evidence, and does not itself classify a scarf or garment as incomplete.
    pixels = [int(x == y) for y in range(20) for x in range(20)]
    records = [box(bounds=(30, 30, 50, 50)), mask(bounds=(30, 30, 50, 50), size=(20, 20), pixels=pixels)]
    item = crop_records(source, records, tmp_path / "crops")[0]
    assert item["metrics"]["mask_fill"] == 0.05
    assert item["metrics"]["mask_area"] == 20
    assert item["metrics"]["source_size"] == [100, 100]
    assert item["metrics"]["frame_edges"] == []
    assert "visibility" not in item["metrics"]
    assert item["path"] != item["context_path"]
    with Image.open(item["context_path"]) as context:
        assert context.size == (28, 28)  # 20% padding on each side.
        assert max(abs(a - b) for a, b in zip(context.getpixel((14, 14)), (40, 100, 180))) < 12
    with Image.open(item["path"]) as cutout:
        assert min(cutout.getpixel((5, 15))) > 225
    assert source.read_bytes() == original_bytes


def test_context_only_migration_has_unknown_mask_metrics_and_cannot_overwrite_source(source, tmp_path):
    before = source.read_bytes()
    output = tmp_path / "context.png"
    assert context_crop(source, [3, 4, 8, 9], output) == output
    metrics = crop_metrics(source, [0, 3, 10, 20])
    assert metrics["mask_fill"] is None and metrics["mask_area"] is None
    assert metrics["frame_edges"] == ["left", "bottom"]
    with pytest.raises(MediaError, match="cannot be overwritten"):
        context_crop(source, [3, 4, 8, 9], source)
    assert source.read_bytes() == before


def test_context_box_marker_and_clipping_are_in_context_coordinates(source, tmp_path):
    output = tmp_path / "context.png"
    context_crop(source, [-3, -4, 10, 10], output)
    with Image.open(output) as context:
        assert context.size == (12, 12)
        assert context.getpixel((0, 0)) == (238, 128, 35)
        assert context.getpixel((9, 9)) == (238, 128, 35)
        assert context.getpixel((11, 11)) == (200, 20, 20)
    with pytest.raises(MediaError, match="Invalid garment bounds"):
        context_crop(source, [30, 30, 40, 40], output)


def test_expanded_context_retains_original_pixels_and_target_coordinates(tmp_path):
    source = tmp_path / "source.png"
    im = Image.new("RGB", (100, 100))
    im.putdata([(x, y, 80) for y in range(100) for x in range(100)])
    im.save(source)
    before = source.read_bytes()
    output = context_crop(source, [40, 30, 60, 50], tmp_path / "expanded.png", padding=1.0)
    with Image.open(output) as actual:
        assert actual.size == (60, 60)
        assert actual.getpixel((0, 0)) == (20, 10, 80)
        assert actual.getpixel((59, 59)) == (79, 69, 80)
        assert actual.getpixel((20, 20)) == (238, 128, 35)
    assert source.read_bytes() == before
    for padding in (-1, 3, float("nan"), float("inf"), True, "1"):
        with pytest.raises(MediaError, match="padding"):
            context_crop(source, [40, 30, 60, 50], output, padding=padding)


def test_video_frame_reference_selects_exact_frame(source, tmp_path):
    records = [box("first", FrameReference(0)), box("later", FrameReference(2)), box("still")]
    assert [x["object_id"] for x in crop_records(source, records, tmp_path / "crops", frame=2)] == ["later"]
    assert [x["object_id"] for x in crop_records(source, records, tmp_path / "crops")] == ["still"]
    render_overlay(source, records, tmp_path / "frame.jpg", frame=2)
    assert (tmp_path / "frame.jpg").exists()


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="ffmpeg is not installed"
)
def test_real_video_normalization_limits_and_frame_accurate_overlay(tmp_path):
    source = tmp_path / "source.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-nostdin",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=white:s=100x80:r=10:d=1",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(source),
        ],
        check=True,
        timeout=15,
    )
    prepared = tmp_path / "clip.mp4"
    metadata = prepare_video(source, prepared, 5)
    assert metadata["fps"] == 5 and metadata["frames"] == 5
    with pytest.raises(MediaError, match="Video exceeds"):
        prepare_video(source, tmp_path / "too-long.mp4", 0)
    output = tmp_path / "overlay.mp4"
    render_video_overlay(prepared, [box("tracked", FrameReference(2), (10, 10, 80, 65))], output, metadata)
    frame0, frame2 = tmp_path / "0.png", tmp_path / "2.png"
    video_frame(output, frame0, 0, 5)
    video_frame(output, frame2, 2, 5)
    with Image.open(frame0) as first, Image.open(frame2) as third:
        assert min(first.getpixel((11, 40))) > 240
        assert min(third.getpixel((11, 40))) < 220
    assert not list(tmp_path.glob("overlay-*/"))
    previous = frame0.read_bytes()
    with pytest.raises(MediaError, match="does not contain the specified frame"):
        video_frame(prepared, frame0, 100, 5)
    assert frame0.read_bytes() == previous

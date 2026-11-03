"""Format and content regressions, with real codecs and no inference requests."""

import asyncio
import shutil
import subprocess

import pytest
from PIL import Image, ImageDraw, features

from wardrobe.config import Settings
from wardrobe.media import MediaError, prepare_image, prepare_video
from wardrobe.pipeline import JobManager
from wardrobe.store import Store


COLORS = {
    "red": (220, 25, 35),
    "green": (25, 180, 45),
    "blue": (30, 55, 220),
    "yellow": (230, 205, 25),
}
HAS_VIDEO = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def assert_color(actual, expected, tolerance=15):
    assert max(abs(a - b) for a, b in zip(actual, expected)) <= tolerance


@pytest.mark.parametrize(
    "orientation,expected",
    [
        (1, ("red", "green", "blue", "yellow")),
        (2, ("green", "red", "yellow", "blue")),
        (3, ("yellow", "blue", "green", "red")),
        (4, ("blue", "yellow", "red", "green")),
        (5, ("red", "blue", "green", "yellow")),
        (6, ("blue", "red", "yellow", "green")),
        (7, ("yellow", "green", "blue", "red")),
        (8, ("green", "yellow", "red", "blue")),
    ],
)
def test_jpeg_exif_all_orientations_move_actual_content_once(tmp_path, orientation, expected):
    # Four differently coloured corners make rotation and mirroring observable;
    # checking width/height alone would miss incorrect reflection and 180° cases.
    raw = Image.new("RGB", (120, 80))
    draw = ImageDraw.Draw(raw)
    for rectangle, color in [
        ((0, 0, 59, 39), "red"),
        ((60, 0, 119, 39), "green"),
        ((0, 40, 59, 79), "blue"),
        ((60, 40, 119, 79), "yellow"),
    ]:
        draw.rectangle(rectangle, fill=COLORS[color])
    exif = raw.getexif()
    exif[274] = orientation
    source, normalized, again = (tmp_path / name for name in ("camera.jpg", "normalized.jpg", "again.jpg"))
    raw.save(source, quality=98, subsampling=0, exif=exif)
    original = source.read_bytes()

    prepare_image(source, normalized)
    prepare_image(normalized, again)
    for path in (normalized, again):
        with Image.open(path) as image:
            assert image.size == ((80, 120) if orientation >= 5 else (120, 80))
            assert image.getexif().get(274) in (None, 1)
            width, height = image.size
            coordinates = [
                (width // 4, height // 4),
                (3 * width // 4, height // 4),
                (width // 4, 3 * height // 4),
                (3 * width // 4, 3 * height // 4),
            ]
            for point, color in zip(coordinates, expected):
                assert_color(image.getpixel(point), COLORS[color])
    assert source.read_bytes() == original


def test_transparent_white_garment_keeps_white_fabric_and_blends_edges(tmp_path):
    source, output = tmp_path / "white-shirt.png", tmp_path / "white-shirt.jpg"
    # Hidden RGB is magenta: simply dropping alpha would stain the background.
    image = Image.new("RGBA", (120, 120), (255, 0, 255, 0))
    draw = ImageDraw.Draw(image)
    draw.polygon(
        [
            (40, 20),
            (80, 20),
            (105, 40),
            (90, 60),
            (80, 50),
            (80, 105),
            (40, 105),
            (40, 50),
            (30, 60),
            (15, 40),
        ],
        fill=(255, 255, 255, 255),
    )
    draw.rectangle((48, 55, 72, 72), fill=(*COLORS["blue"], 255))
    # A semitransparent dark seam should composite to gray, not opaque black.
    draw.rectangle((45, 88, 75, 96), fill=(0, 0, 0, 128))
    image.save(source)

    prepare_image(source, output)
    with Image.open(output) as converted:
        assert converted.mode == "RGB" and converted.format == "JPEG"
        assert_color(converted.getpixel((5, 5)), (255, 255, 255), 5)
        assert_color(converted.getpixel((60, 35)), (255, 255, 255), 5)
        assert_color(converted.getpixel((60, 63)), COLORS["blue"], 15)
        assert_color(converted.getpixel((60, 92)), (127, 127, 127), 8)


@pytest.mark.parametrize("format_name,mode", [("WEBP", "RGB"), ("WEBP", "RGBA"), ("PNG", "P")])
def test_webp_and_palette_png_transcode_pixels_and_alpha(tmp_path, format_name, mode):
    if format_name == "WEBP" and not features.check("webp"):
        pytest.skip("Pillow has no WebP codec")
    source, output = tmp_path / f"input.{format_name.lower()}", tmp_path / "converted.jpg"
    if mode == "P":
        image = Image.new("P", (80, 60), 0)
        image.putpalette([255, 0, 255, *COLORS["green"], *([0, 0, 0] * 254)])
        ImageDraw.Draw(image).rectangle((20, 15, 60, 45), fill=1)
        image.save(source, transparency=0)
    else:
        background = (255, 0, 255, 0) if mode == "RGBA" else (255, 255, 255)
        image = Image.new(mode, (80, 60), background)
        fill = (*COLORS["green"], 255) if mode == "RGBA" else COLORS["green"]
        ImageDraw.Draw(image).rectangle((20, 15, 60, 45), fill=fill)
        image.save(source, format=format_name, lossless=True)
    source_bytes = source.read_bytes()

    assert prepare_image(source, output) == {"width": 80, "height": 60}
    with Image.open(output) as converted:
        assert converted.mode == "RGB" and converted.format == "JPEG"
        assert_color(converted.getpixel((40, 30)), COLORS["green"])
        assert_color(converted.getpixel((5, 5)), (255, 255, 255), 5)
    assert source.read_bytes() == source_bytes


@pytest.mark.skipif(not HAS_VIDEO, reason="ffmpeg and ffprobe are required")
def test_real_video_repeated_track_id_yields_one_candidate_from_correct_frame(tmp_path):
    # Three source frames have unique content; no cloud/model substitutes are
    # used. Only deterministic SAM geometry is supplied to the crop selection.
    frames = tmp_path / "frames"
    frames.mkdir()
    for index, color in enumerate(("red", "green", "blue")):
        Image.new("RGB", (100, 80), COLORS[color]).save(frames / f"{index:03}.png")
    source, prepared = tmp_path / "source.mp4", tmp_path / "clip.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-nostdin",
            "-y",
            "-framerate",
            "5",
            "-start_number",
            "0",
            "-i",
            str(frames / "%03d.png"),
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(source),
        ],
        check=True,
        timeout=15,
    )
    metadata = prepare_video(source, prepared, 30)
    assert metadata["frames"] == 3 and metadata["fps"] == 5
    records = [
        {
            "kind": "box",
            "object_id": "same-garment",
            "frame": frame,
            "left": 10,
            "top": 10,
            "right": right,
            "bottom": bottom,
        }
        for frame, right, bottom in [(0, 30, 30), (1, 70, 60), (1, 70, 60), (2, 40, 40)]
    ]
    settings = Settings(data_dir=tmp_path / "state", api_key=None)
    manager = JobManager(settings, Store(settings.data_dir), providers=None)
    directory = tmp_path / "isolated-job"
    directory.mkdir()
    candidates = asyncio.run(manager._video_crops("isolated-job", prepared, records, directory, metadata))

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate["object_id"] == "same-garment" and candidate["frame"] == 1
    assert candidate["bbox"] == [10, 10, 70, 60]
    assert candidate["context_path"].is_file()
    with Image.open(candidate["path"]) as crop:
        assert_color(crop.getpixel((crop.width // 2, crop.height // 2)), COLORS["green"], 20)
    assert not list(directory.glob("frame-*/"))


@pytest.mark.skipif(not HAS_VIDEO, reason="ffmpeg and ffprobe are required")
def test_real_video_thirty_second_boundary_is_not_silently_truncated(tmp_path):
    for duration, accepted in ((30.0, True), (30.2, False)):
        source, output = tmp_path / f"source-{duration}.mp4", tmp_path / f"output-{duration}.mp4"
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
                f"color=c=blue:s=64x48:r=5:d={duration}",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                str(source),
            ],
            check=True,
            timeout=15,
        )
        if accepted:
            metadata = prepare_video(source, output, 30)
            assert metadata["duration"] == pytest.approx(30.0, abs=0.01)
            assert metadata["frames"] == 150
        else:
            with pytest.raises(MediaError, match="Video exceeds 30 seconds"):
                prepare_video(source, output, 30)
            assert not output.exists()


@pytest.mark.skipif(not HAS_VIDEO, reason="ffmpeg and ffprobe are required")
def test_fake_mp4_extension_does_not_bypass_media_validation(tmp_path):
    source, output = tmp_path / "private-name.mp4", tmp_path / "clip.mp4"
    source.write_bytes(b"PRIVATE_INPUT_BYTES: this is text, not a media container")
    with pytest.raises(MediaError) as error:
        prepare_video(source, output, 30)
    assert not output.exists()
    assert "PRIVATE_INPUT_BYTES" not in str(error.value)
    assert "private-name" not in str(error.value)

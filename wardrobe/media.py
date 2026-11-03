"""Validated media conversion, clipped SAM crops, and frame-accurate overlays."""

from __future__ import annotations

import hashlib
import json
import math
import shutil
import subprocess
import tempfile
from collections import defaultdict
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from PIL import Image, ImageDraw, ImageOps, UnidentifiedImageError
from meta_sam_parser import decode_mask_to_raster


MAX_IMAGE_PIXELS = 24_000_000
MAX_MASK_PIXELS = 4_000_000
PALETTE = [
    (209, 83, 65),
    (55, 132, 98),
    (69, 116, 177),
    (180, 114, 25),
    (130, 87, 164),
    (39, 142, 148),
    (176, 82, 134),
    (95, 126, 51),
]


class MediaError(Exception):
    """Safe user-readable media error; never raw ffmpeg or filename content."""


def _get(value: Any, key: str, default: Any = None) -> Any:
    return value.get(key, default) if isinstance(value, dict) else getattr(value, key, default)


def _load_image(source: Path) -> Image.Image:
    try:
        with Image.open(source) as image:
            if image.width <= 0 or image.height <= 0 or image.width * image.height > MAX_IMAGE_PIXELS:
                raise MediaError("Image is too large. Please use an image under 24 megapixels.")
            image.load()
            transposed = ImageOps.exif_transpose(image)
            if transposed.mode in ("RGBA", "LA") or "transparency" in transposed.info:
                rgba = transposed.convert("RGBA")
                background = Image.new("RGBA", rgba.size, "white")
                background.alpha_composite(rgba)
                return background.convert("RGB")
            return transposed.convert("RGB")
    except MediaError:
        raise
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError):
        raise MediaError(
            "Unable to read image. Please use a valid JPEG, PNG, WebP, or other supported image."
        ) from None


def prepare_image(source: Path, destination: Path) -> dict:
    image = _load_image(Path(source))
    image.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
    Path(destination).parent.mkdir(parents=True, exist_ok=True)
    image.save(destination, format="JPEG", quality=92, optimize=True)
    return {"width": image.width, "height": image.height}


def crop_selection(source: Path, person_box: list | tuple, destination: Path) -> dict:
    """Crop a manual region in normalized, EXIF-oriented image coordinates.

    This does not detect or identify a person. The normalized rectangle belongs
    to the displayed image, after orientation correction, not the encoded JPEG
    raster. Keeping this derived file separate preserves the complete source.
    """
    if (
        not isinstance(person_box, (list, tuple))
        or len(person_box) != 4
        or any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or not 0 <= value <= 1
            for value in person_box
        )
    ):
        raise MediaError(
            "Invalid person selection. Please select the complete target person within the photo."
        )
    left, top, right, bottom = person_box
    if left >= right or top >= bottom:
        raise MediaError(
            "Invalid person selection. Please select the complete target person within the photo."
        )
    source, destination = Path(source), Path(destination)
    if source.resolve() == destination.resolve():
        raise MediaError(
            "Person selection must be saved to a new file; the original image cannot be overwritten."
        )
    image = _load_image(source)
    box = (
        math.floor(round(left * image.width, 10)),
        math.floor(round(top * image.height, 10)),
        math.ceil(round(right * image.width, 10)),
        math.ceil(round(bottom * image.height, 10)),
    )
    if box[0] >= box[2] or box[1] >= box[3]:
        raise MediaError("Person selection is too small. Please reselect the target person.")
    selected = image.crop(box)
    destination.parent.mkdir(parents=True, exist_ok=True)
    selected.save(
        destination,
        format="PNG" if destination.suffix.lower() == ".png" else "JPEG",
        quality=94,
    )
    return {
        "width": selected.width,
        "height": selected.height,
        "bbox": list(box),
        "normalized_box": [float(value) for value in person_box],
    }


def _run(arguments: list[str], timeout: float = 60) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(arguments, capture_output=True, check=True, timeout=timeout, text=True)
    except FileNotFoundError:
        raise MediaError("Video processing requires ffmpeg and ffprobe to be installed.") from None
    except subprocess.TimeoutExpired:
        raise MediaError("Video processing timed out. Please use a shorter or smaller video.") from None
    except (subprocess.CalledProcessError, OSError):
        raise MediaError(
            "Unable to process this video. Please use a valid MP4, MOV, WebM, or AVI file."
        ) from None


def _probe(source: Path) -> dict:
    response = _run(
        [
            "ffprobe",
            "-v",
            "error",
            "-protocol_whitelist",
            "file,pipe",
            "-format_whitelist",
            "mov,matroska,webm,avi,mpegts,mpeg",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height,avg_frame_rate,r_frame_rate,nb_frames,duration:format=duration,format_name",
            "-of",
            "json",
            str(source),
        ],
        timeout=15,
    )
    try:
        data = json.loads(response.stdout)
        stream = data["streams"][0]
        width, height = int(stream["width"]), int(stream["height"])
        duration = float(data.get("format", {}).get("duration") or stream.get("duration"))
        frame_rate = stream.get("avg_frame_rate")
        if not frame_rate or frame_rate == "0/0":
            frame_rate = stream["r_frame_rate"]
        fps = float(Fraction(frame_rate))
        if width < 2 or height < 2 or width * height > MAX_IMAGE_PIXELS or max(width, height) > 8192:
            raise MediaError("Video dimensions exceed the limit. Please reduce to 4K or below.")
        if not math.isfinite(duration) or duration <= 0 or not math.isfinite(fps) or fps <= 0 or fps > 1000:
            raise ValueError("Invalid duration or fps")
        count = stream.get("nb_frames")
        frames = int(count) if count and count != "N/A" else math.ceil(duration * fps)
        if frames < 1:
            raise ValueError("No frames")
        return {"width": width, "height": height, "fps": fps, "frames": frames, "duration": duration}
    except MediaError:
        raise
    except (KeyError, IndexError, TypeError, ValueError, ZeroDivisionError, OverflowError):
        raise MediaError("Unable to read video duration, dimensions, or frame information.") from None


def prepare_video(source: Path, destination: Path, max_seconds: int) -> dict:
    source, destination = Path(source), Path(destination)
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise MediaError("Video processing requires ffmpeg and ffprobe to be installed.")
    metadata = _probe(source)
    if metadata["duration"] > max_seconds + 0.01:
        raise MediaError(f"Video exceeds {max_seconds} seconds. Please trim it before uploading.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    _run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-nostdin",
            "-y",
            "-protocol_whitelist",
            "file,pipe",
            "-format_whitelist",
            "mov,matroska,webm,avi,mpegts,mpeg",
            "-i",
            str(source),
            "-map",
            "0:v:0",
            "-an",
            "-sn",
            "-dn",
            "-t",
            str(max_seconds),
            # 360p keeps bounded video SAM mask decoding and uploads practical on a
            # local CPU; still images keep 1600px detail for individual garments.
            "-vf",
            "fps=5,scale=w='min(640,iw)':h='min(360,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2,setsar=1",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-pix_fmt",
            "yuv420p",
            "-map_metadata",
            "-1",
            "-movflags",
            "+faststart",
            str(destination),
        ],
        timeout=max(30, min(120, max_seconds * 4)),
    )
    return _probe(destination)


def video_frame(source: Path, destination: Path, frame: int, fps: float) -> None:
    if (
        isinstance(frame, bool)
        or not isinstance(frame, int)
        or frame < 0
        or not math.isfinite(fps)
        or fps <= 0
    ):
        raise MediaError("Invalid video frame.")
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Select the source index instead of seek rounding; SAM indices are zero based.
    with tempfile.TemporaryDirectory(prefix="frame-", dir=destination.parent) as temporary:
        pending = Path(temporary) / ("frame" + destination.suffix)
        _run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-nostdin",
                "-y",
                "-i",
                str(source),
                "-an",
                "-vf",
                f"select=eq(n\\,{frame})",
                "-frames:v",
                "1",
                str(pending),
            ],
            timeout=30,
        )
        if not pending.is_file() or not pending.stat().st_size:
            raise MediaError("The video does not contain the specified frame.")
        pending.replace(destination)


def _frame(record: Any) -> int | None:
    frame = _get(record, "frame")
    if frame is None:
        return None
    value = _get(frame, "frame_index", frame)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Invalid frame")
    if not math.isfinite(value) or int(value) != value or value < 0:
        raise ValueError("Invalid frame")
    return int(value)


def _groups(records: list, frame: int | None) -> list[dict]:
    grouped: dict[tuple, dict] = {}
    for record in records:
        kind = _get(record, "kind")
        if kind not in ("box", "mask"):
            continue
        try:
            actual_frame = _frame(record)
        except (TypeError, ValueError, OverflowError):
            continue
        # No frame reference means a still-image detection; never stamp it onto
        # every video frame. A supplied index selects exactly that SAM frame.
        if actual_frame != frame:
            continue
        object_id = str(_get(record, "object_id", ""))
        if not object_id:
            continue
        key = (actual_frame, object_id)
        group = grouped.setdefault(key, {"object_id": object_id, "frame": actual_frame})
        group[kind] = record
    return list(grouped.values())


def _bounds(record: Any, width: int, height: int) -> tuple[int, int, int, int] | None:
    geometry = _get(record, "bounds") if _get(record, "kind") == "mask" else record
    try:
        values = [float(_get(geometry, n)) for n in ("left", "top", "right", "bottom")]
        if not all(math.isfinite(v) and abs(v) <= 1_000_000 for v in values):
            return None
        left, top, right, bottom = values
        if right <= left or bottom <= top:
            return None
        clipped = (
            max(0, min(width, math.floor(left))),
            max(0, min(height, math.floor(top))),
            max(0, min(width, math.ceil(right))),
            max(0, min(height, math.ceil(bottom))),
        )
        return clipped if clipped[2] > clipped[0] and clipped[3] > clipped[1] else None
    except (ValueError, TypeError, OverflowError):
        return None


def _placed_mask(record: Any, width: int, height: int) -> Image.Image:
    """Place a box-local raster with clipping on all four sides.

    Bounds are half-open floats; source masks may have a different resolution
    from their geometric bounds. Resize using nearest neighbours before clipping.
    """
    mask = _get(record, "mask")
    if isinstance(mask, dict):
        mask = SimpleNamespace(**mask)
    try:
        mw, mh = _get(mask, "width"), _get(mask, "height")
        if (
            not isinstance(mw, int)
            or isinstance(mw, bool)
            or not isinstance(mh, int)
            or isinstance(mh, bool)
            or mw <= 0
            or mh <= 0
            or mw * mh > MAX_MASK_PIXELS
        ):
            raise ValueError("Invalid mask dimensions")
        bounds = _get(record, "bounds")
        values = [float(_get(bounds, key)) for key in ("left", "top", "right", "bottom")]
        if not all(math.isfinite(v) and abs(v) <= 1_000_000 for v in values):
            raise ValueError("Invalid mask bounds")
        left, top, right, bottom = (
            math.floor(values[0]),
            math.floor(values[1]),
            math.ceil(values[2]),
            math.ceil(values[3]),
        )
        bw, bh = right - left, bottom - top
        if bw <= 0 or bh <= 0 or bw * bh > MAX_MASK_PIXELS:
            raise ValueError("Invalid mask bounds")
        result = Image.new("L", (width, height), 0)
        clip = _bounds(record, width, height)
        if clip is None:
            return result
        raw = decode_mask_to_raster(mask)
        # meta-sam-parser explicitly returns one byte (0/1) per pixel. Packed
        # bit guesses corrupt narrow masks and are intentionally not supported.
        if len(raw) != mw * mh:
            raise ValueError("Invalid raster length")
        local = Image.frombytes("L", (mw, mh), raw).point(lambda value: 255 if value else 0)
        if local.size != (bw, bh):
            local = local.resize((bw, bh), Image.Resampling.NEAREST)
        clipped_left, clipped_top, clipped_right, clipped_bottom = clip
        local = local.crop(
            (clipped_left - left, clipped_top - top, clipped_right - left, clipped_bottom - top)
        )
        result.paste(local, (clipped_left, clipped_top))
        return result
    except Exception:
        raise MediaError("Invalid segmentation mask data; unable to process this mask.") from None


def _group_geometry(group: dict, width: int, height: int):
    mask = None
    if "mask" in group:
        try:
            mask = _placed_mask(group["mask"], width, height)
        except MediaError:
            # A valid SAM box remains useful if its optional mask is corrupt.
            # A mask-only record with invalid data is discarded.
            pass
    box = _bounds(group.get("box"), width, height) if "box" in group else None
    if box is None and mask is not None:
        box = mask.getbbox()
    if box is None:
        return None, None
    if mask is not None and mask.crop(box).getbbox() is None:
        return None, None
    return box, mask


def _checked_box(bbox: list | tuple, width: int, height: int) -> tuple[int, int, int, int]:
    if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
        raise MediaError("Invalid garment bounds; unable to create context image.")
    box = _bounds(dict(zip(("left", "top", "right", "bottom"), bbox)), width, height)
    if box is None:
        raise MediaError("Invalid garment bounds; unable to create context image.")
    return box


def _context_image(
    source: Image.Image, box: tuple[int, int, int, int], *, padding: float = 0.2
) -> Image.Image:
    left, top, right, bottom = box
    padding_x = max(2, math.ceil((right - left) * padding))
    padding_y = max(2, math.ceil((bottom - top) * padding))
    region = (
        max(0, left - padding_x),
        max(0, top - padding_y),
        min(source.width, right + padding_x),
        min(source.height, bottom + padding_y),
    )
    context = source.crop(region)
    # Keep the actual neighbouring pixels: this is context, not another cutout.
    # Only the derived image gets a thin target marker; the source stays intact.
    draw = ImageDraw.Draw(context)
    draw.rectangle(
        (left - region[0], top - region[1], right - region[0] - 1, bottom - region[1] - 1),
        outline=(238, 128, 35),
        width=max(1, min(3, round(max(context.size) / 600))),
    )
    return context


def context_crop(image_path: Path, bbox: list | tuple, out_path: Path, *, padding: float = 0.2) -> Path:
    """Save original pixels with bounded padding and a target-box outline."""
    image_path, out_path = Path(image_path), Path(out_path)
    if image_path.resolve() == out_path.resolve():
        raise MediaError(
            "Context image must be saved to a new file; the original image cannot be overwritten."
        )
    if isinstance(padding, bool) or not isinstance(padding, (int, float)) or not 0 <= padding <= 2:
        raise MediaError("Context padding must be between 0 and 2 times the target bounds.")
    source = _load_image(image_path)
    box = _checked_box(bbox, source.width, source.height)
    context = _context_image(source, box, padding=padding)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    context.save(out_path, format="PNG" if out_path.suffix.lower() == ".png" else "JPEG", quality=94)
    return out_path


def _crop_metrics(size: tuple[int, int], box: tuple[int, int, int, int], mask: Image.Image | None) -> dict:
    source_width, source_height = size
    left, top, right, bottom = box
    width, height = right - left, bottom - top
    area = width * height
    mask_area = None if mask is None else mask.crop(box).histogram()[255]
    # These are geometry observations, not a completeness classifier. A narrow
    # scarf may be fully visible; an almost rectangular blouse may be occluded.
    margin = max(2, round(min(size) * 0.005))
    edges = []
    for name, gap in (
        ("left", left),
        ("top", top),
        ("right", source_width - right),
        ("bottom", source_height - bottom),
    ):
        if gap <= margin:
            edges.append(name)
    return {
        "mask_fill": None if mask_area is None else round(mask_area / area, 6),
        "mask_area": mask_area,
        "mask_fraction": None if mask_area is None else round(mask_area / (source_width * source_height), 6),
        "bbox_area": area,
        "bbox_fraction": round(area / (source_width * source_height), 6),
        "aspect_ratio": round(width / height, 6),
        "source_size": [source_width, source_height],
        "crop_size": [width, height],
        "frame_edges": edges,
        "edge_margin_px": margin,
    }


def crop_metrics(image_path: Path, bbox: list | tuple) -> dict:
    """Geometry for old box-only records; no mask pixels are inferred from JPEG."""
    source = _load_image(Path(image_path))
    return _crop_metrics(source.size, _checked_box(bbox, source.width, source.height), None)


def crop_records(image: Path, records: list, destination_dir: Path, frame: int | None = None) -> list[dict]:
    source = _load_image(Path(image))
    destination_dir = Path(destination_dir)
    destination_dir.mkdir(parents=True, exist_ok=True)
    results = []
    for group in _groups(records, frame):
        box, mask = _group_geometry(group, source.width, source.height)
        if box is None:
            continue
        crop = source.crop(box)
        if mask is not None:
            crop = Image.composite(crop, Image.new("RGB", crop.size, "white"), mask.crop(box))
        identity = f"{frame}:{group['object_id']}".encode("utf-8")
        digest = hashlib.sha256(identity).hexdigest()[:20]
        filename = f"crop-{digest}.jpg"
        path = destination_dir / filename
        crop.save(path, format="JPEG", quality=94)
        context_path = destination_dir / f"context-{digest}.jpg"
        _context_image(source, box).save(context_path, format="JPEG", quality=94)
        results.append(
            {
                "path": path,
                "context_path": context_path,
                "object_id": group["object_id"],
                "bbox": list(box),
                "frame": frame,
                "metrics": _crop_metrics(source.size, box, mask),
            }
        )
    return results


def _overlay(source: Image.Image, records: list, frame: int | None) -> Image.Image:
    result = source.convert("RGB")
    for group in _groups(records, frame):
        box, mask = _group_geometry(group, result.width, result.height)
        if box is None:
            continue
        color_index = int.from_bytes(hashlib.sha256(group["object_id"].encode()).digest()[:2], "big") % len(
            PALETTE
        )
        color = PALETTE[color_index]
        if mask is not None:
            tint = Image.blend(result, Image.new("RGB", result.size, color), 0.35)
            result = Image.composite(tint, result, mask)
        draw = ImageDraw.Draw(result)
        left, top, right, bottom = box
        draw.rectangle((left, top, right - 1, bottom - 1), outline=color, width=3)
        # Object IDs are untrusted provider text and may be non-ASCII. A stable
        # short hash avoids font errors and has the same color on every frame.
        label = "#" + hashlib.sha256(group["object_id"].encode()).hexdigest()[:5]
        x, y = min(left + 4, max(0, result.width - 45)), min(top + 4, max(0, result.height - 14))
        draw.rectangle((x, y, min(x + 44, result.width - 1), min(y + 13, result.height - 1)), fill=color)
        draw.text((x + 2, y + 1), label, fill="white")
    return result


def render_overlay(image: Path, records: list, destination: Path, frame: int | None = None) -> None:
    result = _overlay(_load_image(Path(image)), records, frame)
    Path(destination).parent.mkdir(parents=True, exist_ok=True)
    result.save(destination, format="PNG" if Path(destination).suffix.lower() == ".png" else "JPEG")


def render_video_overlay(source: Path, records: list, destination: Path, metadata: dict) -> None:
    source, destination = Path(source), Path(destination)
    try:
        fps, expected_frames = float(metadata["fps"]), int(metadata["frames"])
        duration = float(metadata["duration"])
        if (
            not math.isfinite(fps)
            or not math.isfinite(duration)
            or fps <= 0
            or duration <= 0
            or expected_frames < 1
        ):
            raise ValueError("Invalid video metadata")
        if expected_frames > 1800 or duration > 120:
            raise ValueError("Video is not normalized")
    except (ValueError, TypeError, KeyError, OverflowError):
        raise MediaError("Invalid video preview metadata.") from None
    by_frame: dict[int, list] = defaultdict(list)
    for record in records:
        try:
            frame = _frame(record)
            if frame is not None and 0 <= frame < expected_frames:
                by_frame[frame].append(record)
        except (TypeError, ValueError, OverflowError):
            continue
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Temporary frames remain scoped to this job and are removed on failure.
    with tempfile.TemporaryDirectory(prefix="overlay-", dir=destination.parent) as temporary:
        directory = Path(temporary)
        _run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-nostdin",
                "-y",
                "-i",
                str(source),
                "-map",
                "0:v:0",
                "-an",
                "-fps_mode",
                "passthrough",
                "-frames:v",
                str(expected_frames + 1),
                "-start_number",
                "0",
                str(directory / "%05d.png"),
            ],
            timeout=min(120, max(30, duration * 3)),
        )
        frames = sorted(directory.glob("*.png"))
        if not frames or len(frames) > expected_frames + 1:
            raise MediaError("Unable to create video preview frames.")
        for frame_index, path in enumerate(frames):
            image = _load_image(path)
            _overlay(image, by_frame.get(frame_index, []), frame_index).save(path, format="PNG")
        _run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-nostdin",
                "-y",
                "-framerate",
                str(fps),
                "-start_number",
                "0",
                "-i",
                str(directory / "%05d.png"),
                "-an",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                str(destination),
            ],
            timeout=min(120, max(30, duration * 3)),
        )

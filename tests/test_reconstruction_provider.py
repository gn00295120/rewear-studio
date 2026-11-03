"""Real SDK serialization and failure boundaries for reference-guided editing."""

import asyncio
import base64
import copy
import io
import json
import threading
from pathlib import Path

import httpx
import pytest
from openai import AsyncOpenAI
from PIL import Image

import wardrobe.providers as module
from wardrobe.config import Settings
from wardrobe.providers import ProviderError, Providers
from wardrobe.reconstruction_quality import CHECK_NAMES


@pytest.fixture
def record():
    return {
        "category": "outerwear",
        "subtype": "denim jacket",
        "brand": "unverified-private-brand",
        "size_label": "M",
        "colors": ["blue"],
        "pattern": None,
        "material_guess": "denim",
        "condition": "unknown",
        "defects": [],
        "confidence": 0.6,
        "needs_review": False,
    }


@pytest.fixture
def references(tmp_path):
    cutout, context = tmp_path / "cutout.png", tmp_path / "context.png"
    Image.new("RGB", (17, 23), "blue").save(cutout)
    Image.new("RGB", (37, 41), "red").save(context)
    return cutout, context


def png_bytes(color="green"):
    image = io.BytesIO()
    Image.new("RGB", (43, 47), color).save(image, format="PNG")
    return image.getvalue()


def reference_pixel(reference):
    data = base64.b64decode(reference["image_url"].split(",", 1)[1])
    with Image.open(io.BytesIO(data)) as image:
        return image.getpixel((0, 0))


def image_response():
    return {"created": 1, "data": [{"b64_json": base64.b64encode(png_bytes()).decode()}]}


def provider_with_transport(tmp_path, handler, timeout=2):
    settings = Settings(
        data_dir=tmp_path,
        api_key="test-secret-key",
        request_timeout=timeout,
        reconstruction_timeout=timeout,
    )
    provider = Providers(settings)

    async def route(request):
        # These tests exercise edit transport/storage boundaries. The dedicated
        # quality suite inspects all real SDK plan/edit/fidelity request bodies.
        if request.url.path == "/v1/chat/completions":
            name = json.loads(request.content)["response_format"]["json_schema"]["name"]
            if name == "reconstruction_plan":
                value = {
                    "target": "one_garment",
                    "source_view": "front",
                    "view_evidence": "Front closure visible.",
                    "garment_type": "jacket",
                    "visibility": "partial",
                    "primary_front_state": {
                        "state": "not_applicable",
                        "source_image": "context"
                        if any(
                            block.get("text") == "ORIGINAL_CONTEXT"
                            for block in json.loads(request.content)["messages"][1]["content"]
                        )
                        else "cutout",
                        "evidence": "Synthetic solid-colour transport fixture has no front opening.",
                        "box": None,
                    },
                    "visible_features": [{"feature": "colour", "detail": "Blue fabric."}],
                    "uncertain_areas": [],
                    "detail_regions": [],
                }
            else:
                value = {
                    "observed_view": "front",
                    "candidate_front_state": {
                        "state": "not_applicable",
                        "evidence": "Synthetic solid-colour transport fixture has no front opening.",
                        "box": None,
                    },
                    "checks": {
                        key: {"status": "pass", "reason": "Synthetic transport-test answer."}
                        for key in CHECK_NAMES
                    },
                    "feature_checks": [
                        {"feature": "colour", "status": "pass", "reason": "Synthetic answer."}
                    ],
                    "corrections": [],
                    "region_boxes": [],
                    "preview_observations": {
                        "category": {"status": "pass", "reason": "Synthetic same category."},
                        "coarse_color": {"status": "pass", "reason": "Synthetic same dominant colour."},
                    },
                }
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "stop",
                            "message": {"role": "assistant", "content": json.dumps(value)},
                        }
                    ]
                },
            )
        value = handler(request)
        return await value if asyncio.iscoroutine(value) else value

    provider._meta = AsyncOpenAI(
        base_url=settings.meta_url,
        api_key=settings.api_key,
        max_retries=0,
        timeout=timeout,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(route)),
    )
    return provider


@pytest.mark.parametrize("with_context", [False, True])
async def test_sdk_sends_actual_ordered_reference_images_to_edit_endpoint(
    tmp_path, references, record, with_context
):
    cutout, context = references
    before = [path.read_bytes() for path in references]
    original_record = copy.deepcopy(record)
    calls = []

    def respond(request):
        calls.append(request)
        assert request.method == "POST" and request.url.path == "/v1/images/edits"
        assert request.headers["content-type"] == "application/json"
        body = json.loads(request.content)
        assert body["model"] == "muse-image-1.0" and body["n"] == 1
        assert body["output_format"] == "png" and body["response_format"] == "b64_json"
        assert body["tool_enablement"] == {
            "enable_image_search": False,
            "enable_web_search": False,
            "enable_shell": False,
        }
        assert "untrusted" in body["prompt"] and "same target" in body["prompt"].lower()
        assert "unverified-private-brand" not in body["prompt"]
        assert len(body["images"]) == (2 if with_context else 1)
        expected = [((17, 23), (0, 0, 255)), ((37, 41), (255, 0, 0))]
        for reference, (size, color) in zip(body["images"], expected):
            prefix, encoded = reference["image_url"].split(",", 1)
            assert prefix == "data:image/png;base64"
            with Image.open(io.BytesIO(base64.b64decode(encoded, validate=True))) as image:
                assert image.size == size and image.getpixel((0, 0)) == color
        return httpx.Response(200, json=image_response())

    provider = provider_with_transport(tmp_path, respond)
    output = tmp_path / "derived" / "reconstructed.png"
    try:
        await provider.reconstruct(cutout, context if with_context else None, record, output)
    finally:
        await provider.close()
    assert len(calls) == 1
    assert [path.read_bytes() for path in references] == before
    assert record == original_record
    with Image.open(output) as image:
        assert image.format == "PNG" and image.size == (43, 47)
        assert image.getpixel((0, 0)) == (0, 128, 0)


async def test_affordable_preview_returns_three_images_in_one_flat_price_request(
    tmp_path, references, record
):
    cutout, context = references
    calls = []
    colors = ("red", "green", "blue")

    def respond(request):
        calls.append(request)
        assert request.url.path == "/v1/images/edits"
        body = json.loads(request.content)
        assert body["n"] == 3 and len(body["images"]) == 2
        assert [reference_pixel(reference) for reference in body["images"]] == [
            (255, 0, 0),
            (0, 0, 255),
        ]
        assert "affordable preview path" in body["prompt"]
        assert "When ORIGINAL_CONTEXT is present it is Image 1" in body["prompt"]
        assert "A Reference manifest included later in this request is authoritative" in body["prompt"]
        assert "Only when no Reference manifest is supplied" in body["prompt"]
        assert "Never trace white, transparent, skin, arm, hair, bag" in body["prompt"]
        assert '"image_number": 1, "role": "ORIGINAL_CONTEXT", "source_id": "context"' in body["prompt"]
        assert '"image_number": 2, "role": "TARGET_LOCATOR", "source_id": "cutout"' in body["prompt"]
        assert "never turn its cells into flowers" in body["prompt"]
        assert "Do not remove a visible pocket" in body["prompt"]
        assert "include a hood only when the source visibly establishes" in body["prompt"]
        assert "partial strokes" in body["prompt"]
        assert "At least one result must use the simplest completion" in body["prompt"]
        assert "Bare forearm visible between a folded sleeve edge" in body["prompt"]
        assert "do not extend it to the wrist" in body["prompt"]
        assert "Gloves, arm warmers, bracelets, scarves and shawls" in body["prompt"]
        assert "never graft their knit" in body["prompt"]
        assert "A V-shaped area of skin or background" in body["prompt"]
        assert "keep target fabric continuous below that point" in body["prompt"]
        assert "Only visibly separated garment edges continuing down the torso" in body["prompt"]
        assert "Plain, opaque fabric must remain plain and opaque" in body["prompt"]
        assert "not a hairline opening or panels that nearly meet" in body["prompt"]
        assert "Never fuse an outer vest or jacket with an under-shirt" in body["prompt"]
        assert "unverified-private-brand" not in body["prompt"]
        return httpx.Response(
            200,
            json={
                "created": 1,
                "usage": {"input_tokens": 40_000, "output_tokens": 2_000},
                "data": [{"b64_json": base64.b64encode(png_bytes(color)).decode()} for color in colors],
            },
        )

    provider = provider_with_transport(tmp_path, respond)
    usage = []
    provider.bind_usage_recorder(usage.append)
    outputs = [tmp_path / "derived" / f"choice-{number}.png" for number in (1, 2, 3)]
    try:
        result = await provider.reconstruct_preview(cutout, context, record, outputs)
    finally:
        await provider.close()
    assert len(calls) == 1 and result == {"reference_manifest": None, "candidate_count": 3}
    assert [Image.open(path).getpixel((0, 0)) for path in outputs] == [
        (255, 0, 0),
        (0, 128, 0),
        (0, 0, 255),
    ]
    assert len(usage) == 1
    assert usage[0]["operation"] == "reconstruction.preview_variants"
    assert usage[0]["generated_images"] == 3 and usage[0]["estimated_usd"] == 0.03


def test_reference_normalization_keeps_exif_orientation_and_transparency(tmp_path):
    path = tmp_path / "oriented.png"
    source = Image.new("RGBA", (3, 2), (0, 0, 0, 0))
    source.putpixel((0, 0), (255, 0, 0, 255))
    exif = source.getexif()
    exif[274] = 6
    source.save(path, exif=exif)
    encoded = module._reference_data_url(path).split(",", 1)[1]
    with Image.open(io.BytesIO(base64.b64decode(encoded))) as image:
        assert image.size == (2, 3)
        assert image.getpixel((1, 0)) == (255, 0, 0)
        assert image.getpixel((0, 0)) == (255, 255, 255)


@pytest.mark.parametrize("invalid", ["missing-cutout", "missing-context", "broken", "record", "oversize"])
async def test_invalid_reference_or_metadata_does_not_call_model_or_replace_output(
    tmp_path, references, record, invalid, monkeypatch
):
    cutout, context = references
    if invalid == "missing-cutout":
        cutout = tmp_path / "absent.png"
    elif invalid == "missing-context":
        context = tmp_path / "absent.png"
    elif invalid == "broken":
        cutout.write_bytes(b"test-secret-key: not an image")
    elif invalid == "record":
        record["confidence"] = float("nan")
    else:
        monkeypatch.setattr(module, "_MAX_IMAGE_PIXELS", 300)
    calls = []
    provider = provider_with_transport(tmp_path, lambda request: calls.append(request))
    output = tmp_path / "existing.png"
    output.write_bytes(b"existing derived result")
    try:
        with pytest.raises(ProviderError) as caught:
            await provider.reconstruct(cutout, context, record, output)
        assert "test-secret-key" not in str(caught.value)
    finally:
        await provider.close()
    assert calls == [] and output.read_bytes() == b"existing derived result"


@pytest.mark.parametrize("alias", ["cutout", "context", "symlink", "hardlink"])
async def test_reconstruction_never_overwrites_reference_aliases(tmp_path, references, record, alias):
    cutout, context = references
    before = cutout.read_bytes(), context.read_bytes()
    output = cutout if alias == "cutout" else context
    if alias in ("symlink", "hardlink"):
        output = tmp_path / "alias.png"
        if alias == "symlink":
            output.symlink_to(cutout)
        else:
            output.hardlink_to(cutout)
    calls = []
    provider = provider_with_transport(tmp_path, lambda request: calls.append(request))
    try:
        with pytest.raises(ProviderError, match="cannot overwrite"):
            await provider.reconstruct(cutout, context, record, output)
    finally:
        await provider.close()
    assert calls == [] and (cutout.read_bytes(), context.read_bytes()) == before


@pytest.mark.parametrize(
    "status,match", [(400, "rejected"), (401, "access denied"), (429, "quota"), (500, "temporarily")]
)
async def test_rejected_edit_is_redacted_without_generation_fallback(
    tmp_path, references, record, status, match
):
    calls = []

    def reject(request):
        calls.append(request.url.path)
        return httpx.Response(status, json={"error": {"message": "test-secret-key private-image-data"}})

    provider = provider_with_transport(tmp_path, reject)
    output = tmp_path / "reconstructed.png"
    try:
        with pytest.raises(ProviderError, match=match) as caught:
            await provider.reconstruct(*references, record, output)
        assert "test-secret-key" not in str(caught.value)
        assert "private-image-data" not in str(caught.value)
    finally:
        await provider.close()
    assert calls == ["/v1/images/edits"] and not output.exists()


@pytest.mark.parametrize(
    "response",
    [
        {"created": 1, "data": []},
        {"created": 1, "data": [{"b64_json": "invalid!"}]},
        {"created": 1, "data": [{"b64_json": base64.b64encode(b"truncated image").decode()}]},
        {"created": 1, "data": [{"url": "https://127.0.0.1/private-image-data"}]},
    ],
)
async def test_unusable_response_preserves_existing_output(tmp_path, references, record, response):
    provider = provider_with_transport(tmp_path, lambda request: httpx.Response(200, json=response))
    output = tmp_path / "existing.png"
    output.write_bytes(b"existing derived result")
    try:
        with pytest.raises(ProviderError):
            await provider.reconstruct(*references, record, output)
    finally:
        await provider.close()
    assert output.read_bytes() == b"existing derived result"
    assert not list(tmp_path.glob(".image-*"))


@pytest.mark.parametrize("cancel", [False, True])
async def test_edit_deadline_and_cancellation_stop_waiting_for_upstream(tmp_path, references, record, cancel):
    entered = asyncio.Event()
    stopped = asyncio.Event()

    async def stall(request):
        entered.set()
        try:
            await asyncio.sleep(30)
        finally:
            stopped.set()

    provider = provider_with_transport(tmp_path, stall, timeout=0.15 if not cancel else 2)
    output = tmp_path / "reconstructed.png"
    try:
        task = asyncio.create_task(provider.reconstruct(*references, record, output))
        await asyncio.wait_for(entered.wait(), timeout=1)
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(ProviderError, match="timed out"):
                await task
        assert stopped.is_set() and not output.exists()
    finally:
        await provider.close()


def test_failed_atomic_save_leaves_previous_image_intact(tmp_path, monkeypatch):
    output = tmp_path / "existing.png"
    output.write_bytes(b"previous image")
    data = png_bytes()
    writes = []

    def failed_save(image, filename, **kwargs):
        writes.append(Path(filename))
        Path(filename).write_bytes(b"partial write")
        raise OSError("private-path and test-secret-key")

    monkeypatch.setattr(Image.Image, "save", failed_save)
    with pytest.raises(ProviderError) as caught:
        module._save_generated(data, output)
    assert len(writes) == 1 and writes[0] != output
    assert "private-path" not in str(caught.value)
    assert output.read_bytes() == b"previous image"
    assert not list(tmp_path.glob(".image-*"))


async def test_cancellation_waits_for_atomic_writer_before_returning(
    tmp_path, references, record, monkeypatch
):
    entered, release = threading.Event(), threading.Event()
    actual_save = module._save_generated

    def slow_save(data, destination):
        entered.set()
        assert release.wait(timeout=2)
        actual_save(data, destination)

    monkeypatch.setattr(module, "_save_generated", slow_save)
    provider = provider_with_transport(tmp_path, lambda request: httpx.Response(200, json=image_response()))
    output = tmp_path / "reconstructed.png"
    try:
        task = asyncio.create_task(provider.reconstruct(*references, record, output))
        assert await asyncio.to_thread(entered.wait, 1)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not output.exists()
        assert not list(tmp_path.glob(".reconstruction-*"))
        assert not list(tmp_path.glob(".image-*"))
    finally:
        release.set()
        await provider.close()

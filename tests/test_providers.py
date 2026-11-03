import asyncio
import base64
import io
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from PIL import Image
from meta_sam_parser import FrameReference, ResponsesStreamRefusalError

import wardrobe.providers as module
from wardrobe.providers import ProviderError, Providers, _parse_inspection, _parse_record, normalize_records


def settings(tmp_path, **changes):
    data = dict(
        data_dir=tmp_path,
        ollama_url="http://127.0.0.1:11434",
        ollama_model="muse-glimmer:30b",
        meta_url="https://api.meta.ai/v1",
        api_key="test-private-key",
        request_timeout=2,
        max_upload_mb=50,
        max_video_seconds=30,
        max_items=24,
    )
    data.update(changes)
    return SimpleNamespace(**data)


@pytest.fixture
def record():
    return {
        "category": "top",
        "subtype": "t-shirt",
        "brand": None,
        "size_label": None,
        "colors": ["blue"],
        "pattern": "solid",
        "material_guess": None,
        "condition": "unknown",
        "defects": [],
        "confidence": 0.6,
        "needs_review": False,
    }


def test_json_fence_and_confidence_force_review(record):
    assert _parse_record("```json\n" + json.dumps(record) + "\n```")["needs_review"] is True


def test_glimmer_known_terminal_token_is_handled_without_ignoring_prose(record):
    assert _parse_record(json.dumps(record) + "<|eot|>\n")["category"] == "top"
    with pytest.raises(ProviderError):
        _parse_record(json.dumps(record) + "arbitrary model continuation")


@pytest.mark.parametrize(
    "mutation",
    [
        lambda r: r.pop("brand"),
        lambda r: r.update(confidence=1.2),
        lambda r: r.update(confidence=True),
        lambda r: r.update(confidence=float("nan")),
        lambda r: r.update(category="invented"),
        lambda r: r.update(colors="blue"),
        lambda r: r.update(unexpected="extra"),
    ],
)
def test_rejects_invalid_garment_values(record, mutation):
    mutation(record)
    with pytest.raises(ProviderError):
        _parse_record(json.dumps(record))


@pytest.mark.parametrize("content", ["[]", "null", "{broken}", "{} followed by invented commentary", None])
def test_rejects_non_object_or_malformed_output(content):
    with pytest.raises(ProviderError):
        _parse_record(content)


def test_local_native_request_and_safe_errors(tmp_path, record):
    path = tmp_path / "input.jpg"
    path.write_bytes(b"test-image")

    async def run():
        provider = Providers(settings(tmp_path))
        await provider._http.aclose()

        def success(request):
            payload = json.loads(request.content)
            assert request.url.path == "/api/chat"
            assert payload["stream"] is False and payload["think"] is False
            assert payload["format"]["properties"]["confidence"]["maximum"] == 1
            assert payload["messages"][-1]["images"] == [base64.b64encode(b"test-image").decode()]
            return httpx.Response(200, json={"message": {"content": json.dumps(record)}})

        provider._http = httpx.AsyncClient(transport=httpx.MockTransport(success))
        assert (await provider.extract(path, "local"))["category"] == "top"
        await provider._http.aclose()
        provider._http = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(401, text="test-private-key and a private payload")
            )
        )
        with pytest.raises(ProviderError, match="access denied") as error:
            await provider.extract(path, "local")
        assert "test-private-key" not in str(error.value)
        await provider._http.aclose()
        provider._http = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    402, json={"error": {"code": "billing_not_configured", "message": "team_id=private"}}
                )
            )
        )
        with pytest.raises(ProviderError, match="billing is not set up") as error:
            await provider.extract(path, "local")
        assert "team_id" not in str(error.value)
        await provider.close()

    asyncio.run(run())


def test_wall_clock_timeout_and_cancellation(tmp_path):
    path = tmp_path / "input.jpg"
    path.write_bytes(b"test-image")

    async def run():
        provider = Providers(settings(tmp_path, request_timeout=0.02))
        await provider._http.aclose()

        async def stalled(request):
            await asyncio.sleep(30)

        provider._http = httpx.AsyncClient(transport=httpx.MockTransport(stalled))
        with pytest.raises(ProviderError, match="timed out"):
            await provider.extract(path, "local")
        task = asyncio.create_task(provider.extract(path, "local"))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await provider.close()

    asyncio.run(run())


def test_spark_reserves_budget_for_reasoning_and_validates_the_record(tmp_path, record):
    path = tmp_path / "image.jpg"
    path.write_bytes(b"image")

    async def run():
        provider = Providers(settings(tmp_path))
        create = AsyncMock(
            return_value=SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        finish_reason="stop",
                        message=SimpleNamespace(content=json.dumps(record), refusal=None),
                    )
                ]
            )
        )
        provider._meta = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create)), close=AsyncMock()
        )
        assert (await provider.extract(path, "meta"))["category"] == "top"
        assert create.call_args.kwargs["reasoning_effort"] == "minimal"
        assert create.call_args.kwargs["max_tokens"] >= 2048
        create.return_value.choices[0].finish_reason = "length"
        with pytest.raises(ProviderError, match="length limit"):
            await provider.extract(path, "meta")
        await provider.close()

    asyncio.run(run())


@pytest.mark.parametrize("visibility", ["partial", "fragment", "unclear"])
def test_inspection_preserves_visibility_and_requires_review(record, visibility):
    record.update(confidence=0.92, needs_review=False)
    result = _parse_inspection(
        json.dumps(
            {
                "garment": record,
                "target": "one_garment",
                "visibility": visibility,
                "reason": "Lower edge of garment is cropped",
            }
        )
    )
    assert result["visibility"] == visibility
    assert result["garment"]["needs_review"] is True
    assert result["reason"] == "Lower edge of garment is cropped"


def test_unknown_material_does_not_change_complete_visibility(record):
    record.update(material_guess=None, confidence=0.9, needs_review=False)
    result = _parse_inspection(
        json.dumps({"garment": record, "target": "one_garment", "visibility": "complete", "reason": None})
    )
    assert result["visibility"] == "complete" and result["garment"]["material_guess"] is None
    assert result["garment"]["needs_review"] is False


@pytest.mark.parametrize("target", ["multiple_garments", "no_garment", "uncertain"])
def test_non_single_target_forces_review_even_when_complete_and_confident(record, target):
    record.update(confidence=0.99, needs_review=False)
    result = _parse_inspection(
        json.dumps(
            {
                "garment": record,
                "target": target,
                "visibility": "complete",
                "reason": "  Not a confirmed single garment  ",
            }
        )
    )
    assert result["target"] == target and result["visibility"] == "complete"
    assert result["garment"]["confidence"] == 0.99
    assert result["garment"]["needs_review"] is True
    assert result["reason"] == "Not a confirmed single garment"


@pytest.mark.parametrize("target", ["multiple_garments", "no_garment", "uncertain"])
def test_non_single_target_requires_a_reason(record, target):
    with pytest.raises(ProviderError):
        _parse_inspection(
            json.dumps({"garment": record, "target": target, "visibility": "complete", "reason": None})
        )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda d: d.update(visibility="confident"),
        lambda d: d.update(visibility={"complete": True}),
        lambda d: d.pop("garment"),
        lambda d: d.pop("target"),
        lambda d: d.update(target="single"),
        lambda d: d.update(target={"one_garment": True}),
        lambda d: d.update(target=True),
        lambda d: d.update(target=None),
        lambda d: d.pop("reason"),
        lambda d: d.update(reason="x" * 501),
        lambda d: d.update(reason="   "),
        lambda d: d.update(reason=42),
        lambda d: d["garment"].update(confidence=1.5),
        lambda d: d.update(extra="field"),
    ],
)
def test_inspection_rejects_malformed_schema(record, mutation):
    data = {"garment": record, "target": "one_garment", "visibility": "complete", "reason": None}
    mutation(data)
    with pytest.raises(ProviderError):
        _parse_inspection(json.dumps(data))


@pytest.mark.parametrize("provider_name", ["local", "meta"])
def test_inspection_sends_cutout_then_context_in_one_real_request(tmp_path, record, provider_name):
    cutout, context = tmp_path / "cutout.jpg", tmp_path / "context.jpg"
    cutout.write_bytes(b"target-visible-pixels")
    context.write_bytes(b"original-context-with-target-box")
    expected = [base64.b64encode(path.read_bytes()).decode() for path in (cutout, context)]
    response = json.dumps(
        {
            "garment": record,
            "target": "one_garment",
            "visibility": "fragment",
            "reason": "Only a narrow strip of partially obscured fabric is visible",
        }
    )

    async def run():
        provider = Providers(settings(tmp_path))
        calls = []
        if provider_name == "local":
            await provider._http.aclose()

            def respond(request):
                body = json.loads(request.content)
                calls.append(body)
                assert body["messages"][-1]["images"] == expected
                assert body["format"]["required"] == ["garment", "target", "visibility", "reason"]
                assert "not proof of the garment's true outline" in body["messages"][0]["content"]
                return httpx.Response(200, json={"message": {"content": response}})

            provider._http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        else:

            async def create(**body):
                calls.append(body)
                images = body["messages"][-1]["content"][1:]
                assert [x["image_url"]["url"].split(",", 1)[1] for x in images] == expected
                assert body["response_format"]["json_schema"]["schema"]["required"] == [
                    "garment",
                    "target",
                    "visibility",
                    "reason",
                ]
                return SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            finish_reason="stop", message=SimpleNamespace(content=response, refusal=None)
                        )
                    ]
                )

            provider._meta = SimpleNamespace(
                chat=SimpleNamespace(completions=SimpleNamespace(create=create)), close=AsyncMock()
            )
        result = await provider.inspect(cutout, provider_name, context_path=context)
        assert len(calls) == 1
        schema = (
            calls[0]["format"]
            if provider_name == "local"
            else calls[0]["response_format"]["json_schema"]["schema"]
        )
        assert schema["properties"]["target"] == {
            "type": "string",
            "enum": ["one_garment", "multiple_garments", "no_garment", "uncertain"],
        }
        assert result["target"] == "one_garment"
        assert result["visibility"] == "fragment" and result["garment"]["needs_review"] is True
        await provider.close()

    asyncio.run(run())


def test_single_photo_inspection_does_not_describe_a_mask(tmp_path, record):
    path = tmp_path / "photo.jpg"
    path.write_bytes(b"original-photo")

    async def run():
        provider = Providers(settings(tmp_path))
        await provider._http.aclose()

        def respond(request):
            body = json.loads(request.content)
            message = body["messages"][-1]
            assert len(message["images"]) == 1 and "single original photo" in message["content"]
            return httpx.Response(
                200,
                json={
                    "message": {
                        "content": json.dumps(
                            {
                                "garment": record,
                                "target": "one_garment",
                                "visibility": "complete",
                                "reason": None,
                            }
                        )
                    }
                },
            )

        provider._http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        assert (await provider.inspect(path, "local"))["visibility"] == "complete"
        await provider.close()

    asyncio.run(run())


def box(oid="1", frame=None, left=0):
    return SimpleNamespace(kind="box", object_id=oid, frame=frame, left=left, top=0, right=10, bottom=10)


def test_semantic_revision_dedup_and_frame_normalization():
    records = normalize_records(
        [
            box(frame=FrameReference(2)),
            box(frame=FrameReference(2), left=4),
            box(frame=FrameReference(3)),
            box(oid="outside", frame=-1),
            box(oid="invalid", left=float("nan")),
        ]
    )
    assert len(records) == 2
    assert records[0]["left"] == 4 and records[0]["frame"] == 2
    assert records[1]["frame"] == 3


class Parsed:
    def __init__(self, snapshots, final, error=None):
        self.snapshots, self.final, self.error = snapshots, final, error
        self.aclose = AsyncMock()

    async def __aiter__(self):
        for records in self.snapshots:
            yield SimpleNamespace(records=records)
        if self.error:
            raise self.error

    async def final_result(self):
        return self.final


@pytest.mark.parametrize("status,expect_error", [("completed", False), ("incomplete", True)])
def test_final_stream_is_authoritative_and_partial_is_preserved(tmp_path, monkeypatch, status, expect_error):
    path = tmp_path / "input.jpg"
    path.write_bytes(b"image")
    parsed = Parsed(
        [[box("stale"), box("1")], [box("1", left=2)]],
        SimpleNamespace(records=[box("1", left=4)], outcome=SimpleNamespace(status=status), diagnostics=[]),
    )
    monkeypatch.setattr(module, "parse_responses_stream", lambda stream, format: parsed)

    async def run():
        provider = Providers(settings(tmp_path))
        stream = SimpleNamespace(close=AsyncMock())
        provider._meta = SimpleNamespace(
            responses=SimpleNamespace(create=AsyncMock(return_value=stream)), close=AsyncMock()
        )
        if expect_error:
            with pytest.raises(ProviderError) as error:
                await provider.segment(path, "garment")
            records = error.value.records
        else:
            records = await provider.segment(path, "garment")
        assert len(records) == 1 and records[0]["object_id"] == "1" and records[0]["left"] == 4
        parsed.aclose.assert_awaited()
        stream.close.assert_awaited()
        await provider.close()

    asyncio.run(run())


def test_stream_refusal_does_not_expose_payload_and_keeps_partials(tmp_path, monkeypatch):
    path = tmp_path / "input.jpg"
    path.write_bytes(b"image")
    parsed = Parsed([[box()]], None, ResponsesStreamRefusalError("test-private-key", {"raw": "secret"}))
    monkeypatch.setattr(module, "parse_responses_stream", lambda stream, format: parsed)

    async def run():
        provider = Providers(settings(tmp_path))
        provider._meta = SimpleNamespace(
            responses=SimpleNamespace(create=AsyncMock(return_value=SimpleNamespace(close=AsyncMock()))),
            close=AsyncMock(),
        )
        with pytest.raises(ProviderError, match="refused") as error:
            await provider.segment(path, "shirt")
        assert len(error.value.records) == 1
        assert "test-private-key" not in str(error.value)
        await provider.close()

    asyncio.run(run())


def test_sam_timeout_keeps_current_snapshot_and_closes_stream(tmp_path, monkeypatch):
    path = tmp_path / "input.jpg"
    path.write_bytes(b"image")

    class Stalled(Parsed):
        async def __aiter__(self):
            yield SimpleNamespace(records=[box()])
            await asyncio.sleep(30)

    parsed = Stalled([], None)
    monkeypatch.setattr(module, "parse_responses_stream", lambda stream, format: parsed)

    async def run():
        provider = Providers(settings(tmp_path, request_timeout=0.02))
        close = AsyncMock()
        provider._meta = SimpleNamespace(
            responses=SimpleNamespace(create=AsyncMock(return_value=SimpleNamespace(close=close))),
            close=AsyncMock(),
        )
        with pytest.raises(ProviderError, match="timed out") as error:
            await provider.segment(path, "garment")
        assert len(error.value.records) == 1
        close.assert_awaited()
        await provider.close()

    asyncio.run(run())


def test_health_distinguishes_connected_from_model_installed(tmp_path):
    async def run():
        provider = Providers(settings(tmp_path, api_key=None))
        await provider._http.aclose()
        provider._http = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"models": [{"name": "another-model"}]})
            )
        )
        result = await provider.health()
        assert result["local"]["available"] is False
        assert result["meta"]["configured"] is False
        assert "not installed" in result["local"]["error"]
        with pytest.raises(ProviderError, match="not configured"):
            provider._cloud()
        await provider.close()

    asyncio.run(run())


def test_generated_image_is_validated_before_writing(tmp_path):
    async def run():
        provider = Providers(settings(tmp_path))
        payload = io.BytesIO()
        Image.new("RGB", (10, 12), "blue").save(payload, format="PNG")
        generate = AsyncMock(
            return_value=SimpleNamespace(
                data=[SimpleNamespace(b64_json=base64.b64encode(payload.getvalue()).decode())]
            )
        )
        provider._meta = SimpleNamespace(images=SimpleNamespace(generate=generate), close=AsyncMock())
        output = tmp_path / "generated.png"
        await provider.generate("a blue shirt", output)
        with Image.open(output) as image:
            assert image.size == (10, 12) and image.format == "PNG"
        generate.return_value = SimpleNamespace(
            data=[SimpleNamespace(b64_json=base64.b64encode(b"not an image").decode())]
        )
        with pytest.raises(ProviderError):
            await provider.generate("a blue shirt", tmp_path / "invalid.png")
        assert not (tmp_path / "invalid.png").exists()
        await provider.close()

    asyncio.run(run())


def test_generated_download_rejects_local_urls(tmp_path):
    async def run():
        provider = Providers(settings(tmp_path))
        for url in (
            "http://127.0.0.1/image",
            "https://127.0.0.1/image",
            "https://user:secret@example.com/image",
        ):
            with pytest.raises(ProviderError, match="URL"):
                await provider._download_image(url)
        await provider.close()

    asyncio.run(run())

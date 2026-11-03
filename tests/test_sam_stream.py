"""Real SDK/parser lifecycle tests over local SSE, with no model/network calls."""

import asyncio
import gc
import json
import sys
from contextlib import contextmanager
from types import SimpleNamespace

import openai._base_client as sdk_client
import pytest
from meta_sam_parser._mask_codec import _encode_raster
from openai import AsyncOpenAI
from PIL import Image

from wardrobe.providers import ProviderError, Providers

# OpenAI 3 uses httpx2; earlier supported SDK versions use httpx.
sdk_http = getattr(sdk_client, "httpx2", None) or sdk_client.httpx
MASK = _encode_raster([1] * 16, 4, 4)
TEXT = f"<0f>0<|box;x1=0;y1=0;x2=3;y2=3;w=4;h=4|><|mask;x=0;y=0;data=4,4,{MASK}|>\n"
LANE = {"item_id": "explicit-fixture", "output_index": 0, "content_index": 0}
DELTA = {"type": "response.output_text.delta", **LANE, "delta": TEXT}
DONE = {"type": "response.output_text.done", **LANE, "text": TEXT}
COMPLETED = {"type": "response.completed", "response": {"status": "completed"}}


def sse(value, *, event=None):
    prefix = f"event: {event}\n" if event else ""
    return (prefix + "data: " + json.dumps(value) + "\n\n").encode()


class LocalBody(sdk_http.AsyncByteStream):
    def __init__(self, chunks, counters, stalled=None):
        self.chunks = chunks
        self.counters = counters
        self.stalled = stalled

    async def __aiter__(self):
        try:
            for chunk in self.chunks:
                yield chunk
                await asyncio.sleep(0)
            if self.stalled is not None:
                self.stalled.set()
                await asyncio.Event().wait()
        finally:
            self.counters["body_finalized"] += 1
            # Make unordered GC cleanup overlap, as in the production failure.
            await asyncio.sleep(0)

    async def aclose(self):
        self.counters["transport_closed"] += 1
        await asyncio.sleep(0)


def make_provider(tmp_path, chunks, *, timeout=2, stalled=None):
    settings = SimpleNamespace(
        data_dir=tmp_path,
        request_timeout=timeout,
        api_key="explicit-fixture-no-network",
        meta_url="https://fixture.invalid/v1",
    )
    counters = {"requests": 0, "body_finalized": 0, "transport_closed": 0}

    def handler(request):
        counters["requests"] += 1
        payload = json.loads(request.content)
        assert payload["model"] == "sam-3.1" and payload["stream"] is True
        assert payload["metadata"] == {"mask_encoding": "one_bit"}
        return sdk_http.Response(
            200,
            request=request,
            headers={"content-type": "text/event-stream"},
            stream=LocalBody(chunks, counters, stalled),
        )

    provider = Providers(settings)
    provider._meta = AsyncOpenAI(
        api_key=settings.api_key,
        base_url=settings.meta_url,
        http_client=sdk_http.AsyncClient(transport=sdk_http.MockTransport(handler)),
        max_retries=0,
    )
    path = tmp_path / "explicit-fixture.jpg"
    Image.new("RGB", (4, 4), "blue").save(path)
    return provider, path, counters


@contextmanager
def cleanup_evidence():
    loop = asyncio.get_running_loop()
    initial = set(loop._asyncgens)
    errors = []
    old_handler = loop.get_exception_handler()
    old_hook = sys.unraisablehook
    loop.set_exception_handler(
        lambda _loop, context: errors.append((context.get("message"), repr(context.get("exception"))))
    )
    sys.unraisablehook = lambda event: errors.append((event.err_msg, repr(event.exc_value)))

    def pending():
        return [
            generator.ag_code.co_qualname
            for generator in list(loop._asyncgens)
            if generator not in initial and generator.ag_frame is not None
        ]

    try:
        yield errors, pending
    finally:
        loop.set_exception_handler(old_handler)
        sys.unraisablehook = old_hook


async def collect_from_worker():
    # Real image processing can collect the suspended SDK generator chain from
    # a worker thread. Exercise that boundary, not just event-loop shutdown.
    await asyncio.to_thread(gc.collect)
    for _ in range(5):
        await asyncio.sleep(0)


def assert_records(records):
    assert {record["kind"] for record in records} == {"box", "mask"}
    mask = next(record for record in records if record["kind"] == "mask")
    assert mask["mask"]["payload"] == MASK
    assert mask["bounds"] == {"left": 0, "top": 0, "right": 4, "bottom": 4}


def test_terminal_stream_closes_all_generators_before_return_across_40_requests(tmp_path):
    async def run():
        # response.completed is followed by buffered data: SAM stops before
        # exhausting the HTTP iterator, which exposed the old lifetime bug.
        wire = sse(DELTA) + sse(DONE) + sse(COMPLETED) + b"data: [DONE]\n\n"
        provider, path, counters = make_provider(tmp_path, [wire])
        with cleanup_evidence() as (errors, pending):
            try:
                for index in range(40):
                    assert_records(await provider.segment(path, "blue shirt"))
                    assert counters["body_finalized"] == index + 1
                    assert counters["transport_closed"] == index + 1
                    assert pending() == []
                    await collect_from_worker()
                    assert errors == []
                assert counters["requests"] == 40
            finally:
                await provider.close()
            await collect_from_worker()
            assert pending() == [] and errors == []

    asyncio.run(run())


@pytest.mark.parametrize(
    ("terminal", "message"),
    [
        (sse(DONE) + sse({"type": "response.incomplete", "response": {}}), "response did not complete"),
        (sse({"type": "response.refusal.done", "refusal": "fixture-private-text"}), "refused"),
        (
            sse({"type": "response.failed", "response": {"error": {"message": "fixture-private-text"}}}),
            "did not complete successfully",
        ),
        (sse({"error": {"message": "fixture-private-text"}}, event="error"), "did not complete successfully"),
        (sse(["fixture-private-text"]), "did not complete successfully"),
        (b"", "response did not complete"),
    ],
    ids=["incomplete", "refused", "failed", "error-event", "malformed-event", "eof"],
)
def test_unsuccessful_terminal_preserves_actual_mask_and_closes(tmp_path, terminal, message):
    async def run():
        provider, path, counters = make_provider(tmp_path, [sse(DELTA), terminal])
        with cleanup_evidence() as (errors, pending):
            try:
                with pytest.raises(ProviderError, match=message) as failure:
                    await provider.segment(path, "blue shirt")
                assert "fixture-private-text" not in str(failure.value)
                assert_records(failure.value.records)
                assert counters == {"requests": 1, "body_finalized": 1, "transport_closed": 1}
                assert pending() == []
            finally:
                await provider.close()
            await collect_from_worker()
            assert pending() == [] and errors == []

    asyncio.run(run())


@pytest.mark.parametrize("cancel", [False, True], ids=["timeout", "cancel"])
def test_interrupt_while_reading_closes_before_return_and_preserves_timeout_mask(tmp_path, cancel):
    async def run():
        stalled = asyncio.Event()
        provider, path, counters = make_provider(
            tmp_path, [sse(DELTA)], timeout=2 if cancel else 0.1, stalled=stalled
        )
        with cleanup_evidence() as (errors, pending):
            try:
                task = asyncio.create_task(provider.segment(path, "blue shirt"))
                await asyncio.wait_for(stalled.wait(), timeout=1)
                if cancel:
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
                else:
                    with pytest.raises(ProviderError, match="timed out") as failure:
                        await task
                    assert_records(failure.value.records)
                assert counters == {"requests": 1, "body_finalized": 1, "transport_closed": 1}
                assert pending() == []
            finally:
                await provider.close()
            await collect_from_worker()
            assert pending() == [] and errors == []

    asyncio.run(run())

"""Offline Vision adapter contract tests. No Google API, DNS or credentials used."""

import asyncio
import base64
import io
import json
import sys
import threading
import time
import types

import httpx
import pytest
from PIL import Image

from wardrobe import image_search as search


PAGE = "https://shop.public-store.com/products/shirt"
SECRET = "never-display-this-private-token"
_AsyncClient = httpx.AsyncClient


def picture(format="PNG", size=(24, 32)):
    output = io.BytesIO()
    Image.new("RGB", size, "blue").save(output, format)
    return output.getvalue()


def response(pages=None, **web):
    return {"responses": [{"webDetection": {"pagesWithMatchingImages": pages or [], **web}}]}


@pytest.fixture
def original(tmp_path):
    path = tmp_path / "original.png"
    path.write_bytes(picture())
    return path


class Network:
    def __init__(self, monkeypatch, handler=None):
        self.requests, self.clients, self.options = [], [], []
        self.handler = handler or (lambda _: httpx.Response(200, json=response()))

        async def receive(request):
            self.requests.append(request)
            result = self.handler(request)
            return await result if hasattr(result, "__await__") else result

        self.transport = httpx.MockTransport(receive)

        def client(**kwargs):
            self.options.append(kwargs)
            assert kwargs["follow_redirects"] is False and kwargs["trust_env"] is False
            value = _AsyncClient(transport=self.transport, **kwargs)
            self.clients.append(value)
            return value

        monkeypatch.setattr(search.httpx, "AsyncClient", client)


async def test_success_sends_exact_original_bytes_once_in_official_request(original, monkeypatch):
    def receive(request):
        assert request.method == "POST" and str(request.url) == search._ENDPOINT
        assert request.headers["x-goog-api-key"] == SECRET
        assert "authorization" not in request.headers and not request.url.query
        assert request.headers["accept-encoding"] == "identity"
        body = json.loads(request.content)
        assert body == {
            "requests": [
                {
                    "image": {"content": base64.b64encode(original.read_bytes()).decode()},
                    "features": [{"type": "WEB_DETECTION", "maxResults": 20}],
                }
            ]
        }
        return httpx.Response(
            200, json=response([{"url": PAGE, "pageTitle": "<b>Blue</b> shirt &amp; coat"}])
        )

    network = Network(monkeypatch, receive)
    monkeypatch.setattr(search, "_load_adc", lambda *_: pytest.fail("API key must not load ADC"))
    result = await search.GoogleVisionImageSearch(api_key=SECRET).search(original)
    assert result == {
        "sources": [{"url": PAGE, "title": "Blue shirt & coat", "snippet": search._SNIPPET}],
        "retrieved_count": 1,
    }
    assert len(network.requests) == 1 and all(client.is_closed for client in network.clients)
    assert original.read_bytes() == picture()


@pytest.mark.parametrize("format", ["JPEG", "PNG", "WEBP"])
async def test_legal_image_formats_are_validated_by_content_not_extension(format, tmp_path, monkeypatch):
    source = tmp_path / "photo.bin"
    source.write_bytes(picture(format))
    network = Network(monkeypatch)
    assert await search.GoogleVisionImageSearch(api_key=SECRET).search(source) == {
        "sources": [],
        "retrieved_count": 0,
    }
    assert len(network.requests) == 1


async def test_only_real_page_hits_are_used_and_urls_are_normalized_deduplicated(original, monkeypatch):
    pages = [
        {"url": PAGE + "#first", "pageTitle": "A"},
        {"url": PAGE + "#second", "pageTitle": "Duplicate"},
        {
            "url": "https://SHOP.PUBLIC-STORE.COM:443/products/other",
            "pageTitle": "<script>bad()</script><style>hidden</style>Other",
        },
        {"url": "http://shop.public-store.com/p/1"},
        {"url": "https://127.0.0.1/admin"},
        {"url": "https://10.0.0.2/p/1"},
        {"url": "https://[::1]/p/1"},
        {"url": "https://localhost/p/1"},
        {"url": "https://shop.internal/p/1"},
        {"url": "https://user:password@shop.public-store.com/p/1"},
        {"url": "https://shop.public-store.com:8443/p/1"},
        {"url": "https://shop.public-store.com/a%2EPNG?size=1"},
        {"url": "https://shop.public-store.com/a.webp"},
        {"url": "data:image/png;base64,AAAA"},
        {"url": "https://shop.public-store.com/\nprivate"},
        {"url": [PAGE]},
        None,
        "bad entry",
        {},
    ]
    network = Network(
        monkeypatch,
        lambda _: httpx.Response(
            200,
            json=response(
                pages,
                visuallySimilarImages=[{"url": "https://images.public-store.com/image"}],
                fullMatchingImages=[{"url": "https://images.public-store.com/full"}],
                partialMatchingImages=[{"url": "https://images.public-store.com/partial"}],
            ),
        ),
    )
    result = await search.GoogleVisionImageSearch(api_key=SECRET).search(original)
    assert result["retrieved_count"] == 2
    assert [entry["url"] for entry in result["sources"]] == [
        PAGE,
        "https://shop.public-store.com/products/other",
    ]
    assert [entry["title"] for entry in result["sources"]] == ["A", "Other"]
    assert len(network.requests) == 1


async def test_twenty_page_cap_bounded_title_and_host_fallback(original, monkeypatch):
    pages = [
        {"url": f"https://shop.public-store.com/{index}", "pageTitle": "x" * 20_000 if index else None}
        for index in range(25)
    ]
    Network(monkeypatch, lambda _: httpx.Response(200, json=response(pages)))
    result = await search.GoogleVisionImageSearch(api_key=SECRET).search(original)
    assert len(result["sources"]) == result["retrieved_count"] == 20
    assert result["sources"][0]["title"] == "shop.public-store.com"
    assert len(result["sources"][1]["title"]) == 200
    assert all(entry["snippet"] == search._SNIPPET for entry in result["sources"])


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"responses": []},
        {"responses": [None]},
        {"responses": [{}, {}]},
        {"responses": "bad"},
        {"responses": [{"labelAnnotations": []}]},
        {"responses": [{"webDetection": None}]},
        {"responses": [{"webDetection": []}]},
        {"responses": [{"webDetection": {"pagesWithMatchingImages": None}}]},
        {"responses": [{"webDetection": {"pagesWithMatchingImages": {"url": PAGE}}}]},
        {"error": {"message": SECRET}, "responses": [{}]},
        {"responses": [{"error": {"code": 7, "message": SECRET}}]},
        {"responses": [{"error": {}, "webDetection": {"pagesWithMatchingImages": [{"url": PAGE}]}}]},
    ],
)
async def test_malformed_or_embedded_errors_never_become_success(payload, original, monkeypatch):
    network = Network(monkeypatch, lambda _: httpx.Response(200, json=payload))
    with pytest.raises(search.ImageSearchError) as caught:
        await search.GoogleVisionImageSearch(api_key=SECRET).search(original)
    assert SECRET not in str(caught.value) and len(network.requests) == 1
    assert all(client.is_closed for client in network.clients)


@pytest.mark.parametrize("payload", [{"responses": [{}]}, {"responses": [{"webDetection": {}}]}, response()])
async def test_valid_empty_annotation_is_a_zero_result(payload, original, monkeypatch):
    Network(monkeypatch, lambda _: httpx.Response(200, json=payload))
    assert await search.GoogleVisionImageSearch(api_key=SECRET).search(original) == {
        "sources": [],
        "retrieved_count": 0,
    }


@pytest.mark.parametrize(
    "status,code",
    [
        (301, "redirect"),
        (302, "redirect"),
        (307, "redirect"),
        (401, "denied"),
        (403, "denied"),
        (429, "denied"),
        (500, "service"),
        (503, "service"),
    ],
)
async def test_http_errors_are_fixed_safe_no_retry_or_redirect(status, code, original, monkeypatch):
    network = Network(
        monkeypatch,
        lambda _: httpx.Response(
            status, headers={"location": "https://evil.public-store.com/private"}, text=SECRET
        ),
    )
    with pytest.raises(search.ImageSearchError) as caught:
        await search.GoogleVisionImageSearch(api_key=SECRET).search(original)
    assert caught.value.code == code and SECRET not in repr(caught.value)
    assert len(network.requests) == 1 and str(network.requests[0].url) == search._ENDPOINT


class Stream(httpx.AsyncByteStream):
    def __init__(self, chunks, *, block=False):
        self.chunks, self.block = chunks, block
        self.closed = False
        self.entered = asyncio.Event()

    async def __aiter__(self):
        self.entered.set()
        for chunk in self.chunks:
            yield chunk
        if self.block:
            await asyncio.Event().wait()

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize(
    "headers,chunks,code",
    [
        ({"content-length": "1048577"}, [b"{}"], "response_size"),
        ({"content-length": "2"}, [b"x" * (1024 * 1024 + 1)], "response_size"),
        ({}, [b"x" * 65_536] * 17, "response_size"),
        ({"content-encoding": "gzip"}, [b"opaque"], "response"),
        ({}, [b"not-json"], "response"),
        ({}, [b"\xff\xfe\xff"], "response"),
    ],
)
async def test_response_bytes_are_bounded_even_without_honest_content_length(
    headers, chunks, code, original, monkeypatch
):
    stream = Stream(chunks)
    network = Network(monkeypatch, lambda _: httpx.Response(200, headers=headers, stream=stream))
    with pytest.raises(search.ImageSearchError) as caught:
        await search.GoogleVisionImageSearch(api_key=SECRET).search(original)
    assert caught.value.code == code and stream.closed
    assert len(network.requests) == 1


async def test_total_timeout_closes_stream_and_never_retries(original, monkeypatch):
    stream = Stream([], block=True)
    network = Network(monkeypatch, lambda _: httpx.Response(200, stream=stream))
    started = time.monotonic()
    with pytest.raises(search.ImageSearchError) as caught:
        await search.GoogleVisionImageSearch(api_key=SECRET, timeout=0.08).search(original)
    assert caught.value.code == "timeout" and time.monotonic() - started < 0.5
    assert stream.closed and len(network.requests) == 1
    assert all(client.is_closed for client in network.clients)


async def test_cancellation_propagates_and_closes_stream(original, monkeypatch):
    stream = Stream([], block=True)
    network = Network(monkeypatch, lambda _: httpx.Response(200, stream=stream))
    task = asyncio.create_task(search.GoogleVisionImageSearch(api_key=SECRET).search(original))
    await asyncio.wait_for(stream.entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stream.closed and len(network.requests) == 1
    assert all(client.is_closed for client in network.clients)


@pytest.mark.parametrize("directory", sorted(search._FORBIDDEN_DIRECTORIES))
async def test_generated_and_product_directories_rejected_before_auth_or_http(
    directory, tmp_path, monkeypatch
):
    path = tmp_path / directory / "source.png"
    path.parent.mkdir()
    path.write_bytes(picture())
    network = Network(monkeypatch)
    monkeypatch.setattr(search, "_load_adc", lambda *_: pytest.fail("Invalid source must not read ADC"))
    with pytest.raises(search.ImageSearchError) as caught:
        await search.GoogleVisionImageSearch(quota_project="explicit-project").search(path)
    assert caught.value.code == "source" and not network.requests and not network.clients


@pytest.mark.parametrize(
    "kind",
    [
        "symlink",
        "parent_alias",
        "directory",
        "missing",
        "fake_jpeg",
        "gif",
        "truncated",
        "generated_name",
        "oversize",
        "pixels",
    ],
)
async def test_bad_images_and_source_aliases_never_reach_http(kind, tmp_path, monkeypatch):
    path = tmp_path / "source.png"
    path.write_bytes(picture())
    if kind == "symlink":
        target = tmp_path / "alias.png"
        target.symlink_to(path)
        path = target
    elif kind == "parent_alias":
        generated = tmp_path / "generated"
        generated.mkdir()
        (generated / "source.png").write_bytes(picture())
        alias = tmp_path / "originals"
        alias.symlink_to(generated, target_is_directory=True)
        path = alias / "source.png"
    elif kind == "directory":
        path = tmp_path
    elif kind == "missing":
        path = tmp_path / "missing.jpg"
    elif kind == "fake_jpeg":
        path.write_text("Not an image " + SECRET)
    elif kind == "gif":
        path.write_bytes(picture("GIF"))
    elif kind == "truncated":
        path.write_bytes(picture()[:40])
    elif kind == "generated_name":
        path = tmp_path / "generated.png"
        path.write_bytes(picture())
    elif kind == "oversize":
        path.write_bytes(b"x" * (search._MAX_IMAGE_BYTES + 1))
    elif kind == "pixels":
        monkeypatch.setattr(search, "_MAX_PIXELS", 100)
    network = Network(monkeypatch)
    with pytest.raises(search.ImageSearchError) as caught:
        await search.GoogleVisionImageSearch(api_key=SECRET).search(path)
    assert caught.value.code in {"source", "image_size"}
    assert SECRET not in str(caught.value) and not network.requests and not network.clients


@pytest.mark.parametrize("timeout", [0, -1, True, float("inf"), float("nan"), "20"])
def test_invalid_deadline_is_rejected(timeout):
    with pytest.raises(search.ImageSearchError):
        search.GoogleVisionImageSearch(api_key=SECRET, timeout=timeout)


def adc(monkeypatch, tmp_path, *, valid=True, refresh=None, token=SECRET, loader=None):
    credentials = types.SimpleNamespace(valid=valid, token=token)
    credentials.refresh = refresh or (lambda _: pytest.fail("Valid token must not refresh"))
    calls = []

    def load(path, **kwargs):
        calls.append((path, kwargs))
        return loader(path, **kwargs) if loader else (credentials, "must-not-use-inferred-project")

    google = types.ModuleType("google")
    google.__path__ = []
    auth = types.ModuleType("google.auth")
    auth.load_credentials_from_file = load
    google.auth = auth
    monkeypatch.setitem(sys.modules, "google", google)
    monkeypatch.setitem(sys.modules, "google.auth", auth)
    path = tmp_path / "local-adc.json"
    path.write_text("{}")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(path))
    return credentials, calls


async def test_adc_requires_explicit_quota_before_loading_credentials(original, monkeypatch):
    network = Network(monkeypatch)
    monkeypatch.setattr(search, "_load_adc", lambda *_: pytest.fail("No implicit project lookup"))
    with pytest.raises(search.ImageSearchError) as caught:
        await search.GoogleVisionImageSearch().search(original)
    assert caught.value.code == "quota" and not network.requests


async def test_missing_optional_dependency_is_safe_and_no_http(original, monkeypatch):
    monkeypatch.setitem(sys.modules, "google.auth", None)
    network = Network(monkeypatch)
    with pytest.raises(search.ImageSearchError) as caught:
        await search.GoogleVisionImageSearch(quota_project="explicit-project").search(original)
    assert caught.value.code == "dependency" and not network.requests


async def test_adc_valid_token_uses_only_explicit_quota_and_never_discovers_project(
    original, tmp_path, monkeypatch
):
    _, calls = adc(monkeypatch, tmp_path)
    network = Network(monkeypatch)
    await search.GoogleVisionImageSearch(quota_project="explicit-project").search(original)
    assert len(calls) == 1
    assert calls[0][1]["scopes"] == [search._SCOPE]
    assert calls[0][1]["quota_project_id"] == "explicit-project"
    assert len(network.requests) == 1
    request = network.requests[0]
    assert request.headers["authorization"] == "Bearer " + SECRET
    assert request.headers["x-goog-user-project"] == "explicit-project"
    assert (
        "x-goog-api-key" not in request.headers
        and "must-not-use-inferred-project" not in request.headers.values()
    )


async def test_adc_refresh_once_with_bounded_transport_then_one_vision_post(original, tmp_path, monkeypatch):
    refreshes = []
    credentials, _ = adc(monkeypatch, tmp_path, valid=False)

    def refresh(request):
        refreshes.append(1)
        reply = request(
            "https://oauth2.googleapis.com/token",
            method="POST",
            body=b"private-refresh-body",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        credentials.token = json.loads(reply.data)["access_token"]
        credentials.valid = True

    credentials.refresh = refresh
    network = Network(
        monkeypatch,
        lambda request: (
            httpx.Response(200, json={"access_token": SECRET})
            if request.url.host == "oauth2.googleapis.com"
            else httpx.Response(200, json=response())
        ),
    )
    await search.GoogleVisionImageSearch(quota_project="explicit-project").search(original)
    assert refreshes == [1]
    assert [request.url.host for request in network.requests] == [
        "oauth2.googleapis.com",
        "vision.googleapis.com",
    ]
    assert all(client.is_closed for client in network.clients)


@pytest.mark.parametrize(
    "failure", ["exception", "bad_token", "refresh_403", "redirect", "second_refresh", "private_endpoint"]
)
async def test_adc_errors_never_leak_or_submit_image(failure, original, tmp_path, monkeypatch):
    credentials, _ = adc(monkeypatch, tmp_path, valid=False)

    def refresh(request):
        if failure == "exception":
            raise RuntimeError(SECRET + " account@example.com")
        if failure == "bad_token":
            credentials.token = "token\n" + SECRET
            return
        url = (
            "http://169.254.169.254/token"
            if failure == "private_endpoint"
            else "https://oauth2.googleapis.com/token"
        )
        request(url, method="POST")
        if failure == "second_refresh":
            request(url, method="POST")

    credentials.refresh = refresh
    status = 403 if failure == "refresh_403" else 302 if failure == "redirect" else 200
    network = Network(monkeypatch, lambda _: httpx.Response(status, text=SECRET))
    with pytest.raises(search.ImageSearchError) as caught:
        await search.GoogleVisionImageSearch(quota_project="explicit-project").search(original)
    assert caught.value.code == "auth" and SECRET not in str(caught.value)
    assert "account@example.com" not in repr(caught.value)
    assert all(request.url.host == "oauth2.googleapis.com" for request in network.requests)
    assert len(network.requests) <= 1 and all(client.is_closed for client in network.clients)


@pytest.mark.parametrize("cancel", [False, True])
async def test_auth_included_in_deadline_and_cancellation_stops_transport(
    cancel, original, tmp_path, monkeypatch
):
    stream = Stream([], block=True)
    ended = threading.Event()
    credentials, _ = adc(monkeypatch, tmp_path, valid=False)

    def refresh(request):
        try:
            request("https://oauth2.googleapis.com/token", method="POST")
        finally:
            ended.set()

    credentials.refresh = refresh
    network = Network(monkeypatch, lambda _: httpx.Response(200, stream=stream))
    task = asyncio.create_task(
        search.GoogleVisionImageSearch(
            quota_project="explicit-project", timeout=0.1 if not cancel else 2
        ).search(original)
    )
    await asyncio.wait_for(stream.entered.wait(), 1)
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(search.ImageSearchError) as caught:
            await task
        assert caught.value.code == "timeout"
    await asyncio.wait_for(asyncio.to_thread(ended.wait), 1)
    await asyncio.sleep(0)
    assert stream.closed and len(network.requests) == 1
    assert network.requests[0].url.host == "oauth2.googleapis.com"
    assert all(client.is_closed for client in network.clients)


async def test_slow_local_image_read_is_inside_total_deadline(original, monkeypatch):
    network = Network(monkeypatch)

    def slow(_):
        time.sleep(0.1)
        return picture()

    monkeypatch.setattr(search, "_read_original", slow)
    with pytest.raises(search.ImageSearchError) as caught:
        await search.GoogleVisionImageSearch(api_key=SECRET, timeout=0.02).search(original)
    assert caught.value.code == "timeout"
    await asyncio.sleep(0.12)
    assert not network.requests


def test_exception_never_exposes_arbitrary_input():
    assert SECRET not in str(search.ImageSearchError(SECRET))


@pytest.mark.parametrize(
    "title,expected",
    [
        ("&lt;b&gt;Blue&lt;/b&gt; &amp; white", "Blue & white"),
        ("<script>secret()</script><b>Coat</b>", "Coat"),
        ("<style>hidden</style>", "shop.public-store.com"),
        ({"not": "text"}, "shop.public-store.com"),
    ],
)
def test_page_titles_are_plain_bounded_text(title, expected):
    assert search._title(title, PAGE) == expected

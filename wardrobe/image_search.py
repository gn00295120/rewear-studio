"""Optional Google Vision original-photo index lookup, never product verification.

Official contracts: https://docs.cloud.google.com/vision/docs/request and
https://docs.cloud.google.com/docs/authentication/api-keys-use . Only the
pagesWithMatchingImages field is exposed; image-only hits are not page evidence.
"""

from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import io
import json
import math
import os
import stat
import threading
import time
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote, urlsplit

import httpx
from PIL import Image

from .identity_schemas import normalize_source_url

_ENDPOINT = "https://vision.googleapis.com/v1/images:annotate"
_SCOPE = "https://www.googleapis.com/auth/cloud-vision"
_MAX_IMAGE_BYTES = 5 * 1024 * 1024
_MAX_PIXELS = 20_000_000
_MAX_RESPONSE_BYTES = 1024 * 1024
_FORBIDDEN_DIRECTORIES = {
    "reconstruction",
    "reconstructions",
    "generated",
    "generated-images",
    "generated_images",
    "shopping",
    "product",
    "products",
    "product-images",
    "product_images",
}
_IMAGE_SUFFIXES = {
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".gif",
    ".svg",
    ".avif",
    ".bmp",
    ".tif",
    ".tiff",
    ".heic",
    ".ico",
}
_ERRORS = {
    "config": "Google 原圖檢索設定無效。",
    "source": "原圖檢索只能使用有效的原始照片。",
    "image_size": "原圖檢索圖片不得超過 5 MB 或 2,000 萬像素。",
    "quota": "使用 Google 原圖檢索憑證時，必須明確指定配額專案。",
    "dependency": "使用既有 Google 憑證需要安裝 google-auth 套件。",
    "auth": "Google 原圖檢索驗證失敗，請檢查已設定的憑證。",
    "timeout": "Google 原圖檢索逾時。",
    "response": "Google 原圖檢索回應無法處理。",
    "response_size": "Google 原圖檢索回應超過大小限制。",
    "redirect": "Google 原圖檢索回應導向其他網址，已停止。",
    "denied": "Google 原圖檢索未獲授權或配額不足。",
    "service": "Google 原圖檢索目前無法使用。",
}
_SNIPPET = "Google 原圖索引候選；僅表示索引可能包含相符圖片，未確認同款、品牌、庫存或購買管道。"


class ImageSearchError(Exception):
    """Only fixed safe messages; provider/account/request details never escape."""

    def __init__(self, code: str = "service"):
        self.code = code if code in _ERRORS else "service"
        super().__init__(_ERRORS[self.code])


def _header_value(value: str | None, *, maximum: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ImageSearchError("config")
    value = value.strip()
    if not value or len(value) > maximum or any(not 33 <= ord(c) <= 126 for c in value):
        raise ImageSearchError("config")
    return value


def _read_original(path: Path) -> bytes:
    try:
        path = Path(path)
        resolved = path.resolve(strict=True)
        for candidate in (path.absolute(), resolved):
            if any(part.casefold() in _FORBIDDEN_DIRECTORIES for part in candidate.parts[:-1]):
                raise ImageSearchError("source")
            if candidate.stem.casefold() in {"generated", "reconstruction", "model-output"}:
                raise ImageSearchError("source")
        if path.is_symlink():
            raise ImageSearchError("source")
        # Avoid following a last-moment symlink or blocking on a special file.
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        with os.fdopen(os.open(resolved, flags), "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise ImageSearchError("source")
            if info.st_size > _MAX_IMAGE_BYTES:
                raise ImageSearchError("image_size")
            data = stream.read(_MAX_IMAGE_BYTES + 1)
        if not data or len(data) > _MAX_IMAGE_BYTES:
            raise ImageSearchError("image_size")
        with Image.open(io.BytesIO(data)) as image:
            if image.format not in {"JPEG", "PNG", "WEBP"} or getattr(image, "n_frames", 1) != 1:
                raise ImageSearchError("source")
            if image.width <= 0 or image.height <= 0 or image.width * image.height > _MAX_PIXELS:
                raise ImageSearchError("image_size")
            image.verify()
        with Image.open(io.BytesIO(data)) as image:
            image.load()
        return data
    except ImageSearchError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ImageSearchError("image_size") from None
    except Exception:
        raise ImageSearchError("source") from None


class _Title(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style"} and self.hidden:
            self.hidden -= 1

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def _title(value, url: str) -> str:
    if isinstance(value, str):
        parser = _Title()
        parser.feed(unescape(value[:8000]))
        clean = " ".join(" ".join(parser.parts).split())
        clean = "".join(c for c in clean if ord(c) >= 32 and ord(c) != 127)
        if clean:
            return clean[:200]
    return urlsplit(url).hostname[:200]


def _sources(value) -> dict:
    if not isinstance(value, dict) or "error" in value:
        raise ImageSearchError("response")
    responses = value.get("responses")
    if not isinstance(responses, list) or len(responses) != 1 or not isinstance(responses[0], dict):
        raise ImageSearchError("response")
    response = responses[0]
    if "error" in response:
        # Even a partial success alongside an embedded error is not a clean search.
        raise ImageSearchError("service")
    web = response.get("webDetection", {})
    if not isinstance(web, dict) or ("webDetection" not in response and set(response) - {"context"}):
        raise ImageSearchError("response")
    pages = web.get("pagesWithMatchingImages", [])
    if not isinstance(pages, list):
        raise ImageSearchError("response")
    sources, seen = [], set()
    for entry in pages:
        if not isinstance(entry, dict):
            continue
        try:
            url = normalize_source_url(entry.get("url"))
        except ValueError:
            continue
        if Path(unquote(urlsplit(url).path)).suffix.casefold() in _IMAGE_SUFFIXES or url in seen:
            continue
        seen.add(url)
        sources.append({"title": _title(entry.get("pageTitle"), url), "url": url, "snippet": _SNIPPET})
        if len(sources) == 20:
            break
    return {"sources": sources, "retrieved_count": len(sources)}


async def _bounded_body(response: httpx.Response) -> bytes:
    # Do not let a compressed response allocate unbounded decoded memory.
    if response.headers.get("content-encoding", "identity").lower() not in {"", "identity"}:
        raise ImageSearchError("response")
    length = response.headers.get("content-length")
    if length is not None and (not length.isdecimal() or int(length) > _MAX_RESPONSE_BYTES):
        raise ImageSearchError("response_size")
    result = bytearray()
    async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
        result.extend(chunk)
        if len(result) > _MAX_RESPONSE_BYTES:
            raise ImageSearchError("response_size")
    return bytes(result)


class _ADCRequest:
    """Bridge one synchronous google-auth refresh into cancellable async HTTP."""

    def __init__(self, deadline: float):
        self.loop = asyncio.get_running_loop()
        self.deadline = deadline
        self.stopped = threading.Event()
        self.calls = 0
        self.future = None

    def cancel(self):
        self.stopped.set()
        if self.future is not None:
            self.future.cancel()

    def __call__(self, url, method="GET", body=None, headers=None, **kwargs):
        if self.stopped.is_set() or self.calls or time.monotonic() >= self.deadline:
            raise ImageSearchError("auth")
        self.calls += 1
        self.future = asyncio.run_coroutine_threadsafe(self._send(url, method, body, headers), self.loop)
        if self.stopped.is_set():
            self.future.cancel()
        try:
            return self.future.result(timeout=max(0.001, self.deadline - time.monotonic()))
        except (concurrent.futures.TimeoutError, concurrent.futures.CancelledError):
            self.future.cancel()
            raise ImageSearchError("auth") from None

    async def _send(self, url, method, body, headers):
        # Local ADC token exchange only; never discover a project or probe a
        # metadata server. A failed exchange cannot trigger library retries.
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname
            not in {
                "oauth2.googleapis.com",
                "accounts.google.com",
                "www.googleapis.com",
                "sts.googleapis.com",
                "iamcredentials.googleapis.com",
            }
            or parsed.username
            or parsed.password
            or parsed.port not in (None, 443)
        ):
            raise ImageSearchError("auth")
        async with asyncio.timeout(max(0.001, self.deadline - time.monotonic())):
            async with httpx.AsyncClient(timeout=10, follow_redirects=False, trust_env=False) as client:
                async with client.stream(
                    method, url, content=body, headers={**(headers or {}), "Accept-Encoding": "identity"}
                ) as response:
                    if response.status_code != 200:
                        raise ImageSearchError("auth")
                    data = await _bounded_body(response)
                    return SimpleNamespace(
                        status=response.status_code, data=data, headers=dict(response.headers)
                    )


def _load_adc(quota_project: str, request: _ADCRequest) -> dict[str, str]:
    try:
        import google.auth
    except ImportError:
        raise ImageSearchError("dependency") from None
    try:
        configured = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        credential_file = (
            Path(configured).expanduser()
            if configured
            else Path(os.environ.get("CLOUDSDK_CONFIG", str(Path.home() / ".config/gcloud")))
            / "application_default_credentials.json"
        )
        if not credential_file.is_file() or credential_file.stat().st_size > _MAX_RESPONSE_BYTES:
            raise ImageSearchError("auth")
        credentials, _ = google.auth.load_credentials_from_file(
            str(credential_file), scopes=[_SCOPE], quota_project_id=quota_project, request=request
        )
        if request.stopped.is_set():
            raise ImageSearchError("auth")
        if not credentials.valid:
            credentials.refresh(request)
        token = _header_value(credentials.token, maximum=16384)
        if token is None:
            raise ImageSearchError("auth")
        return {"Authorization": f"Bearer {token}", "x-goog-user-project": quota_project}
    except Exception:
        raise ImageSearchError("auth") from None


class GoogleVisionImageSearch:
    def __init__(
        self, *, api_key: str | None = None, quota_project: str | None = None, timeout: float = 20.0
    ):
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ImageSearchError("config")
        self._api_key = _header_value(api_key, maximum=512)
        self._quota_project = _header_value(quota_project, maximum=128)
        self.timeout = float(timeout)

    async def _headers(self, deadline: float) -> dict[str, str]:
        if self._api_key is not None:
            return {"x-goog-api-key": self._api_key}
        if self._quota_project is None:
            raise ImageSearchError("quota")
        request = _ADCRequest(deadline)
        try:
            return await asyncio.to_thread(_load_adc, self._quota_project, request)
        finally:
            request.cancel()

    async def search(self, path: Path) -> dict:
        try:
            async with asyncio.timeout(self.timeout):
                deadline = time.monotonic() + self.timeout
                data = await asyncio.to_thread(_read_original, path)
                headers = await self._headers(deadline)
                body = {
                    "requests": [
                        {
                            "image": {"content": base64.b64encode(data).decode("ascii")},
                            "features": [{"type": "WEB_DETECTION", "maxResults": 20}],
                        }
                    ]
                }
                async with httpx.AsyncClient(
                    timeout=self.timeout, follow_redirects=False, trust_env=False
                ) as client:
                    async with client.stream(
                        "POST", _ENDPOINT, json=body, headers={**headers, "Accept-Encoding": "identity"}
                    ) as response:
                        if 300 <= response.status_code < 400:
                            raise ImageSearchError("redirect")
                        if response.status_code in {401, 403, 429}:
                            raise ImageSearchError("denied")
                        if response.status_code != 200:
                            raise ImageSearchError("service")
                        raw = await _bounded_body(response)
                return _sources(json.loads(raw))
        except ImageSearchError:
            raise
        except (TimeoutError, httpx.TimeoutException):
            raise ImageSearchError("timeout") from None
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError, TypeError):
            raise ImageSearchError("response") from None
        except Exception:
            raise ImageSearchError("service") from None

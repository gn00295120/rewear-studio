"""Simulated retrieval contracts; these tests do not prove shopping recall."""

import asyncio
import copy
import json
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from test_discovery import DiscoveryProviders, RECORD, identity_fixture, seed_item, shopping_fixture
from test_discovery_provider import comparison_response, fetched_product, make_provider, search_response
from wardrobe.app import create_app
from wardrobe.config import Settings
from wardrobe.identity_schemas import ShoppingResult, normalize_identity
from wardrobe.image_search import ImageSearchError


def indexed_source(url="https://retailer.example.com/tw/products/blue-shirt"):
    return {"title": "藍色上衣", "url": url, "snippet": "原圖索引找到的頁面，尚未核驗商品。"}


@pytest.fixture
def original(tmp_path):
    path = tmp_path / "original.png"
    Image.new("RGB", (80, 100), "blue").save(path)
    return path


@pytest.fixture
def observed():
    identity = identity_fixture()
    identity.update(status="unknown", product_code=None)
    return normalize_identity(identity)


def enable(provider, **changes):
    provider.settings = replace(
        provider.settings,
        image_search_provider="google_vision",
        vision_api_key="private-vision-test-key",
        **changes,
    )


def adapter_fixture(monkeypatch, *, sources=None, side_effect=None):
    sources = [indexed_source()] if sources is None else sources
    adapter = SimpleNamespace(
        search=AsyncMock(
            return_value={"sources": sources, "retrieved_count": len(sources)}, side_effect=side_effect
        )
    )
    factory = Mock(return_value=adapter)
    monkeypatch.setattr("wardrobe.providers.GoogleVisionImageSearch", factory)
    return adapter, factory


def empty_text(_request):
    return httpx.Response(200, json=search_response(offers=[], hits=[], cited_urls=[]))


async def test_disabled_never_constructs_or_sends_image_even_if_key_present(
    tmp_path, original, observed, monkeypatch
):
    adapter, factory = adapter_fixture(monkeypatch)
    provider = make_provider(tmp_path, empty_text)
    provider.settings = replace(provider.settings, vision_api_key="configured-but-not-enabled")
    try:
        result = await provider.search_shopping(observed, RECORD, image_path=original)
    finally:
        await provider.close()
    factory.assert_not_called()
    adapter.search.assert_not_awaited()
    assert [attempt["kind"] for attempt in result["search_attempts"]] == ["text", "text"]


async def test_image_pages_remain_unverified_and_do_not_create_an_observed_sku(
    tmp_path, original, observed, monkeypatch
):
    source = indexed_source()
    source["title"] = "Blue shirt STYLE-888"
    adapter, _ = adapter_fixture(monkeypatch, sources=[source])
    before = copy.deepcopy(observed)
    provider = make_provider(tmp_path, empty_text)
    enable(provider)
    try:
        result = await provider.search_shopping(observed, RECORD, image_path=original)
    finally:
        await provider.close()
    adapter.search.assert_awaited_once_with(original)
    assert observed == before and observed["product_code"] is None
    assert result["version"] == 1 and result["status"] == "searched"
    assert [attempt["kind"] for attempt in result["search_attempts"]] == ["image", "text", "text"]
    assert result["search_attempts"][0]["candidate_count"] == 1
    assert result["offers"][0]["match"] == "similar"
    assert "verification" not in result["offers"][0]
    assert result["sources"] == [source]


async def test_image_failure_keeps_text_candidates_and_visible_attempt_error(
    tmp_path, original, observed, monkeypatch
):
    adapter_fixture(monkeypatch, side_effect=ImageSearchError("denied"))
    provider = make_provider(tmp_path, lambda _: httpx.Response(200, json=search_response()))
    enable(provider)
    try:
        result = await provider.search_shopping(observed, RECORD, image_path=original)
    finally:
        await provider.close()
    assert result["offers"]
    assert result["search_attempts"][0]["status"] == "unavailable"
    assert result["search_attempts"][0]["error"] == str(ImageSearchError("denied"))
    assert result["search_attempts"][1]["kind"] == "text"


async def test_missing_configuration_keeps_text_without_google_request(
    tmp_path, original, observed, monkeypatch
):
    _, factory = adapter_fixture(monkeypatch)
    provider = make_provider(tmp_path, empty_text)
    provider.settings = replace(provider.settings, image_search_provider="google_vision", vision_api_key=None)
    try:
        result = await provider.search_shopping(observed, RECORD, image_path=original)
    finally:
        await provider.close()
    factory.assert_not_called()
    assert len(result["search_attempts"]) == 3
    assert "not configured" in result["search_attempts"][0]["error"]


@pytest.mark.parametrize("slow", [False, True])
async def test_text_error_or_timeout_preserves_already_indexed_pages(
    tmp_path, original, observed, monkeypatch, slow
):
    adapter_fixture(monkeypatch)
    stopped = asyncio.Event()

    async def text_response(_request):
        if slow:
            try:
                await asyncio.sleep(3)
            finally:
                stopped.set()
        return httpx.Response(503, json={"error": {"message": "private upstream details"}})

    provider = make_provider(tmp_path, text_response, timeout=0.3)
    enable(provider)
    started = time.monotonic()
    try:
        result = await provider.search_shopping(observed, RECORD, image_path=original)
    finally:
        await provider.close()
    assert time.monotonic() - started < 1
    assert result["offers"][0]["url"] == indexed_source()["url"]
    assert result["search_attempts"][1]["status"] == "unavailable"
    assert "private upstream" not in json.dumps(result)
    if slow:
        assert stopped.is_set()


async def test_cancel_during_image_search_does_not_start_text_search(
    tmp_path, original, observed, monkeypatch
):
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def wait_for_cancel(_path):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    adapter_fixture(monkeypatch, side_effect=wait_for_cancel)
    calls = []
    provider = make_provider(tmp_path, lambda request: calls.append(request) or empty_text(request))
    enable(provider)
    task = asyncio.create_task(provider.search_shopping(observed, RECORD, image_path=original))
    try:
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        await provider.close()
    assert cancelled.is_set() and not calls


async def test_image_candidate_still_requires_product_image_verification(
    tmp_path, original, observed, monkeypatch
):
    adapter_fixture(monkeypatch)

    def response(request):
        if request.url.path.endswith("/responses"):
            return empty_text(request)
        return httpx.Response(200, json=comparison_response(pattern="mismatch"))

    async def fetch(url, market):
        return fetched_product(page_url=url)

    monkeypatch.setattr("wardrobe.providers.fetch_product", fetch)
    provider = make_provider(tmp_path, response)
    enable(provider)
    try:
        candidates = await provider.search_shopping(observed, RECORD, image_path=original)
        result = await provider.verify_shopping(
            observed, RECORD, candidates, original, None, tmp_path / "products"
        )
    finally:
        await provider.close()
    assert result["version"] == 2 and not result["offers"]
    assert result["checked_candidates"][0]["status"] == "rejected"
    assert any("pattern does not match" in reason for reason in result["checked_candidates"][0]["reasons"])


async def test_image_page_filter_preserves_product_routes_but_removes_photos_and_wrong_market(
    tmp_path, original, observed, monkeypatch
):
    urls = [
        "https://www.pexels.com/photo/example-shirt-1/",
        "https://www.instagram.com/p/abc/",
        "https://retailer.example.com/search?q=shirt",
        "https://retailer.example.com/om/products/blue-shirt",
        "https://retailer.example.com/tw/collections/shirts/products/blue-shirt",
    ]
    adapter_fixture(monkeypatch, sources=[indexed_source(url) for url in urls])
    provider = make_provider(tmp_path, empty_text)
    enable(provider)
    try:
        result = await provider.search_shopping(observed, RECORD, image_path=original)
    finally:
        await provider.close()
    assert [offer["url"] for offer in result["offers"]] == [urls[-1]]
    assert result["search_attempts"][0]["retrieved_count"] == 5


def test_health_never_exposes_keys_or_claims_access_and_manager_passes_original(tmp_path):
    class OriginalAwareProvider(DiscoveryProviders):
        async def search_shopping(self, identity, record, market="TW", *, image_path=None):
            self.image_path = image_path
            return await super().search_shopping(identity, record, market)

    provider = OriginalAwareProvider()
    settings = Settings(
        data_dir=tmp_path,
        api_key="private-meta-key",
        image_search_provider="google_vision",
        vision_api_key="private-vision-key",
        vision_project="private-project-123",
    )
    with TestClient(create_app(settings, provider)) as client:
        health = client.get("/api/health").json()
        encoded = json.dumps(health)
        assert not any(
            value in encoded for value in (settings.api_key, settings.vision_api_key, settings.vision_project)
        )
        image = health["discovery"]["image_search"]
        assert image["enabled"] and image["configured"] and "need to be verified" in image["notice"]
        item, _ = seed_item(client.app)
        assert client.post(f"/api/items/{item['id']}/discovery", json={"market": "TW"}).status_code == 202
        for _ in range(100):
            state = client.get(f"/api/items/{item['id']}").json()
            if state["discovery"]["status"] not in {"queued", "running"}:
                break
            time.sleep(0.01)
        assert provider.image_path == Path(provider.identify_calls[0]["cutout"])


def test_settings_default_opt_out_and_attempt_legacy_compatibility(monkeypatch):
    monkeypatch.delenv("REWEAR_IMAGE_SEARCH", raising=False)
    monkeypatch.setenv("REWEAR_VISION_API_KEY", "secret-without-opt-in")
    settings = Settings()
    assert not settings.image_search_enabled and not settings.image_search_configured
    assert "secret-without-opt-in" not in repr(settings)
    with pytest.raises(ValueError, match="REWEAR_IMAGE_SEARCH"):
        Settings(image_search_provider="unknown-provider")
    with pytest.raises(ValueError, match="REWEAR_VISION_PROJECT"):
        Settings(vision_project="bad project / header injection")
    legacy_attempt = {
        "query": "blue shirt",
        "status": "no_results",
        "retrieved_count": 0,
        "candidate_count": 0,
        "error": None,
    }
    old = ShoppingResult.model_validate(shopping_fixture(search_attempts=[legacy_attempt]))
    assert old.search_attempts[0].kind == "text"
    attempts = [{**legacy_attempt, "kind": "image"}, legacy_attempt, legacy_attempt]
    current = ShoppingResult.model_validate(shopping_fixture(search_attempts=attempts))
    assert [attempt.kind for attempt in current.search_attempts] == ["image", "text", "text"]
    with pytest.raises(ValueError):
        ShoppingResult.model_validate(shopping_fixture(search_attempts=attempts + [legacy_attempt]))

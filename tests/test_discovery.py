"""Local discovery lifecycle checks; simulated providers do not establish brand accuracy."""

import asyncio
import csv
import io
import sqlite3
import threading
import time
from collections import deque
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from wardrobe.app import create_app
from wardrobe.config import Settings
from wardrobe.discovery import query_links
from wardrobe.providers import ProviderError
from wardrobe.schemas import unknown_garment


RECORD = {
    **unknown_garment(),
    "category": "top",
    "subtype": "shirt",
    "brand": "Saved catalogue brand",
    "colors": ["blue"],
    "condition": "good",
    "confidence": 0.86,
    "needs_review": False,
}


def test_free_search_links_offer_shopping_and_web_routes_without_model_state():
    links = query_links("Acme blue plaid shirt", "TW")
    assert [link["label"] for link in links] == [
        "Google Shopping",
        "Bing Shopping",
        "Google Web Search",
    ]
    assert all(link["url"].startswith("https://") for link in links)
    assert "tbm=shop" in links[0]["url"] and "gl=tw" in links[0]["url"]
    assert "cc=tw" in links[1]["url"] and "Acme+blue+plaid+shirt" in links[1]["url"]


def identity_fixture(**changes):
    return {
        "status": "observed",
        "brand": "Observed fixture brand",
        "evidence_type": "label",
        "visible_text": "OBSERVED FIXTURE BRAND FIXTURE-42",
        "evidence": "Synthetic provider response for lifecycle testing, not image recognition.",
        "confidence": 0.9,
        "product_code": "FIXTURE-42",
        "search_terms": ["Observed fixture brand", "blue shirt"],
        "visual_profile": {
            "type": "shirt",
            "category": "top",
            "primary_color": "blue",
            "pattern": "solid",
            "sleeve_length": "short",
            "neckline": "collared",
            "description": "A plain blue shirt.",
        },
        **changes,
    }


def shopping_fixture(**changes):
    return {
        "status": "searched",
        "query": "Observed fixture brand blue shirt",
        "offers": [
            {
                "title": "Fixture blue shirt",
                "url": "https://www.nike.com/tw/t/synthetic-test-fixture-blue-shirt",
                "merchant": "Fixture merchant",
                "match": "model_candidate",
                "reason": "Synthetic grounded candidate for integration testing.",
            }
        ],
        "sources": [
            {
                "title": "Fixture product source",
                "url": "https://www.nike.com/tw/t/synthetic-test-fixture-blue-shirt",
                "snippet": "Synthetic test source describing a blue shirt, product code FIXTURE-42.",
            }
        ],
        "searched_at": "2026-10-21T00:00:00+00:00",
        "notice": "Synthetic search results; no purchase or model accuracy claim.",
        **changes,
    }


class InspectionOnly:
    async def health(self):
        return {"local": {"available": True}, "meta": {"configured": True}}

    async def inspect(self, path, provider, *, context_path=None, wearing=False):
        return {
            "garment": deepcopy(RECORD),
            "target": "one_garment",
            "visibility": "complete",
            "reason": None,
        }

    async def close(self):
        pass


class DiscoveryProviders(InspectionOnly):
    def __init__(self, identify=(), shopping=(), verify=(), identity=None, result=None):
        self.identify_outcomes = deque(identify)
        self.shopping_outcomes = deque(shopping)
        self.verify_outcomes = deque(verify)
        self.identity = deepcopy(identity if identity is not None else identity_fixture())
        self.shopping = deepcopy(result if result is not None else shopping_fixture())
        self.identify_calls, self.shopping_calls, self.verify_calls = [], [], []
        self.started = {phase: threading.Event() for phase in ("identify", "shopping", "verify")}
        self.cancelled = {phase: threading.Event() for phase in ("identify", "shopping", "verify")}
        self.release = {phase: threading.Event() for phase in ("identify", "shopping", "verify")}
        self.cleanup_finished = threading.Event()
        self.reference_during_cleanup = None

    async def _outcome(self, phase, outcomes):
        outcome = outcomes.popleft() if outcomes else "success"
        self.started[phase].set()
        if outcome == "hold":
            while not self.release[phase].is_set():
                await asyncio.sleep(0.01)
        if outcome in {"block", "delayed_cancel"}:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled[phase].set()
                if outcome == "delayed_cancel":
                    await asyncio.sleep(0.08)
                    self.reference_during_cleanup = self.identify_calls[0]["cutout"].is_file()
                self.cleanup_finished.set()
                raise
        if outcome == "failure":
            raise ProviderError(f"Synthetic {phase} upstream failure")
        if outcome == "unexpected_failure":
            raise RuntimeError("synthetic-private-key-and-internal-path")

    async def identify_brand(self, cutout, context, record):
        assert cutout.is_file()
        with Image.open(cutout) as image:
            image.verify()
        if context is not None:
            assert context.is_file()
        self.identify_calls.append(
            {
                "cutout": Path(cutout),
                "context": Path(context) if context is not None else None,
                "record": deepcopy(record),
            }
        )
        await self._outcome("identify", self.identify_outcomes)
        return deepcopy(self.identity)

    async def search_shopping(self, identity, record, market="TW"):
        self.shopping_calls.append(
            {"identity": deepcopy(identity), "record": deepcopy(record), "market": market}
        )
        await self._outcome("shopping", self.shopping_outcomes)
        return deepcopy(self.shopping)

    async def verify_shopping(self, identity, record, shopping, cutout, context, destination, market="TW"):
        self.verify_calls.append({"destination": destination, "cutout": cutout, "context": context})
        result = {**deepcopy(shopping), "version": 2, "checked_candidates": []}
        for index, offer in enumerate(result["offers"][:3], 1):
            Image.new("RGB", (64, 80), "blue").save(destination / f"{index}.png")
            offer["verification"] = {
                "status": "verified",
                "page_url": offer["url"],
                "page_checked_at": "2026-10-21T00:00:00+00:00",
                "market": "in_scope",
                "market_evidence": "Fixture TW product page",
                "availability": "in_stock",
                "image_url": f"{index}.png",
                "product_code": identity.get("product_code"),
                "comparison": {
                    key: "match"
                    for key in (
                        "category",
                        "primary_color",
                        "pattern",
                        "sleeve_length",
                        "neckline",
                        "design_details",
                    )
                },
            }
            offer["reason"] = (
                "Product image matches test features; style number 'FIXTURE-42' matches the test page."
            )
            result["checked_candidates"].append(
                {
                    "title": offer["title"],
                    "url": offer["url"],
                    "status": "accepted",
                    "reasons": [offer["reason"]],
                }
            )
        await self._outcome("verify", self.verify_outcomes)
        return result


class CombinedProviders(DiscoveryProviders):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.reconstruct_started = threading.Event()
        self.reconstruct_release = threading.Event()
        self.reconstruct_cancelled = threading.Event()
        self.reconstruct_calls = []
        self.retry_on_cancel = None

    def schedule_retry(self):
        if self.retry_on_cancel is not None:
            callback, self.retry_on_cancel = self.retry_on_cancel, None
            asyncio.get_running_loop().call_soon(callback)

    async def identify_brand(self, *args):
        try:
            return await super().identify_brand(*args)
        except asyncio.CancelledError:
            self.schedule_retry()
            raise

    async def reconstruct(self, cutout, context, record, destination):
        assert cutout.is_file()
        self.reconstruct_calls.append(destination)
        self.reconstruct_started.set()
        try:
            while not self.reconstruct_release.is_set():
                await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            self.reconstruct_cancelled.set()
            self.schedule_retry()
            raise
        Image.new("RGB", (64, 80), "blue").save(destination, "PNG")


@contextmanager
def studio(directory, provider=None, *, api_key="synthetic-key-no-model-calls"):
    provider = provider if provider is not None else DiscoveryProviders()
    app = create_app(Settings(data_dir=directory, api_key=api_key), providers=provider)
    with TestClient(app) as client:
        yield client, app, provider


def seed_item(app, *, record=None, quality_status="ready", job=None, discovery=None):
    store, data_dir = app.state.store, app.state.settings.data_dir
    if job is None:
        ident = uuid4().hex
        job = store.create_job(
            id=ident,
            status="completed",
            mode="outfit",
            media_type="image",
            input_name="source.jpg",
            source_url=f"/media/{ident}/source.jpg",
        )
    directory = data_dir / "jobs" / job["id"]
    directory.mkdir(parents=True, exist_ok=True)
    paths = {}
    for filename, size in (("source.jpg", (120, 160)), ("cutout.png", (64, 80)), ("context.jpg", (90, 110))):
        path = directory / filename
        if not path.exists():
            Image.new("RGB", size, (30, 90, 170)).save(path)
        paths[filename] = path
    item = store.add_item(
        job["id"],
        record={**deepcopy(RECORD), **(record or {})},
        model_record=deepcopy(RECORD),
        reviewed=False,
        quality={
            "version": 3,
            "status": quality_status,
            "visibility": "complete",
            "model_target": "one_garment",
        },
        image_url=f"/media/{job['id']}/context.jpg",
        cutout_url=f"/media/{job['id']}/cutout.png",
        source_url=f"/media/{job['id']}/source.jpg",
        original_url=f"/media/{job['id']}/source.jpg",
        visual_signature={"version": "synthetic-discovery-evidence"},
        discovery=deepcopy(discovery),
    )
    return item, paths


def start(client, item_id, market=None):
    kwargs = {} if market is None else {"json": {"market": market}}
    response = client.post(f"/api/items/{item_id}/discovery", **kwargs)
    assert response.status_code == 202, response.text
    return response.json()


def wait_item(client, item_id, status=None, timeout=6):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/api/items/{item_id}")
        assert response.status_code == 200
        item = response.json()
        discovery = item.get("discovery")
        if discovery and (
            discovery["status"] == status if status else discovery["status"] not in {"queued", "running"}
        ):
            return item
        time.sleep(0.01)
    pytest.fail(f"Discovery did not reach the expected status: {item}")


def unchanged_evidence(before, after):
    assert {key: value for key, value in before.items() if key != "discovery"} == {
        key: value for key, value in after.items() if key != "discovery"
    }


def test_success_preserves_observed_catalogue_and_uses_original_references(tmp_path):
    with studio(tmp_path) as (client, app, provider):
        item, paths = seed_item(app)
        original_bytes = {name: path.read_bytes() for name, path in paths.items()}
        generated = paths["cutout.png"].parent / "reconstructions" / f"{uuid4().hex}.png"
        generated.parent.mkdir()
        Image.new("RGB", (64, 80), "red").save(generated)
        item = app.state.store.update_reconstruction(
            item["id"],
            {
                "id": generated.stem,
                "status": "completed",
                "is_synthetic": True,
                "image_url": f"/media/{item['job_id']}/reconstructions/{generated.name}",
            },
        )
        accepted = start(client, item["id"])
        assert accepted["discovery"]["market"] == "TW"
        done = wait_item(client, item["id"], "completed")
        unchanged_evidence(item, done)
        result = done["discovery"]["result"]
        assert result["identity"] == provider.identity
        assert result["shopping"]["status"] == "searched"
        assert result["shopping"]["sources"] == provider.shopping["sources"]
        assert result["shopping"]["offers"][0]["url"] == provider.shopping["offers"][0]["url"]
        assert result["shopping"]["offers"][0]["title"] == provider.shopping["sources"][0]["title"]
        assert result["shopping"]["offers"][0]["merchant"] == "nike.com"
        assert result["shopping"]["offers"][0]["match"] == "model_candidate"
        assert "FIXTURE-42" in result["shopping"]["offers"][0]["reason"]
        assert result["shopping"]["version"] == 2
        verification = result["shopping"]["offers"][0]["verification"]
        assert verification["status"] == "verified"
        assert verification["image_url"].startswith(
            f"/media/{item['job_id']}/shopping/{done['discovery']['id']}/"
        )
        assert client.get(verification["image_url"]).status_code == 200
        assert result["shopping"]["notice"]
        assert result["search_links"] and result["source_urls"]
        assert all(
            url in {item["cutout_url"], item["image_url"], item["source_url"], item["original_url"]}
            for url in result["source_urls"]
        )
        assert len(provider.identify_calls) == len(provider.shopping_calls) == 1
        assert provider.identify_calls[0]["cutout"] == paths["cutout.png"]
        assert provider.identify_calls[0]["context"] in {paths["source.jpg"], paths["context.jpg"]}
        assert provider.shopping_calls[0]["market"] == "TW"
        assert provider.shopping_calls[0]["identity"] == provider.identity
        assert provider.identify_calls[0]["record"] == provider.shopping_calls[0]["record"] == item["record"]
        assert provider.verify_calls[0]["cutout"] == paths["cutout.png"]
        assert provider.verify_calls[0]["context"] == paths["context.jpg"]
        assert {name: path.read_bytes() for name, path in paths.items()} == original_bytes
        assert client.get(f"/api/export?format=json&job_id={item['job_id']}").json()["items"] == [done]


def test_active_request_same_market_is_idempotent_and_other_market_conflicts(tmp_path):
    provider = DiscoveryProviders(identify=["block"])
    with studio(tmp_path, provider) as (client, app, _):
        item, _ = seed_item(app)
        first = start(client, item["id"])
        assert provider.started["identify"].wait(5)
        repeated = start(client, item["id"], "TW")
        assert repeated["discovery"]["id"] == first["discovery"]["id"]
        response = client.post(f"/api/items/{item['id']}/discovery", json={"market": "global"})
        assert response.status_code == 409
        assert len(provider.identify_calls) == 1 and provider.shopping_calls == []
        response = client.post(f"/api/items/{item['id']}/discovery/cancel")
        assert response.status_code == 200
        cancelled = wait_item(client, item["id"], "cancelled")
        unchanged_evidence(item, cancelled)
        assert provider.cancelled["identify"].is_set()


@pytest.mark.parametrize("failure", ["failure", "unexpected_failure"])
def test_shopping_failure_retains_brand_evidence_as_a_partial_result(tmp_path, failure):
    provider = DiscoveryProviders(shopping=[failure])
    with studio(tmp_path, provider) as (client, app, _):
        item, _ = seed_item(app)
        start(client, item["id"])
        partial = wait_item(client, item["id"], "partial")
        unchanged_evidence(item, partial)
        state = partial["discovery"]
        assert state["error"] and "synthetic-private-key" not in state["error"]
        assert state["result"]["identity"] == provider.identity
        assert state["result"]["shopping"]["status"] == "unavailable"
        assert state["result"]["shopping"]["offers"] == []
        assert state["result"]["search_links"]
        assert len(provider.identify_calls) == len(provider.shopping_calls) == 1


@pytest.mark.parametrize("failure", ["failure", "unexpected_failure"])
def test_complete_failure_keeps_previous_result_and_retry_can_change_market(tmp_path, failure):
    provider = DiscoveryProviders(identify=["success", failure, "success"])
    with studio(tmp_path, provider) as (client, app, _):
        item, _ = seed_item(app)
        start(client, item["id"])
        previous = wait_item(client, item["id"], "completed")
        accepted = start(client, item["id"], "global")
        assert accepted["discovery"]["id"] != previous["discovery"]["id"]
        assert accepted["discovery"]["result"] == previous["discovery"]["result"]
        failed = wait_item(client, item["id"], "failed")
        unchanged_evidence(item, failed)
        assert failed["discovery"]["market"] == "global"
        assert failed["discovery"]["result"] == previous["discovery"]["result"]
        assert failed["discovery"]["error"] and "synthetic-private-key" not in failed["discovery"]["error"]
        assert len(provider.identify_calls) == 2 and len(provider.shopping_calls) == 1
        start(client, item["id"], "global")
        completed = wait_item(client, item["id"], "completed")
        unchanged_evidence(item, completed)
        assert completed["discovery"]["result"]["market"] == "global"
        assert provider.shopping_calls[-1]["market"] == "global"
        assert completed["discovery"]["error"] is None


def test_unknown_brand_still_searches_similar_clothing_without_filling_catalogue_brand(tmp_path):
    identity = identity_fixture(
        status="unknown",
        brand=None,
        evidence_type="none",
        visible_text=None,
        confidence=0.0,
        product_code=None,
        search_terms=["blue shirt"],
    )
    shopping = shopping_fixture(
        offers=[
            {
                "title": "Similar blue shirt",
                "url": "https://www.nike.com/tw/t/synthetic-test-fixture-blue-shirt",
                "merchant": "Fixture merchant",
                "match": "similar",
                "reason": "Similar attributes, no brand identity claim.",
            }
        ]
    )
    provider = DiscoveryProviders(identity=identity, result=shopping)
    with studio(tmp_path, provider) as (client, app, _):
        item, _ = seed_item(app, record={"brand": None})
        start(client, item["id"])
        completed = wait_item(client, item["id"], "completed")
        unchanged_evidence(item, completed)
        assert completed["record"]["brand"] is None
        result = completed["discovery"]["result"]
        assert result["identity"]["status"] == "unknown" and result["identity"]["brand"] is None
        assert result["shopping"]["offers"][0]["match"] == "similar"
        assert provider.shopping_calls[0]["identity"]["brand"] is None


def test_no_results_keeps_search_links_but_does_not_invent_an_offer(tmp_path):
    provider = DiscoveryProviders(result=shopping_fixture(status="no_results", offers=[], sources=[]))
    with studio(tmp_path, provider) as (client, app, _):
        item, _ = seed_item(app)
        start(client, item["id"])
        completed = wait_item(client, item["id"], "completed")
        result = completed["discovery"]["result"]
        assert result["shopping"]["status"] == "no_results"
        assert result["shopping"]["offers"] == [] and result["shopping"]["sources"] == []
        assert result["search_links"]
        assert all(link["label"] and link["url"].startswith("https://") for link in result["search_links"])


@pytest.mark.parametrize("phase", ["identify", "shopping", "verify"])
def test_cancellation_in_each_phase_stops_provider_and_preserves_prior_result(tmp_path, phase):
    provider = DiscoveryProviders(
        identify=["success", "block"] if phase == "identify" else [],
        shopping=["success", "block"] if phase == "shopping" else [],
        verify=["success", "block"] if phase == "verify" else [],
    )
    with studio(tmp_path, provider) as (client, app, _):
        item, _ = seed_item(app)
        start(client, item["id"])
        previous = wait_item(client, item["id"], "completed")
        provider.started[phase].clear()
        start(client, item["id"])
        assert provider.started[phase].wait(5)
        response = client.post(f"/api/items/{item['id']}/discovery/cancel")
        assert response.status_code == 200
        cancelled = wait_item(client, item["id"], "cancelled")
        unchanged_evidence(item, cancelled)
        assert cancelled["discovery"]["result"] == previous["discovery"]["result"]
        assert provider.cancelled[phase].is_set()
        assert client.post(f"/api/items/{item['id']}/discovery/cancel").json() == cancelled
        assert not (tmp_path / "jobs" / item["job_id"] / "shopping" / cancelled["discovery"]["id"]).exists()
        old_image = previous["discovery"]["result"]["shopping"]["offers"][0]["verification"]["image_url"]
        assert client.get(old_image).status_code == 200


def test_cancelling_queued_item_never_calls_either_provider(tmp_path):
    provider = DiscoveryProviders(identify=["block", "success"])
    with studio(tmp_path, provider) as (client, app, _):
        first, _ = seed_item(app)
        second, _ = seed_item(app)
        start(client, first["id"])
        assert provider.started["identify"].wait(5)
        queued = start(client, second["id"])
        assert queued["discovery"]["status"] == "queued"
        assert client.post(f"/api/items/{second['id']}/discovery/cancel").status_code == 200
        cancelled = wait_item(client, second["id"], "cancelled")
        unchanged_evidence(second, cancelled)
        assert len(provider.identify_calls) == 1 and provider.shopping_calls == []
        assert client.get(f"/api/items/{first['id']}").json()["discovery"]["status"] == "running"


@pytest.mark.parametrize(
    "kind",
    [
        "missing",
        "foreign_job",
        "outside_symlink",
        "synthetic_reference",
        "synthetic_symlink",
        "foreign_context",
        "shopping_reference",
        "shopping_symlink",
    ],
)
def test_brand_evidence_rejects_missing_foreign_or_synthetic_sources(tmp_path, kind):
    with studio(tmp_path) as (client, app, provider):
        item, paths = seed_item(app)
        if kind == "missing":
            paths["cutout.png"].unlink()
        elif kind in {"foreign_job", "foreign_context"}:
            other, _ = seed_item(app)
            changes = {"cutout_url" if kind == "foreign_job" else "image_url": other["cutout_url"]}
            item = app.state.store.update_item(item["id"], **changes)
        elif kind == "outside_symlink":
            outside = tmp_path / "private.png"
            paths["cutout.png"].rename(outside)
            paths["cutout.png"].symlink_to(outside)
        else:
            folder = "shopping" if kind.startswith("shopping") else "reconstructions"
            output = paths["cutout.png"].parent / folder
            output.mkdir()
            paths["cutout.png"].rename(output / "old.png")
            if kind.endswith("symlink"):
                paths["cutout.png"].symlink_to(output / "old.png")
            else:
                item = app.state.store.update_item(
                    item["id"], cutout_url=f"/media/{item['job_id']}/{folder}/old.png"
                )
        response = client.post(f"/api/items/{item['id']}/discovery")
        assert response.status_code == 409
        assert provider.identify_calls == provider.shopping_calls == []
        assert app.state.store.item(item["id"]) == item


def test_generated_job_cannot_produce_real_world_brand_claims(tmp_path):
    with studio(tmp_path) as (client, app, provider):
        job = app.state.store.create_job(
            status="completed", media_type="generated", mode="single", is_demo=False
        )
        item, _ = seed_item(app, job=job)
        response = client.post(f"/api/items/{item['id']}/discovery")
        assert response.status_code == 422
        assert provider.identify_calls == provider.shopping_calls == []
        assert app.state.store.item(item["id"]) == item


@pytest.mark.parametrize("status", ["queued", "running"])
def test_restart_fails_interrupted_discovery_without_resending_or_dropping_result(tmp_path, status):
    provider = DiscoveryProviders()
    app = create_app(Settings(data_dir=tmp_path, api_key="synthetic-key"), providers=provider)
    old_result = {
        "identity": identity_fixture(),
        "shopping": shopping_fixture(),
        "search_links": [],
        "source_urls": [],
        "market": "TW",
    }
    state = {
        "id": uuid4().hex,
        "status": status,
        "stage": "Interrupted fixture",
        "market": "TW",
        "error": None,
        "result": old_result,
    }
    item, _ = seed_item(app, discovery=state)
    with TestClient(app) as client:
        recovered = client.get(f"/api/items/{item['id']}").json()
        unchanged_evidence(item, recovered)
        assert recovered["discovery"]["status"] == "failed" and recovered["discovery"]["error"]
        assert recovered["discovery"]["id"] == state["id"] and recovered["discovery"]["result"] == old_result
        assert provider.identify_calls == provider.shopping_calls == []
        assert app.state.discovery.tasks == {}
        start(client, item["id"])
        completed = wait_item(client, item["id"], "completed")
        assert completed["discovery"]["id"] != state["id"]
        unchanged_evidence(item, completed)


@pytest.mark.parametrize("reason", ["no_key", "no_provider"])
def test_unavailable_discovery_does_not_change_item_or_call_provider(tmp_path, reason):
    provider = DiscoveryProviders() if reason == "no_key" else InspectionOnly()
    with studio(tmp_path, provider, api_key=None if reason == "no_key" else "synthetic-key") as (
        client,
        app,
        _,
    ):
        item, _ = seed_item(app)
        assert client.post(f"/api/items/{item['id']}/discovery").status_code == 503
        assert app.state.store.item(item["id"]) == item
        assert getattr(provider, "identify_calls", []) == []


@pytest.mark.parametrize("market", ["CN", "", True, None])
def test_invalid_market_is_rejected_without_starting_discovery(tmp_path, market):
    with studio(tmp_path) as (client, app, provider):
        item, _ = seed_item(app)
        response = client.post(f"/api/items/{item['id']}/discovery", json={"market": market})
        assert response.status_code == 422
        assert provider.identify_calls == provider.shopping_calls == []
        assert app.state.store.item(item["id"]) == item


def test_finishing_discovery_preserves_concurrent_human_changes(tmp_path):
    provider = DiscoveryProviders(shopping=["hold"])
    with studio(tmp_path, provider) as (client, app, _):
        item, _ = seed_item(app)
        start(client, item["id"])
        assert provider.started["shopping"].wait(5)
        edited = client.patch(
            f"/api/items/{item['id']}", json={"record": {"brand": "Human verified brand"}, "reviewed": True}
        )
        assert edited.status_code == 200
        provider.release["shopping"].set()
        completed = wait_item(client, item["id"], "completed")
        unchanged_evidence(edited.json(), completed)
        assert completed["discovery"]["result"]["reference_record"] == item["record"]
        assert provider.shopping_calls[0]["record"] == item["record"]


def test_csv_export_keeps_brand_candidate_separate_and_escapes_formula_content(tmp_path):
    formula = '=HYPERLINK("https://example.invalid","fixture")'
    provider = DiscoveryProviders(
        identity=identity_fixture(
            brand=formula,
            evidence="+fixture evidence",
            product_code="@fixture-code",
            visible_text=formula + " @fixture-code",
        )
    )
    with studio(tmp_path, provider) as (client, app, _):
        item, _ = seed_item(app)
        start(client, item["id"])
        completed = wait_item(client, item["id"], "completed")
        result = completed["discovery"]["result"]
        response = client.get("/api/export?format=csv")
        assert response.status_code == 200
        rows = list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"))))
        assert len(rows) == 1 and rows[0]["brand"] == item["record"]["brand"]
        assert rows[0]["discovery_status"] == "completed"
        assert rows[0]["brand_candidate"] == "'" + formula
        assert rows[0]["brand_evidence"] == "'+fixture evidence"
        assert rows[0]["product_code"] == "'@fixture-code"
        assert rows[0]["brand_evidence_status"] == "observed"
        assert rows[0]["shopping_status"] == "searched" and rows[0]["shopping_market"] == "TW"
        assert rows[0]["shopping_checked_at"] == result["shopping"]["searched_at"]
        assert result["shopping"]["offers"][0]["url"] in rows[0]["shopping_urls"]
        exported = client.get("/api/export?format=json").json()["items"][0]
        assert exported == completed and exported["discovery"]["result"]["identity"]["brand"] == formula


@pytest.mark.parametrize("target", ["item", "job"])
@pytest.mark.parametrize("phase", ["identify", "shopping", "verify"])
def test_deletion_waits_for_provider_cleanup_before_removing_sources(tmp_path, target, phase):
    provider = DiscoveryProviders(
        identify=["delayed_cancel"] if phase == "identify" else [],
        shopping=["delayed_cancel"] if phase == "shopping" else [],
        verify=["delayed_cancel"] if phase == "verify" else [],
    )
    with studio(tmp_path, provider) as (client, app, _):
        item, _ = seed_item(app)
        start(client, item["id"])
        assert provider.started[phase].wait(5)
        route = f"/api/items/{item['id']}" if target == "item" else f"/api/jobs/{item['job_id']}"
        assert client.delete(route).status_code == 204
        assert provider.cancelled[phase].is_set() and provider.cleanup_finished.is_set()
        assert provider.reference_during_cleanup is True
        assert app.state.store.item(item["id"]) is None
        assert item["id"] not in app.state.discovery.tasks


@pytest.mark.parametrize("target", ["item", "job"])
def test_deletion_blocks_new_discovery_during_cancellation_cleanup(tmp_path, target):
    class ConcurrentSubmission(DiscoveryProviders):
        retry = None

        async def identify_brand(self, *args):
            try:
                return await super().identify_brand(*args)
            except asyncio.CancelledError:
                if self.retry is not None:
                    callback, self.retry = self.retry, None
                    asyncio.get_running_loop().call_soon(callback)
                raise

    provider = ConcurrentSubmission(identify=["block", "hold"])
    with studio(tmp_path, provider) as (client, app, _):
        item, _ = seed_item(app)
        submitted = []

        def resubmit():
            try:
                submitted.append(app.state.discovery.enqueue(item["id"], "TW"))
            except ValueError as error:
                submitted.append(getattr(error, "status_code", None))

        provider.retry = resubmit
        start(client, item["id"])
        assert provider.started["identify"].wait(5)
        route = f"/api/items/{item['id']}" if target == "item" else f"/api/jobs/{item['job_id']}"
        assert client.delete(route).status_code == 204
        assert len(submitted) == 1 and submitted[0] in (404, 409)
        assert len(provider.identify_calls) == 1 and provider.shopping_calls == []
        assert app.state.store.item(item["id"]) is None and item["id"] not in app.state.discovery.tasks


@pytest.mark.parametrize("target", ["item", "job"])
def test_delete_rollback_restores_media_and_releases_discovery_guard(tmp_path, target):
    provider = DiscoveryProviders(identify=["block", "success"])
    with studio(tmp_path, provider) as (client, app, _):
        item, paths = seed_item(app)
        original = {name: path.read_bytes() for name, path in paths.items()}
        with app.state.store.connect() as db:
            db.execute(
                "CREATE TRIGGER discovery_test_delete_failure BEFORE DELETE ON items BEGIN SELECT RAISE(ABORT, 'synthetic delete failure'); END"
            )
        start(client, item["id"])
        assert provider.started["identify"].wait(5)
        route = f"/api/items/{item['id']}" if target == "item" else f"/api/jobs/{item['job_id']}"
        with pytest.raises(sqlite3.IntegrityError, match="synthetic delete failure"):
            client.delete(route)
        assert app.state.store.job(item["job_id"]) is not None
        restored = app.state.store.item(item["id"])
        unchanged_evidence(item, restored)
        assert restored["discovery"]["status"] == "cancelled"
        assert {name: path.read_bytes() for name, path in paths.items()} == original
        with app.state.store.connect() as db:
            db.execute("DROP TRIGGER discovery_test_delete_failure")
        start(client, item["id"])
        completed = wait_item(client, item["id"], "completed")
        unchanged_evidence(item, completed)
        assert len(provider.identify_calls) == 2 and len(provider.shopping_calls) == 1


@pytest.mark.parametrize("scope", ["job", "batch"])
def test_job_or_batch_cancel_also_cancels_discovery_for_completed_analysis(tmp_path, scope):
    provider = DiscoveryProviders(identify=["block", "success"])
    with studio(tmp_path, provider) as (client, app, _):
        if scope == "job":
            item, _ = seed_item(app)
            items = [item]
            route = f"/api/jobs/{item['job_id']}/cancel"
        else:
            batch = app.state.store.create_batch(
                [
                    {"status": "completed", "mode": "outfit", "input_name": "source.jpg"},
                    {"status": "completed", "mode": "outfit", "input_name": "source.jpg"},
                ],
                file_count=2,
                provider="local",
                mode="outfit",
                concept="garment",
            )
            items = [seed_item(app, job=job)[0] for job in batch["jobs"]]
            route = f"/api/batches/{batch['id']}/cancel"
        for item in items:
            start(client, item["id"])
        assert provider.started["identify"].wait(5)
        assert client.post(route).status_code == 200
        for item in items:
            cancelled = wait_item(client, item["id"], "cancelled")
            unchanged_evidence(item, cancelled)
            assert app.state.store.job(item["job_id"])["status"] == "completed"
        assert len(provider.identify_calls) == 1 and provider.shopping_calls == []


def test_queued_discovery_rechecks_missing_source_before_calling_provider(tmp_path):
    provider = DiscoveryProviders(identify=["block", "success"])
    with studio(tmp_path, provider) as (client, app, _):
        first, _ = seed_item(app)
        second, paths = seed_item(app)
        start(client, first["id"])
        assert provider.started["identify"].wait(5)
        start(client, second["id"])
        paths["cutout.png"].unlink()
        assert client.post(f"/api/items/{first['id']}/discovery/cancel").status_code == 200
        failed = wait_item(client, second["id"], "failed")
        unchanged_evidence(second, failed)
        assert failed["discovery"]["result"] is None and failed["discovery"]["error"]
        assert len(provider.identify_calls) == 1 and provider.shopping_calls == []


def test_discovery_queue_is_bounded_and_provider_concurrency_is_one(tmp_path):
    provider = DiscoveryProviders(identify=["block"])
    with studio(tmp_path, provider) as (client, app, _):
        first, _ = seed_item(app)
        job = app.state.store.job(first["job_id"])
        items = [first] + [seed_item(app, job=job)[0] for _ in range(64)]
        start(client, first["id"])
        assert provider.started["identify"].wait(5)
        for item in items[1:64]:
            assert start(client, item["id"])["discovery"]["status"] == "queued"
        assert client.post(f"/api/items/{items[64]['id']}/discovery").status_code == 429
        assert app.state.store.item(items[64]["id"]).get("discovery") is None
        assert len(provider.identify_calls) == 1 and provider.shopping_calls == []
        assert client.post(f"/api/jobs/{job['id']}/cancel").status_code == 200
        assert all(
            app.state.store.item(item["id"])["discovery"]["status"] == "cancelled" for item in items[:64]
        )


def test_inactive_cancel_is_idempotent_and_missing_items_remain_absent(tmp_path):
    with studio(tmp_path) as (client, app, provider):
        item, _ = seed_item(app)
        response = client.post(f"/api/items/{item['id']}/discovery/cancel")
        assert response.status_code == 200 and response.json() == item
        start(client, item["id"])
        completed = wait_item(client, item["id"], "completed")
        assert client.post(f"/api/items/{item['id']}/discovery/cancel").json() == completed
        for suffix in ("", "/cancel"):
            assert client.post(f"/api/items/absent/discovery{suffix}").status_code == 404
        assert len(provider.identify_calls) == len(provider.shopping_calls) == 1


def test_new_analysis_does_not_start_brand_or_shopping_search_automatically(tmp_path):
    with studio(tmp_path) as (client, _, provider):
        source = io.BytesIO()
        Image.new("RGB", (100, 120), "blue").save(source, "JPEG")
        response = client.post(
            "/api/jobs",
            files={"file": ("shirt.jpg", source.getvalue(), "image/jpeg")},
            data={"mode": "single"},
        )
        assert response.status_code == 202
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            job = client.get(f"/api/jobs/{response.json()['id']}").json()
            if job["status"] not in {"queued", "running"}:
                break
            time.sleep(0.01)
        assert job["status"] == "completed" and len(job["items"]) == 1
        assert job["items"][0].get("discovery") is None
        assert provider.identify_calls == provider.shopping_calls == []


@pytest.mark.parametrize("status", ["searched", "unavailable"])
def test_ungrounded_candidates_are_not_emitted_as_purchase_offers(tmp_path, status):
    provider = DiscoveryProviders(result=shopping_fixture(status=status, sources=[]))
    with studio(tmp_path, provider) as (client, app, _):
        item, _ = seed_item(app)
        start(client, item["id"])
        done = wait_item(client, item["id"], "partial" if status == "unavailable" else "completed")
        unchanged_evidence(item, done)
        result = done["discovery"]["result"]
        assert result["identity"] == provider.identity
        assert result["shopping"]["status"] == ("unavailable" if status == "unavailable" else "no_results")
        assert result["shopping"]["offers"] == [] and result["shopping"]["sources"] == []
        assert result["search_links"]


@pytest.mark.parametrize("origin", ["discovery", "reconstruction"])
@pytest.mark.parametrize("target", ["item", "job"])
def test_deletion_guards_both_services_before_either_cancellation_finishes(tmp_path, origin, target):
    provider = CombinedProviders(identify=["block"])
    with studio(tmp_path, provider) as (client, app, _):
        item, _ = seed_item(app)
        submitted = []
        other = app.state.reconstruction if origin == "discovery" else app.state.discovery

        def start_other_service():
            try:
                submitted.append(other.enqueue(item["id"]))
            except ValueError as error:
                submitted.append(getattr(error, "status_code", None))

        provider.retry_on_cancel = start_other_service
        response = client.post(f"/api/items/{item['id']}/{origin}")
        assert response.status_code == 202
        started = provider.started["identify"] if origin == "discovery" else provider.reconstruct_started
        assert started.wait(5)
        route = f"/api/items/{item['id']}" if target == "item" else f"/api/jobs/{item['job_id']}"
        assert client.delete(route).status_code == 204
        assert len(submitted) == 1 and submitted[0] in (404, 409)
        assert len(provider.identify_calls) == (1 if origin == "discovery" else 0)
        assert len(provider.reconstruct_calls) == (0 if origin == "discovery" else 1)
        assert app.state.discovery.tasks == app.state.reconstruction.tasks == {}


@pytest.mark.parametrize("first_to_finish", ["discovery", "reconstruction"])
def test_concurrent_discovery_and_reconstruction_keep_each_others_results_and_human_edits(
    tmp_path, first_to_finish
):
    provider = CombinedProviders(shopping=["hold"])
    with studio(tmp_path, provider) as (client, app, _):
        item, _ = seed_item(app)
        assert client.post(f"/api/items/{item['id']}/reconstruction").status_code == 202
        start(client, item["id"])
        assert provider.reconstruct_started.wait(5) and provider.started["shopping"].wait(5)
        response = client.patch(
            f"/api/items/{item['id']}", json={"record": {"brand": "Human edited brand"}, "reviewed": True}
        )
        assert response.status_code == 200
        edited = response.json()

        async def finish(service):
            task = getattr(app.state, service).tasks[item["id"]]
            (provider.release["shopping"] if service == "discovery" else provider.reconstruct_release).set()
            await asyncio.wait_for(asyncio.shield(task), timeout=5)

        client.portal.call(finish, first_to_finish)
        client.portal.call(finish, "reconstruction" if first_to_finish == "discovery" else "discovery")
        completed = client.get(f"/api/items/{item['id']}").json()
        assert completed["discovery"]["status"] == "completed"
        # This fixture writes an image without a fidelity report. Concurrent
        # discovery must preserve that unverified candidate, not promote it.
        assert completed["reconstruction"]["status"] == "needs_review"
        assert completed["reconstruction"]["image_url"] is None
        assert client.get(completed["reconstruction"]["candidate_url"]).status_code == 200
        assert {
            key: value for key, value in completed.items() if key not in {"discovery", "reconstruction"}
        } == {key: value for key, value in edited.items() if key not in {"discovery", "reconstruction"}}
        assert completed["discovery"]["result"]["reference_record"] == item["record"]
        assert completed["reconstruction"]["reference_record"] == item["record"]


def test_old_discovery_attempt_cannot_overwrite_a_newer_persisted_state(tmp_path):
    provider = DiscoveryProviders(shopping=["hold"])
    with studio(tmp_path, provider) as (client, app, _):
        item, _ = seed_item(app)
        accepted = start(client, item["id"])
        assert provider.started["shopping"].wait(5)
        saved = app.state.store.update_discovery(
            item["id"],
            {
                **accepted["discovery"],
                "id": uuid4().hex,
                "status": "failed",
                "stage": "failed",
                "error": "Newer saved state",
            },
        )

        async def finish_old_attempt():
            task = app.state.discovery.tasks[item["id"]]
            provider.release["shopping"].set()
            await asyncio.wait_for(asyncio.shield(task), timeout=5)

        client.portal.call(finish_old_attempt)
        assert app.state.store.item(item["id"]) == saved


@pytest.mark.parametrize("failure", ["failure", "unexpected_failure"])
def test_verification_failure_cleans_output_and_keeps_original_identity(tmp_path, failure):
    provider = DiscoveryProviders(verify=[failure])
    with studio(tmp_path, provider) as (client, app, _):
        item, _ = seed_item(app)
        start(client, item["id"])
        partial = wait_item(client, item["id"], "partial")
        assert partial["discovery"]["result"]["identity"] == provider.identity
        assert partial["discovery"]["result"]["shopping"]["version"] == 2
        assert not partial["discovery"]["result"]["shopping"]["offers"]
        assert not provider.verify_calls[0]["destination"].exists()
        unchanged_evidence(item, partial)


@pytest.mark.parametrize("kind", ["missing", "invalid_image", "outside_symlink", "remote", "foreign_attempt"])
def test_product_output_must_be_valid_image_in_the_current_attempt(tmp_path, kind):
    class InvalidImageProvider(DiscoveryProviders):
        async def verify_shopping(self, *args, **kwargs):
            result = await super().verify_shopping(*args, **kwargs)
            destination = self.verify_calls[-1]["destination"]
            image = destination / "1.png"
            if kind == "missing":
                image.unlink()
            elif kind == "invalid_image":
                image.write_text("not an image")
            elif kind == "outside_symlink":
                outside = tmp_path / "outside.png"
                image.rename(outside)
                image.symlink_to(outside)
            elif kind == "remote":
                result["offers"][0]["verification"]["image_url"] = "https://example.com/image.png"
            else:
                result["offers"][0]["verification"]["image_url"] = (
                    "/media/" + "a" * 32 + "/shopping/" + "b" * 32 + "/1.png"
                )
            return result

    provider = InvalidImageProvider()
    with studio(tmp_path, provider) as (client, app, _):
        item, _ = seed_item(app)
        start(client, item["id"])
        result = wait_item(client, item["id"])
        assert not result["discovery"]["result"]["shopping"]["offers"]
        assert not provider.verify_calls[0]["destination"].exists()
        unchanged_evidence(item, result)
        if kind == "outside_symlink":
            assert (tmp_path / "outside.png").is_file()


def test_verification_stale_attempt_discards_local_images_without_overwriting_state(tmp_path):
    provider = DiscoveryProviders(verify=["hold"])
    with studio(tmp_path, provider) as (client, app, _):
        item, _ = seed_item(app)
        accepted = start(client, item["id"])
        assert provider.started["verify"].wait(5)
        output = provider.verify_calls[0]["destination"]
        assert (output / "1.png").is_file()
        saved = app.state.store.update_discovery(
            item["id"],
            {
                **accepted["discovery"],
                "id": uuid4().hex,
                "status": "failed",
                "stage": "failed",
                "error": "Newer saved attempt",
            },
        )

        async def finish():
            task = app.state.discovery.tasks[item["id"]]
            provider.release["verify"].set()
            await asyncio.wait_for(asyncio.shield(task), timeout=5)

        client.portal.call(finish)
        assert app.state.store.item(item["id"]) == saved
        assert not output.exists()


@pytest.mark.parametrize("retained", [False, True])
def test_restart_removes_only_uncommitted_product_images(tmp_path, retained):
    provider = DiscoveryProviders()
    app = create_app(Settings(data_dir=tmp_path, api_key="synthetic-key"), providers=provider)
    attempt_id = uuid4().hex
    state = {"id": attempt_id, "status": "running", "stage": "verifying", "market": "TW", "result": None}
    item, _ = seed_item(app, discovery=state)
    directory = tmp_path / "jobs" / item["job_id"] / "shopping"
    unfinished = directory / attempt_id
    unfinished.mkdir(parents=True)
    Image.new("RGB", (64, 80), "blue").save(unfinished / "1.png")
    previous = directory / uuid4().hex
    previous.mkdir()
    Image.new("RGB", (64, 80), "blue").save(previous / "1.png")
    if retained:
        state["result"] = {
            "shopping": {
                "offers": [
                    {"verification": {"image_url": f"/media/{item['job_id']}/shopping/{attempt_id}/1.png"}}
                ]
            }
        }
        app.state.store.update_discovery(item["id"], state)
    with TestClient(app):
        assert unfinished.exists() == retained
        assert (previous / "1.png").is_file()
        assert app.state.store.item(item["id"])["discovery"]["status"] == "failed"
        assert not provider.verify_calls


def test_legacy_shopping_export_preserves_json_but_does_not_claim_verified_urls(tmp_path):
    with studio(tmp_path) as (client, app, _):
        item, _ = seed_item(
            app,
            discovery={
                "status": "completed",
                "result": {"shopping": shopping_fixture(), "identity": identity_fixture()},
            },
        )
        row = next(csv.DictReader(io.StringIO(client.get("/api/export?format=csv").text)))
        assert row["shopping_verification_version"] == "1"
        assert row["shopping_urls"] == ""
        exported = client.get("/api/export?format=json").json()["items"][0]
        assert exported["discovery"] == item["discovery"]


def test_failed_search_fallback_does_not_restore_uncertain_brand_or_logo_color(tmp_path):
    identity = identity_fixture(
        status="possible",
        brand="Speculative brand",
        evidence_type="logo",
        visible_text=None,
        product_code=None,
        visual_profile={
            "type": "T-shirt",
            "category": "top",
            "primary_color": None,
            "pattern": None,
            "sleeve_length": "short",
            "neckline": "crew",
            "description": None,
        },
    )
    provider = DiscoveryProviders(identity=identity, shopping=["failure"])
    with studio(tmp_path, provider) as (client, app, _):
        item, _ = seed_item(app, record={"colors": ["black"], "pattern": "floral"})
        start(client, item["id"])
        result = wait_item(client, item["id"], "partial")["discovery"]["result"]
        query = result["shopping"]["query"]
        assert "T-shirt" in query and "short" in query
        assert all(value not in query for value in ("Speculative", "black", "floral", "Saved"))


@pytest.mark.parametrize("alias", ["job", "jobs"])
def test_media_and_both_managers_reject_symlinked_job_roots(tmp_path, alias):
    provider = CombinedProviders()
    with studio(tmp_path / "data", provider) as (client, app, _):
        item, _ = seed_item(app)
        jobs = app.state.settings.data_dir / "jobs"
        original = jobs / item["job_id"] if alias == "job" else jobs
        target = tmp_path / "external"
        original.rename(target)
        original.symlink_to(target, target_is_directory=True)
        assert client.get(item["cutout_url"]).status_code == 404
        for service in ("reconstruction", "discovery"):
            assert client.post(f"/api/items/{item['id']}/{service}").status_code == 409
        assert not provider.identify_calls and not provider.reconstruct_calls
        assert app.state.store.item(item["id"]) == item
        assert target.is_dir()

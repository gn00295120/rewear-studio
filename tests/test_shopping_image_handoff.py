"""Manual image-search handoff must only expose eligible original evidence."""

import json
import subprocess

import pytest

from test_preview_ui_contract import fixture_item, shopping_result
from test_reconstruction_ui_contract import NODE, ROOT

pytestmark = pytest.mark.skipif(NODE is None, reason="The frontend contract needs Node")

PROBE = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('wardrobe/static/app.js', 'utf8');
const boundary = source.indexOf('  document.addEventListener("click", event => {');
if (boundary < 0) throw new Error('Update the UI event-binding boundary in this harness.');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const context = { URL, window: { location: { origin: 'http://127.0.0.1:18500' } }, input };
vm.runInNewContext(source.slice(0, boundary) + `
  state.jobs = input.jobs;
  globalThis.result = {
    reference: originalSearchReference(input.item),
    manual: manualImageSearch(input.item),
    discovery: discoveryResults(input.item),
    free: freeShoppingHandoff(input.item, input.market || 'TW'),
    query: freeShoppingQuery(input.item)
  };
})();`, context, { timeout: 2000 });
process.stdout.write(JSON.stringify(context.result));
"""


def probe(item, jobs=None, market="TW"):
    if jobs is None:
        jobs = [{"id": item["job_id"], "media_type": "image"}]
    process = subprocess.run(
        [NODE, "-e", PROBE],
        input=json.dumps({"item": item, "jobs": jobs, "market": market}),
        text=True,
        capture_output=True,
        cwd=ROOT,
        check=True,
    )
    return json.loads(process.stdout)


def test_prefers_original_cutout_even_when_ai_preview_exists():
    item = fixture_item("a")
    item["reconstruction"] = {"preview_url": "/media/fixture-job/reconstructions/preview.png"}
    result = probe(item)
    assert result["reference"]["url"].endswith("/media/fixture-job/cutout.jpg")
    assert "Download detected region" in result["manual"]
    assert 'href="https://lens.google/" target="_blank" rel="noopener noreferrer"' in result["manual"]
    assert "not verified for exact match or stock" in result["manual"]
    assert "preview.png" not in result["manual"]
    assert "Search with Original Photo" in result["discovery"]


def test_free_text_search_is_available_without_model_or_discovery_cost():
    item = fixture_item("free")
    item.update(reviewed=True, discovery=None)
    item["record"].update(brand="Human Brand", subtype="plaid overshirt", colors=["navy", "white"])
    result = probe(item)
    assert result["query"] == "Human Brand plaid overshirt navy white"
    assert "Quick Search" in result["free"] and "FREE" in result["free"]
    assert "Google Shopping" in result["free"] and "Bing Shopping" in result["free"]
    assert "Web Search" in result["free"] and result["free"].count('target="_blank"') == 3
    assert "tbm=shop" in result["free"] and "gl=tw" in result["free"]
    assert "not verified product pages" in result["free"]
    assert all('rel="noopener noreferrer"' in tag for tag in result["free"].split("<a ")[1:])


def test_unreviewed_model_brand_is_not_used_by_free_search():
    item = fixture_item("unreviewed")
    item["record"].update(brand="Unconfirmed Guess", subtype="shirt", colors=["blue"])
    result = probe(item)
    assert "Unconfirmed Guess" not in result["query"]
    assert result["query"] == "shirt blue"


def test_observed_brand_and_product_code_take_priority_in_free_search():
    item = fixture_item("observed")
    item["discovery"] = {"result": shopping_result()}
    item["discovery"]["result"]["identity"].update(
        status="observed", brand="Visible Brand", product_code="SKU-42"
    )
    result = probe(item, market="global")
    assert result["query"].startswith("Visible Brand SKU-42")
    assert "brand mark visible in the original photo" in result["free"]
    assert "gl=tw" not in result["free"] and "cc=tw" not in result["free"]


def test_missing_or_invalid_cutout_uses_original_context():
    item = fixture_item("a")
    item["cutout_url"] = "/media/another-job/cutout.jpg"
    result = probe(item)
    assert result["reference"]["url"].endswith("/media/fixture-job/context.jpg")
    assert result["reference"]["label"] == "Download original context"


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/cutout.jpg",
        "/media/another-job/cutout.jpg",
        "/media/fixture-job/reconstructions/preview.png",
        "/media/fixture-job/ReConstructions/preview.png",
        "/media/fixture-job/shopping/attempt/1.png",
        "/media/fixture-job/%72econstructions/preview.png",
        "/media/fixture-job/crops%2f..%2freconstructions/preview.png",
        "/media/fixture-job/cutout.jpg?source=preview",
        "/media/fixture-job/cutout.jpg#preview",
        "http://user:secret@127.0.0.1:18500/media/fixture-job/cutout.jpg",
        "javascript:alert(1)",
        "data:image/png;base64,AAAA",
        "/media/fixture-job/source.mp4",
    ],
)
def test_unsafe_or_synthetic_references_do_not_offer_a_download(url):
    item = fixture_item("a")
    item.update(cutout_url=url, image_url=None)
    result = probe(item)
    assert result["reference"] is None and result["manual"] == ""


def test_known_ai_url_is_rejected_even_if_claimed_to_be_in_crops():
    item = fixture_item("a")
    item["reconstruction"] = {"candidate_url": item["cutout_url"]}
    item["image_url"] = None
    assert probe(item)["reference"] is None


@pytest.mark.parametrize("kind", ["provenance", "job", "category", "quality"])
def test_generated_or_ineligible_items_have_no_manual_image_search(kind):
    item = fixture_item("a")
    item["provenance"] = "Meta / muse-spark-1.3"
    jobs = [{"id": item["job_id"], "media_type": "image"}]
    if kind == "provenance":
        item["provenance"] = "generated"
    elif kind == "job":
        jobs = [{"id": "fixture-job", "media_type": "generated"}]
    elif kind == "category":
        item["record"]["category"] = "other"
    else:
        item["quality"]["status"] = "needs_retake"
    result = probe(item, jobs)
    assert result["reference"] is None and result["manual"] == ""


@pytest.mark.parametrize("jobs", [[], [{"id": "another-job", "media_type": "image"}]])
def test_missing_source_job_never_treats_generated_provider_provenance_as_original(jobs):
    item = fixture_item("a")
    item.update(
        provenance="Meta / muse-spark-1.3",
        cutout_url="/media/fixture-job/crops/crop-generated.jpg",
        image_url="/media/fixture-job/preview.jpg",
    )
    result = probe(item, jobs)
    assert result["reference"] is None and result["manual"] == ""
    assert "data-original-search-download" not in result["discovery"]


def test_existing_verified_offers_do_not_get_an_empty_result_fallback():
    item = fixture_item("a")
    item["discovery"] = {"status": "completed", "result": shopping_result()}
    assert "data-original-search-download" not in probe(item)["discovery"]

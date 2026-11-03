import asyncio
import base64
import copy
import io
import json

import httpx
import pytest
from openai import AsyncOpenAI
from PIL import Image
from pydantic import ValidationError

from wardrobe.config import Settings
from wardrobe.identity_schemas import (
    IDENTITY_SCHEMA,
    normalize_identity,
    normalize_shopping,
    normalize_source_url,
)
from wardrobe.providers import (
    ProviderError,
    Providers,
    _shopping_hit_score,
    _shopping_queries,
    _shopping_query,
)


@pytest.fixture
def record():
    return {
        "category": "top",
        "subtype": "t-shirt",
        "brand": "UnverifiedPriorGuess",
        "size_label": None,
        "colors": ["blue"],
        "pattern": "striped",
        "material_guess": None,
        "condition": "unknown",
        "defects": [],
        "confidence": 0.8,
        "needs_review": True,
    }


@pytest.fixture
def identity():
    return {
        "status": "observed",
        "brand": "Example Brand",
        "evidence_type": "label",
        "visible_text": "EXAMPLE BRAND Style A-123",
        "evidence": "Brand and product code readable on the collar label.",
        "confidence": 0.9,
        "product_code": "A-123",
        "search_terms": ["blue striped shirt"],
        "visual_profile": {
            "type": "t-shirt",
            "category": "top",
            "primary_color": "blue",
            "pattern": "striped",
            "sleeve_length": "short",
            "neckline": "crew",
            "description": "Blue striped crew-neck t-shirt.",
        },
    }


@pytest.fixture
def photos(tmp_path):
    cutout, context = tmp_path / "cutout.png", tmp_path / "context.png"
    Image.new("RGB", (12, 17), "blue").save(cutout)
    Image.new("RGB", (31, 33), "red").save(context)
    return cutout, context


def make_provider(tmp_path, handler, timeout=2):
    settings = Settings(data_dir=tmp_path, api_key="private-test-key", request_timeout=timeout)
    provider = Providers(settings)
    provider._meta = AsyncOpenAI(
        base_url=settings.meta_url,
        api_key=settings.api_key,
        max_retries=0,
        timeout=timeout,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    return provider


def offer(url="https://retailer.example.com/product/a123", match="model_candidate"):
    return {
        "title": "Model-generated title",
        "url": url,
        "merchant": "Claimed official store",
        "match": match,
        "reason": "Candidate page mentions a similar garment; please verify independently.",
    }


def shopping_result(offers=None, sources=None, status="searched"):
    return {
        "status": status,
        "query": "blue shirt buy Taiwan",
        "offers": [offer()] if offers is None else offers,
        "sources": [{"title": "Source product title", "url": offer()["url"], "snippet": "Style A-123"}]
        if sources is None
        else sources,
        "searched_at": "2026-10-21T01:02:03+00:00",
        "notice": "untrusted model notice",
    }


def search_response(*, offers=None, cited_urls=None, hits=None, searched=True, status="completed"):
    if offers is None:
        offers = [offer(), offer("https://fabricated.example.com/unsupported")]
    final_text = json.dumps({"offers": offers}, ensure_ascii=False)
    if cited_urls is None:
        cited_urls = [offer()["url"]]
    if hits is None:
        hits = [
            {
                "type": "text_result",
                "url": offer()["url"],
                "title": "Actual product A-123",
                "snippet": "Retrieved snippet A-123",
            }
        ]
    output = [
        {
            "id": "progress",
            "type": "message",
            "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "Searching the live web now.", "annotations": []}],
        }
    ]
    if searched:
        output.append(
            {
                "id": "web1",
                "type": "web_search_call",
                "status": "completed",
                "action": {"type": "search", "query": "blue shirt"},
                "results": hits,
            }
        )
    output.append(
        {
            "id": "final",
            "type": "message",
            "status": "completed",
            "role": "assistant",
            "content": [
                {
                    "type": "output_text",
                    "text": final_text,
                    "annotations": [
                        {
                            "type": "url_citation",
                            "url": url,
                            "title": "Cited title",
                            "start_index": 0,
                            "end_index": len(final_text),
                        }
                        for url in cited_urls
                    ],
                }
            ],
        }
    )
    return {
        "id": "response1",
        "object": "response",
        "created_at": 1,
        "model": "muse-spark-1.3",
        "status": status,
        "output": output,
    }


def test_unknown_identity_erases_guessed_brand_and_product(identity):
    identity.update(status="unknown", evidence_type="logo", confidence=0.99)
    result = normalize_identity(identity)
    assert result["brand"] is result["product_code"] is result["visible_text"] is None
    assert result["confidence"] == 0 and result["evidence_type"] == "none"
    assert identity["brand"] == "Example Brand"


@pytest.mark.parametrize(
    "change",
    [
        {"brand": None},
        {"evidence_type": "none"},
        {"visible_text": None},
        {"evidence": " "},
        {"confidence": float("nan")},
        {"confidence": True},
        {"search_terms": ["a", "b", "c", "d"]},
        {"extra": "field"},
    ],
)
def test_identity_rejects_unsupported_or_malformed_claims(identity, change):
    identity.update(change)
    with pytest.raises(ValidationError):
        normalize_identity(identity)


def test_product_code_must_be_present_in_actual_transcription(identity):
    identity["product_code"] = "B-999"
    assert normalize_identity(identity)["product_code"] is None


def test_incomplete_wordmark_cannot_be_presented_as_a_directly_observed_full_name(identity):
    identity["visible_text"] = "EB Style A-123"
    assert normalize_identity(identity)["status"] == "possible"


@pytest.mark.parametrize(
    "change",
    [
        {"offers": [offer()] * 6},
        {"sources": [{"title": "Source", "url": offer()["url"], "snippet": ""}] * 11},
        {"searched_at": "not-a-date"},
        {"searched_at": "2026-10-21T00:00:00"},
    ],
)
def test_shopping_result_bounds_and_timestamp_are_validated(identity, change):
    result = shopping_result()
    result.update(change)
    with pytest.raises(ValidationError):
        normalize_shopping(result, identity)


@pytest.mark.parametrize(
    "url",
    [
        "http://store.example.com/product",
        "javascript:alert(1)",
        "https://user:pass@store.example.com/x",
        "https://store.example.com:8080/x",
        "https://localhost/x",
        "https://printer.local/x",
        "https://metadata.google.internal/x",
        "https://10.0.0.1/x",
        "https://127.0.0.1/x",
        "https://[::1]/x",
        "https://[::ffff:127.0.0.1]/x",
        "https://169.254.169.254/x",
        "https://127.1/x",
        "https://2130706433/x",
        "https://0x7f000001/x",
        "https://0177.0.0.1/x",
        "https://store.example.com\\@127.0.0.1/x",
        "https://store.example.com/\nprivate",
        "https://store.example.com/" + "x" * 2048,
    ],
)
def test_source_url_rejects_private_credentialled_or_unsafe_links(url):
    with pytest.raises(ValueError):
        normalize_source_url(url)


def test_shopping_normalizer_requires_sources_and_does_not_preserve_claimed_merchant(identity):
    result = normalize_shopping(
        shopping_result(offers=[offer(), offer("https://madeup.example.com/x")]), identity
    )
    assert len(result["offers"]) == 1
    assert result["offers"][0]["merchant"] == "retailer.example.com"
    assert result["offers"][0]["title"] == "Source product title"
    assert result["offers"][0]["match"] == "model_candidate"
    assert "original store" in result["notice"] and result["notice"] != "untrusted model notice"


@pytest.mark.parametrize("change", [{"status": "unknown"}, {"status": "possible"}, {"product_code": None}])
def test_model_candidate_requires_observed_brand_and_readable_code(identity, change):
    identity.update(change)
    result = normalize_shopping(shopping_result(), identity)
    assert result["offers"][0]["match"] == "similar"


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Style A-123 in blue", "model_candidate"),
        ("Style a123 in blue", "model_candidate"),
        ("Product code A.123, blue", "model_candidate"),
        ("Style A1234", "similar"),
        ("Style XA123", "similar"),
        ("Style A123B", "similar"),
        ("Style A123-BLUE", "similar"),
        ("Style B-A123", "similar"),
        ("No product code visible in this result", "similar"),
    ],
)
@pytest.mark.parametrize("field", ["title", "snippet"])
def test_source_must_contain_the_observed_code_as_a_complete_token(identity, text, expected, field):
    source = {"title": "Retrieved product", "url": offer()["url"], "snippet": ""}
    source[field] = text
    proposed = offer()
    proposed["reason"] = "Official in-stock, guaranteed purchase, 100% same item."
    result = normalize_shopping(shopping_result(offers=[proposed], sources=[source]), identity)
    assert result["offers"][0]["match"] == expected
    assert "Official" not in result["offers"][0]["reason"]
    assert "in-stock" not in result["offers"][0]["reason"]
    assert "guaranteed" not in result["offers"][0]["reason"]
    assert "not been verified" in result["offers"][0]["reason"]


def test_unavailable_or_unsourced_search_cannot_return_offers(identity):
    unavailable = normalize_shopping(shopping_result(status="unavailable"), identity)
    assert unavailable["offers"] == unavailable["sources"] == []
    empty = normalize_shopping(shopping_result(sources=[]), identity)
    assert empty["status"] == "no_results" and empty["offers"] == []


async def test_brand_identification_sends_original_images_but_omits_prior_brand(
    tmp_path, photos, record, identity
):
    original = copy.deepcopy(record)
    before = [path.read_bytes() for path in photos]

    def respond(request):
        assert request.url.path == "/v1/chat/completions"
        body = json.loads(request.content)
        assert "UnverifiedPriorGuess" not in request.content.decode()
        assert "tools" not in body
        assert body["response_format"]["json_schema"]["strict"] is True
        content = body["messages"][1]["content"]
        assert len(content) == 3
        for reference, color in zip(content[1:], [(0, 0, 255), (255, 0, 0)]):
            encoded = reference["image_url"]["url"].split(",", 1)[1]
            with Image.open(io.BytesIO(base64.b64decode(encoded))) as image:
                assert image.getpixel((0, 0)) == color
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": json.dumps(identity)},
                    }
                ]
            },
        )

    provider = make_provider(tmp_path, respond)
    try:
        result = await provider.identify_brand(*photos, record)
    finally:
        await provider.close()
    assert result["brand"] == "Example Brand"
    assert record == original and [path.read_bytes() for path in photos] == before


async def test_brand_identification_refuses_stored_reconstruction_even_through_symlink(
    tmp_path, photos, record
):
    directory = tmp_path / "reconstructions"
    directory.mkdir()
    image = directory / "attempt.png"
    image.write_bytes(photos[0].read_bytes())
    alias = tmp_path / "alias.png"
    alias.symlink_to(image)
    calls = []
    provider = make_provider(tmp_path, lambda request: calls.append(request))
    try:
        with pytest.raises(ProviderError, match="original photos"):
            await provider.identify_brand(alias, photos[1], record)
    finally:
        await provider.close()
    assert calls == []


@pytest.mark.parametrize("market", ["TW", "global"])
async def test_search_uses_only_text_and_matches_actual_search_hits(tmp_path, identity, record, market):
    before = copy.deepcopy(identity), copy.deepcopy(record)

    def respond(request):
        assert request.url.path == "/v1/responses"
        body = json.loads(request.content)
        assert body["store"] is False and body["include"] == ["web_search_call.results"]
        assert isinstance(body["input"], str)
        assert "image" not in body["input"] and "UnverifiedPriorGuess" not in body["input"]
        if market == "TW":
            assert body["tools"][0]["user_location"]["country"] == "TW"
        else:
            assert "user_location" not in body["tools"][0]
        return httpx.Response(200, json=search_response())

    provider = make_provider(tmp_path, respond)
    try:
        result = await provider.search_shopping(identity, record, market)
    finally:
        await provider.close()
    assert result["status"] == "searched" and len(result["offers"]) == len(result["sources"]) == 1
    assert result["offers"][0]["url"] == offer()["url"]
    assert result["offers"][0]["title"] == "Actual product A-123"
    assert result["offers"][0]["match"] == "model_candidate"
    assert (identity, record) == before


@pytest.mark.parametrize(
    "kind,status",
    [
        ("no_search", "unavailable"),
        ("no_hits", "no_results"),
        ("citation_only", "no_results"),
        ("failed_search", "unavailable"),
        ("private_url", "no_results"),
    ],
)
async def test_search_never_promotes_ungrounded_or_unsafe_results(tmp_path, identity, record, kind, status):
    response = search_response()
    if kind == "no_search":
        response = search_response(searched=False)
    elif kind == "no_hits":
        response = search_response(hits=[], cited_urls=[], offers=[])
    elif kind == "citation_only":
        response = search_response(hits=[])
    elif kind == "failed_search":
        response["output"][1]["status"] = "failed"
    else:
        url = "https://127.0.0.1/private"
        response = search_response(
            offers=[offer(url)],
            cited_urls=[url],
            hits=[{"type": "text_result", "url": url, "title": "Private", "snippet": ""}],
        )
    provider = make_provider(tmp_path, lambda request: httpx.Response(200, json=response))
    try:
        result = await provider.search_shopping(identity, record)
    finally:
        await provider.close()
    assert result["status"] == status and result["offers"] == [] and result["sources"] == []


@pytest.mark.parametrize("citation", ["absent", "bad_span"])
async def test_completed_search_hit_is_sufficient_without_a_json_citation(
    tmp_path, identity, record, citation
):
    response = search_response(cited_urls=[] if citation == "absent" else [offer()["url"]])
    if citation == "bad_span":
        response["output"][-1]["content"][0]["annotations"][0]["end_index"] = 999999
    # Sources must not include unrelated raw hits, even when they rank first.
    response["output"][1]["results"].insert(
        0,
        {
            "type": "text_result",
            "title": "Unrelated page",
            "url": "https://unrelated.example.com/x",
            "snippet": "",
        },
    )
    provider = make_provider(tmp_path, lambda request: httpx.Response(200, json=response))
    try:
        result = await provider.search_shopping(identity, record)
    finally:
        await provider.close()
    assert result["status"] == "searched"
    assert [item["url"] for item in result["offers"]] == [offer()["url"]]
    assert [item["url"] for item in result["sources"]] == [offer()["url"]]


@pytest.mark.parametrize(
    "raw_url,expected",
    [
        (" \t" + offer()["url"] + "\r\n", "searched"),
        ("https://retailer.example.com/product/\na123", "no_results"),
    ],
)
async def test_raw_url_boundary_whitespace_is_cleaned_but_embedded_controls_are_rejected(
    tmp_path, identity, record, raw_url, expected
):
    response = search_response(
        cited_urls=[],
        hits=[{"type": "text_result", "title": "Actual product A123", "url": raw_url, "snippet": ""}],
    )
    provider = make_provider(tmp_path, lambda request: httpx.Response(200, json=response))
    try:
        result = await provider.search_shopping(identity, record)
    finally:
        await provider.close()
    assert result["status"] == expected
    assert len(result["offers"]) == (1 if expected == "searched" else 0)


async def test_selected_hits_survive_source_cap_and_citations_cannot_add_unretrieved_urls(
    tmp_path, identity, record
):
    hit_urls = [f"https://retailer.example.com/product/{index}" for index in range(12)]
    hits = [
        {"type": "text_result", "title": f"Actual product {index}", "url": url, "snippet": ""}
        for index, url in enumerate(hit_urls)
    ]
    invented = "https://invented.example.com/not-retrieved"
    response = search_response(
        offers=[offer(hit_urls[-1]), offer(invented)],
        hits=hits,
        cited_urls=[*hit_urls[:10], invented],
    )
    provider = make_provider(tmp_path, lambda request: httpx.Response(200, json=response))
    try:
        result = await provider.search_shopping(identity, record)
    finally:
        await provider.close()
    assert [item["url"] for item in result["offers"]] == [hit_urls[-1]]
    assert len(result["sources"]) == 10
    assert result["sources"][0]["url"] == hit_urls[-1]
    assert invented not in [item["url"] for item in result["sources"]]
    assert hit_urls[10] not in [item["url"] for item in result["sources"]]


async def test_unknown_brand_cannot_reappear_via_search_terms_or_catalogue_guess(tmp_path, identity, record):
    identity.update(status="unknown", search_terms=["HallucinatedBrand exclusive product"])

    def respond(request):
        body = json.loads(request.content)
        assert "HallucinatedBrand" not in body["input"] and "UnverifiedPriorGuess" not in body["input"]
        return httpx.Response(200, json=search_response())

    provider = make_provider(tmp_path, respond)
    try:
        result = await provider.search_shopping(identity, record)
    finally:
        await provider.close()
    assert "Example Brand" not in result["query"]
    assert result["offers"][0]["match"] == "similar"


@pytest.mark.parametrize("failure", ["rejected", "truncated", "malformed"])
async def test_search_failure_is_safe_and_preserves_original_metadata(tmp_path, identity, record, failure):
    before = copy.deepcopy(identity), copy.deepcopy(record)
    response = search_response(status="incomplete" if failure == "truncated" else "completed")
    if failure == "malformed":
        response["output"][-1]["content"][0]["text"] += "private-test-key"
    provider = make_provider(
        tmp_path,
        lambda request: httpx.Response(
            401 if failure == "rejected" else 200,
            json={"error": {"message": "private-test-key"}} if failure == "rejected" else response,
        ),
    )
    try:
        with pytest.raises(ProviderError) as error:
            await provider.search_shopping(identity, record)
        assert "private-test-key" not in str(error.value)
    finally:
        await provider.close()
    assert (identity, record) == before


@pytest.mark.parametrize("cancel", [False, True])
async def test_search_timeout_and_cancellation_are_bounded(tmp_path, identity, record, cancel):
    entered, stopped = asyncio.Event(), asyncio.Event()

    async def stall(request):
        entered.set()
        try:
            await asyncio.sleep(30)
        finally:
            stopped.set()

    provider = make_provider(tmp_path, stall, timeout=2 if cancel else 0.1)
    try:
        task = asyncio.create_task(provider.search_shopping(identity, record))
        await asyncio.wait_for(entered.wait(), 1)
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(ProviderError, match="timed out"):
                await task
        assert stopped.is_set()
    finally:
        await provider.close()


def comparison_response(**changes):
    comparison = dict.fromkeys(
        ("category", "primary_color", "pattern", "sleeve_length", "neckline", "design_details"), "match"
    )
    comparison.update(changes)
    return {
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "comparison": comparison,
                            "reasons": [
                                "Visible features compared item by item against original photo and product main image."
                            ],
                        }
                    ),
                },
            }
        ],
    }


def fetched_product(**changes):
    data = io.BytesIO()
    Image.new("RGB", (96, 128), "blue").save(data, format="PNG")
    page = {
        "page_url": offer()["url"],
        "title": "Actual blue striped short-sleeve tee",
        "description": "Blue striped tee",
        "brand": "Example Brand",
        "product_code": "A-123",
        "color": "blue",
        "availability": "in_stock",
        "market": "in_scope",
        "market_evidence": "Product page uses TWD and indicates a Taiwan store.",
        "image_url": "https://cdn.retailer.example.com/blue.png",
        "image_bytes": data.getvalue(),
        "image_mime": "image/png",
        "checked_at": "2026-10-21T02:00:00+00:00",
    }
    page.update(changes)
    return page


def test_visual_profile_is_required_for_new_vision_but_nullable_for_old_records(identity):
    identity.pop("visual_profile")
    assert normalize_identity(identity)["visual_profile"] is None
    assert "visual_profile" in IDENTITY_SCHEMA["required"]
    assert "default" not in IDENTITY_SCHEMA["properties"]["visual_profile"]


@pytest.mark.parametrize(
    "field,value", [("sleeve_length", "guessed"), ("primary_color", "x" * 61), ("description", "x" * 501)]
)
def test_visual_profile_is_bounded_and_uses_known_enums(identity, field, value):
    identity["visual_profile"][field] = value
    with pytest.raises(ValidationError):
        normalize_identity(identity)


async def test_query_uses_main_fabric_colour_not_logo_or_possible_brand(tmp_path, identity, record):
    identity.update(status="possible")
    identity["visual_profile"].update(primary_color="white", pattern="solid")
    record.update(colors=["black", "blue"], brand="MadeUpBrand")
    captured = []

    def respond(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json=search_response())

    provider = make_provider(tmp_path, respond)
    try:
        result = await provider.search_shopping(identity, record)
    finally:
        await provider.close()
    assert result["query"] == "solid white short sleeve crew neck t shirt buy Taiwan"
    assert not any(value in result["query"] for value in ("black", "blue", "Example Brand", "MadeUpBrand"))
    assert "never collection" in captured[0]["instructions"]
    assert "do not add a site restriction" in captured[0]["instructions"]
    assert "application will fetch the page" in captured[0]["instructions"]
    assert all(c["tool_choice"] == "auto" and "max_tool_calls" not in c for c in captured)
    assert len(captured) <= 2


@pytest.mark.parametrize(
    "profile,expected",
    [
        (
            {"primary_color": "rust red", "pattern": "solid"},
            "solid rust red short sleeve crew neck t shirt buy Taiwan",
        ),
        (
            {"primary_color": "White", "pattern": "solid", "type": "T-Shirt"},
            "solid white short sleeve crew neck t shirt buy Taiwan",
        ),
        (
            {
                "primary_color": "navy blue",
                "pattern": "checked",
                "sleeve_length": "long",
                "neckline": "collared",
                "type": "shirt",
            },
            "checked navy blue long sleeve collared shirt buy Taiwan",
        ),
        (
            {
                "primary_color": "off-white",
                "pattern": "striped",
                "sleeve_length": "three_quarter",
                "neckline": "v_neck",
                "type": "sweater",
            },
            "striped off white three quarter sleeve v neck sweater buy Taiwan",
        ),
        (
            {"primary_color": "brick red", "pattern": "solid", "type": "tee"},
            "solid rust red short sleeve crew neck t shirt buy Taiwan",
        ),
    ],
)
def test_taiwan_query_localizes_actual_visible_features(identity, record, profile, expected):
    identity.update(status="unknown")
    identity["visual_profile"].update(profile)
    assert _shopping_query(normalize_identity(identity), record, "TW") == expected


def test_localized_query_keeps_observed_name_and_never_invents_unknown_features(identity, record):
    identity["visual_profile"].update(primary_color=None, pattern=None, sleeve_length=None, neckline=None)
    identity.update(brand="Nike", visible_text="NIKE", product_code=None)
    record.update(colors=["black"], pattern="floral", brand="SavedGuess")
    assert _shopping_query(normalize_identity(identity), record, "TW") == "Nike t shirt buy Taiwan"
    identity["status"] = "possible"
    assert _shopping_query(normalize_identity(identity), record, "TW") == "t shirt buy Taiwan"


def test_global_search_retains_original_feature_words(identity, record):
    identity["status"] = "unknown"
    identity["visual_profile"].update(primary_color="rust red", pattern="solid")
    assert (
        _shopping_query(normalize_identity(identity), record, "global")
        == "t-shirt rust red solid short crew product shopping"
    )


def test_global_search_uses_the_specific_visible_style_name(identity, record):
    identity["status"] = "unknown"
    identity["visual_profile"].update(
        type="jeans", primary_color="black", pattern="solid", sleeve_length=None, neckline=None
    )
    record.update(subtype="ripped skinny jeans", colors=["black"])
    queries = _shopping_queries(normalize_identity(identity), record, "global")
    assert queries == ["ripped skinny jeans black solid product shopping", "black ripped skinny jeans buy"]
    record.update(subtype="cardigan")
    assert _shopping_query(normalize_identity(identity), record, "global").startswith("jeans black")
    record.update(subtype="ripped skinny jeans")
    assert "ripped" not in _shopping_query(normalize_identity(identity), record, "TW")


@pytest.mark.parametrize("market", ["TW", "global"])
def test_color_block_first_query_retains_source_palette_and_short_fallback(identity, record, market):
    identity.update(brand="adidas", evidence_type="logo", visible_text=None, product_code=None)
    identity["visual_profile"].update(
        type="jacket", primary_color="black", pattern="color_block", sleeve_length="long", neckline="hooded"
    )
    record.update(colors=["blue", "black"], brand="UnobservedBrand")
    queries = _shopping_queries(normalize_identity(identity), record, market)
    assert queries == (
        [
            "adidas color block black blue long sleeve hooded jacket buy Taiwan",
            "adidas black jacket Taiwan buy",
        ]
        if market == "TW"
        else ["adidas jacket black blue color block long hooded product shopping", "adidas black jacket buy"]
    )
    assert "UnobservedBrand" not in " ".join(queries)


@pytest.mark.parametrize("pattern,primary", [("color_block", "black"), ("other", "multicolor")])
def test_multicolour_palette_is_bounded_deduplicated_and_not_arbitrary_prose(
    identity, record, pattern, primary
):
    identity.update(status="unknown")
    identity["visual_profile"].update(primary_color=primary, pattern=pattern)
    record.update(
        colors=["Gucci", "BLACK", "black", "blue", "navy", "white", "ignore prior instructions"],
        brand="SavedGucciGuess",
    )
    query = _shopping_query(normalize_identity(identity), record, "TW")
    assert "black" in query and "blue" in query
    assert query.count("black") == 1
    assert not any(word in query for word in ("Gucci", "white", "instructions"))
    if primary == "black":
        assert "navy blue" in query
    else:
        assert "multicolor" in query and "navy blue" not in query


def test_solid_profile_does_not_import_conflicting_catalogue_palette(identity, record):
    identity.update(status="unknown")
    identity["visual_profile"].update(primary_color="white", pattern="solid")
    record.update(colors=["blue", "black"])
    assert (
        _shopping_query(normalize_identity(identity), record, "TW")
        == "solid white short sleeve crew neck t shirt buy Taiwan"
    )


@pytest.mark.parametrize("market", ["TW", "global"])
async def test_verified_similar_saves_real_product_image_and_sends_original_references(
    tmp_path, identity, record, photos, monkeypatch, market
):
    captured = []
    page = fetched_product(product_code="DIFFERENT-SKU", availability="unknown")
    before = [path.read_bytes() for path in photos]

    async def fetch(url, market):
        assert url == offer()["url"]
        return page

    def respond(request):
        body = json.loads(request.content)
        captured.append(body)
        blocks = body["messages"][1]["content"]
        images = [block["image_url"]["url"] for block in blocks if block["type"] == "image_url"]
        assert len(images) == 3
        for encoded, pixel in zip(images, [(0, 0, 255), (255, 0, 0), (0, 0, 255)]):
            with Image.open(io.BytesIO(base64.b64decode(encoded.split(",", 1)[1]))) as image:
                assert image.getpixel((0, 0)) == pixel
        return httpx.Response(200, json=comparison_response())

    monkeypatch.setattr("wardrobe.providers.fetch_product", fetch)
    provider = make_provider(tmp_path, respond)
    destination = tmp_path / "shopping"
    try:
        result = await provider.verify_shopping(
            identity, record, shopping_result(), *photos, destination, market
        )
    finally:
        await provider.close()
    assert result["version"] == 2 and result["status"] == "searched"
    assert len(result["offers"]) == 1 and len(captured) == 1
    accepted = result["offers"][0]
    assert accepted["match"] == "similar"
    assert accepted["verification"]["availability"] == "unknown"
    assert accepted["verification"]["market"] == "in_scope"
    assert result["checked_candidates"][0]["status"] == "accepted"
    with Image.open(destination / accepted["verification"]["image_url"]) as image:
        assert image.size == (96, 128) and image.getpixel((0, 0)) == (0, 0, 255)
    assert [path.read_bytes() for path in photos] == before
    assert normalize_shopping(result, identity) == result
    assert "not been verified" not in accepted["reason"]


@pytest.mark.parametrize(
    "changes,expected_reason",
    [
        ({"primary_color": "mismatch"}, "primary color"),
        ({"pattern": "mismatch", "sleeve_length": "mismatch"}, "pattern"),
        ({"category": "mismatch"}, "garment type"),
        ({"neckline": "uncertain"}, "neckline"),
        ({"design_details": "not_applicable"}, "design details"),
    ],
)
async def test_individual_visual_mismatches_cannot_be_overridden(
    tmp_path, identity, record, photos, monkeypatch, changes, expected_reason
):
    async def fetch(url, market):
        return fetched_product()

    monkeypatch.setattr("wardrobe.providers.fetch_product", fetch)
    provider = make_provider(
        tmp_path, lambda request: httpx.Response(200, json=comparison_response(**changes))
    )
    destination = tmp_path / "shopping"
    try:
        result = await provider.verify_shopping(identity, record, shopping_result(), *photos, destination)
    finally:
        await provider.close()
    assert result["offers"] == []
    checked = result["checked_candidates"][0]
    assert checked["status"] == ("rejected" if "mismatch" in changes.values() else "unverified")
    assert any(expected_reason in reason for reason in checked["reasons"])
    assert not list(destination.glob("*.png"))


@pytest.mark.parametrize(
    "change,reason,status",
    [
        ({"market": "out_of_scope", "market_evidence": "Philippines PHP"}, "region", "rejected"),
        ({"market": "unknown", "market_evidence": ""}, "region", "unverified"),
        ({"availability": "out_of_stock"}, "out of stock", "rejected"),
        ({"image_bytes": None}, "product image", "rejected"),
        ({"brand": "Different Brand"}, "brand", "rejected"),
    ],
)
async def test_page_failures_stop_before_vision_and_do_not_write_images(
    tmp_path, identity, record, photos, monkeypatch, change, reason, status
):
    calls = []

    async def fetch(url, market):
        return fetched_product(**change)

    monkeypatch.setattr("wardrobe.providers.fetch_product", fetch)
    provider = make_provider(tmp_path, lambda request: calls.append(request))
    destination = tmp_path / "shopping"
    try:
        result = await provider.verify_shopping(identity, record, shopping_result(), *photos, destination)
    finally:
        await provider.close()
    assert result["offers"] == [] and not calls
    checked = result["checked_candidates"][0]
    assert checked["status"] == status and any(reason in message for message in checked["reasons"])
    assert not list(destination.glob("*.png"))


def verified_result(identity):
    accepted = offer(match="model_candidate")
    accepted.update(
        reason="Product image blue stripes and short-sleeve construction match.",
        verification={
            "status": "verified",
            "page_url": accepted["url"],
            "page_checked_at": "2026-10-21T02:00:00+00:00",
            "market": "in_scope",
            "market_evidence": "Taiwan store uses TWD.",
            "availability": "in_stock",
            "comparison": dict.fromkeys(
                ("category", "primary_color", "pattern", "sleeve_length", "neckline", "design_details"),
                "match",
            ),
            "image_url": "1.png",
            "product_code": identity["product_code"],
        },
    )
    return {
        **shopping_result(offers=[accepted]),
        "version": 2,
        "checked_candidates": [
            {
                "title": accepted["title"],
                "url": accepted["url"],
                "status": "accepted",
                "reasons": [accepted["reason"]],
            }
        ],
    }


@pytest.mark.parametrize(
    "field,value",
    [
        ("market", "out_of_scope"),
        ("market", "unknown"),
        ("availability", "out_of_stock"),
        ("status", "proposed"),
        ("image_url", "https://cdn.example.com/1.png"),
        ("image_url", "../1.png"),
        ("image_url", "/media/job/shopping/attempt/../1.png"),
        ("image_url", "/media/job/shopping/attempt/4.png"),
        ("page_url", "https://127.0.0.1/product"),
        ("page_checked_at", "2026-10-21T02:00:00"),
    ],
)
def test_version2_normalizer_revalidates_concrete_verification(identity, field, value):
    result = verified_result(identity)
    result["offers"][0]["verification"][field] = value
    with pytest.raises(ValueError):
        normalize_shopping(result, identity)


@pytest.mark.parametrize("status", ["mismatch", "uncertain", "not_applicable"])
def test_version2_normalizer_cannot_accept_missing_known_feature(identity, status):
    result = verified_result(identity)
    result["offers"][0]["verification"]["comparison"]["primary_color"] = status
    with pytest.raises(ValueError, match="Visual evidence"):
        normalize_shopping(result, identity)


def test_version2_requires_visual_profile_and_source_checked_agreement(identity):
    result = verified_result(identity)
    legacy_identity = {key: value for key, value in identity.items() if key != "visual_profile"}
    with pytest.raises(ValueError, match="Visual evidence"):
        normalize_shopping(result, legacy_identity)
    result["sources"] = []
    with pytest.raises(ValueError, match="retrieved and checked"):
        normalize_shopping(result, identity)
    result = verified_result(identity)
    result["checked_candidates"][0]["status"] = "unverified"
    with pytest.raises(ValueError, match="retrieved and checked"):
        normalize_shopping(result, identity)


def test_version2_preserves_specific_reason_and_requires_complete_page_sku(identity):
    result = verified_result(identity)
    original_reason = result["offers"][0]["reason"]
    normalized = normalize_shopping(result, identity)
    assert normalized["offers"][0]["match"] == "model_candidate"
    assert normalized["offers"][0]["reason"] == original_reason
    result["offers"][0]["verification"]["product_code"] += "-OTHER"
    result["offers"][0]["verification"]["image_url"] = "/media/job42/shopping/attempt123/1.png"
    normalized = normalize_shopping(result, identity)
    assert normalized["offers"][0]["match"] == "similar"
    assert normalized["offers"][0]["reason"] == original_reason


async def test_white_original_and_black_product_references_reach_comparator_separately(
    tmp_path, identity, record, photos, monkeypatch
):
    identity["visual_profile"].update(primary_color="white", pattern="solid")
    Image.new("RGB", (96, 128), "white").save(photos[0])
    page = fetched_product()
    encoded = io.BytesIO()
    Image.new("RGB", (96, 128), "black").save(encoded, format="PNG")
    page["image_bytes"] = encoded.getvalue()
    pixels = []

    async def fetch(url, market):
        return page

    def respond(request):
        blocks = json.loads(request.content)["messages"][1]["content"]
        for block in blocks:
            if block["type"] == "image_url":
                raw = base64.b64decode(block["image_url"]["url"].split(",", 1)[1])
                with Image.open(io.BytesIO(raw)) as image:
                    pixels.append(image.getpixel((0, 0)))
        return httpx.Response(200, json=comparison_response(primary_color="mismatch"))

    monkeypatch.setattr("wardrobe.providers.fetch_product", fetch)
    provider = make_provider(tmp_path, respond)
    try:
        result = await provider.verify_shopping(
            identity, record, shopping_result(), *photos, tmp_path / "shopping"
        )
    finally:
        await provider.close()
    assert pixels[0] == (255, 255, 255) and pixels[-1] == (0, 0, 0)
    assert result["offers"] == [] and result["checked_candidates"][0]["status"] == "rejected"


@pytest.mark.parametrize("failure", ["invalid-image", "model-boolean", "secret-error", "collection"])
async def test_invalid_or_unsubstantiated_candidate_never_creates_verified_offer(
    tmp_path, identity, record, photos, monkeypatch, failure
):
    from wardrobe.product_evidence import ProductFetchError

    async def fetch(url, market):
        if failure == "collection":
            raise ProductFetchError(
                "This source is a collection page; cannot confirm a single product.", "not_product"
            )
        if failure == "secret-error":
            raise RuntimeError("private-test-key raw payload secret")
        return (
            fetched_product(image_bytes=b"not an image") if failure == "invalid-image" else fetched_product()
        )

    def respond(request):
        response = comparison_response()
        answer = json.loads(response["choices"][0]["message"]["content"])
        answer["suitable"] = True
        response["choices"][0]["message"]["content"] = json.dumps(answer)
        return httpx.Response(200, json=response)

    monkeypatch.setattr("wardrobe.providers.fetch_product", fetch)
    provider = make_provider(tmp_path, respond)
    destination = tmp_path / "shopping"
    try:
        result = await provider.verify_shopping(identity, record, shopping_result(), *photos, destination)
    finally:
        await provider.close()
    assert not result["offers"] and not list(destination.glob("*.png"))
    assert "private-test-key" not in json.dumps(result)
    assert "raw payload secret" not in json.dumps(result)


async def test_verification_never_uses_reconstruction_even_via_symlink(
    tmp_path, identity, record, photos, monkeypatch
):
    directory = tmp_path / "reconstructions"
    directory.mkdir()
    derived = directory / "attempt.png"
    derived.write_bytes(photos[0].read_bytes())
    alias = tmp_path / "ordinary.png"
    alias.symlink_to(derived)
    calls = []

    async def fetch(url, market):
        calls.append(url)
        return fetched_product()

    monkeypatch.setattr("wardrobe.providers.fetch_product", fetch)
    provider = make_provider(tmp_path, lambda request: None)
    try:
        with pytest.raises(ProviderError, match="original photos"):
            await provider.verify_shopping(
                identity, record, shopping_result(), alias, photos[1], tmp_path / "shopping"
            )
    finally:
        await provider.close()
    assert calls == []


def multiple_candidates(count):
    offers = [offer(f"https://retailer.example.com/product/{index}") for index in range(count)]
    sources = [
        {"title": f"Product {index}", "url": candidate["url"], "snippet": "A-123"}
        for index, candidate in enumerate(offers)
    ]
    return shopping_result(offers=offers, sources=sources)


async def test_verification_is_limited_to_three_candidates(tmp_path, identity, record, photos, monkeypatch):
    calls = []

    async def fetch(url, market):
        calls.append(url)
        return fetched_product(page_url=url)

    monkeypatch.setattr("wardrobe.providers.fetch_product", fetch)
    provider = make_provider(tmp_path, lambda request: httpx.Response(200, json=comparison_response()))
    destination = tmp_path / "shopping"
    try:
        result = await provider.verify_shopping(
            identity, record, multiple_candidates(5), *photos, destination
        )
    finally:
        await provider.close()
    assert len(calls) == len(result["offers"]) == 3
    assert len(result["checked_candidates"]) == 5
    assert all(
        x["status"] == "unverified" and "limit" in x["reasons"][0].lower()
        for x in result["checked_candidates"][3:]
    )
    assert sorted(path.name for path in destination.glob("*.png")) == ["1.png", "2.png", "3.png"]


@pytest.mark.parametrize("cancel", [False, True])
async def test_verification_deadline_and_cancellation_stop_fetches_without_late_files(
    tmp_path, identity, record, photos, monkeypatch, cancel
):
    entered, stopped = asyncio.Event(), asyncio.Event()

    async def fetch(url, market):
        entered.set()
        try:
            await asyncio.sleep(30)
        finally:
            stopped.set()

    monkeypatch.setattr("wardrobe.providers.fetch_product", fetch)
    # The deadline includes thread-pool source encoding. Allow that setup to
    # enter the controlled blocking fetch even on a loaded test machine.
    provider = make_provider(tmp_path, lambda request: None, timeout=2 if cancel else 0.5)
    destination = tmp_path / "shopping"
    try:
        task = asyncio.create_task(
            provider.verify_shopping(identity, record, multiple_candidates(3), *photos, destination)
        )
        await asyncio.wait_for(entered.wait(), 2)
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            result = await task
            assert result["offers"] == [] and len(result["checked_candidates"]) == 3
            assert all(item["status"] == "unverified" for item in result["checked_candidates"])
        assert stopped.is_set() and not list(destination.glob("*.png"))
    finally:
        await provider.close()


async def test_total_deadline_keeps_completed_verified_candidate(
    tmp_path, identity, record, photos, monkeypatch
):
    calls = []

    async def fetch(url, market):
        calls.append(url)
        if len(calls) > 1:
            await asyncio.sleep(30)
        return fetched_product(page_url=url)

    monkeypatch.setattr("wardrobe.providers.fetch_product", fetch)
    provider = make_provider(
        tmp_path, lambda request: httpx.Response(200, json=comparison_response()), timeout=0.15
    )
    destination = tmp_path / "shopping"
    try:
        result = await provider.verify_shopping(
            identity, record, multiple_candidates(3), *photos, destination
        )
    finally:
        await provider.close()
    assert len(result["offers"]) == 1 and result["status"] == "searched"
    assert [item["status"] for item in result["checked_candidates"]] == [
        "accepted",
        "unverified",
        "unverified",
    ]
    assert sorted(path.name for path in destination.glob("*.png")) == ["1.png"]


@pytest.mark.parametrize(
    "readable,proposed,kept",
    [
        ("EXAMPLE BRAND A-123", "A123", True),
        ("EXAMPLE BRAND A-1234", "A-123", False),
        ("EXAMPLE BRAND XA-123", "A-123", False),
        ("EXAMPLE BRAND A-123-BLUE", "A-123", False),
    ],
)
def test_original_product_code_requires_whole_visible_token(identity, readable, proposed, kept):
    identity.update(visible_text=readable, product_code=proposed)
    assert (normalize_identity(identity)["product_code"] is not None) is kept


async def test_second_query_recovers_actual_empty_snippet_product_not_fabricated_url(
    tmp_path, identity, record
):
    identity.update(status="unknown")
    identity["visual_profile"].update(primary_color="white", pattern="solid")
    url = "https://retailer.example.com/tw/products/fixture-white-shirt"
    requests = []

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        hits = (
            []
            if len(requests) == 1
            else [{"type": "text_result", "url": url, "title": "Fixture white T-shirt", "snippet": ""}]
        )
        return httpx.Response(200, json=search_response(offers=[], hits=hits, cited_urls=[]))

    provider = make_provider(tmp_path, respond)
    try:
        result = await provider.search_shopping(identity, record)
    finally:
        await provider.close()
    assert len(requests) == 2
    assert json.loads(requests[0]["input"])["query"] != json.loads(requests[1]["input"])["query"]
    assert [a["status"] for a in result["search_attempts"]] == ["no_results", "searched"]
    assert [o["url"] for o in result["offers"]] == [url]
    assert result["offers"][0]["match"] == "similar"
    assert "verification" not in result["offers"][0]
    assert result["sources"][0]["snippet"] == ""
    assert all(r["tool_choice"] == "auto" for r in requests)


@pytest.mark.parametrize(
    "title,url",
    [
        (
            "Adidas Womens Small Blue Black White Zip Up Sports Jacket Climacool",
            "https://tiffanystreasuresandtrinkets.com/products/adidas-womens-small-blue-black-white-zip-up-sports-jacket-climacool",
        ),
        (
            "Adidas  Full-Zip Colorblocking Sweat Jacket Men's Siz…",
            "https://gem.app/product/adidas-full-zip-colorblocking-sweat-jacket",
        ),
    ],
)
async def test_compound_source_type_recovers_actual_retrieved_product_without_claiming_a_match(
    tmp_path, identity, record, title, url
):
    # Verbatim hit excerpts from the preserved real 2026-10-22 retrieval
    # diagnostics 1 and 2. This transport replay is not a new live search,
    # page/image verification, or evidence that either garment is the same item.
    identity.update(brand="adidas", evidence_type="logo", visible_text=None, product_code=None)
    identity["visual_profile"].update(
        type="hooded jacket", category="outerwear", primary_color="black", pattern="color_block"
    )
    record.update(category="outerwear", subtype="hooded jacket", colors=["blue", "black"])
    calls = []
    hit = {"type": "text_result", "url": url, "title": title, "snippet": ""}

    def respond(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json=search_response(offers=[], hits=[hit], cited_urls=[]))

    provider = make_provider(tmp_path, respond)
    try:
        result = await provider.search_shopping(identity, record)
    finally:
        await provider.close()
    assert len(calls) == 2
    assert [entry["url"] for entry in result["offers"]] == [url]
    assert result["offers"][0]["title"] == title
    assert result["offers"][0]["match"] == "similar"
    assert "verification" not in result["offers"][0]
    assert all(attempt["candidate_count"] == 1 for attempt in result["search_attempts"])
    assert all(json.loads(call["input"])["observed_product_code"] is None for call in calls)
    assert result["sources"] == [{"url": url, "title": title, "snippet": ""}]


@pytest.mark.parametrize(
    "kind,title,expected",
    [
        ("hooded jacket", "adidas jacket", True),
        ("zip-up hoodie", "adidas full zip hoodie", True),
        ("short sleeve t-shirt", "adidas cotton tee", True),
        ("hooded jacket", "adidas cotton tee", False),
        ("zip-up hoodie", "adidas trousers", False),
        ("jacket cover", "adidas jacket", False),
        ("jacket-style dress", "adidas jacket", False),
        ("hooded jacket", "Nike hooded jacket", False),
    ],
)
def test_compound_type_uses_the_terminal_garment_kind_without_losing_brand_or_kind_guards(
    identity, kind, title, expected
):
    identity.update(brand="adidas", evidence_type="logo", visible_text=None, product_code=None)
    identity["visual_profile"]["type"] = kind
    hit = {"url": "https://retailer.example.com/tw/products/explicit-fixture", "title": title, "snippet": ""}
    assert (_shopping_hit_score(hit, identity, "TW") > 0) is expected


@pytest.mark.parametrize(
    "url",
    [
        "https://retailer.example.com/us/products/explicit-fixture",
        "https://retailer.example.com/tw/collections/jackets",
        "https://retailer.example.com/tw/blog/jacket.html",
        "https://retailer.example.com/tw/search/jacket",
    ],
)
def test_compound_type_does_not_override_foreign_or_nonproduct_url_exclusions(identity, url):
    identity.update(brand="adidas", evidence_type="logo", visible_text=None, product_code=None)
    identity["visual_profile"]["type"] = "hooded jacket"
    assert _shopping_hit_score({"url": url, "title": "adidas jacket", "snippet": ""}, identity, "TW") == 0


async def test_plausible_retrieved_pool_is_bounded_and_stops_extra_search(tmp_path, identity, record):
    identity.update(status="unknown")
    hits = [
        {
            "type": "text_result",
            "url": f"https://retailer-{i % 3}.example.com/tw/products/fixture-{i}",
            "title": f"Fixture T-shirt {i}",
            "snippet": "",
        }
        for i in range(8)
    ]
    hits.extend(
        [
            {
                "type": "text_result",
                "url": "https://retailer.example.com/tw/collections/shirts",
                "title": "Fixture T-shirt collection",
                "snippet": "",
            },
            {
                "type": "text_result",
                "url": "https://retailer.example.com/us/products/shirt",
                "title": "Fixture T-shirt",
                "snippet": "",
            },
            {
                "type": "text_result",
                "url": "https://retailer.example.com/tw/products/teapot",
                "title": "Fixture blue teapot",
                "snippet": "",
            },
        ]
    )
    requests = []
    provider = make_provider(
        tmp_path,
        lambda request: (
            requests.append(request)
            or httpx.Response(200, json=search_response(offers=[], hits=hits, cited_urls=[]))
        ),
    )
    try:
        result = await provider.search_shopping(identity, record)
    finally:
        await provider.close()
    assert len(requests) == 1
    assert len(result["offers"]) == len(result["sources"]) == 3
    assert all("fixture-" in x["url"] for x in result["offers"])
    assert result["search_attempts"][0]["retrieved_count"] == 11


async def test_broadening_failure_preserves_first_grounded_candidate_with_safe_error(
    tmp_path, identity, record
):
    requests = []

    def respond(request):
        requests.append(request)
        return (
            httpx.Response(200, json=search_response())
            if len(requests) == 1
            else httpx.Response(503, json={"error": {"message": "private-test-key"}})
        )

    provider = make_provider(tmp_path, respond)
    try:
        result = await provider.search_shopping(identity, record)
    finally:
        await provider.close()
    assert len(requests) == 2 and len(result["offers"]) == 1
    assert result["search_attempts"][1]["status"] == "unavailable"
    assert result["search_attempts"][1]["error"]
    assert "private-test-key" not in json.dumps(result)


async def test_unknown_market_candidates_do_not_suppress_broadening(tmp_path, identity, record):
    identity.update(status="unknown")
    calls = []

    def respond(request):
        calls.append(json.loads(request.content))
        prefix = "https://retailer.example.com/" + ("" if len(calls) == 1 else "tw/")
        hits = [
            {
                "type": "text_result",
                "url": prefix + f"products/fixture-{i}",
                "title": f"Fixture T-shirt {i}",
                "snippet": "",
            }
            for i in range(3)
        ]
        return httpx.Response(200, json=search_response(offers=[], hits=hits, cited_urls=[]))

    provider = make_provider(tmp_path, respond)
    try:
        result = await provider.search_shopping(identity, record)
    finally:
        await provider.close()
    assert len(calls) == 2
    assert len(result["offers"]) == 5
    assert all("/tw/" in item["url"] for item in result["offers"][:3])
    assert [attempt["candidate_count"] for attempt in result["search_attempts"]] == [3, 3]


async def test_blocked_retailer_stops_further_reads_for_same_host(
    tmp_path, identity, record, photos, monkeypatch
):
    from wardrobe.product_evidence import ProductFetchError

    calls = []

    async def fetch(url, market):
        calls.append(url)
        raise ProductFetchError("Fixture website requires verification.", "blocked", http_status=403)

    monkeypatch.setattr("wardrobe.providers.fetch_product", fetch)
    provider = make_provider(
        tmp_path, lambda request: pytest.fail("Blocked page must not reach image comparison")
    )
    try:
        result = await provider.verify_shopping(
            identity, record, multiple_candidates(3), *photos, tmp_path / "shopping"
        )
    finally:
        await provider.close()
    assert len(calls) == 1
    assert len(result["checked_candidates"]) == 3
    assert result["offers"] == []
    assert all(item["status"] == "unverified" for item in result["checked_candidates"])
    assert all("same site" in item["reasons"][0].lower() for item in result["checked_candidates"][1:])


async def test_sold_out_without_image_is_rejected_without_model_call(
    tmp_path, identity, record, photos, monkeypatch
):
    from wardrobe.product_evidence import ProductFetchError

    async def fetch(url, market):
        raise ProductFetchError("Product page explicitly indicates sold out or discontinued.", "out_of_stock")

    monkeypatch.setattr("wardrobe.providers.fetch_product", fetch)
    provider = make_provider(
        tmp_path, lambda request: pytest.fail("Sold out product must not reach image comparison")
    )
    try:
        result = await provider.verify_shopping(
            identity, record, shopping_result(), *photos, tmp_path / "shopping"
        )
    finally:
        await provider.close()
    assert not result["offers"]
    assert result["checked_candidates"][0]["status"] == "rejected"
    assert result["checked_candidates"][0]["reasons"] == [
        "Product page explicitly indicates sold out or discontinued."
    ]


async def test_same_host_search_hits_do_not_early_stop_and_new_host_gets_priority(tmp_path, identity, record):
    identity.update(status="unknown")
    calls = []

    def respond(request):
        calls.append(request)
        host = "retailer-a.example.com" if len(calls) == 1 else "retailer-b.example.com"
        hits = [
            {
                "type": "text_result",
                "url": f"https://{host}/tw/products/fixture-{i}",
                "title": f"Fixture T-shirt {i}",
                "snippet": "",
            }
            for i in range(3)
        ]
        return httpx.Response(200, json=search_response(offers=[], hits=hits, cited_urls=[]))

    provider = make_provider(tmp_path, respond)
    try:
        result = await provider.search_shopping(identity, record)
    finally:
        await provider.close()
    assert len(calls) == 2
    assert [a["candidate_count"] for a in result["search_attempts"]] == [3, 3]
    assert "retailer-a" in result["offers"][0]["url"] and "retailer-b" in result["offers"][1]["url"]
    assert len(result["offers"]) == 5


async def test_blocked_host_skips_reach_saved_fourth_and_fifth_candidates(
    tmp_path, identity, record, photos, monkeypatch
):
    from wardrobe.product_evidence import ProductFetchError

    shopping = multiple_candidates(5)
    for i, host in [(3, "other-a.example.com"), (4, "other-b.example.com")]:
        url = f"https://{host}/tw/product/{i}"
        shopping["offers"][i]["url"] = shopping["sources"][i]["url"] = url
    calls = []

    async def fetch(url, market):
        calls.append(url)
        if len(calls) == 1:
            raise ProductFetchError("Fixture blocked", "blocked", http_status=403)
        return fetched_product(page_url=url)

    monkeypatch.setattr("wardrobe.providers.fetch_product", fetch)
    provider = make_provider(tmp_path, lambda request: httpx.Response(200, json=comparison_response()))
    destination = tmp_path / "shopping"
    try:
        result = await provider.verify_shopping(identity, record, shopping, *photos, destination)
    finally:
        await provider.close()
    assert calls == [shopping["offers"][i]["url"] for i in [0, 3, 4]]
    assert len(result["checked_candidates"]) == 5
    assert [x["status"] for x in result["checked_candidates"]] == [
        "unverified",
        "unverified",
        "unverified",
        "accepted",
        "accepted",
    ]
    assert [x["url"] for x in result["offers"]] == calls[1:]
    assert sorted(p.name for p in destination.glob("*.png")) == ["2.png", "3.png"]
    assert all("same site" in x["reasons"][0].lower() for x in result["checked_candidates"][1:3])


@pytest.mark.parametrize("model_selects", [True, False])
@pytest.mark.parametrize("market", ["TW", "global"])
async def test_retrieved_oman_storefront_does_not_consume_taiwan_page_budget(
    tmp_path, identity, record, photos, monkeypatch, model_selects, market
):
    """Replay the actual locale defect; all API, DNS and merchant access is fake."""
    from wardrobe.product_evidence import ProductFetchError

    oman = "https://www.adidas.com/om/en/essentials-climawarm-3-stripes-insulated-hooded-jacket/JM4040.html"
    # Unknown storefronts remain candidates, never presumed Taiwanese or verified.
    unknown = [f"https://retailer-{i}.example.com/products/fixture-jacket" for i in range(3)]
    urls = [oman, *unknown]
    identity.update(brand="adidas", evidence_type="logo", visible_text=None, product_code=None)
    identity["visual_profile"]["type"] = "hooded jacket"
    identity["visual_profile"]["category"] = "outerwear"
    before = copy.deepcopy(identity)
    hits = [
        {
            "type": "text_result",
            "url": url,
            "title": "Clothing - Essentials CLIMAWARM 3-Stripes Insulated Hooded Jacket - Blue | adidas Oman"
            if url == oman
            else "adidas hooded jacket fixture",
            "snippet": "",
        }
        for url in urls
    ]
    fetched, searches = [], []

    async def fetch(url, market):
        fetched.append(url)
        raise ProductFetchError("Fixture product explicitly sold out.", "out_of_stock")

    def respond(request):
        assert request.url.path == "/v1/responses", "No model image calls for sold-out fixtures"
        searches.append(request)
        return httpx.Response(
            200,
            json=search_response(
                offers=[offer(url, match="similar") for url in urls] if model_selects else [],
                hits=hits,
                cited_urls=[],
            ),
        )

    monkeypatch.setattr("wardrobe.providers.fetch_product", fetch)
    provider = make_provider(tmp_path, respond)
    try:
        shopping = await provider.search_shopping(identity, record, market)
        result = await provider.verify_shopping(
            identity, record, shopping, *photos, tmp_path / "shopping", market
        )
    finally:
        await provider.close()
    assert len(searches) == (2 if market == "TW" else 1)
    assert fetched == (unknown if market == "TW" else [oman, *unknown[:2]])
    if market == "TW":
        assert all(candidate["url"] != oman for candidate in shopping["offers"])
        assert all(attempt["candidate_count"] == 3 for attempt in shopping["search_attempts"])
    else:
        assert shopping["offers"][0]["url"] == oman
    assert result["offers"] == []
    assert all(candidate["status"] == "rejected" for candidate in result["checked_candidates"][:3])
    assert identity == before

"""Offline public-page evidence tests; no real websites, DNS, accounts or models."""

import asyncio
import gzip
import io
import json
import ssl
from datetime import datetime
from html import escape

import httpx
import pytest
from PIL import Image

from wardrobe import product_evidence as evidence


PAGE = "https://shop.public-store.com/tw/products/blue-shirt"
IMAGE = "https://images.public-store.com/blue-shirt.png"
PUBLIC_IP = "93.184.216.34"


def picture(format="PNG", size=(80, 100)):
    buffer = io.BytesIO()
    Image.new("RGB", size, "blue").save(buffer, format)
    return buffer.getvalue()


def product(**changes):
    return {
        "@context": "https://schema.org",
        "@type": "Product",
        "name": "Fixture blue shirt",
        "description": "<p>Fixture cotton shirt.</p>",
        "brand": {"@type": "Brand", "name": "Fixture brand"},
        "mpn": "FIXTURE-42",
        "sku": "retailer-id-1",
        "color": "Blue",
        "image": IMAGE,
        "offers": {
            "@type": "Offer",
            "availability": "https://schema.org/InStock",
            "price": "500",
            "priceCurrency": "TWD",
        },
        **changes,
    }


def page(document=None, *, meta=None, title="Product fixture"):
    tags = "".join(
        f'<meta property="{escape(key)}" content="{escape(value)}">' for key, value in (meta or {}).items()
    )
    script = (
        f'<script type="application/ld+json">{json.dumps(document)}</script>' if document is not None else ""
    )
    return (
        f"<html><head><title>{title}</title>{tags}{script}</head><body>Fixture product</body></html>".encode()
    )


def reply(body, content_type="text/html", status=200, **headers):
    return httpx.Response(
        status, headers={"content-type": content_type, **headers}, stream=httpx.ByteStream(body)
    )


class Network:
    def __init__(self, routes=None, addresses=None):
        self.routes = routes or {PAGE: page(product()), IMAGE: picture()}
        self.addresses = addresses or {}
        self.requests, self.resolutions = [], []
        self.transport = httpx.MockTransport(self.respond)

    async def resolve(self, host):
        self.resolutions.append(host)
        value = self.addresses.get(host, [PUBLIC_IP])
        if callable(value):
            return value()
        return value

    def respond(self, request):
        self.requests.append(request)
        assert request.url.host == PUBLIC_IP or request.url.host == "2606:4700:4700::1111"
        assert request.headers["host"] == request.extensions["sni_hostname"]
        assert "cookie" not in request.headers and "authorization" not in request.headers
        assert "proxy-authorization" not in request.headers
        key = str(request.url.copy_with(host=request.headers["host"]))
        route = self.routes[key]
        if callable(route):
            return route(request)
        return reply(route, "image/png" if key.endswith(".png") else "text/html")

    async def fetch(self, url=PAGE, market="TW"):
        return await evidence._fetch_product(url, market, resolver=self.resolve, transport=self.transport)


async def test_reads_actual_product_metadata_and_image_with_pinned_destination():
    network = Network()
    result = await network.fetch()
    assert result["page_url"] == PAGE and result["image_url"] == IMAGE
    assert result["title"] == "Fixture blue shirt" and result["description"] == "Fixture cotton shirt."
    assert result["brand"] == "Fixture brand" and result["product_code"] == "FIXTURE-42"
    assert result["color"] == "Blue" and result["availability"] == "in_stock"
    assert result["market"] == "in_scope" and "shipping" in result["market_evidence"]
    assert result["image_bytes"] == picture() and result["image_mime"] == "image/png"
    assert datetime.fromisoformat(result["checked_at"]).tzinfo is not None
    assert network.resolutions == ["shop.public-store.com", "images.public-store.com"]
    assert [request.extensions["sni_hostname"] for request in network.requests] == network.resolutions
    assert all(request.headers["connection"] == "close" for request in network.requests)


@pytest.mark.parametrize(
    "url",
    [
        "http://shop.public-store.com/p/1",
        "file:///etc/passwd",
        "https://user:secret@shop.public-store.com/p/1",
        "https://127.0.0.1/p/1",
        "https://[::1]/p/1",
        "https://10.0.0.5/p/1",
        "https://169.254.169.254/meta",
        "https://localhost/p/1",
        "https://shop.internal/p/1",
        "https://shop.public-store.com:8080/p/1",
        "https://shop.public-store.com/\nproduct",
        "https://shop.public-store.com\\@127.0.0.1/x",
    ],
)
async def test_unsafe_url_is_rejected_before_dns_or_transport(url):
    network = Network()
    with pytest.raises(evidence.ProductFetchError) as caught:
        await network.fetch(url)
    assert caught.value.status == "unsafe_url"
    assert network.resolutions == network.requests == []
    assert "secret" not in caught.value.reason


@pytest.mark.parametrize(
    "addresses",
    [
        [],
        ["127.0.0.1"],
        [PUBLIC_IP, "192.168.1.2"],
        ["169.254.169.254"],
        ["::ffff:127.0.0.1"],
        ["2002:7f00:1::1"],
        ["64:ff9b::7f00:1"],
        ["224.0.0.1"],
        ["fc00::1"],
        ["invalid-address"],
    ],
)
async def test_every_dns_answer_must_be_a_direct_public_address(addresses):
    network = Network(addresses={"shop.public-store.com": addresses})
    with pytest.raises(evidence.ProductFetchError) as caught:
        await network.fetch()
    assert caught.value.status == "unsafe_url" and network.requests == []


async def test_ipv6_destination_retains_original_tls_name():
    network = Network(addresses={"shop.public-store.com": ["2606:4700:4700::1111"]})
    assert (await network.fetch())["market"] == "in_scope"
    assert network.requests[0].url.host == "2606:4700:4700::1111"
    assert network.requests[0].extensions["sni_hostname"] == "shop.public-store.com"


async def test_redirect_rechecks_dns_and_does_not_send_cookies_or_follow_private_target():
    redirected = "https://different.public-store.com/tw/products/shirt"
    network = Network(
        {
            PAGE: lambda request: reply(
                b"",
                status=302,
                location=redirected,
                **{"set-cookie": "login=secret; Domain=.public-store.com"},
            ),
        },
        addresses={"different.public-store.com": ["10.1.1.1"]},
    )
    with pytest.raises(evidence.ProductFetchError) as caught:
        await network.fetch()
    assert caught.value.status == "unsafe_url" and len(network.requests) == 1
    assert network.resolutions == ["shop.public-store.com", "different.public-store.com"]


async def test_redirect_same_host_is_resolved_again_and_rebinding_is_rejected():
    answers = iter([[PUBLIC_IP], ["127.0.0.1"]])
    network = Network(
        {PAGE: lambda request: reply(b"", status=302, location="/tw/products/final")},
        addresses={"shop.public-store.com": lambda: next(answers)},
    )
    with pytest.raises(evidence.ProductFetchError) as caught:
        await network.fetch()
    assert caught.value.status == "unsafe_url" and len(network.requests) == 1


async def test_valid_redirects_use_final_page_and_image_urls_without_cookie_state():
    final_page, final_image = (
        "https://shop.public-store.com/us/products/blue-shirt",
        "https://images.public-store.com/final.png",
    )
    network = Network(
        {
            PAGE: lambda request: reply(
                b"", status=302, location=final_page, **{"set-cookie": "session=do-not-send"}
            ),
            final_page: page(product()),
            IMAGE: lambda request: reply(
                b"", status=302, location=final_image, **{"set-cookie": "image-token=do-not-send"}
            ),
            final_image: picture(),
        }
    )
    result = await network.fetch()
    assert result["page_url"] == final_page and result["image_url"] == final_image
    assert result["market"] == "out_of_scope"
    assert len(network.requests) == len(network.resolutions) == 4


@pytest.mark.parametrize("status", [401, 403, 429])
async def test_auth_challenge_and_rate_limit_stop_without_retry(status):
    network = Network({PAGE: lambda request: reply(b"private response body secret-token", status=status)})
    with pytest.raises(evidence.ProductFetchError) as caught:
        await network.fetch()
    assert caught.value.status == "blocked" and caught.value.http_status == status
    assert "secret-token" not in caught.value.reason and len(network.requests) == 1


@pytest.mark.parametrize(
    "html",
    [
        b"<html><title>Just a moment...</title><p>Challenge</p></html>",
        b"<html><title>Security</title><p>Verify you are human</p></html>",
        b'<html><script src="/cf-chl-widget.js"></script></html>',
    ],
)
async def test_200_challenge_page_is_not_retried_or_treated_as_product(html):
    network = Network({PAGE: html})
    with pytest.raises(evidence.ProductFetchError) as caught:
        await network.fetch()
    assert caught.value.status == "blocked" and len(network.requests) == 1


# Reduced from the actual 2026-10-22 public ZARA response, with nonce/domain
# replaced. Original body SHA256: 756a7f4b445daac6297276f23f9c5e43fda1993b0eea425cddad5fd96526e537.
AKAMAI_INTERSTITIAL = b"""<!DOCTYPE html><html><head>
<meta http-equiv="refresh" content="5; URL='/tw/products/blue-shirt?v1=1&bm-verify=fixture-nonce'" />
<title>&nbsp;</title></head><body>
<iframe style="border: none; width: 100vw; height: 100vh;" src="/interstitial/ic.html"></iframe>
<script>function triggerInterstitialChallenge() {
var xhr = new XMLHttpRequest(); xhr.open("POST", "/_sec/verify?provider=interstitial", false);
xhr.send(JSON.stringify({"bm-verify": "fixture-nonce", "pow": 1}));
} triggerInterstitialChallenge();</script></body></html>"""


async def test_real_akamai_interstitial_shape_stops_before_refresh_script_or_image():
    network = Network({PAGE: AKAMAI_INTERSTITIAL})
    with pytest.raises(evidence.ProductFetchError) as caught:
        await network.fetch()
    assert caught.value.status == "blocked"
    assert "fixture-nonce" not in caught.value.reason
    assert len(network.requests) == 1
    assert network.resolutions == ["shop.public-store.com"]


@pytest.mark.parametrize("http_equiv", ["refresh", " ReFrEsH "])
async def test_interstitial_uses_actual_refresh_query_key_and_exact_frame_path(http_equiv):
    fragment = (
        f'<meta http-equiv="{http_equiv}" content="0; uRl=/tw/products/blue-shirt?bm-verify=">'
        '<iframe src="https://shop.public-store.com/interstitial/ic.html?lang=tw"></iframe>'
    ).encode()
    # Product metadata does not override an active challenge redirect/frame.
    network = Network({PAGE: page(product()) + fragment})
    with pytest.raises(evidence.ProductFetchError) as caught:
        await network.fetch()
    assert caught.value.status == "blocked" and len(network.requests) == 1


@pytest.mark.parametrize(
    "fragment",
    [
        '<script src="https://analytics.akamai.example/script.js"></script>'
        '<script>const endpoint = "/_sec/verify?provider=interstitial";</script>',
        '<iframe src="/interstitial/ic.html"></iframe>',
        '<meta http-equiv="refresh" content="5; URL=/tw/products/a?bm-verify=value">',
        '<meta name="refresh" content="5; URL=/tw/products/a?bm-verify=value">'
        '<iframe src="/interstitial/ic.html"></iframe>',
        '<meta http-equiv="refresh" content="5; URL=/tw/products/a?other=bm-verify%3Dvalue">'
        '<iframe src="/interstitial/ic.html"></iframe>',
        '<meta http-equiv="refresh" content="5; URL=/tw/products/a?prefix-bm-verify=value">'
        '<iframe src="/interstitial/ic.html"></iframe>',
        '<meta http-equiv="refresh" content="5; URL=/tw/products/a#bm-verify=value">'
        '<iframe src="/interstitial/ic.html"></iframe>',
        '<meta http-equiv="refresh" content="5; URL=/tw/products/a?bm-verify=value">'
        '<iframe src="/marketing/interstitial/ic.html"></iframe>',
        '<meta http-equiv="refresh" content="5; URL=https://[invalid?bm-verify=value">'
        '<iframe src="/interstitial/ic.html"></iframe>',
    ],
)
async def test_normal_product_analytics_or_partial_markers_do_not_count_as_interstitial(fragment):
    network = Network({PAGE: page(product()) + fragment.encode(), IMAGE: picture()})
    result = await network.fetch()
    assert result["title"] == "Fixture blue shirt"
    assert result["image_bytes"] == picture()
    assert len(network.requests) == 2


@pytest.mark.parametrize(
    "path,locale,market,expected",
    [
        ("/tw/products/a", None, "TW", "in_scope"),
        ("/zh-tw/products/a", None, "TW", "in_scope"),
        ("/en/tw/products/a", None, "TW", "in_scope"),
        ("/shop/tw/products/a", None, "TW", "in_scope"),
        ("/products/a", "zh_TW", "TW", "in_scope"),
        ("/us/products/a", None, "TW", "out_of_scope"),
        ("/en-gb/products/a", None, "TW", "out_of_scope"),
        ("/en/us/products/a", None, "TW", "out_of_scope"),
        ("/products/a", "en_US", "TW", "out_of_scope"),
        ("/products/a", None, "TW", "unknown"),
        ("/us/products/a", "en_US", "global", "in_scope"),
    ],
)
async def test_market_uses_actual_final_url_or_primary_locale_not_currency(path, locale, market, expected):
    url = "https://shop.public-store.com" + path
    network = Network({url: page(product(), meta={"og:locale": locale} if locale else {}), IMAGE: picture()})
    result = await network.fetch(url, market)
    assert result["market"] == expected and result["market_evidence"]


async def test_taiwan_domain_is_evidence_but_unrelated_tw_in_query_is_not():
    url = "https://shop.public-store.tw/products/a"
    network = Network({url: page(product()), IMAGE: picture()})
    assert (await network.fetch(url))["market"] == "in_scope"
    url = "https://shop.public-store.com/products/a?country=tw"
    network = Network({url: page(product()), IMAGE: picture()})
    assert (await network.fetch(url))["market"] == "unknown"


@pytest.mark.parametrize("domain", ["public-store.jp", "public-store.co.uk", "public-store.com.au"])
async def test_explicit_foreign_country_domain_is_outside_taiwan(domain):
    url = f"https://{domain}/products/a"
    network = Network({url: page(product()), IMAGE: picture()})
    assert (await network.fetch(url))["market"] == "out_of_scope"


@pytest.mark.parametrize(
    "url,locale",
    [
        (
            "https://www.adidas.com/om/en/essentials-climawarm-3-stripes-insulated-hooded-jacket/JM4040.html",
            None,
        ),
        ("https://shop.public-store.com/en-OM/products/a", None),
        ("https://shop.public-store.com/en/om/products/a", None),
        ("https://shop.public-store.om/products/a", None),
        ("https://shop.public-store.com.om/products/a", None),
        ("https://shop.public-store.com/products/a", "en_OM"),
        ("https://shop.public-store.com/tw/products/a", "ar_OM"),
        ("https://shop.public-store.com/om/en/products/a", "zh_TW"),
    ],
)
@pytest.mark.parametrize("market", ["TW", "global"])
async def test_explicit_oman_locale_uses_same_market_policy_as_other_foreign_stores(url, locale, market):
    network = Network({url: page(product(), meta={"og:locale": locale} if locale else {}), IMAGE: picture()})
    result = await network.fetch(url, market)
    assert result["market"] == ("out_of_scope" if market == "TW" else "in_scope")
    if locale is None:
        assert evidence.product_market_hint(url, market) == result["market"]


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://shop.public-store.com/products/a", "unknown"),
        ("https://shop.public-store.com/products/om", "unknown"),
        ("https://shop.public-store.com/products/a?country=om", "unknown"),
        ("https://om.public-store.com/products/a", "unknown"),
        ("https://shop.public-store.com/tw/products/om", "in_scope"),
    ],
)
async def test_oman_mention_outside_explicit_storefront_does_not_infer_country(url, expected):
    network = Network({url: page(product(), title="Oman collection fixture"), IMAGE: picture()})
    assert (await network.fetch(url))["market"] == expected
    assert evidence.product_market_hint(url, "TW") == expected


def product_group(url=PAGE):
    """Shape from the saved public ProductGroup, with independent fixture data."""
    return {
        "@type": "ProductGroup",
        "name": "Fixture blue shirt",
        "brand": {"@type": "Brand", "name": "Fixture brand"},
        "hasVariant": [
            {
                "@type": "Product",
                "@id": url + "#size-" + size,
                "name": "Fixture blue shirt - Size " + size,
                "mpn": "FIXTURE-42",
                "size": size,
                "color": "Blue",
                "image": IMAGE,
                "offers": {"@type": "Offer", "url": url, "price": 500, "priceCurrency": "TWD"},
            }
            for size in ("S", "M", "L")
        ],
    }


def next_state(selected, **other):
    return (
        '<script id="__NEXT_DATA__" type="application/json">'
        + json.dumps({"props": {"pageProps": {"selectedProduct": selected, **other}}})
        + "</script>"
    ).encode()


async def test_product_group_collapses_matching_size_variants_and_inherits_brand():
    document = product_group()
    # Other colors in the group must not become the fetched product's image.
    foreign = product_group(PAGE + "-red")["hasVariant"][0]
    foreign.update(mpn="FIXTURE-RED", color="Red", image="https://images.public-store.com/red.png")
    document["hasVariant"].insert(0, foreign)
    network = Network({PAGE: page(document, meta={"og:type": "website"}), IMAGE: picture()})
    result = await network.fetch()
    assert result["title"] == "Fixture blue shirt"
    assert result["brand"] == "Fixture brand" and result["product_code"] == "FIXTURE-42"
    assert result["color"] == "Blue" and result["image_url"] == IMAGE
    assert result["availability"] == "unknown"
    assert len(network.requests) == 2


async def test_product_group_unicode_schema_url_matches_encoded_fetched_url():
    unicode_url = "https://shop.public-store.com/tw/t/短袖/FIXTURE-42"
    url = str(httpx.URL(unicode_url))
    network = Network({url: page([product_group(unicode_url)]), IMAGE: picture()})
    assert (await network.fetch(url))["product_code"] == "FIXTURE-42"


@pytest.mark.parametrize("mismatch", ["mpn", "color", "image", "brand", "no_code", "no_matching_url"])
async def test_product_group_ambiguous_or_unbound_variant_is_rejected(mismatch):
    document = product_group()
    if mismatch == "no_matching_url":
        document = product_group(PAGE + "?variant=another")
    elif mismatch == "no_code":
        for variant in document["hasVariant"]:
            variant.pop("mpn")
    else:
        document["hasVariant"][1][mismatch] = "Different fixture value"
    network = Network({PAGE: page(document, meta={"og:type": "product", "og:image": IMAGE})})
    with pytest.raises(evidence.ProductFetchError) as caught:
        await network.fetch()
    assert caught.value.status == "not_product" and len(network.requests) == 1


async def test_group_selected_state_out_of_stock_requires_matching_url_and_code():
    selected = {"styleColor": "FIXTURE-42", "pdpUrl": {"url": PAGE}, "statusModifier": "OUT_OF_STOCK"}
    body = page([product_group()], meta={"og:type": "website", "og:locale": "zh_TW"})
    network = Network({PAGE: body + next_state(selected), IMAGE: picture()})
    result = await network.fetch()
    assert result["availability"] == "out_of_stock" and result["market"] == "in_scope"
    assert result["product_code"] == "FIXTURE-42" and result["image_bytes"] == picture()


@pytest.mark.parametrize("mismatch", ["code", "url", "missing_url", "missing_code", "recommended_only"])
async def test_unrelated_unavailable_application_state_cannot_taint_selected_product(mismatch):
    selected = {"styleColor": "FIXTURE-42", "pdpUrl": {"url": PAGE}, "statusModifier": "OUT_OF_STOCK"}
    other = {}
    if mismatch == "code":
        selected["styleColor"] = "FIXTURE-OTHER"
    elif mismatch == "url":
        selected["pdpUrl"]["url"] = PAGE + "?variant=other"
    elif mismatch == "missing_url":
        selected.pop("pdpUrl")
    elif mismatch == "missing_code":
        selected.pop("styleColor")
    else:
        other["colorwayImages"] = [selected]
        selected = None
    network = Network({PAGE: page(product()) + next_state(selected, **other), IMAGE: picture()})
    assert (await network.fetch())["availability"] == "in_stock"


async def test_next_state_alone_does_not_establish_product_or_infer_stock_from_active_size():
    selected = {
        "styleColor": "FIXTURE-42",
        "pdpUrl": {"url": PAGE},
        "sizes": [{"status": "ACTIVE"}],
        "image": IMAGE,
    }
    network = Network({PAGE: page(meta={"og:type": "website", "og:image": IMAGE}) + next_state(selected)})
    with pytest.raises(evidence.ProductFetchError) as caught:
        await network.fetch()
    assert caught.value.status == "not_product"
    network = Network({PAGE: page(product_group()) + next_state(selected), IMAGE: picture()})
    assert (await network.fetch())["availability"] == "unknown"


@pytest.mark.parametrize("state", [[], {}, None])
async def test_malformed_selected_stock_state_is_ignored_without_promoting_availability(state):
    selected = {"styleColor": "FIXTURE-42", "pdpUrl": {"url": PAGE}, "statusModifier": state}
    network = Network({PAGE: page(product_group()) + next_state(selected), IMAGE: picture()})
    assert (await network.fetch())["availability"] == "unknown"


@pytest.mark.parametrize(
    "document",
    [
        {"@type": "ItemList", "itemListElement": [product()]},
        {"@type": "CollectionPage", "mainEntity": product()},
        {"@type": "Article", "about": product()},
        [product(name="First"), product(name="Second")],
    ],
)
async def test_collections_articles_and_ambiguous_multi_product_pages_are_rejected(document):
    network = Network({PAGE: page(document, meta={"og:type": "product", "og:image": IMAGE})})
    with pytest.raises(evidence.ProductFetchError) as caught:
        await network.fetch()
    assert caught.value.status == "not_product" and len(network.requests) == 1


async def test_explicit_webpage_main_entity_selects_primary_product_from_graph():
    document = {
        "@graph": [
            {"@type": "WebPage", "mainEntity": {"@id": "#main"}},
            product(**{"@id": "#main"}),
            product(**{"@id": "#recommendation", "name": "Other"}),
        ]
    }
    network = Network({PAGE: page(document), IMAGE: picture()})
    assert (await network.fetch())["title"] == "Fixture blue shirt"


@pytest.mark.parametrize("path", ["/search?q=shirt", "/collections/shirts", "/blog/product-story"])
async def test_listing_or_article_url_is_rejected_even_if_it_embeds_a_product(path):
    url = "https://shop.public-store.com" + path
    network = Network({url: page(product())})
    with pytest.raises(evidence.ProductFetchError) as caught:
        await network.fetch(url)
    assert caught.value.status == "not_product" and len(network.requests) == 1


async def test_shopify_collection_product_path_still_identifies_a_single_product():
    url = "https://shop.public-store.com/collections/shirts/products/one-shirt"
    network = Network({url: page(product()), IMAGE: picture()})
    assert (await network.fetch(url))["title"] == "Fixture blue shirt"


async def test_open_graph_product_metadata_and_relative_main_image_are_supported():
    relative = "https://shop.public-store.com/photo.png"
    network = Network(
        {
            PAGE: page(
                meta={
                    "og:type": "product",
                    "og:title": "OG fixture product",
                    "og:image": "/photo.png",
                    "og:description": "Explicit product metadata",
                    "product:brand": "OG brand",
                    "product:retailer_item_id": "OG42",
                    "product:availability": "out of stock",
                }
            ),
            relative: picture(),
        }
    )
    result = await network.fetch()
    assert result["title"] == "OG fixture product" and result["brand"] == "OG brand"
    assert result["product_code"] == "OG42" and result["availability"] == "out_of_stock"
    assert result["image_url"] == relative


@pytest.mark.parametrize(
    "image",
    [
        None,
        "",
        "data:image/png;base64,AAAA",
        "http://images.public-store.com/x.png",
        "https://images.public-store.com/x.svg",
    ],
)
async def test_no_actual_supported_main_image_does_not_guess_or_fetch_a_url(image):
    network = Network({PAGE: page(product(image=image))})
    with pytest.raises(evidence.ProductFetchError) as caught:
        await network.fetch()
    assert caught.value.status == "missing_image" and len(network.requests) == 1


async def test_image_redirect_to_private_network_is_rejected():
    network = Network(
        {
            PAGE: page(product()),
            IMAGE: lambda request: reply(b"", status=302, location="https://127.0.0.1/image.png"),
        }
    )
    with pytest.raises(evidence.ProductFetchError) as caught:
        await network.fetch()
    assert caught.value.status == "unsafe_url" and len(network.requests) == 2


@pytest.mark.parametrize(
    "availability,expected",
    [
        ("https://schema.org/InStock", "in_stock"),
        ("https://schema.org/OutOfStock", "out_of_stock"),
        ("https://schema.org/PreOrder", "unknown"),
        (None, "unknown"),
    ],
)
async def test_availability_is_only_reported_from_actual_structured_offer(availability, expected):
    network = Network(
        {PAGE: page(product(offers={"@type": "Offer", "availability": availability})), IMAGE: picture()}
    )
    assert (await network.fetch())["availability"] == expected


async def test_conflicting_offer_stock_is_unknown():
    network = Network(
        {
            PAGE: page(product(offers=[{"availability": "InStock"}, {"availability": "OutOfStock"}])),
            IMAGE: picture(),
        }
    )
    assert (await network.fetch())["availability"] == "unknown"


@pytest.mark.parametrize(
    "format,mime", [("JPEG", "image/jpeg"), ("PNG", "image/png"), ("WEBP", "image/webp")]
)
async def test_valid_image_bytes_and_actual_mime_are_preserved(format, mime):
    data = picture(format)
    network = Network({PAGE: page(product()), IMAGE: lambda request: reply(data, mime)})
    result = await network.fetch()
    assert result["image_bytes"] == data and result["image_mime"] == mime


@pytest.mark.parametrize(
    "data,mime",
    [
        (b"<svg></svg>", "image/svg+xml"),
        (b"not an image", "image/png"),
        (picture(size=(1, 1)), "image/png"),
        (picture("GIF"), "image/gif"),
    ],
)
async def test_invalid_svg_tracking_or_unsupported_images_are_rejected(data, mime):
    network = Network({PAGE: page(product()), IMAGE: lambda request: reply(data, mime)})
    with pytest.raises(evidence.ProductFetchError) as caught:
        await network.fetch()
    assert caught.value.status == "invalid_image"


async def test_decoded_pixel_limit_is_checked_before_loading_image(monkeypatch):
    monkeypatch.setattr(evidence, "MAX_IMAGE_PIXELS", 5000)
    network = Network()
    with pytest.raises(evidence.ProductFetchError) as caught:
        await network.fetch()
    assert caught.value.status == "invalid_image"


@pytest.mark.parametrize("kind", ["declared", "streamed", "compressed"])
async def test_html_byte_limit_applies_to_headers_streaming_and_decompression(monkeypatch, kind):
    monkeypatch.setattr(evidence, "MAX_HTML_BYTES", 1024)
    data = b"x" * 2048

    def route(request):
        if kind == "declared":
            return reply(b"x", **{"content-length": "2048"})
        if kind == "compressed":
            return reply(gzip.compress(data), **{"content-encoding": "gzip"})
        return reply(data)

    network = Network({PAGE: route})
    with pytest.raises(evidence.ProductFetchError) as caught:
        await network.fetch()
    assert caught.value.status == "too_large" and len(network.requests) == 1


async def test_small_gzip_page_is_decoded_with_same_limits():
    network = Network(
        {
            PAGE: lambda request: reply(gzip.compress(page(product())), **{"content-encoding": "gzip"}),
            IMAGE: picture(),
        }
    )
    assert (await network.fetch())["product_code"] == "FIXTURE-42"


async def test_image_byte_limit_stops_before_validation(monkeypatch):
    monkeypatch.setattr(evidence, "MAX_IMAGE_BYTES", 32)
    network = Network()
    with pytest.raises(evidence.ProductFetchError) as caught:
        await network.fetch()
    assert caught.value.status == "too_large"


async def test_redirect_loop_stops_without_unbounded_requests():
    network = Network({PAGE: lambda request: reply(b"", status=302, location=PAGE)})
    with pytest.raises(evidence.ProductFetchError):
        await network.fetch()
    assert len(network.requests) == 1


async def test_timeout_is_safe_and_does_not_retry(monkeypatch):
    monkeypatch.setattr(evidence, "TOTAL_TIMEOUT", 0.02)
    calls = []

    async def resolve(host):
        calls.append(host)
        await asyncio.Event().wait()

    with pytest.raises(evidence.ProductFetchError) as caught:
        await evidence._fetch_product(
            PAGE, resolver=resolve, transport=httpx.MockTransport(lambda request: reply(b""))
        )
    assert caught.value.status == "timeout" and len(calls) == 1


async def test_cancellation_propagates_without_retries():
    started, stopped = asyncio.Event(), asyncio.Event()

    async def resolve(host):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    task = asyncio.create_task(evidence._fetch_product(PAGE, resolver=resolve))
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stopped.is_set()


async def test_default_client_disables_proxy_trust_reuse_and_preserves_tls_verification(monkeypatch):
    real_client = httpx.AsyncClient
    network, options = Network(), []

    def factory(**kwargs):
        options.append(kwargs.copy())
        kwargs["transport"] = network.transport
        client = real_client(**kwargs)
        assert client.trust_env is False
        return client

    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("SSL_CERT_FILE", "/private/nonexistent-secret-certificate")
    monkeypatch.setattr(evidence.httpx, "AsyncClient", factory)
    monkeypatch.setattr(evidence, "_resolve", network.resolve)
    result = await evidence.fetch_product(PAGE)
    assert result["title"] == "Fixture blue shirt"
    assert options[0]["verify"] is True and options[0]["trust_env"] is False
    assert options[0]["limits"].max_keepalive_connections == 0
    # The production transport also uses the normal verified SSLContext.
    transport = httpx.AsyncHTTPTransport(verify=True, trust_env=False)
    assert transport._pool._ssl_context.check_hostname is True
    assert transport._pool._ssl_context.verify_mode == ssl.CERT_REQUIRED
    await transport.aclose()


@pytest.mark.parametrize("locale", ["hu", "nl", "pl", "ro", "cz", "br", "mx"])
def test_explicit_foreign_storefront_locale_never_looks_like_taiwan(locale):
    assert (
        evidence.product_market_hint(f"https://retailer.example.com/{locale}/en/t/fixture", "TW")
        == "out_of_scope"
    )
    assert (
        evidence.product_market_hint(f"https://retailer.example.com/{locale}/en/t/fixture", "global")
        == "in_scope"
    )


@pytest.mark.parametrize(
    "availability",
    ["https://schema.org/OutOfStock", "https://schema.org/Discontinued", "https://schema.org/SoldOut"],
)
async def test_explicitly_unavailable_product_without_image_reports_sold_out_first(availability):
    network = Network(
        {PAGE: page(product(image=None, offers={"@type": "Offer", "availability": availability}))}
    )
    with pytest.raises(evidence.ProductFetchError) as caught:
        await network.fetch()
    assert caught.value.status == "out_of_stock"
    assert "sold out or discontinued" in caught.value.reason
    assert len(network.requests) == 1


@pytest.mark.parametrize(
    "availability", [None, "https://schema.org/InStock", "https://schema.org/PreOrder", "unknown"]
)
async def test_missing_image_without_explicit_unavailability_stays_missing_image(availability):
    network = Network(
        {PAGE: page(product(image=None, offers={"@type": "Offer", "availability": availability}))}
    )
    with pytest.raises(evidence.ProductFetchError) as caught:
        await network.fetch()
    assert caught.value.status == "missing_image"
    assert "sold out" not in caught.value.reason
    assert len(network.requests) == 1


STOREFRONT_PAGE = "https://shop.public-store.com/en/products/blue-shirt"


def storefront_data():
    return (
        {
            "appCoreHost": "shoplineapp.com",
            "pageType": "products-show",
            "merchantId": "fixture-merchant",
            "currentPath": "/en/products/blue-shirt",
            "requestCountry": "TW",
            "merchantData": {
                "_id": "fixture-merchant",
                "base_country_code": "TW",
                "base_currency": {"iso_code": "TWD"},
                "base_currency_code": "TWD",
                "brand_home_url": "https://shop.public-store.com",
            },
        },
        {"_id": "retailer-id-1", "owner_id": "fixture-merchant"},
    )


def storefront_script(config, current, quote='"'):
    def literal(value):
        encoded = json.dumps(json.dumps(value, ensure_ascii=False), ensure_ascii=False)
        return "'" + encoded[1:-1].replace("'", "\\'") + "'" if quote == "'" else encoded

    return (
        f"<script>\nwindow.mainConfig = JSON.parse({literal(config)});\n</script>"
        f"<script>\napp.value('product', JSON.parse({literal(current)}));\n</script>"
    ).encode()


@pytest.mark.parametrize("quote", ['"', "'"])
async def test_bound_current_merchant_and_product_establish_taiwan_storefront(quote):
    config, current = storefront_data()
    config["untrusted_label"] = "A fixture's 漢字 label"
    network = Network(
        {STOREFRONT_PAGE: page(product()) + storefront_script(config, current, quote), IMAGE: picture()}
    )
    result = await network.fetch(STOREFRONT_PAGE)
    assert result["market"] == "in_scope"
    assert (
        "merchant data" in result["market_evidence"]
        and "shipping and checkout conditions are not yet confirmed" in result["market_evidence"]
    )
    assert result["availability"] == "in_stock"
    assert result["product_code"] == "FIXTURE-42" and result["brand"] == "Fixture brand"
    assert result["image_bytes"] == picture() and len(network.requests) == 2


@pytest.mark.parametrize(
    "case",
    [
        "other_path",
        "other_owner",
        "other_product",
        "other_merchant",
        "request_country_only",
        "base_currency",
        "product_currency",
        "other_home",
        "other_page_type",
        "no_current_path",
        "duplicate_config",
        "duplicate_product",
        "nonliteral",
        "oversized",
        "not_script",
        "foreign_locale",
    ],
)
async def test_merchant_evidence_cannot_override_missing_binding_or_foreign_locale(case):
    config, current = storefront_data()
    doc, meta = product(), {}
    if case == "other_path":
        config["currentPath"] = "/en/products/another-shirt"
    if case == "other_owner":
        current["owner_id"] = "another-merchant"
    if case == "other_product":
        current["_id"] = "recommendation-product"
    if case == "other_merchant":
        config["merchantData"]["_id"] = "another-merchant"
    if case == "request_country_only":
        config["merchantData"]["base_country_code"] = "HK"
    if case == "base_currency":
        config["merchantData"]["base_currency"]["iso_code"] = "USD"
    if case == "product_currency":
        doc["offers"]["priceCurrency"] = "USD"
    if case == "other_home":
        config["merchantData"]["brand_home_url"] = "https://other-store.com"
    if case == "other_page_type":
        config["pageType"] = "categories-show"
    if case == "no_current_path":
        config.pop("currentPath")
    if case == "oversized":
        config["padding"] = "x" * 131_073
    if case == "foreign_locale":
        meta["og:locale"] = "en_US"
    scripts = storefront_script(config, current)
    if case == "duplicate_config":
        scripts += scripts[: scripts.index(b"</script>") + 9]
    if case == "duplicate_product":
        scripts += scripts[scripts.index(b"</script>") + 9 :]
    if case == "nonliteral":
        scripts = scripts.replace(b"JSON.parse(", b"JSON.parse(evaluateData(")
    if case == "not_script":
        scripts = scripts.replace(b"<script>", b"<div>").replace(b"</script>", b"</div>")
    network = Network({STOREFRONT_PAGE: page(doc, meta=meta) + scripts, IMAGE: picture()})
    result = await network.fetch(STOREFRONT_PAGE)
    assert result["market"] == ("out_of_scope" if case == "foreign_locale" else "unknown")


async def test_taiwan_merchant_evidence_does_not_change_out_of_stock_or_image_requirements():
    config, current = storefront_data()
    document = product(
        image=None,
        offers={"@type": "Offer", "priceCurrency": "TWD", "availability": "https://schema.org/OutOfStock"},
    )
    network = Network({STOREFRONT_PAGE: page(document) + storefront_script(config, current)})
    with pytest.raises(evidence.ProductFetchError) as error:
        await network.fetch(STOREFRONT_PAGE)
    assert error.value.status == "out_of_stock" and len(network.requests) == 1

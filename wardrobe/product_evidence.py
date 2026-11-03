"""Bounded public product-page evidence, without accounts, cookies or URL guessing.

Connections use a validated IP as their network destination and the original
hostname for Host/SNI and certificate verification. Each redirect is resolved
again. Keep-alive is disabled: two hosts sharing an IP must not share a TLS
connection that was authenticated for only the first host.

Metadata describes the page's claims. It does not establish authenticity,
ownership, the original seller, or whether checkout/shipping will succeed.
"""

from __future__ import annotations

import asyncio
import io
import ipaddress
import json
import re
import socket
import warnings
import zlib
from datetime import datetime, timezone
from html import unescape
from html.parser import HTMLParser
from urllib.parse import parse_qs, unquote, urljoin, urlsplit

import httpx
from PIL import Image

from .identity_schemas import normalize_source_url

MAX_HTML_BYTES = 3 * 1024 * 1024
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGE_PIXELS = 16_000_000
MAX_REDIRECTS = 3
TOTAL_TIMEOUT = 30.0
_CHUNK = 16_384
_REDIRECTS = {301, 302, 303, 307, 308}
_MIMES = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}


class ProductFetchError(Exception):
    """Safe display reason plus stable status; never includes response bodies."""

    def __init__(self, reason: str, status: str = "unavailable", *, http_status: int | None = None):
        self.reason, self.status, self.http_status = reason, status, http_status
        super().__init__(reason)


def _safe_url(url):
    try:
        return normalize_source_url(url)
    except (ValueError, TypeError):
        raise ProductFetchError("Product source must be a public HTTPS URL.", "unsafe_url") from None


def _public_ip(value):
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    if not address.is_global or address.is_multicast or address.is_unspecified:
        return False
    if isinstance(address, ipaddress.IPv6Address):
        # Reject transition mechanisms that can encode a different/private IPv4
        # destination even when the outer IPv6 address appears globally routed.
        if address.ipv4_mapped or address.sixtofour or address.teredo:
            return False
        if address in ipaddress.ip_network("64:ff9b::/96"):
            return False
    return True


async def _resolve(host):
    try:
        direct = ipaddress.ip_address(host)
    except ValueError:
        records = await asyncio.get_running_loop().getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        return list(dict.fromkeys(record[4][0] for record in records))
    return [str(direct)]


async def _bounded_body(response, limit):
    length = response.headers.get("content-length")
    if length:
        try:
            if int(length) < 0 or int(length) > limit:
                raise ProductFetchError("Product source content exceeds fetch size limit.", "too_large")
        except ValueError:
            raise ProductFetchError("Product source content-length format is invalid.") from None
    encoding = response.headers.get("content-encoding", "identity").strip().lower()
    if encoding not in {"", "identity", "gzip", "deflate"}:
        raise ProductFetchError("Product source uses an unsupported compression encoding.")
    decoder = (
        zlib.decompressobj(31 if encoding == "gzip" else zlib.MAX_WBITS)
        if encoding in {"gzip", "deflate"}
        else None
    )
    output, raw_size = bytearray(), 0
    try:
        async for chunk in response.aiter_raw(chunk_size=_CHUNK):
            raw_size += len(chunk)
            if raw_size > limit:
                raise ProductFetchError("Product source content exceeds fetch size limit.", "too_large")
            decoded = decoder.decompress(chunk, limit - len(output) + 1) if decoder else chunk
            output.extend(decoded)
            if len(output) > limit or (decoder and decoder.unconsumed_tail):
                raise ProductFetchError("Product source content exceeds fetch size limit.", "too_large")
            if decoder and decoder.unused_data:
                raise ProductFetchError("Product source compressed content format is not supported.")
        if decoder and not decoder.eof:
            raise ProductFetchError("Product source compressed content is incomplete.")
    except zlib.error:
        raise ProductFetchError("Product source compressed content cannot be read.") from None
    if not output:
        raise ProductFetchError("Product source returned no readable content.")
    return bytes(output)


async def _read(client, url, *, kind, resolver):
    url, visited = _safe_url(url), set()
    for redirect in range(MAX_REDIRECTS + 1):
        if url in visited:
            raise ProductFetchError("Product source has a redirect loop; fetch stopped.")
        visited.add(url)
        parsed = urlsplit(url)
        addresses = await resolver(parsed.hostname)
        if not addresses or len(addresses) > 64 or any(not _public_ip(value) for value in addresses):
            raise ProductFetchError(
                "Product source resolved to a non-public network address; fetch stopped.", "unsafe_url"
            )
        address = next((value for value in addresses if ":" not in value), addresses[0])
        destination = httpx.URL(url).copy_with(host=address)
        host_header = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
        # httpcore/_async/connection.py passes this extension directly as
        # start_tls(server_hostname=...), retaining standard cert verification.
        request = httpx.Request(
            "GET",
            destination,
            headers={
                "Host": host_header,
                "User-Agent": "Rewear-Product-Evidence/1.0",
                "Accept": "text/html,application/xhtml+xml"
                if kind == "html"
                else "image/jpeg,image/png,image/webp",
                "Accept-Encoding": "identity",
                "Connection": "close",
            },
            extensions={"sni_hostname": parsed.hostname},
        )
        response = await client.send(request, stream=True, auth=None, follow_redirects=False)
        try:
            status = response.status_code
            if status in {401, 403, 429}:
                raise ProductFetchError(
                    "Product source requires login, verification, or restricts access; fetch stopped.",
                    "blocked",
                    http_status=status,
                )
            if status in _REDIRECTS:
                location = response.headers.get("location")
                if not location or redirect == MAX_REDIRECTS:
                    raise ProductFetchError(
                        "Product source has too many redirects or is missing a destination."
                    )
                url = _safe_url(urljoin(url, location))
                continue
            if status != 200:
                raise ProductFetchError("Product source is currently unreadable.", http_status=status)
            mime = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if kind == "html" and mime not in {"text/html", "application/xhtml+xml"}:
                raise ProductFetchError("This URL does not serve a product page.", "not_product")
            if kind == "image" and mime not in {*_MIMES.values(), "application/octet-stream", ""}:
                raise ProductFetchError(
                    "Product main image is not a supported static image format.", "invalid_image"
                )
            body = await _bounded_body(response, MAX_HTML_BYTES if kind == "html" else MAX_IMAGE_BYTES)
            return url, body, response.headers.get("content-type", "")
        finally:
            await response.aclose()
    raise ProductFetchError("Product source fetch could not be completed.")


def _verification_refresh(content):
    if not isinstance(content, str) or len(content) > 10_000:
        return False
    match = re.fullmatch(r"\s*\d+(?:\.\d+)?\s*;\s*url\s*=\s*(.*?)\s*", content, re.I | re.S)
    if not match:
        return False
    target = match.group(1)
    if len(target) >= 2 and target[0] in {"'", '"'} and target[-1] == target[0]:
        target = target[1:-1]
    try:
        return "bm-verify" in parse_qs(urlsplit(target).query, keep_blank_values=True, max_num_fields=128)
    except ValueError:
        return False


class _Metadata(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta, self.scripts, self.embedded, self.title = {}, [], [], ""
        self.storefront = []
        self.verification_refresh = False
        self.interstitial_frame = False
        self._script = None
        self._script_kind = None
        self._title = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "meta":
            if (attrs.get("http-equiv") or "").strip().lower() == "refresh":
                self.verification_refresh |= _verification_refresh(attrs.get("content"))
            name = (attrs.get("property") or attrs.get("name") or "").lower()
            content = attrs.get("content")
            if name and content and len(self.meta) < 200:
                self.meta.setdefault(name, content[:10_000])
        elif tag == "iframe":
            source = attrs.get("src") or ""
            if len(source) <= 10_000:
                try:
                    self.interstitial_frame |= urlsplit(source).path == "/interstitial/ic.html"
                except ValueError:
                    pass
        elif tag == "title":
            self._title = True
        elif tag == "script" and len(self.scripts) + len(self.embedded) < 40:
            mime = (attrs.get("type") or "").split(";", 1)[0].strip().lower()
            if mime == "application/ld+json" or (
                mime == "application/json" and attrs.get("id") == "__NEXT_DATA__"
            ):
                self._script = []
                self._script_kind = mime
            elif (
                not attrs.get("src")
                and mime in {"", "text/javascript", "application/javascript"}
                and len(self.storefront) < 4
            ):
                self._script = []
                self._script_kind = "storefront"

    def handle_data(self, data):
        if self._script is not None:
            self._script.append(data)
        if self._title:
            self.title = (self.title + data)[:1000]

    def handle_endtag(self, tag):
        if tag == "script" and self._script is not None:
            body = "".join(self._script)
            if self._script_kind == "storefront":
                if len(body) <= 131_072 and (
                    "window.mainConfig" in body or re.search(r"app\.value\(['\"]product['\"]", body)
                ):
                    self.storefront.append(body)
            else:
                target = self.scripts if self._script_kind == "application/ld+json" else self.embedded
                target.append(body)
            self._script = None
        if tag == "title":
            self._title = False


def _types(node):
    value = node.get("@type", []) if isinstance(node, dict) else []
    return (
        {
            part.rstrip("/").rsplit("/", 1)[-1]
            for part in ([value] if isinstance(value, str) else value)
            if isinstance(part, str)
        }
        if isinstance(value, (str, list))
        else set()
    )


def _text(value, limit=500):
    if not isinstance(value, str):
        return None
    clean = " ".join(unescape(re.sub(r"<[^>]*>", " ", value)).split())[:limit]
    return clean or None


def _same_page(value, page_url):
    if not isinstance(value, str):
        return False
    try:
        # HTTPX canonicalizes Unicode/percent-encoded paths consistently. Keep
        # the query: different variant queries must not be silently combined.
        return httpx.URL(_safe_url(urljoin(page_url, value))) == httpx.URL(_safe_url(page_url))
    except ProductFetchError:
        return False


def _group_product(group, page_url):
    """Select only variants explicitly bound to the fetched product URL.

    A ProductGroup may list many colors. Multiple size variants at one URL
    can be combined only when their product code, color, brand and image
    agree; the group itself is not sufficient single-product evidence.
    """
    variants = group.get("hasVariant", [])
    variants = variants if isinstance(variants, list) else [variants]
    if len(variants) > 100:
        raise ProductFetchError("Product variant data exceeds the verifiable range.", "not_product")
    matches = []
    for variant in variants:
        if not isinstance(variant, dict) or "Product" not in _types(variant):
            continue
        offers = variant.get("offers", [])
        offers = offers if isinstance(offers, list) else [offers]
        urls = [variant.get("url"), variant.get("@id")]
        urls.extend(offer.get("url") for offer in offers[:20] if isinstance(offer, dict))
        if any(_same_page(value, page_url) for value in urls):
            inherited = {key: group[key] for key in ("name", "description", "brand") if key in group}
            matches.append({**inherited, **variant})
    if not matches:
        return None
    if len(matches) == 1:
        return matches[0]
    signatures = {
        json.dumps(
            [
                item.get("mpn") or item.get("productID") or item.get("sku"),
                item.get("color"),
                item.get("brand"),
                item.get("image"),
            ],
            sort_keys=True,
        )
        for item in matches
    }
    if len(signatures) != 1 or not (
        matches[0].get("mpn") or matches[0].get("productID") or matches[0].get("sku")
    ):
        raise ProductFetchError(
            "Product variants at the same URL are inconsistent; cannot determine the primary product.",
            "not_product",
        )
    combined = dict(matches[0])
    if group.get("name"):
        combined["name"] = group["name"]
    combined["offers"] = []
    for item in matches:
        offers = item.get("offers", [])
        combined["offers"].extend(offers if isinstance(offers, list) else [offers])
    return combined


def _primary_product(documents, page_url):
    nodes, count = [], 0

    def roots(value, depth=0):
        nonlocal count
        count += 1
        if count > 300 or depth > 12:
            return
        if isinstance(value, list):
            for child in value[:100]:
                roots(child, depth + 1)
        elif isinstance(value, dict):
            nodes.append(value)
            roots(value.get("@graph", []), depth + 1)
            entity = value.get("mainEntity")
            if isinstance(entity, dict):
                roots(entity, depth + 1)

    for value in documents:
        roots(value)
    blocked = {"CollectionPage", "SearchResultsPage", "ItemList", "Article", "NewsArticle", "BlogPosting"}
    if any(_types(node) & blocked for node in nodes):
        raise ProductFetchError(
            "This page is a search, collection, or article and cannot serve as single-product evidence.",
            "not_product",
        )
    products = [node for node in nodes if "Product" in _types(node)]
    groups = [node for node in nodes if "ProductGroup" in _types(node)]
    for group in groups:
        selected = _group_product(group, page_url)
        if selected is not None:
            products.append(selected)
    if groups and not products:
        raise ProductFetchError("Product group has no single variant matching this URL.", "not_product")
    if len(products) <= 1:
        return products[0] if products else None
    explicit = set()
    for node in nodes:
        entity = node.get("mainEntity")
        if isinstance(entity, str):
            explicit.add(urljoin(page_url, entity))
        elif isinstance(entity, dict) and isinstance(entity.get("@id"), str):
            explicit.add(urljoin(page_url, entity["@id"]))
    primary = [
        product
        for product in products
        if isinstance(product.get("@id"), str) and urljoin(page_url, product["@id"]) in explicit
    ]
    if len(primary) == 1:
        return primary[0]
    same_page = []
    for product in products:
        product_url = product.get("url")
        if isinstance(product_url, str):
            try:
                if _same_page(product_url, page_url):
                    same_page.append(product)
            except ProductFetchError:
                pass
    if len(same_page) == 1:
        return same_page[0]
    raise ProductFetchError(
        "This page contains multiple products; cannot determine a single primary product.", "not_product"
    )


def _availability(product, meta):
    offers = product.get("offers", []) if product else []
    offers = offers if isinstance(offers, list) else [offers]
    values = [offer.get("availability") for offer in offers[:20] if isinstance(offer, dict)]
    if not values:
        values = [meta.get("product:availability") or meta.get("og:availability")]
    converted = []
    for value in values:
        key = str(value or "").rstrip("/").rsplit("/", 1)[-1].replace("_", "").replace(" ", "").lower()
        converted.append(
            "in_stock"
            if key == "instock"
            else "out_of_stock"
            if key in {"outofstock", "soldout", "discontinued"}
            else "unknown"
        )
    return converted[0] if converted and len(set(converted)) == 1 else "unknown"


def _selected_unavailable(embedded, product, page_url):
    """An exact selected-product state may conservatively veto availability.

    Embedded application data never establishes a product on its own. Require
    both the selected URL and code to match the schema product, and ignore all
    recommendation/colorway arrays. Never infer availability from absent flags.
    """
    code = _text(product.get("mpn") or product.get("productID") or product.get("sku"), 100)
    if not code:
        return False
    for script in embedded:
        if len(script) > 1_000_000:
            continue
        try:
            data = json.loads(script)
            selected = data["props"]["pageProps"]["selectedProduct"]
        except (ValueError, TypeError, KeyError, RecursionError):
            continue
        if not isinstance(selected, dict):
            continue
        selected_code = _text(
            selected.get("mpn")
            or selected.get("styleColor")
            or selected.get("productID")
            or selected.get("sku"),
            100,
        )
        selected_url = selected.get("pdpUrl") or selected.get("url")
        if isinstance(selected_url, dict):
            selected_url = selected_url.get("url")
        if (
            selected_code
            and selected_code.casefold() == code.casefold()
            and _same_page(selected_url, page_url)
        ):
            state = selected.get("statusModifier")
            if isinstance(state, str) and state in {"OUT_OF_STOCK", "SOLD_OUT", "DISCONTINUED"}:
                return True
    return False


def _json_literal_object(literal):
    """Decode a bounded JSON.parse string literal; never evaluate JavaScript."""
    if len(literal) > 131_072:
        return None
    try:
        if literal.startswith("'"):
            raw, converted, index = literal[1:-1], [], 0
            while index < len(raw):
                if raw[index] == "\\":
                    token = raw[index : index + 2]
                    converted.append("'" if token == "\\'" else token)
                    index += 2
                else:
                    converted.append('\\"' if raw[index] == '"' else raw[index])
                    index += 1
            literal = '"' + "".join(converted) + '"'
        value = json.loads(json.loads(literal))
        return value if isinstance(value, dict) else None
    except (ValueError, TypeError, RecursionError):
        return None


def _shopline_tw_evidence(scripts, product, page_url):
    """An English storefront may still explicitly identify a Taiwan merchant.

    Require the current route, schema Product, embedded product and merchant to
    agree. Currency, request geolocation and unrelated products cannot establish
    this evidence. This identifies the storefront's claim, not delivery support.
    """
    literal = r"""(?P<json>"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')"""
    config_pattern = re.compile(r"(?:^|\n)\s*window\.mainConfig\s*=\s*JSON\.parse\(\s*" + literal + r"\s*\)")
    product_pattern = re.compile(
        r"(?:^|\n)\s*app\.value\(\s*(['\"])product\1\s*,\s*JSON\.parse\(\s*" + literal + r"\s*\)"
    )
    configs, products = [], []
    for script in scripts[:4]:
        if len(script) > 131_072:
            continue
        for pattern, target in ((config_pattern, configs), (product_pattern, products)):
            for match in list(pattern.finditer(script))[:2]:
                target.append(_json_literal_object(match["json"]))
    if len(configs) != 1 or len(products) != 1 or not configs[0] or not products[0]:
        return None
    config, embedded = configs[0], products[0]
    merchant = config.get("merchantData")
    if not isinstance(merchant, dict):
        return None
    merchant_id, current_path = config.get("merchantId"), config.get("currentPath")
    if (
        config.get("appCoreHost") != "shoplineapp.com"
        or config.get("pageType") != "products-show"
        or not isinstance(merchant_id, str)
        or not 1 <= len(merchant_id) <= 100
        or merchant_id != merchant.get("_id")
        or merchant_id != embedded.get("owner_id")
        or not isinstance(current_path, str)
        or not current_path.startswith("/")
        or current_path.startswith("//")
        or not _same_page(current_path, page_url)
        or not isinstance(product.get("sku"), str)
        or product["sku"] != embedded.get("_id")
        or merchant.get("base_country_code") != "TW"
    ):
        return None
    try:
        home = _safe_url(merchant.get("brand_home_url"))
    except ProductFetchError:
        return None
    if urlsplit(home).hostname != urlsplit(page_url).hostname:
        return None
    currency = merchant.get("base_currency")
    offers = product.get("offers", [])
    offers = offers if isinstance(offers, list) else [offers]
    if (
        not isinstance(currency, dict)
        or currency.get("iso_code") != "TWD"
        or merchant.get("base_currency_code", "TWD") != "TWD"
        or not 1 <= len(offers) <= 20
        or any(not isinstance(offer, dict) or offer.get("priceCurrency") != "TWD" for offer in offers)
    ):
        return None
    return "This product page's merchant data explicitly states its location as Taiwan and the product is priced in TWD; merchant, product, and URL correspondence verified, but shipping and checkout conditions are not yet confirmed."


def _market(page_url, market, meta):
    if market == "global":
        return "in_scope", "Query is not region-restricted; this page does not confirm delivery coverage."
    parsed = urlsplit(page_url)
    segments = [segment.lower() for segment in unquote(parsed.path).split("/") if segment]
    first = segments[0] if segments else ""
    locale = (meta.get("og:locale") or "").lower().replace("_", "-")
    foreign_codes = {
        "us",
        "gb",
        "uk",
        "jp",
        "cn",
        "hk",
        "mo",
        "sg",
        "au",
        "ca",
        "de",
        "fr",
        "it",
        "es",
        "kr",
        "my",
        "th",
        "vn",
        "id",
        "ph",
        "in",
        "nz",
        "eu",
        # Explicit storefront locales beyond the most common initial markets.
        "at",
        "be",
        "ch",
        "cz",
        "dk",
        "fi",
        "gr",
        "hu",
        "ie",
        "lu",
        "nl",
        "no",
        "pl",
        "pt",
        "ro",
        "se",
        "sk",
        "tr",
        "br",
        "mx",
        "za",
        "il",
        "ae",
        "sa",
        "om",
    }
    region = first
    if first in {"en", "zh", "zh-hant", "shop", "store", "country"} and len(segments) > 1:
        region = segments[1]
    country = region.rsplit("-", 1)[-1]
    locale_country = locale.rsplit("-", 1)[-1] if "-" in locale else ""
    domain_country = parsed.hostname.rsplit(".", 1)[-1]
    if country in foreign_codes or locale_country in foreign_codes or domain_country in foreign_codes:
        return (
            "out_of_scope",
            "The actual product page URL or primary locale clearly belongs to another region; Taiwan purchase channel not yet confirmed.",
        )
    if domain_country == "tw" or country == "tw" or locale_country == "tw":
        return (
            "in_scope",
            "The actual product page URL or primary locale clearly indicates Taiwan; shipping and checkout conditions still need independent confirmation.",
        )
    return (
        "unknown",
        "Product page has no clear Taiwan URL or locale evidence; cannot confirm applicability to Taiwan.",
    )


def product_market_hint(url: str, market: str = "TW") -> str:
    """Rank search URLs only; final redirects and page locale still need checking."""
    return _market(_safe_url(url), market, {})[0]


def _parse_page(body, content_type, page_url, market):
    charset = re.search(r"charset=([\w-]+)", content_type, re.I)
    try:
        html = body.decode(charset.group(1) if charset else "utf-8", errors="replace")
    except LookupError:
        html = body.decode("utf-8", errors="replace")
    parser = _Metadata()
    parser.feed(html)
    challenge = (parser.title + " " + html[:200_000]).lower()
    if (
        any(
            marker in challenge
            for marker in (
                "cf-chl-",
                "verify you are human",
                "checking your browser",
                "enable javascript and cookies to continue",
                "captcha challenge",
                "attention required! | cloudflare",
            )
        )
        or parser.title.strip().lower() in {"just a moment...", "access denied", "robot or human?"}
        or (parser.verification_refresh and parser.interstitial_frame)
    ):
        raise ProductFetchError(
            "Product source encountered a verification or access restriction; fetch stopped.", "blocked"
        )
    documents = []
    for script in parser.scripts:
        if len(script) > 1_000_000:
            continue
        try:
            documents.append(json.loads(script))
        except (ValueError, RecursionError):
            continue
    product = _primary_product(documents, page_url)
    segments = [segment.lower() for segment in unquote(urlsplit(page_url).path).split("/") if segment]
    listing = {
        "search",
        "search-results",
        "collections",
        "collection",
        "categories",
        "category",
        "catalog",
        "blog",
        "article",
        "articles",
        "news",
    }
    product_segments = {"product", "products", "p", "t", "item", "items", "dp", "pd"}
    last_listing = max((index for index, value in enumerate(segments) if value in listing), default=-1)
    if last_listing >= 0 and not any(value in product_segments for value in segments[last_listing + 1 :]):
        raise ProductFetchError(
            "This URL belongs to a search, collection, or article; cannot confirm a single product.",
            "not_product",
        )
    meta = parser.meta
    if product is None and meta.get("og:type", "").lower() not in {"product", "product.item"}:
        raise ProductFetchError("This page has no verifiable single-product data.", "not_product")
    product = product or {}
    title = _text(product.get("name") or meta.get("og:title") or parser.title)
    if not title:
        raise ProductFetchError("This product page has no verifiable product name.", "not_product")
    images = product.get("image") or meta.get("og:image:secure_url") or meta.get("og:image")
    images = images if isinstance(images, list) else [images]
    image_url = None
    for candidate in images[:5]:
        if isinstance(candidate, dict):
            candidate = candidate.get("contentUrl") or candidate.get("url")
        if isinstance(candidate, str) and candidate.strip():
            try:
                proposed = _safe_url(urljoin(page_url, candidate.strip()))
                if not urlsplit(proposed).path.lower().endswith(".svg"):
                    image_url = proposed
                    break
            except ProductFetchError:
                continue
    availability = (
        "out_of_stock"
        if _selected_unavailable(parser.embedded, product, page_url)
        else _availability(product, meta)
    )
    if image_url is None:
        if availability == "out_of_stock":
            raise ProductFetchError(
                "Product page explicitly indicates sold out or discontinued.", "out_of_stock"
            )
        raise ProductFetchError("This product page has no credible product main image.", "missing_image")
    brand = product.get("brand")
    if isinstance(brand, dict):
        brand = brand.get("name")
    scope, evidence = _market(page_url, market, meta)
    if market == "TW" and scope == "unknown":
        merchant_evidence = _shopline_tw_evidence(parser.storefront, product, page_url)
        if merchant_evidence:
            scope, evidence = "in_scope", merchant_evidence
    price_amount = None
    price_currency = None
    offers = product.get("offers", [])
    offers = offers if isinstance(offers, list) else [offers]
    for offer in offers[:20]:
        if not isinstance(offer, dict):
            continue
        raw = offer.get("price")
        cur = offer.get("priceCurrency")
        if raw is not None and cur:
            try:
                price_amount = float(raw)
                price_currency = str(cur).upper()[:3]
                break
            except (ValueError, TypeError):
                continue
    if price_amount is None:
        raw = meta.get("product:price:amount") or meta.get("og:price:amount")
        cur = meta.get("product:price:currency") or meta.get("og:price:currency")
        if raw and cur:
            try:
                price_amount = float(raw)
                price_currency = str(cur).upper()[:3]
            except (ValueError, TypeError):
                pass
    return {
        "page_url": page_url,
        "title": title,
        "description": _text(
            product.get("description") or meta.get("og:description") or meta.get("description"), 2000
        )
        or "",
        "brand": _text(brand or meta.get("product:brand"), 200),
        "product_code": _text(
            product.get("mpn")
            or product.get("sku")
            or product.get("productID")
            or meta.get("product:retailer_item_id"),
            100,
        ),
        "color": _text(product.get("color") or meta.get("product:color"), 200),
        "availability": availability,
        "market": scope,
        "market_evidence": evidence,
        "image_url": image_url,
        "price_amount": price_amount,
        "price_currency": price_currency,
    }


def _image_mime(data):
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                if image.format not in _MIMES or getattr(image, "n_frames", 1) != 1:
                    raise ProductFetchError(
                        "Product main image is not a supported single JPG, PNG, or WebP.", "invalid_image"
                    )
                if image.width < 32 or image.height < 32 or image.width * image.height > MAX_IMAGE_PIXELS:
                    raise ProductFetchError(
                        "Product main image dimensions are too small or exceed the safe limit.",
                        "invalid_image",
                    )
                mime = _MIMES[image.format]
                image.verify()
            with Image.open(io.BytesIO(data)) as image:
                image.load()
        return mime
    except ProductFetchError:
        raise
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ProductFetchError(
            "Product main image content could not be verified.", "invalid_image"
        ) from None


async def _fetch_product(url, market="TW", *, resolver=None, transport=None):
    """Private injection points keep network/security tests entirely offline."""
    if market not in {"TW", "global"}:
        raise ProductFetchError("Unsupported product query region.", "unsupported_market")
    try:
        async with asyncio.timeout(TOTAL_TIMEOUT):
            async with httpx.AsyncClient(
                transport=transport,
                verify=True,
                trust_env=False,
                follow_redirects=False,
                timeout=httpx.Timeout(8.0, connect=5.0),
                limits=httpx.Limits(max_connections=1, max_keepalive_connections=0),
            ) as client:
                final_url, html, content_type = await _read(
                    client, url, kind="html", resolver=resolver or _resolve
                )
                result = await asyncio.to_thread(_parse_page, html, content_type, final_url, market)
                image_url, data, _ = await _read(
                    client, result["image_url"], kind="image", resolver=resolver or _resolve
                )
                mime = await asyncio.to_thread(_image_mime, data)
                return {
                    **result,
                    "image_url": image_url,
                    "image_bytes": data,
                    "image_mime": mime,
                    "checked_at": datetime.now(timezone.utc).isoformat(),
                }
    except ProductFetchError:
        raise
    except (TimeoutError, httpx.TimeoutException):
        raise ProductFetchError("Product source fetch timed out; stopped retrying.", "timeout") from None
    except (httpx.HTTPError, OSError, ValueError, RecursionError):
        raise ProductFetchError("Product source is currently not safely readable.", "unavailable") from None


async def fetch_product(url: str, market: str = "TW") -> dict:
    return await _fetch_product(url, market)

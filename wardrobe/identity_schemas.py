"""Bounded brand observations and source-backed shopping candidates.

These records are separate from the observed garment catalogue. A search result
is a candidate, never proof of ownership, authenticity, or the original seller.
"""

from __future__ import annotations

import ipaddress
import re
from datetime import datetime
from typing import Annotated, Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


ShortText = Annotated[str, Field(min_length=1, max_length=200)]
EvidenceText = Annotated[str, Field(min_length=1, max_length=700)]
Market = Literal["TW", "global"]
SHOPPING_NOTICE = (
    "Candidates come from actual search results and have not been individually verified; "
    "they are not confirmed to be the same item, authentic, or from the original store, "
    "and price, stock, and shipping coverage have not been checked."
)
VERIFIED_SHOPPING_NOTICE = (
    "Only candidates whose original and product images matched and whose product page region is compatible are listed; "
    "this is still not proof of being the same item, authentic, or from the original store. "
    "Stock and product pages may change — please verify before purchasing."
)


def normalize_source_url(value: str) -> str:
    """Validate display links without fetching pages or resolving remote hosts.

    Only HTTPS links are emitted. No server-side request is made to these URLs;
    network access to a provider-returned URL would additionally need DNS and
    redirect validation, as in product_evidence.fetch_product.
    """
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise ValueError("Invalid source URL")
    if any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("Invalid source URL")
    if "\\" in value:
        raise ValueError("Invalid source URL")
    try:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or parsed.username is not None or parsed.password is not None:
            raise ValueError("Invalid source URL")
        if parsed.port not in (None, 443) or not parsed.hostname:
            raise ValueError("Invalid source URL")
        host = parsed.hostname.rstrip(".").encode("idna").decode("ascii").lower()
        if not host or "%" in host:
            raise ValueError("Invalid source URL")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            labels = host.split(".")
            if (
                len(labels) < 2
                or labels[-1]
                in {"localhost", "local", "internal", "lan", "home", "test", "invalid", "example"}
                or "localhost" in labels
                or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels)
                or all(re.fullmatch(r"(?:[0-9]+|0x[0-9a-f]+)", label) for label in labels)
            ):
                raise ValueError("Invalid source URL") from None
        else:
            if not address.is_global:
                raise ValueError("Invalid source URL")
            if address.version == 6:
                host = f"[{host}]"
        return urlunsplit(("https", host, parsed.path or "/", parsed.query, ""))
    except (UnicodeError, ValueError):
        raise ValueError("Invalid source URL") from None


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)


class VisualProfile(StrictModel):
    type: Annotated[str, Field(min_length=1, max_length=100)] | None
    category: (
        Literal["top", "bottom", "dress", "outerwear", "onesie", "sleepwear", "shoes", "accessory", "other"]
        | None
    )
    primary_color: Annotated[str, Field(min_length=1, max_length=60)] | None
    pattern: (
        Literal["solid", "striped", "checked", "floral", "polka_dot", "graphic", "color_block", "other"]
        | None
    )
    sleeve_length: Literal["sleeveless", "short", "three_quarter", "long", "not_applicable"] | None
    neckline: (
        Literal[
            "crew",
            "v_neck",
            "scoop",
            "collared",
            "hooded",
            "turtleneck",
            "strapless",
            "other",
            "not_applicable",
        ]
        | None
    )
    description: Annotated[str, Field(min_length=1, max_length=500)] | None


class BrandIdentity(StrictModel):
    status: Literal["observed", "possible", "unknown"]
    brand: ShortText | None
    evidence_type: Literal["label", "wordmark", "logo", "none"]
    visible_text: Annotated[str, Field(min_length=1, max_length=500)] | None
    evidence: EvidenceText
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    product_code: Annotated[str, Field(min_length=1, max_length=100)] | None
    search_terms: list[Annotated[str, Field(min_length=1, max_length=120)]] = Field(max_length=3)
    # Old persisted observations remain readable; new model responses must
    # explicitly provide this field (see IDENTITY_SCHEMA below).
    visual_profile: VisualProfile | None = None

    @model_validator(mode="after")
    def require_visual_evidence(self):
        if self.status == "unknown":
            self.brand = self.product_code = self.visible_text = None
            self.confidence = 0.0
            self.evidence_type = "none"
        elif not self.brand or self.evidence_type == "none":
            raise ValueError("A brand observation requires a name and visible brand evidence")
        elif self.evidence_type in ("label", "wordmark") and not self.visible_text:
            raise ValueError("Readable brand evidence requires its visible text")
        elif self.status == "observed" and self.evidence_type in ("label", "wordmark"):
            name = re.sub(r"\W", "", self.brand).casefold()
            text = re.sub(r"\W", "", self.visible_text).casefold()
            if not name or name not in text:
                # An abbreviation or incomplete wordmark can suggest a brand,
                # but it is not a direct reading of the supplied full name.
                self.status = "possible"
        # A product code must actually be transcribed, not recalled from a
        # garment's visual resemblance or supplied catalogue metadata.
        if self.product_code is not None:
            if not _text_mentions_code(self.visible_text or "", self.product_code):
                self.product_code = None
        self.search_terms = list(dict.fromkeys(self.search_terms))
        return self


class ShoppingOffer(StrictModel):
    title: Annotated[str, Field(min_length=1, max_length=500)]
    url: Annotated[str, Field(min_length=1, max_length=2048)]
    merchant: ShortText
    match: Literal["model_candidate", "similar"]
    reason: EvidenceText

    _valid_url = field_validator("url")(normalize_source_url)


ComparisonStatus = Literal["match", "mismatch", "uncertain", "not_applicable"]


class VisualComparison(StrictModel):
    category: ComparisonStatus
    primary_color: ComparisonStatus
    pattern: ComparisonStatus
    sleeve_length: ComparisonStatus
    neckline: ComparisonStatus
    design_details: ComparisonStatus


class ComparisonAnswer(StrictModel):
    comparison: VisualComparison
    reasons: list[EvidenceText] = Field(min_length=1, max_length=6)


def _timestamp(value: str) -> str:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("Time requires a timezone")
    return value


def _local_product_image(value: str) -> str:
    if not re.fullmatch(
        r"(?:/media/[a-zA-Z0-9_-]{1,64}/shopping/[a-zA-Z0-9_-]{1,64}/)?[1-3]\.(?:jpg|png)", value
    ):
        raise ValueError("Verification image must be a local product image")
    return value


class OfferVerification(StrictModel):
    status: Literal["verified"]
    page_url: Annotated[str, Field(min_length=1, max_length=2048)]
    page_checked_at: Annotated[str, Field(min_length=1, max_length=40)]
    market: Literal["in_scope"]
    market_evidence: EvidenceText
    availability: Literal["in_stock", "unknown"]
    comparison: VisualComparison
    image_url: Annotated[str, Field(min_length=1, max_length=250)]
    product_code: Annotated[str, Field(min_length=1, max_length=100)] | None
    price_amount: float | None = None
    price_currency: Annotated[str, Field(min_length=1, max_length=3)] | None = None

    _valid_url = field_validator("page_url")(normalize_source_url)
    _valid_time = field_validator("page_checked_at")(_timestamp)
    _valid_image = field_validator("image_url")(_local_product_image)


class VerifiedShoppingOffer(ShoppingOffer):
    verification: OfferVerification


class CheckedCandidate(StrictModel):
    title: Annotated[str, Field(min_length=1, max_length=500)]
    url: Annotated[str, Field(min_length=1, max_length=2048)]
    status: Literal["accepted", "rejected", "unverified"]
    reasons: list[EvidenceText] = Field(min_length=1, max_length=8)

    _valid_url = field_validator("url")(normalize_source_url)


class ShoppingSource(StrictModel):
    title: Annotated[str, Field(min_length=1, max_length=500)]
    url: Annotated[str, Field(min_length=1, max_length=2048)]
    snippet: Annotated[str, Field(max_length=1000)]

    _valid_url = field_validator("url")(normalize_source_url)


class ShoppingAnswer(StrictModel):
    """The model proposes offers; only the adapter can supply source evidence."""

    offers: list[ShoppingOffer] = Field(max_length=5)


class ShoppingSearchAttempt(StrictModel):
    kind: Literal["text", "image"] = "text"
    query: Annotated[str, Field(min_length=1, max_length=500)]
    status: Literal["searched", "no_results", "unavailable"]
    retrieved_count: int = Field(ge=0, le=10000)
    candidate_count: int = Field(ge=0, le=5)
    error: EvidenceText | None = None


class ShoppingResult(StrictModel):
    version: Literal[1, 2] = 1
    status: Literal["searched", "no_results", "unavailable"]
    query: Annotated[str, Field(min_length=1, max_length=500)]
    offers: list[ShoppingOffer | VerifiedShoppingOffer] = Field(max_length=5)
    sources: list[ShoppingSource] = Field(max_length=10)
    searched_at: Annotated[str, Field(min_length=1, max_length=40)]
    notice: Annotated[str, Field(min_length=1, max_length=700)]
    checked_candidates: list[CheckedCandidate] = Field(default_factory=list, max_length=5)
    search_attempts: list[ShoppingSearchAttempt] = Field(default_factory=list, max_length=3)

    _valid_time = field_validator("searched_at")(_timestamp)


def normalize_identity(value) -> dict:
    return BrandIdentity.model_validate(value).model_dump()


def same_product_code(original: str | None, candidate: str | None) -> bool:
    """Only the entire transcribed SKU may establish a model candidate."""
    if not original or not candidate:
        return False
    if any(character.isalnum() and not character.isascii() for character in original + candidate):
        return False
    first = re.sub(r"[^a-z0-9]", "", original.casefold())
    second = re.sub(r"[^a-z0-9]", "", candidate.casefold())
    return bool(first) and first == second


def comparison_failures(comparison: dict, profile: dict | None) -> list[str]:
    """The adapter, never a model's overall boolean, decides acceptance."""
    parsed = VisualComparison.model_validate(comparison).model_dump()
    names = {
        "category": "garment type",
        "primary_color": "primary color",
        "pattern": "pattern",
        "sleeve_length": "sleeve length",
        "neckline": "neckline",
        "design_details": "design details",
    }
    if not profile or profile.get("category") in (None, "other") or not profile.get("primary_color"):
        return [
            "The garment type or primary color in the original photo cannot be confirmed; unable to verify similar products."
        ]
    failures = []
    for key, status in parsed.items():
        source_value = profile.get("description" if key == "design_details" else key)
        required = key in ("category", "primary_color") or source_value not in (None, "not_applicable")
        if status == "mismatch":
            failures.append(f"{names[key]} does not match the original garment.")
        elif status == "uncertain" or (required and status != "match"):
            failures.append(f"Unable to confirm {names[key]} matches the original garment.")
    return failures


def _text_mentions_code(text: str, code: str | None) -> bool:
    if not code or any(character.isalnum() and not character.isascii() for character in code):
        return False
    normalized = re.sub(r"[^a-z0-9]", "", code.casefold())
    if not normalized:
        return False
    # Compare complete product-code tokens, allowing case and punctuation
    # differences but not A1234, XA123, or A123-BLUE as evidence for A123.
    tokens = re.findall(r"[a-z0-9]+(?:[-._][a-z0-9]+)*", text, re.I)
    return any(re.sub(r"[^a-z0-9]", "", token.casefold()) == normalized for token in tokens)


def _source_mentions_code(source: ShoppingSource, code: str | None) -> bool:
    return _text_mentions_code(source.title + " " + source.snippet, code)


def normalize_shopping(value, identity) -> dict:
    identity = normalize_identity(identity)
    result = ShoppingResult.model_validate(value)
    if result.version == 2:
        return _normalize_verified(result, identity)
    if result.status == "unavailable":
        result.offers = []
        result.sources = []
    else:
        sources = {source.url: source for source in result.sources}
        result.sources = list(sources.values())
        offers = {}
        for offer in result.offers:
            source = sources.get(offer.url)
            if source is None:
                continue
            # Titles and merchant names originate in source metadata, not in
            # an unsupported claim that a seller is the official manufacturer.
            offer.title = source.title
            offer.merchant = urlsplit(offer.url).hostname.removeprefix("www.")[:200]
            if identity["status"] != "observed" or not _source_mentions_code(
                source, identity["product_code"]
            ):
                offer.match = "similar"
            if offer.match == "model_candidate":
                offer.reason = (
                    f"Search title or snippet contains the readable product code '{identity['product_code']}' from the original image; "
                    "listed as a product-code candidate only — product page and item match have not been verified."
                )
            else:
                offer.reason = "Related garment candidate from actual search results; compared by title or snippet only — product page has not been verified."
            offers.setdefault(offer.url, offer)
        result.offers = list(offers.values())
        result.status = "searched" if result.offers else "no_results"
    result.notice = SHOPPING_NOTICE
    return result.model_dump()


def _normalize_verified(result: ShoppingResult, identity: dict) -> dict:
    sources = {source.url: source for source in result.sources}
    checked = {candidate.url: candidate for candidate in result.checked_candidates}
    if len(checked) != len(result.checked_candidates):
        raise ValueError("Duplicate checked candidates")
    offers = {}
    for offer in result.offers:
        if not isinstance(offer, VerifiedShoppingOffer):
            raise ValueError("Version 2 offers require product-image verification")
        if offer.url not in sources or offer.url not in checked or checked[offer.url].status != "accepted":
            raise ValueError("Verified offers require a retrieved and checked candidate")
        verification = offer.verification
        if comparison_failures(verification.comparison.model_dump(), identity["visual_profile"]):
            raise ValueError("Visual evidence does not support a similar product")
        if identity["status"] != "observed" or not same_product_code(
            identity["product_code"], verification.product_code
        ):
            offer.match = "similar"
        # These claims describe a fetched page and an image comparison. Keep
        # their concrete reason instead of the legacy search-only template.
        offer.merchant = urlsplit(verification.page_url).hostname.removeprefix("www.")[:200]
        offers.setdefault(offer.url, offer)
    if any(candidate.status == "accepted" and candidate.url not in offers for candidate in checked.values()):
        raise ValueError("Accepted candidates require a verified offer")
    if result.status == "unavailable" and offers:
        raise ValueError("Unavailable search cannot contain verified offers")
    result.offers = list(offers.values())
    result.sources = list(sources.values())
    result.status = (
        "searched" if offers else ("unavailable" if result.status == "unavailable" else "no_results")
    )
    result.notice = VERIFIED_SHOPPING_NOTICE
    return result.model_dump()


IDENTITY_SCHEMA = BrandIdentity.model_json_schema()
# Backward compatibility applies to stored data, not new model observations.
IDENTITY_SCHEMA["required"].append("visual_profile")
IDENTITY_SCHEMA["properties"]["visual_profile"].pop("default", None)
SHOPPING_ANSWER_SCHEMA = ShoppingAnswer.model_json_schema()
COMPARISON_SCHEMA = ComparisonAnswer.model_json_schema()

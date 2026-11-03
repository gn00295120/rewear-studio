"""Privacy-safe Model API metering and standard-price estimates.

Only counters and operation labels are retained. Prompts, images, responses,
credentials and provider exception bodies never enter the usage ledger.
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4


PRICING_VERSION = "meta-standard-2026-10-22"
PRICES_USD = {
    "spark_input_token": 1.25 / 1_000_000,
    "spark_cached_input_token": 0.15 / 1_000_000,
    "spark_output_token": 4.25 / 1_000_000,
    "web_search_query": 2.50 / 1_000,
    "generated_image": 0.01,
    "sam_image": 2.50 / 1_000,
    "sam_video_frame": 0.20 / 1_000,
}

_SCOPE: ContextVar[dict[str, str]] = ContextVar("rewear_model_usage_scope", default={})
_COUNT_KEYS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "web_search_queries",
    "generated_images",
    "sam_images",
    "sam_video_frames",
)


def begin_usage_scope(*, entity_id: str, feature: str, job_id: str | None = None) -> Token:
    value = {"entity_id": entity_id, "feature": feature}
    if job_id:
        value["job_id"] = job_id
    return _SCOPE.set(value)


def end_usage_scope(token: Token) -> None:
    _SCOPE.reset(token)


def _get(value: Any, name: str, default=None):
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _nonnegative_int(value: Any) -> int:
    return value if type(value) is int and value >= 0 else 0


def token_counts(response: Any) -> tuple[dict[str, int], bool]:
    """Read both Chat Completions and Responses API usage shapes."""
    usage = _get(response, "usage")
    if usage is None:
        return {key: 0 for key in _COUNT_KEYS[:3]}, False
    input_tokens = _nonnegative_int(_get(usage, "input_tokens", _get(usage, "prompt_tokens", 0)))
    output_tokens = _nonnegative_int(_get(usage, "output_tokens", _get(usage, "completion_tokens", 0)))
    details = _get(usage, "input_tokens_details", _get(usage, "prompt_tokens_details", {})) or {}
    cached = min(input_tokens, _nonnegative_int(_get(details, "cached_tokens", 0)))
    return {
        "input_tokens": input_tokens,
        "cached_input_tokens": cached,
        "output_tokens": output_tokens,
    }, True


def web_search_count(response: Any) -> int:
    output = _get(response, "output", []) or []
    return sum(
        1
        for item in output
        if _get(item, "type") == "web_search_call" and _get(item, "status") == "completed"
    )


def estimate_usd(counts: dict[str, int], *, model: str = "muse-spark-1.3") -> float:
    """Estimate the public Standard price for one normalized usage event.

    Muse Image responses expose internal token counters, but the public API is
    billed per returned image. SAM is likewise billed per image/video frame.
    Only Spark token counters therefore contribute token charges.
    """
    token_billed = model.startswith("muse-spark")
    uncached = max(0, counts["input_tokens"] - counts["cached_input_tokens"]) if token_billed else 0
    cached = counts["cached_input_tokens"] if token_billed else 0
    output = counts["output_tokens"] if token_billed else 0
    value = (
        uncached * PRICES_USD["spark_input_token"]
        + cached * PRICES_USD["spark_cached_input_token"]
        + output * PRICES_USD["spark_output_token"]
        + counts["web_search_queries"] * PRICES_USD["web_search_query"]
        + counts["generated_images"] * PRICES_USD["generated_image"]
        + counts["sam_images"] * PRICES_USD["sam_image"]
        + counts["sam_video_frames"] * PRICES_USD["sam_video_frame"]
    )
    return round(value, 8)


def usage_event(
    response: Any,
    *,
    model: str,
    operation: str,
    generated_images: int = 0,
    sam_images: int = 0,
    sam_video_frames: int = 0,
    web_search_queries: int | None = None,
) -> dict:
    tokens, token_usage_reported = token_counts(response)
    counts = {
        **tokens,
        "web_search_queries": (
            web_search_count(response) if web_search_queries is None else _nonnegative_int(web_search_queries)
        ),
        "generated_images": _nonnegative_int(generated_images),
        "sam_images": _nonnegative_int(sam_images),
        "sam_video_frames": _nonnegative_int(sam_video_frames),
    }
    scope = _SCOPE.get()
    return {
        "id": uuid4().hex,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "entity_id": scope.get("entity_id", "system"),
        "job_id": scope.get("job_id"),
        "feature": scope.get("feature", "unscoped"),
        "model": model,
        "operation": operation,
        **counts,
        "estimated_usd": estimate_usd(counts, model=model),
        "token_usage_reported": token_usage_reported,
        "pricing_version": PRICING_VERSION,
    }


def empty_summary() -> dict:
    return {
        "pricing_version": PRICING_VERSION,
        "currency": "USD",
        "event_count": 0,
        **{key: 0 for key in _COUNT_KEYS},
        "estimated_usd": 0.0,
        "token_usage_complete": True,
        "by_feature": {},
    }


def summarize(events: list[dict]) -> dict:
    result = empty_summary()
    for event in events:
        result["event_count"] += 1
        for key in _COUNT_KEYS:
            result[key] += _nonnegative_int(event.get(key))
        result["estimated_usd"] += max(0.0, float(event.get("estimated_usd", 0)))
        result["token_usage_complete"] = result["token_usage_complete"] and bool(
            event.get("token_usage_reported", False)
            or not str(event.get("model", "")).startswith("muse-spark")
        )
        feature = str(event.get("feature") or "unscoped")[:100]
        bucket = result["by_feature"].setdefault(feature, {"events": 0, "estimated_usd": 0.0})
        bucket["events"] += 1
        bucket["estimated_usd"] += max(0.0, float(event.get("estimated_usd", 0)))
    result["estimated_usd"] = round(result["estimated_usd"], 8)
    for bucket in result["by_feature"].values():
        bucket["estimated_usd"] = round(bucket["estimated_usd"], 8)
    return result

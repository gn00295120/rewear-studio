from types import SimpleNamespace

import pytest

from wardrobe.model_usage import (
    begin_usage_scope,
    end_usage_scope,
    estimate_usd,
    summarize,
    usage_event,
)
from wardrobe.store import Store


def test_standard_price_estimate_handles_cached_tokens_and_fixed_units():
    counts = {
        "input_tokens": 10_000,
        "cached_input_tokens": 2_000,
        "output_tokens": 3_000,
        "web_search_queries": 2,
        "generated_images": 1,
        "sam_images": 1,
        "sam_video_frames": 0,
    }
    assert estimate_usd(counts) == pytest.approx(0.04055)
    assert estimate_usd(counts, model="muse-image-1.0") == pytest.approx(0.0175)


def test_image_usage_prices_returned_images_without_spark_token_charges():
    event = usage_event(
        {"usage": {"input_tokens": 40_000, "output_tokens": 2_000}, "data": [{}, {}, {}]},
        model="muse-image-1.0",
        operation="reconstruction.preview",
        generated_images=3,
        web_search_queries=0,
    )
    assert event["input_tokens"] == 40_000
    assert event["output_tokens"] == 2_000
    assert event["estimated_usd"] == 0.03


def test_usage_event_supports_chat_and_responses_shapes_without_content():
    token = begin_usage_scope(entity_id="item-1", job_id="job-1", feature="reconstruction")
    try:
        chat = SimpleNamespace(
            usage=SimpleNamespace(
                prompt_tokens=120,
                completion_tokens=30,
                prompt_tokens_details=SimpleNamespace(cached_tokens=20),
            )
        )
        event = usage_event(chat, model="muse-spark-1.3", operation="plan")
    finally:
        end_usage_scope(token)
    assert event["entity_id"] == "item-1" and event["job_id"] == "job-1"
    assert event["input_tokens"] == 120 and event["cached_input_tokens"] == 20
    assert event["output_tokens"] == 30 and event["token_usage_reported"] is True
    assert set(event).isdisjoint({"prompt", "image", "response", "api_key"})

    response = {
        "usage": {"input_tokens": 50, "output_tokens": 10},
        "output": [
            {"type": "web_search_call", "status": "completed"},
            {"type": "web_search_call", "status": "failed"},
        ],
    }
    searched = usage_event(response, model="muse-spark-1.3", operation="search")
    assert searched["web_search_queries"] == 1


def test_store_persists_and_filters_usage_in_existing_audit_ledger(tmp_path):
    store = Store(tmp_path)
    first = usage_event(
        {"usage": {"input_tokens": 100, "output_tokens": 20}},
        model="muse-spark-1.3",
        operation="inspect",
    )
    first.update(entity_id="job-a", feature="analysis")
    second = usage_event(
        {},
        model="muse-image-1.0",
        operation="image",
        generated_images=1,
        web_search_queries=0,
    )
    second.update(entity_id="item-b", feature="reconstruction")
    store.record_model_usage(first)
    store.record_model_usage(second)

    total = store.model_usage()
    assert total["event_count"] == 2 and total["generated_images"] == 1
    assert total["by_feature"]["analysis"]["events"] == 1
    assert total["by_feature"]["reconstruction"]["estimated_usd"] == 0.01
    assert store.model_usage(entity_id="job-a")["event_count"] == 1
    assert summarize([])["token_usage_complete"] is True

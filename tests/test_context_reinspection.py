"""Real pipeline/media/store with explicit model fixtures, not accuracy claims."""

import asyncio
from pathlib import Path

import pytest
from PIL import Image

from wardrobe.config import Settings
from wardrobe import media
from wardrobe.pipeline import JobManager
from wardrobe.providers import ProviderError
from wardrobe.schemas import unknown_garment
from wardrobe.store import Store


def answer(category="other", visibility="partial", target="one_garment"):
    return {
        "target": target,
        "visibility": visibility,
        "reason": "Garment obscured by outerwear.",
        "garment": {**unknown_garment(), "category": category, "confidence": 0.7},
    }


class Models:
    def __init__(self, second=None, first=None, count=1):
        self.first = first or answer()
        self.second = second if second is not None else answer("top")
        self.count = count
        self.calls = []

    async def segment(self, path, concept, video=False):
        return [
            {
                "kind": "box",
                "object_id": str(n),
                "frame": None,
                "left": 40,
                "top": 50,
                "right": 60,
                "bottom": 80,
            }
            for n in range(self.count)
        ]

    async def inspect(self, path, provider, *, context_path=None, wearing=False):
        self.calls.append((Path(path), Path(context_path) if context_path else None, wearing))
        if context_path and Path(context_path).name.startswith("expanded-"):
            if isinstance(self.second, BaseException):
                raise self.second
            return self.second
        return self.first


def run(tmp_path, model, *, mode="outfit", selection=False):
    settings = Settings(data_dir=tmp_path / "data", api_key=None)
    store = Store(settings.data_dir)
    job = store.create_job(
        mode=mode, input_name="source.jpg", options={"person_box": [0.2, 0.0, 0.8, 1.0]} if selection else {}
    )
    directory = settings.data_dir / "jobs" / job["id"]
    directory.mkdir(parents=True)
    source = directory / "source.jpg"
    Image.new("RGB", (200, 200), "blue").save(source)
    before = source.read_bytes()
    manager = JobManager(settings, store, model)
    asyncio.run(manager._run(job["id"]))
    assert source.read_bytes() == before
    return store.job(job["id"]), store.items(job["id"]), directory


def test_original_context_can_recover_partial_only_into_confirmation(tmp_path):
    model = Models()
    job, items, directory = run(tmp_path, model, selection=True)
    assert job["status"] == "completed" and len(items) == 1
    item = items[0]
    assert item["record"]["category"] == "top"
    assert item["quality"]["status"] == "needs_confirmation" and not item["reviewed"]
    review = item["quality"]["context_review"]
    assert review["initial"] == answer() and review["expanded"] == answer("top")
    assert review["selected"] == "expanded"
    assert len(model.calls) == 2 and model.calls[0][0] == model.calls[1][0]
    with Image.open(model.calls[0][1]) as tight, Image.open(model.calls[1][1]) as expanded:
        assert tight.size == (28, 42) and expanded.size == (60, 90)
    assert item["source_url"].endswith("/selected.jpg")
    assert item["original_url"].endswith("/preview.jpg")
    assert review["context_url"].endswith(model.calls[1][1].name)
    assert (directory / "selected.jpg").is_file()


@pytest.mark.parametrize(
    "second",
    [
        answer(),
        answer("top", "fragment"),
        answer("top", "complete"),
        answer("top", target="multiple_garments"),
        ProviderError("Model temporarily offline"),
        {"bad": True},
    ],
)
def test_retry_cannot_turn_uncertainty_or_failure_into_ready(tmp_path, second):
    model = Models(second=second)
    job, items, _ = run(tmp_path, model)
    assert job["status"] == "completed" and len(model.calls) == 2
    assert items[0]["quality"]["status"] == "needs_retake"
    assert items[0]["quality"]["context_review"]["selected"] == "initial"
    assert items[0]["model_record"] == answer()["garment"]


@pytest.mark.parametrize(
    "first",
    [
        answer("top"),
        answer(visibility="complete"),
        answer(target="no_garment"),
        answer(target="multiple_garments"),
    ],
)
def test_valid_partials_and_wrong_targets_are_not_retried(tmp_path, first):
    model = Models(first=first)
    _, items, _ = run(tmp_path, model)
    assert len(model.calls) == 1
    assert "context_review" not in items[0]["quality"]


@pytest.mark.parametrize("mode", ["single", "rack"])
def test_retry_is_limited_to_worn_clothing(tmp_path, mode):
    model = Models()
    _, items, _ = run(tmp_path, model, mode=mode)
    assert len(model.calls) == 1 and "context_review" not in items[0]["quality"]


def test_large_photo_has_at_most_three_context_reviews(tmp_path):
    model = Models(count=4)
    _, items, _ = run(tmp_path, model)
    assert len(model.calls) == 7
    assert sum("context_review" in item["quality"] for item in items) == 3


def test_cancel_during_retry_propagates_without_publishing_candidate(tmp_path):
    model = Models(second=asyncio.CancelledError())
    job, items, _ = run(tmp_path, model)
    assert job["status"] == "cancelled" and items == []


@pytest.mark.parametrize("failure", [media.MediaError("bad context"), OSError("private path")])
def test_optional_context_failure_retains_initial_assessment(tmp_path, monkeypatch, failure):
    def unavailable(*args, **kwargs):
        raise failure

    monkeypatch.setattr(media, "context_crop", unavailable)
    model = Models()
    job, items, _ = run(tmp_path, model)
    assert job["status"] == "completed" and len(items) == len(model.calls) == 1
    assert items[0]["quality"]["status"] == "needs_retake"
    review = items[0]["quality"]["context_review"]
    assert review["selected"] == "initial" and review["expanded"] is None
    assert review["error"] == "Expanded context review incomplete; keeping initial result."

"""Named-original wiring and provenance, using simulated model answers only.

These tests establish which actual pixels are sent, not model fidelity or a
same-garment conclusion. Account/group authorization is the manager's boundary.
"""

import asyncio
import base64
import copy
import hashlib
import io
import json

import pytest
from PIL import Image
from pydantic import ValidationError

from wardrobe.providers import ProviderError, _reference_data_url
from wardrobe.reconstruction_quality import (
    ReconstructionPlan,
    ReconstructionRepairTargets,
    assess_fidelity,
    build_report,
    is_accepted,
    validate_report,
)
from wardrobe.reconstruction_sources import (
    ReferenceManifest,
    ReconstructionSourceBundle,
)
from wardrobe.schemas import unknown_garment

from test_reconstruction_quality import (
    chat_response,
    create_provider,
    encoded_image,
    fidelity_answer,
    image_pixels,
    image_response,
    regional_fixture,
    source_plan,
)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def bundle_fixture(tmp_path, extras=2, *, context=True):
    names = ["cutout", *(["context"] if context else []), *[f"extra_{n + 1}" for n in range(extras)]]
    colors = {"cutout": "blue", "context": "red", "extra_1": "green", "extra_2": "yellow"}
    paths, entries = {}, []
    for name in names:
        path = tmp_path / f"{name}.png"
        path.write_bytes(encoded_image(colors[name]))
        paths[name] = path
        number = 1 if name in {"cutout", "context"} else int(name[-1]) + 1
        job_id = f"{number + 10:032x}"
        entries.append(
            {
                "id": name,
                "item_id": f"{number:032x}",
                "job_id": job_id,
                "role": "target_locator" if name == "cutout" else "original_context",
                "media_url": f"/media/{job_id}/{name}.png",
                "source_url": f"/media/{job_id}/source.jpg",
                "original_url": None,
                "file_sha256": digest(path.read_bytes()),
                "reference_sha256": None,
            }
        )
    manifest = ReferenceManifest.model_validate(
        {
            "target_item_id": f"{1:032x}",
            "group_id": "a" * 32 if extras else None,
            "membership_sha256": "b" * 64 if extras else None,
            "primary_source": "context" if context else "cutout",
            "sources": entries,
        }
    )
    return ReconstructionSourceBundle(manifest, paths)


def filled_manifest(bundle):
    value = bundle.manifest.model_dump()
    for source in value["sources"]:
        transmitted = base64.b64decode(_reference_data_url(bundle.paths[source["id"]]).split(",", 1)[1])
        source["reference_sha256"] = digest(transmitted)
    return value


def garment():
    return {**unknown_garment(), "category": "top", "subtype": "shirt", "colors": ["red"]}


def pixel(reference):
    data = base64.b64decode(reference["image_url"].split(",", 1)[1])
    with Image.open(io.BytesIO(data)) as picture:
        return picture.getpixel((0, 0))


def matching_region_checks(checks):
    return [
        {
            **check,
            "status": "pass",
            "candidate_observation": "可見原圖細節相符。",
            "reason": "相符。",
            "correction": None,
        }
        for check in checks
    ]


@pytest.mark.parametrize("extras", [1, 2])
async def test_three_and_four_named_originals_reach_plan_generation_and_global_audit(tmp_path, extras):
    bundle = bundle_fixture(tmp_path, extras)
    expected_sources = [(255, 0, 0), (0, 0, 255), (0, 128, 0), (255, 255, 0)][: extras + 2]
    calls = []

    def respond(request):
        body = json.loads(request.content)
        if request.url.path.endswith("/images/edits"):
            calls.append("generation")
            assert [pixel(image) for image in body["images"]] == [
                (0, 0, 255),
                (255, 0, 0),
                *expected_sources[2:],
            ]
            assert "primary_source=context" in body["prompt"]
            assert "NEVER turn a primary back view into front" in body["prompt"]
            assert "Do not mirror viewer sides" in body["prompt"]
            assert "source_id" in body["prompt"] and "extra_1" in body["prompt"]
            assert "BACK-SIDE" in body["prompt"]
            return image_response("purple")
        name = body["response_format"]["json_schema"]["name"]
        calls.append(name)
        prompt, content = body["messages"][0]["content"], body["messages"][1]["content"]
        assert "primary_source=context" in prompt
        assert "NEVER turn a primary back view into front" in prompt
        assert "extra_1 and extra_2" in prompt
        if name == "reconstruction_plan":
            assert image_pixels(content) == expected_sources
            return chat_response(source_plan("back"))
        assert name == "reconstruction_fidelity"
        assert image_pixels(content) == [*expected_sources, (128, 0, 128)]
        return chat_response(fidelity_answer("back"))

    provider = create_provider(tmp_path, respond)
    destination = tmp_path / "candidate.png"
    try:
        report = await provider.reconstruct(
            bundle.paths["cutout"], bundle.paths["context"], garment(), destination, source_bundle=bundle
        )
    finally:
        await provider.close()
    assert calls == ["reconstruction_plan", "generation", "reconstruction_fidelity"]
    assert is_accepted(report) and destination.is_file()
    assert report["plan"]["source_view"] == "back"
    assert report["plan"]["reference_manifest"] == filled_manifest(bundle)
    assert all(source.reference_sha256 is None for source in bundle.manifest.sources)
    for source, saved in zip(
        bundle.manifest.model_dump()["sources"], report["plan"]["reference_manifest"]["sources"]
    ):
        assert {**saved, "reference_sha256": None} == source


async def test_extra_native_roi_uses_bound_original_bytes_through_refinement_generation_and_audit(tmp_path):
    bundle = bundle_fixture(tmp_path)
    plan, answer, checks = regional_fixture("pattern")
    plan["detail_regions"][0]["source_image"] = "extra_1"
    checks = matching_region_checks(checks)
    original_extra = bundle.paths["extra_1"].read_bytes()
    calls = []

    def respond(request):
        body = json.loads(request.content)
        if request.url.path.endswith("/images/edits"):
            calls.append("generation")
            assert [pixel(image) for image in body["images"]] == [
                (0, 0, 255),
                (255, 0, 0),
                (0, 128, 0),
                (255, 255, 0),
                (0, 128, 0),
            ]
            assert '"source_id": "extra_1"' in body["prompt"]
            return image_response("white")
        name = body["response_format"]["json_schema"]["name"]
        calls.append(name)
        content = body["messages"][1]["content"]
        if name == "reconstruction_plan":
            # A concurrent file replacement after normalization must never
            # change source pixels or hashes within this reconstruction.
            bundle.paths["extra_1"].write_bytes(encoded_image("purple"))
            return chat_response(plan)
        if name == "reconstruction_source_details":
            assert image_pixels(content) == [
                (255, 0, 0),
                (0, 0, 255),
                (0, 128, 0),
                (255, 255, 0),
                (0, 128, 0),
                (255, 0, 0),
                (255, 0, 0),
                (255, 0, 0),
            ]
            caption = next(
                block["text"]
                for block in content
                if block.get("text", "").startswith("Inspect this original")
            )
            assert '"source_image": "extra_1"' in caption
            assert '"box": [0.15, 0.15, 0.7, 0.8]' in caption
            surveys = [
                block["text"]
                for block in content
                if block.get("text", "").startswith("SOURCE_BOUNDARY_SURVEY")
            ]
            assert len(surveys) == 3 and all('"source_image": "context"' in value for value in surveys)
            return chat_response(
                {"detail_regions": plan["detail_regions"], "visible_features": [], "additional_regions": []}
            )
        if name == "reconstruction_fidelity":
            assert image_pixels(content) == [
                (255, 0, 0),
                (0, 0, 255),
                (0, 128, 0),
                (255, 255, 0),
                (255, 255, 255),
            ]
            return chat_response(answer)
        assert name == "reconstruction_regional_fidelity"
        assert image_pixels(content) == [(0, 128, 0), (255, 255, 255)]
        assert '"source_image":"extra_1"' in content[0]["text"]
        return chat_response({"regional_checks": checks})

    provider = create_provider(tmp_path, respond)
    try:
        report = await provider.reconstruct(
            bundle.paths["cutout"],
            bundle.paths["context"],
            garment(),
            tmp_path / "candidate.png",
            source_bundle=bundle,
        )
    finally:
        await provider.close()
    assert len(calls) == 5 and is_accepted(report)
    assert report["plan"]["reference_manifest"]["sources"][2]["file_sha256"] == digest(original_extra)
    assert bundle.paths["extra_1"].read_bytes() != original_extra


@pytest.mark.parametrize(
    "change", ["hash", "normalized-hash", "missing-path", "wrong-primary-path", "overwrite-extra"]
)
async def test_stale_or_misbound_sources_fail_before_any_model_call(tmp_path, change):
    bundle = bundle_fixture(tmp_path)
    cutout, destination = bundle.paths["cutout"], tmp_path / "candidate.png"
    if change == "hash":
        bundle.paths["extra_1"].write_bytes(encoded_image("purple"))
    elif change == "normalized-hash":
        manifest = bundle.manifest.model_dump()
        manifest["sources"][2]["reference_sha256"] = "0" * 64
        bundle = ReconstructionSourceBundle(ReferenceManifest.model_validate(manifest), bundle.paths)
    elif change == "missing-path":
        bundle.paths["extra_1"].unlink()
    elif change == "wrong-primary-path":
        cutout = tmp_path / "another-cutout.png"
        cutout.write_bytes(bundle.paths["cutout"].read_bytes())
    else:
        destination = bundle.paths["extra_1"]
    calls = []

    def respond(request):
        calls.append(request)
        raise AssertionError("Invalid references must not make a paid request")

    provider = create_provider(tmp_path, respond)
    try:
        with pytest.raises(ProviderError) as error:
            await provider.reconstruct(
                cutout, bundle.paths["context"], garment(), destination, source_bundle=bundle
            )
    finally:
        await provider.close()
    assert not calls and "test-reconstruction-secret" not in str(error.value)
    if change != "overwrite-extra":
        assert not destination.exists()


@pytest.mark.parametrize(
    "change",
    [
        "duplicate-id",
        "extra-gap",
        "no-group",
        "no-membership",
        "extra-same-item",
        "wrong-primary-item",
        "wrong-primary-job",
        "wrong-role",
        "wrong-order",
        "wrong-primary",
        "too-many",
    ],
)
def test_manifest_requires_unique_bound_originals_and_confirmed_group_snapshot(tmp_path, change):
    value = bundle_fixture(tmp_path).manifest.model_dump()
    sources = value["sources"]
    if change == "duplicate-id":
        sources[3]["id"] = "extra_1"
    elif change == "extra-gap":
        del sources[2]
    elif change == "no-group":
        value["group_id"] = None
    elif change == "no-membership":
        value["membership_sha256"] = None
    elif change == "extra-same-item":
        sources[2]["item_id"] = value["target_item_id"]
    elif change == "wrong-primary-item":
        sources[1]["item_id"] = sources[2]["item_id"]
    elif change == "wrong-primary-job":
        sources[1]["job_id"] = sources[2]["job_id"]
        sources[1]["media_url"], sources[1]["source_url"] = sources[2]["media_url"], None
    elif change == "wrong-role":
        sources[2]["role"] = "target_locator"
    elif change == "wrong-order":
        sources[2:] = reversed(sources[2:])
    elif change == "wrong-primary":
        value["primary_source"] = "cutout"
    else:
        sources.append(copy.deepcopy(sources[3]))
    with pytest.raises(ValidationError):
        ReferenceManifest.model_validate(value)


@pytest.mark.parametrize("field", ["media_url", "source_url", "original_url"])
@pytest.mark.parametrize(
    "path",
    [
        "https://example.com/a.jpg",
        "/media/OTHER/x.jpg",
        "{base}/../source.jpg",
        "{base}/reconstructions/candidate.png",
        "{base}/shopping/1.jpg",
        "{base}/%2e%2e/source.jpg",
        "{base}/a.jpg?token=secret",
        "{base}//source.jpg",
        "{base}/a\\b.jpg",
    ],
)
def test_provenance_urls_are_local_own_job_original_media(tmp_path, field, path):
    value = bundle_fixture(tmp_path).manifest.model_dump()
    entry = value["sources"][2]
    entry[field] = path.format(base=f"/media/{entry['job_id']}")
    with pytest.raises(ValidationError):
        ReferenceManifest.model_validate(value)


def test_source_bundle_freezes_exact_pathmap_but_does_not_invent_nullable_provenance(tmp_path):
    bundle = bundle_fixture(tmp_path, context=False)
    assert bundle.manifest.primary_source == "cutout"
    assert bundle.manifest.sources[0].original_url is None
    with pytest.raises(TypeError):
        bundle.paths["extra_1"] = tmp_path / "other.png"
    for paths in ({"cutout": bundle.paths["cutout"]}, {**bundle.paths, "untrusted": tmp_path / "other.png"}):
        with pytest.raises(ValueError):
            ReconstructionSourceBundle(bundle.manifest, paths)


@pytest.mark.parametrize("location", ["final", "initial", "original-ambiguous"])
@pytest.mark.parametrize("missing", ["manifest", "source", "transmitted-hash"])
def test_extra_region_acceptance_requires_declared_transmitted_reference_everywhere(
    tmp_path, location, missing
):
    bundle = bundle_fixture(tmp_path)
    plan, _, _ = regional_fixture()
    region = copy.deepcopy(plan["detail_regions"][0])
    region["source_image"] = "extra_2"
    plan["reference_manifest"] = filled_manifest(bundle)
    if location == "final":
        plan["detail_regions"] = [region]
    else:
        initial = copy.deepcopy(plan)
        initial.pop("reference_manifest")
        if location == "original-ambiguous":
            region["support"] = "ambiguous"
        initial["detail_regions"] = [region]
        plan["initial_source_plan"] = initial
        plan["original_ambiguous_regions"] = [region] if location == "original-ambiguous" else []
    if missing == "manifest":
        plan.pop("reference_manifest")
    elif missing == "source":
        plan["reference_manifest"]["sources"].pop()
    else:
        plan["reference_manifest"]["sources"][3]["reference_sha256"] = None
    with pytest.raises(ValidationError):
        ReconstructionPlan.model_validate(plan)
    assert not is_accepted({"plan": plan, "status": "accepted"})


def test_valid_extra_region_preserves_unknown_and_real_mismatch_semantics_and_legacy(tmp_path):
    bundle = bundle_fixture(tmp_path)
    plan, answer, checks = regional_fixture()
    plan["reference_manifest"] = filled_manifest(bundle)
    plan["detail_regions"][0]["source_image"] = "extra_1"
    passed = matching_region_checks(checks)
    report = build_report(plan, [assess_fidelity(plan, answer, 1, passed)])
    assert is_accepted(report)
    assert validate_report(report)["plan"]["reference_manifest"] == filled_manifest(bundle)
    assert not is_accepted(build_report(plan, [assess_fidelity(plan, answer, 1, checks)]))
    plan["detail_regions"][0]["support"] = "ambiguous"
    unknown = build_report(plan, [assess_fidelity(plan, answer, 1, passed)])
    assert unknown["status"] == "needs_review" and not is_accepted(unknown)
    legacy_plan = source_plan()
    legacy = build_report(legacy_plan, [assess_fidelity(legacy_plan, fidelity_answer(), 1)])
    legacy["plan"].pop("reference_manifest", None)
    assert is_accepted(legacy) and validate_report(legacy)["plan"]["reference_manifest"] is None


async def test_standalone_audit_requires_the_same_manifest_and_sends_all_originals(tmp_path):
    bundle = bundle_fixture(tmp_path)
    plan = {**source_plan(), "reference_manifest": filled_manifest(bundle)}
    candidate = tmp_path / "candidate.png"
    candidate.write_bytes(encoded_image("purple"))
    requests = []

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        assert body["response_format"]["json_schema"]["name"] == "reconstruction_fidelity"
        assert image_pixels(body["messages"][1]["content"]) == [
            (255, 0, 0),
            (0, 0, 255),
            (0, 128, 0),
            (255, 255, 0),
            (128, 0, 128),
        ]
        return chat_response(fidelity_answer())

    provider = create_provider(tmp_path, respond)
    try:
        with pytest.raises(ProviderError, match="do not match the saved reconstruction reference manifest"):
            await provider.inspect_reconstruction(
                plan, bundle.paths["cutout"], bundle.paths["context"], candidate
            )
        assert not requests
        report = await provider.inspect_reconstruction(
            plan, bundle.paths["cutout"], bundle.paths["context"], candidate, source_bundle=bundle
        )
    finally:
        await provider.close()
    assert is_accepted(report) and len(requests) == 1


async def test_four_bases_five_details_and_previous_candidate_are_ten_distinct_bound_inputs(tmp_path):
    bundle = bundle_fixture(tmp_path)
    plan, _, _ = regional_fixture("pattern")
    plan["reference_manifest"] = filled_manifest(bundle)
    region = plan["detail_regions"][0]
    plan["detail_regions"] = [{**region, "name": f"detail-{n}", "source_image": "extra_2"} for n in range(5)]
    previous = tmp_path / "previous.png"
    previous.write_bytes(encoded_image("purple"))
    requests = []

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        assert request.url.path.endswith("/images/edits")
        assert len(body["images"]) == 10
        assert [pixel(image) for image in body["images"]] == [
            (0, 0, 255),
            (255, 0, 0),
            (0, 128, 0),
            (255, 255, 0),
            *[(255, 255, 0)] * 5,
            (128, 0, 128),
        ]
        entries = json.loads(
            body["prompt"].split("Reference manifest (JSON data): ")[1].split("\nThis is a targeted")[0]
        )
        assert [entry["source_id"] for entry in entries[:4]] == ["cutout", "context", "extra_1", "extra_2"]
        assert all(entry["source_id"] == "extra_2" for entry in entries[4:9])
        assert entries[-1] == {"image_number": 10, "role": "SYNTHETIC_EDIT_BASE"}
        return image_response()

    provider = create_provider(tmp_path, respond)
    try:
        originals, references, _, _ = await provider._reconstruction_inputs(bundle.paths, bundle)
        details = await provider._reconstruction_generation_details(originals, plan)
        await provider._generate_reconstruction(
            references,
            plan,
            {},
            ReconstructionRepairTargets().model_dump(),
            tmp_path / "result.png",
            detail_references=details,
            previous_candidate=previous,
        )
        with pytest.raises(ProviderError, match="exceed the safe quantity limit"):
            await provider._generate_reconstruction(
                references,
                plan,
                {},
                ReconstructionRepairTargets().model_dump(),
                tmp_path / "too-many.png",
                detail_references=[*details, details[0]],
                previous_candidate=previous,
            )
    finally:
        await provider.close()
    assert len(requests) == 1 and not (tmp_path / "too-many.png").exists()


async def test_extra_native_coordinates_follow_its_own_exif_orientation(tmp_path):
    bundle = bundle_fixture(tmp_path)
    picture = Image.new("RGB", (80, 40), "blue")
    picture.paste("red", (0, 0, 40, 20))
    exif = picture.getexif()
    exif[274] = 6
    picture.save(bundle.paths["extra_2"], exif=exif)
    manifest = bundle.manifest.model_dump()
    manifest["sources"][3]["file_sha256"] = digest(bundle.paths["extra_2"].read_bytes())
    bundle = ReconstructionSourceBundle(ReferenceManifest.model_validate(manifest), bundle.paths)
    plan, _, _ = regional_fixture()
    plan["reference_manifest"] = filled_manifest(bundle)
    plan["detail_regions"][0].update(source_image="extra_2", box=[0.55, 0.05, 0.95, 0.45])
    provider = create_provider(tmp_path, lambda _: pytest.fail("This pixel check has no model request"))
    try:
        originals, _, _, bound = await provider._reconstruction_inputs(bundle.paths, bundle)
        details = await provider._reconstruction_generation_details(originals, plan)
    finally:
        await provider.close()
    raw = base64.b64decode(details[0]["image_url"].split(",", 1)[1])
    with Image.open(io.BytesIO(raw)) as crop:
        assert crop.size == (18, 36)
        assert crop.getextrema() == ((255, 255), (0, 0), (0, 0))
    assert bound["sources"][3]["reference_sha256"] != bound["sources"][3]["file_sha256"]


async def test_missing_context_keeps_cutout_primary_and_still_supplies_both_extras(tmp_path):
    bundle = bundle_fixture(tmp_path, context=False)
    calls = []

    def respond(request):
        body = json.loads(request.content)
        calls.append(body)
        assert "primary_source=cutout" in body["messages"][0]["content"]
        assert image_pixels(body["messages"][1]["content"]) == [(0, 0, 255), (0, 128, 0), (255, 255, 0)]
        value = source_plan()
        value["primary_front_state"]["source_image"] = "cutout"
        return chat_response(value)

    provider = create_provider(tmp_path, respond)
    try:
        plan = await provider.plan_reconstruction(
            bundle.paths["cutout"], None, garment(), source_bundle=bundle
        )
    finally:
        await provider.close()
    assert len(calls) == 1 and plan["reference_manifest"]["primary_source"] == "cutout"


async def test_model_cannot_name_absent_extra_even_after_bounded_shape_retry(tmp_path):
    bundle = bundle_fixture(tmp_path, extras=1)
    plan, _, _ = regional_fixture()
    plan["detail_regions"][0]["source_image"] = "extra_2"
    calls = []

    def respond(request):
        body = json.loads(request.content)
        calls.append(body)
        assert body["response_format"]["json_schema"]["name"] == "reconstruction_plan"
        return chat_response(plan)

    provider = create_provider(tmp_path, respond)
    try:
        with pytest.raises(ProviderError) as error:
            await provider.reconstruct(
                bundle.paths["cutout"],
                bundle.paths["context"],
                garment(),
                tmp_path / "candidate.png",
                source_bundle=bundle,
            )
    finally:
        await provider.close()
    assert len(calls) == 2 and not (tmp_path / "candidate.png").exists()
    assert all(entry["status"] == "invalid" for entry in error.value.diagnostics)
    assert image_pixels(calls[0]["messages"][1]["content"]) == image_pixels(
        calls[1]["messages"][1]["content"]
    )


async def test_cancellation_with_four_sources_stops_request_without_writing_originals_or_output(tmp_path):
    bundle = bundle_fixture(tmp_path)
    before = {name: path.read_bytes() for name, path in bundle.paths.items()}
    entered, stopped = asyncio.Event(), asyncio.Event()

    async def respond(request):
        assert request.url.path.endswith("/chat/completions")
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    provider = create_provider(tmp_path, respond)
    candidate = tmp_path / "candidate.png"
    task = asyncio.create_task(
        provider.reconstruct(
            bundle.paths["cutout"], bundle.paths["context"], garment(), candidate, source_bundle=bundle
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stopped.is_set() and not candidate.exists()
        assert {name: path.read_bytes() for name, path in bundle.paths.items()} == before
    finally:
        task.cancel()
        await provider.close()

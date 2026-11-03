"""Keep source-backed UI verdicts aligned with the provider's typed contract."""

import copy
import json
import subprocess

import pytest

from wardrobe.reconstruction_quality import is_accepted, is_preview_ready
from wardrobe.reconstruction_sources import ReferenceManifest

from test_preview_ui_contract import preview_report
from test_reconstruction_regions_ui import region_report
from test_reconstruction_ui_contract import NODE, NODE_PROBE, ROOT

pytestmark = pytest.mark.skipif(NODE is None, reason="Node is required for actual frontend helper checks")

TARGET = "a" * 32
GROUP = "9" * 32
REFERENCES = ["b" * 32, "c" * 32, "d" * 32]


def source_manifest(extras=None, *, normalized=True):
    extras = REFERENCES[:2] if extras is None else extras
    entries = []
    for name, ident, filename in [
        ("cutout", TARGET, "crops/cutout.jpg"),
        ("context", TARGET, "crops/context.jpg"),
        *[(f"extra_{index + 1}", ident, "crops/context.jpg") for index, ident in enumerate(extras)],
    ]:
        entries.append(
            {
                "id": name,
                "item_id": ident,
                "job_id": ident,
                "role": "target_locator" if name == "cutout" else "original_context",
                "media_url": f"/media/{ident}/{filename}",
                "source_url": f"/media/{ident}/preview.jpg",
                "original_url": f"/media/{ident}/source.jpg",
                "file_sha256": "0" * 64,
                "reference_sha256": "1" * 64 if normalized else None,
            }
        )
    return {
        "version": 1,
        "target_item_id": TARGET,
        "group_id": GROUP if extras else None,
        "membership_sha256": "2" * 64 if extras else None,
        "primary_source": "context",
        "sources": entries,
    }


def multiview_report():
    report = region_report()
    report["plan"]["reference_manifest"] = source_manifest()
    report["plan"]["detail_regions"][3]["source_image"] = "extra_1"
    report["plan"]["detail_regions"][4]["source_image"] = "extra_2"
    report["plan"]["initial_source_plan"]["detail_regions"][0]["source_image"] = "extra_1"
    return report


def manifest_cases():
    output = [("canonical-two-extra-originals", source_manifest(), True)]
    for name, mutate in [
        ("extra-one-required", lambda m: m["sources"].pop(2)),
        ("cutout-required", lambda m: m["sources"].pop(0)),
        ("canonical-source-order", lambda m: m["sources"].reverse()),
        ("no-duplicate-source-id", lambda m: m["sources"][3].update(id="extra_1")),
        ("no-duplicate-extra-item", lambda m: m["sources"][3].update(item_id=REFERENCES[0])),
        ("extra-cannot-be-target", lambda m: m["sources"][2].update(item_id=TARGET)),
        ("target-bindings", lambda m: m["sources"][1].update(item_id=REFERENCES[0])),
        ("primary-job-bindings", lambda m: m["sources"][1].update(job_id=REFERENCES[0])),
        ("context-is-primary", lambda m: m.update(primary_source="cutout")),
        ("confirmed-group-required", lambda m: m.update(group_id=None, membership_sha256=None)),
        ("group-snapshot-pair", lambda m: m.update(membership_sha256=None)),
        ("valid-membership-hash", lambda m: m.update(membership_sha256="not-a-hash")),
        ("valid-file-hash", lambda m: m["sources"][2].update(file_sha256="1" * 63)),
        ("lowercase-identifiers", lambda m: m["sources"][2].update(item_id="B" * 32)),
        ("role-matches-source", lambda m: m["sources"][2].update(role="target_locator")),
        ("media-own-job", lambda m: m["sources"][2].update(media_url=f"/media/{TARGET}/source.jpg")),
        ("original-own-job", lambda m: m["sources"][2].update(original_url=f"/media/{TARGET}/source.jpg")),
        ("no-external-url", lambda m: m["sources"][2].update(media_url="https://www.example.com/source.jpg")),
        (
            "no-ai-preview",
            lambda m: m["sources"][2].update(media_url=f"/media/{REFERENCES[0]}/reconstructions/old.jpg"),
        ),
        (
            "no-shopping-image",
            lambda m: m["sources"][2].update(media_url=f"/media/{REFERENCES[0]}/shopping/product.jpg"),
        ),
        (
            "no-encoded-path",
            lambda m: m["sources"][2].update(media_url=f"/media/{REFERENCES[0]}/%2e%2e/old.jpg"),
        ),
        (
            "no-empty-path-segment",
            lambda m: m["sources"][2].update(media_url=f"/media/{REFERENCES[0]}//source.jpg"),
        ),
        ("no-query", lambda m: m["sources"][2].update(media_url=f"/media/{REFERENCES[0]}/source.jpg?x=1")),
        ("image-extension", lambda m: m["sources"][2].update(media_url=f"/media/{REFERENCES[0]}/source.svg")),
        ("forbid-extra-manifest-fields", lambda m: m.update(internal_path="/tmp/not-real")),
        ("forbid-extra-source-fields", lambda m: m["sources"][2].update(path="/tmp/not-real")),
    ]:
        value = source_manifest()
        mutate(value)
        output.append((name, value, False))
    return output


PROBE = NODE_PROBE.replace(
    "accepted: acceptedReconstructionReport(input),",
    "accepted: acceptedReconstructionReport(input), preview: readyPreviewReport(input), manifest: validReferenceManifest(input?.plan?.reference_manifest),",
)


def ui_report(report):
    result = subprocess.run(
        [NODE, "-e", PROBE],
        cwd=ROOT,
        input=json.dumps(report, ensure_ascii=False),
        text=True,
        capture_output=True,
        check=True,
    )
    return json.loads(result.stdout)


@pytest.mark.parametrize(
    "name,manifest,expected", manifest_cases(), ids=lambda value: value if isinstance(value, str) else None
)
def test_manifest_parity(name, manifest, expected):
    report = multiview_report()
    report["plan"]["reference_manifest"] = manifest
    try:
        ReferenceManifest.model_validate(manifest)
        provider_valid = True
    except ValueError:
        provider_valid = False
    actual = ui_report(report)
    assert actual["manifest"] == provider_valid == expected, name
    assert actual["accepted"] == is_accepted(report) == expected, name


def coverage_cases():
    good = multiview_report()
    missing = copy.deepcopy(good)
    missing["plan"].pop("reference_manifest")
    hash_missing = copy.deepcopy(good)
    hash_missing["plan"]["reference_manifest"]["sources"][2]["reference_sha256"] = None
    extra_missing = copy.deepcopy(good)
    extra_missing["plan"]["reference_manifest"]["sources"].pop()
    initial_missing = copy.deepcopy(good)
    initial_missing["plan"]["detail_regions"] = initial_missing["plan"]["detail_regions"][:3]
    initial_missing["regional_checks"] = initial_missing["regional_checks"][:3]
    initial_missing["attempts"][0]["regional_checks"] = copy.deepcopy(initial_missing["regional_checks"])
    initial_missing["plan"]["reference_manifest"] = source_manifest([])
    return [
        ("legacy-primary-only", region_report(), True),
        ("extra-source-obligations-covered", good, True),
        ("extra-region-without-manifest", missing, False),
        ("normalized-reference-hash-required", hash_missing, False),
        ("final-region-source-missing", extra_missing, False),
        ("initial-source-obligation-missing", initial_missing, False),
    ]


@pytest.mark.parametrize(
    "name,report,expected", coverage_cases(), ids=lambda value: value if isinstance(value, str) else None
)
def test_strict_source_coverage(name, report, expected):
    assert ui_report(report)["accepted"] == is_accepted(report) == expected, name


def test_preview_source_coverage_and_original_ambiguity_are_not_erased():
    report = preview_report(tucked=True)
    report["plan"]["reference_manifest"] = source_manifest()
    assert ui_report(report)["preview"] == is_preview_ready(report) is True
    report["plan"]["reference_manifest"]["sources"][0]["reference_sha256"] = None
    assert ui_report(report)["preview"] == is_preview_ready(report) is False
    report = preview_report(tucked=True)
    manifest = source_manifest([])
    initial = copy.deepcopy(report["plan"])
    region = {
        "name": "明確原始不明區域",
        "feature": "colour",
        "source_image": "extra_1",
        "box": [0.1, 0.2, 0.6, 0.8],
        "support": "ambiguous",
        "detail": "原照沒有完整顯示此處。",
    }
    initial["detail_regions"] = [region]
    report["plan"].update(
        initial_source_plan=initial,
        original_ambiguous_regions=[copy.deepcopy(region)],
        reference_manifest=manifest,
    )
    # Keep preview inference parity itself valid, so failure specifically includes missing source coverage.
    inferred = {"area": region["name"], "detail": region["detail"]}
    report["preview"]["inferred_areas"].append(inferred)
    report["attempts"][0]["preview"] = copy.deepcopy(report["preview"])
    assert ui_report(report)["preview"] == is_preview_ready(report) is False


@pytest.mark.parametrize(
    "item_id,job_id,expected",
    [(TARGET, TARGET, True), (REFERENCES[0], TARGET, False), (TARGET, REFERENCES[0], False)],
    ids=[
        "image-bound-to-current-item",
        "different-item-cannot-be-verified-hero",
        "different-job-cannot-be-verified-hero",
    ],
)
def test_manifest_is_bound_to_the_image_item(item_id, job_id, expected):
    probe = PROBE.replace(
        "id: 'explicit-ui-fixture', job_id: 'fixture-job'", f"id: '{item_id}', job_id: '{job_id}'"
    )
    assert probe != PROBE
    result = subprocess.run(
        [NODE, "-e", probe],
        cwd=ROOT,
        input=json.dumps(multiview_report()),
        text=True,
        capture_output=True,
        check=True,
    )
    value = json.loads(result.stdout)
    assert value["accepted"] is True
    assert bool(value["image"]) is expected
    assert value["verifiedLabel"] is expected

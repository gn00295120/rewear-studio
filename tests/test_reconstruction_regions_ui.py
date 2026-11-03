"""The final five-region audit keeps complete source and comparison evidence."""

import copy
import json
import subprocess

import pytest

from wardrobe.reconstruction_quality import is_accepted

from test_reconstruction_ui_contract import NODE, NODE_PROBE, ROOT, fixture_report

pytestmark = pytest.mark.skipif(NODE is None, reason="The frontend contract needs a Node runtime")


def region_report(count=5):
    report = fixture_report()
    regions = [
        {
            "name": f"明確測試區域 {index + 1}",
            "feature": "colour",
            "source_image": "context" if index < 3 else "cutout",
            "box": [0.1, round(0.1 + index * 0.12, 2), 0.8, round(0.18 + index * 0.12, 2)],
            "support": "clear",
            "detail": "明確測試：原始像素中的藍色衣物區域。",
        }
        for index in range(count)
    ]
    initial = copy.deepcopy(report["plan"])
    initial["detail_regions"] = copy.deepcopy(regions[:3])
    report["plan"].update(detail_regions=regions, initial_source_plan=initial, original_ambiguous_regions=[])
    checks = [
        {
            "name": region["name"],
            "feature": region["feature"],
            "source_observation": "明確測試：原圖可見藍色。",
            "candidate_observation": "明確測試：生成圖對應區域維持藍色。",
            "status": "pass",
            "reason": "明確測試：對應區域通過比對。",
            "correction": None,
        }
        for region in regions
    ]
    report["regional_checks"] = checks
    report["attempts"][0]["regional_checks"] = copy.deepcopy(checks)
    return report


def cases():
    output = [
        ("three-plus-two-clear-regions", region_report(), True),
        ("six-regions-blocked", region_report(6), False),
    ]
    for name, mutation in [
        (
            "initial-source-stays-at-three-regions",
            lambda report: report["plan"]["initial_source_plan"].update(
                detail_regions=copy.deepcopy(report["plan"]["detail_regions"][:4])
            ),
        ),
        ("missing-fifth-comparison", lambda report: report["regional_checks"].pop()),
        (
            "fifth-box-outside-source",
            lambda report: report["plan"]["detail_regions"][4].update(box=[0.1, 0.2, 1.1, 0.6]),
        ),
        (
            "fifth-box-empty",
            lambda report: report["plan"]["detail_regions"][4].update(box=[0.5, 0.2, 0.5, 0.6]),
        ),
        (
            "fifth-source-not-original",
            lambda report: report["plan"]["detail_regions"][4].update(source_image="preview"),
        ),
        (
            "fifth-region-ambiguous",
            lambda report: report["plan"]["detail_regions"][4].update(support="ambiguous"),
        ),
        ("fifth-feature-mismatch", lambda report: report["regional_checks"][4].update(feature="graphics")),
        (
            "fifth-region-name-mismatch",
            lambda report: report["regional_checks"][4].update(name="沒有對應的來源區域"),
        ),
        (
            "fifth-source-observation-missing",
            lambda report: report["regional_checks"][4].update(source_observation=""),
        ),
        (
            "fifth-comparison-uncertain",
            lambda report: report["regional_checks"][4].update(status="uncertain"),
        ),
    ]:
        report = region_report()
        mutation(report)
        report["attempts"][0]["regional_checks"] = copy.deepcopy(report["regional_checks"])
        output.append((name, report, False))
    return output


CASES = cases()


@pytest.mark.parametrize(("name", "report", "expected"), CASES, ids=[row[0] for row in CASES])
def test_five_region_frontend_gate_matches_backend(name, report, expected):
    response = subprocess.run(
        [NODE, "-e", NODE_PROBE],
        cwd=ROOT,
        input=json.dumps(report, ensure_ascii=False),
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    )
    actual = json.loads(response.stdout)
    assert actual["accepted"] is expected and is_accepted(report) is expected, name
    assert actual["verifiedLabel"] is expected
    assert actual["defaultMedia"] == ("reconstruction" if expected else "context")
    assert {"context", "source", "cutout", "candidate"}.issubset(entry["key"] for entry in actual["media"])

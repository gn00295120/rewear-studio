"""Run the actual UI helpers in Node and compare their strict evidence gate.

No server, database, browser session or model request is used. The runner case
is an unchanged saved real response; all other reports are labelled fixtures.
"""

import copy
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from wardrobe.reconstruction_quality import CHECK_NAMES, is_accepted


ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="The frontend contract needs a Node runtime")

NODE_PROBE = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('wardrobe/static/app.js', 'utf8');
const marker = '  document.addEventListener("click", event => {';
const boundary = source.indexOf(marker);
if (boundary < 0) throw new Error('UI event binding boundary changed; update the contract harness.');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const context = { URL, window: { location: { origin: 'http://127.0.0.1:18500' } }, input };
const probe = `
  const item = {
    id: 'explicit-ui-fixture', job_id: 'fixture-job', reviewed: false,
    provenance: 'explicit UI state fixture',
    record: { category: 'top', subtype: '測試衣物', colors: ['blue'] },
    quality: { status: 'needs_confirmation', visibility: 'partial' },
    image_url: '/media/fixture/context.jpg', source_url: '/media/fixture/source.jpg',
    original_url: '/media/fixture/source.jpg', cutout_url: '/media/fixture/cutout.jpg',
    reconstruction: {
      status: 'completed', image_url: '/media/fixture/reconstruction.png',
      image_validation: input, validation: input,
      candidate_url: '/media/fixture/candidate.png', candidate_validation: input
    }
  };
  const entries = itemMediaEntries(item);
  const card = renderCard(item, 0);
  globalThis.result = {
    accepted: acceptedReconstructionReport(input),
    image: acceptedReconstruction(item),
    cardImage: card.match(/<img src="([^"]+)"/)?.[1],
    verifiedLabel: card.includes('AI Preview · Verified'),
    media: entries.map(entry => ({ key: entry.key, url: entry.url, caption: entry.caption })),
    defaultMedia: defaultItemMedia(entries), concerns: reconstructionConcerns(input)
  };
})();`;
vm.runInNewContext(source.slice(0, boundary) + probe, context, { timeout: 2000 });
process.stdout.write(JSON.stringify(context.result));
"""

NODE_AFFORDABLE_PROBE = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('wardrobe/static/app.js', 'utf8');
const marker = '  document.addEventListener("click", event => {';
const boundary = source.indexOf(marker);
if (boundary < 0) throw new Error('UI event binding boundary changed.');
const context = { URL, window: { location: { origin: 'http://127.0.0.1:18500' } } };
const probe = `
  const job = 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa';
  const attempt = 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb';
  const item = {
    id: 'cccccccccccccccccccccccccccccccc', job_id: job, reviewed: true,
    provenance: 'explicit affordable preview fixture', created_at: new Date().toISOString(),
    record: { category: 'outerwear', subtype: 'coat', colors: ['grey'] },
    quality: { status: 'ready', visibility: 'complete' },
    image_url: '/media/' + job + '/context.jpg', source_url: '/media/' + job + '/source.jpg',
    original_url: '/media/' + job + '/source.jpg', cutout_url: '/media/' + job + '/cutout.png',
    reconstruction: {
      id: attempt, mode: 'preview', status: 'needs_review', selected_candidate_id: '2',
      candidate_url: '/media/' + job + '/reconstructions/' + attempt + '-preview-2.png',
      preview_candidates: ['1','2','3'].map(id => ({
        id, variant_url: '/media/' + job + '/reconstructions/' + attempt + '-preview-' + id + '.png'
      })),
      validation: { version: 2, mode: 'preview', status: 'needs_review', generated: true, issues: ['fixture'] }
    }
  };
  const candidates = affordablePreviewCandidates(item);
  const entries = itemMediaEntries(item, { id: job, media_type: 'image' });
  const card = renderCard(item, 0);
  globalThis.result = {
    candidates, selected: affordablePreview(item),
    keys: entries.map(entry => entry.key), defaultMedia: defaultItemMedia(entries),
    cardImage: card.match(/<img src="([^"]+)"/)?.[1],
    hasLabel: card.includes('AI Preview · Selected')
  };
})();`;
vm.runInNewContext(source.slice(0, boundary) + probe, context, { timeout: 2000 });
process.stdout.write(JSON.stringify(context.result));
"""


def fixture_report():
    plan = {
        "target": "one_garment",
        "source_view": "front",
        "view_evidence": "明確介面測試資料：照片顯示正面。",
        "garment_type": "shirt",
        "visibility": "partial",
        "visible_features": [{"feature": "colour", "detail": "明確測試資料：藍色。"}],
        "uncertain_areas": [],
        "detail_regions": [],
    }
    checks = {name: {"status": "pass", "reason": "明確介面測試資料。"} for name in CHECK_NAMES}
    features = [{"feature": "colour", "status": "pass", "reason": "明確介面測試資料。"}]
    return {
        "version": 1,
        "status": "accepted",
        "source_view": "front",
        "plan": plan,
        "checks": checks,
        "feature_checks": features,
        "regional_checks": [],
        "issues": [],
        "attempts": [
            {
                "number": 1,
                "observed_view": "front",
                "checks": copy.deepcopy(checks),
                "feature_checks": copy.deepcopy(features),
                "regional_checks": [],
                "status": "accepted",
                "issues": [],
                "corrective_feedback": [],
            }
        ],
        "generated": True,
        "retried": False,
        "selected_attempt": 1,
    }


def cases():
    unshown = {
        "area": "背面",
        "detail": "背面沒有出現在照片或這張正面展示圖中。",
        "critical": False,
        "scope": "unshown",
        "scope_evidence": "原圖及候選均展示正面，背面完全朝向另一側。",
        "unshown_view": "back",
    }
    good = fixture_report()
    good["plan"]["uncertain_areas"] = [copy.deepcopy(unshown)]
    entries = [("clear-no-unknowns", fixture_report(), True), ("legitimate-unshown-back", good, True)]
    rear = copy.deepcopy(good)
    rear["source_view"] = rear["plan"]["source_view"] = rear["attempts"][0]["observed_view"] = "back"
    rear["plan"]["uncertain_areas"][0]["unshown_view"] = "front"
    entries.append(("legitimate-unshown-front", rear, True))
    for name, update, remove in [
        ("displayed-unknown", {"scope": "displayed", "unshown_view": None}, None),
        ("unclear-unknown", {"scope": "unclear", "unshown_view": None}, None),
        ("missing-legacy-scope", {}, "scope"),
        ("null-legacy-scope", {"scope": None}, None),
        ("missing-evidence", {}, "scope_evidence"),
        ("empty-evidence", {"scope_evidence": "   "}, None),
        ("same-side-unshown", {"unshown_view": "front"}, None),
        ("missing-unshown-view", {}, "unshown_view"),
        ("critical-opposite-side", {"critical": True}, None),
    ]:
        report = copy.deepcopy(good)
        area = report["plan"]["uncertain_areas"][0]
        area.update(update)
        if remove:
            del area[remove]
        entries.append((name, report, False))
    retained = copy.deepcopy(good)
    retained["plan"]["initial_source_plan"] = copy.deepcopy(good["plan"])
    retained["plan"]["original_ambiguous_regions"] = []
    entries.append(("retained-legitimate-unshown", retained, True))
    removed = copy.deepcopy(retained)
    removed["plan"]["initial_source_plan"]["uncertain_areas"][0].update(scope="displayed", unshown_view=None)
    removed["plan"]["uncertain_areas"] = []
    entries.append(("cannot-erase-initial-display-unknown", removed, False))
    changed = copy.deepcopy(retained)
    changed["plan"]["uncertain_areas"][0]["detail"] = "後續判讀擅自改寫原始未知。"
    entries.append(("cannot-rewrite-initial-uncertainty", changed, False))
    ambiguous = copy.deepcopy(retained)
    region = {
        "name": "原圖肩部",
        "feature": "silhouette",
        "source_image": "context",
        "box": [0.1, 0.1, 0.6, 0.6],
        "support": "ambiguous",
        "detail": "明確測試：肩部被遮住，縮小檢查範圍也不能確認整體。",
    }
    ambiguous["plan"]["initial_source_plan"]["detail_regions"] = [copy.deepcopy(region)]
    ambiguous["plan"]["original_ambiguous_regions"] = [copy.deepcopy(region)]
    entries.append(("original-ambiguous-obligation", ambiguous, False))
    erased = copy.deepcopy(ambiguous)
    erased["plan"]["original_ambiguous_regions"] = []
    entries.append(("cannot-erase-original-ambiguous-obligation", erased, False))
    raw = json.loads((ROOT / "tests/fixtures/reconstruction_runner_legacy.json").read_text())
    assert raw["report"]["status"] == "accepted"
    entries.append(("real-runner-legacy-false-accept", raw["report"], False))
    return entries


CASES = cases()


@pytest.mark.parametrize(("name", "report", "expected"), CASES, ids=[case[0] for case in CASES])
def test_strict_acceptance_and_inspectable_media_match_backend(name, report, expected):
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
    assert actual["accepted"] is expected
    assert is_accepted(report) is expected, f"Backend/UI acceptance diverged for {name}"
    assert actual["verifiedLabel"] is expected
    assert actual["cardImage"].endswith("/reconstruction.png" if expected else "/context.jpg")
    assert actual["defaultMedia"] == ("reconstruction" if expected else "context")
    entries = {entry["key"]: entry for entry in actual["media"]}
    assert {"context", "source", "cutout", "candidate"}.issubset(entries)
    if not expected:
        assert not actual["image"] and "reconstruction" not in entries
        assert entries["legacy"]["url"].endswith("/reconstruction.png")
        assert "Kept for comparison only" in entries["legacy"]["caption"]
    if name in {"displayed-unknown", "missing-legacy-scope", "real-runner-legacy-false-accept"}:
        assert actual["concerns"], "Unknown source details must stay visible to the user"
    if name in {"original-ambiguous-obligation", "cannot-erase-original-ambiguous-obligation"}:
        detail = report["plan"]["initial_source_plan"]["detail_regions"][0]["detail"]
        assert sum(entry["label"] == detail for entry in actual["concerns"]) == 1


def test_affordable_preview_uses_selected_candidate_without_claiming_verification():
    response = subprocess.run(
        [NODE, "-e", NODE_AFFORDABLE_PROBE],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    )
    actual = json.loads(response.stdout)
    assert len(actual["candidates"]) == 3
    assert actual["selected"].endswith("-preview-2.png")
    assert actual["cardImage"].endswith("-preview-2.png") and actual["hasLabel"] is True
    assert actual["defaultMedia"] == "preview-choice-2"
    assert {"preview-choice-1", "preview-choice-2", "preview-choice-3"}.issubset(actual["keys"])

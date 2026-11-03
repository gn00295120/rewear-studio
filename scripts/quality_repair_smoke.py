"""Same-photo reconstruction experiment: fidelity outcomes are not HTTP success.

Uses saved real SAM observations and the production API in a fresh data directory.
Every final candidate, including rejected images, is retained for visual review.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ACTIVE = {"queued", "running"}


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2))


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--code-root", type=Path, default=ROOT, help="Frozen code snapshot for a labelled baseline run"
    )
    args = parser.parse_args()
    code_root = args.code_root.resolve()
    if not (code_root / "wardrobe/app.py").is_file():
        parser.error("code-root must contain the wardrobe package")
    output = args.output.resolve()
    if not output.is_relative_to(ROOT / "out/verification"):
        parser.error("Output must be a fresh verification directory")
    cases = []
    for path in args.manifest:
        manifest = json.loads(path.read_text())
        if not manifest.get("created_before_generation"):
            parser.error("Predeclare cases and quality criteria before inference")
        cases.extend(manifest["cases"])
    if not 1 <= len(cases) <= 10:
        parser.error("Use 1 to 10 declared cases")
    output.mkdir(parents=True, exist_ok=False)
    save(output / "manifest.json", cases)
    report = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "scope": "Saved real observations -> actual production reconstruction API -> evidence plan + image edit + fidelity comparison",
        "no_mocks": True,
        "code_root": str(code_root),
        "code_hashes": {
            str(p.relative_to(code_root)): digest(p) for p in (code_root / "wardrobe").glob("*.py")
        },
        "cases": [],
        "events": [],
        "limitation": "Technical checks verify integrity and gating, not image accuracy. Every generated candidate still needs a separate visual audit.",
    }
    with tempfile.TemporaryDirectory(prefix="rewear-quality-real-") as directory:
        data = Path(directory).resolve()
        os.environ["REWEAR_DATA_DIR"] = str(data)
        if "wardrobe.app" in sys.modules:
            raise RuntimeError("App was imported before data isolation")
        sys.path.insert(0, str(code_root))
        from fastapi.testclient import TestClient
        from wardrobe.app import app
        from wardrobe.providers import Providers
        from wardrobe.reconstruction_quality import is_accepted, is_preview_ready
        import wardrobe.providers

        assert app.state.settings.data_dir == data
        assert Path(wardrobe.providers.__file__).resolve().parent == code_root / "wardrobe"
        assert type(app.state.manager.providers) is Providers
        assert app.state.reconstruction.available
        with TestClient(app) as client:
            for case in cases:
                source_report = ROOT / case["source_report"]
                if case.get("source_report_sha256"):
                    assert digest(source_report) == case["source_report_sha256"], (
                        "Fixed source report changed"
                    )
                result = next(
                    c for c in json.loads(source_report.read_text())["cases"] if c["case"] == case["case"]
                )
                item = next(i for i in result["job"]["items"] if i["id"] == case["item_id"])
                if case.get("fixed_source_record_sha256"):
                    fixed_record = json.dumps(item, ensure_ascii=False, sort_keys=True).encode()
                    assert hashlib.sha256(fixed_record).hexdigest() == case["fixed_source_record_sha256"], (
                        "Fixed observation changed"
                    )
                source = source_report.parent / "jobs" / item["job_id"]
                target = data / "jobs" / item["job_id"]
                if not target.exists():
                    shutil.copytree(source, target)
                    app.state.store.create_job(**json.loads((source / "persisted-job.json").read_text()))
                before = app.state.store.add_item(
                    item["job_id"], **{k: v for k, v in item.items() if k != "job_id"}
                )
                source_hashes = {
                    str(p.relative_to(target)): digest(p) for p in target.rglob("*") if p.is_file()
                }
                response = client.post(f"/api/items/{item['id']}/reconstruction")
                report["cases"].append(
                    {
                        **case,
                        "before": before,
                        "submission_status": response.status_code,
                        "source_hashes": source_hashes,
                    }
                )
            started, previous = time.monotonic(), None
            while True:
                items = [client.get(f"/api/items/{c['item_id']}").json() for c in report["cases"]]
                states = [(i["id"], (i.get("reconstruction") or {}).get("status")) for i in items]
                if states != previous:
                    event = {"seconds": round(time.monotonic() - started, 2), "states": states}
                    report["events"].append(event)
                    print(json.dumps(event), flush=True)
                    save(output / "report.json", report)
                    previous = states
                if not any(status in ACTIVE for _, status in states):
                    break
                if time.monotonic() - started > min(2600, 250 * len(cases)):
                    report["deadline_exceeded"] = True
                    for case in cases:
                        client.post(f"/api/items/{case['item_id']}/reconstruction/cancel")
                    break
                time.sleep(1)
            for case in report["cases"]:
                item = client.get(f"/api/items/{case['item_id']}").json()
                reconstruction = item.get("reconstruction") or {}
                validation = reconstruction.get("validation") or {}
                generated_url = (
                    reconstruction.get("candidate_url")
                    or reconstruction.get("preview_url")
                    or reconstruction.get("image_url")
                )
                response = client.get(generated_url) if generated_url else None
                target = data / "jobs" / item["job_id"]
                case["after"] = item
                case["preview_ready"] = bool(reconstruction.get("preview_url")) and is_preview_ready(
                    reconstruction.get("preview_validation")
                )
                case["fidelity_accepted"] = bool(reconstruction.get("image_url")) and is_accepted(
                    reconstruction.get("image_validation")
                )
                case["technical_checks"] = {
                    "accepted_request": case["submission_status"] == 202,
                    "terminal_outcome": reconstruction.get("status") not in ACTIVE,
                    "quality_report_present": bool(validation),
                    "primary_requires_fidelity": not reconstruction.get("image_url")
                    or is_accepted(reconstruction.get("image_validation")),
                    "completed_requires_acceptance": reconstruction.get("status") != "completed"
                    or is_accepted(validation),
                    "preview_requires_explicit_checks": not reconstruction.get("preview_url")
                    or is_preview_ready(reconstruction.get("preview_validation")),
                    "candidate_served_when_generated": not validation.get("generated")
                    or bool(
                        response
                        and response.status_code == 200
                        and response.content.startswith(b"\x89PNG\r\n\x1a\n")
                    ),
                    "original_evidence_unchanged": {k: v for k, v in item.items() if k != "reconstruction"}
                    == {k: v for k, v in case["before"].items() if k != "reconstruction"},
                    "source_bytes_unchanged": all(
                        digest(target / name) == sha for name, sha in case["source_hashes"].items()
                    ),
                }
            preview = output / "demo-data"
            preview.mkdir()
            shutil.copytree(data / "jobs", preview / "jobs")
            with (
                app.state.store.connect() as source,
                sqlite3.connect(preview / "wardrobe.sqlite3") as destination,
            ):
                source.backup(destination)
        report["seconds"] = round(time.monotonic() - started, 2)
        report["technical_passed"] = all(all(c["technical_checks"].values()) for c in report["cases"])
        report["outcomes"] = {c["case"]: c["after"]["reconstruction"]["status"] for c in report["cases"]}
        report["preview_summary"] = {
            "ready": sum(c["preview_ready"] for c in report["cases"]),
            "requested": len(cases),
            "fidelity_accepted": sum(c["fidelity_accepted"] for c in report["cases"]),
            "meaning": "Model-audited preview readiness; visible fidelity and independent visual review remain separate.",
        }
        save(output / "report.json", report)
    report["temporary_directory_removed"] = not data.exists()
    save(output / "report.json", report)
    print(
        json.dumps(
            {
                "technical_passed": report["technical_passed"],
                "outcomes": report["outcomes"],
                "preview_summary": report["preview_summary"],
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return 0 if report["technical_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

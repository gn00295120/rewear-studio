"""Run licensed outfit photographs through an isolated, real-provider API batch.

No production app is imported until REWEAR_DATA_DIR points to a fresh temporary
directory. The batch uses actual SAM / Spark or Glimmer, preserves failures and
copies derived media into a new verification directory before temporary cleanup.
The manifest must declare cases and photographic provenance before inference.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
AREA = ROOT / "out/verification/lifestyle"
ACTIVE = {"queued", "running"}


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def save_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def checks_for(case, job, persisted, directory):
    items = job.get("items", [])
    visible = [item for item in items if item["quality"]["status"] in {"ready", "needs_confirmation"}]
    categories = {item["record"]["category"] for item in visible}
    expected = case.get("expected", {})
    selected = case.get("person_box") is not None
    source_name = "selected.jpg" if selected else "preview.jpg"
    source_url = f"/media/{job['id']}/{source_name}"
    original_url = f"/media/{job['id']}/preview.jpg"
    checks = {
        "job_completed": job["status"] == "completed",
        "minimum_items": len(items) >= expected.get("min_items", 1),
        "outfit_quality": bool(items)
        and all(item["quality"].get("capture_mode") == "outfit" for item in items),
        "source_links_match_segmentation": bool(items)
        and all(item["source_url"] == source_url for item in items),
        "full_original_links_preserved": bool(items)
        and all(item["original_url"] == original_url for item in items),
        "selection_persisted": persisted.get("options", {}).get("person_box") == case.get("person_box"),
        "uploaded_original_unmodified": digest(directory / "source.jpg") == digest(ROOT / case["path"]),
        "no_incomplete_target_marked_ready": all(
            item["quality"]["status"] != "ready"
            or (
                item["quality"]["model_target"] == "one_garment"
                and item["quality"]["model_visibility"] == "complete"
            )
            for item in items
        ),
        "no_fragment_in_confirmation": all(
            item["quality"]["status"] != "needs_confirmation"
            or (
                item["quality"]["model_target"] == "one_garment"
                and item["quality"]["model_visibility"] == "partial"
                and item["record"]["category"] != "other"
            )
            for item in items
        ),
    }
    if expected.get("some_categories"):
        checks["expected_visible_categories"] = set(expected["some_categories"]) <= categories
    if expected.get("any_categories"):
        checks["expected_visible_category"] = bool(set(expected["any_categories"]) & categories)
    if expected.get("some_confirmation"):
        checks["recognisable_partial_needs_confirmation"] = any(
            item["quality"]["status"] == "needs_confirmation" for item in items
        )
    return checks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=AREA / "fixtures/matrix.json")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--provider", choices=("local", "meta"), default="meta")
    parser.add_argument("--concept", default="garment")
    parser.add_argument(
        "--code-root", type=Path, default=ROOT, help="Frozen code snapshot for a labelled baseline run"
    )
    parser.add_argument("--deadline-seconds", type=int, default=720)
    args = parser.parse_args()
    code_root = args.code_root.resolve()
    if not (code_root / "wardrobe/app.py").is_file():
        parser.error("code-root must contain the wardrobe package")
    if not 60 <= args.deadline_seconds <= 1800:
        parser.error("deadline-seconds must be between 60 and 1800")
    args.concept = args.concept.strip()
    if not 1 <= len(args.concept) <= 100:
        parser.error("concept must contain 1 to 100 characters")
    output = (args.output or AREA / f"model-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}").resolve()
    if not output.is_relative_to(AREA.resolve()) or not output.name.startswith("model"):
        parser.error("output must be a new model* directory inside out/verification/lifestyle")
    manifest = json.loads(args.manifest.read_text())
    cases = manifest["cases"]
    if not manifest.get("created_before_inference") or not 1 <= len(cases) <= 5:
        parser.error("Use a predeclared manifest with 1 to 5 cases")
    for case in cases:
        path = (ROOT / case["path"]).resolve()
        if not path.is_relative_to((AREA / "fixtures").resolve()) or not path.is_file():
            parser.error("All case inputs must be existing lifecycle fixtures")
    output.mkdir(parents=True, exist_ok=False)
    shutil.copy2(args.manifest, output / "manifest.json")
    shutil.copy2(ROOT / manifest["sources"], output / "sources.json")
    report = {
        "started_at": timestamp(),
        "scope": "Actual isolated FastAPI batch -> SAM segmentation -> provider inspection -> quality policy",
        "provider": args.provider,
        "concept": args.concept,
        "no_mocks": True,
        "code_root": str(code_root),
        "manifest_sha256": digest(args.manifest),
        "source_hashes": {case["id"]: digest(ROOT / case["path"]) for case in cases},
        "code_hashes": {
            name: digest(code_root / "wardrobe" / name)
            for name in (
                "app.py",
                "batches.py",
                "pipeline.py",
                "providers.py",
                "quality.py",
                "media.py",
                "outfit.py",
            )
        },
        "case_definitions": cases,
        "cases": [],
        "events": [],
        "limitations": manifest.get(
            "limitations",
            [
                "Small smoke sample, not an accuracy estimate.",
                "The same-garment pair is the SAME photograph with a selection crop, not different captures.",
                "Automatic contract checks do not replace visual examination of each garment crop.",
            ],
        ),
    }
    report_path = output / "report.json"
    save_json(report_path, report)
    previous_data_dir = os.environ.get("REWEAR_DATA_DIR")
    try:
        with tempfile.TemporaryDirectory(prefix="rewear-lifestyle-model-") as temporary:
            os.environ["REWEAR_DATA_DIR"] = temporary
            if "wardrobe.app" in sys.modules:
                raise RuntimeError("App was already imported before isolation; start a fresh Python process")
            sys.path.insert(0, str(code_root))
            # Keep these imports inside the isolation scope. Importing wardrobe.app
            # constructs its default application and must never touch main data.
            from fastapi.testclient import TestClient
            from wardrobe.app import app
            from wardrobe.providers import INSPECTION_PROMPT, WEARING_PROMPT, Providers
            import wardrobe.providers

            assert Path(wardrobe.providers.__file__).resolve().parent == code_root / "wardrobe"

            settings = app.state.settings
            assert settings.data_dir == Path(temporary).resolve()
            assert type(app.state.manager.providers) is Providers
            report["isolation"] = {"temporary_data_dir": temporary, "set_before_app_import": True}
            report["prompt_sha256"] = hashlib.sha256(
                (INSPECTION_PROMPT + WEARING_PROMPT).encode()
            ).hexdigest()
            report["settings"] = {
                key: getattr(settings, key)
                for key in ("request_timeout", "job_timeout", "max_items", "max_batch_files", "ollama_model")
            }
            if not settings.api_key:
                report["preflight_error"] = "Meta API key is not configured"
                save_json(report_path, report)
                return 1

            with TestClient(app) as client:
                report["health"] = client.get("/api/health").json()
                files = [
                    ("files", (case["id"] + ".jpg", (ROOT / case["path"]).read_bytes(), "image/jpeg"))
                    for case in cases
                ]
                response = client.post(
                    "/api/batches",
                    data={
                        "provider": args.provider,
                        "mode": "outfit",
                        "concept": args.concept,
                        "selections": json.dumps([case.get("person_box") for case in cases]),
                        # This runner measures segmentation/inspection. Reconstruction
                        # is evaluated separately with its own saved generation evidence.
                        "reconstruct": "false",
                    },
                    files=files,
                )
                report["submission"] = {"status_code": response.status_code, "body": response.json()}
                save_json(report_path, report)
                if response.status_code != 202:
                    return 1
                batch_id = response.json()["id"]
                started = time.monotonic()
                last_print = 0
                last_states = None
                completed = set()
                deadline_cancelled = False
                while True:
                    batch = client.get(f"/api/batches/{batch_id}").json()
                    report["batch"] = batch
                    jobs = sorted(batch["jobs"], key=lambda job: job["batch_index"])
                    states = [(job["batch_index"], job["status"], job["stage"]) for job in jobs]
                    elapsed = time.monotonic() - started
                    if states != last_states or elapsed - last_print >= 30:
                        event = {"seconds": round(elapsed, 2), "states": states}
                        report["events"].append(event)
                        print(json.dumps(event, ensure_ascii=False), flush=True)
                        last_states, last_print = states, elapsed
                    for job in jobs:
                        if job["status"] in ACTIVE or job["id"] in completed:
                            continue
                        case = cases[job["batch_index"]]
                        detail = client.get(f"/api/jobs/{job['id']}").json()
                        persisted = app.state.store.job(job["id"])
                        directory = settings.data_dir / "jobs" / job["id"]
                        checks = checks_for(case, detail, persisted, directory)
                        artifacts = output / "jobs" / job["id"]
                        shutil.copytree(directory, artifacts)
                        save_json(artifacts / "job.json", detail)
                        save_json(artifacts / "persisted-job.json", persisted)
                        entry = {
                            "case": case["id"],
                            "job_id": job["id"],
                            "seconds_since_batch_start": round(elapsed, 2),
                            "artifact_directory": str(artifacts.relative_to(ROOT)),
                            "checks": checks,
                            "passed_automatic_checks": all(checks.values()),
                            "model_usage": app.state.store.model_usage(entity_id=job["id"]),
                            "job": detail,
                        }
                        report["cases"].append(entry)
                        completed.add(job["id"])
                        print(
                            json.dumps(
                                {"finished_case": case["id"], "status": detail["status"], "checks": checks},
                                ensure_ascii=False,
                            ),
                            flush=True,
                        )
                    save_json(report_path, report)
                    if all(job["status"] not in ACTIVE for job in jobs):
                        break
                    if elapsed > args.deadline_seconds and not deadline_cancelled:
                        client.post(f"/api/batches/{batch_id}/cancel")
                        report["deadline_cancelled"] = True
                        deadline_cancelled = True
                    if elapsed > args.deadline_seconds + 150:
                        report["cancellation_grace_exceeded"] = True
                        break
                    time.sleep(1)
                report["duplicate_suggestions"] = client.get("/api/duplicates").json()
                report["export_json"] = client.get("/api/export?format=json").json()
                report["summary"] = {
                    "cases": len(report["cases"]),
                    "automatic_checks_passed": sum(row["passed_automatic_checks"] for row in report["cases"]),
                    "automatic_checks_failed": sum(
                        not row["passed_automatic_checks"] for row in report["cases"]
                    ),
                    "garments": sum(len(row["job"].get("items", [])) for row in report["cases"]),
                    "batch_seconds": round(time.monotonic() - started, 2),
                }
                report["model_usage"] = app.state.store.model_usage()
                report["finished_at"] = timestamp()
                save_json(report_path, report)
            report["temporary_app_closed"] = True
        report["temporary_directory_removed"] = not Path(temporary).exists()
        save_json(report_path, report)
    finally:
        if previous_data_dir is None:
            os.environ.pop("REWEAR_DATA_DIR", None)
        else:
            os.environ["REWEAR_DATA_DIR"] = previous_data_dir
    print(json.dumps({"report": str(report_path), "summary": report.get("summary")}), flush=True)
    return 0 if report.get("summary", {}).get("automatic_checks_failed") == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

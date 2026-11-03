"""Verify brand observations and live shopping search in an isolated catalogue.

Predeclare one fresh upload and/or saved original observations in a manifest.
Only response JSON is traced: credentials and image request payloads are never saved.
This makes real Meta requests and preserves all outcomes, including empty searches.
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
AREA = ROOT / "out/verification/discovery"
ACTIVE = {"queued", "running"}
EVIDENCE = (
    "record",
    "model_record",
    "quality",
    "reviewed",
    "visual_signature",
    "image_url",
    "cutout_url",
    "source_url",
    "original_url",
    "provenance",
    "reconstruction",
)


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def poll(client, path, field, deadline, cancel_path):
    started, previous = time.monotonic(), None
    while True:
        response = client.get(path)
        response.raise_for_status()
        item = response.json()
        state = item.get(field, {}) if field else item
        current = (state.get("status"), state.get("stage"))
        if current != previous:
            print(
                json.dumps({"path": path, "state": current, "seconds": round(time.monotonic() - started, 2)}),
                flush=True,
            )
            previous = current
        if state.get("status") not in ACTIVE:
            return item
        if time.monotonic() - started > deadline:
            client.post(cancel_path)
            raise TimeoutError("Real model verification deadline exceeded; attempt cancelled")
        time.sleep(1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--deadline-seconds", type=int, default=300)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text())
    if not manifest.get("created_before_inference") or not 1 <= len(manifest["cases"]) <= 5:
        parser.error("Predeclare 1 to 5 cases before inference")
    if not 60 <= args.deadline_seconds <= 900:
        parser.error("deadline-seconds must be between 60 and 900")
    output = args.output.resolve()
    if not output.is_relative_to(AREA.resolve()) or not output.name.startswith("model-"):
        parser.error("output must be a new model-* directory in out/verification/discovery")
    output.mkdir(parents=True, exist_ok=False)
    shutil.copy2(args.manifest, output / "manifest.json")
    (output / "responses").mkdir()
    report = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "scope": "Isolated API with real original-photo brand identification and live web search",
        "no_model_mocks": True,
        "code_hashes": {
            name: digest(ROOT / "wardrobe" / name)
            for name in (
                "app.py",
                "providers.py",
                "identity_schemas.py",
                "discovery.py",
                "store.py",
                "product_evidence.py",
            )
        },
        "cases": [],
        "response_files": [],
        "limitations": manifest.get("limitations", []),
    }
    save(output / "report.json", report)
    previous_data_dir = os.environ.get("REWEAR_DATA_DIR")
    try:
        with tempfile.TemporaryDirectory(prefix="rewear-discovery-real-") as temporary:
            os.environ["REWEAR_DATA_DIR"] = temporary
            if manifest.get("seed_preview"):
                source_preview = (ROOT / manifest["seed_preview"]).resolve()
                if not source_preview.is_relative_to((ROOT / "out/verification").resolve()):
                    raise ValueError("Saved preview must be an existing isolated verification catalogue")
                shutil.copytree(source_preview / "jobs", Path(temporary) / "jobs")
                with (
                    sqlite3.connect(source_preview / "wardrobe.sqlite3") as source,
                    sqlite3.connect(Path(temporary) / "wardrobe.sqlite3") as target,
                ):
                    source.backup(target)
                report["seed_preview"] = str(source_preview.relative_to(ROOT))
                report["scope"] = (
                    "Isolated repeat discovery from saved original observations; no new segmentation or reconstruction"
                )
            if "wardrobe.app" in sys.modules:
                raise RuntimeError("App imported before isolation; use a fresh process")
            sys.path.insert(0, str(ROOT))
            import httpx
            from fastapi.testclient import TestClient
            from openai import AsyncOpenAI
            from wardrobe.app import app
            from wardrobe.providers import Providers

            settings = app.state.settings
            assert settings.data_dir == Path(temporary).resolve()
            assert type(app.state.manager.providers) is Providers
            assert app.state.discovery.available
            report["isolated_before_import"] = True

            async def trace(response):
                if "application/json" not in response.headers.get("content-type", ""):
                    return
                if not response.request.url.path.endswith(("/responses", "/chat/completions")):
                    return
                await response.aread()
                name = f"responses/{len(report['response_files']) + 1:02d}.json"
                payload = response.json()
                # Keep tool results and public model answers, not opaque internal
                # reasoning blobs; this trace is for source provenance only.
                for item in payload.get("output", []):
                    if "encrypted_content" in item:
                        item.pop("encrypted_content")
                        item["encrypted_content_omitted"] = True
                save(output / name, payload)
                report["response_files"].append(
                    {
                        "file": name,
                        "path": response.request.url.path,
                        "status": response.status_code,
                    }
                )

            app.state.manager.providers._meta = AsyncOpenAI(
                base_url=settings.meta_url,
                api_key=settings.api_key,
                timeout=settings.request_timeout,
                max_retries=0,
                http_client=httpx.AsyncClient(
                    timeout=settings.request_timeout,
                    event_hooks={"response": [trace]},
                ),
            )
            with TestClient(app) as client:
                for case in manifest["cases"]:
                    entry = {"id": case["id"], "definition": case}
                    report["cases"].append(entry)
                    try:
                        if case.get("saved_item_id"):
                            if not manifest.get("seed_preview"):
                                raise ValueError("saved_item_id requires seed_preview")
                            item = app.state.store.item(case["saved_item_id"])
                            if not item:
                                raise ValueError("Saved observation not found")
                            entry["reused_original_observation"] = True
                        elif case.get("upload"):
                            path = (ROOT / case["upload"]).resolve()
                            if not path.is_relative_to((ROOT / "out/verification").resolve()):
                                raise ValueError("Upload must be a prepared verification fixture")
                            entry["uploaded_sha256"] = digest(path)
                            submission = client.post(
                                "/api/jobs",
                                data={
                                    "provider": "meta",
                                    "mode": "outfit",
                                    "concept": "garment",
                                    "reconstruct": str(bool(manifest.get("reconstruct"))).lower(),
                                    **(
                                        {"person_box": json.dumps(case["person_box"])}
                                        if case.get("person_box")
                                        else {}
                                    ),
                                },
                                files={
                                    "file": (
                                        path.name,
                                        path.read_bytes(),
                                        "image/png" if path.suffix == ".png" else "image/jpeg",
                                    )
                                },
                            )
                            entry["analysis_submission"] = submission.json()
                            submission.raise_for_status()
                            job_id = submission.json()["id"]
                            job = poll(
                                client,
                                f"/api/jobs/{job_id}",
                                None,
                                args.deadline_seconds,
                                f"/api/jobs/{job_id}/cancel",
                            )
                            entry["analysis"] = job
                            matches = [
                                item
                                for item in job["items"]
                                if item["record"]["category"] == case["target_category"]
                                and item["quality"]["status"] in ("ready", "needs_confirmation")
                            ]
                            if len(matches) != 1:
                                raise ValueError(f"Expected one eligible target, found {len(matches)}")
                            item = matches[0]
                            if manifest.get("reconstruct"):
                                # Wait for every automatically queued garment, not only the target,
                                # so the exported demo contains a complete settled upload.
                                for observed in job["items"]:
                                    if (observed.get("reconstruction") or {}).get("status") in ACTIVE:
                                        poll(
                                            client,
                                            f"/api/items/{observed['id']}",
                                            "reconstruction",
                                            args.deadline_seconds,
                                            f"/api/items/{observed['id']}/reconstruction/cancel",
                                        )
                                item = client.get(f"/api/items/{item['id']}").json()
                                entry["analysis_after_reconstruction"] = client.get(
                                    f"/api/jobs/{job_id}"
                                ).json()
                        else:
                            source_path = (ROOT / case["source_report"]).resolve()
                            if not source_path.is_relative_to(
                                (ROOT / "out/verification/lifestyle").resolve()
                            ):
                                raise ValueError(
                                    "Saved original observation must be from lifestyle verification"
                                )
                            source_report = json.loads(source_path.read_text())
                            source = next(
                                c for c in source_report["cases"] if c["case"] == case["source_case"]
                            )
                            item = next(i for i in source["job"]["items"] if i["id"] == case["item_id"])
                            source_dir = source_path.parent / "jobs" / item["job_id"]
                            shutil.copytree(source_dir, settings.data_dir / "jobs" / item["job_id"])
                            app.state.store.create_job(
                                **json.loads((source_dir / "persisted-job.json").read_text())
                            )
                            app.state.store.add_item(
                                item["job_id"], **{k: v for k, v in item.items() if k != "job_id"}
                            )
                            entry["reused_original_observation"] = True
                        entry["before"] = item
                        directory = settings.data_dir / "jobs" / item["job_id"]
                        entry["original_hashes"] = {
                            str(p.relative_to(directory)): digest(p)
                            for p in directory.rglob("*")
                            if p.is_file()
                        }
                        market = case.get("market", "TW")
                        if market not in ("TW", "global"):
                            raise ValueError("Invalid predeclared search market")
                        response = client.post(f"/api/items/{item['id']}/discovery", json={"market": market})
                        entry["submission_status"] = response.status_code
                        entry["submission"] = response.json()
                        response.raise_for_status()
                        after = poll(
                            client,
                            f"/api/items/{item['id']}",
                            "discovery",
                            args.deadline_seconds,
                            f"/api/items/{item['id']}/discovery/cancel",
                        )
                        entry["after"] = after
                        discovery = after.get("discovery", {})
                        result = discovery.get("result") or {}
                        identity = result.get("identity") or {}
                        shopping = result.get("shopping") or {}
                        offers = shopping.get("offers", [])
                        sources = {s["url"] for s in shopping.get("sources", [])}
                        expected = case["expected"]
                        entry["checks"] = {
                            "accepted": response.status_code == 202,
                            "completed": discovery.get("status") == "completed",
                            "brand_status": identity.get("status") == expected["status"],
                            "brand_name": (identity.get("brand") or "").casefold()
                            == (expected["brand"] or "").casefold(),
                            "no_unseen_product_code": identity.get("product_code") is None,
                            "live_search_completed": shopping.get("status") in ("searched", "no_results"),
                            "offers_have_sources": all(o["url"] in sources for o in offers),
                            "no_same_model_without_code": all(o["match"] == "similar" for o in offers),
                            "verification_v2": shopping.get("version") == 2,
                            "only_verified_candidates": all(
                                (o.get("verification") or {}).get("status") == "verified"
                                and o["verification"].get("market") == "in_scope"
                                and client.get(o["verification"]["image_url"]).status_code == 200
                                and not {"mismatch", "uncertain"}.intersection(
                                    o["verification"].get("comparison", {}).values()
                                )
                                for o in offers
                            ),
                            "observations_unchanged": all(after.get(k) == item.get(k) for k in EVIDENCE),
                            "source_bytes_unchanged": all(
                                digest(directory / name) == value
                                for name, value in entry["original_hashes"].items()
                            ),
                        }
                        profile = identity.get("visual_profile") or {}
                        for key, value in expected.get("visual_profile", {}).items():
                            entry["checks"]["profile_" + key] = (
                                profile.get(key) in value
                                if isinstance(value, list)
                                else profile.get(key) == value
                            )
                        if manifest.get("reconstruct"):
                            reconstruction = after.get("reconstruction") or {}
                            entry["checks"]["complete_display_image"] = (
                                reconstruction.get("status") == "completed"
                                and reconstruction.get("is_synthetic") is True
                                and client.get(reconstruction["image_url"]).status_code == 200
                            )
                        entry["verified_offer_count"] = len(offers)
                    except Exception as error:
                        entry["runner_error"] = f"{type(error).__name__}: {error}"
                        entry["checks"] = {"runner_completed": False}
                    save(output / "report.json", report)
                preview = output / "preview-data"
                preview.mkdir()
                shutil.copytree(settings.data_dir / "jobs", preview / "jobs")
                with (
                    sqlite3.connect(app.state.store.path) as source,
                    sqlite3.connect(preview / "wardrobe.sqlite3") as target,
                ):
                    source.backup(target)
                report["verified_offer_count"] = sum(
                    case.get("verified_offer_count", 0) for case in report["cases"]
                )
                report["positive_product_match_exercised"] = report["verified_offer_count"] > 0
                report["passed"] = all(all(case["checks"].values()) for case in report["cases"]) and (
                    not manifest.get("require_verified_candidate")
                    or report["positive_product_match_exercised"]
                )
                report["finished_at"] = datetime.now(timezone.utc).isoformat()
                save(output / "report.json", report)
        report["temporary_data_removed"] = True
        save(output / "report.json", report)
    finally:
        if previous_data_dir is None:
            os.environ.pop("REWEAR_DATA_DIR", None)
        else:
            os.environ["REWEAR_DATA_DIR"] = previous_data_dir
    print(json.dumps({"passed": report["passed"], "output": str(output)}, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

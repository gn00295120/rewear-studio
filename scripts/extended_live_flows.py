"""Exercise real single/rack/video inference through a disposable application API."""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

from fastapi.testclient import TestClient

from wardrobe.config import Settings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--image", type=Path, required=True, help="Two fully visible garments: top and bottom"
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = {
        "scope": "Fresh Meta SAM/Spark calls through a real API and isolated SQLite; source is a labelled composite fixture",
        "expected_before_inference": {
            "single": "one needs_retake result with multiple_garments target",
            "rack": "two ready items, one top and one bottom",
            "video": "two ready tracked items, one top and one bottom",
        },
        "runs": [],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as output:

        def persist():
            output.seek(0)
            json.dump(report, output, ensure_ascii=False, indent=2)
            output.truncate()
            output.flush()

        persist()
        with tempfile.TemporaryDirectory(prefix="rewear-real-flows-") as temporary:
            directory = Path(temporary)
            # Importing wardrobe.app also constructs its default ASGI app.
            # Keep even that unused instance inside this disposable directory.
            os.environ["REWEAR_DATA_DIR"] = str(directory / "state")
            from wardrobe.app import create_app

            video = directory / "synthetic-static-pair.mp4"
            subprocess.run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-nostdin",
                    "-y",
                    "-loop",
                    "1",
                    "-i",
                    str(args.image),
                    "-t",
                    "3",
                    "-vf",
                    "scale=640:360:force_original_aspect_ratio=decrease,pad=640:360:(ow-iw)/2:(oh-ih)/2:color=white",
                    "-r",
                    "5",
                    "-c:v",
                    "libx264",
                    "-pix_fmt",
                    "yuv420p",
                    str(video),
                ],
                check=True,
                timeout=30,
            )
            app = create_app(Settings(data_dir=directory / "state"))
            with TestClient(app) as client:
                for case, source, mode in [
                    ("single", args.image, "single"),
                    ("rack", args.image, "rack"),
                    ("video", video, "rack"),
                ]:
                    started = time.monotonic()
                    with source.open("rb") as stream:
                        response = client.post(
                            "/api/jobs",
                            files={"file": (source.name, stream)},
                            data={"provider": "meta", "mode": mode, "concept": "garment"},
                        )
                    response.raise_for_status()
                    job_id = response.json()["id"]
                    deadline = time.monotonic() + 300
                    while time.monotonic() < deadline:
                        job = client.get(f"/api/jobs/{job_id}").json()
                        if job["status"] not in {"queued", "running"}:
                            break
                        time.sleep(0.25)
                    items = job["items"]
                    checks = {"completed": job["status"] == "completed"}
                    if case == "single":
                        checks["ambiguous_target"] = len(items) == 1 and all(
                            item["quality"]["status"] == "needs_retake"
                            and item["quality"].get("model_target") == "multiple_garments"
                            for item in items
                        )
                        checks["usable_count"] = job["usable_count"] == 0
                    else:
                        checks["two_complete_items"] = len(items) == 2 and job["usable_count"] == 2
                        checks["categories"] = {item["record"]["category"] for item in items} == {
                            "top",
                            "bottom",
                        }
                        checks["unique_objects"] = len({item["object_id"] for item in items}) == 2
                        if case == "video":
                            checks["tracked_frames"] = all(isinstance(item["frame"], int) for item in items)
                    csv_response = client.get("/api/export", params={"format": "csv", "job_id": job_id})
                    rows = list(csv.DictReader(io.StringIO(csv_response.content.decode("utf-8-sig"))))
                    checks["csv_matches_quality"] = {row["id"]: row["quality_status"] for row in rows} == {
                        item["id"]: item["quality"]["status"] for item in items
                    }
                    json_items = client.get(
                        "/api/export", params={"format": "json", "job_id": job_id}
                    ).json()["items"]
                    checks["json_matches_items"] = {item["id"]: item for item in json_items} == {
                        item["id"]: item for item in items
                    }
                    media = {
                        url
                        for item in items
                        for key in ("image_url", "cutout_url", "source_url")
                        if (url := item.get(key))
                    }
                    if job.get("overlay_url"):
                        media.add(job["overlay_url"])
                    checks["media_available"] = bool(media) and all(
                        client.get(url).status_code == 200 for url in media
                    )
                    entry = {
                        "case": case,
                        "seconds": round(time.monotonic() - started, 2),
                        "checks": checks,
                        "passed": all(checks.values()),
                        "job": job,
                    }
                    report["runs"].append(entry)
                    persist()
                    print(
                        json.dumps({k: entry[k] for k in ("case", "passed", "seconds", "checks")}), flush=True
                    )
            report["temporary_data_removed_after_run"] = True
        persist()
    raise SystemExit(0 if all(run["passed"] for run in report["runs"]) else 1)


if __name__ == "__main__":
    main()

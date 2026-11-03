"""Submit one real analysis through the running application's public API."""

import argparse
import json
import time
from pathlib import Path

import httpx


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", type=Path)
    parser.add_argument("--provider", choices=["local", "meta"], default="local")
    parser.add_argument("--mode", choices=["single", "rack"], default="single")
    parser.add_argument("--concept", default="garment")
    parser.add_argument("--url", default="http://127.0.0.1:8765")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--min-items", type=int, default=1, help="Expected minimum detections for this fixture"
    )
    args = parser.parse_args()
    started = time.monotonic()
    with httpx.Client(base_url=args.url, timeout=15) as client:
        with args.file.open("rb") as source:
            response = client.post(
                "/api/jobs",
                files={"file": (args.file.name, source)},
                data={"provider": args.provider, "mode": args.mode, "concept": args.concept},
            )
        response.raise_for_status()
        job = response.json()
        last = None
        while time.monotonic() - started < 1500:
            response = client.get(f"/api/jobs/{job['id']}")
            response.raise_for_status()
            job = response.json()
            stage = job["status"], job["stage"]
            if stage != last:
                print(job["id"], *stage, flush=True)
                last = stage
            if job["status"] not in {"queued", "running"}:
                break
            time.sleep(2)
        else:
            raise SystemExit("Analysis did not reach a terminal status in 25 minutes.")
    evidence = {"elapsed_seconds": round(time.monotonic() - started, 2), "job": job}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
    print(
        json.dumps(
            {"status": job["status"], "items": job["item_count"], "evidence": str(args.output)},
            ensure_ascii=False,
        )
    )
    if job["status"] != "completed" or job["item_count"] < args.min_items:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

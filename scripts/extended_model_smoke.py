"""Run a predeclared visual matrix against real providers without writing the catalogue.

Fixtures and expected outcomes are declared in a JSON manifest before inference.
Results include failed cases and prompt hashes; an existing report is never replaced.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from wardrobe.config import Settings
from wardrobe.media import prepare_image
from wardrobe.providers import INSPECTION_PROMPT, INSPECTION_SCHEMA, ProviderError, Providers
from wardrobe.quality import apply_inspection


async def run(args):
    manifest_bytes = args.manifest.read_bytes()
    cases = json.loads(manifest_bytes)["cases"]
    if args.cases:
        selected = set(args.cases.split(","))
        cases = [case for case in cases if case["id"] in selected]
        if {case["id"] for case in cases} != selected:
            raise ValueError("Unknown case ID")
    report = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "manifest": str(args.manifest),
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "prompt_sha256": hashlib.sha256(INSPECTION_PROMPT.encode()).hexdigest(),
        "schema_sha256": hashlib.sha256(json.dumps(INSPECTION_SCHEMA, sort_keys=True).encode()).hexdigest(),
        "scope": "Real provider inspection plus media normalization and quality policy; no catalogue writes",
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
        with tempfile.TemporaryDirectory(prefix="rewear-visual-matrix-") as temporary:
            providers = Providers(Settings(data_dir=Path(temporary)))
            try:
                for case in cases:
                    path = Path(case["path"])
                    normalized = Path(temporary) / f"{case['id']}.jpg"
                    metadata = prepare_image(path, normalized)
                    for provider in args.providers.split(","):
                        started = time.monotonic()
                        entry = {
                            "case": case["id"],
                            "provider": provider,
                            "expected": case.get("expected"),
                            "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                            "normalized_size": metadata,
                        }
                        try:
                            assessment = await providers.inspect(normalized, provider)
                            record, original, quality = apply_inspection(assessment)
                            entry.update(assessment=assessment, record=record, quality=quality)
                            expected = case.get("expected")
                            if expected:
                                checks = {"quality": quality["status"] == expected["status"]}
                                if "category" in expected:
                                    checks["category"] = record["category"] == expected["category"]
                                entry.update(checks=checks, passed=all(checks.values()))
                            else:
                                entry["passed"] = None
                        except ProviderError as error:
                            entry.update(error=str(error), passed=False)
                        entry["seconds"] = round(time.monotonic() - started, 2)
                        report["runs"].append(entry)
                        persist()
                        print(
                            json.dumps(
                                {k: entry.get(k) for k in ("case", "provider", "passed", "seconds", "error")}
                            ),
                            flush=True,
                        )
            finally:
                await providers.close()
        runs = report["runs"]
        report["summary"] = {
            "passed": sum(row["passed"] is True for row in runs),
            "failed": sum(row["passed"] is False for row in runs),
            "observational": sum(row["passed"] is None for row in runs),
        }
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        persist()
    print(json.dumps(report["summary"]), flush=True)
    return report["summary"]["failed"] == 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--providers", choices=["local", "meta", "local,meta"], default="local,meta")
    parser.add_argument("--cases", help="Comma-separated case IDs; omitted runs all")
    raise SystemExit(0 if asyncio.run(run(parser.parse_args())) else 1)


if __name__ == "__main__":
    main()

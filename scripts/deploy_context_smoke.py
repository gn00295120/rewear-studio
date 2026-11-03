"""Verify the real Docker ignore rules using disposable, fake-secret fixtures.

This exports a scratch COPY context with BuildKit. It does not build or run the
application image, read real credentials/data, pull a base image, or publish an
image. All fixture files and the exported context are removed on exit.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXPECTED = {
    "deploy/Dockerfile",
    "deploy/requirements.lock",
    "dist/rewear_studio-1.0.0-py3-none-any.whl",
}
EXCLUDED = {
    ".env",
    "data/wardrobe.sqlite3",
    "data/jobs/public-fixture/input.jpg",
    "out/verification/private-fixture.json",
    "wardrobe/config.py",
    "deploy/.env",
    "deploy/private.env",
    "deploy/nested/credential.json",
    "deploy/compose.yaml",
    "deploy/env.example",
    "dist/private.env",
    "dist/.private-token",
    "dist/nested/backup.sqlite3",
    "dist/rewear_studio-1.0.0.tar.gz",
    "dist/rewear_studio-0.9.0-py3-none-any.whl",
}


def run(ignore_file: Path) -> dict:
    rules = ignore_file.read_bytes()
    with tempfile.TemporaryDirectory(prefix="rewear-context-smoke-") as temporary:
        directory = Path(temporary)
        context, exported = directory / "context", directory / "exported"
        context.mkdir()
        (context / ".dockerignore").write_bytes(rules)
        fixtures = {}
        for name in sorted(EXPECTED | EXCLUDED):
            value = f"PUBLIC TEST FIXTURE ONLY, NOT A SECRET OR VALID PACKAGE: {name}\n".encode()
            fixtures[name] = value
            path = context / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(value)
        completed = subprocess.run(
            [
                "docker",
                "buildx",
                "build",
                "--progress=quiet",
                "--network=none",
                "--file",
                "-",
                "--output",
                f"type=local,dest={exported}",
                str(context),
            ],
            input="FROM scratch\nCOPY . /\n",
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )
        included = {str(path.relative_to(exported)): path for path in exported.rglob("*") if path.is_file()}
        matching = all(
            name in included and included[name].read_bytes() == fixtures[name] for name in EXPECTED
        )
        passed = completed.returncode == 0 and set(included) == EXPECTED and matching
        report = {
            "schema_version": 1,
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "dockerignore_path": str(ignore_file),
            "dockerignore_sha256": hashlib.sha256(rules).hexdigest(),
            "passed": passed,
            "build_exit_code": completed.returncode,
            "included_paths": sorted(included),
            "expected_paths": sorted(EXPECTED),
            "excluded_fixture_paths": sorted(EXCLUDED),
            "excluded_fixture_count": len(EXCLUDED),
            "allowed_fixture_bytes_match": matching,
            "unexpected_paths": sorted(set(included) - EXPECTED),
            "missing_paths": sorted(EXPECTED - set(included)),
            "real_credentials_or_data_used": False,
            "application_image_built_or_run": False,
            "base_image": "scratch",
            "fixture_cleanup": "TemporaryDirectory removed all input and exported fixtures before return",
        }
        if completed.returncode:
            # Do not persist arbitrary daemon diagnostics, which may contain
            # operator-specific paths. The exit code is sufficient for evidence.
            report["error"] = "Docker BuildKit did not export the disposable context."
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ignore-file", type=Path, default=ROOT / ".dockerignore")
    parser.add_argument(
        "--output", type=Path, required=True, help="New JSON evidence path; never overwritten"
    )
    args = parser.parse_args()
    report = run(args.ignore_file.resolve())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2, ensure_ascii=False)
        stream.write("\n")
    print(json.dumps({"passed": report["passed"], "evidence": str(args.output)}))
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()

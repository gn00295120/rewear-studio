"""Reconstruct saved real observations through an isolated API, without rerunning SAM.

Requires a manifest declaring original report/case/item IDs. Each run makes fresh
Meta image-edit requests, preserves failures, and saves a separate preview catalogue.
"""

from __future__ import annotations

import argparse
import hashlib
import html
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
AREA = ROOT / "out/verification/reconstruction"
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
)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def gallery(output, report):
    cards = []
    sources = {source["id"]: source for source in report.get("photo_sources", [])}
    for entry in report["cases"]:
        item = entry["after"]
        reconstruction = item.get("reconstruction") or {}
        choices = [
            (f"AI 完整預覽 {candidate['id']}", candidate.get("variant_url"))
            for candidate in reconstruction.get("preview_candidates", [])
            if isinstance(candidate, dict)
        ]
        if not choices:
            choices = [
                (
                    "AI 重建完整展示圖",
                    reconstruction.get("image_url")
                    or reconstruction.get("preview_url")
                    or reconstruction.get("candidate_url"),
                )
            ]
        urls = [
            ("原始照片", item.get("original_url") or item["source_url"]),
            ("實際可見的辨識區域", item["cutout_url"]),
            *choices,
        ]
        figures = []
        for label, url in urls:
            image = (
                f'<img src="preview-data/jobs/{html.escape(url.removeprefix("/media/"), quote=True)}" '
                f'alt="{label}">'
                if url
                else "<p>未產生圖片</p>"
            )
            figures.append(f"<figure>{image}<figcaption>{label}</figcaption></figure>")
        source = sources.get(entry["case"])
        credit = (
            f'<p>原照片：<a href="{html.escape(source["source_page"], quote=True)}">'
            f"{html.escape(source['author'])} / Pexels</a> · "
            '<a href="https://www.pexels.com/license/">Pexels License</a>。重建展示圖經 AI 修改。</p>'
            if source
            else ""
        )
        cards.append(
            f'<article><h2>{html.escape(entry["title"])}</h2><div class="images">'
            + "".join(figures)
            + f"</div><p>{html.escape(entry['inference_note'])}</p>{credit}</article>"
        )
    page = (
        """<!doctype html><html lang="zh-Hant"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>衣序 — 完整衣物重建實測</title><style>
body{margin:0;padding:32px;background:#f6f5ef;color:#244438;font:16px/1.7 system-ui}
main{max-width:1320px;margin:auto}h1{font:36px Georgia,serif}article{margin:48px 0}
.images{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:16px}figure{margin:0}
img{width:100%;height:480px;object-fit:contain;background:#e9e9e0}figcaption{padding:8px}
@media(max-width:700px){body{padding:18px}.images{grid-template-columns:1fr}img{height:400px}}
</style><main><h1>從生活照，到完整衣物展示圖</h1>
<p>本頁衣物均使用實際辨識留下的參考圖，經應用程式 API 重新呼叫 Meta 圖片模型。
重建圖是 AI 展示圖；遮住或未入鏡的部分是推測。原始衣物欄位、完整度和確認狀態不變。</p>
"""
        + "".join(cards)
        + '<p><a href="report.json">完整驗證紀錄</a></p></main></html>'
    )
    (output / "index.html").write_text(page, encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--deadline-seconds", type=int, default=600)
    args = parser.parse_args()
    if not 60 <= args.deadline_seconds <= 1800:
        parser.error("deadline-seconds must be between 60 and 1800")
    manifest = json.loads(args.manifest.read_text())
    if not manifest.get("created_before_generation") or not 1 <= len(manifest["cases"]) <= 5:
        parser.error("Predeclare 1 to 5 cases before generation")
    output = (args.output or AREA / f"model-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}").resolve()
    if not output.is_relative_to(AREA.resolve()) or not output.name.startswith("model-"):
        parser.error("output must be a new model-* directory in out/verification/reconstruction")
    observations, photo_sources = [], {}
    for case in manifest["cases"]:
        report_path = (ROOT / case["source_report"]).resolve()
        if not report_path.is_relative_to((ROOT / "out/verification/lifestyle").resolve()):
            parser.error("Source report must be an existing lifestyle verification report")
        source_report = json.loads(report_path.read_text())
        result = next(c for c in source_report["cases"] if c["case"] == case["case"])
        item = next(i for i in result["job"]["items"] if i["id"] == case["item_id"])
        observations.append((case, report_path.parent / "jobs" / result["job_id"], item))
        source_file = report_path.parent / "sources.json"
        if source_file.is_file():
            for source in json.loads(source_file.read_text()).get("photos", []):
                if source["id"] == case["case"]:
                    photo_sources[source["id"]] = source
    output.mkdir(parents=True, exist_ok=False)
    shutil.copy2(args.manifest, output / "manifest.json")
    report = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "scope": "Real saved SAM/Spark observations -> isolated API -> new Meta image editing",
        "no_generation_mocks": True,
        "reused_observations_not_resegmented": True,
        "photo_sources": list(photo_sources.values()),
        "code_hashes": {
            name: digest(ROOT / "wardrobe" / name)
            for name in ("app.py", "reconstruction.py", "providers.py", "store.py")
        },
        "cases": [],
        "events": [],
    }
    previous_data_dir = os.environ.get("REWEAR_DATA_DIR")
    try:
        with tempfile.TemporaryDirectory(prefix="rewear-reconstruction-real-") as temporary:
            os.environ["REWEAR_DATA_DIR"] = temporary
            if "wardrobe.app" in sys.modules:
                raise RuntimeError("App imported before isolation; use a fresh process")
            sys.path.insert(0, str(ROOT))
            from fastapi.testclient import TestClient
            from wardrobe.app import app
            from wardrobe.providers import Providers

            assert app.state.settings.data_dir == Path(temporary).resolve()
            assert type(app.state.manager.providers) is Providers
            if not app.state.reconstruction.available:
                raise RuntimeError("Meta image model is not configured")
            report["isolated_before_import"] = True
            with TestClient(app) as client:
                for case, source_dir, item in observations:
                    directory = Path(temporary) / "jobs" / item["job_id"]
                    if not directory.exists():
                        shutil.copytree(source_dir, directory)
                        job = json.loads((source_dir / "persisted-job.json").read_text())
                        app.state.store.create_job(**job)
                    before = app.state.store.add_item(
                        item["job_id"], **{key: value for key, value in item.items() if key != "job_id"}
                    )
                    hashes = {
                        str(p.relative_to(directory)): digest(p) for p in directory.rglob("*") if p.is_file()
                    }
                    mode = case.get("mode", manifest.get("mode", "verified"))
                    if mode not in {"preview", "verified"}:
                        raise RuntimeError("Manifest reconstruction mode must be preview or verified")
                    response = client.post(
                        f"/api/items/{item['id']}/reconstruction",
                        json={"reference_item_ids": [], "mode": mode},
                    )
                    report["cases"].append(
                        {
                            **case,
                            "mode": mode,
                            "before": before,
                            "source_hashes": hashes,
                            "submission_status": response.status_code,
                            "submission": response.json(),
                        }
                    )
                started, states = time.monotonic(), None
                while True:
                    current = [client.get(f"/api/items/{c['item_id']}").json() for c in report["cases"]]
                    next_states = [(i["id"], (i.get("reconstruction") or {}).get("status")) for i in current]
                    if next_states != states:
                        event = {"seconds": round(time.monotonic() - started, 2), "states": next_states}
                        report["events"].append(event)
                        print(json.dumps(event), flush=True)
                        states = next_states
                        save(output / "report.json", report)
                    if not any(status in ACTIVE for _, status in next_states):
                        break
                    if time.monotonic() - started > args.deadline_seconds:
                        report["deadline_exceeded"] = True
                        for entry in report["cases"]:
                            client.post(f"/api/items/{entry['item_id']}/reconstruction/cancel")
                        break
                    time.sleep(1)
                for entry in report["cases"]:
                    item = client.get(f"/api/items/{entry['item_id']}").json()
                    reconstruction = item.get("reconstruction") or {}
                    directory = Path(temporary) / "jobs" / item["job_id"]
                    candidates = reconstruction.get("preview_candidates") or []
                    candidate_media = [
                        client.get(candidate["variant_url"])
                        for candidate in candidates
                        if isinstance(candidate, dict) and candidate.get("variant_url")
                    ]
                    media = (
                        client.get(reconstruction["image_url"]) if reconstruction.get("image_url") else None
                    )
                    preview_mode = entry["mode"] == "preview"
                    entry["after"] = item
                    entry["model_usage"] = app.state.store.model_usage(entity_id=item["id"])
                    entry["checks"] = {
                        "accepted": entry["submission_status"] == 202,
                        "expected_terminal_state": reconstruction.get("status")
                        == ("needs_review" if preview_mode else "completed"),
                        "real_generated_png_served": (
                            len(candidate_media) == 3
                            and all(
                                response.status_code == 200
                                and response.content.startswith(b"\x89PNG\r\n\x1a\n")
                                for response in candidate_media
                            )
                            if preview_mode
                            else media is not None
                            and media.status_code == 200
                            and media.content.startswith(b"\x89PNG\r\n\x1a\n")
                        ),
                        "unverified_preview_not_certified": (
                            not reconstruction.get("image_url")
                            and not reconstruction.get("preview_url")
                            and reconstruction.get("validation", {}).get("mode") == "preview"
                            if preview_mode
                            else True
                        ),
                        "synthetic_label": reconstruction.get("is_synthetic") is True,
                        "observations_unchanged": all(
                            item.get(k) == entry["before"].get(k) for k in EVIDENCE
                        ),
                        "source_bytes_unchanged": all(
                            digest(directory / name) == value
                            for name, value in entry["source_hashes"].items()
                        ),
                    }
                preview = output / "preview-data"
                preview.mkdir()
                shutil.copytree(Path(temporary) / "jobs", preview / "jobs")
                with (
                    sqlite3.connect(app.state.store.path) as source,
                    sqlite3.connect(preview / "wardrobe.sqlite3") as target,
                ):
                    source.backup(target)
                report["passed"] = all(all(c["checks"].values()) for c in report["cases"])
                report["quality_claim"] = False
                report["model_usage"] = app.state.store.model_usage()
                report["elapsed_seconds"] = round(time.monotonic() - started, 2)
                save(output / "report.json", report)
                gallery(output, report)
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

"""Brand/shopping UI on a temporary real API with explicit provider fixtures.

No actual image model, web search, retailer page, main server or user catalogue
is contacted. Public-looking offer links are inert fixture data, never opened.
"""

import argparse
import asyncio
import copy
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import traceback
from datetime import UTC, datetime
from pathlib import Path

import httpx
from playwright.sync_api import expect, sync_playwright

from lifestyle_browser_smoke import file_payload, no_overflow, photo, wait_batch
from reconstruction_browser_smoke import ReconstructionFixtures, original_attributes


ROOT = Path(__file__).resolve().parent.parent
OFFER_A = "https://www.nike.com/tw/t/explicit-browser-fixture-a"
OFFER_B = "https://www.adidas.com.tw/explicit-browser-fixture-b"
OFFER_C = "https://www.zara.com/tw/explicit-browser-fixture-c"


class DiscoveryFixtures(ReconstructionFixtures):
    """Deterministic states for integration, not claims about real brands/products."""

    def __init__(self):
        super().__init__()
        self.identity_calls = {}

    async def generate(self, prompt, destination):
        Path(destination).write_bytes(photo())
        return destination

    async def identify_brand(self, cutout_path, context_path, record):
        assert Path(cutout_path).is_file()
        assert "reconstructions" not in Path(cutout_path).parts
        if context_path:
            assert Path(context_path).is_file()
            assert "reconstructions" not in Path(context_path).parts
        subtype = record.get("subtype", "")
        call = self.identity_calls.get(subtype, 0) + 1
        self.identity_calls[subtype] = call
        await asyncio.sleep(2)
        if subtype == "fixture-identity-retry" and call == 1:
            from wardrobe.providers import ProviderError

            raise ProviderError("明確測試用：第一次品牌判讀失敗，請重新查找。")
        unknown = subtype == "fixture-unknown"
        possible = subtype == "fixture-possible"
        return {
            "status": "unknown" if unknown else "possible" if possible else "observed",
            "brand": None if unknown else "EXPLICIT FIXTURE BRAND",
            "evidence_type": "none" if unknown else "logo" if possible else "label",
            "visible_text": None if unknown or possible else "EXPLICIT FIXTURE BRAND CODE123",
            "evidence": "明確測試用的品牌線索，不代表照片實際品牌或真實模型推論。",
            "confidence": 0.0 if unknown else 0.65 if possible else 0.92,
            "product_code": None if unknown or possible else "CODE123",
            "search_terms": [] if unknown else ["EXPLICIT FIXTURE BRAND"],
            "visual_profile": {
                "type": "明確測試用短袖上衣",
                "category": "top",
                "primary_color": "紅色",
                "pattern": "solid",
                "sleeve_length": "short",
                "neckline": "crew",
                "description": "明確測試：紅色圓領短袖素面上衣。",
            },
        }

    async def search_shopping(self, identity, record, market="TW"):
        subtype = record.get("subtype", "")
        await asyncio.sleep(20 if subtype == "fixture-shopping-slow" else 2)
        if subtype == "fixture-shopping-unavailable":
            from wardrobe.providers import ProviderError

            raise ProviderError("明確測試用：搜尋服務暫時無法使用，品牌線索保留。")
        unknown = identity["status"] == "unknown"
        shopping = {
            "status": "no_results" if unknown else "searched",
            "query": f"EXPLICIT BROWSER FIXTURE {market} clothing",
            "offers": []
            if unknown
            else [
                {
                    "title": "明確測試候選 A",
                    "url": OFFER_A,
                    "merchant": "fixture merchant",
                    "match": "model_candidate",
                    "reason": "明確測試用型號文字，尚未驗證是否相同商品。",
                },
                {
                    "title": "明確測試候選 B",
                    "url": OFFER_B,
                    "merchant": "fixture merchant",
                    "match": "similar",
                    "reason": "明確測試用相似款候選，不能據此確認同一商品。",
                },
            ],
            "sources": []
            if unknown
            else [
                {
                    "title": "明確測試候選 A（非真實商品）",
                    "url": OFFER_A,
                    "snippet": "明確測試碼 CODE123；此頁面未被測試程式讀取，僅為來源連結 fixture。",
                },
                {
                    "title": "明確測試候選 B（相似款）",
                    "url": OFFER_B,
                    "snippet": "明確標示的測試來源，不是實際搜尋結果。",
                },
            ],
            "searched_at": datetime.now(UTC).isoformat(),
            "notice": "明確測試用購買候選，未開啟任何商店網頁。",
        }
        if subtype in {"fixture-exclusions", "fixture-all-rejected"}:
            shopping["offers"].append(
                {
                    "title": "明確測試候選 C",
                    "url": OFFER_C,
                    "merchant": "fixture merchant",
                    "match": "similar",
                    "reason": "明確測試用待核對候選。",
                }
            )
            shopping["sources"].append(
                {"title": "明確測試候選 C", "url": OFFER_C, "snippet": "明確標示的核對測試來源。"}
            )
        return shopping

    async def verify_shopping(
        self, identity, record, shopping, cutout_path, context_path, destination, market="TW"
    ):
        """Write real local JPEGs; all page/visual judgments remain explicit fixtures."""
        assert Path(cutout_path).is_file()
        assert not {"reconstructions", "shopping"}.intersection(Path(cutout_path).parts)
        if context_path:
            assert Path(context_path).is_file()
            assert not {"reconstructions", "shopping"}.intersection(Path(context_path).parts)
        subtype = record.get("subtype", "")
        await asyncio.sleep(20 if subtype == "fixture-verifying-slow" else 2.5)
        result = copy.deepcopy(shopping)
        result["version"] = 2
        result["checked_candidates"] = []
        result["offers"] = []
        destination = Path(destination)
        destination.mkdir(parents=True, exist_ok=True)
        for index, offer in enumerate(shopping["offers"]):
            candidate = {
                "title": offer["title"],
                "url": offer["url"],
                "status": "accepted",
                "reasons": ["明確測試：原圖與商品主圖可見條件相符。"],
            }
            if subtype == "fixture-exclusions" and index > 0:
                candidate.update(
                    status="rejected",
                    reasons=[
                        "主色為藍色、袖長為長袖，與原圖紅色短袖不符。"
                        if index == 1
                        else "來源頁僅列美國地區，未符合台灣來源條件。"
                    ],
                )
            elif subtype == "fixture-all-rejected":
                candidate.update(
                    status="unverified" if index == 2 else "rejected",
                    reasons=[
                        (
                            "主色與袖長不符，不能列為相似款。",
                            "商品頁標示缺貨，已排除。",
                            "商品主圖無法讀取，無法比對領口與圖案。",
                        )[index]
                    ],
                )
            result["checked_candidates"].append(candidate)
            if candidate["status"] != "accepted":
                continue
            name = f"{index + 1}.jpg"
            (destination / name).write_bytes(photo())
            accepted = copy.deepcopy(offer)
            accepted["reason"] = "明確測試：商品主圖與原圖的六項可見條件相符；不是實際商品驗證。"
            accepted["verification"] = {
                "status": "verified",
                "page_url": offer["url"],
                "page_checked_at": datetime.now(UTC).isoformat(),
                "market": "in_scope",
                "market_evidence": (
                    "明確測試：來源頁列出台灣／TWD；未讀取真實商店。"
                    if market == "TW"
                    else "明確測試：不限地區的商品來源；未讀取真實商店。"
                ),
                "availability": "in_stock" if index == 0 else "unknown",
                "comparison": dict.fromkeys(
                    ("category", "primary_color", "pattern", "sleeve_length", "neckline", "design_details"),
                    "match",
                ),
                "image_url": name,
                "product_code": identity.get("product_code") if index == 0 else None,
            }
            result["offers"].append(accepted)
        result["status"] = "searched" if result["offers"] else "no_results"
        return result


def serve(directory, port):
    os.environ["REWEAR_DATA_DIR"] = str(directory)
    sys.path.insert(0, str(ROOT))
    import uvicorn
    from wardrobe.app import create_app
    from wardrobe.config import Settings

    uvicorn.run(
        create_app(
            Settings(data_dir=directory, api_key="explicit-fixture-not-a-credential"), DiscoveryFixtures()
        ),
        host="127.0.0.1",
        port=port,
        log_level="warning",
        access_log=False,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("out/verification/discovery-browser"))
    parser.add_argument(
        "--policy-dir", type=Path, default=Path.home() / ".agents/skills/webapp-testing/scripts"
    )
    parser.add_argument("--serve", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--data-dir", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--port", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.serve:
        serve(args.data_dir, args.port)
        return
    sys.path.insert(0, str(args.policy_dir))
    from network_policy import LocalNetworkPolicy, install_network_policy

    args.output.mkdir(parents=True, exist_ok=True)
    output = args.output.resolve()
    report = {
        "status": "running",
        "scenarios": [],
        "page_errors": [],
        "external_requests": [],
        "environment": "Temporary real API/SQLite; explicit brand/search/reconstruction fixtures; no real inference/search/retailer requests",
        "network": "loopback-only policy before pages; service workers blocked; unsafe API response injected only for frontend defense test",
    }
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    with tempfile.TemporaryDirectory(prefix="rewear-discovery-browser-") as temporary:
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", temporary),
            "LANG": "en_US.UTF-8",
            "REWEAR_DATA_DIR": temporary,
            "PYTHONUNBUFFERED": "1",
        }
        with (output / "server.log").open("w") as log:
            server = subprocess.Popen(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--serve",
                    "--data-dir",
                    temporary,
                    "--port",
                    str(port),
                ],
                cwd=ROOT,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            try:
                for _ in range(80):
                    try:
                        if httpx.get(url + "/api/health", timeout=1).status_code == 200:
                            break
                    except httpx.RequestError:
                        pass
                    if server.poll() is not None:
                        raise RuntimeError("Temporary API exited; see server.log")
                    time.sleep(0.2)
                else:
                    raise RuntimeError("Temporary API did not start")
                with httpx.Client(base_url=url, timeout=30) as client, sync_playwright() as p:
                    browser = p.chromium.launch(headless=True)
                    context = browser.new_context(
                        viewport={"width": 1440, "height": 1000}, service_workers="block"
                    )
                    install_network_policy(context, LocalNetworkPolicy.for_http(url))
                    page = context.new_page()
                    page.on("pageerror", lambda error: report["page_errors"].append(str(error)))
                    page.on(
                        "request",
                        lambda request: (
                            report["external_requests"].append(request.url)
                            if request.url.startswith(("http://", "https://"))
                            and not request.url.startswith(url + "/")
                            else None
                        ),
                    )
                    discovery_posts = []
                    page.on(
                        "request",
                        lambda request: (
                            discovery_posts.append(request.url)
                            if request.method == "POST" and request.url.endswith("/discovery")
                            else None
                        ),
                    )
                    page.set_default_timeout(15000)
                    expect.set_options(timeout=15000)
                    page.goto(url, wait_until="networkidle")
                    shared = {}

                    def screenshot(name):
                        full = not page.locator("dialog[open]").count()
                        if full:
                            page.evaluate("window.scrollTo({top:0,behavior:'instant'})")
                        page.screenshot(path=output / name, full_page=full, animations="disabled")

                    def check(name, function):
                        entry = {"name": name}
                        try:
                            entry.update(status="passed", details=function() or {})
                        except Exception as error:
                            entry.update(status="failed", error=str(error), traceback=traceback.format_exc())
                            screenshot(f"failure-{len(report['scenarios']) + 1}.png")
                            raise
                        finally:
                            report["scenarios"].append(entry)
                            (output / "report.json").write_text(
                                json.dumps(report, ensure_ascii=False, indent=2)
                            )
                            print(json.dumps(entry, ensure_ascii=False), flush=True)

                    def refresh():
                        page.locator('[data-view="history"]').click()
                        page.locator("#history-refresh").click()
                        expect(page.locator("#history-refresh")).to_be_enabled()
                        page.locator('[data-view="wardrobe"]').click()

                    def item_now(ident):
                        response = client.get(f"/api/items/{ident}")
                        response.raise_for_status()
                        return response.json()

                    def open_item(ident):
                        page.locator(f'[data-item-id="{ident}"]').click()
                        expect(page.locator("#item-dialog")).to_be_visible()

                    def change_record(ident, subtype):
                        response = client.patch(f"/api/items/{ident}", json={"record": {"subtype": subtype}})
                        response.raise_for_status()
                        refresh()
                        open_item(ident)
                        return response.json()

                    def start(ident, market="TW", keyboard=False):
                        page.locator("#discovery-market").select_option(market)
                        with page.expect_response(
                            lambda response: (
                                response.url == f"{url}/api/items/{ident}/discovery"
                                and response.request.method == "POST"
                            )
                        ) as result:
                            button = page.locator('[data-discovery-action="start"]')
                            if keyboard:
                                button.focus()
                                page.keyboard.press("Enter")
                            else:
                                button.click()
                        response = result.value
                        assert response.status == 202, response.text()
                        if keyboard:
                            expect(page.locator('[data-discovery-action="cancel"]')).to_be_focused()
                        return response.json()

                    def completed():
                        expect(page.locator(".discovery-status")).to_have_text("查找完成，請核對以下線索")

                    def manual_observed_and_draft():
                        page.locator('input[name="mode"][value="outfit"]').check()
                        # The default import defers shopping until the user
                        # requests it for an individual garment.
                        expect(page.locator("#upload-discover")).not_to_be_checked()
                        page.locator("#file-input").set_input_files(file_payload("explicit-brand-source.jpg"))
                        with page.expect_response(
                            lambda response: (
                                response.url == url + "/api/batches" and response.request.method == "POST"
                            )
                        ) as result:
                            page.locator("#upload-button").click()
                        assert result.value.status == 202, result.value.text()
                        batch = wait_batch(client, result.value.json()["id"])
                        item = next(
                            item
                            for item in client.get("/api/items").json()["items"]
                            if item["job_id"] == batch["jobs"][0]["id"]
                        )
                        ident = shared["item"] = item["id"]
                        assert item.get("discovery") is None
                        response = client.patch(
                            f"/api/items/{ident}", json={"record": {"brand": "原有品牌欄位，不能自動覆寫"}}
                        )
                        response.raise_for_status()
                        before = original_attributes(response.json())
                        expect(page.locator("#upload-button")).to_be_enabled()
                        refresh()
                        open_item(ident)
                        assert discovery_posts == [], discovery_posts
                        start(ident)
                        expect(page.locator("#discovery-market")).to_be_disabled()
                        expect(page.locator(".discovery-status")).to_contain_text("正在讀取原圖品牌線索")
                        page.locator("#item-brand").fill("查找期間的未儲存草稿 🧵")
                        page.locator("#item-defects").fill("不得被輪詢覆寫")
                        expect(page.locator(".discovery-status")).to_contain_text("正在查找購買候選")
                        expect(page.locator(".discovery-status")).to_contain_text(
                            "正在核對商品頁、圖片與地區"
                        )
                        completed()
                        expect(page.locator("#item-brand")).to_have_value("查找期間的未儲存草稿 🧵")
                        expect(page.locator("#item-defects")).to_have_value("不得被輪詢覆寫")
                        expect(page.locator("#item-defects")).to_be_focused()
                        after = item_now(ident)
                        shared["verified_result"] = copy.deepcopy(after["discovery"]["result"])
                        assert original_attributes(after) == before
                        assert after["discovery"]["result"]["market"] == "TW"
                        expect(page.locator(".brand-finding h4")).to_have_text(
                            "原圖可見標記 · EXPLICIT FIXTURE BRAND"
                        )
                        expect(page.locator(".brand-certainty")).to_contain_text("尚未驗證真偽")
                        expect(page.locator(".shopping-offers > li")).to_have_count(2)
                        expect(page.locator(".offer-match").first).to_have_text("型號候選")
                        expect(page.locator(".offer-match").nth(1)).to_have_text("相似款")
                        assert after["discovery"]["result"]["shopping"]["version"] == 2
                        expect(page.locator(".offer-product-image")).to_have_count(2)
                        page.wait_for_function(
                            "[...document.querySelectorAll('.offer-product-image')].every(img => img.complete && img.naturalWidth > 0)"
                        )
                        image_sources = page.locator(".offer-product-image").evaluate_all(
                            "nodes => nodes.map(img => img.src)"
                        )
                        assert all(
                            source.startswith(f"{url}/media/{after['job_id']}/shopping/")
                            for source in image_sources
                        ), image_sources
                        assert page.locator(".offer-comparison dd").all_text_contents() == ["相符"] * 12
                        expect(page.locator(".offer-availability").first).to_contain_text(
                            "查核時頁面標示有貨"
                        )
                        expect(page.locator(".offer-availability").nth(1)).to_contain_text("庫存未確認")
                        expect(page.locator(".offer-page-check").first).to_contain_text(
                            "不代表同款或真偽驗證"
                        )
                        expect(page.locator(".shopping-heading > span")).to_have_text("台灣")
                        expect(page.locator(".shopping-time")).not_to_contain_text("尚無")
                        # The legacy image-only fixture has no fidelity report;
                        # it must remain inspectable without becoming verified.
                        assert after["reconstruction"]["candidate_url"]
                        assert after["reconstruction"]["image_url"] is None
                        evidence = page.locator(".discovery-image-evidence img")
                        expect(evidence).to_have_attribute("src", url + after["original_url"])
                        assert "reconstructions" not in evidence.get_attribute("src")
                        page.locator("#item-discovery").scroll_into_view_if_needed()
                        screenshot("desktop-observed-purchase-candidates.png")
                        page.locator(".shopping-offers > li").first.scroll_into_view_if_needed()
                        screenshot("desktop-verified-product-comparison.png")
                        page.keyboard.press("Escape")
                        return {
                            "on_demand_only": True,
                            "three_stages": True,
                            "TW": True,
                            "draft_and_record_brand_preserved": True,
                            "original_not_reconstruction": True,
                            "verified_product_images": "two local JPEGs decoded; no retailer image requests",
                            "comparison": "six visible features, market and timestamped availability",
                        }

                    def legacy_results_require_recheck():
                        ident = shared["item"]
                        original = copy.deepcopy(item_now(ident)["discovery"])
                        posts_before = len(discovery_posts)
                        for version in (1, None):
                            payload = client.get("/api/items").json()
                            target = next(item for item in payload["items"] if item["id"] == ident)
                            shopping = target["discovery"]["result"]["shopping"]
                            if version is None:
                                shopping.pop("version", None)
                            else:
                                shopping["version"] = version

                            def legacy_items(route):
                                route.fulfill(status=200, json=payload)

                            context.route(url + "/api/items", legacy_items)
                            try:
                                refresh()
                                open_item(ident)
                                expect(page.locator(".shopping-legacy")).to_contain_text("請重新查找")
                                expect(page.locator(".shopping-offers > li")).to_have_count(0)
                                expect(page.locator(".offer-product-image")).to_have_count(0)
                                expect(page.locator(".discovery-web-sources")).to_have_count(0)
                                expect(page.locator(".brand-finding h4")).to_contain_text("原圖可見標記")
                                expect(page.locator(".discovery-search-links a")).to_have_count(1)
                                if version == 1:
                                    page.locator(".shopping-results").scroll_into_view_if_needed()
                                    screenshot("legacy-unverified-results-hidden.png")
                                page.keyboard.press("Escape")
                            finally:
                                context.unroute(url + "/api/items", legacy_items)
                        assert item_now(ident)["discovery"] == original
                        assert len(discovery_posts) == posts_before
                        refresh()
                        return {
                            "legacy_versions": [1, "missing"],
                            "unverified_cards_and_images": "hidden, explicit recheck guidance",
                            "brand_evidence_and_history": "preserved without a new lookup",
                        }

                    def global_possible_and_retained_market():
                        ident = shared["item"]
                        before = original_attributes(change_record(ident, "fixture-possible"))
                        start(ident, "global")
                        expect(page.locator(".discovery-previous").first).to_contain_text("保留上次")
                        expect(page.locator(".shopping-heading > span")).to_have_text("台灣")
                        expect(page.locator(".discovery-status")).to_contain_text("不限地區")
                        completed()
                        expect(page.locator(".brand-finding h4")).to_have_text(
                            "可能品牌 · EXPLICIT FIXTURE BRAND"
                        )
                        expect(page.locator(".brand-certainty")).to_contain_text("不會自動填入")
                        expect(page.locator(".shopping-heading > span")).to_have_text("不限地區")
                        assert page.locator(".offer-match").all_text_contents() == ["相似款", "相似款"]
                        assert original_attributes(item_now(ident)) == before
                        page.keyboard.press("Escape")
                        return {
                            "new_market": "global",
                            "previous_result_market_during_retry": "TW",
                            "possible_brand": "distinct from observed and saved brand",
                        }

                    def unknown_and_no_results():
                        ident = shared["item"]
                        before = original_attributes(change_record(ident, "fixture-unknown"))
                        start(ident)
                        completed()
                        expect(page.locator(".brand-finding h4")).to_have_text("無法確認品牌")
                        expect(page.locator(".brand-certainty")).to_contain_text(
                            "補拍清楚的 Logo、領標或洗標"
                        )
                        expect(page.locator(".shopping-offers > li")).to_have_count(0)
                        expect(page.locator(".discovery-search-links a")).to_have_count(1)
                        expect(page.locator(".discovery-search-links p")).to_have_text(
                            "以下是搜尋入口，不是商品頁。"
                        )
                        assert original_attributes(item_now(ident)) == before
                        page.locator("#item-discovery").scroll_into_view_if_needed()
                        screenshot("unknown-brand-no-results.png")
                        page.keyboard.press("Escape")
                        return {
                            "unknown": "no saved brand promoted to evidence",
                            "guidance": "Logo/labels",
                            "no_results": "Google search entry remains, not shown as a product",
                        }

                    def mismatched_candidates_excluded():
                        ident = shared["item"]
                        before = original_attributes(change_record(ident, "fixture-exclusions"))
                        start(ident)
                        completed()
                        expect(page.locator(".shopping-offers > li")).to_have_count(1)
                        expect(page.locator(".shopping-checks summary")).to_contain_text("2 項已排除")
                        assert not page.locator(".shopping-checks").evaluate("node => node.open")
                        page.locator(".shopping-checks summary").focus()
                        page.keyboard.press("Enter")
                        expect(page.locator(".shopping-checks li")).to_have_count(2)
                        expect(page.locator(".shopping-checks")).to_contain_text("主色為藍色、袖長為長袖")
                        expect(page.locator(".shopping-checks")).to_contain_text("未符合台灣來源條件")
                        assert page.locator(".shopping-offers a").get_attribute("href") == OFFER_A
                        assert original_attributes(item_now(ident)) == before
                        page.locator(".shopping-checks").scroll_into_view_if_needed()
                        screenshot("color-sleeve-market-exclusions.png")
                        page.keyboard.press("Escape")
                        return {
                            "accepted": 1,
                            "excluded": "color/sleeve mismatch and wrong market are not product cards",
                            "reasons": "collapsed initially, keyboard expandable",
                            "attributes": "unchanged",
                        }

                    def all_candidates_rejected_or_unverified():
                        ident = shared["item"]
                        before = original_attributes(change_record(ident, "fixture-all-rejected"))
                        start(ident)
                        completed()
                        expect(page.locator(".shopping-offers > li")).to_have_count(0)
                        expect(page.locator(".offer-product-image")).to_have_count(0)
                        expect(page.locator(".shopping-reminder")).to_contain_text("沒有通過核對")
                        expect(page.locator(".shopping-reminder")).to_contain_text(
                            "2 項已排除、1 項尚未完成核對"
                        )
                        expect(page.locator(".brand-finding h4")).to_contain_text("原圖可見標記")
                        expect(page.locator(".discovery-search-links a")).to_have_count(1)
                        assert page.locator(".shopping-checks").evaluate("node => node.open")
                        expect(page.locator(".shopping-checks")).to_contain_text("商品頁標示缺貨")
                        expect(page.locator(".shopping-checks")).to_contain_text("商品主圖無法讀取")
                        expect(page.locator(".shopping-checks li")).to_have_count(3)
                        expect(page.locator(".shopping-checks a").first).to_be_visible()
                        assert original_attributes(item_now(ident)) == before
                        page.locator(".shopping-results").scroll_into_view_if_needed()
                        screenshot("all-candidates-excluded-or-unverified.png")
                        page.keyboard.press("Escape")
                        return {
                            "offers": 0,
                            "rejected": 2,
                            "unverified": 1,
                            "empty_state": "explains mismatch, no stock and unavailable image",
                            "brand": "independent original evidence retained",
                        }

                    def failure_and_retry():
                        ident = shared["item"]
                        initial = change_record(ident, "fixture-identity-retry")
                        previous = copy.deepcopy(initial["discovery"]["result"])
                        before = original_attributes(initial)
                        start(ident, "global")
                        expect(page.locator(".discovery-status")).to_have_text("這次查找未能完成")
                        expect(page.locator("#item-discovery .inline-error")).to_contain_text("明確測試用")
                        assert item_now(ident)["discovery"]["result"] == previous
                        expect(page.locator(".discovery-previous").first).to_contain_text("保留上次")
                        start(ident, "global", keyboard=True)
                        completed()
                        assert original_attributes(item_now(ident)) == before
                        expect(page.locator("#item-discovery .inline-error")).to_have_count(0)
                        page.keyboard.press("Escape")
                        return {
                            "failure": "visible; previous result preserved",
                            "retry": "keyboard starts successful lookup",
                            "record_quality_reviewed": "unchanged",
                        }

                    def cancel_search():
                        ident = shared["item"]
                        initial = change_record(ident, "fixture-shopping-slow")
                        previous = copy.deepcopy(initial["discovery"]["result"])
                        before = original_attributes(initial)
                        start(ident, "TW", keyboard=True)
                        expect(page.locator(".discovery-status")).to_contain_text("正在查找購買候選")
                        with page.expect_response(
                            lambda response: response.url.endswith(f"/api/items/{ident}/discovery/cancel")
                        ) as result:
                            page.locator('[data-discovery-action="cancel"]').focus()
                            page.keyboard.press("Enter")
                        assert result.value.status == 200, result.value.text()
                        expect(page.locator(".discovery-status")).to_have_text("已取消這次查找")
                        expect(page.locator('[data-discovery-action="start"]')).to_be_focused()
                        after = item_now(ident)
                        assert after["discovery"]["result"] == previous
                        assert original_attributes(after) == before
                        expect(page.locator(".shopping-heading > span")).to_have_text("不限地區")
                        page.keyboard.press("Escape")
                        initial = change_record(ident, "fixture-verifying-slow")
                        before_verifying = original_attributes(initial)
                        previous = copy.deepcopy(initial["discovery"]["result"])
                        start(ident)
                        expect(page.locator(".discovery-status")).to_contain_text(
                            "正在核對商品頁、圖片與地區"
                        )
                        page.locator('[data-discovery-action="cancel"]').focus()
                        page.keyboard.press("Enter")
                        expect(page.locator(".discovery-status")).to_have_text("已取消這次查找")
                        expect(page.locator('[data-discovery-action="start"]')).to_be_focused()
                        cancelled = item_now(ident)
                        assert cancelled["discovery"]["result"] == previous
                        assert original_attributes(cancelled) == before_verifying
                        page.keyboard.press("Escape")
                        return {
                            "cancel_search_stage": True,
                            "cancel_verifying_stage": True,
                            "old_global_result_preserved_during_TW_attempt": True,
                            "keyboard_focus": "returned to retry",
                        }

                    def partial_search_error():
                        ident = shared["item"]
                        before = original_attributes(change_record(ident, "fixture-shopping-unavailable"))
                        start(ident)
                        expect(page.locator(".discovery-status")).to_have_text("已保留部分查找結果")
                        expect(page.locator(".brand-finding h4")).to_contain_text("原圖可見標記")
                        expect(page.locator("#item-discovery .inline-error")).to_contain_text("搜尋服務")
                        expect(page.locator(".shopping-reminder")).to_contain_text("未能完成商品核對")
                        expect(page.locator(".discovery-search-links a")).to_have_count(1)
                        assert original_attributes(item_now(ident)) == before
                        page.locator("#item-discovery").scroll_into_view_if_needed()
                        screenshot("partial-search-unavailable.png")
                        page.keyboard.press("Escape")
                        return {
                            "partial": "brand evidence and search entry survive search provider failure",
                            "attributes": "unchanged",
                        }

                    def generated_evidence_prohibited():
                        response = client.post(
                            "/api/generate",
                            json={
                                "prompt": "明確測試用衣物生成圖片，不是原始照片",
                                "provider": "local",
                                "mode": "single",
                            },
                        )
                        assert response.status_code == 202, response.text
                        ident = response.json()["id"]
                        deadline = time.monotonic() + 20
                        while time.monotonic() < deadline:
                            job = client.get(f"/api/jobs/{ident}").json()
                            if job["status"] not in {"queued", "running"}:
                                break
                            time.sleep(0.05)
                        assert job["status"] == "completed", job
                        item = next(
                            item
                            for item in client.get("/api/items").json()["items"]
                            if item["job_id"] == ident
                        )
                        refresh()
                        open_item(item["id"])
                        expect(page.locator('[data-discovery-action="start"]')).to_be_disabled()
                        expect(page.locator(".discovery-explanation")).to_contain_text(
                            "來自 AI 生成圖片，不能作為真實品牌證據"
                        )
                        page.keyboard.press("Escape")
                        return {
                            "source": "generated by explicit fixture, not a real model",
                            "lookup": "disabled, asks for original clothing photograph",
                        }

                    def safe_links_mobile_and_network_recovery():
                        ident = shared["item"]
                        payload = client.get("/api/items").json()
                        item = next(item for item in payload["items"] if item["id"] == ident)
                        injected = item["discovery"]["result"]
                        malicious_text = '<img src=x onerror="alert(1)"> 明確惡意文字測試 🧵'
                        invalid_urls = [
                            "javascript:alert(1)",
                            "http://localhost/store",
                            "https://127.0.0.1/store",
                            "https://192.168.1.1/",
                            "https://[::1]/",
                            "https://shop.internal/",
                            "https://shop.example/",
                            "https://u:p@www.nike.com/",
                            "file:///tmp/private",
                            "data:text/html,test",
                        ]
                        accepted = copy.deepcopy(shared["verified_result"]["shopping"]["offers"][0])
                        accepted.update(title=malicious_text, reason=malicious_text)
                        unsafe_offers = []
                        for href in invalid_urls:
                            unsafe = copy.deepcopy(accepted)
                            unsafe.update(title="unsafe fixture", url=href)
                            unsafe["verification"]["page_url"] = href
                            unsafe_offers.append(unsafe)
                        remote_image = copy.deepcopy(accepted)
                        remote_image.update(title="remote tracking-image fixture", url=OFFER_B)
                        remote_image["verification"].update(
                            page_url=OFFER_B,
                            image_url="https://www.adidas.com.tw/explicit-fixture-tracking.jpg",
                        )
                        injected["shopping"].update(
                            version=2,
                            status="searched",
                            offers=[accepted, *unsafe_offers, remote_image],
                            checked_candidates=[],
                            sources=[{"title": malicious_text, "url": OFFER_A, "snippet": malicious_text}]
                            + [
                                {"title": "unsafe", "url": href, "snippet": "unsafe"} for href in invalid_urls
                            ],
                        )
                        injected["search_links"] += [
                            {"label": "unsafe", "url": href} for href in invalid_urls
                        ]

                        def injected_items(route):
                            route.fulfill(status=200, json=payload)

                        context.route(url + "/api/items", injected_items)
                        try:
                            page.set_viewport_size({"width": 390, "height": 844})
                            refresh()
                            open_item(ident)
                            expect(page.locator(".shopping-offers > li")).to_have_count(1)
                            expect(page.locator(".shopping-offers a")).to_contain_text(malicious_text)
                            expect(page.locator(".offer-product-image")).to_have_count(1)
                            assert (
                                page.locator(".offer-product-image")
                                .get_attribute("src")
                                .startswith(f"{url}/media/{item['job_id']}/shopping/")
                            )
                            expect(page.locator("img[src=x]")).to_have_count(0)
                            expect(page.locator(".discovery-search-links a")).to_have_count(1)
                            page.locator(".discovery-web-sources summary").click()
                            expect(page.locator(".discovery-web-sources a")).to_have_count(1)
                            links = page.locator(".shopping-results a").evaluate_all(
                                "nodes => nodes.map(a=>({href:a.href,rel:a.rel,target:a.target}))"
                            )
                            assert len(links) == 3, links
                            assert all(
                                link["target"] == "_blank"
                                and "noopener" in link["rel"]
                                and "noreferrer" in link["rel"]
                                for link in links
                            )
                            assert all(
                                link["href"].startswith(("https://www.nike.com/", "https://www.google.com/"))
                                for link in links
                            ), links
                            no_overflow(page, "#item-dialog")
                            no_overflow(page)
                            page.locator(".shopping-results").scroll_into_view_if_needed()
                            screenshot("mobile-purchase-links-and-escaped-content.png")
                            page.keyboard.press("Escape")
                        finally:
                            context.unroute(url + "/api/items", injected_items)
                        refresh()
                        open_item(ident)
                        endpoint = f"{url}/api/items/{ident}/discovery"
                        context.route(endpoint, lambda route: route.abort("failed"), times=1)
                        page.locator('[data-discovery-action="start"]').click()
                        expect(page.locator("#item-discovery .inline-error")).to_contain_text("無法連線")
                        start(ident, "TW", keyboard=True)
                        expect(page.locator(".discovery-status")).to_have_text("已保留部分查找結果")
                        page.locator("#item-discovery").scroll_into_view_if_needed()
                        screenshot("mobile-discovery-controls.png")
                        page.keyboard.press("Escape")
                        health = client.get("/api/health").json()
                        health["discovery"]["available"] = False
                        context.route(
                            url + "/api/health", lambda route: route.fulfill(status=200, json=health), times=1
                        )
                        page.reload(wait_until="networkidle")
                        open_item(ident)
                        expect(page.locator('[data-discovery-action="start"]')).to_be_disabled()
                        expect(page.locator(".discovery-explanation")).to_contain_text("服務目前未就緒")
                        page.keyboard.press("Escape")
                        return {
                            "viewport": [390, 844],
                            "unsafe_urls_rejected": len(invalid_urls),
                            "remote_tracking_image": "candidate excluded; never requested",
                            "HTML": "escaped",
                            "links": "noopener noreferrer, external targets never opened",
                            "network_error": "visible; retry recovers",
                            "unavailable_health": "disabled with explanation",
                        }

                    try:
                        check(
                            "manual TW lookup, original evidence and editable draft preservation",
                            manual_observed_and_draft,
                        )
                        check(
                            "legacy results require recheck without changing history",
                            legacy_results_require_recheck,
                        )
                        check(
                            "global possible brand and accurate retained-result market",
                            global_possible_and_retained_market,
                        )
                        check("unknown brand and no-results search entry", unknown_and_no_results)
                        check(
                            "color/sleeve and market mismatches are excluded", mismatched_candidates_excluded
                        )
                        check(
                            "all candidates rejected or unverifiable", all_candidates_rejected_or_unverified
                        )
                        check("identity failure, preserved result and successful retry", failure_and_retry)
                        check("keyboard cancellation during shopping search", cancel_search)
                        check(
                            "partial result keeps identity when web search is unavailable",
                            partial_search_error,
                        )
                        check(
                            "generated original cannot be used as real brand evidence",
                            generated_evidence_prohibited,
                        )
                        check(
                            "safe escaped links, 390px and network recovery",
                            safe_links_mobile_and_network_recovery,
                        )
                    finally:
                        context.close()
                        browser.close()
            except Exception as error:
                report["error"] = str(error)
                report["traceback"] = traceback.format_exc()
            finally:
                server.terminate()
                try:
                    server.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait(timeout=5)
    failed = [entry for entry in report["scenarios"] if entry["status"] != "passed"]
    report["status"] = (
        "passed"
        if len(report["scenarios"]) == 11
        and not failed
        and not report["page_errors"]
        and not report["external_requests"]
        and "error" not in report
        else "failed"
    )
    report["summary"] = {
        "passed": len(report["scenarios"]) - len(failed),
        "failed": len(failed),
        "expected": 11,
    }
    report["temporary_server_stopped"] = True
    report["temporary_data_removed"] = True
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(
        json.dumps(
            {"status": report["status"], "summary": report["summary"], "report": str(output / "report.json")}
        ),
        flush=True,
    )
    if report["status"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()

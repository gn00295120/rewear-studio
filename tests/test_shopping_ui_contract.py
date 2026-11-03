"""Search telemetry stays visible without implying unverified pages are buyable."""

import copy
import json
import re
import subprocess

import pytest

from test_preview_ui_contract import fixture_item, shopping_result
from test_reconstruction_ui_contract import NODE, ROOT

pytestmark = pytest.mark.skipif(NODE is None, reason="The frontend contract needs a Node runtime")


def empty_shopping_result():
    result = shopping_result()
    result["shopping"].update(
        status="no_results",
        offers=[],
        search_attempts=[
            {
                "query": "台灣 藍色 短袖 上衣",
                "status": "searched",
                "retrieved_count": 7,
                "candidate_count": 2,
                "error": None,
            },
            {
                "query": "藍色 圓領 棉質 T恤 購買",
                "status": "searched",
                "retrieved_count": 9,
                "candidate_count": 3,
                "error": None,
            },
        ],
        checked_candidates=[
            {
                "title": "明確測試：袖長不同",
                "url": "https://www.nike.com/tw/t/explicit-rejected",
                "status": "rejected",
                "reasons": ["候選為長袖，原圖可見短袖，因此已排除。"],
            },
            {
                "title": "明確測試：商品頁無法讀取",
                "url": "https://www.uniqlo.com/tw/zh_TW/explicit-unverified",
                "status": "unverified",
                "reasons": ["商品頁回傳 403，尚未取得可核對的頁面與主圖。"],
            },
            {
                "title": "明確測試：沒有商品主圖",
                "url": "https://www.adidas.com.tw/explicit-no-image",
                "status": "unverified",
                "reasons": ["商品頁需要執行 JavaScript，尚未取得商品主圖。"],
            },
        ],
    )
    return result


PROBE = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('wardrobe/static/app.js', 'utf8');
const marker = '  document.addEventListener("click", event => {';
const boundary = source.indexOf(marker);
if (boundary < 0) throw new Error('UI event binding boundary changed.');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const context = { URL, window: { location: { origin: 'http://127.0.0.1:18500' } }, input };
const probe = `
  globalThis.result = {
    card: cardShopping(input),
    shopping: shoppingResults(input, input.discovery.result, 'TW'),
  };
})();`;
vm.runInNewContext(source.slice(0, boundary) + probe, context, { timeout: 2000 });
process.stdout.write(JSON.stringify(context.result));
"""


def rendered(result=None, *, status="completed"):
    item = fixture_item("shopping-telemetry")
    item["discovery"] = {
        "status": status,
        "stage": "searching",
        "market": "TW",
        "result": result or empty_shopping_result(),
    }
    command = subprocess.run(
        [NODE, "-e", PROBE],
        input=json.dumps(item, ensure_ascii=False),
        capture_output=True,
        text=True,
        check=True,
        timeout=5,
        cwd=ROOT,
    )
    return json.loads(command.stdout)


def test_two_real_queries_and_unverified_reasons_are_visible_without_offers():
    html = rendered()
    assert "2 searches" in html["card"]
    assert "1 rejected, 2 unverified" in html["card"]
    assert '<details class="shopping-checks" open>' in html["shopping"]
    assert "shopping-offers" not in html["shopping"]
    for attempt in empty_shopping_result()["shopping"]["search_attempts"]:
        assert attempt["query"] in html["shopping"]
    for candidate in empty_shopping_result()["shopping"]["checked_candidates"]:
        assert candidate["url"] in html["shopping"]
        assert candidate["reasons"][0] in html["shopping"]
    assert "Search history and candidate sources do not confirm purchase availability" in html["shopping"]
    assert 'class="offer-verified"' not in html["shopping"]


def test_failed_search_attempt_is_not_counted_as_completed():
    result = empty_shopping_result()
    result["shopping"]["search_attempts"][1].update(
        status="unavailable", retrieved_count=0, candidate_count=0, error="測試：搜尋暫時無法連線。"
    )
    html = rendered(result)
    assert "2 searches (1 incomplete)" in html["card"]
    assert html["card"].count("2 searches") == html["card"].count("2 searches (1 incomplete)")
    assert "Search not completed" in html["shopping"]
    assert "搜尋暫時無法連線" in html["shopping"]


def test_legacy_without_telemetry_does_not_invent_queries_or_show_old_offers():
    result = shopping_result()
    result["shopping"]["version"] = 1
    html = rendered(result)
    assert "searches" not in html["card"]
    assert 'aria-label="Search history"' not in html["shopping"]
    assert "Re-run the search" in html["shopping"]
    assert "shopping-offers" not in html["shopping"]


def test_retry_labels_search_telemetry_as_previous_result():
    html = rendered(status="running")
    assert "Searching" in html["card"]
    assert "Previous: 2 searches" in html["card"]


def test_unsafe_candidate_urls_and_query_markup_remain_inert():
    result = empty_shopping_result()
    result["shopping"]["search_attempts"][0]["query"] = '<img src=x onerror="alert(1)">'
    result["shopping"]["checked_candidates"][0]["url"] = "javascript:alert(1)"
    html = rendered(result)["shopping"]
    assert "<img src=x" not in html
    assert "&lt;img" in html
    assert 'href="javascript:' not in html
    assert "候選為長袖" in html
    assert all('rel="noopener noreferrer"' in tag for tag in re.findall(r"<a\b[^>]+>", html))


def test_verified_offer_remains_separate_from_rejected_candidates():
    result = empty_shopping_result()
    result["shopping"]["offers"] = copy.deepcopy(shopping_result()["shopping"]["offers"])
    html = rendered(result)
    assert "2 searches · 1 similar" in html["card"]
    assert '<details class="shopping-checks">' in html["shopping"]
    assert '<details class="shopping-checks" open>' not in html["shopping"]
    assert html["shopping"].count('class="offer-verified"') == 1

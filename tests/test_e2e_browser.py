"""Browser end-to-end tests: a real server, SQLite store and media pipeline driven by headless Chromium.

Model calls are scripted fixtures, so nothing reaches Meta. Uploads, the outfit
pipeline, batches, retries, polling and every screen are the shipped code.
"""

import base64
import copy
import re
import socket
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
import uvicorn
from PIL import Image, ImageDraw

sync_api = pytest.importorskip("playwright.sync_api")
expect, sync_playwright = sync_api.expect, sync_api.sync_playwright

from wardrobe.app import create_app  # noqa: E402
from wardrobe.config import Settings  # noqa: E402
from wardrobe.providers import ProviderError  # noqa: E402

BILLING_ERROR = "Meta SAM billing is not set up for this API key; resolve billing at dev.meta.ai, then retry."
JACKET = {
    "category": "outerwear",
    "subtype": "denim jacket",
    "brand": None,
    "size_label": None,
    "colors": ["blue"],
    "pattern": "solid",
    "material_guess": "denim",
    "condition": "unknown",
    "defects": [],
    "confidence": 0.9,
    "needs_review": True,
}
TROUSERS = {
    **JACKET,
    "category": "bottom",
    "subtype": "wide-leg trousers",
    "colors": ["black"],
    "material_guess": None,
}
PHONE = {
    "viewport": {"width": 390, "height": 844},
    "device_scale_factor": 2,
    "is_mobile": True,
    "has_touch": True,
}
DESKTOP = {"viewport": {"width": 1440, "height": 900}}
RESULT_TIMEOUT = 30_000


class ScriptedModels:
    """Deterministic SAM/Spark stand-ins: every photo holds a blue jacket above black trousers."""

    def __init__(self):
        self.segment_error = None
        self.inspect_error = None
        self.fragment = False
        self.duplicate = False

    async def health(self):
        return {
            "local": {"available": False, "model": "e2e-fixture", "error": None},
            "meta": {"configured": True},
        }

    async def close(self):
        pass

    async def segment(self, path, concept, video=False):
        if self.segment_error:
            raise ProviderError(self.segment_error)
        with Image.open(path) as image:
            width, height = image.size

        def box(object_id, left, top, right, bottom):
            return {
                "kind": "box",
                "object_id": object_id,
                "frame": None,
                "left": int(width * left),
                "top": int(height * top),
                "right": int(width * right),
                "bottom": int(height * bottom),
            }

        boxes = [box("jacket", 0.2, 0.22, 0.8, 0.64), box("trousers", 0.3, 0.66, 0.7, 0.95)]
        boxes += [box("jacket-again", 0.26, 0.26, 0.74, 0.6)] if self.duplicate else []
        return boxes + ([box("fragment", 0.03, 0.75, 0.17, 0.87)] if self.fragment else [])

    async def inspect(self, path, provider, *, context_path=None, wearing=False):
        if self.inspect_error:
            raise ProviderError(self.inspect_error)
        with Image.open(path) as crop:
            red, green, blue = crop.convert("RGB").resize((1, 1)).getpixel((0, 0))
        if green > red + 40 and green > blue + 40:
            return {
                "garment": copy.deepcopy(JACKET),
                "target": "one_garment",
                "visibility": "fragment",
                "reason": "Scripted fixture: only a scrap of fabric.",
            }
        jacket = blue > red + 25
        return {
            "garment": copy.deepcopy(JACKET if jacket else TROUSERS),
            "target": "one_garment",
            "visibility": "complete" if jacket else "partial",
            "reason": None if jacket else "Scripted fixture: the hem is cropped.",
        }

    async def extract(self, path, provider):
        return copy.deepcopy(JACKET)


class LiveServer:
    def __init__(self, app):
        self.socket = socket.socket()
        self.socket.bind(("127.0.0.1", 0))
        self.url = f"http://127.0.0.1:{self.socket.getsockname()[1]}"
        self.server = uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False))
        self.thread = threading.Thread(target=self.server.run, kwargs={"sockets": [self.socket]}, daemon=True)

    def __enter__(self):
        self.thread.start()
        deadline = time.monotonic() + 15
        while not self.server.started:
            if time.monotonic() > deadline or not self.thread.is_alive():
                raise RuntimeError("The end-to-end server did not start.")
            time.sleep(0.05)
        return self

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(timeout=15)
        self.socket.close()


@pytest.fixture(scope="module")
def playwright():
    with sync_playwright() as instance:
        yield instance


@pytest.fixture(scope="module")
def browser(playwright):
    try:
        launched = playwright.chromium.launch(headless=True)
    except Exception as error:
        pytest.skip(f"Chromium for Playwright is not installed: {error}")
    yield launched
    launched.close()


@pytest.fixture
def studio(tmp_path):
    models = ScriptedModels()
    app = create_app(Settings(data_dir=tmp_path / "data", api_key="e2e-fixture-no-network"), models)
    with LiveServer(app) as server:
        yield server.url, models


@pytest.fixture
def photos(tmp_path):
    def make(*names):
        paths = []
        for name in names:
            image = Image.new("RGB", (600, 800), "#ece8df")
            draw = ImageDraw.Draw(image)
            draw.ellipse((255, 60, 345, 160), fill="#c7a585")
            draw.rectangle((120, 176, 480, 512), fill=(35, 70, 140))
            draw.rectangle((180, 528, 420, 760), fill=(30, 30, 30))
            draw.rectangle((24, 604, 96, 692), fill=(40, 170, 60))
            path = tmp_path / name
            image.save(path, "JPEG", quality=92)
            paths.append(path)
        return paths

    return make


def open_page(browser, url, device=DESKTOP):
    context = browser.new_context(**device)
    page = context.new_page()
    page.errors = []
    page.on("pageerror", lambda error: page.errors.append(str(error)))
    page.on("console", lambda message: page.errors.append(message.text) if message.type == "error" else None)
    page.goto(url)
    expect(page.locator("#items-region")).to_have_attribute("aria-busy", "false")
    return context, page


def assert_clean(page):
    assert page.errors == []
    assert page.evaluate("document.documentElement.scrollWidth - window.innerWidth") == 0


def card_titles(page):
    return page.locator("#items-region .card-title").all_inner_texts()


def test_take_photo_finds_each_garment_and_links_straight_to_shopping(browser, studio, photos):
    url, _ = studio
    context, page = open_page(browser, url)
    expect(page.get_by_text("Upload a photo", exact=True)).to_be_visible()
    page.set_input_files("#camera-input", str(photos("street.jpg")[0]))

    banner = page.locator("#results-banner")
    expect(banner).to_contain_text("2 pieces found", timeout=RESULT_TIMEOUT)
    expect(banner).to_contain_text("street.jpg")
    assert sorted(card_titles(page)) == ["Denim jacket", "Wide-leg trousers"]
    expect(page.locator("#items-region .card-status")).to_have_count(0)

    shop = page.locator("#items-region .garment-card", has_text="Denim jacket").locator("a.card-shop")
    link = urlparse(shop.get_attribute("href"))
    assert (link.netloc, link.path) == ("www.google.com", "/search")
    query = parse_qs(link.query)
    assert query["tbm"] == ["shop"] and "denim jacket" in query["q"][0] and "blue" in query["q"][0]
    assert "gl" not in query
    assert shop.get_attribute("target") == "_blank" and "noopener" in shop.get_attribute("rel")

    page.locator("#items-region [data-item-id]", has_text="Denim jacket").click()
    dialog = page.locator("#item-dialog")
    expect(dialog).to_be_visible()
    expect(dialog.locator("#item-dialog-title")).to_have_text("Denim jacket")
    expect(dialog.get_by_role("link", name="Shop similar on Google")).to_be_visible()
    more = dialog.locator("details.item-more")
    assert more.get_attribute("open") is None
    dialog.locator("details.item-more > summary").click()
    assert more.get_attribute("open") is not None
    page.mouse.click(5, 5)
    expect(dialog).to_be_hidden()

    banner.get_by_role("button", name="All finds").click()
    expect(page.locator("#catalogue-heading")).to_have_text("Your finds")
    page.fill("#search-input", "trousers")
    assert card_titles(page) == ["Wide-leg trousers"]
    page.fill("#search-input", "")
    assert len(card_titles(page)) == 2
    assert_clean(page)
    context.close()


def test_bulk_import_analyzes_every_photo_and_lands_in_history(browser, studio, photos):
    url, _ = studio
    context, page = open_page(browser, url)
    page.set_input_files("#file-input", [str(path) for path in photos("a.jpg", "b.jpg", "c.jpg")])

    banner = page.locator("#results-banner")
    expect(banner).to_contain_text("6 pieces found", timeout=RESULT_TIMEOUT)
    expect(banner).to_contain_text("3 photos")
    assert len(card_titles(page)) == 6

    page.locator('[data-view="history"]').click()
    expect(page.locator("#history-view")).to_be_visible()
    expect(page.locator(".workspace-heading")).to_be_hidden()
    expect(page.locator("#batches-region")).to_contain_text("Outfit · 3 files")
    assert_clean(page)
    context.close()


def test_dropping_photos_on_the_page_starts_analysis(browser, studio, photos):
    url, _ = studio
    context, page = open_page(browser, url)
    payload = base64.b64encode(photos("dropped.jpg")[0].read_bytes()).decode()
    transfer = page.evaluate_handle(
        """payload => {
            const bytes = Uint8Array.from(atob(payload), character => character.charCodeAt(0));
            const transfer = new DataTransfer();
            transfer.items.add(new File([bytes], "dropped.jpg", { type: "image/jpeg" }));
            return transfer;
        }""",
        payload,
    )
    page.dispatch_event("#drop-zone", "drop", {"dataTransfer": transfer})
    expect(page.locator("#results-banner")).to_contain_text("2 pieces found", timeout=RESULT_TIMEOUT)
    assert_clean(page)
    context.close()


def test_service_error_is_explained_and_try_again_recovers(browser, studio, photos):
    url, models = studio
    models.segment_error = BILLING_ERROR
    context, page = open_page(browser, url)
    page.set_input_files("#camera-input", str(photos("street.jpg")[0]))

    banner = page.locator("#results-banner .results-banner.failed")
    expect(banner).to_contain_text("Analysis didn't finish", timeout=RESULT_TIMEOUT)
    expect(banner).to_contain_text("billing is not set up")
    expect(page.locator("body")).not_to_contain_text("better shot")

    models.segment_error = None
    banner.get_by_role("button", name="Try again").click()
    expect(page.locator("#results-banner")).to_contain_text("2 pieces found", timeout=RESULT_TIMEOUT)
    assert_clean(page)
    context.close()


def test_garments_that_could_not_be_read_stay_out_of_finds_until_retried(browser, studio, photos):
    url, models = studio
    models.inspect_error = BILLING_ERROR.replace("SAM", "Spark")
    context, page = open_page(browser, url)
    page.set_input_files("#camera-input", str(photos("street.jpg")[0]))

    expect(page.locator("#results-banner")).to_contain_text("Analysis didn't finish", timeout=RESULT_TIMEOUT)
    failed = page.locator("#items-region .garment-card")
    expect(failed).to_have_count(2)
    expect(failed.locator(".card-status")).to_have_text(["Analysis failed", "Analysis failed"])
    expect(failed.locator("a.card-shop")).to_have_count(0)

    failed.first.locator("[data-item-id]").click()
    dialog = page.locator("#item-dialog")
    expect(dialog).to_contain_text("This photo couldn't be analyzed")
    expect(dialog).to_contain_text("Meta Spark billing is not set up")
    expect(dialog).not_to_contain_text("better shot")
    page.keyboard.press("Escape")

    models.inspect_error = None
    failed.first.get_by_role("button", name="Try again").click()
    expect(page.locator("#results-banner")).to_contain_text("2 pieces found", timeout=RESULT_TIMEOUT)
    page.locator("#results-banner").get_by_role("button", name="All finds").click()
    assert sorted(card_titles(page)) == ["Denim jacket", "Wide-leg trousers"]
    assert_clean(page)
    context.close()


def test_unreadable_fragments_are_explained_but_kept_out_of_finds(browser, studio, photos):
    url, models = studio
    models.fragment = True
    context, page = open_page(browser, url)
    page.set_input_files("#camera-input", str(photos("street.jpg")[0]))

    banner = page.locator("#results-banner")
    expect(banner).to_contain_text("2 pieces found", timeout=RESULT_TIMEOUT)
    expect(banner).to_contain_text("1 area couldn't be identified")
    cards = page.locator("#items-region .garment-card")
    expect(cards).to_have_count(2)
    toggle = page.get_by_role("button", name="1 more area couldn't be identified")
    expect(toggle).to_have_attribute("aria-expanded", "false")
    toggle.click()
    expect(cards).to_have_count(3)
    expect(cards.nth(2).locator(".card-status")).to_have_text("Retake needed")
    expect(cards.nth(2).locator("a.card-shop")).to_have_count(0)
    expect(toggle).to_have_attribute("aria-expanded", "true")

    banner.get_by_role("button", name="All finds").click()
    assert sorted(card_titles(page)) == ["Denim jacket", "Wide-leg trousers"]
    assert_clean(page)
    context.close()


def test_a_garment_located_twice_in_one_photo_is_shown_once(browser, studio, photos):
    url, models = studio
    models.duplicate = True
    context, page = open_page(browser, url)
    page.set_input_files("#camera-input", str(photos("street.jpg")[0]))
    expect(page.locator("#results-banner")).to_contain_text("2 pieces found", timeout=RESULT_TIMEOUT)
    assert sorted(card_titles(page)) == ["Denim jacket", "Wide-leg trousers"]
    page.locator("#results-banner").get_by_role("button", name="All finds").click()
    assert sorted(card_titles(page)) == ["Denim jacket", "Wide-leg trousers"]
    assert_clean(page)
    context.close()


def test_sample_result_is_one_tap_from_the_empty_state(browser, studio):
    url, _ = studio
    context, page = open_page(browser, url)
    expect(page.get_by_text("Nothing here yet.", exact=True)).to_be_visible()
    expect(page.locator(".catalogue-tools")).to_be_hidden()
    page.get_by_role("button", name="See a sample result").click()
    expect(page.locator("#results-banner")).to_contain_text("pieces found", timeout=RESULT_TIMEOUT)
    assert len(card_titles(page)) >= 1
    expect(page.locator("#items-region a.card-shop").first).to_be_visible()
    assert_clean(page)
    context.close()


def test_cards_keep_their_place_when_the_grid_rerenders(browser, studio, photos):
    url, _ = studio
    context, page = open_page(browser, url)
    page.set_input_files("#camera-input", str(photos("street.jpg")[0]))
    expect(page.locator("#results-banner")).to_contain_text("2 pieces found", timeout=RESULT_TIMEOUT)
    page.locator("#results-banner").get_by_role("button", name="All finds").click()
    page.evaluate(
        "document.querySelectorAll('#items-region .garment-card').forEach(card => card.dataset.probe = 'kept')"
    )
    page.fill("#search-input", "e")
    page.fill("#search-input", "")
    kept = page.evaluate(
        "[...document.querySelectorAll('#items-region .garment-card')].map(card => [card.dataset.probe, card.classList.contains('is-settled')])"
    )
    assert kept == [["kept", True], ["kept", True]]
    context.close()


def test_phone_flow_uses_the_camera_a_bottom_sheet_and_a_floating_camera(browser, studio, photos):
    url, _ = studio
    context, page = open_page(browser, url, PHONE)
    assert "touch" in page.evaluate("document.documentElement.className")
    expect(page.get_by_text("Take a photo", exact=True)).to_be_visible()
    expect(page.get_by_text("Upload a photo", exact=True)).to_be_hidden()
    expect(page.locator(".file-picker")).to_be_hidden()
    assert page.locator("#camera-input").get_attribute("capture") == "environment"

    page.set_input_files("#camera-input", str(photos("snap.jpg")[0]))
    expect(page.locator("#results-banner")).to_contain_text("2 pieces found", timeout=RESULT_TIMEOUT)
    assert_clean(page)

    page.locator("#items-region [data-item-id]").first.click()
    dialog = page.locator("#item-dialog")
    expect(dialog).to_be_visible()
    page.wait_for_timeout(400)
    box = dialog.bounding_box()
    assert abs(box["y"] + box["height"] - 844) <= 2 and box["width"] == 390
    page.mouse.click(195, 20)
    expect(dialog).to_be_hidden()

    page.set_input_files("#file-input", [str(path) for path in photos("one.jpg", "two.jpg", "three.jpg")])
    expect(page.locator("#results-banner")).to_contain_text("6 pieces found", timeout=RESULT_TIMEOUT)
    fab = page.locator(".capture-fab")
    page.evaluate("window.scrollTo(0, 0)")
    expect(fab).to_be_hidden()
    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    expect(fab).to_be_visible()
    assert fab.get_attribute("for") == "camera-input"
    page.evaluate("window.scrollTo(0, 0)")
    expect(fab).to_be_hidden()
    assert_clean(page)
    context.close()


EXTENSION = Path(__file__).resolve().parents[1] / "extension"
FEED = """<!doctype html><meta charset="utf-8"><title>Feed</title>
<body style="margin:0;background:#fafafa;font-family:sans-serif">
<article style="width:480px;margin:40px auto;background:#fff">
  <header style="padding:12px">a friend</header>
  <div style="position:relative;width:480px;height:640px">
    <img src="photo.jpg" alt="" style="display:block;width:480px;height:640px">
    <div class="veil" style="position:absolute;inset:0"></div>
  </div>
  <p style="padding:12px">new fit</p>
</article>"""


@pytest.fixture
def feed(tmp_path, photos):
    folder = tmp_path / "feed"
    folder.mkdir()
    photos("photo.jpg")[0].rename(folder / "photo.jpg")
    (folder / "index.html").write_text(FEED, encoding="utf-8")
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(SimpleHTTPRequestHandler, directory=str(folder)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    # "localhost" is a different origin from the studio's 127.0.0.1, like a social site and Rewear.
    yield f"http://localhost:{server.server_address[1]}/"
    server.shutdown()
    server.server_close()


@pytest.fixture
def extension(playwright, tmp_path):
    try:
        context = playwright.chromium.launch_persistent_context(
            str(tmp_path / "profile"),
            channel="chromium",
            headless=True,
            viewport={"width": 1280, "height": 900},
            args=[f"--disable-extensions-except={EXTENSION}", f"--load-extension={EXTENSION}"],
        )
    except Exception as error:
        pytest.skip(f"Chromium with extension support is not installed: {error}")
    try:
        worker = context.service_workers[0] if context.service_workers else context.wait_for_event("serviceworker", timeout=10_000)
    except Exception:
        context.close()
        pytest.skip("This Chromium build does not run extensions headlessly.")
    yield context, worker
    context.close()


def hover_feed_photo(context, url):
    page = context.new_page()
    page.goto(url)
    box = page.locator("img").bounding_box()
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    pill = page.locator(".rewear-ext-pill")
    expect(pill).to_have_class(re.compile("rewear-ext-visible"))
    pill_box = pill.bounding_box()
    assert box["x"] < pill_box["x"] and pill_box["x"] + pill_box["width"] <= box["x"] + box["width"]
    assert box["y"] < pill_box["y"] < box["y"] + 60
    return page, pill


def test_extension_sends_a_feed_photo_to_rewear_and_finds_each_piece(extension, studio, feed):
    context, worker = extension
    url, _ = studio
    worker.evaluate("url => chrome.storage.local.set({ rewearUrl: url })", f"{url}/")
    page, pill = hover_feed_photo(context, feed)

    with context.expect_page() as opened:
        pill.click()
    rewear = opened.value
    rewear.errors = []
    rewear.on("pageerror", lambda error: rewear.errors.append(str(error)))
    banner = rewear.locator("#results-banner")
    expect(banner).to_contain_text("2 pieces found", timeout=RESULT_TIMEOUT)
    expect(banner).to_contain_text("photo-from-localhost.jpg")
    assert sorted(card_titles(rewear)) == ["Denim jacket", "Wide-leg trousers"]
    expect(page.locator(".rewear-ext-notice")).to_have_text("Opened in Rewear — finding every piece.")
    # The button never appears on Rewear itself, and the second photo reuses the open Rewear tab.
    rewear.mouse.move(400, 400)
    expect(rewear.locator(".rewear-ext-pill")).to_have_count(0)
    pages = len(context.pages)
    page.bring_to_front()
    page.mouse.move(5, 5)
    box = page.locator("img").bounding_box()
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 3)
    page.locator(".rewear-ext-pill").click()
    expect(rewear.locator("#results-banner")).to_contain_text("2 pieces found", timeout=RESULT_TIMEOUT)
    assert len(context.pages) == pages
    expect(rewear.locator('[data-view="history"] .nav-count')).to_have_text("2")
    assert rewear.errors == []


def test_extension_explains_when_rewear_is_not_running(extension, feed):
    context, worker = extension
    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    port = closed.getsockname()[1]
    closed.close()
    worker.evaluate("url => chrome.storage.local.set({ rewearUrl: url })", f"http://127.0.0.1:{port}/")
    page, pill = hover_feed_photo(context, feed)
    pages = len(context.pages)
    pill.click()
    notice = page.locator(".rewear-ext-notice")
    expect(notice).to_contain_text("Rewear isn't running", timeout=10_000)
    expect(notice).to_have_class(re.compile("rewear-ext-error"))
    assert len(context.pages) == pages

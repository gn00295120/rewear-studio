"""Record the extension scene with live Meta models: feed -> hover -> Shop this look -> Rewear analysis -> results -> previews -> detail.
Usage: record_ext.py <rewear_url> <feed_url> <extension_dir> <out_dir>"""
import json
import shutil
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

REWEAR, FEED, EXT, OUT = sys.argv[1], sys.argv[2], str(Path(sys.argv[3]).resolve()), Path(sys.argv[4])
OUT.mkdir(parents=True, exist_ok=True)
CURSOR = r"""
(() => {
  if (window.top !== window) return;
  const install = () => {
    if (document.getElementById('__demo_cursor')) return;
    const cursor = document.createElement('div');
    cursor.id = '__demo_cursor';
    cursor.style.cssText = 'position:fixed;left:0;top:0;width:30px;height:30px;z-index:2147483647;pointer-events:none;transform:translate(-200px,-200px);';
    cursor.innerHTML = '<svg width="30" height="30" viewBox="0 0 28 28"><path d="M5 3l15 12.5-6.6.6 3.9 8.1-3.1 1.5-3.9-8.2L5 22z" fill="#111" stroke="#fff" stroke-width="1.6" stroke-linejoin="round"/></svg>';
    document.documentElement.appendChild(cursor);
    const at = sessionStorage.getItem('__demo_cursor_at');
    if (at) cursor.style.transform = at;
    addEventListener('mousemove', event => {
      const value = `translate(${event.clientX - 5}px, ${event.clientY - 3}px)`;
      cursor.style.transform = value;
      sessionStorage.setItem('__demo_cursor_at', value);
    }, true);
    addEventListener('mousedown', event => {
      const dot = document.createElement('div');
      dot.style.cssText = `position:fixed;left:${event.clientX - 22}px;top:${event.clientY - 22}px;width:44px;height:44px;border-radius:50%;background:rgba(37,70,60,.18);border:2px solid rgba(37,70,60,.75);pointer-events:none;z-index:2147483646;transform:scale(.5);opacity:1;transition:transform .45s ease,opacity .6s ease .1s;`;
      document.documentElement.appendChild(dot);
      requestAnimationFrame(() => { dot.style.transform = 'scale(1.3)'; dot.style.opacity = '0'; });
      setTimeout(() => dot.remove(), 900);
    }, true);
  };
  document.readyState === 'loading' ? addEventListener('DOMContentLoaded', install) : install();
})();
"""
events, position = {}, [1240.0, 520.0]


def mark(name):
    events[name] = round(time.monotonic() - t0, 2)
    print(f"{events[name]:7.2f}s  {name}", flush=True)


def glide(page, x, y, steps=24, pause=12):
    sx, sy = position
    for i in range(1, steps + 1):
        p = i / steps
        e = p * p * (3 - 2 * p)
        page.mouse.move(sx + (x - sx) * e, sy + (y - sy) * e)
        page.wait_for_timeout(pause)
    position[:] = [x, y]


with sync_playwright() as playwright:
    context = playwright.chromium.launch_persistent_context(
        str(OUT / "profile"), channel="chromium", headless=True, locale="en-US",
        viewport={"width": 1600, "height": 900}, device_scale_factor=1,
        record_video_dir=str(OUT / "raw"), record_video_size={"width": 1600, "height": 900},
        args=[f"--disable-extensions-except={EXT}", f"--load-extension={EXT}", "--host-resolver-rules=MAP feed.example 127.0.0.1"],
    )
    worker = context.service_workers[0] if context.service_workers else context.wait_for_event("serviceworker", timeout=15000)
    worker.evaluate("url => chrome.storage.local.set({ rewearUrl: url })", REWEAR)
    context.add_init_script(CURSOR)
    page = context.pages[0] if context.pages else context.new_page()
    t0 = time.monotonic()
    page.goto(FEED, wait_until="networkidle")
    page.mouse.move(*position)
    page.wait_for_timeout(2200)
    mark("feed")
    top = page.evaluate("document.querySelector('#post-2').getBoundingClientRect().top + scrollY - 72")
    page.evaluate(f"window.scrollTo({{top: {top}, behavior: 'smooth'}})")
    page.wait_for_timeout(1700)
    mark("scrolled")
    box = page.locator("#post-2 img").bounding_box()
    glide(page, box["x"] + box["width"] * 0.42, box["y"] + box["height"] * 0.6, 34, 14)
    page.wait_for_timeout(1300)
    mark("pill")
    pill = page.locator(".rewear-ext-pill").bounding_box()
    glide(page, pill["x"] + pill["width"] * 0.55, pill["y"] + pill["height"] * 0.5, 22, 14)
    page.wait_for_timeout(600)
    mark("click")
    with context.expect_page() as opened:
        page.mouse.down()
        page.wait_for_timeout(90)
        page.mouse.up()
    rewear = opened.value
    mark("rewear_open")
    rewear.wait_for_load_state("domcontentloaded")
    rewear.mouse.move(*position)
    rewear.wait_for_selector("#active-batch:not([hidden])", timeout=60000)
    mark("scanning")
    glide(rewear, 900, 520, 30, 16)
    rewear.wait_for_selector("#results-banner:not([hidden])", timeout=900000)
    mark("results")
    rewear.wait_for_timeout(1500)
    top = rewear.evaluate("document.querySelector('#results-banner').getBoundingClientRect().top + scrollY - 96")
    rewear.evaluate(f"window.scrollTo({{top: {top}, behavior: 'smooth'}})")
    rewear.wait_for_timeout(2600)
    mark("results_hold_end")
    deadline = time.monotonic() + 900
    while time.monotonic() < deadline:
        busy = rewear.evaluate("[...document.querySelectorAll('#items-region .card-reconstruction-progress')].some(n => !n.classList.contains('failed'))")
        if not busy:
            break
        rewear.wait_for_timeout(2000)
    rewear.wait_for_timeout(2000)
    mark("previews_done")
    banner = rewear.locator("#results-banner").inner_text().replace("\n", " | ")
    rewear.wait_for_timeout(600)
    mark("scrolled_results")
    card = rewear.locator("#items-region .garment-card .card-button").first.bounding_box()
    glide(rewear, card["x"] + card["width"] / 2, card["y"] + card["height"] * 0.45, 26, 16)
    rewear.wait_for_timeout(700)
    rewear.mouse.down()
    rewear.wait_for_timeout(90)
    rewear.mouse.up()
    rewear.wait_for_timeout(3200)
    mark("detail_open")
    shop = rewear.locator("#item-dialog a", has_text="Shop similar on Google").bounding_box()
    if shop:
        glide(rewear, shop["x"] + shop["width"] / 2, shop["y"] + shop["height"] / 2, 22, 16)
    rewear.wait_for_timeout(1800)
    mark("detail_end")
    items = rewear.evaluate("[...document.querySelectorAll('#items-region .garment-card')].map(c => c.innerText.split('\\n').slice(0, 3).join(' / '))")
    context.close()
    shutil.copy(page.video.path(), OUT / "feed.webm")
    shutil.copy(rewear.video.path(), OUT / "rewear.webm")

(OUT / "events.json").write_text(json.dumps({"events": events, "banner": banner, "items": items}, indent=2))
print("banner:", banner)
print("items:", json.dumps(items, indent=1))

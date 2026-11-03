// Rewear — Shop this look. Sends the one photo the user clicks to their own Rewear Studio and nothing else:
// no background scanning, no page addresses, no account data.
const DEFAULT_URL = "http://127.0.0.1:8765/";
const MIN_SIDE = 200;
const MAX_SIDE = 3072;
const KEEP_TYPES = new Set(["image/jpeg", "image/png", "image/webp"]);
const EXTENSIONS = { "image/jpeg": "jpg", "image/png": "png", "image/webp": "webp" };

async function rewearBase() {
  const { rewearUrl } = await chrome.storage.local.get("rewearUrl");
  const url = new URL(rewearUrl || DEFAULT_URL);
  url.hash = "";
  url.search = "";
  if (!url.pathname.endsWith("/")) url.pathname += "/";
  return url;
}

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.create({ id: "rewear-shop", title: "Shop this look with Rewear", contexts: ["image"] });
});

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  if (info.menuItemId !== "rewear-shop" || !info.srcUrl) return;
  const result = await shop({ src: info.srcUrl, host: hostOf(info.pageUrl) }, tab);
  if (!result.ok && tab?.id !== undefined) {
    chrome.tabs.sendMessage(tab.id, { type: "rewear:status", text: result.error, error: true }).catch(() => {});
  }
});

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message?.type !== "rewear:shop" || !sender.tab) return false;
  shop(message, sender.tab).then(sendResponse);
  return true;
});

async function shop(request, sourceTab) {
  try {
    const base = await rewearBase();
    await ensureRunning(base);
    const photo = await grab(request, sourceTab);
    const tab = await openRewear(base, sourceTab);
    if (tab.url && new URL(tab.url).pathname.endsWith("/login")) {
      throw new Error("Sign in to Rewear in the tab that just opened, then click Shop this look again.");
    }
    const reply = await deliver(tab.id, { ...photo, source: request.host || "the web" });
    if (!reply?.ok) throw new Error(reply?.reason || "Rewear didn't accept the photo. Reload the Rewear tab and try again.");
    return { ok: true };
  } catch (error) {
    return { ok: false, error: error?.message || String(error) };
  }
}

async function ensureRunning(base) {
  // Any HTTP answer means Rewear is reachable; a hosted site answers 401 until you sign in, which the tab handles.
  try {
    await fetch(new URL("api/health", base), { signal: AbortSignal.timeout(5000) });
    return;
  } catch {}
  throw new Error(`Rewear isn't reachable at ${base.host}. Start it with "uv run rewear" or check the address in the Rewear popup.`);
}

async function grab({ src, rect, view }, tab) {
  let blob = null;
  if (typeof src === "string" && /^(https?:|data:image\/)/i.test(src)) {
    try {
      const response = await fetch(src, { credentials: "omit", signal: AbortSignal.timeout(15000) });
      if (response.ok) blob = await response.blob();
    } catch {}
  }
  if (blob && !blob.type.startsWith("image/")) blob = null;
  let bitmap = blob && (await createImageBitmap(blob).catch(() => null));
  if (!bitmap && rect && tab) {
    // Fall back to what is on screen when the file itself can't be fetched.
    blob = await captureRegion(tab, rect, view);
    bitmap = blob && (await createImageBitmap(blob).catch(() => null));
  }
  if (!bitmap) throw new Error("This photo can't be read. Open it at full size and try again.");
  if (Math.min(bitmap.width, bitmap.height) < MIN_SIDE) throw new Error("This photo is too small to find the pieces in it.");
  const keep = KEEP_TYPES.has(blob.type) && Math.max(bitmap.width, bitmap.height) <= MAX_SIDE && blob.size <= 15 * 1024 * 1024;
  const output = keep ? blob : await reencode(bitmap);
  bitmap.close();
  return { dataUrl: await dataUrlOf(output), type: output.type };
}

async function captureRegion(tab, rect, view) {
  const shot = await chrome.tabs.captureVisibleTab(tab.windowId, { format: "png" }).catch(() => null);
  if (!shot) return null;
  const screen = await createImageBitmap(await (await fetch(shot)).blob());
  const scale = screen.width / Math.max(1, view?.width || screen.width);
  const left = Math.max(0, Math.round(rect.x * scale));
  const top = Math.max(0, Math.round(rect.y * scale));
  const width = Math.min(screen.width - left, Math.round(rect.width * scale));
  const height = Math.min(screen.height - top, Math.round(rect.height * scale));
  if (width < 1 || height < 1) return null;
  const canvas = new OffscreenCanvas(width, height);
  canvas.getContext("2d").drawImage(screen, left, top, width, height, 0, 0, width, height);
  screen.close();
  return canvas.convertToBlob({ type: "image/jpeg", quality: 0.92 });
}

async function reencode(bitmap) {
  const scale = Math.min(1, MAX_SIDE / Math.max(bitmap.width, bitmap.height));
  const canvas = new OffscreenCanvas(Math.round(bitmap.width * scale), Math.round(bitmap.height * scale));
  const context = canvas.getContext("2d");
  context.fillStyle = "#fff";
  context.fillRect(0, 0, canvas.width, canvas.height);
  context.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
  return canvas.convertToBlob({ type: "image/jpeg", quality: 0.92 });
}

async function dataUrlOf(blob) {
  const bytes = new Uint8Array(await blob.arrayBuffer());
  let binary = "";
  for (let index = 0; index < bytes.length; index += 0x8000) binary += String.fromCharCode(...bytes.subarray(index, index + 0x8000));
  return `data:${blob.type};base64,${btoa(binary)}`;
}

async function openRewear(base, sourceTab) {
  const open = (await chrome.tabs.query({})).find(tab => tab.url && tab.url.startsWith(base.href));
  if (open) {
    await chrome.tabs.update(open.id, { active: true });
    await chrome.windows.update(open.windowId, { focused: true }).catch(() => {});
    return waitForLoad(open.id);
  }
  const created = await chrome.tabs.create({
    url: base.href,
    windowId: sourceTab?.windowId,
    index: sourceTab ? sourceTab.index + 1 : undefined,
    openerTabId: sourceTab?.id,
  });
  return waitForLoad(created.id);
}

function waitForLoad(tabId) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => finish(new Error("Rewear took too long to open.")), 20000);
    const listener = (id, info, tab) => {
      if (id === tabId && info.status === "complete") finish(null, tab);
    };
    function finish(error, tab) {
      clearTimeout(timer);
      chrome.tabs.onUpdated.removeListener(listener);
      error ? reject(error) : resolve(tab);
    }
    chrome.tabs.onUpdated.addListener(listener);
    chrome.tabs.get(tabId).then(tab => tab.status === "complete" && finish(null, tab), finish);
  });
}

async function deliver(tabId, photo) {
  await chrome.scripting.executeScript({ target: { tabId }, files: ["bridge.js"] });
  const name = `photo-from-${photo.source.replace(/^www\./, "").replace(/[^a-z0-9.-]/gi, "") || "web"}.${EXTENSIONS[photo.type] || "jpg"}`;
  return chrome.tabs.sendMessage(tabId, { type: "rewear:deliver", dataUrl: photo.dataUrl, name, source: photo.source });
}

function hostOf(url) {
  try {
    return new URL(url).hostname;
  } catch {
    return "";
  }
}

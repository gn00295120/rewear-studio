// Shows a "Shop this look" button over the photo under the mouse. Nothing happens until the user clicks it.
(() => {
  if (window.top !== window || globalThis.__rewearShop) return;
  globalThis.__rewearShop = true;
  const DEFAULT_URL = "http://127.0.0.1:8765/";
  const MIN_SIDE = 160;
  const SVG = "http://www.w3.org/2000/svg";
  let pill = null;
  let notice = null;
  let target = null;
  let point = null;
  let frame = 0;
  let hideTimer = 0;
  let noticeTimer = 0;
  let busy = false;

  chrome.storage.local.get("rewearUrl").then(({ rewearUrl }) => {
    let origin = "";
    try {
      origin = new URL(rewearUrl || DEFAULT_URL).origin;
    } catch {}
    if (location.origin !== origin) start();
  });

  function start() {
    pill = document.createElement("button");
    pill.type = "button";
    pill.className = "rewear-ext-pill";
    pill.setAttribute("aria-label", "Shop this look with Rewear");
    pill.append(hanger(), Object.assign(document.createElement("span"), { textContent: "Shop this look" }));
    notice = document.createElement("div");
    notice.className = "rewear-ext-notice";
    notice.setAttribute("role", "status");
    document.documentElement.append(pill, notice);

    for (const type of ["pointerdown", "mousedown", "pointerup", "mouseup", "touchstart", "dblclick"]) {
      pill.addEventListener(type, event => event.stopPropagation(), true);
    }
    pill.addEventListener("click", send, true);
    addEventListener("pointermove", event => {
      if (event.pointerType === "touch") return;
      point = [event.clientX, event.clientY];
      if (!frame) frame = requestAnimationFrame(update);
    }, { capture: true, passive: true });
    addEventListener("scroll", () => target && place(target), { capture: true, passive: true });
    addEventListener("resize", () => target && place(target), { passive: true });
    chrome.runtime.onMessage.addListener(message => {
      if (message?.type === "rewear:status") say(message.text, message.error);
    });
  }

  function hanger() {
    const icon = document.createElementNS(SVG, "svg");
    icon.setAttribute("viewBox", "0 0 24 24");
    icon.setAttribute("aria-hidden", "true");
    const path = document.createElementNS(SVG, "path");
    path.setAttribute("d", "M9 6a3 3 0 1 1 4 2.8V11l8 5a1 1 0 0 1-.5 2H3.5a1 1 0 0 1-.5-2l10-6");
    icon.append(path);
    return icon;
  }

  function update() {
    frame = 0;
    if (busy || !point) return;
    const [x, y] = point;
    const top = document.elementFromPoint(x, y);
    if (top && (top === pill || pill.contains(top))) return keep();
    const image = imageAt(x, y);
    if (!image) return hideSoon();
    keep();
    target = image;
    place(image);
    pill.classList.add("rewear-ext-visible");
  }

  function imageAt(x, y) {
    const stack = document.elementsFromPoint(x, y).filter(node => node !== pill && !pill.contains(node));
    for (const node of stack.slice(0, 8)) {
      if (node instanceof HTMLImageElement) {
        if (usable(node, x, y)) return node;
        continue;
      }
      // Feeds often lay a transparent layer over the photo; look beside it for the image underneath.
      for (const image of [...(node.parentElement?.querySelectorAll("img") || [])].slice(0, 12)) {
        if (usable(image, x, y)) return image;
      }
    }
    return null;
  }

  function usable(image, x, y) {
    const box = image.getBoundingClientRect();
    return box.width >= MIN_SIDE && box.height >= MIN_SIDE && x >= box.left && x <= box.right && y >= box.top && y <= box.bottom
      && image.complete && image.naturalWidth >= 200 && image.naturalHeight >= 200 && Boolean(image.currentSrc || image.src);
  }

  function place(image) {
    const box = image.getBoundingClientRect();
    if (box.bottom < 0 || box.top > innerHeight || !image.isConnected) return hide();
    const width = pill.offsetWidth || 150;
    const height = pill.offsetHeight || 38;
    const left = Math.min(Math.max(box.right - width - 14, box.left + 10, 8), innerWidth - width - 8);
    const top = Math.min(Math.max(box.top + 14, 8), innerHeight - height - 8);
    pill.style.setProperty("transform", `translate(${Math.round(left)}px, ${Math.round(top)}px)`, "important");
  }

  function keep() {
    clearTimeout(hideTimer);
    hideTimer = 0;
  }

  function hideSoon() {
    if (!hideTimer) hideTimer = setTimeout(hide, 350);
  }

  function hide() {
    keep();
    target = null;
    pill.classList.remove("rewear-ext-visible");
  }

  async function send(event) {
    event.preventDefault();
    event.stopPropagation();
    event.stopImmediatePropagation();
    const image = target;
    if (busy || !image) return;
    const box = image.getBoundingClientRect();
    busy = true;
    hide();
    say("Sending this photo to Rewear…");
    let reply;
    try {
      reply = await chrome.runtime.sendMessage({
        type: "rewear:shop",
        src: image.currentSrc || image.src,
        rect: { x: box.left, y: box.top, width: box.width, height: box.height },
        view: { width: innerWidth, height: innerHeight },
        host: location.hostname,
      });
    } catch {
      reply = { ok: false, error: "Rewear was updated. Reload this page to keep using it." };
    }
    busy = false;
    say(reply?.ok ? "Opened in Rewear — finding every piece." : reply?.error || "Couldn't send this photo.", !reply?.ok);
  }

  function say(text, error = false) {
    clearTimeout(noticeTimer);
    notice.textContent = text;
    notice.classList.toggle("rewear-ext-error", error);
    notice.classList.add("rewear-ext-visible");
    noticeTimer = setTimeout(() => notice.classList.remove("rewear-ext-visible"), error ? 7000 : 3500);
  }
})();

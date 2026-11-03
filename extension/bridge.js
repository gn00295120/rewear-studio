// Runs in the Rewear Studio tab: hands the photo to the page, which uploads it like any photo the user picks.
if (!globalThis.__rewearBridge) {
  globalThis.__rewearBridge = true;
  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (message?.type !== "rewear:deliver") return false;
    const id = crypto.randomUUID();
    const timer = setTimeout(() => finish({ ok: false, reason: "Rewear didn't respond. Reload the Rewear tab and try again." }), 8000);
    function onReply(event) {
      if (event.source !== window || event.origin !== location.origin) return;
      if (event.data?.type !== "rewear:import-result" || event.data.id !== id) return;
      finish({ ok: event.data.ok === true, reason: event.data.reason || "" });
    }
    function finish(result) {
      clearTimeout(timer);
      window.removeEventListener("message", onReply);
      sendResponse(result);
    }
    window.addEventListener("message", onReply);
    window.postMessage({ type: "rewear:import", id, dataUrl: message.dataUrl, name: message.name, source: message.source }, location.origin);
    return true;
  });
}

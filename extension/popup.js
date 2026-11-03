const DEFAULT_URL = "http://127.0.0.1:8765/";
const input = document.getElementById("url");
const status = document.getElementById("status");

function normalized(value) {
  const url = new URL(value);
  if (!/^https?:$/.test(url.protocol)) throw new Error("Use an http:// or https:// address.");
  url.hash = "";
  url.search = "";
  if (!url.pathname.endsWith("/")) url.pathname += "/";
  return url.href;
}

function show(text, kind = "") {
  status.textContent = text;
  status.className = `status ${kind}`;
}

async function check(base) {
  show("Checking Rewear…");
  try {
    const response = await fetch(new URL("api/health", base), { signal: AbortSignal.timeout(5000) });
    if (!response.ok) throw new Error(String(response.status));
    const health = await response.json();
    const ready = health?.meta?.configured;
    show(ready ? "Connected · Meta AI is ready." : "Connected, but no Meta API key is set in Rewear.", ready ? "ok" : "error");
  } catch {
    show(`Rewear isn't running at ${new URL(base).host}. Start it with "uv run rewear".`, "error");
  }
}

async function load() {
  const { rewearUrl } = await chrome.storage.local.get("rewearUrl");
  input.value = rewearUrl || DEFAULT_URL;
  check(input.value);
}

document.getElementById("settings").addEventListener("submit", async event => {
  event.preventDefault();
  try {
    const value = normalized(input.value.trim());
    await chrome.storage.local.set({ rewearUrl: value });
    input.value = value;
    check(value);
  } catch (error) {
    show(error.message || "That address isn't valid.", "error");
  }
});

document.getElementById("open").addEventListener("click", async () => {
  const { rewearUrl } = await chrome.storage.local.get("rewearUrl");
  await chrome.tabs.create({ url: rewearUrl || DEFAULT_URL });
  window.close();
});

load();

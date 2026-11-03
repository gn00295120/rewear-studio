"use strict";

(() => {
  const $ = selector => document.querySelector(selector);
  const $$ = selector => [...document.querySelectorAll(selector)];
  const state = { csrf: null, user: null, mode: null, managed: false, busy: false, recovery: null, recoveryKind: null, username: "", downloadURL: null };
  const channel = typeof BroadcastChannel === "function" ? new BroadcastChannel("rewear-session") : null;
  const announce = () => channel?.postMessage("session-changed");
  const error = (id, message) => { $(id).textContent = message || ""; $(id).hidden = !message; };
  const usernameError = value => /^[A-Za-z0-9._@+\-]{3,80}$/.test(value) ? "" : "Username must be 3–80 characters: letters, digits, or . _ @ + -.";
  const passwordError = value => [...value].length >= 12 && [...value].length <= 128 ? "" : "Password must be 12–128 characters. Your input will not be truncated.";

  async function request(path, body) {
    if (body !== undefined) {
      const session = await request("/auth/session");
      if (session.mode !== "hosted") throw new Error("Account mode is not active. Please reopen the wardrobe.");
      state.csrf = session.authenticated ? session.csrf_token : null;
    }
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 30000);
    const headers = new Headers();
    if (body !== undefined) {
      headers.set("Content-Type", "application/json");
      headers.set("X-Rewear-Request", "1");
      if (state.csrf) headers.set("X-CSRF-Token", state.csrf);
    }
    try {
      const response = await fetch(path, { method: body === undefined ? "GET" : "POST", credentials: "same-origin", cache: "no-store", headers, ...(body === undefined ? {} : { body: JSON.stringify(body) }), signal: controller.signal });
      const data = await response.json().catch(() => null);
      if (!response.ok) {
        let message = typeof data?.detail === "string" ? data.detail : "The account service could not complete your request.";
        if (response.status === 429) message += " Please wait a moment before trying again.";
        throw new Error(message);
      }
      if (!data) throw new Error("The account service returned unreadable data. Please try again.");
      return data;
    } catch (problem) {
      if (problem.name === "AbortError") throw new Error("The account service timed out. Please try again later.");
      if (problem instanceof TypeError) throw new Error("Unable to connect to the account service. Please check your connection and try again.");
      throw problem;
    } finally { clearTimeout(timeout); }
  }

  function setTab(name, focus = false) {
    if (state.busy || state.recovery) return;
    if (!["login", "register", "recover"].includes(name) || (state.managed && name !== "login")) name = "login";
    $$('[data-auth-tab]').forEach(button => {
      const selected = button.dataset.authTab === name;
      button.setAttribute("aria-selected", String(selected));
      button.tabIndex = selected ? 0 : -1;
      $(`#panel-${button.dataset.authTab}`).hidden = !selected;
    });
    document.title = `${{ login: "Sign In", register: "Create Account", recover: "Reset Password" }[name]} — Rewear Studio`;
    if (focus) $(`#tab-${name}`).focus();
  }

  async function initialize() {
    $("#auth-loading").hidden = false;
    $("#auth-content").hidden = true;
    $("#auth-recheck").hidden = true;
    error("#auth-bootstrap-error", "");
    try {
      const session = await request("/auth/session");
      if (!["local", "hosted"].includes(session.mode)) throw new Error("Unable to determine account mode. Please try again later.");
      state.mode = session.mode;
      state.managed = session.accounts === "managed";
      // Managed sites hand out logins (for example to judges); there is no sign-up or self-service reset.
      ["register", "recover"].forEach(name => { $(`#tab-${name}`).hidden = state.managed; });
      $("#auth-managed-note").hidden = !state.managed;
      if (state.managed) {
        $("#username-help").textContent = "Use the username you were given.";
        const privacy = $(".auth-privacy"); if (privacy) privacy.textContent = "This demo account is managed by the organizers; the password cannot be changed or reset from this site.";
      }
      if (state.managed) setTab("login");
      state.user = session.authenticated ? session.user : null;
      state.csrf = session.authenticated ? session.csrf_token : null;
      $("#auth-local").hidden = session.mode !== "local";
      $("#auth-content").hidden = session.mode !== "hosted";
      if (state.user) {
        $("#auth-session-info").replaceChildren(document.createTextNode(`Signed in as ${state.user.display_name || state.user.username}.`));
        const link = document.createElement("a"); link.href = "/"; link.textContent = "Back to wardrobe";
        $("#auth-session-info").append(link);
        $("#auth-session-info").hidden = false;
      }
    } catch (problem) {
      error("#auth-bootstrap-error", problem.message);
      $("#auth-recheck").hidden = false;
    } finally { $("#auth-loading").hidden = true; }
  }

  function showRecovery(kind, code, username) {
    if (typeof code !== "string" || !code) throw new Error("The service did not return a recovery code. Please keep your current session and contact an administrator.");
    state.recovery = code;
    state.recoveryKind = kind;
    state.username = username;
    $("#auth-content").hidden = true;
    $("#recovery-card").hidden = false;
    $("#recovery-intro").textContent = kind === "register" ? `Account "${username}" has been created. Save this code to enter the wardrobe.` : `The password for "${username}" has been reset and the old recovery code is now invalid. Save the new code, then sign in with your new password.`;
    $("#recovery-value").value = code;
    $("#recovery-saved").checked = false;
    $("#recovery-continue").disabled = true;
    $("#recovery-continue").textContent = kind === "register" ? "Saved — enter wardrobe" : "Saved — back to sign in";
    $("#recovery-copy-status").textContent = "";
    $("#recovery-card").scrollIntoView({ block: "start", behavior: "instant" });
    $("#recovery-value").focus({ preventScroll: true });
  }

  for (const kind of ["login", "register", "recover"]) {
    $(`#${kind}-form`).addEventListener("submit", async event => {
      event.preventDefault();
      if (state.busy || state.mode !== "hosted") return;
      const username = $(`#${kind}-username`).value.trim();
      const password = $(`#${kind}-password`).value;
      let message = usernameError(username);
      if (!message) message = kind === "login" ? password ? "" : "Please enter your password." : passwordError(password);
      if (!message && kind !== "login" && password !== $(`#${kind}-confirm`).value) message = "Passwords do not match. Please try again.";
      const displayName = kind === "register" ? $("#register-display-name").value.trim() : "";
      if (!message && kind === "register" && (!displayName || [...displayName].length > 60)) message = "Please enter a display name (1–60 characters).";
      if (!message && kind === "recover" && !$("#recover-code").value.trim()) message = "Please enter the recovery code you saved.";
      error(`#${kind}-error`, message);
      if (message) return;
      const body = kind === "recover" ? { username, recovery_code: $("#recover-code").value.trim(), new_password: password } : { username, password, ...(kind === "register" ? { display_name: displayName } : {}) };
      state.busy = true;
      const controls = [...event.target.querySelectorAll("input, button"), ...$$('[data-auth-tab]')];
      controls.forEach(control => { control.disabled = true; });
      const submit = event.target.querySelector('[type="submit"]');
      const original = submit.textContent; submit.textContent = "Processing…";
      try {
        const result = await request(`/auth/${kind}`, body);
        if (kind !== "recover" && (!result.user?.id || typeof result.csrf_token !== "string" || !result.csrf_token)) throw new Error("Unable to verify the new session. Please check the account service.");
        state.user = result.user || null;
        state.csrf = result.csrf_token || null;
        event.target.querySelectorAll('input[type="password"]').forEach(input => { input.value = ""; });
        if (kind === "recover") $("#recover-code").value = "";
        announce();
        if (kind === "login") location.replace("/");
        else showRecovery(kind, result.recovery_code, result.user?.username || username);
      } catch (problem) { error(`#${kind}-error`, problem.message); }
      finally { state.busy = false; controls.forEach(control => { control.disabled = false; }); submit.textContent = original; }
    });
  }

  $$('[data-auth-tab]').forEach(button => {
    button.addEventListener("click", () => setTab(button.dataset.authTab));
    button.addEventListener("keydown", event => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      event.preventDefault();
      const names = ["login", "register", "recover"];
      const current = names.indexOf(button.dataset.authTab);
      const index = event.key === "Home" ? 0 : event.key === "End" ? 2 : (current + (event.key === "ArrowRight" ? 1 : 2)) % 3;
      setTab(names[index], true);
    });
  });
  $("#auth-recheck").addEventListener("click", initialize);
  $("#recovery-saved").addEventListener("change", () => { $("#recovery-continue").disabled = !$("#recovery-saved").checked; });
  $("#recovery-copy").addEventListener("click", async () => {
    if (!state.recovery) return;
    try { await navigator.clipboard.writeText(state.recovery); $("#recovery-copy-status").textContent = "Copied. Paste it somewhere safe to save it."; }
    catch (_) { $("#recovery-value").focus(); $("#recovery-value").select(); $("#recovery-copy-status").textContent = "Unable to copy automatically. The code has been selected — please copy or download it manually."; }
  });
  $("#recovery-download").addEventListener("click", () => {
    if (!state.recovery) return;
    if (state.downloadURL) URL.revokeObjectURL(state.downloadURL);
    state.downloadURL = URL.createObjectURL(new Blob([`Rewear Studio Recovery Code\nAccount: ${state.username}\nRecovery code: ${state.recovery}\n\nKeep this safe and do not share it. A new code will be issued after use.\n`], { type: "text/plain;charset=utf-8" }));
    const link = document.createElement("a"); link.href = state.downloadURL; link.download = "rewear-recovery-code.txt"; document.body.append(link); link.click(); link.remove();
  });
  $("#recovery-continue").addEventListener("click", () => {
    if (!state.recovery || !$("#recovery-saved").checked) return;
    const kind = state.recoveryKind;
    state.recovery = null;
    $("#recovery-value").value = "";
    if (state.downloadURL) { URL.revokeObjectURL(state.downloadURL); state.downloadURL = null; }
    if (kind === "register") location.replace("/");
    else {
      $("#recovery-card").hidden = true; $("#auth-content").hidden = false;
      $("#auth-session-info").hidden = true;
      $("#login-username").value = state.username;
      setTab("login"); $("#login-password").focus();
    }
  });
  window.addEventListener("beforeunload", event => {
    if (state.recovery) { event.preventDefault(); event.returnValue = ""; }
  });
  window.addEventListener("pagehide", () => { if (state.downloadURL) URL.revokeObjectURL(state.downloadURL); });
  initialize();
})();

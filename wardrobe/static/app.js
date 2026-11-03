"use strict";

(() => {
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const t = window.i18n ? window.i18n.t : (zh, en) => en || zh;
  const categories = { top: t("上衣","Top"), bottom: t("下身","Bottom"), dress: t("洋裝","Dress"), outerwear: t("外套","Outerwear"), onesie: t("連身衣","Onesie"), sleepwear: t("睡衣","Sleepwear"), shoes: t("鞋履","Shoes"), accessory: t("配件","Accessory"), other: t("其他／尚未判定","Other") };
  const conditions = { new_with_tags: t("全新吊牌","New with tags"), like_new: t("近全新","Like new"), good: t("良好","Good"), fair: t("一般","Fair"), poor: t("明顯磨損","Poor"), unknown: t("尚未確認","Unknown") };
  const statuses = { queued: t("等待處理","Queued"), running: t("分析中","Analyzing"), completed: t("分析完成","Completed"), partial: t("部分完成","Partial"), failed: t("處理失敗","Failed"), cancelled: t("已取消","Cancelled") };
  const stageNames = { queued: t("準備開始","Preparing"), preparing: t("準備媒體","Preparing media"), validating: t("檢查檔案","Validating"), normalizing: t("整理媒體格式","Normalizing"), segmenting: t("尋找衣物區域","Finding garments"), extracting: t("讀取衣物細節","Reading details"), saving: t("儲存分析結果","Saving results"), generating: t("生成衣物圖片","Generating images"), completed: t("分析結果已儲存","Results saved"), failed: t("未能完成這次分析","Analysis failed"), cancelled: t("這次分析已取消","Cancelled"), partial: t("已保留完成的分析結果","Partial results saved"), recovering: t("恢復上次處理","Recovering"), preparing_media: t("準備媒體","Preparing media"), analyzing: t("讀取衣物細節","Reading details"), cropping: t("裁切衣物區域","Cropping"), overlay: t("準備分割預覽","Preparing overlay") };
  const activeStatuses = new Set(["queued", "running"]);
  const retryableStatuses = new Set(["failed", "partial", "cancelled"]);
  const state = {
    health: null, healthError: "", healthChecking: false, healthRequest: 0, items: [], jobs: [], batches: [], groups: [], suggestions: [], relatedError: "", view: "wardrobe", filter: { q: "", category: "", review: "all", job: "", batch: "" },
    file: null, fileURL: null, selectedJobId: null, editingItemId: null, latestJobId: null,
    itemSignature: "", jobSignature: "", loaded: false, refreshing: false, pending: new Set(), toastTimer: null,
    pollTimer: null, lastHealthAt: 0, confirmResolve: null,
    files: [], uploadClientId: null, uploadFingerprint: "", uploading: false, generating: false, personFileId: null, personDraft: null,
    selectedBatchId: null, latestBatchId: null, selectedSuggestionId: null, groupSignature: "", batchSignature: "",
    reconstructionErrors: new Map(), reconstructionReferences: new Map(), referenceDataReady: false,
    discoveryErrors: new Map(), discoveryMarkets: new Map(),
  };
  const account = { mode: null, user: null, csrf: null, ready: false, blocked: false, reason: "", epoch: 0, checking: null, controllers: new Set(), downloadURLs: new Set() };
  const storage = { data: null, error: "", loading: false, cleaning: false, request: 0, cleanupState: null, quota: null };
  const accountChannel = typeof BroadcastChannel === "function" ? new BroadcastChannel("rewear-session") : null;
  const announceAccountChange = () => accountChannel?.postMessage("session-changed");

  function renderAccount() {
    const hosted = account.mode === "hosted";
    document.body.dataset.accountMode = hosted ? "hosted" : "local";
    $("#account-button").hidden = !hosted || !account.user;
    $("#account-display-name").textContent = account.user?.display_name || account.user?.username || "";
    $("#account-button").setAttribute("aria-label", `${account.user?.display_name || account.user?.username || ""}, ${t("帳號管理","Account")}`);
    $("#account-identity").textContent = account.user ? `${account.user.display_name} · ${account.user.username}` : "";
    // Organizer-provided (managed) logins are shared, so their password cannot be changed here.
    $("#password-form").hidden = Boolean(account.managed);
    const footerVersion = $(".footer-version"); if (footerVersion) footerVersion.textContent = hosted ? "PERSONAL WORKSPACE / 01" : "LOCAL WORKSPACE / 01";
    $(".local-footnote span").textContent = hosted ? t("目錄儲存在你的帳號，隨時可匯出。","Your catalog is stored in your account and can be exported anytime.") : t("目錄儲存在本機，隨時可匯出。","Your catalog is stored locally and can be exported anytime.");
    $("#status-dialog .dialog-heading p").textContent = hosted ? t("模型可用狀態與伺服器的處理限制。","Model availability and server processing limits.") : t("模型可用狀態與這台裝置的處理限制。","Model availability and device processing limits.");
  }

  function clearAccountWorkspace() {
    account.epoch++;
    clearTimeout(state.pollTimer);
    clearTimeout(state.toastTimer);
    for (const controller of account.controllers) controller.abort();
    for (const url of account.downloadURLs) URL.revokeObjectURL(url);
    account.downloadURLs.clear();
    state.files.forEach(entry => { if (entry.url) URL.revokeObjectURL(entry.url); });
    state.files = []; state.file = null; state.fileURL = null;
    state.items = []; state.jobs = []; state.batches = []; state.groups = []; state.suggestions = [];
    state.health = null; state.healthRequest++; state.loaded = false;
    state.itemSignature = state.jobSignature = state.batchSignature = state.groupSignature = "";
    state.filter = { q: "", category: "", review: "all", job: "", batch: "" };
    state.selectedJobId = state.editingItemId = state.selectedBatchId = state.selectedSuggestionId = null;
    state.personFileId = state.personDraft = state.latestJobId = state.latestBatchId = null;
    state.uploadClientId = null; state.uploadFingerprint = "";
    state.discoveryErrors.clear(); state.discoveryMarkets.clear(); state.reconstructionErrors.clear(); state.pending.clear();
    state.reconstructionReferences.clear(); state.referenceDataReady = false;
    storage.request++; storage.data = null; storage.error = ""; storage.loading = storage.cleaning = false; storage.cleanupState = storage.quota = null;
    renderStorage();
    $$("form").forEach(form => form.reset());
    $$("dialog[open]").forEach(dialog => { if (dialog.id !== "session-dialog") dialog.close(); });
    ["items-region", "jobs-region", "batches-region", "item-dialog-body", "job-dialog-body", "batch-dialog-body", "duplicates-body", "file-queue", "toast-region", "status-dialog-body", "active-job"].forEach(id => $(`#${id}`)?.replaceChildren());
    $("#person-photo").removeAttribute("src");
    $("#job-count").textContent = "0"; $("#job-count").hidden = true;
    $("#duplicates-count").textContent = "0";
    $("#person-filename").textContent = "";
    $("#confirm-message").textContent = "";
    $("#main-content").hidden = true;
    renderCounts();
  }

  function blockAccount(message, changed = false) {
    if (account.mode !== "hosted") return;
    if (changed && account.reason !== "changed") { clearAccountWorkspace(); account.user = null; }
    account.blocked = true; account.reason = changed ? "changed" : "expired"; account.csrf = null;
    clearTimeout(state.pollTimer);
    $("#main-content").inert = true;
    $("#session-dialog .section-index").textContent = changed ? "ACCOUNT CHANGED" : "YOUR WORK IS STILL HERE";
    $("#session-title").textContent = changed ? t("帳號已變更。","Account changed.") : t("請重新登入。","Please sign in again.");
    $("#session-message").textContent = message;
    $("#reauth-form").hidden = changed;
    $("#reauth-username").value = account.user?.username || "";
    $("#session-leave").textContent = changed ? t("重新開啟衣物室","Reopen wardrobe") : t("放棄本頁草稿，返回登入","Discard draft, return to login");
    renderAccount();
    if (!$("#session-dialog").open) { $("#session-dialog").showModal(); $(changed ? "#session-leave" : "#reauth-password").focus({ preventScroll: true }); }
  }

  function accountChanged() {
    blockAccount("Account changed in another tab or window. Data and drafts for the previous account have been cleared; please reopen the wardrobe.", true);
  }

  function acceptSession(session, initial = false) {
    if (session?.mode === "local") {
      if (!initial && account.mode === "hosted") { accountChanged(); return false; }
      account.mode = "local"; account.user = null; account.csrf = null; account.ready = true;
      return true;
    }
    if (session?.mode !== "hosted") throw new Error(t("無法確認衣物室模式，已暫停載入資料。請重新連線。","Cannot confirm wardrobe mode. Please reconnect."));
    account.mode = "hosted";
    if (!session.authenticated) {
      if (account.user) blockAccount(t("登入已失效。本頁尚未儲存的草稿仍保留，重新登入同一帳號後可繼續。","Session expired. Unsaved drafts are preserved; sign in again to continue."));
      else { account.blocked = true; location.replace("/login"); }
      return false;
    }
    if (!session.user?.id || typeof session.user.username !== "string" || typeof session.csrf_token !== "string" || !session.csrf_token) throw new Error(t("無法確認帳號工作階段，已暫停載入資料。請重新連線。","Cannot confirm account session. Please reconnect."));
    if (account.user && account.user.id !== session.user.id) { accountChanged(); return false; }
    account.user = session.user; account.csrf = session.csrf_token; account.ready = true;
    account.managed = session.accounts === "managed";
    renderAccount();
    return true;
  }

  async function verifyAccountSession() {
    if (account.mode === "local") return true;
    if (!account.checking) {
      account.checking = api("/auth/session").then(session => acceptSession(session)).finally(() => { account.checking = null; });
    }
    return account.checking;
  }

  async function initializeAccount() {
    $("#account-bootstrap").hidden = true;
    $("#account-bootstrap-retry").disabled = true;
    try {
      if (!acceptSession(await api("/auth/session"), true)) return;
      account.blocked = false;
      $("#main-content").inert = false;
      renderAccount();
      await Promise.allSettled([refreshHealth(), refreshData()]);
      schedulePoll();
    } catch (error) {
      $("#account-bootstrap-message").textContent = error.message;
      $("#account-bootstrap").hidden = false;
    } finally { $("#account-bootstrap-retry").disabled = false; }
  }

  async function resumeAccount() {
    account.blocked = false; account.reason = "";
    $("#main-content").inert = false;
    $("#reauth-password").value = "";
    showError("reauth-error", "");
    $("#session-dialog").close();
    renderAccount();
    await Promise.allSettled([refreshData(true), refreshHealth()]);
    if ($("#account-dialog").open) await refreshStorage();
    schedulePoll();
  }

  function storageSize(bytes) {
    if (!Number.isFinite(bytes) || bytes < 0) return "—";
    const units = ["B", "KB", "MB", "GB"];
    const index = bytes ? Math.min(3, Math.floor(Math.log(bytes) / Math.log(1024))) : 0;
    return `${(bytes / (1024 ** index)).toLocaleString(_locale, { maximumFractionDigits: 1 })} ${units[index]}`;
  }

  function validStorage(data) {
    return [data?.limits?.jobs, data?.limits?.media_bytes, data?.usage?.jobs, data?.usage?.media_bytes, data?.usage?.reserved_jobs, data?.usage?.reserved_bytes, data?.cleanup?.pending_directories].every(value => Number.isSafeInteger(value) && value >= 0);
  }

  function renderStorage() {
    const data = storage.data;
    const pending = Number(data?.cleanup?.pending_directories || 0);
    const knownPending = pending > 0 || storage.cleanupState === "pending";
    $("#account-storage").hidden = account.mode !== "hosted";
    $("#storage-loading").hidden = !storage.loading;
    $("#storage-refresh").disabled = storage.loading || storage.cleaning;
    $("#storage-jobs").textContent = data ? `${data.usage.jobs.toLocaleString(_locale)} / ${data.limits.jobs.toLocaleString(_locale)} ${t("筆","records")}` : "—";
    $("#storage-bytes").textContent = data ? `${storageSize(data.usage.media_bytes)} / ${storageSize(data.limits.media_bytes)}` : "—";
    $("#storage-reserved").hidden = !data;
    $("#storage-reserved").textContent = data ? t(`進行中的工作另預留 ${data.usage.reserved_jobs} 筆、${storageSize(data.usage.reserved_bytes)}；不代表這些檔案已產生。`,`Active jobs reserve ${data.usage.reserved_jobs} records and ${storageSize(data.usage.reserved_bytes)}; files may not exist yet.`) : "";
    showError("storage-error", storage.error);
    $("#storage-pending").hidden = !knownPending;
    $("#storage-cleanup-message").textContent = knownPending ? t(`${pending ? `${pending} 組` : "已刪除紀錄的"}媒體檔案尚待清理，仍佔用空間。${typeof data?.cleanup?.error === "string" ? ` ${data.cleanup.error}` : "請重試清理。"}`,`${pending ? `${pending} sets of` : "Deleted records'"} media files pending cleanup, still using space.${typeof data?.cleanup?.error === "string" ? ` ${data.cleanup.error}` : " Please retry cleanup."}`) : "";
    $("#storage-cleanup").disabled = storage.cleaning || storage.loading || !knownPending;
    const notices = [];
    if (storage.quota) notices.push(`${storage.quota === "jobs" ? t("上次工作超過帳號保留工作上限","Previous job exceeded retention limit") : t("上次工作需要的儲存空間不足","Insufficient storage for previous job")}。可在「處理紀錄」移除不需要的工作，再嘗試開始新工作；已存資料仍可讀取與匯出。`);
    if (knownPending) notices.push(t("紀錄已刪除，但媒體仍待清理，暫時仍佔用空間；可在帳號管理重試清理。","Records deleted but media cleanup pending; retry in Account settings."));
    else if (storage.cleanupState === "unknown") notices.push(t("紀錄已刪除，媒體清理狀態尚無法確認。請更新儲存用量。","Records deleted; cleanup status unknown. Refresh storage usage."));
    $("#storage-alert").hidden = account.mode !== "hosted" || !notices.length;
    $("#storage-alert-message").textContent = notices.join(" ");
  }

  function acceptStorage(data) {
    if (!validStorage(data)) throw new Error(t("無法讀取儲存用量，請重新更新。","Cannot read storage usage. Please refresh."));
    storage.data = data;
    storage.error = "";
    storage.cleanupState = data.cleanup.pending_directories > 0 ? "pending" : null;
  }

  async function refreshStorage() {
    if (account.mode !== "hosted" || !account.ready || account.blocked || storage.cleaning) return;
    const request = ++storage.request;
    storage.loading = true; storage.error = ""; renderStorage();
    try {
      const data = await api("/api/storage");
      if (request !== storage.request || account.blocked) return;
      acceptStorage(data);
    } catch (error) {
      if (request !== storage.request || account.blocked) return;
      storage.data = null; storage.error = error.message;
    } finally {
      if (request === storage.request) { storage.loading = false; renderStorage(); }
    }
  }

  function openAccountManagement(focusStorage = false) {
    if (account.mode !== "hosted" || account.blocked) return;
    renderAccount();
    $("#password-form").reset();
    showError("password-error", ""); showError("logout-error", "");
    $("#password-success").hidden = true;
    if (!$("#account-dialog").open) $("#account-dialog").showModal();
    $(focusStorage ? "#storage-title" : '[data-close="account-dialog"]').focus({ preventScroll: true });
    if (focusStorage) $("#account-storage").scrollIntoView({ block: "start" });
    else $("#account-dialog").scrollTop = 0;
    refreshStorage();
  }

  function escapeHTML(value) {
    return String(value ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  function icon(name, className = "") {
    return `<svg class="icon ${className}" aria-hidden="true"><use href="#i-${name}"/></svg>`;
  }

  function safeMediaURL(value) {
    if (!value || typeof value !== "string") return "";
    try {
      const url = new URL(value, window.location.origin);
      return url.origin === window.location.origin && ["http:", "https:"].includes(url.protocol) ? url.href : "";
    } catch (_) { return ""; }
  }

  function safePublicURL(value) {
    if (typeof value !== "string" || value.length > 2048 || /[\s\\\x00-\x1f\x7f]/.test(value) || !/^https?:\/\//i.test(value)) return "";
    try {
      const url = new URL(value);
      const host = url.hostname.toLowerCase().replace(/\.$/, "");
      if (!["http:", "https:"].includes(url.protocol) || url.username || url.password || !host.includes(".") || host.includes(":")) return "";
      if (/^[\d.]+$/.test(host) || /(^|\.)(localhost|localdomain|local|internal|lan|home|home\.arpa|test|invalid|example|onion|intranet)$/.test(host)) return "";
      if (host.split(".").some(label => label === "localhost" || !/^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/.test(label))) return "";
      return url.href;
    } catch (_) { return ""; }
  }

  function isVideoURL(url) { return /\.(mp4|mov|webm|m4v)(?:\?|$)/i.test(url || ""); }
  function isVideoFile(file) { return !!file && (file.type.startsWith("video/") || /\.(mp4|mov|webm|m4v)$/i.test(file.name)); }
  function isActive(job) { return job && activeStatuses.has(job.status); }
  function reconstructionEligible(item) { return !!item.record?.category && item.record.category !== "other" && (item.reviewed || ["ready", "needs_confirmation"].includes(item.quality?.status)); }
  function reconstructionAvailable() { return state.health?.reconstruction?.available ?? state.health?.meta?.configured; }
  const reconstructionCheckNames = ["viewpoint", "visible_details", "color_pattern", "cut_structure", "single_garment", "no_person_or_extras", "complete_outline"];
  const reconstructionCheckLabels = { viewpoint: t("正反面與視角","Viewpoint & angle"), visible_details: t("可見設計細節","Visible details"), color_pattern: t("顏色與花紋","Color & pattern"), cut_structure: t("剪裁與衣物結構","Cut & structure"), single_garment: t("單件衣物","Single garment"), no_person_or_extras: t("人物或多餘物件","Person or extras"), complete_outline: t("完整衣物輪廓","Complete outline") };
  function reconstructionUncertainties(plan) {
    return plan?.initial_source_plan != null ? plan.initial_source_plan.uncertain_areas : plan?.uncertain_areas;
  }
  function unshownReconstructionArea(area, sourceView) {
    const oppositeView = { front: "back", back: "front" }[sourceView];
    return !!oppositeView && area?.critical === false && area.scope === "unshown"
      && typeof area.scope_evidence === "string" && !!area.scope_evidence.trim() && area.scope_evidence.trim().length <= 600
      && area.unshown_view === oppositeView;
  }
  function validReferenceManifest(manifest, normalized = false) {
    const object = value => value !== null && typeof value === "object" && !Array.isArray(value);
    const identifier = value => typeof value === "string" && /^[a-f0-9]{32}$/.test(value);
    const hash = value => typeof value === "string" && /^[a-f0-9]{64}$/.test(value);
    const sourceIds = ["cutout", "context", "extra_1", "extra_2"];
    const keys = ["version", "target_item_id", "group_id", "membership_sha256", "primary_source", "sources"];
    const sourceKeys = ["id", "item_id", "job_id", "role", "media_url", "source_url", "original_url", "file_sha256", "reference_sha256"];
    if (!object(manifest) || Object.keys(manifest).some(key => !keys.includes(key))
      || (manifest.version !== undefined && manifest.version !== 1) || !identifier(manifest.target_item_id)
      || !["context", "cutout"].includes(manifest.primary_source)
      || !Array.isArray(manifest.sources) || manifest.sources.length < 1 || manifest.sources.length > 4) return false;
    if (manifest.group_id != null && !identifier(manifest.group_id) || manifest.membership_sha256 != null && !hash(manifest.membership_sha256)
      || (manifest.group_id == null) !== (manifest.membership_sha256 == null)) return false;
    function originalURL(value, jobId, image = false) {
      if (typeof value !== "string" || !value || value.length > 2048 || /[\\%?#\x00-\x1f\x7f]/.test(value)) return false;
      const prefix = `/media/${jobId}/`;
      return value.startsWith(prefix) && !value.slice(prefix.length).split("/").some(part => ["", ".", "..", "reconstructions", "shopping"].includes(part))
        && (!image || /\.(?:jpe?g|png|webp)$/i.test(value));
    }
    if (manifest.sources.some(source => !object(source) || Object.keys(source).some(key => !sourceKeys.includes(key))
      || !sourceIds.includes(source.id) || !identifier(source.item_id) || !identifier(source.job_id) || !hash(source.file_sha256)
      || (normalized ? !hash(source.reference_sha256) : source.reference_sha256 != null && !hash(source.reference_sha256))
      || source.role !== (source.id === "cutout" ? "target_locator" : "original_context")
      || !originalURL(source.media_url, source.job_id, true)
      || [source.source_url, source.original_url].some(value => value != null && !originalURL(value, source.job_id)))) return false;
    const ids = manifest.sources.map(source => source.id);
    const cutout = manifest.sources.find(source => source.id === "cutout");
    if (!cutout || new Set(ids).size !== ids.length || JSON.stringify(ids) !== JSON.stringify(sourceIds.filter(id => ids.includes(id)))
      || manifest.primary_source !== (ids.includes("context") ? "context" : "cutout")
      || ids.includes("extra_2") && !ids.includes("extra_1")) return false;
    if (manifest.sources.some(source => ["cutout", "context"].includes(source.id)
      && (source.item_id !== manifest.target_item_id || source.job_id !== cutout.job_id))) return false;
    const extras = manifest.sources.filter(source => source.id.startsWith("extra_"));
    return new Set(extras.map(source => source.item_id)).size === extras.length
      && !extras.some(source => source.item_id === manifest.target_item_id) && (!extras.length || manifest.group_id != null);
  }

  function reconstructionSourcesCovered(plan) {
    if (!plan || plan.reference_manifest != null && !validReferenceManifest(plan.reference_manifest, true)) return false;
    const available = new Set(plan.reference_manifest?.sources.map(source => source.id) || ["cutout", "context"]);
    const regions = [plan.detail_regions === undefined ? [] : plan.detail_regions, plan.original_ambiguous_regions === undefined ? [] : plan.original_ambiguous_regions,
      plan.initial_source_plan?.detail_regions === undefined ? [] : plan.initial_source_plan.detail_regions];
    return regions.every(entries => Array.isArray(entries) && entries.every(region => available.has(region?.source_image)));
  }

  function reconstructionSourcesMatchItem(report, item) {
    const manifest = report?.plan?.reference_manifest;
    return manifest == null || manifest.target_item_id === item.id && manifest.sources?.find(source => source.id === "cutout")?.job_id === item.job_id;
  }

  function validReconstructionFrontState(value, primary = false) {
    if (value == null) return true;
    const keys = primary ? ["state", "evidence", "box", "source_image"] : ["state", "evidence", "box"];
    const states = ["open", "partly_open", "closed", "overlapping", "not_applicable", "unknown"];
    if (typeof value !== "object" || Array.isArray(value) || Object.keys(value).some(key => !keys.includes(key))
      || !states.includes(value.state) || typeof value.evidence !== "string" || !value.evidence.trim()
      || value.evidence.trim().length > 600 || !Object.hasOwn(value, "box")
      || primary && !["context", "cutout"].includes(value.source_image)) return false;
    const box = value.box;
    if (box === null) return ["unknown", "not_applicable"].includes(value.state);
    return Array.isArray(box) && box.length === 4
      && box.every(number => typeof number === "number" && Number.isFinite(number) && number >= 0 && number <= 1)
      && box[2] - box[0] >= 0.01 && box[3] - box[1] >= 0.01;
  }

  // Reasons, inferred areas and the preview notice mirror reconstruction_quality.py verbatim; never translate them.
  function reconstructionFrontComparison(source, candidate) {
    if (source == null) return candidate == null ? null : { status: "uncertain", reason: "缺少主照前襟狀態依據，不能確認候選觀察與原圖一致。" };
    if (source.state === "not_applicable") return null;
    if (source.state === "unknown") return { status: "uncertain", reason: "主照前襟展示狀態無法確認，不能宣稱候選已保留。" };
    if (candidate == null || ["unknown", "not_applicable"].includes(candidate.state)) return { status: "uncertain", reason: "候選缺少可確認的前襟展示觀察，不能判定與主照一致。" };
    if (candidate.state === source.state) return { status: "pass", reason: "候選的獨立前襟展示觀察與主照狀態一致。" };
    const labels = { open: "敞開分離", partly_open: "部分敞開", closed: "靠攏閉合", overlapping: "前片交疊" };
    return { status: "fail", reason: `主照前襟為「${labels[source.state]}」，候選為「${labels[candidate.state]}」，未保留主照展示狀態。` };
  }

  function reconstructionFrontEvidence(report) {
    const plan = report?.plan;
    const source = plan?.primary_front_state ?? null;
    const initial = plan?.initial_source_plan;
    if (!validReconstructionFrontState(source, true)
      || initial != null && !sameJSONValue(source, initial.primary_front_state ?? null)) return false;
    const manifest = plan?.reference_manifest;
    if (source && manifest && (source.source_image !== manifest.primary_source
      || !manifest.sources?.some(entry => entry.id === source.source_image))) return false;
    const attempts = report?.attempts;
    // A legacy report adds no new front-state claim; retain its existing gates.
    if (source == null && report?.candidate_front_state == null
      && (!Array.isArray(attempts) || attempts.every(attempt => attempt?.candidate_front_state == null))) return true;
    if (!Array.isArray(attempts) || !attempts.length || attempts.length > 2) return false;
    const selectedNumber = report.selected_attempt ?? attempts.length;
    if (!Number.isInteger(selectedNumber) || selectedNumber < 1 || selectedNumber > attempts.length) return false;
    const selected = attempts[selectedNumber - 1];
    if (!sameJSONValue(report.candidate_front_state ?? null, selected?.candidate_front_state ?? null)) return false;
    const severity = { pass: 0, uncertain: 1, fail: 2 };
    return [...attempts, report].every(entry => {
      if (!validReconstructionFrontState(entry?.candidate_front_state)) return false;
      const compared = reconstructionFrontComparison(source, entry?.candidate_front_state);
      if (!compared || compared.status === "pass") return true;
      const checks = [entry?.checks?.visible_details, entry?.checks?.cut_structure,
        ...(Array.isArray(entry?.feature_checks) ? entry.feature_checks.filter(check => check?.feature === "closures") : [])];
      return checks.every(check => Object.hasOwn(severity, check?.status) && severity[check.status] >= severity[compared.status]);
    });
  }

  function acceptedReconstructionReport(report) {
    const plan = report?.plan;
    const features = plan?.visible_features;
    const checked = report?.feature_checks;
    const regions = plan?.detail_regions === undefined ? [] : plan.detail_regions;
    const regionalChecks = report?.regional_checks === undefined ? [] : report.regional_checks;
    const initialPlan = plan?.initial_source_plan;
    const uncertain = reconstructionUncertainties(plan);
    const originalRegions = plan?.original_ambiguous_regions === undefined ? [] : plan.original_ambiguous_regions;
    const initialRegions = initialPlan?.detail_regions === undefined ? [] : initialPlan.detail_regions;
    const uncertaintyKeys = ["area", "detail", "critical", "scope", "scope_evidence", "unshown_view"];
    const retainedEvidence = Array.isArray(originalRegions) && originalRegions.length === 0
      && (initialPlan == null || (typeof initialPlan === "object"
        && Array.isArray(initialRegions) && initialRegions.length <= 3 && !initialRegions.some(region => region?.support === "ambiguous")
        && Array.isArray(uncertain) && Array.isArray(plan.uncertain_areas)
        && uncertain.length === plan.uncertain_areas.length
        && uncertain.every((area, index) => area && plan.uncertain_areas[index]
          && uncertaintyKeys.every(key => (area[key] ?? null) === (plan.uncertain_areas[index][key] ?? null)))));
    const passes = check => check?.status === "pass" && typeof check.reason === "string" && !!check.reason.trim();
    const hasText = value => typeof value === "string" && !!value.trim();
    const validBox = box => Array.isArray(box) && box.length === 4
      && box.every(value => typeof value === "number" && Number.isFinite(value) && value >= 0 && value <= 1)
      && box[2] - box[0] >= 0.01 && box[3] - box[1] >= 0.01;
    const regionsPass = Array.isArray(regions) && Array.isArray(regionalChecks)
      && regions.length <= 5 && regionalChecks.length === regions.length
      && new Set(regions.map(region => region?.name)).size === regions.length
      && new Set(regionalChecks.map(check => check?.name)).size === regionalChecks.length
      && regions.every(region => hasText(region?.name) && hasText(region.feature) && hasText(region.detail)
        && region.support === "clear" && ["context", "cutout", "extra_1", "extra_2"].includes(region.source_image) && validBox(region.box)
        && regionalChecks.some(check => check?.name === region.name && check.feature === region.feature
          && passes(check) && hasText(check.source_observation) && hasText(check.candidate_observation)));
    return !!report && report.version === 1 && report.status === "accepted" && report.generated === true
      && ["front", "back"].includes(report.source_view)
      && plan?.target === "one_garment" && plan.source_view === report.source_view
      && ["complete", "partial"].includes(plan.visibility)
      && typeof plan.garment_type === "string" && !!plan.garment_type.trim()
      && reconstructionCheckNames.every(name => passes(report.checks?.[name]))
      && Object.values(report.checks || {}).every(passes)
      && retainedEvidence && Array.isArray(uncertain) && uncertain.length <= 10
      && uncertain.every(area => hasText(area?.area) && hasText(area.detail)
        && unshownReconstructionArea(area, plan.source_view))
      && Array.isArray(features) && features.length > 0 && Array.isArray(checked) && features.length === checked.length
      && new Set(checked.map(entry => entry?.feature)).size === checked.length
      && features.every(feature => typeof feature?.feature === "string" && checked.some(entry => entry?.feature === feature.feature && passes(entry)))
      && regionsPass && reconstructionSourcesCovered(plan) && reconstructionFrontEvidence(report);
  }
  function acceptedReconstruction(item) {
    const report = item.reconstruction?.image_validation;
    return acceptedReconstructionReport(report) && reconstructionSourcesMatchItem(report, item) ? safeMediaURL(item.reconstruction?.image_url) : "";
  }
  const previewCheckNames = ["category", "coarse_color", "single_garment", "no_person_or_extras", "complete_outline"];
  function sameJSONValue(left, right) {
    if (left === right) return true;
    if (!left || !right || typeof left !== "object" || typeof right !== "object" || Array.isArray(left) !== Array.isArray(right)) return false;
    const keys = Object.keys(left);
    return keys.length === Object.keys(right).length && keys.every(key => Object.hasOwn(right, key) && sameJSONValue(left[key], right[key]));
  }
  function readyPreviewReport(report) {
    const preview = report?.preview;
    const plan = report?.plan;
    const attempts = report?.attempts;
    const selectedNumber = report?.selected_attempt ?? attempts?.length;
    const selected = Array.isArray(attempts) && Number.isInteger(selectedNumber) && selectedNumber >= 1 && selectedNumber <= attempts.length ? attempts[selectedNumber - 1] : null;
    const hasText = value => typeof value === "string" && !!value.trim();
    const passes = check => check?.status === "pass" && hasText(check.reason);
    const uncertainties = reconstructionUncertainties(plan);
    const sourcePlan = plan?.initial_source_plan ?? plan;
    const regions = sourcePlan?.detail_regions === undefined ? [] : sourcePlan.detail_regions;
    const originalRegions = plan?.original_ambiguous_regions === undefined ? [] : plan.original_ambiguous_regions;
    const ambiguous = Array.isArray(regions) ? regions.filter(region => region?.support === "ambiguous") : [];
    const retainedEvidence = Array.isArray(uncertainties) && Array.isArray(regions) && Array.isArray(originalRegions)
      && (plan?.initial_source_plan == null ? originalRegions.length === 0
        : sameJSONValue(plan.uncertain_areas, uncertainties) && sameJSONValue(originalRegions, ambiguous));
    const expectedInferences = Array.isArray(uncertainties) ? uncertainties.filter(area => !unshownReconstructionArea(area, plan?.source_view)).map(area => ({ area: area?.area, detail: area?.detail })) : [];
    expectedInferences.push(...ambiguous.map(region => ({ area: region.name, detail: region.detail })));
    if (!["front", "back"].includes(plan?.source_view)) expectedInferences.push({ area: "展示面", detail: "原圖未完整確立正面或背面，預覽採推測的正面展示。" });
    const expectedWarnings = [...new Set([
      ...reconstructionCheckNames.map(name => selected?.checks?.[name]),
      ...(Array.isArray(selected?.feature_checks) ? selected.feature_checks : []),
      ...(Array.isArray(selected?.regional_checks) ? selected.regional_checks : []),
      reconstructionFrontComparison(plan?.primary_front_state, selected?.candidate_front_state),
    ].filter(check => check?.status === "fail").map(check => check.reason))];
    return !!report && report.version === 1 && report.generated === true
      && report.plan?.target === "one_garment" && hasText(report.plan.garment_type)
      && ["complete", "partial"].includes(report.plan.visibility)
      && Array.isArray(report.plan.visible_features) && report.plan.visible_features.length > 0
      && preview?.version === 1 && preview.status === "ready"
      && ["front", "back", "side", "unclear"].includes(preview.source_view)
      && preview.source_view === report.source_view && preview.source_view === report.plan.source_view
      && ["front", "back"].includes(preview.display_view) && typeof preview.view_inferred === "boolean"
      && (["front", "back"].includes(preview.source_view)
        ? preview.display_view === preview.source_view && preview.view_inferred === false
        : preview.display_view === "front" && preview.view_inferred === true)
      && previewCheckNames.every(name => passes(preview.checks?.[name]))
      && Object.values(preview.checks || {}).every(passes)
      && !!selected && sameJSONValue(selected.preview, preview)
      && reconstructionCheckNames.every(name => ["pass", "fail", "uncertain"].includes(selected.checks?.[name]?.status) && hasText(selected.checks[name].reason))
      && ["single_garment", "no_person_or_extras", "complete_outline"].every(name => sameJSONValue(selected.checks?.[name], preview.checks[name]))
      && retainedEvidence && sameJSONValue(preview.inferred_areas, expectedInferences) && sameJSONValue(preview.mismatch_warnings, expectedWarnings)
      && Array.isArray(preview.inferred_areas) && preview.inferred_areas.length <= 14
      && preview.inferred_areas.every(area => hasText(area?.area) && area.area.length <= 100 && hasText(area.detail) && area.detail.length <= 600)
      && Array.isArray(preview.mismatch_warnings) && preview.mismatch_warnings.length <= 25
      && preview.mismatch_warnings.every(warning => hasText(warning) && warning.length <= 600)
      && preview.notice === "AI 預覽・含推測；只確認完整展示的基本適用性，不代表已確認為同一件衣物。"
      && reconstructionSourcesCovered(plan) && reconstructionFrontEvidence(report);
  }
  function readyPreview(item) {
    const report = item.reconstruction?.preview_validation;
    return readyPreviewReport(report) && reconstructionSourcesMatchItem(report, item) ? safeMediaURL(item.reconstruction?.preview_url) : "";
  }
  function affordablePreviewCandidates(item) {
    const saved = item.reconstruction;
    if (saved?.mode !== "preview" || !Array.isArray(saved.preview_candidates)
      || saved.preview_candidates.length < 1 || saved.preview_candidates.length > 3) return [];
    const prefix = `/media/${encodeURIComponent(item.job_id)}/reconstructions/`;
    const ids = new Set();
    const result = [];
    for (const entry of saved.preview_candidates) {
      if (!entry || typeof entry !== "object" || !/^[1-3]$/.test(entry.id) || ids.has(entry.id)) return [];
      const url = safeMediaURL(entry.variant_url);
      if (!url) return [];
      const parsed = new URL(url);
      const filename = parsed.pathname.startsWith(prefix) ? parsed.pathname.slice(prefix.length) : "";
      if (parsed.search || parsed.hash || !/^[a-f0-9]{32}-preview-[1-3]\.png$/.test(filename)) return [];
      ids.add(entry.id); result.push({ id: entry.id, url });
    }
    return result;
  }
  function affordablePreview(item) {
    const candidates = affordablePreviewCandidates(item);
    const selected = candidates.find(entry => entry.id === item.reconstruction?.selected_candidate_id);
    return (selected || candidates[0])?.url || "";
  }
  function reconstructionNotice(item) {
    const saved = item.reconstruction;
    if (!saved) return "";
    if (affordablePreview(item) && !acceptedReconstruction(item) && !readyPreview(item)) {
      return `${affordablePreviewCandidates(item).length} ${t("張 AI 完整預覽 · 已選第","AI previews · selected #")}${saved.selected_candidate_id || "1"}${t("，請與原圖核對","; compare with original")}`;
    }
    if (readyPreview(item) && !acceptedReconstruction(item)) {
      const warnings = saved.preview_validation.preview.mismatch_warnings;
      return warnings.length ? `${t("AI 預覽有可見差異","AI preview has visible differences")} · ${warnings[0]}` : t("AI 預覽含推測細節 · 請與原圖核對","AI preview contains inferred details · compare with original");
    }
    if (saved.status === "rejected") return t("生成圖與原圖不符 · 查看差異","Generated image differs · view differences");
    if (saved.status === "needs_review") return saved.candidate_url ? t("生成圖待核對 · 仍以原圖為準","Generated image needs review · original takes precedence") : t("原圖細節不足 · 暫不生成","Insufficient detail · skipped");
    if (saved.image_url && !acceptedReconstruction(item)) return t("舊版生成圖未核對 · 仍以原圖為準","Legacy image not reviewed · original takes precedence");
    return "";
  }
  function reconstructionConcerns(report) {
    if (!report || typeof report !== "object") return [];
    const concerns = [];
    for (const name of reconstructionCheckNames) {
      const check = report.checks?.[name];
      if (!["fail", "uncertain"].includes(check?.status)) continue;
      concerns.push({ label: `${reconstructionCheckLabels[name]}${check.status === "fail" ? t("未通過比對。"," failed.") : t("仍無法確認。"," unconfirmed.")}`, reason: typeof check.reason === "string" ? check.reason.slice(0, 600) : "" });
    }
    const areas = reconstructionUncertainties(report.plan);
    const uncertain = Array.isArray(areas) ? areas.filter(area => !unshownReconstructionArea(area, report.plan?.source_view)).map(area => area?.detail) : [];
    const originalRegions = report.plan?.original_ambiguous_regions;
    const initialRegions = report.plan?.initial_source_plan?.detail_regions;
    const originalConcerns = [...(Array.isArray(originalRegions) ? originalRegions : []), ...(Array.isArray(initialRegions) ? initialRegions.filter(region => region?.support === "ambiguous") : [])].map(region => region?.detail);
    for (const reason of [...(Array.isArray(report.issues) ? report.issues : []), ...uncertain, ...originalConcerns].slice(0, 20)) {
      if (typeof reason !== "string" || !reason.trim() || concerns.some(entry => entry.reason === reason || entry.label === reason.slice(0, 600))) continue;
      concerns.push({ label: /[\u3400-\u9fff]/.test(reason) ? reason.slice(0, 600) : t("部分款式細節仍需核對。","Some style details need review."), reason: /[\u3400-\u9fff]/.test(reason) ? "" : reason.slice(0, 600) });
    }
    return concerns;
  }
  function generatedSource(item) { return item.provenance === "generated" || state.jobs.find(job => job.id === item.job_id)?.media_type === "generated"; }
  function discoveryEligible(item) { return !generatedSource(item) && reconstructionEligible(item); }
  function marketName(value) { return value === "TW" ? "Taiwan" : value === "global" ? "Global" : "Not recorded"; }
  function percentage(value) { return Math.max(0, Math.min(100, Math.round(Number(value) || 0))); }
  function providerName(value) { return value === "meta" ? "Meta Spark" : value === "local" ? t("Glimmer · 本機","Glimmer · Local") : String(value || t("未指定","Unspecified")); }
  function stageName(job) { return stageNames[job.stage] || job.stage || statuses[job.status] || t("處理中","Processing"); }
  const _locale = window.i18n?.lang === "en" ? "en-US" : "zh-TW";
  function formatDate(value, full = false) {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return t("日期未提供","Date unavailable");
    return new Intl.DateTimeFormat(_locale, { month: "2-digit", day: "2-digit", ...(full ? { year: "numeric", hour: "2-digit", minute: "2-digit", hour12: false } : {}) }).format(date);
  }
  function isDemoItem(item) {
    return /historical|demo/i.test(String(item.provenance || "")) || !!state.jobs.find(job => job.id === item.job_id)?.is_demo;
  }
  function needsRetake(item) { return !item.reviewed && !isDemoItem(item) && item.quality?.status === "needs_retake"; }
  function isUnassessed(item) { return !item.reviewed && !isDemoItem(item) && item.quality?.status === "unassessed"; }
  function needsConfirmation(item) { return !item.reviewed && !isDemoItem(item) && item.quality?.status === "needs_confirmation"; }
  function needsAttention(item) { return needsRetake(item) || isUnassessed(item); }
  function isUsable(item) { return !needsAttention(item) && !needsConfirmation(item); }
  function analysisFailed(item) { return isUnassessed(item) && typeof item.error === "string" && !!item.error.trim(); }
  function defaultMarket() { return window.i18n?.lang === "zh-TW" ? "TW" : "global"; }
  function sentenceCase(value) { const text = String(value || "").trim(); return text && text.charAt(0).toLocaleUpperCase() + text.slice(1); }
  function groupFor(item) { return state.groups.find(group => group.item_ids?.includes(typeof item === "string" ? item : item.id)); }
  function groupMembers(group) { return (group?.item_ids || []).map(id => state.items.find(item => item.id === id)).filter(Boolean); }
  function representativeItems(items) {
    const seen = new Set();
    return items.flatMap(item => {
      const group = groupFor(item);
      const primary = group ? state.items.find(entry => entry.id === group.primary_item_id) || item : item;
      if (seen.has(primary.id)) return [];
      seen.add(primary.id);
      return [primary];
    });
  }
  function modeName(mode) { return { single: t("單件","Single"), rack: t("多件／影片","Multi / Video"), outfit: t("生活穿搭","Outfit") }[mode] || mode; }
  function qualityReasons(item) {
    const reasons = Array.isArray(item.quality?.reasons) ? item.quality.reasons.filter(reason => typeof reason === "string" && reason.trim()) : [];
    if (reasons.length) return reasons;
    if (item.quality?.status === "unassessed") return [t("目前無法可靠判讀這個區域的衣物完整度。","Unable to reliably assess garment completeness in this area.")];
    if (item.quality?.status === "needs_retake") return [t("影像尚不足以確認這是一件完整、清楚的衣物。","Image insufficient to confirm a complete, clear garment.")];
    return [];
  }
  function retakeAdvice(item) {
    if (item.quality?.model_target === "multiple_garments") return t("衣物彼此重疊時，請分開拍攝，讓每件衣物都清楚入鏡。","When pieces overlap, photograph each one separately so it's fully visible.");
    if (item.quality?.model_target === "no_garment") return t("請換成衣物照片，將衣物攤平或正面掛好，讓整件衣物清楚入鏡。","Use a garment photo. Lay flat or hang so the full item is visible.");
    if (item.quality?.model_target === "uncertain") return t("確認畫面主體是一件衣物，使用充足光線並對焦清楚，再重新拍攝。","Ensure subject is a garment with good lighting and focus.");
    if (item.quality?.visibility === "fragment") return t("拉開衣架間距，讓衣物正面完整露出；仍有重疊時，改成逐件拍攝。","Space hangers so the front is fully visible; photograph separately if overlapping.");
    if (item.quality?.visibility === "partial") return t("退後一步，將領口、袖口與下襬完整拍進畫面，避免被照片邊緣裁掉。","Step back to capture neckline, cuffs and hem without cropping.");
    if (item.quality?.visibility === "unclear") return t("使用充足、均勻的光線，對焦清楚並保持鏡頭穩定，再拍攝整件衣物。","Use even lighting, focus clearly and hold steady.");
    return t("將衣物攤平或正面掛好，以清楚的單件照片重新上傳，並保留四周留白。","Lay flat or hang; retake with clear margins around the item.");
  }
  function resultCounts(items) {
    return { total: items.length, usable: items.filter(isUsable).length, pending: items.filter(item => !item.reviewed && isUsable(item)).length, reviewed: items.filter(item => item.reviewed).length, retake: items.filter(needsRetake).length, unassessed: items.filter(isUnassessed).length, confirmation: items.filter(needsConfirmation).length };
  }
  function syncJobCounts(jobId) {
    const counts = resultCounts(state.items.filter(item => item.job_id === jobId));
    state.jobs = state.jobs.map(job => job.id === jobId ? { ...job, item_count: counts.total, usable_count: counts.usable, retake_count: counts.retake, unassessed_count: counts.unassessed, confirmation_count: counts.confirmation } : job);
    renderJobs();
  }
  function jobResultSummary(job) {
    const total = Number(job.item_count) || 0;
    const items = state.items.filter(item => item.job_id === job.id);
    const counts = resultCounts(items);
    const usable = Number.isFinite(job.usable_count) ? job.usable_count : items.length === total ? counts.usable : null;
    const retake = Number.isFinite(job.retake_count) ? job.retake_count : counts.retake;
    const unassessed = Number.isFinite(job.unassessed_count) ? job.unassessed_count : counts.unassessed;
    const confirmation = Number.isFinite(job.confirmation_count) ? job.confirmation_count : counts.confirmation;
    return [`${total} ${t("筆分析結果","results")}`, usable !== null ? `${usable} ${t("件可整理","usable")}` : "", confirmation ? `${confirmation} ${t("筆穿搭待確認","need confirmation")}` : "", retake ? `${retake} ${t("筆需補拍","need retake")}` : "", unassessed ? `${unassessed} ${t("筆尚無法判讀","unassessed")}` : ""].filter(Boolean).join(" · ");
  }
  function finishedToast(jobs) {
    const ids = new Set(jobs.map(job => job.id));
    const items = distinctItems(state.items.filter(item => ids.has(item.job_id)));
    const pieces = items.filter(item => !needsAttention(item)).length;
    if (jobs.length && jobs.every(job => job.status === "cancelled")) return [t("已取消分析，完成的結果仍然保留。","Analysis cancelled; finished results are kept."), false];
    if (pieces) return [`${t("分析完成：找到","Done — found")} ${pieces} ${pieces === 1 ? t("件衣物","piece") : t("件衣物","pieces")}`, false];
    const error = items.find(analysisFailed)?.error || jobs.find(job => job.error)?.error;
    if (error) return [`${t("分析未完成：","Analysis didn't finish: ")}${error}`, true];
    return [items.length ? t("分析完成：請換一張更清楚的照片","Done — try a clearer photo") : t("分析完成：沒有找到衣物","Done — no garments found"), true];
  }
  function titleFor(item) {
    if (analysisFailed(item)) return t("這張照片未能完成分析","Couldn't analyze this photo");
    if (isUnassessed(item)) return t("尚無法判讀的衣物區域","Couldn't read this piece");
    if (needsRetake(item)) {
      const targetTitle = { multiple_garments: t("畫面中有多件衣物","Several pieces overlap"), no_garment: t("未偵測到衣物","No garment detected"), uncertain: t("尚無法確認衣物","Cannot confirm garment") }[item.quality?.model_target];
      return targetTitle || (item.quality?.visibility === "partial" ? t("衣物未完整入鏡","Garment not fully visible") : t("需補拍的衣物區域","Needs a clearer photo"));
    }
    const record = item.record || {};
    const kind = record.category && record.category !== "other" ? categories[record.category] : t("衣物","garment");
    return sentenceCase(record.subtype || [record.colors?.[0], kind?.toLocaleLowerCase()].filter(Boolean).join(" "));
  }
  function errorMessage(data, fallback) {
    const detail = data?.detail;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) return detail.map(x => [Array.isArray(x.loc) ? x.loc.filter(p => p !== "body").join(" / ") : "", x.msg || t("資料格式不正確","Invalid data format")].filter(Boolean).join(": ")).join("；");
    return typeof data?.message === "string" ? data.message : fallback;
  }

  async function api(path, options = {}) {
    const protectedRequest = /^\/(?:api|media)\//.test(path);
    if (protectedRequest && (!account.ready || account.blocked)) throw new Error(t("請先確認登入狀態；本頁草稿仍保留。","Please confirm login status; drafts are preserved."));
    if (protectedRequest && account.mode === "hosted" && (!await verifyAccountSession() || account.blocked)) throw new Error(t("登入狀態已變更，請先重新登入。","Login status changed. Please sign in again."));
    const epoch = account.epoch;
    const expectedAccount = account.mode === "hosted" ? account.user?.id : null;
    const controller = new AbortController();
    account.controllers.add(controller);
    const timeout = setTimeout(() => controller.abort(), options.body instanceof FormData ? 120000 : 30000);
    const headers = new Headers(options.headers || {});
    if (options.body && !(options.body instanceof FormData)) headers.set("Content-Type", "application/json");
    const mutation = !["GET", "HEAD"].includes((options.method || "GET").toUpperCase());
    if (mutation && (account.mode === "hosted" || path.startsWith("/auth/"))) headers.set("X-Rewear-Request", "1");
    if (mutation && account.mode === "hosted" && account.csrf) headers.set("X-CSRF-Token", account.csrf);
    const { responseType, ...requestOptions } = options;
    try {
      const response = await fetch(path, { ...requestOptions, credentials: "same-origin", headers, signal: controller.signal });
      if (response.status === 401 && ["/auth/password", "/auth/logout"].includes(path) && account.user) {
        try { acceptSession(await api("/auth/session")); }
        catch (_) { blockAccount("無法確認登入狀態。草稿仍保留，請重新登入同一帳號後繼續。"); }
      }
      if (protectedRequest && account.mode === "hosted") {
        if (epoch !== account.epoch || account.blocked) throw new Error(t("帳號已變更，這個回應未套用。","Account changed; response not applied."));
        if (response.status === 401) {
          blockAccount("登入已失效。本頁尚未儲存的草稿仍保留，重新登入同一帳號後可繼續。");
          throw new Error(t("登入已失效；請重新登入同一帳號，本頁草稿仍保留。","Session expired; sign in again. Drafts preserved."));
        }
        const responseAccount = response.headers.get("X-Rewear-Account");
        if (responseAccount !== expectedAccount) {
          if (responseAccount) accountChanged();
          else blockAccount("無法確認服務回應所屬帳號，已暫停載入。草稿仍保留；請重新登入，若持續發生請聯絡管理者。");
          throw new Error(t("服務回應的帳號無法確認，資料未套用。","Cannot confirm account; data not applied."));
        }
      }
      if (response.status === 204) return responseType === "cleanup" ? { cleanup: response.headers.get("X-Rewear-Cleanup") } : null;
      if (response.ok && responseType === "blob") {
        const blob = await response.blob();
        if (epoch !== account.epoch || account.blocked) throw new Error(t("帳號已變更，下載已停止。","Account changed; download stopped."));
        return blob;
      }
      const data = await response.json().catch(() => null);
      if (protectedRequest && (epoch !== account.epoch || account.blocked)) throw new Error(t("帳號已變更，這個回應未套用。","Account changed. This response was not applied."));
      if (!response.ok) {
        const quota = account.mode === "hosted" && (response.status === 507 || response.status === 429 && typeof data?.detail === "string" && data.detail.includes("Account job limit reached"));
        if (quota) {
          storage.quota = response.status === 507 ? "media" : "jobs";
          renderStorage();
        }
        throw new Error(errorMessage(data, `${t("服務暫時無法完成請求","Service temporarily unable to complete request")} (${response.status}).`) + (quota ? ` ${t("可前往處理紀錄移除不需要的工作，或在帳號管理查看用量與清理。","Remove unneeded jobs in History, or check usage in Account settings.")}` : response.status === 429 ? ` ${t("請稍候再試，避免連續送出。","Please wait before retrying.")}` : ""));
      }
      if (protectedRequest && mutation && ["/api/jobs", "/api/batches", "/api/generate"].includes(path)) { storage.quota = null; renderStorage(); }
      if (data === null) throw new Error(t("服務回傳了無法讀取的資料，請稍後重試。","Server returned unreadable data. Try again later."));
      return data;
    } catch (error) {
      if (error.name === "AbortError") throw new Error(t("等候服務回應逾時，請重新整理後再試。","Request timed out. Refresh and try again."));
      if (error instanceof TypeError) throw new Error(t("無法連線至衣序服務，請確認應用程式仍在執行。","Cannot connect to Rewear. Confirm the app is running."));
      throw error;
    } finally { clearTimeout(timeout); account.controllers.delete(controller); }
  }

  function showError(id, message) {
    const node = $(`#${id}`);
    if (!node) return;
    node.textContent = message || "";
    node.hidden = !message;
  }

  function toast(message, error = false) {
    clearTimeout(state.toastTimer);
    const region = $("#toast-region");
    region.innerHTML = `<div class="toast${error ? " error" : ""}">${icon(error ? "info" : "check")}<span>${escapeHTML(message)}</span></div>`;
    state.toastTimer = setTimeout(() => region.replaceChildren(), error ? 7500 : 4500);
  }

  async function withButton(button, label, task) {
    if (!button || button.disabled) return;
    const original = button.innerHTML;
    button.disabled = true;
    button.innerHTML = `<span class="spinner" aria-hidden="true"></span><span>${escapeHTML(label)}</span>`;
    try { return await task(); }
    finally { if (button.isConnected) { button.disabled = false; button.innerHTML = original; } }
  }

  function confirmDeletion(title, message) {
    $("#confirm-title").textContent = title;
    $("#confirm-message").textContent = message;
    $("#confirm-dialog").showModal();
    $("#confirm-cancel").focus();
    return new Promise(resolve => { state.confirmResolve = resolve; });
  }

  function finishConfirmation(value) {
    const resolve = state.confirmResolve;
    state.confirmResolve = null;
    $("#confirm-dialog").close();
    if (resolve) resolve(value);
  }

  function setView(view, scroll = false) {
    state.view = view === "history" ? "history" : "wardrobe";
    document.body.dataset.view = state.view;
    $("#wardrobe-view").hidden = state.view !== "wardrobe";
    $("#history-view").hidden = state.view !== "history";
    $$('[data-view]').forEach(button => {
      const active = button.dataset.view === state.view;
      button.classList.toggle("active", active);
      if (active) button.setAttribute("aria-current", "page");
      else button.removeAttribute("aria-current");
    });
    if (location.hash !== `#${state.view}`) history.replaceState(null, "", `#${state.view}`);
    if (scroll) $(".catalogue-panel").scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function queuedVideo(mode) {
    return mode !== "outfit" && state.files.some(entry => !entry.error && isVideoFile(entry.file));
  }

  function imageSearchService() {
    const configuration = state.health?.discovery?.image_search;
    const notice = typeof configuration?.notice === "string" ? configuration.notice.trim() : "";
    if (!state.health) return { label: "Checking", description: "Confirming search service configuration — check Models & Services for status.", notice };
    if (configuration?.enabled !== true) return { label: "Disabled", description: "Meta reads photo clues and runs text search + product matching. Google image search is not enabled — no images sent to Google.", notice };
    if (configuration.provider !== "google_vision" || configuration.configured !== true) return { label: "Not configured", description: "Google image search is enabled but not configured. No images will be sent to Google this time. Meta text search and product matching still available.", notice };
    return { label: "Configured (unverified)", description: "Search sends original garment photos to Google image search, then uses Meta text search and product matching. Configuration is complete but permissions and results are not verified.", notice };
  }

  function providerBlock(provider, mode, generation = false) {
    if (state.healthChecking) return t("正在確認模型連線…","Checking model connection…");
    if (!state.health) return t("無法確認模型連線，請重新檢查。","Cannot confirm model connection. Please recheck.");
    if (provider === "local" && state.health.local?.available !== true) return t("本機分析目前未連線。請啟動 Ollama 後重新檢查。","Local analysis offline. Start Ollama and recheck.");
    if ((provider === "meta" || mode === "rack" || mode === "outfit" || generation || queuedVideo(mode)) && state.health.meta?.configured !== true) {
      return generation ? t("圖片生成需要先設定 Meta 服務。","Image generation requires Meta service setup.") : provider === "meta" ? `${t("Meta 尚未設定。請設定服務","Meta not configured. Set up the service")}${state.health.local?.available === true ? t("，或改用已連線的本機分析",", or switch to local analysis") : ""}.` : t("這個模式或影片需要 Meta 分割。請先設定 Meta，或改為單件照片。","This mode requires Meta segmentation. Set up Meta or use single-item photos.");
    }
    if (!generation && queuedVideo(mode) && state.health.ffmpeg !== true) return t("影片處理尚未就緒，請安裝 FFmpeg 或改選照片。","Video processing not ready. Install FFmpeg or select photos.");
    return "";
  }

  function updateProviderNote() {
    const meta = $("#provider-select").value === "meta";
    const mode = $("input[name=mode]:checked").value;
    const rack = mode === "rack";
    const outfit = mode === "outfit";
    $("#capture-guidance").hidden = !rack && !outfit;
    $("#capture-guidance span").textContent = outfit ? t("生活穿搭只接受照片。多人合照請在下方逐張框選目標人物；未框選會分析全圖。衣物屬性僅記錄可見細節，重建展示圖會另外標示 AI 推測。","Outfit mode accepts photos only. For group shots, select the target person below; otherwise the full image is analyzed. AI-inferred details are labeled separately.") : t("拉開衣架間距，讓每件衣物的領口、袖口與下襬完整入鏡。衣物重疊時，請改成逐件拍攝。","Space hangers so neckline, cuffs and hem are fully visible. Photograph overlapping items separately.");
    $("#reconstruction-option").hidden = !outfit;
    $("#upload-discovery-option").hidden = !outfit;
    $("#upload-discovery-description").textContent = `Check this to find purchase links — or search individual items later. ${imageSearchService().description} AI previews are never used as brand or product evidence.`;
    $("#upload-discovery-market").disabled = state.uploading || !$("#upload-discover").checked;
    const note = $("#provider-note");
    let message;
    if (rack || outfit) message = `${outfit ? t("生活穿搭照片","Outfit photos") : t("多件／影片","Multi-item / video")} ${t("會送至 Meta SAM 分割，再由","will be segmented by Meta SAM, then analyzed by")}${meta ? " Spark" : t(" 本機 Glimmer"," local Glimmer")}.`;
    else if (queuedVideo(mode)) message = meta ? t("照片與影片會送至 Meta；影片使用 SAM 分割，再由 Spark 分析。","Photos and videos sent to Meta; videos use SAM segmentation, then Spark analysis.") : t("照片使用本機分析；清單中的影片會送至 Meta SAM 分割，再由本機 Glimmer 分析。","Photos analyzed locally; videos use Meta SAM segmentation with local Glimmer.");
    else message = meta ? t("衣物照片會送至 Meta Spark 進行雲端分析。","Garment photos sent to Meta Spark for cloud analysis.") : account.mode === "hosted" ? t("使用伺服器上的 Ollama 分析；照片會上傳至衣序伺服器。","Using server-side Ollama; photos uploaded to Rewear server.") : t("本機模式使用 Ollama，照片留在這台裝置。","Local mode uses Ollama. Photos stay on this device.");
    note.textContent = message;
    note.classList.toggle("cloud", meta || rack || outfit || queuedVideo(mode));
    const checking = state.healthChecking || !state.health;
    const localReady = state.health?.local?.available === true;
    const metaConfigured = state.health?.meta?.configured === true;
    const localOption = $('#provider-select option[value="local"]');
    const metaOption = $('#provider-select option[value="meta"]');
    localOption.textContent = `Glimmer · ${t("本機分析","Local")}${checking ? t("（待確認）"," (checking)") : localReady ? t("（已連線）"," (connected)") : t("（未連線）"," (offline)")}`;
    metaOption.textContent = `Spark · Meta ${t("雲端","Cloud")}${checking ? t("（待確認）"," (checking)") : metaConfigured ? t("（已設定）"," (configured)") : t("（未設定）"," (not configured)")}`;
    localOption.disabled = checking || !localReady;
    metaOption.disabled = checking || !metaConfigured;
    const blocked = providerBlock(meta ? "meta" : "local", mode);
    const generationBlocked = providerBlock(meta ? "meta" : "local", mode, true);
    $("#provider-recovery").hidden = !blocked;
    $("#provider-readiness").textContent = blocked;
    const canSwitch = !meta && !checking && !localReady && metaConfigured;
    $("#provider-use-cloud").hidden = !canSwitch;
    $("#provider-use-cloud").disabled = state.uploading || state.generating;
    $("#provider-cloud-disclosure").hidden = !canSwitch;
    $("#provider-recheck").disabled = state.healthChecking || state.uploading;
    $("#provider-recheck").textContent = state.healthChecking ? t(t("正在檢查…","Checking…"),"Checking…") : t("重新檢查連線","Recheck connection");
    $("#upload-button").disabled = state.uploading || !!blocked;
    $("#generate-button").disabled = state.generating || !!generationBlocked;
    $("#generate-availability").hidden = !generationBlocked;
    $("#generate-availability").textContent = generationBlocked;
    renderFileQueue();
  }

  async function refreshHealth(force = false) {
    if (!account.ready || account.blocked) return;
    const request = ++state.healthRequest;
    state.healthChecking = true;
    renderHealth();
    updateProviderNote();
    try {
      const health = await api(`/api/health${force === true ? "?refresh=true" : ""}`);
      if (request !== state.healthRequest) return;
      state.health = health;
      state.healthError = "";
      state.lastHealthAt = Date.now();
    } catch (error) {
      if (request !== state.healthRequest) return;
      state.healthError = error.message;
      state.health = null;
    }
    if (request !== state.healthRequest) return;
    state.healthChecking = false;
    renderHealth();
    updateProviderNote();
    const item = state.items.find(entry => entry.id === state.editingItemId);
    if (item) { updateItemReconstruction(item); updateItemDiscovery(item); }
  }

  function renderHealth() {
    const health = state.health;
    const dot = $("#health-dot");
    let summary = t("服務未連線","Service offline");
    dot.className = "status-dot error";
    if (state.healthChecking) { summary = t("正在檢查連線","Checking connection"); dot.className = "status-dot checking"; }
    else if (health) {
      if (health.local?.available) { summary = t("本機模型就緒","Local model ready"); dot.className = "status-dot ready"; }
      else if (health.meta?.configured) { summary = t("Meta 已設定","Meta configured"); dot.className = "status-dot"; }
      else { summary = t("模型尚未就緒","Models not ready"); dot.className = "status-dot"; }
    }
    $("#health-summary").textContent = summary;
    $("#health-button").setAttribute("aria-label", `${summary} — ${t("查看模型與服務狀態","view model & service status")}`);
    if (health?.limits) {
      $("#file-help").textContent = `JPG · PNG · WebP · MP4 · MOV · WebM · ${health.limits.upload_mb} MB ${t("每檔","per file")}`;
      $("#queue-limits").textContent = `${t("最多","Up to")} ${health.limits.batch_files || 32} ${t("個檔案","files")} · ${health.limits.upload_mb} MB ${t("每檔","each")} · ${health.limits.batch_upload_mb || 256} MB ${t("合計","total")}`;
    }
    const content = $("#status-dialog-body");
    if (!health) {
      content.innerHTML = `<p class="inline-error">${escapeHTML(state.healthError || t("正在檢查服務…","Checking service…"))}</p>`;
      return;
    }
    const local = health.local || {};
    const limits = health.limits || {};
    const usage = health.model_usage || {};
    const usageCost = Number.isFinite(usage.estimated_usd) && usage.estimated_usd >= 0
      ? usage.estimated_usd.toLocaleString("en-US", { style: "currency", currency: "USD", minimumFractionDigits: 2, maximumFractionDigits: 4 })
      : "—";
    const usageDetail = Number.isSafeInteger(usage.event_count) && usage.event_count > 0
      ? `${usage.event_count.toLocaleString(_locale)} ${t("次模型呼叫","model calls")} · ${Number(usage.generated_images || 0).toLocaleString(_locale)} ${t("張生成圖","generated images")} · ${Number(usage.web_search_queries || 0).toLocaleString(_locale)} ${t("次網路搜尋","web searches")}${usage.token_usage_complete === false ? t("；部分舊回應沒有 token 用量，金額是下限"," — some older responses lack token usage; amount is a lower bound") : ""}`
      : t("尚無已記錄的付費模型用量。","No recorded paid model usage.");
    const imageSearch = imageSearchService();
    content.innerHTML = `
      <div class="health-row"><h3>${t("Glimmer · 本機衣物分析","Glimmer · Local Analysis")}</h3><span class="health-value${local.available ? " ready" : ""}">${local.available ? t("已連線","Connected") : t("未連線","Disconnected")}</span><p>${escapeHTML(local.model || t("未指定模型","No model specified"))}${local.error ? `<br>${escapeHTML(local.error)}` : ""}</p></div>
      <div class="health-row"><h3>${t("Meta · Spark / SAM / 圖片生成","Meta · Spark / SAM / Image Generation")}</h3><span class="health-value${health.meta?.configured ? " ready" : ""}">${health.meta?.configured ? t("已設定","Configured") : t("未設定","Not configured")}</span><p>${health.meta?.configured ? t("已設定服務憑證；實際可用狀態以每次處理結果為準。","Service credentials configured. Availability confirmed per processing run.") : t("請在伺服器設定 Meta API key 後重新啟動。單件本機分析與範例匯入不需要 Meta。","Set Meta API key in server config and restart. Single-item local analysis and samples do not require Meta.")}</p></div>
      <div class="health-row"><h3>${t("本衣物室的模型成本估算","Estimated Model Cost")}</h3><span class="health-value">${escapeHTML(usageCost)}</span><p>${escapeHTML(usageDetail)} ${t("依 Meta Standard 公開單價估算，免費額度與實際帳單以供應商為準；不保存提示詞或照片內容。","Estimated at Meta Standard public rates. Free tier and actual billing per provider. No prompts or photo content stored.")}</p></div>
      <div class="health-row"><h3>Brand &amp; Shopping Search</h3><span class="health-value${health.discovery?.available ? " ready" : ""}">${health.discovery?.available ? "Configured" : "Not ready"}</span><p>${health.discovery?.available ? "Outfit photos can auto-search after analysis. Also searchable per garment card. Only original photos are used for brand evidence." : "Requires configured Meta brand search service. You can still edit and confirm garment info."}</p></div>
      <div class="health-row" id="image-search-health"><h3>Google · Image Product Index</h3><span class="health-value">${escapeHTML(imageSearch.label)}</span><p>${escapeHTML(imageSearch.description)}${imageSearch.notice ? `<br>${escapeHTML(imageSearch.notice)}` : ""}</p></div>
      <div class="health-row"><h3>${t("FFmpeg · 影片處理","FFmpeg · Video Processing")}</h3><span class="health-value${health.ffmpeg ? " ready" : ""}">${health.ffmpeg ? t("可用","Available") : t("未安裝","Not installed")}</span><p>${health.ffmpeg ? t("可將影片轉成衣物分割使用的影格。","Converts video into frames for garment segmentation.") : t("目前可整理照片；處理影片需先安裝 FFmpeg 與 ffprobe。","Photos can be processed now. Video requires FFmpeg and ffprobe.")}</p></div>
      <div class="health-limits"><div><strong>${escapeHTML(limits.upload_mb ?? "—")}<small> MB</small></strong>${t("單次上傳","per upload")}</div><div><strong>${escapeHTML(limits.video_seconds ?? "—")}<small> ${t("秒","sec")}</small></strong>${t("影片長度","video length")}</div><div><strong>${escapeHTML(limits.max_items ?? "—")}<small> ${t("件","items")}</small></strong>${t("單次衣物上限","per batch")}</div></div>`;
  }

  function fileError(file) {
    const max = state.health?.limits?.upload_mb || 50;
    if (!file.size) return t("檔案是空的，請選擇有內容的照片或影片。","File is empty. Please select a photo or video with content.");
    if (file.size > max * 1024 * 1024) return t(`檔案超過 ${max} MB。`,`File exceeds ${max} MB.`);
    if (!isVideoFile(file) && !/^image\/(jpeg|png|webp)$/i.test(file.type) && !/\.(jpe?g|png|webp)$/i.test(file.name)) return t("不支援此格式，請使用 JPG、PNG、WebP 或 MP4、MOV、WebM。","Unsupported format. Please use JPG, PNG, WebP, MP4, MOV or WebM.");
    return "";
  }

  function setFile(file) {
    state.files.forEach(entry => { if (entry.url) URL.revokeObjectURL(entry.url); });
    state.files = [];
    state.uploadClientId = null;
    state.uploadFingerprint = "";
    $("#file-input").value = "";
    if (file) addFiles([file]);
    else { state.file = null; state.fileURL = null; updateProviderNote(); }
  }

  function addFiles(files) {
    if (state.uploading) return;
    showError("upload-error", "");
    const max = state.health?.limits?.batch_files || 32;
    let rejected = 0;
    for (const file of files) {
      if (state.files.length >= max) { rejected++; continue; }
      const error = fileError(file);
      state.files.push({ id: crypto.randomUUID(), file, url: error ? "" : URL.createObjectURL(file), selection: null, error, serverError: "" });
    }
    if (rejected) showError("upload-error", `${t("最多","Max")} ${max} ${t("個檔案；另有","files;")} ${rejected} ${t("個未加入，請分批整理。","not added. Upload in batches.")}`);
    if (state.files.length === 1 && isVideoFile(state.files[0].file) && $('input[name="mode"]:checked').value !== "outfit") $('input[name="mode"][value="rack"]').checked = true;
    $("#file-input").value = "";
    updateProviderNote();
  }

  async function captureFiles(selection) {
    const files = [...selection];
    if (state.uploading || !files.length) return;
    setFile(null);
    addFiles(files);
    if (state.healthChecking || !state.health) await refreshHealth(true);
    if (state.files.length && !state.uploading) $("#upload-form").requestSubmit();
  }

  // A photo handed over by the "Shop this look" browser extension (a JPEG, PNG or WebP data URL).
  function fileFromDataUrl(value, name) {
    const match = typeof value === "string" ? /^data:(image\/(?:jpeg|png|webp));base64,([A-Za-z0-9+/]+={0,2})$/.exec(value) : null;
    if (!match) return null;
    const binary = atob(match[2]);
    const bytes = new Uint8Array(binary.length);
    for (let index = 0; index < binary.length; index++) bytes[index] = binary.charCodeAt(index);
    const fallback = `photo.${match[1] === "image/jpeg" ? "jpg" : match[1].slice(6)}`;
    return new File([bytes], typeof name === "string" && /^[\w.-]{1,120}$/.test(name) ? name : fallback, { type: match[1] });
  }

  function removeQueuedFile(id) {
    if (state.uploading) return;
    const entry = state.files.find(entry => entry.id === id);
    if (entry?.url) URL.revokeObjectURL(entry.url);
    state.files = state.files.filter(entry => entry.id !== id);
    showError("upload-error", "");
    updateProviderNote();
  }

  function renderFileQueue() {
    const mode = $('input[name="mode"]:checked').value;
    const region = $("#file-queue");
    const total = state.files.reduce((sum, entry) => sum + entry.file.size, 0);
    const maxTotal = state.health?.limits?.batch_upload_mb || 256;
    state.file = state.files[0]?.file || null;
    state.fileURL = state.files[0]?.url || null;
    region.hidden = !state.files.length;
    region.innerHTML = state.files.length ? `<div class="queue-summary"><strong>${state.files.length} ${t("個待上傳","to upload")}</strong><span>${(total / 1024 / 1024).toFixed(1)} / ${maxTotal} MB</span></div>${total > maxTotal * 1024 * 1024 ? `<p class="inline-error">${t("合計超過","Total exceeds")} ${maxTotal} MB. ${t("請移除部分檔案再上傳。","Remove some files.")}</p>` : ""}<ol>${state.files.map(entry => {
      const video = isVideoFile(entry.file);
      const error = entry.serverError || entry.error;
      return `<li class="queue-item${error ? " has-error" : ""}" data-queue-id="${entry.id}"><div class="queue-thumb">${entry.url ? video ? `<video src="${entry.url}" muted preload="metadata" playsinline></video>` : `<img src="${entry.url}" alt="${escapeHTML(entry.file.name)} ${t("預覽","preview")}">` : icon("image")}</div><div class="queue-info"><strong>${escapeHTML(entry.file.name)}</strong><span>${(entry.file.size / 1024 / 1024).toFixed(1)} MB${entry.selection ? ` · ${t("已框選人物","person selected")}` : ""}</span>${error ? `<p class="queue-error">${escapeHTML(error)}</p>` : mode === "outfit" && video ? '<p class="queue-error">' + t('生活穿搭不支援影片；此檔會略過，其他照片仍可處理。','Outfit mode doesn\'t support video; this file will be skipped.') + '</p>' : ""}${mode === "outfit" && !video && !entry.error ? `<button class="text-button" type="button" data-person-file="${entry.id}"${state.uploading ? " disabled" : ""}>${entry.selection ? t("調整人物框選","Adjust selection") : t("框選目標人物","Select person")} ${icon("plus")}</button>` : ""}</div><button class="icon-button queue-remove" type="button" data-remove-file="${entry.id}" aria-label="${t("移除","Remove")} ${escapeHTML(entry.file.name)}"${state.uploading ? " disabled" : ""}>${icon("close")}</button></li>`;
    }).join("")}</ol>` : "";
    const singleVideo = state.files.length === 1 && isVideoFile(state.file);
    $('input[name="mode"][value="single"]').disabled = state.uploading || singleVideo;
    $("#clear-file").hidden = !state.files.length;
    $("#clear-file").disabled = state.uploading;
    $("#upload-art").hidden = !!state.files.length;
    $("#file-preview").hidden = state.files.length !== 1 || !state.fileURL;
    $("#file-preview").replaceChildren();
    if (state.files.length === 1 && state.fileURL) {
      const media = document.createElement(singleVideo ? "video" : "img");
      media.src = state.fileURL;
      if (singleVideo) { media.muted = true; media.preload = "metadata"; media.playsInline = true; }
      else media.alt = t("選擇的衣物照片預覽","Selected garment preview");
      $("#file-preview").append(media);
    }
    $("#file-label").textContent = state.files.length === 1 ? state.file.name : state.files.length ? `${state.files.length} ${t("個檔案已選擇","files selected")}` : t("或把照片拖曳到這裡","or drop photos here");
    $("#upload-button").hidden = !state.files.length;
    if (!state.uploading) $("#upload-button span").textContent = state.files.length > 1 ? `${t("分析","Analyze")} ${state.files.length} ${t("張照片","photos")}` : t("分析照片","Analyze photo");
    $("#file-choose").innerHTML = `${state.files.length ? t("繼續加入檔案","Add more files") : t("或選擇檔案","or choose files")} ${icon("arrow")}`;
  }

  function openPersonSelection(id) {
    const entry = state.files.find(entry => entry.id === id);
    if (!entry?.url || isVideoFile(entry.file)) return;
    state.personFileId = id;
    state.personDraft = entry.selection ? [...entry.selection] : null;
    $("#person-filename").textContent = entry.file.name;
    $("#person-photo").src = entry.url;
    showError("person-error", "");
    renderPersonSelection();
    $("#person-dialog").showModal();
    $("#person-default").focus({ preventScroll: true });
    $("#person-dialog").scrollTop = 0;
  }

  function renderPersonSelection() {
    const box = $("#person-box");
    const coords = state.personDraft;
    box.hidden = !coords;
    if (coords) {
      box.style.left = `${coords[0] * 100}%`;
      box.style.top = `${coords[1] * 100}%`;
      box.style.width = `${(coords[2] - coords[0]) * 100}%`;
      box.style.height = `${(coords[3] - coords[1]) * 100}%`;
    }
    ["left", "top", "right", "bottom"].forEach((name, index) => { $(`#person-${name}`).value = coords ? (coords[index] * 100).toFixed(1) : ""; });
    $("#person-clear").disabled = !coords;
  }

  function validPersonBox(coords) {
    return coords.length === 4 && coords.every(value => Number.isFinite(value) && value >= 0 && value <= 1) && coords[2] - coords[0] >= 0.01 && coords[3] - coords[1] >= 0.01;
  }

  function savePersonSelection() {
    const entry = state.files.find(entry => entry.id === state.personFileId);
    if (!entry) { $("#person-dialog").close(); return; }
    const values = ["left", "top", "right", "bottom"].map(name => $(`#person-${name}`).value.trim());
    const coords = values.every(value => value === "") ? null : values.map(value => value === "" ? NaN : Number(value) / 100);
    if (coords && !validPersonBox(coords)) { showError("person-error", t("請填入 0–100 的百分比；右邊須大於左邊，下邊須大於上邊，至少相距 1%。","Enter 0–100 percentages; right > left, bottom > top, at least 1% apart.")); return; }
    entry.selection = coords;
    entry.serverError = "";
    $("#person-dialog").close();
    renderFileQueue();
  }

  function validateProvider(provider, mode, generation = false) {
    const message = providerBlock(provider, mode, generation);
    if (message) throw new Error(message);
  }

  async function acceptJob(job, message) {
    state.latestJobId = job.id;
    state.filter = { q: "", category: "", review: "all", job: job.id, batch: "" };
    state.showUnclear = false;
    resetFilterControls();
    state.jobs = [job, ...state.jobs.filter(x => x.id !== job.id)];
    renderJobs();
    renderActiveJob();
    setView("wardrobe");
    toast(message);
    await refreshData(true);
    schedulePoll();
  }

  async function upload(event) {
    event.preventDefault();
    showError("upload-error", "");
    if (!state.files.length) { showError("upload-error", t("先選擇衣物照片或影片，再開始整理。","Select garment photos or videos first.")); $("#file-input").focus(); return; }
    const provider = $("#provider-select").value;
    const mode = $('input[name="mode"]:checked').value;
    const hasValidFile = state.files.some(entry => !entry.error && (mode !== "outfit" || !isVideoFile(entry.file)));
    if (!hasValidFile) { showError("upload-error", t("請先移除不適用的檔案，並加入至少一個可處理的檔案；生活穿搭只支援照片。","Remove incompatible files and add at least one valid one. Outfit mode only supports photos.")); return; }
    const maxTotal = state.health?.limits?.batch_upload_mb || 256;
    if (state.files.reduce((sum, entry) => sum + entry.file.size, 0) > maxTotal * 1024 * 1024) { showError("upload-error", `${t("合計檔案大小超過","Total file size exceeds")} ${maxTotal} MB. ${t("請分批整理。","Upload in batches.")}`); return; }
    try { validateProvider(provider, mode); }
    catch (error) { showError("upload-error", error.message); return; }
    const data = new FormData();
    const queued = [...state.files];
    const batch = queued.length > 1 || mode === "outfit";
    const selections = queued.map(entry => mode === "outfit" ? entry.selection : null);
    const concept = $("#concept-input").value.trim() || "garment";
    const reconstruct = mode === "outfit" && $("#upload-reconstruct").checked;
    const discover = mode === "outfit" && $("#upload-discover").checked;
    const discoveryMarket = $("#upload-discovery-market").value === "global" ? "global" : "TW";
    const fingerprint = JSON.stringify([queued.map(entry => entry.id), provider, mode, concept, selections, reconstruct, discover, discoveryMarket]);
    if (fingerprint !== state.uploadFingerprint || !state.uploadClientId) { state.uploadClientId = crypto.randomUUID(); state.uploadFingerprint = fingerprint; }
    queued.forEach(entry => data.append(batch ? "files" : "file", entry.file));
    data.append("provider", provider);
    data.append("mode", mode);
    data.append("concept", concept);
    data.append("reconstruct", String(reconstruct));
    data.append("discover", String(discover));
    data.append("discovery_market", discoveryMarket);
    if (batch) { data.append("selections", JSON.stringify(selections)); data.append("client_id", state.uploadClientId); }
    await withButton($("#upload-button"), t("正在上傳…","Uploading…"), async () => {
      state.uploading = true;
      $$("#file-input, #provider-select, #concept-input, #upload-reconstruct, #upload-discover, #upload-discovery-market, input[name=mode]").forEach(input => { input.disabled = true; });
      renderFileQueue();
      try {
        const result = await api(batch ? "/api/batches" : "/api/jobs", { method: "POST", body: data });
        if (batch) {
          const failures = new Map((result.errors || []).map(error => [error.index, error.error]));
          state.files = queued.filter((entry, index) => {
            if (failures.has(index)) { entry.serverError = failures.get(index); return true; }
            if (entry.url) URL.revokeObjectURL(entry.url);
            return false;
          });
          state.uploadFingerprint = "";
          state.uploadClientId = null;
          await acceptBatch(result, failures.size ? `${t("已建立批次；","Batch created;")} ${failures.size} ${t("個檔案未能加入，原因保留在上傳清單。","files could not be added.")}` : "");
        } else { setFile(null); await acceptJob(result, t("已開始整理，可隨時查看處理進度。","Processing started. Check progress anytime.")); }
        $(".catalogue-panel").scrollIntoView({ behavior: "smooth", block: "start" });
      } catch (error) { showError("upload-error", error.message); }
      finally {
        state.uploading = false;
        $$("#file-input, #provider-select, #concept-input, #upload-reconstruct, #upload-discover, #upload-discovery-market, input[name=mode]").forEach(input => { input.disabled = false; });
        $("#upload-discovery-market").disabled = !$("#upload-discover").checked;
        renderFileQueue();
      }
    });
    updateProviderNote();
  }

  async function importDemo() {
    await withButton($("#demo-button"), t("正在開啟…","Opening…"), async () => {
      try {
        const job = await api("/api/demo", { method: "POST" });
        state.filter = { q: "", category: "", review: "all", job: "", batch: "" };
        resetFilterControls();
        await acceptJob(job, t("歷史範例已匯入；這次沒有執行新的模型辨識。","Sample imported; no new model analysis was run."));
        $(".catalogue-panel").scrollIntoView({ behavior: "smooth", block: "start" });
      } catch (error) { toast(error.message, true); }
    });
  }

  async function generate(event) {
    event.preventDefault();
    showError("generate-error", "");
    const prompt = $("#generate-prompt").value.trim();
    if (prompt.length < 10) { showError("generate-error", t("請用至少 10 個字描述想生成的衣物。","Describe the garment in at least 10 characters.")); return; }
    const provider = $("#provider-select").value;
    const mode = $('input[name="mode"]:checked').value;
    try { validateProvider(provider, mode, true); }
    catch (error) { showError("generate-error", error.message); return; }
    await withButton($("#generate-button"), t("正在送出…","Submitting…"), async () => {
      state.generating = true;
      try {
        const job = await api("/api/generate", { method: "POST", body: JSON.stringify({ prompt, provider, mode, concept: $("#concept-input").value.trim() || "garment" }) });
        await acceptJob(job, t("已排入圖片生成，完成後會接著整理衣物。","Image generation queued; garment analysis will follow."));
        $(".catalogue-panel").scrollIntoView({ behavior: "smooth", block: "start" });
      } catch (error) { showError("generate-error", error.message); }
      finally { state.generating = false; }
    });
    updateProviderNote();
  }

  async function refreshData(force = false) {
    if (!account.ready || account.blocked) return;
    if (state.refreshing) {
      await state.refreshPromise;
      if (force) return refreshData(true);
      return;
    }
    state.refreshing = true;
    const epoch = account.epoch;
    let finishRefresh;
    state.refreshPromise = new Promise(resolve => { finishRefresh = resolve; });
    try {
      const results = await Promise.allSettled([api("/api/items"), api("/api/jobs"), api("/api/batches"), api("/api/garment-groups"), api("/api/duplicates")]);
      if (epoch !== account.epoch || account.blocked) return;
      const errors = [];
      state.referenceDataReady = [0, 1, 3].every(index => results[index].status === "fulfilled");
      let groupsChanged = false;
      if (results[3].status === "fulfilled") {
        state.groups = results[3].value.groups || [];
        const signature = JSON.stringify(state.groups);
        groupsChanged = signature !== state.groupSignature;
        state.groupSignature = signature;
      } else errors.push(results[3].reason.message);
      if (results[4].status === "fulfilled") { state.suggestions = results[4].value.suggestions || []; state.relatedError = ""; }
      else state.relatedError = results[4].reason.message;
      $("#duplicates-count").textContent = state.relatedError ? "—" : String(state.suggestions.length);
      if (results[0].status === "fulfilled") {
        const previous = new Map(state.items.map(item => [item.id, item.reconstruction?.status]));
        const previousDiscovery = new Map(state.items.map(item => [item.id, item.discovery?.status]));
        state.items = results[0].value.items || [];
        const signature = JSON.stringify(state.items);
        if (force || groupsChanged || signature !== state.itemSignature || !state.loaded) { state.itemSignature = signature; renderItems(); }
        renderCounts();
        const editing = state.items.find(item => item.id === state.editingItemId);
        if (editing) updateItemDiscovery(editing);
        for (const item of state.items) {
          if (activeStatuses.has(previous.get(item.id)) && item.reconstruction?.status === "completed") {
            if (acceptedReconstruction(item) && acceptedReconstructionReport(item.reconstruction.validation)) toast(t("展示圖已通過款式比對；仍請和原圖一起核對。","AI preview passed the style check — still compare it with your photo."));
            else if (readyPreview(item) && readyPreviewReport(item.reconstruction.validation)) toast(t("完整 AI 預覽已生成，包含推測細節，請與原圖一起核對。","Full AI preview ready — it includes inferred details, so compare it with your photo."));
            else toast(t("這張生成圖尚未通過款式核對，目錄仍以原圖為準。","The AI preview didn't pass the style check; your original photo is used."), true);
          }
          if (activeStatuses.has(previous.get(item.id)) && ["needs_review", "rejected"].includes(item.reconstruction?.status)) {
            if (readyPreview(item) && readyPreviewReport(item.reconstruction.validation)) toast(t("完整 AI 預覽已生成；推測部位與可見差異已標明，請查看原圖比對。","Full AI preview ready — inferred parts and visible differences are marked."));
            else if (item.reconstruction.status === "needs_review" && affordablePreview(item)) toast(t("AI 完整預覽已生成，請與原圖比對。","AI previews ready — compare them with your photo."));
            else toast(item.reconstruction.status === "rejected" ? t("生成圖與原圖有差異，已保留供比較，沒有採用為展示圖。","Generated image differs from the original; kept for comparison but not used as the display image.") : t("原圖或生成結果仍有細節無法確認，請查看核對說明。","Some details of the original or generated result could not be confirmed; please review the comparison notes."), true);
          }
          if (activeStatuses.has(previous.get(item.id)) && item.reconstruction?.status === "failed") toast(`${t("完整展示圖未能生成：","AI preview couldn't be generated: ")}${item.reconstruction.error || t("請稍後重新生成。","please try again later.")}`, true);
          if (activeStatuses.has(previousDiscovery.get(item.id)) && ["completed", "partial"].includes(item.discovery?.status)) toast(t("品牌與購買線索已更新，請核對原圖與目前商品頁面。","Shopping results updated — check them against your photo."));
          if (activeStatuses.has(previousDiscovery.get(item.id)) && item.discovery?.status === "failed") toast(`${t("品牌查找未能完成：","Shopping search didn't finish: ")}${item.discovery.error || t("請稍後重新查找。","please try again later.")}`, true);
        }
      } else errors.push(results[0].reason.message);
      if (results[1].status === "fulfilled") {
        const previous = new Map(state.jobs.map(job => [job.id, job.status]));
        state.jobs = results[1].value.jobs || [];
        const editing = state.items.find(item => item.id === state.editingItemId);
        if (editing) updateItemDiscovery(editing);
        const signature = JSON.stringify(state.jobs);
        if (force || signature !== state.jobSignature || !state.loaded) { state.jobSignature = signature; renderJobs(); renderActiveJob(); if (state.loaded) renderItems(); }
        if (!state.loaded && results[0].status === "fulfilled") { renderItems(); renderCounts(); }
        const batchJobIds = new Set((results[2].status === "fulfilled" ? results[2].value.batches || [] : state.batches).flatMap(batch => batch.job_ids || (batch.jobs || []).map(job => job.id)));
        for (const job of state.jobs) {
          const shownInBanner = state.view === "wardrobe" && state.filter.job === job.id;
          if (activeStatuses.has(previous.get(job.id)) && !isActive(job) && !batchJobIds.has(job.id) && !shownInBanner) toast(...finishedToast([job]));
        }
        if (state.selectedJobId && $("#job-dialog").open) {
          const job = state.jobs.find(x => x.id === state.selectedJobId);
          if (job) updateJobDialog(job);
        }
      } else errors.push(results[1].reason.message);
      const referenceItem = state.items.find(item => item.id === state.editingItemId);
      if (referenceItem) updateItemReconstruction(referenceItem);
      updateJobFollowups();
      if (results[2].status === "fulfilled") {
        const previous = new Map(state.batches.map(batch => [batch.id, batch.status]));
        state.batches = results[2].value.batches || [];
        const signature = JSON.stringify(state.batches);
        if (force || signature !== state.batchSignature || !state.loaded) { state.batchSignature = signature; renderBatches(); }
        for (const batch of state.batches) {
          const shownInBanner = state.view === "wardrobe" && state.filter.batch === batch.id;
          if (activeStatuses.has(previous.get(batch.id)) && !isActive(batch) && !shownInBanner) toast(...finishedToast(batch.jobs || []));
        }
        if (state.selectedBatchId && $("#batch-dialog").open) {
          const batch = state.batches.find(entry => entry.id === state.selectedBatchId);
          if (batch) renderBatchDialog(batch);
        }
      } else {
        $("#batches-region").innerHTML = `<p class="inline-error">${t("無法讀取批次紀錄：","Cannot load batch records: ")}${escapeHTML(results[2].reason.message)}</p>`;
      }
      renderActiveJob();
      renderResultsBanner();
      state.loaded = true;
      $("#items-region").setAttribute("aria-busy", "false");
      $("#jobs-region").setAttribute("aria-busy", "false");
      $("#service-error").hidden = errors.length === 0;
      $("#service-error-message").textContent = [...new Set(errors)].join(" ");
      if (results[0].status === "rejected" && !state.itemSignature) $("#items-region").innerHTML = `<div class="empty-state"><svg class="icon image-fallback" aria-hidden="true"><use href="#i-info"/></svg><h3>${t("衣物室暫時連不上","Wardrobe temporarily unavailable")}</h3><p>${t("請確認服務仍在執行，再按上方的「重試」。","Check the service is running, then press Retry.")}</p></div>`;
      if (results[1].status === "rejected" && !state.jobSignature) $("#jobs-region").innerHTML = `<div class="empty-state"><h3>${t("暫時無法讀取紀錄","Cannot load records")}</h3><p>${t("服務恢復後，已儲存的處理紀錄會再次顯示。","Saved records will appear once the service recovers.")}</p></div>`;
    } finally { state.refreshing = false; finishRefresh(); }
  }

  function schedulePoll() {
    clearTimeout(state.pollTimer);
    if (!account.ready || account.blocked) return;
    state.pollTimer = setTimeout(async () => {
      if (!document.hidden) {
        await refreshData();
        if (Date.now() - state.lastHealthAt > 60000) await refreshHealth();
      }
      schedulePoll();
    }, state.jobs.some(isActive) || state.batches.some(isActive) || state.items.some(item => isActive(item.reconstruction) || isActive(item.discovery)) ? 1800 : 12000);
  }

  function renderCounts() {
    const counts = resultCounts(representativeItems(state.items));
    $("#total-count").textContent = String(state.items.length);
    $("#usable-count").textContent = String(counts.usable);
    $("#attention-count").textContent = String(counts.retake + counts.unassessed);
    $("#pending-count").textContent = String(counts.pending);
    $("#reviewed-count").textContent = String(counts.reviewed);
    $("#attention-tab-count").textContent = String(counts.retake + counts.unassessed);
    $("#confirmation-count").textContent = String(counts.confirmation);
    $("#catalogue-footer-count").textContent = `${state.items.length} ${t("筆分析結果","results")} · ${counts.usable} ${t("件可整理衣物","usable")}${state.groups.length ? t("（同件來源合併計數）"," (merged sources)") : ""}`;
  }

  // Planning can locate one garment twice in a photo; show it once, keeping the larger, more complete view.
  function duplicatePieceIds(items) {
    const area = box => Math.max(0, box[2] - box[0]) * Math.max(0, box[3] - box[1]);
    const headNoun = subtype => String(subtype || "").toLowerCase().trim().split(/\s+/).pop();
    const key = item => [item.job_id, item.frame ?? "", item.record?.category, headNoun(item.record?.subtype)].join("|");
    const groups = new Map();
    for (const item of items) {
      if (needsAttention(item) || !Array.isArray(item.bbox) || item.bbox.length !== 4) continue;
      groups.set(key(item), [...(groups.get(key(item)) || []), item]);
    }
    const hidden = new Set();
    for (const group of groups.values()) {
      group.sort((a, b) => area(b.bbox) - area(a.bbox) || (b.record?.confidence || 0) - (a.record?.confidence || 0));
      group.forEach((kept, index) => {
        if (hidden.has(kept.id)) return;
        const colors = new Set((kept.record?.colors || []).map(color => String(color).toLowerCase()));
        for (const other of group.slice(index + 1)) {
          const otherColors = (other.record?.colors || []).map(color => String(color).toLowerCase());
          if (hidden.has(other.id) || !(otherColors.some(color => colors.has(color)) || (!colors.size && !otherColors.length))) continue;
          const overlap = area([Math.max(kept.bbox[0], other.bbox[0]), Math.max(kept.bbox[1], other.bbox[1]), Math.min(kept.bbox[2], other.bbox[2]), Math.min(kept.bbox[3], other.bbox[3])]);
          if (overlap >= 0.75 * Math.min(area(kept.bbox), area(other.bbox))) hidden.add(other.id);
        }
      });
    }
    return hidden;
  }
  function distinctItems(items) {
    const hidden = duplicatePieceIds(items);
    return hidden.size ? items.filter(item => !hidden.has(item.id)) : items;
  }

  function scopedJobIds() {
    if (state.filter.job) return new Set([state.filter.job]);
    if (!state.filter.batch) return null;
    const batch = state.batches.find(entry => entry.id === state.filter.batch);
    return new Set(batch?.job_ids || (batch?.jobs || []).map(job => job.id));
  }

  function filteredItems() {
    const query = state.filter.q.trim().toLocaleLowerCase();
    const scope = scopedJobIds();
    const candidates = distinctItems(scope ? state.items : representativeItems(state.items));
    return candidates.filter(item => {
      const record = item.record || {};
      if (scope ? !scope.has(item.job_id) : needsAttention(item)) return false;
      if (state.filter.category && (needsAttention(item) || record.category !== state.filter.category)) return false;
      if (state.filter.review === "pending" && (item.reviewed || !isUsable(item))) return false;
      if (state.filter.review === "reviewed" && !item.reviewed) return false;
      if (state.filter.review === "attention" && !needsAttention(item)) return false;
      if (state.filter.review === "confirmation" && !needsConfirmation(item)) return false;
      if (!query) return true;
      const searchable = needsAttention(item) ? [titleFor(item), ...qualityReasons(item)] : [record.subtype, record.brand, record.size_label, categories[record.category], record.category, record.pattern, record.material_guess, ...(record.colors || []), ...(record.defects || [])];
      return searchable.filter(Boolean).join(" ").toLocaleLowerCase().includes(query);
    });
  }

  function emptyIllustration() {
    return `<svg class="empty-illustration" viewBox="0 0 240 160" fill="none" aria-hidden="true"><path d="M22 137h196M40 136V28h160v108M35 28h170M33 136v5m174-5v5" stroke="currentColor" stroke-width="1.3"/><path d="M76 47a5 5 0 1 1 7 5v7l26 16H58l25-16M144 49a5 5 0 1 1 7 5v7l25 16h-49l24-16" stroke="currentColor" stroke-width="1.3"/><path d="m68 65-15 9 7 15 8-4v34h30V85l8 4 7-15-16-9c-4 8-24 8-29 0Z" fill="#e5eadd" stroke="currentColor" stroke-width="1.3"/><path d="M137 71h29l7 52h-22l-1-32-2 32h-20l9-52Z" fill="#e5eadd" stroke="currentColor" stroke-width="1.3"/><path d="m107 14 2-6m5 10 6-2M216 79v8m-4-4h8M24 65v5m-2.5-2.5h5" stroke="#b7896c" stroke-width="1.2"/><path d="M46 145h148" stroke="currentColor" opacity=".3"/></svg>`;
  }

  function renderItems() {
    const items = filteredItems();
    const scope = scopedJobIds();
    $("#visible-count").textContent = String(items.length);
    $("#wardrobe-view").classList.toggle("is-scoped", !!scope);
    $("#wardrobe-view").classList.toggle("is-empty", !scope && !state.items.some(item => !needsAttention(item)));
    renderResultsBanner();
    const region = $("#items-region");
    if (!items.length) {
      const searching = !scope && (state.filter.q || state.filter.category || state.filter.review !== "all");
      region.innerHTML = scope ? "" : `<div class="empty-state">${emptyIllustration()}<h3>${searching ? t("沒有符合條件的衣物。","No matching finds.") : t("還沒有任何衣物。","Nothing here yet.")}</h3><p>${searching ? t("試著換個關鍵字。","Try a different keyword.") : t("拍下一套穿搭，或先看看範例結果。","Snap an outfit above, or see how a result looks with a sample.")}</p>${searching ? `<button class="button button-secondary" data-action="clear-filters" type="button">${t("清除搜尋","Clear search")} ${icon("refresh")}</button>` : `<button class="button button-secondary" data-action="load-demo" type="button">${t("看看範例結果","See a sample result")} ${icon("arrow")}</button>`}</div>`;
      return;
    }
    const pieces = items.filter(item => !needsAttention(item));
    const unclear = items.filter(item => needsAttention(item) && !analysisFailed(item));
    const collapsed = pieces.length > 0 && unclear.length > 0 && !state.showUnclear;
    renderGrid(region, [...pieces, ...(collapsed ? [] : unclear), ...items.filter(analysisFailed)]);
    let toggle = region.querySelector(":scope > .unclear-toggle");
    if (pieces.length && unclear.length) {
      if (!toggle) {
        toggle = document.createElement("button");
        toggle.type = "button";
        toggle.className = "text-button unclear-toggle";
        toggle.dataset.action = "toggle-unclear";
        region.append(toggle);
      }
      toggle.innerHTML = `${unclear.length} ${t("個區域無法辨識成衣物", unclear.length === 1 ? "more area couldn't be identified" : "more areas couldn't be identified")} · ${collapsed ? t("顯示","Show") : t("隱藏","Hide")} ${icon("chevron")}`;
      toggle.setAttribute("aria-expanded", String(!collapsed));
    } else toggle?.remove();
  }

  // Keyed update: unchanged cards keep their DOM, so polling never reloads images or replays the entrance animation.
  function renderGrid(region, items) {
    let grid = region.querySelector(":scope > .item-grid");
    if (!grid) {
      grid = document.createElement("div");
      grid.className = "item-grid";
      region.replaceChildren(grid);
    }
    const existing = new Map([...grid.children].map(node => [node.dataset.key, node]));
    const nodes = items.map(item => {
      const html = renderCard(item);
      const current = existing.get(item.id);
      if (current?.dataset.html === html) { current.classList.add("is-settled"); return current; }
      const template = document.createElement("template");
      template.innerHTML = html;
      const node = template.content.firstElementChild;
      node.dataset.key = item.id;
      node.dataset.html = html;
      if (current) node.classList.add("is-settled");
      attachImageFallbacks(node);
      return node;
    });
    const keep = new Set(nodes);
    [...grid.children].forEach(child => { if (!keep.has(child)) child.remove(); });
    nodes.forEach((node, index) => { if (grid.children[index] !== node) grid.insertBefore(node, grid.children[index] || null); });
  }

  function jobThumbnail(job) {
    return safeMediaURL(job?.preview_url || (!isVideoURL(job?.source_url) ? job?.source_url : ""));
  }

  function renderResultsBanner() {
    const host = $("#results-banner");
    const scope = scopedJobIds();
    const jobs = scope ? state.jobs.filter(job => scope.has(job.id)) : [];
    const batch = state.filter.batch ? state.batches.find(entry => entry.id === state.filter.batch) : null;
    if (!scope || !jobs.length || jobs.some(isActive) || isActive(batch)) {
      host.hidden = true;
      host.replaceChildren();
      delete host.dataset.signature;
      return;
    }
    const items = distinctItems(state.items.filter(item => scope.has(item.job_id)));
    const pieces = items.filter(item => !needsAttention(item)).length;
    const unclear = items.filter(item => needsAttention(item) && !analysisFailed(item)).length;
    const failedItems = items.filter(analysisFailed);
    const failed = !pieces && !unclear && (failedItems.length > 0 || jobs.some(job => job.status === "failed"));
    const error = failedItems[0]?.error || jobs.find(job => job.error)?.error || t("分析服務沒有回傳結果。","The analysis service returned no result.");
    const source = jobs.length === 1 ? jobs[0].filename : `${jobs.length} ${t("張照片","photos")}`;
    const title = failed ? t("分析未完成","Analysis didn't finish") : pieces ? `${pieces} ${pieces === 1 ? t("件衣物","piece found") : t("件衣物","pieces found")}` : unclear ? t("需要更清楚的照片","Needs a clearer photo") : t("沒有找到衣物","No garments found");
    const detail = failed ? error : [
      pieces && unclear ? `${unclear} ${t("個區域無法辨識", unclear === 1 ? "area couldn't be identified" : "areas couldn't be identified")}` : "",
      failedItems.length ? `${failedItems.length} ${t("件未能分析","couldn't be analyzed")}` : "",
      !pieces && !unclear ? t("試試整套穿搭清楚入鏡的照片。","Try a photo where the whole outfit is clearly visible.") : "",
    ].filter(Boolean).join(" · ");
    const retryable = batch ? ["failed", "partial", "cancelled"].some(status => batch.counts?.[status] > 0) : jobs.some(job => retryableStatuses.has(job.status));
    const retry = (failed || failedItems.length) && retryable
      ? `<button class="button button-secondary" type="button" ${batch ? `data-batch-action="retry" data-id="${escapeHTML(batch.id)}"` : `data-job-action="retry" data-id="${escapeHTML(jobs[0].id)}"`}>${icon("refresh")}${t("重試","Try again")}</button>` : "";
    const thumbs = jobs.slice(0, 3).map(jobThumbnail).filter(Boolean);
    const html = `<div class="results-banner${failed ? " failed" : ""}"><div class="results-photos">${thumbs.map(url => `<img src="${escapeHTML(url)}" alt="" loading="lazy">`).join("")}</div><div class="results-copy"><span class="results-eyebrow">${t("最新結果","Latest result")}<span class="results-source"> · ${escapeHTML(source)}</span></span><strong>${escapeHTML(title)}</strong>${detail ? `<p>${escapeHTML(detail)}</p>` : ""}</div><div class="results-actions">${retry}<button class="text-button" data-action="clear-scope" type="button">${t("所有衣物","All finds")} ${icon("arrow")}</button></div></div>`;
    host.hidden = false;
    if (host.dataset.signature === html) return;
    host.dataset.signature = html;
    host.innerHTML = html;
    attachImageFallbacks(host);
  }

  function renderCard(item) {
    const record = item.record || {};
    const attention = needsAttention(item);
    const failed = analysisFailed(item);
    const reconstructed = acceptedReconstruction(item);
    const preview = readyPreview(item);
    const affordable = affordablePreview(item);
    const synthetic = reconstructed || preview || affordable;
    const imageURL = synthetic || safeMediaURL(item.image_url || item.source_url);
    const imageLabel = reconstructed ? t("AI 展示圖 · 已比對","AI Preview · Verified") : preview ? t("AI 預覽・含推測","AI Preview · Speculative") : affordable ? t("AI 完整預覽 · 已選","AI Preview · Selected") : "";
    const meta = failed ? t("分析失敗","Analysis failed") : attention ? t("需要更清楚的照片","Needs a clearer photo") : [record.category && record.category !== "other" ? categories[record.category] : t("衣物","Garment"), record.brand].filter(Boolean).join(" · ");
    const details = [record.pattern, record.material_guess].filter(Boolean).slice(0, 1);
    const label = failed ? t("分析失敗","Analysis failed") : needsRetake(item) ? t("需要補拍","Retake needed") : isUnassessed(item) ? t("無法判讀","Can't read") : item.reviewed ? t("已確認","Confirmed") : "";
    const group = groupFor(item);
    const description = failed ? `<p class="card-quality-reason">${escapeHTML(item.error)}</p>`
      : attention ? `<p class="card-quality-reason">${escapeHTML(qualityReasons(item)[0] || "")}</p><span class="card-quality-action">${t("查看原圖","View original photo")} ${icon("arrow")}</span>`
      : `<div class="card-details">${record.size_label ? `<span class="card-size">${escapeHTML(record.size_label)}</span>` : ""}<span>${escapeHTML((record.colors || []).slice(0, 2).join(", ") || t("顏色待確認","Color pending"))}</span>${details.length ? `<span class="detail-separator"></span><span>${escapeHTML(details[0])}</span>` : ""}</div>`;
    return `<article class="garment-card${attention ? " attention-card" : ""}"><button class="card-button" type="button" data-item-id="${escapeHTML(item.id)}" aria-label="${escapeHTML(titleFor(item))}${label ? `, ${label}` : ""}${synthetic ? `, ${imageLabel}` : ""}"><div class="garment-image">${imageURL ? `<img src="${escapeHTML(imageURL)}" alt="${escapeHTML(titleFor(item))}${synthetic ? `, ${imageLabel}` : ""}" loading="lazy" decoding="async">` : icon("hanger", "image-fallback")}${label ? `<span class="card-status${failed ? " failed" : item.reviewed ? " reviewed" : " attention"}">${item.reviewed ? icon("check") : icon("info")}${label}</span>` : ""}${synthetic ? `<span class="card-reconstruction${!reconstructed ? " preview" : ""}">${icon("spark")}${imageLabel}</span>` : ""}<span class="card-open">${icon("arrow")}</span></div><div class="card-meta"><span>${escapeHTML(meta)}</span></div><h3 class="card-title">${escapeHTML(titleFor(item))}</h3>${description}${isActive(item.reconstruction) ? `<span class="card-reconstruction-progress">${icon("spark")}${synthetic ? t("正在重新生成預覽","Regenerating preview") : t("正在生成完整預覽","Generating full preview")}</span>` : item.reconstruction?.status === "failed" ? `<span class="card-reconstruction-progress failed">${t("預覽生成失敗 · 可重試","Preview generation failed · Retry available")}</span>` : ""}${reconstructionNotice(item) ? `<span class="card-reconstruction-notice">${icon("info")}${escapeHTML(reconstructionNotice(item))}</span>` : ""}${group ? `<span class="card-sources">${icon("image")}${group.item_ids.length} ${t("個來源 · 已合併","sources · Merged")}</span>` : ""}</button>${cardActions(item)}</article>`;
  }

  function cardActions(item) {
    if (analysisFailed(item)) {
      const job = state.jobs.find(entry => entry.id === item.job_id);
      return job && retryableStatuses.has(job.status) ? `<button class="card-shop secondary" type="button" data-job-action="retry" data-id="${escapeHTML(job.id)}">${icon("refresh")}${t("重新分析","Try again")}</button>` : "";
    }
    if (needsAttention(item) || generatedSource(item)) return "";
    const [, url] = freeShoppingLinks(item, state.discoveryMarkets.get(item.id) || item.discovery?.market || defaultMarket())[0];
    return `<a class="card-shop" href="${escapeHTML(safePublicURL(url))}" target="_blank" rel="noopener noreferrer">${icon("search")}${t("搜尋同款","Shop similar")}${icon("external")}</a>${item.discovery ? cardShopping(item) : ""}`;
  }

  function cardShopping(item) {
    const discovery = item.discovery;
    const shopping = discovery?.result?.shopping;
    const offers = shopping?.version === 2 && Array.isArray(shopping.offers) ? shopping.offers.filter(offer => verifiedShoppingOffer(offer, item)) : [];
    const modelCount = offers.filter(offer => offer.match === "model_candidate").length;
    const similarCount = offers.length - modelCount;
    const counts = [modelCount ? `${modelCount} exact match${modelCount > 1 ? "es" : ""}` : "", similarCount ? `${similarCount} similar` : ""].filter(Boolean).join(", ");
    const checked = shopping?.version === 2 && Array.isArray(shopping.checked_candidates) ? shopping.checked_candidates : [];
    const rejected = checked.filter(candidate => candidate?.status === "rejected").length;
    const unverified = checked.filter(candidate => candidate?.status === "unverified").length;
    const unchecked = [rejected ? `${rejected} rejected` : "", unverified ? `${unverified} unverified` : ""].filter(Boolean).join(", ");
    const previousResult = discovery?.result ? [shoppingSearchSummary(shopping), counts || unchecked || "No verified candidates yet"].filter(Boolean).join(" · ") : "";
    const pending = state.pending.has(`discovery:${item.id}`);
    const active = isActive(discovery);
    const eligible = discoveryEligible(item);
    const available = state.health?.discovery?.available !== false;
    const canStart = !active && eligible && available && (!discovery || ["failed", "cancelled"].includes(discovery.status));
    const status = pending ? "Submitting…" : active ? (discovery.stage === "verifying" ? "Verifying products…" : discovery.status === "queued" ? "Queued for search" : "Searching…")
      : discovery?.status === "failed" ? "Search incomplete, retry available" : discovery?.status === "cancelled" ? "Search cancelled"
      : previousResult || (generatedSource(item) ? "AI-generated source — brand check unavailable" : !eligible ? "Confirm garment type to search" : !available ? "Search service not ready" : "Not searched yet");
    return `<div class="card-shopping${active ? " is-active" : ""}" aria-label="Shopping"><div><span class="card-shopping-title">${icon("search")}Shopping</span><p>${escapeHTML(status)}</p>${previousResult && (active || ["failed", "cancelled"].includes(discovery?.status)) ? `<small>Previous: ${escapeHTML(previousResult)}</small>` : ""}</div><button class="text-button" type="button" data-open-discovery="${escapeHTML(item.id)}"${canStart ? ' data-start-discovery="true"' : ""}${pending ? " disabled" : ""}>${canStart ? discovery ? "Retry Search" : "Find Where to Buy" : "View Shopping Results"}${icon("arrow")}</button></div>`;
  }

  function attachImageFallbacks(root) {
    $$("img", root).forEach(img => img.addEventListener("error", () => {
      const replacement = document.createElement("span");
      replacement.className = "image-unavailable";
      replacement.innerHTML = `${icon("image")}<span>Image unavailable</span>`;
      img.replaceWith(replacement);
    }, { once: true }));
  }

  function jobFollowupSummary(job) {
    const items = state.items.filter(item => item.job_id === job.id);
    const reconstructions = items.filter(item => item.reconstruction);
    const discoveries = items.filter(item => item.discovery);
    const active = items.some(item => isActive(item.reconstruction) || isActive(item.discovery));
    const lines = [];
    const summarize = (entries, task, labels) => Object.entries(labels).map(([status, label]) => {
      const count = entries.filter(item => item[task]?.status === status).length;
      return count ? `${count} ${label}` : "";
    }).filter(Boolean);
    if (reconstructions.length) {
      const parts = summarize(reconstructions, "reconstruction", { running: "generating", queued: "waiting", failed: "failed", cancelled: "cancelled" });
      const finished = reconstructions.filter(item => ["completed", "needs_review", "rejected"].includes(item.reconstruction.status));
      const compared = finished.filter(item => acceptedReconstruction(item)).length;
      const previews = finished.filter(item => !acceptedReconstruction(item) && readyPreview(item));
      const differences = previews.filter(item => item.reconstruction.preview_validation.preview.mismatch_warnings.length > 0).length;
      const needsReview = finished.length - compared - previews.length;
      if (compared) parts.push(`${compared} compared`);
      if (previews.length) parts.push(`${previews.length} generated (with inferences)`);
      if (differences) parts.push(`${differences} with known differences`);
      if (previews.length > differences) parts.push(`${previews.length - differences} to review`);
      if (needsReview) parts.push(`${needsReview} results to review`);
      if (reconstructions.some(item => !finished.includes(item) && (readyPreview(item) || acceptedReconstruction(item)))) parts.push("previous previews preserved");
      lines.push({ label: "Previews", text: parts.join(" · ") || "status pending" });
    }
    if (discoveries.length) {
      const parts = summarize(discoveries, "discovery", { running: "searching", queued: "waiting", completed: "done", partial: "partial", failed: "failed", cancelled: "cancelled" });
      const attempts = discoveries.reduce((count, item) => count + shoppingSearchAttempts(item.discovery.result?.shopping || {}).length, 0);
      const offers = discoveries.reduce((count, item) => {
        const shopping = item.discovery.result?.shopping;
        return count + (shopping?.version === 2 && Array.isArray(shopping.offers) ? shopping.offers.filter(offer => verifiedShoppingOffer(offer, item)).length : 0);
      }, 0);
      if (attempts) parts.push(`${attempts} searches run`);
      if (offers) parts.push(`${offers} products verified`);
      else if (discoveries.some(item => !isActive(item.discovery))) parts.push("no verified products yet");
      if (discoveries.some(item => !["completed", "partial"].includes(item.discovery.status) && item.discovery.result)) parts.push("previous results preserved");
      lines.push({ label: "Shopping", text: parts.join(" · ") || "status pending" });
    }
    return { active, lines, count: items.length || job.item_count || 0 };
  }

  function updateJobFollowup(host, job) {
    const summary = jobFollowupSummary(job);
    const hasTasks = summary.lines.length > 0;
    const show = summary.count > 0 && (hasTasks || host.dataset.history !== undefined);
    host.hidden = !show;
    if (!show) { host.replaceChildren(); delete host.dataset.signature; return; }
    const signature = JSON.stringify([summary, isActive(job)]);
    if (host.dataset.signature === signature) return;
    const focused = host.contains(document.activeElement) && document.activeElement.hasAttribute("data-job-items");
    host.dataset.signature = signature;
    const title = summary.active ? (isActive(job) ? "Previews & shopping search in progress" : "Analysis complete, follow-up tasks in progress") : "Previews & shopping search results";
    host.innerHTML = `${hasTasks ? `<div class="job-followup-status" role="status"><strong>${summary.active ? '<span class="spinner" aria-hidden="true"></span>' : ""}${title}</strong><dl>${summary.lines.map(line => `<div><dt>${line.label}</dt><dd>${escapeHTML(line.text)}</dd></div>`).join("")}</dl></div>` : ""}<button class="button button-secondary job-results-link" type="button" data-job-items="${escapeHTML(job.id)}">${hasTasks ? "View items & progress" : `View ${summary.count} results`}${icon("arrow")}</button>`;
    if (focused) $("[data-job-items]", host)?.focus({ preventScroll: true });
  }

  function updateJobFollowups() {
    $$("[data-job-followup]").forEach(host => {
      const job = state.jobs.find(entry => entry.id === host.dataset.jobFollowup);
      if (job) updateJobFollowup(host, job);
    });
  }

  function renderJobs() {
    $("#history-count").textContent = String(state.jobs.length);
    $("#job-count").textContent = String(state.jobs.length);
    $("#job-count").hidden = !state.jobs.length;
    const region = $("#jobs-region");
    if (!state.jobs.length) {
      region.innerHTML = `<div class="empty-state">${emptyIllustration()}<h3>${t("下一次整理，從這裡開始。","Your next session starts here.")}</h3><p>${t("上傳照片、影片或匯入歷史範例後，可以在這裡查看處理進度與原始檔案。","Upload photos, videos or samples to see processing progress here.")}</p></div>`;
      return;
    }
    region.innerHTML = `<div class="job-list">${state.jobs.map(job => {
      const thumb = safeMediaURL(job.preview_url || (!isVideoURL(job.source_url) ? job.source_url : ""));
      const info = job.is_demo ? t("歷史範例 · 未執行新辨識","Sample · no new analysis") : `${providerName(job.provider)} · ${modeName(job.mode)}`;
      return `<article class="job-history-entry"><button class="job-row" type="button" data-job-id="${escapeHTML(job.id)}" aria-label="${t("查看","View")} ${escapeHTML(job.filename)} ${t("處理詳情","details")}"><span class="job-thumbnail">${thumb && !isVideoURL(thumb) ? `<img src="${escapeHTML(thumb)}" alt="" loading="lazy">` : icon(job.media_type === "video" ? "video" : "image")}</span><span class="job-row-info"><span class="job-row-title">${escapeHTML(job.filename)}</span><span class="job-row-meta"><span>${escapeHTML(info)}</span><span>${escapeHTML(jobResultSummary(job))}</span></span>${job.error ? `<span class="job-row-error">${escapeHTML(job.error)}</span>` : ""}${isActive(job) ? `<span class="progress-track"><span class="progress-bar" style="width:${percentage(job.progress)}%"></span></span>` : ""}</span><span class="job-row-end"><span class="status-pill ${escapeHTML(job.status)}">${escapeHTML(statuses[job.status] || job.status)}${isActive(job) ? ` ${percentage(job.progress)}%` : ""}</span><small>${escapeHTML(formatDate(job.created_at))}</small></span></button><div class="job-followup" data-job-followup="${escapeHTML(job.id)}" data-history hidden></div></article>`;
    }).join("")}</div>`;
    updateJobFollowups();
    attachImageFallbacks(region);
  }

  // Rebuilding the card on every poll would restart the scan animation; only text and progress change in place.
  function renderAnalysisCard(region, key, jobs, title, progress, meta, cancel) {
    const thumb = jobs.map(jobThumbnail).find(Boolean) || "";
    const shell = `${key}|${thumb}|${jobs.length}`;
    if (region.dataset.shell !== shell) {
      region.dataset.shell = shell;
      region.innerHTML = `<div class="analysis-card"><div class="analysis-photo">${thumb ? `<img src="${escapeHTML(thumb)}" alt="">` : icon("image")}${jobs.length > 1 ? `<span class="analysis-count">+${jobs.length - 1}</span>` : ""}</div><div class="analysis-copy"><span class="analysis-eyebrow">${t("Meta AI 分析中","Analyzing with Meta AI")}</span><strong data-analysis-title></strong><div class="progress-track" role="progressbar" aria-label="${t("分析進度","Analysis progress")}" aria-valuemin="0" aria-valuemax="100"><div class="progress-bar"></div></div><div class="analysis-meta"><span data-analysis-meta></span><span data-analysis-cancel></span></div></div></div>`;
    }
    $("[data-analysis-title]", region).textContent = title;
    $("[data-analysis-meta]", region).textContent = meta;
    $(".progress-track", region).setAttribute("aria-valuenow", String(progress));
    $(".progress-bar", region).style.width = `${progress}%`;
    const slot = $("[data-analysis-cancel]", region);
    if (slot.dataset.html !== cancel) { slot.dataset.html = cancel; slot.innerHTML = cancel; }
  }

  function renderActiveJob() {
    const batchJobIds = new Set(state.batches.filter(isActive).flatMap(batch => batch.job_ids || (batch.jobs || []).map(job => job.id)));
    const active = state.jobs.filter(job => isActive(job) && !batchJobIds.has(job.id));
    const job = active.find(x => x.id === state.latestJobId) || active[0];
    const region = $("#active-job");
    region.hidden = !job;
    if (!job) { region.replaceChildren(); delete region.dataset.shell; return; }
    const progress = percentage(job.progress);
    renderAnalysisCard(region, job.id, [job], `${stageName(job)}…`, progress, `${progress}% · ${job.filename}${active.length > 1 ? ` · +${active.length - 1}` : ""}`, `<button class="text-button" type="button" data-job-action="cancel" data-id="${escapeHTML(job.id)}"${state.pending.has(`cancel:${job.id}`) ? " disabled" : ""}>${t("取消","Cancel")}</button>`);
  }

  function fileCount(batch) {
    const count = batch.file_count ?? batch.job_count ?? batch.jobs?.length ?? 0;
    return `${count} ${t("個檔案", count === 1 ? "file" : "files")}`;
  }

  function batchSummary(batch) {
    const counts = batch.counts || {};
    return [fileCount(batch), counts.completed ? `${counts.completed} ${t("已完成","done")}` : "", counts.running ? `${counts.running} ${t("處理中","running")}` : "", counts.queued ? `${counts.queued} ${t("等待中","queued")}` : "", counts.partial ? `${counts.partial} ${t("部分完成","partial")}` : "", counts.failed ? `${counts.failed} ${t("失敗","failed")}` : "", counts.cancelled ? `${counts.cancelled} ${t("已取消","cancelled")}` : "", batch.deleted_count > 0 ? `${batch.deleted_count} ${t("已移除","removed")}` : "", batch.errors?.length ? `${batch.errors.length} ${t("未能加入","not added")}` : ""].filter(Boolean).join(" · ");
  }

  function batchActions(batch) {
    const id = escapeHTML(batch.id);
    const busy = action => state.pending.has(`batch-${action}:${batch.id}`) ? " disabled" : "";
    const retry = (batch.counts?.failed || 0) + (batch.counts?.partial || 0) + (batch.counts?.cancelled || 0);
    return `${isActive(batch) ? `<button class="button button-secondary" type="button" data-batch-action="cancel" data-id="${id}"${busy("cancel")}>${icon("stop")}${t("取消整批未完成任務","Cancel all pending")}</button>` : ""}${!isActive(batch) && retry ? `<button class="button button-secondary" type="button" data-batch-action="retry" data-id="${id}"${busy("retry")}>${icon("refresh")}${t("重試","Retry")} ${retry} ${t("個未完成檔案","incomplete files")}</button>` : ""}`;
  }

  function renderBatches() {
    $("#batches-region").innerHTML = state.batches.length ? `<section class="batch-history" aria-label="${t("批次整理紀錄","Batch processing records")}"><div class="batch-heading"><span class="section-index">COLLECTIONS</span><h3>${t("這些照片，一起整理。","Photos processed together.")}</h3></div>${state.batches.map(batch => `<button class="batch-row" type="button" data-batch-id="${escapeHTML(batch.id)}"><span class="batch-symbol">${icon("image")}</span><span><strong>${escapeHTML(modeName(batch.mode || batch.jobs?.[0]?.mode || "single"))} · ${fileCount(batch)}</strong><small>${escapeHTML(batchSummary(batch))}</small><small>${escapeHTML(formatDate(batch.created_at, true))}</small></span><span class="status-pill ${escapeHTML(batch.status)}">${escapeHTML(statuses[batch.status] || batch.status)}${isActive(batch) ? ` ${percentage(batch.progress)}%` : ""}</span></button>`).join("")}</section>` : "";
    const active = state.batches.filter(isActive);
    const batch = active.find(entry => entry.id === state.latestBatchId) || active[0];
    const region = $("#active-batch");
    region.hidden = !batch;
    if (!batch) { region.replaceChildren(); delete region.dataset.shell; return; }
    const jobs = batch.jobs || [];
    const current = jobs.find(job => job.status === "running") || jobs.find(isActive);
    const done = jobs.filter(job => !isActive(job)).length;
    const progress = percentage(batch.progress);
    renderAnalysisCard(region, batch.id, jobs, current ? `${stageName(current)}…` : t("準備開始…","Getting ready…"), progress, jobs.length > 1 ? `${done} / ${jobs.length} ${t("張照片完成","photos done")}` : `${progress}% · ${jobs[0]?.filename || ""}`, `<button class="text-button" type="button" data-batch-action="cancel" data-id="${escapeHTML(batch.id)}"${state.pending.has(`batch-cancel:${batch.id}`) ? " disabled" : ""}>${t("取消","Cancel")}</button>`);
  }

  async function acceptBatch(batch, message) {
    state.latestBatchId = batch.id;
    state.batches = [batch, ...state.batches.filter(entry => entry.id !== batch.id)];
    if (batch.jobs) { const ids = new Set(batch.jobs.map(job => job.id)); state.jobs = [...batch.jobs, ...state.jobs.filter(job => !ids.has(job.id))]; }
    state.filter = { q: "", category: "", review: "all", job: "", batch: batch.id };
    state.showUnclear = false;
    resetFilterControls();
    renderBatches(); renderJobs(); renderActiveJob();
    setView("wardrobe");
    if (message) toast(message);
    await refreshData(true);
    schedulePoll();
  }

  async function openBatch(id) {
    state.selectedBatchId = id;
    $("#batch-dialog-body").dataset.signature = "";
    $("#batch-dialog-body").innerHTML = '<div class="loading-state"><span class="spinner"></span><p>${t("正在讀取整批進度…","Loading batch progress…")}</p></div>';
    $("#batch-dialog").showModal();
    $("#batch-dialog .dialog-close").focus({ preventScroll: true });
    try {
      const batch = await api(`/api/batches/${encodeURIComponent(id)}`);
      if (state.selectedBatchId === id && $("#batch-dialog").open) renderBatchDialog(batch);
    } catch (error) { if (state.selectedBatchId === id) $("#batch-dialog-body").innerHTML = `<p class="inline-error">${escapeHTML(error.message)}</p>`; }
  }

  function renderBatchDialog(batch) {
    const region = $("#batch-dialog-body");
    const signature = JSON.stringify([batch, [...state.pending]]);
    if (region.dataset.signature === signature) return;
    region.dataset.signature = signature;
    region.innerHTML = `<div class="dialog-heading"><span class="section-index">ONE COLLECTION</span><h2 id="batch-dialog-title">${escapeHTML(modeName(batch.mode || batch.jobs?.[0]?.mode || "single"))} · ${fileCount(batch)}</h2><p>${escapeHTML(formatDate(batch.created_at, true))}</p></div><div class="batch-status"><span class="status-pill ${escapeHTML(batch.status)}">${escapeHTML(statuses[batch.status] || batch.status)}</span><p>${escapeHTML(batchSummary(batch))}</p></div><div class="progress-track" role="progressbar" aria-label="${t("整批處理進度","Batch progress")}" aria-valuenow="${percentage(batch.progress)}" aria-valuemin="0" aria-valuemax="100"><div class="progress-bar" style="width:${percentage(batch.progress)}%"></div></div>${batch.errors?.length ? `<div class="job-warning"><strong>未能加入的檔案</strong>${batch.errors.map(error => `<p>${escapeHTML(error.filename)}：${escapeHTML(error.error)}</p>`).join("")}<p>這些檔案尚未建立任務，請修正後從上傳清單重新加入。</p></div>` : ""}<div class="batch-job-list">${(batch.jobs || []).map(job => `<button type="button" data-batch-job="${escapeHTML(job.id)}"><span><strong>${escapeHTML(job.filename)}</strong><small>${escapeHTML(isActive(job) ? stageName(job) : jobResultSummary(job))}</small>${job.error ? `<small class="queue-error">${escapeHTML(job.error)}</small>` : ""}</span><span class="status-pill ${escapeHTML(job.status)}">${escapeHTML(statuses[job.status] || job.status)}</span>${icon("arrow")}</button>`).join("")}</div><p class="helper-text">重試會建立新批次，只處理失敗、部分完成或已取消的檔案；既有分析結果仍會保留。</p><div class="job-detail-actions">${batchActions(batch)}</div>`;
  }

  async function batchAction(action, id, button) {
    const key = `batch-${action}:${id}`;
    if (state.pending.has(key)) return;
    state.pending.add(key);
    if (button) button.disabled = true;
    try {
      const batch = await api(`/api/batches/${encodeURIComponent(id)}/${action}`, { method: "POST" });
      if (action === "retry") { $("#batch-dialog").close(); await acceptBatch(batch, t("已建立新的重試批次，原有結果保留。","Retry batch created; existing results kept.")); }
      else { state.batches = state.batches.map(entry => entry.id === id ? batch : entry); renderBatches(); renderBatchDialog(batch); toast(t("已取消整批未完成任務，已完成的結果保留。","Cancelled; finished results are kept.")); }
      await refreshData(true);
      schedulePoll();
    } catch (error) { toast(error.message, true); }
    finally { state.pending.delete(key); if (button?.isConnected) button.disabled = false; }
  }

  function renderGroupSources(item) {
    const group = groupFor(item);
    if (!group) return "";
    return `<section class="group-sources" aria-label="${t("同件衣物的全部來源","All sources for this garment")}"><div><h3>${group.item_ids.length} 張來源，同一件衣物</h3><p>每張照片仍有自己的紀錄與確認狀態。切換來源前請先儲存修改。</p></div><div class="source-strip">${groupMembers(group).map(source => `<button type="button" data-group-item="${escapeHTML(source.id)}" aria-pressed="${source.id === item.id}"><img src="${escapeHTML(safeMediaURL(source.image_url || source.source_url))}" alt="${escapeHTML(titleFor(source))}"><span>${source.id === group.primary_item_id ? "主要紀錄" : "來源紀錄"}<small>${source.reviewed ? t("已人工確認","Manually confirmed") : needsConfirmation(source) ? "穿搭待確認" : needsAttention(source) ? "需核對" : "待確認"}</small></span></button>`).join("")}</div><button class="text-button" type="button" data-ungroup="${escapeHTML(group.id)}">取消合併，恢復分開展示</button></section>`;
  }

  function openDuplicates() {
    state.selectedSuggestionId = null;
    renderDuplicateList();
    $("#duplicates-dialog").showModal();
    $("#duplicates-dialog .dialog-close").focus({ preventScroll: true });
    $("#duplicates-dialog").scrollTop = 0;
  }

  function renderDuplicateList() {
    const region = $("#duplicates-body");
    if (state.relatedError) { region.innerHTML = `<p class="inline-error">${escapeHTML(state.relatedError)}</p><button class="button button-secondary" type="button" data-duplicates-refresh>${t("重新讀取","Reload")}</button>`; return; }
    const suggestions = state.suggestions.filter(suggestion => suggestion.item_ids?.every(id => state.items.some(item => item.id === id)));
    region.innerHTML = suggestions.length ? `<p class="duplicate-explainer">${t("比對分數僅供排序，不是同一件衣物的機率；請核對款式、圖案與細節。","Scores are for ranking only. Verify style, pattern and details.")}</p><div class="duplicate-list">${suggestions.map(suggestion => {
      const items = suggestion.item_ids.map(id => state.items.find(item => item.id === id));
      return `<button class="duplicate-pair" type="button" data-suggestion-id="${escapeHTML(suggestion.id)}"><span class="duplicate-thumbnails">${items.map(item => `<img src="${escapeHTML(safeMediaURL(item.image_url || item.source_url))}" alt="${escapeHTML(titleFor(item))}">`).join("")}</span><span><strong>${items.map(item => escapeHTML(titleFor(item))).join(" / ")}</strong><small>${(suggestion.reasons || []).map(escapeHTML).join(" · ") || t("有相似的可見特徵","Similar visible features")}</small><small>${t("比對分數","Score")} ${Number.isFinite(suggestion.score) ? suggestion.score.toFixed(2) : t("未提供","N/A")}</small></span>${icon("arrow")}</button>`;
    }).join("")}</div>` : `<div class="empty-state compact-empty">${icon("hanger", "image-fallback")}<h3>${t("目前沒有待核對的相似衣物。","No similar items to review.")}</h3><p>${t("不同照片整理完成後，相似結果會出現在這裡。已合併的來源可在衣物詳情查看。","Similar results appear here after processing. Merged sources are in item details.")}</p></div>`;
    attachImageFallbacks(region);
  }

  function reviewDuplicate(id) {
    const suggestion = state.suggestions.find(entry => entry.id === id);
    if (!suggestion) { renderDuplicateList(); return; }
    const items = suggestion.item_ids.map(itemId => state.items.find(item => item.id === itemId)).filter(Boolean);
    if (items.length < 2) { renderDuplicateList(); return; }
    state.selectedSuggestionId = id;
    const region = $("#duplicates-body");
    region.innerHTML = `<button class="text-button" type="button" data-duplicates-back>${t("返回相似衣物","Back to similar items")}</button><p class="duplicate-explainer">${t("選擇目錄要顯示的主要紀錄。其他照片、品質評估與編輯內容都會保留；合併不會自動確認衣物屬性。","Choose the primary record. Other photos, quality assessments and edits are kept. Merging does not confirm attributes.")}</p><form id="merge-form"><div class="merge-candidates">${items.map((item, index) => `<label class="merge-candidate"><input type="radio" name="primary_item_id" value="${escapeHTML(item.id)}"${index === 0 ? " checked" : ""}><span class="merge-choice">${t("保留為主要紀錄","Keep as primary")}</span><img src="${escapeHTML(safeMediaURL(item.image_url || item.source_url))}" alt="${escapeHTML(titleFor(item))}"><strong>${escapeHTML(titleFor(item))}</strong><span>${escapeHTML((item.record?.colors || []).join(", ") || t("顏色未確認","Colors unconfirmed"))} · ${item.reviewed ? t("已確認","Confirmed") : needsConfirmation(item) ? t("穿搭待確認","Outfit pending") : t("待確認","Pending")}</span><small>${escapeHTML(formatDate(item.created_at, true))}</small></label>`).join("")}</div><div class="duplicate-reasons"><strong>${t("為什麼放在一起比較","Why these are compared")}</strong><ul>${(suggestion.reasons || []).map(reason => `<li>${escapeHTML(reason)}</li>`).join("")}</ul><p>比對分數 ${Number.isFinite(suggestion.score) ? suggestion.score.toFixed(2) : "未提供"} · ${t("僅供排序，仍須人工核對。","For ranking only; manual review needed.")}</p></div><label class="review-checkbox"><input type="checkbox" id="merge-confirm" required><span>${t("我已核對，這些照片是同一件衣物。","I confirmed these photos are the same garment.")}<small>${t("合併後可隨時取消，所有來源紀錄都會保留。","Merge can be undone; all source records are kept.")}</small></span></label><p class="inline-error" id="merge-error" role="alert" hidden></p><button class="button button-primary merge-submit" id="merge-submit" type="submit" disabled>${t("確認是同一件，合併展示","Confirm & merge")} ${icon("check")}</button></form>`;
    $("#merge-confirm").addEventListener("change", event => { $("#merge-submit").disabled = !event.target.checked; });
    $("#merge-form").addEventListener("submit", mergeGarments);
    attachImageFallbacks(region);
    $("#duplicates-dialog").scrollTop = 0;
  }

  async function mergeGarments(event) {
    event.preventDefault();
    if (!$("#merge-confirm").checked) return;
    const suggestion = state.suggestions.find(entry => entry.id === state.selectedSuggestionId);
    if (!suggestion) { showError("merge-error", t("這組建議已更新，請返回重新選擇。","This suggestion was updated; go back and reselect.")); return; }
    const ids = new Set(suggestion.item_ids);
    for (const id of suggestion.item_ids) { for (const member of groupFor(id)?.item_ids || []) ids.add(member); }
    const primary = $('input[name="primary_item_id"]:checked', event.target)?.value;
    await withButton($("#merge-submit"), t("正在合併…","Merging…"), async () => {
      try {
        await api("/api/garment-groups", { method: "POST", body: JSON.stringify({ item_ids: [...ids], primary_item_id: primary }) });
        $("#duplicates-dialog").close();
        await refreshData(true);
        toast("已合併展示，所有來源與各自確認狀態都保留。");
        openItem(primary);
      } catch (error) { showError("merge-error", error.message); }
    });
  }

  async function ungroupGarments(id, button) {
    await withButton(button, t("正在恢復…","Restoring…"), async () => {
      try {
        await api(`/api/garment-groups/${encodeURIComponent(id)}`, { method: "DELETE" });
        $("#item-dialog").close();
        await refreshData(true);
        toast("已取消合併，每張來源恢復分開展示。");
      } catch (error) { toast(error.message, true); }
    });
  }

  function optionsFor(entries, selected) {
    return Object.entries(entries).map(([value, label]) => `<option value="${escapeHTML(value)}"${value === selected ? " selected" : ""}>${escapeHTML(label)}</option>`).join("");
  }

  function textField(id, label, value, full = false, placeholder = t("未辨識到可留空","Leave blank if unrecognized")) {
    return `<div class="form-field${full ? " full" : ""}"><label for="item-${id}">${escapeHTML(label)}</label><input id="item-${id}" name="${id}" value="${escapeHTML(value || "")}" placeholder="${escapeHTML(placeholder)}" maxlength="500"></div>`;
  }

  function qualityPanel(item) {
    if (isDemoItem(item)) return "";
    if (analysisFailed(item)) {
      const job = state.jobs.find(entry => entry.id === item.job_id);
      return `<section class="quality-assessment" aria-label="${t("分析狀態","Analysis status")}"><div class="quality-assessment-title">${icon("info")}<h3>${t("這張照片未能完成分析","This photo couldn't be analyzed")}</h3></div><p>${escapeHTML(item.error)}</p><div class="retake-advice"><strong>${t("下一步","Next step")}</strong><p>${t("這不是照片的問題；問題排除後重新分析即可，原始照片已保留。","This isn't a problem with your photo. Once the issue is fixed, try again — your original photo is kept.")}</p>${job && retryableStatuses.has(job.status) ? `<button class="button button-secondary" type="button" data-job-action="retry" data-id="${escapeHTML(job.id)}">${icon("refresh")}${t("重新分析","Try again")}</button>` : ""}</div></section>`;
    }
    const quality = item.quality;
    if (!quality) return '<p class="quality-legacy-note">' + t('此結果尚無完整度評估，請核對照片與衣物資訊。','No quality assessment for this result. Review photo and garment info.') + '</p>';
    if (quality.status === "needs_confirmation") return `<section class="quality-assessment outfit-assessment${item.reviewed ? " reviewed-override" : ""}" aria-label="${t("穿搭衣物確認","Outfit garment confirmation")}"><div class="quality-assessment-title">${icon("info")}<h3>${item.reviewed ? t("已人工確認這張穿搭紀錄","Outfit record manually confirmed") : t("穿搭衣物，請核對可見細節","Outfit garment — review visible details")}</h3></div><p>${item.reviewed ? t("已列入衣物室，原始遮擋與品質評估仍然保留。","Added to wardrobe; original occlusion and quality assessment kept.") : t("照片中的衣物部分被遮擋或未完整入鏡；下方保留有依據的可見屬性，尚未列入可整理衣物。","Garment partially occluded or not fully visible; visible attributes preserved below.")}</p><ul>${qualityReasons(item).map(reason => `<li>${escapeHTML(reason)}</li>`).join("")}</ul><div class="retake-advice"><strong>${t("確認時留意","Review notes")}</strong><p>${t("核對是否屬於同一件衣物、分類與可見色彩；看不清楚的品牌、尺寸或材質請留空。確認後再收進衣物室。","Verify category and visible colors. Leave unclear brand, size or material blank. Confirm to add to wardrobe.")}</p></div></section>`;
    if (quality.status === "ready") return `<div class="quality-ready-note">${icon("check")}<p>${t("畫面可供整理；類別、顏色與材質仍須人工確認。","Image ready; category, colors and material need manual review.")}</p></div>`;
    if (!["needs_retake", "unassessed"].includes(quality.status)) return "";
    const visibility = { multiple_garments: t("多件衣物","Multiple garments"), no_garment: t("未偵測到衣物","No garment"), uncertain: t("目標不明","Uncertain") }[quality.model_target] || { complete: t("完整","Complete"), partial: t("未完整入鏡","Partially visible"), fragment: t("僅局部區域","Fragment only"), unclear: t("畫面不清楚","Unclear") }[quality.visibility] || t("尚無法判讀","Unassessed");
    const title = item.reviewed ? t("已人工覆核，原始品質判定已保留","Manually reviewed; original quality kept") : quality.status === "unassessed" ? t("尚無法判讀這個區域","Cannot assess this region") : { multiple_garments: "請使用多件衣物模式", no_garment: "未偵測到衣物", uncertain: "尚無法確認衣物" }[quality.model_target] || t("需要補拍完整衣物","Full garment retake needed");
    return `<section class="quality-assessment${item.reviewed ? " reviewed-override" : ""}" aria-label="${t("影像完整度與拍攝建議","Image quality & retake advice")}"><div class="quality-assessment-title">${icon("info")}<h3>${title}</h3></div><p>${item.reviewed ? t("此結果已由你確認並列入目錄，原始品質紀錄保留如下。","Confirmed and cataloged; original quality record below.") : t("此結果尚未列入可整理衣物，暫不採用不可靠的衣物推測。","Not yet added to usable garments; unreliable guesses are not used.")}</p><span class="quality-visibility">${t("畫面：","Image: ")}${visibility}</span><ul>${qualityReasons(item).map(reason => `<li>${escapeHTML(reason)}</li>`).join("")}</ul><div class="retake-advice"><strong>${t("怎麼拍得更清楚","How to get a better shot")}</strong><p>${escapeHTML(retakeAdvice(item))}</p></div></section>`;
  }

  function originalReferenceURL(value, item) {
    const job = state.jobs.find(entry => entry.id === item?.job_id);
    if (!job || !["image", "video"].includes(job.media_type) || generatedSource(item)) return "";
    const media = safeMediaURL(value);
    if (!media || typeof item.job_id !== "string" || !/^[a-z0-9_-]+$/i.test(item.job_id)) return "";
    const url = new URL(media);
    const prefix = `/media/${encodeURIComponent(item.job_id)}/`;
    if (url.username || url.password || url.search || url.hash || !url.pathname.startsWith(prefix)) return "";
    const relative = url.pathname.slice(prefix.length);
    if (!/^(?:[a-z0-9_-]+\/)*[a-z0-9_-]+\.(?:jpe?g|png|webp)$/i.test(relative)) return "";
    if (relative.toLowerCase().split("/").some(part => ["reconstructions", "shopping", "generated"].includes(part))) return "";
    const synthetic = [item.reconstruction?.image_url, item.reconstruction?.preview_url, item.reconstruction?.candidate_url].map(safeMediaURL);
    return synthetic.includes(media) ? "" : media;
  }

  function reconstructionReferenceOptions(item) {
    return groupMembers(groupFor(item)).filter(source => source.id !== item.id && /^[a-f0-9]{32}$/i.test(source.id)
      && reconstructionEligible(source) && originalReferenceURL(source.image_url || source.source_url, source))
      .map(source => ({ item: source, url: originalReferenceURL(source.image_url || source.source_url, source) }));
  }

  function reconstructionReferenceState(item) {
    const group = groupFor(item);
    const membership = group ? JSON.stringify([group.id, [...(group.item_ids || [])].sort()]) : "";
    const options = reconstructionReferenceOptions(item);
    const draft = state.reconstructionReferences.get(item.id) || { ids: [], membership, invalid: false };
    const active = isActive(item.reconstruction);
    if (!active && state.referenceDataReady && draft.ids.length && (draft.membership !== membership
      || draft.ids.some(id => !options.some(option => option.item.id === id)))) draft.invalid = true;
    const primary = originalReferenceURL(item.image_url || item.source_url || item.cutout_url, item);
    const available = state.referenceDataReady && !!primary;
    const submitted = item.reconstruction?.reference_manifest?.sources;
    const selected = active && Array.isArray(submitted) ? submitted.filter(source => ["extra_1", "extra_2"].includes(source.id)).map(source => source.item_id) : draft.ids;
    return { group, membership, options, draft, primary, available, selected, locked: active || state.pending.has(`reconstruction:${item.id}`), blocked: draft.invalid || draft.ids.length > 0 && !available };
  }

  function renderReconstructionReferences(item, references) {
    if (!reconstructionEligible(item) && !references.draft.ids.length) return "";
    const disabled = references.locked || !references.available || references.draft.invalid || !reconstructionEligible(item) || reconstructionAvailable() === false;
    const hint = references.locked ? t("生成期間固定使用已送出的照片，完成或取消後才能調整。","References locked during generation. Adjust after completion or cancel.")
      : !state.referenceDataReady ? t("暫時無法確認同件來源，請重新讀取資料後再選擇。","Cannot confirm sources. Reload data to select references.")
      : !references.primary ? t("目前無法確認主照片的原始來源，暫不能加入其他參考。","Cannot confirm primary photo source. References unavailable.")
      : !references.group ? t("請先在「相似衣物」核對並合併同一件衣物，這裡就能選擇其他原照。","Merge the same garment in Similar Items first, then select references here.")
      : !references.options.length ? t("此群組暫無可用的其他原照；生成圖、來源缺失或尚無法辨識的紀錄不能作為參考。","No other original photos available in this group.")
      : t("只會使用你選取的同件原照；未選取時，仍只使用目前這張照片。","Only selected original photos are used. Without selection, only the current photo is used.");
    return `<details class="reconstruction-reference-picker"${references.draft.invalid ? " open" : ""}><summary>${t("參考其他照片","Reference photos")} <span>${t("選用 · 最多 2 張","optional · max 2")}</span></summary><p>${t("以目前這張主照片決定展示視角，其他照片只輔助補充細節。看不清楚的部位仍可能由 AI 推測。","The primary photo sets the display angle. Other photos add detail. Unclear areas may be AI-inferred.")}</p>${references.primary ? `<div class="reference-primary"><img src="${escapeHTML(references.primary)}" alt="${t("本次主照片","Primary photo")}"><span>${t("主照片 · 固定展示視角","Primary photo · sets display angle")}<small>${t("目前正在編輯的這張來源","Currently editing this source")}</small></span></div>` : ""}<p class="reference-hint" id="reconstruction-reference-hint">${hint}</p>${references.draft.invalid ? '<p class="inline-error" role="status">${t("同件來源已變更，原先選擇已停用。請重新選擇參考照片，或改用目前這一張。","Sources changed. Reselect references or use the current photo.")}</p>' : ""}${references.options.length ? `<fieldset class="reference-options" aria-describedby="reconstruction-reference-hint"${disabled ? " disabled" : ""}><legend class="visually-hidden">${t("選擇同件衣物的其他原照","Select other originals of the same garment")}</legend>${references.options.map((option, index) => { const checked = references.selected.includes(option.item.id); return `<label class="reference-option${checked ? " selected" : ""}"><input type="checkbox" data-reconstruction-reference="${escapeHTML(option.item.id)}"${checked ? " checked" : ""}${!checked && references.selected.length >= 2 ? " disabled" : ""}><img src="${escapeHTML(option.url)}" alt="${t("參考照片","Reference photo")} ${index + 1}"><span>${t("照片","Photo")} ${index + 1}<small>${escapeHTML(titleFor(option.item))}</small><small>${escapeHTML(formatDate(option.item.created_at, true))}</small></span></label>`; }).join("")}</fieldset><p class="reference-count" role="status">${t("已選","Selected")} ${references.selected.length} / 2 ${t("張其他照片","additional photos")}</p>` : ""}<div class="reference-picker-actions">${references.draft.ids.length && !references.locked ? `<button class="text-button" type="button" data-reference-reset>${references.draft.invalid ? t("重新選擇參考照片","Reselect references") : t("改用單張主照片","Use primary photo only")}</button>` : ""}${!state.referenceDataReady && !references.locked ? '<button class="text-button" type="button" data-reference-refresh>${t("重新讀取來源","Reload sources")}</button>' : ""}</div></details>`;
  }

  function changeReconstructionReference(id, checked) {
    const item = state.items.find(entry => entry.id === state.editingItemId);
    if (!item) return;
    const references = reconstructionReferenceState(item);
    if (references.locked || !references.available || references.draft.invalid || !references.options.some(option => option.item.id === id)) return;
    const ids = checked ? [...new Set([...references.draft.ids, id])] : references.draft.ids.filter(value => value !== id);
    if (ids.length > 2) return;
    state.reconstructionReferences.set(item.id, { ids, membership: references.membership, invalid: false });
    renderReconstructionPanel(item);
  }

  function imageReferenceManifest(reconstruction, kind) {
    return reconstruction?.[`${kind}_validation`]?.plan?.reference_manifest ?? reconstruction?.[`${kind}_reference_manifest`] ?? null;
  }

  function renderReferenceManifest(item, manifest, title) {
    if (!validReferenceManifest(manifest) || manifest.target_item_id !== item.id) {
      return `<section class="reference-history"><h4>${escapeHTML(title)}</h4><p>${t("此圖未記錄可確認的參考來源，請對照原照片核對。","No confirmed reference sources recorded. Verify against original photo.")}</p></section>`;
    }
    const primary = manifest.sources.find(source => source.id === manifest.primary_source && source.item_id === item.id && source.job_id === item.job_id);
    if (!primary) return `<section class="reference-history"><h4>${escapeHTML(title)}</h4><p>${t("暫時無法確認這次使用的主照片。","Cannot confirm the primary photo used.")}</p></section>`;
    const sources = [primary, ...manifest.sources.filter(source => ["extra_1", "extra_2"].includes(source.id))];
    return `<section class="reference-history"><h4>${escapeHTML(title)}</h4><div class="reference-history-grid">${sources.map((source, index) => {
      const record = state.items.find(entry => entry.id === source.item_id && entry.job_id === source.job_id);
      const url = record && (source.role === "original_context" || source.id === "cutout" && source.role === "target_locator") ? originalReferenceURL(source.media_url, record) : "";
      const label = index ? `${t("參考照片","Reference")} ${index}` : t("主照片","Primary");
      return `<figure data-reference-role="${index ? "extra" : "primary"}">${url ? `<a href="${escapeHTML(url)}" target="_blank" rel="noopener noreferrer" aria-label="${t("開啟","Open")} ${label}"><img src="${escapeHTML(url)}" alt="${label}"></a>` : '<span class="reference-unavailable">${t("來源照片已移除","Source removed")}<br>${t("或無法讀取","or unavailable")}</span>'}<figcaption>${label}</figcaption></figure>`;
    }).join("")}</div><p>${t("參考照用來補充可見細節，不代表已確認完整款式；遮擋與未見部位仍可能含推測。","References add visible detail but do not confirm the full style. Occluded areas may be inferred.")}</p></section>`;
  }

  function updateReconstructionReferenceHistory(item) {
    const media = $("#item-media-region");
    const entry = media?._entries?.find(value => value.key === media.dataset.selected);
    const current = item.reconstruction?.reference_manifest;
    const currentHost = $("#reconstruction-current-references");
    const imageHost = $("#reconstruction-image-references");
    const histories = [
      [currentHost, current && (!entry?.synthetic || !sameJSONValue(current, entry.referenceManifest) || isActive(item.reconstruction)) ? renderReferenceManifest(item, current, "Photos used for this generation") : ""],
      [imageHost, entry?.synthetic ? renderReferenceManifest(item, entry.referenceManifest, `Photos used for the${entry.key === "preview" ? " AI preview" : " generated image"} you are viewing`) : ""],
    ];
    for (const [host, html] of histories) {
      if (!host || host._html === html) continue;
      host._html = html; host.innerHTML = html; host.hidden = !html;
      attachImageFallbacks(host);
    }
  }

  function itemMediaEntries(item, sourceJob) {
    const reconstruction = acceptedReconstruction(item);
    const preview = readyPreview(item);
    const affordable = affordablePreviewCandidates(item);
    const oldImage = safeMediaURL(item.reconstruction?.image_url);
    const legacy = !reconstruction && oldImage !== preview ? oldImage : "";
    const candidate = safeMediaURL(item.reconstruction?.candidate_url) || (!preview ? safeMediaURL(item.reconstruction?.preview_url) : "");
    const context = safeMediaURL(item.image_url);
    const source = safeMediaURL(item.original_url || item.source_url || (sourceJob?.media_type === "image" ? sourceJob.preview_url || sourceJob.source_url : ""));
    const selected = safeMediaURL(item.source_url);
    const cutout = safeMediaURL(item.cutout_url);
    const reference = context || source || cutout;
    const entries = [];
    if (reconstruction) entries.push({ key: "reconstruction", label: "展示圖與原圖", url: reconstruction, reference, referenceManifest: imageReferenceManifest(item.reconstruction, "image"), synthetic: true, imageLabel: "AI 展示圖 · 已比對", caption: "可見款式已通過模型比對，仍不代表未見部位就是實物設計；原圖保留在旁供你核對。" });
    if (preview && preview !== reconstruction) {
      const report = item.reconstruction.preview_validation.preview;
      entries.push({ key: "preview", label: t("完整 AI 預覽","Full AI Preview"), url: preview, reference, referenceManifest: imageReferenceManifest(item.reconstruction, "preview"), synthetic: true, imageLabel: t("AI 預覽・含推測","AI Preview · Inferred"), caption: `${t("這是包含 AI 補畫的完整衣物預覽，不能用來確認同款、品牌或看不見的實物細節。","AI-generated full garment preview. Do not use to confirm brand or unseen details.")}${report.view_inferred ? t("原圖沒有正面視角，預覽正面由 AI 推測。","No front view in original; front preview is AI-inferred.") : ""}${report.mismatch_warnings.length ? ` ${t("已知差異：","Known differences: ")}${report.mismatch_warnings.slice(0, 2).join("; ")}` : t("請核對下方列出的推測部位。","Review the inferred areas listed below.")}` });
    }
    for (const entry of affordable) {
      entries.push({
        key: `preview-choice-${entry.id}`,
        selected: entry.id === item.reconstruction?.selected_candidate_id,
        label: `${t("AI 預覽","AI Preview")} ${entry.id}${entry.id === item.reconstruction?.selected_candidate_id ? t(" · 已選"," · Selected") : ""}`,
        url: entry.url,
        reference,
        referenceManifest: item.reconstruction?.preview_candidates_reference_manifest || null,
        synthetic: true,
        imageLabel: `${t("AI 完整預覽","AI Preview")} ${entry.id} · ${t("未逐項比對","unverified")}`,
        caption: t("這是低成本 AI 完整預覽，遮擋與未拍到的部位由模型推測；請直接與旁邊原圖核對。選取只改變目錄展示，不會改寫原始辨識。","Low-cost AI preview. Occluded areas are inferred. Compare with the original. Selection only changes the display."),
      });
    }
    if (context) entries.push({ key: "context", label: t("原圖上下文","Close-up"), url: context, caption: t("保留周圍畫面，方便核對這個區域在原圖中的位置。","This piece as it appears in your photo.") });
    if (source && source !== context) entries.push({ key: "source", label: t("完整原圖","Full photo"), url: source, caption: item.frame !== null && item.frame !== undefined ? `${t("來源影片第","Video frame")} ${item.frame} ${t("影格的完整畫面。","full frame.")}` : t("完整來源照片；請核對衣物是否被遮擋或裁切。","Your full photo.") });
    if (selected && selected !== source && selected !== context) entries.push({ key: "selected", label: t(t("框選人物","Person Selection"),"Person Selection"), url: selected, caption: t("這次送入衣物分割的選取範圍；完整原圖仍然保留。","Selection sent for segmentation. Full original is preserved.") });
    if (cutout && cutout !== context && cutout !== source) entries.push({ key: "cutout", label: t("辨識區域","Cutout"), url: cutout, caption: t("這是模型選取的可見區域，不代表整件衣物。","The visible part Meta AI cut out — not the whole garment.") });
    if (legacy) entries.push({ key: "legacy", label: t("舊版生成圖・未核對","Legacy · Unverified"), url: legacy, reference, referenceManifest: imageReferenceManifest(item.reconstruction, "image"), synthetic: true, imageLabel: t("舊版生成圖 · 未核對","Legacy · Unverified"), caption: t("這張先前生成圖缺少足夠的款式核對依據，仍有無法確認的細節；保留供比較，不採用為目錄主圖。","Previous image lacks sufficient style verification. Kept for comparison only.") });
    if (candidate && candidate !== reconstruction && candidate !== preview && candidate !== legacy && !affordable.some(entry => entry.url === candidate)) {
      const report = item.reconstruction?.candidate_validation;
      const hasReport = report?.version === 1;
      const reasons = reconstructionConcerns(report).slice(0, 3).map(entry => entry.label).join(" ");
      entries.push({ key: "candidate", label: hasReport ? t("待核對生成圖","Pending Review") : t("舊版生成圖・未核對","Legacy · Unverified"), url: candidate, reference, referenceManifest: imageReferenceManifest(item.reconstruction, "candidate"), synthetic: true, imageLabel: !hasReport ? t("舊版生成圖 · 未核對","Legacy · Unverified") : report.status === "rejected" ? t("生成圖 · 比對未通過","Generated · Failed") : t("生成圖 · 待核對","Generated · Pending"), caption: (!hasReport ? t("這張先前保留的生成圖沒有完整款式核對紀錄，可能改變設計；僅供比較，不採用為主圖。","Previous image without full style check. Kept for comparison only.") : report.status === "rejected" ? t("這張生成圖與原圖有差異，僅保留供比較，目錄沒有採用它。","Differs from original. Kept for comparison only.") : t("這張生成圖仍有無法確認的款式細節，僅供比較，目錄沒有採用它。","Unconfirmed style details. Kept for comparison only.")) + (reasons ? ` ${reasons}` : "") });
    }
    return entries;
  }

  function defaultItemMedia(entries) {
    return entries.find(entry => entry.key === "reconstruction" || entry.key === "preview" || entry.selected === true)?.key
      || entries.find(entry => ["context", "source", "selected", "cutout"].includes(entry.key))?.key || "";
  }

  function selectItemMedia(key) {
    const region = $("#item-media-region");
    if (!region) return;
    const entry = (region._entries || []).find(candidate => candidate.key === key);
    region.dataset.selected = key || "";
    $$("[data-item-media-key]", region).forEach(button => {
      const active = button.dataset.itemMediaKey === key;
      button.classList.toggle("active", active);
      button.setAttribute("aria-pressed", String(active));
    });
    const content = $("#item-media-content");
    content.classList.toggle("synthetic-media", !!entry?.synthetic);
    content.classList.toggle("media-comparison", !!entry?.synthetic && !!entry?.reference);
    const item = state.items.find(value => value.id === state.editingItemId);
    if (item) updateReconstructionReferenceHistory(item);
    if (!entry) { delete content.dataset.signature; content.innerHTML = `<span class="image-unavailable">${icon("image")}<span>${t("尚無可用的原圖預覽","No preview available")}</span></span>`; $("#item-media-caption").textContent = ""; return; }
    $("#item-media-caption").textContent = entry.caption;
    const signature = JSON.stringify([entry.key, entry.url, entry.reference, entry.imageLabel]);
    if (content.dataset.signature === signature) return;
    content.dataset.signature = signature;
    function picture(url, label) {
      const media = document.createElement(isVideoURL(url) ? "video" : "img");
      media.src = url;
      media.className = "item-photo";
      if (media.tagName === "VIDEO") { media.controls = true; media.preload = "metadata"; media.playsInline = true; }
      else media.alt = label;
      return media;
    }
    if (entry.synthetic && entry.reference) {
      content.replaceChildren(...[[entry.reference, t("原圖 · 可見依據","Original · Reference")], [entry.url, entry.imageLabel]].map(([url, label]) => {
        const figure = document.createElement("figure");
        const caption = document.createElement("figcaption");
        caption.textContent = label;
        figure.append(picture(url, label), caption);
        return figure;
      }));
    } else content.replaceChildren(picture(entry.url, entry.label));
    attachImageFallbacks(content);
  }

  function renderReconstructionPanel(item) {
    const region = $("#item-reconstruction");
    if (!region) return;
    const reconstruction = item.reconstruction;
    const image = acceptedReconstruction(item);
    const preview = readyPreview(item);
    const affordableCandidates = affordablePreviewCandidates(item);
    const affordable = affordablePreview(item);
    const previewReport = preview ? reconstruction.preview_validation.preview : null;
    const legacy = !image && safeMediaURL(reconstruction?.image_url) !== preview ? safeMediaURL(reconstruction?.image_url) : "";
    const candidate = safeMediaURL(reconstruction?.candidate_url);
    const eligible = reconstructionEligible(item);
    const available = reconstructionAvailable() !== false;
    const pending = state.pending.has(`reconstruction:${item.id}`);
    const requestError = state.reconstructionErrors.get(item.id) || "";
    const references = reconstructionReferenceState(item);
    const signature = JSON.stringify([reconstruction, eligible, available, pending, requestError, state.referenceDataReady, references]);
    if (region.dataset.signature === signature) return;
    const focusedAction = region.contains(document.activeElement) ? document.activeElement.dataset.reconstructionAction || (document.activeElement === region ? region.dataset.focusAction : null) : null;
    const focusedReference = region.contains(document.activeElement) ? document.activeElement.dataset.reconstructionReference : null;
    const referencesOpen = $(".reconstruction-reference-picker", region)?.open;
    region.dataset.signature = signature;
    const currentPreview = preview && readyPreviewReport(reconstruction?.validation);
    const status = affordableCandidates.length && !isActive(reconstruction) ? `${affordableCandidates.length} ${t("張完整預覽；目前選擇第","full previews; selected #")} ${reconstruction.selected_candidate_id || "1"}` : !image && currentPreview && !isActive(reconstruction) ? t("完整 AI 預覽已產生，推測細節仍須核對","Full AI preview generated; inferred details need review") : { queued: t("等待檢查原圖與生成","Queued for source check and generation"), running: reconstruction?.mode === "preview" ? t("正在一次生成 3 張完整預覽","Generating 3 full previews") : t("正在生成完整預覽並比對款式","Generating preview with style matching"), completed: image ? t("展示圖已通過款式比對","Display image passed style matching") : t("先前生成圖尚需核對","Previous generated image needs review"), needs_review: reconstruction?.validation?.generated === false ? t("原圖細節不足，這次暫不生成","Insufficient source detail; skipped this time") : t("生成圖仍需核對","Generated image needs review"), rejected: t("這次結果未通過款式比對","This result failed style matching"), failed: t("這次生成未能完成","Generation failed"), cancelled: t("已取消這次生成","Generation cancelled") }[reconstruction?.status] || t("從原照片，補全單件衣物預覽","Generate full garment preview from original photo");
    const explanation = !eligible ? t("請先核對原圖，確認衣物類別並儲存；目前的辨識結果不足以生成完整展示圖。","Review the original, confirm the category and save; current results are insufficient for generation.") : !available ? t("完整展示圖需要 Meta 圖片生成服務。請設定服務憑證後重新檢查模型狀態。","Full preview requires Meta image generation. Set up credentials and recheck model status.") : t("生成只改變展示圖片，不會改動衣物資訊、品質評估或人工確認狀態。","Generation only changes the preview image; garment info, quality and review status are unchanged.");
    const error = requestError || (reconstruction?.status === "failed" ? reconstruction.error : "");
    const concerns = reconstructionConcerns(reconstruction?.validation);
    const concernList = entries => `<ul>${entries.map(entry => `<li><strong>${escapeHTML(entry.label)}</strong>${entry.reason ? `<span>${escapeHTML(entry.reason)}</span>` : ""}</li>`).join("")}</ul>`;
    const review = concerns.length ? `<div class="reconstruction-review" aria-label="${t("款式核對說明","Style comparison details")}"><h4>${preview ? t("與原圖仍需核對的細節","Details to verify against original") : t("本次為什麼沒有採用","Why this result was not accepted")}</h4>${concernList(concerns.slice(0, 3))}${concerns.length > 3 ? `<details><summary>${t("其餘核對說明","More details")} (${concerns.length - 3})</summary>${concernList(concerns.slice(3))}</details>` : ""}</div>` : "";
    const inferred = previewReport ? `<div class="preview-inferences" aria-label="${t("AI 預覽推測說明","AI preview inference details")}"><h4>${t("AI 補畫了哪些部分","What AI inferred")}</h4><p>${escapeHTML(previewReport.notice)}</p>${previewReport.view_inferred ? '<p>' + t('原圖只顯示側面或無法確認正反面，這張正面預覽由 AI 推測。','Original shows only side; front preview is AI-inferred.') + '</p>' : ""}${previewReport.inferred_areas.length ? `<ul>${previewReport.inferred_areas.map(area => `<li><strong>${escapeHTML(area.area)}</strong>：${escapeHTML(area.detail)}</li>`).join("")}</ul>` : '<p>' + t('外形與遮擋部分仍可能包含推測，請以實物及原圖為準。','Shape and occluded areas may be inferred; refer to original.') + '</p>'}</div>${previewReport.mismatch_warnings.length ? `<div class="preview-inferences preview-mismatches" aria-label="${t("預覽與原圖的已知差異","Known differences from original")}"><h4>${t("已知差異，請留意","Known differences")}</h4><ul>${previewReport.mismatch_warnings.map(warning => `<li>${escapeHTML(warning)}</li>`).join("")}</ul></div>` : ""}` : "";
    const candidateConcerns = candidate && JSON.stringify(reconstruction?.candidate_validation) !== JSON.stringify(reconstruction?.validation) ? reconstructionConcerns(reconstruction?.candidate_validation) : [];
    const previousReview = candidateConcerns.length ? `<details class="reconstruction-review"><summary>${t("保留候選圖的核對說明","Candidate image review details")}</summary>${concernList(candidateConcerns)}</details>` : "";
    const variantPicker = affordableCandidates.length ? `<section class="preview-candidate-picker" aria-label="${t("選擇完整衣物預覽","Select garment preview")}"><div><h4>${t("從","Choose from")} ${affordableCandidates.length} ${t("張預覽中選一張","previews")}</h4><p>${t("每張都由原圖重建；未拍到的部分仍是推測。選擇不會改動原始辨識。","Each rebuilt from original; unseen areas are inferred. Does not change analysis.")}</p></div><div class="preview-candidate-grid">${affordableCandidates.map(entry => `<button type="button" data-select-reconstruction-candidate="${entry.id}" data-id="${escapeHTML(item.id)}" aria-pressed="${entry.id === reconstruction.selected_candidate_id}"${pending ? " disabled" : ""}><img src="${escapeHTML(entry.url)}" alt="${t("完整衣物預覽","Full preview")} ${entry.id}"><span>${entry.id === reconstruction.selected_candidate_id ? `${icon("check")}${t("已選擇","Selected")}` : t("選這張","Select")}</span></button>`).join("")}</div><p class="preview-cost-note">${t("本次標準費率估算約 US$0.03（三張圖）；不另做付費逐項核對。","Estimated ~US$0.03 for 3 images; no per-item verification.")}</p></section>` : "";
    region.classList.toggle("requires-review", ["needs_review", "rejected"].includes(reconstruction?.status) || !!legacy);
    region.innerHTML = `<div class="reconstruction-heading">${icon("spark")}<h3>${t("完整衣物預覽","Full Garment Preview")}</h3>${image ? '<span class="synthetic-label">${t("AI 生成 · 已比對","AI Generated · Verified")}</span>' : preview ? '<span class="synthetic-label unverified">${t("AI 預覽・含推測","AI Preview · Inferred")}</span>' : affordable ? '<span class="synthetic-label unverified">${t("AI 完整預覽 · 未逐項比對","AI Preview · Unverified")}</span>' : legacy || candidate ? '<span class="synthetic-label unverified">${t("未採用的生成圖","Unused generated image")}</span>' : ""}</div><p class="reconstruction-disclaimer">${t("完整預覽會補畫側面、遮擋或紮入的部位；預覽可用不代表與實物完全相同。只有另行通過款式比對的圖片才標示「已比對」。","Previews fill in sides and occluded areas. Only style-matched images are labeled Verified.")}</p><p class="reconstruction-status" role="status">${isActive(reconstruction) ? '<span class="spinner" aria-hidden="true"></span>' : ""}${status}</p>${image && reconstruction.status !== "completed" ? '<p class="reconstruction-previous">${t("目前仍保留上次通過比對的展示圖；本次結果沒有取代它。","Previous verified image kept; this result did not replace it.")}</p>' : ""}${legacy ? '<p class="reconstruction-previous">${t("這張先前生成圖尚未核對，僅保留供比較。","Previous image not reviewed; kept for comparison.")}</p>' : ""}${preview && !image && !currentPreview ? '<p class="reconstruction-previous">${t("目前保留先前完整預覽，本次結果沒有取代它。","Previous preview kept; this result did not replace it.")}</p>' : ""}${variantPicker}${inferred}${review}${previousReview}<div id="reconstruction-current-references" hidden></div><div id="reconstruction-image-references" hidden></div>${renderReconstructionReferences(item, references)}<p class="reconstruction-explanation">${explanation}</p>${error ? `<p class="inline-error" role="alert">${escapeHTML(error)}</p>` : ""}<div class="reconstruction-actions">${isActive(reconstruction) ? `<button class="button button-secondary" type="button" data-reconstruction-action="cancel" data-id="${escapeHTML(item.id)}"${pending ? " disabled" : ""}>${t("取消生成","Cancel")}</button>` : `<button class="button button-secondary" type="button" data-reconstruction-action="generate" data-reconstruction-mode="preview" data-id="${escapeHTML(item.id)}"${!eligible || !available || pending || references.blocked ? " disabled" : ""}>${icon("spark")}${pending ? "正在送出…" : affordableCandidates.length ? t("再產生 3 張 · 約 US$0.03","Generate 3 more · ~US$0.03") : t("產生 3 張預覽 · 約 US$0.03","Generate 3 previews · ~US$0.03")}</button><details class="verified-generation-option"><summary>${t("需要模型逐項比對","Model-verified matching")}</summary><p>${t("會分析可見細節、生成並核對候選；費用與時間依圖片複雜度增加。","Analyzes details, generates and verifies candidates; cost depends on complexity.")}</p><button class="text-button" type="button" data-reconstruction-action="generate" data-reconstruction-mode="verified" data-id="${escapeHTML(item.id)}"${!eligible || !available || pending || references.blocked ? " disabled" : ""}>${t("生成並深度比對","Generate & verify")}</button></details>`}${image ? '<button class="text-button" type="button" data-item-media-key="reconstruction">${t("比較展示圖與原圖","Compare display with original")}</button>' : ""}${preview && preview !== image ? '<button class="text-button" type="button" data-item-media-key="preview">${t("比較 AI 預覽與原圖","Compare preview with original")}</button>' : ""}${legacy ? '<button class="text-button" type="button" data-item-media-key="legacy">${t("查看舊版生成圖","View legacy image")}</button>' : ""}${candidate && candidate !== image && candidate !== preview && candidate !== legacy && !affordable ? '<button class="text-button" type="button" data-item-media-key="candidate">${t("查看待核對生成圖","View pending image")}</button>' : ""}</div>`;
    if (referencesOpen) $(".reconstruction-reference-picker", region)?.setAttribute("open", "");
    updateReconstructionReferenceHistory(item);
    attachImageFallbacks(region);
    if (focusedReference) $(`[data-reconstruction-reference="${focusedReference}"]`, region)?.focus({ preventScroll: true });
    if (focusedAction) {
      const next = $(`[data-reconstruction-action="${focusedAction}"]`, region) || $("[data-reconstruction-action]", region);
      if (next && !next.disabled) { next.focus({ preventScroll: true }); delete region.dataset.focusAction; }
      else { region.dataset.focusAction = focusedAction; region.tabIndex = -1; region.focus({ preventScroll: true }); }
    }
  }

  function updateItemReconstruction(item) {
    if (state.editingItemId !== item.id || !$("#item-dialog").open) return;
    renderReconstructionPanel(item);
    updateReconstructionReferenceHistory(item);
    const region = $("#item-media-region");
    const entries = itemMediaEntries(item, state.jobs.find(job => job.id === item.job_id));
    if (JSON.stringify(entries) === JSON.stringify(region._entries)) return;
    const selected = region.dataset.selected;
    const focused = region.contains(document.activeElement) ? document.activeElement.dataset.itemMediaKey : null;
    region._entries = entries;
    $(".item-media-tabs", region).innerHTML = entries.map(entry => `<button type="button" data-item-media-key="${entry.key}">${entry.label}</button>`).join("");
    selectItemMedia(entries.some(entry => entry.key === selected) ? selected : defaultItemMedia(entries));
    if (focused) $(`[data-item-media-key="${focused}"]`, region)?.focus({ preventScroll: true });
  }

  async function reconstructionAction(action, id, mode = "preview") {
    const item = state.items.find(entry => entry.id === id);
    const key = `reconstruction:${id}`;
    if (!item || state.pending.has(key)) return;
    const references = reconstructionReferenceState(item);
    if (action !== "cancel" && isActive(item.reconstruction)) return;
    if (action !== "cancel" && references.blocked) {
      state.reconstructionErrors.set(id, "參考來源已變更或暫時無法確認，請重新選擇照片後再生成。");
      updateItemReconstruction(item);
      return;
    }
    const referenceIds = [...references.draft.ids];
    const epoch = account.epoch;
    state.pending.add(key);
    state.reconstructionErrors.delete(id);
    updateItemReconstruction(item);
    try {
      const updated = await api(`/api/items/${encodeURIComponent(id)}/reconstruction${action === "cancel" ? "/cancel" : ""}`, {
        method: "POST", ...(action !== "cancel" ? { body: JSON.stringify({ reference_item_ids: referenceIds, mode }) } : {}),
      });
      state.items = state.items.map(entry => entry.id === id ? updated : entry);
      renderItems();
      updateItemReconstruction(updated);
      toast(action === "cancel" ? "已取消這次生成，原照片與上次展示圖保留。" : "已排入完整衣物圖生成，原照片與衣物資訊保留。");
      schedulePoll();
    } catch (error) {
      if (epoch !== account.epoch) return;
      state.reconstructionErrors.set(id, error.message);
      if (state.editingItemId !== id) toast(error.message, true);
      if (!account.blocked) await refreshData(true);
    } finally {
      state.pending.delete(key);
      const updated = state.items.find(entry => entry.id === id);
      if (updated) updateItemReconstruction(updated);
    }
  }

  async function selectReconstructionCandidate(id, candidateId) {
    const key = `reconstruction-select:${id}`;
    if (state.pending.has(key)) return;
    state.pending.add(key);
    try {
      const updated = await api(`/api/items/${encodeURIComponent(id)}/reconstruction/select`, {
        method: "POST", body: JSON.stringify({ candidate_id: candidateId }),
      });
      state.items = state.items.map(entry => entry.id === id ? updated : entry);
      renderItems(); updateItemReconstruction(updated);
      selectItemMedia(`preview-choice-${candidateId}`);
      toast(`已選擇完整預覽 ${candidateId}；原圖與辨識資料保持不變。`);
    } catch (error) { toast(error.message, true); }
    finally {
      state.pending.delete(key);
      const updated = state.items.find(entry => entry.id === id);
      if (updated) updateItemReconstruction(updated);
    }
  }

  function discoveryText(value) {
    return typeof value === "string" ? value : Array.isArray(value) ? value.filter(entry => typeof entry === "string").join(", ") : "";
  }

  function discoveryEvidenceURL(value, item) {
    const url = safeMediaURL(value);
    if (!url || generatedSource(item)) return "";
    const path = new URL(url).pathname;
    return path.startsWith(`/media/${encodeURIComponent(item.job_id)}/`) && !path.includes("/reconstructions/") && !path.includes("/shopping/") ? url : "";
  }

  function originalSearchReference(item) {
    const sourceJob = state.jobs.find(job => job.id === item.job_id);
    if (!sourceJob || !["image", "video"].includes(sourceJob.media_type)) return null;
    if (!discoveryEligible(item) || typeof item.job_id !== "string" || !/^[a-z0-9_-]+$/i.test(item.job_id)) return null;
    const synthetic = [item.reconstruction?.image_url, item.reconstruction?.preview_url, item.reconstruction?.candidate_url].map(safeMediaURL).filter(Boolean);
    for (const [value, label] of [[item.cutout_url, t("下載原始辨識區域","Download detected region")], [item.image_url, t("下載原圖上下文","Download original context")]]) {
      const media = discoveryEvidenceURL(value, item);
      if (!media || synthetic.includes(media)) continue;
      const url = new URL(media);
      const relative = url.pathname.slice(`/media/${encodeURIComponent(item.job_id)}/`.length);
      if (url.username || url.password || url.search || url.hash || !/^(?:[a-z0-9_-]+\/)*[a-z0-9_-]+\.(?:jpe?g|png|webp)$/i.test(relative)) continue;
      if (relative.toLowerCase().split("/").some(part => ["reconstructions", "shopping", "generated"].includes(part))) continue;
      return { url: media, label, filename: relative.split("/").pop() };
    }
    return null;
  }

  function manualImageSearch(item) {
    const source = originalSearchReference(item);
    if (!source) return "";
    return `<section class="shopping-image-handoff" aria-label="Search with original photo"><h5>Search with Original Photo</h5><p>Download the original, then upload to Google Lens. This is an external manual search — not verified for exact match or stock.</p><div class="image-handoff-actions"><a class="button button-secondary" data-original-search-download href="${escapeHTML(source.url)}" download="${escapeHTML(source.filename)}">${icon("download")}${source.label}</a><a class="button button-secondary" data-lens-link href="${escapeHTML(safePublicURL("https://lens.google/"))}" target="_blank" rel="noopener noreferrer">Open Google Lens ${icon("external")}</a></div><p class="image-handoff-privacy">Your photo is not sent automatically. Use original photos, not AI previews.</p></section>`;
  }

  function freeShoppingQuery(item) {
    const record = item?.record || {};
    const identity = item?.discovery?.result?.identity || {};
    const observed = identity.status === "observed";
    const profile = identity.visual_profile || {};
    const values = [
      observed ? identity.brand : item?.reviewed ? record.brand : null,
      observed ? identity.product_code : null,
      profile.type || record.subtype || (record.category && record.category !== "other" ? categories[record.category] : "clothing"),
      profile.primary_color || (Array.isArray(record.colors) ? record.colors.slice(0, 3).join(" ") : null),
      profile.pattern || record.pattern,
    ];
    const words = [...new Set(values.map(discoveryText).map(value => value.trim()).filter(Boolean))].join(" ").split(/\s+/);
    return words.filter((word, index) => words.findIndex(other => other.toLocaleLowerCase() === word.toLocaleLowerCase()) === index).join(" ").slice(0, 300) || "clothing";
  }

  function freeShoppingLinks(item, market) {
    const query = freeShoppingQuery(item);
    const google = new URL("https://www.google.com/search");
    google.searchParams.set("q", query);
    google.searchParams.set("tbm", "shop");
    google.searchParams.set("hl", "en");
    if (market === "TW") google.searchParams.set("gl", "tw");
    const bing = new URL("https://www.bing.com/shop");
    bing.searchParams.set("q", query);
    bing.searchParams.set("setlang", "en");
    if (market === "TW") { bing.searchParams.set("cc", "tw"); }
    return [
      ["Google Shopping", google.toString()],
      ["Bing Shopping", bing.toString()],
      ["Web Search", `https://www.google.com/search?q=${encodeURIComponent(query)}`],
    ];
  }

  function itemShopSummary(item) {
    if (generatedSource(item)) return "";
    const record = item.record || {};
    const attributes = [(record.colors || []).slice(0, 3).join(", "), record.pattern, record.material_guess].filter(Boolean);
    const [primary, ...others] = freeShoppingLinks(item, state.discoveryMarkets.get(item.id) || item.discovery?.market || defaultMarket());
    return `<div class="item-shop">${attributes.length ? `<p class="item-attributes">${attributes.map(escapeHTML).join(" · ")}</p>` : ""}<a class="card-shop item-shop-primary" href="${escapeHTML(safePublicURL(primary[1]))}" target="_blank" rel="noopener noreferrer">${icon("search")}${t("在 Google 搜尋同款","Shop similar on Google")}${icon("external")}</a><div class="item-shop-more">${others.map(([label, url]) => `<a class="card-shop secondary" href="${escapeHTML(safePublicURL(url))}" target="_blank" rel="noopener noreferrer">${label}${icon("external")}</a>`).join("")}</div><p class="item-shop-note">${t("免費搜尋連結，不呼叫模型；請以原圖核對款式。","Free search links — no model call. Compare styles with your photo.")}</p></div>`;
  }

  function freeShoppingHandoff(item, market) {
    if (generatedSource(item)) return "";
    const query = freeShoppingQuery(item);
    const links = freeShoppingLinks(item, market);
    const identity = item?.discovery?.result?.identity || {};
    const basis = identity.status === "observed" && identity.brand
      ? "Query includes the brand mark visible in the original photo; products still need individual verification."
      : item?.reviewed && item.record?.brand
        ? "Query includes the brand you confirmed."
        : "Query uses garment type, color and pattern — no brand guesses.";
    return `<section class="free-shopping-handoff" aria-label="Free shopping search"><div class="free-shopping-title"><h4>Quick Search — Free</h4><span class="free-badge">FREE — no API cost</span></div><p>${escapeHTML(basis)} These are external search links, not verified product pages.</p><p class="shopping-query">Query: ${escapeHTML(query)}</p><div class="free-shopping-actions">${links.map(([label, url]) => `<a class="button button-quiet" href="${escapeHTML(safePublicURL(url))}" target="_blank" rel="noopener noreferrer">${label}${icon("external")}</a>`).join("")}</div></section>`;
  }

  function shoppingImageURL(value, item) {
    const media = safeMediaURL(value);
    if (!media) return "";
    const url = new URL(media);
    const prefix = `/media/${encodeURIComponent(item.job_id)}/shopping/`;
    if (url.username || url.password || url.search || url.hash || !url.pathname.startsWith(prefix)) return "";
    return /^[a-z0-9_-]+\/[a-z0-9_-]+\.(?:jpe?g|png|webp)$/i.test(url.pathname.slice(prefix.length)) ? media : "";
  }

  const comparisonFields = { category: "Type", primary_color: "Color", pattern: "Pattern", sleeve_length: "Sleeves", neckline: "Neckline", design_details: "Details" };
  const comparisonLabels = { match: "Match", mismatch: "Mismatch", uncertain: "Uncertain", not_applicable: "N/A" };

  function verifiedShoppingOffer(offer, item) {
    const check = offer?.verification;
    return safePublicURL(offer?.url) && check?.status === "verified" && safePublicURL(check.page_url)
      && check.market === "in_scope" && ["in_stock", "unknown"].includes(check.availability)
      && Number.isFinite(Date.parse(check.page_checked_at)) && shoppingImageURL(check.image_url, item)
      && Object.keys(comparisonFields).every(key => ["match", "not_applicable"].includes(check.comparison?.[key]));
  }

  function shoppingOfferCard(offer, item, market) {
    const check = offer.verification;
    const pageURL = safePublicURL(check.page_url);
    const pageTime = escapeHTML(formatDate(check.page_checked_at, true));
    return `<li><div class="offer-product"><div><img class="offer-product-image" src="${escapeHTML(shoppingImageURL(check.image_url, item))}" alt="${escapeHTML(discoveryText(offer.title) || "Verified product photo")}"><div class="offer-image-note">Product photo</div></div><div><div class="offer-heading"><span class="offer-match">${offer.match === "model_candidate" ? "Exact Match" : "Similar Style"}</span><span class="offer-merchant">${escapeHTML(new URL(pageURL).hostname.replace(/^www\./, ""))}</span></div><a href="${escapeHTML(pageURL)}" target="_blank" rel="noopener noreferrer">${escapeHTML(discoveryText(offer.title) || "View product page")}${icon("external")}</a><span class="offer-verified">Verified against product page</span>${offer.reason ? `<p>${escapeHTML(discoveryText(offer.reason))}</p>` : ""}</div></div><div class="offer-comparison"><h5>Compared with Original</h5><dl>${Object.entries(comparisonFields).map(([key, label]) => `<div><dt>${label}</dt><dd${check.comparison[key] === "not_applicable" ? ' class="comparison-unclear"' : ""}>${comparisonLabels[check.comparison[key]]}</dd></div>`).join("")}</dl></div><div class="offer-page-check"><p><strong>${market === "TW" ? "Taiwan region evidence" : "Region evidence"}</strong>: ${escapeHTML(discoveryText(check.market_evidence) || "Candidate region verified")}</p><p class="offer-availability"><strong>${check.availability === "in_stock" ? "In stock at time of check" : "Stock unconfirmed"}</strong> · ${pageTime}</p><p>Please verify current stock and shipping on the product page. Match results do not confirm authenticity.</p></div></li>`;
  }

  function shoppingSearchAttempts(shopping) {
    return Array.isArray(shopping?.search_attempts) ? shopping.search_attempts.filter(attempt => typeof attempt?.query === "string" && attempt.query.trim() && (attempt.kind == null || ["text", "image"].includes(attempt.kind)) && ["searched", "no_results", "unavailable"].includes(attempt.status)) : [];
  }

  function shoppingSearchSummary(shopping) {
    const attempts = shoppingSearchAttempts(shopping);
    if (!attempts.length) return "";
    const unavailable = attempts.filter(attempt => attempt.status === "unavailable").length;
    const images = attempts.filter(attempt => attempt.kind === "image").length;
    const summary = unavailable ? `${attempts.length} searches (${unavailable} incomplete)` : `${attempts.length} searches`;
    return `${summary}${images ? ` · ${images} image, ${attempts.length - images} text` : ""}`;
  }

  function shoppingSearchHistory(shopping) {
    const attempts = shoppingSearchAttempts(shopping);
    if (!attempts.length) return "";
    const count = value => Number.isInteger(value) && value >= 0 ? value : "N/A";
    return `<section class="shopping-query shopping-attempts" aria-label="Search history"><strong>${escapeHTML(shoppingSearchSummary(shopping))}</strong><ol>${attempts.map(attempt => `<li data-search-kind="${attempt.kind === "image" ? "image" : "text"}"><strong>${attempt.kind === "image" ? "Image search · Google Web Detection" : `Query: ${escapeHTML(attempt.query)}`}</strong><p>${{ searched: "Results retrieved", no_results: "No verifiable candidates found", unavailable: "Search not completed" }[attempt.status]} · ${count(attempt.retrieved_count)} web sources · ${count(attempt.candidate_count)} candidates to verify</p>${attempt.error ? `<p>${escapeHTML(discoveryText(attempt.error))}</p>` : ""}</li>`).join("")}</ol><p>Search history and candidate sources do not confirm purchase availability. See verified results below.</p></section>`;
  }

  function shoppingResults(item, result, market) {
    const shopping = result.shopping || {};
    const verifiedVersion = shopping.version === 2;
    const rawOffers = verifiedVersion && Array.isArray(shopping.offers) ? shopping.offers : [];
    const offers = rawOffers.filter(offer => verifiedShoppingOffer(offer, item));
    const sources = (verifiedVersion && Array.isArray(shopping.sources) ? shopping.sources : []).filter(source => safePublicURL(source?.url));
    const links = (Array.isArray(result.search_links) ? result.search_links : []).filter(link => safePublicURL(link?.url));
    const checked = (verifiedVersion && Array.isArray(shopping.checked_candidates) ? shopping.checked_candidates : []).filter(candidate => ["rejected", "unverified"].includes(candidate?.status));
    const rejected = checked.filter(candidate => candidate.status === "rejected").length;
    const unverified = checked.length - rejected;
    const counts = [rejected ? `${rejected} rejected` : "", unverified ? `${unverified} unverified` : ""].filter(Boolean).join(", ");
    const message = !verifiedVersion ? "These are older results that haven't been verified against product pages. Re-run the search to get verified candidates." : shopping.status === "unavailable" ? "Product verification could not be completed. Brand evidence and search links are preserved." : !offers.length ? `No verified purchase candidates found.${counts ? ` ${counts} — sources and reasons preserved below.` : " No verifiable product pages or images retrieved. Use the search links below to search manually."}` : "The following candidates have been verified against product pages. Please confirm current style, price and availability on the product page.";
    return `<section class="shopping-results" aria-label="Shopping Results"><div class="shopping-heading"><h4>Shopping Results</h4><span>${escapeHTML(marketName(market))}</span></div><p class="shopping-time">Searched: ${shopping.searched_at ? escapeHTML(formatDate(shopping.searched_at, true)) : "No search completed yet"}</p>${shopping.query ? `<p class="shopping-query">Query: ${escapeHTML(discoveryText(shopping.query))}</p>` : ""}${shoppingSearchHistory(shopping)}<p class="shopping-reminder${verifiedVersion ? "" : " shopping-legacy"}">${message}</p>${offers.length ? "" : manualImageSearch(item)}${verifiedVersion && offers.length && shopping.notice ? `<p class="shopping-notice">${escapeHTML(discoveryText(shopping.notice))}</p>` : ""}${rawOffers.length > offers.length ? '<p class="shopping-notice">Some candidates lack verification data or local product images — please re-run the search.</p>' : ""}${offers.length ? `<ul class="shopping-offers">${offers.map(offer => shoppingOfferCard(offer, item, market)).join("")}</ul>` : ""}${checked.length ? `<details class="shopping-checks"${offers.length ? "" : " open"}><summary>Unverified &amp; Rejected Candidates · ${counts}</summary><ul>${checked.map(candidate => `<li><span class="candidate-status">${candidate.status === "rejected" ? "Rejected" : "Unverified"}</span>${safePublicURL(candidate.url) ? `<a href="${escapeHTML(safePublicURL(candidate.url))}" target="_blank" rel="noopener noreferrer">${escapeHTML(discoveryText(candidate.title) || "Candidate source")}</a>` : `<strong>${escapeHTML(discoveryText(candidate.title) || "Candidate source")}</strong>`}<p>${escapeHTML(discoveryText(candidate.reasons) || (candidate.status === "rejected" ? "Did not match garment or region criteria." : "Insufficient product page or image evidence."))}</p></li>`).join("")}</ul></details>` : ""}${links.length ? `<div class="discovery-search-links"><h5>Search Manually</h5><p>These are search entry points, not product pages.</p>${links.map(link => `<a class="button button-quiet" href="${escapeHTML(safePublicURL(link.url))}" target="_blank" rel="noopener noreferrer">${escapeHTML(discoveryText(link.label) || "Open search")}${icon("external")}</a>`).join("")}</div>` : ""}${sources.length ? `<details class="discovery-web-sources"><summary>Web Sources (not all verified) <span>${sources.length}</span></summary><ul>${sources.map(source => `<li><a href="${escapeHTML(safePublicURL(source.url))}" target="_blank" rel="noopener noreferrer">${escapeHTML(discoveryText(source.title) || new URL(safePublicURL(source.url)).hostname)}${icon("external")}</a>${source.snippet ? `<p>${escapeHTML(discoveryText(source.snippet))}</p>` : ""}</li>`).join("")}</ul></details>` : ""}</section>`;
  }

  function discoveryResults(item) {
    const discovery = item.discovery;
    const result = discovery?.result;
    if (!result || typeof result !== "object") return manualImageSearch(item);
    const identity = result.identity || {};
    const shopping = result.shopping || {};
    const known = ["observed", "possible"].includes(identity.status) && discoveryText(identity.brand);
    const identityTitle = known ? `${identity.status === "observed" ? "Visible mark on photo" : "Possible brand"} · ${discoveryText(identity.brand)}` : "Brand not identified";
    const evidenceType = { label: "Label / Tag", wordmark: "Wordmark", logo: "Logo", none: "No clear mark found" }[identity.evidence_type] || "No clear mark found";
    const evidenceURLs = [...new Set([item.original_url || item.source_url || item.image_url, ...(Array.isArray(result.source_urls) ? result.source_urls : [])].map(url => discoveryEvidenceURL(url, item)).filter(Boolean))];
    const previous = !["completed", "partial"].includes(discovery.status);
    const market = result.market || shopping.market || (!previous ? discovery.market : "");
    const recordChanged = result.reference_record && Object.entries(result.reference_record).some(([key, value]) => JSON.stringify(item.record?.[key]) !== JSON.stringify(value));
    return `<div class="discovery-results">${previous ? '<p class="discovery-previous">Previous results preserved below — current search has not replaced them.</p>' : ""}${recordChanged ? '<p class="discovery-previous">Garment info has been updated. These are results from a previous search — re-run if needed.</p>' : ""}<div class="brand-finding ${known ? escapeHTML(identity.status) : "unknown"}"><span class="discovery-eyebrow">Brand Evidence</span><h4>${escapeHTML(identityTitle)}</h4><p class="brand-certainty">${!known ? "Take a clearer photo of the logo, label or care tag. Style and color alone cannot confirm a brand." : identity.status === "observed" ? "A mark was read from the original photo. Authenticity and model number are not verified." : "This is a possible brand pending verification — it will not be auto-filled into the brand field."}</p><dl class="brand-evidence"><div><dt>Evidence type</dt><dd>${evidenceType}</dd></div>${identity.visible_text ? `<div><dt>Visible text</dt><dd>${escapeHTML(discoveryText(identity.visible_text))}</dd></div>` : ""}${identity.evidence ? `<div><dt>Basis</dt><dd>${escapeHTML(discoveryText(identity.evidence))}</dd></div>` : ""}${identity.product_code ? `<div><dt>Model clue</dt><dd>${escapeHTML(discoveryText(identity.product_code))}</dd></div>` : ""}</dl>${evidenceURLs.length ? `<div class="discovery-image-evidence"><img src="${escapeHTML(evidenceURLs[0])}" alt="Original photo used for brand search"><div><strong>Only original photos used</strong><p>AI-generated previews are never used as brand or purchase evidence.</p>${evidenceURLs.map((url, index) => `<a href="${escapeHTML(url)}" target="_blank" rel="noopener noreferrer">Source photo ${index + 1} ${icon("external")}</a>`).join("")}</div></div>` : ""}</div>${shoppingResults(item, result, market)}</div>`;
  }

  function renderDiscoveryPanel(item) {
    const region = $("#item-discovery");
    if (!region) return;
    const discovery = item.discovery;
    const eligible = discoveryEligible(item);
    const available = state.health?.discovery?.available !== false;
    const pending = state.pending.has(`discovery:${item.id}`);
    const market = state.discoveryMarkets.get(item.id) || discovery?.market || defaultMarket();
    const requestError = state.discoveryErrors.get(item.id) || "";
    const imageSearch = imageSearchService();
    const signature = JSON.stringify([discovery, item.record, eligible, available, pending, market, requestError, originalSearchReference(item), imageSearch]);
    if (region.dataset.signature === signature) return;
    const focused = region.contains(document.activeElement) ? document.activeElement.dataset.discoveryAction || (document.activeElement.hasAttribute("data-original-search-download") ? "original-download" : document.activeElement.hasAttribute("data-lens-link") ? "lens-link" : document.activeElement.id === "discovery-market" ? "market" : document.activeElement === region ? region.dataset.focusAction : null) : null;
    const sourcesOpen = $(".discovery-web-sources", region)?.open;
    const checksOpen = $(".shopping-checks", region)?.open;
    region.dataset.signature = signature;
    const stage = { identifying: "Reading brand clues from photo…", searching: "Searching for purchase candidates…", verifying: "Verifying product pages, images & region…" }[discovery?.stage] || "Searching for brand & purchase leads…";
    const status = discovery?.status === "running" ? stage : { queued: "Waiting to start search", completed: "Search complete — review the results below", partial: "Partial results preserved", failed: "Search could not be completed", cancelled: "Search cancelled" }[discovery?.status] || "Press search to use your photo and garment info.";
    const explanation = generatedSource(item) ? "This entry is from an AI-generated image and cannot serve as real brand evidence. Upload the original garment photo to search." : !eligible ? "Please review the original photo, confirm the garment type and save before searching." : !available ? "Brand search service is not ready. Check the Models & Services settings." : `${imageSearch.description} The search uses saved garment info. It will not auto-fill the brand field or use AI previews as evidence.`;
    const error = requestError || (["failed", "partial"].includes(discovery?.status) ? discovery.error : "");
    region.innerHTML = `<div class="discovery-heading">${icon("search")}<h3>Brand &amp; Shopping</h3></div><p class="discovery-explanation">${explanation}</p>${freeShoppingHandoff(item, market)}<div class="discovery-paid-divider"><span>AI-powered verification</span></div><div class="discovery-controls"><div class="discovery-market-field"><label for="discovery-market">Search Region</label><div class="select-wrap"><select id="discovery-market"${pending || isActive(discovery) ? " disabled" : ""}><option value="TW"${market === "TW" ? " selected" : ""}>Taiwan</option><option value="global"${market === "global" ? " selected" : ""}>Global</option></select>${icon("chevron")}</div></div>${isActive(discovery) ? `<button class="button button-secondary" type="button" data-discovery-action="cancel" data-id="${escapeHTML(item.id)}"${pending ? " disabled" : ""}>Cancel Search</button>` : `<button class="button button-secondary" type="button" data-discovery-action="start" data-id="${escapeHTML(item.id)}"${!eligible || !available || pending ? " disabled" : ""}>${icon("search")}${pending ? "Submitting…" : discovery ? "Re-run AI Verification" : "AI Brand &amp; Product Search"}</button>`}</div><p class="discovery-status" role="status">${isActive(discovery) ? '<span class="spinner" aria-hidden="true"></span>' : ""}${status}${isActive(discovery) ? ` · ${escapeHTML(marketName(discovery.market))}` : ""}</p>${error ? `<p class="inline-error" role="alert">${escapeHTML(error)}</p>` : ""}${discoveryResults(item)}`;
    $("#discovery-market", region).addEventListener("change", event => { state.discoveryMarkets.set(item.id, event.target.value); });
    if (sourcesOpen && $(".discovery-web-sources", region)) $(".discovery-web-sources", region).open = true;
    if (checksOpen && $(".shopping-checks", region)) $(".shopping-checks", region).open = true;
    attachImageFallbacks(region);
    if (focused) {
      const next = focused === "original-download" ? $("[data-original-search-download]", region) : focused === "lens-link" ? $("[data-lens-link]", region) : focused === "market" ? $("#discovery-market", region) : $(`[data-discovery-action="${focused}"]`, region) || $("[data-discovery-action]", region);
      if (next && !next.disabled) { next.focus({ preventScroll: true }); delete region.dataset.focusAction; }
      else { region.dataset.focusAction = focused; region.tabIndex = -1; region.focus({ preventScroll: true }); }
    }
  }

  function updateItemDiscovery(item) {
    if (state.editingItemId === item.id && $("#item-dialog").open) renderDiscoveryPanel(item);
  }

  async function discoveryAction(action, id) {
    const item = state.items.find(entry => entry.id === id);
    const key = `discovery:${id}`;
    if (!item || state.pending.has(key)) return;
    const market = $("#discovery-market")?.value === "global" ? "global" : "TW";
    state.discoveryMarkets.set(id, market);
    state.pending.add(key);
    state.discoveryErrors.delete(id);
    renderItems();
    updateItemDiscovery(item);
    try {
      const updated = await api(`/api/items/${encodeURIComponent(id)}/discovery${action === "cancel" ? "/cancel" : ""}`, { method: "POST", ...(action === "cancel" ? {} : { body: JSON.stringify({ market }) }) });
      state.items = state.items.map(entry => entry.id === id ? updated : entry);
      renderItems();
      updateItemDiscovery(updated);
      toast(action === "cancel" ? "Search cancelled. Previous results and garment info preserved." : "Brand & product search queued. Review results when complete." );
      schedulePoll();
    } catch (error) {
      state.discoveryErrors.set(id, error.message);
      if (state.editingItemId !== id) toast(error.message, true);
    } finally {
      state.pending.delete(key);
      renderItems();
      const updated = state.items.find(entry => entry.id === id);
      if (updated) updateItemDiscovery(updated);
    }
  }

  function openItem(id) {
    const item = state.items.find(x => x.id === id);
    if (!item) { toast(t("這筆結果已不存在，請重新整理。","This result no longer exists. Please refresh."), true); return; }
    state.editingItemId = id;
    const r = item.record || {};
    const sourceJob = state.jobs.find(job => job.id === item.job_id);
    const entries = itemMediaEntries(item, sourceJob);
    const attention = needsAttention(item);
    const provenance = isDemoItem(item) ? t("歷史範例 · 使用既有辨識結果，這次未執行模型。","Sample · using existing results, no model was run.") : item.provenance === "generated" ? t("AI 生成圖片 · 衣物資訊仍需人工確認。","AI-generated image · details need manual review.") : `${providerName(sourceJob?.provider || item.provenance)} ${t("分析結果","analysis results")}`;
    $("#item-dialog-body").innerHTML = `
      <div class="item-detail${attention ? " attention-detail" : ""}">
        <div class="item-visual">
          <div class="item-media-panel" id="item-media-region">
            <div class="item-media-tabs" role="group" aria-label="${t("核對原圖、辨識區域與完整展示圖","Compare original, detected region and full preview")}">${entries.map(entry => `<button type="button" data-item-media-key="${entry.key}">${entry.label}</button>`).join("")}</div>
            <div class="item-media-display" id="item-media-content"></div>
            <p class="item-media-caption" id="item-media-caption"></p>
          </div>
        </div>
        <div class="item-form-panel">
          <div class="dialog-heading"><span class="section-index">${attention ? "REGION REVIEW" : "GARMENT DETAILS"}</span><h2 id="item-dialog-title">${escapeHTML(titleFor(item))}</h2><div class="item-confidence">${attention ? `<span class="confidence-note review">${isUnassessed(item) ? t("尚無法判讀","Unassessed") : t("需補拍","Needs retake")} · ${t("尚未收錄","not cataloged")}</span>` : `<span class="confidence-note"> ${t("模型信心","Confidence")} ${percentage(Number(r.confidence || 0) * 100)}%</span>${r.needs_review && !item.reviewed ? `<span class="confidence-note review">${t("仍需人工確認","Needs review")}</span>` : ""}${item.reviewed ? `<span class="confidence-note">${t("已人工確認","Confirmed")}</span>` : ""}`}</div></div>
          ${attention ? qualityPanel(item) : itemShopSummary(item)}
          <details class="item-more"${item.discovery ? " open" : ""}><summary>${t("更多：AI 精準比對、完整預覽與編輯","More: AI exact match, full preview & edit")}${icon("chevron")}</summary>
          ${attention ? "" : qualityPanel(item)}
          <section class="discovery-panel" id="item-discovery" aria-label="${t("品牌與購買管道","Brand & shopping")}"></section>
          <section class="reconstruction-panel" id="item-reconstruction" aria-label="${t("完整衣物展示圖","Full garment preview")}"></section>
          ${renderGroupSources(item)}
          ${item.error && !analysisFailed(item) ? `<p class="inline-error">${escapeHTML(item.error)}</p>` : ""}
          ${attention ? `<p class="quality-edit-note">${t("若你能從原圖辨識實際衣物，可手動填寫後確認；原始完整度評估會繼續保留。","If you can identify the garment, fill in details and confirm; quality assessment is kept.")}</p>` : ""}
          <form id="item-form"><div class="form-grid"><div class="form-field"><label for="item-category">${t("類別","Category")}</label><div class="select-wrap"><select name="category" id="item-category">${optionsFor(categories, r.category)}</select>${icon("chevron")}</div></div>${textField("subtype", t("衣物名稱 / 款式","Name / Style"), r.subtype)}${textField("brand", t("品牌","Brand"), r.brand)}${textField("size_label", t("尺寸標籤","Size label"), r.size_label)}${textField("colors", t("顏色","Colors"), (r.colors || []).join("、"), false, t("以頓號或逗號分隔","Separate with commas"))}${textField("pattern", t("圖案 / 花色","Pattern"), r.pattern)}${textField("material_guess", t("材質（模型推測，請核對）","Material (AI guess, verify)"), r.material_guess)}<div class="form-field"><label for="item-condition">${t("衣物狀況","Condition")}</label><div class="select-wrap"><select name="condition" id="item-condition">${optionsFor(conditions, r.condition)}</select>${icon("chevron")}</div></div><div class="form-field full"><label for="item-defects">${t("瑕疵與注意事項","Defects & notes")}</label><textarea name="defects" id="item-defects" rows="2" maxlength="2000" placeholder="${t("未發現可留空；多項請換行","Leave blank if none")}">${escapeHTML((r.defects || []).join("\n"))}</textarea></div></div>
          <label class="review-checkbox"><input type="checkbox" name="reviewed" id="item-reviewed"${item.reviewed ? " checked" : ""}><span>${t("我已核對原圖與這件衣物的資訊","I reviewed the original and garment details")}<small>${attention ? t("人工覆核後會列入可整理衣物，原始品質紀錄保留。","Manual review adds to usable garments; original quality record is kept.") : t("人工確認會另外記錄，保留原始模型信心與品質評估。","Manual confirmation is recorded separately; original model confidence is kept.")}</small></span></label><p class="inline-error" id="item-error" role="alert" hidden></p><div class="item-form-actions"><button class="button button-secondary" type="submit">${t("儲存變更","Save changes")}</button><button class="button button-primary" type="submit" data-review="true">${icon("check")}${t("儲存並確認","Save & confirm")}</button></div></form>
          <div class="item-delete-row"><button class="text-button" type="button" id="delete-item-button">${icon("trash")}${attention ? t("刪除結果","Delete result") : t("刪除衣物","Delete item")}</button><span>${account.mode === "hosted" ? t("資料儲存在你的帳號衣物室","Data stored in your account wardrobe") : t("資料僅儲存於本機目錄","Data stored locally only")}</span></div>
          <div class="item-source-row"><p>${escapeHTML(provenance)} · ${escapeHTML(formatDate(item.created_at, true))}${item.frame !== null && item.frame !== undefined ? ` · ${t("來源影格","Source frame")} ${escapeHTML(item.frame)}` : ""}</p><button class="text-button" type="button" data-item-source="${escapeHTML(item.job_id)}">${t("查看來源與分割","View source & segmentation")} ${icon("arrow")}</button></div>
          </details>
        </div>
      </div>`;
    state.itemDirty = false;
    $("#item-form").addEventListener("input", () => { state.itemDirty = true; });
    $("#item-media-region")._entries = entries;
    selectItemMedia(defaultItemMedia(entries));
    renderReconstructionPanel(item);
    renderDiscoveryPanel(item);
    $("#item-form").addEventListener("submit", saveItem);
    $("#delete-item-button").addEventListener("click", deleteItem);
    const dialog = $("#item-dialog");
    dialog.showModal();
    $(".dialog-close", dialog).focus({ preventScroll: true });
    dialog.scrollTop = 0;
  }

  async function saveItem(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const id = state.editingItemId;
    if (!id || state.pending.has(`item:${id}`)) return;
    const data = new FormData(form);
    const nullable = key => String(data.get(key) || "").trim() || null;
    const splitValues = (key, separators) => String(data.get(key) || "").split(separators).map(x => x.trim()).filter(Boolean);
    const reviewed = event.submitter?.dataset.review === "true" || data.has("reviewed");
    const payload = { reviewed, record: { category: data.get("category"), subtype: nullable("subtype"), brand: nullable("brand"), size_label: nullable("size_label"), colors: splitValues("colors", /[,，、\n]/), pattern: nullable("pattern"), material_guess: nullable("material_guess"), condition: data.get("condition"), defects: splitValues("defects", /\n/) } };
    state.pending.add(`item:${id}`);
    showError("item-error", "");
    const controls = $$("button, input, textarea, select", form);
    controls.forEach(control => { control.disabled = true; });
    try {
      const item = await api(`/api/items/${encodeURIComponent(id)}`, { method: "PATCH", body: JSON.stringify(payload) });
      state.items = state.items.map(x => x.id === id ? item : x);
      syncJobCounts(item.job_id);
      renderItems();
      renderCounts();
      if (state.editingItemId === id) $("#item-dialog").close();
      toast(reviewed ? "已儲存並人工確認，已列入可整理衣物。" : "分析結果已儲存。");
      await refreshData(true);
    } catch (error) { if (state.editingItemId === id) showError("item-error", error.message); else toast(error.message, true); }
    finally { state.pending.delete(`item:${id}`); controls.forEach(control => { control.disabled = false; }); }
  }

  async function deleteItem() {
    const id = state.editingItemId;
    const item = state.items.find(x => x.id === id);
    if (!item || state.pending.has(`item:${id}`)) return;
    if (!(await confirmDeletion(needsAttention(item) ? t("刪除這筆分析結果？","Delete this result?") : t("讓這件衣物離開目錄？","Remove this item?"), `${titleFor(item)}: ${t("的結果資料將被刪除。原始處理紀錄會保留。","Result data will be deleted. Processing records are kept.")}`))) return;
    await withButton($("#delete-item-button"), t("正在刪除…","Deleting…"), async () => {
      try {
        await api(`/api/items/${encodeURIComponent(id)}`, { method: "DELETE" });
        state.items = state.items.filter(x => x.id !== id);
        syncJobCounts(item.job_id);
        renderItems();
        renderCounts();
        if (state.editingItemId === id) $("#item-dialog").close();
        toast(t("已刪除這筆結果。","Result deleted."));
        await refreshData(true);
      } catch (error) { if (state.editingItemId === id) showError("item-error", error.message); else toast(error.message, true); }
    });
  }

  function jobMediaEntries(job) {
    const entries = [["preview", t("處理預覽","Preview"), job.preview_url], ["source", t("原始檔案","Source File"), job.source_url], ["selection", "框選人物", job.selection_url], ["overlay", t("分割標記","Segmentation"), job.overlay_url]];
    return entries.filter(entry => safeMediaURL(entry[2])).map(([key, label, url]) => ({ key, label, url: safeMediaURL(url) }));
  }

  async function openJob(id) {
    state.selectedJobId = id;
    $("#job-dialog-body").innerHTML = `<div class="dialog-heading"><span class="section-index">PROCESS DETAILS</span><h2 id="job-dialog-title">${t("處理詳情","Process Details")}</h2></div><div class="loading-state"><span class="spinner"></span><p>${t("正在讀取處理結果…","Loading results…")}</p></div>`;
    if (!$("#job-dialog").open) $("#job-dialog").showModal();
    $("#job-dialog .dialog-close").focus();
    try {
      const job = await api(`/api/jobs/${encodeURIComponent(id)}`);
      if (state.selectedJobId !== id || !$("#job-dialog").open) return;
      renderJobDialog(job);
    } catch (error) {
      if (state.selectedJobId === id) $("#job-dialog-body").innerHTML = `<div class="dialog-heading"><span class="section-index">PROCESS DETAILS</span><h2 id="job-dialog-title">${t("無法讀取處理詳情","Cannot load details")}</h2></div><p class="inline-error">${escapeHTML(error.message)}</p><button class="button button-secondary" type="button" data-job-id="${escapeHTML(id)}">${t("重新讀取","Reload")}</button>`;
    }
  }

  function renderJobDialog(job) {
    $("#job-dialog-body").innerHTML = `<div class="dialog-heading"><span class="section-index">PROCESS DETAILS${job.is_demo ? " / HISTORICAL SAMPLE" : ""}</span><h2 id="job-dialog-title">${escapeHTML(job.filename)}</h2><p>${escapeHTML(formatDate(job.created_at, true))}${job.is_demo ? ` · ${t("歷史範例，這次沒有執行新的模型辨識","Historical sample; no new analysis")}` : ""}</p></div><div class="job-status-row" id="job-status-row"></div><div class="job-followup" id="job-detail-followup" data-job-followup="${escapeHTML(job.id)}" hidden></div><div id="job-detail-progress"></div><div id="job-detail-errors"></div><div id="job-media-region"></div><dl class="job-metadata"><div><dt>${t("分析模型","Model")}</dt><dd>${escapeHTML(providerName(job.provider))}</dd></div><div><dt>${t("整理模式","Mode")}</dt><dd>${escapeHTML(modeName(job.mode))}</dd></div><div><dt>${t("辨識目標","Target")}</dt><dd>${escapeHTML(job.concept || "garment")}</dd></div></dl><div class="job-detail-actions" id="job-detail-actions"></div>`;
    updateJobDialog(job);
  }

  function updateJobDialog(job) {
    const statusRow = $("#job-status-row");
    if (!statusRow) return;
    statusRow.innerHTML = `<span class="status-pill ${escapeHTML(job.status)}">${escapeHTML(statuses[job.status] || job.status)}</span><span>${escapeHTML(jobResultSummary(job))}</span><span>${escapeHTML(stageName(job))}</span>`;
    const followup = $("#job-detail-followup");
    updateJobFollowup(followup, job);
    $("#job-detail-progress").innerHTML = isActive(job) ? `<div class="job-detail-progress"><div class="progress-track" role="progressbar" aria-label="${t("處理進度","Processing progress")}" aria-valuenow="${percentage(job.progress)}" aria-valuemin="0" aria-valuemax="100"><div class="progress-bar" style="width:${percentage(job.progress)}%"></div></div><span>${percentage(job.progress)}% · ${t("可關閉此視窗，處理會繼續進行。","You can close this window; processing continues.")}</span></div>` : "";
    $("#job-detail-errors").innerHTML = `${job.error ? `<p class="inline-error">${escapeHTML(job.error)}</p>` : ""}${job.warnings?.length ? `<div class="job-warning">${job.warnings.map(warning => `<p>${escapeHTML(warning)}</p>`).join("")}</div>` : ""}`;
    const entries = jobMediaEntries(job);
    const mediaRegion = $("#job-media-region");
    const signature = JSON.stringify(entries);
    if (mediaRegion.dataset.signature !== signature) {
      const previousMedia = mediaRegion.dataset.selected;
      mediaRegion.dataset.signature = signature;
      mediaRegion.innerHTML = `<div class="job-media-tabs" role="group" aria-label="${t("檢視處理媒體","View processing media")}">${entries.map(entry => `<button type="button" data-media-key="${entry.key}">${entry.label}</button>`).join("")}</div><div class="job-media" id="job-media-content"></div><div class="job-media-caption" id="job-media-caption"></div>`;
      mediaRegion._entries = entries;
      selectJobMedia(entries.some(entry => entry.key === previousMedia) ? previousMedia : entries[0]?.key);
    }
    const id = escapeHTML(job.id);
    const pending = (action) => state.pending.has(`${action}:${job.id}`) ? " disabled" : "";
    const actions = $("#job-detail-actions");
    const actionSignature = JSON.stringify([job.id, job.item_count, job.status, job.is_demo, followup.hidden, pending("cancel"), pending("retry"), pending("delete")]);
    if (actions.dataset.signature !== actionSignature) {
      actions.dataset.signature = actionSignature;
      actions.innerHTML = `${job.item_count ? `${followup.hidden ? `<button class="button button-primary" type="button" data-job-items="${id}">${t("查看","View")} ${escapeHTML(job.item_count)} ${t("筆分析結果","results")} ${icon("arrow")}</button>` : ""}<a class="job-export-link" href="/api/export?format=json&amp;job_id=${encodeURIComponent(job.id)}" download>${t("匯出","Export")} JSON</a><a class="job-export-link" href="/api/export?format=csv&amp;job_id=${encodeURIComponent(job.id)}" download>CSV</a>` : ""}${isActive(job) ? `<button class="button button-secondary" type="button" data-job-action="cancel" data-id="${id}"${pending("cancel")}>${icon("stop")}${t("取消處理","Cancel")}</button>` : !job.is_demo && retryableStatuses.has(job.status) ? `<button class="button button-secondary" type="button" data-job-action="retry" data-id="${id}"${pending("retry")}>${icon("refresh")}${t("重新整理此檔案","Retry this file")}</button>` : ""}${!isActive(job) ? `<button class="text-button" type="button" data-job-action="delete" data-id="${id}"${pending("delete")}>${icon("trash")}${t("刪除紀錄","Delete record")}</button>` : ""}`;
    }
  }

  function selectJobMedia(key) {
    const region = $("#job-media-region");
    const entry = (region._entries || []).find(x => x.key === key);
    region.dataset.selected = key || "";
    $$("[data-media-key]", region).forEach(button => { const active = button.dataset.mediaKey === key; button.classList.toggle("active", active); button.setAttribute("aria-pressed", String(active)); });
    const content = $("#job-media-content");
    if (!entry) { content.innerHTML = "<p>" + t("媒體預覽尚未產生，完成的結果會顯示在這裡。","Media preview not yet available. Results will appear here.") + "</p>"; $("#job-media-caption").replaceChildren(); return; }
    const media = document.createElement(isVideoURL(entry.url) ? "video" : "img");
    media.src = entry.url;
    if (media.tagName === "VIDEO") { media.controls = true; media.preload = "metadata"; media.playsInline = true; }
    else media.alt = entry.label;
    content.replaceChildren(media);
    attachImageFallbacks(content);
    $("#job-media-caption").innerHTML = `<span>${entry.key === "overlay" ? t("分割標記與結果可能需要人工核對。","Segmentation results may need manual review.") : t("保留原始視角，方便核對衣物細節。","Original angle preserved for detail verification.")}</span><a class="text-button" href="${escapeHTML(entry.url)}" target="_blank" rel="noopener">${t("開啟檔案","Open file")} ${icon("external")}</a>`;
  }

  async function jobAction(action, id, button) {
    const key = `${action}:${id}`;
    if (state.pending.has(key)) return;
    const job = state.jobs.find(x => x.id === id);
    const deletionMessage = account.mode === "hosted" ? `${job?.filename || t("這次整理","this session")}: ${t("紀錄、相關衣物、本次照片／影片及生成圖將永久移除，無法復原。請先匯出所需資料。","Records, garments, photos/videos and generated images will be permanently deleted. Export first.")}` : `${job?.filename || t("這次整理","this session")}: ${t("處理紀錄與相關衣物將從目錄移除；來源媒體會保留在本機回收目錄。請先匯出所需資料。","Processing records and garments removed. Source media kept in recycle. Export first.")}`;
    if (action === "delete" && !(await confirmDeletion(t("刪除這次整理紀錄？","Delete this record?"), deletionMessage))) return;
    state.pending.add(key);
    if (button) button.disabled = true;
    try {
      if (action === "delete") {
        const deletion = await api(`/api/jobs/${encodeURIComponent(id)}`, { method: "DELETE", ...(account.mode === "hosted" ? { responseType: "cleanup" } : {}) });
        $("#job-dialog").close();
        if (state.filter.job === id) state.filter.job = "";
        state.selectedJobId = null;
        if (account.mode === "hosted") {
          storage.cleanupState = deletion.cleanup === "pending" ? "pending" : deletion.cleanup === "complete" ? null : "unknown";
          storage.data = null;
          renderStorage();
          toast(deletion.cleanup === "complete" ? t("已永久移除處理紀錄、相關衣物與媒體。","Records, garments and media permanently deleted.") : deletion.cleanup === "pending" ? t("紀錄已刪除；媒體仍待清理、仍佔用空間。請在帳號管理重試清理。","Deleted; media cleanup pending. Retry in account settings.") : t("紀錄已刪除；媒體清理狀態尚無法確認，請更新儲存用量。","Deleted; media cleanup status unknown. Refresh storage."), deletion.cleanup !== "complete");
        } else toast(t("已刪除處理紀錄與相關衣物；來源媒體保留於本機回收目錄。","Records and garments deleted. Source media kept in recycle."));
      } else {
        const updated = await api(`/api/jobs/${encodeURIComponent(id)}/${action}`, { method: "POST" });
        if (action === "retry") {
          $("#job-dialog").close();
          $("#item-dialog").close();
          await acceptJob(updated, t("已建立新的整理任務，原有結果仍然保留。","New task created; existing results kept."));
        } else {
          state.jobs = state.jobs.map(x => x.id === id ? updated : x);
          if ($( "#job-dialog").open && state.selectedJobId === id) updateJobDialog(updated);
          toast(t("已取消處理，完成的結果仍然保留。","Cancelled; completed results kept."));
        }
      }
      await refreshData(true);
      schedulePoll();
    } catch (error) { toast(error.message, true); }
    finally { state.pending.delete(key); if (button?.isConnected) button.disabled = false; }
  }

  function resetFilterControls() {
    $("#search-input").value = state.filter.q;
    $("#category-filter").value = state.filter.category;
    $$(".review-tabs [data-review], #confirmation-filter").forEach(button => { const active = button.dataset.review === state.filter.review; button.classList.toggle("active", active); button.setAttribute("aria-pressed", String(active)); });
  }

  document.addEventListener("click", event => {
    const button = event.target.closest("button, a[data-action]");
    if (!button || button.disabled) return;
    if (button.dataset.view) setView(button.dataset.view, true);
    if (button.dataset.close) $(`#${button.dataset.close}`).close();
    if (button.dataset.itemId) openItem(button.dataset.itemId);
    if (button.dataset.openDiscovery) {
      openItem(button.dataset.openDiscovery);
      const region = $("#item-discovery");
      region?.closest("details")?.setAttribute("open", "");
      if (region) { region.tabIndex = -1; region.focus({ preventScroll: true }); region.scrollIntoView({ block: "start" }); }
      if (button.dataset.startDiscovery) discoveryAction("start", button.dataset.openDiscovery);
    }
    if (button.dataset.jobId) openJob(button.dataset.jobId);
    if (button.dataset.jobAction) jobAction(button.dataset.jobAction, button.dataset.id, button);
    if (button.dataset.batchId) openBatch(button.dataset.batchId);
    if (button.dataset.batchAction) batchAction(button.dataset.batchAction, button.dataset.id, button);
    if (button.dataset.batchJob) { $("#batch-dialog").close(); openJob(button.dataset.batchJob); }
    if (button.dataset.removeFile) removeQueuedFile(button.dataset.removeFile);
    if (button.dataset.personFile) openPersonSelection(button.dataset.personFile);
    if (button.dataset.groupItem) openItem(button.dataset.groupItem);
    if (button.dataset.ungroup) ungroupGarments(button.dataset.ungroup, button);
    if (button.dataset.suggestionId) reviewDuplicate(button.dataset.suggestionId);
    if (button.hasAttribute("data-duplicates-back")) { state.selectedSuggestionId = null; renderDuplicateList(); }
    if (button.hasAttribute("data-duplicates-refresh")) withButton(button, t(t("正在讀取…","Loading…"),"Loading…"), async () => { await refreshData(true); renderDuplicateList(); });
    if (button.dataset.mediaKey) selectJobMedia(button.dataset.mediaKey);
    if (button.dataset.itemMediaKey) {
      selectItemMedia(button.dataset.itemMediaKey);
      if (!button.closest("#item-media-region")) $("#item-media-region")?.scrollIntoView({ behavior: "smooth", block: "start" });
    }
    if (button.dataset.reconstructionAction) reconstructionAction(button.dataset.reconstructionAction, button.dataset.id, button.dataset.reconstructionMode || "preview");
    if (button.dataset.selectReconstructionCandidate) selectReconstructionCandidate(button.dataset.id, button.dataset.selectReconstructionCandidate);
    if (button.hasAttribute("data-reference-reset")) {
      const item = state.items.find(entry => entry.id === state.editingItemId);
      if (item && !isActive(item.reconstruction) && !state.pending.has(`reconstruction:${item.id}`)) {
        state.reconstructionReferences.delete(item.id);
        state.reconstructionErrors.delete(item.id);
        renderReconstructionPanel(item);
        $(".reconstruction-reference-picker summary")?.focus({ preventScroll: true });
      }
    }
    if (button.hasAttribute("data-reference-refresh")) withButton(button, t("正在讀取…","Loading…"), () => refreshData(true));
    if (button.dataset.discoveryAction) discoveryAction(button.dataset.discoveryAction, button.dataset.id);
    if (button.dataset.itemSource) { $("#item-dialog").close(); openJob(button.dataset.itemSource); }
    if (button.dataset.jobItems) {
      state.filter = { q: "", category: "", review: "all", job: button.dataset.jobItems, batch: "" };
      resetFilterControls();
      renderItems();
      $("#job-dialog").close();
      setView("wardrobe", true);
    }
    if (button.closest(".review-tabs, .lifestyle-tools") && button.dataset.review) {
      state.filter.review = button.dataset.review;
      resetFilterControls();
      renderItems();
    }
    if (button.dataset.action === "clear-filters") {
      state.filter = { q: "", category: "", review: "all", job: "", batch: "" };
      resetFilterControls();
      renderItems();
    }
    if (button.dataset.action === "clear-scope") { state.filter.job = ""; state.filter.batch = ""; state.showUnclear = false; renderItems(); }
    if (button.dataset.action === "toggle-unclear") { state.showUnclear = !state.showUnclear; renderItems(); }
    if (button.dataset.action === "load-demo") importDemo();
  });

  $(".brand").addEventListener("click", event => { event.preventDefault(); setView("wardrobe"); window.scrollTo({ top: 0, behavior: "smooth" }); });
  $("#upload-form").addEventListener("submit", upload);
  $("#generate-form").addEventListener("submit", generate);
  $("#demo-button").addEventListener("click", importDemo);
  $("#file-input").addEventListener("change", event => captureFiles(event.target.files || []));
  $("#camera-input").addEventListener("change", event => { captureFiles(event.target.files || []); event.target.value = ""; });
  // Only this page and the extension's bridge in this tab can post to it (same window, same origin).
  window.addEventListener("message", event => {
    const data = event.data;
    if (event.source !== window || event.origin !== location.origin || data?.type !== "rewear:import") return;
    const reply = (ok, reason = "") => window.postMessage({ type: "rewear:import-result", id: data.id, ok, reason }, location.origin);
    const file = fileFromDataUrl(data.dataUrl, data.name);
    if (!file) return reply(false, t("無法讀取這張照片。", "This photo couldn't be read."));
    if (state.uploading) return reply(false, t("Rewear 正在上傳另一張照片，請稍候再試。", "Rewear is still uploading another photo. Try again in a moment."));
    reply(true);
    const source = typeof data.source === "string" ? data.source.replace(/^www\./, "").slice(0, 80) : "";
    if (source) toast(t(`已收到來自 ${source} 的照片，正在找出每一件。`, `Photo from ${source} received. Finding every piece…`));
    captureFiles([file]);
  });
  $("#clear-file").addEventListener("click", () => setFile(null));
  $("#provider-select").addEventListener("change", () => { showError("upload-error", ""); updateProviderNote(); });
  $("#provider-use-cloud").addEventListener("click", () => {
    if (state.healthChecking || state.health?.meta?.configured !== true || state.uploading || state.generating) return;
    $("#provider-select").value = "meta";
    showError("upload-error", "");
    updateProviderNote();
    $("#provider-select").focus({ preventScroll: true });
  });
  $("#provider-recheck").addEventListener("click", () => refreshHealth(true));
  $("#upload-discover").addEventListener("change", () => { $("#upload-discovery-market").disabled = state.uploading || !$("#upload-discover").checked; });
  $$('input[name="mode"]').forEach(input => input.addEventListener("change", updateProviderNote));
  $("#search-input").addEventListener("input", event => { state.filter.q = event.target.value; renderItems(); });
  $("#category-filter").addEventListener("change", event => { state.filter.category = event.target.value; renderItems(); });
  $("#duplicates-button").addEventListener("click", openDuplicates);
  $("#person-save").addEventListener("click", savePersonSelection);
  $("#person-clear").addEventListener("click", () => { state.personDraft = null; showError("person-error", ""); renderPersonSelection(); });
  $("#person-default").addEventListener("click", () => { state.personDraft = [0.2, 0.05, 0.8, 0.95]; showError("person-error", ""); renderPersonSelection(); $("#person-box").focus({ preventScroll: true }); });
  $("#health-button").addEventListener("click", () => { renderHealth(); $("#status-dialog").showModal(); });
  $("#health-refresh").addEventListener("click", () => withButton($("#health-refresh"), t("正在檢查…","Checking…"), () => refreshHealth(true)));
  $("#history-refresh").addEventListener("click", () => withButton($("#history-refresh"), t("正在讀取…","Loading…"), () => refreshData(true)));
  $("#refresh-button").addEventListener("click", () => withButton($("#refresh-button"), t("重試中…","Retrying…"), async () => { await Promise.allSettled([refreshData(true), refreshHealth(true)]); }));
  $("#confirm-cancel").addEventListener("click", () => finishConfirmation(false));
  $("#confirm-submit").addEventListener("click", () => finishConfirmation(true));
  $("#confirm-dialog").addEventListener("cancel", event => { event.preventDefault(); finishConfirmation(false); });
  $("#account-bootstrap-retry").addEventListener("click", initializeAccount);
  $("#account-button").addEventListener("click", () => openAccountManagement());
  $("#storage-manage").addEventListener("click", () => openAccountManagement(true));
  $("#storage-refresh").addEventListener("click", refreshStorage);
  $("#storage-history").addEventListener("click", () => { $("#account-dialog").close(); setView("history", true); });
  $("#storage-cleanup").addEventListener("click", () => withButton($("#storage-cleanup"), t("正在清理…","Cleaning up…"), async () => {
    if (storage.cleaning || storage.loading || !(storage.cleanupState === "pending" || storage.data?.cleanup?.pending_directories > 0)) return;
    const request = ++storage.request;
    storage.cleaning = true; storage.error = ""; renderStorage();
    try {
      const data = await api("/api/storage/cleanup", { method: "POST" });
      if (request !== storage.request || account.blocked) return;
      acceptStorage(data);
      toast(data.cleanup.pending_directories > 0 ? "仍有媒體未能清理，檔案仍計入容量。請稍後再試。" : "待清理檔案已移除，用量已更新。", data.cleanup.pending_directories > 0);
    } catch (error) { if (request === storage.request && !account.blocked) storage.error = error.message; }
    finally { if (request === storage.request) { storage.cleaning = false; renderStorage(); } }
  }));
  $("#account-dialog").addEventListener("close", () => { $("#password-form").reset(); });
  $("#password-form").addEventListener("submit", async event => {
    event.preventDefault();
    const currentPassword = $("#current-password").value;
    const newPassword = $("#new-password").value;
    const length = [...newPassword].length;
    const message = !currentPassword ? t("請輸入目前密碼。","Enter your current password.") : length < 12 || length > 128 ? t("新密碼需為 12–128 個字元；輸入不會被截短。","Password must be 12–128 characters.") : newPassword !== $("#confirm-password").value ? t("兩次輸入的新密碼不同，請再確認。","Passwords do not match.") : "";
    showError("password-error", message);
    if (message) return;
    await withButton(event.target.querySelector('[type="submit"]'), t("正在更新…","Updating…"), async () => {
      try {
        if (!await verifyAccountSession() || account.blocked) return;
        const expected = account.user.id;
        const result = await api("/auth/password", { method: "POST", body: JSON.stringify({ current_password: currentPassword, new_password: newPassword }) });
        if (result.user?.id !== expected) { accountChanged(); return; }
        if (typeof result.csrf_token !== "string" || !result.csrf_token) throw new Error(t("密碼已送出更新，但無法確認新工作階段；請重新登入。","Password updated but session unconfirmed; sign in again."));
        account.user = result.user; account.csrf = result.csrf_token;
        $("#password-form").reset();
        $("#password-success").textContent = t("密碼已更新。其他登入工作階段已撤銷，這個分頁可繼續使用。","Password updated. Other sessions revoked; this tab remains active.");
        $("#password-success").hidden = false;
        renderAccount(); announceAccountChange();
      } catch (error) { showError("password-error", error.message); }
    });
  });
  $("#logout-button").addEventListener("click", () => withButton($("#logout-button"), t("正在登出…","Signing out…"), async () => {
    showError("logout-error", "");
    try {
      if (!await verifyAccountSession() || account.blocked) return;
      await api("/auth/logout", { method: "POST" });
      clearAccountWorkspace(); account.user = null; account.csrf = null; account.blocked = true;
      renderAccount(); announceAccountChange(); location.replace("/login");
    } catch (error) { showError("logout-error", error.message); }
  }));
  $("#session-dialog").addEventListener("cancel", event => event.preventDefault());
  $("#reauth-form").addEventListener("submit", async event => {
    event.preventDefault();
    if (!account.user || !account.blocked) return;
    const password = $("#reauth-password").value;
    if (!password) { showError("reauth-error", t("請輸入密碼。","Enter your password.")); return; }
    const expected = account.user.id;
    const username = account.user.username;
    showError("reauth-error", "");
    await withButton(event.target.querySelector('[type="submit"]'), t("正在登入…","Signing in…"), async () => {
      try {
        const session = await api("/auth/session");
        if (session.mode !== "hosted") { accountChanged(); return; }
        if (session.authenticated && session.user?.id !== expected) { accountChanged(); return; }
        account.csrf = session.authenticated ? session.csrf_token : null;
        const result = await api("/auth/login", { method: "POST", body: JSON.stringify({ username, password }) });
        if (result.user?.id !== expected) { accountChanged(); return; }
        if (!acceptSession({ mode: "hosted", authenticated: true, ...result })) return;
        announceAccountChange();
        await resumeAccount();
      } catch (error) { showError("reauth-error", error.message); }
    });
  });
  $("#session-leave").addEventListener("click", () => {
    const changed = account.reason === "changed";
    clearAccountWorkspace(); account.user = null; account.csrf = null;
    location.replace(changed ? "/" : "/login");
  });
  accountChannel?.addEventListener("message", async event => {
    if (event.data !== "session-changed" || account.mode !== "hosted" || !account.ready || account.reason === "changed") return;
    try { if (await verifyAccountSession() && account.blocked) await resumeAccount(); }
    catch (_) { blockAccount("無法確認其他分頁更新後的登入狀態。草稿仍保留，請重新登入以繼續。"); }
  });
  document.addEventListener("click", async event => {
    const link = event.target.closest("a[download]");
    if (!link || account.mode !== "hosted") return;
    const url = new URL(link.href, location.href);
    if (url.origin !== location.origin || !/^\/(?:api|media)\//.test(url.pathname)) return;
    event.preventDefault();
    try {
      const blob = await api(url.pathname + url.search, { responseType: "blob" });
      const objectURL = URL.createObjectURL(blob); account.downloadURLs.add(objectURL);
      const download = document.createElement("a"); download.href = objectURL;
      download.download = link.download || (url.pathname.startsWith("/api/export") ? `rewear-export.${url.searchParams.get("format") === "csv" ? "csv" : "json"}` : url.pathname.split("/").pop());
      document.body.append(download); download.click(); download.remove();
      setTimeout(() => { URL.revokeObjectURL(objectURL); account.downloadURLs.delete(objectURL); }, 1000);
    } catch (error) { toast(error.message, true); }
  }, true);
  $("#item-dialog").addEventListener("close", () => { state.editingItemId = null; });
  $("#item-dialog").addEventListener("change", event => {
    if (event.target.matches("[data-reconstruction-reference]")) changeReconstructionReference(event.target.dataset.reconstructionReference, event.target.checked);
  });
  $("#job-dialog").addEventListener("close", () => { state.selectedJobId = null; $$("video", $("#job-dialog")).forEach(video => video.pause()); });
  $("#batch-dialog").addEventListener("close", () => { state.selectedBatchId = null; });
  $("#person-dialog").addEventListener("close", () => { state.personFileId = null; state.personDraft = null; });

  let personDrag = null;
  const personStage = $("#person-stage");
  const personPoint = event => { const rect = personStage.getBoundingClientRect(); return [Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width)), Math.min(1, Math.max(0, (event.clientY - rect.top) / rect.height))]; };
  personStage.addEventListener("pointerdown", event => {
    if (event.button !== 0 || !$("#person-photo").naturalWidth) return;
    event.preventDefault();
    personDrag = { id: event.pointerId, start: personPoint(event) };
    personStage.setPointerCapture(event.pointerId);
  });
  personStage.addEventListener("pointermove", event => {
    if (!personDrag || personDrag.id !== event.pointerId) return;
    const end = personPoint(event);
    const start = personDrag.start;
    state.personDraft = [Math.min(start[0], end[0]), Math.min(start[1], end[1]), Math.max(start[0], end[0]), Math.max(start[1], end[1])];
    renderPersonSelection();
  });
  personStage.addEventListener("pointerup", event => {
    if (!personDrag || personDrag.id !== event.pointerId) return;
    personDrag = null;
    if (personStage.hasPointerCapture(event.pointerId)) personStage.releasePointerCapture(event.pointerId);
    if (!state.personDraft || !validPersonBox(state.personDraft)) { state.personDraft = null; showError("person-error", t("請拖曳出至少 1% 寬與高的範圍，或使用下方欄位設定。","Drag at least 1% wide and tall, or use the fields below.")); }
    else showError("person-error", "");
    renderPersonSelection();
  });
  personStage.addEventListener("pointercancel", () => { personDrag = null; });
  $("#person-box").addEventListener("keydown", event => {
    if (!state.personDraft || !["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(event.key)) return;
    event.preventDefault();
    const box = [...state.personDraft];
    const horizontal = event.key === "ArrowLeft" || event.key === "ArrowRight";
    const index = horizontal ? 0 : 1;
    const delta = event.key === "ArrowLeft" || event.key === "ArrowUp" ? -0.01 : 0.01;
    if (event.shiftKey) box[index + 2] += delta;
    else { const move = Math.min(1 - box[index + 2], Math.max(-box[index], delta)); box[index] += move; box[index + 2] += move; }
    if (validPersonBox(box)) { state.personDraft = box; renderPersonSelection(); }
  });
  $$("#person-coordinates input").forEach(input => input.addEventListener("change", () => {
    const values = ["left", "top", "right", "bottom"].map(name => $(`#person-${name}`).value.trim());
    const coords = values.map(value => value === "" ? NaN : Number(value) / 100);
    if (validPersonBox(coords)) { state.personDraft = coords; showError("person-error", ""); renderPersonSelection(); }
  }));

  document.documentElement.classList.toggle("touch", matchMedia("(hover: none), (pointer: coarse)").matches);
  $("#item-dialog").addEventListener("click", event => { if (event.target === event.currentTarget && !state.itemDirty) event.currentTarget.close(); });
  if ("IntersectionObserver" in window) {
    new IntersectionObserver(([entry]) => document.body.classList.toggle("capture-offscreen", !entry.isIntersecting)).observe($(".capture-actions"));
  }
  const dropZone = $("#drop-zone");
  let dragDepth = 0;
  dropZone.addEventListener("dragenter", event => { event.preventDefault(); dragDepth++; dropZone.classList.add("dragover"); });
  dropZone.addEventListener("dragover", event => { event.preventDefault(); if (event.dataTransfer) event.dataTransfer.dropEffect = "copy"; });
  dropZone.addEventListener("dragleave", event => { event.preventDefault(); dragDepth--; if (dragDepth <= 0) dropZone.classList.remove("dragover"); });
  dropZone.addEventListener("drop", event => { event.preventDefault(); dragDepth = 0; dropZone.classList.remove("dragover"); const files = event.dataTransfer?.files; if (files?.length) captureFiles(files); });
  document.addEventListener("dragover", event => { if (event.dataTransfer?.types.includes("Files")) event.preventDefault(); });
  document.addEventListener("drop", event => { if (event.dataTransfer?.types.includes("Files")) event.preventDefault(); });
  document.addEventListener("keydown", event => {
    if (event.key === "/" && !event.metaKey && !event.ctrlKey && !event.altKey && !event.target.closest("input, textarea, select, [contenteditable], dialog[open]")) {
      event.preventDefault(); setView("wardrobe"); $("#search-input").focus();
    }
    if (event.key === "Escape") $$(".export-menu[open]").forEach(menu => { menu.open = false; });
  });
  document.addEventListener("click", event => { if (!event.target.closest(".export-menu")) $$(".export-menu[open]").forEach(menu => { menu.open = false; }); });
  document.addEventListener("visibilitychange", () => { if (!document.hidden && account.ready && !account.blocked) { refreshData(); schedulePoll(); } });
  window.addEventListener("hashchange", () => setView(location.hash.slice(1)));
  window.addEventListener("beforeunload", () => { state.files.forEach(entry => { if (entry.url) URL.revokeObjectURL(entry.url); }); });

  const langToggle = document.getElementById("lang-toggle");
  if (langToggle && window.i18n) langToggle.addEventListener("click", () => window.i18n.toggleLang());

  setView(location.hash.slice(1));
  initializeAccount();
})();

"use strict";

// ---------------------------------------------------------------- helpers
const $ = (id) => document.getElementById(id);
const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

async function api(method, path, body) {
  const res = await fetch(path, {
    method,
    headers: body === undefined ? {} : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let data = null;
  try { data = await res.json(); } catch (_) { /* empty body */ }
  if (!res.ok) {
    const detail = data && data.detail;
    throw new Error(typeof detail === "string" ? detail : `Request failed (${res.status})`);
  }
  return data;
}

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

let toastTimer = null;
function toast(message, ms = 4000) {
  const t = $("toast");
  t.textContent = message;
  t.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add("hidden"), ms);
}

function fmtEta(seconds) {
  if (seconds === null || seconds === undefined || seconds <= 0 || !isFinite(seconds)) return "-";
  const s = Math.floor(seconds % 60), m = Math.floor(seconds / 60) % 60, h = Math.floor(seconds / 3600);
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}
const MB = 1024 * 1024;
function fmtSize(done, total) {
  return total > 0 ? `${(done / MB).toFixed(1)} / ${(total / MB).toFixed(1)} MB` : "-";
}
function fmtSpeed(v) { return v > 0 ? `${v.toFixed(2)} MB/s` : "-"; }

function statusClass(status) {
  if (status.includes("Error") || status === "CAPTCHA Timeout" || status === "Contains Errors") return "err";
  if (status === "Completed" || status === "Extracted") return "ok";
  if (status === "Waiting for you to solve CAPTCHA") return "warn";
  return "";
}

// Resolves with the dialog's returnValue when it closes.
function openDialog(dlg) {
  return new Promise((resolve) => {
    dlg.addEventListener("close", () => resolve(dlg.returnValue), { once: true });
    dlg.returnValue = "";
    dlg.showModal();
  });
}

async function confirmDialog(title, bodyText, { files = false, dontAsk = false, yes = "Yes" } = {}) {
  $("confirm-title").textContent = title;
  $("confirm-body").textContent = bodyText;
  $("confirm-yes").textContent = yes;
  $("confirm-files").checked = false;
  $("confirm-dontask").checked = false;
  $("confirm-files-row").classList.toggle("hidden", !files);
  $("confirm-dontask-row").classList.toggle("hidden", !dontAsk);
  const result = await openDialog($("dlg-confirm"));
  return {
    yes: result === "yes",
    files: $("confirm-files").checked,
    dontAsk: $("confirm-dontask").checked,
  };
}

// ------------------------------------------------------------------ state
let state = null;
let settings = {};
const selected = new Set();      // task uids
const collapsed = new Set();     // batch names
let lastEventId = 0;
let eventsPrimed = false;
const speedSamples = [];

function selectedUids() {
  const live = new Set(state ? state.tasks.map((t) => t.uid) : []);
  return [...selected].filter((u) => live.has(u));
}

// ----------------------------------------------------------------- render
const rows = new Map();   // key -> {tr, refs}

function makeBatchRow(name) {
  const tr = el("tr", "batch");
  const check = el("input"); check.type = "checkbox";
  const tdCheck = el("td", "c-check"); tdCheck.append(check);
  const caret = el("span", "caret");
  const label = el("span");
  const tdName = el("td", "name"); tdName.append(caret, label);
  const status = el("td"), prog = el("td"), speed = el("td"), eta = el("td"), size = el("td");
  const bar = el("div", "bar"), fill = el("i"), pct = el("span");
  bar.append(fill, pct); prog.append(bar);
  tr.append(tdCheck, tdName, status, prog, speed, eta, size);
  check.addEventListener("change", () => {
    for (const t of state.tasks.filter((x) => x.folder === name)) {
      check.checked ? selected.add(t.uid) : selected.delete(t.uid);
    }
    renderTable();
  });
  tdName.addEventListener("click", () => {
    collapsed.has(name) ? collapsed.delete(name) : collapsed.add(name);
    renderTable();
  });
  return { tr, refs: { check, caret, label, status, fill, pct, speed, eta, size } };
}

function makeTaskRow(uid) {
  const tr = el("tr", "task");
  tr.dataset.uid = uid;
  const check = el("input"); check.type = "checkbox";
  const tdCheck = el("td", "c-check"); tdCheck.append(check);
  const name = el("td", "name");
  const status = el("td"), prog = el("td"), speed = el("td"), eta = el("td"), size = el("td");
  const bar = el("div", "bar"), fill = el("i"), pct = el("span");
  bar.append(fill, pct); prog.append(bar);
  tr.append(tdCheck, name, status, prog, speed, eta, size);
  check.addEventListener("change", () => {
    check.checked ? selected.add(uid) : selected.delete(uid);
    renderTable();
  });
  tr.addEventListener("click", (e) => {
    if (e.target === check) return;
    if (!e.ctrlKey && !e.metaKey) selected.clear();
    selected.has(uid) ? selected.delete(uid) : selected.add(uid);
    renderTable();
  });
  tr.addEventListener("contextmenu", (e) => {
    e.preventDefault();
    if (!selected.has(uid)) { selected.clear(); selected.add(uid); renderTable(); }
    showContextMenu(e.clientX, e.clientY);
  });
  return { tr, refs: { check, name, status, fill, pct, speed, eta, size } };
}

function setBar(refs, progress, text, cls) {
  refs.fill.style.width = `${Math.max(0, Math.min(100, progress))}%`;
  refs.pct.textContent = text;
  refs.fill.parentElement.className = "bar" + (cls ? ` st-${cls}` : "");
}

function renderTable() {
  if (!state) return;
  const body = $("queue-body");
  const wanted = [];

  for (const batch of state.batches) {
    const key = `b:${batch.name}`;
    let entry = rows.get(key);
    if (!entry) { entry = makeBatchRow(batch.name); rows.set(key, entry); }
    const r = entry.refs;
    const children = state.tasks.filter((t) => t.folder === batch.name);
    const nSel = children.filter((t) => selected.has(t.uid)).length;
    r.check.checked = children.length > 0 && nSel === children.length;
    r.check.indeterminate = nSel > 0 && nSel < children.length;
    r.caret.textContent = collapsed.has(batch.name) ? "▸" : "▾";
    r.label.textContent = batch.name;
    r.status.textContent = batch.status;
    r.status.className = "status-" + (statusClass(batch.status) || "n");
    setBar(r, batch.progress, batch.progress_known ? `${batch.progress.toFixed(1)}%` : "-", statusClass(batch.status));
    r.speed.textContent = fmtSpeed(batch.speed);
    r.eta.textContent = fmtEta(batch.eta);
    r.size.textContent = fmtSize(batch.downloaded_bytes, batch.total_bytes);
    wanted.push([key, entry.tr]);

    if (collapsed.has(batch.name)) continue;
    for (const t of children) {
      const tkey = `t:${t.uid}`;
      let te = rows.get(tkey);
      if (!te) { te = makeTaskRow(t.uid); rows.set(tkey, te); }
      const tr = te.refs;
      tr.check.checked = selected.has(t.uid);
      te.tr.classList.toggle("selected", selected.has(t.uid));
      tr.name.textContent = t.filename;
      tr.name.title = t.link;
      tr.status.textContent = t.status;
      tr.status.className = "status-" + (statusClass(t.status) || "n");
      if (t.status.includes("Error") && t.error) tr.status.title = t.error;
      else if (t.status === "Waiting for you to solve CAPTCHA") tr.status.title = "Complete the “Verify you are human” check in the solver browser.";
      else tr.status.title = "";
      setBar(tr, t.progress, t.progress_text, statusClass(t.status));
      tr.speed.textContent = fmtSpeed(t.speed);
      tr.eta.textContent = fmtEta(t.eta);
      tr.size.textContent = fmtSize(t.downloaded_bytes, t.total_bytes);
      wanted.push([tkey, te.tr]);
    }
  }

  const keep = new Set(wanted.map(([k]) => k));
  for (const [key, entry] of rows) {
    if (!keep.has(key)) { entry.tr.remove(); rows.delete(key); }
  }
  wanted.forEach(([, tr], i) => {
    if (body.children[i] !== tr) body.insertBefore(tr, body.children[i] || null);
  });
  $("empty").classList.toggle("hidden", state.tasks.length > 0);
}

function drawGraph() {
  const c = $("graph"), g = c.getContext("2d");
  const w = c.width, h = c.height;
  g.clearRect(0, 0, w, h);
  if (speedSamples.length < 2) return;
  const max = Math.max(1, ...speedSamples) * 1.2;
  const step = w / (30 - 1);
  const style = getComputedStyle(document.documentElement);
  const accent = style.getPropertyValue("--accent").trim() || "#388bfd";
  g.beginPath();
  speedSamples.forEach((v, i) => {
    const x = w - (speedSamples.length - 1 - i) * step, y = h - (v / max) * (h - 4) - 2;
    i === 0 ? g.moveTo(x, y) : g.lineTo(x, y);
  });
  g.strokeStyle = accent; g.lineWidth = 2; g.stroke();
  g.lineTo(w, h); g.lineTo(w - (speedSamples.length - 1) * step, h); g.closePath();
  g.globalAlpha = 0.18; g.fillStyle = accent; g.fill(); g.globalAlpha = 1;
}

function handleEvents(events) {
  const fresh = events.filter((e) => e.id > lastEventId);
  if (fresh.length) lastEventId = fresh[fresh.length - 1].id;
  if (!eventsPrimed) { eventsPrimed = true; return; }   // don't replay old ones on page load
  for (const e of fresh) {
    if (e.type === "offpeak_summary") {
      const s = e.summary;
      toast(`Scheduled window finished: ${s.files_completed} file(s), ` +
        `${(s.bytes_downloaded / 1024 ** 3).toFixed(2)} GB, avg ${s.avg_speed_mbps.toFixed(2)} MB/s`, 12000);
    }
  }
}

function render(s) {
  state = s;
  $("version").textContent = s.version;
  $("stat-active").textContent = s.stats.active;
  $("stat-queued").textContent = s.stats.queued;
  $("stat-done").textContent = s.stats.done;
  $("stat-speed").textContent = `${s.stats.speed.toFixed(2)} MB/s`;
  if (s.stats.speed > 0 || s.tasks.some((t) => t.status === "Downloading")) {
    speedSamples.push(s.stats.speed);
    while (speedSamples.length > 30) speedSamples.shift();
  }
  drawGraph();

  const dot = $("schedule-dot");
  dot.classList.toggle("hidden", !s.schedule.description);
  dot.classList.toggle("active", s.schedule.window_active);
  dot.title = s.schedule.description + (s.schedule.window_active ? " (window open)" : "");

  $("captcha-banner").classList.toggle("hidden", !s.stats.captcha_waiting);
  if (!s.stats.captcha_waiting && $("dlg-solver").open) closeSolver();

  handleEvents(s.events);
  renderTable();
}

// -------------------------------------------------------------- websocket
function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/api/ws`);
  ws.onmessage = (m) => render(JSON.parse(m.data));
  ws.onclose = () => setTimeout(connect, 2000);
}

// ---------------------------------------------------------------- actions
function needSelection() {
  const uids = selectedUids();
  if (!uids.length) toast("Select one or more downloads first.");
  return uids;
}

async function guard(fn) {
  try { return await fn(); } catch (e) { toast(e.message); }
}

const actions = {
  start: (uids) => api("POST", "/api/tasks/start", { uids }),
  pause: (uids) => api("POST", "/api/tasks/pause", { uids }),
  cancel: (uids) => api("POST", "/api/tasks/cancel", { uids }),
  retry: (uids) => api("POST", "/api/tasks/retry", { uids }),

  async extract(uids) {
    let res = await api("POST", "/api/tasks/extract", { uids });
    if (res.confirm) {
      const list = res.confirm.slice(0, 10).map((n) => `  • ${n}`).join("\n") +
        (res.confirm.length > 10 ? `\n  … and ${res.confirm.length - 10} more` : "");
      const c = await confirmDialog("Already Extracted",
        `These are already extracted:\n\n${list}\n\nExtract them again? Existing files will be overwritten.`);
      res = await api("POST", "/api/tasks/extract",
        { uids, confirm: true, skip_folders: c.yes ? [] : res.confirm });
    }
    if (!res.started.length) toast("Nothing to extract.");
  },

  async delete(uids) {
    let deleteFiles = false;
    if (!settings.skip_delete_confirmation) {
      const c = await confirmDialog("Confirm Delete",
        `Are you sure you want to delete ${uids.length} selected task(s)?`, { files: true, dontAsk: true });
      if (!c.yes) return;
      deleteFiles = c.files;
      if (c.dontAsk) await saveSettings({ skip_delete_confirmation: true });
    }
    await api("POST", "/api/tasks/delete", { uids, delete_files: deleteFiles });
    uids.forEach((u) => selected.delete(u));
  },

  async redownload(uids) {
    let res = await api("POST", "/api/tasks/redownload", { uids });
    if (res.confirm) {
      const c = await confirmDialog("Confirm Force Redownload",
        `${res.confirm.length} selected file(s) already have download progress.\n\n` +
        "Forcing a redownload will PERMANENTLY DELETE the partially downloaded file(s) and start from 0%.\n\n" +
        "Are you sure you want to proceed?");
      if (!c.yes) return;
      res = await api("POST", "/api/tasks/redownload?confirm=true", { uids });
    }
    if (res.skipped || res.failed || res.queued === 0) {
      toast(`Queued: ${res.queued}  Skipped active tasks: ${res.skipped}  Failed: ${res.failed}`);
    }
  },

  async copyError(uids) {
    const failed = state.tasks.find((t) => uids.includes(t.uid) && t.status.includes("Error"));
    if (!failed) { toast("Select a failed task first, then copy its error details."); return; }
    const { text } = await api("GET", `/api/tasks/${failed.uid}/error`);
    await copyText(text);
    toast("Error details copied to your clipboard.");
  },
};

async function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) {
    await navigator.clipboard.writeText(text);
    return;
  }
  const ta = el("textarea"); ta.value = text;
  document.body.append(ta); ta.select(); document.execCommand("copy"); ta.remove();
}

function runAction(name) {
  const uids = needSelection();
  if (uids.length) guard(() => actions[name](uids));
}

// ----------------------------------------------------------- context menu
function showContextMenu(x, y) {
  const menu = $("ctx");
  menu.replaceChildren();
  const add = (label, fn) => { const d = el("div", null, label); d.addEventListener("click", () => { hideContextMenu(); fn(); }); menu.append(d); };
  add("Start / Resume", () => runAction("start"));
  add("Pause", () => runAction("pause"));
  add("Cancel", () => runAction("cancel"));
  add("Retry", () => runAction("retry"));
  menu.append(el("hr"));
  add("Extract Now", () => runAction("extract"));
  add("Force Redownload", () => runAction("redownload"));
  menu.append(el("hr"));
  add("Copy Error Details", () => runAction("copyError"));
  add("Schedule download at specific interval", () => openScheduler(true));
  menu.append(el("hr"));
  add("Delete", () => runAction("delete"));
  menu.classList.remove("hidden");
  menu.style.left = `${Math.min(x, innerWidth - menu.offsetWidth - 8)}px`;
  menu.style.top = `${Math.min(y, innerHeight - menu.offsetHeight - 8)}px`;
}
function hideContextMenu() { $("ctx").classList.add("hidden"); }
document.addEventListener("click", hideContextMenu);
document.addEventListener("scroll", hideContextMenu, true);

// -------------------------------------------------------------- add links
function appendLinks(text) {
  const box = $("links");
  box.value = box.value.trim() ? `${box.value.replace(/\s+$/, "")}\n${text}` : text;
}

$("links").addEventListener("paste", async (e) => {
  const html = e.clipboardData && e.clipboardData.getData("text/html");
  if (!html) return;   // plain text pastes natively
  const text = e.clipboardData.getData("text/plain");
  e.preventDefault();
  const res = await guard(() => api("POST", "/api/links/parse", { text, html }));
  if (res && res.links.length) appendLinks(res.links.join("\n"));
  else if (text) appendLinks(text);
});

$("btn-paste").addEventListener("click", () => guard(async () => {
  let html = "", text = "";
  if (navigator.clipboard && navigator.clipboard.read) {
    for (const item of await navigator.clipboard.read()) {
      if (item.types.includes("text/html")) html = await (await item.getType("text/html")).text();
      if (item.types.includes("text/plain")) text = await (await item.getType("text/plain")).text();
    }
  } else if (navigator.clipboard && navigator.clipboard.readText) {
    text = await navigator.clipboard.readText();
  } else {
    toast("Your browser can't read the clipboard here. Paste into the box with Ctrl+V instead.");
    return;
  }
  const res = await api("POST", "/api/links/parse", { text, html });
  if (res.links.length) appendLinks(res.links.join("\n"));
}));

$("btn-import").addEventListener("click", () => $("file-import").click());
$("file-import").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  e.target.value = "";
  if (file) appendLinks(await file.text());
});

$("btn-add").addEventListener("click", () => guard(async () => {
  const text = $("links").value.trim();
  if (!text) return;
  const parsed = await api("POST", "/api/links/parse", { text });
  if (!parsed.links.length) { toast("No http(s) links found."); return; }
  $("folder-name").value = parsed.suggested_folder;
  const result = openDialog($("dlg-folder"));
  $("folder-name").focus();
  if ((await result) !== "ok" || !$("folder-name").value.trim()) return;
  await api("POST", "/api/tasks", {
    links: parsed.links,
    folder_name: $("folder-name").value.trim(),
    save_dir: $("save-dir").value.trim() || null,
  });
  $("links").value = "";
}));

// ---------------------------------------------------------------- toolbar
document.querySelectorAll("[data-action]").forEach((b) =>
  b.addEventListener("click", () => runAction(b.dataset.action)));

$("btn-select-all").addEventListener("click", () => {
  if (!state || !state.tasks.length) return;
  const all = state.tasks.every((t) => selected.has(t.uid));
  state.tasks.forEach((t) => (all ? selected.delete(t.uid) : selected.add(t.uid)));
  renderTable();
});
$("btn-clear").addEventListener("click", () => guard(() => api("POST", "/api/tasks/clear-finished", {})));

document.addEventListener("keydown", (e) => {
  if (document.querySelector("dialog[open]")) return;
  if (/^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName)) return;
  if (e.ctrlKey || e.metaKey || e.altKey || !state) return;
  const uids = selectedUids();
  if (!uids.length) return;
  const key = e.key.toLowerCase();
  if (key === " ") {
    e.preventDefault();
    const running = state.tasks.some((t) => uids.includes(t.uid) && ["Downloading", "Pending", "Starting..."].includes(t.status));
    runAction(running ? "pause" : "start");
  } else if (key === "s") runAction("start");
  else if (key === "p") runAction("pause");
  else if (key === "c") runAction("cancel");
  else if (key === "r") runAction("retry");
  else if (key === "f") runAction("redownload");
  else if (key === "delete" || key === "backspace") runAction("delete");
});

// ---------------------------------------------------------------- settings
function applyTheme(name) {
  document.documentElement.dataset.theme = (name || "Dark").toLowerCase();
  drawGraph();
}

async function loadSettings() {
  settings = await api("GET", "/api/settings");
  applyTheme(settings.theme);
  $("auto-extract").checked = !!settings.extract_after_download;
  if (!$("save-dir").value) $("save-dir").value = settings.default_save_dir || "";
}

async function saveSettings(updates) {
  settings = await api("PUT", "/api/settings", updates);
  applyTheme(settings.theme);
  $("auto-extract").checked = !!settings.extract_after_download;
  return settings;
}

$("auto-extract").addEventListener("change", (e) =>
  guard(() => saveSettings({ extract_after_download: e.target.checked })));

$("btn-theme").addEventListener("click", () =>
  guard(() => saveSettings({ theme: document.documentElement.dataset.theme === "dark" ? "Light" : "Dark" })));

$("btn-settings").addEventListener("click", () => guard(async () => {
  await loadSettings();
  const form = $("form-settings");
  for (const input of form.querySelectorAll("[name]")) {
    if (input.type === "checkbox") input.checked = !!settings[input.name];
    else input.value = settings[input.name] ?? "";
  }
  $("settings-err").textContent = "";
  await openDialog($("dlg-settings"));
}));

$("settings-save").addEventListener("click", async (e) => {
  e.preventDefault();
  const updates = {};
  for (const input of $("form-settings").querySelectorAll("[name]")) {
    if (input.type === "checkbox") updates[input.name] = input.checked;
    else if (input.type === "number") updates[input.name] = parseInt(input.value, 10);
    else updates[input.name] = input.value;
  }
  try {
    await saveSettings(updates);
    $("save-dir").value = settings.default_save_dir;
    $("dlg-settings").close("save");
  } catch (err) {
    $("settings-err").textContent = err.message;
  }
});

// --------------------------------------------------------------- scheduler
function initScheduler() {
  for (const p of ["start", "end"]) {
    for (let h = 1; h <= 12; h++) $(`s-${p}-h`).add(new Option(String(h).padStart(2, "0"), h));
    for (let m = 0; m < 60; m++) $(`s-${p}-m`).add(new Option(String(m).padStart(2, "0"), m));
  }
  DAYS.forEach((d, i) => {
    const label = el("label"); const cb = el("input"); cb.type = "checkbox"; cb.value = i;
    label.append(cb, d); $("s-days").append(label);
  });
  document.querySelectorAll("[name=recurrence]").forEach((r) => r.addEventListener("change", syncRecurrence));
}

function syncRecurrence() {
  const weekly = document.querySelector("[name=recurrence]:checked").value === "weekly";
  $("s-days").querySelectorAll("input").forEach((c) => (c.disabled = !weekly));
  $("s-date").disabled = weekly;
}

function setTime(prefix, hhmm) {
  const [h, m] = hhmm.split(":").map(Number);
  $(`s-${prefix}-h`).value = h % 12 || 12;
  $(`s-${prefix}-m`).value = m;
  $(`s-${prefix}-ap`).value = h < 12 ? "AM" : "PM";
}
function getTime(prefix) {
  let h = Number($(`s-${prefix}-h`).value) % 12;
  if ($(`s-${prefix}-ap`).value === "PM") h += 12;
  return `${String(h).padStart(2, "0")}:${String(Number($(`s-${prefix}-m`).value)).padStart(2, "0")}`;
}

let schedTargets = null;
function openScheduler(selectedOnly) {
  const cfg = state.schedule.config;
  let scope = "entire queue";
  schedTargets = null;
  if (selectedOnly) {
    const uids = needSelection();
    if (!uids.length) return;
    schedTargets = uids;
    scope = uids.length === 1 ? state.tasks.find((t) => t.uid === uids[0]).filename : `${uids.length} selected download(s)`;
  }
  $("sched-scope").textContent = scope;
  setTime("start", cfg.start || "02:00");
  setTime("end", cfg.end || "06:00");
  const active = new Set(cfg.days || DAYS.map((_, i) => i));
  $("s-days").querySelectorAll("input").forEach((c) => (c.checked = active.has(Number(c.value))));
  document.querySelector(`[name=recurrence][value=${cfg.recurrence === "once" ? "once" : "weekly"}]`).checked = true;
  $("s-date").value = cfg.date || new Date().toISOString().slice(0, 10);
  $("s-keep-awake").checked = cfg.keep_awake !== false;
  $("sched-remove").classList.toggle("hidden", !cfg.enabled);
  $("sched-err").textContent = "";
  syncRecurrence();
  openDialog($("dlg-schedule"));
}

async function submitSchedule(enabled) {
  const body = {
    enabled,
    start: getTime("start"), end: getTime("end"),
    recurrence: document.querySelector("[name=recurrence]:checked").value,
    days: [...$("s-days").querySelectorAll("input:checked")].map((c) => Number(c.value)),
    date: $("s-date").value,
    keep_awake: $("s-keep-awake").checked,
    uids: schedTargets,
  };
  try {
    await api("PUT", "/api/schedule", body);
    $("dlg-schedule").close(enabled ? "add" : "remove");
  } catch (err) {
    $("sched-err").textContent = err.message;
  }
}
$("sched-add").addEventListener("click", (e) => { e.preventDefault(); submitSchedule(true); });
$("sched-remove").addEventListener("click", (e) => { e.preventDefault(); submitSchedule(false); });
$("btn-scheduler").addEventListener("click", () => openScheduler(false));
$("schedule-dot").addEventListener("click", () => openScheduler(false));

// ----------------------------------------------------------- CAPTCHA view
$("btn-solve").addEventListener("click", async () => {
  const info = await guard(() => api("GET", "/api/captcha"));
  if (!info) return;
  $("solver-frame").src = `${location.protocol}//${location.hostname}:${info.novnc_port}/vnc.html?autoconnect=1&resize=scale`;
  openDialog($("dlg-solver")).then(closeSolver);
});
function closeSolver() {
  $("solver-frame").src = "about:blank";
  if ($("dlg-solver").open) $("dlg-solver").close();
}

// Enter inside a dialog field must not trigger the first (Cancel) button.
document.querySelectorAll("dialog").forEach((d) =>
  d.addEventListener("keydown", (e) => {
    if (e.key !== "Enter" || e.target.tagName !== "INPUT" || e.target.type === "checkbox") return;
    e.preventDefault();
    const primary = d.querySelector("button.primary");
    if (primary) primary.click();
  }));

// ------------------------------------------------------------------- boot
initScheduler();
guard(loadSettings);
guard(async () => render(await api("GET", "/api/state")));
connect();

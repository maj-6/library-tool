"use strict";

/* Catalog Explorer front-end.
 *
 * Two top-level tabs:
 *   CHECKED BOOKS — the working area: a top table (checked books + manual
 *     entries, or the editable WHL catalog), a left panel (Open Library
 *     search / manual entry / WHL record editor), and a tabbed bottom pane
 *     of live-filtered source tables (Open Library / CH catalog / WHL).
 *   UPLOAD LIST — approved scan sources plus the book builder: catalog
 *     entries being prepared for submission to World Herb Library.
 *
 * All mutating actions are undoable (see the history section).
 */

const CHPANE_RENDER = 300;
const LS_KEY = "whl_cad_checked_v1";
const SETTINGS_KEY = "whl_cad_settings_v1";

const MANUAL_FIELDS = [
  "title", "author", "publisher", "city", "year", "edition", "volume",
  "language", "pages", "condition", "price", "illustrations", "categories",
  "notes",
];

// Metadata columns of the combined table, in cell order; click-to-edit.
const BOOK_COLS = [
  "title", "author", "year", "edition", "volume", "publisher", "city",
  "language", "pages", "condition", "illustrations", "price", "acquired",
  "categories", "notes",
];

const CHECKED_COLS = [
  ["src", "SRC"], ["title", "TITLE"], ["author", "AUTHOR"], ["year", "YEAR"],
  ["edition", "EDITION"], ["volume", "VOLUME"], ["publisher", "PUBLISHER"],
  ["city", "CITY"], ["language", "LANGUAGE"], ["pages", "PAGES"],
  ["condition", "CONDITION"], ["illustrations", "ILLUSTRATIONS"],
  ["price", "PRICE"], ["acquired", "ACQUIRED"], ["categories", "CATEGORIES"],
  ["notes", "NOTES"], ["copyright", "COPYRIGHT"], ["whl", "WHL"],
  ["ia", "IA"], ["ht", "HT"], ["mark", "MARK"], ["action", "ACTION"],
];

const WHL_ROW_FIELDS = ["title", "subtitle", "authors", "year", "publisher",
  "pages", "language", "subject", "categories", "description"];

const BUILD_FIELDS = ["title", "subtitle", "authors", "year", "publisher",
  "publisher_city", "edition", "language", "pages", "categories",
  "pdf_source", "source_url", "notes"];

const state = {
  // key `${source}:${idx}` -> { book, checks, scans, verify, manual_urls }
  checked: new Map(),
  manual: [],
  rowsById: new Map(),        // combined-table row lookup (per render)
  checkedFilter: "",
  chBooks: null,              // CH catalogue rows (lazy)
  whlRows: null,              // WHL catalogue + scrape + corrections (lazy)
  whlSelected: null,          // search-mode repopulation target (row idx)
  whlEditIdx: null,           // row loaded in the WHL EDIT tab
  olOverride: null,           // search-mode query override
  olRows: null,               // realtime Open Library results
  olNote: "",
  bottomRecords: [],
  builds: {},                 // book builder entries (id -> build)
  buildSel: null,             // selected build id
  downloads: new Map(),
  dlTimers: new Map(),
  downloadedIds: new Set(),
  prov: {},                   // manual-form field provenance
  msrcTarget: null,
  mdTarget: null,             // markdown overlay target textarea
  settings: {
    checkedCols: {}, showCatalog: false, markFilter: "ALL",
    topTable: "checked", bottomTabs: ["ol", "ch"], bottomActive: 0,
    whlMode: "edit", paneWidth: null, theme: "", font: "",
    whlCons: { title: false, authors: false, year: true },
  },
};

// --- appearance: themes are full chrome redesigns; fonts are user-selectable --

const THEMES = [
  ["", "CLASSIC CAD"],
  ["ledger", "ARCHIVE LEDGER"],
  ["workstation", "WORKSTATION 2000"],
  ["slate", "SLATE STUDIO"],
  ["platinum", "PLATINUM"],
  ["blueprint", "BLUEPRINT"],
  ["mainframe", "MAINFRAME TERMINAL"],
];
const LEGACY_THEMES = { cde: "ledger", xp2003: "workstation", acad: "slate" };

const FONTS = [
  ["", "CONSOLAS (DEFAULT)"],
  ['"Courier New", Courier, monospace', "COURIER NEW"],
  ['"Lucida Console", Monaco, monospace', "LUCIDA CONSOLE"],
  ['"Cascadia Mono", Consolas, monospace', "CASCADIA MONO"],
  ['"IBM Plex Mono", Consolas, monospace', "IBM PLEX MONO"],
  ['Tahoma, "Segoe UI", sans-serif', "TAHOMA"],
  ['Verdana, Geneva, sans-serif', "VERDANA"],
  ['Georgia, "Times New Roman", serif', "GEORGIA"],
];

function applyTheme() {
  let t = state.settings.theme || "";
  if (LEGACY_THEMES[t]) {
    t = LEGACY_THEMES[t];
    state.settings.theme = t;
    saveSettings();
  }
  if (t) document.body.dataset.theme = t;
  else delete document.body.dataset.theme;
}

function applyFont() {
  const f = state.settings.font || "";
  if (f) document.body.style.setProperty("--mono", f);
  else document.body.style.removeProperty("--mono");
}

const el = (id) => document.getElementById(id);
const esc = (s) =>
  String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function status(msg) { el("status-msg").textContent = msg; }
function ckey(source, idx) { return `${source}:${idx}`; }

// --- undo / redo -------------------------------------------------------------
// Every mutating action pushes an operation with its inverse. Client-side
// state (the checked map) is snapshot-restored; server-backed changes run
// their inverse call.

const history = { stack: [], ptr: 0 };

function pushOp(label, undoFn, redoFn) {
  history.stack.length = history.ptr;
  history.stack.push({ label, undoFn, redoFn });
  if (history.stack.length > 100) history.stack.shift();
  history.ptr = history.stack.length;
  updateHistoryButtons();
}

function updateHistoryButtons() {
  const u = el("undo-btn"), r = el("redo-btn");
  u.disabled = history.ptr === 0;
  r.disabled = history.ptr >= history.stack.length;
  u.dataset.tip = history.ptr
    ? `Undo (Ctrl+Z): ${history.stack[history.ptr - 1].label}` : "Undo (Ctrl+Z)";
  r.dataset.tip = history.ptr < history.stack.length
    ? `Redo (Ctrl+Y): ${history.stack[history.ptr].label}` : "Redo (Ctrl+Y)";
}

let historyBusy = false;
async function undo() {
  if (historyBusy || !history.ptr) { if (!history.ptr) status("NOTHING TO UNDO"); return; }
  historyBusy = true;
  const op = history.stack[--history.ptr];
  try { await op.undoFn(); status(`UNDO :: ${op.label}`); }
  catch (e) { status(`UNDO FAILED :: ${op.label}`); }
  historyBusy = false;
  updateHistoryButtons();
}

async function redo() {
  if (historyBusy || history.ptr >= history.stack.length) {
    if (history.ptr >= history.stack.length) status("NOTHING TO REDO");
    return;
  }
  historyBusy = true;
  const op = history.stack[history.ptr++];
  try { await op.redoFn(); status(`REDO :: ${op.label}`); }
  catch (e) { status(`REDO FAILED :: ${op.label}`); }
  historyBusy = false;
  updateHistoryButtons();
}

function snapshotChecked(key) {
  const v = state.checked.get(key);
  return v ? JSON.parse(JSON.stringify(v)) : null;
}

function restoreChecked(key, snap) {
  if (snap) state.checked.set(key, JSON.parse(JSON.stringify(snap)));
  else state.checked.delete(key);
  saveChecked();
  renderChecked();
  updateCheckedCount();
}

function trackChecked(label, key, mutate) {
  const before = snapshotChecked(key);
  mutate();
  const after = snapshotChecked(key);
  pushOp(label,
    () => restoreChecked(key, before),
    () => restoreChecked(key, after));
}

// --- persistence -----------------------------------------------------------

function saveChecked() {
  const arr = [...state.checked.entries()].map(([k, v]) => [k, v]);
  try { localStorage.setItem(LS_KEY, JSON.stringify(arr)); } catch (e) {}
}
function loadChecked() {
  try {
    const arr = JSON.parse(localStorage.getItem(LS_KEY) || "[]");
    state.checked = new Map(arr);
  } catch (e) { state.checked = new Map(); }
}

function saveSettings() {
  try { localStorage.setItem(SETTINGS_KEY, JSON.stringify(state.settings)); } catch (e) {}
}
function loadSettings() {
  try {
    const s = JSON.parse(localStorage.getItem(SETTINGS_KEY) || "{}");
    state.settings = Object.assign(state.settings, s);
  } catch (e) { /* keep defaults */ }
  state.settings.checkedCols = state.settings.checkedCols || {};
  state.settings.whlCons = state.settings.whlCons ||
    { title: false, authors: false, year: true };
}

// --- tabs + header -----------------------------------------------------------

const TAB_TITLES = { checked: "CHECKED BOOKS", upload: "UPLOAD LIST" };

function setHeader(tabId) {
  const name = `${TAB_TITLES[tabId] || ""} :: CATALOG EXPLORER`;
  el("tb-name").textContent = name;
  document.title = name;
}

function initTabs() {
  for (const tab of document.querySelectorAll("#tabs .tab")) {
    tab.addEventListener("click", () => {
      document.querySelectorAll("#tabs .tab").forEach((t) => t.classList.remove("active"));
      document.querySelectorAll(".panel-view").forEach((p) => p.classList.remove("active"));
      tab.classList.add("active");
      el(tab.dataset.tab).classList.add("active");
      setHeader(tab.dataset.tab);
      if (tab.dataset.tab === "checked") renderChecked();
      if (tab.dataset.tab === "upload") renderUpload();
    });
  }
  setHeader("checked");
}

// --- tooltip (match info + overflowed cells) ---------------------------------

function showTip(text, x, y) {
  const tip = el("cad-tooltip");
  if (!text) { tip.hidden = true; return; }
  tip.textContent = text;
  tip.hidden = false;
  const pad = 14;
  const r = tip.getBoundingClientRect();
  let left = x + pad, top = y + pad;
  if (left + r.width > innerWidth - 8) left = Math.max(8, innerWidth - r.width - 8);
  if (top + r.height > innerHeight - 8) top = Math.max(8, y - r.height - pad);
  tip.style.left = left + "px";
  tip.style.top = top + "px";
}
function hideTip() { el("cad-tooltip").hidden = true; }

function initTooltips() {
  document.addEventListener("mouseover", (ev) => {
    const tagged = ev.target.closest("[data-tip]");
    if (tagged) { showTip(tagged.dataset.tip, ev.clientX, ev.clientY); return; }
    const td = ev.target.closest("td, th");
    if (td && !td.querySelector("input") && td.scrollWidth > td.clientWidth + 1) {
      showTip(td.textContent.trim(), ev.clientX, ev.clientY);
      return;
    }
    hideTip();
  });
  document.addEventListener("scroll", hideTip, true);
  document.addEventListener("mouseleave", hideTip);
}

// --- settings window -----------------------------------------------------------

function applyColumnVisibility(tableId, colSettings) {
  const table = el(tableId);
  if (!table) return;
  const ths = [...table.querySelectorAll("thead th")];
  const hide = ths.map((th) => colSettings[th.dataset.col] === false);
  ths.forEach((th, i) => { th.style.display = hide[i] ? "none" : ""; });
  for (const tr of table.querySelectorAll("tbody tr")) {
    [...tr.children].forEach((td, i) => { td.style.display = hide[i] ? "none" : ""; });
  }
}

function renderSettings() {
  const themeSel = el("theme-select");
  themeSel.innerHTML = "";
  for (const [id, label] of THEMES) {
    const o = document.createElement("option");
    o.value = id;
    o.textContent = label;
    themeSel.appendChild(o);
  }
  themeSel.value = LEGACY_THEMES[state.settings.theme] || state.settings.theme || "";
  themeSel.onchange = () => {
    state.settings.theme = themeSel.value;
    saveSettings();
    applyTheme();
  };

  const fontSel = el("font-select");
  fontSel.innerHTML = "";
  for (const [val, label] of FONTS) {
    const o = document.createElement("option");
    o.value = val;
    o.textContent = label;
    fontSel.appendChild(o);
  }
  fontSel.value = state.settings.font || "";
  fontSel.onchange = () => {
    state.settings.font = fontSel.value;
    saveSettings();
    applyFont();
  };

  const wrap = el("cols-checked");
  wrap.innerHTML = "";
  for (const [key, label] of CHECKED_COLS) {
    const lab = document.createElement("label");
    lab.className = "settings-col";
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = state.settings.checkedCols[key] !== false;
    cb.addEventListener("change", () => {
      if (cb.checked) delete state.settings.checkedCols[key];
      else state.settings.checkedCols[key] = false;
      saveSettings();
      applyColumnVisibility("checked-table", state.settings.checkedCols);
    });
    lab.appendChild(cb);
    lab.appendChild(document.createTextNode(" " + label));
    wrap.appendChild(lab);
  }
}

function openSettings() { renderSettings(); el("settings-overlay").hidden = false; }
function closeSettings() { el("settings-overlay").hidden = true; }

// --- FIND syntax ---------------------------------------------------------------
// @token constrains by author (last name), #token by publication year,
// everything else is title text.

function parseFind(text) {
  const out = { title: [], author: [], year: "" };
  for (const tok of (text || "").trim().split(/\s+/)) {
    if (!tok) continue;
    if (tok.startsWith("@") && tok.length > 1) out.author.push(tok.slice(1));
    else if (tok.startsWith("#") && tok.length > 1) {
      const y = tok.slice(1).replace(/\D/g, "");
      if (y) out.year = y;
    } else out.title.push(tok);
  }
  return {
    title: out.title.join(" "),
    author: out.author.join(" "),
    year: out.year,
    empty: !out.title.length && !out.author.length && !out.year,
  };
}

function findQuery() { return parseFind(state.checkedFilter); }

function matchesFind(q, title, author, year) {
  if (q.empty) return true;
  if (q.title) {
    const hay = (title || "").toLowerCase();
    for (const w of q.title.toLowerCase().split(/\s+/)) {
      if (w && !hay.includes(w)) return false;
    }
  }
  if (q.author) {
    const hay = (author || "").toLowerCase();
    for (const w of q.author.toLowerCase().split(/\s+/)) {
      if (w && !hay.includes(w)) return false;
    }
  }
  if (q.year && !(String(year || "").includes(q.year))) return false;
  return true;
}

// --- badges ---------------------------------------------------------------
// Uniform width; abbreviated labels; the tag itself links to the matched
// record, and the tooltip carries the full match details.

function badge(cls, label, opts = {}) {
  const tip = opts.tip ? ` data-tip="${esc(opts.tip)}"` : "";
  const attrs = opts.attrs || "";
  if (opts.href)
    return `<a class="badge ${cls}" href="${esc(opts.href)}" target="_blank" rel="noopener"${tip}${attrs}>${esc(label)}</a>`;
  return `<span class="badge ${cls}"${tip}${attrs}>${esc(label)}</span>`;
}

function tipForLocalWhl(checks) {
  if (!checks) return "";
  const m = checks.whl_match;
  if (!m) return "WHL CATALOG: " + (checks.in_whl || "not checked");
  const lines = ["WHL CATALOG MATCH: " + m.title];
  if (m.author) lines.push("AUTHOR: " + m.author);
  if (m.year) lines.push("YEAR: " + m.year);
  lines.push("STATUS: " + (m.status || "?"));
  return lines.join("\n");
}

function whlBadge(row) {
  const rejected = getVerify(row, "whl") === "rejected";
  const murl = getManualUrl(row, "whl");
  const wrap = (tagHtml) => verifyUnit(row, "whl", tagHtml);
  const c = row.checks;
  if (!c || c.error) return badge("unknown", "---", { tip: "Not checked yet" });
  const tip = tipForLocalWhl(c);
  const m = c.whl_match || {};
  switch (c.in_whl) {
    case "yes":
    case "draft": {
      if (rejected) {
        if (murl)
          return wrap(badge("available", "YES", {
            tip: "MANUALLY LOCATED SOURCE:\n" + murl +
              "\n(automatic match was rejected as a false positive)",
            href: murl,
          }));
        return wrap(badge("missing", "NO", {
          tip: "REJECTED AS FALSE POSITIVE.\n" + tip +
            "\nCLICK TAG: paste the URL of a manually located source",
          href: m.permalink || "",
        }));
      }
      return wrap(badge(c.in_whl === "yes" ? "available" : "missing",
        c.in_whl === "yes" ? "YES" : "DRFT", { tip, href: m.permalink || "" }));
    }
    case "no": return badge("missing", "NO", { tip });
    default: return badge("unknown", "?", { tip: "whl_catalog.csv not found" });
  }
}

function copyrightBadge(checks) {
  // The column asks "is it under copyright?": NO = public domain (green),
  // YES = in copyright (red).
  if (!checks) return badge("unknown", "---", { tip: "Not checked yet" });
  if (checks.error) return badge("error", "ERR", { tip: checks.error });
  const s = checks.copyright_status || "";
  if (s.startsWith("Public domain")) return badge("available", "NO", { tip: s });
  if (s.startsWith("In copyright")) return badge("error", "YES", { tip: s });
  return badge("unknown", "?", { tip: s });
}

function tipForScan(s, isHt) {
  if (!s) return "Not checked";
  if (s.error) return "ERROR: " + s.error;
  const b = s.best_match;
  if (!b) return s.note || (s.available === false ? "No match found" : "Could not determine");
  const lines = [(s.available === false ? "NO CONFIDENT MATCH — CLOSEST: " : "MATCH: ") + b.title];
  if (b.author) lines.push("AUTHOR: " + b.author);
  if (b.year) lines.push("YEAR: " + b.year);
  if (b.accuracy != null) lines.push("ACCURACY: " + b.accuracy);
  if (isHt && b.items && b.items.length)
    lines.push("ITEMS: " + b.items.map((i) => `${i.volume || "copy"} [${i.rights}]`).join(", "));
  return lines.join("\n");
}

// --- per-source match verification ---------------------------------------------
// Positive matches carry a marker fused into the tag's right edge: yellow =
// pending, green = approved, red = rejected as a false positive. Clicking
// the MARKER cycles the state; a rejected tag opens the paste-URL box.

function getVerify(row, source) {
  return (row.verify || {})[source] || "pending";
}

function getManualUrl(row, source) {
  return (row.manualUrls || {})[source] || "";
}

const VERIFY_TIPS = {
  pending: "PENDING — CLICK MARKER TO APPROVE",
  approved: "APPROVED — CLICK MARKER TO REJECT (false positive)",
  rejected: "REJECTED (FALSE POSITIVE) — CLICK MARKER TO RESET.\nClick the tag to paste a manually located source.",
};

function verifyUnit(row, source, tagHtml) {
  const st = getVerify(row, source);
  const manual = st === "rejected" && getManualUrl(row, source);
  const cls = manual ? "approved" : st;
  const tip = manual
    ? "MANUALLY LOCATED SOURCE — CLICK MARKER TO RESET"
    : VERIFY_TIPS[st];
  return `<span class="tag-unit" data-vsrc="${source}">${tagHtml}` +
    `<span class="vmark ${cls}" data-tip="${esc(tip)}"></span></span>`;
}

function scanBadge(row, source) {
  const scans = row.scans;
  if (!scans || !scans[source]) return badge("unknown", "---", { tip: "Not scanned yet" });
  const s = scans[source];
  const isHt = source === "hathitrust";
  const tip = tipForScan(s, isHt);
  if (s.error) return badge("error", "ERR", { tip });
  if (s.available === true) {
    const best = s.best_match || {};
    const href = best.url || best.record_url || "";
    if (getVerify(row, source) === "rejected") {
      const murl = getManualUrl(row, source);
      if (murl) {
        return verifyUnit(row, source, badge("available", "YES", {
          tip: "MANUALLY LOCATED SOURCE:\n" + murl +
            "\n(automatic match was rejected as a false positive)",
          href: murl,
        }));
      }
      return verifyUnit(row, source, badge("missing", "NO", {
        tip: "REJECTED AS FALSE POSITIVE.\n" + tip +
          "\nCLICK TAG: paste the URL of a manually located source",
        href,
      }));
    }
    return verifyUnit(row, source,
      badge("available", isHt && s.full_view ? "VIEW" : "YES", { tip, href }));
  }
  if (s.available === false) return badge("missing", "NO", { tip });
  return badge("unknown", "?", { tip, href: s.search_url || "" });
}

// --- SCAN / UPLOAD marks -------------------------------------------------------

function effScan(row, source) {
  const s = row.scans && row.scans[source];
  if (!s) return null;
  if (s.available === true && getVerify(row, source) === "rejected")
    return getManualUrl(row, source) ? true : false;
  return s.available;
}

function computeMark(row) {
  const c = row.checks;
  const localWhl = c && !c.error ? c.in_whl : null;
  const whlMatched = localWhl === "yes" || localWhl === "draft";
  const whlRejected = whlMatched && getVerify(row, "whl") === "rejected" &&
    !getManualUrl(row, "whl");
  if (whlMatched && !whlRejected)
    return { mark: null, reason: "Already in WHL — nothing to do" };
  const whlAbsent = whlRejected || localWhl === "no";
  if (!whlAbsent)
    return { mark: null, reason: "WHL status unknown — scan pending" };
  if (!row.scans)
    return { mark: null, reason: "Not in WHL — scan pending" };
  const ia = effScan(row, "internet_archive"), ht = effScan(row, "hathitrust");
  if (ia === true || ht === true)
    return {
      mark: "UPLOAD",
      reason: "Not in WHL; a scan exists in an online archive.\nVerify each found source (click its marker); approved sources land in the UPLOAD LIST tab.",
    };
  const pd = c && (c.copyright_status || "").startsWith("Public domain");
  if (!pd)
    return { mark: null, reason: "Not public domain: " + ((c && c.copyright_status) || "copyright unknown") };
  if (ia === false && ht !== true)
    return {
      mark: "SCAN",
      reason: "Not in WHL; public domain; no scan found online (or only false positives).\nThis book should be scanned.",
    };
  return { mark: null, reason: "Online-archive status inconclusive — rerun scans" };
}

function anyApprovedSource(row) {
  return ["internet_archive", "hathitrust"].some((src) =>
    (getVerify(row, src) === "approved" &&
      row.scans && row.scans[src] && row.scans[src].available === true) ||
    (getVerify(row, src) === "rejected" && getManualUrl(row, src)));
}

function rowMarkState(row) {
  const m = computeMark(row).mark;
  if (m === "UPLOAD") return anyApprovedSource(row) ? "APPROVED" : "UPLOAD";
  return m || "NONE";
}

function markCell(row) {
  const { mark, reason } = computeMark(row);
  if (mark === "SCAN") return badge("scan", "SCAN", { tip: reason });
  if (mark === "UPLOAD") {
    if (anyApprovedSource(row))
      return badge("approved", "UPLD", { tip: "Approved source(s) ready — see the UPLOAD LIST tab" });
    return badge("upload", "UPLD", { tip: reason });
  }
  return badge("unknown", "—", { tip: reason });
}

// --- combined checked-books + manual-entries table -----------------------------

function manualToBook(e) {
  return {
    title: e.title || "", subtitle: "", author: e.author || "",
    year: e.year || "", edition: e.edition || "", volume: e.volume || "",
    publisher: e.publisher || "", city: e.city || "", language: e.language || "",
    pages: e.pages || "", condition: e.condition || "",
    illustrations: e.illustrations || "", price: e.price || "",
    acquired: "", categories: e.categories || "", notes: e.notes || "",
  };
}

function migrateVerify(v) {
  if (v.verify) return v.verify;
  if (!v.approved || !v.scans) return null;
  const ia = v.scans.internet_archive, ht = v.scans.hathitrust;
  if (ia && ia.available === true) return { internet_archive: "approved" };
  if (ht && ht.available === true) return { hathitrust: "approved" };
  return null;
}

function combinedRows() {
  const rows = [];
  for (const e of state.manual) {
    rows.push({
      kind: "manual", id: e.id, source: "manual", book: manualToBook(e),
      checks: e.checks || null, scans: e.scans || null,
      verify: migrateVerify(e) || {},
      manualUrls: e.manual_urls || {},
    });
  }
  for (const [k, v] of state.checked.entries()) {
    rows.push({
      kind: "catalog", id: k, source: k.split(":")[0],
      book: Object.assign({ volume: "", language: "" }, v.book),
      checks: v.checks || null, scans: v.scans || null,
      verify: migrateVerify(v) || {},
      manualUrls: v.manual_urls || {},
    });
  }
  return rows;
}

function rowById(id) {
  const m = state.manual.find((x) => x.id === id);
  if (m) return { kind: "manual", id, book: manualToBook(m) };
  const e = state.checked.get(id);
  if (e) return { kind: "catalog", id, book: e.book };
  return null;
}

function iaIdentifier(scans) {
  const s = scans && scans.internet_archive;
  if (!s || s.available !== true || !s.best_match) return "";
  return s.best_match.identifier ||
    ((s.best_match.url || "").split("/details/")[1] || "");
}

function iaIdentifierForRow(row) {
  if (getVerify(row, "internet_archive") === "rejected") {
    const murl = getManualUrl(row, "internet_archive");
    if (murl && murl.includes("/details/"))
      return murl.split("/details/")[1].split(/[/?#]/)[0];
    return "";
  }
  return iaIdentifier(row.scans);
}

function dlPct(dl) {
  if (!dl || !dl.total) return "...";
  return Math.round((dl.bytes / dl.total) * 100) + "%";
}

function iaCell(row) {
  let html = scanBadge(row, "internet_archive");
  const ident = iaIdentifierForRow(row);
  if (ident) {
    const dl = state.downloads.get(ident);
    if ((dl && dl.status === "done") || state.downloadedIds.has(ident)) {
      html += ` <span class="dl-done" data-tip="PDF saved under downloads/ia/ with a catalog entry">SAVED</span>`;
    } else if (dl && dl.status === "downloading") {
      html += ` <span class="dl-prog">${dlPct(dl)}</span>`;
    } else if (dl && dl.status === "error") {
      html += ` <span class="dl-err" data-tip="${esc(dl.error || "download failed")}">DL ERR</span>`;
    }
  }
  return html;
}

const TIP_FIELDS = [
  ["title", "TITLE"], ["subtitle", "SUBTITLE"], ["author", "AUTHOR"],
  ["publisher", "PUBLISHER"], ["city", "CITY"], ["year", "YEAR"],
  ["edition", "EDITION"], ["volume", "VOLUME"], ["language", "LANGUAGE"],
  ["pages", "PAGES"], ["subject", "SUBJECT"], ["condition", "CONDITION"],
  ["price", "PRICE"], ["illustrations", "ILLUSTRATIONS"],
  ["categories", "CATEGORIES"], ["notes", "NOTES"], ["status", "STATUS"],
  ["acquired", "ACQUIRED"], ["url", "URL"],
];

function recordTip(rec, header) {
  const lines = header ? [header] : [];
  for (const [k, label] of TIP_FIELDS) {
    const v = (rec[k] || "").toString().trim();
    if (v) lines.push(`${label}: ${v}`);
  }
  lines.push("CLICK ROW: add to the top-pane table");
  return lines.join("\n");
}

function updateCheckedCount() {
  el("checked-count").textContent =
    `${state.checked.size} CHECKED / ${state.manual.length} MANUAL`;
}

function renderChecked() {
  // Background re-renders must not destroy an in-progress cell edit.
  const active = document.activeElement;
  if (active && active.classList && active.classList.contains("cell-edit")) return;
  updateCheckedCount();
  const tbody = el("checked-rows");
  tbody.innerHTML = "";
  let rows = combinedRows();
  state.rowsById = new Map(rows.map((r) => [String(r.id), r]));

  const q = findQuery();
  if (!q.empty)
    rows = rows.filter((r) => matchesFind(
      q, `${r.book.title} ${r.book.subtitle || ""}`, r.book.author, r.book.year));
  const mf = state.settings.markFilter || "ALL";
  if (mf !== "ALL") rows = rows.filter((r) => rowMarkState(r) === mf);

  el("checked-empty").hidden = rows.length !== 0;

  for (const row of rows) {
    const b = row.book;
    const tr = document.createElement("tr");
    tr.dataset.rowId = row.id;
    if (row.kind === "manual") tr.classList.add("is-manual");
    const editable = (f) =>
      row.kind === "manual" && f === "acquired" ? "" : ` class="editable" data-edit="${f}"`;
    const cell = (f) => `<td${editable(f)}>${esc(b[f])}</td>`;
    tr.innerHTML = `
      <td>${row.kind === "manual" ? "MANUAL" : esc(row.source.toUpperCase())}</td>
      ${BOOK_COLS.map(cell).join("\n      ")}
      <td class="col-whl">${copyrightBadge(row.checks)}</td>
      <td class="col-whl">${whlBadge(row)}</td>
      <td class="col-whl">${iaCell(row)}</td>
      <td class="col-whl">${scanBadge(row, "hathitrust")}</td>
      <td class="col-whl">${markCell(row)}</td>
      <td class="col-act">
        ${row.kind === "manual"
          ? `<button class="cad-btn tiny danger" data-mdel="${esc(row.id)}">DEL</button>`
          : `<button class="cad-btn tiny" data-unchk="${esc(row.id)}" data-tip="Remove from checked books">UNCHK</button>`}
      </td>`;
    tbody.appendChild(tr);
  }

  applyColumnVisibility("checked-table", state.settings.checkedCols);
  renderBottomPane();
}

// One delegated handler covers verify markers / delete / uncheck / edit clicks.
function onCheckedClick(ev) {
  const mark = ev.target.closest(".vmark");
  if (mark) {
    const unit = mark.closest("[data-vsrc]");
    const tr = mark.closest("tr");
    if (unit && tr) cycleVerify(tr.dataset.rowId, unit.dataset.vsrc);
    return;
  }
  const tag = ev.target.closest(".tag-unit a.badge");
  if (tag) {
    const unit = tag.closest("[data-vsrc]");
    const tr = tag.closest("tr");
    const row = tr && state.rowsById.get(String(tr.dataset.rowId));
    if (row && unit && getVerify(row, unit.dataset.vsrc) === "rejected" &&
        !getManualUrl(row, unit.dataset.vsrc)) {
      ev.preventDefault();
      openManualSource(tr.dataset.rowId, unit.dataset.vsrc);
    }
    return;
  }
  const t = ev.target.closest("[data-mdel],[data-unchk]");
  if (t) {
    if (t.dataset.mdel !== undefined) deleteManual(t.dataset.mdel);
    else if (t.dataset.unchk !== undefined) uncheckRow(t.dataset.unchk);
    return;
  }
  const td = ev.target.closest("td[data-edit]");
  if (td) startEdit(td);
}

function uncheckRow(key) {
  const title = ((state.checked.get(key) || {}).book || {}).title || key;
  trackChecked(`uncheck ${String(title).slice(0, 40)}`, key, () => {
    state.checked.delete(key);
  });
  saveChecked();
  renderChecked();
  updateCheckedCount();
  status("REMOVED FROM CHECKED BOOKS");
}

// --- click-to-edit cells --------------------------------------------------------

function startEdit(td) {
  if (td.querySelector("input")) return;
  const tr = td.closest("tr");
  const row = state.rowsById.get(String(tr.dataset.rowId));
  if (!row) return;
  const field = td.dataset.edit;
  const original = String(row.book[field] || "");
  hideTip();
  td.classList.add("editing");
  td.innerHTML = `<input class="cell-edit" value="${esc(original)}" />`;
  const input = td.querySelector("input");
  input.focus();
  input.select();
  let done = false;
  const finish = (commit) => {
    if (done) return;
    done = true;
    input.blur();
    const val = input.value.trim();
    if (commit && val !== original.trim()) commitEdit(row, field, val);
    else renderChecked();
  };
  input.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") { ev.preventDefault(); finish(true); }
    else if (ev.key === "Escape") { ev.stopPropagation(); finish(false); }
  });
  input.addEventListener("blur", () => finish(true));
}

async function patchManualField(id, field, value) {
  const res = await fetch(`/api/manual/${encodeURIComponent(id)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ [field]: value }),
  });
  const data = await res.json().catch(() => ({}));
  if (res.ok && data.ok) {
    const i = state.manual.findIndex((x) => x.id === id);
    if (i >= 0) state.manual[i] = data.entry;
    renderChecked();
    queueScan(id);
    return true;
  }
  return false;
}

async function commitEdit(row, field, value) {
  if (row.kind === "manual") {
    const oldValue = String(row.book[field] || "");
    if (await patchManualField(row.id, field, value)) {
      pushOp(`edit ${field} of ${row.book.title.slice(0, 32)}`,
        () => patchManualField(row.id, field, oldValue),
        () => patchManualField(row.id, field, value));
      status(`UPDATED ${field.toUpperCase()} :: RESCANNING`);
    } else {
      status("UPDATE FAILED");
    }
  } else {
    const entry = state.checked.get(row.id);
    if (!entry) return;
    trackChecked(`edit ${field} of ${row.book.title.slice(0, 32)}`, row.id, () => {
      entry.book = Object.assign({}, entry.book, { [field]: value });
      entry.checks = null;
      entry.scans = null;
      entry.verify = null;
      queueScan(row.id);
    });
    saveChecked();
    status(`UPDATED ${field.toUpperCase()} :: RESCANNING`);
  }
  renderChecked();
}

// --- generalized top / bottom panes ----------------------------------------------

async function loadChBooks() {
  if (state.chBooks) return;
  try {
    const res = await fetch("/api/books");
    state.chBooks = res.ok ? (await res.json()).books || [] : [];
  } catch (e) { state.chBooks = []; }
}

async function loadWhlRows(force) {
  if (state.whlRows && !force) return;
  try {
    const res = await fetch("/api/whl_catalog");
    state.whlRows = res.ok ? (await res.json()).rows || [] : [];
  } catch (e) { state.whlRows = []; }
}

function chToRecord(b) {
  return {
    _src: "ch", _idx: b.idx,
    title: b.title, subtitle: b.subtitle || "", author: b.author,
    publisher: b.publisher, city: b.city, year: b.year, edition: b.edition,
    volume: "", language: "", pages: b.pages, condition: b.condition,
    price: b.price, illustrations: b.illustrations, categories: b.categories,
    notes: b.notes, acquired: b.acquired, url: "",
  };
}

function whlToRecord(r) {
  return {
    _src: "whl", _idx: r.idx,
    title: r.title, subtitle: r.subtitle || "", author: r.authors,
    publisher: r.publisher || "", city: "",
    year: r.year, edition: "", volume: "", language: r.language || "",
    pages: r.pages || "", subject: r.subject || "",
    categories: r.categories || "", notes: r.description || "",
    status: r.status, url: r.permalink || "",
  };
}

function olToRecord(r) {
  return {
    _src: "ol", _idx: r.key,
    title: r.title, subtitle: r.subtitle || "",
    author: (r.authors || []).join("; "),
    publisher: r.publisher || "", city: r.city || "",
    year: r.year || (r.first_year ? String(r.first_year) : ""),
    edition: r.edition || "", volume: r.volume || "",
    language: r.language || "", pages: r.pages || "",
    categories: "", notes: "", url: r.url || "",
  };
}

const BOTTOM_TABLES = {
  ol: {
    label: "OPEN LIBRARY",
    cols: ["TITLE", "AUTHOR", "YEAR", "PUBLISHER", "CITY", "ED", "VOL", "LANG"],
    cells: (r) => [linkCell(r.title, r.url), esc(r.author), esc(r.year),
                   esc(r.publisher), esc(r.city), esc(r.edition),
                   esc(r.volume), esc(r.language)],
  },
  ch: {
    label: "CH CATALOG",
    cols: ["TITLE", "AUTHOR", "YEAR", "PUBLISHER", "CITY", "CATEGORIES"],
    cells: (r) => [esc(r.title), esc(r.author), esc(r.year), esc(r.publisher),
                   esc(r.city), esc(r.categories)],
  },
  whl: {
    label: "WHL CATALOG",
    cols: ["TITLE", "AUTHORS", "YEAR", "STATUS"],
    cells: (r) => [linkCell(r.title, r.url), esc(r.author), esc(r.year),
                   esc(r.status)],
  },
};

function linkCell(text, url) {
  const t = esc(text) || "<em>(untitled)</em>";
  return url
    ? `<a href="${esc(url)}" target="_blank" rel="noopener">${t}</a>`
    : t;
}

function bottomTabs() {
  let tabs = state.settings.bottomTabs;
  if (!Array.isArray(tabs) || !tabs.length) tabs = ["ol", "ch"];
  state.settings.bottomTabs = tabs.filter((t) => BOTTOM_TABLES[t]);
  if (!state.settings.bottomTabs.length) state.settings.bottomTabs = ["ol"];
  if (state.settings.bottomActive == null ||
      state.settings.bottomActive >= state.settings.bottomTabs.length) {
    state.settings.bottomActive = 0;
  }
  return state.settings.bottomTabs;
}

function renderBottomTabs() {
  const tabs = bottomTabs();
  const wrap = el("bottom-tabs");
  wrap.innerHTML = "";
  tabs.forEach((t, i) => {
    if (i === state.settings.bottomActive) {
      const sel = document.createElement("select");
      sel.className = "cad-input bottom-tabsel";
      for (const [id, def] of Object.entries(BOTTOM_TABLES)) {
        const o = document.createElement("option");
        o.value = id;
        o.textContent = def.label;
        sel.appendChild(o);
      }
      sel.value = t;
      sel.addEventListener("change", () => {
        state.settings.bottomTabs[i] = sel.value;
        saveSettings();
        renderBottomPane();
      });
      wrap.appendChild(sel);
      if (tabs.length > 1) {
        const x = document.createElement("button");
        x.className = "cad-btn tiny";
        x.textContent = "✕";
        x.dataset.tip = "Close this tab";
        x.addEventListener("click", () => {
          state.settings.bottomTabs.splice(i, 1);
          state.settings.bottomActive = Math.max(0, i - 1);
          saveSettings();
          renderBottomPane();
        });
        wrap.appendChild(x);
      }
    } else {
      const b = document.createElement("button");
      b.className = "cad-btn tiny bottom-tabbtn";
      b.textContent = BOTTOM_TABLES[t].label;
      b.addEventListener("click", () => {
        state.settings.bottomActive = i;
        saveSettings();
        renderBottomPane();
      });
      wrap.appendChild(b);
    }
  });
}

function activeBottomTable() {
  return bottomTabs()[state.settings.bottomActive];
}

async function renderBottomPane() {
  const pane = el("bottom-pane");
  pane.hidden = !state.settings.showCatalog;
  if (pane.hidden) return;
  renderBottomTabs();
  const t = activeBottomTable();
  if (t === "ch") await loadChBooks();
  if (t === "whl") await loadWhlRows();
  if (t === "ol" && state.olRows === null) { olRealtime(); }
  renderBottomRows();
}

function renderBottomRows() {
  const t = activeBottomTable();
  const def = BOTTOM_TABLES[t];
  el("bottom-head").innerHTML =
    "<tr>" + def.cols.map((c) => `<th>${c}</th>`).join("") + "</tr>";
  const tbody = el("bottom-rows");
  tbody.innerHTML = "";

  const q = findQuery();
  let records;
  if (t === "ol") {
    records = (state.olRows || []).map(olToRecord);
  } else if (t === "ch") {
    records = (state.chBooks || [])
      .filter((b) => matchesFind(q, `${b.title} ${b.subtitle || ""}`, b.author, b.year))
      .slice(0, CHPANE_RENDER).map(chToRecord);
  } else {
    records = (state.whlRows || [])
      .filter((r) => matchesFind(q, `${r.title} ${r.subtitle || ""}`, r.authors, r.year))
      .slice(0, CHPANE_RENDER).map(whlToRecord);
  }

  state.bottomRecords = records;
  records.forEach((rec, i) => {
    const tr = document.createElement("tr");
    tr.className = "bottom-row";
    tr.dataset.bi = i;
    tr.dataset.tip = recordTip(rec, def.label);
    tr.innerHTML = def.cells(rec).map((c) => `<td>${c == null ? "" : c}</td>`).join("");
    tbody.appendChild(tr);
  });
  el("bottom-empty").hidden = records.length !== 0;
  el("bottom-count").textContent =
    `${records.length} ROWS` + (t === "ol" && state.olNote ? ` — ${state.olNote}` : "");
}

// --- realtime Open Library table --

let olRtTimer = null;
let olRtSeq = 0;
function scheduleOlRealtime() {
  clearTimeout(olRtTimer);
  olRtTimer = setTimeout(olRealtime, 220);
}

async function olRealtime() {
  if (activeBottomTable() !== "ol" || !state.settings.showCatalog) return;
  const params = new URLSearchParams({ limit: "60" });
  const ov = state.olOverride;
  const q = ov
    ? { title: ov.title, author: ov.author || "", year: ov.year || "", empty: false }
    : findQuery();
  const sTitle = el("s-title").value.trim();
  if (q.title) params.set("title", q.title);
  else if (sTitle) params.set("title", sTitle);
  if (ov && ov.verbatim) params.set("title_verbatim", "1");
  if (q.author) params.set("author", q.author);
  if (q.year) params.set("year", q.year);
  for (const f of ["author", "publisher", "city", "year", "edition", "volume"]) {
    const v = el("s-" + f).value.trim();
    if (v && !params.has(f)) params.set(f, v);
  }
  if (![...params.keys()].some((k) => k !== "limit")) {
    state.olRows = [];
    state.olNote = "TYPE IN FIND OR THE SEARCH FORM";
    renderBottomRows();
    return;
  }
  const seq = ++olRtSeq;
  try {
    const data = await (await fetch("/api/ol/realtime?" + params)).json();
    if (seq !== olRtSeq) return;
    state.olRows = data.results || [];
    state.olNote = data.error || data.note || "";
  } catch (e) {
    if (seq !== olRtSeq) return;
    state.olRows = [];
    state.olNote = "SEARCH FAILED";
  }
  renderBottomRows();
}

// --- adding a bottom-pane record to the top-pane table --

async function addToTop(rec) {
  if (state.settings.topTable === "whl") {
    if (whlMode() === "search" && state.whlSelected != null) {
      await repopulateWhlRow(rec);
      return;
    }
    const addBody = { add: {
      title: rec.title + (rec.subtitle ? ": " + rec.subtitle : ""),
      authors: rec.author, year: rec.year,
    } };
    const data = await whlPost(addBody);
    if (data) {
      let curIdx = data.idx;
      pushOp(`add WHL row ${rec.title.slice(0, 34)}`,
        () => whlPost({ remove_added: curIdx }),
        async () => {
          const d = await whlPost(addBody);
          if (d) curIdx = d.idx;
        });
      status(`ADDED TO WHL CATALOG (CORRECTIONS) :: ${rec.title}`);
    } else {
      status("WHL ADD FAILED");
    }
    return;
  }
  // top = checked books
  if (rec._src === "ch") { addChBook(rec._idx); return; }
  const body = {
    title: rec.title + (rec.subtitle ? ": " + rec.subtitle : ""),
    author: rec.author, publisher: rec.publisher, city: rec.city,
    year: rec.year, edition: rec.edition, volume: rec.volume,
    language: rec.language, pages: rec.pages || "",
    condition: "", price: "", illustrations: "", categories: rec.categories || "",
    notes: rec.url ? `From ${rec._src === "ol" ? "Open Library" : "WHL catalog"}: ${rec.url}` : "",
  };
  try {
    const res = await fetch("/api/manual", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    if (res.ok && data.ok) {
      state.manual.unshift(data.entry);
      pushManualCreateOp(data.entry);
      renderChecked();
      queueScan(data.entry.id);
      status(`ADDED TO CHECKED BOOKS :: ${rec.title}`);
    } else {
      status(data.error || "ADD FAILED");
    }
  } catch (e) {
    status("ADD FAILED");
  }
}

function addChBook(idx) {
  const book = (state.chBooks || []).find((x) => x.idx === idx);
  if (!book) return;
  const key = ckey("ch_library", idx);
  trackChecked(`add ${book.title.slice(0, 40)}`, key, () => {
    const prev = state.checked.get(key) || {};
    state.checked.set(key, {
      book,
      checks: prev.checks || null,
      scans: prev.scans || null,
      verify: prev.verify || null,
      manual_urls: prev.manual_urls || null,
    });
    if (!prev.scans) queueScan(key);
  });
  saveChecked();
  renderChecked();
  updateCheckedCount();
  status(`ADDED TO CHECKED BOOKS :: ${book.title}`);
}

// --- top pane: WHL catalog view (modes, corrections, scrape) ---------------------

function whlMode() { return state.settings.whlMode === "search" ? "search" : "edit"; }

function setWhlMode(m) {
  state.settings.whlMode = m;
  saveSettings();
  if (m !== "search") {
    state.whlSelected = null;
    state.olOverride = null;
  }
  renderWhlTop();
  status(m === "search"
    ? "WHL SEARCH MODE :: click a title to look it up on Open Library; click a result to repopulate the row"
    : "WHL EDIT MODE :: click a cell to correct it; Ctrl+click a row for the full metadata tab");
}

function switchTopTable(t) {
  state.settings.topTable = t;
  saveSettings();
  el("top-table").value = t;
  el("checked-pane").hidden = t !== "checked";
  el("whltop-pane").hidden = t !== "whl";
  for (const id of ["run-scans", "dl-approved", "export-json", "clear-checked"]) {
    el(id).disabled = t !== "checked";
  }
  renderTop();
}

async function renderTop() {
  if (state.settings.topTable === "whl") {
    await loadWhlRows();
    renderWhlTop();
  } else {
    el("whl-mode").hidden = true;
    el("whl-cons").hidden = true;
    renderChecked();
    el("top-count").textContent = "";
  }
}

function renderWhlTop() {
  const mode = whlMode();
  const btn = el("whl-mode");
  btn.hidden = state.settings.topTable !== "whl";
  btn.textContent = `MODE: ${mode.toUpperCase()} (CTRL+E)`;
  el("whl-cons").hidden = state.settings.topTable !== "whl" || mode !== "search";
  const q = findQuery();
  const rows = (state.whlRows || [])
    .filter((r) => matchesFind(q, `${r.title} ${r.subtitle || ""}`, r.authors, r.year));
  const shown = rows.slice(0, 400);
  const tbody = el("whltop-rows");
  tbody.innerHTML = "";
  const editable = (r, f) => mode === "edit"
    ? ` class="editable" data-wedit="${f}"`
    : "";
  for (const r of shown) {
    const tr = document.createElement("tr");
    tr.dataset.widx = r.idx;
    // Corrected, added, and draft rows are visually distinct.
    if (r.added) tr.classList.add("whl-row-added");
    else if (r.corrected) tr.classList.add("whl-row-corrected");
    if (r.status === "draft") tr.classList.add("whl-row-draft");
    if (state.whlSelected === r.idx) tr.classList.add("whl-selected");
    tr.dataset.tip = recordTip(
      Object.assign(whlToRecord(r), { subtitle: r.subtitle || "",
        categories: r.categories || "", notes: r.description || "" }),
      mode === "edit"
        ? "WHL ROW — click a cell to edit; Ctrl+click for the full editor"
        : "WHL ROW — click the title to search Open Library for it");
    tr.innerHTML = `
      <td>${r.added ? "ADDED" : r.corrected ? "EDITED" : r.scraped ? "WEB" : "CSV"}</td>
      <td${editable(r, "title")}${mode === "search" ? ' data-wsearch="1"' : ""}>${esc(r.title)}</td>
      <td${editable(r, "subtitle")}>${esc(r.subtitle || "")}</td>
      <td${editable(r, "authors")}>${esc(r.authors)}</td>
      <td${editable(r, "year")}>${esc(r.year)}</td>
      <td${editable(r, "publisher")}>${esc(r.publisher || "")}</td>
      <td${editable(r, "pages")}>${esc(r.pages || "")}</td>
      <td${editable(r, "language")}>${esc(r.language || "")}</td>
      <td${editable(r, "subject")}>${esc(r.subject || "")}</td>
      <td${editable(r, "description")}>${esc(r.description || "")}</td>
      <td class="col-whl">${r.permalink
        ? badge(r.status === "publish" ? "available" : "missing",
                r.status === "publish" ? "PUB" : (r.status || "?").slice(0, 4).toUpperCase(),
                { href: r.permalink, tip: "Open the WHL catalogue page" })
        : badge("unknown", (r.status || "—").slice(0, 5).toUpperCase())}</td>`;
    tbody.appendChild(tr);
  }
  el("whltop-empty").hidden = shown.length !== 0;
  el("top-count").textContent =
    `${rows.length} WHL ROWS` + (rows.length > 400 ? " (SHOWING 400)" : "");
}

function whlRowByIdx(idx) {
  return (state.whlRows || []).find((r) => r.idx === idx);
}

// --- WHL corrections with undo support --

async function whlPost(body) {
  const res = await fetch("/api/whl_catalog", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok || data.ok === false) return null;
  await loadWhlRows(true);
  renderWhlTop();
  renderBottomRows();
  return data;
}

function whlFieldSnaps(row, fields) {
  return fields.map((f) => ({
    f, val: String(row[f] || ""),
    corrected: !!row.added || (row.edited_fields || []).includes(f),
  }));
}

async function whlApplySnaps(idx, snaps) {
  const fields = {}, clear = [];
  for (const s of snaps) {
    if (s.corrected) fields[s.f] = s.val;
    else clear.push(s.f);
  }
  const body = { idx };
  if (Object.keys(fields).length) body.fields = fields;
  if (clear.length) body.clear_fields = clear;
  return whlPost(body);
}

function pushWhlFieldsOp(label, idx, beforeSnaps, afterFields) {
  pushOp(label,
    () => whlApplySnaps(idx, beforeSnaps),
    () => whlPost({ idx, fields: afterFields }));
}

function selectWhlSearchRow(idx) {
  const row = whlRowByIdx(idx);
  if (!row) return;
  state.whlSelected = idx;
  const cons = state.settings.whlCons || {};
  state.olOverride = {
    title: row.title,
    verbatim: !!cons.title,
    author: cons.authors ? row.authors : "",
    year: cons.year ? row.year : "",
  };
  if (!state.settings.showCatalog) {
    state.settings.showCatalog = true;
    el("show-catalog").checked = true;
    saveSettings();
  }
  const tabs = bottomTabs();
  let i = tabs.indexOf("ol");
  if (i < 0) { tabs.push("ol"); i = tabs.length - 1; }
  state.settings.bottomActive = i;
  saveSettings();
  renderWhlTop();
  renderBottomPane().then(olRealtime);
  status(`SEARCHING OPEN LIBRARY :: ${row.title} — click a result to repopulate this row`);
}

async function repopulateWhlRow(rec) {
  const idx = state.whlSelected;
  const row = whlRowByIdx(idx);
  if (row == null) return;
  const fields = {
    title: rec.title, subtitle: rec.subtitle || "",
    authors: rec.author, year: rec.year,
  };
  const before = whlFieldSnaps(row, Object.keys(fields));
  if (await whlPost({ idx, fields })) {
    pushWhlFieldsOp(`repopulate WHL row ${rec.title.slice(0, 30)}`, idx, before, fields);
    status(`WHL ROW REPOPULATED FROM OPEN LIBRARY :: ${rec.title}`);
  } else {
    status("WHL REPOPULATE FAILED");
  }
}

function startWhlEdit(td) {
  if (td.querySelector("input")) return;
  const tr = td.closest("tr");
  const idx = parseInt(tr.dataset.widx, 10);
  const row = whlRowByIdx(idx);
  if (!row) return;
  const field = td.dataset.wedit;
  const original = String(row[field] || "");
  hideTip();
  td.classList.add("editing");
  td.innerHTML = `<input class="cell-edit" value="${esc(original)}" />`;
  const input = td.querySelector("input");
  input.focus();
  input.select();
  let done = false;
  const finish = async (commit) => {
    if (done) return;
    done = true;
    input.blur();
    const val = input.value.trim();
    if (commit && val !== original.trim()) {
      const before = whlFieldSnaps(row, [field]);
      if (await whlPost({ idx, fields: { [field]: val } })) {
        pushWhlFieldsOp(`correct WHL ${field} of ${row.title.slice(0, 28)}`,
          idx, before, { [field]: val });
        status(`WHL ${field.toUpperCase()} CORRECTED :: ${row.title}`);
        return;
      }
      status("WHL EDIT FAILED");
    }
    renderWhlTop();
  };
  input.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") { ev.preventDefault(); finish(true); }
    else if (ev.key === "Escape") { ev.stopPropagation(); finish(false); }
  });
  input.addEventListener("blur", () => finish(true));
}

// --- full-record editor tab (Ctrl+click in edit mode) --

function openWhlEditTab(idx) {
  const row = whlRowByIdx(idx);
  if (!row) return;
  state.whlEditIdx = idx;
  el("whledit-tab").hidden = false;
  el("whledit-note").textContent =
    `ROW ${idx >= 0 ? "#" + idx : "(ADDED)"} :: ${row.title.slice(0, 60)}`;
  for (const f of WHL_ROW_FIELDS) el("w-" + f).value = row[f] || "";
  el("whledit-msg").textContent = "";
  switchPaneTab("pane-whledit");
  el("w-title").focus();
}

async function saveWhlEditTab(ev) {
  ev.preventDefault();
  const idx = state.whlEditIdx;
  const row = whlRowByIdx(idx);
  if (row == null) { el("whledit-msg").textContent = "NO ROW LOADED"; return; }
  const fields = {};
  for (const f of WHL_ROW_FIELDS) fields[f] = el("w-" + f).value.trim();
  if (!fields.title) { el("whledit-msg").textContent = "TITLE IS REQUIRED"; return; }
  const before = whlFieldSnaps(row, WHL_ROW_FIELDS);
  if (await whlPost({ idx, fields })) {
    pushWhlFieldsOp(`edit WHL record ${fields.title.slice(0, 30)}`, idx, before, fields);
    el("whledit-msg").textContent = "SAVED";
    status(`WHL CORRECTIONS SAVED :: ${fields.title}`);
  } else {
    el("whledit-msg").textContent = "SAVE FAILED";
  }
}

// --- WHL website metadata scrape --

let scrapePoll = null;
async function startWhlScrape() {
  const btn = el("whl-scrape");
  btn.disabled = true;
  try {
    await fetch("/api/whl_scrape", { method: "POST" });
  } catch (e) {
    btn.disabled = false;
    status("SCRAPE FAILED TO START");
    return;
  }
  status("SCRAPING WHL WEBSITE METADATA ...");
  if (scrapePoll) clearInterval(scrapePoll);
  scrapePoll = setInterval(async () => {
    let s;
    try {
      s = await (await fetch("/api/whl_scrape/status")).json();
    } catch (e) { return; }
    if (s.status === "running") {
      status(`SCRAPING WHL :: PAGE ${s.page}/${s.pages || "?"} — ${s.records || 0} BOOKS`);
      return;
    }
    clearInterval(scrapePoll);
    scrapePoll = null;
    btn.disabled = false;
    if (s.status === "error") {
      status(`SCRAPE ERROR :: ${s.error || "unknown"}`);
      return;
    }
    await loadWhlRows(true);
    renderWhlTop();
    renderBottomRows();
    status(`SCRAPE COMPLETE :: ${s.scraped_total || 0} PUBLISHED BOOKS HAVE FULL METADATA`);
  }, 1500);
}

// --- markdown ---------------------------------------------------------------
// A small renderer covering what catalog descriptions need: headings, bold,
// italic, inline code, links, bullet/numbered lists, paragraphs, rules.

function mdInline(t) {
  return esc(t)
    .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
    .replace(/\*([^*]+)\*/g, "<i>$1</i>")
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener">$1</a>');
}

function mdRender(src) {
  const lines = String(src || "").replace(/\r/g, "").split("\n");
  let html = "", list = null, para = [];
  const flushP = () => {
    if (para.length) { html += `<p>${mdInline(para.join(" "))}</p>`; para = []; }
  };
  const closeList = () => {
    if (list) { html += `</${list}>`; list = null; }
  };
  for (const raw of lines) {
    const line = raw.trim();
    if (!line) { flushP(); closeList(); continue; }
    const h = line.match(/^(#{1,4})\s+(.*)/);
    if (h) {
      flushP(); closeList();
      const lvl = Math.min(h[1].length + 2, 6);
      html += `<h${lvl}>${mdInline(h[2])}</h${lvl}>`;
      continue;
    }
    if (/^(-{3,}|\*{3,})$/.test(line)) { flushP(); closeList(); html += "<hr>"; continue; }
    const ul = line.match(/^[-*]\s+(.*)/);
    if (ul) {
      flushP();
      if (list !== "ul") { closeList(); html += "<ul>"; list = "ul"; }
      html += `<li>${mdInline(ul[1])}</li>`;
      continue;
    }
    const ol = line.match(/^\d+[.)]\s+(.*)/);
    if (ol) {
      flushP();
      if (list !== "ol") { closeList(); html += "<ol>"; list = "ol"; }
      html += `<li>${mdInline(ol[1])}</li>`;
      continue;
    }
    para.push(line);
  }
  flushP();
  closeList();
  return html;
}

function bindMdPreview(textareaId, previewId) {
  const ta = el(textareaId), pv = el(previewId);
  const update = () => { pv.innerHTML = mdRender(ta.value); };
  ta.addEventListener("input", update);
  return update;
}

// The overlay editor targets one textarea at a time (the WHL description).
let mdOverlayUpdate = null;
function openMarkdownEditor(targetTextareaId, title) {
  state.mdTarget = targetTextareaId;
  el("md-title").textContent = title || "MARKDOWN EDITOR";
  el("md-input").value = el(targetTextareaId).value;
  el("md-overlay").hidden = false;
  mdOverlayUpdate();
  el("md-input").focus();
}

function closeMarkdownEditor(apply) {
  if (apply && state.mdTarget) {
    el(state.mdTarget).value = el("md-input").value;
  }
  state.mdTarget = null;
  el("md-overlay").hidden = true;
}

// --- Internet Archive PDF downloads ---------------------------------------------

async function loadDownloads() {
  try {
    const catalog = await (await fetch("/api/ia/downloads")).json();
    state.downloadedIds = new Set(Object.keys(catalog));
  } catch (e) { state.downloadedIds = new Set(); }
}

async function startDownload(identifier, book) {
  if (!identifier) return;
  try {
    const res = await fetch("/api/ia/download", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ identifier, book: book || {} }),
    });
    const data = await res.json();
    state.downloads.set(identifier, data);
    if (data.status === "done") state.downloadedIds.add(identifier);
    else if (data.status === "downloading") pollDownload(identifier);
  } catch (e) {
    status("IA DOWNLOAD FAILED TO START");
  }
  renderChecked();
}

function pollDownload(identifier) {
  if (state.dlTimers.has(identifier)) return;
  let failures = 0;
  const t = setInterval(async () => {
    try {
      const data = await (await fetch(`/api/ia/download/${encodeURIComponent(identifier)}`)).json();
      failures = 0;
      state.downloads.set(identifier, data);
      if (data.status === "downloading") {
        status(`IA DOWNLOAD ${dlPct(data)} :: ${identifier}`);
      } else {
        clearInterval(t);
        state.dlTimers.delete(identifier);
        if (data.status === "done") {
          state.downloadedIds.add(identifier);
          status(`IA PDF SAVED :: ${data.path || identifier}`);
        } else if (data.status === "error") {
          status(`IA DOWNLOAD ERROR :: ${data.error || "unknown"}`);
        }
      }
      renderChecked();
    } catch (e) {
      failures += 1;
      if (failures >= 8) {
        clearInterval(t);
        state.dlTimers.delete(identifier);
        status(`IA DOWNLOAD POLLING STOPPED (SERVER UNREACHABLE) :: ${identifier}`);
      }
    }
  }, 1500);
  state.dlTimers.set(identifier, t);
}

async function downloadApproved() {
  const approved = combinedRows().filter((r) =>
    (getVerify(r, "internet_archive") === "approved" &&
      r.scans && r.scans.internet_archive && r.scans.internet_archive.available === true) ||
    (getVerify(r, "internet_archive") === "rejected" && getManualUrl(r, "internet_archive")));
  if (!approved.length) { status("NO APPROVED IA SOURCES"); return; }
  let started = 0, saved = 0, noIa = 0;
  for (const row of approved) {
    const ident = iaIdentifierForRow(row);
    if (!ident) { noIa += 1; continue; }
    if (state.downloadedIds.has(ident)) { saved += 1; continue; }
    const dl = state.downloads.get(ident);
    if (dl && dl.status === "downloading") continue;
    started += 1;
    await startDownload(ident, row.book);
  }
  status(`IA DOWNLOADS :: ${started} STARTED / ${saved} ALREADY SAVED` +
    (noIa ? ` / ${noIa} WITHOUT IA MATCH` : ""));
}

// --- automatic checks + scans -----------------------------------------------

const scanQueue = [];
let scanQueueRunning = false;

function queueScan(id) {
  if (scanQueue.includes(id)) return;
  scanQueue.push(id);
  processScanQueue();
}

async function processScanQueue() {
  if (scanQueueRunning) return;
  scanQueueRunning = true;
  while (scanQueue.length) {
    const id = scanQueue.shift();
    const row = rowById(id);
    if (!row || !(row.book.title || "").trim()) continue;
    status(`AUTO SCAN :: ${row.book.title}`);
    try {
      const scans = await runRowScans(row);
      status(`AUTO SCAN DONE :: ${row.book.title} :: ${scanStatusLine(scans)}`);
    } catch (e) {
      status(`AUTO SCAN FAILED :: ${row.book.title}`);
    }
    renderChecked();
  }
  scanQueueRunning = false;
}

async function fetchChecks(book) {
  const url = `/api/check?title=${encodeURIComponent(book.title)}` +
    `&author=${encodeURIComponent(book.author || "")}` +
    `&year=${encodeURIComponent(book.year || "")}`;
  const res = await fetch(url);
  if (!res.ok) throw new Error(`check failed (${res.status})`);
  return await res.json();
}

async function fetchScans(book) {
  const url = `/api/scans?title=${encodeURIComponent(book.title)}` +
    `&author=${encodeURIComponent(book.author || "")}` +
    `&year=${encodeURIComponent(book.year || "")}`;
  const res = await fetch(url);
  if (!res.ok) throw new Error(`scan search failed (${res.status})`);
  return await res.json();
}

async function runRowScans(row) {
  if (row.kind === "manual") {
    const res = await fetch(`/api/manual/${encodeURIComponent(row.id)}/scans`, { method: "POST" });
    const data = await res.json();
    if (!data.ok) throw new Error("scan search failed");
    const i = state.manual.findIndex((x) => x.id === row.id);
    if (i >= 0) state.manual[i] = data.entry;
    return data.entry.scans;
  }
  const entry = state.checked.get(row.id);
  if (!entry) return null;
  if (!entry.checks || entry.checks.error) {
    try { entry.checks = await fetchChecks(entry.book); } catch (e) { /* keep going */ }
  }
  entry.scans = await fetchScans(entry.book);
  saveChecked();
  return entry.scans;
}

async function runScansBatch() {
  const rows = combinedRows().filter((r) => !r.scans);
  if (!rows.length) { status("ALL ROWS ALREADY SCANNED"); return; }
  for (const row of rows) queueScan(row.id);
  status(`QUEUED ${rows.length} BOOKS FOR SCANNING`);
}

function scanStatusLine(scans) {
  if (!scans) return "no result";
  const flag = (s) =>
    s.error ? "ERR" : s.available === true ? "YES" : s.available === false ? "NO" : "?";
  return `IA ${flag(scans.internet_archive)} / HT ${flag(scans.hathitrust)}`;
}

// --- per-source verification ------------------------------------------------------

async function setVerify(id, source, verdict, track = true) {
  const row = state.rowsById.get(String(id));
  if (!row) return;
  if (row.kind === "manual") {
    const prior = getVerify(row, source);
    const priorUrl = getManualUrl(row, source);
    const res = await fetch(`/api/manual/${encodeURIComponent(id)}/verify`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source, state: verdict }),
    });
    const data = await res.json().catch(() => ({}));
    if (data.ok) {
      const i = state.manual.findIndex((x) => x.id === id);
      if (i >= 0) state.manual[i] = data.entry;
      if (track) {
        pushOp(`verify ${source} ${verdict} on ${row.book.title.slice(0, 30)}`,
          async () => {
            await setVerify(id, source, prior, false);
            if (priorUrl) await setManualUrl(id, source, priorUrl, false);
          },
          () => setVerify(id, source, verdict, false));
      }
    }
  } else {
    const entry = state.checked.get(id);
    if (!entry) return;
    const mutate = () => {
      entry.verify = Object.assign({}, migrateVerify(entry) || {});
      if (verdict === "pending") delete entry.verify[source];
      else entry.verify[source] = verdict;
      if (verdict !== "rejected" && entry.manual_urls) delete entry.manual_urls[source];
      entry.approved = null;
    };
    if (track) {
      trackChecked(`verify ${source} ${verdict} on ${row.book.title.slice(0, 30)}`,
        id, mutate);
    } else {
      mutate();
    }
    saveChecked();
  }
  renderChecked();
  const names = { whl: "WHL", internet_archive: "IA", hathitrust: "HT" };
  status(`${names[source] || source} MATCH ${verdict.toUpperCase()} :: ${row.book.title}`);
}

function cycleVerify(id, source) {
  const row = state.rowsById.get(String(id));
  if (!row) return;
  const cur = getVerify(row, source);
  const next = cur === "pending" ? "approved" : cur === "approved" ? "rejected" : "pending";
  setVerify(id, source, next);
}

// --- manually located sources ------------------------------------------------------

async function setManualUrl(id, source, url, track = true) {
  const row = state.rowsById.get(String(id));
  if (!row) return;
  if (row.kind === "manual") {
    const prior = getManualUrl(row, source);
    const res = await fetch(`/api/manual/${encodeURIComponent(id)}/source`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source, url }),
    });
    const data = await res.json().catch(() => ({}));
    if (data.ok) {
      const i = state.manual.findIndex((x) => x.id === id);
      if (i >= 0) state.manual[i] = data.entry;
      if (track) {
        pushOp(`manual source on ${row.book.title.slice(0, 32)}`,
          () => setManualUrl(id, source, prior, false),
          () => setManualUrl(id, source, url, false));
      }
    }
  } else {
    const entry = state.checked.get(id);
    if (!entry) return;
    const mutate = () => {
      entry.manual_urls = Object.assign({}, entry.manual_urls || {});
      if (url) entry.manual_urls[source] = url;
      else delete entry.manual_urls[source];
    };
    if (track) {
      trackChecked(`manual source on ${row.book.title.slice(0, 32)}`, id, mutate);
    } else {
      mutate();
    }
    saveChecked();
  }
  renderChecked();
  status(url ? `MANUAL SOURCE SAVED :: ${row.book.title}` : "MANUAL SOURCE CLEARED");
}

function openManualSource(id, source) {
  state.msrcTarget = { id: String(id), source };
  const row = state.rowsById.get(String(id));
  const names = { whl: "WHL", internet_archive: "INTERNET ARCHIVE", hathitrust: "HATHITRUST" };
  el("msrc-label").textContent =
    `${names[source] || source} :: ${row ? row.book.title : id} — the automatic match was ` +
    `rejected; paste the URL of the correct record.`;
  el("msrc-url").value = row ? getManualUrl(row, source) : "";
  el("msrc-msg").textContent = "";
  el("msrc-overlay").hidden = false;
  el("msrc-url").focus();
}

function closeManualSource() { el("msrc-overlay").hidden = true; }

async function saveManualSource(clear) {
  const t = state.msrcTarget;
  if (!t) return;
  const url = clear ? "" : el("msrc-url").value.trim();
  if (url && !/^https?:\/\//i.test(url)) {
    el("msrc-msg").textContent = "URL MUST START WITH http(s)://";
    return;
  }
  await setManualUrl(t.id, t.source, url);
  closeManualSource();
}

// --- manual entry form -----------------------------------------------------------

const PROV_FIELDS = ["title", "author", "publisher", "city", "year", "edition", "volume"];
const EDITION_CONSTRAINT_FIELDS = ["publisher", "city", "year", "edition", "volume"];

function setProv(field, kind) {
  if (kind) state.prov[field] = kind;
  else delete state.prov[field];
  const input = el("m-" + field);
  input.classList.toggle("prov-manual", state.prov[field] === "manual");
  input.classList.toggle("prov-auto", state.prov[field] === "auto");
}

function clearProv() {
  for (const f of PROV_FIELDS) setProv(f, null);
}

function initPaneTabs() {
  for (const t of document.querySelectorAll(".pane-tab[data-ptab]")) {
    t.addEventListener("click", () => switchPaneTab(t.dataset.ptab));
  }
}

function switchPaneTab(id) {
  document.querySelectorAll(".pane-tab[data-ptab]").forEach((t) =>
    t.classList.toggle("active", t.dataset.ptab === id));
  document.querySelectorAll("#manual-pane .pane-sub").forEach((p) =>
    p.classList.toggle("active", p.id === id));
}

async function loadOlStatus() {
  try {
    const st = await (await fetch("/api/ol/status")).json();
    const ed = st.editions || {};
    el("status-right").textContent = ed.available
      ? `OL INDEX: ${(ed.editions / 1e6).toFixed(1)}M EDITIONS`
      : (st.available ? `OL WORKS INDEX: ${(st.works / 1e6).toFixed(1)}M` : "NO OL INDEX");
  } catch (e) { /* leave empty */ }
}

let olTimer = null;
function onTitleInput() {
  clearTimeout(olTimer);
  const q = el("m-title").value.trim();
  if (q.length < 3) { hideOlSuggest(); return; }
  olTimer = setTimeout(fetchOlSuggest, 350);
}

function manualConstraints(prefix) {
  const params = {};
  for (const f of EDITION_CONSTRAINT_FIELDS.concat(["author"])) {
    if (prefix === "m-" && state.prov[f] !== "manual") continue;
    const v = el(prefix + f).value.trim();
    if (v) params[f] = v;
  }
  return params;
}

async function fetchOlSuggest() {
  const q = el("m-title").value.trim();
  if (q.length < 3) { hideOlSuggest(); return; }
  const params = new URLSearchParams(
    Object.assign({ title: q, limit: "8" }, manualConstraints("m-")));
  let data;
  try {
    data = await (await fetch("/api/ol/search?" + params)).json();
  } catch (e) { hideOlSuggest(); return; }
  if (el("m-title").value.trim() !== q) return;
  renderOlSuggest(data);
}

function renderOlSuggest(data) {
  const wrap = el("ol-suggest");
  const results = data.results || [];
  if (data.error || data.note) {
    wrap.innerHTML = `<div class="ol-note">${esc(data.error || data.note)}</div>`;
    wrap.hidden = false;
    return;
  }
  if (!results.length) { hideOlSuggest(); return; }
  wrap.innerHTML = results.map((r, i) => `
    <div class="ol-item" data-i="${i}">
      <span class="t">${esc(r.title)}${r.subtitle ? `: ${esc(r.subtitle)}` : ""}</span>
      <span class="m">${esc((r.authors || []).filter((a) => a && a !== "?").join("; ")) || "&mdash;"}${r.year ? ` [${r.year}]` : (r.first_year ? ` [${r.first_year}]` : "")}</span>
    </div>`).join("");
  wrap.querySelectorAll(".ol-item").forEach((item) =>
    item.addEventListener("mousedown", (ev) => {
      ev.preventDefault();
      pickOlWork(results[parseInt(item.dataset.i, 10)]);
    }));
  wrap.hidden = false;
}

function hideOlSuggest() { el("ol-suggest").hidden = true; }

function fillAuto(field, value) {
  if (!value) return;
  if (field !== "title" && state.prov[field] === "manual") return;
  el("m-" + field).value = value;
  setProv(field, "auto");
}

function populateFromWork(r, best) {
  fillAuto("title", r.title + (r.subtitle ? ": " + r.subtitle : ""));
  fillAuto("author", (r.authors || []).filter((a) => a && a !== "?").join("; "));
  if (best) {
    for (const f of EDITION_CONSTRAINT_FIELDS) fillAuto(f, best[f]);
  }
}

async function pickOlWork(r) {
  hideOlSuggest();
  if (r.kind === "edition") {
    populateFromWork(r, {
      publisher: r.publisher, city: r.city, year: r.year,
      edition: r.edition, volume: r.volume,
    });
    status(`OPEN LIBRARY :: POPULATED FROM EDITION ${r.key}`);
    return;
  }
  status(`OPEN LIBRARY :: FETCHING EDITIONS :: ${r.title}`);
  let best = null, count = 0;
  try {
    const params = new URLSearchParams(
      Object.assign({ work: r.key }, manualConstraints("m-")));
    params.delete("author");
    const data = await (await fetch("/api/ol/editions?" + params)).json();
    if (data.ok) { best = data.best; count = data.editions_count; }
  } catch (e) { /* populate what the work record has */ }
  populateFromWork(r, best);
  status(`OPEN LIBRARY :: POPULATED FROM ${r.key}` +
    (count ? ` (BEST OF ${count} EDITIONS)` : " (NO EDITION DETAILS)"));
}

async function olSearch(ev) {
  ev.preventDefault();
  if (!state.settings.showCatalog) {
    state.settings.showCatalog = true;
    el("show-catalog").checked = true;
    saveSettings();
  }
  const tabs = bottomTabs();
  let i = tabs.indexOf("ol");
  if (i < 0) { tabs.push("ol"); i = tabs.length - 1; }
  state.settings.bottomActive = i;
  saveSettings();
  await renderBottomPane();
  await olRealtime();
  el("ol-msg").textContent =
    `${(state.olRows || []).length} RESULTS IN THE OPEN LIBRARY TABLE BELOW`;
}

function manualStatusLine(entry) {
  const c = entry.checks || {};
  const cp = c.copyright_status || "?";
  const whl = { yes: "IN WHL", draft: "WHL DRAFT", no: "NOT IN WHL" }[c.in_whl] || "WHL ?";
  return `SUBMITTED :: ${entry.title} :: ${cp.toUpperCase()} / ${whl}`;
}

// Old title pages carry Roman-numeral dates; the footer shows the Arabic
// year while one is typed.
function romanToArabic(s) {
  const t = String(s || "").trim().toUpperCase().replace(/\.$/, "").replace(/\s+/g, "");
  if (!t || !/^[MDCLXVI]+$/.test(t)) return null;
  const vals = { M: 1000, D: 500, C: 100, L: 50, X: 10, V: 5, I: 1 };
  let total = 0;
  for (let i = 0; i < t.length; i++) {
    const v = vals[t[i]];
    total += v < (vals[t[i + 1]] || 0) ? -v : v;
  }
  return total >= 1 && total <= 3999 ? total : null;
}

function onYearInput() {
  const raw = el("m-year").value;
  const n = romanToArabic(raw);
  el("status-right").textContent =
    n ? `ROMAN YEAR :: ${raw.trim().toUpperCase()} = ${n}` : "";
}

async function submitManual(ev) {
  ev.preventDefault();
  const body = {};
  for (const f of MANUAL_FIELDS) body[f] = el("m-" + f).value;
  if (!body.title.trim()) { el("manual-msg").textContent = "TITLE IS REQUIRED"; return; }

  const btn = el("manual-submit");
  btn.disabled = true;
  el("manual-msg").textContent = "CHECKING ...";
  status(`CHECKING :: ${body.title}`);
  try {
    const res = await fetch("/api/manual", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    if (res.ok && data.ok) {
      state.manual.unshift(data.entry);
      pushManualCreateOp(data.entry);
      renderChecked();
      el("manual-form").reset();
      clearProv();
      hideOlSuggest();
      el("m-title").focus();
      el("manual-msg").textContent = "SAVED";
      status(manualStatusLine(data.entry));
      queueScan(data.entry.id);
    } else {
      el("manual-msg").textContent = data.error || "SAVE FAILED";
    }
  } catch (e) {
    el("manual-msg").textContent = "SAVE FAILED";
  }
  btn.disabled = false;
}

async function loadManual() {
  try {
    const res = await fetch("/api/manual");
    state.manual = res.ok ? await res.json() : [];
  } catch (e) { state.manual = []; }
  updateCheckedCount();
  renderChecked();
}

async function deleteManualById(id) {
  const res = await fetch(`/api/manual/${encodeURIComponent(id)}`, { method: "DELETE" });
  if (!res.ok) return false;
  state.manual = state.manual.filter((x) => x.id !== id);
  renderChecked();
  return true;
}

async function restoreManualEntry(snap) {
  const res = await fetch("/api/manual/restore", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ entry: snap }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok || !data.ok) return false;
  state.manual = state.manual.filter((x) => x.id !== snap.id);
  state.manual.unshift(data.entry);
  renderChecked();
  return true;
}

function pushManualCreateOp(entry) {
  const snap = JSON.parse(JSON.stringify(entry));
  pushOp(`create entry ${String(entry.title || "").slice(0, 36)}`,
    () => deleteManualById(snap.id),
    () => restoreManualEntry(snap));
}

async function deleteManual(id) {
  const e = state.manual.find((x) => x.id === id);
  if (!window.confirm(`Delete manual entry "${e ? e.title : id}"?`)) return;
  const snap = e ? JSON.parse(JSON.stringify(e)) : null;
  if (await deleteManualById(id)) {
    if (snap) {
      pushOp(`delete entry ${String(snap.title || "").slice(0, 36)}`,
        () => restoreManualEntry(snap),
        () => deleteManualById(snap.id));
    }
    status("MANUAL ENTRY DELETED");
  } else {
    status("DELETE FAILED");
  }
}

// --- checked-tab batch actions ----------------------------------------------------

function exportJson() {
  const payload = combinedRows().map((r) => ({
    source: r.kind === "manual" ? "manual_entries" : r.source,
    metadata: r.book,
    checks: r.checks || null,
    scans: r.scans || null,
    mark: rowMarkState(r),
    verify: r.verify || {},
  }));
  const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "whl_checked_books.json";
  a.click();
  URL.revokeObjectURL(a.href);
  status(`EXPORTED ${payload.length} RECORDS`);
}

function clearChecked() {
  if (!window.confirm("Remove ALL checked catalog books? (Manual entries are kept.)")) return;
  const before = JSON.parse(JSON.stringify([...state.checked.entries()]));
  const applyMap = (entries) => {
    state.checked = new Map(JSON.parse(JSON.stringify(entries)));
    saveChecked();
    renderChecked();
    updateCheckedCount();
  };
  state.checked.clear();
  pushOp(`clear ${before.length} checked books`,
    () => applyMap(before),
    () => applyMap([]));
  saveChecked();
  renderChecked();
  status("CLEARED CHECKED BOOKS");
}

// --- upload list: approved sources + the book builder ------------------------------

const ARCHIVE_NAMES = { internet_archive: "Internet Archive", hathitrust: "HathiTrust" };

function approvedSources() {
  const out = [];
  for (const row of combinedRows()) {
    for (const source of ["internet_archive", "hathitrust"]) {
      const meta = {
        title: row.book.title || "",
        subtitle: row.book.subtitle || "",
        author: row.book.author || "",
        publisher: row.book.publisher || "",
        year: row.book.year || "",
        archive: ARCHIVE_NAMES[source],
      };
      const st = getVerify(row, source);
      if (st === "approved") {
        const s = row.scans && row.scans[source];
        const b = s && s.available === true ? s.best_match : null;
        if (!b) continue;
        out.push(Object.assign(meta, {
          url: b.url || b.record_url || "",
          matched_title: b.title || "",
          identifier: b.identifier || "",
          _rowId: row.id,
        }));
      } else if (st === "rejected" && getManualUrl(row, source)) {
        const murl = getManualUrl(row, source);
        out.push(Object.assign(meta, {
          url: murl,
          matched_title: "(manually located source)",
          identifier: murl.includes("/details/")
            ? murl.split("/details/")[1].split(/[/?#]/)[0] : "",
          _rowId: row.id,
        }));
      }
    }
  }
  return out;
}

function renderUpload() {
  renderBuildsList();
  renderBuildEditor();
  const sources = approvedSources();
  const tbody = el("upload-rows");
  tbody.innerHTML = "";
  el("sources-count").textContent = `${sources.length} APPROVED SOURCES`;
  el("upload-empty").hidden = sources.length !== 0;
  sources.forEach((s, i) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${esc(s.title)}</td>
      <td>${esc(s.subtitle)}</td>
      <td>${esc(s.author)}</td>
      <td>${esc(s.publisher)}</td>
      <td>${esc(s.year)}</td>
      <td>${esc(s.archive)}</td>
      <td>${s.url
        ? `<a href="${esc(s.url)}" target="_blank" rel="noopener">${esc(s.matched_title) || "(record)"}</a>`
        : esc(s.matched_title)}</td>
      <td class="col-act"><button class="cad-btn tiny" data-build-src="${i}"
        data-tip="Start a catalog entry prefilled from this source">BUILD</button></td>`;
    tbody.appendChild(tr);
  });
  el("upload-count").textContent =
    `${Object.keys(state.builds).length} ENTRIES / ` +
    `${Object.values(state.builds).filter((b) => b.status === "ready").length} READY`;
}

function downloadUploadList() {
  const sources = approvedSources().map(({ _rowId, ...s }) => s);
  if (!sources.length) { status("NO APPROVED SOURCES"); return; }
  const blob = new Blob([JSON.stringify(sources, null, 2)], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "whl_upload_list.json";
  a.click();
  URL.revokeObjectURL(a.href);
  status(`DOWNLOADED SOURCES LIST :: ${sources.length}`);
}

// --- the book builder --

async function loadBuilds() {
  try {
    const res = await fetch("/api/builds");
    state.builds = res.ok ? (await res.json()).builds || {} : {};
  } catch (e) { state.builds = {}; }
}

function buildsSorted() {
  return Object.values(state.builds)
    .sort((a, b) => (b.updated_at || "").localeCompare(a.updated_at || ""));
}

function renderBuildsList() {
  const list = el("builds-list");
  list.innerHTML = "";
  const builds = buildsSorted();
  el("builds-empty").hidden = builds.length !== 0;
  for (const b of builds) {
    const li = document.createElement("li");
    li.className = "build-item" + (b.id === state.buildSel ? " active" : "") +
      (b.status === "ready" ? " ready" : "");
    li.dataset.bid = b.id;
    li.dataset.tip = `${b.title || "(untitled)"}\n` +
      `${b.authors ? "AUTHORS: " + b.authors + "\n" : ""}` +
      `${b.year ? "YEAR: " + b.year + "\n" : ""}` +
      `STATUS: ${(b.status || "draft").toUpperCase()}\nUPDATED: ${b.updated_at || ""}`;
    li.innerHTML = `
      <span class="bi-title">${esc(b.title) || "<em>(untitled)</em>"}</span>
      <span class="bi-meta">${esc(b.year || "")} ${b.status === "ready" ? "&#10003; READY" : "DRAFT"}</span>`;
    list.appendChild(li);
  }
}

function renderBuildEditor() {
  const ed = el("build-editor");
  const b = state.buildSel ? state.builds[state.buildSel] : null;
  ed.hidden = !b;
  if (!b) return;
  for (const f of BUILD_FIELDS) {
    const input = el("b-" + f);
    if (input) input.value = b[f] || "";
  }
  el("b-ready").checked = b.status === "ready";
  el("b-description").value = b.description || "";
  el("b-desc-preview").innerHTML = mdRender(b.description || "");
  const pdf = (b.pdf_source || "").trim();
  el("b-pdf-open").hidden = !/^https?:\/\//i.test(pdf);
  el("b-pdf-open").href = pdf;
  const src = (b.source_url || "").trim();
  el("b-src-open").hidden = !/^https?:\/\//i.test(src);
  el("b-src-open").href = src;
  el("build-msg").textContent = "";
  el("build-desc-msg").textContent = "";
}

function selectBuild(id) {
  state.buildSel = id;
  renderBuildsList();
  renderBuildEditor();
}

async function createBuild(seed, label) {
  const res = await fetch("/api/builds", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ build: seed || {} }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok || !data.ok) { status("BUILD CREATE FAILED"); return null; }
  state.builds[data.build.id] = data.build;
  const snap = JSON.parse(JSON.stringify(data.build));
  pushOp(`create build ${label || snap.title || snap.id}`,
    async () => {
      await fetch(`/api/builds/${snap.id}`, { method: "DELETE" });
      delete state.builds[snap.id];
      if (state.buildSel === snap.id) state.buildSel = null;
      renderUpload();
    },
    async () => {
      await fetch("/api/builds/restore", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ build: snap }),
      });
      state.builds[snap.id] = snap;
      renderUpload();
    });
  selectBuild(data.build.id);
  renderUpload();
  return data.build;
}

function buildSeedFromSource(s) {
  // If the PDF was already downloaded from IA, point at the local file.
  let pdf = "";
  if (s.identifier && state.downloadedIds.has(s.identifier)) {
    pdf = `downloads/ia/${s.identifier}.pdf`;
  } else if (s.identifier) {
    pdf = `https://archive.org/download/${s.identifier}/${s.identifier}.pdf`;
  }
  return {
    title: s.title, subtitle: s.subtitle, authors: s.author,
    year: s.year, publisher: s.publisher,
    pdf_source: pdf, source_url: s.url,
    notes: `Source: ${s.archive}${s.matched_title ? " — " + s.matched_title : ""}`,
  };
}

async function patchBuild(id, fields, label) {
  const b = state.builds[id];
  if (!b) return false;
  const before = {};
  for (const f of Object.keys(fields)) before[f] = b[f] || "";
  const doPatch = async (vals) => {
    const res = await fetch(`/api/builds/${encodeURIComponent(id)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(vals),
    });
    const data = await res.json().catch(() => ({}));
    if (res.ok && data.ok) {
      state.builds[id] = data.build;
      renderUpload();
      return true;
    }
    return false;
  };
  if (await doPatch(fields)) {
    pushOp(label || `edit build ${b.title || id}`,
      () => doPatch(before),
      () => doPatch(fields));
    return true;
  }
  return false;
}

async function saveBuildFields(ev) {
  ev.preventDefault();
  const id = state.buildSel;
  if (!id) return;
  const fields = {};
  for (const f of BUILD_FIELDS) {
    const input = el("b-" + f);
    if (input) fields[f] = input.value.trim();
  }
  fields.status = el("b-ready").checked ? "ready" : "draft";
  if (!fields.title) { el("build-msg").textContent = "TITLE IS REQUIRED"; return; }
  if (await patchBuild(id, fields, `edit build ${fields.title.slice(0, 30)}`)) {
    el("build-msg").textContent = "SAVED";
    status(`BUILD SAVED :: ${fields.title}`);
  } else {
    el("build-msg").textContent = "SAVE FAILED";
  }
}

async function saveBuildDescription() {
  const id = state.buildSel;
  if (!id) return;
  if (await patchBuild(id, { description: el("b-description").value },
      "edit build description")) {
    el("build-desc-msg").textContent = "SAVED";
    status("BUILD DESCRIPTION SAVED");
  } else {
    el("build-desc-msg").textContent = "SAVE FAILED";
  }
}

async function deleteBuild() {
  const id = state.buildSel;
  const b = state.builds[id];
  if (!b) return;
  if (!window.confirm(`Delete entry "${b.title || id}"?`)) return;
  const snap = JSON.parse(JSON.stringify(b));
  const res = await fetch(`/api/builds/${encodeURIComponent(id)}`, { method: "DELETE" });
  if (res.ok) {
    delete state.builds[id];
    state.buildSel = null;
    pushOp(`delete build ${snap.title || id}`,
      async () => {
        await fetch("/api/builds/restore", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ build: snap }),
        });
        state.builds[snap.id] = snap;
        renderUpload();
      },
      async () => {
        await fetch(`/api/builds/${snap.id}`, { method: "DELETE" });
        delete state.builds[snap.id];
        renderUpload();
      });
    renderUpload();
    status(`BUILD DELETED :: ${snap.title || id}`);
  } else {
    status("BUILD DELETE FAILED");
  }
}

function exportBuilds() {
  const builds = buildsSorted();
  if (!builds.length) { status("NO ENTRIES TO EXPORT"); return; }
  const blob = new Blob([JSON.stringify(builds, null, 2)], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "whl_submission_entries.json";
  a.click();
  URL.revokeObjectURL(a.href);
  const ready = builds.filter((b) => b.status === "ready").length;
  status(`EXPORTED ${builds.length} ENTRIES (${ready} READY)`);
}

function switchBuildTab(id) {
  document.querySelectorAll("#build-editor .pane-tab").forEach((t) =>
    t.classList.toggle("active", t.dataset.btab === id));
  document.querySelectorAll("#build-editor .pane-sub").forEach((p) =>
    p.classList.toggle("active", p.id === id));
}

// --- wire up ---------------------------------------------------------------

function init() {
  loadSettings();
  applyTheme();
  applyFont();
  loadChecked();
  initTabs();
  initTooltips();
  initPaneTabs();
  loadOlStatus();

  // undo / redo
  el("undo-btn").addEventListener("click", undo);
  el("redo-btn").addEventListener("click", redo);
  document.addEventListener("keydown", (ev) => {
    if (!(ev.ctrlKey || ev.metaKey)) return;
    const t = ev.target;
    if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA")) return;
    const k = ev.key.toLowerCase();
    if (k === "z" && !ev.shiftKey) { ev.preventDefault(); undo(); }
    else if (k === "y" || (k === "z" && ev.shiftKey)) { ev.preventDefault(); redo(); }
  });

  // checked tab toolbar
  el("checked-search").addEventListener("input", () => {
    state.checkedFilter = el("checked-search").value.trim();
    state.olOverride = null;
    renderTop();
    renderBottomRows();
    scheduleOlRealtime();
  });
  el("mark-filter").value = state.settings.markFilter || "ALL";
  el("mark-filter").addEventListener("change", () => {
    state.settings.markFilter = el("mark-filter").value;
    saveSettings();
    renderChecked();
  });
  el("show-catalog").checked = !!state.settings.showCatalog;
  el("show-catalog").addEventListener("change", () => {
    state.settings.showCatalog = el("show-catalog").checked;
    saveSettings();
    renderBottomPane();
  });
  el("run-scans").addEventListener("click", runScansBatch);
  el("whl-scrape").addEventListener("click", startWhlScrape);
  el("dl-approved").addEventListener("click", downloadApproved);
  el("export-json").addEventListener("click", exportJson);
  el("clear-checked").addEventListener("click", clearChecked);
  el("checked-rows").addEventListener("click", onCheckedClick);

  // top pane: table selector + WHL interactions
  el("top-table").addEventListener("change", () => switchTopTable(el("top-table").value));
  el("whl-mode").addEventListener("click", () =>
    setWhlMode(whlMode() === "edit" ? "search" : "edit"));
  document.addEventListener("keydown", (ev) => {
    if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === "e" &&
        state.settings.topTable === "whl") {
      ev.preventDefault();
      setWhlMode(whlMode() === "edit" ? "search" : "edit");
    }
  });
  el("whltop-rows").addEventListener("click", (ev) => {
    if (ev.target.closest("a")) return;
    const tr = ev.target.closest("tr");
    if (!tr) return;
    const idx = parseInt(tr.dataset.widx, 10);
    if (whlMode() === "search") {
      if (ev.target.closest("td[data-wsearch]")) selectWhlSearchRow(idx);
      return;
    }
    if (ev.ctrlKey || ev.metaKey) { openWhlEditTab(idx); return; }
    const td = ev.target.closest("td[data-wedit]");
    if (td) startWhlEdit(td);
  });
  el("whledit-form").addEventListener("submit", saveWhlEditTab);
  const cons = state.settings.whlCons;
  for (const [box, key] of [["wc-title", "title"], ["wc-authors", "authors"], ["wc-year", "year"]]) {
    el(box).checked = !!cons[key];
    el(box).addEventListener("change", () => {
      cons[key] = el(box).checked;
      saveSettings();
      if (whlMode() === "search" && state.whlSelected != null) {
        selectWhlSearchRow(state.whlSelected);
      }
    });
  }

  // bottom pane
  el("bottom-addtab").addEventListener("click", () => {
    bottomTabs().push("ol");
    state.settings.bottomActive = state.settings.bottomTabs.length - 1;
    saveSettings();
    renderBottomPane();
  });
  el("bottom-rows").addEventListener("click", (ev) => {
    if (ev.target.closest("a")) return;
    const tr = ev.target.closest("tr.bottom-row");
    if (!tr) return;
    const rec = state.bottomRecords[parseInt(tr.dataset.bi, 10)];
    if (rec) addToTop(rec);
  });

  // left panel: search form, manual entry, provenance, autocomplete
  el("ol-form").addEventListener("submit", olSearch);
  for (const f of PROV_FIELDS) {
    el("m-" + f).addEventListener("input", () =>
      setProv(f, el("m-" + f).value.trim() ? "manual" : null));
    el("s-" + f).addEventListener("input", scheduleOlRealtime);
  }
  el("m-title").addEventListener("input", onTitleInput);
  el("m-title").addEventListener("blur", () => setTimeout(hideOlSuggest, 150));
  el("m-title").addEventListener("keydown", (ev) => {
    if (ev.key === "Escape") { ev.stopPropagation(); hideOlSuggest(); }
  });
  el("m-year").addEventListener("input", onYearInput);
  el("manual-form").addEventListener("submit", submitManual);

  // resizable left panel
  (() => {
    const sp = el("pane-splitter");
    const pane = el("manual-pane");
    if (state.settings.paneWidth) pane.style.width = state.settings.paneWidth + "px";
    let dragging = false;
    sp.addEventListener("mousedown", (ev) => {
      dragging = true;
      ev.preventDefault();
      document.body.classList.add("resizing");
    });
    document.addEventListener("mousemove", (ev) => {
      if (!dragging) return;
      const left = pane.getBoundingClientRect().left;
      pane.style.width = Math.min(760, Math.max(260, ev.clientX - left)) + "px";
    });
    document.addEventListener("mouseup", () => {
      if (!dragging) return;
      dragging = false;
      document.body.classList.remove("resizing");
      state.settings.paneWidth = parseInt(pane.style.width, 10) || null;
      saveSettings();
    });
  })();

  // markdown editor (overlay for the WHL description, inline for builds)
  mdOverlayUpdate = bindMdPreview("md-input", "md-preview");
  el("w-desc-md").addEventListener("click", () =>
    openMarkdownEditor("w-description", "MARKDOWN :: WHL DESCRIPTION"));
  el("md-apply").addEventListener("click", () => closeMarkdownEditor(true));
  el("md-cancel").addEventListener("click", () => closeMarkdownEditor(false));
  el("md-close").addEventListener("click", () => closeMarkdownEditor(false));
  el("md-overlay").addEventListener("mousedown", (ev) => {
    if (ev.target === el("md-overlay")) closeMarkdownEditor(false);
  });
  bindMdPreview("b-description", "b-desc-preview");

  // upload list / book builder
  el("build-new").addEventListener("click", () => createBuild({}, "(blank)"));
  el("export-builds").addEventListener("click", exportBuilds);
  el("download-upload-list").addEventListener("click", downloadUploadList);
  el("builds-list").addEventListener("click", (ev) => {
    const li = ev.target.closest("li.build-item");
    if (li) selectBuild(li.dataset.bid);
  });
  el("build-form").addEventListener("submit", saveBuildFields);
  el("build-delete").addEventListener("click", deleteBuild);
  el("build-desc-save").addEventListener("click", saveBuildDescription);
  el("upload-rows").addEventListener("click", (ev) => {
    const b = ev.target.closest("[data-build-src]");
    if (!b) return;
    const s = approvedSources()[parseInt(b.dataset.buildSrc, 10)];
    if (s) createBuild(buildSeedFromSource(s), s.title.slice(0, 30));
  });
  for (const t of document.querySelectorAll("#build-editor .pane-tab")) {
    t.addEventListener("click", () => switchBuildTab(t.dataset.btab));
  }

  // manual source + settings windows
  el("msrc-close").addEventListener("click", closeManualSource);
  el("msrc-save").addEventListener("click", () => saveManualSource(false));
  el("msrc-clear").addEventListener("click", () => saveManualSource(true));
  el("msrc-url").addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") { ev.preventDefault(); saveManualSource(false); }
  });
  el("msrc-overlay").addEventListener("mousedown", (ev) => {
    if (ev.target === el("msrc-overlay")) closeManualSource();
  });
  el("open-settings").addEventListener("click", openSettings);
  el("settings-close").addEventListener("click", closeSettings);
  el("settings-overlay").addEventListener("mousedown", (ev) => {
    if (ev.target === el("settings-overlay")) closeSettings();
  });
  document.addEventListener("keydown", (ev) => {
    if (ev.key !== "Escape") return;
    if (!el("md-overlay").hidden) closeMarkdownEditor(false);
    else if (!el("msrc-overlay").hidden) closeManualSource();
    else if (!el("settings-overlay").hidden) closeSettings();
  });

  loadDownloads();
  loadManual();
  loadBuilds().then(renderUpload);
  switchTopTable(state.settings.topTable === "whl" ? "whl" : "checked");
  renderBottomPane();
}

init();

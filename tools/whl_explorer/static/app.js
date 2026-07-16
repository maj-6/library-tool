"use strict";

/* Library Tool front-end.
 *
 * Chrome: a titlebar, an application toolbar (undo/redo, the active tab's
 * commands, settings), two top-level tabs, and a status bar (footer) that
 * carries the WHL mode tag.
 *
 *   CHECKED BOOKS — the working area: a top table (checked books + manual
 *     entries, or the editable WHL catalog), a left panel (Open Library
 *     search / manual entry / WHL record editor), and a tabbed bottom pane
 *     of live-filtered source tables (Open Library / master list / WHL).
 *   UPLOAD LIST — the book builder (catalog entries being prepared for WHL
 *     submission, with a live Markdown description editor and a PDF source
 *     tab) over a resizable approved-sources table.
 *
 * Reusable components (built for reuse elsewhere in the interface):
 *   createMdEditor(container)  — Obsidian-style live Markdown editor
 *   createPdfViewer()          — embedded PDF viewer (local via /api/pdf,
 *                                or a remote URL)
 *   openFileBrowser(start, cb) — local-directory PDF picker window
 *
 * All mutating actions are undoable (see the history section).
 */

const LS_KEY = "whl_cad_checked_v1";
const SETTINGS_KEY = "whl_cad_settings_v1";
const VIEWSTATE_KEY = "whl_cad_viewstate_v1";
// Per-device UI / session state: persisted LOCALLY but never synced to the server
// as "settings". These are things one machine should not push onto another — the
// active table/mode, toolbar filters, split-pane and column sizes, and the
// first-run flags. Everything else in state.settings is a real preference and
// still syncs. (state.settings stays the single in-memory object; only the
// persistence layer partitions it.)
const VIEW_STATE_KEYS = new Set([
  "markFilter", "srcFilter", "dlFilter", "yearFrom", "yearTo",
  "topTable", "bottomActive", "whlMode", "checkedMode", "showCatalog",
  "paneWidth", "uploadSplitH", "publishSidebarCollapsed", "workbenchPhase",
  "jobsDrawerOpen", "authPromptDismissed", "checkedCols",
]);
// Credentials are never persisted client-side. They live in the server's
// Host-guarded secrets store (/api/secrets); Mistral additionally syncs through
// the signed-in user's private cloud profile so Book Capture can share it.
const SECRET_KEYS = new Set([
  "aiKey", "mistralKey", "ocrClaudeKey", "ocrAzureKey", "ocrAwsKey",
  "ocrAwsSecret", "supabaseKey", "supabaseAnonKey", "r2KeyId", "r2Secret",
  "gsKeyFile",
]);
function partitionSettings(s) {
  const prefs = {}, view = {};
  for (const k of Object.keys(s)) {
    if (SECRET_KEYS.has(k)) continue;               // secrets go to the server-only store
    (VIEW_STATE_KEYS.has(k) ? view : prefs)[k] = s[k];
  }
  return { prefs, view };
}

// Hydrate the renderer's ephemeral credential cache. Mistral may arrive from
// the signed-in user's private cloud profile; partitionSettings keeps every
// value here out of localStorage and client_state.
async function hydrateSecrets() {
  try {
    const res = await fetch("/api/secrets");
    if (!res.ok) return {};
    const secrets = await res.json();
    for (const k of SECRET_KEYS) state.settings[k] = secrets[k] || "";
    return secrets;
  } catch (e) {
    return {};
  }
}

// Persist personal service credentials through the same loopback-only endpoint
// used by Settings. Normal settings deliberately exclude SECRET_KEYS, so a
// wizard must never rely on saveSettings() for these values.
async function persistSecrets(updates) {
  const clean = {};
  for (const [k, value] of Object.entries(updates || {})) {
    if (!SECRET_KEYS.has(k)) continue;
    clean[k] = String(value || "").trim();
  }
  if (!Object.keys(clean).length) return;
  const res = await fetch("/api/secrets", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ updates: clean }),
  });
  if (!res.ok) throw new Error(`credential save failed (HTTP ${res.status})`);
  for (const [k, value] of Object.entries(clean)) state.settings[k] = value;
}

// `categories` left this list with the taxonomy overhaul: assignments are
// category_ids lists handled by the chip pickers, not looped text inputs.
const MANUAL_FIELDS = [
  "title", "subtitle", "author", "publisher", "city", "year", "edition",
  "volume", "language", "pages", "condition", "price", "illustrations",
  "notes",
];

// Metadata columns of the combined table, in cell order; click-to-edit.
const BOOK_COLS = [
  "title", "subtitle", "author", "year", "edition", "volume", "publisher",
  "city", "language", "pages", "condition", "illustrations", "price",
  "acquired", "categories", "notes",
];

const CHECKED_COLS = [
  ["src", "Src"], ["title", "Title"], ["subtitle", "Subtitle"],
  ["author", "Author"], ["year", "Year"],
  ["edition", "Edition"], ["volume", "Volume"], ["publisher", "Publisher"],
  ["city", "City"], ["language", "Language"], ["pages", "Pages"],
  ["condition", "Condition"], ["illustrations", "Illustrations"],
  ["price", "Price"], ["acquired", "Acquired"], ["categories", "Categories"],
  ["notes", "Notes"], ["img", "Img"], ["copyright", "Copyright"],
  ["whl", "WHL"], ["ia", "IA"], ["ht", "HT"], ["mark", "Mark"],
];

const WHL_ROW_FIELDS = ["title", "subtitle", "authors", "year", "publisher",
  "pages", "language", "subject", "categories", "description"];

const BUILD_FIELDS = ["title", "subtitle", "authors", "year", "publisher",
  "publisher_city", "edition", "volume", "group_id", "language", "pages",
  "rights", "pdf_source", "pdf_file", "source_url", "notes"];

const state = {
  // key `${source}:${idx}` -> { book, checks, scans, verify, manual_urls }
  checked: new Map(),
  manual: [],
  rowsById: new Map(),        // combined-table row lookup (per render)
  checkedFilter: "",
  chBooks: null,              // CH catalogue rows (lazy)
  whlRows: null,              // WHL catalogue + scrape + corrections (lazy)
  whlSelected: null,          // search-mode repopulation target (WHL row idx)
  checkedSelected: null,      // search-mode repopulation target (checked row id)
  whlEditIdx: null,           // row loaded in the WHL EDIT tab
  olOverride: null,           // search-mode query override
  olRows: null,               // realtime Open Library results
  olNote: "",
  bottomRecords: [],
  uploadSources: [],          // approved sources as last rendered
  builds: {},                 // book builder entries (id -> build)
  buildSel: null,             // selected build id
  taxonomy: {},               // category nodes (id -> {name, parent})
  anSel: null,                // build open in the Analyze tab
  publishEntries: [],         // public volumes rows shown in the Publish tree
  publishSel: null,           // stable "book:<slug>" or "set:<group_id>" key
  publishOpen: new Set(),     // explicitly opened Publish tree folders
  publishClosed: new Set(),   // organizational folders closed from default-open
  publishDetails: new Map(),  // slug -> {about, notes} preview cache
  publishLoaded: false,
  publishSource: "",
  publishWarning: "",
  publishSiteUrl: "",
  publishCatalogSeq: 0,
  publishCatalogLoading: null,
  publishPreviewSeq: 0,
  buildFolder: null,          // entry-folder info for the selected build
  downloads: new Map(),
  dlTimers: new Map(),
  downloadedIds: new Set(),
  autoDlQueue: [],            // {ident, book} pending background IA downloads
  autoDlActive: new Set(),    // identifiers currently auto-downloading
  prov: {},                   // manual-form field provenance
  msrcTarget: null,
  mdTarget: null,             // markdown overlay target textarea
  settings: {
    checkedCols: {}, showCatalog: true,
    markFilter: "ALL", srcFilter: "ALL", dlFilter: "ALL",
    yearFrom: null, yearTo: null,   // inclusive year-range filter (both tables)
    // Open Library search constraints (title=verbatim; also toggled by Ctrl+click
    // a column header). Persistent; extra fields (publisher/city/…) via Ctrl+click.
    searchCons: { author: true, year: true },
    autoIaDownload: true,           // background-download an IA PDF when a source is found
    topTable: "checked", bottomActive: 0,
    whlMode: "search", checkedMode: "search",
    // "" is the retired Classic CAD id; applyTheme() migrates it to DEFAULT_THEME
    paneWidth: null, theme: "", font: "", fontUi: "", fontMono2: "",
    aiBase: "", aiModel: "", aiKey: "", aiInstructions: "",
    // embeddings provider for search-index publishing (#140); blank = lexical-only
    embedBase: "", embedModel: "", embedKey: "",
    // OCR services (Settings > OCR). Tesseract runs locally; Claude /
    // Textract / Azure / OpenAI need credentials — cloud processing is
    // TODO-verify until the user has API keys.
    // Cloud capture (phone -> Supabase -> manual entries) + Mistral key,
    // shared by the capture pipeline and the Mistral OCR service
    supabaseUrl: "", supabaseKey: "", supabaseAnonKey: "", mistralKey: "",
    authPromptDismissed: false,   // "Work locally" said: don't ask at startup
    wizardDone: false,            // first-run setup guide finished or skipped
    // Cloudflare R2 for published PDFs (Supabase storage is 1 GB on free)
    r2Account: "", r2Bucket: "", r2KeyId: "", r2Secret: "", r2PublicBase: "",
    cloudSyncMinutes: 0, cloudDeleteRemote: true,
    ocrService: "tesseract", ocrAzureEndpoint: "", ocrAzureKey: "",
    ocrTesseract: "", ocrClaudeKey: "", ocrClaudeModel: "",
    ocrAwsKey: "", ocrAwsSecret: "", ocrAwsRegion: "",
    ocrImageWidth: 1400,
    ocrLayout: true,    // page view: place words where they sit on the page
    ocrFurniture: true, // region facsimile: show marginalia/headers/catchwords
    textAnalysisService: "configured",
    workbenchPhase: "record",   // last phase open on the Workbench
    jobsDrawerOpen: false,      // the jobs drawer starts as a one-line summary
    userName: "",       // attributed to your changes in the activity feed        // rasterization width for OCR input —
                                // tune to see how shrinking affects quality
    // page-view digit shortcuts: press N over a page to queue it
    ocrKeyMap: { 1: "tesseract", 2: "claude", 3: "textract", 4: "azure", 5: "openai" },
    // master list -> Google Sheets publishing (Settings > Sync)
    gsSpreadsheetId: "", gsKeyFile: "", gsSheetName: "Master list",
    // cloud search + downloadable databases (Settings > Sync)
    cloudSearchUrl: "",         // remote instance of this app; used when no local index
    dbUrls: {},                 // per-database download URLs (name -> url)
    uploadSplitH: null, pdfBrowseDir: "",
    scanRecentMin: 30,          // scan-attach picker: show only PDFs this new
    whlModalOcr: false,         // OCR panel in the WHL publication viewer
    previewPages: 20,           // page cap for PDF preview derivatives
    previewOriginal: false,     // view the original PDF instead of a preview
    keepOriginals: true,        // keep IA originals after a folder build
    trimBlank: false,           // auto-trim blank pages during folder sync
    colVis: {},                 // per-table column visibility
    colWidths: {},              // per-table column widths (px)
    sets: {},                   // multi-volume sets: baseKey -> {count, exp}
    expandSets: false,          // expand multi-volume sets by default
    hideVolTitles: false,       // hide the titles of individual volumes
    // --- Settings redesign, Stage 2: new tunables ---
    aiTemperature: "",          // "" = per-call defaults; a number overrides all Analyze calls
    aiTimeout: 240,             // seconds allowed for an Analyze/AI request
    ocrMaxTokens: 8192,         // vision-OCR output cap (raise for dense pages)
    historyLimit: 300,          // recent actions shown in the History feed
    olLimit: 60,                // Open Library results per realtime search
    confirmDiscard: true,       // ask before discarding unsaved page edits
    verboseLogging: false,      // raise the server log level to DEBUG
    autoUpdate: true,           // desktop: check for updates on launch
    includePrereleaseUpdates: false, // desktop: allow alpha/beta/rc auto-updates
    publishGroup: "sets",           // organization of the Publish file tree
    publishSidebarCollapsed: false,
  },
  editTarget: null,             // record open in the EDIT tab
  sort: { checked: null, whl: null },  // {key, dir} per top table
  olColMarks: {},               // OL column -> "copy" | "exclude" (repopulation)
};

// --- appearance: themes are full chrome redesigns; fonts are user-selectable --

// All light. This is a scholarly tool, read for hours: no dark modes, no loud
// accents. The paper set differs by stock, rule weight and ink colour; the
// classic set translates period desktop chrome (bevels, pinstripes, navy
// bands) onto the same fixed geometry.
const THEMES = [
  ["sage", "Sage"],
  ["ledger", "Ledger"],
  ["foolscap", "Manuscript"],
  ["vellum", "Vellum"],
  ["linen", "Linen"],
];
const DEFAULT_THEME = "sage";
// Retired ids map to the survivor closest in spirit, so a stored theme never
// falls through to the bare :root fallback. "" was Classic CAD, the old default.
const LEGACY_THEMES = {
  "": DEFAULT_THEME,
  // platinum retired 2026-07-11 -> linen, the surviving neutral; every chrome
  // id that used to inherit platinum follows it there.
  platinum: "linen",
  quarto: "linen", pewter: "linen", folio: "linen",
  redmond: "linen", motif: "linen",
  // the dark/loud round, retired earlier
  scope: "linen", "terminal-amber": "vellum", "blueprint-linen": "linen",
  oxblood: "ledger", porcelain: "linen", herbarium: "vellum",
  // the original classic-chrome set
  blueprint: "linen", modern: "linen",
  dark: "linen", stone: "linen", midnight: "linen",
  cde: "linen", xp2003: "linen", acad: "linen",
  workstation: "linen", slate: "linen", mainframe: "vellum",
  graphite: "linen",
};

// OCR engines, mirrored from the two <select id="…ocr-service"> lists so the
// Settings-menu picker stays in step with them (index.html).
const OCR_SERVICES = [
  ["tesseract", "Tesseract (local)"],
  ["mistral", "Mistral OCR"],
  ["claude", "Claude"],
  ["textract", "Amazon Textract"],
  ["azure", "Azure Document Intelligence"],
  ["openai", "OpenAI vision"],
];

// One shared font list; the interface (--ui) and data/table (--mono) fonts
// are chosen independently from it.
const FONT_CHOICES = [
  ["", "Default"],
  ['"Segoe UI", Tahoma, sans-serif', "Segoe UI"],
  ['Tahoma, "Segoe UI", sans-serif', "Tahoma"],
  ['Verdana, Geneva, sans-serif', "Verdana"],
  ['Arial, Helvetica, sans-serif', "Arial"],
  ['"Trebuchet MS", Tahoma, sans-serif', "Trebuchet MS"],
  ['Calibri, "Segoe UI", sans-serif', "Calibri"],
  ['Georgia, "Times New Roman", serif', "Georgia"],
  ['"Consolas", "Courier New", monospace', "Consolas"],
  ['"Courier New", Courier, monospace', "Courier New"],
  ['"Lucida Console", Monaco, monospace', "Lucida Console"],
  ['"Cascadia Mono", Consolas, monospace', "Cascadia Mono"],
  ['"Cascadia Code", Consolas, monospace', "Cascadia Code"],
  ['"IBM Plex Mono", Consolas, monospace', "IBM Plex Mono"],
  ['"JetBrains Mono", Consolas, monospace', "JetBrains Mono"],
  ['"Source Code Pro", Consolas, monospace', "Source Code Pro"],
  ['"Fira Code", Consolas, monospace', "Fira Code"],
];

// --- text normalization for Open Library fills ----------------------------------

const TC_SMALL = new Set(["a", "an", "and", "as", "at", "but", "by", "for",
  "from", "in", "into", "nor", "of", "on", "or", "the", "to", "with", "upon",
  "de", "la", "le", "du", "des", "et", "von", "van", "der"]);

// conventional title case; words that already carry interior capitals
// (acronyms, McNames, Roman numerals) and digit-leading words (2nd, 4to)
// are left alone
function titleCase(s) {
  s = String(s || "").trim();
  if (!s) return s;
  const words = s.split(/\s+/);
  return words.map((w, i) => {
    if (/[A-Z]/.test(w.slice(1))) return w;
    if (/^["'([]*[0-9]/.test(w)) return w;
    const core = w.toLowerCase().replace(/[^a-z']/g, "");
    if (i !== 0 && i !== words.length - 1 && TC_SMALL.has(core))
      return w.toLowerCase();
    return w.toLowerCase().replace(/^([^a-z]*)([a-z])/,
      (m, pre, c) => pre + c.toUpperCase());
  }).join(" ");
}

// "Last, First" -> "First Last" (per author; credential tails left alone)
function flipName(name) {
  return String(name || "").split(";").map((part) => {
    const p = part.trim();
    if (!p) return "";
    const m = p.match(/^([^,]+),\s*([^,]+)$/);
    if (!m) return p;
    const tail = m[2].trim();
    if (/^(jr|sr|esq|md|m\.\s?d|phd|ph\.\s?d|[ivx]+)\.?$/i.test(tail)) return p;
    return `${tail} ${m[1].trim()}`;
  }).filter(Boolean).join("; ");
}

// --- bibliographic title parsing --------------------------------------------------
// "Title: Subtitle" splits at the first colon; volume indicators (vol. 1,
// v2, v. iii) and edition indicators (2nd ed., Third Edition) are removed
// from the title text and land in their own fields. parseBook() only fills
// EMPTY fields (existing subtitle/volume/edition values are never clobbered,
// and the title keeps its indicator when the target field is occupied), so
// the parse is idempotent and safe to run over existing entries.

// roman numerals reuse romanToArabic (declared later; hoisted), capped to
// plausible volume/edition numbers
function romanToInt(s) {
  const n = romanToArabic(s);
  return n && n < 200 ? n : 0;
}

// tidy a string after a token was cut out of it
function tidyTitleText(s) {
  return String(s || "")
    .replace(/\(\s*\)|\[\s*\]/g, " ")        // emptied brackets
    .replace(/\s{2,}/g, " ")
    .replace(/\s+([,;.)\]])/g, "$1")
    .replace(/^[\s,;:.\-]+|[\s,;:\-]+$/g, "")
    .replace(/,\s*\./g, ".")
    .trim();
}

// Editor/author initials masquerade as roman numerals ("ed. C. F. Leyel",
// "by V. L. Komarov"). A single-letter "numeral" is rejected when it can
// only plausibly be an initial: L/C/D/M alone (50/100/500/1000 are never
// written as one letter), or I/V/X followed by a capitalized word — the
// start of a name.
function romanTokenPlausible(tok, after) {
  if (/^[0-9]+$/.test(tok)) return true;
  if (tok.length === 1) {
    if (/[lcdm]/i.test(tok)) return false;
    if (/^\s*[A-Z]/.test(after)) return false;
  }
  return true;
}

// volume indicators: "volume 2", "vol. 1", "vols. 3", "v. iii", "v2"
const VOL_RES = [
  /(^|[\s,;:.([])(?:volumes?|vols?\.?)\s*(?:no\.?\s*)?([0-9]{1,4}|[ivxlcdm]{1,8})(?![a-z0-9])\.?/i,
  /(^|[\s,;:.([])v\.\s*([0-9]{1,4}|[ivxlcdm]{1,8})(?![a-z0-9])\.?/i,
  /(^|[\s,;:.([])v([0-9]{1,4})(?![a-z0-9])/i,
];

function extractVolume(text) {
  for (const re of VOL_RES) {
    const m = re.exec(text);
    if (!m) continue;
    let num = m[2];
    if (!romanTokenPlausible(num, text.slice(m.index + m[0].length))) continue;
    if (!/^[0-9]+$/.test(num)) {
      const r = romanToInt(num);
      if (!r) continue;
      num = String(r);
    }
    return {
      clean: tidyTitleText(text.slice(0, m.index) + m[1] + text.slice(m.index + m[0].length)),
      volume: num,
    };
  }
  return null;
}

// A volume-of-total designator: an explicit "N/M" or "N of M" (optionally with
// a vol/v. keyword) at the very END of the title — e.g. "Elements of Botany
// 2/5" (or a whole volume field of "2/5"). Unlike VOL_RES it carries the SET
// SIZE (the total), used to auto-create a group. ANCHORED to the end so an
// incidental fraction earlier in a title ("The 1/2 Blood Prince", "Notes on
// 3/4 and 5/8 meter") is never misread; a page/part prefix ("pp. 2/5") and a
// non-lettered base are rejected. `clean` has the designator removed.
const VOL_TOTAL_RE =
  /(^|[\s,;:.([])(?:vol(?:ume)?s?\.?\s*|v\.?\s*)?([0-9]{1,3})\s*(?:\/|\s+of\s+)\s*([0-9]{1,3})\s*[)\].]*\s*$/i;
const VOL_PAGEISH_RE = /\b(?:p|pp|pg|pgs|pages?|pt|pts|parts?|no|nos|num|figs?|plates?)\.?\s*$/i;

function extractVolTotal(text) {
  const s = String(text || "");
  const m = VOL_TOTAL_RE.exec(s);
  if (!m) return null;
  const vol = parseInt(m[2], 10), total = parseInt(m[3], 10);
  if (!(vol > 0) || !(total > 1) || total > 99 || vol > total) return null;
  const head = s.slice(0, m.index) + m[1];       // everything up to the designator
  if (VOL_PAGEISH_RE.test(head)) return null;     // "pp. 2/5" is a page range, not a set
  const clean = tidyTitleText(head + s.slice(m.index + m[0].length));
  if (!/[a-z]/i.test(clean)) return null;         // the base must be a real, lettered title
  return { vol, total, clean };
}

// Grouping intent from an edited TITLE: { title: clean, volume: "N", count }.
// Prefers the "N/M" total form (declares an M-volume set); falls back to a
// plain "vol N" indicator (sets the volume, leaves the set size open: count 0).
function volGroupFromTitle(value) {
  const vt = extractVolTotal(value);
  if (vt && vt.clean) return { title: vt.clean, volume: String(vt.vol), count: vt.total };
  const v = extractVolume(value);
  if (v && v.clean) return { title: v.clean, volume: v.volume, count: 0 };
  return null;
}

// Grouping intent from an edited VOLUME field: { volume: "N", count }.
// "N/M" -> volume N of an M-volume set; "N" (no slash) -> volume N of an
// N-volume set (per the spec). Non-numeric input returns null (kept verbatim).
function volGroupFromVolume(value) {
  const m = String(value || "").trim().match(/^([0-9]{1,3})\s*(?:\/\s*([0-9]{1,3}))?$/);
  if (!m) return null;
  const vol = parseInt(m[1], 10);
  if (!(vol > 0)) return null;
  const total = m[2] ? parseInt(m[2], 10) : vol;
  if (total > 99 || vol > total) return null;
  return { volume: String(vol), count: total };
}

const ORD_WORDS = {
  first: 1, second: 2, third: 3, fourth: 4, fifth: 5, sixth: 6, seventh: 7,
  eighth: 8, ninth: 9, tenth: 10, eleventh: 11, twelfth: 12,
};

function ordinal(n) {
  const rem = n % 100;
  if (rem >= 11 && rem <= 13) return n + "th";
  return n + (["th", "st", "nd", "rd"][n % 10] || "th");
}

// edition indicators: "2nd edition", "3d ed.", "Third Edition", "ed. 2",
// "edition iv". Bare "ed." accepts only digits — "ed. <letter>" is an
// editor credit ("ed. C. F. Leyel"), not an edition.
const ED_RES = [
  [/(^|[\s,;:.([])([0-9]{1,2})(?:st|nd|rd|th|d)?\s+(?:rev(?:ised)?\.?\s+)?ed(?:ition|n)?\b\.?/i,
   (m) => parseInt(m[2], 10)],
  [/(^|[\s,;:.([])(first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|eleventh|twelfth)\s+(?:rev(?:ised)?\.?\s+)?ed(?:ition|n)?\b\.?/i,
   (m) => ORD_WORDS[m[2].toLowerCase()]],
  [/(^|[\s,;:.([])edition\s*([0-9]{1,2}|[ivxlcdm]{1,6})(?![a-z0-9])\.?/i,
   (m) => /^[0-9]+$/.test(m[2]) ? parseInt(m[2], 10) : romanToInt(m[2])],
  [/(^|[\s,;:.([])ed\.\s*([0-9]{1,2})(?![a-z0-9])\.?/i,
   (m) => parseInt(m[2], 10)],
];

function extractEdition(text) {
  for (const [re, toNum] of ED_RES) {
    const m = re.exec(text);
    if (!m) continue;
    if (!romanTokenPlausible(m[2], text.slice(m.index + m[0].length))) continue;
    const n = toNum(m);
    if (!n) continue;
    return {
      clean: tidyTitleText(text.slice(0, m.index) + m[1] + text.slice(m.index + m[0].length)),
      edition: ordinal(n),
    };
  }
  return null;
}

// fill a book's empty subtitle/volume/edition fields from its title text
function parseBook(b) {
  const out = Object.assign({}, b);
  let title = String(out.title || "").trim();
  if (!String(out.subtitle || "").trim()) {
    const m = title.match(/^(.+?):\s+(.+)$/);
    if (m) { title = m[1].trim(); out.subtitle = m[2].trim(); }
  }
  let subtitle = String(out.subtitle || "");
  const fields = [
    ["volume", extractVolume],
    ["edition", extractEdition],
  ];
  for (const [field, extract] of fields) {
    if (String(out[field] || "").trim()) continue;
    // an extraction must never leave the title empty ("Vol. 2" stays a title)
    let r = extract(title);
    if (r && r.clean) {
      title = r.clean;
    } else {
      r = extract(subtitle);
      if (r) subtitle = r.clean;
      else r = null;
    }
    if (r) out[field] = r.volume || r.edition;
  }
  out.title = title;
  out.subtitle = subtitle;
  return out;
}

function bookParseChanged(a, b) {
  return ["title", "subtitle", "volume", "edition"].some(
    (f) => String(a[f] || "") !== String(b[f] || ""));
}

function applyTheme() {
  let t = state.settings.theme || "";
  // migrate a retired id, and clamp anything unrecognised: an orphan id would
  // otherwise stick in localStorage, sync to the server, and silently render
  // the bare :root fallback while the picker showed nothing
  if (t in LEGACY_THEMES) t = LEGACY_THEMES[t];
  if (!THEMES.some(([id]) => id === t)) t = DEFAULT_THEME;
  if (t !== state.settings.theme) {
    state.settings.theme = t;
    saveSettings();
  }
  document.body.dataset.theme = t;
}

// the one way to change theme: the Settings menu and the Appearance select
// both come through here, so the menu ticks and the <select> never disagree
function setTheme(id) {
  state.settings.theme = id;
  saveSettings();
  applyTheme();
  const sel = el("theme-select");
  if (sel) sel.value = id;
}

function applyFont() {
  const f = state.settings.font || "";
  if (f) document.body.style.setProperty("--mono", f);
  else document.body.style.removeProperty("--mono");
  const u = state.settings.fontUi || "";
  if (u) document.body.style.setProperty("--ui", u);
  else document.body.style.removeProperty("--ui");
  const m2 = state.settings.fontMono2 || "";
  if (m2) document.body.style.setProperty("--mono2", m2);
  else document.body.style.removeProperty("--mono2");
}

// --- Experimental: interface sharpening ---------------------------------------
// An opt-in unsharp filter drawn over the whole UI. A click-through overlay
// (#exp-sharpen-layer) carries a backdrop-filter that runs an SVG convolution
// over everything painted behind it, so text and hairlines crispen without any
// change to the DOM beneath. The kernel is the identity plus a scaled cross
// Laplacian; it sums to 1, so overall brightness is preserved. When the feature
// is off (or amount 0) the layer is hidden and carries no filter, costing
// nothing. Confirmed to render in the desktop shell's Chromium.
const EXP_SHARPEN_MAX = 1.4;        // convolution strength at 100%

function expSharpenAmount() {
  let a = parseInt(state.settings.expSharpenAmount, 10);
  if (!Number.isFinite(a)) a = 0;
  return Math.max(0, Math.min(100, a));
}

function applyExpSharpen() {
  const layer = el("exp-sharpen-layer");
  const kernel = el("exp-sharpen-kernel");
  if (!layer || !kernel) return;             // markup absent in this build
  const pct = expSharpenAmount();
  const on = !!state.settings.expSharpen && pct > 0;
  if (!on) {
    layer.hidden = true;
    layer.style.backdropFilter = "";
    layer.style.webkitBackdropFilter = "";
    return;
  }
  const a = (pct / 100) * EXP_SHARPEN_MAX;
  const c = 1 + 4 * a, e = -a;
  kernel.setAttribute("kernelMatrix", `0 ${e} 0  ${e} ${c} ${e}  0 ${e} 0`);
  layer.hidden = false;
  layer.style.backdropFilter = "url(#exp-sharpen-filter)";
  layer.style.webkitBackdropFilter = "url(#exp-sharpen-filter)";
}

const el = (id) => document.getElementById(id);
const esc = (s) =>
  String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

// The footer carries the last thing that happened, at its true severity:
// "error" tints it amber, "critical" red. A failure that only cost time is an
// error; one that means the user's data did not persist, or that the backend is
// gone, is critical. Any later plain status() clears the tint, so no timer.
// Every line is also teed into the Info tab's console, which keeps scrollback.
function status(msg, level) {
  const n = el("status-msg");
  n.textContent = msg;
  n.classList.toggle("err", level === "error");
  n.classList.toggle("crit", level === "critical");
  // #status-msg is role="status" aria-live in the template; a critical failure
  // (data did not persist, backend gone) interrupts, everything else is polite.
  n.setAttribute("aria-live", level === "critical" ? "assertive" : "polite");
  conPut(level === "critical" ? "error" : level === "error" ? "warn" : "info", msg, "app");
}
const statusErr = (msg) => status(msg, "error");
const statusCrit = (msg) => status(msg, "critical");

// --- console (Info tab) -------------------------------------------------------
// One stream, two sources: `app` lines are teed from status() and from anything
// the page throws; `server`/`http` lines are pulled from the Flask ring over
// /api/log. The footer only ever shows the newest line -- this is the scrollback.

const CON_CAP = 3000;
const CON_RANK = { debug: 0, info: 1, warn: 2, error: 3 };
const conState = { lines: [], since: 0, follow: true, dirty: false, dropped: false };

function conPut(level, msg, src) {
  conState.lines.push({ ts: Date.now(), level, src: src || "app", msg: String(msg) });
  if (conState.lines.length > CON_CAP) conState.lines.splice(0, conState.lines.length - CON_CAP);
  conState.dirty = true;
  if (conVisible()) scheduleConRender();
}

function conVisible() {
  const p = el("infotab");
  return !!p && p.classList.contains("active");
}

let _conTimer = null;
function scheduleConRender() {
  if (_conTimer) return;
  _conTimer = setTimeout(() => { _conTimer = null; renderConsole(); }, 120);
}

function conFiltered() {
  const min = CON_RANK[el("con-level").value] ?? 1;
  const q = el("con-find").value.trim().toLowerCase();
  return conState.lines.filter((l) =>
    (CON_RANK[l.level] ?? 1) >= min && (!q || l.msg.toLowerCase().includes(q)));
}

const conTime = (ts) => new Date(ts).toTimeString().slice(0, 8);

function renderConsole() {
  const box = el("con-lines");
  if (!box) return;
  conState.dirty = false;
  const rows = conFiltered();
  el("con-count").textContent =
    `${rows.length}/${conState.lines.length}` + (conState.dropped ? " (truncated)" : "");
  box.innerHTML = rows.map((l) =>
    `<div class="con-line con-${esc(l.level)}">` +
    `<span class="con-ts">${conTime(l.ts)}</span>` +
    `<span class="con-src">${esc(l.src)}</span>` +
    `<span class="con-msg">${esc(l.msg)}</span></div>`).join("") ||
    `<div class="empty">Nothing to show at this level</div>`;
  if (conState.follow) box.scrollTop = box.scrollHeight;
}

let _conPollBusy = false;
async function pollConsoleLog() {
  if (document.hidden) return;
  // Skip the round-trip while the Info tab isn't showing: the `since` cursor
  // catches the client up in one request when the tab opens (activation fires
  // an immediate poll), and the server keeps its own ring meanwhile.
  if (!conVisible()) return;
  // Single flight: tab activation and the 3s interval can overlap; two polls
  // reading the same cursor would append every returned line twice.
  if (_conPollBusy) return;
  _conPollBusy = true;
  try {
    const r = await (await fetch("/api/log?since=" + conState.since)).json();
    if (!r.ok) return;
    if (r.dropped) conState.dropped = true;
    conState.since = r.next;
    for (const e of r.entries) conPut(e.level, e.msg, e.src);
  } catch (e) { /* the server going away is itself reported by the caller */ }
  finally { _conPollBusy = false; }
}

function initConsole() {
  // anything the page throws lands here, not only in devtools
  addEventListener("error", (ev) =>
    conPut("error", `${ev.message} (${ev.filename}:${ev.lineno})`, "app"));
  addEventListener("unhandledrejection", (ev) =>
    conPut("error", "Unhandled rejection: " + (ev.reason && ev.reason.message || ev.reason), "app"));
  for (const lvl of ["warn", "error"]) {
    const orig = console[lvl].bind(console);
    console[lvl] = (...a) => {
      conPut(lvl, a.map((x) => (x && x.stack) || String(x)).join(" "), "app");
      orig(...a);
    };
  }
  el("con-level").addEventListener("change", renderConsole);
  el("con-find").addEventListener("input", renderConsole);
  el("con-follow").addEventListener("click", () => {
    conState.follow = !conState.follow;
    el("con-follow").classList.toggle("active", conState.follow);
    if (conState.follow) renderConsole();
  });
  el("con-lines").addEventListener("scroll", () => {
    // scrolling up detaches follow, the way a terminal does
    const box = el("con-lines");
    const atEnd = box.scrollHeight - box.scrollTop - box.clientHeight < 24;
    if (conState.follow !== atEnd) {
      conState.follow = atEnd;
      el("con-follow").classList.toggle("active", atEnd);
    }
  });
  el("con-copy").addEventListener("click", () => {
    const text = conFiltered().map((l) => `${conTime(l.ts)} ${l.src} ${l.level} ${l.msg}`).join("\n");
    navigator.clipboard.writeText(text).then(() => status(`COPIED ${conFiltered().length} LINES`));
  });
  el("con-clear").addEventListener("click", () => {
    conState.lines = [];
    conState.dropped = false;
    renderConsole();
  });
  pollConsoleLog();
  setInterval(pollConsoleLog, 3000);
}
function ckey(source, idx) { return `${source}:${idx}`; }

// --- icon set (inline SVG, stroke = currentColor) ------------------------------

const _SVG = (body) =>
  `<svg viewBox="0 0 16 16" width="14" height="14" fill="none" ` +
  `stroke="currentColor" stroke-width="1.6" stroke-linecap="round" ` +
  `stroke-linejoin="round" aria-hidden="true">${body}</svg>`;

const ICONS = {
  search: _SVG('<circle cx="7" cy="7" r="4.2"/><path d="M10.2 10.2 L14 14"/>'),
  undo: _SVG('<path d="M3.5 6.5 h6.5 a3.5 3.5 0 0 1 0 7 h-3"/><path d="M6.5 3.5 L3.5 6.5 L6.5 9.5"/>'),
  redo: _SVG('<path d="M12.5 6.5 h-6.5 a3.5 3.5 0 0 0 0 7 h3"/><path d="M9.5 3.5 L12.5 6.5 L9.5 9.5"/>'),
  gear: _SVG('<circle cx="8" cy="8" r="2.6"/><path d="M8 1.8v2M8 12.2v2M1.8 8h2M12.2 8h2M3.6 3.6l1.4 1.4M11 11l1.4 1.4M12.4 3.6L11 5M5 11l-1.4 1.4"/>'),
  download: _SVG('<path d="M8 2.5 v7.5 M4.8 7 L8 10.2 L11.2 7"/><path d="M3 13.2 h10"/>'),
  filter: _SVG('<path d="M2.5 3.5 h11 L9.6 8.4 v4.2 l-3.2 1.4 v-5.6 Z"/>'),
  columns: _SVG('<rect x="2" y="3" width="12" height="10"/><path d="M6.3 3 v10 M10.6 3 v10"/>'),
  save: _SVG('<path d="M3 3 h8 l2 2 v8 h-10 Z"/><path d="M5.5 3 v3.4 h4.4 V3"/><rect x="5" y="9" width="6" height="4"/>'),
  trash: _SVG('<path d="M3 4.5 h10 M6.3 4.5 V3 h3.4 v1.5"/><path d="M4.4 4.5 l0.7 9 h5.8 l0.7-9"/><path d="M6.6 7 v4.4 M9.4 7 v4.4"/>'),
  remove: _SVG('<circle cx="8" cy="8" r="5.6"/><path d="M5.4 8 h5.2"/>'),
  folder: _SVG('<path d="M2 4 h4.4 l1.4 1.8 H14 v7 H2 Z"/>'),
  attach: _SVG('<path d="M11.5 4.6 L6.4 9.7 a1.8 1.8 0 0 0 2.6 2.6 L13.4 7.9 a3.3 3.3 0 0 0-4.6-4.6 L4.2 7.8"/>'),
  docplus: _SVG('<path d="M3.5 2 h6 l3 3 v9 h-9 Z"/><path d="M9.5 2 v3 h3"/><path d="M8 7.5 v4 M6 9.5 h4"/>'),
  plus: _SVG('<path d="M8 2.5 v11 M2.5 8 h11"/>'),
  export: _SVG('<path d="M8 10 V2.5 M4.8 5.5 L8 2.3 L11.2 5.5"/><path d="M3 9.5 v4 h10 v-4"/>'),
  check: _SVG('<path d="M2.8 8.6 L6.4 12 L13.2 4"/>'),
  target: _SVG('<circle cx="8" cy="8" r="4.4"/><path d="M8 1.5 v3 M8 11.5 v3 M1.5 8 h3 M11.5 8 h3"/>'),
  image: _SVG('<rect x="2" y="3" width="12" height="10" rx="1"/><circle cx="5.8" cy="6.3" r="1.1"/><path d="M3.5 11.5 L7 8 l2.2 2.2 L11 8.5 l2.5 3"/>'),
  camera: _SVG('<path d="M2 5 h2.2 l1.1-1.8 h5.4 L11.8 5 H14 v8 H2 Z"/><circle cx="8" cy="8.8" r="2.5"/>'),
  manual: _SVG('<path d="M2.5 2.5 h7 l2.5 2.5 v8.5 h-9.5 Z"/><path d="M9.5 2.5 V5 H12"/><path d="M5 11.8 l.5-2 L11 4.3 l1.7 1.7 -5.5 5.5 Z"/>'),
  listfile: _SVG('<path d="M3 2 h7 l3 3 v9 H3 Z"/><path d="M10 2 v3 h3 M5.2 7.3 h5.6 M5.2 9.6 h5.6 M5.2 11.9 h3.8"/>'),
  text: _SVG('<path d="M2.5 3.5 h11 M2.5 6.5 h11 M2.5 9.5 h8 M2.5 12.5 h10"/>'),
  sparkle: _SVG('<path d="M8 1.8 L9.5 6.5 L14.2 8 L9.5 9.5 L8 14.2 L6.5 9.5 L1.8 8 L6.5 6.5 Z"/><path d="M12.8 2 v2.6 M11.5 3.3 h2.6"/>'),
  fileup: _SVG('<path d="M3.5 2 h6 l3 3 v9 h-9 Z"/><path d="M9.5 2 v3 h3"/><path d="M8 11.5 V7.5 M6.2 9.2 L8 7.4 L9.8 9.2"/>'),
  foldersync: _SVG('<path d="M2 4 h4.4 l1.4 1.8 H14 v7 H2 Z"/><path d="M6 9.4 a2.2 2.2 0 0 1 4.2-.8 M10.4 9.6 a2.2 2.2 0 0 1-4.2.8"/><path d="M10.6 7.4 v1.4 h-1.4 M5.8 11.6 v-1.4 h1.4"/>'),
  pencil: _SVG('<path d="M3 13 l0.8-3 L11 2.8 a1.3 1.3 0 0 1 2.2 2.2 L6 12.2 Z"/><path d="M10.2 3.6 l2.2 2.2"/>'),
  diff: _SVG('<rect x="2" y="2.5" width="5.2" height="11"/><rect x="8.8" y="2.5" width="5.2" height="11"/><path d="M3.4 5.5 h2.4 M3.4 8 h2.4 M10.2 8 h2.4 M10.2 10.5 h2.4"/>'),
  pdfpage: _SVG('<rect x="2" y="2.5" width="5.6" height="11"/><path d="M3.2 5 h3.2 M3.2 7.5 h3.2 M3.2 10 h2.2"/><path d="M9.6 2.5 h4.4 v11 h-4.4 Z" stroke-dasharray="1.6 1.3"/>'),
  replace: _SVG('<path d="M2.5 5.5 h8 M8.2 3.2 L10.5 5.5 L8.2 7.8"/><path d="M13.5 10.5 h-8 M7.8 8.2 L5.5 10.5 L7.8 12.8"/>'),
  star: _SVG('<path d="M8 2 L9.8 6 L14 6.4 L10.8 9.2 L11.8 13.4 L8 11.2 L4.2 13.4 L5.2 9.2 L2 6.4 L6.2 6 Z"/>'),
  go: _SVG('<path d="M2.5 8 h9 M8.2 4.5 L11.8 8 L8.2 11.5"/>'),
  // a page with a figure block above flowing text: the facsimile layout view
  layout: _SVG('<rect x="3" y="2" width="10" height="12" rx="1"/><rect x="5" y="4" width="6" height="3.4"/><path d="M5 9.6 h6 M5 11.8 h4.2"/>'),
  // a page with a note block in the margin beside the text column: the
  // furniture (marginalia / running heads / catchwords) visibility toggle
  margins: _SVG('<rect x="2.5" y="2" width="11" height="12" rx="1"/><rect x="4.2" y="6" width="2.4" height="3.2"/><path d="M8.6 4.4 h3 M8.6 6.6 h3 M8.6 8.8 h3 M8.6 11 h2"/>'),
  pdf: _SVG('<path d="M3.5 2 h6 l3 3 v9 h-9 Z"/><path d="M9.5 2 v3 h3"/><path d="M5.4 7.5 h5.2 M5.4 9.7 h5.2 M5.4 11.9 h3.4"/>'),
  // a magic wand with a star tip: the smart-check trigger
  wand: _SVG('<path d="M2.4 13.6 L9.4 6.6"/><path d="M12 1.6 L12.7 3.3 L14.4 4 L12.7 4.7 L12 6.4 L11.3 4.7 L9.6 4 L11.3 3.3 Z"/><path d="M13.4 7.8 v1.8 M12.5 8.7 h1.8"/>'),
  close: _SVG('<path d="M4.2 4.2 L11.8 11.8 M11.8 4.2 L4.2 11.8"/>'),
};

// Glyphs that stand in for a tag's text label. Sized to sit inside a 15px
// badge (the full-size ICONS are 14px and would overflow it).
const _SVGB = (body) =>
  `<svg class="bico" viewBox="0 0 16 16" width="11" height="11" fill="none" ` +
  `stroke="currentColor" stroke-width="2.2" stroke-linecap="round" ` +
  `stroke-linejoin="round" aria-hidden="true">${body}</svg>`;

const BICONS = {
  check: _SVGB('<path d="M3 8.4 L6.4 11.8 L13 3.8"/>'),      // approved match
  cross: _SVGB('<path d="M4 4 L12 12 M12 4 L4 12"/>'),        // rejected match
  pencil: _SVGB('<path d="M3 13 l0.7-2.8 L10.9 2.6 l2.5 2.5 -8 8 Z"/>'),  // WHL draft
  upload: _SVGB('<path d="M8 12.8 V3.6 M4.4 7.2 L8 3.6 L11.6 7.2"/>'),    // ready to upload
  download: _SVGB('<path d="M8 2.4 V9.6 M4.6 6.4 L8 9.8 L11.4 6.4 M3.8 13.4 H12.2"/>'),  // file on disk
};

function injectIcons() {
  for (const node of document.querySelectorAll("[data-icon]")) {
    const svg = ICONS[node.dataset.icon];
    if (svg) node.innerHTML = svg;
  }
}

// --- confirmation dialog -------------------------------------------------------
// Product-styled replacement for window.confirm: resolves true/false, never
// blocks. `danger` paints the confirming action red and starts focus on the
// safe choice; `cost` renders an amber money/credits note. Enter activates
// the focused button (initial focus = the default), Escape/backdrop/close all
// take the Cancel path.

let _confirmResolve = null;
let _confirmOpener = null;   // survives dialog-replaces-dialog, so focus
                             // returns to the ORIGINAL opener, not the confirm

function confirmDialog(opts) {
  const o = typeof opts === "string" ? { message: opts } : (opts || {});
  const overlay = el("confirm-overlay");
  const okBtn = el("confirm-ok");
  if (_confirmResolve) _confirmResolve(false);   // replaced: keep the opener
  else _confirmOpener = document.activeElement;
  el("confirm-title").textContent = o.title || "Confirm";
  el("confirm-msg").textContent = o.message || "";
  el("confirm-detail").textContent = o.detail || "";
  el("confirm-detail").hidden = !o.detail;
  el("confirm-cost").textContent = o.cost || "";
  el("confirm-cost").hidden = !o.cost;
  okBtn.textContent = o.confirmLabel || "OK";
  okBtn.classList.toggle("danger", !!o.danger);
  el("confirm-cancel").textContent = o.cancelLabel || "Cancel";
  overlay.hidden = false;
  // destructive confirms focus the safe choice; routine ones the primary
  (o.danger ? el("confirm-cancel") : okBtn).focus();
  return new Promise((resolve) => {
    _confirmResolve = (val) => {
      _confirmResolve = null;
      overlay.hidden = true;
      resolve(val);
      // Restore focus after the caller has finished closing whatever
      // spawned the confirm, and only if the opener is still visible — a
      // discarded editor takes its focus target away with it.
      setTimeout(() => {
        if (_confirmResolve) return;   // a follow-up confirm owns focus now
        const op = _confirmOpener;
        _confirmOpener = null;
        if (op && op.focus && op.isConnected && op.offsetParent !== null) op.focus();
      }, 0);
    };
  });
}

function initConfirmDialog() {
  const overlay = el("confirm-overlay");
  const okBtn = el("confirm-ok");
  okBtn.addEventListener("click", () => _confirmResolve && _confirmResolve(true));
  el("confirm-cancel").addEventListener("click", () => _confirmResolve && _confirmResolve(false));
  el("confirm-close").addEventListener("click", () => _confirmResolve && _confirmResolve(false));
  overlay.addEventListener("mousedown", (ev) => {
    if (ev.target === overlay && _confirmResolve) {
      ev.preventDefault();   // don't let the browser refocus under the dialog
      _confirmResolve(false);
    }
  });
  // Capture phase so an open confirm behaves like a real modal: it owns
  // Escape/Tab/Enter, and every other key stops here so app-wide shortcuts
  // (undo, Ctrl+S, the OCR page keys) cannot act behind it. Native button
  // activation is a default action, so Enter/Space on a focused action
  // still works after stopImmediatePropagation.
  document.addEventListener("keydown", (ev) => {
    if (overlay.hidden || !_confirmResolve) return;
    if (ev.key === "Escape") {
      ev.preventDefault();
      ev.stopImmediatePropagation();
      _confirmResolve(false);
      return;
    }
    const ring = [el("confirm-close"), el("confirm-cancel"), okBtn];
    if (ev.key === "Tab") {
      const i = ring.indexOf(document.activeElement);
      ev.preventDefault();
      ev.stopImmediatePropagation();
      ring[(i + (ev.shiftKey ? -1 : 1) + ring.length) % ring.length].focus();
      return;
    }
    if (ev.key === "Enter" && !ring.includes(document.activeElement)) {
      // focus strayed outside the dialog: Enter takes the default action
      // (the safe choice on destructive confirms)
      ev.preventDefault();
      ev.stopImmediatePropagation();
      (okBtn.classList.contains("danger") ? el("confirm-cancel") : okBtn).click();
      return;
    }
    ev.stopImmediatePropagation();
  }, true);
}

// --- shared modal behaviour for .overlay > .win windows ----------------------
// The confirm dialog (#confirm-overlay) and the image lightbox (#img-lightbox)
// carry their own modal role + focus handling; EVERY OTHER overlay window gets
// it here, stamped once at boot and driven by the overlay's `hidden` attribute,
// so none of the scattered open/close call sites has to change. Each window then
// gets: role="dialog" + aria-modal + aria-labelledby (a screen reader announces
// it, by name, as modal), initial focus, a Tab focus-trap, focus restored to the
// opener on close, and `inert` on the app chrome behind it.
const _modalStack = [];
const _SELF_MANAGED_OVERLAYS = new Set(["confirm-overlay", "img-lightbox"]);
// The observer below runs a microtask after a dialog opens -- too late to read
// the opener from document.activeElement once a dialog has self-focused (e.g.
// auth -> email). So we remember the focus that PRECEDED the current one and use
// it as the opener when the dialog has already moved focus into itself.
let _priorFocus = null, _activeFocus = null;

function _overlayWin(overlay) { return overlay.querySelector(".win"); }

function _focusables(root) {
  return [...root.querySelectorAll(
    'a[href],button:not([disabled]),input:not([disabled]):not([type=hidden]),' +
    'select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])')]
    .filter((e) => e.getClientRects().length > 0);
}

// Disable everything behind the dialog: every top-level app section except the
// overlays themselves and the floating tooltip/popover.
function _setAppInert(on) {
  for (const c of document.body.children) {
    // keep #statusbar interactive so its role="status" aria-live region still
    // announces to a screen reader while a modal is open (it sits behind the
    // backdrop and the Tab-trap, so it is not reachable by mouse or keyboard).
    if (c.classList.contains("overlay") || c.id === "cad-tooltip"
        || c.id === "attn-pop" || c.id === "statusbar" || c.tagName === "SCRIPT") continue;
    if (on) c.setAttribute("inert", "");
    else c.removeAttribute("inert");
  }
}

function _overlayShown(overlay) {
  const win = _overlayWin(overlay);
  if (!win) return;                       // not a .win dialog (e.g. md-live-overlay)
  // if the dialog already grabbed focus, the true opener is the prior focus
  overlay._opener = win.contains(document.activeElement) ? _priorFocus : document.activeElement;
  _modalStack.push(overlay);
  _setAppInert(true);
  // Respect a dialog that already placed its own focus (e.g. auth -> email);
  // otherwise land on the first field, else the first non-close control.
  if (!win.contains(document.activeElement)) {
    const f = _focusables(win);
    const field = f.find((e) => /^(INPUT|SELECT|TEXTAREA)$/.test(e.tagName));
    const target = field || f.find((e) => !e.classList.contains("win-close")) || f[0];
    (target || win).focus?.();
  }
}

function _overlayHidden(overlay) {
  const i = _modalStack.indexOf(overlay);
  if (i < 0) return;                      // wasn't tracked as an open .win modal
  _modalStack.splice(i, 1);
  if (!_modalStack.length) _setAppInert(false);
  const op = overlay._opener; overlay._opener = null;
  if (op && op.isConnected && op.offsetParent !== null) op.focus?.();
}

function initOverlayModals() {
  // remember the previous focus target so _overlayShown can recover the real
  // opener even after a dialog synchronously self-focuses
  document.addEventListener("focusin", (ev) => {
    _priorFocus = _activeFocus; _activeFocus = ev.target;
  }, true);
  for (const overlay of document.querySelectorAll(".overlay")) {
    if (_SELF_MANAGED_OVERLAYS.has(overlay.id)) continue;
    const win = _overlayWin(overlay);
    if (!win) continue;
    if (!win.getAttribute("role")) win.setAttribute("role", "dialog");
    win.setAttribute("aria-modal", "true");
    const title = win.querySelector(".win-titlebar span, .ia-title");
    if (title) {
      if (!title.id) title.id = `${overlay.id}-arialabel`;
      win.setAttribute("aria-labelledby", title.id);
    }
    new MutationObserver(() => {
      if (overlay.hidden) _overlayHidden(overlay);
      else _overlayShown(overlay);
    }).observe(overlay, { attributes: true, attributeFilter: ["hidden"] });
  }
  // Tab focus-trap for the topmost tracked modal. Defer to the confirm dialog
  // and the lightbox when either is open -- they trap their own focus.
  document.addEventListener("keydown", (ev) => {
    if (ev.key !== "Tab" || !_modalStack.length) return;
    if (!el("confirm-overlay").hidden) return;
    const lb = document.getElementById("img-lightbox");
    if (lb && !lb.hidden) return;
    const win = _overlayWin(_modalStack[_modalStack.length - 1]);
    const f = win ? _focusables(win) : [];
    if (!f.length) return;
    const first = f[0], last = f[f.length - 1];
    if (!win.contains(document.activeElement)) { ev.preventDefault(); first.focus(); }
    else if (ev.shiftKey && document.activeElement === first) { ev.preventDefault(); last.focus(); }
    else if (!ev.shiftKey && document.activeElement === last) { ev.preventDefault(); first.focus(); }
  }, true);
}

// --- undo / redo -------------------------------------------------------------
// Every mutating action pushes an operation with its inverse. Client-side
// state (the checked map) is snapshot-restored; server-backed changes run
// their inverse call.

const history = { stack: [], ptr: 0 };

function pushOp(label, undoFn, redoFn, revert) {
  const id = logAction(label, revert);   // logAction mints the id from the freshest max
  history.stack.length = history.ptr;
  history.stack.push({ id, label, undoFn, redoFn });
  if (history.stack.length > 100) history.stack.shift();
  history.ptr = history.stack.length;
  updateHistoryButtons();
  return id;
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
  catch (e) { statusErr(`UNDO FAILED :: ${op.label}`); }
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
  catch (e) { statusErr(`REDO FAILED :: ${op.label}`); }
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
    () => restoreChecked(key, after),
    { kind: "checked", key, before });   // full snapshot -> cross-session revert
}

// --- persistent action log (History tab) -------------------------------------
// Every undo-tracked action is logged with a serializable "revert" descriptor
// so the History tab can revert it even across reloads (the in-memory undo
// closures can't be persisted). Stored in localStorage; clearable in Settings.
const ACTIONLOG_KEY = "whl_action_log_v1";
const ACTIONLOG_CAP = 1000;
let _actionLog = null;
let _opSeq = 0;

function actionLog() {
  if (_actionLog) return _actionLog;
  try { _actionLog = JSON.parse(localStorage.getItem(ACTIONLOG_KEY) || "[]"); }
  catch (e) { _actionLog = []; }
  if (!Array.isArray(_actionLog)) _actionLog = [];
  for (const r of _actionLog) if (typeof r.id === "number" && r.id > _opSeq) _opSeq = r.id;
  return _actionLog;
}
function saveActionLog() {
  try { localStorage.setItem(ACTIONLOG_KEY, JSON.stringify(actionLog())); } catch (e) {}
}
// canonical action type (drives the History colour) from the label's first word
const ACTION_TYPES = {
  edit: "edit", correct: "edit", bake: "edit",
  add: "add", check: "add", create: "add",
  uncheck: "remove", delete: "remove",
  repopulate: "repopulate", verify: "verify",
  attach: "scan", detach: "scan", manual: "source", source: "source",
};
function actionType(label) {
  const w = String(label || "").trim().split(/\s+/)[0].toLowerCase();
  return ACTION_TYPES[w] || "other";
}
function actionTargetKey(revert) {
  if (!revert) return "";
  if (revert.kind === "checked") return "checked:" + revert.key;
  if (revert.kind === "whl") return "whl:" + revert.idx;
  if (String(revert.kind || "").startsWith("manual")) return "manual:" + revert.id;
  if (revert.kind === "smartbake") {
    // a single-book bake targets that book; a grown group spans records and
    // has no single key (scGroupLog clears the tkey when it folds item #2)
    const it = (revert.items || []).length === 1 ? revert.items[0] : null;
    if (!it) return "";
    if (it.kind === "whl") return "whl:" + it.idx;
    if (it.kind === "checked") return "checked:" + it.key;
    if (it.kind === "manual-fields") return "manual:" + it.id;
    if (it.kind === "build-fields") return "build:" + it.id;
  }
  return "";
}
function logAction(label, revert) {
  // Re-read the persisted log so a second tab's appends aren't clobbered, and
  // mint the id from the freshest max (no cross-tab / cross-session id reuse).
  let stored = [];
  try {
    const a = JSON.parse(localStorage.getItem(ACTIONLOG_KEY) || "[]");
    if (Array.isArray(a)) stored = a;
  } catch (e) { /* ignore */ }
  const id = stored.reduce((m, r) => Math.max(m, r.id || 0), _opSeq) + 1;
  _opSeq = id;
  stored.push({
    id, ts: Date.now(), type: actionType(label), label: String(label || ""),
    tkey: actionTargetKey(revert), revert: revert || null, reverted: false,
  });
  if (stored.length > ACTIONLOG_CAP) stored.splice(0, stored.length - ACTIONLOG_CAP);
  _actionLog = stored;
  saveActionLog();
  if (typeof activeBottomTable === "function" && activeBottomTable() === "history") {
    renderBottomRows();
  }
  return id;
}

// --- persistence -----------------------------------------------------------
// Checked books, settings, and attention marks are cached in localStorage
// (fast, offline) AND written through to the server doc store, which is
// authoritative on load — so they are port-independent and sync-ready.

function checkedArray() {
  return [...state.checked.entries()].map(([k, v]) => [k, v]);
}

function saveChecked() {
  try { localStorage.setItem(LS_KEY, JSON.stringify(checkedArray())); } catch (e) {}
  pushClientState("checked");
}
function loadChecked() {
  try {
    const arr = JSON.parse(localStorage.getItem(LS_KEY) || "[]");
    state.checked = new Map(arr);
  } catch (e) { state.checked = new Map(); }
}

function saveSettings() {
  // preferences -> SETTINGS_KEY (and the server); per-device view state -> its
  // own local key, never pushed. state.settings stays whole in memory.
  const { prefs, view } = partitionSettings(state.settings);
  try {
    localStorage.setItem(SETTINGS_KEY, JSON.stringify(prefs));
    localStorage.setItem(VIEWSTATE_KEY, JSON.stringify(view));
  } catch (e) {}
  pushClientState("settings");
}

// --- UI scale (whole-interface zoom) -----------------------------------------
// A Settings > Appearance control and Ctrl/Cmd +/- (reset with Ctrl/Cmd 0).
// Applied as a CSS zoom on the root, so it scales the custom chrome too and
// works both in Electron and the dev browser. The native menu is removed
// (main.js), so there is no built-in zoom to fight.
const UI_SCALE_MIN = 0.7, UI_SCALE_MAX = 2.0, UI_SCALE_STEP = 0.1;
const UI_SCALES = [0.7, 0.8, 0.9, 1.0, 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8, 1.9, 2.0];

function applyUiScale() {
  const s = state.settings.uiScale || 1;
  document.documentElement.style.zoom = String(s);
  // The shell is height:100vh, but `zoom` renders it at 100vh * s, which would
  // push it past the window and raise a page scrollbar. Expose the factor so
  // the body height can divide it back out (see body { height } in style.css).
  document.documentElement.style.setProperty("--ui-scale", String(s));
  const sel = el("ui-scale-select");
  if (sel) sel.value = String(s);
  // CSS zoom changes the width available to fixed-layout tables without
  // reliably producing a window resize event. Re-run their stretch-column
  // calculation after the browser has applied the new scale.
  scheduleTableChromeRefresh();
}
function setUiScale(v) {
  state.settings.uiScale =
    Math.round(Math.min(UI_SCALE_MAX, Math.max(UI_SCALE_MIN, v)) * 100) / 100;
  applyUiScale();
  saveSettings();
}
function nudgeUiScale(d) { setUiScale((state.settings.uiScale || 1) + d); }

function onUiScaleKey(ev) {
  if (!(ev.ctrlKey || ev.metaKey) || ev.altKey) return;
  const k = ev.key;
  if (k === "=" || k === "+") nudgeUiScale(UI_SCALE_STEP);
  else if (k === "-" || k === "_") nudgeUiScale(-UI_SCALE_STEP);
  else if (k === "0") setUiScale(1);
  else return;
  ev.preventDefault();                       // stop the browser's own zoom
  status("UI SCALE :: " + Math.round((state.settings.uiScale || 1) * 100) + "%");
}
function loadSettings() {
  try {
    const s = JSON.parse(localStorage.getItem(SETTINGS_KEY) || "{}");
    const v = JSON.parse(localStorage.getItem(VIEWSTATE_KEY) || "{}");
    // legacy caches kept view state inside SETTINGS_KEY; apply it, then let the
    // dedicated view-state store win. The next saveSettings re-partitions both.
    state.settings = Object.assign(state.settings, s, v);
  } catch (e) { /* keep defaults */ }
  normalizeSettings();
}

// defaults + version migrations for the settings object, applied whether it
// came from localStorage or the server
function normalizeSettings() {
  state.settings.checkedCols = state.settings.checkedCols || {};
  if (!state.settings.searchCons || typeof state.settings.searchCons !== "object")
    state.settings.searchCons = { author: true, year: true };
  if (!state.settings.sets || typeof state.settings.sets !== "object")
    state.settings.sets = {};
  state.settings.expandSets = !!state.settings.expandSets;
  state.settings.hideVolTitles = !!state.settings.hideVolTitles;
  state.settings.setsBackfilled = !!state.settings.setsBackfilled;
  state.settings.groupMetadataBackfilled = !!state.settings.groupMetadataBackfilled;
  if (!["sets", "author", "category", "date"].includes(state.settings.publishGroup))
    state.settings.publishGroup = "sets";
  state.settings.publishSidebarCollapsed = !!state.settings.publishSidebarCollapsed;
  if (!["tesseract", "mistral", "claude", "textract"].includes(state.settings.ocrService))
    state.settings.ocrService = "tesseract";
  state.settings.textAnalysisService = "configured";
  if (!WB_PHASES.includes(state.settings.workbenchPhase))
    state.settings.workbenchPhase = "record";
  state.settings.jobsDrawerOpen = !!state.settings.jobsDrawerOpen;
  if (!state.settings.copyrightSources || typeof state.settings.copyrightSources !== "object")
    state.settings.copyrightSources = { cprs: true, nypl: false };
  state.settings.cloudSearchUrl = state.settings.cloudSearchUrl || "";
  if (!state.settings.dbUrls || typeof state.settings.dbUrls !== "object")
    state.settings.dbUrls = {};
  state.settings.colVis = state.settings.colVis || {};
  state.settings.colWidths = state.settings.colWidths || {};
  // migrate the old single-table column setting
  if (Object.keys(state.settings.checkedCols).length &&
      !state.settings.colVis.checked) {
    state.settings.colVis.checked = state.settings.checkedCols;
  }
  state.settings.srcFilter = state.settings.srcFilter || "ALL";
  state.settings.dlFilter = state.settings.dlFilter || "ALL";
  // v2.1 had a single font applied to the whole UI; migrate a saved sans
  // value to the interface font
  const f = state.settings.font || "";
  if (/^(Tahoma|Verdana)/.test(f) && !state.settings.fontUi) {
    state.settings.fontUi = f;
    state.settings.font = "";
  }
  state.settings.maxRows =
    Math.max(50, Math.min(5000, parseInt(state.settings.maxRows, 10) || 400));
  const _uisc = Number(state.settings.uiScale);
  state.settings.uiScale =
    (Number.isFinite(_uisc) && _uisc >= UI_SCALE_MIN && _uisc <= UI_SCALE_MAX)
      ? Math.round(_uisc * 100) / 100 : 1;
  let _srm = parseInt(state.settings.scanRecentMin, 10);
  if (!Number.isFinite(_srm)) _srm = 30;         // 0 = no recency filter
  state.settings.scanRecentMin = Math.max(0, Math.min(1440, _srm));
  // Experimental: interface sharpening (off by default; amount is a 0-100 %)
  state.settings.expSharpen = !!state.settings.expSharpen;
  let _esa = parseInt(state.settings.expSharpenAmount, 10);
  if (!Number.isFinite(_esa)) _esa = 35;         // a gentle default once enabled
  state.settings.expSharpenAmount = Math.max(0, Math.min(100, _esa));
  // v2.6 inserted Subtitle/Vol/Ed into the master-list bottom table; its
  // saved column settings are keyed by index (c0..cN), so pre-v2.6 keys
  // must shift to keep pointing at the same columns
  // (old: Title,Author,Year,Publisher,City,Categories
  //  new: Title,Subtitle,Author,Year,Vol,Ed,Publisher,City,Categories)
  if (!state.settings.bchColsV26) {
    const remap = { c1: "c2", c2: "c3", c3: "c6", c4: "c7", c5: "c8" };
    for (const store of [state.settings.colVis, state.settings.colWidths]) {
      const old = store["b-ch"];
      if (old && Object.keys(old).length) {
        const out = {};
        for (const [k, v] of Object.entries(old)) out[remap[k] || k] = v;
        store["b-ch"] = out;
      }
    }
    state.settings.bchColsV26 = true;
    saveSettings();
  }
}


// --- client-state sync (localStorage cache + authoritative server copy) ----------
// The three blobs (checked books / settings / attention marks) write through
// to /api/client_state so they survive a port change and can sync to the
// cloud later. Credential fields are excluded from this general settings blob;
// Mistral uses the dedicated private profile_secrets sync path instead.

let clientStateReady = false;   // gates write-through until the load-sync ran
const _csPending = {};          // kind -> true, coalesced
let _csTimer = null;
let _csBulk = 0;                // >0 during a bulk op: accumulate, flush once at end

function clientStateBlob(kind) {
  if (kind === "checked") return checkedArray();
  if (kind === "settings") return partitionSettings(state.settings).prefs;
  if (kind === "attention") return state.attn || {};
  return null;
}

// debounced write-through of one or more changed blobs
function pushClientState(kind) {
  if (!clientStateReady) return;   // never clobber the server before load-sync
  _csPending[kind] = true;
  // During a bulk op (e.g. a multi-book scan) each item mutates the checked map;
  // without this a whole ~1MB client_state PUT would fire per item whenever the
  // 700ms debounce lands between items. Accumulate here and flush once when the
  // batch ends (withBulkClientState), collapsing N PUTs into one.
  if (_csBulk > 0) return;
  clearTimeout(_csTimer);
  _csTimer = setTimeout(flushClientState, 700);
}

// Run an async op with per-item client_state pushes suppressed, flushing a
// single coalesced PUT when it finishes (even if it throws). Safe to nest.
async function withBulkClientState(fn) {
  _csBulk++;
  try {
    return await fn();
  } finally {
    _csBulk--;
    if (_csBulk === 0 && Object.keys(_csPending).length) flushClientState();
  }
}

async function flushClientState() {
  const kinds = Object.keys(_csPending);
  if (!kinds.length) return;
  for (const k of kinds) delete _csPending[k];
  const body = {};
  for (const k of kinds) body[k] = clientStateBlob(k);
  try {
    await fetch("/api/client_state", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  } catch (e) { /* offline: the localStorage cache still holds it */ }
}

// When the same book is checked in both the local cache and the server copy,
// keep whichever entry carries more work (scan results, verify state, checks).
function richerEntry(a, b) {
  if (!a) return b;
  if (!b) return a;
  const score = (e) => (e && typeof e === "object")
    ? (e.scans ? 4 : 0) + (e.verify ? 2 : 0) + (e.checks ? 1 : 0) : 0;
  const sa = score(a), sb = score(b);
  if (sa !== sb) return sa > sb ? a : b;
  try { return JSON.stringify(b).length >= JSON.stringify(a).length ? b : a; }
  catch (e) { return b; }
}

// On load the SERVER copy is authoritative. If the server has nothing yet
// (first run of this build), seed it from whatever localStorage held so no
// existing work is lost. Returns true if server state was adopted (the
// caller re-applies theme/fonts and re-renders).
async function syncClientStateOnLoad() {
  let server = null;
  try { server = await (await fetch("/api/client_state")).json(); }
  catch (e) { clientStateReady = true; return false; }   // offline: keep local
  const hasServer = server &&
    (server.checked || server.settings || server.attention);
  if (hasServer) {
    // Adopt-by-MERGE, not replace: union the server copy with the local cache
    // so a near-empty client can never wipe a fuller set (and vice versa). If
    // the local cache turns out to be fuller (the server was clobbered), heal
    // the server by pushing the merged result back.
    let healChecked = false;
    if (Array.isArray(server.checked)) {
      const merged = new Map(state.checked);       // local cache (may be fuller)
      for (const [k, v] of server.checked) {
        merged.set(k, richerEntry(merged.get(k), v));
      }
      // heal vs the count of DISTINCT server keys, so a duplicate-laden server
      // array (only reachable via external corruption) is repaired, not ignored
      const serverDistinct = new Set(server.checked.map((p) => p[0])).size;
      healChecked = merged.size > serverDistinct;
      state.checked = merged;
      try { localStorage.setItem(LS_KEY, JSON.stringify(checkedArray())); } catch (e) {}
    }
    let migratePersistentView = false;
    if (server.settings && typeof server.settings === "object") {
      // adopt the server's PREFERENCES; per-device view state stays whatever this
      // machine holds (the server no longer carries it, but old data might)
      const incoming = {};
      for (const k of Object.keys(server.settings))
        if (!VIEW_STATE_KEYS.has(k)) incoming[k] = server.settings[k];
      state.settings = Object.assign(state.settings, incoming);
      normalizeSettings();
      // wizardDone and colWidths used to be browser-origin view state. Existing
      // settings prove this is not a first launch, even if the old origin-local
      // wizard flag is unavailable on today's sidecar port. Migrate both values
      // into the port-independent settings file.
      if (!("wizardDone" in server.settings) &&
          (state.settings.wizardDone === true || Object.keys(server.settings).length > 0)) {
        state.settings.wizardDone = true;
        migratePersistentView = true;
      }
      if (!("colWidths" in server.settings) &&
          Object.keys(state.settings.colWidths || {}).length > 0) {
        migratePersistentView = true;
      }
      syncYearFilterInputs();   // reflect the (local) year-range into the toolbar
      syncSearchConsCheckboxes();
      try {
        const { prefs, view } = partitionSettings(state.settings);
        localStorage.setItem(SETTINGS_KEY, JSON.stringify(prefs));
        localStorage.setItem(VIEWSTATE_KEY, JSON.stringify(view));
      } catch (e) {}
    }
    if (server.attention && typeof server.attention === "object") {
      // Attention marks (unlike checked books) are a low-stakes set that
      // supports removal, so the server copy is authoritative on load — a
      // union would resurrect marks the user deliberately cleared.
      state.attn = server.attention;
      try { localStorage.setItem(ATTN_KEY, JSON.stringify(state.attn)); } catch (e) {}
    }
    clientStateReady = true;
    if (healChecked) pushClientState("checked");
    if (migratePersistentView) pushClientState("settings");
    return true;
  }
  // seed the server from local, then allow write-through
  try {
    await fetch("/api/client_state", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        checked: checkedArray(),
        settings: state.settings,
        attention: state.attn || {},
      }),
    });
  } catch (e) { /* will retry on the next change */ }
  clientStateReady = true;
  return false;
}

// --- Home ---------------------------------------------------------------------
// Recent activity comes from the server's append-only feed; pending tasks are
// derived from data already in memory, so they need no store of their own.

// Every write carries who made it. A signed-in account's display name wins
// (the server also knows the session and prefers it); the Settings name covers
// working locally.
function installActorHeader() {
  const raw = window.fetch.bind(window);
  window.fetch = (input, init) => {
    const method = String((init && init.method) ||
      (input && input.method) || "GET").toUpperCase();
    const who = authState.displayName || state.settings.userName;
    if (method !== "GET" && method !== "HEAD" && who) {
      init = { ...(init || {}) };
      init.headers = new Headers(init.headers || (input && input.headers) || {});
      init.headers.set("X-WHL-Actor", who);
    }
    return raw(input, init);
  };
}

// --- cloud account -------------------------------------------------------------
// A real Supabase user. The server owns the session (tokens never reach the
// browser); this side only asks who is signed in and shows the door.

const authState = { cloud: false, signedIn: false, email: "", displayName: "" };

async function refreshAuthStatus() {
  try {
    const r = await (await fetch("/api/auth/status")).json();
    authState.cloud = !!r.cloud;
    authState.signedIn = !!r.signed_in;
    authState.email = r.email || "";
    authState.displayName = r.display_name || "";
  } catch (e) { /* server unreachable; keep whatever we knew */ }
  renderAccountState();
}

function renderAccountState() {
  // the wizard's account step mirrors the same signed-in state
  if (typeof wizAccountState === "function" && !el("wizard-overlay").hidden) wizAccountState();
  const s = el("set-account-state"), b = el("set-account-btn");
  if (!s || !b) return;
  if (authState.signedIn) {
    s.textContent = `${authState.displayName || authState.email} (${authState.email})`;
    b.textContent = "Sign out";
  } else {
    s.textContent = authState.cloud ? "Not signed in"
      : "Not signed in — set the Supabase URL and anon key under Sync first";
    b.textContent = "Sign in…";
  }
}

function authMsg(text, ok) {
  const m = el("auth-msg");
  m.textContent = text || "";
  m.hidden = !text;
  m.classList.toggle("ok", !!ok);
}

function showAuthOverlay() {
  setAuthMode(false);
  authMsg("");
  el("auth-overlay").hidden = false;
  el("auth-email").focus();
}

function hideAuthOverlay() {
  el("auth-overlay").hidden = true;
}

// one dialog, two modes: sign in, or create an account (adds a name field)
let authSignup = false;
function setAuthMode(signup) {
  authSignup = signup;
  el("auth-title").textContent = signup ? "Create account" : "Sign in";
  el("auth-submit").textContent = signup ? "Create account" : "Sign in";
  el("auth-mode").textContent = signup ? "I have an account…" : "Create account…";
  el("auth-pass").autocomplete = signup ? "new-password" : "current-password";
  for (const n of document.querySelectorAll(".auth-signup-only")) n.hidden = !signup;
}

async function submitAuth() {
  const email = el("auth-email").value.trim();
  const password = el("auth-pass").value;
  if (!email || !password) { authMsg("email and password are both required"); return; }
  el("auth-submit").disabled = true;
  authMsg(authSignup ? "Creating account…" : "Signing in…", true);
  try {
    const body = { email, password };
    if (authSignup) body.display_name = el("auth-name").value.trim();
    const r = await fetch(authSignup ? "/api/auth/signup" : "/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok || !j.ok) { authMsg(j.error || `sign-in failed (HTTP ${r.status})`); return; }
    if (j.confirm) {   // account made; the project wants the email link clicked first
      setAuthMode(false);
      authMsg("Account created — click the link in your email, then sign in here.", true);
      return;
    }
    hideAuthOverlay();
    await refreshAuthStatus();
    status(`SIGNED IN :: ${j.display_name || j.email}`);
    loadActivity();          // the feed switches to the shared cloud view
  } catch (e) {
    authMsg("could not reach the server");
  } finally {
    el("auth-submit").disabled = false;
  }
}

function initAuth() {
  el("auth-win").addEventListener("submit", (ev) => { ev.preventDefault(); submitAuth(); });
  el("auth-mode").onclick = () => { setAuthMode(!authSignup); authMsg(""); };
  el("auth-close").onclick = hideAuthOverlay;
  // backdrop click dismisses, like every other form modal (same as the close X)
  el("auth-overlay").addEventListener("mousedown", (ev) => {
    if (ev.target === el("auth-overlay")) hideAuthOverlay();
  });
  el("auth-skip").onclick = () => {
    state.settings.authPromptDismissed = true;
    saveSettings();
    hideAuthOverlay();
  };
  // Escape lives in init()'s exclusive overlay chain — a handler here would
  // also close the Settings window underneath in the same keypress.
  el("set-account-btn").onclick = async () => {
    if (authState.signedIn) {
      try { await fetch("/api/auth/logout", { method: "POST" }); } catch (e) {}
      await refreshAuthStatus();
      status("SIGNED OUT");
      loadActivity();        // back to the local feed
    } else {
      showAuthOverlay();
    }
  };
  refreshAuthStatus();
}

// Ask once at startup, and only when it could work: cloud configured, no
// session, and the user hasn't said "work locally". Called AFTER the server's
// client_state has been adopted: before that, authPromptDismissed is a local
// default, and a "Work locally" click would be dropped by the write-through
// gate and then overwritten by the sync.
function maybeAuthPrompt() {
  refreshAuthStatus().then(() => {
    if (!el("wizard-overlay").hidden) return;   // the wizard's account step covers it
    if (authState.cloud && !authState.signedIn && !state.settings.authPromptDismissed) {
      showAuthOverlay();
    }
  });
}

// --- first-run setup wizard ------------------------------------------------------
// Shown once, on the desktop shell's first launch (the installer stays dumb;
// the app is where keys, Tesseract and the big optional downloads make sense).
// Re-openable any time from Help > Setup guide. Every step is skippable, and
// everything it sets remains available from the Settings window.

// No cloud-keys step: the app ships knowing its own cloud (the server bakes
// in the project URL + public anon key), so accounts just work. The service
// key is an owner concern and stays out of this normal-user flow.
const WIZ_STEPS = [
  ["welcome", "Welcome"],
  ["account", "Profile"],
  ["services", "Text tools"],
  ["db", "Offline search"],
  ["done", "Ready"],
];
let wizStep = 0;
let wizDbTimer = null;
let wizSecretsLoad = null;

function showWizard() {
  wizStep = 0;
  // Start this while the user reads the first steps. When they reach Text
  // tools, existing keys can be shown without a visible loading pause.
  wizSecretsLoad = hydrateSecrets();
  el("wizard-overlay").hidden = false;
  wizRender();
}

function closeWizard(markDone) {
  if (markDone) {
    state.settings.wizardDone = true;
    saveSettings();
  }
  clearTimeout(wizDbTimer);
  wizDbTimer = null;
  el("wizard-overlay").hidden = true;
}

// Values are committed when leaving a step, so Back/Next/Finish all keep work.
// Credentials use /api/secrets; saveSettings intentionally strips them.
async function wizCommit() {
  const step = WIZ_STEPS[wizStep][0];
  if (step === "account") {
    state.settings.userName = el("wiz-name").value.trim().slice(0, 60);
    const un = el("set-user-name");
    if (un) un.value = state.settings.userName;
    saveSettings();
  } else if (step === "services") {
    const fields = [["wiz-mistral", "mistralKey"], ["wiz-deepseek", "aiKey"]];
    const updates = {};
    for (const [id, key] of fields) {
      const value = el(id).value.trim();
      if (value !== String(state.settings[key] || "")) updates[key] = value;
    }
    if (!Object.keys(updates).length) return true;
    const msg = el("wiz-keys-msg");
    msg.textContent = "Saving API keys…";
    msg.classList.remove("err");
    try {
      await persistSecrets(updates);
      msg.textContent = "API keys saved.";
    } catch (e) {
      msg.textContent = "Could not save API keys. Try again, or leave them blank and continue.";
      msg.classList.add("err");
      statusErr("SETUP :: COULD NOT SAVE API KEYS");
      return false;
    }
  } else {
    return true;
  }
  return true;
}

function wizRender() {
  const [step, title] = WIZ_STEPS[wizStep];
  for (const p of document.querySelectorAll("#wizard-body .wiz-pane")) {
    p.hidden = p.dataset.step !== step;
  }
  el("wizard-title").textContent = title;
  el("wizard-step").textContent = `Step ${wizStep + 1} of ${WIZ_STEPS.length}`;
  el("wizard-progress").value = wizStep + 1;
  el("wizard-back").disabled = wizStep === 0;
  el("wizard-next").textContent = wizStep === WIZ_STEPS.length - 1 ? "Start" : "Next";
  el("wizard-skip").hidden = wizStep === WIZ_STEPS.length - 1;
  clearTimeout(wizDbTimer);
  wizDbTimer = null;
  if (step === "account") {
    el("wiz-name").value = state.settings.userName || "";
    wizAccountState();
  } else if (step === "services") {
    const fields = [["wiz-mistral", "mistralKey"], ["wiz-deepseek", "aiKey"]];
    for (const [id, key] of fields) {
      const input = el(id);
      delete input.dataset.edited;
      input.value = state.settings[key] || "";
    }
    el("wiz-keys-msg").textContent = "";
    // A slow profile refresh must not overwrite a key the user has started
    // typing. The input listener in initWizard marks touched fields.
    (wizSecretsLoad || hydrateSecrets()).then((secrets) => {
      if (WIZ_STEPS[wizStep][0] !== "services") return;
      for (const [id, key] of fields) {
        const input = el(id);
        if (!input.dataset.edited) input.value = secrets[key] || "";
      }
    });
    wizCheckTesseract();
  } else if (step === "db") {
    wizDbTick();
  }
}

function wizAccountState() {
  const s = el("wiz-account-state"), b = el("wiz-signin");
  if (authState.signedIn) {
    s.textContent = `${authState.displayName || authState.email}`;
    b.textContent = "Signed in";
    b.disabled = true;
  } else {
    s.textContent = "Not signed in";
    b.textContent = "Sign in…";
    b.disabled = false;
  }
}

async function wizCheckTesseract() {
  const s = el("wiz-tess-state"), link = el("wiz-tess-link");
  s.textContent = "Checking for Tesseract…";
  s.className = "tool-label";
  link.hidden = true;
  try {
    const r = await (await fetch("/api/ocr/tesseract")).json();
    if (r.installed) {
      s.textContent = `Tesseract found — ${r.version || r.path}`;
      s.classList.add("wiz-tess-ok");
    } else {
      s.textContent = "Tesseract not found (local OCR unavailable until installed)";
      s.classList.add("wiz-tess-no");
      link.hidden = false;
    }
  } catch (e) {
    s.textContent = "Could not check for Tesseract";
  }
}

const wizBytes = (n) => !n ? "" : n >= 1 << 30 ? (n / (1 << 30)).toFixed(1) + " GB"
  : n >= 1 << 20 ? Math.round(n / (1 << 20)) + " MB" : Math.round(n / 1024) + " KB";

async function wizDbTick() {
  let data;
  try {
    data = await (await fetch("/api/db/status")).json();
  } catch (e) {
    el("wiz-db-list").innerHTML = `<span class="tool-label">Could not read database status</span>`;
    return;
  }
  const rows = [];
  let polling = false;
  for (const [name, t] of Object.entries(data.targets || {})) {
    let stateHtml;
    const job = t.job;
    if (job && job.status === "downloading") {
      polling = true;
      const pct = job.total ? Math.round(100 * job.downloaded / job.total) : 0;
      stateHtml = `<span class="wiz-db-track"><span class="wiz-db-bar" style="width:${pct}%"></span></span>` +
        `<span class="tool-label">${pct}%</span>`;
    } else if (t.present) {
      stateHtml = `<span class="tool-label wiz-tess-ok">downloaded (${wizBytes(t.size)})</span>`;
    } else if (!t.url) {
      stateHtml = `<span class="tool-label">no URL set — see Settings &gt; Integrations</span>`;
    } else {
      stateHtml = `<button class="cad-btn" data-wizdl="${esc(name)}" type="button">Download</button>`;
    }
    rows.push(`<div class="wiz-db-row"><span class="tool-label">${esc(t.label || name)}</span>${stateHtml}</div>`);
  }
  el("wiz-db-list").innerHTML = rows.join("") ||
    `<span class="tool-label">No downloadable databases configured</span>`;
  if (polling && !el("wizard-overlay").hidden && WIZ_STEPS[wizStep][0] === "db") {
    wizDbTimer = setTimeout(wizDbTick, 1500);
  }
}

function initWizard() {
  el("wizard-next").onclick = async () => {
    const button = el("wizard-next");
    button.disabled = true;
    try {
      if (!(await wizCommit())) return;
      if (wizStep >= WIZ_STEPS.length - 1) { closeWizard(true); return; }
      wizStep++;
      wizRender();
    } finally {
      button.disabled = false;
    }
  };
  el("wizard-back").onclick = async () => {
    const button = el("wizard-back");
    button.disabled = true;
    try {
      if (!(await wizCommit())) return;
      if (wizStep > 0) { wizStep--; wizRender(); }
    } finally {
      button.disabled = false;
    }
  };
  el("wizard-skip").onclick = async () => { await wizCommit(); closeWizard(true); };
  el("wiz-signin").onclick = () => showAuthOverlay();   // z 62, above the wizard
  for (const id of ["wiz-mistral", "wiz-deepseek"])
    el(id).addEventListener("input", () => { el(id).dataset.edited = "1"; });
  el("wiz-db-list").addEventListener("click", async (ev) => {
    const b = ev.target.closest("[data-wizdl]");
    if (!b) return;
    b.disabled = true;
    try {
      await fetch("/api/db/download", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ names: [b.dataset.wizdl] }),
      });
    } catch (e) { /* the next tick shows whatever happened */ }
    wizDbTick();
  });
}

// auto-show only in the desktop shell: a fresh install is the moment the guide
// is for. In a browser the server may hold years of state, and Help > Setup
// guide is always there.
function maybeWizard() {
  const d = window.whlDesktop;
  if (d && d.isDesktop && !state.settings.wizardDone) showWizard();
}

const homeState = { events: [], loaded: false, expanded: new Set() };

async function loadActivity() {
  loadReviews().then(renderHome);   // the review count rides the same visit
  try {
    const r = await (await fetch(
      "/api/activity?limit=" + (state.settings.historyLimit || 300))).json();
    homeState.events = r.ok ? r.events : [];
  } catch (e) { homeState.events = []; }
  homeState.loaded = true;
  renderHome();
}

// "Andrew Miller added 5 books to Checked Books": consecutive events by the same
// actor doing the same thing collapse into one line, if they happened close
// together. Anything further apart is a separate session and stays separate.
const GROUP_GAP_MS = 30 * 60 * 1000;

function groupActivity(events) {
  const out = [];
  for (const e of events) {                    // newest first
    const at = Date.parse(e.ts) || 0;
    const last = out[out.length - 1];
    if (last && last.actor === e.actor && last.verb === e.verb &&
        last.subject === e.subject && Math.abs(last.oldest - at) <= GROUP_GAP_MS) {
      last.n += e.n || 1;
      last.oldest = at;
      if (last.items.length < 40) last.items.push(e);   // enough for the expansion
      continue;
    }
    out.push({ actor: e.actor, verb: e.verb, subject: e.subject,
               n: e.n || 1, at, oldest: at, items: [e] });
  }
  return out;
}

function relTime(ms) {
  const s = Math.max(0, (Date.now() - ms) / 1000);
  if (s < 90) return "just now";
  const m = s / 60;
  if (m < 60) return Math.round(m) + " min ago";
  const h = m / 60;
  if (h < 24) return Math.round(h) + " h ago";
  const d = h / 24;
  if (d < 30) return Math.round(d) + " d ago";
  return new Date(ms).toISOString().slice(0, 10);
}

const relIso = (iso) => { const t = Date.parse(iso || ""); return isNaN(t) ? "" : relTime(t); };

// "2026-07-10 14:32" in local time — the expanded per-event view is exact
function exactTime(ms) {
  const d = new Date(ms);
  const p = (x) => String(x).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ` +
         `${p(d.getHours())}:${p(d.getMinutes())}`;
}

// "Checked Books" is a place you add to; every other subject is a bare singular
// noun ("book", "manual entry") that takes an article or a plural.
const article = (w) => (/^[aeiou]/i.test(w) ? "an " : "a ");
const pluralize = (w) => (w.endsWith("y") ? w.slice(0, -1) + "ies" : w + "s");

function activityPhrase(g) {
  if (g.subject === "Checked Books") {
    const prep = g.verb === "removed" ? "from" : "to";   // never "removed ... to"
    return `${g.verb} ${g.n} book${g.n === 1 ? "" : "s"} ${prep} Checked Books`;
  }
  if (g.subject === "the cloud") return `${g.verb} ${g.subject}`;   // "signed in to the cloud"
  return g.n === 1
    ? `${g.verb} ${article(g.subject)}${g.subject}`
    : `${g.verb} ${g.n} ${pluralize(g.subject)}`;
}

// Everything here is computed from data the app already holds.
function progressSummary() {
  const builds = Object.values(state.builds || {});
  const drafts = builds.filter((b) => b.status === "draft");
  const ready = builds.filter((b) => b.status === "ready").length;
  // a source is settled once a verified entry has been built from it
  const srcPending = approvedSources()
    .filter((s) => sourceBuildStatus(s) !== "done").length;
  // catalog-side marks (rows + the attn map) live in the Catalogs tab;
  // marked builds live in the Editor's Pending queue — kept apart so the
  // attention tile can land where its items actually are
  const attnCat = Object.keys(state.attn || {}).length +
    [...(state.rowsById || new Map()).values()].filter((r) => r.attention).length;
  const attnEd = builds.filter((b) => b.attention && b.status !== "uploaded").length;
  const openReviews = Object.values(reviewsState.items || {})
    .filter((r) => r.status === "open").length;
  return { drafts, ready, srcPending, attnCat, attnEd, openReviews };
}

const HOME_DRAFTS_SHOWN = 4;

function renderHome() {
  const prog = el("home-progress");
  const feed = el("home-activity");
  if (!prog || !feed) return;

  const p = progressSummary();
  // one line per metric, count first, breakdown right-aligned and muted —
  // a status readout in the app's row idiom, not a dashboard of tiles
  const row = (n, label, act, detail) =>
    `<button class="home-row" ${act}>` +
      `<span class="hr-n">${n}</span>` +
      `<span class="hr-l">${esc(label)}</span>` +
      (detail ? `<span class="hr-d">${esc(detail)}</span>` : "") +
    `</button>`;
  const inEditor = p.drafts.length + p.ready;
  const attn = p.attnCat + p.attnEd;
  let html =
    row(inEditor, inEditor === 1 ? "entry in the workbench" : "entries in the workbench",
        `data-gotab="workbench"`, inEditor ? `${p.drafts.length} draft · ${p.ready} to publish` : "") +
    row(p.srcPending, p.srcPending === 1 ? "PDF source pending verification"
        : "PDF sources pending verification", `data-gotab="workbench"`) +
    row(attn, attn === 1 ? "item marked for attention"
        : "items marked for attention",
        `data-gotab="${p.attnCat || !p.attnEd ? "checked" : "workbench"}"`,
        p.attnCat && p.attnEd ? `${p.attnCat} catalog · ${p.attnEd} workbench` : "") +
    row(p.openReviews, p.openReviews === 1 ? "item awaiting review"
        : "items awaiting review", `data-review="1"`);

  // the freshest few drafts, so unfinished work is one click away
  const drafts = p.drafts.slice()
    .sort((a, b) => (b.updated_at || "").localeCompare(a.updated_at || ""));
  const shown = drafts.slice(0, HOME_DRAFTS_SHOWN);
  if (shown.length) {
    html += `<div class="home-h home-h-sub">Drafts</div>` +
      shown.map((b) => `<button class="home-draft" data-draft="${esc(b.id)}"
          data-tip="Open this draft in the Workbench">` +
        `<span class="hd-t">${esc(b.title) || "<em>(untitled)</em>"}</span>` +
        `<span class="hd-meta">${esc(b.authors || "")}${b.authors && b.year ? " · " : ""}${esc(b.year || "")}</span>` +
        `<span class="hd-when">${esc(relIso(b.updated_at))}</span></button>`).join("");
    if (drafts.length > shown.length)
      html += `<button class="home-more" data-gotab="workbench">` +
        `${drafts.length - shown.length} more in the workbench…</button>`;
  }
  prog.innerHTML = html;

  const users = el("home-users");
  if (!homeState.loaded) {
    feed.innerHTML = `<div class="empty">Loading …</div>`;
    if (users) users.innerHTML = `<div class="empty">Loading …</div>`;
    return;
  }

  // a group expands into its member events: exact local time + what exactly
  const groups = groupActivity(homeState.events).slice(0, 12);
  const gkey = (g) => `${g.actor}|${g.verb}|${g.subject}|${g.at}`;
  feed.innerHTML = groups.length
    ? groups.map((g) => {
        const k = gkey(g);
        const open = homeState.expanded.has(k);
        const det = !open ? "" : `<div class="home-act-det">` +
          g.items.map((e) => `<div class="had-row">` +
            `<span class="had-ts">${esc(exactTime(Date.parse(e.ts) || 0))}</span>` +
            `<span class="had-txt">${esc(e.detail ||
              activityPhrase({ verb: e.verb, subject: e.subject, n: e.n || 1 }))}</span>` +
            `</div>`).join("") + `</div>`;
        return `<div class="home-act${open ? " open" : ""}" data-gk="${esc(k)}">` +
          `<span class="home-act-arrow">${open ? "&#9662;" : "&#9656;"}</span>` +
          `<span class="home-who">${esc(g.actor)}</span> ` +
          `<span class="home-what">${esc(activityPhrase(g))}</span>` +
          `<span class="home-when">${esc(relTime(g.at))}</span></div>` + det;
      }).join("")
    : `<div class="empty">No activity recorded yet</div>`;

  // everyone the feed has seen, newest first; your own name is always present
  if (users) {
    const me = (state.settings.userName || "").trim();
    const seen = new Map();
    for (const e of homeState.events) {
      const who = String(e.actor || "").trim() || "Unnamed user";
      const at = Date.parse(e.ts) || 0;
      const m = seen.get(who) || { n: 0, last: 0 };
      m.n += e.n || 1;
      if (at > m.last) m.last = at;
      seen.set(who, m);
    }
    if (me && !seen.has(me)) seen.set(me, { n: 0, last: 0 });
    const list = [...seen.entries()].sort((a, b) => b[1].last - a[1].last);
    users.innerHTML = list.length
      ? list.map(([who, m]) => `<div class="home-user">` +
          `<span class="hu-name">${esc(who)}</span>` +
          (who === me ? `<span class="hu-you">you</span>` : "") +
          `<span class="hu-meta">${m.n
            ? `${m.n} ${m.n === 1 ? "change" : "changes"}`
            : "no changes yet"}</span>` +
          `<span class="hu-when">${m.last ? esc(relTime(m.last)) : ""}</span>` +
          `</div>`).join("")
      : `<div class="empty">No contributors recorded yet</div>`;
  }

  // the review queue as an inline pane (the overlay window still exists too)
  const hcb = el("home-review-resolved");
  if (hcb) hcb.checked = reviewsState.showResolved;
  renderReviewsInto(el("home-reviews"));
}

function initHome() {
  // the version number is stated once, in the title bar markup; the home
  // page wordmark mirrors it so the two can never disagree
  el("home-ver").textContent = el("tb-meta").textContent;
  el("home-progress").addEventListener("click", (ev) => {
    const d = ev.target.closest("[data-draft]");
    if (d) {
      state.buildsTab = "pending";   // drafts live in the Pending queue
      document.querySelector(`#tabs .tab[data-tab="workbench"]`).click();
      // a draft's next step is its Record — land there with the book selected
      selectWorkbenchBook(d.dataset.draft, "record");
      return;
    }
    if (ev.target.closest("[data-review]")) { openReviewWin(); return; }
    const b = ev.target.closest("[data-gotab]");
    if (b) {
      // every workbench-bound row advertises pending work, so land on the
      // Pending queue even if the book list was left on Uploaded
      if (b.dataset.gotab === "workbench") state.buildsTab = "pending";
      document.querySelector(`#tabs .tab[data-tab="${b.dataset.gotab}"]`).click();
    }
  });
  // an activity row toggles its per-event detail
  el("home-activity").addEventListener("click", (ev) => {
    const row = ev.target.closest(".home-act[data-gk]");
    if (!row) return;
    const k = row.dataset.gk;
    if (homeState.expanded.has(k)) homeState.expanded.delete(k);
    else homeState.expanded.add(k);
    renderHome();
  });
  loadActivity();
}

// --- tabs + header -----------------------------------------------------------

const TAB_TITLES = { home: "Home", checked: "Catalogs",
                     workbench: "Workbench", replica: "Replica",
                     publish: "Published Library",
                     infotab: "Info" };

function setHeader(tabId) {
  // Both the visible title bar and the OS window title carry the active tab:
  // "Catalogs :: Library Tool v2.9".
  const name = TAB_TITLES[tabId] || "";
  document.title = `${name} :: Library Tool`;
  el("tb-tab").textContent = name ? name + " :: " : "";
  // the tab strip shows the active tab's command icons
  el("tg-upload").hidden = tabId !== "workbench";
}

// The title is centred on the window, so the wider of the two flanking regions
// sets how much room it may claim before it would slide under one of them.
// Below twice that, a centred title cannot avoid the menus at all -- drop it.
function fitTitleBar() {
  const w = (id) => el(id).getBoundingClientRect().width;
  const inset = Math.max(w("menubar"), w("win-controls")) + 12;
  const bar = el("titlebar");
  bar.style.setProperty("--tb-inset", inset + "px");
  el("tb-title").hidden = bar.clientWidth - 2 * inset < 56;
}

function initTabs() {
  for (const tab of document.querySelectorAll("#tabs .tab")) {
    tab.addEventListener("click", () => {
      document.querySelectorAll("#tabs .tab").forEach((t) => t.classList.remove("active"));
      document.querySelectorAll(".panel-view").forEach((p) => p.classList.remove("active"));
      tab.classList.add("active");
      el(tab.dataset.tab).classList.add("active");
      setHeader(tab.dataset.tab);
      if (tab.dataset.tab === "home") loadActivity();   // refresh on every visit
      if (tab.dataset.tab === "checked") { renderChecked(); loadOlStatus(); }
      if (tab.dataset.tab === "infotab") { pollConsoleLog(); renderConsole(); }
      // refresh builds and entry folders on every visit — either may have
      // changed elsewhere meanwhile
      if (tab.dataset.tab === "workbench") {
        renderUpload();
        loadOcrBooks().then(() => { renderOcrTab(); renderAnalyze(); renderWorkbench(); });
        pollJobs();       // paint the jobs drawer now, not next tick
      }
      if (tab.dataset.tab === "replica") {
        loadOcrBooks().then(renderReplicaBooks);
      }
      if (tab.dataset.tab === "publish") renderPublish();
    });
  }
  setHeader("home");
}

// --- tooltip (match info + overflowed cells) ---------------------------------

function showTip(text, x, y) {
  const tip = el("cad-tooltip");
  if (!text) { tip.hidden = true; return; }
  tip.textContent = text;
  tip.hidden = false;
  // Root CSS zoom scales fixed-position offsets as well as the tooltip itself,
  // while pointer coordinates and viewport bounds remain in visual pixels.
  // Measure at a stable origin, do all collision work in visual pixels, then
  // convert the final position back to the root's pre-zoom coordinate space.
  tip.style.left = "0px";
  tip.style.top = "0px";
  const r = tip.getBoundingClientRect();
  const scale = Math.max(0.01, Number(state.settings.uiScale) || 1);
  const pad = 14 * scale, edge = 8 * scale;
  let left = x + pad, top = y + pad;
  if (left + r.width > innerWidth - edge)
    left = Math.max(edge, innerWidth - r.width - edge);
  if (top + r.height > innerHeight - edge)
    top = Math.max(edge, y - r.height - pad);
  tip.style.left = (left / scale) + "px";
  tip.style.top = (top / scale) + "px";
}
function hideTip() { el("cad-tooltip").hidden = true; }

function initTooltips() {
  document.addEventListener("mouseover", (ev) => {
    const tagged = ev.target.closest("[data-tip]");
    if (tagged) { showTip(tagged.dataset.tip, ev.clientX, ev.clientY); return; }
    // an overflowing form field shows its full value
    if (ev.target.tagName === "INPUT" && ev.target.classList.contains("cad-input") &&
        ev.target.type !== "password" &&
        ev.target.scrollWidth > ev.target.clientWidth + 1 && ev.target.value) {
      showTip(ev.target.value, ev.clientX, ev.clientY);
      return;
    }
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

// --- table chrome: per-table column visibility + resizable columns --------------
// Every .grid table is registered here; the column icon above each table
// opens a visibility menu, and dragging a header's right edge resizes it.
// Both persist (settings.colVis / settings.colWidths, keyed per table).

const WHL_COLS = [
  ["src", "Src"], ["title", "Title"], ["subtitle", "Subtitle"],
  ["authors", "Authors"], ["year", "Year"], ["publisher", "Publisher"],
  ["pages", "Pages"], ["lang", "Lang"], ["subject", "Subject"],
  ["description", "Description"], ["status", "Status"], ["copyright", "©"],
];
const UPLOAD_COLS = [
  ["title", "Title"], ["subtitle", "Subtitle"], ["author", "Author"],
  ["publisher", "Publisher"], ["year", "Year"], ["archive", "Archive"],
  ["record", "Matched record"], ["status", "Status"], ["action", "Action"],
];

// tag/action columns have LOCKED widths (compact, not resizable); one
// column per table stretches to absorb leftover width so the table never
// leaves empty space on its right — the Title column by default.
const LOCKED_COLS = {
  checked: { img: 30, copyright: 30, whl: 38, ia: 38, ht: 38, mark: 40 },
  whl: { status: 38, copyright: 30 },
  upload: { status: 38, action: 40 },
};
const STRETCH_COL = { checked: "title", whl: "title", upload: "title" };

function tableDef(key) {
  switch (key) {
    case "checked": return { tableId: "checked-table", cols: CHECKED_COLS,
                             locked: LOCKED_COLS.checked, stretch: STRETCH_COL.checked };
    case "whl": return { tableId: "whltop-table", cols: WHL_COLS,
                         locked: LOCKED_COLS.whl, stretch: STRETCH_COL.whl };
    case "upload": return { tableId: "upload-table", cols: UPLOAD_COLS,
                            locked: LOCKED_COLS.upload, stretch: STRETCH_COL.upload };
    default: {
      // bottom pane tables: b-ol / b-ch / b-whl
      const t = key.slice(2);
      const def = BOTTOM_TABLES[t];
      return def ? {
        tableId: "bottom-table",
        cols: def.cols.map((label, i) => ["c" + i, label]),
        locked: {},
        stretch: "c0",   // Title is the first column in every bottom table
      } : null;
    }
  }
}

// Table widths depend on the space left after the side panes and UI scale are
// applied. Coalesce layout-affecting changes into one animation-frame pass so
// splitter drags remain smooth while every visible table stays flush with its
// pane.
let tableChromeRefreshFrame = null;
function refreshVisibleTableChrome() {
  applyTableChrome(state.settings.topTable === "whl" ? "whl" : "checked");
  applyTableChrome("upload");
  if (state.settings.showCatalog)
    applyTableChrome("b-" + activeBottomTable());
}
function scheduleTableChromeRefresh() {
  if (tableChromeRefreshFrame !== null)
    cancelAnimationFrame(tableChromeRefreshFrame);
  tableChromeRefreshFrame = requestAnimationFrame(() => {
    tableChromeRefreshFrame = null;
    refreshVisibleTableChrome();
  });
}

function colKeyAt(def, i) {
  return def.cols[i] ? def.cols[i][0] : "c" + i;
}

function applyTableChrome(key) {
  const def = tableDef(key);
  if (!def) return;
  const table = el(def.tableId);
  if (!table) return;
  const vis = state.settings.colVis[key] || {};
  const widths = state.settings.colWidths[key] || {};
  const locked = def.locked || {};
  const sized = Object.keys(widths).length > 0;
  const ths = [...table.querySelectorAll("thead th")];
  const hide = def.cols.map(([k]) => vis[k] === false);
  let total = 0;
  let stretchTh = null;
  ths.forEach((th, i) => {
    const ck = colKeyAt(def, i);
    th.style.display = hide[i] ? "none" : "";
    th.classList.toggle("col-locked", ck in locked);
    let w;
    if (ck in locked) {
      w = locked[ck];             // locked columns never change width
    } else {
      w = widths[ck];
      if (sized && !w && !hide[i]) w = 110;  // column re-shown after sizing
    }
    th.style.width = sized && w ? w + "px" : "";
    if (!hide[i] && w) total += w;
    if (!hide[i] && ck === def.stretch) stretchTh = th;
    const grip = th.querySelector(".col-rz");
    if (ck in locked) {
      if (grip) grip.remove();
    } else if (!grip) {
      const rz = document.createElement("span");
      rz.className = "col-rz";
      rz.dataset.ci = i;
      th.appendChild(rz);
    }
  });
  // stretch column hidden: the last visible unlocked column absorbs instead
  if (!stretchTh) {
    for (let i = ths.length - 1; i >= 0; i--) {
      if (!hide[i] && !(colKeyAt(def, i) in locked)) { stretchTh = ths[i]; break; }
    }
  }
  // Once any column has been resized, EVERY visible column carries an
  // explicit width and the table is sized to their sum — with fixed layout
  // and a partial width set the browser would redistribute the remaining
  // space and every other column would jump around. Leftover container
  // width goes to the stretch column so no empty space is left on the
  // table's right.
  table.style.tableLayout = sized ? "fixed" : "";
  if (sized) {
    const wrap = table.closest(".drafting");
    const avail = wrap ? wrap.clientWidth : 0;
    if (stretchTh && avail > total + 2) {
      const w = (parseInt(stretchTh.style.width, 10) || 110) + (avail - total);
      stretchTh.style.width = w + "px";
      total = avail;
    }
    table.style.width = total + "px";
  } else {
    table.style.width = "";
  }
  table.dataset.ck = key;
  // Re-masking every cell is O(rows*cols). On a width-only refresh (splitter
  // drag, window resize, zoom) the hidden-column set is unchanged, so skip it —
  // the rows already carry the right mask. Freshly built rows are masked by
  // streamRows per chunk (and renderHistoryRows resets this signature so its
  // direct-built rows are re-masked), so a skip here never strands a new row.
  const hideSig = hide.join(",");
  if (table.dataset.hideSig !== hideSig) {
    applyColHide(table, table.querySelectorAll("tbody tr"));
    table.dataset.hideSig = hideSig;
  }
}

// Column hiding is per-<td> (there is no colgroup): set display:none on each
// hidden column's cell. Column widths are header-driven under table-layout:fixed,
// so ONLY this display mask needs re-applying to rows built after
// applyTableChrome's one-shot pass — i.e. every streamed chunk past the first,
// which is why streamRows() calls this on each appended chunk.
function applyColHide(table, rows) {
  const key = table && table.dataset.ck;
  const def = key && tableDef(key);
  if (!def) return;
  const vis = state.settings.colVis[key] || {};
  const hide = def.cols.map(([k]) => vis[k] === false);
  for (const tr of rows) {
    const tds = tr.children;
    for (let i = 0; i < tds.length; i++) tds[i].style.display = hide[i] ? "none" : "";
  }
}

// one delegated drag handler covers every table's resize grips
function initColResize() {
  let drag = null;
  document.addEventListener("mousedown", (ev) => {
    const rz = ev.target.closest(".col-rz");
    if (!rz) return;
    ev.preventDefault();
    const th = rz.parentElement;
    const table = th.closest("table");
    const key = table.dataset.ck;
    const def = tableDef(key);
    if (!def) return;
    // first drag on this table: freeze every visible column at its
    // current width so only the dragged column moves
    let widths = state.settings.colWidths[key];
    let captured = false;
    if (!widths || !Object.keys(widths).length) {
      widths = {};
      [...table.querySelectorAll("thead th")].forEach((h, i) => {
        if (h.style.display === "none") return;
        const ck = colKeyAt(def, i);
        if (def.locked && ck in def.locked) return;  // locked widths are constant
        widths[ck] = h.offsetWidth;
      });
      state.settings.colWidths[key] = widths;
      applyTableChrome(key);
      captured = true;
    }
    drag = { th, table, key, def, ci: +rz.dataset.ci,
             x: ev.clientX, w: th.offsetWidth, moved: false, captured };
    document.body.classList.add("resizing");
  });
  document.addEventListener("mousemove", (ev) => {
    if (!drag) return;
    drag.moved = true;
    const w = Math.max(36, drag.w + ev.clientX - drag.x);
    const colKey = colKeyAt(drag.def, drag.ci);
    state.settings.colWidths[drag.key][colKey] = w;
    drag.th.style.width = w + "px";
    // keep the table at the exact sum of its column widths
    let total = 0;
    [...drag.table.querySelectorAll("thead th")].forEach((h) => {
      if (h.style.display !== "none") total += parseInt(h.style.width, 10) || h.offsetWidth;
    });
    drag.table.style.width = total + "px";
  });
  document.addEventListener("mouseup", () => {
    if (!drag) return;
    if (drag.moved) {
      saveSettings();
      // re-apply chrome so the stretch column reabsorbs leftover width —
      // narrowing a column must not leave empty space on the table's right
      applyTableChrome(drag.key);
      // the browser will still synthesize a click on the header — don't
      // let a resize end as a sort
      sortSuppress = true;
      setTimeout(() => { sortSuppress = false; }, 0);
    } else if (drag.captured) {
      // a click that never dragged: undo the width freeze
      delete state.settings.colWidths[drag.key];
      applyTableChrome(drag.key);
    }
    document.body.classList.remove("resizing");
    drag = null;
  });
}

let sortSuppress = false;

// --- column sorting (checked + WHL top tables) -----------------------------------

function copyrightState(status) {
  const value = String(status == null ? "" : status).trim();
  if (!value) return "pending";
  if (value.startsWith("Public domain (published")) return "public-domain-age";
  if (value.startsWith("Public domain")) return "public-domain";
  if (value.startsWith("In copyright")) return "in-copyright";
  return "unknown";
}

// Ascending copyright order runs from most clearly reusable to most
// restricted. Blank/pending values stay last via sortRowsBy's empty handling.
function copyrightSortRank(status) {
  switch (copyrightState(status)) {
    case "public-domain-age": return 0;
    case "public-domain": return 1;
    case "unknown": return 2;
    case "in-copyright": return 3;
    default: return "";
  }
}

function sortRowsBy(rows, getVal, dir) {
  return rows.slice().sort((x, y) => {
    const a = String(getVal(x) == null ? "" : getVal(x)).trim();
    const b = String(getVal(y) == null ? "" : getVal(y)).trim();
    if (!a && !b) return 0;
    if (!a) return 1;   // empties last regardless of direction
    if (!b) return -1;
    // numbers sort together (before text) so the comparator stays transitive
    const aNum = !isNaN(Number(a)), bNum = !isNaN(Number(b));
    if (aNum && bNum) return (Number(a) - Number(b)) * dir;
    if (aNum !== bNum) return (aNum ? -1 : 1) * dir;
    return a.localeCompare(b, undefined, { sensitivity: "base" }) * dir;
  });
}

function scanSortVal(row, src) {
  const v = effScan(row, src);
  return v === true ? "yes" : v === false ? "no" : "";
}

function checkedSortVal(row, key) {
  switch (key) {
    case "src": return row.captured ? "captured" : row.kind === "manual" ? "manual" : row.source;
    case "copyright": return row.checks && !row.checks.error
      ? copyrightSortRank(row.checks.copyright_status) : "";
    case "whl": return (row.checks && row.checks.in_whl) || "";
    case "ia": return scanSortVal(row, "internet_archive");
    case "ht": return scanSortVal(row, "hathitrust");
    case "mark": return rowMarkState(row);
    default: return row.book[key];
  }
}

function whlSortVal(r, key) {
  switch (key) {
    case "src": return r.added ? "added" : r.corrected ? "edited" : r.scraped ? "web" : "csv";
    case "lang": return r.language || "";
    case "copyright": return copyrightSortRank(copyrightStatusForSort({
      title: r.title, author: r.authors, year: r.year,
    }));
    default: return r[key] || "";
  }
}

function markSortHeaders(tkey) {
  const def = tableDef(tkey);
  const so = state.sort[tkey];
  const searching = topMode() === "search";
  const cons = state.settings.searchCons || {};
  [...el(def.tableId).querySelectorAll("thead th")].forEach((th, i) => {
    const key = def.cols[i] ? def.cols[i][0] : null;
    if (so && key === so.key) th.dataset.sorted = so.dir > 0 ? "asc" : "desc";
    else delete th.dataset.sorted;
    // active search constraints (checkbox or Ctrl+click) — only shown while searching
    const fkey = key ? searchMarkKey(key) : null;
    const marked = searching && fkey && SEARCH_MARK_FIELDS.has(fkey) && !!cons[fkey];
    th.classList.toggle("mark-search", marked);
  });
}

function initSortHeaders() {
  const wire = (tkey) => {
    const def = tableDef(tkey);
    el(def.tableId).querySelector("thead").addEventListener("click", (ev) => {
      if (sortSuppress || ev.target.closest(".col-rz")) return;
      const th = ev.target.closest("th");
      if (!th) return;
      const i = [...th.parentElement.children].indexOf(th);
      const key = def.cols[i] ? def.cols[i][0] : null;
      if (!key || key === "action") return;
      // Ctrl/Cmd+click on a markable column in search mode toggles that search
      // constraint (same persistent set as the Title/Author/Year checkboxes).
      // Any other Ctrl+click — edit mode, or a non-search column — sorts.
      const fkey = searchMarkKey(key);
      if ((ev.ctrlKey || ev.metaKey) && topMode() === "search" &&
          SEARCH_MARK_FIELDS.has(fkey)) {
        ev.preventDefault();
        const cons = state.settings.searchCons || (state.settings.searchCons = {});
        if (cons[fkey]) delete cons[fkey]; else cons[fkey] = true;
        saveSettings();
        rebuildSearchFromMarks();
        return;
      }
      const cur = state.sort[tkey];
      state.sort[tkey] = cur && cur.key === key
        ? { key, dir: -cur.dir }
        : { key, dir: 1 };
      if (tkey === "checked") renderChecked();
      else renderWhlTop();
    });
  };
  wire("checked");
  wire("whl");
}

// --- popup menus (filter + column visibility) ------------------------------------

let popupAnchor = null;

function closePopup() {
  el("popup-menu").hidden = true;
  popupAnchor = null;
}

function openPopup(anchor, html, wire) {
  const pop = el("popup-menu");
  if (!pop.hidden && popupAnchor === anchor) { closePopup(); return; }
  popupAnchor = anchor;
  pop.innerHTML = html;
  pop.hidden = false;
  const r = anchor.getBoundingClientRect();
  pop.style.top = Math.min(r.bottom + 4, innerHeight - pop.offsetHeight - 8) + "px";
  pop.style.left = Math.max(8, Math.min(r.right - pop.offsetWidth,
    innerWidth - pop.offsetWidth - 8)) + "px";
  if (wire) wire(pop);
}

function openColumnMenu(anchor, key, rerender) {
  const def = tableDef(key);
  if (!def) return;
  const vis = state.settings.colVis[key] || {};
  const html = `<div class="pm-head">Visible columns</div>` +
    def.cols.map(([k, label]) => `
      <label class="pm-item"><input type="checkbox" data-k="${esc(k)}"
        ${vis[k] === false ? "" : "checked"} /> ${esc(label)}</label>`).join("");
  openPopup(anchor, html, (pop) => {
    pop.querySelectorAll("input[data-k]").forEach((cb) => {
      cb.addEventListener("change", () => {
        state.settings.colVis[key] = state.settings.colVis[key] || {};
        if (cb.checked) delete state.settings.colVis[key][cb.dataset.k];
        else state.settings.colVis[key][cb.dataset.k] = false;
        saveSettings();
        rerender();
      });
    });
  });
}

const FILTER_GROUPS = [
  ["markFilter", "Mark", [["ALL", "All"], ["SCAN", "Scan"], ["UPLOAD", "Upload"],
    ["APPROVED", "Approved"], ["NONE", "Unmarked"]]],
  ["srcFilter", "Source", [["ALL", "All"], ["MANUAL", "Manual entries"],
    ["CATALOG", "Catalog books"]]],
  ["dlFilter", "Download", [["ALL", "All"], ["DONE", "Downloaded"],
    ["NOT", "Not downloaded"], ["FAILED", "Download failed"]]],
];

function filtersActive() {
  return FILTER_GROUPS.some(([k]) => (state.settings[k] || "ALL") !== "ALL");
}

function syncFilterBtn() {
  el("filter-btn").classList.toggle("active", filtersActive());
}

function openFilterMenu(anchor) {
  const html = FILTER_GROUPS.map(([k, head, opts]) =>
    `<div class="pm-head">${head}</div>` +
    opts.map(([v, label]) => `
      <label class="pm-item"><input type="radio" name="pm-${k}" value="${v}"
        ${(state.settings[k] || "ALL") === v ? "checked" : ""} /> ${label}</label>`).join("")
  ).join("");
  openPopup(anchor, html, (pop) => {
    pop.querySelectorAll("input[type=radio]").forEach((rb) => {
      rb.addEventListener("change", () => {
        state.settings[rb.name.slice(3)] = rb.value;
        saveSettings();
        syncFilterBtn();
        renderChecked();
      });
    });
  });
}

// --- settings window -----------------------------------------------------------

function fillFontSelect(id, list, settingKey, apply) {
  const sel = el(id);
  sel.innerHTML = "";
  for (const [val, label] of list) {
    const o = document.createElement("option");
    o.value = val;
    o.textContent = label;
    sel.appendChild(o);
  }
  sel.value = state.settings[settingKey] || "";
  if (sel.value !== (state.settings[settingKey] || "")) sel.value = "";
  sel.onchange = () => {
    state.settings[settingKey] = sel.value;
    saveSettings();
    apply();
  };
}

// --- Settings > Sync: downloadable databases --------------------------------

let _dbPollTimer = null;

function dbFmtBytes(n) {
  n = n || 0;
  if (n >= 1e9) return (n / 1e9).toFixed(1) + " GB";
  if (n >= 1e6) return (n / 1e6).toFixed(1) + " MB";
  if (n >= 1e3) return (n / 1e3).toFixed(0) + " KB";
  return n + " B";
}

function dbStatusMsg(targets) {
  const msg = el("db-status-msg");
  if (!msg) return;
  const jobs = Object.entries(targets).map(([n, t]) => [n, t.job]).filter(([, j]) => j);
  const dl = jobs.filter(([, j]) => j.status === "downloading");
  if (dl.length) {
    msg.textContent = dl.map(([n, j]) =>
      `${n}: ${dbFmtBytes(j.downloaded)}${j.total ? " / " + dbFmtBytes(j.total) : ""}`).join("  ·  ");
  } else {
    const err = jobs.find(([, j]) => j.status === "error");
    msg.textContent = err ? ("error: " + err[1].error) : "";
  }
}

async function renderDbSync() {
  const host = el("db-rows");
  if (!host) return;
  let data;
  try { data = await (await fetch("/api/db/status")).json(); }
  catch (e) { host.innerHTML = "<span class='tool-label'>backend unavailable</span>"; return; }
  const targets = data.targets || {};
  const pathEl = el("db-folder-path");
  if (pathEl) pathEl.textContent = data.db_dir || data.data_root || "";
  const openBtn = el("db-open-folder");
  if (openBtn) openBtn.onclick = openDataFolder;
  host.innerHTML = "";
  for (const [name, t] of Object.entries(targets)) {
    const file = t.filename || (t.path || "").split("/").pop();
    const lab = document.createElement("label");
    lab.className = "tool-label";
    lab.setAttribute("for", "set-dburl-" + name);
    lab.textContent = t.label + (t.present
      ? ` — present ✓ (${dbFmtBytes(t.size)})`
      : ` — not found · drop ${file} in the data folder, or download`);
    const inp = document.createElement("input");
    inp.id = "set-dburl-" + name;
    inp.className = "cad-input";
    inp.spellcheck = false;
    inp.placeholder = "https://…/" + t.path.split("/").pop();
    inp.value = (state.settings.dbUrls && state.settings.dbUrls[name]) || t.url || "";
    inp.onchange = () => {
      state.settings.dbUrls = state.settings.dbUrls || {};
      state.settings.dbUrls[name] = inp.value.trim();
      saveSettings();
    };
    host.appendChild(lab);
    host.appendChild(inp);
  }
  const btn = el("db-download");
  if (btn) btn.onclick = startDbDownload;
  dbStatusMsg(targets);
}

// Open the writable data folder so the user can drop database files straight in
// (local-first: a file here is used offline, no download or URL needed).
async function openDataFolder() {
  try {
    const r = await (await fetch("/api/db/reveal", { method: "POST" })).json();
    if (r && r.ok === false) status("DATA FOLDER :: " + (r.error || "could not open"));
  } catch (e) { status("DATA FOLDER :: could not open"); }
}

async function startDbDownload() {
  // capture any un-blurred URL edits, then push settings so the server sees them
  state.settings.dbUrls = state.settings.dbUrls || {};
  for (const inp of document.querySelectorAll("[id^='set-dburl-']"))
    state.settings.dbUrls[inp.id.replace("set-dburl-", "")] = inp.value.trim();
  const cu = el("set-cloud-url");
  if (cu) state.settings.cloudSearchUrl = cu.value.trim();
  saveSettings();
  await flushClientState();
  const btn = el("db-download");
  if (btn) btn.disabled = true;
  el("db-status-msg").textContent = "Starting …";
  try {
    const r = await (await fetch("/api/db/download",
      { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" })).json();
    if (!r.started || !r.started.length) {
      el("db-status-msg").textContent =
        "Nothing to download — set a source URL, or drop the file in the data folder.";
      if (btn) btn.disabled = false;
      return;
    }
  } catch (e) {
    el("db-status-msg").textContent = "Could not start the download.";
    if (btn) btn.disabled = false;
    return;
  }
  pollDbStatus();
}

function pollDbStatus() {
  clearTimeout(_dbPollTimer);
  _dbPollTimer = setTimeout(async () => {
    let data;
    try { data = await (await fetch("/api/db/status")).json(); } catch (e) { return; }
    const targets = data.targets || {};
    dbStatusMsg(targets);
    if (Object.values(targets).some((t) => t.job && t.job.status === "downloading")) {
      pollDbStatus();
    } else {
      const btn = el("db-download");
      if (btn) btn.disabled = false;
      renderDbSync();   // refresh present/size after completion
    }
  }, 1000);
}

function renderSettings() {
  // GENERAL
  el("gen-info").textContent =
    `Library Tool ${el("tb-meta").textContent} — ` +
    `${state.manual.length} manual entries / ${state.checked.size} checked books. ` +
    (el("status-right").textContent || "");
  el("reset-settings").onclick = async () => {
    if (!(await confirmDialog({
      title: "Reset interface settings",
      message: "Reset every interface setting?",
      detail: "Catalog data is kept.",
      confirmLabel: "Reset settings",
      danger: true,
    }))) return;
    try {
      localStorage.removeItem(SETTINGS_KEY);
      localStorage.removeItem(VIEWSTATE_KEY);
    } catch (e) {}
    location.reload();
  };
  el("clear-history-btn").onclick = async () => {
    if (!(await confirmDialog({
      title: "Clear history",
      message: "Clear the entire action history?",
      detail: "This cannot be undone.",
      confirmLabel: "Clear history",
      danger: true,
    }))) return;
    _actionLog = [];
    saveActionLog();
    if (activeBottomTable() === "history") renderBottomRows();
    status("HISTORY CLEARED");
  };
  const un = el("set-user-name");
  un.value = state.settings.userName || "";
  un.onchange = () => {
    state.settings.userName = un.value.trim().slice(0, 60);
    saveSettings();
    renderHome();          // the feed labels your own past events too
  };
  const ocr = el("set-whl-ocr");
  ocr.checked = !!state.settings.whlModalOcr;
  ocr.onchange = () => {
    state.settings.whlModalOcr = ocr.checked;
    saveSettings();
  };
  const pp = el("set-preview-pages");
  pp.value = state.settings.previewPages || 20;
  pp.onchange = () => {
    state.settings.previewPages =
      Math.max(1, Math.min(500, parseInt(pp.value, 10) || 20));
    pp.value = state.settings.previewPages;
    saveSettings();
  };
  const sr = el("set-scan-recent");
  sr.value = state.settings.scanRecentMin;
  sr.onchange = () => {
    let v = parseInt(sr.value, 10);
    if (!Number.isFinite(v)) v = 30;
    state.settings.scanRecentMin = Math.max(0, Math.min(1440, v));
    sr.value = state.settings.scanRecentMin;
    saveSettings();
  };
  // auto-IA-download toggle: disabling it must also drop anything still queued
  const autoDl = el("set-auto-ia-dl");
  autoDl.checked = state.settings.autoIaDownload !== false;
  autoDl.onchange = () => {
    state.settings.autoIaDownload = autoDl.checked;
    if (!autoDl.checked) { state.autoDlQueue = []; updateDlProgress(); }
    saveSettings();
  };
  for (const [id, k] of [["set-preview-original", "previewOriginal"],
                         ["set-keep-originals", "keepOriginals"],
                         ["set-trim-blank", "trimBlank"]]) {
    const n = el(id);
    n.checked = !!state.settings[k];
    n.onchange = () => {
      state.settings[k] = n.checked;
      saveSettings();
    };
  }
  // multi-volume set display prefs — re-render the checked table on change
  for (const [id, k] of [["set-expand-sets", "expandSets"],
                         ["set-hide-vol-titles", "hideVolTitles"]]) {
    const n = el(id);
    n.checked = !!state.settings[k];
    n.onchange = () => {
      state.settings[k] = n.checked;
      saveSettings();
      renderChecked();
    };
  }
  // copyright registration sources (left half of the copyright tag)
  for (const [id, k] of [["set-cr-cprs", "cprs"], ["set-cr-nypl", "nypl"]]) {
    const n = el(id);
    if (!n) continue;
    n.checked = !!(state.settings.copyrightSources || {})[k];
    n.onchange = () => {
      state.settings.copyrightSources =
        Object.assign({}, state.settings.copyrightSources, { [k]: n.checked });
      saveSettings();
      renderChecked();   // reg cache is keyed by source set -> re-fetch under the new key
    };
  }
  renderLanSettings();

  // APPEARANCE
  const themeSel = el("theme-select");
  themeSel.innerHTML = "";
  for (const [id, label] of THEMES) {
    const o = document.createElement("option");
    o.value = id;
    o.textContent = label;
    themeSel.appendChild(o);
  }
  themeSel.value = state.settings.theme;   // applyTheme() has already normalized it
  themeSel.onchange = () => setTheme(themeSel.value);
  const scaleSel = el("ui-scale-select");
  if (scaleSel) {
    scaleSel.innerHTML = UI_SCALES.map((v) =>
      `<option value="${v}">${Math.round(v * 100)}%</option>`).join("");
    scaleSel.value = String(state.settings.uiScale || 1);
    scaleSel.onchange = () => setUiScale(Number(scaleSel.value));
  }
  fillFontSelect("font-ui-select", FONT_CHOICES, "fontUi", applyFont);
  fillFontSelect("font-select", FONT_CHOICES, "font", applyFont);
  fillFontSelect("font-mono2-select", FONT_CHOICES, "fontMono2", applyFont);

  // AI
  for (const [id, k] of [["set-r2-account", "r2Account"], ["set-r2-bucket", "r2Bucket"],
                        ["set-r2-key", "r2KeyId"], ["set-r2-secret", "r2Secret"],
                        ["set-r2-public", "r2PublicBase"],
                        ["set-ai-base", "aiBase"], ["set-ai-model", "aiModel"],
                         ["set-ai-key", "aiKey"],
                         ["set-embed-base", "embedBase"],
                         ["set-embed-model", "embedModel"],
                         ["set-ai-instructions", "aiInstructions"]]) {
    const n = el(id);
    n.value = state.settings[k] || "";
    n.onchange = () => {
      state.settings[k] = n.value.trim();
      saveSettings();
    };
  }

  // OCR services (Tesseract runs locally; cloud credentials are verified
  // once the user has API keys)
  const svc = el("set-ocr-service");
  svc.value = state.settings.ocrService || "tesseract";
  svc.onchange = () => {
    state.settings.ocrService = svc.value;
    el("ocr-service").value = svc.value;
    saveSettings();
    updateDefaultEngineSummary();
  };
  for (const [id, k] of [["set-ocr-azure-endpoint", "ocrAzureEndpoint"],
                         ["set-ocr-azure-key", "ocrAzureKey"],
                         ["set-ocr-tesseract", "ocrTesseract"],
                         ["set-ocr-claude-key", "ocrClaudeKey"],
                         ["set-ocr-claude-model", "ocrClaudeModel"],
                         ["set-ocr-aws-key", "ocrAwsKey"],
                         ["set-ocr-aws-secret", "ocrAwsSecret"],
                         ["set-ocr-aws-region", "ocrAwsRegion"],
                         ["set-gs-sheet-id", "gsSpreadsheetId"],
                         ["set-gs-keyfile", "gsKeyFile"],
                         ["set-gs-sheet-name", "gsSheetName"],
                         ["set-cloud-url", "cloudSearchUrl"],
                         ["set-sb-url", "supabaseUrl"],
                         ["set-sb-key", "supabaseKey"],
                         ["set-sb-anon", "supabaseAnonKey"],
                         ["set-mistral-key", "mistralKey"]]) {
    const n = el(id);
    n.value = state.settings[k] || "";
    n.onchange = () => {
      state.settings[k] = n.value.trim();
      saveSettings();
    };
  }
  // Credentials: re-wire the secret fields the generic loops above touched so
  // their values load from and save to the server's Host-guarded secrets API.
  // Mistral is cached there and synchronized with the private cloud profile;
  // the others remain device-local.
  (async () => {
    const SECRET_FIELDS = [
      ["set-ai-key", "aiKey"], ["set-embed-key", "embedKey"],
      ["set-mistral-key", "mistralKey"],
      ["set-ocr-claude-key", "ocrClaudeKey"], ["set-ocr-azure-key", "ocrAzureKey"],
      ["set-ocr-aws-key", "ocrAwsKey"], ["set-ocr-aws-secret", "ocrAwsSecret"],
      ["set-sb-key", "supabaseKey"], ["set-sb-anon", "supabaseAnonKey"],
      ["set-r2-key", "r2KeyId"], ["set-r2-secret", "r2Secret"],
      ["set-gs-keyfile", "gsKeyFile"],
    ];
    const secrets = await hydrateSecrets();
    for (const [id, k] of SECRET_FIELDS) {
      const n = el(id);
      if (!n) continue;
      const v = secrets[k] || "";
      n.value = v;
      state.settings[k] = v;                 // in-memory only (client-side uses, e.g. master sync)
      n.onchange = () => {
        const val = n.value.trim();
        state.settings[k] = val;
        fetch("/api/secrets", {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ updates: { [k]: val } }),
        }).catch(() => {});
      };
    }
  })();
  // Phone capture uses the signed-in account + built-in public project key;
  // there is no Supabase credential for a normal user to configure here.
  const cm = el("set-cloud-minutes");
  cm.value = state.settings.cloudSyncMinutes || 0;
  cm.onchange = () => {
    let v = parseInt(cm.value, 10);
    if (!Number.isFinite(v)) v = 0;
    state.settings.cloudSyncMinutes = Math.max(0, Math.min(1440, v));
    cm.value = state.settings.cloudSyncMinutes;
    saveSettings();
  };
  const cdr = el("set-cloud-delremote");
  cdr.checked = state.settings.cloudDeleteRemote !== false;
  cdr.onchange = () => {
    state.settings.cloudDeleteRemote = cdr.checked;
    saveSettings();
  };
  el("cloud-test").onclick = async () => {
    el("cloud-test-msg").textContent = "Testing…";
    try {
      await flushClientState();   // the server reads settings server-side — push first
      const r = await (await fetch("/api/cloudsync/test")).json();
      el("cloud-test-msg").textContent = r.ok
        ? "OK — signed-in phone sync is reachable"
        : (r.error || "Failed");
    } catch (e) {
      el("cloud-test-msg").textContent = "Server unreachable";
    }
  };
  renderDbSync();
  // OCR rasterization width — the compression/shrink experiment knob
  const iw = el("set-ocr-width");
  iw.value = state.settings.ocrImageWidth || 1400;
  iw.onchange = () => {
    state.settings.ocrImageWidth =
      Math.max(600, Math.min(3000, parseInt(iw.value, 10) || 1400));
    iw.value = state.settings.ocrImageWidth;
    saveSettings();
  };
  // digit -> service shortcut mapping for the page view
  for (const key of ["1", "2", "3", "4", "5"]) {
    const n = el("set-ocr-key" + key);
    if (!n) continue;
    state.settings.ocrKeyMap = state.settings.ocrKeyMap || {};
    n.value = state.settings.ocrKeyMap[key] || "";
    n.onchange = () => {
      if (n.value) state.settings.ocrKeyMap[key] = n.value;
      else delete state.settings.ocrKeyMap[key];
      saveSettings();
    };
  }

  // TABLE VIEW
  const wrap = el("cols-checked");
  wrap.innerHTML = "";
  const vis = state.settings.colVis.checked || {};
  for (const [key, label] of CHECKED_COLS) {
    const lab = document.createElement("label");
    lab.className = "settings-col";
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = vis[key] !== false;
    cb.addEventListener("change", () => {
      state.settings.colVis.checked = state.settings.colVis.checked || {};
      if (cb.checked) delete state.settings.colVis.checked[key];
      else state.settings.colVis.checked[key] = false;
      saveSettings();
      applyTableChrome("checked");
    });
    lab.appendChild(cb);
    lab.appendChild(document.createTextNode(" " + label));
    wrap.appendChild(lab);
  }
  el("reset-widths").onclick = () => {
    state.settings.colWidths = {};
    saveSettings();
    for (const id of ["checked-table", "whltop-table", "upload-table", "bottom-table"]) {
      const t = el(id);
      t.style.tableLayout = "";
      t.style.width = "";
      t.querySelectorAll("thead th").forEach((th) => { th.style.width = ""; });
    }
    status("COLUMN WIDTHS RESET");
  };

  // FILE PATHS
  const bd = el("set-browse-dir");
  bd.value = state.settings.pdfBrowseDir || "";
  bd.onchange = () => {
    state.settings.pdfBrowseDir = bd.value.trim();
    saveSettings();
  };

  // --- Stage 2 tunables (guarded by id, so a control's absence is harmless) ---
  // AI: temperature override (blank = each call's own default) + request timeout
  const aiTemp = el("set-ai-temp");
  if (aiTemp) {
    aiTemp.value = state.settings.aiTemperature ?? "";
    aiTemp.onchange = () => {
      const v = aiTemp.value.trim();
      state.settings.aiTemperature =
        v === "" ? "" : Math.max(0, Math.min(2, parseFloat(v) || 0));
      aiTemp.value = state.settings.aiTemperature;
      saveSettings();
    };
  }
  const aiTo = el("set-ai-timeout");
  if (aiTo) {
    aiTo.value = state.settings.aiTimeout || 240;
    aiTo.onchange = () => {
      state.settings.aiTimeout =
        Math.max(10, Math.min(1200, parseInt(aiTo.value, 10) || 240));
      aiTo.value = state.settings.aiTimeout;
      saveSettings();
    };
  }
  // OCR: vision output-token cap (dense pages truncate at 8192)
  const omt = el("set-ocr-maxtokens");
  if (omt) {
    omt.value = state.settings.ocrMaxTokens || 8192;
    omt.onchange = () => {
      state.settings.ocrMaxTokens =
        Math.max(1024, Math.min(32000, parseInt(omt.value, 10) || 8192));
      omt.value = state.settings.ocrMaxTokens;
      saveSettings();
    };
  }
  // EDITING
  const hl = el("set-history-limit");
  if (hl) {
    hl.value = state.settings.historyLimit || 300;
    hl.onchange = () => {
      state.settings.historyLimit =
        Math.max(20, Math.min(2000, parseInt(hl.value, 10) || 300));
      hl.value = state.settings.historyLimit;
      saveSettings();
    };
  }
  const oll = el("set-ol-limit");
  if (oll) {
    oll.value = state.settings.olLimit || 60;
    oll.onchange = () => {
      state.settings.olLimit =
        Math.max(1, Math.min(100, parseInt(oll.value, 10) || 60));
      oll.value = state.settings.olLimit;
      saveSettings();
    };
  }
  const cd = el("set-confirm-discard");
  if (cd) {
    cd.checked = state.settings.confirmDiscard !== false;
    cd.onchange = () => { state.settings.confirmDiscard = cd.checked; saveSettings(); };
  }
  // ADVANCED: verbose server logging (pushed so the server re-reads its level)
  const vl = el("set-verbose-log");
  if (vl) {
    vl.checked = !!state.settings.verboseLogging;
    vl.onchange = () => {
      state.settings.verboseLogging = vl.checked;
      saveSettings();
      flushClientState();
    };
  }
  // EXPERIMENTAL: interface sharpening (toggle + amount slider, live preview)
  const esOn = el("set-exp-sharpen");
  const esAmt = el("set-exp-sharpen-amt");
  const esVal = el("set-exp-sharpen-val");
  if (esOn && esAmt && esVal) {
    const paintEs = () => {
      esVal.textContent = expSharpenAmount() + "%";
      esAmt.disabled = !esOn.checked;      // amount only bites while it is on
    };
    esOn.checked = !!state.settings.expSharpen;
    esAmt.value = String(expSharpenAmount());
    paintEs();
    esOn.onchange = () => {
      state.settings.expSharpen = esOn.checked;
      paintEs();
      applyExpSharpen();
      saveSettings();
    };
    const readAmt = () => {
      state.settings.expSharpenAmount =
        Math.max(0, Math.min(100, parseInt(esAmt.value, 10) || 0));
    };
    esAmt.oninput = () => {          // drag: update the readout + preview live
      readAmt();
      esVal.textContent = expSharpenAmount() + "%";
      applyExpSharpen();
    };
    esAmt.onchange = () => {         // release: persist
      readAmt();
      applyExpSharpen();
      saveSettings();
    };
  }
  // UPDATES (desktop shell reads these off client_state at launch)
  const au = el("set-auto-update");
  const pre = el("set-prerelease-update");
  const syncPrereleaseEnabled = () => {
    if (pre) pre.disabled = au ? !au.checked : false;
  };
  if (au) {
    au.checked = state.settings.autoUpdate !== false;
    au.onchange = () => {
      state.settings.autoUpdate = au.checked;
      syncPrereleaseEnabled();
      saveSettings();
    };
  }
  if (pre) {
    pre.checked = state.settings.includePrereleaseUpdates === true;
    pre.onchange = () => {
      state.settings.includePrereleaseUpdates = pre.checked;
      saveSettings();
    };
  }
  syncPrereleaseEnabled();
}

function initSettingsNav() {
  for (const b of document.querySelectorAll("#settings-nav .snav")) {
    b.addEventListener("click", () => {
      document.querySelectorAll("#settings-nav .snav").forEach((x) =>
        x.classList.toggle("active", x === b));
      document.querySelectorAll("#settings-content .settings-sec").forEach((s) =>
        s.classList.toggle("active", s.id === b.dataset.sec));
    });
  }
}

// LAN capture: bind the toggle/port to settings, and pull the live token + this
// machine's addresses from the server (which starts/stops the listener on save).
function renderLanSettings() {
  const en = el("set-lan-enable"), port = el("set-lan-port");
  const token = el("set-lan-token"), ips = el("set-lan-ips"), note = el("set-lan-note");
  if (!en) return;                                   // section not in this build
  en.checked = !!state.settings.lanCapture;
  port.value = state.settings.lanPort || 8899;
  const refresh = () => {
    fetch("/api/lan_info").then(r => r.json()).then(info => {
      token.value = info.token || "";
      ips.textContent = (info.ips && info.ips.length)
        ? info.ips.map(ip => ip + ":" + info.port).join("   ")
        : "no LAN address found";
      note.textContent = info.enabled
        ? "Listening. Pair the phone with an address + the token above."
        : "Off. Turn on to accept captures from the phone.";
    }).catch(() => { note.textContent = "LAN info unavailable."; });
  };
  en.onchange = () => {
    state.settings.lanCapture = en.checked;
    saveSettings();                                  // PUT re-applies the listener
    setTimeout(refresh, 400);
  };
  port.onchange = () => {
    state.settings.lanPort = Math.max(1024, Math.min(65535, parseInt(port.value, 10) || 8899));
    port.value = state.settings.lanPort;
    saveSettings();
    setTimeout(refresh, 400);
  };
  refresh();
}

function openSettings() { renderSettings(); el("settings-overlay").hidden = false; }
function closeSettings() {
  el("settings-overlay").hidden = true;
  // The Databases panel lives in Settings; a file dropped into ~/.library-tool
  // (or a finished download) leaves the OL-index badge stale, and closing a
  // modal fires no window-focus event to refresh it — so refresh on the way out.
  loadOlStatus();
}

function openAbout() { const o = el("about-overlay"); if (o) o.hidden = false; }
function closeAbout() { const o = el("about-overlay"); if (o) o.hidden = true; }
function initAbout() {
  const close = el("about-close");
  if (close) close.addEventListener("click", closeAbout);
  const ov = el("about-overlay");
  if (ov) ov.addEventListener("mousedown", (ev) => { if (ev.target === ov) closeAbout(); });
  // the About footer buttons reuse menu commands (Website / Changelog)
  for (const b of document.querySelectorAll("#about-body [data-cmd]")) {
    b.addEventListener("click", () => {
      closeAbout();
      const f = MENU_CMDS[b.dataset.cmd];
      if (f) f();
    });
  }
}

// --- changelog viewer ----------------------------------------------------------
// Renders the shared release notes (website/changelog.md, served by
// /api/changelog). Each version contains Additions, Other Changes, and/or
// Bugfixes headings with plain bullets. Everything is escaped; no markdown HTML
// is trusted.
let changelogLoaded = false;
function openChangelog() {
  el("changelog-overlay").hidden = false;
  if (changelogLoaded) return;
  const body = el("changelog-body");
  body.innerHTML = '<p class="pane-note">Loading…</p>';
  fetch("/api/changelog")
    .then((r) => (r.ok ? r.text() : Promise.reject(new Error("http " + r.status))))
    .then((md) => { body.innerHTML = changelogHTML(parseChangelog(md)); changelogLoaded = true; })
    .catch(() => { body.innerHTML = '<p class="pane-note">Couldn’t load the changelog.</p>'; });
}
function closeChangelog() { el("changelog-overlay").hidden = true; }

const CHANGELOG_CATEGORIES = new Map([
  ["additions", "Additions"],
  ["other changes", "Other Changes"],
  ["bugfixes", "Bugfixes"],
]);

function changelogCategory(cur, name) {
  let category = cur.categories.find((c) => c.name === name);
  if (!category) {
    category = { name, items: [] };
    cur.categories.push(category);
  }
  return category;
}

function parseChangelog(md) {
  const versions = [];
  let cur = null;
  let category = null;
  for (const raw of String(md || "").split(/\r?\n/)) {
    const line = raw.trim();
    let m;
    if ((m = /^##\s+(.+?)(?:\s+[—–·-]\s+(.+))?$/.exec(line))) {   // em/en/middot/hyphen date separator
      cur = {
        version: m[1].trim(),
        date: (m[2] || "").trim(),
        categories: [],
        items: [],
      };
      versions.push(cur);
      category = null;
    } else if (cur && (m = /^###\s+(.+)$/.exec(line))) {
      const name = CHANGELOG_CATEGORIES.get(m[1].trim().toLowerCase());
      category = name ? changelogCategory(cur, name) : null;
    } else if (cur && (m = /^[-*]\s+(.+)$/.exec(line))) {
      const item = m[1].trim();
      (category || changelogCategory(cur, "Other Changes")).items.push(item);
      cur.items.push(item);                    // legacy flat-list consumers
    }
    // title, preamble, unknown subheadings, and blank lines are ignored
  }
  return versions;
}

function changelogCategoryHTML(category) {
  return '<div class="cl-category"><h4 class="cl-category-name">' +
    esc(category.name) + '</h4><ul class="cl-list">' +
    category.items.map((i) => "<li>" + esc(i) + "</li>").join("") +
    "</ul></div>";
}

function changelogHTML(versions) {
  if (!versions.length) return '<p class="pane-note">No changelog yet.</p>';
  return versions.map((v) => {
    const categories = (v.categories || []).filter((c) => c.items && c.items.length);
    const notes = categories.length
      ? categories.map(changelogCategoryHTML).join("")
      : '<ul class="cl-list">' +
        (v.items || []).map((i) => "<li>" + esc(i) + "</li>").join("") + "</ul>";
    return '<section class="cl-rel"><h3 class="cl-ver">' + esc(v.version) +
      (v.date ? ' <span class="cl-date">' + esc(v.date) + "</span>" : "") +
      "</h3>" + notes + "</section>";
  }).join("");
}

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

// The query the embedded bottom catalogs (Master list, WHL) filter by: a
// search-mode row selection overrides the Find box so those tabs search for the
// selected row + its marks, matching what the Open Library tab is querying.
function bottomFilterQuery() {
  const ov = state.olOverride;
  if (ov) return { title: ov.title || "", author: ov.author || "",
                   year: ov.year || "", empty: false };
  return findQuery();
}

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
  // download state rides as a bold 2px border on the tag itself (dl-ok/err/prog),
  // not a dot inside it; its tip folds into the tag's own tooltip. A file that
  // is actually on disk also takes over the label with a download glyph — the
  // verification state it displaces stays visible on the marker beside it.
  const dl = opts.dot ? " dl-" + opts.dot.cls : "";
  if (opts.dot && opts.dot.cls === "ok") label = BICONS.download;
  let tipText = opts.tip || "";
  if (opts.dot && opts.dot.tip)
    tipText = tipText ? tipText + "\n" + opts.dot.tip : opts.dot.tip;
  const tip = tipText ? ` data-tip="${esc(tipText)}"` : "";
  const attrs = opts.attrs || "";
  // a label may be one of the inline-SVG glyphs below instead of text
  const body = String(label).startsWith("<svg") ? label : esc(label);
  if (opts.href)
    return `<a class="badge ${cls}${dl}" href="${esc(opts.href)}" target="_blank" rel="noopener"${tip}${attrs}>${body}</a>`;
  return `<span class="badge ${cls}${dl}"${tip}${attrs}>${body}</span>`;
}

function tipForLocalWhl(checks) {
  if (!checks) return "";
  const m = checks.whl_match;
  if (!m) return "WHL catalog: " + (checks.in_whl || "not checked");
  const lines = ["WHL catalog match: " + m.title];
  if (m.author) lines.push("Author: " + m.author);
  if (m.year) lines.push("Year: " + m.year);
  lines.push("Status: " + (m.status || "?"));
  if (m.permalink) lines.push("URL: " + m.permalink);
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
          return wrap(badge("available", BICONS.check, {
            tip: "Manually located source:\n" + murl +
              "\n(automatic match was rejected as a false positive)",
            href: murl,
          }));
        return wrap(badge("missing", BICONS.cross, {
          tip: "REJECTED AS FALSE POSITIVE.\n" + tip +
            "\nCLICK TAG: paste the URL of a manually located source",
          href: m.permalink || "",
        }));
      }
      // a draft entry is shown as a pencil whatever its verify state
      if (c.in_whl === "draft")
        return wrap(badge("missing", BICONS.pencil, { tip, href: m.permalink || "" }));
      return wrap(badge("available", verifyGlyph(row, "whl"),
        { tip, href: m.permalink || "" }));
    }
    case "no": return badge("missing", NO_TAG, { tip });
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

// --- copyright split tag: left = registration record, right = renewal --------
// The renewal (right) half comes from the offline checks.copyright_status; the
// registration (left) half is fetched lazily per book from
// /api/copyright/registration (network, cached) and filled in progressively.
const REG_KEY = "whl_reg_cache_v2";
const REG_NEGATIVE_TTL_MS = 24 * 60 * 60 * 1000;
let _regCache = null;
const _regQueue = [];
const _regPending = new Set();
let _regInFlight = 0;
const REG_CONCURRENCY = 3;

function regMissExpired(value, now = Date.now()) {
  return value && value.found === false &&
    now - Number(value._cachedAt || 0) >= REG_NEGATIVE_TTL_MS;
}

function regCache() {
  if (_regCache) return _regCache;
  _regCache = new Map();
  try {
    const o = JSON.parse(localStorage.getItem(REG_KEY) || "{}");
    const now = Date.now();
    for (const k in o) {
      const value = o[k];
      if (!regMissExpired(value, now)) _regCache.set(k, value);
    }
  } catch (e) { /* ignore */ }
  return _regCache;
}
function saveRegCache() {
  try {
    const o = {};
    for (const [k, v] of regCache()) o[k] = v;
    localStorage.setItem(REG_KEY, JSON.stringify(o));
  } catch (e) { /* ignore */ }
}
function cachedReg(key) {
  const cache = regCache();
  const value = cache.get(key);
  if (!regMissExpired(value)) return value;
  cache.delete(key);
  saveRegCache();
  return undefined;
}
function copyrightSources() {
  const c = state.settings.copyrightSources || {};
  return ["cprs", "nypl"].filter((k) => c[k]);
}
function regKey(book, sources = copyrightSources()) {
  const n = (s) => String(s || "").toLowerCase().replace(/\s+/g, " ").trim();
  // include the sources so toggling them in settings re-fetches (a cached
  // result is only valid for the source set it was fetched with)
  return n(book && book.title) + "|" + n(book && book.author) + "|" +
    n(book && book.year) + "|" + sources.join(",");
}
function queueReg(book) {
  const sources = copyrightSources();
  if (!sources.length) return;
  const sourceKey = sources.join(",");
  const key = regKey(book, sources);
  if (cachedReg(key) !== undefined || _regPending.has(key)) return;
  _regPending.add(key);
  _regQueue.push({ _key: key, title: book.title || "", author: book.author || "",
                   year: book.year || "", sources: sourceKey });
  pumpRegQueue();
}
function pumpRegQueue() {
  while (_regInFlight < REG_CONCURRENCY && _regQueue.length) {
    const b = _regQueue.shift();
    // A settings change invalidates queued requests whose key names the old
    // source set. Drop them; the next render can enqueue the current key.
    if (!b.sources || b.sources !== copyrightSources().join(",")) {
      _regPending.delete(b._key);
      continue;
    }
    _regInFlight++;
    const p = new URLSearchParams({ title: b.title, author: b.author,
      year: b.year, sources: b.sources });
    fetch("/api/copyright/registration?" + p)
      .then((r) => {
        if (!r.ok) throw new Error(`registration lookup failed (HTTP ${r.status})`);
        return r.json();
      })
      .then((res) => {
        regCache().set(b._key, Object.assign({}, res, { _cachedAt: Date.now() }));
        saveRegCache();
        scheduleCrRefresh();
      })
      .catch(() => { /* leave uncached; retried on next render */ })
      .finally(() => {
        _regPending.delete(b._key);
        _regInFlight--;
        pumpRegQueue();
      });
  }
}
// --- copyright renewal-status cache (needed for WHL rows, which lack checks) ---
const CRSTATUS_KEY = "whl_cr_status_v1";
let _crStatusCache = null;
const _crStatusQueue = [];
const _crStatusPending = new Set();
let _crStatusInFlight = 0;

function crStatusCache() {
  if (_crStatusCache) return _crStatusCache;
  _crStatusCache = new Map();
  try {
    const o = JSON.parse(localStorage.getItem(CRSTATUS_KEY) || "{}");
    for (const k in o) _crStatusCache.set(k, o[k]);
  } catch (e) { /* ignore */ }
  return _crStatusCache;
}
function saveCrStatusCache() {
  try {
    const o = {};
    for (const [k, v] of crStatusCache()) o[k] = v;
    localStorage.setItem(CRSTATUS_KEY, JSON.stringify(o));
  } catch (e) { /* ignore */ }
}
function crStatusKey(b) {
  const n = (s) => String(s || "").toLowerCase().replace(/\s+/g, " ").trim();
  return n(b.title) + "|" + n(b.author) + "|" + n(b.year);
}
function queueCrStatus(b) {
  const key = crStatusKey(b);
  if (crStatusCache().has(key) || _crStatusPending.has(key)) return;
  _crStatusPending.add(key);
  _crStatusQueue.push({ _key: key, title: b.title || "", author: b.author || "", year: b.year || "" });
  pumpCrStatus();
}
function pumpCrStatus() {
  while (_crStatusInFlight < REG_CONCURRENCY && _crStatusQueue.length) {
    const b = _crStatusQueue.shift();
    _crStatusInFlight++;
    const p = new URLSearchParams({ title: b.title, author: b.author, year: b.year });
    fetch("/api/copyright/status?" + p).then((r) => r.json())
      .then((res) => { crStatusCache().set(b._key, res.copyright_status || ""); saveCrStatusCache(); scheduleCrRefresh(); })
      .catch(() => { /* leave uncached; retried on next render */ })
      .finally(() => {
        _crStatusPending.delete(b._key);
        _crStatusInFlight--;
        pumpCrStatus();
      });
  }
}

// Sorting the full WHL catalogue must not launch one request per row. Most
// statuses follow directly from the publication year; only renewal-era books
// need the offline database lookup, and those are queued through the cache.
function copyrightStatusForSort(b) {
  const cached = crStatusCache().get(crStatusKey(b));
  if (cached !== undefined) return cached;
  const match = /\b(1[5-9]\d{2}|20\d{2})\b/.exec(String(b.year || ""));
  if (!match) return "Unknown (no year)";
  const year = Number(match[1]);
  if (year <= new Date().getFullYear() - 96)
    return `Public domain (published ${year})`;
  if (year > 1963)
    return year <= 1977 ? "In copyright (auto-renewed)" : "In copyright";
  queueCrStatus(b);
  return "";
}

// --- renewal record details (the dates the tooltip quotes) -------------------
// A renewal status names its record ("In copyright (renewal R64009)"); the dates
// live in the renewals CSV, fetched per ID in batches and cached for good.
const REN_KEY = "whl_ren_cache_v1";
let _renCache = null;
const _renQueue = new Set();
let _renTimer = null;

function renCache() {
  if (_renCache) return _renCache;
  _renCache = new Map();
  try {
    const o = JSON.parse(localStorage.getItem(REN_KEY) || "{}");
    for (const k in o) _renCache.set(k, o[k]);
  } catch (e) { /* ignore */ }
  return _renCache;
}
function saveRenCache() {
  try {
    const o = {};
    for (const [k, v] of renCache()) o[k] = v;
    localStorage.setItem(REN_KEY, JSON.stringify(o));
  } catch (e) { /* ignore */ }
}
function queueRen(id) {
  if (!id || renCache().has(id) || _renQueue.has(id)) return;
  _renQueue.add(id);
  clearTimeout(_renTimer);
  _renTimer = setTimeout(flushRenQueue, 60);   // one request per render pass
}
function flushRenQueue() {
  const ids = [..._renQueue].slice(0, 200);
  if (!ids.length) return;
  ids.forEach((i) => _renQueue.delete(i));
  fetch("/api/copyright/renewal?ids=" + encodeURIComponent(ids.join(",")))
    .then((r) => r.json())
    .then((res) => {
      // cache misses too ({}), so an absent record is never re-requested
      for (const id of ids) renCache().set(id, res[id] || {});
      saveRenCache();
      scheduleCrRefresh();
      if (_renQueue.size) flushRenQueue();
    })
    .catch(() => { /* leave uncached; retried on the next render */ });
}
// the renewals CSV writes dates as "12Jun50" (all 20th century)
function crDate(s) {
  const m = /^(\d{1,2})([A-Za-z]{3})(\d{2})$/.exec(String(s || "").trim());
  if (!m) return String(s || "").trim();
  const mon = m[2][0].toUpperCase() + m[2].slice(1).toLowerCase();
  return `${Number(m[1])} ${mon} 19${m[3]}`;
}

// re-render all on-screen copyright tags after a status/registration fetch resolves
let _crRefreshTimer = null;
function scheduleCrRefresh() {
  clearTimeout(_crRefreshTimer);
  _crRefreshTimer = setTimeout(refreshAllCrTags, 180);
}
function refreshAllCrTags() {
  const resortWhl = state.settings.topTable === "whl" && state.sort.whl &&
    state.sort.whl.key === "copyright";
  if (resortWhl) {
    // Statuses arriving from the renewal-era queue can move a row between
    // semantic buckets. This call is already debounced by scheduleCrRefresh.
    renderWhlTop();
  } else {
    document.querySelectorAll(".cr-tag").forEach((tag) => {
      const b = { title: tag.dataset.crt || "", author: tag.dataset.cra || "", year: tag.dataset.cry || "" };
      let st = tag.dataset.crs;
      if (st === "") { const c = crStatusCache().get(crStatusKey(b)); st = c === undefined ? undefined : c; }
      const tmp = document.createElement("template");
      tmp.innerHTML = renderCrTag(b, st).trim();
      if (tmp.content.firstElementChild) tag.replaceWith(tmp.content.firstElementChild);
    });
  }
  // the Info pane spells the same records out, so it resolves on the same fetch
  refreshInfoIfActive();
}

// Semantic colours for the diagonal tag. The upper-left half answers only
// whether registration evidence exists; the lower-right half answers only the
// resulting copyright status. Era and renewal details belong in the tooltip,
// not in extra hues that make the same colour mean two different things.

// Registration evidence has TWO independent sources: the CPRS/NYPL lookup, and
// the renewal record itself, which cites the original registration it renews.
// Hollingsworth's "Flower Chronicles" is the case that forced this — its renewal
// names an original registration that the searched registers do not hold. A
// renewal cannot exist without a registration, so the citation IS a record.
function crEvidence(b, status) {
  const e = { status, pdAge: false, renewedId: "", autoRen: false, notRen: false,
              post63: false, reg: null, regPending: false, renewal: null,
              renPending: false, record: null };
  const value = String(status || "");
  e.pdAge = value.startsWith("Public domain (published");
  e.renewedId = (/^In copyright \(renewal (.+)\)$/.exec(value) || [])[1] || "";
  e.autoRen = value.startsWith("In copyright (auto-renewed");
  e.notRen = value.startsWith("Public domain (no renewal");
  e.post63 = value.startsWith("In copyright") && !e.renewedId;

  if (e.renewedId) {
    const r = renCache().get(e.renewedId);
    if (r === undefined) { queueRen(e.renewedId); e.renPending = true; }
    else if (r && Object.keys(r).length) e.renewal = r;
  }
  if (copyrightSources().length) {
    const r = cachedReg(regKey(b));
    if (r === undefined) { queueReg(b); e.regPending = true; }
    else e.reg = r;
  }

  const m = e.reg && e.reg.found ? e.reg.match : null;
  if (m) {
    e.record = { number: m.reg_number || "", date: m.date || m.year || "",
                 via: (e.reg.sources || []).map((s) => s.toUpperCase()).join(", "),
                 title: m.title || "", author: m.author || "" };
  } else if (e.renewal && (e.renewal.registration_number || e.renewal.registration_date)) {
    e.record = { number: e.renewal.registration_number || "",
                 date: crDate(e.renewal.registration_date) || "",
                 via: "cited by renewal " + e.renewedId, title: "", author: "" };
  }
  return e;
}

// "D64591 · 24 Jun 1923" + where it came from
function crRecordLine(rec) {
  const head = "Registration " + (rec.number || "(unnumbered)") +
    (rec.date ? " · " + rec.date : "");
  return rec.via ? head + "\n" + rec.via : head;
}

function copyrightColors(b, status) {
  if (status === undefined) return { left: "pending", right: "pending", lt: "Checking copyright …", rt: "Checking copyright …" };
  const e = crEvidence(b, status);
  const rec = e.record;
  const noRec = !copyrightSources().length
    ? "Registration lookup disabled — enable a source in Settings"
    : "No copyright registration record found";

  // --- lower-right half: overall copyright status
  let rc, rt;
  if (e.pdAge) { rc = "public-domain"; rt = status; }
  else if (e.renewedId) {
    rc = "in-copyright";
    rt = "Renewal " + e.renewedId + " on file — in copyright";
    if (e.renewal && (e.renewal.renewal_date || e.renewal.renewal_year))
      rt += "\nRenewed " + (crDate(e.renewal.renewal_date) || e.renewal.renewal_year);
    if (e.renewal && e.renewal.registration_date)
      rt += "\nRenews registration " + (e.renewal.registration_number || "(unnumbered)") +
        " of " + crDate(e.renewal.registration_date);
  } else if (e.autoRen) {
    rc = "in-copyright";
    rt = "In copyright — published 1964-1977 with automatic renewal";
  } else if (e.post63) {
    rc = "in-copyright";
    rt = "In copyright — published from 1978; registration is not required";
  } else if (e.notRen && rec) {
    rc = "public-domain";
    rt = "Registration found but no renewal found — likely public domain";
  } else if (e.notRen) {
    rc = "inconclusive";
    rt = "No renewal found, but no registration record backs that result";
  } else {
    rc = "unknown";
    rt = status || "Copyright status unknown";
  }

  // --- upper-left half: registration evidence
  if (!rec && (e.regPending || (e.renewedId && e.renPending)))
    return { left: "pending", right: rc, lt: "Checking registration …", rt };

  if (rec) {
    const lt = crRecordLine(rec) +
      (rec.title ? "\n" + rec.title + (rec.author ? " — " + rec.author : "") : "");
    return { left: "reg-found", right: rc, lt, rt };
  }
  return {
    left: copyrightSources().length ? "reg-none" : "unknown",
    right: rc,
    lt: noRec,
    rt,
  };
}

function renderCrTag(b, status) {
  const c = copyrightColors(b, status);
  const spoken = (value) => String(value || "unknown").replace(/\n/g, ". ")
    .trim().replace(/[.\s]+$/, "");
  const aria = "Registration evidence: " + spoken(c.lt) +
    ". Copyright status: " + spoken(c.rt) + ".";
  const attrs = `data-crkey="${esc(regKey(b))}" data-crt="${esc(b.title || "")}" ` +
    `data-cra="${esc(b.author || "")}" data-cry="${esc(b.year || "")}" ` +
    `data-crs="${esc(status == null ? "" : status)}" role="img" ` +
    `aria-label="${esc(aria)}" data-tip="${esc(aria)}"`;
  return `<span class="cr-tag cr-split" ${attrs}>` +
    `<span class="cr-left cr-${c.left}" aria-hidden="true" data-tip="${esc(c.lt)}"></span>` +
    `<span class="cr-right cr-${c.right}" aria-hidden="true" data-tip="${esc(c.rt)}"></span></span>`;
}

// book: {title, author, year}; status: copyright_status string, or undefined to
// fetch it (WHL / unchecked rows). Checked rows pass their checks.copyright_status.
// undefined while the offline status is still being fetched
function crStatusFor(b) {
  const cached = crStatusCache().get(crStatusKey(b));
  if (cached === undefined) queueCrStatus(b);
  return cached;
}

function copyrightTag(book, status) {
  const b = { title: book.title, author: book.author, year: book.year };
  if (status === undefined) status = crStatusFor(b);
  return renderCrTag(b, status);
}
function copyrightCell(row) {
  const ch = row.checks;
  if (ch && ch.error) return badge("error", "ERR", { tip: ch.error });
  return copyrightTag(row.book, ch ? ch.copyright_status : undefined);
}

function tipForScan(s, isHt) {
  if (!s) return "Not checked";
  if (s.error) return "Error: " + s.error;
  const b = s.best_match;
  if (!b) return s.note || (s.available === false ? "No match found" : "Could not determine");
  const lines = [(s.available === false ? "No confident match — closest: " : "Match: ") + b.title];
  if (b.author) lines.push("Author: " + b.author);
  if (b.year) lines.push("Year: " + b.year);
  if (b.accuracy != null) lines.push("Accuracy: " + b.accuracy);
  const url = b.url || b.record_url;
  if (url) lines.push("URL: " + url);
  if (isHt && b.items && b.items.length)
    lines.push("Items: " + b.items.map((i) => `${i.volume || "copy"} [${i.rights}]`).join(", "));
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
  pending: "Pending — click the marker to approve",
  approved: "Approved — click the marker to reject (false positive)",
  rejected: "Rejected (false positive) — click the marker to reset.\nClick the tag to paste a manually located source.",
};

// "no match here" reads as a dash rather than a word
const NO_TAG = "–";

// An unrejected match shows its verification state as the tag itself:
// pending = ~, approved = check. (Rejected tags are built by the callers,
// which also need to swap the link/tooltip, and show a cross.)
function verifyGlyph(row, source) {
  return getVerify(row, source) === "approved" ? BICONS.check : "~";
}

function verifyUnit(row, source, tagHtml) {
  const st = getVerify(row, source);
  const manual = st === "rejected" && getManualUrl(row, source);
  const cls = manual ? "approved" : st;
  const tip = manual
    ? "Manually located source — click the marker to reset"
    : VERIFY_TIPS[st];
  return `<span class="tag-unit" data-vsrc="${source}">${tagHtml}` +
    `<span class="vmark ${cls}" data-tip="${esc(tip)}"></span></span>`;
}

function scanBadge(row, source, dot) {
  const scans = row.scans;
  if (!scans || !scans[source]) return badge("unknown", "---", { tip: "Not scanned yet", dot });
  const s = scans[source];
  const isHt = source === "hathitrust";
  const tip = tipForScan(s, isHt);
  if (s.error) return badge("error", "ERR", { tip, dot });
  if (s.available === true) {
    const best = s.best_match || {};
    const href = best.url || best.record_url || "";
    // A HathiTrust match without full view is view-only: there is no source to
    // fetch, so it carries no verification marker.
    if (isHt && !s.full_view) return badge("error", "VO", {
      tip: "View-only on HathiTrust (no full view — nothing to download).\n" + tip,
      href, dot });
    if (getVerify(row, source) === "rejected") {
      const murl = getManualUrl(row, source);
      if (murl) {
        return verifyUnit(row, source, badge("available", BICONS.check, {
          tip: "Manually located source:\n" + murl +
            "\n(automatic match was rejected as a false positive)",
          href: murl, dot,
        }));
      }
      return verifyUnit(row, source, badge("missing", BICONS.cross, {
        tip: "Rejected as false positive.\n" + tip +
          "\nClick the tag to paste the URL of a manually located source",
        href, dot,
      }));
    }
    return verifyUnit(row, source,
      badge("available", verifyGlyph(row, source), { tip, href, dot }));
  }
  // IA matched books, but every one is borrow/lending only (no direct download)
  if (s.no_download) return badge("missing", "ND", {
    tip: "Found on Internet Archive, but every match is borrow/lending only" +
      " — no available download.\n" + tip,
    href: s.search_url || "", dot });
  if (s.available === false) return badge("missing", NO_TAG, { tip, dot });
  return badge("unknown", "?", { tip, href: s.search_url || "", dot });
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
      reason: "Not in WHL; a scan exists in an online archive.\nVerify each found source (click its marker); approved sources land in the EDITOR tab.",
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
  // a locally attached scan makes the book a verified source, whatever the
  // computed mark says
  if (row.localPdf) return "APPROVED";
  const m = computeMark(row).mark;
  if (m === "UPLOAD") return anyApprovedSource(row) ? "APPROVED" : "UPLOAD";
  return m || "NONE";
}

function markCell(row) {
  const { mark, reason } = computeMark(row);
  const attached = !!row.localPdf;
  // Clicking the mark opens the file browser to attach/replace a local PDF
  // (Shift+click detaches); an attached PDF shows as a download glyph in a
  // green-bordered tag, marking it as an approved verified source.
  const dot = attached ? { cls: "ok", tip: "PDF attached — verified source" } : null;
  const tail = attached
    ? `Attached PDF:\n${row.localPdf}\nClick to replace · Shift+click to detach`
    : "Click to attach a local PDF";
  let cls, label, base;
  if (mark === "SCAN") {
    // NF = not found: no scan exists anywhere, so this copy must be scanned
    cls = attached ? "approved" : "scan"; label = "NF"; base = reason;
  } else if (mark === "UPLOAD") {
    const ready = anyApprovedSource(row);
    cls = attached || ready ? "approved" : "upload"; label = BICONS.upload;
    base = ready ? "Verified source(s) ready — see the Editor tab" : reason;
  } else if (attached) {
    // no computed mark, but an attached scan keeps the row clickable/detachable
    cls = "approved"; label = "NF"; base = reason;
  } else {
    // no mark and nothing attached — not an attach target
    return badge("unknown", "—", { tip: reason });
  }
  return `<span data-scanattach="1">${badge(cls, label, {
    tip: base ? base + "\n" + tail : tail, dot })}</span>`;
}

// attach a local PDF scan to a SCAN-marked row: it becomes a verified
// source, ready to seed a new WHL entry in the Editor tab
function attachRowScan(id) {
  const row = state.rowsById.get(String(id));
  if (!row) return;
  // default to the Downloads folder filtered to recently downloaded PDFs; if
  // the row already has a scan, reopen at that file's folder (unfiltered)
  const start = row.localPdf ? row.localPdf.replace(/[\\/][^\\/]*$/, "") : "";
  openFileBrowser(start, (path) => setRowLocalPdf(id, path),
    { downloadsDefault: !start, recentMin: state.settings.scanRecentMin });
}

// PATCH a manual entry's local_pdf without touching its scans
async function patchManualLocalPdf(id, path) {
  const res = await fetch(`/api/manual/${encodeURIComponent(id)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    // attaching a scan doesn't change the book's identity: keep scans
    body: JSON.stringify({ local_pdf: path, _preserve: true }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok || !data.ok) return false;
  const i = state.manual.findIndex((x) => x.id === id);
  if (i >= 0) state.manual[i] = data.entry;
  renderChecked();
  renderUpload();
  return true;
}

async function setRowLocalPdf(id, path) {
  const row = state.rowsById.get(String(id));
  if (!row) return;
  const verb = path ? "attach scan" : "detach scan";
  if (row.kind === "manual") {
    const entry = state.manual.find((x) => x.id === id) || {};
    const prior = entry.local_pdf || "";
    if (!await patchManualLocalPdf(id, path)) { statusErr("ATTACH FAILED"); return; }
    pushOp(`${verb} ${(row.book.title || "").slice(0, 36)}`,
      () => patchManualLocalPdf(id, prior),
      () => patchManualLocalPdf(id, path),
      { kind: "manual-localpdf", id, before: prior });
  } else {
    const entry = state.checked.get(id);
    if (!entry) return;
    trackChecked(`${verb} ${(row.book.title || "").slice(0, 36)}`, id, () => {
      entry.local_pdf = path;
    });
    saveChecked();
  }
  renderChecked();
  renderUpload();
  status(path ? `Scan attached — verified source :: ${path}` : "Scan detached");
}

// --- combined checked-books + manual-entries table -----------------------------

function manualToBook(e) {
  const book = {
    title: e.title || "", subtitle: e.subtitle || "", author: e.author || "",
    year: e.year || "", edition: e.edition || "", volume: e.volume || "",
    group_id: e.group_id || "",
    publisher: e.publisher || "", city: e.city || "", language: e.language || "",
    pages: e.pages || "", condition: e.condition || "",
    illustrations: e.illustrations || "", price: e.price || "",
    acquired: "", categories: e.categories || "", notes: e.notes || "",
    category_ids: e.category_ids || [],
  };
  // non-column metadata (phone captures): shown in the Info panel
  if (e.extra && Object.keys(e.extra).length) book.extra = e.extra;
  if (e.images && e.images.length) book.images = e.images;
  if (e.capture_id) book.capture_id = e.capture_id;
  return book;
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
      captured: !!e.capture_id,
      edited: !!e.edited,
      checks: e.checks || null, scans: e.scans || null,
      verify: migrateVerify(e) || {},
      manualUrls: e.manual_urls || {},
      localPdf: e.local_pdf || "",
      attention: e.attention || "",   // "" / "1" / the reason text
    });
  }
  for (const [k, v] of state.checked.entries()) {
    rows.push({
      kind: "catalog", id: k, source: k.split(":")[0],
      captured: false,
      edited: !!v.edited,
      book: Object.assign({ subtitle: "", volume: "", language: "" }, v.book),
      checks: v.checks || null, scans: v.scans || null,
      verify: migrateVerify(v) || {},
      manualUrls: v.manual_urls || {},
      localPdf: v.local_pdf || "",
      attention: v.attention || "",   // "" / "1" / the reason text
    });
  }
  return rows;
}

// --- multi-volume sets -------------------------------------------------------
// Books carrying the same metadata group_id and a volume number render as a
// set. Membership belongs to each book; only presentation state (defined count
// and expansion) lives in settings.

function volNum(book) {
  const n = parseInt(book && book.volume, 10);
  return Number.isFinite(n) && n > 0 ? n : 0;
}

// the base title (volume stripped) used for display
function setBaseTitle(book) {
  let t = String((book && book.title) || "");
  const vt = extractVolTotal(t);       // trailing "N/M" / "N of M"
  if (vt && vt.clean) t = vt.clean;
  else { const v = extractVolume(t); if (v && v.clean) t = v.clean; }  // "vol N"
  return t.trim();
}

// stable default association for newly detected legacy volume sets
function setKeyOf(book) {
  return setBaseTitle(book).toLowerCase().replace(/\s+/g, " ");
}

// Editor builds carry their association explicitly. The title-derived key is
// only a default when a volume first enters the Editor; subsequent grouping
// reads group_id from the book metadata instead of guessing from duplicate
// titles on every render.
function buildGroupIdFor(book) {
  const explicit = String((book && book.group_id) || "").trim();
  return explicit || (volNum(book) > 0 ? setKeyOf(book) : "");
}

function groupIdOf(book) {
  return String((book && book.group_id) || "").trim();
}

function setsMap() {
  if (!state.settings.sets || typeof state.settings.sets !== "object")
    state.settings.sets = {};
  return state.settings.sets;
}
function setRec(key) { return setsMap()[key] || null; }
function setDefinedCount(key) {
  const r = setRec(key);
  return r && r.count > 0 ? r.count : 0;
}
function setExpanded(key) {
  const r = setRec(key);
  return r && typeof r.exp === "boolean" ? r.exp : !!state.settings.expandSets;
}
function setSetExpanded(key, val) {
  const m = setsMap();
  m[key] = Object.assign({}, m[key], { exp: !!val });
  saveSettings();
}
function setSetCount(key, count) {
  const m = setsMap();
  m[key] = Object.assign({}, m[key], { count: Math.max(0, count | 0) });
  saveSettings();
}

function firstVal(rows, f) {
  for (const r of rows) { const v = (r.book && r.book[f]) || ""; if (v) return v; }
  return "";
}

// group a flat (already filtered/sorted) row list into an ordered display list:
//   { type:"row", row } | { type:"set", key, title, author, publisher,
//                            count, expanded, vols:[row...] }
// Only rows that carry a volume number join a group; a group becomes a rendered
// set once it has >=2 volumes present OR a defined count of >=2.
function groupSets(rows) {
  const groups = new Map();
  for (const r of rows) {
    if (volNum(r.book) <= 0) continue;
    const key = groupIdOf(r.book);
    if (!key) continue;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(r);
  }
  const isSet = (key) =>
    (groups.get(key) || []).length >= 2 || setDefinedCount(key) >= 2;
  const out = [], emitted = new Set();
  for (const r of rows) {
    const key = volNum(r.book) > 0 ? groupIdOf(r.book) : null;
    if (key && isSet(key)) {
      if (emitted.has(key)) continue;   // emit the set at its first member
      emitted.add(key);
      const vols = groups.get(key).slice()
        .sort((a, b) => volNum(a.book) - volNum(b.book));
      const maxVol = vols.reduce((m, x) => Math.max(m, volNum(x.book)), 0);
      const count = Math.max(setDefinedCount(key), vols.length, maxVol);
      out.push({
        type: "set", key, title: setBaseTitle(vols[0].book),
        author: firstVal(vols, "author"), publisher: firstVal(vols, "publisher"),
        count, expanded: setExpanded(key), vols,
      });
    } else {
      out.push({ type: "row", row: r });
    }
  }
  return out;
}

// --- "needs attention" marks (press Q while hovering a row) ---------------------
// Checked/manual rows and builder entries persist their flag on the data;
// every other table keeps a lightweight browser-side mark keyed per row.

const ATTN_KEY = "whl_cad_attention_v1";

function loadAttn() {
  try { state.attn = JSON.parse(localStorage.getItem(ATTN_KEY) || "{}"); }
  catch (e) { state.attn = {}; }
}

function attnHas(k) { return !!(state.attn || {})[k]; }

// a mark's value is "" (unmarked), "1" (plain mark), or the reason text
function attnReason(v) {
  return typeof v === "string" && v && v !== "1" ? v : "";
}

function setAttnKey(k, val) {
  state.attn = state.attn || {};
  if (val) state.attn[k] = val;
  else delete state.attn[k];
  try { localStorage.setItem(ATTN_KEY, JSON.stringify(state.attn)); } catch (e) {}
  pushClientState("attention");
  status(val ? "Marked: needs attention" : "Attention mark cleared");
}

// Resolve whatever row (any table) or builder entry the mouse is over into
// an attention target: {label, current, apply(value)}. Q marks it and opens
// the reason popover on it.
function attnTargetAtHover() {
  const bi = document.querySelector("#ocr-books .build-item:hover");
  if (bi) {
    const b = state.builds[bi.dataset.bid];
    if (!b) return null;
    return {
      node: bi,
      kind: "build", ref: bi.dataset.bid,
      label: b.title || bi.dataset.bid,
      current: String(b.attention || ""),
      apply: (v) => patchBuildRaw(bi.dataset.bid, { attention: v })
        .then(() => status(v ? "Marked: needs attention" : "Attention mark cleared")),
    };
  }
  const tr = document.querySelector(
    "#checked-rows tr:hover, #whltop-rows tr:hover, " +
    "#upload-rows tr:hover, #bottom-rows tr:hover");
  if (!tr) return null;
  const host = tr.parentElement.id;
  if (host === "checked-rows" && tr.dataset.rowId) {
    const row = state.rowsById.get(String(tr.dataset.rowId));
    if (!row) return null;
    return {
      node: tr,
      kind: "row", ref: String(tr.dataset.rowId),
      label: row.book.title || tr.dataset.rowId,
      current: String(row.attention || ""),
      apply: (v) => setRowAttention(tr.dataset.rowId, v),
    };
  }
  const keyTarget = (k, label, rerender) => ({
    node: tr,
    kind: "key", ref: k,
    label,
    current: String((state.attn || {})[k] || ""),
    apply: (v) => { setAttnKey(k, v); rerender(); },
  });
  if (host === "whltop-rows" && tr.dataset.widx != null) {
    return keyTarget("whl:" + tr.dataset.widx,
      tr.children[1] ? tr.children[1].textContent : "WHL row", renderWhlTop);
  }
  if (host === "upload-rows" && tr.dataset.si != null) {
    const s = (state.uploadSources || [])[+tr.dataset.si];
    if (!s) return null;
    return keyTarget("src:" + (s.url || s.local_pdf || s.title),
      s.title || "source", renderUpload);
  }
  if (host === "bottom-rows" && tr.dataset.bi != null) {
    const rec = (state.bottomRecords || [])[+tr.dataset.bi];
    if (!rec) return null;
    if (rec._src === "manual" && rec._mid) {
      // master-list manual rows share the manual entry's persistent flag
      const e = state.manual.find((x) => x.id === rec._mid);
      return {
        node: tr,
        kind: "row", ref: String(rec._mid),
        label: rec.title || "manual entry",
        current: String((e && e.attention) || ""),
        apply: (v) => setRowAttention(rec._mid, v).then(renderBottomRows),
      };
    }
    return keyTarget(`${rec._src}:${rec._idx}`, rec.title || "row", renderBottomRows);
  }
  return null;
}

// Q on whatever the mouse is over: mark it as needing attention and open the
// reason popover so you can say why. An unmarked row is marked at once, so
// dismissing the popover still leaves the plain mark behind (what Q always did).
// Ctrl+Q is kept as an alias, but most browsers reserve it (Firefox: Quit), so
// it never reaches the page — which is why reason capture moved onto plain Q.
function onAttentionKey(ev) {
  if (ev.key !== "q" && ev.key !== "Q") return;
  if (/^(INPUT|TEXTAREA|SELECT)$/.test(ev.target.tagName) ||
      ev.target.isContentEditable) return;
  const target = attnTargetAtHover();
  if (!target) return;
  ev.preventDefault();
  // grab the anchor rect first: apply() re-renders some tables, detaching the row
  const rect = target.node ? target.node.getBoundingClientRect() : null;
  if (!target.current) target.apply("1");   // mark first; the reason is optional
  openAttnPop(target, rect);
}

// Resolve the row/entry under the mouse into its book fields (for the "S"
// Google-search shortcut).
function bookAtHover() {
  const bi = document.querySelector("#ocr-books .build-item:hover");
  if (bi) {
    const b = state.builds[bi.dataset.bid];
    if (b) return { title: b.title, author: b.authors, year: b.year,
      publisher: b.publisher, city: b.publisher_city, edition: b.edition };
  }
  const tr = document.querySelector(
    "#checked-rows tr:hover, #whltop-rows tr:hover, " +
    "#upload-rows tr:hover, #bottom-rows tr:hover");
  if (!tr) return null;
  const host = tr.parentElement.id;
  if (host === "checked-rows") {
    if (tr.classList.contains("set-header") && tr.dataset.setKey) {
      const vols = setMembers(tr.dataset.setKey);
      if (vols.length) return { title: setBaseTitle(vols[0].book),
        author: firstVal(vols, "author"), year: firstVal(vols, "year"),
        publisher: firstVal(vols, "publisher") };
    }
    if (tr.dataset.rowId) {
      const row = state.rowsById.get(String(tr.dataset.rowId));
      if (row) return { title: row.book.title, author: row.book.author,
        year: row.book.year, publisher: row.book.publisher, city: row.book.city,
        edition: row.book.edition, volume: row.book.volume };
    }
    return null;
  }
  if (host === "whltop-rows" && tr.dataset.widx != null) {
    const r = whlRowByIdx(parseInt(tr.dataset.widx, 10));
    if (r) return { title: r.title, author: r.authors, year: r.year,
      publisher: r.publisher, volume: r.volume };
  }
  if (host === "upload-rows" && tr.dataset.si != null) {
    const s = (state.uploadSources || [])[+tr.dataset.si];
    if (s) return { title: s.title, author: s.authors || s.author, year: s.year,
      publisher: s.publisher, edition: s.edition, volume: s.volume };
  }
  if (host === "bottom-rows" && tr.dataset.bi != null) {
    const rec = (state.bottomRecords || [])[+tr.dataset.bi];
    if (rec) return { title: rec.title, author: rec.author || rec.authors,
      year: rec.year || rec.first_year, publisher: rec.publisher,
      city: rec.city, edition: rec.edition, volume: rec.volume };
  }
  return null;
}

// S over any entry: Google the title + author + year, in the embedded web view
// Quote a value as a phrase for an Internet Archive field query.
function iaPhrase(s) { return '"' + String(s).replace(/["\\]+/g, " ").trim() + '"'; }

// Build an Internet Archive Advanced-Search URL from a book + the active search
// marks: title:(...) AND creator:(...) AND year:... AND volume:(...). Title is
// always included; the volume is matched whenever the book carries one.
function iaAdvancedUrl(b) {
  const on = searchGate();
  const author = b.author || b.authors || "";
  const ym = String(b.year || "").match(/\d{4}/);   // first 4-digit year of a range/prefix
  const year = ym ? ym[0] : "";
  const parts = [];
  if (b.title) parts.push("title:(" + iaPhrase(b.title) + ")");
  if (on("author", true) && author) parts.push("creator:(" + iaPhrase(author) + ")");
  if (on("year", true) && year) parts.push("year:" + year);
  if (on("publisher", false) && b.publisher) parts.push("publisher:(" + iaPhrase(b.publisher) + ")");
  if (b.volume) parts.push("volume:(" + iaPhrase(b.volume) + ")");
  return "https://archive.org/search?query=" + encodeURIComponent(parts.join(" AND "));
}

function onSearchKey(ev) {
  if (ev.key !== "s" && ev.key !== "S") return;
  if (ev.ctrlKey || ev.metaKey || ev.altKey) return;
  if (/^(INPUT|TEXTAREA|SELECT)$/.test(ev.target.tagName) || ev.target.isContentEditable) return;
  const b = bookAtHover();
  if (!b || !b.title) return;
  ev.preventDefault();
  // over an Internet Archive tag -> IA Advanced Search (marked terms + volume)
  if (document.querySelector('.tag-unit[data-vsrc="internet_archive"]:hover')) {
    window.open(iaAdvancedUrl(b), "_blank", "noopener");
    status("IA SEARCH :: " + String(b.title || "").slice(0, 55));
    return;
  }
  // otherwise Google title + author + year in a new browser tab (the embedded
  // view can't render Google — it blocks framing)
  const q = [b.title, b.author, b.year].map((x) => String(x || "").trim())
    .filter(Boolean).join(" ");
  const url = "https://www.google.com/search?q=" + encodeURIComponent(q);
  window.open(url, "_blank", "noopener");
  status("SEARCH :: " + q.slice(0, 60));
}

// --- the reason popover (Q) --------------------------------------------------
// A tooltip-shaped popover pinned to the marked row, hosting a word-wrapped
// textarea. Unlike #cad-tooltip it takes pointer events (you type in it), so it
// is a separate element and initTooltips' hideTip() must not reach it.

let attnPopTarget = null;
let attnPopRect = null;   // the anchor row's rect, captured before apply() re-renders it

function openAttnPop(target, rect) {
  attnPopTarget = target;
  attnPopRect = rect;
  const pop = el("attn-pop");
  el("attn-pop-label").textContent = (target.label || "").slice(0, 60);
  const ta = el("attn-pop-reason");
  ta.value = attnReason(target.current);
  // pre-tick when this item already sits in the shared review queue
  const rk = target.kind ? `${target.kind}:${target.ref}` : "";
  el("attn-pop-review").checked = !!(rk && Object.values(reviewsState.items || {})
    .some((r) => r.key === rk && r.status === "open"));
  pop.hidden = false;
  hideTip();                       // the hover tooltip would sit under it otherwise
  positionAttnPop();
  ta.focus();
  ta.setSelectionRange(ta.value.length, ta.value.length);
}

// pin below the anchor row, flipping up / clamping in at the viewport edges
function positionAttnPop() {
  const pop = el("attn-pop");
  const p = pop.getBoundingClientRect();
  const r = attnPopRect ||
    { left: innerWidth / 2, top: innerHeight / 2, bottom: innerHeight / 2 };
  let left = r.left;
  let top = r.bottom + 4;
  if (left + p.width > innerWidth - 8) left = Math.max(8, innerWidth - p.width - 8);
  if (top + p.height > innerHeight - 8) top = Math.max(8, r.top - p.height - 4);
  pop.style.left = Math.max(8, left) + "px";
  pop.style.top = top + "px";
}

// Escape / click-away / scroll leave the mark as it stands: the row was marked
// the moment the popover opened, so dismissing can never silently unmark it.
function closeAttnPop() {
  el("attn-pop").hidden = true;
  attnPopTarget = null;
  attnPopRect = null;
}

function saveAttnPop() {
  if (!attnPopTarget) return;
  const t = attnPopTarget;
  const reason = el("attn-pop-reason").value.trim();
  t.apply(reason || "1");   // empty reason = plain mark
  // "Needs review" raises (or refreshes) a shared queue item. Unticking never
  // withdraws one — resolution is explicit, in the queue itself.
  if (el("attn-pop-review").checked && t.kind) {
    fetch("/api/reviews", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kind: t.kind, ref: String(t.ref),
                             label: t.label || "", reason }),
    }).then(async (res) => {
      if (res.ok) {
        await loadReviews();
        renderHome();
        status("ADDED TO REVIEW QUEUE");
      } else {
        status("REVIEW REQUEST FAILED :: NOT QUEUED");
      }
    }).catch(() => status("REVIEW REQUEST FAILED :: NOT QUEUED"));
  }
  closeAttnPop();
}

function initAttnPop() {
  el("attn-pop-save").addEventListener("click", saveAttnPop);
  el("attn-pop-clear").addEventListener("click", () => {
    if (!attnPopTarget) return;
    attnPopTarget.apply("");
    closeAttnPop();
  });
  el("attn-pop-reason").addEventListener("keydown", (ev) => {
    ev.stopPropagation();            // Q / S / Delete / undo must not fire while typing
    if (ev.key === "Enter" && !ev.shiftKey) {
      ev.preventDefault();
      saveAttnPop();
    } else if (ev.key === "Escape") {
      closeAttnPop();
    }
  });
  // click anywhere outside dismisses (the mark survives)
  document.addEventListener("mousedown", (ev) => {
    if (el("attn-pop").hidden) return;
    if (!(ev.target.closest && ev.target.closest("#attn-pop"))) closeAttnPop();
  });
  // scrolling moves the anchor row out from under the popover -- but the
  // textarea's own overflow scroll must not close it
  document.addEventListener("scroll", (ev) => {
    if (el("attn-pop").hidden) return;
    const t = ev.target;
    if (t && t.closest && t.closest("#attn-pop")) return;
    closeAttnPop();
  }, true);
  addEventListener("resize", () => {
    if (!el("attn-pop").hidden) positionAttnPop();
  });
}

// --- the review queue ---------------------------------------------------------
// Items flagged "Needs review" in the Q popover. Server-backed and shared:
// every contributor sees the same queue, comments under their own name
// (Settings > Your name), and an explicit resolution closes the item and
// clears the underlying attention mark.

const reviewsState = { items: {}, loaded: false, showResolved: false };

async function loadReviews() {
  try {
    const r = await (await fetch("/api/reviews")).json();
    // a failed fetch keeps the last-known queue rather than blanking it —
    // "0 items awaiting review" must never be a euphemism for "server error"
    if (r.ok) reviewsState.items = r.reviews || {};
    reviewsState.loaded = true;
  } catch (e) { /* keep the last-known queue */ }
}

function openReviewWin() {
  el("review-overlay").hidden = false;
  renderReviewList();                       // instant paint from what we have
  loadReviews().then(renderReviewList);     // then freshen from the server
}
function closeReviewWin() { el("review-overlay").hidden = true; }

function reviewsSorted() {
  const all = Object.values(reviewsState.items || {});
  const open = all.filter((r) => r.status === "open")
    .sort((a, b) => (b.created_at || "").localeCompare(a.created_at || ""));
  if (!reviewsState.showResolved) return open;
  return open.concat(all.filter((r) => r.status !== "open")
    .sort((a, b) => (b.resolved_at || "").localeCompare(a.resolved_at || "")));
}

function reviewItemHtml(r) {
  const resolved = r.status !== "open";
  return `<div class="review-item${resolved ? " resolved" : ""}" data-rid="${esc(r.id)}">` +
    `<div class="ri-head">` +
      `<span class="ri-label">${esc(r.label) || "(unlabelled item)"}</span>` +
      `<span class="ri-meta">${resolved
        ? `resolved by ${esc(r.resolved_by || "?")} &middot; ${esc(relIso(r.resolved_at))}`
        : `${esc(r.created_by || "?")} &middot; ${esc(relIso(r.created_at))}`}</span>` +
      `<button class="cad-btn tiny" type="button" data-rv-resolve="${resolved ? "0" : "1"}" ` +
        `data-tip="${resolved ? "Reopen this item"
          : "Mark resolved (also clears the attention mark)"}">${resolved ? "Reopen" : "Resolve"}</button>` +
    `</div>` +
    (r.reason ? `<div class="ri-reason">${esc(r.reason)}</div>` : "") +
    (r.comments || []).map((c) => `<div class="ri-comment">` +
      `<span class="ric-author">${esc(c.author || "?")}</span>` +
      `<span class="ric-when">${esc(relIso(c.ts))}</span>` +
      `<div class="ric-text">${esc(c.text)}</div></div>`).join("") +
    `<div class="ri-add">` +
      `<input class="cad-input ri-comment-input" placeholder="Add a comment&hellip;" spellcheck="false" />` +
      `<button class="cad-btn tiny" type="button" data-rv-comment>Comment</button>` +
    `</div></div>`;
}

// Render the queue into one host (the overlay list or the home pane). A rebuild
// must never clobber a comment in progress, so typed drafts and the caret are
// carried across the innerHTML replacement, per host.
function renderReviewsInto(host) {
  if (!host) return;
  const drafts = {};
  let focusRid = null;
  for (const i of host.querySelectorAll(".ri-comment-input")) {
    const it = i.closest(".review-item");
    if (!it) continue;
    if (i.value.trim()) drafts[it.dataset.rid] = i.value;
    if (i === document.activeElement) focusRid = it.dataset.rid;
  }
  const items = reviewsSorted();
  if (!items.length) {
    host.innerHTML = `<div class="empty">${reviewsState.showResolved
      ? "No review items yet" : "Nothing awaiting review"}</div>`;
    return;
  }
  host.innerHTML = items.map(reviewItemHtml).join("");
  for (const [rid, val] of Object.entries(drafts)) {
    const inp = host.querySelector(`.review-item[data-rid="${CSS.escape(rid)}"] .ri-comment-input`);
    if (inp) inp.value = val;
  }
  if (focusRid) {
    const inp = host.querySelector(`.review-item[data-rid="${CSS.escape(focusRid)}"] .ri-comment-input`);
    if (inp) { inp.focus(); inp.setSelectionRange(inp.value.length, inp.value.length); }
  }
}

// The queue lives in two places at once — the overlay window and the home
// pane — so a change in either refreshes both.
function renderReviewList() {
  const cb = el("review-show-resolved");
  if (cb) cb.checked = reviewsState.showResolved;
  const hcb = el("home-review-resolved");
  if (hcb) hcb.checked = reviewsState.showResolved;
  renderReviewsInto(el("review-list"));
  renderReviewsInto(el("home-reviews"));
}

// resolving a review also clears the underlying attention mark, wherever
// that mark lives (attn map / manual row / checked book / editor build)
async function clearMark(kind, ref) {
  if (kind === "key") {
    setAttnKey(ref, "");
    // repaint whichever table bakes this mark into its rows (the tab-switch
    // renders only cover the checked/upload tables)
    if (ref.startsWith("whl:")) renderWhlTop();
    else if (ref.startsWith("src:")) renderUpload();
    else renderBottomRows();
    return;
  }
  if (kind === "build") {
    if ((state.builds || {})[ref]) await patchBuildRaw(ref, { attention: "" });
    return;
  }
  if (kind !== "row") return;
  if (state.rowsById && state.rowsById.get(String(ref))) {
    await setRowAttention(ref, "");
    return;
  }
  // the combined table may not have rendered this session — go to the data
  const e = (state.manual || []).find((x) => x.id === ref);
  if (e) {
    await fetch(`/api/manual/${encodeURIComponent(ref)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ attention: "", _preserve: true }),
    }).catch(() => {});
    e.attention = "";
    return;
  }
  const v = state.checked && state.checked.get(ref);
  if (v) { v.attention = ""; saveChecked(); }
}

// Enter in a comment box posts it (the button holds the actual logic).
function onReviewKeydown(ev) {
  if (ev.key === "Enter" && ev.target.classList.contains("ri-comment-input")) {
    ev.preventDefault();
    const btn = ev.target.closest(".review-item").querySelector("[data-rv-comment]");
    if (btn) btn.click();
  }
}

// Resolve / reopen / comment — shared by the overlay list and the home pane.
async function onReviewClick(ev) {
  const item = ev.target.closest(".review-item");
  if (!item) return;
  const rid = item.dataset.rid;
  const rbtn = ev.target.closest("[data-rv-resolve]");
  if (rbtn) {
    const resolved = rbtn.dataset.rvResolve === "1";
    const res = await fetch(`/api/reviews/${encodeURIComponent(rid)}/resolve`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ resolved }),
    }).catch(() => null);
    if (!res || !res.ok) {
      status(res && res.status === 409
        ? "This item already has an open review"
        : "Review update failed");
      return;
    }
    let note = resolved ? "Review resolved" : "Review reopened";
    const r = (reviewsState.items || {})[rid];
    if (resolved && r) {
      // the review IS resolved at this point; a failed mark-clear must not
      // abort the refresh below, only be reported
      try { await clearMark(r.kind, r.ref); }
      catch (e) { note = "Review resolved — attention mark not cleared"; }
    }
    await loadReviews();
    renderReviewList();
    renderHome();
    status(note);
    return;
  }
  if (ev.target.closest("[data-rv-comment]")) {
    const input = item.querySelector(".ri-comment-input");
    const text = (input.value || "").trim();
    if (!text) return;
    const res = await fetch(`/api/reviews/${encodeURIComponent(rid)}/comment`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    }).catch(() => null);
    if (res && res.ok) {
      input.value = "";   // posted — the draft is a comment now
      await loadReviews();
      renderReviewList();
    } else {
      status("COMMENT FAILED :: NOT SAVED");
    }
  }
}

function initReviewWin() {
  el("review-close").addEventListener("click", closeReviewWin);
  el("review-overlay").addEventListener("mousedown", (ev) => {
    if (ev.target === el("review-overlay")) closeReviewWin();
  });
  const onShowResolved = (ev) => {
    reviewsState.showResolved = ev.target.checked;
    renderReviewList();
  };
  el("review-show-resolved").addEventListener("change", onShowResolved);
  const hcb = el("home-review-resolved");
  if (hcb) hcb.addEventListener("change", onShowResolved);
  // the queue is interactive in both places, so bind both hosts
  for (const host of [el("review-list"), el("home-reviews")]) {
    if (!host) continue;
    host.addEventListener("keydown", onReviewKeydown);
    host.addEventListener("click", onReviewClick);
  }
}

async function setRowAttention(id, value) {
  const row = state.rowsById.get(String(id));
  if (!row) return;
  if (row.kind === "manual") {
    const res = await fetch(`/api/manual/${encodeURIComponent(id)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ attention: value, _preserve: true }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || !data.ok) return;
    const i = state.manual.findIndex((x) => x.id === id);
    if (i >= 0) state.manual[i] = data.entry;
  } else {
    const entry = state.checked.get(id);
    if (!entry) return;
    entry.attention = value;
    saveChecked();
  }
  renderChecked();
  status(value ? "Marked: needs attention" : "Attention mark cleared");
}

// one-time (idempotent) migration: volume / edition / subtitle indicators in
// stored titles move into their own fields. Checked entries live in
// localStorage; manual entries persist via a scan-preserving PATCH.
function migrateParsedChecked() {
  let changed = 0;
  for (const v of state.checked.values()) {
    const before = Object.assign({ subtitle: "", volume: "", edition: "" }, v.book);
    const after = parseBook(before);
    if (bookParseChanged(before, after)) {
      v.book = Object.assign({}, v.book, {
        title: after.title, subtitle: after.subtitle,
        volume: after.volume, edition: after.edition,
      });
      changed++;
    }
  }
  if (changed) saveChecked();
  return changed;
}

async function migrateParsedManual() {
  let changed = 0;
  for (const e of state.manual.slice()) {
    const before = manualToBook(e);
    const after = parseBook(before);
    if (!bookParseChanged(before, after)) continue;
    const res = await fetch(`/api/manual/${encodeURIComponent(e.id)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        title: after.title, subtitle: after.subtitle,
        volume: after.volume, edition: after.edition,
        _preserve: true,   // identity unchanged — keep checks/scans/verify
      }),
    });
    const data = await res.json().catch(() => ({}));
    if (res.ok && data.ok) {
      const i = state.manual.findIndex((x) => x.id === e.id);
      if (i >= 0) state.manual[i] = data.entry;
      changed++;
    }
  }
  return changed;
}

// One-time: persist group_id on legacy volume-bearing books and retain their
// existing set count/expansion presentation settings.
async function backfillSets() {
  if (state.settings.groupMetadataBackfilled) return 0;
  const rows = combinedRows();
  if (!rows.length) return 0;                 // data not ready — retry on a later pass
  const maxByKey = new Map();
  let checkedChanged = false;
  let migrationComplete = true;
  for (const r of rows) {
    const v = volNum(r.book);
    if (v <= 0) continue;
    const key = groupIdOf(r.book) || setKeyOf(r.book);
    if (!key) continue;
    if (!groupIdOf(r.book)) {
      if (r.kind === "manual") {
        try {
          const res = await fetch(`/api/manual/${encodeURIComponent(r.id)}`, {
            method: "PATCH", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ group_id: key, _preserve: true }),
          });
          const data = await res.json().catch(() => ({}));
          const i = state.manual.findIndex((e) => e.id === r.id);
          if (res.ok && data.ok && i >= 0) state.manual[i] = data.entry;
          else { migrationComplete = false; continue; }
        } catch (e) { migrationComplete = false; continue; }
      } else {
        const entry = state.checked.get(r.id);
        if (!entry) continue;
        entry.book = Object.assign({}, entry.book, { group_id: key });
        checkedChanged = true;
      }
    }
    maxByKey.set(key, Math.max(maxByKey.get(key) || 0, v));
  }
  if (checkedChanged) saveChecked();
  const m = setsMap();
  let n = 0;
  for (const [key, maxVol] of maxByKey) {
    if (maxVol < 2) continue;                 // a lone volume 1 is not a set
    const want = Math.max(setDefinedCount(key), maxVol);
    if (setDefinedCount(key) !== want) { m[key] = Object.assign({}, m[key], { count: want }); n++; }
  }
  state.settings.groupMetadataBackfilled = migrationComplete;
  saveSettings();                             // one persist for the whole backfill
  return n;
}

async function migrateParsedEntries() {
  const n = migrateParsedChecked() + (await migrateParsedManual());
  const s = await backfillSets();
  if (n || s) {
    renderChecked();
    const bits = [];
    if (n) bits.push(`parsed volume/edition/subtitle out of ${n} title${n > 1 ? "s" : ""}`);
    if (s) bits.push(`grouped ${s} multi-volume set${s > 1 ? "s" : ""}`);
    status(bits.join(" · ").replace(/^./, (c) => c.toUpperCase()));
  }
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

// download state of a row's IA source: "done" | "failed" | "downloading" | ""
function dlState(row) {
  const ident = iaIdentifierForRow(row);
  if (!ident) return "";
  const dl = state.downloads.get(ident);
  if ((dl && dl.status === "done") || state.downloadedIds.has(ident)) return "done";
  if (dl && dl.status === "error") return "failed";
  if (dl && dl.status === "downloading") return "downloading";
  return "";
}

function iaCell(row) {
  // download state colours the tag's 2px border: green = downloaded (the tag
  // also shows a download glyph), red = failed, amber = in progress
  const ident = iaIdentifierForRow(row);
  const st = dlState(row);
  let dot = null;
  if (st === "done") {
    dot = { cls: "ok", tip: "Saved: downloads/ia/" + ident + ".pdf" };
  } else if (st === "failed") {
    dot = { cls: "err",
            tip: (state.downloads.get(ident) || {}).error || "Download failed" };
  } else if (st === "downloading") {
    dot = { cls: "prog",
            tip: "Downloading — " + dlPct(state.downloads.get(ident)) };
  }
  return scanBadge(row, "internet_archive", dot);
}

const TIP_FIELDS = [
  ["title", "Title"], ["subtitle", "Subtitle"], ["author", "Author"],
  ["publisher", "Publisher"], ["city", "City"], ["year", "Year"],
  ["edition", "Edition"], ["volume", "Volume"], ["language", "Language"],
  ["pages", "Pages"], ["subject", "Subject"], ["condition", "Condition"],
  ["price", "Price"], ["illustrations", "Illustrations"],
  ["categories", "Categories"], ["notes", "Notes"], ["status", "Status"],
  ["acquired", "Acquired"], ["url", "URL"],
];

function recordTip(rec, header) {
  const lines = header ? [header] : [];
  for (const [k, label] of TIP_FIELDS) {
    let v = k === "categories"
      ? bookCatsText(rec)
      : (rec[k] || "").toString().trim();
    if (!v) continue;
    // keep the tooltip a manageable size: long fields (notes, descriptions)
    // are abbreviated
    const cap = (k === "notes" || k === "description") ? 90 : 140;
    if (v.length > cap) v = v.slice(0, cap).trimEnd() + " …";
    lines.push(`${label}: ${v}`);
  }
  return lines.join("\n");
}

function updateCheckedCount() {
  el("checked-count").textContent =
    `${state.checked.size} checked / ${state.manual.length} manual`;
}

// the FIND box + the filter menu, applied identically for display and export
// the first 4-digit year in a value ("c1887", "1887-90" -> 1887), or null
function parseYearNum(y) {
  const m = String(y == null ? "" : y).match(/\d{4}/);
  return m ? parseInt(m[0], 10) : null;
}
// inclusive year-range filter shared by both top tables. With no bounds set,
// everything passes; with a bound set, rows whose year can't be parsed are out.
function yearInRange(y) {
  const from = state.settings.yearFrom, to = state.settings.yearTo;
  if (from == null && to == null) return true;
  const n = parseYearNum(y);
  if (n == null) return false;
  if (from != null && n < from) return false;
  if (to != null && n > to) return false;
  return true;
}

// reflect the persisted year-range into the toolbar inputs (init + after the
// server copy of settings is adopted)
function syncYearFilterInputs() {
  const f = el("year-from"), t = el("year-to");
  if (f) f.value = state.settings.yearFrom != null ? state.settings.yearFrom : "";
  if (t) t.value = state.settings.yearTo != null ? state.settings.yearTo : "";
}

function filteredCheckedRows(prebuilt) {
  // prebuilt lets renderChecked reuse the combinedRows() array it already
  // built for rowsById, instead of rescanning all entries a second time.
  let rows = prebuilt || combinedRows();
  const q = findQuery();
  if (!q.empty)
    rows = rows.filter((r) => matchesFind(
      q, `${r.book.title} ${r.book.subtitle || ""}`, r.book.author, r.book.year));
  rows = rows.filter((r) => yearInRange(r.book.year));
  const mf = state.settings.markFilter || "ALL";
  if (mf !== "ALL") rows = rows.filter((r) => rowMarkState(r) === mf);
  const sf = state.settings.srcFilter || "ALL";
  if (sf === "MANUAL") rows = rows.filter((r) => r.kind === "manual");
  else if (sf === "CATALOG") rows = rows.filter((r) => r.kind !== "manual");
  const df = state.settings.dlFilter || "ALL";
  if (df === "DONE") rows = rows.filter((r) => dlState(r) === "done");
  else if (df === "FAILED") rows = rows.filter((r) => dlState(r) === "failed");
  else if (df === "NOT") rows = rows.filter((r) => dlState(r) !== "done");
  return rows;
}

// build a normal (or volume-child) row <tr>
function checkedRowTr(row, cmode, opts) {
  opts = opts || {};
  const b = row.book;
  const tr = document.createElement("tr");
  tr.dataset.rowId = row.id;
  if (opts.isVol) {
    tr.classList.add("set-vol", "set-open");
    if (opts.setKey) tr.dataset.setKey = opts.setKey;
  }
  if (row.attention) {
    tr.classList.add("attention");
    const why = attnReason(row.attention);
    if (why) tr.dataset.tip = "Needs attention: " + why;
  }
  if (cmode === "search" && state.checkedSelected === String(row.id))
    tr.classList.add("whl-selected");
  // volume titles are implied by the set header — optionally hide them
  const hideTitle = opts.isVol && !!state.settings.hideVolTitles;
  const cell = (f) => {
    const val = f === "title" && hideTitle ? ""
      : f === "categories" ? bookCatsText(b) : b[f];
    const classes = [], attrs = [];
    if (cmode !== "search" && !(row.kind === "manual" && f === "acquired")) {
      classes.push("editable");
      attrs.push(`data-edit="${f}"`);
    }
    const coreLabel = { year: "date", author: "author", publisher: "publisher" }[f];
    if (coreLabel && !String(val || "").trim()) {
      classes.push("missing-core");
      attrs.push(`data-tip="Missing ${coreLabel}"`);
    }
    if (f === "title" && cmode === "search") attrs.push('data-csearch="1"');
    return `<td${classes.length ? ` class="${classes.join(" ")}"` : ""}` +
      `${attrs.length ? " " + attrs.join(" ") : ""}>${esc(val)}</td>`;
  };
  tr.innerHTML = `
    <td class="col-src">${rowSourceMark(row)}</td>
    ${BOOK_COLS.map(cell).join("\n      ")}
    <td class="col-whl">${imgCell(row)}</td>
    <td class="col-whl">${copyrightCell(row)}</td>
    <td class="col-whl">${whlBadge(row)}</td>
    <td class="col-whl">${iaCell(row)}</td>
    <td class="col-whl">${scanBadge(row, "hathitrust")}</td>
    <td class="col-whl">${markCell(row)}</td>`;
  scDecorateCheckedRow(tr, row);
  return tr;
}

function rowSourceMark(row) {
  let glyph, label;
  if (row.captured) {
    glyph = ICONS.camera;
    label = "Phone capture";
  } else if (row.kind === "manual") {
    glyph = ICONS.manual;
    label = "Manual entry";
  } else if (row.source === "ch_library") {
    glyph = ICONS.listfile;
    label = "Master list";
  } else {
    glyph = esc(String(row.source || "catalog").toUpperCase());
    label = String(row.source || "Catalog");
  }
  const edited = row.edited
    ? '<span class="src-edited" aria-label="edited">*</span>' : "";
  return `<span class="src-mark" data-tip="${esc(label)}${row.edited ? " — edited" : ""}">` +
    `${glyph}${edited}</span>`;
}

// tiny image marker: present when the entry has photos; click -> Info panel
function imgCell(row) {
  const imgs = (row.book && row.book.images) || [];
  if (!imgs.length) return "";
  const n = imgs.length;
  return `<span class="img-flag" data-imginfo="1" ` +
    `data-tip="${n} photo${n > 1 ? "s" : ""} — click to view in Info">` +
    `${ICONS.image}</span>`;
}

// build a set-header <tr>: colored tag, drop-down arrow, base title + (N)
function checkedSetHeaderTr(item, cmode) {
  const tr = document.createElement("tr");
  tr.className = "set-header" + (item.expanded ? " set-open" : "");
  tr.dataset.setKey = item.key;
  const arrow = item.expanded ? "▾" : "▸";   // down / right triangle
  const titleCell =
    `<td class="set-title-cell">` +
      `<span class="set-arrow">${arrow}</span>` +
      `<span class="set-title">${esc(item.title)}</span> ` +
      `<span class="set-count">(${item.count})</span></td>`;
  const cells = BOOK_COLS.map((f) =>
    f === "title" ? titleCell
      : f === "author" ? `<td>${esc(item.author)}</td>`
      : f === "publisher" ? `<td>${esc(item.publisher)}</td>`
      : "<td></td>").join("\n      ");
  tr.innerHTML = `
    <td class="set-src"><span class="set-tag" title="Multi-volume set"></span></td>
    ${cells}
    <td class="col-whl"></td>
    <td class="col-whl"></td>
    <td class="col-whl"></td>
    <td class="col-whl"></td>
    <td class="col-whl"></td>
    <td class="col-whl"></td>`;
  return tr;
}

// Chunked ("streaming") table rendering. Render the first STREAM_CHUNK rows
// now, then append the next chunk as the user scrolls toward the bottom — an
// IntersectionObserver watches the tail and pulls the next chunk a screenful
// early (rootMargin). This replaces the old fixed maxRows cap: every row is
// reachable, but only a viewport-plus-buffer's worth sit in the DOM at first,
// so a table of thousands opens instantly instead of building every <tr> up
// front. items[] is the full ordered list; renderItem(item, index) returns a
// <tr>. The delegated table click/edit handlers are unaffected — they read
// dataset off whatever rows are currently in the DOM.
const STREAM_CHUNK = 200;
function streamRows(tbody, items, renderItem) {
  const pane = tbody.closest(".drafting");   // the scroll container
  if (tbody._streamCleanup) tbody._streamCleanup();   // detach the previous render's listener
  tbody.innerHTML = "";
  let rendered = 0;
  const appendChunk = () => {
    const end = Math.min(rendered + STREAM_CHUNK, items.length);
    const frag = document.createDocumentFragment();
    const fresh = [];
    for (let i = rendered; i < end; i++) {
      const tr = renderItem(items[i], i);
      if (tr) { frag.appendChild(tr); fresh.push(tr); }
    }
    tbody.appendChild(frag);
    rendered = end;
    // carry the hidden-column mask onto the new rows (applyTableChrome only ran
    // over the first chunk); no-op when nothing is hidden or the table is fresh.
    applyColHide(tbody.closest("table"), fresh);
  };
  // A scroll listener, not an IntersectionObserver: IO does not fire for
  // table-row targets, and a <tbody> can't hold a non-table element to observe
  // instead. Append the next chunk whenever the pane is scrolled within 800px
  // of the bottom (and, on first render, until 200 rows overflow the pane).
  // clientHeight>0 guards a hidden pane (a re-render behind another tab), where
  // "near bottom" would otherwise be trivially true and load every row.
  const nearBottom = () => !pane ||
    (pane.clientHeight > 0 && pane.scrollTop + pane.clientHeight >= pane.scrollHeight - 800);
  const fill = () => { while (rendered < items.length && nearBottom()) appendChunk(); };
  if (pane) pane.addEventListener("scroll", fill, { passive: true });
  tbody._streamCleanup = () => {
    if (pane) pane.removeEventListener("scroll", fill);
    tbody._streamCleanup = null;
  };
  appendChunk();   // first chunk now
  fill();          // top up until the (visible) pane is filled
}

function renderChecked() {
  // Background re-renders must not destroy an in-progress cell edit.
  const active = document.activeElement;
  if (active && active.classList && active.classList.contains("cell-edit")) return;
  updateCheckedCount();
  const tbody = el("checked-rows");
  const allRows = combinedRows();           // one scan, reused by the filter below
  state.rowsById = new Map(allRows.map((r) => [String(r.id), r]));
  // a deleted/unchecked row can no longer be the repopulation target
  if (state.checkedSelected != null &&
      !state.rowsById.has(String(state.checkedSelected))) {
    state.checkedSelected = null;
  }
  // Skip the expensive DOM teardown+rebuild while the checked table is not
  // visible — a background download/scan re-render fired while the user is on
  // another tab, or while the WHL table is shown. state.rowsById (above) stays
  // fresh so lookups remain correct; every path that reveals the table
  // (tab click -> renderChecked at initTabs, switchTopTable -> renderTop)
  // re-renders it, so nothing is left stale.
  if (!el("checked").classList.contains("active") || el("checked-pane").hidden)
    return;
  const cmode = checkedMode();
  let rows = filteredCheckedRows(allRows);
  const so = state.sort.checked;
  if (so) rows = sortRowsBy(rows, (r) => checkedSortVal(r, so.key), so.dir);

  el("checked-empty").hidden = rows.length !== 0;

  // flatten sets (a header + its expanded volumes) into one ordered list so the
  // streamer can chunk across set boundaries.
  const items = [];
  for (const item of groupSets(rows)) {
    if (item.type === "set") {
      items.push({ set: item });
      if (item.expanded) {
        item.vols.forEach((vr, i) =>
          items.push({ vr, setKey: item.key, last: i === item.vols.length - 1 }));
      }
    } else {
      items.push({ row: item.row });
    }
  }
  streamRows(tbody, items, (d) => {
    if (d.set) return checkedSetHeaderTr(d.set, cmode);
    if (d.vr) {
      const tr = checkedRowTr(d.vr, cmode, { isVol: true, setKey: d.setKey });
      if (d.last) tr.classList.add("set-last");
      return tr;
    }
    return checkedRowTr(d.row, cmode, {});
  });

  applyTableChrome("checked");
  markSortHeaders("checked");
  refreshInfoIfActive();
  renderBottomPane();
}

// rAF-coalesced renderChecked for high-frequency BACKGROUND callers (the IA
// download and auto-scan pollers): repeated calls in one frame collapse to a
// single rebuild, and while the window is hidden the frame never fires at all.
// User-action callers keep calling renderChecked() directly — some read
// state.rowsById synchronously right afterwards, so they must not be deferred.
let _renderCheckedFrame = null;
function scheduleRenderChecked() {
  if (_renderCheckedFrame !== null) return;
  _renderCheckedFrame = requestAnimationFrame(() => {
    _renderCheckedFrame = null;
    renderChecked();
  });
}

// One delegated handler covers verify markers / delete / uncheck / edit clicks.
function onCheckedClick(ev) {
  const setHdr = ev.target.closest("tr.set-header");
  if (ev.ctrlKey || ev.metaKey) {
    if (setHdr) { ev.preventDefault(); openSetEditTab(setHdr.dataset.setKey); return; }
    const tr = ev.target.closest("tr");
    if (tr && tr.dataset.rowId) {
      ev.preventDefault();
      openBookEditTab(tr.dataset.rowId);
    }
    return;
  }
  // plain click anywhere on a set header (arrow, tag, title) expands/collapses it
  if (setHdr) { toggleSet(setHdr.dataset.setKey); return; }
  const mark = ev.target.closest(".vmark");
  if (mark) {
    const unit = mark.closest("[data-vsrc]");
    const tr = mark.closest("tr");
    if (unit && tr) cycleVerify(tr.dataset.rowId, unit.dataset.vsrc);
    return;
  }
  // SCAN mark: attach a local scan PDF (the row becomes a verified source);
  // Shift+click detaches an attached scan
  const scanTag = ev.target.closest("[data-scanattach]");
  if (scanTag) {
    const tr = scanTag.closest("tr");
    if (tr) {
      const row = state.rowsById.get(String(tr.dataset.rowId));
      if (ev.shiftKey && row && row.localPdf) setRowLocalPdf(tr.dataset.rowId, "");
      else attachRowScan(tr.dataset.rowId);
    }
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
  // the Img marker: open this entry's photos in the Info panel
  const imf = ev.target.closest("[data-imginfo]");
  if (imf) {
    const tr = imf.closest("tr");
    if (tr && tr.dataset.rowId) {
      state.editTarget = { kind: "row", id: String(tr.dataset.rowId) };
      switchPaneTab("pane-info");
    }
    return;
  }
  // search mode: clicking a title looks it up on Open Library
  const cs = ev.target.closest("td[data-csearch]");
  if (cs) {
    const tr = cs.closest("tr");
    if (tr) selectCheckedSearchRow(tr.dataset.rowId);
    return;
  }
  const td = ev.target.closest("td[data-edit]");
  if (td) startEdit(td);
}

// The selected row's fields that can become search terms. Ctrl+click a top
// column to add/drop it (see initSortHeaders). Title is always the base term;
// The Open Library / WHL / IA search is constrained to the fields turned on in
// state.settings.searchCons (persistent). Title is always the base term; a
// "title" constraint makes it a verbatim phrase. The Title/Author/Year
// checkboxes and Ctrl+click on any column header both toggle searchCons.
// (Language is intentionally excluded — no search path honors it.)
const SEARCH_MARK_FIELDS = new Set(
  ["title", "author", "year", "publisher", "city", "edition", "volume"]);
const SEARCH_CONS_BOXES = [["wc-title", "title"], ["wc-author", "author"], ["wc-year", "year"]];
function searchMarkKey(colKey) { return colKey === "authors" ? "author" : colKey; }

function searchGate() {
  const cons = state.settings.searchCons || {};
  return (k) => !!cons[k];
}

// reflect searchCons into the toolbar checkboxes (init / after Ctrl+click / adopt)
function syncSearchConsCheckboxes() {
  const cons = state.settings.searchCons || {};
  for (const [id, k] of SEARCH_CONS_BOXES) {
    const e = el(id);
    if (e) e.checked = !!cons[k];
  }
}

// Build the OL/WHL search override from a selected row's fields + the constraints.
function searchOverrideFrom(f) {
  const on = searchGate();
  const ov = { title: f.title || "", verbatim: on("title") };
  if (on("author") && f.author) ov.author = f.author;
  if (on("year") && f.year) ov.year = f.year;
  if (on("publisher") && f.publisher) ov.publisher = f.publisher;
  if (on("city") && f.city) ov.city = f.city;
  if (on("edition") && f.edition) ov.edition = f.edition;
  // a volume always narrows the match to its volume number when present
  if (f.volume) ov.volume = f.volume;
  return ov;
}

// Re-derive the active search from the selected row after the constraints change.
function rebuildSearchFromMarks() {
  syncSearchConsCheckboxes();
  if (state.settings.topTable === "whl" && state.whlSelected != null)
    selectWhlSearchRow(state.whlSelected);
  else if (state.settings.topTable === "checked" && state.checkedSelected != null)
    selectCheckedSearchRow(state.checkedSelected);
  else { markSortHeaders("checked"); markSortHeaders("whl"); }
}

function selectCheckedSearchRow(id) {
  const row = state.rowsById.get(String(id));
  if (!row) return;
  state.checkedSelected = String(id);
  state.editTarget = null;   // a live search selection supersedes a prior edit target (Info pane)
  const b = row.book;
  state.olOverride = searchOverrideFrom({
    title: b.title, author: b.author, year: b.year, publisher: b.publisher,
    city: b.city, edition: b.edition, volume: b.volume,
  });
  setSearchPane(true);
  const tabs = bottomTabs();
  let i = tabs.indexOf("ol");
  if (i < 0) { tabs.push("ol"); i = tabs.length - 1; }
  state.settings.bottomActive = i;
  saveSettings();
  renderChecked();
  renderBottomPane().then(olRealtime);
  status(`OPEN LIBRARY SEARCH :: ${row.book.title}`);
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

// Delete key while hovering a checked-table row trashes that entry — the
// reversible replacement for the removed Actions column (undo restores it).
function onRowDeleteKey(ev) {
  if (ev.key !== "Delete" || ev.repeat) return;   // ignore key auto-repeat (a hold = one delete)
  if (/^(INPUT|TEXTAREA|SELECT)$/.test(ev.target.tagName) || ev.target.isContentEditable) return;
  // don't delete a row out from under an open floating popup (it doesn't cover
  // the rows the way a full-screen .overlay modal does)
  const pm = el("popup-menu"), asp = el("adv-search-pop");
  if ((pm && !pm.hidden) || (asp && !asp.hidden)) return;
  const tr = document.querySelector("#checked-rows tr:hover");
  if (!tr || !tr.dataset.rowId) return;   // real/volume rows only (not set headers)
  const row = state.rowsById.get(String(tr.dataset.rowId));
  if (!row) return;
  ev.preventDefault();
  if (row.kind === "manual") deleteManual(row.id);   // both are undoable
  else uncheckRow(row.id);
}

// --- click-to-edit cells --------------------------------------------------------

function startEdit(td) {
  if (td.querySelector("input")) return;
  const tr = td.closest("tr");
  const row = state.rowsById.get(String(tr.dataset.rowId));
  if (!row) return;
  const field = td.dataset.edit;
  // categories are structured now — they edit through the chip picker
  if (field === "categories") return startEditCategories(td, row);
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

// Apply a one-or-more-field patch to a checked/manual book, tracked for undo.
async function applyEditPatch(row, patch) {
  const fields = Object.keys(patch);
  const label = `edit ${fields.join("+")} of ${String(row.book.title || "").slice(0, 32)}`;
  const tag = fields.join(", ").toUpperCase();
  if (row.kind === "manual") {
    const before = {};
    for (const k of fields) before[k] = String(row.book[k] || "");
    if (await patchManualFields(row.id, patch)) {
      pushOp(label,
        () => patchManualFields(row.id, before),
        () => patchManualFields(row.id, patch),
        { kind: "manual-fields", id: row.id, before });
      status(`UPDATED ${tag} :: RESCANNING`);
      return true;
    }
    statusCrit("UPDATE FAILED");
    return false;
  }
  const entry = state.checked.get(row.id);
  if (!entry) return false;
  trackChecked(label, row.id, () => {
    entry.book = Object.assign({}, entry.book, patch);
    entry.edited = true;
    entry.checks = null;
    entry.scans = null;
    entry.verify = null;
    queueScan(row.id);
  });
  saveChecked();
  status(`UPDATED ${tag} :: RESCANNING`);
  return true;
}

async function commitEdit(row, field, value) {
  // Editing the title or the volume field with a volume designator — a title
  // like "Elements of Botany 2/5", or a volume of "2/5" / "3" — strips the
  // designator into the volume field and auto-declares a multi-volume set for
  // the base title (see volGroupFromTitle / volGroupFromVolume).
  let patch = { [field]: value };
  let count = 0;
  if (field === "title") {
    const g = volGroupFromTitle(value);
    if (g) {
      const book = Object.assign({}, row.book, { title: g.title, volume: g.volume });
      patch = { title: g.title, volume: g.volume,
                group_id: groupIdOf(row.book) || setKeyOf(book) };
      count = g.count;
    }
  } else if (field === "volume") {
    const g = volGroupFromVolume(value);
    if (g) {
      const book = Object.assign({}, row.book, { volume: g.volume });
      patch = { volume: g.volume,
                group_id: groupIdOf(row.book) || setKeyOf(book) };
      count = g.count;
    }
  }
  const ok = await applyEditPatch(row, patch);
  if (ok && count >= 2) {
    // group_id is the durable association; the title may change independently
    const key = patch.group_id || groupIdOf(row.book);
    const m = setsMap();
    const want = key ? Math.max(setDefinedCount(key), count) : 0;
    if (key && setDefinedCount(key) !== want) {
      const beforeRec = m[key] ? Object.assign({}, m[key]) : null;
      setSetCount(key, want);
      const afterRec = Object.assign({}, m[key]);
      // Fold the set declaration into the edit's undo op (applyEditPatch just
      // pushed it) so a single Ctrl+Z reverts BOTH the fields and the set —
      // otherwise undo would leave an orphan set record in settings.
      const top = history.stack[history.ptr - 1];
      if (top) {
        const baseUndo = top.undoFn, baseRedo = top.redoFn;
        const put = (rec) => {
          const mm = setsMap();
          if (rec) mm[key] = Object.assign({}, rec); else delete mm[key];
          saveSettings();
        };
        top.undoFn = async () => { put(beforeRec); const r = await baseUndo(); renderChecked(); return r; };
        top.redoFn = async () => { put(afterRec); const r = await baseRedo(); renderChecked(); return r; };
        // also record the folded set change in the persisted History revert
        // descriptor so a History-tab revert undoes the set, not just the fields
        const lr = actionLog();
        const rec = lr.length && lr[lr.length - 1].id === top.id ? lr[lr.length - 1] : null;
        if (rec && rec.revert) { rec.revert.set = { key, beforeRec }; saveActionLog(); }
      }
    }
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
  // the master list source is read-only, so the title parse (subtitle /
  // volume / edition) applies at display time
  return parseBook({
    _src: "ch", _idx: b.idx,
    title: b.title, subtitle: b.subtitle || "", author: b.author,
    publisher: b.publisher, city: b.city, year: b.year, edition: b.edition,
    volume: "", language: "", pages: b.pages, condition: b.condition,
    price: b.price, illustrations: b.illustrations, categories: b.categories,
    notes: b.notes, acquired: b.acquired, url: "",
  });
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
  return parseBook({
    _src: "ol", _idx: r.key,
    title: r.title, subtitle: r.subtitle || "",
    author: (r.authors || []).join("; "),
    publisher: r.publisher || "", city: r.city || "",
    year: r.year || (r.first_year ? String(r.first_year) : ""),
    edition: r.edition || "", volume: r.volume || "",
    language: r.language || "", pages: r.pages || "",
    categories: "", notes: "", url: r.url || "",
  });
}

const BOTTOM_TABLES = {
  ol: {
    label: "Open Library",
    cols: ["Title", "Author", "Year", "Publisher", "City", "Ed", "Vol", "Lang"],
    cells: (r) => [linkCell(r.title, r.url), esc(r.author), esc(r.year),
                   esc(r.publisher), esc(r.city), esc(r.edition),
                   esc(r.volume), esc(r.language)],
  },
  ch: {
    label: "Master list",
    // Vol/Ed hold the values the display parse lifts out of raw titles —
    // multi-volume sets stay distinguishable in the table
    cols: ["Title", "Subtitle", "Author", "Year", "Vol", "Ed", "Publisher",
           "City", "Categories"],
    cells: (r) => [esc(r.title), esc(r.subtitle), esc(r.author), esc(r.year),
                   esc(r.volume), esc(r.edition), esc(r.publisher),
                   esc(r.city), esc(r.categories)],
  },
  whl: {
    label: "WHL catalog",
    cols: ["Title", "Authors", "Year", "Status"],
    cells: (r) => [linkCell(r.title, r.url), esc(r.author), esc(r.year),
                   esc(r.status)],
  },
  history: {
    label: "History",
    cols: ["Time", "Action", ""],   // custom-rendered (see renderHistoryRows)
    history: true,
  },
};

function linkCell(text, url) {
  const t = esc(text) || "<em>(untitled)</em>";
  return url
    ? `<a href="${esc(url)}" target="_blank" rel="noopener" data-tip="${esc(url)}">${t}</a>`
    : t;
}

// A fixed strip of regular tabs — one per defined bottom table (Open Library,
// Master list, WHL, …) — in place of the old add/remove + dropdown mechanism.
function bottomTabs() {
  const tabs = Object.keys(BOTTOM_TABLES);
  const a = state.settings.bottomActive;
  if (a == null || a < 0 || a >= tabs.length) state.settings.bottomActive = 0;
  return tabs;
}

function renderBottomTabs() {
  const tabs = bottomTabs();
  const wrap = el("bottom-tabs");
  wrap.innerHTML = "";
  tabs.forEach((t, i) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "bottom-tab" + (i === state.settings.bottomActive ? " active" : "");
    b.textContent = BOTTOM_TABLES[t].label;
    b.addEventListener("click", () => {
      if (state.settings.bottomActive === i) return;
      state.settings.bottomActive = i;
      saveSettings();
      renderBottomPane();
    });
    wrap.appendChild(b);
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
  if (def.history) { renderHistoryRows(); return; }
  el("bottom-head").innerHTML =
    "<tr>" + def.cols.map((c) => `<th>${c}</th>`).join("") + "</tr>";
  if (t === "ol") {
    // repopulation marks: green = copy to the selected WHL row, red = exclude
    [...el("bottom-head").querySelectorAll("th")].forEach((th, i) => {
      const fkey = OL_MARK_FIELDS["c" + i];
      if (!fkey) return;
      const m = state.olColMarks[fkey];
      th.classList.toggle("mark-copy", m === "copy");
      th.classList.toggle("mark-exclude", m === "exclude");
      th.dataset.tip = "Ctrl+click: copy to the selected row\nShift+click: exclude";
    });
  }
  const tbody = el("bottom-rows");
  tbody.innerHTML = "";

  // When a row is selected in search mode, the embedded catalogs (Master list,
  // WHL) search for THAT row + its marks; otherwise they follow the Find box.
  const q = bottomFilterQuery();
  let records;
  if (t === "ol") {
    records = (state.olRows || []).map(olToRecord);
  } else if (t === "ch") {
    records = (state.chBooks || [])
      .filter((b) => matchesFind(q, `${b.title} ${b.subtitle || ""}`, b.author, b.year))
      .map(chToRecord);
    // the master list is the Google Sheets publish preview: manual entries
    // (light yellow) are the rows a sync would append
    for (const e of state.manual) {
      const b = manualToBook(e);
      if (!matchesFind(q, `${b.title} ${b.subtitle || ""}`, b.author, b.year)) continue;
      records.push(Object.assign({}, b, {
        _src: "manual", _mid: e.id, url: "",
        acquired: "", condition: b.condition || "",
      }));
    }
  } else {
    records = (state.whlRows || [])
      .filter((r) => matchesFind(q, `${r.title} ${r.subtitle || ""}`, r.authors, r.year))
      .map(whlToRecord);
  }

  state.bottomRecords = records;
  streamRows(tbody, records, (rec, i) => bottomRecordTr(rec, i, t, def));
  el("bottom-empty").textContent = "No matches";   // reset from the History tab's message
  el("bottom-empty").hidden = records.length !== 0;
  el("bottom-count").textContent =
    `${records.length} rows` + (t === "ol" && state.olNote ? ` — ${state.olNote}` : "");
  applyTableChrome("b-" + t);
}

// build one bottom-pane row <tr>; `i` is the absolute index into
// state.bottomRecords, read back by the delegated click handler.
function bottomRecordTr(rec, i, t, def) {
  const tr = document.createElement("tr");
  tr.className = "bottom-row";
  // master-list publish preview: manual additions light yellow, rows
  // already checked into the working table light blue
  if (t === "ch") {
    if (rec._src === "manual") tr.classList.add("ml-manual");
    else if (state.checked.has(ckey("ch_library", rec._idx))) tr.classList.add("ml-checked");
  }
  // needs-attention marks (Q while hovering) apply here too
  let bWhy = "";
  if (rec._src === "manual" && rec._mid) {
    const e = state.manual.find((x) => x.id === rec._mid);
    if (e && e.attention) {
      tr.classList.add("attention");
      bWhy = attnReason(e.attention);
    }
  } else if (attnHas(`${rec._src}:${rec._idx}`)) {
    tr.classList.add("attention");
    bWhy = attnReason((state.attn || {})[`${rec._src}:${rec._idx}`]);
  }
  tr.dataset.bi = i;
  tr.dataset.tip = recordTip(rec, def.label) +
    (bWhy ? "\nNeeds attention: " + bWhy : "");
  tr.innerHTML = def.cells(rec).map((c) => `<td>${c == null ? "" : c}</td>`).join("");
  return tr;
}

// --- History tab: a persistent, revertible action log ------------------------
const ACTION_TYPE_LABEL = {
  edit: "Edit", add: "Add", remove: "Remove", repopulate: "Repopulate",
  verify: "Verify", scan: "Scan", source: "Source", other: "Action",
};
function fmtActionTime(ts) {
  const d = new Date(ts), now = new Date();
  const hm = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return d.toDateString() === now.toDateString()
    ? hm : d.toLocaleDateString([], { month: "short", day: "numeric" }) + " " + hm;
}
function histCanRevert(r) {
  return !r.reverted && !!r.revert;   // data-based revert only (see revertHistoryAction)
}

function renderHistoryRows() {
  el("bottom-head").innerHTML = "<tr><th>Time</th><th>Action</th><th></th></tr>";
  const tbody = el("bottom-rows");
  // #bottom-rows is shared with the streamed tables; drop any live stream scroll
  // listener so it can't append foreign (Master-list) rows into the History list.
  if (tbody._streamCleanup) tbody._streamCleanup();
  tbody.innerHTML = "";
  const log = actionLog();
  const q = (state.checkedFilter || "").toLowerCase();
  const rows = log.filter((r) => !q || r.label.toLowerCase().includes(q)).slice().reverse();
  for (const r of rows) {
    const tr = document.createElement("tr");
    tr.className = "hist-row hist-" + r.type + (r.reverted ? " hist-reverted" : "");
    tr.dataset.hid = r.id;
    tr.dataset.tip = histCanRevert(r)
      ? "Click for detail · Ctrl+click to revert this action"
      : "Click for detail" + (r.reverted ? " (already reverted)" : " (not revertible here)");
    tr.innerHTML =
      `<td class="hist-time">${esc(fmtActionTime(r.ts))}</td>` +
      `<td class="hist-label" data-tip="${esc(ACTION_TYPE_LABEL[r.type] || "Action")}">${esc(r.label)}</td>` +
      `<td class="hist-rev">${r.reverted ? "✓" : (histCanRevert(r) ? "↶" : "")}</td>`;
    tbody.appendChild(tr);
  }
  el("bottom-empty").hidden = rows.length !== 0;
  if (!rows.length) el("bottom-empty").textContent = "No actions recorded yet";
  el("bottom-count").textContent =
    `${log.length} action${log.length === 1 ? "" : "s"}` + (q ? ` (${rows.length} shown)` : "");
  // History rows are built directly (not via streamRows' per-chunk mask), so
  // force applyTableChrome's guarded mask to re-run on these fresh rows.
  const histTable = el("bottom-rows").closest("table");
  if (histTable) histTable.dataset.hideSig = "";
  applyTableChrome("b-history");
}

function histDetailHtml(rec) {
  const meta = [
    `Time: ${new Date(rec.ts).toLocaleString()}`,
    `Type: ${ACTION_TYPE_LABEL[rec.type] || rec.type}`,
    rec.tkey ? `Target: ${rec.tkey}` : "",
    rec.reverted ? "Status: REVERTED" : "",
  ].filter(Boolean).join("  ·  ");
  const b = rec.revert && (rec.revert.before !== undefined ? rec.revert.before
    : rec.revert.snap !== undefined ? rec.revert.snap : rec.revert.beforeSnaps);
  let json = "";
  if (b !== undefined && b !== null) {
    try { json = JSON.stringify(b, null, 1); } catch (e) { json = String(b); }
  }
  return `<td colspan="3"><div class="hist-detail"><div class="hist-detail-meta">${esc(meta)}</div>` +
    (json ? `<div class="hist-detail-cap">Restores on revert:</div><pre class="hist-detail-json">${esc(json.slice(0, 4000))}</pre>` : "") +
    `</div></td>`;
}

// data-based inverse of a logged action (works across reloads)
async function applyRevert(m) {
  if (!m) return false;
  let ok = false;
  switch (m.kind) {
    case "checked": restoreChecked(m.key, m.before); ok = true; break;
    case "manual-fields": ok = !!(await patchManualFields(m.id, m.before)); break;
    case "manual-create": ok = !!(await deleteManualById(m.id)); break;
    case "manual-restore": ok = !!(await restoreManualEntry(m.snap)); break;
    case "manual-localpdf": ok = !!(await patchManualLocalPdf(m.id, m.before)); break;
    case "manual-verify":
      await setVerify(m.id, m.source, m.before, false);
      if (m.beforeUrl) await setManualUrl(m.id, m.source, m.beforeUrl, false);
      ok = true; break;
    case "manual-url": ok = !!(await setManualUrl(m.id, m.source, m.before, false)); break;
    case "whl": ok = !!(await whlApplySnaps(m.idx, m.beforeSnaps)); break;
    case "smartbake": ok = await scRevertItems(m.items || []); break;
    default: ok = false;
  }
  // also revert a multi-volume set declaration that commitEdit folded into this edit
  if (ok && m.set) {
    const mm = setsMap();
    if (m.set.beforeRec) mm[m.set.key] = Object.assign({}, m.set.beforeRec);
    else delete mm[m.set.key];
    saveSettings();
    renderChecked();
  }
  return ok;
}

async function revertHistoryAction(hid) {
  const log = actionLog();
  const rec = log.find((r) => r.id === hid);
  if (!rec || rec.reverted || !rec.revert) return;
  if (rec.tkey && log.some((r) => r.id > rec.id && !r.reverted && r.tkey === rec.tkey)) {
    if (!(await confirmDialog({
      title: "Revert action",
      message: "A newer action changed the same record.",
      detail: "Reverting this one will discard that newer change.",
      confirmLabel: "Revert anyway",
      danger: true,
    }))) return;
  }
  const ok = await applyRevert(rec.revert);
  if (ok) {
    rec.reverted = true;
    saveActionLog();
    // neutralize the still-live in-session undo op so Ctrl+Z/Y won't re-apply it
    const op = history.stack.find((o) => o.id === hid);
    if (op) { op.undoFn = op.redoFn = () => {}; }
    renderBottomRows();
    status("REVERTED :: " + rec.label.slice(0, 50));
  } else statusCrit("REVERT FAILED :: " + rec.label.slice(0, 40));
}

// expand/collapse the full-detail row under a History entry
function toggleHistDetail(hrow, hid) {
  const next = hrow.nextElementSibling;
  if (next && next.classList.contains("hist-detail-row")) { next.remove(); return; }
  document.querySelectorAll("tr.hist-detail-row").forEach((el2) => el2.remove());
  const rec = actionLog().find((r) => r.id === hid);
  if (!rec) return;
  const dr = document.createElement("tr");
  dr.className = "hist-detail-row";
  dr.innerHTML = histDetailHtml(rec);
  hrow.after(dr);
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
  const params = new URLSearchParams({ limit: String(state.settings.olLimit || 60) });
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
  // a selected volume narrows the match to its volume number
  if (ov && ov.volume) params.set("volume", ov.volume);
  // extra fields carried by the search-term marks
  if (ov) for (const f of ["publisher", "city", "edition"]) {
    if (ov[f]) params.set(f, ov[f]);
  }
  for (const f of ["author", "publisher", "city", "year", "edition", "volume"]) {
    const v = el("s-" + f).value.trim();
    if (v && !params.has(f)) params.set(f, v);
  }
  if (![...params.keys()].some((k) => k !== "limit")) {
    state.olRows = [];
    state.olNote = "Type in Find or the search form";
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
    state.olNote = "Search failed";
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
    const olSrc = rec._src === "ol";
    const addBody = { add: {
      title: (olSrc ? titleCase(rec.title) : rec.title) +
        (rec.subtitle ? ": " + (olSrc ? titleCase(rec.subtitle) : rec.subtitle) : ""),
      authors: olSrc ? flipName(rec.author) : rec.author,
      year: rec.year,
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
      statusErr("WHL ADD FAILED");
    }
    return;
  }
  // top = checked books
  if (checkedMode() === "search" && state.checkedSelected != null) {
    await repopulateCheckedRow(rec);
    return;
  }
  if (rec._src === "ch") { addChBook(rec._idx); return; }
  const cased = (t) => rec._src === "ol" ? titleCase(t) : t;
  // volume / edition / subtitle indicators split out of the title on add
  const body = parseBook({
    title: cased(rec.title), subtitle: cased(rec.subtitle || ""),
    author: rec.author, publisher: rec.publisher, city: rec.city,
    year: rec.year, edition: rec.edition, volume: rec.volume,
    language: rec.language, pages: rec.pages || "",
    condition: "", price: "", illustrations: "", categories: rec.categories || "",
    notes: rec.url ? `From ${rec._src === "ol" ? "Open Library" : "WHL catalog"}: ${rec.url}` : "",
  });
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
      statusErr(data.error || "ADD FAILED");
    }
  } catch (e) {
    statusErr("ADD FAILED");
  }
}

function addChBook(idx) {
  const raw = (state.chBooks || []).find((x) => x.idx === idx);
  if (!raw) return;
  const book = parseBook(raw);   // subtitle / volume / edition split
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
function checkedMode() { return state.settings.checkedMode === "search" ? "search" : "edit"; }

// the active top table's EDIT / SEARCH mode
function topMode() {
  return state.settings.topTable === "whl" ? whlMode() : checkedMode();
}

// the current mode is shown as a tag in the footer
function updateModeTag() {
  const tag = el("mode-tag");
  tag.hidden = false;
  const m = topMode();
  const name = state.settings.topTable === "whl" ? "WHL" : "Checked";
  tag.textContent = `${name} mode: ${m}`;
  tag.className = "foot-tag " + (m === "edit" ? "tag-edit" : "tag-search");
}

function setTopMode(m) {
  if (state.settings.topTable === "whl") state.settings.whlMode = m;
  else state.settings.checkedMode = m;
  saveSettings();
  if (m !== "search") {
    state.whlSelected = null;
    state.checkedSelected = null;
    state.olOverride = null;
  }
  renderTop();
  status(m === "search" ? "SEARCH MODE" : "EDIT MODE");
}

function renderModeBar() {
  const btn = el("whl-mode");
  btn.hidden = false;
  btn.textContent = `Mode: ${topMode()}`;
  el("whl-cons").hidden = topMode() !== "search";
}

function switchTopTable(t) {
  state.settings.topTable = t;
  saveSettings();
  // a table switch abandons the previous table's search selection so the bottom
  // catalogs follow the new table / Find box rather than a now-hidden row (the
  // search constraints in searchCons are a persistent preference — kept)
  state.whlSelected = null;
  state.checkedSelected = null;
  state.olOverride = null;
  el("top-table").value = t;
  el("checked-pane").hidden = t !== "checked";
  el("whltop-pane").hidden = t !== "whl";
  for (const id of ["dl-approved", "export-json", "filter-btn"]) {
    el(id).disabled = t !== "checked";
  }
  updateModeTag();
  renderTop();
}

async function renderTop() {
  renderModeBar();
  updateModeTag();
  if (state.settings.topTable === "whl") {
    await loadWhlRows();
    renderWhlTop();
  } else {
    renderChecked();
    el("top-count").textContent = "";
  }
}

function renderWhlTop() {
  // the WHL table owns the top pane only when selected there — a save from
  // the EDIT tab (reachable from the bottom pane) must not repaint it or
  // clobber the shared count label
  if (state.settings.topTable !== "whl") return;
  // background re-renders (e.g. scrape completion) must not destroy an
  // in-progress cell edit
  const active = document.activeElement;
  if (active && active.classList && active.classList.contains("cell-edit")) return;
  const mode = whlMode();
  renderModeBar();
  updateModeTag();
  const q = findQuery();
  let rows = (state.whlRows || [])
    .filter((r) => matchesFind(q, `${r.title} ${r.subtitle || ""}`, r.authors, r.year))
    .filter((r) => yearInRange(r.year));
  const so = state.sort.whl;
  if (so) rows = sortRowsBy(rows, (r) => whlSortVal(r, so.key), so.dir);
  origRowShown = null;
  streamRows(el("whltop-rows"), rows, (r) => whlRowTr(r, mode));
  el("whltop-empty").hidden = rows.length !== 0;
  el("top-count").textContent = `${rows.length} WHL rows`;
  applyTableChrome("whl");
  markSortHeaders("whl");
  refreshInfoIfActive();
}

// build one WHL top-table row <tr>
function whlRowTr(r, mode) {
  const tr = document.createElement("tr");
  tr.dataset.widx = r.idx;
  // Corrected, added, and draft rows are visually distinct.
  if (r.added) tr.classList.add("whl-row-added");
  else if (r.corrected) tr.classList.add("whl-row-corrected");
  if (r.status === "draft") tr.classList.add("whl-row-draft");
  if (attnHas("whl:" + r.idx)) tr.classList.add("attention");
  if (state.whlSelected === r.idx) tr.classList.add("whl-selected");
  tr.dataset.tip = recordTip(
    Object.assign(whlToRecord(r), { subtitle: r.subtitle || "",
      categories: r.categories || "", notes: r.description || "" }), "WHL");
  const whlWhy = attnReason((state.attn || {})["whl:" + r.idx]);
  if (whlWhy) tr.dataset.tip += "\nNeeds attention: " + whlWhy;
  tr.innerHTML = whlRowCells(r, mode);
  scDecorateWhlRow(tr, r, mode);
  return tr;
}

function whlRowCells(r, mode) {
  const editable = (f) => mode === "edit"
    ? ` class="editable" data-wedit="${f}"`
    : "";
  let statusCell;
  if (r.permalink) {
    const isPub = r.status === "publish";
    // published entries with a publication file open in the PDF viewer
    // window instead of a browser tab
    const modal = mode !== "orig" && isPub && r.file;
    statusCell = badge(isPub ? "available" : "missing",
      isPub ? "PUB" : (r.status || "?").slice(0, 4).toUpperCase(),
      {
        href: modal ? r.file : r.permalink,
        tip: modal ? "View the publication PDF\n" + r.file
                   : "Open the WHL catalogue page\n" + r.permalink,
        attrs: modal ? ` data-pdfm="${r.idx}"` : "",
      });
  } else {
    statusCell = badge("unknown", (r.status || "—").slice(0, 5).toUpperCase());
  }
  return `
      <td>${r.added ? "ADDED" : r.corrected ? "EDITED" : r.scraped ? "WEB" : "CSV"}</td>
      <td${editable("title")}${mode === "search" ? ' data-wsearch="1"' : ""}>${esc(r.title)}</td>
      <td${editable("subtitle")}>${esc(r.subtitle || "")}</td>
      <td${editable("authors")}>${esc(r.authors)}</td>
      <td${editable("year")}>${esc(r.year)}</td>
      <td${editable("publisher")}>${esc(r.publisher || "")}</td>
      <td${editable("pages")}>${esc(r.pages || "")}</td>
      <td${editable("language")}>${esc(r.language || "")}</td>
      <td${editable("subject")}>${esc(r.subject || "")}</td>
      <td${editable("description")}>${esc(r.description || "")}</td>
      <td class="col-whl">${statusCell}</td>
      <td class="col-whl">${copyrightTag({ title: r.title, author: r.authors, year: r.year })}</td>`;
}

// --- ALT: view the original (pre-correction) record ------------------------------
// Holding Alt over an edited WHL row swaps it to the original values;
// in the EDIT panel, holding Alt swaps the fields the same way. Both views
// are grayed and highlighted to read as "original record".

let origRowShown = null;
let curHoverTr = null;

function origMerged(r) {
  return Object.assign({}, r, r.orig || {});
}

function showOrigRow(tr, r) {
  if (origRowShown === tr || !r || !r.orig) return;
  // never swap out a row holding an in-progress cell edit
  if (tr.querySelector(".cell-edit")) return;
  clearOrigRow();
  tr.dataset.editedHtml = tr.innerHTML;
  tr.innerHTML = whlRowCells(origMerged(r), "orig");
  // keep the hidden-column layout of the rest of the table
  const vis = state.settings.colVis.whl || {};
  [...tr.children].forEach((td, i) => {
    const key = WHL_COLS[i] ? WHL_COLS[i][0] : null;
    td.style.display = key && vis[key] === false ? "none" : "";
  });
  tr.classList.add("orig-view");
  origRowShown = tr;
}

function clearOrigRow() {
  if (!origRowShown) return;
  if (origRowShown.dataset.editedHtml != null) {
    origRowShown.innerHTML = origRowShown.dataset.editedHtml;
    delete origRowShown.dataset.editedHtml;
  }
  origRowShown.classList.remove("orig-view");
  origRowShown = null;
}

let editOrigShown = false;
let editOrigSaved = null;

function showEditOrig() {
  const t = state.editTarget;
  if (editOrigShown || !t || t.kind !== "whl") return;
  if (!el("pane-edit").classList.contains("active") || el("whledit-form").hidden) return;
  const row = whlRowByIdx(t.idx);
  if (!row || !row.orig) return;
  const merged = origMerged(row);
  editOrigSaved = {};
  for (const f of WHL_ROW_FIELDS) {
    const inp = el("w-" + f);
    editOrigSaved[f] = inp.value;
    inp.value = merged[f] || "";
    inp.readOnly = true;
  }
  el("whledit-form").classList.add("orig-view");
  editOrigShown = true;
}

function clearEditOrig() {
  if (!editOrigShown) return;
  for (const f of WHL_ROW_FIELDS) {
    const inp = el("w-" + f);
    inp.value = editOrigSaved[f];
    inp.readOnly = false;
  }
  el("whledit-form").classList.remove("orig-view");
  editOrigShown = false;
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
    () => whlPost({ idx, fields: afterFields }),
    { kind: "whl", idx, beforeSnaps });
}

function selectWhlSearchRow(idx) {
  const row = whlRowByIdx(idx);
  if (!row) return;
  state.whlSelected = idx;
  state.editTarget = null;   // a live search selection supersedes a prior edit target (Info pane)
  state.olOverride = searchOverrideFrom({
    title: row.title, author: row.authors, year: row.year,
    publisher: row.publisher, city: row.city, edition: row.edition,
    volume: row.volume,
  });
  setSearchPane(true);
  const tabs = bottomTabs();
  let i = tabs.indexOf("ol");
  if (i < 0) { tabs.push("ol"); i = tabs.length - 1; }
  state.settings.bottomActive = i;
  saveSettings();
  renderWhlTop();
  renderBottomPane().then(olRealtime);
  status(`OPEN LIBRARY SEARCH :: ${row.title}`);
}

// Which OL columns copy into the selected row. Title/author/year copy by
// default; Ctrl+click a column header to force-include it (green),
// Shift+click to exclude it (red). WHL rows only use the fields they have.
const OL_MARK_FIELDS = { c0: "title", c1: "author", c2: "year",
                         c3: "publisher", c4: "city", c5: "edition",
                         c6: "volume", c7: "language" };

// Build the column-inclusion test from the OL column marks. If ANY column is
// marked "include" (copy), the marks act as an allow-list — unmarked columns
// are excluded (their per-field default is ignored); "exclude" always wins.
// With no "include" marks, the per-field defaults apply (title/author/year).
function olMarkGate() {
  const marks = state.olColMarks || {};
  const anyCopy = Object.values(marks).some((v) => v === "copy");
  return (k, dflt) =>
    marks[k] === "exclude" ? false
      : marks[k] === "copy" ? true
        : anyCopy ? false : dflt;
}

// checked/manual rows can take every Open Library column.
// titleCase/flipName only normalize Open Library records — CH and WHL
// records copy verbatim, matching every other copy path.
function repopBookFields(rec) {
  const ol = rec._src === "ol";
  const cased = (t) => ol ? titleCase(t) : t;
  const on = olMarkGate();
  const f = {};
  if (on("title", true)) {
    f.title = cased(rec.title);
    // the subtitle goes in the Subtitle field, not appended to the title
    if (rec.subtitle) f.subtitle = cased(rec.subtitle);
  }
  if (on("author", true)) f.author = ol ? flipName(rec.author) : rec.author;
  if (on("year", true)) f.year = rec.year;
  if (on("publisher", false) && rec.publisher) f.publisher = rec.publisher;
  if (on("city", false) && rec.city) f.city = rec.city;
  if (on("edition", false) && rec.edition) f.edition = rec.edition;
  if (on("volume", false) && rec.volume) f.volume = rec.volume;
  if (on("language", false) && rec.language) f.language = rec.language;
  return f;
}

// A copy/repopulate consumes the OL column marks; clear them afterwards so the
// next book starts unconstrained (the marks persist only for the pending copy).
function clearOlColMarks() {
  // the search constraints (searchCons) are a persistent preference, not consumed
  if (!state.olColMarks || !Object.keys(state.olColMarks).length) return;
  state.olColMarks = {};
  renderBottomRows();
}

async function repopulateCheckedRow(rec) {
  const row = state.rowsById.get(String(state.checkedSelected));
  if (!row) return;
  const vals = repopBookFields(rec);
  if (!Object.keys(vals).length) { status("ALL COLUMNS EXCLUDED"); return; }
  const label = `repopulate ${(vals.title || row.book.title).slice(0, 30)}`;
  if (row.kind === "manual") {
    const before = {};
    for (const k of Object.keys(vals)) before[k] = row.book[k] || "";
    if (await patchManualFields(row.id, vals)) {
      pushOp(label,
        () => patchManualFields(row.id, before),
        () => patchManualFields(row.id, vals));
      status(`ROW REPOPULATED :: ${vals.title || row.book.title}`);
      clearOlColMarks();
    } else {
      statusErr("REPOPULATE FAILED");
    }
    return;
  }
  const entry = state.checked.get(row.id);
  if (!entry) return;
  trackChecked(label, row.id, () => {
    entry.book = Object.assign({}, entry.book, vals);
    entry.edited = true;
    entry.checks = null;
    entry.scans = null;
    entry.verify = null;
    queueScan(row.id);
  });
  saveChecked();
  renderChecked();
  status(`ROW REPOPULATED :: ${vals.title || row.book.title}`);
  clearOlColMarks();
}

function repopFields(rec) {
  const ol = rec._src === "ol";
  const cased = (t) => ol ? titleCase(t) : t;
  const on = olMarkGate();
  const fields = {};
  if (on("title", true)) {
    // OL titles are sentence case; catalog entries use conventional caps
    fields.title = cased(rec.title);
    fields.subtitle = cased(rec.subtitle || "");
  }
  if (on("author", true))
    fields.authors = ol ? flipName(rec.author) : rec.author;
  if (on("year", true)) fields.year = rec.year;
  if (on("publisher", false) && rec.publisher) fields.publisher = rec.publisher;
  if (on("language", false) && rec.language) fields.language = rec.language;
  return fields;
}

async function repopulateWhlRow(rec) {
  const idx = state.whlSelected;
  const row = whlRowByIdx(idx);
  if (row == null) return;
  const fields = repopFields(rec);
  if (!Object.keys(fields).length) { status("ALL COLUMNS EXCLUDED"); return; }
  const before = whlFieldSnaps(row, Object.keys(fields));
  if (await whlPost({ idx, fields })) {
    pushWhlFieldsOp(`repopulate WHL row ${(fields.title || row.title).slice(0, 30)}`,
      idx, before, fields);
    status(`WHL ROW REPOPULATED :: ${fields.title || row.title}`);
    clearOlColMarks();
  } else {
    statusErr("WHL REPOPULATE FAILED");
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
      statusErr("WHL EDIT FAILED");
    }
    renderWhlTop();
  };
  input.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") { ev.preventDefault(); finish(true); }
    else if (ev.key === "Escape") { ev.stopPropagation(); finish(false); }
  });
  input.addEventListener("blur", () => finish(true));
}

// --- the EDIT tab: a record editor opened with Ctrl+click from any table.
// It shows the WHL field set for WHL rows and the book field set for
// checked / manual / CH-catalog records.

function showEditForms(kind) {
  // a pending Alt original-view snapshot belongs to the previous record:
  // discard it WITHOUT restoring (the caller just filled the new values)
  if (editOrigShown) {
    for (const f of WHL_ROW_FIELDS) el("w-" + f).readOnly = false;
    el("whledit-form").classList.remove("orig-view");
    editOrigShown = false;
    editOrigSaved = null;
  }
  el("whledit-tab").hidden = false;
  el("whledit-form").hidden = kind !== "whl";
  el("setedit-form").hidden = kind !== "set";
  el("bookedit-form").hidden = kind === "whl" || kind === "set";
  switchPaneTab("pane-edit");
}

// --- multi-volume set editor -------------------------------------------------

// current volume rows of a set (share the base key, carry a volume), vol-sorted
function setMembers(key) {
  return combinedRows()
    .filter((r) => volNum(r.book) > 0 && groupIdOf(r.book) === key)
    .sort((a, b) => volNum(a.book) - volNum(b.book));
}

function toggleSet(key) {
  if (!key) return;
  setSetExpanded(key, !setExpanded(key));
  renderChecked();
}

// update a checked catalog book's metadata + queue a fresh check
function updateCheckedBook(id, fields) {
  const entry = state.checked.get(id);
  if (!entry) return;
  trackChecked(`edit ${(fields.title || entry.book.title || "").slice(0, 36)}`, id, () => {
    entry.book = Object.assign({}, entry.book, fields);
    entry.edited = true;
    entry.checks = null; entry.scans = null; entry.verify = null;
    queueScan(id);
  });
  saveChecked();
}

// POST a new manual book (used to autofill a set's missing volumes)
async function createManualBook(book) {
  try {
    const res = await fetch("/api/manual", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(parseBook(book)),
    });
    const data = await res.json().catch(() => ({}));
    if (res.ok && data.ok) {
      state.manual.unshift(data.entry);
      queueScan(data.entry.id);
      return data.entry;
    }
  } catch (e) { /* offline / failed — skip */ }
  return null;
}

// ensure a set has volumes 1..count, creating missing ones as manual books
// autofilled from the shared title/author/publisher
async function ensureSetVolumes(key, count, shared) {
  const have = new Set(setMembers(key).map((r) => volNum(r.book)));
  let created = 0;
  for (let v = 1; v <= count; v++) {
    if (have.has(v)) continue;
    if (await createManualBook({
      title: shared.title, author: shared.author || "",
      publisher: shared.publisher || "", volume: String(v), group_id: key,
    })) created++;
  }
  return created;
}

function openSetEditTab(key) {
  const vols = setMembers(key);
  if (!vols.length) return;
  const title = setBaseTitle(vols[0].book);
  state.editTarget = { kind: "set", key };
  el("whledit-note").textContent = `Multi-volume set :: ${title.slice(0, 60)}`;
  el("es-title").value = title;
  el("es-author").value = firstVal(vols, "author");
  el("es-publisher").value = firstVal(vols, "publisher");
  const maxVol = vols.reduce((m, x) => Math.max(m, volNum(x.book)), 0);
  el("es-count").value = Math.max(setDefinedCount(key), vols.length, maxVol);
  el("setedit-msg").textContent = "";
  showEditForms("set");
  el("es-title").focus();
}

async function saveSetEditTab(ev) {
  ev.preventDefault();
  const t = state.editTarget;
  if (!t || t.kind !== "set") return;
  const title = el("es-title").value.trim();
  const author = el("es-author").value.trim();
  const publisher = el("es-publisher").value.trim();
  const count = Math.max(1, Math.min(99, parseInt(el("es-count").value, 10) || 1));
  if (!title) { el("setedit-msg").textContent = "Title is required"; return; }
  el("setedit-msg").textContent = "Saving ...";

  // apply the shared fields to every existing volume
  for (const r of setMembers(t.key)) {
    const fields = {};
    if (title !== setBaseTitle(r.book)) fields.title = title;
    if (author && author !== (r.book.author || "")) fields.author = author;
    if (publisher && publisher !== (r.book.publisher || "")) fields.publisher = publisher;
    if (!Object.keys(fields).length) continue;
    if (r.kind === "manual") await patchManualFields(r.id, fields);
    else updateCheckedBook(r.id, fields);
  }
  // Keep the existing metadata association even when the shared title changes.
  const newKey = t.key;   // stable metadata association; titles may change
  setSetCount(newKey, count);
  const created = await ensureSetVolumes(newKey, count, { title, author, publisher });
  renderChecked();
  el("setedit-msg").textContent = created
    ? `Saved — ${created} volume${created > 1 ? "s" : ""} autofilled`
    : "Saved";
  status(`SET SAVED :: ${title} (${count})`);
}

// promote a single book to an N-volume set from the book editor's "# volumes"
async function promoteRowToSet(rowId, vals, count) {
  const row = state.rowsById.get(String(rowId));
  if (!row) return;
  const key = setKeyOf({ title: vals.title });
  const fields = { group_id: key };
  if (volNum(row.book) <= 0) fields.volume = "1";   // the anchor becomes volume 1
  if (row.kind === "manual") await patchManualFields(rowId, fields);
  else updateCheckedBook(rowId, fields);
  setSetCount(key, count);
  await ensureSetVolumes(key, count,
    { title: vals.title, author: vals.author, publisher: vals.publisher });
  renderChecked();
}

function openWhlEditTab(idx) {
  const row = whlRowByIdx(idx);
  if (!row) return;
  state.whlEditIdx = idx;
  state.editTarget = { kind: "whl", idx };
  el("whledit-note").textContent =
    `WHL ROW ${idx >= 0 ? "#" + idx : "(ADDED)"} :: ${row.title.slice(0, 60)}`;
  for (const f of WHL_ROW_FIELDS) el("w-" + f).value = row[f] || "";
  el("whledit-msg").textContent = "";
  showEditForms("whl");
  el("w-title").focus();
}

const BOOK_EDIT_FIELDS = MANUAL_FIELDS.concat(["acquired"]);

function fillBookEditForm(book, showAcquired) {
  for (const f of BOOK_EDIT_FIELDS) {
    el("e-" + f).value = book[f] || "";
  }
  catPickers["e-categories"].set(book.category_ids || []);
  // manual entries have no ACQUIRED field — the form adapts to the source
  el("e-acquired").closest(".mf-field").hidden = !showAcquired;
  el("bookedit-msg").textContent = "";
}

// a checked-books / manual row
// how many volumes the book's set holds today (1 = not a set)
function bookSetCount(book) {
  const key = groupIdOf(book);
  if (!key) return Math.max(volNum(book), 1);
  const members = combinedRows()
    .filter((r) => volNum(r.book) > 0 && groupIdOf(r.book) === key);
  const maxVol = members.reduce((m, x) => Math.max(m, volNum(x.book)), 0);
  return Math.max(setDefinedCount(key), maxVol, volNum(book), 1);
}

function openBookEditTab(rowId) {
  const row = state.rowsById.get(String(rowId));
  if (!row) return;
  state.editTarget = { kind: "row", id: String(rowId) };
  el("whledit-note").textContent =
    `${row.kind === "manual" ? "Manual entry" : "Checked book"} :: ` +
    `${(row.book.title || "").slice(0, 60)}`;
  fillBookEditForm(row.book, row.kind !== "manual");
  el("e-setcount").value = bookSetCount(row.book);
  showEditForms("book");
  el("e-title").focus();
}

// a master-list record (bottom pane); SAVE checks it with the edited metadata
function openChEditTab(idx) {
  const book = (state.chBooks || []).find((x) => x.idx === idx);
  if (!book) return;
  state.editTarget = { kind: "ch", idx };
  el("whledit-note").textContent =
    `Master list #${idx} :: ${(book.title || "").slice(0, 60)}`;
  const existing = state.checked.get(ckey("ch_library", idx));
  // prefill matches what the table shows: the parsed title/subtitle/vol/ed
  fillBookEditForm(existing ? existing.book : parseBook(book), true);
  el("e-setcount").value = volNum(existing ? existing.book : book) || 1;
  showEditForms("book");
  el("e-title").focus();
}

async function patchManualFields(id, fields) {
  const res = await fetch(`/api/manual/${encodeURIComponent(id)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(Object.assign({}, fields, { _edited: true })),
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

async function saveBookEditTab(ev) {
  ev.preventDefault();
  const t = state.editTarget;
  if (!t || t.kind === "whl") return;
  const vals = {};
  for (const f of BOOK_EDIT_FIELDS) vals[f] = el("e-" + f).value.trim();
  const catIds = catPickers["e-categories"].get();
  if (!vals.title) { el("bookedit-msg").textContent = "Title is required"; return; }
  // "Volumes in set" >= 2 turns this book into (or updates) a multi-volume set
  const setCount = Math.max(1, Math.min(99, parseInt(el("e-setcount").value, 10) || 1));

  if (t.kind === "row") {
    const row = state.rowsById.get(t.id);
    if (!row) { el("bookedit-msg").textContent = "Row is gone"; return; }
    if (row.kind === "manual") {
      const fields = { category_ids: catIds };
      const before = { category_ids: (row.book.category_ids || []).slice() };
      for (const f of MANUAL_FIELDS) {
        fields[f] = vals[f];
        before[f] = row.book[f] || "";
      }
      if (await patchManualFields(t.id, fields)) {
        pushOp(`edit entry ${vals.title.slice(0, 32)}`,
          () => patchManualFields(t.id, before),
          () => patchManualFields(t.id, fields),
          { kind: "manual-fields", id: t.id, before });
        if (setCount >= 2) await promoteRowToSet(t.id, vals, setCount);
        el("bookedit-msg").textContent = setCount >= 2 ? "Saved as set" : "Saved";
        status(`ENTRY SAVED :: ${vals.title} :: RESCANNING`);
      } else {
        el("bookedit-msg").textContent = "Save failed";
      }
      return;
    }
    // checked catalog row: client-side metadata + fresh checks/scans
    const entry = state.checked.get(t.id);
    if (!entry) { el("bookedit-msg").textContent = "Row is gone"; return; }
    trackChecked(`edit ${vals.title.slice(0, 36)}`, t.id, () => {
      entry.book = Object.assign({}, entry.book, vals,
                                 { category_ids: catIds });
      entry.edited = true;
      entry.checks = null;
      entry.scans = null;
      entry.verify = null;
      queueScan(t.id);
    });
    saveChecked();
    renderChecked();
    if (setCount >= 2) await promoteRowToSet(t.id, vals, setCount);
    el("bookedit-msg").textContent = setCount >= 2 ? "Saved as set" : "Saved";
    status(`BOOK SAVED :: ${vals.title} :: RESCANNING`);
    return;
  }

  // Master-list record: check it (or update the checked copy) with the
  // edits — parse-on-add applies here as on every other add path
  const key = ckey("ch_library", t.idx);
  trackChecked(`check ${vals.title.slice(0, 38)}`, key, () => {
    const prev = state.checked.get(key) || {};
    const base = (state.chBooks || []).find((x) => x.idx === t.idx) || {};
    state.checked.set(key, {
      book: parseBook(Object.assign({ idx: t.idx }, base, prev.book || {}, vals)),
      edited: true,
      checks: null, scans: null, verify: null, manual_urls: null,
    });
    queueScan(key);
  });
  saveChecked();
  renderChecked();
  updateCheckedCount();
  el("bookedit-msg").textContent = "Saved — added to checked books";
  status(`CH BOOK CHECKED WITH EDITS :: ${vals.title}`);
}

async function saveWhlEditTab(ev) {
  ev.preventDefault();
  // the form is showing the ORIGINAL record while Alt is held — saving
  // that would write the pre-correction values back as corrections
  if (editOrigShown) return;
  const idx = state.whlEditIdx;
  const row = whlRowByIdx(idx);
  if (row == null) { el("whledit-msg").textContent = "No row loaded"; return; }
  const fields = {};
  for (const f of WHL_ROW_FIELDS) fields[f] = el("w-" + f).value.trim();
  if (!fields.title) { el("whledit-msg").textContent = "Title is required"; return; }
  const before = whlFieldSnaps(row, WHL_ROW_FIELDS);
  if (await whlPost({ idx, fields })) {
    pushWhlFieldsOp(`edit WHL record ${fields.title.slice(0, 30)}`, idx, before, fields);
    el("whledit-msg").textContent = "Saved";
    status(`WHL CORRECTIONS SAVED :: ${fields.title}`);
  } else {
    el("whledit-msg").textContent = "Save failed";
  }
}

// --- WHL website metadata scrape --

let scrapePoll = null;
let scrapeRunning = false;

async function startWhlScrape() {
  if (scrapeRunning) return;
  scrapeRunning = true;
  try {
    await fetch("/api/whl_scrape", { method: "POST" });
  } catch (e) {
    scrapeRunning = false;
    statusErr("SCRAPE FAILED TO START");
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
    scrapeRunning = false;
    if (s.status === "error") {
      statusErr(`SCRAPE ERROR :: ${s.error || "unknown"}`);
      return;
    }
    await loadWhlRows(true);
    renderWhlTop();
    renderBottomRows();
    status(`SCRAPE COMPLETE :: ${s.scraped_total || 0} PUBLISHED BOOKS HAVE FULL METADATA`);
  }, 1500);
}

// --- markdown: line grammar shared by the live editor ------------------------
// Per-line rendering only (headings, list items, quotes, rules, and inline
// bold / italic / code / links). Marker characters are tracked as hidden
// tokens so a rendered-text caret offset can be mapped back to the source.

function mdLineBlock(src) {
  let m;
  if ((m = src.match(/^(#{1,4})\s+/)))
    return { cls: "md-h" + m[1].length, skip: m[0].length, bullet: "" };
  if ((m = src.match(/^[-*]\s+/)))
    return { cls: "md-li", skip: m[0].length, bullet: "• " };
  if (/^\d+[.)]\s+/.test(src))
    return { cls: "md-oli", skip: 0, bullet: "" };
  if (/^(-{3,}|\*{3,})\s*$/.test(src) && src.trim())
    return { cls: "md-hr", skip: src.length, bullet: "" };
  if ((m = src.match(/^>\s?/)))
    return { cls: "md-q", skip: m[0].length, bullet: "" };
  return { cls: "", skip: 0, bullet: "" };
}

function mdTokenizeInline(src) {
  // tokens with s/e over src; marker tokens are hidden in rendered mode
  const toks = [];
  const push = (s, e, text, cls, marker, href) =>
    toks.push({ s, e, text, cls: cls || "", marker: !!marker, href: href || "" });
  let i = 0;
  while (i < src.length) {
    const rest = src.slice(i);
    let m;
    if ((m = rest.match(/^\*\*([^*]+)\*\*/))) {
      push(i, i + 2, "**", "", true);
      push(i + 2, i + 2 + m[1].length, m[1], "md-b");
      push(i + m[0].length - 2, i + m[0].length, "**", "", true);
      i += m[0].length;
    } else if ((m = rest.match(/^\*([^*]+)\*/))) {
      push(i, i + 1, "*", "", true);
      push(i + 1, i + 1 + m[1].length, m[1], "md-i");
      push(i + m[0].length - 1, i + m[0].length, "*", "", true);
      i += m[0].length;
    } else if ((m = rest.match(/^`([^`]+)`/))) {
      push(i, i + 1, "`", "", true);
      push(i + 1, i + 1 + m[1].length, m[1], "md-code");
      push(i + m[0].length - 1, i + m[0].length, "`", "", true);
      i += m[0].length;
    } else if ((m = rest.match(/^\[([^\]]+)\]\((https?:[^)\s]+)\)/))) {
      push(i, i + 1, "[", "", true);
      push(i + 1, i + 1 + m[1].length, m[1], "md-link", false, m[2]);
      push(i + 1 + m[1].length, i + m[0].length, "](" + m[2] + ")", "", true);
      i += m[0].length;
    } else {
      const nx = rest.slice(1).search(/[*`[]/);
      const len = nx === -1 ? rest.length : nx + 1;
      push(i, i + len, rest.slice(0, len), "");
      i += len;
    }
  }
  return toks;
}

function mdTokenHtml(t) {
  const tip = t.href ? ` data-tip="${esc(t.href)}"` : "";
  return t.cls
    ? `<span class="${t.cls}"${tip}>${esc(t.text)}</span>`
    : esc(t.text);
}

// rendered view of one line: markers hidden
function mdLineHtml(src) {
  const blk = mdLineBlock(src);
  if (blk.cls === "md-hr")
    return { cls: "md-hr", html: `<span class="md-hrline"></span>` };
  let html = blk.bullet ? `<span class="md-bullet">${blk.bullet}</span>` : "";
  for (const t of mdTokenizeInline(src.slice(blk.skip))) {
    if (!t.marker) html += mdTokenHtml(t);
  }
  return { cls: blk.cls, html: html || "<br>" };
}

// source view of one line: every character present, markers dimmed
function mdLineSrcHtml(src) {
  let blk = mdLineBlock(src);
  if (blk.cls === "md-hr") blk = { cls: "", skip: 0, bullet: "" };
  let html = blk.skip
    ? `<span class="mtok">${esc(src.slice(0, blk.skip))}</span>` : "";
  for (const t of mdTokenizeInline(src.slice(blk.skip))) {
    html += t.marker ? `<span class="mtok">${esc(t.text)}</span>` : mdTokenHtml(t);
  }
  return { cls: (blk.cls + " src").trim(), html: html || "<br>" };
}

// plain text of a line as displayed in rendered mode (markers hidden)
function mdRenderedText(src) {
  const blk = mdLineBlock(src);
  if (blk.cls === "md-hr") return "";
  let out = blk.bullet || "";
  for (const t of mdTokenizeInline(src.slice(blk.skip))) {
    if (!t.marker) out += t.text;
  }
  return out;
}

// map an offset in the RENDERED text of a line back to its source offset
function mdSrcOffset(src, renderedOffset) {
  const blk = mdLineBlock(src);
  if (blk.cls === "md-hr") return src.length;
  let ro = renderedOffset;
  if (blk.bullet) {
    if (ro <= blk.bullet.length) return Math.min(blk.skip, src.length);
    ro -= blk.bullet.length;
  }
  for (const t of mdTokenizeInline(src.slice(blk.skip))) {
    if (t.marker) continue;
    if (ro <= t.text.length) return blk.skip + t.s + ro;
    ro -= t.text.length;
  }
  return src.length;
}

// --- live markdown editor (reusable component) --------------------------------
// Obsidian-style: the editing surface IS the rendered document. Lines away
// from the caret display fully rendered (markers hidden); the line(s) under
// the caret/selection show their raw source with the markers dimmed.

function createMdEditor(container, opts = {}) {
  container.classList.add("md-live");
  container.contentEditable = "true";
  container.spellcheck = false;
  let activeRange = null;   // [firstLine, lastLine] shown as source
  let internal = false;     // guards our own DOM writes
  let composing = false;    // IME composition in progress: hands off the DOM

  function lineDivs() {
    return [...container.children].filter((n) => n.nodeType === 1);
  }

  function renderLineDiv(div, raw, asSource) {
    div.dataset.src = raw;
    const view = asSource ? mdLineSrcHtml(raw) : mdLineHtml(raw);
    div.dataset.mode = asSource ? "src" : "html";
    div.className = ("md-line " + view.cls).trim();
    div.innerHTML = view.html;
  }

  function lineSource(div) {
    return div.dataset.mode === "html" ? (div.dataset.src || "") : div.textContent;
  }

  function normalizeDom() {
    for (const n of [...container.childNodes]) {
      if (n.nodeType === 3 || (n.nodeType === 1 && n.tagName === "BR")) {
        const div = document.createElement("div");
        div.className = "md-line";
        div.dataset.mode = "src";
        div.textContent = n.nodeType === 3 ? n.textContent : "";
        if (!div.textContent) div.innerHTML = "<br>";
        container.replaceChild(div, n);
      }
    }
    if (!container.children.length) {
      const div = document.createElement("div");
      renderLineDiv(div, "", false);
      container.appendChild(div);
    }
  }

  // (line index, plain-text offset) of a DOM point inside the container
  function pointOf(node, off) {
    if (node === container) {
      // a container-level offset of N children means "after the last line"
      // (this is what Ctrl+A produces) — map it to the END of that line
      const divs = lineDivs();
      if (off >= divs.length) {
        const last = divs.length - 1;
        return { line: Math.max(0, last),
                 offset: divs[last] ? divs[last].textContent.length : 0 };
      }
      return { line: Math.max(0, off), offset: 0 };
    }
    let div = node.nodeType === 1 ? node : node.parentNode;
    while (div && div.parentNode !== container) div = div.parentNode;
    if (!div) return null;
    const idx = lineDivs().indexOf(div);
    if (idx < 0) return null;
    const r = document.createRange();
    r.selectNodeContents(div);
    try { r.setEnd(node, off); } catch (e) { return { line: idx, offset: 0 }; }
    return { line: idx, offset: r.toString().length };
  }

  function caretRange() {
    const sel = document.getSelection();
    if (!sel.rangeCount || !container.contains(sel.anchorNode)) return null;
    const a = pointOf(sel.anchorNode, sel.anchorOffset);
    const f = pointOf(sel.focusNode, sel.focusOffset);
    if (!a || !f) return null;
    if (a.line < f.line || (a.line === f.line && a.offset <= f.offset))
      return { a, f, backward: false };
    return { a: f, f: a, backward: true };
  }

  // DOM point at a plain-text offset within a line div
  function placePoint(div, offset) {
    const walker = document.createTreeWalker(div, NodeFilter.SHOW_TEXT);
    let node, remaining = offset;
    while ((node = walker.nextNode())) {
      if (remaining <= node.textContent.length) return { node, off: remaining };
      remaining -= node.textContent.length;
    }
    return { node: div, off: 0 };
  }

  // render every line back to html (no line is "active" any more)
  function deactivate() {
    if (!activeRange) return;
    internal = true;
    for (const d of lineDivs()) {
      if (d.dataset.mode !== "html") renderLineDiv(d, d.textContent, false);
    }
    activeRange = null;
    internal = false;
  }

  // show the lines under the selection as source (with caret mapping)
  function activateRange(cr) {
    const ns = cr.a.line, ne = cr.f.line;
    const divs = lineDivs();
    if (activeRange && activeRange[0] === ns && activeRange[1] === ne) {
      let allSrc = true;
      for (let i = ns; i <= ne; i++) {
        if (divs[i] && divs[i].dataset.mode === "html") { allSrc = false; break; }
      }
      if (allSrc) return;
    }
    // map endpoint offsets (rendered -> source) before any re-render
    const mapOff = (p) => {
      const d = divs[p.line];
      return d && d.dataset.mode === "html"
        ? mdSrcOffset(d.dataset.src || "", p.offset)
        : p.offset;
    };
    const aOff = mapOff(cr.a), fOff = mapOff(cr.f);
    internal = true;
    let changed = false;
    if (activeRange) {
      for (let i = activeRange[0]; i <= activeRange[1]; i++) {
        const d = divs[i];
        if (d && (i < ns || i > ne) && d.dataset.mode !== "html") {
          renderLineDiv(d, d.textContent, false);
          changed = true;
        }
      }
    }
    for (let i = ns; i <= ne; i++) {
      const d = divs[i];
      if (d && d.dataset.mode === "html") {
        renderLineDiv(d, d.dataset.src || "", true);
        changed = true;
      }
    }
    activeRange = [ns, ne];
    if (changed && divs[ns] && divs[ne]) {
      const sel = document.getSelection();
      const p1 = placePoint(divs[ns], Math.min(aOff, lineSource(divs[ns]).length));
      const p2 = placePoint(divs[ne], Math.min(fOff, lineSource(divs[ne]).length));
      try {
        // setBaseAndExtent keeps the selection's direction (anchor->focus),
        // so shift+arrow extension keeps working across the re-render
        if (cr.backward) sel.setBaseAndExtent(p2.node, p2.off, p1.node, p1.off);
        else sel.setBaseAndExtent(p1.node, p1.off, p2.node, p2.off);
      } catch (e) { /* leave the browser selection */ }
    }
    internal = false;
  }

  function onSelectionChange() {
    if (internal || composing) return;
    if (document.activeElement !== container) {
      // fallback for environments where focusout is unreliable
      deactivate();
      return;
    }
    const cr = caretRange();
    if (!cr) return;
    activateRange(cr);
  }

  // join line i with line i+1 in SOURCE space (a cross-line Backspace or
  // Delete must not concatenate rendered text — that would silently drop
  // the hidden markdown markers)
  function mergeLines(i) {
    const divs = lineDivs();
    const a = divs[i], b = divs[i + 1];
    if (!a || !b) return;
    const left = lineSource(a), right = lineSource(b);
    internal = true;
    renderLineDiv(a, left + right, true);
    b.remove();
    activeRange = [i, i];
    const p = placePoint(a, Math.min(left.length, a.textContent.length));
    const sel = document.getSelection();
    const r = document.createRange();
    try {
      r.setStart(p.node, p.off);
      r.collapse(true);
      sel.removeAllRanges();
      sel.addRange(r);
    } catch (e) { /* ignore */ }
    internal = false;
    if (opts.onChange) opts.onChange();
  }

  // beforeinput fires synchronously before the DOM changes: convert every
  // line the edit touches to source view first, so the browser's edit
  // always lands on 1:1 source text (typing can otherwise hit a rendered
  // line before the async selectionchange has converted it). Cross-line
  // deletes are taken over entirely (see mergeLines).
  function onBeforeInput(ev) {
    if (internal || composing || ev.isComposing) return;
    const cr = caretRange();
    if (!cr) return;
    const type = ev.inputType || "";
    const collapsed = cr.a.line === cr.f.line && cr.a.offset === cr.f.offset;
    if (collapsed &&
        (type === "deleteContentBackward" || type === "deleteContentForward")) {
      const divs = lineDivs();
      const d = divs[cr.a.line];
      const srcOff = d && d.dataset.mode === "html"
        ? mdSrcOffset(d.dataset.src || "", cr.a.offset)
        : cr.a.offset;
      if (type === "deleteContentBackward" && srcOff === 0 && cr.a.line > 0) {
        ev.preventDefault();
        mergeLines(cr.a.line - 1);
        return;
      }
      if (type === "deleteContentForward" && d &&
          srcOff >= lineSource(d).length && cr.a.line < divs.length - 1) {
        ev.preventDefault();
        mergeLines(cr.a.line);
        return;
      }
    }
    activateRange(cr);
  }

  function onInput() {
    if (internal || composing) return;
    internal = true;
    normalizeDom();
    // restyle the source lines (their text may hold new markdown now),
    // keeping the caret at the same plain-text offset (source view is 1:1)
    const sel = document.getSelection();
    let saved = null;
    if (sel.rangeCount && container.contains(sel.focusNode)) {
      saved = pointOf(sel.focusNode, sel.focusOffset);
    }
    for (const d of lineDivs()) {
      if (d.dataset.mode !== "html") {
        renderLineDiv(d, d.textContent, true);
      } else if (d.dataset.src == null ||
                 d.textContent !== mdRenderedText(d.dataset.src)) {
        // safety net: an edit landed on a rendered line anyway (e.g. a
        // drop) — adopt its visible text as the new source
        renderLineDiv(d, d.textContent, false);
      }
    }
    if (saved) {
      const divs = lineDivs();
      const d = divs[Math.min(saved.line, divs.length - 1)];
      if (d) {
        const p = placePoint(d, Math.min(saved.offset, d.textContent.length));
        try {
          const r = document.createRange();
          r.setStart(p.node, p.off);
          r.collapse(true);
          sel.removeAllRanges();
          sel.addRange(r);
        } catch (e) { /* ignore */ }
      }
    }
    internal = false;
    onSelectionChange();
    if (opts.onChange) opts.onChange();
  }

  function onPaste(ev) {
    ev.preventDefault();
    const text = (ev.clipboardData || window.clipboardData).getData("text/plain");
    const cr = caretRange();
    if (!cr) return;
    const divs = lineDivs();
    const srcOff = (p) => {
      const d = divs[p.line];
      return d.dataset.mode === "html"
        ? mdSrcOffset(d.dataset.src || "", p.offset)
        : p.offset;
    };
    const lines = divs.map(lineSource);
    const aO = srcOff(cr.a), fO = srcOff(cr.f);
    const before = lines[cr.a.line].slice(0, aO);
    const after = lines[cr.f.line].slice(fO);
    const ins = String(text || "").replace(/\r/g, "").split("\n");
    ins[0] = before + ins[0];
    const caretOff = ins[ins.length - 1].length;
    ins[ins.length - 1] += after;
    lines.splice(cr.a.line, cr.f.line - cr.a.line + 1, ...ins);
    setValue(lines.join("\n"));
    // place the caret at the end of the pasted text
    const lineIdx = cr.a.line + ins.length - 1;
    internal = true;
    const d = lineDivs()[lineIdx];
    if (d) {
      renderLineDiv(d, lines[lineIdx], true);
      activeRange = [lineIdx, lineIdx];
      const p = placePoint(d, Math.min(caretOff, d.textContent.length));
      const sel = document.getSelection();
      const r = document.createRange();
      try {
        r.setStart(p.node, p.off);
        r.collapse(true);
        sel.removeAllRanges();
        sel.addRange(r);
      } catch (e) { /* ignore */ }
    }
    internal = false;
    if (opts.onChange) opts.onChange();
  }

  function onFocusOut() {
    setTimeout(() => {
      if (document.activeElement !== container) deactivate();
    }, 0);
  }

  function setValue(text) {
    internal = true;
    container.innerHTML = "";
    const lines = String(text || "").replace(/\r/g, "").split("\n");
    for (const raw of lines) {
      const div = document.createElement("div");
      renderLineDiv(div, raw, false);
      container.appendChild(div);
    }
    activeRange = null;
    internal = false;
  }

  function getValue() {
    return lineDivs().map(lineSource).join("\n");
  }

  container.addEventListener("beforeinput", onBeforeInput);
  container.addEventListener("input", onInput);
  container.addEventListener("paste", onPaste);
  container.addEventListener("focusout", onFocusOut);
  container.addEventListener("compositionstart", () => { composing = true; });
  container.addEventListener("compositionend", () => {
    composing = false;
    onInput();
  });
  document.addEventListener("selectionchange", onSelectionChange);

  setValue(opts.value || "");
  return { el: container, get: getValue, set: setValue,
           focus: () => container.focus() };
}

// --- markdown overlay window (WHL description pencil) --

let overlayMd = null;
let mdOpenValue = "";   // editor content at open time, to detect dirty state

function openMarkdownEditor(targetTextareaId, title) {
  state.mdTarget = targetTextareaId;
  el("md-title").textContent = title || "Markdown editor";
  overlayMd.set(el(targetTextareaId).value);
  mdOpenValue = overlayMd.get();
  el("md-overlay").hidden = false;
  overlayMd.focus();
}

async function closeMarkdownEditor(apply) {
  // Cancel/Escape/backdrop with edited content confirms before discarding,
  // so an incidental click outside the window can't throw the text away.
  if (!apply && state.mdTarget && overlayMd.get() !== mdOpenValue &&
      state.settings.confirmDiscard !== false) {
    if (!(await confirmDialog({
      title: "Discard changes",
      message: "Discard the unsaved Markdown changes?",
      confirmLabel: "Discard changes",
      cancelLabel: "Keep editing",
      danger: true,
    }))) return;
  }
  if (apply && state.mdTarget) {
    el(state.mdTarget).value = overlayMd.get();
  }
  state.mdTarget = null;
  el("md-overlay").hidden = true;
}

// --- PDF viewer (reusable component) -------------------------------------------
// Renders a PDF inline via the browser's viewer. Local files stream through
// /api/pdf; remote URLs load directly (with an open-in-tab fallback).

function pdfLocalSrc(path) {
  return "/api/pdf?path=" + encodeURIComponent(path);
}

// remote PDFs can't be iframed directly (X-Frame-Options: "refused to
// connect") — the server proxies them through the download cache
function pdfProxySrc(url) {
  return "/api/pdf?url=" + encodeURIComponent(url);
}

function fmtBytes(n) {
  if (!n && n !== 0) return "";
  if (n >= 1e9) return (n / 1e9).toFixed(2) + " GB";
  if (n >= 1e6) return (n / 1e6).toFixed(1) + " MB";
  return Math.max(1, Math.round(n / 1e3)) + " KB";
}

function createPdfViewer() {
  const root = document.createElement("div");
  root.className = "pdf-viewer";
  root.innerHTML = `
    <div class="pdf-bar">
      <span class="pdf-path tool-label"></span>
      <span class="pdf-size tool-label"></span>
      <button class="cad-btn tiny icon-btn pdf-pagesbtn" type="button"
              data-tip="PDF pages beside the OCR text (like the OCR tab)" hidden>${ICONS.pdfpage}</button>
      <button class="cad-btn tiny icon-btn pdf-laybtn" type="button"
              data-tip="Facsimile layout: the OCR text at the position and scale it occupies on the page (read-only)" hidden>${ICONS.layout}</button>
      <button class="cad-btn tiny icon-btn pdf-pagesave" type="button"
              data-tip="Save the page-view edits to the OCR file" hidden>${ICONS.save}</button>
      <button class="cad-btn tiny icon-btn pdf-ocr" type="button"
              data-tip="OCR text" hidden>${ICONS.text}</button>
      <a class="cad-btn tiny pdf-open" target="_blank" rel="noopener" hidden>OPEN IN TAB</a>
    </div>
    <div class="pdf-body">
      <div class="pdf-framewrap" hidden><iframe class="pdf-frame" title="PDF preview"></iframe></div>
      <pre class="pdf-ocrpane" hidden></pre>
      <div class="pdf-pagesbox" hidden></div>
      <div class="pdf-note empty">No PDF</div>
    </div>`;
  const frame = root.querySelector(".pdf-frame");
  const frameWrap = root.querySelector(".pdf-framewrap");
  const note = root.querySelector(".pdf-note");
  const path = root.querySelector(".pdf-path");
  const size = root.querySelector(".pdf-size");
  const open = root.querySelector(".pdf-open");
  const ocrBtn = root.querySelector(".pdf-ocr");
  const ocrPane = root.querySelector(".pdf-ocrpane");
  const pagesBtn = root.querySelector(".pdf-pagesbtn");
  const layBtn = root.querySelector(".pdf-laybtn");
  const pagesSave = root.querySelector(".pdf-pagesave");
  const pagesBox = root.querySelector(".pdf-pagesbox");
  let sizeSeq = 0;
  let textSrc = "";
  let ocrOn = false;
  let ocrLoadedFor = "";
  let pagesOn = false;
  // facsimile mode of the page view — read from settings at use time, so
  // both viewer instances (and a server-adopted settings blob) stay in sync
  const isLay = () => !!state.settings.viewerLayout;
  let layObs = null;        // this viewer's own lazy-fill observer
  let pagesPdf = "";        // local path for /api/pdf/pageimg
  let pagesSaveTo = null;   // {buildId, name} — where page edits save
  let pagesSec = null;      // {pre, map} sections while the page view is up
  let pagesWhole = false;   // marker-less file: one whole-text box, verbatim
  let pagesDirty = false;   // unsaved page-view edits
  let pagesSeq = 0;

  // side-by-side page view: one row per page (image | that page's OCR
  // text), scrolling together — the same idiom as the OCR tab
  async function renderPages() {
    const seq = ++pagesSeq;
    // a rebuild of the SAME pdf (layout toggle, refetch) keeps the scroll
    const keepTop = pagesBox.dataset.pdf === pagesPdf ? pagesBox.scrollTop : 0;
    // no stale sections may survive into the next render: a Save clicked
    // mid-load must be a no-op, never a cross-build overwrite. The old
    // render stays visible through the fetches below, so its textareas are
    // locked — with pagesSec null their keystrokes would silently vanish.
    pagesSec = null;
    pagesWhole = false;
    pagesDirty = false;
    pagesSave.hidden = true;
    if (layObs) { layObs.disconnect(); layObs = null; }
    pagesBox.querySelectorAll("textarea").forEach((t) => { t.disabled = true; });
    let info = ocrState.pdfInfo[pagesPdf];
    if (!info || !textSrc) {
      pagesBox.innerHTML = `<p class="empty">Loading pages &hellip;</p>`;
    }
    if (!info) {
      try {
        const r = await (await fetch(
          "/api/pdf/info?path=" + encodeURIComponent(pagesPdf))).json();
        if (r.ok) { info = r; ocrState.pdfInfo[pagesPdf] = r; }
      } catch (e) { /* handled below */ }
    }
    const count = info ? info.pages : 0;
    let text = "";
    let textOk = !textSrc;   // no OCR source at all = legitimately empty
    if (textSrc) {
      try {
        const data = await (await fetch(textSrc)).json();
        if (data.ok) { text = data.text || ""; textOk = true; }
      } catch (e) { /* textOk stays false */ }
    }
    if (seq !== pagesSeq || !pagesOn) return;
    if (!count) {
      pagesBox.innerHTML = `<p class="empty">Could not read the PDF</p>`;
      delete pagesBox.dataset.pdf;
      return;
    }
    const sections = ocrPageSections(text);
    pagesWhole = textOk && !sections && !!text.trim();
    if (textOk) {
      pagesSec = sections ||
        { pre: "", map: new Map(text.trim() ? [[1, text]] : []) };
    }
    // a failed OCR fetch renders read-only with saving disabled — one
    // stray Save must not overwrite the real file with emptiness
    const editable = !!pagesSaveTo && textOk && !isLay();
    const shown = count;   // no cap: images window in via observePageImgs
    // reserved page boxes: lazy image loads must not shift the content
    const dims = (info && info.dims) || [];
    const ar = (n) => {
      const dd = dims[n - 1];
      return dd && dd[0] > 0 && dd[1] > 0 ? `aspect-ratio:${dd[0]} / ${dd[1]};` : "";
    };
    const img = (n) => `<img decoding="async" alt="page ${n}" style="${ar(n)}"
        data-thumb="/api/pdf/pageimg?path=${encodeURIComponent(pagesPdf)}&page=${n}&w=200"
        data-src="/api/pdf/pageimg?path=${encodeURIComponent(pagesPdf)}&page=${n}&w=700" />`;
    const notes =
      (!textOk ? `<div class="ocr-pgnote empty">OCR text unavailable — saving disabled</div>` : "") +
      (pagesWhole ? `<div class="ocr-pgnote empty">This OCR file has no page markers — the full text sits beside page 1${isLay() ? "" : " and saves verbatim"}</div>` : "") +
      (count > shown ? `<div class="ocr-pgnote empty">Showing the first ${shown} of ${count} pages</div>` : "");
    if (isLay()) {
      // facsimile mode: the page's text where it sits on the page, read-only
      pagesBox.innerHTML = notes +
        Array.from({ length: shown }, (_, i) => `
        <div class="ocr-pgrow" data-page="${i + 1}">
          <div class="ocr-pgimg">${img(i + 1)}</div>
          <div class="ocr-pglayout" data-lay="${i + 1}" style="${ar(i + 1)}"></div>
        </div>`).join("");
      if (layObs) layObs.disconnect();
      layObs = makeLayoutObserver(pagesBox, fillViewerLayout);
      pagesBox.dataset.pdf = pagesPdf;
      pagesBox.scrollTop = keepTop;   // 0 on a pdf switch: no bleed-through
      observePageImgs(pagesBox);
      return;
    }
    pagesBox.innerHTML = notes +
      Array.from({ length: shown }, (_, i) => `
        <div class="ocr-pgrow" data-page="${i + 1}">
          <div class="ocr-pgimg">${img(i + 1)}</div>
          <textarea class="ocr-pgtext cad-input" spellcheck="false"
            ${pagesWhole
              ? (i === 0 ? 'data-whole="1"' : "readonly")
              : `data-pn="${i + 1}"`}
            ${editable ? "" : "readonly"}></textarea>
        </div>`).join("");
    if (pagesSec) {
      if (pagesWhole) {
        const wta = pagesBox.querySelector("[data-whole]");
        if (wta) wta.value = text;
      } else {
        pagesBox.querySelectorAll("textarea[data-pn]").forEach((ta) => {
          ta.value = pagesSec.map.get(+ta.dataset.pn) || "";
        });
      }
    }
    pagesSave.hidden = !editable;
    pagesBox.dataset.pdf = pagesPdf;
    pagesBox.scrollTop = keepTop;   // 0 on a pdf switch: no bleed-through
    observePageImgs(pagesBox);
  }

  // one facsimile pane: the extraction gets the word boxes of THIS pdf's
  // text layer; OCR results flow their own page text (figures inline).
  // Exact name match — "extracted_claude.txt" is somebody's OCR output,
  // not the pdf's own text layer.
  async function fillViewerLayout(pane) {
    const page = +pane.dataset.lay;
    const name = ((pagesSaveTo && pagesSaveTo.name) || "").toLowerCase();
    const bid = (pagesSaveTo && pagesSaveTo.buildId) || "";
    const src = (pagesSaveTo && pagesSaveTo.src) || "primary";
    if (name === "extracted.txt" || !pagesSec) {
      return fillWordLayout(pane, pagesPdf, page, bid);
    }
    const meta = bid ? await ocrLayoutMeta(bid)
      : { images: {}, wordPages: {}, wordDocs: {}, regionPages: {} };
    if (!pane.isConnected) return;
    // the doc that produced the word boxes places a facsimile; any other doc
    // flows its own text (the boxes carry the box run's transcription, which
    // outlives text-only re-OCRs — see ocrWordsAreDocs)
    if (ocrHasWords(meta, src, page) &&
        ocrWordsAreDocs(meta, src, page, (pagesSaveTo && pagesSaveTo.name) || "")) {
      return fillWordLayout(pane, pagesPdf, page, bid);
    }
    if (bid && ocrHasRegions(meta, src, page)) {
      if (await fillRegionLayout(pane, bid, src, page,
                                 (pagesSaveTo && pagesSaveTo.name) || "")) return;
      if (!pane.isConnected) return;
    }
    const text = pagesSec.map.has(page) ? pagesSec.map.get(page) : null;
    fillDocLayout(pane, text, bid, meta.images);
  }

  pagesBox.addEventListener("input", (ev) => {
    const ta = ev.target.closest("textarea.ocr-pgtext");
    if (!ta || !pagesSec) return;
    if (ta.dataset.whole) {
      pagesSec.map.set(1, ta.value);
    } else if (ta.dataset.pn) {
      pagesSec.map.set(+ta.dataset.pn, ta.value);
    }
    pagesDirty = true;
  });

  async function savePages(saveTo) {
    const target = saveTo || pagesSaveTo;
    if (!target || !pagesSec) return;
    // a marker-less file saves verbatim — no page markers are injected
    const body = pagesWhole
      ? (pagesSec.map.get(1) || "")
      : ocrPagesToText(pagesSec);
    try {
      const res = await fetch(
        `/api/builds/${encodeURIComponent(target.buildId)}/ocr`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name: target.name, text: body }),
        });
      const data = await res.json().catch(() => ({}));
      if (data.ok) pagesDirty = false;
      if (data.ok) status(`OCR SAVED :: ${target.name}`);
      else statusCrit("OCR SAVE FAILED");
    } catch (e) {
      statusCrit("OCR SAVE FAILED");
    }
  }

  function setPages(on) {
    pagesOn = !!on && !!pagesPdf;
    pagesBtn.classList.toggle("active", pagesOn);
    layBtn.hidden = !pagesOn;              // layout is a mode of the page view
    layBtn.classList.toggle("active", isLay());
    pagesBox.hidden = !pagesOn;
    pagesSave.hidden = true;   // renderPages re-shows it when editable
    frameWrap.hidden = pagesOn || !frame.getAttribute("src");
    if (pagesOn) {
      ocrPane.hidden = true;
      ocrBtn.classList.remove("active");   // the OCR pane isn't showing
      renderPages();
    } else {
      pagesSec = null;
      pagesWhole = false;
      pagesDirty = false;
      pagesBox.innerHTML = "";
      delete pagesBox.dataset.pdf;
      setOcr(ocrOn);
    }
  }
  const confirmDiscardPages = async () =>
    state.settings.confirmDiscard === false || confirmDialog({
      title: "Discard page edits",
      message: "Discard unsaved page edits?",
      confirmLabel: "Discard edits",
      cancelLabel: "Keep editing",
      danger: true,
    });
  pagesBtn.addEventListener("click", async () => {
    if (pagesOn && pagesDirty && !(await confirmDiscardPages())) return;
    setPages(!pagesOn);
  });
  layBtn.addEventListener("click", async () => {
    if (pagesDirty && !(await confirmDiscardPages())) return;
    state.settings.viewerLayout = !isLay();
    saveSettings();
    layBtn.classList.toggle("active", isLay());
    if (pagesOn) renderPages();
  });
  pagesSave.addEventListener("click", () => savePages());

  async function loadOcr() {
    if (!textSrc || ocrLoadedFor === textSrc) return;
    const want = textSrc;
    ocrPane.textContent = "Extracting text ...";
    let data;
    try {
      data = await (await fetch(want)).json();
    } catch (e) { data = { ok: false, error: "extraction failed" }; }
    if (want !== textSrc) return;
    if (data.ok) {
      ocrLoadedFor = want;
      ocrPane.textContent =
        (data.shown < data.pages ? `[${data.shown} of ${data.pages} pages]\n\n` : "") +
        (data.text || "(no text layer)");
    } else {
      ocrPane.textContent = data.error || "extraction failed";
    }
  }

  function setOcr(on) {
    ocrOn = !!on && !!textSrc;
    ocrBtn.classList.toggle("active", ocrOn);
    ocrPane.hidden = !ocrOn || pagesOn;
    if (ocrOn && !pagesOn) loadOcr();
  }
  ocrBtn.addEventListener("click", async () => {
    if (pagesOn) {
      if (pagesDirty && !(await confirmDiscardPages())) return;
      // intent: leave the page view and SHOW the OCR pane
      setPages(false);
      setOcr(true);
      return;
    }
    setOcr(!ocrOn);
  });

  return {
    el: root,
    setOcr,
    show(src, label, opts = {}) {
      // undecorated: suppress the browser PDF viewer's toolbar/side panes
      // (scrollbar=0 for viewers that honor it; the frame is also
      // oversized so remaining scrollbars are clipped away)
      const framed = src.startsWith("/api/pdf")
        ? src + "#toolbar=0&navpanes=0&scrollbar=0" : src;
      if (frame.getAttribute("src") !== framed) frame.src = framed;
      note.hidden = true;
      path.textContent = label || src;
      path.dataset.tip = src;
      open.href = src;
      open.hidden = false;
      size.textContent = "";
      textSrc = opts.textSrc || "";
      ocrBtn.hidden = !textSrc;
      // a re-show (build switch, OCR-chip click, folder sync) with unsaved
      // page edits: preserve them by saving to the file they belong to —
      // the body is snapshotted synchronously, before any reassignment
      if (pagesOn && pagesDirty && pagesSaveTo) {
        savePages(pagesSaveTo);
      }
      // the page-aligned view needs a LOCAL pdf path for page images
      pagesPdf = opts.pagesPdf || "";
      pagesSaveTo = opts.pagesSaveTo || null;
      pagesBtn.hidden = !pagesPdf;
      // OCR files are editable (OCR tab / re-upload), so a same-URL show()
      // must refetch — only pane toggles within one view use the cache
      ocrLoadedFor = "";
      // re-showing (build switch) re-renders or leaves the page view
      setPages(pagesOn && !!pagesPdf);
      frameWrap.hidden = pagesOn;
      setOcr(opts.ocr != null ? opts.ocr : ocrOn);
      const seq = ++sizeSeq;
      if (src.startsWith("/api/pdf")) {
        fetch(src, { method: "HEAD" }).then((r) => {
          if (seq !== sizeSeq || !r.ok) return;
          const n = parseInt(r.headers.get("content-length") || "", 10);
          if (n) size.textContent = fmtBytes(n);
        }).catch(() => {});
      }
    },
    clear(msg) {
      sizeSeq++;
      frame.removeAttribute("src");
      frameWrap.hidden = true;
      note.textContent = msg || "No PDF";
      note.hidden = false;
      path.textContent = "";
      size.textContent = "";
      delete path.dataset.tip;
      open.hidden = true;
      textSrc = "";
      ocrBtn.hidden = true;
      ocrPane.hidden = true;
      pagesPdf = "";
      pagesSaveTo = null;
      pagesSec = null;
      pagesOn = false;
      pagesBtn.hidden = true;
      pagesBtn.classList.remove("active");
      layBtn.hidden = true;
      pagesSave.hidden = true;
      pagesBox.hidden = true;
      pagesBox.innerHTML = "";
      delete pagesBox.dataset.pdf;
      if (layObs) { layObs.disconnect(); layObs = null; }
    },
  };
}

// --- WHL publication viewer window ------------------------------------------------

let pdfmViewer = null;

function openPdfModal(idx) {
  const r = whlRowByIdx(idx);
  if (!r || !r.file) return;
  el("pdfm-title").textContent = (r.title || "Publication").slice(0, 90);
  el("pdfm-overlay").hidden = false;
  // proxied: worldherblibrary.org PDFs refuse to be iframed directly
  pdfmViewer.show(pdfProxySrc(r.file), r.file, {
    textSrc: "/api/pdf/text?url=" + encodeURIComponent(r.file),
    ocr: !!state.settings.whlModalOcr,
  });
}

function closePdfModal() {
  el("pdfm-overlay").hidden = true;
  pdfmViewer.clear();
}

// --- local file browser (pick a PDF) ---------------------------------------------

let fbOnPick = null;
let fbOpts = {};          // { downloadsDefault, recentMin } for the current open
let fbShowAll = false;    // "show all" overrides the recency filter

function fmtSize(n) {
  if (!n && n !== 0) return "";
  if (n >= 1e9) return (n / 1e9).toFixed(1) + " GB";
  if (n >= 1e6) return (n / 1e6).toFixed(1) + " MB";
  return Math.max(1, Math.round(n / 1e3)) + " KB";
}

async function fbLoad(dir) {
  let url = "/api/pdf/browse?dir=" + encodeURIComponent(dir || "");
  if (!dir && fbOpts.downloadsDefault) url += "&preset=downloads";
  let data;
  try {
    data = await (await fetch(url)).json();
  } catch (e) {
    el("fb-list").innerHTML = `<p class="empty">Cannot list this folder</p>`;
    return;
  }
  el("fb-path").value = data.dir;
  state.settings.pdfBrowseDir = data.dir;
  saveSettings();

  const drives = el("fb-drives");
  drives.innerHTML = "";
  for (const d of data.drives || []) {
    const b = document.createElement("button");
    b.className = "cad-btn tiny";
    b.type = "button";
    b.textContent = d;
    b.addEventListener("click", () => fbLoad(d));
    drives.appendChild(b);
  }

  const list = el("fb-list");
  list.innerHTML = "";
  const row = (label, cls, cb, tip) => {
    const div = document.createElement("div");
    div.className = "fb-row " + cls;
    div.textContent = label;
    if (tip) div.dataset.tip = tip;
    div.addEventListener("click", cb);
    list.appendChild(div);
  };
  // the list shows PDF files only; navigate folders via the path bar / drives

  // scan-attach: show only PDFs downloaded within the last N minutes
  let pdfs = data.pdfs || [];
  let hidden = 0;
  const filtering = fbOpts.recentMin && !fbShowAll;
  if (filtering) {
    const now = data.now || (Date.now() / 1000);
    const cutoff = now - fbOpts.recentMin * 60;
    const kept = pdfs.filter((f) => (f.mtime || 0) >= cutoff);
    hidden = pdfs.length - kept.length;
    pdfs = kept;
  }
  if (filtering && (pdfs.length || hidden)) {
    const note = document.createElement("div");
    note.className = "fb-note";
    note.textContent = `Showing PDFs downloaded in the last ${fbOpts.recentMin} min`;
    list.appendChild(note);
  }
  for (const f of pdfs)
    row(`▤ ${f.name}  (${fmtSize(f.size)})`, "pdf", () => {
      if (fbOnPick) fbOnPick(f.path);
      closeFileBrowser();
    }, f.path);
  if (hidden) {
    const showAll = document.createElement("div");
    showAll.className = "fb-row note";
    showAll.textContent = `— ${hidden} older PDF${hidden > 1 ? "s" : ""} hidden · show all`;
    showAll.addEventListener("click", () => { fbShowAll = true; fbLoad(dir); });
    list.appendChild(showAll);
  }
  if (!pdfs.length && !hidden)
    list.innerHTML = `<p class="empty">No PDF files in this folder</p>`;
}

function openFileBrowser(startDir, onPick, opts) {
  fbOnPick = onPick;
  fbOpts = opts || {};
  fbShowAll = false;
  el("fb-overlay").hidden = false;
  const start = startDir ||
    (fbOpts.downloadsDefault ? "" : (state.settings.pdfBrowseDir || "downloads/ia"));
  fbLoad(start);
}

function closeFileBrowser() {
  fbOnPick = null;
  el("fb-overlay").hidden = true;
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
    statusErr("IA DOWNLOAD FAILED TO START");
  }
  // if this call did not enter the polling loop (already saved, or failed to
  // start), release any background-download slot it was holding and pump next
  if (!state.dlTimers.has(identifier)) {
    state.autoDlActive.delete(identifier);
    pumpAutoDl();
  }
  updateDlProgress();
  scheduleRenderChecked();
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
        state.autoDlActive.delete(identifier);   // free a background slot
        if (data.status === "done") {
          state.downloadedIds.add(identifier);
          status(`IA PDF SAVED :: ${data.path || identifier}`);
        } else if (data.status === "error") {
          statusErr(`IA DOWNLOAD ERROR :: ${data.error || "unknown"}`);
        }
        pumpAutoDl();                             // start the next queued download
      }
      updateDlProgress();
      scheduleRenderChecked();
    } catch (e) {
      failures += 1;
      if (failures >= 8) {
        clearInterval(t);
        state.dlTimers.delete(identifier);
        state.autoDlActive.delete(identifier);
        pumpAutoDl();
        updateDlProgress();
        statusCrit(`IA DOWNLOAD POLLING STOPPED (SERVER UNREACHABLE) :: ${identifier}`);
      }
    }
  }, 1500);
  state.dlTimers.set(identifier, t);
}

// One eligibility contract for both the toolbar's batch action and the
// automatic action after a user approves an IA match. A rejected automatic
// match with a manually supplied IA URL remains eligible for the batch action.
function iaDownloadCandidate(row, verdict) {
  const current = verdict == null ? getVerify(row, "internet_archive") : verdict;
  const ia = row && row.scans && row.scans.internet_archive;
  const eligible = (current === "approved" && ia && ia.available === true) ||
    (current === "rejected" && getManualUrl(row, "internet_archive"));
  if (!eligible) return null;
  return { ident: iaIdentifierForRow(row), book: row.book || {} };
}

async function downloadApproved() {
  const approved = combinedRows().map((row) => ({
    row, candidate: iaDownloadCandidate(row),
  })).filter((item) => item.candidate);
  if (!approved.length) { status("NO VERIFIED IA SOURCES"); return; }
  let started = 0, saved = 0, noIa = 0;
  for (const { row, candidate } of approved) {
    const { ident } = candidate;
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

// --- automatic background IA download (on a found source) --------------------
// When a scan turns up an available IA source, queue a background download of
// its PDF (which the server also turns into a compressed 10-page preview). A
// small concurrency cap keeps it from hammering archive.org / the disk.
const AUTO_DL_MAX = 2;

// enqueue one background download (deduped); the single entry point so every
// caller (auto-scan + viewer fallback) shares the AUTO_DL_MAX cap + accounting
function enqueueAutoDl(ident, book) {
  if (!ident) return false;
  if (state.downloadedIds.has(ident)) return false;       // already saved
  const known = state.downloads.get(ident);
  // An error is retryable: the server replaces errored jobs safely under its
  // download lock. Only terminal success and active work are duplicates.
  if (known && (known.status === "downloading" || known.status === "done")) return false;
  if (state.autoDlActive.has(ident)) return false;
  if (state.autoDlQueue.some((q) => q.ident === ident)) return false;
  state.autoDlQueue.push({ ident, book: book || {} });
  pumpAutoDl();
  return true;
}

function maybeAutoDownloadIa(row) {
  if (!state.settings.autoIaDownload) return;
  const s = row && row.scans && row.scans.internet_archive;
  if (!s || s.available !== true) return;           // only a genuinely-found source
  enqueueAutoDl(iaIdentifierForRow(row), row.book || {});
}

function maybeAutoDownloadVerifiedIa(row, verdict = "approved") {
  if (!state.settings.autoIaDownload) return false;
  const candidate = iaDownloadCandidate(row, verdict);
  if (!candidate || !candidate.ident) return false;
  return enqueueAutoDl(candidate.ident, candidate.book);
}

function pumpAutoDl() {
  // disabling the setting mid-run abandons anything still queued
  if (!state.settings.autoIaDownload) { state.autoDlQueue = []; updateDlProgress(); return; }
  while (state.autoDlActive.size < AUTO_DL_MAX && state.autoDlQueue.length) {
    const { ident, book } = state.autoDlQueue.shift();
    const known = (state.downloads.get(ident) || {}).status;
    if (state.downloadedIds.has(ident) || state.autoDlActive.has(ident) ||
        known === "downloading" || known === "done") continue;
    state.autoDlActive.add(ident);
    startDownload(ident, book);   // pollDownload's terminal state pumps the next
  }
  updateDlProgress();
}

// footer progress bar: aggregate of all in-flight downloads + queue depth
function updateDlProgress() {
  const wrap = el("dl-progress");
  if (!wrap) return;
  let active = 0, bytes = 0, total = 0;
  for (const dl of state.downloads.values()) {
    if (dl && dl.status === "downloading") {
      active += 1; bytes += dl.bytes || 0; total += dl.total || 0;
    }
  }
  const queued = state.autoDlQueue.length;
  if (!active && !queued) { wrap.hidden = true; return; }
  wrap.hidden = false;
  const pct = total ? Math.round((bytes / total) * 100) : 0;
  el("dl-progbar").style.width = pct + "%";
  el("dl-progtext").textContent =
    `IA ${active} downloading` + (queued ? ` · ${queued} queued` : "") +
    (total ? ` · ${pct}%` : "");
}

// --- automatic checks + scans -----------------------------------------------

const scanQueue = [];
let scanQueueRunning = false;

function queueScan(id, autoDownload = true) {
  if (scanQueue.some((task) => task.id === id)) return;
  scanQueue.push({ id, autoDownload });
  processScanQueue();
}

async function processScanQueue() {
  if (scanQueueRunning) return;
  scanQueueRunning = true;
  // One coalesced client_state PUT for the whole batch instead of one per book.
  await withBulkClientState(async () => {
    while (scanQueue.length) {
      const task = scanQueue.shift();
      const { id } = task;
      const row = rowById(id);
      if (!row || !(row.book.title || "").trim()) continue;
      status(`AUTO SCAN :: ${row.book.title}`);
      try {
        const scans = await runRowScans(row);
        status(`AUTO SCAN DONE :: ${row.book.title} :: ${scanStatusLine(scans)}`);
        // rowById()'s row has no scans field — pass the freshly-fetched ones
        if (task.autoDownload) maybeAutoDownloadIa({ ...row, scans });
      } catch (e) {
        statusErr(`AUTO SCAN FAILED :: ${row.book.title}`);
      }
      scheduleRenderChecked();
    }
  });
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
  for (const row of rows) queueScan(row.id, false);
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
  if (!row) return false;
  if (row.kind === "manual") {
    const prior = getVerify(row, source);
    const priorUrl = getManualUrl(row, source);
    let res, data;
    try {
      res = await fetch(`/api/manual/${encodeURIComponent(id)}/verify`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ source, state: verdict }),
      });
      data = await res.json().catch(() => ({}));
    } catch (e) {
      statusErr(`${source.toUpperCase()} MATCH COULD NOT BE SAVED`);
      return false;
    }
    if (!res.ok || !data.ok || !data.entry) {
      statusErr(`${source.toUpperCase()} MATCH COULD NOT BE SAVED`);
      return false;
    }
    const i = state.manual.findIndex((x) => x.id === id);
    if (i >= 0) state.manual[i] = data.entry;
    if (track) {
      pushOp(`verify ${source} ${verdict} on ${row.book.title.slice(0, 30)}`,
        async () => {
          await setVerify(id, source, prior, false);
          if (priorUrl) await setManualUrl(id, source, priorUrl, false);
        },
        () => setVerify(id, source, verdict, false),
        { kind: "manual-verify", id, source, before: prior, beforeUrl: priorUrl });
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
  if (source === "internet_archive" && verdict === "approved") {
    // renderChecked refreshes rowsById; use that row when available so the
    // download sees the persisted verdict and latest scan metadata.
    maybeAutoDownloadVerifiedIa(state.rowsById.get(String(id)) || row, verdict);
  }
  return true;
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
          () => setManualUrl(id, source, url, false),
          { kind: "manual-url", id, source, before: prior });
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

let msrcOpenValue = "";   // URL at open time, to detect an unsaved edit

function openManualSource(id, source) {
  state.msrcTarget = { id: String(id), source };
  const row = state.rowsById.get(String(id));
  const names = { whl: "WHL", internet_archive: "INTERNET ARCHIVE", hathitrust: "HATHITRUST" };
  el("msrc-label").textContent =
    `${names[source] || source} :: ${row ? row.book.title : id}`;
  el("msrc-url").value = row ? getManualUrl(row, source) : "";
  msrcOpenValue = el("msrc-url").value;
  el("msrc-msg").textContent = "";
  el("msrc-overlay").hidden = false;
  el("msrc-url").focus();
}

// Escape/backdrop/close with an edited URL confirms before discarding;
// the save path passes force after it has persisted the value.
async function closeManualSource(force) {
  if (force !== true && !el("msrc-overlay").hidden &&
      el("msrc-url").value.trim() !== msrcOpenValue.trim() &&
      state.settings.confirmDiscard !== false) {
    if (!(await confirmDialog({
      title: "Discard link",
      message: "Discard the unsaved source link?",
      confirmLabel: "Discard",
      cancelLabel: "Keep editing",
      danger: true,
    }))) return;
  }
  el("msrc-overlay").hidden = true;
}

async function saveManualSource(clear) {
  const t = state.msrcTarget;
  if (!t) return;
  const url = clear ? "" : el("msrc-url").value.trim();
  if (url && !/^https?:\/\//i.test(url)) {
    el("msrc-msg").textContent = "URL must start with http(s)://";
    return;
  }
  await setManualUrl(t.id, t.source, url);
  closeManualSource(true);
}

// --- Analyze tab -------------------------------------------------------------------
// AI over VERIFIED builds (status ready/uploaded): summary, About article,
// category suggestions, page-aligned translations, anchored annotations, and
// the relevance assessment (internal only). Long jobs run server-side and are
// polled like OCR jobs. DeepSeek is the default provider (Settings > AI).

let anAboutMd = null;          // live markdown editor for the About article
const anJobs = new Map();      // job id -> {kind, buildId}
let anPollTimer = null;
let anFacsimileSeq = 0;
let anFacsimileObserver = null;

function anSelected() {
  const b = state.anSel && state.builds[state.anSel] ? state.builds[state.anSel] : null;
  return b && anAnalyzable(b) ? b : null;
}

function anAnalyzable(b) { return b.status === "ready" || b.status === "uploaded"; }

async function renderAnalyze() {
  await loadBuilds();
  if (ocrState.book && anAnalyzable(state.builds[ocrState.book] || {}))
    state.anSel = ocrState.book;
  if (state.anSel && !anAnalyzable(state.builds[state.anSel] || {})) state.anSel = null;
  renderAnList();
  renderAnMain();
  renderAnFacsimile();
  updateAnProvider();
}

// --- Workbench shell (#133 stage 1) --------------------------------------------
// One book-centred surface: a compact phase rail (Record / Source / Text /
// Knowledge / Publish) over ONE shared selection. The phases host the former
// Editor and Analyze panes, moved with their ids and event contracts intact.

const WB_PHASES = ["record", "source", "text", "knowledge", "publish"];

// Legacy adapter: the Analyze workspace toggle became Workbench phases.
function setAnalyzeWorkspace(which, persist) {
  setWorkbenchPhase(which === "document" ? "text" : "knowledge", persist);
}

function wbActivePhase() {
  const btn = document.querySelector("#wb-rail .wb-phase-btn.active");
  return btn ? btn.dataset.phase : "record";
}

function setWorkbenchPhase(phase, persist) {
  if (!WB_PHASES.includes(phase)) phase = "record";
  document.querySelectorAll("#wb-rail .wb-phase-btn").forEach((b) =>
    b.classList.toggle("active", b.dataset.phase === phase));
  document.querySelectorAll("#wb-main .wb-phase").forEach((p) =>
    p.classList.toggle("active", p.id === "phase-" + phase));
  if (persist) {
    state.settings.workbenchPhase = phase;
    saveSettings();
  }
  applyWorkbenchGates();
  // entering a phase refreshes its panes, as its old tab switch did
  if (phase === "record") renderBuildEditor();
  else if (phase === "source") {
    if (activeBuildTab() === "btab-resources") refreshResourcesTab();
    else refreshSourceTab();
  } else if (phase === "text") renderOcrTab();
  else if (phase === "knowledge") { renderAnMain(); renderAnFacsimile(); }
  else if (phase === "publish") {
    const b = anSelected();
    if (b) { renderAnBundle(b); renderAnIndexCard(b); }
  }
}

// Draft gating: what the Analyze tab used to lock stays locked per phase —
// Text / Knowledge / Publish want a verified entry; Record / Source never lock.
// The Publish phase keeps its action bar visible for drafts: the verify toggle
// that lifts the lock lives there.
function wbLockNote(phase) {
  const b = currentBuild();
  if (!b || anAnalyzable(b)) return "";
  if (phase === "publish") {
    return "This entry is a draft — mark it verified (the check above) to " +
      "enable publishing and the bundle.";
  }
  return "This entry is a draft — verify it in the Publish phase to unlock " +
    (phase === "text" ? "text work." : "analysis.");
}

function applyWorkbenchGates() {
  const b = currentBuild();
  const textNote = wbLockNote("text");
  el("wb-text-locked").hidden = !textNote;
  el("wb-text-locked").textContent = textNote;
  el("ocr-document-workspace").hidden = !!textNote;
  const knowNote = wbLockNote("knowledge");
  el("wb-knowledge-locked").hidden = !knowNote;
  el("wb-knowledge-locked").textContent = knowNote;
  el("ocr-analysis-workspace").hidden = !!knowNote;
  const pubNote = b ? wbLockNote("publish") : "Select a book on the left.";
  el("wb-publish-locked").hidden = !pubNote;
  el("wb-publish-locked").textContent = pubNote;
  el("wb-publish-actions").hidden = !b;
  el("wb-publish-cards").hidden = !b || !!pubNote;
  el("wb-source-empty").hidden = !!b;
  el("wb-source-body").hidden = !b;
}

// Per-phase readiness, computed CLIENT-side from data already loaded: the
// build record plus the entry-folder manifest (#135 provenance rows).
function wbReadiness(b) {
  const folder = (ocrState.books || {})[b.id] || null;
  const ocr = (folder && folder.ocr) || [];
  const r = {};
  r.record = String(b.title || "").trim()
    ? { state: "ok", note: "Title present" }
    : { state: "todo", note: "No title yet" };
  r.source = ocrBookPdf(b.id)
    ? { state: "ok", note: "PDF attached" }
    : { state: "todo", note: "No PDF attached" };
  const staleOcr = ocr.filter((f) => f.stale === true).length;
  r.text = !ocr.length
    ? { state: "todo", note: "No OCR yet" }
    : staleOcr
      ? { state: "warn", badge: staleOcr,
          note: `${staleOcr} OCR file${staleOcr === 1 ? "" : "s"} outdated` }
      : b.ocr_verified
        ? { state: "ok", note: `OCR verified — ${b.ocr_verified}` }
        : { state: "warn",
            note: `Needs review${b.ocr_quality ? ` — quality: ${b.ocr_quality}` : ""}` };
  const summary = (folder && folder.summary) || {};
  const about = (folder && folder.about) || {};
  const analysis = (folder && folder.analysis) || [];
  const translations = (folder && folder.translations) || [];
  const have = [summary.exists && "summary", about.exists && "about",
                analysis.length && "page analysis",
                translations.length && "translations"].filter(Boolean);
  const outdated = (summary.stale === true ? 1 : 0) +
    (about.stale === true ? 1 : 0) +
    analysis.filter((a) => a.stale === true).length +
    translations.filter((t) => t.stale === true).length;
  r.knowledge = !have.length
    ? { state: "todo", note: "No analysis yet" }
    : outdated
      ? { state: "warn", badge: outdated, note: `${outdated} outdated` }
      : { state: "ok", note: "Has " + have.join(", ") };
  r.publish = b.status === "uploaded"
    ? { state: "ok",
        note: b.published_slug ? `Published — ${b.published_slug}` : "Published" }
    : b.status === "ready"
      ? { state: "warn", note: "Verified — ready to publish" }
      : { state: "todo", note: "Draft" };
  return r;
}

function renderWorkbenchChips() {
  const host = el("wb-chips");
  if (!host) return;
  const b = currentBuild();
  if (!b) { host.innerHTML = ""; return; }
  const r = wbReadiness(b);
  host.innerHTML = WB_PHASES.map((p) => {
    const c = r[p] || { state: "todo", note: "" };
    return `<button class="wb-chip ${c.state}" type="button" data-phase="${p}" ` +
      `data-tip="${esc(c.note || "")}">${p[0].toUpperCase()}${p.slice(1)}${
      c.badge ? ` ${c.badge}` : ""}</button>`;
  }).join("");
}

// head + chips + gates: everything that tracks the shared selection
function renderWorkbench() {
  const b = currentBuild();
  el("wb-title").textContent = b ? (b.title || "(untitled)") : "No book selected";
  el("wb-sub").textContent = b
    ? `${b.authors || ""}${b.authors && b.year ? " · " : ""}${b.year || ""}`
    : "";
  renderWorkbenchChips();
  applyWorkbenchGates();
}

// THE selection entry point (#133): one book for every phase. state.buildSel,
// ocrState.book and state.anSel remain aliases of the same choice, updated
// together, so every existing consumer keeps working unchanged.
async function selectWorkbenchBook(bid, phase) {
  if (bid !== state.buildSel && buildIsDirty() &&
      state.settings.confirmDiscard !== false &&
      !(await confirmDialog({
        title: "Discard entry changes",
        message: "Discard unsaved changes to this entry?",
        confirmLabel: "Discard changes",
        cancelLabel: "Keep editing",
        danger: true,
      }))) return;
  state.buildSel = bid;
  const b = state.builds[bid];
  // the queue view follows the book so the selection is always visible
  if (b) state.buildsTab = b.status === "uploaded" ? "uploaded" : "pending";
  renderBuildEditor();
  if (wbActivePhase() === "source" && activeBuildTab() === "btab-resources") {
    refreshResourcesTab();
  }
  selectOcrBook(bid);          // async: loads the folder's OCR documents
  if (phase) setWorkbenchPhase(phase, false);
  renderWorkbench();
}

function setJobsDrawer(open, persist) {
  const body = el("wb-jobs-body");
  if (!body) return;
  body.hidden = !open;
  el("ocr-queue-splitter").hidden = !open;
  const t = el("wb-jobs-toggle");
  t.innerHTML = open ? "&#9662;" : "&#9656;";
  t.dataset.tip = open ? "Hide the jobs queue" : "Show the jobs queue";
  t.setAttribute("aria-expanded", String(open));
  if (persist) {
    state.settings.jobsDrawerOpen = !!open;
    saveSettings();
  }
  if (open) renderOcrQueue();
}

function initWorkbench() {
  for (const btn of document.querySelectorAll("#wb-rail .wb-phase-btn")) {
    btn.addEventListener("click", () => setWorkbenchPhase(btn.dataset.phase, true));
  }
  // a readiness chip is also a jump to its phase
  el("wb-chips").addEventListener("click", (ev) => {
    const chip = ev.target.closest("[data-phase]");
    if (chip) setWorkbenchPhase(chip.dataset.phase, true);
  });
  el("wb-jobs-toggle").addEventListener("click", () =>
    setJobsDrawer(el("wb-jobs-body").hidden, true));
  setJobsDrawer(!!state.settings.jobsDrawerOpen, false);
  // the rights select left the entry form (it lives in the Publish phase) but
  // still saves with the form — keep the dirty flag in step
  el("b-rights").addEventListener("change", () => { buildDirty = true; });
  renderWorkbench();
}

function decorateAnFacsimile() {
  const box = el("an-facsimile");
  if (!box) return;
  const d = ocrSelDoc();
  const bid = d && d.buildId;
  for (const row of box.querySelectorAll(".an-fac-page")) {
    const page = +row.dataset.page;
    const key = `${bid}:${docSrcKey(d)}:${page}`;
    const staged = bid ? ocrState.analysisTags.get(key) : null;
    const running = [...anJobs.values()].some((job) => !job.finished &&
      job.buildId === bid && (job.src || "primary") === docSrcKey(d) &&
      Array.isArray(job.pages) && job.pages.includes(page));
    row.classList.toggle("pg-sel", ocrState.pageSel.has(page));
    row.classList.toggle("pg-analysis-staged", !!staged);
    row.classList.toggle("pg-analysis-running", running);
    const badge = row.querySelector(".an-fac-badge");
    if (badge) {
      badge.hidden = !staged && !running;
      badge.textContent = running ? "ANALYZING" : "STAGED";
    }
  }
}

async function renderAnFacsimile() {
  const box = el("an-facsimile");
  if (!box || el("ocr-analysis-workspace").hidden || box.offsetParent === null) return;
  const seq = ++anFacsimileSeq;
  if (anFacsimileObserver) { anFacsimileObserver.disconnect(); anFacsimileObserver = null; }
  const d = ocrSelDoc();
  const b = d && d.buildId ? state.builds[d.buildId] : null;
  if (!d || !b || !anAnalyzable(b)) {
    box.innerHTML = `<p class="empty">Select an available verified book with OCR data.</p>`;
    return;
  }
  let sections = ocrPageSections(d.text);
  if (!sections) sections = { pre: "", map: new Map([[1, d.text]]) };
  const pages = [...sections.map.keys()].sort((a, z) => a - z);
  if (!pages.length) {
    box.innerHTML = `<p class="empty">This artifact has no page-aligned OCR text.</p>`;
    return;
  }
  const pdf = docPdf(d);
  let info = pdf ? ocrState.pdfInfo[pdf] : null;
  if (pdf && !info) {
    try {
      const data = await (await fetch("/api/pdf/info?path=" + encodeURIComponent(pdf))).json();
      if (data.ok) { info = data; ocrState.pdfInfo[pdf] = data; }
    } catch (e) { /* a text-only facsimile still renders */ }
  }
  if (seq !== anFacsimileSeq || ocrSelDoc() !== d) return;
  ocrState.pages = sections;
  ocrState.pagesSrc = docSrcKey(d);
  box.innerHTML = pages.map((page) => {
    const dim = info && info.dims && info.dims[page - 1];
    const ratio = dim && dim[0] > 0 && dim[1] > 0
      ? `aspect-ratio:${dim[0]} / ${dim[1]};` : "aspect-ratio:3 / 4;";
    return `<article class="an-fac-page" data-page="${page}">
      <button class="an-fac-pagebar" type="button" data-tip="Select page ${page}; Ctrl+click extends a range">
        <span>Page ${page}</span><span class="an-fac-badge" hidden></span>
      </button>
      <div class="ocr-pglayout" data-lay="${page}" style="${ratio}"></div>
    </article>`;
  }).join("");
  const panes = [...box.querySelectorAll(".ocr-pglayout")];
  const fill = (pane) => fillOcrLayout(pane, pdf);
  if ("IntersectionObserver" in window) {
    anFacsimileObserver = new IntersectionObserver((entries) => {
      for (const entry of entries) {
        if (!entry.isIntersecting) continue;
        anFacsimileObserver.unobserve(entry.target);
        fill(entry.target);
      }
    }, { root: box, rootMargin: "500px 0px" });
    panes.forEach((pane) => anFacsimileObserver.observe(pane));
  } else {
    panes.forEach(fill);
  }
  decorateAnFacsimile();
}

function onAnFacsimileClick(ev) {
  const bar = ev.target.closest(".an-fac-pagebar");
  if (!bar) return;
  const row = bar.closest(".an-fac-page");
  const page = row ? +row.dataset.page : 0;
  if (!page) return;
  if (ev.ctrlKey && ocrState.selAnchor) {
    const from = Math.min(ocrState.selAnchor, page);
    const to = Math.max(ocrState.selAnchor, page);
    for (let n = from; n <= to; n++) ocrState.pageSel.add(n);
  } else if (ocrState.pageSel.has(page)) {
    ocrState.pageSel.delete(page);
    ocrState.selAnchor = page;
  } else {
    ocrState.pageSel.add(page);
    ocrState.selAnchor = page;
  }
  decorateAnFacsimile();
  decorateOcrPages();
  updateOcrStagedMsg();
}

// show which provider/model an analysis will run on, and warn when no key is set
async function updateAnProvider() {
  const host = el("an-provider");
  if (!host) return;
  const s = state.settings;
  const model = (s.aiModel || "").trim() || "deepseek-chat";
  const base = (s.aiBase || "").trim();
  const provider = base ? base.replace(/^https?:\/\//, "").split("/")[0] : "DeepSeek";
  let hasKey = false;
  try {
    const sec = await (await fetch("/api/secrets")).json();
    hasKey = !!String(sec.aiKey || "").trim();
  } catch (e) { /* leave as no-key */ }
  host.classList.toggle("an-warn", !hasKey);
  host.textContent = hasKey
    ? `${provider} · ${model}`
    : "No AI key — set one in Settings > AI";
}

function renderAnList() {
  const ul = el("an-list");
  const items = Object.values(state.builds)
    .filter(anAnalyzable)
    .sort((a, b) => (a.title || "").localeCompare(b.title || ""));
  ul.innerHTML = items.map((b) => {
    const sel = state.anSel === b.id;
    return `<li class="build-item an-item${sel ? " active" : ""}"
      data-id="${esc(b.id)}">
      <div class="bi-title">${esc(b.title || "(untitled)")}</div>
      <div class="bi-meta">${esc(b.authors || "")}${b.year ? " · " + esc(b.year) : ""}
        · ${b.status === "uploaded" ? "published" : esc(b.status)}</div></li>`;
  }).join("") || `<li class="empty">No verified entries available.</li>`;
  el("an-count").textContent = items.length ? `${items.length} available` : "";
}

function anSelect(id) {
  const b = state.builds[id];
  if (!b || !anAnalyzable(b)) return;
  state.anSel = id;
  selectWorkbenchBook(id);
}

function activeAnPane() {
  const t = document.querySelector("#an-tabs .pane-tab.active");
  return t ? t.dataset.antab : "an-overview";
}

function switchAnPane(id) {
  document.querySelectorAll("#an-tabs .pane-tab").forEach((t) =>
    t.classList.toggle("active", t.dataset.antab === id));
  document.querySelectorAll("#ocr-analysis-workspace .an-pane").forEach((p) =>
    p.classList.toggle("active", p.id === id));
  renderAnPane(id);
}

function renderAnMain() {
  const b = anSelected();
  el("an-empty").hidden = !!b;
  el("an-work").hidden = !b;
  if (!b) return;
  el("an-title").textContent = b.title || "(untitled)";
  el("an-sub").textContent =
    `${b.authors || ""}${b.year ? " · " + b.year : ""} · ` +
    (b.status === "uploaded" ? "published" : "verified");
  renderAnPane(activeAnPane());
}

function renderAnPane(id) {
  const b = anSelected();
  if (!b) return;
  if (id === "an-overview") loadAnOverview(b);
  else if (id === "an-cats") renderAnCats(b);
  else if (id === "an-trans") loadAnTranslations(b);
  else if (id === "an-notes") loadAnNotes(b);
  else if (id === "an-passages") loadAnPassages(b);
  else if (id === "an-rel") renderAnRelevance(b);
  else if (id === "an-bundle") renderAnBundle(b);
}

// --- jobs: start + poll -------------------------------------------------------

async function anStartJob(path, body, label, btn) {
  // guard re-entry: a second job of the same kind on the same entry would race
  // (two summaries overwrite; a translate recomputes and re-does pages)
  for (const m of anJobs.values()) {
    if (!m.finished && path !== "/api/analyze/pages" &&
        m.kind === label && m.buildId === body.build_id) {
      el("an-msg").textContent = `${label} already running…`;
      return null;
    }
  }
  el("an-msg").textContent = "";
  body.engine = body.engine || state.settings.textAnalysisService || "configured";
  if (btn) btn.disabled = true;
  try {
    const res = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || !data.ok) {
      if (btn) btn.disabled = false;
      el("an-msg").textContent = data.error || "failed";
      statusErr(`ANALYZE :: ${(data.error || "FAILED").toUpperCase()}`);
      return null;
    }
    const build = state.builds[body.build_id] || {};
    anJobs.set(data.job, {
      kind: label, buildId: body.build_id, btn, path,
      book: build.title || body.build_id,
      engine: data.engine || body.engine || "configured",
      pages: data.pages || body.pages || [], src: body.src || "primary",
      doc: data.doc || body.doc || "", artifact: data.artifact || "",
      status: "Starting...", at: new Date().toLocaleTimeString(), finished: false,
    });
    el("an-msg").textContent = `${label} — starting…`;
    status(`ANALYZE :: ${label.toUpperCase()} STARTED`);
    renderOcrQueue();
    decorateAnFacsimile();
    anEnsurePolling();
    return data;
  } catch (e) {
    if (btn) btn.disabled = false;
    el("an-msg").textContent = "request failed";
    return null;
  }
}

function anEnsurePolling() {
  if (anPollTimer || ![...anJobs.values()].some((job) => !job.finished)) return;
  anPollTimer = setInterval(async () => {
    for (const [id, meta] of [...anJobs]) {
      if (meta.finished) continue;
      let job = null;
      try {
        const res = await fetch(`/api/analyze/job/${id}`);
        if (res.status === 404) {
          meta.finished = true;
          meta.status = "Lost - the server restarted mid-job";
          if (meta.btn) meta.btn.disabled = false;
          continue;
        }
        job = await res.json();
      } catch (e) { continue; }
      const pct = job.total ? Math.round((job.done / job.total) * 100) : 0;
      meta.status = job.status === "running"
        ? `Running - ${job.done}/${job.total}` : String(job.status || "Running");
      meta.pages = job.pages || meta.pages;
      meta.artifact = job.artifact || meta.artifact;
      status(`ANALYZE :: ${meta.kind.toUpperCase()} :: ${job.done}/${job.total} (${pct}%)` +
        (job.errors ? ` :: ${job.errors} ERRORS` : ""));
      // in-pane progress (only while the user is on that entry)
      if (state.anSel === meta.buildId && !job.status.startsWith("done") &&
          job.status !== "error") {
        el("an-msg").textContent = `${meta.kind} — ${job.done}/${job.total}` +
          (job.errors ? ` (${job.errors} errors)` : "");
      }
      if (job.status.startsWith("done") || job.status === "error" ||
          job.status === "cancelled" || job.status === "interrupted") {
        meta.finished = true;
        meta.status = job.status === "error"
          ? `Error - ${job.error || "analysis failed"}`
          : `${job.status}${job.note ? ` - ${job.note}` : ""}`;
        if (meta.btn) meta.btn.disabled = false;
        if (job.status === "error") {
          statusErr(`ANALYZE :: ${meta.kind.toUpperCase()} FAILED :: ${job.error}`);
          if (state.anSel === meta.buildId) el("an-msg").textContent = job.error;
        } else {
          status(`ANALYZE :: ${meta.kind.toUpperCase()} ${job.status.toUpperCase()}` +
            (job.note ? ` :: ${job.note}` : ""));
          if (["relevance", "summarize", "about"].includes(meta.kind)) {
            await loadBuilds();
            if (["summarize", "about"].includes(meta.kind) &&
                state.buildSel === meta.buildId && !buildIsDirty()) renderBuildEditor();
          }
          if (meta.path === "/api/analyze/pages") await loadOcrBooks();
          if (state.anSel === meta.buildId) {
            el("an-msg").textContent = "";
            renderAnPane(activeAnPane());
            renderOcrDocs();
            // the Publish phase's Search index card tracks these jobs too
            if (["segment", "index-publish"].includes(meta.kind) &&
                wbActivePhase() === "publish") {
              const cur = state.builds[meta.buildId];
              if (cur) renderAnIndexCard(cur);
            }
          }
        }
      }
    }
    renderOcrQueue();
    decorateAnFacsimile();
    if (![...anJobs.values()].some((job) => !job.finished)) {
      clearInterval(anPollTimer);
      anPollTimer = null;
    }
  }, 1500);
}

// --- Overview: summary + About article -----------------------------------------

async function loadAnOverview(b) {
  try {
    const [s, a, f] = await Promise.all([
      fetch(`/api/builds/${b.id}/summary`).then((r) => r.json()),
      fetch(`/api/builds/${b.id}/about`).then((r) => r.json()),
      fetch(`/api/builds/${b.id}/folder`).then((r) => r.json()).catch(() => ({})),
    ]);
    if (state.anSel !== b.id) return;   // stale response
    el("an-summary").textContent = (s.text || "").trim() || "No summary yet.";
    // manifest staleness: only an explicit true shows the notice (legacy
    // artifacts without a manifest row report null)
    el("an-summary-stale").hidden = !(f.summary && f.summary.stale === true);
    el("an-about-stale").hidden = !(f.about && f.about.stale === true);
    // don't clobber an in-progress edit with a background refresh
    if (anAboutMd && document.activeElement?.closest?.("#an-about-editor") == null) {
      anAboutMd.set(a.text || "");
    }
  } catch (e) { /* leave the pane as-is */ }
}

// --- Categories: assignment + suggestions --------------------------------------

let anSuggestions = [];

function renderAnCats(b) {
  const picker = catPickers["an-cat-picker"];
  picker.set(b.category_ids || []);
  renderAnSuggestions();
}

function renderAnSuggestions() {
  const host = el("an-sugg");
  el("an-sugg-all").hidden = !anSuggestions.some((s) => s.exists && !s.added);
  if (!anSuggestions.length) {
    host.innerHTML = `<p class="pane-note">No suggestions yet.</p>`;
    return;
  }
  host.innerHTML = anSuggestions.map((s, i) =>
    `<div class="an-sugg-row">
      <button class="cad-btn tiny" data-sg="${i}" type="button"
        ${s.added ? "disabled" : ""}>${s.added ? "Added" : s.exists ? "Assign" : "Create + assign"}</button>
      <span class="an-sugg-path${s.exists ? "" : " an-sugg-new"}">${esc(s.path.join(" › "))}</span>
      <span class="an-sugg-why">${esc(s.reason || "")}</span>
    </div>`).join("");
}

// a novel suggested path: create missing nodes along the chain, return leaf id
async function anCreatePath(path) {
  let parent = "";
  for (const name of path) {
    const low = name.toLowerCase();
    let nid = Object.keys(state.taxonomy).find((k) =>
      (state.taxonomy[k].parent || "") === parent &&
      (state.taxonomy[k].name || "").trim().toLowerCase() === low);
    if (!nid) {
      const res = await fetch("/api/categories", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, parent }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || !data.ok) return "";
      state.taxonomy[data.id] = data.node;
      nid = data.id;
    }
    parent = nid;
  }
  return parent;
}

async function anAssignSuggestion(i) {
  const b = anSelected();
  const s = anSuggestions[i];
  if (!b || !s || s.added) return;
  const nid = s.exists ? s.id : await anCreatePath(s.path);
  if (!nid) { el("an-msg").textContent = "could not create the category"; return; }
  const ids = (b.category_ids || []).slice();
  if (!ids.includes(nid)) ids.push(nid);
  if (await patchBuild(b.id, { category_ids: ids }, "assign category")) {
    s.added = true;
    await loadTaxonomy();
    renderAnCats(state.builds[b.id]);
  }
}

// --- Translations ----------------------------------------------------------------

async function loadAnTranslations(b) {
  el("an-trans-view").hidden = true;
  try {
    const data = await (await fetch(`/api/builds/${b.id}/translations`)).json();
    if (state.anSel !== b.id) return;
    const list = data.translations || [];
    el("an-trans-list").innerHTML = list.length
      ? list.map((t) =>
        `<div class="an-trans-row">
          <span class="mono">${esc(t.lang)}</span>
          <span class="tool-label">${t.pages} pages</span>
          ${t.stale ? `<span class="an-trans-stale">${t.stale} outdated</span>
          <button class="cad-btn tiny" data-tstale="${esc(t.lang)}" type="button"
            data-tip="Re-translate the pages whose OCR text changed since">Update</button>` : ""}
          <button class="cad-btn tiny" data-tview="${esc(t.lang)}" type="button">View</button>
          <button class="cad-btn tiny danger" data-tdel="${esc(t.lang)}" type="button">Delete</button>
        </div>`).join("")
      : `<p class="pane-note">No translations yet. Pages translate one by one and
         partial runs resume where they stopped.</p>`;
  } catch (e) { /* keep pane */ }
}

// --- Annotations -----------------------------------------------------------------

async function loadAnNotes(b) {
  try {
    const data = await (await fetch(`/api/builds/${b.id}/annotations`)).json();
    if (state.anSel !== b.id) return;
    const notes = (data.doc && data.doc.notes) || [];
    const counts = { approved: 0, suggested: 0, rejected: 0 };
    notes.forEach((n) => { counts[n.status] = (counts[n.status] || 0) + 1; });
    const orphaned = notes.filter((n) => n.anchor === "orphaned").length;
    el("an-notes-count").textContent = notes.length
      ? `${counts.approved} approved · ${counts.suggested} suggested · ${counts.rejected} rejected`
        + (orphaned ? ` · ${orphaned} orphaned` : "")
      : "";
    el("an-notes-list").innerHTML = notes.length
      ? notes.sort((a, x) => (a.page - x.page) || (a.created_at || "").localeCompare(x.created_at || ""))
        .map((n) =>
          `<div class="an-note an-note-${esc(n.status)}" data-note="${esc(n.id)}">
            <div class="an-note-head">
              <span class="an-note-page">p.${n.page}</span>
              <span class="an-note-kind">${esc(n.kind || "")}</span>
              <span class="tb-spacer"></span>
              <button class="cad-btn tiny" data-napp="1" type="button"
                ${n.status === "approved" ? "disabled" : ""}>Approve</button>
              <button class="cad-btn tiny" data-nrej="1" type="button"
                ${n.status === "rejected" ? "disabled" : ""}>Reject</button>
              <button class="cad-btn tiny danger" data-ndel="1" type="button">&#10005;</button>
            </div>
            ${n.quote ? `<div class="an-note-quote${n.anchor === "orphaned" ? " orphaned" : ""}"${
              n.anchor === "orphaned" ? ' data-tip="Quote no longer matches the page text"' : ""
            }>&ldquo;${esc(n.quote)}&rdquo;</div>` : ""}
            <div class="an-note-body" data-tip="Click to edit">${esc(n.body)}</div>
          </div>`).join("")
      : `<p class="pane-note">No annotations yet. Generated notes arrive as
         suggestions; only approved ones publish.</p>`;
  } catch (e) { /* keep pane */ }
}

async function anNotePatch(bid, payload) {
  const res = await fetch(`/api/builds/${bid}/annotations`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = await res.json().catch(() => ({}));
  if (res.ok && data.ok) loadAnNotes(state.builds[bid]);
  else el("an-msg").textContent = data.error || "update failed";
}

// --- Relevance ---------------------------------------------------------------------

function anCriteria() {
  const c = state.settings.relevanceCriteria;
  return Array.isArray(c) ? c : [];
}

function renderAnRelevance(b) {
  const crits = anCriteria();
  el("an-crit").innerHTML = crits.length
    ? crits.map((c, i) =>
      `<div class="an-crit-row" data-ci="${i}">
        <input class="cad-input an-crit-name" value="${esc(c.name || "")}"
               placeholder="criterion" />
        <input class="cad-input an-crit-desc" value="${esc(c.description || "")}"
               placeholder="what makes a work score high" />
        <button class="cad-btn tiny danger" data-cdel="${i}" type="button">&#10005;</button>
      </div>`).join("")
    : `<p class="pane-note">No criteria yet — define what makes a work relevant
       to the collection, then assess. Scores stay internal: they are never
       published.</p>`;
  const r = b.relevance;
  el("an-relres").innerHTML = !r
    ? `<p class="pane-note">Not assessed yet.</p>`
    : `<div class="an-rel-overall">
         <span class="an-score">${r.overall}/10</span>
         <span>${esc(r.summary || "")}</span>
         <span class="tool-label">${esc(r.model || "")} · ${esc((r.assessed_at || "").slice(0, 10))}</span>
       </div>` +
      (r.criteria || []).map((c) =>
        `<div class="an-rel-row">
          <span class="an-rel-name">${esc(c.name)}</span>
          <span class="an-bar"><span class="an-bar-fill" style="width:${(c.score || 0) * 10}%"></span></span>
          <span class="an-score">${c.score}/10</span>
          <div class="an-rel-why">${esc(c.rationale || "")}</div>
        </div>`).join("");
}

function anSaveCriteria() {
  const rows = [...document.querySelectorAll("#an-crit .an-crit-row")];
  state.settings.relevanceCriteria = rows.map((r, i) => ({
    id: (anCriteria()[i] || {}).id || String(Date.now()) + i,
    name: r.querySelector(".an-crit-name").value.trim(),
    description: r.querySelector(".an-crit-desc").value.trim(),
  })).filter((c) => c.name);
  saveSettings();
}

// --- Bundle ---------------------------------------------------------------------

async function renderAnBundle(b) {
  const bundle = b.bundle || {};
  let langs = [];
  try {
    const data = await (await fetch(`/api/builds/${b.id}/translations`)).json();
    langs = (data.translations || []).map((t) => t.lang);
  } catch (e) { /* offline: no langs */ }
  const chosen = bundle.translations || [];
  const row = (id, label, checked, tip) =>
    `<label class="an-bundle-row" data-tip="${esc(tip)}">
      <input type="checkbox" id="${id}" ${checked ? "checked" : ""} /> ${label}</label>`;
  el("an-bundle-opts").innerHTML =
    row("anb-about", "About article", bundle.about,
        "about.md — shown on the book's page in the public library") +
    row("anb-pages", "Original text, page-aligned", bundle.pages_text,
        "the OCR text layer, one row per page, for the reader's text panel") +
    row("anb-notes", "Approved annotations", bundle.annotations,
        "margin notes — only the ones marked approved") +
    (langs.length
      ? `<div class="tool-label" style="margin-top:6px">Translations</div>` +
        langs.map((l) =>
          `<label class="an-bundle-row"><input type="checkbox" data-anb-lang="${esc(l)}"
            ${chosen.includes(l) ? "checked" : ""} /> ${esc(l)}</label>`).join("")
      : "");
}

async function anSaveBundle() {
  const b = anSelected();
  if (!b) return;
  const bundle = {
    about: el("anb-about").checked,
    pages_text: el("anb-pages").checked,
    annotations: el("anb-notes").checked,
    translations: [...document.querySelectorAll("[data-anb-lang]")]
      .filter((x) => x.checked).map((x) => x.dataset.anbLang),
  };
  if (await patchBuild(b.id, { bundle }, "edit publish bundle")) {
    el("an-bundle-msg").textContent = b.status === "uploaded"
      ? "Saved — republish to apply"
      : "Saved — applies when the entry publishes";
  } else el("an-bundle-msg").textContent = "save failed";
}

// --- Passages (#140): structure-aware search passages over the OCR text ----------

let anPassages = null;    // {doc, state} for the selected book, or null
const AN_PSG_CAP = 300;   // rows rendered; the artifact itself is uncapped

async function loadAnPassages(b) {
  try {
    const data = await (await fetch(`/api/builds/${b.id}/passages`)).json();
    if (state.anSel !== b.id) return;   // stale response
    anPassages = data.ok ? data : null;
  } catch (e) { anPassages = null; }
  renderAnPassages();
}

function psgTokens(t) {
  const s = String(t || "").trim();
  return s ? s.split(/\s+/).length : 0;
}

function renderAnPassages() {
  const host = el("an-psg-rows");
  if (!host) return;
  const doc = anPassages && anPassages.doc;
  const st = (anPassages && anPassages.state) || {};
  const passages = (doc && doc.passages) || [];
  const excluded = new Set((doc && doc.excluded) || []);
  el("an-psg-generate").textContent = doc ? "Regenerate" : "Generate";
  const r = st.recipe || {};
  el("an-psg-recipe").textContent = r.child_min
    ? `${r.child_min}–${r.child_max} / ${r.parent_min}–${r.parent_max} tokens`
    : "";
  el("an-psg-count").textContent = passages.length
    ? `${passages.length} passages · ${excluded.size} excluded` : "";
  el("an-psg-stale").hidden = st.stale !== true;
  el("an-psg-empty").hidden = !!passages.length;
  el("an-psg-view").hidden = true;
  const over = passages.length > AN_PSG_CAP;
  el("an-psg-more").hidden = !over;
  if (over) {
    el("an-psg-more").textContent =
      `Showing the first ${AN_PSG_CAP} of ${passages.length} passages.`;
  }
  let prevParent = null;
  host.innerHTML = passages.slice(0, AN_PSG_CAP).map((p, i) => {
    // a thin separator row where a new parent section begins — under-styled
    const sep = prevParent !== null && p.parent_id !== prevParent
      ? `<tr class="psg-sep"><td colspan="4"></td></tr>` : "";
    prevParent = p.parent_id;
    const pages = p.page_from === p.page_to
      ? String(p.page_from ?? "") : `${p.page_from}–${p.page_to}`;
    const toks = psgTokens(p.text);
    const open = String(p.text || "").trim().split(/\s+/).slice(0, 9).join(" ");
    const out = excluded.has(p.id);
    const next = passages[i + 1];
    const canMerge = !!next && next.parent_id === p.parent_id;
    return sep +
      `<tr class="psg-row${out ? " psg-excluded" : ""}" data-psg="${esc(p.id)}">
      <td class="psg-pages">${esc(pages)}</td>
      <td class="psg-open">${esc(open)}${toks > 9 ? "…" : ""}</td>
      <td class="psg-toks">${toks}</td>
      <td class="psg-acts">
        <button class="cad-btn tiny" data-psg-x="${esc(p.id)}" type="button">${
          out ? "Include" : "Exclude"}</button>
        <button class="cad-btn tiny" data-psg-split="${esc(p.id)}" type="button"
                data-tip="Split at the middle sentence boundary">Split</button>
        <button class="cad-btn tiny" data-psg-merge="${esc(p.id)}" type="button"${
          canMerge ? "" : " disabled"}
                data-tip="Merge with the next passage in the same section">Merge</button>
      </td></tr>`;
  }).join("");
}

async function anPsgPatch(body) {
  const b = anSelected();
  if (!b) return;
  try {
    const res = await fetch(`/api/builds/${b.id}/passages`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || !data.ok) {
      el("an-psg-msg").textContent = data.error || "edit failed";
      return;
    }
    el("an-psg-msg").textContent = "";
    anPassages = data;
    renderAnPassages();
  } catch (e) {
    el("an-psg-msg").textContent = "request failed";
  }
}

function onAnPsgClick(ev) {
  const x = ev.target.closest("[data-psg-x]");
  if (x) {
    const out = x.closest(".psg-row").classList.contains("psg-excluded");
    anPsgPatch(out ? { include: [x.dataset.psgX] }
                   : { exclude: [x.dataset.psgX] });
    return;
  }
  const s = ev.target.closest("[data-psg-split]");
  if (s) { anPsgPatch({ split: { id: s.dataset.psgSplit } }); return; }
  const m = ev.target.closest("[data-psg-merge]");
  if (m) { anPsgPatch({ merge: { id: m.dataset.psgMerge } }); return; }
  const row = ev.target.closest(".psg-row");
  if (!row) return;
  const doc = anPassages && anPassages.doc;
  const p = ((doc && doc.passages) || []).find((q) => q.id === row.dataset.psg);
  if (p) {
    el("an-psg-view").textContent = p.text || "";
    el("an-psg-view").hidden = false;
  }
}

// --- Publish phase: the Search index card (#140) ---------------------------------

async function renderAnIndexCard(b) {
  const stateEl = el("an-index-state");
  if (!stateEl || !b) return;
  stateEl.textContent = "…";
  el("an-index-note").textContent = "";
  el("an-index-versions").innerHTML = "";
  el("an-index-rollback").hidden = true;
  let data = null;
  try {
    data = await (await fetch(
      `/api/knowledge/index/status?build_id=${encodeURIComponent(b.id)}`)).json();
  } catch (e) { /* fall through */ }
  if (state.buildSel !== b.id) return;   // stale response
  if (!data || !data.ok) { stateEl.textContent = "status unavailable"; return; }
  const st = data.state || {};
  const versions = data.versions || [];
  let line;
  if (versions.length) {
    const v = versions[0];
    const when = v.built_at ? new Date(v.built_at).toLocaleString() : "";
    line = `published v${versions.length} · ${v.channel || "stable"}` +
      (when ? ` · ${when}` : "");
    if (st.sha256 && v.source_hash && v.source_hash !== st.sha256) {
      line += " · outdated — the text changed since";
    }
  } else if (!st.exists) line = "no passages";
  else if (st.stale) line = "passages outdated";
  else line = "ready to publish";
  stateEl.textContent = line;
  if (data.warning) el("an-index-note").textContent = data.warning;
  el("an-index-rollback").hidden = versions.length < 2;
  el("an-index-versions").innerHTML = versions.slice(0, 6).map((v, i) => {
    const s = v.stats || {};
    const model = (v.config || {}).model;
    return `<div class="an-index-ver">
      <span class="psg-pages">v${versions.length - i}</span>
      <span>${esc(v.channel || "stable")}</span>
      <span>${v.built_at ? esc(new Date(v.built_at).toLocaleString()) : ""}</span>
      <span>${Number(s.passages) || 0} passages</span>
      <span>${model ? esc(model) : "lexical"}</span>
    </div>`;
  }).join("");
}

async function anIndexPublish() {
  const b = anSelected();
  if (!b) return;
  const btn = el("an-index-publish");
  btn.disabled = true;
  el("an-index-note").textContent = "";
  try {
    const res = await fetch("/api/knowledge/index/publish", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ build_id: b.id }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || !data.ok) {
      btn.disabled = false;
      el("an-index-note").textContent = data.error || "failed";
      return;
    }
    anJobs.set(data.job, {
      kind: "index-publish", buildId: b.id, btn,
      path: "/api/knowledge/index/publish",
      book: b.title || b.id, engine: data.model || "lexical",
      pages: [], doc: "", artifact: data.slug || "",
      status: "Starting...", at: new Date().toLocaleTimeString(),
      finished: false,
    });
    el("an-index-state").textContent = "publishing…";
    status("SEARCH INDEX :: PUBLISH STARTED");
    renderOcrQueue();
    anEnsurePolling();
  } catch (e) {
    btn.disabled = false;
    el("an-index-note").textContent = "request failed";
  }
}

async function anIndexRollback() {
  const b = anSelected();
  if (!b) return;
  if (!(await confirmDialog({
    title: "Roll back search index",
    message: "Remove the newest index version?",
    detail: "The previous version becomes current. The archive entry is untouched.",
    confirmLabel: "Roll back",
    danger: true,
  }))) return;
  try {
    const res = await fetch("/api/knowledge/index/rollback", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ build_id: b.id }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || !data.ok) {
      el("an-index-note").textContent = data.error || "rollback failed";
      return;
    }
    status("SEARCH INDEX :: ROLLED BACK");
    renderAnIndexCard(b);
  } catch (e) {
    el("an-index-note").textContent = "request failed";
  }
}

// --- wiring ----------------------------------------------------------------------

function initAnalyze() {
  // The former top-level Analyze panel now lives beside the OCR facsimile in
  // the unified Analyze workspace. Moving the existing node preserves every
  // pane/editor id and its event contracts while removing the duplicate book list.
  el("an-integrated-host").appendChild(el("an-main"));
  makeCatPicker("an-cat-picker", async (ids) => {
    const b = anSelected();
    if (b && await patchBuildRaw(b.id, { category_ids: ids }, true)) {
      status("CATEGORIES UPDATED");
    }
  });

  el("an-list").addEventListener("click", (ev) => {
    const li = ev.target.closest(".an-item");
    if (li) anSelect(li.dataset.id);
  });
  for (const t of document.querySelectorAll("#an-tabs .pane-tab")) {
    t.addEventListener("click", () => switchAnPane(t.dataset.antab));
  }

  anAboutMd = createMdEditor(el("an-about-editor"));
  el("an-facsimile").addEventListener("click", onAnFacsimileClick);
  el("an-stage-pages").addEventListener("click", stageSelectedAnalysisPages);

  el("an-summarize").addEventListener("click", () => {
    const b = anSelected();
    if (b) anStartJob("/api/analyze/summarize", { build_id: b.id }, "summarize",
                      el("an-summarize"));
  });
  el("an-about-draft").addEventListener("click", async () => {
    const b = anSelected();
    if (!b) return;
    const existing = anAboutMd.get().trim();
    if (existing && !(await confirmDialog({
      title: "Replace About draft",
      message: "Replace the current About draft?",
      detail: "The existing draft text is overwritten by the new one.",
      confirmLabel: "Replace draft",
      danger: true,
    }))) return;
    anStartJob("/api/analyze/about",
               { build_id: b.id, overwrite: !!existing }, "about", el("an-about-draft"));
  });
  el("an-about-save").addEventListener("click", async () => {
    const b = anSelected();
    if (!b) return;
    const res = await fetch(`/api/builds/${b.id}/about`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: anAboutMd.get() }),
    });
    const data = await res.json().catch(() => ({}));
    el("an-msg").textContent = res.ok && data.ok ? "About saved" : (data.error || "save failed");
    if (res.ok && data.ok) {
      await loadBuilds();
      if (state.buildSel === b.id && !buildIsDirty()) renderBuildEditor();
      await loadOcrBooks();
      renderOcrDocs();
    }
  });

  el("an-suggest").addEventListener("click", async () => {
    const b = anSelected();
    if (!b) return;
    el("an-sugg").innerHTML = `<p class="pane-note">Asking…</p>`;
    try {
      const res = await fetch("/api/analyze/categories", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ build_id: b.id }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || !data.ok) {
        el("an-sugg").innerHTML =
          `<p class="pane-note">${esc(data.error || "failed")}</p>`;
        return;
      }
      anSuggestions = (data.suggestions || []).map((s) =>
        Object.assign(s, { added: s.exists && (b.category_ids || []).includes(s.id) }));
      renderAnSuggestions();
    } catch (e) {
      el("an-sugg").innerHTML = `<p class="pane-note">request failed</p>`;
    }
  });
  el("an-sugg").addEventListener("click", (ev) => {
    const btn = ev.target.closest("[data-sg]");
    if (btn) anAssignSuggestion(parseInt(btn.dataset.sg, 10));
  });
  el("an-sugg-all").addEventListener("click", async () => {
    for (let i = 0; i < anSuggestions.length; i++) {
      if (anSuggestions[i].exists && !anSuggestions[i].added) {
        await anAssignSuggestion(i);
      }
    }
    renderAnSuggestions();
  });

  el("an-translate").addEventListener("click", async () => {
    const b = anSelected();
    const lang = el("an-lang").value.trim().toLowerCase();
    if (!b || !lang) { el("an-trans-msg").textContent = "language code?"; return; }
    if (!(await confirmDialog({
      title: "Translate entry",
      message: `Translate this entry into “${lang}”?`,
      detail: "It saves as it goes and resumes if interrupted.",
      cost: "This runs one AI request per untranslated page — a long book " +
        "can be hundreds of calls and use real API credits.",
      confirmLabel: "Translate",
    }))) return;
    el("an-trans-msg").textContent = "";
    anStartJob("/api/analyze/translate", { build_id: b.id, lang },
               `translate ${lang}`, el("an-translate"));
  });
  el("an-trans-list").addEventListener("click", async (ev) => {
    const b = anSelected();
    if (!b) return;
    const stale = ev.target.closest("[data-tstale]");
    if (stale) {
      anStartJob("/api/analyze/translate",
                 { build_id: b.id, lang: stale.dataset.tstale, mode: "stale" },
                 `translate ${stale.dataset.tstale}`, stale);
      return;
    }
    const view = ev.target.closest("[data-tview]");
    const del = ev.target.closest("[data-tdel]");
    if (view) {
      const data = await (await fetch(
        `/api/builds/${b.id}/translations/${encodeURIComponent(view.dataset.tview)}`)).json();
      el("an-trans-view").textContent = data.text || "";
      el("an-trans-view").hidden = false;
    } else if (del) {
      if (!(await confirmDialog({
        title: "Delete translation",
        message: `Delete the ${del.dataset.tdel} translation?`,
        confirmLabel: "Delete",
        danger: true,
      }))) return;
      await fetch(`/api/builds/${b.id}/translations/${encodeURIComponent(del.dataset.tdel)}`,
                  { method: "DELETE" });
      loadAnTranslations(b);
    }
  });

  el("an-annotate").addEventListener("click", () => {
    const b = anSelected();
    if (b) anStartJob("/api/analyze/annotate", { build_id: b.id }, "annotate",
                      el("an-annotate"));
  });
  el("an-notes-list").addEventListener("click", (ev) => {
    const b = anSelected();
    const box = ev.target.closest(".an-note");
    if (!b || !box) return;
    const id = box.dataset.note;
    if (ev.target.closest("[data-napp]")) {
      anNotePatch(b.id, { update: { id, status: "approved" } });
    } else if (ev.target.closest("[data-nrej]")) {
      anNotePatch(b.id, { update: { id, status: "rejected" } });
    } else if (ev.target.closest("[data-ndel]")) {
      anNotePatch(b.id, { remove: id });
    } else if (ev.target.closest(".an-note-body")) {
      const bodyEl = box.querySelector(".an-note-body");
      const old = bodyEl.textContent;
      bodyEl.innerHTML = `<textarea class="cad-input an-note-edit">${esc(old)}</textarea>`;
      const ta = bodyEl.querySelector("textarea");
      ta.focus();
      const done = () => {
        const v = ta.value.trim();
        if (v && v !== old) anNotePatch(b.id, { update: { id, body: v } });
        else loadAnNotes(b);
      };
      ta.addEventListener("blur", done);
      ta.addEventListener("keydown", (kev) => {
        if (kev.key === "Escape") { kev.stopPropagation(); ta.value = old; ta.blur(); }
      });
    }
  });

  el("an-crit-add").addEventListener("click", () => {
    anSaveCriteria();
    state.settings.relevanceCriteria = anCriteria().concat(
      [{ id: String(Date.now()), name: "", description: "" }]);
    const b = anSelected();
    if (b) renderAnRelevance(b);
  });
  el("an-crit").addEventListener("change", anSaveCriteria);
  el("an-crit").addEventListener("click", (ev) => {
    const d = ev.target.closest("[data-cdel]");
    if (!d) return;
    const crits = anCriteria();
    crits.splice(parseInt(d.dataset.cdel, 10), 1);
    state.settings.relevanceCriteria = crits;
    saveSettings();
    const b = anSelected();
    if (b) renderAnRelevance(b);
  });
  el("an-assess").addEventListener("click", () => {
    anSaveCriteria();
    const b = anSelected();
    if (!b) return;
    if (!anCriteria().length) {
      el("an-msg").textContent = "define at least one criterion first";
      return;
    }
    anStartJob("/api/analyze/relevance", { build_id: b.id }, "relevance",
               el("an-assess"));
  });

  el("an-bundle-save").addEventListener("click", anSaveBundle);

  el("an-psg-generate").addEventListener("click", () => {
    const b = anSelected();
    if (b) anStartJob("/api/knowledge/segment", { build_id: b.id }, "segment",
                      el("an-psg-generate"));
  });
  el("an-psg-wrap").addEventListener("click", onAnPsgClick);
  el("an-index-publish").addEventListener("click", anIndexPublish);
  el("an-index-rollback").addEventListener("click", anIndexRollback);

  // Record -> Knowledge jump for the open book (same Workbench selection)
  el("b-analyze").addEventListener("click", () => {
    const b = currentBuild();
    if (!b) return;
    if (!anAnalyzable(b)) {
      el("build-msg").textContent = "mark it verified first";
      return;
    }
    state.anSel = b.id;
    if (ocrState.book !== b.id) selectOcrBook(b.id);
    setWorkbenchPhase("knowledge", true);
  });
}

// --- category taxonomy -------------------------------------------------------------
// The hierarchical vocabulary behind category_ids (docs/library-analyze-design.md).
// state.taxonomy holds the {id: {name, parent}} node map from /api/categories;
// records carry category_ids lists. The old free-text `categories` is display
// fallback only.

async function loadTaxonomy() {
  try {
    const res = await fetch("/api/categories");
    const data = await res.json().catch(() => ({}));
    if (res.ok && data.ok) state.taxonomy = data.nodes || {};
  } catch (e) { /* offline boot: pickers degrade to empty vocab */ }
  for (const p of Object.values(catPickers)) p.refresh();
  if (!el("cat-overlay").hidden) renderCatTree();
  renderChecked();   // table cells resolve names once the vocab lands
}

// root→leaf names for one node; cycle-safe (a bad sync must not hang render)
function catPathNames(id) {
  const names = [], seen = new Set();
  let cur = String(id || "");
  while (cur && state.taxonomy[cur] && !seen.has(cur)) {
    seen.add(cur);
    names.push(state.taxonomy[cur].name || "?");
    cur = String(state.taxonomy[cur].parent || "");
  }
  return names.reverse();
}

function catPathText(id) { return catPathNames(id).join(" › "); }

// leaf names for a record's assignment — what dense table cells show
function catNamesText(ids) {
  return (ids || [])
    .map((i) => (state.taxonomy[i] || {}).name || "")
    .filter(Boolean).join(", ");
}

// a book's categories for display: resolved names, else the legacy text
function bookCatsText(b) {
  if (b && Array.isArray(b.category_ids) && b.category_ids.length) {
    const t = catNamesText(b.category_ids);
    if (t) return t;
  }
  return (b && b.categories) || "";
}

const catPickers = {};   // mount id -> picker, so loadTaxonomy can refresh all

// A chip picker: chips for the assigned nodes + an autocomplete input over
// the taxonomy. Options are labelled with their full path; an unmatched
// entry offers "Create". Mounted into a .cat-picker div; get()/set() speak
// category_ids.
function makeCatPicker(mountId, onChange) {
  const mount = el(mountId);
  let ids = [];
  mount.classList.add("cat-picker");
  mount.innerHTML =
    `<span class="cat-chips"></span>` +
    `<input class="cat-input" type="text" autocomplete="off" ` +
    `placeholder="add category…" />` +
    `<div class="catpick-pop" hidden></div>`;
  const chips = mount.querySelector(".cat-chips");
  const input = mount.querySelector(".cat-input");
  const pop = mount.querySelector(".catpick-pop");
  let active = -1, options = [];

  function renderChips() {
    chips.innerHTML = ids.map((i) =>
      `<span class="cat-chip" data-id="${esc(i)}" data-tip="${esc(catPathText(i))}">` +
      `${esc((state.taxonomy[i] || {}).name || "?")}` +
      `<button type="button" class="cat-x" aria-label="Remove">&#10005;</button></span>`
    ).join("");
  }

  function close() { pop.hidden = true; active = -1; }

  function openPop() {
    const q = input.value.trim().toLowerCase();
    options = Object.keys(state.taxonomy)
      .filter((i) => !ids.includes(i))
      .map((i) => ({ id: i, path: catPathText(i) }))
      .filter((o) => !q || o.path.toLowerCase().includes(q))
      .sort((a, b) => a.path.localeCompare(b.path))
      .slice(0, 12);
    const exact = Object.values(state.taxonomy).some(
      (n) => (n.name || "").toLowerCase() === q);
    const rows = options.map((o, i) =>
      `<div class="catpick-item${i === active ? " active" : ""}" data-i="${i}">` +
      `${esc(o.path)}</div>`);
    if (q && !exact) {
      rows.push(`<div class="catpick-item catpick-new${active === options.length ? " active" : ""}" ` +
        `data-new="1">Create &ldquo;${esc(input.value.trim())}&rdquo;</div>`);
    }
    pop.innerHTML = rows.join("");
    pop.hidden = !rows.length;
  }

  async function createAndAdd(name) {
    try {
      const res = await fetch("/api/categories", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name }),
      });
      const data = await res.json().catch(() => ({}));
      if (res.ok && data.ok) {
        state.taxonomy[data.id] = data.node;
        add(data.id);
        for (const p of Object.values(catPickers)) if (p !== api) p.refresh();
      } else statusErr(`CATEGORY :: ${(data.error || "create failed").toUpperCase()}`);
    } catch (e) { statusErr("CATEGORY :: CREATE FAILED"); }
  }

  function add(id) {
    if (!ids.includes(id)) ids.push(id);
    input.value = "";
    renderChips();
    close();
    if (onChange) onChange(ids.slice());
  }

  function pick(i) {
    if (i >= 0 && i < options.length) add(options[i].id);
    else if (input.value.trim()) createAndAdd(input.value.trim());
  }

  input.addEventListener("input", () => { active = -1; openPop(); });
  input.addEventListener("focus", openPop);
  input.addEventListener("blur", () => setTimeout(close, 150));
  input.addEventListener("keydown", (ev) => {
    const total = pop.hidden ? 0 : pop.children.length;
    if (ev.key === "ArrowDown" && total) {
      ev.preventDefault(); active = (active + 1) % total; openPop();
    } else if (ev.key === "ArrowUp" && total) {
      ev.preventDefault(); active = (active - 1 + total) % total; openPop();
    } else if (ev.key === "Enter") {
      ev.preventDefault();
      if (total) pick(active >= 0 ? active : 0);
      else if (input.value.trim()) createAndAdd(input.value.trim());
    } else if (ev.key === "Escape" && !pop.hidden) {
      ev.stopPropagation(); close();
    } else if (ev.key === "Backspace" && !input.value && ids.length) {
      ids.pop(); renderChips();
      if (onChange) onChange(ids.slice());
    }
  });
  // mousedown, not click: the input's blur fires first and closes the pop
  pop.addEventListener("mousedown", (ev) => {
    ev.preventDefault();
    const item = ev.target.closest(".catpick-item");
    if (!item) return;
    if (item.dataset.new) pick(-1);
    else pick(parseInt(item.dataset.i, 10));
  });
  chips.addEventListener("click", (ev) => {
    const x = ev.target.closest(".cat-x");
    if (!x) return;
    const id = x.closest(".cat-chip").dataset.id;
    ids = ids.filter((i) => i !== id);
    renderChips();
    if (onChange) onChange(ids.slice());
  });

  const api = {
    get: () => ids.slice(),
    set: (v) => { ids = (v || []).slice(); input.value = ""; renderChips(); close(); },
    refresh: renderChips,
  };
  catPickers[mountId] = api;
  return api;
}

// The categories cell of the combined table edits through a floating picker,
// not the plain text input — the field is structured now.
function startEditCategories(td, row) {
  hideTip();
  const before = ((row.book && row.book.category_ids) || []).slice();
  const holder = document.createElement("div");
  holder.className = "catcell-pop";
  holder.innerHTML = `<div id="catcell-picker"></div>`;
  document.body.appendChild(holder);
  const r = td.getBoundingClientRect();
  holder.style.left = `${Math.min(r.left, window.innerWidth - 340)}px`;
  holder.style.top = `${r.bottom + 2}px`;
  const picker = makeCatPicker("catcell-picker");
  picker.set(before);
  holder.querySelector(".cat-input").focus();

  let done = false;
  const finish = (commit) => {
    if (done) return;
    done = true;
    const after = picker.get();
    delete catPickers["catcell-picker"];
    holder.remove();
    document.removeEventListener("mousedown", onAway, true);
    document.removeEventListener("keydown", onKey, true);
    if (commit && JSON.stringify(after) !== JSON.stringify(before)) {
      applyCategoryEdit(row, before, after);
    } else renderChecked();
  };
  const onAway = (ev) => { if (!holder.contains(ev.target)) finish(true); };
  const onKey = (ev) => {
    if (ev.key === "Escape") {
      // let an open suggestion list close first; second Escape cancels
      if (holder.querySelector(".catpick-pop").hidden) { ev.stopPropagation(); finish(false); }
    } else if (ev.key === "Enter" && !ev.target.closest(".cat-input")) finish(true);
  };
  document.addEventListener("mousedown", onAway, true);
  document.addEventListener("keydown", onKey, true);
}

async function applyCategoryEdit(row, before, after) {
  const label = `edit categories of ${String(row.book.title || "").slice(0, 32)}`;
  if (row.kind === "manual") {
    // _preserve: assigning categories doesn't change the book's identity, so
    // checks/scans/verifications survive
    const patch = (ids) =>
      patchManualFields(row.id, { category_ids: ids, _preserve: true });
    if (await patch(after)) {
      pushOp(label, () => patch(before), () => patch(after));
      status("CATEGORIES UPDATED");
    } else statusCrit("UPDATE FAILED");
    renderChecked();
    return;
  }
  const entry = state.checked.get(row.id);
  if (!entry) return;
  const setIds = (ids) => {
    const e = state.checked.get(row.id);
    if (e) { e.book = Object.assign({}, e.book, { category_ids: ids }); }
  };
  pushOp(label,
    () => { setIds(before); saveChecked(); renderChecked(); },
    () => { setIds(after); saveChecked(); renderChecked(); });
  setIds(after);
  saveChecked();
  renderChecked();
  status("CATEGORIES UPDATED");
}

// --- the taxonomy manager window (Tools > Categories…) ---------------------------

let catMergeFrom = null;   // node id armed for a merge, or null
let catAdoptPreview = null; // adopt runs dry first; the second click applies

function openCategories() {
  catMergeFrom = null;
  catAdoptPreview = null;
  el("cat-msg").textContent = "";
  el("cat-overlay").hidden = false;
  loadTaxonomy().then(renderCatTree);
}


// --- Publish tab: public-catalogue tree + website-format preview ------------

function publishSlug(v) { return String((v && v.slug) || "").trim(); }

function publishStableCompare(a, b) {
  const left = String(a == null ? "" : a), right = String(b == null ? "" : b);
  const foldedLeft = left.normalize("NFKC").toLowerCase();
  const foldedRight = right.normalize("NFKC").toLowerCase();
  if (foldedLeft < foldedRight) return -1;
  if (foldedLeft > foldedRight) return 1;
  if (left < right) return -1;
  if (left > right) return 1;
  return 0;
}

function publishPositiveVolume(value) {
  const text = String(value == null ? "" : value).trim();
  if (!/^\d+$/.test(text)) return null;
  const number = Number(text);
  return Number.isSafeInteger(number) && number > 0 ? number : null;
}

function publishMemberSort(a, b) {
  const av = publishPositiveVolume(a.volume), bv = publishPositiveVolume(b.volume);
  const an = av !== null, bn = bv !== null;
  if (an !== bn) return an ? -1 : 1;
  if (an && av !== bv) return av - bv;
  const vt = publishStableCompare(a.volume, b.volume);
  if (vt) return vt;
  return publishStableCompare(a.title, b.title) ||
    publishStableCompare(publishSlug(a), publishSlug(b));
}

function publishEntities() {
  const groups = new Map(), singles = [];
  for (const v of state.publishEntries) {
    if (!publishSlug(v)) continue;
    const gid = String(v.group_id || "").trim();
    if (!gid) {
      singles.push({ kind: "book", key: "book:" + publishSlug(v),
        label: String(v.title || "Untitled"), entries: [v] });
      continue;
    }
    if (!groups.has(gid)) groups.set(gid, []);
    groups.get(gid).push(v);
  }
  const out = singles;
  for (const [gid, members] of groups) {
    members.sort(publishMemberSort);
    out.push({ kind: "set", key: "set:" + gid, groupId: gid,
      label: setBaseTitle(members[0]) || String(members[0].title || gid),
      entries: members });
  }
  return out.sort((a, b) => publishStableCompare(a.label, b.label) ||
    publishStableCompare(a.key, b.key));
}

function publishBookLabel(v, inSet) {
  const title = String(v.title || "Untitled");
  if (!inSet) return title;
  const vol = String(v.volume || "").trim();
  return (vol ? `Volume ${vol}` : "Volume") + (title ? ` — ${title}` : "");
}

function publishAliasNodeId(parentNodeId, selectionKey) {
  return `alias:${encodeURIComponent(String(parentNodeId))}:` +
    encodeURIComponent(String(selectionKey));
}

function publishEntityNode(entity, parentNodeId) {
  const nodeId = publishAliasNodeId(parentNodeId, entity.key);
  if (entity.kind === "book") {
    return { kind: "book", nodeId, selectKey: entity.key, label: entity.label,
      entry: entity.entries[0], children: [] };
  }
  return { kind: "set", nodeId, selectKey: entity.key, label: entity.label,
    entries: entity.entries,
    children: entity.entries.map((v) => {
      const selectKey = "book:" + publishSlug(v);
      return { kind: "book", nodeId: publishAliasNodeId(nodeId, selectKey),
        selectKey, label: publishBookLabel(v, true), entry: v, children: [] };
    }) };
}

function publishEntryPaths(v) {
  const raw = Array.isArray(v.category_paths) ? v.category_paths : [];
  const seen = new Set(), out = [];
  for (const p of raw) {
    if (!Array.isArray(p)) continue;
    const clean = p.map((x) => String(x || "").trim()).filter(Boolean);
    const key = JSON.stringify(clean);
    if (clean.length && !seen.has(key)) { seen.add(key); out.push(clean); }
  }
  return out;
}

function publishMostSpecificPaths(paths) {
  return paths.filter((p, i) => !paths.some((q, j) => j !== i && q.length > p.length &&
    p.every((name, n) => q[n] === name)));
}

function publishEntityPaths(entity) {
  const byKey = new Map();
  for (const v of entity.entries) {
    for (const path of publishEntryPaths(v)) byKey.set(JSON.stringify(path), path);
  }
  return publishMostSpecificPaths([...byKey.values()]);
}

function publishYearBucket(raw) {
  const year = Number(raw);
  if (!Number.isInteger(year) || year < 1000 || year > 2999)
    return { key: "undated", label: "Undated", rank: 9999 };
  const start = Math.floor(year / 100) * 100;
  return { key: String(start), label: `${start}–${start + 99}`, rank: start };
}

function publishGroupNode(id, label, entities, rank) {
  return { kind: "group", nodeId: id, selectKey: "", label,
    rank: rank == null ? 0 : rank,
    children: entities.map((entity) => publishEntityNode(entity, id)) };
}

const PUBLISH_UNKNOWN_AUTHOR = Symbol("publish-unknown-author");

function publishTreeModel() {
  const entities = publishEntities();
  const mode = state.settings.publishGroup;
  if (mode === "sets")
    return entities.map((entity) => publishEntityNode(entity, "group:sets:root"));

  if (mode === "author") {
    const buckets = new Map();
    for (const entity of entities) {
      const authors = [...new Set(entity.entries.map((v) => String(v.authors || "").trim())
        .filter(Boolean))];
      for (const author of (authors.length ? authors : [PUBLISH_UNKNOWN_AUTHOR])) {
        if (!buckets.has(author)) buckets.set(author, new Map());
        buckets.get(author).set(entity.key, entity);
      }
    }
    return [...buckets].sort((a, b) => {
      if (a[0] === PUBLISH_UNKNOWN_AUTHOR)
        return b[0] === PUBLISH_UNKNOWN_AUTHOR ? 0 : 1;
      if (b[0] === PUBLISH_UNKNOWN_AUTHOR) return -1;
      return publishStableCompare(a[0], b[0]);
    }).map(([author, rows]) => {
      const unknown = author === PUBLISH_UNKNOWN_AUTHOR;
      const label = unknown ? "Unknown author" : author;
      const id = unknown ? "group:author:unknown" :
        "group:author:" + encodeURIComponent(author);
      return publishGroupNode(id, label, [...rows.values()]);
    });
  }

  if (mode === "date") {
    const buckets = new Map();
    for (const entity of entities) {
      const values = new Map();
      for (const v of entity.entries) {
        const bucket = publishYearBucket(v.year);
        if (bucket.key !== "undated") values.set(bucket.key, bucket);
      }
      if (!values.size) values.set("undated", publishYearBucket(null));
      for (const bucket of values.values()) {
        if (!buckets.has(bucket.key)) buckets.set(bucket.key,
          { bucket, entities: new Map() });
        buckets.get(bucket.key).entities.set(entity.key, entity);
      }
    }
    return [...buckets.values()].sort((a, b) => a.bucket.rank - b.bucket.rank)
      .map(({ bucket, entities: rows }) => publishGroupNode(
        "group:date:" + bucket.key, bucket.label, [...rows.values()], bucket.rank));
  }

  const root = { children: new Map(), entities: new Map() };
  const uncategorized = new Map();
  for (const entity of entities) {
    const paths = publishEntityPaths(entity);
    if (!paths.length) { uncategorized.set(entity.key, entity); continue; }
    for (const path of paths) {
      let branch = root;
      for (let i = 0; i < path.length; i++) {
        const names = path.slice(0, i + 1);
        const key = JSON.stringify(names);
        if (!branch.children.has(key)) branch.children.set(key,
          { id: "group:category:" + key, label: path[i], children: new Map(),
            entities: new Map() });
        branch = branch.children.get(key);
      }
      branch.entities.set(entity.key, entity);
    }
  }
  const categoryNodes = (branch) => {
    const folders = [...branch.children.values()]
      .sort((a, b) => publishStableCompare(a.label, b.label) ||
        publishStableCompare(a.id, b.id))
      .map((child) => ({ kind: "group", nodeId: child.id, selectKey: "",
        label: child.label,
        children: [...categoryNodes(child), ...[...child.entities.values()]
          .map((entity) => publishEntityNode(entity, child.id))] }));
    return folders;
  };
  const out = categoryNodes(root);
  if (uncategorized.size) out.push(publishGroupNode(
    "group:category:uncategorized", "Uncategorized", [...uncategorized.values()], 9999));
  return out;
}

function publishNodeOpen(node, depth) {
  if (state.publishOpen.has(node.nodeId)) return true;
  if (state.publishClosed.has(node.nodeId)) return false;
  return node.kind === "group" && depth === 0;
}

function publishSelectedNodePath(nodes, selectKey) {
  for (const node of nodes) {
    if (node.selectKey && node.selectKey === selectKey) return [node.nodeId];
    const childPath = publishSelectedNodePath(node.children || [], selectKey);
    if (childPath) return [node.nodeId, ...childPath];
  }
  return null;
}

function publishRevealSelected(nodes, selectKey) {
  const path = publishSelectedNodePath(nodes, selectKey);
  if (!path) return null;
  for (const nodeId of path.slice(0, -1)) {
    state.publishClosed.delete(nodeId);
    state.publishOpen.add(nodeId);
  }
  return path;
}

function publishTreeNodeHtml(node, depth) {
  const folder = node.kind === "group" || node.kind === "set";
  const open = folder && publishNodeOpen(node, depth);
  const selected = !!node.selectKey && node.selectKey === state.publishSel;
  const selectable = node.kind === "book" || node.kind === "set";
  const count = node.kind === "set" ? `${node.entries.length}` : "";
  const caret = folder
    ? `<button class="publish-caret" type="button" data-publish-toggle="${esc(node.nodeId)}"
         aria-label="${open ? "Collapse" : "Expand"}" aria-expanded="${open}">${open ? "▾" : "▸"}</button>`
    : `<span class="publish-caret spacer"></span>`;
  const label = selectable
    ? `<button class="publish-tree-label" type="button" data-publish-select="${esc(node.selectKey)}" title="${esc(node.label)}">${esc(node.label)}</button>`
    : folder
      ? `<button class="publish-tree-label" type="button" data-publish-toggle="${esc(node.nodeId)}" aria-expanded="${open}" title="${esc(node.label)}">${esc(node.label)}</button>`
      : `<span class="publish-tree-label" title="${esc(node.label)}">${esc(node.label)}</span>`;
  const children = open && node.children.length
    ? `<div class="publish-tree-children">${node.children
      .map((child) => publishTreeNodeHtml(child, depth + 1)).join("")}</div>` : "";
  return `<div class="publish-tree-node ${node.kind}${selected ? " selected" : ""}"
      data-publish-node="${esc(node.nodeId)}">
    <div class="publish-tree-row" style="--tree-depth:${depth}">${caret}
      <span class="publish-tree-icon" aria-hidden="true">${node.kind === "book" ? "□" : "▣"}</span>
      ${label}${count ? `<span class="publish-tree-count">${count}</span>` : ""}</div>${children}</div>`;
}

function publishFindEntity(key) {
  if (String(key || "").startsWith("book:")) {
    const slug = String(key).slice(5);
    const entry = state.publishEntries.find((v) => publishSlug(v) === slug);
    if (entry) return { kind: "book", key: "book:" + slug,
      label: String(entry.title || "Untitled"), entries: [entry] };
  }
  return publishEntities().find((entity) => entity.key === key) || null;
}

function renderPublishTree(revealSelection = false) {
  const model = publishTreeModel();
  const entities = publishEntities();
  if (!publishFindEntity(state.publishSel) && entities.length) {
    state.publishSel = entities[0].key;
    revealSelection = true;
  }
  if (revealSelection) publishRevealSelected(model, state.publishSel);
  const tree = el("publish-tree");
  tree.removeAttribute("role");
  tree.innerHTML = model.map((node) => publishTreeNodeHtml(node, 0)).join("");
  el("publish-tree-empty").hidden = entities.length > 0;
  el("publish-count").textContent = `${state.publishEntries.length} volume${
    state.publishEntries.length === 1 ? "" : "s"}`;
}

function publishSafeUrl(raw, allowLocal) {
  const value = String(raw || "").trim();
  if (allowLocal && value.startsWith("/")) return value;
  try {
    const u = new URL(value);
    return /^(https?:)$/.test(u.protocol) ? u.href : "";
  } catch (e) { return ""; }
}

function publishThumb(v) {
  return publishSafeUrl(v.thumbnail_url, false) ||
    publishSafeUrl(v.preview_thumbnail, true);
}

function publishCategoryHtml(v, links) {
  return publishEntryPaths(v).map((path) => {
    const text = path.join(" › ");
    return links
      ? `<span class="ppub-cat-link">${esc(text)}</span>`
      : `<span class="ppub-chip">${esc(text)}</span>`;
  }).join(links ? "<br>" : "");
}

function publishMetaRow(label, value) {
  return value ? `<tr><th>${esc(label)}</th><td>${value}</td></tr>` : "";
}

function publishDay(raw) {
  const date = new Date(raw || "");
  return Number.isNaN(date.getTime()) ? "" : date.toISOString().slice(0, 10);
}

function publishMetadata(v) {
  const src = publishSafeUrl(v.source_url, false);
  const rows = [
    publishMetaRow("Author", v.authors ? esc(v.authors) : ""),
    publishMetaRow("Published", v.year ? esc(v.year) : ""),
    publishMetaRow("Publisher", v.publisher ? esc(v.publisher) : ""),
    publishMetaRow("Place", v.publisher_city ? esc(v.publisher_city) : ""),
    publishMetaRow("Edition", v.edition ? esc(v.edition) : ""),
    publishMetaRow("Volume", v.volume ? esc(v.volume) : ""),
    publishMetaRow("Language", v.language ? esc(v.language) : ""),
    publishMetaRow("Pages", v.pages ? esc(v.pages) : ""),
    publishMetaRow("Categories", publishCategoryHtml(v, true)),
    publishMetaRow("Source", src ? `<a href="${esc(src)}" target="_blank" rel="noopener">${esc(new URL(src).hostname.replace(/^www\./, ""))}</a>` : ""),
    publishMetaRow("Copyright", v.copyright_status ? esc(v.copyright_status) : ""),
    publishMetaRow("Added", publishDay(v.created_at)),
  ].join("");
  return `<table class="ppub-meta"><tbody>${rows}</tbody></table>`;
}

function publishImprint(v) {
  return [v.publisher, v.publisher_city, v.year, v.edition,
    v.pages && `${v.pages} pp`, v.volume && `Volume ${v.volume}`]
    .filter(Boolean).map((x) => `<span>${esc(x)}</span>`).join("");
}

function publishUnescapeEntities(value) {
  return String(value).replace(/&amp;/g, "&").replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">").replace(/&quot;/g, '"').replace(/&#39;/g, "'");
}

function publishMarkdownInline(text) {
  let value = text;
  value = value.replace(/`([^`]+)`/g, (_, code) => `<code>${code}</code>`);
  value = value.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
  value = value.replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
  value = value.replace(/(^|[^_\w])_([^_\n]+)_/g, "$1<em>$2</em>");
  value = value.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (match, label, url) => {
    const safe = publishSafeUrl(publishUnescapeEntities(url), false);
    return safe
      ? `<a href="${esc(safe)}" target="_blank" rel="noopener nofollow">${label}</a>`
      : label;
  });
  return value;
}

// Kept in step with website/assets/markdown.js: escape the whole source first,
// allow no raw HTML, and validate every link target before creating an anchor.
function publishMarkdown(src) {
  const lines = esc(String(src || "")).replace(/\r\n?/g, "\n").split("\n");
  const out = [];
  let i = 0, paragraph = [];
  const flushParagraph = () => {
    if (paragraph.length) {
      out.push(`<p>${publishMarkdownInline(paragraph.join(" "))}</p>`);
      paragraph = [];
    }
  };
  while (i < lines.length) {
    const line = lines[i];
    if (/^```/.test(line)) {
      flushParagraph();
      const code = [];
      i++;
      while (i < lines.length && !/^```/.test(lines[i])) code.push(lines[i++]);
      i++;
      out.push(`<pre><code>${code.join("\n")}</code></pre>`);
      continue;
    }
    if (/^\s*$/.test(line)) { flushParagraph(); i++; continue; }
    const heading = line.match(/^(#{1,6})\s+(.*)$/);
    if (heading) {
      flushParagraph();
      const level = heading[1].length;
      out.push(`<h${level}>${publishMarkdownInline(heading[2].trim())}</h${level}>`);
      i++;
      continue;
    }
    if (/^(-{3,}|\*{3,}|_{3,})\s*$/.test(line)) {
      flushParagraph(); out.push("<hr>"); i++; continue;
    }
    if (/^&gt;\s?/.test(line)) {
      flushParagraph();
      const quote = [];
      while (i < lines.length && /^&gt;\s?/.test(lines[i]))
        quote.push(lines[i++].replace(/^&gt;\s?/, ""));
      out.push(`<blockquote>${publishMarkdownInline(quote.join(" "))}</blockquote>`);
      continue;
    }
    if (/^\s*[-*+]\s+/.test(line)) {
      flushParagraph();
      const items = [];
      while (i < lines.length && /^\s*[-*+]\s+/.test(lines[i]))
        items.push(`<li>${publishMarkdownInline(lines[i++].replace(/^\s*[-*+]\s+/, ""))}</li>`);
      out.push(`<ul>${items.join("")}</ul>`);
      continue;
    }
    if (/^\s*\d+\.\s+/.test(line)) {
      flushParagraph();
      const items = [];
      while (i < lines.length && /^\s*\d+\.\s+/.test(lines[i]))
        items.push(`<li>${publishMarkdownInline(lines[i++].replace(/^\s*\d+\.\s+/, ""))}</li>`);
      out.push(`<ol>${items.join("")}</ol>`);
      continue;
    }
    paragraph.push(line.trim());
    i++;
  }
  flushParagraph();
  return out.join("\n");
}

function publishNotes(notes) {
  if (!Array.isArray(notes) || !notes.length) return "";
  const shown = notes.slice(0, 3).map((n) => `<div class="ppub-note">
    <span class="ppub-note-page">p. ${esc(n.page || "?")}</span>
    ${n.kind ? `<span class="ppub-note-kind">${esc(n.kind)}</span>` : ""}
    ${n.quote ? `<p class="ppub-note-quote">“${esc(n.quote)}”</p>` : ""}
    ${n.body ? `<p>${esc(n.body)}</p>` : ""}</div>`).join("");
  const more = notes.length > 3
    ? `<p class="ppub-note-more">…and ${notes.length - 3} more in the reader.</p>` : "";
  return `<h2 class="ppub-section-head">Annotations · ${notes.length}</h2>${shown}${more}`;
}

function publishAvailability(v) {
  const assets = v.assets && typeof v.assets === "object" ? v.assets : {};
  const items = [];
  if (assets.about) items.push("About article");
  if (+assets.pages) items.push(`Full text · ${+assets.pages} page${+assets.pages === 1 ? "" : "s"}`);
  if (assets.translations && typeof assets.translations === "object") {
    const langs = Object.keys(assets.translations);
    if (langs.length) items.push(`Translations · ${langs.join(", ")}`);
  }
  if (+assets.notes) items.push(`${+assets.notes} annotation${+assets.notes === 1 ? "" : "s"}`);
  return `<div class="ppub-avail"><span class="ppub-label">Also available</span>${
    items.length ? items.map((x) => `<div>› ${esc(x)}</div>`).join("")
      : `<div class="ppub-faint">The scan only, so far.</div>`}</div>`;
}

function publishActions(v) {
  const pdf = publishSafeUrl(v.pdf_url, false);
  const base = publishSafeUrl(state.publishSiteUrl, false).replace(/\/$/, "");
  const read = base && publishSlug(v)
    ? `${base}/read.html?slug=${encodeURIComponent(publishSlug(v))}` : "";
  if (!pdf) return `<span class="ppub-btn disabled">Read online</span>
    <span class="ppub-btn disabled">PDF pending</span>`;
  const readButton = read
    ? `<a class="ppub-btn primary" href="${esc(read)}" target="_blank" rel="noopener">Read online</a>`
    : `<span class="ppub-btn disabled">Read online</span>`;
  return `${readButton}
    <a class="ppub-btn" href="${esc(pdf)}" target="_blank" rel="noopener">Download PDF</a>`;
}

function publishMasthead(body) {
  return `<div class="ppub-masthead"><img src="/static/icon.png" width="40" height="40" alt="">
    <div><div class="ppub-wordmark">World Herb Library</div>
      <div class="ppub-sub">An archival collection of botanical &amp; medical works</div></div></div>
    <div class="ppub-shell">${body}</div>`;
}

function publishBookHtml(v, details) {
  const thumb = publishThumb(v);
  const cats = publishCategoryHtml(v, false);
  const assets = v.assets && typeof v.assets === "object" ? v.assets : {};
  const aboutHead = details && /^\s*#{1,6}\s/.test(details.about || "")
    ? "" : `<h2 class="ppub-section-head">About this volume</h2>`;
  const about = details && details.about ? `${aboutHead}
    <div class="ppub-prose">${publishMarkdown(details.about)}</div>` :
    (!details && assets.about ? `<p class="ppub-loading">Loading About article…</p>` : "");
  const fallback = details && details.source === "local" && details.warning
    ? `<div class="ppub-preview-warning" role="status" title="${esc(details.warning)}">Online article details are unavailable. Showing the last uploaded local copy.</div>`
    : "";
  return publishMasthead(`<p class="ppub-crumb">Catalogue › ${esc(v.title || "Untitled")}</p>
    <div class="ppub-record-page"><div class="ppub-book-main">
      <h1 class="ppub-book-title">${esc(v.title || "Untitled")}</h1>
      ${v.subtitle ? `<p class="ppub-book-subtitle">${esc(v.subtitle)}</p>` : ""}
      ${v.authors ? `<p class="ppub-book-author">${esc(v.authors)}</p>` : ""}
      <p class="ppub-imprint">${publishImprint(v)}</p>
      ${cats ? `<div class="ppub-cats">${cats}</div>` : ""}
      ${fallback}${about}${publishNotes(details && details.notes)}
    </div><aside class="ppub-book-side">
      ${thumb ? `<img class="ppub-thumb" src="${esc(thumb)}" alt="">` : ""}
      ${publishMetadata(v)}
      <div class="ppub-actions">${publishActions(v)}</div>
      ${publishAvailability(v)}
    </aside></div>`);
}

function publishPlainDescription(value) {
  return String(value || "").replace(/[*_`>#]+/g, "").replace(/\s+/g, " ").trim();
}

function publishCatalogRow(v) {
  const thumb = publishThumb(v), desc = publishPlainDescription(v.description);
  const cats = publishCategoryHtml(v, false);
  return `<li class="ppub-catalog-row${thumb ? " has-thumb" : ""}">
    ${thumb ? `<img src="${esc(thumb)}" alt="" loading="lazy">` : ""}
    <div><button class="ppub-row-title" type="button" data-publish-book="${esc(publishSlug(v))}">${esc(v.title || "Untitled")}</button>
      ${v.subtitle ? `<div class="ppub-row-author">${esc(v.subtitle)}</div>` : ""}
      ${v.authors ? `<div class="ppub-row-author">${esc(v.authors)}</div>` : ""}
      <div class="ppub-row-imprint">${publishImprint(v)}</div>
      ${cats ? `<div class="ppub-row-cats">${cats}</div>` : ""}
      ${desc ? `<p>${esc(desc)}</p>` : ""}
      <button class="ppub-btn" type="button" data-publish-book="${esc(publishSlug(v))}">Preview volume</button>
    </div></li>`;
}

function publishSetHtml(entity) {
  return publishMasthead(`<p class="ppub-crumb">Catalogue › Book set</p>
    <div class="ppub-set-head"><span class="ppub-label">Book set</span>
      <h1 class="ppub-book-title">${esc(entity.label)}</h1>
      <p>${entity.entries.length} published volume${entity.entries.length === 1 ? "" : "s"}</p></div>
    <ol class="ppub-catalog-list">${entity.entries.map(publishCatalogRow).join("")}</ol>`);
}

async function renderPublishPreview() {
  const entity = publishFindEntity(state.publishSel);
  el("publish-empty").hidden = !!entity;
  el("publish-record").hidden = !entity;
  el("publish-open-live").hidden = !entity || entity.kind !== "book" ||
    !publishSafeUrl(state.publishSiteUrl, false);
  if (!entity) { el("publish-record").innerHTML = ""; return; }
  if (entity.kind === "set") {
    el("publish-preview-label").textContent = `${entity.label} · set preview`;
    el("publish-record").innerHTML = publishSetHtml(entity);
    return;
  }
  const v = entity.entries[0], slug = publishSlug(v), mine = ++state.publishPreviewSeq;
  el("publish-preview-label").textContent = `${v.title || "Untitled"} · catalog preview`;
  const cached = state.publishDetails.get(slug);
  el("publish-record").innerHTML = publishBookHtml(v, cached || null);
  if (cached) return;
  try {
    const res = await fetch("/api/publish/preview/" + encodeURIComponent(slug));
    const data = await res.json().catch(() => ({}));
    if (!res.ok || !data.ok) throw new Error(data.error || `HTTP ${res.status}`);
    state.publishDetails.set(slug, data);
    if (mine === state.publishPreviewSeq && state.publishSel === entity.key)
      el("publish-record").innerHTML = publishBookHtml(v, data);
  } catch (e) {
    if (mine === state.publishPreviewSeq) statusErr("PUBLISH PREVIEW :: " + e.message);
  }
}

function setPublishSidebarCollapsed(on, persist) {
  const collapsed = !!on;
  el("publish-layout").classList.toggle("sidebar-collapsed", collapsed);
  el("publish-side-toggle").textContent = collapsed ? "▶" : "◀";
  el("publish-side-toggle").setAttribute("aria-expanded", String(!collapsed));
  el("publish-side-toggle").dataset.tip = collapsed
    ? "Expand published books sidebar" : "Collapse published books sidebar";
  if (persist) {
    state.settings.publishSidebarCollapsed = collapsed;
    saveSettings();
  }
}

function loadPublishCatalog() {
  if (state.publishCatalogLoading) return state.publishCatalogLoading;
  const mine = ++state.publishCatalogSeq;
  const refresh = el("publish-refresh");
  refresh.disabled = true;
  el("publish-source").textContent = state.publishEntries.length ? "Refreshing…" : "Loading…";
  state.publishCatalogLoading = (async () => {
    try {
      const res = await fetch("/api/publish/catalog");
      const data = await res.json().catch(() => ({}));
      if (!res.ok || !data.ok) throw new Error(data.error || `HTTP ${res.status}`);
      if (mine !== state.publishCatalogSeq) return;
      state.publishEntries = Array.isArray(data.entries) ? data.entries : [];
      state.publishSource = data.source || "";
      state.publishWarning = data.warning || "";
      state.publishSiteUrl = String(data.site_url || "");
      state.publishDetails.clear();
      state.publishLoaded = true;
      el("publish-source").textContent = state.publishWarning ||
        (state.publishSource === "cloud" ? "Online catalogue" : "Local uploads");
      el("publish-source").title = state.publishWarning;
      renderPublishTree(true);
      renderPublishPreview();
    } catch (e) {
      if (mine !== state.publishCatalogSeq) return;
      el("publish-source").textContent = "Could not load catalogue";
      el("publish-tree-empty").textContent = e.message;
      el("publish-tree-empty").hidden = state.publishEntries.length > 0;
      statusErr("PUBLISH CATALOGUE :: " + e.message);
    } finally {
      if (mine === state.publishCatalogSeq) state.publishCatalogLoading = null;
      refresh.disabled = false;
    }
  })();
  return state.publishCatalogLoading;
}

function renderPublish() {
  el("publish-group").value = state.settings.publishGroup;
  setPublishSidebarCollapsed(state.settings.publishSidebarCollapsed, false);
  if (state.publishLoaded) {
    renderPublishTree(true);
    renderPublishPreview();
  } else loadPublishCatalog();
}

function closeCategories() { el("cat-overlay").hidden = true; }

// usage counts: how many records point at each node (builds + manual + checked)
function catUsage() {
  const n = {};
  const bump = (ids) => (ids || []).forEach((i) => { n[i] = (n[i] || 0) + 1; });
  for (const b of Object.values(state.builds || {})) bump(b.category_ids);
  for (const e of state.manual || []) bump(e.category_ids);
  for (const [, entry] of state.checked) bump((entry.book || {}).category_ids);
  return n;
}

function renderCatTree() {
  const tree = el("cat-tree");
  const nodes = state.taxonomy;
  const use = catUsage();
  const kids = {};
  for (const [id, node] of Object.entries(nodes)) {
    const p = nodes[node.parent] ? node.parent : "";
    (kids[p] = kids[p] || []).push(id);
  }
  for (const arr of Object.values(kids)) {
    arr.sort((a, b) => (nodes[a].name || "").localeCompare(nodes[b].name || ""));
  }
  const rows = [];
  const walk = (parent, depth, seen) => {
    for (const id of kids[parent] || []) {
      if (seen.has(id)) continue;   // cycle guard
      seen.add(id);
      rows.push({ id, depth });
      walk(id, depth + 1, seen);
    }
  };
  walk("", 0, new Set());
  el("cat-count").textContent = `${rows.length} categories`;
  if (!rows.length) {
    tree.innerHTML = `<p class="pane-note">No categories yet. Add a root, or adopt
      the legacy text fields.</p>`;
    return;
  }
  tree.innerHTML = rows.map(({ id, depth }) => {
    const count = use[id] || 0;
    return `<div class="cat-row${catMergeFrom === id ? " merge-src" : ""}" ` +
      `draggable="true" data-id="${esc(id)}" style="padding-left:${8 + depth * 18}px">` +
      `<span class="cat-name" data-tip="Click to rename">${esc(nodes[id].name || "?")}</span>` +
      `<span class="cat-use">${count ? count : ""}</span>` +
      `<span class="cat-acts">` +
      `<button type="button" class="cad-btn tiny" data-act="child" data-tip="Add a subcategory">+</button>` +
      `<button type="button" class="cad-btn tiny" data-act="merge" data-tip="Merge into another category">&#8646;</button>` +
      `<button type="button" class="cad-btn tiny danger" data-act="del" data-tip="Delete (children move up)">&#10005;</button>` +
      `</span></div>`;
  }).join("") +
  `<div class="cat-root-drop" data-rootdrop="1">drop here for top level</div>`;
}

async function catApi(method, path, body) {
  try {
    const res = await fetch(path, {
      method,
      headers: { "Content-Type": "application/json" },
      body: body ? JSON.stringify(body) : undefined,
    });
    const data = await res.json().catch(() => ({}));
    if (res.ok && data.ok) return data;
    el("cat-msg").textContent = data.error || "failed";
  } catch (e) { el("cat-msg").textContent = "request failed"; }
  return null;
}

async function catAfterChange() {
  await loadTaxonomy();
  renderCatTree();
  renderChecked();               // table cells + tooltips resolve names
  renderBuildEditor();
}

function catInlineRename(rowEl, id) {
  const nameEl = rowEl.querySelector(".cat-name");
  const old = (state.taxonomy[id] || {}).name || "";
  nameEl.innerHTML = `<input class="cell-edit" value="${esc(old)}" />`;
  const input = nameEl.querySelector("input");
  input.focus();
  input.select();
  let done = false;
  const finish = async (commit) => {
    if (done) return;
    done = true;
    const v = input.value.trim();
    if (commit && v && v !== old) {
      if (await catApi("PATCH", `/api/categories/${encodeURIComponent(id)}`, { name: v })) {
        await catAfterChange();
        return;
      }
    }
    renderCatTree();
  };
  input.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") { ev.preventDefault(); finish(true); }
    else if (ev.key === "Escape") { ev.stopPropagation(); finish(false); }
  });
  input.addEventListener("blur", () => finish(true));
}

function initCategories() {
  makeCatPicker("m-categories");
  makeCatPicker("e-categories");
  makeCatPicker("b-categories");
  MENU_CMDS["categories"] = () => openCategories();
  el("cat-close").addEventListener("click", closeCategories);
  el("cat-overlay").addEventListener("mousedown", (ev) => {
    if (ev.target === el("cat-overlay")) closeCategories();
  });

  // window.prompt is unavailable in the Electron shell, so "add" flows
  // through an inline input in the window's toolbar
  let catNewParent = null;
  const newInput = el("cat-new-name");
  const askNewCat = (parent) => {
    catNewParent = parent;
    newInput.placeholder = parent
      ? `new subcategory of ${(state.taxonomy[parent] || {}).name || "?"}…`
      : "new top-level category…";
    newInput.hidden = false;
    newInput.value = "";
    newInput.focus();
  };
  newInput.addEventListener("keydown", async (ev) => {
    if (ev.key === "Escape") {
      ev.stopPropagation();
      newInput.hidden = true;
      return;
    }
    if (ev.key !== "Enter") return;
    ev.preventDefault();
    const name = newInput.value.trim();
    if (!name) { newInput.hidden = true; return; }
    if (await catApi("POST", "/api/categories",
                     { name, parent: catNewParent || "" })) {
      newInput.hidden = true;
      await catAfterChange();
    }
  });
  newInput.addEventListener("blur", () => { newInput.hidden = true; });

  el("cat-add-root").addEventListener("click", () => askNewCat(""));

  // adopt is destructive-ish (writes every store), so it runs dry first and
  // asks for a second click with the numbers on the table
  el("cat-adopt").addEventListener("click", async () => {
    if (!catAdoptPreview) {
      const data = await catApi("POST", "/api/categories/adopt", { dry_run: true });
      if (!data) return;
      if (!data.records) {
        el("cat-msg").textContent = "nothing to adopt — no legacy text without categories";
        return;
      }
      catAdoptPreview = data;
      el("cat-msg").textContent =
        `${data.records} records, ${(data.new || []).length} new categories — click again to apply`;
      return;
    }
    const data = await catApi("POST", "/api/categories/adopt", {});
    catAdoptPreview = null;
    if (data) {
      el("cat-msg").textContent =
        `adopted: ${data.records} records, ${data.created} new categories`;
      status(`CATEGORIES :: ADOPTED ${data.records} RECORDS`);
      await catAfterChange();
    }
  });

  el("cat-tree").addEventListener("click", async (ev) => {
    const rowEl = ev.target.closest(".cat-row");
    if (!rowEl) return;
    const id = rowEl.dataset.id;
    const act = (ev.target.closest("[data-act]") || {}).dataset;
    if (act && act.act === "child") {
      askNewCat(id);
      return;
    }
    if (act && act.act === "merge") {
      if (catMergeFrom === id) {
        catMergeFrom = null;
        el("cat-msg").textContent = "";
      } else if (catMergeFrom) {
        const from = catMergeFrom;
        catMergeFrom = null;
        const data = await catApi("POST", "/api/categories/merge", { from, into: id });
        if (data) {
          el("cat-msg").textContent = `merged — ${data.reassigned} records moved`;
          await catAfterChange();
          return;
        }
      } else {
        catMergeFrom = id;
        el("cat-msg").textContent =
          `merging "${(state.taxonomy[id] || {}).name}" — click ⇄ on the target`;
      }
      renderCatTree();
      return;
    }
    if (act && act.act === "del") {
      const n = (state.taxonomy[id] || {}).name || "?";
      if (!(await confirmDialog({
        title: "Delete category",
        message: `Delete "${n}"?`,
        detail: "Its subcategories move up a level; record assignments drop it.",
        confirmLabel: "Delete category",
        danger: true,
      }))) return;
      if (await catApi("DELETE", `/api/categories/${encodeURIComponent(id)}`)) {
        await catAfterChange();
      }
      return;
    }
    if (ev.target.closest(".cat-name")) catInlineRename(rowEl, id);
  });

  // drag a row onto another row (or the root strip) to re-parent
  el("cat-tree").addEventListener("dragstart", (ev) => {
    const rowEl = ev.target.closest(".cat-row");
    if (!rowEl) return;
    ev.dataTransfer.setData("text/whl-cat", rowEl.dataset.id);
    ev.dataTransfer.effectAllowed = "move";
  });
  el("cat-tree").addEventListener("dragover", (ev) => {
    if (ev.dataTransfer.types.includes("text/whl-cat")) {
      ev.preventDefault();
      const over = ev.target.closest(".cat-row, .cat-root-drop");
      el("cat-tree").querySelectorAll(".drop-target").forEach((x) =>
        x.classList.remove("drop-target"));
      if (over) over.classList.add("drop-target");
    }
  });
  el("cat-tree").addEventListener("drop", async (ev) => {
    const id = ev.dataTransfer.getData("text/whl-cat");
    if (!id) return;
    ev.preventDefault();
    el("cat-tree").querySelectorAll(".drop-target").forEach((x) =>
      x.classList.remove("drop-target"));
    const over = ev.target.closest(".cat-row, .cat-root-drop");
    if (!over) return;
    const parent = over.dataset.rootdrop ? "" : over.dataset.id;
    if (parent === id) return;
    if (await catApi("PATCH", `/api/categories/${encodeURIComponent(id)}`,
                     { parent })) {
      await catAfterChange();
    }
  });
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
  for (const t of document.querySelectorAll("#manual-pane .pane-tab[data-ptab]")) {
    t.addEventListener("click", () => switchPaneTab(t.dataset.ptab));
  }
}

function clearSearchForm() {
  for (const f of ["title", "author", "publisher", "city", "year",
                   "edition", "volume"]) {
    el("s-" + f).value = "";
  }
  el("ol-msg").textContent = "";
  // a row-selection override would keep feeding the old query — clearing
  // means clearing
  state.olOverride = null;
  if (activeBottomTable() === "ol") olRealtime();
}

function switchPaneTab(id) {
  document.querySelectorAll("#manual-pane .pane-tab[data-ptab]").forEach((t) =>
    t.classList.toggle("active", t.dataset.ptab === id));
  document.querySelectorAll("#manual-pane .pane-sub").forEach((p) =>
    p.classList.toggle("active", p.id === id));
  if (id === "pane-info") renderInfoPane();
}

// --- Advanced Search popup (the form lives outside the left pane now) ---------
function openAdvSearch() {
  const pop = el("adv-search-pop"), r = el("adv-search-btn").getBoundingClientRect();
  pop.style.top = (r.bottom + 4) + "px";
  pop.style.left = r.left + "px";
  pop.hidden = false;
  el("s-title").focus();
}
function closeAdvSearch() {
  el("adv-search-pop").hidden = true;
  // dropping the advanced fields stops them constraining the bottom pane once
  // the popup is dismissed; a row-selection override (state.olOverride) is left
  // intact — use the popup's Clear button to reset that too.
  let had = false;
  for (const f of ["title", "author", "publisher", "city", "year", "edition", "volume"]) {
    if (el("s-" + f).value) { el("s-" + f).value = ""; had = true; }
  }
  el("ol-msg").textContent = "";
  if (had && activeBottomTable() === "ol") olRealtime();
}
function toggleAdvSearch() {
  if (el("adv-search-pop").hidden) openAdvSearch(); else closeAdvSearch();
}

// --- Info tab: a read-only inspector that follows the selected / open row ----
function currentInfoTarget() {
  const et = state.editTarget;
  if (et) {
    if (et.kind === "row") {
      const r = state.rowsById.get(String(et.id));
      if (r) return { label: r.kind === "manual" ? "Manual entry" : "Checked book",
                      book: r.book, row: r };
    } else if (et.kind === "ch") {
      const b = (state.chBooks || []).find((x) => x.idx === et.idx);
      if (b) {
        const ex = state.checked.get(ckey("ch_library", et.idx));
        return { label: "Master list #" + et.idx,
                 book: ex ? ex.book : parseBook(b), row: ex || null };
      }
    } else if (et.kind === "whl") {
      const w = whlRowByIdx(et.idx);
      if (w) return { label: "WHL catalog", whl: w };
    } else if (et.kind === "set") {
      const vols = setMembers(et.key);
      if (vols.length) return { label: "Volume set", set: { key: et.key, vols } };
    }
  }
  if (state.checkedSelected != null) {
    const r = state.rowsById.get(String(state.checkedSelected));
    if (r) return { label: r.kind === "manual" ? "Manual entry" : "Checked book",
                    book: r.book, row: r };
  }
  if (state.whlSelected != null) {
    const w = whlRowByIdx(state.whlSelected);
    if (w) return { label: "WHL catalog", whl: w };
  }
  return null;
}

function infoRow(label, valueHtml, cls) {
  if (valueHtml == null || valueHtml === "") return "";
  return `<div class="info-row ${cls || ""}"><span class="info-k">${esc(label)}</span>` +
    `<span class="info-v">${valueHtml}</span></div>`;
}

function infoExtraValue(value) {
  if (value == null) return "";
  if (typeof value === "object") {
    try { return JSON.stringify(value, null, 2); }
    catch (e) { return String(value); }
  }
  return String(value);
}

// The records behind the copyright tag, spelled out: the registration (whether
// found in a register or merely cited by the renewal), and the renewal itself.
function infoCopyright(b, status) {
  const key = { title: b.title, author: b.author, year: b.year };
  if (status === undefined) status = crStatusFor(key);
  const sec = (rows) => `<div class="info-sec"><div class="info-sec-h">Copyright</div>${rows}</div>`;
  if (status === undefined) return sec(infoRow("Status", "Checking …"));
  if (!status) return "";

  const e = crEvidence(key, status);
  let h = infoRow("Status", esc(status));

  const rec = e.record;
  if (rec) {
    h += infoRow("Registration",
      esc([rec.number || "(unnumbered)", rec.date].filter(Boolean).join(" · ")));
    if (rec.via) h += infoRow("Source", esc(rec.via), "info-sub");
    if (rec.title)
      h += infoRow("Registered as", esc(rec.title + (rec.author ? " — " + rec.author : "")), "info-sub");
  } else if (e.regPending || (e.renewedId && e.renPending)) {
    h += infoRow("Registration", "Checking …");
  } else if (!copyrightSources().length) {
    h += infoRow("Registration", "Lookup disabled");
  } else {
    h += infoRow("Registration", "No record found");
  }

  if (e.renewedId) {
    const r = e.renewal;
    h += infoRow("Renewal", esc([e.renewedId,
      r && (crDate(r.renewal_date) || r.renewal_year)].filter(Boolean).join(" · ")) +
      (!r && e.renPending ? " <span class='info-sub'>checking …</span>" : ""));
  } else if (e.notRen) {
    h += infoRow("Renewal", "No renewal record found");
  }
  return sec(h);
}

function infoStatusRow(label, st) {
  const dot = st.cls ? `<span class="info-dot ${st.cls}"></span>` : "";
  const val = st.url
    ? `<a href="${esc(st.url)}" target="_blank" rel="noopener">${esc(st.text)}</a>`
    : esc(st.text);
  return infoRow(label, dot + val);
}

function infoWhlStatus(row) {
  const c = row && row.checks;
  if (!c || c.error) return { text: "Not checked", cls: "" };
  const m = c.whl_match || {};
  const suf = m.title ? " — " + m.title : "";
  if (c.in_whl === "yes") return { text: "In WHL" + suf, cls: "ok", url: m.permalink };
  if (c.in_whl === "draft") return { text: "Draft in WHL" + suf, cls: "warn", url: m.permalink };
  if (c.in_whl === "no") return { text: "Not in WHL", cls: "" };
  return { text: "Unknown", cls: "" };
}

function infoScanStatus(row, source) {
  const s = row && row.scans && row.scans[source];
  if (!s) return { text: "Not searched", cls: "" };
  if (s.error) return { text: "Error", cls: "err" };
  if (s.available === true) {
    const best = s.best_match || {};
    return { text: s.full_view ? "Full view available" : "Available", cls: "ok",
             url: best.url || best.record_url || "" };
  }
  if (s.no_download) return { text: "Borrow/lending only", cls: "warn", url: s.search_url || "" };
  if (s.available === false) return { text: "Not found", cls: "" };
  return { text: "Unknown", cls: "" };
}

function renderInfoPane() {
  const body = el("info-body");
  if (!body) return;
  const t = currentInfoTarget();
  if (!t) {
    body.innerHTML = `<div class="info-empty">Select a row to see its details.</div>`;
    return;
  }
  if (t.whl) {                                        // a WHL catalogue row
    const w = t.whl;
    let h = `<div class="info-head">${esc(t.label)}</div><div class="info-sec">`;
    h += infoRow("Title", esc(w.title));
    h += infoRow("Authors", esc(w.authors));
    h += infoRow("Year", esc(w.year));
    h += infoRow("Publisher", esc(w.publisher));
    h += infoRow("Pages", esc(w.pages));
    h += infoRow("Language", esc(w.language));
    h += infoRow("Subject", esc(w.subject));
    h += infoRow("Status", esc(w.status));
    const wurl = w.permalink || w.url;   // WHL rows carry the page under permalink
    if (wurl) h += infoRow("URL",
      `<a href="${esc(wurl)}" target="_blank" rel="noopener">${esc(wurl)}</a>`);
    body.innerHTML = h + `</div>`;
    return;
  }
  if (t.set) {                                        // a multi-volume set
    const vols = t.set.vols;
    let h = `<div class="info-head">${esc(t.label)} :: ${esc(setBaseTitle(vols[0].book))}</div>`;
    h += `<div class="info-sec">`;
    h += infoRow("Volumes present", esc(String(vols.length)));
    h += infoRow("Declared count", esc(String(setDefinedCount(t.set.key) || "?")));
    h += `</div><div class="info-sec"><div class="info-sec-h">Volumes</div>`;
    for (const v of vols) h += infoRow("Vol " + (volNum(v.book) || "?"), esc(v.book.title));
    body.innerHTML = h + `</div>`;
    return;
  }
  const b = t.book || {};                             // a checked / manual / master book
  const row = t.row;
  let h = `<div class="info-head">${esc(t.label)}</div>`;
  h += `<div class="info-sec"><div class="info-sec-h">Fields</div>`;
  for (const [k, label] of TIP_FIELDS) {
    if (k === "url" || k === "status") continue;      // not book-owned in this table
    const v = (b[k] == null ? "" : String(b[k])).trim();
    if (v) h += infoRow(label, esc(v));
  }
  h += `</div>`;
  // non-column metadata captured with the entry (phone captures etc.)
  const extra = b.extra || {};
  if (Object.keys(extra).length) {
    h += `<div class="info-sec"><div class="info-sec-h">Extra</div>`;
    for (const k of Object.keys(extra).sort())
      h += infoRow(k.replace(/_/g, " "), esc(infoExtraValue(extra[k])));
    h += `</div>`;
  }
  // associated photos: thumbnails, click for full size
  const imgs = b.images || [];
  if (imgs.length) {
    h += `<div class="info-sec"><div class="info-sec-h">Photos</div><div class="info-imgs">`;
    const capTitle = (b.title || "").trim() || "Entry";
    imgs.forEach((p, i) => {
      const url = "/api/capture/image?path=" + encodeURIComponent(p);
      const cap = `${capTitle} — photo ${i + 1} of ${imgs.length}`;
      h += `<img class="info-thumb" loading="lazy" src="${esc(url)}" tabindex="0" ` +
        `data-lightbox="${esc(url)}" data-cap="${esc(cap)}" alt="${esc(cap)}">`;
    });
    h += `</div></div>`;
  }
  if (row) {                                          // derived status needs a checked row
    h += `<div class="info-sec"><div class="info-sec-h">Status</div>`;
    h += infoStatusRow("WHL", infoWhlStatus(row));
    h += infoStatusRow("Internet Archive", infoScanStatus(row, "internet_archive"));
    h += infoStatusRow("HathiTrust", infoScanStatus(row, "hathitrust"));
    const dl = dlState(row);
    const dlText = { done: "Downloaded", downloading: "Downloading…", failed: "Download failed" }[dl];
    if (dlText) h += infoStatusRow("Download",
      { text: dlText, cls: dl === "done" ? "ok" : dl === "failed" ? "err" : "warn" });
    const lpdf = row.localPdf || row.local_pdf;   // raw state.checked uses snake_case
    if (lpdf) h += infoRow("Local scan", esc(lpdf));
    h += `</div>`;
  }
  // the copyright + renewal records behind the tag (works for any book, not
  // just checked rows: the offline status is fetched on demand)
  h += infoCopyright(b, row && row.checks ? row.checks.copyright_status : undefined);
  const sk = groupIdOf(b), cnt = sk ? setDefinedCount(sk) : 0, vn = volNum(b);
  if (cnt > 0 || vn > 0) {
    h += `<div class="info-sec"><div class="info-sec-h">Volume set</div>`;
    h += infoRow("This volume", vn ? esc("Volume " + vn) : "—");
    h += infoRow("Set size", cnt ? esc(cnt + " volumes") : "?");
    h += `</div>`;
  }
  body.innerHTML = h;
}

function refreshInfoIfActive() {
  if (document.querySelector("#pane-info.active")) renderInfoPane();
}

async function loadOlStatus() {
  try {
    const st = await (await fetch("/api/ol/status")).json();
    const ed = st.editions || {};
    el("status-right").textContent = ed.available
      ? `OL INDEX: ${(ed.editions / 1e6).toFixed(1)}M EDITIONS`
      : (st.available ? `OL WORKS INDEX: ${(st.works / 1e6).toFixed(1)}M` : "No OL index");
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
  fillAuto("title", titleCase(r.title) +
    (r.subtitle ? ": " + titleCase(r.subtitle) : ""));
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
  setSearchPane(true);
  const tabs = bottomTabs();
  let i = tabs.indexOf("ol");
  if (i < 0) { tabs.push("ol"); i = tabs.length - 1; }
  state.settings.bottomActive = i;
  saveSettings();
  await renderBottomPane();
  await olRealtime();
  el("ol-msg").textContent =
    `${(state.olRows || []).length} results in the Open Library table below`;
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
  let body = {};
  for (const f of MANUAL_FIELDS) body[f] = el("m-" + f).value;
  if (!body.title.trim()) { el("manual-msg").textContent = "Title is required"; return; }
  body = parseBook(body);   // colon subtitle + volume / edition indicators
  body.category_ids = catPickers["m-categories"].get();

  const btn = el("manual-submit");
  btn.disabled = true;
  el("manual-msg").textContent = "Checking ...";
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
      catPickers["m-categories"].set([]);   // pickers sit outside form.reset()
      clearProv();
      hideOlSuggest();
      el("m-title").focus();
      el("manual-msg").textContent = "Saved";
      status(manualStatusLine(data.entry));
      queueScan(data.entry.id);
    } else {
      el("manual-msg").textContent = data.error || "Save failed";
    }
  } catch (e) {
    el("manual-msg").textContent = "Save failed";
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

// --- phone-capture cloud sync (Supabase) ---------------------------------------
// The server pulls pending captures, runs the photo pipeline, and files them as
// manual entries; this triggers a run and refreshes the table when it finishes.
let _cloudPoll = null;

async function runCloudSync() {
  const btn = el("cloud-sync-btn");
  try {
    await flushClientState();   // the engine reads settings server-side — push first
    const r = await (await fetch("/api/cloudsync/run", { method: "POST" })).json();
    if (!r.ok) { statusErr("CLOUD SYNC :: " + (r.error || "failed to start")); return; }
  } catch (e) { statusCrit("CLOUD SYNC :: server unreachable"); return; }
  btn.disabled = true;
  status("CLOUD SYNC :: RUNNING");
  clearInterval(_cloudPoll);
  _cloudPoll = setInterval(async () => {
    let st = null;
    try { st = await (await fetch("/api/cloudsync/status")).json(); }
    catch (e) { return; }                      // transient; keep polling
    if (st.running) return;
    clearInterval(_cloudPoll);
    _cloudPoll = null;
    btn.disabled = false;
    const r = st.last_result || {};
    if (r.imported) await loadManual();        // new entries -> refresh the table
    // stores that pulled records changed local files -> refresh their views
    const stores = r.stores || {};
    const sum = (k) => Object.values(stores).reduce((n, s) => n + (s[k] || 0), 0);
    const up = sum("pushed") + sum("tombstoned");
    const down = sum("pulled") + sum("deleted");
    if (((stores.builds || {}).pulled || 0) + ((stores.builds || {}).deleted || 0)) {
      await loadBuilds();
      renderBuildsList();
    }
    if (((stores.corrections || {}).pulled || 0) + ((stores.corrections || {}).deleted || 0)) {
      await loadWhlRows(true);
      renderWhlTop();
    }
    if (r.ok === false) {
      statusCrit("CLOUD SYNC FAILED :: " +
        (r.error || (r.errors || []).join("; ") || "?"));
    } else if (r.owner_sync) {
      // a sync that finished but dropped rows is an error, not a success
      const dropped = (r.errors || []).length;
      const en = r.entries || {};
      status(`CLOUD SYNC :: ${r.imported || 0} imported / ${r.books_pushed || 0} books pushed` +
        ` / stores ${up} up ${down} down` +
        (en.pushed || en.pulled ? ` / files ${en.pushed || 0} up ${en.pulled || 0} down` : "") +
        (dropped ? ` / ${dropped} errors` : ""), dropped ? "error" : undefined);
    } else {
      status(`PHONE SYNC :: ${r.imported || 0} imported / ${r.skipped || 0} already present`);
    }
  }, 1500);
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
    () => restoreManualEntry(snap),
    { kind: "manual-create", id: snap.id });
}

async function deleteManual(id) {
  // no confirmation: deletion is undoable
  const e = state.manual.find((x) => x.id === id);
  const snap = e ? JSON.parse(JSON.stringify(e)) : null;
  if (await deleteManualById(id)) {
    if (snap) {
      pushOp(`delete entry ${String(snap.title || "").slice(0, 36)}`,
        () => restoreManualEntry(snap),
        () => deleteManualById(snap.id),
        { kind: "manual-restore", snap });
    }
    status("MANUAL ENTRY DELETED");
  } else {
    statusCrit("DELETE FAILED");
  }
}

// --- checked-tab batch actions ----------------------------------------------------

function exportJson() {
  // the export contains exactly what the table shows: the FIND box and the
  // filter menu both apply
  const rows = filteredCheckedRows();
  const payload = rows.map((r) => ({
    source: r.kind === "manual" ? "manual_entries" : r.source,
    metadata: r.book,
    checks: r.checks || null,
    scans: r.scans || null,
    mark: rowMarkState(r),
    verify: r.verify || {},
  }));
  if (!payload.length) { status("NOTHING TO EXPORT (check the filters)"); return; }
  const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "whl_checked_books.json";
  a.click();
  URL.revokeObjectURL(a.href);
  status(`EXPORTED ${payload.length} RECORDS` +
    (filtersActive() || state.checkedFilter ? " (FILTERED)" : ""));
}

function setSearchPane(on) {
  state.settings.showCatalog = !!on;
  saveSettings();
  renderBottomPane();
}

// --- upload list: approved sources + the book builder ------------------------------

const ARCHIVE_NAMES = { internet_archive: "Internet Archive", hathitrust: "HathiTrust" };

function capturedSourceMeta(row) {
  const book = (row && row.book) || {};
  return {
    extra: book.extra && typeof book.extra === "object" ? book.extra : {},
    images: Array.isArray(book.images) ? book.images.slice() : [],
    capture_id: String(book.capture_id || ""),
  };
}

function approvedSources() {
  const out = [];
  for (const row of combinedRows()) {
    // a locally attached scan is a verified source in its own right
    if (row.localPdf) {
      out.push(Object.assign({
        title: row.book.title || "",
        subtitle: row.book.subtitle || "",
        author: row.book.author || "",
        publisher: row.book.publisher || "",
        year: row.book.year || "",
        category_ids: (row.book.category_ids || []).slice(),
        archive: "Local scan",
        url: "",
        matched_title: row.localPdf.split(/[\\/]/).pop() || row.localPdf,
        identifier: "",
        local_pdf: row.localPdf,
        _rowId: row.id,
      }, capturedSourceMeta(row)));
    }
    for (const source of ["internet_archive", "hathitrust"]) {
      const meta = Object.assign({
        title: row.book.title || "",
        subtitle: row.book.subtitle || "",
        author: row.book.author || "",
        publisher: row.book.publisher || "",
        year: row.book.year || "",
        category_ids: (row.book.category_ids || []).slice(),
        archive: ARCHIVE_NAMES[source],
      }, capturedSourceMeta(row));
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
  // list in the order the underlying books were added: manual entries
  // oldest-first (combinedRows yields them newest-first), then checked
  // catalog books in the order they were checked. Sources from one book
  // stay adjacent (stable sort preserves local-scan / IA / HT emission order).
  const rank = addedRankByRowId();
  out.sort((a, b) =>
    (rank.has(a._rowId) ? rank.get(a._rowId) : Infinity) -
    (rank.has(b._rowId) ? rank.get(b._rowId) : Infinity));
  return out;
}

// per-row "added" ordering: manual entries by created_at ascending, then
// checked catalog books in check (Map insertion) order
function addedRankByRowId() {
  const rank = new Map();
  let i = 0;
  const manualAsc = state.manual.slice().sort((a, b) =>
    (a.created_at || "").localeCompare(b.created_at || ""));
  for (const e of manualAsc) rank.set(e.id, i++);
  for (const k of state.checked.keys()) rank.set(k, i++);
  return rank;
}

function renderUpload() {
  renderBuildsList();
  renderBuildEditor();
  // snapshot: the BUILD buttons index into the list as rendered, so a
  // background state change can't misdirect a click
  const sources = approvedSources();
  state.uploadSources = sources;
  const tbody = el("upload-rows");
  tbody.innerHTML = "";
  el("sources-count").textContent = `${sources.length} rows`;
  el("upload-empty").hidden = sources.length !== 0;
  sources.forEach((s, i) => {
    const st = sourceBuildStatus(s);
    const flt = state.settings.srcStatusFilter || {};
    if (flt[st] === false) return;   // hidden by the status filter
    const tr = document.createElement("tr");
    tr.dataset.si = i;
    // yellow = a draft entry exists in the editor; green = its entry is done
    if (st === "draft") tr.classList.add("src-draft");
    else if (st === "done") tr.classList.add("src-done");
    const srcAttn = (state.attn || {})["src:" + (s.url || s.local_pdf || s.title)];
    if (srcAttn) {
      tr.classList.add("attention");
      if (attnReason(srcAttn)) tr.dataset.tip = "Needs attention: " + attnReason(srcAttn);
    }
    tr.innerHTML = `
      <td>${esc(s.title)}</td>
      <td>${esc(s.subtitle)}</td>
      <td>${esc(s.author)}</td>
      <td>${esc(s.publisher)}</td>
      <td>${esc(s.year)}</td>
      <td>${esc(s.archive)}</td>
      <td>${s.url
        ? `<a href="${esc(s.url)}" target="_blank" rel="noopener" data-tip="${esc(s.url)}">${esc(s.matched_title) || "(record)"}</a>`
        : esc(s.matched_title)}</td>
      <td class="col-whl">${st === "done" ? badge("approved", "DONE", { tip: "The entry built from this source is verified" })
          : st === "draft" ? badge("upload", BICONS.pencil, { tip: "An entry built from this source is in the editor" })
          : ""}</td>
      <td class="col-act"><button class="cad-btn tiny icon-btn" data-build-src="${i}"
        data-tip="Build a catalog entry prefilled from this source">${ICONS.docplus}</button></td>`;
    tbody.appendChild(tr);
  });
  // the count sits in the narrow book-list bar now: total only, detail in the tip
  const count = el("upload-count");
  count.textContent = `${Object.keys(state.builds).length} entries`;
  count.dataset.tip =
    `${Object.values(state.builds).filter((b) => b.status === "ready").length} verified / ` +
    `${Object.values(state.builds).filter((b) => b.status === "uploaded").length} published`;
  applyTableChrome("upload");
  renderWorkbench();   // head, chips and gates track every builds re-render
}

// which build (if any) was seeded from this verified source, and how far
// along it is: "" (unstarted) / "draft" / "done" (verified or uploaded)
function sourceBuildStatus(s) {
  const builds = Object.values(state.builds);
  const b = builds.find((x) =>
    (s.local_pdf && x.pdf_file === s.local_pdf) ||
    (s.url && x.source_url === s.url));
  if (!b) return "unstarted";
  return b.status === "ready" || b.status === "uploaded" ? "done" : "draft";
}

const SRC_STATUS_LABELS = [
  ["unstarted", "Unstarted"], ["draft", "Draft (in the editor)"],
  ["done", "Done (entry verified)"],
];

// the verified-sources filter: choose which statuses stay visible
function openSrcFilterMenu(anchor) {
  const flt = state.settings.srcStatusFilter =
    state.settings.srcStatusFilter || {};
  const html = `<div class="pm-head">Show sources</div>` +
    SRC_STATUS_LABELS.map(([k, label]) => `
      <label class="pm-item"><input type="checkbox" data-k="${k}"
        ${flt[k] === false ? "" : "checked"} /> ${label}</label>`).join("");
  openPopup(anchor, html, (pop) => {
    pop.querySelectorAll("input[data-k]").forEach((cb) => {
      cb.addEventListener("change", () => {
        if (cb.checked) delete flt[cb.dataset.k];
        else flt[cb.dataset.k] = false;
        saveSettings();
        syncSrcFilterBtn();
        renderUpload();
      });
    });
  });
}

function syncSrcFilterBtn() {
  const flt = state.settings.srcStatusFilter || {};
  el("src-filter").classList.toggle("active",
    Object.values(flt).some((v) => v === false));
}

function downloadUploadList() {
  const sources = approvedSources().map(({ _rowId, ...s }) => s);
  if (!sources.length) { status("NO VERIFIED SOURCES"); return; }
  const blob = new Blob([JSON.stringify(sources, null, 2)], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "whl_upload_list.json";
  a.click();
  URL.revokeObjectURL(a.href);
  status(`DOWNLOADED SOURCES LIST :: ${sources.length}`);
}

// --- the book builder --

let buildDescMd = null;      // live markdown editor in the ENTRY tab
let buildPdfViewer = null;   // PDF viewer in the SOURCE tab
const descState = { id: null, val: null };  // last value set into the editor
let buildDirty = false;      // the entry form has unsaved metadata edits
function buildIsDirty() {
  // The markdown editor exists before any entry is selected; its initial empty
  // value differs from descState.val=null, but that is not an unsaved draft.
  if (!currentBuild()) return false;
  return buildDirty ||
    (buildDescMd && descState.id === state.buildSel &&
     buildDescMd.get() !== descState.val);
}

async function loadBuilds() {
  try {
    const res = await fetch("/api/builds");
    state.builds = res.ok ? (await res.json()).builds || {} : {};
    // One-time migration for builds that already had a volume before explicit
    // group metadata was introduced. Persist the association, then render from
    // metadata on every subsequent load.
    for (const b of Object.values(state.builds)) {
      if (!volNum(b) || String(b.group_id || "").trim()) continue;
      const groupId = buildGroupIdFor(b);
      if (!groupId) continue;
      b.group_id = groupId;
      try {
        const patch = await fetch(`/api/builds/${encodeURIComponent(b.id)}`, {
          method: "PATCH", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ group_id: groupId }),
        });
        const data = await patch.json().catch(() => ({}));
        if (patch.ok && data.ok) state.builds[b.id] = data.build;
      } catch (e) { /* local display still uses the migrated association */ }
    }
  } catch (e) { state.builds = {}; }
}

// the sidebar shows one queue at a time: Pending (awaiting upload to WHL)
// or Uploaded (already sent)
function buildsTab() {
  return state.buildsTab === "uploaded" ? "uploaded" : "pending";
}

// every build, newest first — exports and the OCR tab's book list must not
// depend on which Editor sidebar tab happens to be active
function allBuildsSorted() {
  return Object.values(state.builds)
    .sort((a, b) => (b.updated_at || "").localeCompare(a.updated_at || ""));
}

function buildsSorted() {
  const uploaded = buildsTab() === "uploaded";
  return allBuildsSorted().filter((b) => (b.status === "uploaded") === uploaded);
}

function currentBuild() {
  return state.buildSel ? state.builds[state.buildSel] || null : null;
}

// legacy name — the Editor sidebar became the unified Workbench book list
function renderBuildsList() {
  renderOcrBooks();
}

function editorBuildItems(builds) {
  const allGroups = new Map();
  for (const b of Object.values(state.builds)) {
    const id = String(b.group_id || "").trim();
    if (!id) continue;
    if (!allGroups.has(id)) allGroups.set(id, []);
    allGroups.get(id).push(b);
  }
  const visibleGroups = new Map();
  for (const b of builds) {
    const id = String(b.group_id || "").trim();
    if (!id) continue;
    if (!visibleGroups.has(id)) visibleGroups.set(id, []);
    visibleGroups.get(id).push(b);
  }
  const out = [], emitted = new Set();
  for (const b of builds) {
    const id = String(b.group_id || "").trim();
    const members = id ? (visibleGroups.get(id) || []) : [];
    const total = id ? (allGroups.get(id) || []).length : 0;
    if (total < 2) {
      out.push({ type: "build", build: b });
      continue;
    }
    if (emitted.has(id)) continue;
    emitted.add(id);
    members.sort((a, z) => volNum(a) - volNum(z) ||
      (a.updated_at || "").localeCompare(z.updated_at || ""));
    out.push({ type: "group", id, members, total, expanded: setExpanded(id),
               title: setBaseTitle(members[0]) || id });
  }
  return out;
}

function appendBuildListItem(list, b, grouped) {
    const ready = b.status === "ready";
    const uploaded = b.status === "uploaded";
    const folder = (ocrState.books || {})[b.id];
    const li = document.createElement("li");
    li.className = "build-item" + (b.id === state.buildSel ? " active" : "") +
      (ready ? " ready" : "") + (b.attention ? " attention" : "") +
      (grouped ? " volume-member" : "");
    li.dataset.bid = b.id;
    li.dataset.tip = `${b.title || "(untitled)"}\n` +
      `${b.authors ? "Authors: " + b.authors + "\n" : ""}` +
      `${b.year ? "Year: " + b.year + "\n" : ""}` +
      `Status: ${uploaded ? "published" : ready ? "verified" : "draft"}\n` +
      (folder ? `${folder.ocr.length} OCR file(s)\n` : "") +
      `Updated: ${b.updated_at || ""}` +
      (attnReason(b.attention) ? `\nNeeds attention: ${attnReason(b.attention)}` : "") +
      "\nQ: mark as needing attention, with a reason";
    // compact: title with the status icon inline on the right, then a
    // single author · year · OCR-count meta line
    li.innerHTML = `
      <span class="bi-row">
        <span class="bi-title">${esc(b.title) || "<em>(untitled)</em>"}</span>
        <span class="bi-status ${ready || uploaded ? "ok" : ""}"
              data-tip="${uploaded ? "Published" : ready ? "Verified" : "Draft"}">${
                uploaded ? ICONS.export : ready ? ICONS.check : ICONS.pencil}</span>
      </span>
      <span class="bi-meta">${b.volume ? `Vol. ${esc(b.volume)} &middot; ` : ""}${
        esc(b.authors || "")}${b.authors && b.year ? " &middot; " : ""}${esc(b.year || "")}${
        folder && folder.ocr.length ? ` &middot; ${folder.ocr.length} OCR` : ""}</span>`;
    list.appendChild(li);
}

// Publish a verified entry to the Library Tool cloud: its PDF to object
// storage, its metadata to Supabase, where the website's library browser reads
// it. This used to be "Upload to WHL", which flipped a status field and sent
// nothing anywhere -- there was no WHL write API to call.
async function uploadBuild() {
  const b = currentBuild();
  if (!b) return;
  if (b.status !== "ready") {
    el("build-msg").textContent = "Only verified entries can be published";
    return;
  }
  if (!(b.pdf_file || "").trim()) {
    el("build-msg").textContent = "Attach the PDF before publishing";
    return;
  }
  // convenience only — the publish route enforces this server-side
  if (!(b.rights || "").trim()) {
    el("build-msg").textContent = "Set Rights before publishing";
    return;
  }
  let res;
  try {
    res = await (await fetch("/api/volumes/publish", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ build_id: b.id }),
    })).json();
  } catch (e) {
    statusCrit("PUBLISH :: server unreachable");
    return;
  }
  if (!res.ok) { statusErr("PUBLISH :: " + (res.error || "failed to start")); return; }
  status(`PUBLISHING :: ${b.title || b.id}`);
  pollPublish();
}

// A 129 MB volume is minutes, not seconds, so the upload runs server-side and
// the footer carries its progress.
let _publishTimer = null;
function pollPublish() {
  clearInterval(_publishTimer);
  _publishTimer = setInterval(async () => {
    let st;
    try {
      st = await (await fetch("/api/volumes/publish/status")).json();
    } catch (e) {
      clearInterval(_publishTimer);
      statusCrit("PUBLISH :: server unreachable");
      return;
    }
    if (st.stage === "uploading" && st.total) {
      const pct = Math.floor((st.sent / st.total) * 100);
      status(`PUBLISHING :: ${pct}% of ${(st.total / 1048576).toFixed(0)} MB via ${st.store}`);
      return;
    }
    if (st.stage === "recording") { status("PUBLISHING :: recording the volume"); return; }
    if (st.running) return;
    clearInterval(_publishTimer);
    if (st.stage === "error") { statusCrit("PUBLISH FAILED :: " + st.error); return; }
    if (st.stage === "cancelled") {
      status("PUBLISH CANCELLED :: nothing published");
      return;
    }
    if (st.stage === "done") {
      await loadBuilds();
      // clear the (now published) selection in all three aliases
      state.buildSel = null;
      state.anSel = null;
      ocrState.book = null;
      renderUpload();
      renderHome();
      status(`PUBLISHED :: ${st.slug}${st.note ? " :: " + st.note : ""}`);
    }
  }, 700);
}

// Rights suggestion: the offline renewal check (the same one behind the
// Catalogs copyright tag). Only a clear "Public domain (…)" finding
// preselects the state; the curator still reviews and saves.
async function suggestRights() {
  const msg = el("build-msg");
  const title = el("b-title").value.trim();
  if (!title) { msg.textContent = "Title is required"; return; }
  const q = new URLSearchParams({
    title, author: el("b-authors").value.trim(), year: el("b-year").value.trim(),
  });
  let res;
  try {
    res = await (await fetch("/api/copyright/status?" + q)).json();
  } catch (e) { msg.textContent = "Copyright check unreachable"; return; }
  const s = res.copyright_status || "";
  if (s.startsWith("Public domain")) {
    el("b-rights").value = "public-domain";
    buildDirty = true;
  }
  msg.textContent = s || "No determination";
}

// which Source-phase sub-view is active (the Entry pane is its own phase now)
function activeBuildTab() {
  const t = document.querySelector("#build-tabs .pane-tab.active");
  return t ? t.dataset.btab : "btab-source";
}

function renderBuildEditor() {
  const ed = el("build-editor");
  const b = currentBuild();
  ed.hidden = !b;
  el("build-empty").hidden = !!b;
  if (!b) return;
  for (const f of BUILD_FIELDS) {
    const input = el("b-" + f);
    if (input) input.value = b[f] || "";
  }
  catPickers["b-categories"].set(b.category_ids || []);
  el("b-ready").classList.toggle("active", b.status === "ready");
  el("b-verified-tag").hidden = b.status !== "ready";
  // only reset the description editor when its saved content changed —
  // background renders must not wipe an in-progress edit
  if (descState.id !== b.id || descState.val !== (b.description || "")) {
    buildDescMd.set(b.description || "");
    descState.id = b.id;
    descState.val = b.description || "";
  }
  const pdf = (b.pdf_source || "").trim();
  el("b-pdf-open").hidden = !/^https?:\/\//i.test(pdf);
  el("b-pdf-open").href = pdf;
  const src = (b.source_url || "").trim();
  el("b-src-open").hidden = !/^https?:\/\//i.test(src);
  el("b-src-open").href = src;
  el("build-msg").textContent = "";
  buildDirty = false;   // a freshly loaded form is clean
  scApplyBuildOverlay();
  if (wbActivePhase() === "source" && activeBuildTab() === "btab-source") {
    refreshSourceTab();
  }
}

// legacy name — selection is unified across the Workbench (#133)
function selectBuild(id) {
  selectWorkbenchBook(id);
}

async function createBuild(seed, label) {
  const res = await fetch("/api/builds", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ build: seed || {} }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok || !data.ok) { statusCrit("BUILD CREATE FAILED"); return null; }
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
  // pdf_source records where the scan lives online; pdf_file is filled with
  // the already-downloaded local copy when one exists. Locally attached
  // scans have no online source — the local file IS the source.
  let pdfUrl = "", pdfFile = "";
  if (s.local_pdf) {
    pdfFile = s.local_pdf;
  } else if (s.identifier) {
    pdfUrl = `https://archive.org/download/${s.identifier}/${s.identifier}.pdf`;
    if (state.downloadedIds.has(s.identifier)) {
      pdfFile = `downloads/ia/${s.identifier}.pdf`;
    }
  } else if (/^https?:\/\/.*\.pdf(\?|$)/i.test(s.url || "")) {
    pdfUrl = s.url;
  }
  return {
    title: s.title, subtitle: s.subtitle, authors: s.author,
    year: s.year, publisher: s.publisher, volume: s.volume || "",
    group_id: buildGroupIdFor(s),
    category_ids: s.category_ids || [],
    extra: s.extra || {}, images: s.images || [], capture_id: s.capture_id || "",
    pdf_source: pdfUrl, pdf_file: pdfFile, source_url: s.url,
    notes: `Source: ${s.archive}${s.matched_title ? " — " + s.matched_title : ""}`,
  };
}

let buildPatchConflict = false;  // last patch came back 409 (record reloaded)

async function patchBuildRaw(id, fields, quiet) {
  buildPatchConflict = false;
  const res = await fetch(`/api/builds/${encodeURIComponent(id)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(fields),
  });
  const data = await res.json().catch(() => ({}));
  if (res.ok && data.ok) {
    state.builds[id] = data.build;
    // quiet: background patches (auto-attach) must not re-render the form
    // and wipe unsaved field edits
    if (!quiet) renderUpload();
    return true;
  }
  if (res.status === 409 && data.build) {
    // another writer (analysis, sync) got there first: adopt its record
    buildPatchConflict = true;
    state.builds[id] = data.build;
    renderUpload();
  }
  return false;
}

async function patchBuild(id, fields, label) {
  const b = state.builds[id];
  if (!b) return false;
  const before = {};
  for (const f of Object.keys(fields)) before[f] = b[f] || "";
  if (await patchBuildRaw(id, fields)) {
    pushOp(label || `edit build ${b.title || id}`,
      () => patchBuildRaw(id, before),
      () => patchBuildRaw(id, fields));
    return true;
  }
  return false;
}

async function saveBuildFields(ev) {
  if (ev) ev.preventDefault();
  const id = state.buildSel;
  if (!id) return false;
  // a smart-check overlay must never ride into a save — baking is explicit
  scClearBuildOverlay();
  const fields = {};
  for (const f of BUILD_FIELDS) {
    const input = el("b-" + f);
    if (input) fields[f] = input.value.trim();
  }
  fields.category_ids = catPickers["b-categories"].get();
  fields.description = buildDescMd.get();
  if (fields.volume && !fields.group_id) fields.group_id = buildGroupIdFor(fields);
  // an uploaded entry keeps its status — saving a typo fix must not pull
  // it back into the Pending queue
  const cur0 = currentBuild();
  fields.status = cur0 && cur0.status === "uploaded"
    ? "uploaded"
    : el("b-ready").classList.contains("active") ? "ready" : "draft";
  // saving verifies the currently active OCR file for this book
  const cur = currentBuild();
  if (cur && cur.ocr_active) fields.ocr_verified = cur.ocr_active;
  if (!fields.title) { el("build-msg").textContent = "Title is required"; return false; }
  // optimistic concurrency: a save over a record another writer already
  // bumped comes back 409 and reloads instead of clobbering it
  fields.expect_updated_at = (state.builds[id] || {}).updated_at || "";
  if (await patchBuild(id, fields, `edit build ${fields.title.slice(0, 30)}`)) {
    descState.id = id;
    descState.val = fields.description;
    buildDirty = false;
    el("build-msg").textContent = "Saved";
    status(`BUILD SAVED :: ${fields.title}`);
    return true;
  } else {
    el("build-msg").textContent = buildPatchConflict
      ? "changed elsewhere — reloaded" : "Save failed";
    return false;
  }
}

async function deleteBuild() {
  // no confirmation: deletion is undoable
  const id = state.buildSel;
  const b = state.builds[id];
  if (!b) return;
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
    statusCrit("BUILD DELETE FAILED");
  }
}

function exportBuilds() {
  const builds = allBuildsSorted();   // every entry, whatever tab is active
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
  document.querySelectorAll("#build-tabs .pane-tab").forEach((t) =>
    t.classList.toggle("active", t.dataset.btab === id));
  document.querySelectorAll("#wb-source-body .pane-sub").forEach((p) =>
    p.classList.toggle("active", p.id === id));
  if (id === "btab-source") refreshSourceTab();
  if (id === "btab-resources") refreshResourcesTab();
}

// --- AI summary from the OCR text (OpenAI-compatible API via the server) --

async function generateAiSummary() {
  const b = currentBuild();
  if (!b) return;
  const s = state.settings;
  const msg = el("b-ai-msg");
  if (!(s.aiKey || "").trim() || !(s.aiModel || "").trim()) {
    msg.textContent = "Configure the model + API key (Settings > AI)";
    return;
  }
  const localPath = (b.pdf_file || "").trim();
  const url = (b.pdf_source || "").trim();
  const textSrc = localPath
    ? "/api/pdf/text?path=" + encodeURIComponent(localPath)
    : (/^https?:\/\//i.test(url)
        ? "/api/pdf/text?url=" + encodeURIComponent(url) : null);
  if (!textSrc) { msg.textContent = "No PDF source"; return; }
  el("b-ai").disabled = true;
  try {
    msg.textContent = "Extracting text ...";
    const ocr = await (await fetch(textSrc)).json();
    if (!ocr.ok || !(ocr.text || "").trim()) {
      msg.textContent = (ocr.error || "No text layer").slice(0, 80);
      return;
    }
    msg.textContent = "Generating ...";
    const res = await fetch("/api/ai/summarize", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        base_url: s.aiBase || "",
        api_key: s.aiKey || "",
        model: s.aiModel || "",
        instructions: s.aiInstructions || "",
        text: ocr.text,
      }),
    });
    const data = await res.json().catch(() => ({}));
    if (data.ok) {
      // the request is slow: the user may have switched builds meanwhile
      if (state.buildSel !== b.id) {
        status(`AI SUMMARY DISCARDED (BUILD CHANGED) :: ${b.title || b.id}`);
        return;
      }
      buildDescMd.set(data.summary || "");
      msg.textContent = "Generated (unsaved)";
      status(`AI SUMMARY GENERATED :: ${b.title || b.id}`);
    } else {
      msg.textContent = (data.error || "Failed").slice(0, 80);
    }
  } catch (e) {
    msg.textContent = "Request failed";
  } finally {
    el("b-ai").disabled = false;
  }
}

function loadDescriptionFile(file) {
  if (!file) return;
  const forBuild = state.buildSel;
  const reader = new FileReader();
  reader.onload = () => {
    if (state.buildSel !== forBuild) return;
    buildDescMd.set(String(reader.result || ""));
    el("b-ai-msg").textContent = "Loaded (unsaved)";
  };
  reader.readAsText(file);
}

// --- the builder's SOURCE tab: verify the PDF before marking READY --

function iaIdentFromBuild(b) {
  for (const u of [b.pdf_source, b.source_url]) {
    if (!u) continue;
    if (u.includes("/details/"))
      return u.split("/details/")[1].split(/[/?#]/)[0];
    if (u.includes("archive.org/download/"))
      return u.split("/download/")[1].split(/[/?#]/)[0].split("/")[0];
  }
  return "";
}

// preview derivative (compressed + truncated) unless the user opted to
// view the original unmodified PDF
function pdfViewSrc(path) {
  const base = pdfLocalSrc(path);
  return state.settings.previewOriginal
    ? base
    : base + "&preview=1&pages=" + (state.settings.previewPages || 20);
}

// The Source-tab viewer shows the PRIMARY PDF's pages, so only OCR files
// that belong to the primary may sit beside them — a secondary scan's
// active file would misalign every page (and page-view edits would save
// back misaligned).
function buildPrimaryOcrFiles() {
  return ((state.buildFolder && state.buildFolder.ocr) || [])
    .filter((f) => (f.src || "primary") === "primary");
}

// the OCR file page-view edits save into: the active file, else extracted
function buildActiveOcrName(b) {
  const files = buildPrimaryOcrFiles();
  if (b.ocr_active && files.some((f) => f.name === b.ocr_active)) return b.ocr_active;
  return "extracted.txt";
}

// the active OCR file's text feeds the viewer's OCR pane; without one, the
// folder's extracted.txt, then live extraction from the PDF itself
function buildTextSrc(b) {
  const files = buildPrimaryOcrFiles();
  const name = (b.ocr_active && files.some((f) => f.name === b.ocr_active))
    ? b.ocr_active
    : (files.some((f) => f.name === "extracted.txt") ? "extracted.txt" : "");
  if (name) {
    return `/api/builds/${encodeURIComponent(b.id)}/ocr/` +
      encodeURIComponent(name);
  }
  const localPath = (b.pdf_file || "").trim();
  // live extraction auto-saves into the entry folder (ocr/extracted.txt);
  // pages=0 extracts every page — the default 100 (and the old 400) would
  // permanently truncate longer books
  if (localPath) {
    return "/api/pdf/text?pages=0&path=" + encodeURIComponent(localPath) +
      "&save_build=" + encodeURIComponent(b.id);
  }
  const url = (b.pdf_source || "").trim();
  if (/^https?:\/\//i.test(url)) {
    return "/api/pdf/text?pages=0&url=" + encodeURIComponent(url) +
      "&save_build=" + encodeURIComponent(b.id);
  }
  return "";
}

function renderOcrChips(b) {
  const wrap = el("b-ocr-list");
  wrap.innerHTML = "";
  const folder = state.buildFolder;
  const files = (folder && folder.ocr) || [];
  for (const f of files) {
    const chip = document.createElement("span");
    chip.className = "ocr-chip" +
      (b.ocr_active === f.name ? " active" : "") +
      (b.ocr_verified === f.name ? " verified" : "");
    chip.textContent = f.name.replace(/\.txt$/i, "") +
      (b.ocr_verified === f.name ? " ✓" : "");
    chip.dataset.tip = `${f.name} (${fmtSize(f.size)})` +
      (b.ocr_active === f.name ? "\nActive" : "\nClick to make active") +
      (b.ocr_verified === f.name ? "\nVerified" : "");
    chip.dataset.ocr = f.name;
    wrap.appendChild(chip);
  }
}

async function loadBuildFolder(b) {
  try {
    state.buildFolder =
      await (await fetch(`/api/builds/${encodeURIComponent(b.id)}/folder`)).json();
  } catch (e) {
    state.buildFolder = null;
  }
}

// --- secondary PDF sources: other scans of the same book -------------------------

function renderPdfSources(b) {
  const wrap = el("b-pdf-sources");
  wrap.innerHTML = "";
  const list = (b.pdf_sources || []);
  if (!list.length) {
    const noneEl = document.createElement("span");
    noneEl.className = "tool-label";
    noneEl.textContent = "none";
    wrap.appendChild(noneEl);
    return;
  }
  for (const s of list) {
    const chip = document.createElement("span");
    chip.className = "ocr-chip src2-chip";
    chip.dataset.tip = `${s.path}\nOCR files of this scan sit under it in the OCR tab`;
    chip.innerHTML = `${esc((s.path || "").replace(/\\/g, "/").split("/").pop())}
      <button class="src2-del" type="button" data-sid="${esc(s.id)}"
              data-tip="Remove this secondary source (its OCR files stay)">&times;</button>`;
    wrap.appendChild(chip);
  }
}

async function addSecondaryPdf(path) {
  const b = currentBuild();
  if (!b || !path) return;
  const p = path.trim();
  let ok = false;
  try { ok = (await fetch(pdfLocalSrc(p), { method: "HEAD" })).ok; } catch (e) {}
  if (!ok) { el("b-src-msg").textContent = "File not found (or not a PDF)"; return; }
  const cur = b.pdf_sources || [];
  if ((b.pdf_file || "").trim() === p || cur.some((s) => s.path === p)) {
    el("b-src-msg").textContent = "Already attached";
    return;
  }
  const id = Math.random().toString(16).slice(2, 10);
  if (await patchBuild(b.id, { pdf_sources: [...cur, { id, path: p }] },
      `add secondary PDF to ${b.title || b.id}`)) {
    el("b-src-msg").textContent = "Secondary source added";
    refreshSourceTab();
  }
}

async function removeSecondaryPdf(sid) {
  const b = currentBuild();
  if (!b) return;
  const cur = b.pdf_sources || [];
  const next = cur.filter((s) => s.id !== sid);
  if (next.length === cur.length) return;
  if (await patchBuild(b.id, { pdf_sources: next },
      `remove secondary PDF from ${b.title || b.id}`)) {
    el("b-src-msg").textContent = "Secondary source removed";
    refreshSourceTab();
  }
}

// --- Resources tab: pick a thumbnail from title pages, a computed cover
// candidate, or any figures the OCR pipeline already extracted -------------

function resourceCard(src, source, label) {
  return `
    <div class="res-card" data-source="${esc(source)}">
      <img loading="lazy" decoding="async" src="${esc(src)}" alt="" />
      <div class="res-card-label">${esc(label)}</div>
      <button type="button" class="cad-btn tiny res-use">Use as thumbnail</button>
    </div>`;
}

function resourcePreviewHtml(bid, pdf, source) {
  const m = /^page:(\d+)$/.exec(source || "");
  if (m && pdf) {
    return `<img class="res-current-img" loading="lazy" decoding="async"
      src="/api/pdf/pageimg?path=${encodeURIComponent(pdf)}&page=${m[1]}&w=220" alt="" />`;
  }
  const im = /^image:(.+)$/.exec(source || "");
  if (im) {
    return `<img class="res-current-img" loading="lazy" decoding="async"
      src="/api/builds/${encodeURIComponent(bid)}/ocr/images/${encodeURIComponent(im[1])}" alt="" />`;
  }
  return `<p class="res-empty">None chosen — falls back to an auto-detected page at publish.</p>`;
}

async function setThumbnailSource(bid, source) {
  if (await patchBuildRaw(bid, { thumbnail_source: source }, true)) {
    status(`THUMBNAIL SET :: ${source}`);
    refreshResourcesTab();
  }
}

async function refreshResourcesTab() {
  const b = currentBuild();
  const bid = b && b.id;
  if (!bid) return;
  const stale = () => state.buildSel !== bid;
  const pdf = (b.pdf_file || "").trim();

  el("res-current").innerHTML = resourcePreviewHtml(bid, pdf, b.thumbnail_source);

  const titles = [...titlePageSet(b)].sort((a, z) => a - z);
  el("res-titlepages").innerHTML = pdf && titles.length
    ? titles.map((n) =>
        resourceCard(`/api/pdf/pageimg?path=${encodeURIComponent(pdf)}&page=${n}&w=220`,
          `page:${n}`, `Page ${n}`)).join("")
    : `<p class="res-empty">No title pages marked yet — mark one in the OCR tab's page view.</p>`;

  if (!pdf) {
    el("res-cover").innerHTML = `<p class="res-empty">Attach a PDF first.</p>`;
  } else {
    el("res-cover").innerHTML = `<p class="res-empty">Checking…</p>`;
    try {
      const r = await (await fetch(`/api/builds/${encodeURIComponent(bid)}/cover-candidate`)).json();
      if (stale()) return;
      el("res-cover").innerHTML = r.ok && r.page
        ? resourceCard(`/api/pdf/pageimg?path=${encodeURIComponent(pdf)}&page=${r.page}&w=220`,
            `page:${r.page}`, `Cover (page ${r.page})`)
        : `<p class="res-empty">No content page detected.</p>`;
    } catch (e) {
      if (!stale()) el("res-cover").innerHTML = `<p class="res-empty">Could not check for a cover candidate.</p>`;
    }
  }

  const meta = await ocrLayoutMeta(bid);
  if (stale()) return;
  const names = Object.keys(meta.images || {});
  el("res-images").innerHTML = names.length
    ? names.map((name) => {
        const page = (meta.images[name] || {}).page;
        return resourceCard(`/api/builds/${encodeURIComponent(bid)}/ocr/images/${encodeURIComponent(name)}`,
          `image:${name}`, page ? `p. ${page}` : name);
      }).join("")
    : `<p class="res-empty">None yet — run OCR with the Mistral service to extract figures.</p>`;
}

async function refreshSourceTab() {
  const b = currentBuild();
  if (!b) return;
  // the awaits below can outlive a build switch — never render stale data
  const stale = () => state.buildSel !== b.id;
  let localPath = (b.pdf_file || "").trim();
  // auto-populate: a PDF that was auto-sourced from a URL and already
  // downloaded gets its local path attached without asking
  if (!localPath) {
    const ident = iaIdentFromBuild(b);
    if (ident && state.downloadedIds.has(ident)) {
      localPath = `downloads/ia/${ident}.pdf`;
      await patchBuildRaw(b.id, { pdf_file: localPath }, true);
      if (stale()) return;
      el("b-src-msg").textContent = "Local PDF attached automatically";
    }
  }
  el("b-pdf_file").value = localPath;
  await loadBuildFolder(b);
  if (stale()) return;
  renderOcrChips(b);
  renderPdfSources(b);
  const textSrc = buildTextSrc(b);
  // no attached file? fall back to the entry folder's own primary.pdf /
  // preview.pdf, exactly as the Source chip and the Text phase (ocrBookPdf)
  // do — the viewer and the chip must agree. The attach row stays empty.
  const viewPath = localPath || ocrBookPdf(b.id);
  if (viewPath) {
    // an entry folder's preview.pdf is already a derivative — serve it as-is
    const entryPrev = /^output[\/\\]entries[\/\\]/i.test(viewPath);
    const derived = !state.settings.previewOriginal && !entryPrev;
    buildPdfViewer.show(
      derived ? pdfViewSrc(viewPath) : pdfLocalSrc(viewPath),
      viewPath + (derived || entryPrev ? "  (preview)" : ""), {
        textSrc,
        // page-aligned OCR view (like the OCR tab), editable + savable
        pagesPdf: viewPath,
        pagesSaveTo: { buildId: b.id, name: buildActiveOcrName(b) },
      });
  } else if (/^https?:\/\//i.test((b.pdf_source || "").trim())) {
    const url = b.pdf_source.trim();
    // proxied through the server: direct iframes of third-party PDFs are
    // blocked by X-Frame-Options
    const derived = !state.settings.previewOriginal;
    buildPdfViewer.show(
      pdfProxySrc(url) +
        (derived ? "&preview=1&pages=" + (state.settings.previewPages || 20) : ""),
      url + (derived ? "  (remote preview)" : "  (remote)"), { textSrc });
  } else {
    buildPdfViewer.clear("No PDF");
  }
}

// create/refresh the entry folder: metadata + PDF preview + extracted OCR
async function syncBuildFolder() {
  const b = currentBuild();
  if (!b) return;
  el("b-src-msg").textContent = "Building folder ...";
  el("b-folder").disabled = true;
  try {
    const res = await fetch(`/api/builds/${encodeURIComponent(b.id)}/folder`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        pages: state.settings.previewPages || 20,
        keep_original: state.settings.keepOriginals !== false,
        trim_blank: !!state.settings.trimBlank,
      }),
    });
    const data = await res.json().catch(() => ({}));
    if (data.ok) {
      state.buildFolder = data;
      // the sync may have retired the IA original and repointed pdf_file
      // at the folder's preview.pdf
      if (data.build) state.builds[b.id] = data.build;
      // a blank-page trim rewrites the PDF: cached counts/dims are stale,
      // and the trim renumbers the word/region sidecars server-side
      ocrState.pdfInfo = {};
      ocrState.wordsCache.clear();
      ocrState.regionsCache.clear();
      delete ocrState.layoutMeta[b.id];
      el("b-src-msg").textContent =
        "Folder ready" + (data.notes && data.notes.length
          ? " — " + data.notes.join("; ") : "");
      status(`ENTRY FOLDER :: ${data.path}`);
      if (state.buildSel === b.id) {
        refreshSourceTab();
        loadDownloads();  // the original may have been removed
      }
    } else {
      el("b-src-msg").textContent = "Folder build failed";
    }
  } catch (e) {
    el("b-src-msg").textContent = "Folder build failed";
  } finally {
    el("b-folder").disabled = false;
  }
}

async function uploadOcrFile(file) {
  const b = currentBuild();
  if (!b || !file) return;
  const forBuild = b.id;
  const reader = new FileReader();
  reader.onload = async () => {
    const res = await fetch(`/api/builds/${encodeURIComponent(forBuild)}/ocr`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: file.name, text: String(reader.result || "") }),
    });
    const data = await res.json().catch(() => ({}));
    if (data.ok && state.buildSel === forBuild) {
      state.buildFolder = data.folder;
      renderOcrChips(currentBuild());
      el("b-src-msg").textContent = `OCR loaded :: ${data.name}`;
    }
  };
  reader.readAsText(file);
}

async function setActiveOcr(name) {
  const b = currentBuild();
  if (!b) return;
  if (await patchBuildRaw(b.id, { ocr_active: name }, true)) {
    renderOcrChips(currentBuild());
    // point the viewer's OCR pane at the newly active file
    refreshSourceTab();
    status(`ACTIVE OCR :: ${name}`);
  }
}

async function attachPdfFile(path) {
  const b = currentBuild();
  if (!b) return;
  const p = (path != null ? path : el("b-pdf_file").value).trim();
  el("b-pdf_file").value = p;
  if (p) {
    // confirm the file is actually readable before saving the path
    let ok = false;
    try { ok = (await fetch(pdfLocalSrc(p), { method: "HEAD" })).ok; } catch (e) {}
    if (!ok) {
      el("b-src-msg").textContent = "File not found (or not a PDF)";
      return;
    }
  }
  if (await patchBuild(b.id, { pdf_file: p },
      p ? `attach PDF to ${b.title || b.id}` : `detach PDF from ${b.title || b.id}`)) {
    el("b-src-msg").textContent = p ? "Attached" : "Detached";
    refreshSourceTab();
  } else {
    el("b-src-msg").textContent = "Save failed";
  }
}

// --- OCR tab: load, review, compare, and correct OCR text -------------------------
// OCR targets are the books' PDFs. The sidebar lists every book folder;
// selecting one loads its OCR text files as documents. Cloud/local OCR
// processing (Azure Document Intelligence, OpenAI vision, Tesseract) plugs
// into the queue once service credentials exist (Settings > OCR) — TODO:
// verify against a live service when the user has an API key.

const ocrState = {
  docs: [], sel: null, view: "pdf", jobs: [], seq: 0,   // page view is home
  lastRendered: null,      // doc id the views currently hold, so a re-render
                           // of the SAME doc can keep the reader's scroll
  layout: false,           // page view: words placed as they sit on the page
  books: null,             // build id -> {ocr, preview} (entry folders)
  book: null,              // selected book (build id)
  bookLoading: null,       // build id currently loading (re-entrancy guard)
  verifiedOnly: false,     // off by default: drafts must be reachable to edit
                           // their Record (the filter remains a toggle)
  pages: null,             // {pre, map} sections for the side-by-side view
  pageTags: new Map(),     // "bid:src:page" -> service, STAGED (submit is manual)
  pageRunning: new Map(),  // "bid:src:page" -> service, submitted and processing
  analysisTags: new Map(), // "bid:src:page" -> {engine, doc}, staged analysis
  pageSel: new Set(),      // selected page numbers (current page view's PDF;
                           // cleared whenever the view swaps to another PDF)
  selAnchor: 0,            // last plain-clicked page (Ctrl+click ranges)
  pagesPdf: "",            // pdf path the page view currently shows — a
                           // re-render of the SAME pdf keeps the scroll
  pagesSrc: "primary",     // source key of that pdf (staging/selection bind
                           // to the source, not just the book)
  pdfInfo: {},             // pdf path -> {pages, dims} (/api/pdf/info cache)
  layoutMeta: {},          // build id -> extracted-figure boxes (ocr-layout)
  wordsCache: new Map(),   // "pdf|page" -> /api/pdf/words result
  regionsCache: new Map(), // "bid|src|page" -> /ocr-regions record
  treeCollapsed: new Set(),// "bid:srckey" — collapsed nodes of the docs tree
};

function ocrSelDoc() {
  return ocrState.docs.find((d) => d.id === ocrState.sel) || null;
}

function ocrSyncEditor() {
  // a pending page-view edit must land in ITS document before anything
  // switches docs/views or saves
  flushOcrPageSync();
  const d = ocrSelDoc();
  if (d && ocrState.view === "edit") d.text = el("ocr-editor").value;
}

function ocrAddDoc(name, text, buildId, fileName) {
  ocrSyncEditor();
  const id = "d" + (++ocrState.seq);
  ocrState.docs.push({ id, name, text: String(text || ""),
                       buildId: buildId || null, fileName: fileName || null });
  ocrState.sel = id;
  renderOcrTab();
}

// --- the book-folder sidebar --

async function loadOcrBooks() {
  try {
    const data = await (await fetch("/api/entries")).json();
    ocrState.books = data.entries || {};
  } catch (e) { ocrState.books = {}; }
}

// the book's OCR target: its PRIMARY PDF (the attached file, or the entry
// folder's own primary.pdf / legacy preview.pdf derivative)
function ocrBookPdf(bid) {
  const b = state.builds[bid];
  const pf = b && (b.pdf_file || "").trim();
  if (pf) return pf;
  const folder = (ocrState.books || {})[bid];
  if (folder && folder.primary_pdf) {
    return `output/entries/${bid}/${folder.primary_pdf}`;
  }
  if (folder && folder.preview) return `output/entries/${bid}/preview.pdf`;
  return "";
}

// every PDF source of a book: the primary plus the secondaries, in order
function bookSources(bid) {
  const b = state.builds[bid];
  const out = [];
  const primary = ocrBookPdf(bid);
  const primaryName = primary.replace(/\\/g, "/").split("/").pop();
  out.push({ key: "primary", path: primary,
             label: primaryName ? `Original PDF - ${primaryName}` : "Original PDF" });
  for (const s of (b && b.pdf_sources) || []) {
    const path = (s.path || "").trim();
    if (!path) continue;
    out.push({ key: s.id, path,
               label: path.replace(/\\/g, "/").split("/").pop() || s.id });
  }
  return out;
}

function ocrSrcPdf(bid, key) {
  if (!key || key === "primary") return ocrBookPdf(bid);
  const s = bookSources(bid).find((x) => x.key === key);
  return s ? s.path : "";
}

// which PDF a document belongs to: its recorded source, else the primary
function docSrcKey(d) { return (d && d.src) || "primary"; }
function docPdf(d) { return d && d.buildId ? ocrSrcPdf(d.buildId, docSrcKey(d)) : ""; }

// the OCR-merge target for a source: one compiled file per PDF
function srcCompiledName(key) {
  return !key || key === "primary" ? "compiled.txt" : `compiled-${key}.txt`;
}
function srcExtractedName(key) {
  return !key || key === "primary" ? "extracted.txt" : `extracted-${key}.txt`;
}

// The ONE Workbench book list: every build — with or without an entry folder —
// in the Editor's old Pending / Uploaded queues, grouped by volume set, with
// the existing status icons and an OCR count where a folder exists.
function renderOcrBooks() {
  const list = el("ocr-books");
  list.innerHTML = "";
  document.querySelectorAll("#builds-tabs .pane-tab").forEach((t) =>
    t.classList.toggle("active", t.dataset.bstab === buildsTab()));
  el("ocr-filter-verified").classList.toggle("active", ocrState.verifiedOnly);
  const builds = buildsSorted().filter((b) =>
    !ocrState.verifiedOnly || anAnalyzable(b));
  el("ocr-books-empty").hidden = builds.length !== 0;
  el("ocr-books-empty").textContent =
    buildsTab() === "uploaded" ? "Nothing published yet" : "No entries yet";
  for (const item of editorBuildItems(builds)) {
    if (item.type === "group") {
      const li = document.createElement("li");
      li.className = "build-group";
      li.dataset.gid = item.id;
      li.innerHTML = `<span class="bi-row"><span class="bi-title">${
        item.expanded ? "&#9662;" : "&#9656;"} ${esc(item.title)}</span></span>
        <span class="bi-meta">${item.total} volume${item.total === 1 ? "" : "s"}${
          item.members.length === item.total ? "" : ` &middot; ${item.members.length} in this queue`}</span>`;
      list.appendChild(li);
      if (item.expanded) {
        for (const member of item.members) appendBuildListItem(list, member, true);
      }
      continue;
    }
    appendBuildListItem(list, item.build, false);
  }
}

// load a book folder's OCR files as documents (replacing its previous docs)
async function selectOcrBook(bid) {
  // double-clicks / rapid re-clicks must not run two loads of the same book
  if (ocrState.bookLoading === bid) return;
  ocrSyncEditor();
  ocrState.book = bid;
  state.anSel = anAnalyzable(state.builds[bid] || {}) ? bid : null;
  // the three selections are aliases of one Workbench choice (#133)
  if (state.buildSel !== bid) {
    state.buildSel = bid;
    renderBuildEditor();
  }
  renderWorkbench();
  clearOcrPageSel();   // selections don't carry across books
  let folder = (ocrState.books || {})[bid];
  // A selection can arrive before the folder list has ever loaded (the Home
  // jump selects immediately after switching tabs) — fetch it, don't give up.
  if (!folder && ocrState.books === null) {
    await loadOcrBooks();
    if (ocrState.book !== bid) return;   // switched away while loading
    folder = (ocrState.books || {})[bid];
  }
  if (!folder) { renderOcrTab(); return; }
  // the guard must cover the auto-extraction await too, or a double-click
  // runs the (expensive) extraction twice
  ocrState.bookLoading = bid;
  try {
    // a folder without OCR files gets its extraction saved automatically
    // the first time the book is opened here (pages=0 = every page)
    if (!folder.ocr.length && ocrBookPdf(bid)) {
      try {
        const ex = await (await fetch("/api/pdf/text?pages=0&path=" +
          encodeURIComponent(ocrBookPdf(bid)) +
          "&save_build=" + encodeURIComponent(bid))).json();
        // A scan carries a text layer on its cover sheet and nowhere else, so
        // the extraction "succeeds" and yields one page of Google boilerplate.
        // Say so, rather than presenting an empty folder as a mystery.
        if (ex.ok && (ex.pages_with_text || 0) <= 1) {
          el("ocr-msg").textContent =
            "This PDF has no text layer — OCR the pages (digit keys stage a service)";
        }
        await loadOcrBooks();
        folder = (ocrState.books || {})[bid] || folder;
      } catch (e) { /* extraction failed; the empty folder renders as-is */ }
      if (ocrState.book !== bid) return;
    }
    // fetch everything first, then commit atomically — an interleaved load
    // of another book can't leave duplicates behind
    const loaded = [];
    for (const f of folder.ocr) {
      try {
        const data = await (await fetch(
          `/api/builds/${encodeURIComponent(bid)}/ocr/` +
          encodeURIComponent(f.name))).json();
        if (data.ok) loaded.push({ name: f.name, text: String(data.text || ""),
                                   src: f.src || "primary" });
      } catch (e) { /* skip unreadable files */ }
      if (ocrState.book !== bid) return;   // switched away mid-load
    }
    ocrState.docs = ocrState.docs.filter((d) => d.buildId !== bid);
    let firstId = null;
    for (const l of loaded) {
      const id = "d" + (++ocrState.seq);
      ocrState.docs.push({ id, name: l.name, text: l.text,
                           buildId: bid, fileName: l.name, src: l.src });
      if (!firstId) firstId = id;
    }
    if (firstId) ocrState.sel = firstId;
    renderOcrTab();
    renderAnList();
    renderAnMain();
    renderWorkbench();   // the folder manifest feeds the readiness chips
    status(folder.ocr.length
      ? `Loaded ${folder.ocr.length} OCR file(s)`
      : "No OCR files for this book yet (attach a PDF in the Source phase)");
  } finally {
    if (ocrState.bookLoading === bid) ocrState.bookLoading = null;
  }
}

// only the current book's documents (plus loose local files) are listed
function ocrVisibleDocs() {
  return ocrState.docs.filter((d) => !d.buildId || d.buildId === ocrState.book);
}

// The documents pane is a file tree: one node per PDF source of the selected
// book (primary.pdf first, then each secondary scan), its OCR files beneath.
// Loose .txt files loaded by hand sit under their own "Local files" node.
function legacyRenderDocuments() {
  const list = el("ocr-docs");
  list.innerHTML = "";
  const docs = ocrVisibleDocs();
  const bid = ocrState.book;
  const sources = bid ? bookSources(bid) : [];
  el("ocr-docs-empty").hidden = docs.length !== 0 || sources.length !== 0;

  const docLi = (d) => {
    const li = document.createElement("li");
    li.className = "ocr-doc tree-doc" + (d.id === ocrState.sel ? " active" : "");
    li.dataset.did = d.id;
    li.innerHTML = `
      <span class="bi-title">${esc(d.name)}</span>
      <span class="bi-meta">${Math.max(1, Math.round(d.text.length / 1000))}k chars</span>`;
    return li;
  };

  for (const s of sources) {
    const children = docs.filter((d) => d.buildId === bid && docSrcKey(d) === s.key);
    const collapsed = ocrState.treeCollapsed.has(`${bid}:${s.key}`);
    const li = document.createElement("li");
    li.className = "ocr-src";
    li.dataset.src = s.key;
    li.dataset.tip = s.path || "No PDF attached";
    // a source with a PDF but no extraction offers to pull its text layer
    const canExtract = s.path &&
      !children.some((d) => (d.fileName || d.name) === srcExtractedName(s.key));
    li.innerHTML = `
      <span class="tree-arrow">${collapsed ? "&#9656;" : "&#9662;"}</span>
      <span class="tree-ico">${ICONS.pdf}</span>
      <span class="bi-title">${esc(s.label)}</span>
      <span class="tree-count">${children.length || ""}</span>` +
      (canExtract ? `<button class="cad-btn tiny icon-btn src-extract" type="button"
         data-src="${esc(s.key)}" data-tip="Extract this PDF's text layer">${ICONS.text}</button>` : "");
    list.appendChild(li);
    if (!collapsed) for (const d of children) list.appendChild(docLi(d));
  }
  // docs whose source was removed still exist on disk and stay reachable —
  // a book must never hold an invisible selected document
  const keys = new Set(sources.map((s) => s.key));
  const orphans = docs.filter((d) => d.buildId === bid && !keys.has(docSrcKey(d)));
  if (orphans.length) {
    const li = document.createElement("li");
    li.className = "ocr-src";
    li.dataset.src = "";
    li.dataset.tip = "OCR files of a PDF source that was removed from the book";
    li.innerHTML = `
      <span class="tree-arrow"></span>
      <span class="tree-ico">${ICONS.folder}</span>
      <span class="bi-title">Removed source</span>
      <span class="tree-count">${orphans.length}</span>`;
    list.appendChild(li);
    for (const d of orphans) list.appendChild(docLi(d));
  }
  const loose = docs.filter((d) => !d.buildId);
  if (loose.length) {
    const li = document.createElement("li");
    li.className = "ocr-src";
    li.dataset.src = "";
    li.innerHTML = `
      <span class="tree-arrow"></span>
      <span class="tree-ico">${ICONS.folder}</span>
      <span class="bi-title">Local files</span>
      <span class="tree-count">${loose.length}</span>`;
    list.appendChild(li);
    for (const d of loose) list.appendChild(docLi(d));
  }

  // figures an OCR service (Mistral) cut out of a page, alongside the
  // compiled text output above — from the same /api/entries folder info
  const images = bid ? ((ocrState.books || {})[bid] || {}).images || [] : [];
  if (images.length) {
    const hdr = document.createElement("li");
    hdr.className = "ocr-src";
    hdr.dataset.src = "";
    hdr.dataset.tip = "Figures an OCR service cut out of a page";
    hdr.innerHTML = `
      <span class="tree-arrow"></span>
      <span class="tree-ico">${ICONS.image}</span>
      <span class="bi-title">Images</span>
      <span class="tree-count">${images.length}</span>`;
    list.appendChild(hdr);
    for (const im of images) {
      const row = document.createElement("li");
      row.className = "ocr-doc tree-doc ocr-img-row";
      row.dataset.imgName = im.name;
      row.innerHTML = `
        <img class="ocr-img-thumb" loading="lazy" decoding="async" alt=""
             src="/api/builds/${encodeURIComponent(bid)}/ocr/images/${encodeURIComponent(im.name)}" />
        <span class="bi-title">${im.page ? `p. ${im.page}` : esc(im.name)}</span>`;
      list.appendChild(row);
    }
  }
}

// The Analyze sidebar is an artifact manifest, not just an OCR-file list.
// Text artifacts lazy-load into the central Document workspace; large PDFs and
// images remain external viewers so selecting an artifact never reads a whole
// scan into renderer memory.
function renderOcrDocs() {
  const list = el("ocr-docs");
  list.innerHTML = "";
  const docs = ocrVisibleDocs();
  const bid = ocrState.book;
  const folder = bid ? ((ocrState.books || {})[bid] || {}) : {};
  const sources = bid ? bookSources(bid) : [];

  const header = (label, icon, count, tip) => {
    const li = document.createElement("li");
    li.className = "artifact-group";
    if (tip) li.dataset.tip = tip;
    li.innerHTML = `<span class="tree-ico">${icon}</span>` +
      `<span class="bi-title">${esc(label)}</span>` +
      `<span class="tree-count">${count || ""}</span>`;
    list.appendChild(li);
  };
  const placeholder = (label) => {
    const li = document.createElement("li");
    li.className = "artifact-placeholder";
    li.textContent = label;
    list.appendChild(li);
  };
  // manifest staleness: true = an input changed since generation; null/undefined
  // (no manifest row — legacy artifact) shows nothing
  const staleMark = (meta) => meta && meta.stale === true
    ? `<span class="artifact-stale" data-tip="An input changed since this was generated">outdated</span>`
    : "";
  const textArtifact = (label, kind, name, meta) => {
    const li = document.createElement("li");
    const selected = ocrSelDoc();
    li.className = "ocr-doc tree-doc artifact-text" +
      (selected && selected.artifactKind === kind && selected.artifactName === name ? " active" : "");
    li.dataset.artifactKind = kind;
    li.dataset.artifactName = name;
    const size = meta && meta.size ? `${Math.max(1, Math.round(meta.size / 1024))}k` : "";
    li.innerHTML = `<span class="tree-ico">${ICONS.text}</span>` +
      `<span class="bi-title">${esc(label)}</span>` +
      staleMark(meta) +
      `<span class="bi-meta">${esc(size)}</span>`;
    list.appendChild(li);
  };

  if (!bid) {
    el("ocr-docs-empty").hidden = docs.length !== 0;
    for (const d of docs) {
      const li = document.createElement("li");
      li.className = "ocr-doc tree-doc" + (d.id === ocrState.sel ? " active" : "");
      li.dataset.did = d.id;
      li.innerHTML = `<span class="bi-title">${esc(d.name)}</span>`;
      list.appendChild(li);
    }
    return;
  }
  el("ocr-docs-empty").hidden = true;

  header("PDF documents", ICONS.pdf, sources.filter((s) => s.path).length +
    (folder.processed_pdf ? 1 : 0));
  for (const s of sources) {
    const li = document.createElement("li");
    li.className = "artifact-pdf-row" + (s.path ? "" : " unavailable");
    li.dataset.src = s.key;
    li.dataset.tip = s.path || "No PDF attached";
    const canExtract = s.path && !docs.some((d) => !d.artifactKind &&
      d.buildId === bid && docSrcKey(d) === s.key &&
      (d.fileName || d.name) === srcExtractedName(s.key));
    li.innerHTML = `<span class="tree-ico">${ICONS.pdf}</span>` +
      `<span class="bi-title">${esc(s.label)}</span>` +
      (s.path ? `<button class="cad-btn tiny artifact-open-pdf" type="button" ` +
        `data-pdf="${esc(s.path)}">Open</button>` : `<span class="bi-meta">missing</span>`) +
      (canExtract ? `<button class="cad-btn tiny icon-btn src-extract" type="button" ` +
        `data-src="${esc(s.key)}" data-tip="Extract this PDF's text layer">${ICONS.text}</button>` : "");
    list.appendChild(li);
  }
  if (folder.processed_pdf) {
    const processed = `output/entries/${bid}/${folder.processed_pdf}`;
    const li = document.createElement("li");
    li.className = "artifact-pdf-row";
    li.innerHTML = `<span class="tree-ico">${ICONS.pdf}</span>` +
      `<span class="bi-title">Processed PDF</span>` +
      `<button class="cad-btn tiny artifact-open-pdf" type="button" data-pdf="${esc(processed)}">Open</button>`;
    list.appendChild(li);
  } else {
    placeholder("Processed PDF - not generated (preprocessing is coming later)");
  }

  const ocrDocs = docs.filter((d) => d.buildId === bid && !d.artifactKind);
  const ocrProv = new Map((folder.ocr || []).map((o) => [o.name, o]));
  header("OCR data", ICONS.text, ocrDocs.length);
  if (!ocrDocs.length) placeholder("No OCR data");
  for (const d of ocrDocs) {
    const li = document.createElement("li");
    li.className = "ocr-doc tree-doc" + (d.id === ocrState.sel ? " active" : "");
    li.dataset.did = d.id;
    const source = sources.find((s) => s.key === docSrcKey(d));
    li.innerHTML = `<span class="bi-title">${esc(d.name)}</span>` +
      staleMark(ocrProv.get(d.fileName || d.name)) +
      `<span class="bi-meta">${esc(source ? source.label : "removed source")}</span>`;
    list.appendChild(li);
  }

  const fullText = folder.full_text || [];
  header("Full text", ICONS.text, fullText.length,
         "Curated text is distinct from any single OCR pass");
  if (!fullText.length) placeholder("No combined full text");
  for (const f of fullText)
    textArtifact(f.artifact || f.name, "full_text", f.artifact || f.name, f);

  const translations = folder.translations || [];
  header("Translations", ICONS.text, translations.length);
  if (!translations.length) placeholder("No translations");
  for (const t of translations) {
    textArtifact(`${String(t.lang || t.name).toUpperCase()} translation`,
                 "translation", t.lang || String(t.name || "").replace(/\.txt$/i, ""), t);
  }

  const analysis = folder.analysis || [];
  const analysisCount = analysis.length + (folder.summary && folder.summary.exists ? 1 : 0) +
    (folder.about && folder.about.exists ? 1 : 0);
  header("Analysis data", ICONS.text, analysisCount);
  if (folder.summary && folder.summary.exists)
    textArtifact("Summary", "summary", "summary.md", folder.summary);
  if (folder.about && folder.about.exists)
    textArtifact("Description / About", "about", "about.md", folder.about);
  for (const a of analysis) textArtifact(a.name, "analysis", a.name, a);
  if (!analysisCount) placeholder("No text-analysis artifacts");

  const capturedImages = folder.captured_images || [];
  const extractedImages = folder.images || [];
  header("Images", ICONS.image, capturedImages.length + extractedImages.length);
  if (!capturedImages.length && !extractedImages.length)
    placeholder("No captured or extracted images");
  for (const im of capturedImages) {
    const row = document.createElement("li");
    const url = "/api/capture/image?path=" + encodeURIComponent(im.path || "");
    row.className = "ocr-doc tree-doc ocr-img-row capture-img-row" +
      (im.available ? "" : " unavailable");
    row.dataset.capturePath = im.path || "";
    row.dataset.captureAvailable = im.available ? "1" : "";
    row.innerHTML = (im.available
      ? `<img class="ocr-img-thumb" loading="lazy" decoding="async" alt="" src="${esc(url)}" />`
      : `<span class="tree-ico">${ICONS.image}</span>`) +
      `<span class="bi-title">Captured photo - ${esc(im.name || "image")}</span>` +
      (im.available ? "" : `<span class="bi-meta">missing</span>`);
    list.appendChild(row);
  }
  for (const im of extractedImages) {
    const row = document.createElement("li");
    row.className = "ocr-doc tree-doc ocr-img-row";
    row.dataset.imgName = im.name;
    row.innerHTML = `<img class="ocr-img-thumb" loading="lazy" decoding="async" alt="" ` +
      `src="/api/builds/${encodeURIComponent(bid)}/ocr/images/${encodeURIComponent(im.name)}" />` +
      `<span class="bi-title">${im.page ? `p. ${im.page}` : esc(im.name)}</span>`;
    list.appendChild(row);
  }

  const loose = docs.filter((d) => !d.buildId);
  if (loose.length) {
    header("Local files", ICONS.folder, loose.length);
    for (const d of loose) {
      const li = document.createElement("li");
      li.className = "ocr-doc tree-doc" + (d.id === ocrState.sel ? " active" : "");
      li.dataset.did = d.id;
      li.innerHTML = `<span class="bi-title">${esc(d.name)}</span>`;
      list.appendChild(li);
    }
  }
}

async function openTextArtifact(kind, name) {
  const bid = ocrState.book;
  if (!bid || !kind || !name) return;
  let url = "";
  if (kind === "summary" || kind === "about") {
    url = `/api/builds/${encodeURIComponent(bid)}/${kind}`;
  } else if (kind === "translation") {
    url = `/api/builds/${encodeURIComponent(bid)}/translations/${encodeURIComponent(name)}`;
  } else {
    const artifactPath = String(name).split("/").map(encodeURIComponent).join("/");
    url = `/api/builds/${encodeURIComponent(bid)}/artifact/${encodeURIComponent(kind)}/` +
      artifactPath;
  }
  try {
    const res = await fetch(url);
    const data = await res.json().catch(() => ({}));
    if (!res.ok || !data.ok) throw new Error(data.error || `HTTP ${res.status}`);
    const id = `artifact:${bid}:${kind}:${name}`;
    ocrState.docs = ocrState.docs.filter((d) => d.id !== id);
    ocrState.docs.push({ id, name, text: String(data.text || ""), buildId: bid,
                         artifactKind: kind, artifactName: name,
                         readOnly: true, src: "primary" });
    ocrState.sel = id;
    setAnalyzeWorkspace("document", true);
    setOcrView("edit");
    renderOcrTab();
  } catch (e) {
    el("ocr-msg").textContent = `Could not open artifact: ${e.message || e}`;
  }
}

// Pull a secondary (or the primary) PDF's text layer into its own document.
// Only the NEW file is fetched and added — the book's other documents stay
// as they are, unsaved edits included.
async function ocrExtractSource(key) {
  const bid = ocrState.book;
  const pdf = ocrSrcPdf(bid, key);
  if (!bid || !pdf) return;
  el("ocr-msg").textContent = "Extracting text ...";
  try {
    const name = srcExtractedName(key);
    const ex = await (await fetch("/api/pdf/text?pages=0&path=" +
      encodeURIComponent(pdf) +
      "&save_build=" + encodeURIComponent(bid) +
      "&save_name=" + encodeURIComponent(name) +
      "&src=" + encodeURIComponent(key))).json();
    if (ex.ok && (ex.pages_with_text || 0) <= 1) {
      el("ocr-msg").textContent =
        "This PDF has no text layer — OCR the pages (digit keys stage a service)";
    } else {
      el("ocr-msg").textContent = ex.ok ? "" : (ex.error || "extraction failed");
    }
    await loadOcrBooks();
    if (ocrState.book !== bid) return;
    if (ex.ok && ex.saved &&
        !ocrState.docs.some((d) => d.buildId === bid &&
                                   (d.fileName || d.name) === ex.saved)) {
      ocrSyncEditor();
      const id = "d" + (++ocrState.seq);
      ocrState.docs.push({ id, name: ex.saved, text: ex.text || "",
                           buildId: bid, fileName: ex.saved, src: key });
      ocrState.sel = id;
    }
    renderOcrTab();
  } catch (e) {
    el("ocr-msg").textContent = "extraction failed";
  }
}

// a small line diff (LCS) — capped so huge scans stay responsive
function diffLines(aText, bText) {
  const CAP = 2500;
  const A = aText.split("\n"), B = bText.split("\n");
  const at = A.slice(0, CAP), bt = B.slice(0, CAP);
  const n = at.length, m = bt.length;
  const W = m + 1;
  const dp = new Uint16Array((n + 1) * W);
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      dp[i * W + j] = at[i] === bt[j]
        ? dp[(i + 1) * W + j + 1] + 1
        : Math.max(dp[(i + 1) * W + j], dp[i * W + j + 1]);
    }
  }
  const out = [];
  let i = 0, j = 0;
  while (i < n && j < m) {
    if (at[i] === bt[j]) { out.push(["=", at[i]]); i++; j++; }
    else if (dp[(i + 1) * W + j] >= dp[i * W + j + 1]) out.push(["-", at[i++]]);
    else out.push(["+", bt[j++]]);
  }
  while (i < n) out.push(["-", at[i++]]);
  while (j < m) out.push(["+", bt[j++]]);
  if (A.length > CAP || B.length > CAP)
    out.push(["~", `[diff truncated at ${CAP} lines]`]);
  return out;
}

function renderOcrDiff() {
  const a = ocrSelDoc();
  const bid = el("ocr-diff-with").value;
  const b = ocrState.docs.find((d) => d.id === bid);
  const box = el("ocr-diff");
  if (!a || !b || a.id === b.id) {
    box.innerHTML = `<p class="empty">Pick two different documents</p>`;
    return;
  }
  const ops = diffLines(a.text, b.text);
  const parts = [];
  let same = 0;
  for (const [op, line] of ops) {
    if (op === "=") {
      same++;
      if (same <= 3) parts.push(`<div class="d-same">${esc(line) || "&nbsp;"}</div>`);
      continue;
    }
    if (same > 3) {
      parts.push(`<div class="d-skip">&middot; &middot; &middot; ${same - 3} more unchanged lines</div>`);
    }
    same = 0;
    if (op === "-") parts.push(`<div class="d-del">- ${esc(line)}</div>`);
    else if (op === "+") parts.push(`<div class="d-add">+ ${esc(line)}</div>`);
    else parts.push(`<div class="d-skip">${esc(line)}</div>`);
  }
  if (same > 3)
    parts.push(`<div class="d-skip">&middot; &middot; &middot; ${same - 3} more unchanged lines</div>`);
  box.innerHTML = parts.join("") || `<p class="empty">No differences</p>`;
}

// the digit->engine staging legend shown above the page view — the core
// staging gesture is otherwise only documented in Settings > OCR
function buildOcrKeymapLegend() {
  const host = el("ocr-keymap");
  if (!host) return;
  const map = state.settings.ocrKeyMap || {};
  const SHORT = { tesseract: "Tesseract", mistral: "Mistral", claude: "Claude",
                  textract: "Textract", azure: "Azure", openai: "OpenAI" };
  const parts = [];
  for (const k of ["1", "2", "3", "4", "5"]) {
    if (map[k]) parts.push(`<b>${k}</b> ${esc(SHORT[map[k]] || map[k])}`);
  }
  host.innerHTML = parts.length
    ? `Hover a page and press a digit to stage its engine — ${parts.join(" · ")}` +
      ` · <b>T</b> title page`
    : "";
}

function setOcrView(v) {
  ocrSyncEditor();
  ocrState.view = v;
  el("ocr-view-edit").classList.toggle("active", v === "edit");
  el("ocr-view-diff").classList.toggle("active", v === "diff");
  el("ocr-view-pdf").classList.toggle("active", v === "pdf");
  el("ocr-editor").hidden = v !== "edit";
  el("ocr-diff").hidden = v !== "diff";
  el("ocr-pages").hidden = v !== "pdf";
  el("ocr-layout").hidden = v !== "pdf";       // layout is a mode of the page view
  el("ocr-furniture").hidden = v !== "pdf";    // …as is the furniture toggle
  el("ocr-pagenav").hidden = v !== "pdf";      // page jump/nav is page-view only
  el("ocr-keymap").hidden = v !== "pdf";       // digit->engine legend, page-view only
  if (v === "pdf") buildOcrKeymapLegend();
  el("ocr-layout").classList.toggle("active", ocrState.layout);
  el("ocr-furniture").classList.toggle("active", !!state.settings.ocrFurniture);
  el("ocr-pages").classList.toggle("ocr-hidefurn", !state.settings.ocrFurniture);
  if (v === "diff") renderOcrDiff();
  else if (v === "pdf") renderOcrPages();
  else {
    const d = ocrSelDoc();
    el("ocr-editor").value = d ? d.text : "";
  }
}

function setOcrLayout(on) {
  ocrSyncEditor();          // don't lose pending edits when the textareas go away
  ocrState.layout = !!on;
  state.settings.ocrLayout = ocrState.layout;
  saveSettings();
  el("ocr-layout").classList.toggle("active", ocrState.layout);
  if (ocrState.view === "pdf") renderOcrPages();
}

// show/hide page furniture (marginalia, running heads, catchwords…) in the
// region facsimile — a CSS class flip, no re-render; the regions stay mounted
function setOcrFurniture(on) {
  state.settings.ocrFurniture = !!on;
  saveSettings();
  el("ocr-furniture").classList.toggle("active", !!on);
  el("ocr-pages").classList.toggle("ocr-hidefurn", !on);
}

// --- side-by-side page view: PDF page images next to that page's OCR text --
// One scroll container holds a row per page (image | text), so the PDF and
// the OCR text scroll together and each page's text box is stretched to the
// height of its page image.

const PAGE_MARK_RE = /^--- page (\d+) ---$/gm;

// Split OCR text into per-page sections by the "--- page N ---" markers
// that live extraction writes; null when the text has no markers. The
// structure is LOSSLESS: any preamble before the first marker and the
// original page numbers are preserved, so reassembly never drops pages
// beyond what the view shows, never renumbers, and never loses the head.
function ocrPageSections(text) {
  PAGE_MARK_RE.lastIndex = 0;
  text = String(text || "");
  const marks = [...text.matchAll(PAGE_MARK_RE)];
  if (!marks.length) return null;
  const map = new Map();
  for (let i = 0; i < marks.length; i++) {
    const m = marks[i];
    const from = m.index + m[0].length;
    const to = i + 1 < marks.length ? marks[i + 1].index : text.length;
    map.set(parseInt(m[1], 10), text.slice(from, to).replace(/^\n/, "").replace(/\n+$/, ""));
  }
  return { pre: text.slice(0, marks[0].index).replace(/\n+$/, ""), map };
}

function ocrPagesToText(sec) {
  const parts = [];
  if (sec.pre) parts.push(sec.pre);
  for (const n of [...sec.map.keys()].sort((a, b) => a - b)) {
    parts.push(`--- page ${n} ---\n${sec.map.get(n)}`);
  }
  return parts.join("\n\n");
}

// pending debounced page-view sync — bound to ITS doc and section
// structure, flushable before any doc/view switch or save
let ocrPageSync = null;

function flushOcrPageSync() {
  if (!ocrPageSync) return;
  clearTimeout(ocrPageSync.t);
  const run = ocrPageSync.run;
  ocrPageSync = null;
  run();
}

async function renderOcrPages() {
  const box = el("ocr-pages");
  // Rebuilding while the page view is display:none (another tab, or a job
  // finishing in the background) reads scrollTop as 0 and destroys the real
  // position. Skip — returning to the tab re-renders via renderOcrTab with
  // the box visible and its scroll offset intact.
  if (box.offsetParent === null) return;
  const d = ocrSelDoc();
  if (!d) { box.innerHTML = `<p class="empty">Select a book on the left to view and correct its OCR.</p>`; ocrState.pagesPdf = ""; return; }
  const pdf = docPdf(d);   // the doc's OWN source: a secondary scan's OCR
                           // renders beside the secondary PDF's pages
  // a view swap to ANOTHER pdf invalidates the page selection — its page
  // numbers pointed at the previous scan (Delete would hit the wrong file)
  if (ocrState.pagesPdf !== pdf && ocrState.pageSel.size) clearOcrPageSel();
  if (!pdf) {
    box.innerHTML = `<p class="empty">No PDF for this document — attach one in the Editor tab</p>`;
    ocrState.pagesPdf = "";
    return;
  }
  // A rebuild of the SAME pdf (layout toggle, doc switch, replace-all, job
  // merge) must never throw the reader back to the top: each page's box is
  // reserved from info.dims below, so restoring scrollTop lands exactly.
  const keepTop = ocrState.pagesPdf === pdf ? box.scrollTop : 0;
  let info = ocrState.pdfInfo[pdf];
  if (!info) {
    box.innerHTML = `<p class="empty">Loading pages &hellip;</p>`;
    try {
      const r = await (await fetch("/api/pdf/info?path=" + encodeURIComponent(pdf))).json();
      if (r.ok) { info = r; ocrState.pdfInfo[pdf] = r; }
    } catch (e) { /* handled below */ }
    if (ocrSelDoc() !== d || ocrState.view !== "pdf") return;   // switched away
  }
  const count = info ? info.pages : 0;
  if (!count) {
    box.innerHTML = `<p class="empty">Could not read the PDF (${esc(pdf)})</p>`;
    ocrState.pagesPdf = "";
    return;
  }
  const sections = ocrPageSections(d.text);
  // Every page is reachable — no fixed cap. The images window in via
  // observePageImgs (only near-viewport pages fetch), so building all rows up
  // front is DOM-only and cheap: ~60ms at 1000 pages, ~150ms at 2000 (measured),
  // against a silent 400-page truncation before.
  const shown = count;
  ocrState.pages = null;
  // reserving each page's true shape up front keeps lazy image loads from
  // shifting the content under the reader
  const dims = info.dims || [];
  const ar = (n) => {
    const dd = dims[n - 1];
    return dd && dd[0] > 0 && dd[1] > 0 ? `aspect-ratio:${dd[0]} / ${dd[1]};` : "";
  };
  const img = (n) => `<img decoding="async" alt="page ${n}" style="${ar(n)}"
      data-thumb="/api/pdf/pageimg?path=${encodeURIComponent(pdf)}&page=${n}&w=200"
      data-src="/api/pdf/pageimg?path=${encodeURIComponent(pdf)}&page=${n}&w=700" />`;
  const done = () => {
    ocrState.pagesPdf = pdf;
    ocrState.pagesSrc = d.buildId ? docSrcKey(d) : "primary";
    box.scrollTop = keepTop;   // 0 on a pdf switch: no scroll bleed-through
    decorateOcrPages();
    observePageImgs(box);
    el("ocr-page-total").textContent = "/ " + ocrPageRows().length;
    ocrSyncPageInput(ocrTopPage());
  };
  // Layout mode swaps each editable textarea for a facsimile pane: the page's
  // own words, at the position and scale they occupy on the page. Boxes are
  // fetched per page as it scrolls into view -- a 400-page book must not fire
  // 400 requests up front.
  if (ocrState.layout) {
    box.innerHTML =
      (count > shown ? `<div class="ocr-pgnote empty">Showing the first ${shown} of ${count} pages</div>` : "") +
      Array.from({ length: shown }, (_, i) => `
      <div class="ocr-pgrow" data-page="${i + 1}">
        <div class="ocr-pgimg">${img(i + 1)}</div>
        <div class="ocr-pglayout" data-lay="${i + 1}" style="${ar(i + 1)}"></div>
      </div>`).join("");
    if (sections) ocrState.pages = sections;
    observeOcrLayout(pdf);
    done();
    return;
  }
  if (!sections) {
    // no page markers: the whole text sits beside the first page
    box.innerHTML = `
      <div class="ocr-pgnote empty">This OCR file has no page markers — showing the full text beside page 1</div>
      <div class="ocr-pgrow" data-page="1">
        <div class="ocr-pgimg">${img(1)}</div>
        <textarea class="ocr-pgtext cad-input" data-whole="1" spellcheck="false"></textarea>
      </div>` +
      Array.from({ length: Math.min(shown, 30) - 1 }, (_, i) => `
      <div class="ocr-pgrow" data-page="${i + 2}">
        <div class="ocr-pgimg">${img(i + 2)}</div>
        <textarea class="ocr-pgtext cad-input" spellcheck="false" disabled></textarea>
      </div>`).join("");
    box.querySelector("[data-whole]").value = d.text;
    done();   // title-page chips apply here too
    return;
  }
  ocrState.pages = sections;
  const beyond = [...sections.map.keys()].filter((n) => n > shown).length;
  box.innerHTML =
    (count > shown
      ? `<div class="ocr-pgnote empty">Showing the first ${shown} of ${count} pages` +
        (beyond ? ` — the ${beyond} section(s) beyond stay untouched` : "") + `</div>` : "") +
    Array.from({ length: shown }, (_, i) => `
      <div class="ocr-pgrow" data-page="${i + 1}">
        <div class="ocr-pgimg">${img(i + 1)}</div>
        <textarea class="ocr-pgtext cad-input" data-pn="${i + 1}" spellcheck="false"></textarea>
      </div>`).join("");
  box.querySelectorAll("textarea[data-pn]").forEach((ta) => {
    ta.value = sections.map.get(+ta.dataset.pn) || "";
  });
  done();
}

// --- layout mode: the page's own words, where they sit on the page -----------
// The coordinates come from the PDF's embedded text layer (the same ones the
// browser's viewer uses to draw a selection), read server-side with PyMuPDF and
// normalised to 0..1, so they survive any render width.

// panes fill lazily as they scroll into view — a 400-page book must not
// fire 400 fetches up front. Shared by the OCR tab and the pdf viewer
// (each holds its own observer instance).
function makeLayoutObserver(rootEl, fill) {
  const obs = new IntersectionObserver((entries) => {
    for (const e of entries) {
      if (!e.isIntersecting) continue;
      obs.unobserve(e.target);
      fill(e.target).catch(() => { /* page left the view */ });
    }
  }, { root: rootEl, rootMargin: "300px" });
  for (const pane of rootEl.querySelectorAll(".ocr-pglayout")) {
    obs.observe(pane);
  }
  return obs;
}

// Page images load only near the viewport, and — crucially — an image scrolled
// away before it finished loading is aborted (its src removed) so it stops
// holding one of the browser's ~6 per-origin connections. Native loading="lazy"
// never cancels, so a fast scroll fires a request per page in DOM order with no
// way to jump the queue: the page you land on waits behind every page you flew
// past, and the ones at the bottom take forever. Finished images are kept (no
// reload flicker on a small scroll-back); in-flight ones are dropped and reload
// when they return. Rows and their textareas always stay mounted, so saving and
// decorateOcrPages still see every page.
function observePageImgs(container) {
  if (container._imgObs) container._imgObs.disconnect();
  const obs = new IntersectionObserver((entries) => {
    for (const e of entries) {
      const im = e.target;
      if (e.isIntersecting) {
        // a low-res thumbnail sits behind the img as a blur-up placeholder, so
        // the row is never a blank box while the full render arrives
        const box = im.parentElement;   // .ocr-pgimg
        if (box && im.dataset.thumb && !box.style.backgroundImage) {
          box.style.backgroundImage = `url("${im.dataset.thumb}")`;
        }
        if (im.dataset.src && !im.getAttribute("src")) im.src = im.dataset.src;
      } else if (!im.complete && im.getAttribute("src")) {
        im.removeAttribute("src");   // abort the in-flight load, free the slot
      }
    }
  }, { root: container, rootMargin: "1000px 0px" });
  for (const im of container.querySelectorAll(".ocr-pgimg img[data-src]")) {
    obs.observe(im);
  }
  container._imgObs = obs;
  return obs;
}

let ocrLayoutObs = null;

function observeOcrLayout(pdf) {
  if (ocrLayoutObs) ocrLayoutObs.disconnect();
  ocrLayoutObs = makeLayoutObserver(el("ocr-pages"),
    (pane) => fillOcrLayout(pane, pdf));
}

// the OCR sidecar for a book (ocr/layout.json), fetched once: the extracted-
// figure boxes AND, per source, the pages that carry OCR word boxes (so Layout
// knows which pages to place as a facsimile rather than flow as text)
async function ocrLayoutMeta(bid) {
  if (!bid) return { images: {}, wordPages: {} };
  if (!ocrState.layoutMeta[bid]) {
    try {
      const r = await (await fetch(
        `/api/builds/${encodeURIComponent(bid)}/ocr-layout`)).json();
      ocrState.layoutMeta[bid] = r.ok
        ? { images: r.images || {}, wordPages: r.word_pages || {},
            wordDocs: r.word_docs || {}, regionPages: r.region_pages || {},
            regionStates: r.region_states || {} }
        : { images: {}, wordPages: {}, wordDocs: {}, regionPages: {}, regionStates: {} };
    } catch (e) {
      ocrState.layoutMeta[bid] = { images: {}, wordPages: {}, wordDocs: {},
                                   regionPages: {}, regionStates: {} };
    }
  }
  return ocrState.layoutMeta[bid];
}

// does the selected source have OCR word boxes for this page?
function ocrHasWords(meta, srcKey, page) {
  const wp = meta && meta.wordPages && meta.wordPages[srcKey || "primary"];
  return Array.isArray(wp) && wp.includes(page);
}

// ...and do those boxes belong to the doc being viewed? The boxes' `t` values
// are the transcription of the run that produced them (word_docs); showing
// them under a DIFFERENT doc would place stale text over that doc's fresh
// content, so the mismatch flows the doc's own text instead. A sidecar from
// before word_docs existed has no record — treat as matching.
function ocrWordsAreDocs(meta, srcKey, page, docName) {
  const dm = meta && meta.wordDocs && meta.wordDocs[srcKey || "primary"];
  const rec = dm && dm[String(page)];
  return !rec || String(rec).toLowerCase() === String(docName || "").toLowerCase();
}

// does the selected source have a typed-region record for this page?
function ocrHasRegions(meta, srcKey, page) {
  const rp = meta && meta.regionPages && meta.regionPages[srcKey || "primary"];
  return Array.isArray(rp) && rp.includes(page);
}

// one page's typed regions (/ocr-regions), cached like the word boxes
async function ocrPageRegions(bid, srcKey, page) {
  const ck = `${bid}|${srcKey || "primary"}|${page}`;
  let rec = ocrState.regionsCache.get(ck);
  if (!rec) {
    try {
      rec = await (await fetch(
        `/api/builds/${encodeURIComponent(bid)}/ocr-regions` +
        `?src=${encodeURIComponent(srcKey || "primary")}&page=${page}`)).json();
    } catch (e) { rec = { ok: false }; }
    if (rec && rec.ok) {
      if (ocrState.regionsCache.size > 500) ocrState.regionsCache.clear();
      ocrState.regionsCache.set(ck, rec);
    }
  }
  return rec;
}

// mirror of the server's SECONDARY_ROLES: page furniture the reading-focus
// toggle can hide (the facsimile's point is showing it IN place, so default on)
const OCR_FURNITURE_ROLES = new Set(["marginalia", "header", "footer",
  "page-number", "catch-word", "signature-mark"]);

// The region facsimile: each typed region (Mistral OCR-4 blocks, role-
// classified server-side) becomes a positioned box with its text flowed
// inside — paragraph grain, so margin notes sit IN the margin instead of
// polluting the body flow. Placed only when the record carries the doc being
// viewed (the words_doc staleness contract); returns false so the caller can
// flow the doc's own text instead. `still` re-checks the caller's context
// after the fetch: a same-book doc switch keeps the panes MOUNTED
// (refillOcrPageText), so isConnected alone can't stop a late fill from
// painting the previous doc's regions over the new doc's page.
async function fillRegionLayout(pane, bid, srcKey, page, docName, still) {
  const rec = await ocrPageRegions(bid, srcKey, page);
  if (!pane.isConnected || (still && !still())) {
    return true;   // context moved on: its own fill pass owns this pane now
  }
  if (!rec || !rec.ok || !rec.found || !(rec.items || []).length) return false;
  if (rec.doc &&
      String(rec.doc).toLowerCase() !== String(docName || "").toLowerCase()) {
    return false;
  }
  const dims = rec.dims || {};
  pane.classList.remove("doctext", "empty");
  pane.textContent = "";
  if (dims.w > 0 && dims.h > 0) pane.style.aspectRatio = `${dims.w} / ${dims.h}`;
  const paneW = pane.clientWidth;
  const paneH = pane.clientHeight ||
    (dims.w > 0 ? paneW * dims.h / dims.w : paneW * 1.4);
  const frag = document.createDocumentFragment();
  for (const it of rec.items || []) {
    const box = it.box || {};
    const e = document.createElement("div");
    e.className = "ocr-region";
    e.dataset.role = it.role || "body";
    e.title = it.role || "";
    if (OCR_FURNITURE_ROLES.has(it.role)) e.dataset.furniture = "1";
    e.style.left = (box.x || 0) * 100 + "%";
    e.style.top = (box.y || 0) * 100 + "%";
    e.style.width = (box.w || 0) * 100 + "%";
    e.style.height = (box.h || 0) * 100 + "%";
    const text = String(it.text || "");
    const fig = it.role === "figure" &&
      text.match(/!\[[^\]\n]*\]\(([\w.\- ]+)\)/);
    if (fig) {
      const img = document.createElement("img");
      img.className = "ocr-regimg";
      img.loading = "lazy";
      img.decoding = "async";
      img.alt = fig[1];
      img.src = `/api/builds/${encodeURIComponent(bid)}` +
        `/ocr/images/${encodeURIComponent(fig[1])}`;
      e.appendChild(img);
    } else {
      // one type size per block, chosen so its lines roughly fill the box —
      // the block-grain analogue of the word facsimile's per-line sizing
      const lines = Math.max(1, text.split("\n").length);
      const size = Math.max(6, Math.min(
        ((box.h || 0) * paneH) / lines * 0.78, paneW * 0.05));
      e.style.fontSize = size + "px";
      e.textContent = text;
    }
    frag.appendChild(e);
  }
  pane.appendChild(frag);
  return true;
}

// Markdown-lite for one page of OCR output (Mistral emits markdown): headers,
// paragraphs, and the extracted figures back at their place in the page flow,
// each scaled to the width fraction it occupied on the printed page.
function ocrMarkdownHtml(text, bid, meta) {
  let h = esc(text);
  h = h.replace(/!\[[^\]\n]*\]\(([\w.\- ]+)\)/g, (m, src) => {
    const box = (meta || {})[src];
    const style = box && box.w ? ` style="width:${Math.min(100, box.w * 100).toFixed(1)}%"` : "";
    return `<img class="ocr-layimg" loading="lazy" decoding="async" alt="${src}"${style} src="/api/builds/${encodeURIComponent(bid)}/ocr/images/${encodeURIComponent(src)}">`;
  });
  return h.split(/\n{2,}/).map((par) => {
    const mh = par.match(/^(#{1,6})\s+([\s\S]+)$/);
    if (mh) return `<div class="ocr-mdh h${mh[1].length}">${mh[2]}</div>`;
    return `<p>${par.replace(/\n/g, "<br>")}</p>`;
  }).join("");
}

// OCR output (compiled.txt and friends): the page's text flowed into the
// page-shaped pane with its figures inline. Shared by the OCR tab and the
// pdf viewer; `text` may be null (no section for this page).
function fillDocLayout(pane, text, bid, meta) {
  pane.classList.remove("doctext", "empty");
  if (text == null || !text.trim()) {
    pane.classList.add("empty");
    pane.textContent = "No text for this page in this document — OCR it";
    return;
  }
  pane.classList.add("doctext");
  pane.innerHTML = ocrMarkdownHtml(text, bid, meta);
}

async function fillOcrLayout(pane, pdf) {
  const page = +pane.dataset.lay;
  const d = ocrSelDoc();
  // The PDF's own text-layer extraction always shows the placed facsimile. Any
  // other doc (an OCR result) shows ITS content, so switching docs swaps what
  // the page holds: the doc whose run produced the sidecar's word boxes
  // (Tesseract/Textract, incl. an image-only scan) places a facsimile; every
  // other doc — a text-only result (Claude/Mistral), or a different target —
  // flows its own text into the page. Boxes persist across text-only re-OCRs
  // (they still fit the page image), so the doc match is what keeps a fresh
  // transcription from being shadowed by the box run's older text.
  if (d && d.buildId && ocrState.pages &&
      (d.fileName || d.name) !== srcExtractedName(docSrcKey(d))) {
    const meta = await ocrLayoutMeta(d.buildId);
    if (!pane.isConnected || ocrSelDoc() !== d) return;
    if (ocrHasWords(meta, docSrcKey(d), page) &&
        ocrWordsAreDocs(meta, docSrcKey(d), page, d.fileName || d.name)) {
      return fillWordLayout(pane, pdf, page, d.buildId);
    }
    // no word boxes for this doc: a typed-region record (Mistral blocks)
    // gives a paragraph-grain facsimile; failing that, flow the text
    if (ocrHasRegions(meta, docSrcKey(d), page)) {
      if (await fillRegionLayout(pane, d.buildId, docSrcKey(d), page,
                                 d.fileName || d.name,
                                 () => ocrSelDoc() === d)) return;
      if (!pane.isConnected || ocrSelDoc() !== d) return;
    }
    const sec = ocrState.pages;
    const text = sec && sec.map.has(page) ? sec.map.get(page) : null;
    fillDocLayout(pane, text, d.buildId, meta.images);
    return;
  }
  return fillWordLayout(pane, pdf, page, d && d.buildId);
}

// the word-box facsimile of one page: the PDF's own text layer, or (with a
// buildId, for a scan that has no text layer) this book's stored OCR boxes —
// shared by the OCR tab and the pdf viewer
async function fillWordLayout(pane, pdf, page, buildId) {
  pane.classList.remove("doctext");
  pane.textContent = "…";
  const ck = `${pdf}|${page}|${buildId || ""}`;
  let res = ocrState.wordsCache.get(ck);
  if (!res) {
    let url = `/api/pdf/words?path=${encodeURIComponent(pdf)}&page=${page}`;
    if (buildId) url += `&build_id=${encodeURIComponent(buildId)}`;
    res = await (await fetch(url)).json();
    if (res && res.ok) {
      if (ocrState.wordsCache.size > 500) ocrState.wordsCache.clear();
      ocrState.wordsCache.set(ck, res);
    }
  }
  if (!pane.isConnected) return;
  if (!res.ok || !res.found) {
    pane.classList.add("empty");
    pane.textContent = res.ok
      ? "No text layer on this page — OCR it"
      : (res.error || "Could not read the page");
    return;
  }
  pane.classList.remove("empty");
  pane.textContent = "";
  pane.style.aspectRatio = `${res.page_w} / ${res.page_h}`;
  // read the pane's box once: touching clientWidth per word would force a
  // layout per word, and a dense page carries ~400 of them
  const paneW = pane.clientWidth;
  const paneH = pane.clientHeight || (paneW * res.page_h / res.page_w);
  const frag = document.createDocumentFragment();
  const placed = [];
  for (const line of res.lines) {
    const size = Math.max(1, line.s * paneH);   // one type size for the line
    for (const sp of line.spans) {
      const e = document.createElement("span");
      e.className = "ocr-word";
      e.textContent = sp.t;
      e.style.left = sp.x * 100 + "%";
      e.style.top = line.y * 100 + "%";         // the line's baseline
      e.style.fontSize = size + "px";
      frag.appendChild(e);
      placed.push([e, sp.w * paneW]);
    }
  }
  pane.appendChild(frag);
  // Squeeze each span to the width of its box -- the screen font is never the
  // page's font, so glyph widths never agree. Read every width first, THEN
  // write every transform: interleaving them would thrash layout once per span.
  // The lift is an approximate ascent, since `top` is the baseline.
  const widths = placed.map(([e]) => e.getBoundingClientRect().width);
  placed.forEach(([e, want], i) => {
    const k = widths[i] > 0.5 && want > 0.5 ? want / widths[i] : 1;
    e.style.transform = `translateY(-0.79em) scaleX(${k})`;
  });
}

// Swap only the OCR text alongside the already-rendered page images — used when
// selecting another document of the SAME book in the page view, so the PDF page
// images stay loaded instead of being torn down and re-fetched. Returns false
// if the on-screen structure (paged vs. whole) doesn't match the new doc, so
// the caller can fall back to a full re-render.
function refillOcrPageText(d) {
  const box = el("ocr-pages");
  // a doc from ANOTHER PDF source shows different page images — refill
  // can't help there, the caller must rebuild against the doc's own PDF
  if (ocrState.pagesPdf && docPdf(d) !== ocrState.pagesPdf) return false;
  // layout mode: the panes stay mounted (and the page images loaded), but the
  // facsimiles must show the NEW doc's text — re-observe every pane, so the
  // visible ones refill immediately and the rest as they scroll into view
  if (ocrState.layout) {
    if (!box.querySelector(".ocr-pglayout")) return false;
    ocrState.pages = ocrPageSections(d.text);
    observeOcrLayout(docPdf(d));
    decorateOcrPages();
    return true;
  }
  const paged = box.querySelectorAll("textarea[data-pn]");
  const whole = box.querySelector("textarea[data-whole]");
  const sections = ocrPageSections(d.text);
  if (sections && paged.length) {
    ocrState.pages = sections;
    paged.forEach((ta) => { ta.value = sections.map.get(+ta.dataset.pn) || ""; });
    decorateOcrPages();
    return true;
  }
  if (!sections && whole) {
    ocrState.pages = null;
    whole.value = d.text;
    decorateOcrPages();
    return true;
  }
  return false;
}

// edits in the page view flow back into the document text (debounced; the
// pending write is bound to this doc + section structure, so a late fire
// after switching documents can never touch the wrong one)
function onOcrPageInput(ev) {
  const ta = ev.target.closest("textarea.ocr-pgtext");
  if (!ta) return;
  const d = ocrSelDoc();
  if (!d) return;
  if (ta.dataset.whole) { d.text = ta.value; return; }
  const sec = ocrState.pages;
  if (!sec) return;
  sec.map.set(+ta.dataset.pn, ta.value);
  if (ocrPageSync) clearTimeout(ocrPageSync.t);
  const run = () => { d.text = ocrPagesToText(sec); };
  ocrPageSync = { t: setTimeout(() => { ocrPageSync = null; run(); }, 250), run };
}

// --- unified job registry (server /api/jobs) ------------------------------------
// One poller for every background kind: the server registry is authoritative —
// it survives restarts (`interrupted` entries) and carries OCR + text analysis
// + publish plus read-only download/sync rows. The legacy per-kind pollers
// stay for their side effects (page merges, badges, button re-enables); the
// queue table itself renders from here.
const JOB_ACTIVE_STATES = ["queued", "running", "cancelling"];
const jobsState = { rows: [], active: 0 };

function jobTypeLabel(kind) {
  if (kind === "ocr") return "OCR";
  if (kind === "publish") return "Publish";
  if (kind === "download") return "Download";
  if (kind === "cloudsync") return "Cloud sync";
  if (kind === "segment") return "Passages";
  if (kind === "index-publish") return "Search index";
  return "Text analysis";
}

function jobStatusText(r) {
  if (r.state === "queued") return "Queued";
  if (r.state === "running") {
    if ((r.kind === "download" || r.kind === "publish") && r.total) {
      return `${r.note || "Running"} — ` +
        `${Math.round(((r.done || 0) / r.total) * 100)}%`;
    }
    return r.total ? `Running — ${r.done || 0}/${r.total}`
      : (r.note || "Running");
  }
  if (r.state === "cancelling") return "Cancelling…";
  if (r.state === "cancelled") return `Cancelled${r.note ? ` — ${r.note}` : ""}`;
  if (r.state === "failed") return `Error — ${r.error || r.note || "failed"}`;
  if (r.state === "interrupted") {
    return `Interrupted${r.note ? ` — ${r.note}` : ""}`;
  }
  return (r.status || r.state || "") +
    (r.errors ? ` — ${r.errors} errors` : "");
}

async function pollJobs() {
  if (document.hidden) return;
  let data;
  try {
    data = await (await fetch("/api/jobs")).json();
  } catch (e) { return; }
  if (!data.ok) return;
  jobsState.rows = data.jobs || [];
  jobsState.active = data.active || 0;
  renderJobsFooter();
  const tab = document.querySelector("#tabs .tab.active");
  if (tab && tab.dataset.tab === "workbench") renderOcrQueue();
}

// The jobs drawer's one-line summary, plus the footer's quiet "N jobs running"
// marker when work is active out of sight of the Workbench; clicking the
// marker opens the drawer there.
function renderJobsFooter() {
  const line = jobsState.active
    ? `${jobsState.active} job${jobsState.active === 1 ? "" : "s"} running`
    : "";
  const summary = el("wb-jobs-summary");
  if (summary) summary.textContent = line || "No jobs running";
  const n = el("status-jobs");
  if (!n) return;
  const tab = document.querySelector("#tabs .tab.active");
  const away = !tab || tab.dataset.tab !== "workbench";
  n.hidden = !(jobsState.active && away);
  if (!n.hidden) n.textContent = line;
}

function initJobs() {
  const n = el("status-jobs");
  if (n) {
    n.addEventListener("click", () => {
      const tab = document.querySelector('#tabs .tab[data-tab="workbench"]');
      if (tab) tab.click();
      setJobsDrawer(true, false);   // land with the queue in view
    });
  }
  pollJobs();
  setInterval(pollJobs, 2000);
}

async function cancelJob(jobId) {
  try {
    const res = await fetch(`/api/jobs/${encodeURIComponent(jobId)}/cancel`, {
      method: "POST",
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || !data.ok) throw new Error(data.error || `HTTP ${res.status}`);
    const row = jobsState.rows.find((r) => r.id === jobId);
    if (row) row.state = "cancelling";
    // the OCR page loop keeps its own flag; mirror it so badges update fast
    const j = ocrState.jobs.find((x) => x.id === jobId);
    if (j) j.cancelling = true;
    status("JOB CANCELLATION REQUESTED");
    renderOcrQueue();
  } catch (e) {
    statusErr(`JOB STOP FAILED :: ${e.message || e}`);
  }
}

function renderOcrQueue() {
  const tbody = el("ocr-queue-rows");
  tbody.innerHTML = "";
  // Server registry rows first, enriched with the richer presentation this
  // session already has for its own jobs (book/artifact/engine columns).
  const localOcr = new Map(
    ocrState.jobs.filter((j) => j.id).map((j) => [j.id, j]));
  const seen = new Set();
  for (const r of jobsState.rows) {
    if (r.id) seen.add(r.id);
    const m = (r.id && (localOcr.get(r.id) || anJobs.get(r.id))) || null;
    const active = JOB_ACTIVE_STATES.includes(r.state);
    const stop = r.id && active && r.kind !== "download" && r.kind !== "cloudsync"
      ? `<button class="cad-btn tiny danger" type="button" data-job-cancel="${esc(r.id)}"` +
        `${r.state === "cancelling" ? " disabled" : ""}>` +
        `${r.state === "cancelling" ? "Stopping…" : "Stop"}</button>`
      : "";
    const book = (m && m.book) || r.label || r.build_id || "";
    const artifact = m
      ? (m.target || m.artifact || (m.pages && m.pages.length
        ? `Pages ${m.pages.join(", ")}` : m.kind || r.kind))
      : r.kind;
    const engine = m
      ? (m.service || TEXT_ANALYSIS_LABELS[m.engine] || m.engine || "") : "";
    const at = (m && m.at) ||
      (r.created_at ? new Date(r.created_at).toLocaleTimeString() : "");
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${esc(jobTypeLabel(r.kind))}</td><td>${esc(book)}</td>` +
      `<td>${esc(artifact)}</td><td>${esc(engine)}</td>` +
      `<td>${esc(jobStatusText(r))}</td><td>${esc(at)}</td><td>${stop}</td>`;
    tbody.appendChild(tr);
  }
  // Session-local rows the registry doesn't know: engine stubs (never got a
  // server job) and just-submitted jobs whose first /api/jobs poll is pending.
  for (const j of ocrState.jobs) {
    if (j.id && seen.has(j.id)) continue;
    const tr = document.createElement("tr");
    const stop = j.id && !j.finished
      ? `<button class="cad-btn tiny danger" type="button" data-ocr-cancel="${esc(j.id)}"` +
        `${j.cancelling ? " disabled" : ""}>${j.cancelling ? "Stopping…" : "Stop"}</button>`
      : "";
    const artifact = j.target || String(j.pdf || "").replace(/\\/g, "/").split("/").pop();
    tr.innerHTML = `<td>OCR</td><td>${esc(j.book)}</td>` +
      `<td data-tip="${esc(j.pdf)}">${esc(artifact)}</td>` +
      `<td>${esc(j.service)}</td><td>${esc(j.status)}</td><td>${esc(j.at)}</td><td>${stop}</td>`;
    tbody.appendChild(tr);
  }
  for (const [id, j] of anJobs.entries()) {
    if (seen.has(id)) continue;
    const tr = document.createElement("tr");
    tr.dataset.analysisJob = id;
    const artifact = j.artifact || (j.pages && j.pages.length
      ? `Pages ${j.pages.join(", ")}` : j.kind);
    tr.innerHTML = `<td>Text analysis</td><td>${esc(j.book || j.buildId)}</td>` +
      `<td>${esc(artifact)}</td>` +
      `<td>${esc(TEXT_ANALYSIS_LABELS[j.engine] || j.engine || "Configured AI")}</td>` +
      `<td>${esc(j.status || "Starting...")}</td><td>${esc(j.at || "")}</td><td></td>`;
    tbody.appendChild(tr);
  }
  const total = tbody.children.length;
  el("ocr-queue-empty").hidden = total !== 0;
  const staged = (ocrState.book ? stagedCountFor(ocrState.book) : 0) +
    (ocrState.book ? analysisStagedCount(ocrState.book) : 0);
  el("ocr-queue-count").textContent =
    `${total} job${total === 1 ? "" : "s"}${staged ? ` · ${staged} staged` : ""}`;
}

async function cancelOcrJob(jobId) {
  const j = ocrState.jobs.find((x) => x.id === jobId && !x.finished);
  if (!j || j.cancelling) return;
  j.cancelling = true;
  j.status = "Cancelling — finishing the current page…";
  renderOcrQueue();
  try {
    const res = await fetch(`/api/ocr/job/${encodeURIComponent(jobId)}/cancel`, {
      method: "POST",
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || !data.ok) throw new Error(data.error || `HTTP ${res.status}`);
    status("OCR CANCELLATION REQUESTED");
  } catch (e) {
    j.cancelling = false;
    j.status = `Could not stop — ${e.message || e}`;
    statusErr(`OCR STOP FAILED :: ${e.message || e}`);
    renderOcrQueue();
  }
}

function renderOcrTab() {
  renderOcrBooks();
  // a selected doc that fell out of view (book switch) yields to the first
  // visible one — BEFORE the list renders its active highlight
  const visible = ocrVisibleDocs();
  if (ocrState.sel && !visible.some((d) => d.id === ocrState.sel)) {
    ocrState.sel = visible.length ? visible[0].id : null;
  }
  renderOcrDocs();
  const sel = el("ocr-diff-with");
  const prevWith = sel.value;   // keep the chosen compare target
  sel.innerHTML = "";
  for (const d of visible) {
    if (d.id === ocrState.sel) continue;
    const o = document.createElement("option");
    o.value = d.id;
    o.textContent = d.name.slice(0, 30);
    sel.appendChild(o);
  }
  if (prevWith && [...sel.options].some((o) => o.value === prevWith)) {
    sel.value = prevWith;
  }
  const d = ocrSelDoc();
  // A re-render of the doc already on screen must not throw the reader back to
  // the top -- this fires from the OCR-job poller, mid-read. Only a switch to a
  // different document legitimately resets the scroll.
  const sameDoc = !!d && d.id === ocrState.lastRendered;
  ocrState.lastRendered = d ? d.id : null;
  if (ocrState.view === "edit") {
    const ta = el("ocr-editor");
    const top = sameDoc ? ta.scrollTop : 0;   // assigning .value resets scrollTop
    ta.value = d ? d.text : "";
    ta.readOnly = !!(d && d.readOnly);
    ta.scrollTop = top;
  } else if (ocrState.view === "diff") {
    const box = el("ocr-diff");
    const top = sameDoc ? box.scrollTop : 0;
    renderOcrDiff();
    box.scrollTop = top;
  } else if (!(sameDoc && d && refillOcrPageText(d))) {
    // refillOcrPageText swaps text into the rows that are already mounted, so
    // scroll and the loaded page images survive. It declines when the page
    // structure changed (markers appeared, pages deleted) -- then rebuild.
    renderOcrPages();
  }
  // quality reflects the doc's book (when folder-sourced)
  const build = d && d.buildId ? state.builds[d.buildId] : null;
  el("ocr-quality").value = (build && build.ocr_quality) || "";
  el("ocr-quality").disabled = !!(d && d.readOnly);
  el("ocr-replace").disabled = !!(d && d.readOnly);
  el("ocr-replace-all").disabled = !!(d && d.readOnly);
  el("ocr-set-active").disabled = !(d && d.buildId) || !!d.readOnly;
  el("ocr-save").disabled = !d || !!d.readOnly;
  renderOcrQueue();
  renderAnFacsimile();
}

function ocrFindNext() {
  const needle = el("ocr-find").value;
  if (!needle) return;
  if (el("ocr-editor").hidden) setOcrView("edit");   // Find operates on the editable text
  const ta = el("ocr-editor");
  const from = ta.selectionEnd || 0;
  let i = ta.value.indexOf(needle, from);
  if (i < 0) i = ta.value.indexOf(needle);   // wrap around
  if (i < 0) { el("ocr-msg").textContent = "Not found"; return; }
  ta.focus();
  ta.setSelectionRange(i, i + needle.length);
  el("ocr-msg").textContent = "";
}

function ocrReplaceAll() {
  const d = ocrSelDoc();
  const needle = el("ocr-find").value;
  if (!d || d.readOnly || !needle) return;
  ocrSyncEditor();
  const repl = el("ocr-replace").value;
  const count = d.text.split(needle).length - 1;
  if (!count) { el("ocr-msg").textContent = "Not found"; return; }
  d.text = d.text.split(needle).join(repl);
  el("ocr-editor").value = d.text;
  if (ocrState.view === "diff") renderOcrDiff();       // keep the view current
  else if (ocrState.view === "pdf") renderOcrPages();
  el("ocr-msg").textContent = `${count} replaced (unsaved)`;
}

async function ocrSaveDoc() {
  const d = ocrSelDoc();
  if (!d || d.readOnly) return;
  ocrSyncEditor();
  if (d.buildId) {
    const res = await fetch(`/api/builds/${encodeURIComponent(d.buildId)}/ocr`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: d.fileName || d.name, text: d.text,
                             src: docSrcKey(d) }),
    });
    const data = await res.json().catch(() => ({}));
    el("ocr-msg").textContent = data.ok ? "Saved" : "Save failed";
    if (data.ok) status(`OCR SAVED :: ${data.name}`);
  } else {
    // local documents save back to disk as a download
    const blob = new Blob([d.text], { type: "text/plain" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = d.name.endsWith(".txt") ? d.name : d.name + ".txt";
    a.click();
    URL.revokeObjectURL(a.href);
    el("ocr-msg").textContent = "Downloaded";
  }
}

async function ocrSetActive() {
  const d = ocrSelDoc();
  if (!d || !d.buildId) return;
  if (await patchBuildRaw(d.buildId, { ocr_active: d.fileName || d.name }, true)) {
    el("ocr-msg").textContent = `Active :: ${d.fileName || d.name}`;
    status(`ACTIVE OCR :: ${d.fileName || d.name}`);
  }
}

async function ocrSetQuality(v) {
  const d = ocrSelDoc();
  if (!d || !d.buildId) { el("ocr-msg").textContent = "No book folder"; return; }
  if (await patchBuildRaw(d.buildId, { ocr_quality: v }, true)) {
    el("ocr-msg").textContent = v ? `Quality :: ${v}` : "Quality cleared";
  }
}

// is the chosen OCR service configured? (Settings > OCR)
// services the server can actually run today; azure/openai queue as stubs
// (their processors are TODO until credentials exist)
const OCR_RUNNABLE = { tesseract: true, mistral: true, claude: true, textract: true };
const OCR_SERVICE_LABELS = {
  tesseract: "Tesseract (local)", mistral: "Mistral OCR", claude: "Claude",
  textract: "Amazon Textract",
  azure: "Azure Document Intelligence", openai: "OpenAI vision",
};

function ocrServiceReady(svc) {
  const s = state.settings;
  if (svc === "tesseract") return true;   // server falls back to the default install
  if (svc === "mistral") return !!s.mistralKey;   // shared with the capture pipeline
  if (svc === "claude") return !!s.ocrClaudeKey;
  if (svc === "textract") return !!(s.ocrAwsKey && s.ocrAwsSecret);
  if (svc === "azure") return !!(s.ocrAzureEndpoint && s.ocrAzureKey);
  if (svc === "openai") return !!s.aiKey;         // reuses the AI credentials
  return false;
}

const TEXT_ANALYSIS_LABELS = { configured: "Configured AI provider" };

function analysisServiceReady(engine) {
  return engine === "configured" && !!String(state.settings.aiKey || "").trim();
}

function updateDefaultEngineSummary() {
  const host = el("engine-defaults-summary");
  if (!host) return;
  const ocr = OCR_SERVICE_LABELS[state.settings.ocrService] || "Tesseract (local)";
  const analysis = TEXT_ANALYSIS_LABELS[state.settings.textAnalysisService] ||
    "Configured AI provider";
  host.textContent = `${ocr} / ${analysis}`;
}

function refreshDefaultEngineOptions() {
  const ocr = el("ocr-service");
  if (!ocr) return;
  for (const option of ocr.options) {
    const ready = ocrServiceReady(option.value);
    option.disabled = !ready;
    option.title = ready ? "" : "Add this engine's API key in Settings > OCR";
  }
  ocr.value = state.settings.ocrService || "tesseract";
  const analysis = el("analysis-service");
  const ready = analysisServiceReady("configured");
  analysis.value = "configured";
  analysis.disabled = !ready;
  analysis.options[0].disabled = !ready;
  const model = String(state.settings.aiModel || "").trim() || "deepseek-chat";
  analysis.options[0].textContent = ready
    ? `Configured AI provider - ${model}`
    : `Configured AI provider - API key required`;
  el("ocr-engine-note").textContent = [...ocr.options].some((o) => o.disabled)
    ? "Cloud engines without credentials are unavailable." : "";
  el("analysis-engine-note").textContent = ready
    ? "" : "Add an AI API key in Settings > AI.";
  updateDefaultEngineSummary();
}

async function openDefaultEngines() {
  await hydrateSecrets();
  refreshDefaultEngineOptions();
  el("engine-overlay").hidden = false;
}

function closeDefaultEngines() { el("engine-overlay").hidden = true; }

function stagedAnalysisPagesFor(bid) {
  const groups = new Map();
  for (const [key, meta] of ocrState.analysisTags) {
    const [book, src, rawPage] = key.split(":");
    if (book !== bid) continue;
    const groupKey = `${src}|${meta.engine}|${meta.doc}`;
    if (!groups.has(groupKey)) groups.set(groupKey, {
      src, engine: meta.engine, doc: meta.doc, pages: [],
    });
    groups.get(groupKey).pages.push(+rawPage);
  }
  for (const group of groups.values()) group.pages.sort((a, b) => a - b);
  return groups;
}

function analysisStagedCount(bid) {
  let count = 0;
  for (const group of stagedAnalysisPagesFor(bid).values()) count += group.pages.length;
  return count;
}

async function stageSelectedAnalysisPages() {
  const d = ocrSelDoc();
  const bid = d && d.buildId;
  if (!bid || d.artifactKind || !anAnalyzable(state.builds[bid] || {})) {
    el("an-page-msg").textContent = "Select OCR data for an available verified book.";
    return;
  }
  if (!ocrState.pageSel.size) {
    el("an-page-msg").textContent = "Select one or more facsimile pages first.";
    return;
  }
  const engine = state.settings.textAnalysisService || "configured";
  if (!analysisServiceReady(engine)) await hydrateSecrets();
  if (!analysisServiceReady(engine)) {
    el("an-page-msg").textContent = "Text Analysis needs an API key.";
    await openDefaultEngines();
    return;
  }
  const src = docSrcKey(d);
  const doc = d.fileName || d.name;
  for (const page of ocrState.pageSel) {
    ocrState.analysisTags.set(`${bid}:${src}:${page}`, { engine, doc });
  }
  decorateAnFacsimile();
  updateOcrStagedMsg();
  renderOcrQueue();
}

async function submitAnalysisStaged(bid) {
  const groups = stagedAnalysisPagesFor(bid);
  if (!groups.size) return { submitted: false, failed: false };
  if (!analysisServiceReady(state.settings.textAnalysisService || "configured"))
    await hydrateSecrets();
  if (!analysisServiceReady(state.settings.textAnalysisService || "configured")) {
    el("an-page-msg").textContent = "Text Analysis needs an API key.";
    return { submitted: false, failed: true };
  }
  let submitted = false;
  let failed = false;
  for (const group of groups.values()) {
    const data = await anStartJob("/api/analyze/pages", {
      build_id: bid, pages: group.pages, doc: group.doc,
      engine: group.engine, src: group.src,
    }, "page analysis", null);
    if (!data) { failed = true; continue; }
    submitted = true;
    for (const page of group.pages)
      ocrState.analysisTags.delete(`${bid}:${group.src}:${page}`);
  }
  decorateAnFacsimile();
  return { submitted, failed };
}

// POST a page batch to the server OCR runner; results merge into ONE
// compiled OCR document per PDF source (ocr/compiled.txt for the primary,
// compiled-<src>.txt for a secondary), saved page by page. The batch runs
// against the source its pages were STAGED on — never whatever doc
// happens to be selected at submit time.
async function ocrQueuePages(bid, srcKey, pages) {
  const b = state.builds[bid];
  srcKey = srcKey || "primary";
  if (srcKey !== "primary" && !bookSources(bid).some((s) => s.key === srcKey)) {
    el("ocr-msg").textContent =
      "That PDF source was removed from the book — staging discarded";
    for (const x of pages) ocrState.pageTags.delete(`${bid}:${srcKey}:${x.page}`);
    return;
  }
  const pdf = ocrSrcPdf(bid, srcKey);
  const target = srcCompiledName(srcKey);
  if (!b || !pdf || !pages.length) return;
  const bad = pages.find((x) => !OCR_RUNNABLE[x.service]);
  if (bad) {
    // azure/openai: keep the honest stub row — no processor yet
    ocrState.jobs.push({
      book: b.title || bid, pdf,
      service: OCR_SERVICE_LABELS[bad.service] || bad.service,
      status: ocrServiceReady(bad.service)
        ? "Queued — processing not implemented yet"
        : "Queued — service not configured (Settings > OCR)",
      at: new Date().toLocaleTimeString(),
    });
    renderOcrQueue();
    return;
  }
  // Opening Settings is not a prerequisite for cloud-backed credentials.
  // Refresh on demand before deciding that a selected service is unavailable.
  if (pages.some((x) => x.service !== "tesseract" && !ocrServiceReady(x.service)))
    await hydrateSecrets();
  const missing = pages.find((x) => !ocrServiceReady(x.service));
  if (missing) {
    el("ocr-msg").textContent =
      `${OCR_SERVICE_LABELS[missing.service]} is not configured (Settings > OCR)`;
    return;
  }
  const s = state.settings;
  try {
    const res = await fetch("/api/ocr/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        build_id: bid, pdf, pages, target, src: srcKey,
        width: s.ocrImageWidth || 1400,
        tesseract: s.ocrTesseract || "",
        claude_key: s.ocrClaudeKey || "", claude_model: s.ocrClaudeModel || "",
        aws_key: s.ocrAwsKey || "", aws_secret: s.ocrAwsSecret || "",
        aws_region: s.ocrAwsRegion || "",
        mistral_key: s.mistralKey || "",
      }),
    });
    const data = await res.json().catch(() => ({}));
    if (!data.ok) {
      el("ocr-msg").textContent = data.error || "OCR queue failed";
      return;
    }
    const job = data.job;
    ocrState.jobs.push({
      id: job.id, buildId: bid, book: b.title || bid, pdf,
      target, src: srcKey,
      service: [...new Set(pages.map((x) => OCR_SERVICE_LABELS[x.service]))].join(", "),
      status: `Running — 0/${job.pages.length}`,
      at: new Date().toLocaleTimeString(),
    });
    // keyed by build AND source AND page: markers must not leak across
    // books or across scans of one book
    for (const x of pages) {
      ocrState.pageRunning.set(`${bid}:${srcKey}:${x.page}`, x.service);
    }
    decorateOcrPages();
    renderOcrQueue();
    pollOcrJobs();
    el("ocr-msg").textContent = "";
    return true;
  } catch (e) {
    el("ocr-msg").textContent = "OCR queue failed";
  }
  return false;
}

// staged tags for one book: src key -> [{page, service}], each source's
// pages sorted — one job per PDF source, so page numbers always run
// against the scan they were staged on
function stagedPagesFor(bid) {
  const bySrc = new Map();
  for (const [k, svc] of ocrState.pageTags) {
    const parts = k.split(":");
    if (parts[0] !== bid) continue;
    const src = parts[1];
    if (!bySrc.has(src)) bySrc.set(src, []);
    bySrc.get(src).push({ page: +parts[2], service: svc });
  }
  for (const list of bySrc.values()) list.sort((a, b) => a.page - b.page);
  return bySrc;
}

function stagedCountFor(bid) {
  let n = 0;
  for (const list of stagedPagesFor(bid).values()) n += list.length;
  return n;
}

function updateOcrStagedMsg() {
  const bid = ocrState.book;
  const n = bid ? stagedCountFor(bid) : 0;
  const analysis = bid ? analysisStagedCount(bid) : 0;
  const sel = ocrState.pageSel.size;
  const bits = [];
  if (sel) bits.push(`${sel} page(s) selected`);
  if (n) bits.push(`${n} staged for OCR`);
  if (analysis) bits.push(`${analysis} staged for text analysis`);
  const message = bits.join(" · ") + ((n || analysis) ? " — press Submit" : "");
  el("ocr-msg").textContent = message;
  if (el("an-page-msg")) el("an-page-msg").textContent = message;
  renderOcrQueue();
}

// SUBMIT: processing is prompted manually — the staged mix (possibly
// several services) goes out as one job PER SOURCE, each against the scan
// its pages were staged on
let ocrSubmitting = false;
async function ocrSubmitStaged() {
  if (ocrSubmitting) return;              // double-clicks must not duplicate jobs
  ocrSubmitting = true;
  const button = el("ocr-submit");
  if (button) button.disabled = true;
  try {
    await ocrSubmitStagedOnce();
  } finally {
    ocrSubmitting = false;
    if (button) button.disabled = false;
  }
}

async function ocrSubmitStagedOnce() {
  const bid = ocrState.book;
  if (!bid) { el("ocr-msg").textContent = "Pick a book first"; return; }
  const bySrc = stagedPagesFor(bid);
  const analysisGroups = stagedAnalysisPagesFor(bid);
  if (!bySrc.size && !analysisGroups.size) {
    el("ocr-msg").textContent = "Nothing staged — select pages for OCR or text analysis";
    return;
  }
  let failed = false;
  let submitted = false;
  for (const [src, staged] of bySrc) {
    // stub services can't run: one honest queue row PER service
    const stubs = staged.filter((x) => !OCR_RUNNABLE[x.service]);
    const runnable = staged.filter((x) => OCR_RUNNABLE[x.service]);
    for (const svc of new Set(stubs.map((x) => x.service))) {
      ocrQueuePages(bid, src, [stubs.find((x) => x.service === svc)]);
    }
    for (const x of stubs) ocrState.pageTags.delete(`${bid}:${src}:${x.page}`);
    if (runnable.length) {
      if (await ocrQueuePages(bid, src, runnable)) {
        submitted = true;
        for (const x of runnable) {
          ocrState.pageTags.delete(`${bid}:${src}:${x.page}`);
        }
      } else {
        failed = true;   // ocrQueuePages already explained why in ocr-msg
      }
    }
  }
  const analysis = await submitAnalysisStaged(bid);
  submitted = submitted || analysis.submitted;
  failed = failed || analysis.failed;
  // Successful pages are already unstaged above. Clear the visual page
  // selection too, so another digit + submit cannot enqueue the same pages.
  // Failed pages remain staged for an intentional retry.
  if (submitted) clearOcrPageSel();
  decorateOcrPages();
  decorateAnFacsimile();
  if (!failed) updateOcrStagedMsg();
}

// un-stage every page staged on the current book (an escape from a stray
// "stage every page"); the per-source keys keep other books' staging intact
function clearOcrStaging() {
  const bid = ocrState.book;
  if (!bid) return;
  for (const k of [...ocrState.pageTags.keys()]) {
    if (k.startsWith(bid + ":")) ocrState.pageTags.delete(k);
  }
  for (const k of [...ocrState.analysisTags.keys()]) {
    if (k.startsWith(bid + ":")) ocrState.analysisTags.delete(k);
  }
  decorateOcrPages();
  decorateAnFacsimile();
  updateOcrStagedMsg();
}

async function cancelAllOcrJobs() {
  const running = ocrState.jobs.filter((j) => j.id && !j.finished && !j.cancelling);
  if (!running.length) {
    status("OCR :: NO RUNNING JOBS TO STOP");
    return;
  }
  await Promise.all(running.map((j) => cancelOcrJob(j.id)));
}

let ocrPollTimer = null;

function pollOcrJobs() {
  if (ocrPollTimer) return;
  ocrPollTimer = setInterval(async () => {
    const running = ocrState.jobs.filter((j) => j.id && !j.finished);
    if (!running.length) {
      clearInterval(ocrPollTimer);
      ocrPollTimer = null;
      return;
    }
    for (const j of running) {
      try {
        const res = await fetch(`/api/ocr/job/${j.id}`);
        if (res.status === 404) {
          // the server restarted: the in-memory job is gone for good
          j.finished = true;
          j.status = "Lost — the server restarted mid-job";
          continue;
        }
        const data = await res.json();
        if (!data.ok) continue;
        const job = data.job;
        if (!(j.mergedPages instanceof Set)) j.mergedPages = new Set();
        const newlyCompleted = job.pages
          .filter((x) => x.status === "ok" && !j.mergedPages.has(x.page))
          .map((x) => x.page);
        if (newlyCompleted.length && await refreshCompiledDoc(
            j.buildId, newlyCompleted, j.target, j.src, true)) {
          for (const page of newlyCompleted) j.mergedPages.add(page);
        }
        // Page badges stop showing the spinner as each page completes; they do
        // not wait for the rest of a large batch either.
        for (const x of job.pages) {
          if (x.status !== "queued" && x.status !== "running") {
            ocrState.pageRunning.delete(`${j.buildId}:${j.src || "primary"}:${x.page}`);
          }
        }
        const pageErrors = job.pages.filter((x) =>
          typeof x.status === "string" && x.status.startsWith("error:"));
        const errorDetail = pageErrors.length
          ? pageErrors.map((x) => `page ${x.page} (${OCR_SERVICE_LABELS[x.service] || x.service}): ` +
              x.status.slice("error:".length).trim()).join("; ")
          : "";
        const active = job.status === "running" || job.status === "cancelling";
        j.cancelling = job.status === "cancelling";
        j.status = job.status === "running"
          ? `Running — ${job.done}/${job.pages.length}`
          : job.status === "cancelling"
            ? `Cancelling — ${job.done}/${job.pages.length} completed; finishing current page…`
            : job.status === "cancelled"
              ? `Cancelled — ${job.done} completed, ${job.cancelled || 0} skipped`
              : job.status + (errorDetail ? ` — ${errorDetail}` : "");
        if (!active) {
          j.finished = true;
          if (errorDetail) {
            const message = `OCR FAILED :: ${j.book} :: ${errorDetail}`;
            statusErr(message);
            // statusErr already tees this into Info; keep the OCR pane useful
            // without requiring a tab switch as well.
            el("ocr-msg").textContent = errorDetail;
          }
          // finished pages (ok or errored) are no longer running
          for (const x of job.pages) {
            ocrState.pageRunning.delete(`${j.buildId}:${j.src || "primary"}:${x.page}`);
          }
        }
      } catch (e) {
        // repeated garbage responses: give up rather than poll forever
        j.failPolls = (j.failPolls || 0) + 1;
        if (j.failPolls > 10) {
          j.finished = true;
          j.status = "Unreachable — polling stopped";
        }
      }
    }
    renderOcrQueue();
    decorateOcrPages();
  }, 1500);
}

// Pull newly finished pages from the server's compiled.txt while a job is
// still running. Every other section keeps the user's local (possibly
// unsaved) version, so a running edit session is not clobbered.
async function refreshCompiledDoc(bid, donePages, target, src, quiet) {
  target = target || "compiled.txt";
  delete ocrState.layoutMeta[bid];   // the job may have added figures / boxes
  ocrState.regionsCache.clear();     // …or replaced/dropped region records
  ocrState.wordsCache.clear();       // OCR'd pages now have placeable boxes
  await loadOcrBooks();
  try {
    const data = await (await fetch(
      `/api/builds/${encodeURIComponent(bid)}/ocr/` +
      encodeURIComponent(target))).json();
    if (!data.ok) return false;
    const doc = ocrState.docs.find(
      (d) => d.buildId === bid && (d.fileName || d.name) === target);
    if (!doc) {
      ocrState.docs.push({ id: "d" + (++ocrState.seq), name: target,
                           text: data.text, buildId: bid, fileName: target,
                           src: src || "primary" });
    } else {
      ocrSyncEditor();   // flush pending editor/page-view edits first
      const local = ocrPageSections(doc.text);
      const server = ocrPageSections(data.text);
      if (!local || !server || !donePages) {
        doc.text = data.text;
      } else {
        for (const n of donePages) {
          if (server.map.has(n)) local.map.set(n, server.map.get(n));
        }
        doc.text = ocrPagesToText(local);
      }
    }
    renderOcrTab();
    if (!quiet) status(`OCR RESULT MERGED :: ${target}`);
    return true;
  } catch (e) {
    return false;   // retry these pages on the next poll
  }
}

// stage the whole book with the selected service (submit stays manual);
// "the book" means the PDF the page view currently shows — the selected
// doc's own source
async function ocrQueueJob() {
  const bid = ocrState.book;
  const b = bid ? state.builds[bid] : null;
  if (!b) { el("ocr-msg").textContent = "Pick a book first"; return; }
  const d = ocrSelDoc();
  const src = d && d.buildId === bid ? docSrcKey(d) : "primary";
  const pdf = ocrSrcPdf(bid, src);
  if (!pdf) { el("ocr-msg").textContent = "This book has no PDF"; return; }
  const svc = el("ocr-service").value;
  let count = 0;
  try {
    const info = await (await fetch("/api/pdf/info?path=" + encodeURIComponent(pdf))).json();
    if (info.ok) count = info.pages;   // stage the whole book, not just page 400
  } catch (e) { /* handled below */ }
  if (!count) { el("ocr-msg").textContent = "Could not read the PDF"; return; }
  for (let n = 1; n <= count; n++) {
    ocrState.pageTags.set(`${bid}:${src}:${n}`, svc);
  }
  decorateOcrPages();
  updateOcrStagedMsg();
}

// --- page-view interactions: selection, digit staging, title pages --
// Click a page image to select it, Ctrl+click to extend the selection as a
// range from the last click. Pressing a digit (mapping in Settings > OCR)
// STAGES the selected pages — or just the hovered page — for that service;
// different digits build a mixed batch, and nothing is processed until the
// submit button sends it. T marks the hovered page as a title page.
// Selected pages can be deleted from the FULL PDF (trash button / Delete).

let ocrHoverPage = 0;

function ocrPagesActive() {
  return !!document.querySelector('#tabs .tab.active[data-tab="workbench"]') &&
    !!document.querySelector("#phase-text.active") &&
    !el("ocr-document-workspace").hidden && ocrState.view === "pdf";
}

function clearOcrPageSel() {
  ocrState.pageSel.clear();
  ocrState.selAnchor = 0;
}

// --- page navigation -----------------------------------------------------------
// The reader is a continuous scroll (not a one-page-at-a-time viewer), so arrows
// keep their native line-scroll; PageUp/PageDown step a whole page, Home/End
// jump to the ends, and the pane-bar box reads (and jumps to) the page currently
// at the top of the viewport. data-page is 1..N contiguous, so row count == last
// page number.
function ocrPageRows() { return el("ocr-pages").querySelectorAll(".ocr-pgrow"); }

function ocrTopPage() {
  const box = el("ocr-pages");
  const rows = ocrPageRows();
  if (!rows.length) return 1;
  const top = box.getBoundingClientRect().top;
  let cur = +rows[0].dataset.page;
  for (const r of rows) {
    if (r.getBoundingClientRect().top - top <= 4) cur = +r.dataset.page;   // scrolled to/above the top edge
    else break;
  }
  return cur;
}

// reflect the current page in the jump box, unless the user is typing in it
function ocrSyncPageInput(n) {
  const inp = el("ocr-page-jump");
  if (inp && document.activeElement !== inp) inp.value = n;
}

function ocrScrollToPage(n) {
  const rows = ocrPageRows();
  if (!rows.length) return;
  n = Math.max(1, Math.min(rows.length, n));
  const row = el("ocr-pages").querySelector(`.ocr-pgrow[data-page="${n}"]`);
  if (row) { row.scrollIntoView({ block: "start" }); ocrSyncPageInput(n); }
}

function onOcrPagesKey(ev) {
  if (!ocrPagesActive()) return;
  if (/^(INPUT|TEXTAREA|SELECT)$/.test(ev.target.tagName) ||
      ev.target.isContentEditable) return;
  // page navigation works on any viewed PDF, so it runs before the build gate
  if (ev.key === "PageDown") { ev.preventDefault(); ocrScrollToPage(ocrTopPage() + 1); return; }
  if (ev.key === "PageUp") { ev.preventDefault(); ocrScrollToPage(ocrTopPage() - 1); return; }
  if (ev.key === "Home") { ev.preventDefault(); ocrScrollToPage(1); return; }
  if (ev.key === "End") { ev.preventDefault(); ocrScrollToPage(ocrPageRows().length); return; }
  const d = ocrSelDoc();
  const bid = d && d.buildId;
  if (!bid) return;
  if (ev.key === "Escape") {
    clearOcrPageSel();
    decorateOcrPages();
    updateOcrStagedMsg();
    return;
  }
  if (ev.key === "Delete" || ev.key === "Backspace") {
    if (ocrState.pageSel.size) {
      ev.preventDefault();
      deleteSelectedPages();
    }
    return;
  }
  if (/^[1-9]$/.test(ev.key)) {
    const svc = (state.settings.ocrKeyMap || {})[ev.key];
    if (!svc) return;
    ev.preventDefault();
    // stage the selection (or the hovered page); the same digit untags.
    // Tags bind to the page view's SOURCE: page numbers of one scan mean
    // nothing on another.
    const targets = ocrState.pageSel.size
      ? [...ocrState.pageSel]
      : (ocrHoverPage ? [ocrHoverPage] : []);
    for (const n of targets) {
      const k = `${bid}:${ocrState.pagesSrc}:${n}`;
      if (ocrState.pageTags.get(k) === svc) ocrState.pageTags.delete(k);
      else ocrState.pageTags.set(k, svc);
    }
    decorateOcrPages();
    updateOcrStagedMsg();
    return;
  }
  if ((ev.key === "t" || ev.key === "T") && ocrHoverPage) {
    ev.preventDefault();
    // title pages are counted on the PRIMARY PDF; numbers from another
    // scan's view would mark the wrong pages
    if (ocrState.pagesSrc !== "primary") {
      el("ocr-msg").textContent =
        "Title pages are marked on the primary PDF's page view";
      return;
    }
    toggleTitlePage(bid, ocrHoverPage);
  }
}

function onOcrPagesClick(ev) {
  // the title-page toggle sits inside .ocr-pgimg — handle it before the
  // image-click (row-select) logic below, or clicking it would also
  // select/deselect the row
  const titleBtn = ev.target.closest(".pg-title-toggle");
  if (titleBtn) {
    ev.preventDefault();
    const row = titleBtn.closest(".ocr-pgrow");
    const d = ocrSelDoc();
    if (row && d && d.buildId) toggleTitlePage(d.buildId, +row.dataset.page);
    return;
  }
  // clicks on the page IMAGE select; clicks in the text boxes edit
  const img = ev.target.closest(".ocr-pgimg");
  if (!img) return;
  const row = img.closest(".ocr-pgrow");
  if (!row) return;
  ev.preventDefault();
  const page = +row.dataset.page;
  if (ev.ctrlKey && ocrState.selAnchor) {
    const from = Math.min(ocrState.selAnchor, page);
    const to = Math.max(ocrState.selAnchor, page);
    for (let n = from; n <= to; n++) ocrState.pageSel.add(n);
  } else if (ocrState.pageSel.has(page)) {
    ocrState.pageSel.delete(page);
    ocrState.selAnchor = page;
  } else {
    ocrState.pageSel.add(page);
    ocrState.selAnchor = page;
  }
  decorateOcrPages();
  updateOcrStagedMsg();
}

// delete the selected pages from the build's ACTUAL PDF (never the
// truncated preview derivative); the server keeps a .bak.pdf and renumbers
// the OCR files + title pages
async function deleteSelectedPages() {
  const d = ocrSelDoc();
  const bid = d && d.buildId;
  if (!bid || !ocrState.pageSel.size) return;
  // the deletion hits the PDF the page view shows — the doc's own source;
  // the server renumbers only that source's OCR files
  const pdf = docPdf(d);
  if (!pdf) { el("ocr-msg").textContent = "This book has no attached PDF"; return; }
  if (/^output[\/\\]entries[\/\\]/i.test(pdf)) {
    el("ocr-msg").textContent =
      "This book only has the truncated preview — re-attach the original scan first";
    return;
  }
  // deleting shifts page numbers under a running job's feet
  if ([...ocrState.pageRunning.keys()].some((k) => k.startsWith(bid + ":"))) {
    el("ocr-msg").textContent = "An OCR job is running for this book — wait for it";
    return;
  }
  const pages = [...ocrState.pageSel].sort((a, z) => a - z);
  if (!(await confirmDialog({
      title: "Delete PDF pages",
      message: `Delete ${pages.length} page(s) [${pages.join(", ")}] from the PDF?`,
      detail: `${pdf}\n\nThe previous version is kept as a .bak.pdf next to ` +
        "it; OCR files and title pages are renumbered to match.",
      confirmLabel: "Delete pages",
      danger: true,
    }))) {
    return;
  }
  el("ocr-msg").textContent = "Deleting pages ...";
  // unsaved edits must survive: flush and save the book's folder docs so
  // the server renumbers the CURRENT text, not a stale file
  ocrSyncEditor();
  for (const doc of ocrState.docs) {
    if (doc.buildId !== bid || !(doc.fileName || doc.name)) continue;
    try {
      await fetch(`/api/builds/${encodeURIComponent(bid)}/ocr`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: doc.fileName || doc.name, text: doc.text }),
      });
    } catch (e) { /* the renumber then works from the last saved version */ }
  }
  try {
    const res = await fetch("/api/pdf/pages/delete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ build_id: bid, pdf, pages }),
    });
    const data = await res.json().catch(() => ({}));
    if (!data.ok) {
      el("ocr-msg").textContent = data.error || "Page deletion failed";
      return;
    }
    if (data.build) state.builds[bid] = data.build;
    clearOcrPageSel();
    // staged/running markers no longer match the new numbering
    for (const k of [...ocrState.pageTags.keys()]) {
      if (k.startsWith(bid + ":")) ocrState.pageTags.delete(k);
    }
    for (const k of [...ocrState.analysisTags.keys()]) {
      if (k.startsWith(bid + ":")) ocrState.analysisTags.delete(k);
    }
    status(`PAGES DELETED :: ${pages.length} (backup: ${data.backup})`);
    el("ocr-msg").textContent = "";
    // the PDF changed on disk: page counts, dims, word boxes, and the
    // renumbered region records are all stale — a cache hit here would
    // paint the deleted page's regions beside the shifted page image
    ocrState.pdfInfo = {};
    ocrState.wordsCache.clear();
    ocrState.regionsCache.clear();
    delete ocrState.layoutMeta[bid];
    // reload the renumbered OCR docs and the shrunken PDF
    await loadOcrBooks();
    ocrState.bookLoading = null;
    ocrState.docs = ocrState.docs.filter((x) => x.buildId !== bid);
    await selectOcrBook(bid);
    setOcrView("pdf");
  } catch (e) {
    el("ocr-msg").textContent = "Page deletion failed";
  }
}

// title pages persist on the build (comma-separated page numbers)
function titlePageSet(b) {
  return new Set(String((b && b.title_pages) || "").split(",")
    .map((x) => parseInt(x, 10)).filter((n) => n > 0));
}

async function toggleTitlePage(bid, page) {
  const b = state.builds[bid];
  if (!b) return;
  const set = titlePageSet(b);
  const marking = !set.has(page);
  if (marking) set.add(page);
  else set.delete(page);
  const val = [...set].sort((a, z) => a - z).join(",");
  if (await patchBuildRaw(bid, { title_pages: val }, true)) {
    decorateOcrPages();
    status(marking ? `TITLE PAGE :: ${page}` : `TITLE PAGE CLEARED :: ${page}`);
  }
}

// corner chips + outlines on the page rows: T = title page, amber chip =
// staged (awaiting submit), cyan chip = processing, amber outline = selected
function decorateOcrPages() {
  const box = el("ocr-pages");
  // Hidden (another tab, or a background job while the user is elsewhere): the
  // chips are pure display over ocrState, so skip — a return to the tab
  // re-renders and re-decorates with the box visible.
  if (box.offsetParent === null) return;
  const d = ocrSelDoc();
  const b = d && d.buildId ? state.builds[d.buildId] : null;
  const titles = titlePageSet(b);
  box.querySelectorAll(".ocr-pgrow").forEach((row) => {
    const n = +row.dataset.page;
    const k = `${d && d.buildId}:${ocrState.pagesSrc}:${n}`;
    const staged = b ? ocrState.pageTags.get(k) : undefined;
    const running = b ? ocrState.pageRunning.get(k) : undefined;
    const title = titles.has(n);
    row.classList.toggle("pg-title", title);
    row.classList.toggle("pg-staged", !!staged);
    row.classList.toggle("pg-queued", !!running);
    row.classList.toggle("pg-sel", ocrState.pageSel.has(n));
    // The chip HTML depends only on title/staged/running. Rebuild it only when
    // one of those changed, so the 1.5s job poller doesn't rewrite every row's
    // innerHTML every tick — on a long book only the handful of pages that just
    // changed state get touched. (Selection is a class-only cue, not a chip.)
    const sig = `${title ? "T" : ""}|${staged || ""}|${running || ""}`;
    if (row.dataset.chipSig === sig) return;
    row.dataset.chipSig = sig;
    let chip = row.querySelector(".pg-chips");
    if (!chip) {
      chip = document.createElement("span");
      chip.className = "pg-chips";
      row.querySelector(".ocr-pgimg").appendChild(chip);
    }
    // the title-page toggle is always in the DOM (so it's clickable), and CSS
    // keeps it subtle until the row is hovered or it's already marked — the
    // "T" keyboard shortcut still works too, this is just the discoverable path
    chip.innerHTML =
      `<button type="button" class="pg-chip pg-title-toggle${title ? " on" : ""}"
               data-tip="${title ? "Title page — click to unmark" : "Mark as title page"}">T</button>` +
      (staged ? `<span class="pg-chip staged" data-tip="Staged: ${esc(OCR_SERVICE_LABELS[staged])} — press submit">${esc(staged.slice(0, 2).toUpperCase())}</span>` : "") +
      (running ? `<span class="pg-chip svc" data-tip="Processing: ${esc(OCR_SERVICE_LABELS[running])}">${esc(running.slice(0, 2).toUpperCase())}</span>` : "");
  });
}

function selectOcrDocument(id) {
  ocrSyncEditor();
  const prev = ocrSelDoc();
  ocrState.sel = id;
  const d = ocrSelDoc();
  // Page view + same book: keep the loaded page images and swap only the text
  // alongside them (don't tear down and re-fetch the PDF).
  const sameBook = ocrState.view === "pdf" && d && prev && d.buildId &&
    d.buildId === prev.buildId && el("ocr-pages").querySelector(".ocr-pgrow");
  if (sameBook && refillOcrPageText(d)) {
    // the views now hold THIS doc — a later re-render (the OCR-job poller)
    // must see it as the same doc, or it tears the page view down
    ocrState.lastRendered = d.id;
    renderOcrDocs();   // refresh the docs-list active highlight only
    // Analysis has its own facsimile tree. The page-view refill above does not
    // touch it, so refresh it explicitly before taking this optimized return.
    renderAnFacsimile();
    return;
  }
  renderOcrTab();
}

// --- Replica workbench (docs/facsimile-workbench-plan.md, Phase 3) ----------
// Correct the machine-proposed page regions — draw, move, resize, split,
// merge, digit-key roles, reading order — then recompile the compiled
// document's body text from them. The machine proposes, the person disposes.

// digit keys 1..9,0 assign these roles, mirroring the OCR engine keymap habit
const RW_ROLES = ["body", "marginalia", "header", "footer", "title",
                  "caption", "figure", "page-number", "catch-word",
                  "signature-mark"];

const rwState = {
  book: "", src: "primary", page: 0,
  pdf: "",          // the source PDF (rasters + word clip)
  doc: "",          // compiled file this page's regions belong to
  dims: null, items: [],
  sel: [],          // selected region ids; the LAST one is active
  dirty: false, pageCount: 0, regionPages: [],
  regionStates: {}, // page -> review flag ("verified"), for the strip chips
  outliers: [],     // pages flagged by the last template-outlier run
  templates: [],    // this source's template names
  layer: "text",    // which text layer the panel edits: "text" | "norm"
  pageState: "",    // this page's review flag
  drag: null,       // {kind: move|resize|draw, id, corner, start, box0, moved}
  lastPos: null,    // pointer in page fractions — the split guide
  seq: 0, idSeq: 0,
};

function rwActiveId() { return rwState.sel[rwState.sel.length - 1] || ""; }
function rwActive() { return rwState.items.find((x) => x.id === rwActiveId()) || null; }
function rwItem(id) { return rwState.items.find((x) => x.id === id) || null; }
function rwNewId() { return "n" + (++rwState.idSeq); }

function rwPdfForSrc(bid, src) {
  if (!src || src === "primary") return ocrBookPdf(bid);
  const b = state.builds[bid] || {};
  const s = (b.pdf_sources || []).find((x) => x.id === src);
  return s ? s.path : "";
}

function renderReplicaBooks() {
  const list = el("rw-books");
  list.innerHTML = "";
  // every build with an entry folder — NOT ocrBookList(), whose verified-only
  // filter belongs to Analyze; drafts are exactly what the workbench corrects
  const books = allBuildsSorted().filter((b) => (ocrState.books || {})[b.id]);
  el("rw-book-count").textContent = books.length || "";
  for (const b of books) {
    const li = document.createElement("li");
    li.className = "ocr-book" + (b.id === rwState.book ? " active" : "");
    li.dataset.bid = b.id;
    li.innerHTML = `<span class="bi-title">${esc(b.title) || "<em>(untitled)</em>"}</span>
      <span class="bi-meta">${esc(b.authors || "")}${b.authors && b.year ? " &middot; " : ""}${esc(b.year || "")}</span>`;
    list.appendChild(li);
  }
}

async function selectReplicaBook(bid) {
  if (rwState.dirty && !confirm("Discard unsaved region edits?")) return;
  // one shared sequence for book, source, AND page context: any in-flight
  // fetch from before this click resolves into a dead sequence number and
  // must not install its data (two quick book clicks used to interleave —
  // book A's page strip and PDF under book B's regions)
  const seq = ++rwState.seq;
  rwState.book = bid; rwState.page = 0; rwState.items = [];
  rwState.sel = []; rwState.dirty = false;
  rwState.outliers = [];   // the last scan flagged ANOTHER book's pages
  el("rw-canvas").hidden = true;
  el("rw-empty").hidden = false;
  renderReplicaBooks();
  // fresh meta on every visit: jobs and edits move the sidecar under us
  delete ocrState.layoutMeta[bid];
  const b = state.builds[bid] || {};
  const srcs = ["primary", ...(b.pdf_sources || []).map((s) => s.id)];
  const sel = el("rw-src");
  sel.innerHTML = srcs.map((s) => `<option value="${esc(s)}">${esc(s)}</option>`).join("");
  sel.hidden = srcs.length < 2;
  rwState.src = srcs.includes(rwState.src) ? rwState.src : "primary";
  sel.value = rwState.src;
  await renderReplicaPages(seq);
  if (seq !== rwState.seq) return;
  await rwLoadTemplates(seq);
  if (seq !== rwState.seq) return;
  rwSyncBar();
}

async function rwLoadTemplates(seq) {
  let names = [];
  try {
    const r = await (await fetch(
      `/api/builds/${encodeURIComponent(rwState.book)}/ocr-templates` +
      `?src=${encodeURIComponent(rwState.src)}`)).json();
    if (r.ok) names = (r.templates || []).map((t) => t.name);
  } catch (e) { /* no templates yet */ }
  if (seq !== rwState.seq) return;
  rwState.templates = names;
  const sel = el("rw-tpl");
  sel.innerHTML = ['<option value="">tpl…</option>',
    ...names.map((n) => `<option value="${esc(n)}">${esc(n)}</option>`)].join("");
}

// the page strip chip: an outlier flag beats the review check beats the
// plain has-regions dot
function rwPageChip(n) {
  if (rwState.outliers.includes(n)) return '<span class="rw-warn">!</span>';
  if ((rwState.regionStates || {})[String(n)] === "verified") {
    return '<span class="rw-check">✓</span>';
  }
  return rwState.regionPages.includes(n) ? '<span class="rw-dot">●</span>' : "";
}

async function renderReplicaPages(seq) {
  const bid = rwState.book;
  const src = rwState.src;
  const pdf = rwPdfForSrc(bid, src);
  const meta = await ocrLayoutMeta(bid);
  if (seq !== rwState.seq) return;
  const regionPages = ((meta.regionPages || {})[src] || []).slice();
  let count = 0;
  if (pdf) {
    let info = ocrState.pdfInfo[pdf];
    if (!info) {
      try {
        const data = await (await fetch(
          "/api/pdf/info?path=" + encodeURIComponent(pdf))).json();
        if (data.ok) { info = data; ocrState.pdfInfo[pdf] = data; }
      } catch (e) { /* the strip still lists the region pages */ }
      if (seq !== rwState.seq) return;
    }
    if (info) count = info.pages || (info.dims || []).length || 0;
  }
  rwState.pdf = pdf;
  rwState.regionPages = regionPages;
  rwState.regionStates = (meta.regionStates || {})[src] || {};
  rwState.pageCount = Math.max(count, ...regionPages, 0);
  const box = el("rw-pages");
  box.innerHTML = "";
  const frag = document.createDocumentFragment();
  for (let n = 1; n <= rwState.pageCount; n++) {
    const d = document.createElement("button");
    d.type = "button";
    d.className = "rw-pagebtn" + (n === rwState.page ? " active" : "");
    d.dataset.page = n;
    d.innerHTML = `${n}` + rwPageChip(n);
    frag.appendChild(d);
  }
  box.appendChild(frag);
  el("rw-page-count").textContent = rwState.pageCount || "";
}

async function selectReplicaPage(n) {
  if (rwState.dirty && !confirm("Discard unsaved region edits?")) return;
  rwState.page = n; rwState.sel = []; rwState.dirty = false;
  const seq = ++rwState.seq;
  for (const b of el("rw-pages").querySelectorAll(".rw-pagebtn")) {
    b.classList.toggle("active", +b.dataset.page === n);
  }
  el("rw-page-img").src = `/api/pdf/pageimg?path=${encodeURIComponent(rwState.pdf)}&page=${n}&w=1100`;
  el("rw-canvas").hidden = false;
  el("rw-empty").hidden = true;
  // always fetched fresh — this is the editor, never a cached copy
  let rec = { found: false };
  try {
    rec = await (await fetch(
      `/api/builds/${encodeURIComponent(rwState.book)}/ocr-regions` +
      `?src=${encodeURIComponent(rwState.src)}&page=${n}`)).json();
  } catch (e) { /* a bare page: draw from scratch */ }
  if (seq !== rwState.seq) return;
  rwState.items = (rec.found ? rec.items || [] : []).map((it, i) => ({
    id: "r" + i, role: it.role || "body",
    order: it.order != null ? +it.order : i,
    box: { x: +((it.box || {}).x) || 0, y: +((it.box || {}).y) || 0,
           w: +((it.box || {}).w) || 0, h: +((it.box || {}).h) || 0 },
    text: String(it.text || ""),
    norm: String(it.norm || ""),
  }));
  rwState.doc = (rec.found && rec.doc) || "compiled.txt";
  rwState.dims = rec.found ? rec.dims : null;
  rwState.pageState = (rec.found && rec.state) || "";
  rwSetLayer("text");
  let ratio = "";
  if (rwState.dims && rwState.dims.w > 0) {
    ratio = `${rwState.dims.w} / ${rwState.dims.h}`;
  } else {
    const info = ocrState.pdfInfo[rwState.pdf];
    const d = info && info.dims && info.dims[n - 1];
    if (d && d[0] > 0) ratio = `${d[0]} / ${d[1]}`;
  }
  el("rw-canvas").style.aspectRatio = ratio || "3 / 4";
  rwRenderOverlay();
  rwSyncBar();
}

function rwRenderOverlay() {
  const ov = el("rw-overlay");
  ov.innerHTML = "";
  const frag = document.createDocumentFragment();
  for (const it of rwState.items) {
    const e = document.createElement("div");
    e.className = "rw-region role-" + it.role;
    if (rwState.sel.includes(it.id)) e.classList.add("sel");
    if (it.id === rwActiveId()) e.classList.add("active");
    e.dataset.id = it.id;
    e.style.left = it.box.x * 100 + "%";
    e.style.top = it.box.y * 100 + "%";
    e.style.width = it.box.w * 100 + "%";
    e.style.height = it.box.h * 100 + "%";
    const lab = document.createElement("span");
    lab.className = "rw-lab";
    lab.textContent = `${it.order} ${it.role}`;
    e.appendChild(lab);
    if (it.id === rwActiveId()) {
      for (const c of ["nw", "ne", "sw", "se"]) {
        const h = document.createElement("span");
        h.className = "rw-handle rw-" + c;
        h.dataset.corner = c;
        e.appendChild(h);
      }
    }
    frag.appendChild(e);
  }
  ov.appendChild(frag);
}

function rwPaintRegion(it) {
  const e = el("rw-overlay").querySelector(`[data-id="${it.id}"]`);
  if (!e) return;
  e.style.left = it.box.x * 100 + "%";
  e.style.top = it.box.y * 100 + "%";
  e.style.width = it.box.w * 100 + "%";
  e.style.height = it.box.h * 100 + "%";
}

// switch which text layer the panel shows and edits (diplomatic | normalized)
function rwSetLayer(layer) {
  rwState.layer = layer === "norm" ? "norm" : "text";
  el("rw-layer-dipl").classList.toggle("active", rwState.layer === "text");
  el("rw-layer-norm").classList.toggle("active", rwState.layer === "norm");
  const ta = el("rw-text");
  ta.placeholder = rwState.layer === "norm"
    ? "No normalized reading yet — Propose drafts one from the diplomatic text"
    : "Select a region";
  rwSyncBar();
}

// the mechanical normalization PROPOSAL: resolve long s and the common
// ligatures, join line-end hyphenation. Deliberately shallow — u/v and i/j
// swaps, spelling, and everything judgement-shaped stays with the human.
function rwNormalize(text) {
  let t = String(text || "")
    .replace(/ſ/g, "s")
    .replace(/ﬀ/g, "ff").replace(/ﬁ/g, "fi").replace(/ﬂ/g, "fl")
    .replace(/ﬃ/g, "ffi").replace(/ﬄ/g, "ffl").replace(/ﬅ|ﬆ/g, "st")
    .replace(/ /g, " ");
  // dehyphenate to a fixpoint: a single global pass consumes the next
  // line's word (its trailing hyphen included), so "ma-\nte-\nria" only
  // half-joined — every second break survived
  for (let prev = ""; prev !== t;) {
    prev = t;
    t = t.replace(/([A-Za-zæœÆŒ])[-¬⸗]\n(\S+)/g, "$1$2");
  }
  return t;
}

function rwSyncBar() {
  const nItems = rwState.items.length;
  el("rw-status").textContent = rwState.page
    ? `p ${rwState.page} · ${nItems} region${nItems === 1 ? "" : "s"}` +
      (rwState.pageState === "verified" ? " · ✓" : "") +
      (rwState.dirty ? " · unsaved" : "")
    : "";
  el("rw-save").disabled = !rwState.dirty || !rwState.page;
  el("rw-recompile").disabled = !rwState.book || !rwState.regionPages.length;
  el("rw-tpl-save").disabled = !rwState.page;
  el("rw-tpl-apply").disabled = !el("rw-tpl").value;
  el("rw-tpl-outliers").disabled = !el("rw-tpl").value;
  const a = rwActive();
  el("rw-region-info").textContent = a ? `${a.role} · order ${a.order}` : "";
  const ta = el("rw-text");
  ta.disabled = !a;
  if (document.activeElement !== ta) {
    ta.value = a ? (rwState.layer === "norm" ? a.norm || "" : a.text) : "";
  }
  el("rw-clip").disabled = !a;
  el("rw-normalize").disabled = !a;
}

function rwDirty() { rwState.dirty = true; rwSyncBar(); }

function rwPos(ev) {
  const r = el("rw-overlay").getBoundingClientRect();
  if (!r.width || !r.height) return { x: 0, y: 0 };
  return { x: Math.min(1, Math.max(0, (ev.clientX - r.left) / r.width)),
           y: Math.min(1, Math.max(0, (ev.clientY - r.top) / r.height)) };
}

function rwMouseDown(ev) {
  if (!rwState.page || ev.button !== 0) return;
  ev.preventDefault();
  const pos = rwPos(ev);
  const handle = ev.target.closest(".rw-handle");
  const regEl = ev.target.closest(".rw-region");
  if (handle && regEl) {
    rwState.drag = { kind: "resize", id: regEl.dataset.id,
                     corner: handle.dataset.corner, start: pos,
                     box0: { ...rwItem(regEl.dataset.id).box } };
    return;
  }
  if (regEl) {
    const id = regEl.dataset.id;
    if (ev.shiftKey) {
      rwState.sel = rwState.sel.includes(id)
        ? rwState.sel.filter((x) => x !== id) : [...rwState.sel, id];
    } else if (rwState.sel.includes(id)) {
      rwState.sel = [...rwState.sel.filter((x) => x !== id), id];
    } else {
      rwState.sel = [id];
    }
    rwState.drag = { kind: "move", id, start: pos,
                     box0: { ...rwItem(id).box }, moved: false };
    rwRenderOverlay(); rwSyncBar();
    return;
  }
  rwState.sel = [];
  rwState.drag = { kind: "draw", start: pos, id: "" };
  rwRenderOverlay(); rwSyncBar();
}

function rwMouseMove(ev) {
  const panel = el("replica");
  if (!panel || !panel.classList.contains("active")) return;
  const ovr = el("rw-overlay").getBoundingClientRect();
  if (ovr.width > 0 && ev.clientX >= ovr.left && ev.clientX <= ovr.right &&
      ev.clientY >= ovr.top && ev.clientY <= ovr.bottom) {
    rwState.lastPos = rwPos(ev);
  }
  const d = rwState.drag;
  if (!d) return;
  const pos = rwPos(ev);
  const dx = pos.x - d.start.x, dy = pos.y - d.start.y;
  if (d.kind === "move") {
    const it = rwItem(d.id); if (!it) return;
    if (Math.abs(dx) + Math.abs(dy) > 0.002) d.moved = true;
    it.box.x = Math.min(1 - it.box.w, Math.max(0, d.box0.x + dx));
    it.box.y = Math.min(1 - it.box.h, Math.max(0, d.box0.y + dy));
    rwPaintRegion(it);
  } else if (d.kind === "resize") {
    const it = rwItem(d.id); if (!it) return;
    const b = { ...d.box0 };
    if (d.corner.includes("w")) {
      b.x = Math.min(d.box0.x + d.box0.w - 0.005, Math.max(0, d.box0.x + dx));
      b.w = d.box0.x + d.box0.w - b.x;
    }
    if (d.corner.includes("e")) b.w = Math.max(0.005, d.box0.w + dx);
    if (d.corner.includes("n")) {
      b.y = Math.min(d.box0.y + d.box0.h - 0.005, Math.max(0, d.box0.y + dy));
      b.h = d.box0.y + d.box0.h - b.y;
    }
    if (d.corner.includes("s")) b.h = Math.max(0.005, d.box0.h + dy);
    b.w = Math.min(b.w, 1 - b.x);
    b.h = Math.min(b.h, 1 - b.y);
    it.box = b;
    rwPaintRegion(it);
  } else if (d.kind === "draw") {
    if (!d.id && (Math.abs(dx) > 0.004 || Math.abs(dy) > 0.004)) {
      const it = { id: rwNewId(), role: "body",
                   order: rwState.items.length
                     ? Math.max(...rwState.items.map((x) => x.order)) + 1 : 0,
                   box: { x: d.start.x, y: d.start.y, w: 0.004, h: 0.004 },
                   text: "" };
      rwState.items.push(it);
      d.id = it.id;
      rwState.sel = [it.id];
      rwRenderOverlay();
    }
    if (d.id) {
      const it = rwItem(d.id);
      it.box.x = Math.min(d.start.x, pos.x);
      it.box.y = Math.min(d.start.y, pos.y);
      it.box.w = Math.abs(pos.x - d.start.x);
      it.box.h = Math.abs(pos.y - d.start.y);
      rwPaintRegion(it);
    }
  }
}

function rwMouseUp() {
  const d = rwState.drag;
  if (!d) return;
  rwState.drag = null;
  if ((d.kind === "move" && d.moved) || d.kind === "resize" ||
      (d.kind === "draw" && d.id)) rwDirty();
  rwRenderOverlay(); rwSyncBar();
}

function rwSplit(it, axis) {
  // cut under the pointer when it's inside the region, else the midpoint
  const p = rwState.lastPos;
  let f = 0.5;
  if (p) {
    if (axis === "h" && p.y > it.box.y && p.y < it.box.y + it.box.h) {
      f = (p.y - it.box.y) / it.box.h;
    } else if (axis === "v" && p.x > it.box.x && p.x < it.box.x + it.box.w) {
      f = (p.x - it.box.x) / it.box.w;
    }
  }
  f = Math.min(0.95, Math.max(0.05, f));
  for (const x of rwState.items) if (x.order > it.order) x.order += 1;
  const twin = { id: rwNewId(), role: it.role, order: it.order + 1,
                 box: { ...it.box }, text: "" };
  if (axis === "h") {
    const cut = it.box.h * f;
    twin.box.y = it.box.y + cut;
    twin.box.h = it.box.h - cut;
    it.box.h = cut;
    // a horizontal cut can split the text by line, proportionally
    const lines = it.text.split("\n");
    if (lines.length > 1) {
      const at = Math.max(1, Math.min(lines.length - 1,
                                      Math.round(lines.length * f)));
      twin.text = lines.slice(at).join("\n");
      it.text = lines.slice(0, at).join("\n");
    }
  } else {
    const cut = it.box.w * f;
    twin.box.x = it.box.x + cut;
    twin.box.w = it.box.w - cut;
    it.box.w = cut;   // a vertical cut can't split text — the left keeps it
  }
  rwState.items.push(twin);
  rwState.sel = [twin.id];
  rwDirty(); rwRenderOverlay(); rwSyncBar();
}

function rwMerge() {
  const sel = rwState.sel.map(rwItem).filter(Boolean)
    .sort((a, b) => a.order - b.order);
  if (sel.length < 2) return;
  const first = sel[0];
  const x0 = Math.min(...sel.map((r) => r.box.x));
  const y0 = Math.min(...sel.map((r) => r.box.y));
  first.box = {
    x: x0, y: y0,
    w: Math.max(...sel.map((r) => r.box.x + r.box.w)) - x0,
    h: Math.max(...sel.map((r) => r.box.y + r.box.h)) - y0,
  };
  first.text = sel.map((r) => r.text.trim()).filter(Boolean).join("\n");
  const drop = new Set(sel.slice(1).map((r) => r.id));
  rwState.items = rwState.items.filter((x) => !drop.has(x.id));
  rwState.sel = [first.id];
  rwDirty(); rwRenderOverlay(); rwSyncBar();
}

function rwReorder(it, dir) {
  const sorted = [...rwState.items].sort((a, b) => a.order - b.order);
  const i = sorted.indexOf(it);
  const j = i + dir;
  if (j < 0 || j >= sorted.length) return;
  const other = sorted[j];
  const tmp = it.order; it.order = other.order; other.order = tmp;
  rwDirty(); rwRenderOverlay(); rwSyncBar();
}

function rwKeyDown(ev) {
  const panel = el("replica");
  if (!panel || !panel.classList.contains("active")) return;
  // a global overlay (Settings, catalog pickers…) over the tab owns its own
  // keys — Del/Tab/digits reaching the workbench would edit regions unseen
  if (document.querySelector(".overlay:not([hidden])")) return;
  // only keys aimed at the workbench (or at nothing at all) count: focus in
  // some other component's control must not drive the region editor
  const t = ev.target;
  const elT = t instanceof Element ? t : null;
  const inReplica = !!(elT && elT.closest("#replica"));
  if (!inReplica && t !== document.body && t !== document.documentElement) return;
  if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === "s") {
    ev.preventDefault(); rwSave(); return;
  }
  if (elT && (elT.isContentEditable || elT.tagName === "TEXTAREA" ||
              elT.tagName === "INPUT" || elT.tagName === "SELECT")) return;
  // every command below is a BARE key: Ctrl+V is a paste reflex, not a
  // request to flip the review flag, and Ctrl+digit belongs to the shell
  if (ev.ctrlKey || ev.metaKey || ev.altKey) return;
  const a = rwActive();
  if (/^[0-9]$/.test(ev.key) && rwState.sel.length) {
    const role = RW_ROLES[(parseInt(ev.key, 10) + 9) % 10];
    for (const id of rwState.sel) { const it = rwItem(id); if (it) it.role = role; }
    rwDirty(); rwRenderOverlay(); rwSyncBar();
  } else if (ev.key === "Delete" || ev.key === "Backspace") {
    if (!rwState.sel.length) return;
    rwState.items = rwState.items.filter((x) => !rwState.sel.includes(x.id));
    rwState.sel = [];
    rwDirty(); rwRenderOverlay(); rwSyncBar();
  } else if (ev.key === "Escape") {
    rwState.sel = [];
    rwRenderOverlay(); rwSyncBar();
  } else if ((ev.key === "s" || ev.key === "S") && a) {
    rwSplit(a, ev.shiftKey ? "v" : "h");
  } else if ((ev.key === "v" || ev.key === "V") && rwState.page) {
    // toggle the page's review flag; saved with the regions. The flag lives
    // ON the region record, so an empty page has nowhere to keep it — the
    // server would drop record and flag together while the UI showed ✓
    if (!rwState.items.length) {
      status("VERIFY :: nothing on this page to verify — add regions first");
      return;
    }
    rwState.pageState = rwState.pageState === "verified" ? "" : "verified";
    rwDirty();
  } else if ((ev.key === "m" || ev.key === "M") && rwState.sel.length >= 2) {
    rwMerge();
  } else if (ev.key === "Tab" && rwState.items.length) {
    ev.preventDefault();
    const sorted = [...rwState.items].sort((x, y) => x.order - y.order);
    const idx = sorted.findIndex((x) => x.id === rwActiveId());
    const next = sorted[(idx + (ev.shiftKey ? -1 : 1) + sorted.length) % sorted.length];
    rwState.sel = [next.id];
    rwRenderOverlay(); rwSyncBar();
  } else if ((ev.key === "[" || ev.key === "]") && a) {
    rwReorder(a, ev.key === "]" ? 1 : -1);
  }
}

async function rwClipWords() {
  const a = rwActive();
  if (!a || !rwState.pdf) return;
  const seq = rwState.seq;
  let res = null;
  try {
    res = await (await fetch(
      `/api/pdf/words?path=${encodeURIComponent(rwState.pdf)}` +
      `&page=${rwState.page}&build_id=${encodeURIComponent(rwState.book)}`)).json();
  } catch (e) { /* handled below */ }
  // the page (or the region itself) may be gone by now: applying the clip
  // would write onto an orphan and dirty a page the user never touched
  if (seq !== rwState.seq || !rwState.items.includes(a)) return;
  if (!res || !res.ok || !res.found) {
    status("CLIP :: no word boxes on this page — run Tesseract, or no text layer");
    return;
  }
  const out = [];
  for (const line of res.lines || []) {
    const cy = line.y - (line.s || 0) / 2;   // line.y is the baseline
    if (cy < a.box.y || cy > a.box.y + a.box.h) continue;
    const picked = (line.spans || []).filter((sp) => {
      const cx = sp.x + (sp.w || 0) / 2;
      return cx >= a.box.x && cx <= a.box.x + a.box.w;
    }).map((sp) => sp.t);
    if (picked.length) out.push(picked.join(" "));
  }
  if (!out.length) { status("CLIP :: no words inside this region"); return; }
  a.text = out.join("\n");   // clipped OCR words are diplomatic material
  rwSetLayer("text");
  rwDirty(); rwSyncBar();
  status(`CLIP :: ${out.length} line(s) from word boxes`);
}

async function rwSave() {
  if (!rwState.dirty || !rwState.page) return;
  // everything after the await must use THIS context, not whatever page or
  // book the user navigated to while the PUT was in flight — the old code
  // wiped the NEW page's dirty flag and dotted the wrong page button
  const book = rwState.book, src = rwState.src, page = rwState.page;
  const seq = rwState.seq;
  const pageState = rwState.pageState;
  const items = [...rwState.items].sort((a, b) => a.order - b.order)
    .map((it, i) => ({ role: it.role, order: i, box: it.box,
                       text: it.text, norm: it.norm || "" }));
  try {
    const r = await (await fetch(
      `/api/builds/${encodeURIComponent(book)}/ocr-regions`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ src, page, doc: rwState.doc,
                               dims: rwState.dims, state: pageState, items }),
      })).json();
    if (!r.ok) throw new Error(r.error || "save failed");
  } catch (e) {
    status("REGIONS :: " + e.message);
    return;
  }
  // the facsimile caches now describe the old record
  ocrState.regionsCache.clear();
  delete ocrState.layoutMeta[book];
  if (book === rwState.book && src === rwState.src) {
    const has = rwState.regionPages.includes(page);
    if (items.length && !has) rwState.regionPages.push(page);
    if (!items.length && has) {
      rwState.regionPages = rwState.regionPages.filter((p) => p !== page);
    }
    if (items.length && pageState) rwState.regionStates[String(page)] = pageState;
    else delete rwState.regionStates[String(page)];
    const btn = el("rw-pages").querySelector(`[data-page="${page}"]`);
    if (btn) btn.innerHTML = `${page}` + rwPageChip(page);
  }
  if (seq === rwState.seq) {
    rwState.dirty = false;
    // an empty save dropped the record server-side — and any flag with it
    if (!items.length) rwState.pageState = "";
  }
  status(`REGIONS SAVED :: p ${page} · ${items.length}`);
  rwSyncBar();
}

async function rwRecompile() {
  if (!rwState.book) return;
  if (rwState.dirty) { status("RECOMPILE :: save the page first"); return; }
  // the toggled text layer is the layer that compiles: diplomatic body into
  // each record's own doc, the normalized reading into normalized.txt
  const body = { src: rwState.src };
  if (rwState.layer === "norm") body.layer = "norm";
  try {
    const r = await (await fetch(
      `/api/builds/${encodeURIComponent(rwState.book)}/ocr-regions/recompile`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      })).json();
    if (!r.ok) throw new Error(r.error || "recompile failed");
    status(`RECOMPILED :: ${r.pages} page(s) -> ${(r.docs || []).join(", ")}`);
  } catch (e) {
    status("RECOMPILE :: " + e.message);
  }
}

// Electron's renderer does not implement window.prompt (it throws) — a
// minimal inline dialog stands in. Enter = OK, Esc / scrim click = cancel;
// resolves the entered string, or null on cancel, like prompt() did.
function rwPrompt(message, dflt) {
  return new Promise((resolve) => {
    const ov = document.createElement("div");
    ov.className = "overlay";
    ov.innerHTML = `<div class="win rw-prompt">
      <p></p><input class="cad-input" type="text">
      <div class="rw-prompt-btns">
        <button class="cad-btn tiny" type="button" data-ok>OK</button>
        <button class="cad-btn tiny" type="button" data-cancel>Cancel</button>
      </div></div>`;
    ov.querySelector("p").textContent = message;
    const input = ov.querySelector("input");
    input.value = dflt || "";
    const done = (v) => { ov.remove(); resolve(v); };
    ov.querySelector("[data-ok]").addEventListener("click", () => done(input.value));
    ov.querySelector("[data-cancel]").addEventListener("click", () => done(null));
    ov.addEventListener("mousedown", (ev) => { if (ev.target === ov) done(null); });
    input.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter") done(input.value);
      else if (ev.key === "Escape") done(null);
      ev.stopPropagation();
    });
    document.body.appendChild(ov);
    input.focus();
    input.select();
  });
}

async function rwTplSave() {
  if (!rwState.page) return;
  if (rwState.dirty) { status("TEMPLATE :: save the page first"); return; }
  const name = ((await rwPrompt("Template name (e.g. recto, verso):",
                                el("rw-tpl").value || "recto")) || "").trim();
  if (!name) return;
  try {
    const r = await (await fetch(
      `/api/builds/${encodeURIComponent(rwState.book)}/ocr-templates`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ src: rwState.src, name,
                               from_page: rwState.page }),
      })).json();
    if (!r.ok) throw new Error(r.error || "template save failed");
    status(`TEMPLATE :: "${name}" · ${r.items} regions (from p ${rwState.page})`);
  } catch (e) {
    status("TEMPLATE :: " + e.message);
    return;
  }
  await rwLoadTemplates(rwState.seq);
  el("rw-tpl").value = name;
  rwSyncBar();
}

// "121-140, 150" -> [121..140, 150], capped at 500 pages
function rwParsePages(spec) {
  const out = new Set();
  for (const part of String(spec || "").split(",")) {
    const m = part.trim().match(/^(\d+)(?:\s*-\s*(\d+))?$/);
    if (!m) continue;
    const a = +m[1], b = m[2] ? +m[2] : +m[1];
    for (let n = Math.min(a, b); n <= Math.max(a, b) && out.size < 500; n++) {
      if (n >= 1) out.add(n);
    }
  }
  return [...out].sort((a, b) => a - b);
}

async function rwTplApply() {
  const name = el("rw-tpl").value;
  if (!name || !rwState.book) return;
  const spec = await rwPrompt(
    `Apply "${name}" to pages (e.g. 121-140, 150).\n` +
    "Pages that already have regions are skipped. Each stamped region " +
    "pre-fills its text from the word boxes inside it, when the page has any.",
    "");
  const pages = rwParsePages(spec);
  if (!pages.length) return;
  try {
    const r = await (await fetch(
      `/api/builds/${encodeURIComponent(rwState.book)}/ocr-templates/apply`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ src: rwState.src, name, pages }),
      })).json();
    if (!r.ok) throw new Error(r.error || "apply failed");
    status(`TEMPLATE :: applied to ${r.applied.length}` +
           (r.skipped.length ? ` · skipped ${r.skipped.length} (have regions)` : "") +
           (r.clipped.length ? ` · text clipped on ${r.clipped.length}` : ""));
  } catch (e) {
    status("TEMPLATE :: " + e.message);
    return;
  }
  const seq = ++rwState.seq;   // strip + meta changed server-side
  delete ocrState.layoutMeta[rwState.book];
  ocrState.regionsCache.clear();
  await renderReplicaPages(seq);
  if (seq !== rwState.seq) return;
  if (rwState.page) selectReplicaPage(rwState.page);
  rwSyncBar();
}

async function rwTplOutliers() {
  const name = el("rw-tpl").value;
  if (!name || !rwState.book) return;
  try {
    const r = await (await fetch(
      `/api/builds/${encodeURIComponent(rwState.book)}/ocr-templates/outliers`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ src: rwState.src, name }),
      })).json();
    if (!r.ok) throw new Error(r.error || "outlier scan failed");
    rwState.outliers = r.outliers || [];
    for (const btn of el("rw-pages").querySelectorAll(".rw-pagebtn")) {
      const n = +btn.dataset.page;
      btn.innerHTML = `${n}` + rwPageChip(n);
    }
    status(`OUTLIERS :: ${rwState.outliers.length} page(s) break the "${name}" grid` +
           (rwState.outliers.length
             ? ` — ${rwState.outliers.slice(0, 12).join(", ")}` : ""));
  } catch (e) {
    status("OUTLIERS :: " + e.message);
  }
}

function initReplica() {
  el("rw-books").addEventListener("click", (ev) => {
    const li = ev.target.closest("[data-bid]");
    if (li) selectReplicaBook(li.dataset.bid);
  });
  el("rw-pages").addEventListener("click", (ev) => {
    const b = ev.target.closest(".rw-pagebtn");
    if (b) selectReplicaPage(+b.dataset.page);
  });
  el("rw-src").addEventListener("change", async () => {
    if (rwState.dirty && !confirm("Discard unsaved region edits?")) {
      el("rw-src").value = rwState.src;
      return;
    }
    const seq = ++rwState.seq;   // kill any in-flight fetch of the old source
    rwState.src = el("rw-src").value;
    rwState.page = 0; rwState.items = []; rwState.sel = [];
    rwState.dirty = false;
    rwState.outliers = [];       // scores belong to the OLD source's grid
    el("rw-canvas").hidden = true;
    el("rw-empty").hidden = false;
    await renderReplicaPages(seq);
    if (seq !== rwState.seq) return;
    // templates are per source too — source A's names must not stamp B
    await rwLoadTemplates(seq);
    if (seq !== rwState.seq) return;
    rwSyncBar();
  });
  el("rw-overlay").addEventListener("mousedown", rwMouseDown);
  document.addEventListener("mousemove", rwMouseMove);
  document.addEventListener("mouseup", rwMouseUp);
  document.addEventListener("keydown", rwKeyDown);
  el("rw-text").addEventListener("input", () => {
    const a = rwActive();
    if (!a) return;
    if (rwState.layer === "norm") a.norm = el("rw-text").value;
    else a.text = el("rw-text").value;
    rwDirty();
  });
  el("rw-clip").addEventListener("click", rwClipWords);
  el("rw-save").addEventListener("click", rwSave);
  el("rw-recompile").addEventListener("click", rwRecompile);
  el("rw-layer-dipl").addEventListener("click", () => rwSetLayer("text"));
  el("rw-layer-norm").addEventListener("click", () => rwSetLayer("norm"));
  el("rw-normalize").addEventListener("click", () => {
    const a = rwActive();
    if (!a) return;
    const proposal = rwNormalize(a.text);
    // a differing existing norm is human judgement — never silently replace
    if (a.norm && a.norm !== proposal &&
        !confirm("This region already has a normalized reading. Replace it " +
                 "with a fresh mechanical proposal?")) return;
    a.norm = proposal;
    rwSetLayer("norm");
    rwDirty();
  });
  el("rw-tpl").addEventListener("change", rwSyncBar);
  el("rw-tpl-save").addEventListener("click", rwTplSave);
  el("rw-tpl-apply").addEventListener("click", rwTplApply);
  el("rw-tpl-outliers").addEventListener("click", rwTplOutliers);
  el("rw-legend").innerHTML = RW_ROLES.map((r, i) =>
    `<span class="rw-key role-${esc(r)}" data-tip="${esc(r)}">${(i + 1) % 10}</span>`).join("");
}

function initOcrTab() {
  el("ocr-load-file").addEventListener("click", () => el("ocr-file-input").click());
  el("ocr-file-input").addEventListener("change", () => {
    for (const f of el("ocr-file-input").files) {
      const reader = new FileReader();
      reader.onload = () => ocrAddDoc(f.name, reader.result, null, null);
      reader.readAsText(f);
    }
    el("ocr-file-input").value = "";
  });
  el("ocr-filter-verified").addEventListener("click", () => {
    ocrState.verifiedOnly = !ocrState.verifiedOnly;
    renderOcrBooks();
  });
  el("ocr-books").addEventListener("click", (ev) => {
    const group = ev.target.closest("li.build-group");
    if (group) {
      setSetExpanded(group.dataset.gid, !setExpanded(group.dataset.gid));
      renderOcrBooks();
      return;
    }
    const li = ev.target.closest("li.build-item");
    if (li) selectWorkbenchBook(li.dataset.bid);
  });
  el("ocr-docs").addEventListener("click", (ev) => {
    // tree chrome first: extract-text action, then node collapse/expand
    const ex = ev.target.closest("button.src-extract");
    if (ex) { ocrExtractSource(ex.dataset.src); return; }
    const pdf = ev.target.closest("button.artifact-open-pdf");
    if (pdf) { window.open(pdfApiUrl(pdf.dataset.pdf), "_blank"); return; }
    const artifact = ev.target.closest("li.artifact-text");
    if (artifact) {
      openTextArtifact(artifact.dataset.artifactKind, artifact.dataset.artifactName);
      return;
    }
    const captureRow = ev.target.closest("li.capture-img-row");
    if (captureRow) {
      if (captureRow.dataset.captureAvailable)
        window.open("/api/capture/image?path=" +
          encodeURIComponent(captureRow.dataset.capturePath), "_blank");
      return;
    }
    // an extracted-image row isn't a document to select — open it full size
    const imgRow = ev.target.closest("li.ocr-img-row");
    if (imgRow) {
      window.open(`/api/builds/${encodeURIComponent(ocrState.book)}/ocr/images/` +
        encodeURIComponent(imgRow.dataset.imgName), "_blank");
      return;
    }
    const srcLi = ev.target.closest("li.ocr-src");
    if (srcLi) {
      if (!srcLi.dataset.src) return;   // the local-files node doesn't fold
      const k = `${ocrState.book}:${srcLi.dataset.src}`;
      if (ocrState.treeCollapsed.has(k)) ocrState.treeCollapsed.delete(k);
      else ocrState.treeCollapsed.add(k);
      renderOcrDocs();
      return;
    }
    const li = ev.target.closest("li.ocr-doc");
    if (!li) return;
    selectOcrDocument(li.dataset.did);
  });
  el("ocr-view-edit").addEventListener("click", () => setOcrView("edit"));
  el("ocr-view-diff").addEventListener("click", () => setOcrView("diff"));
  el("ocr-view-pdf").addEventListener("click", () =>
    setOcrView(ocrState.view === "pdf" ? "edit" : "pdf"));
  el("ocr-pages").addEventListener("input", onOcrPageInput);
  // page-view shortcuts: click selects, Ctrl+click extends the range,
  // digits STAGE the selection/hovered page, T marks a title page,
  // Delete removes selected pages from the PDF. These come FIRST: they are the
  // load-bearing handlers, and anything that throws above them takes them out.
  el("ocr-pages").addEventListener("mouseover", (ev) => {
    const row = ev.target.closest(".ocr-pgrow");
    if (row) ocrHoverPage = +row.dataset.page;
  });
  el("ocr-pages").addEventListener("mouseleave", () => { ocrHoverPage = 0; });
  el("ocr-pages").addEventListener("click", onOcrPagesClick);
  document.addEventListener("keydown", onOcrPagesKey);
  // jump-to-page box + a scroll-tracked "page N / total" readout
  el("ocr-page-jump").addEventListener("keydown", (ev) => {
    if (ev.key !== "Enter") return;
    ev.preventDefault();
    const n = parseInt(el("ocr-page-jump").value, 10);
    if (n) ocrScrollToPage(n);
  });
  let ocrPageRaf = 0;
  el("ocr-pages").addEventListener("scroll", () => {
    if (ocrPageRaf) return;
    ocrPageRaf = requestAnimationFrame(() => { ocrPageRaf = 0; ocrSyncPageInput(ocrTopPage()); });
  }, { passive: true });
  ocrState.layout = state.settings.ocrLayout !== false;   // layout is home
  el("ocr-layout").addEventListener("click", () => setOcrLayout(!ocrState.layout));
  el("ocr-furniture").addEventListener("click",
    () => setOcrFurniture(!state.settings.ocrFurniture));
  // reflect the default view (page view, layout on) in the toolbar/panes —
  // the tab isn't visible yet, so no page render fires here
  setOcrView(ocrState.view);
  // page-image failures (e.g. PyMuPDF not installed — 501) must be visible,
  // not a wall of broken-image icons; error events don't bubble, so capture
  el("ocr-pages").addEventListener("error", (ev) => {
    if (ev.target.tagName !== "IMG") return;
    const box = el("ocr-pages");
    if (box.querySelector(".ocr-pgerr")) return;
    const n = document.createElement("div");
    n.className = "ocr-pgnote ocr-pgerr empty";
    n.textContent = "Page images unavailable — check the server log " +
      "(PyMuPDF must be installed: pip install -r tools/requirements.txt)";
    box.prepend(n);
  }, true);
  el("ocr-diff-with").addEventListener("change", () => {
    if (ocrState.view === "diff") renderOcrDiff();
  });
  el("ocr-find-next").addEventListener("click", ocrFindNext);
  el("ocr-find").addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") { ev.preventDefault(); ocrFindNext(); }
  });
  el("ocr-replace-all").addEventListener("click", ocrReplaceAll);
  el("ocr-quality").addEventListener("change", () =>
    ocrSetQuality(el("ocr-quality").value));
  el("ocr-save").addEventListener("click", ocrSaveDoc);
  el("ocr-set-active").addEventListener("click", ocrSetActive);
  // the Workbench opens on the phase it was left on
  setWorkbenchPhase(state.settings.workbenchPhase || "record", false);
  // the queue's service select starts on the configured default and keeps
  // the setting in sync when changed here
  el("ocr-service").value = state.settings.ocrService || "tesseract";
  el("ocr-service").addEventListener("change", () => {
    state.settings.ocrService = el("ocr-service").value;
    saveSettings();
    updateDefaultEngineSummary();
  });
  el("analysis-service").value = state.settings.textAnalysisService || "configured";
  el("analysis-service").addEventListener("change", () => {
    state.settings.textAnalysisService = el("analysis-service").value;
    saveSettings();
    updateDefaultEngineSummary();
  });
  el("engine-defaults-open").addEventListener("click", openDefaultEngines);
  el("engine-close").addEventListener("click", closeDefaultEngines);
  el("engine-done").addEventListener("click", closeDefaultEngines);
  el("engine-settings").addEventListener("click", () => {
    closeDefaultEngines();
    el("open-settings").click();
  });
  el("engine-overlay").addEventListener("click", (ev) => {
    if (ev.target === el("engine-overlay")) closeDefaultEngines();
  });
  updateDefaultEngineSummary();
  el("ocr-queue-add").addEventListener("click", ocrQueueJob);
  el("ocr-queue-clear").addEventListener("click", clearOcrStaging);
  el("ocr-submit").addEventListener("click", ocrSubmitStaged);
  el("ocr-queue-stop").addEventListener("click", cancelAllOcrJobs);
  el("ocr-queue-rows").addEventListener("click", (ev) => {
    const btn = ev.target.closest("[data-ocr-cancel]");
    if (btn) cancelOcrJob(btn.dataset.ocrCancel);
    const unified = ev.target.closest("[data-job-cancel]");
    if (unified) cancelJob(unified.dataset.jobCancel);
  });
  el("ocr-del-pages").addEventListener("click", deleteSelectedPages);
  el("ocr-editor").addEventListener("input", () => {
    const d = ocrSelDoc();
    if (d) d.text = el("ocr-editor").value;
  });
}

// --- smart check: extract real metadata from the book's own PDF -------------------
// Any record with a reachable PDF gets a magic-wand button. It asks the server
// to OCR the PDF's front matter (Mistral) and extract bibliographic fields
// (DeepSeek — the phone capture's exact chain), then holds the result as a
// PENDING overlay: extracted values render in light yellow with a dotted
// border, TAB flips the view back to the originals, and SPACE over a hovered
// row bakes the fields into the record. Bakes are logged in History with full
// before-snapshots (revertible), and consecutive bakes within SC_GROUP_MS fold
// into ONE grouped history entry — bulk cleanups revert as a unit.

let scChecks = {};          // target -> pending record (server: smart_checks.json)
let scShow = true;          // TAB view: true = extracted values visible
const scJobs = new Map();   // target -> { id } while a check runs
let scPollTimer = null;
let scGroup = null;         // open bake fold: { opId, ts0, items }
const SC_GROUP_MS = 5 * 60 * 1000;

async function loadSmartChecks() {
  try {
    const r = await (await fetch("/api/smartcheck")).json();
    scChecks = (r && r.pending) || {};
  } catch (e) { scChecks = {}; }
}

function scHasPending() {
  // records only — a still-running job has nothing to toggle yet, and must
  // not make TAB stop moving focus
  return Object.keys(scChecks).length > 0;
}

// -- targets and their PDFs ---------------------------------------------------
// A target names one book record ("whl:12", "build:<id>", "manual:<id>",
// "checked:<key>"); the PDF reference is resolved fresh at click time.

function scTargetForRow(row) {
  return (row.kind === "manual" ? "manual:" : "checked:") + row.id;
}

function scPdfForRow(row) {
  if (row.localPdf) return { pdf: row.localPdf };
  const ident = iaIdentifierForRow(row);
  if (ident && dlState(row) === "done")
    return { pdf: "downloads/ia/" + ident + ".pdf" };
  if (ident) return { url: `https://archive.org/download/${ident}/${ident}.pdf` };
  return null;
}

function scPdfForWhl(r) {
  return !r.added && r.file ? { url: r.file } : null;
}

function scPdfForBuild(b) {
  const f = String(b.pdf_file || "").trim();
  if (f) return { pdf: f };
  const u = String(b.pdf_source || "").trim();
  if (/^https?:\/\//i.test(u)) return { url: u };
  return null;
}

// the fields of a pending record that actually differ from the current values
// ({} = record matches the book; null = no pending record at all)
function scProvFields(target, cur) {
  const rec = scChecks[target];
  if (!rec) return null;
  const out = {};
  for (const [f, v] of Object.entries(rec.mapped || {})) {
    const val = String(v == null ? "" : v).trim();
    if (!val) continue;
    const c = cur(f);
    if (String(c == null ? "" : c).trim() !== val) out[f] = val;
  }
  return out;
}

// -- the wand -----------------------------------------------------------------

// what/when/how of a pending record, for the wand tooltip — the audit data
// the server stores is surfaced right where the user decides to bake
function scProvenanceTip(rec) {
  const eng = rec.engine || {};
  const lines = [`Extracted by ${eng.ocr || "?"} + ${eng.extract || "?"}` +
    (rec.pages_ocred && rec.pages_ocred.length
      ? ` from PDF page(s) ${rec.pages_ocred.join(", ")}` : "") +
    (rec.created_at ? ` · ${rec.created_at}` : "")];
  const extra = Object.entries(rec.extra || {}).slice(0, 4);
  if (extra.length) {
    lines.push("Also found: " + extra.map(([k, v]) =>
      `${k}: ${String(v).slice(0, 40)}`).join(" · "));
  }
  return lines.join("\n");
}

function scWandHtml(target, pdfref, prov) {
  const rec = scChecks[target];
  if (!pdfref && !rec && !scJobs.has(target)) return "";
  let cls = "";
  let tip = "Smart check — extract real metadata from this book's PDF\n" +
    "(Mistral OCR, then DeepSeek). Results overlay the row until baked.";
  if (scJobs.has(target)) {
    cls = " sc-running";
    tip = "Smart check running — reading the PDF's front matter…";
  } else if (rec && prov && Object.keys(prov).length) {
    cls = " sc-ready";
    tip = `Smart check: ${Object.keys(prov).length} field(s) differ — ` +
      "the light-yellow dotted cells are extracted values\n" +
      scProvenanceTip(rec) + "\n" +
      "TAB compares with the original · SPACE over the row bakes them in\n" +
      "Shift+click dismisses · click re-runs";
  } else if (rec) {
    cls = " sc-match";
    tip = "Smart check: the PDF agrees with this record\n" +
      scProvenanceTip(rec) + "\n" +
      "Shift+click dismisses · click re-runs";
  }
  return `<span class="sc-wand${cls}" data-sc-run="${esc(target)}"` +
    ` data-tip="${esc(tip)}">${ICONS.wand}</span>`;
}

// -- row decoration (runs inside the row builders on every render) -------------

// whlRowCells' fixed cell order (src, ...fields..., status, copyright)
const SC_WHL_CELL = { title: 1, subtitle: 2, authors: 3, year: 4, publisher: 5,
                      pages: 6, language: 7, subject: 8, description: 9 };

function scDecorateWhlRow(tr, r, mode) {
  if (mode === "orig") return;         // Alt view shows the pre-correction record
  const target = "whl:" + r.idx;
  const pdfref = scPdfForWhl(r);
  const prov = scProvFields(target, (f) => r[f]);
  const wand = scWandHtml(target, pdfref, prov);
  if (wand) {
    // the title cell: the badge columns are width-locked and cannot take it
    const td = tr.children[1];
    if (td) td.insertAdjacentHTML("beforeend", wand);
  }
  if (!scShow || !prov) return;
  for (const [f, v] of Object.entries(prov)) {
    const td = tr.children[SC_WHL_CELL[f]];
    if (!td) continue;
    const keep = f === "title" ? td.querySelector(".sc-wand") : null;
    td.textContent = v;
    if (keep) td.appendChild(keep);
    td.classList.add("sc-prov");
    td.classList.remove("missing-core");
    td.dataset.tip = "Smart check — original: " +
      (String(r[f] || "").trim() || "(blank)");
  }
}

function scDecorateCheckedRow(tr, row) {
  const target = scTargetForRow(row);
  const pdfref = scPdfForRow(row);
  const prov = scProvFields(target, (f) => (row.book || {})[f]);
  const wand = scWandHtml(target, pdfref, prov);
  if (wand) {
    // the title cell: the badge columns are width-locked and cannot take it
    const td = tr.children[1];
    if (td) td.insertAdjacentHTML("beforeend", wand);
  }
  if (!scShow || !prov) return;
  for (const [f, v] of Object.entries(prov)) {
    const i = BOOK_COLS.indexOf(f);
    const td = i >= 0 ? tr.children[1 + i] : null;
    if (!td) continue;
    const keep = f === "title" ? td.querySelector(".sc-wand") : null;
    td.textContent = v;
    if (keep) td.appendChild(keep);
    td.classList.add("sc-prov");
    td.classList.remove("missing-core");
    td.dataset.tip = "Smart check — original: " +
      (String((row.book || {})[f] || "").trim() || "(blank)");
  }
}

// -- Editor form overlay --------------------------------------------------------
// Overlaid inputs go readOnly (bake or TAB back to edit); scBuildOrig remembers
// what we wrote so the overlay never survives into a save or another build.

let scBuildOrig = null;    // { id, vals: {f: original}, set: {f: provisional} }

function scClearBuildOverlay() {
  const saved = scBuildOrig;
  scBuildOrig = null;
  for (const f of BUILD_FIELDS) {
    const inp = el("b-" + f);
    if (!inp || !inp.classList.contains("sc-prov")) continue;
    inp.classList.remove("sc-prov");
    inp.readOnly = false;
    delete inp.dataset.tip;
    // restore only what we wrote — a re-rendered or re-selected form keeps
    // its own fresh values
    if (saved && saved.id === state.buildSel && inp.value === saved.set[f]) {
      inp.value = saved.vals[f];
    }
  }
}

function scApplyBuildOverlay() {
  scClearBuildOverlay();
  const btn = el("b-smartcheck");
  if (btn) btn.classList.remove("sc-running", "sc-ready", "sc-match");
  const b = currentBuild();
  if (!b) return;
  const target = "build:" + b.id;
  const prov = scProvFields(target, (f) => b[f]);
  if (btn) {
    if (scJobs.has(target)) btn.classList.add("sc-running");
    else if (prov && Object.keys(prov).length) btn.classList.add("sc-ready");
    else if (scChecks[target]) btn.classList.add("sc-match");
  }
  if (!scShow || !prov || !Object.keys(prov).length) return;
  scBuildOrig = { id: b.id, vals: {}, set: {} };
  for (const [f, v] of Object.entries(prov)) {
    const inp = el("b-" + f);
    if (!inp) continue;
    scBuildOrig.vals[f] = inp.value;
    scBuildOrig.set[f] = v;
    inp.value = v;
    inp.classList.add("sc-prov");
    inp.readOnly = true;
    inp.dataset.tip = "Smart check — original: " +
      (String(b[f] || "").trim() || "(blank)") +
      "\nSPACE over the form bakes · TAB shows the original";
  }
}

function scRerender() {
  if (state.whlRows) renderWhlTop();
  renderChecked();
  scApplyBuildOverlay();
  // the footer tag is the persistent "provisional data exists" indicator
  const n = Object.keys(scChecks).length;
  const foot = el("status-smart");
  if (foot) {
    foot.hidden = !n && !scJobs.size;
    foot.textContent = scJobs.size
      ? `SMART CHECK: ${scJobs.size} running`
      : `SMART CHECK: ${n} held (${scShow ? "shown" : "hidden"})`;
  }
}

// -- running a check ------------------------------------------------------------

function scRunInfo(target) {
  const kind = target.split(":")[0];
  const ident = target.slice(kind.length + 1);
  if (kind === "whl") {
    const row = whlRowByIdx(parseInt(ident, 10));
    return row ? { pdfref: scPdfForWhl(row), label: row.title || "" } : null;
  }
  if (kind === "build") {
    const b = state.builds[ident];
    return b ? { pdfref: scPdfForBuild(b), label: b.title || "" } : null;
  }
  const row = state.rowsById.get(String(ident));
  if (row) return { pdfref: scPdfForRow(row), label: (row.book || {}).title || "" };
  return null;
}

async function startSmartCheck(target, pdfref, label) {
  const body = Object.assign({ target, label }, pdfref);
  let data;
  try {
    const res = await fetch("/api/smartcheck/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    data = await res.json().catch(() => ({}));
    if (!res.ok || !data.ok) throw new Error(data.error || "HTTP " + res.status);
  } catch (e) {
    statusErr(`SMART CHECK FAILED :: ${e.message || e}`);
    return;
  }
  scJobs.set(target, { id: data.job.id });
  scEnsurePolling();
  scRerender();
  status(`SMART CHECK :: ${label || target} — reading the PDF`);
}

function scEnsurePolling() {
  if (scPollTimer) return;
  scPollTimer = setInterval(async () => {
    if (document.hidden || !scJobs.size) {
      if (!scJobs.size) { clearInterval(scPollTimer); scPollTimer = null; }
      return;
    }
    for (const [target, j] of [...scJobs]) {
      let r;
      try {
        const res = await fetch("/api/smartcheck/job/" + j.id);
        if (res.status === 404) {   // gone from both registries: give up now
          scJobs.delete(target);
          statusErr("SMART CHECK LOST :: the server no longer knows this job");
          scRerender();
          continue;
        }
        r = await res.json();
        j.bad = 0;
      } catch (e) {
        // a restarting/unreachable server: retry a few times, then let go
        j.bad = (j.bad || 0) + 1;
        if (j.bad >= 8) {
          scJobs.delete(target);
          statusErr("SMART CHECK LOST :: no answer from the server");
          scRerender();
        }
        continue;
      }
      const state2 = r.state || "";
      if (state2 === "queued" || state2 === "running" || state2 === "cancelling") continue;
      scJobs.delete(target);
      if (String(r.status || "").startsWith("done")) {
        await loadSmartChecks();
        scShow = true;
        status(`SMART CHECK DONE :: ${r.label || target} — ` +
          "TAB compares, SPACE over the row bakes");
      } else if (r.status === "cancelled") {
        status(`SMART CHECK CANCELLED :: ${r.label || target}`);
      } else {
        statusErr(`SMART CHECK FAILED :: ${r.error || r.status || "lost"}`);
      }
      scRerender();
    }
  }, 1500);
}

async function scResolve(target, action, applied) {
  try {
    await fetch("/api/smartcheck/resolve", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ target, action, applied: applied || {} }),
    });
  } catch (e) { /* the pending record just stays; harmless */ }
}

async function scWandClick(target, ev) {
  if (ev.shiftKey && scChecks[target]) {
    await scResolve(target, "dismissed", {});
    delete scChecks[target];
    scRerender();
    status("SMART CHECK :: dismissed");
    return;
  }
  if (scJobs.has(target)) { status("SMART CHECK :: already running"); return; }
  // a re-run replaces the held result and spends OCR/AI calls — make sure
  if (scChecks[target] && state.settings.confirmDiscard !== false &&
      !window.confirm("Re-run the smart check for this book? " +
        "The held result will be replaced.")) return;
  const info = scRunInfo(target);
  if (!info || !info.pdfref) {
    statusErr("SMART CHECK :: no reachable PDF for this book");
    return;
  }
  await startSmartCheck(target, info.pdfref, info.label);
}

// -- baking: provisional fields -> the record, grouped + revertible ---------------

async function scRevertItem(it) {
  switch (it.kind) {
    case "whl": return !!(await whlApplySnaps(it.idx, it.beforeSnaps));
    case "build-fields": return !!(await patchBuildRaw(it.id, it.before));
    case "manual-fields": return !!(await patchManualFields(it.id, it.before));
    case "checked": restoreChecked(it.key, it.before); return true;
    default: return false;
  }
}

async function scRevertItems(items) {
  let ok = true;
  for (const it of [...items].reverse()) ok = (await scRevertItem(it)) && ok;
  renderChecked();
  return ok;
}

async function scReplayItem(it) {
  switch (it.kind) {
    case "whl": return !!(await whlPost({ idx: it.idx, fields: it.after }));
    case "build-fields": return !!(await patchBuildRaw(it.id, it.after));
    case "manual-fields": return !!(await patchManualFields(it.id, it.after));
    case "checked": {
      const entry = state.checked.get(it.key);
      if (!entry) return false;
      // mirror the original bake exactly: merged fields invalidate the
      // stale checks/scans/verify and requeue the scan
      entry.book = Object.assign({}, entry.book, it.after);
      entry.edited = true;
      entry.checks = null;
      entry.scans = null;
      entry.verify = null;
      queueScan(it.key);
      saveChecked();
      return true;
    }
    default: return false;
  }
}

async function scReplayItems(items) {
  let ok = true;
  for (const it of items) ok = (await scReplayItem(it)) && ok;
  renderChecked();
  return ok;
}

// One bake = one history entry; bakes landing within SC_GROUP_MS of the group's
// start fold into it (same pattern commitEdit uses for set declarations), so
// "cleaned up 30 books" is ONE revertible History row.
function scGroupLog(item, label) {
  const now = Date.now();
  const lr = actionLog();
  const top = history.stack[history.ptr - 1];
  const last = lr.length ? lr[lr.length - 1] : null;
  const open = scGroup && top && top.id === scGroup.opId &&
    last && last.id === scGroup.opId && !last.reverted &&
    last.revert && now - scGroup.ts0 < SC_GROUP_MS;
  if (open) {
    scGroup.items.push(item);
    last.revert.items = scGroup.items;   // re-link (logAction may have re-parsed)
    last.label = `bake smart check into ${scGroup.items.length} books`;
    last.tkey = "";   // a grown group spans records; no single conflict key
    saveActionLog();
    const items = scGroup.items;
    top.label = last.label;
    top.undoFn = () => scRevertItems(items);
    top.redoFn = () => scReplayItems(items);
    updateHistoryButtons();
    if (activeBottomTable() === "history") renderBottomRows();
    return;
  }
  const items = [item];
  const opId = pushOp(`bake smart check into ${String(label || "").slice(0, 40)}`,
    () => scRevertItems(items),
    () => scReplayItems(items),
    { kind: "smartbake", items });
  scGroup = { opId, ts0: now, items };
}

let scBaking = false;   // re-entry guard: a held SPACE must not double-bake

async function scBakeTarget(target) {
  const rec = scChecks[target];
  if (!rec || scBaking) return false;
  scBaking = true;
  try {
    return await scBakeTargetInner(target, rec);
  } finally {
    scBaking = false;
  }
}

async function scBakeTargetInner(target, rec) {
  const kind = target.split(":")[0];
  const ident = target.slice(kind.length + 1);
  let prov = null;
  let item = null;
  let label = rec.label || "";
  if (kind === "whl") {
    const idx = parseInt(ident, 10);
    const row = whlRowByIdx(idx);
    if (!row) { statusErr("SMART CHECK :: row not loaded"); return false; }
    label = label || row.title || "";
    prov = scProvFields(target, (f) => row[f]);
    if (prov && Object.keys(prov).length) {
      const snaps = whlFieldSnaps(row, Object.keys(prov));
      if (!(await whlPost({ idx, fields: prov }))) {
        statusErr("BAKE FAILED"); return false;
      }
      item = { kind: "whl", idx, beforeSnaps: snaps, after: prov };
    }
  } else if (kind === "build") {
    const b = state.builds[ident];
    if (!b) { statusErr("SMART CHECK :: entry not loaded"); return false; }
    label = label || b.title || ident;
    prov = scProvFields(target, (f) => b[f]);
    if (prov && Object.keys(prov).length) {
      const before = {};
      for (const f of Object.keys(prov)) before[f] = b[f] || "";
      scClearBuildOverlay();   // the form must not hold overlay values through a patch
      // quiet: a full renderUpload/renderBuildEditor would wipe the user's
      // unsaved edits in OTHER fields — refresh only the inputs we baked
      if (!(await patchBuildRaw(ident, prov, true))) {
        statusErr("BAKE FAILED"); return false;
      }
      for (const [f, v] of Object.entries(prov)) {
        const inp = el("b-" + f);
        if (inp) inp.value = v;
      }
      item = { kind: "build-fields", id: ident, before, after: prov };
    }
  } else if (kind === "manual") {
    const entry = state.manual.find((e) => String(e.id) === ident);
    if (!entry) { statusErr("SMART CHECK :: entry not loaded"); return false; }
    label = label || entry.title || "";
    prov = scProvFields(target, (f) => entry[f]);
    if (prov && Object.keys(prov).length) {
      const before = {};
      for (const f of Object.keys(prov)) before[f] = String(entry[f] || "");
      if (!(await patchManualFields(ident, prov))) {
        statusErr("BAKE FAILED"); return false;
      }
      item = { kind: "manual-fields", id: ident, before, after: prov };
    }
  } else {   // checked
    const entry = state.checked.get(ident);
    if (!entry) { statusErr("SMART CHECK :: row not loaded"); return false; }
    label = label || (entry.book || {}).title || "";
    prov = scProvFields(target, (f) => (entry.book || {})[f]);
    if (prov && Object.keys(prov).length) {
      const before = snapshotChecked(ident);
      entry.book = Object.assign({}, entry.book, prov);
      entry.edited = true;
      entry.checks = null;
      entry.scans = null;
      entry.verify = null;
      queueScan(ident);
      saveChecked();
      item = { kind: "checked", key: ident, before, after: prov };
    }
  }
  if (item) scGroupLog(item, label);
  await scResolve(target, "baked", prov || {});
  delete scChecks[target];
  scRerender();
  status(item
    ? `BAKED :: ${label} — ${Object.keys(prov).length} field(s) updated`
    : `SMART CHECK :: ${label} — matches, nothing to change`);
  return true;
}

// -- keyboard: TAB toggles the view, SPACE over a row bakes it --------------------

function scHoverTarget() {
  const tr = document.querySelector("#whltop-rows tr:hover, #checked-rows tr:hover");
  if (tr) {
    if (tr.dataset.widx !== undefined) return "whl:" + tr.dataset.widx;
    if (tr.dataset.rowId) {
      const row = state.rowsById.get(String(tr.dataset.rowId));
      if (row) return scTargetForRow(row);
    }
    return null;
  }
  if (document.querySelector("#build-editor:hover")) {
    const b = currentBuild();
    if (b) return "build:" + b.id;
  }
  return null;
}

function onSmartKey(ev) {
  if (ev.ctrlKey || ev.metaKey || ev.altKey || ev.repeat) return;
  const t = ev.target;
  if (/^(INPUT|TEXTAREA|SELECT|BUTTON|A)$/.test(t.tagName) || t.isContentEditable) return;
  // never fight a modal (settings, wizard, viewers all use .overlay)
  if (document.querySelector(".overlay:not([hidden])")) return;
  if (ev.key === "Tab") {
    if (!scHasPending()) return;      // nothing provisional: TAB moves focus
    ev.preventDefault();
    scShow = !scShow;
    scRerender();
    status(scShow
      ? "SMART CHECK :: extracted values shown — SPACE over a row bakes them in"
      : "SMART CHECK :: original values shown — TAB switches back");
    return;
  }
  if (ev.key === " ") {
    const target = scHoverTarget();
    if (!target || !scChecks[target]) return;   // plain space scrolls as usual
    ev.preventDefault();
    if (!scShow) {
      // never bake values the user can't see
      status("SMART CHECK :: extracted values are hidden — TAB shows them first");
      return;
    }
    scBakeTarget(target);
  }
}

function initSmartCheck() {
  loadSmartChecks().then(scRerender);
  document.addEventListener("keydown", onSmartKey);
  // capture phase: a wand click must not double as a row click
  for (const id of ["whltop-rows", "checked-rows"]) {
    el(id).addEventListener("click", (ev) => {
      const w = ev.target.closest("[data-sc-run]");
      if (!w) return;
      ev.preventDefault();
      ev.stopPropagation();
      scWandClick(w.dataset.scRun, ev);
    }, true);
  }
  el("b-smartcheck").addEventListener("click", (ev) => {
    // drop focus so SPACE stays the bake key, not a button re-click
    ev.currentTarget.blur();
    const b = currentBuild();
    if (b) scWandClick("build:" + b.id, ev);
  });
  el("status-smart").addEventListener("click", async (ev) => {
    // Shift+click = dismiss everything held — the escape hatch for records
    // whose row is gone (an unchecked book has no wand left to click)
    if (ev.shiftKey) {
      const targets = Object.keys(scChecks);
      if (!targets.length) return;
      if (!window.confirm(`Dismiss all ${targets.length} held smart-check ` +
        "result(s)? Nothing already baked is affected.")) return;
      for (const t of targets) {
        await scResolve(t, "dismissed", {});
        delete scChecks[t];
      }
      scRerender();
      status("SMART CHECK :: all held results dismissed");
      return;
    }
    scShow = !scShow;
    scRerender();
  });
}

// --- menu bar ---------------------------------------------------------------

// publish the master list (plus manual entries) to the configured Google
// Sheet — always a manual, user-prompted action
async function syncMasterList() {
  status("SYNCING MASTER LIST TO GOOGLE SHEETS ...");
  try {
    const res = await fetch("/api/master/sync", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        spreadsheet_id: state.settings.gsSpreadsheetId || "",
        service_account_file: state.settings.gsKeyFile || "",
        sheet_name: state.settings.gsSheetName || "Master list",
      }),
    });
    const data = await res.json().catch(() => ({}));
    if (data.ok) status(`MASTER LIST SYNCED :: ${data.rows} ROWS`);
    else statusCrit(`SYNC FAILED :: ${data.error || "?"}`);
  } catch (e) {
    statusCrit("SYNC FAILED");
  }
}

const MENU_CMDS = {
  "export": () => exportJson(),
  "export-builds": () => exportBuilds(),
  "dl-sources": () => downloadUploadList(),
  "master-sync": () => syncMasterList(),
  "settings": () => openSettings(),
  "undo": () => undo(),
  "redo": () => redo(),
  "search-pane": () => setSearchPane(!state.settings.showCatalog),
  "table-checked": () => switchTopTable("checked"),
  "table-whl": () => switchTopTable("whl"),
  "run-scans": () => runScansBatch(),
  "scrape": () => startWhlScrape(),
  "dl-approved": () => downloadApproved(),
  "setup-guide": () => showWizard(),
  "changelog": () => openChangelog(),
  "site-home": () => openWebView("https://maj-6.github.io/library-tool/"),
  "about": () => openAbout(),
  // File > New entry: same flow as the Workbench toolbar's + button, but
  // reachable from anywhere — land on the Record phase so the form is visible.
  "new-entry": () => {
    const t = document.querySelector('#tabs .tab[data-tab="workbench"]');
    if (t) t.click();
    setWorkbenchPhase("record", false);
    createBuild({}, "(blank)");
  },
  // quick settings (mirror the Settings-dialog handlers, side effects and all)
  "opt-auto-ia": () => {
    const on = state.settings.autoIaDownload === false;   // was off -> turning on
    state.settings.autoIaDownload = on;
    if (!on) { state.autoDlQueue = []; updateDlProgress(); }   // off: drop the queue
    saveSettings();
  },
  "opt-expand-sets": () => {
    state.settings.expandSets = !state.settings.expandSets;
    saveSettings();
    renderChecked();                                       // the checked table shows sets
  },
};

// Settings > (themes): generated from THEMES so adding a theme needs no markup.
// Each gets a MENU_CMDS entry, since the menu dispatcher is a data-cmd lookup.
function buildThemeMenu() {
  const host = el("menu-themes");
  if (!host) return;
  host.innerHTML = "";
  for (const [id, label] of THEMES) {
    const cmd = "theme:" + id;
    MENU_CMDS[cmd] = () => setTheme(id);
    const b = document.createElement("button");
    b.type = "button";
    b.className = "menu-item";
    b.dataset.cmd = cmd;
    b.innerHTML = `<span class="menu-check"></span>${esc(label)}`;
    host.appendChild(b);
  }
}

// Settings > OCR engine: the same generated-picker pattern as themes. The value
// lives in TWO <select>s (the OCR toolbar + the Settings dialog); keep both in
// step, exactly as the dialog's onchange does.
function setOcrService(id) {
  state.settings.ocrService = id;
  saveSettings();
  const q = el("ocr-service"); if (q) q.value = id;
  const s = el("set-ocr-service"); if (s) s.value = id;
  updateDefaultEngineSummary();
}
function buildOcrMenu() {
  const host = el("menu-ocr-service");
  if (!host) return;
  host.innerHTML = "";
  for (const [id, label] of OCR_SERVICES) {
    const cmd = "ocrsvc:" + id;
    MENU_CMDS[cmd] = () => setOcrService(id);
    const b = document.createElement("button");
    b.type = "button";
    b.className = "menu-item";
    b.dataset.cmd = cmd;
    b.innerHTML = `<span class="menu-check"></span>${esc(label)}`;
    host.appendChild(b);
  }
}

function updateMenuState() {
  const onChecked = state.settings.topTable === "checked";
  const dis = (cmd, v) => {
    const b = document.querySelector(`.menu-item[data-cmd="${cmd}"]`);
    if (b) b.disabled = !!v;
  };
  const check = (cmd, v) => {
    const b = document.querySelector(`.menu-item[data-cmd="${cmd}"] .menu-check`);
    if (b) b.classList.toggle("on", !!v);
  };
  dis("export", !onChecked);
  dis("run-scans", !onChecked);
  dis("dl-approved", !onChecked);
  dis("scrape", scrapeRunning);
  dis("undo", history.ptr === 0);
  dis("redo", history.ptr >= history.stack.length);
  check("search-pane", state.settings.showCatalog);
  check("table-checked", onChecked);
  check("table-whl", !onChecked);
  check("opt-auto-ia", state.settings.autoIaDownload !== false);   // default-on
  check("opt-expand-sets", !!state.settings.expandSets);
  for (const [id] of THEMES) check("theme:" + id, id === state.settings.theme);
  const svc = state.settings.ocrService || "tesseract";
  for (const [id] of OCR_SERVICES) check("ocrsvc:" + id, id === svc);
}

function initMenubar() {
  buildThemeMenu();
  buildOcrMenu();
  const menus = [...document.querySelectorAll("#menubar .menu")];
  let openMenu = null;
  const closeAll = () => {
    for (const m of menus) {
      m.querySelector(".menu-drop").hidden = true;
      m.querySelector(".menu-btn").classList.remove("active");
    }
    openMenu = null;
  };
  const openOne = (m) => {
    closeAll();
    updateMenuState();
    m.querySelector(".menu-drop").hidden = false;
    m.querySelector(".menu-btn").classList.add("active");
    openMenu = m;
  };
  for (const m of menus) {
    const btn = m.querySelector(".menu-btn");
    btn.addEventListener("mousedown", (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      closePopup();  // stopPropagation would keep the filter/columns popup open
      if (openMenu === m) closeAll();
      else openOne(m);
    });
    btn.addEventListener("mouseenter", () => {
      if (openMenu && openMenu !== m) openOne(m);
    });
  }
  document.addEventListener("mousedown", (ev) => {
    if (openMenu && !ev.target.closest("#menubar")) closeAll();
  });
  document.addEventListener("click", (ev) => {
    const item = ev.target.closest("#menubar .menu-item");
    if (!item || item.disabled) return;
    if (item.classList.contains("menu-sub")) return;   // a submenu parent only opens its child (on hover)
    closeAll();
    const cmd = MENU_CMDS[item.dataset.cmd];
    if (cmd) cmd();
  });
  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape" && openMenu) closeAll();
    // Ctrl/Cmd+, opens Preferences (the desktop convention)
    if ((ev.ctrlKey || ev.metaKey) && ev.key === ",") {
      ev.preventDefault();
      openSettings();
    }
  });
}

// --- wire up ---------------------------------------------------------------

// --- embedded web view -------------------------------------------------------
// Links that would open a new browser tab open here instead: a proxied,
// SANDBOXED iframe. /api/webview strips the X-Frame-Options that block framing;
// the sandbox has no allow-same-origin, so the proxied page's scripts run
// isolated and cannot reach the app. Ctrl/Cmd+click still opens a real tab.
function openWebView(url) {
  if (!/^https?:\/\//i.test(url)) return false;
  // In the desktop shell a plain web link belongs in the real browser, not an
  // embedded frame — many sites refuse framing, and the OS browser is right
  // there. The Internet Archive viewer is a different path (a curated feature
  // with local preview + downloads) and stays in-app on desktop.
  const d = window.whlDesktop;
  if (d && d.isDesktop && d.openExternal) { d.openExternal(url); return true; }
  el("webview-url").textContent = url;
  el("webview-url").dataset.url = url;
  el("webview-frame").src = "/api/webview?url=" + encodeURIComponent(url);
  el("webview-overlay").hidden = false;
  return true;
}
function closeWebView() {
  el("webview-overlay").hidden = true;
  el("webview-frame").src = "about:blank";   // stop loading and drop the page
}
function reloadWebView() {
  const u = el("webview-url").dataset.url;
  if (u) el("webview-frame").src = "/api/webview?url=" + encodeURIComponent(u);
}

// --- Internet Archive viewer (PDF preview + metadata + downloads) ------------
// --- IA preview page viewer (local compressed copy, arrow-key paging) --------
const iaViewer = { pages: 0, page: 1 };

function renderIaPages(previewPath, pages) {
  const box = el("ia-pages");
  let h = "";
  for (let i = 1; i <= pages; i++) {
    h += `<div class="ia-pgrow" data-page="${i}">` +
      `<img loading="lazy" alt="page ${i}" ` +
      `src="/api/pdf/pageimg?path=${encodeURIComponent(previewPath)}&page=${i}&w=900">` +
      `<div class="ia-pgnum">${i} / ${pages}</div></div>`;
  }
  box.innerHTML = h;
  box.scrollTop = 0;
  iaViewer.pages = pages;
  iaViewer.page = 1;
  highlightIaPage(1, false);
}

function highlightIaPage(n, scroll) {
  const box = el("ia-pages");
  box.querySelectorAll(".ia-pgrow").forEach((r) =>
    r.classList.toggle("ia-pgcur", +r.dataset.page === n));
  if (scroll) {
    const cur = box.querySelector(`.ia-pgrow[data-page="${n}"]`);
    if (cur) cur.scrollIntoView({ behavior: "smooth", block: "start" });
  }
}

function iaGoPage(n) {
  if (!iaViewer.pages) return;
  iaViewer.page = Math.max(1, Math.min(iaViewer.pages, n));
  highlightIaPage(iaViewer.page, true);
}

// ←/↑/PageUp and →/↓/PageDown step through the framed preview
function onIaViewerKey(ev) {
  if (el("ia-overlay").hidden || el("ia-pages").hidden) return;
  if (/^(INPUT|TEXTAREA|SELECT)$/.test(ev.target.tagName) || ev.target.isContentEditable) return;
  if (ev.key === "ArrowRight" || ev.key === "ArrowDown" || ev.key === "PageDown") {
    ev.preventDefault(); iaGoPage(iaViewer.page + 1);
  } else if (ev.key === "ArrowLeft" || ev.key === "ArrowUp" || ev.key === "PageUp") {
    ev.preventDefault(); iaGoPage(iaViewer.page - 1);
  }
}

// choose the framed local preview if we have one; else the proxied iframe
async function showIaPreview(ident, data) {
  el("ia-pages").hidden = true;
  el("ia-frame").hidden = false;
  let pv = null;
  try { pv = await (await fetch("/api/ia/preview/" + encodeURIComponent(ident))).json(); }
  catch (e) { pv = null; }
  if (pv && pv.ok && pv.pages) {
    renderIaPages(pv.preview, pv.pages);
    el("ia-pages").hidden = false;
    el("ia-frame").hidden = true;
    el("ia-frame").src = "about:blank";
    return;
  }
  el("ia-frame").src = (data && data.pdf)
    ? "/api/pdf?url=" + encodeURIComponent(data.pdf) + "&preview=1"
    : "/api/webview?url=" + encodeURIComponent(
        (data && data.details) || ("https://archive.org/details/" + ident));
  // no local copy yet — queue a background download (shares the cap/accounting)
  // so the framed preview (and its compressed 10-page copy) is ready next time
  if (state.settings.autoIaDownload) enqueueAutoDl(ident, {});
}

async function openIaViewer(ident) {
  if (!ident) return;
  const meta = el("ia-meta"), dls = el("ia-downloads");
  el("ia-title").textContent = "Internet Archive :: " + ident;
  el("ia-frame").src = "about:blank";
  meta.innerHTML = "<tr><td>Loading …</td></tr>";
  dls.innerHTML = "";
  el("ia-external").onclick = () =>
    window.open("https://archive.org/details/" + ident, "_blank", "noopener");
  el("ia-overlay").hidden = false;
  let data;
  try { data = await (await fetch("/api/ia/meta?id=" + encodeURIComponent(ident))).json(); }
  catch (e) { meta.innerHTML = "<tr><td>Could not load Internet Archive metadata</td></tr>"; return; }
  const md = data.metadata || {};
  const arr = (v) => Array.isArray(v) ? v.join("; ") : (v == null ? "" : String(v));
  el("ia-title").textContent = arr(md.title) || ident;
  el("ia-external").onclick = () => window.open(data.details, "_blank", "noopener");
  // Prefer the locally-downloaded compressed preview (page frames + arrow keys);
  // otherwise fall back to the proxied remote preview iframe.
  await showIaPreview(ident, data);
  const rows = [["Title", "title"], ["Author", "creator"], ["Year", "year"], ["Date", "date"],
    ["Publisher", "publisher"], ["Language", "language"], ["Pages", "imagecount"],
    ["Subjects", "subject"], ["Collection", "collection"]];
  meta.innerHTML = rows.map(([label, k]) => {
    const v = arr(md[k]);
    return v ? `<tr><th>${esc(label)}</th><td>${esc(v)}</td></tr>` : "";
  }).join("") || "<tr><td>No metadata</td></tr>";
  // download buttons — window.open bypasses the embedded-view interceptor
  dls.innerHTML = (data.downloads || []).map((d, i) =>
    `<button class="cad-btn tiny ia-dl" type="button" data-i="${i}">` +
    `${esc(d.format || d.name)}${d.size ? " · " + fmtSize(+d.size) : ""}</button>`)
    .join("") || "<span class='empty'>No downloads available</span>";
  dls.querySelectorAll(".ia-dl").forEach((btn) => {
    btn.onclick = () => window.open(data.downloads[+btn.dataset.i].url, "_blank", "noopener");
  });
}
function closeIaViewer() {
  el("ia-overlay").hidden = true;
  el("ia-frame").src = "about:blank";
  el("ia-pages").innerHTML = "";
  el("ia-pages").hidden = true;
  iaViewer.pages = 0;
  iaViewer.page = 1;
}

function initWebView() {
  el("webview-close").onclick = closeWebView;
  el("webview-reload").onclick = reloadWebView;
  el("webview-external").onclick = () => {
    const u = el("webview-url").dataset.url;
    if (u) window.open(u, "_blank", "noopener");   // escape hatch: real tab
  };
  el("webview-overlay").addEventListener("click", (ev) => {
    if (ev.target === el("webview-overlay")) closeWebView();
  });
  // intercept target=_blank clicks app-wide. Bubble phase + defaultPrevented
  // check so specialized handlers (e.g. a rejected badge opening the manual-
  // source modal) still win; modifier-click keeps the native new-tab behavior.
  document.addEventListener("click", (ev) => {
    if (ev.defaultPrevented || ev.button !== 0 || ev.ctrlKey || ev.metaKey || ev.shiftKey || ev.altKey)
      return;
    const a = ev.target.closest && ev.target.closest('a[target="_blank"]');
    if (!a) return;
    const href = a.getAttribute("href") || "";
    if (!/^https?:\/\//i.test(href)) return;
    ev.preventDefault();
    // Internet Archive links open the rich IA viewer instead of the web view
    const iam = href.match(/archive\.org\/details\/([^/?#]+)/i);
    if (iam) { openIaViewer(decodeURIComponent(iam[1])); return; }
    openWebView(href);
  });
  el("ia-close").onclick = closeIaViewer;
  el("ia-overlay").addEventListener("click", (ev) => {
    if (ev.target === el("ia-overlay")) closeIaViewer();
  });
  document.addEventListener("keydown", onIaViewerKey);   // arrow-key paging
  // entry-photo viewer: any Info-panel thumbnail opens full size. Focus
  // moves to the viewer's close control and returns to the thumbnail on
  // close; only the backdrop and the close button dismiss (not the image).
  let lightboxOpener = null;
  const openLightbox = (th) => {
    lightboxOpener = th;
    const cap = th.dataset.cap || th.alt || "Entry photo";
    el("img-lightbox-img").src = th.dataset.lightbox;
    el("img-lightbox-img").alt = cap;
    el("img-lightbox-cap").textContent = cap;
    el("img-lightbox").hidden = false;
    el("img-lightbox-close").focus();
  };
  const closeLightbox = () => {
    el("img-lightbox").hidden = true;
    el("img-lightbox-img").src = "";
    if (lightboxOpener && document.contains(lightboxOpener)) lightboxOpener.focus();
    lightboxOpener = null;
  };
  document.addEventListener("click", (ev) => {
    const th = ev.target.closest("[data-lightbox]");
    if (th) openLightbox(th);
  });
  document.addEventListener("keydown", (ev) => {
    if (ev.key !== "Enter" || ev.repeat || !el("img-lightbox").hidden) return;
    const th = ev.target.closest && ev.target.closest("[data-lightbox]");
    if (th) openLightbox(th);
  });
  el("img-lightbox-close").addEventListener("click", closeLightbox);
  el("img-lightbox").addEventListener("mousedown", (ev) => {
    if (ev.target === el("img-lightbox")) {
      ev.preventDefault();
      closeLightbox();
    }
  });
  document.addEventListener("keydown", (ev) => {
    if (el("img-lightbox").hidden) return;
    // the close control is the viewer's only focusable: keep Tab on it
    if (ev.key === "Tab") {
      ev.preventDefault();
      el("img-lightbox-close").focus();
    }
  });
  document.addEventListener("keydown", (ev) => {
    if (ev.key !== "Escape") return;
    if (!el("img-lightbox").hidden) closeLightbox();
    else if (!el("ia-overlay").hidden) closeIaViewer();
    else if (!el("webview-overlay").hidden) closeWebView();
  });
}

// custom window controls for the frameless Electron shell (no-op in a browser)
const _WIN_MAX_ICON =
  '<svg viewBox="0 0 10 10"><rect x="0.5" y="0.5" width="9" height="9" fill="none" stroke="currentColor"/></svg>';
const _WIN_RESTORE_ICON =
  '<svg viewBox="0 0 10 10"><rect x="0.5" y="2.5" width="7" height="7" fill="none" stroke="currentColor"/>' +
  '<path d="M2.5 2.5 V0.5 H9.5 V7.5 H7.5" fill="none" stroke="currentColor"/></svg>';

function initDesktopChrome() {
  const d = window.whlDesktop;
  if (!d || !d.isDesktop || !d.win) return;   // a browser keeps its own chrome
  document.body.classList.add("desktop");
  const min = el("win-min"), max = el("win-max"), close = el("win-close");
  if (min) min.onclick = () => d.win.minimize();
  if (max) max.onclick = () => d.win.toggleMaximize();
  if (close) close.onclick = () => d.win.close();
  if (d.win.onMaximized) d.win.onMaximized((m) => {
    if (!max) return;
    max.innerHTML = m ? _WIN_RESTORE_ICON : _WIN_MAX_ICON;
    max.title = m ? "Restore" : "Maximize";
    max.setAttribute("aria-label", m ? "Restore" : "Maximize");
  });
}

// One init step must not be able to take out the ones after it. A missing
// element used to throw here and silently disable everything downstream -- an
// old template plus a new app.js killed the OCR page handlers that way, and the
// only trace was a devtools line nobody was reading. Now each step is isolated
// and its failure is reported, in the footer and in the Info tab's console.
function boot(name, fn) {
  try {
    fn();
  } catch (e) {
    conPut("error", `init: ${name} failed -- ${e.message}`, "app");
    statusCrit(`STARTUP :: ${name.toUpperCase()} FAILED (see the Info tab)`);
  }
}

function init() {
  initConsole();          // first: it is where every later failure is reported
  boot("desktop chrome", initDesktopChrome);
  boot("web view", initWebView);
  boot("settings", loadSettings);
  boot("theme", applyTheme);
  boot("ui scale", applyUiScale);
  boot("font", applyFont);
  boot("exp sharpen", applyExpSharpen);
  boot("checked books", loadChecked);
  boot("icons", injectIcons);
  boot("confirm dialog", initConfirmDialog);
  boot("overlay modals", initOverlayModals);
  boot("tabs", initTabs);
  boot("jobs", initJobs);
  boot("smart check", initSmartCheck);
  boot("tooltips", initTooltips);
  boot("pane tabs", initPaneTabs);
  boot("actor header", installActorHeader);   // before any write goes out
  boot("menu bar", initMenubar);
  boot("home", initHome);
  boot("account", initAuth);
  boot("setup wizard", initWizard);
  boot("title bar", fitTitleBar);   // after the menus exist: their width sets the clamp
  boot("settings nav", initSettingsNav);
  boot("about", initAbout);
  boot("column resize", initColResize);
  boot("open library status", loadOlStatus);
  // A database dropped into ~/.library-tool while the app is open should
  // register without a restart: the backend already resolves it live, so just
  // re-read the offline-index status whenever the window regains focus (e.g.
  // after dropping files in the file manager). Throttled so a focus/blur storm
  // can't hammer the endpoint.
  let _olStatusAt = 0;
  const refreshOlStatusOnReturn = () => {
    if (document.visibilityState !== "visible") return;
    const now = Date.now();
    if (now - _olStatusAt < 2000) return;
    _olStatusAt = now;
    loadOlStatus();
  };
  document.addEventListener("visibilitychange", refreshOlStatusOnReturn);
  window.addEventListener("focus", refreshOlStatusOnReturn);

  // undo / redo (toolbar)
  el("undo-btn").addEventListener("click", undo);
  el("redo-btn").addEventListener("click", redo);
  document.addEventListener("keydown", (ev) => {
    if (!(ev.ctrlKey || ev.metaKey)) return;
    const t = ev.target;
    if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable)) return;
    const k = ev.key.toLowerCase();
    if (k === "z" && !ev.shiftKey) { ev.preventDefault(); undo(); }
    else if (k === "y" || (k === "z" && ev.shiftKey)) { ev.preventDefault(); redo(); }
  });

  // table-bar commands (run-scans / scrape / search-pane are menu items)
  el("dl-approved").addEventListener("click", downloadApproved);
  el("export-json").addEventListener("click", exportJson);
  initSortHeaders();

  // filter + column-visibility popups
  el("filter-btn").addEventListener("click", () =>
    openFilterMenu(el("filter-btn")));
  el("colvis-top").addEventListener("click", () => {
    if (state.settings.topTable === "whl")
      openColumnMenu(el("colvis-top"), "whl", renderWhlTop);
    else openColumnMenu(el("colvis-top"), "checked", renderChecked);
  });
  el("colvis-bottom").addEventListener("click", () =>
    openColumnMenu(el("colvis-bottom"), "b-" + activeBottomTable(), renderBottomRows));
  el("colvis-upload").addEventListener("click", () =>
    openColumnMenu(el("colvis-upload"), "upload", renderUpload));
  document.addEventListener("mousedown", (ev) => {
    if (!el("popup-menu").hidden &&
        !ev.target.closest("#popup-menu") &&
        !(popupAnchor && popupAnchor.contains(ev.target))) {
      closePopup();
    }
  });
  // Escape dismisses it (every sibling surface does), and a resize orphans its
  // fixed position (it is placed once in openPopup) — close it either way.
  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape" && !el("popup-menu").hidden) closePopup();
  });
  addEventListener("resize", () => { if (!el("popup-menu").hidden) closePopup(); });
  syncFilterBtn();
  syncSrcFilterBtn();
  el("ol-clear").addEventListener("click", clearSearchForm);
  // Advanced Search popup (moved out of the left pane)
  el("adv-search-btn").addEventListener("click", toggleAdvSearch);
  el("adv-search-close").addEventListener("click", closeAdvSearch);
  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape" && !el("adv-search-pop").hidden) closeAdvSearch();
  });
  document.addEventListener("mousedown", (ev) => {
    const pop = el("adv-search-pop");
    if (pop.hidden) return;
    if (pop.contains(ev.target) || ev.target.closest("#adv-search-btn")) return;
    closeAdvSearch();
  });

  // checked-tab find bar
  el("sync-master-btn").addEventListener("click", syncMasterList);
  el("cloud-sync-btn").addEventListener("click", runCloudSync);
  // Debounce the render: update state synchronously so the box stays live, but
  // rebuild the (potentially thousands-of-node) tables only ~150ms after typing
  // pauses, instead of tearing them down on every keystroke.
  let _searchDebounce = null;
  el("checked-search").addEventListener("input", () => {
    state.checkedFilter = el("checked-search").value.trim();
    state.olOverride = null;
    clearTimeout(_searchDebounce);
    _searchDebounce = setTimeout(() => {
      renderTop();
      // renderChecked already rebuilds the bottom pane in checked mode — the
      // direct call was doubling that work. Only the WHL top table leaves the
      // bottom stale (renderWhlTop doesn't touch it), and only when the
      // catalog pane is actually shown (mirrors renderBottomPane's guard).
      if (state.settings.topTable === "whl" && state.settings.showCatalog)
        renderBottomRows();
      scheduleOlRealtime();
    }, 150);
  });
  el("checked-rows").addEventListener("click", onCheckedClick);

  // year-range filter (applies to whichever top table is shown) — same debounce,
  // which also collapses the per-keystroke saveSettings() client_state PUT.
  let _yearDebounce = null;
  const onYearFilter = () => {
    const num = (v) => { const n = parseInt(v, 10); return Number.isFinite(n) ? n : null; };
    state.settings.yearFrom = num(el("year-from").value);
    state.settings.yearTo = num(el("year-to").value);
    clearTimeout(_yearDebounce);
    _yearDebounce = setTimeout(() => { saveSettings(); renderTop(); }, 150);
  };
  el("year-from").addEventListener("input", onYearFilter);
  el("year-to").addEventListener("input", onYearFilter);
  el("year-clear").addEventListener("click", () => {
    clearTimeout(_yearDebounce);           // cancel a pending trailing render
    el("year-from").value = "";
    el("year-to").value = "";
    state.settings.yearFrom = state.settings.yearTo = null;
    saveSettings();
    renderTop();
  });
  syncYearFilterInputs();

  // Open Library search constraint checkboxes (Title/Author/Year) — the same
  // persistent searchCons set toggled by Ctrl+click on a column header
  for (const [id, k] of SEARCH_CONS_BOXES) {
    el(id).addEventListener("change", () => {
      const cons = state.settings.searchCons || (state.settings.searchCons = {});
      if (el(id).checked) cons[k] = true; else delete cons[k];
      saveSettings();
      markSortHeaders("checked");
      markSortHeaders("whl");
      rebuildSearchFromMarks();
    });
  }
  syncSearchConsCheckboxes();

  // top pane: table selector + WHL interactions
  el("top-table").addEventListener("change", () => switchTopTable(el("top-table").value));
  el("whl-mode").addEventListener("click", () =>
    setTopMode(topMode() === "edit" ? "search" : "edit"));
  document.addEventListener("keydown", (ev) => {
    if (!(ev.ctrlKey || ev.metaKey) || ev.key.toLowerCase() !== "e") return;
    const t = ev.target;
    if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable)) return;
    ev.preventDefault();
    setTopMode(topMode() === "edit" ? "search" : "edit");
  });
  el("whltop-rows").addEventListener("click", (ev) => {
    const a = ev.target.closest("a");
    if (a) {
      // published entries with a publication file open in the PDF window
      if (a.dataset.pdfm !== undefined) {
        ev.preventDefault();
        openPdfModal(parseInt(a.dataset.pdfm, 10));
      }
      return;
    }
    const tr = ev.target.closest("tr");
    if (!tr) return;
    const idx = parseInt(tr.dataset.widx, 10);
    // Ctrl+click opens the record in the EDIT tab from either mode
    if (ev.ctrlKey || ev.metaKey) { openWhlEditTab(idx); return; }
    if (whlMode() === "search") {
      if (ev.target.closest("td[data-wsearch]")) selectWhlSearchRow(idx);
      return;
    }
    const td = ev.target.closest("td[data-wedit]");
    if (td) startWhlEdit(td);
  });

  // Alt shows the original (pre-correction) record: over an edited WHL row,
  // and in the EDIT panel
  el("whltop-rows").addEventListener("mouseover", (ev) => {
    const tr = ev.target.closest("tr");
    curHoverTr = tr;
    if (tr && ev.altKey && tr.classList.contains("whl-row-corrected")) {
      showOrigRow(tr, whlRowByIdx(parseInt(tr.dataset.widx, 10)));
    } else if (origRowShown && origRowShown !== tr) {
      clearOrigRow();
    }
  });
  el("whltop-rows").addEventListener("mouseleave", () => {
    curHoverTr = null;
    clearOrigRow();
  });
  document.addEventListener("keydown", (ev) => {
    if (ev.key !== "Alt" || ev.repeat) return;
    if (curHoverTr && curHoverTr.classList.contains("whl-row-corrected")) {
      showOrigRow(curHoverTr, whlRowByIdx(parseInt(curHoverTr.dataset.widx, 10)));
    }
    showEditOrig();
  });
  document.addEventListener("keyup", (ev) => {
    if (ev.key !== "Alt") return;
    if (origRowShown || editOrigShown) ev.preventDefault();
    clearOrigRow();
    clearEditOrig();
  });
  window.addEventListener("blur", () => {
    clearOrigRow();
    clearEditOrig();
  });
  el("whledit-form").addEventListener("submit", saveWhlEditTab);
  el("bookedit-form").addEventListener("submit", saveBookEditTab);
  el("setedit-form").addEventListener("submit", saveSetEditTab);
  // (search constraints are now Ctrl+click column marks — see initSortHeaders)

  // OL column marks: choose which columns repopulate the selected WHL row
  el("bottom-head").addEventListener("click", (ev) => {
    if (activeBottomTable() !== "ol") return;
    if (!ev.ctrlKey && !ev.metaKey && !ev.shiftKey) return;
    if (sortSuppress || ev.target.closest(".col-rz")) return;
    const th = ev.target.closest("th");
    if (!th) return;
    const i = [...th.parentElement.children].indexOf(th);
    const fkey = OL_MARK_FIELDS["c" + i];
    if (!fkey) return;
    ev.preventDefault();
    const want = ev.shiftKey ? "exclude" : "copy";
    if (state.olColMarks[fkey] === want) delete state.olColMarks[fkey];
    else state.olColMarks[fkey] = want;
    renderBottomRows();
  });
  el("bottom-rows").addEventListener("click", (ev) => {
    if (ev.target.closest("a")) return;
    // History tab: Ctrl+click reverts that action; plain click toggles detail
    const hrow = ev.target.closest("tr.hist-row");
    if (hrow) {
      const hid = parseInt(hrow.dataset.hid, 10);
      if (ev.ctrlKey || ev.metaKey) revertHistoryAction(hid);
      else toggleHistDetail(hrow, hid);
      return;
    }
    const tr = ev.target.closest("tr.bottom-row");
    if (!tr) return;
    const rec = state.bottomRecords[parseInt(tr.dataset.bi, 10)];
    if (!rec) return;
    if (rec._src === "manual") {
      status("ALREADY A MANUAL ENTRY IN CHECKED-BOOKS");
      return;
    }
    if (ev.ctrlKey || ev.metaKey) {
      // Ctrl+click: open the record in the EDIT tab instead of adding it
      if (rec._src === "ch") openChEditTab(rec._idx);
      else if (rec._src === "whl") openWhlEditTab(rec._idx);
      else status("OPEN LIBRARY ROWS HAVE NO EDITOR — click to add instead");
      return;
    }
    addToTop(rec);
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
      scheduleTableChromeRefresh();
    });
    document.addEventListener("mouseup", () => {
      if (!dragging) return;
      dragging = false;
      document.body.classList.remove("resizing");
      state.settings.paneWidth = parseInt(pane.style.width, 10) || null;
      saveSettings();
      scheduleTableChromeRefresh();
    });
  })();

  // resizable approved-sources pane (vertical splitter in the upload tab)
  (() => {
    const sp = el("upload-splitter");
    const top = el("upload-split");
    // The stored height is applied as-is; #upload-split's responsive CSS
    // min-height raises a too-small split to a usable floor on tall windows
    // and relaxes it on short ones (so the sources pane never overflows).
    if (state.settings.uploadSplitH) {
      top.style.height = state.settings.uploadSplitH + "px";
      top.style.flex = "none";
    }
    let dragging = false;
    sp.addEventListener("mousedown", (ev) => {
      dragging = true;
      ev.preventDefault();
      document.body.classList.add("resizing-v");
    });
    document.addEventListener("mousemove", (ev) => {
      if (!dragging) return;
      const min = 160;   // the CSS min-height enforces the usable floor
      const max = Math.max(min, el("workbench").clientHeight - 180);
      const h = Math.min(max, Math.max(min, ev.clientY - top.getBoundingClientRect().top));
      top.style.height = h + "px";
      top.style.flex = "none";
    });
    document.addEventListener("mouseup", () => {
      if (!dragging) return;
      dragging = false;
      document.body.classList.remove("resizing-v");
      state.settings.uploadSplitH = parseInt(top.style.height, 10) || null;
      saveSettings();
    });
  })();

  // Generic pane splitter: drag a gutter to size the target pane, persist
  // per-split in settings.paneSizes, double-click to fall back to the
  // stylesheet default. `measure(ev)` turns the pointer position into the
  // target's new pixel size; `apply(px)`/`reset()` write/clear it.
  function initSplitter(id, opts) {
    const sp = el(id);
    if (!sp) return;
    const sizes = () => (state.settings.paneSizes =
      state.settings.paneSizes || {});
    // clamp a saved size before applying it — a corrupt/stale value must
    // not wedge a pane off-screen. opts.max() can't be trusted here (the
    // pane may still be display:none, measuring 0), so a fixed ceiling.
    if (sizes()[opts.key]) {
      opts.apply(Math.max(opts.min, Math.min(2400, +sizes()[opts.key] || opts.min)));
    }
    let dragging = false;
    sp.addEventListener("mousedown", (ev) => {
      dragging = true;
      ev.preventDefault();
      document.body.classList.add(opts.vertical ? "resizing-v" : "resizing");
    });
    document.addEventListener("mousemove", (ev) => {
      if (!dragging) return;
      opts.apply(Math.min(opts.max(), Math.max(opts.min, opts.measure(ev))));
    });
    document.addEventListener("mouseup", () => {
      if (!dragging) return;
      dragging = false;
      document.body.classList.remove("resizing", "resizing-v");
      sizes()[opts.key] = Math.round(opts.value());
      saveSettings();
    });
    sp.addEventListener("dblclick", () => {
      delete sizes()[opts.key];
      opts.reset();
      saveSettings();
    });
  }
  const widthSplit = (id, key, paneId, min, max) => {
    const pane = el(paneId);
    initSplitter(id, {
      key, min, max: () => max,
      measure: (ev) => ev.clientX - pane.getBoundingClientRect().left,
      apply: (px) => { pane.style.width = px + "px"; },
      value: () => pane.offsetWidth,
      reset: () => { pane.style.width = ""; },
    });
  };
  widthSplit("ocr-splitter", "ocrSide", "ocr-side", 200, 620);
  widthSplit("an-facsimile-splitter", "anFacsimile", "an-facsimile-pane", 280, 900);
  widthSplit("form-splitter", "buildForm", "build-form", 280, 720);
  widthSplit("publish-splitter", "publishSide", "publish-side", 230, 620);
  initSplitter("ocr-side-splitter", {
    key: "ocrBooks", vertical: true, min: 60,
    max: () => el("ocr-side").clientHeight - 120,
    measure: (ev) => ev.clientY - el("ocr-books-wrap").getBoundingClientRect().top,
    apply: (px) => { el("ocr-books-wrap").style.flex = `0 0 ${px}px`; },
    value: () => el("ocr-books-wrap").offsetHeight,
    reset: () => { el("ocr-books-wrap").style.flex = ""; },
  });
  initSplitter("ocr-queue-splitter", {
    key: "ocrQueue", vertical: true, min: 60,
    max: () => el("workbench").clientHeight - 220,
    measure: (ev) =>
      el("ocr-queue-wrap").getBoundingClientRect().bottom - ev.clientY,
    apply: (px) => { el("ocr-queue-wrap").style.flex = `0 0 ${px}px`; },
    value: () => el("ocr-queue-wrap").offsetHeight,
    reset: () => { el("ocr-queue-wrap").style.flex = ""; },
  });

  // markdown: the builder's live editor + the overlay window (WHL pencil)
  buildDescMd = createMdEditor(el("b-desc-editor"));
  overlayMd = createMdEditor(el("md-live-overlay"));
  el("w-desc-md").addEventListener("click", () =>
    openMarkdownEditor("w-description", "Markdown :: WHL description"));
  el("md-apply").addEventListener("click", () => closeMarkdownEditor(true));
  el("md-cancel").addEventListener("click", () => closeMarkdownEditor(false));
  el("md-close").addEventListener("click", () => closeMarkdownEditor(false));
  el("md-overlay").addEventListener("mousedown", (ev) => {
    if (ev.target === el("md-overlay")) {
      ev.preventDefault();   // keep focus with the discard confirm, not <body>
      closeMarkdownEditor(false);
    }
  });

  // upload list / book builder
  buildPdfViewer = createPdfViewer();
  el("b-pdf-viewer").appendChild(buildPdfViewer.el);
  pdfmViewer = createPdfViewer();
  el("pdfm-body").appendChild(pdfmViewer.el);
  el("pdfm-close").addEventListener("click", closePdfModal);
  el("pdfm-overlay").addEventListener("mousedown", (ev) => {
    if (ev.target === el("pdfm-overlay")) closePdfModal();
  });
  el("b-ready").addEventListener("click", async () => {
    const b = currentBuild();
    if (!b) return;
    const on = el("b-ready").classList.toggle("active");
    el("b-verified-tag").hidden = !on;
    // Verification is a save action: persist every field and the description,
    // not just the badge. This also records the active OCR file as verified.
    const saved = await saveBuildFields();
    if (!saved) {
      const cur = currentBuild();
      const verified = cur && (cur.status === "ready" || cur.status === "uploaded");
      el("b-ready").classList.toggle("active", !!verified);
      el("b-verified-tag").hidden = !verified;
    }
    renderBuildsList();
  });
  el("build-new").addEventListener("click", () => {
    setWorkbenchPhase("record", false);   // the blank form is the next step
    createBuild({}, "(blank)");
  });
  el("export-builds").addEventListener("click", exportBuilds);
  el("download-upload-list").addEventListener("click", downloadUploadList);
  for (const t of document.querySelectorAll("#builds-tabs .pane-tab")) {
    t.addEventListener("click", () => {
      state.buildsTab = t.dataset.bstab;
      if (currentBuild() && !buildsSorted().some((b) => b.id === state.buildSel)) {
        state.buildSel = null;
      }
      renderUpload();
    });
  }
  el("build-upload").addEventListener("click", uploadBuild);

  // public catalogue tree + website-format publication preview
  el("publish-side-toggle").addEventListener("click", () =>
    setPublishSidebarCollapsed(!state.settings.publishSidebarCollapsed, true));
  el("publish-group").addEventListener("change", () => {
    state.settings.publishGroup = el("publish-group").value;
    state.publishOpen.clear();
    state.publishClosed.clear();
    saveSettings();
    renderPublishTree(true);
    renderPublishPreview();
  });
  el("publish-refresh").addEventListener("click", loadPublishCatalog);
  el("publish-tree").addEventListener("click", (ev) => {
    const toggle = ev.target.closest("[data-publish-toggle]");
    if (toggle) {
      const id = toggle.dataset.publishToggle;
      const node = (() => {
        const walk = (rows, depth) => {
          for (const n of rows) {
            if (n.nodeId === id) return { node: n, depth };
            const hit = walk(n.children || [], depth + 1);
            if (hit) return hit;
          }
          return null;
        };
        return walk(publishTreeModel(), 0);
      })();
      if (node) {
        if (publishNodeOpen(node.node, node.depth)) {
          state.publishOpen.delete(id); state.publishClosed.add(id);
        } else {
          state.publishClosed.delete(id); state.publishOpen.add(id);
        }
        renderPublishTree();
        const replacement = [...el("publish-tree").querySelectorAll(
          "[data-publish-toggle]")].find((button) =>
          button.dataset.publishToggle === id);
        if (replacement) replacement.focus();
      }
      return;
    }
    const pick = ev.target.closest("[data-publish-select]");
    if (!pick) return;
    state.publishSel = pick.dataset.publishSelect;
    renderPublishTree(true);
    renderPublishPreview();
  });
  el("publish-preview").addEventListener("click", (ev) => {
    const pick = ev.target.closest("[data-publish-book]");
    if (!pick) return;
    state.publishSel = "book:" + pick.dataset.publishBook;
    renderPublishTree(true);
    renderPublishPreview();
  });
  el("publish-open-live").addEventListener("click", () => {
    const entity = publishFindEntity(state.publishSel);
    if (!entity || entity.kind !== "book") return;
    const base = publishSafeUrl(state.publishSiteUrl, false).replace(/\/$/, "");
    const slug = publishSlug(entity.entries[0]);
    if (base && slug) openWebView(`${base}/book.html?slug=${encodeURIComponent(slug)}`);
  });
  el("src-filter").addEventListener("click", () => openSrcFilterMenu(el("src-filter")));
  el("btab-resources").addEventListener("click", (ev) => {
    const btn = ev.target.closest(".res-use");
    if (!btn) return;
    const card = btn.closest(".res-card");
    const b = currentBuild();
    if (!card || !b) return;
    setThumbnailSource(b.id, card.dataset.source);
  });
  el("build-form").addEventListener("submit", saveBuildFields);
  el("build-form").addEventListener("input", () => { buildDirty = true; });
  el("build-save").addEventListener("click", saveBuildFields);
  el("build-delete").addEventListener("click", deleteBuild);
  // Ctrl/Cmd+S saves the open entry (only on the Workbench's editor-derived
  // phases — the Text phase keeps the OCR editor's own save flow)
  document.addEventListener("keydown", (ev) => {
    if (!((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === "s")) return;
    const tab = document.querySelector("#tabs .tab.active");
    if (tab && tab.dataset.tab === "workbench" &&
        ["record", "source", "publish"].includes(wbActivePhase()) &&
        !el("build-editor").hidden) {
      ev.preventDefault();
      saveBuildFields();
    }
  });
  el("upload-rows").addEventListener("click", (ev) => {
    const b = ev.target.closest("[data-build-src]");
    if (!b) return;
    const s = (state.uploadSources || [])[parseInt(b.dataset.buildSrc, 10)];
    if (s) createBuild(buildSeedFromSource(s), s.title.slice(0, 30));
  });
  for (const t of document.querySelectorAll("#build-tabs .pane-tab")) {
    t.addEventListener("click", () => switchBuildTab(t.dataset.btab));
  }
  el("b-rights-suggest").addEventListener("click", (ev) => {
    ev.preventDefault();
    suggestRights();
  });
  el("b-ai").addEventListener("click", generateAiSummary);
  el("b-desc-load").addEventListener("click", () => el("b-desc-file").click());
  el("b-desc-file").addEventListener("change", () => {
    loadDescriptionFile(el("b-desc-file").files[0]);
    el("b-desc-file").value = "";
  });
  el("b-folder").addEventListener("click", syncBuildFolder);
  el("b-ocr-load").addEventListener("click", () => el("b-ocr-file").click());
  el("b-ocr-file").addEventListener("change", () => {
    uploadOcrFile(el("b-ocr-file").files[0]);
    el("b-ocr-file").value = "";
  });
  el("b-ocr-list").addEventListener("click", (ev) => {
    const chip = ev.target.closest("[data-ocr]");
    if (chip) setActiveOcr(chip.dataset.ocr);
  });
  boot("OCR tab", initOcrTab);
  // Q while hovering a row (any table) or a builder entry: mark it as needing
  // attention, and offer a reason
  loadAttn();
  document.addEventListener("keydown", onAttentionKey);
  document.addEventListener("keydown", onSearchKey);
  document.addEventListener("keydown", onRowDeleteKey);
  document.addEventListener("keydown", onUiScaleKey);
  boot("attention popover", initAttnPop);
  boot("review queue", initReviewWin);
  boot("categories", initCategories);
  boot("analyze", initAnalyze);
  boot("workbench", initWorkbench);
  boot("replica", initReplica);
  loadTaxonomy();   // async; pickers and cells refresh when the vocab lands
  el("b-pdf-attach").addEventListener("click", () => attachPdfFile());
  el("b-pdf_file").addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") { ev.preventDefault(); attachPdfFile(); }
  });
  el("b-pdf-browse").addEventListener("click", () => {
    const cur = el("b-pdf_file").value.trim();
    const dir = cur.includes("/") || cur.includes("\\")
      ? cur.replace(/[/\\][^/\\]*$/, "") : "";
    openFileBrowser(dir, (path) => attachPdfFile(path));
  });
  // secondary PDF sources: browse-to-add; × on a chip removes it
  el("b-pdf2-add").addEventListener("click", () => {
    const cur = el("b-pdf_file").value.trim();
    const dir = cur.includes("/") || cur.includes("\\")
      ? cur.replace(/[/\\][^/\\]*$/, "") : "";
    openFileBrowser(dir, (path) => addSecondaryPdf(path));
  });
  el("b-pdf-sources").addEventListener("click", (ev) => {
    const del = ev.target.closest("button.src2-del");
    if (del) removeSecondaryPdf(del.dataset.sid);
  });

  // file browser window
  el("fb-close").addEventListener("click", closeFileBrowser);
  el("fb-go").addEventListener("click", () => fbLoad(el("fb-path").value.trim()));
  el("fb-path").addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") { ev.preventDefault(); fbLoad(el("fb-path").value.trim()); }
  });
  el("fb-overlay").addEventListener("mousedown", (ev) => {
    if (ev.target === el("fb-overlay")) closeFileBrowser();
  });

  // manual source + settings windows
  el("msrc-close").addEventListener("click", closeManualSource);
  el("msrc-save").addEventListener("click", () => saveManualSource(false));
  el("msrc-clear").addEventListener("click", () => saveManualSource(true));
  el("msrc-url").addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") { ev.preventDefault(); saveManualSource(false); }
  });
  el("msrc-overlay").addEventListener("mousedown", (ev) => {
    if (ev.target === el("msrc-overlay")) {
      ev.preventDefault();   // keep focus with the discard confirm, not <body>
      closeManualSource();
    }
  });
  el("open-settings").addEventListener("click", openSettings);
  el("settings-close").addEventListener("click", closeSettings);
  el("settings-overlay").addEventListener("mousedown", (ev) => {
    if (ev.target === el("settings-overlay")) closeSettings();
  });
  el("changelog-close").addEventListener("click", closeChangelog);
  el("changelog-overlay").addEventListener("mousedown", (ev) => {
    if (ev.target === el("changelog-overlay")) closeChangelog();
  });
  document.addEventListener("keydown", (ev) => {
    if (ev.key !== "Escape") return;
    if (!el("auth-overlay").hidden) hideAuthOverlay();   // topmost (z 62)
    else if (!el("wizard-overlay").hidden) {
      // Esc = set up later. Keep anything typed before recording completion.
      wizCommit().finally(() => closeWizard(true));
    }
    else if (!el("attn-pop").hidden) closeAttnPop();
    else if (!el("fb-overlay").hidden) closeFileBrowser();
    else if (!el("pdfm-overlay").hidden) closePdfModal();
    else if (!el("md-overlay").hidden) closeMarkdownEditor(false);
    else if (!el("msrc-overlay").hidden) closeManualSource();
    else if (!el("review-overlay").hidden) closeReviewWin();
    else if (!el("cat-overlay").hidden) closeCategories();
    else if (!el("changelog-overlay").hidden) closeChangelog();
    else if (!el("settings-overlay").hidden) closeSettings();
    else if (!el("engine-overlay").hidden) closeDefaultEngines();
    else if (!el("about-overlay").hidden) closeAbout();
  });

  // keep sized tables filling their panes when the window or panes resize
  let rzTimer = null;
  window.addEventListener("resize", () => {
    fitTitleBar();        // cheap; must not wait for the debounce
    clearTimeout(rzTimer);
    rzTimer = setTimeout(() => {
      refreshVisibleTableChrome();
    }, 120);
  });

  // Paint immediately from the localStorage cache: loadSettings/loadChecked
  // already ran synchronously in init(), so the data to render is in memory.
  // This shows the window's content without waiting on the ~1MB client_state
  // network round-trip below. pushClientState self-guards on clientStateReady
  // (still false here), so this early paint can never write back to the server
  // before the sync reconciles.
  switchTopTable(state.settings.topTable === "whl" ? "whl" : "checked");
  renderBottomPane();
  renderHome();

  // Adopt the authoritative server copy of checked / settings / attention
  // (or seed it from localStorage on first run), THEN re-render so the views
  // reflect whatever the server holds (the merge may differ from the cache).
  syncClientStateOnLoad().then((adopted) => {
    hydrateSecrets();      // warm credentials without delaying the initial UI
    if (adopted) { applyTheme(); applyFont(); applyExpSharpen(); }
    maybeWizard();       // first desktop launch: the guide covers sign-in too
    maybeAuthPrompt();   // needs the adopted settings: authPromptDismissed
    loadDownloads();
    // Home's pending tasks are derived from these, so it re-renders once the
    // data it counts has actually arrived
    loadManual().then(migrateParsedEntries).then(renderHome);
    loadBuilds().then(renderUpload).then(renderHome);
    switchTopTable(state.settings.topTable === "whl" ? "whl" : "checked");
    renderBottomPane();
    renderHome();
  });
}

init();

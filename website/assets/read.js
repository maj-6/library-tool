// The reader. Self-hosted pdf.js (vendored under assets/vendor/pdfjs/, keeping
// the no-CDN promise) streams the PDF by HTTP Range — disableAutoFetch means a
// big scan loads lazily as you scroll. Pages render into a virtualized vertical
// scroll: only pages near the viewport hold a canvas; the rest keep a
// pre-sized placeholder so the scrollbar never jumps. Margin annotations come
// from volume_notes; a page-aligned text/translation panel from volume_pages.

import { getVolume, getNotes, getPages, getAllPages, pdfHref, searchVolume, usingCloud } from "./data.js";
import { normalizeSearchText, findMatchRanges, searchPages, rpcSnippetHtml, rpcHitsUsable } from "./textsearch.js";
import * as pdfjsLib from "./vendor/pdfjs/build/pdf.min.mjs";

// The worker is a sibling file; resolve it relative to THIS module so it works
// no matter what path the page is served from.
pdfjsLib.GlobalWorkerOptions.workerSrc =
  new URL("./vendor/pdfjs/build/pdf.worker.min.mjs", import.meta.url).href;

const el = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const scroller = el("scroller");
const thumbsRail = el("thumbs");
const textPanel = el("textpanel");

const slug = new URLSearchParams(location.search).get("slug") || "";
const LS_KEY = `whl_reader_${slug}`;

// ---- reader state ----------------------------------------------------------
const S = {
  pdf: null,
  numPages: 0,
  scale: 1,
  fit: "width",          // "width" | "page" | null(manual)
  base: { width: 612, height: 792 },   // page-1 size at scale 1
  current: 1,
  pages: [],             // per page: {box, wrap, margin, rendered, rendering, task}
  notesByPage: new Map(),
  showThumbs: false,
  showNotes: false,
  showText: false,
  textLang: "",          // "" = original layer
  textCache: {},         // { lang: { page: text } }
  searchQuery: "",       // the active in-book search; "" = none
  searchCache: {},       // { lang: Promise<[{page, body}]> } — the whole text, once
  thumbsBuilt: false,
};

const DPR = Math.min(window.devicePixelRatio || 1, 2);
const RENDER_MARGIN = "900px 0px";     // render a little beyond the viewport

function loadPrefs() {
  try {
    const p = JSON.parse(localStorage.getItem(LS_KEY) || "{}");
    if (p && typeof p === "object") return p;
  } catch { /* ignore */ }
  return {};
}
let saveTimer;
function savePrefs() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => {
    try {
      localStorage.setItem(LS_KEY, JSON.stringify({
        page: S.current, scale: S.scale, fit: S.fit, lang: S.textLang,
        thumbs: S.showThumbs, notes: S.showNotes, text: S.showText,
      }));
    } catch { /* quota / private mode — non-fatal */ }
  }, 300);
}

function message(label, text) {
  scroller.innerHTML = `<div class="reader-msg"><span class="label">${esc(label)}</span>${esc(text)}</div>`;
}

// ---- scale -----------------------------------------------------------------
function fitScale(mode) {
  const pad = 44;
  const availW = scroller.clientWidth - pad;
  const availH = scroller.clientHeight - pad;
  if (mode === "page") {
    return Math.max(0.1, Math.min(availW / S.base.width, availH / S.base.height));
  }
  return Math.max(0.1, availW / S.base.width);   // fit width
}

function applyScale(rerender = true) {
  if (S.fit) S.scale = fitScale(S.fit);
  S.scale = Math.max(0.2, Math.min(S.scale, 6));
  // Re-size every placeholder to the new estimated dimensions so the column
  // width is right immediately, then re-render the pages that hold a canvas.
  const w = Math.round(S.base.width * S.scale);
  const h = Math.round(S.base.height * S.scale);
  for (const p of S.pages) {
    if (!p.rendered) { p.wrap.style.width = w + "px"; p.wrap.style.height = h + "px"; }
  }
  if (rerender) {
    for (const p of S.pages) { if (p.rendered) { unloadPage(p); } }
    renderVisible();
  }
  savePrefs();
}

// ---- page rendering --------------------------------------------------------
function placeholder(n) {
  const w = Math.round(S.base.width * S.scale);
  const h = Math.round(S.base.height * S.scale);
  return `<div class="page-placeholder" style="width:${w}px;height:${h}px">Page ${n}</div>`;
}

function unloadPage(p) {
  if (!p.rendered) return;
  if (p.task) { try { p.task.cancel(); } catch { /* */ } p.task = null; }
  // Re-estimate the box from the current scale so a page that was unloaded
  // after a zoom keeps a correctly sized placeholder until it renders again.
  const w = Math.round(S.base.width * S.scale);
  const h = Math.round(S.base.height * S.scale);
  p.wrap.style.width = w + "px";
  p.wrap.style.height = h + "px";
  p.wrap.innerHTML = placeholder(p.n);
  p.rendered = false;
}

async function renderPage(p) {
  if (p.rendered || p.rendering) return;
  p.rendering = true;
  try {
    const page = await S.pdf.getPage(p.n);
    const vp = page.getViewport({ scale: S.scale });
    const canvas = document.createElement("canvas");
    canvas.width = Math.floor(vp.width * DPR);
    canvas.height = Math.floor(vp.height * DPR);
    canvas.style.width = Math.floor(vp.width) + "px";
    canvas.style.height = Math.floor(vp.height) + "px";
    const ctx = canvas.getContext("2d", { alpha: false });
    p.wrap.style.width = Math.floor(vp.width) + "px";
    p.wrap.style.height = Math.floor(vp.height) + "px";
    p.wrap.innerHTML = "";
    p.wrap.appendChild(canvas);
    const transform = DPR !== 1 ? [DPR, 0, 0, DPR, 0, 0] : null;
    p.task = page.render({ canvasContext: ctx, viewport: vp, transform });
    await p.task.promise;
    p.task = null;
    p.rendered = true;
  } catch (e) {
    // A cancelled render (scale change / scrolled away) is expected; ignore it.
    if (!e || e.name !== "RenderingCancelledException") {
      p.wrap.innerHTML = `<div class="page-placeholder">Page ${p.n} — could not render</div>`;
    }
  } finally {
    p.rendering = false;
  }
}

let io;
function renderVisible() {
  // The IntersectionObserver drives rendering; this kicks it for the initial
  // paint and after a scale change by checking what is on screen right now.
  const top = scroller.scrollTop, bottom = top + scroller.clientHeight;
  const margin = scroller.clientHeight;
  for (const p of S.pages) {
    const pt = p.box.offsetTop, pb = pt + p.box.offsetHeight;
    if (pb >= top - margin && pt <= bottom + margin) renderPage(p);
  }
}

// ---- current-page tracking -------------------------------------------------
let rafPending = false;
function onScroll() {
  if (rafPending) return;
  rafPending = true;
  requestAnimationFrame(() => {
    rafPending = false;
    const mid = scroller.scrollTop + scroller.clientHeight * 0.35;
    let best = 1;
    for (const p of S.pages) {
      if (p.box.offsetTop <= mid) best = p.n; else break;
    }
    if (best !== S.current) {
      S.current = best;
      el("pageinput").value = String(best);
      updateThumbActive();
      if (S.showText) refreshText();
      savePrefs();
    }
  });
}

function scrollToPage(n) {
  const p = S.pages[n - 1];
  if (!p) return;
  scroller.scrollTo({ top: Math.max(0, p.box.offsetTop - 14), behavior: "auto" });
}

// ---- notes -----------------------------------------------------------------
function marginHtml(notes) {
  return notes.map((n) => `
    <div class="margin-note">
      ${n.kind ? `<span class="mn-tag">${esc(n.kind)}</span>` : ""}
      ${n.quote ? `<p class="mn-quote">“${esc(n.quote)}”</p>` : ""}
      ${n.body ? `<p class="mn-body">${esc(n.body)}</p>` : ""}
    </div>`).join("");
}

function fillMargins() {
  for (const p of S.pages) {
    const notes = S.notesByPage.get(p.n) || [];
    if (S.showNotes && notes.length) {
      p.margin.innerHTML = marginHtml(notes);
      p.margin.hidden = false;
    } else {
      p.margin.hidden = true;
    }
  }
}

// ---- text / translation panel ---------------------------------------------
function buildLangOptions(v) {
  const a = (v && v.assets) || {};
  const opts = [];
  if (a.pages) opts.push(["", "Original text"]);
  const tr = a.translations && typeof a.translations === "object" ? a.translations : {};
  for (const lang of Object.keys(tr)) opts.push([lang, lang.toUpperCase()]);
  const sel = el("tp-lang");
  sel.innerHTML = opts.map(([val, label]) =>
    `<option value="${esc(val)}">${esc(label)}</option>`).join("");
  if (opts.length && !opts.some(([val]) => val === S.textLang)) S.textLang = opts[0][0];
  sel.value = S.textLang;
  return opts.length > 0;
}

let textSeq = 0;   // stale-response guard: fast page/lang flips must not
                   // let a slow older fetch overwrite the newer panel
async function refreshText() {
  if (!S.showText) return;
  const seq = ++textSeq;
  const from = Math.max(1, S.current - 2);
  const to = Math.min(S.numPages, S.current + 4);
  const lang = S.textLang;
  const cache = S.textCache[lang] || (S.textCache[lang] = {});
  const missing = [];
  for (let n = from; n <= to; n++) if (!(n in cache)) missing.push(n);
  if (missing.length) {
    try {
      const got = await getPages(slug, lang, Math.min(...missing), Math.max(...missing));
      for (let n = Math.min(...missing); n <= Math.max(...missing); n++) cache[n] = got[n] || "";
    } catch {
      if (seq !== textSeq) return;
      el("tp-body").innerHTML = `<p class="note-more">Text could not be loaded.</p>`;
      return;
    }
    if (seq !== textSeq) return;
  }
  const parts = [];
  for (let n = from; n <= to; n++) {
    const body = cache[n];
    if (!body) continue;
    parts.push(`<div class="tp-page ${n === S.current ? "current" : ""}">
        <div class="tp-pnum">Page ${n}</div>
        <div class="tp-body">${bodyHtml(body)}</div>
      </div>`);
  }
  el("tp-body").innerHTML = parts.length ? parts.join("") :
    `<p class="note-more">No text for these pages.</p>`;
}

// A page body for the panel: escaped, with the active search's matches marked.
// Escape each segment first, then splice the <mark>s between them — a page
// body must never reach innerHTML raw.
function bodyHtml(body) {
  const ranges = S.searchQuery ? findMatchRanges(body, S.searchQuery) : [];
  if (!ranges.length) return esc(body);
  let html = "";
  let at = 0;
  for (const [s, e] of ranges) {
    html += `${esc(body.slice(at, s))}<mark>${esc(body.slice(s, e))}</mark>`;
    at = e;
  }
  return html + esc(body.slice(at));
}

// ---- in-book search ---------------------------------------------------------
let searchSeq = 0;   // the same stale-response guard refreshText uses

// The whole text layer, fetched once per language and kept as a promise so
// two quick searches never fire two full fetches.
function allPagesFor(lang) {
  if (!S.searchCache[lang]) {
    S.searchCache[lang] = getAllPages(slug, lang).catch((e) => {
      delete S.searchCache[lang];    // a failed fetch must stay retryable
      throw e;
    });
  }
  return S.searchCache[lang];
}

async function runSearch() {
  const q = el("tp-q").value.trim();
  const seq = ++searchSeq;
  const box = el("tp-results");
  const nq = normalizeSearchText(q);
  if (nq.length < 2) {                         // nothing (sensible) to search for
    S.searchQuery = "";
    box.hidden = true;
    box.innerHTML = "";
    refreshText();
    return;
  }
  S.searchQuery = q;
  const lang = S.textLang;
  box.hidden = false;

  // Cloud mode asks the database first: one search_volume round-trip, ranked
  // by Postgres FTS with a trigram fallback, snippets cut server-side. The
  // query goes over folded, matching the published search layer, so
  // "phyſick" finds "physick" there too. Any failure (a live project still
  // behind on migrations answers 404, or the network is down) or zero hits
  // falls back silently to the client-side path below, whose own folding may
  // still match. The panel's in-page highlighting stays local either way.
  if (usingCloud) {
    let rows = null;
    try { rows = await searchVolume(slug, nq, lang); } catch { rows = null; }
    if (seq !== searchSeq || lang !== S.textLang) return;
    if (rpcHitsUsable(rows)) {
      renderHits(rows.map((r) =>
        ({ page: r.page, html: rpcSnippetHtml(r.snippet), more: 0 })));
      refreshText();           // the visible pages pick up their <mark>s
      return;
    }
  }

  if (!S.searchCache[lang]) box.innerHTML = `<p class="note-more">Fetching the full text…</p>`;
  let pages;
  try {
    pages = await allPagesFor(lang);
  } catch {
    if (seq !== searchSeq) return;
    box.innerHTML = `<p class="note-more">The text could not be fetched.</p>`;
    return;
  }
  if (seq !== searchSeq || lang !== S.textLang) return;
  renderHits(searchPages(pages, q));
  refreshText();               // the visible pages pick up their <mark>s
}

function snippetHtml(h) {
  return esc(h.snippet.slice(0, h.matchStart)) +
    `<mark>${esc(h.snippet.slice(h.matchStart, h.matchEnd))}</mark>` +
    esc(h.snippet.slice(h.matchEnd));
}

// One hit row's snippet HTML. Client-side hits carry match offsets into a
// verbatim snippet and are escaped-then-marked here; RPC hits arrive as
// prebuilt safe HTML (rpcSnippetHtml) with the fragment ts_headline chose.
function hitHtml(h) {
  if (h.html !== undefined) return h.html;
  return `${h.cutStart ? "…" : ""}${snippetHtml(h)}${h.cutEnd ? "…" : ""}`;
}

function renderHits(hits) {
  const box = el("tp-results");
  box.hidden = false;
  if (!hits.length) {
    box.innerHTML = `<p class="note-more">No matches in this text.</p>`;
    return;
  }
  const pageCount = new Set(hits.map((h) => h.page)).size;
  const total = hits.reduce((n, h) => n + 1 + (h.more || 0), 0);
  const rows = hits.map((h) => `
    <button type="button" class="tp-hit" data-page="${h.page}">
      <span class="tp-hit-page">p. ${h.page}</span>
      <span class="tp-hit-snip">${hitHtml(h)}</span>
      ${h.more ? `<span class="tp-hit-more">+${h.more} more on this page</span>` : ""}
    </button>`).join("");
  box.innerHTML = `
    <div class="tp-hits">
      <div class="tp-hitcount">${total} match${total === 1 ? "" : "es"} · ${pageCount} page${pageCount === 1 ? "" : "s"}</div>
      ${rows}
    </div>`;
  box.querySelectorAll(".tp-hit").forEach((b) =>
    b.addEventListener("click", () => scrollToPage(Number(b.dataset.page))));
}

// ---- thumbnails ------------------------------------------------------------
let thumbIO;
function buildThumbs() {
  if (S.thumbsBuilt) return;
  S.thumbsBuilt = true;
  const frag = document.createDocumentFragment();
  thumbIO = new IntersectionObserver((entries) => {
    for (const ent of entries) {
      if (ent.isIntersecting) { renderThumb(Number(ent.target.dataset.n)); thumbIO.unobserve(ent.target); }
    }
  }, { root: thumbsRail, rootMargin: "300px 0px" });
  for (let n = 1; n <= S.numPages; n++) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "thumb";
    b.dataset.n = n;
    b.innerHTML = `<span class="tnum">${n}</span>`;
    b.addEventListener("click", () => scrollToPage(n));
    frag.appendChild(b);
    thumbIO.observe(b);
  }
  thumbsRail.appendChild(frag);
  updateThumbActive();
}

async function renderThumb(n) {
  const b = thumbsRail.querySelector(`.thumb[data-n="${n}"]`);
  if (!b || b.dataset.done) return;
  b.dataset.done = "1";
  try {
    const page = await S.pdf.getPage(n);
    const base = page.getViewport({ scale: 1 });
    const w = 146;
    const vp = page.getViewport({ scale: w / base.width });
    const canvas = document.createElement("canvas");
    canvas.width = Math.floor(vp.width);
    canvas.height = Math.floor(vp.height);
    await page.render({ canvasContext: canvas.getContext("2d", { alpha: false }), viewport: vp }).promise;
    b.insertBefore(canvas, b.firstChild);
  } catch { /* a thumbnail is optional */ }
}

function updateThumbActive() {
  if (!S.thumbsBuilt) return;
  thumbsRail.querySelectorAll(".thumb").forEach((t) =>
    t.classList.toggle("active", Number(t.dataset.n) === S.current));
  const act = thumbsRail.querySelector(".thumb.active");
  if (act && S.showThumbs) act.scrollIntoView({ block: "nearest" });
}

// ---- toggles ---------------------------------------------------------------
function setThumbs(on) {
  S.showThumbs = on;
  thumbsRail.hidden = !on;
  el("tthumbs").classList.toggle("on", on);
  if (on) buildThumbs();
  if (S.fit) applyScale();   // the scroller just changed width; re-fit
  savePrefs();
}
function setNotes(on) {
  S.showNotes = on;
  el("tnotes").classList.toggle("on", on);
  fillMargins();
  savePrefs();
}
function setText(on) {
  S.showText = on;
  textPanel.hidden = !on;
  el("ttext").classList.toggle("on", on);
  if (on) refreshText();
  if (S.fit) applyScale();   // the scroller just changed width; re-fit
  savePrefs();
}

// ---- zoom controls ---------------------------------------------------------
function zoomBy(f) { S.fit = null; S.scale *= f; applyScale(); }

// ---- boot ------------------------------------------------------------------
async function main() {
  if (!slug) { message("No volume", "This reader needs a ?slug= in the address."); return; }

  let v;
  try { v = await getVolume(slug); }
  catch (e) { message("Error", `Could not load the record: ${e.message}`); return; }
  if (!v) { message("Not found", "No volume is filed under this slug."); return; }

  el("title").textContent = v.title;
  el("title").title = v.title;
  el("back").href = `book.html?slug=${encodeURIComponent(slug)}`;

  const url = pdfHref(v);
  if (!url) {
    message("Not yet available", "No scan has been published for this volume yet.");
    return;
  }

  // Restore preferences before we lay pages out.
  const prefs = loadPrefs();
  if (typeof prefs.scale === "number") S.scale = prefs.scale;
  if (prefs.fit === "width" || prefs.fit === "page" || prefs.fit === null) S.fit = prefs.fit;
  if (typeof prefs.lang === "string") S.textLang = prefs.lang;

  let pdf;
  try {
    pdf = await pdfjsLib.getDocument({
      url,
      disableAutoFetch: true,     // stream lazily by range instead of pulling the whole file
      disableStream: false,
      rangeChunkSize: 65536,
    }).promise;
  } catch (e) {
    const msg = /fetch|network|CORS|Failed/i.test(String(e && e.message))
      ? "The scan could not be fetched — it may be offline, or blocked by cross-origin rules."
      : `The PDF could not be opened (${esc(e && e.message || "unknown error")}).`;
    message("Cannot open", msg);
    return;
  }

  S.pdf = pdf;
  S.numPages = pdf.numPages;
  el("pagecount").textContent = `/ ${pdf.numPages}`;

  try {
    const page1 = await pdf.getPage(1);
    const vp = page1.getViewport({ scale: 1 });
    S.base = { width: vp.width, height: vp.height };
  } catch { /* keep the default letter size */ }

  if (S.fit) S.scale = fitScale(S.fit);

  // Build the page column: one box per page, each a placeholder until rendered.
  scroller.innerHTML = "";
  const frag = document.createDocumentFragment();
  io = new IntersectionObserver((entries) => {
    for (const ent of entries) {
      const p = S.pages[Number(ent.target.dataset.n) - 1];
      if (!p) continue;
      if (ent.isIntersecting) renderPage(p);
      else unloadPage(p);       // virtualization: drop canvases that scroll away
    }
  }, { root: scroller, rootMargin: RENDER_MARGIN });

  for (let n = 1; n <= pdf.numPages; n++) {
    const box = document.createElement("div");
    box.className = "pagebox";
    box.dataset.n = n;
    const wrap = document.createElement("div");
    wrap.className = "page-canvas-wrap";
    wrap.dataset.n = n;
    wrap.innerHTML = placeholder(n);
    const margin = document.createElement("div");
    margin.className = "page-margin";
    margin.hidden = true;
    box.appendChild(wrap);
    box.appendChild(margin);
    frag.appendChild(box);
    S.pages.push({ n, box, wrap, margin, rendered: false, rendering: false, task: null });
    io.observe(wrap);
  }
  scroller.appendChild(frag);

  renderVisible();

  // Notes.
  try {
    const notes = await getNotes(slug);
    for (const nt of notes || []) {
      const list = S.notesByPage.get(nt.page) || [];
      list.push(nt);
      S.notesByPage.set(nt.page, list);
    }
  } catch { /* no notes is fine */ }

  // Text panel availability.
  const hasText = buildLangOptions(v);
  if (!hasText) el("ttext").disabled = true;
  if (!S.notesByPage.size) el("tnotes").disabled = true;

  // Restore panel toggles (only those that have content).
  setThumbs(Boolean(prefs.thumbs));
  setNotes(Boolean(prefs.notes) && S.notesByPage.size > 0);
  setText(Boolean(prefs.text) && hasText);

  // Restore scroll position.
  const startPage = Number(prefs.page);
  if (Number.isFinite(startPage) && startPage > 1) {
    // Let the layout settle so offsetTop is meaningful, then jump.
    requestAnimationFrame(() => requestAnimationFrame(() => scrollToPage(startPage)));
    S.current = startPage;
    el("pageinput").value = String(startPage);
  }

  wireControls();

  // Deep link: ?q= (book.html's search form) opens the panel and runs the search.
  const deepQ = new URLSearchParams(location.search).get("q") || "";
  if (deepQ && hasText) {
    el("tp-q").value = deepQ;
    if (!S.showText) setText(true);
    runSearch();
  }
}

function wireControls() {
  scroller.addEventListener("scroll", onScroll, { passive: true });

  el("pprev").addEventListener("click", () => scrollToPage(Math.max(1, S.current - 1)));
  el("pnext").addEventListener("click", () => scrollToPage(Math.min(S.numPages, S.current + 1)));
  el("pageinput").addEventListener("change", () => {
    const n = Math.trunc(Number(el("pageinput").value));
    if (Number.isFinite(n) && n >= 1 && n <= S.numPages) scrollToPage(n);
    else el("pageinput").value = String(S.current);
  });

  el("zoomin").addEventListener("click", () => zoomBy(1.2));
  el("zoomout").addEventListener("click", () => zoomBy(1 / 1.2));
  el("fitwidth").addEventListener("click", () => { S.fit = "width"; applyScale(); });
  el("fitpage").addEventListener("click", () => { S.fit = "page"; applyScale(); });

  el("tthumbs").addEventListener("click", () => setThumbs(!S.showThumbs));
  el("tnotes").addEventListener("click", () => { if (!el("tnotes").disabled) setNotes(!S.showNotes); });
  el("ttext").addEventListener("click", () => { if (!el("ttext").disabled) setText(!S.showText); });
  el("tp-lang").addEventListener("change", () => {
    S.textLang = el("tp-lang").value;
    refreshText();
    savePrefs();
    if (el("tp-q").value.trim()) runSearch();   // re-run against the new layer
  });

  let searchTimer;
  el("tp-q").addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(runSearch, 300);
  });
  el("tp-q").addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") {
      ev.preventDefault();
      clearTimeout(searchTimer);
      runSearch();
    } else if (ev.key === "Escape") {
      el("tp-q").value = "";
      clearTimeout(searchTimer);
      runSearch();
    }
  });

  let resizeTimer;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => { if (S.fit) applyScale(); }, 150);
  });

  document.addEventListener("keydown", (ev) => {
    const tag = (ev.target && ev.target.tagName) || "";
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
    switch (ev.key) {
      case "ArrowRight": case "PageDown":
        ev.preventDefault(); scrollToPage(Math.min(S.numPages, S.current + 1)); break;
      case "ArrowLeft": case "PageUp":
        ev.preventDefault(); scrollToPage(Math.max(1, S.current - 1)); break;
      case "+": case "=":
        ev.preventDefault(); zoomBy(1.2); break;
      case "-": case "_":
        ev.preventDefault(); zoomBy(1 / 1.2); break;
      case "t": case "T":
        ev.preventDefault(); setThumbs(!S.showThumbs); break;
      default: break;
    }
  });
}

main();

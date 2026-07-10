// The faceted catalogue. Extends the original URL-state machine (readUrl /
// writeUrl, replaceState while typing, pushState on a filter change, popstate,
// a `seq` stale-response guard, and a 220ms debounce) with category and
// language facets so every view of the catalogue deep-links.

import { searchVolumes, pdfHref, usingCloud, safeYear, facetSource, catText } from "./data.js";

const PAGE = 24;
const el = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

// A description may carry Markdown emphasis; the snippet wants plain text.
const plain = (s) => String(s ?? "").replace(/[*_`>#]+/g, "").replace(/\s+/g, " ").trim();

const SORTS = new Set(["title", "year", "year-desc", "recent"]);
const state = { q: "", from: null, to: null, cat: "", lang: "", sort: "title", page: 0 };

// The query lives in the URL, so a search is linkable and the back button works.
function readUrl() {
  const p = new URLSearchParams(location.search);
  state.q = (p.get("q") || "").slice(0, 200);
  state.from = safeYear(p.get("from"));      // "abc" and "1e999" are not years
  state.to = safeYear(p.get("to"));
  state.cat = (p.get("cat") || "").slice(0, 300);
  state.lang = (p.get("lang") || "").slice(0, 60);
  state.sort = SORTS.has(p.get("sort")) ? p.get("sort") : "title";
  const page = Math.trunc(Number(p.get("page")));
  state.page = Number.isFinite(page) && page > 0 ? page - 1 : 0;
  el("q").value = state.q;
  el("from").value = state.from ?? "";
  el("to").value = state.to ?? "";
  el("sort").value = state.sort;
}

function writeUrl(replace) {
  const p = new URLSearchParams();
  if (state.q) p.set("q", state.q);
  if (state.from != null) p.set("from", state.from);
  if (state.to != null) p.set("to", state.to);
  if (state.cat) p.set("cat", state.cat);
  if (state.lang) p.set("lang", state.lang);
  if (state.sort !== "title") p.set("sort", state.sort);
  if (state.page) p.set("page", state.page + 1);
  const url = p.toString() ? `?${p}` : location.pathname;
  history[replace ? "replaceState" : "pushState"]({}, "", url);
}

function bytes(n) {
  if (!n) return "";
  const mb = n / 1048576;
  return mb >= 1 ? `${mb.toFixed(0)} MB` : `${(n / 1024).toFixed(0)} KB`;
}

// ---- catalogue record ------------------------------------------------------
function chips(v) {
  const paths = Array.isArray(v.category_paths) ? v.category_paths : [];
  return paths.map((p) => {
    const t = catText(p);
    if (!t) return "";
    return `<a class="chip" href="?cat=${encodeURIComponent(t)}">${esc(t)}</a>`;
  }).join("");
}

function record(v) {
  const href = pdfHref(v);
  const slug = encodeURIComponent(v.slug);
  const imprint = [
    v.publisher && esc(v.publisher),
    v.publisher_city && esc(v.publisher_city),
    v.year && String(v.year),
    v.edition && esc(v.edition),
    v.pages && `${v.pages} pp`,
  ].filter(Boolean).map((x) => `<span>${x}</span>`).join("");

  const actions = href
    ? `<a class="btn primary" href="read.html?slug=${slug}">Read</a>
       <a class="btn" href="${esc(href)}" target="_blank" rel="noopener"
          title="${bytes(v.pdf_bytes) || "PDF"}">PDF</a>`
    : `<a class="btn" aria-disabled="true" title="No scan yet">Read</a>`;

  const cats = chips(v);
  const desc = plain(v.description);

  return `<li class="record">
    <h3 class="rec-title"><a href="book.html?slug=${slug}">${esc(v.title)}</a></h3>
    ${v.subtitle ? `<div class="rec-author">${esc(v.subtitle)}</div>` : ""}
    ${v.authors ? `<div class="rec-author">${esc(v.authors)}</div>` : ""}
    ${imprint ? `<div class="rec-imprint">${imprint}</div>` : ""}
    ${cats ? `<div class="rec-cats">${cats}</div>` : ""}
    ${desc ? `<p class="rec-desc">${esc(desc)}</p>` : ""}
    <div class="rec-actions">${actions}</div>
  </li>`;
}

// ---- facets (built once from the whole corpus) -----------------------------
// Counts are over the entire catalogue, not the current filter, so the rail is
// a stable table of contents rather than something that empties as you narrow.
function buildFacets(items) {
  const nodes = new Map();   // pathText -> {name, path[], depth, count}
  const roots = new Set();
  const childrenOf = new Map();
  const langs = new Map();
  let ymin = Infinity, ymax = -Infinity;

  const ensure = (path) => {
    const key = catText(path);
    if (!nodes.has(key)) {
      nodes.set(key, { name: path[path.length - 1], path: [...path], depth: path.length, count: 0 });
    }
    return key;
  };

  for (const it of items) {
    const paths = Array.isArray(it.category_paths) ? it.category_paths : [];
    const touched = new Set();
    for (const raw of paths) {
      const clean = (Array.isArray(raw) ? raw : []).map((x) => String(x)).filter(Boolean);
      for (let i = 1; i <= clean.length; i++) {
        const prefix = clean.slice(0, i);
        const key = ensure(prefix);
        if (i === 1) roots.add(key);
        else {
          const parent = catText(clean.slice(0, i - 1));
          if (!childrenOf.has(parent)) childrenOf.set(parent, new Set());
          childrenOf.get(parent).add(key);
        }
        touched.add(key);
      }
    }
    for (const k of touched) nodes.get(k).count++;

    const lang = String(it.language || "").trim();
    if (lang) langs.set(lang, (langs.get(lang) || 0) + 1);
    const y = Number(it.year);
    if (Number.isFinite(y)) { ymin = Math.min(ymin, y); ymax = Math.max(ymax, y); }
  }

  return { nodes, roots: [...roots], childrenOf, langs, ymin, ymax };
}

function renderCatTree(f) {
  const box = el("facet-cats");
  const rows = [];
  const walk = (key) => {
    const n = f.nodes.get(key);
    if (!n) return;
    const cls = `cat-d${Math.min(n.depth - 1, 3)}`;
    rows.push(
      `<div class="cat-row ${cls}" data-cat="${esc(key)}">
         <a href="?cat=${encodeURIComponent(key)}">${esc(n.name)}</a>
         <span class="cat-count">${n.count}</span>
       </div>`
    );
    const kids = [...(f.childrenOf.get(key) || [])]
      .sort((a, b) => f.nodes.get(a).name.localeCompare(f.nodes.get(b).name));
    for (const c of kids) walk(c);
  };
  f.roots.sort((a, b) => f.nodes.get(a).name.localeCompare(f.nodes.get(b).name));
  for (const r of f.roots) walk(r);
  box.innerHTML = rows.join("") || `<div class="note-more">No categories yet.</div>`;
}

function renderLangs(f) {
  const box = el("facet-langs");
  const rows = [...f.langs.entries()].sort((a, b) => a[0].localeCompare(b[0]));
  box.innerHTML = rows.length
    ? rows.map(([lang, n]) =>
        `<div class="lang-row" data-lang="${esc(lang)}">
           <a href="?lang=${encodeURIComponent(lang)}">${esc(lang)}</a>
           <span class="cat-count">${n}</span>
         </div>`).join("")
    : `<div class="note-more">No languages recorded.</div>`;
}

// Reflect the active cat/lang in the rail without rebuilding it.
function markFacets() {
  document.querySelectorAll(".cat-row").forEach((r) =>
    r.classList.toggle("active", r.dataset.cat === state.cat));
  document.querySelectorAll(".lang-row").forEach((r) =>
    r.classList.toggle("active", r.dataset.lang === state.lang));
}

function renderActiveFilters() {
  const tags = [];
  if (state.cat) tags.push(["Category", state.cat, "cat"]);
  if (state.lang) tags.push(["Language", state.lang, "lang"]);
  if (state.from != null || state.to != null) {
    const range = `${state.from ?? "…"}–${state.to ?? "…"}`;
    tags.push(["Year", range, "year"]);
  }
  el("active-filters").innerHTML = tags.map(([k, v, key]) =>
    `<span class="filter-tag">${esc(k)}: ${esc(v)}
       <a href="#" data-drop="${key}" role="button" aria-label="Remove ${esc(k)} filter">×</a></span>`
  ).join("");
  const any = state.cat || state.lang || state.from != null || state.to != null || state.q;
  el("clear-facets").hidden = !any;
}

// ---- render ----------------------------------------------------------------
let seq = 0;
async function render() {
  const mine = ++seq;                       // a slow query must not overwrite a fast one
  el("count").textContent = "Loading…";
  markFacets();
  renderActiveFilters();
  let res;
  try {
    res = await searchVolumes({
      q: state.q, yearFrom: state.from, yearTo: state.to,
      cat: state.cat, lang: state.lang, sort: state.sort,
      limit: PAGE, offset: state.page * PAGE,
    });
  } catch (e) {
    if (mine !== seq) return;
    el("results").innerHTML = "";
    el("count").textContent = `Could not load the library: ${e.message}`;
    el("pager").hidden = true;
    return;
  }
  if (mine !== seq) return;

  const { rows, total } = res;

  // a stale deep link can point past the last page; snap back to it once
  if (!rows.length && total > 0 && state.page > 0) {
    state.page = Math.max(0, Math.ceil(total / PAGE) - 1);
    writeUrl(true);
    return render();
  }

  el("results").innerHTML = rows.length
    ? rows.map(record).join("")
    : `<li class="empty">${state.q ? `Nothing matches “${esc(state.q)}”.` : "No volumes match these filters."}</li>`;

  const first = state.page * PAGE + 1;
  el("count").textContent = total
    ? `${total} volume${total === 1 ? "" : "s"}` +
      (total > PAGE ? ` · showing ${first}–${Math.min(first + rows.length - 1, total)}` : "")
    : "0 volumes";

  const pages = Math.max(1, Math.ceil(total / PAGE));
  el("pager").hidden = pages <= 1;
  el("page").textContent = `Page ${state.page + 1} of ${pages}`;
  el("prev").disabled = state.page === 0;
  el("next").disabled = state.page + 1 >= pages;
}

function go(replace) { writeUrl(replace); render(); }

// ---- events ----------------------------------------------------------------
// One input listener over the whole body catches the search box, the sort
// select, and the year inputs (which live in the facet rail); typing replaces
// history, a filter change pushes it.
let debounce;
el("lib-body").addEventListener("input", (ev) => {
  const id = ev.target && ev.target.id;
  if (!["q", "from", "to", "sort"].includes(id)) return;
  clearTimeout(debounce);
  debounce = setTimeout(() => {
    state.q = el("q").value.trim().slice(0, 200);
    state.from = safeYear(el("from").value);
    state.to = safeYear(el("to").value);
    state.sort = SORTS.has(el("sort").value) ? el("sort").value : "title";
    state.page = 0;
    go(id === "q");   // typing replaces history; a filter change pushes
  }, 220);
});
el("controls").addEventListener("submit", (ev) => ev.preventDefault());

// Category and language rows are links (so they deep-link and open in a new tab
// on middle-click), but a plain click filters in place without a navigation.
el("facets").addEventListener("click", (ev) => {
  const catRow = ev.target.closest("[data-cat]");
  const langRow = ev.target.closest("[data-lang]");
  if (!catRow && !langRow) return;
  ev.preventDefault();
  if (catRow) state.cat = catRow.dataset.cat === state.cat ? "" : catRow.dataset.cat;
  if (langRow) state.lang = langRow.dataset.lang === state.lang ? "" : langRow.dataset.lang;
  state.page = 0;
  go(false);
});

el("active-filters").addEventListener("click", (ev) => {
  const drop = ev.target.closest("[data-drop]");
  if (!drop) return;
  ev.preventDefault();
  const which = drop.dataset.drop;
  if (which === "cat") state.cat = "";
  else if (which === "lang") state.lang = "";
  else if (which === "year") { state.from = state.to = null; el("from").value = ""; el("to").value = ""; }
  state.page = 0;
  go(false);
});

el("clear-facets").addEventListener("click", () => {
  state.q = state.cat = state.lang = "";
  state.from = state.to = null;
  state.sort = "title";
  state.page = 0;
  el("q").value = ""; el("from").value = ""; el("to").value = ""; el("sort").value = "title";
  go(false);
});

el("prev").addEventListener("click", () => { state.page--; go(); el("results").scrollIntoView({ block: "start" }); });
el("next").addEventListener("click", () => { state.page++; go(); el("results").scrollIntoView({ block: "start" }); });
addEventListener("popstate", () => { readUrl(); render(); });

// ---- boot ------------------------------------------------------------------
el("offline").hidden = usingCloud;
readUrl();
render();
facetSource()
  .then((items) => {
    const f = buildFacets(items);
    renderCatTree(f);
    renderLangs(f);
    markFacets();
  })
  .catch(() => {
    el("facet-cats").innerHTML = `<div class="note-more">Categories unavailable.</div>`;
  });

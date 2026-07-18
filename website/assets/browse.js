// The faceted catalogue. Extends the original URL-state machine (readUrl /
// writeUrl, replaceState while typing, pushState on a filter change, popstate,
// a `seq` stale-response guard, and a 220ms debounce) with category and
// language facets so every view of the catalogue deep-links.

import {
  searchVolumes, usingCloud, safeYear, facetSource, catText,
  suggestTitles, suggestAuthors, getAuthorBio, bookTitleHtml,
} from "./data.js";
import { renderRecord } from "./records.js";

const PAGE = 24;
const el = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const SORTS = new Set(["title", "year", "year-desc", "recent"]);
const state = { q: "", from: null, to: null, cat: "", lang: "", author: "", sort: "title", page: 0 };

// The query lives in the URL, so a search is linkable and the back button works.
function readUrl() {
  const p = new URLSearchParams(location.search);
  state.q = (p.get("q") || "").slice(0, 200);
  state.from = safeYear(p.get("from"));      // "abc" and "1e999" are not years
  state.to = safeYear(p.get("to"));
  state.cat = (p.get("cat") || "").slice(0, 300);
  state.lang = (p.get("lang") || "").slice(0, 60);
  state.author = (p.get("author") || "").slice(0, 300);
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
  if (state.author) p.set("author", state.author);
  if (state.sort !== "title") p.set("sort", state.sort);
  if (state.page) p.set("page", state.page + 1);
  const url = p.toString() ? `?${p}` : location.pathname;
  history[replace ? "replaceState" : "pushState"]({}, "", url);
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
  if (state.author) tags.push(["Author", state.author, "author"]);
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
  const any = state.cat || state.lang || state.author || state.from != null || state.to != null || state.q;
  el("clear-facets").hidden = !any;
}

// ---- author About-card ------------------------------------------------------
// Shown above the results when browsing a single author (arrived at via the
// autocomplete or a deep link). The bio fills in asynchronously so it never
// blocks the results render.
let authorBioSeq = 0;
function renderAuthorCard(author, total) {
  const box = el("author-card");
  if (!author) { box.hidden = true; box.innerHTML = ""; return; }
  box.hidden = false;
  box.innerHTML = `
    <h2 class="author-card-name">${esc(author)}</h2>
    <p class="author-stats">${total == null ? "Works" : `${total} work${total === 1 ? "" : "s"}`} in the catalogue ·
      <a href="author.html?author=${encodeURIComponent(author)}">About this author →</a></p>
    <div class="author-card-bio" id="author-card-bio"></div>`;
  const mine = ++authorBioSeq;
  getAuthorBio(author).then((bio) => {
    if (mine !== authorBioSeq || !bio) return;
    const bioBox = document.getElementById("author-card-bio");
    if (bioBox) bioBox.textContent = bio.replace(/[*_`>#]+/g, "").split(/\n{2,}/)[0].trim();
  }).catch(() => { /* a missing bio is not an error */ });
}

// ---- render ----------------------------------------------------------------
// The result-count line. total === null means the rows arrived but the exact
// count did not (the API omitted or declined it): say so, in words -- an
// invented "0 volumes" over a page of visible results is worse than honesty.
function countLabel(total, shown, first) {
  if (total == null) {
    return shown ? `Showing ${first}–${first + shown - 1} · count unavailable`
                 : "Count unavailable";
  }
  if (!total) return "0 volumes";
  return `${total} volume${total === 1 ? "" : "s"}` +
    (total > PAGE ? ` · showing ${first}–${Math.min(first + shown - 1, total)}` : "");
}

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
      cat: state.cat, lang: state.lang, author: state.author, sort: state.sort,
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

  const { rows, total } = res;   // total === null: rows came, the exact count did not

  // a stale deep link can point past the last page; snap back to it once
  // (with no exact count "the last page" is unknowable, so go to the first)
  if (!rows.length && state.page > 0 && (total == null || total > 0)) {
    state.page = total == null ? 0 : Math.max(0, Math.ceil(total / PAGE) - 1);
    writeUrl(true);
    return render();
  }

  renderAuthorCard(state.author, total);

  el("results").innerHTML = rows.length
    ? rows.map(renderRecord).join("")
    : `<li class="empty">${state.q ? `Nothing matches “${esc(state.q)}”.` : "No volumes match these filters."}</li>`;

  el("count").textContent = countLabel(total, rows.length, state.page * PAGE + 1);

  if (total == null) {
    // still pageable, but the end is unknown: a full page may have a next one
    el("pager").hidden = state.page === 0 && rows.length < PAGE;
    el("page").textContent = `Page ${state.page + 1}`;
    el("prev").disabled = state.page === 0;
    el("next").disabled = rows.length < PAGE;
  } else {
    const pages = Math.max(1, Math.ceil(total / PAGE));
    el("pager").hidden = pages <= 1;
    el("page").textContent = `Page ${state.page + 1} of ${pages}`;
    el("prev").disabled = state.page === 0;
    el("next").disabled = state.page + 1 >= pages;
  }
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
  else if (which === "author") state.author = "";
  else if (which === "year") { state.from = state.to = null; el("from").value = ""; el("to").value = ""; }
  state.page = 0;
  go(false);
});

el("clear-facets").addEventListener("click", () => {
  state.q = state.cat = state.lang = state.author = "";
  state.from = state.to = null;
  state.sort = "title";
  state.page = 0;
  el("q").value = ""; el("from").value = ""; el("to").value = ""; el("sort").value = "title";
  go(false);
});

el("prev").addEventListener("click", () => { state.page--; go(); el("results").scrollIntoView({ block: "start" }); });
el("next").addEventListener("click", () => { state.page++; go(); el("results").scrollIntoView({ block: "start" }); });
addEventListener("popstate", () => { readUrl(); render(); });

// ---- autocomplete -----------------------------------------------------------
// Titles and authors, in one dropdown under the search box. A title suggestion
// goes straight to its record; an author suggestion replaces the search with
// that author's whole bibliography (the About-card view above), rather than
// just narrowing the current text search.
let suggestSeq = 0;
let suggestItems = [];   // flat list, titles then authors, in display order
let suggestActive = -1;

function closeSuggest() {
  el("q-suggest").hidden = true;
  el("q-suggest").innerHTML = "";
  suggestItems = [];
  suggestActive = -1;
  el("q").setAttribute("aria-expanded", "false");
}

function renderSuggest(titles, authors) {
  suggestItems = [
    ...titles.map((t) => ({ type: "title", ...t })),
    ...authors.map((a) => ({ type: "author", ...a })),
  ];
  suggestActive = -1;
  if (!suggestItems.length) { closeSuggest(); return; }
  const titleRows = titles.map((t, i) =>
    `<li class="suggest-row" data-idx="${i}" role="option">
       <span class="suggest-title">${bookTitleHtml(t)}</span>
       ${t.authors ? `<span class="suggest-meta">${esc(t.authors)}</span>` : ""}
     </li>`).join("");
  const authorRows = authors.map((a, i) =>
    `<li class="suggest-row" data-idx="${titles.length + i}" role="option">
       <span class="suggest-title">${esc(a.author)}</span>
       <span class="cat-count">${a.work_count}</span>
     </li>`).join("");
  el("q-suggest").innerHTML =
    (titles.length ? `<li class="suggest-group-label">Titles</li>${titleRows}` : "") +
    (authors.length ? `<li class="suggest-group-label">Authors</li>${authorRows}` : "");
  el("q-suggest").hidden = false;
  el("q").setAttribute("aria-expanded", "true");
}

function markSuggestActive() {
  el("q-suggest").querySelectorAll(".suggest-row").forEach((r) =>
    r.classList.toggle("active", Number(r.dataset.idx) === suggestActive));
}

function chooseSuggest(idx) {
  const item = suggestItems[idx];
  if (!item) return;
  closeSuggest();
  if (item.type === "title") {
    location.href = `book.html?slug=${encodeURIComponent(item.slug)}`;
    return;
  }
  state.author = item.author;
  state.q = "";
  el("q").value = "";
  state.page = 0;
  go(false);
}

let suggestDebounce;
el("q").addEventListener("input", () => {
  clearTimeout(suggestDebounce);
  const q = el("q").value.trim();
  if (q.length < 2) { closeSuggest(); return; }
  suggestDebounce = setTimeout(async () => {
    const mine = ++suggestSeq;
    const [titles, authors] = await Promise.all([
      suggestTitles(q).catch(() => []),
      suggestAuthors(q).catch(() => []),
    ]);
    if (mine !== suggestSeq) return;
    if (el("q").value.trim() !== q) return;   // stale by the time it resolved
    renderSuggest(titles, authors);
  }, 150);
});

// mousedown (not click) so it fires before the input's blur would close the list
el("q-suggest").addEventListener("mousedown", (ev) => {
  const row = ev.target.closest("[data-idx]");
  if (!row) return;
  ev.preventDefault();
  chooseSuggest(Number(row.dataset.idx));
});

el("q").addEventListener("keydown", (ev) => {
  if (el("q-suggest").hidden) return;
  if (ev.key === "ArrowDown") {
    ev.preventDefault();
    suggestActive = Math.min(suggestActive + 1, suggestItems.length - 1);
    markSuggestActive();
  } else if (ev.key === "ArrowUp") {
    ev.preventDefault();
    suggestActive = Math.max(suggestActive - 1, 0);
    markSuggestActive();
  } else if (ev.key === "Enter") {
    if (suggestActive >= 0) { ev.preventDefault(); chooseSuggest(suggestActive); }
  } else if (ev.key === "Escape") {
    closeSuggest();
  }
});

document.addEventListener("click", (ev) => {
  if (!ev.target.closest(".searchfield")) closeSuggest();
});

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
    if (Number.isFinite(f.ymin) && Number.isFinite(f.ymax)) {
      el("from").placeholder = String(f.ymin);
      el("to").placeholder = String(f.ymax);
      el("from").min = el("to").min = String(f.ymin);
      el("from").max = el("to").max = String(f.ymax);
    }
  })
  .catch(() => {
    el("facet-cats").innerHTML = `<div class="note-more">Categories unavailable.</div>`;
    el("facet-langs").innerHTML = `<div class="note-more">Languages unavailable.</div>`;
  });

import { searchVolumes, pdfHref, usingCloud, safeYear } from "./data.js";

const PAGE = 24;
const el = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const SORTS = new Set(["title", "year", "year-desc", "recent"]);
const state = { q: "", from: null, to: null, sort: "title", page: 0 };

// The query lives in the URL, so a search is linkable and the back button works.
function readUrl() {
  const p = new URLSearchParams(location.search);
  state.q = (p.get("q") || "").slice(0, 200);
  state.from = safeYear(p.get("from"));      // "abc" and "1e999" are not years
  state.to = safeYear(p.get("to"));
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

function card(v) {
  const href = pdfHref(v);
  const meta = [
    v.year && String(v.year),
    v.publisher && esc(v.publisher),
    v.publisher_city && esc(v.publisher_city),
    v.edition && esc(v.edition),
    v.pages && `${v.pages} pp`,
    bytes(v.pdf_bytes),
  ].filter(Boolean).map((x) => `<span>${x}</span>`).join("");

  // A volume with no PDF yet is listed but not offered: the alternative is a
  // link that 404s, which is worse than a disabled button.
  const action = href
    ? `<a class="btn primary" href="${esc(href)}" target="_blank" rel="noopener">Read</a>`
    : `<a class="btn" aria-disabled="true" title="Not yet uploaded">Pending</a>`;

  return `<li class="volume">
    <div>
      <h3>${href ? `<a href="${esc(href)}" target="_blank" rel="noopener">${esc(v.title)}</a>`
                 : esc(v.title)}</h3>
      ${v.subtitle ? `<div class="by">${esc(v.subtitle)}</div>` : ""}
      ${v.authors ? `<div class="by">${esc(v.authors)}</div>` : ""}
    </div>
    <div class="actions">${action}</div>
    ${meta ? `<div class="meta">${meta}</div>` : ""}
  </li>`;
}

let seq = 0;
async function render() {
  const mine = ++seq;                       // a slow query must not overwrite a fast one
  el("count").textContent = "Loading…";
  let res;
  try {
    // map explicitly: spreading `state` would pass from/to, which searchVolumes
    // calls yearFrom/yearTo, and the year filter would silently do nothing
    res = await searchVolumes({
      q: state.q, yearFrom: state.from, yearTo: state.to, sort: state.sort,
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
  el("results").innerHTML = rows.map(card).join("");
  const first = state.page * PAGE + 1;
  el("count").textContent = total
    ? `${total} volume${total === 1 ? "" : "s"}` +
      (total > PAGE ? ` · showing ${first}–${Math.min(first + rows.length - 1, total)}` : "")
    : state.q ? `Nothing matches “${state.q}”` : "The library is empty";

  const pages = Math.max(1, Math.ceil(total / PAGE));
  el("pager").hidden = pages <= 1;
  el("page").textContent = `Page ${state.page + 1} of ${pages}`;
  el("prev").disabled = state.page === 0;
  el("next").disabled = state.page + 1 >= pages;
}

function go(replace) { writeUrl(replace); render(); }

let debounce;
el("controls").addEventListener("input", (ev) => {
  clearTimeout(debounce);
  debounce = setTimeout(() => {
    state.q = el("q").value.trim().slice(0, 200);
    state.from = safeYear(el("from").value);
    state.to = safeYear(el("to").value);
    state.sort = SORTS.has(el("sort").value) ? el("sort").value : "title";
    state.page = 0;
    go(ev.target.id === "q");   // typing replaces history; a filter change pushes
  }, 220);
});
el("controls").addEventListener("submit", (ev) => ev.preventDefault());
el("prev").addEventListener("click", () => { state.page--; go(); window.scrollTo(0, 0); });
el("next").addEventListener("click", () => { state.page++; go(); window.scrollTo(0, 0); });
addEventListener("popstate", () => { readUrl(); render(); });

el("offline").hidden = usingCloud;
readUrl();
render();

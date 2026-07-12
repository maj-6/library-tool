// Shared catalogue-record markup, used by both browse.js (the results list)
// and author.js (an author's bibliography) so the two never drift apart.

import { pdfHref, thumbHref, catText } from "./data.js";

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

// A description may carry Markdown emphasis; the snippet wants plain text.
const plain = (s) => String(s ?? "").replace(/[*_`>#]+/g, "").replace(/\s+/g, " ").trim();

function bytes(n) {
  if (!n) return "";
  const mb = n / 1048576;
  return mb >= 1 ? `${mb.toFixed(0)} MB` : `${(n / 1024).toFixed(0)} KB`;
}

// Absolute to browse.html (not a relative "?cat=…") since this markup is
// shared with author.html, which isn't the catalogue page itself.
function chips(v) {
  const paths = Array.isArray(v.category_paths) ? v.category_paths : [];
  return paths.map((p) => {
    const t = catText(p);
    if (!t) return "";
    return `<a class="chip" href="browse.html?cat=${encodeURIComponent(t)}">${esc(t)}</a>`;
  }).join("");
}

export function renderRecord(v) {
  const href = pdfHref(v);
  const thumb = thumbHref(v);
  const slug = encodeURIComponent(v.slug);
  const imprint = [
    v.publisher && esc(v.publisher),
    v.publisher_city && esc(v.publisher_city),
    v.year && String(v.year),
    v.edition && esc(v.edition),
    v.pages && `${v.pages} pp`,
  ].filter(Boolean).map((x) => `<span>${x}</span>`).join("");

  // quiet row actions on purpose — a primary button on every record would
  // stripe the catalogue with dark blocks; the book page carries the primary
  const actions = href
    ? `<a class="btn" href="read.html?slug=${slug}">Read</a>
       <a class="btn" href="${esc(href)}" target="_blank" rel="noopener"
          title="${bytes(v.pdf_bytes) || "PDF"}">PDF</a>`
    : `<a class="btn" aria-disabled="true" title="No scan yet">Read</a>`;

  const cats = chips(v);
  const desc = plain(v.description);
  const thumbImg = thumb ? `<img class="rec-thumb" src="${esc(thumb)}" alt="" loading="lazy" />` : "";

  return `<li class="record">
    ${thumbImg}
    <div class="rec-body">
      <h3 class="rec-title"><a href="book.html?slug=${slug}">${esc(v.title)}</a></h3>
      ${v.subtitle ? `<div class="rec-author">${esc(v.subtitle)}</div>` : ""}
      ${v.authors ? `<div class="rec-author">${esc(v.authors)}</div>` : ""}
      ${imprint ? `<div class="rec-imprint">${imprint}</div>` : ""}
      ${cats ? `<div class="rec-cats">${cats}</div>` : ""}
      ${desc ? `<p class="rec-desc">${esc(desc)}</p>` : ""}
      <div class="rec-actions">${actions}</div>
    </div>
  </li>`;
}

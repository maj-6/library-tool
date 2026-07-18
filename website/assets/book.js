// The item record page. Two columns: the main column carries the title block,
// the rendered About article (Markdown from volume_texts), and an annotations
// preview; the side column carries a formal metadata table and the action
// buttons, with availability affordances driven by volumes.assets.

import {
  getVolume, getAbout, getNotes, pdfHref, thumbHref, safeHttpUrl, catText,
  bookTitleHtml, bookTitleText,
} from "./data.js";
import { renderMarkdown } from "./markdown.js";

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const box = document.getElementById("record");

function bytes(n) {
  if (!n) return "";
  const mb = n / 1048576;
  return mb >= 1 ? `${mb.toFixed(0)} MB` : `${(n / 1024).toFixed(0)} KB`;
}
function day(raw) {
  const d = new Date(raw ?? "");
  return Number.isNaN(d.getTime()) ? "" : d.toISOString().slice(0, 10);
}

function catLinks(v) {
  const paths = Array.isArray(v.category_paths) ? v.category_paths : [];
  const links = paths.map((p) => {
    const t = catText(p);
    return t ? `<a href="browse.html?cat=${encodeURIComponent(t)}">${esc(t)}</a>` : "";
  }).filter(Boolean);
  return links.join("<br>");
}

function chips(v) {
  const paths = Array.isArray(v.category_paths) ? v.category_paths : [];
  return paths.map((p) => {
    const t = catText(p);
    return t ? `<a class="chip" href="browse.html?cat=${encodeURIComponent(t)}">${esc(t)}</a>` : "";
  }).join("");
}

function metaRow(label, valueHtml) {
  if (!valueHtml) return "";
  return `<tr><th>${esc(label)}</th><td>${valueHtml}</td></tr>`;
}

function metaTable(v) {
  const src = safeHttpUrl(v.source_url);
  const rows = [
    metaRow("Author", v.authors ? esc(v.authors) : ""),
    metaRow("Published", v.year ? String(v.year) : ""),
    metaRow("Publisher", v.publisher ? esc(v.publisher) : ""),
    metaRow("Place", v.publisher_city ? esc(v.publisher_city) : ""),
    metaRow("Edition", v.edition ? esc(v.edition) : ""),
    metaRow("Language", v.language ? esc(v.language) : ""),
    metaRow("Pages", v.pages ? String(v.pages) : ""),
    metaRow("Categories", catLinks(v)),
    metaRow("Source", src ? `<a href="${esc(src)}" target="_blank" rel="noopener">${esc(hostOf(src))}</a>` : ""),
    metaRow("Copyright", v.copyright_status ? esc(v.copyright_status) : ""),
    metaRow("Added", day(v.created_at)),
  ].filter(Boolean).join("");
  return `<table class="meta-table"><tbody>${rows}</tbody></table>`;
}

function bookThumb(v) {
  const thumb = thumbHref(v);
  return thumb ? `<img class="book-thumb" src="${esc(thumb)}" alt="" loading="lazy" />` : "";
}

function hostOf(url) {
  try { return new URL(url).hostname.replace(/^www\./, ""); } catch { return url; }
}

function availability(v) {
  const a = (v && v.assets) || {};
  const items = [];
  // assets is untyped jsonb on an anon-readable row — numbers are coerced,
  // never interpolated raw
  const pages = Number(a.pages) || 0;
  const notes = Number(a.notes) || 0;
  if (a.about) items.push("About article");
  if (pages) items.push(`Full text · ${pages} page${pages === 1 ? "" : "s"}`);
  if (a.translations && typeof a.translations === "object") {
    const langs = Object.keys(a.translations);
    if (langs.length) items.push(`Translations · ${langs.map(esc).join(", ")}`);
  }
  if (notes) items.push(`${notes} annotation${notes === 1 ? "" : "s"}`);
  const body = items.length
    ? items.map((x) => `<div class="avail-item">${x}</div>`).join("")
    : `<div class="avail-none">The scan only, so far.</div>`;
  return `<div class="avail"><span class="label">Also available</span>${body}</div>`;
}

// Search inside the book: a plain GET form straight into the reader, which
// picks the query up as its ?q= deep link. Only offered when page text exists.
function searchForm(v) {
  const a = (v && v.assets) || {};
  if (!Number(a.pages)) return "";
  return `
    <form class="book-search" action="read.html" method="get">
      <input type="hidden" name="slug" value="${esc(v.slug)}" />
      <input type="search" name="q" placeholder="Search inside this book…"
             aria-label="Search inside this book" required />
      <button class="btn" type="submit">Search</button>
    </form>`;
}

function actions(v) {
  const href = pdfHref(v);
  const slug = encodeURIComponent(v.slug);
  if (href) {
    const size = bytes(v.pdf_bytes);
    return `
      <a class="btn primary" href="read.html?slug=${slug}">Read online</a>
      <a class="btn" href="${esc(href)}" target="_blank" rel="noopener">Download PDF${size ? ` · ${size}` : ""}</a>`;
  }
  // no scan yet: say so in plain text rather than dead button-shaped controls
  return `<span class="rec-noscan">Scan unavailable — no digitized copy yet</span>`;
}

function notFound(slug) {
  box.innerHTML = `
    <p class="crumb"><a href="browse.html">← Back to the catalogue</a></p>
    <div class="notfound">
      <h1>Not in the catalogue</h1>
      <p>No volume is filed under <span class="mono">${esc(slug)}</span>. It may have been
      removed, or the link may be mistyped.</p>
      <p><a href="browse.html">Browse the full catalogue →</a></p>
    </div>`;
}

function titleBlock(v) {
  return `
    <h1 class="book-title">${bookTitleHtml(v)}</h1>
    ${v.subtitle ? `<p class="book-subtitle">${esc(v.subtitle)}</p>` : ""}
    ${v.authors ? `<p class="book-author">${esc(v.authors)}</p>` : ""}
    <p class="book-imprint">
      ${[v.publisher, v.publisher_city, v.year, v.edition, v.pages && `${v.pages} pp`]
        .filter(Boolean).map((x) => `<span>${esc(String(x))}</span>`).join("")}
    </p>
    ${chips(v) ? `<div class="book-cats">${chips(v)}</div>` : ""}`;
}

function notesPreview(notes) {
  if (!notes.length) return "";
  const shown = notes.slice(0, 3).map((n) => `
    <div class="note-card">
      <span class="note-page">p. ${esc(String(n.page))}</span>
      ${n.kind ? `<span class="note-tag">${esc(n.kind)}</span>` : ""}
      ${n.quote ? `<p class="note-quote">“${esc(n.quote)}”</p>` : ""}
      ${n.body ? `<p class="note-body">${esc(n.body)}</p>` : ""}
    </div>`).join("");
  const more = notes.length > 3
    ? `<p class="note-more">…and ${notes.length - 3} more, shown in the reader.</p>` : "";
  return `
    <h2 class="section-head">Annotations · ${notes.length}</h2>
    <div class="note-preview">${shown}${more}</div>`;
}

async function main() {
  const slug = new URLSearchParams(location.search).get("slug") || "";
  if (!slug) { notFound("(none)"); return; }

  let v;
  try {
    v = await getVolume(slug);
  } catch (e) {
    box.innerHTML = `<p class="crumb"><a href="browse.html">← Catalogue</a></p>
      <div class="notfound"><h1>Could not load this record</h1><p>${esc(e.message)}</p></div>`;
    return;
  }
  if (!v) { notFound(slug); return; }

  // Frame first (metadata is already in hand); About and notes fill in after.
  box.innerHTML = `
    <p class="crumb"><a href="browse.html">Catalogue</a> › ${bookTitleHtml(v)}</p>
    <div class="record-page">
      <div class="book-main">
        ${titleBlock(v)}
        <div id="about-slot"></div>
        <div id="notes-slot"></div>
      </div>
      <aside class="book-side">
        ${bookThumb(v)}
        ${metaTable(v)}
        <div class="side-actions">${actions(v)}</div>
        ${searchForm(v)}
        ${availability(v)}
      </aside>
    </div>`;
  document.title = `${bookTitleText(v)} · Archive Browser`;

  const a = (v && v.assets) || {};
  if (a.about) {
    getAbout(slug).then((md) => {
      if (!md) return;
      // articles often open with their own heading — don't stack ours on it
      const head = /^\s*#{1,6}\s/.test(md)
        ? "" : `<h2 class="section-head">About this volume</h2>`;
      document.getElementById("about-slot").innerHTML =
        `${head}<div class="prose">${renderMarkdown(md)}</div>`;
    }).catch(() => { /* an unavailable article is not a page failure */ });
  }
  if (a.notes) {
    getNotes(slug).then((notes) => {
      document.getElementById("notes-slot").innerHTML = notesPreview(notes || []);
    }).catch(() => { /* leave the section empty */ });
  }
}

main();

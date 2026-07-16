// The full "About this author" page: a bio (once one has been curated) and
// the complete bibliography. Mirrors book.js's structure -- the frame (works
// list) is already in hand once the search resolves; the bio fills in after.

import { searchVolumes, getAuthorBio } from "./data.js";
import { renderRecord } from "./records.js";
import { renderMarkdown } from "./markdown.js";

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const box = document.getElementById("record");

function notFound(author) {
  box.innerHTML = `
    <p class="crumb"><a href="browse.html">← Back to the catalogue</a></p>
    <div class="notfound">
      <h1>No works filed under this name</h1>
      <p>No volume in the catalogue is credited to <span class="mono">${esc(author)}</span>.
      The link may be mistyped, or the credit may have changed.</p>
      <p><a href="browse.html">Browse the full catalogue →</a></p>
    </div>`;
}

async function main() {
  const author = new URLSearchParams(location.search).get("author") || "";
  if (!author) { notFound("(none)"); return; }

  let res;
  try {
    res = await searchVolumes({ author, sort: "year", limit: 500 });
  } catch (e) {
    box.innerHTML = `<p class="crumb"><a href="browse.html">← Catalogue</a></p>
      <div class="notfound"><h1>Could not load this author</h1><p>${esc(e.message)}</p></div>`;
    return;
  }
  const { rows, total } = res;
  if (!rows.length) { notFound(author); return; }
  // total is null when the cloud answered rows without an exact count;
  // the rows in hand are then a truthful floor, never an invented zero.
  const works = total ?? rows.length;

  box.innerHTML = `
    <p class="crumb"><a href="browse.html">Catalogue</a> › ${esc(author)}</p>
    <h1 class="book-title">${esc(author)}</h1>
    <p class="author-stats">${works} work${works === 1 ? "" : "s"} in the catalogue</p>
    <div id="bio-slot"></div>
    <h2 class="section-head">Works</h2>
    <ol class="records-list">${rows.map(renderRecord).join("")}</ol>`;
  document.title = `${author} · Archive Browser`;

  getAuthorBio(author).then((bio) => {
    document.getElementById("bio-slot").innerHTML = bio
      ? `<div class="prose">${renderMarkdown(bio)}</div>`
      : `<p class="avail-none">No biography yet.</p>`;
  }).catch(() => {
    document.getElementById("bio-slot").innerHTML = `<p class="avail-none">No biography yet.</p>`;
  });
}

main();

// The full release history from changelog.md. No cloud needed — the changelog
// is a static file the site (and the desktop app) share.
import { fetchChangelog } from "./data.js";

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const li = (i) => `<li>${esc(i)}</li>`;

// A stable in-page anchor for a version, e.g. "3.2.0" -> "v3-2-0".
const anchor = (v) => "v" + String(v).replace(/^v/i, "").replace(/[^\w.-]+/g, "").replace(/\./g, "-");

function category(c) {
  return `<div class="cl-category">
    <h4 class="cl-category-name">${esc(c.name)}</h4>
    <ul class="cl-list">${c.items.map(li).join("")}</ul>
  </div>`;
}

function release(v) {
  const categories = (v.categories || []).filter((c) => c.items && c.items.length);
  const notes = categories.length
    ? categories.map(category).join("")
    : `<ul class="cl-list">${(v.items || []).map(li).join("")}</ul>`;
  return `<section class="cl-rel" id="${esc(anchor(v.version))}">
    <h3 class="cl-ver">${esc(v.version)}${v.date ? ` <span class="cl-date">${esc(v.date)}</span>` : ""}</h3>
    ${notes}
  </section>`;
}

const box = document.getElementById("changelog");
const versions = await fetchChangelog();
box.innerHTML = versions.length
  ? versions.map(release).join("")
  : `<div class="note">No release notes yet.</div>`;

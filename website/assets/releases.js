// The full release history: every version from changelog.md, newest first.
// No cloud needed — the changelog is a static file the site (and the desktop
// app) share.
import { fetchChangelog } from "./data.js";

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function release(v) {
  return `<section class="cl-rel">
    <h3 class="cl-ver">${esc(v.version)}${v.date ? ` <span class="cl-date">${esc(v.date)}</span>` : ""}</h3>
    <ul class="cl-list">${v.items.map((i) => `<li>${esc(i)}</li>`).join("")}</ul>
  </section>`;
}

const box = document.getElementById("changelog");
const versions = await fetchChangelog();
box.innerHTML = versions.length
  ? versions.map(release).join("")
  : `<div class="note">No release notes yet.</div>`;

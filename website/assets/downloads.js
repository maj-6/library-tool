import { latestReleases, usingCloud, safeHttpUrl, fetchChangelog, isSignificantVersion } from "./data.js";

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const PLATFORM = {
  windows: { name: "Windows", note: "Installer (.exe)" },
  macos:   { name: "macOS",   note: "Disk image (.dmg)" },
  linux:   { name: "Linux",   note: "AppImage" },
  android: { name: "Android", note: "Book Capture (.apk)" },
};

const bytes = (n) => (n ? `${(n / 1048576).toFixed(0)} MB` : "");

// toISOString() throws a RangeError on an unparseable date, which would take the
// whole page down rather than lose one line of metadata.
function day(raw) {
  const d = new Date(raw ?? "");
  return Number.isNaN(d.getTime()) ? "" : d.toISOString().slice(0, 10);
}

function card(r) {
  const p = PLATFORM[r.platform] || { name: r.platform, note: "" };
  const href = safeHttpUrl(r.url);
  const meta = [p.note, r.version && `v${esc(r.version)}`, bytes(r.bytes), day(r.published_at)]
    .filter(Boolean).join(" · ");
  const action = href
    ? `<a class="btn primary" href="${esc(href)}">Download</a>`
    : `<a class="btn" aria-disabled="true" title="No download link">Unavailable</a>`;
  return `<div class="rel">
    <h3>${esc(p.name)}</h3>
    <div class="actions">${action}</div>
    <div class="meta">${meta}</div>
    ${r.notes ? `<div class="meta">${esc(r.notes)}</div>` : ""}
  </div>`;
}

// Only the newest build of each platform+channel; the table keeps the history.
function newest(rows) {
  const best = new Map();
  for (const r of rows) {
    const k = `${r.platform}/${r.channel}`;
    if (!best.has(k)) best.set(k, r);            // already ordered newest-first
  }
  return [...best.values()];
}

const box = document.getElementById("releases");
try {
  const rows = usingCloud ? newest(await latestReleases()) : [];
  box.innerHTML = rows.length
    ? rows.map(card).join("")
    : `<div class="note">No release has been published yet — check back once the
       first installer ships, or build from the source on GitHub.</div>`;
} catch (e) {
  box.innerHTML = `<div class="note">Could not load releases: ${esc(e.message)}</div>`;
}

// "What's new": the newest *significant* release. Cosmetic patch releases
// (e.g. 3.0.1) stay off the download page — the full history, including them,
// is on the Release notes page.
const clBox = document.getElementById("changelog");
const versions = await fetchChangelog();
const latest = versions.find((v) => isSignificantVersion(v.version));
clBox.innerHTML = latest
  ? release(latest)
  : `<p class="muted">No release notes yet.</p>`;

function release(v) {
  return `<section class="cl-rel">
    <h3 class="cl-ver">${esc(v.version)}${v.date ? ` <span class="cl-date">${esc(v.date)}</span>` : ""}</h3>
    <ul class="cl-list">${v.items.map((i) => `<li>${esc(i)}</li>`).join("")}</ul>
  </section>`;
}

import { latestReleases, usingCloud } from "./data.js";

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const PLATFORM = {
  windows: { name: "Windows", note: "Installer (.exe)" },
  macos:   { name: "macOS",   note: "Disk image (.dmg)" },
  linux:   { name: "Linux",   note: "AppImage" },
  android: { name: "Android", note: "Book Capture (.apk)" },
};

const bytes = (n) => (n ? `${(n / 1048576).toFixed(0)} MB` : "");

function card(r) {
  const p = PLATFORM[r.platform] || { name: r.platform, note: "" };
  const meta = [p.note, r.version && `v${esc(r.version)}`, bytes(r.bytes),
                r.published_at && new Date(r.published_at).toISOString().slice(0, 10)]
    .filter(Boolean).join(" · ");
  return `<div class="rel">
    <h3>${esc(p.name)}</h3>
    <div class="actions"><a class="btn primary" href="${esc(r.url)}">Download</a></div>
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
    : `<div class="note">No release has been published yet. Build from source
       below, or check back once the first installer ships.</div>`;
} catch (e) {
  box.innerHTML = `<div class="note">Could not load releases: ${esc(e.message)}</div>`;
}

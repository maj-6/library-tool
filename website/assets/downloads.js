import { latestReleases, usingCloud, safeHttpUrl } from "./data.js";
import { isPublicRelease, isStableRelease, releaseChannel } from "./release-policy.js";

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const PLATFORM = {
  windows: { name: "Windows", note: "Installer (.exe)", tag: "Desktop" },
  macos:   { name: "macOS",   note: "Disk image (.dmg)" },
  linux:   { name: "Linux",   note: "AppImage" },
  android: { name: "Android", note: "Library Tool Capture (.apk)" },
};

// Desktop workbench first, then the phone app that feeds it, then the rest.
const PLATFORM_ORDER = ["windows", "android", "macos", "linux"];

// Monochrome platform glyphs (currentColor, decorative). Static markup — never
// interpolated from data — so they carry no injection risk. An unknown platform
// falls back to a generic disc.
const PLATFORM_ICON = {
  windows: `<path d="M3 5 11 3.8V11H3zM13 3.5 21 2.3V11h-8zM3 13h8v7.2L3 19.2zM13 13h8v8.7l-8-1.2z"/>`,
  android: `<path d="M6 9h12v7.5a1 1 0 0 1-1 1H7a1 1 0 0 1-1-1zM3.8 9.4a1.2 1.2 0 0 1 2.4 0v4.8a1.2 1.2 0 0 1-2.4 0zM17.8 9.4a1.2 1.2 0 0 1 2.4 0v4.8a1.2 1.2 0 0 1-2.4 0zM8.6 18.4h1.9v2.4a1.15 1.15 0 0 1-2.3 0zM13.5 18.4h1.9v2.4a1.15 1.15 0 0 1-2.3 0zM6.3 8a5.7 5.7 0 0 1 11.4 0z"/>`,
  macos:   `<path d="M16.4 12.9c0-2.3 1.9-3.4 2-3.5-1.1-1.6-2.8-1.8-3.4-1.8-1.4-.1-2.8.9-3.5.9s-1.8-.9-3-.9c-1.5 0-2.9.9-3.7 2.3-1.6 2.7-.4 6.8 1.1 9 .7 1.1 1.6 2.3 2.8 2.2 1.1 0 1.5-.7 2.9-.7s1.7.7 2.9.7c1.2 0 2-1.1 2.7-2.2.5-.8.9-1.6 1.2-2.5-2.6-1-2.7-3.6-2.7-3.5zM14.3 6.3c.6-.8 1-1.8.9-2.9-.9 0-2 .6-2.6 1.4-.6.7-1.1 1.7-1 2.7 1 .1 2-.5 2.7-1.2z"/>`,
  linux:   `<path d="M12 2c-2 0-3.4 1.7-3.4 3.8v3.4C7.4 10.4 6.6 12 6.6 13.6c0 .9-1 1.7-1 2.9 0 .7.6 1 1.3 1.2.6.2.4 1.3 1.4 1.6 1.2.4 2.4-.4 3.7-.4s2.5.8 3.7.4c1-.3.8-1.4 1.4-1.6.7-.2 1.3-.5 1.3-1.2 0-1.2-1-2-1-2.9 0-1.6-.8-3.2-2-4.4V5.8C15.4 3.7 14 2 12 2z"/>`,
};
const GENERIC_ICON = `<circle cx="12" cy="12" r="9"/>`;
const platIcon = (k) =>
  `<svg class="dl-ico" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">${PLATFORM_ICON[k] || GENERIC_ICON}</svg>`;

const DL_ARROW = `<svg class="dl-arrow" viewBox="0 0 16 16" aria-hidden="true"><path d="M8 1v9M4.5 6.5 8 10l3.5-3.5M2.5 13h11" fill="none" stroke="currentColor" stroke-width="1.6"/></svg>`;

const bytes = (n) => (n ? `${(n / 1048576).toFixed(0)} MB` : "");

// toISOString() throws a RangeError on an unparseable date, which would take the
// whole page down rather than lose one line of metadata.
function day(raw) {
  const d = new Date(raw ?? "");
  return Number.isNaN(d.getTime()) ? "" : d.toISOString().slice(0, 10);
}
// One download as a row: platform glyph, name (+ tag) over its metadata, and a
// ghost download button that fills on hover. The desktop build is tinted as the
// primary one. In the "Pre-release builds" section (opts.channel) a prerelease row
// is badged with its channel (alpha/beta/rc) instead of the platform tag, and is
// never tinted as primary — a testing build shouldn't read as the headline one.
function card(r, opts = {}) {
  const p = PLATFORM[r.platform] || { name: r.platform, note: "" };
  const href = safeHttpUrl(r.url);
  const meta = [p.note, r.version && `v${esc(r.version)}`, bytes(r.bytes), day(r.published_at)]
    .filter(Boolean).join(" · ");
  const chan = releaseChannel(r) || "";
  const isPre = opts.channel && chan && chan !== "stable";
  const tag = isPre
    ? ` <span class="dl-tag is-pre">${esc(chan)}</span>`
    : (p.tag ? ` <span class="dl-tag">${esc(p.tag)}</span>` : "");
  const primary = !isPre && r.platform === "windows";
  // no link = plain visible metadata, never a dead button-shaped control
  const action = href
    ? `<a class="dl-btn" href="${esc(href)}">${DL_ARROW}Download</a>`
    : `<span class="dl-unavail">Unavailable</span>`;
  return `<div class="dl-row${primary ? " is-primary" : ""}">
    ${platIcon(r.platform)}
    <div class="dl-body">
      <div class="dl-name">${esc(p.name)}${tag}</div>
      <div class="dl-meta">${meta}</div>
      ${r.notes ? `<div class="dl-meta">${esc(r.notes)}</div>` : ""}
    </div>
    ${action}
  </div>`;
}

// Only the newest build of each platform+channel; the table keeps the history.
function newest(rows) {
  const best = new Map();
  for (const r of rows) {
    const k = `${r.platform}/${releaseChannel(r)}`;
    if (!best.has(k)) best.set(k, r);            // already ordered newest-first
  }
  return [...best.values()];
}

// Present the desktop workbench first, then the phone app, then anything else.
function ordered(rows) {
  const rank = (r) => {
    const i = PLATFORM_ORDER.indexOf(r.platform);
    return i < 0 ? PLATFORM_ORDER.length : i;
  };
  return [...rows].sort((a, b) => rank(a) - rank(b));
}

// A row is stable unless it carries a non-stable channel (alpha/beta/rc). The
// releases query returns every channel, so the main list MUST filter to stable —
// otherwise a published testing build would leak in as an unlabelled duplicate.
const box = document.getElementById("releases");
const preBox = document.getElementById("prereleases");
try {
  // Fail closed on manually inserted/internal channels and artifacts explicitly
  // labelled DONOTPUBLISH. Filtering before newest() lets an older valid row
  // remain visible when a bad row was inserted later.
  const rows = usingCloud ? (await latestReleases()).filter(isPublicRelease) : [];
  const all = ordered(newest(rows));
  const stable = all.filter(isStableRelease);
  const pre = all.filter((r) => !isStableRelease(r));

  box.innerHTML = stable.length
    ? `<div class="dl-list">${stable.map((r) => card(r)).join("")}</div>`
    : `<div class="note">No release has been published yet — check back once the
       first installer ships, or build from the source on GitHub.</div>`;

  if (preBox) {
    preBox.innerHTML = pre.length
      ? `<div class="dl-list is-compact">${pre.map((r) => card(r, { channel: true })).join("")}</div>`
      : `<div class="note">No testing build is published right now.</div>`;
  }
} catch (e) {
  box.innerHTML = `<div class="note">Could not load releases: ${esc(e.message)}</div>`;
  if (preBox) preBox.innerHTML = `<div class="note">Could not load testing builds.</div>`;
}

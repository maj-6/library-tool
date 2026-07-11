// The site's only data layer.
//
// With window.WHL_CONFIG set (assets/config.js, gitignored) it queries Supabase
// over PostgREST directly -- no SDK, no build step, the same HTTP the desktop's
// supabase_sync.py speaks. Without it, it falls back to fixtures/volumes.json,
// so the site is developable and reviewable before the cloud has any rows.
//
// The anon key is meant to be public. Row-level security is what protects the
// project: anon may read `volumes` and `releases` and nothing else.

const CFG = window.WHL_CONFIG || {};
export const usingCloud = Boolean(CFG.supabaseUrl && CFG.supabaseAnonKey);

function rest(path) {
  return fetch(`${CFG.supabaseUrl.replace(/\/$/, "")}/rest/v1/${path}`, {
    headers: {
      apikey: CFG.supabaseAnonKey,
      Authorization: `Bearer ${CFG.supabaseAnonKey}`,
    },
  }).then((r) => {
    if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
    return r.json();
  });
}

let _fixture = null;
async function fixture() {
  if (!_fixture) {
    const r = await fetch("fixtures/volumes.json");
    if (!r.ok) throw new Error("no cloud configured and no fixture found");
    _fixture = await r.json();
  }
  return _fixture;
}

const norm = (s) => String(s || "").toLowerCase();

/** {q, yearFrom, yearTo, sort, limit, offset} -> {rows, total} */
export async function searchVolumes(opts = {}) {
  const { q = "", yearFrom = null, yearTo = null, sort = "title", limit = 24, offset = 0 } = opts;

  if (!usingCloud) {
    let rows = await fixture();
    if (q) {
      const words = norm(q).split(/\s+/).filter(Boolean);
      rows = rows.filter((v) => {
        const hay = norm([v.title, v.subtitle, v.authors, v.publisher, v.categories, v.description].join(" "));
        return words.every((w) => hay.includes(w));
      });
    }
    if (yearFrom != null) rows = rows.filter((v) => v.year && v.year >= yearFrom);
    if (yearTo != null) rows = rows.filter((v) => v.year && v.year <= yearTo);
    rows = sortRows(rows, sort);
    return { rows: rows.slice(offset, offset + limit), total: rows.length };
  }

  // PostgREST: the database does the search, so a query never ships the catalogue
  const params = ["select=*"];
  if (q) params.push(`fts=plfts(english).${encodeURIComponent(q)}`);
  if (yearFrom != null) params.push(`year=gte.${yearFrom}`);
  if (yearTo != null) params.push(`year=lte.${yearTo}`);
  params.push(`order=${orderClause(sort)}`);
  params.push(`limit=${limit}`, `offset=${offset}`);

  const rows = await rest(`volumes?${params.join("&")}`);
  // a count needs a HEAD with Prefer: count=exact; one extra cheap request
  const total = await countVolumes(q, yearFrom, yearTo).catch(() => rows.length);
  return { rows, total };
}

async function countVolumes(q, yearFrom, yearTo) {
  const params = ["select=id"];
  if (q) params.push(`fts=plfts(english).${encodeURIComponent(q)}`);
  if (yearFrom != null) params.push(`year=gte.${yearFrom}`);
  if (yearTo != null) params.push(`year=lte.${yearTo}`);
  const r = await fetch(`${CFG.supabaseUrl.replace(/\/$/, "")}/rest/v1/volumes?${params.join("&")}`, {
    method: "HEAD",
    headers: {
      apikey: CFG.supabaseAnonKey,
      Authorization: `Bearer ${CFG.supabaseAnonKey}`,
      Prefer: "count=exact",
      Range: "0-0",
    },
  });
  const cr = r.headers.get("content-range") || "";        // "0-0/137"
  const n = parseInt(cr.split("/")[1], 10);
  return Number.isFinite(n) ? n : 0;
}

export async function getVolume(slug) {
  if (!usingCloud) return (await fixture()).find((v) => v.slug === slug) || null;
  const rows = await rest(`volumes?slug=eq.${encodeURIComponent(slug)}&select=*&limit=1`);
  return rows[0] || null;
}

export async function latestReleases() {
  if (!usingCloud) return [];
  return rest("releases?select=*&order=published_at.desc");
}

/** The shared release notes: changelog.md at the site root, the same file the
 *  desktop app bundles. Returns [{version, date, items[]}] newest-first, or []
 *  on any failure so the pages degrade to no changelog rather than an error. */
export async function fetchChangelog() {
  try {
    const r = await fetch("changelog.md");
    if (!r.ok) return [];
    return parseChangelog(await r.text());
  } catch {
    return [];
  }
}

/** A release is "significant" — highlighted on the Downloads page — when it is
 *  a major or minor version, i.e. its patch component is absent or 0 (3.0.0,
 *  3.1.0, 4.0). Cosmetic patch releases (3.0.1, 3.1.2) show only on the full
 *  Release notes page. */
export function isSignificantVersion(version) {
  const patch = String(version || "").trim().replace(/^v/i, "").split(".")[2];
  return patch === undefined || /^0+$/.test(patch);
}

/** Terse markdown -> versions. "## <version> — <date>" starts an entry; "- "
 *  lines are its bullets; a title or any preamble before the first entry is
 *  ignored. Pure text out; the caller escapes before it touches the DOM. */
export function parseChangelog(md) {
  const out = [];
  let cur = null;
  for (const raw of String(md || "").split(/\r?\n/)) {
    const line = raw.trim();
    let m;
    if ((m = /^##\s+(.+?)(?:\s+[—–·-]\s+(.+))?$/.exec(line))) {   // em/en/middot/hyphen date separator
      cur = { version: m[1].trim(), date: (m[2] || "").trim(), items: [] };
      out.push(cur);
    } else if (cur && (m = /^[-*]\s+(.+)$/.exec(line))) {
      cur.items.push(m[1].trim());
    }
  }
  return out;
}

function orderClause(sort) {
  if (sort === "year") return "year.asc.nullslast,title.asc";
  if (sort === "year-desc") return "year.desc.nullslast,title.asc";
  if (sort === "recent") return "created_at.desc";
  return "title.asc";
}

function sortRows(rows, sort) {
  const by = {
    title: (a, b) => a.title.localeCompare(b.title),
    year: (a, b) => (a.year || 9999) - (b.year || 9999) || a.title.localeCompare(b.title),
    "year-desc": (a, b) => (b.year || 0) - (a.year || 0) || a.title.localeCompare(b.title),
    recent: (a, b) => String(b.created_at || "").localeCompare(String(a.created_at || "")),
  }[sort] || ((a, b) => a.title.localeCompare(b.title));
  return [...rows].sort(by);
}

/** Where the PDF actually lives: an absolute URL wins, else the public bucket.
 *
 * pdf_url is a database column, and `volumes` is writable by any authenticated
 * user. Rendering it straight into an href would let a row carrying
 * `javascript:…` run script for every visitor, since escaping the string does
 * nothing to the scheme. Only http(s) survives.
 */
export function pdfHref(v) {
  if (v.pdf_url) return safeHttpUrl(v.pdf_url);
  if (v.pdf_path && usingCloud) {
    return `${CFG.supabaseUrl.replace(/\/$/, "")}/storage/v1/object/public/volumes/${encodeURI(v.pdf_path)}`;
  }
  return "";
}

/** Escaping a string does nothing to its scheme, so anything that reaches an
 *  href has to come through here. Only http(s) survives. */
export function safeHttpUrl(raw) {
  try {
    const u = new URL(String(raw), location.href);
    return u.protocol === "https:" || u.protocol === "http:" ? u.href : "";
  } catch {
    return "";
  }
}

/** A year from a URL is a string an attacker chose: "abc" -> NaN, "1e999" ->
 *  Infinity, and either goes straight into a PostgREST filter. */
export function safeYear(raw) {
  if (raw === null || raw === undefined || raw === "") return null;
  const n = Math.trunc(Number(raw));
  return Number.isFinite(n) && n >= 1000 && n <= 2999 ? n : null;
}

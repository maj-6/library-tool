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

/** Where the PDF actually lives: an absolute URL wins, else the public bucket. */
export function pdfHref(v) {
  if (v.pdf_url) return v.pdf_url;
  if (v.pdf_path && usingCloud) {
    return `${CFG.supabaseUrl.replace(/\/$/, "")}/storage/v1/object/public/volumes/${v.pdf_path}`;
  }
  return "";
}

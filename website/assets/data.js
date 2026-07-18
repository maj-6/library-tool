// The site's only data layer.
//
// With window.WHL_CONFIG set (assets/config.js, gitignored) it queries Supabase
// over PostgREST directly -- no SDK, no build step, the same HTTP the desktop's
// supabase_sync.py speaks. Without it, it falls back to the fixtures/ folder,
// so the site is developable and reviewable before the cloud has any rows.
//
// The anon key is meant to be public. Row-level security is what protects the
// project: anon may read `volumes`, `volume_texts`, `volume_pages`,
// `volume_notes`, `author_pages`, `author_index` and `releases`, and nothing
// else.

const CFG = window.WHL_CONFIG || {};
export const usingCloud = Boolean(CFG.supabaseUrl && CFG.supabaseAnonKey);

// Presentation-only volume title formatting shared by every public-library
// view. The volume remains metadata; this helper only emits escaped display
// markup and never rewrites the stored title.
const titleEsc = (value) => String(value ?? "").replace(/[&<>"']/g, (char) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));

export function bookVolumeValue(volume) {
  if (!volume) return "";
  for (const value of [volume.volume, volume.volume_number]) {
    const number = String(value ?? "").trim();
    if (number) return number;
  }
  return "";
}

export function bookTitleText(volume, fallback = "Untitled") {
  const title = String((volume && volume.title) || fallback || "").trim();
  const number = bookVolumeValue(volume);
  return number ? `Vol. ${number} ${title}` : title;
}

export function bookTitleHtml(volume, fallback = "Untitled") {
  const title = String((volume && volume.title) || fallback || "").trim();
  const number = bookVolumeValue(volume);
  return `<span class="book-title-display">${number
    ? `<span class="volume-title-tag">Vol. ${titleEsc(number)}</span>`
    : ""}<span class="book-title-text">${titleEsc(title)}</span></span>`;
}

// The separator used to render a category path (root -> leaf) as one string.
// It matches the desktop publish step. Category filtering is a path-prefix
// test over `category_paths` (rowInCat below) in both modes, so selecting
// "Botany" takes its whole subtree and never an unrelated branch that merely
// contains the same text somewhere in its rendering.
export const CAT_SEP = " › ";                 // " › "
export const catText = (path) =>
  (Array.isArray(path) ? path : []).map((n) => String(n)).filter(Boolean).join(CAT_SEP);

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

// One GET that carries its own exact total: `Prefer: count=exact` makes
// PostgREST answer "first-last/total" in Content-Range, so the rows and the
// count arrive together -- there is no second count request to race the
// first or to fail on its own. total comes back null when the header is
// missing or uncounted ("0-23/*"); callers must treat null as "unknown",
// never as zero.
async function restWithCount(path) {
  const r = await fetch(`${CFG.supabaseUrl.replace(/\/$/, "")}/rest/v1/${path}`, {
    headers: {
      apikey: CFG.supabaseAnonKey,
      Authorization: `Bearer ${CFG.supabaseAnonKey}`,
      Prefer: "count=exact",
    },
  });
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
  return { rows: await r.json(), total: contentRangeTotal(r.headers.get("content-range")) };
}

// The total out of a Content-Range header: "0-23/137" -> 137, "*/0" -> 0,
// "0-23/*" (the server declined to count), malformed, and missing -> null.
export function contentRangeTotal(header) {
  const m = /^\s*(?:\d+-\d+|\*)\/(\d+)\s*$/.exec(String(header || ""));
  return m ? parseInt(m[1], 10) : null;
}

// PostgREST stored functions: POST /rest/v1/rpc/<name> with the arguments as
// the JSON body, same anon credentials as rest(). Failures surface as
// exceptions -- callers that can degrade (the reader's search) catch and
// fall back rather than letting one missing function break the page.
function rpc(name, params) {
  return fetch(`${CFG.supabaseUrl.replace(/\/$/, "")}/rest/v1/rpc/${name}`, {
    method: "POST",
    headers: {
      apikey: CFG.supabaseAnonKey,
      Authorization: `Bearer ${CFG.supabaseAnonKey}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(params),
  }).then((r) => {
    if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
    return r.json();
  });
}

// A tiny fixture loader/cache. Each fixture file is fetched at most once.
const _fx = {};
async function fixture(name = "volumes") {
  if (!_fx[name]) {
    _fx[name] = fetch(`fixtures/${name}.json`).then((r) => {
      if (!r.ok) throw new Error(`fixture ${name} not found`);
      return r.json();
    });
  }
  return _fx[name];
}

const norm = (s) => String(s || "").toLowerCase();

// PostgREST `ilike` runs SQL LIKE underneath: `%` and `_` are wildcards there,
// and `*` is PostgREST's own wildcard token. A category name an attacker chose
// must not smuggle any of them in, so escape them before wrapping in `*...*`.
const likeEscape = (s) => String(s).replace(/([\\%_*])/g, "\\$1");

// Does a row sit under a category path (subtree prefix match)? This is THE
// definition of category filtering: the fixture path filters rows with it
// directly, and the cloud path uses it to pick slugs out of the facet corpus
// (see searchVolumes) -- one function, so the two modes cannot drift apart.
function rowInCat(v, wanted) {
  if (!wanted) return true;
  const paths = Array.isArray(v.category_paths) ? v.category_paths : [];
  return paths.some((p) => {
    const t = catText(p);
    return t === wanted || t.startsWith(wanted + CAT_SEP);
  });
}

// How many slugs one `slug=in.(...)` filter may carry. Slugs run ~40-60
// characters, so 50 keeps the whole query URL a few KB -- safely inside
// proxy and server header limits -- while a low-thousands corpus still
// resolves in a handful of requests.
const IN_CHUNK = 50;

// PostgREST in-list values are double-quoted so a slug carrying `,` or `)`
// cannot break the list apart.
const inValue = (s) => encodeURIComponent(`"${String(s).replace(/([\\"])/g, "\\$1")}"`);

// Fetch volumes rows for an explicit slug list, IN_CHUNK slugs per request.
// No server-side order: across chunks it would be meaningless anyway, so
// callers sort what comes back.
async function bySlugs(slugs, select, params = []) {
  const out = [];
  for (let i = 0; i < slugs.length; i += IN_CHUNK) {
    const chunk = slugs.slice(i, i + IN_CHUNK);
    out.push(...await rest(
      `volumes?select=${select}&slug=in.(${chunk.map(inValue).join(",")})` +
      (params.length ? `&${params.join("&")}` : "")
    ));
  }
  return out;
}

/** {q, yearFrom, yearTo, cat, lang, author, sort, limit, offset} ->
 *  {rows, total}. total is the exact match count, or null when the cloud
 *  answered with rows but no usable count -- "unknown", which callers must
 *  surface as such and never render as zero. */
export async function searchVolumes(opts = {}) {
  const {
    q = "", yearFrom = null, yearTo = null, cat = "", lang = "", author = "",
    sort = "title", limit = 24, offset = 0,
  } = opts;

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
    if (cat) rows = rows.filter((v) => rowInCat(v, cat));
    if (lang) rows = rows.filter((v) => String(v.language || "") === lang);
    if (author) rows = rows.filter((v) => v.authors === author);
    rows = sortRows(rows, sort);
    return { rows: rows.slice(offset, offset + limit), total: rows.length };
  }

  // PostgREST: the database does the search, so a query never ships the
  // catalogue. Categories are the one exception: PostgREST's flat filters
  // cannot express "some path in this jsonb array starts with this prefix",
  // and an `ilike` substring over the flat categories text matched unrelated
  // branches (selecting the root "Flora" also caught "Botany › Flora"). So a
  // category resolves client-side against the complete, paginated facet
  // corpus with the same rowInCat the fixture path uses, and the rows come
  // back by slug.
  if (cat) {
    const corpus = await facetSource();
    const slugs = corpus.filter((v) => rowInCat(v, cat)).map((v) => v.slug);
    if (!slugs.length) return { rows: [], total: 0 };

    // The slug in-list is chunked (URL length), and chunks come back in
    // table order, not ours -- so the merged set is sorted here, over light
    // rows, before one last fetch hydrates just the visible page. The
    // corpus is small (low thousands); this stays cheap.
    const light = await bySlugs(slugs, "slug,title,year,created_at",
      filterParams(q, yearFrom, yearTo, lang, author));
    const page = sortRows(light, sort).slice(offset, offset + limit);
    const full = new Map((await bySlugs(page.map((v) => v.slug), "*")).map((r) => [r.slug, r]));
    return { rows: page.map((v) => full.get(v.slug)).filter(Boolean), total: light.length };
  }

  const params = filterParams(q, yearFrom, yearTo, lang, author);
  params.unshift("select=*");
  params.push(`order=${orderClause(sort)}`);
  params.push(`limit=${limit}`, `offset=${offset}`);
  // rows + exact total in one request; total is null if the count is unavailable
  return restWithCount(`volumes?${params.join("&")}`);
}

// The filter half of a volumes query. Category is not here: it cannot be a
// plain PostgREST filter (see searchVolumes) and resolves to a slug list.
function filterParams(q, yearFrom, yearTo, lang, author) {
  const params = [];
  if (q) params.push(`fts=plfts(english).${encodeURIComponent(q)}`);
  if (yearFrom != null) params.push(`year=gte.${yearFrom}`);
  if (yearTo != null) params.push(`year=lte.${yearTo}`);
  if (lang) params.push(`language=eq.${encodeURIComponent(lang)}`);
  // Exact match: this is "this literal published author string," matching how
  // author_pages/author_index are keyed -- never a substring/ilike filter here.
  if (author) params.push(`authors=eq.${encodeURIComponent(author)}`);
  return params;
}

export async function getVolume(slug) {
  if (!usingCloud) return (await fixture()).find((v) => v.slug === slug) || null;
  const rows = await rest(`volumes?slug=eq.${encodeURIComponent(slug)}&select=*&limit=1`);
  return rows[0] || null;
}

/** The About article (Markdown) for a volume, or "" when there is none. */
export async function getAbout(slug) {
  if (!usingCloud) {
    const t = (await fixture("texts").catch(() => ({})))[slug];
    return (t && t.about) || "";
  }
  const rows = await rest(
    `volume_texts?slug=eq.${encodeURIComponent(slug)}&kind=eq.about&select=body,lang&order=lang.asc&limit=1`
  );
  return (rows[0] && rows[0].body) || "";
}

/** Anchored annotations for a volume, page-ascending. */
export async function getNotes(slug) {
  if (!usingCloud) {
    return (await fixture("notes").catch(() => ({})))[slug] || [];
  }
  return rest(
    `volume_notes?slug=eq.${encodeURIComponent(slug)}&select=note_id,page,quote,kind,body&order=page.asc,note_id.asc`
  );
}

/** Page-aligned text: lang "" is the original layer; "es"/"de"/... translations.
 *  Returns a { <page>: <text> } map for pages in [from, to]. */
export async function getPages(slug, lang = "", from = 1, to = 9999) {
  const out = {};
  if (!usingCloud) {
    const byLang = (await fixture("pages").catch(() => ({})))[slug] || {};
    const src = byLang[lang] || {};
    for (const k of Object.keys(src)) {
      const p = Number(k);
      if (p >= from && p <= to) out[p] = src[k];
    }
    return out;
  }
  const rows = await rest(
    `volume_pages?slug=eq.${encodeURIComponent(slug)}&lang=eq.${encodeURIComponent(lang)}` +
    `&page=gte.${from}&page=lte.${to}&select=page,body&order=page.asc`
  );
  for (const r of rows) out[r.page] = r.body;
  return out;
}

/** Every page of one text layer, page-ascending: [{page, body}]. This feeds
 *  in-book search, which needs the whole text at once. PostgREST silently caps
 *  an unpaginated response (~1000 rows), so the cloud path walks limit/offset
 *  batches until a short batch says the table is drained -- a long book must
 *  never come back silently truncated. */
export async function getAllPages(slug, lang = "") {
  if (!usingCloud) {
    const byLang = (await fixture("pages").catch(() => ({})))[slug] || {};
    const src = byLang[lang] || {};
    return Object.keys(src)
      .map((k) => ({ page: Number(k), body: src[k] }))
      .sort((a, b) => a.page - b.page);
  }
  const out = [];
  const limit = 500;
  for (let offset = 0; ; offset += limit) {
    const rows = await rest(
      `volume_pages?slug=eq.${encodeURIComponent(slug)}&lang=eq.${encodeURIComponent(lang)}` +
      `&select=page,body&order=page.asc&limit=${limit}&offset=${offset}`
    );
    out.push(...rows);
    if (rows.length < limit) break;
  }
  return out;
}

/** Ranked in-book search in one round-trip: the search_volume RPC (Postgres
 *  FTS with a trigram fallback over the published search layer) returns
 *  [{page, rank, snippet}], the snippet carrying «...» around each match --
 *  see rpcSnippetHtml in textsearch.js. Cloud only: fixture mode has no
 *  database, and a live project still behind on
 *  docs/cloud/migrations/003_page_search.sql answers 404 -- the reader
 *  catches either and falls back to the client-side search path. */
export async function searchVolume(slug, q, lang = "") {
  if (!usingCloud) throw new Error("no cloud configured");
  const rows = await rpc("search_volume",
    { p_slug: slug, p_query: q, p_lang: lang });
  return Array.isArray(rows) ? rows : [];
}

/** The light rows the browse page needs to build its facets client-side:
 *  {slug, category_paths, language, year} for EVERY volume. PostgREST
 *  silently caps an unpaginated response (~1000 rows), so the cloud path
 *  walks ordered limit/offset batches until a short batch says the table is
 *  drained (the getAllPages idiom) -- a growing catalogue must never come
 *  back silently truncated, or the facet rail and the category filter would
 *  both quietly lose volumes. Cached: the browse page boots from it and
 *  every category-filtered search reuses it. */
let _facetRows = null;
export async function facetSource() {
  if (!usingCloud) {
    return (await fixture()).map((v) => ({
      slug: v.slug,
      category_paths: v.category_paths || [],
      language: v.language || "",
      year: v.year ?? null,
    }));
  }
  if (!_facetRows) {
    _facetRows = (async () => {
      const out = [];
      const limit = 500;
      for (let offset = 0; ; offset += limit) {
        const rows = await rest(
          `volumes?select=slug,category_paths,language,year&order=slug.asc&limit=${limit}&offset=${offset}`
        );
        out.push(...rows);
        if (rows.length < limit) break;
      }
      return out;
    })();
    _facetRows.catch(() => { _facetRows = null; });   // a failed walk may be retried
  }
  return _facetRows;
}

/** Title matches for autocomplete: [{slug, title, volume, authors, year}]. */
export async function suggestTitles(q, limit = 6) {
  const words = norm(q);
  if (!words) return [];
  if (!usingCloud) {
    const rows = (await fixture()).filter((v) => norm(v.title).includes(words));
    return sortRows(rows, "title").slice(0, limit)
      .map((v) => ({ slug: v.slug, title: v.title, volume: v.volume,
        authors: v.authors, year: v.year }));
  }
  return rest(
    `volumes?select=slug,title,volume,authors,year&title=ilike.*${encodeURIComponent(likeEscape(q))}*` +
    `&order=title.asc&limit=${limit}`
  );
}

/** Author matches for the search-box autocomplete: [{author, work_count}],
 *  grouped on the exact `authors` string (see author_pages/author_index — no
 *  name-variant merging). */
export async function suggestAuthors(q, limit = 6) {
  const words = norm(q);
  if (!words) return [];
  if (!usingCloud) {
    const counts = new Map();
    for (const v of await fixture()) {
      const a = v.authors;
      if (a) counts.set(a, (counts.get(a) || 0) + 1);
    }
    return [...counts.entries()]
      .filter(([author]) => norm(author).includes(words))
      .map(([author, work_count]) => ({ author, work_count }))
      .sort((a, b) => b.work_count - a.work_count || a.author.localeCompare(b.author))
      .slice(0, limit);
  }
  return rest(
    `author_index?author=ilike.*${encodeURIComponent(likeEscape(q))}*` +
    `&order=work_count.desc,author.asc&limit=${limit}`
  );
}

/** The bio (Markdown) for an author, or "" when none has been written yet. */
export async function getAuthorBio(author) {
  if (!usingCloud) {
    const a = (await fixture("authors").catch(() => ({})))[author];
    return (a && a.bio) || "";
  }
  const rows = await rest(`author_pages?author=eq.${encodeURIComponent(author)}&select=bio&limit=1`);
  return (rows[0] && rows[0].bio) || "";
}

export async function latestReleases() {
  if (!usingCloud) return [];
  return rest("releases?select=*&order=published_at.desc");
}

/** The shared release notes: changelog.md at the site root, the same file the
 *  desktop app bundles. Returns [{version, date, categories, items}] newest-first,
 *  or [] on any failure so the pages degrade to no changelog rather than an error. */
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

const CHANGELOG_CATEGORIES = new Map([
  ["additions", "Additions"],
  ["other changes", "Other Changes"],
  ["bugfixes", "Bugfixes"],
]);

function changelogCategory(cur, name) {
  let category = cur.categories.find((c) => c.name === name);
  if (!category) {
    category = { name, items: [] };
    cur.categories.push(category);
  }
  return category;
}

/** Terse markdown -> versions. "## <version> — <date>" starts an entry and
 *  "### Additions", "### Other Changes", or "### Bugfixes" starts a category.
 *  Plain bullets without a category remain supported as Other Changes. Titles,
 *  preamble, and unknown subheadings are ignored. Pure text out; the caller
 *  escapes before it touches the DOM. */
export function parseChangelog(md) {
  const out = [];
  let cur = null;
  let category = null;
  for (const raw of String(md || "").split(/\r?\n/)) {
    const line = raw.trim();
    let m;
    if ((m = /^##\s+(.+?)(?:\s+[—–·-]\s+(.+))?$/.exec(line))) {   // em/en/middot/hyphen date separator
      cur = {
        version: m[1].trim(),
        date: (m[2] || "").trim(),
        categories: [],
        items: [],
      };
      out.push(cur);
      category = null;
    } else if (cur && (m = /^###\s+(.+)$/.exec(line))) {
      const name = CHANGELOG_CATEGORIES.get(m[1].trim().toLowerCase());
      category = name ? changelogCategory(cur, name) : null;
    } else if (cur && (m = /^[-*]\s+(.+)$/.exec(line))) {
      const item = m[1].trim();
      (category || changelogCategory(cur, "Other Changes")).items.push(item);
      cur.items.push(item);                    // legacy flat-list consumers
    }
  }
  return out;
}

/** Group a newest-first version list by major version, order preserved:
 *  [{major:"3", versions:[…]}, …]. The Release notes page renders each major as
 *  a heading with its versions as subheadings. */
export function groupByMajor(versions) {
  const groups = [];
  const at = new Map();
  for (const v of versions || []) {
    const major = String(v.version || "").replace(/^v/i, "").split(".")[0] || "?";
    if (!at.has(major)) { at.set(major, groups.length); groups.push({ major, versions: [] }); }
    groups[at.get(major)].versions.push(v);
  }
  return groups;
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

/** Where the PDF actually lives.
 *
 * In fixture mode there is no cloud storage, so any volume whose fixture assets
 * declare an original text layer (assets.pages) is served the bundled
 * fixtures/sample.pdf -- a relative path, resolved against the page that reads
 * it (browse/book/read all sit at the site root, so it resolves the same way).
 *
 * On the cloud path, pdf_url is a database column and `volumes` is
 * attacker-influenceable, so a row carrying `javascript:…` would run script for
 * every visitor; escaping the string does nothing to its scheme. Only http(s)
 * survives, via safeHttpUrl().
 */
export function pdfHref(v) {
  if (!usingCloud) {
    const a = (v && v.assets) || {};
    return a.pages ? "fixtures/sample.pdf" : "";
  }
  if (v.pdf_url) return safeHttpUrl(v.pdf_url);
  if (v.pdf_path) {
    return `${CFG.supabaseUrl.replace(/\/$/, "")}/storage/v1/object/public/volumes/${encodeURI(v.pdf_path)}`;
  }
  return "";
}

/** Where the thumbnail actually lives -- same dual-field pattern as pdfHref().
 *
 * In fixture mode, a volume whose assets declare a thumbnail (assets.thumbnail)
 * is served the bundled fixtures/sample-thumb.jpg, mirroring how assets.pages
 * serves fixtures/sample.pdf.
 */
export function thumbHref(v) {
  if (!usingCloud) {
    const a = (v && v.assets) || {};
    return a.thumbnail ? "fixtures/sample-thumb.jpg" : "";
  }
  if (v.thumbnail_url) return safeHttpUrl(v.thumbnail_url);
  if (v.thumbnail_path) {
    return `${CFG.supabaseUrl.replace(/\/$/, "")}/storage/v1/object/public/volumes/${encodeURI(v.thumbnail_path)}`;
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

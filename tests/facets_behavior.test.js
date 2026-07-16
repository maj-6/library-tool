// The facet/count contract (issue #115): the facet corpus is paginated until
// drained (never silently truncated at the API row cap), category filtering
// is the same exact path-prefix test in fixture and cloud modes (one
// rowInCat, no ilike substring), the slug in-list is chunked with the sort
// preserved across chunks, and an exact count rides on the rows request --
// a missing count is "unavailable", never an invented zero.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");


const root = path.join(__dirname, "..");
const websiteData = fs.readFileSync(
  path.join(root, "website", "assets", "data.js"), "utf8");
const websiteBrowse = fs.readFileSync(
  path.join(root, "website", "assets", "browse.js"), "utf8");

function block(source, startMarker, endMarker) {
  const start = source.indexOf(startMarker);
  const end = source.indexOf(endMarker, start);
  assert.ok(start >= 0 && end > start, `${startMarker} block is present`);
  return source.slice(start, end);
}

// data.js is an ES module with no imports, so with the export keywords
// stripped the whole file runs in a bare vm context -- module state (the
// fixture and facet caches) is fresh per context, and `fetch` is a stub.
const dataSource = websiteData.replace(/^export /gm, "");

function dataApi({ cloud = true, fetch: fetchFn }) {
  const context = vm.createContext({
    window: {
      WHL_CONFIG: cloud
        ? { supabaseUrl: "https://cloud.test", supabaseAnonKey: "anon" }
        : {},
    },
    fetch: fetchFn,
  });
  vm.runInContext(
    `${dataSource}\nthis.api = { searchVolumes, facetSource, contentRangeTotal };`,
    context,
  );
  return context.api;
}

// A recording fetch stub. Routes are [match(url, init), respond(url, init)]
// pairs, first match wins; a responder returns {json, contentRange, fail}.
function fetchStub(routes) {
  const calls = [];
  const fn = async (url, init = {}) => {
    calls.push({ url: String(url), init });
    for (const [match, respond] of routes) {
      if (!match(String(url), init)) continue;
      const out = respond(String(url), init) || {};
      if (out.fail) {
        return { ok: false, status: 500, statusText: "boom",
                 json: async () => { throw new Error("no body"); },
                 headers: { get: () => null } };
      }
      return {
        ok: true, status: 200, statusText: "OK",
        json: async () => out.json ?? [],
        headers: { get: (k) =>
          (String(k).toLowerCase() === "content-range" ? out.contentRange ?? null : null) },
      };
    }
    throw new Error(`unexpected fetch: ${url}`);
  };
  fn.calls = calls;
  return fn;
}

const qp = (url, key) => new URLSearchParams(url.split("?")[1]).get(key);
const inListSlugs = (url) => {
  const raw = qp(url, "slug") || "";                       // 'in.("a","b")'
  assert.ok(raw.startsWith("in.(") && raw.endsWith(")"), `slug filter is an in-list: ${raw}`);
  return raw.slice(4, -1).split(",").map((s) => JSON.parse(s));
};
const plain = (value) => JSON.parse(JSON.stringify(value));

const isFacetWalk = (u) => u.includes("select=slug,category_paths,language,year");
const isLightFetch = (u) => u.includes("select=slug,title,year,created_at");
const isFullFetch = (u) => u.includes("select=*");

// A corpus whose category texts overlap as substrings: under the old ilike
// filter "Herb" also caught "Herbals"/"Herbal Medicine", and the root
// "Botany" also caught "Gardens › Botany" on an unrelated branch. Path-prefix
// semantics must keep them apart. One row is multi-path.
const CATALOGUE = [
  { slug: "herbals-1600", title: "Herball", year: 1600, created_at: "2026-01-01",
    language: "English", category_paths: [["Botany", "Herbals"]] },
  { slug: "botany-root", title: "Botanicon", year: 1650, created_at: "2026-01-02",
    language: "Latin", category_paths: [["Botany"]] },
  { slug: "garden-botany", title: "Garden Botany", year: 1700, created_at: "2026-01-03",
    language: "English", category_paths: [["Gardens", "Botany"]] },
  { slug: "herbal-medicine", title: "Remedies", year: 1750, created_at: "2026-01-04",
    language: "English", category_paths: [["Materia Medica", "Herbal Medicine"]] },
  { slug: "herb-root", title: "Of Herb", year: 1800, created_at: "2026-01-05",
    language: "English", category_paths: [["Herb"]] },
  { slug: "multi-path", title: "Anthology", year: 1850, created_at: "2026-01-06",
    language: "French", category_paths: [["Botany", "Flora"], ["Medicine", "Regimen"]] },
];

const lightRow = (v) =>
  ({ slug: v.slug, title: v.title, year: v.year, created_at: v.created_at });
const facetRow = (v) =>
  ({ slug: v.slug, category_paths: v.category_paths, language: v.language, year: v.year });

// The standard cloud stub over a catalogue: facet walk, chunked light
// fetches, chunked full-row hydration. Table order is deliberately NOT the
// requested order, so any sort the caller sees was applied client-side.
function cloudRoutes(catalogue) {
  const bySlug = new Map(catalogue.map((v) => [v.slug, v]));
  return [
    [isFacetWalk, (u) => {
      const limit = Number(qp(u, "limit")), offset = Number(qp(u, "offset"));
      assert.ok(qp(u, "order"), "the facet walk is ordered, so batches cannot shuffle");
      return { json: catalogue.slice(offset, offset + limit).map(facetRow) };
    }],
    [isLightFetch, (u) =>
      ({ json: inListSlugs(u).map((s) => bySlug.get(s)).filter(Boolean).map(lightRow) })],
    [isFullFetch, (u) =>
      ({ json: inListSlugs(u).map((s) => bySlug.get(s)).filter(Boolean) })],
  ];
}

function fixtureRoutes(catalogue) {
  return [[(u) => u.includes("fixtures/volumes.json"), () => ({ json: catalogue })]];
}

test("cloud facetSource paginates until a short batch drains the table", async () => {
  const corpus = [];
  for (let i = 0; i < 1137; i++) {
    corpus.push({ slug: `v-${String(i).padStart(4, "0")}`,
                  category_paths: [["Botany"]], language: "English", year: 1700 });
  }
  const fetchFn = fetchStub([[isFacetWalk, (u) => {
    const limit = Number(qp(u, "limit")), offset = Number(qp(u, "offset"));
    assert.ok(limit > 0, "the walk is limit/offset paginated");
    assert.ok(qp(u, "order"), "the walk is ordered");
    return { json: corpus.slice(offset, offset + limit) };
  }]]);
  const api = dataApi({ fetch: fetchFn });

  const rows = plain(await api.facetSource());
  assert.equal(rows.length, 1137);                 // nothing truncated at ~1000
  assert.equal(fetchFn.calls.length, 3);           // 500 + 500 + 137
  assert.deepEqual(rows.map((r) => r.slug), corpus.map((r) => r.slug));

  await api.facetSource();                         // cached: no second walk
  assert.equal(fetchFn.calls.length, 3);
});

test("category filtering is exact path-prefix in both modes, and they agree", async () => {
  const expected = {
    // title order: Anthology, Botanicon, Herball
    "Botany": ["multi-path", "botany-root", "herbals-1600"],
    "Botany › Herbals": ["herbals-1600"],
    "Herb": ["herb-root"],                        // NOT Herbals / Herbal Medicine
    "Materia Medica": ["herbal-medicine"],
    "Medicine": ["multi-path"],                   // via its second path
    "Nowhere": [],
  };
  for (const [cat, want] of Object.entries(expected)) {
    const fixtureApi = dataApi({ cloud: false, fetch: fetchStub(fixtureRoutes(CATALOGUE)) });
    const cloudFetch = fetchStub(cloudRoutes(CATALOGUE));
    const cloudApi = dataApi({ fetch: cloudFetch });

    const fx = plain(await fixtureApi.searchVolumes({ cat }));
    const cl = plain(await cloudApi.searchVolumes({ cat }));
    assert.deepEqual(fx.rows.map((r) => r.slug), want, `fixture: ${cat}`);
    assert.deepEqual(cl.rows.map((r) => r.slug), want, `cloud: ${cat}`);
    assert.equal(fx.total, want.length);
    assert.equal(cl.total, want.length);

    // the cloud never falls back to the substring filter, and never issues
    // a separate HEAD count request
    for (const { url, init } of cloudFetch.calls) {
      assert.ok(!url.includes("ilike"), `no substring category filter: ${url}`);
      assert.notEqual(init.method, "HEAD");
    }
  }
});

test("the slug in-list is chunked and the sort survives across chunks", async () => {
  // 120 matching volumes whose title order is the reverse of slug order, so a
  // per-chunk sort (or none) would interleave wrongly.
  const catalogue = [];
  for (let i = 0; i < 120; i++) {
    catalogue.push({
      slug: `c-${String(i).padStart(3, "0")}`,
      title: `T${String(119 - i).padStart(3, "0")}`,
      year: 1600 + i, created_at: `2026-01-01T00:00:${String(i % 60).padStart(2, "0")}`,
      language: "English", category_paths: [["Botany"]],
    });
  }
  const fetchFn = fetchStub(cloudRoutes(catalogue));
  const api = dataApi({ fetch: fetchFn });

  const res = plain(await api.searchVolumes({ cat: "Botany", sort: "title", limit: 24, offset: 0 }));
  assert.equal(res.total, 120);
  assert.equal(res.rows.length, 24);
  // title T000..T023 live on slugs c-119..c-096: a cross-chunk merge is required
  assert.deepEqual(
    res.rows.map((r) => r.slug),
    Array.from({ length: 24 }, (_, i) => `c-${String(119 - i).padStart(3, "0")}`),
  );

  const light = fetchFn.calls.filter((c) => isLightFetch(c.url));
  assert.equal(light.length, 3);                   // 50 + 50 + 20
  assert.deepEqual(light.map((c) => inListSlugs(c.url).length), [50, 50, 20]);
  const full = fetchFn.calls.filter((c) => isFullFetch(c.url));
  assert.equal(full.length, 1);                    // only the visible page hydrates
  assert.equal(inListSlugs(full[0].url).length, 24);
  for (const { url } of fetchFn.calls) {
    assert.ok(url.length < 4096, `query URL stays inside header limits: ${url.length}`);
  }
});

test("other filters ride the chunked requests; in-list values are quoted", async () => {
  const fetchFn = fetchStub(cloudRoutes(CATALOGUE));
  const api = dataApi({ fetch: fetchFn });
  await api.searchVolumes({ cat: "Botany", lang: "Latin", yearFrom: 1600 });

  const light = fetchFn.calls.filter((c) => isLightFetch(c.url));
  assert.ok(light.length >= 1);
  for (const { url } of light) {
    assert.ok(url.includes("language=eq.Latin"), "lang filter reaches the server");
    assert.ok(url.includes("year=gte.1600"), "year filter reaches the server");
    assert.ok(url.includes("%22"), "in-list slugs are quoted");
  }
});

test("content-range parsing: totals, zero, and the uncounted star", () => {
  const { contentRangeTotal } = dataApi({ fetch: async () => { throw new Error("unused"); } });
  assert.equal(contentRangeTotal("0-23/137"), 137);
  assert.equal(contentRangeTotal("5-9/42"), 42);
  assert.equal(contentRangeTotal("*/0"), 0);       // an empty result still counts
  assert.equal(contentRangeTotal("0-23/*"), null); // the server declined to count
  assert.equal(contentRangeTotal(""), null);
  assert.equal(contentRangeTotal(null), null);
  assert.equal(contentRangeTotal("garbage"), null);
});

test("the exact count rides the rows request -- one GET, no second race", async () => {
  const rows = CATALOGUE.slice(0, 2);
  const fetchFn = fetchStub([[isFullFetch, () => ({ json: rows, contentRange: "0-1/57" })]]);
  const api = dataApi({ fetch: fetchFn });

  const res = plain(await api.searchVolumes({ q: "herb" }));
  assert.equal(res.total, 57);
  assert.equal(res.rows.length, 2);
  assert.equal(fetchFn.calls.length, 1);
  const { url, init } = fetchFn.calls[0];
  assert.equal(init.headers.Prefer, "count=exact");
  assert.ok(!init.method, "a plain GET, not a HEAD count probe");
  assert.ok(url.includes("limit=24") && url.includes("offset=0"));
});

test("a missing count is null (unavailable), never zero over visible rows", async () => {
  const rows = CATALOGUE.slice(0, 3);
  const fetchFn = fetchStub([[isFullFetch, () => ({ json: rows, contentRange: "0-2/*" })]]);
  const api = dataApi({ fetch: fetchFn });

  const res = plain(await api.searchVolumes({}));
  assert.equal(res.rows.length, 3);
  assert.equal(res.total, null);                   // unknown -- not 0, not rows.length
});

test("API failures reject; an empty category answers without extra requests", async () => {
  const failing = dataApi({ fetch: fetchStub([[() => true, () => ({ fail: true })]]) });
  await assert.rejects(failing.searchVolumes({}), /500/);
  await assert.rejects(failing.searchVolumes({ cat: "Botany" }), /500/);

  const fetchFn = fetchStub(cloudRoutes(CATALOGUE));
  const api = dataApi({ fetch: fetchFn });
  const res = plain(await api.searchVolumes({ cat: "Nowhere" }));
  assert.deepEqual(res, { rows: [], total: 0 });
  assert.ok(fetchFn.calls.every((c) => isFacetWalk(c.url)),
    "no slug fetches for a category that matches nothing");
});

test("the browse count line says 'unavailable', never an invented number", () => {
  const context = vm.createContext({});
  const label = block(websiteBrowse, "function countLabel", "let seq = 0");
  vm.runInContext(`const PAGE = 24;\n${label}\nthis.api = { countLabel };`, context);
  const { countLabel } = context.api;

  assert.equal(countLabel(null, 24, 1), "Showing 1–24 · count unavailable");
  assert.equal(countLabel(null, 10, 25), "Showing 25–34 · count unavailable");
  assert.equal(countLabel(null, 0, 1), "Count unavailable");
  assert.equal(countLabel(0, 0, 1), "0 volumes");
  assert.equal(countLabel(1, 1, 1), "1 volume");
  assert.equal(countLabel(20, 20, 1), "20 volumes");
  assert.equal(countLabel(137, 24, 25), "137 volumes · showing 25–48");
  assert.equal(countLabel(30, 6, 25), "30 volumes · showing 25–30");
});

test("one rowInCat defines category semantics for both modes", () => {
  const definitions = websiteData.match(/function rowInCat\(/g) || [];
  assert.equal(definitions.length, 1);
  const uses = websiteData.match(/rowInCat\(v, /g) || [];
  assert.ok(uses.length >= 2, "fixture filtering and the cloud slug set share it");
  assert.ok(!websiteData.includes("categories=ilike"),
    "the substring category filter is gone");
});

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");


const appPath = path.join(
  __dirname, "..", "tools", "whl_explorer", "static", "app.js");
const source = fs.readFileSync(appPath, "utf8");

function block(startMarker, endMarker) {
  const start = source.indexOf(startMarker);
  const end = source.indexOf(endMarker, start);
  assert.ok(start >= 0 && end > start, `${startMarker} block is present`);
  return source.slice(start, end);
}

function declaration(name) {
  const plain = `function ${name}(`;
  const async = `async function ${name}(`;
  let start = source.indexOf(async);
  if (start < 0) start = source.indexOf(plain);
  assert.ok(start >= 0, `${name} declaration is present`);
  const end = /^}\r?$/m.exec(source.slice(start));
  assert.ok(end, `${name} declaration has a closing brace`);
  return source.slice(start, start + end.index + end[0].length);
}

function plain(value) {
  return JSON.parse(JSON.stringify(value));
}

const modelSource = [
  declaration("homeAttentionDestination"),
  declaration("bookVolumeValue"),
  block("const REMARK_CATEGORIES = [", "const remarksState ="),
  declaration("attnHas"),
  declaration("attnValue"),
  declaration("attnReason"),
  declaration("attnMeta"),
  declaration("remarkKeyParts"),
  declaration("remarkDecodePart"),
  declaration("replicaPageRemarkKey"),
  declaration("replicaPageRemarkParts"),
  declaration("publicationRemarkKey"),
  declaration("publicationRemarkSelection"),
  declaration("remarkCategoryForKey"),
  declaration("sourceRemarkKeys"),
  declaration("activeSourceRemarkKey"),
  declaration("sourceMatchesRemark"),
  declaration("setAttnKey"),
  declaration("remappedPageRemarkValue"),
  declaration("remapPageRemarkKeys"),
  declaration("remarksDefaultFilter"),
  declaration("remarksFilterForTab"),
  declaration("setRemarksFilterForTab"),
  declaration("remarkSecondary"),
  declaration("replicaPageSourceAvailable"),
  declaration("keyedRemarkDescriptor"),
  declaration("reviewLabelBook"),
  declaration("reviewOnlyRemarkDescriptor"),
  declaration("remarksItems"),
  declaration("remarkRoute"),
  declaration("remarkReviewForItem"),
  declaration("remarkCommentCount"),
].join("\n");

function remarksHarness({ state: stateSeed, rows = [], sources = [], reviews = {} } = {}) {
  const state = Object.assign({
    settings: { remarksFilters: {} },
    builds: {},
    attn: {},
    whlRows: [],
    chBooks: [],
    olRows: [],
    publishEntries: [],
    publishLoaded: false,
    rowsById: new Map(),
  }, stateSeed || {});
  const saves = [];
  const writes = [];
  const publishFindEntity = (key) => {
    if (String(key || "").startsWith("book:")) {
      const slug = String(key).slice(5);
      const entry = (state.publishEntries || []).find((item) => item.slug === slug);
      return entry ? { kind: "book", key, label: entry.title || "Untitled", entries: [entry] }
        : null;
    }
    if (String(key || "").startsWith("set:")) {
      const groupId = String(key).slice(4);
      const entries = (state.publishEntries || []).filter(
        (item) => String(item.group_id || "") === groupId);
      return entries.length
        ? { kind: "set", key, label: entries[0].title || groupId, entries } : null;
    }
    return null;
  };
  const context = vm.createContext({
    state,
    reviewsState: { items: reviews },
    combinedRows: () => rows,
    approvedSources: () => sources,
    publishFindEntity,
    saveSettings: () => saves.push(plain(state.settings.remarksFilters)),
    localStorage: { setItem: (...args) => writes.push(args) },
    pushClientState: (kind) => writes.push(["push", kind]),
    renderReplicaAttentionMarks: () => {},
    decorateOcrPages: () => {},
    decorateAnFacsimile: () => {},
    renderRemarks: () => {},
    renderHome: () => {},
    ATTN_KEY: "attention",
    ATTN_DIRTY_KEY: "attention-dirty",
  });
  vm.runInContext(`${modelSource}
this.api = {
  homeAttentionDestination,
  REMARK_CATEGORIES, REMARK_TAB_DEFAULTS,
  attnValue, attnReason, remarkKeyParts, remarkCategoryForKey,
  replicaPageRemarkKey, replicaPageRemarkParts,
  publicationRemarkKey, publicationRemarkSelection,
  sourceRemarkKeys, activeSourceRemarkKey, sourceMatchesRemark, setAttnKey,
  remapPageRemarkKeys,
  remarksDefaultFilter, remarksFilterForTab, setRemarksFilterForTab,
  reviewLabelBook, reviewOnlyRemarkDescriptor,
  remarksItems, remarkRoute, remarkReviewForItem, remarkCommentCount,
};`, context);
  return { api: context.api, saves, state, writes };
}

function pageDeleteHarness({
  saveResponses = [{ httpOk: true, data: { ok: true } }],
  deleteData = { ok: true, backup: "book.bak.pdf" },
  refreshError = null,
} = {}) {
  const bid = "book-a";
  const messages = { "ocr-msg": { textContent: "" } };
  const requests = [];
  const events = [];
  const docs = [
    { id: "doc-1", buildId: bid, fileName: "compiled.txt", text: "saved text" },
    { id: "doc-2", buildId: bid, fileName: "extracted.txt", text: "second text" },
  ];
  const state = { builds: { [bid]: { id: bid, updated_at: "revision-1" } } };
  const ocrState = {
    docs,
    pageSel: new Set([2]),
    pageRunning: new Map(),
    pageTags: new Map([[`${bid}:primary:2`, "tesseract"]]),
    analysisTags: new Map([[`${bid}:primary:2`, { engine: "test" }]]),
    pdfInfo: { "source/book.pdf": { pages: 3 } },
    wordsCache: new Map([["word", {}]]),
    regionsCache: new Map([["region", {}]]),
    layoutMeta: { [bid]: {} },
    bookLoading: null,
  };
  let saveIndex = 0;
  const response = (spec) => ({
    ok: spec.httpOk !== false,
    status: spec.status || (spec.httpOk === false ? 500 : 200),
    json: async () => {
      if (spec.jsonError) throw new Error("invalid json");
      return spec.data;
    },
  });
  const context = vm.createContext({
    state,
    ocrState,
    el: (id) => messages[id],
    ocrSelDoc: () => docs[0],
    docPdf: () => "source/book.pdf",
    confirmDialog: async () => true,
    ocrSyncEditor: () => {
      events.push("sync-editor");
      docs[0].text = "unsaved editor text";
    },
    fetch: async (url, init) => {
      requests.push({ url, init, body: JSON.parse(init.body) });
      events.push(url);
      if (url.endsWith("/ocr")) {
        const spec = saveResponses[Math.min(saveIndex++, saveResponses.length - 1)];
        if (spec.requestError) throw new Error(spec.requestError);
        return response(spec);
      }
      if (url === "/api/pdf/pages/delete") return response({
        httpOk: true, data: deleteData,
      });
      throw new Error(`unexpected request: ${url}`);
    },
    pushClientState: (kind) => events.push(`push-${kind}`),
    flushClientState: async () => { events.push("flush-client-state"); return true; },
    remapPageRemarkKeys: () => { events.push("remap-remarks"); return true; },
    loadReviews: async () => { events.push("load-reviews"); },
    renderRemarks: () => events.push("render-remarks"),
    renderHome: () => events.push("render-home"),
    clearOcrPageSel: () => ocrState.pageSel.clear(),
    status: (message) => events.push(`status:${message}`),
    loadOcrBooks: async () => {
      events.push("load-ocr-books");
      if (refreshError) throw refreshError;
    },
    selectOcrBook: async () => events.push("select-ocr-book"),
    setOcrView: (view) => events.push(`view-${view}`),
    // the delete adds a trash row, so it invalidates that pane's short cache
    trashState: { data: null, loading: false, error: "", loadedAt: 12345 },
    activeInfoSection: () => "info-console",
    loadTrash: async () => events.push("load-trash"),
  });
  vm.runInContext([
    declaration("saveOcrDocumentsBeforePageDelete"),
    declaration("deleteSelectedPages"),
    "this.api = { deleteSelectedPages };",
  ].join("\n"), context);
  return { api: context.api, docs, events, messages, ocrState, requests, state };
}

test("attention values and keyed references preserve legacy marks and URL colons", () => {
  const { api } = remarksHarness();

  assert.equal(api.attnValue(true), "1");
  assert.equal(api.attnValue({ value: "Check the date" }), "Check the date");
  assert.equal(api.attnValue({ reason: "Legacy object" }), "Legacy object");
  assert.equal(api.attnReason("1"), "");
  assert.equal(api.attnReason({ value: "Check the date" }), "Check the date");

  const sourceKey = "src:https://archive.example:443/item/a:b.pdf";
  assert.deepEqual(plain(api.remarkKeyParts(sourceKey)), {
    prefix: "src",
    ref: "https://archive.example:443/item/a:b.pdf",
  });
  assert.deepEqual(plain(api.remarkKeyParts("unprefixed")), {
    prefix: "",
    ref: "unprefixed",
  });
  assert.equal(api.remarkCategoryForKey(sourceKey), "sources");
  assert.equal(api.remarkCategoryForKey("src2:row|archive|item"), "sources");
  assert.equal(api.remarkCategoryForKey("whl:17"), "catalogs");
});

test("page and publication keys are stable, source-scoped, and reject malformed refs", () => {
  const { api } = remarksHarness();
  const page = api.replicaPageRemarkKey("book|α:1", "scan!*'()|二", 12);
  assert.equal(page,
    "page:book%7C%CE%B1%3A1|scan!*'()%7C%E4%BA%8C|12");
  assert.deepEqual(plain(api.replicaPageRemarkParts(page)), {
    buildId: "book|α:1", sourceId: "scan!*'()|二", page: 12,
    deleted: false,
    encodedBuild: "book%7C%CE%B1%3A1",
    encodedSource: "scan!*'()%7C%E4%BA%8C",
  });
  assert.equal(api.replicaPageRemarkParts("page:book|%E0%A4%A|3"), null);
  assert.equal(api.replicaPageRemarkParts("page:book|primary|01"), null);
  assert.equal(api.replicaPageRemarkParts("page:book|primary|1e0"), null);
  assert.equal(api.replicaPageRemarkParts("page:book|primary"), null);
  assert.equal(api.replicaPageRemarkKey("", "primary", 1), "");
  assert.equal(api.remarkCategoryForKey(page), "pages");
  assert.equal(api.remarkCategoryForKey("page-deleted:book|primary|2|review-1"), "pages");

  assert.equal(api.publicationRemarkKey("book:herbal:one"),
    "pub:book%3Aherbal%3Aone");
  assert.equal(api.publicationRemarkSelection("pub:book%3Aherbal%3Aone"),
    "book:herbal:one");
  assert.equal(api.publicationRemarkKey("group:author"), "");
  assert.equal(api.publicationRemarkSelection("pub:%E0%A4%A"), "");
  assert.equal(api.remarkCategoryForKey("pub:set%3Aherbals"), "publications");
});

test("new source identities distinguish catalog owners while legacy keys still resolve", () => {
  const { api, state } = remarksHarness();
  const first = {
    _rowId: "manual-1", archive: "Internet Archive",
    identifier: "shared-scan", url: "https://archive.example/shared",
    title: "First catalog title",
  };
  const second = { ...first, _rowId: "manual-2", title: "Second catalog title" };
  const firstKeys = plain(api.sourceRemarkKeys(first));
  const secondKeys = plain(api.sourceRemarkKeys(second));

  assert.equal(firstKeys.legacy, secondKeys.legacy);
  assert.notEqual(firstKeys.stable, secondKeys.stable);
  assert.equal(api.sourceMatchesRemark(first, firstKeys.stable), true);
  assert.equal(api.sourceMatchesRemark(first, firstKeys.legacy), true);

  state.attn[firstKeys.legacy] = "Old mark";
  assert.equal(api.activeSourceRemarkKey(first), firstKeys.legacy);
  delete state.attn[firstKeys.legacy];
  assert.equal(api.activeSourceRemarkKey(first), firstKeys.stable);
});

test("keyed marks keep the legacy string format and sync metadata separately", () => {
  const writes = [];
  const state = {
    attn: {},
    settings: { remarksMeta: {} },
  };
  const context = vm.createContext({
    state,
    localStorage: { setItem: (key, value) => writes.push([key, value]) },
    saveSettings: () => writes.push(["settings"]),
    pushClientState: (kind) => writes.push(["push", kind]),
    status: () => {},
    renderRemarks: () => {},
    renderHome: () => {},
  });
  vm.runInContext(`${modelSource}
this.api = { setAttnKey };`, context);

  assert.equal(context.api.setAttnKey("src:https://example.test/scan", "Check pages", {
    label: "Example scan", category: "sources",
  }), true);
  assert.equal(state.attn["src:https://example.test/scan"], "Check pages");
  assert.deepEqual(plain(state.settings.remarksMeta), {
    "src:https://example.test/scan": {
      label: "Example scan", category: "sources",
    },
  });
  assert.ok(writes.some(([kind, value]) => kind === "push" && value === "attention"));

  context.api.setAttnKey("src:https://example.test/scan", "");
  assert.equal("src:https://example.test/scan" in state.attn, false);
  assert.equal("src:https://example.test/scan" in state.settings.remarksMeta, false);
});

test("page deletion remaps marks and metadata for only the exact book and source", () => {
  const seed = remarksHarness();
  const key = (book, source, page) => seed.api.replicaPageRemarkKey(book, source, page);
  const p1 = key("book!*'()", "primary", 1);
  const p2 = key("book!*'()", "primary", 2);
  const p3 = key("book!*'()", "primary", 3);
  const p5 = key("book!*'()", "primary", 5);
  const p6 = key("book!*'()", "primary", 6);
  const secondary = key("book!*'()", "scan|two", 5);
  const otherBook = key("other", "primary", 5);
  const publication = seed.api.publicationRemarkKey("book:public");
  const { api, state, writes } = remarksHarness({
    state: {
      settings: {
        remarksFilters: {},
        remarksMeta: {
          [p1]: { label: "one", category: "pages" },
          [p2]: { label: "deleted", category: "pages" },
          [p3]: { label: "Herbal \u00b7 page 3", category: "pages" },
          [p5]: { label: "Herbal \u00b7 page 5", category: "pages" },
          [p6]: { label: "Herbal \u00b7 page 6", category: "pages" },
          [secondary]: { label: "secondary", category: "pages" },
        },
      },
      attn: {
        [p1]: "keep one", [p2]: "remove two", [p3]: "move three",
        [p5]: "move five", [secondary]: "leave secondary",
        [otherBook]: "leave other", [publication]: "leave publication",
      },
    },
  });

  assert.equal(api.remapPageRemarkKeys("book!*'()", "primary", [4, 2, 2]), true);
  const shifted2 = key("book!*'()", "primary", 2);
  const shifted3 = key("book!*'()", "primary", 3);
  const shifted4 = key("book!*'()", "primary", 4);
  assert.deepEqual(plain(state.attn), {
    [p1]: "keep one",
    [secondary]: "leave secondary",
    [otherBook]: "leave other",
    [publication]: "leave publication",
    [shifted2]: "move three",
    [shifted3]: "move five",
  });
  assert.equal(state.settings.remarksMeta[p2].label, "Herbal \u00b7 page 2");
  assert.equal(state.settings.remarksMeta[shifted3].label, "Herbal \u00b7 page 3");
  assert.equal(state.settings.remarksMeta[shifted4].label, "Herbal \u00b7 page 4");
  assert.equal(state.settings.remarksMeta[secondary].label, "secondary");
  assert.equal(Object.values(state.settings.remarksMeta)
    .some((meta) => meta.label === "deleted"), false);
  assert.ok(writes.some(([kind, value]) => kind === "push" && value === "attention"));
  assert.ok(writes.some(([kind]) => kind === "attention-dirty"));
  assert.equal(api.remapPageRemarkKeys("book!*'()", "primary", []), false);
});

test("page deletion requires both HTTP and JSON success from every OCR save", async () => {
  const failures = [
    { httpOk: false, status: 503, data: { ok: true } },
    { httpOk: true, data: { ok: false, error: "disk is read-only" } },
    { httpOk: true, jsonError: true },
    { requestError: "sidecar unavailable" },
  ];
  for (const failure of failures) {
    const run = pageDeleteHarness({ saveResponses: [failure] });
    await run.api.deleteSelectedPages();
    assert.equal(run.requests.some((request) =>
      request.url === "/api/pdf/pages/delete"), false);
    assert.equal(run.docs[0].text, "unsaved editor text");
    assert.equal(run.ocrState.pageSel.has(2), true);
    assert.match(run.messages["ocr-msg"].textContent,
      /^Pages were not deleted .* OCR edits could not be saved/);
  }

  const laterFailure = pageDeleteHarness({ saveResponses: [
    { httpOk: true, data: { ok: true } },
    { httpOk: true, data: { ok: false, error: "second save failed" } },
  ] });
  await laterFailure.api.deleteSelectedPages();
  assert.equal(laterFailure.requests.filter((request) =>
    request.url.endsWith("/ocr")).length, 2);
  assert.equal(laterFailure.requests.some((request) =>
    request.url === "/api/pdf/pages/delete"), false);
  assert.equal(laterFailure.docs[0].text, "unsaved editor text");
  assert.equal(laterFailure.docs[1].text, "second text");
});

test("page deletion saves OCR first, sends the revision, and names refresh failure", async () => {
  const run = pageDeleteHarness({
    deleteData: {
      ok: true,
      backup: "book.bak.pdf",
      partial: true,
      warnings: ["OCR page layout could not be renumbered"],
      build: { id: "book-a", updated_at: "revision-2" },
    },
    refreshError: new Error("entry list unavailable"),
  });

  await run.api.deleteSelectedPages();
  const save = run.requests.find((request) => request.url.endsWith("/ocr"));
  const deletion = run.requests.find((request) =>
    request.url === "/api/pdf/pages/delete");
  assert.ok(save);
  assert.ok(deletion);
  assert.equal(save.body.text, "unsaved editor text");
  assert.equal(deletion.body.page_revision, "revision-1");
  const saveEvents = run.events.filter((event) => event.endsWith("/ocr"));
  assert.equal(saveEvents.length, 2);
  assert.ok(run.events.lastIndexOf(save.url) < run.events.indexOf("flush-client-state"));
  assert.ok(run.events.indexOf("flush-client-state") < run.events.indexOf(deletion.url));
  const message = run.messages["ocr-msg"].textContent;
  assert.match(message, /^Pages were deleted \(restorable from Info > Trash\)/);
  assert.match(message, /interface refresh failed: entry list unavailable/);
  assert.match(message, /affected references\/artifacts/);
  assert.doesNotMatch(message, /Page deletion failed/);
  assert.ok(run.events.some((event) =>
    event.includes("COMMITTED") && event.includes("REFRESH FAILED")));
});

test("whole-blob attention writes serialize so stale state cannot finish last", async () => {
  const requests = [];
  const removals = [];
  const state = { attn: { first: "old" }, settings: {} };
  const deferred = () => {
    let resolve;
    const promise = new Promise((done) => { resolve = done; });
    return { promise, resolve };
  };
  const context = vm.createContext({
    state,
    checkedArray: () => [],
    partitionSettings: (settings) => ({ prefs: settings }),
    fetch: (_url, opts) => {
      const wait = deferred();
      requests.push({ body: JSON.parse(opts.body), wait });
      return wait.promise;
    },
    localStorage: { removeItem: (key) => removals.push(key) },
    setTimeout: () => 1,
    clearTimeout: () => {},
    ATTN_DIRTY_KEY: "attention-dirty",
  });
  const sync = block(
    "let clientStateReady = false;",
    "// When the same book is checked",
  );
  vm.runInContext(`${sync}\nclientStateReady = true;
this.api = { pushClientState, flushClientState };`, context);

  context.api.pushClientState("attention");
  const first = context.api.flushClientState();
  await Promise.resolve();
  assert.equal(requests.length, 1);
  assert.deepEqual(requests[0].body.attention, { first: "old" });

  state.attn = { first: "new", second: "mark" };
  context.api.pushClientState("attention");
  const joined = context.api.flushClientState();
  assert.equal(joined, first, "a second flush joins the in-flight drain");
  await Promise.resolve();
  assert.equal(requests.length, 1, "the replacement PUT is not concurrent");

  requests[0].wait.resolve({ ok: true, status: 200 });
  for (let i = 0; i < 4 && requests.length < 2; i++) await Promise.resolve();
  assert.equal(requests.length, 2);
  assert.deepEqual(requests[1].body.attention, { first: "new", second: "mark" });
  assert.deepEqual(removals, [], "dirty state remains until the newest PUT wins");

  requests[1].wait.resolve({ ok: true, status: 200 });
  assert.equal(await first, true);
  assert.equal(await joined, true);
  assert.deepEqual(removals, ["attention-dirty"]);
});

test("failed client-state writes keep the dirty bit and honor backoff", async () => {
  const delays = [];
  const removals = [];
  const state = { attn: { first: "keep" }, settings: {} };
  const context = vm.createContext({
    state,
    checkedArray: () => [],
    partitionSettings: (settings) => ({ prefs: settings }),
    fetch: async () => ({ ok: false, status: 503 }),
    localStorage: { removeItem: (key) => removals.push(key) },
    setTimeout: (_fn, delay) => { delays.push(delay); return delays.length; },
    clearTimeout: () => {},
    ATTN_DIRTY_KEY: "attention-dirty",
  });
  const sync = block(
    "let clientStateReady = false;",
    "// When the same book is checked",
  );
  vm.runInContext(`${sync}\nclientStateReady = true;
this.api = { pushClientState, flushClientState };`, context);

  context.api.pushClientState("attention");
  assert.equal(await context.api.flushClientState(), false);
  assert.deepEqual(removals, [], "failure must preserve the durable dirty bit");
  assert.ok(delays.includes(2000), "the first retry uses bounded backoff");
  assert.equal(delays.at(-1), 2000, "settlement must not replace backoff with 0ms");
});

test("each top-level tab has a primary default and retains an independent filter", () => {
  const { api, saves, state } = remarksHarness({
    state: {
      settings: {
        remarksFilters: { checked: "sources", workbench: "not-a-category" },
      },
    },
  });

  assert.deepEqual(plain(api.REMARK_CATEGORIES), [
    ["catalogs", "Catalogs"],
    ["sources", "Sources"],
    ["entries", "Entries"],
    ["pages", "Pages"],
    ["publications", "Publications"],
  ]);
  assert.deepEqual(plain(api.REMARK_TAB_DEFAULTS), {
    home: "all",
    checked: "catalogs",
    workbench: "entries",
    replica: "pages",
    publish: "publications",
    infotab: "all",
  });

  assert.equal(api.remarksFilterForTab("checked"), "sources");
  assert.equal(api.remarksFilterForTab("workbench"), "entries");
  assert.equal(api.remarksFilterForTab("replica"), "pages");
  assert.equal(api.remarksDefaultFilter("future-tab"), "all");

  assert.equal(api.setRemarksFilterForTab("workbench", "pages", true), "pages");
  assert.equal(state.settings.remarksFilters.checked, "sources");
  assert.equal(state.settings.remarksFilters.workbench, "pages");
  assert.equal(saves.length, 1);

  assert.equal(api.setRemarksFilterForTab("replica", "invalid", false), "pages");
  assert.equal(state.settings.remarksFilters.replica, "pages");
  assert.equal(saves.length, 1);
});

test("Home opens the remarks category represented by its attention count", () => {
  const { api } = remarksHarness();
  assert.deepEqual(plain(api.homeAttentionDestination({
    attnCat: 0, attnEntries: 0, attnSources: 3,
  })), { tab: "workbench", filter: "sources" });
  assert.deepEqual(plain(api.homeAttentionDestination({
    attnCat: 0, attnEntries: 2, attnSources: 1,
  })), { tab: "workbench", filter: "all" });
  assert.deepEqual(plain(api.homeAttentionDestination({
    attnCat: 1, attnEntries: 0, attnSources: 0,
  })), { tab: "checked", filter: "catalogs" });
  assert.deepEqual(plain(api.homeAttentionDestination({
    attnCat: 1, attnEntries: 0, attnSources: 1,
  })), { tab: "checked", filter: "all" });
  assert.deepEqual(plain(api.homeAttentionDestination({
    attnPages: 2,
  })), { tab: "replica", filter: "pages" });
  assert.deepEqual(plain(api.homeAttentionDestination({
    attnPublications: 1,
  })), { tab: "publish", filter: "publications" });
  assert.deepEqual(plain(api.homeAttentionDestination({
    attnPages: 1, attnPublications: 1,
  })), { tab: "home", filter: "all" });
  assert.deepEqual(plain(api.homeAttentionDestination({
    attnCat: 1, attnPages: 1,
  })), { tab: "home", filter: "all" });
});

test("remarks aggregate canonical rows, builds, and keyed marks exactly once", () => {
  const sourceKey = "src:https://archive.example/item:a";
  const rows = [
    { id: "checked-1", kind: "checked", attention: "Verify year",
      book: { title: "A Checked Herbal", volume: "2", author: "Ada", year: "1901" } },
    { id: "manual-1", kind: "manual", attention: true,
      book: { title: "Manual Herbal", author: "Bea", year: "1902" } },
    { id: "capture-1", kind: "manual", captured: true, attention: "Retake photo",
      book: { title: "Phone Capture", author: "Cal", year: "1903" } },
    { id: "clean", kind: "checked", attention: "",
      book: { title: "No Mark" } },
  ];
  const sources = [{
    url: "https://archive.example/item:a",
    title: "Archive Scan",
    archive: "Internet Archive",
    author: "Dana",
    year: "1874", volume: "3",
  }];
  const { api } = remarksHarness({
    rows,
    sources,
    state: {
      rowsById: new Map(rows.map((row) => [row.id, row])),
      builds: {
        draft: { id: "draft", status: "draft", attention: "Add rights",
          title: "Draft Entry", authors: "Eli", year: "1880" },
        published: { id: "published", status: "uploaded",
          attention: { reason: "Replace cover" }, title: "Published Entry" },
        clean: { id: "clean-build", status: "ready", attention: "", title: "Clean" },
      },
      attn: {
        [sourceKey]: { value: "1", label: "Stored source", category: "sources" },
        "whl:7": "Check WHL metadata",
        "ch:12": { value: "Confirm edition", label: "Stored master row" },
        "ol:work:key": { value: "Recover result", label: "Historical OL hit" },
      },
      whlRows: [{ idx: 7, title: "WHL Herbal", authors: "Fay", year: "1770" }],
      chBooks: [{ idx: 12, title: "Master Herbal", author: "Gia", year: "1760" }],
      olRows: [],
    },
  });

  const items = plain(api.remarksItems());
  assert.equal(items.length, 9);
  assert.equal(new Set(items.map((item) => item.id)).size, items.length);
  assert.deepEqual(
    Object.fromEntries(["catalogs", "sources", "entries"].map((category) => [
      category, items.filter((item) => item.category === category).length,
    ])),
    { catalogs: 6, sources: 1, entries: 2 },
  );

  assert.equal(items.find((item) => item.id === "row:checked-1").subtype, "Checked book");
  assert.equal(items.find((item) => item.id === "row:checked-1").volume, "2");
  assert.equal(items.find((item) => item.id === "row:manual-1").subtype, "Manual entry");
  assert.equal(items.find((item) => item.id === "row:capture-1").subtype, "Phone capture");
  assert.equal(items.find((item) => item.id === "build:published").subtype, "Published entry");

  const source = items.find((item) => item.id === `key:${sourceKey}`);
  assert.equal(source.label, "Archive Scan");
  assert.equal(source.volume, "3");
  assert.equal(source.reason, "");
  assert.equal(source.canOpen, true);

  const stale = items.find((item) => item.id === "key:ol:work:key");
  assert.equal(stale.label, "Historical OL hit");
  assert.equal(stale.reason, "Recover result");
  assert.equal(stale.canOpen, false);
});

test("remarks resolve page and publication descriptors without alias duplicates", () => {
  const seed = remarksHarness();
  const pageKey = seed.api.replicaPageRemarkKey("build-1", "secondary|scan", 7);
  const pubBook = seed.api.publicationRemarkKey("book:herbal-one");
  const pubSet = seed.api.publicationRemarkKey("set:works");
  const staleSet = seed.api.publicationRemarkKey("set:retired");
  const { api } = remarksHarness({
    state: {
      settings: {
        remarksFilters: {},
        remarksMeta: {
          [pageKey]: { label: "Stored page", category: "pages" },
          [staleSet]: { label: "Retired set", category: "publications" },
        },
      },
      builds: {
        "build-1": { id: "build-1", title: "Replica Herbal", volume: "2",
          pdf_sources: [{ id: "secondary|scan", path: "scan.pdf" }] },
      },
      publishLoaded: true,
      publishEntries: [
        { slug: "herbal-one", title: "Published Herbal", volume: "3",
          authors: "Ada", year: "1890" },
        { slug: "works-1", title: "Collected Works", group_id: "works", volume: "1" },
        { slug: "works-2", title: "Collected Works", group_id: "works", volume: "2" },
      ],
      attn: {
        [pageKey]: "Check transcription",
        [pubBook]: "Fix public description",
        [pubSet]: "Check set order",
        [staleSet]: "Confirm removal",
      },
    },
  });

  const items = plain(api.remarksItems());
  assert.equal(items.length, 4);
  assert.equal(new Set(items.map((item) => item.id)).size, 4);
  const page = items.find((item) => item.id === `key:${pageKey}`);
  assert.deepEqual({
    category: page.category, subtype: page.subtype, label: page.label,
    volume: page.volume, secondary: page.secondary, canOpen: page.canOpen,
  }, {
    category: "pages", subtype: "Replica page", label: "Replica Herbal",
    volume: "2", secondary: "Page 7 · Source secondary|scan", canOpen: true,
  });
  const book = items.find((item) => item.id === `key:${pubBook}`);
  assert.equal(book.category, "publications");
  assert.equal(book.subtype, "Published volume");
  assert.equal(book.volume, "3");
  assert.equal(book.canOpen, true);
  const set = items.find((item) => item.id === `key:${pubSet}`);
  assert.equal(set.subtype, "Published set");
  assert.equal(set.secondary, "2 published volumes");
  const stale = items.find((item) => item.id === `key:${staleSet}`);
  assert.equal(stale.label, "Retired set");
  assert.equal(stale.subtype, "Published set");
  assert.equal(stale.canOpen, false);
});

test("page remarks for removed secondary sources remain visible but are stale", () => {
  const seed = remarksHarness();
  const pageKey = seed.api.replicaPageRemarkKey("build-1", "removed-scan", 3);
  const { api } = remarksHarness({
    state: {
      settings: { remarksFilters: {} },
      builds: {
        "build-1": { id: "build-1", title: "Replica Herbal",
          pdf_sources: [{ id: "live-scan", path: "live.pdf" }] },
      },
      attn: { [pageKey]: "Historical page note" },
    },
  });

  const page = plain(api.remarksItems())[0];
  assert.equal(page.label, "Replica Herbal");
  assert.equal(page.secondary, "Page 3 · Source removed-scan");
  assert.equal(page.canOpen, false);
});

test("open review threads remain in Remarks after their attention mark is gone", () => {
  const rows = [
    { id: "marked", kind: "checked", attention: "Keep attention",
      book: { title: "Marked Herbal" } },
    { id: "clean", kind: "manual", attention: "",
      book: { title: "Clean Herbal" } },
  ];
  const { api } = remarksHarness({
    rows,
    state: {
      builds: {
        draft: { id: "draft", status: "draft", attention: "",
          title: "Draft Herbal", volume: "3", authors: "Ada", year: "1903" },
      },
    },
    reviews: {
      duplicate: {
        id: "duplicate", key: "row:marked", kind: "row", ref: "marked",
        status: "open", label: "Marked Herbal", reason: "Shared copy", comments: [],
      },
      orphan: {
        id: "orphan", key: "row:missing", kind: "row", ref: "missing",
        status: "open", label: "Vol. 4 Orphan Herbal", reason: "Still discuss this",
        comments: [{ author: "Ada", text: "Keep reachable" }],
      },
      build: {
        id: "build", key: "build:draft", kind: "build", ref: "draft",
        status: "open", label: "Draft Herbal", reason: "Check rights", comments: [],
      },
      closed: {
        id: "closed", key: "row:closed", kind: "row", ref: "closed",
        status: "resolved", label: "Closed Herbal", reason: "Done", comments: [],
      },
    },
  });

  const items = plain(api.remarksItems());
  assert.deepEqual(items.map((item) => item.id).sort(),
    ["build:draft", "row:marked", "row:missing"]);
  assert.equal(items.filter((item) => item.id === "row:marked").length, 1,
    "an open review does not duplicate an existing attention item");
  assert.equal(items.find((item) => item.id === "row:marked").hasAttention, true);

  const orphan = items.find((item) => item.id === "row:missing");
  assert.equal(orphan.hasAttention, false);
  assert.equal(orphan.reviewOnly, true);
  assert.equal(orphan.label, "Orphan Herbal");
  assert.equal(orphan.volume, "4");
  assert.equal(orphan.reason, "Still discuss this");
  assert.equal(orphan.canOpen, false);
  assert.equal(api.remarkCommentCount(orphan), 1);

  const build = items.find((item) => item.id === "build:draft");
  assert.equal(build.hasAttention, false);
  assert.equal(build.canOpen, true);
  assert.equal(build.volume, "3");
});

test("remarks prefer the open shared thread and report its comment count", () => {
  const item = { kind: "build", ref: "draft-1" };
  const { api } = remarksHarness({
    reviews: {
      old: {
        id: "old", key: "build:draft-1", status: "resolved",
        created_at: "2026-07-18T10:00:00Z",
        comments: [{ text: "old" }, { text: "thread" }],
      },
      current: {
        id: "current", key: "build:draft-1", status: "open",
        created_at: "2026-07-18T09:00:00Z",
        comments: [{ text: "current reply" }],
      },
      other: {
        id: "other", key: "row:unrelated", status: "open",
        created_at: "2026-07-18T11:00:00Z", comments: [],
      },
    },
  });

  assert.equal(api.remarkReviewForItem(item).id, "current");
  assert.equal(api.remarkCommentCount(item), 1);
  assert.equal(api.remarkReviewForItem({ kind: "row", ref: "missing" }), null);
  assert.equal(api.remarkCommentCount({ kind: "row", ref: "missing" }), 0);
});

test("sidebar Reply posts to the shared review thread and clears only a saved draft", async () => {
  const calls = [];
  const item = { id: "build:draft-1", kind: "build", ref: "draft-1", label: "Draft" };
  const review = { id: "review-1", key: "build:draft-1", status: "open", comments: [] };
  const saved = { ...review, comments: [{ author: "Ada", text: "Please verify" }] };
  const remarksState = {
    replyDraft: "Please verify", replySaving: false, replying: item.id,
    error: "", errorItem: null, expanded: new Set(),
  };
  const reviewsState = { items: {} };
  const context = vm.createContext({
    remarksState, reviewsState,
    remarksItemById: () => item,
    ensureRemarkReview: async () => review,
    fetch: async (url, opts) => {
      calls.push([url, JSON.parse(opts.body)]);
      return { ok: true, json: async () => ({ review: saved }) };
    },
    renderRemarks() {}, renderReviewList() {}, status() {},
  });
  vm.runInContext(`${declaration("submitRemarkReply")}
this.api = { submitRemarkReply };`, context);

  assert.equal(await context.api.submitRemarkReply(item.id), true);
  assert.deepEqual(calls, [["/api/reviews/review-1/comment", { text: "Please verify" }]]);
  assert.equal(remarksState.replyDraft, "");
  assert.equal(remarksState.replying, null);
  assert.equal(remarksState.expanded.has(item.id), true);
  assert.equal(reviewsState.items[review.id].comments.length, 1);
});

test("sidebar Resolve closes the shared review before clearing its attention mark", async () => {
  const calls = [];
  const item = { id: "row:checked-1", kind: "row", ref: "checked-1", label: "Herbal" };
  const review = { id: "review-2", key: "row:checked-1", status: "open", comments: [] };
  const remarksState = {
    resolving: null, error: "", errorItem: null, expanded: new Set([item.id]),
    editing: null, replying: null, replyDraft: "",
  };
  const reviewsState = { items: { [review.id]: review } };
  const context = vm.createContext({
    remarksState, reviewsState,
    remarksItemById: () => item,
    bookTitleText: (book) => book.title,
    confirmDialog: async () => true,
    remarkReviewForItem: () => review,
    fetch: async (url, opts) => {
      calls.push(["fetch", url, JSON.parse(opts.body)]);
      return { ok: true, json: async () => ({ review: { ...review, status: "resolved" } }) };
    },
    applyRemarkValue: async (...args) => { calls.push(["clear", ...args]); return true; },
    renderRemarks() {}, renderReviewList() {}, status() {},
  });
  vm.runInContext(`${declaration("resolveRemarkItem")}
this.api = { resolveRemarkItem };`, context);

  assert.equal(await context.api.resolveRemarkItem(item.id), true);
  assert.deepEqual(plain(calls), [
    ["fetch", "/api/reviews/review-2/resolve", { resolved: true }],
    ["clear", item, ""],
  ]);
  assert.equal(reviewsState.items[review.id].status, "resolved");
  assert.equal(remarksState.expanded.has(item.id), false);
});

test("resolving a review-only remark closes the thread without recreating a mark", async () => {
  const calls = [];
  let confirmCopy = null;
  const item = {
    id: "row:missing", kind: "row", ref: "missing", label: "Orphan Herbal",
    hasAttention: false, reviewOnly: true,
  };
  const review = { id: "review-3", key: "row:missing", status: "open", comments: [] };
  const remarksState = {
    resolving: null, error: "", errorItem: null, expanded: new Set([item.id]),
    editing: null, replying: null, replyDraft: "",
  };
  const reviewsState = { items: { [review.id]: review } };
  const context = vm.createContext({
    remarksState, reviewsState,
    remarksItemById: () => item,
    bookTitleText: (book) => book.title,
    confirmDialog: async (copy) => { confirmCopy = copy; return true; },
    remarkReviewForItem: () => review,
    fetch: async (url, opts) => {
      calls.push(["fetch", url, JSON.parse(opts.body)]);
      return { ok: true, json: async () => ({ review: { ...review, status: "resolved" } }) };
    },
    applyRemarkValue: async (...args) => { calls.push(["unexpected-clear", ...args]); return true; },
    renderRemarks() {}, renderReviewList() {}, status() {},
  });
  vm.runInContext(`${declaration("resolveRemarkItem")}
this.api = { resolveRemarkItem };`, context);

  assert.equal(await context.api.resolveRemarkItem(item.id), true);
  assert.deepEqual(calls, [
    ["fetch", "/api/reviews/review-3/resolve", { resolved: true }],
  ]);
  assert.equal(confirmCopy.detail, "This closes the shared discussion.");
  assert.equal(reviewsState.items[review.id].status, "resolved");
});

test("remark routes cover every supported target without mutating sidebar state", () => {
  const { api, state } = remarksHarness();
  const before = plain(state.settings);

  assert.deepEqual(plain(api.remarkRoute({ kind: "build", ref: "b-1" })),
    { tab: "workbench", phase: "record", buildId: "b-1" });
  assert.deepEqual(plain(api.remarkRoute({
    kind: "key", ref: "src:https://example.test/a:b",
  })), {
    tab: "workbench", phase: "record", sourceKey: "src:https://example.test/a:b",
  });
  assert.deepEqual(plain(api.remarkRoute({
    kind: "key", ref: "src2:row-1|Internet%20Archive|item-1",
  })), {
    tab: "workbench", phase: "record",
    sourceKey: "src2:row-1|Internet%20Archive|item-1",
  });
  assert.deepEqual(plain(api.remarkRoute({ kind: "row", ref: "row-1" })),
    { tab: "checked", table: "checked", rowId: "row-1" });
  assert.deepEqual(plain(api.remarkRoute({ kind: "key", ref: "whl:-1" })),
    { tab: "checked", table: "whl", whlIdx: "-1" });
  assert.deepEqual(plain(api.remarkRoute({ kind: "key", ref: "ch:17" })),
    { tab: "checked", bottom: "ch", recordRef: "17" });
  assert.deepEqual(plain(api.remarkRoute({ kind: "key", ref: "ol:work:key" })),
    { tab: "checked", bottom: "ol", recordRef: "work:key" });
  const pageKey = api.replicaPageRemarkKey("build|1", "scan:two", 14);
  assert.deepEqual(plain(api.remarkRoute({ kind: "key", ref: pageKey })), {
    tab: "replica", replicaBookId: "build|1",
    replicaSource: "scan:two", replicaPage: 14,
  });
  assert.deepEqual(plain(api.remarkRoute({
    kind: "key", ref: api.publicationRemarkKey("set:herbals"),
  })), { tab: "publish", publicationSelection: "set:herbals" });
  assert.equal(api.remarkRoute({
    kind: "key", ref: "page-deleted:build%7C1|scan%3Atwo|14|review-1",
  }), null);
  assert.equal(api.remarkRoute({ kind: "key", ref: "page:bad|primary|01" }), null);
  assert.equal(api.remarkRoute({ kind: "key", ref: "unknown:1" }), null);
  assert.equal(api.remarkRoute(null), null);
  assert.deepEqual(plain(state.settings), before);
});

test("stale Replica sources are rejected before dirty context can change", async () => {
  const calls = [];
  const state = {
    builds: {
      target: { id: "target", pdf_sources: [{ id: "live-scan", path: "live.pdf" }] },
    },
  };
  const rwState = { book: "dirty-book", src: "primary", page: 4, dirty: true };
  const context = vm.createContext({
    state,
    rwState,
    remarkRoute: () => ({
      tab: "replica", replicaBookId: "target",
      replicaSource: "removed-scan", replicaPage: 2,
    }),
    status: (message) => calls.push(["status", message]),
    selectReplicaBook: async (...args) => calls.push(["select-book", ...args]),
  });
  vm.runInContext(`${declaration("replicaPageSourceAvailable")}
${declaration("openRoutedItem")}
this.api = { openRoutedItem };`, context);

  await context.api.openRoutedItem({ kind: "key", ref: "stale-page" });

  assert.deepEqual(calls, [["status", "REPLICA PAGE IS NO LONGER AVAILABLE"]]);
  assert.deepEqual(plain(rwState), {
    book: "dirty-book", src: "primary", page: 4, dirty: true,
  });
});

function applyHarness({ keyResult = true, buildResult = true, rowResult = true,
  buildThrows = false } = {}) {
  const calls = { key: [], build: [], row: [], refresh: [], remarks: 0, home: 0 };
  const state = { rowsById: new Map() };
  const remarksState = { error: "old error" };
  const rows = [{ id: "row-1", attention: "Keep me" }];
  const context = vm.createContext({
    state,
    remarksState,
    combinedRows: () => rows,
    setAttnKey: (...args) => { calls.key.push(plain(args)); return keyResult; },
    refreshRemarkTarget: (item) => calls.refresh.push(plain(item)),
    patchBuildRaw: async (...args) => {
      calls.build.push(plain(args));
      if (buildThrows) throw new Error("offline");
      return buildResult;
    },
    setRowAttention: async (...args) => {
      calls.row.push({ args: plain(args), lookupReady: state.rowsById.has("row-1") });
      return rowResult;
    },
    renderRemarks: () => { calls.remarks += 1; },
    renderHome: () => { calls.home += 1; },
  });
  vm.runInContext(`${declaration("applyRemarkValue")}
this.api = { applyRemarkValue };`, context);
  return { api: context.api, calls, remarksState, state };
}

test("removing marks dispatches by target kind and refreshes only after success", async () => {
  const { api, calls, remarksState } = applyHarness();

  assert.equal(await api.applyRemarkValue({
    kind: "key", ref: "whl:7", label: "WHL row", category: "catalogs",
  }, ""), true);
  assert.deepEqual(calls.key, [["whl:7", "", {
    label: "WHL row", category: "catalogs",
  }]]);
  assert.equal(calls.refresh.length, 1);

  assert.equal(await api.applyRemarkValue({ kind: "build", ref: "build-1" }, ""), true);
  assert.deepEqual(calls.build, [["build-1", { attention: "" }]]);

  assert.equal(await api.applyRemarkValue({ kind: "row", ref: "row-1" }, ""), true);
  assert.deepEqual(calls.row, [{ args: ["row-1", ""], lookupReady: true }]);
  assert.equal(calls.remarks, 3);
  assert.equal(calls.home, 3);
  assert.equal(remarksState.error, "");
});

test("failed mark removal keeps the remark and exposes a retryable error", async () => {
  for (const options of [{ buildResult: false }, { buildThrows: true }]) {
    const { api, calls, remarksState } = applyHarness(options);
    assert.equal(await api.applyRemarkValue({
      kind: "build", ref: "build-1", label: "Draft Entry",
    }, ""), false);
    assert.equal(calls.home, 0);
    assert.equal(calls.remarks, 1);
    assert.equal(remarksState.error,
      "Could not save this remark. The mark was kept.");
  }
});

test("streamed tables can reveal a keyed row beyond the initial chunk", () => {
  const pane = {
    clientHeight: 0, scrollTop: 0, scrollHeight: 0,
    addEventListener() {}, removeEventListener() {},
  };
  const tbody = {
    rows: [],
    closest(selector) { return selector === ".drafting" ? pane : {}; },
    set innerHTML(value) { if (value === "") this.rows = []; },
    appendChild(fragment) { this.rows.push(...fragment.rows); },
  };
  const context = vm.createContext({
    document: { createDocumentFragment: () => ({
      rows: [], appendChild(row) { this.rows.push(row); },
    }) },
    applyColHide() {},
  });
  vm.runInContext(`const STREAM_CHUNK = 200;
${declaration("streamRows")}
this.api = { streamRows };`, context);

  const items = Array.from({ length: 450 }, (_, index) => ({ id: `row:${index}` }));
  context.api.streamRows(tbody, items, (item) => item, (item) => item.id);
  assert.equal(tbody.rows.length, 200);
  assert.equal(tbody._streamReveal("row:350"), true);
  assert.equal(tbody.rows.length, 400);
  assert.equal(tbody._streamReveal("missing"), false);
  assert.equal(tbody.rows.length, 400);
  tbody._streamCleanup();
  assert.equal(tbody._streamReveal, null);
});

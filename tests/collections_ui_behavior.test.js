const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");


const appPath = path.join(
  __dirname, "..", "tools", "whl_explorer", "static", "app.js");
const source = fs.readFileSync(appPath, "utf8");
const html = fs.readFileSync(path.join(
  __dirname, "..", "tools", "whl_explorer", "templates", "index.html"), "utf8");
const css = fs.readFileSync(path.join(
  __dirname, "..", "tools", "whl_explorer", "static", "style.css"), "utf8");

function declaration(name) {
  const asyncMarker = `async function ${name}(`;
  const marker = source.includes(asyncMarker) ? asyncMarker : `function ${name}(`;
  const start = source.indexOf(marker);
  assert.ok(start >= 0, `${name} declaration is present`);
  const end = /^}\r?$/m.exec(source.slice(start));
  assert.ok(end, `${name} declaration has a closing brace`);
  return source.slice(start, start + end.index + end[0].length);
}

function provenanceApi(extra = {}) {
  const context = vm.createContext(extra);
  vm.runInContext([
    declaration("scanExtraText"),
    declaration("scanProvenance"),
    declaration("applyScanProvenance"),
    declaration("collectionFilterKey"),
    declaration("collectionFilterMatch"),
    "this.api = { scanProvenance, applyScanProvenance, collectionFilterKey, collectionFilterMatch };",
  ].join("\n"), context);
  return { context, api: context.api };
}

const plain = (value) => JSON.parse(JSON.stringify(value));

test("display provenance comes only from safe scan_ strings", () => {
  const { api } = provenanceApi();
  const book = {
    collection: "model guessed collection",
    from: "model guessed origin",
    extra: {
      scan_collection_id: "id-a",
      scan_collection: "  Blue crate  ",
      scan_from: " Storage ",
    },
  };

  assert.deepEqual(plain(api.applyScanProvenance(book)), {
    collection: "Blue crate",
    from: "Storage",
    extra: book.extra,
  });
  assert.deepEqual(plain(api.scanProvenance({
    scan_collection_id: { toString: () => "forged" },
    scan_collection: ["forged"],
    scan_from: 17,
  })), { id: "", collection: "", from: "" });
});

test("collection filtering is id-stable and old phones stay name-unlinked", () => {
  const { api } = provenanceApi();
  const oldName = { extra: {
    scan_collection_id: "id-a", scan_collection: "Old crate name",
  } };
  const renamedSnapshot = { extra: {
    scan_collection_id: "id-a", scan_collection: "Newer snapshot name",
  } };
  const sameNameOtherIdentity = { extra: {
    scan_collection_id: "id-b", scan_collection: "Old crate name",
  } };
  const oldPhone = { extra: { scan_collection: "Old crate name" } };

  assert.equal(api.collectionFilterKey(oldName), "id:id-a");
  assert.equal(api.collectionFilterKey(renamedSnapshot), "id:id-a");
  assert.equal(api.collectionFilterKey(sameNameOtherIdentity), "id:id-b");
  assert.equal(api.collectionFilterKey(oldPhone), "name:Old crate name");
  assert.equal(api.collectionFilterMatch(oldName, "id:id-a"), true);
  assert.equal(api.collectionFilterMatch(renamedSnapshot, "id:id-a"), true);
  assert.equal(api.collectionFilterMatch(sameNameOtherIdentity, "id:id-a"), false);
  assert.equal(api.collectionFilterMatch(oldPhone, "id:id-a"), false);
});

test("filter labels use current row names without relabeling snapshots", () => {
  const books = [
    { extra: { scan_collection_id: "id-a", scan_collection: "Captured old name" } },
    { extra: { scan_collection_id: "id-b", scan_collection: "Captured old name" } },
    { extra: { scan_collection: "Legacy <crate>\"" } },
  ];
  const state = { collections: [
    { id: "id-a", name: "Current renamed crate" },
    { id: "id-b", name: "Captured old name" },
  ] };
  const { context } = provenanceApi({ state, combinedRows: () => books.map((book) => ({ book })) });
  vm.runInContext(
    `${declaration("collectionFilterOptions")}\nthis.options = collectionFilterOptions();`,
    context,
  );
  const options = plain(context.options);

  assert.ok(options.some(([value, label]) =>
    value === "id:id-a" && label.startsWith("Current renamed crate")));
  assert.ok(options.some(([value]) => value === "id:id-b"));
  assert.ok(options.some(([value, label]) =>
    value === 'name:Legacy <crate>"' && label.includes("unlinked")));
  assert.equal(books[0].extra.scan_collection, "Captured old name");
});

test("counts and merge repoint identities but preserve snapshot strings and offline cache", () => {
  const rows = [
    { book: { extra: { scan_collection_id: "loser", scan_collection: "Blue", scan_from: "Office" } } },
    { book: { extra: { scan_collection_id: "loser", scan_collection: "Old Blue", scan_from: "Office" } } },
    { book: { extra: { scan_collection_id: "other", scan_collection: "Blue", scan_from: "Office" } } },
    { book: { extra: { scan_collection: "Blue", scan_from: "Office" } } },
  ];
  const state = {
    manual: [{ extra: { scan_collection_id: "loser", scan_collection: "Blue", scan_from: "Office" } }],
    checked: new Map([["row", { book: {
      extra: { scan_collection_id: "loser", scan_collection: "Old Blue", scan_from: "Office" },
    } }]]),
  };
  const writes = [];
  const { context } = provenanceApi({
    state,
    localStorage: { setItem: (key, value) => writes.push([key, value]) },
    LS_KEY: "checked-cache",
    checkedArray: () => [...state.checked.entries()],
  });
  vm.runInContext([
    declaration("collectionUsage"),
    declaration("repointCollectionAliases"),
    declaration("repointCollectionState"),
    "this.usage = collectionUsage(this.rows);",
    "repointCollectionState('loser', 'survivor');",
  ].join("\n"), Object.assign(context, { rows }));

  assert.equal(context.usage.linked.get("loser").count, 2);
  assert.equal(context.usage.linked.get("other").count, 1);
  assert.equal(context.usage.unlinked.size, 1);
  assert.deepEqual(plain(state.manual[0].extra), {
    scan_collection_id: "survivor", scan_collection: "Blue", scan_from: "Office",
  });
  assert.deepEqual(plain(state.checked.get("row").book.extra), {
    scan_collection_id: "survivor", scan_collection: "Old Blue", scan_from: "Office",
  });
  assert.equal(writes.length, 1);
  assert.equal(writes[0][0], "checked-cache");
  assert.match(writes[0][1], /survivor/);
  assert.doesNotMatch(writes[0][1], /"scan_collection_id":"loser"/);
});

test("remote merge aliases heal loaded rows and the active id filter", () => {
  const state = {
    manual: [{ extra: {
      scan_collection_id: "loser", scan_collection: "Frozen name", scan_from: "Office",
    } }],
    checked: new Map([["row", { book: { extra: {
      scan_collection_id: "middle", scan_collection: "Other frozen name", scan_from: "Office",
    } } }]]),
    settings: { collectionFilter: "id:loser" },
  };
  const writes = [], settingsWrites = [];
  const { context } = provenanceApi({
    state,
    localStorage: { setItem: (key, value) => writes.push([key, value]) },
    LS_KEY: "checked-cache",
    checkedArray: () => [...state.checked.entries()],
    saveSettings: () => settingsWrites.push(state.settings.collectionFilter),
  });
  vm.runInContext([
    declaration("repointCollectionAliases"),
    "this.changed = repointCollectionAliases({ loser: 'final', middle: 'final' });",
  ].join("\n"), context);

  assert.equal(context.changed, true);
  assert.equal(state.manual[0].extra.scan_collection_id, "final");
  assert.equal(state.checked.get("row").book.extra.scan_collection_id, "final");
  assert.equal(state.manual[0].extra.scan_collection, "Frozen name");
  assert.equal(state.settings.collectionFilter, "id:final");
  assert.deepEqual(settingsWrites, ["id:final"]);
  assert.equal(writes.length, 1);
  assert.match(writes[0][1], /final/);
});

test("table and generic edit paths keep capture fields read-only and escape dynamic facets", () => {
  assert.match(source,
    /const READ_ONLY_BOOK_FIELDS = new Set\(\["collection", "from"\]\)/);
  assert.match(source, /cmode === "edit" && !snapshotField/);
  assert.match(source,
    /fields\.some\(\(field\) => READ_ONLY_BOOK_FIELDS\.has\(field\)\)/);
  assert.match(source, /READ_ONLY_BOOK_FIELDS\.has\(field\)[\s\S]*capture provenance is read-only/);
  assert.match(source, /value="\$\{esc\(v\)\}"/);
  assert.match(source, /\$\{esc\(label\)\}<\/label>/);
  assert.match(source,
    /data\.aliases && repointCollectionAliases\(data\.aliases\)/);
  assert.match(source, /"acquired", "collection", "from", "categories"/);
  assert.match(source, /\["collection", "Collection"\][\s\S]*\["from", "From"\]/);
});

test("stale collection loads cannot overwrite a newer load or local mutation", async () => {
  const pending = [];
  const state = {
    collections: [], collectionsSignedIn: false, collectionsLoaded: false,
    collectionsLoading: false, collectionsWritable: false, collectionsError: "",
  };
  const overlay = { hidden: true };
  const context = vm.createContext({
    state,
    collectionsLoadSeq: 0,
    collectionsMutationBusy: false,
    collectionEditDrafts: new Map(),
    COLLECTION_DRAFT_GUARD: "guard",
    el: () => overlay,
    renderCollections: () => {},
    renderChecked: () => {},
    repointCollectionAliases: () => false,
    fetch: () => new Promise((resolve) => pending.push(resolve)),
  });
  vm.runInContext([
    declaration("loadCollections"),
    declaration("invalidateCollectionsLoad"),
    "this.loadCollections = loadCollections;",
    "this.invalidateCollectionsLoad = invalidateCollectionsLoad;",
  ].join("\n"), context);

  const older = context.loadCollections();
  const newer = context.loadCollections();
  pending[1]({ ok: true, status: 200, json: async () => ({
    ok: true, signed_in: true, collections: [{ id: "new", name: "New" }], aliases: {},
  }) });
  await newer;
  pending[0]({ ok: true, status: 200, json: async () => ({
    ok: true, signed_in: true, collections: [{ id: "old", name: "Old" }], aliases: {},
  }) });
  await older;
  assert.deepEqual(plain(state.collections), [{ id: "new", name: "New" }]);

  const stale = context.loadCollections();
  context.invalidateCollectionsLoad();
  state.collections = [{ id: "saved", name: "Saved locally" }];
  pending[2]({ ok: true, status: 200, json: async () => ({
    ok: true, signed_in: true, collections: [{ id: "stale", name: "Stale" }], aliases: {},
  }) });
  await stale;
  assert.deepEqual(plain(state.collections), [{ id: "saved", name: "Saved locally" }]);
});

test("collection mutations serialize and an auth rejection makes the manager read-only", async () => {
  let resolveFetch;
  let fetches = 0;
  const state = {
    collections: [], collectionsSignedIn: true, collectionsLoaded: true,
    collectionsLoading: false, collectionsWritable: true, collectionsError: "",
  };
  const context = vm.createContext({
    state,
    collectionsLoadSeq: 0,
    collectionsMutationBusy: false,
    renderCollections: () => {},
    renderChecked: () => {},
    collectionReplaceCurrent: () => {},
    repointCollectionAliases: () => false,
    fetch: () => {
      fetches += 1;
      return new Promise((resolve) => { resolveFetch = resolve; });
    },
  });
  vm.runInContext([
    declaration("collectionsCanMutate"),
    declaration("invalidateCollectionsLoad"),
    declaration("collectionApi"),
    "this.collectionApi = collectionApi;",
  ].join("\n"), context);

  const first = context.collectionApi("POST", "/api/collections", { name: "Blue" });
  const duplicate = context.collectionApi("POST", "/api/collections", { name: "Blue" });
  assert.equal(await duplicate, null);
  assert.equal(fetches, 1);
  resolveFetch({ ok: true, status: 200, json: async () => ({ ok: true, collection: { id: "a" } }) });
  assert.equal((await first).collection.id, "a");

  context.fetch = async () => ({
    ok: false, status: 401, json: async () => ({ ok: false, error: "expired" }),
  });
  assert.equal(await context.collectionApi("PATCH", "/api/collections/a", {}), null);
  assert.equal(state.collectionsSignedIn, false);
  assert.equal(state.collectionsWritable, false);
});

test("merge confirmation distinguishes duplicate identities and states permanence", () => {
  const context = vm.createContext({});
  vm.runInContext([
    declaration("collectionIdShort"),
    declaration("collectionIdentityLabel"),
    declaration("collectionBookCount"),
    declaration("collectionMergePrompt"),
    "this.prompt = collectionMergePrompt(" +
      "{ id: 'aaaaaaaa-1', name: 'Blue', from: 'Office' }, " +
      "{ id: 'bbbbbbbb-2', name: 'Blue', from: 'Store' }, " +
      "{ linked: new Map([['aaaaaaaa-1', { count: 2 }], ['bbbbbbbb-2', { count: 5 }]]) });",
  ].join("\n"), context);
  const prompt = plain(context.prompt);
  assert.match(prompt.message, /aaaaaaaa/);
  assert.match(prompt.message, /bbbbbbbb/);
  assert.match(prompt.message, /survivor/);
  assert.match(prompt.detail, /2 books, From Office/);
  assert.match(prompt.detail, /5 books, From Store/);
  assert.match(prompt.detail, /permanent/);
});

test("collection dialog exposes busy-safe controls, focus handling, and responsive sizing", () => {
  assert.match(html, /aria-describedby="collections-note"/);
  assert.match(html, /id="collections-revert"[\s\S]*Revert edits/);
  assert.match(source, /requestAnimationFrame\(\(\) => el\("collections-close"\)\.focus\(\)\)/);
  assert.match(source, /requestAnimationFrame\(\(\) => restore\.focus\(\)\)/);
  assert.match(source, /aria-label="\$\{esc\(`Save collection \$\{identity\}`\)\}"/);
  assert.match(source, /const controlDisabled = canMutate \? "" : " disabled"/);
  assert.match(css, /#collections-window[\s\S]*max-height:\s*84vh/);
  assert.match(css, /#collections-list[\s\S]*height:\s*clamp\(180px, 48vh, 380px\)/);
  assert.match(css, /#collections-add-form\s*\{[^}]*flex-wrap:\s*wrap/);
});

test("scan collections and the deferred match review queue have explicit desktop controls", () => {
  assert.match(html,
    /id="collections-new-type"[\s\S]*value="capture"[\s\S]*value="scan"/);
  assert.match(html, /id="scan-queue-panel"[\s\S]*Physical scan review queue/);
  assert.match(html, /confidence and OCR, color, and feature evidence/);
  assert.match(source,
    /collectionApi\("POST", "\/api\/collections", \{[\s\S]*collection_type/);
  assert.match(source,
    /fetch\("\/api\/scan\/search-queue"\), fetch\("\/api\/scan\/state"\)/);
  assert.match(source,
    /\/api\/scan-search-queue\/\$\{encodeURIComponent\(queueId\)\}\/\$\{decision\}/);
  assert.match(source, /JSON\.stringify\(\{ capture_id: queueItem\.candidate_capture_id \}\)/);
  assert.match(source,
    /collection && collection\.collection_type === "scan" \? "scan" : "capture"/);
  assert.match(source, /scanQueueReviewItems\(state\.scanQueue\)/);
  assert.match(source, /data-scan-queue-act="approve"/);
  assert.match(source, /data-scan-queue-act="reject"/);
  assert.doesNotMatch(source, /data-scan-match|data-scan-queue-act="complete"/);
  assert.match(css, /#scan-queue-panel[\s\S]*max-height:\s*30vh/);
  assert.match(css, /\.scan-match-evidence[\s\S]*\.scan-queue-actions/);
});

test("scan review groups a capture session and blocks conflicting proposals", () => {
  const context = vm.createContext({
    SCAN_QUEUE_STATUSES: new Set(["pending", "proposed", "matched", "rejected", "failed"]),
  });
  vm.runInContext([
    declaration("scanQueueStatus"),
    declaration("scanQueueReviewItems"),
    "this.group = scanQueueReviewItems;",
  ].join("\n"), context);

  const grouped = plain(context.group([
    { id: "cover", session_id: "session-1", status: "pending" },
    { id: "title", session_id: "session-1", status: "proposed",
      candidate_capture_id: "capture-a", match_confidence: 0.91 },
    { id: "solo", status: "pending", ocr_text: "" },
  ]));
  assert.equal(grouped.length, 2);
  assert.equal(grouped[0].id, "title");
  assert.equal(grouped[0].status, "pending");
  assert.equal(grouped[0].candidate_capture_id, "");
  assert.equal(grouped[0]._stale_proposal, true);
  assert.equal(grouped[0]._review_items.length, 2);
  assert.equal(grouped[1].status, "pending");

  const conflict = plain(context.group([
    { id: "one", session_id: "session-2", status: "proposed",
      candidate_capture_id: "capture-a" },
    { id: "two", session_id: "session-2", status: "proposed",
      candidate_capture_id: "capture-b" },
  ]))[0];
  assert.equal(conflict.status, "failed");
  assert.equal(conflict._proposal_conflict, true);
});

test("scan review presents bounded escaped confidence and visual evidence", () => {
  const escapeHtml = (value) => String(value == null ? "" : value)
    .replace(/[&<>"']/g, (character) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[character]));
  const context = vm.createContext({
    esc: escapeHtml,
    SCAN_QUEUE_STATUSES: new Set(["pending", "proposed", "matched", "rejected", "failed"]),
  });
  vm.runInContext([
    declaration("scanQueueStatus"),
    declaration("scanQueueConfidence"),
    declaration("scanQueueEvidenceLabel"),
    declaration("scanQueueEvidenceValue"),
    declaration("scanQueueEvidenceEntries"),
    declaration("scanQueueEvidenceHtml"),
    "this.confidence = scanQueueConfidence;",
    "this.evidence = scanQueueEvidenceHtml;",
  ].join("\n"), context);

  assert.deepEqual(plain(context.confidence(0.91)), {
    value: 0.91, percent: 91, rating: "high",
  });
  assert.equal(context.confidence(null), null);
  assert.equal(context.confidence(1.1), null);
  const rendered = context.evidence({
    match_confidence: 0.74,
    match_evidence: { components: {
      text: 0.92,
      color: 0.71,
      structure: 0.86,
      gradient: 0.82,
    }, band: "review", reasons: ['<img src=x onerror="bad">'] },
  });
  assert.match(rendered, /Confidence 74% · medium/);
  assert.match(rendered, /OCR\/title similarity<\/dt><dd>92%/);
  assert.match(rendered, /Color similarity<\/dt><dd>71%/);
  assert.match(rendered, /Structure similarity<\/dt><dd>86%/);
  assert.match(rendered, /Edge-feature similarity<\/dt><dd>82%/);
  assert.match(rendered, /Confidence band<\/dt><dd>review/);
  assert.doesNotMatch(rendered, /<img src=/);
  assert.match(rendered, /&lt;img src=x onerror=&quot;bad&quot;&gt;/);
});

test("scan proposal decisions pin the candidate and update the whole session", async () => {
  const calls = [];
  let loads = 0;
  const state = {
    scanQueueBusy: false, scanQueueError: "",
    scanQueue: [
      { id: "queue/one", session_id: "session-1", status: "proposed",
        candidate_capture_id: "capture-a" },
      { id: "queue-two", session_id: "session-1", status: "proposed",
        candidate_capture_id: "capture-a" },
    ],
  };
  const context = vm.createContext({
    state,
    SCAN_QUEUE_STATUSES: new Set(["pending", "proposed", "matched", "rejected", "failed"]),
    renderScanQueue: () => {},
    loadScanQueue: async () => { loads += 1; },
    fetch: async (url, init) => {
      calls.push({ url, init });
      return { ok: true, status: 200, json: async () => ({ ok: true }) };
    },
  });
  vm.runInContext([
    declaration("scanQueueStatus"),
    declaration("decideScanQueueRow"),
    "this.decide = decideScanQueueRow;",
  ].join("\n"), context);

  await context.decide({ dataset: { scanQueueId: "queue/one" } }, "approve");
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "/api/scan-search-queue/queue%2Fone/approve");
  assert.equal(calls[0].init.method, "POST");
  assert.equal(calls[0].init.headers["Content-Type"], "application/json");
  assert.deepEqual(JSON.parse(calls[0].init.body), { capture_id: "capture-a" });
  assert.deepEqual(state.scanQueue.map((item) => item.status), ["matched", "matched"]);
  assert.deepEqual(state.scanQueue.map((item) => item.matched_capture_id),
    ["capture-a", "capture-a"]);
  assert.equal(loads, 1);

  state.scanQueue = state.scanQueue.map((item) => ({
    ...item, status: "proposed", matched_capture_id: null,
  }));
  await context.decide({ dataset: { scanQueueId: "queue-two" } }, "reject");
  assert.equal(calls[1].url, "/api/scan-search-queue/queue-two/reject");
  assert.deepEqual(JSON.parse(calls[1].init.body), { capture_id: "capture-a" });
  assert.deepEqual(state.scanQueue.map((item) => item.status), ["rejected", "rejected"]);
  assert.deepEqual(state.scanQueue.map((item) => item.matched_capture_id), [null, null]);
  assert.equal(loads, 2);

  state.scanQueue[0].status = "pending";
  await context.decide({ dataset: { scanQueueId: "queue/one" } }, "approve");
  assert.equal(calls.length, 2, "pending items cannot be approved before a proposal exists");
});

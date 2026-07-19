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

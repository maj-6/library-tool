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
  const marker = `function ${name}(`;
  const start = source.indexOf(marker);
  assert.ok(start >= 0, `${name} declaration is present`);
  const end = /^}\r?$/m.exec(source.slice(start));
  assert.ok(end, `${name} declaration has a closing brace`);
  return source.slice(start, start + end.index + end[0].length);
}

function copyrightSortHarness() {
  const lookups = [];
  const lookupStatuses = new Map();
  const context = vm.createContext({
    copyrightStatusForSort: (book) => {
      lookups.push({
        title: book.title,
        author: book.author,
        year: book.year,
      });
      return lookupStatuses.get(book.title) || "";
    },
  });
  vm.runInContext([
    declaration("sortRowsBy"),
    declaration("copyrightState"),
    declaration("copyrightSortRank"),
    declaration("checkedSortVal"),
    declaration("whlSortVal"),
    "this.api = { sortRowsBy, copyrightState, copyrightSortRank, checkedSortVal, whlSortVal };",
  ].join("\n"), context);
  return { api: context.api, lookups, lookupStatuses };
}

test("copyright sort follows rights meaning instead of status label spelling", () => {
  const { api } = copyrightSortHarness();
  const rows = [
    { id: "blank", status: "" },
    { id: "copyright", status: "In copyright (renewal R12345)" },
    { id: "unknown", status: "Unknown (renewals database missing)" },
    { id: "public-renewal", status: "Public domain (no renewal found)" },
    { id: "public-age", status: "Public domain (published 1928)" },
  ];

  const sorted = api.sortRowsBy(
    rows, (row) => api.copyrightSortRank(row.status), 1);

  assert.deepEqual(Array.from(sorted, (row) => row.id), [
    "public-age",
    "public-renewal",
    "unknown",
    "copyright",
    "blank",
  ]);
});

test("checked copyright sort values use semantic ranks", () => {
  const { api } = copyrightSortHarness();
  for (const status of [
    "Public domain (published 1928)",
    "Public domain (no renewal found)",
    "Unknown (no year)",
    "In copyright (auto-renewed)",
    "",
  ]) {
    const row = { book: {}, checks: status ? { copyright_status: status } : null };
    assert.equal(
      api.checkedSortVal(row, "copyright"),
      api.copyrightSortRank(status),
    );
  }
});

test("WHL copyright sorting looks up the displayed book identity", () => {
  const { api, lookups, lookupStatuses } = copyrightSortHarness();
  lookupStatuses.set("Fauna and Flora of the Bible", "In copyright");
  const row = {
    title: "Fauna and Flora of the Bible",
    authors: "United Bible Societies",
    year: "1980",
  };

  assert.equal(
    api.whlSortVal(row, "copyright"),
    api.copyrightSortRank("In copyright"),
  );
  assert.deepEqual(lookups, [{
    title: "Fauna and Flora of the Bible",
    author: "United Bible Societies",
    year: "1980",
  }]);
});

function downloadHarness() {
  const state = {
    settings: { autoIaDownload: true },
    downloadedIds: new Set(),
    downloads: new Map(),
    autoDlActive: new Set(),
    autoDlQueue: [],
  };
  const starts = [];
  const context = vm.createContext({
    state,
    combinedRows: () => [],
    getVerify: (row) => row.verdict || "pending",
    getManualUrl: (row) => row.manualUrl || "",
    iaIdentifierForRow: (row) => row.ident || "",
    startDownload: (ident, book) => { starts.push({ ident, book }); },
    status: () => {},
    updateDlProgress: () => {},
  });
  vm.runInContext(
    block("function iaDownloadCandidate", "// footer progress bar:") + `
      this.api = { iaDownloadCandidate, enqueueAutoDl,
        maybeAutoDownloadVerifiedIa, pumpAutoDl };`,
    context,
  );
  return { api: context.api, starts, state };
}

test("verified IA eligibility is shared and rejects ineligible matches", () => {
  const { api } = downloadHarness();
  const approved = {
    verdict: "approved", ident: "good", book: { title: "Good" },
    scans: { internet_archive: { available: true } },
  };
  const candidate = api.iaDownloadCandidate(approved);
  assert.equal(candidate.ident, "good");
  assert.equal(candidate.book.title, "Good");

  assert.equal(api.iaDownloadCandidate({
    ...approved, scans: { internet_archive: { available: false } },
  }), null);
  assert.equal(api.iaDownloadCandidate({ ...approved, verdict: "pending" }), null);

  const manual = api.iaDownloadCandidate({
    verdict: "rejected", manualUrl: "https://archive.org/details/manual-copy",
    ident: "manual-copy", book: {},
  });
  assert.equal(manual.ident, "manual-copy");
});

test("automatic verified downloads retry errors and deduplicate active work", () => {
  const { api, starts, state } = downloadHarness();
  const row = {
    ident: "retry-me", book: { title: "Retry" },
    scans: { internet_archive: { available: true } },
  };
  state.downloads.set("retry-me", { status: "error", error: "temporary" });

  assert.equal(api.maybeAutoDownloadVerifiedIa(row, "approved"), true);
  assert.deepEqual(starts.map((item) => item.ident), ["retry-me"]);
  assert.equal(api.maybeAutoDownloadVerifiedIa(row, "approved"), false);
  assert.equal(starts.length, 1);

  state.settings.autoIaDownload = false;
  assert.equal(api.maybeAutoDownloadVerifiedIa({
    ...row, ident: "disabled",
  }, "approved"), false);
  assert.equal(starts.length, 1);
});

test("automatic verified downloads skip saved, downloading, and done items", () => {
  for (const status of ["downloading", "done"]) {
    const { api, starts, state } = downloadHarness();
    state.downloads.set("known", { status });
    assert.equal(api.maybeAutoDownloadVerifiedIa({
      ident: "known", book: {},
      scans: { internet_archive: { available: true } },
    }, "approved"), false);
    assert.equal(starts.length, 0);
  }

  const { api, starts, state } = downloadHarness();
  state.downloadedIds.add("saved");
  assert.equal(api.maybeAutoDownloadVerifiedIa({
    ident: "saved", book: {},
    scans: { internet_archive: { available: true } },
  }, "approved"), false);
  assert.equal(starts.length, 0);
});

function verificationHarness({ kind = "checked", response, networkError } = {}) {
  const row = {
    id: "row-1", kind, book: { title: "A Book" },
    scans: { internet_archive: { available: true } },
  };
  const entry = { id: row.id, book: row.book, verify: {} };
  const state = {
    rowsById: new Map([[row.id, row]]),
    manual: kind === "manual" ? [{ id: row.id, title: "A Book" }] : [],
    checked: new Map(kind === "manual" ? [] : [[row.id, entry]]),
  };
  const auto = [], errors = [], statuses = [];
  const context = vm.createContext({
    state,
    fetch: async () => {
      if (networkError) throw new Error("offline");
      return response || { ok: true, json: async () => ({ ok: true, entry }) };
    },
    getVerify: () => "pending",
    getManualUrl: () => "",
    activeHistoryTab: () => "checked",
    migrateVerify: (value) => value.verify || {},
    pushOp: () => {},
    setManualUrl: async () => {},
    trackChecked: (_label, _id, mutate) => mutate(),
    saveChecked: () => {},
    renderChecked: () => {},
    status: (message) => statuses.push(message),
    statusErr: (message) => errors.push(message),
    maybeAutoDownloadVerifiedIa: (...args) => { auto.push(args); return true; },
  });
  vm.runInContext(
    block("async function setVerify", "function cycleVerify") +
      "\nthis.api = { setVerify };",
    context,
  );
  return { api: context.api, auto, entry, errors, state, statuses };
}

test("a successfully persisted IA approval invokes automatic download", async () => {
  const checked = verificationHarness();
  assert.equal(await checked.api.setVerify(
    "row-1", "internet_archive", "approved", false), true);
  assert.equal(checked.entry.verify.internet_archive, "approved");
  assert.equal(checked.auto.length, 1);
  assert.equal(checked.auto[0][1], "approved");

  const savedEntry = {
    id: "row-1", title: "A Book", verify: { internet_archive: "approved" },
  };
  const manual = verificationHarness({
    kind: "manual",
    response: { ok: true, json: async () => ({ ok: true, entry: savedEntry }) },
  });
  assert.equal(await manual.api.setVerify(
    "row-1", "internet_archive", "approved", false), true);
  assert.equal(manual.state.manual[0], savedEntry);
  assert.equal(manual.auto.length, 1);
});

test("failed manual verification persistence never starts a download", async () => {
  const rejected = verificationHarness({
    kind: "manual",
    response: { ok: false, json: async () => ({ ok: false }) },
  });
  assert.equal(await rejected.api.setVerify(
    "row-1", "internet_archive", "approved", false), false);
  assert.equal(rejected.auto.length, 0);
  assert.equal(rejected.errors.length, 1);
  assert.equal(rejected.statuses.length, 0);

  const offline = verificationHarness({ kind: "manual", networkError: true });
  assert.equal(await offline.api.setVerify(
    "row-1", "internet_archive", "approved", false), false);
  assert.equal(offline.auto.length, 0);
  assert.equal(offline.errors.length, 1);
});

function documentHarness() {
  const docs = [
    { id: "compiled", buildId: "book", name: "compiled.txt", text: "" },
    { id: "extracted", buildId: "book", name: "extracted.txt", text: "page" },
    { id: "other", buildId: "other-book", name: "compiled.txt", text: "other" },
  ];
  const ocrState = { docs, sel: "compiled", view: "pdf", lastRendered: "compiled" };
  const calls = { facsimile: 0, full: 0, list: 0, refill: [], sync: 0 };
  const context = vm.createContext({
    ocrState,
    ocrSyncEditor: () => { calls.sync += 1; },
    ocrSelDoc: () => docs.find((doc) => doc.id === ocrState.sel) || null,
    el: () => ({ querySelector: () => ({}) }),
    refillOcrPageText: (doc) => { calls.refill.push(doc.id); return true; },
    renderOcrDocs: () => { calls.list += 1; },
    renderAnFacsimile: () => { calls.facsimile += 1; },
    renderOcrTab: () => { calls.full += 1; },
  });
  vm.runInContext(
    block("function selectOcrDocument", "function initOcrTab") +
      "\nthis.api = { selectOcrDocument };",
    context,
  );
  return { api: context.api, calls, ocrState };
}

test("compiled and extracted fast-path switches both refresh the facsimile", () => {
  const { api, calls, ocrState } = documentHarness();
  api.selectOcrDocument("extracted");
  api.selectOcrDocument("compiled"); // empty/image-only content takes the same safe path

  assert.deepEqual(calls.refill, ["extracted", "compiled"]);
  assert.equal(calls.facsimile, 2);
  assert.equal(calls.list, 2);
  assert.equal(calls.full, 0);
  assert.equal(calls.sync, 2);
  assert.equal(ocrState.lastRendered, "compiled");
});

test("a cross-book document switch retains the full-render fallback", () => {
  const { api, calls, ocrState } = documentHarness();
  api.selectOcrDocument("other");

  assert.equal(ocrState.sel, "other");
  assert.equal(calls.full, 1);
  assert.equal(calls.facsimile, 0);
  assert.deepEqual(calls.refill, []);
});

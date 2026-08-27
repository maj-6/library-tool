const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const root = path.join(__dirname, "..");
const app = fs.readFileSync(path.join(
  root, "tools", "whl_explorer", "static", "app.js"), "utf8");
const template = fs.readFileSync(path.join(
  root, "tools", "whl_explorer", "templates", "index.html"), "utf8");

function declaration(name) {
  const asyncMarker = `async function ${name}(`;
  const plainMarker = `function ${name}(`;
  const asyncStart = app.indexOf(asyncMarker);
  const start = asyncStart >= 0 ? asyncStart : app.indexOf(plainMarker);
  assert.ok(start >= 0, `${name} declaration is present`);
  const end = /^}\r?$/m.exec(app.slice(start));
  assert.ok(end, `${name} declaration has a closing brace`);
  return app.slice(start, start + end.index + end[0].length);
}

function json(value) {
  return JSON.parse(JSON.stringify(value));
}

function samplePlan(overrides = {}) {
  const zeroes = { create: 0, update: 0, delete: 0, unchanged: 0, conflict: 0 };
  return {
    schema: "librarytool.portable-book-import-plan/1",
    committable: true,
    counts: {
      metadata: { ...zeroes, update: 1 },
      assessment: { ...zeroes, unchanged: 1 },
    },
    actions: [{
      source: { namespace: "manual_entries", source_id: "manual-1" },
      metadata: "update",
      assessment: "unchanged",
      conflicts: [],
    }],
    ...overrides,
  };
}

test("File menu preserves JSON export and exposes explicit portable ZIP actions", () => {
  assert.match(template, /data-cmd="export"[^>]*>Export table \(JSON\)/);
  assert.match(template, /data-cmd="portable-bundle-export"/);
  assert.match(template, /data-cmd="portable-bundle-import"/);
  assert.match(template,
    /id="portable-bundle-file"[^>]*type="file"[^>]*accept="\.zip,application\/zip"[^>]*hidden/);
  for (const part of ["metadata", "assessment"]) {
    for (const outcome of ["create", "update", "delete", "unchanged", "conflict"])
      assert.match(template, new RegExp(`id="portable-count-${part}-${outcome}"`));
  }
  assert.match(template, /id="portable-conflict-list"/);
  assert.match(template, /id="portable-import-commit"[^>]*disabled/);
});

test("portable export identities are exact, ordered, and fail closed", () => {
  const context = vm.createContext({});
  vm.runInContext([
    declaration("portableBundleSourceForRow"),
    declaration("portableBundleSources"),
    "this.api = { portableBundleSourceForRow, portableBundleSources };",
  ].join("\n"), context);

  assert.deepEqual(json(context.api.portableBundleSources([
    { kind: "manual", id: "capture-a" },
    { kind: "catalog", source: "ch_library", id: "ch_library:17" },
  ])), [
    { namespace: "manual_entries", source_id: "capture-a" },
    { namespace: "ch_library", source_id: "17" },
  ]);
  assert.throws(() => context.api.portableBundleSources([
    { kind: "catalog", source: "whl", id: "whl:17" },
  ]), /no portable source identity/);
  assert.throws(() => context.api.portableBundleSources([
    { kind: "manual", id: "same" }, { kind: "manual", id: "same" },
  ]), /occurs twice/);
  assert.equal(context.api.portableBundleSourceForRow({
    kind: "catalog", source: "ch_library", id: "ch_library:01",
  }), null);
});

test("portable export enumerates only filtered rows and refuses an empty view", async () => {
  const calls = [], downloads = [], messages = [];
  let visible = [
    { kind: "manual", id: "only-visible" },
    { kind: "catalog", source: "ch_library", id: "ch_library:4" },
  ];
  const context = vm.createContext({
    PORTABLE_BUNDLE_EXPORT_URL: "/api/v1/portable-book-bundles/export",
    state: { settings: { topTable: "checked" } },
    filteredCheckedRows: () => visible,
    fetch: async (...args) => {
      calls.push(args);
      return {
        ok: true,
        headers: { get: () => 'attachment; filename="backup.zip"' },
        blob: async () => ({ size: 12 }),
      };
    },
    downloadPortableBundle: (...args) => downloads.push(args),
    portableResponseError: async () => "failed",
    status: (message) => messages.push(message),
    statusErr: (message) => messages.push(message),
  });
  vm.runInContext([
    declaration("portableBundleSourceForRow"),
    declaration("portableBundleSources"),
    declaration("portableBundleFilename"),
    declaration("exportPortableBookBundle"),
    "this.run = exportPortableBookBundle;",
  ].join("\n"), context);

  assert.equal(await context.run(), true);
  assert.equal(calls.length, 1);
  assert.equal(calls[0][0], "/api/v1/portable-book-bundles/export");
  assert.equal(calls[0][1].method, "POST");
  assert.equal(calls[0][1].headers["Content-Type"], "application/json");
  assert.deepEqual(JSON.parse(calls[0][1].body), { sources: [
    { namespace: "manual_entries", source_id: "only-visible" },
    { namespace: "ch_library", source_id: "4" },
  ] });
  assert.equal(downloads.length, 1);
  assert.equal(downloads[0][1], "backup.zip");

  visible = [];
  assert.equal(await context.run(), false);
  assert.equal(calls.length, 1, "empty filtered views never reach the endpoint");
  assert.match(messages.at(-1), /NOTHING TO BACK UP/);
});

test("dry-run plan validation exposes nested counts and per-source conflicts", () => {
  const context = vm.createContext({});
  vm.runInContext(`
    const PORTABLE_PLAN_PARTS = ["metadata", "assessment"];
    const PORTABLE_PLAN_OUTCOMES = ["create", "update", "delete", "unchanged", "conflict"];
    ${declaration("portableImportPlanValid")}
    ${declaration("portableImportConflictItems")}
    this.api = { portableImportPlanValid, portableImportConflictItems };
  `, context);
  const conflict = samplePlan({
    committable: false,
    counts: {
      metadata: { create: 0, update: 0, delete: 0, unchanged: 0, conflict: 1 },
      assessment: { create: 0, update: 0, delete: 0, unchanged: 0, conflict: 1 },
    },
    actions: [{
      source: { namespace: "ch_library", source_id: "9" },
      metadata: "conflict", assessment: "conflict",
      conflicts: ["source hash changed"],
    }],
  });
  assert.equal(context.api.portableImportPlanValid(conflict), true);
  assert.deepEqual(json(context.api.portableImportConflictItems(conflict)), [{
    source_ref: "ch_library:9", reasons: ["source hash changed"],
  }]);
  conflict.counts.metadata.create = -1;
  assert.equal(context.api.portableImportPlanValid(conflict), false);
});

test("ZIP import always posts raw bytes to planning before any commit", async () => {
  const calls = [], shown = [];
  const file = { name: "complete-backup.zip", marker: "raw-file-body" };
  const plan = samplePlan();
  const context = vm.createContext({
    PORTABLE_BUNDLE_PLAN_URL: "/api/v1/portable-book-bundles/import-plans",
    fetch: async (...args) => {
      calls.push(args);
      return { ok: true, json: async () => ({ ok: true, plan_id: "plan-1", plan }) };
    },
    portableImportPlanValid: () => true,
    showPortableImportPlan: (...args) => shown.push(args),
    status: () => {},
    statusErr: () => {},
  });
  vm.runInContext(`${declaration("planPortableBookBundle")}
    this.run = planPortableBookBundle;`, context);

  assert.equal(await context.run(file), true);
  assert.equal(calls.length, 1);
  assert.equal(calls[0][0], "/api/v1/portable-book-bundles/import-plans");
  assert.equal(calls[0][1].method, "POST");
  assert.equal(calls[0][1].headers["Content-Type"], "application/zip");
  assert.equal(calls[0][1].body, file);
  assert.equal(shown.length, 1);
  assert.equal(shown[0][0], "plan-1");
  assert.equal(shown[0][1], plan);
});

test("commit requires human confirmation and sends the pinned idempotent request", async () => {
  const calls = [], busy = [];
  const plan = samplePlan();
  let approved = false;
  const portableImportState = {
    planId: "plan A/1", plan, idempotencyKey: "portable-operation-1", busy: false,
  };
  const context = vm.createContext({
    PORTABLE_BUNDLE_PLAN_URL: "/api/v1/portable-book-bundles/import-plans",
    PORTABLE_BUNDLE_COMMIT_CONFIRMATION: "COMMIT-PORTABLE-BOOK-BUNDLE",
    portableImportState,
    confirmDialog: async () => approved,
    setPortableImportBusy: (...args) => busy.push(args),
    closePortableImportPlan: () => true,
    refreshPortableBookState: async () => {},
    status: () => {},
    statusErr: () => {},
    statusCrit: () => {},
    fetch: async (...args) => {
      calls.push(args);
      return { ok: true, json: async () => ({ ok: true, receipt: {} }) };
    },
  });
  vm.runInContext(`${declaration("commitPortableBookBundle")}
    this.run = commitPortableBookBundle;`, context);

  assert.equal(await context.run(), false);
  assert.equal(calls.length, 0, "cancelled confirmation performs no write request");

  approved = true;
  assert.equal(await context.run(), true);
  assert.equal(calls.length, 1);
  assert.equal(calls[0][0],
    "/api/v1/portable-book-bundles/import-plans/plan%20A%2F1/commit");
  assert.equal(calls[0][1].method, "POST");
  assert.equal(calls[0][1].headers["Idempotency-Key"], "portable-operation-1");
  assert.deepEqual(JSON.parse(calls[0][1].body), {
    confirmation: "COMMIT-PORTABLE-BOOK-BUNDLE",
  });
  assert.equal(busy[0][0], true);
});

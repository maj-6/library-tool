const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const root = path.join(__dirname, "..");
const source = fs.readFileSync(path.join(
  root, "tools", "whl_explorer", "static", "app.js"), "utf8");
const template = fs.readFileSync(path.join(
  root, "tools", "whl_explorer", "templates", "index.html"), "utf8");
const styles = fs.readFileSync(path.join(
  root, "tools", "whl_explorer", "static", "style.css"), "utf8");

function declaration(name) {
  const markers = [`async function ${name}(`, `function ${name}(`];
  let start = -1;
  for (const marker of markers) {
    start = source.indexOf(marker);
    if (start >= 0) break;
  }
  assert.ok(start >= 0, `${name} declaration is present`);
  const open = source.indexOf("{", start);
  let depth = 0;
  for (let i = open; i < source.length; i += 1) {
    if (source[i] === "{") depth += 1;
    else if (source[i] === "}") {
      depth -= 1;
      if (depth === 0) return source.slice(start, i + 1);
    }
  }
  assert.fail(`${name} declaration has a closing brace`);
}

function pureApi() {
  const context = vm.createContext({});
  vm.runInContext([
    declaration("cloudSyncNumber"),
    declaration("cloudSyncRunKey"),
    declaration("cloudSyncFailureHeadline"),
    declaration("cloudSyncProgressValues"),
    declaration("cloudSyncMeterModel"),
    declaration("cloudSyncItems"),
    declaration("cloudSyncCurrent"),
    declaration("cloudSyncItemKey"),
    declaration("cloudSyncOutcomeLabel"),
    declaration("cloudSyncItemDetail"),
    declaration("cloudSyncTerminalModel"),
    "this.api = { cloudSyncFailureHeadline, cloudSyncProgressValues,",
    " cloudSyncMeterModel, cloudSyncItems,",
    " cloudSyncCurrent, cloudSyncItemKey, cloudSyncOutcomeLabel,",
    " cloudSyncItemDetail, cloudSyncTerminalModel };",
  ].join("\n"), context);
  return context.api;
}

function plain(value) {
  return JSON.parse(JSON.stringify(value));
}

test("cloud sync panel exposes accessible live progress and durable details", () => {
  for (const id of [
    "cloud-sync-progress", "cloud-sync-stage", "cloud-sync-meter",
    "cloud-sync-counts", "cloud-sync-current", "cloud-sync-details",
    "cloud-sync-recent", "cloud-sync-terminal", "cloud-sync-terminal-facts",
    "cloud-sync-store-list", "cloud-sync-error-list",
  ]) {
    assert.match(template, new RegExp(`id=["']${id}["']`), `${id} exists`);
  }
  assert.match(template, /id="cloud-sync-stage"[^>]*role="status"/);
  assert.match(template, /id="cloud-sync-stage"[^>]*aria-live="polite"/);
  assert.match(template, /id="cloud-sync-progress"[^>]*aria-busy="false"/);
  assert.match(template,
    /id="cloud-sync-meter"[^>]*aria-labelledby="cloud-sync-title"/);
  assert.match(styles, /\.cloud-sync-progress\[data-state="error"\]/);
  assert.match(styles, /\.cloud-sync-recent\s*\{[^}]*max-height/s);
  assert.match(styles, /\.cloud-sync-terminal-facts\s*\{/);
});

test("failure headline stays concise while detailed errors remain available", () => {
  const api = pureApi();
  const first = `capture 12ab: ${"x".repeat(220)}`;
  const headline = api.cloudSyncFailureHeadline({
    last_result: { ok: false, errors: [first, "another detailed problem"] },
    last_error: `${first}; another detailed problem`,
  });
  assert.equal(headline.length, 160);
  assert.match(headline, /^capture 12ab:/);
  assert.ok(headline.endsWith("…"));
});

test("canonical backend events and flattened current progress normalize for UI", () => {
  const api = pureApi();
  const sync = {
    run_id: "run-7",
    progress: {
      completed: 2, total: 4, imported: 1, skipped: 0, failed: 1,
      current_capture: "capture-3", current_book: "A New Herbal",
      current_index: 3, photo_count: 5,
    },
    events: [{
      seq: 18, kind: "capture", status: "imported",
      capture_id: "capture-2", book_id: "manual-22", title: "Flora",
      message: "Book imported", details: ["OCR warning"],
      at: "2026-07-22T12:00:00Z",
    }],
  };
  assert.deepEqual(plain(api.cloudSyncProgressValues(sync)), {
    completed: 2, total: 4, imported: 1, skipped: 0, failed: 1,
  });
  assert.deepEqual(plain(api.cloudSyncCurrent(sync)), {
    index: 3, total: 4, capture_id: "capture-3",
    title: "A New Herbal", photo_count: 5,
  });
  const item = plain(api.cloudSyncItems(sync)[0]);
  assert.equal(item.sequence, 18);
  assert.equal(item.entry_id, "manual-22");
  assert.equal(item.outcome, "imported");
  assert.equal(api.cloudSyncItemKey(sync, item), "run-7:18");
  assert.equal(api.cloudSyncOutcomeLabel("skipped"), "Already present");
  assert.equal(api.cloudSyncItemDetail(item), "Book imported · OCR warning");
});

test("capture import is determinate while later owner phases keep working indeterminately", () => {
  const api = pureApi();
  const progress = {
    completed: 2, total: 4, imported: 2, skipped: 0, failed: 0,
  };
  const importing = plain(api.cloudSyncMeterModel({
    running: true, stage: "capture_import",
    progress: { ...progress, phase: "capture_import" },
  }));
  assert.equal(importing.indeterminate, false);
  assert.equal(importing.max, 4);
  assert.equal(importing.value, 2);
  assert.match(importing.label, /^2 of 4 captures/);

  const owner = plain(api.cloudSyncMeterModel({
    running: true, stage: "owner_stores",
    progress: {
      ...progress, completed: 0, total: 0, phase: "owner_stores",
      indeterminate: true, capture_completed: 4, capture_total: 4,
    },
  }));
  assert.equal(owner.indeterminate, true);
  assert.match(owner.label, /^4 of 4 captures processed/);

  const terminal = plain(api.cloudSyncMeterModel({
    running: false, stage: "complete",
    progress: { ...progress, completed: 4, phase: "complete" },
  }));
  assert.equal(terminal.indeterminate, false);
  assert.equal(terminal.value, 4);
});

test("terminal details retain owner transfers, review results, and separate errors", () => {
  const model = plain(pureApi().cloudSyncTerminalModel({
    ok: false,
    owner_sync: true,
    books_pushed: 7,
    capture_metadata_pushed: 3,
    stores: {
      builds: {
        pushed: 2, tombstoned: 1, pulled: 4, deleted: 1, in_sync: 9,
      },
      taxonomy: {
        pushed: 0, tombstoned: 0, pulled: 0, deleted: 0, in_sync: 5,
        guard: "remote store unexpectedly empty",
      },
    },
    entries: { pushed: 8, pulled: 6, in_sync: 10 },
    capture_reviews: {
      read: 4, merged: 2, pushed: 1, conflicts: 1,
      errors: ["capture abc review: conflict"],
    },
    errors: ["builds: transport failed"],
  }));
  assert.deepEqual(model.facts.map((fact) => fact.label), [
    "Books pushed", "Phone metadata", "Owner stores",
    "Entry files", "Capture reviews",
  ]);
  assert.equal(model.facts[0].value, "7");
  assert.match(model.facts[2].value, /^3 up · 5 down · 14 unchanged$/);
  assert.match(model.facts[3].value, /^8 up · 6 down/);
  assert.match(model.facts[4].value, /2 merged · 1 pushed · 1 conflicts/);
  assert.equal(model.stores[0].tombstoned, 1);
  assert.ok(model.errors.includes("builds: transport failed"));
  assert.ok(model.errors.includes("taxonomy: remote store unexpectedly empty"));
  assert.ok(model.errors.includes("capture abc review: conflict"));
});

test("each imported event fetches and merges only its durable manual entry", () => {
  const load = declaration("loadManual");
  const merge = declaration("mergeCloudManualEntry");
  const reveal = declaration("revealCloudSyncItems");
  assert.ok(load.includes("/api/manual/${encodeURIComponent"));
  assert.ok(load.includes("reveal.entryId"));
  assert.ok(merge.includes("state.manual.unshift(entry)"));
  assert.ok(merge.includes("scheduleRenderChecked()"));
  assert.ok(reveal.includes("await loadManual({"));
  assert.ok(reveal.includes("await cloudSyncNextPaint()"));
  assert.ok(reveal.indexOf("await loadManual({") <
    reveal.indexOf("cloudSyncUi.seenItems.add(key)"));
  assert.ok(declaration("finalizeCloudSync").includes("await loadManual()"));
});

test("one recursive poll observes automatic, resumed, and manually started runs", () => {
  const poll = declaration("pollCloudSyncStatus");
  const init = declaration("initCloudSync");
  const run = declaration("runCloudSync");
  assert.ok(poll.includes('fetch("/api/cloudsync/status")'));
  assert.ok(poll.includes("commandGeneration !== cloudSyncUi.commandGeneration"));
  assert.ok(poll.includes("scheduleCloudSyncPoll(running ? 750 : 5000)"));
  assert.ok(!poll.includes("setInterval"));
  assert.ok(init.includes("visibilitychange"));
  assert.ok(init.includes('visibilityState === "visible"'));
  assert.ok(run.includes("cloudSyncUi.commandGeneration += 1"));
  assert.ok(run.includes("cloudSyncUi.commandPending = true"));
  assert.ok(run.includes("cloudSyncUi.pendingRunId = String(result.run_id"));
  assert.ok(run.includes("scheduleCloudSyncPoll(0)"));
  const apply = declaration("applyCloudSyncStatus");
  assert.ok(apply.includes("if (cloudSyncUi.commandPending)"));
  assert.ok(apply.includes("statusRunId !== cloudSyncUi.pendingRunId"));
});

test("manual start ignores idle status until the claimed run identity appears", async () => {
  const rendered = [];
  const button = {
    attributes: {},
    setAttribute(name, value) { this.attributes[name] = value; },
  };
  const cloudSyncUi = {
    commandPending: true,
    pendingRunId: "",
    runId: "old-run",
    seenItems: new Set(),
    revision: 4,
    terminalKey: "",
    lastStatus: null,
  };
  const context = vm.createContext({
    cloudSyncUi,
    cloudSyncRunKey: (sync) => String(sync.run_id || sync.last_run || ""),
    renderCloudSyncStatus: (sync) => rendered.push(sync.run_id),
    revealCloudSyncItems: async () => {},
    finalizeCloudSync: async () => {},
    el: () => button,
  });
  vm.runInContext(
    `${declaration("applyCloudSyncStatus")}\nthis.apply = applyCloudSyncStatus;`,
    context,
  );

  assert.equal(await context.apply({
    running: false, run_id: "old-run", revision: 4,
  }), false);
  assert.equal(cloudSyncUi.commandPending, true);
  assert.deepEqual(rendered, []);
  assert.equal(button.disabled, undefined);

  cloudSyncUi.pendingRunId = "new-run";
  assert.equal(await context.apply({
    running: true, run_id: "new-run", revision: 5,
  }), true);
  assert.equal(cloudSyncUi.commandPending, false);
  assert.equal(cloudSyncUi.pendingRunId, "");
  assert.deepEqual(rendered, ["new-run"]);
  assert.equal(button.disabled, true);
});

test("the unified jobs drawer reports capture counts and the current book", () => {
  const status = declaration("jobStatusText");
  assert.ok(status.includes('r.kind === "cloudsync"'));
  assert.ok(status.includes("progress.current_book"));
  assert.ok(status.includes("${imported} imported"));
  assert.ok(status.includes("${skipped} skipped"));
  assert.ok(status.includes("${failed} failed"));
});

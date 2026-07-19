const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(
  __dirname, "..", "tools", "whl_explorer", "static", "app.js"), "utf8");

function declaration(name) {
  let start = source.indexOf(`function ${name}(`);
  assert.ok(start >= 0, `${name} declaration is present`);
  if (source.slice(start - 6, start) === "async ") start -= 6;
  const end = /^}\r?$/m.exec(source.slice(start));
  assert.ok(end, `${name} declaration has a closing brace`);
  return source.slice(start, start + end.index + end[0].length);
}

function historyHarness() {
  const start = source.indexOf("const histories = new Map();");
  const end = source.indexOf("function snapshotChecked(", start);
  assert.ok(start >= 0 && end > start, "tab history implementation is present");
  const buttons = {
    "undo-btn": { disabled: false, dataset: {} },
    "redo-btn": { disabled: false, dataset: {} },
  };
  let active = "home";
  let nextId = 0;
  const messages = [];
  const context = vm.createContext({
    Map,
    document: {
      querySelector: () => ({ dataset: { tab: active } }),
    },
    el: (id) => buttons[id],
    logAction: () => ++nextId,
    status: (message) => messages.push(message),
    statusErr: (message) => messages.push(message),
    // updateHistoryButtons now routes tooltips through setTip (the icon-only
    // buttons derive their accessible name from it). These stubs carry no
    // dataset.tipNamed, so mirroring just the data-tip write is faithful.
    setTip: (node, text) => { if (node) node.dataset.tip = text; },
  });
  vm.runInContext(`${source.slice(start, end)}
this.api = { pushOp, undo, redo, updateHistoryButtons, historyForTab,
             findSessionHistoryOp };`, context);
  return {
    api: context.api,
    context,
    buttons,
    messages,
    setActive(tab) { active = tab; context.api.updateHistoryButtons(); },
  };
}

test("undo and redo operate only on the active top-level tab", async () => {
  const h = historyHarness();
  const calls = [];
  const op = (label) => h.api.pushOp(
    label,
    async () => calls.push(`undo:${label}`),
    async () => calls.push(`redo:${label}`),
  );

  const homeId = op("home edit");
  h.setActive("workbench");
  const workbenchId = op("workbench edit");

  await h.api.undo();
  assert.deepEqual(calls, ["undo:workbench edit"]);
  assert.equal(h.api.historyForTab("home").ptr, 1);
  assert.equal(h.api.historyForTab("workbench").ptr, 0);

  h.setActive("home");
  assert.match(h.buttons["undo-btn"].dataset.tip, /home edit/);
  await h.api.undo();
  assert.deepEqual(calls, ["undo:workbench edit", "undo:home edit"]);
  assert.equal(h.api.historyForTab("home").ptr, 0);

  h.setActive("workbench");
  await h.api.redo();
  assert.deepEqual(calls, [
    "undo:workbench edit", "undo:home edit", "redo:workbench edit",
  ]);
  assert.equal(h.api.findSessionHistoryOp(homeId).label, "home edit");
  assert.equal(h.api.findSessionHistoryOp(workbenchId).label, "workbench edit");
});

test("a new action clears only that tab's redo branch", async () => {
  const h = historyHarness();
  const noop = async () => {};
  h.api.pushOp("home one", noop, noop);
  await h.api.undo();

  h.setActive("checked");
  h.api.pushOp("catalog one", noop, noop);
  h.setActive("home");
  h.api.pushOp("home replacement", noop, noop);

  assert.deepEqual(
    Array.from(h.api.historyForTab("home").stack, (op) => op.label),
    ["home replacement"],
  );
  assert.deepEqual(
    Array.from(h.api.historyForTab("checked").stack, (op) => op.label),
    ["catalog one"],
  );
});

test("failed inverse mutations remain available for a conditional retry", async () => {
  const h = historyHarness();
  let rejectUndo = true;
  let rejectRedo = true;
  h.api.pushOp(
    "conditional source change",
    async () => {
      if (rejectUndo) throw new Error("revision conflict");
    },
    async () => {
      if (rejectRedo) throw new Error("revision conflict");
    },
  );

  await h.api.undo();
  assert.equal(h.api.historyForTab("home").ptr, 1,
    "a failed undo must not be recorded as applied");
  rejectUndo = false;
  await h.api.undo();
  assert.equal(h.api.historyForTab("home").ptr, 0);

  await h.api.redo();
  assert.equal(h.api.historyForTab("home").ptr, 0,
    "a failed redo must remain redoable");
  rejectRedo = false;
  await h.api.redo();
  assert.equal(h.api.historyForTab("home").ptr, 1);
});

test("an async Workbench mutation stays in its initiating tab after a tab switch", async () => {
  const h = historyHarness();
  let finishPatch;
  const patchResponse = new Promise((resolve) => { finishPatch = resolve; });
  h.context.state = {
    builds: { book1: { id: "book1", title: "The Herbal", status: "draft" } },
  };
  h.context.patchBuildRaw = () => patchResponse;
  vm.runInContext(`${declaration("patchBuild")}
this.patchBuild = patchBuild;`, h.context);

  h.setActive("workbench");
  const pending = h.context.patchBuild(
    "book1", { status: "ready" }, "verify build");
  h.setActive("home");
  finishPatch(true);
  assert.equal(await pending, true);

  assert.deepEqual(
    Array.from(h.api.historyForTab("workbench").stack, (op) => op.label),
    ["verify build"],
  );
  assert.deepEqual(
    Array.from(h.api.historyForTab("home").stack, (op) => op.label),
    [],
  );
});

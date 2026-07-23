const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const {
  WorkbenchWindowRegistry,
  clampWindowBounds,
  normalizeWorkbenchContext,
  workbenchReuseKey,
} = require("../desktop/window-registry");


const root = path.join(__dirname, "..");
const mainSource = fs.readFileSync(path.join(root, "desktop", "main.js"), "utf8");
const preloadSource = fs.readFileSync(path.join(root, "desktop", "preload.js"), "utf8");
const desktopPackage = JSON.parse(fs.readFileSync(
  path.join(root, "desktop", "package.json"), "utf8"));


function context(overrides = {}) {
  return {
    schema: "librarytool.workbench-context/1",
    workbench_id: "corrections",
    workspace_id: "workspace-1",
    item_id: "book-1",
    representation_id: "scan-1",
    ...overrides,
  };
}


function trustedDocumentUrl(value, origin, documentPath = "/") {
  try {
    const url = new URL(value);
    return url.origin === origin && url.pathname === documentPath &&
      !url.search && !url.username && !url.password;
  } catch (error) {
    return false;
  }
}


class FakeEvents {
  constructor() { this.handlers = new Map(); }
  on(name, callback) {
    const values = this.handlers.get(name) || [];
    values.push({ callback, once: false });
    this.handlers.set(name, values);
  }
  once(name, callback) {
    const values = this.handlers.get(name) || [];
    values.push({ callback, once: true });
    this.handlers.set(name, values);
  }
  emit(name, ...args) {
    const values = this.handlers.get(name) || [];
    this.handlers.set(name, values.filter((value) => !value.once));
    for (const value of values) value.callback(...args);
  }
}


class FakeWindow extends FakeEvents {
  constructor(id, bounds = { x: 20, y: 30, width: 1200, height: 800 }) {
    super();
    this.destroyed = false;
    this.minimized = false;
    this.maximized = false;
    this.normalBounds = bounds;
    this.focusCalls = 0;
    this.showCalls = 0;
    this.restoreCalls = 0;
    this.webContents = {
      id,
      mainFrame: { url: "" },
      sent: [],
      send: (channel, value) => this.webContents.sent.push([channel, value]),
    };
  }
  isDestroyed() { return this.destroyed; }
  isMinimized() { return this.minimized; }
  isMaximized() { return this.maximized; }
  restore() { this.minimized = false; this.restoreCalls += 1; }
  show() { this.showCalls += 1; }
  focus() { this.focusCalls += 1; }
  getNormalBounds() { return { ...this.normalBounds }; }
  close() {
    this.emit("close", { preventDefault() {} });
    this.destroyed = true;
    this.emit("closed");
  }
}


function registryHarness(options = {}) {
  let nextWindow = 20;
  let nextIdentity = 1;
  const created = [];
  const state = new Map();
  const writes = [];
  const stateStore = {
    get(profile, workbench) {
      return state.get(`${profile}:${workbench}`) || null;
    },
    set(profile, workbench, value) {
      writes.push({ profile, workbench, value });
      state.set(`${profile}:${workbench}`, value);
    },
  };
  if (options.savedState) {
    state.set("corrections/default:corrections", options.savedState);
  }
  const registry = new WorkbenchWindowRegistry({
    origin: "http://127.0.0.1:45678",
    definitions: {
      corrections: {
        documentPath: "/corrections",
        width: 1280,
        height: 840,
        minWidth: 900,
        minHeight: 600,
        defaultUiProfileKey: "corrections/default",
      },
    },
    stateStore,
    getDisplays: () => options.displays || [{
      id: 1,
      workArea: { x: 0, y: 0, width: 1920, height: 1080 },
    }],
    makeWindowId: () => `window-${nextIdentity++}`,
  });
  const open = (request) => registry.open(request, ({ bounds }) => {
    const win = new FakeWindow(nextWindow++, bounds);
    created.push(win);
    return win;
  });
  return { registry, created, open, state, writes };
}


test("portable Corrections contexts are strict, canonical, and locally profiled", () => {
  const normalized = normalizeWorkbenchContext(context({
    canvas_id: "folio:1r",
    artifact_id: "artifact_1",
    annotation_id: "region-2",
    resource_revision: 12,
    view_hint: { focus: "annotation", editor_type: "image-overlay" },
    origin: { kind: "attention-item", id: "review_1" },
  }), {
    expectedWorkbenchId: "corrections",
    defaultUiProfileKey: "corrections/default",
  });
  assert.equal(normalized.ui_profile_key, "corrections/default");
  assert.deepEqual(Object.keys(normalized.view_hint), ["editor_type", "focus"]);
  assert.equal(normalized.canvas_id, "folio:1r");

  for (const invalid of [
    {},
    context({ schema: "librarytool.workbench-context/2" }),
    context({ workbench_id: "replica" }),
    context({ workspace_id: "../workspace" }),
    context({ ui_profile_key: "corrections/../other" }),
    { ...context(), local_path: "C:/private/book.jpg" },
  ]) {
    assert.throws(() => normalizeWorkbenchContext(invalid, {
      expectedWorkbenchId: "corrections",
      defaultUiProfileKey: "corrections/default",
    }), TypeError);
  }
});


test("reuse identity ignores selectors but includes item and representation", () => {
  const base = normalizeWorkbenchContext(context(), {
    expectedWorkbenchId: "corrections",
  });
  const navigated = normalizeWorkbenchContext(context({
    canvas_id: "page-9",
    artifact_id: "image-4",
    view_hint: { focus: "artifact" },
  }), { expectedWorkbenchId: "corrections" });
  const otherScan = normalizeWorkbenchContext(context({
    representation_id: "scan-2",
  }), { expectedWorkbenchId: "corrections" });
  assert.equal(workbenchReuseKey(base), workbenchReuseKey(navigated));
  assert.notEqual(workbenchReuseKey(base), workbenchReuseKey(otherScan));
});


test("inherited definition names cannot open an unregistered workbench", () => {
  const harness = registryHarness();
  assert.throws(() => harness.open({
    context: context({ workbench_id: "constructor" }),
    newWindow: false,
  }), /unknown workbench/);
  assert.equal(harness.created.length, 0);
});


test("registered document paths cannot escape the sidecar origin", () => {
  const harness = registryHarness();
  const manager = new FakeWindow(8);
  assert.throws(() => harness.registry.registerManager(manager, {
    documentPath: "//evil.example/corrections",
  }), /escapes the application origin/);
  assert.equal(harness.registry.byWebContentsId.has(manager.webContents.id), false);
});


test("same-context opens navigate and focus; explicit new windows stay independent", () => {
  const harness = registryHarness();
  const first = harness.open({ context: context(), newWindow: false });
  first.record.window.webContents.mainFrame.url = first.record.documentUrl;
  assert.equal(first.reused, false);
  assert.equal(harness.created.length, 1);

  first.record.window.minimized = true;
  const navigatedContext = context({
    canvas_id: "page-2",
    artifact_id: "figure-3",
    view_hint: { focus: "artifact" },
  });
  const reused = harness.open({ context: navigatedContext, newWindow: false });
  assert.equal(reused.reused, true);
  assert.equal(reused.record.windowId, first.record.windowId);
  assert.equal(harness.created.length, 1);
  assert.equal(first.record.window.restoreCalls, 1);
  assert.equal(first.record.window.showCalls, 1);
  assert.equal(first.record.window.focusCalls, 1);
  assert.equal(first.record.context.artifact_id, "figure-3");
  assert.deepEqual(first.record.window.webContents.sent.at(-1), [
    "workbench:context",
    normalizeWorkbenchContext(navigatedContext, {
      expectedWorkbenchId: "corrections",
      defaultUiProfileKey: "corrections/default",
    }),
  ]);

  const duplicate = harness.open({ context: context(), newWindow: true });
  duplicate.record.window.webContents.mainFrame.url = duplicate.record.documentUrl;
  assert.equal(duplicate.reused, false);
  assert.notEqual(duplicate.record.windowId, first.record.windowId);
  assert.equal(harness.created.length, 2);
});


test("only exact registered live main frames resolve as trusted application senders", () => {
  const harness = registryHarness();
  const opened = harness.open({ context: context(), newWindow: false });
  const record = opened.record;
  const win = record.window;
  win.webContents.mainFrame.url = record.documentUrl;
  const event = {
    sender: win.webContents,
    senderFrame: win.webContents.mainFrame,
  };

  assert.equal(harness.registry.recordForEvent(event, trustedDocumentUrl), record);
  assert.equal(harness.registry.trustForWebRequest(
    win.webContents.id, trustedDocumentUrl).documentPath, "/corrections");
  assert.equal(harness.registry.recordForEvent({
    ...event, senderFrame: { url: record.documentUrl },
  }, trustedDocumentUrl), null, "same-URL subframe is not the registered main frame");
  assert.equal(harness.registry.recordForEvent({
    ...event, sender: { ...win.webContents },
  }, trustedDocumentUrl), null, "same-id spoof is not the registered webContents");
  assert.equal(harness.registry.recordForEvent({
    sender: { id: 99, mainFrame: { url: record.documentUrl } },
    senderFrame: { url: record.documentUrl },
  }, trustedDocumentUrl), null, "resource and unknown windows are not registered");

  win.webContents.mainFrame.url = "http://127.0.0.1:45678/";
  assert.equal(harness.registry.recordForEvent(event, trustedDocumentUrl), null);
  assert.equal(harness.registry.trustForWebRequest(
    win.webContents.id, trustedDocumentUrl), null, "navigated frame loses transport trust");
  win.webContents.mainFrame.url = "https://attacker.example/corrections";
  assert.equal(harness.registry.recordForEvent(event, trustedDocumentUrl), null);
});


test("restored bounds are profile-scoped, clamped, and persisted on close", () => {
  const harness = registryHarness({
    savedState: {
      bounds: { x: 8000, y: 6000, width: 2600, height: 1800 },
      maximized: true,
    },
  });
  const opened = harness.open({ context: context(), newWindow: false });
  assert.deepEqual(opened.record.window.normalBounds,
    { x: 0, y: 0, width: 1920, height: 1080 });
  assert.equal(opened.record.restoredState.maximized, true);

  opened.record.window.normalBounds = { x: 40, y: 50, width: 1320, height: 870 };
  opened.record.window.maximized = false;
  opened.record.window.close();
  assert.deepEqual(harness.writes, [{
    profile: "corrections/default",
    workbench: "corrections",
    value: {
      bounds: { x: 40, y: 50, width: 1320, height: 870 },
      maximized: false,
    },
  }]);
  assert.equal(harness.registry.trustForWebRequest(
    opened.record.window.webContents.id, trustedDocumentUrl), null);
});


test("bounds restoration keeps partially visible windows on their display", () => {
  assert.deepEqual(clampWindowBounds(
    { x: 1810, y: 900, width: 1200, height: 800 },
    { width: 1200, height: 800, minWidth: 900, minHeight: 600 },
    [{ workArea: { x: 0, y: 0, width: 1920, height: 1080 } }],
  ), { x: 720, y: 280, width: 1200, height: 800 });

  assert.deepEqual(clampWindowBounds(
    { x: 2100, y: 100, width: 1100, height: 700 },
    { width: 1200, height: 800, minWidth: 900, minHeight: 600 },
    [
      { workArea: { x: 0, y: 0, width: 1920, height: 1080 } },
      { workArea: { x: 1920, y: 0, width: 1600, height: 900 } },
    ],
  ), { x: 2100, y: 100, width: 1100, height: 700 });
});


test("main-process wiring scopes controls and keeps workbench close separate from quit", () => {
  assert.match(mainSource,
    /ipcMain\.on\("win:minimize"[\s\S]*?record\.window\.minimize\(\)/);
  assert.match(mainSource,
    /ipcMain\.on\("win:toggle-maximize"[\s\S]*?record\.window\.maximize\(\)/);
  assert.match(mainSource,
    /ipcMain\.on\("win:close"[\s\S]*?record\.window\.close\(\)/);
  assert.match(mainSource,
    /ensureWorkbenchWindowRegistry\(\)\.registerManager\(mainWindow/);
  assert.match(mainSource,
    /trustForWebRequest\([\s\S]*?isTrustedAppDocumentUrl/);

  const workbenchBlock = mainSource.slice(
    mainSource.indexOf("function configureWorkbenchWindow"),
    mainSource.indexOf("function createWindow"),
  );
  assert.doesNotMatch(workbenchBlock,
    /confirmCloseWithJobs|sidecar\.kill|app\.quit|closingThrough/);
  assert.match(workbenchBlock, /win\.loadURL\(record\.documentUrl\)/);
  assert.match(workbenchBlock, /sandbox: true/);
});


test("preload and packaging expose only the validated workbench IPC bridge", () => {
  assert.match(preloadSource,
    /workbenches:[\s\S]*?ipcRenderer\.invoke\("workbench:open"/);
  assert.match(preloadSource,
    /currentContext: \(\) => ipcRenderer\.invoke\("workbench:context:get"\)/);
  assert.match(preloadSource,
    /ipcRenderer\.removeListener\("workbench:context", listener\)/);
  assert.ok(desktopPackage.build.files.includes("window-registry.js"));
});

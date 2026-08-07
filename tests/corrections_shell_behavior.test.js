const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const {
  createDefaultEditorRegistry,
  resourceFamily,
} = require("../tools/whl_explorer/static/corrections/editor-registry");
const {
  CorrectionsIndexStore,
} = require("../tools/whl_explorer/static/corrections/books");
const {
  EngineClient,
} = require("../tools/whl_explorer/static/engine-client");
const {
  CorrectionsProfileStore,
  PROFILE_SCHEMA,
  TOOL_PROFILE_SCHEMA,
  validateProfileKey,
} = require("../tools/whl_explorer/static/corrections/ui-profile");
const {
  createImageAdjustTool,
  normalizeImageAdjustProfile,
} = require("../tools/whl_explorer/static/corrections/image-adjust-tool");
const {
  CorrectionCommandRegistry,
  DEFAULT_CLASSIFICATION_COMMANDS,
  normalizeKeyBinding,
} = require("../tools/whl_explorer/static/corrections/commands");
const {
  DEFAULT_LAYOUT,
  EDITOR_MIN_HEIGHT,
  EDITOR_MIN_WIDTH,
  LayoutController,
  fitHorizontalLayoutState,
  fitVerticalLayoutState,
  keyboardCoordinateDelta,
  normalizeLayoutState,
  resizeLayoutState,
} = require("../tools/whl_explorer/static/corrections/layout-controller");
const {
  BOOKS_NAVIGATION_COMMANDS,
  CONTEXT_SCHEMA,
  CorrectionsShell,
  CorrectionsWindowState,
  artifactSelection,
  correctionsRuntimePorts,
  navigationOnlyTarget,
  nextTrayTab,
  normalizeSelection,
  normalizeWorkbenchContext,
  selectionContext,
} = require("../tools/whl_explorer/static/corrections/shell");
const {
  FakeNode,
  fakeDocument,
} = require("./fixtures/corrections_fake_dom");


const root = path.join(__dirname, "..");
const templateSource = fs.readFileSync(path.join(
  root, "tools", "whl_explorer", "templates", "corrections.html"), "utf8");
const cssSource = fs.readFileSync(path.join(
  root, "tools", "whl_explorer", "static", "corrections", "corrections.css"), "utf8");
const shellSource = fs.readFileSync(path.join(
  root, "tools", "whl_explorer", "static", "corrections", "shell.js"), "utf8");
const layoutSource = fs.readFileSync(path.join(
  root, "tools", "whl_explorer", "static", "corrections", "layout-controller.js"), "utf8");


function context(overrides = {}) {
  return {
    schema: CONTEXT_SCHEMA,
    workbench_id: "corrections",
    workspace_id: "workspace-1",
    item_id: "book-1",
    representation_id: "scan-1",
    ...overrides,
  };
}


class MemoryStorage {
  constructor() { this.values = new Map(); }
  getItem(key) { return this.values.has(key) ? this.values.get(key) : null; }
  setItem(key, value) { this.values.set(key, String(value)); }
  removeItem(key) { this.values.delete(key); }
}


class MiniNode {
  constructor(tagName, documentRef = null) {
    this.tagName = tagName.toUpperCase();
    this.ownerDocument = documentRef;
    this.children = [];
    this.attributes = new Map();
    this.textContent = "";
    this.className = "";
  }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; }
  setAttribute(name, value) { this.attributes.set(name, String(value)); }
}


function miniDocument() {
  const documentRef = {
    createElement(name) { return new MiniNode(name, documentRef); },
  };
  return documentRef;
}


class FakeEventTarget {
  constructor() { this.listeners = new Map(); }
  addEventListener(type, callback) {
    const listeners = this.listeners.get(type) || [];
    listeners.push(callback);
    this.listeners.set(type, listeners);
  }
  removeEventListener(type, callback) {
    const listeners = this.listeners.get(type) || [];
    this.listeners.set(type, listeners.filter((listener) => listener !== callback));
  }
  emit(type, value) {
    for (const listener of this.listeners.get(type) || []) listener(value);
  }
}


function layoutHarness(options = {}) {
  const documentRef = new FakeEventTarget();
  const styleValues = new Map();
  const workspace = {
    clientWidth: options.width || 1600,
    clientHeight: options.height || 900,
    dataset: {},
    style: { setProperty: (name, value) => styleValues.set(name, value) },
  };
  const rootElement = new FakeEventTarget();
  rootElement.dataset = {};
  rootElement.ownerDocument = documentRef;
  rootElement.querySelector = (selector) =>
    selector === "[data-workspace-layout]" ? workspace : null;
  rootElement.querySelectorAll = () => [];
  const changes = [];
  const controller = new LayoutController({
    root: rootElement,
    documentRef,
    bind: false,
    initialState: options.initialState,
    onChange: (state, reason) => changes.push({ state, reason }),
  });
  return { changes, controller, documentRef, rootElement, styleValues, workspace };
}


test("typed editor registry routes supported resources and safely falls back", () => {
  const documentRef = miniDocument();
  const registry = createDefaultEditorRegistry({ documentRef });

  const image = { id: "page-1", kind: "captured-image", url: "/resource/page-1" };
  assert.equal(resourceFamily(image), "image");
  assert.equal(resourceFamily({
    ...image,
    family: "image",
    media_type: "image/jpeg",
    regions: [],
  }), "image", "decoded image details retain image editor precedence");
  assert.equal(resourceFamily({
    ...image,
    media_type: "image/jpeg",
    regions: [],
  }), "image", "an optional regions collection cannot mask image media");
  assert.deepEqual(registry.compatibleEditors(image).map((editor) => editor.id), [
    "image-overlay", "image-plain",
  ]);
  assert.equal(registry.setResource(image).id, "image-overlay");
  assert.equal(registry.selectEditor("image-plain"), true);
  assert.equal(registry.selectEditor("ocr-text"), false);

  assert.equal(registry.setResource({ kind: "ocr-text", text: "leaf text" }).id, "ocr-text");
  assert.equal(registry.setResource({ kind: "metadata", metadata: { title: "Herbs" } }).id,
    "structured-metadata");
  assert.equal(registry.setResource({ kind: "regions", regions: [] }).id, "region-list");
  assert.equal(registry.setResource(image).id, "image-plain", "choice is remembered by family");

  registry.restoreChoices({ image: "ocr-text", text: "ocr-text", unknown: "bad-id" });
  assert.deepEqual(registry.serializeChoices(), { text: "ocr-text" });

  const host = new MiniNode("div", documentRef);
  registry.setResource(null);
  assert.equal(registry.render(host), "empty-resource");
  assert.equal(host.children[0].className, "editor-empty");
  registry.setResource({ id: "unsafe", kind: "executable-widget", label: "<script>bad()</script>" });
  assert.equal(registry.render(host), "unsupported-resource");
  assert.equal(host.children[0].className, "editor-unsupported");

  registry.setResource({ kind: "image", url: "javascript:alert(1)" });
  assert.equal(registry.render(host), "image-overlay");
  assert.equal(host.children[0].children[0].className, "editor-unsupported");
});


test("editor registry disposes interactive renderers before replacement and destroy", () => {
  const documentRef = miniDocument();
  let renders = 0;
  let cleanups = 0;
  const registry = createDefaultEditorRegistry({
    documentRef,
    imageOverlayRenderer({ container }) {
      renders += 1;
      container.replaceChildren(new MiniNode("canvas", documentRef));
      return () => { cleanups += 1; };
    },
  });
  const host = new MiniNode("div", documentRef);
  registry.setResource({ id: "page-1", kind: "captured-image", url: "/page-1" });
  registry.render(host);
  registry.render(host);
  assert.equal(renders, 2);
  assert.equal(cleanups, 1);

  registry.setResource({ id: "ocr-1", kind: "ocr-text", text: "sage" });
  registry.render(host);
  assert.equal(cleanups, 2);
  registry.destroy();
  assert.equal(cleanups, 2);
});


test("layout validation clamps dimensions and accepts only explicit collapse state", () => {
  const state = normalizeLayoutState({
    navigatorWidth: -500,
    booksHeight: 9000,
    propertiesWidth: "450",
    trayHeight: Number.POSITIVE_INFINITY,
    collapsed: { books: true, artifacts: 1, properties: false, tray: true },
    primaryMaximized: "true",
  });
  assert.deepEqual(state, {
    navigatorWidth: 220,
    booksHeight: 720,
    propertiesWidth: 320,
    trayHeight: 220,
    collapsed: { books: true, artifacts: false, properties: false, tray: true },
    primaryMaximized: false,
  });

  assert.equal(resizeLayoutState(DEFAULT_LAYOUT, "navigator", -1000).navigatorWidth, 220);
  assert.equal(resizeLayoutState(DEFAULT_LAYOUT, "navigator", 1000).navigatorWidth, 520);
  assert.equal(resizeLayoutState(DEFAULT_LAYOUT, "properties", 40).propertiesWidth, 280);
  assert.equal(resizeLayoutState(DEFAULT_LAYOUT, "books", -1000).booksHeight, 120);
  assert.equal(resizeLayoutState(DEFAULT_LAYOUT, "tray", -1000).trayHeight, 440);
  assert.throws(() => resizeLayoutState(DEFAULT_LAYOUT, "unknown", 10), TypeError);
});

test("restored side panes jointly preserve the editor minimum above compact mode", () => {
  const width = 1050;
  const fitted = fitHorizontalLayoutState({
    navigatorWidth: 520,
    propertiesWidth: 560,
  }, width);
  assert.ok(fitted.navigatorWidth >= 220);
  assert.ok(fitted.propertiesWidth >= 240);
  assert.equal(
    fitted.navigatorWidth + fitted.propertiesWidth + EDITOR_MIN_WIDTH + 14,
    width,
  );

  const collapsed = fitHorizontalLayoutState({
    navigatorWidth: 520,
    propertiesWidth: 560,
    collapsed: { properties: true },
  }, width);
  assert.equal(collapsed.navigatorWidth, 520,
    "a hidden Properties panel must not erase its remembered expanded width");
  assert.equal(collapsed.propertiesWidth, 560);

  const compact = fitHorizontalLayoutState({
    navigatorWidth: 520,
    propertiesWidth: 560,
  }, 900);
  assert.equal(compact.navigatorWidth, 520,
    "compact drawers do not consume horizontal editor space");
  assert.equal(compact.propertiesWidth, 560);
});

test("restored vertical panes preserve editor and sibling minimum heights", () => {
  const height = 494;
  const fitted = fitVerticalLayoutState({
    booksHeight: 720,
    trayHeight: 440,
  }, height);
  assert.equal(fitted.booksHeight + 120 + 7, height);
  assert.equal(fitted.trayHeight + EDITOR_MIN_HEIGHT + 7, height);

  const collapsed = fitVerticalLayoutState({
    booksHeight: 720,
    trayHeight: 440,
    collapsed: { artifacts: true, tray: true },
  }, height);
  assert.equal(collapsed.booksHeight, 720);
  assert.equal(collapsed.trayHeight, 440);
});


test("layout gutters support keyboard resize, reset, collapse, maximize, and compact drawers", () => {
  const { changes, controller, rootElement, styleValues } = layoutHarness();
  const keyEvent = (key, shiftKey = false) => ({
    key,
    shiftKey,
    prevented: false,
    preventDefault() { this.prevented = true; },
  });

  assert.equal(keyboardCoordinateDelta("navigator", "ArrowRight"), 16);
  assert.equal(keyboardCoordinateDelta("books", "ArrowDown", true), 48);
  assert.equal(keyboardCoordinateDelta("navigator", "ArrowDown"), null);

  const growNavigator = keyEvent("ArrowRight", true);
  assert.equal(controller.handleGutterKey("navigator", growNavigator), true);
  assert.equal(growNavigator.prevented, true);
  assert.equal(controller.getState().navigatorWidth, 340);

  const shrinkProperties = keyEvent("ArrowRight");
  controller.handleGutterKey("properties", shrinkProperties);
  assert.equal(controller.getState().propertiesWidth, 304);
  controller.handleGutterKey("properties", keyEvent("Home"));
  assert.equal(controller.getState().propertiesWidth, 240);
  controller.handleGutterKey("properties", keyEvent("End"));
  assert.equal(controller.getState().propertiesWidth, 560);
  assert.equal(controller.handleGutterKey("properties", keyEvent("PageDown")), false);

  assert.equal(controller.toggleCollapse("artifacts", true), true);
  assert.equal(controller.getState().collapsed.artifacts, true);
  assert.equal(controller.toggleCollapse("not-a-pane"), false);
  assert.equal(controller.togglePrimaryMaximized(true), true);
  assert.equal(controller.getState().primaryMaximized, true);
  controller.resetDimension("navigator");
  assert.equal(controller.getState().navigatorWidth, DEFAULT_LAYOUT.navigatorWidth);
  controller.reset();
  assert.deepEqual(controller.getState(), normalizeLayoutState(DEFAULT_LAYOUT));

  assert.equal(controller.toggleDrawer("navigator"), false, "drawers require compact mode");
  controller.setCompact(true);
  assert.equal(rootElement.dataset.compact, "true");
  assert.equal(controller.toggleDrawer("navigator"), true);
  assert.equal(controller.drawers.navigator, true);
  assert.equal(controller.toggleDrawer("properties"), true);
  assert.deepEqual(controller.drawers, { navigator: false, properties: true });
  controller.closeDrawers();
  assert.deepEqual(controller.drawers, { navigator: false, properties: false });
  controller.togglePrimaryMaximized(true);
  assert.equal(controller.toggleDrawer("navigator"), false,
    "drawers stay closed while the primary editor is maximized");
  controller.togglePrimaryMaximized(false);
  assert.equal(styleValues.get("--navigator-width"), "292px");
  assert.ok(changes.some((entry) => entry.reason === "keyboard-resize"));
});


test("pointer resizing uses the closest live coordinate and releases drag state", () => {
  const { controller, documentRef } = layoutHarness();
  const gutter = { dataset: {}, setPointerCapture() {} };
  let prevented = false;
  controller.startPointerResize("navigator", gutter, {
    button: 0,
    clientX: 100,
    clientY: 0,
    pointerId: 7,
    preventDefault() { prevented = true; },
  });
  assert.equal(prevented, true);
  assert.equal(gutter.dataset.dragging, "true");
  documentRef.emit("pointermove", { clientX: 148, clientY: 0 });
  assert.equal(controller.getState().navigatorWidth, DEFAULT_LAYOUT.navigatorWidth + 48);
  documentRef.emit("pointerup", {});
  assert.equal("dragging" in gutter.dataset, false);
  assert.equal(controller.activePointerCleanup, null);
});


test("layout limits preserve a usable editor at smaller non-compact widths", () => {
  const width = 1050;
  const { controller } = layoutHarness({ width, height: 600 });
  controller.handleGutterKey("navigator", {
    key: "End", shiftKey: false, preventDefault() {},
  });
  controller.handleGutterKey("tray", {
    key: "End", shiftKey: false, preventDefault() {},
  });
  const state = controller.getState();
  assert.equal(state.navigatorWidth, 356);
  assert.ok(state.navigatorWidth + state.propertiesWidth + EDITOR_MIN_WIDTH + 14 <= width);
  assert.equal(controller.getState().trayHeight, 333);
});


test("UI profiles are isolated, validated, and persist presentation/tool choices only", () => {
  const storage = new MemoryStorage();
  const registry = createDefaultEditorRegistry();
  const store = new CorrectionsProfileStore({
    storage,
    normalizeLayout: normalizeLayoutState,
    normalizeEditors: (value) => registry.validateChoices(value),
    normalizeTools: (value) => ({
      imageAdjust: normalizeImageAdjustProfile(value && value.imageAdjust),
    }),
  });
  const saved = store.save("corrections/default", {
    layout: { navigatorWidth: 410, collapsed: { tray: true } },
    editors: { image: "image-plain", text: "image-overlay" },
    tools: {
      imageAdjust: { lastAppliedBrightness: 24 },
      privateLocator: "must-not-persist",
    },
    selection: { itemId: "must-not-persist" },
    drafts: { caption: "must-not-persist" },
  });
  assert.equal(saved.schema, PROFILE_SCHEMA);
  assert.equal(saved.layout.navigatorWidth, 410);
  assert.deepEqual(saved.editors, { image: "image-plain" });
  assert.deepEqual(saved.tools, {
    imageAdjust: { lastAppliedBrightness: 24 },
  });
  assert.deepEqual(
    store.load("corrections/default").tools.imageAdjust,
    { lastAppliedBrightness: 24 },
  );
  store.save("corrections/default", {
    tools: { imageAdjust: { lastAppliedBrightness: -12 } },
  });
  assert.deepEqual(
    store.load("corrections/default").tools.imageAdjust,
    { lastAppliedBrightness: -12 },
    "the public full-profile save contract also replaces tool sidecars",
  );
  assert.deepEqual(Object.keys(saved).sort(),
    ["editors", "layout", "profile_key", "schema", "tools"]);
  assert.equal(store.load("corrections/default").found, true);
  assert.equal(store.load("corrections/alternate").found, false);

  storage.setItem(store.key("corrections/broken"), "{bad json");
  const broken = store.load("corrections/broken");
  assert.equal(broken.found, false);
  assert.deepEqual(broken.layout, normalizeLayoutState({}));
  assert.throws(() => validateProfileKey("corrections/../private"), TypeError);
  assert.throws(() => store.load("corrections/__proto__"), TypeError);
  assert.equal(store.matchesStorageEvent("corrections/default", {
    key: store.key("corrections/default"),
    storageArea: storage,
  }), true);
  assert.equal(store.matchesStorageEvent("corrections/default", {
    key: store.toolKey("corrections/default", "imageAdjust"),
    storageArea: storage,
  }), true);
  assert.equal(store.matchesStorageEvent("corrections/default", {
    key: store.key("corrections/alternate"),
    storageArea: storage,
  }), false);
  assert.equal(store.matchesStorageEvent("corrections/default", {
    key: store.key("corrections/default"),
    storageArea: new MemoryStorage(),
  }), false);
  assert.throws(
    () => store.toolKey("corrections/default", "__proto__"),
    TypeError,
  );
  const imageAdjustKey = store.toolKey(
    "corrections/default", "imageAdjust");
  assert.equal(store.clear("corrections/default"), true);
  assert.equal(storage.getItem(imageAdjustKey), null);
  assert.equal(store.load("corrections/default").found, false);
});


test("shell profile persistence restores classification remaps without domain state", () => {
  const registry = new CorrectionCommandRegistry();
  for (const command of DEFAULT_CLASSIFICATION_COMMANDS) {
    registry.register({ ...command, execute: async () => null });
  }
  const shell = Object.create(CorrectionsShell.prototype);
  shell.classificationController = { registry };
  shell.restoringProfile = false;
  shell.restoreClassificationProfile({
    bindings: {
      "corrections.category.title-page": "ctrl+t",
      "corrections.category.cover": "v",
    },
  });
  assert.equal(registry.bindingFor("corrections.category.title-page"), "ctrl+t");
  assert.equal(registry.bindingFor("corrections.category.cover"), "v");
  assert.equal(registry.bindingFor("corrections.category.spine"), "s");

  let saved = null;
  shell.layout = { getState: () => ({ navigatorWidth: 300 }) };
  shell.editorRegistry = { serializeChoices: () => ({ image: "image-overlay" }) };
  shell.imageAdjustTool = {
    serializeProfile: () => ({ lastAppliedBrightness: 18 }),
  };
  shell.profileKey = "corrections/default";
  shell.profileStore = {
    save(profileKey, value) { saved = { profileKey, value }; },
  };
  shell.updateProfileLabel = () => {};
  shell.persistProfile();

  assert.equal(saved.profileKey, "corrections/default");
  assert.deepEqual(saved.value.tools.imageAdjust, {
    lastAppliedBrightness: 18,
  });
  assert.equal(
    saved.value.tools.classification.bindings["corrections.category.title-page"],
    "ctrl+t",
  );
  assert.equal("selection" in saved.value, false);
  assert.equal("drafts" in saved.value, false);

  shell.restoreClassificationProfile(null);
  for (const command of DEFAULT_CLASSIFICATION_COMMANDS) {
    assert.equal(registry.bindingFor(command.id), command.defaultBinding);
  }
});


test("cross-window layout saves cannot roll back committed Image Adjust brightness", () => {
  const storage = new MemoryStorage();
  const createStore = () => new CorrectionsProfileStore({
    storage,
    normalizeLayout: normalizeLayoutState,
    normalizeEditors: (value) => value && typeof value === "object" ? value : {},
    normalizeTools: (value) => ({
      imageAdjust: normalizeImageAdjustProfile(value && value.imageAdjust),
      classification: value && value.classification || { bindings: {} },
    }),
  });
  const firstStore = createStore();
  const secondStore = createStore();
  firstStore.save("corrections/default", {
    tools: {
      imageAdjust: { lastAppliedBrightness: 0 },
      classification: { bindings: {} },
    },
  });

  function profileShell(store, brightness, navigatorWidth) {
    const shell = Object.create(CorrectionsShell.prototype);
    shell.profileKey = "corrections/default";
    shell.profileStore = store;
    shell.layout = {
      getState: () => ({ navigatorWidth }),
    };
    shell.editorRegistry = { serializeChoices: () => ({}) };
    shell.classificationController = null;
    shell.imageAdjustTool = createImageAdjustTool({
      profile: { lastAppliedBrightness: brightness },
    });
    shell.updateProfileLabel = () => {};
    return shell;
  }

  const firstWindow = profileShell(firstStore, 0, 320);
  const secondWindow = profileShell(secondStore, 0, 410);
  firstWindow.persistProfile({
    toolUpdates: {
      imageAdjust: { lastAppliedBrightness: 37 },
    },
  });
  secondWindow.persistProfile();

  assert.deepEqual(
    secondStore.load("corrections/default").tools.imageAdjust,
    { lastAppliedBrightness: 37 },
    "an unrelated stale window save preserves the successful commit",
  );
  assert.equal(secondWindow.handleProfileStorageEvent({
    key: secondStore.key("corrections/default"),
    storageArea: storage,
  }), true);
  assert.deepEqual(secondWindow.imageAdjustTool.serializeProfile(), {
    lastAppliedBrightness: 37,
  });
  assert.equal(secondWindow.imageAdjustTool.getState().brightness, 37);

  assert.equal(secondWindow.handleProfileStorageEvent({
    key: secondStore.key("corrections/alternate"),
    storageArea: storage,
  }), false);
  firstWindow.imageAdjustTool.destroy();
  secondWindow.imageAdjustTool.destroy();
});


test("tool sidecars survive an interleaved cross-window profile write", () => {
  const storage = new MemoryStorage();
  const createStore = () => new CorrectionsProfileStore({
    storage,
    normalizeLayout: normalizeLayoutState,
    normalizeEditors: (value) => value && typeof value === "object" ? value : {},
    normalizeTools: (value) => ({
      imageAdjust: normalizeImageAdjustProfile(value && value.imageAdjust),
      classification: value && value.classification || { bindings: {} },
    }),
  });
  const firstStore = createStore();
  const secondStore = createStore();
  firstStore.save("corrections/default", {
    tools: {
      imageAdjust: { lastAppliedBrightness: 0 },
      classification: { bindings: {} },
    },
  });

  function profileShell(store, brightness, navigatorWidth) {
    const shell = Object.create(CorrectionsShell.prototype);
    Object.assign(shell, {
      profileKey: "corrections/default",
      profileStore: store,
      layout: { getState: () => ({ navigatorWidth }) },
      editorRegistry: { serializeChoices: () => ({}) },
      classificationController: null,
      imageAdjustTool: createImageAdjustTool({
        profile: { lastAppliedBrightness: brightness },
      }),
      updateProfileLabel() {},
    });
    return shell;
  }

  const firstWindow = profileShell(firstStore, 0, 320);
  const secondWindow = profileShell(secondStore, 0, 410);
  const originalLoad = secondStore.load.bind(secondStore);
  let interleave = true;
  secondStore.load = (profileKey) => {
    const stale = originalLoad(profileKey);
    if (interleave) {
      interleave = false;
      firstWindow.persistProfile({
        toolUpdates: {
          imageAdjust: { lastAppliedBrightness: 37 },
        },
      });
    }
    return stale;
  };

  secondWindow.persistProfile();

  assert.deepEqual(
    originalLoad("corrections/default").tools.imageAdjust,
    { lastAppliedBrightness: 37 },
  );
  const sidecar = JSON.parse(storage.getItem(
    firstStore.toolKey("corrections/default", "imageAdjust"),
  ));
  assert.equal(sidecar.schema, TOOL_PROFILE_SCHEMA);
  assert.equal(sidecar.value.lastAppliedBrightness, 37);
  firstWindow.imageAdjustTool.destroy();
  secondWindow.imageAdjustTool.destroy();
});


test("profile storage listeners are window-scoped and removed on shell destroy", () => {
  const windowRef = new FakeEventTarget();
  const shell = Object.create(CorrectionsShell.prototype);
  const received = [];
  Object.assign(shell, {
    windowRef,
    listeners: [],
    destroyed: false,
    contextGeneration: 0,
    featureContextGeneration: 0,
    unsubscribeContext: null,
    unsubscribeTransformResults: null,
    unsubscribeClassificationBindings: null,
    classificationControls: null,
    classificationController: null,
    booksFeature: null,
    artifactsFeature: null,
    itemProperties: null,
    selectionListeners: new Set(),
    editorRegistry: null,
    imageAdjustTool: null,
    layout: { destroy() {} },
    handleProfileStorageEvent(event) {
      received.push(event);
    },
  });
  shell.bindProfileSync();

  const event = { key: "librarytool.corrections-ui-profile:test" };
  windowRef.emit("storage", event);
  assert.deepEqual(received, [event]);

  shell.destroy();
  windowRef.emit("storage", { key: "after-destroy" });
  assert.deepEqual(received, [event]);
  assert.equal(windowRef.listeners.get("storage").length, 0);
});


test("classification shortcuts stay scoped and context menus use exact event targets", () => {
  const shell = Object.create(CorrectionsShell.prototype);
  shell.root = { dataset: {} };
  const captureTarget = {
    key: "artifact:capture-1",
    objectType: "raster-artifact",
    family: "image",
    itemId: "book-1",
    id: "capture-1",
    revision: "capture-r1",
  };
  const artifactTarget = {
    key: "artifact:figure-1",
    objectType: "raster-artifact",
    family: "image",
    itemId: "book-1",
    id: "figure-1",
    revision: "figure-r1",
  };
  const overlayTarget = {
    key: "annotation:region-1",
    objectType: "spatial-annotation",
    itemId: "book-1",
    id: "region-1",
    revision: "region-r1",
  };
  const canvasTarget = {
    key: "artifact:canvas-image",
    objectType: "raster-artifact",
    family: "image",
    itemId: "book-1",
    id: "canvas-image",
    revision: "canvas-r1",
  };
  shell.booksFeature = { books: {
    commandTargetForSelection(address) {
      return address.itemId === "book-1" &&
          address.artifactId === "capture-1"
        ? captureTarget : null;
    },
  } };
  shell.artifactsFeature = {
    items: new Map([[artifactTarget.key, artifactTarget]]),
  };
  shell.classificationController = {
    stateSnapshot: () => ({
      selectionFocused: true,
      selectionTarget: overlayTarget,
      hotTarget: null,
    }),
  };
  shell.state = { resource: { summary: canvasTarget } };

  const reviewButton = {
    dataset: { reviewAction: "resolve" },
    parentNode: { dataset: { trayPanel: "reviews" }, parentNode: shell.root },
  };
  const booksList = { dataset: { booksList: "" }, parentNode: shell.root };
  const captureButton = {
    dataset: { itemId: "book-1", artifactId: "capture-1" },
    parentNode: booksList,
  };
  const bookRow = {
    dataset: { bookId: "book-1" },
    parentNode: booksList,
  };
  const artifactsTree = {
    dataset: { artifactsTree: "" },
    parentNode: shell.root,
  };
  const artifactRow = {
    dataset: { artifactKey: artifactTarget.key },
    parentNode: artifactsTree,
  };
  const artifactGroup = {
    dataset: { treeKey: "group:source-images" },
    parentNode: artifactsTree,
  };
  const editorHost = { dataset: { editorHost: "" }, parentNode: shell.root };
  const overlayWrapper = {
    dataset: { overlayKey: overlayTarget.key },
    parentNode: editorHost,
  };
  const overlayMarker = {
    dataset: {},
    parentNode: overlayWrapper,
  };
  const editorCanvas = {
    dataset: { classificationCanvas: "true" },
    parentNode: editorHost,
  };
  const editorWhitespace = {
    dataset: {},
    parentNode: editorHost,
  };
  const classificationToolbarButton = {
    dataset: {},
    parentNode: { dataset: { classificationToolbar: "" }, parentNode: shell.root },
  };
  assert.equal(shell.classificationEventEligible(
    { target: reviewButton }, null, {}), false);
  assert.equal(shell.classificationEventEligible(
    { target: captureButton }, null, {}), true);
  assert.equal(shell.classificationEventEligible(
    { target: editorCanvas }, null, {}), true);
  assert.equal(shell.classificationEventEligible(
    { target: classificationToolbarButton }, null, {}), true);
  assert.equal(shell.classificationEventEligible(
    { target: reviewButton }, null, { softTarget: overlayTarget }), true,
    "a hovered target keeps its hotkeys while focus sits outside the surfaces");
  assert.equal(shell.classificationEventEligible(
    { target: reviewButton },
    { targetKind: "annotation" },
    { softTarget: overlayTarget }), true);
  assert.equal(shell.classificationEventEligible(
    { target: reviewButton },
    { targetKind: "image" },
    { softTarget: overlayTarget }), false,
    "hover only widens the gate for commands that accept the hovered target");

  assert.equal(shell.classificationContextMenuTarget(
    { target: captureButton }), captureTarget);
  assert.equal(shell.classificationContextMenuTarget(
    { target: artifactRow }), artifactTarget);
  assert.equal(shell.classificationContextMenuTarget(
    { target: overlayMarker }), overlayTarget);
  assert.equal(shell.classificationContextMenuTarget(
    { target: editorCanvas }), canvasTarget);
  assert.equal(shell.classificationContextMenuTarget(
    { target: bookRow }), null,
  "book rows without a capture cannot borrow a stale classification target");
  assert.equal(shell.classificationContextMenuTarget(
    { target: artifactGroup }), null,
  "non-classifiable tree rows cannot borrow a stale classification target");
  assert.equal(shell.classificationContextMenuTarget(
    { target: editorWhitespace }), null,
  "editor whitespace cannot borrow a stale classification target");
  assert.equal(shell.classificationContextMenuEligible(
    { target: classificationToolbarButton }), false,
    "classification context menus stay on browsable image/artifact surfaces");
});


test("navigation hints stay inactive and cannot overwrite hydrated selection targets", () => {
  const hint = {
    key: "artifact:capture-1",
    objectType: "raster-artifact",
    itemId: "book-1",
    id: "capture-1",
    revision: "index:abc123",
  };
  const hydrated = { ...hint, revision: "capture-r9" };
  assert.equal(navigationOnlyTarget(hint), true);
  assert.equal(navigationOnlyTarget(hydrated), false);

  const published = [];
  let current = null;
  const shell = Object.create(CorrectionsShell.prototype);
  shell.classificationController = {
    stateSnapshot: () => ({ selectionTarget: current }),
    setSelectionTarget(target, detail) {
      current = target;
      published.push({ target, detail });
      return target;
    },
  };
  const hintEcho = {
    source: "selection",
    navigationHint: true,
    address: { itemId: "book-1", artifactId: "capture-1" },
  };

  shell.publishClassificationSelectionTarget(null, hintEcho);
  shell.publishClassificationSelectionTarget(hydrated, { source: "artifacts" });
  shell.publishClassificationSelectionTarget(null, hintEcho);

  assert.equal(current, hydrated);
  assert.deepEqual(published.map((entry) => entry.target), [null, hydrated],
    "the Books echo is ignored only after matching artifact detail hydrates");

  shell.publishClassificationSelectionTarget(null, {
    ...hintEcho,
    address: { itemId: "book-1", artifactId: "capture-2" },
  });
  assert.equal(current, null,
    "a different hinted capture still clears the previous target");
});


test("artifact selection finishes with authoritative non-Books targets", () => {
  const raster = {
    key: "artifact:processed-1",
    objectType: "raster-artifact",
    itemId: "book-1",
    id: "processed-1",
    revision: "processed-r1",
    source: { representationId: "primary", canvasId: "page-1" },
  };
  const annotation = {
    key: "annotation:region-1",
    objectType: "spatial-annotation",
    itemId: "book-1",
    id: "region-1",
    revision: "region-r1",
    source: { representationId: "primary", canvasId: "page-1" },
  };
  let current = null;
  const published = [];
  const shell = Object.create(CorrectionsShell.prototype);
  shell.state = {
    selection: {
      itemId: "book-1",
      representationId: "primary",
      canvasId: "page-1",
      artifactId: null,
      annotationId: null,
    },
  };
  shell.artifactTreeElement = () => null;
  shell.classificationController = {
    stateSnapshot: () => ({ selectionTarget: current }),
    setSelectionTarget(target) {
      current = target;
      published.push(target);
      return target;
    },
  };
  shell.selectAddress = (address) => {
    shell.state.selection = address;
    // Model the Books facade echo for a target absent from the capture index.
    shell.publishClassificationSelectionTarget(null, {
      source: "selection",
      navigationHint: false,
      address,
    });
    return address;
  };

  shell.selectArtifactItem(raster);
  assert.equal(current, raster);
  shell.selectArtifactItem(annotation);
  assert.equal(current, annotation);
  assert.deepEqual(published, [null, raster, null, annotation]);
});


test("later Books null echoes preserve same-address authoritative targets", () => {
  const raster = {
    key: "artifact:processed-1",
    objectType: "raster-artifact",
    itemId: "book-1",
    id: "processed-1",
    revision: "processed-r1",
  };
  const annotation = {
    key: "annotation:region-1",
    objectType: "spatial-annotation",
    itemId: "book-1",
    id: "region-1",
    revision: "region-r1",
  };
  let current = null;
  const published = [];
  const shell = Object.create(CorrectionsShell.prototype);
  shell.classificationController = {
    stateSnapshot: () => ({ selectionTarget: current }),
    setSelectionTarget(target, detail) {
      current = target;
      published.push({ target, detail });
      return target;
    },
  };

  shell.publishClassificationSelectionTarget(raster, { source: "artifacts" });
  shell.publishClassificationSelectionTarget(null, {
    source: "refresh",
    navigationHint: false,
    address: { itemId: "book-1", artifactId: "processed-1" },
  });
  assert.equal(current, raster,
    "a delayed Books refresh cannot clear the selected processed image");

  shell.publishClassificationSelectionTarget(annotation, { source: "artifacts" });
  shell.publishClassificationSelectionTarget(null, {
    source: "context",
    navigationHint: false,
    address: { itemId: "book-1", annotationId: "region-1" },
  });
  assert.equal(current, annotation,
    "a slower Books context load cannot clear the selected annotation");
  assert.deepEqual(published.map((entry) => entry.target), [raster, annotation]);

  shell.publishClassificationSelectionTarget(null, {
    source: "selection",
    navigationHint: false,
    address: { itemId: "book-1", artifactId: "processed-2" },
  });
  assert.equal(current, null,
    "a different object address still clears the previous target");
});


test("overlay blur demotes classification focus without erasing its selected target", () => {
  const shell = Object.create(CorrectionsShell.prototype);
  const target = { key: "annotation:box-1" };
  const calls = [];
  shell.classificationController = {
    setSelectionFocus(value) { calls.push(value); },
  };
  shell.demoteClassificationFocus();
  assert.deepEqual(calls, [false]);

  let retained = null;
  shell.classificationController = {
    stateSnapshot: () => ({ selectionTarget: target }),
    setSelectionTarget(value, options) { retained = { value, options }; },
  };
  shell.demoteClassificationFocus();
  assert.equal(retained.value, target);
  assert.equal(retained.options.focused, false);

  shell.classificationController = { mount() {} };
  shell.root = { querySelector: () => ({}) };
  assert.doesNotThrow(() => shell.mountClassificationControls(),
    "partial injected controllers must not crash the workbench");
});


test("item and artifact switches drop the classification hot target", () => {
  const shell = Object.create(CorrectionsShell.prototype);
  const hotCalls = [];
  Object.assign(shell, {
    state: new CorrectionsWindowState(),
    selectionListeners: new Set(),
    booksFeature: null,
    itemProperties: null,
    ocrProposalsFeature: null,
    chPanelFeature: null,
    artifactsFeature: null,
    classificationController: {
      setHotTarget(target) { hotCalls.push(target); },
    },
  });

  shell.selectAddress({ itemId: "book-1", artifactId: "capture-1" });
  assert.deepEqual(hotCalls, [null],
    "selecting an item clears any hover left by the previous surfaces");

  shell.selectAddress({
    itemId: "book-1",
    artifactId: "capture-1",
    annotationId: "region-1",
  });
  assert.deepEqual(hotCalls, [null],
    "an annotation-only change keeps the still-visible hover");

  shell.selectAddress({
    itemId: "book-1",
    artifactId: "capture-2",
    annotationId: null,
  });
  assert.deepEqual(hotCalls, [null, null],
    "an artifact switch clears the hover; its regions are gone");

  shell.selectAddress({
    itemId: "book-2",
    artifactId: null,
    annotationId: null,
  });
  assert.deepEqual(hotCalls, [null, null, null],
    "an item switch clears the hover");
});


test("editor overlay teardown clears a hovered classification target", () => {
  const shell = Object.create(CorrectionsShell.prototype);
  const documentRef = fakeDocument();
  const stage = new FakeNode("div", documentRef);
  const image = new FakeNode("img", documentRef);
  stage.append(image);
  const hot = [];
  Object.assign(shell, {
    documentRef,
    windowRef: {},
    classificationController: {
      setHotTarget(target, detail) { hot.push({ target, detail }); },
      setSelectionTarget() {},
    },
  });
  const resource = {
    summary: { itemId: "book-1" },
    coordinateSpace: "canvas-normalized",
    regions: [{
      annotation_id: "region-1",
      object_type: "spatial-annotation",
      revision: "region-r1",
      selector: {
        coordinate_space: "canvas-normalized",
        points: [
          { x: 0.1, y: 0.1 }, { x: 0.5, y: 0.1 }, { x: 0.5, y: 0.4 },
        ],
      },
    }],
    dimensions: { width: 100, height: 100 },
  };

  const cleanup = shell.mountArtifactOverlay({ image }, resource);
  assert.equal(typeof cleanup, "function");
  const marker = stage.querySelector(".corrections-artifact-overlay-shape");
  assert.ok(marker, "the overlay renders the region marker");
  marker.emit("pointerenter");
  assert.equal(hot.at(-1).target.key, "annotation:region-1");
  assert.equal(hot.at(-1).detail.source, "editor-overlay");

  cleanup();
  assert.equal(hot.at(-1).target, null,
    "teardown withdraws the hovered region as a command target");
  assert.equal(stage.querySelector("[data-overlay-key]"), null,
    "teardown removes the overlay layer");
});


test("category apply, undo, and conflict refresh expanded artifacts once", async () => {
  const artifactRefreshes = [];
  const detailRefreshes = [];
  const bookRefreshes = [];
  const source = {
    key: "artifact:capture-1",
    group: "source-images",
    revision: "capture-r1",
  };
  const shell = Object.create(CorrectionsShell.prototype);
  Object.assign(shell, {
    artifactsFeature: {
      items: new Map([[source.key, source]]),
      async refresh(options) {
        artifactRefreshes.push(options);
      },
      async reloadDetail(key) {
        detailRefreshes.push(key);
        return this.items.get(key) || null;
      },
    },
    booksFeature: {
      async refresh(reason) { bookRefreshes.push(reason); },
    },
    setStatus() {},
  });
  const syntheticTarget = {
    key: source.key,
    objectType: "raster-artifact",
    family: "image",
  };

  await shell.refreshClassificationTarget(syntheticTarget, {
    command: {
      id: "corrections.category.cover",
      action: "category.assign",
    },
    reason: "committed",
  });
  await shell.refreshClassificationTarget(syntheticTarget, {
    command: {
      id: "corrections.category.cover.undo",
      action: "inverse.execute",
    },
    undo: { commandId: "corrections.category.cover" },
    reason: "undo-committed",
  });
  await shell.refreshClassificationTarget(syntheticTarget, {
    command: {
      id: "corrections.category.cover",
      action: "category.assign",
    },
    reason: "conflict",
  });

  assert.deepEqual(artifactRefreshes, [
    { preserveSelection: true, reason: "category-inheritance" },
    { preserveSelection: true, reason: "category-inheritance" },
    { preserveSelection: true, reason: "category-inheritance" },
  ]);
  assert.deepEqual(detailRefreshes, []);
  assert.deepEqual(bookRefreshes, [
    "classification",
    "classification",
    "classification",
  ]);
});


test("multi-target role convergence reloads exact details without collection storms", async () => {
  const artifactRefreshes = [];
  const detailRefreshes = [];
  const bookRefreshes = [];
  const shell = Object.create(CorrectionsShell.prototype);
  Object.assign(shell, {
    artifactsFeature: {
      items: new Map(),
      async refresh(options) { artifactRefreshes.push(options); },
      async reloadDetail(key) {
        detailRefreshes.push(key);
        return { key };
      },
    },
    booksFeature: {
      async refresh(reason) { bookRefreshes.push(reason); },
    },
    setStatus() {},
  });
  const detail = {
    command: {
      id: "corrections.region.illustration",
      action: "role.assign",
    },
    reason: "committed",
  };

  await shell.refreshClassificationTarget({
    key: "annotation:region-1",
    group: "layout-regions",
  }, detail);
  await shell.refreshClassificationTarget({
    key: "artifact:figure-1",
    group: "extracted-figures",
  }, detail);

  assert.deepEqual(detailRefreshes, [
    "annotation:region-1",
    "artifact:figure-1",
  ]);
  assert.deepEqual(artifactRefreshes, []);
  assert.deepEqual(bookRefreshes, []);
});


test("selection, resources, and drafts remain independent per window instance", () => {
  const first = new CorrectionsWindowState();
  const second = new CorrectionsWindowState();
  first.applyContext(context({ artifact_id: "figure-1", annotation_id: "box-3" }));
  second.applyContext(context({ item_id: "book-2", representation_id: "scan-9" }));
  first.setDraft("figure-1:caption", { value: "Medicinal sage" });
  first.setResource({ id: "figure-1", metadata: { caption: "Sage" } });

  assert.equal(first.snapshot().selection.artifactId, "figure-1");
  assert.equal(second.snapshot().selection.itemId, "book-2");
  assert.equal(second.getDraft("figure-1:caption"), undefined);
  assert.deepEqual(first.getDraft("figure-1:caption"), { value: "Medicinal sage" });

  const snapshot = first.snapshot();
  snapshot.resource.metadata.caption = "Changed outside";
  assert.equal(first.snapshot().resource.metadata.caption, "Sage");
  first.applyContext(context({ artifact_id: "figure-2" }));
  assert.deepEqual(first.getDraft("figure-1:caption"), { value: "Medicinal sage" });
});


test("window activation refreshes shared corrections state once", async () => {
  const windowRef = new FakeEventTarget();
  const documentRef = new FakeEventTarget();
  documentRef.visibilityState = "visible";
  let releaseBooks;
  const calls = [];
  const shell = Object.create(CorrectionsShell.prototype);
  Object.assign(shell, {
    windowRef,
    documentRef,
    listeners: [],
    destroyed: false,
    externalRefreshPromise: null,
    booksFeature: {
      refresh(reason) {
        calls.push(["books", reason]);
        return new Promise((resolve) => { releaseBooks = resolve; });
      },
    },
    artifactsFeature: {
      refresh(options) {
        calls.push(["artifacts", options]);
        return Promise.resolve();
      },
    },
    itemProperties: {
      refresh(reason) {
        calls.push(["metadata", reason]);
        return Promise.resolve();
      },
    },
  });
  shell.bindExternalRefresh();

  windowRef.emit("focus");
  documentRef.emit("visibilitychange");
  await Promise.resolve();
  assert.deepEqual(calls, [
    ["books", "window-focus"],
    ["artifacts", {
      preserveSelection: true,
      reason: "window-focus",
    }],
    ["metadata", "window-focus"],
  ], "focus and visibility events share one in-flight refresh");
  releaseBooks();
  await shell.externalRefreshPromise;

  documentRef.visibilityState = "hidden";
  documentRef.emit("visibilitychange");
  assert.equal(calls.length, 3);

  documentRef.visibilityState = "visible";
  documentRef.emit("visibilitychange");
  await Promise.resolve();
  releaseBooks();
  await shell.externalRefreshPromise;
  assert.deepEqual(calls.slice(3), [
    ["books", "window-visible"],
    ["artifacts", {
      preserveSelection: true,
      reason: "window-visible",
    }],
    ["metadata", "window-visible"],
  ]);
});

test("external index convergence refreshes selected detail panels without a second Books load",
  async () => {
    let releaseArtifacts;
    const calls = [];
    const shell = Object.create(CorrectionsShell.prototype);
    Object.assign(shell, {
      destroyed: false,
      externalRefreshPromise: null,
      booksFeature: {
        refresh(reason) {
          calls.push(["books", reason]);
          return Promise.resolve();
        },
      },
      artifactsFeature: {
        refresh(options) {
          calls.push(["artifacts", options]);
          return new Promise((resolve) => { releaseArtifacts = resolve; });
        },
      },
      itemProperties: {
        refresh(reason) {
          calls.push(["metadata", reason]);
          return Promise.resolve();
        },
      },
    });

    const first = shell.refreshExternalState("external-change", {
      includeBooks: false,
    });
    const coalesced = shell.refreshExternalState("external-change", {
      includeBooks: false,
    });
    await Promise.resolve();

    assert.strictEqual(coalesced, first);
    assert.deepEqual(calls, [
      ["artifacts", {
        preserveSelection: true,
        reason: "external-change",
      }],
      ["metadata", "external-change"],
    ]);
    releaseArtifacts();
    await first;
    assert.equal(shell.externalRefreshPromise, null);
  });


test("cross-panel selection addresses retain context without carrying stale object IDs", () => {
  const prior = normalizeSelection({
    itemId: "book-1",
    representationId: "scan-1",
    canvasId: "page-1",
    artifactId: "capture-1",
    annotationId: null,
  });
  const annotation = artifactSelection({
    id: "region-2",
    key: "annotation:region-2",
    itemId: "book-1",
    objectType: "spatial-annotation",
    source: { representationId: "scan-1", canvasId: "page-2" },
  }, prior);
  assert.deepEqual(annotation, {
    itemId: "book-1",
    representationId: "scan-1",
    canvasId: "page-2",
    artifactId: null,
    annotationId: "region-2",
  });

  const merged = selectionContext(context({ artifact_id: "capture-1" }), annotation);
  assert.equal(merged.canvas_id, "page-2");
  assert.equal(merged.annotation_id, "region-2");
  assert.equal(Object.hasOwn(merged, "artifact_id"), false);

  const transform = artifactSelection({
    id: "transform-4",
    key: "transform:transform-4",
    itemId: "book-1",
    objectType: "transform",
  }, prior);
  assert.equal(transform.artifactId, null);
  assert.equal(transform.annotationId, null);
});


test("Books navigation previews are forwarded only into the artifact context", async () => {
  const artifactContexts = [];
  const state = new CorrectionsWindowState();
  state.applyContext(context());
  const shell = Object.create(CorrectionsShell.prototype);
  Object.assign(shell, {
    artifactsFeature: {
      setContext(value) {
        artifactContexts.push(value);
        return Promise.resolve();
      },
    },
    destroyed: false,
    root: { querySelector() { return null; } },
    selectionListeners: new Set(),
    setStatus() {},
    state,
  });
  const navigationPreview = Object.freeze({
    itemId: "book-1",
    representationId: "scan-1",
    canvasId: "page-1",
    artifactId: "capture-1",
    url: "/thumb/capture-1.jpg",
    label: "Capture 1",
  });

  shell.selectAddress({
    itemId: "book-1",
    representationId: "scan-1",
    canvasId: "page-1",
    artifactId: "capture-1",
    annotationId: null,
  }, {
    source: "books",
    targetKind: "image",
    navigationPreview,
  });
  await Promise.resolve();
  await Promise.resolve();

  assert.deepEqual(artifactContexts.at(-1).navigationPreview, navigationPreview);
  assert.equal(Object.hasOwn(state.context, "navigationPreview"), false,
    "temporary display metadata must not enter durable window context");
});


test("invalidated feature selection clears every object address without losing drafts", async () => {
  const bookSelections = [];
  const artifactContexts = [];
  const state = new CorrectionsWindowState();
  state.applyContext(context({ artifact_id: "capture-1" }));
  state.setDraft("caption:capture-1", { text: "keep me" });
  const shell = Object.create(CorrectionsShell.prototype);
  Object.assign(shell, {
    artifactsFeature: {
      setContext(value) {
        artifactContexts.push(value);
        return Promise.resolve();
      },
    },
    booksFeature: {
      setSelection(value) { bookSelections.push(value); },
    },
    destroyed: false,
    root: { querySelector() { return null; } },
    selectionListeners: new Set(),
    setResource(value) { state.setResource(value); },
    setStatus() {},
    state,
  });

  shell.clearSelection();
  await Promise.resolve();
  await Promise.resolve();

  assert.deepEqual(state.selection, {
    itemId: null,
    representationId: null,
    canvasId: null,
    artifactId: null,
    annotationId: null,
  });
  assert.equal(bookSelections.at(-1), null);
  assert.equal(artifactContexts.at(-1).item_id, undefined);
  assert.deepEqual(state.getDraft("caption:capture-1"), { text: "keep me" });
});


test("Corrections context validation is canonical and matches the desktop contract", () => {
  const normalized = normalizeWorkbenchContext(context({
    canvas_id: "folio:1r",
    resource_revision: 3,
    view_hint: { label: "constructor", editor_type: "image-overlay" },
    origin: { id: "attention-2", kind: "attention-item" },
  }));
  assert.equal(normalized.ui_profile_key, "corrections/default");
  assert.deepEqual(Object.keys(normalized.view_hint), ["editor_type", "label"]);
  assert.equal(normalized.view_hint.label, "constructor");

  for (const invalid of [
    {},
    context({ schema: "librarytool.workbench-context/2" }),
    context({ workbench_id: "replica" }),
    context({ workspace_id: "../workspace" }),
    context({ local_path: "C:/private/page.jpg" }),
    context({ ui_profile_key: "corrections/../other" }),
    context({ view_hint: JSON.parse('{"constructor":"blocked-key"}') }),
    context({ view_hint: { value: Number.NaN } }),
  ]) {
    assert.throws(() => normalizeWorkbenchContext(invalid), TypeError);
  }
});


test("standalone runtime uses engine artifact ports while desktop remains preferred", () => {
  const engineClient = {
    rasterArtifacts: {
      list() {},
      get() {},
      resourceUrl() {},
    },
    spatialAnnotations: {
      list() {},
      get() {},
    },
    corrections: {
      queueTransform() {},
      index() {},
      getReview() {},
      listReviewHistory() {},
      resolveCorrections() {},
      reopenCorrections() {},
    },
    jobs: {
      get() {},
    },
    processingPresets: {
      list() {},
      create() {},
      update() {},
      remove() {},
    },
  };
  const standalone = correctionsRuntimePorts({ engineClient }, null);
  assert.equal(typeof standalone.artifacts.catalog.list, "function");
  assert.equal(typeof standalone.artifacts.resources.resolveRaster, "function");
  assert.equal(typeof standalone.invokeCommand, "function");
  assert.equal(typeof standalone.books.loadIndex, "function");
  assert.equal(typeof standalone.books.getReview, "function");
  assert.equal(typeof standalone.books.resolveReview, "function");
  assert.equal(typeof standalone.books.reopenReview, "function");
  assert.equal(typeof standalone.books.subscribe, "function");
  assert.equal(typeof standalone.transforms.subscribeResults, "function");
  assert.equal(typeof standalone.processingPresets.list, "function");
  assert.equal(standalone.books.trustedActor, true);

  const desktopCorrections = { artifacts: { catalog: { list() {} } } };
  assert.equal(
    correctionsRuntimePorts({ engineClient }, desktopCorrections),
    null,
    "the authenticated desktop bridge remains authoritative when present",
  );
  assert.equal(correctionsRuntimePorts({}, null), null);
});


test("standalone shell mounts presets from the production EngineClient port", async () => {
  const operation = {
    schema: "org.whl.raster.processing-operation",
    version: 1,
    algorithm: "gamma-v1",
    rule: "round_half_up(255 * (value/255) ** (100/gamma_hundredths)), clamped_0_255",
    gamma_hundredths: 120,
  };
  const preset = {
    schema: "org.whl.processing-preset",
    version: 1,
    preset_id: "gamma-only",
    name: "Gamma only",
    category: "cover",
    operations: [operation],
    adjustment: null,
    revision: "a".repeat(64),
  };
  const calls = [];
  const response = (status, body) => ({
    status,
    ok: status >= 200 && status < 300,
    json: async () => body,
  });
  const engineClient = new EngineClient({
    baseUrl: "/api",
    transport: async (url, init) => {
      calls.push({ url, init });
      if (init.method === "GET" && url === "/api/v1/processing-presets") {
        return response(200, {
          ok: true,
          schema: "librarytool.processing-presets/1",
          presets: [preset],
          revision: "preset-list-r1",
        });
      }
      throw new Error(`unexpected transport: ${init.method} ${url}`);
    },
  });
  const documentRef = fakeDocument();
  const windowRef = {
    engineClient,
    localStorage: new MemoryStorage(),
    addEventListener() {},
    removeEventListener() {},
  };
  documentRef.defaultView = windowRef;
  documentRef.querySelector = () => null;
  const rootElement = new FakeNode("div", documentRef);
  const shell = new CorrectionsShell({
    root: rootElement,
    documentRef,
    windowRef,
    layoutController: {
      getState: () => ({ ...DEFAULT_LAYOUT }),
      replaceState() {},
      destroy() {},
    },
    features: false,
    booksFeature: false,
    artifactsFeature: false,
    itemProperties: false,
    ocrProposalsFeature: false,
    chPanelFeature: false,
  });
  const container = new FakeNode("div", documentRef);
  shell.editorRegistry.setResource({
    id: "capture-7",
    kind: "captured-image",
    media_type: "image/jpeg",
    url: "/api/v1/items/book-1/raster-artifacts/capture-7/resource",
    correction: {
      item_id: "book-1",
      artifact_id: "capture-7",
      artifact_revision: "artifact-r3",
      source_revision: "source-r17",
      source_sha256: "b".repeat(64),
      proposal: null,
    },
  });
  shell.editorRegistry.render(container);
  await new Promise((resolve) => setImmediate(resolve));

  const panel = container.querySelector(".preset-panel");
  assert.ok(panel, "the shell-created image tool mounts the preset panel");
  assert.equal(calls[0].url, "/api/v1/processing-presets");
  const chooser = panel.querySelector(".preset-chooser");
  chooser.value = "gamma-only";
  chooser.emit("change");
  panel.querySelector(".preset-apply").emit("click");

  const controller = shell.imageAdjustTool.mountRecord.controller;
  assert.equal(controller.getState().tool, "image-adjust",
    "Apply activates the tool whose recipe will be queued");
  assert.deepEqual(controller.getState().operations, [operation]);
  assert.equal(shell.imageAdjustTool.getAdjustment({
    state: controller.getState(),
  }), null, "an operations-only preset stays operations-only");
  shell.destroy();
});


test("mask drafts own classification keys and editor replacement releases them", () => {
  const documentRef = fakeDocument();
  const windowRef = {
    localStorage: new MemoryStorage(),
    addEventListener() {},
    removeEventListener() {},
  };
  documentRef.defaultView = windowRef;
  const rootElement = new FakeNode("div", documentRef);
  const owners = [];
  const shell = new CorrectionsShell({
    root: rootElement,
    documentRef,
    windowRef,
    classificationController: {
      setCanvasOwner(owner) { owners.push(owner); },
      destroy() {},
    },
    layoutController: {
      getState: () => ({ ...DEFAULT_LAYOUT }),
      replaceState() {},
      destroy() {},
    },
    features: false,
    booksFeature: false,
    artifactsFeature: false,
    itemProperties: false,
    ocrProposalsFeature: false,
    chPanelFeature: false,
  });
  const container = new FakeNode("div", documentRef);
  shell.editorRegistry.setResource({
    id: "capture-mask",
    kind: "captured-image",
    media_type: "image/jpeg",
    url: "/capture-mask.jpg",
    correction: {
      item_id: "book-1",
      artifact_id: "capture-mask",
      artifact_revision: "artifact-r1",
      source_revision: "source-r1",
      source_sha256: "a".repeat(64),
      proposal: null,
    },
  });
  shell.editorRegistry.render(container);
  const canvas = container.querySelector("[data-classification-canvas]");
  canvas.getBoundingClientRect = () => ({
    left: 0, top: 0, width: 400, height: 400,
  });
  canvas.setPointerCapture = () => {};
  container.querySelector("[data-image-tool='polygon']").emit("click");
  canvas.emit("pointerdown", {
    pointerId: 7,
    button: 0,
    clientX: 40,
    clientY: 40,
  });

  assert.deepEqual(owners.at(-1), {
    active: true,
    tool: "polygon",
    ownsKeyboard: true,
  });

  shell.editorRegistry.setResource({
    id: "metadata-note",
    kind: "unknown",
    media_type: "application/octet-stream",
  });
  shell.editorRegistry.render(container);
  assert.equal(owners.at(-1), null,
    "disposing the drafted image editor clears canvas ownership");
  shell.destroy();
});


test("batch presets hydrate every category target before building unique commands", async () => {
  const operation = {
    schema: "org.whl.raster.processing-operation",
    version: 1,
    algorithm: "gamma-v1",
    rule: "round_half_up(255 * (value/255) ** (100/gamma_hundredths)), clamped_0_255",
    gamma_hundredths: 120,
  };
  const preset = {
    preset_id: "cover-gamma",
    category: "cover",
    operations: [operation],
    adjustment: null,
    revision: "a".repeat(64),
  };
  const captures = [
    { artifact_id: "cover-1", effective_category: "cover" },
  ];
  const index = {
    revision: "index-r2",
    books: [{ id: "book-1", captures }],
  };
  const pages = new Map([
    [null, {
      revision: "source-inventory-r3",
      items: [
        { key: { artifact_id: "cover-1" } },
        { key: { artifact_id: "pdf-scan-2" }, kind: "scan-page" },
      ],
      nextCursor: "source-page-2",
    }],
    ["source-page-2", {
      revision: "source-inventory-r3",
      items: [
        { key: { artifact_id: "pdf-scan-2" }, kind: "scan-page" },
        { key: { artifact_id: "summary-spine-detail-cover" } },
        { key: { artifact_id: "summary-cover-detail-spine" } },
      ],
      nextCursor: null,
    }],
  ]);
  const hydrated = [];
  const listed = [];
  const invoked = [];
  let refreshes = 0;
  const shell = Object.create(CorrectionsShell.prototype);
  Object.assign(shell, {
    booksFeature: {
      store: {
        snapshot: () => ({
          status: "ready",
          workspaceId: "workspace-1",
          index,
        }),
      },
      async refresh(reason) {
        assert.equal(reason, "preset-batch");
        refreshes += 1;
        return index;
      },
    },
    engineCorrections: {
      artifacts: {
        catalog: {
          async list(args) {
            listed.push(args);
            return pages.get(args.cursor);
          },
          async get({ context: targetContext, key }) {
            const artifactId = key.slice("artifact:".length);
            hydrated.push({ artifactId, targetContext });
            const sequence = {
              "cover-1": 1,
              "pdf-scan-2": 2,
              "summary-spine-detail-cover": 3,
              "summary-cover-detail-spine": 4,
            }[artifactId];
            const sourceRevision = `source-r${sequence}`;
            return {
              id: artifactId,
              resource_state: "available",
              effective_category: artifactId === "summary-cover-detail-spine"
                ? "spine" : "cover",
              correction: {
                item_id: "book-1",
                artifact_id: artifactId,
                artifact_revision: `artifact-r${sequence}`,
                source_revision: sourceRevision,
                source_sha256: String(sequence).repeat(64),
                proposal: {
                  schema: "org.whl.page-boundary-proposal",
                  version: 1,
                  coordinate_space: "exif_oriented_normalized",
                  point_order: ["top_left", "top_right", "bottom_right", "bottom_left"],
                  quad: [[0.1 * sequence, 0.1], [0.9, 0.1], [0.9, 0.9], [0.1, 0.9]],
                  confidence: 0.9,
                  detector: "batch-test",
                  detector_version: "1",
                  source_revision: sourceRevision,
                },
              },
            };
          },
        },
      },
    },
    async invokeCommand(commandId, payload) {
      invoked.push({ commandId, payload });
      return { job_id: `job-${invoked.length}` };
    },
    presetBatchOperationIdFactory: ({ sequence }) => `preset:batch-${sequence}`,
    presetBatchSequence: 0,
    presetBatchRetryCommands: new Map(),
    presetBatchRuns: new Map(),
    contextGeneration: 7,
    destroyed: false,
    state: {
      context: context({
        canvas_id: "page-9",
        artifact_id: "cover-1",
      }),
      selection: { itemId: "book-1" },
    },
    windowRef: null,
  });

  const outcome = await shell.batchApplyProcessingPreset(preset, {
    resource: {
      correction: {
        item_id: "book-1",
        artifact_id: "cover-1",
        artifact_revision: "artifact-editor-r1",
        source_revision: "source-editor-r1",
        source_sha256: "e".repeat(64),
      },
    },
  });

  assert.deepEqual(outcome, { queued: 3, failed: 0 });
  assert.equal(refreshes, 1);
  assert.deepEqual(listed.map((entry) => entry.cursor), [null, "source-page-2"]);
  assert.ok(listed.every((entry) =>
    entry.group === "source-images" && entry.limit === 100 &&
    entry.context.item_id === "book-1" &&
    !Object.hasOwn(entry.context, "representation_id") &&
    !Object.hasOwn(entry.context, "canvas_id") &&
    !Object.hasOwn(entry.context, "artifact_id")),
  "the catalog is paged with a book-scoped invocation context");
  assert.deepEqual(hydrated.map((entry) => entry.artifactId), [
    "cover-1",
    "pdf-scan-2",
    "summary-spine-detail-cover",
    "summary-cover-detail-spine",
  ]);
  assert.ok(hydrated.every((entry) => entry.targetContext.item_id === "book-1"));
  assert.deepEqual(invoked.map((entry) => entry.commandId), [
    "corrections.transform.queue",
    "corrections.transform.queue",
    "corrections.transform.queue",
  ]);
  assert.deepEqual(invoked.map((entry) => entry.payload.command.operation_id), [
    "preset:batch-1", "preset:batch-2", "preset:batch-3",
  ]);
  assert.deepEqual(invoked.map((entry) => entry.payload.command.artifact_id), [
    "cover-1", "pdf-scan-2", "summary-spine-detail-cover",
  ]);
  assert.deepEqual(invoked.map((entry) => entry.payload.command.artifact_revision), [
    "artifact-r1", "artifact-r2", "artifact-r3",
  ]);
  assert.deepEqual(invoked[0].payload.command.quad,
    [[0.1, 0.1], [0.9, 0.1], [0.9, 0.9], [0.1, 0.9]]);
  assert.deepEqual(invoked[1].payload.command.quad,
    [[0.2, 0.1], [0.9, 0.1], [0.9, 0.9], [0.1, 0.9]]);
  assert.equal(invoked[1].payload.resource.id, "pdf-scan-2",
    "a source PDF scan absent from phone capture hints is included");
  assert.ok(invoked.every((entry) =>
    entry.payload.trigger === "preset-batch" &&
    entry.payload.command.adjustment === null));
});


function singleTargetBatchHarness(options = {}) {
  const artifactId = options.artifactId || "cover-1";
  const index = {
    revision: "index-r1",
    books: [{ id: "book-1", captures: [] }],
  };
  let snapshot = {
    status: "ready",
    workspaceId: "workspace-1",
    index,
  };
  const calls = { refresh: [], list: [], get: [], invoke: [] };
  const shell = Object.create(CorrectionsShell.prototype);
  const resource = (revision = "1") => ({
    id: artifactId,
    resource_state: "available",
    effective_category: "cover",
    correction: {
      item_id: "book-1",
      artifact_id: artifactId,
      artifact_revision: `artifact-r${revision}`,
      source_revision: `source-r${revision}`,
      source_sha256: String(revision).repeat(64),
      proposal: null,
    },
  });
  Object.assign(shell, {
    booksFeature: {
      store: { snapshot: () => snapshot },
      async refresh(reason) {
        calls.refresh.push(reason);
        const result = options.refresh
          ? await options.refresh({ index, shell, snapshot }) : index;
        snapshot = result ? {
          status: "ready",
          workspaceId: "workspace-1",
          index: result,
        } : { ...snapshot, status: "error" };
        return result;
      },
    },
    engineCorrections: { artifacts: { catalog: {
      async list(args) {
        calls.list.push(args);
        return options.list ? options.list(args, calls.list.length) : {
          revision: "source-inventory-r1",
          items: [{ key: { artifact_id: artifactId } }],
          nextCursor: null,
        };
      },
      async get(args) {
        calls.get.push(args);
        return options.get
          ? options.get(args, calls.get.length) : resource("1");
      },
    } } },
    async invokeCommand(commandId, payload) {
      calls.invoke.push({ commandId, payload });
      return options.invoke
        ? options.invoke({ commandId, payload }, calls.invoke.length)
        : { job_id: `job-${calls.invoke.length}` };
    },
    presetBatchOperationIdFactory: ({ sequence }) =>
      `preset:retry-${sequence}`,
    presetBatchSequence: 0,
    presetBatchRetryCommands: new Map(),
    presetBatchRuns: new Map(),
    contextGeneration: 3,
    destroyed: false,
    state: {
      context: context({ artifact_id: artifactId }),
      selection: { itemId: "book-1" },
    },
    windowRef: null,
  });
  const controller = {
    resource: { correction: {
      item_id: "book-1",
      artifact_id: artifactId,
      artifact_revision: "artifact-editor-r1",
      source_revision: "source-editor-r1",
      source_sha256: "e".repeat(64),
    } },
  };
  const preset = {
    schema: "org.whl.processing-preset",
    version: 1,
    preset_id: "cover-gamma",
    name: "Cover gamma",
    category: "cover",
    operations: [{
      schema: "org.whl.raster.processing-operation",
      version: 1,
      algorithm: "gamma-v1",
      rule: "round_half_up(255 * (value/255) ** (100/gamma_hundredths)), clamped_0_255",
      gamma_hundredths: 120,
    }],
    adjustment: null,
    revision: "a".repeat(64),
  };
  return { calls, controller, index, preset, resource, shell };
}


test("batch refresh failure rejects stale inventory before catalog reads", async () => {
  const harness = singleTargetBatchHarness({ refresh: async () => null });
  await assert.rejects(
    harness.shell.batchApplyProcessingPreset(
      harness.preset, harness.controller),
    /latest book inventory could not be loaded/i,
  );
  assert.deepEqual(harness.calls.refresh, ["preset-batch"]);
  assert.equal(harness.calls.list.length, 0);
  assert.equal(harness.calls.invoke.length, 0);
});


test("batch catalog paging fails closed on unstable or non-progressing pages", async () => {
  for (const scenario of ["revision", "cursor", "empty"]) {
    const harness = singleTargetBatchHarness({
      list(_args, attempt) {
        if (scenario === "empty") {
          return {
            revision: "source-r1",
            items: [],
            nextCursor: "next",
          };
        }
        return attempt === 1 ? {
          revision: "source-r1",
          items: [{ key: { artifact_id: "cover-1" } }],
          nextCursor: "next",
        } : {
          revision: scenario === "revision" ? "source-r2" : "source-r1",
          items: [{ key: { artifact_id: "cover-1" } }],
          nextCursor: scenario === "cursor" ? "next" : null,
        };
      },
    });
    await assert.rejects(
      harness.shell.batchApplyProcessingPreset(
        harness.preset, harness.controller),
      scenario === "revision" ? /inventory changed/i
        : scenario === "cursor" ? /invalid cursor/i
          : /empty continuation page/i,
      scenario,
    );
    assert.equal(harness.calls.invoke.length, 0, scenario);
  }
});


test("batch binds editor book before refresh and cancels selection drift", async () => {
  const mismatch = singleTargetBatchHarness();
  mismatch.controller.resource.correction.item_id = "book-2";
  await assert.rejects(
    mismatch.shell.batchApplyProcessingPreset(
      mismatch.preset, mismatch.controller),
    /different book/i,
  );
  assert.equal(mismatch.calls.refresh.length, 0,
    "editor/selection mismatch fails synchronously before any refresh");

  const drift = singleTargetBatchHarness({
    refresh: async ({ index, shell }) => {
      shell.state.selection.itemId = "book-2";
      return index;
    },
  });
  await assert.rejects(
    drift.shell.batchApplyProcessingPreset(drift.preset, drift.controller),
    (error) => error && error.code === "preset-batch-context-changed",
  );
  assert.equal(drift.calls.list.length, 0);
  assert.equal(drift.calls.invoke.length, 0);
});


test("concurrent remounted batch calls share one in-flight mutation", async () => {
  let releaseQueue;
  let markStarted;
  const started = new Promise((resolve) => { markStarted = resolve; });
  const queued = new Promise((resolve) => { releaseQueue = resolve; });
  const harness = singleTargetBatchHarness({
    invoke() {
      markStarted();
      return queued;
    },
  });

  const first = harness.shell.batchApplyProcessingPreset(
    harness.preset, harness.controller);
  await started;
  const remountedController = { resource: harness.controller.resource };
  const second = harness.shell.batchApplyProcessingPreset(
    harness.preset, remountedController);
  assert.equal(harness.calls.refresh.length, 1);
  assert.equal(harness.calls.get.length, 1);
  assert.equal(harness.calls.invoke.length, 1);

  releaseQueue({ job_id: "job-one-batch" });
  assert.deepEqual(await first, { queued: 1, failed: 0 });
  assert.deepEqual(await second, { queued: 1, failed: 0 });
  assert.equal(harness.calls.invoke.length, 1);
});


test("context drift after one target prevents every later mutation", async () => {
  const harness = singleTargetBatchHarness({
    list() {
      return {
        revision: "source-inventory-r1",
        items: [
          { key: { artifact_id: "cover-1" } },
          { key: { artifact_id: "cover-2" } },
        ],
        nextCursor: null,
      };
    },
    get({ key }) {
      const artifactId = key.slice("artifact:".length);
      const detail = harness.resource(artifactId.endsWith("1") ? "1" : "2");
      detail.id = artifactId;
      detail.correction.artifact_id = artifactId;
      return detail;
    },
    invoke() {
      harness.shell.state.selection.itemId = "book-2";
      return { job_id: "job-first-only" };
    },
  });

  await assert.rejects(
    harness.shell.batchApplyProcessingPreset(
      harness.preset, harness.controller),
    (error) => error && error.code === "preset-batch-context-changed",
  );
  assert.equal(harness.calls.get.length, 1);
  assert.equal(harness.calls.invoke.length, 1,
    "the second source image is never mutated after selection drift");
});


test("ambiguous batch retry replays the exact command without rehydrating", async () => {
  const harness = singleTargetBatchHarness({
    get(_args, attempt) {
      return attempt === 1
        ? harness.resource("1") : harness.resource("2");
    },
    invoke(_call, attempt) {
      if (attempt === 1) {
        const error = new Error("response lost");
        error.retryable = true;
        throw error;
      }
      return { job_id: "job-replayed" };
    },
  });

  assert.deepEqual(await harness.shell.batchApplyProcessingPreset(
    harness.preset, harness.controller), { queued: 0, failed: 1 });
  assert.deepEqual(await harness.shell.batchApplyProcessingPreset(
    harness.preset, harness.controller), { queued: 1, failed: 0 });

  assert.equal(harness.calls.get.length, 1,
    "retry uses the cached authoritative resource even if new pins would differ");
  assert.equal(harness.calls.invoke.length, 2);
  assert.equal(
    harness.calls.invoke[0].payload.command,
    harness.calls.invoke[1].payload.command,
    "the exact serialized command object is replayed",
  );
  assert.equal(
    harness.calls.invoke[0].payload.resource,
    harness.calls.invoke[1].payload.resource,
    "the authoritative resource bound to the uncertain command is replayed",
  );
  assert.equal(harness.calls.invoke[0].payload.command.operation_id,
    "preset:retry-1");
  assert.equal(harness.calls.invoke[1].payload.command.operation_id,
    "preset:retry-1");
  assert.equal(harness.shell.presetBatchRetryCommands.size, 0);
});


test("a truncated 202 batch response replays through EngineClient with one command",
  async () => {
    const harness = singleTargetBatchHarness();
    const adapterCommands = [];
    const wireCommands = [];
    let attempt = 0;
    const engineClient = new EngineClient({
      baseUrl: "/api",
      transport: async (url, init) => {
        assert.match(url, /\/v1\/items\/book-1\/raster-artifacts\/cover-1\/transforms$/);
        assert.equal(init.method, "POST");
        const command = JSON.parse(init.body);
        wireCommands.push(command);
        attempt += 1;
        if (attempt === 1) {
          return {
            ok: true,
            status: 202,
            json: async () => {
              throw new SyntaxError("truncated queue receipt");
            },
          };
        }
        const jobId = "correction-transform-batch-replay";
        return {
          ok: true,
          status: 200,
          json: async () => ({
            ok: true,
            schema: "librarytool.correction-transform-queue-receipt/1",
            replayed: true,
            operation_id: command.operation_id,
            job_id: jobId,
            job: {
              id: jobId,
              kind: "correction.transform",
              state: "queued",
              subject: {
                item_id: command.item_id,
                source_id: command.artifact_id,
              },
              progress: {
                completed: 0,
                total: 6,
                unit: "phase",
                phase: "queued",
              },
              cancellable: true,
              revision: 1,
              created_at: "2026-08-06T12:00:00+00:00",
              updated_at: "2026-08-06T12:00:00+00:00",
              finished_at: "",
              note: "",
              error: null,
              input_revisions: {
                artifact_id: command.artifact_id,
                artifact_revision: command.artifact_revision,
                source_revision: command.source_revision,
                source_sha256: command.source_sha256,
                operation_id: command.operation_id,
                transform: {
                  quad: command.quad,
                  adjustment: command.adjustment,
                  rerun_ocr: command.rerun_ocr,
                  ...(command.mask_polygon
                    ? { mask_polygon: command.mask_polygon }
                    : {}),
                  ...(command.operations
                    ? { operations: command.operations }
                    : {}),
                },
              },
              outputs: [],
            },
          }),
        };
      },
    });
    const runtime = correctionsRuntimePorts({ engineClient }, null);
    const invokeCommand = runtime.invokeCommand;
    harness.shell.invokeCommand = (commandId, payload) => {
      adapterCommands.push(payload.command);
      return invokeCommand(commandId, payload);
    };

    assert.deepEqual(await harness.shell.batchApplyProcessingPreset(
      harness.preset, harness.controller), { queued: 0, failed: 1 });
    assert.equal(harness.shell.presetBatchRetryCommands.size, 1);
    assert.deepEqual(await harness.shell.batchApplyProcessingPreset(
      harness.preset, harness.controller), { queued: 1, failed: 0 });

    assert.equal(harness.calls.get.length, 1,
      "the ambiguous response keeps the authoritative hydration cached");
    assert.equal(adapterCommands.length, 2);
    assert.equal(adapterCommands[0], adapterCommands[1],
      "the shell replays the same cached command object");
    assert.deepEqual(wireCommands[0], wireCommands[1]);
    assert.equal(wireCommands[0].operation_id, "preset:retry-1");
    assert.equal(harness.shell.presetBatchRetryCommands.size, 0);
  });


test("definitive batch failure discards retry state and mints a later id", async () => {
  const harness = singleTargetBatchHarness({
    invoke(_call, attempt) {
      if (attempt === 1) {
        const error = new Error("invalid request");
        error.retryable = false;
        throw error;
      }
      return { job_id: "job-new-command" };
    },
  });

  assert.deepEqual(await harness.shell.batchApplyProcessingPreset(
    harness.preset, harness.controller), { queued: 0, failed: 1 });
  assert.deepEqual(await harness.shell.batchApplyProcessingPreset(
    harness.preset, harness.controller), { queued: 1, failed: 0 });
  assert.equal(harness.calls.get.length, 2);
  assert.deepEqual(harness.calls.invoke.map((entry) =>
    entry.payload.command.operation_id), ["preset:retry-1", "preset:retry-2"]);
  assert.notEqual(harness.calls.invoke[0].payload.command,
    harness.calls.invoke[1].payload.command);
});


test("batch fingerprint accepts the full sixteen-operation preset contract", async () => {
  const harness = singleTargetBatchHarness();
  harness.preset.operations = Array.from({ length: 16 }, () => ({
    schema: "org.whl.raster.processing-operation",
    version: 1,
    algorithm: "white-balance-v1",
    rule: "gray_world_or_manual_channel_balance_v1",
    mode: "gray_world",
    strength_percent: 100,
    temperature: 0,
    tint: 0,
  }));

  assert.deepEqual(await harness.shell.batchApplyProcessingPreset(
    harness.preset, harness.controller), { queued: 1, failed: 0 });
  assert.equal(harness.calls.invoke[0].payload.command.operations.length, 16);
});

test("standalone shell resolves reviews through the real engine client adapter",
  async () => {
    let state = "needs_attention";
    let reviewRevision = "review-r1";
    let indexRevision = 1;
    const calls = [];
    const latestEvent = () => {
      if (state === "resolved") {
        return {
          operation_id: "shell-resolve-op",
          action: "attention.resolve",
          actor_id: "local-desktop",
          occurred_at: "2026-07-23T18:01:00Z",
          before_state: "needs_attention",
          after_state: "resolved",
          reason: "Check the title leaf",
          comment: "Verified",
        };
      }
      return {
        operation_id: "shell-mark-op",
        action: "attention.mark",
        actor_id: "local-desktop",
        occurred_at: "2026-07-23T18:00:00Z",
        before_state: "clear",
        after_state: "needs_attention",
        reason: "Check the title leaf",
        comment: "",
      };
    };
    const reviewSummary = () => ({
      revision: reviewRevision,
      state,
      reason: "Check the title leaf",
      history_count: state === "resolved" ? 2 : 1,
      latest_event: latestEvent(),
    });
    const indexBody = () => ({
      ok: true,
      schema: "librarytool.corrections-index/2",
      revision: `index-r${indexRevision}`,
      books: [{
        id: "book-1",
        revision: `book-r${indexRevision}`,
        kind: "book",
        title: "A Herbal",
        import_state: "ready",
        issues: [],
        review: reviewSummary(),
        captures: [],
      }],
      attention: [{
        key: "attention:book-1",
        target: { kind: "book", item_id: "book-1" },
        review: reviewSummary(),
      }],
    });
    // The tiers the engine actually publishes, projected from the same state
    // the /2 body reports, so this exercises the real routes end to end.
    const summaryBody = () => {
      const index = indexBody();
      return {
        ok: true,
        schema: "librarytool.corrections-index-summary/1",
        revision: `cri1-index-r${indexRevision}`,
        books: index.books.map((book) => ({
          id: book.id,
          revision: book.revision,
          kind: book.kind,
          title: book.title,
          review: book.review,
        })),
        attention: index.attention,
      };
    };
    const detailBody = (itemIds) => {
      const books = new Map(indexBody().books.map((book) => [book.id, book]));
      return {
        ok: true,
        schema: "librarytool.corrections-index-detail/1",
        revision: `crd-index-r${indexRevision}`,
        books: itemIds.filter((itemId) => books.has(itemId))
          .map((itemId) => books.get(itemId)),
        missing: itemIds.filter((itemId) => !books.has(itemId)),
      };
    };
    const response = (body) => ({
      ok: true,
      status: 200,
      json: async () => body,
    });
    const engineClient = new EngineClient({
      transport: async (url, init) => {
        calls.push({ url, init });
        if (url.startsWith("/api/v1/corrections/index/probe")) {
          return response({
            ok: true,
            schema: "librarytool.corrections-index-probe/1",
            revision: `cri1-index-r${indexRevision}`,
          });
        }
        if (url.startsWith("/api/v1/corrections/index/summary")) {
          return response(summaryBody());
        }
        if (url.startsWith("/api/v1/corrections/index/details")) {
          return response(detailBody(JSON.parse(init.body).item_ids));
        }
        if (url.startsWith("/api/v1/corrections/index")) {
          return response(indexBody());
        }
        if (url.endsWith("/corrections/review/resolve")) {
          const beforeReview = reviewRevision;
          state = "resolved";
          reviewRevision = "review-r2";
          indexRevision += 1;
          return response({
            ok: true,
            schema: "librarytool.correction-mutation-receipt/1",
            replayed: false,
            receipt: {
              action: "attention.resolve",
              operation_id: "shell-resolve-op",
              item_id: "book-1",
              before_aggregate_revision: "aggregate-r1",
              after_aggregate_revision: "aggregate-r2",
              targets: [{
                kind: "review",
                target_id: "book-1",
                before_revision: beforeReview,
                after_revision: reviewRevision,
              }],
              inverse: {
                action: "attention.reopen",
                expected_aggregate_revision: "aggregate-r2",
                expected_targets: [{
                  kind: "review",
                  target_id: "book-1",
                  before_revision: beforeReview,
                  after_revision: reviewRevision,
                }],
                payload: {
                  reason: "Check the title leaf",
                  append_audit: true,
                },
              },
            },
          });
        }
        throw new Error(`unexpected transport: ${init.method} ${url}`);
      },
    });
    const documentRef = miniDocument();
    const windowRef = {
      engineClient,
      localStorage: new MemoryStorage(),
    };
    documentRef.defaultView = windowRef;
    const rootElement = {
      dataset: {},
      ownerDocument: documentRef,
      querySelector: () => null,
      querySelectorAll: () => [],
    };
    const shell = new CorrectionsShell({
      root: rootElement,
      documentRef,
      windowRef,
      imageAdjustTool: createImageAdjustTool(),
      editorRegistry: createDefaultEditorRegistry({ documentRef }),
      layoutController: {
        getState: () => ({ ...DEFAULT_LAYOUT }),
        replaceState() {},
      },
      classificationController: false,
      booksFeature: false,
      artifactsFeature: false,
    });

    assert.equal(shell.booksApi, shell.engineCorrections.books);
    assert.equal(shell.booksApi.trustedActor, true);
    const store = new CorrectionsIndexStore({ api: shell.booksApi });
    await store.openWorkspace("workspace-1");
    const result = await store.transitionReview("resolve", {
      entry: store.index.attention[0],
      operationId: "shell-resolve-op",
      comment: "Verified",
    });

    assert.equal(result.entry.review.state, "resolved");
    assert.equal(store.index.books[0].review.state, "resolved");
    assert.deepEqual(calls.map(({ url, init }) => [
      init.method,
      url,
      init.body === undefined ? null : JSON.parse(init.body),
    ]), [
      // The probe is read before the summary, so what the two-second poll
      // compares is always a probe against a probe.
      [
        "GET",
        "/api/v1/corrections/index/probe?workspace_id=workspace-1",
        null,
      ],
      [
        "GET",
        "/api/v1/corrections/index/summary?workspace_id=workspace-1",
        null,
      ],
      [
        "POST",
        "/api/v1/items/book-1/corrections/review/resolve",
        { comment: "Verified" },
      ],
      // Convergence needs only the review, which the summary carries whole.
      [
        "GET",
        "/api/v1/corrections/index/summary?workspace_id=workspace-1",
        null,
      ],
    ]);
  });


test("late currentContext results cannot overwrite a newer pushed context", async () => {
  let pushContext;
  let resolveCurrent;
  const applied = [];
  const shell = Object.create(CorrectionsShell.prototype);
  shell.contextGeneration = 0;
  shell.desktop = { workbenches: {
    onContext(callback) {
      pushContext = callback;
      return () => { pushContext = null; };
    },
    currentContext() {
      return new Promise((resolve) => { resolveCurrent = resolve; });
    },
  } };
  shell.applyContextSafely = (value) => applied.push(value.item_id);
  shell.setStatus = () => {};

  const connecting = shell.connectDesktopContext();
  pushContext(context({ item_id: "newer-book" }));
  resolveCurrent(context({ item_id: "stale-book" }));
  await connecting;
  assert.deepEqual(applied, ["newer-book"]);
  assert.equal(typeof shell.unsubscribeContext, "function");
});

test("context delivery ignores stale failures and invalid pushes", async () => {
  let pushContext;
  let rejectCurrent;
  const applied = [];
  const statuses = [];
  const shell = Object.create(CorrectionsShell.prototype);
  shell.contextGeneration = 0;
  shell.destroyed = false;
  shell.desktop = { workbenches: {
    onContext(callback) {
      pushContext = callback;
      return () => { pushContext = null; };
    },
    currentContext() {
      return new Promise((_resolve, reject) => { rejectCurrent = reject; });
    },
  } };
  shell.applyContextSafely = (value) => {
    if (value.invalid) return false;
    applied.push(value.item_id);
    return true;
  };
  shell.setStatus = (message, error) => statuses.push({ message, error });

  const connecting = shell.connectDesktopContext();
  pushContext({ invalid: true });
  assert.equal(shell.contextGeneration, 0,
    "an invalid push must not suppress the valid current-context snapshot");
  pushContext(context({ item_id: "newer-book" }));
  rejectCurrent(new Error("stale current-context failure"));
  await connecting;

  assert.deepEqual(applied, ["newer-book"]);
  assert.deepEqual(statuses, [],
    "a stale snapshot failure must not overwrite the newer pushed context status");
});

test("destroy invalidates in-flight context delivery", async () => {
  let pushContext;
  let resolveCurrent;
  let layoutDestroyed = false;
  const applied = [];
  const shell = Object.create(CorrectionsShell.prototype);
  shell.contextGeneration = 0;
  shell.destroyed = false;
  shell.listeners = [];
  shell.layout = { destroy() { layoutDestroyed = true; } };
  shell.desktop = { workbenches: {
    onContext(callback) {
      pushContext = callback;
      return () => { pushContext = null; };
    },
    currentContext() {
      return new Promise((resolve) => { resolveCurrent = resolve; });
    },
  } };
  shell.applyContextSafely = (value) => {
    applied.push(value.item_id);
    return true;
  };
  shell.setStatus = () => {};

  const connecting = shell.connectDesktopContext();
  shell.destroy();
  resolveCurrent(context({ item_id: "late-book" }));
  await connecting;

  assert.equal(layoutDestroyed, true);
  assert.equal(pushContext, null);
  assert.deepEqual(applied, []);
});

test("tray tabs implement wrapping keyboard navigation", () => {
  assert.equal(nextTrayTab("reviews", "ArrowRight"), "jobs");
  assert.equal(nextTrayTab("jobs", "ArrowRight"), "reviews");
  assert.equal(nextTrayTab("jobs", "ArrowLeft"), "reviews");
  assert.equal(nextTrayTab("reviews", "ArrowLeft"), "jobs");
  assert.equal(nextTrayTab("jobs", "Home"), "reviews");
  assert.equal(nextTrayTab("reviews", "End"), "jobs");
  assert.equal(nextTrayTab("reviews", "ArrowDown"), null);
  assert.equal(nextTrayTab("missing", "ArrowRight"), null);
});


test("standalone shell markup exposes accessible panes, tree, editor, tray, and gutters", () => {
  assert.match(templateSource, /data-corrections-root/);
  const rootTag = templateSource.match(/<div class="corrections-app"[\s\S]*?>/)[0];
  for (const duplicateState of [
    "data-books-collapsed",
    "data-artifacts-collapsed",
    "data-properties-collapsed",
    "data-tray-collapsed",
    "data-primary-maximized",
    "data-navigator-open",
    "data-properties-open",
  ]) {
    assert.doesNotMatch(rootTag, new RegExp(duplicateState),
      `${duplicateState} belongs only to the live workspace layout`);
  }
  assert.match(templateSource, /<nav[^>]+id="corrections-books"/);
  assert.match(templateSource, /id="corrections-artifacts"[\s\S]*?role="tree"/);
  assert.match(templateSource, /<main[^>]+id="corrections-editor"/);
  assert.match(templateSource, /<aside[^>]+id="corrections-properties"/);
  assert.match(templateSource, /id="corrections-tray"[\s\S]*?role="tablist"/);
  assert.match(templateSource, /data-editor-selector/);
  assert.match(templateSource,
    /data-editor-resource-label[^>]+aria-live="polite"[^>]+aria-atomic="true"/);
  assert.doesNotMatch(templateSource, /data-editor-host[^>]+aria-live=/,
    "large OCR or metadata documents must not become atomic live-region announcements");
  assert.match(templateSource, /data-layout-action="maximize-primary"/);
  assert.match(templateSource, /data-layout-action="reset"/);
  assert.match(templateSource,
    /data-classification-controls[^>]+aria-label="Classification commands"/);
  assert.match(templateSource,
    /data-classification-toolbar[^>]+aria-label="Classification commands"/);
  assert.match(templateSource,
    /data-classification-palette-trigger[^>]+aria-label="Open classification command palette"/);
  assert.match(templateSource,
    /data-corrections-command-target[^>]+aria-live="polite"[^>]+aria-atomic="true"/);
  assert.match(templateSource, /corrections\/commands\.js/);
  assert.match(templateSource, /corrections\/classification-controls\.js/);
  assert.match(templateSource, /corrections\/image-adjust-tool\.js/);
  assert.match(templateSource, /engine-client\.js/);
  assert.match(templateSource, /corrections\/engine-adapter\.js/);
  assert.match(templateSource, /engine-client\.js'\) \}\}\?v=\{\{ corrections_engine_client_v \}\}/);
  assert.match(
    templateSource,
    /corrections\/engine-adapter\.js'\) \}\}\?v=\{\{ corrections_engine_adapter_v \}\}/,
  );
  assert.ok(
    templateSource.indexOf("engine-client.js") <
      templateSource.indexOf("corrections/engine-adapter.js"),
  );
  assert.ok(
    templateSource.indexOf("corrections/engine-adapter.js") <
      templateSource.indexOf("corrections/shell.js"),
  );

  const separators = [...templateSource.matchAll(/<div[^>]+role="separator"[^>]*>/g)];
  assert.equal(separators.length, 4);
  for (const separator of separators) {
    assert.match(separator[0], /tabindex="0"/);
    assert.match(separator[0], /aria-orientation="(?:horizontal|vertical)"/);
    assert.match(separator[0], /aria-valuemin=/);
    assert.match(separator[0], /aria-valuemax=/);
    assert.match(separator[0], /aria-valuenow=/);
  }
  assert.doesNotMatch(templateSource, /app\.js/);
});


test("corrections markup hosts the OCR proposals panel and loads its module", () => {
  assert.match(templateSource, /data-ocr-proposals\b/);
  const host = templateSource.match(/<section[^>]+data-ocr-proposals[^>]*>/)[0];
  assert.match(host, /\bhidden\b/,
    "the panel stays hidden until discovery proves the read capability");
  assert.match(templateSource, /corrections\/ocr-proposals\.js/);
  assert.ok(
    templateSource.indexOf("corrections/image-adjust-tool.js") <
      templateSource.indexOf("corrections/ocr-proposals.js"),
  );
  assert.ok(
    templateSource.indexOf("corrections/ocr-proposals.js") <
      templateSource.indexOf("corrections/shell.js"),
  );
  assert.match(cssSource, /\.ocr-proposals-host/);
  assert.match(cssSource, /\.ocr-proposal-text\b/);
});


test("corrections markup hosts the CH panel above OCR proposals and loads its module", () => {
  assert.match(templateSource, /data-ch-panel\b/);
  const host = templateSource.match(/<section[^>]+data-ch-panel[^>]*>/)[0];
  assert.match(host, /\bhidden\b/,
    "the panel stays hidden until an item proves capture-backed");
  assert.ok(
    templateSource.indexOf("data-ch-panel") <
      templateSource.indexOf("data-ocr-proposals"),
    "bibliographic context reads above machine OCR output");
  assert.match(templateSource, /corrections\/ch-panel\.js/);
  assert.match(templateSource,
    /ch-panel\.js'\) \}\}\?v=\{\{ ch_panel_v \}\}/);
  assert.match(templateSource,
    /ocr-proposals\.js'\) \}\}\?v=\{\{ ocr_proposals_v \}\}/,
    "each proposals module versions independently of the shell");
  assert.ok(
    templateSource.indexOf("corrections/ch-panel.js") <
      templateSource.indexOf("corrections/shell.js"),
  );
  assert.match(cssSource, /\.ch-panel-host/);
  assert.match(cssSource, /\.ch-candidate\b/);
});


test("the shell wires OCR proposals through discovery, selection, and re-OCR", {
  timeout: 5000,
}, async () => {
  const proposalSummary = {
    proposal_ref: `cop-${"a".repeat(40)}`,
    operation_id: `correction-reocr:${"c".repeat(48)}`,
    source: {
      kind: "ocr-ready",
      artifact_id: "result-ocr-ready-1",
      artifact_revision: "ocr-ready-r1",
      content_sha256: "b".repeat(64),
    },
    provider: { provider_id: "mistral", model: "mistral-ocr-latest" },
    publication_policy: "machine-proposal-only",
    content_sha256: "c".repeat(64),
    availability: "available",
  };
  const queueOperationId = `correction-reocr:${"c".repeat(48)}`;
  const queueJobId = `correction-ocr-${"d".repeat(24)}`;
  const queuedJob = {
    id: queueJobId,
    kind: "correction.ocr-followup",
    state: "queued",
    subject: { item_id: "book-1", source_id: "result-ocr-ready-1" },
    progress: { completed: 0, total: 4, unit: "phase", phase: "queued" },
    cancellable: true,
    revision: 1,
    created_at: "2026-08-02T12:00:00Z",
    updated_at: "2026-08-02T12:00:00Z",
    finished_at: "",
    note: "",
    error: null,
    input_revisions: {
      parent_operation_id: queueOperationId,
      artifact_id: "result-ocr-ready-1",
      artifact_revision: "ocr-ready-r1",
      source_sha256: "b".repeat(64),
      publication_policy: "machine-proposal-only",
    },
    outputs: [],
  };
  const terminalJob = {
    ...queuedJob,
    state: "done",
    progress: { completed: 4, total: 4, unit: "phase", phase: "complete" },
    cancellable: false,
    revision: 2,
    finished_at: "2026-08-02T12:00:03Z",
    note: "proposal committed",
    outputs: [
      { kind: "ocr-proposal", ref: `cop-${"a".repeat(40)}`, partial: false },
    ],
  };
  const calls = [];
  const response = (status, body) => ({
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  });
  const engineClient = new EngineClient({
    transport: async (url, init) => {
      calls.push({ method: init.method, url });
      if (url === "/api/v1/capabilities") {
        return response(200, {
          ok: true,
          schema: "librarytool.capabilities/1",
          capabilities: [
            {
              id: "library.corrections.ocr-proposals.read",
              version: 1,
              providers: ["library.corrections.transforms"],
            },
            {
              id: "library.corrections.reocr.queue",
              version: 1,
              providers: ["library.corrections.reocr"],
            },
          ],
          modules: [],
          workbenches: [],
        });
      }
      if (url.startsWith("/api/v1/items/book-1/ocr-proposals")) {
        return response(200, {
          ok: true,
          schema: "librarytool.correction-ocr-proposals/1",
          item_id: "book-1",
          snapshot_revision: `cops-${"e".repeat(64)}`,
          proposals: [proposalSummary],
          next_cursor: null,
          total: 1,
        });
      }
      if (url.endsWith("/reocr")) {
        return response(202, {
          ok: true,
          schema: "librarytool.correction-reocr-queue-receipt/1",
          replayed: false,
          operation_id: queueOperationId,
          job_id: queueJobId,
          job: queuedJob,
          source: { ...proposalSummary.source },
        });
      }
      if (url.startsWith("/api/v1/jobs/")) {
        return response(200, { ok: true, job: terminalJob });
      }
      throw new Error(`unexpected transport: ${init.method} ${url}`);
    },
  });
  const documentRef = fakeDocument();
  const windowRef = { engineClient, localStorage: new MemoryStorage() };
  documentRef.defaultView = windowRef;
  const rootElement = new FakeNode("div", documentRef);
  const proposalsHost = new FakeNode("section", documentRef);
  proposalsHost.setAttribute("data-ocr-proposals", "");
  proposalsHost.hidden = true;
  const statusNode = new FakeNode("span", documentRef);
  statusNode.setAttribute("data-status-message", "");
  rootElement.append(proposalsHost, statusNode);

  const shell = new CorrectionsShell({
    root: rootElement,
    documentRef,
    windowRef,
    editorRegistry: createDefaultEditorRegistry({ documentRef }),
    layoutController: {
      getState: () => ({ ...DEFAULT_LAYOUT }),
      replaceState() {},
      destroy() {},
    },
    classificationController: false,
    booksFeature: false,
    artifactsFeature: false,
    itemProperties: false,
  });
  assert.ok(shell.engineCorrections.ocrProposals,
    "engine runtime exposes the OCR proposals port");
  assert.equal(typeof shell.engineCorrections.ocrProposals.queueReocr,
    "function");
  assert.ok(shell.ocrProposalsFeature, "the shell owns the mounted panel");

  shell.mount();
  await shell.capabilitiesPromise;
  assert.equal(proposalsHost.hidden, false,
    "discovery unhides the proposals panel");
  assert.equal(shell.imageAdjustTool.getState().reocrCapability, true,
    "discovery arms the Re-OCR affordance");

  shell.selectAddress({ itemId: "book-1" });
  const settled = (predicate) => new Promise((resolve, reject) => {
    const deadline = Date.now() + 3000;
    const check = () => {
      if (predicate()) return resolve(null);
      if (Date.now() > deadline) return reject(new Error("condition timed out"));
      setTimeout(check, 20);
    };
    check();
  });
  await settled(() => {
    const count = rootElement.querySelector("[data-ocr-proposals-count]");
    return count && count.textContent === "1";
  });

  const receipt = await shell.queueStandaloneReocr({
    operationId: "reocr-click-1",
    itemId: "book-1",
    artifactId: "corrected-display-1",
    expectedArtifactRevision: "corrected-display-r1",
  });
  assert.equal(receipt.job_id, queueJobId);
  assert.equal(statusNode.textContent, "Re-OCR queued");

  // The tracked job completes on the polling port and refreshes the panel.
  await settled(() =>
    statusNode.textContent === "Re-OCR complete — proposal ready");
  assert.ok(calls.some(({ method, url }) =>
    method === "GET" && url === `/api/v1/jobs/${queueJobId}`));
  const proposalReads = calls.filter(({ url }) =>
    url.startsWith("/api/v1/items/book-1/ocr-proposals"));
  assert.ok(proposalReads.length >= 2,
    "the terminal result reloads the proposals catalog");

  shell.destroy();
});


test("the shell wires the CH panel to selection, metadata saves, and refreshes", async () => {
  const fetchCalls = [];
  const fetchImpl = async (url, init) => {
    fetchCalls.push({ url, init });
    return {
      ok: true,
      status: 200,
      json: async () => ({
        ok: true,
        item_id: "book-1",
        list_available: true,
        match: null,
        candidates: [],
        rejected: null,
      }),
    };
  };
  const item = {
    id: "book-1",
    kind: "capture",
    title: "Captured Herbal",
    metadata: {},
    revision: "item-r1",
  };
  const loadCalls = [];
  const itemMetadataApi = {
    async loadItem({ itemId }) {
      loadCalls.push(itemId);
      return item;
    },
    async updateItem() {
      throw new Error("unused");
    },
  };
  const bookRefreshes = [];
  const documentRef = fakeDocument();
  const windowRef = { localStorage: new MemoryStorage() };
  documentRef.defaultView = windowRef;
  const rootElement = new FakeNode("div", documentRef);
  const chHost = new FakeNode("section", documentRef);
  chHost.setAttribute("data-ch-panel", "");
  chHost.hidden = true;
  const propertiesHost = new FakeNode("section", documentRef);
  propertiesHost.setAttribute("data-item-properties", "");
  const statusNode = new FakeNode("span", documentRef);
  statusNode.setAttribute("data-status-message", "");
  rootElement.append(chHost, propertiesHost, statusNode);
  const shell = new CorrectionsShell({
    root: rootElement,
    documentRef,
    windowRef,
    fetchImpl,
    itemMetadataApi,
    editorRegistry: createDefaultEditorRegistry({ documentRef }),
    layoutController: {
      getState: () => ({ ...DEFAULT_LAYOUT }),
      replaceState() {},
      destroy() {},
    },
    classificationController: false,
    booksFeature: {
      mount() {},
      destroy() {},
      setSelection() {},
      refresh(reason) {
        bookRefreshes.push(reason);
        return Promise.resolve();
      },
    },
    artifactsFeature: false,
  });
  assert.ok(shell.chPanelFeature, "the shell owns the mounted CH panel");
  shell.mount();
  assert.equal(chHost.hidden, true,
    "the panel stays hidden until a capture-backed item is selected");
  const settle = () => new Promise((resolve) => setImmediate(resolve));

  shell.selectAddress({ itemId: "book-1" });
  await settle();
  assert.equal(fetchCalls[0].url, "/api/corrections/ch/state?item_id=book-1");
  assert.equal(fetchCalls[0].init.cache, "no-store");
  assert.equal(chHost.hidden, false);
  assert.deepEqual(loadCalls, ["book-1"],
    "item selection also loads the metadata editor");

  const beforeSave = fetchCalls.length;
  shell.itemProperties.onChanged(item, { replayed: false });
  await settle();
  assert.equal(fetchCalls.length, beforeSave + 1,
    "a metadata save refreshes the CH match");
  assert.deepEqual(bookRefreshes, ["metadata"]);

  const beforeExternal = fetchCalls.length;
  await shell.refreshExternalState("external-change", { includeBooks: false });
  assert.equal(fetchCalls.length, beforeExternal + 1,
    "external convergence refreshes the CH panel with the other detail panes");

  const beforeDecision = loadCalls.length;
  shell.chPanelFeature.onChanged({ ok: true, replayed: false });
  await settle();
  assert.equal(loadCalls.length, beforeDecision + 1,
    "a CH decision reloads the item metadata it rewrote");
  assert.equal(bookRefreshes.at(-1), "ch-reconcile");

  shell.destroy();
  assert.equal(shell.chPanelFeature, null);
  assert.equal(chHost.children.length, 0,
    "destroy releases the CH panel's DOM");
});


test("Books navigation commands register on j/k without classification conflicts",
  async () => {
    const registry = new CorrectionCommandRegistry();
    for (const command of DEFAULT_CLASSIFICATION_COMMANDS) {
      registry.register({ ...command, execute: async () => null });
    }
    const shell = Object.create(CorrectionsShell.prototype);
    const steps = [];
    shell.classificationController = { registry };
    shell.booksFeature = { books: {
      stepSelection(direction) {
        steps.push(direction);
        return { id: "book-1" };
      },
      canStepSelection(direction) { return direction === 1; },
    } };
    shell.registerBooksNavigationCommands();

    for (const command of BOOKS_NAVIGATION_COMMANDS) {
      assert.equal(normalizeKeyBinding(command.defaultBinding),
        command.defaultBinding,
        "navigation bindings must satisfy the binding grammar");
      assert.ok(!DEFAULT_CLASSIFICATION_COMMANDS.some((candidate) =>
        candidate.defaultBinding === command.defaultBinding),
      "navigation bindings must not shadow classification defaults");
    }
    assert.equal(registry.bindingFor("corrections.books.next-item"), "j");
    assert.equal(registry.bindingFor("corrections.books.previous-item"), "k");
    assert.equal(
      registry.commandForBinding("j").id, "corrections.books.next-item");
    assert.equal(
      registry.commandForBinding("k").id, "corrections.books.previous-item");
    for (const command of DEFAULT_CLASSIFICATION_COMMANDS) {
      assert.deepEqual(
        [...registry.conflicts(command.defaultBinding, command.id)], []);
    }
    assert.equal(registry.canInvoke("corrections.books.next-item", {}), true);
    assert.equal(
      registry.canInvoke("corrections.books.previous-item", {}), false,
      "previous stays unavailable when the panel has nothing before");
    await registry.invoke("corrections.books.next-item", {});
    assert.deepEqual(steps, [1]);

    shell.registerBooksNavigationCommands();
    assert.equal(registry.bindingFor("corrections.books.next-item"), "j",
      "re-registration is idempotent");
  });


test("stored classification remaps cannot claim the Books navigation keys", () => {
  const registry = new CorrectionCommandRegistry();
  for (const command of DEFAULT_CLASSIFICATION_COMMANDS) {
    registry.register({ ...command, execute: async () => null });
  }
  const shell = Object.create(CorrectionsShell.prototype);
  shell.classificationController = { registry };
  shell.booksFeature = { books: {
    stepSelection: () => null,
    canStepSelection: () => false,
  } };
  shell.restoringProfile = false;
  shell.registerBooksNavigationCommands();
  shell.restoreClassificationProfile({
    bindings: { "corrections.category.cover": "j" },
  });
  assert.equal(registry.bindingFor("corrections.category.cover"), "",
    "a stored remap that collides with a navigation key is dropped");
  assert.equal(registry.bindingFor("corrections.books.next-item"), "j");
  assert.equal(registry.bindingFor("corrections.category.spine"), "s",
    "unrelated classification bindings restore normally");
});


test("Books navigation hotkeys stay scoped to the browsable surfaces", () => {
  const shell = Object.create(CorrectionsShell.prototype);
  shell.root = { dataset: {} };
  const navCommand = {
    id: "corrections.books.next-item",
    targetKind: "books-item",
  };
  const overlayTarget = {
    key: "annotation:region-1",
    objectType: "spatial-annotation",
    itemId: "book-1",
    id: "region-1",
    revision: "region-r1",
  };
  const outside = {
    dataset: { reviewAction: "resolve" },
    parentNode: { dataset: { trayPanel: "reviews" }, parentNode: shell.root },
  };
  const booksList = { dataset: { booksList: "" }, parentNode: shell.root };
  const artifactsTree = { dataset: { artifactsTree: "" }, parentNode: shell.root };
  const editorHost = { dataset: { editorHost: "" }, parentNode: shell.root };
  const booksViewBar = { dataset: { booksViewBar: "" }, parentNode: shell.root };
  const viewBarButton = { dataset: { booksNav: "next" }, parentNode: booksViewBar };
  assert.equal(shell.classificationEventEligible(
    { target: outside }, navCommand, { softTarget: overlayTarget }), false,
  "a hovered region cannot route navigation keys from outside the surfaces");
  assert.equal(shell.classificationEventEligible(
    { target: booksList }, navCommand, {}), true);
  assert.equal(shell.classificationEventEligible(
    { target: artifactsTree }, navCommand, {}), true);
  assert.equal(shell.classificationEventEligible(
    { target: editorHost }, navCommand, {}), true);
  assert.equal(shell.classificationEventEligible(
    { target: viewBarButton }, navCommand, {}), true,
  "the Books view bar (view + prev/next buttons) is a browsable surface");
});


test("shell styles and controllers cover compact and reduced-motion operation", () => {
  assert.match(cssSource, /\[data-primary-maximized="true"\]/);
  assert.match(cssSource, /\[data-compact="true"\]/);
  assert.match(cssSource,
    /\[data-compact="true"\]\[data-primary-maximized="true"\]\s*\{\s*display:\s*block/);
  assert.match(cssSource,
    /\[data-compact="true"\]\[data-tray-collapsed="true"\]\s*\{[\s\S]*?grid-template-rows:\s*minmax\(260px,\s*1fr\)\s+0\s+38px/);
  assert.match(cssSource, /@media \(prefers-reduced-motion: reduce\)/);
  assert.match(cssSource, /:focus-visible/);
  assert.match(cssSource, /minmax\(360px, 1fr\)/);
  assert.match(layoutSource, /addEventListener\("pointermove"/);
  assert.match(layoutSource, /handleGutterKey/);
  assert.match(layoutSource, /matchMedia/);
  assert.match(shellSource, /workbenches\.currentContext/);
  assert.match(shellSource, /workbenches\.onContext/);
  assert.doesNotMatch(shellSource, /innerHTML|window\.correctionsState|app\.js/);
});


test("lean Books targets borrow archived state from the artifacts feature", () => {
  // ``captureCommandTarget`` in books.js publishes identity and revision only.
  // The archive command decides archive-vs-restore from metadata assertions,
  // so without this top-up an already-archived capture selected from Books
  // re-asserts ``archived`` and the toggle never restores.
  const booksTarget = Object.freeze({
    key: "artifact:capture-1",
    objectType: "raster-artifact",
    family: "image",
    group: "source-images",
    kind: "capture",
    itemId: "book-1",
    id: "capture-1",
    artifactId: "capture-1",
    revision: "capture-r1",
    label: "Capture 1",
  });
  const archived = Object.freeze([
    Object.freeze({ name: "archived", value: true, origin: "manual" }),
    Object.freeze({
      name: "archived_at",
      value: "2026-08-05T00:00:00Z",
      origin: "manual",
    }),
  ]);
  const shell = Object.create(CorrectionsShell.prototype);
  shell.artifactsFeature = {
    items: new Map([["artifact:capture-1", {
      key: "artifact:capture-1",
      objectType: "raster-artifact",
      itemId: "book-1",
      id: "capture-1",
      revision: "capture-r1",
      metadataAssertions: archived,
    }]]),
  };

  const enriched = shell.classificationTargetMetadata(booksTarget);
  assert.notEqual(enriched, booksTarget);
  assert.deepEqual(enriched.metadataAssertions, archived);
  assert.equal(enriched.revision, "capture-r1",
    "the top-up borrows assertions only; identity and revision stay Books'");
  assert.equal(enriched.artifactId, "capture-1");

  // A target that already carries assertions is returned untouched, so the
  // artifacts tree keeps publishing the exact decoded item it selected.
  const decoded = Object.freeze({
    key: "artifact:capture-1",
    itemId: "book-1",
    id: "capture-1",
    metadataAssertions: Object.freeze([]),
  });
  assert.equal(shell.classificationTargetMetadata(decoded), decoded);

  // Nothing to borrow from leaves the target alone rather than inventing one.
  assert.equal(
    shell.classificationTargetMetadata(Object.freeze({
      key: "artifact:capture-9",
      itemId: "book-1",
      id: "capture-9",
    })).metadataAssertions,
    undefined,
  );
  assert.equal(shell.classificationTargetMetadata(null), null);

  // The Books publication paths all route through the top-up.
  const published = [];
  shell.classificationController = {
    stateSnapshot: () => ({ selectionTarget: null }),
    setSelectionTarget(target) {
      published.push(target);
      return target;
    },
  };
  shell.publishClassificationSelectionTarget(booksTarget, { source: "books" });
  assert.deepEqual(published[0].metadataAssertions, archived);
});


test("region extraction refuses a region linked to another open image", () => {
  // ``decodeArtifactSummary`` folds every link shape into ``linkedKeys``; the
  // raw ``linked_artifact_ids`` wire name does not survive decoding, so a
  // guard reading only the wire names saw no links on a decoded target and
  // waved cross-image extractions through.
  const shell = Object.create(CorrectionsShell.prototype);
  shell.state = {
    resource: { correction: { artifact_id: "figure-1", item_id: "book-1" } },
  };

  assert.equal(
    shell.classificationTransformContract({
      key: "spatial-annotation:region-1",
      linkedKeys: Object.freeze(["artifact:figure-1"]),
    }),
    shell.state.resource.correction,
    "a decoded region linked to the open image extracts",
  );
  assert.equal(
    shell.classificationTransformContract({
      key: "spatial-annotation:region-2",
      linkedKeys: Object.freeze(["artifact:figure-2"]),
    }),
    null,
    "a decoded region linked elsewhere cannot crop the open image",
  );
  assert.equal(
    shell.classificationTransformContract({
      key: "spatial-annotation:region-3",
      linkedKeys: Object.freeze([]),
    }),
    shell.state.resource.correction,
    "a link-free legacy region still uses the open resource",
  );

  // The raw wire shapes the overlay publishes keep working.
  assert.equal(
    shell.classificationTransformContract({
      key: "spatial-annotation:region-4",
      linked_artifact_ids: ["figure-1"],
    }),
    shell.state.resource.correction,
  );
  assert.equal(
    shell.classificationTransformContract({
      key: "spatial-annotation:region-5",
      linked_artifact_ids: ["figure-2"],
    }),
    null,
  );
  assert.equal(
    shell.classificationTransformContract({
      key: "spatial-annotation:region-6",
      linkedArtifactId: "figure-2",
    }),
    null,
  );

  shell.state = { resource: {} };
  assert.equal(
    shell.classificationTransformContract({ linkedKeys: [] }), null,
    "no open correction resource means nothing to crop",
  );
});


test("capture geometry is drawn without re-applying the display orientation", () => {
  // capture_geometry normalizes against the EXIF-upright display: cv2
  // applies the orientation when it decodes and Pillow's exif_transpose
  // reaches the same frame. Re-applying the declared orientation here
  // would rotate every box off the text on a rotated capture. Rendering
  // the same region in a space already known to be upright pins that.
  const render = (coordinateSpace) => {
    const documentRef = fakeDocument();
    const stage = new FakeNode("div", documentRef);
    const image = new FakeNode("img", documentRef);
    stage.append(image);
    const shell = Object.create(CorrectionsShell.prototype);
    Object.assign(shell, {
      documentRef,
      windowRef: {},
      classificationController: null,
    });
    shell.mountArtifactOverlay({ image }, {
      summary: { itemId: "book-1" },
      coordinateSpace,
      // A display that declares a rotation is exactly the case the old
      // whitelist mishandled.
      dimensions: { width: 100, height: 200, orientation: 6 },
      regions: [{
        annotation_id: "capture-region:abc",
        object_type: "spatial-annotation",
        revision: "region-r1",
        selector: {
          coordinate_space: coordinateSpace,
          points: [
            { x: 0.1, y: 0.1 }, { x: 0.5, y: 0.1 }, { x: 0.5, y: 0.4 },
          ],
        },
      }],
    });
    const marker = stage.querySelector(".corrections-artifact-overlay-shape");
    assert.ok(marker, `the overlay renders a marker for ${coordinateSpace}`);
    const wrapper = stage.querySelector("[data-overlay-key]");
    return {
      clip: marker.style.clipPath,
      left: wrapper.style.left,
      top: wrapper.style.top,
    };
  };

  assert.deepEqual(
    render("display_normalized"),
    render("canvas-normalized"),
    "display_normalized coordinates are already upright, like canvas-normalized",
  );
});

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const {
  createDefaultEditorRegistry,
  resourceFamily,
} = require("../tools/whl_explorer/static/corrections/editor-registry");
const {
  CorrectionsProfileStore,
  PROFILE_SCHEMA,
  validateProfileKey,
} = require("../tools/whl_explorer/static/corrections/ui-profile");
const {
  normalizeImageAdjustProfile,
} = require("../tools/whl_explorer/static/corrections/image-adjust-tool");
const {
  CorrectionCommandRegistry,
  DEFAULT_CLASSIFICATION_COMMANDS,
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
  CONTEXT_SCHEMA,
  CorrectionsShell,
  CorrectionsWindowState,
  artifactSelection,
  nextTrayTab,
  normalizeSelection,
  normalizeWorkbenchContext,
  selectionContext,
} = require("../tools/whl_explorer/static/corrections/shell");


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
    { target: reviewButton }, null, { softTarget: { id: "hovered-image" } }), false,
    "hover state cannot escape the pane that owns the keyboard event");

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

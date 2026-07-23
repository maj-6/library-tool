"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const {
  CLASSIFICATION_COMMAND_IDS,
} = require("../tools/whl_explorer/static/corrections/commands");
const {
  createClassificationController,
} = require("../tools/whl_explorer/static/corrections/keymap");
const {
  CLASSIFICATION_CONTROL_COMMAND_IDS,
  createClassificationControls,
} = require("../tools/whl_explorer/static/corrections/classification-controls");
const {
  FakeNode,
  fakeDocument,
} = require("./fixtures/corrections_fake_dom");


function image() {
  return {
    key: "artifact:scan-1",
    objectType: "raster-artifact",
    family: "image",
    group: "source-images",
    itemId: "book-1",
    id: "scan-1",
    revision: "scan-r1",
    label: "Front capture",
  };
}


function annotation() {
  return {
    key: "annotation:region-1",
    objectType: "spatial-annotation",
    family: "regions",
    group: "layout-regions",
    itemId: "book-1",
    id: "region-1",
    revision: "region-r1",
    label: "Margin note",
    linkedKeys: [],
  };
}


function settled() {
  return new Promise((resolve) => setImmediate(resolve));
}


function byDataset(root, selector, name, value) {
  return root.querySelectorAll(selector)
    .find((node) => node.dataset && node.dataset[name] === value);
}


function harness() {
  const documentRef = fakeDocument();
  documentRef.hasFocus = () => true;
  const scope = new FakeNode("main", documentRef);
  const host = new FakeNode("div", documentRef);
  host.dataset.classificationControls = "true";
  const toolbar = new FakeNode("div", documentRef);
  toolbar.dataset.classificationToolbar = "true";
  const paletteTrigger = new FakeNode("button", documentRef);
  paletteTrigger.dataset.classificationPaletteTrigger = "true";
  const contextTarget = new FakeNode("button", documentRef);
  contextTarget.dataset.classificationContextTarget = "true";
  scope.append(host, toolbar, paletteTrigger, contextTarget);
  const calls = [];
  const errors = [];
  const bindingChanges = [];
  let sequence = 0;
  const controller = createClassificationController({
    scope,
    documentRef,
    port: {
      async assignImageCategory(payload) {
        calls.push(["image", payload]);
        return {};
      },
      async assignRegionRole(payload) {
        calls.push(["role", payload]);
        return {};
      },
    },
    operationIdFactory: (prefix) => `${prefix}-controls-${++sequence}`,
    onError: (error) => errors.push(error),
  });
  const controls = createClassificationControls({
    root: host,
    documentRef,
    controller,
    toolbarRoot: toolbar,
    paletteTrigger,
    contextScope: scope,
    isContextMenuEvent: (event) =>
      Boolean(event.target && event.target.dataset &&
        event.target.dataset.classificationContextTarget),
    onError: (error) => errors.push(error),
    onBindingsChanged: (bindings, change) =>
      bindingChanges.push([bindings, change]),
  });
  return {
    bindingChanges,
    calls,
    controller,
    controls,
    documentRef,
    errors,
    host,
    contextTarget,
    paletteTrigger,
    scope,
    toolbar,
  };
}


test("presenter renders and binds exactly the six visible registered commands", async () => {
  const { calls, controller, controls, host } = harness();
  controller.setSelectionTarget(image());
  controller.mount();
  controls.mount();

  const buttons = host.querySelectorAll("[data-command-button]");
  assert.equal(buttons.length, 6);
  assert.deepEqual(
    buttons.map((button) => button.dataset.classificationCommand),
    CLASSIFICATION_CONTROL_COMMAND_IDS,
  );
  const titleButton = byDataset(
    host,
    "[data-command-button]",
    "classificationCommand",
    CLASSIFICATION_COMMAND_IDS.titlePage,
  );
  const marginaliaButton = byDataset(
    host,
    "[data-command-button]",
    "classificationCommand",
    CLASSIFICATION_COMMAND_IDS.marginalia,
  );
  assert.equal(titleButton.disabled, false);
  assert.equal(marginaliaButton.disabled, true);
  assert.equal(titleButton.getAttribute("aria-keyshortcuts"), "T");
  assert.equal(
    titleButton.querySelector(".classification-command-key").textContent,
    "T",
  );

  titleButton.emit("click");
  await settled();
  assert.equal(calls.length, 1);
  assert.equal(calls[0][0], "image");
  assert.equal(calls[0][1].category, "title_page");

  controller.setSelectionTarget(annotation());
  assert.equal(titleButton.disabled, true);
  assert.equal(marginaliaButton.disabled, false);
});


test("toolbar, context menu, and palette reuse the registered command entries", async () => {
  const {
    calls,
    contextTarget,
    controller,
    controls,
    documentRef,
    paletteTrigger,
    scope,
    toolbar,
  } = harness();
  controller.setSelectionTarget(image());
  controller.mount();
  controls.mount();

  const toolbarButtons = toolbar.querySelectorAll("[data-command-button]");
  assert.equal(toolbarButtons.length, 6);
  assert.equal(controller.registry.list().length, 6,
    "additional command surfaces must not register parallel definitions");
  controller.registry.remap(CLASSIFICATION_COMMAND_IDS.titlePage, "x");
  const toolbarTitle = byDataset(
    toolbar,
    "[data-command-button]",
    "classificationCommand",
    CLASSIFICATION_COMMAND_IDS.titlePage,
  );
  assert.equal(toolbarTitle.getAttribute("aria-keyshortcuts"), "X");

  const contextEvent = scope.emit("contextmenu", {
    target: contextTarget,
    clientX: 24,
    clientY: 32,
  });
  assert.equal(contextEvent.defaultPrevented, true);
  const menu = scope.querySelector("[data-classification-context-menu]");
  assert.equal(menu.hidden, false);
  assert.equal(menu.querySelectorAll("[data-surface-command]").length, 4,
    "the image context menu contains only currently available registry entries");
  const menuQuerySelectorAll = menu.querySelectorAll.bind(menu);
  menu.querySelectorAll = (selector) => {
    const matches = menuQuerySelectorAll(selector);
    return {
      length: matches.length,
      item: (index) => matches[index] || null,
      [Symbol.iterator]: function* iterateMatches() {
        yield* matches;
      },
    };
  };
  menu.emit("keydown", { key: "End" });
  assert.equal(
    documentRef.activeElement.dataset.surfaceCommand,
    CLASSIFICATION_COMMAND_IDS.contentSpecimen,
    "keyboard navigation accepts browser NodeList results without Array methods",
  );
  menu.querySelectorAll = menuQuerySelectorAll;
  const contextCover = byDataset(
    menu,
    "[data-surface-command]",
    "surfaceCommand",
    CLASSIFICATION_COMMAND_IDS.cover,
  );
  contextCover.emit("click");
  await settled();
  assert.equal(calls.at(-1)[0], "image");
  assert.equal(calls.at(-1)[1].category, "cover");
  assert.equal(menu.hidden, true);

  paletteTrigger.emit("click");
  const palette = scope.querySelector("[data-classification-palette]");
  assert.equal(palette.hidden, false);
  const paletteButtons = palette.querySelectorAll("[data-surface-command]");
  assert.equal(paletteButtons.length, 6);
  const paletteSpine = byDataset(
    palette,
    "[data-surface-command]",
    "surfaceCommand",
    CLASSIFICATION_COMMAND_IDS.spine,
  );
  const paletteMarginalia = byDataset(
    palette,
    "[data-surface-command]",
    "surfaceCommand",
    CLASSIFICATION_COMMAND_IDS.marginalia,
  );
  assert.equal(paletteSpine.disabled, false);
  assert.equal(paletteMarginalia.disabled, true);
  paletteSpine.emit("click");
  await settled();
  assert.equal(calls.at(-1)[1].category, "spine");
  assert.equal(palette.hidden, true);

  controls.destroy();
  assert.equal(toolbar.children.length, 0);
  assert.equal(scope.querySelector("[data-classification-context-menu]"), null);
  assert.equal(scope.querySelector("[data-classification-palette]"), null);
  controller.destroy();
});


test("context menu entries and invocation use the target owned by the event", async () => {
  const {
    calls,
    contextTarget,
    controller,
    controls,
    scope,
  } = harness();
  const eventTarget = annotation();
  controls.isContextMenuEvent = (event) =>
    event.target === contextTarget ? eventTarget : null;
  controller.setSelectionTarget(image());
  controller.mount();
  controls.mount();

  const whitespaceEvent = scope.emit("contextmenu", { target: scope });
  assert.equal(whitespaceEvent.defaultPrevented, false);

  const contextEvent = scope.emit("contextmenu", {
    target: contextTarget,
    clientX: 16,
    clientY: 20,
  });
  assert.equal(contextEvent.defaultPrevented, true);
  const menu = scope.querySelector("[data-classification-context-menu]");
  assert.equal(menu.querySelectorAll("[data-surface-command]").length, 2,
    "the event-owned annotation determines command availability");
  const marginalia = byDataset(
    menu,
    "[data-surface-command]",
    "surfaceCommand",
    CLASSIFICATION_COMMAND_IDS.marginalia,
  );
  marginalia.emit("click");
  await settled();
  assert.equal(calls.at(-1)[0], "role");
  assert.equal(calls.at(-1)[1].annotationId, "region-1");
  assert.equal(calls.at(-1)[1].role, "marginalia");
  controls.destroy();
  controller.destroy();
});


test("inline shortcut editor captures remaps, updates labels, and reports conflicts", async () => {
  const {
    bindingChanges,
    calls,
    controller,
    controls,
    errors,
    host,
    scope,
  } = harness();
  controller.setSelectionTarget(image());
  controller.mount();
  controls.mount();
  const titleInput = byDataset(
    host,
    "[data-shortcut-input]",
    "shortcutInput",
    CLASSIFICATION_COMMAND_IDS.titlePage,
  );
  const titleButton = byDataset(
    host,
    "[data-command-button]",
    "classificationCommand",
    CLASSIFICATION_COMMAND_IDS.titlePage,
  );
  const status = host.querySelector(".classification-shortcut-status");

  const remapEvent = titleInput.emit("keydown", {
    key: "x",
    target: titleInput,
  });
  assert.equal(remapEvent.defaultPrevented, true);
  assert.equal(
    controller.registry.bindingFor(CLASSIFICATION_COMMAND_IDS.titlePage),
    "x",
  );
  assert.equal(titleInput.value, "X");
  assert.equal(titleButton.getAttribute("aria-keyshortcuts"), "X");
  assert.equal(
    titleButton.querySelector(".classification-command-key").textContent,
    "X",
  );
  assert.match(status.textContent, /now uses X/);
  assert.equal(bindingChanges.length, 1);

  scope.emit("keydown", { key: "x", target: scope });
  await settled();
  assert.equal(calls.length, 1);
  assert.equal(calls[0][1].category, "title_page");

  titleInput.emit("keydown", { key: "c", target: titleInput });
  assert.equal(
    controller.registry.bindingFor(CLASSIFICATION_COMMAND_IDS.titlePage),
    "x",
  );
  assert.match(status.textContent, /already used by Mark as cover/);
  assert.equal(status.dataset.error, "true");
  assert.equal(errors.at(-1).code, "key_binding_conflict");

  titleInput.emit("keydown", {
    key: "t",
    target: titleInput,
    ctrlKey: true,
  });
  assert.equal(
    controller.registry.bindingFor(CLASSIFICATION_COMMAND_IDS.titlePage),
    "ctrl+t",
  );
  assert.equal(titleInput.value, "Ctrl+T");
  assert.equal(titleButton.getAttribute("aria-keyshortcuts"), "Control+T");
});


test("shortcut fields clear, cancel, reset one command, and reset all defaults", () => {
  const { controller, controls, host } = harness();
  controller.setSelectionTarget(image());
  controls.mount();
  const titleInput = byDataset(
    host,
    "[data-shortcut-input]",
    "shortcutInput",
    CLASSIFICATION_COMMAND_IDS.titlePage,
  );
  const coverInput = byDataset(
    host,
    "[data-shortcut-input]",
    "shortcutInput",
    CLASSIFICATION_COMMAND_IDS.cover,
  );
  const titleReset = byDataset(
    host,
    "[data-reset-shortcut]",
    "resetShortcut",
    CLASSIFICATION_COMMAND_IDS.titlePage,
  );
  const resetAll = host.querySelector("[data-reset-all-shortcuts]");
  const status = host.querySelector(".classification-shortcut-status");

  titleInput.emit("keydown", { key: "Backspace", target: titleInput });
  assert.equal(
    controller.registry.bindingFor(CLASSIFICATION_COMMAND_IDS.titlePage),
    "",
  );
  assert.equal(titleInput.value, "Unassigned");

  titleReset.emit("click");
  assert.equal(
    controller.registry.bindingFor(CLASSIFICATION_COMMAND_IDS.titlePage),
    "t",
  );

  titleInput.emit("keydown", { key: "x", target: titleInput });
  titleInput.emit("keydown", { key: "Escape", target: titleInput });
  assert.equal(
    controller.registry.bindingFor(CLASSIFICATION_COMMAND_IDS.titlePage),
    "x",
    "Escape cancels capture instead of changing the existing binding",
  );
  assert.match(status.textContent, /cancelled/);

  coverInput.emit("keydown", { key: "y", target: coverInput });
  resetAll.emit("click");
  assert.equal(
    controller.registry.bindingFor(CLASSIFICATION_COMMAND_IDS.titlePage),
    "t",
  );
  assert.equal(
    controller.registry.bindingFor(CLASSIFICATION_COMMAND_IDS.cover),
    "c",
  );
  assert.match(status.textContent, /reset to their defaults/);
});


test("shortcut inputs remain typing targets and cannot invoke classification commands", async () => {
  const { calls, controller, controls, host, scope } = harness();
  controller.setSelectionTarget(image());
  controller.mount();
  controls.mount();
  const titleInput = byDataset(
    host,
    "[data-shortcut-input]",
    "shortcutInput",
    CLASSIFICATION_COMMAND_IDS.titlePage,
  );

  scope.emit("keydown", { key: "t", target: titleInput });
  await settled();
  assert.equal(calls.length, 0);
});


test("classification controls install through the standalone browser namespace", () => {
  const context = vm.createContext({});
  const staticRoot = path.join(
    __dirname,
    "..",
    "tools",
    "whl_explorer",
    "static",
    "corrections",
  );
  for (const name of [
    "commands.js",
    "keymap.js",
    "classification-controls.js",
  ]) {
    vm.runInContext(
      fs.readFileSync(path.join(staticRoot, name), "utf8"),
      context,
      { filename: name },
    );
  }
  assert.equal(
    typeof context.LibraryToolCorrections.createClassificationControls,
    "function",
  );
  assert.equal(
    context.LibraryToolCorrections.CLASSIFICATION_CONTROL_COMMAND_IDS.length,
    6,
  );
});

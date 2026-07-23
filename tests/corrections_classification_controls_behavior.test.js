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
  scope.append(host);
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
    scope,
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

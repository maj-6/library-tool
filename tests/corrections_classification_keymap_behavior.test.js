"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");

const {
  CLASSIFICATION_COMMAND_IDS,
} = require("../tools/whl_explorer/static/corrections/commands");
const {
  createClassificationController,
  eligibleKeyEvent,
  nodeInside,
} = require("../tools/whl_explorer/static/corrections/keymap");
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


function settled() {
  return new Promise((resolve) => setImmediate(resolve));
}


function keyEvent(target, values = {}) {
  return {
    key: "t",
    target,
    defaultPrevented: false,
    preventDefault() {
      this.defaultPrevented = true;
    },
    ...values,
  };
}


test("classification key events are gated while typing, repeating, or composing", () => {
  const documentRef = fakeDocument();
  documentRef.hasFocus = () => true;
  const scope = new FakeNode("main", documentRef);
  const input = new FakeNode("input", documentRef);
  const editable = new FakeNode("div", documentRef);
  editable.setAttribute("contenteditable", "true");
  const textbox = new FakeNode("div", documentRef);
  textbox.setAttribute("role", "textbox");
  scope.append(input, editable, textbox);

  const options = { scope, documentRef };
  assert.equal(eligibleKeyEvent(keyEvent(input), options), false);
  assert.equal(eligibleKeyEvent(keyEvent(editable), options), false);
  assert.equal(eligibleKeyEvent(keyEvent(textbox), options), false);
  assert.equal(eligibleKeyEvent(keyEvent(scope, { repeat: true }), options), false);
  assert.equal(eligibleKeyEvent(keyEvent(scope, { isComposing: true }), options), false);
  assert.equal(eligibleKeyEvent(keyEvent(scope, { ctrlKey: true }), options), true);
  assert.equal(eligibleKeyEvent(keyEvent(scope, { shiftKey: true }), options), true);
  assert.equal(eligibleKeyEvent(keyEvent(scope), options), true);
});


test("default bare bindings ignore modifiers while explicit modifier remaps work", async () => {
  const documentRef = fakeDocument();
  documentRef.hasFocus = () => true;
  const scope = new FakeNode("main", documentRef);
  const calls = [];
  const controller = createClassificationController({
    scope,
    documentRef,
    port: {
      async assignImageCategory(payload) {
        calls.push(payload);
        return {};
      },
    },
    operationIdFactory: () => `modifier-op-${calls.length + 1}`,
  });
  controller.setSelectionTarget(image());
  controller.mount();

  const untouched = scope.emit("keydown", {
    key: "t",
    target: scope,
    ctrlKey: true,
  });
  await settled();
  assert.equal(untouched.defaultPrevented, false);
  assert.equal(calls.length, 0);

  controller.registry.remap(
    CLASSIFICATION_COMMAND_IDS.titlePage,
    "Ctrl+T",
  );
  const remapped = scope.emit("keydown", {
    key: "t",
    target: scope,
    ctrlKey: true,
  });
  await settled();
  assert.equal(remapped.defaultPrevented, true);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].category, "title_page");
});


test("closed dialogs do not block the scope, but an open dialog does", () => {
  const documentRef = fakeDocument();
  documentRef.hasFocus = () => true;
  const scope = new FakeNode("main", documentRef);
  const dialog = new FakeNode("dialog", documentRef);
  scope.append(dialog);
  const options = { scope, documentRef };

  assert.equal(eligibleKeyEvent(keyEvent(scope), options), true);
  dialog.setAttribute("open", "");
  assert.equal(eligibleKeyEvent(keyEvent(scope), options), false);
});


test("dialogs, another window, focus loss, another scope, and canvas gestures cannot label", async () => {
  const documentRef = fakeDocument();
  let hasFocus = true;
  documentRef.hasFocus = () => hasFocus;
  const ownWindow = {};
  const scope = new FakeNode("main", documentRef);
  const outside = new FakeNode("div", documentRef);
  const calls = [];
  const errors = [];
  const controller = createClassificationController({
    scope,
    documentRef,
    windowRef: ownWindow,
    port: {
      async assignImageCategory(payload) {
        calls.push(payload);
        return {};
      },
    },
    operationIdFactory: () => "classification-op-1",
    onError: (error) => errors.push(error),
  });
  controller.setSelectionTarget(image());
  controller.mount();

  scope.emit("keydown", { key: "t", target: outside });
  scope.emit("keydown", { key: "t", target: scope, repeat: true });
  scope.emit("keydown", { key: "t", target: scope, view: {} });
  hasFocus = false;
  scope.emit("keydown", { key: "t", target: scope, view: ownWindow });
  hasFocus = true;

  const dialog = new FakeNode("div", documentRef);
  dialog.setAttribute("role", "dialog");
  scope.append(dialog);
  scope.emit("keydown", { key: "t", target: dialog, view: ownWindow });
  scope.removeChild(dialog);

  const modal = new FakeNode("div", documentRef);
  modal.setAttribute("aria-modal", "true");
  scope.append(modal);
  scope.emit("keydown", { key: "t", target: scope, view: ownWindow });
  scope.removeChild(modal);

  controller.setCanvasOwner({ active: true, tool: "perspective-corner" });
  scope.emit("keydown", { key: "t", target: scope, view: ownWindow });
  controller.setCanvasOwner(null);
  controller.setScopeActive(false);
  scope.emit("keydown", { key: "t", target: scope, view: ownWindow });
  controller.setScopeActive(true);

  await settled();
  assert.equal(calls.length, 0);
  assert.equal(errors.length, 0);

  const valid = scope.emit("keydown", {
    key: "t",
    target: scope,
    view: ownWindow,
  });
  await settled();
  assert.equal(valid.defaultPrevented, true);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].category, "title_page");
  assert.equal(errors.length, 0);
});


test("optional event eligibility gates panes while preserving an allowed soft-hover target", async () => {
  const documentRef = fakeDocument();
  documentRef.hasFocus = () => true;
  const scope = new FakeNode("main", documentRef);
  const reviewPane = new FakeNode("section", documentRef);
  const reviewAction = new FakeNode("button", documentRef);
  const imageSurface = new FakeNode("section", documentRef);
  const hoveredImage = new FakeNode("img", documentRef);
  const typingInput = new FakeNode("input", documentRef);
  reviewPane.append(reviewAction);
  imageSurface.append(hoveredImage, typingInput);
  scope.append(reviewPane, imageSurface);
  const calls = [];
  const gateCalls = [];
  const controller = createClassificationController({
    scope,
    documentRef,
    port: {
      async assignImageCategory(payload) {
        calls.push(payload);
        return {};
      },
    },
    operationIdFactory: () => "pane-scope-op-1",
    isEventEligible(event, command, context) {
      gateCalls.push({ event, command, context });
      return nodeInside(imageSurface, event.target);
    },
  });
  controller.setSelectionTarget(image());
  controller.mount();

  const disallowed = scope.emit("keydown", {
    key: "t",
    target: reviewAction,
  });
  await settled();
  assert.equal(disallowed.defaultPrevented, false);
  assert.equal(calls.length, 0);
  assert.equal(gateCalls.length, 1);

  scope.emit("keydown", { key: "t", target: typingInput });
  assert.equal(gateCalls.length, 1,
    "the pane predicate runs only after the native typing gate");

  controller.setSelectionTarget(null);
  controller.setHotTarget(image(), { element: hoveredImage });
  const allowed = scope.emit("keydown", {
    key: "t",
    target: hoveredImage,
  });
  await settled();

  assert.equal(allowed.defaultPrevented, true);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].artifactId, "scan-1");
  assert.equal(gateCalls.length, 2);
  assert.equal(
    gateCalls[1].command.id,
    CLASSIFICATION_COMMAND_IDS.titlePage,
  );
  assert.equal(gateCalls[1].context.softTarget.key, "artifact:scan-1");
});


test("selection focus can be demoted without replacing the selected target", () => {
  const documentRef = fakeDocument();
  const scope = new FakeNode("main", documentRef);
  const selectedElement = new FakeNode("button", documentRef);
  scope.append(selectedElement);
  const controller = createClassificationController({
    scope,
    documentRef,
    port: {},
  });
  const selected = image();
  const changes = [];
  controller.setSelectionTarget(selected, {
    element: selectedElement,
    focused: true,
  });
  const unsubscribe = controller.subscribe((snapshot, change) => {
    changes.push({ snapshot, change });
  });

  assert.equal(controller.setSelectionFocus(false), false);
  assert.equal(controller.stateSnapshot().selectionTarget, selected);
  assert.equal(controller.stateSnapshot().selectionFocused, false);
  assert.equal(selectedElement.dataset.classificationFocused, undefined);
  assert.equal(changes.at(-1).change.type, "selection-focus");

  assert.equal(controller.setSelectionFocus(true), true);
  assert.equal(controller.stateSnapshot().selectionTarget, selected);
  assert.equal(controller.stateSnapshot().selectionFocused, true);
  assert.equal(selectedElement.dataset.classificationFocused, "true");

  unsubscribe();
  controller.destroy();
  assert.equal(controller.setSelectionFocus(true), false);
  assert.equal(selectedElement.dataset.classificationFocused, undefined);
});


test("the Corrections M binding is scoped and cannot consume Replica's external M", async () => {
  const documentRef = fakeDocument();
  documentRef.hasFocus = () => true;
  const corrections = new FakeNode("main", documentRef);
  const replica = new FakeNode("main", documentRef);
  let roleCalls = 0;
  const controller = createClassificationController({
    scope: corrections,
    documentRef,
    port: {
      async assignRegionRole() {
        roleCalls += 1;
        return {};
      },
    },
    resolveLinkedArtifact: () => null,
    operationIdFactory: () => "role-op-1",
  });
  controller.setSelectionTarget({
    key: "annotation:region-1",
    objectType: "spatial-annotation",
    itemId: "book-1",
    id: "region-1",
    revision: "region-r1",
    label: "Margin",
    linkedKeys: [],
  });
  controller.mount();

  const event = corrections.emit("keydown", { key: "m", target: replica });
  await settled();
  assert.equal(event.defaultPrevented, false);
  assert.equal(roleCalls, 0);

  await controller.invoke(CLASSIFICATION_COMMAND_IDS.marginalia);
  assert.equal(roleCalls, 1);
});

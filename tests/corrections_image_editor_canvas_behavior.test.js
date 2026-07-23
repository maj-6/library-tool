const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const {
  POINT_ORDER,
  PROPOSAL_SCHEMA,
  TOOLS,
} = require("../tools/whl_explorer/static/corrections/image-editor-state");
const {
  correctionResourceContract,
  createPerspectiveImageRenderer,
  safeRasterUrl,
} = require("../tools/whl_explorer/static/corrections/image-editor");


class FakeNode {
  constructor(tagName, documentRef) {
    this.tagName = String(tagName).toUpperCase();
    this.nodeName = this.tagName;
    this.ownerDocument = documentRef;
    this.parentNode = null;
    this.children = [];
    this.attributes = new Map();
    this.dataset = {};
    this.listeners = new Map();
    this.className = "";
    this.textContent = "";
    this.value = "";
    this.disabled = false;
    this.tabIndex = -1;
    this.captures = new Set();
    this.rect = { left: 10, top: 20, width: 400, height: 200 };
  }

  append(...nodes) {
    for (const node of nodes) {
      node.parentNode = this;
      this.children.push(node);
    }
  }

  replaceChildren(...nodes) {
    for (const child of this.children) child.parentNode = null;
    this.children = [];
    this.append(...nodes);
  }

  setAttribute(name, value) {
    this.attributes.set(name, String(value));
  }

  getAttribute(name) {
    return this.attributes.has(name) ? this.attributes.get(name) : null;
  }

  removeAttribute(name) {
    this.attributes.delete(name);
  }

  addEventListener(type, callback) {
    const listeners = this.listeners.get(type) || [];
    listeners.push(callback);
    this.listeners.set(type, listeners);
  }

  removeEventListener(type, callback) {
    const listeners = this.listeners.get(type) || [];
    this.listeners.set(type, listeners.filter((listener) => listener !== callback));
  }

  emit(type, properties = {}) {
    const event = {
      type,
      target: this,
      currentTarget: this,
      defaultPrevented: false,
      propagationStopped: false,
      preventDefault() { this.defaultPrevented = true; },
      stopPropagation() { this.propagationStopped = true; },
      ...properties,
    };
    for (const listener of [...(this.listeners.get(type) || [])]) listener(event);
    return event;
  }

  contains(candidate) {
    if (candidate === this) return true;
    return this.children.some((child) => child.contains(candidate));
  }

  closest() {
    return ["INPUT", "TEXTAREA", "SELECT", "BUTTON", "A"].includes(this.tagName)
      ? this : null;
  }

  focus() {
    const previous = this.ownerDocument.activeElement;
    if (previous && previous !== this) previous.emit("blur", { relatedTarget: this });
    this.ownerDocument.activeElement = this;
    this.emit("focus", { relatedTarget: previous });
  }

  getBoundingClientRect() {
    return { ...this.rect };
  }

  setPointerCapture(pointerId) {
    this.captures.add(pointerId);
  }

  hasPointerCapture(pointerId) {
    return this.captures.has(pointerId);
  }

  releasePointerCapture(pointerId) {
    this.captures.delete(pointerId);
  }
}


class FakeCanvas extends FakeNode {
  constructor(documentRef) {
    super("canvas", documentRef);
    this.width = 0;
    this.height = 0;
    this.drawCalls = [];
    const methods = [
      "setTransform", "clearRect", "save", "setLineDash", "beginPath", "moveTo",
      "lineTo", "closePath", "fill", "stroke", "arc", "fillText", "restore",
    ];
    this.context = {};
    for (const method of methods) {
      this.context[method] = (...args) => this.drawCalls.push([method, ...args]);
    }
  }

  getContext(kind) {
    return kind === "2d" ? this.context : null;
  }
}


class FakeDocument {
  constructor() {
    this.activeElement = null;
    this.defaultView = {
      devicePixelRatio: 2,
      addEventListener() {},
      removeEventListener() {},
    };
  }

  createElement(name) {
    return String(name).toLowerCase() === "canvas"
      ? new FakeCanvas(this) : new FakeNode(name, this);
  }

  querySelector() {
    return null;
  }
}


class FakeResizeObserver {
  static instances = [];

  constructor(callback) {
    this.callback = callback;
    this.observed = [];
    this.disconnected = false;
    FakeResizeObserver.instances.push(this);
  }

  observe(node) {
    this.observed.push(node);
  }

  disconnect() {
    this.disconnected = true;
  }
}


function descendants(root) {
  return [root, ...root.children.flatMap(descendants)];
}


function byClass(root, className) {
  return descendants(root).filter((node) =>
    String(node.className || "").split(/\s+/).includes(className));
}


function byTag(root, tagName) {
  return descendants(root).filter((node) => node.tagName === tagName.toUpperCase());
}


function fixtureProposal(overrides = {}) {
  return {
    schema: PROPOSAL_SCHEMA,
    version: 1,
    coordinate_space: "exif_oriented_normalized",
    point_order: [...POINT_ORDER],
    quad: [[0.08, 0.12], [0.91, 0.08], [0.86, 0.94], [0.12, 0.89]],
    confidence: 0.875,
    detector: "contour",
    detector_version: "2.1.0",
    source_revision: "source-r17",
    ...overrides,
  };
}


function fixtureResource(overrides = {}) {
  return {
    id: "capture-7",
    label: "Folio 7 recto",
    kind: "captured-image",
    media_type: "image/jpeg",
    url: "/api/v1/artifacts/capture-7/raster",
    correction: {
      item_id: "book-1",
      artifact_id: "capture-7",
      artifact_revision: "artifact-r3",
      source_revision: "source-r17",
      source_sha256: "a".repeat(64),
      proposal: fixtureProposal(),
    },
    ...overrides,
  };
}


function renderHarness(options = {}, resource = fixtureResource()) {
  FakeResizeObserver.instances.length = 0;
  const documentRef = new FakeDocument();
  const container = new FakeNode("div", documentRef);
  let mountedController = null;
  const renderer = createPerspectiveImageRenderer({
    ResizeObserver: FakeResizeObserver,
    onMount(controller) {
      mountedController = controller;
      return options.mountCleanup;
    },
    ...options,
  });
  const dispose = renderer({ container, documentRef, resource, family: "image" });
  return {
    container,
    controller: mountedController || dispose.controller,
    dispose,
    documentRef,
    resource,
  };
}


function nextTurn() {
  return new Promise((resolve) => setImmediate(resolve));
}


test("state and renderer modules install through both CommonJS and browser globals", () => {
  const context = vm.createContext({});
  const root = path.join(__dirname, "..", "tools", "whl_explorer", "static", "corrections");
  vm.runInContext(fs.readFileSync(path.join(root, "image-editor-state.js"), "utf8"), context);
  vm.runInContext(fs.readFileSync(path.join(root, "image-editor.js"), "utf8"), context);
  assert.equal(
    typeof context.LibraryToolCorrections.createImageEditorState,
    "function",
  );
  assert.equal(
    typeof context.LibraryToolCorrections.createPerspectiveImageRenderer,
    "function",
  );
  assert.equal(context.LibraryToolCorrections.TOOLS.PERSPECTIVE, "perspective");
});


test("renderer exposes strict resource pins, safe raster URLs, and accessible numeric controls", () => {
  assert.deepEqual(correctionResourceContract(fixtureResource()), {
    pins: {
      item_id: "book-1",
      artifact_id: "capture-7",
      artifact_revision: "artifact-r3",
      source_revision: "source-r17",
      source_sha256: "a".repeat(64),
    },
    proposal: fixtureProposal(),
  });
  assert.equal(safeRasterUrl({ url: "javascript:alert(1)" }), "");
  assert.equal(safeRasterUrl({ url: "data:text/html,bad" }), "");
  assert.equal(safeRasterUrl({ url: "data:image/png;base64,AAAA" }),
    "data:image/png;base64,AAAA");

  const { container, controller, dispose } = renderHarness();
  assert.ok(controller);
  const surface = byClass(container, "perspective-editor")[0];
  assert.equal(surface.getAttribute("role"), "region");
  assert.match(surface.getAttribute("aria-label"), /Folio 7 recto/);

  const fieldsets = byTag(container, "fieldset");
  const legends = byTag(container, "legend");
  assert.equal(fieldsets.length, 1);
  assert.equal(legends[0].textContent, "Perspective corners");

  const inputs = byTag(container, "input");
  const labels = byTag(container, "label");
  assert.equal(inputs.length, 8);
  assert.equal(labels.length, 8);
  for (const input of inputs) {
    assert.equal(input.type, "number");
    assert.equal(input.min, "0");
    assert.equal(input.max, "1");
    assert.equal(input.step, "0.001");
    assert.equal(input.inputMode, "decimal");
    assert.match(input.getAttribute("aria-describedby"), /coordinate-hint/);
    assert.match(input.getAttribute("aria-describedby"), /validation/);
    assert.ok(labels.find((label) => label.htmlFor === input.id));
  }
  assert.equal(byClass(container, "perspective-validation-status")[0]
    .getAttribute("aria-live"), "polite");
  assert.equal(controller.canvas.tabIndex, 0);
  assert.match(controller.canvas.getAttribute("aria-label"), /four-corner/i);
  assert.ok(controller.canvas.drawCalls.some(([name]) => name === "fillText"));
  dispose();
});


test("canvas pointer interaction moves the screen-nearest vertex as one undo gesture", () => {
  const { controller, dispose } = renderHarness({
    initialTool: TOOLS.PERSPECTIVE,
  });
  const canvas = controller.canvas;
  canvas.rect = { left: 10, top: 20, width: 400, height: 200 };
  const original = controller.getState().quad.map((point) => [...point]);

  const down = canvas.emit("pointerdown", {
    pointerId: 4,
    button: 0,
    clientX: 50,
    clientY: 40,
  });
  assert.equal(down.defaultPrevented, true);
  assert.equal(canvas.hasPointerCapture(4), true);
  canvas.emit("pointermove", {
    pointerId: 4,
    clientX: 90,
    clientY: 60,
  });
  canvas.emit("pointermove", {
    pointerId: 4,
    clientX: 100,
    clientY: 64,
  });
  canvas.emit("pointerup", {
    pointerId: 4,
    clientX: 110,
    clientY: 70,
  });

  const edited = controller.getState();
  assert.deepEqual(edited.quad[0], [0.25, 0.25]);
  assert.deepEqual(edited.quad.slice(1), original.slice(1),
    "vertex identity/order is preserved");
  assert.equal(edited.undoStack.length, 1,
    "down, multiple moves, and up are one undoable gesture");
  assert.equal(edited.gesture, null);
  assert.equal(canvas.hasPointerCapture(4), false);
  assert.equal(edited.quadSource.kind, "user-edited");

  controller.dispatch({ type: "UNDO" });
  assert.deepEqual(controller.getState().quad, original);

  canvas.emit("pointerdown", {
    pointerId: 5, button: 0, clientX: 70, clientY: 50,
  });
  canvas.emit("pointermove", {
    pointerId: 5, clientX: 200, clientY: 100,
  });
  canvas.emit("pointercancel", { pointerId: 5 });
  assert.deepEqual(controller.getState().quad, original);
  assert.equal(controller.getState().undoStack.length, 0);
  dispose();
});


test("numeric corner controls are keyboard-editable, validate, commit once, and cancel", () => {
  const { container, controller, dispose, documentRef } = renderHarness({
    initialTool: TOOLS.PERSPECTIVE,
  });
  const inputs = byTag(container, "input");
  const topLeftX = inputs[0];
  const original = controller.getState().quad.map((point) => [...point]);

  topLeftX.focus();
  assert.equal(controller.getState().gesture.kind, "numeric");
  topLeftX.value = "0.2";
  topLeftX.emit("input");
  topLeftX.value = "0.22";
  topLeftX.emit("input");
  const enter = topLeftX.emit("keydown", { key: "Enter" });
  assert.equal(enter.defaultPrevented, true);
  assert.equal(controller.getState().gesture, null);
  assert.equal(controller.getState().undoStack.length, 1);
  assert.equal(controller.getState().quad[0][0], 0.22);

  topLeftX.focus();
  topLeftX.value = "not-a-number";
  topLeftX.emit("input");
  assert.equal(topLeftX.getAttribute("aria-invalid"), "true");
  assert.equal(byClass(container, "perspective-queue-button")[0].disabled, true);
  const escape = byClass(container, "perspective-editor")[0].emit("keydown", {
    key: "Escape",
    target: topLeftX,
  });
  assert.equal(escape.defaultPrevented, true);
  assert.equal(controller.getState().gesture, null);
  assert.equal(controller.getState().quad[0][0], 0.22);
  assert.equal(documentRef.activeElement, topLeftX);

  controller.dispatch({ type: "UNDO" });
  assert.deepEqual(controller.getState().quad, original);
  dispose();
});


test("toolbar and focused Space use one command path and retry the exact idempotent command", async () => {
  const invocations = [];
  let operationIds = 0;
  const invokeCommand = async (commandId, payload) => {
    invocations.push({ commandId, payload });
    if (invocations.length === 1) {
      const error = new Error("response lost");
      error.retryable = true;
      throw error;
    }
    return { job_id: "correction-transform-job-7" };
  };
  const { container, controller, dispose, documentRef } = renderHarness({
    initialTool: TOOLS.PERSPECTIVE,
    invokeCommand,
    createOperationId() {
      operationIds += 1;
      return "correction-op-stable";
    },
  });
  const queueButton = byClass(container, "perspective-queue-button")[0];
  queueButton.emit("click");
  await nextTurn();
  assert.equal(controller.getState().submission.status, "retryable");
  assert.equal(invocations.length, 1);

  controller.canvas.focus();
  assert.equal(documentRef.activeElement, controller.canvas);
  const space = controller.surface.emit("keydown", {
    key: " ",
    code: "Space",
    target: controller.canvas,
    repeat: false,
  });
  assert.equal(space.defaultPrevented, true);
  await nextTurn();

  assert.equal(invocations.length, 2);
  assert.equal(invocations[0].commandId, "corrections.transform.queue");
  assert.equal(invocations[1].commandId, "corrections.transform.queue");
  assert.equal(invocations[0].payload.trigger, "toolbar");
  assert.equal(invocations[1].payload.trigger, "shortcut");
  assert.equal(invocations[0].payload.command, invocations[1].payload.command,
    "an ambiguous retry reuses the exact command object");
  assert.equal(invocations[1].payload.command.operation_id, "correction-op-stable");
  assert.equal(operationIds, 1);
  assert.equal(controller.getState().submission.status, "queued");
  assert.equal(controller.getState().submission.jobId, "correction-transform-job-7");

  const duplicate = controller.surface.emit("keydown", {
    key: " ",
    code: "Space",
    target: controller.canvas,
    repeat: false,
  });
  await nextTurn();
  assert.equal(duplicate.defaultPrevented, false);
  assert.equal(invocations.length, 2, "a queued command cannot be duplicated");

  const input = byTag(container, "input")[0];
  input.focus();
  const formSpace = controller.surface.emit("keydown", {
    key: " ", code: "Space", target: input,
  });
  assert.equal(formSpace.defaultPrevented, false);
  assert.equal(invocations.length, 2);
  dispose();
});


test("Space gates modal, repeat, invalid pins, and an active pointer gesture", async () => {
  const calls = [];
  let modal = false;
  const invalidResource = fixtureResource({
    correction: {
      ...fixtureResource().correction,
      source_sha256: "not-a-digest",
    },
  });
  const invalid = renderHarness({
    initialTool: TOOLS.PERSPECTIVE,
    invokeCommand: async (...args) => calls.push(args),
  }, invalidResource);
  invalid.controller.canvas.focus();
  invalid.controller.surface.emit("keydown", {
    key: " ", code: "Space", target: invalid.controller.canvas,
  });
  await nextTurn();
  assert.equal(calls.length, 0);
  invalid.dispose();

  const valid = renderHarness({
    initialTool: TOOLS.PERSPECTIVE,
    invokeCommand: async (...args) => calls.push(args),
    isModalOpen: () => modal,
  });
  valid.controller.canvas.focus();
  modal = true;
  valid.controller.surface.emit("keydown", {
    key: " ", code: "Space", target: valid.controller.canvas,
  });
  modal = false;
  valid.controller.surface.emit("keydown", {
    key: " ", code: "Space", target: valid.controller.canvas, repeat: true,
  });
  valid.controller.canvas.emit("pointerdown", {
    pointerId: 8, button: 0, clientX: 50, clientY: 40,
  });
  valid.controller.surface.emit("keydown", {
    key: " ", code: "Space", target: valid.controller.canvas,
  });
  await nextTurn();
  assert.equal(calls.length, 0);
  valid.controller.canvas.emit("pointercancel", { pointerId: 8 });
  valid.dispose();
});


test("Escape uses cancel, tool-exit, and host-selection rungs in order", () => {
  let clears = 0;
  const { controller, dispose } = renderHarness({
    initialTool: TOOLS.PERSPECTIVE,
    hasSelection: () => true,
    clearSelection: () => { clears += 1; },
  });
  const canvas = controller.canvas;
  canvas.focus();
  canvas.emit("pointerdown", {
    pointerId: 2, button: 0, clientX: 80, clientY: 50,
  });
  assert.ok(controller.getState().gesture);

  const first = controller.surface.emit("keydown", {
    key: "Escape", target: canvas,
  });
  assert.equal(first.defaultPrevented, true);
  assert.equal(controller.getState().gesture, null);
  assert.equal(controller.getState().tool, TOOLS.PERSPECTIVE);

  controller.surface.emit("keydown", { key: "Escape", target: canvas });
  assert.equal(controller.getState().tool, TOOLS.SELECT);
  assert.equal(clears, 0);

  controller.surface.emit("keydown", { key: "Escape", target: canvas });
  assert.equal(clears, 1);
  assert.equal(controller.getState().selectedCorner, null);
  dispose();
});


test("renderer disposer disconnects observers and removes all owned listeners", () => {
  let mountCleanups = 0;
  const { container, dispose } = renderHarness({
    mountCleanup: () => { mountCleanups += 1; },
  });
  const nodes = descendants(container);
  const listenerCount = () => nodes.reduce(
    (total, node) => total + [...node.listeners.values()]
      .reduce((count, listeners) => count + listeners.length, 0),
    0,
  );
  assert.ok(listenerCount() > 20);
  assert.equal(FakeResizeObserver.instances.length, 1);
  assert.equal(FakeResizeObserver.instances[0].observed.length, 1);

  dispose();
  assert.equal(listenerCount(), 0);
  assert.equal(FakeResizeObserver.instances[0].disconnected, true);
  assert.equal(mountCleanups, 1);
  dispose();
  assert.equal(mountCleanups, 1, "cleanup is idempotent");
});


test("unsafe or missing raster resources render an inert unavailable state", () => {
  const documentRef = new FakeDocument();
  const container = new FakeNode("div", documentRef);
  const renderer = createPerspectiveImageRenderer();
  const dispose = renderer({
    container,
    documentRef,
    resource: fixtureResource({ url: "javascript:alert(1)" }),
  });
  assert.equal(byClass(container, "editor-unsupported").length, 1);
  assert.match(container.children[0].children[0].textContent, /unavailable/i);
  assert.equal(typeof dispose, "function");
  dispose();
});

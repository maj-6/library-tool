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
  originalBackupContract,
  safeRasterUrl,
  serializeProcessingPresetCommand,
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


function gammaOperation() {
  return {
    schema: "org.whl.raster.processing-operation",
    version: 1,
    algorithm: "gamma-v1",
    rule: "round_half_up(255 * (value/255) ** (100/gamma_hundredths)), clamped_0_255",
    gamma_hundredths: 120,
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
  assert.equal(controller.canvas.dataset.classificationCanvas, "true");
  assert.match(controller.canvas.getAttribute("aria-label"), /four-corner/i);
  assert.ok(controller.canvas.drawCalls.some(([name]) => name === "fillText"));
  dispose();
});


test("verified original actions appear only on backed-up display artifacts",
  async () => {
    const marker = {
      available: true,
      restore_available: true,
      sha256: "b".repeat(64),
      bytes: 7,
      media_type: "image/jpeg",
    };
    const display = fixtureResource({
      resourceRef: {
        id: "capture-7-display",
        revision: "display-resource-r3",
        variant: "display",
      },
      extensions: { original_backup: marker },
    });
    assert.deepEqual(originalBackupContract(display), marker);
    assert.equal(originalBackupContract({
      ...display,
      resourceRef: { ...display.resourceRef, variant: "thumbnail" },
    }), null);

    const calls = [];
    const revoked = [];
    const { container, dispose } = renderHarness({
      engine: {
        rasterArtifacts: {
          async resolveOriginalBackup(args) {
            calls.push(args);
            return {
              blob: new Blob([new Uint8Array([1, 2, 3, 4, 5, 6, 7])], {
                type: "image/jpeg",
              }),
              mediaType: "image/jpeg",
              revision: "artifact-r3",
            };
          },
          async restoreOriginalBackup() {
            throw new Error("not used");
          },
        },
      },
      objectUrls: {
        createObjectURL: () => "blob:verified-original-1",
        revokeObjectURL: (url) => revoked.push(url),
      },
    }, display);

    assert.equal(byClass(container, "perspective-view-original").length, 1);
    assert.equal(byClass(container, "perspective-restore-original").length, 1);
    byClass(container, "perspective-view-original")[0].emit("click");
    await nextTurn();

    assert.deepEqual(calls, [{
      itemId: "book-1",
      artifactId: "capture-7",
      revision: "artifact-r3",
    }]);
    const dialog = byClass(container, "perspective-original-dialog")[0];
    assert.equal(dialog.getAttribute("open"), "");
    assert.equal(byClass(container, "perspective-original-preview")[0].src,
      "blob:verified-original-1");
    assert.match(
      byClass(container, "perspective-original-status")[0].textContent,
      /verified original opened/i,
    );

    byClass(container, "perspective-original-close")[0].emit("click");
    assert.deepEqual(revoked, ["blob:verified-original-1"]);
    assert.equal(dialog.getAttribute("open"), null);
    dispose();

    const ordinary = renderHarness({}, fixtureResource({
      extensions: { original_backup: marker },
      resourceRef: {
        id: "capture-7-thumbnail",
        revision: "thumbnail-r3",
        variant: "thumbnail",
      },
    }));
    assert.equal(byClass(ordinary.container, "perspective-view-original").length, 0);
    assert.equal(byClass(ordinary.container, "perspective-restore-original").length, 0);
    ordinary.dispose();

    const restored = renderHarness({}, fixtureResource({
      resourceRef: display.resourceRef,
      extensions: {
        original_backup: { ...marker, restore_available: false },
      },
    }));
    assert.equal(byClass(restored.container, "perspective-view-original").length, 1);
    assert.equal(byClass(restored.container, "perspective-restore-original").length, 0);
    restored.dispose();
  });


test("restore original confirms, uses current CAS, and refreshes the same selection",
  async () => {
    const display = fixtureResource({
      resourceRef: {
        id: "capture-7-display",
        revision: "display-resource-r3",
        variant: "display",
      },
      extensions: {
        original_backup: {
          available: true,
          restore_available: true,
          sha256: "b".repeat(64),
          bytes: 7,
          media_type: "image/jpeg",
        },
      },
    });
    const restores = [];
    const refreshes = [];
    const confirmations = [false, true];
    const receipt = {
      operation_id: "correction:restore-original-1",
      item_id: "book-1",
      artifact_id: "capture-7",
      before_revision: "artifact-r3",
      after_revision: "artifact-original-r4",
      backup_sha256: "b".repeat(64),
      replayed: false,
    };
    const { container, dispose, resource } = renderHarness({
      engine: {
        rasterArtifacts: {
          async resolveOriginalBackup() {
            throw new Error("not used");
          },
          async restoreOriginalBackup(args) {
            restores.push(args);
            return receipt;
          },
        },
      },
      createOperationId: () => "correction:restore-original-1",
      confirmRestoreOriginal(detail) {
        assert.match(detail.message, /replace the corrected display/i);
        return confirmations.shift();
      },
      async refreshAfterOriginalRestore(detail) {
        refreshes.push(detail);
      },
    }, display);
    const restore = byClass(container, "perspective-restore-original")[0];

    restore.emit("click");
    await nextTurn();
    assert.equal(restores.length, 0, "cancelling confirmation cannot mutate");

    restore.emit("click");
    await nextTurn();
    assert.deepEqual(restores, [{
      itemId: "book-1",
      artifactId: "capture-7",
      expectedArtifactRevision: "artifact-r3",
      idempotencyKey: "correction:restore-original-1",
    }]);
    assert.equal(refreshes.length, 1);
    assert.equal(refreshes[0].itemId, "book-1");
    assert.equal(refreshes[0].artifactId, "capture-7");
    assert.equal(refreshes[0].preserveSelection, true);
    assert.equal(refreshes[0].receipt, receipt);
    assert.equal(refreshes[0].resource, resource);
    assert.match(
      byClass(container, "perspective-original-status")[0].textContent,
      /original display restored/i,
    );
    dispose();
  });


test("preset command serialization derives fresh pins and quad from its target resource", () => {
  const target = fixtureResource({
    id: "capture-9",
    correction: {
      item_id: "book-2",
      artifact_id: "capture-9",
      artifact_revision: "artifact-r9",
      source_revision: "source-r29",
      source_sha256: "b".repeat(64),
      proposal: fixtureProposal({
        source_revision: "source-r29",
        quad: [[0.1, 0.1], [0.8, 0.08], [0.9, 0.9], [0.12, 0.88]],
      }),
    },
  });
  const command = serializeProcessingPresetCommand({
    resource: target,
    preset: {
      operations: [gammaOperation()],
      adjustment: null,
    },
    operationId: "preset:target-9",
  });

  assert.equal(command.item_id, "book-2");
  assert.equal(command.artifact_id, "capture-9");
  assert.equal(command.artifact_revision, "artifact-r9");
  assert.equal(command.source_revision, "source-r29");
  assert.deepEqual(command.quad, target.correction.proposal.quad);
  assert.equal(command.adjustment, null);
  assert.deepEqual(command.operations, [gammaOperation()]);
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


test("processing operations in editor state reach the queued transform command", async () => {
  const invocations = [];
  const { container, controller, dispose } = renderHarness({
    initialTool: TOOLS.IMAGE_ADJUST,
    canQueue: () => true,
    getAdjustment: () => null,
    invokeCommand: async (commandId, payload) => {
      invocations.push({ commandId, payload });
      return { job_id: "correction-transform-job-preset" };
    },
    createOperationId: () => "correction-op-preset",
  });
  controller.dispatch({
    type: "SET_PROCESSING_OPERATIONS",
    operations: [gammaOperation()],
  });

  byClass(container, "perspective-queue-button")[0].emit("click");
  await nextTurn();

  assert.equal(invocations.length, 1);
  assert.equal(invocations[0].payload.command.adjustment, null);
  assert.deepEqual(invocations[0].payload.command.operations, [gammaOperation()]);
  assert.deepEqual(controller.getState().operations, [gammaOperation()]);
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

test("a mounted hidden command palette does not block perspective Space", async () => {
  const calls = [];
  const harness = renderHarness({
    initialTool: TOOLS.PERSPECTIVE,
    invokeCommand: async (...args) => {
      calls.push(args);
      return { job_id: "job-hidden-palette" };
    },
  });
  const palette = new FakeNode("dialog", harness.documentRef);
  palette.hidden = true;
  palette.setAttribute("role", "dialog");
  palette.setAttribute("aria-modal", "true");
  harness.documentRef.querySelectorAll = () => [palette];
  harness.controller.canvas.focus();

  const event = harness.controller.surface.emit("keydown", {
    key: " ",
    code: "Space",
    target: harness.controller.canvas,
  });
  await nextTurn();

  assert.equal(event.defaultPrevented, true);
  assert.equal(calls.length, 1);
  harness.dispose();
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
  // The stage drives canvas redraws; the viewport drives the --pane-w/-h
  // republication that caps the image by its pane.
  assert.equal(FakeResizeObserver.instances[0].observed.length, 2);

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

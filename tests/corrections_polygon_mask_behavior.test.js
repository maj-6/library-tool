const assert = require("node:assert/strict");
const test = require("node:test");

const {
  COORDINATE_SPACE,
  FREEHAND_MIN_STEP,
  MAX_MASK_POLYGON_POINTS,
  POINT_ORDER,
  POLYGON_CLOSE_DISTANCE,
  PROPOSAL_SCHEMA,
  TOOLS,
  createImageEditorState,
  reduceImageEditorState,
  serializeCorrectionTransformCommand,
  validateMaskPolygon,
} = require("../tools/whl_explorer/static/corrections/image-editor-state");
const {
  TOOL_DEFINITIONS,
  createPerspectiveImageRenderer,
} = require("../tools/whl_explorer/static/corrections/image-editor");


const TRIANGLE_MASK = [[0.1, 0.1], [0.9, 0.2], [0.5, 0.9]];
const FULL_FRAME_ENTRY_QUAD = [[0, 0], [1, 0], [1, 1], [0, 1]];


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


function descendants(root) {
  return [root, ...root.children.flatMap(descendants)];
}


function byClass(root, className) {
  return descendants(root).filter((node) =>
    String(node.className || "").split(/\s+/).includes(className));
}


function proposal(overrides = {}) {
  return {
    schema: PROPOSAL_SCHEMA,
    version: 1,
    coordinate_space: COORDINATE_SPACE,
    point_order: [...POINT_ORDER],
    quad: [[0.08, 0.12], [0.91, 0.08], [0.86, 0.94], [0.12, 0.89]],
    confidence: 0.875,
    detector: "contour",
    detector_version: "2.1.0",
    source_revision: "source-r17",
    ...overrides,
  };
}


function pins(overrides = {}) {
  return {
    item_id: "book-1",
    artifact_id: "capture-7",
    artifact_revision: "artifact-r3",
    source_revision: "source-r17",
    source_sha256: "a".repeat(64),
    ...overrides,
  };
}


function maskState(overrides = {}) {
  return createImageEditorState({
    proposal: proposal(),
    sourceRevision: "source-r17",
    tool: TOOLS.POLYGON,
    hasSelection: true,
    ...overrides,
  });
}


function resource(overrides = {}) {
  return {
    id: "capture-7",
    label: "Folio 7 recto",
    kind: "captured-image",
    media_type: "image/jpeg",
    url: "/api/v1/artifacts/capture-7/raster",
    correction: { ...pins(), proposal: proposal() },
    ...overrides,
  };
}


function traceMask(state, ring, freehand = false) {
  let next = reduceImageEditorState(state, {
    type: "MASK_BEGIN", point: ring[0], freehand,
  });
  for (const point of ring.slice(1)) {
    next = reduceImageEditorState(next, { type: "MASK_EXTEND", point });
  }
  return next;
}


function commitMask(state) {
  return reduceImageEditorState(state, { type: "MASK_COMMIT" });
}


function freehandSample(index) {
  return [
    0.05 + (index % 100) * 0.009,
    0.05 + Math.floor(index / 100) * 0.09,
  ];
}


function maskHarness(options = {}) {
  const documentRef = new FakeDocument();
  const container = new FakeNode("div", documentRef);
  const renderer = createPerspectiveImageRenderer({
    initialTool: TOOLS.POLYGON,
    ...options,
  });
  const dispose = renderer({
    container, documentRef, resource: resource(), family: "image",
  });
  const controller = dispose.controller;
  controller.canvas.rect = { left: 0, top: 0, width: 400, height: 400 };
  return { container, controller, dispose, documentRef };
}


function clickCanvas(canvas, pointerId, clientX, clientY) {
  canvas.emit("pointerdown", { pointerId, button: 0, clientX, clientY });
  canvas.emit("pointerup", { pointerId, clientX, clientY });
}


function nextTurn() {
  return new Promise((resolve) => setImmediate(resolve));
}


test("a new image editor state carries no mask and no draft", () => {
  const state = maskState();
  assert.equal(state.tool, TOOLS.POLYGON);
  assert.equal(state.maskPolygon, null);
  assert.equal(state.maskDraft, null);
  assert.equal(state.maskFreehand, false);
  assert.deepEqual(state.undoStack, []);
  assert.deepEqual(state.redoStack, []);
});


test("a traced ring becomes a committed mask polygon", () => {
  let state = reduceImageEditorState(maskState(), {
    type: "MASK_BEGIN", point: TRIANGLE_MASK[0], freehand: false,
  });
  assert.deepEqual(state.maskDraft, [TRIANGLE_MASK[0]]);
  assert.equal(state.maskPolygon, null, "an open draft is not an applied mask");

  state = reduceImageEditorState(state, {
    type: "MASK_EXTEND", point: TRIANGLE_MASK[1],
  });
  state = reduceImageEditorState(state, {
    type: "MASK_EXTEND", point: TRIANGLE_MASK[2],
  });
  assert.equal(state.maskDraft.length, 3);

  state = reduceImageEditorState(state, { type: "MASK_COMMIT" });
  assert.deepEqual(state.maskPolygon, TRIANGLE_MASK);
  assert.equal(state.maskDraft, null);
  assert.equal(state.maskFreehand, false);
  assert.equal(state.undoStack.length, 1);
  assert.equal(state.undoStack[0].maskPolygon, null,
    "the undo entry remembers there was no mask before");
  assert.deepEqual(state.redoStack, []);
});


test("committing a ring the engine would reject discards the draft and keeps the applied mask", () => {
  const applied = commitMask(traceMask(maskState(), TRIANGLE_MASK));
  assert.deepEqual(applied.maskPolygon, TRIANGLE_MASK);

  for (const [ring, code, reason] of [
    [[[0, 0], [1, 0], [0, 1], [1, 1]], "polygon-self-intersection", "a bowtie"],
    [[[0, 0], [0.001, 0], [0, 0.001]], "polygon-area", "a degenerate sliver"],
    [[[0.2, 0.2], [0.8, 0.2]], "polygon-point-count", "a two-point ring"],
  ]) {
    assert.equal(validateMaskPolygon(ring).code, code, reason);
    const attempted = commitMask(traceMask(applied, ring));
    assert.equal(attempted.maskDraft, null, reason);
    assert.equal(attempted.maskFreehand, false, reason);
    assert.deepEqual(attempted.maskPolygon, TRIANGLE_MASK, reason);
    assert.equal(attempted.undoStack.length, applied.undoStack.length, reason);
    assert.deepEqual(attempted.redoStack, applied.redoStack, reason);
  }
});


test("cancelling a draft keeps the mask that was already committed", () => {
  const applied = commitMask(traceMask(maskState(), TRIANGLE_MASK));
  const drafting = traceMask(applied, [[0.3, 0.3], [0.7, 0.3], [0.5, 0.7]]);
  assert.equal(drafting.maskDraft.length, 3);

  const cancelled = reduceImageEditorState(drafting, { type: "MASK_CANCEL" });
  assert.equal(cancelled.maskDraft, null);
  assert.equal(cancelled.maskFreehand, false);
  assert.deepEqual(cancelled.maskPolygon, TRIANGLE_MASK);
  assert.equal(cancelled.undoStack.length, applied.undoStack.length);
  assert.equal(reduceImageEditorState(cancelled, { type: "MASK_CANCEL" }), cancelled,
    "cancelling with no draft open is inert");
});


test("clearing a mask is undoable and clearing nothing does not grow the undo stack", () => {
  const empty = maskState();
  assert.equal(reduceImageEditorState(empty, { type: "MASK_CLEAR" }), empty,
    "clearing an already empty mask is a no-op");

  const applied = commitMask(traceMask(empty, TRIANGLE_MASK));
  const cleared = reduceImageEditorState(applied, { type: "MASK_CLEAR" });
  assert.equal(cleared.maskPolygon, null);
  assert.equal(cleared.undoStack.length, applied.undoStack.length + 1);
  assert.deepEqual(
    cleared.undoStack[cleared.undoStack.length - 1].maskPolygon,
    TRIANGLE_MASK,
  );

  assert.equal(reduceImageEditorState(cleared, { type: "MASK_CLEAR" }), cleared,
    "a second clear does not grow the undo stack");
  assert.deepEqual(
    reduceImageEditorState(cleared, { type: "UNDO" }).maskPolygon,
    TRIANGLE_MASK,
  );
});


test("undo and redo restore the mask alongside the quad and tolerate a maskless entry", () => {
  let state = maskState();
  const originalQuad = state.quad.map((point) => [...point]);

  state = reduceImageEditorState(state, {
    type: "BEGIN_GESTURE", kind: "numeric", cornerIndex: 0,
  });
  state = reduceImageEditorState(state, { type: "MOVE_CORNER", point: [0.2, 0.2] });
  state = reduceImageEditorState(state, { type: "COMMIT_GESTURE" });
  const editedQuad = state.quad.map((point) => [...point]);

  state = commitMask(traceMask(state, TRIANGLE_MASK));
  assert.equal(state.undoStack.length, 2);

  state = reduceImageEditorState(state, { type: "UNDO" });
  assert.equal(state.maskPolygon, null, "undoing the mask commit removes the mask");
  assert.deepEqual(state.quad, editedQuad, "the quad edit is left alone");

  state = reduceImageEditorState(state, { type: "UNDO" });
  assert.deepEqual(state.quad, originalQuad);
  assert.equal(state.maskPolygon, null);

  state = reduceImageEditorState(state, { type: "REDO" });
  assert.deepEqual(state.quad, editedQuad);
  assert.equal(state.maskPolygon, null);

  state = reduceImageEditorState(state, { type: "REDO" });
  assert.deepEqual(state.quad, editedQuad);
  assert.deepEqual(state.maskPolygon, TRIANGLE_MASK);
  assert.deepEqual(state.redoStack, []);
  assert.equal(state.undoStack.length, 2);

  const legacy = {
    ...state,
    undoStack: [{
      quad: FULL_FRAME_ENTRY_QUAD.map((point) => [...point]),
      quadSource: { ...state.quadSource },
    }],
    redoStack: [],
  };
  const restored = reduceImageEditorState(legacy, { type: "UNDO" });
  assert.equal(restored.maskPolygon, null,
    "an entry written before masking existed restores as no mask");
  assert.deepEqual(restored.quad, FULL_FRAME_ENTRY_QUAD);
});


test("undo and redo are refused while a draft is open", () => {
  let state = commitMask(traceMask(maskState(), TRIANGLE_MASK));
  state = reduceImageEditorState(state, { type: "MASK_CLEAR" });
  state = reduceImageEditorState(state, { type: "UNDO" });
  assert.equal(state.undoStack.length, 1);
  assert.equal(state.redoStack.length, 1);

  const drafting = traceMask(state, [[0.3, 0.3], [0.7, 0.3], [0.5, 0.7]]);
  assert.equal(reduceImageEditorState(drafting, { type: "UNDO" }), drafting);
  assert.equal(reduceImageEditorState(drafting, { type: "REDO" }), drafting);

  const settled = reduceImageEditorState(drafting, { type: "MASK_CANCEL" });
  assert.equal(reduceImageEditorState(settled, { type: "UNDO" }).undoStack.length, 0);
});


test("freehand tracing drops samples below the minimum step and stops at the point ceiling", () => {
  const nudge = FREEHAND_MIN_STEP / 2;
  let state = reduceImageEditorState(maskState(), {
    type: "MASK_BEGIN", point: [0.2, 0.2], freehand: true,
  });
  assert.equal(state.maskFreehand, true);
  state = reduceImageEditorState(state, {
    type: "MASK_EXTEND", point: [0.2 + nudge, 0.2],
  });
  assert.equal(state.maskDraft.length, 1, "a sample inside the freehand step is dropped");
  state = reduceImageEditorState(state, {
    type: "MASK_EXTEND", point: [0.2 + FREEHAND_MIN_STEP * 3, 0.2],
  });
  assert.equal(state.maskDraft.length, 2);

  const clicked = reduceImageEditorState(reduceImageEditorState(maskState(), {
    type: "MASK_BEGIN", point: [0.2, 0.2], freehand: false,
  }), { type: "MASK_EXTEND", point: [0.2 + nudge, 0.2] });
  assert.equal(clicked.maskDraft.length, 2,
    "click placement keeps a deliberately close vertex");

  let traced = reduceImageEditorState(maskState(), {
    type: "MASK_BEGIN", point: freehandSample(0), freehand: true,
  });
  for (let index = 1; index < MAX_MASK_POLYGON_POINTS + 100; index += 1) {
    traced = reduceImageEditorState(traced, {
      type: "MASK_EXTEND", point: freehandSample(index),
    });
  }
  assert.equal(traced.maskDraft.length, MAX_MASK_POLYGON_POINTS);
});


test("mask drafts only begin under the polygon tool and tool ids stay closed", () => {
  const select = createImageEditorState({
    proposal: proposal(), sourceRevision: "source-r17",
  });
  assert.equal(select.tool, TOOLS.SELECT);
  assert.equal(reduceImageEditorState(select, {
    type: "MASK_BEGIN", point: [0.5, 0.5], freehand: false,
  }), select, "MASK_BEGIN outside the polygon tool is inert");
  assert.equal(reduceImageEditorState(select, {
    type: "MASK_EXTEND", point: [0.5, 0.5],
  }), select, "MASK_EXTEND without a draft is inert");

  assert.throws(() => reduceImageEditorState(select, {
    type: "SET_TOOL", tool: "lasso",
  }), /unknown image editor tool/);
  const polygon = reduceImageEditorState(select, {
    type: "SET_TOOL", tool: TOOLS.POLYGON,
  });
  assert.equal(polygon.tool, "polygon");
  assert.equal(reduceImageEditorState(polygon, {
    type: "MASK_BEGIN", point: [0.5, 0.5],
  }).maskDraft.length, 1);
});


test("a committed mask serializes as mask_polygon and no mask leaves the command untouched", () => {
  const base = {
    pins: pins(),
    quad: proposal().quad,
    adjustment: null,
    rerunOcr: false,
    operationId: "correction-op-1",
  };
  const masked = commitMask(traceMask(maskState(), TRIANGLE_MASK));
  const command = serializeCorrectionTransformCommand({
    ...base, maskPolygon: masked.maskPolygon,
  });
  assert.deepEqual(command.mask_polygon, TRIANGLE_MASK);
  assert.equal(Object.keys(command).length, 12);

  const bare = serializeCorrectionTransformCommand({
    ...base, maskPolygon: maskState().maskPolygon,
  });
  assert.equal(Object.prototype.hasOwnProperty.call(bare, "mask_polygon"), false);
  assert.deepEqual(Object.keys(bare), [
    "schema", "version", "item_id", "artifact_id", "artifact_revision",
    "source_revision", "source_sha256", "quad", "adjustment", "rerun_ocr",
    "operation_id",
  ]);
  assert.equal(Object.keys(bare).length, 11);
});


test("the toolbar offers a mask tool that switches the editor into polygon mode", () => {
  assert.deepEqual(TOOL_DEFINITIONS.map((definition) => definition.id), [
    "select", "perspective", "image-adjust", "polygon",
  ]);
  assert.deepEqual(
    TOOL_DEFINITIONS.find((definition) => definition.id === TOOLS.POLYGON),
    { id: "polygon", label: "Mask", shortcut: "" },
  );

  const { container, controller, dispose } = maskHarness({ initialTool: TOOLS.SELECT });
  const maskButton = byClass(container, "perspective-tool-button")
    .find((button) => button.dataset.imageTool === TOOLS.POLYGON);
  assert.ok(maskButton, "the toolbar renders a mask tool button");
  assert.equal(maskButton.textContent, "Mask");
  assert.equal(maskButton.getAttribute("aria-pressed"), "false");

  maskButton.emit("click");
  assert.equal(controller.getState().tool, TOOLS.POLYGON);
  assert.equal(maskButton.getAttribute("aria-pressed"), "true");
  dispose();
});


test("a freehand drag on the canvas commits a mask polygon", () => {
  const { controller, dispose } = maskHarness();
  const canvas = controller.canvas;

  const down = canvas.emit("pointerdown", {
    pointerId: 4, button: 0, clientX: 40, clientY: 40,
  });
  assert.equal(down.defaultPrevented, true);
  assert.equal(canvas.hasPointerCapture(4), true);
  assert.deepEqual(controller.getState().maskDraft, [[0.1, 0.1]]);
  assert.equal(controller.getState().maskFreehand, false,
    "a press alone is not yet a freehand trace");

  canvas.emit("pointermove", { pointerId: 4, clientX: 360, clientY: 40 });
  assert.equal(controller.getState().maskFreehand, true,
    "the first move promotes the draft to freehand");
  canvas.emit("pointermove", { pointerId: 4, clientX: 200, clientY: 360 });
  assert.equal(controller.getState().maskDraft.length, 3);
  assert.equal(controller.getState().maskPolygon, null);
  assert.ok(canvas.drawCalls.some(([name, pattern]) =>
    name === "setLineDash" && Array.isArray(pattern) &&
    pattern[0] === 6 && pattern[1] === 4),
  "an open draft is drawn dashed");

  canvas.emit("pointerup", { pointerId: 4, clientX: 200, clientY: 360 });
  const committed = controller.getState();
  assert.deepEqual(committed.maskPolygon, [[0.1, 0.1], [0.9, 0.1], [0.5, 0.9]]);
  assert.equal(committed.maskDraft, null);
  assert.equal(committed.maskFreehand, false);
  assert.equal(committed.undoStack.length, 1);
  assert.equal(canvas.hasPointerCapture(4), false);
  assert.ok(canvas.drawCalls.some(([name, x, y]) =>
    name === "moveTo" && x === 40 && y === 40),
  "the mask ring is drawn in canvas space");
  dispose();
});


test("clicked vertices close into a mask when the ring is clicked shut", () => {
  const { controller, dispose } = maskHarness();
  const canvas = controller.canvas;

  clickCanvas(canvas, 1, 80, 80);
  assert.equal(controller.getState().maskDraft.length, 1);
  assert.equal(controller.getState().maskFreehand, false);
  clickCanvas(canvas, 2, 320, 80);
  clickCanvas(canvas, 3, 200, 320);
  assert.equal(controller.getState().maskDraft.length, 3);
  assert.equal(controller.getState().maskPolygon, null, "an open ring is never applied");

  clickCanvas(canvas, 4, 100, 140);
  assert.equal(controller.getState().maskDraft.length, 4,
    "a click outside the close distance places another vertex");
  assert.ok(Math.hypot(0.25 - 0.2, 0.35 - 0.2) > POLYGON_CLOSE_DISTANCE);

  clickCanvas(canvas, 5, 82, 82);
  const committed = controller.getState();
  assert.deepEqual(committed.maskPolygon, [
    [0.2, 0.2], [0.8, 0.2], [0.5, 0.8], [0.25, 0.35],
  ]);
  assert.equal(committed.maskDraft, null);
  assert.equal(committed.undoStack.length, 1);
  dispose();
});


test("escape cancels an open draft and enter commits it", () => {
  const { controller, dispose } = maskHarness();
  const canvas = controller.canvas;

  clickCanvas(canvas, 1, 80, 80);
  clickCanvas(canvas, 2, 320, 80);
  clickCanvas(canvas, 3, 200, 320);
  const escape = controller.surface.emit("keydown", { key: "Escape", target: canvas });
  assert.equal(escape.defaultPrevented, true);
  assert.equal(controller.getState().maskDraft, null);
  assert.equal(controller.getState().maskPolygon, null);
  assert.equal(controller.getState().tool, TOOLS.POLYGON,
    "escape is spent on the draft before it leaves the tool");
  assert.equal(controller.getState().undoStack.length, 0);

  clickCanvas(canvas, 4, 80, 80);
  clickCanvas(canvas, 5, 320, 80);
  clickCanvas(canvas, 6, 200, 320);
  const enter = controller.surface.emit("keydown", { key: "Enter", target: canvas });
  assert.equal(enter.defaultPrevented, true);
  assert.deepEqual(controller.getState().maskPolygon, [
    [0.2, 0.2], [0.8, 0.2], [0.5, 0.8],
  ]);
  assert.equal(controller.getState().maskDraft, null);
  dispose();
});


test("a mask committed on the canvas is carried into the queued transform command", async () => {
  const invocations = [];
  const { container, controller, dispose } = maskHarness({
    invokeCommand: async (commandId, payload) => {
      invocations.push({ commandId, payload });
      return { job_id: "correction-transform-job-mask" };
    },
    createOperationId: () => "correction-op-mask",
  });
  const canvas = controller.canvas;
  const traced = [[0.1, 0.1], [0.9, 0.1], [0.5, 0.9]];

  canvas.emit("pointerdown", { pointerId: 7, button: 0, clientX: 40, clientY: 40 });
  canvas.emit("pointermove", { pointerId: 7, clientX: 360, clientY: 40 });
  canvas.emit("pointermove", { pointerId: 7, clientX: 200, clientY: 360 });
  canvas.emit("pointerup", { pointerId: 7, clientX: 200, clientY: 360 });
  assert.deepEqual(controller.getState().maskPolygon, traced);

  byClass(container, "perspective-tool-button")
    .find((button) => button.dataset.imageTool === TOOLS.PERSPECTIVE)
    .emit("click");
  assert.deepEqual(controller.getState().maskPolygon, traced,
    "changing tools does not drop an applied mask");

  byClass(container, "perspective-queue-button")[0].emit("click");
  await nextTurn();
  assert.equal(invocations.length, 1);
  assert.equal(invocations[0].commandId, "corrections.transform.queue");
  assert.deepEqual(invocations[0].payload.command.mask_polygon, traced);
  assert.equal(controller.getState().submission.status, "queued");
  dispose();
});

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const {
  MAX_OPERATIONS_PER_RECIPE,
  POINT_ORDER,
  PROPOSAL_SCHEMA,
  TOOLS,
  createImageEditorState,
  normalizeProcessingOperations,
  reduceImageEditorState,
  serializeCorrectionTransformCommand,
} = require("../tools/whl_explorer/static/corrections/image-editor-state");
const {
  BINARY_ALGORITHM,
  BRIGHTNESS_MAX,
  BRIGHTNESS_MIN,
  CONTRAST_MAX,
  CONTRAST_MIN,
  DEFAULT_CONTRAST,
  IMAGE_ADJUST_PROFILE_KEY,
  PROCESSING_OPERATION_DEFAULTS,
  THRESHOLD_RULE,
  applyManualBinaryPreview,
  canApplyWheel,
  canEnterImageAdjust,
  canQueueImageAdjustShortcut,
  clampOperationParameter,
  composeImageAdjustRendererOptions,
  createImageAdjustTool,
  createManualBinaryAdjustment,
  createProcessingOperation,
  normalizeImageAdjustProfile,
  renderBinaryCanvasPreview,
  serializeImageAdjustProfile,
  thresholdForBrightness,
} = require("../tools/whl_explorer/static/corrections/image-adjust-tool");
const {
  FakeNode,
  fakeDocument,
} = require("./fixtures/corrections_fake_dom");
const {
  createPresetPanel,
} = require("../tools/whl_explorer/static/corrections/preset-panel");

const parityFixture = JSON.parse(fs.readFileSync(path.join(
  __dirname,
  "fixtures",
  "manual_binary_adjust_parity.json",
), "utf8"));

const canonicalOperations = JSON.parse(fs.readFileSync(path.join(
  __dirname,
  "fixtures",
  "processing_operations_canonical.json",
), "utf8"));


function pins() {
  return {
    item_id: "book-1",
    artifact_id: "capture-7",
    artifact_revision: "artifact-r3",
    source_revision: "source-r17",
    source_sha256: "a".repeat(64),
  };
}


function editorState(overrides = {}) {
  return createImageEditorState({
    proposal: {
      schema: PROPOSAL_SCHEMA,
      version: 1,
      coordinate_space: "exif_oriented_normalized",
      point_order: [...POINT_ORDER],
      quad: [[0.08, 0.12], [0.91, 0.08], [0.86, 0.94], [0.12, 0.89]],
      confidence: 0.875,
      detector: "contour",
      detector_version: "2.1.0",
      source_revision: "source-r17",
    },
    sourceRevision: "source-r17",
    tool: TOOLS.SELECT,
    hasSelection: true,
    ...overrides,
  });
}


function shortcutContext(overrides = {}) {
  return {
    key: "a",
    target: { tagName: "CANVAS" },
    canvasFocused: true,
    canvasTarget: true,
    modalOpen: false,
    rectangleEditing: false,
    formControl: false,
    repeat: false,
    isComposing: false,
    altKey: false,
    ctrlKey: false,
    metaKey: false,
    shiftKey: false,
    defaultPrevented: false,
    ...overrides,
  };
}


function command(operationId, brightness, rerunOcr = false) {
  return serializeCorrectionTransformCommand({
    pins: pins(),
    quad: editorState({ tool: TOOLS.IMAGE_ADJUST }).quad,
    adjustment: createManualBinaryAdjustment(brightness),
    rerunOcr,
    operationId,
  });
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


function committedResult(operationId, ocrState = "not_requested") {
  return {
    job_id: `job-${operationId}`,
    operation_id: operationId,
    image_commit: {
      operation_id: operationId,
      outputs: [
        {
          kind: "corrected-display",
          artifact_id: "corrected-display-1",
        },
        { kind: "ocr-ready", artifact_id: "ocr-ready-1" },
        { kind: "thumbnail", artifact_id: "thumbnail-1" },
        { kind: "transform-manifest", artifact_id: "manifest-1" },
      ],
    },
    ocr_followup: {
      state: ocrState,
      source: ocrState === "not_requested"
        ? null : { kind: "ocr-ready", artifact_id: "ocr-ready-1" },
      proposal_ref: ocrState === "succeeded" ? "ocr-proposal-1" : "",
      failure: ocrState === "failed"
        ? { code: "ocr_followup_failed", message: "provider unavailable" }
        : null,
    },
    cancelled_before_commit: false,
  };
}


function descendants(root) {
  return [root, ...root.children.flatMap(descendants)];
}


function byClass(root, className) {
  return descendants(root).filter((node) =>
    String(node.className || "").split(/\s+/).includes(className));
}


function activateImageAdjust(harness) {
  harness.controller.dispatch({ type: "SET_TOOL", tool: TOOLS.IMAGE_ADJUST });
  harness.tool.syncEditorState(
    harness.controller.getState(),
    harness.controller.resource,
  );
}


function operationsEditor(harness) {
  const panel = byClass(harness.inspector, "image-adjust-panel")[0];
  return {
    panel,
    select: byClass(panel, "image-adjust-operation-add-select")[0],
    add: byClass(panel, "image-adjust-operation-add")[0],
    hint: byClass(panel, "image-adjust-operations-hint")[0],
    rows: () => byClass(panel, "image-adjust-operation"),
  };
}


function mountedHarness(options = {}) {
  const documentRef = fakeDocument();
  documentRef.defaultView = null;
  documentRef.querySelector = () => null;
  documentRef.listeners = new Map();
  documentRef.addEventListener = (type, listener) => {
    const listeners = documentRef.listeners.get(type) || [];
    listeners.push(listener);
    documentRef.listeners.set(type, listeners);
  };
  documentRef.removeEventListener = (type, listener) => {
    documentRef.listeners.set(
      type,
      (documentRef.listeners.get(type) || [])
        .filter((candidate) => candidate !== listener),
    );
  };
  documentRef.emit = (type, values = {}) => {
    const event = {
      type,
      target: values.target || documentRef,
      currentTarget: documentRef,
      defaultPrevented: false,
      preventDefault() { this.defaultPrevented = true; },
      stopPropagation() { this.propagationStopped = true; },
      ...values,
    };
    for (const listener of documentRef.listeners.get(type) || []) listener(event);
    return event;
  };
  const surface = new FakeNode("section", documentRef);
  const canvas = new FakeNode("canvas", documentRef);
  const inspector = new FakeNode("aside", documentRef);
  const toolbar = new FakeNode("header", documentRef);
  const adjustButton = new FakeNode("button", documentRef);
  adjustButton.dataset.imageTool = TOOLS.IMAGE_ADJUST;
  toolbar.append(adjustButton);
  const imageStage = new FakeNode("div", documentRef);
  const image = new FakeNode("img", documentRef);
  image.naturalWidth = 400;
  image.naturalHeight = 200;
  imageStage.append(image, canvas);
  let state = editorState();
  const queueCalls = [];
  const controller = {
    canvas,
    image,
    inspector,
    resource: { id: "capture-7" },
    surface,
    toolbar,
    dispatch(action) {
      state = reduceImageEditorState(state, action);
      return state;
    },
    getPins: pins,
    getState: () => state,
    requestQueue(trigger) {
      queueCalls.push(trigger);
      return Promise.resolve({ job_id: "job-1" });
    },
  };
  const previewCalls = [];
  const tool = createImageAdjustTool({
    previewAdapter(args) {
      previewCalls.push(args.adjustment);
      return { width: 400, height: 200 };
    },
    ...options,
  });
  const cleanup = tool.mount(controller, controller.resource);
  return {
    adjustButton,
    canvas,
    cleanup,
    controller,
    documentRef,
    image,
    imageStage,
    inspector,
    previewCalls,
    queueCalls,
    surface,
    tool,
  };
}


test("profile value is pure, bounded, and stable under serialization", () => {
  assert.equal(IMAGE_ADJUST_PROFILE_KEY, "imageAdjust");
  assert.deepEqual(normalizeImageAdjustProfile(null), {
    lastAppliedBrightness: 0,
  });
  assert.deepEqual(normalizeImageAdjustProfile({ lastAppliedBrightness: -37 }), {
    lastAppliedBrightness: -37,
  });
  for (const invalid of [-101, 101, 2.5, "12", true, Number.NaN]) {
    assert.deepEqual(
      normalizeImageAdjustProfile({ lastAppliedBrightness: invalid }),
      { lastAppliedBrightness: 0 },
    );
  }
  const tool = createImageAdjustTool({
    profile: { lastAppliedBrightness: 18 },
  });
  assert.deepEqual(serializeImageAdjustProfile(tool), {
    lastAppliedBrightness: 18,
  });
  const serialized = tool.serializeProfile();
  serialized.lastAppliedBrightness = 99;
  assert.deepEqual(tool.serializeProfile(), { lastAppliedBrightness: 18 });
});


test("external profile refresh preserves an active draft until the next editor session", () => {
  const harness = mountedHarness({
    profile: { lastAppliedBrightness: 5 },
  });
  harness.controller.dispatch({
    type: "SET_TOOL",
    tool: TOOLS.IMAGE_ADJUST,
  });
  harness.tool.syncEditorState(
    harness.controller.getState(),
    harness.controller.resource,
  );
  harness.tool.setBrightness(16);

  harness.tool.restoreProfile({ lastAppliedBrightness: 37 });
  assert.equal(harness.tool.getState().brightness, 16);
  assert.equal(harness.tool.getState().rememberedBrightness, 37);

  harness.cleanup();
  const cleanup = harness.tool.mount(
    harness.controller,
    harness.controller.resource,
  );
  assert.equal(harness.tool.getState().brightness, 37);
  cleanup();
});


test("external profile refresh updates an inactive mounted editor immediately", () => {
  const harness = mountedHarness({
    profile: { lastAppliedBrightness: 5 },
  });
  assert.equal(harness.controller.getState().tool, TOOLS.SELECT);

  harness.tool.restoreProfile({ lastAppliedBrightness: 37 });

  assert.equal(harness.tool.getState().brightness, 37);
  assert.equal(harness.tool.getState().rememberedBrightness, 37);
  assert.match(
    byClass(harness.inspector, "image-adjust-threshold")[0].textContent,
    /brightness 37/,
  );

  harness.canvas.focus();
  harness.surface.emit("keydown", {
    key: "a",
    target: harness.canvas,
  });
  assert.equal(harness.controller.getState().tool, TOOLS.IMAGE_ADJUST);
  assert.equal(harness.tool.getState().brightness, 37);
  harness.cleanup();
});


test("a mounted hidden command palette does not block the A shortcut", () => {
  const harness = mountedHarness();
  const palette = new FakeNode("dialog", harness.documentRef);
  palette.hidden = true;
  palette.setAttribute("role", "dialog");
  palette.setAttribute("aria-modal", "true");
  harness.documentRef.querySelectorAll = () => [palette];
  harness.canvas.focus();

  const event = harness.surface.emit("keydown", {
    key: "a",
    target: harness.canvas,
  });

  assert.equal(event.defaultPrevented, true);
  assert.equal(harness.controller.getState().tool, TOOLS.IMAGE_ADJUST);
  harness.cleanup();
});


test("canonical recipe and threshold exactly match the production wire contract", () => {
  assert.equal(DEFAULT_CONTRAST, 100);
  assert.equal(BINARY_ALGORITHM, "grayscale-threshold-blend-v1");
  assert.equal(
    THRESHOLD_RULE,
    "round_half_up(127.5 - brightness_percent * 1.275), clamped_0_255",
  );
  assert.deepEqual(
    [-100, -50, 0, 50, 100].map(thresholdForBrightness),
    [255, 191, 128, 64, 0],
  );
  assert.deepEqual(createManualBinaryAdjustment(0), {
    schema: "org.whl.raster.manual-binary-adjust",
    version: 1,
    algorithm: "grayscale-threshold-blend-v1",
    contrast_percent: 100,
    brightness_percent: 0,
    threshold: 128,
    threshold_rule:
      "round_half_up(127.5 - brightness_percent * 1.275), clamped_0_255",
    comparison: "grayscale_value > threshold",
  });
  assert.throws(() => thresholdForBrightness(2.5), /integer/);
  assert.throws(() => createManualBinaryAdjustment(101), /-100 through 100/);
});


test("contrast 100 preview is truly binary and mirrors Pillow alpha and RGB-to-L", () => {
  const input = new Uint8ClampedArray([
    255, 0, 0, 255,
    0, 255, 0, 255,
    0, 0, 255, 255,
    0, 0, 0, 0,
    0, 0, 0, 128,
    20, 100, 200, 127,
    128, 128, 128, 255,
  ]);
  const output = applyManualBinaryPreview(
    input,
    createManualBinaryAdjustment(0),
  );
  assert.deepEqual(Array.from(output), [
    0, 0, 0, 255,
    255, 255, 255, 255,
    0, 0, 0, 255,
    255, 255, 255, 255,
    0, 0, 0, 255,
    255, 255, 255, 255,
    0, 0, 0, 255,
  ]);
  assert.deepEqual(new Set(output.filter((_value, index) => index % 4 !== 3)),
    new Set([0, 255]));
  assert.deepEqual(Array.from(input.slice(0, 4)), [255, 0, 0, 255],
    "the source buffer is immutable");

  const halfContrast = applyManualBinaryPreview(
    new Uint8ClampedArray([128, 128, 128, 255]),
    createManualBinaryAdjustment(0, 50),
  );
  assert.deepEqual(Array.from(halfContrast), [64, 64, 64, 255],
    "integer half-up blend matches the processor for non-default contrast");
});


test("browser preview matches the shared Pillow processor parity fixture", () => {
  assert.equal(
    parityFixture.schema,
    "librarytool.manual-binary-preview-parity/1",
  );
  const input = new Uint8ClampedArray(parityFixture.input_rgba.flat());
  for (const expectation of parityFixture.expectations) {
    const output = applyManualBinaryPreview(
      input,
      createManualBinaryAdjustment(expectation.brightness_percent),
    );
    const luminance = Array.from(output)
      .filter((_value, index) => index % 4 === 0);
    assert.deepEqual(
      luminance,
      expectation.output_l,
      `brightness ${expectation.brightness_percent}`,
    );
    assert.ok(
      Array.from(output).every((value, index) =>
        index % 4 === 3 ? value === 255 : value === 0 || value === 255),
      "contrast 100 stays binary and opaque",
    );
  }
});


test("canvas preview uses the exact pixel kernel rather than a CSS approximation", () => {
  let written = null;
  const imageData = {
    data: new Uint8ClampedArray([
      0, 255, 0, 255,
      255, 0, 0, 255,
    ]),
  };
  const context = {
    clearRect() {},
    drawImage() {},
    getImageData() { return imageData; },
    putImageData(value) { written = Array.from(value.data); },
  };
  const canvas = {
    getContext() { return context; },
    width: 0,
    height: 0,
  };
  const result = renderBinaryCanvasPreview({
    image: { naturalWidth: 2, naturalHeight: 1 },
    canvas,
    adjustment: createManualBinaryAdjustment(0),
  });
  assert.equal(result.width, 2);
  assert.equal(result.height, 1);
  assert.deepEqual(written, [
    255, 255, 255, 255,
    0, 0, 0, 255,
  ]);
});


test("bare A precedence excludes gestures, modals, modifiers, and native controls", () => {
  const state = editorState();
  assert.equal(canEnterImageAdjust(shortcutContext(), state), true);
  assert.equal(canEnterImageAdjust(shortcutContext({
    canvasFocused: false,
    canvasTarget: false,
    imageHovered: true,
  }), state), false, "hover cannot bypass focused-canvas ownership");
  const exclusions = [
    { canvasFocused: false },
    { canvasTarget: false },
    { modalOpen: true },
    { rectangleEditing: true },
    { formControl: true },
    { repeat: true },
    { isComposing: true },
    { ctrlKey: true },
    { metaKey: true },
    { altKey: true },
    { shiftKey: true },
    { defaultPrevented: true },
    { target: { tagName: "INPUT" } },
  ];
  for (const values of exclusions) {
    assert.equal(canEnterImageAdjust(shortcutContext(values), state), false,
      JSON.stringify(values));
  }
  assert.equal(canEnterImageAdjust(
    shortcutContext(),
    { ...state, gesture: { kind: "pointer" } },
  ), false);
});


test("A is canvas-focused and cleanup removes only the editor-scoped listener", () => {
  const harness = mountedHarness();
  const {
    canvas,
    cleanup,
    controller,
    documentRef,
    inspector,
    surface,
  } = harness;
  const outside = new FakeNode("main", documentRef);
  documentRef.activeElement = outside;
  assert.equal((documentRef.listeners.get("keydown") || []).length, 0);
  assert.equal((surface.listeners.get("keydown") || []).length, 1);
  const outsideA = surface.emit("keydown", {
    key: "a",
    target: outside,
  });
  assert.equal(outsideA.defaultPrevented, false);
  assert.equal(controller.getState().tool, TOOLS.SELECT);

  documentRef.activeElement = canvas;
  const canvasA = surface.emit("keydown", {
    key: "a",
    target: canvas,
  });
  assert.equal(canvasA.defaultPrevented, true);
  assert.equal(controller.getState().tool, TOOLS.IMAGE_ADJUST);

  cleanup();
  assert.equal((surface.listeners.get("keydown") || []).length, 0);
  assert.equal(byClass(inspector, "image-adjust-panel").length, 0);
  controller.dispatch({ type: "SET_TOOL", tool: TOOLS.SELECT });
  const afterCleanupA = surface.emit("keydown", {
    key: "a",
    target: canvas,
  });
  assert.equal(afterCleanupA.defaultPrevented, false);
  assert.equal(controller.getState().tool, TOOLS.SELECT);
});


test("Image Adjust Space and wheel gates are canvas-local and source-pinned", () => {
  const state = editorState({ tool: TOOLS.IMAGE_ADJUST });
  const space = shortcutContext({ key: " ", code: "Space" });
  assert.equal(canQueueImageAdjustShortcut(space, state, pins()), true);
  assert.equal(canQueueImageAdjustShortcut(
    { ...space, target: { tagName: "INPUT" } },
    state,
    pins(),
  ), false);
  assert.equal(canQueueImageAdjustShortcut(
    space,
    state,
    { ...pins(), source_sha256: "invalid" },
  ), false);
  assert.equal(canQueueImageAdjustShortcut(
    space,
    { ...state, submission: { status: "queued" } },
    pins(),
  ), false);

  const wheel = shortcutContext({ key: undefined, deltaY: -1 });
  assert.equal(canApplyWheel(wheel, state), true);
  assert.equal(canApplyWheel({ ...wheel, canvasFocused: false }, state), false);
  assert.equal(canApplyWheel({ ...wheel, ctrlKey: true }, state), false,
    "browser zoom gestures remain native");
  assert.equal(canApplyWheel({
    ...wheel,
    target: { tagName: "INPUT" },
  }, state), false);
  assert.equal(canApplyWheel(wheel, editorState()), false);
});


test("mounted UI exposes controls, A enters mode, and wheel direction clamps", () => {
  const harness = mountedHarness({
    profile: { lastAppliedBrightness: BRIGHTNESS_MAX - 1 },
  });
  const {
    adjustButton,
    canvas,
    cleanup,
    controller,
    documentRef,
    inspector,
    previewCalls,
    queueCalls,
    surface,
    tool,
  } = harness;
  const panel = byClass(inspector, "image-adjust-panel")[0];
  const inputs = descendants(panel).filter((node) => node.tagName === "INPUT");
  const brightness = inputs.find(
    (node) => node.type === "number" && node.min === "-100");
  const contrast = inputs.find(
    (node) => node.type === "number" && node.min === "0");
  const rerun = inputs.find((node) => node.type === "checkbox");

  assert.ok(panel);
  assert.equal(adjustButton.getAttribute("aria-keyshortcuts"), "A");
  assert.equal(brightness.min, "-100");
  assert.equal(brightness.max, "100");
  assert.equal(brightness.value, "99");
  assert.equal(contrast.max, "100");
  assert.equal(contrast.value, String(DEFAULT_CONTRAST));
  assert.ok(rerun);
  assert.match(panel.textContent, /Re-run OCR/);
  assert.deepEqual(tool.serializeProfile(), { lastAppliedBrightness: 99 });

  documentRef.activeElement = canvas;
  const enter = surface.emit("keydown", {
    key: "a",
    target: canvas,
    stopPropagation() { this.propagationStopped = true; },
  });
  assert.equal(enter.defaultPrevented, true);
  assert.equal(controller.getState().tool, TOOLS.IMAGE_ADJUST);
  tool.syncEditorState(controller.getState(), controller.resource);
  assert.equal(panel.dataset.active, "true");
  assert.match(panel.textContent, /Active tool: Image Adjust/);
  assert.match(canvas.getAttribute("aria-label"), /Use the wheel/);
  assert.ok(previewCalls.length >= 1);

  const queue = surface.emit("keydown", {
    key: " ",
    code: "Space",
    target: canvas,
  });
  assert.equal(queue.defaultPrevented, true);
  assert.deepEqual(queueCalls, ["shortcut"]);

  const wheelUp = canvas.emit("wheel", { deltaY: -10, target: canvas });
  assert.equal(wheelUp.defaultPrevented, true);
  assert.equal(tool.getState().brightness, 100);
  canvas.emit("wheel", { deltaY: -10, target: canvas });
  assert.equal(tool.getState().brightness, 100, "upper bound clamps");
  canvas.emit("wheel", { deltaY: 10, target: canvas });
  assert.equal(tool.getState().brightness, 99);

  documentRef.activeElement = brightness;
  const nativeWheel = canvas.emit("wheel", { deltaY: 10, target: brightness });
  assert.equal(nativeWheel.defaultPrevented, false);
  assert.equal(tool.getState().brightness, 99);

  rerun.checked = true;
  rerun.emit("change");
  assert.equal(tool.getState().rerunOcr, true);
  assert.deepEqual(tool.serializeProfile(), { lastAppliedBrightness: 99 },
    "preview edits never persist the remembered value");
  cleanup();
  assert.equal(byClass(inspector, "image-adjust-panel").length, 0);
  assert.equal(canvas.getAttribute("aria-label"), null);
});


test("preset Apply activates Image Adjust and preserves exact recipe semantics", async () => {
  const operationsOnly = {
    preset_id: "gamma-only",
    name: "Gamma only",
    category: "cover",
    operations: [gammaOperation()],
    adjustment: null,
    revision: "preset-r1",
  };
  const adjustmentOnly = {
    preset_id: "binary-only",
    name: "Binary only",
    category: "cover",
    operations: [],
    adjustment: createManualBinaryAdjustment(35, 25),
    revision: "preset-r2",
  };
  const rows = [operationsOnly, adjustmentOnly];
  const created = [];
  const presets = {
    async list() { return { presets: rows.slice() }; },
    async create({ preset }) {
      created.push(preset);
      rows.push({ ...preset, revision: `preset-new-${created.length}` });
      return { preset: rows.at(-1) };
    },
    async remove() { return { removed: true }; },
  };
  const harness = mountedHarness({ createPresetPanel, presets });
  await new Promise((resolve) => setImmediate(resolve));
  const panel = byClass(harness.inspector, "preset-panel")[0];
  const chooser = byClass(panel, "preset-chooser")[0];
  const applyButton = byClass(panel, "preset-apply")[0];

  chooser.value = "gamma-only";
  chooser.emit("change");
  applyButton.emit("click");
  assert.equal(harness.controller.getState().tool, TOOLS.IMAGE_ADJUST);
  assert.equal(harness.tool.getState().adjustmentEnabled, false);
  assert.equal(harness.tool.getAdjustment({
    state: harness.controller.getState(),
  }), null);
  assert.deepEqual(harness.controller.getState().operations, [gammaOperation()]);

  byClass(panel, "preset-name")[0].value = "Gamma copy";
  byClass(panel, "preset-save")[0].emit("click");
  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(created[0].adjustment, null,
    "saving does not invent the manual-binary adjustment");
  assert.deepEqual(created[0].operations, [gammaOperation()]);

  chooser.value = "binary-only";
  chooser.emit("change");
  applyButton.emit("click");
  assert.equal(harness.tool.getState().adjustmentEnabled, true);
  assert.equal(harness.tool.getState().brightness, 35);
  assert.equal(harness.tool.getState().contrast, 25);
  assert.deepEqual(harness.tool.getAdjustment({
    state: harness.controller.getState(),
  }), adjustmentOnly.adjustment);
  assert.deepEqual(harness.previewCalls.at(-1), adjustmentOnly.adjustment,
    "the exact non-default contrast reaches the preview adapter");
  assert.deepEqual(harness.controller.getState().operations, [],
    "an adjustment-only preset clears operations from the prior recipe");

  const serialized = serializeCorrectionTransformCommand({
    pins: pins(),
    quad: harness.controller.getState().quad,
    adjustment: composeImageAdjustRendererOptions(harness.tool)
      .getAdjustment({ state: harness.controller.getState() }),
    rerunOcr: false,
    operationId: "preset-adjustment-25",
  });
  assert.deepEqual(serialized.adjustment, adjustmentOnly.adjustment,
    "the exact non-default contrast reaches the queue command");

  byClass(panel, "preset-name")[0].value = "Binary copy";
  byClass(panel, "preset-save")[0].emit("click");
  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(created[1].adjustment, adjustmentOnly.adjustment,
    "saving the current recipe preserves every canonical adjustment field");
  assert.deepEqual(created[1].operations, []);
  harness.cleanup();
});


test("renderer composition serializes brightness and visible OCR choice", () => {
  const harness = mountedHarness();
  const { controller, tool } = harness;
  controller.dispatch({ type: "SET_TOOL", tool: TOOLS.IMAGE_ADJUST });
  tool.syncEditorState(controller.getState(), controller.resource);
  tool.setBrightness(27);
  tool.setRerunOcr(true);
  const composed = composeImageAdjustRendererOptions(tool);
  const context = {
    state: controller.getState(),
    resource: controller.resource,
  };
  const serialized = serializeCorrectionTransformCommand({
    pins: pins(),
    quad: controller.getState().quad,
    adjustment: composed.getAdjustment(context),
    rerunOcr: composed.getRerunOcr(context),
    operationId: "adjust-op-27",
  });
  assert.equal(serialized.adjustment.brightness_percent, 27);
  assert.equal(serialized.adjustment.contrast_percent, 100);
  assert.equal(serialized.adjustment.threshold, 93);
  assert.equal(serialized.rerun_ocr, true);
  harness.cleanup();
});


test("remembered brightness changes only after a real committed image result", () => {
  const profileEvents = [];
  const ocrEvents = [];
  const tool = createImageAdjustTool({
    profile: { lastAppliedBrightness: 5 },
    onProfileChange: (profile, detail) => profileEvents.push({ profile, detail }),
    onOcrOutcome: (outcome, detail) => ocrEvents.push({ outcome, detail }),
  });
  const composed = composeImageAdjustRendererOptions(tool);

  const cancelledCommand = command("adjust-cancel", 22, true);
  composed.onQueueResult({ job_id: "job-cancel" }, cancelledCommand, { id: "a" });
  const cancelled = tool.observeTransformResult({
    job_id: "job-cancel",
    operation_id: "adjust-cancel",
    image_commit: null,
    ocr_followup: {
      state: "not_requested",
      source: null,
      proposal_ref: "",
      failure: null,
    },
    cancelled_before_commit: true,
  });
  assert.equal(cancelled.imageCommitted, false);
  assert.equal(cancelled.profileChanged, false);
  assert.deepEqual(tool.serializeProfile(), { lastAppliedBrightness: 5 });
  assert.equal(profileEvents.length, 0);

  const failedCommand = command("adjust-failed", -40);
  composed.onQueueResult({ job_id: "job-failed" }, failedCommand, { id: "a" });
  const failed = tool.observeTransformResult({
    operation_id: "adjust-failed",
    image_commit: null,
    cancelled_before_commit: false,
  });
  assert.equal(failed.profileChanged, false);
  assert.deepEqual(tool.serializeProfile(), { lastAppliedBrightness: 5 });

  const appliedCommand = command("adjust-applied", 33, true);
  composed.onQueueResult(
    { job_id: "job-adjust-applied" },
    appliedCommand,
    { id: "a" },
  );
  const applied = tool.observeTransformResult(
    committedResult("adjust-applied", "failed"),
  );
  assert.equal(applied.imageCommitted, true);
  assert.equal(applied.profileChanged, true);
  assert.deepEqual(applied.profile, { lastAppliedBrightness: 33 });
  assert.equal(profileEvents.length, 1);
  assert.equal(profileEvents[0].detail.reason, "transform-committed");
  assert.equal(ocrEvents.at(-1).outcome.state, "failed");
  assert.equal(ocrEvents.at(-1).outcome.failure.code, "ocr_followup_failed");
  assert.equal(applied.ocrOutcome.state, "failed",
    "OCR follow-up remains separately observable from image success");

  const newWindow = createImageAdjustTool({ profile: tool.serializeProfile() });
  assert.equal(newWindow.getState().brightness, 33);
  assert.equal(newWindow.getState().rememberedBrightness, 33);
});


test("a validated terminal commit completes the queued mounted editor", () => {
  const harness = mountedHarness({
    profile: { lastAppliedBrightness: 4 },
  });
  const { controller, tool } = harness;
  const queuedCommand = command("adjust-converged", 36, true);
  controller.dispatch({ type: "SET_TOOL", tool: TOOLS.IMAGE_ADJUST });
  controller.dispatch({ type: "QUEUE_STARTED", command: queuedCommand });
  controller.dispatch({
    type: "QUEUE_ACCEPTED",
    jobId: "job-adjust-converged",
  });
  tool.handleQueueAccepted(
    { job_id: "job-adjust-converged" },
    queuedCommand,
    controller.resource,
  );

  const result = tool.observeTransformResult(
    committedResult("adjust-converged", "failed"),
  );

  assert.equal(result.imageCommitted, true);
  assert.equal(result.invalidImageCommit, false);
  assert.equal(result.editorSettled, true);
  assert.equal(result.terminalState, "done");
  assert.equal(controller.getState().submission.status, "complete");
  assert.deepEqual(tool.serializeProfile(), { lastAppliedBrightness: 36 });
  assert.equal(result.ocrOutcome.state, "failed");
  assert.deepEqual(tool.getState().pendingOperationIds, []);
  harness.cleanup();
});


test("empty terminal outputs reset the editor without remembering brightness", () => {
  const harness = mountedHarness({
    profile: { lastAppliedBrightness: 8 },
  });
  const { controller, tool } = harness;
  const queuedCommand = command("adjust-invalid-output", 49, true);
  controller.dispatch({ type: "SET_TOOL", tool: TOOLS.IMAGE_ADJUST });
  controller.dispatch({ type: "QUEUE_STARTED", command: queuedCommand });
  controller.dispatch({
    type: "QUEUE_ACCEPTED",
    jobId: "job-adjust-invalid-output",
  });
  tool.handleQueueAccepted(
    { job_id: "job-adjust-invalid-output" },
    queuedCommand,
    controller.resource,
  );
  const invalid = committedResult("adjust-invalid-output", "succeeded");
  invalid.terminal_state = "done";
  invalid.image_commit.outputs = [];

  const result = tool.observeTransformResult(invalid);

  assert.equal(result.imageCommitted, false);
  assert.equal(result.invalidImageCommit, true);
  assert.equal(result.editorSettled, true);
  assert.equal(controller.getState().submission.status, "idle");
  assert.deepEqual(tool.serializeProfile(), { lastAppliedBrightness: 8 });
  assert.equal(result.ocrOutcome, null);
  assert.deepEqual(tool.getState().pendingOperationIds, []);
  harness.cleanup();
});


test("cancellation before commit resets the queued mounted editor", () => {
  const harness = mountedHarness({
    profile: { lastAppliedBrightness: -6 },
  });
  const { controller, tool } = harness;
  const queuedCommand = command("adjust-cancelled-mounted", 54, true);
  controller.dispatch({ type: "SET_TOOL", tool: TOOLS.IMAGE_ADJUST });
  controller.dispatch({ type: "QUEUE_STARTED", command: queuedCommand });
  controller.dispatch({
    type: "QUEUE_ACCEPTED",
    jobId: "job-adjust-cancelled-mounted",
  });
  tool.handleQueueAccepted(
    { job_id: "job-adjust-cancelled-mounted" },
    queuedCommand,
    controller.resource,
  );

  const result = tool.observeTransformResult({
    job_id: "job-adjust-cancelled-mounted",
    operation_id: "adjust-cancelled-mounted",
    terminal_state: "cancelled",
    image_commit: null,
    ocr_followup: {
      state: "not_requested",
      source: null,
      proposal_ref: "",
      failure: null,
    },
    cancelled_before_commit: true,
    failure: null,
  });

  assert.equal(result.imageCommitted, false);
  assert.equal(result.editorSettled, true);
  assert.equal(controller.getState().submission.status, "idle");
  assert.deepEqual(tool.serializeProfile(), { lastAppliedBrightness: -6 });
  assert.equal(result.ocrOutcome, null);
  harness.cleanup();
});


test("a reopened window may commit from a persisted job command without pending UI state", () => {
  const tool = createImageAdjustTool({
    profile: { lastAppliedBrightness: -3 },
  });
  const result = tool.observeTransformResult(
    committedResult("adjust-reopened", "succeeded"),
    command("adjust-reopened", 41, true),
  );
  assert.equal(result.imageCommitted, true);
  assert.equal(result.profileChanged, true);
  assert.deepEqual(tool.serializeProfile(), { lastAppliedBrightness: 41 });
  assert.equal(result.ocrOutcome.state, "succeeded");
  assert.equal(result.ocrOutcome.proposal_ref, "ocr-proposal-1");
});


test("a command adapter observes an immediately returned terminal result", () => {
  const profileEvents = [];
  const tool = createImageAdjustTool({
    onProfileChange: (profile) => profileEvents.push(profile),
  });
  const composed = composeImageAdjustRendererOptions(tool);
  composed.onQueueResult(
    committedResult("adjust-immediate", "succeeded"),
    command("adjust-immediate", -24, true),
    { id: "capture-7" },
  );
  assert.deepEqual(tool.serializeProfile(), {
    lastAppliedBrightness: -24,
  });
  assert.equal(profileEvents.length, 1);
  assert.equal(tool.getState().pendingOperationIds.length, 0);
  assert.equal(tool.getState().lastOcrOutcome.state, "succeeded");
});


test("authoring builds the engine's canonical operation documents byte-for-byte", () => {
  assert.equal(
    canonicalOperations.schema,
    "librarytool.processing-operations-canonical/1",
  );
  const algorithms = new Set();
  for (const entry of canonicalOperations.operations) {
    const built = createProcessingOperation(
      entry.authoring.algorithm,
      entry.authoring.parameters,
    );
    assert.deepEqual(built, entry.canonical, entry.authoring.algorithm);
    assert.deepEqual(
      normalizeProcessingOperations([built]),
      [entry.canonical],
      `${entry.authoring.algorithm} passes the shared client validator`,
    );
    algorithms.add(entry.authoring.algorithm);
  }
  assert.deepEqual(
    [...algorithms].sort(),
    Object.keys(PROCESSING_OPERATION_DEFAULTS).sort(),
    "every published algorithm is pinned by the Python-generated fixture",
  );
  for (const algorithm of Object.keys(PROCESSING_OPERATION_DEFAULTS)) {
    assert.ok(
      canonicalOperations.operations.some((entry) =>
        entry.authoring.algorithm === algorithm &&
        Object.keys(entry.authoring.parameters).length === 0),
      `${algorithm} Add-button defaults are pinned by the fixture`,
    );
  }
  assert.throws(
    () => createProcessingOperation("median-filter-v1"),
    /unsupported processing algorithm/,
  );
});


test("operation parameter authoring clamps to the engine's validated ranges", () => {
  assert.equal(clampOperationParameter("gamma-v1", "gamma_hundredths", 5000), 1000);
  assert.equal(clampOperationParameter("gamma-v1", "gamma_hundredths", 0), 10);
  assert.equal(clampOperationParameter("contrast-v1", "contrast_percent", -250), -100);
  assert.equal(clampOperationParameter("unsharp-mask-v1", "radius_tenths", 2.6), 3);
  const clamped = createProcessingOperation("channel-gain-v1", {
    red_percent: 999,
    green_percent: -3,
    blue_percent: 200.4,
  });
  assert.equal(clamped.red_percent, 400);
  assert.equal(clamped.green_percent, 0);
  assert.equal(clamped.blue_percent, 200);
  assert.deepEqual(normalizeProcessingOperations([clamped]), [clamped]);
  const grayWorld = createProcessingOperation("white-balance-v1", {
    mode: "gray_world",
    temperature: 40,
    tint: -20,
  });
  assert.equal(grayWorld.temperature, 0, "gray_world cannot carry temperature");
  assert.equal(grayWorld.tint, 0, "gray_world cannot carry tint");
  assert.deepEqual(normalizeProcessingOperations([grayWorld]), [grayWorld]);
});


test("the operations editor authors, edits, reorders, and removes the pipeline", () => {
  const harness = mountedHarness();
  const editor = operationsEditor(harness);
  assert.equal(editor.add.disabled, true, "authoring requires the active tool");
  assert.equal(editor.hint.hidden, true, "no queue-time note before any op exists");
  activateImageAdjust(harness);
  assert.equal(editor.add.disabled, false);

  editor.select.value = "gamma-v1";
  editor.add.emit("click");
  assert.deepEqual(
    harness.controller.getState().operations,
    normalizeProcessingOperations([createProcessingOperation("gamma-v1")]),
    "Add dispatches the exact normalized default document",
  );
  assert.equal(editor.hint.hidden, false);
  assert.match(editor.hint.textContent, /Applied on queue/);

  editor.select.value = "contrast-v1";
  editor.add.emit("click");
  let rows = editor.rows();
  assert.equal(rows.length, 2);
  assert.equal(rows[0].dataset.operationAlgorithm, "gamma-v1");
  assert.equal(rows[1].dataset.operationAlgorithm, "contrast-v1");

  const gammaInput = descendants(rows[0]).find((node) => node.tagName === "INPUT");
  gammaInput.value = "120";
  gammaInput.emit("change");
  assert.equal(
    harness.controller.getState().operations[0].gamma_hundredths, 120);
  assert.equal(
    harness.controller.getState().operations[0].rule,
    "round_half_up(255 * (value/255) ** (100/gamma_hundredths)), clamped_0_255",
    "an edited parameter keeps the canonical rule string",
  );

  rows = editor.rows();
  const overflow = descendants(rows[0]).find((node) => node.tagName === "INPUT");
  overflow.value = "99999";
  overflow.emit("change");
  assert.equal(
    harness.controller.getState().operations[0].gamma_hundredths, 1000,
    "out-of-range edits clamp to the engine bound");
  assert.equal(descendants(editor.rows()[0])
    .find((node) => node.tagName === "INPUT").value, "1000");

  rows = editor.rows();
  byClass(rows[0], "image-adjust-operation-down")[0].emit("click");
  assert.deepEqual(
    harness.controller.getState().operations.map((value) => value.algorithm),
    ["contrast-v1", "gamma-v1"],
    "order is authored, never sorted",
  );
  byClass(editor.rows()[0], "image-adjust-operation-up")[0].emit("click");
  assert.deepEqual(
    harness.controller.getState().operations.map((value) => value.algorithm),
    ["contrast-v1", "gamma-v1"],
    "moving the first row up is a guarded no-op",
  );

  byClass(editor.rows()[1], "image-adjust-operation-remove")[0].emit("click");
  assert.deepEqual(
    harness.controller.getState().operations.map((value) => value.algorithm),
    ["contrast-v1"],
  );

  const serialized = serializeCorrectionTransformCommand({
    pins: pins(),
    quad: harness.controller.getState().quad,
    adjustment: createManualBinaryAdjustment(0),
    operations: harness.controller.getState().operations,
    rerunOcr: false,
    operationId: "authored-ops-1",
  });
  assert.deepEqual(
    serialized.operations,
    harness.controller.getState().operations,
    "the authored pipeline reaches the queue command untouched",
  );

  harness.controller.dispatch({
    type: "SET_PROCESSING_OPERATIONS",
    operations: Array.from(
      { length: MAX_OPERATIONS_PER_RECIPE },
      () => createProcessingOperation("gamma-v1"),
    ),
  });
  harness.tool.syncEditorState(
    harness.controller.getState(), harness.controller.resource);
  assert.equal(editor.add.disabled, true, "the 16-operation cap disables Add");
  editor.add.emit("click");
  assert.equal(
    harness.controller.getState().operations.length,
    MAX_OPERATIONS_PER_RECIPE,
    "a full recipe cannot grow past the engine cap",
  );
  harness.cleanup();
});


test("white balance authoring keeps gray_world canonical and manual editable", () => {
  const harness = mountedHarness();
  activateImageAdjust(harness);
  const editor = operationsEditor(harness);
  const byLabel = (nodes, pattern) => nodes.find((node) =>
    pattern.test(String(node.getAttribute("aria-label"))));

  editor.select.value = "white-balance-v1";
  editor.add.emit("click");
  let inputs = descendants(editor.rows()[0])
    .filter((node) => node.tagName === "INPUT");
  assert.equal(byLabel(inputs, /Temp/).disabled, true);
  assert.equal(byLabel(inputs, /Tint/).disabled, true);
  assert.match(
    harness.controller.getState().operations[0].rule,
    /mean_of_all_channels/,
  );

  const mode = descendants(editor.rows()[0])
    .find((node) => node.tagName === "SELECT");
  mode.value = "manual";
  mode.emit("change");
  inputs = descendants(editor.rows()[0])
    .filter((node) => node.tagName === "INPUT");
  const temperature = byLabel(inputs, /Temp/);
  assert.equal(temperature.disabled, false);
  temperature.value = "-300";
  temperature.emit("change");
  const manual = harness.controller.getState().operations[0];
  assert.equal(manual.mode, "manual");
  assert.equal(manual.temperature, -100, "clamps to the engine range");
  assert.match(manual.rule, /red gain = 1 \+ strength_percent/);
  assert.deepEqual(normalizeProcessingOperations([manual]), [manual]);

  const back = descendants(editor.rows()[0])
    .find((node) => node.tagName === "SELECT");
  back.value = "gray_world";
  back.emit("change");
  const grayWorld = harness.controller.getState().operations[0];
  assert.equal(grayWorld.temperature, 0, "switching modes re-canonicalizes");
  assert.equal(grayWorld.tint, 0);
  assert.match(grayWorld.rule, /mean_of_all_channels/);
  assert.deepEqual(normalizeProcessingOperations([grayWorld]), [grayWorld]);
  harness.cleanup();
});


test("the Binarize input authors the real 0..100 blend weight", () => {
  assert.equal(CONTRAST_MIN, 0);
  assert.equal(CONTRAST_MAX, 100);
  const harness = mountedHarness();
  activateImageAdjust(harness);
  const panel = byClass(harness.inspector, "image-adjust-panel")[0];
  const contrast = descendants(panel).find((node) =>
    node.tagName === "INPUT" && node.type === "number" && node.min === "0");
  assert.equal(contrast.disabled, false);
  assert.equal(contrast.value, String(DEFAULT_CONTRAST));
  assert.match(
    String(contrast.getAttribute("aria-label")),
    /blend weight/,
    "the control is labeled as the blend weight it is, not a contrast dial",
  );

  contrast.value = "25";
  contrast.emit("input");
  assert.equal(harness.tool.getState().contrast, 25);
  const adjustment = harness.tool.getAdjustment({
    state: harness.controller.getState(),
  });
  assert.equal(adjustment.contrast_percent, 25);
  assert.deepEqual(harness.previewCalls.at(-1), adjustment,
    "the exact blend weight reaches the preview kernel");

  contrast.value = "999";
  contrast.emit("change");
  assert.equal(harness.tool.getState().contrast, 100, "clamps high");
  assert.equal(contrast.value, "100");

  contrast.value = "-3";
  contrast.emit("change");
  assert.equal(harness.tool.getState().contrast, 0, "clamps low");

  contrast.value = "abc";
  contrast.emit("input");
  assert.equal(contrast.getAttribute("aria-invalid"), "true");
  assert.equal(harness.tool.getState().contrast, 0,
    "invalid input never dispatches");

  assert.match(
    byClass(panel, "image-adjust-threshold")[0].textContent,
    /Binarize 0%/,
  );
  harness.cleanup();
});


test("a preset saved after authoring captures the exact operations pipeline", async () => {
  const created = [];
  const rows = [];
  const presets = {
    async list() { return { presets: rows.slice() }; },
    async create({ preset }) {
      created.push(preset);
      rows.push({ ...preset, revision: `preset-r${created.length}` });
      return { preset: rows.at(-1) };
    },
    async remove() { return { removed: true }; },
  };
  const harness = mountedHarness({ createPresetPanel, presets });
  await new Promise((resolve) => setImmediate(resolve));
  activateImageAdjust(harness);
  const editor = operationsEditor(harness);
  editor.select.value = "unsharp-mask-v1";
  editor.add.emit("click");
  editor.select.value = "contrast-v1";
  editor.add.emit("click");
  const contrastInput = descendants(editor.rows()[1])
    .find((node) => node.tagName === "INPUT");
  contrastInput.value = "25";
  contrastInput.emit("change");

  const panel = byClass(harness.inspector, "preset-panel")[0];
  byClass(panel, "preset-name")[0].value = "Authored pipeline";
  byClass(panel, "preset-save")[0].emit("click");
  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));

  assert.equal(created.length, 1);
  assert.deepEqual(created[0].operations, [
    createProcessingOperation("unsharp-mask-v1"),
    createProcessingOperation("contrast-v1", { contrast_percent: 25 }),
  ]);
  assert.deepEqual(
    created[0].operations,
    harness.controller.getState().operations,
    "the preset captures the authored state, not a rebuilt approximation",
  );
  harness.cleanup();
});


function reocrResource(outputKind = "corrected-display") {
  return {
    id: "corrected-display-1",
    summary: {
      itemId: "book-1",
      id: "corrected-display-1",
      revision: "corrected-display-r2",
      extensions: {
        correction_transform: {
          operation_id: "transform-op-1",
          output_kind: outputKind,
          source_revision: "source-r17",
        },
      },
    },
  };
}


function reocrControls(harness) {
  const row = byClass(harness.inspector, "image-adjust-reocr-control")[0] || null;
  const button = row && descendants(row).find((node) =>
    node.dataset && node.dataset.imageReocr) || null;
  return { button, row };
}


test("the Re-OCR action gates on capability and committed transform lineage", async () => {
  const requests = [];
  const harness = mountedHarness({
    requestReocr(request) {
      requests.push(request);
      return Promise.resolve({ replayed: false });
    },
  });

  // The plain capture resource has no correction_transform pin.
  let controls = reocrControls(harness);
  assert.ok(controls.row, "wired tools render the Re-OCR row");
  assert.equal(controls.row.hidden, true);

  harness.tool.mount(harness.controller, reocrResource());
  controls = reocrControls(harness);
  assert.equal(controls.row.hidden, true,
    "the pin alone is not enough without the queue capability");

  harness.tool.setReocrCapability(true);
  assert.equal(controls.row.hidden, false);
  assert.equal(controls.button.disabled, false);
  assert.equal(harness.tool.getState().reocrCapability, true);

  controls.button.emit("click");
  await new Promise((resolve) => setImmediate(resolve));
  controls.button.emit("click");
  await new Promise((resolve) => setImmediate(resolve));

  assert.equal(requests.length, 2);
  for (const request of requests) {
    assert.match(request.operationId, /^reocr-/);
    assert.equal(request.itemId, "book-1");
    assert.equal(request.artifactId, "corrected-display-1");
    assert.equal(request.expectedArtifactRevision, "corrected-display-r2");
  }
  assert.notEqual(requests[0].operationId, requests[1].operationId,
    "every click mints a fresh operation id");

  harness.tool.setReocrCapability(false);
  assert.equal(controls.row.hidden, true);
  controls.button.emit("click");
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(requests.length, 2, "a hidden action cannot queue");
  harness.tool.destroy();
});


test("a queued Re-OCR reports its receipt and stays single-flight", async () => {
  let release;
  const requests = [];
  const harness = mountedHarness({
    requestReocr(request) {
      requests.push(request);
      return new Promise((resolve) => { release = resolve; });
    },
  });
  harness.tool.mount(harness.controller, reocrResource("ocr-ready"));
  harness.tool.setReocrCapability(true);
  const controls = reocrControls(harness);

  controls.button.emit("click");
  assert.equal(controls.button.disabled, true, "in-flight queueing disables");
  assert.equal(harness.tool.getState().reocrBusy, true);
  controls.button.emit("click");
  assert.equal(requests.length, 1);

  release({ replayed: true });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(controls.button.disabled, false);
  const jobStatus = byClass(
    harness.inspector, "image-adjust-job-status")[0];
  assert.equal(
    jobStatus.textContent,
    "Re-OCR already queued for this rendition.",
  );
  harness.tool.destroy();
});


test("a failed Re-OCR queue keeps the panel usable and reports the error", async () => {
  const harness = mountedHarness({
    requestReocr() {
      return Promise.reject(new Error("the raster artifact changed elsewhere"));
    },
  });
  harness.tool.mount(harness.controller, reocrResource());
  harness.tool.setReocrCapability(true);
  const controls = reocrControls(harness);

  controls.button.emit("click");
  await new Promise((resolve) => setImmediate(resolve));

  assert.equal(controls.button.disabled, false);
  const jobStatus = byClass(
    harness.inspector, "image-adjust-job-status")[0];
  assert.equal(jobStatus.textContent, "the raster artifact changed elsewhere");
  harness.tool.destroy();
});


test("unwired tools render no Re-OCR affordance at all", () => {
  const harness = mountedHarness();
  harness.tool.mount(harness.controller, reocrResource());
  harness.tool.setReocrCapability(true);
  assert.equal(reocrControls(harness).row, null);
  harness.tool.destroy();
});


test("module installs through browser globals as well as CommonJS", () => {
  const context = vm.createContext({});
  const root = path.join(
    __dirname,
    "..",
    "tools",
    "whl_explorer",
    "static",
    "corrections",
  );
  vm.runInContext(
    fs.readFileSync(path.join(root, "image-editor-state.js"), "utf8"),
    context,
  );
  vm.runInContext(
    fs.readFileSync(path.join(root, "image-adjust-tool.js"), "utf8"),
    context,
  );
  assert.equal(
    typeof context.LibraryToolCorrections.createImageAdjustTool,
    "function",
  );
  assert.equal(
    context.LibraryToolCorrections.IMAGE_ADJUST_PROFILE_KEY,
    "imageAdjust",
  );
});

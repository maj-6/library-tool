const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const {
  POINT_ORDER,
  PROPOSAL_SCHEMA,
  TOOLS,
  createImageEditorState,
  serializeCorrectionTransformCommand,
} = require("../tools/whl_explorer/static/corrections/image-editor-state");
const {
  BINARY_ALGORITHM,
  BRIGHTNESS_MAX,
  BRIGHTNESS_MIN,
  DEFAULT_CONTRAST,
  IMAGE_ADJUST_PROFILE_KEY,
  THRESHOLD_RULE,
  applyManualBinaryPreview,
  canApplyWheel,
  canEnterImageAdjust,
  canQueueImageAdjustShortcut,
  composeImageAdjustRendererOptions,
  createImageAdjustTool,
  createManualBinaryAdjustment,
  normalizeImageAdjustProfile,
  renderBinaryCanvasPreview,
  serializeImageAdjustProfile,
  thresholdForBrightness,
} = require("../tools/whl_explorer/static/corrections/image-adjust-tool");
const {
  FakeNode,
  fakeDocument,
} = require("./fixtures/corrections_fake_dom");


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


function committedResult(operationId, ocrState = "not_requested") {
  return {
    job_id: `job-${operationId}`,
    operation_id: operationId,
    image_commit: {
      operation_id: operationId,
      outputs: [
        { kind: "display", artifact_id: "display-1" },
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
      if (action.type === "SET_TOOL") state = { ...state, tool: action.tool };
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
  const brightness = inputs.find((node) => node.type === "number");
  const rerun = inputs.find((node) => node.type === "checkbox");

  assert.ok(panel);
  assert.equal(adjustButton.getAttribute("aria-keyshortcuts"), "A");
  assert.equal(brightness.min, "-100");
  assert.equal(brightness.max, "100");
  assert.equal(brightness.value, "99");
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
  composed.onQueueResult({ job_id: "job-applied" }, appliedCommand, { id: "a" });
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

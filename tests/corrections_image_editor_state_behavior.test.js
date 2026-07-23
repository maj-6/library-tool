const assert = require("node:assert/strict");
const test = require("node:test");

const {
  COORDINATE_SPACE,
  FULL_FRAME_QUAD,
  POINT_ORDER,
  PROPOSAL_SCHEMA,
  TOOLS,
  TRANSFORM_COMMAND_ID,
  canQueuePerspectiveShortcut,
  canQueueTransform,
  clientToNormalized,
  containedImageRect,
  createImageEditorState,
  nearestCornerIndex,
  normalizedToClient,
  reduceImageEditorState,
  resolveEscape,
  resolveInitialQuad,
  serializeCorrectionTransformCommand,
  validatePerspectiveQuad,
} = require("../tools/whl_explorer/static/corrections/image-editor-state");


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
    source_sha256: "A".repeat(64),
    ...overrides,
  };
}


function perspectiveState(overrides = {}) {
  return createImageEditorState({
    proposal: proposal(),
    sourceRevision: "source-r17",
    tool: TOOLS.PERSPECTIVE,
    hasSelection: true,
    ...overrides,
  });
}


function spaceEvent(overrides = {}) {
  return {
    key: " ",
    code: "Space",
    repeat: false,
    editorFocused: true,
    modalOpen: false,
    target: { tagName: "CANVAS" },
    ...overrides,
  };
}


test("strictly pinned proposals initialize immediately and missing or unsafe proposals fall back", () => {
  const accepted = resolveInitialQuad({
    proposal: proposal(),
    sourceRevision: "source-r17",
  });
  assert.deepEqual(accepted.quad, proposal().quad);
  assert.deepEqual(accepted.quadSource, {
    kind: "proposal",
    basedOn: "proposal",
    reason: null,
    message: "Auto-detected page boundary.",
    detector: "contour",
    detectorVersion: "2.1.0",
    confidence: 0.875,
  });

  const cases = [
    [null, "missing-proposal"],
    [proposal({ schema: "org.whl.page-boundary-proposal-v2" }),
      "unsupported-proposal-schema"],
    [proposal({ version: true }), "unsupported-proposal-schema"],
    [proposal({ coordinate_space: "raw_pixels" }), "unsupported-coordinate-space"],
    [proposal({ point_order: [...POINT_ORDER].reverse() }), "unsupported-point-order"],
    [proposal({ source_revision: "source-r16" }), "stale-proposal"],
    [proposal({ confidence: Number.NaN }), "invalid-proposal-metadata"],
    [proposal({ detector: "" }), "invalid-proposal-metadata"],
    [proposal({ quad: [[0, 0], [1, 1], [1, 0], [0, 1]] }),
      "invalid-proposal-self-intersection"],
  ];
  for (const [candidate, reason] of cases) {
    const result = resolveInitialQuad({
      proposal: candidate,
      sourceRevision: "source-r17",
    });
    assert.deepEqual(result.quad, FULL_FRAME_QUAD, reason);
    assert.equal(result.quadSource.kind, "fallback");
    assert.equal(result.quadSource.basedOn, "fallback");
    assert.equal(result.quadSource.reason, reason);
  }

  assert.equal(resolveInitialQuad({
    proposal: proposal(), sourceRevision: null,
  }).quadSource.reason, "source-revision-unavailable");
});


test("quad validation mirrors engine thresholds and error precedence", () => {
  const valid = [[0.08, 0.12], [0.91, 0.08], [0.86, 0.94], [0.12, 0.89]];
  assert.equal(validatePerspectiveQuad(valid).valid, true);

  const cases = [
    [[[0, 0], [1, 0], [1, 1]], "point-count"],
    [[[0], [1, 0], [1, 1], [0, 1]], "point-arity"],
    [[[0, 0], ["1", 0], [1, 1], [0, 1]], "coordinate-type"],
    [[[0, 0], [1, 0], [1, 1], [Number.NaN, 1]], "coordinate-finite"],
    [[[0, 0], [1.01, 0], [1, 1], [0, 1]], "coordinate-bounds"],
    [[[0, 0], [1, 0], [1, 1], [0, 0]], "duplicate-vertices"],
    [[[0, 0], [1, 1], [1, 0], [0, 1]], "self-intersection"],
    [[[0, 0], [1, 0], [0.4, 0.2], [0, 1]], "non-convex"],
    [[[0, 1], [1, 1], [1, 0], [0, 0]], "point-order"],
    [[[1, 0], [1, 1], [0, 1], [0, 0]], "point-labels"],
    [[[0.1, 0.1], [0.1004, 0.1], [0.1004, 0.1004], [0.1, 0.1004]],
      "area-too-small"],
    [[[0, 0], [0.0005, 0], [1, 1], [0, 1]], "edge-too-short"],
  ];
  for (const [quad, code] of cases) {
    const validation = validatePerspectiveQuad(quad);
    assert.equal(validation.valid, false, code);
    assert.equal(validation.code, code);
    assert.ok(validation.message, code);
    assert.equal(validation.errors.length, 1, code);
  }
});


test("nearest corner is measured in rendered screen space with deterministic ties", () => {
  const imageRect = { left: 80, top: 40, width: 800, height: 120 };
  assert.deepEqual(normalizedToClient([0.25, 0.75], imageRect), [280, 130]);
  assert.deepEqual(clientToNormalized([280, 130], imageRect), [0.25, 0.75]);
  assert.deepEqual(
    clientToNormalized({ clientX: 0, clientY: 500 }, imageRect, { clamp: true }),
    [0, 1],
  );

  assert.equal(nearestCornerIndex(FULL_FRAME_QUAD, imageRect, [870, 150]), 2);
  assert.equal(
    nearestCornerIndex(FULL_FRAME_QUAD, imageRect, [480, 100]),
    0,
    "an exact four-way tie keeps the first TL/TR/BR/BL vertex",
  );

  const letterboxed = containedImageRect(
    { left: 100, top: 50, width: 1000, height: 600 },
    100,
    200,
  );
  assert.deepEqual(letterboxed, {
    left: 450, top: 50, width: 300, height: 600,
  });
  assert.equal(nearestCornerIndex(FULL_FRAME_QUAD, letterboxed, [740, 640]), 2);

  const zoomedAndPanned = containedImageRect(
    { left: 100, top: 50, width: 1000, height: 600 },
    100,
    200,
    { zoom: 2, panX: 35, panY: -20 },
  );
  assert.deepEqual(zoomedAndPanned, {
    left: 335, top: -270, width: 600, height: 1200,
  });
  assert.equal(
    nearestCornerIndex(FULL_FRAME_QUAD, zoomedAndPanned, [340, 920]),
    3,
  );

  const orientedPortrait = containedImageRect(
    { left: 0, top: 0, width: 600, height: 600 },
    40,
    80,
  );
  assert.deepEqual(orientedPortrait, {
    left: 150, top: 0, width: 300, height: 600,
  }, "the oriented dimensions are used directly without reapplying EXIF");
});


test("pointer and numeric edits each commit as one undoable gesture", () => {
  let state = perspectiveState();
  const original = state.quad.map((point) => [...point]);

  state = reduceImageEditorState(state, {
    type: "BEGIN_GESTURE",
    kind: "pointer",
    pointerId: 9,
    cornerIndex: 0,
    point: [0.10, 0.15],
  });
  state = reduceImageEditorState(state, {
    type: "MOVE_CORNER", cornerIndex: 0, point: [0.12, 0.17],
  });
  state = reduceImageEditorState(state, {
    type: "MOVE_CORNER", cornerIndex: 0, point: [0.14, 0.19],
  });
  assert.equal(state.undoStack.length, 0);
  state = reduceImageEditorState(state, { type: "COMMIT_GESTURE" });
  assert.equal(state.undoStack.length, 1);
  assert.deepEqual(state.quad[0], [0.14, 0.19]);
  assert.equal(state.quadSource.kind, "user-edited");
  assert.equal(state.quadSource.basedOn, "proposal");

  state = reduceImageEditorState(state, { type: "UNDO" });
  assert.deepEqual(state.quad, original);
  assert.equal(state.quadSource.kind, "proposal");
  state = reduceImageEditorState(state, { type: "REDO" });
  assert.deepEqual(state.quad[0], [0.14, 0.19]);

  state = reduceImageEditorState(state, {
    type: "BEGIN_GESTURE", kind: "numeric", cornerIndex: 1,
  });
  state = reduceImageEditorState(state, {
    type: "MOVE_CORNER", point: [0.88, 0.08],
  });
  state = reduceImageEditorState(state, { type: "COMMIT_GESTURE" });
  assert.equal(state.undoStack.length, 2);

  const beforeCancel = state.quad.map((point) => [...point]);
  state = reduceImageEditorState(state, {
    type: "BEGIN_GESTURE", kind: "pointer", pointerId: 10,
    cornerIndex: 2, point: [0.25, 0.25],
  });
  assert.equal(state.validation.valid, false);
  state = reduceImageEditorState(state, { type: "CANCEL_GESTURE" });
  assert.deepEqual(state.quad, beforeCancel);
  assert.equal(state.undoStack.length, 2);
});


test("invalid edits remain visible but cannot queue", () => {
  let state = perspectiveState();
  state = reduceImageEditorState(state, {
    type: "BEGIN_GESTURE", cornerIndex: 0, point: [...state.quad[2]],
  });
  state = reduceImageEditorState(state, { type: "COMMIT_GESTURE" });
  assert.equal(state.validation.valid, false);
  assert.equal(state.validation.code, "duplicate-vertices");
  assert.deepEqual(state.quad[0], state.quad[2]);
  assert.equal(canQueueTransform(state, pins()), false);
  assert.throws(
    () => serializeCorrectionTransformCommand({
      pins: pins(), quad: state.quad, adjustment: null,
      rerunOcr: false, operationId: "correction-op-1",
    }),
    (error) => error.code === "duplicate-vertices",
  );
});


test("escape resolves one common ladder rung per invocation", () => {
  let state = perspectiveState();
  state = reduceImageEditorState(state, {
    type: "BEGIN_GESTURE", cornerIndex: 0, point: [0.2, 0.2],
  });
  let resolution = resolveEscape(state, true);
  assert.equal(resolution.action.type, "CANCEL_GESTURE");
  state = reduceImageEditorState(state, resolution.action);

  resolution = resolveEscape(state, true);
  assert.deepEqual(resolution.action, { type: "SET_TOOL", tool: TOOLS.SELECT });
  state = reduceImageEditorState(state, resolution.action);

  resolution = resolveEscape(state, true);
  assert.equal(resolution.action.type, "CLEAR_SELECTION");
  assert.equal(resolution.clearHostSelection, true);
  state = reduceImageEditorState(state, resolution.action);
  assert.equal(resolveEscape(state, false), null);
});


test("Space gating fails closed for focus, modal, forms, repeat, modifiers, geometry, and duplicates", () => {
  const goodState = perspectiveState();
  assert.equal(canQueuePerspectiveShortcut(spaceEvent(), goodState, pins()), true);

  for (const event of [
    spaceEvent({ repeat: true }),
    spaceEvent({ editorFocused: false }),
    spaceEvent({ modalOpen: true }),
    spaceEvent({ ctrlKey: true }),
    spaceEvent({ shiftKey: true }),
    spaceEvent({ isComposing: true }),
    spaceEvent({ defaultPrevented: true }),
    spaceEvent({ target: { tagName: "INPUT" } }),
    spaceEvent({ target: { tagName: "BUTTON" } }),
    spaceEvent({ key: "Enter", code: "Enter" }),
  ]) {
    assert.equal(canQueuePerspectiveShortcut(event, goodState, pins()), false);
  }

  const selectState = { ...goodState, tool: TOOLS.SELECT };
  assert.equal(canQueuePerspectiveShortcut(spaceEvent(), selectState, pins()), false);
  assert.equal(canQueuePerspectiveShortcut(
    spaceEvent(), goodState, pins({ source_sha256: "not-a-digest" }),
  ), false);

  let gesture = reduceImageEditorState(goodState, {
    type: "BEGIN_GESTURE", cornerIndex: 0,
  });
  assert.equal(canQueuePerspectiveShortcut(spaceEvent(), gesture, pins()), false);
  gesture = reduceImageEditorState(gesture, { type: "CANCEL_GESTURE" });
  const command = serializeCorrectionTransformCommand({
    pins: pins(),
    quad: gesture.quad,
    adjustment: null,
    rerunOcr: false,
    operationId: "correction-op-1",
  });
  const submitting = reduceImageEditorState(gesture, {
    type: "QUEUE_STARTED", command,
  });
  assert.equal(canQueuePerspectiveShortcut(spaceEvent(), submitting, pins()), false);
  const queued = reduceImageEditorState(submitting, {
    type: "QUEUE_ACCEPTED", jobId: "job-1",
  });
  assert.equal(canQueuePerspectiveShortcut(spaceEvent(), queued, pins()), false);
});


test("transform serialization exactly follows the engine command contract", () => {
  const command = serializeCorrectionTransformCommand({
    pins: pins(),
    quad: proposal().quad,
    adjustment: null,
    rerunOcr: false,
    operationId: "correction-op-1",
  });
  assert.deepEqual(command, {
    schema: "org.whl.correction-transform-command",
    version: 1,
    item_id: "book-1",
    artifact_id: "capture-7",
    artifact_revision: "artifact-r3",
    source_revision: "source-r17",
    source_sha256: "a".repeat(64),
    quad: proposal().quad,
    adjustment: null,
    rerun_ocr: false,
    operation_id: "correction-op-1",
  });
  assert.equal(TRANSFORM_COMMAND_ID, "corrections.transform.queue");

  const adjustment = {
    schema: "org.whl.raster.manual-binary-adjust",
    version: 1,
    algorithm: "grayscale-threshold-blend-v1",
    contrast_percent: 100,
    brightness_percent: 10,
    threshold: 115,
    threshold_rule:
      "round_half_up(127.5 - brightness_percent * 1.275), clamped_0_255",
    comparison: "grayscale_value > threshold",
  };
  assert.deepEqual(serializeCorrectionTransformCommand({
    pins: pins(),
    quad: proposal().quad,
    adjustment,
    rerunOcr: true,
    operationId: "correction-op-2",
  }).adjustment, adjustment);
  assert.throws(() => serializeCorrectionTransformCommand({
    pins: pins(),
    quad: proposal().quad,
    adjustment: { ...adjustment, threshold: 114 },
    rerunOcr: true,
    operationId: "correction-op-2",
  }), /canonical raster recipe/);
});


test("retryable queue state retains the exact command while definitive failure releases it", () => {
  let state = perspectiveState();
  const command = serializeCorrectionTransformCommand({
    pins: pins(), quad: state.quad, adjustment: null,
    rerunOcr: false, operationId: "correction-op-stable",
  });
  state = reduceImageEditorState(state, { type: "QUEUE_STARTED", command });
  state = reduceImageEditorState(state, {
    type: "QUEUE_RETRYABLE", error: "response lost",
  });
  assert.equal(state.submission.status, "retryable");
  assert.equal(state.submission.command, command);
  assert.equal(canQueueTransform(state, pins()), true);

  state = reduceImageEditorState(state, {
    type: "QUEUE_STARTED", command: state.submission.command,
  });
  assert.equal(state.submission.command, command);
  state = reduceImageEditorState(state, {
    type: "QUEUE_FAILED", error: "rejected",
  });
  assert.equal(state.submission.status, "failed");
  assert.equal(state.submission.command, null);
  assert.equal(canQueueTransform(state, pins()), true);
});

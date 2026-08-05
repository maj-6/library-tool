const assert = require("node:assert/strict");
const { performance } = require("node:perf_hooks");
const test = require("node:test");

const {
  buildArtifactTreeRows,
  virtualArtifactWindow,
} = require(
  "../tools/whl_explorer/static/corrections/artifact-model");
const {
  BooksPanelController,
  CorrectionsIndexStore,
  normalizeCorrectionsIndex,
  sortedBooks,
} = require("../tools/whl_explorer/static/corrections/books");
const {
  canQueueImageAdjustShortcut,
  createImageAdjustTool,
} = require(
  "../tools/whl_explorer/static/corrections/image-adjust-tool");
const {
  COORDINATE_SPACE,
  POINT_ORDER,
  PROPOSAL_SCHEMA,
  TOOLS,
  createImageEditorState,
  nearestCornerIndex,
  reduceImageEditorState,
  serializeCorrectionTransformCommand,
} = require(
  "../tools/whl_explorer/static/corrections/image-editor-state");
const {
  FakeNode,
  fakeDocument,
} = require("./fixtures/corrections_fake_dom");


// These fixture sizes make the UI specification's 100 ms direct-manipulation
// and 200 ms cached-view budgets executable without timing network/provider I/O.
const POINTER_FEEDBACK_BUDGET_MS = 100;
const CACHED_BROWSE_BUDGET_MS = 200;
const LARGE_BOOK_CAPTURE_COUNT = 5_000;
const LARGE_ARTIFACT_COUNT = 10_000;


function sourcePins() {
  return {
    item_id: "gate-book",
    artifact_id: "capture-title",
    artifact_revision: "capture-title-r1",
    source_revision: "bytes-r1",
    source_sha256: "a".repeat(64),
  };
}


function proposal() {
  return {
    schema: PROPOSAL_SCHEMA,
    version: 1,
    coordinate_space: COORDINATE_SPACE,
    point_order: [...POINT_ORDER],
    quad: [[0.08, 0.12], [0.91, 0.08], [0.86, 0.94], [0.12, 0.89]],
    confidence: 0.91,
    detector: "mistral-layout",
    detector_version: "gate-r1",
    source_revision: "bytes-r1",
  };
}


function editorState(tool = TOOLS.IMAGE_ADJUST) {
  return createImageEditorState({
    proposal: proposal(),
    sourceRevision: "bytes-r1",
    tool,
    hasSelection: true,
  });
}


function spaceEvent() {
  return {
    key: " ",
    code: "Space",
    repeat: false,
    canvasFocused: true,
    canvasTarget: true,
    modalOpen: false,
    rectangleEditing: false,
    target: { tagName: "CANVAS" },
  };
}


function committedWithOcrFailure(operationId) {
  return {
    job_id: "correction-transform-gate",
    operation_id: operationId,
    image_commit: {
      operation_id: operationId,
      outputs: [
        { kind: "corrected-display", artifact_id: "corrected-display-gate" },
        { kind: "ocr-ready", artifact_id: "ocr-ready-gate" },
        { kind: "thumbnail", artifact_id: "thumbnail-gate" },
        { kind: "transform-manifest", artifact_id: "manifest-gate" },
      ],
    },
    ocr_followup: {
      state: "failed",
      source: { kind: "ocr-ready", artifact_id: "ocr-ready-gate" },
      proposal_ref: "",
      failure: {
        code: "ocr_followup_failed",
        message: "provider unavailable",
        retryable: true,
      },
    },
    cancelled_before_commit: false,
  };
}


test("release flow keeps one Space command across retry, closure, and OCR failure",
  () => {
    let state = editorState();
    const firstWindow = createImageAdjustTool({
      profile: { lastAppliedBrightness: -4 },
    });
    firstWindow.setBrightness(27);
    firstWindow.setRerunOcr(true);

    assert.equal(
      canQueueImageAdjustShortcut(spaceEvent(), state, sourcePins()),
      true,
    );
    const command = serializeCorrectionTransformCommand({
      pins: sourcePins(),
      quad: state.quad,
      adjustment: firstWindow.getAdjustment({ state }),
      rerunOcr: firstWindow.getRerunOcr(),
      operationId: "gate-space-op",
    });
    state = reduceImageEditorState(state, {
      type: "QUEUE_STARTED",
      command,
    });
    state = reduceImageEditorState(state, {
      type: "QUEUE_RETRYABLE",
      error: "queue response was lost",
    });
    assert.equal(state.submission.command, command);
    assert.equal(
      canQueueImageAdjustShortcut(spaceEvent(), state, sourcePins()),
      true,
      "a retryable response may safely retry the retained command",
    );

    const retainedCommand = state.submission.command;
    state = reduceImageEditorState(state, {
      type: "QUEUE_STARTED",
      command: retainedCommand,
    });
    state = reduceImageEditorState(state, {
      type: "QUEUE_ACCEPTED",
      jobId: "correction-transform-gate",
    });
    assert.equal(state.submission.command, command);
    assert.equal(
      canQueueImageAdjustShortcut(spaceEvent(), state, sourcePins()),
      false,
      "duplicate Space is inert after one logical job is accepted",
    );

    firstWindow.handleQueueAccepted(
      { job_id: "correction-transform-gate" },
      retainedCommand,
      { id: "capture-title" },
    );
    firstWindow.destroy();

    const reopenedWindow = createImageAdjustTool({
      profile: { lastAppliedBrightness: -4 },
    });
    const observed = reopenedWindow.observeTransformResult(
      committedWithOcrFailure("gate-space-op"),
      retainedCommand,
    );
    assert.equal(observed.imageCommitted, true);
    assert.equal(observed.profileChanged, true);
    assert.deepEqual(observed.profile, { lastAppliedBrightness: 27 });
    assert.equal(observed.ocrOutcome.state, "failed");
    assert.equal(
      observed.ocrOutcome.failure.code,
      "ocr_followup_failed",
      "image success remains independently usable and OCR failure stays visible",
    );

    const cancelWindow = createImageAdjustTool({
      profile: { lastAppliedBrightness: 9 },
    });
    const cancelled = cancelWindow.observeTransformResult({
      job_id: "correction-transform-cancelled",
      operation_id: "gate-cancelled-op",
      image_commit: null,
      ocr_followup: {
        state: "not_requested",
        source: null,
        proposal_ref: "",
        failure: null,
      },
      cancelled_before_commit: true,
    }, {
      ...retainedCommand,
      operation_id: "gate-cancelled-op",
    });
    assert.equal(cancelled.imageCommitted, false);
    assert.equal(cancelled.profileChanged, false);
    assert.deepEqual(cancelled.profile, { lastAppliedBrightness: 9 });
  });


function clearReview() {
  return {
    revision: "review-r1",
    state: "clear",
    reason: "",
    history_count: 0,
    latest_event: null,
  };
}


function capture(index) {
  return {
    artifact_id: `capture-${index}`,
    revision: `capture-r${index + 1}`,
    capture_order: index,
    label: `Page ${index + 1}`,
    effective_category: "other",
    resource_state: "available",
    import_state: "ready",
    freshness: "current",
    thumbnail: {
      url: `/api/v1/gate-book/thumbnails/${index}`,
      alt: `Page ${index + 1}`,
      width: 128,
      height: 192,
    },
  };
}


test("large cached thumbnail and artifact browsing stays bounded by release budgets",
  async () => {
    const payload = {
      schema: "librarytool.corrections-index/2",
      revision: "large-index-r1",
      books: [{
        id: "gate-book",
        revision: "gate-book-r1",
        kind: "book",
        title: "Large release-gate book",
        import_state: "ready",
        issues: [],
        review: clearReview(),
        captures: Array.from(
          { length: LARGE_BOOK_CAPTURE_COUNT },
          (_value, index) => capture(index),
        ),
      }],
      attention: [],
    };
    const artifacts = Array.from(
      { length: LARGE_ARTIFACT_COUNT },
      (_value, index) => ({
        key: `artifact:source-${index}`,
        label: `Source image ${index + 1}`,
        group: "source-images",
      }),
    );
    const groups = new Map([["source-images", {
      items: artifacts,
      total: artifacts.length,
      loaded: true,
      loading: false,
      nextCursor: null,
      error: null,
    }]]);

    const started = performance.now();
    const index = normalizeCorrectionsIndex(payload);
    const sorted = sortedBooks(index);
    const rows = buildArtifactTreeRows(groups, new Set(["source-images"]));
    const windowed = virtualArtifactWindow(rows, {
      rowHeight: 28,
      viewportHeight: 280,
      scrollTop: 140_000,
      overscan: 4,
      activeKey: "artifact:source-9000",
    });
    const elapsed = performance.now() - started;

    assert.equal(sorted[0].captures.length, LARGE_BOOK_CAPTURE_COUNT);
    assert.equal(sorted[0].captures[4_999].thumbnail.width, 128);
    assert.ok(windowed.rows.length <= 18,
      "ten visible rows plus four-row overscan on each side remain mounted");
    assert.ok(windowed.rows.some(
      (row) => row.key === "artifact:source-9000"),
    "the active descendant remains mounted without rendering all artifacts");
    assert.equal(windowed.totalHeight, rows.length * 28);
    assert.ok(elapsed < CACHED_BROWSE_BUDGET_MS,
      `cached large-book projection took ${elapsed.toFixed(2)} ms`);

    const documentRef = fakeDocument();
    const booksRoot = new FakeNode("nav", documentRef);
    const count = new FakeNode("span", documentRef);
    count.setAttribute("data-books-count", "");
    const filter = new FakeNode("input", documentRef);
    filter.setAttribute("data-books-filter", "");
    const list = new FakeNode("ul", documentRef);
    list.setAttribute("data-books-list", "");
    booksRoot.append(count, filter, list);
    const store = new CorrectionsIndexStore({
      api: { loadIndex: async () => payload },
    });
    const panel = new BooksPanelController({
      root: booksRoot,
      documentRef,
      store,
    }).mount();
    const renderStarted = performance.now();
    await store.openWorkspace("local-library");
    const renderElapsed = performance.now() - renderStarted;
    const captureButtons = list.querySelectorAll("[data-artifact-id]");
    assert.equal(captureButtons.length, 12,
      "the production Books controller bounds the initial capture DOM");
    assert.ok(list.querySelector("[data-captures-load-more]"),
      "the remaining captures stay explicitly reachable in bounded batches");
    assert.equal(captureButtons.at(-1).querySelector("img").loading, "lazy",
      "mounted thumbnail decoding remains delegated to the browser");
    assert.ok(renderElapsed < CACHED_BROWSE_BUDGET_MS,
      `cached Books rendering took ${renderElapsed.toFixed(2)} ms`);
    panel.destroy();
  });


test("screen-nearest pointer feedback stays inside the 100 ms local budget", () => {
  let state = editorState(TOOLS.PERSPECTIVE);
  const imageRect = { left: 80, top: 40, width: 800, height: 600 };
  const cornerIndex = nearestCornerIndex(state.quad, imageRect, [150, 125]);
  state = reduceImageEditorState(state, {
    type: "BEGIN_GESTURE",
    kind: "pointer",
    pointerId: 7,
    cornerIndex,
    point: state.quad[cornerIndex],
  });

  const started = performance.now();
  for (let index = 0; index < 100; index += 1) {
    const point = [0.08 + index / 10_000, 0.12 + index / 10_000];
    const nearest = nearestCornerIndex(state.quad, imageRect, [
      imageRect.left + point[0] * imageRect.width,
      imageRect.top + point[1] * imageRect.height,
    ]);
    state = reduceImageEditorState(state, {
      type: "MOVE_CORNER",
      cornerIndex: nearest,
      point,
    });
  }
  const elapsed = performance.now() - started;

  assert.ok(Math.abs(state.quad[0][0] - 0.0899) < 1e-12);
  assert.ok(Math.abs(state.quad[0][1] - 0.1299) < 1e-12);
  assert.equal(state.validation.valid, true);
  assert.ok(elapsed < POINTER_FEEDBACK_BUDGET_MS,
    `100 local pointer updates took ${elapsed.toFixed(2)} ms`);
});

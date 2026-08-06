const assert = require("node:assert/strict");
const test = require("node:test");

const {
  IMAGE_CATEGORIES,
  maskPolygonFromBoundingBox,
  serializeCorrectionTransformCommand,
} = require("../tools/whl_explorer/static/corrections/image-editor-state");
const {
  serializeRegionExtractionCommand,
} = require("../tools/whl_explorer/static/corrections/image-editor");
const {
  REGION_EXTRACTION_STATES,
} = require("../tools/whl_explorer/static/corrections/commands");
const {
  createClassificationController,
} = require("../tools/whl_explorer/static/corrections/keymap");
const {
  createArtifactOverlay,
} = require("../tools/whl_explorer/static/corrections/artifact-overlay");
const { FakeNode, fakeDocument } = require("./fixtures/corrections_fake_dom");


const BOX_POINTS = [[0.2, 0.3], [0.7, 0.3], [0.7, 0.8], [0.2, 0.8]];
const BOX_MASK = [[0.2, 0.3], [0.7, 0.3], [0.7, 0.8], [0.2, 0.8]];
const FULL_FRAME_QUAD = [[0, 0], [1, 0], [1, 1], [0, 1]];
const PLAIN_COMMAND_KEYS = [
  "schema", "version", "item_id", "artifact_id", "artifact_revision",
  "source_revision", "source_sha256", "quad", "adjustment", "rerun_ocr",
  "operation_id",
];


function _pins() {
  return {
    item_id: "item-1",
    artifact_id: "artifact-1",
    artifact_revision: "artifact-r4",
    source_revision: "source-r2",
    source_sha256: "a".repeat(64),
  };
}


function _resource() {
  return { id: "artifact-1", correction: { ..._pins() } };
}


function _region(overrides = {}) {
  return {
    key: "annotation:region-1",
    objectType: "spatial-annotation",
    kind: "mistral-box",
    itemId: "item-1",
    annotationId: "region-1",
    revision: "annotation-r3",
    label: "Machine box 1",
    // Every layout region the engine emits links to the page image it sits on
    // (corrections_artifact_repository.py sets linked_artifact_ids to the
    // display artifact). Omitting it here would let a guard that misreads that
    // link as "already extracted" pass its tests while being inert in the app.
    linkedArtifactIds: ["capture:asset-1:display"],
    selector: {
      coordinate_space: "exif_oriented_normalized",
      points: BOX_POINTS,
    },
    ...overrides,
  };
}


function _image() {
  return {
    key: "artifact:artifact-1",
    objectType: "raster-artifact",
    family: "image",
    kind: "page-image",
    itemId: "item-1",
    artifactId: "artifact-1",
    revision: "artifact-r4",
    label: "Page 12",
  };
}


function _port() {
  const transforms = [];
  const categories = [];
  return {
    transforms,
    categories,
    queueTransform(payload) {
      transforms.push(payload);
      return Promise.resolve({ job_id: "job-7" });
    },
    assignImageCategory(payload) {
      categories.push(payload);
      return Promise.resolve({ receipt: { targets: [] } });
    },
  };
}


function _harness(options = {}) {
  const documentRef = fakeDocument();
  const scope = new FakeNode("div", documentRef);
  const port = options.port || _port();
  const states = [];
  const errors = [];
  const controller = createClassificationController({
    scope,
    documentRef,
    port,
    serializeExtractionCommand: serializeRegionExtractionCommand,
    getExtractionResource: () => _resource(),
    onExtractionState: (detail) => states.push(detail),
    operationIdFactory: (prefix) => `${prefix}-op-1`,
    onError: (error) => errors.push(error),
    ...options.controller,
  }).mount();
  return { controller, documentRef, errors, port, scope, states };
}


function _tick() {
  return new Promise((resolve) => setTimeout(resolve, 0));
}


function _overlay(controller, documentRef, regions) {
  const stage = new FakeNode("div", documentRef);
  const overlay = createArtifactOverlay({
    root: stage,
    documentRef,
    getViewport: () => ({ width: 400, height: 300 }),
    onSoftTarget: (target, detail) => controller.setHotTarget(target, detail),
    onActivate: (target) => {
      const queued = controller.extractRegion(target);
      if (queued) void queued.catch(() => {});
      stage.dataset.activated = queued ? "extract" : "select";
    },
  }).mount();
  overlay.setRegions(regions, {
    sourceWidth: 1000,
    sourceHeight: 800,
    coordinateSpace: "exif_oriented_normalized",
  });
  return { overlay, stage };
}


function _shapes(stage) {
  return stage.querySelectorAll(".corrections-artifact-overlay-shape");
}


test("clicking a machine box queues an extraction with no category of its own", async () => {
  const { controller, documentRef, port } = _harness();
  const { stage } = _overlay(controller, documentRef, [_region()]);

  _shapes(stage)[0].emit("click");
  await _tick();

  assert.equal(stage.dataset.activated, "extract");
  assert.equal(port.transforms.length, 1);
  const command = port.transforms[0].command;
  assert.equal(command.extract, true);
  assert.deepEqual(command.mask_polygon, BOX_MASK);
  assert.equal(
    Object.prototype.hasOwnProperty.call(command, "extract_category"),
    false,
    "an unnamed extraction leaves the category for the classifier to suggest",
  );
  assert.deepEqual(command.quad, FULL_FRAME_QUAD);
  assert.equal(command.rerun_ocr, false);
  assert.equal(command.item_id, "item-1");
  assert.equal(command.artifact_id, "artifact-1");
  assert.equal(port.categories.length, 0);
});


test("a category hotkey over a box extracts it and asserts that category", async () => {
  const { controller, port, scope } = _harness();
  const region = _region();
  controller.setSelectionTarget(_image(), { focused: true });
  controller.setHotTarget(region);

  const event = scope.emit("keydown", { key: "t", target: scope });
  await _tick();

  assert.equal(event.defaultPrevented, true);
  assert.equal(port.transforms.length, 1);
  const command = port.transforms[0].command;
  assert.equal(command.extract, true);
  assert.equal(command.extract_category, "title_page");
  assert.deepEqual(command.mask_polygon, BOX_MASK);
  assert.equal(
    port.categories.length,
    0,
    "the hovered box takes the hotkey instead of the page image behind it",
  );
});


test("each category hotkey carries its own value into the extraction", async () => {
  for (const [binding, category] of [
    ["c", "cover"], ["s", "spine"], ["e", "content_specimen"],
  ]) {
    const { controller, port, scope } = _harness();
    controller.setHotTarget(_region());
    scope.emit("keydown", { key: binding, target: scope });
    await _tick();
    assert.equal(port.transforms[0].command.extract_category, category);
  }
});


test("a category hotkey with nothing hovered still assigns the page image", async () => {
  const { controller, port, scope } = _harness();
  controller.setSelectionTarget(_image(), { focused: true });
  controller.setHotTarget(null);

  scope.emit("keydown", { key: "t", target: scope });
  await _tick();

  assert.equal(port.transforms.length, 0);
  assert.equal(port.categories.length, 1);
  assert.equal(port.categories[0].category, "title_page");
  assert.equal(port.categories[0].artifactId, "artifact-1");
  assert.equal(port.categories[0].expectedArtifactRevision, "artifact-r4");
});


test("a degenerate box is refused before anything reaches the port", async () => {
  const { controller, port } = _harness();
  const flat = _region({
    selector: { points: [[0.2, 0.3], [0.2, 0.3], [0.2, 0.8], [0.2, 0.8]] },
  });

  await assert.rejects(
    () => controller.extraction.queue(flat, {}),
    (error) => error.code === "polygon-duplicate-vertices",
  );
  assert.equal(port.transforms.length, 0);
  assert.equal(
    controller.extraction.stateFor(flat),
    "",
    "a command that never left the client leaves no in-flight state behind",
  );

  const sliver = _region({
    selector: { points: [[0.2, 0.3], [0.2001, 0.3], [0.2001, 0.8], [0.2, 0.8]] },
  });
  await assert.rejects(
    () => controller.extraction.queue(sliver, {}),
    (error) => error.code === "polygon-area",
  );
  assert.equal(port.transforms.length, 0);
});


test("a box that is already extracting is not extracted a second time", async () => {
  const { controller, documentRef, port, states } = _harness();
  const region = _region();
  const { stage } = _overlay(controller, documentRef, [region]);

  _shapes(stage)[0].emit("click");
  await _tick();
  assert.deepEqual(states.map((detail) => detail.status), [
    REGION_EXTRACTION_STATES.QUEUEING, REGION_EXTRACTION_STATES.QUEUED,
  ]);
  assert.equal(states[1].jobId, "job-7");

  assert.equal(controller.extraction.available(region), false);
  assert.equal(controller.extractRegion(region), null);
  _shapes(stage)[0].emit("click");
  await _tick();
  assert.equal(port.transforms.length, 1);
  assert.equal(stage.dataset.activated, "select");
  assert.equal(
    _shapes(stage)[0].parentNode.dataset.extraction,
    undefined,
    "the marker only carries state the overlay was told about",
  );
});


test("the mask follows the orientation the box was drawn under", async () => {
  const { controller, documentRef, port } = _harness();
  const region = _region();
  const { overlay, stage } = _overlay(controller, documentRef, [region]);
  // Selector coordinates are declared against the undecoded frame, so a
  // quarter-turn image has to rotate the ring before it becomes a mask.
  overlay.setView({ orientation: 6 });
  controller.extraction.getGeometry = (target) =>
    overlay.orientedRegionPolygon(target.key);

  _shapes(stage)[0].emit("click");
  await _tick();

  const mask = port.transforms[0].command.mask_polygon;
  assert.equal(mask.length, 4);
  for (const [index, corner] of [
    [0.2, 0.2], [0.7, 0.2], [0.7, 0.7], [0.2, 0.7],
  ].entries()) {
    assert.ok(
      Math.abs(mask[index][0] - corner[0]) < 1e-9 &&
      Math.abs(mask[index][1] - corner[1]) < 1e-9,
      `corner ${index} rotates onto ${corner.join(", ")}`,
    );
  }
});


test("the overlay stamps extraction state onto the marker and keeps it across renders", () => {
  const { controller, documentRef } = _harness();
  const region = _region();
  const { overlay, stage } = _overlay(controller, documentRef, [region]);

  overlay.setExtractionState(region.key, REGION_EXTRACTION_STATES.QUEUEING);
  assert.equal(
    _shapes(stage)[0].parentNode.dataset.extraction, "queueing");

  overlay.setView({ zoom: 2 });
  assert.equal(
    _shapes(stage)[0].parentNode.dataset.extraction,
    "queueing",
    "a re-render must not wipe the state the reviewer is reading",
  );

  overlay.setExtractionState(region.key, "");
  assert.equal(_shapes(stage)[0].parentNode.dataset.extraction, undefined);
  assert.equal(overlay.extractionState(region.key), "");
});


test("a production-shaped region carrying its page link is still extractable", async () => {
  // Regression: every layout region links to the page image it sits on, so a
  // guard that reads any link as "already cropped" makes the trigger inert
  // against real data while still passing a fixture that omits the field.
  const { controller, port, scope } = _harness();
  controller.setSelectionTarget(_image(), { focused: true });
  controller.setHotTarget(_region());

  scope.emit("keydown", { key: "t", target: scope });
  await _tick();

  assert.equal(port.transforms.length, 1);
  assert.equal(port.categories.length, 0);
  assert.equal(port.transforms[0].command.extract, true);
});


test("re-extracting one region reuses its operation so the engine replays", async () => {
  // De-duplication is durable rather than in-memory: the operation id is
  // derived from what is being cropped, so a reload and a second click resolve
  // to the original publication instead of an unreconciled second figure.
  const first = _harness();
  first.controller.setSelectionTarget(_image(), { focused: true });
  first.controller.setHotTarget(_region());
  first.scope.emit("keydown", { key: "t", target: first.scope });
  await _tick();

  const reloaded = _harness();
  reloaded.controller.setSelectionTarget(_image(), { focused: true });
  reloaded.controller.setHotTarget(_region());
  reloaded.scope.emit("keydown", { key: "t", target: reloaded.scope });
  await _tick();

  assert.equal(first.port.transforms.length, 1);
  assert.equal(reloaded.port.transforms.length, 1);
  assert.equal(
    reloaded.port.transforms[0].command.operation_id,
    first.port.transforms[0].command.operation_id,
    "a fresh session must resolve the same region to the same operation",
  );
  assert.notEqual(first.port.transforms[0].command.operation_id, "");
});


test("extraction stays unavailable when the shell injects no serializer", async () => {
  const { controller, port, scope } = _harness({
    controller: { serializeExtractionCommand: null },
  });
  controller.setSelectionTarget(_image(), { focused: true });
  controller.setHotTarget(_region());

  scope.emit("keydown", { key: "t", target: scope });
  await _tick();

  assert.equal(port.transforms.length, 0);
  assert.equal(port.categories.length, 1);
});


test("a failed queue releases the box so the reviewer can try again", async () => {
  const failures = [];
  const port = _port();
  port.queueTransform = (payload) => {
    failures.push(payload);
    return Promise.reject(new Error("the sidecar is offline"));
  };
  const { controller, states } = _harness({ port });
  const region = _region();

  await assert.rejects(() => controller.extraction.queue(region, {}));
  assert.deepEqual(states.map((detail) => detail.status), ["queueing", ""]);
  assert.equal(controller.extraction.available(region), true);
  assert.equal(failures.length, 1);
});


test("a bounding box becomes a four corner mask and a degenerate one is refused", () => {
  assert.deepEqual(
    maskPolygonFromBoundingBox({ x: 0.2, y: 0.3, width: 0.5, height: 0.5 }).polygon,
    BOX_MASK,
  );
  assert.deepEqual(
    maskPolygonFromBoundingBox({
      left: 0.2, top: 0.3, right: 0.7, bottom: 0.8,
    }).polygon,
    BOX_MASK,
  );
  assert.deepEqual(maskPolygonFromBoundingBox(BOX_POINTS).polygon, BOX_MASK);
  assert.deepEqual(
    maskPolygonFromBoundingBox({ points: [[0.2, 0.8], [0.7, 0.3]] }).polygon,
    BOX_MASK,
    "two opposite corners still describe the whole box",
  );

  assert.equal(maskPolygonFromBoundingBox(null).valid, false);
  assert.equal(
    maskPolygonFromBoundingBox({ left: 0.2, top: "0.3", right: 0.7, bottom: 0.8 }).code,
    "polygon-box-bounds",
  );
  assert.equal(
    maskPolygonFromBoundingBox({ x: 0.2, y: 0.3, width: 0, height: 0.5 }).code,
    "polygon-duplicate-vertices",
  );
  assert.equal(
    maskPolygonFromBoundingBox({ x: 0.2, y: 0.3, width: 0.5, height: 2 }).code,
    "polygon-coordinate-bounds",
  );
});


test("extract and extract_category are emitted only when they are carried", () => {
  const base = {
    pins: _pins(),
    quad: FULL_FRAME_QUAD,
    adjustment: null,
    rerunOcr: false,
    operationId: "correction-op-1",
  };

  const plain = serializeCorrectionTransformCommand(base);
  assert.deepEqual(Object.keys(plain), PLAIN_COMMAND_KEYS);
  assert.equal(Object.keys(plain).length, 11);

  const declined = serializeCorrectionTransformCommand({
    ...base, extract: false, extractCategory: "",
  });
  assert.deepEqual(Object.keys(declined), PLAIN_COMMAND_KEYS);

  const extracted = serializeCorrectionTransformCommand({
    ...base, maskPolygon: BOX_MASK, extract: true,
  });
  assert.equal(extracted.extract, true);
  assert.equal(
    Object.prototype.hasOwnProperty.call(extracted, "extract_category"), false);

  const named = serializeCorrectionTransformCommand({
    ...base, maskPolygon: BOX_MASK, extract: true, extractCategory: "cover",
  });
  assert.deepEqual(Object.keys(named), [
    ...PLAIN_COMMAND_KEYS, "mask_polygon", "extract", "extract_category",
  ]);
  assert.equal(named.extract_category, "cover");
});


test("the serializer refuses the extractions the engine would reject", () => {
  const base = {
    pins: _pins(),
    quad: FULL_FRAME_QUAD,
    adjustment: null,
    rerunOcr: false,
    operationId: "correction-op-1",
  };

  assert.throws(() => serializeCorrectionTransformCommand({
    ...base, extract: true,
  }), /extraction requires a mask polygon/);
  assert.throws(() => serializeCorrectionTransformCommand({
    ...base, maskPolygon: BOX_MASK, extract: true, rerunOcr: true,
  }), /cannot request an OCR follow-up/);
  assert.throws(() => serializeCorrectionTransformCommand({
    ...base, maskPolygon: BOX_MASK, extractCategory: "cover",
  }), /extract_category requires extract/);
  assert.throws(() => serializeCorrectionTransformCommand({
    ...base, maskPolygon: BOX_MASK, extract: true, extractCategory: "frontispiece",
  }), /canonical image vocabulary/);
  assert.throws(() => serializeCorrectionTransformCommand({
    ...base, maskPolygon: BOX_MASK, extract: "yes",
  }), /extract must be boolean/);

  assert.deepEqual(IMAGE_CATEGORIES.slice().sort(), [
    "content_specimen", "cover", "other", "spine", "title_page",
  ]);
});

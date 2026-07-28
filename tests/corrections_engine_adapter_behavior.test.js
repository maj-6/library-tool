const assert = require("node:assert/strict");
const test = require("node:test");

const {
  correctionTransformJob,
  createCorrectionsEnginePorts,
  decorateRasterArtifact,
  decorateSpatialAnnotation,
} = require(
  "../tools/whl_explorer/static/corrections/engine-adapter",
);


function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((resolveValue, rejectValue) => {
    resolve = resolveValue;
    reject = rejectValue;
  });
  return { promise, reject, resolve };
}


function raster(id, kind = "captured-image", overrides = {}) {
  return {
    key: { item_id: "book-1", artifact_id: id },
    revision: `${id}-r1`,
    kind,
    label: id,
    media_type: "image/jpeg",
    content_sha256: "a".repeat(64),
    dimensions: { width: 1200, height: 1600, orientation: 1 },
    resource_state: "available",
    resource: {
      id: `resource:${id}`,
      revision: `${id}-resource-r1`,
      variant: "display",
    },
    freshness: "current",
    source: {
      representation_id: "scan-1",
      representation_revision: "scan-r1",
      canvas_id: "page-1",
      canvas_revision: "page-r1",
    },
    extensions: {},
    ...overrides,
  };
}


function annotation(id, overrides = {}) {
  return {
    key: { item_id: "book-1", annotation_id: id },
    revision: `${id}-r1`,
    label: id,
    freshness: "current",
    source: {
      representation_id: "scan-1",
      representation_revision: "scan-r1",
      canvas_id: "page-1",
      canvas_revision: "page-r1",
    },
    selector: {
      type: "polygon",
      coordinate_space: "canvas-normalized",
      coordinate_space_revision: "page-r1",
      points: [
        { x: 0.1, y: 0.1 },
        { x: 0.4, y: 0.1 },
        { x: 0.4, y: 0.4 },
      ],
    },
    linked_artifact_ids: [],
    role_assignments: [],
    caption_assertions: [],
    effective_role: "",
    provenance: { origin: "ocr", provider_id: "mistral" },
    extensions: {},
    ...overrides,
  };
}


function engineHarness(overrides = {}) {
  const calls = {
    corrections: [],
    rasterGet: [],
    rasterList: [],
    resourceUrl: [],
    spatialGet: [],
    spatialList: [],
  };
  const engineClient = {
    rasterArtifacts: {
      async list(args) {
        calls.rasterList.push(args);
        return {
          revision: "raster-inventory-r1",
          artifacts: [],
          next_cursor: null,
          total: 0,
        };
      },
      async get(args) {
        calls.rasterGet.push(args);
        return { artifact: raster(args.artifactId) };
      },
      resourceUrl(args) {
        calls.resourceUrl.push(args);
        return `/api/raster/${args.artifactId}?revision=${args.revision}`;
      },
      ...overrides.rasterArtifacts,
    },
    spatialAnnotations: {
      async list(args) {
        calls.spatialList.push(args);
        return {
          revision: "spatial-inventory-r1",
          annotations: [],
          next_cursor: null,
          total: 0,
        };
      },
      async get(args) {
        calls.spatialGet.push(args);
        return { annotation: annotation(args.annotationId) };
      },
      ...overrides.spatialAnnotations,
    },
  };
  if (overrides.corrections) {
    engineClient.corrections = overrides.corrections;
  }
  if (overrides.jobs) {
    engineClient.jobs = overrides.jobs;
  }
  return { calls, engineClient };
}


function transformCommand(overrides = {}) {
  return {
    schema: "org.whl.correction-transform-command",
    version: 1,
    item_id: "book-1",
    artifact_id: "capture-1",
    artifact_revision: "artifact-r1",
    source_revision: "source-r1",
    source_sha256: "a".repeat(64),
    quad: [[0, 0], [1, 0], [1, 1], [0, 1]],
    adjustment: {
      schema: "org.whl.raster.manual-binary-adjust",
      version: 1,
      algorithm: "grayscale-threshold-blend-v1",
      contrast_percent: 100,
      brightness_percent: 12,
      threshold: 112,
      threshold_rule:
        "round_half_up(127.5 - brightness_percent * 1.275), clamped_0_255",
      comparison: "grayscale_value > threshold",
    },
    rerun_ocr: true,
    operation_id: "transform-op-1",
    ...overrides,
  };
}


function transformJob(command, overrides = {}) {
  return {
    id: "correction-transform-job-1",
    kind: "correction.transform",
    state: "queued",
    subject: {
      item_id: command.item_id,
      source_id: command.artifact_id,
    },
    progress: { completed: 0, total: 6, unit: "phase", phase: "queued" },
    cancellable: true,
    revision: 1,
    created_at: "2026-07-27T12:00:00Z",
    updated_at: "2026-07-27T12:00:00Z",
    finished_at: "",
    note: "queued",
    error: null,
    input_revisions: {
      artifact_id: command.artifact_id,
      artifact_revision: command.artifact_revision,
      source_revision: command.source_revision,
      source_sha256: command.source_sha256,
      operation_id: command.operation_id,
    },
    outputs: [],
    ...overrides,
  };
}


function transformOutputs() {
  return [
    {
      kind: "corrected-display",
      ref: "result-corrected-display-1",
      partial: false,
    },
    { kind: "ocr-ready", ref: "result-ocr-ready-1", partial: false },
    { kind: "thumbnail", ref: "result-thumbnail-1", partial: false },
    {
      kind: "transform-manifest",
      ref: "result-transform-manifest-1",
      partial: false,
    },
  ];
}


function manualScheduler() {
  const scheduled = [];
  return {
    cancelSchedule(token) {
      token.cancelled = true;
    },
    schedule(callback, delay) {
      const token = { callback, cancelled: false, delay };
      scheduled.push(token);
      return token;
    },
    scheduled,
  };
}


test("engine decorations preserve transport values and supply artifact model identity", () => {
  const rawRaster = raster("capture:asset-1:display");
  const rawAnnotation = annotation("region:1");

  const decoratedRaster = decorateRasterArtifact(rawRaster);
  const decoratedAnnotation = decorateSpatialAnnotation(rawAnnotation);

  assert.equal(decoratedRaster.object_type, "raster-artifact");
  assert.equal(decoratedRaster.artifact_id, "capture:asset-1:display");
  assert.equal(decoratedRaster.group, "source-images");
  assert.deepEqual(decoratedRaster.correction, {
    item_id: "book-1",
    artifact_id: "capture:asset-1:display",
    artifact_revision: "capture:asset-1:display-r1",
    source_revision: "capture:asset-1:display-resource-r1",
    source_sha256: "a".repeat(64),
    proposal: null,
  });
  assert.equal(decoratedAnnotation.object_type, "spatial-annotation");
  assert.equal(decoratedAnnotation.annotation_id, "region:1");
  assert.equal(decoratedAnnotation.kind, "spatial-annotation");
  assert.equal(decoratedAnnotation.group, "layout-regions");
  assert.equal(Object.hasOwn(rawRaster, "object_type"), false);
  assert.equal(Object.hasOwn(rawAnnotation, "object_type"), false);
});


test("raster catalog delegates group paging in one bounded engine call", async () => {
  const { calls, engineClient } = engineHarness({
    rasterArtifacts: {
      async list(args) {
        calls.rasterList.push(args);
        const artifacts = args.group === "generated-images"
          ? [raster("future-1", "ai-upscaled-image")]
          : [raster("capture-1")];
        return {
          revision: "raster-inventory-r1",
          artifacts,
          next_cursor: null,
          total: 1,
        };
      },
    },
  });
  const ports = createCorrectionsEnginePorts(engineClient);
  const page = await ports.artifacts.catalog.list({
    context: {
      itemId: "book-1",
      representationId: "scan-1",
      canvasId: "page-1",
    },
    group: "source-images",
    cursor: null,
    limit: 20,
  });

  assert.deepEqual(page.items.map((item) => item.key.artifact_id), ["capture-1"]);
  assert.equal(page.nextCursor, null);
  assert.equal(page.total, 1);
  assert.deepEqual(calls.rasterList.map((call) => call.cursor), [null]);
  assert.equal(calls.rasterList[0].group, "source-images");
  assert.equal(calls.rasterList[0].itemId, "book-1");
  assert.equal(calls.rasterList[0].representationId, "scan-1");
  assert.equal(calls.rasterList[0].canvasId, "page-1");

  const future = await ports.artifacts.catalog.list({
    context: { itemId: "book-1" },
    group: "generated-images",
    cursor: null,
    limit: 20,
  });
  assert.deepEqual(
    future.items.map((item) => item.key.artifact_id),
    ["future-1"],
  );
  assert.equal(future.items[0].group, "generated-images");

  const empty = await ports.artifacts.catalog.list({
    context: { item_id: "book-1" },
    group: "ocr-text",
    cursor: null,
    limit: 20,
  });
  assert.deepEqual(empty.items, []);
  assert.equal(calls.rasterList.length, 2,
    "groups outside the #227 raster/spatial projection do not issue broad reads");
});


test("spatial catalog and region resources retain engine paging", async () => {
  const first = annotation("region-1");
  const second = annotation("region-2", {
    source: {
      representation_id: "scan-7",
      representation_revision: "scan-r7",
      canvas_id: "page-7",
      canvas_revision: "page-r7",
    },
  });
  const { calls, engineClient } = engineHarness({
    spatialAnnotations: {
      async list(args) {
        calls.spatialList.push(args);
        return {
          revision: "spatial-inventory-r1",
          annotations: args.cursor ? [second] : [first],
          next_cursor: args.cursor ? null : "regions-2",
          total: 2,
        };
      },
      async get(args) {
        calls.spatialGet.push(args);
        return { annotation: first };
      },
    },
  });
  const ports = createCorrectionsEnginePorts(engineClient);
  const page = await ports.artifacts.catalog.list({
    context: { item_id: "book-1", representation_id: "scan-1" },
    group: "layout-regions",
    cursor: null,
    limit: 10,
  });
  assert.equal(page.items[0].object_type, "spatial-annotation");
  assert.equal(page.nextCursor, "regions-2");
  assert.equal(page.total, 2);

  const next = await ports.artifacts.resources.listRegions({
    context: { itemId: "book-1", representationId: "scan-1" },
    representationId: "scan-7",
    canvasId: "page-7",
    cursor: "regions-2",
    limit: 10,
  });
  assert.deepEqual(next.items.map((item) => item.key.annotation_id), ["region-2"]);
  assert.equal(calls.spatialList[1].representationId, "scan-7");
  assert.equal(calls.spatialList[1].canvasId, "page-7");

  const detail = await ports.artifacts.catalog.get({
    context: { itemId: "book-1" },
    key: "annotation:region-1",
  });
  assert.equal(detail.key.annotation_id, "region-1");
  assert.deepEqual(calls.spatialGet[0], {
    itemId: "book-1",
    annotationId: "region-1",
    signal: undefined,
  });
});


test("raster details advertise paged regions and pin resource URLs to revisions", async () => {
  const figure = raster("figure:1", "extracted-figure");
  const pageImage = raster("page-image:1", "page-image");
  const display = raster(
    "capture:asset-1:display",
    "processed-image",
    {
      source: {
        representation_id: "capture",
        representation_revision: "capture-r1",
        canvas_id: "capture:asset-1",
        canvas_revision: "display-r1",
      },
    },
  );
  const region = annotation("figure-region", {
    linked_artifact_ids: ["capture:asset-1:display"],
    source: {
      representation_id: "capture",
      representation_revision: "capture-r1",
      canvas_id: "capture:asset-1",
      canvas_revision: "display-r1",
    },
  });
  const staleRegion = annotation("stale-region", {
    source: {
      representation_id: "capture",
      representation_revision: "capture-r1",
      canvas_id: "capture:asset-1",
      canvas_revision: "display-r0",
    },
  });
  const { calls, engineClient } = engineHarness({
    rasterArtifacts: {
      async get(args) {
        calls.rasterGet.push(args);
        return {
          artifact: args.artifactId === "figure:1"
            ? figure
            : args.artifactId === "page-image:1"
              ? pageImage
              : display,
        };
      },
    },
    spatialAnnotations: {
      async list(args) {
        calls.spatialList.push(args);
        return {
          revision: "spatial-inventory-r1",
          annotations: [region, staleRegion],
          next_cursor: null,
          total: 2,
        };
      },
    },
  });
  const ports = createCorrectionsEnginePorts(engineClient);
  const detail = await ports.artifacts.catalog.get({
    context: { item_id: "book-1", canvas_id: "ignored-context-canvas" },
    key: "artifact:capture:asset-1:display",
  });

  assert.equal(detail.group, "processed-images");
  assert.equal(detail.extensions.corrections_ui.paged_regions, true);
  assert.equal(calls.spatialList.length, 0,
    "the editor loads annotations through its bounded paging port");

  const regions = await ports.artifacts.resources.listRegions({
    context: { itemId: "book-1", representationId: "scan-1" },
    representationId: "capture",
    canvasId: "capture:asset-1",
    canvasRevision: "display-r1",
    cursor: null,
    limit: 200,
  });
  assert.equal(regions.items[0].annotation_id, "figure-region");
  assert.equal(regions.items.length, 1,
    "annotations from another canvas revision stay out of the editor");
  assert.equal(calls.spatialList[0].representationId, "capture");
  assert.equal(calls.spatialList[0].canvasId, "capture:asset-1");
  assert.equal(calls.spatialList[0].canvasRevision, "display-r1");

  const crop = await ports.artifacts.catalog.get({
    context: { itemId: "book-1" },
    key: "artifact:figure:1",
  });
  assert.equal(crop.group, "extracted-figures");
  assert.equal(crop.extensions.corrections_ui.paged_regions, false,
    "page-space boxes must not be drawn directly over extracted crop bytes");

  const fullCanvas = await ports.artifacts.catalog.get({
    context: { itemId: "book-1" },
    key: "artifact:page-image:1",
  });
  assert.equal(fullCanvas.extensions.corrections_ui.paged_regions, true,
    "known full-canvas rasters retain revision-filtered overlays");

  const resolved = ports.artifacts.resources.resolveRaster({
    itemId: "book-1",
    artifactId: "figure:1",
    resourceRef: {
      id: "resource:figure:1",
      revision: "figure-resource-r7",
      variant: "display",
    },
    variant: "display",
  });
  assert.equal(
    resolved.url,
    "/api/raster/figure:1?revision=figure-resource-r7",
  );
  assert.deepEqual(calls.resourceUrl[0], {
    itemId: "book-1",
    artifactId: "figure:1",
    revision: "figure-resource-r7",
  });
});


test("adapter fails closed when the required engine surfaces are incomplete", async () => {
  assert.throws(
    () => createCorrectionsEnginePorts({ rasterArtifacts: {} }),
    /require rasterArtifacts and spatialAnnotations/,
  );
  const { engineClient } = engineHarness();
  const ports = createCorrectionsEnginePorts(engineClient);
  assert.equal(Object.hasOwn(ports.artifacts, "commands"), false,
    "read-only engine clients do not advertise mutation commands");
  await assert.rejects(
    ports.artifacts.resources.readText(),
    (error) => error.code === "capability-unavailable",
  );
  await assert.rejects(
    ports.artifacts.catalog.get({
      context: { itemId: "book-1" },
      key: "job:not-an-artifact",
    }),
    /catalog key is invalid/,
  );
});


test("classification commands delegate operation IDs and revision pins", async () => {
  const invocations = [];
  const corrections = {
    async assignImageCategory(payload) {
      invocations.push(["assignImageCategory", payload]);
      return { receipt: { action: "category.assign" } };
    },
    async clearImageCategory(payload) {
      invocations.push(["clearImageCategory", payload]);
      return { receipt: { action: "category.clear" } };
    },
    async assignRegionRole(payload) {
      invocations.push(["assignRegionRole", payload]);
      return { receipt: { action: "role.assign" } };
    },
    async clearRegionRole(payload) {
      invocations.push(["clearRegionRole", payload]);
      return { receipt: { action: "role.clear" } };
    },
  };
  const { engineClient } = engineHarness({ corrections });
  const { commands } = createCorrectionsEnginePorts(engineClient).artifacts;
  const signal = new AbortController().signal;

  await commands.assignImageCategory({
    itemId: "book-1",
    artifactId: "image-1",
    expectedArtifactRevision: "image-r1",
    category: "cover",
    operationId: "category-op",
    signal,
  });
  await commands.clearImageCategory({
    itemId: "book-1",
    artifactId: "image-1",
    expectedArtifactRevision: "image-r2",
    operationId: "category-clear-op",
  });
  await commands.assignRegionRole({
    itemId: "book-1",
    annotationId: "region-1",
    expectedAnnotationRevision: "region-r1",
    role: "figure",
    linkedArtifactId: "figure-1",
    expectedLinkedArtifactRevision: "figure-r1",
    operationId: "role-op",
  });
  await commands.clearRegionRole({
    itemId: "book-1",
    annotationId: "region-1",
    expectedAnnotationRevision: "region-r2",
    operationId: "role-clear-op",
  });

  assert.deepEqual(invocations, [
    ["assignImageCategory", {
      itemId: "book-1",
      artifactId: "image-1",
      expectedArtifactRevision: "image-r1",
      category: "cover",
      idempotencyKey: "category-op",
      signal,
    }],
    ["clearImageCategory", {
      itemId: "book-1",
      artifactId: "image-1",
      expectedArtifactRevision: "image-r2",
      idempotencyKey: "category-clear-op",
    }],
    ["assignRegionRole", {
      itemId: "book-1",
      annotationId: "region-1",
      expectedAnnotationRevision: "region-r1",
      role: "figure",
      linkedArtifactId: "figure-1",
      expectedLinkedArtifactRevision: "figure-r1",
      idempotencyKey: "role-op",
    }],
    ["clearRegionRole", {
      itemId: "book-1",
      annotationId: "region-1",
      expectedAnnotationRevision: "region-r2",
      idempotencyKey: "role-clear-op",
    }],
  ]);
});


test("transform commands bridge the image editor to EngineClient", async () => {
  const calls = [];
  const command = {
    schema: "org.whl.correction-transform-command",
    operation_id: "transform-op",
  };
  const { engineClient } = engineHarness({
    corrections: {
      async queueTransform(payload) {
        calls.push(payload);
        return { job_id: "correction-transform-job-1" };
      },
    },
  });
  const ports = createCorrectionsEnginePorts(engineClient);
  const signal = new AbortController().signal;

  const result = await ports.invokeCommand(
    "corrections.transform.queue",
    { command, signal, trigger: "keyboard", resource: { id: "image-1" } },
  );

  assert.equal(result.job_id, "correction-transform-job-1");
  assert.deepEqual(calls, [{ command, signal }]);
  assert.equal(typeof ports.artifacts.commands.queueTransform, "function");
  await assert.rejects(
    ports.invokeCommand("corrections.transform.unknown", { command }),
    (error) => error.code === "capability-unavailable",
  );
});


test("transform polling converts a terminal image commit and OCR failure", {
  timeout: 1000,
}, async () => {
  const command = transformCommand();
  const scheduler = manualScheduler();
  const jobReads = [];
  const activeJob = transformJob(command);
  const runningJob = transformJob(command, {
    state: "running",
    progress: {
      completed: 2,
      total: 6,
      unit: "phase",
      phase: "transforming",
    },
    revision: 2,
    updated_at: "2026-07-27T12:00:01Z",
    note: "transforming",
  });
  const terminalJob = transformJob(command, {
    state: "done",
    progress: { completed: 6, total: 6, unit: "phase", phase: "complete" },
    cancellable: false,
    revision: 4,
    updated_at: "2026-07-27T12:00:02Z",
    finished_at: "2026-07-27T12:00:02Z",
    note: "image committed; OCR follow-up failed",
    input_revisions: {
      ...activeJob.input_revisions,
      command_sha256: "b".repeat(64),
    },
    outputs: transformOutputs(),
  });
  const { engineClient } = engineHarness({
    corrections: {
      async queueTransform() {
        return {
          job_id: activeJob.id,
          operation_id: command.operation_id,
          job: activeJob,
        };
      },
    },
    jobs: {
      async get(args) {
        jobReads.push(args);
        return {
          ok: true,
          job: jobReads.length === 1 ? runningJob : terminalJob,
        };
      },
    },
  });
  const ports = createCorrectionsEnginePorts(engineClient, {
    transformPolling: scheduler,
  });
  const observed = deferred();
  const unsubscribe = ports.transforms.subscribeResults(
    (result, observedCommand) => observed.resolve([result, observedCommand]),
  );

  await ports.invokeCommand("corrections.transform.queue", { command });
  assert.equal(scheduler.scheduled.length, 1);
  scheduler.scheduled.shift().callback();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(scheduler.scheduled.length, 1);
  scheduler.scheduled.shift().callback();
  const [result, observedCommand] = await observed.promise;

  assert.deepEqual(jobReads, [
    { jobId: activeJob.id },
    { jobId: activeJob.id },
  ]);
  assert.equal(result.terminal_state, "done");
  assert.equal(result.operation_id, command.operation_id);
  assert.deepEqual(
    result.image_commit.outputs.map((output) => output.kind),
    ["corrected-display", "ocr-ready", "thumbnail", "transform-manifest"],
  );
  assert.equal(result.ocr_followup.state, "failed");
  assert.deepEqual(result.ocr_followup.source, {
    kind: "ocr-ready",
    artifact_id: "result-ocr-ready-1",
  });
  assert.equal(result.failure, null);
  assert.deepEqual(observedCommand, command);
  const normalized = correctionTransformJob(
    terminalJob, command, activeJob.id,
  );
  assert.equal(
    Object.hasOwn(normalized.input_revisions, "command_sha256"),
    false,
  );
  unsubscribe();
});


test("transform polling rejects a completed job without committed outputs", {
  timeout: 1000,
}, async () => {
  const command = transformCommand({ rerun_ocr: false });
  const scheduler = manualScheduler();
  const activeJob = transformJob(command);
  const invalidTerminal = transformJob(command, {
    state: "done",
    progress: { completed: 6, total: 6, unit: "phase", phase: "complete" },
    cancellable: false,
    revision: 2,
    updated_at: "2026-07-27T12:00:02Z",
    finished_at: "2026-07-27T12:00:02Z",
    note: "correction complete",
  });
  const { engineClient } = engineHarness({
    corrections: {
      async queueTransform() {
        return { job_id: activeJob.id, job: activeJob };
      },
    },
    jobs: {
      async get() {
        return { ok: true, job: invalidTerminal };
      },
    },
  });
  const ports = createCorrectionsEnginePorts(engineClient, {
    transformPolling: scheduler,
  });
  const observed = deferred();
  ports.transforms.subscribeResults((result) => observed.resolve(result));

  await ports.invokeCommand("corrections.transform.queue", { command });
  scheduler.scheduled.shift().callback();
  const result = await observed.promise;

  assert.equal(result.terminal_state, "failed");
  assert.equal(result.image_commit, null);
  assert.equal(result.ocr_followup, null);
  assert.equal(result.failure.code, "invalid-transform-job-result");
  assert.equal(result.failure.retryable, false);
});


test("caption metadata and review commands delegate operation IDs", async () => {
  const invocations = [];
  const corrections = {};
  for (const name of [
    "setManualCaption",
    "clearManualCaption",
    "assertArtifactMetadata",
    "markAttention",
    "resolveCorrections",
    "reopenCorrections",
  ]) {
    corrections[name] = async (payload) => {
      invocations.push([name, payload]);
      return { receipt: { action: name } };
    };
  }
  const { engineClient } = engineHarness({ corrections });
  const { commands } = createCorrectionsEnginePorts(engineClient).artifacts;
  const signal = new AbortController().signal;

  await commands.setManualCaption({
    itemId: "book-1",
    artifactId: "image-1",
    expectedArtifactRevision: "image-r1",
    text: "Human caption",
    language: "en",
    operationId: "caption-set-op",
    signal,
  });
  await commands.clearManualCaption({
    itemId: "book-1",
    artifactId: "image-1",
    expectedArtifactRevision: "image-r2",
    operationId: "caption-clear-op",
  });
  await commands.assertArtifactMetadata({
    itemId: "book-1",
    artifactId: "image-1",
    expectedArtifactRevision: "image-r3",
    assertions: { plate_number: 8 },
    clearNames: [],
    operationId: "metadata-op",
  });
  await commands.markAttention({
    itemId: "book-1",
    expectedReviewRevision: "review-r1",
    reason: "Caption needs checking",
    actorId: "curator-1",
    comment: "",
    operationId: "attention-op",
  });
  await commands.resolveCorrections({
    itemId: "book-1",
    expectedReviewRevision: "review-r2",
    actorId: "curator-1",
    comment: "Corrected",
    operationId: "resolve-op",
  });
  await commands.reopenCorrections({
    itemId: "book-1",
    expectedReviewRevision: "review-r3",
    actorId: "curator-2",
    comment: "Second look",
    operationId: "reopen-op",
  });

  assert.deepEqual(invocations, [
    ["setManualCaption", {
      itemId: "book-1",
      artifactId: "image-1",
      expectedArtifactRevision: "image-r1",
      text: "Human caption",
      language: "en",
      signal,
      idempotencyKey: "caption-set-op",
    }],
    ["clearManualCaption", {
      itemId: "book-1",
      artifactId: "image-1",
      expectedArtifactRevision: "image-r2",
      idempotencyKey: "caption-clear-op",
    }],
    ["assertArtifactMetadata", {
      itemId: "book-1",
      artifactId: "image-1",
      expectedArtifactRevision: "image-r3",
      assertions: { plate_number: 8 },
      clearNames: [],
      idempotencyKey: "metadata-op",
    }],
    ["markAttention", {
      itemId: "book-1",
      expectedReviewRevision: "review-r1",
      reason: "Caption needs checking",
      comment: "",
      idempotencyKey: "attention-op",
    }],
    ["resolveCorrections", {
      itemId: "book-1",
      expectedReviewRevision: "review-r2",
      comment: "Corrected",
      idempotencyKey: "resolve-op",
    }],
    ["reopenCorrections", {
      itemId: "book-1",
      expectedReviewRevision: "review-r3",
      comment: "Second look",
      idempotencyKey: "reopen-op",
    }],
  ]);
});

test("review reads resolve item context through the dedicated adapter port",
  async () => {
    const invocations = [];
    const signal = new AbortController().signal;
    const { engineClient } = engineHarness({
      corrections: {
        async getReview(payload) {
          invocations.push(payload);
          return {
            item_id: "book-1",
            review: {
              revision: "review-r3",
              state: "resolved",
              reason: "Caption needed checking",
              history_count: 2,
              history_tail: [
                { action: "attention.mark" },
                { action: "attention.resolve" },
              ],
            },
          };
        },
        async listReviewHistory(payload) {
          invocations.push(payload);
          return {
            item_id: "book-1",
            review_revision: "review-r3",
            review_state: "resolved",
            events: [{ action: "attention.resolve" }],
            next_cursor: "next-page",
            total: 2,
          };
        },
      },
    });
    const ports = createCorrectionsEnginePorts(engineClient);

    const review = await ports.reviews.get({
      context: { item_id: "book-1" },
      signal,
    });

    assert.deepEqual(review, {
      itemId: "book-1",
      revision: "review-r3",
      state: "resolved",
      reason: "Caption needed checking",
      history_count: 2,
      history_tail: [
        { action: "attention.mark" },
        { action: "attention.resolve" },
      ],
    });
    assert.equal(Object.isFrozen(review), true);
    const page = await ports.reviews.listHistory({
      context: { item_id: "book-1" },
      reviewRevision: "review-r3",
      cursor: "history-page",
      limit: 25,
      signal,
    });
    assert.deepEqual(page, {
      itemId: "book-1",
      revision: "review-r3",
      state: "resolved",
      events: [{ action: "attention.resolve" }],
      nextCursor: "next-page",
      total: 2,
    });
    assert.equal(Object.isFrozen(page), true);
    assert.equal(Object.isFrozen(page.events), true);
    assert.deepEqual(invocations, [
      { itemId: "book-1", signal },
      {
        itemId: "book-1",
        reviewRevision: "review-r3",
        cursor: "history-page",
        limit: 25,
        signal,
      },
    ]);
  });

test("production books port pins audit paging and trusts the server actor",
  async () => {
    const mark = {
      operation_id: "review-mark-1",
      action: "attention.mark",
      actor_id: "server-user",
      occurred_at: "2026-07-23T12:00:00Z",
      before_state: "clear",
      after_state: "needs_attention",
      reason: "Check the title leaf",
      comment: "",
    };
    const resolve = {
      operation_id: "review-resolve-1",
      action: "attention.resolve",
      actor_id: "server-user",
      occurred_at: "2026-07-23T12:05:00Z",
      before_state: "needs_attention",
      after_state: "resolved",
      reason: mark.reason,
      comment: "Verified",
    };
    const invocations = [];
    let indexRevision = 1;
    const index = (state, revision, event) => {
      const review = {
        revision,
        state,
        reason: mark.reason,
        history_count: event === mark ? 1 : 2,
        latest_event: event,
      };
      return {
        schema: "librarytool.corrections-index/1",
        revision: `index-r${indexRevision}`,
        books: [{
          id: "book-1",
          revision: "book-r1",
          title: "A Herbal",
          import_state: "ready",
          issues: [],
          review: { ...review },
          captures: [],
        }, {
          id: "book-2",
          revision: `book-r${indexRevision}`,
          title: indexRevision === 1 ? "Second book" : "Second book updated",
          import_state: "ready",
          issues: [],
          review: {
            revision: "review-clear-r1",
            state: "clear",
            reason: "",
            history_count: 0,
            latest_event: null,
          },
          captures: [],
        }],
        attention: [{
        key: "attention:book-1",
        target: { kind: "book", item_id: "book-1" },
          review,
        }],
      };
    };
    const { engineClient } = engineHarness({
      corrections: {
        async index(payload) {
          invocations.push(["index", payload]);
          return index(
            indexRevision === 1 ? "needs_attention" : "resolved",
            indexRevision === 1 ? "review-r2" : "review-r3",
            indexRevision === 1 ? mark : resolve,
          );
        },
        async getReview(payload) {
          invocations.push(["getReview", payload]);
          return {
            item_id: "book-1",
            review: {
              revision: "review-r3",
              state: "resolved",
              reason: mark.reason,
              history_count: 2,
              history_tail: [mark, resolve],
            },
          };
        },
        async listReviewHistory(payload) {
          invocations.push(["listReviewHistory", payload]);
          return {
            item_id: "book-1",
            review_revision: "review-r3",
            review_state: "resolved",
            events: payload.cursor ? [resolve] : [mark],
            next_cursor: payload.cursor ? null : "history-page-2",
            total: 2,
          };
        },
        async resolveCorrections(payload) {
          invocations.push(["resolveCorrections", payload]);
          indexRevision = 2;
          return {
            receipt: {
              targets: [{
                kind: "review",
                target_id: "book-1",
                before_revision: "review-r2",
                after_revision: "review-r3",
              }],
            },
          };
        },
      },
    });
    const ports = createCorrectionsEnginePorts(engineClient);
    const signal = new AbortController().signal;
    const target = { kind: "book", item_id: "book-1" };

    await ports.books.loadIndex({ workspaceId: "workspace-1", signal });
    const document = await ports.books.getReview({ target, signal });
    const result = await ports.books.resolveReview({
      target,
      expectedRevision: "review-r2",
      operationId: "resolve-op-1",
      comment: "Verified",
      signal,
    });

    assert.equal(ports.books.trustedActor, true);
    assert.deepEqual(
      document.review.history.map((event) => event.action),
      ["attention.mark", "attention.resolve"],
    );
    assert.equal(result.index_revision, "index-r2");
    assert.equal(result.entry.review.state, "resolved");
    assert.equal(result.index.books[1].title, "Second book updated",
      "the complete converged index retains unrelated concurrent changes");
    assert.deepEqual(invocations, [
      ["index", { workspaceId: "workspace-1", signal }],
      ["getReview", { itemId: "book-1", signal }],
      ["listReviewHistory", {
        itemId: "book-1",
        reviewRevision: "review-r3",
        cursor: null,
        limit: 100,
        signal,
      }],
      ["listReviewHistory", {
        itemId: "book-1",
        reviewRevision: "review-r3",
        cursor: "history-page-2",
        limit: 100,
        signal,
      }],
      ["resolveCorrections", {
        itemId: "book-1",
        expectedReviewRevision: "review-r2",
        idempotencyKey: "resolve-op-1",
        comment: "Verified",
        signal,
      }],
      ["index", { workspaceId: "workspace-1", signal }],
    ]);
  });

test("production books port ignores a late aborted workspace load", async () => {
  const workspaceA = deferred();
  const workspaceB = deferred();
  const indexCalls = [];
  const review = {
    revision: "review-r2",
    state: "resolved",
    reason: "Check the title leaf",
    history_count: 2,
    latest_event: {
      operation_id: "resolve-op",
      action: "attention.resolve",
      actor_id: "server-user",
      occurred_at: "2026-07-23T12:05:00Z",
      before_state: "needs_attention",
      after_state: "resolved",
      reason: "Check the title leaf",
      comment: "Verified",
    },
  };
  const converged = {
    schema: "librarytool.corrections-index/1",
    revision: "index-r2",
    books: [{
      id: "book-1",
      revision: "book-r1",
      title: "A Herbal",
      import_state: "ready",
      issues: [],
      review: { ...review },
      captures: [],
    }],
    attention: [{
      key: "attention:book-1",
      target: { kind: "book", item_id: "book-1" },
      review: { ...review },
    }],
  };
  const { engineClient } = engineHarness({
    corrections: {
      index(payload) {
        indexCalls.push(payload);
        if (indexCalls.length === 1) return workspaceA.promise;
        if (indexCalls.length === 2) return workspaceB.promise;
        return Promise.resolve(converged);
      },
      async getReview() {
        throw new Error("not expected");
      },
      async listReviewHistory() {
        throw new Error("not expected");
      },
      async resolveCorrections() {
        return {
          receipt: {
            targets: [{
              kind: "review",
              target_id: "book-1",
              before_revision: "review-r1",
              after_revision: "review-r2",
            }],
          },
        };
      },
    },
  });
  const books = createCorrectionsEnginePorts(engineClient).books;
  const superseded = new AbortController();
  const openingA = books.loadIndex({
    workspaceId: "workspace-a",
    signal: superseded.signal,
  });
  superseded.abort();
  const openingB = books.loadIndex({ workspaceId: "workspace-b" });

  workspaceB.resolve({});
  await openingB;
  workspaceA.resolve({});
  await openingA;
  const result = await books.resolveReview({
    target: { kind: "book", item_id: "book-1" },
    expectedRevision: "review-r1",
    operationId: "resolve-op",
  });

  assert.equal(result.index_revision, "index-r2");
  assert.equal(indexCalls.length, 3);
  assert.equal(indexCalls[2].workspaceId, "workspace-b",
    "the late aborted load cannot reclaim adapter workspace authority");
});

test("same-workspace refresh does not invalidate mutation convergence",
  async () => {
    const mutation = deferred();
    let indexCalls = 0;
    const review = {
      revision: "review-r2",
      state: "resolved",
      reason: "Check the title leaf",
      history_count: 2,
      latest_event: {
        operation_id: "resolve-op",
        action: "attention.resolve",
        actor_id: "server-user",
        occurred_at: "2026-07-23T12:05:00Z",
        before_state: "needs_attention",
        after_state: "resolved",
        reason: "Check the title leaf",
        comment: "Verified",
      },
    };
    const converged = {
      schema: "librarytool.corrections-index/1",
      revision: "index-r2",
      books: [{
        id: "book-1",
        revision: "book-r1",
        title: "A Herbal",
        import_state: "ready",
        issues: [],
        review: { ...review },
        captures: [],
      }],
      attention: [{
        key: "attention:book-1",
        target: { kind: "book", item_id: "book-1" },
        review: { ...review },
      }],
    };
    const { engineClient } = engineHarness({
      corrections: {
        async index() {
          indexCalls += 1;
          return indexCalls < 3 ? {} : converged;
        },
        async getReview() {
          throw new Error("not expected");
        },
        async listReviewHistory() {
          throw new Error("not expected");
        },
        resolveCorrections: () => mutation.promise,
      },
    });
    const books = createCorrectionsEnginePorts(engineClient).books;
    const target = { kind: "book", item_id: "book-1" };
    await books.loadIndex({ workspaceId: "workspace-a" });

    const mutating = books.resolveReview({
      target,
      expectedRevision: "review-r1",
      operationId: "resolve-op",
    });
    await books.loadIndex({ workspaceId: "workspace-a" });
    mutation.resolve({
      receipt: {
        targets: [{
          kind: "review",
          target_id: "book-1",
          before_revision: "review-r1",
          after_revision: "review-r2",
        }],
      },
    });
    const result = await mutating;

    assert.equal(result.index_revision, "index-r2");
    assert.equal(indexCalls, 3);
  });

test("production books port rejects summary and paged audit drift", async () => {
  const event = {
    operation_id: "review-mark-1",
    action: "attention.mark",
    actor_id: "server-user",
    occurred_at: "2026-07-23T12:00:00Z",
    before_state: "clear",
    after_state: "needs_attention",
    reason: "Check the title leaf",
    comment: "",
  };
  const { engineClient } = engineHarness({
    corrections: {
      async index() {
        return {};
      },
      async getReview() {
        return {
          item_id: "book-1",
          review: {
            revision: "review-r2",
            state: "needs_attention",
            reason: event.reason,
            history_count: 1,
            history_tail: [{ ...event, comment: "different evidence" }],
          },
        };
      },
      async listReviewHistory() {
        return {
          item_id: "book-1",
          review_revision: "review-r2",
          review_state: "needs_attention",
          events: [event],
          next_cursor: null,
          total: 1,
        };
      },
    },
  });
  const books = createCorrectionsEnginePorts(engineClient).books;

  await assert.rejects(
    books.getReview({
      target: { kind: "book", item_id: "book-1" },
    }),
    (error) => error.code === "invalid-corrections-review-history",
  );
});

test("production books port surfaces post-receipt review drift", async () => {
  let indexCalls = 0;
  const review = (revision, state) => ({
    revision,
    state,
    reason: "Check the title leaf",
    history_count: state === "resolved" ? 2 : 3,
    latest_event: {
      operation_id: `operation-${revision}`,
      action: state === "resolved"
        ? "attention.resolve" : "attention.reopen",
      actor_id: "server-user",
      occurred_at: "2026-07-23T12:05:00Z",
      before_state: state === "resolved"
        ? "needs_attention" : "resolved",
      after_state: state,
      reason: "Check the title leaf",
      comment: "",
    },
  });
  const index = (summary) => ({
    schema: "librarytool.corrections-index/1",
    revision: `index-${summary.revision}`,
    books: [{
      id: "book-1",
      revision: "book-r1",
      title: "A Herbal",
      import_state: "ready",
      issues: [],
      review: { ...summary },
      captures: [],
    }],
    attention: [{
      key: "attention:book-1",
      target: { kind: "book", item_id: "book-1" },
      review: { ...summary },
    }],
  });
  const { engineClient } = engineHarness({
    corrections: {
      async index() {
        indexCalls += 1;
        return index(indexCalls === 1
          ? review("review-r2", "needs_attention")
          : review("review-r4", "needs_attention"));
      },
      async getReview() {
        throw new Error("not expected");
      },
      async listReviewHistory() {
        throw new Error("not expected");
      },
      async resolveCorrections() {
        return {
          receipt: {
            targets: [{
              kind: "review",
              target_id: "book-1",
              before_revision: "review-r2",
              after_revision: "review-r3",
            }],
          },
        };
      },
    },
  });
  const books = createCorrectionsEnginePorts(engineClient).books;
  const target = { kind: "book", item_id: "book-1" };
  await books.loadIndex({ workspaceId: "workspace-1" });

  await assert.rejects(
    books.resolveReview({
      target,
      expectedRevision: "review-r2",
      operationId: "resolve-op-1",
    }),
    (error) => error.code === "review_revision_conflict" &&
      error.status === 409,
  );
  assert.equal(indexCalls, 2);
});

test("production books port bounds unique zero-progress history cursors",
  async () => {
    let pageCalls = 0;
    const { engineClient } = engineHarness({
      corrections: {
        async index() {
          return {};
        },
        async getReview() {
          return {
            item_id: "book-1",
            review: {
              revision: "review-r1",
              state: "clear",
              reason: "",
              history_count: 0,
              history_tail: [],
            },
          };
        },
        async listReviewHistory() {
          pageCalls += 1;
          return {
            item_id: "book-1",
            review_revision: "review-r1",
            review_state: "clear",
            events: [],
            next_cursor: `unique-cursor-${pageCalls}`,
            total: 0,
          };
        },
      },
    });
    const books = createCorrectionsEnginePorts(engineClient).books;

    await assert.rejects(
      books.getReview({
        target: { kind: "book", item_id: "book-1" },
      }),
      (error) => error.code === "invalid-corrections-review-history" &&
        /declared total/.test(error.message),
    );
    assert.equal(pageCalls, 1);
  });

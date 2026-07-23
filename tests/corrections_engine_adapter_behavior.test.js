const assert = require("node:assert/strict");
const test = require("node:test");

const {
  createCorrectionsEnginePorts,
  decorateRasterArtifact,
  decorateSpatialAnnotation,
} = require(
  "../tools/whl_explorer/static/corrections/engine-adapter",
);


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
  return { calls, engineClient };
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
      actorId: "curator-1",
      comment: "",
      idempotencyKey: "attention-op",
    }],
    ["resolveCorrections", {
      itemId: "book-1",
      expectedReviewRevision: "review-r2",
      actorId: "curator-1",
      comment: "Corrected",
      idempotencyKey: "resolve-op",
    }],
    ["reopenCorrections", {
      itemId: "book-1",
      expectedReviewRevision: "review-r3",
      actorId: "curator-2",
      comment: "Second look",
      idempotencyKey: "reopen-op",
    }],
  ]);
});
test("engine books port owns review translation and trusted actor identity",
  async () => {
    const calls = [];
    let state = "needs_attention";
    let revision = "review-r1";
    let indexRevision = 1;
    const target = { kind: "book", item_id: "book-1" };
    const summary = () => {
      const action = state === "resolved"
        ? "attention.resolve"
        : revision === "review-r3"
          ? "attention.reopen"
          : "attention.mark";
      const beforeState = action === "attention.mark"
        ? "clear"
        : action === "attention.resolve"
          ? "needs_attention"
          : "resolved";
      return {
        revision,
        state,
        reason: "Check the title leaf",
        history_count: revision === "review-r1" ? 1
          : revision === "review-r2" ? 2 : 3,
        latest_event: {
        operation_id: `${action}:op`,
        action,
        actor_id: "local-desktop",
        occurred_at: "2026-07-23T18:00:00Z",
        before_state: beforeState,
        after_state: state,
        reason: "Check the title leaf",
        comment: "",
        },
      };
    };
    const index = () => ({
      schema: "librarytool.corrections-index/1",
      revision: `index-r${indexRevision}`,
      books: [{
        id: "book-1",
        revision: `book-r${indexRevision}`,
        title: "A Herbal",
        import_state: "ready",
        issues: [],
        review: summary(),
        captures: [],
      }],
      attention: [{
        key: "attention:book-1",
        target,
        review: summary(),
      }],
    });
    const { engineClient } = engineHarness({
      corrections: {
        async index(payload) {
          calls.push(["index", payload]);
          return index();
        },
        async getReview(payload) {
          calls.push(["getReview", payload]);
          return {
            schema: "librarytool.corrections-review/1",
            target,
            review: {
              revision,
              state,
              reason: "Check the title leaf",
              history: [],
            },
          };
        },
        async resolveCorrections(payload) {
          calls.push(["resolveCorrections", payload]);
          state = "resolved";
          revision = "review-r2";
          indexRevision += 1;
          return { receipt: { action: "attention.resolve" } };
        },
        async reopenCorrections(payload) {
          calls.push(["reopenCorrections", payload]);
          state = "needs_attention";
          revision = "review-r3";
          indexRevision += 1;
          return { receipt: { action: "attention.reopen" } };
        },
      },
    });
    const { books } = createCorrectionsEnginePorts(engineClient);
    const signal = new AbortController().signal;

    assert.equal(books.trustedActor, true);
    await books.loadIndex({ workspaceId: "workspace-1", signal });
    await books.getReview({ target, signal });
    const resolved = await books.resolveReview({
      target,
      expectedRevision: "review-r1",
      operationId: "resolve-op",
      comment: "Corrected",
      signal,
    });
    const reopened = await books.reopenReview({
      target,
      expectedRevision: "review-r2",
      operationId: "reopen-op",
      comment: "Second look",
      signal,
    });

    assert.equal(resolved.schema,
      "librarytool.corrections-review-result/1");
    assert.equal(resolved.entry.review.state, "resolved");
    assert.equal(reopened.entry.review.state, "needs_attention");
    assert.deepEqual(calls, [
      ["index", { workspaceId: "workspace-1", signal }],
      ["getReview", { itemId: "book-1", signal }],
      ["resolveCorrections", {
        itemId: "book-1",
        expectedReviewRevision: "review-r1",
        idempotencyKey: "resolve-op",
        comment: "Corrected",
        signal,
      }],
      ["index", { workspaceId: "workspace-1", signal }],
      ["reopenCorrections", {
        itemId: "book-1",
        expectedReviewRevision: "review-r2",
        idempotencyKey: "reopen-op",
        comment: "Second look",
        signal,
      }],
      ["index", { workspaceId: "workspace-1", signal }],
    ]);
    assert.equal(
      JSON.stringify(calls).includes("actorId"),
      false,
    );
  });

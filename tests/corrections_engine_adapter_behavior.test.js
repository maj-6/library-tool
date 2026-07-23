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
  assert.equal(decoratedAnnotation.object_type, "spatial-annotation");
  assert.equal(decoratedAnnotation.annotation_id, "region:1");
  assert.equal(decoratedAnnotation.kind, "spatial-annotation");
  assert.equal(decoratedAnnotation.group, "layout-regions");
  assert.equal(Object.hasOwn(rawRaster, "object_type"), false);
  assert.equal(Object.hasOwn(rawAnnotation, "object_type"), false);
});


test("raster catalog paging skips unrelated engine pages without losing cursors", async () => {
  const { calls, engineClient } = engineHarness({
    rasterArtifacts: {
      async list(args) {
        calls.rasterList.push(args);
        if (!args.cursor) {
          return {
            revision: "raster-inventory-r1",
            artifacts: [raster("processed-1", "processed-image")],
            next_cursor: "page-2",
            total: 2,
          };
        }
        return {
          revision: "raster-inventory-r1",
          artifacts: [raster("capture-1")],
          next_cursor: null,
          total: 2,
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
  assert.deepEqual(calls.rasterList.map((call) => call.cursor), [null, "page-2"]);
  assert.equal(calls.rasterList[0].itemId, "book-1");
  assert.equal(calls.rasterList[0].representationId, "scan-1");
  assert.equal(calls.rasterList[0].canvasId, "page-1");

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
  const second = annotation("region-2");
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
  const region = annotation("figure-region", {
    linked_artifact_ids: ["figure:1"],
  });
  const { calls, engineClient } = engineHarness({
    rasterArtifacts: {
      async get(args) {
        calls.rasterGet.push(args);
        return { artifact: figure };
      },
    },
    spatialAnnotations: {
      async list(args) {
        calls.spatialList.push(args);
        return {
          revision: "spatial-inventory-r1",
          annotations: [region],
          next_cursor: null,
          total: 1,
        };
      },
    },
  });
  const ports = createCorrectionsEnginePorts(engineClient);
  const detail = await ports.artifacts.catalog.get({
    context: { item_id: "book-1", canvas_id: "ignored-context-canvas" },
    key: "artifact:figure:1",
  });

  assert.equal(detail.group, "extracted-figures");
  assert.equal(detail.extensions.corrections_ui.paged_regions, true);
  assert.equal(calls.spatialList.length, 0,
    "the editor loads annotations through its bounded paging port");

  const regions = await ports.artifacts.resources.listRegions({
    context: { itemId: "book-1", representationId: "scan-1" },
    canvasId: "page-1",
    cursor: null,
    limit: 200,
  });
  assert.equal(regions.items[0].annotation_id, "figure-region");
  assert.equal(calls.spatialList[0].representationId, "scan-1");
  assert.equal(calls.spatialList[0].canvasId, "page-1");

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

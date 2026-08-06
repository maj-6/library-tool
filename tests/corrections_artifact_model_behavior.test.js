const assert = require("node:assert/strict");
const test = require("node:test");

const {
  ARTIFACT_GROUPS,
  buildArtifactTreeRows,
  buildLinkIndex,
  decodeArtifactDetail,
  decodeArtifactPage,
  decodeArtifactSummary,
  virtualArtifactWindow,
} = require("../tools/whl_explorer/static/corrections/artifact-model");


function artifact(overrides = {}) {
  return {
    key: { item_id: "book-1", artifact_id: "artifact-1" },
    revision: "artifact-r1",
    kind: "generated-image",
    label: "Generated plate",
    media_type: "image/png",
    resource_state: "available",
    resource: {
      resource_id: "resource-1",
      revision: "resource-r1",
      variant: "display",
    },
    freshness: "current",
    provenance: { origin: "generated", provider_id: "mistral", model: "pixtral" },
    ...overrides,
  };
}


test("artifact decoder covers every Corrections group without provider-specific types", () => {
  const cases = [
    ["metadata", "generated-metadata", "metadata"],
    ["ocr", "ocr-text", "text"],
    ["mistral-box", "layout-regions", "regions"],
    ["captured-image", "source-images", "image"],
    ["illustration", "extracted-figures", "image"],
    ["corrected-image", "processed-images", "image"],
    ["correction-transform", "transforms", "unknown"],
    ["reworked-image", "generated-images", "image"],
    ["alien-output", "unknown", "unknown"],
  ];

  for (const [kind, group, family] of cases) {
    const mediaType = family === "image" ? "image/png"
      : family === "text" ? "text/plain"
        : family === "metadata" ? "application/json" : "application/octet-stream";
    const spatial = group === "layout-regions";
    const decoded = decodeArtifactSummary(artifact({
      key: spatial
        ? { item_id: "book-1", annotation_id: `annotation-${kind}` }
        : { item_id: "book-1", artifact_id: `artifact-${kind}` },
      kind,
      media_type: mediaType,
      resource_state: family === "image" ? "available" : "unavailable",
    }));
    assert.equal(decoded.group, group, kind);
    assert.equal(decoded.family, family, kind);
  }

  assert.deepEqual(ARTIFACT_GROUPS.map((group) => group.id), [
    "generated-metadata",
    "ocr-text",
    "layout-regions",
    "source-images",
    "extracted-figures",
    "processed-images",
    "transforms",
    "generated-images",
    "unknown",
  ]);
});


test("raster and spatial contracts normalize to bounded summaries and opaque refs", () => {
  const raster = decodeArtifactDetail(artifact({
    category_assignments: [
      { category: "cover", origin: "manual", revision: "category-r1" },
      { category: "other", origin: "suggested", revision: "category-r0" },
    ],
    effective_category: "cover",
    caption_assertions: [
      { text: "Human caption", origin: "manual", revision: "caption-r2" },
      { text: "Machine caption", origin: "machine", revision: "caption-r1",
        confidence: 0.91 },
    ],
    metadata_assertions: [
      {
        name: "plate_number",
        value: 3,
        origin: "machine",
        revision: "metadata-r1",
        provenance: { origin: "machine", provider_id: "mistral" },
      },
      {
        name: "plate_number",
        value: 4,
        origin: "manual",
        revision: "metadata-r2",
        provenance: { origin: "manual" },
      },
    ],
    effective_metadata: { plate_number: 4 },
    lineage: [{
      artifact_id: "source-1",
      artifact_revision: "source-r1",
      relation: "derived_from",
    }],
    source: {
      representation_id: "scan-1",
      representation_revision: "scan-r1",
      canvas_id: "page-1",
      canvas_revision: "page-r1",
    },
    dimensions: { width: 1200, height: 800, orientation: 1 },
    correction: {
      artifact_revision: "artifact-r1",
      source_revision: "scan-r1",
      source_sha256: "A".repeat(64),
      proposal: {
        schema: "org.whl.page-boundary-proposal",
        version: 1,
        source_revision: "scan-r1",
        quad: [[0.1, 0.1], [0.9, 0.1], [0.9, 0.9], [0.1, 0.9]],
      },
    },
  }));
  assert.equal(raster.key, "artifact:artifact-1");
  assert.deepEqual(raster.resourceRef, {
    id: "resource-1",
    revision: "resource-r1",
    variant: "display",
  });
  assert.equal(Object.hasOwn(raster.resourceRef, "path"), false);
  assert.equal(Object.hasOwn(raster.resourceRef, "url"), false);
  assert.equal(raster.effectiveCaption.text, "Human caption");
  assert.equal(raster.effectiveCategory, "cover");
  assert.equal(raster.metadataAssertions.length, 2);
  assert.deepEqual(
    raster.metadataAssertions.map((assertion) => [
      assertion.name,
      assertion.origin,
      assertion.value,
    ]),
    [
      ["plate_number", "machine", 3],
      ["plate_number", "manual", 4],
    ],
  );
  assert.deepEqual(raster.metadata, { plate_number: 4 });
  assert.deepEqual(raster.linkedKeys, ["artifact:source-1"]);
  assert.deepEqual(raster.dimensions, { height: 800, orientation: 1, width: 1200 });
  assert.deepEqual(raster.correction, {
    item_id: "book-1",
    artifact_id: "artifact-1",
    artifact_revision: "artifact-r1",
    source_revision: "scan-r1",
    source_sha256: "a".repeat(64),
    proposal: {
      quad: [[0.1, 0.1], [0.9, 0.1], [0.9, 0.9], [0.1, 0.9]],
      schema: "org.whl.page-boundary-proposal",
      source_revision: "scan-r1",
      version: 1,
    },
  });

  const spatial = decodeArtifactDetail({
    key: { item_id: "book-1", annotation_id: "region-1" },
    revision: "region-r1",
    kind: "spatial-annotation",
    label: "Marginal note",
    freshness: "stale",
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
      points: [{ x: 0.1, y: 0.2 }, { x: 0.4, y: 0.2 }, { x: 0.4, y: 0.5 }],
    },
    role_assignments: [
      { role: "marginalia", origin: "manual", revision: "role-r1" },
    ],
    effective_role: "marginalia",
    linked_artifact_ids: ["artifact-1"],
    provenance: { origin: "ocr", provider_id: "mistral" },
  });
  assert.equal(spatial.key, "annotation:region-1");
  assert.equal(spatial.group, "layout-regions");
  assert.equal(spatial.effectiveRole, "marginalia");
  assert.equal(spatial.freshness, "stale");
  assert.deepEqual(spatial.linkedKeys, ["artifact:artifact-1"]);
  assert.equal(spatial.selector.points.length, 3);
});


test("artifact decoder preserves deeply nested effective metadata at transport limits", () => {
  let nested = "herbarium leaf";
  for (let depth = 0; depth < 8; depth += 1) {
    nested = { next: nested };
  }
  const longText = "x".repeat(8192);
  const samples = Array.from({ length: 510 }, () => null);
  const decoded = decodeArtifactSummary(artifact({
    metadata_assertions: [
      {
        name: "description",
        value: nested,
        origin: "machine",
        revision: "metadata-r1",
      },
      {
        name: "transcript",
        value: longText,
        origin: "manual",
        revision: "metadata-r2",
      },
    ],
    effective_metadata: {
      description: nested,
      transcript: longText,
    },
  }));

  assert.deepEqual(decoded.metadata.description, nested);
  assert.equal(decoded.metadata.transcript, longText);
  assert.deepEqual(decoded.metadataAssertions[0].value, nested);
  assert.equal(decoded.metadataAssertions[1].value, longText);
  assert.equal(Object.isFrozen(decoded.metadata.description.next), true);

  const nodeBoundary = decodeArtifactSummary(artifact({
    metadata_assertions: [{
      name: "samples",
      value: samples,
      origin: "manual",
      revision: "metadata-r3",
    }],
    effective_metadata: { samples },
  }));
  assert.equal(nodeBoundary.metadata.samples.length, 510,
    "the 512-node public metadata limit remains usable");

  const aggregateMetadata = Object.fromEntries(
    Array.from({ length: 5 }, (_value, index) =>
      [`segment_${index}`, "y".repeat(7000)]),
  );
  assert.throws(
    () => decodeArtifactSummary(artifact({
      metadata_assertions: Object.entries(aggregateMetadata).map(([name, value]) => ({
        name,
        value,
        origin: "manual",
        revision: `metadata-aggregate-r${name.at(-1)}`,
      })),
      effective_metadata: aggregateMetadata,
    })),
    /effective metadata exceeds its encoded size budget/,
    "effective metadata enforces the engine's aggregate extension budget",
  );
});


test("artifact decoder rejects invalid metadata instead of substituting fallbacks", () => {
  const tooDeep = {};
  let cursor = tooDeep;
  for (let depth = 0; depth < 13; depth += 1) {
    cursor.next = {};
    cursor = cursor.next;
  }

  const invalidEffectiveMetadata = [
    tooDeep,
    { value: "x".repeat(8193) },
    { values: Array.from({ length: 511 }, () => null) },
    null,
  ];
  for (const effectiveMetadata of invalidEffectiveMetadata) {
    assert.throws(
      () => decodeArtifactSummary(artifact({
        effective_metadata: effectiveMetadata,
      })),
      TypeError,
    );
  }

  assert.throws(
    () => decodeArtifactSummary(artifact({
      metadata_assertions: [{
        name: "confidence",
        value: Number.POSITIVE_INFINITY,
        origin: "machine",
        revision: "metadata-r1",
      }],
      effective_metadata: {},
    })),
    /non-portable number/,
    "invalid assertion values must not silently become null",
  );
  assert.throws(
    () => decodeArtifactSummary(artifact({
      metadata_assertions: [{
        name: "segments",
        value: { chunks: Array.from({ length: 5 }, () => "z".repeat(7000)) },
        origin: "machine",
        revision: "metadata-r2",
      }],
      effective_metadata: {},
    })),
    /encoded size budget/,
    "each assertion retains the engine's individual 32 KiB encoded budget",
  );
  assert.throws(
    () => decodeArtifactSummary(artifact({
      metadata_assertions: [{
        name: "samples",
        value: Array.from({ length: 511 }, () => null),
        origin: "machine",
        revision: "metadata-r3",
      }],
      effective_metadata: {},
    })),
    /too many values/,
    "the assertion-name wrapper counts toward the engine's node budget",
  );
  assert.throws(
    () => decodeArtifactSummary(artifact({
      metadata_assertions: [{
        name: "local-path",
        value: "C:/private/scan.png",
        origin: "machine",
        revision: "metadata-r4",
      }],
      effective_metadata: {},
    })),
    /invalid field/,
    "assertion names cannot smuggle private resource metadata",
  );
});


test("page decoder is bounded and generic unknown artifacts stay inspectable", () => {
  const unknown = decodeArtifactPage({
    revision: "inventory-r1",
    items: [artifact({
      key: { item_id: "book-1", artifact_id: "unknown-1" },
      kind: "future-artifact-kind",
      media_type: "application/octet-stream",
      resource_state: "unavailable",
      provenance: { future: { nested: "safe" } },
    })],
    nextCursor: "cursor-2",
  });
  assert.equal(unknown.items[0].group, "unknown");
  assert.equal(unknown.items[0].family, "unknown");
  assert.equal(unknown.nextCursor, "cursor-2");
  assert.deepEqual(unknown.items[0].provenance, { future: { nested: "safe" } });

  assert.throws(() => decodeArtifactPage({
    items: Array.from({ length: 513 }, (_value, index) => artifact({
      key: { item_id: "book-1", artifact_id: `artifact-${index}` },
    })),
  }), /page is too large/);

  const excessive = {};
  let cursor = excessive;
  for (let depth = 0; depth < 14; depth += 1) {
    cursor.next = {};
    cursor = cursor.next;
  }
  const bounded = decodeArtifactSummary(artifact({ provenance: excessive }));
  assert.deepEqual(bounded.provenance, {},
    "oversized optional provenance degrades safely instead of entering UI state");
});


test("link index is symmetric and tree rows expose lazy paging states", () => {
  const figure = decodeArtifactSummary(artifact({
    key: { item_id: "book-1", artifact_id: "figure-1" },
    kind: "figure",
    linked_keys: ["annotation:region-1"],
  }));
  const region = decodeArtifactSummary({
    key: { item_id: "book-1", annotation_id: "region-1" },
    revision: "region-r1",
    kind: "spatial-annotation",
    linked_artifact_ids: ["figure-1"],
  });
  const links = buildLinkIndex([figure, region]);
  assert.deepEqual(links.get("artifact:figure-1"), ["annotation:region-1"]);
  assert.deepEqual(links.get("annotation:region-1"), ["artifact:figure-1"]);

  const states = new Map([
    ["extracted-figures", {
      items: [figure],
      loaded: true,
      loading: false,
      nextCursor: "next-page",
      error: null,
    }],
    ["ocr-text", {
      items: [],
      loaded: true,
      loading: false,
      nextCursor: null,
      error: null,
    }],
  ]);
  const rows = buildArtifactTreeRows(states,
    new Set(["extracted-figures", "ocr-text"]));
  assert.ok(rows.some((row) => row.key === "artifact:figure-1"));
  assert.ok(rows.some((row) => row.key === "more:extracted-figures"));
  assert.ok(rows.some((row) => row.key === "status:ocr-text:empty"));
});


test("virtual tree window stays bounded and always mounts the active descendant", () => {
  const rows = Array.from({ length: 10_000 }, (_value, index) => ({
    key: `artifact:item-${index}`,
  }));
  const windowed = virtualArtifactWindow(rows, {
    rowHeight: 28,
    viewportHeight: 280,
    scrollTop: 0,
    overscan: 4,
    activeKey: "artifact:item-9000",
  });
  assert.ok(windowed.rows.length <= 18);
  assert.ok(windowed.rows.some((row) => row.key === "artifact:item-9000"));
  assert.equal(windowed.totalHeight, 280_000);
  assert.ok(windowed.paddingTop > 0);
});

test("archived artifacts hide behind a per-group reveal row", () => {
  const active = { key: "artifact:keep-1", label: "Front cover" };
  const archived = {
    key: "artifact:price-1",
    label: "Price marking",
    metadataAssertions: [
      { name: "archived", value: true, origin: "manual", revision: "m-r1" },
    ],
  };
  const states = new Map([
    ["source-images", {
      items: [active, archived],
      loaded: true,
      loading: false,
      nextCursor: null,
      error: null,
    }],
  ]);
  const hidden = buildArtifactTreeRows(states, new Set(["source-images"]));
  assert.ok(hidden.some((row) => row.key === "artifact:keep-1"));
  assert.ok(!hidden.some((row) => row.key === "artifact:price-1"),
    "archived rows stay out of the working set by default");
  const toggle = hidden.find((row) => row.key === "archived:source-images");
  assert.ok(toggle);
  assert.equal(toggle.type, "archived");
  assert.match(toggle.label, /Archived \(1\)/);

  const revealed = buildArtifactTreeRows(
    states, new Set(["source-images"]), new Set(["source-images"]));
  const archivedRow = revealed.find((row) => row.key === "artifact:price-1");
  assert.ok(archivedRow, "revealing shows the archived artifact again");
  assert.equal(archivedRow.archived, true);
  assert.match(
    revealed.find((row) => row.key === "archived:source-images").label,
    /Hide archived \(1\)/,
  );
  // A cleared or false assertion keeps the artifact in the working set.
  const restored = buildArtifactTreeRows(
    new Map([["source-images", {
      items: [{
        ...archived,
        metadataAssertions: [
          { name: "archived", value: false, origin: "manual", revision: "m-r2" },
        ],
      }],
      loaded: true,
      loading: false,
      nextCursor: null,
      error: null,
    }]]),
    new Set(["source-images"]),
  );
  assert.ok(restored.some((row) => row.key === "artifact:price-1"));
  assert.ok(!restored.some((row) => row.key === "archived:source-images"));
});

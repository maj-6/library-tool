const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const clientPath = path.join(
  __dirname, "..", "tools", "whl_explorer", "static", "engine-client.js");
const appPath = path.join(
  __dirname, "..", "tools", "whl_explorer", "static", "app.js");
const templatePath = path.join(
  __dirname, "..", "tools", "whl_explorer", "templates", "index.html");
const clientSource = fs.readFileSync(clientPath, "utf8");
const { EngineClient, EngineClientError } = require(clientPath);

function response(status, body) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  };
}

function harness(body = { ok: true }) {
  const calls = [];
  const client = new EngineClient({
    transport: async (url, init) => {
      calls.push({ url, init });
      return response(200, body);
    },
  });
  return { client, calls };
}

function lifecycleState(overrides = {}) {
  return {
    ok: true,
    schema: "librarytool.item-lifecycle-state/1",
    state: "live",
    item_id: "book:one",
    item_revision: "item-r1",
    managed_tree_revision: "tree-r1",
    revision: "lifecycle-r1",
    ...overrides,
  };
}

function secretStatus(overrides = {}) {
  const configured = overrides.configured === true;
  return {
    id: "provider:ai:api-key",
    configured,
    masked_hint: configured ? "••••" : "",
    revision: "secret-r1",
    ...overrides,
  };
}

function secretMutationResult(action, overrides = {}) {
  const replace = action === "replace";
  const before = secretStatus({
    configured: !replace,
    masked_hint: replace ? "" : "••••",
    revision: replace ? "secret-r1" : "secret-r2",
  });
  const after = secretStatus({
    configured: replace,
    masked_hint: replace ? "••••" : "",
    revision: replace ? "secret-r2" : "secret-r3",
  });
  return {
    ok: true,
    schema: "librarytool.secret-mutation-receipt/1",
    replayed: false,
    receipt: {
      action,
      operation_id: `${action}-ai-1`,
      secret_id: before.id,
      before,
      after,
    },
    ...overrides,
  };
}

function itemTombstone(overrides = {}) {
  return {
    tombstone_id: "deleted:one",
    revision: "tomb-r1",
    state: "deleted",
    item_id: "book:one",
    deleted_item_revision: "item-r1",
    managed_tree_revision: "tree-r1",
    restored_item_revision: "",
    ...overrides,
  };
}

function lifecycleResult(action, options = {}) {
  const restore = action === "restore";
  const tombstone = itemTombstone(restore ? {
    revision: "tomb-r2",
    state: "restored",
    restored_item_revision: "item-r2",
  } : {});
  Object.assign(tombstone, options.tombstone || {});
  const receipt = {
    action,
    operation_id: restore ? "restore:one" : "delete:one",
    item_id: "book:one",
    deleted_item_revision: "item-r1",
    restored_item_revision: restore ? "item-r2" : "",
    managed_tree_revision: "tree-r1",
    tombstone_before_revision: restore ? "tomb-r1" : "",
    tombstone,
    ...(options.receipt || {}),
  };
  return {
    ok: true,
    schema: "librarytool.item-lifecycle-receipt/1",
    replayed: false,
    receipt,
    ...(options.envelope || {}),
  };
}

function copyJson(value) {
  return JSON.parse(JSON.stringify(value));
}

function artifactProvenance(overrides = {}) {
  return {
    origin: "capture",
    provider_id: "",
    model: "",
    recipe_revision: "",
    operation_id: "",
    generated_at: "",
    extensions: {},
    ...overrides,
  };
}

function rasterArtifact(overrides = {}) {
  return {
    key: { item_id: "book:one", artifact_id: "image:one" },
    revision: "artifact-r1",
    kind: "capture",
    label: "Cover capture",
    media_type: "image/png",
    content_sha256: "a".repeat(64),
    dimensions: { width: 1200, height: 1600, orientation: 1 },
    source: {
      representation_id: "capture:one",
      representation_revision: "capture-r1",
      canvas_id: "page:one",
      canvas_revision: "canvas-r1",
    },
    resource_state: "available",
    resource: {
      id: "capture:image:one",
      revision: "resource-r1",
      variant: "display",
    },
    freshness: "current",
    lineage: [],
    category_assignments: [],
    effective_category: "other",
    caption_assertions: [],
    effective_caption: null,
    provenance: artifactProvenance(),
    extensions: {},
    ...overrides,
  };
}

function spatialAnnotation(overrides = {}) {
  return {
    key: { item_id: "book:one", annotation_id: "region:one" },
    revision: "annotation-r1",
    source: {
      representation_id: "capture:one",
      representation_revision: "capture-r1",
      canvas_id: "page:one",
      canvas_revision: "canvas-r1",
    },
    selector: {
      type: "polygon",
      coordinate_space: "canvas-normalized",
      coordinate_space_revision: "canvas-r1",
      points: [
        { x: 0.1, y: 0.1 },
        { x: 0.9, y: 0.1 },
        { x: 0.9, y: 0.9 },
        { x: 0.1, y: 0.9 },
      ],
    },
    order: 0,
    label: "Illustration",
    freshness: "current",
    role_assignments: [],
    effective_role: "",
    caption_assertions: [],
    linked_artifact_ids: ["image:one"],
    provenance: artifactProvenance({ origin: "mistral" }),
    extensions: {},
    ...overrides,
  };
}

test("EngineClient exposes the complete Replica compatibility surface", () => {
  const { client } = harness();
  assert.equal(typeof client.capabilities, "function");
  assert.equal(typeof client.ocr.layout, "function");
  assert.equal(typeof client.jobs.list, "function");
  assert.equal(typeof client.jobs.get, "function");
  assert.equal(typeof client.jobs.cancel, "function");
  assert.equal(typeof client.jobs.events, "function");
  assert.equal(typeof client.items.list, "function");
  assert.equal(typeof client.items.get, "function");
  assert.equal(typeof client.items.create, "function");
  assert.equal(typeof client.items.update, "function");
  assert.equal(typeof client.items.seedCompatibility, "function");
  assert.equal(typeof client.items.lifecycle, "function");
  assert.equal(typeof client.items.delete, "function");
  assert.equal(typeof client.items.representations, "function");
  assert.equal(typeof client.items.attachRepresentation, "function");
  assert.equal(typeof client.items.replaceRepresentation, "function");
  assert.equal(typeof client.items.detachRepresentation, "function");
  assert.equal(typeof client.items.artifacts, "function");
  assert.equal(typeof client.items.readiness, "function");
  assert.equal(typeof client.rasterArtifacts.list, "function");
  assert.equal(typeof client.rasterArtifacts.get, "function");
  assert.equal(typeof client.rasterArtifacts.resourceUrl, "function");
  assert.equal(typeof client.spatialAnnotations.list, "function");
  assert.equal(typeof client.spatialAnnotations.get, "function");
  assert.equal(typeof client.itemTombstones.list, "function");
  assert.equal(typeof client.itemTombstones.get, "function");
  assert.equal(typeof client.itemTombstones.restore, "function");
  assert.equal(typeof client.secrets.list, "function");
  assert.equal(typeof client.secrets.get, "function");
  assert.equal(typeof client.secrets.replace, "function");
  assert.equal(typeof client.secrets.clear, "function");
  const jsonMethods = [
    client.replica.templates.list,
    client.pdf.info,
    client.replica.pages.get,
    client.replica.detection.start,
    client.pdf.words,
    client.replica.figures.rework,
    client.replica.proposals.decide,
    client.replica.pages.save,
    client.replica.pages.recompile,
    client.replica.templates.saveFromPage,
    client.replica.templates.apply,
    client.replica.templates.outliers,
    client.replica.styles.get,
    client.translations.list,
    client.translations.get,
    client.translations.replacePage,
    client.replica.styles.save,
    client.replica.instructions.get,
    client.replica.instructions.save,
    client.replica.styles.reset,
    client.replica.packages.open,
    client.replica.packages.import,
  ];
  const urlBuilders = [
    client.pdf.pageImageUrl,
    client.replica.pages.imageUrl,
    client.replica.figures.imageUrl,
    client.replica.packages.exportUrl,
    client.replica.printUrl,
  ];
  assert.equal(jsonMethods.length, 22);
  assert.ok(jsonMethods.every((method) => typeof method === "function"));
  assert.equal(urlBuilders.length, 5);
  assert.ok(urlBuilders.every((method) => typeof method === "function"));
});

test("EngineClient exposes versioned capability discovery", async () => {
  const { client, calls } = harness({
    ok: true, schema: "librarytool.capabilities/1",
  });
  const result = await client.capabilities();
  assert.equal(result.schema, "librarytool.capabilities/1");
  assert.equal(calls[0].url, "/api/v1/capabilities");
  assert.equal(calls[0].init.method, "GET");
});

test("EngineClient validates versioned Corrections artifact reads", async () => {
  const raster = rasterArtifact();
  const annotation = spatialAnnotation();
  const bodies = [
    {
      ok: true,
      schema: "librarytool.raster-artifacts/1",
      item_id: "book:one",
      revision: "rac-r1",
      artifacts: [raster],
      next_cursor: "next-page",
      total: 2,
    },
    {
      ok: true,
      schema: "librarytool.raster-artifact/1",
      artifact: raster,
    },
    {
      ok: true,
      schema: "librarytool.spatial-annotations/1",
      item_id: "book:one",
      revision: "sac-r1",
      annotations: [annotation],
      next_cursor: null,
      total: 1,
    },
    {
      ok: true,
      schema: "librarytool.spatial-annotation/1",
      annotation,
    },
  ];
  const calls = [];
  const client = new EngineClient({
    transport: async (url, init) => {
      calls.push({ url, init });
      return response(200, bodies.shift());
    },
  });

  assert.equal((await client.rasterArtifacts.list({
    itemId: "book:one",
    representationId: "capture:one",
    canvasId: "page:one",
    limit: 1,
  })).artifacts[0].key.artifact_id, "image:one");
  assert.equal((await client.rasterArtifacts.get({
    itemId: "book:one",
    artifactId: "image:one",
  })).artifact.revision, "artifact-r1");
  assert.equal((await client.spatialAnnotations.list({
    itemId: "book:one",
    representationId: "capture:one",
    canvasId: "page:one",
    limit: 200,
  })).annotations[0].key.annotation_id, "region:one");
  assert.equal((await client.spatialAnnotations.get({
    itemId: "book:one",
    annotationId: "region:one",
  })).annotation.revision, "annotation-r1");
  assert.deepEqual(calls.map(({ url }) => url), [
    "/api/v1/items/book%3Aone/raster-artifacts" +
      "?representation_id=capture%3Aone&canvas_id=page%3Aone&limit=1",
    "/api/v1/items/book%3Aone/raster-artifacts/image%3Aone",
    "/api/v1/items/book%3Aone/spatial-annotations" +
      "?representation_id=capture%3Aone&canvas_id=page%3Aone&limit=200",
    "/api/v1/items/book%3Aone/spatial-annotations/region%3Aone",
  ]);
  assert.equal(client.rasterArtifacts.resourceUrl({
    itemId: "book:one",
    artifactId: "image:one",
    revision: "resource-r1",
  }), "/api/v1/items/book%3Aone/raster-artifacts/image%3Aone/resource" +
    "?revision=resource-r1");
});

test("EngineClient rejects malformed or path-leaking Corrections views", async () => {
  const malformed = rasterArtifact({
    extensions: { localPath: "C:/private/scan.png" },
  });
  const client = new EngineClient({
    transport: async () => response(200, {
      ok: true,
      schema: "librarytool.raster-artifacts/1",
      item_id: "book:one",
      revision: "rac-r1",
      artifacts: [malformed],
      next_cursor: null,
      total: 1,
    }),
  });
  await assert.rejects(
    client.rasterArtifacts.list({ itemId: "book:one" }),
    (error) => error instanceof EngineClientError &&
      error.code === "invalid-response",
  );

  assert.throws(() => client.rasterArtifacts.resourceUrl({
    itemId: "book:one",
    artifactId: "image:one",
    revision: "bad revision",
  }), TypeError);
  assert.throws(() => client.spatialAnnotations.list({
    itemId: "book:one",
    limit: 513,
  }), TypeError);
});

test("secret reads expose only versioned masked status", async () => {
  const calls = [];
  const bodies = [
    {
      ok: true,
      schema: "librarytool.secret-status-list/1",
      health: { available: true, state: "ready", writable: true },
      secrets: [secretStatus()],
    },
    {
      ok: true,
      schema: "librarytool.secret-status/1",
      status: secretStatus({
        configured: true,
        masked_hint: "••••",
        revision: "secret-r2",
      }),
    },
  ];
  const client = new EngineClient({
    transport: async (url, init) => {
      calls.push({ url, init });
      return response(200, bodies.shift());
    },
  });

  const listed = await client.secrets.list();
  const detail = await client.secrets.get({
    secretId: "provider:ai:api-key",
  });

  assert.equal(listed.secrets[0].configured, false);
  assert.equal(detail.status.masked_hint, "••••");
  assert.deepEqual(calls.map(({ url }) => url), [
    "/api/v1/secrets",
    "/api/v1/secrets/provider%3Aai%3Aapi-key",
  ]);
  assert.ok(calls.every(({ init }) => init.method === "GET"));
  assert.ok(calls.every(({ init }) => init.cache === "no-store"));
  assert.ok(calls.every(({ init }) => init.body === undefined));
});

test("secret mutations own exact CAS and idempotency transport", async () => {
  const calls = [];
  const bodies = [
    secretMutationResult("replace"),
    secretMutationResult("clear"),
  ];
  const client = new EngineClient({
    transport: async (url, init) => {
      calls.push({ url, init });
      return response(200, bodies.shift());
    },
  });

  const replacement = await client.secrets.replace({
    secretId: "provider:ai:api-key",
    revision: "secret-r1",
    credential: "transport-only-value",
    idempotencyKey: "replace-ai-1",
  });
  const cleared = await client.secrets.clear({
    secretId: "provider:ai:api-key",
    revision: "secret-r2",
    idempotencyKey: "clear-ai-1",
  });

  assert.equal(replacement.receipt.after.configured, true);
  assert.equal(cleared.receipt.after.configured, false);
  assert.deepEqual(calls[0], {
    url: "/api/v1/secrets/provider%3Aai%3Aapi-key",
    init: {
      method: "PUT",
      headers: {
        Accept: "application/json",
        "Idempotency-Key": "replace-ai-1",
        "If-Match": '"secret-r1"',
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ credential: "transport-only-value" }),
      cache: "no-store",
    },
  });
  assert.deepEqual(calls[1], {
    url: "/api/v1/secrets/provider%3Aai%3Aapi-key",
    init: {
      method: "DELETE",
      headers: {
        Accept: "application/json",
        "Idempotency-Key": "clear-ai-1",
        "If-Match": '"secret-r2"',
      },
      cache: "no-store",
    },
  });
  assert.equal(JSON.stringify(replacement).includes("transport-only-value"), false);
});

test("secret client rejects disclosure-shaped responses without retaining them",
  async () => {
    const leaked = {
      ok: true,
      schema: "librarytool.secret-status-list/1",
      health: { available: true, state: "ready", writable: true },
      secrets: [secretStatus()],
      credential: "must-not-survive-validation",
    };
    const { client } = harness(leaked);

    await assert.rejects(
      client.secrets.list(),
      (error) => error instanceof EngineClientError &&
        error.code === "invalid-response" && error.body === null &&
        !JSON.stringify(error).includes("must-not-survive-validation"),
    );
  });

test("secret mutations reject unsafe commands before transport", async () => {
  const { client, calls } = harness();
  await assert.rejects(client.secrets.replace({
    secretId: "provider:ai:api-key",
    revision: "secret-r1",
    credential: "",
    idempotencyKey: "replace-ai-1",
  }), /credential/);
  await assert.rejects(client.secrets.replace({
    secretId: "provider/ai/key",
    revision: "secret-r1",
    credential: "value",
    idempotencyKey: "replace-ai-1",
  }), /secretId/);
  await assert.rejects(client.secrets.clear({
    secretId: "provider:ai:api-key",
    revision: 'W/"secret-r1"',
    idempotencyKey: "clear-ai-1",
  }), /revision/);
  await assert.rejects(client.secrets.clear({
    secretId: "provider:ai:api-key",
    revision: "secret-r2",
    idempotencyKey: "unsafe/key",
  }), /idempotencyKey/);
  assert.equal(calls.length, 0);
});

test("secret ids follow the exact engine namespace contract", async () => {
  const valid = ["a".repeat(51), "b".repeat(50), "c".repeat(50),
    "d".repeat(50), "e".repeat(50)].join(":");
  assert.equal(valid.length, 255);
  assert.equal(valid.split(":").length, 5);
  const { client, calls } = harness({
    ok: true,
    schema: "librarytool.secret-status/1",
    status: secretStatus({ id: valid }),
  });
  await client.secrets.get({ secretId: valid });
  assert.equal(calls.length, 1);

  const invalid = ["a".repeat(52), "b".repeat(50), "c".repeat(50),
    "d".repeat(50), "e".repeat(50)].join(":");
  assert.equal(invalid.length, 256);
  for (const secretId of [
    invalid,
    "Provider:ai:api-key",
    `${"a".repeat(64)}:key`,
    "provider",
    "provider::key",
    "provider:key\n",
    "provider:key\u2028",
  ]) {
    await assert.rejects(client.secrets.get({ secretId }), /secretId/);
  }
  assert.equal(calls.length, 1);
});

test("legacy renderer import is explicit and credential-safe on the wire",
  async () => {
    const { client, calls } = harness(secretMutationResult("replace"));
    const result = await client.secrets.replace({
      secretId: "provider:ai:api-key",
      revision: "secret-r1",
      credential: "legacy-cache-value",
      idempotencyKey: "replace-ai-1",
      legacyLocalImport: true,
    });

    assert.equal(calls[0].init.headers["X-WHL-Secret-Source"],
      "legacy-renderer-local-storage-v1");
    assert.equal(calls[0].init.body,
      JSON.stringify({ credential: "legacy-cache-value" }));
    assert.equal(JSON.stringify(result).includes("legacy-cache-value"), false);
  });

test("shared OCR layout metadata also crosses the client boundary", async () => {
  const { client, calls } = harness({ ok: true, region_pages: {} });
  await client.ocr.layout({ bookId: "book / one" });
  assert.equal(calls[0].url, "/api/builds/book%20%2F%20one/ocr-layout");
  assert.equal(calls[0].init.method, "GET");
});

test("background jobs use the versioned engine transport", async () => {
  const { client, calls } = harness({ ok: true, jobs: [] });
  await client.jobs.list({
    state: ["running", "cancelling"], kind: "ocr", itemId: "book / one",
  });
  await client.jobs.get({ jobId: "job / one" });
  await client.jobs.cancel({ jobId: "job / one" });
  await client.jobs.events({ after: 12, limit: 50 });

  assert.equal(calls[0].url,
    "/api/v1/jobs?state=running%2Ccancelling&kind=ocr&item_id=book%20%2F%20one");
  assert.equal(calls[0].init.method, "GET");
  assert.equal(calls[1].url, "/api/v1/jobs/job%20%2F%20one");
  assert.equal(calls[1].init.method, "GET");
  assert.equal(calls[2].url, "/api/v1/jobs/job%20%2F%20one/cancel");
  assert.equal(calls[2].init.method, "POST");
  assert.equal(calls[3].url, "/api/v1/job-events?after=12&limit=50");
  assert.equal(calls[3].init.method, "GET");
});

test("item queries use versioned path-safe engine resources", async () => {
  const { client, calls } = harness({ ok: true, items: [] });
  await client.items.list({ includeBuildCompatibility: true });
  await client.items.get({ itemId: "book / one" });
  await client.items.representations({ itemId: "book / one" });
  await client.items.artifacts({ itemId: "book / one" });
  await client.items.readiness({ itemId: "book / one" });

  assert.equal(calls[0].url,
    "/api/v1/items?projection=build-workbench");
  assert.equal(calls[1].url, "/api/v1/items/book%20%2F%20one");
  assert.equal(calls[2].url,
    "/api/v1/items/book%20%2F%20one/representations");
  assert.equal(calls[3].url,
    "/api/v1/items/book%20%2F%20one/artifacts");
  assert.equal(calls[4].url,
    "/api/v1/items/book%20%2F%20one/readiness");
  assert.ok(calls.every(({ init }) => init.method === "GET"));
});

test("item commands use versioned idempotent JSON contracts", async () => {
  const { client, calls } = harness({ ok: true, item: { id: "book-1" } });
  const item = {
    kind: "book",
    title: "A Book",
    metadata: {},
    representations: [],
  };
  const patch = {
    title: "A Revised Book",
    metadata_set: { cataloguer: "Ada" },
    metadata_remove: [],
    representations: null,
  };

  await client.items.create({
    item,
    idempotencyKey: "item-create-1",
  });
  await client.items.update({
    itemId: "book / one!*",
    patch,
    recordRevision: "ir-current",
    idempotencyKey: "item-update-1",
  });

  assert.equal(calls[0].url, "/api/v1/items");
  assert.equal(calls[0].init.method, "POST");
  assert.deepEqual(calls[0].init.headers, {
    Accept: "application/json",
    "Idempotency-Key": "item-create-1",
    "Content-Type": "application/json",
  });
  assert.deepEqual(JSON.parse(calls[0].init.body), { item });

  assert.equal(calls[1].url, "/api/v1/items/book%20%2F%20one%21%2A");
  assert.equal(calls[1].init.method, "PATCH");
  assert.deepEqual(calls[1].init.headers, {
    Accept: "application/json",
    "Idempotency-Key": "item-update-1",
    "If-Record-Match": '"ir-current"',
    "Content-Type": "application/json",
  });
  assert.deepEqual(JSON.parse(calls[1].init.body), { patch });
});

test("compatibility acquisition seeding is isolated behind conditional transport", async () => {
  const { client, calls } = harness({ ok: true, build: { id: "book-1" } });
  const compatibility = {
    extra: { scan_collection_id: "collection-1" },
    images: ["capture/cover.jpg"],
    capture_id: "phone-1",
  };

  await client.items.seedCompatibility({
    itemId: "book / one!*",
    compatibility,
    recordRevision: "ir-current",
  });

  assert.equal(calls[0].url, "/api/builds/book%20%2F%20one%21%2A");
  assert.equal(calls[0].init.method, "PATCH");
  assert.deepEqual(calls[0].init.headers, {
    Accept: "application/json",
    "Content-Type": "application/json",
  });
  assert.deepEqual(JSON.parse(calls[0].init.body), {
    ...compatibility,
    expect_updated_at: "ir-current",
  });
});

test("item commands reject missing or unsafe preconditions locally", () => {
  const { client, calls } = harness();
  const item = {
    kind: "book", title: "A Book", metadata: {}, representations: [],
  };
  const patch = {
    title: "Revised", metadata_set: {}, metadata_remove: [],
    representations: null,
  };

  assert.throws(
    () => client.items.create({ item }),
    (error) => error instanceof TypeError && /idempotencyKey/.test(error.message),
  );
  assert.throws(
    () => client.items.create({ item, idempotencyKey: "unsafe/key" }),
    (error) => error instanceof TypeError && /idempotencyKey/.test(error.message),
  );
  assert.throws(
    () => client.items.update({
      itemId: "book-1", patch, recordRevision: "ir-current",
    }),
    (error) => error instanceof TypeError && /idempotencyKey/.test(error.message),
  );
  assert.throws(
    () => client.items.update({
      itemId: "book-1", patch, recordRevision: "ir-current",
      idempotencyKey: "unsafe/key",
    }),
    (error) => error instanceof TypeError && /idempotencyKey/.test(error.message),
  );
  assert.throws(
    () => client.items.update({
      itemId: "book-1", patch, idempotencyKey: "item-update-1",
    }),
    (error) => error instanceof TypeError && /recordRevision/.test(error.message),
  );
  assert.throws(
    () => client.items.update({
      itemId: "book-1", patch, recordRevision: 'W/"ir-current"',
      idempotencyKey: "item-update-1",
    }),
    (error) => error instanceof TypeError && /recordRevision/.test(error.message),
  );
  for (const recordRevision of ["*", "ir current", "ir/current", `ir-${"x".repeat(512)}`]) {
    assert.throws(
      () => client.items.update({
        itemId: "book-1", patch, recordRevision,
        idempotencyKey: "item-update-1",
      }),
      (error) => error instanceof TypeError && /recordRevision/.test(error.message),
    );
  }
  assert.equal(calls.length, 0);
});

test("item lifecycle and tombstone reads use canonical cache-safe resources",
  async () => {
    const calls = [];
    const bodies = [
      lifecycleState({ future_state_metadata: { supported: true } }),
      {
        ok: true,
        schema: "librarytool.item-tombstone-list/1",
        tombstones: [itemTombstone()],
        future_list_metadata: true,
      },
      {
        ok: true,
        schema: "librarytool.item-tombstone/1",
        tombstone: itemTombstone(),
        future_detail_metadata: true,
      },
      {
        ok: true,
        schema: "librarytool.item-tombstone-list/1",
        tombstones: [],
      },
    ];
    const client = new EngineClient({
      transport: async (url, init) => {
        calls.push({ url, init });
        return response(200, bodies.shift());
      },
    });

    const state = await client.items.lifecycle({ itemId: "book:one" });
    const listed = await client.itemTombstones.list({ state: "deleted" });
    const detail = await client.itemTombstones.get({
      tombstoneId: "deleted:one",
    });
    await client.itemTombstones.list();

    assert.equal(state.item_revision, "item-r1");
    assert.equal(listed.tombstones[0].tombstone_id, "deleted:one");
    assert.equal(detail.tombstone.item_id, "book:one");
    assert.deepEqual(calls.map(({ url }) => url), [
      "/api/v1/items/book%3Aone/lifecycle",
      "/api/v1/item-tombstones?state=deleted",
      "/api/v1/item-tombstones/deleted%3Aone",
      "/api/v1/item-tombstones",
    ]);
    assert.ok(calls.every(({ init }) => init.method === "GET"));
    assert.ok(calls.every(({ init }) => init.cache === "no-cache"));
    assert.ok(calls.every(({ init }) => init.body === undefined));
    assert.ok(calls.every(({ init }) =>
      Object.keys(init.headers).length === 1 &&
      init.headers.Accept === "application/json"));
  });

test("item delete and tombstone restore own exact conditional headers",
  async () => {
    const calls = [];
    const bodies = [lifecycleResult("delete"), lifecycleResult("restore")];
    const client = new EngineClient({
      transport: async (url, init) => {
        calls.push({ url, init });
        return response(calls.length === 2 ? 201 : 200, bodies.shift());
      },
    });

    const deletion = await client.items.delete({
      itemId: "book:one",
      recordRevision: "item-r1",
      managedTreeRevision: "tree-r1",
      idempotencyKey: "delete:one",
    });
    const restoration = await client.itemTombstones.restore({
      tombstoneId: "deleted:one",
      tombstoneRevision: "tomb-r1",
      idempotencyKey: "restore:one",
    });

    assert.equal(deletion.receipt.action, "delete");
    assert.equal(restoration.receipt.action, "restore");
    assert.deepEqual(calls[0], {
      url: "/api/v1/items/book%3Aone",
      init: {
        method: "DELETE",
        headers: {
          Accept: "application/json",
          "Idempotency-Key": "delete:one",
          "If-Record-Match": '"item-r1"',
          "If-Managed-Tree-Match": '"tree-r1"',
        },
        cache: "no-store",
      },
    });
    assert.deepEqual(calls[1], {
      url: "/api/v1/item-tombstones/deleted%3Aone/restore",
      init: {
        method: "POST",
        headers: {
          Accept: "application/json",
          "Idempotency-Key": "restore:one",
          "If-Tombstone-Match": '"tomb-r1"',
        },
        cache: "no-store",
      },
    });
    assert.equal(calls[0].init.headers["Content-Type"], undefined);
    assert.equal(calls[1].init.headers["Content-Type"], undefined);
  });

test("lifecycle commands reject unsafe identities and preconditions locally",
  async () => {
    const { client, calls } = harness();
    const deletion = {
      itemId: "book:one",
      recordRevision: "item-r1",
      managedTreeRevision: "tree-r1",
      idempotencyKey: "delete:one",
    };
    const restoration = {
      tombstoneId: "deleted:one",
      tombstoneRevision: "tomb-r1",
      idempotencyKey: "restore:one",
    };
    const badIdentifiers = [
      undefined, null, 7, "", " bad", "../bad", "bad/id", "bad id",
      `x${"y".repeat(128)}`,
    ];

    for (const itemId of badIdentifiers) {
      await assert.rejects(client.items.lifecycle({ itemId }), /itemId/);
      await assert.rejects(client.items.delete({ ...deletion, itemId }),
        /itemId/);
    }
    for (const tombstoneId of badIdentifiers) {
      await assert.rejects(client.itemTombstones.get({ tombstoneId }),
        /tombstoneId/);
      await assert.rejects(client.itemTombstones.restore({
        ...restoration, tombstoneId,
      }), /tombstoneId/);
    }
    for (const idempotencyKey of badIdentifiers) {
      await assert.rejects(client.items.delete({
        ...deletion, idempotencyKey,
      }), /idempotencyKey/);
      await assert.rejects(client.itemTombstones.restore({
        ...restoration, idempotencyKey,
      }), /idempotencyKey/);
    }

    const badRecordRevisions = [
      undefined, null, 7, "", "*", "item revision", "item/revision",
      'W/"item-r1"', `r${"x".repeat(512)}`,
    ];
    for (const recordRevision of badRecordRevisions) {
      await assert.rejects(client.items.delete({
        ...deletion, recordRevision,
      }), /recordRevision/);
    }

    const badLifecycleRevisions = [
      undefined, null, 7, "", " tree-r1", "tree-r1 ", 'W/"tree-r1"',
      "tree\\revision", "tree\nrevision", "tree\ud800revision",
      `r${"x".repeat(512)}`,
    ];
    for (const managedTreeRevision of badLifecycleRevisions) {
      await assert.rejects(client.items.delete({
        ...deletion, managedTreeRevision,
      }), /managedTreeRevision/);
    }
    for (const tombstoneRevision of badLifecycleRevisions) {
      await assert.rejects(client.itemTombstones.restore({
        ...restoration, tombstoneRevision,
      }), /tombstoneRevision/);
    }
    for (const state of ["live", "DELETED", 7, {}, []]) {
      await assert.rejects(client.itemTombstones.list({ state }), /state/);
    }
    assert.equal(calls.length, 0);
  });

test("item lifecycle state validation fails closed on incoherent responses",
  async () => {
    const invalidBodies = [
      {},
      lifecycleState({ ok: "true" }),
      lifecycleState({ schema: "librarytool.item-lifecycle-state/2" }),
      lifecycleState({ state: "deleted" }),
      lifecycleState({ item_id: "other-item" }),
      lifecycleState({ item_revision: "" }),
      lifecycleState({ managed_tree_revision: 7 }),
      lifecycleState({ revision: 'W/"lifecycle-r1"' }),
    ];

    for (const body of invalidBodies) {
      const { client } = harness(body);
      await assert.rejects(
        client.items.lifecycle({ itemId: "book:one" }),
        (error) => {
          assert.ok(error instanceof EngineClientError);
          assert.equal(error.code, "invalid-response");
          assert.equal(error.status, 200);
          assert.equal(error.retryable, true);
          assert.equal(error.method, "GET");
          assert.equal(error.url, "/api/v1/items/book%3Aone/lifecycle");
          assert.strictEqual(error.body, body);
          return true;
        },
      );
    }
  });

test("tombstone read validation rejects aliases, duplicates, and bad states",
  async () => {
    const invalidDetails = [
      {
        ok: true, schema: "librarytool.item-tombstone/2",
        tombstone: itemTombstone(),
      },
      {
        ok: true, schema: "librarytool.item-tombstone/1",
        tombstone: itemTombstone({ tombstone_id: undefined }),
      },
      {
        ok: true, schema: "librarytool.item-tombstone/1",
        tombstone: itemTombstone({ tombstone_id: "DELETED:ONE" }),
      },
      {
        ok: true, schema: "librarytool.item-tombstone/1",
        tombstone: itemTombstone({ restored_item_revision: "item-r2" }),
      },
      {
        ok: true, schema: "librarytool.item-tombstone/1",
        tombstone: itemTombstone({
          state: "restored", restored_item_revision: "item-r1",
        }),
      },
    ];
    for (const body of invalidDetails) {
      const { client } = harness(body);
      await assert.rejects(client.itemTombstones.get({
        tombstoneId: "deleted:one",
      }), (error) => {
        assert.ok(error instanceof EngineClientError);
        assert.equal(error.code, "invalid-response");
        assert.equal(error.method, "GET");
        assert.equal(error.url,
          "/api/v1/item-tombstones/deleted%3Aone");
        assert.strictEqual(error.body, body);
        return true;
      });
    }

    const invalidLists = [
      { ok: true, schema: "librarytool.item-tombstone-list/1" },
      {
        ok: true, schema: "librarytool.item-tombstones/1", tombstones: [],
      },
      {
        ok: true, schema: "librarytool.item-tombstone-list/1",
        tombstones: [itemTombstone(), itemTombstone()],
      },
      {
        ok: true, schema: "librarytool.item-tombstone-list/1",
        tombstones: [itemTombstone(), itemTombstone({
          tombstone_id: "DELETED:ONE", revision: "tomb-r2",
          state: "restored", restored_item_revision: "item-r2",
        })],
      },
      {
        ok: true, schema: "librarytool.item-tombstone-list/1",
        tombstones: [itemTombstone(), itemTombstone({
          tombstone_id: "deleted:two", revision: "tomb-r2",
        })],
      },
      {
        ok: true, schema: "librarytool.item-tombstone-list/1",
        tombstones: [itemTombstone({
          state: "restored", revision: "tomb-r2",
          restored_item_revision: "item-r2",
        })],
      },
      {
        ok: true, schema: "librarytool.item-tombstone-list/1",
        tombstones: [itemTombstone({ item_id: undefined })],
      },
    ];
    for (const body of invalidLists) {
      const { client } = harness(body);
      await assert.rejects(client.itemTombstones.list({ state: "deleted" }),
        (error) => {
          assert.ok(error instanceof EngineClientError);
          assert.equal(error.code, "invalid-response");
          assert.equal(error.url,
            "/api/v1/item-tombstones?state=deleted");
          assert.strictEqual(error.body, body);
          return true;
        });
    }
  });

test("item deletion validates public receipts against the exact command",
  async () => {
    const missingReceipt = lifecycleResult("delete");
    missingReceipt.receipt = null;
    const invalidBodies = [
      missingReceipt,
      lifecycleResult("delete", { envelope: { ok: "true" } }),
      lifecycleResult("delete", {
        envelope: { schema: "librarytool.item-lifecycle-receipt/2" },
      }),
      lifecycleResult("delete", { envelope: { replayed: "false" } }),
      lifecycleResult("delete", {
        envelope: { command_sha256: "a".repeat(64) },
      }),
      lifecycleResult("delete", {
        receipt: { command_sha256: "a".repeat(64) },
      }),
      lifecycleResult("delete", { receipt: { action: "restore" } }),
      lifecycleResult("delete", { receipt: { operation_id: undefined } }),
      lifecycleResult("delete", { receipt: { operation_id: "delete:other" } }),
      lifecycleResult("delete", { receipt: { item_id: "book:other" } }),
      lifecycleResult("delete", {
        receipt: { deleted_item_revision: "item-r0" },
      }),
      lifecycleResult("delete", {
        receipt: { managed_tree_revision: "tree-r0" },
      }),
      lifecycleResult("delete", {
        receipt: { restored_item_revision: "item-r2" },
      }),
      lifecycleResult("delete", {
        receipt: { tombstone_before_revision: "tomb-r0" },
      }),
      lifecycleResult("delete", {
        tombstone: { item_id: "book:other" },
      }),
      lifecycleResult("delete", {
        tombstone: { deleted_item_revision: "item-r0" },
      }),
      lifecycleResult("delete", {
        tombstone: { managed_tree_revision: "tree-r0" },
      }),
      lifecycleResult("delete", {
        tombstone: {
          state: "restored", revision: "tomb-r2",
          restored_item_revision: "item-r2",
        },
      }),
    ];

    for (const body of invalidBodies) {
      const { client } = harness(body);
      await assert.rejects(client.items.delete({
        itemId: "book:one",
        recordRevision: "item-r1",
        managedTreeRevision: "tree-r1",
        idempotencyKey: "delete:one",
      }), (error) => {
        assert.ok(error instanceof EngineClientError);
        assert.equal(error.code, "invalid-response");
        assert.equal(error.retryable, true);
        assert.equal(error.method, "DELETE");
        assert.equal(error.url, "/api/v1/items/book%3Aone");
        assert.strictEqual(error.body, body);
        return true;
      });
    }
  });

test("item restoration validates public receipts against the tombstone CAS",
  async () => {
    const invalidBodies = [
      lifecycleResult("restore", { receipt: { action: "delete" } }),
      lifecycleResult("restore", {
        receipt: { operation_id: "restore:other" },
      }),
      lifecycleResult("restore", {
        receipt: { tombstone_before_revision: "tomb-r0" },
      }),
      lifecycleResult("restore", {
        receipt: { restored_item_revision: "" },
      }),
      lifecycleResult("restore", {
        receipt: { restored_item_revision: "item-r1" },
        tombstone: { restored_item_revision: "item-r1" },
      }),
      lifecycleResult("restore", {
        tombstone: { tombstone_id: "deleted:other" },
      }),
      lifecycleResult("restore", {
        tombstone: { revision: "tomb-r1" },
      }),
      lifecycleResult("restore", {
        tombstone: { state: "deleted", restored_item_revision: "" },
      }),
      lifecycleResult("restore", {
        tombstone: { restored_item_revision: "item-r3" },
      }),
      lifecycleResult("restore", {
        receipt: { command_sha256: "b".repeat(64) },
      }),
    ];

    for (const body of invalidBodies) {
      const { client } = harness(body);
      await assert.rejects(client.itemTombstones.restore({
        tombstoneId: "deleted:one",
        tombstoneRevision: "tomb-r1",
        idempotencyKey: "restore:one",
      }), (error) => {
        assert.ok(error instanceof EngineClientError);
        assert.equal(error.code, "invalid-response");
        assert.equal(error.retryable, true);
        assert.equal(error.method, "POST");
        assert.equal(error.url,
          "/api/v1/item-tombstones/deleted%3Aone/restore");
        assert.strictEqual(error.body, body);
        return true;
      });
    }
  });

test("ambiguous lifecycle failures preserve exact retry inputs", async () => {
  const calls = [];
  let attempt = 0;
  const client = new EngineClient({
    transport: async (url, init) => {
      calls.push({ url, init });
      attempt += 1;
      if (attempt === 1) throw new Error("connection closed after send");
      return response(200, lifecycleResult("delete", {
        envelope: { replayed: true },
      }));
    },
  });
  const command = Object.freeze({
    itemId: "book:one",
    recordRevision: "item-r1",
    managedTreeRevision: "tree-r1",
    idempotencyKey: "delete:one",
  });
  const original = copyJson(command);

  await assert.rejects(client.items.delete(command), (error) => {
    assert.ok(error instanceof EngineClientError);
    assert.equal(error.code, "network-error");
    assert.equal(error.retryable, true);
    assert.equal(error.method, "DELETE");
    assert.equal(error.url, "/api/v1/items/book%3Aone");
    return true;
  });
  const replay = await client.items.delete(command);

  assert.equal(replay.replayed, true);
  assert.deepEqual(command, original);
  assert.equal(calls.length, 2);
  assert.deepEqual(calls[1], calls[0]);
  assert.equal(calls[0].init.body, undefined);
});

test("lifecycle resources reject unexpected successful HTTP statuses",
  async () => {
    const cases = [
      [203, lifecycleState(), (client) => client.items.lifecycle({
        itemId: "book:one",
      })],
      [201, lifecycleResult("delete"), (client) => client.items.delete({
        itemId: "book:one",
        recordRevision: "item-r1",
        managedTreeRevision: "tree-r1",
        idempotencyKey: "delete:one",
      })],
      [202, {
        ok: true,
        schema: "librarytool.item-tombstone-list/1",
        tombstones: [],
      }, (client) => client.itemTombstones.list()],
      [204, {
        ok: true,
        schema: "librarytool.item-tombstone/1",
        tombstone: itemTombstone(),
      }, (client) => client.itemTombstones.get({
        tombstoneId: "deleted:one",
      })],
      [200, lifecycleResult("restore"),
        (client) => client.itemTombstones.restore({
          tombstoneId: "deleted:one",
          tombstoneRevision: "tomb-r1",
          idempotencyKey: "restore:one",
        })],
      [201, lifecycleResult("restore", { envelope: { replayed: true } }),
        (client) => client.itemTombstones.restore({
          tombstoneId: "deleted:one",
          tombstoneRevision: "tomb-r1",
          idempotencyKey: "restore:one",
        })],
    ];

    for (const [status, body, invoke] of cases) {
      const client = new EngineClient({
        transport: async () => response(status, body),
      });
      await assert.rejects(invoke(client), (error) => {
        assert.ok(error instanceof EngineClientError);
        assert.equal(error.code, "invalid-response");
        assert.equal(error.status, status);
        assert.equal(error.retryable, true);
        return true;
      });
    }
  });

test("lifecycle engine errors preserve structured conflicts and sent CAS",
  async () => {
    const calls = [];
    const body = {
      ok: false,
      error: "the managed tree changed",
      code: "managed_tree_revision_conflict",
      retryable: false,
      conflict: { expected: "tree-r1", actual: "tree-r2" },
      details: { resource: "managed-tree" },
    };
    const client = new EngineClient({
      transport: async (url, init) => {
        calls.push({ url, init });
        return response(409, body);
      },
    });

    await assert.rejects(client.items.delete({
      itemId: "book:one",
      recordRevision: "item-r1",
      managedTreeRevision: "tree-r1",
      idempotencyKey: "delete:one",
    }), (error) => {
      assert.ok(error instanceof EngineClientError);
      assert.equal(error.status, 409);
      assert.equal(error.code, "managed_tree_revision_conflict");
      assert.equal(error.retryable, false);
      assert.deepEqual(error.details, { resource: "managed-tree" });
      assert.deepEqual(error.conflict,
        { expected: "tree-r1", actual: "tree-r2" });
      assert.strictEqual(error.body, body);
      return true;
    });
    assert.equal(calls[0].init.headers["If-Record-Match"], '"item-r1"');
    assert.equal(calls[0].init.headers["If-Managed-Tree-Match"],
      '"tree-r1"');
    assert.equal(calls[0].init.headers["Idempotency-Key"], "delete:one");
  });

test("representation commands use path-safe dual-CAS engine resources", async () => {
  const { client, calls } = harness({ ok: true, receipt: {} });
  const representation = {
    source_token: "C:\\Scans\\herbal.pdf",
    acquisition: "reference",
    expected_content_sha256: "",
    expected_size: null,
    role: "alternate",
    media_type: "application/pdf",
    label: "Alternate scan",
    metadata: { shelf: "A-2" },
  };

  await client.items.attachRepresentation({
    itemId: "book / one",
    representationId: "scan !*",
    representation,
    recordRevision: "item-r1",
    idempotencyKey: "attach-1",
  });
  await client.items.replaceRepresentation({
    itemId: "book / one",
    representationId: "scan !*",
    representation,
    recordRevision: "item-r2",
    representationRevision: "source-r1",
    idempotencyKey: "replace-1",
  });
  await client.items.detachRepresentation({
    itemId: "book / one",
    representationId: "scan !*",
    recordRevision: "item-r3",
    representationRevision: "source-r2",
    idempotencyKey: "detach-1",
  });

  const url = "/api/v1/items/book%20%2F%20one/representations/scan%20%21%2A";
  assert.deepEqual(calls.map((call) => [call.url, call.init.method]), [
    [url, "PUT"], [url, "PUT"], [url, "DELETE"],
  ]);
  assert.equal(calls[0].init.headers["If-Representation-Match"], undefined);
  assert.equal(calls[0].init.headers["If-Record-Match"], '"item-r1"');
  assert.equal(calls[1].init.headers["If-Representation-Match"],
    '"source-r1"');
  assert.deepEqual(JSON.parse(calls[0].init.body), { representation });
  assert.equal(calls[2].init.headers["If-Representation-Match"],
    '"source-r2"');
  assert.equal(calls[2].init.body, undefined);
});

test("representation commands reject ambiguous or missing preconditions locally", () => {
  const { client, calls } = harness();
  const base = {
    itemId: "book", representationId: "scan", representation: {},
    recordRevision: "item-r1", idempotencyKey: "representation-1",
  };
  assert.throws(
    () => client.items.attachRepresentation({
      ...base, representationRevision: "source-r1",
    }), /does not accept representationRevision/);
  assert.throws(
    () => client.items.replaceRepresentation(base),
    /representationRevision/);
  assert.throws(
    () => client.items.detachRepresentation({
      itemId: "book", representationId: "scan",
      recordRevision: "item-r1", idempotencyKey: "detach-1",
    }), /representationRevision/);
  assert.equal(calls.length, 0);
});

test("translations use versioned aggregate reads and dual-CAS page writes", async () => {
  const { client, calls } = harness({
    ok: true, translation: { id: "translation-1" },
  });
  await client.translations.list({ itemId: "book / one" });
  await client.translations.get({
    itemId: "book / one", translationId: "translation / one",
  });
  await client.translations.replacePage({
    itemId: "book / one", translationId: "translation / one",
    selector: "page:7", text: "Nueva.",
    documentRevision: "tr-current", sourceRevision: "ts-current",
  });

  assert.equal(calls[0].url,
    "/api/v1/items/book%20%2F%20one/translations");
  assert.equal(calls[0].init.method, "GET");
  assert.equal(calls[1].url,
    "/api/v1/items/book%20%2F%20one/translations/translation%20%2F%20one");
  assert.equal(calls[1].init.method, "GET");
  assert.equal(calls[2].url,
    "/api/v1/items/book%20%2F%20one/translations/translation%20%2F%20one" +
    "/pages/page%3A7");
  assert.equal(calls[2].init.method, "PUT");
  assert.equal(
    calls[2].init.headers["If-Document-Match"], '"tr-current"');
  assert.equal(calls[2].init.headers["If-Source-Match"], '"ts-current"');
  assert.deepEqual(JSON.parse(calls[2].init.body), {
    text: "Nueva.",
    expected_document_revision: "tr-current",
    expected_source_revision: "ts-current",
  });
});

test("translation page writes reject missing revision tokens locally", () => {
  const { client, calls } = harness();
  assert.throws(() => client.translations.replacePage({
    itemId: "book", translationId: "translation-1",
    selector: "page:1", text: "Nueva.", sourceRevision: "ts-current",
  }), /documentRevision/);
  assert.throws(() => client.translations.replacePage({
    itemId: "book", translationId: "translation-1",
    selector: "page:1", text: "Nueva.", documentRevision: "tr-current",
  }), /sourceRevision/);
  assert.equal(calls.length, 0);
});

test("EngineClient encodes Replica path components and query values", async () => {
  const { client, calls } = harness({ ok: true, found: false, revision: "rr-1" });
  await client.replica.pages.get({
    bookId: "book /alpha", sourceId: "scan !'()* /二", page: 7,
  });

  assert.equal(calls.length, 1);
  assert.equal(calls[0].url,
    "/api/builds/book%20%2Falpha/ocr-regions" +
    "?src=scan%20%21%27%28%29%2A%20%2F%E4%BA%8C&page=7");
  assert.equal(calls[0].init.method, "GET");
  assert.deepEqual(calls[0].init.headers, { Accept: "application/json" });
  assert.equal(calls[0].init.body, undefined);
});

test("page save owns JSON encoding and the If-Match contract", async () => {
  const { client, calls } = harness({ ok: true, revision: "rr-next" });
  const items = [{ rid: "region-1", role: "body" }];
  await client.replica.pages.save({
    bookId: "book-1", sourceId: "primary", page: 4, revision: "rr-old",
    record: {
      doc: "compiled.txt", dims: { w: 100, h: 200 }, ext: { keep: true },
      state: "verified", items,
    },
  });

  const { url, init } = calls[0];
  assert.equal(url, "/api/builds/book-1/ocr-regions");
  assert.equal(init.method, "PUT");
  assert.equal(init.headers["If-Match"], '"rr-old"');
  assert.equal(init.headers["Content-Type"], "application/json");
  assert.deepEqual(JSON.parse(init.body), {
    src: "primary", page: 4, doc: "compiled.txt",
    dims: { w: 100, h: 200 }, ext: { keep: true },
    state: "verified", items, expect_revision: "rr-old",
  });
});

test("Replica region detection starts a versioned page-scoped job", async () => {
  const { client, calls } = harness({
    ok: true,
    provider: "mistral",
    job: { id: "job-1", state: "running" },
  });
  const result = await client.replica.detection.start({
    bookId: "book / one", sourceId: "scan 2", page: 17,
    revision: "rr-current", provider: "automatic",
    idempotencyKey: "detect-command-1",
  });

  assert.equal(result.job.id, "job-1");
  assert.equal(calls[0].url,
    "/api/v1/items/book%20%2F%20one/replica/region-detection-jobs");
  assert.equal(calls[0].init.method, "POST");
  assert.equal(calls[0].init.headers["If-Match"], '"rr-current"');
  assert.deepEqual(JSON.parse(calls[0].init.body), {
    source_id: "scan 2", page: 17, provider: "automatic",
    expect_revision: "rr-current", idempotency_key: "detect-command-1",
  });
});

test("proposal decisions own both conditional revisions", async () => {
  const { client, calls } = harness({ ok: true });
  await client.replica.proposals.decide({
    bookId: "book", sourceId: "scan", page: 9, action: "apply",
    revision: "rr-current", proposalRevision: "rp-current",
  });

  const { init } = calls[0];
  assert.equal(init.method, "POST");
  assert.equal(init.headers["If-Match"], '"rr-current"');
  assert.equal(init.headers["If-Proposal-Match"], '"rp-current"');
  assert.deepEqual(JSON.parse(init.body), {
    src: "scan", page: 9, action: "apply",
    expect_revision: "rr-current",
    expect_proposal_revision: "rp-current",
  });
});

test("Replica import uses the versioned idempotent multipart contract", async () => {
  class FakeFormData {
    constructor() { this.entries = []; }
    append(name, value) { this.entries.push([name, value]); }
  }
  const calls = [];
  const client = new EngineClient({
    transport: async (url, init) => {
      calls.push({ url, init });
      return response(200, { ok: true });
    },
    formDataFactory: () => new FakeFormData(),
  });
  const file = { name: "edition.lib" };
  await client.replica.packages.import({
    bookId: "book one", sourceId: "scan & notes", file,
    overwrite: true, idempotencyKey: "import-command-1",
  });

  const { url, init } = calls[0];
  assert.equal(url,
    "/api/v1/items/book%20one/replica/lib-imports" +
    "?source_id=scan%20%26%20notes&overwrite=1");
  assert.equal(init.method, "POST");
  assert.deepEqual(init.body.entries, [["lib", file]]);
  assert.equal(init.headers["Idempotency-Key"], "import-command-1");
  assert.equal(init.headers["Content-Type"], undefined);
  assert.equal(init.headers.Accept, "application/json");
});

test("Replica open uses the composite new-item multipart contract", async () => {
  class FakeFormData {
    constructor() { this.entries = []; }
    append(name, value) { this.entries.push([name, value]); }
  }
  const calls = [];
  const client = new EngineClient({
    transport: async (url, init) => {
      calls.push({ url, init });
      return response(201, { ok: true, replayed: false });
    },
    formDataFactory: () => new FakeFormData(),
  });
  const file = { name: "edition.lib" };

  await client.replica.packages.open({
    file, idempotencyKey: "open-command-1",
  });

  const { url, init } = calls[0];
  assert.equal(url, "/api/v1/lib-opens");
  assert.equal(init.method, "POST");
  assert.deepEqual(init.body.entries, [["lib", file]]);
  assert.equal(init.headers["Idempotency-Key"], "open-command-1");
  assert.equal(init.headers["Content-Type"], undefined);
});

test("Replica package commands reject unsafe idempotency keys locally", () => {
  class FakeFormData {
    append() {}
  }
  let calls = 0;
  const client = new EngineClient({
    transport: async () => {
      calls += 1;
      return response(200, { ok: true });
    },
    formDataFactory: () => new FakeFormData(),
  });

  assert.throws(
    () => client.replica.packages.import({
      bookId: "book", sourceId: "primary", file: {},
    }),
    (error) => error instanceof TypeError && /idempotencyKey/.test(error.message),
  );
  assert.throws(
    () => client.replica.packages.import({
      bookId: "book", sourceId: "primary", file: {},
      idempotencyKey: "unsafe/key",
    }),
    (error) => error instanceof TypeError && /idempotencyKey/.test(error.message),
  );
  assert.throws(
    () => client.replica.packages.open({ file: {} }),
    (error) => error instanceof TypeError && /idempotencyKey/.test(error.message),
  );
  assert.equal(calls, 0);
});

test("EngineClientError preserves structured engine conflict details", async () => {
  const client = new EngineClient({
    transport: async () => response(409, {
      ok: false,
      error: "regions changed elsewhere",
      code: "revision-conflict",
      conflict: { expected: "rr-old", actual: "rr-new" },
    }),
  });

  await assert.rejects(
    client.replica.pages.get({ bookId: "book", sourceId: "primary", page: 1 }),
    (error) => {
      assert.ok(error instanceof EngineClientError);
      assert.equal(error.message, "regions changed elsewhere");
      assert.equal(error.status, 409);
      assert.equal(error.code, "revision-conflict");
      assert.deepEqual(error.conflict,
        { expected: "rr-old", actual: "rr-new" });
      assert.equal(error.retryable, false);
      assert.equal(error.method, "GET");
      assert.match(error.url, /ocr-regions/);
      return true;
    });
});

test("malformed and network responses are normalized as EngineClientError", async () => {
  const malformed = new EngineClient({
    transport: async () => ({
      ok: true, status: 200, json: async () => { throw new SyntaxError("bad"); },
    }),
  });
  await assert.rejects(
    malformed.pdf.info({ path: "book.pdf" }),
    (error) => error instanceof EngineClientError &&
      error.code === "invalid-response" && error.status === 200);

  const offline = new EngineClient({
    transport: async () => { throw new Error("offline"); },
  });
  await assert.rejects(
    offline.pdf.info({ path: "book.pdf" }),
    (error) => error instanceof EngineClientError &&
      error.code === "network-error" && error.retryable);
});

test("URL builders centralize all Replica resource routes", () => {
  const { client } = harness();
  const page = client.pdf.pageImageUrl({
    path: "source #1.pdf", page: 3, width: 1100,
  });
  assert.equal(page,
    "/api/pdf/pageimg?path=source%20%231.pdf&page=3&w=1100");
  assert.equal(client.replica.pages.imageUrl({
    pdfPath: "source #1.pdf", page: 3, width: 1100,
  }), page);
  assert.equal(client.replica.figures.imageUrl({
    bookId: "book / 1", name: "plate #1.png",
  }), "/api/builds/book%20%2F%201/ocr/images/plate%20%231.png");
  assert.equal(client.replica.packages.exportUrl({
    bookId: "book", sourceId: "scan two",
  }), "/api/builds/book/replica-export?src=scan%20two");
  assert.equal(client.replica.printUrl({
    bookId: "book", sourceId: "primary", layer: "français",
  }), "/api/builds/book/replica-print?src=primary&layer=fran%C3%A7ais");
  assert.equal(client.replica.printUrl({
    bookId: "book", sourceId: "primary", layer: "",
  }), "/api/builds/book/replica-print?src=primary");
});

test("classic browser script exposes one global engineClient instance", () => {
  class FakeFormData { append() {} }
  const sandbox = {
    fetch: async () => response(200, { ok: true }),
    FormData: FakeFormData,
  };
  sandbox.window = sandbox;
  vm.runInNewContext(clientSource, sandbox, { filename: "engine-client.js" });

  assert.equal(typeof sandbox.EngineClient, "function");
  assert.equal(typeof sandbox.EngineClientError, "function");
  assert.ok(sandbox.engineClient instanceof sandbox.EngineClient);
  const first = sandbox.engineClient;
  vm.runInNewContext(clientSource, sandbox, { filename: "engine-client.js" });
  assert.equal(sandbox.engineClient, first);
});

test("Replica workbench contains no direct transport or API route literals", () => {
  const app = fs.readFileSync(appPath, "utf8");
  const start = app.indexOf("// --- Replica workbench");
  const end = app.indexOf("function initOcrTab", start);
  assert.ok(start >= 0 && end > start, "Replica source boundaries are present");
  const replica = app.slice(start, end);
  assert.doesNotMatch(replica, /\bfetch\s*\(/);
  assert.doesNotMatch(replica, /["'`]\/api\//);

  const template = fs.readFileSync(templatePath, "utf8");
  const clientScript = template.indexOf("filename='engine-client.js'");
  const appScript = template.indexOf("filename='app.js'");
  assert.ok(clientScript >= 0 && clientScript < appScript,
    "engine-client.js loads before app.js");
});

test("Replica translation preview consumes aggregate summaries and selectors", () => {
  const app = fs.readFileSync(appPath, "utf8");
  const start = app.indexOf("async function rwLoadTranslations");
  const end = app.indexOf("let rwRenderSeq", start);
  assert.ok(start >= 0 && end > start);
  const preview = app.slice(start, end);
  assert.match(preview, /translations\.list\(\{\s*itemId:/);
  assert.match(preview, /t\.id\s*&&\s*t\.target_language/);
  assert.match(preview, /translations\.get\(\{[\s\S]*translationId:\s*layer/);
  assert.match(preview, /translationPage\.selector/);
  assert.match(preview, /sec\.get\(`page:\$\{page\}`\)/);
  assert.doesNotMatch(preview, /ocrPageSections\(r\.text/);

  const printStart = app.indexOf('el("rw-print").addEventListener');
  const printEnd = app.indexOf('el("rw-import-file").addEventListener', printStart);
  const print = app.slice(printStart, printEnd);
  assert.match(print, /transSummaries\.find/);
  assert.match(print, /summary\s*\?\s*summary\.target_language/);
});

test("the initial build load crosses the semantic item client boundary", () => {
  const app = fs.readFileSync(appPath, "utf8");
  const start = app.indexOf("async function loadBuilds()");
  const end = app.indexOf("function allBuildsSorted", start);
  assert.ok(start >= 0 && end > start);
  const loader = app.slice(start, end);
  assert.match(loader, /engineClient\.items\.list/);
  assert.match(loader, /includeBuildCompatibility:\s*true/);
  assert.doesNotMatch(loader, /\bfetch\s*\(/);
  assert.doesNotMatch(loader, /engineClient\.items\.update/);
});

test("the initial build load derives legacy volume grouping without writing", async () => {
  const app = fs.readFileSync(appPath, "utf8");
  const start = app.indexOf("async function loadBuilds()");
  const end = app.indexOf("function allBuildsSorted", start);
  assert.ok(start >= 0 && end > start);
  const calls = { list: 0, update: 0, fetch: 0 };
  const state = { builds: {} };
  const context = vm.createContext({
    state,
    engineClient: { items: {
      list: async () => {
        calls.list += 1;
        return { items: [{ id: "book", compatibility: { build: {
          id: "book", title: "Herbal", volume: "II", group_id: "",
          updated_at: "item-r1",
        } } }] };
      },
      update: async () => { calls.update += 1; },
    } },
    engineBuildProjection: (item) => ({
      ...item.compatibility.build,
      _record_revision: item.compatibility.build.updated_at,
    }),
    volNum: (build) => build.volume,
    buildGroupIdFor: (build) => `group:${build.volume}`,
    fetch: async () => { calls.fetch += 1; },
  });
  vm.runInContext(`${app.slice(start, end)}
this.loadBuilds = loadBuilds;`, context);

  await context.loadBuilds();
  assert.deepEqual(calls, { list: 1, update: 0, fetch: 0 });
  assert.equal(state.builds.book.group_id, "group:II");
  assert.equal(state.builds.book._record_revision, "item-r1",
    "a display-only derivation cannot invent a committed item revision");
});

test("build projections retain item command eligibility", () => {
  const app = fs.readFileSync(appPath, "utf8");
  const start = app.indexOf("function engineBuildProjection");
  const end = app.indexOf("async function loadBuilds", start);
  assert.ok(start >= 0 && end > start);
  const context = vm.createContext({});
  vm.runInContext(`${app.slice(start, end)}
this.project = engineBuildProjection;`, context);
  const item = {
    id: "book", title: "Herbal", record_revision: "item-r1",
    compatibility: { build: { id: "legacy", title: "Old" } },
    representations: [],
    workbench_state: { available_commands: [
      "representation.attach", "representation.attach", "replica.open",
    ] },
  };

  const build = context.project(item);
  assert.deepEqual(Array.from(build._available_commands), [
    "representation.attach", "replica.open",
  ]);
  item.workbench_state.available_commands.push("representation.detach");
  assert.equal(build._available_commands.includes("representation.detach"), false);
});

test("late semantic item reads cannot replace a newer incarnation", async () => {
  const app = fs.readFileSync(appPath, "utf8");
  const refreshStart = app.indexOf("async function refreshBuildEngineRecord");
  const refreshEnd = app.indexOf("function representationOperationKey", refreshStart);
  const generationStart = app.indexOf("const buildEngineRecordGenerations");
  const generationEnd = app.indexOf("let secondaryRepresentationSequence", generationStart);
  assert.ok(refreshStart >= 0 && refreshEnd > refreshStart);
  assert.ok(generationStart >= 0 && generationEnd > generationStart);

  const pendingReads = [];
  const state = { builds: {
    book: { id: "book", _record_revision: "item-r1", marker: "initial" },
  } };
  const context = vm.createContext({
    state,
    engineClient: { items: { get: () => new Promise((resolve) => {
      pendingReads.push(resolve);
    }) } },
    engineBuildProjection: (item) => item.build,
  });
  vm.runInContext(`${app.slice(generationStart, generationEnd)}
${app.slice(refreshStart, refreshEnd)}
this.api = { refreshBuildEngineRecord, advanceBuildEngineRecordGeneration };`, context);

  const staleAfterReceipt = context.api.refreshBuildEngineRecord("book");
  context.api.advanceBuildEngineRecordGeneration("book");
  state.builds.book = {
    id: "book", _record_revision: "item-r2", marker: "receipt",
  };
  pendingReads.shift()({
    item: { build: {
      id: "book", _record_revision: "item-r1", marker: "stale-read",
    } },
  });
  await staleAfterReceipt;
  assert.equal(state.builds.book.marker, "receipt");

  const staleAfterDelete = context.api.refreshBuildEngineRecord("book");
  context.api.advanceBuildEngineRecordGeneration("book");
  delete state.builds.book;
  pendingReads.shift()({
    item: { build: {
      id: "book", _record_revision: "item-r2", marker: "deleted-read",
    } },
  });
  await staleAfterDelete;
  assert.equal("book" in state.builds, false);
});

test("interactive PDF attachment crosses the representation command boundary", () => {
  const app = fs.readFileSync(appPath, "utf8");
  const start = app.indexOf("async function refreshBuildEngineRecord");
  const end = app.indexOf("const ITEM_EDIT_METADATA_FIELDS", start);
  assert.ok(start >= 0 && end > start);
  const attachment = app.slice(start, end);

  assert.match(attachment, /engineClient\.items\.get\(/);
  assert.match(attachment, /engineClient\.items\.attachRepresentation\(/);
  assert.match(attachment, /engineClient\.items\.replaceRepresentation\(/);
  assert.match(attachment, /engineClient\.items\.detachRepresentation\(/);
  assert.match(attachment, /pendingRepresentationMutations/);
  assert.match(attachment, /return receipt/);
  assert.match(attachment, /recordRevision:\s*transition\.receipt\.after_item_revision/);
  assert.match(attachment, /representationRevision:\s*current\s*&&\s*current\.revision/);
  assert.doesNotMatch(attachment, /\bfetch\s*\(/);
  assert.doesNotMatch(attachment, /["'`]\/api\/builds/);
  assert.doesNotMatch(attachment, /method:\s*["']HEAD["']/);

  const saveStart = app.indexOf("async function commitBuildMetadataFields");
  const saveEnd = app.indexOf("const pendingBuildLifecycleDeletes", saveStart);
  const save = app.slice(saveStart, saveEnd);
  assert.match(save, /sourceDraftPending/);
  const metadataStart = app.indexOf("function buildMetadataPatchFromEditor");
  const metadataEnd = app.indexOf("function buildMetadataPatchFromBuild",
    metadataStart);
  assert.doesNotMatch(app.slice(metadataStart, metadataEnd),
    /metadata\["pdf_file"\]/);

  const refreshStart = app.indexOf("async function refreshSourceTab");
  const refreshEnd = app.indexOf("async function syncBuildFolder", refreshStart);
  const refresh = app.slice(refreshStart, refreshEnd);
  assert.match(refresh,
    /const receipt = await setBuildRepresentation\([\s\S]*if \(receipt\)/);
  assert.match(refresh, /representationFailureMessage/);
});

function representationSnapshot(id, revision) {
  return {
    id,
    revision,
    role: id === "primary" ? "primary" : "alternate",
    media_type: "application/pdf",
    locator: `urn:test:${id}`,
    label: id === "primary" ? "Primary source" : "Alternate source",
    available: true,
    disposition: "referenced",
    content_sha256: "a".repeat(64),
    size: 42,
    metadata: { fixture: true },
  };
}

function representationResult(action, {
  itemId = "book", representationId = "primary",
  beforeItem = "item-r1", afterItem = "item-r2",
  before = null, after = representationSnapshot(representationId, "source-r1"),
} = {}) {
  return {
    ok: true,
    replayed: false,
    receipt: {
      action,
      operation_id: `${action}-operation`,
      command_sha256: "b".repeat(64),
      item_id: itemId,
      representation_id: representationId,
      before_item_revision: beforeItem,
      after_item_revision: afterItem,
      before,
      after,
    },
  };
}

function itemCreateResult(args, {
  itemId = "new-book", revision = "item-r1", replayed = false,
} = {}) {
  return {
    ok: true,
    schema: "librarytool.item-mutation-receipt/1",
    replayed,
    receipt: {
      action: "create",
      operation_id: args.idempotencyKey,
      command_sha256: "c".repeat(64),
      item_id: itemId,
      before_revision: "",
      after_revision: revision,
      item: {
        id: itemId,
        revision,
        kind: args.item.kind,
        title: args.item.title,
        metadata: JSON.parse(JSON.stringify(args.item.metadata)),
        representations: [],
      },
      deletion: null,
    },
  };
}

function representationUiHarness({ build, attach, replace, detach, refresh } = {}) {
  const app = fs.readFileSync(appPath, "utf8");
  const start = app.indexOf("function representationOperationKey");
  const end = app.indexOf("async function patchBuildRaw", start);
  assert.ok(start >= 0 && end > start);
  const operations = [];
  const initialBuild = build || {
    id: "book", title: "Herbal", _record_revision: "item-r1",
    _representations: [], pdf_file: "", pdf_sources: [],
  };
  if (!Object.prototype.hasOwnProperty.call(initialBuild, "_available_commands")) {
    initialBuild._available_commands = [
      "representation.attach", "representation.replace", "representation.detach",
    ];
  }
  const state = { builds: { book: initialBuild } };
  const context = vm.createContext({
    state,
    crypto: { randomUUID: () => "12345678-1234-4234-8234-123456789abc" },
    engineClient: { items: {
      attachRepresentation: attach || (async () => {
        throw new Error("unexpected attach");
      }),
      replaceRepresentation: replace || (async () => {
        throw new Error("unexpected replace");
      }),
      detachRepresentation: detach || (async () => {
        throw new Error("unexpected detach");
      }),
    } },
    refreshBuildEngineRecord: refresh || (async (id) => state.builds[id]),
    pushOp: (label, undoFn, redoFn, revert, originTab) => {
      operations.push({ label, undoFn, redoFn, revert, originTab });
      return operations.length;
    },
  });
  vm.runInContext(`${app.slice(start, end)}
this.api = { setBuildRepresentation, pushRepresentationOp,
  secondaryRepresentationId, representationFailureMessage,
  representationCommandAvailable, updateRepresentationMutationControls,
  clearPendingRepresentationMutationsForItem,
  invalidateRepresentationItemIncarnation,
  generation: buildEngineRecordGeneration,
  pendingCount: () => pendingRepresentationMutations.size };`, context);
  return { api: context.api, context, state, operations };
}

test("representation controls follow advertised workbench commands", () => {
  const build = {
    id: "book", _record_revision: "item-r1", _representations: [],
    _available_commands: [], pdf_file: "", pdf_sources: [],
  };
  const h = representationUiHarness({ build });
  const elements = {
    "b-pdf_file": { value: "", disabled: false },
    "b-pdf-attach": { disabled: false },
    "b-pdf-browse": { disabled: false },
    "b-pdf2-add": { disabled: false },
    "b-src-msg": { textContent: "" },
    "b-pdf-sources": {
      querySelectorAll: () => [{ disabled: false }],
    },
  };
  h.context.el = (id) => elements[id] || null;

  h.api.updateRepresentationMutationControls(build);
  assert.equal(elements["b-pdf_file"].disabled, true);
  assert.equal(elements["b-pdf-attach"].disabled, true);
  assert.equal(elements["b-pdf-browse"].disabled, true);
  assert.equal(elements["b-pdf2-add"].disabled, true);
  assert.equal(elements["b-src-msg"].textContent, "Source tools unavailable");

  build._available_commands = ["representation.attach"];
  elements["b-src-msg"].textContent = "";
  h.api.updateRepresentationMutationControls(build);
  assert.equal(elements["b-pdf_file"].disabled, false);
  assert.equal(elements["b-pdf-attach"].disabled, true);
  assert.equal(elements["b-pdf-browse"].disabled, false);
  assert.equal(elements["b-pdf2-add"].disabled, false);
  assert.equal(elements["b-src-msg"].textContent, "");

  elements["b-pdf_file"].value = "C:/scans/herbal.pdf";
  h.api.updateRepresentationMutationControls(build);
  assert.equal(elements["b-pdf-attach"].disabled, false);
});

test("unknown representation capability state fails closed", async () => {
  let attachCalls = 0;
  const build = {
    id: "book", _record_revision: "item-r1", _representations: [],
    pdf_file: "", pdf_sources: [],
  };
  const h = representationUiHarness({
    build,
    attach: async () => { attachCalls += 1; },
  });
  delete build._available_commands;
  const elements = {
    "b-pdf_file": { value: "C:/scans/herbal.pdf", disabled: false },
    "b-pdf-attach": { disabled: false },
    "b-pdf-browse": { disabled: false },
    "b-pdf2-add": { disabled: false },
    "b-src-msg": { textContent: "" },
    "b-pdf-sources": { querySelectorAll: () => [] },
  };
  h.context.el = (id) => elements[id] || null;

  h.api.updateRepresentationMutationControls(build);
  assert.equal(elements["b-pdf_file"].disabled, true);
  assert.equal(elements["b-pdf-attach"].disabled, true);
  assert.equal(elements["b-pdf-browse"].disabled, true);
  assert.equal(elements["b-pdf2-add"].disabled, true);
  assert.equal(elements["b-src-msg"].textContent, "Source tools unavailable");

  const receipt = await h.api.setBuildRepresentation(
    "book", "primary", "C:/scans/herbal.pdf",
    { intent: "attach", recordRevision: "item-r1" });
  assert.equal(receipt, null);
  assert.equal(attachCalls, 0);
});

test("representation failures keep unavailable, invalid PDF, and CAS states distinct", async () => {
  async function failureMessage(error, build) {
    const h = representationUiHarness({
      build: build || {
        id: "book", _record_revision: "item-r1", _representations: [],
        _available_commands: ["representation.attach"],
        pdf_file: "", pdf_sources: [],
      },
      attach: async () => { throw error; },
      replace: async () => { throw error; },
    });
    const current = h.state.builds.book._representations[0] || null;
    await h.api.setBuildRepresentation(
      "book", "primary", "C:/scans/herbal.pdf", {
        intent: current ? "replace" : "attach",
        recordRevision: "item-r1",
        representationRevision: current && current.revision,
      });
    return h.api.representationFailureMessage("book", "primary");
  }

  assert.equal(await failureMessage(Object.assign(new Error("module absent"), {
    code: "representation_command_unavailable", status: 503, retryable: true,
  })), "Source tools unavailable");
  assert.equal(await failureMessage(Object.assign(new Error("not a PDF"), {
    code: "invalid_representation_source", status: 400,
  })), "Invalid or changed PDF");
  const current = representationSnapshot("primary", "source-r1");
  assert.equal(await failureMessage(Object.assign(new Error("stale"), {
    code: "representation_revision_conflict", status: 409,
  }), {
    id: "book", _record_revision: "item-r1", _representations: [current],
    _available_commands: ["representation.replace"],
    pdf_file: "C:/scans/old.pdf", pdf_sources: [],
  }), "Source changed elsewhere");
});

test("deleting an item incarnation drops uncertain representation commands", async () => {
  const error = Object.assign(new Error("response lost"), {
    code: "network-error", status: 0, retryable: true,
  });
  const h = representationUiHarness({ attach: async () => { throw error; } });
  await h.api.setBuildRepresentation(
    "book", "primary", "C:/scans/herbal.pdf",
    { intent: "attach", recordRevision: "item-r1" });
  assert.equal(h.api.pendingCount(), 1);

  h.api.invalidateRepresentationItemIncarnation("book");
  assert.equal(h.api.pendingCount(), 0);
  assert.equal(h.api.generation("book"), 1);

  const app = fs.readFileSync(appPath, "utf8");
  const start = app.indexOf("const pendingBuildLifecycleDeletes");
  const end = app.indexOf("function exportBuilds", start);
  const lifecycle = app.slice(start, end);
  assert.match(lifecycle, /invalidateRepresentationItemIncarnation/);
  assert.match(lifecycle, /engineClient\.items\.lifecycle/);
  assert.match(lifecycle, /engineClient\.items\.delete/);
  assert.match(lifecycle, /engineClient\.itemTombstones\.restore/);
  assert.doesNotMatch(lifecycle, /\bfetch\s*\(/);
  assert.doesNotMatch(lifecycle, /["'`]\/api\/(?:builds|trash)/);
});

function buildLifecycleUiHarness({
  build, preflight, deleteItem, restoreItem, refresh,
} = {}) {
  const app = fs.readFileSync(appPath, "utf8");
  const eligibilityStart = app.indexOf("function itemDeleteAvailable");
  const eligibilityEnd = app.indexOf("// legacy name", eligibilityStart);
  const lifecycleStart = app.indexOf("const pendingBuildLifecycleDeletes");
  const lifecycleEnd = app.indexOf("function exportBuilds", lifecycleStart);
  assert.ok(eligibilityStart >= 0 && eligibilityEnd > eligibilityStart);
  assert.ok(lifecycleStart >= 0 && lifecycleEnd > lifecycleStart);
  const state = { builds: {
    book: build || {
      id: "book", title: "Herbal", updated_at: "item-r1",
      _record_revision: "item-r1", _representations: [],
      _available_commands: ["item.delete", "representation.attach"],
      pdf_file: "", pdf_sources: [],
    },
  }, buildSel: "book" };
  const operations = [];
  const invalidated = [];
  const statuses = [];
  const calls = { preflight: [], delete: [], restore: [], refresh: [] };
  const tombstones = new Map();
  let deleteCount = 0;
  let restoreCount = 0;
  let uuid = 0;
  const defaultPreflight = async ({ itemId }) => ({
    item_id: itemId,
    item_revision: state.builds[itemId]._record_revision,
    managed_tree_revision: `tree-r${deleteCount + 1}`,
  });
  const defaultDelete = async (args) => {
    deleteCount += 1;
    const tombstone = {
      tombstone_id: `tomb-${deleteCount}`,
      revision: `tomb-r${deleteCount}`,
      state: "deleted",
      item_id: args.itemId,
      deleted_item_revision: args.recordRevision,
      managed_tree_revision: args.managedTreeRevision,
      restored_item_revision: "",
    };
    tombstones.set(tombstone.tombstone_id, tombstone);
    return { ok: true, replayed: false, receipt: {
      action: "delete",
      operation_id: args.idempotencyKey,
      item_id: args.itemId,
      deleted_item_revision: args.recordRevision,
      restored_item_revision: "",
      managed_tree_revision: args.managedTreeRevision,
      tombstone_before_revision: "",
      tombstone,
    } };
  };
  const defaultRestore = async (args) => {
    restoreCount += 1;
    const before = tombstones.get(args.tombstoneId);
    const restoredRevision = `item-restored-${restoreCount}`;
    const tombstone = {
      ...before,
      revision: `tomb-restored-${restoreCount}`,
      state: "restored",
      restored_item_revision: restoredRevision,
    };
    return { ok: true, replayed: false, receipt: {
      action: "restore",
      operation_id: args.idempotencyKey,
      item_id: before.item_id,
      deleted_item_revision: before.deleted_item_revision,
      restored_item_revision: restoredRevision,
      managed_tree_revision: before.managed_tree_revision,
      tombstone_before_revision: args.tombstoneRevision,
      tombstone,
    } };
  };
  const context = vm.createContext({
    state,
    crypto: { randomUUID: () => `00000000-0000-4000-8000-${
      String(++uuid).padStart(12, "0")}` },
    engineClient: {
      items: {
        lifecycle: async (args) => {
          calls.preflight.push({ ...args });
          return (preflight || defaultPreflight)(args, defaultPreflight);
        },
        delete: async (args) => {
          calls.delete.push({ ...args });
          return (deleteItem || defaultDelete)(args, defaultDelete);
        },
      },
      itemTombstones: {
        restore: async (args) => {
          calls.restore.push({ ...args });
          return (restoreItem || defaultRestore)(args, defaultRestore);
        },
      },
    },
    invalidateRepresentationItemIncarnation: (id) => invalidated.push(id),
    refreshBuildEngineRecord: async (id) => {
      calls.refresh.push(id);
      if (refresh) return refresh(id, state);
      throw new Error("projection refresh unavailable");
    },
    pushOp: (label, undoFn, redoFn) => {
      operations.push({ label, undoFn, redoFn });
    },
    activeHistoryTab: () => "workbench",
    renderUpload: () => {},
    status: () => {},
    statusCrit: (message) => statuses.push(message),
    currentBuild: () => state.builds[state.buildSel] || null,
    el: () => null,
  });
  vm.runInContext(`${app.slice(eligibilityStart, eligibilityEnd)}
${app.slice(lifecycleStart, lifecycleEnd)}
this.api = { deleteBuild, deleteBuildToTombstone,
  restoreDeletedBuildFromTombstone, itemDeleteAvailable,
  updateItemLifecycleControls,
  pendingCount: () => pendingBuildLifecycleDeletes.size };`, context);
  return { api: context.api, context, state, operations, invalidated,
    statuses, calls };
}

test("build lifecycle history restores and re-deletes with new tombstones",
  async () => {
    const h = buildLifecycleUiHarness();

    await h.api.deleteBuild("workbench");
    assert.equal("book" in h.state.builds, false);
    assert.equal(h.operations.length, 1);
    assert.deepEqual(h.calls.preflight[0], { itemId: "book" });
    assert.equal(h.calls.delete[0].recordRevision, "item-r1");
    assert.equal(h.calls.delete[0].managedTreeRevision, "tree-r1");
    const firstDeleteKey = h.calls.delete[0].idempotencyKey;

    await h.operations[0].undoFn();
    assert.equal(h.state.builds.book._record_revision, "item-restored-1");
    assert.deepEqual(h.calls.restore[0], {
      tombstoneId: "tomb-1",
      tombstoneRevision: "tomb-r1",
      idempotencyKey: h.calls.restore[0].idempotencyKey,
    });
    const firstRestoreKey = h.calls.restore[0].idempotencyKey;

    await h.operations[0].redoFn();
    assert.equal("book" in h.state.builds, false);
    assert.equal(h.calls.preflight.length, 2,
      "redo obtains a new coherent lifecycle preflight");
    assert.equal(h.calls.delete[1].recordRevision, "item-restored-1");
    assert.equal(h.calls.delete[1].managedTreeRevision, "tree-r2");
    assert.notEqual(h.calls.delete[1].idempotencyKey, firstDeleteKey,
      "redo is a new delete command, not an idempotency replay");

    await h.operations[0].undoFn();
    assert.equal(h.state.builds.book._record_revision, "item-restored-2");
    assert.equal(h.calls.restore[1].tombstoneId, "tomb-2");
    assert.equal(h.calls.restore[1].tombstoneRevision, "tomb-r2");
    assert.notEqual(h.calls.restore[1].idempotencyKey, firstRestoreKey);
    assert.deepEqual(h.invalidated, ["book", "book", "book", "book"]);
    assert.deepEqual(h.calls.refresh, ["book", "book"],
      "a failed follow-up projection does not roll back valid receipts");
  });

test("ambiguous lifecycle commands retain exact operation and CAS inputs",
  async () => {
    let deleteAttempts = 0;
    let restoreAttempts = 0;
    const ambiguous = () => Object.assign(new Error("response lost"), {
      code: "network-error", status: 0, retryable: true,
    });
    const h = buildLifecycleUiHarness({
      deleteItem: async (args, confirmed) => {
        deleteAttempts += 1;
        if (deleteAttempts === 1) throw ambiguous();
        return confirmed(args);
      },
      restoreItem: async (args, confirmed) => {
        restoreAttempts += 1;
        if (restoreAttempts === 1) throw ambiguous();
        return confirmed(args);
      },
    });

    await h.api.deleteBuild("workbench");
    assert.equal(h.state.builds.book.id, "book");
    assert.equal(h.operations.length, 0);
    assert.equal(h.api.pendingCount(), 1);
    await h.api.deleteBuild("workbench");
    assert.equal(h.calls.preflight.length, 1);
    assert.deepEqual(h.calls.delete[1], h.calls.delete[0]);
    assert.equal(h.api.pendingCount(), 0);
    assert.equal(h.operations.length, 1);

    await assert.rejects(h.operations[0].undoFn, /response lost/);
    assert.equal("book" in h.state.builds, false);
    await h.operations[0].undoFn();
    assert.deepEqual(h.calls.restore[1], h.calls.restore[0]);
    assert.equal(h.state.builds.book._record_revision, "item-restored-1");
  });

test("retryable 5xx lifecycle failures retain exact delete and restore commands",
  async () => {
    let deleteAttempts = 0;
    let restoreAttempts = 0;
    const unavailable = () => Object.assign(new Error("engine unavailable"), {
      code: "http-503", status: 503, retryable: true,
    });
    const h = buildLifecycleUiHarness({
      deleteItem: async (args, confirmed) => {
        deleteAttempts += 1;
        if (deleteAttempts === 1) throw unavailable();
        return confirmed(args);
      },
      restoreItem: async (args, confirmed) => {
        restoreAttempts += 1;
        if (restoreAttempts === 1) throw unavailable();
        return confirmed(args);
      },
    });

    await h.api.deleteBuild("workbench");
    assert.equal(h.api.pendingCount(), 1);
    assert.equal(h.calls.preflight.length, 1);
    await h.api.deleteBuild("workbench");
    assert.equal(h.calls.preflight.length, 1,
      "retrying a 5xx delete does not obtain new CAS inputs");
    assert.deepEqual(h.calls.delete[1], h.calls.delete[0]);
    assert.equal(h.operations.length, 1);

    await assert.rejects(h.operations[0].undoFn, /engine unavailable/);
    await h.operations[0].undoFn();
    assert.deepEqual(h.calls.restore[1], h.calls.restore[0],
      "retrying a 5xx restore reuses tombstone CAS and operation key");
    assert.equal(h.state.builds.book._record_revision, "item-restored-1");
  });

test("lifecycle delete and restore surface concurrent state conflicts",
  async () => {
    let deleteCalls = 0;
    const stale = buildLifecycleUiHarness({
      preflight: async () => ({
        item_id: "book", item_revision: "item-r2",
        managed_tree_revision: "tree-r1",
      }),
      deleteItem: async () => { deleteCalls += 1; },
    });
    await stale.api.deleteBuild("workbench");
    assert.equal(deleteCalls, 0);
    assert.equal(stale.state.builds.book.id, "book");
    assert.equal(stale.operations.length, 0);
    assert.match(stale.statuses[0], /CONFLICT/);

    const collision = buildLifecycleUiHarness();
    await collision.api.deleteBuild("workbench");
    collision.state.builds.book = {
      id: "book", _record_revision: "other-r1",
      _available_commands: ["item.delete"],
    };
    await assert.rejects(collision.operations[0].undoFn, /recreated/);
    assert.equal(collision.calls.restore.length, 0);
    assert.equal(collision.state.builds.book._record_revision, "other-r1");
  });

test("definitive lifecycle CAS failures do not commit or reuse operation keys",
  async () => {
    const conflict = (code) => Object.assign(new Error("changed elsewhere"), {
      code, status: 409, retryable: false,
    });
    let deleteAttempts = 0;
    const deletion = buildLifecycleUiHarness({
      deleteItem: async (args, confirmed) => {
        deleteAttempts += 1;
        if (deleteAttempts === 1)
          throw conflict("managed_tree_revision_conflict");
        return confirmed(args);
      },
    });
    await deletion.api.deleteBuild("workbench");
    assert.equal(deletion.state.builds.book.id, "book");
    assert.equal(deletion.operations.length, 0);
    assert.equal(deletion.api.pendingCount(), 0);
    const rejectedDeleteKey = deletion.calls.delete[0].idempotencyKey;
    await deletion.api.deleteBuild("workbench");
    assert.equal("book" in deletion.state.builds, false);
    assert.equal(deletion.calls.preflight.length, 2);
    assert.notEqual(deletion.calls.delete[1].idempotencyKey,
      rejectedDeleteKey);

    const restoration = buildLifecycleUiHarness({
      restoreItem: async () => {
        throw conflict("tombstone_revision_conflict");
      },
    });
    await restoration.api.deleteBuild("workbench");
    await assert.rejects(restoration.operations[0].undoFn,
      (error) => error.status === 409);
    await assert.rejects(restoration.operations[0].undoFn,
      (error) => error.status === 409);
    assert.equal("book" in restoration.state.builds, false);
    assert.equal(restoration.calls.restore[1].tombstoneId,
      restoration.calls.restore[0].tombstoneId);
    assert.equal(restoration.calls.restore[1].tombstoneRevision,
      restoration.calls.restore[0].tombstoneRevision);
    assert.notEqual(restoration.calls.restore[1].idempotencyKey,
      restoration.calls.restore[0].idempotencyKey);
  });

test("item deletion eligibility follows advertised commands and fails closed",
  async () => {
    const unknown = buildLifecycleUiHarness({ build: {
      id: "book", title: "Herbal", _record_revision: "item-r1",
      _representations: [],
    } });
    const button = { disabled: false };
    unknown.context.el = (id) => id === "build-delete" ? button : null;
    unknown.api.updateItemLifecycleControls(unknown.state.builds.book);
    assert.equal(button.disabled, true);
    assert.equal(unknown.api.itemDeleteAvailable(unknown.state.builds.book),
      false);
    await unknown.api.deleteBuild("workbench");
    assert.equal(unknown.calls.preflight.length, 0);
    assert.equal(unknown.calls.delete.length, 0);
    assert.equal(unknown.operations.length, 0);
    assert.match(unknown.statuses[0], /UNAVAILABLE/);

    unknown.state.builds.book._available_commands = ["item.delete"];
    unknown.api.updateItemLifecycleControls(unknown.state.builds.book);
    assert.equal(button.disabled, false);
    assert.equal(unknown.api.itemDeleteAvailable(unknown.state.builds.book),
      true);
  });

test("representation mutation retries and later recovery reuse one operation key", async () => {
  const keys = [];
  let attempts = 0;
  const confirmed = representationResult("attach");
  const h = representationUiHarness({
    attach: async (args) => {
      keys.push(args.idempotencyKey);
      attempts += 1;
      if (attempts <= 2) {
        const error = new Error("connection dropped");
        error.code = "network-error";
        error.status = 0;
        throw error;
      }
      return confirmed;
    },
    refresh: async () => { throw new Error("still offline"); },
  });

  const first = await h.api.setBuildRepresentation(
    "book", "primary", "C:/scans/herbal.pdf",
    { intent: "attach", recordRevision: "item-r1" });
  assert.equal(first, null);
  assert.equal(h.api.pendingCount(), 1);

  const recovered = await h.api.setBuildRepresentation(
    "book", "primary", "C:/scans/herbal.pdf",
    { intent: "replace", recordRevision: "item-newer" });
  assert.equal(recovered, confirmed.receipt);
  assert.equal(new Set(keys).size, 1,
    "the uncertain attach is replayed even if refreshed state suggests replace");
  assert.equal(h.api.pendingCount(), 0);
});

test("a receipt-confirmed mutation succeeds when its follow-up refresh fails", async () => {
  const calls = [];
  const confirmed = representationResult("attach");
  const h = representationUiHarness({
    attach: async (args) => {
      calls.push(args);
      if (calls.length === 1) {
        const error = new Error("response lost after send");
        error.code = "network-error";
        error.status = 0;
        throw error;
      }
      return confirmed;
    },
    refresh: async () => { throw new Error("refresh unavailable"); },
  });

  const receipt = await h.api.setBuildRepresentation(
    "book", "primary", "C:/scans/herbal.pdf",
    { intent: "attach", recordRevision: "item-r1" });

  assert.equal(receipt, confirmed.receipt);
  assert.equal(calls.length, 2);
  assert.equal(calls[0].idempotencyKey, calls[1].idempotencyKey);
  assert.equal(h.state.builds.book._record_revision, "item-r2");
  assert.equal(h.state.builds.book.pdf_file, "C:/scans/herbal.pdf");
  assert.equal(h.state.builds.book._representations[0].revision, "source-r1");
  assert.equal(h.api.generation("book"), 1);
});

test("representation history chains exact receipt revisions through undo and redo", async () => {
  const firstSource = representationSnapshot("primary", "source-r1");
  const redoSource = representationSnapshot("primary", "source-r2");
  const initial = representationResult("attach", { after: firstSource }).receipt;
  const detachCalls = [];
  const attachCalls = [];
  const h = representationUiHarness({
    build: {
      id: "book", title: "Herbal", _record_revision: "item-r2",
      _representations: [firstSource], pdf_file: "C:/scans/herbal.pdf",
      pdf_sources: [],
    },
    detach: async (args) => {
      detachCalls.push(args);
      if (detachCalls.length > 1) {
        const error = new Error("changed elsewhere");
        error.code = "representation_revision_conflict";
        error.status = 409;
        throw error;
      }
      return representationResult("detach", {
        beforeItem: "item-r2", afterItem: "item-r3",
        before: firstSource, after: null,
      });
    },
    attach: async (args) => {
      attachCalls.push(args);
      return representationResult("attach", {
        beforeItem: "item-r3", afterItem: "item-r4", after: redoSource,
      });
    },
    refresh: async () => { throw new Error("refresh unavailable"); },
  });

  h.api.pushRepresentationOp(
    "attach source", "book", "primary", initial,
    "", "C:/scans/herbal.pdf", "workbench");
  const operation = h.operations[0];
  await operation.undoFn();
  assert.equal(detachCalls[0].recordRevision, "item-r2");
  assert.equal(detachCalls[0].representationRevision, "source-r1");

  await operation.redoFn();
  assert.equal(attachCalls[0].recordRevision, "item-r3");
  assert.equal("representationRevision" in attachCalls[0], false);
  assert.equal(attachCalls[0].representation.expected_content_sha256,
    firstSource.content_sha256);

  h.state.builds.book._record_revision = "item-concurrent";
  h.state.builds.book._representations[0].revision = "source-concurrent";
  await assert.rejects(operation.undoFn, /changed elsewhere/i);
  assert.equal(detachCalls[1].recordRevision, "item-r4");
  assert.equal(detachCalls[1].representationRevision, "source-r2");
});

test("secondary IDs are portable and attach intent never degrades into replace", async () => {
  const collided = representationSnapshot("collision", "source-r1");
  let attachCalls = 0;
  let replaceCalls = 0;
  const h = representationUiHarness({
    build: {
      id: "book", _record_revision: "item-r1",
      _representations: [collided], pdf_sources: [
        { id: "collision", path: "C:/old.pdf" },
      ],
    },
    attach: async () => {
      attachCalls += 1;
      const error = new Error("already exists");
      error.code = "representation_already_exists";
      error.status = 409;
      throw error;
    },
    replace: async () => { replaceCalls += 1; },
  });

  const id = h.api.secondaryRepresentationId(h.state.builds.book);
  assert.match(id, /^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$/);
  assert.notEqual(id.toLowerCase(), "collision");
  const result = await h.api.setBuildRepresentation(
    "book", "collision", "C:/new.pdf",
    { intent: "attach", recordRevision: "item-r1" });
  assert.equal(result, null);
  assert.equal(attachCalls, 1);
  assert.equal(replaceCalls, 0);
});

function stableJson(value) {
  const normalize = (entry) => {
    if (Array.isArray(entry)) return entry.map(normalize);
    if (!entry || typeof entry !== "object") return entry;
    return Object.fromEntries(Object.keys(entry).sort().map(
      (key) => [key, normalize(entry[key])]));
  };
  return JSON.stringify(normalize(value));
}

function metadataUiHarness(options = {}) {
  const app = fs.readFileSync(appPath, "utf8");
  const metadataStart = app.indexOf("const ITEM_EDIT_METADATA_FIELDS");
  const metadataEnd = app.indexOf("async function patchBuildRaw", metadataStart);
  const saveStart = app.indexOf("async function commitBuildMetadataFields");
  const saveEnd = app.indexOf("const pendingBuildLifecycleDeletes", saveStart);
  const verifyStart = app.indexOf("async function setVerified");
  const verifyEnd = app.indexOf("// a locked phase offers", verifyStart);
  assert.ok(metadataStart >= 0 && metadataEnd > metadataStart);
  assert.ok(saveStart >= 0 && saveEnd > saveStart);
  assert.ok(verifyStart >= 0 && verifyEnd > verifyStart);

  const classNames = new Set();
  const elements = {
    "b-title": { value: options.title || "Revised Herbal" },
    "b-subtitle": { value: options.subtitle || "New subtitle" },
    "b-year": { value: options.year || "1701" },
    "b-pdf_file": { value: options.pdfDraft || "C:/scans/original.pdf" },
    "b-ready": { classList: {
      toggle: (name, on) => on ? classNames.add(name) : classNames.delete(name),
      contains: (name) => classNames.has(name),
    } },
    "b-verified-tag": { hidden: true },
    "build-msg": { textContent: "" },
    "b-src-msg": { textContent: "" },
  };
  const initial = {
    id: "book", title: "Old Herbal", subtitle: "Old subtitle", year: "1700",
    category_ids: ["botany"], description: "Old description",
    status: "ready", published_slug: "old-herbal", ocr_active: "active.md",
    ocr_verified: "reviewed.md", ocr_quality: "good",
    capture_id: "phone-1", extra: { collection: "one" },
    images: ["capture/cover.jpg"], pdf_file: "C:/scans/original.pdf",
    pdf_sources: [{ id: "alternate", path: "C:/scans/alternate.pdf" }],
    future_extension: { shelf: 3 }, updated_at: "item-r1",
    _record_revision: "item-r1",
    _representations: [{ id: "primary", revision: "source-r1" }],
    _available_commands: ["item.update", "representation.replace"],
    ...(options.build || {}),
  };
  for (const key of options.absent || []) delete initial[key];
  const state = { buildSel: "book", builds: { book: copyJson(initial) } };
  let canonicalTitle = initial.title;
  let canonicalMetadata = {};
  for (const key of [
    "subtitle", "year", "category_ids", "description", "future_extension",
  ]) {
    if (Object.prototype.hasOwnProperty.call(initial, key))
      canonicalMetadata[key] = copyJson(initial[key]);
  }
  const revisions = [...(options.revisions || ["item-r2"] )];
  const calls = { updates: [], legacy: [], refreshes: 0, operations: [],
    statuses: [], errors: [], list: 0, workbench: 0, upload: 0,
    remarks: 0, home: 0 };
  const generations = new Map();
  let updateNumber = 0;
  let context;

  const defaultUpdate = async (args) => {
    canonicalTitle = args.patch.title;
    for (const key of args.patch.metadata_remove || []) delete canonicalMetadata[key];
    Object.assign(canonicalMetadata, copyJson(args.patch.metadata_set || {}));
    const after = revisions.shift() || `item-r${updateNumber + 2}`;
    return {
      ok: true,
      schema: "librarytool.item-mutation-receipt/1",
      replayed: updateNumber > 1,
      receipt: {
        action: "update",
        operation_id: args.idempotencyKey,
        command_sha256: "d".repeat(64),
        item_id: args.itemId,
        before_revision: args.recordRevision,
        after_revision: after,
        item: {
          id: args.itemId, revision: after, kind: "book",
          title: canonicalTitle, metadata: copyJson(canonicalMetadata),
          representations: [],
        },
        deletion: null,
      },
    };
  };
  const update = async (args) => {
    updateNumber += 1;
    calls.updates.push(copyJson(args));
    return options.update
      ? options.update(args, updateNumber, defaultUpdate)
      : defaultUpdate(args);
  };
  const refresh = async (id) => {
    calls.refreshes += 1;
    if (options.refresh) return options.refresh(id, calls.refreshes, state);
    return state.builds[id] || null;
  };
  const legacyFetch = async (url, init) => {
    calls.legacy.push({ url, init: copyJson(init) });
    if (options.fetch) return options.fetch(url, init, state, calls);
    throw new Error("unexpected legacy fetch");
  };
  const mergeCompatibility = (raw, prior) => ({
    ...raw,
    _record_revision: raw.updated_at || prior._record_revision || "",
    _representations: prior._representations || [],
    _available_commands: prior._available_commands,
  });
  context = vm.createContext({
    state,
    crypto: { randomUUID: () => `00000000-0000-4000-8000-${
      String(calls.updates.length + 1).padStart(12, "0")}` },
    engineClient: { items: { update } },
    ITEM_CREATE_NON_METADATA_FIELDS: new Set([
      "id", "item_id", "kind", "title", "created_at", "updated_at",
      "revision", "representations", "artifacts", "capture_id",
      "published_slug", "ocr_active", "ocr_verified", "ocr_quality",
      "title_pages", "thumbnail_source", "status", "pdf_file",
      "pdf_sources", "images", "extra", "representation_manifest",
    ]),
    itemCreatePendingKey: stableJson,
    buildEngineRecordGeneration: (id) => generations.get(id) || 0,
    advanceBuildEngineRecordGeneration: (id) => {
      const next = (generations.get(id) || 0) + 1;
      generations.set(id, next);
      return next;
    },
    refreshBuildEngineRecord: refresh,
    mergeBuildCompatibility: mergeCompatibility,
    fetch: legacyFetch,
    encodeURIComponent,
    el: (id) => elements[id] || null,
    catPickers: { "b-categories": { get: () => ["botany", "history"] } },
    buildDescMd: { get: () => context.description },
    description: options.description || "New description",
    buildGroupIdFor: () => "",
    buildIsDirty: () => context.metadataDirty,
    metadataDirty: options.metadataDirty !== false,
    activeHistoryTab: () => "workbench",
    currentBuild: () => state.builds[state.buildSel] || null,
    buildEditGeneration: 1,
    buildDirty: true,
    descState: { id: "book", val: "Old description" },
    pushOp: (label, undoFn, redoFn) => {
      calls.operations.push({ label, undoFn, redoFn });
      return calls.operations.length;
    },
    renderBuildsList: () => { calls.list += 1; },
    renderWorkbench: () => { calls.workbench += 1; },
    renderUpload: () => { calls.upload += 1; },
    renderRemarks: () => { calls.remarks += 1; },
    renderHome: () => { calls.home += 1; },
    status: (message) => calls.statuses.push(message),
    statusErr: (message) => calls.errors.push(message),
  });
  vm.runInContext(`let buildPatchConflict = false;
${app.slice(metadataStart, metadataEnd)}
${app.slice(saveStart, saveEnd)}
${app.slice(verifyStart, verifyEnd)}
this.api = { saveBuildFields, setVerified, runBuildMetadataUpdate,
  updateBuildPortableMetadata, patchBuild, patchBuildVerificationCompatibility,
  pendingCount: () => pendingBuildMetadataUpdates.size };`, context);
  return { api: context.api, context, calls, elements, state, classNames };
}

test("metadata Save uses only the durable portable item patch", async () => {
  const h = metadataUiHarness({
    refresh: async () => { throw new Error("semantic read unavailable"); },
  });
  const managedBefore = copyJson({
    status: h.state.builds.book.status,
    published_slug: h.state.builds.book.published_slug,
    ocr_active: h.state.builds.book.ocr_active,
    ocr_verified: h.state.builds.book.ocr_verified,
    ocr_quality: h.state.builds.book.ocr_quality,
    capture_id: h.state.builds.book.capture_id,
    extra: h.state.builds.book.extra,
    images: h.state.builds.book.images,
    pdf_file: h.state.builds.book.pdf_file,
    pdf_sources: h.state.builds.book.pdf_sources,
    _representations: h.state.builds.book._representations,
    _available_commands: h.state.builds.book._available_commands,
  });

  assert.equal(await h.api.saveBuildFields(), true);
  assert.equal(h.calls.updates.length, 1);
  const command = h.calls.updates[0];
  assert.equal(command.recordRevision, "item-r1");
  assert.deepEqual(command.patch.metadata_remove, []);
  assert.equal(command.patch.representations, null);
  assert.equal(command.patch.title, "Revised Herbal");
  assert.equal("status" in command.patch.metadata_set, false);
  assert.equal("ocr_verified" in command.patch.metadata_set, false);
  assert.equal("pdf_file" in command.patch.metadata_set, false);
  assert.equal("expect_updated_at" in command.patch, false);
  assert.equal(h.calls.legacy.length, 0);
  assert.equal(h.state.builds.book.title, "Revised Herbal");
  assert.equal(h.state.builds.book._record_revision, "item-r2");
  assert.deepEqual(copyJson({
    status: h.state.builds.book.status,
    published_slug: h.state.builds.book.published_slug,
    ocr_active: h.state.builds.book.ocr_active,
    ocr_verified: h.state.builds.book.ocr_verified,
    ocr_quality: h.state.builds.book.ocr_quality,
    capture_id: h.state.builds.book.capture_id,
    extra: h.state.builds.book.extra,
    images: h.state.builds.book.images,
    pdf_file: h.state.builds.book.pdf_file,
    pdf_sources: h.state.builds.book.pdf_sources,
    _representations: h.state.builds.book._representations,
    _available_commands: h.state.builds.book._available_commands,
  }), managedBefore);
  assert.deepEqual(copyJson(h.state.builds.book.future_extension), { shelf: 3 });
});

test("portable Workbench metadata has no raw build PATCH call site", () => {
  const app = fs.readFileSync(appPath, "utf8");
  const rawCallLines = app.split(/\r?\n/).filter((line) =>
    line.includes("patchBuildRaw(") &&
    !line.includes("function patchBuildRaw("));
  for (const line of rawCallLines) {
    assert.doesNotMatch(line, /\b(attention|category_ids|bundle|group_id)\b/);
  }

  const compatibilityStart = app.indexOf(
    "const BUILD_COMPATIBILITY_MUTATION_FIELDS");
  const compatibilityEnd = app.indexOf(
    "const pendingBuildMetadataUpdates", compatibilityStart);
  assert.ok(compatibilityStart >= 0 && compatibilityEnd > compatibilityStart);
  assert.doesNotMatch(app.slice(compatibilityStart, compatibilityEnd),
    /\b(attention|category_ids|bundle|group_id)\b/);

  const portableCallers = [
    "applyRemarkValue", "attnTargetAtHover", "clearMark", "patchBuild",
  ];
  for (const name of portableCallers) {
    const start = [
      app.indexOf(`async function ${name}(`),
      app.indexOf(`function ${name}(`),
    ].find((index) => index >= 0);
    const end = /^}\r?$/m.exec(app.slice(start));
    assert.ok(start >= 0 && end, `${name} is present`);
    assert.match(app.slice(start, start + end.index + end[0].length),
      /updateBuildPortableMetadata/);
  }
  const analyzePicker = app.slice(
    app.indexOf('makeCatPicker("an-cat-picker"'),
    app.indexOf('el("an-list").addEventListener',
      app.indexOf('makeCatPicker("an-cat-picker"')));
  assert.match(analyzePicker, /updateBuildPortableMetadata/);
});

test("attention, categories, and publish bundle use receipt-chained item updates", async () => {
  const oldBundle = {
    about: false, annotations: false, pages_text: false, translations: [],
  };
  const newBundle = {
    about: true, annotations: true, pages_text: false, translations: ["fr"],
  };
  const h = metadataUiHarness({
    build: { attention: "Old note", bundle: oldBundle },
    revisions: ["item-r2", "item-r3", "item-r4", "item-r5"],
  });

  assert.ok(await h.api.updateBuildPortableMetadata(
    "book", { attention: "Check title page" }));
  assert.equal(await h.api.patchBuild(
    "book", { category_ids: ["history"] }, "assign category", "workbench"), true);
  assert.equal(await h.api.patchBuild(
    "book", { bundle: newBundle }, "edit publish bundle", "workbench"), true);

  assert.equal(h.calls.legacy.length, 0);
  assert.deepEqual(h.calls.updates.map((call) => call.recordRevision),
    ["item-r1", "item-r2", "item-r3"]);
  assert.equal(h.calls.updates[0].patch.metadata_set.attention,
    "Check title page");
  assert.deepEqual(copyJson(h.calls.updates[1].patch.metadata_set.category_ids),
    ["history"]);
  assert.deepEqual(copyJson(h.calls.updates[2].patch.metadata_set.bundle),
    newBundle);
  assert.equal(h.calls.operations.length, 2);
  assert.equal(h.calls.remarks, 1);
  assert.equal(h.calls.home, 1);

  await h.calls.operations[1].undoFn();
  assert.equal(h.calls.updates[3].recordRevision, "item-r4");
  assert.deepEqual(copyJson(h.calls.updates[3].patch.metadata_set.bundle),
    oldBundle);
});

test("portable one-click metadata retains retry identity and rejects stale undo", async () => {
  let h;
  h = metadataUiHarness({
    revisions: ["item-r2", "item-r3"],
    update: async (args, number, commit) => {
      if (number <= 2) {
        const error = new Error("response lost");
        error.code = "network-error";
        error.status = 0;
        throw error;
      }
      if (number === 5) {
        const error = new Error("changed elsewhere");
        error.code = "item_revision_conflict";
        error.status = 409;
        throw error;
      }
      return commit(args);
    },
  });

  assert.equal(await h.api.updateBuildPortableMetadata(
    "book", { attention: "Check binding" }), null);
  assert.equal(h.api.pendingCount(), 1);
  assert.ok(await h.api.updateBuildPortableMetadata(
    "book", { attention: "Check binding" }));
  assert.equal(new Set(h.calls.updates.slice(0, 3).map(
    (call) => call.idempotencyKey)).size, 1);
  assert.equal(h.api.pendingCount(), 0);

  assert.equal(await h.api.patchBuild(
    "book", { category_ids: ["history"] }, "assign category", "workbench"), true);
  const operation = h.calls.operations[0];
  await assert.rejects(operation.undoFn, /Item changed elsewhere/);
  assert.equal(h.calls.updates[4].recordRevision, "item-r3");
  assert.equal(h.calls.updates.length, 5,
    "a stale inverse is not rebased or resent");
});

test("metadata response loss replays one exact command", async () => {
  const h = metadataUiHarness({
    update: async (args, number, commit) => {
      if (number <= 2) {
        const error = new Error("response lost");
        error.code = "network-error";
        error.status = 0;
        throw error;
      }
      return commit(args);
    },
  });
  assert.equal(await h.api.saveBuildFields(), false);
  assert.equal(h.api.pendingCount(), 1);
  assert.equal(h.elements["build-msg"].textContent,
    "Save status unknown — retry to confirm");
  assert.equal(await h.api.saveBuildFields(), true);
  assert.equal(h.calls.updates.length, 3);
  assert.equal(new Set(h.calls.updates.map((call) => call.idempotencyKey)).size, 1);
  assert.equal(new Set(h.calls.updates.map((call) => stableJson(call.patch))).size, 1);
  assert.equal(h.api.pendingCount(), 0);
  assert.equal(h.calls.operations.length, 1);
});

test("stale metadata Save does not rebase or overwrite a newer draft", async () => {
  let h;
  h = metadataUiHarness({
    update: async () => {
      h.elements["b-title"].value = "Newest local draft";
      h.context.buildEditGeneration += 1;
      const error = new Error("changed elsewhere");
      error.status = 409;
      error.code = "item_revision_conflict";
      throw error;
    },
  });
  assert.equal(await h.api.saveBuildFields(), false);
  assert.equal(h.calls.updates.length, 1);
  assert.equal(h.calls.refreshes, 0);
  assert.equal(h.state.builds.book.title, "Old Herbal");
  assert.equal(h.elements["b-title"].value, "Newest local draft");
  assert.equal(h.elements["build-msg"].textContent,
    "changed elsewhere — your edits are still here");
  assert.equal(h.calls.operations.length, 0);
});

test("metadata Save leaves a PDF source draft unattached", async () => {
  const h = metadataUiHarness({
    pdfDraft: "C:/scans/new.pdf", metadataDirty: false,
  });
  assert.equal(await h.api.saveBuildFields(), false);
  assert.equal(h.calls.updates.length, 0);
  assert.equal(h.elements["b-src-msg"].textContent, "Not attached");

  h.context.metadataDirty = true;
  assert.equal(await h.api.saveBuildFields(), true);
  assert.equal("pdf_file" in h.calls.updates[0].patch.metadata_set, false);
  assert.equal(h.state.builds.book.pdf_file, "C:/scans/original.pdf");
  assert.equal(h.elements["b-pdf_file"].value, "C:/scans/new.pdf");
  assert.equal(h.elements["b-src-msg"].textContent, "Not attached");

  const app = fs.readFileSync(appPath, "utf8");
  const listenerStart = app.indexOf('el("build-form").addEventListener("input"');
  const listenerEnd = app.indexOf('el("build-save").addEventListener', listenerStart);
  assert.match(app.slice(listenerStart, listenerEnd),
    /ev\.target\.id === "b-pdf_file"\) return/);
});

test("metadata history retries exactly and chains receipt revisions", async () => {
  const h = metadataUiHarness({
    revisions: ["item-r2", "item-r3", "item-r4"],
    update: async (args, number, commit) => {
      if (number === 2 || number === 3) {
        const error = new Error("response lost");
        error.code = "network-error";
        error.status = 0;
        throw error;
      }
      return commit(args);
    },
  });
  assert.equal(await h.api.saveBuildFields(), true);
  const operation = h.calls.operations[0];
  await assert.rejects(operation.undoFn, /Metadata update failed/);
  await operation.undoFn();
  await operation.redoFn();

  assert.equal(h.calls.updates[1].recordRevision, "item-r2");
  assert.equal(h.calls.updates[2].recordRevision, "item-r2");
  assert.equal(h.calls.updates[3].recordRevision, "item-r2");
  assert.equal(new Set(h.calls.updates.slice(1, 4).map(
    (call) => call.idempotencyKey)).size, 1);
  assert.equal(h.calls.updates[3].patch.title, "Old Herbal");
  assert.equal(h.calls.updates[4].recordRevision, "item-r3");
  assert.equal(h.calls.updates[4].patch.title, "Revised Herbal");
});

test("metadata history restores an originally absent field", async () => {
  const h = metadataUiHarness({
    absent: ["subtitle"], revisions: ["item-r2", "item-r3"],
  });
  assert.equal(await h.api.saveBuildFields(), true);
  await h.calls.operations[0].undoFn();
  assert.deepEqual(copyJson(h.calls.updates[1].patch.metadata_remove),
    ["subtitle"]);
  assert.equal("subtitle" in h.calls.updates[1].patch.metadata_set, false);
  assert.equal(Object.prototype.hasOwnProperty.call(
    h.state.builds.book, "subtitle"), false);
});

test("a committed receipt cannot clean a superseded local projection", async () => {
  let h;
  h = metadataUiHarness({
    update: async (args, _number, commit) => {
      h.context.advanceBuildEngineRecordGeneration("book");
      h.state.builds.book = {
        ...h.state.builds.book,
        title: "Concurrent projection",
        _record_revision: "item-concurrent",
        updated_at: "item-concurrent",
      };
      return commit(args);
    },
  });
  const outcome = await h.api.saveBuildFields(null, { returnOutcome: true });
  assert.equal(outcome.ok, true, "the valid receipt remains the commit point");
  assert.equal(outcome.adopted, false);
  assert.equal(outcome.projectionCurrent, false);
  assert.equal(h.context.buildDirty, true);
  assert.equal(h.state.builds.book.title, "Concurrent projection");
  assert.equal(h.elements["build-msg"].textContent,
    "Earlier edits saved — newer edits are not saved");
  assert.equal(h.calls.refreshes, 1);
});

test("verification saves metadata then patches only workflow compatibility", async () => {
  const h = metadataUiHarness({
    build: { status: "draft", ocr_active: "active.md", ocr_verified: "" },
    fetch: async (_url, init, state) => {
      const body = JSON.parse(init.body);
      return response(200, { ok: true, build: {
        ...copyJson(state.builds.book), ...body,
        id: "book", updated_at: "item-r3",
      } });
    },
  });
  assert.equal(await h.api.setVerified(true), true);
  assert.equal(h.calls.updates.length, 1);
  assert.equal("status" in h.calls.updates[0].patch.metadata_set, false);
  assert.equal("ocr_verified" in h.calls.updates[0].patch.metadata_set, false);
  assert.equal(h.calls.legacy.length, 1);
  assert.deepEqual(JSON.parse(h.calls.legacy[0].init.body), {
    status: "ready", ocr_verified: "active.md",
    expect_updated_at: "item-r2",
  });
  assert.equal(h.state.builds.book.status, "ready");
  assert.equal(h.state.builds.book.ocr_verified, "active.md");
});

test("verification preserves uploaded state and reports partial success", async () => {
  const uploaded = metadataUiHarness({
    build: { status: "uploaded", ocr_active: "", ocr_verified: "" },
  });
  assert.equal(await uploaded.api.setVerified(false), true);
  assert.equal(uploaded.calls.legacy.length, 0);
  assert.equal(uploaded.state.builds.book.status, "uploaded");

  const partial = metadataUiHarness({
    build: { status: "draft", ocr_active: "", ocr_verified: "" },
    fetch: async () => { throw new Error("workflow service offline"); },
  });
  assert.equal(await partial.api.setVerified(true), false);
  assert.equal(partial.state.builds.book.title, "Revised Herbal");
  assert.equal(partial.state.builds.book._record_revision, "item-r2");
  assert.equal(partial.elements["build-msg"].textContent,
    "Metadata saved; verification was not changed");
  assert.match(partial.calls.errors.at(-1), /METADATA SAVED/);
});

test("verification recognizes a lost committed compatibility response", async () => {
  const h = metadataUiHarness({
    build: { status: "draft", ocr_active: "", ocr_verified: "" },
    fetch: async () => { throw new Error("response lost"); },
    refresh: async (id, number, state) => {
      if (number === 2) {
        state.builds[id] = {
          ...state.builds[id], status: "ready", updated_at: "item-r3",
          _record_revision: "item-r3",
        };
      }
      return state.builds[id];
    },
  });
  assert.equal(await h.api.setVerified(true), true);
  assert.equal(h.calls.legacy.length, 1);
  assert.equal(h.calls.refreshes, 2);
  assert.equal(h.state.builds.book.status, "ready");
});

test("build creation strips legacy sources and attaches before history or selection", async () => {
  const app = fs.readFileSync(appPath, "utf8");
  const start = app.indexOf("const ITEM_CREATE_NON_METADATA_FIELDS");
  const end = app.indexOf("function buildSeedFromSource", start);
  assert.ok(start >= 0 && end > start);
  const events = [];
  const messages = { "b-src-msg": { textContent: "" } };
  const state = { builds: {} };
  const compatibilityBuild = {
    id: "new-book", title: "Seeded Herbal", status: "draft",
    subtitle: "A field guide", category_ids: ["plants"],
    future_extension: { shelf: 3 },
    bundle: { about: true, annotations: false, pages_text: false,
      translations: [] },
    notes: "concurrent catalogue edit",
    pdf_file: "", pdf_sources: [],
    extra: { scan_collection_id: "collection-1", shelf: "A" },
    images: ["capture/cover.jpg"], capture_id: "phone-1",
    updated_at: "item-r1c",
  };
  const concurrentBuild = {
    ...copyJson(compatibilityBuild),
    extra: {}, images: [], capture_id: "", updated_at: "item-r1u",
  };
  let compatibilityAttempts = 0;
  const context = vm.createContext({
    state,
    crypto: { randomUUID: () => "create-uuid" },
    engineClient: { items: {
      create: async (args) => {
        events.push({ type: "command", args: JSON.parse(JSON.stringify(args)) });
        return itemCreateResult(args);
      },
      seedCompatibility: async (args) => {
        compatibilityAttempts += 1;
        events.push({ type: "compatibility", args: copyJson(args) });
        if (compatibilityAttempts === 1) {
          const conflict = new Error("concurrent catalogue edit");
          conflict.status = 409;
          conflict.body = { build: copyJson(concurrentBuild) };
          throw conflict;
        }
        if (compatibilityAttempts === 2) {
          const error = new Error("response lost after compatibility commit");
          error.code = "network-error";
          error.status = 0;
          error.retryable = true;
          throw error;
        }
        const conflict = new Error("revision changed");
        conflict.status = 409;
        conflict.body = { build: copyJson(compatibilityBuild) };
        throw conflict;
      },
    } },
    setBuildRepresentation: async (itemId, sourceId, sourcePath, options) => {
      events.push({ type: "representation", itemId, sourceId, sourcePath,
        options: { ...options } });
      if (sourceId !== "primary") return null;
      const after = representationSnapshot("primary", "source-r1");
      state.builds[itemId].pdf_file = sourcePath;
      state.builds[itemId]._representations = [after];
      state.builds[itemId]._record_revision = "item-r2";
      return representationResult("attach", {
        itemId, beforeItem: "item-r1", afterItem: "item-r2", after,
      }).receipt;
    },
    invalidateRepresentationItemIncarnation: (itemId) => {
      events.push({ type: "incarnation", itemId });
    },
    mergeBuildCompatibility: (raw, prior) => ({
      ...raw,
      _record_revision: raw.updated_at || prior._record_revision || "",
      _representations: prior._representations || [],
      _available_commands: prior._available_commands,
    }),
    refreshBuildEngineRecord: async (itemId) => {
      events.push({ type: "semantic", itemId });
      state.builds[itemId]._available_commands = [
        "representation.attach", "representation.replace", "representation.detach",
      ];
      return state.builds[itemId];
    },
    secondaryRepresentationId: () => "s1-secondary",
    representationFailureMessage: () => "Source update rejected",
    pushOp: () => {
      events.push({ type: "history", snapshot: {
        ...state.builds["new-book"],
      } });
    },
    selectBuild: (id) => events.push({ type: "select", id }),
    renderUpload: () => events.push({ type: "render" }),
    statusCrit: (message) => events.push({ type: "critical", message }),
    el: (id) => messages[id] || null,
  });
  vm.runInContext(`${app.slice(start, end)}
this.createBuild = createBuild;`, context);

  const result = await context.createBuild({
    title: "  Seeded Herbal  ",
    subtitle: "  A field guide  ",
    category_ids: ["plants"],
    future_extension: { shelf: 3 },
    bundle: { about: true, annotations: false, pages_text: false,
      translations: [] },
    pdf_file: "C:/scans/primary.pdf",
    pdf_sources: [{ id: "scan", path: "C:/scans/alternate.pdf" }],
    status: "ready",
    images: ["capture/cover.jpg"],
    extra: { scan_collection_id: "collection-1", shelf: " A " },
    capture_id: "phone-1",
  }, "seeded", "workbench");

  const command = events.find((event) => event.type === "command").args;
  assert.equal(command.idempotencyKey, "item-create-create-uuid");
  assert.deepEqual(command.item, {
    kind: "book",
    title: "Seeded Herbal",
    metadata: {
      subtitle: "A field guide",
      category_ids: ["plants"],
      future_extension: { shelf: 3 },
      bundle: { about: true, annotations: false, pages_text: false,
        translations: [] },
    },
    representations: [],
  });
  for (const managed of [
    "pdf_file", "pdf_sources", "status", "images", "extra", "capture_id",
  ]) assert.equal(managed in command.item.metadata, false);
  const mutationEvents = events.filter((event) =>
    event.type === "representation");
  assert.equal(mutationEvents.length, 2);
  assert.equal(mutationEvents[0].options.intent, "attach");
  assert.equal(mutationEvents[0].options.recordRevision, "item-r1c");
  assert.equal(mutationEvents[1].options.recordRevision, "item-r2");
  assert.ok(events.findIndex((event) => event.type === "command") <
    events.findIndex((event) => event.type === "compatibility"));
  assert.ok(events.findIndex((event) => event.type === "compatibility") <
    events.findIndex((event) => event.type === "semantic"));
  assert.ok(events.findIndex((event) => event.type === "semantic") <
    events.findIndex((event) => event.type === "representation"));
  assert.ok(events.findIndex((event) => event.type === "representation") <
    events.findIndex((event) => event.type === "history"));
  assert.ok(events.findIndex((event) => event.type === "history") <
    events.findIndex((event) => event.type === "select"));
  const creation = app.slice(start, end);
  assert.match(creation, /deleteBuildToTombstone/);
  assert.match(creation, /restoreDeletedBuildFromTombstone/);
  assert.doesNotMatch(creation, /deleteBuildToTrash|\/api\/trash/);
  assert.doesNotMatch(creation, /fetch\(["']\/api\/builds/);
  assert.match(creation, /engineClient\.items\.create/);
  assert.match(creation, /engineClient\.items\.seedCompatibility/);
  assert.equal(result.pdf_file, "C:/scans/primary.pdf");
  assert.deepEqual(result.extra,
    { scan_collection_id: "collection-1", shelf: "A" });
  assert.deepEqual(result.images, ["capture/cover.jpg"]);
  assert.equal(result.capture_id, "phone-1");
  assert.equal(result.status, "draft");
  assert.equal(result.notes, "concurrent catalogue edit");
  assert.deepEqual(copyJson(result.bundle), {
    about: true, annotations: false, pages_text: false, translations: [],
  });
  assert.equal(compatibilityAttempts, 3);
  assert.deepEqual(events.filter((event) => event.type === "compatibility")
    .map((event) => event.args.recordRevision),
  ["item-r1", "item-r1u", "item-r1u"]);
  assert.equal(messages["b-src-msg"].textContent,
    "Source update rejected");
  assert.ok(events.some((event) => event.type === "critical" &&
    /ATTACH FAILED/.test(event.message)));
  assert.equal(events.some((event) =>
    /ATTACHED|SOURCE ADDED/.test(event.message || "")), false);
});

test("build creation retains one durable command across ambiguous retries", async () => {
  const app = fs.readFileSync(appPath, "utf8");
  const start = app.indexOf("const ITEM_CREATE_NON_METADATA_FIELDS");
  const end = app.indexOf("function buildSeedFromSource", start);
  assert.ok(start >= 0 && end > start);
  const state = { builds: {} };
  const calls = [];
  const statuses = [];
  let confirm = false;
  const context = vm.createContext({
    state,
    crypto: { randomUUID: () => "lost-response" },
    engineClient: { items: { create: async (args) => {
      calls.push(args);
      if (!confirm) {
        const error = new Error("connection closed after commit");
        error.code = "network-error";
        error.status = 0;
        error.retryable = true;
        throw error;
      }
      return itemCreateResult(args, { replayed: true });
    } } },
    invalidateRepresentationItemIncarnation: () => {},
    refreshBuildEngineRecord: async () => {
      throw new Error("compatibility projection unavailable");
    },
    setBuildRepresentation: async () => {
      throw new Error("no source attachment expected");
    },
    secondaryRepresentationId: () => "secondary",
    representationFailureMessage: () => "failed",
    pushOp: () => {},
    selectBuild: () => {},
    renderUpload: () => {},
    statusCrit: (message) => statuses.push(message),
    el: () => null,
  });
  vm.runInContext(`${app.slice(start, end)}
this.createBuild = createBuild;`, context);

  const seed = { title: "Recovered Create", notes: "same command" };
  assert.equal(await context.createBuild(seed, "lost", "workbench"), null);
  assert.equal(calls.length, 2);
  confirm = true;
  const created = await context.createBuild({
    notes: "same command", title: "Recovered Create",
  }, "lost", "workbench");

  assert.equal(calls.length, 3);
  assert.equal(new Set(calls.map((args) => args.idempotencyKey)).size, 1);
  assert.equal(calls[0].idempotencyKey, "item-create-lost-response");
  assert.equal(created.id, "new-book");
  assert.equal(created._record_revision, "item-r1");
  assert.equal(created.notes, "same command");
  assert.equal(Object.keys(state.builds).length, 1);
  assert.deepEqual(statuses, ["BUILD CREATE FAILED"]);
});

test("pending create identity includes sources and acquisition provenance", async () => {
  const app = fs.readFileSync(appPath, "utf8");
  const start = app.indexOf("const ITEM_CREATE_NON_METADATA_FIELDS");
  const end = app.indexOf("function buildSeedFromSource", start);
  assert.ok(start >= 0 && end > start);
  const calls = [];
  let sequence = 0;
  const context = vm.createContext({
    state: { builds: {} },
    crypto: { randomUUID: () => `intent-${++sequence}` },
    engineClient: { items: { create: async (args) => {
      calls.push(args);
      const error = new Error("ambiguous create");
      error.code = "network-error";
      error.status = 0;
      error.retryable = true;
      throw error;
    } } },
    statusCrit: () => {},
  });
  vm.runInContext(`${app.slice(start, end)}
this.createBuild = createBuild;`, context);

  const metadata = { title: "Same Catalogue Record", authors: "A. Author" };
  await context.createBuild({
    ...metadata, pdf_file: "scans/copy-a.pdf",
    extra: { scan_collection_id: "collection-a" },
  }, "copy a", "workbench");
  await context.createBuild({
    ...metadata, pdf_file: "scans/copy-b.pdf",
    extra: { scan_collection_id: "collection-a" },
  }, "copy b", "workbench");
  await context.createBuild({
    ...metadata, pdf_file: "scans/copy-b.pdf",
    extra: { scan_collection_id: "collection-b" },
  }, "copy b provenance", "workbench");

  assert.equal(calls.length, 6);
  const keys = calls.map((args) => args.idempotencyKey);
  assert.deepEqual(keys, [
    "item-create-intent-1", "item-create-intent-1",
    "item-create-intent-2", "item-create-intent-2",
    "item-create-intent-3", "item-create-intent-3",
  ]);
  assert.ok(calls.every((args) =>
    !Object.hasOwn(args.item.metadata, "extra") &&
    !Object.hasOwn(args.item.metadata, "pdf_file")));
});

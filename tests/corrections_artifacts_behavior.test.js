const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const {
  createDefaultEditorRegistry,
} = require("../tools/whl_explorer/static/corrections/editor-registry");
const {
  ARTIFACT_EDITOR_IDS,
  registerArtifactEditors,
} = require("../tools/whl_explorer/static/corrections/artifact-editors");
const {
  createArtifactsFeature,
} = require("../tools/whl_explorer/static/corrections/artifacts");
const {
  FakeNode,
  deferred,
  fakeDocument,
} = require("./fixtures/corrections_fake_dom");


const repositoryRoot = path.join(__dirname, "..");
const artifactsSource = fs.readFileSync(path.join(
  repositoryRoot, "tools", "whl_explorer", "static", "corrections", "artifacts.js"), "utf8");
const artifactStyles = fs.readFileSync(path.join(
  repositoryRoot, "tools", "whl_explorer", "static", "corrections", "artifacts.css"), "utf8");


function raster(id, kind = "captured-image", overrides = {}) {
  return {
    key: { item_id: "book-1", artifact_id: id },
    revision: `${id}-r1`,
    kind,
    label: id.replaceAll("-", " "),
    media_type: "image/jpeg",
    resource_state: "available",
    resource: {
      resource_id: `${id}-resource`,
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

function documentArtifact(
  id,
  kind = "generated-metadata",
  overrides = {},
) {
  return {
    schema: "librarytool.document-artifact/1",
    key: { item_id: "book-1", artifact_id: id },
    artifact_id: id,
    object_type: "document-artifact",
    revision: `${id}-r1`,
    kind,
    label: id.replaceAll("-", " "),
    language: "",
    media_type: "application/json",
    resource_state: "available",
    resource: {
      id: `docres-${id}`,
      revision: `${id}-resource-r1`,
      variant: "text",
    },
    freshness: "current",
    source: {
      representation_id: "capture-1",
      representation_revision: "capture-r1",
    },
    metadata: {},
    ...overrides,
  };
}


function annotation(id, linkedArtifactId = "") {
  return {
    key: { item_id: "book-1", annotation_id: id },
    revision: `${id}-r1`,
    kind: "spatial-annotation",
    label: id.replaceAll("-", " "),
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
      points: [{ x: 0.1, y: 0.1 }, { x: 0.3, y: 0.1 }, { x: 0.3, y: 0.3 }],
    },
    linked_artifact_ids: linkedArtifactId ? [linkedArtifactId] : [],
    role_assignments: [{
      origin: "machine",
      revision: "role-r1",
      role: "figure",
      confidence: 0.8,
    }],
    effective_role: "figure",
    provenance: { origin: "ocr", provider_id: "mistral", model: "pixtral" },
  };
}


function harness(options = {}) {
  const documentRef = fakeDocument();
  const treeRoot = new FakeNode("div", documentRef);
  treeRoot.clientHeight = options.clientHeight || 280;
  const published = [];
  const selections = [];
  const hotTargets = [];
  const statuses = [];
  const feature = createArtifactsFeature({
    treeRoot,
    documentRef,
    catalog: options.catalog,
    resources: options.resources,
    commands: options.commands,
    initialExpandedGroups: options.initialExpandedGroups,
    rowHeight: options.rowHeight || 28,
    overscan: options.overscan == null ? 2 : options.overscan,
    pageLimit: options.pageLimit || 2,
    objectUrls: options.objectUrls,
    onResource: (resource) => published.push(resource),
    onSelection: (selection) => selections.push(selection),
    onHotTarget: (target) => hotTargets.push(target),
    onStatus: (...status) => statuses.push(status),
  }).mount();
  return {
    documentRef,
    feature,
    hotTargets,
    published,
    selections,
    statuses,
    treeRoot,
  };
}


test("tree groups load lazily, page on demand, and remain keyboard navigable", async () => {
  const calls = [];
  const pages = {
    "source-images": [
      { items: [raster("capture-1")], nextCursor: "source-page-2" },
      { items: [raster("capture-2")], nextCursor: null },
    ],
    "ocr-text": [{ items: [{
      key: { item_id: "book-1", artifact_id: "ocr-1" },
      revision: "ocr-r1",
      kind: "ocr",
      label: "OCR text",
      media_type: "text/plain",
      resource_state: "unavailable",
    }], nextCursor: null }],
  };
  const { feature, treeRoot } = harness({
    initialExpandedGroups: ["source-images"],
    catalog: {
      async list({ group, cursor, limit }) {
        calls.push({ group, cursor, limit });
        const index = cursor ? 1 : 0;
        return { revision: `${group}-inventory-r1`, ...(pages[group] || [{ items: [] }])[index] };
      },
      async get({ key }) {
        return key === "artifact:capture-1" ? raster("capture-1") : raster("capture-2");
      },
    },
    resources: {
      async resolveRaster() { return { url: "/safe/display.jpg" }; },
      async readText() { throw new Error("not used"); },
      async listRegions() { throw new Error("not used"); },
    },
  });

  await feature.setContext({ item_id: "book-1" });
  assert.deepEqual(calls, [{ group: "source-images", cursor: null, limit: 2 }]);
  assert.ok(feature.rows.some((row) => row.key === "more:source-images"));
  assert.equal(treeRoot.getAttribute("role"), "tree");
  assert.equal(treeRoot.getAttribute("tabindex"), "0");
  assert.ok(treeRoot.getAttribute("aria-activedescendant"));

  await feature.loadGroup("source-images");
  assert.deepEqual(calls[1], {
    group: "source-images",
    cursor: "source-page-2",
    limit: 2,
  });
  assert.equal(feature.groupState("source-images").items.length, 2);
  assert.equal(feature.rows.some((row) => row.key === "more:source-images"), false);

  await feature.toggleGroup("ocr-text", true);
  assert.equal(calls[2].group, "ocr-text");
  feature.activeKey = "group:source-images";
  const down = {
    key: "ArrowDown",
    prevented: false,
    preventDefault() { this.prevented = true; },
  };
  await feature.handleKeydown(down);
  assert.equal(down.prevented, true);
  assert.notEqual(feature.activeKey, "group:source-images");
  assert.ok(treeRoot.getAttribute("aria-activedescendant"));
});


test("read-only artifact composition preserves missing caption capabilities", () => {
  const documentRef = fakeDocument();
  const treeRoot = new FakeNode("div", documentRef);
  const propertiesRoot = new FakeNode("dl", documentRef);
  const feature = createArtifactsFeature({
    treeRoot,
    propertiesRoot,
    documentRef,
  }).mount();

  assert.deepEqual(feature.properties.captionCapabilities, {
    set: false,
    clear: false,
    undo: false,
  });
  feature.destroy();
});


test("context and selection generations discard stale results and abort prior work", async () => {
  const first = deferred();
  const second = deferred();
  const signals = [];
  const { feature } = harness({
    initialExpandedGroups: ["source-images"],
    catalog: {
      list({ context, signal }) {
        signals.push(signal);
        return context.itemId === "book-old" ? first.promise : second.promise;
      },
      async get() { throw new Error("not used"); },
    },
  });

  const oldContext = feature.setContext({ item_id: "book-old" });
  const newContext = feature.setContext({ item_id: "book-new" });
  assert.equal(signals[0].aborted, true);
  second.resolve({ items: [raster("capture-new", "captured-image", {
    key: { item_id: "book-new", artifact_id: "capture-new" },
  })] });
  await newContext;
  first.resolve({ items: [raster("capture-old", "captured-image", {
    key: { item_id: "book-old", artifact_id: "capture-old" },
  })] });
  await oldContext;

  assert.equal(feature.items.has("artifact:capture-new"), true);
  assert.equal(feature.items.has("artifact:capture-old"), false);
  assert.equal(feature.context.itemId, "book-new");
});


test("navigation previews stay non-authoritative while deferred detail remains stale-safe",
  async () => {
    const firstDetail = deferred();
    const secondDetail = deferred();
    const rasterStarted = deferred();
    const rasterResponse = deferred();
    const detailRequests = [];
    const rasterRequests = [];
    const { feature, published, selections } = harness({
      initialExpandedGroups: [],
      catalog: {
        async list() { return { items: [] }; },
        get({ key, signal }) {
          detailRequests.push({ key, signal });
          return key === "artifact:capture-1"
            ? firstDetail.promise : secondDetail.promise;
        },
      },
      resources: {
        async resolveRaster({ artifactId }) {
          rasterRequests.push(artifactId);
          rasterStarted.resolve();
          return rasterResponse.promise;
        },
      },
    });
    const preview = (artifactId, canvasId) => ({
      itemId: "book-1",
      representationId: "scan-1",
      canvasId,
      artifactId,
      url: `/thumb/${artifactId}.jpg`,
      label: artifactId,
    });

    const firstContext = feature.setContext({
      item_id: "book-1",
      representation_id: "scan-1",
      canvas_id: "page-1",
      artifact_id: "capture-1",
      navigationPreview: preview("capture-1", "page-1"),
    });
    const firstPreview = feature.currentResource;
    assert.equal(firstPreview.url, "/thumb/capture-1.jpg");
    assert.equal(firstPreview.navigationOnly, true);
    assert.equal(feature.getCommandTarget(), null);
    assert.deepEqual(selections, [null],
      "preview publication must not emit an artifact selection");
    for (const field of [
      "revision", "resourceRef", "correction", "requestFull", "summary",
    ]) {
      assert.equal(Object.hasOwn(firstPreview, field), false,
        `preview must not expose ${field}`);
    }

    const secondContext = feature.setContext({
      item_id: "book-1",
      representation_id: "scan-1",
      canvas_id: "page-2",
      artifact_id: "capture-2",
      navigationPreview: preview("capture-2", "page-2"),
    });
    const secondPreview = feature.currentResource;
    assert.equal(secondPreview.url, "/thumb/capture-2.jpg");
    assert.equal(feature.getCommandTarget(), null);
    assert.equal(detailRequests[0].signal.aborted, true);

    firstDetail.resolve(raster("capture-1"));
    assert.equal(await firstContext, null);
    assert.equal(feature.currentResource, secondPreview,
      "a superseded detail response must not replace the current preview");
    assert.deepEqual(rasterRequests, []);

    secondDetail.resolve(raster("capture-2", "captured-image", {
      source: {
        representation_id: "scan-1",
        representation_revision: "scan-r1",
        canvas_id: "page-2",
        canvas_revision: "page-2-r1",
      },
    }));
    await rasterStarted.promise;
    assert.equal(feature.currentResource, secondPreview,
      "the matching preview must remain visible while the display raster resolves");
    assert.equal(feature.getCommandTarget().id, "capture-2",
      "only hydrated detail, not the preview, becomes a command target");
    rasterResponse.resolve({ url: "/resolved/capture-2.jpg" });
    await secondContext;

    assert.equal(feature.currentResource.url, "/resolved/capture-2.jpg");
    assert.notEqual(feature.currentResource, secondPreview);
    assert.notEqual(feature.currentResource.navigationOnly, true);
    assert.equal(feature.currentResource.summary.revision, "capture-2-r1");
    assert.equal(typeof feature.currentResource.requestFull, "function");
    assert.equal(feature.getCommandTarget().id, "capture-2");
    assert.equal(selections.at(-1).id, "capture-2");
    assert.deepEqual(rasterRequests, ["capture-2"]);
  });


test("navigation previews reject mismatched addresses and unsafe raster URLs", async () => {
  const firstDetail = deferred();
  const secondDetail = deferred();
  const { feature, published } = harness({
    initialExpandedGroups: [],
    catalog: {
      async list() { return { items: [] }; },
      get({ key }) {
        return key === "artifact:capture-1"
          ? firstDetail.promise : secondDetail.promise;
      },
    },
    resources: {
      async resolveRaster({ artifactId }) {
        return { url: `/resolved/${artifactId}.jpg` };
      },
    },
  });
  const basePreview = {
    itemId: "book-1",
    representationId: "scan-1",
    canvasId: "page-1",
    artifactId: "capture-1",
    url: "/thumb/capture-1.jpg",
    label: "Capture 1",
  };

  const mismatched = feature.setContext({
    item_id: "book-1",
    representation_id: "scan-1",
    canvas_id: "page-1",
    artifact_id: "capture-1",
    navigationPreview: { ...basePreview, itemId: "book-other" },
  });
  assert.equal(feature.currentResource, null);

  const unsafe = feature.setContext({
    item_id: "book-1",
    representation_id: "scan-1",
    canvas_id: "page-2",
    artifact_id: "capture-2",
    navigationPreview: {
      ...basePreview,
      canvasId: "page-2",
      artifactId: "capture-2",
      url: "javascript:alert(1)",
    },
  });
  assert.equal(feature.currentResource, null);
  assert.equal(published.some((resource) =>
    resource && resource.navigationOnly === true), false);

  firstDetail.resolve(raster("capture-1"));
  assert.equal(await mismatched, null);
  secondDetail.resolve(raster("capture-2", "captured-image", {
    source: {
      representation_id: "scan-1",
      canvas_id: "page-2",
      canvas_revision: "page-2-r1",
    },
  }));
  await unsafe;
  assert.equal(feature.currentResource.url, "/resolved/capture-2.jpg");
});


test("superseded deep links cannot start group loads against a newer context",
  async () => {
    const oldDetail = deferred();
    const listCalls = [];
    const { feature } = harness({
      initialExpandedGroups: ["source-images"],
      catalog: {
        async list({ context }) {
          listCalls.push(context.itemId);
          return { items: [] };
        },
        get({ context }) {
          if (context.itemId === "book-old") return oldDetail.promise;
          return Promise.resolve(raster("capture-new", "captured-image", {
            key: { item_id: "book-new", artifact_id: "capture-new" },
          }));
        },
      },
    });

    const oldContext = feature.setContext({
      item_id: "book-old", artifact_id: "capture-old",
    });
    const newContext = feature.setContext({ item_id: "book-new" });
    await newContext;
    oldDetail.resolve(raster("capture-old", "captured-image", {
      key: { item_id: "book-old", artifact_id: "capture-old" },
    }));
    await oldContext;

    assert.deepEqual(listCalls, ["book-new"]);
    assert.equal(feature.context.itemId, "book-new");
  });


test("failed deep links retain their error status", async () => {
  const { feature, statuses } = harness({
    initialExpandedGroups: ["source-images"],
    catalog: {
      async list() { return { items: [] }; },
      async get() { throw new Error("capture detail unavailable"); },
    },
  });

  await feature.setContext({
    item_id: "book-1", artifact_id: "missing-capture",
  });
  await Promise.resolve();

  assert.ok(statuses.some(([message, error]) =>
    message === "capture detail unavailable" && error === true));
  assert.equal(statuses.some(([message, error]) =>
    error === false && /(?:Selected artifact|Artifact context) ready/.test(message)),
  false);
});


test("refresh evicts same-revision details before restoring inherited-category selection", async () => {
  let effectiveCategory = "cover";
  let detailReads = 0;
  const source = () => raster("capture-1", "captured-image", {
    category_assignments: [{
      category: effectiveCategory,
      origin: "manual",
      revision: "capture-category-r1",
    }],
    effective_category: effectiveCategory,
  });
  const child = () => raster("processed-1", "corrected-image", {
    revision: "processed-1-r1",
    source_artifact_id: "capture-1",
    category_assignments: [{
      category: effectiveCategory,
      origin: "inherited",
      revision: "capture-category-r1",
      inherited_from_artifact_id: "capture-1",
    }],
    effective_category: effectiveCategory,
  });
  const { feature } = harness({
    initialExpandedGroups: ["source-images", "processed-images"],
    catalog: {
      async list({ group }) {
        return {
          revision: `${group}-${effectiveCategory}`,
          items: group === "source-images" ? [source()] : [child()],
        };
      },
      async get({ key }) {
        detailReads += 1;
        return key === "artifact:capture-1" ? source() : child();
      },
    },
    resources: {
      async resolveRaster() { return { url: "/safe/display.jpg" }; },
    },
  });

  await feature.setContext({ item_id: "book-1" });
  await feature.select("artifact:processed-1");
  assert.equal(feature.items.get("artifact:processed-1").effectiveCategory, "cover");
  assert.equal(detailReads, 1);

  effectiveCategory = "title_page";
  await feature.refresh({ preserveSelection: true });

  assert.equal(feature.selectedKey, "artifact:processed-1");
  assert.equal(detailReads, 2,
    "the old same-revision detail must not overwrite the refreshed group summary");
  assert.equal(
    feature.items.get("artifact:processed-1").effectiveCategory,
    "title_page",
  );
  assert.equal(
    feature.currentResource.summary.effectiveCategory,
    "title_page",
  );
});


test("selected capture display reload preserves its group and tree cursor", async () => {
  const artifactId = "capture:stable-display:display";
  const key = `artifact:${artifactId}`;
  let revision = 1;
  let listReads = 0;
  let detailReads = 0;
  const resolved = [];
  const revoked = [];
  const display = () => raster(artifactId, "captured-image", {
    revision: `capture-display-r${revision}`,
    resource: {
      resource_id: "capture-display-resource",
      revision: `capture-display-resource-r${revision}`,
      variant: "display",
    },
  });
  const { feature, treeRoot } = harness({
    initialExpandedGroups: ["source-images"],
    catalog: {
      async list() {
        listReads += 1;
        return { revision: `source-index-r${revision}`, items: [display()] };
      },
      async get() {
        detailReads += 1;
        return display();
      },
    },
    resources: {
      async resolveRaster({ resourceRef }) {
        resolved.push(resourceRef.revision);
        const currentRevision = revision;
        return {
          url: `/safe/display-r${currentRevision}.jpg`,
          revoke: () => revoked.push(currentRevision),
        };
      },
    },
  });

  await feature.setContext({ item_id: "book-1" });
  await feature.select(key);
  assert.equal(feature.currentResource.url, "/safe/display-r1.jpg");
  feature.activeKey = "group:source-images";
  treeRoot.scrollTop = 73;

  revision = 2;
  await feature.reloadSelection(key);

  assert.equal(feature.selectedKey, key);
  assert.equal(feature.activeKey, "group:source-images",
    "terminal repaint does not steal the keyboard cursor");
  assert.equal(treeRoot.scrollTop, 73,
    "terminal repaint does not scroll the tree back to the selection");
  assert.equal(feature.items.get(key).group, "source-images");
  assert.equal(feature.expandedGroups.has("source-images"), true);
  assert.equal(feature.currentResource.url, "/safe/display-r2.jpg");
  assert.equal(feature.currentResource.summary.revision, "capture-display-r2");
  assert.equal(listReads, 1, "the stable display reload skips collection paging");
  assert.equal(detailReads, 2, "only the selected detail is force-refreshed");
  assert.deepEqual(resolved, [
    "capture-display-resource-r1",
    "capture-display-resource-r2",
  ]);
  assert.deepEqual(revoked, [1], "the replaced raster lease is released");
});


test("capture display reload rebases a paginated group cursor", async () => {
  const artifactId = "capture:stable-paged:display";
  const key = `artifact:${artifactId}`;
  const siblingKey = "artifact:capture-paged-sibling";
  const finalKey = "artifact:capture-paged-final";
  const listCursors = [];
  let revision = 1;
  const display = () => raster(artifactId, "captured-image", {
    revision: `capture-display-r${revision}`,
    resource: {
      resource_id: "capture-display-resource",
      revision: `capture-display-resource-r${revision}`,
      variant: "display",
    },
  });
  const { feature, treeRoot } = harness({
    initialExpandedGroups: ["source-images"],
    pageLimit: 2,
    catalog: {
      async list({ cursor }) {
        listCursors.push(cursor);
        if (listCursors.length === 1) {
          assert.equal(cursor, null);
          return {
            revision: "source-index-r1",
            items: [display(), raster("capture-paged-sibling")],
            nextCursor: "source-cursor-r1-page-2",
            total: 3,
          };
        }
        if (cursor == null) {
          return {
            revision: "source-index-r2",
            items: [display(), raster("capture-paged-sibling")],
            nextCursor: "source-cursor-r2-page-2",
            total: 3,
          };
        }
        assert.equal(cursor, "source-cursor-r2-page-2",
          "the old revision-bound cursor must not be reused");
        return {
          revision: "source-index-r2",
          items: [raster("capture-paged-final")],
          nextCursor: null,
          total: 3,
        };
      },
      async get() { return display(); },
    },
    resources: {
      async resolveRaster({ resourceRef }) {
        return { url: `/safe/${resourceRef.revision}.jpg` };
      },
    },
  });

  await feature.setContext({ item_id: "book-1" });
  await feature.select(key);
  feature.activeKey = siblingKey;
  treeRoot.scrollTop = 91;

  revision = 2;
  await feature.reloadSelection(key);

  const rebased = feature.groupState("source-images");
  assert.equal(rebased.revision, "source-index-r2");
  assert.equal(rebased.nextCursor, "source-cursor-r2-page-2");
  assert.equal(feature.selectedKey, key);
  assert.equal(feature.activeKey, siblingKey);
  assert.equal(treeRoot.scrollTop, 91);
  assert.equal(feature.expandedGroups.has("source-images"), true);
  assert.equal(feature.currentResource.summary.revision, "capture-display-r2");

  await feature.loadGroup("source-images");
  assert.deepEqual(listCursors, [
    null,
    null,
    "source-cursor-r2-page-2",
  ]);
  assert.equal(feature.items.has(finalKey), true,
    "the next page remains reachable after the display replacement");
  assert.equal(feature.groupState("source-images").error, null);
});


test("newest capture display reload wins when older detail resolves first", async () => {
  const artifactId = "capture:stable-race:display";
  const key = `artifact:${artifactId}`;
  const reloadRequests = [];
  const resolved = [];
  let detailReads = 0;
  const display = (revision) => raster(artifactId, "captured-image", {
    revision: `capture-display-r${revision}`,
    resource: {
      resource_id: "capture-display-resource",
      revision: `capture-display-resource-r${revision}`,
      variant: "display",
    },
  });
  const { feature } = harness({
    initialExpandedGroups: ["source-images"],
    catalog: {
      async list() {
        return { revision: "source-index-r1", items: [display(1)] };
      },
      get({ signal }) {
        detailReads += 1;
        if (detailReads === 1) return Promise.resolve(display(1));
        const request = deferred();
        reloadRequests.push({ ...request, signal });
        return request.promise;
      },
    },
    resources: {
      async resolveRaster({ resourceRef }) {
        resolved.push(resourceRef.revision);
        return { url: `/safe/${resourceRef.revision}.jpg` };
      },
    },
  });

  await feature.setContext({ item_id: "book-1" });
  await feature.select(key);
  const older = feature.reloadSelection(key);
  const newer = feature.reloadSelection(key);
  assert.equal(reloadRequests.length, 2);

  reloadRequests[0].resolve(display(2));
  assert.equal(await older, null, "a superseded detail is never merged or routed");
  assert.equal(feature.currentResource.summary.revision, "capture-display-r1");
  assert.equal(reloadRequests[1].signal.aborted, false,
    "the older completion cannot abort the newer repaint");

  reloadRequests[1].resolve(display(3));
  await newer;
  assert.equal(feature.items.get(key).revision, "capture-display-r3");
  assert.equal(feature.currentResource.summary.revision, "capture-display-r3");
  assert.equal(feature.currentResource.url,
    "/safe/capture-display-resource-r3.jpg");
  assert.deepEqual(resolved, [
    "capture-display-resource-r1",
    "capture-display-resource-r3",
  ], "the stale r2 resource is never resolved");
});


test("capture display reload cannot cross an away-and-back selection epoch", async () => {
  const artifactId = "capture:stable-selection:display";
  const key = `artifact:${artifactId}`;
  const siblingKey = "artifact:capture-sibling";
  const pendingReload = deferred();
  let displayReads = 0;
  const display = (revision) => raster(artifactId, "captured-image", {
    revision: `capture-display-r${revision}`,
    resource: {
      resource_id: "capture-display-resource",
      revision: `capture-display-resource-r${revision}`,
      variant: "display",
    },
  });
  const { feature } = harness({
    initialExpandedGroups: ["source-images"],
    catalog: {
      async list() {
        return { items: [display(1), raster("capture-sibling")] };
      },
      get({ key: requestedKey }) {
        if (requestedKey === siblingKey) {
          return Promise.resolve(raster("capture-sibling"));
        }
        displayReads += 1;
        return displayReads === 1
          ? Promise.resolve(display(1)) : pendingReload.promise;
      },
    },
    resources: {
      async resolveRaster({ resourceRef }) {
        return { url: `/safe/${resourceRef.revision}.jpg` };
      },
    },
  });

  await feature.setContext({ item_id: "book-1" });
  await feature.select(key);
  const staleReload = feature.reloadSelection(key);
  await feature.select(siblingKey);
  await feature.select(key);

  pendingReload.resolve(display(2));
  assert.equal(await staleReload, null);
  assert.equal(feature.selectedKey, key);
  assert.equal(feature.currentResource.summary.revision, "capture-display-r1",
    "a response from the earlier selection epoch cannot repaint after return");
});


test("image selection resolves only display data until full resolution is explicit", async () => {
  const variants = [];
  const revoked = [];
  const resources = {
    async resolveRaster({ itemId, artifactId, resourceRef, variant }) {
      variants.push({ itemId, artifactId, id: resourceRef.id, variant });
      return {
        url: `/resource/${resourceRef.id}/${variant}`,
        revoke: () => revoked.push(`${resourceRef.id}:${variant}`),
      };
    },
    async readText() { throw new Error("not used"); },
    async listRegions() { throw new Error("not used"); },
  };
  const { feature, published } = harness({
    initialExpandedGroups: ["source-images"],
    catalog: {
      async list() { return { items: [raster("capture-1")] }; },
      async get() {
        return raster("capture-1", "captured-image", {
          correction: {
            artifact_revision: "capture-1-r1",
            source_revision: "scan-r1",
            source_sha256: "b".repeat(64),
            proposal: {
              schema: "org.whl.page-boundary-proposal",
              version: 1,
              source_revision: "scan-r1",
              quad: [[0, 0], [1, 0], [1, 1], [0, 1]],
            },
          },
        });
      },
    },
    resources,
  });
  await feature.setContext({ item_id: "book-1" });
  await feature.select("artifact:capture-1");

  assert.deepEqual(variants, [{
    itemId: "book-1",
    artifactId: "capture-1",
    id: "capture-1-resource",
    variant: "display",
  }]);
  const display = published.at(-1);
  assert.equal(display.url, "/resource/capture-1-resource/display");
  assert.equal(typeof display.requestFull, "function");
  assert.equal(display.correction.item_id, "book-1");
  assert.equal(display.correction.artifact_id, "capture-1");
  assert.equal(display.correction.source_sha256, "b".repeat(64));
  assert.equal(display.correction.proposal.source_revision, "scan-r1");

  const full = await display.requestFull();
  assert.equal(full.url, "/resource/capture-1-resource/full");
  assert.deepEqual(variants, [
    {
      itemId: "book-1",
      artifactId: "capture-1",
      id: "capture-1-resource",
      variant: "display",
    },
    {
      itemId: "book-1",
      artifactId: "capture-1",
      id: "capture-1-resource",
      variant: "full",
    },
  ]);
  feature.destroy();
  assert.deepEqual(revoked.sort(), [
    "capture-1-resource:display",
    "capture-1-resource:full",
  ]);
});


test("engine-backed images page same-canvas annotations into the editor overlay", async () => {
  const regionCalls = [];
  const region = (id) => ({
    key: { item_id: "book-1", annotation_id: id },
    annotation_id: id,
    object_type: "spatial-annotation",
    revision: `${id}-r1`,
    label: id,
    selector: {
      type: "polygon",
      coordinate_space: "canvas-normalized",
      coordinate_space_revision: "page-r1",
      points: [
        { x: 0.1, y: 0.1 },
        { x: 0.3, y: 0.1 },
        { x: 0.3, y: 0.3 },
      ],
    },
  });
  const { feature, published } = harness({
    initialExpandedGroups: ["source-images"],
    catalog: {
      async list() { return { items: [raster("capture-1")] }; },
      async get() {
        return raster("capture-1", "captured-image", {
          extensions: { corrections_ui: { paged_regions: true } },
        });
      },
    },
    resources: {
      async resolveRaster() { return { url: "/safe/display.jpg" }; },
      async listRegions({
        representationId, canvasId, canvasRevision, cursor, limit,
      }) {
        regionCalls.push({
          representationId, canvasId, canvasRevision, cursor, limit,
        });
        return cursor
          ? { items: [region("region-2")], nextCursor: null }
          : { items: [region("region-1")], nextCursor: "regions-2" };
      },
    },
  });

  await feature.setContext({ item_id: "book-1" });
  await feature.select("artifact:capture-1");

  assert.deepEqual(regionCalls, [
    {
      representationId: "scan-1",
      canvasId: "page-1",
      canvasRevision: "page-r1",
      cursor: null,
      limit: 200,
    },
    {
      representationId: "scan-1",
      canvasId: "page-1",
      canvasRevision: "page-r1",
      cursor: "regions-2",
      limit: 200,
    },
  ]);
  assert.deepEqual(
    published.at(-1).regions.map((value) => value.annotation_id),
    ["region-1", "region-2"],
  );
});


test("paged image regions do not delay the resolved display resource", async () => {
  const regionRequest = deferred();
  const regionPage = deferred();
  const overlay = annotation("region-1");
  const { feature, published } = harness({
    initialExpandedGroups: ["source-images"],
    catalog: {
      async list() { return { items: [raster("capture-1")] }; },
      async get() {
        return raster("capture-1", "captured-image", {
          extensions: { corrections_ui: { paged_regions: true } },
        });
      },
    },
    resources: {
      async resolveRaster() { return { url: "/safe/display.jpg" }; },
      async listRegions() {
        regionRequest.resolve();
        return regionPage.promise;
      },
    },
  });

  await feature.setContext({ item_id: "book-1" });
  const selection = feature.select("artifact:capture-1");
  await regionRequest.promise;

  const display = published.at(-1);
  assert.equal(display.url, "/safe/display.jpg");
  assert.deepEqual(display.regions, []);
  assert.equal(feature.currentResource, display);

  regionPage.resolve({ items: [overlay], nextCursor: null });
  await selection;

  const enriched = published.at(-1);
  assert.notEqual(enriched, display);
  assert.equal(enriched.url, "/safe/display.jpg");
  assert.deepEqual(
    enriched.regions.map((value) =>
      value.annotation_id || value.key && value.key.annotation_id),
    ["region-1"],
  );
});


test("region detail paging keeps all source pins and deduplicates the selected row", async () => {
  const selected = annotation("region-1");
  const sibling = annotation("region-2");
  const later = annotation("region-3");
  const calls = [];
  const { feature, published } = harness({
    initialExpandedGroups: ["layout-regions"],
    catalog: {
      async list() { return { items: [selected], nextCursor: null }; },
      async get() { return selected; },
    },
    resources: {
      async listRegions(args) {
        calls.push(args);
        return args.cursor
          ? { items: [sibling, later], nextCursor: null }
          : { items: [selected, sibling], nextCursor: "regions-2" };
      },
    },
  });

  await feature.setContext({ item_id: "book-1" });
  await feature.select("annotation:region-1");
  const first = published.at(-1);
  const identity = (value) => value.annotation_id || value.annotationId ||
    value.id || value.key && value.key.annotation_id;

  assert.deepEqual(first.regions.map(identity), ["region-1", "region-2"]);
  assert.equal(calls[0].representationId, "scan-1");
  assert.equal(calls[0].canvasId, "page-1");
  assert.equal(calls[0].canvasRevision, "page-r1");

  await first.loadNext();
  assert.deepEqual(
    published.at(-1).regions.map(identity),
    ["region-1", "region-2", "region-3"],
  );
  assert.equal(calls[1].representationId, "scan-1");
  assert.equal(calls[1].canvasId, "page-1");
  assert.equal(calls[1].canvasRevision, "page-r1");
});


test("a full region page keeps its last row when selected detail is prepended", async () => {
  const selected = annotation("selected-region");
  const page = Array.from({ length: 200 }, (_value, index) =>
    annotation(index === 0 ? "selected-region" : `region-${index + 1}`));
  const { feature, published } = harness({
    initialExpandedGroups: ["layout-regions"],
    catalog: {
      async list() { return { items: [selected], nextCursor: null }; },
      async get() { return selected; },
    },
    resources: {
      async listRegions() {
        return { items: page, nextCursor: "regions-200" };
      },
    },
  });

  await feature.setContext({ item_id: "book-1" });
  await feature.select("annotation:selected-region");
  const resource = published.at(-1);
  const identities = resource.regions.map((value) =>
    value.annotation_id || value.annotationId || value.id ||
      value.key && value.key.annotation_id);

  assert.equal(identities.length, 200);
  assert.equal(identities.filter((id) => id === "selected-region").length, 1);
  assert.equal(identities.includes("region-200"), true);
  assert.equal(resource.nextCursor, "regions-200");
});


test("paged OCR stays bounded and unavailable artifacts are explicit", async () => {
  const reads = [];
  const ocr = {
    key: { item_id: "book-1", artifact_id: "ocr-1" },
    revision: "ocr-r1",
    kind: "ocr",
    label: "OCR text",
    media_type: "text/plain",
    resource_state: "available",
    resource: {
      resource_id: "ocr-resource",
      revision: "ocr-resource-r1",
      variant: "text",
    },
    freshness: "untracked",
  };
  const missing = raster("missing-1", "corrected-image", {
    resource_state: "missing",
    resource: null,
    freshness: "stale",
    generated: true,
  });
  const byKey = new Map([
    ["artifact:ocr-1", ocr],
    ["artifact:missing-1", missing],
  ]);
  const { feature, published } = harness({
    initialExpandedGroups: ["ocr-text", "processed-images"],
    catalog: {
      async list({ group }) {
        return { items: group === "ocr-text" ? [ocr] : [missing] };
      },
      async get({ key }) { return byKey.get(key); },
    },
    resources: {
      async resolveRaster() { throw new Error("missing raster must not resolve"); },
      async readText({ cursor, limit }) {
        reads.push({ cursor, limit });
        return cursor == null
          ? { text: "first page", nextCursor: "page-2" }
          : { text: " second page", nextCursor: null };
      },
      async listRegions() { throw new Error("not used"); },
    },
  });
  await feature.setContext({ item_id: "book-1" });
  await feature.select("artifact:ocr-1");
  const textResource = published.at(-1);
  assert.equal(textResource.text, "first page");
  assert.equal(textResource.paged, true);
  assert.equal(textResource.truncated, true);
  assert.deepEqual(reads[0], { cursor: null, limit: 48 * 1024 });
  await textResource.loadNext();
  assert.equal(published.at(-1).text, "first page second page");
  assert.equal(published.at(-1).nextCursor, null);

  await feature.select("artifact:missing-1");
  const missingResource = published.at(-1);
  assert.equal(missingResource.missing, true);
  assert.equal(missingResource.resourceState, "missing");
  const missingRow = feature.rows.find((row) => row.key === "artifact:missing-1");
  assert.equal(missingRow.item.freshness, "stale");
  assert.equal(missingRow.item.generated, true);
});

test("capture metadata uses bounded document pages and large JSON stays paged",
  async () => {
    const metadata = documentArtifact("capture-generated-metadata");
    const notes = documentArtifact("capture-notes", "capture-notes");
    const missing = documentArtifact("capture-missing-metadata", "metadata", {
      resource_state: "missing",
      resource: null,
      freshness: "stale",
    });
    const byKey = new Map([
      ["document:capture-generated-metadata", metadata],
      ["document:capture-notes", notes],
      ["document:capture-missing-metadata", missing],
    ]);
    const reads = [];
    const { feature, published } = harness({
      initialExpandedGroups: ["generated-metadata"],
      catalog: {
        async list() {
          return { items: [metadata, notes, missing], nextCursor: null };
        },
        async get({ key }) { return byKey.get(key); },
      },
      resources: {
        async readText(args) {
          reads.push(args);
          if (args.artifactId === "capture-generated-metadata") {
            return {
              text: "{\"title\":\"A Herbal\",\"pages\":42}",
              nextCursor: null,
            };
          }
          return args.cursor == null
            ? { text: "{\"cataloguer\":\"Ada\",", nextCursor: "20" }
            : { text: "\"note\":\"checked\"}", nextCursor: null };
        },
      },
    });

    await feature.setContext({ item_id: "book-1" });
    await feature.select("document:capture-generated-metadata");
    const structured = published.at(-1);
    assert.equal(structured.family, "metadata");
    assert.deepEqual(structured.metadata, {
      title: "A Herbal",
      pages: 42,
    });
    assert.equal(reads[0].itemId, "book-1");
    assert.equal(reads[0].artifactId, "capture-generated-metadata");
    assert.equal(reads[0].artifactRevision,
      "capture-generated-metadata-r1");
    assert.deepEqual(reads[0].resourceRef, {
      id: "docres-capture-generated-metadata",
      revision: "capture-generated-metadata-resource-r1",
      variant: "text",
    });
    assert.equal(reads[0].cursor, null);
    assert.equal(reads[0].limit, 48 * 1024);

    await feature.select("document:capture-notes");
    const paged = published.at(-1);
    assert.equal(paged.family, "text");
    assert.equal(paged.paged, true);
    assert.equal(paged.truncated, true);
    assert.equal(paged.text, "{\"cataloguer\":\"Ada\",");
    await paged.loadNext();
    assert.equal(published.at(-1).text,
      "{\"cataloguer\":\"Ada\",\"note\":\"checked\"}");
    assert.equal(reads[2].cursor, "20");
    assert.equal(reads[2].artifactId, "capture-notes");

    const readCount = reads.length;
    await feature.select("document:capture-missing-metadata");
    assert.equal(reads.length, readCount,
      "missing capture documents never issue resource reads");
    assert.equal(published.at(-1).missing, true);
    assert.equal(published.at(-1).resourceState, "missing");
  });


test("linked image and annotation cross-highlight with selection and soft hot-target hooks", async () => {
  const figure = raster("figure-1", "figure", {
    linked_keys: ["annotation:region-1"],
  });
  const region = annotation("region-1", "figure-1");
  const rows = new Map([
    ["artifact:figure-1", figure],
    ["annotation:region-1", region],
  ]);
  const { feature, hotTargets, treeRoot } = harness({
    clientHeight: 420,
    initialExpandedGroups: ["extracted-figures", "layout-regions"],
    catalog: {
      async list({ group }) {
        return { items: group === "extracted-figures" ? [figure] : [region] };
      },
      async get({ key }) { return rows.get(key); },
    },
    resources: {
      async resolveRaster() { return { url: "/figure.jpg" }; },
      async readText() { throw new Error("not used"); },
      async listRegions() { return { items: [region], nextCursor: null }; },
    },
  });
  await feature.setContext({ item_id: "book-1" });
  await feature.select("artifact:figure-1");

  assert.deepEqual(feature.selectionSnapshot().linked, ["annotation:region-1"]);
  const linkedRow = treeRoot.querySelectorAll("[data-artifact-key]")
    .find((node) => node.dataset.artifactKey === "annotation:region-1");
  assert.equal(linkedRow.dataset.linked, "true");

  feature.handlePointerOver({ target: linkedRow });
  assert.equal(feature.getCommandTarget().key, "annotation:region-1");
  assert.equal(hotTargets.at(-1).key, "annotation:region-1");
  feature.setHotTarget("");
  assert.equal(feature.getCommandTarget().key, "artifact:figure-1");
});


test("artifact editors add paged tabs and a safe generic unknown inspector", () => {
  const documentRef = fakeDocument();
  const registry = createDefaultEditorRegistry({ documentRef });
  registerArtifactEditors(registry);
  assert.equal(registry.editors.has(ARTIFACT_EDITOR_IDS.pagedText), true);
  assert.equal(registry.editors.has(ARTIFACT_EDITOR_IDS.pagedRegions), true);
  assert.equal(registry.editors.has(ARTIFACT_EDITOR_IDS.regionOverlay), true);
  assert.equal(registry.editors.has(ARTIFACT_EDITOR_IDS.generic), true);

  registry.setResource({
    id: "future-1",
    kind: "future-artifact",
    family: "unknown",
    label: "<script>not markup</script>",
    detail: { safe: true, html: "<img onerror=bad()>" },
  });
  assert.equal(registry.currentEditor().id, ARTIFACT_EDITOR_IDS.generic);
  const host = new FakeNode("div", documentRef);
  registry.render(host);
  assert.match(host.textContent, /<script>not markup<\/script>/);
  assert.match(host.textContent, /<img onerror=bad\(\)>/);
  assert.equal(host.querySelector("script"), null);

  registry.setResource({
    id: "ocr-1",
    kind: "ocr-text",
    family: "text",
    media_type: "text/plain",
    paged: true,
    text: "bounded first page",
    nextCursor: "next",
    async loadNext() {},
  });
  assert.equal(registry.selectEditor(ARTIFACT_EDITOR_IDS.pagedText), true);
  registry.render(host);
  assert.match(host.textContent, /bounded first page/);
  assert.match(host.textContent, /Load more/);
});


test("feature source and scoped styles enforce cancellation, virtualization, and state cues", () => {
  assert.match(artifactsSource, /AbortController/);
  assert.match(artifactsSource, /variant,\s*\"display\"|"display",\s*selectionGeneration/);
  assert.doesNotMatch(artifactsSource, /innerHTML|file:\/\//);
  assert.match(artifactStyles, /\[data-linked="true"\]/);
  assert.match(artifactStyles, /\[data-hot="true"\]/);
  assert.match(artifactStyles, /aria-selected/);
  assert.match(artifactStyles, /prefers-reduced-motion/);
});


test("all #234 modules install through the browser LibraryToolCorrections namespace", () => {
  const context = vm.createContext({});
  for (const name of [
    "artifact-model.js",
    "engine-adapter.js",
    "artifact-editors.js",
    "properties.js",
    "artifacts.js",
  ]) {
    const source = fs.readFileSync(path.join(
      repositoryRoot, "tools", "whl_explorer", "static", "corrections", name), "utf8");
    vm.runInContext(source, context, { filename: name });
  }
  const exported = context.LibraryToolCorrections;
  assert.equal(typeof exported.decodeArtifactSummary, "function");
  assert.equal(typeof exported.createCorrectionsEnginePorts, "function");
  assert.equal(typeof exported.registerArtifactEditors, "function");
  assert.equal(typeof exported.createPropertiesInspector, "function");
  assert.equal(typeof exported.createArtifactsFeature, "function");
  assert.equal(typeof exported.createUnavailableArtifactPorts, "function");
});

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


test("image selection resolves only display data until full resolution is explicit", async () => {
  const variants = [];
  const revoked = [];
  const resources = {
    async resolveRaster({ resourceRef, variant }) {
      variants.push({ id: resourceRef.id, variant });
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
  assert.deepEqual(variants.map((entry) => entry.variant), ["display", "full"]);
  feature.destroy();
  assert.deepEqual(revoked.sort(), [
    "capture-1-resource:display",
    "capture-1-resource:full",
  ]);
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
  assert.deepEqual(reads[0], { cursor: null, limit: 64 * 1024 });
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
  assert.equal(typeof exported.registerArtifactEditors, "function");
  assert.equal(typeof exported.createPropertiesInspector, "function");
  assert.equal(typeof exported.createArtifactsFeature, "function");
  assert.equal(typeof exported.createUnavailableArtifactPorts, "function");
});

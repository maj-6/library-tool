"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const {
  BooksPanelController,
  CorrectionsIndexStore,
} = require("../tools/whl_explorer/static/corrections/books");
const {
  decodeArtifactDetail,
} = require("../tools/whl_explorer/static/corrections/artifact-model");
const {
  createArtifactsFeature,
} = require("../tools/whl_explorer/static/corrections/artifacts");
const {
  createClassificationController,
} = require("../tools/whl_explorer/static/corrections/keymap");
const {
  FakeNode,
  fakeDocument,
} = require("./fixtures/corrections_fake_dom");


const fixturePath = path.join(
  __dirname,
  "fixtures",
  "corrections_books_index_v2.json",
);
const correctionsTemplatePath = path.join(
  __dirname,
  "..",
  "tools",
  "whl_explorer",
  "templates",
  "corrections.html",
);


function fixture() {
  return JSON.parse(fs.readFileSync(fixturePath, "utf8"));
}


function raster(overrides = {}) {
  return {
    key: { item_id: "book-herbarium", artifact_id: "capture-cover" },
    revision: "capture-cover-r2",
    kind: "captured-image",
    label: "Front cover",
    media_type: "image/jpeg",
    resource_state: "available",
    resource: {
      resource_id: "capture-cover-resource",
      revision: "capture-cover-bytes-r1",
      variant: "display",
    },
    freshness: "current",
    source: {
      representation_id: "scan-herbarium",
      representation_revision: "scan-herbarium-r1",
      canvas_id: "canvas-cover",
      canvas_revision: "canvas-cover-r1",
    },
    category_assignments: [{
      origin: "manual",
      revision: "capture-cover-category-r1",
      category: "cover",
    }],
    effective_category: "cover",
    caption_assertions: [{
      origin: "machine",
      revision: "capture-cover-caption-machine-r1",
      text: "Machine cover caption",
      confidence: 0.84,
    }],
    effective_caption: {
      origin: "machine",
      revision: "capture-cover-caption-machine-r1",
      text: "Machine cover caption",
      confidence: 0.84,
    },
    provenance: {
      origin: "capture",
      provider_id: "android",
      model: "camera",
    },
    ...overrides,
  };
}


function mistralRegion(overrides = {}) {
  return {
    key: {
      item_id: "book-herbarium",
      annotation_id: "region-marginalia-1",
    },
    revision: "region-marginalia-r1",
    kind: "spatial-annotation",
    label: "Handwritten margin note",
    freshness: "current",
    source: {
      representation_id: "scan-herbarium",
      representation_revision: "scan-herbarium-r1",
      canvas_id: "canvas-cover",
      canvas_revision: "canvas-cover-r1",
    },
    selector: {
      type: "polygon",
      coordinate_space: "canvas-normalized",
      coordinate_space_revision: "canvas-cover-r1",
      points: [
        { x: 0.08, y: 0.12 },
        { x: 0.42, y: 0.12 },
        { x: 0.42, y: 0.38 },
        { x: 0.08, y: 0.38 },
      ],
    },
    linked_artifact_ids: ["capture-cover"],
    role_assignments: [{
      origin: "machine",
      revision: "region-role-machine-r1",
      role: "text",
      confidence: 0.71,
    }],
    effective_role: "text",
    caption_assertions: [{
      origin: "machine",
      revision: "region-caption-machine-r1",
      text: "Unclassified handwritten note",
    }],
    effective_caption: {
      origin: "machine",
      revision: "region-caption-machine-r1",
      text: "Unclassified handwritten note",
    },
    provenance: {
      origin: "ocr",
      provider_id: "mistral",
      model: "pixtral",
    },
    ...overrides,
  };
}


function receiptTarget(kind, id, before, after) {
  return {
    kind,
    target_id: id,
    before_revision: before,
    after_revision: after,
  };
}


function inverse(action, targets) {
  return {
    action,
    expected_targets: targets,
    payload: {},
  };
}


function appendBooksPane(documentRef, scope) {
  const root = new FakeNode("nav", documentRef);
  root.setAttribute("aria-label", "Books");
  const count = new FakeNode("span", documentRef);
  count.setAttribute("data-books-count", "");
  const filter = new FakeNode("input", documentRef);
  filter.setAttribute("data-books-filter", "");
  filter.setAttribute("aria-label", "Filter books");
  const list = new FakeNode("ul", documentRef);
  list.setAttribute("data-books-list", "");
  root.append(count, filter, list);
  scope.append(root);
  return { count, filter, list, root };
}


function appendPropertiesPane(documentRef, scope) {
  const pane = new FakeNode("aside", documentRef);
  pane.setAttribute("aria-labelledby", "release-properties-heading");
  const heading = new FakeNode("h2", documentRef);
  heading.id = "release-properties-heading";
  heading.textContent = "Properties";
  const root = new FakeNode("dl", documentRef);
  pane.append(heading, root);
  scope.append(pane);
  return { heading, pane, root };
}


function nextTurn() {
  return new Promise((resolve) => setImmediate(resolve));
}


async function keyboardSelect(feature, treeRoot, key) {
  treeRoot.focus();
  assert.equal(treeRoot.ownerDocument.activeElement, treeRoot,
    "tree keyboard events require the production focus target");
  const rows = feature.navigableRows();
  const index = rows.findIndex((row) => row.key === key);
  assert.ok(index >= 0, `${key} must be keyboard reachable`);
  treeRoot.emit("keydown", { key: "Home", target: treeRoot });
  for (let offset = 0; offset < index; offset += 1) {
    treeRoot.emit("keydown", { key: "ArrowDown", target: treeRoot });
  }
  const active = feature.navigableRows().find(
    (row) => row.key === feature.activeKey,
  );
  assert.equal(active && active.key, key);
  const activation = treeRoot.emit("keydown", {
    key: "Enter",
    target: treeRoot,
  });
  assert.equal(activation.defaultPrevented, true);
  await nextTurn();
  await nextTurn();
  return feature.selectionSnapshot().selected;
}


test("production Corrections markup owns the pane names and live-region contract",
  () => {
    const template = fs.readFileSync(correctionsTemplatePath, "utf8");
    assert.match(template,
      /<nav\b[^>]*id="corrections-books"[^>]*aria-labelledby="books-heading"/);
    assert.match(template, /<h2\b[^>]*id="books-heading">Books<\/h2>/);
    assert.match(template,
      /<aside\b[^>]*id="corrections-properties"[^>]*aria-labelledby="properties-heading"/);
    assert.match(template, /<h2\b[^>]*id="properties-heading">Properties<\/h2>/);
    assert.match(template, /<dl\b[^>]*data-properties-list/);
    assert.match(template,
      /<span\b[^>]*data-corrections-command-target[^>]*aria-live="polite"[^>]*aria-atomic="true"/);
  });


test("keyboard-only Corrections modules expose accessible Books, Artifacts, and Properties state",
  async () => {
    const documentRef = fakeDocument();
    documentRef.hasFocus = () => true;
    const scope = new FakeNode("main", documentRef);
    const commandLive = new FakeNode("p", documentRef);
    commandLive.setAttribute("data-corrections-command-target", "");
    commandLive.setAttribute("aria-live", "polite");
    scope.append(commandLive);

    const booksPane = appendBooksPane(documentRef, scope);
    const store = new CorrectionsIndexStore({
      api: { loadIndex: async () => fixture() },
    });
    const books = new BooksPanelController({
      root: booksPane.root,
      documentRef,
      store,
    }).mount();
    await store.openWorkspace("local-library");

    assert.equal(booksPane.count.textContent, "4");
    assert.equal(booksPane.list.children[0].dataset.bookId, "book-herbarium",
      "the attention-marked book remains first");
    const coverButton = booksPane.list.querySelectorAll("[data-artifact-id]")
      .find((node) => node.dataset.artifactId === "capture-cover");
    assert.ok(coverButton);
    assert.equal(coverButton.tagName, "BUTTON",
      "capture activation uses native keyboard button semantics");
    assert.match(coverButton.getAttribute("aria-label"), /Front cover, Cover/);
    coverButton.focus();
    assert.equal(documentRef.activeElement, coverButton);

    let image = raster();
    let region = mistralRegion();
    const categoryCalls = [];
    const roleCalls = [];
    const captionCalls = [];
    const commandPort = {
      async assignImageCategory(payload) {
        categoryCalls.push(payload);
        const before = image.revision;
        image = raster({
          revision: "capture-cover-r3",
          category_assignments: [{
            origin: "manual",
            revision: "capture-cover-category-r2",
            category: payload.category,
          }],
          effective_category: payload.category,
        });
        const targets = [receiptTarget(
          "artifact",
          "capture-cover",
          before,
          image.revision,
        )];
        return {
          receipt: {
            item_id: "book-herbarium",
            targets,
            inverse: inverse("category.assign", targets),
          },
          detail: image,
        };
      },
      async assignRegionRole(payload) {
        roleCalls.push(payload);
        const annotationBefore = region.revision;
        const imageBefore = image.revision;
        region = mistralRegion({
          revision: "region-marginalia-r2",
          role_assignments: [
            {
              origin: "manual",
              revision: "region-role-manual-r2",
              role: payload.role,
            },
            ...region.role_assignments,
          ],
          effective_role: payload.role,
        });
        image = { ...image, revision: "capture-cover-r4" };
        const targets = [
          receiptTarget(
            "annotation",
            "region-marginalia-1",
            annotationBefore,
            region.revision,
          ),
          receiptTarget(
            "artifact",
            "capture-cover",
            imageBefore,
            image.revision,
          ),
        ];
        return {
          receipt: {
            item_id: "book-herbarium",
            targets,
            inverse: inverse("role.assign", targets),
          },
          details: [region, image],
        };
      },
      async setManualCaption(payload) {
        captionCalls.push(payload);
        const before = image.revision;
        image = {
          ...image,
          revision: "capture-cover-r5",
          caption_assertions: [
            {
              origin: "manual",
              revision: "capture-cover-caption-manual-r2",
              text: payload.text,
              language: payload.language,
            },
            ...image.caption_assertions.filter(
              (assertion) => assertion.origin !== "manual",
            ),
          ],
          effective_caption: {
            origin: "manual",
            revision: "capture-cover-caption-manual-r2",
            text: payload.text,
            language: payload.language,
          },
        };
        const targets = [receiptTarget(
          "artifact",
          "capture-cover",
          before,
          image.revision,
        )];
        return {
          receipt: {
            item_id: "book-herbarium",
            targets,
            inverse: inverse("caption.clear", targets),
          },
          detail: image,
        };
      },
      async clearManualCaption() {
        throw new Error("not used");
      },
      async executeInverse() {
        throw new Error("not used");
      },
    };

    const treeRoot = new FakeNode("div", documentRef);
    treeRoot.clientHeight = 280;
    scope.append(treeRoot);
    const propertiesPane = appendPropertiesPane(documentRef, scope);
    let classification = null;
    const selectionChanges = [];
    const artifacts = createArtifactsFeature({
      treeRoot,
      propertiesRoot: propertiesPane.root,
      documentRef,
      commands: commandPort,
      initialExpandedGroups: ["layout-regions", "source-images"],
      catalog: {
        async list({ group }) {
          const items = group === "layout-regions" ? [region]
            : group === "source-images" ? [image] : [];
          return {
            revision: `${group}-inventory-r1`,
            items,
            nextCursor: null,
          };
        },
        async get({ key }) {
          if (key === "artifact:capture-cover") return image;
          if (key === "annotation:region-marginalia-1") return region;
          throw new Error(`unexpected artifact ${key}`);
        },
      },
      resources: {
        async resolveRaster() {
          return { url: "/api/v1/resources/capture-cover" };
        },
        async listRegions() {
          return { items: [region], nextCursor: null };
        },
        async readText() {
          throw new Error("not used");
        },
      },
      onSelection(detail) {
        if (detail) selectionChanges.push(detail.key);
        if (classification) classification.setSelectionTarget(detail);
      },
    }).mount();

    const commandErrors = [];
    classification = createClassificationController({
      scope,
      documentRef,
      port: commandPort,
      operationIdFactory: (prefix) => `release-keyboard-${prefix}`,
      resolveLinkedArtifact(_target, request) {
        return artifacts.items.get(request.linkedKey) || null;
      },
      async onChanged(result) {
        for (const raw of [result.detail, ...(result.details || [])]) {
          if (raw) artifacts.mergeDetail(decodeArtifactDetail(raw));
        }
      },
      onError(error) {
        commandErrors.push(error);
      },
    }).mount();

    await artifacts.setContext({
      item_id: "book-herbarium",
      representation_id: "scan-herbarium",
      canvas_id: "canvas-cover",
    });
    assert.equal(treeRoot.getAttribute("role"), "tree");
    assert.equal(treeRoot.getAttribute("tabindex"), "0");
    assert.ok(treeRoot.getAttribute("aria-activedescendant"));

    const selectedImage = await keyboardSelect(
      artifacts,
      treeRoot,
      "artifact:capture-cover",
    );
    assert.equal(selectedImage.objectType, "artifact");
    const selectedImageRow = treeRoot.querySelectorAll("[data-tree-key]")
      .find((row) => row.dataset.treeKey === "artifact:capture-cover");
    assert.ok(selectedImageRow);
    assert.equal(selectedImageRow.getAttribute("role"), "treeitem");
    assert.equal(selectedImageRow.getAttribute("aria-selected"), "true");
    assert.equal(treeRoot.getAttribute("aria-activedescendant"),
      selectedImageRow.id);
    const titleShortcut = scope.emit("keydown", {
      key: "t",
      target: treeRoot,
    });
    assert.equal(titleShortcut.defaultPrevented, true);
    await nextTurn();
    await nextTurn();
    assert.equal(categoryCalls.length, 1);
    assert.equal(categoryCalls[0].category, "title_page");
    assert.equal(categoryCalls[0].expectedArtifactRevision, "capture-cover-r2");
    assert.match(commandLive.textContent, /Title page: Front cover/);
    assert.equal(
      artifacts.items.get("artifact:capture-cover").effectiveCategory,
      "title_page",
    );

    const selectedRegion = await keyboardSelect(
      artifacts,
      treeRoot,
      "annotation:region-marginalia-1",
    );
    assert.equal(selectedRegion.objectType, "spatial-annotation");
    const selectedRegionRow = treeRoot.querySelectorAll("[data-tree-key]")
      .find((row) => row.dataset.treeKey ===
        "annotation:region-marginalia-1");
    assert.ok(selectedRegionRow);
    assert.equal(selectedRegionRow.getAttribute("role"), "treeitem");
    assert.equal(selectedRegionRow.getAttribute("aria-selected"), "true");
    assert.equal(treeRoot.getAttribute("aria-activedescendant"),
      selectedRegionRow.id);
    const marginaliaShortcut = scope.emit("keydown", {
      key: "m",
      target: treeRoot,
    });
    assert.equal(marginaliaShortcut.defaultPrevented, true);
    await nextTurn();
    await nextTurn();
    assert.equal(roleCalls.length, 1);
    assert.equal(roleCalls[0].role, "marginalia");
    assert.equal(roleCalls[0].linkedArtifactId, "capture-cover");
    assert.equal(roleCalls[0].expectedLinkedArtifactRevision, "capture-cover-r3");
    assert.match(commandLive.textContent, /Marginalia: Handwritten margin note/);
    assert.equal(
      artifacts.items.get("annotation:region-marginalia-1").effectiveRole,
      "marginalia",
    );

    await keyboardSelect(artifacts, treeRoot, "artifact:capture-cover");
    assert.equal(propertiesPane.pane.getAttribute("aria-labelledby"),
      "release-properties-heading");
    assert.equal(propertiesPane.root.tagName, "DL");
    assert.equal(propertiesPane.root.querySelectorAll(".property-card").length, 3);
    assert.match(propertiesPane.root.textContent, /Machine and source facts/);
    assert.match(propertiesPane.root.textContent, /Human assertions/);
    assert.match(propertiesPane.root.textContent, /Machine cover caption/);
    assert.match(propertiesPane.root.textContent, /Effective categorytitle_page/);

    const textarea = propertiesPane.root.querySelector("textarea");
    const form = propertiesPane.root.querySelector("form");
    const labels = propertiesPane.root.querySelectorAll("label");
    const submit = propertiesPane.root.querySelectorAll("button")
      .find((button) => button.type === "submit");
    assert.ok(textarea && form && submit);
    assert.equal(labels[0].htmlFor, textarea.id);
    assert.equal(submit.tagName, "BUTTON",
      "caption save retains native keyboard submit semantics");
    textarea.focus();
    textarea.value = "Keyboard-reviewed botanical cover";
    textarea.emit("input", { target: textarea });
    const submitEvent = form.emit("submit", { target: form });
    assert.equal(submitEvent.defaultPrevented, true);
    await nextTurn();
    await nextTurn();
    assert.equal(captionCalls.length, 1);
    assert.equal(captionCalls[0].text, "Keyboard-reviewed botanical cover");
    assert.equal(captionCalls[0].expectedArtifactRevision, "capture-cover-r4");
    assert.match(propertiesPane.root.textContent,
      /Keyboard-reviewed botanical cover/);
    assert.match(propertiesPane.root.textContent, /Machine cover caption/,
      "human edits do not hide retained machine evidence");
    assert.deepEqual(commandErrors, []);
    assert.ok(selectionChanges.includes("artifact:capture-cover"));
    assert.ok(selectionChanges.includes("annotation:region-marginalia-1"));

    classification.destroy();
    artifacts.destroy();
    books.destroy();
  });

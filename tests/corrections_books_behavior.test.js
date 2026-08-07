const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const {
  detailsOf,
  marksOf,
  settle,
  summaryOf,
  tiered,
} = require("./fixtures/corrections_tiers");
const {
  BooksPanelController,
  CORRECTIONS_INDEX_CHANGE_SCHEMA,
  CORRECTIONS_INDEX_SCHEMA,
  CorrectionsContractError,
  CorrectionsIndexStore,
  attentionBooks,
  bookNeedsAttention,
  booksForView,
  captureBooks,
  markImportedAt,
  captureCommandTarget,
  normalizeCaptureMarks,
  normalizeCorrectionsIndex,
  normalizeCorrectionsIndexDetail,
  normalizeCorrectionsIndexSummary,
  sortedBooks,
} = require("../tools/whl_explorer/static/corrections/books");


const fixturePath = path.join(
  __dirname, "fixtures", "corrections_books_index_v2.json");


function fixture() {
  return JSON.parse(fs.readFileSync(fixturePath, "utf8"));
}


// A few panel tests drive the store directly rather than through its api.
// This leaves all three tiers as a completed load would.
function seedStore(store, indexValue) {
  store.index = normalizeCorrectionsIndexSummary(summaryOf(indexValue));
  store.marks = new Map(normalizeCaptureMarks(marksOf(indexValue)).marks
    .map((mark) => [mark.item_id, mark]));
  store.details = new Map(normalizeCorrectionsIndexDetail(
    detailsOf(indexValue, indexValue.books.map((book) => book.id))
  ).books.map((book) => [book.id, book]));
  store.status = "ready";
  store.emit();
}


function clone(value) {
  return JSON.parse(JSON.stringify(value));
}


function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((resolveValue, rejectValue) => {
    resolve = resolveValue;
    reject = rejectValue;
  });
  return { promise, reject, resolve };
}


function withRevision(value, revision) {
  const result = clone(value);
  result.revision = revision;
  return result;
}


function resolvedEntry(value, revision = "review-resolved-r1") {
  const entry = clone(value);
  entry.review = {
    revision,
    state: "resolved",
    reason: entry.review.reason,
    history_count: entry.review.history_count + 1,
    latest_event: {
      operation_id: `op-${revision}`,
      action: "attention.resolve",
      actor_id: "curator-test",
      occurred_at: "2026-07-22T20:00:00Z",
      before_state: "needs_attention",
      after_state: "resolved",
      reason: entry.review.reason,
      comment: "Checked",
    },
  };
  return entry;
}


function reopenedEntry(value, revision = "review-reopened-r1") {
  const entry = clone(value);
  entry.review = {
    revision,
    state: "needs_attention",
    reason: entry.review.reason,
    history_count: entry.review.history_count + 1,
    latest_event: {
      operation_id: `op-${revision}`,
      action: "attention.reopen",
      actor_id: "curator-test",
      occurred_at: "2026-07-22T20:10:00Z",
      before_state: "resolved",
      after_state: "needs_attention",
      reason: entry.review.reason,
      comment: "Check again",
    },
  };
  return entry;
}


class MiniClassList {
  constructor() { this.values = new Set(); }
  add(value) { this.values.add(value); }
  remove(value) { this.values.delete(value); }
  contains(value) { return this.values.has(value); }
}


class MiniNode {
  constructor(tagName, documentRef = null) {
    this.tagName = tagName.toUpperCase();
    this.ownerDocument = documentRef;
    this.parentNode = null;
    this.children = [];
    this.attributes = new Map();
    this.listeners = new Map();
    this.classList = new MiniClassList();
    this.dataset = {};
    this.textContent = "";
    this.className = "";
    this.value = "";
  }
  append(...nodes) {
    for (const node of nodes) {
      node.parentNode = this;
      this.children.push(node);
    }
  }
  insertBefore(node, reference) {
    const index = reference ? this.children.indexOf(reference) : -1;
    node.parentNode = this;
    if (index < 0) this.children.push(node);
    else this.children.splice(index, 0, node);
    return node;
  }
  replaceChildren(...nodes) {
    const active = this.ownerDocument && this.ownerDocument.activeElement;
    if (active && this.contains(active)) {
      active.emit("blur");
      if (this.ownerDocument.activeElement === active) {
        this.ownerDocument.activeElement = null;
      }
    }
    for (const child of this.children) child.parentNode = null;
    this.children = [];
    this.append(...nodes);
  }
  removeChild(node) {
    const index = this.children.indexOf(node);
    if (index >= 0) {
      this.children.splice(index, 1);
      node.parentNode = null;
    }
    return node;
  }
  contains(node) {
    if (node === this) return true;
    return this.children.some((child) => child.contains(node));
  }
  setAttribute(name, value) { this.attributes.set(name, String(value)); }
  getAttribute(name) { return this.attributes.get(name) || null; }
  addEventListener(type, callback) {
    const listeners = this.listeners.get(type) || [];
    listeners.push(callback);
    this.listeners.set(type, listeners);
  }
  removeEventListener(type, callback) {
    this.listeners.set(type,
      (this.listeners.get(type) || []).filter((value) => value !== callback));
  }
  emit(type, overrides = {}) {
    const event = {
      key: "",
      preventDefault() { this.defaultPrevented = true; },
      ...overrides,
    };
    for (const listener of this.listeners.get(type) || []) listener(event);
    return event;
  }
  focus() {
    const active = this.ownerDocument && this.ownerDocument.activeElement;
    if (active && active !== this) active.emit("blur");
    if (this.ownerDocument) this.ownerDocument.activeElement = this;
    this.emit("focus");
  }
  matches(selector) {
    const attribute = selector.match(/^\[data-([a-z-]+)\]$/);
    if (attribute) {
      const name = attribute[1].replace(/-([a-z])/g,
        (_match, letter) => letter.toUpperCase());
      return Object.prototype.hasOwnProperty.call(this.dataset, name);
    }
    return this.tagName === selector.toUpperCase();
  }
  querySelectorAll(selector) {
    const result = [];
    for (const child of this.children) {
      if (child.matches(selector)) result.push(child);
      result.push(...child.querySelectorAll(selector));
    }
    return result;
  }
}


function miniHarness() {
  const documentRef = {
    activeElement: null,
    createElement(name) { return new MiniNode(name, documentRef); },
  };
  const count = new MiniNode("span", documentRef);
  const filter = new MiniNode("input", documentRef);
  const list = new MiniNode("ul", documentRef);
  const body = new MiniNode("div", documentRef);
  body.append(list);
  const nodes = new Map([
    ["[data-books-count]", count],
    ["[data-books-filter]", filter],
    ["[data-books-list]", list],
  ]);
  const root = {
    ownerDocument: documentRef,
    querySelector(selector) { return nodes.get(selector) || null; },
  };
  return { body, count, documentRef, filter, list, root };
}


function thumbnailObserverHarness() {
  const instances = [];
  return {
    instances,
    factory(callback, options) {
      const instance = {
        callback,
        options,
        observed: new Set(),
        unobserved: [],
        disconnected: false,
        observe(node) { this.observed.add(node); },
        unobserve(node) {
          this.observed.delete(node);
          this.unobserved.push(node);
        },
        disconnect() {
          this.disconnected = true;
          this.observed.clear();
        },
        intersect(node) {
          callback([{ target: node, isIntersecting: true, intersectionRatio: 1 }]);
        },
      };
      instances.push(instance);
      return instance;
    },
  };
}


function viewButton(harness, view) {
  return descendants(harness.body, "button")
    .find((button) => button.dataset.booksView === view) || null;
}


function navButton(harness, name) {
  return descendants(harness.body, "button")
    .find((button) => button.dataset.booksNav === name) || null;
}


function textOf(node) {
  return [node.textContent, ...node.children.map(textOf)].join(" ");
}


function descendants(node, tagName) {
  const result = [];
  for (const child of node.children) {
    if (child.tagName === tagName.toUpperCase()) result.push(child);
    result.push(...descendants(child, tagName));
  }
  return result;
}


test("Corrections index validation is strict and capture order is explicit", () => {
  const normalized = normalizeCorrectionsIndex(fixture());
  assert.equal(normalized.schema, CORRECTIONS_INDEX_SCHEMA);
  assert.deepEqual(
    normalized.books[0].captures.map((capture) => capture.artifact_id),
    ["capture-title", "capture-cover"],
  );
  assert.ok(Object.isFrozen(normalized));
  assert.ok(Object.isFrozen(normalized.books[0].captures));

  const wrongSchema = fixture();
  wrongSchema.schema = "librarytool.corrections-index/1";
  assert.throws(() => normalizeCorrectionsIndex(wrongSchema),
    (error) => error instanceof CorrectionsContractError &&
      error.path === "$.schema");

  const unknown = fixture();
  unknown.books[0].legacy_path = "C:/private/book";
  assert.throws(() => normalizeCorrectionsIndex(unknown),
    /legacy_path: is not a recognized field/);

  const missingKind = fixture();
  delete missingKind.books[0].kind;
  assert.throws(() => normalizeCorrectionsIndex(missingKind),
    /kind: is required/);

  const unsupportedKind = fixture();
  unsupportedKind.books[0].kind = "periodical";
  assert.throws(() => normalizeCorrectionsIndex(unsupportedKind),
    /kind: has an unsupported value/);

  const implicitOrder = fixture();
  delete implicitOrder.books[0].captures[0].capture_order;
  assert.throws(() => normalizeCorrectionsIndex(implicitOrder),
    /capture_order: is required/);

  const duplicateOrder = fixture();
  duplicateOrder.books[0].captures[1].capture_order =
    duplicateOrder.books[0].captures[0].capture_order;
  assert.throws(() => normalizeCorrectionsIndex(duplicateOrder),
    /duplicate capture_order/);

  const unsafeThumbnail = fixture();
  unsafeThumbnail.books[2].captures[0].thumbnail.url = "javascript:alert(1)";
  assert.throws(() => normalizeCorrectionsIndex(unsafeThumbnail),
    /disallowed URL scheme/);

  const malformedTarget = fixture();
  malformedTarget.attention[0].target.artifact_id = "not-a-book-target";
  assert.throws(() => normalizeCorrectionsIndex(malformedTarget),
    /book targets cannot contain subordinate identifiers/);

  const contradictoryBookReview = fixture();
  contradictoryBookReview.attention[0].review.revision =
    "review-contradictory-r1";
  assert.throws(() => normalizeCorrectionsIndex(contradictoryBookReview),
    /must exactly match its book attention entry/);

  const missingBookAttention = fixture();
  missingBookAttention.attention.splice(0, 1);
  assert.throws(() => normalizeCorrectionsIndex(missingBookAttention),
    /non-clear book reviews require one book attention entry/);

  const attentionForClearBook = fixture();
  attentionForClearBook.attention[1].target = {
    kind: "book",
    item_id: "book-pending",
  };
  assert.throws(() => normalizeCorrectionsIndex(attentionForClearBook),
    /clear book reviews cannot have a book attention entry/);
});


test("needs-attention books pin immediately with deterministic title and ID ties", async () => {
  const data = fixture();
  const store = new CorrectionsIndexStore({
    api: tiered({ loadIndex: async () => data }),
  });
  await store.openWorkspace("workspace-1");

  assert.deepEqual(sortedBooks(store.index).map((book) => book.id), [
    "book-herbarium",
    "book-pending",
    "book-empty",
    "book-legacy",
  ]);
  assert.equal(bookNeedsAttention(
    store.index.books.find((book) => book.id === "book-pending"),
    store.index.attention,
  ), true, "image attention pins its parent book");

  const original = data.attention[0];
  const resolved = resolvedEntry(original);
  store.applyAttentionEntry(resolved, "index-r8");
  assert.deepEqual(sortedBooks(store.index).map((book) => book.id), [
    "book-pending",
    "book-herbarium",
    "book-empty",
    "book-legacy",
  ]);

  store.applyAttentionEntry(reopenedEntry(resolved), "index-r9");
  assert.deepEqual(sortedBooks(store.index).map((book) => book.id), [
    "book-herbarium",
    "book-pending",
    "book-empty",
    "book-legacy",
  ]);
});


test("store ignores stale async responses and aborts the superseded request", async () => {
  const first = deferred();
  const second = deferred();
  const calls = [];
  const store = new CorrectionsIndexStore({
    api: tiered({
      loadIndex(options) {
        calls.push(options);
        return calls.length === 1 ? first.promise : second.promise;
      },
    }),
  });

  const opening = store.openWorkspace("workspace-1");
  const refreshing = store.refresh({ reason: "manual" });
  assert.equal(store.snapshot().status, "loading");
  assert.equal(calls[0].signal.aborted, true);

  second.resolve(withRevision(fixture(), "index-newest"));
  await refreshing;
  assert.equal(store.index.revision, "index-newest");

  first.resolve(withRevision(fixture(), "index-stale"));
  await opening;
  assert.equal(store.index.revision, "index-newest");
});


test("refresh preserves owned selection or reports precisely when it disappears", async () => {
  let current = fixture();
  const invalidated = [];
  const store = new CorrectionsIndexStore({
    api: tiered({ loadIndex: async () => current }),
    onSelectionInvalidated: (event) => invalidated.push(event),
  });
  await store.openWorkspace("workspace-1");
  const selection = {
    itemId: "book-herbarium",
    representationId: "scan-herbarium",
    canvasId: "canvas-title",
    artifactId: "capture-title",
    annotationId: null,
  };
  store.setSelection(selection, { ownedByFeature: true });
  current = withRevision(current, "index-r8");
  await store.refresh();
  assert.deepEqual(store.selection, selection);
  assert.equal(invalidated.length, 0);

  current = withRevision(current, "index-r9");
  current.books[0].captures = current.books[0].captures.filter(
    (capture) => capture.artifact_id !== "capture-title");
  await store.refresh();
  assert.equal(store.selection, null);
  assert.equal(invalidated.length, 1);
  assert.equal(invalidated[0].reason, "selection_disappeared");
  assert.equal(invalidated[0].selection.artifactId, "capture-title");

  store.setSelection({
    itemId: "book-herbarium",
    representationId: null,
    canvasId: null,
    artifactId: "artifact-owned-by-another-feature",
    annotationId: null,
  }, { ownedByFeature: false });
  current = withRevision(current, "index-r10");
  await store.refresh();
  assert.equal(store.selection.artifactId, "artifact-owned-by-another-feature",
    "the Books index must not discard another feature's local selection");
});


test("a selection survives a book whose captures have not been read", async () => {
  const data = fixture();
  const invalidated = [];
  const detailRequests = [];
  const store = new CorrectionsIndexStore({
    api: {
      loadIndex: async () => summaryOf(data),
      loadCaptureMarks: async () => marksOf(data),
      // Never answers: this is the window between the index landing and the
      // captures arriving, which every cold open passes through.
      loadDetails: ({ itemIds }) => {
        detailRequests.push(itemIds);
        return new Promise(() => {});
      },
    },
    onSelectionInvalidated: (event) => invalidated.push(event),
  });
  const opening = store.openWorkspace("workspace-1");
  store.setSelection({
    itemId: "book-herbarium",
    representationId: "scan-herbarium",
    canvasId: "canvas-title",
    artifactId: "capture-title",
    annotationId: null,
  }, { ownedByFeature: true });
  await Promise.race([opening, settle()]);

  // Nothing has been read about this book's captures, and unknown is not
  // absent. Answering "gone" here would delete the reader's own selection and
  // report that it disappeared, on every cold open of a capture address.
  assert.equal(store.snapshot().status, "ready");
  assert.equal(store.selection.artifactId, "capture-title");
  assert.deepEqual(invalidated, []);
  assert.ok(detailRequests.flat().includes("book-herbarium"),
    "the store asks for the selected book's captures without being rendered");
});


test("external index notices refresh data without importing another window's selection",
  async () => {
    let current = fixture();
    let onChange;
    let loads = 0;
    const externalChanges = [];
    const store = new CorrectionsIndexStore({
      onExternalChange: (change) => externalChanges.push(change),
      api: tiered({
        async loadIndex() {
          loads += 1;
          return current;
        },
        subscribe(options) {
          onChange = options.onChange;
          return () => {};
        },
      }),
    });
    await store.openWorkspace("workspace-1");
    const selection = {
      itemId: "book-empty",
      representationId: null,
      canvasId: null,
      artifactId: null,
      annotationId: null,
    };
    store.setSelection(selection, { ownedByFeature: true });
    current = withRevision(current, "index-external-r1");
    onChange({
      schema: CORRECTIONS_INDEX_CHANGE_SCHEMA,
      revision: "index-external-r1",
    });
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(loads, 2);
    assert.deepEqual(store.selection, selection);
    assert.deepEqual(externalChanges, [{
      workspaceId: "workspace-1",
      revision: "index-external-r1",
    }]);
  });


test("Books panel renders honest states, accessible chips, and keyboard-focusable captures",
  async () => {
    const data = fixture();
    const store = new CorrectionsIndexStore({
      api: tiered({ loadIndex: async () => data }),
    });
    const harness = miniHarness();
    const navigations = [];
    const controller = new BooksPanelController({
      root: harness.root,
      documentRef: harness.documentRef,
      store,
      onNavigate: (address, metadata) => navigations.push({ address, metadata }),
    }).mount();
    await store.openWorkspace("workspace-1");

    assert.equal(harness.count.textContent, "4");
    assert.equal(harness.list.children.length, 4);
    assert.equal(harness.list.children[0].dataset.bookId, "book-herbarium");
    const firstCaptureList = harness.list.children[0].children[1];
    const captureButtons = firstCaptureList.children.map((item) => item.children[0]);
    assert.deepEqual(captureButtons.map((button) => button.dataset.artifactId), [
      "capture-title", "capture-cover",
    ]);
    assert.match(captureButtons[0].getAttribute("aria-label"),
      /Title page, Image missing/);
    assert.match(textOf(harness.list), /Needs attention/);
    assert.match(textOf(harness.list), /Captured entry/);
    const capturedRow = harness.list.children
      .find((row) => row.dataset.bookId === "book-pending");
    assert.match(capturedRow.children[0].getAttribute("aria-label"),
      /captured entry/);
    assert.match(textOf(harness.list), /No captured images/);
    assert.match(textOf(harness.list), /Pending import/);
    assert.match(textOf(harness.list), /Legacy import/);
    const images = descendants(harness.list, "img");
    assert.ok(images.length >= 2);
    assert.ok(images.every((image) => image.loading === "lazy"));
    assert.ok(images.every((image) => image.decoding === "async"));

    captureButtons[1].emit("click");
    assert.deepEqual(navigations[0], {
      address: {
        itemId: "book-herbarium",
        representationId: "scan-herbarium",
        canvasId: "canvas-cover",
        artifactId: "capture-cover",
        annotationId: null,
      },
      metadata: { source: "books", targetKind: "image" },
    });

    harness.filter.value = "legacy";
    harness.filter.emit("input");
    assert.equal(harness.list.children.length, 1);
    assert.equal(harness.list.children[0].dataset.bookId, "book-legacy");

    harness.filter.emit("keydown", { key: "Escape" });
    assert.equal(harness.filter.value, "");
    assert.equal(harness.list.children.length, 4);
    controller.destroy();
  });


test("navigation-only capture revisions cannot become correction targets", () => {
  const value = fixture().books[0];
  const capture = { ...value.captures[0], revision: "index:abc123" };

  assert.equal(captureCommandTarget(value, capture), null);
});


test("index-only capture clicks carry a validated thumbnail navigation preview",
  async () => {
    const value = fixture();
    const capture = value.books[0].captures.find((candidate) =>
      candidate.artifact_id === "capture-cover");
    capture.revision = "index:cover-preview-r1";
    const store = new CorrectionsIndexStore({
      api: tiered({ loadIndex: async () => value }),
    });
    const harness = miniHarness();
    const navigations = [];
    const controller = new BooksPanelController({
      root: harness.root,
      documentRef: harness.documentRef,
      store,
      onNavigate: (address, metadata) => navigations.push({ address, metadata }),
    }).mount();
    await store.openWorkspace("workspace-1");

    const button = descendants(harness.list, "button").find((candidate) =>
      candidate.dataset.artifactId === "capture-cover");
    button.emit("click");

    assert.deepEqual(navigations[0].metadata, {
      source: "books",
      targetKind: "image",
      navigationPreview: {
        itemId: "book-herbarium",
        representationId: "scan-herbarium",
        canvasId: "canvas-cover",
        artifactId: "capture-cover",
        url: "/api/v1/resources/thumb-cover",
        label: "Front cover",
      },
    });
    assert.equal(Object.isFrozen(navigations[0].metadata.navigationPreview), true);
    controller.destroy();
  });


test("hint capture blur does not clobber an authoritative hydrated target",
  async () => {
    const value = fixture();
    value.books[0].captures[0].revision = "index:abc123";
    const store = new CorrectionsIndexStore({
      api: tiered({ loadIndex: async () => value }),
    });
    const harness = miniHarness();
    let classificationTarget = null;
    const controller = new BooksPanelController({
      root: harness.root,
      documentRef: harness.documentRef,
      store,
      onSelectionTarget(target) { classificationTarget = target; },
    }).mount();
    await store.openWorkspace("workspace-1");
    const button = descendants(harness.list, "button").find((candidate) =>
      candidate.dataset.artifactId === value.books[0].captures[0].artifact_id);

    button.emit("click");
    const hydrated = { artifactId: button.dataset.artifactId, revision: "artifact-r9" };
    classificationTarget = hydrated;
    button.emit("blur");

    assert.equal(classificationTarget, hydrated,
      "classification keeps the artifact detail revision after leaving the hint row");

    store.setSelection({
      itemId: value.books[0].id,
      representationId: value.books[0].representation_id,
      canvasId: value.books[0].captures[0].canvas_id,
      artifactId: null,
      annotationId: "region-1",
    }, { ownedByFeature: false });
    const annotation = { key: "annotation:region-1", revision: "region-r1" };
    classificationTarget = annotation;
    button.emit("blur");
    assert.equal(classificationTarget, annotation,
      "a later Books blur cannot clear an authoritative annotation target");
    controller.destroy();
  });


test("Books panel bounds book and capture DOM while retaining deep selections",
  async () => {
    const source = fixture();
    const base = source.books[0];
    const books = Array.from({ length: 70 }, (_value, index) => ({
      ...clone(base),
      id: `book-batch-${index}`,
      revision: `book-batch-r${index}`,
      title: `Book ${String(index).padStart(2, "0")}`,
      review: {
        revision: `review-batch-r${index}`,
        state: "clear",
        reason: "",
        history_count: 0,
        latest_event: null,
      },
      captures: base.captures.map((capture, captureIndex) => ({
        ...clone(capture),
        artifact_id: `capture-batch-${index}-${captureIndex}`,
        revision: `capture-batch-r${index}-${captureIndex}`,
        capture_order: captureIndex,
        canvas_id: `canvas-batch-${index}-${captureIndex}`,
      })),
    }));
    const indexValue = {
      schema: CORRECTIONS_INDEX_SCHEMA,
      revision: "index-batch-r1",
      books,
      attention: [],
    };
    const store = new CorrectionsIndexStore({
      api: tiered({ loadIndex: async () => indexValue }),
    });
    const harness = miniHarness();
    const controller = new BooksPanelController({
      root: harness.root,
      documentRef: harness.documentRef,
      store,
    }).mount();
    await store.openWorkspace("workspace-1");

    assert.equal(harness.list.children.length, 49,
      "48 book rows plus one load-more row are rendered");
    store.setSelection({
      itemId: "book-batch-69",
      representationId: null,
      canvasId: null,
      artifactId: "capture-batch-69-1",
      annotationId: null,
    }, { ownedByFeature: true });
    assert.ok(harness.list.children.some((row) =>
      row.dataset.bookId === "book-batch-69"));
    // The pinned row is outside the drawn window, so its captures were never
    // asked for until it was pinned; they arrive a round trip later.
    await settle();
    assert.ok(descendants(harness.list, "button").some((button) =>
      button.dataset.artifactId === "capture-batch-69-1"));
    store.setSelection({
      itemId: "book-batch-0",
      representationId: null,
      canvasId: null,
      artifactId: null,
      annotationId: null,
    }, { ownedByFeature: true });
    assert.ok(harness.list.children.some((row) =>
      row.dataset.bookId === "book-batch-47"),
    "leaving a deep selection restores the natural bounded book window");
    assert.equal(harness.list.children.some((row) =>
      row.dataset.bookId === "book-batch-69"), false,
    "the former deep-selection pin does not remain cached");

    const booksMore = descendants(harness.list, "button").find((button) =>
      Object.prototype.hasOwnProperty.call(button.dataset, "booksLoadMore"));
    booksMore.focus();
    booksMore.emit("click");
    assert.equal(harness.documentRef.activeElement.dataset.bookSelect,
      "book-batch-48",
    "the last Books page focuses its first newly revealed row");

    const many = clone(indexValue);
    many.revision = "index-many-captures-r1";
    const captureTemplate = clone(base.captures[1]);
    many.books = [{
      ...clone(base),
      id: "book-many-captures",
      revision: "book-many-captures-r1",
      review: {
        revision: "review-many-captures-r1",
        state: "clear",
        reason: "",
        history_count: 0,
        latest_event: null,
      },
      captures: Array.from({ length: 30 }, (_capture, captureIndex) => ({
        ...clone(captureTemplate),
        artifact_id: `capture-many-${captureIndex}`,
        revision: `capture-many-r${captureIndex}`,
        capture_order: captureIndex,
        canvas_id: `canvas-many-${captureIndex}`,
      })),
    }];
    await store.openWorkspace("workspace-2", { selection: null });
    // Replace the API result for the new workspace and force the bounded load.
    seedStore(store, many);
    assert.equal(descendants(harness.list, "button").filter((button) =>
      button.dataset.artifactId).length, 12);
    store.setSelection({
      itemId: "book-many-captures",
      representationId: "scan-herbarium",
      canvasId: "canvas-many-29",
      artifactId: "capture-many-29",
      annotationId: null,
    }, { ownedByFeature: true });
    assert.ok(descendants(harness.list, "button").some((button) =>
      button.dataset.artifactId === "capture-many-29"));
    assert.equal(descendants(harness.list, "button").filter((button) =>
      button.dataset.artifactId).length, 12);
    store.setSelection({
      itemId: "book-many-captures",
      representationId: "scan-herbarium",
      canvasId: "canvas-many-0",
      artifactId: "capture-many-0",
      annotationId: null,
    }, { ownedByFeature: true });
    assert.ok(descendants(harness.list, "button").some((button) =>
      button.dataset.artifactId === "capture-many-11"),
    "leaving a deep capture restores the natural bounded capture window");
    assert.equal(descendants(harness.list, "button").some((button) =>
      button.dataset.artifactId === "capture-many-29"), false,
    "the former deep capture pin does not remain cached");

    const capturesMore = () => descendants(harness.list, "button")
      .find((button) =>
        button.dataset.capturesLoadMore === "book-many-captures");
    capturesMore().focus();
    capturesMore().emit("click");
    assert.equal(harness.documentRef.activeElement, capturesMore(),
      "an intermediate capture page keeps focus on the replacement pager");
    capturesMore().emit("click");
    assert.equal(harness.documentRef.activeElement.dataset.artifactId,
      "capture-many-24",
    "the last capture page focuses its first newly revealed capture");
    controller.destroy();
  });


test("Books panel paints capture placeholders before visibility-driven thumbnails",
  async () => {
    const observers = thumbnailObserverHarness();
    const store = new CorrectionsIndexStore({
      api: tiered({ loadIndex: async () => fixture() }),
    });
    const harness = miniHarness();
    const controller = new BooksPanelController({
      root: harness.root,
      documentRef: harness.documentRef,
      store,
      thumbnailObserverFactory: observers.factory.bind(observers),
    }).mount();
    await store.openWorkspace("workspace-1");

    const thumbnailButtons = descendants(harness.list, "button")
      .filter((button) => button.dataset.thumbnailState === "pending");
    assert.equal(thumbnailButtons.length, 2);
    for (const button of thumbnailButtons) {
      const image = descendants(button, "img")[0];
      assert.equal(image.hidden, true);
      assert.equal(image.src, undefined,
        "painting a capture row does not start its image request");
    }
    const observer = observers.instances.at(-1);
    assert.equal(observer.options.root, harness.body,
      "the observer is rooted at the scrolling pane, not the expanding list");
    assert.equal(observer.observed.size, 2);

    const visible = thumbnailButtons[0];
    observer.callback([{
      target: visible,
      isIntersecting: false,
      intersectionRatio: 0,
    }]);
    assert.equal(descendants(visible, "img")[0].src, undefined,
      "off-screen observer entries do not begin requests");
    observer.intersect(visible);
    const visibleImage = descendants(visible, "img")[0];
    assert.equal(visibleImage.src,
      "/api/v1/resources/thumb-cover");
    assert.equal(visibleImage.hidden, true);
    assert.equal(visible.dataset.thumbnailState, "loading");
    assert.equal(descendants(visible, "span").some((node) =>
      node.className.includes("capture-thumbnail-placeholder")), true,
    "the placeholder remains until the image has actually loaded");
    visibleImage.emit("load");
    assert.equal(visibleImage.hidden, false);
    assert.equal(visible.dataset.thumbnailState, "loaded");
    assert.equal(descendants(visible, "span").some((node) =>
      node.className.includes("capture-thumbnail-placeholder")), false);

    const focused = thumbnailButtons[1];
    focused.focus();
    const failedImage = descendants(focused, "img")[0];
    assert.equal(failedImage.src,
      "/api/v1/resources/thumb-legacy",
    "keyboard focus hydrates without waiting for an observer callback");
    failedImage.emit("error");
    assert.equal(failedImage.hidden, true);
    assert.equal(focused.dataset.thumbnailState, "error");
    assert.equal(descendants(focused, "span").find((node) =>
      node.className.includes("capture-thumbnail-placeholder")).textContent,
    "Image unavailable");
    assert.equal(observer.observed.size, 0);

    controller.destroy();
    assert.equal(observer.disconnected, true);
    assert.equal(controller.pendingThumbnails.size, 0);
  });


test("Books thumbnails fall back safely when observation is unavailable",
  async () => {
    const store = new CorrectionsIndexStore({
      api: tiered({ loadIndex: async () => fixture() }),
    });
    const harness = miniHarness();
    const controller = new BooksPanelController({
      root: harness.root,
      documentRef: harness.documentRef,
      store,
      thumbnailObserverFactory() {
        throw new Error("observer unavailable");
      },
    }).mount();
    await store.openWorkspace("workspace-1");

    const images = descendants(harness.list, "img");
    assert.deepEqual(images.map((image) => image.src), [
      "/api/v1/resources/thumb-cover",
      "/api/v1/resources/thumb-legacy",
    ]);
    assert.equal(images.every((image) => image.hidden), true,
      "eager fallback still waits for load before revealing images");
    images[0].emit("load");
    assert.equal(images[0].hidden, false);
    controller.destroy();
  });


test("selected captures hydrate eagerly without delaying click navigation",
  async () => {
    const observers = thumbnailObserverHarness();
    const store = new CorrectionsIndexStore({
      api: tiered({ loadIndex: async () => fixture() }),
    });
    const harness = miniHarness();
    const navigationImages = [];
    const controller = new BooksPanelController({
      root: harness.root,
      documentRef: harness.documentRef,
      store,
      thumbnailObserverFactory: observers.factory.bind(observers),
      onNavigate(_address, _metadata) {
        const selected = descendants(harness.list, "button").find((button) =>
          button.getAttribute("aria-pressed") === "true" &&
          button.dataset.artifactId);
        navigationImages.push(selected && descendants(selected, "img")[0].src);
      },
    }).mount();
    await store.openWorkspace("workspace-1");

    const captureButton = (artifactId) => descendants(harness.list, "button")
      .find((button) => button.dataset.artifactId === artifactId);
    store.setSelection({
      itemId: "book-herbarium",
      representationId: "scan-herbarium",
      canvasId: "canvas-cover",
      artifactId: "capture-cover",
      annotationId: null,
    }, { ownedByFeature: true });
    const externallySelected = captureButton("capture-cover");
    assert.equal(descendants(externallySelected, "img")[0].src,
      "/api/v1/resources/thumb-cover");
    assert.equal(descendants(externallySelected, "img")[0].fetchPriority, "high");

    const clicked = captureButton("capture-legacy");
    assert.equal(descendants(clicked, "img")[0].src, undefined);
    clicked.emit("click");
    assert.deepEqual(navigationImages, [undefined],
      "authoritative selection navigation is dispatched before the thumbnail");
    assert.equal(descendants(clicked, "img")[0].src,
      "/api/v1/resources/thumb-legacy");
    assert.equal(descendants(clicked, "img")[0].fetchPriority, "high");
    controller.destroy();
  });


test("capture rerenders restore focused selection and blur reads the live selection",
  async () => {
    const store = new CorrectionsIndexStore({
      api: tiered({ loadIndex: async () => fixture() }),
    });
    const harness = miniHarness();
    const targets = [];
    const controller = new BooksPanelController({
      root: harness.root,
      documentRef: harness.documentRef,
      store,
      onSelectionTarget: (target, detail) => targets.push({ target, detail }),
    }).mount();
    await store.openWorkspace("workspace-1");

    const captureButton = (artifactId) => descendants(harness.list, "button")
      .find((button) => button.dataset.artifactId === artifactId);
    const titleAddress = {
      itemId: "book-herbarium",
      representationId: "scan-herbarium",
      canvasId: "canvas-title",
      artifactId: "capture-title",
      annotationId: null,
    };
    store.setSelection(titleAddress, { ownedByFeature: true });
    const selectedTitle = captureButton("capture-title");
    const nonselectedCover = captureButton("capture-cover");
    nonselectedCover.focus();
    nonselectedCover.emit("blur");
    assert.equal(targets.at(-1).target.artifactId, "capture-title",
      "blurring another capture must restore the live selected capture");
    assert.equal(targets.at(-1).detail.element, selectedTitle);
    assert.equal(targets.at(-1).detail.focused, false);

    const original = nonselectedCover;
    const originalThumbnail = descendants(original, "img")[0];
    let selectionEmits = 0;
    const unsubscribe = store.subscribe(() => { selectionEmits += 1; });
    selectionEmits = 0;
    original.focus();
    original.emit("click");
    const replacement = captureButton("capture-cover");
    assert.equal(replacement, original,
      "selection-only updates preserve the capture DOM node");
    assert.equal(descendants(replacement, "img")[0], originalThumbnail,
      "selection-only updates do not restart a thumbnail request");
    assert.equal(selectionEmits, 1,
      "one capture click publishes one store selection");
    assert.equal(harness.documentRef.activeElement, replacement);
    assert.equal(replacement.getAttribute("aria-pressed"), "true");
    assert.equal(replacement.dataset.itemId, "book-herbarium");
    assert.equal(targets.at(-1).target.artifactId, "capture-cover");
    assert.equal(targets.at(-1).detail.focused, true);

    const legacyFocusCalls = [];
    controller.restoreCaptureFocus({
      querySelectorAll: () => [{
        dataset: {
          itemId: "book-herbarium",
          artifactId: "capture-title",
        },
        focus(options) {
          legacyFocusCalls.push(options);
          if (options) throw new TypeError("focus options unsupported");
        },
      }],
    }, titleAddress);
    assert.equal(legacyFocusCalls.length, 2);
    assert.equal(legacyFocusCalls[1], undefined,
      "focus restoration falls back for browsers without focus options");
    unsubscribe();
    controller.destroy();
  });


test("Books rerenders preserve a still-present focused nonselected capture",
  async () => {
    const store = new CorrectionsIndexStore({
      api: tiered({ loadIndex: async () => fixture() }),
    });
    const harness = miniHarness();
    const targets = [];
    const controller = new BooksPanelController({
      root: harness.root,
      documentRef: harness.documentRef,
      store,
      onSelectionTarget: (target, detail) => targets.push({ target, detail }),
    }).mount();
    await store.openWorkspace("workspace-1");
    store.setSelection({
      itemId: "book-herbarium",
      representationId: "scan-herbarium",
      canvasId: "canvas-title",
      artifactId: "capture-title",
      annotationId: null,
    }, { ownedByFeature: true });

    const captureButton = (artifactId) => descendants(harness.list, "button")
      .find((button) => button.dataset.artifactId === artifactId);
    const original = captureButton("capture-cover");
    original.focus();
    assert.equal(original.getAttribute("aria-pressed"), "false");

    controller.render(store.snapshot());
    const replacement = captureButton("capture-cover");
    assert.equal(replacement, original);
    assert.equal(harness.documentRef.activeElement, original);
    assert.equal(replacement.getAttribute("aria-pressed"), "false");
    assert.equal(store.snapshot().selection.artifactId, "capture-title",
      "restoring focus must not change the selected capture");
    assert.equal(targets.at(-1).target.artifactId, "capture-cover");
    assert.equal(targets.at(-1).detail.focused, true);
    controller.destroy();
  });


test("Books rerenders withdraw a hover contribution left without pointerleave",
  async () => {
    const store = new CorrectionsIndexStore({
      api: tiered({ loadIndex: async () => fixture() }),
    });
    const harness = miniHarness();
    const hot = [];
    const controller = new BooksPanelController({
      root: harness.root,
      documentRef: harness.documentRef,
      store,
      onHotTarget: (target) => hot.push(target),
    }).mount();
    await store.openWorkspace("workspace-1");

    const captureButton = (artifactId) => descendants(harness.list, "button")
      .find((button) => button.dataset.artifactId === artifactId);
    captureButton("capture-title").emit("pointerenter");
    assert.equal(hot.at(-1).artifactId, "capture-title");

    controller.render(store.snapshot());
    assert.equal(hot.at(-1), null,
      "the replaced row cannot stay hot — no pointerleave will fire for it");
    assert.equal(hot.length, 2);

    controller.render(store.snapshot());
    assert.equal(hot.length, 2, "no live hover, nothing to withdraw");

    const replacement = captureButton("capture-title");
    replacement.emit("pointerenter");
    replacement.emit("pointerleave");
    assert.equal(hot.at(-1), null);
    assert.equal(hot.length, 4);
    controller.render(store.snapshot());
    assert.equal(hot.length, 4,
      "a hover already released by pointerleave is not withdrawn again");
    controller.destroy();
  });


test("Books panel does not misrepresent a missing production API as an empty library", () => {
  const store = new CorrectionsIndexStore();
  const harness = miniHarness();
  const controller = new BooksPanelController({
    root: harness.root,
    documentRef: harness.documentRef,
    store,
  }).mount();
  assert.match(textOf(harness.list), /Books unavailable/);
  assert.match(textOf(harness.list), /No Corrections data API is configured/);
  assert.doesNotMatch(textOf(harness.list), /no books/i);
  controller.destroy();
});


test("Books panel distinguishes loading, empty, initial error, and stale refresh error",
  async () => {
    const pending = deferred();
    const empty = fixture();
    empty.revision = "index-empty-r1";
    empty.books = [];
    empty.attention = [];
    let failRefresh = false;
    const store = new CorrectionsIndexStore({
      api: tiered({
        async loadIndex() {
          if (failRefresh) throw new Error("network unavailable");
          return pending.promise;
        },
      }),
    });
    const harness = miniHarness();
    const controller = new BooksPanelController({
      root: harness.root,
      documentRef: harness.documentRef,
      store,
    }).mount();
    const opening = store.openWorkspace("workspace-empty");
    assert.match(textOf(harness.list), /Loading books/);
    pending.resolve(empty);
    await opening;
    assert.match(textOf(harness.list), /This workspace contains no books/);

    failRefresh = true;
    await store.refresh();
    assert.match(textOf(harness.list), /Refresh failed/);
    assert.match(textOf(harness.list), /network unavailable/);
    assert.match(textOf(harness.list), /This workspace contains no books/);
    controller.destroy();

    const failingStore = new CorrectionsIndexStore({
      api: tiered({ loadIndex: async () => { throw new Error("service offline"); } }),
    });
    const failingHarness = miniHarness();
    const failingController = new BooksPanelController({
      root: failingHarness.root,
      documentRef: failingHarness.documentRef,
      store: failingStore,
    }).mount();
    await failingStore.openWorkspace("workspace-error");
    assert.match(textOf(failingHarness.list), /Books could not be loaded/);
    assert.match(textOf(failingHarness.list), /service offline/);
    failingController.destroy();
  });


test("import timestamps are optional, validated, and default to empty", () => {
  const normalized = normalizeCorrectionsIndex(fixture());
  const herbarium = normalized.books.find((book) => book.id === "book-herbarium");
  assert.equal(herbarium.latest_imported_at, "2026-07-28T09:00:00+00:00");
  assert.equal(herbarium.captures.find(
    (capture) => capture.artifact_id === "capture-cover").imported_at,
  "2026-07-28T09:00:00+00:00");
  assert.equal(herbarium.captures.find(
    (capture) => capture.artifact_id === "capture-title").imported_at, "",
  "captures without desktop_import report an empty import time");
  const legacy = normalized.books.find((book) => book.id === "book-legacy");
  assert.equal(legacy.latest_imported_at, "");
  assert.equal(legacy.captures[0].imported_at, "");

  const older = fixture();
  for (const book of older.books) {
    delete book.latest_imported_at;
    for (const capture of book.captures) delete capture.imported_at;
  }
  const normalizedOlder = normalizeCorrectionsIndex(older);
  assert.ok(normalizedOlder.books.every((book) =>
    book.latest_imported_at === "" &&
    book.captures.every((capture) => capture.imported_at === "")),
  "indexes that predate import timestamps must still validate");

  // The engine keeps any timestamp its own Python parser accepted — forms
  // Date.parse cannot read (comma fractions, basic format) arrive verbatim
  // — so the one field degrades to untimed instead of failing the index.
  const unparseable = fixture();
  unparseable.books[0].captures[0].imported_at = "2026-07-28T09:00:00,500";
  unparseable.books[0].latest_imported_at = "20260728T090000";
  const degraded = normalizeCorrectionsIndex(unparseable);
  const degradedBook = degraded.books.find(
    (book) => book.id === "book-herbarium");
  assert.equal(degradedBook.latest_imported_at, "",
    "an engine-accepted basic-format timestamp degrades to untimed");
  assert.equal(degradedBook.captures.find(
    (capture) => capture.artifact_id === "capture-cover").imported_at, "",
  "an engine-accepted comma-fraction timestamp degrades to untimed");

  const unsafeCapture = fixture();
  unsafeCapture.books[0].captures[0].imported_at = "2026-07-28\u0000";
  assert.throws(() => normalizeCorrectionsIndex(unsafeCapture),
    /imported_at: must be a safe string/);

  const oversizedBook = fixture();
  oversizedBook.books[0].latest_imported_at = "9".repeat(65);
  assert.throws(() => normalizeCorrectionsIndex(oversizedBook),
    /latest_imported_at: must be a safe string/);
});


test("unparseable engine-accepted timestamps degrade without failing the panel",
  async () => {
    const data = fixture();
    data.books[0].captures[0].imported_at = "2026-07-28T09:00:00,500";
    data.books[0].latest_imported_at = "2026-07-28T09:00:00,500";
    const store = new CorrectionsIndexStore({
      api: tiered({ loadIndex: async () => data }),
    });
    const harness = miniHarness();
    const controller = new BooksPanelController({
      root: harness.root,
      documentRef: harness.documentRef,
      store,
    }).mount();
    await store.openWorkspace("workspace-1");

    assert.equal(store.snapshot().status, "ready",
      "one unparseable timestamp must not kill the whole Books panel");
    assert.doesNotMatch(textOf(harness.list), /Books could not be loaded/);
    assert.equal(harness.list.children.length, 4);
    await settle();
    const herbarium = store.snapshot().details.get("book-herbarium");
    assert.equal(herbarium.latest_imported_at, "");
    assert.equal(herbarium.captures.find(
      (capture) => capture.artifact_id === "capture-cover").imported_at, "");
    controller.setView("captures");
    await settle();
    assert.equal(store.snapshot().marks.get("book-herbarium")
      .latest_imported_at, "",
    "the capture mark degrades the same way the inlined field did");
    assert.deepEqual(
      harness.list.children.map((row) => row.dataset.bookId),
      ["book-pending", "book-herbarium", "book-legacy"],
      "the degraded item sorts as untimed instead of erroring the view");
    controller.destroy();
  });


test("captures view orders newest import first with untimed items after", () => {
  const source = fixture();
  const index = normalizeCorrectionsIndexSummary(summaryOf(source));
  const marks = new Map(normalizeCaptureMarks(marksOf(source)).marks
    .map((mark) => [mark.item_id, mark]));
  assert.deepEqual(captureBooks(index, marks).map((book) => book.id), [
    "book-pending",
    "book-herbarium",
    "book-legacy",
  ], "newest import first, no-timestamp items after, book-empty excluded");
  assert.deepEqual(attentionBooks(index).map((book) => book.id), [
    "book-herbarium",
    "book-pending",
  ]);
  assert.deepEqual(booksForView(index, "all").map((book) => book.id),
    sortedBooks(index).map((book) => book.id));
  assert.equal(markImportedAt(
    marks, index.books.find((book) => book.id === "book-pending")),
  "2026-07-30T10:15:00+00:00");
  // No marks read yet is not the same as no captures: the view is empty rather
  // than wrongly ordered, and fills in when the marks land.
  assert.deepEqual(captureBooks(index, null), []);
  assert.equal(markImportedAt(null, index.books[0]), "");
});


test("Books panel views compose with the text filter and show honest counts",
  async () => {
    const store = new CorrectionsIndexStore({
      api: tiered({ loadIndex: async () => fixture() }),
    });
    const harness = miniHarness();
    const controller = new BooksPanelController({
      root: harness.root,
      documentRef: harness.documentRef,
      store,
    }).mount();
    await store.openWorkspace("workspace-1");

    const captures = viewButton(harness, "captures");
    const attention = viewButton(harness, "attention");
    const all = viewButton(harness, "all");
    assert.equal(all.getAttribute("aria-pressed"), "true");
    assert.equal(textOf(all).includes("4"), true);
    assert.equal(textOf(attention).includes("2"), true);
    // How many books have captures is the only count that costs a
    // whole-collection read of the captures, so the control says it does not
    // know rather than showing a number it has not earned.
    assert.equal(textOf(captures).includes("—"), true);
    assert.equal(textOf(captures).includes("3"), false);

    captures.emit("click");
    await settle();
    assert.equal(captures.getAttribute("aria-pressed"), "true");
    assert.equal(all.getAttribute("aria-pressed"), "false");
    assert.equal(textOf(captures).includes("3"), true,
      "opening the view is what pays for the count");
    assert.deepEqual(
      harness.list.children.map((row) => row.dataset.bookId),
      ["book-pending", "book-herbarium", "book-legacy"],
    );

    harness.filter.value = "materia";
    harness.filter.emit("input");
    assert.deepEqual(
      harness.list.children.map((row) => row.dataset.bookId),
      ["book-legacy"],
      "the text filter composes with the active view");

    harness.filter.value = "zzz";
    harness.filter.emit("input");
    assert.match(textOf(harness.list), /No matches/);

    harness.filter.value = "";
    harness.filter.emit("input");
    attention.emit("click");
    assert.deepEqual(
      harness.list.children.map((row) => row.dataset.bookId),
      ["book-herbarium", "book-pending"],
    );

    const cleared = fixture();
    cleared.revision = "index-cleared-r1";
    for (const book of cleared.books) book.captures = [];
    controller.setView("captures");
    controller.render({
      status: "ready",
      index: normalizeCorrectionsIndexSummary(summaryOf(cleared)),
      // Read, and naming nobody — the route omits books without captures, so
      // an empty set is the positive claim that every book lost theirs. An
      // unread set would empty this view too, for the wrong reason.
      marks: new Map(),
      details: new Map(),
      selection: null,
      error: null,
    });
    assert.match(textOf(harness.list), /No captures/);
    controller.destroy();
  });


test("prev/next walk the current view order with hard end stops", async () => {
  const store = new CorrectionsIndexStore({
    api: tiered({ loadIndex: async () => fixture() }),
  });
  const harness = miniHarness();
  const navigations = [];
  const controller = new BooksPanelController({
    root: harness.root,
    documentRef: harness.documentRef,
    store,
    onNavigate: (address, metadata) => navigations.push({ address, metadata }),
  }).mount();
  await store.openWorkspace("workspace-1");

  assert.equal(controller.stepSelection(-1).id, "book-legacy",
    "previous with no selection lands on the view's last item");
  controller.setSelection(null);

  const order = ["book-herbarium", "book-pending", "book-empty", "book-legacy"];
  for (const expected of order) {
    assert.equal(controller.stepSelection(1).id, expected);
    assert.equal(store.snapshot().selection.itemId, expected);
  }
  assert.equal(controller.stepSelection(1), null, "no wrap past the end");
  assert.equal(navButton(harness, "next").disabled, true);
  assert.equal(navButton(harness, "previous").disabled, false);
  assert.equal(controller.stepSelection(-1).id, "book-empty");
  assert.deepEqual(navigations.at(-1).metadata,
    { source: "books", targetKind: "book" },
    "stepping navigates through the same path as a click");
  assert.deepEqual(navigations.at(-1).address, {
    itemId: "book-empty",
    representationId: null,
    canvasId: null,
    artifactId: null,
    annotationId: null,
  });

  navButton(harness, "previous").emit("click");
  assert.equal(store.snapshot().selection.itemId, "book-pending");
  controller.destroy();
});


test("when the selected item leaves the view, next takes its former slot",
  async () => {
    const data = fixture();
    const store = new CorrectionsIndexStore({
      api: tiered({ loadIndex: async () => data }),
    });
    const harness = miniHarness();
    const controller = new BooksPanelController({
      root: harness.root,
      documentRef: harness.documentRef,
      store,
    }).mount();
    await store.openWorkspace("workspace-1");
    controller.setView("attention");

    assert.equal(controller.stepSelection(1).id, "book-herbarium");
    const resolved = resolvedEntry(data.attention[0]);
    store.applyAttentionEntry(resolved, "index-r8");
    assert.deepEqual(
      harness.list.children.map((row) => row.dataset.bookId),
      ["book-pending"],
      "the resolved item leaves the Attention view but stays selected");
    assert.equal(store.snapshot().selection.itemId, "book-herbarium");

    assert.equal(controller.stepSelection(1).id, "book-pending",
      "next selects the item now occupying the departed item's slot");

    store.applyAttentionEntry(reopenedEntry(resolved), "index-r9");
    controller.stepSelection(-1);
    assert.equal(store.snapshot().selection.itemId, "book-herbarium");
    store.applyAttentionEntry(resolvedEntry(data.attention[0],
      "review-resolved-r2"), "index-r10");
    assert.equal(controller.stepSelection(-1), null,
      "previous from the departed first slot has nothing before it");
    controller.destroy();
  });


test("previous clamps a remembered slot that outlived a shrunken view",
  async () => {
    let current = fixture();
    const store = new CorrectionsIndexStore({
      api: tiered({ loadIndex: async () => current }),
    });
    const harness = miniHarness();
    const controller = new BooksPanelController({
      root: harness.root,
      documentRef: harness.documentRef,
      store,
    }).mount();
    await store.openWorkspace("workspace-1");
    // The shell echoes selections unowned; slot 3 (book-legacy) is
    // remembered for the "all" view.
    store.setSelection({
      itemId: "book-legacy",
      representationId: null,
      canvasId: null,
      artifactId: null,
      annotationId: null,
    }, { ownedByFeature: false });

    const shrunk = fixture();
    shrunk.revision = "index-shrunk-r1";
    shrunk.books = shrunk.books.filter((book) => book.id === "book-empty");
    shrunk.attention = [];
    current = shrunk;
    await store.refresh();
    assert.equal(store.snapshot().selection.itemId, "book-legacy",
      "the unowned selection survives the refresh that removed its item");
    assert.equal(controller.canStepSelection(-1), true,
      "previous cannot be a dead no-op while next still works");
    assert.equal(controller.stepSelection(-1).id, "book-empty",
      "previous from a vanished slot beyond the view takes the last row");
    controller.destroy();
  });


test("stepping keeps keyboard focus on the stepped row across rebuilds",
  async () => {
    const store = new CorrectionsIndexStore({
      api: tiered({ loadIndex: async () => fixture() }),
    });
    const harness = miniHarness();
    const navigations = [];
    const controller = new BooksPanelController({
      root: harness.root,
      documentRef: harness.documentRef,
      store,
      onNavigate: (address, metadata) => navigations.push({ address, metadata }),
    }).mount();
    await store.openWorkspace("workspace-1");
    const rowButton = (itemId) => descendants(harness.list, "button")
      .find((button) => button.dataset.bookSelect === itemId) || null;

    const first = controller.stepSelection(1);
    assert.equal(first.id, "book-herbarium");
    assert.deepEqual(navigations.at(-1).address, {
      itemId: "book-herbarium",
      representationId: "scan-herbarium",
      canvasId: "canvas-title",
      artifactId: "capture-title",
      annotationId: null,
    }, "stepping onto a captured item opens its first capture");
    assert.equal(navigations.at(-1).metadata.targetKind, "image");
    assert.equal(harness.documentRef.activeElement,
      rowButton("book-herbarium"),
      "the selection rebuild puts DOM focus on the stepped-to row");

    // The shell echoes the navigation back as an unowned selection, which
    // rebuilds the list a second time before the next keystroke.
    store.setSelection(navigations.at(-1).address, { ownedByFeature: false });
    assert.equal(harness.documentRef.activeElement,
      rowButton("book-herbarium"),
      "the echo rebuild preserves the focused row instead of dropping it");

    const second = controller.stepSelection(1);
    assert.equal(second.id, "book-pending");
    assert.equal(navigations.at(-1).address.artifactId, "capture-pending");
    assert.equal(harness.documentRef.activeElement, rowButton("book-pending"),
      "the second step still resolves and moves focus onward");

    // Stepping also recovers from focus sitting on a capture thumbnail.
    descendants(harness.list, "button")
      .find((button) => button.dataset.artifactId === "capture-pending")
      .focus();
    const third = controller.stepSelection(1);
    assert.equal(third.id, "book-empty");
    assert.equal(navigations.at(-1).metadata.targetKind, "book",
      "items without captures keep the bare book address");
    assert.equal(harness.documentRef.activeElement, rowButton("book-empty"));

    navButton(harness, "next").emit("click");
    assert.equal(store.snapshot().selection.itemId, "book-legacy");
    assert.equal(harness.documentRef.activeElement, rowButton("book-legacy"),
      "the ‹/› buttons hand focus to the stepped-to row too");

    // Renders that were not caused by a step never steal outside focus.
    harness.filter.focus();
    controller.render(store.snapshot());
    assert.equal(harness.documentRef.activeElement, harness.filter,
      "an external refresh leaves focus outside the list alone");
    controller.destroy();
  });

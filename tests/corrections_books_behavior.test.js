const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const {
  BooksPanelController,
  CORRECTIONS_INDEX_CHANGE_SCHEMA,
  CORRECTIONS_INDEX_SCHEMA,
  CorrectionsContractError,
  CorrectionsIndexStore,
  bookNeedsAttention,
  normalizeCorrectionsIndex,
  sortedBooks,
} = require("../tools/whl_explorer/static/corrections/books");


const fixturePath = path.join(
  __dirname, "fixtures", "corrections_books_index_v1.json");


function fixture() {
  return JSON.parse(fs.readFileSync(fixturePath, "utf8"));
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
  const nodes = new Map([
    ["[data-books-count]", count],
    ["[data-books-filter]", filter],
    ["[data-books-list]", list],
  ]);
  const root = {
    ownerDocument: documentRef,
    querySelector(selector) { return nodes.get(selector) || null; },
  };
  return { count, documentRef, filter, list, root };
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
  wrongSchema.schema = "librarytool.corrections-index/2";
  assert.throws(() => normalizeCorrectionsIndex(wrongSchema),
    (error) => error instanceof CorrectionsContractError &&
      error.path === "$.schema");

  const unknown = fixture();
  unknown.books[0].legacy_path = "C:/private/book";
  assert.throws(() => normalizeCorrectionsIndex(unknown),
    /legacy_path: is not a recognized field/);

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
});


test("needs-attention books pin immediately with deterministic title and ID ties", async () => {
  const data = fixture();
  const store = new CorrectionsIndexStore({
    api: { loadIndex: async () => data },
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
    api: {
      loadIndex(options) {
        calls.push(options);
        return calls.length === 1 ? first.promise : second.promise;
      },
    },
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
    api: { loadIndex: async () => current },
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


test("external index notices refresh data without importing another window's selection",
  async () => {
    let current = fixture();
    let onChange;
    let loads = 0;
    const store = new CorrectionsIndexStore({
      api: {
        async loadIndex() {
          loads += 1;
          return current;
        },
        subscribe(options) {
          onChange = options.onChange;
          return () => {};
        },
      },
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
  });


test("Books panel renders honest states, accessible chips, and keyboard-focusable captures",
  async () => {
    const data = fixture();
    const store = new CorrectionsIndexStore({
      api: { loadIndex: async () => data },
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


test("capture rerenders restore focused selection and blur reads the live selection",
  async () => {
    const store = new CorrectionsIndexStore({
      api: { loadIndex: async () => fixture() },
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
    original.focus();
    original.emit("click");
    const replacement = captureButton("capture-cover");
    assert.notEqual(replacement, original, "selection rerenders the capture row");
    assert.equal(harness.documentRef.activeElement, replacement,
      "the replacement for the selected capture recovers DOM focus");
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
    controller.destroy();
  });


test("Books rerenders preserve a still-present focused nonselected capture",
  async () => {
    const store = new CorrectionsIndexStore({
      api: { loadIndex: async () => fixture() },
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
    assert.notEqual(replacement, original);
    assert.equal(harness.documentRef.activeElement, replacement);
    assert.equal(replacement.getAttribute("aria-pressed"), "false");
    assert.equal(store.snapshot().selection.artifactId, "capture-title",
      "restoring focus must not change the selected capture");
    assert.equal(targets.at(-1).target.artifactId, "capture-cover");
    assert.equal(targets.at(-1).detail.focused, true);
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
      api: {
        async loadIndex() {
          if (failRefresh) throw new Error("network unavailable");
          return pending.promise;
        },
      },
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
      api: { loadIndex: async () => { throw new Error("service offline"); } },
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

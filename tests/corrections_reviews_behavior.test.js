const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const {
  CORRECTIONS_REVIEW_RESULT_SCHEMA,
  CORRECTIONS_REVIEW_SCHEMA,
  CorrectionsIndexStore,
  CorrectionsReviewConflictError,
  normalizeCorrectionsIndex,
  normalizeReviewDocument,
  selectionAddressFromTarget,
} = require("../tools/whl_explorer/static/corrections/books");
const {
  ReviewsPanelController,
  createBooksAttentionFeature,
  filterReviewEntries,
  nextReviewEntry,
  reviewEntryLabel,
} = require("../tools/whl_explorer/static/corrections/reviews");


const fixturePath = path.join(
  __dirname, "fixtures", "corrections_books_index_v2.json");


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


function reviewEvent(overrides = {}) {
  return {
    operation_id: "op-mark-test",
    action: "attention.mark",
    actor_id: "curator-test",
    occurred_at: "2026-07-22T18:00:00Z",
    before_state: "clear",
    after_state: "needs_attention",
    reason: "Verify the title page",
    comment: "",
    ...overrides,
  };
}


function transitionedEntry(entryValue, action, revision) {
  const entry = clone(entryValue);
  const resolving = action === "resolve";
  entry.review = {
    revision,
    state: resolving ? "resolved" : "needs_attention",
    reason: entry.review.reason,
    history_count: entry.review.history_count + 1,
    latest_event: reviewEvent({
      operation_id: `op-${action}-${revision}`,
      action: resolving ? "attention.resolve" : "attention.reopen",
      occurred_at: resolving
        ? "2026-07-22T20:00:00Z" : "2026-07-22T20:05:00Z",
      before_state: resolving ? "needs_attention" : "resolved",
      after_state: resolving ? "resolved" : "needs_attention",
      reason: entry.review.reason,
      comment: resolving ? "Corrected" : "Needs another pass",
    }),
  };
  return entry;
}


function mutationResult(entry, revision, data = fixture()) {
  return {
    schema: CORRECTIONS_REVIEW_RESULT_SCHEMA,
    index_revision: revision,
    entry,
    index: indexWithEntry(data, entry, revision),
  };
}


function indexWithEntry(data, entry, revision) {
  const result = clone(data);
  result.revision = revision;
  const position = result.attention.findIndex(
    (candidate) => candidate.key === entry.key);
  result.attention[position] = clone(entry);
  if (entry.target.kind === "book") {
    const book = result.books.find((candidate) => candidate.id === entry.target.item_id);
    book.review = clone(entry.review);
  }
  return result;
}


function fullReviewDocument(entry) {
  const history = [];
  if (entry.review.state === "needs_attention") {
    history.push(reviewEvent({
      operation_id: entry.review.latest_event.operation_id,
      actor_id: entry.review.latest_event.actor_id,
      occurred_at: entry.review.latest_event.occurred_at,
      reason: entry.review.reason,
      comment: entry.review.latest_event.comment,
    }));
  } else {
    history.push(
      reviewEvent({
        operation_id: "op-region-mark",
        reason: entry.review.reason,
      }),
      reviewEvent({
        operation_id: entry.review.latest_event.operation_id,
        action: "attention.resolve",
        occurred_at: entry.review.latest_event.occurred_at,
        before_state: "needs_attention",
        after_state: "resolved",
        reason: entry.review.reason,
        comment: entry.review.latest_event.comment,
      }),
    );
  }
  return {
    schema: CORRECTIONS_REVIEW_SCHEMA,
    target: clone(entry.target),
    review: {
      revision: entry.review.revision,
      state: entry.review.state,
      reason: entry.review.reason,
      history,
    },
  };
}


test("full review documents accept the engine audit-history capacity", () => {
  const history = Array.from({ length: 10_001 }, (_, index) => {
    const mark = index === 0;
    const resolve = index % 2 === 1;
    return reviewEvent({
      operation_id: `audit-op-${index}`,
      action: mark
        ? "attention.mark"
        : resolve
          ? "attention.resolve"
          : "attention.reopen",
      before_state: mark
        ? "clear"
        : resolve
          ? "needs_attention"
          : "resolved",
      after_state: resolve ? "resolved" : "needs_attention",
    });
  });
  const document = normalizeReviewDocument({
    schema: CORRECTIONS_REVIEW_SCHEMA,
    target: { kind: "book", item_id: "book-1" },
    review: {
      revision: "review-large-r1",
      state: "needs_attention",
      reason: "Verify the title page",
      history,
    },
  });

  assert.equal(document.review.history.length, 10_001);
});


class MiniClassList {
  constructor() { this.values = new Set(); }
  add(value) { this.values.add(value); }
  remove(value) { this.values.delete(value); }
}


class MiniNode {
  constructor(tagName, documentRef = null) {
    this.tagName = tagName.toUpperCase();
    this.ownerDocument = documentRef;
    this.children = [];
    this.attributes = new Map();
    this.listeners = new Map();
    this.classList = new MiniClassList();
    this.dataset = {};
    this.textContent = "";
    this.className = "";
    this.value = "";
    this.checked = false;
    this.disabled = false;
  }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; }
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
      preventDefault() { this.defaultPrevented = true; },
      ...overrides,
    };
    for (const listener of this.listeners.get(type) || []) listener(event);
    return event;
  }
}


function reviewHarness(options = {}) {
  const documentRef = {
    createElement(name) { return new MiniNode(name, documentRef); },
  };
  const panel = new MiniNode("div", documentRef);
  const count = new MiniNode("span", documentRef);
  const booksCount = new MiniNode("span", documentRef);
  const booksFilter = new MiniNode("input", documentRef);
  const booksList = new MiniNode("ul", documentRef);
  const root = {
    ownerDocument: documentRef,
    querySelector(selector) {
      const nodes = {
        "[data-tray-panel='reviews']": panel,
        "[data-review-count]": count,
        "[data-books-count]": booksCount,
        "[data-books-filter]": booksFilter,
        "[data-books-list]": booksList,
      };
      return nodes[selector] || null;
    },
  };
  return {
    booksCount, booksFilter, booksList, count, documentRef, panel, root, ...options,
  };
}


function descendants(node) {
  return node.children.flatMap((child) => [child, ...descendants(child)]);
}


function byClass(node, className) {
  return descendants(node).filter((candidate) =>
    String(candidate.className).split(/\s+/).includes(className));
}


function textOf(node) {
  return [node.textContent, ...node.children.map(textOf)].join(" ");
}


test("review filtering and traversal support precise book, image, and region targets", () => {
  const index = normalizeCorrectionsIndex(fixture());
  const open = filterReviewEntries(index);
  assert.deepEqual(open.map((entry) => entry.key), [
    "attention-book-herbarium",
    "attention-image-pending",
  ]);
  assert.deepEqual(
    filterReviewEntries(index, { scope: "image" }).map((entry) => entry.key),
    ["attention-image-pending"],
  );
  assert.deepEqual(
    filterReviewEntries(index, { scope: "region" }).map((entry) => entry.key),
    [],
  );
  assert.deepEqual(
    filterReviewEntries(index, {
      scope: "region",
      showResolved: true,
    }).map((entry) => entry.key),
    ["attention-region-herbarium"],
  );
  assert.equal(nextReviewEntry(
    index, "attention-book-herbarium").key, "attention-image-pending");
  assert.equal(nextReviewEntry(
    index, "attention-image-pending").key, "attention-book-herbarium");

  const region = index.attention.find(
    (entry) => entry.target.kind === "region");
  assert.deepEqual(selectionAddressFromTarget(region.target), {
    itemId: "book-herbarium",
    representationId: "scan-herbarium",
    canvasId: "canvas-cover",
    artifactId: "capture-cover",
    annotationId: "region-illustration-1",
  });
  assert.match(reviewEntryLabel(index, region),
    /An Herbarium · Region · region-illustration-1/);
});


test("resolve and reopen send CAS revisions, actor, operation, and comment", async () => {
  const data = fixture();
  const calls = [];
  const api = {
    loadIndex: async () => data,
    async resolveReview(options) {
      calls.push({ action: "resolve", options });
      return mutationResult(
        transitionedEntry(data.attention[0], "resolve", "review-book-r3"),
        "index-r8",
      );
    },
    async reopenReview(options) {
      calls.push({ action: "reopen", options });
      return mutationResult(
        transitionedEntry(options.entry || transitionedEntry(
          data.attention[0], "resolve", "review-book-r3"),
        "reopen", "review-book-r4"),
        "index-r9",
      );
    },
  };
  const store = new CorrectionsIndexStore({ api });
  await store.openWorkspace("workspace-1");
  const original = store.index.attention[0];
  await store.transitionReview("resolve", {
    entry: original,
    actorId: "curator-9",
    operationId: "operation-resolve-1",
    comment: "Title verified",
  });
  assert.deepEqual(calls[0], {
    action: "resolve",
    options: {
      target: original.target,
      expectedRevision: "review-book-r2",
      actorId: "curator-9",
      operationId: "operation-resolve-1",
      comment: "Title verified",
      signal: undefined,
    },
  });
  assert.equal(store.index.revision, "index-r8");
  assert.equal(store.index.attention[0].review.state, "resolved");
  assert.equal(store.index.books[0].review.state, "resolved");

  const resolved = store.index.attention[0];
  api.reopenReview = async (options) => {
    calls.push({ action: "reopen", options });
    return mutationResult(
      transitionedEntry(resolved, "reopen", "review-book-r4"),
      "index-r9",
    );
  };
  await store.transitionReview("reopen", {
    entry: resolved,
    actorId: "curator-9",
    operationId: "operation-reopen-1",
    comment: "Second look requested",
  });
  assert.equal(calls[1].options.expectedRevision, "review-book-r3");
  assert.equal(calls[1].options.comment, "Second look requested");
  assert.equal(store.index.attention[0].review.state, "needs_attention");
  assert.equal(store.index.revision, "index-r9");
});

test("trusted engine reviews do not require or transmit a renderer actor",
  async () => {
    const data = fixture();
    const resolved = transitionedEntry(
      data.attention[0], "resolve", "review-book-r3");
    let mutation;
    const api = {
      trustedActor: true,
      loadIndex: async () => data,
      async resolveReview(options) {
        mutation = options;
        return mutationResult(resolved, "index-r8");
      },
      reopenReview: async () => {
        throw new Error("not expected");
      },
    };
    const store = new CorrectionsIndexStore({ api });
    await store.openWorkspace("workspace-1");
    const controller = new ReviewsPanelController({
      root: { ownerDocument: {}, querySelector: () => null },
      documentRef: {},
      store,
      operationIdFactory: () => "trusted-resolve-op",
    });

    assert.deepEqual(controller.actionCapability(), {
      enabled: true,
      reason: "",
    });
    await controller.transition(
      store.index.attention[0],
      "resolve",
      "Verified locally",
    );
    assert.deepEqual(mutation, {
      target: data.attention[0].target,
      expectedRevision: "review-book-r2",
      operationId: "trusted-resolve-op",
      comment: "Verified locally",
      signal: undefined,
    });
    assert.equal(Object.prototype.hasOwnProperty.call(mutation, "actorId"),
      false);
  });

test("partial mutation results reload the complete index before convergence",
  async () => {
    const data = fixture();
    const resolved = transitionedEntry(
      data.attention[0], "resolve", "review-book-r3");
    const converged = indexWithEntry(data, resolved, "index-r8");
    converged.books[1].title = "Concurrent title correction";
    converged.books[1].revision = "book-concurrent-r2";
    let loads = 0;
    const store = new CorrectionsIndexStore({
      api: {
        async loadIndex() {
          loads += 1;
          return loads === 1 ? data : converged;
        },
        async resolveReview() {
          return {
            schema: CORRECTIONS_REVIEW_RESULT_SCHEMA,
            index_revision: "index-r8",
            entry: resolved,
          };
        },
      },
    });
    await store.openWorkspace("workspace-1");

    await store.transitionReview("resolve", {
      entry: store.index.attention[0],
      actorId: "curator-9",
      operationId: "operation-resolve-partial",
      comment: "Verified",
    });

    assert.equal(loads, 2);
    assert.equal(store.index.revision, "index-r8");
    assert.equal(store.index.attention[0].review.state, "resolved");
    assert.equal(store.index.books[1].title, "Concurrent title correction");
  });

test("a review mutation re-converges after an overlapping refresh", async () => {
  const data = fixture();
  const mutation = deferred();
  const refreshed = deferred();
  const resolved = transitionedEntry(
    data.attention[0], "resolve", "review-stale-r3");
  const overlappingIndex = clone(data);
  overlappingIndex.revision = "index-refresh-r9";
  overlappingIndex.books[1].title = "Title from the overlapping refresh";
  const convergedIndex = indexWithEntry(
    overlappingIndex,
    resolved,
    "index-converged-r10",
  );
  let loads = 0;
  const store = new CorrectionsIndexStore({
    api: {
      loadIndex() {
        loads += 1;
        if (loads === 1) return Promise.resolve(data);
        if (loads === 2) return refreshed.promise;
        return Promise.resolve(convergedIndex);
      },
      resolveReview: () => mutation.promise,
    },
  });
  await store.openWorkspace("workspace-1");

  const mutating = store.transitionReview("resolve", {
    entry: store.index.attention[0],
    actorId: "curator-1",
    operationId: "resolve-racing-refresh",
    comment: "",
  });
  await Promise.resolve();
  const refreshing = store.refresh({ reason: "external" });
  refreshed.resolve(overlappingIndex);
  await refreshing;
  mutation.resolve(mutationResult(resolved, "index-stale-r3"));
  await mutating;

  assert.equal(loads, 3);
  assert.equal(store.index.revision, "index-converged-r10");
  assert.equal(store.index.attention[0].review.state, "resolved");
  assert.equal(store.index.books[1].title,
    "Title from the overlapping refresh");
});

test("a review mutation cannot overwrite a newly opened workspace", async () => {
  const data = fixture();
  const mutation = deferred();
  const workspaceB = deferred();
  const resolved = transitionedEntry(
    data.attention[0], "resolve", "review-workspace-a-r3");
  const workspaceBIndex = clone(data);
  workspaceBIndex.revision = "index-workspace-b-r1";
  workspaceBIndex.books[1].title = "Workspace B";
  const store = new CorrectionsIndexStore({
    api: {
      loadIndex({ workspaceId }) {
        return workspaceId === "workspace-a"
          ? Promise.resolve(data)
          : workspaceB.promise;
      },
      resolveReview: () => mutation.promise,
    },
  });
  await store.openWorkspace("workspace-a");

  const mutating = store.transitionReview("resolve", {
    entry: store.index.attention[0],
    actorId: "curator-1",
    operationId: "resolve-workspace-a",
    comment: "",
  });
  await Promise.resolve();
  const openingB = store.openWorkspace("workspace-b");
  workspaceB.resolve(workspaceBIndex);
  await openingB;
  mutation.resolve(mutationResult(resolved, "index-workspace-a-r3"));
  await mutating;

  assert.equal(store.workspaceId, "workspace-b");
  assert.equal(store.index.revision, "index-workspace-b-r1");
  assert.equal(store.index.books[1].title, "Workspace B");
});

test("concurrent review requests serialize so every committed result converges",
  async () => {
    const data = fixture();
    const firstMutation = deferred();
    const secondMutation = deferred();
    let mutations = 0;
    const store = new CorrectionsIndexStore({
      api: {
        loadIndex: async () => data,
        resolveReview() {
          mutations += 1;
          return mutations === 1 ? firstMutation.promise : secondMutation.promise;
        },
      },
    });
    await store.openWorkspace("workspace-1");
    const firstSource = store.index.attention[0];
    const secondSource = store.index.attention[1];

    const first = store.transitionReview("resolve", {
      entry: firstSource,
      actorId: "curator-1",
      operationId: "resolve-first",
      comment: "",
    });
    const second = store.transitionReview("resolve", {
      entry: secondSource,
      actorId: "curator-2",
      operationId: "resolve-second",
      comment: "",
    });
    await Promise.resolve();
    assert.equal(mutations, 1,
      "the second request waits until the first has converged");
    const firstEntry = transitionedEntry(
      firstSource,
      "resolve",
      "review-first-r3",
    );
    firstMutation.resolve(mutationResult(firstEntry, "index-first-r8"));
    await first;
    await Promise.resolve();
    assert.equal(mutations, 2);
    const firstIndex = indexWithEntry(data, firstEntry, "index-first-r8");
    const secondEntry = transitionedEntry(
      secondSource,
      "resolve",
      "review-second-r4",
    );
    secondMutation.resolve(
      mutationResult(secondEntry, "index-second-r9", firstIndex),
    );
    await second;

    assert.equal(store.index.revision, "index-second-r9");
    assert.equal(store.index.attention[0].review.revision, "review-first-r3");
    assert.equal(store.index.attention[1].review.revision, "review-second-r4");
  });


test("a CAS conflict refreshes the queue instead of overwriting newer state", async () => {
  const data = fixture();
  const externalEntry = transitionedEntry(
    data.attention[0], "resolve", "review-external-r1");
  const external = indexWithEntry(data, externalEntry, "index-external-r1");
  let loads = 0;
  const store = new CorrectionsIndexStore({
    api: {
      async loadIndex() {
        loads += 1;
        return loads === 1 ? data : external;
      },
      async resolveReview() {
        const error = new Error("revision mismatch");
        error.status = 409;
        error.code = "target_revision_conflict";
        throw error;
      },
    },
  });
  await store.openWorkspace("workspace-1");
  await assert.rejects(
    () => store.transitionReview("resolve", {
      entry: store.index.attention[0],
      actorId: "curator-1",
      operationId: "operation-conflict-1",
      comment: "",
    }),
    (error) => error instanceof CorrectionsReviewConflictError,
  );
  assert.equal(loads, 2);
  assert.equal(store.index.revision, "index-external-r1");
  assert.equal(store.index.attention[0].review.state, "resolved");
});


test("controller can advance to the next open target after a successful resolve",
  async () => {
    const data = fixture();
    const resolved = transitionedEntry(
      data.attention[0], "resolve", "review-book-r3");
    const api = {
      loadIndex: async () => data,
      resolveReview: async () => mutationResult(resolved, "index-r8"),
      reopenReview: async () => {
        throw new Error("not expected");
      },
    };
    const store = new CorrectionsIndexStore({ api });
    await store.openWorkspace("workspace-1");
    const navigations = [];
    const controller = new ReviewsPanelController({
      root: { ownerDocument: {}, querySelector: () => null },
      documentRef: {},
      store,
      actorIdProvider: "curator-advance",
      operationIdFactory: () => "operation-advance-1",
      onNavigate: (address, metadata) => navigations.push({ address, metadata }),
    });
    const result = await controller.transition(
      store.index.attention[0], "resolve", "Checked");
    assert.ok(result);
    assert.deepEqual(navigations, [{
      address: {
        itemId: "book-pending",
        representationId: null,
        canvasId: null,
        artifactId: "capture-pending",
        annotationId: null,
      },
      metadata: {
        source: "reviews-advance",
        targetKind: "image",
        reviewKey: "attention-image-pending",
      },
    }]);
  });


test("audit history is fetched lazily, validated, and cached by review revision",
  async () => {
    const data = fixture();
    let auditCalls = 0;
    const store = new CorrectionsIndexStore({
      api: {
        loadIndex: async () => data,
        async getReview({ target }) {
          auditCalls += 1;
          const entry = data.attention.find(
            (candidate) => candidate.target.item_id === target.item_id &&
              candidate.target.kind === target.kind);
          return fullReviewDocument(entry);
        },
      },
    });
    await store.openWorkspace("workspace-1");
    const root = { ownerDocument: {}, querySelector: () => null };
    const controller = new ReviewsPanelController({
      root,
      documentRef: {},
      store,
    });
    const entry = store.index.attention[0];
    const first = await controller.loadAudit(entry);
    const second = await controller.loadAudit(entry);
    assert.equal(auditCalls, 1);
    assert.strictEqual(first, second);
    assert.equal(first.history.length, 1);
    assert.equal(first.history[0].actor_id, "curator-1");
  });


test("Review panel exposes focused deep links and honest action capabilities", async () => {
  const data = fixture();
  const store = new CorrectionsIndexStore({
    api: {
      loadIndex: async () => data,
      getReview: async ({ target }) => fullReviewDocument(
        data.attention.find((entry) =>
          entry.target.kind === target.kind &&
          entry.target.item_id === target.item_id)),
    },
  });
  const harness = reviewHarness();
  const navigations = [];
  const controller = new ReviewsPanelController({
    root: harness.root,
    documentRef: harness.documentRef,
    store,
    onNavigate: (address, metadata) => navigations.push({ address, metadata }),
  }).mount();
  await store.openWorkspace("workspace-1");

  assert.equal(harness.count.textContent, "2");
  assert.match(textOf(harness.panel), /2 open reviews/);
  assert.equal(byClass(harness.panel, "review-entry").length, 2);
  assert.equal(byClass(harness.panel, "review-resolve").length, 2);
  assert.ok(byClass(harness.panel, "review-resolve").every(
    (button) => button.disabled),
  "mutations are disabled until an actor and write API are injected");
  assert.ok(byClass(harness.panel, "review-resolve").every(
    (button) => /unavailable|identity/i.test(button.getAttribute("title"))));

  const open = byClass(harness.panel, "review-open")[1];
  open.emit("click");
  assert.deepEqual(navigations[0], {
    address: {
      itemId: "book-pending",
      representationId: null,
      canvasId: null,
      artifactId: "capture-pending",
      annotationId: null,
    },
    metadata: {
      source: "reviews",
      targetKind: "image",
      reviewKey: "attention-image-pending",
    },
  });

  const scope = byClass(harness.panel, "review-filter")[0];
  scope.value = "region";
  scope.emit("change");
  assert.match(textOf(harness.panel), /Nothing in this target filter needs attention/);
  const showResolved = descendants(harness.panel).find(
    (node) => Object.prototype.hasOwnProperty.call(
      node.dataset, "reviewShowResolved"));
  showResolved.checked = true;
  showResolved.emit("change");
  assert.equal(byClass(harness.panel, "review-entry").length, 1);
  assert.match(textOf(harness.panel), /Check whether the boxed mark is an illustration/);
  controller.destroy();
});


test("feature factory shares one per-window store and never falls back to sample data",
  async () => {
    const data = fixture();
    const harness = reviewHarness();
    const navigations = [];
    const feature = createBooksAttentionFeature({
      root: harness.root,
      documentRef: harness.documentRef,
      api: { loadIndex: async () => data },
      onNavigate: (address) => navigations.push(address),
    });
    feature.mount();
    await feature.setContext({
      workspace_id: "workspace-1",
      item_id: "book-empty",
    });
    assert.strictEqual(feature.books.store, feature.reviews.store);
    assert.strictEqual(feature.store, feature.books.store);
    assert.equal(feature.store.selection.itemId, "book-empty");
    assert.equal(feature.store.snapshot().status, "ready");
    feature.destroy();

    const unavailableHarness = reviewHarness();
    const unavailable = createBooksAttentionFeature({
      root: unavailableHarness.root,
      documentRef: unavailableHarness.documentRef,
    });
    unavailable.mount();
    await unavailable.setContext({
      workspace_id: "workspace-1",
      item_id: "book-empty",
    });
    assert.equal(unavailable.store.snapshot().status, "unavailable");
    assert.match(textOf(unavailableHarness.panel), /Review queue unavailable/);
    assert.match(textOf(unavailableHarness.booksList), /Books unavailable/);
    unavailable.destroy();
  });


test("feature external convergence refreshes the local capture command target",
  async () => {
    let current = fixture();
    let notify;
    const targets = [];
    const changes = [];
    const harness = reviewHarness();
    const feature = createBooksAttentionFeature({
      root: harness.root,
      documentRef: harness.documentRef,
      api: {
        async loadIndex() { return current; },
        subscribe(options) {
          notify = options.onChange;
          return () => { notify = null; };
        },
      },
      onSelectionTarget: (target, detail) => targets.push({ target, detail }),
      onExternalChange: (change) => changes.push(change),
    });
    await feature.setContext({
      workspace_id: "workspace-1",
      item_id: "book-herbarium",
      representation_id: "scan-herbarium",
      canvas_id: "canvas-cover",
      artifact_id: "capture-cover",
    });
    assert.equal(targets.at(-1).target.revision, "capture-cover-r2");

    current = clone(current);
    current.revision = "index-external-r8";
    current.books[0].revision = "book-herbarium-r4";
    current.books[0].captures.find(
      (capture) => capture.artifact_id === "capture-cover",
    ).revision = "capture-cover-r3";
    notify({
      schema: "librarytool.corrections-index-change/1",
      revision: "index-external-r8",
    });
    await new Promise((resolve) => setImmediate(resolve));

    assert.equal(feature.store.selection.artifactId, "capture-cover");
    assert.equal(targets.at(-1).target.revision, "capture-cover-r3");
    assert.equal(targets.at(-1).detail.source, "external");
    assert.deepEqual(changes, [{
      workspaceId: "workspace-1",
      revision: "index-external-r8",
    }]);
    feature.destroy();
    assert.equal(notify, null);
  });

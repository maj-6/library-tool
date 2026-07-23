(function installCorrectionsBooks(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else Object.assign(root.LibraryToolCorrections ||= {}, api);
})(typeof globalThis !== "undefined" ? globalThis : this, function booksFactory() {
  "use strict";

  const CORRECTIONS_INDEX_SCHEMA = "librarytool.corrections-index/1";
  const CORRECTIONS_REVIEW_SCHEMA = "librarytool.corrections-review/1";
  const CORRECTIONS_REVIEW_RESULT_SCHEMA =
    "librarytool.corrections-review-result/1";
  const CORRECTIONS_INDEX_CHANGE_SCHEMA =
    "librarytool.corrections-index-change/1";
  const IDENTIFIER_RE = /^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,255}$/;
  const REVIEW_STATES = new Set(["clear", "needs_attention", "resolved"]);
  const REVIEW_ACTIONS = new Set([
    "attention.mark", "attention.resolve", "attention.reopen", "attention.clear",
  ]);
  const TARGET_KINDS = new Set(["book", "image", "region"]);
  const IMAGE_CATEGORIES = new Set([
    "title_page", "cover", "spine", "content_specimen", "other",
  ]);
  const IMPORT_STATES = new Set([
    "ready", "pending", "legacy", "partial", "missing", "unavailable",
  ]);
  const RESOURCE_STATES = new Set(["available", "missing", "unavailable"]);
  const FRESHNESS_STATES = new Set(["current", "stale", "untracked"]);
  const CATEGORY_PRESENTATION = Object.freeze({
    title_page: Object.freeze({ label: "Title page", icon: "▤" }),
    cover: Object.freeze({ label: "Cover", icon: "▣" }),
    spine: Object.freeze({ label: "Spine", icon: "▥" }),
    content_specimen: Object.freeze({ label: "Content specimen", icon: "≡" }),
    other: Object.freeze({ label: "Other", icon: "◇" }),
  });

  class CorrectionsContractError extends TypeError {
    constructor(message, path = "$") {
      super(`${path}: ${message}`);
      this.name = "CorrectionsContractError";
      this.code = "invalid_corrections_contract";
      this.path = path;
    }
  }

  class CorrectionsReviewConflictError extends Error {
    constructor(message = "The review changed in another window", cause = null) {
      super(message);
      this.name = "CorrectionsReviewConflictError";
      this.code = "review_revision_conflict";
      this.cause = cause;
    }
  }

  function fail(path, message) {
    throw new CorrectionsContractError(message, path);
  }

  function isPlainObject(value) {
    if (!value || typeof value !== "object" || Array.isArray(value)) return false;
    const prototype = Object.getPrototypeOf(value);
    return prototype === Object.prototype || prototype === null;
  }

  function exactObject(value, path, allowed, required = allowed) {
    if (!isPlainObject(value)) fail(path, "must be an object");
    const allowedSet = new Set(allowed);
    for (const key of Object.keys(value)) {
      if (!allowedSet.has(key)) fail(`${path}.${key}`, "is not a recognized field");
    }
    for (const key of required) {
      if (!Object.prototype.hasOwnProperty.call(value, key)) {
        fail(`${path}.${key}`, "is required");
      }
    }
    return value;
  }

  function identifier(value, path) {
    if (typeof value !== "string" || !IDENTIFIER_RE.test(value)) {
      fail(path, "must be a portable opaque identifier");
    }
    return value;
  }

  function optionalIdentifier(value, path) {
    return value == null ? null : identifier(value, path);
  }

  function revision(value, path) {
    if (typeof value !== "string" || !value || value.length > 512 ||
        value !== value.trim() || /[\s"\\]/.test(value)) {
      fail(path, "must be an opaque revision token");
    }
    return value;
  }

  function safeText(value, path, maximum, allowEmpty = true) {
    if (typeof value !== "string" || value.length > maximum ||
        /[\u0000-\u0008\u000b\u000c\u000e-\u001f]/.test(value) ||
        (!allowEmpty && !value.trim())) {
      fail(path, `must be a${allowEmpty ? "" : " non-empty"} safe string`);
    }
    return value;
  }

  function enumValue(value, path, values) {
    if (!values.has(value)) fail(path, "has an unsupported value");
    return value;
  }

  function boundedArray(value, path, maximum) {
    if (!Array.isArray(value) || value.length > maximum) {
      fail(path, `must be an array with at most ${maximum} entries`);
    }
    return value;
  }

  function nonNegativeInteger(value, path) {
    if (!Number.isSafeInteger(value) || value < 0) {
      fail(path, "must be a non-negative safe integer");
    }
    return value;
  }

  function positiveInteger(value, path) {
    if (!Number.isSafeInteger(value) || value < 1) {
      fail(path, "must be a positive safe integer");
    }
    return value;
  }

  function normalizeTimestamp(value, path) {
    const result = safeText(value, path, 64, false);
    if (!/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?Z$/.test(result) ||
        !Number.isFinite(Date.parse(result))) {
      fail(path, "must be an RFC 3339 UTC timestamp");
    }
    return result;
  }

  function freezeDeep(value) {
    if (!value || typeof value !== "object" || Object.isFrozen(value)) return value;
    for (const entry of Object.values(value)) freezeDeep(entry);
    return Object.freeze(value);
  }

  function normalizeAuditEvent(value, path) {
    exactObject(value, path, [
      "operation_id", "action", "actor_id", "occurred_at", "before_state",
      "after_state", "reason", "comment",
    ]);
    const result = {
      operation_id: identifier(value.operation_id, `${path}.operation_id`),
      action: enumValue(value.action, `${path}.action`, REVIEW_ACTIONS),
      actor_id: identifier(value.actor_id, `${path}.actor_id`),
      occurred_at: normalizeTimestamp(value.occurred_at, `${path}.occurred_at`),
      before_state: enumValue(value.before_state, `${path}.before_state`, REVIEW_STATES),
      after_state: enumValue(value.after_state, `${path}.after_state`, REVIEW_STATES),
      reason: safeText(value.reason, `${path}.reason`, 2048),
      comment: safeText(value.comment, `${path}.comment`, 8192),
    };
    if (result.action === "attention.mark" && !result.reason.trim()) {
      fail(`${path}.reason`, "is required for attention.mark");
    }
    return freezeDeep(result);
  }

  function validateReviewState(result, path) {
    if (result.state === "clear" && result.reason) {
      fail(`${path}.reason`, "must be empty while review state is clear");
    }
    if (result.state !== "clear" && !result.reason.trim()) {
      fail(`${path}.reason`, "is required while attention is retained");
    }
    return result;
  }

  function normalizeReviewSummary(value, path = "$.review") {
    exactObject(value, path, [
      "revision", "state", "reason", "history_count", "latest_event",
    ]);
    const historyCount = nonNegativeInteger(value.history_count, `${path}.history_count`);
    const latestEvent = value.latest_event == null ? null :
      normalizeAuditEvent(value.latest_event, `${path}.latest_event`);
    if ((historyCount === 0) !== (latestEvent === null)) {
      fail(`${path}.latest_event`, "must be present exactly when history_count is non-zero");
    }
    const result = validateReviewState({
      revision: revision(value.revision, `${path}.revision`),
      state: enumValue(value.state, `${path}.state`, REVIEW_STATES),
      reason: safeText(value.reason, `${path}.reason`, 2048),
      history_count: historyCount,
      latest_event: latestEvent,
    }, path);
    if (latestEvent && latestEvent.after_state !== result.state) {
      fail(`${path}.latest_event.after_state`, "must match the current review state");
    }
    return freezeDeep(result);
  }

  function normalizeFullReview(value, path = "$.review") {
    exactObject(value, path, ["revision", "state", "reason", "history"]);
    const history = boundedArray(value.history, `${path}.history`, 10_000)
      .map((entry, index) => normalizeAuditEvent(entry, `${path}.history[${index}]`));
    for (let index = 1; index < history.length; index += 1) {
      if (history[index - 1].after_state !== history[index].before_state) {
        fail(`${path}.history[${index}].before_state`,
          "must continue the preceding audit state");
      }
    }
    const result = validateReviewState({
      revision: revision(value.revision, `${path}.revision`),
      state: enumValue(value.state, `${path}.state`, REVIEW_STATES),
      reason: safeText(value.reason, `${path}.reason`, 2048),
      history: freezeDeep(history),
    }, path);
    if (history.length && history[history.length - 1].after_state !== result.state) {
      fail(`${path}.history`, "must end at the current review state");
    }
    return freezeDeep(result);
  }

  function normalizeTarget(value, path = "$.target") {
    exactObject(value, path, [
      "kind", "item_id", "representation_id", "canvas_id", "artifact_id",
      "annotation_id",
    ], ["kind", "item_id"]);
    const kind = enumValue(value.kind, `${path}.kind`, TARGET_KINDS);
    const result = {
      kind,
      item_id: identifier(value.item_id, `${path}.item_id`),
    };
    for (const field of [
      "representation_id", "canvas_id", "artifact_id", "annotation_id",
    ]) {
      const normalized = optionalIdentifier(value[field], `${path}.${field}`);
      if (normalized !== null) result[field] = normalized;
    }
    if (kind === "book" &&
        (result.representation_id || result.canvas_id ||
         result.artifact_id || result.annotation_id)) {
      fail(path, "book targets cannot contain subordinate identifiers");
    }
    if (kind === "image" && !result.artifact_id) {
      fail(`${path}.artifact_id`, "is required for image targets");
    }
    if (kind === "image" && result.annotation_id) {
      fail(`${path}.annotation_id`, "is not valid for image targets");
    }
    if (kind === "region" && !result.annotation_id) {
      fail(`${path}.annotation_id`, "is required for region targets");
    }
    return freezeDeep(result);
  }

  function targetIdentity(target) {
    return [
      target.kind, target.item_id, target.representation_id || "",
      target.canvas_id || "", target.artifact_id || "", target.annotation_id || "",
    ].join("\u001f");
  }

  function normalizeAttentionEntry(value, path = "$.attention[]") {
    exactObject(value, path, ["key", "target", "review"]);
    const result = {
      key: identifier(value.key, `${path}.key`),
      target: normalizeTarget(value.target, `${path}.target`),
      review: normalizeReviewSummary(value.review, `${path}.review`),
    };
    if (result.review.state === "clear") {
      fail(`${path}.review.state`, "clear reviews do not belong in the attention index");
    }
    return freezeDeep(result);
  }

  function normalizeThumbnail(value, path) {
    if (value == null) return null;
    exactObject(value, path, ["url", "alt", "width", "height"], ["url", "alt"]);
    const url = safeText(value.url, `${path}.url`, 4096, false);
    if (/^(?:javascript|file|filesystem):/i.test(url) ||
        (/^data:/i.test(url) && !/^data:image\//i.test(url))) {
      fail(`${path}.url`, "uses a disallowed URL scheme");
    }
    const result = {
      url,
      alt: safeText(value.alt, `${path}.alt`, 512),
    };
    if (value.width != null) result.width = positiveInteger(value.width, `${path}.width`);
    if (value.height != null) result.height = positiveInteger(value.height, `${path}.height`);
    return freezeDeep(result);
  }

  function normalizeCapture(value, path) {
    exactObject(value, path, [
      "artifact_id", "revision", "capture_order", "label", "representation_id",
      "canvas_id", "effective_category", "resource_state", "import_state",
      "freshness", "thumbnail",
    ], [
      "artifact_id", "revision", "capture_order", "label", "effective_category",
      "resource_state", "import_state", "freshness", "thumbnail",
    ]);
    const result = {
      artifact_id: identifier(value.artifact_id, `${path}.artifact_id`),
      revision: revision(value.revision, `${path}.revision`),
      capture_order: nonNegativeInteger(value.capture_order, `${path}.capture_order`),
      label: safeText(value.label, `${path}.label`, 512),
      effective_category: enumValue(
        value.effective_category, `${path}.effective_category`, IMAGE_CATEGORIES),
      resource_state: enumValue(
        value.resource_state, `${path}.resource_state`, RESOURCE_STATES),
      import_state: enumValue(
        value.import_state, `${path}.import_state`, IMPORT_STATES),
      freshness: enumValue(value.freshness, `${path}.freshness`, FRESHNESS_STATES),
      thumbnail: normalizeThumbnail(value.thumbnail, `${path}.thumbnail`),
    };
    for (const field of ["representation_id", "canvas_id"]) {
      const normalized = optionalIdentifier(value[field], `${path}.${field}`);
      if (normalized !== null) result[field] = normalized;
    }
    if (result.resource_state !== "available" && result.thumbnail !== null) {
      fail(`${path}.thumbnail`, "must be null when the resource is not available");
    }
    return freezeDeep(result);
  }

  function comparePortable(left, right) {
    return left < right ? -1 : left > right ? 1 : 0;
  }

  function stableTitleKey(value) {
    return String(value || "").normalize("NFKC").toLowerCase();
  }

  function compareCaptures(left, right) {
    return left.capture_order - right.capture_order ||
      comparePortable(left.artifact_id, right.artifact_id);
  }

  function normalizeBook(value, path) {
    exactObject(value, path, [
      "id", "revision", "title", "import_state", "issues", "review", "captures",
    ]);
    const captures = boundedArray(value.captures, `${path}.captures`, 100_000)
      .map((capture, index) => normalizeCapture(capture, `${path}.captures[${index}]`))
      .sort(compareCaptures);
    const captureIds = captures.map((capture) => capture.artifact_id.toLowerCase());
    const captureOrders = captures.map((capture) => capture.capture_order);
    if (new Set(captureIds).size !== captureIds.length) {
      fail(`${path}.captures`, "contains duplicate artifact identifiers");
    }
    if (new Set(captureOrders).size !== captureOrders.length) {
      fail(`${path}.captures`, "contains duplicate capture_order values");
    }
    const issues = boundedArray(value.issues, `${path}.issues`, 1024)
      .map((issue, index) => safeText(issue, `${path}.issues[${index}]`, 2048, false));
    return freezeDeep({
      id: identifier(value.id, `${path}.id`),
      revision: revision(value.revision, `${path}.revision`),
      title: safeText(value.title, `${path}.title`, 2048),
      import_state: enumValue(
        value.import_state, `${path}.import_state`, IMPORT_STATES),
      issues: freezeDeep(issues),
      review: normalizeReviewSummary(value.review, `${path}.review`),
      captures: freezeDeep(captures),
    });
  }

  function normalizeCorrectionsIndex(value) {
    exactObject(value, "$", ["schema", "revision", "books", "attention"]);
    if (value.schema !== CORRECTIONS_INDEX_SCHEMA) {
      fail("$.schema", `must equal ${CORRECTIONS_INDEX_SCHEMA}`);
    }
    const books = boundedArray(value.books, "$.books", 100_000)
      .map((book, index) => normalizeBook(book, `$.books[${index}]`));
    const bookIds = books.map((book) => book.id.toLowerCase());
    if (new Set(bookIds).size !== bookIds.length) {
      fail("$.books", "contains duplicate item identifiers");
    }
    const knownBooks = new Set(books.map((book) => book.id));
    const attention = boundedArray(value.attention, "$.attention", 1_000_000)
      .map((entry, index) =>
        normalizeAttentionEntry(entry, `$.attention[${index}]`));
    const attentionKeys = attention.map((entry) => entry.key.toLowerCase());
    const targetKeys = attention.map((entry) => targetIdentity(entry.target));
    if (new Set(attentionKeys).size !== attentionKeys.length) {
      fail("$.attention", "contains duplicate attention keys");
    }
    if (new Set(targetKeys).size !== targetKeys.length) {
      fail("$.attention", "contains duplicate review targets");
    }
    for (let index = 0; index < attention.length; index += 1) {
      if (!knownBooks.has(attention[index].target.item_id)) {
        fail(`$.attention[${index}].target.item_id`,
          "must identify a book in this index");
      }
    }
    return freezeDeep({
      schema: CORRECTIONS_INDEX_SCHEMA,
      revision: revision(value.revision, "$.revision"),
      books: freezeDeep(books),
      attention: freezeDeep(attention),
    });
  }

  function normalizeReviewDocument(value) {
    exactObject(value, "$", ["schema", "target", "review"]);
    if (value.schema !== CORRECTIONS_REVIEW_SCHEMA) {
      fail("$.schema", `must equal ${CORRECTIONS_REVIEW_SCHEMA}`);
    }
    return freezeDeep({
      schema: CORRECTIONS_REVIEW_SCHEMA,
      target: normalizeTarget(value.target, "$.target"),
      review: normalizeFullReview(value.review, "$.review"),
    });
  }

  function normalizeReviewMutationResult(value) {
    exactObject(value, "$", ["schema", "index_revision", "entry"]);
    if (value.schema !== CORRECTIONS_REVIEW_RESULT_SCHEMA) {
      fail("$.schema", `must equal ${CORRECTIONS_REVIEW_RESULT_SCHEMA}`);
    }
    return freezeDeep({
      schema: CORRECTIONS_REVIEW_RESULT_SCHEMA,
      index_revision: revision(value.index_revision, "$.index_revision"),
      entry: normalizeAttentionEntry(value.entry, "$.entry"),
    });
  }

  function normalizeIndexChange(value) {
    exactObject(value, "$", ["schema", "revision"]);
    if (value.schema !== CORRECTIONS_INDEX_CHANGE_SCHEMA) {
      fail("$.schema", `must equal ${CORRECTIONS_INDEX_CHANGE_SCHEMA}`);
    }
    return freezeDeep({
      schema: CORRECTIONS_INDEX_CHANGE_SCHEMA,
      revision: revision(value.revision, "$.revision"),
    });
  }

  function bookNeedsAttention(book, attention) {
    return book.review.state === "needs_attention" ||
      attention.some((entry) =>
        entry.target.item_id === book.id && entry.review.state === "needs_attention");
  }

  function compareBooks(left, right, attention = []) {
    const attentionIds = attention instanceof Set ? attention : null;
    const leftAttention = left.review.state === "needs_attention" ||
      (attentionIds ? attentionIds.has(left.id) : bookNeedsAttention(left, attention));
    const rightAttention = right.review.state === "needs_attention" ||
      (attentionIds ? attentionIds.has(right.id) : bookNeedsAttention(right, attention));
    if (leftAttention !== rightAttention) return leftAttention ? -1 : 1;
    return comparePortable(stableTitleKey(left.title), stableTitleKey(right.title)) ||
      comparePortable(left.id, right.id);
  }

  function sortedBooks(index) {
    if (!index) return [];
    const attentionIds = new Set(index.attention
      .filter((entry) => entry.review.state === "needs_attention")
      .map((entry) => entry.target.item_id));
    return [...index.books].sort((left, right) =>
      compareBooks(left, right, attentionIds));
  }

  function selectionAddressFromTarget(target) {
    const normalized = normalizeTarget(target);
    return freezeDeep({
      itemId: normalized.item_id,
      representationId: normalized.representation_id || null,
      canvasId: normalized.canvas_id || null,
      artifactId: normalized.artifact_id || null,
      annotationId: normalized.annotation_id || null,
    });
  }

  function normalizeSelectionAddress(value, path = "$.selection") {
    exactObject(value, path, [
      "itemId", "representationId", "canvasId", "artifactId", "annotationId",
    ], ["itemId"]);
    return freezeDeep({
      itemId: identifier(value.itemId, `${path}.itemId`),
      representationId: optionalIdentifier(
        value.representationId, `${path}.representationId`),
      canvasId: optionalIdentifier(value.canvasId, `${path}.canvasId`),
      artifactId: optionalIdentifier(value.artifactId, `${path}.artifactId`),
      annotationId: optionalIdentifier(value.annotationId, `${path}.annotationId`),
    });
  }

  function selectionExists(index, address) {
    const book = index.books.find((candidate) => candidate.id === address.itemId);
    if (!book) return false;
    if (!address.artifactId && !address.annotationId) return true;
    if (address.annotationId) {
      return index.attention.some((entry) =>
        entry.target.item_id === address.itemId &&
        entry.target.annotation_id === address.annotationId);
    }
    return book.captures.some((capture) =>
      capture.artifact_id === address.artifactId) ||
      index.attention.some((entry) =>
        entry.target.item_id === address.itemId &&
        entry.target.artifact_id === address.artifactId);
  }

  function isConflict(error) {
    return !!error && (
      error.status === 409 || error.code === "review_revision_conflict" ||
      error.code === "target_revision_conflict" ||
      error.code === "correction_revision_conflict"
    );
  }

  function abortError(error) {
    return !!error && (error.name === "AbortError" || error.code === "ABORT_ERR");
  }

  function errorMessage(error) {
    if (!error) return "Unknown error";
    const value = typeof error.message === "string" ? error.message : String(error);
    return value.slice(0, 1000);
  }

  class CorrectionsIndexStore {
    constructor(options = {}) {
      this.api = options.api || null;
      this.onSelectionInvalidated =
        typeof options.onSelectionInvalidated === "function"
          ? options.onSelectionInvalidated : null;
      this.listeners = new Set();
      this.workspaceId = null;
      this.index = null;
      this.status = this.api && typeof this.api.loadIndex === "function"
        ? "idle" : "unavailable";
      this.error = null;
      this.refreshReason = null;
      this.selection = null;
      this.selectionOwned = false;
      this.generation = 0;
      this.abortController = null;
      this.unsubscribeExternal = null;
      this.destroyed = false;
    }

    snapshot() {
      return Object.freeze({
        status: this.status,
        workspaceId: this.workspaceId,
        index: this.index,
        error: this.error,
        refreshReason: this.refreshReason,
        selection: this.selection,
      });
    }

    subscribe(listener) {
      if (typeof listener !== "function") throw new TypeError("listener must be a function");
      this.listeners.add(listener);
      listener(this.snapshot());
      return () => this.listeners.delete(listener);
    }

    emit() {
      const snapshot = this.snapshot();
      for (const listener of [...this.listeners]) listener(snapshot);
    }

    setSelection(value, options = {}) {
      this.selection = value == null ? null : normalizeSelectionAddress(value);
      this.selectionOwned = options.ownedByFeature === true;
      this.emit();
      return this.selection;
    }

    async openWorkspace(workspaceId, options = {}) {
      const normalizedWorkspace = identifier(workspaceId, "$.workspace_id");
      if (options.selection !== undefined) {
        this.selection = options.selection == null ? null :
          normalizeSelectionAddress(options.selection);
        this.selectionOwned = options.selectionOwned === true;
      }
      if (this.workspaceId === normalizedWorkspace) {
        this.emit();
        if (this.index || this.status === "loading") return this.index;
        return this.refresh({ reason: "context" });
      }
      this._cancelLoad();
      this._disconnectExternal();
      this.workspaceId = normalizedWorkspace;
      this.index = null;
      this.error = null;
      this.status = this.api && typeof this.api.loadIndex === "function"
        ? "idle" : "unavailable";
      this.emit();
      this._connectExternal();
      return this.refresh({ reason: "context" });
    }

    async refresh(options = {}) {
      if (this.destroyed) return null;
      if (!this.workspaceId) {
        this.status = this.api && typeof this.api.loadIndex === "function"
          ? "idle" : "unavailable";
        this.emit();
        return null;
      }
      if (!this.api || typeof this.api.loadIndex !== "function") {
        this.status = "unavailable";
        this.error = null;
        this.emit();
        return null;
      }
      this._cancelLoad();
      const generation = ++this.generation;
      const controller = typeof AbortController === "function"
        ? new AbortController() : null;
      this.abortController = controller;
      this.status = "loading";
      this.error = null;
      this.refreshReason = options.reason || "manual";
      this.emit();
      try {
        const value = await this.api.loadIndex({
          workspaceId: this.workspaceId,
          signal: controller && controller.signal,
        });
        if (this.destroyed || generation !== this.generation) return null;
        const index = normalizeCorrectionsIndex(value);
        this.abortController = null;
        this.index = index;
        this.status = "ready";
        this.error = null;
        this._reconcileSelection();
        this.emit();
        return index;
      } catch (error) {
        if (this.destroyed || generation !== this.generation || abortError(error)) return null;
        this.abortController = null;
        this.status = "error";
        this.error = Object.freeze({
          code: error && error.code || "corrections_index_unavailable",
          message: errorMessage(error),
        });
        this.emit();
        return null;
      }
    }

    async getReview(target, options = {}) {
      if (!this.api || typeof this.api.getReview !== "function") {
        throw new Error("Review audit history is unavailable");
      }
      const normalizedTarget = normalizeTarget(target);
      const value = await this.api.getReview({
        target: normalizedTarget,
        signal: options.signal,
      });
      const document = normalizeReviewDocument(value);
      if (targetIdentity(document.target) !== targetIdentity(normalizedTarget)) {
        throw new CorrectionsContractError(
          "review response target does not match the request", "$.target");
      }
      return document;
    }

    async transitionReview(action, options = {}) {
      if (!["resolve", "reopen"].includes(action)) {
        throw new TypeError("review action must be resolve or reopen");
      }
      const entry = normalizeAttentionEntry(options.entry, "$.entry");
      const actorId = identifier(options.actorId, "$.actor_id");
      const operationId = identifier(options.operationId, "$.operation_id");
      const comment = safeText(options.comment || "", "$.comment", 8192);
      const methodName = action === "resolve" ? "resolveReview" : "reopenReview";
      if (!this.api || typeof this.api[methodName] !== "function") {
        throw new Error(`${action === "resolve" ? "Resolve" : "Reopen"} is unavailable`);
      }
      try {
        const value = await this.api[methodName]({
          target: entry.target,
          expectedRevision: entry.review.revision,
          actorId,
          operationId,
          comment,
          signal: options.signal,
        });
        const result = normalizeReviewMutationResult(value);
        if (result.entry.key !== entry.key ||
            targetIdentity(result.entry.target) !== targetIdentity(entry.target)) {
          throw new CorrectionsContractError(
            "review mutation returned a different target", "$.entry.target");
        }
        const expectedState = action === "resolve" ? "resolved" : "needs_attention";
        if (result.entry.review.state !== expectedState) {
          throw new CorrectionsContractError(
            `review mutation must return state ${expectedState}`, "$.entry.review.state");
        }
        this.applyAttentionEntry(result.entry, result.index_revision);
        return result;
      } catch (error) {
        if (!isConflict(error)) throw error;
        await this.refresh({ reason: "conflict" });
        throw new CorrectionsReviewConflictError(undefined, error);
      }
    }

    applyAttentionEntry(entryValue, indexRevision) {
      if (!this.index) throw new Error("The Corrections index has not been loaded");
      const entry = normalizeAttentionEntry(entryValue);
      const normalizedRevision = revision(indexRevision, "$.index_revision");
      const existingIndex = this.index.attention.findIndex(
        (candidate) => candidate.key === entry.key);
      const attention = [...this.index.attention];
      if (existingIndex >= 0) attention[existingIndex] = entry;
      else attention.push(entry);
      const books = this.index.books.map((book) => {
        if (entry.target.kind !== "book" || book.id !== entry.target.item_id) return book;
        return freezeDeep({ ...book, review: entry.review });
      });
      this.index = freezeDeep({
        schema: CORRECTIONS_INDEX_SCHEMA,
        revision: normalizedRevision,
        books: freezeDeep(books),
        attention: freezeDeep(attention),
      });
      this.status = "ready";
      this.error = null;
      this._reconcileSelection();
      this.emit();
    }

    _reconcileSelection() {
      if (!this.index || !this.selection || !this.selectionOwned ||
          selectionExists(this.index, this.selection)) return;
      const previous = this.selection;
      this.selection = null;
      this.selectionOwned = false;
      if (this.onSelectionInvalidated) {
        this.onSelectionInvalidated(Object.freeze({
          reason: "selection_disappeared",
          selection: previous,
          indexRevision: this.index.revision,
        }));
      }
    }

    _cancelLoad() {
      this.generation += 1;
      if (this.abortController) this.abortController.abort();
      this.abortController = null;
    }

    _connectExternal() {
      if (!this.api || typeof this.api.subscribe !== "function" || !this.workspaceId) return;
      try {
        const unsubscribe = this.api.subscribe({
          workspaceId: this.workspaceId,
          afterRevision: this.index && this.index.revision || null,
          onChange: (value) => {
            if (this.destroyed) return;
            try {
              const change = normalizeIndexChange(value);
              if (!this.index || change.revision !== this.index.revision) {
                this.refresh({ reason: "external" });
              }
            } catch (error) {
              this.status = "error";
              this.error = Object.freeze({
                code: error.code || "invalid_corrections_change",
                message: errorMessage(error),
              });
              this.emit();
            }
          },
        });
        if (typeof unsubscribe === "function") this.unsubscribeExternal = unsubscribe;
      } catch (error) {
        this.status = "error";
        this.error = Object.freeze({
          code: error.code || "corrections_subscription_unavailable",
          message: errorMessage(error),
        });
        this.emit();
      }
    }

    _disconnectExternal() {
      if (typeof this.unsubscribeExternal === "function") this.unsubscribeExternal();
      this.unsubscribeExternal = null;
    }

    destroy() {
      this.destroyed = true;
      this._cancelLoad();
      this._disconnectExternal();
      this.listeners.clear();
    }
  }

  function clearNode(node) {
    if (!node) return;
    if (typeof node.replaceChildren === "function") node.replaceChildren();
    else while (node.firstChild) node.removeChild(node.firstChild);
  }

  function element(documentRef, name, className, text) {
    const node = documentRef.createElement(name);
    if (className) node.className = className;
    if (text != null) node.textContent = String(text);
    return node;
  }

  function setAttribute(node, name, value) {
    if (node && typeof node.setAttribute === "function") {
      node.setAttribute(name, String(value));
    }
  }

  function captureState(capture) {
    if (capture.import_state === "pending") return "Pending import";
    if (capture.resource_state === "missing" || capture.import_state === "missing") {
      return "Image missing";
    }
    if (capture.resource_state === "unavailable" ||
        capture.import_state === "unavailable") return "Image unavailable";
    if (capture.import_state === "legacy") return "Legacy import";
    if (capture.import_state === "partial") return "Partial import";
    if (capture.freshness === "stale") return "Stale";
    if (capture.freshness === "untracked") return "Freshness unknown";
    return "Available";
  }

  function captureAddress(book, capture) {
    return freezeDeep({
      itemId: book.id,
      representationId: capture.representation_id || null,
      canvasId: capture.canvas_id || null,
      artifactId: capture.artifact_id,
      annotationId: null,
    });
  }

  function bookAddress(book) {
    return freezeDeep({
      itemId: book.id,
      representationId: null,
      canvasId: null,
      artifactId: null,
      annotationId: null,
    });
  }

  function addressEqual(left, right) {
    if (!left || !right) return false;
    return ["itemId", "representationId", "canvasId", "artifactId", "annotationId"]
      .every((field) => (left[field] || null) === (right[field] || null));
  }

  class BooksPanelController {
    constructor(options = {}) {
      if (!options.root || typeof options.root.querySelector !== "function") {
        throw new TypeError("Books panel root is required");
      }
      if (!options.store || typeof options.store.subscribe !== "function") {
        throw new TypeError("Corrections index store is required");
      }
      this.root = options.root;
      this.store = options.store;
      this.documentRef = options.documentRef || this.root.ownerDocument;
      this.onNavigate = typeof options.onNavigate === "function"
        ? options.onNavigate : () => {};
      this.onStatus = typeof options.onStatus === "function"
        ? options.onStatus : () => {};
      this.filter = "";
      this.unsubscribe = null;
      this.listeners = [];
      this.rowListeners = [];
      this.mounted = false;
    }

    listen(target, type, handler) {
      if (!target || typeof target.addEventListener !== "function") return;
      target.addEventListener(type, handler);
      this.listeners.push(() => target.removeEventListener(type, handler));
    }

    listenRow(target, type, handler) {
      if (!target || typeof target.addEventListener !== "function") return;
      target.addEventListener(type, handler);
      this.rowListeners.push(() => target.removeEventListener(type, handler));
    }

    mount() {
      if (this.mounted) return this;
      this.mounted = true;
      const filter = this.root.querySelector("[data-books-filter]");
      this.listen(filter, "input", () => {
        this.filter = String(filter.value || "");
        this.render(this.store.snapshot());
      });
      this.listen(filter, "keydown", (event) => {
        if (event.key !== "Escape" || !filter.value) return;
        event.preventDefault();
        filter.value = "";
        this.filter = "";
        this.render(this.store.snapshot());
      });
      this.unsubscribe = this.store.subscribe((snapshot) => this.render(snapshot));
      return this;
    }

    setSelection(address, options = {}) {
      this.store.setSelection(address, {
        ownedByFeature: options.ownedByFeature === true,
      });
    }

    visibleBooks(snapshot) {
      if (!snapshot.index) return [];
      const query = stableTitleKey(this.filter.trim());
      return sortedBooks(snapshot.index).filter((book) => {
        if (!query) return true;
        return stableTitleKey(book.title).includes(query) ||
          stableTitleKey(book.id).includes(query);
      });
    }

    render(snapshot) {
      for (const remove of this.rowListeners.splice(0)) remove();
      const list = this.root.querySelector("[data-books-list]");
      const count = this.root.querySelector("[data-books-count]");
      if (!list || !this.documentRef) return;
      const books = this.visibleBooks(snapshot);
      if (count) count.textContent = snapshot.index ? String(snapshot.index.books.length) : "0";
      setAttribute(list, "aria-busy", snapshot.status === "loading" ? "true" : "false");
      if (snapshot.status === "loading" && snapshot.index) {
        list.classList && list.classList.add("is-refreshing");
      } else if (list.classList) list.classList.remove("is-refreshing");

      if (!snapshot.index) {
        const messages = {
          unavailable: [
            "Books unavailable",
            "No Corrections data API is configured for this window.",
          ],
          idle: ["Waiting for workspace", "Open a workspace to load books."],
          loading: ["Loading books", "Loading books and capture summaries…"],
          error: [
            "Books could not be loaded",
            snapshot.error && snapshot.error.message || "The index is unavailable.",
          ],
        };
        const [title, message] = messages[snapshot.status] || messages.idle;
        this.renderMessage(list, title, message, snapshot.status === "error");
        return;
      }

      clearNode(list);
      if (snapshot.status === "error") {
        list.append(this.messageRow(
          "Refresh failed",
          snapshot.error && snapshot.error.message ||
            "The last loaded book index is shown.",
          true,
        ));
      }
      if (!snapshot.index.books.length) {
        list.append(this.messageRow("No books", "This workspace contains no books."));
        return;
      }
      if (!books.length) {
        list.append(this.messageRow("No matches",
          `No books match “${this.filter.trim().slice(0, 120)}”.`));
        return;
      }
      for (const book of books) list.append(this.renderBook(book, snapshot));
    }

    renderMessage(list, title, message, error = false) {
      clearNode(list);
      list.append(this.messageRow(title, message, error));
    }

    messageRow(title, message, error = false) {
      const row = element(this.documentRef, "li",
        `books-panel-state${error ? " is-error" : ""}`);
      setAttribute(row, "role", error ? "alert" : "status");
      row.append(
        element(this.documentRef, "strong", "books-panel-state-title", title),
        element(this.documentRef, "span", "books-panel-state-message", message),
      );
      return row;
    }

    renderBook(book, snapshot) {
      const needsAttention = bookNeedsAttention(book, snapshot.index.attention);
      const row = element(this.documentRef, "li",
        `corrections-book${needsAttention ? " needs-attention" : ""}`);
      row.dataset && (row.dataset.bookId = book.id);
      const select = element(this.documentRef, "button", "book-select");
      select.type = "button";
      const title = book.title.trim() || `Untitled (${book.id})`;
      const selected = snapshot.selection &&
        snapshot.selection.itemId === book.id && !snapshot.selection.artifactId &&
        !snapshot.selection.annotationId;
      setAttribute(select, "aria-pressed", selected ? "true" : "false");
      setAttribute(select, "aria-label",
        `${title}${needsAttention ? ", needs attention" : ""}`);
      select.append(element(this.documentRef, "span", "book-title", title));
      if (needsAttention) {
        const attention = element(this.documentRef, "span", "book-attention");
        const icon = element(this.documentRef, "span", "book-attention-icon", "!");
        setAttribute(icon, "aria-hidden", "true");
        attention.append(icon, element(this.documentRef, "span", "", "Needs attention"));
        select.append(attention);
      }
      if (book.import_state !== "ready") {
        select.append(element(this.documentRef, "span", "book-import-state",
          `${book.import_state.replace("_", " ")} import`));
      }
      this.listenRow(select, "click", () => this.navigate(bookAddress(book), "book"));
      row.append(select);

      if (!book.captures.length) {
        row.append(element(this.documentRef, "p", "book-no-captures",
          "No captured images"));
        return row;
      }
      const captures = element(this.documentRef, "ul", "book-captures");
      setAttribute(captures, "aria-label", `Captured images for ${title}`);
      for (const capture of book.captures) {
        const item = element(this.documentRef, "li", "book-capture");
        const button = element(this.documentRef, "button", "capture-select");
        button.type = "button";
        const category = CATEGORY_PRESENTATION[capture.effective_category];
        const state = captureState(capture);
        const label = capture.label.trim() ||
          `Capture ${capture.capture_order + 1}`;
        setAttribute(button, "aria-label",
          `${label}, ${category.label}, ${state}`);
        setAttribute(button, "aria-pressed",
          addressEqual(snapshot.selection, captureAddress(book, capture)) ? "true" : "false");
        if (button.dataset) {
          button.dataset.artifactId = capture.artifact_id;
          button.dataset.category = capture.effective_category;
          button.dataset.resourceState = capture.resource_state;
        }
        if (capture.thumbnail) {
          const image = element(this.documentRef, "img", "capture-thumbnail");
          image.src = capture.thumbnail.url;
          image.alt = capture.thumbnail.alt;
          image.loading = "lazy";
          image.decoding = "async";
          if (capture.thumbnail.width) image.width = capture.thumbnail.width;
          if (capture.thumbnail.height) image.height = capture.thumbnail.height;
          button.append(image);
        } else {
          const placeholder = element(this.documentRef, "span",
            "capture-thumbnail capture-thumbnail-placeholder", state);
          setAttribute(placeholder, "aria-hidden", "true");
          button.append(placeholder);
        }
        const chip = element(this.documentRef, "span", "capture-category");
        const icon = element(this.documentRef, "span", "capture-category-icon",
          category.icon);
        setAttribute(icon, "aria-hidden", "true");
        chip.append(icon, element(this.documentRef, "span", "", category.label));
        button.append(
          chip,
          element(this.documentRef, "span", "capture-state", state),
        );
        this.listenRow(button, "click", () =>
          this.navigate(captureAddress(book, capture), "image"));
        item.append(button);
        captures.append(item);
      }
      row.append(captures);
      return row;
    }

    navigate(address, targetKind) {
      this.store.setSelection(address, { ownedByFeature: true });
      this.onNavigate(address, Object.freeze({
        source: "books",
        targetKind,
      }));
    }

    destroy() {
      if (typeof this.unsubscribe === "function") this.unsubscribe();
      this.unsubscribe = null;
      for (const remove of this.listeners.splice(0)) remove();
      for (const remove of this.rowListeners.splice(0)) remove();
      this.mounted = false;
    }
  }

  return {
    CATEGORY_PRESENTATION,
    CORRECTIONS_INDEX_CHANGE_SCHEMA,
    CORRECTIONS_INDEX_SCHEMA,
    CORRECTIONS_REVIEW_RESULT_SCHEMA,
    CORRECTIONS_REVIEW_SCHEMA,
    BooksPanelController,
    CorrectionsContractError,
    CorrectionsIndexStore,
    CorrectionsReviewConflictError,
    addressEqual,
    bookAddress,
    bookNeedsAttention,
    captureAddress,
    captureState,
    compareBooks,
    normalizeAttentionEntry,
    normalizeCorrectionsIndex,
    normalizeIndexChange,
    normalizeReviewDocument,
    normalizeReviewMutationResult,
    normalizeSelectionAddress,
    normalizeTarget,
    selectionAddressFromTarget,
    sortedBooks,
    stableTitleKey,
    targetIdentity,
  };
});

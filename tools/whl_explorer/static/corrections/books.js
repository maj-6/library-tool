(function installCorrectionsBooks(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else Object.assign(root.LibraryToolCorrections ||= {}, api);
})(typeof globalThis !== "undefined" ? globalThis : this, function booksFactory() {
  "use strict";

  const CORRECTIONS_INDEX_SCHEMA = "librarytool.corrections-index/2";
  const CORRECTIONS_INDEX_SUMMARY_SCHEMA =
    "librarytool.corrections-index-summary/1";
  const CORRECTIONS_INDEX_DETAIL_SCHEMA =
    "librarytool.corrections-index-detail/1";
  const CORRECTIONS_CAPTURE_MARKS_SCHEMA =
    "librarytool.corrections-capture-marks/1";
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
  const MAX_CORRECTION_AUDIT_EVENTS = 100_000;
  const TARGET_KINDS = new Set(["book", "image", "region"]);
  const ITEM_KINDS = new Set(["book", "capture"]);
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

  // Older desktop imports predate desktop_import.imported_at, so this field
  // is optional and empty means "import time unknown". The engine passes
  // through any timestamp its own Python parser accepted — ISO 8601 forms
  // Date.parse cannot read (comma fractions, basic format) arrive verbatim —
  // so an unparseable value degrades to "" the same way the engine degrades
  // its own parse failures, instead of failing the whole index.
  function normalizeImportTimestamp(value, path) {
    if (value == null) return "";
    const result = safeText(value, path, 64);
    return Number.isFinite(Date.parse(result)) ? result : "";
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

  function sameAuditEvent(left, right) {
    if (left === null || right === null) return left === right;
    return [
      "operation_id", "action", "actor_id", "occurred_at", "before_state",
      "after_state", "reason", "comment",
    ].every((field) => left[field] === right[field]);
  }

  function sameReviewSummary(left, right) {
    return left.revision === right.revision &&
      left.state === right.state &&
      left.reason === right.reason &&
      left.history_count === right.history_count &&
      sameAuditEvent(left.latest_event, right.latest_event);
  }

  function normalizeFullReview(value, path = "$.review") {
    exactObject(value, path, ["revision", "state", "reason", "history"]);
    const history = boundedArray(
      value.history,
      `${path}.history`,
      MAX_CORRECTION_AUDIT_EVENTS,
    )
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
      "freshness", "imported_at", "thumbnail",
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
      imported_at: normalizeImportTimestamp(
        value.imported_at, `${path}.imported_at`),
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
      "id", "revision", "kind", "title", "import_state", "issues", "review",
      "captures", "latest_imported_at",
    ], [
      "id", "revision", "kind", "title", "import_state", "issues", "review",
      "captures",
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
      kind: enumValue(value.kind, `${path}.kind`, ITEM_KINDS),
      title: safeText(value.title, `${path}.title`, 2048),
      import_state: enumValue(
        value.import_state, `${path}.import_state`, IMPORT_STATES),
      issues: freezeDeep(issues),
      review: normalizeReviewSummary(value.review, `${path}.review`),
      captures: freezeDeep(captures),
      latest_imported_at: normalizeImportTimestamp(
        value.latest_imported_at, `${path}.latest_imported_at`),
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
    const bookAttention = new Map(attention
      .filter((entry) => entry.target.kind === "book")
      .map((entry) => [entry.target.item_id, entry]));
    for (let index = 0; index < books.length; index += 1) {
      const book = books[index];
      const entry = bookAttention.get(book.id);
      if (book.review.state === "clear" && entry) {
        fail(`$.books[${index}].review`,
          "clear book reviews cannot have a book attention entry");
      }
      if (book.review.state !== "clear" && !entry) {
        fail(`$.books[${index}].review`,
          "non-clear book reviews require one book attention entry");
      }
      if (entry && !sameReviewSummary(book.review, entry.review)) {
        fail(`$.books[${index}].review`,
          "must exactly match its book attention entry");
      }
    }
    return freezeDeep({
      schema: CORRECTIONS_INDEX_SCHEMA,
      revision: revision(value.revision, "$.revision"),
      books: freezeDeep(books),
      attention: freezeDeep(attention),
    });
  }

  function normalizeSummaryBook(value, path) {
    exactObject(value, path, ["id", "revision", "kind", "title", "review"]);
    return freezeDeep({
      id: identifier(value.id, `${path}.id`),
      revision: revision(value.revision, `${path}.revision`),
      kind: enumValue(value.kind, `${path}.kind`, ITEM_KINDS),
      title: safeText(value.title, `${path}.title`, 2048),
      review: normalizeReviewSummary(value.review, `${path}.review`),
    });
  }

  // The books and attention arrays are each the whole collection here, exactly
  // as they are in the full index, so the same closed statement holds: every
  // non-clear review has one attention entry and they agree. That is why the
  // cross-field checks below are a copy rather than a relaxation.
  function normalizeCorrectionsIndexSummary(value) {
    exactObject(value, "$", ["schema", "revision", "books", "attention"]);
    if (value.schema !== CORRECTIONS_INDEX_SUMMARY_SCHEMA) {
      fail("$.schema", `must equal ${CORRECTIONS_INDEX_SUMMARY_SCHEMA}`);
    }
    const books = boundedArray(value.books, "$.books", 100_000)
      .map((book, index) => normalizeSummaryBook(book, `$.books[${index}]`));
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
    const bookAttention = new Map(attention
      .filter((entry) => entry.target.kind === "book")
      .map((entry) => [entry.target.item_id, entry]));
    for (let index = 0; index < books.length; index += 1) {
      const book = books[index];
      const entry = bookAttention.get(book.id);
      if (book.review.state === "clear" && entry) {
        fail(`$.books[${index}].review`,
          "clear book reviews cannot have a book attention entry");
      }
      if (book.review.state !== "clear" && !entry) {
        fail(`$.books[${index}].review`,
          "non-clear book reviews require one book attention entry");
      }
      if (entry && !sameReviewSummary(book.review, entry.review)) {
        fail(`$.books[${index}].review`,
          "must exactly match its book attention entry");
      }
    }
    return freezeDeep({
      schema: CORRECTIONS_INDEX_SUMMARY_SCHEMA,
      revision: revision(value.revision, "$.revision"),
      books: freezeDeep(books),
      attention: freezeDeep(attention),
    });
  }

  function normalizeCorrectionsIndexDetail(value) {
    exactObject(value, "$", ["schema", "revision", "books", "missing"]);
    if (value.schema !== CORRECTIONS_INDEX_DETAIL_SCHEMA) {
      fail("$.schema", `must equal ${CORRECTIONS_INDEX_DETAIL_SCHEMA}`);
    }
    const books = boundedArray(value.books, "$.books", 100_000)
      .map((book, index) => normalizeBook(book, `$.books[${index}]`));
    const missing = boundedArray(value.missing, "$.missing", 100_000)
      .map((itemId, index) => identifier(itemId, `$.missing[${index}]`));
    const bookIds = books.map((book) => book.id.toLowerCase());
    const missingIds = missing.map((itemId) => itemId.toLowerCase());
    if (new Set(bookIds).size !== bookIds.length) {
      fail("$.books", "contains duplicate item identifiers");
    }
    if (new Set(missingIds).size !== missingIds.length) {
      fail("$.missing", "contains duplicate item identifiers");
    }
    const missingSet = new Set(missingIds);
    if (bookIds.some((itemId) => missingSet.has(itemId))) {
      fail("$.missing", "cannot repeat a book this response returned");
    }
    return freezeDeep({
      schema: CORRECTIONS_INDEX_DETAIL_SCHEMA,
      revision: revision(value.revision, "$.revision"),
      books: freezeDeep(books),
      missing: freezeDeep(missing),
    });
  }

  function normalizeCaptureMark(value, path) {
    exactObject(value, path, [
      "item_id", "capture_count", "latest_imported_at",
    ]);
    return freezeDeep({
      item_id: identifier(value.item_id, `${path}.item_id`),
      capture_count: positiveInteger(
        value.capture_count, `${path}.capture_count`),
      latest_imported_at: normalizeImportTimestamp(
        value.latest_imported_at, `${path}.latest_imported_at`),
    });
  }

  // Only books that have captures are marked, so an id missing from a loaded
  // set asserts "no captures" — as against the whole set being absent, which
  // is the separate state "not read yet".
  function normalizeCaptureMarks(value) {
    exactObject(value, "$", ["schema", "revision", "marks"]);
    if (value.schema !== CORRECTIONS_CAPTURE_MARKS_SCHEMA) {
      fail("$.schema", `must equal ${CORRECTIONS_CAPTURE_MARKS_SCHEMA}`);
    }
    const marks = boundedArray(value.marks, "$.marks", 100_000)
      .map((mark, index) => normalizeCaptureMark(mark, `$.marks[${index}]`));
    const itemIds = marks.map((mark) => mark.item_id.toLowerCase());
    if (new Set(itemIds).size !== itemIds.length) {
      fail("$.marks", "contains duplicate item identifiers");
    }
    return freezeDeep({
      schema: CORRECTIONS_CAPTURE_MARKS_SCHEMA,
      revision: revision(value.revision, "$.revision"),
      marks: freezeDeep(marks),
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
    exactObject(value, "$", [
      "schema", "index_revision", "entry", "index",
    ], ["schema", "index_revision", "entry"]);
    if (value.schema !== CORRECTIONS_REVIEW_RESULT_SCHEMA) {
      fail("$.schema", `must equal ${CORRECTIONS_REVIEW_RESULT_SCHEMA}`);
    }
    const indexRevision = revision(value.index_revision, "$.index_revision");
    const entry = normalizeAttentionEntry(value.entry, "$.entry");
    const result = {
      schema: CORRECTIONS_REVIEW_RESULT_SCHEMA,
      index_revision: indexRevision,
      entry,
    };
    if (value.index !== undefined) {
      // In lockstep with the store's own index read: the mutation's converged
      // index is the same shape the store keeps, not the full one.
      const index = normalizeCorrectionsIndexSummary(value.index);
      if (index.revision !== indexRevision) {
        fail("$.index_revision", "must match the complete index revision");
      }
      const indexedEntry = index.attention.find((candidate) =>
        targetIdentity(candidate.target) === targetIdentity(entry.target));
      if (!indexedEntry || indexedEntry.key !== entry.key ||
          !sameReviewSummary(indexedEntry.review, entry.review)) {
        fail("$.entry", "must exactly match its entry in the complete index");
      }
      result.index = index;
    }
    return freezeDeep(result);
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

  const BOOKS_PANEL_VIEWS = Object.freeze(["all", "captures", "attention"]);

  function importedAtTime(value) {
    if (!value) return NaN;
    const time = Date.parse(value);
    return Number.isFinite(time) ? time : NaN;
  }

  const EMPTY_LIST = Object.freeze([]);
  // Read, and resolved to nothing — as against an absent entry, which means
  // not read yet. Only the second may be shown as unknown.
  const MISSING_DETAIL = Object.freeze({
    import_state: "missing",
    issues: EMPTY_LIST,
    captures: EMPTY_LIST,
    latest_imported_at: "",
  });

  // The mark's stamp is already the newest of the book's captures — the engine
  // takes that maximum with the same time-then-value tie-break this comparison
  // uses — so the capture rows add nothing to the answer. That is what lets
  // the Captures view be ordered without reading a single capture row.
  function markImportedAt(marks, book) {
    const mark = marks && marks.get(book.id);
    return mark ? mark.latest_imported_at || "" : "";
  }

  function detailCaptures(details, book) {
    const detail = details && details.get(book.id);
    return detail ? detail.captures : EMPTY_LIST;
  }

  function compareCaptureBooks(left, right, marks = null) {
    const leftTime = importedAtTime(markImportedAt(marks, left));
    const rightTime = importedAtTime(markImportedAt(marks, right));
    const leftKnown = Number.isFinite(leftTime);
    if (leftKnown !== Number.isFinite(rightTime)) return leftKnown ? -1 : 1;
    if (leftKnown && leftTime !== rightTime) return rightTime - leftTime;
    return comparePortable(stableTitleKey(left.title), stableTitleKey(right.title)) ||
      comparePortable(left.id, right.id);
  }

  // Membership comes from the mark, which covers the whole collection, and not
  // from the loaded details, which cover only the drawn window — filtering on
  // the details would make the view grow as the reader scrolled it.
  function captureBooks(index, marks = null) {
    if (!index) return [];
    return index.books
      .filter((book) => {
        const mark = marks && marks.get(book.id);
        return mark ? mark.capture_count > 0 : false;
      })
      .sort((left, right) => compareCaptureBooks(left, right, marks));
  }

  function attentionBooks(index) {
    if (!index) return [];
    return sortedBooks(index).filter((book) =>
      bookNeedsAttention(book, index.attention));
  }

  function booksForView(index, view, marks = null) {
    if (view === "captures") return captureBooks(index, marks);
    if (view === "attention") return attentionBooks(index);
    return sortedBooks(index);
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

  function selectionExists(index, address, details = null) {
    const book = index.books.find((candidate) => candidate.id === address.itemId);
    if (!book) return false;
    if (!address.artifactId && !address.annotationId) return true;
    if (address.annotationId) {
      return index.attention.some((entry) =>
        entry.target.item_id === address.itemId &&
        entry.target.annotation_id === address.annotationId);
    }
    const detail = details && details.get(address.itemId);
    // Unknown is not absent. Until this book's captures are read there is no
    // evidence the artifact went away, and answering "gone" would delete the
    // reader's own selection and announce that it disappeared — on every cold
    // open of a capture address, which is the ordinary case.
    if (!detail) return true;
    return detail.captures.some((capture) =>
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
      this.onExternalChange =
        typeof options.onExternalChange === "function"
          ? options.onExternalChange : null;
      this.listeners = new Set();
      this.workspaceId = null;
      this.index = null;
      // null until the whole-collection read lands, and a Map afterwards. The
      // distinction is load-bearing: a loaded Map omits every book without
      // captures, so "absent id" and "nothing read yet" are different answers
      // and only the second may be shown as unknown.
      this.marks = null;
      this.marksPending = null;
      // Absent id means "not read yet" — details are only ever fetched for
      // the rows being drawn, so this Map is never a claim about the rest.
      this.details = new Map();
      this.detailsPending = new Set();
      this.detailsQueued = new Set();
      this.detailsFlush = null;
      this.status = this.api && typeof this.api.loadIndex === "function"
        ? "idle" : "unavailable";
      this.error = null;
      this.refreshReason = null;
      this.selection = null;
      this.selectionOwned = false;
      this.generation = 0;
      this.mutationGeneration = 0;
      this.mutationQueue = Promise.resolve();
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
        marks: this.marks,
        details: this.details,
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
      const next = value == null ? null : normalizeSelectionAddress(value);
      const ownsSelection = Object.prototype.hasOwnProperty.call(
        options, "ownedByFeature")
        ? options.ownedByFeature === true : this.selectionOwned;
      const unchanged = this.selection === null && next === null ||
        this.selection !== null && next !== null &&
          addressEqual(this.selection, next);
      if (unchanged && this.selectionOwned === ownsSelection) {
        return this.selection;
      }
      this.selection = next;
      this.selectionOwned = ownsSelection;
      this.emit();
      void this._ensureSelectionDetail();
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

    refresh(options = {}) {
      return this._refresh(options);
    }

    async _refresh(options = {}, owner = null) {
      if (owner && (
        owner.mutationGeneration !== this.mutationGeneration ||
        owner.workspaceId !== this.workspaceId
      )) return null;
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
      const workspaceId = this.workspaceId;
      const controller = typeof AbortController === "function"
        ? new AbortController() : null;
      this.abortController = controller;
      this.status = "loading";
      this.error = null;
      this.refreshReason = options.reason || "manual";
      this.emit();
      try {
        const value = await this.api.loadIndex({
          workspaceId,
          signal: controller && controller.signal,
        });
        if (this.destroyed || generation !== this.generation ||
            (owner &&
              owner.mutationGeneration !== this.mutationGeneration) ||
            workspaceId !== this.workspaceId) return null;
        const index = normalizeCorrectionsIndexSummary(value);
        this.abortController = null;
        this.index = index;
        // Dropped on every successful refresh, and deliberately not keyed on
        // index.revision. The summary revision covers only id, kind, title and
        // review, so importing a photo — or deleting one — leaves it byte for
        // byte the same. A cache that kept captures across an unchanged
        // revision would serve the reader a stale capture list for as long as
        // they left the window open. Refetching is the cost of that being
        // impossible.
        this._dropCaptureReads();
        this.status = "ready";
        this.error = null;
        this._reconcileSelection();
        this.emit();
        // A refresh is not finished until the selected item has answered. The
        // list is already painted by the emit above; what waits here is only
        // the promise, so callers that await a refresh — setContext, an
        // explicit refresh, the external-change notice — see the real capture
        // target instead of the placeholder they would never be told to
        // replace.
        await this._ensureSelectionDetail();
        return index;
      } catch (error) {
        if (this.destroyed || generation !== this.generation ||
            (owner &&
              owner.mutationGeneration !== this.mutationGeneration) ||
            workspaceId !== this.workspaceId || abortError(error)) return null;
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

    // The selected item's captures are what answer selectionExists and the
    // capture command target, so the store reads them itself rather than
    // waiting to be asked by a panel that may not be mounted.
    _ensureSelectionDetail() {
      const selection = this.selection;
      if (!selection || !selection.itemId || !selection.artifactId) return null;
      return this.ensureDetails([selection.itemId]);
    }

    _dropCaptureReads() {
      this.marks = null;
      this.marksPending = null;
      this.details = new Map();
      this.detailsPending = new Set();
      this.detailsQueued = new Set();
      this.detailsFlush = null;
    }

    ensureMarks() {
      if (this.marks || this.marksPending || !this.index || !this.workspaceId ||
          !this.api || typeof this.api.loadCaptureMarks !== "function") {
        return this.marksPending;
      }
      const workspaceId = this.workspaceId;
      const index = this.index;
      const pending = (async () => {
        try {
          const value = await this.api.loadCaptureMarks({ workspaceId });
          const marks = normalizeCaptureMarks(value);
          if (this.destroyed || this.marksPending !== pending ||
              this.index !== index) return;
          this.marks = new Map(marks.marks.map((mark) =>
            [mark.item_id, mark]));
          this.marksPending = null;
          this.emit();
        } catch (error) {
          // The capture counts are an ornament on a list that already renders.
          // Failing to read them leaves them unknown, which the shell shows as
          // unknown; it must not tear down the index the reader is using.
          if (!this.destroyed && this.marksPending === pending) {
            this.marksPending = null;
          }
        }
      })();
      this.marksPending = pending;
      return pending;
    }

    ensureDetails(itemIds) {
      if (!Array.isArray(itemIds) || !this.index || !this.workspaceId ||
          !this.api || typeof this.api.loadDetails !== "function") return null;
      let queued = false;
      for (const itemId of itemIds) {
        if (typeof itemId !== "string" || !itemId ||
            this.details.has(itemId) || this.detailsPending.has(itemId) ||
            this.detailsQueued.has(itemId)) continue;
        this.detailsQueued.add(itemId);
        queued = true;
      }
      if (!queued || this.detailsFlush) return this.detailsFlush;
      // A single render calls this from several places — the drawn rows, the
      // rows a step can reach. Coalescing through one turn of the microtask
      // queue turns that burst into one request, which matters because the
      // route's cost is dominated by listing the catalogue once, not by the
      // window's size.
      const flush = Promise.resolve().then(() => {
        if (this.detailsFlush !== flush) return null;
        this.detailsFlush = null;
        return this._loadQueuedDetails();
      });
      this.detailsFlush = flush;
      return flush;
    }

    async _loadQueuedDetails() {
      if (this.destroyed || !this.index || !this.workspaceId) return null;
      const itemIds = [...this.detailsQueued].slice(0, 256);
      if (!itemIds.length) return null;
      this.detailsQueued = new Set(
        [...this.detailsQueued].filter((itemId) => !itemIds.includes(itemId)));
      for (const itemId of itemIds) this.detailsPending.add(itemId);
      const workspaceId = this.workspaceId;
      const index = this.index;
      try {
        const value = await this.api.loadDetails({ workspaceId, itemIds });
        const detail = normalizeCorrectionsIndexDetail(value);
        if (this.destroyed || this.index !== index ||
            this.workspaceId !== workspaceId) return null;
        const details = new Map(this.details);
        for (const book of detail.books) details.set(book.id, book);
        // A book that no longer resolves is recorded as read-and-empty rather
        // than left pending, which would make every render ask again. The next
        // refresh drops it from the summary.
        for (const itemId of detail.missing) details.set(itemId, MISSING_DETAIL);
        this.details = details;
        for (const itemId of itemIds) this.detailsPending.delete(itemId);
        this._reconcileSelection();
        this.emit();
        return detail;
      } catch (error) {
        // Leaving these ids unloaded rather than marking them empty keeps the
        // difference between "no captures" and "not read" intact; the next
        // render asks again.
        if (!this.destroyed && this.index === index) {
          for (const itemId of itemIds) this.detailsPending.delete(itemId);
        }
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
      const trustedActor = this.api && this.api.trustedActor === true;
      const actorId = trustedActor
        ? null
        : identifier(options.actorId, "$.actor_id");
      const operationId = identifier(options.operationId, "$.operation_id");
      const comment = safeText(options.comment || "", "$.comment", 8192);
      const methodName = action === "resolve" ? "resolveReview" : "reopenReview";
      if (!this.api || typeof this.api[methodName] !== "function") {
        throw new Error(`${action === "resolve" ? "Resolve" : "Reopen"} is unavailable`);
      }
      const queuedWorkspaceId = this.workspaceId;
      const precedingMutation = this.mutationQueue;
      let releaseMutation;
      this.mutationQueue = new Promise((resolve) => {
        releaseMutation = resolve;
      });
      await precedingMutation;
      try {
        if (this.destroyed || queuedWorkspaceId !== this.workspaceId) {
          throw new CorrectionsReviewConflictError(
            "The Corrections workspace changed before the queued review could run");
        }
        return await this._transitionReview({
          action,
          actorId,
          comment,
          entry,
          methodName,
          operationId,
          signal: options.signal,
          trustedActor,
        });
      } finally {
        releaseMutation();
      }
    }

    async _transitionReview({
      action,
      actorId,
      comment,
      entry,
      methodName,
      operationId,
      signal,
      trustedActor,
    }) {
      const interruptedLoad = this.status === "loading";
      this._cancelLoad();
      if (interruptedLoad) {
        this.status = this.index ? "ready" : "idle";
        this.error = null;
        this.emit();
      }
      const loadGeneration = this.generation;
      const mutationGeneration = ++this.mutationGeneration;
      const workspaceId = this.workspaceId;
      const owner = Object.freeze({ mutationGeneration, workspaceId });
      try {
        const mutation = {
          target: entry.target,
          expectedRevision: entry.review.revision,
          operationId,
          comment,
          signal,
        };
        if (!trustedActor) mutation.actorId = actorId;
        const value = await this.api[methodName](mutation);
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
        if (this.destroyed ||
            mutationGeneration !== this.mutationGeneration ||
            workspaceId !== this.workspaceId) return result;
        if (result.index && loadGeneration === this.generation) {
          this.index = result.index;
          this.status = "ready";
          this.error = null;
          this._reconcileSelection();
          this.emit();
        } else {
          const converged = await this._refresh({
            reason: "review-mutation",
          }, owner);
          if (!converged) {
            if (this.destroyed ||
                mutationGeneration !== this.mutationGeneration ||
                workspaceId !== this.workspaceId) return result;
            if (this.status === "ready" || this.status === "loading") {
              return result;
            }
            throw new Error(
              "The committed review could not be converged with the full index");
          }
        }
        return result;
      } catch (error) {
        if (!isConflict(error)) throw error;
        if (!this.destroyed &&
            mutationGeneration === this.mutationGeneration &&
            workspaceId === this.workspaceId) {
          await this._refresh({ reason: "conflict" }, owner);
        }
        throw new CorrectionsReviewConflictError(undefined, error);
      }
    }

    applyAttentionEntry(entryValue, indexRevision) {
      if (!this.index) throw new Error("The Corrections index has not been loaded");
      this._cancelLoad();
      this.mutationGeneration += 1;
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
        schema: CORRECTIONS_INDEX_SUMMARY_SCHEMA,
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
          selectionExists(this.index, this.selection, this.details)) return;
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
                const workspaceId = this.workspaceId;
                void Promise.resolve(this.refresh({ reason: "external" }))
                  .finally(() => {
                    if (this.destroyed || workspaceId !== this.workspaceId ||
                        !this.onExternalChange) return;
                    return this.onExternalChange(Object.freeze({
                      workspaceId,
                      revision: change.revision,
                    }));
                  })
                  .catch(() => {
                    // The index owns its own load error state. A secondary
                    // panel refresh failure must not break future notices.
                  });
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
      this.mutationGeneration += 1;
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

  function nodeInside(root, node) {
    if (!root || !node) return false;
    if (root === node) return true;
    if (typeof root.contains === "function") return root.contains(node);
    let cursor = node;
    while (cursor) {
      if (cursor === root) return true;
      cursor = cursor.parentNode;
    }
    return false;
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

  function captureCommandTarget(book, capture) {
    // ``index:`` revisions are navigation hints derived without reading image
    // bytes. They are never valid optimistic-concurrency preconditions. The
    // artifact feature publishes an authoritative target after detail hydration.
    if (String(capture && capture.revision || "").startsWith("index:")) {
      return null;
    }
    return freezeDeep({
      key: `artifact:${capture.artifact_id}`,
      objectType: "raster-artifact",
      family: "image",
      group: "source-images",
      kind: "capture",
      itemId: book.id,
      id: capture.artifact_id,
      artifactId: capture.artifact_id,
      revision: capture.revision,
      label: capture.label || `Capture ${capture.capture_order + 1}`,
      effectiveCategory: capture.effective_category,
      source: {
        representationId: capture.representation_id || "",
        canvasId: capture.canvas_id || "",
      },
    });
  }

  function captureIsNavigationHint(capture) {
    return String(capture && capture.revision || "").startsWith("index:");
  }

  function captureNavigationPreview(book, capture) {
    if (!captureIsNavigationHint(capture) || !capture.thumbnail) return null;
    return freezeDeep({
      itemId: book.id,
      representationId: capture.representation_id || null,
      canvasId: capture.canvas_id || null,
      artifactId: capture.artifact_id,
      url: capture.thumbnail.url,
      label: capture.label.trim() || `Capture ${capture.capture_order + 1}`,
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
      this.onSelectionTarget = typeof options.onSelectionTarget === "function"
        ? options.onSelectionTarget : () => {};
      this.onHotTarget = typeof options.onHotTarget === "function"
        ? options.onHotTarget : () => {};
      this.onStatus = typeof options.onStatus === "function"
        ? options.onStatus : () => {};
      this.filter = "";
      this.view = "all";
      this.viewControls = null;
      this.navControls = null;
      this.lastViewReference = null;
      this.pendingStepFocus = null;
      this.pendingSelectionTarget = "";
      this.renderBatch = Number.isSafeInteger(options.renderBatch) &&
        options.renderBatch >= 8 && options.renderBatch <= 200
        ? options.renderBatch : 48;
      this.renderLimit = this.renderBatch;
      this.captureRenderBatch = Number.isSafeInteger(options.captureRenderBatch) &&
        options.captureRenderBatch >= 4 && options.captureRenderBatch <= 100
        ? options.captureRenderBatch : 12;
      this.captureRenderLimits = new Map();
      this.captureRenderRevision = 0;
      const view = this.documentRef && this.documentRef.defaultView;
      const Observer = options.IntersectionObserver ||
        view && view.IntersectionObserver ||
        (typeof IntersectionObserver === "function" ? IntersectionObserver : null);
      this.thumbnailObserverFactory =
        typeof options.thumbnailObserverFactory === "function"
          ? options.thumbnailObserverFactory
          : Observer
            ? (callback, observerOptions) =>
                new Observer(callback, observerOptions)
            : null;
      this.thumbnailObserver = null;
      this.pendingThumbnails = new Map();
      this.renderedIndex = null;
      // Both are replaced wholesale rather than mutated, so identity is enough
      // to notice that captures arrived under an unchanged index.
      this.renderedDetails = null;
      this.renderedMarks = null;
      this.renderedStatus = "";
      this.renderedError = null;
      this.renderedFilter = "";
      this.renderedView = "";
      this.renderedBookPin = "";
      this.renderedCapturePin = "";
      this.renderedLimit = 0;
      this.renderedCaptureRevision = 0;
      this.unsubscribe = null;
      this.listeners = [];
      this.rowListeners = [];
      this.hotCapture = false;
      this.deferSelectedThumbnail = false;
      this.mounted = false;
    }

    resetThumbnailHydration(list = null) {
      if (this.thumbnailObserver &&
          typeof this.thumbnailObserver.disconnect === "function") {
        this.thumbnailObserver.disconnect();
      }
      this.thumbnailObserver = null;
      this.pendingThumbnails.clear();
      if (!list || !this.thumbnailObserverFactory) return;
      try {
        // The list grows to fit every rendered row; its parent .pane-body is
        // the actual scrollport. Using the list itself as the observer root
        // would make every off-screen descendant intersect immediately.
        const observerRoot = list.parentNode || null;
        const observer = this.thumbnailObserverFactory((entries) => {
          for (const entry of Array.from(entries || [])) {
            if (!entry || (!entry.isIntersecting && !(entry.intersectionRatio > 0))) {
              continue;
            }
            this.hydrateCaptureThumbnail(entry.target);
          }
        }, {
          root: observerRoot,
          rootMargin: "192px 0px",
          threshold: 0.01,
        });
        if (observer && typeof observer.observe === "function") {
          this.thumbnailObserver = observer;
        }
      } catch (error) {
        // IntersectionObserver is a performance enhancement. A broken or
        // partially implemented observer must retain the eager fallback.
        this.thumbnailObserver = null;
      }
    }

    hydrateCaptureThumbnail(button, options = {}) {
      const pending = this.pendingThumbnails.get(button);
      if (!pending) return false;
      if (options.priority === "high") pending.image.fetchPriority = "high";
      if (pending.started) return true;
      pending.started = true;
      if (this.thumbnailObserver &&
          typeof this.thumbnailObserver.unobserve === "function") {
        try {
          this.thumbnailObserver.unobserve(button);
        } catch (error) {
          // The image can still hydrate after an observer tears itself down.
        }
      }
      if (pending.image.fetchPriority !== "high") {
        pending.image.fetchPriority = "low";
      }
      pending.image.src = pending.url;
      if (button && button.dataset) button.dataset.thumbnailState = "loading";
      return true;
    }

    settleCaptureThumbnail(button, pending, loaded) {
      if (this.pendingThumbnails.get(button) !== pending) return;
      this.pendingThumbnails.delete(button);
      if (loaded) {
        pending.image.hidden = false;
        if (pending.placeholder && pending.placeholder.parentNode &&
            typeof pending.placeholder.parentNode.removeChild === "function") {
          pending.placeholder.parentNode.removeChild(pending.placeholder);
        }
        if (button && button.dataset) button.dataset.thumbnailState = "loaded";
        return;
      }
      pending.image.hidden = true;
      if (pending.placeholder) {
        pending.placeholder.textContent = "Image unavailable";
        pending.placeholder.className =
          "capture-thumbnail capture-thumbnail-placeholder is-error";
        setAttribute(pending.placeholder, "aria-hidden", "false");
      }
      if (button && button.dataset) button.dataset.thumbnailState = "error";
    }

    appendCaptureThumbnail(button, capture, state, selected = false) {
      if (!capture.thumbnail) {
        const placeholder = element(this.documentRef, "span",
          "capture-thumbnail capture-thumbnail-placeholder", state);
        setAttribute(placeholder, "aria-hidden", "true");
        button.append(placeholder);
        return;
      }
      const image = element(this.documentRef, "img", "capture-thumbnail");
      image.alt = capture.thumbnail.alt;
      // Native lazy loading is a second line of defence for browsers that
      // schedule a request between src assignment and observer teardown.
      image.loading = "lazy";
      image.decoding = "async";
      image.hidden = true;
      if (capture.thumbnail.width) image.width = capture.thumbnail.width;
      if (capture.thumbnail.height) image.height = capture.thumbnail.height;
      const placeholder = element(this.documentRef, "span",
        "capture-thumbnail capture-thumbnail-placeholder is-loading", "");
      setAttribute(placeholder, "aria-hidden", "true");
      button.append(placeholder, image);
      if (button.dataset) button.dataset.thumbnailState = "pending";
      const pending = {
        image,
        placeholder,
        url: capture.thumbnail.url,
        started: false,
      };
      this.pendingThumbnails.set(button, pending);
      this.listenRow(image, "load", () =>
        this.settleCaptureThumbnail(button, pending, true));
      this.listenRow(image, "error", () =>
        this.settleCaptureThumbnail(button, pending, false));
      if (selected) {
        this.hydrateCaptureThumbnail(button, { priority: "high" });
        return;
      }
      if (!this.thumbnailObserver) {
        this.hydrateCaptureThumbnail(button);
        return;
      }
      try {
        this.thumbnailObserver.observe(button);
      } catch (error) {
        this.hydrateCaptureThumbnail(button);
      }
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
        this.renderLimit = this.renderBatch;
        this.render(this.store.snapshot());
      });
      this.listen(filter, "keydown", (event) => {
        if (event.key !== "Escape" || !filter.value) return;
        event.preventDefault();
        filter.value = "";
        this.filter = "";
        this.renderLimit = this.renderBatch;
        this.render(this.store.snapshot());
      });
      this.mountViewControls();
      this.unsubscribe = this.store.subscribe((snapshot) => this.render(snapshot));
      return this;
    }

    // The Books markup predates the view control, so the panel builds its own
    // toolbar row directly above the list.
    mountViewControls() {
      if (this.viewControls) return;
      const list = this.root.querySelector("[data-books-list]");
      const host = list && list.parentNode;
      if (!host || typeof host.insertBefore !== "function" ||
          !this.documentRef) return;
      const bar = element(this.documentRef, "div", "books-view-bar");
      // The bar sits beside [data-books-list], outside every surface the
      // shell keymap accepts; its own hook keeps j/k alive after a click on
      // the view or prev/next buttons.
      if (bar.dataset) bar.dataset.booksViewBar = "";
      const group = element(this.documentRef, "div", "books-view-switch");
      setAttribute(group, "role", "group");
      setAttribute(group, "aria-label", "Books view");
      this.viewControls = new Map();
      for (const [view, label] of [
        ["all", "All"], ["captures", "Captures"], ["attention", "Attention"],
      ]) {
        const button = element(this.documentRef, "button", "books-view-option");
        button.type = "button";
        if (button.dataset) button.dataset.booksView = view;
        const count = element(this.documentRef, "span", "books-view-count", "0");
        setAttribute(count, "aria-hidden", "true");
        button.append(element(this.documentRef, "span", "", label), count);
        this.listen(button, "click", () => this.setView(view));
        this.viewControls.set(view, Object.freeze({ button, count }));
        group.append(button);
      }
      const nav = element(this.documentRef, "div", "books-view-nav");
      this.navControls = new Map();
      for (const [direction, name, glyph, label] of [
        [-1, "previous", "‹", "Select the previous item"],
        [1, "next", "›", "Select the next item"],
      ]) {
        const button = element(this.documentRef, "button",
          "books-nav-button", glyph);
        button.type = "button";
        if (button.dataset) button.dataset.booksNav = name;
        setAttribute(button, "aria-label", label);
        this.listen(button, "click", () => this.stepSelection(direction));
        this.navControls.set(direction, button);
        nav.append(button);
      }
      bar.append(group, nav);
      host.insertBefore(bar, list);
    }

    setView(view) {
      if (!BOOKS_PANEL_VIEWS.includes(view) || view === this.view) return this.view;
      this.view = view;
      this.renderLimit = this.renderBatch;
      this.render(this.store.snapshot());
      return this.view;
    }

    setSelection(address, options = {}) {
      const storeOptions = {};
      if (Object.prototype.hasOwnProperty.call(options, "ownedByFeature")) {
        storeOptions.ownedByFeature = options.ownedByFeature === true;
      }
      this.store.setSelection(address, storeOptions);
      this.syncSelectionTarget(address, { focused: false, source: "selection" });
    }

    commandTargetForSelection(address) {
      const match = this.captureForSelection(address);
      return match ? captureCommandTarget(match.book, match.capture) : null;
    }

    // Null while the book's captures are still being read. Every caller here
    // already treats null as "no capture target yet" — the same answer they
    // get for a book address — so a pending detail degrades to a bare
    // selection rather than a wrong one.
    captureForSelection(address) {
      if (!address || !address.itemId || !address.artifactId) return null;
      const snapshot = this.store.snapshot();
      const book = snapshot.index && snapshot.index.books
        .find((candidate) => candidate.id === address.itemId);
      const capture = book && detailCaptures(snapshot.details, book)
        .find((candidate) => candidate.artifact_id === address.artifactId);
      return book && capture ? { book, capture } : null;
    }

    syncSelectionTarget(address, options = {}) {
      const match = this.captureForSelection(address);
      const target = match
        ? captureCommandTarget(match.book, match.capture) : null;
      this.onSelectionTarget(target, {
        focused: options.focused === true,
        source: options.source || "books",
        navigationHint: !!match && captureIsNavigationHint(match.capture),
        address: match
          ? captureAddress(match.book, match.capture) : address || null,
      });
      return target;
    }

    visibleBooks(snapshot) {
      if (!snapshot.index) return [];
      const query = stableTitleKey(this.filter.trim());
      return booksForView(snapshot.index, this.view, snapshot.marks)
        .filter((book) => {
          if (!query) return true;
          return stableTitleKey(book.title).includes(query) ||
            stableTitleKey(book.id).includes(query);
        });
    }

    viewOrderKey() {
      return `${this.view}:${stableTitleKey(this.filter.trim())}`;
    }

    rememberSelectionIndex(snapshot, books) {
      const selection = snapshot.selection;
      const index = selection && selection.itemId
        ? books.findIndex((book) => book.id === selection.itemId) : -1;
      if (index >= 0) {
        this.lastViewReference = Object.freeze({
          key: this.viewOrderKey(),
          index,
        });
      } else if (this.lastViewReference &&
          this.lastViewReference.key !== this.viewOrderKey()) {
        this.lastViewReference = null;
      }
    }

    stepTarget(direction) {
      const snapshot = this.store.snapshot();
      const books = this.visibleBooks(snapshot);
      if (!books.length) return null;
      const selection = snapshot.selection;
      const currentIndex = selection && selection.itemId
        ? books.findIndex((book) => book.id === selection.itemId) : -1;
      if (currentIndex >= 0) return books[currentIndex + direction] || null;
      const reference = this.lastViewReference &&
        this.lastViewReference.key === this.viewOrderKey()
        ? this.lastViewReference.index : null;
      if (selection && selection.itemId && reference !== null) {
        // The selected item left this view (for example it was resolved while
        // in the Attention view): next takes the row now holding its slot,
        // previous takes the row before that slot. A remembered slot beyond a
        // since-shrunken view clamps to just past the end, so previous can
        // still reach the last remaining row instead of going dead.
        const anchor = Math.min(reference, books.length);
        const slot = direction > 0
          ? Math.min(anchor, books.length - 1)
          : anchor - 1;
        return slot >= 0 && slot < books.length ? books[slot] : null;
      }
      return direction > 0 ? books[0] : books[books.length - 1];
    }

    canStepSelection(direction) {
      return this.stepTarget(direction) !== null;
    }

    stepSelection(direction) {
      const book = this.stepTarget(direction);
      if (!book) return null;
      // Focus follows the step: navigation rebuilds this list (and may
      // replace the editor), which drops DOM focus onto the document body —
      // outside every keymap surface — so without a restored focus target
      // the next j/k would never reach the shell.
      this.pendingStepFocus = book.id;
      // Stepping is the fix-page → next-page loop: land on the item's first
      // capture so its photo opens immediately. Items without captures keep
      // the bare book address, as do items whose captures have not been read
      // yet — which is why the render asks for the step neighbours' details
      // alongside the rows it draws.
      const capture = detailCaptures(this.store.snapshot().details, book)[0];
      if (capture) {
        this.navigate(
          captureAddress(book, capture),
          "image",
          captureNavigationPreview(book, capture),
        );
      }
      else this.navigate(bookAddress(book), "book");
      return book;
    }

    renderViewControls(snapshot) {
      if (this.viewControls) {
        const index = snapshot.index;
        const counts = {
          all: index ? String(index.books.length) : "0",
          // How many books have captures is the one count that costs a
          // whole-collection read, so it is only known once the Captures view
          // has asked for it. Under-stated rather than absent: the control
          // keeps its shape and says it does not know.
          captures: index && snapshot.marks
            ? String(captureBooks(index, snapshot.marks).length) : "—",
          attention: index ? String(attentionBooks(index).length) : "0",
        };
        for (const [view, control] of this.viewControls) {
          setAttribute(control.button, "aria-pressed",
            view === this.view ? "true" : "false");
          control.count.textContent = counts[view];
          if (control.count.classList) {
            if (counts[view] === "—") control.count.classList.add("is-unknown");
            else control.count.classList.remove("is-unknown");
          }
        }
      }
      if (this.navControls) {
        for (const [direction, button] of this.navControls) {
          button.disabled = !this.canStepSelection(direction);
        }
      }
    }

    focusedCapture(list) {
      const active = this.documentRef && this.documentRef.activeElement;
      if (!active || !nodeInside(list, active)) return null;
      let owner = active;
      while (owner && owner !== list) {
        const dataset = owner.dataset || {};
        if (dataset.itemId && dataset.artifactId) {
          return Object.freeze({
            itemId: dataset.itemId,
            artifactId: dataset.artifactId,
          });
        }
        owner = owner.parentNode;
      }
      return null;
    }

    captureElement(list, capture) {
      if (!capture || !list ||
          typeof list.querySelectorAll !== "function") return null;
      return Array.from(list.querySelectorAll("[data-artifact-id]"))
        .find((candidate) => {
          const dataset = candidate.dataset || {};
          return dataset.itemId === capture.itemId &&
            dataset.artifactId === capture.artifactId;
        }) || null;
    }

    focusedBook(list) {
      const active = this.documentRef && this.documentRef.activeElement;
      if (!active || !nodeInside(list, active)) return null;
      let owner = active;
      while (owner && owner !== list) {
        const dataset = owner.dataset || {};
        if (dataset.bookSelect) return dataset.bookSelect;
        owner = owner.parentNode;
      }
      return null;
    }

    bookElement(list, itemId) {
      if (!itemId || !list ||
          typeof list.querySelectorAll !== "function") return null;
      return Array.from(list.querySelectorAll("[data-book-select]"))
        .find((candidate) =>
          (candidate.dataset || {}).bookSelect === itemId) || null;
    }

    restoreButtonFocus(button) {
      if (!button || typeof button.focus !== "function") return;
      try {
        button.focus({ preventScroll: true });
      } catch (error) {
        button.focus();
      }
    }

    restoreCaptureFocus(list, capture) {
      this.restoreButtonFocus(this.captureElement(list, capture));
    }

    restoreBookFocus(list, itemId) {
      this.restoreButtonFocus(this.bookElement(list, itemId));
    }

    syncRenderedSelection(list, snapshot) {
      if (!list || typeof list.querySelectorAll !== "function") return;
      const selection = snapshot.selection;
      for (const button of Array.from(list.querySelectorAll("button"))) {
        const dataset = button.dataset || {};
        if (!dataset.itemId) continue;
        const selected = !!selection && selection.itemId === dataset.itemId &&
          (dataset.artifactId
            ? selection.artifactId === dataset.artifactId
            : !selection.artifactId && !selection.annotationId);
        setAttribute(button, "aria-pressed", selected ? "true" : "false");
        if (selected && dataset.artifactId && !this.deferSelectedThumbnail) {
          this.hydrateCaptureThumbnail(button, { priority: "high" });
        }
      }
    }

    selectionRenderPins(snapshot, books) {
      const selection = snapshot.selection;
      if (!selection || !selection.itemId) {
        return { book: "", capture: "" };
      }
      const bookIndex = books.findIndex((book) => book.id === selection.itemId);
      if (bookIndex < 0) return { book: "", capture: "" };
      const book = books[bookIndex];
      const bookPin = bookIndex >= this.renderLimit ? book.id : "";
      if (!selection.artifactId) return { book: bookPin, capture: "" };
      const captureIndex = detailCaptures(snapshot.details, book)
        .findIndex((capture) => capture.artifact_id === selection.artifactId);
      const captureLimit = this.captureRenderLimits.get(book.id) ||
        this.captureRenderBatch;
      const capturePin = captureIndex >= captureLimit
        ? JSON.stringify([book.id, selection.artifactId]) : "";
      return { book: bookPin, capture: capturePin };
    }

    render(snapshot) {
      const list = this.root.querySelector("[data-books-list]");
      // A step owns the next rebuild's focus; otherwise the rebuild only
      // preserves whichever row or capture button already held it, so
      // renders that were not caused by a step never steal focus.
      const stepFocus = this.pendingStepFocus;
      const focusedCapture = stepFocus ? null : this.focusedCapture(list);
      const focusedBook = stepFocus ||
        (focusedCapture ? null : this.focusedBook(list));
      const count = this.root.querySelector("[data-books-count]");
      if (!list || !this.documentRef) return;
      if (this.renderedIndex !== snapshot.index) {
        this.renderLimit = this.renderBatch;
        this.captureRenderLimits.clear();
        this.captureRenderRevision += 1;
      }
      // The Captures view orders and filters the whole collection, so it needs
      // every mark; no other view does, which is why this is asked for here
      // and not with the index.
      if (this.view === "captures") this.store.ensureMarks();
      const books = this.visibleBooks(snapshot);
      this.requestVisibleDetails(snapshot, books);
      const renderPins = this.selectionRenderPins(snapshot, books);
      const renderedButtons = typeof list.querySelectorAll === "function"
        ? Array.from(list.querySelectorAll("button")) : [];
      const selectedNodeMissing = !!(
        snapshot.selection && snapshot.selection.itemId &&
        (!snapshot.selection.artifactId ||
          !this.captureElement(list, snapshot.selection)) &&
        !renderedButtons.some((button) => {
          const dataset = button.dataset || {};
          return dataset.itemId === snapshot.selection.itemId &&
            (!snapshot.selection.artifactId ||
              dataset.artifactId === snapshot.selection.artifactId);
        })
      );
      const selectionOnly = !this.hotCapture &&
        this.renderedIndex === snapshot.index &&
        this.renderedDetails === snapshot.details &&
        this.renderedMarks === snapshot.marks &&
        this.renderedStatus === snapshot.status &&
        this.renderedError === snapshot.error &&
        this.renderedFilter === this.filter &&
        this.renderedView === this.view &&
        this.renderedBookPin === renderPins.book &&
        this.renderedCapturePin === renderPins.capture &&
        this.renderedLimit === this.renderLimit &&
        this.renderedCaptureRevision === this.captureRenderRevision &&
        !selectedNodeMissing;
      this.rememberSelectionIndex(snapshot, books);
      this.renderViewControls(snapshot);
      this.publishPendingSelectionTarget(snapshot);
      if (selectionOnly) {
        this.syncRenderedSelection(list, snapshot);
        this.pendingStepFocus = null;
        this.restoreBookFocus(list, stepFocus);
        return;
      }
      this.renderedIndex = snapshot.index;
      this.renderedDetails = snapshot.details;
      this.renderedMarks = snapshot.marks;
      this.renderedStatus = snapshot.status;
      this.renderedError = snapshot.error;
      this.renderedFilter = this.filter;
      this.renderedView = this.view;
      this.renderedBookPin = renderPins.book;
      this.renderedCapturePin = renderPins.capture;
      this.renderedLimit = this.renderLimit;
      this.renderedCaptureRevision = this.captureRenderRevision;
      this.pendingStepFocus = null;
      for (const remove of this.rowListeners.splice(0)) remove();
      this.resetThumbnailHydration(list);
      // Replacing rows under a stationary pointer fires no pointerleave, so
      // an active hover contribution must be withdrawn before its row goes.
      if (this.hotCapture) {
        this.hotCapture = false;
        this.onHotTarget(null, { source: "books" });
      }
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
        if (this.filter.trim()) {
          list.append(this.messageRow("No matches",
            `No books match “${this.filter.trim().slice(0, 120)}”.`));
        } else if (this.view === "captures") {
          list.append(this.messageRow("No captures",
            "No items have synced phone captures."));
        } else {
          list.append(this.messageRow("Nothing needs attention",
            "No items are marked for attention."));
        }
        return;
      }
      let visible = books.slice(0, this.renderLimit);
      const selectedBook = snapshot.selection && books.find(
        (book) => book.id === snapshot.selection.itemId);
      if (selectedBook && !visible.includes(selectedBook)) {
        visible = [
          ...visible.slice(0, Math.max(0, this.renderLimit - 1)),
          selectedBook,
        ];
      }
      for (const book of visible) list.append(this.renderBook(book, snapshot));
      if (visible.length < books.length) {
        const row = element(this.documentRef, "li", "books-load-more");
        const button = element(
          this.documentRef,
          "button",
          "books-load-more-button",
          `Show ${Math.min(this.renderBatch, books.length - visible.length)} more`,
        );
        button.type = "button";
        if (button.dataset) button.dataset.booksLoadMore = "";
        setAttribute(button, "aria-label", button.textContent);
        this.listenRow(button, "click", () => {
          const firstNewBook = books[this.renderLimit] || null;
          this.renderLimit += this.renderBatch;
          this.render(this.store.snapshot());
          const replacement = typeof list.querySelectorAll === "function"
            ? Array.from(list.querySelectorAll("[data-books-load-more]"))[0] || null
            : null;
          if (replacement) this.restoreButtonFocus(replacement);
          else this.restoreBookFocus(list, firstNewBook && firstNewBook.id);
        });
        row.append(button);
        list.append(row);
      }
      this.syncRenderedSelection(list, snapshot);
      this.restoreCaptureFocus(list, focusedCapture);
      this.restoreBookFocus(list, focusedBook);
    }

    // A capture's command target cannot be known until its book's captures
    // have been read, so a cold open on a capture address publishes "no target
    // yet". This answers it when the read lands: every other publication is
    // driven by the selection changing, and this selection never changed.
    // Fires once per selection that was unresolvable and became resolvable, so
    // a selection that was answerable all along publishes exactly as before.
    publishPendingSelectionTarget(snapshot) {
      const selection = snapshot.selection;
      const key = selection && selection.itemId && selection.artifactId
        ? `${selection.itemId} ${selection.artifactId}` : "";
      if (!key) {
        this.pendingSelectionTarget = "";
        return;
      }
      if (!this.commandTargetForSelection(selection)) {
        this.pendingSelectionTarget = key;
        return;
      }
      if (this.pendingSelectionTarget !== key) return;
      this.pendingSelectionTarget = "";
      this.syncSelectionTarget(selection, {
        focused: false,
        source: "captures",
      });
    }

    // The rows about to be drawn, plus the rows one step of the keyboard can
    // reach. The step targets cost two ids and are what keeps j/k landing on
    // an item's first photo instead of its bare book address; without them the
    // row past the render limit is always a page the reader has to open twice.
    requestVisibleDetails(snapshot, books) {
      if (typeof this.store.ensureDetails !== "function") return;
      const wanted = books.slice(0, this.renderLimit).map((book) => book.id);
      const selection = snapshot.selection;
      const selected = selection && selection.itemId
        ? books.findIndex((book) => book.id === selection.itemId) : -1;
      if (selected >= 0) {
        for (const neighbour of [books[selected - 1], books[selected + 1]]) {
          if (neighbour) wanted.push(neighbour.id);
        }
      }
      if (selection && selection.itemId) wanted.push(selection.itemId);
      this.store.ensureDetails(wanted);
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
      if (select.dataset) {
        select.dataset.bookSelect = book.id;
        select.dataset.itemId = book.id;
      }
      const title = book.title.trim() || `Untitled (${book.id})`;
      const selected = snapshot.selection &&
        snapshot.selection.itemId === book.id && !snapshot.selection.artifactId &&
        !snapshot.selection.annotationId;
      setAttribute(select, "aria-pressed", selected ? "true" : "false");
      setAttribute(select, "aria-label",
        `${title}${book.kind === "capture" ? ", captured entry" : ""}` +
        `${needsAttention ? ", needs attention" : ""}`);
      select.append(element(this.documentRef, "span", "book-title", title));
      if (book.kind === "capture") {
        select.append(element(
          this.documentRef,
          "span",
          "book-kind",
          "Captured entry",
        ));
      }
      if (needsAttention) {
        const attention = element(this.documentRef, "span", "book-attention");
        const icon = element(this.documentRef, "span", "book-attention-icon", "!");
        setAttribute(icon, "aria-hidden", "true");
        attention.append(icon, element(this.documentRef, "span", "", "Needs attention"));
        select.append(attention);
      }
      const detail = snapshot.details && snapshot.details.get(book.id) || null;
      // The import state is a capture-derived fact, so it is unknown until the
      // detail lands. Saying nothing is already how this row reports "ready" —
      // the chip only ever appears for the exceptions — so an unread book is
      // indistinguishable from a healthy one for the moment it takes to read.
      if (detail && detail.import_state !== "ready") {
        select.append(element(this.documentRef, "span", "book-import-state",
          `${detail.import_state.replace("_", " ")} import`));
      }
      this.listenRow(select, "click", () => this.navigate(bookAddress(book), "book"));
      row.append(select);

      const bookCaptures = detail ? detail.captures : null;
      if (!bookCaptures) {
        const mark = snapshot.marks && snapshot.marks.get(book.id);
        const pending = element(this.documentRef, "p", "book-captures-pending",
          mark
            ? `Loading ${mark.capture_count} captured image${
                mark.capture_count === 1 ? "" : "s"}…`
            : "Loading captured images…");
        setAttribute(pending, "aria-busy", "true");
        row.append(pending);
        return row;
      }
      if (!bookCaptures.length) {
        row.append(element(this.documentRef, "p", "book-no-captures",
          "No captured images"));
        return row;
      }
      const captures = element(this.documentRef, "ul", "book-captures");
      setAttribute(captures, "aria-label", `Captured images for ${title}`);
      const captureLimit = this.captureRenderLimits.get(book.id) ||
        this.captureRenderBatch;
      let visibleCaptures = bookCaptures.slice(0, captureLimit);
      const selectedCapture = snapshot.selection &&
        snapshot.selection.itemId === book.id &&
        bookCaptures.find((capture) =>
          capture.artifact_id === snapshot.selection.artifactId);
      if (selectedCapture && !visibleCaptures.includes(selectedCapture)) {
        visibleCaptures = [
          ...visibleCaptures.slice(0, Math.max(0, captureLimit - 1)),
          selectedCapture,
        ];
      }
      for (const capture of visibleCaptures) {
        const commandTarget = captureCommandTarget(book, capture);
        const address = captureAddress(book, capture);
        const navigationHint = captureIsNavigationHint(capture);
        const item = element(this.documentRef, "li", "book-capture");
        const button = element(this.documentRef, "button", "capture-select");
        button.type = "button";
        const category = CATEGORY_PRESENTATION[capture.effective_category];
        const state = captureState(capture);
        const label = capture.label.trim() ||
          `Capture ${capture.capture_order + 1}`;
        setAttribute(button, "aria-label",
          `${label}, ${category.label}, ${state}`);
        const captureSelected =
          addressEqual(snapshot.selection, address);
        setAttribute(button, "aria-pressed", captureSelected ? "true" : "false");
        if (button.dataset) {
          button.dataset.itemId = book.id;
          button.dataset.artifactId = capture.artifact_id;
          button.dataset.category = capture.effective_category;
          button.dataset.resourceState = capture.resource_state;
        }
        this.appendCaptureThumbnail(button, capture, state, captureSelected);
        const chip = element(this.documentRef, "span", "capture-category");
        const icon = element(this.documentRef, "span", "capture-category-icon",
          category.icon);
        setAttribute(icon, "aria-hidden", "true");
        chip.append(icon, element(this.documentRef, "span", "", category.label));
        button.append(
          chip,
          element(this.documentRef, "span", "capture-state", state),
        );
        this.listenRow(button, "pointerenter", () => {
          this.hydrateCaptureThumbnail(button);
          this.hotCapture = true;
          this.onHotTarget(commandTarget, { element: button, source: "books" });
        });
        this.listenRow(button, "pointerleave", () => {
          this.hotCapture = false;
          this.onHotTarget(null, { element: button, source: "books" });
        });
        this.listenRow(button, "focus", () => {
          this.hydrateCaptureThumbnail(button);
          this.onSelectionTarget(commandTarget, {
            element: button,
            focused: true,
            source: "books",
            navigationHint,
            address,
          });
        });
        this.listenRow(button, "blur", () => {
          const selection = this.store.snapshot().selection;
          const selectedTarget = this.commandTargetForSelection(selection);
          const list = this.root.querySelector("[data-books-list]");
          // A navigation hint has no mutation revision. Once the artifact
          // feature hydrates and publishes the authoritative target, a later
          // blur must not replace it with this deliberate null placeholder.
          // Annotation selection is equally authoritative even though it has
          // no capture artifact ID for the Books index to resolve.
          if (!selectedTarget && selection &&
              (selection.artifactId || selection.annotationId)) return;
          this.onSelectionTarget(selectedTarget, {
            element: this.captureElement(list, selection),
            focused: false,
            source: "books",
          });
        });
        this.listenRow(button, "click", () => {
          this.onSelectionTarget(commandTarget, {
            element: button,
            focused: true,
            source: "books",
            navigationHint,
            address,
          });
          this.deferSelectedThumbnail = true;
          try {
            this.navigate(
              address,
              "image",
              captureNavigationPreview(book, capture),
            );
          } finally {
            this.deferSelectedThumbnail = false;
          }
          // Navigation publishes the selected address first so authoritative
          // detail work is never queued behind a thumbnail request.
          this.hydrateCaptureThumbnail(button, { priority: "high" });
        });
        item.append(button);
        captures.append(item);
      }
      if (visibleCaptures.length < bookCaptures.length) {
        const moreItem = element(
          this.documentRef, "li", "book-captures-load-more");
        const remaining = bookCaptures.length - visibleCaptures.length;
        const more = element(
          this.documentRef,
          "button",
          "book-captures-load-more-button",
          `Show ${Math.min(this.captureRenderBatch, remaining)} more captures`,
        );
        more.type = "button";
        if (more.dataset) more.dataset.capturesLoadMore = book.id;
        setAttribute(more, "aria-label",
          `${more.textContent} for ${title}`);
        this.listenRow(more, "click", () => {
          const firstNewCapture = bookCaptures[captureLimit] || null;
          this.captureRenderLimits.set(book.id,
            captureLimit + this.captureRenderBatch);
          this.captureRenderRevision += 1;
          this.render(this.store.snapshot());
          const currentList = this.root.querySelector("[data-books-list]");
          const replacement = currentList &&
              typeof currentList.querySelectorAll === "function"
            ? Array.from(currentList.querySelectorAll("[data-captures-load-more]"))
              .find((button) =>
                (button.dataset || {}).capturesLoadMore === book.id) || null
            : null;
          if (replacement) this.restoreButtonFocus(replacement);
          else if (firstNewCapture) {
            this.restoreCaptureFocus(currentList, {
              itemId: book.id,
              artifactId: firstNewCapture.artifact_id,
            });
          }
        });
        moreItem.append(more);
        captures.append(moreItem);
      }
      row.append(captures);
      return row;
    }

    navigate(address, targetKind, navigationPreview = null) {
      this.store.setSelection(address, { ownedByFeature: true });
      const metadata = {
        source: "books",
        targetKind,
      };
      if (navigationPreview) metadata.navigationPreview = navigationPreview;
      this.onNavigate(address, freezeDeep(metadata));
    }

    destroy() {
      if (typeof this.unsubscribe === "function") this.unsubscribe();
      this.unsubscribe = null;
      this.resetThumbnailHydration();
      for (const remove of this.listeners.splice(0)) remove();
      for (const remove of this.rowListeners.splice(0)) remove();
      this.mounted = false;
    }
  }

  return {
    BOOKS_PANEL_VIEWS,
    CATEGORY_PRESENTATION,
    CORRECTIONS_CAPTURE_MARKS_SCHEMA,
    CORRECTIONS_INDEX_CHANGE_SCHEMA,
    CORRECTIONS_INDEX_DETAIL_SCHEMA,
    CORRECTIONS_INDEX_SCHEMA,
    CORRECTIONS_INDEX_SUMMARY_SCHEMA,
    CORRECTIONS_REVIEW_RESULT_SCHEMA,
    CORRECTIONS_REVIEW_SCHEMA,
    BooksPanelController,
    CorrectionsContractError,
    CorrectionsIndexStore,
    CorrectionsReviewConflictError,
    addressEqual,
    attentionBooks,
    bookAddress,
    bookNeedsAttention,
    booksForView,
    captureAddress,
    captureBooks,
    captureCommandTarget,
    captureState,
    compareBooks,
    compareCaptureBooks,
    detailCaptures,
    markImportedAt,
    normalizeAttentionEntry,
    normalizeCaptureMarks,
    normalizeCorrectionsIndex,
    normalizeCorrectionsIndexDetail,
    normalizeCorrectionsIndexSummary,
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

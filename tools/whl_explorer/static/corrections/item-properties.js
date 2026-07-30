(function installCorrectionsItemProperties(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else Object.assign(root.LibraryToolCorrections ||= {}, api);
})(typeof globalThis !== "undefined" ? globalThis : this,
  function correctionsItemPropertiesFactory() {
    "use strict";

    const CORRECTIONS_ITEM_SCHEMA = "librarytool.corrections-item/1";
    const CORRECTIONS_ITEM_MUTATION_SCHEMA =
      "librarytool.corrections-item-mutation/1";
    const ITEM_ID_RE = /^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,255}$/;
    const OPERATION_ID_RE = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
    const MAX_METADATA_TEXT = 1024 * 1024;
    const MAX_MUTATION_BYTES = 64 * 1024;
    const SERVER_MANAGED_METADATA = new Set([
      "id",
      "item_id",
      "kind",
      "title",
      "created_at",
      "updated_at",
      "revision",
      "record_revision",
      "representations",
      "artifacts",
      "relevance",
      "capture_id",
      "capture_book_id",
      "capture_transport",
      "origin",
      "association_state",
      "active_storage_kind",
      "canonical_item_id",
      "canonical_book_id",
      "image_count",
      "attention",
      "published_slug",
      "ocr_active",
      "ocr_verified",
      "ocr_quality",
      "title_pages",
      "thumbnail_source",
      "status",
      "representation_manifest",
      "pdf_file",
      "pdf_sources",
      "images",
      "extra",
      "storage_id",
      "active_storage_id",
      "manual_entry_id",
      "build_id",
      "entry_directory",
      "local_pdf",
      "server_path",
      "local_path",
    ]);
    const UNSAFE_KEYS = new Set(["__proto__", "constructor", "prototype"]);

    function isPlainObject(value) {
      if (!value || typeof value !== "object" || Array.isArray(value)) return false;
      const prototype = Object.getPrototypeOf(value);
      return prototype === Object.prototype || prototype === null;
    }

    function clearNode(node) {
      if (typeof node.replaceChildren === "function") node.replaceChildren();
      else while (node.firstChild) node.removeChild(node.firstChild);
    }

    function element(documentRef, name, className, value) {
      const node = documentRef.createElement(name);
      if (className) node.className = className;
      if (value != null) node.textContent = String(value);
      return node;
    }

    function setAttribute(node, name, value) {
      if (node && typeof node.setAttribute === "function") {
        node.setAttribute(name, String(value));
      }
    }

    function portableItemId(value, field = "item id") {
      if (typeof value !== "string" || !ITEM_ID_RE.test(value)) {
        throw new TypeError(`${field} must be a portable identifier`);
      }
      return value;
    }

    function recordRevision(value) {
      if (typeof value !== "string" || !value || value.length > 512 ||
          /[\u0000-\u0020"\\\u007f]/.test(value)) {
        throw new TypeError("item revision is invalid");
      }
      return value;
    }

    function portableOperationId(value) {
      if (typeof value !== "string" || !OPERATION_ID_RE.test(value)) {
        throw new TypeError("operation id must be portable");
      }
      return value;
    }

    function quoteRecordRevision(value) {
      return `"${recordRevision(value)}"`;
    }

    function cloneJson(value) {
      return JSON.parse(JSON.stringify(value));
    }

    function utf8ByteLength(value) {
      if (typeof TextEncoder === "function") {
        return new TextEncoder().encode(value).length;
      }
      return unescape(encodeURIComponent(value)).length;
    }

    function isServerManagedMetadataKey(key) {
      if (typeof key !== "string" || !key || key.length > 128 ||
          key.startsWith("_") || UNSAFE_KEYS.has(key)) return true;
      const canonical = key.replace(/([a-z0-9])([A-Z])/g, "$1_$2").toLowerCase();
      return SERVER_MANAGED_METADATA.has(canonical);
    }

    function editableMetadata(value) {
      const source = isPlainObject(value) ? value : {};
      const entries = [];
      for (const key of Object.keys(source).sort()) {
        if (!isServerManagedMetadataKey(key)) entries.push([key, cloneJson(source[key])]);
      }
      return Object.fromEntries(entries);
    }

    function prettyMetadata(value) {
      const editable = editableMetadata(value);
      const pretty = JSON.stringify(editable, null, 2);
      return pretty.length <= MAX_METADATA_TEXT
        ? pretty : JSON.stringify(editable);
    }

    function decodeCorrectionsItem(value, expectedId = null) {
      if (!isPlainObject(value)) throw new TypeError("item must be an object");
      const id = portableItemId(value.id);
      if (expectedId != null && id !== expectedId) {
        throw new TypeError("item response identity does not match the request");
      }
      if (!["book", "capture"].includes(value.kind)) {
        throw new TypeError("item kind must be book or capture");
      }
      if (typeof value.title !== "string" || value.title.length > 4096) {
        throw new TypeError("item title is invalid");
      }
      if (!isPlainObject(value.metadata)) {
        throw new TypeError("item metadata must be an object");
      }
      const revision = recordRevision(
        value.record_revision === undefined ? value.revision : value.record_revision);
      return Object.freeze({
        id,
        kind: value.kind,
        title: value.title,
        metadata: Object.freeze(editableMetadata(value.metadata)),
        revision,
      });
    }

    function responseError(response, body) {
      const payload = isPlainObject(body) && isPlainObject(body.error)
        ? body.error : isPlainObject(body) ? body : {};
      const message = typeof payload.message === "string" && payload.message
        ? payload.message
        : isPlainObject(body) && typeof body.error === "string" && body.error
          ? body.error
        : `Corrections item request failed (${Number(response && response.status) || 0})`;
      const error = new Error(message);
      error.name = "CorrectionsItemApiError";
      error.status = Number(response && response.status) || 0;
      error.code = typeof payload.code === "string" ? payload.code : "request_failed";
      error.details = isPlainObject(payload.details) ? cloneJson(payload.details) : {};
      return error;
    }

    async function readJsonResponse(response) {
      if (!response || typeof response.json !== "function") {
        throw new TypeError("Corrections item response is unavailable");
      }
      let body;
      try {
        body = await response.json();
      } catch (cause) {
        const error = new Error("Corrections item response was not valid JSON");
        error.name = "CorrectionsItemApiError";
        error.status = Number(response.status) || 0;
        error.code = "invalid_response";
        throw error;
      }
      if (!response.ok) throw responseError(response, body);
      return body;
    }

    function decodeEnvelope(body, schema, expectedId, mutation = false) {
      if (!isPlainObject(body) || body.ok !== true || body.schema !== schema ||
          !Object.prototype.hasOwnProperty.call(body, "item")) {
        throw new TypeError("Corrections item response does not match its schema");
      }
      const item = decodeCorrectionsItem(body.item, expectedId);
      if (mutation && typeof body.replayed !== "boolean") {
        throw new TypeError("Corrections item mutation replay state is invalid");
      }
      return mutation
        ? Object.freeze({ item, replayed: body.replayed })
        : item;
    }

    let operationCounter = 0;
    function defaultItemOperationId() {
      const cryptoRef = typeof globalThis !== "undefined" && globalThis.crypto;
      if (cryptoRef && typeof cryptoRef.randomUUID === "function") {
        return `corrections-item-${cryptoRef.randomUUID()}`;
      }
      operationCounter += 1;
      return `corrections-item-${Date.now().toString(36)}-${Math.random()
        .toString(36).slice(2)}-${operationCounter.toString(36)}`;
    }

    class CorrectionsItemApi {
      constructor(options = {}) {
        if (typeof options.fetchImpl !== "function") {
          throw new TypeError("Corrections item fetch implementation is required");
        }
        this.fetchImpl = options.fetchImpl;
        this.basePath = String(options.basePath || "/api/v1/corrections/items")
          .replace(/\/+$/, "");
      }

      url(itemId) {
        return `${this.basePath}/${encodeURIComponent(portableItemId(itemId))}`;
      }

      async loadItem({ itemId, signal } = {}) {
        const id = portableItemId(itemId);
        const response = await this.fetchImpl(this.url(id), {
          method: "GET",
          headers: { Accept: "application/json" },
          credentials: "same-origin",
          cache: "no-store",
          signal,
        });
        const body = await readJsonResponse(response);
        return decodeEnvelope(body, CORRECTIONS_ITEM_SCHEMA, id);
      }

      async updateItem({
        itemId,
        expectedRevision,
        patch,
        operationId,
        signal,
      } = {}) {
        const id = portableItemId(itemId);
        const revision = recordRevision(expectedRevision);
        const idempotencyKey = portableOperationId(operationId);
        if (!isPlainObject(patch) ||
            !Object.prototype.hasOwnProperty.call(patch, "title") ||
            !isPlainObject(patch.metadata_set) ||
            !Array.isArray(patch.metadata_remove) ||
            Object.keys(patch).sort().join(",") !==
              "metadata_remove,metadata_set,title") {
          throw new TypeError("item metadata patch is invalid");
        }
        const response = await this.fetchImpl(this.url(id), {
          method: "PATCH",
          headers: {
            Accept: "application/json",
            "Content-Type": "application/json",
            "If-Record-Match": quoteRecordRevision(revision),
            "Idempotency-Key": idempotencyKey,
          },
          credentials: "same-origin",
          cache: "no-store",
          body: (() => {
            const encoded = JSON.stringify({ patch });
            if (utf8ByteLength(encoded) > MAX_MUTATION_BYTES) {
              throw new TypeError(
                "The metadata changes exceed the 64 KiB request limit");
            }
            return encoded;
          })(),
          signal,
        });
        const body = await readJsonResponse(response);
        return decodeEnvelope(
          body, CORRECTIONS_ITEM_MUTATION_SCHEMA, id, true);
      }
    }

    function createCorrectionsItemApi(options = {}) {
      let fetchImpl = options.fetchImpl;
      if (typeof fetchImpl !== "function" && typeof globalThis !== "undefined" &&
          typeof globalThis.fetch === "function") {
        fetchImpl = globalThis.fetch.bind(globalThis);
      }
      return new CorrectionsItemApi({ ...options, fetchImpl });
    }

    function draftStore(value) {
      if (value && typeof value.getDraft === "function" &&
          typeof value.setDraft === "function" &&
          typeof value.clearDraft === "function") return value;
      const drafts = value instanceof Map ? value : new Map();
      return {
        getDraft: (key) => drafts.get(key),
        setDraft: (key, draft) => drafts.set(key, draft),
        clearDraft: (key) => drafts.delete(key),
      };
    }

    function itemDraftKey(itemId) {
      return itemId ? `item-metadata:${itemId}` : "";
    }

    function metadataValueEqual(left, right) {
      return JSON.stringify(left) === JSON.stringify(right);
    }

    function parseDraftMetadata(text) {
      if (typeof text !== "string" || text.length > MAX_METADATA_TEXT) {
        throw new TypeError("Metadata must be at most 1 MiB");
      }
      let parsed;
      try {
        parsed = JSON.parse(text);
      } catch (cause) {
        throw new TypeError("Metadata must be a valid JSON object");
      }
      if (!isPlainObject(parsed)) {
        throw new TypeError("Metadata must be a JSON object");
      }
      const managed = Object.keys(parsed).filter(isServerManagedMetadataKey);
      if (managed.length) {
        throw new TypeError(
          `Server-managed metadata cannot be edited: ${managed.sort().join(", ")}`);
      }
      return cloneJson(parsed);
    }

    function cleanItemDraft(item) {
      const metadata = editableMetadata(item.metadata);
      return {
        title: item.title,
        metadataText: prettyMetadata(metadata),
        dirty: false,
        baseRevision: item.revision,
        baseTitle: item.title,
        baseMetadata: metadata,
        rebaseConflicts: [],
      };
    }

    function normalizedItemDraft(item, saved) {
      if (!saved || saved.dirty === false) return cleanItemDraft(item);
      const supplied = Object.prototype.hasOwnProperty.call(saved, "title") ||
        Object.prototype.hasOwnProperty.call(saved, "metadataText");
      if (saved.dirty !== true && !supplied) return cleanItemDraft(item);
      const hasBase = typeof saved.baseRevision === "string" &&
        typeof saved.baseTitle === "string" && isPlainObject(saved.baseMetadata);
      return {
        title: String(saved.title == null ? "" : saved.title),
        metadataText: String(saved.metadataText == null
          ? prettyMetadata(item.metadata) : saved.metadataText),
        dirty: true,
        baseRevision: hasBase ? recordRevision(saved.baseRevision) : item.revision,
        baseTitle: hasBase ? saved.baseTitle : item.title,
        baseMetadata: hasBase
          ? editableMetadata(saved.baseMetadata) : editableMetadata(item.metadata),
        rebaseConflicts: Array.isArray(saved.rebaseConflicts)
          ? saved.rebaseConflicts.filter((value) => typeof value === "string")
          : [],
      };
    }

    function draftIntent(item, draft) {
      const normalized = normalizedItemDraft(item, draft);
      const desired = parseDraftMetadata(normalized.metadataText);
      const base = editableMetadata(normalized.baseMetadata);
      const metadataSet = {};
      const metadataRemove = [];
      for (const key of Object.keys(desired).sort()) {
        if (!Object.prototype.hasOwnProperty.call(base, key) ||
            !metadataValueEqual(base[key], desired[key])) {
          metadataSet[key] = desired[key];
        }
      }
      for (const key of Object.keys(base).sort()) {
        if (!Object.prototype.hasOwnProperty.call(desired, key)) {
          metadataRemove.push(key);
        }
      }
      return {
        draft: normalized,
        title: normalized.title,
        titleChanged: normalized.title !== normalized.baseTitle,
        metadataSet,
        metadataRemove,
      };
    }

    function rebaseItemDraft(item, draft) {
      const intent = draftIntent(item, draft);
      if (intent.draft.baseRevision === item.revision) {
        return Object.freeze({
          draft: intent.draft,
          rebased: false,
          conflicts: Object.freeze([]),
        });
      }
      const latest = editableMetadata(item.metadata);
      const base = editableMetadata(intent.draft.baseMetadata);
      const merged = cloneJson(latest);
      const conflicts = [];
      if (intent.titleChanged && item.title !== intent.draft.baseTitle &&
          item.title !== intent.title) {
        conflicts.push("title");
      }
      for (const [key, desired] of Object.entries(intent.metadataSet)) {
        const baseHas = Object.prototype.hasOwnProperty.call(base, key);
        const latestHas = Object.prototype.hasOwnProperty.call(latest, key);
        const latestChanged = baseHas !== latestHas ||
          baseHas && !metadataValueEqual(base[key], latest[key]);
        if (latestChanged &&
            (!latestHas || !metadataValueEqual(latest[key], desired))) {
          conflicts.push(key);
        }
        merged[key] = desired;
      }
      for (const key of intent.metadataRemove) {
        const latestHas = Object.prototype.hasOwnProperty.call(latest, key);
        const latestChanged = !latestHas ||
          !metadataValueEqual(base[key], latest[key]);
        if (latestChanged && latestHas) conflicts.push(key);
        delete merged[key];
      }
      const mergedTitle = intent.titleChanged ? intent.title : item.title;
      const changed = mergedTitle !== item.title ||
        !metadataValueEqual(latest, merged);
      const rebased = {
        title: mergedTitle,
        metadataText: prettyMetadata(merged),
        dirty: changed,
        baseRevision: item.revision,
        baseTitle: item.title,
        baseMetadata: latest,
        rebaseConflicts: [...new Set(conflicts)].sort(),
      };
      return Object.freeze({
        draft: rebased,
        rebased: true,
        conflicts: Object.freeze(rebased.rebaseConflicts),
      });
    }

    function patchFromDraft(item, draft) {
      if (!item || !draft) throw new TypeError("item and draft are required");
      const intent = draftIntent(item, draft);
      if (intent.draft.baseRevision !== item.revision) {
        const error = new Error(
          "This metadata draft is based on an older item revision");
        error.code = "stale_item_metadata_draft";
        throw error;
      }
      const title = intent.title;
      if (title.length > 4096 || title !== title.trim()) {
        throw new TypeError("Title must not have leading or trailing whitespace");
      }
      return Object.freeze({
        title: intent.titleChanged ? title : null,
        metadata_set: Object.freeze(intent.metadataSet),
        metadata_remove: Object.freeze(intent.metadataRemove),
      });
    }

    function patchIsEmpty(patch) {
      return patch.title === null && Object.keys(patch.metadata_set).length === 0 &&
        patch.metadata_remove.length === 0;
    }

    function isConflict(error) {
      return !!error &&
        ["item_revision_conflict", "record_revision_conflict",
          "correction_item_revision_conflict"].includes(error.code);
    }

    class ItemMetadataEditor {
      constructor(options = {}) {
        if (!options.root || typeof options.root.replaceChildren !== "function") {
          throw new TypeError("Item metadata editor root is required");
        }
        if (!options.api || typeof options.api.loadItem !== "function" ||
            typeof options.api.updateItem !== "function") {
          throw new TypeError("Item metadata API is required");
        }
        this.root = options.root;
        this.documentRef = options.documentRef || this.root.ownerDocument;
        this.api = options.api;
        this.drafts = draftStore(options.draftStore);
        this.operationIdFactory = typeof options.operationIdFactory === "function"
          ? options.operationIdFactory : defaultItemOperationId;
        this.onChanged = typeof options.onChanged === "function"
          ? options.onChanged : () => {};
        this.onStatus = typeof options.onStatus === "function"
          ? options.onStatus : () => {};
        this.selectedId = null;
        this.item = null;
        this.draft = null;
        this.loading = false;
        this.busy = false;
        this.message = "";
        this.messageError = false;
        this.replayed = false;
        this.draftNotice = null;
        this.generation = 0;
        this.abortController = null;
        this.destroyed = false;
      }

      mount() {
        this.render();
        return this;
      }

      abortWork() {
        this.generation += 1;
        if (this.abortController) this.abortController.abort();
        this.abortController = typeof AbortController === "function"
          ? new AbortController() : null;
        return {
          generation: this.generation,
          signal: this.abortController && this.abortController.signal,
        };
      }

      draftFor(item, preserved = null) {
        const saved = preserved || this.drafts.getDraft(itemDraftKey(item.id));
        const normalized = normalizedItemDraft(item, saved);
        this.draftNotice = null;
        if (!normalized.dirty || normalized.baseRevision === item.revision) {
          return normalized;
        }
        try {
          const result = rebaseItemDraft(item, normalized);
          this.draftNotice = {
            rebased: true,
            conflicts: [...result.conflicts],
            error: null,
          };
          return result.draft;
        } catch (error) {
          this.draftNotice = {
            rebased: false,
            conflicts: [],
            error,
          };
          return normalized;
        }
      }

      rememberDraft() {
        if (!this.selectedId || !this.draft) return;
        this.drafts.setDraft(itemDraftKey(this.selectedId), { ...this.draft });
      }

      updateDraft(value) {
        if (!this.draft || this.busy) return null;
        this.draft = { ...this.draft, ...value, dirty: true };
        this.rememberDraft();
        this.message = "";
        this.messageError = false;
        this.replayed = false;
        this.draftNotice = null;
        this.updateRenderedDraftState();
        return this.draft;
      }

      updateRenderedDraftState() {
        if (!this.root || typeof this.root.querySelector !== "function") return;
        const save = this.root.querySelector("[data-item-metadata-save]");
        const reset = this.root.querySelector("[data-item-metadata-reset]");
        const message = this.root.querySelector(".item-metadata-message");
        if (save) save.disabled = this.busy || !this.draft || !this.draft.dirty;
        if (reset) reset.disabled = this.busy || !this.draft || !this.draft.dirty;
        if (message) message.hidden = true;
        if (this.root.dataset) {
          this.root.dataset.state = this.busy
            ? "saving" : this.draft && this.draft.dirty ? "dirty" : "ready";
        }
      }

      async setSelection(itemId) {
        const next = itemId == null || itemId === ""
          ? null : portableItemId(itemId);
        if (next === this.selectedId && (this.item || this.loading)) return this.item;
        this.selectedId = next;
        this.item = null;
        this.draft = null;
        this.loading = false;
        this.busy = false;
        this.message = "";
        this.messageError = false;
        this.replayed = false;
        this.draftNotice = null;
        if (!next) {
          this.abortWork();
          this.render();
          return null;
        }
        return this.load();
      }

      async load(options = {}) {
        if (!this.selectedId || this.destroyed) return null;
        const selectedId = this.selectedId;
        const preserved = options.preservedDraft || null;
        const work = this.abortWork();
        this.loading = true;
        this.busy = false;
        this.message = options.message || "";
        this.messageError = options.messageError === true;
        this.render();
        try {
          const item = await this.api.loadItem({
            itemId: selectedId,
            signal: work.signal,
          });
          if (this.destroyed || work.generation !== this.generation ||
              selectedId !== this.selectedId) return null;
          this.item = decodeCorrectionsItem(item, selectedId);
          this.draft = this.draftFor(this.item, preserved);
          if (this.draft.dirty) this.rememberDraft();
          else this.drafts.clearDraft(itemDraftKey(this.item.id));
          this.loading = false;
          if (this.draftNotice && this.draftNotice.error) {
            this.message = "A newer item revision was loaded, but this draft could not " +
              "be merged until its metadata is valid, editable JSON.";
            this.messageError = true;
            this.onStatus(this.message, true, this.draftNotice.error);
          } else if (this.draftNotice && this.draftNotice.rebased) {
            const conflicts = this.draftNotice.conflicts;
            this.message = !this.draft.dirty
              ? "The latest revision already contains your draft changes; no save is needed."
              : conflicts.length
              ? `A newer revision was loaded. Your draft was merged; review concurrent ` +
                `changes to: ${conflicts.join(", ")}.`
              : "A newer revision was loaded. Your draft changes were merged without " +
                "discarding concurrent metadata.";
            this.messageError = conflicts.length > 0;
            this.onStatus(this.message, this.messageError);
          }
          this.render();
          return this.item;
        } catch (error) {
          if (this.destroyed || work.generation !== this.generation ||
              selectedId !== this.selectedId ||
              error && error.name === "AbortError") return null;
          this.loading = false;
          this.message = error && error.message || "Item metadata could not be loaded";
          this.messageError = true;
          this.onStatus(this.message, true, error);
          this.render();
          return null;
        }
      }

      resetDraft() {
        if (!this.item || this.busy) return;
        this.drafts.clearDraft(itemDraftKey(this.item.id));
        this.draft = this.draftFor(this.item);
        this.message = "Unsaved metadata edits reset";
        this.messageError = false;
        this.replayed = false;
        this.render();
      }

      async reloadConflict(attempted, work, error) {
        const selectedId = this.selectedId;
        try {
          const latest = await this.api.loadItem({
            itemId: selectedId,
            signal: work.signal,
          });
          if (this.destroyed || work.generation !== this.generation ||
              selectedId !== this.selectedId) return;
          this.item = decodeCorrectionsItem(latest, selectedId);
          this.draft = this.draftFor(this.item, attempted);
          this.rememberDraft();
          const conflicts = this.draftNotice && this.draftNotice.conflicts || [];
          if (this.draftNotice && this.draftNotice.error) {
            this.message = "This item changed elsewhere. Your edits were kept, but they " +
              "cannot be merged until the metadata is valid, editable JSON.";
          } else if (!this.draft.dirty) {
            this.message = "This item changed elsewhere, but the latest revision already " +
              "contains your draft changes; no save is needed.";
          } else {
            this.message = conflicts.length
              ? `This item changed elsewhere. Your edits were merged with the latest ` +
                `revision; review concurrent changes to: ${conflicts.join(", ")}.`
              : "This item changed elsewhere. Your edits were merged with the latest " +
                "revision without discarding concurrent metadata; review and save again.";
          }
        } catch (reloadError) {
          if (this.destroyed || work.generation !== this.generation ||
              selectedId !== this.selectedId ||
              reloadError && reloadError.name === "AbortError") return;
          this.message = "This item changed elsewhere. Your edits were kept, but the " +
            "latest metadata could not be loaded.";
        }
        this.busy = false;
        this.loading = false;
        this.messageError = true;
        this.replayed = false;
        this.onStatus(this.message, true, error);
        this.render();
      }

      async save() {
        if (!this.item || !this.draft || this.busy || this.destroyed) return null;
        if (this.draft.baseRevision !== this.item.revision) {
          try {
            const result = rebaseItemDraft(this.item, this.draft);
            this.draft = result.draft;
            if (this.draft.dirty) this.rememberDraft();
            else this.drafts.clearDraft(itemDraftKey(this.item.id));
            this.message = result.conflicts.length
              ? `Your draft was merged with the latest revision. Review concurrent ` +
                `changes to: ${result.conflicts.join(", ")}, then save again.`
              : "Your draft was merged with the latest revision. Review it, then save again.";
            this.messageError = result.conflicts.length > 0;
            this.replayed = false;
            this.onStatus(this.message, this.messageError);
          } catch (error) {
            this.message = "This draft is based on an older revision and cannot be " +
              "merged until its metadata is valid, editable JSON.";
            this.messageError = true;
            this.replayed = false;
            this.onStatus(this.message, true, error);
          }
          this.render();
          return null;
        }
        let patch;
        try {
          patch = patchFromDraft(this.item, this.draft);
        } catch (error) {
          this.message = error.message;
          this.messageError = true;
          this.replayed = false;
          this.onStatus(this.message, true, error);
          this.render();
          return null;
        }
        if (patchIsEmpty(patch)) {
          this.drafts.clearDraft(itemDraftKey(this.item.id));
          this.draft = this.draftFor(this.item);
          this.message = "No metadata changes to save";
          this.messageError = false;
          this.replayed = false;
          this.render();
          return null;
        }
        const attempted = { ...this.draft, dirty: true };
        const work = this.abortWork();
        const selectedId = this.item.id;
        const expectedRevision = this.item.revision;
        this.busy = true;
        this.loading = false;
        this.message = "Saving metadata\u2026";
        this.messageError = false;
        this.replayed = false;
        this.render();
        try {
          const result = await this.api.updateItem({
            itemId: selectedId,
            expectedRevision,
            patch,
            operationId: this.operationIdFactory("item.metadata.update", this.item),
            signal: work.signal,
          });
          if (this.destroyed || work.generation !== this.generation ||
              selectedId !== this.selectedId) return null;
          this.item = decodeCorrectionsItem(result.item, selectedId);
          this.drafts.clearDraft(itemDraftKey(selectedId));
          this.draft = this.draftFor(this.item);
          this.busy = false;
          this.messageError = false;
          this.replayed = result.replayed === true;
          this.message = this.replayed
            ? "Metadata save replayed; the committed result is current."
            : "Metadata saved";
          this.onChanged(this.item, Object.freeze({ replayed: this.replayed }));
          this.onStatus(this.message, false, result);
          this.render();
          return result;
        } catch (error) {
          if (this.destroyed || work.generation !== this.generation ||
              selectedId !== this.selectedId ||
              error && error.name === "AbortError") return null;
          if (isConflict(error)) {
            await this.reloadConflict(attempted, work, error);
            return null;
          }
          this.busy = false;
          this.draft = attempted;
          this.rememberDraft();
          this.message = error && error.message || "Metadata could not be saved";
          this.messageError = true;
          this.replayed = false;
          this.onStatus(this.message, true, error);
          this.render();
          return null;
        }
      }

      identityHeader() {
        const wrapper = element(this.documentRef, "div", "item-metadata-identity");
        const label = this.item
          ? this.item.kind === "capture" ? "Captured entry" : "Book record"
          : "Selected item";
        const badge = element(this.documentRef, "span",
          `item-kind-badge item-kind-${this.item ? this.item.kind : "unknown"}`, label);
        const id = element(this.documentRef, "code", "canonical-item-id",
          this.selectedId || "");
        wrapper.append(badge, id);
        if (this.item) {
          const revision = element(this.documentRef, "span",
            "item-record-revision", `Revision ${this.item.revision}`);
          wrapper.append(revision);
        }
        return wrapper;
      }

      renderMessage(container) {
        if (!this.message) return;
        const message = element(this.documentRef, "p",
          this.messageError
            ? "item-metadata-message property-error"
            : `item-metadata-message${this.replayed ? " is-replayed" : ""}`,
          this.message);
        setAttribute(message, "role", this.messageError ? "alert" : "status");
        container.append(message);
      }

      render() {
        if (this.destroyed || !this.documentRef) return;
        clearNode(this.root);
        if (this.root.dataset) {
          this.root.dataset.state = this.loading
            ? "loading" : this.busy ? "saving" : this.messageError
              ? "error" : this.replayed ? "replayed"
                : this.draft && this.draft.dirty ? "dirty" : "ready";
        }
        if (this.loading || this.busy) setAttribute(this.root, "aria-busy", "true");
        else if (typeof this.root.removeAttribute === "function") {
          this.root.removeAttribute("aria-busy");
        }
        const card = element(this.documentRef, "section",
          "item-metadata-card property-card");
        const heading = element(this.documentRef, "h3",
          "property-card-title", "Item metadata");
        card.append(heading);
        const body = element(this.documentRef, "div",
          "property-card-body item-metadata-body");
        if (!this.selectedId) {
          body.append(element(this.documentRef, "p", "empty-row",
            "Select a book or captured entry to edit its metadata."));
          card.append(body);
          this.root.append(card);
          return;
        }
        body.append(this.identityHeader());
        if (this.loading && !this.item) {
          const loading = element(this.documentRef, "p", "property-loading",
            "Loading item metadata\u2026");
          setAttribute(loading, "role", "status");
          setAttribute(loading, "aria-busy", "true");
          body.append(loading);
          this.renderMessage(body);
          card.append(body);
          this.root.append(card);
          return;
        }
        if (!this.item || !this.draft) {
          this.renderMessage(body);
          const retry = element(this.documentRef, "button",
            "item-metadata-retry", "Retry");
          retry.type = "button";
          retry.addEventListener("click", () => void this.load());
          body.append(retry);
          card.append(body);
          this.root.append(card);
          return;
        }

        const form = element(this.documentRef, "form", "item-metadata-form");
        const titleId = `corrections-item-title-${this.item.id}`;
        const titleLabel = element(this.documentRef, "label", "", "Title");
        titleLabel.htmlFor = titleId;
        const title = element(this.documentRef, "input");
        title.id = titleId;
        title.type = "text";
        title.maxLength = 4096;
        title.value = this.draft.title;
        title.disabled = this.busy;
        title.addEventListener("input", () => this.updateDraft({ title: title.value }));

        const metadataId = `corrections-item-metadata-${this.item.id}`;
        const metadataLabel = element(this.documentRef, "label", "", "Metadata (JSON)");
        metadataLabel.htmlFor = metadataId;
        const metadata = element(this.documentRef, "textarea");
        metadata.id = metadataId;
        metadata.rows = 12;
        metadata.maxLength = MAX_METADATA_TEXT;
        metadata.value = this.draft.metadataText;
        metadata.disabled = this.busy;
        metadata.addEventListener("input", () =>
          this.updateDraft({ metadataText: metadata.value }));
        const note = element(this.documentRef, "p", "item-metadata-note",
          "Only editable catalogue metadata is shown. Server-managed identifiers, " +
          "file locations, and processing state are omitted.");

        const actions = element(this.documentRef, "div",
          "property-actions item-metadata-actions");
        const save = element(this.documentRef, "button", "", "Save metadata");
        save.type = "submit";
        save.dataset && (save.dataset.itemMetadataSave = "");
        setAttribute(save, "data-item-metadata-save", "");
        save.disabled = this.busy || !this.draft.dirty;
        const reset = element(this.documentRef, "button", "", "Reset edits");
        reset.type = "button";
        reset.dataset && (reset.dataset.itemMetadataReset = "");
        setAttribute(reset, "data-item-metadata-reset", "");
        reset.disabled = this.busy || !this.draft.dirty;
        reset.addEventListener("click", () => this.resetDraft());
        actions.append(save, reset);
        form.addEventListener("submit", (event) => {
          event.preventDefault();
          this.updateDraft({ title: title.value, metadataText: metadata.value });
          void this.save();
        });
        form.append(titleLabel, title, metadataLabel, metadata, note, actions);
        body.append(form);
        this.renderMessage(body);
        card.append(body);
        this.root.append(card);
      }

      destroy() {
        this.destroyed = true;
        this.abortWork();
        clearNode(this.root);
      }
    }

    function createItemMetadataEditor(options) {
      return new ItemMetadataEditor(options);
    }

    return {
      CORRECTIONS_ITEM_MUTATION_SCHEMA,
      CORRECTIONS_ITEM_SCHEMA,
      CorrectionsItemApi,
      ItemMetadataEditor,
      MAX_METADATA_TEXT,
      createCorrectionsItemApi,
      createItemMetadataEditor,
      decodeCorrectionsItem,
      defaultItemOperationId,
      editableMetadata,
      isConflict,
      isServerManagedMetadataKey,
      itemDraftKey,
      parseDraftMetadata,
      patchFromDraft,
      patchIsEmpty,
      quoteRecordRevision,
    };
  });

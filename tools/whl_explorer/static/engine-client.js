/*
 * Browser transport boundary for the Library Tool engine.
 *
 * Workbenches call semantic methods on EngineClient; only this file knows the
 * current Flask route layout, JSON envelope, conditional-write headers, or
 * multipart encoding.  The client is intentionally stateless.  Draft state,
 * caching, request generations, and user-facing error messages belong to the
 * workbench controller.
 */
(function installEngineClient(root, factory) {
  const api = factory();

  if (typeof module === "object" && module.exports) module.exports = api;

  // A classic script is used for now because app.js still owns globals.  Keep
  // the constructor available for injected test/alternate transports and one
  // shared browser instance for the current UI.
  if (root && root.window === root) {
    root.EngineClient = api.EngineClient;
    root.EngineClientError = api.EngineClientError;
    if (!root.engineClient && typeof root.fetch === "function") {
      root.engineClient = new api.EngineClient({
        transport: root.fetch.bind(root),
        formDataFactory: () => new root.FormData(),
      });
    }
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function engineClientFactory() {
  "use strict";

  function encodePart(value) {
    // encodeURIComponent deliberately leaves !'()* alone.  Encoding them too
    // keeps path components and query values unambiguous across transports.
    return encodeURIComponent(String(value)).replace(/[!'()*]/g, (char) =>
      `%${char.charCodeAt(0).toString(16).toUpperCase()}`);
  }

  function quoteRevision(value, name) {
    const revision = String(value || "");
    if (!revision) throw new TypeError(`${name} is required`);
    if (/[\u0000-\u001f\u007f"\\]/.test(revision)) {
      throw new TypeError(`${name} is not a valid revision token`);
    }
    return `"${revision}"`;
  }

  function quoteRecordRevision(value, name) {
    const revision = String(value || "");
    if (!/^[A-Za-z0-9][A-Za-z0-9._:+-]{0,511}$/.test(revision)) {
      throw new TypeError(`${name} is not a valid record revision`);
    }
    return `"${revision}"`;
  }

  function operationKey(value, name) {
    const key = String(value || "");
    if (!/^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(key)) {
      throw new TypeError(`${name} is required and must be a portable identifier`);
    }
    return key;
  }

  const PORTABLE_IDENTIFIER = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;

  function isPortableIdentifier(value) {
    return typeof value === "string" && PORTABLE_IDENTIFIER.test(value);
  }

  function portableIdentifier(value, name) {
    if (!isPortableIdentifier(value)) {
      throw new TypeError(`${name} is required and must be a portable identifier`);
    }
    return value;
  }

  function isLifecycleRevision(value, optional = false) {
    if (typeof value !== "string") return false;
    if (!value) return optional;
    return value.length <= 512 && value === value.trim() &&
      !/[\u0000-\u0020\u007f"\\]/.test(value) &&
      !/[\ud800-\udfff]/.test(value);
  }

  function quoteLifecycleRevision(value, name) {
    if (!isLifecycleRevision(value)) {
      throw new TypeError(`${name} is not a valid strong revision token`);
    }
    return `"${value}"`;
  }

  function isObject(value) {
    return !!value && typeof value === "object" && !Array.isArray(value);
  }

  function isItemTombstone(value) {
    if (!isObject(value) ||
        !isPortableIdentifier(value.tombstone_id) ||
        !isLifecycleRevision(value.revision) ||
        !["deleted", "restored"].includes(value.state) ||
        !isPortableIdentifier(value.item_id) ||
        !isLifecycleRevision(value.deleted_item_revision) ||
        !isLifecycleRevision(value.managed_tree_revision) ||
        !isLifecycleRevision(value.restored_item_revision, true)) return false;
    if (value.state === "deleted") return value.restored_item_revision === "";
    return !!value.restored_item_revision &&
      value.restored_item_revision !== value.deleted_item_revision;
  }

  function hasUniqueTombstoneIdentities(tombstones) {
    const tombstoneIds = new Set();
    const activelyDeletedItems = new Set();
    for (const tombstone of tombstones) {
      const tombstoneId = tombstone.tombstone_id.toLowerCase();
      if (tombstoneIds.has(tombstoneId)) return false;
      tombstoneIds.add(tombstoneId);
      if (tombstone.state !== "deleted") continue;
      const itemId = tombstone.item_id.toLowerCase();
      if (activelyDeletedItems.has(itemId)) return false;
      activelyDeletedItems.add(itemId);
    }
    return true;
  }

  function isLifecycleReceipt(receipt, action, expected) {
    if (!isObject(receipt) || receipt.action !== action ||
        Object.prototype.hasOwnProperty.call(receipt, "command_sha256") ||
        !isPortableIdentifier(receipt.operation_id) ||
        receipt.operation_id !== expected.operationId ||
        !isPortableIdentifier(receipt.item_id) ||
        !isLifecycleRevision(receipt.deleted_item_revision) ||
        !isLifecycleRevision(receipt.restored_item_revision, true) ||
        !isLifecycleRevision(receipt.managed_tree_revision) ||
        !isLifecycleRevision(receipt.tombstone_before_revision, true) ||
        !isItemTombstone(receipt.tombstone) ||
        receipt.tombstone.item_id !== receipt.item_id ||
        receipt.tombstone.deleted_item_revision !== receipt.deleted_item_revision ||
        receipt.tombstone.managed_tree_revision !== receipt.managed_tree_revision) {
      return false;
    }
    if (action === "delete") {
      return receipt.item_id === expected.itemId &&
        receipt.deleted_item_revision === expected.recordRevision &&
        receipt.managed_tree_revision === expected.managedTreeRevision &&
        receipt.restored_item_revision === "" &&
        receipt.tombstone_before_revision === "" &&
        receipt.tombstone.state === "deleted";
    }
    return receipt.tombstone.tombstone_id === expected.tombstoneId &&
      receipt.tombstone_before_revision === expected.tombstoneRevision &&
      receipt.tombstone_before_revision !== receipt.tombstone.revision &&
      receipt.tombstone.state === "restored" &&
      !!receipt.restored_item_revision &&
      receipt.tombstone.restored_item_revision === receipt.restored_item_revision;
  }

  function fallbackCode(status) {
    if (status === 409) return "conflict";
    if (status === 428) return "precondition-required";
    if (status === 404) return "not-found";
    if (status === 401 || status === 403) return "forbidden";
    if (status === 429) return "rate-limited";
    if (status >= 500) return "engine-unavailable";
    return "request-failed";
  }

  class EngineClientError extends Error {
    constructor(message, options = {}) {
      super(message || "Engine request failed");
      this.name = "EngineClientError";
      this.status = Number(options.status) || 0;
      this.code = options.code || fallbackCode(this.status);
      this.details = options.details || null;
      this.conflict = options.conflict || null;
      this.retryable = options.retryable != null
        ? !!options.retryable
        : this.status === 0 || this.status === 429 || this.status >= 500;
      this.method = options.method || "";
      this.url = options.url || "";
      this.body = options.body == null ? null : options.body;
      if (options.cause !== undefined) this.cause = options.cause;
    }
  }

  class EngineClient {
    constructor(options = {}) {
      if (typeof options.transport !== "function") {
        throw new TypeError("EngineClient requires an injected transport");
      }
      this._transport = options.transport;
      this._baseUrl = String(options.baseUrl || "/api").replace(/\/+$/, "");
      this._formDataFactory = options.formDataFactory || (() => {
        if (typeof FormData !== "function") {
          throw new TypeError("No FormData implementation is available");
        }
        return new FormData();
      });

      const pageImageUrl = (args) => this._pageImageUrl(args);
      this.pdf = Object.freeze({
        info: (args) => this._pdfInfo(args),
        words: (args) => this._pdfWords(args),
        pageImageUrl,
      });
      this.translations = Object.freeze({
        list: (args) => this._translationList(args),
        get: (args) => this._translationGet(args),
        replacePage: (args) => this._translationReplacePage(args),
      });
      this.ocr = Object.freeze({
        layout: (args) => this._ocrLayout(args),
      });
      this.jobs = Object.freeze({
        list: (args) => this._jobsList(args),
        get: (args) => this._jobGet(args),
        cancel: (args) => this._jobCancel(args),
        events: (args) => this._jobEvents(args),
      });
      this.items = Object.freeze({
        list: (args) => this._itemsList(args),
        get: (args) => this._itemGet(args),
        create: (args) => this._itemCreate(args),
        update: (args) => this._itemUpdate(args),
        seedCompatibility: (args) => this._itemSeedCompatibility(args),
        lifecycle: (args) => this._itemLifecycle(args),
        delete: (args) => this._itemDelete(args),
        representations: (args) => this._itemRepresentations(args),
        attachRepresentation: (args) => this._representationAttach(args),
        replaceRepresentation: (args) => this._representationReplace(args),
        detachRepresentation: (args) => this._representationDetach(args),
        artifacts: (args) => this._itemArtifacts(args),
        readiness: (args) => this._itemReadiness(args),
      });
      this.itemTombstones = Object.freeze({
        list: (args) => this._itemTombstonesList(args),
        get: (args) => this._itemTombstoneGet(args),
        restore: (args) => this._itemTombstoneRestore(args),
      });
      this.capabilities = (args) => this._capabilities(args);

      const pages = Object.freeze({
        get: (args) => this._replicaPageGet(args),
        save: (args) => this._replicaPageSave(args),
        recompile: (args) => this._replicaPageRecompile(args),
        // Semantic alias for the scan raster used by the page editor.  The
        // preview can use pdf.pageImageUrl directly.
        imageUrl: pageImageUrl,
      });
      const proposals = Object.freeze({
        decide: (args) => this._replicaProposalDecide(args),
      });
      const detection = Object.freeze({
        start: (args) => this._replicaDetectionStart(args),
      });
      const templates = Object.freeze({
        list: (args) => this._replicaTemplatesList(args),
        saveFromPage: (args) => this._replicaTemplateSaveFromPage(args),
        apply: (args) => this._replicaTemplateApply(args),
        outliers: (args) => this._replicaTemplateOutliers(args),
      });
      const styles = Object.freeze({
        get: (args) => this._replicaStylesGet(args),
        save: (args) => this._replicaStylesSave(args),
        reset: (args) => this._replicaStylesReset(args),
      });
      const instructions = Object.freeze({
        get: (args) => this._replicaInstructionsGet(args),
        save: (args) => this._replicaInstructionsSave(args),
      });
      const figures = Object.freeze({
        rework: (args) => this._replicaFigureRework(args),
        imageUrl: (args) => this._replicaFigureImageUrl(args),
      });
      const packages = Object.freeze({
        open: (args) => this._replicaPackageOpen(args),
        import: (args) => this._replicaPackageImport(args),
        exportUrl: (args) => this._replicaPackageExportUrl(args),
      });
      this.replica = Object.freeze({
        pages, proposals, detection, templates, styles, instructions, figures,
        packages,
        printUrl: (args) => this._replicaPrintUrl(args),
      });
    }

    _query(values) {
      const pairs = [];
      for (const [key, value] of Object.entries(values || {})) {
        if (value === undefined || value === null || value === "") continue;
        pairs.push(`${encodePart(key)}=${encodePart(value)}`);
      }
      return pairs.length ? `?${pairs.join("&")}` : "";
    }

    _url(path, query) {
      return `${this._baseUrl}${path}${this._query(query)}`;
    }

    _buildPath(bookId, suffix) {
      return `/builds/${encodePart(bookId)}/${suffix}`;
    }

    async _requestJson(method, path, options = {}) {
      const url = this._url(path, options.query);
      const headers = { Accept: "application/json", ...(options.headers || {}) };
      const init = { method, headers };
      if (options.signal) init.signal = options.signal;
      if (options.cache) init.cache = options.cache;
      if (options.multipart !== undefined) {
        init.body = options.multipart;
      } else if (options.body !== undefined) {
        headers["Content-Type"] = "application/json";
        init.body = JSON.stringify(options.body);
      }

      let response;
      try {
        response = await this._transport(url, init);
      } catch (cause) {
        if (cause instanceof EngineClientError) throw cause;
        const aborted = cause && cause.name === "AbortError";
        throw new EngineClientError(aborted ? "Engine request aborted" :
          (cause && cause.message || "Unable to reach the engine"), {
          status: 0, code: aborted ? "aborted" : "network-error",
          retryable: !aborted, method, url, cause,
        });
      }

      const status = Number(response && response.status) || 0;
      const responseOk = response && typeof response.ok === "boolean"
        ? response.ok : status >= 200 && status < 300;
      let body = null;
      try {
        if (!response || typeof response.json !== "function") {
          throw new TypeError("Transport response has no json() method");
        }
        body = await response.json();
      } catch (cause) {
        if (responseOk && options.allowEmpty) return { ok: true };
        throw new EngineClientError(responseOk
          ? "Engine returned an invalid JSON response"
          : `Engine request failed (${status || "no status"})`, {
          status, code: responseOk ? "invalid-response" : fallbackCode(status),
          retryable: status === 429 || status >= 500,
          method, url, body: null, cause,
        });
      }

      if (!responseOk || !body || body.ok === false) {
        const payload = body && typeof body === "object" ? body : {};
        throw new EngineClientError(payload.error || payload.message ||
          `Engine request failed (${status || "no status"})`, {
          status,
          code: payload.code || payload.error_code || fallbackCode(status),
          details: payload.details || payload.conflict || null,
          conflict: payload.conflict || null,
          retryable: payload.retryable,
          method, url, body,
        });
      }
      return options.includeStatus ? { body, status } : body;
    }

    _pdfInfo({ path, signal } = {}) {
      return this._requestJson("GET", "/pdf/info", {
        query: { path }, signal,
      });
    }

    _capabilities({ signal } = {}) {
      return this._requestJson("GET", "/v1/capabilities", { signal });
    }

    _itemsList({ includeBuildCompatibility = false, signal } = {}) {
      return this._requestJson("GET", "/v1/items", {
        query: {
          projection: includeBuildCompatibility ? "build-workbench" : undefined,
        },
        signal,
      });
    }

    _itemGet({ itemId, includeBuildCompatibility = false, signal } = {}) {
      return this._requestJson("GET", `/v1/items/${encodePart(itemId)}`, {
        query: {
          projection: includeBuildCompatibility ? "build-workbench" : undefined,
        },
        signal,
      });
    }

    _itemCreate({ item, idempotencyKey, signal } = {}) {
      return this._requestJson("POST", "/v1/items", {
        headers: {
          "Idempotency-Key": operationKey(idempotencyKey, "idempotencyKey"),
        },
        body: { item },
        signal,
      });
    }

    // Transitional storage-only acquisition fields are not part of the
    // portable ItemDraft. Keep their legacy route and CAS spelling contained
    // here so workbench code still depends on a semantic transport method.
    _itemSeedCompatibility({ itemId, compatibility, recordRevision,
      signal } = {}) {
      const revision = String(recordRevision || "");
      if (!/^[A-Za-z0-9][A-Za-z0-9._:+-]{0,511}$/.test(revision)) {
        throw new TypeError("recordRevision is not a valid record revision");
      }
      if (!isObject(compatibility) ||
          Object.keys(compatibility).some((key) =>
            !["extra", "images", "capture_id"].includes(key)) ||
          (Object.prototype.hasOwnProperty.call(compatibility, "extra") &&
            !isObject(compatibility.extra)) ||
          (Object.prototype.hasOwnProperty.call(compatibility, "images") &&
            (!Array.isArray(compatibility.images) ||
              compatibility.images.some((value) =>
                typeof value !== "string"))) ||
          (Object.prototype.hasOwnProperty.call(compatibility, "capture_id") &&
            typeof compatibility.capture_id !== "string")) {
        throw new TypeError("compatibility must contain only acquisition fields");
      }
      return this._requestJson(
        "PATCH", `/builds/${encodePart(itemId)}`, {
          body: { ...compatibility, expect_updated_at: revision },
          signal,
        });
    }

    _itemUpdate({ itemId, bookId, patch, recordRevision, idempotencyKey,
      signal } = {}) {
      const id = itemId != null ? itemId : bookId;
      return this._requestJson("PATCH", `/v1/items/${encodePart(id)}`, {
        headers: {
          "Idempotency-Key": operationKey(idempotencyKey, "idempotencyKey"),
          "If-Record-Match": quoteRecordRevision(
            recordRevision, "recordRevision"),
        },
        body: { patch },
        signal,
      });
    }

    _invalidLifecycleResponse(message, method, path, body, query, status = 200) {
      throw new EngineClientError(message, {
        status,
        code: "invalid-response",
        retryable: true,
        method,
        url: this._url(path, query),
        body,
      });
    }

    async _itemLifecycle({ itemId, signal } = {}) {
      const id = portableIdentifier(itemId, "itemId");
      const path = `/v1/items/${encodePart(id)}/lifecycle`;
      const { body, status } = await this._requestJson("GET", path, {
        signal, cache: "no-cache", includeStatus: true,
      });
      if (status !== 200 || !isObject(body) || body.ok !== true ||
          body.schema !== "librarytool.item-lifecycle-state/1" ||
          body.state !== "live" || body.item_id !== id ||
          !isLifecycleRevision(body.item_revision) ||
          !isLifecycleRevision(body.managed_tree_revision) ||
          !isLifecycleRevision(body.revision)) {
        this._invalidLifecycleResponse(
          "Engine returned an invalid item lifecycle state",
          "GET", path, body, undefined, status);
      }
      return body;
    }

    async _itemDelete({ itemId, recordRevision, managedTreeRevision,
      idempotencyKey, signal } = {}) {
      const id = portableIdentifier(itemId, "itemId");
      const operationId = portableIdentifier(
        idempotencyKey, "idempotencyKey");
      if (typeof recordRevision !== "string") {
        throw new TypeError("recordRevision is not a valid record revision");
      }
      const path = `/v1/items/${encodePart(id)}`;
      const { body, status } = await this._requestJson("DELETE", path, {
        headers: {
          "Idempotency-Key": operationId,
          "If-Record-Match": quoteRecordRevision(
            recordRevision, "recordRevision"),
          "If-Managed-Tree-Match": quoteLifecycleRevision(
            managedTreeRevision, "managedTreeRevision"),
        },
        signal,
        cache: "no-store",
        includeStatus: true,
      });
      if (status !== 200 || !isObject(body) || body.ok !== true ||
          body.schema !== "librarytool.item-lifecycle-receipt/1" ||
          Object.prototype.hasOwnProperty.call(body, "command_sha256") ||
          typeof body.replayed !== "boolean" ||
          !isLifecycleReceipt(body.receipt, "delete", {
            operationId,
            itemId: id,
            recordRevision,
            managedTreeRevision,
          })) {
        this._invalidLifecycleResponse(
          "Engine returned an invalid item deletion receipt",
          "DELETE", path, body, undefined, status);
      }
      return body;
    }

    async _itemTombstonesList({ state, signal } = {}) {
      const wantedState = state == null || state === "" ? "" : state;
      if (typeof wantedState !== "string" ||
          (wantedState && !["deleted", "restored"].includes(wantedState))) {
        throw new TypeError("state must be deleted or restored");
      }
      const path = "/v1/item-tombstones";
      const query = { state: wantedState || undefined };
      const { body, status } = await this._requestJson("GET", path, {
        query, signal, cache: "no-cache", includeStatus: true,
      });
      if (status !== 200 || !isObject(body) || body.ok !== true ||
          body.schema !== "librarytool.item-tombstone-list/1" ||
          !Array.isArray(body.tombstones) ||
          !body.tombstones.every(isItemTombstone) ||
          (wantedState && !body.tombstones.every(
            (tombstone) => tombstone.state === wantedState)) ||
          !hasUniqueTombstoneIdentities(body.tombstones)) {
        this._invalidLifecycleResponse(
          "Engine returned an invalid item tombstone list",
          "GET", path, body, query, status);
      }
      return body;
    }

    async _itemTombstoneGet({ tombstoneId, signal } = {}) {
      const id = portableIdentifier(tombstoneId, "tombstoneId");
      const path = `/v1/item-tombstones/${encodePart(id)}`;
      const { body, status } = await this._requestJson("GET", path, {
        signal, cache: "no-cache", includeStatus: true,
      });
      if (status !== 200 || !isObject(body) || body.ok !== true ||
          body.schema !== "librarytool.item-tombstone/1" ||
          !isItemTombstone(body.tombstone) ||
          body.tombstone.tombstone_id !== id) {
        this._invalidLifecycleResponse(
          "Engine returned an invalid item tombstone",
          "GET", path, body, undefined, status);
      }
      return body;
    }

    async _itemTombstoneRestore({ tombstoneId, tombstoneRevision,
      idempotencyKey, signal } = {}) {
      const id = portableIdentifier(tombstoneId, "tombstoneId");
      const operationId = portableIdentifier(
        idempotencyKey, "idempotencyKey");
      const path = `/v1/item-tombstones/${encodePart(id)}/restore`;
      const { body, status } = await this._requestJson("POST", path, {
        headers: {
          "Idempotency-Key": operationId,
          "If-Tombstone-Match": quoteLifecycleRevision(
            tombstoneRevision, "tombstoneRevision"),
        },
        signal,
        cache: "no-store",
        includeStatus: true,
      });
      const expectedStatus = body && body.replayed === true ? 200 : 201;
      if (status !== expectedStatus || !isObject(body) || body.ok !== true ||
          body.schema !== "librarytool.item-lifecycle-receipt/1" ||
          Object.prototype.hasOwnProperty.call(body, "command_sha256") ||
          typeof body.replayed !== "boolean" ||
          !isLifecycleReceipt(body.receipt, "restore", {
            operationId,
            tombstoneId: id,
            tombstoneRevision,
          })) {
        this._invalidLifecycleResponse(
          "Engine returned an invalid item restoration receipt",
          "POST", path, body, undefined, status);
      }
      return body;
    }

    _itemRepresentations({ itemId, signal } = {}) {
      return this._requestJson(
        "GET", `/v1/items/${encodePart(itemId)}/representations`, { signal });
    }

    _representationPut({ itemId, representationId, representation,
      recordRevision, representationRevision, idempotencyKey, signal } = {}) {
      const headers = {
        "Idempotency-Key": operationKey(idempotencyKey, "idempotencyKey"),
        "If-Record-Match": quoteRecordRevision(
          recordRevision, "recordRevision"),
      };
      if (representationRevision != null) {
        headers["If-Representation-Match"] = quoteRecordRevision(
          representationRevision, "representationRevision");
      }
      return this._requestJson(
        "PUT",
        `/v1/items/${encodePart(itemId)}/representations/` +
          encodePart(representationId),
        { headers, body: { representation }, signal });
    }

    _representationAttach(args = {}) {
      if (args.representationRevision != null) {
        throw new TypeError(
          "attachRepresentation does not accept representationRevision");
      }
      return this._representationPut(args);
    }

    _representationReplace(args = {}) {
      if (!args.representationRevision) {
        throw new TypeError("representationRevision is required");
      }
      return this._representationPut(args);
    }

    _representationDetach({ itemId, representationId, recordRevision,
      representationRevision, idempotencyKey, signal } = {}) {
      return this._requestJson(
        "DELETE",
        `/v1/items/${encodePart(itemId)}/representations/` +
          encodePart(representationId),
        {
          headers: {
            "Idempotency-Key": operationKey(idempotencyKey, "idempotencyKey"),
            "If-Record-Match": quoteRecordRevision(
              recordRevision, "recordRevision"),
            "If-Representation-Match": quoteRecordRevision(
              representationRevision, "representationRevision"),
          },
          signal,
        });
    }

    _itemArtifacts({ itemId, signal } = {}) {
      return this._requestJson(
        "GET", `/v1/items/${encodePart(itemId)}/artifacts`, { signal });
    }

    _itemReadiness({ itemId, signal } = {}) {
      return this._requestJson(
        "GET", `/v1/items/${encodePart(itemId)}/readiness`, { signal });
    }

    _jobsList({ state, kind, itemId, signal } = {}) {
      return this._requestJson("GET", "/v1/jobs", {
        query: {
          state: Array.isArray(state) ? state.join(",") : state,
          kind: Array.isArray(kind) ? kind.join(",") : kind,
          item_id: itemId,
        },
        signal,
      });
    }

    _jobGet({ jobId, signal } = {}) {
      return this._requestJson(
        "GET", `/v1/jobs/${encodePart(jobId)}`, { signal });
    }

    _jobCancel({ jobId, signal } = {}) {
      return this._requestJson(
        "POST", `/v1/jobs/${encodePart(jobId)}/cancel`, { signal });
    }

    _jobEvents({ after, limit, signal } = {}) {
      return this._requestJson("GET", "/v1/job-events", {
        query: { after, limit }, signal,
      });
    }

    _pdfWords({ path, page, bookId, signal } = {}) {
      return this._requestJson("GET", "/pdf/words", {
        query: { path, page, build_id: bookId }, signal,
      });
    }

    _pageImageUrl({ path, pdfPath, page, width } = {}) {
      return this._url("/pdf/pageimg", {
        path: path != null ? path : pdfPath, page, w: width,
      });
    }

    _ocrLayout({ bookId, signal } = {}) {
      return this._requestJson(
        "GET", this._buildPath(bookId, "ocr-layout"), { signal });
    }

    _replicaTemplatesList({ bookId, sourceId, signal } = {}) {
      return this._requestJson("GET", this._buildPath(bookId, "ocr-templates"), {
        query: { src: sourceId }, signal,
      });
    }

    _replicaPageGet({ bookId, sourceId, page, signal } = {}) {
      return this._requestJson("GET", this._buildPath(bookId, "ocr-regions"), {
        query: { src: sourceId, page }, signal,
      });
    }

    _replicaPageSave({ bookId, sourceId, page, revision, record, signal } = {}) {
      const value = record || {};
      return this._requestJson("PUT", this._buildPath(bookId, "ocr-regions"), {
        headers: { "If-Match": quoteRevision(revision, "revision") },
        body: {
          src: sourceId, page,
          doc: value.doc, dims: value.dims, ext: value.ext,
          state: value.state, items: value.items,
          expect_revision: revision,
        },
        signal,
      });
    }

    _replicaProposalDecide({ bookId, sourceId, page, action, revision,
      proposalRevision, signal } = {}) {
      return this._requestJson("POST",
        this._buildPath(bookId, "ocr-region-proposals"), {
          headers: {
            "If-Match": quoteRevision(revision, "revision"),
            "If-Proposal-Match": quoteRevision(
              proposalRevision, "proposalRevision"),
          },
          body: {
            src: sourceId, page, action,
            expect_revision: revision,
            expect_proposal_revision: proposalRevision,
          },
          signal,
        });
    }

    _replicaPageRecompile({ bookId, sourceId, layer, signal } = {}) {
      const body = { src: sourceId };
      if (layer === "norm") body.layer = "norm";
      return this._requestJson("POST",
        this._buildPath(bookId, "ocr-regions/recompile"), { body, signal });
    }

    _replicaTemplateSaveFromPage({ bookId, sourceId, name, page, signal } = {}) {
      return this._requestJson("PUT", this._buildPath(bookId, "ocr-templates"), {
        body: { src: sourceId, name, from_page: page }, signal,
      });
    }

    _replicaDetectionStart({ bookId, sourceId, page, revision,
      provider = "automatic", idempotencyKey, signal } = {}) {
      return this._requestJson("POST",
        `/v1/items/${encodePart(bookId)}/replica/region-detection-jobs`, {
          headers: { "If-Match": quoteRevision(revision, "revision") },
          body: {
            source_id: sourceId, page, provider,
            expect_revision: revision,
            idempotency_key: idempotencyKey,
          },
          signal,
        });
    }

    _replicaTemplateApply({ bookId, sourceId, name, pages, signal } = {}) {
      return this._requestJson("POST",
        this._buildPath(bookId, "ocr-templates/apply"), {
          body: { src: sourceId, name, pages }, signal,
        });
    }

    _replicaTemplateOutliers({ bookId, sourceId, name, signal } = {}) {
      return this._requestJson("POST",
        this._buildPath(bookId, "ocr-templates/outliers"), {
          body: { src: sourceId, name }, signal,
        });
    }

    _replicaFigureRework({ bookId, sourceId, figure, prompt, signal } = {}) {
      return this._requestJson("POST", this._buildPath(bookId, "rework-figure"), {
        body: { src: sourceId, figure, prompt }, signal,
      });
    }

    _replicaFigureImageUrl({ bookId, name } = {}) {
      return this._url(this._buildPath(bookId,
        `ocr/images/${encodePart(name)}`));
    }

    _replicaStylesGet({ bookId, signal } = {}) {
      return this._requestJson("GET", this._buildPath(bookId, "replica-style"), {
        signal,
      });
    }

    _replicaStylesSave({ bookId, styles, signal } = {}) {
      return this._requestJson("PUT", this._buildPath(bookId, "replica-style"), {
        body: { styles }, signal,
      });
    }

    _replicaStylesReset({ bookId, signal } = {}) {
      return this._requestJson("DELETE",
        this._buildPath(bookId, "replica-style"), {
          signal, allowEmpty: true,
        });
    }

    _replicaInstructionsGet({ bookId, signal } = {}) {
      return this._requestJson("GET",
        this._buildPath(bookId, "replica-instructions"), { signal });
    }

    _replicaInstructionsSave({ bookId, text, signal } = {}) {
      return this._requestJson("PUT",
        this._buildPath(bookId, "replica-instructions"), {
          body: { text }, signal,
        });
    }

    _translationList({ itemId, bookId, signal } = {}) {
      const id = itemId != null ? itemId : bookId;
      return this._requestJson("GET",
        `/v1/items/${encodePart(id)}/translations`, { signal });
    }

    _translationGet({ itemId, bookId, translationId, signal } = {}) {
      const id = itemId != null ? itemId : bookId;
      return this._requestJson("GET",
        `/v1/items/${encodePart(id)}/translations/${encodePart(translationId)}`,
        { signal });
    }

    _translationReplacePage({ itemId, bookId, translationId, selector, text,
      documentRevision, sourceRevision, signal } = {}) {
      const id = itemId != null ? itemId : bookId;
      return this._requestJson("PUT",
        `/v1/items/${encodePart(id)}/translations/${encodePart(translationId)}` +
        `/pages/${encodePart(selector)}`, {
          headers: {
            "If-Document-Match": quoteRevision(
              documentRevision, "documentRevision"),
            "If-Source-Match": quoteRevision(sourceRevision, "sourceRevision"),
          },
          body: {
            text,
            expected_document_revision: documentRevision,
            expected_source_revision: sourceRevision,
          },
          signal,
        });
    }

    _replicaPackageImport({ bookId, sourceId, file, overwrite = false,
      idempotencyKey, signal } = {}) {
      const form = this._formDataFactory();
      form.append("lib", file);
      return this._requestJson("POST",
        `/v1/items/${encodePart(bookId)}/replica/lib-imports`, {
          headers: {
            "Idempotency-Key": operationKey(
              idempotencyKey, "idempotencyKey"),
          },
          query: {
            source_id: sourceId,
            overwrite: overwrite ? 1 : undefined,
          },
          multipart: form,
          signal,
        });
    }

    _replicaPackageOpen({ file, idempotencyKey, signal } = {}) {
      const form = this._formDataFactory();
      form.append("lib", file);
      return this._requestJson("POST", "/v1/lib-opens", {
        headers: {
          "Idempotency-Key": operationKey(
            idempotencyKey, "idempotencyKey"),
        },
        multipart: form,
        signal,
      });
    }

    _replicaPackageExportUrl({ bookId, sourceId } = {}) {
      return this._url(this._buildPath(bookId, "replica-export"), {
        src: sourceId,
      });
    }

    _replicaPrintUrl({ bookId, sourceId, layer } = {}) {
      return this._url(this._buildPath(bookId, "replica-print"), {
        src: sourceId, layer,
      });
    }
  }

  return { EngineClient, EngineClientError };
});

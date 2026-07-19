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
      return body;
    }

    _pdfInfo({ path, signal } = {}) {
      return this._requestJson("GET", "/pdf/info", {
        query: { path }, signal,
      });
    }

    _capabilities({ signal } = {}) {
      return this._requestJson("GET", "/v1/capabilities", { signal });
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

    _translationList({ bookId, signal } = {}) {
      return this._requestJson("GET", this._buildPath(bookId, "translations"), {
        signal,
      });
    }

    _translationGet({ bookId, language, signal } = {}) {
      return this._requestJson("GET", this._buildPath(bookId,
        `translations/${encodePart(language)}`), { signal });
    }

    _replicaPackageImport({ bookId, sourceId, file, signal } = {}) {
      const form = this._formDataFactory();
      form.append("lib", file);
      return this._requestJson("POST", this._buildPath(bookId, "replica-import"), {
        query: { src: sourceId }, multipart: form, signal,
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

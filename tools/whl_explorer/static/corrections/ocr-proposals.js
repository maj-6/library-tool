(function installCorrectionsOcrProposals(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else Object.assign(root.LibraryToolCorrections ||= {}, api);
})(typeof globalThis !== "undefined" ? globalThis : this, function ocrProposalsFactory() {
  "use strict";

  const OCR_PROPOSALS_READ_CAPABILITY = "library.corrections.ocr-proposals.read";
  const REOCR_QUEUE_CAPABILITY = "library.corrections.reocr.queue";
  const PROPOSAL_PAGE_LIMIT = 100;
  // The engine catalog is bounded at 10 000 proposals per item.
  const MAX_PROPOSAL_PAGES = 100;
  const APPLY_BLOCKED_HINT =
    "No imported build entry — import the capture before applying OCR.";
  const RECOGNITION_INCOMPLETE_HINT =
    "This proposal has no region layout to apply.";

  function isPlainObject(value) {
    if (!value || typeof value !== "object" || Array.isArray(value)) return false;
    const prototype = Object.getPrototypeOf(value);
    return prototype === Object.prototype || prototype === null;
  }

  let operationCounter = 0;
  function defaultOperationId() {
    const cryptoRef = typeof globalThis !== "undefined" && globalThis.crypto;
    if (cryptoRef && typeof cryptoRef.randomUUID === "function") {
      return `ocr-apply-${cryptoRef.randomUUID()}`;
    }
    operationCounter += 1;
    return `ocr-apply-${Date.now().toString(36)}-${Math.random()
      .toString(36).slice(2)}-${operationCounter.toString(36)}`;
  }

  function element(documentRef, tagName, className = "", text = null) {
    const node = documentRef.createElement(tagName);
    if (className) node.className = className;
    if (text != null) node.textContent = String(text);
    return node;
  }

  function shortDigest(value) {
    return typeof value === "string" && value.length > 12
      ? `${value.slice(0, 12)}…` : String(value || "");
  }

  // Recognition payload shapes vary by provider; every read below tolerates
  // a missing or differently-typed field instead of trusting one schema.
  function recognitionText(recognition) {
    const value = isPlainObject(recognition) ? recognition.text : null;
    return typeof value === "string" ? value : null;
  }

  function recognitionSummary(recognition) {
    const value = isPlainObject(recognition) ? recognition : {};
    const regions = Array.isArray(value.regions) ? value.regions.length : null;
    const dims = isPlainObject(value.dims) ? value.dims : null;
    const width = dims ? Number(
      dims.width !== undefined ? dims.width : dims.w) : Number.NaN;
    const height = dims ? Number(
      dims.height !== undefined ? dims.height : dims.h) : Number.NaN;
    const parts = [];
    parts.push(regions === null
      ? "no region layout"
      : `${regions} region${regions === 1 ? "" : "s"}`);
    if (Number.isFinite(width) && Number.isFinite(height) &&
        width > 0 && height > 0) {
      parts.push(`${width}×${height}`);
    }
    if (Array.isArray(value.words)) {
      parts.push(`${value.words.length} word${
        value.words.length === 1 ? "" : "s"}`);
    }
    return parts.join(" · ");
  }

  function recognitionApplicable(recognition) {
    const value = isPlainObject(recognition) ? recognition : {};
    return Array.isArray(value.regions) &&
      value.regions.every(isPlainObject) &&
      isPlainObject(value.dims);
  }

  class OcrProposalsPanel {
    constructor(options = {}) {
      if (!options.root || typeof options.root.replaceChildren !== "function") {
        throw new TypeError("OCR proposals root is required");
      }
      if (!options.port || typeof options.port.list !== "function" ||
          typeof options.port.get !== "function") {
        throw new TypeError("an OCR proposals port is required");
      }
      this.root = options.root;
      this.documentRef = options.documentRef || this.root.ownerDocument;
      this.port = options.port;
      this.onStatus = typeof options.onStatus === "function"
        ? options.onStatus : () => {};
      this.operationIdFactory = typeof options.operationIdFactory === "function"
        ? options.operationIdFactory : defaultOperationId;
      this.capabilities = { read: false, queue: false };
      this.itemId = null;
      this.proposals = [];
      this.snapshotRevision = "";
      this.loaded = false;
      this.loading = false;
      this.selectedRef = "";
      this.detail = null;
      this.detailError = "";
      this.applyBusy = false;
      this.applyBlocked = new Map();
      this.message = "";
      this.messageError = false;
      // Catalog and detail reads race independently: one shared counter lets
      // a row click strand an in-flight refresh (loading stuck true) and an
      // auto-refresh strand an in-flight detail load ("Loading proposal…").
      this.catalogGeneration = 0;
      this.detailGeneration = 0;
      this.unsubscribeResults = null;
      this.destroyed = false;
      this.nodes = null;
    }

    mount() {
      const documentRef = this.documentRef;
      const header = element(documentRef, "header", "ocr-proposals-header");
      const heading = element(documentRef, "h3", "", "OCR proposals");
      const count = element(documentRef, "span", "pane-count", "0");
      count.setAttribute("data-ocr-proposals-count", "true");
      const refresh = element(documentRef, "button", "", "↻");
      refresh.type = "button";
      refresh.setAttribute("data-ocr-proposals-refresh", "true");
      refresh.setAttribute("aria-label", "Refresh OCR proposals");
      header.append(heading, count, refresh);

      const list = element(documentRef, "ul", "ocr-proposals-list");
      list.setAttribute("data-ocr-proposals-list", "true");
      list.setAttribute("aria-label", "Machine OCR proposals");

      const detail = element(documentRef, "div", "ocr-proposal-detail");
      detail.setAttribute("data-ocr-proposal-detail", "true");
      detail.hidden = true;
      const provider = element(documentRef, "p", "ocr-proposal-provider");
      provider.setAttribute("data-ocr-proposal-provider", "true");
      const source = element(documentRef, "p", "ocr-proposal-source");
      source.setAttribute("data-ocr-proposal-source", "true");
      const summary = element(documentRef, "p", "ocr-proposal-summary");
      summary.setAttribute("data-ocr-proposal-summary", "true");
      const text = element(documentRef, "pre", "ocr-proposal-text");
      text.setAttribute("data-ocr-proposal-text", "true");
      text.setAttribute("tabindex", "0");
      const actions = element(documentRef, "div", "ocr-proposal-actions");
      const apply = element(documentRef, "button", "", "Apply to page");
      apply.type = "button";
      apply.setAttribute("data-ocr-proposal-apply", "true");
      const applyHint = element(documentRef, "span", "ocr-proposal-apply-hint");
      applyHint.setAttribute("data-ocr-proposal-apply-hint", "true");
      actions.append(apply, applyHint);
      detail.append(provider, source, summary, text, actions);

      const message = element(documentRef, "p", "ocr-proposals-message");
      message.setAttribute("data-ocr-proposals-message", "true");
      message.setAttribute("aria-live", "polite");

      this.root.replaceChildren(header, list, detail, message);
      this.nodes = {
        count, refresh, list, detail, provider, source, summary, text,
        apply, applyHint, message,
      };
      refresh.addEventListener("click", () => { void this.refresh(); });
      apply.addEventListener("click", () => { void this.apply(); });
      if (typeof this.port.subscribeResults === "function") {
        this.unsubscribeResults = this.port.subscribeResults(
          (result, command) => this.observeReocrResult(result, command));
      }
      this.root.hidden = !this.capabilities.read;
      this.render();
      return this;
    }

    setCapabilities(value = {}) {
      this.capabilities = {
        read: value.read === true,
        queue: value.queue === true,
      };
      this.root.hidden = !this.capabilities.read;
      if (this.capabilities.read && this.itemId && !this.loaded &&
          !this.loading) {
        return this.refresh();
      }
      return Promise.resolve(null);
    }

    setItem(itemId) {
      const next = typeof itemId === "string" && itemId ? itemId : null;
      if (next === this.itemId) return Promise.resolve(null);
      this.itemId = next;
      this.detailGeneration += 1;
      this.proposals = [];
      this.snapshotRevision = "";
      this.loaded = false;
      this.selectedRef = "";
      this.detail = null;
      this.detailError = "";
      this.applyBlocked.clear();
      this.setMessage("");
      if (!next || !this.capabilities.read) {
        // No refresh will bump the counter here, so a late catalog response
        // for the previous item must be invalidated explicitly.
        this.catalogGeneration += 1;
        this.loading = false;
        this.render();
        return Promise.resolve(null);
      }
      return this.refresh();
    }

    async refresh() {
      if (this.destroyed || !this.itemId || !this.capabilities.read) {
        this.render();
        return null;
      }
      const generation = ++this.catalogGeneration;
      const itemId = this.itemId;
      this.loading = true;
      this.render();
      try {
        const page = await this.loadCatalog(itemId, true);
        if (this.destroyed || generation !== this.catalogGeneration) return null;
        this.proposals = page.proposals;
        this.snapshotRevision = page.snapshotRevision;
        this.loaded = true;
        if (this.selectedRef && !this.proposals.some(
          (proposal) => proposal.proposal_ref === this.selectedRef)) {
          this.detailGeneration += 1;
          this.selectedRef = "";
          this.detail = null;
          this.detailError = "";
        }
        this.setMessage("");
        return page;
      } catch (error) {
        if (this.destroyed || generation !== this.catalogGeneration) return null;
        this.proposals = [];
        this.snapshotRevision = "";
        this.loaded = false;
        if (error && typeof error.code === "string" &&
            error.code.endsWith("_module_unavailable")) {
          this.setMessage("OCR proposal module is unavailable.");
        } else {
          this.setMessage(error && error.message ||
            "OCR proposals could not be loaded.", true);
        }
        return null;
      } finally {
        if (generation === this.catalogGeneration) {
          this.loading = false;
          this.render();
        }
      }
    }

    // One complete catalog read pinned to a single snapshot revision; a
    // concurrent write conflicts (409) and the read restarts once, fresh.
    async loadCatalog(itemId, retryOnConflict) {
      const proposals = [];
      let snapshotRevision = "";
      let cursor = null;
      try {
        for (let pageIndex = 0; pageIndex < MAX_PROPOSAL_PAGES; pageIndex += 1) {
          const page = await this.port.list({
            itemId,
            cursor,
            limit: PROPOSAL_PAGE_LIMIT,
            snapshotRevision: snapshotRevision || undefined,
          });
          snapshotRevision = page.snapshot_revision;
          proposals.push(...page.proposals);
          cursor = page.next_cursor;
          if (cursor == null) {
            return { proposals, snapshotRevision };
          }
        }
      } catch (error) {
        if (retryOnConflict && error &&
            error.code === "correction_ocr_proposal_catalog_changed") {
          return this.loadCatalog(itemId, false);
        }
        throw error;
      }
      throw new Error("OCR proposal catalog exceeded its page budget");
    }

    async select(proposalRef) {
      if (this.destroyed || !this.itemId) return null;
      const summary = this.proposals.find(
        (proposal) => proposal.proposal_ref === proposalRef);
      if (!summary) return null;
      const generation = ++this.detailGeneration;
      this.selectedRef = proposalRef;
      this.detail = null;
      this.detailError = "";
      this.render();
      try {
        const response = await this.port.get({
          itemId: this.itemId,
          proposalRef,
        });
        if (this.destroyed || generation !== this.detailGeneration) return null;
        this.detail = response.proposal;
        return this.detail;
      } catch (error) {
        if (this.destroyed || generation !== this.detailGeneration) return null;
        this.detailError = error && error.message ||
          "The OCR proposal could not be loaded.";
        return null;
      } finally {
        if (generation === this.detailGeneration) this.render();
      }
    }

    async apply() {
      if (this.destroyed || this.applyBusy || !this.itemId ||
          !this.selectedRef || typeof this.port.apply !== "function" ||
          this.applyBlocked.has(this.selectedRef) ||
          !this.detail || !recognitionApplicable(this.detail.recognition)) {
        return null;
      }
      const generation = this.catalogGeneration;
      const proposalRef = this.selectedRef;
      this.applyBusy = true;
      this.render();
      try {
        const receipt = await this.port.apply({
          operationId: this.operationIdFactory(),
          itemId: this.itemId,
          proposalRef,
        });
        if (this.destroyed || generation !== this.catalogGeneration) return receipt;
        const applied = receipt.applied;
        if (applied.regions === "proposed") {
          this.setMessage(
            "Saved as proposals — the page holds reviewed work, so nothing " +
            "was overwritten.");
        } else {
          this.setMessage(`OCR applied to page ${applied.page}${
            receipt.replayed === true ? " (already applied)" : ""}.`);
        }
        return receipt;
      } catch (error) {
        if (this.destroyed || generation !== this.catalogGeneration) return null;
        if (error && error.code === "ocr_apply_capture_entry_unavailable") {
          this.applyBlocked.set(proposalRef, APPLY_BLOCKED_HINT);
          this.setMessage("");
        } else {
          this.setMessage(error && error.message ||
            "The OCR proposal could not be applied.", true);
        }
        return null;
      } finally {
        this.applyBusy = false;
        if (generation === this.catalogGeneration) this.render();
      }
    }

    observeReocrResult(result, command) {
      if (this.destroyed) return;
      const followup = result && result.ocr_followup;
      const state = followup && followup.state;
      if (state === "succeeded") {
        this.onStatus("Re-OCR complete — proposal ready");
      } else if (state === "cancelled") {
        this.onStatus("Re-OCR was cancelled");
      } else {
        const failure = (followup && followup.failure) ||
          (result && result.failure);
        this.onStatus(failure && failure.message || "Re-OCR failed", true);
      }
      if (command && command.item_id === this.itemId) {
        void this.refresh();
      }
    }

    setMessage(message, error = false) {
      this.message = String(message || "");
      this.messageError = error === true && Boolean(this.message);
      const node = this.nodes && this.nodes.message;
      if (node) {
        node.textContent = this.message;
        node.setAttribute("role", this.messageError ? "alert" : "status");
      }
    }

    render() {
      const nodes = this.nodes;
      if (!nodes || this.destroyed) return;
      nodes.count.textContent = String(this.proposals.length);
      nodes.message.textContent = this.message;
      this.renderList(nodes);
      this.renderDetail(nodes);
    }

    renderList(nodes) {
      const documentRef = this.documentRef;
      nodes.list.replaceChildren();
      if (!this.itemId) {
        nodes.list.append(element(documentRef, "li", "empty-row",
          "Select a book to list OCR proposals."));
        return;
      }
      if (this.loading && !this.proposals.length) {
        nodes.list.append(element(documentRef, "li", "empty-row",
          "Loading OCR proposals…"));
        return;
      }
      if (!this.proposals.length) {
        nodes.list.append(element(documentRef, "li", "empty-row",
          this.loaded ? "No machine OCR proposals for this item." : ""));
        return;
      }
      for (const proposal of this.proposals) {
        const row = element(documentRef, "li", "ocr-proposal-row");
        const button = element(documentRef, "button", "ocr-proposal-open");
        button.type = "button";
        button.setAttribute("data-proposal-ref", proposal.proposal_ref);
        button.setAttribute("aria-pressed",
          String(proposal.proposal_ref === this.selectedRef));
        const title = element(documentRef, "span", "ocr-proposal-row-title",
          `${proposal.provider.provider_id} · ${proposal.provider.model}`);
        const meta = element(documentRef, "span", "ocr-proposal-row-meta",
          `${proposal.source.artifact_id} · ${proposal.availability}`);
        button.append(title, meta);
        button.addEventListener("click", () => {
          void this.select(proposal.proposal_ref);
        });
        row.append(button);
        nodes.list.append(row);
      }
    }

    renderDetail(nodes) {
      const summaryRow = this.proposals.find(
        (proposal) => proposal.proposal_ref === this.selectedRef) || null;
      nodes.detail.hidden = !summaryRow;
      if (!summaryRow) return;
      nodes.provider.textContent =
        `${summaryRow.provider.provider_id} · ${summaryRow.provider.model}`;
      nodes.source.textContent = `from ${summaryRow.source.artifact_id} · ` +
        `sha ${shortDigest(summaryRow.source.content_sha256)}`;
      const detail = this.detail;
      if (this.detailError) {
        nodes.summary.textContent = this.detailError;
        nodes.text.textContent = "";
        nodes.apply.disabled = true;
        nodes.applyHint.textContent = "";
        return;
      }
      if (!detail) {
        nodes.summary.textContent = "Loading proposal…";
        nodes.text.textContent = "";
        nodes.apply.disabled = true;
        nodes.applyHint.textContent = "";
        return;
      }
      nodes.summary.textContent = recognitionSummary(detail.recognition);
      const text = recognitionText(detail.recognition);
      nodes.text.textContent = text === null
        ? "No text payload in this proposal." : text;
      nodes.text.setAttribute("data-ocr-proposal-text-state",
        text === null ? "absent" : "present");
      const blockedHint = this.applyBlocked.get(this.selectedRef) || "";
      const applicable = recognitionApplicable(detail.recognition);
      const canApply = typeof this.port.apply === "function" &&
        applicable && !blockedHint && !this.applyBusy;
      nodes.apply.disabled = !canApply;
      nodes.applyHint.textContent = blockedHint ||
        (applicable ? "" : RECOGNITION_INCOMPLETE_HINT);
    }

    destroy() {
      if (this.destroyed) return;
      this.destroyed = true;
      this.catalogGeneration += 1;
      this.detailGeneration += 1;
      if (typeof this.unsubscribeResults === "function") {
        this.unsubscribeResults();
      }
      this.unsubscribeResults = null;
      this.nodes = null;
      this.root.replaceChildren();
    }
  }

  function createOcrProposalsPanel(options = {}) {
    return new OcrProposalsPanel(options);
  }

  return {
    OCR_PROPOSALS_READ_CAPABILITY,
    OcrProposalsPanel,
    REOCR_QUEUE_CAPABILITY,
    createOcrProposalsPanel,
  };
});

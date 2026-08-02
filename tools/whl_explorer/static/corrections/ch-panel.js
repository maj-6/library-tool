(function installCorrectionsChPanel(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else Object.assign(root.LibraryToolCorrections ||= {}, api);
})(typeof globalThis !== "undefined" ? globalThis : this, function chPanelFactory() {
  "use strict";

  const CH_FIELD_LABELS = {
    title: "Title",
    author: "Author",
    year: "Year",
    edition: "Edition",
    volume: "Volume",
    publisher: "Publisher",
    city: "City",
    pages: "Pages",
    condition: "Condition",
    illustrations: "Illustrations",
    price: "Price",
    categories: "Categories",
    notes: "Notes",
  };
  // The phone's ChMergePresenter.FIELD_ORDER, minus the identity trio the
  // definition list already leads with.
  const CH_DETAIL_FIELD_ORDER = [
    "edition", "volume", "publisher", "city", "pages", "condition",
    "illustrations", "price", "categories", "notes",
  ];
  const REMATCH_HINT =
    "The master-list row changed since approval — approve again to re-pin it.";
  const CONFLICT_QUIET_MESSAGE =
    "The item changed elsewhere — the CH state was reloaded.";
  const HIDDEN_CODES = new Set(["not_capture_backed", "item_not_found"]);
  const STALE_LIST_CODES = new Set([
    "ch_key_unknown", "ch_key_ambiguous", "ch_list_unavailable",
  ]);

  function isPlainObject(value) {
    if (!value || typeof value !== "object" || Array.isArray(value)) return false;
    const prototype = Object.getPrototypeOf(value);
    return prototype === Object.prototype || prototype === null;
  }

  let operationCounter = 0;
  function defaultChOperationId() {
    const cryptoRef = typeof globalThis !== "undefined" && globalThis.crypto;
    if (cryptoRef && typeof cryptoRef.randomUUID === "function") {
      return `ch-${cryptoRef.randomUUID()}`;
    }
    operationCounter += 1;
    return `ch-${Date.now().toString(36)}-${Math.random()
      .toString(36).slice(2)}-${operationCounter.toString(36)}`;
  }

  function element(documentRef, tagName, className = "", text = null) {
    const node = documentRef.createElement(tagName);
    if (className) node.className = className;
    if (text != null) node.textContent = String(text);
    return node;
  }

  function fieldList(value) {
    return Array.isArray(value)
      ? value.filter((name) => typeof name === "string" && name) : [];
  }

  function scoreText(value) {
    return typeof value === "number" && Number.isFinite(value)
      ? value.toFixed(2) : "";
  }

  class ChPanel {
    constructor(options = {}) {
      if (!options.root || typeof options.root.replaceChildren !== "function") {
        throw new TypeError("CH panel root is required");
      }
      if (typeof options.fetchImpl !== "function") {
        throw new TypeError("a CH panel fetch implementation is required");
      }
      this.root = options.root;
      this.documentRef = options.documentRef || this.root.ownerDocument;
      this.fetchImpl = options.fetchImpl;
      this.basePath = String(options.basePath || "/api/corrections/ch")
        .replace(/\/+$/, "");
      this.operationIdFactory = typeof options.operationIdFactory === "function"
        ? options.operationIdFactory : defaultChOperationId;
      this.onChanged = typeof options.onChanged === "function"
        ? options.onChanged : () => {};
      this.itemId = null;
      this.stateData = null;
      this.loading = false;
      this.busy = false;
      this.panelHidden = true;
      this.message = "";
      this.messageError = false;
      this.generation = 0;
      this.destroyed = false;
      this.nodes = null;
    }

    mount() {
      const documentRef = this.documentRef;
      const header = element(documentRef, "header", "ch-panel-header");
      const heading = element(documentRef, "h3", "", "CH master list");
      const refresh = element(documentRef, "button", "", "↻");
      refresh.type = "button";
      refresh.setAttribute("data-ch-refresh", "true");
      refresh.setAttribute("aria-label", "Refresh CH master-list match");
      header.append(heading, refresh);

      const state = element(documentRef, "p", "ch-state-line");
      state.setAttribute("data-ch-state", "empty");
      const stateText = element(documentRef, "span", "ch-state-text");
      stateText.setAttribute("data-ch-state-text", "true");
      const resolution = element(documentRef, "span", "ch-resolution");
      resolution.setAttribute("data-ch-resolution", "true");
      resolution.hidden = true;
      const score = element(documentRef, "span", "ch-score");
      score.setAttribute("data-ch-score", "true");
      score.hidden = true;
      state.append(stateText, resolution, score);

      const rematchHint = element(
        documentRef, "p", "ch-rematch-hint", REMATCH_HINT);
      rematchHint.setAttribute("data-ch-rematch-hint", "true");
      rematchHint.hidden = true;

      const fields = element(documentRef, "dl", "ch-match-fields");
      fields.setAttribute("data-ch-match-fields", "true");
      fields.hidden = true;

      const markers = element(documentRef, "p", "ch-markers");
      const adopted = element(documentRef, "span", "ch-marker");
      adopted.setAttribute("data-ch-adopted", "true");
      adopted.hidden = true;
      const conflicts = element(
        documentRef, "span", "ch-marker ch-marker-conflict");
      conflicts.setAttribute("data-ch-conflicts", "true");
      conflicts.hidden = true;
      markers.append(adopted, conflicts);

      const actions = element(documentRef, "div", "ch-match-actions");
      actions.setAttribute("data-ch-match-actions", "true");
      actions.hidden = true;
      const approveMatch = element(documentRef, "button", "", "Approve");
      approveMatch.type = "button";
      approveMatch.setAttribute("data-ch-approve-match", "true");
      approveMatch.hidden = true;
      const rejectMatch = element(documentRef, "button", "", "Reject");
      rejectMatch.type = "button";
      rejectMatch.setAttribute("data-ch-reject-match", "true");
      actions.append(approveMatch, rejectMatch);

      const candidates = element(documentRef, "ul", "ch-candidates");
      candidates.setAttribute("data-ch-candidates", "true");
      candidates.setAttribute("aria-label", "CH master-list candidates");

      const rejected = element(documentRef, "p", "ch-rejected");
      rejected.setAttribute("data-ch-rejected", "true");
      rejected.hidden = true;
      const rejectedText = element(documentRef, "span", "ch-rejected-text");
      rejectedText.setAttribute("data-ch-rejected-text", "true");
      const unreject = element(documentRef, "button", "", "Undo");
      unreject.type = "button";
      unreject.setAttribute("data-ch-unreject", "true");
      unreject.setAttribute("aria-label", "Undo the CH rejection");
      rejected.append(rejectedText, unreject);

      const message = element(documentRef, "p", "ch-message");
      message.setAttribute("data-ch-message", "true");
      message.setAttribute("aria-live", "polite");

      this.root.replaceChildren(header, state, rematchHint, fields, markers,
        actions, candidates, rejected, message);
      this.nodes = {
        state, stateText, resolution, score, rematchHint, fields, adopted,
        conflicts, actions, approveMatch, rejectMatch, candidates, rejected,
        rejectedText, unreject, message, refresh,
      };
      refresh.addEventListener("click", () => { void this.refresh(); });
      approveMatch.addEventListener("click", () => {
        void this.approve(this.matchActionKey());
      });
      rejectMatch.addEventListener("click", () => {
        void this.reject(this.matchActionKey());
      });
      unreject.addEventListener("click", () => { void this.unreject(); });
      this.root.hidden = true;
      this.render();
      return this;
    }

    // On resolution "rematch" the stamp key is stale; every action must use
    // the resolved row's current content key instead.
    matchActionKey() {
      const match = this.stateData && this.stateData.match || null;
      if (!match) return "";
      return match.row && match.row.key ? match.row.key : match.key || "";
    }

    setItem(itemId) {
      const next = typeof itemId === "string" && itemId ? itemId : null;
      if (next === this.itemId) return Promise.resolve(null);
      this.itemId = next;
      this.stateData = null;
      this.busy = false;
      this.setMessage("");
      if (!next) {
        this.generation += 1;
        this.loading = false;
        this.panelHidden = true;
        this.render();
        return Promise.resolve(null);
      }
      return this.refresh();
    }

    async refresh(options = {}) {
      if (this.destroyed || !this.itemId) {
        this.render();
        return null;
      }
      // While a decision is posting, an external refresh could read
      // pre-decision state and land after the decision installs its result;
      // only the decision's own reloads may pass.
      if (this.busy && options.force !== true) return null;
      const generation = ++this.generation;
      const itemId = this.itemId;
      this.loading = true;
      if (options.keepMessage !== true) this.setMessage("");
      this.render();
      try {
        const body = await this.request("GET",
          `${this.basePath}/state?item_id=${encodeURIComponent(itemId)}`);
        if (this.destroyed || generation !== this.generation) return null;
        this.stateData = body;
        this.panelHidden = false;
        return body;
      } catch (error) {
        if (this.destroyed || generation !== this.generation) return null;
        this.stateData = null;
        if (error && HIDDEN_CODES.has(error.code)) {
          this.panelHidden = true;
        } else {
          this.panelHidden = false;
          this.setMessage(error && error.message ||
            "The CH master-list state could not be loaded.", true);
        }
        return null;
      } finally {
        if (generation === this.generation) {
          this.loading = false;
          this.render();
        }
      }
    }

    approve(key) {
      return this.decide("approve", key);
    }

    reject(key) {
      return this.decide("reject", key);
    }

    async decide(kind, key) {
      const itemId = this.itemId;
      const cleanKey = typeof key === "string" ? key : "";
      if (this.destroyed || this.busy || !itemId || !cleanKey) return null;
      this.busy = true;
      this.setMessage("");
      this.render();
      try {
        return await this.postDecision(kind, itemId, cleanKey, false);
      } finally {
        this.busy = false;
        if (!this.destroyed && itemId === this.itemId) this.render();
      }
    }

    // A rejection must be recoverable without a confirm prompt: one Undo on
    // the rejected line clears it and restores whatever the matcher offers.
    async unreject() {
      const itemId = this.itemId;
      if (this.destroyed || this.busy || !itemId) return null;
      this.busy = true;
      this.setMessage("");
      this.render();
      try {
        const body = await this.request("POST", `${this.basePath}/unreject`, {
          item_id: itemId,
          operation_id: this.operationIdFactory(),
        });
        if (this.destroyed || itemId !== this.itemId) return body;
        this.setMessage(body.replayed === true
          ? "There was no rejection to undo."
          : "CH rejection undone.");
        await this.refresh({ keepMessage: true, force: true });
        if (body.replayed !== true) this.onChanged(body);
        return body;
      } catch (error) {
        if (this.destroyed || itemId !== this.itemId) return null;
        this.setMessage(error && error.message ||
          "The CH rejection could not be undone.", true);
        return null;
      } finally {
        this.busy = false;
        if (!this.destroyed && itemId === this.itemId) this.render();
      }
    }

    async postDecision(kind, itemId, key, retried) {
      try {
        const body = await this.request("POST", `${this.basePath}/${kind}`, {
          item_id: itemId,
          key,
          operation_id: this.operationIdFactory(),
        });
        if (this.destroyed || itemId !== this.itemId) return body;
        if (kind === "approve") {
          // An approval leaves exactly this state behind: the stamped match,
          // no candidates, and any prior rejection cleared. A GET raced
          // before the decision must not overwrite it with pre-decision data.
          this.generation += 1;
          this.loading = false;
          this.stateData = {
            ok: true,
            item_id: itemId,
            list_available: true,
            match: body.match || null,
            candidates: [],
            rejected: null,
          };
          this.panelHidden = false;
          const conflicts = fieldList(body.conflicts);
          this.setMessage(body.replayed === true
            ? "This CH link was already recorded."
            : conflicts.length
              ? `Linked — the scan kept: ${conflicts.join(", ")}.`
              : "CH record linked.");
        } else {
          this.setMessage(body.replayed === true
            ? "This rejection was already recorded."
            : "CH record rejected.");
          await this.refresh({ keepMessage: true, force: true });
        }
        if (body.replayed !== true) this.onChanged(body);
        return body;
      } catch (error) {
        if (this.destroyed || itemId !== this.itemId) return null;
        if (error && error.code === "item_revision_conflict") {
          // A conflicting write landed between the server's read and update;
          // reload and replay the intent once under a fresh operation id.
          await this.refresh({ keepMessage: true, force: true });
          if (!retried && !this.destroyed && itemId === this.itemId) {
            return this.postDecision(kind, itemId, key, true);
          }
          this.setMessage(CONFLICT_QUIET_MESSAGE);
          return null;
        }
        if (error && STALE_LIST_CODES.has(error.code)) {
          this.setMessage(error.message ||
            "The CH master list changed.", true);
          await this.refresh({ keepMessage: true, force: true });
          return null;
        }
        this.setMessage(error && error.message ||
          "The CH decision could not be recorded.", true);
        return null;
      }
    }

    async request(method, url, payload = null) {
      const init = {
        method,
        headers: { Accept: "application/json" },
        credentials: "same-origin",
        cache: "no-store",
      };
      if (payload !== null) {
        init.headers["Content-Type"] = "application/json";
        init.body = JSON.stringify(payload);
      }
      const response = await this.fetchImpl(url, init);
      let body = null;
      try {
        body = response && typeof response.json === "function"
          ? await response.json() : null;
      } catch (cause) {
        body = null;
      }
      if (response && response.ok && isPlainObject(body) && body.ok === true) {
        return body;
      }
      const error = new Error(
        isPlainObject(body) && typeof body.error === "string" && body.error
          ? body.error
          : `CH master-list request failed (${
            Number(response && response.status) || 0})`);
      error.name = "ChReconcileError";
      error.status = Number(response && response.status) || 0;
      error.code = isPlainObject(body) && typeof body.code === "string"
        ? body.code : "request_failed";
      throw error;
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

    stateKind(state) {
      if (!state) return this.loading ? "loading" : "error";
      const match = state.match || null;
      if (!state.list_available) return "unavailable";
      if (match) {
        if (match.resolution === "key") return "confirmed";
        if (match.resolution === "rematch") return "rematch";
        return "unresolved";
      }
      if (Array.isArray(state.candidates) && state.candidates.length) {
        return "proposed";
      }
      if (state.rejected) return "rejected";
      return "absent";
    }

    stateText(kind, state) {
      if (kind === "loading") return "Loading CH master-list match…";
      if (kind === "error") return "CH master-list state unavailable.";
      if (kind === "confirmed" || kind === "rematch") return "Confirmed";
      if (kind === "unresolved") {
        return "Approved CH record no longer resolves in the master list.";
      }
      if (kind === "unavailable") {
        return state && state.match
          ? "CH master list unavailable (approval recorded)."
          : "CH master list unavailable.";
      }
      if (kind === "proposed") {
        const count = state.candidates.length;
        return `Proposed — ${count} candidate${count === 1 ? "" : "s"}`;
      }
      if (kind === "rejected") return "No remaining master-list candidates.";
      return "No master-list match.";
    }

    render() {
      const nodes = this.nodes;
      if (!nodes || this.destroyed) return;
      this.root.hidden = this.panelHidden || !this.itemId;
      nodes.message.textContent = this.message;
      nodes.message.setAttribute("role",
        this.messageError ? "alert" : "status");
      const state = this.stateData;
      const match = state && state.match || null;
      const kind = this.stateKind(state);
      nodes.state.setAttribute("data-ch-state", kind);
      nodes.stateText.textContent = this.stateText(kind, state);
      const badged = kind === "confirmed" || kind === "rematch";
      nodes.resolution.hidden = !badged;
      nodes.resolution.textContent = badged ? match.resolution : "";
      const score = badged && match ? scoreText(match.score) : "";
      nodes.score.hidden = !score;
      nodes.score.textContent = score;
      nodes.rematchHint.hidden = kind !== "rematch";
      this.renderFields(nodes, match);
      this.renderMarkers(nodes, match);
      nodes.actions.hidden = !badged;
      nodes.approveMatch.hidden = kind !== "rematch";
      nodes.approveMatch.disabled = this.busy;
      nodes.rejectMatch.disabled = this.busy;
      nodes.refresh.disabled = this.busy;
      this.renderCandidates(nodes, state);
      const rejected = state && state.rejected || null;
      nodes.rejected.hidden = !rejected;
      nodes.rejectedText.textContent = rejected
        ? `Rejected ${rejected.key}${
          rejected.decided_at ? ` · ${rejected.decided_at}` : ""}`
        : "";
      nodes.unreject.disabled = this.busy;
    }

    renderFields(nodes, match) {
      nodes.fields.replaceChildren();
      const row = match && match.row || null;
      const source = row || match;
      if (!source) {
        nodes.fields.hidden = true;
        return;
      }
      const entries = [
        ["Title", source.title],
        ["Author", source.author],
        ["Year", row ? row.year : ""],
      ];
      const fields = row && isPlainObject(row.fields) ? row.fields : {};
      for (const name of CH_DETAIL_FIELD_ORDER) {
        entries.push([CH_FIELD_LABELS[name], fields[name]]);
      }
      let populated = false;
      for (const [label, value] of entries) {
        const text = value == null ? "" : String(value).trim();
        if (!text) continue;
        nodes.fields.append(
          element(this.documentRef, "dt", "", label),
          element(this.documentRef, "dd", "", text),
        );
        populated = true;
      }
      nodes.fields.hidden = !populated;
    }

    renderMarkers(nodes, match) {
      const adopted = match ? fieldList(match.adopted) : [];
      const conflicts = match ? fieldList(match.conflicts) : [];
      nodes.adopted.hidden = !adopted.length;
      nodes.adopted.textContent = adopted.length
        ? `adopted: ${adopted.join(", ")}` : "";
      nodes.conflicts.hidden = !conflicts.length;
      nodes.conflicts.textContent = conflicts.length
        ? `scan kept: ${conflicts.join(", ")}` : "";
    }

    renderCandidates(nodes, state) {
      nodes.candidates.replaceChildren();
      const candidates = state && Array.isArray(state.candidates)
        ? state.candidates : [];
      for (const candidate of candidates) {
        if (!isPlainObject(candidate) || !candidate.key) continue;
        const row = element(this.documentRef, "li", "ch-candidate");
        row.setAttribute("data-ch-candidate", candidate.key);
        const title = element(this.documentRef, "span",
          "ch-candidate-title", candidate.title || "(untitled row)");
        const meta = element(this.documentRef, "span", "ch-candidate-meta",
          [candidate.author, candidate.year, scoreText(candidate.score)]
            .filter(Boolean).join(" · "));
        const actions = element(this.documentRef, "div",
          "ch-candidate-actions");
        const approve = element(this.documentRef, "button", "", "Approve");
        approve.type = "button";
        approve.setAttribute("data-ch-approve", "true");
        approve.setAttribute("data-ch-key", candidate.key);
        approve.disabled = this.busy;
        approve.addEventListener("click", () => {
          void this.approve(candidate.key);
        });
        const reject = element(this.documentRef, "button", "", "Reject");
        reject.type = "button";
        reject.setAttribute("data-ch-reject", "true");
        reject.setAttribute("data-ch-key", candidate.key);
        reject.disabled = this.busy;
        reject.addEventListener("click", () => {
          void this.reject(candidate.key);
        });
        actions.append(approve, reject);
        row.append(title, meta, actions);
        nodes.candidates.append(row);
      }
    }

    destroy() {
      if (this.destroyed) return;
      this.destroyed = true;
      this.generation += 1;
      this.nodes = null;
      this.root.replaceChildren();
      this.root.hidden = true;
    }
  }

  function createChPanel(options = {}) {
    return new ChPanel(options);
  }

  return {
    ChPanel,
    createChPanel,
    defaultChOperationId,
  };
});

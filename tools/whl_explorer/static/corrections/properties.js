(function installCorrectionsProperties(root, factory) {
  const dependencies = typeof module === "object" && module.exports
    ? require("./artifact-model") : root.LibraryToolCorrections;
  const api = factory(dependencies);
  if (typeof module === "object" && module.exports) module.exports = api;
  else Object.assign(root.LibraryToolCorrections ||= {}, api);
})(typeof globalThis !== "undefined" ? globalThis : this,
  function correctionsPropertiesFactory(model) {
    "use strict";

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

    function captionDraftKey(detail) {
      if (!detail || !detail.itemId || !detail.id || !detail.revision) return "";
      return `caption:${detail.itemId}:${detail.id}:${detail.revision}`;
    }

    function assertionByOrigin(detail, origin) {
      const assertions = Array.isArray(detail && detail.captionAssertions)
        ? detail.captionAssertions : [];
      return assertions.find((assertion) => assertion.origin === origin) || null;
    }

    function roleAssertionByOrigin(detail, origin) {
      const assertions = Array.isArray(detail && detail.roleAssignments)
        ? detail.roleAssignments : [];
      return assertions.find((assertion) => assertion.origin === origin) || null;
    }

    function effectiveCaption(detail) {
      if (detail && detail.effectiveCaption) return detail.effectiveCaption;
      for (const origin of ["manual", "imported", "inherited", "machine"]) {
        const assertion = assertionByOrigin(detail, origin);
        if (assertion) return assertion;
      }
      return null;
    }

    function isRevisionConflict(error) {
      return !!error && (error.code === "artifact_revision_conflict" ||
        error.code === "conflict" || Number(error.status) === 409);
    }

    function unavailableCommands() {
      const unavailable = () => {
        const error = new Error("Correction commands are not available");
        error.code = "capability-unavailable";
        return Promise.reject(error);
      };
      return Object.freeze({
        setManualCaption: unavailable,
        clearManualCaption: unavailable,
        executeInverse: unavailable,
      });
    }

    function draftAdapter(value) {
      if (value && typeof value.getDraft === "function" &&
          typeof value.setDraft === "function" &&
          typeof value.clearDraft === "function") return value;
      const values = value instanceof Map ? value : new Map();
      return {
        getDraft: (key) => values.get(key),
        setDraft: (key, draft) => values.set(key, draft),
        clearDraft: (key) => values.delete(key),
      };
    }

    function normalizedDetail(value) {
      if (!value) return null;
      if (value.key && value.itemId && value.id && Array.isArray(value.captionAssertions)) {
        return value;
      }
      return model.decodeArtifactSummary(value);
    }

    function readReceipt(result) {
      if (!result || typeof result !== "object") return null;
      return result.receipt && typeof result.receipt === "object"
        ? result.receipt : null;
    }

    function detailFromResult(result) {
      if (!result || typeof result !== "object") return null;
      return result.detail || result.artifact || result.resource || null;
    }

    function printable(value) {
      if (value == null || value === "") return "—";
      if (typeof value === "string" || typeof value === "number" ||
          typeof value === "boolean") return String(value);
      try {
        return JSON.stringify(model.boundedJson(value), null, 2);
      } catch (error) {
        return "Unavailable";
      }
    }

    let operationCounter = 0;
    function defaultOperationId() {
      const cryptoRef = typeof globalThis !== "undefined" && globalThis.crypto;
      if (cryptoRef && typeof cryptoRef.randomUUID === "function") {
        return `caption-${cryptoRef.randomUUID()}`;
      }
      operationCounter += 1;
      return `caption-${Date.now().toString(36)}-${Math.random()
        .toString(36).slice(2)}-${operationCounter.toString(36)}`;
    }

    class PropertiesInspector {
      constructor(options = {}) {
        if (!options.root || typeof options.root.replaceChildren !== "function") {
          throw new TypeError("Properties root is required");
        }
        this.root = options.root;
        this.documentRef = options.documentRef || this.root.ownerDocument;
        const commands = options.commands || null;
        this.commands = commands || unavailableCommands();
        this.captionCapabilities = Object.freeze({
          set: Boolean(commands &&
            typeof commands.setManualCaption === "function"),
          clear: Boolean(commands &&
            typeof commands.clearManualCaption === "function"),
          undo: Boolean(commands &&
            typeof commands.executeInverse === "function"),
        });
        this.drafts = draftAdapter(options.draftStore);
        this.history = options.history || null;
        this.reloadDetail = typeof options.reloadDetail === "function"
          ? options.reloadDetail : async () => this.detail;
        this.onChanged = typeof options.onChanged === "function"
          ? options.onChanged : () => {};
        this.onStatus = typeof options.onStatus === "function"
          ? options.onStatus : () => {};
        this.operationIdFactory = typeof options.operationIdFactory === "function"
          ? options.operationIdFactory : defaultOperationId;
        this.detail = null;
        this.loading = false;
        this.busy = false;
        this.message = "";
        this.messageError = false;
        this.draft = null;
        this.undoStack = [];
        this.mutationGeneration = 0;
        this.mutationAbort = null;
        this.destroyed = false;
      }

      mount() {
        this.render();
        return this;
      }

      setSelection(value, options = {}) {
        const previousKey = this.detail && this.detail.key;
        this.detail = normalizedDetail(value);
        this.loading = options.loading === true;
        this.message = options.message || "";
        this.messageError = options.error === true;
        if (!this.detail) {
          this.draft = null;
        } else {
          const key = captionDraftKey(this.detail);
          const saved = options.draft || this.drafts.getDraft(key);
          const manual = assertionByOrigin(this.detail, "manual");
          this.draft = saved ? {
            text: String(saved.text || ""),
            language: String(saved.language || ""),
            dirty: saved.dirty !== false,
          } : {
            text: manual ? manual.text : "",
            language: manual ? manual.language : "",
            dirty: false,
          };
          if (options.draft) this.drafts.setDraft(key, this.draft);
        }
        if (previousKey !== (this.detail && this.detail.key)) this.busy = false;
        this.render();
        return this.detail;
      }

      setLoading(value) {
        this.loading = value !== false;
        this.render();
      }

      setMessage(message, error = false) {
        this.message = String(message || "");
        this.messageError = !!error;
        this.render();
      }

      machineRows() {
        const detail = this.detail;
        const machineCaption = assertionByOrigin(detail, "machine");
        const manualCaption = assertionByOrigin(detail, "manual");
        const machineRole = roleAssertionByOrigin(detail, "machine");
        const manualRole = roleAssertionByOrigin(detail, "manual");
        const effective = effectiveCaption(detail);
        const provenance = detail && detail.provenance || {};
        const source = detail && detail.source || {};
        return [
          ["Object", detail.objectType],
          ["Kind", detail.kind],
          ["Revision", detail.revision],
          ["Resource", detail.resourceState],
          ["Freshness", detail.freshness],
          ["Generated", detail.generated ? "Yes" : "No"],
          ["Effective category", detail.effectiveCategory || "other"],
          ["Effective role", detail.effectiveRole || "unlabeled"],
          ["Effective caption", effective ? effective.text : "—"],
          ["Effective caption source", effective ? effective.origin : "—"],
          ["Machine role", machineRole ? machineRole.role : "—"],
          ["Machine role confidence",
            machineRole && machineRole.confidence != null
              ? machineRole.confidence : "—"],
          ["Human role override", manualRole ? manualRole.role : "—"],
          ["Machine caption", machineCaption ? machineCaption.text : "—"],
          ["Machine caption confidence",
            machineCaption && machineCaption.confidence != null
              ? machineCaption.confidence : "—"],
          ["Human caption override", manualCaption ? manualCaption.text : "—"],
          ["Provider", provenance.provider_id || provenance.providerId || "—"],
          ["Model", provenance.model || "—"],
          ["Origin", provenance.origin || "—"],
          ["Representation", source.representationId || "—"],
          ["Representation revision", source.representationRevision || "—"],
          ["Canvas", source.canvasId || "—"],
          ["Canvas revision", source.canvasRevision || "—"],
          ["Lineage", detail.lineage && detail.lineage.length
            ? detail.lineage.map((entry) =>
              `${entry.relation}: ${entry.artifactId}@${entry.artifactRevision}`).join("\n")
            : "—"],
        ];
      }

      propertyRows(rows) {
        const list = element(this.documentRef, "dl", "property-section-list");
        for (const [name, value] of rows) {
          const row = element(this.documentRef, "div", "property-row");
          const term = element(this.documentRef, "dt", "", name);
          const description = element(this.documentRef, "dd");
          const displayed = printable(value);
          if (displayed.includes("\n")) {
            description.append(element(this.documentRef, "pre", "", displayed));
          } else {
            description.textContent = displayed;
          }
          row.append(term, description);
          list.append(row);
        }
        return list;
      }

      section(name, content, className = "") {
        const group = element(this.documentRef, "div",
          `property-card ${className}`.trim());
        const term = element(this.documentRef, "dt", "property-card-title", name);
        const description = element(this.documentRef, "dd", "property-card-body");
        description.append(content);
        group.append(term, description);
        return group;
      }

      saveDraft(value) {
        if (!this.detail || !this.draft) return;
        this.draft = { ...this.draft, ...value, dirty: true };
        const key = captionDraftKey(this.detail);
        if (key) this.drafts.setDraft(key, { ...this.draft });
      }

      humanEditor() {
        const wrapper = element(this.documentRef, "div", "caption-assertion-editor");
        const manual = assertionByOrigin(this.detail, "manual");
        const form = element(this.documentRef, "form");
        const textId = `manual-caption-${this.detail.id}`;
        const textLabel = element(this.documentRef, "label", "", "Manual caption");
        textLabel.htmlFor = textId;
        const textarea = element(this.documentRef, "textarea");
        textarea.id = textId;
        textarea.rows = 6;
        textarea.value = this.draft ? this.draft.text : "";
        textarea.disabled = this.busy || !this.captionCapabilities.set;
        textarea.addEventListener("input", () => this.saveDraft({ text: textarea.value }));

        const languageId = `manual-caption-language-${this.detail.id}`;
        const languageLabel = element(this.documentRef, "label", "", "Language");
        languageLabel.htmlFor = languageId;
        const language = element(this.documentRef, "input");
        language.id = languageId;
        language.type = "text";
        language.maxLength = 64;
        language.value = this.draft ? this.draft.language : "";
        language.disabled = this.busy || !this.captionCapabilities.set;
        language.addEventListener("input", () =>
          this.saveDraft({ language: language.value }));

        const actions = element(this.documentRef, "div", "property-actions");
        const save = element(this.documentRef, "button", "", "Save caption");
        save.type = "submit";
        save.disabled = this.busy || !this.captionCapabilities.set ||
          !(this.draft && this.draft.text.trim());
        const clear = element(this.documentRef, "button", "", "Clear manual caption");
        clear.type = "button";
        clear.disabled = this.busy || !this.captionCapabilities.clear || !manual;
        clear.addEventListener("click", () => void this.clearManualCaption());
        const undo = element(this.documentRef, "button", "", "Undo last caption change");
        undo.type = "button";
        undo.disabled = this.busy || !this.captionCapabilities.undo ||
          this.undoStack.length === 0;
        undo.addEventListener("click", () => void this.undoLast());
        actions.append(save, clear, undo);

        form.addEventListener("submit", (event) => {
          event.preventDefault();
          this.saveDraft({ text: textarea.value, language: language.value });
          void this.setManualCaption();
        });
        form.append(textLabel, textarea, languageLabel, language, actions);
        const assertionNote = manual
          ? `Human assertion at ${manual.revision || this.detail.revision}`
          : "No human caption assertion. Machine data remains immutable.";
        const note = element(this.documentRef, "p", "property-assertion-note",
          !this.captionCapabilities.set && !this.captionCapabilities.clear
            ? `${assertionNote} Caption editing is unavailable in this read-only session.`
            : assertionNote);
        wrapper.append(note, form);
        return wrapper;
      }

      genericDetail() {
        return this.propertyRows([
          ["Metadata", this.detail.metadata || {}],
          ["Provenance", this.detail.provenance || {}],
          ["Linked objects", this.detail.linkedKeys || []],
        ]);
      }

      render() {
        if (this.destroyed || !this.documentRef) return;
        clearNode(this.root);
        if (!this.detail) {
          const empty = element(this.documentRef, "div", "empty-row",
            "Nothing selected");
          this.root.append(empty);
          return;
        }
        if (this.loading) {
          const loading = element(this.documentRef, "p", "property-loading",
            "Loading artifact details…");
          loading.setAttribute("role", "status");
          this.root.append(loading);
        }
        if (this.message) {
          const message = element(this.documentRef, "p",
            this.messageError ? "property-message property-error" : "property-message",
            this.message);
          message.setAttribute("role", this.messageError ? "alert" : "status");
          this.root.append(message);
        }
        this.root.append(this.section("Machine and source facts",
          this.propertyRows(this.machineRows()), "machine-properties"));
        if (["artifact", "raster-artifact"].includes(this.detail.objectType)) {
          this.root.append(this.section("Human assertions",
            this.humanEditor(), "human-properties"));
        }
        this.root.append(this.section("Artifact data",
          this.genericDetail(), "generic-properties"));
      }

      async refreshAfterMutation(result, preservedDraft = null) {
        const inline = detailFromResult(result);
        let detail = inline ? normalizedDetail(inline) : null;
        if (!detail) detail = await this.reloadDetail(this.detail.key);
        if (detail) {
          this.setSelection(detail, preservedDraft ? { draft: preservedDraft } : {});
          this.onChanged(this.detail);
        } else {
          this.render();
        }
        return this.detail;
      }

      rememberInverse(result, label) {
        const receipt = readReceipt(result);
        const inverse = receipt && receipt.inverse;
        if (!inverse) return;
        const entry = Object.freeze({
          label,
          itemId: this.detail.itemId,
          receipt,
          inverse,
        });
        this.undoStack.push(entry);
        if (this.history && typeof this.history.push === "function") {
          this.history.push(entry);
        }
      }

      beginMutation() {
        this.mutationGeneration += 1;
        if (this.mutationAbort) this.mutationAbort.abort();
        this.mutationAbort = typeof AbortController === "function"
          ? new AbortController() : null;
        this.busy = true;
        this.message = "";
        this.render();
        return {
          generation: this.mutationGeneration,
          signal: this.mutationAbort && this.mutationAbort.signal,
        };
      }

      finishMutation(generation) {
        if (generation !== this.mutationGeneration || this.destroyed) return false;
        this.busy = false;
        this.render();
        return true;
      }

      async handleMutationFailure(error, attemptedDraft, generation) {
        if (generation !== this.mutationGeneration || this.destroyed) return;
        const conflict = isRevisionConflict(error);
        let message = conflict
          ? "This artifact changed elsewhere. The latest revision was loaded; your draft was kept."
          : error && error.message || "The caption change failed.";
        if (conflict) {
          try {
            await this.refreshAfterMutation(null, attemptedDraft);
          } catch (refreshError) {
            message += " The latest details could not be loaded.";
          }
        }
        this.busy = false;
        this.message = message;
        this.messageError = true;
        this.onStatus(message, true, error);
        this.render();
      }

      async setManualCaption() {
        if (!this.captionCapabilities.set || !this.detail || !this.draft ||
            !this.draft.text.trim() || this.busy) return null;
        const attempted = { ...this.draft, dirty: true };
        const mutation = this.beginMutation();
        const payload = {
          itemId: this.detail.itemId,
          artifactId: this.detail.id,
          expectedArtifactRevision: this.detail.revision,
          text: attempted.text,
          operationId: this.operationIdFactory("caption.set", this.detail),
          language: attempted.language,
          signal: mutation.signal,
        };
        try {
          const result = await this.commands.setManualCaption(payload);
          if (mutation.generation !== this.mutationGeneration || this.destroyed) return null;
          this.drafts.clearDraft(captionDraftKey(this.detail));
          this.rememberInverse(result, "Set manual caption");
          await this.refreshAfterMutation(result);
          this.busy = false;
          this.message = "Manual caption saved";
          this.messageError = false;
          this.onStatus(this.message, false, result);
          this.render();
          return result;
        } catch (error) {
          await this.handleMutationFailure(error, attempted, mutation.generation);
          return null;
        } finally {
          this.finishMutation(mutation.generation);
        }
      }

      async clearManualCaption() {
        if (!this.captionCapabilities.clear || !this.detail ||
            !assertionByOrigin(this.detail, "manual") || this.busy) return null;
        const attempted = this.draft ? { ...this.draft, dirty: true } : null;
        const mutation = this.beginMutation();
        const payload = {
          itemId: this.detail.itemId,
          artifactId: this.detail.id,
          expectedArtifactRevision: this.detail.revision,
          operationId: this.operationIdFactory("caption.clear", this.detail),
          signal: mutation.signal,
        };
        try {
          const result = await this.commands.clearManualCaption(payload);
          if (mutation.generation !== this.mutationGeneration || this.destroyed) return null;
          this.drafts.clearDraft(captionDraftKey(this.detail));
          this.rememberInverse(result, "Clear manual caption");
          await this.refreshAfterMutation(result);
          this.busy = false;
          this.message = "Manual caption cleared; the machine caption is now effective.";
          this.messageError = false;
          this.onStatus(this.message, false, result);
          this.render();
          return result;
        } catch (error) {
          await this.handleMutationFailure(error, attempted, mutation.generation);
          return null;
        } finally {
          this.finishMutation(mutation.generation);
        }
      }

      async undoLast() {
        if (!this.captionCapabilities.undo || !this.detail ||
            !this.undoStack.length || this.busy) return null;
        const entry = this.undoStack[this.undoStack.length - 1];
        const mutation = this.beginMutation();
        try {
          const result = await this.commands.executeInverse({
            itemId: entry.itemId,
            inverse: entry.inverse,
            operationId: this.operationIdFactory("correction.undo", this.detail),
            signal: mutation.signal,
          });
          if (mutation.generation !== this.mutationGeneration || this.destroyed) return null;
          this.undoStack.pop();
          await this.refreshAfterMutation(result);
          this.busy = false;
          this.message = "Caption change undone";
          this.messageError = false;
          this.onStatus(this.message, false, result);
          this.render();
          return result;
        } catch (error) {
          await this.handleMutationFailure(error, this.draft, mutation.generation);
          return null;
        } finally {
          this.finishMutation(mutation.generation);
        }
      }

      destroy() {
        this.destroyed = true;
        this.mutationGeneration += 1;
        if (this.mutationAbort) this.mutationAbort.abort();
        this.mutationAbort = null;
        clearNode(this.root);
      }
    }

    function createPropertiesInspector(options) {
      return new PropertiesInspector(options);
    }

    return {
      PropertiesInspector,
      captionDraftKey,
      createPropertiesInspector,
      effectiveCaption,
      isRevisionConflict,
    };
  });

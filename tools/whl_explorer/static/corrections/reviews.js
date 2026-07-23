(function installCorrectionsReviews(root, factory) {
  const dependencies = typeof module === "object" && module.exports
    ? require("./books") : root.LibraryToolCorrections;
  const api = factory(dependencies);
  if (typeof module === "object" && module.exports) module.exports = api;
  else Object.assign(root.LibraryToolCorrections ||= {}, api);
})(typeof globalThis !== "undefined" ? globalThis : this, function reviewsFactory(deps) {
  "use strict";

  const REVIEW_SCOPES = Object.freeze(["all", "book", "image", "region"]);
  const TARGET_RANK = Object.freeze({ book: 0, image: 1, region: 2 });
  const ACTION_PRESENTATION = Object.freeze({
    "attention.mark": "Marked for attention",
    "attention.resolve": "Resolved",
    "attention.reopen": "Reopened",
    "attention.clear": "Cleared",
  });
  let fallbackOperationSequence = 0;

  function comparePortable(left, right) {
    return left < right ? -1 : left > right ? 1 : 0;
  }

  function bookForEntry(index, entry) {
    return index.books.find((book) => book.id === entry.target.item_id) || null;
  }

  function reviewEntryLabel(index, entry) {
    const book = bookForEntry(index, entry);
    const title = book && book.title.trim() || entry.target.item_id;
    if (entry.target.kind === "book") return title;
    if (entry.target.kind === "image") {
      const capture = book && book.captures.find(
        (candidate) => candidate.artifact_id === entry.target.artifact_id);
      return `${title} · Image · ${
        capture && capture.label.trim() || entry.target.artifact_id}`;
    }
    return `${title} · Region · ${entry.target.annotation_id}`;
  }

  function compareReviewEntries(index, left, right, booksById = null) {
    if (left.review.state !== right.review.state) {
      return left.review.state === "needs_attention" ? -1 : 1;
    }
    const leftBook = booksById
      ? booksById.get(left.target.item_id) : bookForEntry(index, left);
    const rightBook = booksById
      ? booksById.get(right.target.item_id) : bookForEntry(index, right);
    return comparePortable(
      deps.stableTitleKey(leftBook && leftBook.title || left.target.item_id),
      deps.stableTitleKey(rightBook && rightBook.title || right.target.item_id),
    ) ||
      comparePortable(left.target.item_id, right.target.item_id) ||
      (TARGET_RANK[left.target.kind] - TARGET_RANK[right.target.kind]) ||
      comparePortable(deps.targetIdentity(left.target), deps.targetIdentity(right.target)) ||
      comparePortable(left.key, right.key);
  }

  function filterReviewEntries(index, options = {}) {
    if (!index) return [];
    const scope = REVIEW_SCOPES.includes(options.scope) ? options.scope : "all";
    const showResolved = options.showResolved === true;
    const booksById = new Map(index.books.map((book) => [book.id, book]));
    return [...index.attention]
      .filter((entry) =>
        (scope === "all" || entry.target.kind === scope) &&
        (showResolved || entry.review.state === "needs_attention"))
      .sort((left, right) =>
        compareReviewEntries(index, left, right, booksById));
  }

  function nextReviewEntry(index, currentKey, options = {}) {
    const entries = filterReviewEntries(index, options);
    if (!entries.length) return null;
    const currentIndex = entries.findIndex((entry) => entry.key === currentKey);
    if (currentIndex < 0) return entries[0];
    return entries[(currentIndex + 1) % entries.length];
  }

  function defaultOperationId() {
    const cryptoRef = typeof globalThis !== "undefined" && globalThis.crypto;
    if (cryptoRef && typeof cryptoRef.randomUUID === "function") {
      return `review-${cryptoRef.randomUUID()}`;
    }
    fallbackOperationSequence += 1;
    return `review-${Date.now().toString(36)}-${fallbackOperationSequence.toString(36)}`;
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

  function safeActor(value) {
    if (typeof value !== "string" ||
        !/^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,255}$/.test(value)) {
      throw new TypeError("A portable actor identity is required");
    }
    return value;
  }

  function eventDescription(event) {
    const action = ACTION_PRESENTATION[event.action] || event.action;
    const suffix = event.comment ? ` — ${event.comment}` : "";
    return `${action} by ${event.actor_id} at ${event.occurred_at}${suffix}`;
  }

  class ReviewsPanelController {
    constructor(options = {}) {
      if (!options.root || typeof options.root.querySelector !== "function") {
        throw new TypeError("Review panel root is required");
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
      this.actorIdProvider = options.actorIdProvider || null;
      this.operationIdFactory = typeof options.operationIdFactory === "function"
        ? options.operationIdFactory : defaultOperationId;
      this.scope = "all";
      this.showResolved = false;
      this.advanceOnResolve = options.advanceOnResolve !== false;
      this.busyKeys = new Set();
      this.auditOpen = new Set();
      this.auditCache = new Map();
      this.auditErrors = new Map();
      this.auditLoading = new Set();
      this.unsubscribe = null;
      this.listeners = [];
      this.mounted = false;
      this.lastMessage = "";
      this.lastMessageError = false;
    }

    listen(target, type, handler) {
      if (!target || typeof target.addEventListener !== "function") return;
      target.addEventListener(type, handler);
      this.listeners.push(() => target.removeEventListener(type, handler));
    }

    mount() {
      if (this.mounted) return this;
      this.mounted = true;
      this.unsubscribe = this.store.subscribe((snapshot) => this.render(snapshot));
      return this;
    }

    actionCapability() {
      if (!this.store.api ||
          typeof this.store.api.resolveReview !== "function" ||
          typeof this.store.api.reopenReview !== "function") {
        return { enabled: false, reason: "Review actions are unavailable." };
      }
      if (!this.actorIdProvider) {
        return {
          enabled: false,
          reason: "Review actions require an actor identity.",
        };
      }
      return { enabled: true, reason: "" };
    }

    entries(snapshot = this.store.snapshot()) {
      return filterReviewEntries(snapshot.index, {
        scope: this.scope,
        showResolved: this.showResolved,
      });
    }

    navigate(entry, source = "reviews") {
      const address = deps.selectionAddressFromTarget(entry.target);
      this.store.setSelection(address, { ownedByFeature: true });
      this.onNavigate(address, Object.freeze({
        source,
        targetKind: entry.target.kind,
        reviewKey: entry.key,
      }));
      return address;
    }

    async actorId() {
      const value = typeof this.actorIdProvider === "function"
        ? await this.actorIdProvider() : this.actorIdProvider;
      return safeActor(value);
    }

    async transition(entry, action, comment = "") {
      if (this.busyKeys.has(entry.key)) return null;
      const capability = this.actionCapability();
      if (!capability.enabled) {
        this.setMessage(capability.reason, true);
        return null;
      }
      const beforeEntries = this.entries();
      const priorIndex = beforeEntries.findIndex((candidate) => candidate.key === entry.key);
      this.busyKeys.add(entry.key);
      this.setMessage(
        `${action === "resolve" ? "Resolving" : "Reopening"} ${entry.key}…`);
      this.render(this.store.snapshot());
      try {
        const result = await this.store.transitionReview(action, {
          entry,
          actorId: await this.actorId(),
          operationId: this.operationIdFactory(action, entry),
          comment,
        });
        this.auditCache.delete(`${entry.key}@${entry.review.revision}`);
        this.auditOpen.delete(entry.key);
        this.setMessage(action === "resolve" ? "Review resolved." : "Review reopened.");
        if (action === "resolve" && this.advanceOnResolve) {
          const remaining = this.entries();
          if (remaining.length) {
            const next = remaining[Math.min(
              Math.max(priorIndex, 0), remaining.length - 1)];
            this.navigate(next, "reviews-advance");
          }
        }
        return result;
      } catch (error) {
        if (error instanceof deps.CorrectionsReviewConflictError ||
            error && error.code === "review_revision_conflict") {
          this.setMessage(
            "This review changed in another window. The queue has been refreshed.",
            true,
          );
        } else {
          this.setMessage(
            `The review could not be updated: ${
              error && error.message || "unknown error"}`,
            true,
          );
        }
        return null;
      } finally {
        this.busyKeys.delete(entry.key);
        this.render(this.store.snapshot());
      }
    }

    async loadAudit(entry) {
      const cacheKey = `${entry.key}@${entry.review.revision}`;
      if (this.auditCache.has(cacheKey)) return this.auditCache.get(cacheKey);
      if (this.auditLoading.has(cacheKey)) return null;
      this.auditLoading.add(cacheKey);
      this.auditErrors.delete(cacheKey);
      this.render(this.store.snapshot());
      try {
        const document = await this.store.getReview(entry.target);
        if (document.review.revision !== entry.review.revision) {
          await this.store.refresh({ reason: "audit_revision_changed" });
          throw new deps.CorrectionsReviewConflictError(
            "The audit changed while it was loading");
        }
        this.auditCache.set(cacheKey, document.review);
        return document.review;
      } catch (error) {
        this.auditErrors.set(cacheKey,
          String(error && error.message || "Audit history is unavailable").slice(0, 1000));
        return null;
      } finally {
        this.auditLoading.delete(cacheKey);
        this.render(this.store.snapshot());
      }
    }

    toggleAudit(entry) {
      if (this.auditOpen.has(entry.key)) {
        this.auditOpen.delete(entry.key);
        this.render(this.store.snapshot());
        return;
      }
      this.auditOpen.add(entry.key);
      this.render(this.store.snapshot());
      this.loadAudit(entry);
    }

    setMessage(message, error = false) {
      this.lastMessage = String(message || "").slice(0, 1000);
      this.lastMessageError = error;
      this.onStatus(this.lastMessage, error);
    }

    render(snapshot) {
      for (const remove of this.listeners.splice(0)) remove();
      const panel = this.root.querySelector("[data-tray-panel='reviews']") ||
        this.root.querySelector("[data-tray-panel=\"reviews\"]");
      const count = this.root.querySelector("[data-review-count]") ||
        this.root.querySelector("[data-tray-tab='reviews'] .pane-count") ||
        this.root.querySelector("[data-tray-tab=\"reviews\"] .pane-count");
      const openCount = snapshot.index
        ? snapshot.index.attention.filter(
          (entry) => entry.review.state === "needs_attention").length : 0;
      if (count) count.textContent = String(openCount);
      if (!panel || !this.documentRef) return;
      clearNode(panel);

      const section = element(this.documentRef, "section", "review-queue");
      setAttribute(section, "aria-label", "Corrections review queue");
      const status = element(this.documentRef, "p", "review-queue-status");
      status.dataset && (status.dataset.reviewStatus = "");
      const statusError = this.lastMessageError ||
        (!this.lastMessage && snapshot.status === "error");
      setAttribute(status, "role", statusError ? "alert" : "status");
      setAttribute(status, "aria-live", statusError ? "assertive" : "polite");

      if (!snapshot.index) {
        const messages = {
          unavailable: [
            "Review queue unavailable",
            "No Corrections data API is configured for this window.",
          ],
          idle: ["Waiting for workspace", "Open a workspace to load reviews."],
          loading: ["Loading reviews", "Loading the attention queue…"],
          error: [
            "Reviews could not be loaded",
            snapshot.error && snapshot.error.message || "The queue is unavailable.",
          ],
        };
        const [title, message] = messages[snapshot.status] || messages.idle;
        section.append(this.renderState(title, message, snapshot.status === "error"));
        panel.append(section);
        return;
      }

      const toolbar = element(this.documentRef, "div", "review-toolbar");
      const scopeLabel = element(this.documentRef, "label", "review-filter-label");
      scopeLabel.append(element(this.documentRef, "span", "", "Scope"));
      const scope = element(this.documentRef, "select", "review-filter");
      scope.dataset && (scope.dataset.reviewFilter = "");
      setAttribute(scope, "aria-label", "Filter reviews by target type");
      for (const [value, label] of [
        ["all", "All targets"], ["book", "Books"], ["image", "Images"],
        ["region", "Regions"],
      ]) {
        const option = element(this.documentRef, "option", "", label);
        option.value = value;
        option.selected = value === this.scope;
        scope.append(option);
      }
      this.listen(scope, "change", () => {
        this.scope = REVIEW_SCOPES.includes(scope.value) ? scope.value : "all";
        this.render(this.store.snapshot());
      });
      scopeLabel.append(scope);

      const resolvedLabel = element(this.documentRef, "label", "review-toggle-label");
      const resolved = element(this.documentRef, "input", "");
      resolved.type = "checkbox";
      resolved.checked = this.showResolved;
      resolved.dataset && (resolved.dataset.reviewShowResolved = "");
      this.listen(resolved, "change", () => {
        this.showResolved = resolved.checked === true;
        this.render(this.store.snapshot());
      });
      resolvedLabel.append(resolved, element(this.documentRef, "span", "", "Show resolved"));

      const advanceLabel = element(this.documentRef, "label", "review-toggle-label");
      const advance = element(this.documentRef, "input", "");
      advance.type = "checkbox";
      advance.checked = this.advanceOnResolve;
      advance.dataset && (advance.dataset.reviewAdvance = "");
      this.listen(advance, "change", () => {
        this.advanceOnResolve = advance.checked === true;
      });
      advanceLabel.append(advance, element(this.documentRef, "span", "",
        "Advance after resolve"));

      const refresh = element(this.documentRef, "button", "review-refresh", "Refresh");
      refresh.type = "button";
      refresh.disabled = snapshot.status === "loading";
      this.listen(refresh, "click", () => this.store.refresh({ reason: "manual" }));
      toolbar.append(scopeLabel, resolvedLabel, advanceLabel, refresh);
      section.append(toolbar);

      status.textContent = this.lastMessage ||
        (snapshot.status === "error"
          ? `Review refresh failed: ${
            snapshot.error && snapshot.error.message || "unknown error"}`
          : snapshot.status === "loading" ? "Refreshing reviews…" :
            `${openCount} open review${openCount === 1 ? "" : "s"}.`);
      section.append(status);

      const entries = this.entries(snapshot);
      if (!entries.length) {
        section.append(this.renderState(
          "No matching reviews",
          this.showResolved
            ? "No review entries match this target filter."
            : "Nothing in this target filter needs attention.",
        ));
        panel.append(section);
        return;
      }
      const list = element(this.documentRef, "ol", "review-list");
      list.dataset && (list.dataset.reviewList = "");
      setAttribute(list, "aria-label", "Review items");
      for (const entry of entries) list.append(this.renderEntry(snapshot.index, entry));
      section.append(list);
      panel.append(section);
    }

    renderState(title, message, error = false) {
      const state = element(this.documentRef, "div",
        `review-panel-state${error ? " is-error" : ""}`);
      setAttribute(state, "role", error ? "alert" : "status");
      state.append(
        element(this.documentRef, "strong", "review-panel-state-title", title),
        element(this.documentRef, "span", "review-panel-state-message", message),
      );
      return state;
    }

    renderEntry(index, entry) {
      const row = element(this.documentRef, "li",
        `review-entry review-${entry.review.state}`);
      if (row.dataset) {
        row.dataset.reviewKey = entry.key;
        row.dataset.targetKind = entry.target.kind;
      }
      const heading = element(this.documentRef, "div", "review-entry-heading");
      const open = element(this.documentRef, "button", "review-open",
        reviewEntryLabel(index, entry));
      open.type = "button";
      setAttribute(open, "aria-label",
        `Open ${reviewEntryLabel(index, entry)}, ${entry.target.kind} review`);
      this.listen(open, "click", () => this.navigate(entry));
      heading.append(
        open,
        element(this.documentRef, "span", "review-target-kind",
          entry.target.kind[0].toUpperCase() + entry.target.kind.slice(1)),
      );
      row.append(
        heading,
        element(this.documentRef, "p", "review-reason", entry.review.reason),
      );

      const controls = element(this.documentRef, "div", "review-entry-controls");
      const commentLabel = element(this.documentRef, "label", "review-comment-label");
      const commentId = `review-comment-${entry.key}`;
      const commentText = element(this.documentRef, "span", "", "Comment (optional)");
      const comment = element(this.documentRef, "input", "review-comment");
      comment.type = "text";
      comment.maxLength = 8192;
      comment.id = commentId;
      comment.dataset && (comment.dataset.reviewComment = entry.key);
      setAttribute(comment, "aria-label",
        `Optional comment for ${reviewEntryLabel(index, entry)}`);
      commentLabel.append(commentText, comment);

      const action = entry.review.state === "needs_attention" ? "resolve" : "reopen";
      const actionLabel = action === "resolve" ? "Resolve" : "Reopen";
      const actionButton = element(this.documentRef, "button",
        `review-${action}`, actionLabel);
      actionButton.type = "button";
      const capability = this.actionCapability();
      actionButton.disabled = !capability.enabled || this.busyKeys.has(entry.key);
      if (!capability.enabled) setAttribute(actionButton, "title", capability.reason);
      this.listen(actionButton, "click", () =>
        this.transition(entry, action, String(comment.value || "")));

      const history = element(this.documentRef, "button", "review-history",
        this.auditOpen.has(entry.key) ? "Hide audit" : "Inspect audit");
      history.type = "button";
      const auditId = `review-audit-${entry.key}`;
      setAttribute(history, "aria-expanded",
        this.auditOpen.has(entry.key) ? "true" : "false");
      setAttribute(history, "aria-controls", auditId);
      this.listen(history, "click", () => this.toggleAudit(entry));
      controls.append(commentLabel, actionButton, history);
      row.append(controls);

      if (this.auditOpen.has(entry.key)) {
        row.append(this.renderAudit(entry, auditId));
      }
      return row;
    }

    renderAudit(entry, auditId) {
      const host = element(this.documentRef, "section", "review-audit");
      host.id = auditId;
      setAttribute(host, "aria-label", `Audit history for ${entry.key}`);
      const cacheKey = `${entry.key}@${entry.review.revision}`;
      if (this.auditLoading.has(cacheKey)) {
        setAttribute(host, "aria-busy", "true");
        host.append(element(this.documentRef, "p", "", "Loading audit history…"));
        return host;
      }
      if (this.auditErrors.has(cacheKey)) {
        const error = element(this.documentRef, "p", "review-audit-error",
          this.auditErrors.get(cacheKey));
        setAttribute(error, "role", "alert");
        host.append(error);
        return host;
      }
      const review = this.auditCache.get(cacheKey);
      if (!review) {
        host.append(element(this.documentRef, "p", "", "Audit history has not loaded."));
        return host;
      }
      if (!review.history.length) {
        host.append(element(this.documentRef, "p", "", "No audit events recorded."));
        return host;
      }
      const list = element(this.documentRef, "ol", "review-audit-list");
      for (const event of review.history) {
        const item = element(this.documentRef, "li", "review-audit-event");
        item.append(
          element(this.documentRef, "span", "review-audit-description",
            eventDescription(event)),
        );
        if (event.reason) {
          item.append(element(this.documentRef, "span", "review-audit-reason",
            `Reason: ${event.reason}`));
        }
        list.append(item);
      }
      host.append(list);
      return host;
    }

    destroy() {
      if (typeof this.unsubscribe === "function") this.unsubscribe();
      this.unsubscribe = null;
      for (const remove of this.listeners.splice(0)) remove();
      this.mounted = false;
    }
  }

  function contextSelection(context) {
    if (!context || !context.item_id) return null;
    return {
      itemId: context.item_id,
      representationId: context.representation_id || null,
      canvasId: context.canvas_id || null,
      artifactId: context.artifact_id || null,
      annotationId: context.annotation_id || null,
    };
  }

  function createBooksAttentionFeature(options = {}) {
    if (!options.root) throw new TypeError("Corrections feature root is required");
    const status = typeof options.onStatus === "function"
      ? options.onStatus : () => {};
    const store = options.store || new deps.CorrectionsIndexStore({
      api: options.api || null,
      onSelectionInvalidated: (event) => {
        status("The selected item disappeared after the Corrections index refreshed.", true);
        if (typeof options.onSelectionInvalidated === "function") {
          options.onSelectionInvalidated(event);
        }
      },
    });
    const shared = {
      root: options.root,
      documentRef: options.documentRef,
      store,
      onNavigate: options.onNavigate,
      onStatus: status,
      onSelectionTarget: options.onSelectionTarget,
      onHotTarget: options.onHotTarget,
    };
    const books = options.booksController || new deps.BooksPanelController(shared);
    const reviews = options.reviewsController || new ReviewsPanelController({
      ...shared,
      actorIdProvider: options.actorIdProvider,
      operationIdFactory: options.operationIdFactory,
      advanceOnResolve: options.advanceOnResolve,
    });
    let mounted = false;
    return Object.freeze({
      store,
      books,
      reviews,
      mount() {
        if (!mounted) {
          books.mount();
          reviews.mount();
          mounted = true;
        }
        return this;
      },
      setContext(context) {
        const selection = contextSelection(context);
        if (selection) store.setSelection(selection, { ownedByFeature: false });
        if (!context || !context.workspace_id) return Promise.resolve(null);
        return store.openWorkspace(context.workspace_id, {
          selection,
          selectionOwned: false,
        }).then((result) => {
          if (typeof books.syncSelectionTarget === "function") {
            books.syncSelectionTarget(selection, {
              focused: false,
              source: "context",
            });
          }
          return result;
        });
      },
      setSelection(selection) {
        store.setSelection(selection, { ownedByFeature: false });
        if (typeof books.syncSelectionTarget === "function") {
          books.syncSelectionTarget(selection, {
            focused: false,
            source: "selection",
          });
        }
      },
      refresh(reason = "manual") {
        return store.refresh({ reason }).then((result) => {
          if (typeof books.syncSelectionTarget === "function") {
            books.syncSelectionTarget(store.snapshot().selection, {
              focused: false,
              source: "refresh",
            });
          }
          return result;
        });
      },
      destroy() {
        books.destroy();
        reviews.destroy();
        store.destroy();
        mounted = false;
      },
    });
  }

  return {
    ACTION_PRESENTATION,
    REVIEW_SCOPES,
    ReviewsPanelController,
    compareReviewEntries,
    createBooksAttentionFeature,
    defaultOperationId,
    eventDescription,
    filterReviewEntries,
    nextReviewEntry,
    reviewEntryLabel,
  };
});

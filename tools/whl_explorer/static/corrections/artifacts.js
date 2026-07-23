(function installCorrectionsArtifacts(root, factory) {
  const dependencies = typeof module === "object" && module.exports ? {
    ...require("./artifact-model"),
    ...require("./properties"),
    ...require("./artifact-editors"),
  } : root.LibraryToolCorrections;
  const api = factory(dependencies);
  if (typeof module === "object" && module.exports) module.exports = api;
  else Object.assign(root.LibraryToolCorrections ||= {}, api);
})(typeof globalThis !== "undefined" ? globalThis : this,
  function correctionsArtifactsFactory(deps) {
    "use strict";

    const DEFAULT_PAGE_LIMIT = 100;
    const TEXT_PAGE_LIMIT = 64 * 1024;
    const REGION_PAGE_LIMIT = 200;
    const MAX_TEXT_CHUNKS = 4;
    const MAX_REGION_PAGES = 4;
    const DETAIL_CACHE_LIMIT = 64;

    function capabilityError(capability) {
      const error = new Error(`${capability} is not available`);
      error.code = "capability-unavailable";
      error.capability = capability;
      return error;
    }

    function createUnavailableArtifactPorts() {
      const reject = (name) => () => Promise.reject(capabilityError(name));
      return Object.freeze({
        catalog: Object.freeze({
          list: reject("artifact catalog"),
          get: reject("artifact detail"),
        }),
        resources: Object.freeze({
          resolveRaster: reject("raster resource resolver"),
          readText: reject("paged text reader"),
          listRegions: reject("spatial annotation reader"),
        }),
        commands: Object.freeze({
          setManualCaption: reject("caption commands"),
          clearManualCaption: reject("caption commands"),
          executeInverse: reject("correction undo"),
        }),
      });
    }

    const UNAVAILABLE = createUnavailableArtifactPorts();

    function element(documentRef, name, className, value) {
      const node = documentRef.createElement(name);
      if (className) node.className = className;
      if (value != null) node.textContent = String(value);
      return node;
    }

    function clearNode(node) {
      if (typeof node.replaceChildren === "function") node.replaceChildren();
      else while (node.firstChild) node.removeChild(node.firstChild);
    }

    function cloneContext(value) {
      if (!value || typeof value !== "object") return null;
      const result = {};
      for (const name of [
        "workspace_id", "workspaceId", "item_id", "itemId",
        "representation_id", "representationId", "canvas_id", "canvasId",
        "artifact_id", "artifactId", "annotation_id", "annotationId",
        "resource_revision", "resourceRevision", "view_hint", "viewHint",
      ]) {
        if (value[name] != null) result[name] = value[name];
      }
      result.itemId = String(value.itemId || value.item_id || "");
      result.representationId = String(
        value.representationId || value.representation_id || "");
      result.canvasId = String(value.canvasId || value.canvas_id || "");
      return Object.freeze(result);
    }

    function isAbort(error) {
      return !!error && (error.name === "AbortError" || error.code === "aborted");
    }

    function errorSummary(error) {
      return Object.freeze({
        code: String(error && error.code || "artifact-load-failed").slice(0, 128),
        message: String(error && error.message || "Artifact data could not be loaded")
          .slice(0, 512),
        retryable: !!(error && error.retryable),
      });
    }

    function domToken(value) {
      let hash = 2166136261;
      for (const character of String(value)) {
        hash ^= character.codePointAt(0);
        hash = Math.imul(hash, 16777619);
      }
      return (hash >>> 0).toString(36);
    }

    function safeRasterUrl(value) {
      const url = String(value || "").trim();
      if (!url || /[\u0000-\u001f]/.test(url) ||
          /^(?:javascript|file|filesystem):/i.test(url)) return "";
      if (/^data:/i.test(url) && !/^data:image\//i.test(url)) return "";
      if (/^data:image\/svg\+xml/i.test(url)) return "";
      return url;
    }

    function resourceCursor(value) {
      if (value == null || value === "") return null;
      if (typeof value !== "string" || value.length > 1024 ||
          /[\u0000-\u001f]/.test(value)) {
        throw new TypeError("resource page cursor is invalid");
      }
      return value;
    }

    function textPage(value) {
      if (!value || typeof value !== "object" || typeof value.text !== "string") {
        throw new TypeError("text resource page is invalid");
      }
      return Object.freeze({
        text: value.text.slice(0, TEXT_PAGE_LIMIT),
        nextCursor: resourceCursor(
          value.nextCursor != null ? value.nextCursor : value.next_cursor),
      });
    }

    function regionPage(value) {
      if (!value || typeof value !== "object" || !Array.isArray(value.items) ||
          value.items.length > REGION_PAGE_LIMIT) {
        throw new TypeError("region resource page is invalid");
      }
      return Object.freeze({
        items: Object.freeze(value.items.map((row) => deps.boundedJson(row))),
        nextCursor: resourceCursor(
          value.nextCursor != null ? value.nextCursor : value.next_cursor),
      });
    }

    function defaultObjectUrls() {
      const api = typeof URL === "function" || typeof URL === "object" ? URL : null;
      return {
        create(blob) {
          if (!api || typeof api.createObjectURL !== "function") {
            throw capabilityError("object URL creation");
          }
          return api.createObjectURL(blob);
        },
        revoke(url) {
          if (api && typeof api.revokeObjectURL === "function") api.revokeObjectURL(url);
        },
      };
    }

    function emptyGroupState() {
      return {
        items: Object.freeze([]),
        loaded: false,
        loading: false,
        error: null,
        nextCursor: null,
        revision: "",
        total: null,
      };
    }

    function resourceLabel(detail) {
      return detail && (detail.label || detail.id) || "Unnamed artifact";
    }

    class ArtifactsFeature {
      constructor(options = {}) {
        if (!options.treeRoot || typeof options.treeRoot.querySelector !== "function") {
          throw new TypeError("Artifacts tree root is required");
        }
        this.treeRoot = options.treeRoot;
        this.documentRef = options.documentRef || this.treeRoot.ownerDocument;
        this.catalog = options.catalog || UNAVAILABLE.catalog;
        this.resources = options.resources || UNAVAILABLE.resources;
        this.commands = options.commands || UNAVAILABLE.commands;
        this.editorRegistry = options.editorRegistry || null;
        this.onResource = typeof options.onResource === "function"
          ? options.onResource : (resource) => {
            if (this.editorRegistry) this.editorRegistry.setResource(resource);
          };
        this.onStatus = typeof options.onStatus === "function"
          ? options.onStatus : () => {};
        this.selectionListeners = new Set();
        this.hotTargetListeners = new Set();
        if (typeof options.onSelection === "function") {
          this.selectionListeners.add(options.onSelection);
        }
        if (typeof options.onHotTarget === "function") {
          this.hotTargetListeners.add(options.onHotTarget);
        }
        this.countNode = options.countNode || null;
        this.rowHeight = Number.isFinite(options.rowHeight)
          ? Math.max(18, options.rowHeight) : deps.DEFAULT_ROW_HEIGHT;
        this.overscan = Number.isSafeInteger(options.overscan)
          ? Math.max(0, options.overscan) : deps.DEFAULT_OVERSCAN;
        this.pageLimit = Number.isSafeInteger(options.pageLimit)
          ? Math.max(1, Math.min(deps.MAX_PAGE_ITEMS, options.pageLimit))
          : DEFAULT_PAGE_LIMIT;
        this.objectUrls = options.objectUrls || defaultObjectUrls();
        this.initialExpanded = new Set(
          Array.isArray(options.initialExpandedGroups)
            ? options.initialExpandedGroups : []);
        this.expandedGroups = new Set(this.initialExpanded);
        this.groupStates = new Map(deps.ARTIFACT_GROUPS.map((group) =>
          [group.id, emptyGroupState()]));
        this.items = new Map();
        this.linkIndex = new Map();
        this.rows = Object.freeze([]);
        this.activeKey = "";
        this.selectedKey = "";
        this.hotKey = "";
        this.relatedKeys = new Set();
        this.context = null;
        this.contextGeneration = 0;
        this.selectionGeneration = 0;
        this.contextAbort = null;
        this.selectionAbort = null;
        this.listeners = [];
        this.detailCache = new Map();
        this.detailInflight = new Map();
        this.resourceReleases = [];
        this.currentResource = null;
        this.destroyed = false;
        this.mounted = false;
        this.properties = options.properties || (
          options.propertiesRoot && deps.createPropertiesInspector
            ? deps.createPropertiesInspector({
              root: options.propertiesRoot,
              documentRef: this.documentRef,
              commands: this.commands,
              draftStore: options.draftStore,
              history: options.history,
              operationIdFactory: options.operationIdFactory,
              reloadDetail: (key) => this.reloadDetail(key),
              onChanged: (detail) => this.mergeDetail(detail),
              onStatus: this.onStatus,
            })
            : null
        );
        if (this.editorRegistry && options.registerEditors !== false &&
            typeof deps.registerArtifactEditors === "function") {
          deps.registerArtifactEditors(this.editorRegistry);
        }
      }

      listen(target, type, handler, options) {
        if (!target || typeof target.addEventListener !== "function") return;
        target.addEventListener(type, handler, options);
        this.listeners.push(() => target.removeEventListener(type, handler, options));
      }

      mount() {
        if (this.mounted || this.destroyed) return this;
        this.mounted = true;
        this.treeRoot.setAttribute("role", "tree");
        this.treeRoot.setAttribute("tabindex", "0");
        this.treeRoot.setAttribute("aria-label",
          this.treeRoot.getAttribute("aria-label") || "Artifacts for selected book");
        this.listen(this.treeRoot, "scroll", () => this.handleScroll());
        this.listen(this.treeRoot, "click", (event) => this.handleClick(event));
        this.listen(this.treeRoot, "keydown", (event) => this.handleKeydown(event));
        this.listen(this.treeRoot, "focus", () => {
          if (!this.activeKey) this.activateFirst();
        });
        this.listen(this.treeRoot, "pointerover", (event) => this.handlePointerOver(event));
        this.listen(this.treeRoot, "pointerleave", () => this.setHotTarget(""));
        if (this.properties && typeof this.properties.mount === "function") {
          this.properties.mount();
        }
        this.render();
        return this;
      }

      subscribeSelection(listener) {
        if (typeof listener !== "function") throw new TypeError("listener is required");
        this.selectionListeners.add(listener);
        return () => this.selectionListeners.delete(listener);
      }

      subscribeHotTarget(listener) {
        if (typeof listener !== "function") throw new TypeError("listener is required");
        this.hotTargetListeners.add(listener);
        return () => this.hotTargetListeners.delete(listener);
      }

      emitSelection(item) {
        for (const listener of this.selectionListeners) listener(item || null);
      }

      emitHotTarget(item) {
        for (const listener of this.hotTargetListeners) listener(item || null);
      }

      resetGroups() {
        this.groupStates = new Map(deps.ARTIFACT_GROUPS.map((group) =>
          [group.id, emptyGroupState()]));
        this.items.clear();
        this.linkIndex.clear();
        this.rows = Object.freeze([]);
      }

      abortContextWork() {
        if (this.contextAbort) this.contextAbort.abort();
        this.contextAbort = typeof AbortController === "function"
          ? new AbortController() : null;
      }

      abortSelectionWork() {
        this.selectionGeneration += 1;
        if (this.selectionAbort) this.selectionAbort.abort();
        this.selectionAbort = typeof AbortController === "function"
          ? new AbortController() : null;
        this.releaseResources();
      }

      async setContext(value) {
        if (this.destroyed) return null;
        const context = cloneContext(value);
        this.contextGeneration += 1;
        this.abortContextWork();
        this.abortSelectionWork();
        this.context = context;
        this.expandedGroups = new Set(this.initialExpanded);
        this.resetGroups();
        this.activeKey = "";
        this.selectedKey = "";
        this.hotKey = "";
        this.relatedKeys.clear();
        this.detailCache.clear();
        this.detailInflight.clear();
        this.publishResource(null);
        this.emitSelection(null);
        this.emitHotTarget(null);
        if (this.properties) this.properties.setSelection(null);
        this.render();
        if (!context || !context.itemId) {
          this.onStatus("Select a book to browse artifacts", false);
          return null;
        }
        const generation = this.contextGeneration;
        const expandedLoads = Array.from(this.expandedGroups,
          (group) => this.loadGroup(group));
        const deepArtifact = value && (value.artifactId || value.artifact_id);
        const deepAnnotation = value && (value.annotationId || value.annotation_id);
        let deepLoad = null;
        if (deepArtifact) deepLoad = this.openDeepLink(`artifact:${deepArtifact}`);
        else if (deepAnnotation) deepLoad = this.openDeepLink(`annotation:${deepAnnotation}`);
        await Promise.allSettled([...expandedLoads, ...(deepLoad ? [deepLoad] : [])]);
        if (generation === this.contextGeneration && !this.destroyed) {
          this.onStatus("Artifact context ready", false);
        }
        return context;
      }

      async refresh(options = {}) {
        if (!this.context || this.destroyed) return null;
        const selectedKey = options.preserveSelection === false ? "" : this.selectedKey;
        const expanded = new Set(this.expandedGroups);
        this.contextGeneration += 1;
        this.abortContextWork();
        this.abortSelectionWork();
        this.resetGroups();
        this.expandedGroups = expanded;
        this.selectedKey = "";
        this.relatedKeys.clear();
        this.render();
        await Promise.allSettled(Array.from(expanded, (group) => this.loadGroup(group)));
        if (selectedKey) {
          if (this.items.has(selectedKey)) await this.select(selectedKey);
          else await this.openDeepLink(selectedKey);
        }
        return this.context;
      }

      groupState(group) {
        if (!this.groupStates.has(group)) this.groupStates.set(group, emptyGroupState());
        return this.groupStates.get(group);
      }

      replaceGroupState(group, patch) {
        const next = { ...this.groupState(group), ...patch };
        this.groupStates.set(group, next);
        return next;
      }

      async loadGroup(group, options = {}) {
        if (!this.context || !this.context.itemId || this.destroyed) return null;
        const state = this.groupState(group);
        if (state.loading) return null;
        if (!options.reset && state.loaded && !state.nextCursor && !state.error) return state;
        const cursor = options.reset ? null : state.nextCursor;
        const generation = this.contextGeneration;
        this.replaceGroupState(group, {
          items: options.reset ? Object.freeze([]) : state.items,
          loaded: options.reset ? false : state.loaded,
          loading: true,
          error: null,
          nextCursor: options.reset ? null : state.nextCursor,
        });
        this.render();
        try {
          const response = await this.catalog.list({
            context: this.context,
            group,
            cursor,
            limit: this.pageLimit,
            signal: this.contextAbort && this.contextAbort.signal,
          });
          if (generation !== this.contextGeneration || this.destroyed) return null;
          const page = deps.decodeArtifactPage(response, group);
          const current = this.groupState(group);
          const merged = deps.mergeArtifactItems(
            options.reset ? [] : current.items,
            page.items,
          );
          const nextCursor = page.nextCursor === cursor ? null : page.nextCursor;
          this.replaceGroupState(group, {
            items: merged,
            loaded: true,
            loading: false,
            error: null,
            nextCursor,
            revision: page.revision,
            total: Number.isSafeInteger(response.total) ? response.total : merged.length,
          });
          for (const item of merged) this.items.set(item.key, item);
          this.rebuildLinks();
          this.render();
          return this.groupState(group);
        } catch (error) {
          if (generation !== this.contextGeneration || this.destroyed || isAbort(error)) {
            return null;
          }
          const summary = errorSummary(error);
          this.replaceGroupState(group, {
            loading: false,
            loaded: true,
            error: summary,
            nextCursor: null,
          });
          this.onStatus(summary.message, true, error);
          this.render();
          return null;
        }
      }

      async openDeepLink(key) {
        if (!this.context || !key) return null;
        try {
          const detail = await this.loadDetail(key, { force: true,
            signal: this.contextAbort && this.contextAbort.signal });
          if (!detail || this.destroyed) return null;
          this.mergeDetail(detail);
          this.expandedGroups.add(detail.group);
          this.render();
          return this.select(detail.key);
        } catch (error) {
          if (!isAbort(error)) this.onStatus(
            error && error.message || "The linked artifact is unavailable", true, error);
          return null;
        }
      }

      cacheDetail(detail) {
        const cacheKey = `${detail.key}@${detail.revision}`;
        if (this.detailCache.has(cacheKey)) this.detailCache.delete(cacheKey);
        this.detailCache.set(cacheKey, detail);
        while (this.detailCache.size > DETAIL_CACHE_LIMIT) {
          this.detailCache.delete(this.detailCache.keys().next().value);
        }
      }

      async loadDetail(key, options = {}) {
        if (!this.context || !key) return null;
        const summary = this.items.get(key);
        const revision = summary && summary.revision || "";
        const cacheKey = `${key}@${revision}`;
        if (!options.force && this.detailCache.has(cacheKey)) {
          return this.detailCache.get(cacheKey);
        }
        const inflightKey = `${this.contextGeneration}:${cacheKey}`;
        if (!options.force && this.detailInflight.has(inflightKey)) {
          return this.detailInflight.get(inflightKey);
        }
        const generation = this.contextGeneration;
        const request = Promise.resolve(this.catalog.get({
          context: this.context,
          key,
          signal: options.signal || this.selectionAbort && this.selectionAbort.signal ||
            this.contextAbort && this.contextAbort.signal,
        })).then((response) => {
          if (generation !== this.contextGeneration || this.destroyed) {
            const error = new Error("Artifact detail response is stale");
            error.name = "AbortError";
            throw error;
          }
          const raw = response && (response.item || response.artifact ||
            response.annotation || response.detail || response);
          const detail = deps.decodeArtifactDetail(raw);
          if (detail.key !== key) throw new TypeError("artifact detail identity changed");
          this.cacheDetail(detail);
          return detail;
        }).finally(() => this.detailInflight.delete(inflightKey));
        this.detailInflight.set(inflightKey, request);
        return request;
      }

      async reloadDetail(key) {
        const detail = await this.loadDetail(key, {
          force: true,
          signal: this.selectionAbort && this.selectionAbort.signal,
        });
        if (detail) this.mergeDetail(detail);
        return detail;
      }

      mergeDetail(detail) {
        if (!detail) return null;
        const state = this.groupState(detail.group);
        const items = deps.mergeArtifactItems(state.items, [detail]);
        this.replaceGroupState(detail.group, { items, loaded: true });
        this.items.set(detail.key, detail);
        this.rebuildLinks();
        if (this.selectedKey === detail.key) {
          this.relatedKeys = new Set(this.linkIndex.get(detail.key) || []);
          this.emitSelection(detail);
        }
        this.render();
        return detail;
      }

      rebuildLinks() {
        this.linkIndex = deps.buildLinkIndex(Array.from(this.items.values()));
        if (this.selectedKey) {
          this.relatedKeys = new Set(this.linkIndex.get(this.selectedKey) || []);
        }
      }

      rowFromEvent(event) {
        let node = event && event.target;
        while (node && node !== this.treeRoot) {
          if (node.dataset && node.dataset.treeKey) return node;
          node = node.parentNode;
        }
        return null;
      }

      async handleClick(event) {
        const node = this.rowFromEvent(event);
        if (!node) return;
        const row = this.rows.find((candidate) => candidate.key === node.dataset.treeKey);
        if (!row || row.disabled) return;
        this.activeKey = row.key;
        if (row.type === "group") await this.toggleGroup(row.group);
        else if (row.type === "more") await this.loadGroup(row.group);
        else if (row.type === "item") await this.select(row.key, { focus: true });
        this.render();
      }

      handlePointerOver(event) {
        const node = this.rowFromEvent(event);
        const row = node && this.rows.find((candidate) =>
          candidate.key === node.dataset.treeKey);
        this.setHotTarget(row && row.type === "item" ? row.key : "");
      }

      setHotTarget(key) {
        const normalized = this.items.has(key) ? key : "";
        if (normalized === this.hotKey) return;
        this.hotKey = normalized;
        this.emitHotTarget(normalized ? this.items.get(normalized) : null);
        this.render();
      }

      navigableRows() {
        return this.rows.filter((row) => !row.disabled && row.type !== "status");
      }

      activateFirst() {
        const first = this.navigableRows()[0];
        if (!first) return;
        this.activeKey = first.key;
        this.ensureActiveVisible();
        this.render();
      }

      moveActive(delta) {
        const rows = this.navigableRows();
        if (!rows.length) return;
        let index = rows.findIndex((row) => row.key === this.activeKey);
        if (index < 0) index = delta > 0 ? -1 : 0;
        index = Math.max(0, Math.min(rows.length - 1, index + delta));
        this.activeKey = rows[index].key;
        this.ensureActiveVisible();
        this.render();
      }

      async handleKeydown(event) {
        const row = this.rows.find((candidate) => candidate.key === this.activeKey);
        if (event.key === "ArrowDown") {
          event.preventDefault();
          this.moveActive(1);
          return;
        }
        if (event.key === "ArrowUp") {
          event.preventDefault();
          this.moveActive(-1);
          return;
        }
        if (event.key === "Home" || event.key === "End") {
          event.preventDefault();
          const rows = this.navigableRows();
          const target = event.key === "Home" ? rows[0] : rows[rows.length - 1];
          if (target) {
            this.activeKey = target.key;
            this.ensureActiveVisible();
            this.render();
          }
          return;
        }
        if (event.key === "ArrowRight" && row) {
          if (row.type === "group" && !row.expanded) {
            event.preventDefault();
            await this.toggleGroup(row.group, true);
          } else if (row.type === "group") {
            const child = this.rows.find((candidate) =>
              candidate.group === row.group && candidate.level === 2 && !candidate.disabled);
            if (child) {
              event.preventDefault();
              this.activeKey = child.key;
              this.ensureActiveVisible();
              this.render();
            }
          }
          return;
        }
        if (event.key === "ArrowLeft" && row) {
          if (row.type === "group" && row.expanded) {
            event.preventDefault();
            await this.toggleGroup(row.group, false);
          } else if (row.level === 2) {
            event.preventDefault();
            this.activeKey = `group:${row.group}`;
            this.ensureActiveVisible();
            this.render();
          }
          return;
        }
        if ((event.key === "Enter" || event.key === " ") && row && !row.disabled) {
          event.preventDefault();
          if (row.type === "group") await this.toggleGroup(row.group);
          else if (row.type === "more") await this.loadGroup(row.group);
          else if (row.type === "item") await this.select(row.key);
        }
      }

      async toggleGroup(group, force) {
        const expanded = force == null ? !this.expandedGroups.has(group) : !!force;
        if (expanded) this.expandedGroups.add(group);
        else this.expandedGroups.delete(group);
        this.render();
        const state = this.groupState(group);
        if (expanded && (!state.loaded || state.error)) {
          await this.loadGroup(group, { reset: !!state.error });
        }
      }

      ensureActiveVisible() {
        const index = this.rows.findIndex((row) => row.key === this.activeKey);
        if (index < 0) return;
        const top = index * this.rowHeight;
        const bottom = top + this.rowHeight;
        const height = Number(this.treeRoot.clientHeight) || this.rowHeight * 12;
        const scrollTop = Number(this.treeRoot.scrollTop) || 0;
        if (top < scrollTop) this.treeRoot.scrollTop = top;
        else if (bottom > scrollTop + height) {
          this.treeRoot.scrollTop = Math.max(0, bottom - height);
        }
      }

      handleScroll() {
        const index = Math.max(0, Math.min(this.rows.length - 1,
          Math.floor((Number(this.treeRoot.scrollTop) || 0) / this.rowHeight)));
        const visible = this.rows.slice(index).find((row) =>
          !row.disabled && row.type !== "status");
        if (visible) this.activeKey = visible.key;
        this.render();
      }

      statusBadges(item) {
        const badges = [];
        if (item.resourceState !== "available") badges.push(item.resourceState);
        if (item.freshness !== "current") badges.push(item.freshness);
        if (item.generated) badges.push("generated");
        if (item.effectiveCategory && item.effectiveCategory !== "other") {
          badges.push(item.effectiveCategory);
        }
        if (item.effectiveRole) badges.push(item.effectiveRole);
        return badges;
      }

      rowElement(row, index) {
        const node = element(this.documentRef, "div",
          `artifact-tree-row artifact-tree-${row.type}`);
        node.id = `artifact-tree-row-${domToken(row.key)}`;
        node.dataset.treeKey = row.key;
        node.dataset.treeType = row.type;
        node.setAttribute("role", "treeitem");
        node.setAttribute("aria-level", String(row.level));
        node.style.height = `${this.rowHeight}px`;
        if (row.type === "group") {
          node.setAttribute("aria-expanded", String(row.expanded));
          node.setAttribute("aria-label",
            `${row.label}, ${row.count} loaded${row.loading ? ", loading" : ""}`);
        }
        if (row.disabled) node.setAttribute("aria-disabled", "true");
        if (row.type === "item") {
          node.dataset.artifactKey = row.key;
          node.setAttribute("aria-selected", String(row.key === this.selectedKey));
          if (this.relatedKeys.has(row.key)) node.dataset.linked = "true";
          if (row.key === this.hotKey) node.dataset.hot = "true";
        }
        if (row.key === this.activeKey) node.dataset.active = "true";

        const label = element(this.documentRef, "span", "artifact-tree-label", row.label);
        node.append(label);
        if (row.type === "group") {
          const count = element(this.documentRef, "span", "artifact-tree-count", row.count);
          count.setAttribute("aria-hidden", "true");
          node.append(count);
          if (row.loading) node.append(element(this.documentRef, "span",
            "artifact-tree-state", "Loading"));
          if (row.error) node.append(element(this.documentRef, "span",
            "artifact-tree-state artifact-tree-error", "Unavailable"));
        } else if (row.item) {
          for (const badgeValue of this.statusBadges(row.item)) {
            const badge = element(this.documentRef, "span",
              `artifact-state artifact-state-${String(badgeValue)
                .replace(/[^a-z0-9_-]/gi, "-").toLowerCase()}`, badgeValue);
            node.append(badge);
          }
        }
        const siblings = row.level === 1
          ? this.rows.filter((candidate) => candidate.level === 1)
          : this.rows.filter((candidate) =>
            candidate.level === row.level && candidate.group === row.group);
        node.setAttribute("aria-posinset",
          String(siblings.findIndex((candidate) => candidate.key === row.key) + 1));
        node.setAttribute("aria-setsize", String(siblings.length));
        return node;
      }

      render() {
        if (!this.documentRef || this.destroyed) return;
        this.rows = deps.buildArtifactTreeRows(this.groupStates, this.expandedGroups);
        if (this.activeKey && !this.rows.some((row) => row.key === this.activeKey &&
            !row.disabled)) this.activeKey = "";
        if (!this.activeKey) {
          const first = this.rows.find((row) => !row.disabled && row.type !== "status");
          if (first) this.activeKey = first.key;
        }
        const windowed = deps.virtualArtifactWindow(this.rows, {
          rowHeight: this.rowHeight,
          viewportHeight: Number(this.treeRoot.clientHeight) || this.rowHeight * 12,
          scrollTop: Number(this.treeRoot.scrollTop) || 0,
          overscan: this.overscan,
          activeKey: this.activeKey,
        });
        clearNode(this.treeRoot);
        const spacer = element(this.documentRef, "div", "artifact-tree-spacer");
        spacer.style.height = `${windowed.totalHeight}px`;
        const layer = element(this.documentRef, "div", "artifact-tree-window");
        layer.style.transform = `translateY(${windowed.paddingTop}px)`;
        for (let offset = 0; offset < windowed.rows.length; offset += 1) {
          const row = windowed.rows[offset];
          layer.append(this.rowElement(row, windowed.start + offset));
        }
        spacer.append(layer);
        this.treeRoot.append(spacer);
        const active = windowed.rows.find((row) => row.key === this.activeKey);
        if (active) {
          this.treeRoot.setAttribute("aria-activedescendant",
            `artifact-tree-row-${domToken(active.key)}`);
        } else {
          this.treeRoot.removeAttribute("aria-activedescendant");
        }
        if (this.countNode) this.countNode.textContent = String(this.items.size);
      }

      getCommandTarget() {
        const key = this.hotKey || (
          this.items.has(this.activeKey) ? this.activeKey : this.selectedKey);
        return key && this.items.get(key) || null;
      }

      selectionSnapshot() {
        return Object.freeze({
          context: this.context,
          selected: this.selectedKey && this.items.get(this.selectedKey) || null,
          active: this.items.get(this.activeKey) || null,
          hot: this.hotKey && this.items.get(this.hotKey) || null,
          linked: Object.freeze(Array.from(this.relatedKeys)),
        });
      }

      async select(key, options = {}) {
        const summary = this.items.get(key);
        if (!summary || this.destroyed) return null;
        this.selectedKey = key;
        this.activeKey = key;
        this.relatedKeys = new Set(this.linkIndex.get(key) || []);
        this.ensureActiveVisible();
        this.abortSelectionWork();
        const selectionGeneration = this.selectionGeneration;
        this.emitSelection(summary);
        if (this.properties) this.properties.setSelection(summary, { loading: true });
        this.publishResource(this.loadingResource(summary));
        this.render();
        if (options.focus && typeof this.treeRoot.focus === "function") this.treeRoot.focus();
        try {
          const detail = await this.loadDetail(key, {
            signal: this.selectionAbort && this.selectionAbort.signal,
          });
          if (!detail || selectionGeneration !== this.selectionGeneration ||
              this.selectedKey !== key || this.destroyed) return null;
          this.mergeDetail(detail);
          if (this.properties) this.properties.setSelection(detail);
          this.emitSelection(detail);
          await this.routeResource(detail, selectionGeneration);
          return detail;
        } catch (error) {
          if (selectionGeneration !== this.selectionGeneration || isAbort(error) ||
              this.destroyed) return null;
          const summaryError = errorSummary(error);
          if (this.properties) {
            this.properties.setSelection(summary, {
              message: summaryError.message,
              error: true,
            });
          }
          this.publishResource(this.unavailableResource(summary, summaryError));
          this.onStatus(summaryError.message, true, error);
          return null;
        }
      }

      loadingResource(detail) {
        return {
          id: detail.id,
          label: resourceLabel(detail),
          kind: detail.kind,
          family: detail.family,
          media_type: detail.mediaType,
          loading: true,
          missing: false,
          summary: detail,
        };
      }

      unavailableResource(detail, error = null) {
        return {
          id: detail.id,
          label: resourceLabel(detail),
          kind: detail.kind,
          family: detail.family,
          media_type: detail.mediaType,
          missing: true,
          resourceState: detail.resourceState,
          freshness: detail.freshness,
          error,
          summary: detail,
        };
      }

      publishResource(resource) {
        this.currentResource = resource || null;
        this.onResource(this.currentResource);
        if (!resource || !this.editorRegistry ||
            typeof deps.artifactEditorHint !== "function") return;
        const hint = deps.artifactEditorHint(resource);
        const remembered = this.editorRegistry.choices &&
          this.editorRegistry.choices[this.editorRegistry.family];
        if (hint && !remembered &&
            typeof this.editorRegistry.selectEditor === "function") {
          this.editorRegistry.selectEditor(hint);
        }
      }

      releaseResources() {
        for (const release of this.resourceReleases.splice(0)) {
          try { release(); } catch (error) { /* best-effort lease cleanup */ }
        }
        this.currentResource = null;
      }

      leaseResolvedRaster(value) {
        let url = "";
        let release = null;
        if (typeof value === "string") url = safeRasterUrl(value);
        else if (value && typeof value === "object") {
          url = safeRasterUrl(value.url);
          if (typeof value.revoke === "function") release = () => value.revoke();
          else if (!url && value.blob) {
            url = safeRasterUrl(this.objectUrls.create(value.blob));
            if (url) release = () => this.objectUrls.revoke(url);
          }
        }
        if (!url) throw new TypeError("raster resolver returned no safe display resource");
        if (release) this.resourceReleases.push(release);
        return Object.freeze({ url, release });
      }

      async resolveRaster(detail, variant, selectionGeneration) {
        if (!detail.resourceRef) throw capabilityError("raster resource reference");
        const value = await this.resources.resolveRaster({
          resourceRef: detail.resourceRef,
          variant,
          signal: this.selectionAbort && this.selectionAbort.signal,
        });
        if (selectionGeneration !== this.selectionGeneration || this.destroyed) {
          if (value && typeof value.revoke === "function") value.revoke();
          const error = new Error("Raster response is stale");
          error.name = "AbortError";
          throw error;
        }
        return this.leaseResolvedRaster(value);
      }

      async routeResource(detail, selectionGeneration) {
        if (detail.resourceState !== "available" &&
            !["metadata", "regions"].includes(detail.family)) {
          this.publishResource(this.unavailableResource(detail));
          return;
        }
        if (detail.family === "image") {
          const display = await this.resolveRaster(detail, "display", selectionGeneration);
          const resource = {
            id: detail.id,
            label: resourceLabel(detail),
            kind: detail.kind,
            family: "image",
            media_type: detail.mediaType || "image/*",
            url: display.url,
            resourceRef: detail.resourceRef,
            freshness: detail.freshness,
            correction: detail.correction,
            summary: detail,
            requestFull: () => this.resolveRaster(
              detail, "full", this.selectionGeneration),
          };
          this.publishResource(resource);
          return;
        }
        if (detail.family === "text") {
          await this.openText(detail, selectionGeneration);
          return;
        }
        if (detail.family === "regions") {
          await this.openRegions(detail, selectionGeneration);
          return;
        }
        if (detail.family === "metadata") {
          this.publishResource({
            id: detail.id,
            label: resourceLabel(detail),
            kind: "metadata",
            family: "metadata",
            media_type: detail.mediaType || "application/json",
            metadata: detail.metadata,
            detail,
          });
          return;
        }
        this.publishResource({
          id: detail.id,
          label: resourceLabel(detail),
          kind: detail.kind,
          family: "unknown",
          detail,
          summary: detail,
        });
      }

      async openText(detail, selectionGeneration) {
        if (!detail.resourceRef) throw capabilityError("paged text resource reference");
        const page = textPage(await this.resources.readText({
          resourceRef: detail.resourceRef,
          cursor: null,
          limit: TEXT_PAGE_LIMIT,
          signal: this.selectionAbort && this.selectionAbort.signal,
        }));
        if (selectionGeneration !== this.selectionGeneration || this.destroyed) return;
        const chunks = [page.text || detail.previewText || ""];
        const resource = {
          id: detail.id,
          label: resourceLabel(detail),
          kind: "ocr-text",
          family: "text",
          media_type: detail.mediaType || "text/plain",
          paged: true,
          chunks,
          text: chunks.join(""),
          nextCursor: page.nextCursor,
          truncated: !!page.nextCursor,
          summary: detail,
        };
        resource.loadNext = () => this.loadMoreText(
          resource, detail, selectionGeneration);
        this.publishResource(resource);
      }

      async loadMoreText(resource, detail, selectionGeneration) {
        if (!resource.nextCursor || selectionGeneration !== this.selectionGeneration) return;
        const cursor = resource.nextCursor;
        const page = textPage(await this.resources.readText({
          resourceRef: detail.resourceRef,
          cursor,
          limit: TEXT_PAGE_LIMIT,
          signal: this.selectionAbort && this.selectionAbort.signal,
        }));
        if (selectionGeneration !== this.selectionGeneration || this.destroyed ||
            this.currentResource !== resource) return;
        resource.chunks.push(page.text);
        if (resource.chunks.length > MAX_TEXT_CHUNKS) resource.chunks.shift();
        resource.text = resource.chunks.join("");
        resource.nextCursor = page.nextCursor;
        resource.truncated = resource.chunks.length >= MAX_TEXT_CHUNKS ||
          !!resource.nextCursor;
        this.publishResource(resource);
      }

      async openRegions(detail, selectionGeneration) {
        const first = detail.selector ? [{
          key: detail.key,
          id: detail.id,
          label: detail.label,
          selector: detail.selector,
          effectiveRole: detail.effectiveRole,
        }] : [];
        let page = { items: [], nextCursor: null };
        if (this.resources && typeof this.resources.listRegions === "function") {
          try {
            page = regionPage(await this.resources.listRegions({
              context: this.context,
              canvasId: detail.source && detail.source.canvasId ||
                this.context.canvasId,
              cursor: null,
              limit: REGION_PAGE_LIMIT,
              signal: this.selectionAbort && this.selectionAbort.signal,
            }));
          } catch (error) {
            if (!first.length || error.code !== "capability-unavailable") throw error;
          }
        }
        if (selectionGeneration !== this.selectionGeneration || this.destroyed) return;
        const rows = [...first, ...(Array.isArray(page.items) ? page.items : [])]
          .slice(0, REGION_PAGE_LIMIT);
        const resource = {
          id: detail.id,
          label: resourceLabel(detail),
          kind: "regions",
          family: "regions",
          paged: true,
          pages: [rows],
          regions: rows,
          nextCursor: page.nextCursor,
          summary: detail,
        };
        resource.loadNext = () => this.loadMoreRegions(
          resource, detail, selectionGeneration);
        this.publishResource(resource);
      }

      async loadMoreRegions(resource, detail, selectionGeneration) {
        if (!resource.nextCursor || selectionGeneration !== this.selectionGeneration) return;
        const page = regionPage(await this.resources.listRegions({
          context: this.context,
          canvasId: detail.source && detail.source.canvasId || this.context.canvasId,
          cursor: resource.nextCursor,
          limit: REGION_PAGE_LIMIT,
          signal: this.selectionAbort && this.selectionAbort.signal,
        }));
        if (selectionGeneration !== this.selectionGeneration || this.destroyed ||
            this.currentResource !== resource) return;
        const rows = Array.isArray(page.items) ? page.items.slice(0, REGION_PAGE_LIMIT) : [];
        resource.pages.push(rows);
        if (resource.pages.length > MAX_REGION_PAGES) resource.pages.shift();
        resource.regions = resource.pages.flat();
        resource.nextCursor = page.nextCursor;
        this.publishResource(resource);
      }

      destroy() {
        if (this.destroyed) return;
        this.destroyed = true;
        this.contextGeneration += 1;
        this.abortContextWork();
        this.abortSelectionWork();
        for (const remove of this.listeners.splice(0)) remove();
        if (this.properties && typeof this.properties.destroy === "function") {
          this.properties.destroy();
        }
        this.selectionListeners.clear();
        this.hotTargetListeners.clear();
        clearNode(this.treeRoot);
      }
    }

    function createArtifactsFeature(options) {
      return new ArtifactsFeature(options);
    }

    return {
      ArtifactsFeature,
      createArtifactsFeature,
      createUnavailableArtifactPorts,
      safeRasterUrl,
    };
  });

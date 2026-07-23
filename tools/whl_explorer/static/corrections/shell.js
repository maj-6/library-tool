(function installCorrectionsShell(root, factory) {
  const dependencies = typeof module === "object" && module.exports ? {
    ...require("./editor-registry"),
    ...require("./ui-profile"),
    ...require("./layout-controller"),
    ...require("./reviews"),
    ...require("./artifacts"),
    ...require("./image-editor"),
  } : root.LibraryToolCorrections;
  const api = factory(dependencies);
  if (typeof module === "object" && module.exports) module.exports = api;
  else {
    Object.assign(root.LibraryToolCorrections ||= {}, api);
    api.installAutoBoot(root);
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function shellFactory(deps) {
  "use strict";

  const CONTEXT_SCHEMA = "librarytool.workbench-context/1";
  const MAX_CONTEXT_BYTES = 16 * 1024;
  const MAX_HINT_DEPTH = 5;
  const MAX_HINT_KEYS = 128;
  const CONTEXT_ID_RE = /^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,255}$/;
  const CONTEXT_FIELDS = new Set([
    "schema", "workbench_id", "workspace_id", "item_id", "representation_id",
    "canvas_id", "artifact_id", "annotation_id", "resource_revision",
    "view_hint", "origin", "ui_profile_key",
  ]);
  const OPTIONAL_IDS = [
    "item_id", "representation_id", "canvas_id", "artifact_id", "annotation_id",
  ];
  const TRAY_TABS = Object.freeze(["reviews", "jobs"]);

  function isPlainObject(value) {
    if (!value || typeof value !== "object" || Array.isArray(value)) return false;
    const prototype = Object.getPrototypeOf(value);
    return prototype === Object.prototype || prototype === null;
  }

  function contextIdentifier(value, name, required = false) {
    if (value == null || value === "") {
      if (required) throw new TypeError(`${name} is required`);
      return null;
    }
    if (typeof value !== "string" || !CONTEXT_ID_RE.test(value)) {
      throw new TypeError(`${name} must be a portable identifier`);
    }
    return value;
  }

  function canonicalPortableValue(value, state, depth = 0) {
    if (depth > MAX_HINT_DEPTH) throw new TypeError("context hint is too deeply nested");
    if (value === null || typeof value === "boolean") return value;
    if (typeof value === "string") {
      if (value.length > 2048 || /[\u0000-\u0008\u000b\u000c\u000e-\u001f]/.test(value)) {
        throw new TypeError("context hint contains an invalid string");
      }
      return value;
    }
    if (typeof value === "number") {
      if (!Number.isFinite(value)) throw new TypeError("context hint contains a non-finite number");
      return value;
    }
    if (Array.isArray(value)) {
      if (value.length > 128) throw new TypeError("context hint array is too large");
      return value.map((entry) => canonicalPortableValue(entry, state, depth + 1));
    }
    if (!isPlainObject(value)) throw new TypeError("context hint must be portable JSON");

    const keys = Object.keys(value).sort();
    state.keys += keys.length;
    if (state.keys > MAX_HINT_KEYS) throw new TypeError("context hint has too many fields");
    const result = {};
    for (const key of keys) {
      if (!key || key.length > 128 ||
          ["__proto__", "constructor", "prototype"].includes(key)) {
        throw new TypeError("context hint contains an invalid field name");
      }
      const entry = value[key];
      if (entry === undefined || typeof entry === "function" ||
          typeof entry === "symbol" || typeof entry === "bigint") {
        throw new TypeError("context hint must be portable JSON");
      }
      result[key] = canonicalPortableValue(entry, state, depth + 1);
    }
    return result;
  }

  function utf8ByteLength(value) {
    if (typeof TextEncoder === "function") return new TextEncoder().encode(value).length;
    return value.length * 3;
  }

  function normalizeWorkbenchContext(value) {
    if (!isPlainObject(value)) {
      throw new TypeError("workbench context must be an object");
    }
    const unknown = Object.keys(value).filter((key) => !CONTEXT_FIELDS.has(key));
    if (unknown.length) throw new TypeError(`unknown workbench context field: ${unknown[0]}`);
    if (value.schema !== CONTEXT_SCHEMA || value.workbench_id !== "corrections") {
      throw new TypeError("Corrections workbench context is required");
    }
    const result = {
      schema: CONTEXT_SCHEMA,
      workbench_id: "corrections",
      workspace_id: contextIdentifier(value.workspace_id, "workspace_id", true),
    };
    for (const field of OPTIONAL_IDS) {
      const normalized = contextIdentifier(value[field], field);
      if (normalized !== null) result[field] = normalized;
    }
    if (value.resource_revision != null) {
      if (Number.isSafeInteger(value.resource_revision) && value.resource_revision >= 0) {
        result.resource_revision = value.resource_revision;
      } else {
        result.resource_revision = contextIdentifier(
          value.resource_revision, "resource_revision", true);
      }
    }
    const hintState = { keys: 0 };
    for (const field of ["view_hint", "origin"]) {
      if (value[field] == null) continue;
      if (!isPlainObject(value[field])) throw new TypeError(`${field} must be an object`);
      result[field] = canonicalPortableValue(value[field], hintState);
    }
    result.ui_profile_key = deps.validateProfileKey(
      value.ui_profile_key == null || value.ui_profile_key === ""
        ? "corrections/default" : value.ui_profile_key);
    if (utf8ByteLength(JSON.stringify(result)) > MAX_CONTEXT_BYTES) {
      throw new TypeError("workbench context is too large");
    }
    return result;
  }

  function clone(value) {
    return value == null ? value : JSON.parse(JSON.stringify(value));
  }

  function emptySelection() {
    return {
      itemId: null,
      representationId: null,
      canvasId: null,
      artifactId: null,
      annotationId: null,
    };
  }

  function normalizeSelection(value, fallback = null) {
    if (value == null) return emptySelection();
    if (!isPlainObject(value)) throw new TypeError("selection must be an object");
    const previous = fallback || emptySelection();
    const read = (camel, snake) => value[camel] !== undefined
      ? value[camel] : value[snake] !== undefined ? value[snake] : previous[camel];
    return {
      itemId: contextIdentifier(read("itemId", "item_id"), "selection.itemId"),
      representationId: contextIdentifier(
        read("representationId", "representation_id"),
        "selection.representationId",
      ),
      canvasId: contextIdentifier(read("canvasId", "canvas_id"), "selection.canvasId"),
      artifactId: contextIdentifier(
        read("artifactId", "artifact_id"), "selection.artifactId"),
      annotationId: contextIdentifier(
        read("annotationId", "annotation_id"), "selection.annotationId"),
    };
  }

  function selectionContext(context, selection) {
    if (!context) return null;
    const result = { ...context };
    const mappings = [
      ["item_id", "itemId"],
      ["representation_id", "representationId"],
      ["canvas_id", "canvasId"],
      ["artifact_id", "artifactId"],
      ["annotation_id", "annotationId"],
    ];
    for (const [snake, camel] of mappings) {
      if (selection[camel]) result[snake] = selection[camel];
      else delete result[snake];
    }
    return result;
  }

  function artifactSelection(item, previous = null) {
    if (!item || typeof item !== "object") return null;
    const source = item.source && typeof item.source === "object" ? item.source : {};
    const objectType = String(item.objectType || item.object_type || "").toLowerCase();
    const isAnnotation = objectType.includes("annotation") ||
      String(item.key || "").startsWith("annotation:");
    const isArtifact = !isAnnotation && (
      objectType === "artifact" ||
      objectType === "raster-artifact" ||
      String(item.key || "").startsWith("artifact:")
    );
    const base = previous || emptySelection();
    return normalizeSelection({
      itemId: item.itemId || item.item_id || base.itemId,
      representationId: source.representationId || source.representation_id ||
        base.representationId,
      canvasId: source.canvasId || source.canvas_id || base.canvasId,
      artifactId: isArtifact ? item.id || item.artifact_id : null,
      annotationId: isAnnotation ? item.id || item.annotation_id : null,
    });
  }

  class CorrectionsWindowState {
    constructor() {
      this.context = null;
      this.selection = emptySelection();
      this.resource = null;
      this.drafts = new Map();
    }

    applyContext(value) {
      const context = normalizeWorkbenchContext(value);
      this.context = context;
      this.selection = {
        itemId: context.item_id || null,
        representationId: context.representation_id || null,
        canvasId: context.canvas_id || null,
        artifactId: context.artifact_id || null,
        annotationId: context.annotation_id || null,
      };
      return clone(context);
    }

    setResource(resource) {
      this.resource = resource || null;
      return this.resource;
    }

    setSelection(value) {
      this.selection = normalizeSelection(value, this.selection);
      return { ...this.selection };
    }

    setDraft(key, value) {
      if (typeof key !== "string" || !key || key.length > 512) {
        throw new TypeError("draft key is invalid");
      }
      this.drafts.set(key, value);
    }

    getDraft(key) {
      return this.drafts.get(key);
    }

    clearDraft(key) {
      return this.drafts.delete(key);
    }

    snapshot() {
      return {
        context: clone(this.context),
        selection: { ...this.selection },
        resource: clone(this.resource),
        draftCount: this.drafts.size,
      };
    }
  }

  function safeStorage(windowRef) {
    try { return windowRef && windowRef.localStorage || null; } catch (error) { return null; }
  }

  function replaceText(node, value) {
    if (node) node.textContent = String(value);
  }

  function nextTrayTab(current, key) {
    const currentIndex = TRAY_TABS.indexOf(current);
    if (currentIndex < 0) return null;
    if (key === "Home") return TRAY_TABS[0];
    if (key === "End") return TRAY_TABS[TRAY_TABS.length - 1];
    if (key === "ArrowLeft") {
      return TRAY_TABS[(currentIndex - 1 + TRAY_TABS.length) % TRAY_TABS.length];
    }
    if (key === "ArrowRight") {
      return TRAY_TABS[(currentIndex + 1) % TRAY_TABS.length];
    }
    return null;
  }

  class CorrectionsShell {
    constructor(options = {}) {
      if (!options.root || typeof options.root.querySelector !== "function") {
        throw new TypeError("Corrections shell root is required");
      }
      this.root = options.root;
      this.documentRef = options.documentRef || this.root.ownerDocument;
      this.windowRef = options.windowRef ||
        (this.documentRef && this.documentRef.defaultView) || null;
      this.desktop = options.desktop || this.windowRef && this.windowRef.whlDesktop || null;
      this.state = options.state || new CorrectionsWindowState();
      this.profileKey = "corrections/default";
      this.listeners = [];
      this.selectionListeners = new Set();
      this.contextGeneration = 0;
      this.featureContextGeneration = 0;
      this.unsubscribeContext = null;
      this.destroyed = false;
      this.activeTrayTab = "reviews";
      const desktopCorrections = this.desktop && this.desktop.corrections || null;
      this.booksApi = options.booksApi || desktopCorrections || null;
      this.artifactPorts = options.artifactPorts ||
        desktopCorrections && desktopCorrections.artifacts || {};
      const invokeCommand = typeof options.invokeCommand === "function"
        ? options.invokeCommand
        : desktopCorrections && typeof desktopCorrections.invokeCommand === "function"
          ? desktopCorrections.invokeCommand.bind(desktopCorrections)
          : null;
      const imageOverlayRenderer = options.imageOverlayRenderer ||
        typeof deps.createPerspectiveImageRenderer === "function" &&
          deps.createPerspectiveImageRenderer({
            invokeCommand,
            initialTool: deps.TOOLS && deps.TOOLS.PERSPECTIVE,
            hasSelection: () => Boolean(
              this.state.selection.artifactId || this.state.selection.annotationId),
            clearSelection: () => this.clearResourceSelection(),
            onCommandError: (error) => this.setStatus(
              error && error.message || "The transform could not be queued", true),
            onQueueResult: () => this.setStatus("Perspective transform queued"),
          });
      this.editorRegistry = options.editorRegistry || deps.createDefaultEditorRegistry({
        documentRef: this.documentRef,
        imageOverlayRenderer,
        onSelectionChange: () => {
          this.renderEditor();
          this.persistProfile();
        },
      });
      if (typeof deps.registerArtifactEditors === "function") {
        deps.registerArtifactEditors(this.editorRegistry);
      }
      this.profileStore = options.profileStore || new deps.CorrectionsProfileStore({
        storage: options.storage || safeStorage(this.windowRef),
        normalizeLayout: deps.normalizeLayoutState,
        normalizeEditors: (value) => this.editorRegistry.validateChoices(value),
      });
      const profile = this.profileStore.load(this.profileKey);
      this.editorRegistry.restoreChoices(profile.editors);
      this.layout = options.layoutController || new deps.LayoutController({
        root: this.root,
        documentRef: this.documentRef,
        windowRef: this.windowRef,
        initialState: profile.layout,
        onChange: () => this.persistProfile(),
      });
      this.booksFeature = options.booksFeature === false ? null :
        options.booksFeature || this.createBooksFeature(options);
      this.artifactsFeature = options.artifactsFeature === false ? null :
        options.artifactsFeature || this.createArtifactsFeature(options);
    }

    createBooksFeature(options) {
      if (options.features === false ||
          typeof deps.createBooksAttentionFeature !== "function") return null;
      return deps.createBooksAttentionFeature({
        root: this.root,
        documentRef: this.documentRef,
        api: this.booksApi,
        actorIdProvider: options.actorIdProvider,
        operationIdFactory: options.reviewOperationIdFactory,
        advanceOnResolve: options.advanceOnResolve,
        onNavigate: (address, metadata) => this.selectAddress(address, metadata),
        onSelectionInvalidated: () => this.clearSelection(),
        onStatus: (message, error) => this.setStatus(message, error),
      });
    }

    createArtifactsFeature(options) {
      if (options.features === false ||
          typeof deps.createArtifactsFeature !== "function") return null;
      const treeRoot = this.root.querySelector("[data-artifacts-tree]");
      if (!treeRoot) return null;
      const ports = this.artifactPorts || {};
      return deps.createArtifactsFeature({
        treeRoot,
        countNode: this.root.querySelector("[data-artifacts-count]"),
        propertiesRoot: this.root.querySelector("[data-properties-list]"),
        documentRef: this.documentRef,
        editorRegistry: this.editorRegistry,
        registerEditors: false,
        catalog: ports.catalog,
        resources: ports.resources,
        commands: ports.commands,
        draftStore: this.state,
        history: options.correctionHistory,
        operationIdFactory: options.correctionOperationIdFactory,
        initialExpandedGroups: options.initialExpandedArtifactGroups || [
          "generated-metadata",
          "ocr-text",
          "layout-regions",
          "source-images",
        ],
        onResource: (resource) => this.setResource(resource),
        onSelection: (item) => {
          const address = artifactSelection(item, this.state.selection);
          if (address) this.selectAddress(address, { source: "artifacts" });
        },
        onHotTarget: (item) => {
          this.root.dataset.hotArtifactKey = item && item.key || "";
        },
        onStatus: (message, error) => this.setStatus(message, error),
      });
    }

    listen(target, type, handler, options) {
      if (!target || typeof target.addEventListener !== "function") return;
      target.addEventListener(type, handler, options);
      this.listeners.push(() => target.removeEventListener(type, handler, options));
    }

    mount() {
      this.bindEditorSelector();
      this.bindLayoutReset();
      this.bindWindowControls();
      this.bindTrayTabs();
      if (this.booksFeature && typeof this.booksFeature.mount === "function") {
        this.booksFeature.mount();
      }
      if (this.artifactsFeature && typeof this.artifactsFeature.mount === "function") {
        this.artifactsFeature.mount();
      }
      this.renderEditor();
      if (!this.artifactsFeature) this.renderProperties();
      this.updateProfileLabel();
      this.connectDesktopContext();
      return this;
    }

    subscribeSelection(listener) {
      if (typeof listener !== "function") throw new TypeError("selection listener is required");
      this.selectionListeners.add(listener);
      listener({ ...this.state.selection });
      return () => this.selectionListeners.delete(listener);
    }

    emitSelection(metadata = {}) {
      const selection = Object.freeze({ ...this.state.selection });
      for (const listener of [...this.selectionListeners]) {
        listener(selection, metadata);
      }
    }

    selectAddress(value, metadata = {}) {
      const previous = { ...this.state.selection };
      const selection = this.state.setSelection(value);
      if (this.booksFeature && typeof this.booksFeature.setSelection === "function") {
        this.booksFeature.setSelection(selection.itemId ? selection : null);
      }
      const changedItem = previous.itemId !== selection.itemId;
      const changedDeepLink = previous.artifactId !== selection.artifactId ||
        previous.annotationId !== selection.annotationId;
      if (this.artifactsFeature && metadata.source !== "artifacts" &&
          (changedItem || changedDeepLink || metadata.forceContext === true)) {
        const context = selectionContext(this.state.context, selection);
        void Promise.resolve()
          .then(() => this.artifactsFeature.setContext(context))
          .catch((error) => {
            if (!this.destroyed) this.setStatus(
              error && error.message || "Artifacts could not be loaded", true);
          });
      }
      this.updateContextLabels();
      this.emitSelection(metadata);
      return selection;
    }

    clearResourceSelection() {
      const selection = this.selectAddress({
        ...this.state.selection,
        artifactId: null,
        annotationId: null,
      }, { source: "editor", forceContext: true });
      this.setResource(null);
      return selection;
    }

    clearSelection() {
      const selection = this.selectAddress(emptySelection(), {
        source: "selection-invalidated",
        forceContext: true,
      });
      this.setResource(null);
      return selection;
    }

    bindEditorSelector() {
      const selector = this.root.querySelector("[data-editor-selector]");
      this.listen(selector, "change", () => {
        if (this.editorRegistry.selectEditor(selector.value)) {
          this.refreshEditorSelector();
          this.renderEditor();
          this.persistProfile();
        }
      });
      this.refreshEditorSelector();
    }

    bindLayoutReset() {
      const reset = this.root.querySelector("[data-layout-action='reset']");
      this.listen(reset, "click", () => {
        this.profileStore.clear(this.profileKey);
        this.layout.reset(false);
        this.editorRegistry.resetChoices();
        this.refreshEditorSelector();
        this.renderEditor();
        this.persistProfile();
        this.setStatus("Layout and editor choices reset");
      });
    }

    bindWindowControls() {
      const controls = this.desktop && this.desktop.win;
      for (const button of this.root.querySelectorAll("[data-window-action]")) {
        this.listen(button, "click", () => {
          if (!controls) return;
          const action = button.dataset.windowAction;
          if (action === "minimize" && typeof controls.minimize === "function") controls.minimize();
          else if (action === "maximize" && typeof controls.toggleMaximize === "function") {
            controls.toggleMaximize();
          } else if (action === "close" && typeof controls.close === "function") controls.close();
        });
      }
      if (controls && typeof controls.onMaximized === "function") {
        controls.onMaximized((maximized) => {
          const button = this.root.querySelector("[data-window-action='maximize']");
          if (button) button.setAttribute("aria-label",
            maximized ? "Restore window" : "Maximize window");
        });
      }
    }

    bindTrayTabs() {
      const activate = (name, focus = false) => {
        if (!TRAY_TABS.includes(name)) return;
        this.activeTrayTab = name;
        for (const tab of this.root.querySelectorAll("[data-tray-tab]")) {
          const selected = tab.dataset.trayTab === name;
          tab.setAttribute("aria-selected", String(selected));
          tab.tabIndex = selected ? 0 : -1;
          if (selected && focus) tab.focus();
        }
        for (const panel of this.root.querySelectorAll("[data-tray-panel]")) {
          panel.hidden = panel.dataset.trayPanel !== name;
        }
      };
      for (const tab of this.root.querySelectorAll("[data-tray-tab]")) {
        this.listen(tab, "click", () => activate(tab.dataset.trayTab));
        this.listen(tab, "keydown", (event) => {
          const next = nextTrayTab(tab.dataset.trayTab, event.key);
          if (!next) return;
          event.preventDefault();
          activate(next, true);
        });
      }
      activate(this.activeTrayTab);
    }

    async connectDesktopContext() {
      if (this.destroyed) return;
      const workbenches = this.desktop && this.desktop.workbenches;
      if (!workbenches) {
        this.setStatus("Browser preview — no desktop workbench context");
        return;
      }
      const startingGeneration = this.contextGeneration;
      if (typeof workbenches.onContext === "function") {
        this.unsubscribeContext = workbenches.onContext((context) => {
          if (this.destroyed) return;
          if (this.applyContextSafely(context)) this.contextGeneration += 1;
        });
      }
      if (typeof workbenches.currentContext === "function") {
        try {
          const context = await workbenches.currentContext();
          if (!this.destroyed && this.contextGeneration === startingGeneration && context) {
            if (this.applyContextSafely(context)) this.contextGeneration += 1;
          }
        } catch (error) {
          if (!this.destroyed && this.contextGeneration === startingGeneration) {
            this.setStatus("Workbench context is unavailable", true);
          }
        }
      }
    }

    applyContextSafely(value) {
      try {
        this.applyContext(value);
        return true;
      } catch (error) {
        this.setStatus("The workbench context is invalid", true);
        return false;
      }
    }

    applyContext(value) {
      const context = normalizeWorkbenchContext(value);
      if (context.ui_profile_key !== this.profileKey) this.applyProfile(context.ui_profile_key);
      this.state.applyContext(context);
      this.updateContextLabels();
      this.renderContextNavigation();
      this.setResource(null);
      this.emitSelection({ source: "context" });
      this.applyFeatureContext(context);
      this.setStatus("Context ready");
      return context;
    }

    updateContextLabels() {
      const context = this.state.context;
      if (!context) return;
      const selection = this.state.selection;
      const address = [
        context.workspace_id,
        selection.itemId,
        selection.representationId,
      ].filter(Boolean).join(" · ");
      replaceText(this.root.querySelector("[data-context-label]"), address);
      replaceText(this.root.querySelector("[data-workspace-status]"),
        selection.itemId
          ? `Book ${selection.itemId}`
          : `Workspace ${context.workspace_id}`);
    }

    applyFeatureContext(context) {
      const generation = ++this.featureContextGeneration;
      const tasks = [];
      if (this.booksFeature && typeof this.booksFeature.setContext === "function") {
        tasks.push(Promise.resolve().then(() => this.booksFeature.setContext(context)));
      }
      if (this.artifactsFeature && typeof this.artifactsFeature.setContext === "function") {
        tasks.push(Promise.resolve().then(() => this.artifactsFeature.setContext(context)));
      }
      if (!tasks.length) return Promise.resolve([]);
      return Promise.allSettled(tasks).then((results) => {
        if (this.destroyed || generation !== this.featureContextGeneration) return results;
        const failure = results.find((result) => result.status === "rejected");
        if (failure) {
          this.setStatus(
            failure.reason && failure.reason.message ||
              "One or more Corrections panels could not be loaded",
            true,
          );
        }
        return results;
      });
    }

    applyProfile(profileKey) {
      const profile = this.profileStore.load(profileKey);
      this.profileKey = profile.profile_key;
      this.layout.replaceState(profile.layout, false);
      this.editorRegistry.restoreChoices(profile.editors);
      this.refreshEditorSelector();
      this.renderEditor();
      this.updateProfileLabel();
    }

    persistProfile() {
      if (!this.layout || !this.editorRegistry) return;
      this.profileStore.save(this.profileKey, {
        layout: this.layout.getState(),
        editors: this.editorRegistry.serializeChoices(),
      });
      this.updateProfileLabel();
    }

    updateProfileLabel() {
      replaceText(this.root.querySelector("[data-profile-label]"),
        `Profile: ${this.profileKey}`);
    }

    setResource(resource) {
      this.state.setResource(resource);
      this.editorRegistry.setResource(resource);
      this.refreshEditorSelector();
      this.renderEditor();
      if (!this.artifactsFeature) this.renderProperties();
    }

    refreshEditorSelector() {
      const selector = this.root.querySelector("[data-editor-selector]");
      if (!selector || !this.documentRef) return;
      selector.replaceChildren();
      const compatible = this.editorRegistry.compatibleEditors();
      if (!compatible.length) {
        const editor = this.editorRegistry.currentEditor();
        const option = this.documentRef.createElement("option");
        option.value = editor ? editor.id : "";
        option.textContent = editor ? editor.label : "No compatible editor";
        selector.append(option);
        selector.disabled = true;
      } else {
        for (const editor of compatible) {
          const option = this.documentRef.createElement("option");
          option.value = editor.id;
          option.textContent = editor.label;
          option.selected = editor.id === this.editorRegistry.selectedEditorId;
          selector.append(option);
        }
        selector.disabled = compatible.length < 2;
      }
    }

    renderEditor() {
      const host = this.root.querySelector("[data-editor-host]");
      if (!host) return;
      this.editorRegistry.render(host);
      replaceText(this.root.querySelector("[data-editor-resource-label]"),
        deps.resourceLabel(this.state.resource));
    }

    renderProperties() {
      const list = this.root.querySelector("[data-properties-list]");
      if (!list || !this.documentRef) return;
      list.replaceChildren();
      const values = this.state.resource ? [
        ["Selection", deps.resourceLabel(this.state.resource)],
        ["Resource type", deps.resourceFamily(this.state.resource)],
      ] : [["Selection", "Nothing selected"]];
      for (const [name, value] of values) {
        const row = this.documentRef.createElement("div");
        const term = this.documentRef.createElement("dt");
        const description = this.documentRef.createElement("dd");
        term.textContent = name;
        description.textContent = value;
        row.append(term, description);
        list.append(row);
      }
    }

    renderContextNavigation() {
      const books = this.root.querySelector("[data-books-list]");
      const artifacts = this.root.querySelector("[data-artifacts-tree]");
      const context = this.state.context;
      if (!this.booksFeature && books && this.documentRef) {
        books.replaceChildren();
        const row = this.documentRef.createElement("li");
        row.className = "empty-row";
        row.textContent = context && context.item_id
          ? `Selected book: ${context.item_id}` : "No book selected";
        books.append(row);
      }
      if (!this.artifactsFeature && artifacts && this.documentRef) {
        artifacts.replaceChildren();
        const row = this.documentRef.createElement("div");
        row.className = "empty-row";
        row.setAttribute("role", "treeitem");
        row.setAttribute("aria-disabled", "true");
        row.textContent = context && context.artifact_id
          ? `Loading artifact ${context.artifact_id}`
          : "Artifact data has not been loaded";
        artifacts.append(row);
      }
    }

    setStatus(message, error = false) {
      const node = this.root.querySelector("[data-status-message]");
      replaceText(node, message);
      if (node) node.setAttribute("role", error ? "alert" : "status");
    }

    destroy() {
      this.destroyed = true;
      this.contextGeneration += 1;
      this.featureContextGeneration += 1;
      if (typeof this.unsubscribeContext === "function") this.unsubscribeContext();
      this.unsubscribeContext = null;
      if (this.booksFeature && typeof this.booksFeature.destroy === "function") {
        this.booksFeature.destroy();
      }
      if (this.artifactsFeature && typeof this.artifactsFeature.destroy === "function") {
        this.artifactsFeature.destroy();
      }
      this.booksFeature = null;
      this.artifactsFeature = null;
      if (this.selectionListeners) this.selectionListeners.clear();
      if (this.editorRegistry && typeof this.editorRegistry.destroy === "function") {
        this.editorRegistry.destroy();
      }
      this.layout.destroy();
      for (const remove of this.listeners.splice(0)) remove();
    }
  }

  function installAutoBoot(browserRoot) {
    if (!browserRoot || !browserRoot.document) return;
    let shell = null;
    const boot = () => {
      const element = browserRoot.document.querySelector("[data-corrections-root]");
      if (!element || shell) return;
      shell = new CorrectionsShell({
        root: element,
        documentRef: browserRoot.document,
        windowRef: browserRoot,
      }).mount();
    };
    if (browserRoot.document.readyState === "loading") {
      browserRoot.document.addEventListener("DOMContentLoaded", boot, { once: true });
    } else boot();
  }

  return {
    CONTEXT_SCHEMA,
    CorrectionsShell,
    CorrectionsWindowState,
    artifactSelection,
    installAutoBoot,
    nextTrayTab,
    normalizeSelection,
    normalizeWorkbenchContext,
    selectionContext,
  };
});

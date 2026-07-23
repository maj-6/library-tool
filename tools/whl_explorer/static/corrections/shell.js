(function installCorrectionsShell(root, factory) {
  const dependencies = typeof module === "object" && module.exports ? {
    ...require("./editor-registry"),
    ...require("./ui-profile"),
    ...require("./layout-controller"),
    ...require("./reviews"),
    ...require("./artifacts"),
    ...require("./engine-adapter"),
    ...require("./commands"),
    ...require("./keymap"),
    ...require("./artifact-overlay"),
    ...require("./classification-controls"),
    ...require("./image-editor"),
    ...require("./image-adjust-tool"),
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

  function normalizeClassificationProfile(value) {
    const source = isPlainObject(value) && isPlainObject(value.bindings)
      ? value.bindings : {};
    const bindings = {};
    const occupied = new Set();
    const definitions = Array.isArray(deps.DEFAULT_CLASSIFICATION_COMMANDS)
      ? deps.DEFAULT_CLASSIFICATION_COMMANDS : [];
    for (const command of definitions) {
      const supplied = Object.prototype.hasOwnProperty.call(source, command.id)
        ? source[command.id] : command.defaultBinding;
      let binding = "";
      try {
        binding = typeof deps.normalizeKeyBinding === "function"
          ? deps.normalizeKeyBinding(supplied) : "";
      } catch (error) {
        binding = command.defaultBinding || "";
      }
      if (binding && occupied.has(binding)) binding = "";
      if (binding) occupied.add(binding);
      bindings[command.id] = binding;
    }
    return { bindings };
  }

  function classificationProfile(controller) {
    if (!controller || !controller.registry ||
        typeof controller.registry.bindingFor !== "function") {
      return normalizeClassificationProfile(null);
    }
    const bindings = {};
    for (const command of deps.DEFAULT_CLASSIFICATION_COMMANDS || []) {
      bindings[command.id] = controller.registry.bindingFor(command.id);
    }
    return normalizeClassificationProfile({ bindings });
  }

  function targetKey(value) {
    if (!value || typeof value !== "object") return "";
    const key = String(value.key || "");
    if (key.includes(":")) return key;
    const objectType = String(
      value.objectType || value.object_type || value.type || "").toLowerCase();
    const id = value.annotationId || value.annotation_id ||
      value.artifactId || value.artifact_id || value.id || "";
    if (!id) return "";
    return objectType.includes("annotation") ||
        ["region", "mistral-box", "spatial-annotation"].includes(
          String(value.kind || "").toLowerCase())
      ? `annotation:${id}` : `artifact:${id}`;
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

  function correctionsRuntimePorts(windowRef, desktopCorrections) {
    if (desktopCorrections || !windowRef || !windowRef.engineClient ||
        typeof deps.createCorrectionsEnginePorts !== "function") {
      return null;
    }
    return deps.createCorrectionsEnginePorts(windowRef.engineClient);
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
      this.unsubscribeTransformResults = null;
      this.unsubscribeClassificationBindings = null;
      this.restoringProfile = false;
      this.destroyed = false;
      this.activeTrayTab = "reviews";
      const desktopCorrections = this.desktop && this.desktop.corrections || null;
      this.engineCorrections = correctionsRuntimePorts(
        this.windowRef, desktopCorrections);
      this.booksApi = options.booksApi || desktopCorrections ||
        this.engineCorrections && this.engineCorrections.books || null;
      this.artifactPorts = options.artifactPorts ||
        desktopCorrections && desktopCorrections.artifacts ||
        this.engineCorrections && this.engineCorrections.artifacts || {};
      const invokeCommand = typeof options.invokeCommand === "function"
        ? options.invokeCommand
        : desktopCorrections && typeof desktopCorrections.invokeCommand === "function"
          ? desktopCorrections.invokeCommand.bind(desktopCorrections)
          : this.engineCorrections &&
              typeof this.engineCorrections.invokeCommand === "function"
            ? this.engineCorrections.invokeCommand.bind(
              this.engineCorrections)
            : null;
      const imageAdjustOptions = isPlainObject(options.imageAdjustOptions)
        ? options.imageAdjustOptions : {};
      this.imageAdjustTool = options.imageAdjustTool ||
        typeof deps.createImageAdjustTool === "function" &&
          deps.createImageAdjustTool({
            ...imageAdjustOptions,
            profile: null,
            onProfileChange: (value, detail) => {
              if (typeof imageAdjustOptions.onProfileChange === "function") {
                imageAdjustOptions.onProfileChange(value, detail);
              }
              this.persistProfile();
            },
            onOcrOutcome: (outcome, detail) => {
              if (typeof imageAdjustOptions.onOcrOutcome === "function") {
                imageAdjustOptions.onOcrOutcome(outcome, detail);
              }
              const state = outcome && outcome.state;
              this.setStatus(
                state === "failed"
                  ? "Image applied; OCR follow-up failed"
                  : state === "cancelled"
                    ? "Image applied; OCR follow-up cancelled"
                    : "Image applied; OCR follow-up completed",
                state === "failed",
              );
            },
          });
      this.subscribeTransformResults =
        typeof options.subscribeTransformResults === "function"
          ? options.subscribeTransformResults
          : desktopCorrections && desktopCorrections.transforms &&
              typeof desktopCorrections.transforms.subscribeResults === "function"
            ? desktopCorrections.transforms.subscribeResults.bind(
              desktopCorrections.transforms)
            : null;
      let imageRendererOptions = {
        invokeCommand,
        initialTool: deps.TOOLS && deps.TOOLS.PERSPECTIVE,
        hasSelection: () => Boolean(
          this.state.selection.artifactId || this.state.selection.annotationId),
        clearSelection: () => this.clearResourceSelection(),
        onCommandError: (error) => this.setStatus(
          error && error.message || "The transform could not be queued", true),
        onQueueResult: (_result, command) => this.setStatus(
          command && command.adjustment
            ? "Image adjustment queued"
            : "Perspective transform queued"),
        onStateChange: (state) => {
          if (!this.classificationController ||
              typeof this.classificationController.setCanvasOwner !== "function") {
            return;
          }
          this.classificationController.setCanvasOwner(
            state && state.gesture
              ? {
                active: true,
                tool: state.tool,
                ownsKeyboard: true,
              }
              : null,
          );
        },
        onMount: (controller, resource) =>
          this.mountArtifactOverlay(controller, resource),
      };
      if (this.imageAdjustTool &&
          typeof deps.composeImageAdjustRendererOptions === "function") {
        imageRendererOptions = deps.composeImageAdjustRendererOptions(
          this.imageAdjustTool,
          imageRendererOptions,
        );
      }
      const imageOverlayRenderer = options.imageOverlayRenderer ||
        typeof deps.createPerspectiveImageRenderer === "function" &&
          deps.createPerspectiveImageRenderer(imageRendererOptions);
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
        normalizeTools: (value) => ({
          imageAdjust: typeof deps.normalizeImageAdjustProfile === "function"
            ? deps.normalizeImageAdjustProfile(
              isPlainObject(value) ? value.imageAdjust : null)
            : {},
          classification: normalizeClassificationProfile(
            isPlainObject(value) ? value.classification : null),
        }),
      });
      const profile = this.profileStore.load(this.profileKey);
      if (this.imageAdjustTool &&
          typeof this.imageAdjustTool.restoreProfile === "function") {
        this.imageAdjustTool.restoreProfile(profile.tools && profile.tools.imageAdjust);
      }
      this.editorRegistry.restoreChoices(profile.editors);
      this.layout = options.layoutController || new deps.LayoutController({
        root: this.root,
        documentRef: this.documentRef,
        windowRef: this.windowRef,
        initialState: profile.layout,
        onChange: () => this.persistProfile(),
      });
      this.classificationController = options.classificationController === false
        ? null
        : options.classificationController ||
          this.createClassificationFeature(options, profile);
      this.classificationControls = null;
      if (this.classificationController &&
          this.classificationController.registry &&
          typeof this.classificationController.registry.subscribe === "function") {
        this.unsubscribeClassificationBindings =
          this.classificationController.registry.subscribe((change) => {
            if (!this.restoringProfile && change.type === "remapped") {
              this.persistProfile();
            }
          });
      }
      this.restoreClassificationProfile(
        profile.tools && profile.tools.classification,
      );
      this.booksFeature = options.booksFeature === false ? null :
        options.booksFeature || this.createBooksFeature(options);
      this.artifactsFeature = options.artifactsFeature === false ? null :
        options.artifactsFeature || this.createArtifactsFeature(options);
    }

    createClassificationFeature(options, profile) {
      if (options.features === false ||
          typeof deps.createClassificationController !== "function") return null;
      const classification = profile && profile.tools &&
        profile.tools.classification || normalizeClassificationProfile(null);
      return deps.createClassificationController({
        scope: this.root,
        documentRef: this.documentRef,
        windowRef: this.windowRef,
        port: this.artifactPorts && this.artifactPorts.commands,
        bindings: classification.bindings,
        history: options.correctionHistory,
        operationIdFactory: options.correctionOperationIdFactory,
        isEventEligible: (event, command, context) =>
          this.classificationEventEligible(event, command, context),
        resolveLinkedArtifact: (_target, detail = {}) =>
          this.resolveLinkedArtifact(detail.linkedKey),
        refreshTarget: (target) => this.refreshClassificationTarget(target),
        promoteSoftTarget: (target) => this.promoteClassificationTarget(target),
        onChanged: (_result, detail) =>
          this.refreshClassificationTarget(detail && detail.target),
        onConflict: (error) => this.setStatus(
          error && error.message ||
            "The classification target changed; its latest revision was loaded",
          true,
        ),
        onStatus: (message, error) => this.setStatus(message, error),
        onError: (error) => this.setStatus(
          error && error.message || "The classification could not be applied",
          true,
        ),
      });
    }

    classificationEventEligible(event, _command, _context = {}) {
      return this.classificationSurfaceEligible(event, [
        "booksList",
        "artifactsTree",
        "editorHost",
        "classificationControls",
        "classificationToolbar",
      ]);
    }

    classificationContextMenuEligible(event) {
      return Boolean(this.classificationContextMenuTarget(event));
    }

    classificationContextMenuOwner(event) {
      let capture = null;
      let artifact = null;
      let overlay = null;
      let canvas = null;
      let node = event && event.target || null;
      while (node) {
        const dataset = node.dataset || {};
        if (!capture && dataset.itemId && dataset.artifactId) {
          capture = {
            kind: "book-capture",
            node,
            itemId: dataset.itemId,
            artifactId: dataset.artifactId,
          };
        }
        if (!artifact && dataset.artifactKey) {
          artifact = {
            kind: "artifact",
            node,
            key: dataset.artifactKey,
          };
        }
        if (!overlay && dataset.overlayKey) {
          overlay = {
            kind: "overlay",
            node,
            key: dataset.overlayKey,
          };
        }
        if (!canvas &&
            Object.prototype.hasOwnProperty.call(dataset, "classificationCanvas")) {
          canvas = { kind: "editor-canvas", node };
        }
        if (Object.prototype.hasOwnProperty.call(dataset, "booksList")) {
          return capture;
        }
        if (Object.prototype.hasOwnProperty.call(dataset, "artifactsTree")) {
          return artifact;
        }
        if (Object.prototype.hasOwnProperty.call(dataset, "editorHost")) {
          return overlay || canvas;
        }
        if (node === this.root) break;
        node = node.parentElement || node.parentNode || null;
      }
      return null;
    }

    classificationStateTarget(key) {
      const controller = this.classificationController;
      const snapshot = controller &&
        typeof controller.stateSnapshot === "function"
        ? controller.stateSnapshot() : null;
      if (!snapshot) return null;
      return [
        snapshot.selectionFocused && snapshot.selectionTarget,
        snapshot.selectionTarget,
        snapshot.hotTarget,
      ].find((target) => targetKey(target) === key) || null;
    }

    classificationContextMenuTarget(event) {
      const owner = this.classificationContextMenuOwner(event);
      if (!owner) return null;
      if (owner.kind === "book-capture") {
        const books = this.booksFeature &&
          (this.booksFeature.books || this.booksFeature);
        if (!books ||
            typeof books.commandTargetForSelection !== "function") return null;
        return books.commandTargetForSelection({
          itemId: owner.itemId,
          artifactId: owner.artifactId,
        });
      }
      if (owner.kind === "artifact") {
        return this.artifactsFeature && this.artifactsFeature.items &&
          this.artifactsFeature.items.get(owner.key) || null;
      }
      if (owner.kind === "overlay") {
        return this.classificationStateTarget(owner.key) ||
          this.artifactsFeature && this.artifactsFeature.items &&
            this.artifactsFeature.items.get(owner.key) || null;
      }
      const resource = this.state && this.state.resource;
      return resource && (resource.summary || resource) || null;
    }

    classificationSurfaceEligible(event, names) {
      const accepted = new Set(names);
      let node = event && event.target || null;
      while (node) {
        const dataset = node.dataset || {};
        for (const name of accepted) {
          if (Object.prototype.hasOwnProperty.call(dataset, name)) return true;
        }
        if (node === this.root) break;
        node = node.parentElement || node.parentNode || null;
      }
      return false;
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
        onSelectionTarget: (target, detail) => {
          if (this.classificationController &&
              typeof this.classificationController.setSelectionTarget === "function") {
            this.classificationController.setSelectionTarget(target, detail);
          }
        },
        onHotTarget: (target, detail) => {
          if (this.classificationController &&
              typeof this.classificationController.setHotTarget === "function") {
            this.classificationController.setHotTarget(target, detail);
          }
        },
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
          if (this.classificationController &&
              typeof this.classificationController.setSelectionTarget === "function") {
            this.classificationController.setSelectionTarget(item, {
              element: this.artifactTreeElement(targetKey(item)),
              focused: true,
              source: "artifacts",
            });
          }
          const address = artifactSelection(item, this.state.selection);
          if (address) this.selectAddress(address, { source: "artifacts" });
        },
        onHotTarget: (item) => {
          this.root.dataset.hotArtifactKey = item && item.key || "";
          if (this.classificationController &&
              typeof this.classificationController.setHotTarget === "function") {
            this.classificationController.setHotTarget(item, {
              element: this.artifactTreeElement(targetKey(item)),
              source: "artifacts",
            });
          }
        },
        onStatus: (message, error) => this.setStatus(message, error),
      });
    }

    artifactTreeElement(key) {
      if (!key) return null;
      const tree = this.root.querySelector("[data-artifacts-tree]");
      if (!tree || typeof tree.querySelectorAll !== "function") return null;
      return Array.from(tree.querySelectorAll("[data-artifact-key]"))
        .find((node) => node.dataset && node.dataset.artifactKey === key) || null;
    }

    async resolveLinkedArtifact(key) {
      if (!key || !String(key).startsWith("artifact:") ||
          !this.artifactsFeature) return null;
      const cached = this.artifactsFeature.items &&
        this.artifactsFeature.items.get(key);
      if (cached && cached.revision) return cached;
      if (typeof this.artifactsFeature.loadDetail !== "function") return null;
      try {
        return await this.artifactsFeature.loadDetail(key, { force: true });
      } catch (error) {
        this.setStatus(
          error && error.message || "The linked artifact could not be loaded",
          true,
        );
        return null;
      }
    }

    async refreshClassificationTarget(target) {
      const key = targetKey(target);
      let refreshed = null;
      if (key && this.artifactsFeature &&
          typeof this.artifactsFeature.reloadDetail === "function") {
        try {
          refreshed = await this.artifactsFeature.reloadDetail(key);
        } catch (error) {
          this.setStatus(
            error && error.message || "The latest artifact revision could not be loaded",
            true,
          );
        }
      }
      if (this.booksFeature && typeof this.booksFeature.refresh === "function") {
        try {
          await this.booksFeature.refresh("classification");
        } catch (error) {
          this.setStatus(
            error && error.message || "The Books panel could not be refreshed",
            true,
          );
        }
      }
      return refreshed || target || null;
    }

    async promoteClassificationTarget(target) {
      const key = targetKey(target);
      if (key && this.artifactsFeature) {
        let selected = null;
        if (this.artifactsFeature.items && this.artifactsFeature.items.has(key) &&
            typeof this.artifactsFeature.select === "function") {
          selected = await this.artifactsFeature.select(key, { focus: true });
        } else if (typeof this.artifactsFeature.openDeepLink === "function") {
          selected = await this.artifactsFeature.openDeepLink(key);
        }
        if (selected) return selected;
      }
      const address = artifactSelection(target, this.state.selection);
      if (address) this.selectAddress(address, { source: "classification" });
      return target;
    }

    mountArtifactOverlay(controller, resource) {
      if (!controller || !controller.image ||
          typeof deps.createArtifactOverlay !== "function") return null;
      const stage = controller.image.parentNode;
      if (!stage || typeof stage.append !== "function") return null;
      const summary = resource && resource.summary || {};
      const rawRegions = Array.isArray(resource && resource.regions)
        ? resource.regions : [];
      const regions = rawRegions.map((raw) => {
        const value = isPlainObject(raw) ? raw : {};
        const objectType = value.objectType || value.object_type ||
          value.type || "spatial-annotation";
        const id = value.annotationId || value.annotation_id || value.id || "";
        const normalized = {
          ...value,
          objectType,
          itemId: value.itemId || value.item_id || summary.itemId || "",
          id,
          revision: value.revision || value.annotationRevision ||
            value.annotation_revision || "",
        };
        return { ...normalized, key: targetKey(normalized) };
      });
      const dimensions = resource && resource.dimensions ||
        summary.dimensions || {};
      const overlay = deps.createArtifactOverlay({
        root: stage,
        documentRef: this.documentRef,
        ResizeObserver: this.windowRef && this.windowRef.ResizeObserver,
        getViewport: () => ({
          width: Number(stage.clientWidth) || Number(controller.image.clientWidth) || 1,
          height: Number(stage.clientHeight) || Number(controller.image.clientHeight) || 1,
        }),
        onSoftTarget: (target, detail) => {
          if (this.classificationController &&
              typeof this.classificationController.setHotTarget === "function") {
            this.classificationController.setHotTarget(target, {
              ...detail,
              source: "editor-overlay",
            });
          }
        },
        onFocusTarget: (target, detail) => {
          if (target && this.classificationController &&
              typeof this.classificationController.setSelectionTarget === "function") {
            this.classificationController.setSelectionTarget(target, {
              ...detail,
              focused: true,
              source: "editor-overlay",
            });
          } else if (!target) {
            this.demoteClassificationFocus();
          }
        },
        onActivate: (target) => {
          void this.promoteClassificationTarget(target);
        },
      }).mount();
      const sync = () => {
        const sourceWidth = Number(
          dimensions.width || dimensions.pixel_width ||
          controller.image.naturalWidth || controller.image.width) || 1;
        const sourceHeight = Number(
          dimensions.height || dimensions.pixel_height ||
          controller.image.naturalHeight || controller.image.height) || 1;
        const declaredOrientation = Number(
          dimensions.orientation || dimensions.exif_orientation ||
          summary.orientation || 1) || 1;
        const coordinatesAreOriented = rawRegions.some((region) => {
          const selector = region && (region.selector || region.polygon) || region;
          const coordinateSpace = String(
            selector && (selector.coordinate_space || selector.coordinateSpace) ||
            resource && resource.coordinateSpace || "").toLowerCase();
          return coordinateSpace.includes("canvas-normalized") ||
            coordinateSpace.includes("exif_oriented");
        });
        const orientation = coordinatesAreOriented ? 1 : declaredOrientation;
        overlay.setView({ sourceWidth, sourceHeight, orientation });
        overlay.setRegions(regions, {
          sourceWidth,
          sourceHeight,
          coordinateSpace: resource && resource.coordinateSpace,
        });
      };
      controller.image.addEventListener("load", sync);
      sync();
      return () => {
        controller.image.removeEventListener("load", sync);
        overlay.destroy();
      };
    }

    demoteClassificationFocus() {
      const controller = this.classificationController;
      if (!controller) return;
      if (typeof controller.setSelectionFocus === "function") {
        controller.setSelectionFocus(false);
        return;
      }
      if (typeof controller.stateSnapshot !== "function" ||
          typeof controller.setSelectionTarget !== "function") return;
      const snapshot = controller.stateSnapshot();
      if (!snapshot || !snapshot.selectionTarget) return;
      controller.setSelectionTarget(snapshot.selectionTarget, {
        focused: false,
        source: "editor-overlay-blur",
      });
    }

    mountClassificationControls() {
      if (!this.classificationController) return;
      if (typeof this.classificationController.mount === "function") {
        this.classificationController.mount();
      }
      const host = this.root.querySelector("[data-classification-controls]");
      const registry = this.classificationController.registry;
      const presenterReady = registry &&
        typeof registry.get === "function" &&
        typeof registry.subscribe === "function" &&
        typeof registry.bindingFor === "function" &&
        typeof registry.remap === "function" &&
        typeof registry.resetBinding === "function" &&
        typeof this.classificationController.bindControl === "function" &&
        typeof this.classificationController.invoke === "function";
      if (!host || !presenterReady ||
          typeof deps.createClassificationControls !== "function") return;
      this.classificationControls = deps.createClassificationControls({
        root: host,
        documentRef: this.documentRef,
        windowRef: this.windowRef,
        toolbarRoot: this.root.querySelector("[data-classification-toolbar]"),
        paletteTrigger: this.root.querySelector(
          "[data-classification-palette-trigger]"),
        contextScope: this.root,
        isContextMenuEvent: (event) =>
          this.classificationContextMenuTarget(event),
        controller: this.classificationController,
        onError: (error) => this.setStatus(
          error && error.message || "The classification command failed",
          true,
        ),
      }).mount();
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
      this.mountClassificationControls();
      this.listen(this.windowRef, "blur", () => {
        if (this.classificationController &&
            typeof this.classificationController.setScopeActive === "function") {
          this.classificationController.setScopeActive(false);
        }
      });
      this.listen(this.windowRef, "focus", () => {
        if (this.classificationController &&
            typeof this.classificationController.setScopeActive === "function") {
          this.classificationController.setScopeActive(true);
        }
      });
      if (this.booksFeature && typeof this.booksFeature.mount === "function") {
        this.booksFeature.mount();
      }
      if (this.artifactsFeature && typeof this.artifactsFeature.mount === "function") {
        this.artifactsFeature.mount();
      }
      this.connectTransformResults();
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

    connectTransformResults() {
      if (!this.imageAdjustTool || !this.subscribeTransformResults ||
          this.unsubscribeTransformResults) return;
      try {
        const release = this.subscribeTransformResults((result, command = null) => {
          if (this.destroyed ||
              typeof this.imageAdjustTool.observeTransformResult !== "function") return;
          this.imageAdjustTool.observeTransformResult(result, command);
        });
        if (typeof release === "function") this.unsubscribeTransformResults = release;
      } catch (error) {
        this.setStatus("Transform result updates are unavailable", true);
      }
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
      if (this.classificationController &&
          typeof this.classificationController.setHotTarget === "function") {
        this.classificationController.setHotTarget(null);
      }
      this.setResource(null);
      return selection;
    }

    clearSelection() {
      const selection = this.selectAddress(emptySelection(), {
        source: "selection-invalidated",
        forceContext: true,
      });
      if (this.classificationController) {
        if (typeof this.classificationController.setSelectionTarget === "function") {
          this.classificationController.setSelectionTarget(null);
        }
        if (typeof this.classificationController.setHotTarget === "function") {
          this.classificationController.setHotTarget(null);
        }
      }
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
        if (this.imageAdjustTool &&
            typeof this.imageAdjustTool.restoreProfile === "function") {
          this.imageAdjustTool.restoreProfile(null);
        }
        this.restoreClassificationProfile(null);
        this.refreshEditorSelector();
        this.renderEditor();
        this.persistProfile();
        this.setStatus("Layout, editor choices, and tool settings reset");
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
      if (this.imageAdjustTool &&
          typeof this.imageAdjustTool.restoreProfile === "function") {
        this.imageAdjustTool.restoreProfile(profile.tools && profile.tools.imageAdjust);
      }
      this.restoreClassificationProfile(
        profile.tools && profile.tools.classification,
      );
      this.refreshEditorSelector();
      this.renderEditor();
      this.updateProfileLabel();
    }

    restoreClassificationProfile(value) {
      const controller = this.classificationController;
      const registry = controller && controller.registry;
      if (!registry || typeof registry.get !== "function" ||
          typeof registry.remap !== "function") return;
      const profile = normalizeClassificationProfile(value);
      const definitions = Array.isArray(deps.DEFAULT_CLASSIFICATION_COMMANDS)
        ? deps.DEFAULT_CLASSIFICATION_COMMANDS : [];
      this.restoringProfile = true;
      try {
        for (const command of definitions) {
          if (!registry.get(command.id)) continue;
          registry.remap(command.id, "", { replaceConflicts: true });
        }
        for (const command of definitions) {
          if (!registry.get(command.id)) continue;
          registry.remap(command.id, profile.bindings[command.id] || "", {
            replaceConflicts: false,
          });
        }
      } finally {
        this.restoringProfile = false;
      }
    }

    persistProfile() {
      if (!this.layout || !this.editorRegistry) return;
      this.profileStore.save(this.profileKey, {
        layout: this.layout.getState(),
        editors: this.editorRegistry.serializeChoices(),
        tools: {
          imageAdjust: this.imageAdjustTool &&
              typeof this.imageAdjustTool.serializeProfile === "function"
            ? this.imageAdjustTool.serializeProfile()
            : {},
          classification: classificationProfile(this.classificationController),
        },
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
      if (typeof this.unsubscribeTransformResults === "function") {
        this.unsubscribeTransformResults();
      }
      this.unsubscribeTransformResults = null;
      if (typeof this.unsubscribeClassificationBindings === "function") {
        this.unsubscribeClassificationBindings();
      }
      this.unsubscribeClassificationBindings = null;
      if (this.classificationControls &&
          typeof this.classificationControls.destroy === "function") {
        this.classificationControls.destroy();
      }
      this.classificationControls = null;
      if (this.classificationController &&
          typeof this.classificationController.destroy === "function") {
        this.classificationController.destroy();
      }
      this.classificationController = null;
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
      if (this.imageAdjustTool &&
          typeof this.imageAdjustTool.destroy === "function") {
        this.imageAdjustTool.destroy();
      }
      this.imageAdjustTool = null;
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
    correctionsRuntimePorts,
    installAutoBoot,
    nextTrayTab,
    normalizeSelection,
    normalizeWorkbenchContext,
    selectionContext,
  };
});

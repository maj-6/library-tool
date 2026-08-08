(function installCorrectionsShell(root, factory) {
  const dependencies = typeof module === "object" && module.exports ? {
    ...require("./editor-registry"),
    ...require("./ui-profile"),
    ...require("./layout-controller"),
    ...require("./reviews"),
    ...require("./artifacts"),
    ...require("./item-properties"),
    ...require("./engine-adapter"),
    ...require("./commands"),
    ...require("./keymap"),
    ...require("./artifact-overlay"),
    ...require("./classification-controls"),
    // image-editor-state supplies TOOLS; without it the Node tests resolve
    // initialTool to undefined and exercise a different default tool than
    // the browser bundle does.
    ...require("./image-editor-state"),
    ...require("./image-editor"),
    ...require("./image-adjust-tool"),
    ...require("./preset-panel"),
    ...require("./ocr-proposals"),
    ...require("./ch-panel"),
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
  // The command registry's binding grammar accepts single letters only, so
  // the bracket-style previous/next keys land on j/k (both unclaimed by the
  // default classification bindings t/c/s/e/m/i/n/p/d).
  const BOOKS_NAVIGATION_COMMANDS = Object.freeze([
    Object.freeze({
      id: "corrections.books.previous-item",
      label: "Select the previous Books item",
      shortLabel: "Previous item",
      code: "PRV",
      defaultBinding: "k",
      targetKind: "books-item",
      direction: -1,
    }),
    Object.freeze({
      id: "corrections.books.next-item",
      label: "Select the next Books item",
      shortLabel: "Next item",
      code: "NXT",
      defaultBinding: "j",
      targetKind: "books-item",
      direction: 1,
    }),
  ]);
  const BOOKS_NAVIGATION_IDS = new Set(
    BOOKS_NAVIGATION_COMMANDS.map((command) => command.id));
  const PRESET_BATCH_PAGE_LIMIT = 100;
  const PRESET_BATCH_MAX_PAGES = 64;
  const PRESET_BATCH_MAX_TARGETS = 4096;
  const PRESET_BATCH_RETRY_LIMIT = 4096;
  const PRESET_BATCH_CONTEXT_CHANGED = "preset-batch-context-changed";

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

  function processingPresetFingerprint(preset) {
    const revision = preset && preset.revision;
    if (typeof revision !== "string" || !/^[0-9a-f]{64}$/.test(revision)) {
      throw new TypeError(
        "batch apply requires a content-revisioned processing preset");
    }
    return revision;
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
    // The Books previous/next keys are fixed, so a stored classification
    // remap can never claim them; the colliding remap is dropped instead.
    const occupied = new Set(
      BOOKS_NAVIGATION_COMMANDS.map((command) => command.defaultBinding));
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

  function navigationOnlyTarget(value) {
    if (!value || typeof value !== "object") return false;
    const revision = value.revision || value.artifactRevision ||
      value.artifact_revision || value.annotationRevision ||
      value.annotation_revision || "";
    return String(revision).startsWith("index:");
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

  function correctionsRuntimePorts(
    windowRef, desktopCorrections, documentRef = null,
  ) {
    if (desktopCorrections || !windowRef || !windowRef.engineClient ||
        typeof deps.createCorrectionsEnginePorts !== "function") {
      return null;
    }
    return deps.createCorrectionsEnginePorts(windowRef.engineClient, {
      indexPolling: {
        lifecycle: documentRef || windowRef.document || null,
        schedule: typeof windowRef.setTimeout === "function"
          ? windowRef.setTimeout.bind(windowRef) : undefined,
        cancelSchedule: typeof windowRef.clearTimeout === "function"
          ? windowRef.clearTimeout.bind(windowRef) : undefined,
      },
    });
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
      this.externalRefreshPromise = null;
      this.restoringProfile = false;
      this.destroyed = false;
      this.activeTrayTab = "reviews";
      const desktopCorrections = this.desktop && this.desktop.corrections || null;
      this.engineCorrections = correctionsRuntimePorts(
        this.windowRef, desktopCorrections, this.documentRef);
      this.processingPresets = Object.prototype.hasOwnProperty.call(
        options, "processingPresets",
      ) ? options.processingPresets
        : desktopCorrections && desktopCorrections.processingPresets ||
          this.engineCorrections && this.engineCorrections.processingPresets ||
          this.windowRef && this.windowRef.engineClient &&
            this.windowRef.engineClient.processingPresets || null;
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
      this.invokeCommand = invokeCommand;
      this.presetBatchOperationIdFactory =
        typeof options.presetBatchOperationIdFactory === "function"
          ? options.presetBatchOperationIdFactory : null;
      this.presetBatchSequence = 0;
      this.presetBatchRetryCommands = new Map();
      this.presetBatchRuns = new Map();
      const imageAdjustOptions = isPlainObject(options.imageAdjustOptions)
        ? options.imageAdjustOptions : {};
      const presetPanelFactory = typeof imageAdjustOptions.createPresetPanel === "function"
        ? imageAdjustOptions.createPresetPanel : deps.createPresetPanel;
      const presetPort = Object.prototype.hasOwnProperty.call(
        imageAdjustOptions, "presets",
      ) ? imageAdjustOptions.presets : this.processingPresets;
      const internalBatchHandler = invokeCommand && this.engineCorrections &&
          this.engineCorrections.artifacts &&
          this.engineCorrections.artifacts.catalog &&
          typeof this.engineCorrections.artifacts.catalog.list === "function" &&
          typeof this.engineCorrections.artifacts.catalog.get === "function"
        ? (preset, controller) =>
            this.batchApplyProcessingPreset(preset, controller)
        : null;
      const presetBatchHandler =
        typeof imageAdjustOptions.onPresetBatchApply === "function"
          ? imageAdjustOptions.onPresetBatchApply : internalBatchHandler;
      this.imageAdjustTool = options.imageAdjustTool ||
        typeof deps.createImageAdjustTool === "function" &&
          deps.createImageAdjustTool({
            requestReocr: (request, detail) =>
              this.queueStandaloneReocr(request, detail),
            ...imageAdjustOptions,
            createPresetPanel: presetPanelFactory,
            presets: presetPort,
            onPresetBatchApply: presetBatchHandler,
            profile: null,
            onProfileChange: (value, detail) => {
              if (typeof imageAdjustOptions.onProfileChange === "function") {
                imageAdjustOptions.onProfileChange(value, detail);
              }
              this.persistProfile({
                toolUpdates: { imageAdjust: value },
              });
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
            : this.engineCorrections && this.engineCorrections.transforms &&
                typeof this.engineCorrections.transforms.subscribeResults === "function"
              ? this.engineCorrections.transforms.subscribeResults.bind(
                this.engineCorrections.transforms)
              : null;
      let imageRendererOptions = {
        invokeCommand,
        // Select, not Perspective: the perspective/image-adjust surfaces
        // deliberately mute overlay pointer events (classification.css), so
        // opening in one of those tools left region boxes hover- and
        // click-dead until the user discovered the tool switcher.
        initialTool: deps.TOOLS && deps.TOOLS.SELECT,
        hasSelection: () => Boolean(
          this.state.selection.artifactId || this.state.selection.annotationId),
        clearSelection: () => this.clearResourceSelection(),
        onCommandError: (error) => this.setStatus(
          error && error.message || "The transform could not be queued", true),
        onQueueResult: (_result, command) => this.setStatus(
          command && (command.adjustment || command.operations)
            ? "Image processing queued"
            : "Perspective transform queued"),
        onStateChange: (state) => {
          if (!this.classificationController ||
              typeof this.classificationController.setCanvasOwner !== "function") {
            return;
          }
          this.classificationController.setCanvasOwner(
            state && (state.gesture || state.maskDraft)
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
              this.persistProfile({
                toolUpdates: {
                  classification: classificationProfile(
                    this.classificationController),
                },
              });
            }
          });
      }
      this.restoreClassificationProfile(
        profile.tools && profile.tools.classification,
      );
      this.booksFeature = options.booksFeature === false ? null :
        options.booksFeature || this.createBooksFeature(options);
      this.registerBooksNavigationCommands();
      this.artifactsFeature = options.artifactsFeature === false ? null :
        options.artifactsFeature || this.createArtifactsFeature(options);
      this.itemProperties = options.itemProperties === false ? null :
        options.itemProperties || this.createItemPropertiesFeature(options);
      this.capabilitiesPromise = null;
      this.ocrProposalsFeature = options.ocrProposalsFeature === false ? null :
        options.ocrProposalsFeature || this.createOcrProposalsFeature(options);
      this.chPanelFeature = options.chPanelFeature === false ? null :
        options.chPanelFeature || this.createChPanelFeature(options);
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
        transformContract: (target) =>
          this.classificationTransformContract(target),
        serializeTransformCommand: (value) =>
          typeof deps.serializeCorrectionTransformCommand === "function"
            ? deps.serializeCorrectionTransformCommand(value)
            : null,
        refreshTarget: (target, detail) =>
          this.refreshClassificationTarget(target, detail),
        promoteSoftTarget: (target) => this.promoteClassificationTarget(target),
        onChanged: (_result, detail) => detail && detail.refreshAttempted
          ? null : this.refreshClassificationTarget(detail && detail.target, detail),
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

    classificationEventEligible(event, command, context = {}) {
      // Books navigation acts on the panel, not the hovered target, so a
      // hover must not widen its gate beyond the browsable surfaces.
      if (!(command && BOOKS_NAVIGATION_IDS.has(command.id)) &&
          this.classificationHoverEligible(command, context)) return true;
      return this.classificationSurfaceEligible(event, [
        "booksList",
        "booksViewBar",
        "artifactsTree",
        "editorHost",
        "classificationControls",
        "classificationToolbar",
      ]);
    }

    booksPanel() {
      const feature = this.booksFeature;
      const books = feature && (feature.books || feature);
      return books && typeof books.stepSelection === "function" ? books : null;
    }

    registerBooksNavigationCommands() {
      const registry = this.classificationController &&
        this.classificationController.registry;
      if (!registry || typeof registry.register !== "function" ||
          !this.booksPanel()) return;
      for (const definition of BOOKS_NAVIGATION_COMMANDS) {
        if (typeof registry.get === "function" && registry.get(definition.id)) {
          continue;
        }
        const command = {
          ...definition,
          available: () => {
            const books = this.booksPanel();
            return !!books &&
              typeof books.canStepSelection === "function" &&
              books.canStepSelection(definition.direction);
          },
          execute: () => {
            const books = this.booksPanel();
            if (!books) throw new Error("The Books panel is unavailable");
            return books.stepSelection(definition.direction);
          },
        };
        try {
          registry.register(command);
        } catch (error) {
          if (!error || error.code !== "key_binding_conflict") throw error;
          // An injected registry already claimed this key; keep the command
          // reachable through buttons and the palette without a shortcut.
          registry.register({ ...command, defaultBinding: "" });
        }
      }
    }

    // A hovered target keeps its hotkeys wherever document focus sits;
    // typing surfaces and dialogs are still rejected by the keymap's own
    // gates before this predicate runs.
    classificationHoverEligible(command, context = {}) {
      const soft = context && context.softTarget || null;
      if (!soft) return false;
      if (!command || typeof deps.resolveClassificationTarget !== "function") {
        return true;
      }
      return Boolean(deps.resolveClassificationTarget({ softTarget: soft }, command));
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

    // Books publishes a lean capture row: authoritative identity and revision,
    // but no metadata assertions. The archive command reads those assertions
    // to decide whether its key archives or restores, so a Books-published
    // target would always look unarchived and the toggle would never restore.
    // Top the target up from the artifacts feature's decoded item for the same
    // key: the panel's own target stays a pure navigation record, and every
    // surface that publishes a target gets the same answer.
    classificationTargetMetadata(target) {
      if (!target || typeof target !== "object") return target;
      if (Array.isArray(target.metadataAssertions) ||
          Array.isArray(target.metadata_assertions)) return target;
      const key = targetKey(target);
      const item = key && this.artifactsFeature && this.artifactsFeature.items &&
        this.artifactsFeature.items.get(key) || null;
      if (!item || item === target ||
          !Array.isArray(item.metadataAssertions)) return target;
      return Object.freeze({
        ...target,
        metadataAssertions: item.metadataAssertions,
      });
    }

    publishClassificationSelectionTarget(value, detail = {}) {
      const target = this.classificationTargetMetadata(value);
      const controller = this.classificationController;
      if (!controller ||
          typeof controller.setSelectionTarget !== "function") return null;
      if (!target && detail.address &&
          typeof controller.stateSnapshot === "function") {
        const current = controller.stateSnapshot().selectionTarget;
        const address = detail.address;
        const currentItemId = current &&
          (current.itemId || current.item_id || "");
        const addressItemId = address.itemId || address.item_id || "";
        const addressArtifactId = address.artifactId || address.artifact_id || "";
        const addressAnnotationId = address.annotationId ||
          address.annotation_id || "";
        const addressKey = addressAnnotationId
          ? `annotation:${addressAnnotationId}`
          : addressArtifactId ? `artifact:${addressArtifactId}` : "";
        if (current && !navigationOnlyTarget(current) &&
            addressKey && currentItemId === addressItemId &&
            targetKey(current) === addressKey) {
          // Books owns only capture rows. Its immediate or delayed null echo
          // cannot overwrite authoritative detail for the same selected
          // artifact or annotation, including targets absent from its index.
          return current;
        }
      }
      return controller.setSelectionTarget(target, detail);
    }

    classificationContextMenuTarget(event) {
      const owner = this.classificationContextMenuOwner(event);
      if (!owner) return null;
      if (owner.kind === "book-capture") {
        const books = this.booksFeature &&
          (this.booksFeature.books || this.booksFeature);
        if (!books ||
            typeof books.commandTargetForSelection !== "function") return null;
        return this.classificationTargetMetadata(books.commandTargetForSelection({
          itemId: owner.itemId,
          artifactId: owner.artifactId,
        }));
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
          this.publishClassificationSelectionTarget(target, detail);
        },
        onHotTarget: (target, detail) => {
          if (this.classificationController &&
              typeof this.classificationController.setHotTarget === "function") {
            this.classificationController.setHotTarget(
              this.classificationTargetMetadata(target), detail);
          }
        },
        onSelectionInvalidated: () => this.clearSelection(),
        onExternalChange: () =>
          this.refreshExternalState("external-change", {
            includeBooks: false,
          }),
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
          "source-images",
        ],
        onResource: (resource) => this.setResource(resource),
        onSelection: (item) => this.selectArtifactItem(item),
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

    createItemPropertiesFeature(options) {
      if (options.features === false ||
          typeof deps.createItemMetadataEditor !== "function") return null;
      const propertiesRoot = this.root.querySelector("[data-item-properties]");
      if (!propertiesRoot) return null;
      let api = options.itemMetadataApi || null;
      if (!api && typeof deps.createCorrectionsItemApi === "function") {
        const fetchImpl = typeof options.fetchImpl === "function"
          ? options.fetchImpl
          : this.windowRef && typeof this.windowRef.fetch === "function"
            ? this.windowRef.fetch.bind(this.windowRef)
            : typeof fetch === "function" ? fetch.bind(globalThis) : null;
        if (fetchImpl) api = deps.createCorrectionsItemApi({ fetchImpl });
      }
      if (!api) return null;
      return deps.createItemMetadataEditor({
        root: propertiesRoot,
        documentRef: this.documentRef,
        api,
        draftStore: this.state,
        operationIdFactory: options.itemMetadataOperationIdFactory,
        onChanged: () => {
          if (this.chPanelFeature &&
              typeof this.chPanelFeature.refresh === "function") {
            void Promise.resolve(this.chPanelFeature.refresh("metadata"))
              .catch((error) => this.setStatus(
                error && error.message ||
                  "Metadata saved, but the CH panel could not be refreshed",
                true,
              ));
          }
          if (!this.booksFeature ||
              typeof this.booksFeature.refresh !== "function") return;
          void Promise.resolve(this.booksFeature.refresh("metadata"))
            .catch((error) => this.setStatus(
              error && error.message ||
                "Metadata saved, but the Books panel could not be refreshed",
              true,
            ));
        },
        onStatus: (message, error) => this.setStatus(message, error),
      });
    }

    createOcrProposalsFeature(options) {
      if (options.features === false ||
          typeof deps.createOcrProposalsPanel !== "function") return null;
      const host = this.root.querySelector("[data-ocr-proposals]");
      const port = options.ocrProposalsPort ||
        this.engineCorrections && this.engineCorrections.ocrProposals || null;
      if (!host || !port) return null;
      return deps.createOcrProposalsPanel({
        root: host,
        documentRef: this.documentRef,
        port,
        operationIdFactory: options.ocrProposalOperationIdFactory,
        onStatus: (message, error) => this.setStatus(message, error),
      });
    }

    createChPanelFeature(options) {
      if (options.features === false ||
          typeof deps.createChPanel !== "function") return null;
      const host = this.root.querySelector("[data-ch-panel]");
      if (!host) return null;
      const fetchImpl = typeof options.fetchImpl === "function"
        ? options.fetchImpl
        : this.windowRef && typeof this.windowRef.fetch === "function"
          ? this.windowRef.fetch.bind(this.windowRef)
          : typeof fetch === "function" ? fetch.bind(globalThis) : null;
      if (!fetchImpl) return null;
      return deps.createChPanel({
        root: host,
        documentRef: this.documentRef,
        fetchImpl,
        operationIdFactory: options.chOperationIdFactory,
        onChanged: () => this.refreshChMergeTargets(),
      });
    }

    // A CH approval or rejection rewrites the item's metadata server-side, so
    // the panels showing that metadata must reload their copies.
    refreshChMergeTargets() {
      if (this.itemProperties &&
          typeof this.itemProperties.refresh === "function") {
        void Promise.resolve(this.itemProperties.refresh("ch-reconcile"))
          .catch((error) => this.setStatus(
            error && error.message ||
              "CH decision saved, but item metadata could not be refreshed",
            true,
          ));
      }
      if (this.booksFeature &&
          typeof this.booksFeature.refresh === "function") {
        void Promise.resolve(this.booksFeature.refresh("ch-reconcile"))
          .catch((error) => this.setStatus(
            error && error.message ||
              "CH decision saved, but the Books panel could not be refreshed",
            true,
          ));
      }
    }

    // The button and panel stay hidden until engine discovery proves the
    // corrections workbench enhancements are actually installed.
    loadWorkbenchCapabilities() {
      if (this.capabilitiesPromise) return this.capabilitiesPromise;
      const client = this.windowRef && this.windowRef.engineClient;
      if (!client || typeof client.capabilities !== "function") {
        return Promise.resolve(null);
      }
      this.capabilitiesPromise = Promise.resolve()
        .then(() => client.capabilities())
        .then((discovery) => {
          if (this.destroyed) return null;
          const rows = discovery && Array.isArray(discovery.capabilities)
            ? discovery.capabilities : [];
          const ids = new Set(rows
            .map((row) => row && row.id)
            .filter((id) => typeof id === "string"));
          if (this.imageAdjustTool &&
              typeof this.imageAdjustTool.setReocrCapability === "function") {
            this.imageAdjustTool.setReocrCapability(
              ids.has("library.corrections.reocr.queue"));
          }
          if (this.ocrProposalsFeature &&
              typeof this.ocrProposalsFeature.setCapabilities === "function") {
            void this.ocrProposalsFeature.setCapabilities({
              read: ids.has("library.corrections.ocr-proposals.read"),
              queue: ids.has("library.corrections.reocr.queue"),
            });
          }
          return ids;
        })
        .catch(() => null);
      return this.capabilitiesPromise;
    }

    async queueStandaloneReocr(request = {}) {
      const port = this.engineCorrections &&
        this.engineCorrections.ocrProposals;
      if (!port || typeof port.queueReocr !== "function") {
        throw new Error("Re-OCR is unavailable in this window");
      }
      const receipt = await port.queueReocr({
        operationId: request.operationId,
        itemId: request.itemId,
        artifactId: request.artifactId,
        expectedArtifactRevision: request.expectedArtifactRevision,
      });
      this.setStatus(receipt.replayed === true
        ? "Re-OCR already queued" : "Re-OCR queued");
      return receipt;
    }

    nextPresetBatchOperationId(preset, capture) {
      this.presetBatchSequence += 1;
      if (this.presetBatchOperationIdFactory) {
        return this.presetBatchOperationIdFactory({
          preset,
          capture,
          sequence: this.presetBatchSequence,
        });
      }
      const cryptoRef = this.windowRef && this.windowRef.crypto;
      if (cryptoRef && typeof cryptoRef.randomUUID === "function") {
        return `preset:${cryptoRef.randomUUID()}`;
      }
      return `preset:${Date.now().toString(36)}:` +
        this.presetBatchSequence.toString(36);
    }

    presetBatchContextError(message =
      "The Corrections context changed; the preset batch was cancelled") {
      const error = new Error(message);
      error.code = PRESET_BATCH_CONTEXT_CHANGED;
      return error;
    }

    createPresetBatchBinding(preset, controller) {
      if (!preset || typeof preset.category !== "string" || !preset.category) {
        throw new TypeError("Batch apply requires a categorized processing preset");
      }
      if (!controller || !controller.resource ||
          typeof deps.correctionResourceContract !== "function") {
        throw new Error("Batch apply requires the active image editor");
      }
      const editorContract = deps.correctionResourceContract(
        controller.resource);
      const editorPins = editorContract && editorContract.pins;
      const editorItemId = editorPins && editorPins.item_id;
      if (!editorItemId || typeof editorPins.artifact_id !== "string" ||
          !editorPins.artifact_id ||
          typeof editorPins.artifact_revision !== "string" ||
          !editorPins.artifact_revision ||
          typeof editorPins.source_revision !== "string" ||
          !editorPins.source_revision ||
          typeof editorPins.source_sha256 !== "string" ||
          !/^[0-9a-f]{64}$/.test(editorPins.source_sha256)) {
        throw new Error("The active image has no authoritative source pins");
      }
      const state = this.state;
      const itemId = state && state.selection && state.selection.itemId;
      if (!itemId) {
        throw new Error("Select a book before applying a preset in batch");
      }
      if (itemId !== editorItemId) {
        throw this.presetBatchContextError(
          "The active image belongs to a different book; nothing was queued");
      }
      const context = normalizeWorkbenchContext(state && state.context);
      const contextSignature = JSON.stringify(context);
      const presetFingerprint = processingPresetFingerprint(preset);
      // Catalog enumeration is deliberately book-scoped. Carrying the
      // selected representation/canvas would make the engine return only a
      // slice of source-images and break the panel's "All" promise.
      const catalogContext = {
        ...context,
        itemId,
        item_id: itemId,
        workspaceId: context.workspace_id,
      };
      for (const field of [
        "representation_id", "canvas_id", "artifact_id", "annotation_id",
        "resource_revision", "representationId", "canvasId", "artifactId",
        "annotationId", "resourceRevision",
      ]) delete catalogContext[field];
      const generation = Number.isSafeInteger(this.contextGeneration)
        ? this.contextGeneration : null;
      const runKey = JSON.stringify({
        workspace_id: context.workspace_id,
        item_id: itemId,
        context: contextSignature,
        preset: presetFingerprint,
      });
      return Object.freeze({
        catalogContext: Object.freeze(catalogContext),
        contextGeneration: generation,
        contextSignature,
        editorArtifactId: editorPins.artifact_id,
        itemId,
        presetFingerprint,
        runKey,
        workspaceId: context.workspace_id,
      });
    }

    assertPresetBatchBinding(binding) {
      if (this.destroyed) throw this.presetBatchContextError(
        "The Corrections window closed; the preset batch was cancelled");
      const state = this.state;
      const itemId = state && state.selection && state.selection.itemId;
      let signature = "";
      try {
        signature = JSON.stringify(normalizeWorkbenchContext(
          state && state.context));
      } catch (error) {
        throw this.presetBatchContextError();
      }
      if (itemId !== binding.itemId || signature !== binding.contextSignature ||
          binding.contextGeneration !== null &&
            this.contextGeneration !== binding.contextGeneration) {
        throw this.presetBatchContextError();
      }
      const store = this.booksFeature && this.booksFeature.store;
      const snapshot = store && typeof store.snapshot === "function"
        ? store.snapshot() : null;
      if (snapshot && snapshot.workspaceId != null &&
          snapshot.workspaceId !== binding.workspaceId) {
        throw this.presetBatchContextError(
          "The Corrections workspace changed; the preset batch was cancelled");
      }
      return snapshot;
    }

    async freshPresetBatchIndex(binding) {
      const feature = this.booksFeature;
      if (!feature || !feature.store ||
          typeof feature.store.snapshot !== "function" ||
          typeof feature.refresh !== "function") {
        throw new Error("The current book inventory is unavailable");
      }
      const refreshed = await feature.refresh("preset-batch");
      this.assertPresetBatchBinding(binding);
      const snapshot = feature.store.snapshot();
      const books = snapshot && snapshot.index && snapshot.index.books;
      const freshRevision = refreshed && refreshed.revision;
      const snapshotRevision = snapshot && snapshot.index &&
        snapshot.index.revision;
      if (!refreshed || !snapshot || snapshot.status !== "ready" ||
          snapshot.workspaceId !== binding.workspaceId ||
          !snapshot.index || !Array.isArray(books) ||
          freshRevision && snapshotRevision !== freshRevision) {
        throw new Error(
          "The latest book inventory could not be loaded; nothing was queued");
      }
      if (!books.some((candidate) => candidate.id === binding.itemId)) {
        throw this.presetBatchContextError(
          "The selected book changed; the preset batch was cancelled");
      }
      return snapshot.index;
    }

    presetBatchArtifactId(value) {
      const key = value && value.key;
      const id = value && (
        value.artifact_id || value.artifactId || value.id ||
        key && (key.artifact_id || key.artifactId)
      );
      return contextIdentifier(id, "preset batch artifact id", true);
    }

    async listPresetBatchTargets(binding) {
      const catalog = this.engineCorrections.artifacts.catalog;
      const targets = [];
      const artifactIds = new Set();
      const cursors = new Set();
      let cursor = null;
      let inventoryRevision = "";
      for (let pageNumber = 0;
        pageNumber < PRESET_BATCH_MAX_PAGES;
        pageNumber += 1) {
        this.assertPresetBatchBinding(binding);
        const page = await catalog.list({
          context: binding.catalogContext,
          group: "source-images",
          cursor,
          limit: PRESET_BATCH_PAGE_LIMIT,
        });
        this.assertPresetBatchBinding(binding);
        if (!isPlainObject(page) || !Array.isArray(page.items) ||
            page.items.length > PRESET_BATCH_PAGE_LIMIT) {
          throw new Error("The source image inventory returned an invalid page");
        }
        const pageRevision = typeof page.revision === "string"
          ? page.revision : "";
        if (pageRevision) {
          if (inventoryRevision && pageRevision !== inventoryRevision) {
            throw new Error(
              "The source image inventory changed while it was being read");
          }
          inventoryRevision = pageRevision;
        }
        for (const target of page.items) {
          const artifactId = this.presetBatchArtifactId(target);
          if (artifactIds.has(artifactId)) continue;
          if (targets.length >= PRESET_BATCH_MAX_TARGETS) {
            throw new Error("The source image inventory is too large for batch apply");
          }
          artifactIds.add(artifactId);
          targets.push(Object.freeze({ artifactId, target }));
        }
        const next = page.nextCursor != null
          ? page.nextCursor : page.next_cursor;
        if (next == null || next === "") return Object.freeze(targets);
        if (typeof next !== "string" || next.length > 1024 ||
            /[\u0000-\u001f]/.test(next) || cursors.has(next) || next === cursor) {
          throw new Error("The source image inventory returned an invalid cursor");
        }
        if (!page.items.length) {
          throw new Error(
            "The source image inventory returned an empty continuation page");
        }
        cursors.add(next);
        cursor = next;
      }
      throw new Error("The source image inventory exceeded its page limit");
    }

    presetBatchResource(value) {
      return value && (value.item || value.artifact || value.detail || value);
    }

    presetBatchResourceCategory(resource) {
      const summary = resource && resource.summary;
      const value = resource && (
        resource.effective_category || resource.effectiveCategory
      ) || summary && (
        summary.effective_category || summary.effectiveCategory
      );
      return typeof value === "string" ? value : "";
    }

    presetBatchResourceAvailable(resource) {
      const summary = resource && resource.summary;
      const value = resource && (
        resource.resource_state || resource.resourceState
      ) || summary && (
        summary.resource_state || summary.resourceState
      );
      const correction = resource && resource.correction ||
        summary && summary.correction;
      return value === "available" && Boolean(correction);
    }

    presetBatchRetryKey(binding, artifactId) {
      return JSON.stringify({
        artifact_id: artifactId,
        item_id: binding.itemId,
        preset: binding.presetFingerprint,
        workspace_id: binding.workspaceId,
      });
    }

    presetBatchRetryCache() {
      if (!(this.presetBatchRetryCommands instanceof Map)) {
        this.presetBatchRetryCommands = new Map();
      }
      return this.presetBatchRetryCommands;
    }

    async runProcessingPresetBatch(preset, binding) {
      const catalog = this.engineCorrections &&
        this.engineCorrections.artifacts &&
        this.engineCorrections.artifacts.catalog;
      if (!this.invokeCommand || !catalog ||
          typeof catalog.list !== "function" ||
          typeof catalog.get !== "function" ||
          typeof deps.correctionResourceContract !== "function" ||
          typeof deps.serializeProcessingPresetCommand !== "function") {
        throw new Error("Batch image processing is unavailable in this window");
      }

      await this.freshPresetBatchIndex(binding);
      const targets = await this.listPresetBatchTargets(binding);
      let queued = 0;
      let failed = 0;
      const operationIds = new Set();
      const retryCommands = this.presetBatchRetryCache();
      for (const { artifactId, target } of targets) {
        this.assertPresetBatchBinding(binding);
        const retryKey = this.presetBatchRetryKey(binding, artifactId);
        let entry = retryCommands.get(retryKey) || null;
        try {
          if (!entry) {
            let resource;
            try {
              resource = this.presetBatchResource(await catalog.get({
                context: binding.catalogContext,
                key: `artifact:${artifactId}`,
              }));
              this.assertPresetBatchBinding(binding);
            } catch (error) {
              if (error && error.code === PRESET_BATCH_CONTEXT_CHANGED) {
                throw error;
              }
              failed += 1;
              continue;
            }
            if (this.presetBatchResourceCategory(resource) !== preset.category) {
              continue;
            }
            if (!this.presetBatchResourceAvailable(resource)) {
              failed += 1;
              continue;
            }
            if (retryCommands.size >= PRESET_BATCH_RETRY_LIMIT) {
              throw new Error(
                "Too many uncertain preset commands are awaiting retry");
            }
            const operationId = this.nextPresetBatchOperationId(
              preset, target);
            const command = deps.serializeProcessingPresetCommand({
              resource,
              preset,
              operationId,
            });
            entry = Object.freeze({
              artifactId,
              command,
              resource,
            });
            retryCommands.set(retryKey, entry);
          }
          const command = entry.command;
          if (operationIds.has(command.operation_id)) {
            throw new Error("Batch operation identifiers must be unique");
          }
          operationIds.add(command.operation_id);
          try {
            await this.invokeCommand("corrections.transform.queue", {
              command,
              trigger: "preset-batch",
              resource: entry.resource,
            });
            retryCommands.delete(retryKey);
            queued += 1;
          } catch (error) {
            if (!(error && (error.retryable === true ||
                error.ambiguous === true))) {
              retryCommands.delete(retryKey);
            }
            failed += 1;
          }
          this.assertPresetBatchBinding(binding);
        } catch (error) {
          if (error && error.code === PRESET_BATCH_CONTEXT_CHANGED) throw error;
          failed += 1;
        }
      }
      return Object.freeze({ queued, failed });
    }

    async batchApplyProcessingPreset(preset, controller) {
      const binding = this.createPresetBatchBinding(preset, controller);
      if (!(this.presetBatchRuns instanceof Map)) {
        this.presetBatchRuns = new Map();
      }
      const current = this.presetBatchRuns.get(binding.runKey);
      if (current) return current;
      const run = this.runProcessingPresetBatch(preset, binding);
      this.presetBatchRuns.set(binding.runKey, run);
      try {
        return await run;
      } finally {
        if (this.presetBatchRuns.get(binding.runKey) === run) {
          this.presetBatchRuns.delete(binding.runKey);
        }
      }
    }

    selectArtifactItem(item) {
      // Synchronize cross-panel navigation first. Books can legitimately
      // publish null when this target is not a capture row. Publishing the
      // artifact second makes authoritative detail the final command target,
      // while an index hint still finishes as deliberately unavailable.
      const address = artifactSelection(item, this.state.selection);
      if (address) this.selectAddress(address, { source: "artifacts" });
      const target = navigationOnlyTarget(item) ? null : item;
      this.publishClassificationSelectionTarget(target, {
        element: this.artifactTreeElement(targetKey(item)),
        focused: true,
        source: "artifacts",
      });
      return target;
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

    classificationTransformContract(target) {
      const resource = this.state && this.state.resource;
      const correction = resource && resource.correction;
      if (!correction || !correction.artifact_id) return null;
      // Extraction crops the image currently open in the editor; accept the
      // request only when the hovered region is linked to that image (or
      // carries no links at all — legacy rows — in which case the open
      // resource is the only sensible source).
      //
      // The links have to be read the way the artifact decoders write them:
      // ``decodeArtifactSummary`` folds every link shape into ``linkedKeys``
      // (``artifact:<id>`` entries), so a guard that inspected only the raw
      // ``linked_artifact_ids`` wire names saw no links at all on a decoded
      // target and waved every cross-image extraction through.
      const links = deps.linkedArtifactKeys(target);
      if (links.length &&
          !links.includes(`artifact:${correction.artifact_id}`)) {
        return null;
      }
      return correction;
    }

    categoryInventoryRefresh(_target, detail = {}) {
      const command = detail && detail.command || {};
      const undo = detail && detail.undo || {};
      const action = String(command.action || "").toLowerCase();
      const commandIds = [
        command.id,
        undo.commandId,
        undo.command_id,
      ].filter(Boolean).map((value) => String(value).toLowerCase());
      return action.startsWith("category.") ||
        commandIds.some((value) => value.startsWith("corrections.category."));
    }

    async refreshClassificationTarget(target, detail = {}) {
      const key = targetKey(target);
      let refreshed = null;
      const refreshInherited = this.categoryInventoryRefresh(target, detail);
      if (refreshInherited && this.artifactsFeature &&
          typeof this.artifactsFeature.refresh === "function") {
        try {
          await this.artifactsFeature.refresh({
            preserveSelection: true,
            reason: "category-inheritance",
          });
          refreshed = key && this.artifactsFeature.items &&
            this.artifactsFeature.items.get(key) || null;
        } catch (error) {
          this.setStatus(
            error && error.message ||
              "Inherited artifact categories could not be refreshed",
            true,
          );
        }
      } else if (key && this.artifactsFeature &&
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
      if (refreshInherited && this.booksFeature &&
          typeof this.booksFeature.refresh === "function") {
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
      // Promotion runs at invoke time, so this is also the freshest chance to
      // give a lean hovered target its metadata assertions.
      return this.classificationTargetMetadata(target);
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
        // The overlay layer is positioned inset:0 in the stage, whose box is
        // the image box by CSS invariant. Measure the image first anyway: if
        // the stage ever diverges again (the alpha.11 clamp bug), regions
        // stay pinned to the pixels they describe rather than to the frame.
        getViewport: () => ({
          width: Number(controller.image.clientWidth) || Number(stage.clientWidth) || 1,
          height: Number(controller.image.clientHeight) || Number(stage.clientHeight) || 1,
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
          // display_normalized belongs here too: capture geometry is
          // normalized against the EXIF-upright display rendition — every
          // stage that produces it (cv2, which applies the orientation on
          // decode, and Pillow's exif_transpose) works in that frame, as
          // librarytool.processing.capture_geometry documents. Applying
          // the declared orientation again would rotate the boxes off the
          // text for any capture whose display reports orientation != 1.
          return coordinateSpace.includes("canvas-normalized") ||
            coordinateSpace.includes("exif_oriented") ||
            coordinateSpace.includes("display_normalized");
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
        // The overlay releases its own soft target on destroy; this covers
        // hot targets that reached the controller from any other surface the
        // editor swap is about to invalidate.
        if (this.classificationController &&
            typeof this.classificationController.setHotTarget === "function") {
          this.classificationController.setHotTarget(null);
        }
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

    refreshExternalState(reason = "window-activation", options = {}) {
      if (this.destroyed) return Promise.resolve([]);
      if (this.externalRefreshPromise) return this.externalRefreshPromise;
      const tasks = [];
      if (options.includeBooks !== false && this.booksFeature &&
          typeof this.booksFeature.refresh === "function") {
        tasks.push(Promise.resolve().then(() =>
          this.booksFeature.refresh(reason)));
      }
      if (this.artifactsFeature &&
          typeof this.artifactsFeature.refresh === "function") {
        tasks.push(Promise.resolve().then(() =>
          this.artifactsFeature.refresh({
            preserveSelection: true,
            reason,
          })));
      }
      if (this.itemProperties &&
          typeof this.itemProperties.refresh === "function") {
        tasks.push(Promise.resolve().then(() =>
          this.itemProperties.refresh(reason)));
      }
      if (this.chPanelFeature &&
          typeof this.chPanelFeature.refresh === "function") {
        tasks.push(Promise.resolve().then(() =>
          this.chPanelFeature.refresh(reason)));
      }
      const refresh = Promise.allSettled(tasks).finally(() => {
        if (this.externalRefreshPromise === refresh) {
          this.externalRefreshPromise = null;
        }
      });
      this.externalRefreshPromise = refresh;
      return refresh;
    }

    bindExternalRefresh() {
      this.listen(this.windowRef, "focus", () => {
        void this.refreshExternalState("window-focus");
      });
      this.listen(this.documentRef, "visibilitychange", () => {
        if (this.documentRef.visibilityState === "visible") {
          void this.refreshExternalState("window-visible");
        }
      });
    }

    bindProfileSync() {
      this.listen(this.windowRef, "storage", (event) => {
        this.handleProfileStorageEvent(event);
      });
    }

    mount() {
      this.bindEditorSelector();
      this.bindLayoutReset();
      this.bindWindowControls();
      this.bindTrayTabs();
      this.bindProfileSync();
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
      this.bindExternalRefresh();
      if (this.booksFeature && typeof this.booksFeature.mount === "function") {
        this.booksFeature.mount();
      }
      if (this.artifactsFeature && typeof this.artifactsFeature.mount === "function") {
        this.artifactsFeature.mount();
      }
      if (this.itemProperties && typeof this.itemProperties.mount === "function") {
        this.itemProperties.mount();
      }
      if (this.ocrProposalsFeature &&
          typeof this.ocrProposalsFeature.mount === "function") {
        this.ocrProposalsFeature.mount();
      }
      if (this.chPanelFeature &&
          typeof this.chPanelFeature.mount === "function") {
        this.chPanelFeature.mount();
      }
      void this.loadWorkbenchCapabilities();
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
      if (this.itemProperties &&
          typeof this.itemProperties.setSelection === "function") {
        void Promise.resolve(this.itemProperties.setSelection(selection.itemId))
          .catch((error) => this.setStatus(
            error && error.message || "Item metadata could not be loaded", true));
      }
      if (this.ocrProposalsFeature &&
          typeof this.ocrProposalsFeature.setItem === "function") {
        void Promise.resolve(this.ocrProposalsFeature.setItem(selection.itemId))
          .catch((error) => this.setStatus(
            error && error.message || "OCR proposals could not be loaded", true));
      }
      if (this.chPanelFeature &&
          typeof this.chPanelFeature.setItem === "function") {
        void Promise.resolve(this.chPanelFeature.setItem(selection.itemId))
          .catch((error) => this.setStatus(
            error && error.message ||
              "The CH master-list match could not be loaded", true));
      }
      const changedItem = previous.itemId !== selection.itemId;
      const changedDeepLink = previous.artifactId !== selection.artifactId ||
        previous.annotationId !== selection.annotationId;
      // An item or artifact switch replaces the hoverable surfaces without
      // firing pointerleave, so a stale hot target would keep routing
      // single-letter hotkeys at content that is no longer visible.
      if ((changedItem || previous.artifactId !== selection.artifactId) &&
          this.classificationController &&
          typeof this.classificationController.setHotTarget === "function") {
        this.classificationController.setHotTarget(null);
      }
      if (this.artifactsFeature && metadata.source !== "artifacts" &&
          (changedItem || changedDeepLink || metadata.forceContext === true)) {
        const context = selectionContext(this.state.context, selection);
        if (context && metadata.source === "books" && metadata.navigationPreview) {
          context.navigationPreview = metadata.navigationPreview;
        }
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
        // Browser preview (no Electron bridge): a context may arrive via
        // the URL hash — #context=<url-encoded JSON> — validated by the
        // same normalizeWorkbenchContext gate the bridge path uses.
        if (!this.applyContextFromLocationHash()) {
          this.setStatus("Browser preview — no desktop workbench context");
        }
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

    applyContextFromLocationHash() {
      const location = this.windowRef && this.windowRef.location;
      const hash = location && typeof location.hash === "string"
        ? location.hash : "";
      const match = /[#&]context=([^&]+)/.exec(hash);
      if (!match) return false;
      let value = null;
      try {
        value = JSON.parse(decodeURIComponent(match[1]));
      } catch (error) {
        return false;
      }
      if (!this.applyContextSafely(value)) return false;
      this.contextGeneration += 1;
      return true;
    }

    applyContext(value) {
      const context = normalizeWorkbenchContext(value);
      if (context.ui_profile_key !== this.profileKey) this.applyProfile(context.ui_profile_key);
      this.state.applyContext(context);
      if (this.itemProperties &&
          typeof this.itemProperties.setSelection === "function") {
        void Promise.resolve(
          this.itemProperties.setSelection(this.state.selection.itemId))
          .catch((error) => this.setStatus(
            error && error.message || "Item metadata could not be loaded", true));
      }
      if (this.ocrProposalsFeature &&
          typeof this.ocrProposalsFeature.setItem === "function") {
        void Promise.resolve(
          this.ocrProposalsFeature.setItem(this.state.selection.itemId))
          .catch((error) => this.setStatus(
            error && error.message || "OCR proposals could not be loaded", true));
      }
      if (this.chPanelFeature &&
          typeof this.chPanelFeature.setItem === "function") {
        void Promise.resolve(
          this.chPanelFeature.setItem(this.state.selection.itemId))
          .catch((error) => this.setStatus(
            error && error.message ||
              "The CH master-list match could not be loaded", true));
      }
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

    handleProfileStorageEvent(event) {
      if (!this.profileStore ||
          typeof this.profileStore.matchesStorageEvent !== "function" ||
          !this.profileStore.matchesStorageEvent(this.profileKey, event)) {
        return false;
      }
      const profile = this.profileStore.load(this.profileKey);
      if (this.imageAdjustTool &&
          typeof this.imageAdjustTool.restoreProfile === "function") {
        this.imageAdjustTool.restoreProfile(
          profile.tools && profile.tools.imageAdjust);
      }
      return true;
    }

    persistProfile(options = {}) {
      if (!this.layout || !this.editorRegistry) return;
      const currentTools = {
        imageAdjust: this.imageAdjustTool &&
            typeof this.imageAdjustTool.serializeProfile === "function"
          ? this.imageAdjustTool.serializeProfile()
          : {},
        classification: classificationProfile(this.classificationController),
      };
      let tools = currentTools;
      if (this.profileStore && typeof this.profileStore.load === "function") {
        const latest = this.profileStore.load(this.profileKey);
        if (latest.found && isPlainObject(latest.tools)) {
          tools = { ...latest.tools };
        }
      }
      const toolUpdates = isPlainObject(options.toolUpdates)
        ? options.toolUpdates : {};
      if (this.profileStore &&
          typeof this.profileStore.saveTool === "function") {
        for (const [toolName, value] of Object.entries(toolUpdates)) {
          this.profileStore.saveTool(this.profileKey, toolName, value);
        }
      }
      this.profileStore.save(this.profileKey, {
        layout: this.layout.getState(),
        editors: this.editorRegistry.serializeChoices(),
        // Tool profile fields are durable preferences with independent write
        // triggers. Merge the latest stored tool document so a layout save in
        // another Corrections window cannot roll back a successfully committed
        // Image Adjust brightness (or a keymap remap).
        tools: { ...tools, ...toolUpdates },
      }, {
        // Explicit tool updates were written to independent per-tool sidecars
        // above. Presentation-only saves must not rewrite another window's
        // concurrently committed tool preference.
        writeTools: false,
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
      if (this.itemProperties &&
          typeof this.itemProperties.destroy === "function") {
        this.itemProperties.destroy();
      }
      if (this.ocrProposalsFeature &&
          typeof this.ocrProposalsFeature.destroy === "function") {
        this.ocrProposalsFeature.destroy();
      }
      if (this.chPanelFeature &&
          typeof this.chPanelFeature.destroy === "function") {
        this.chPanelFeature.destroy();
      }
      this.booksFeature = null;
      this.artifactsFeature = null;
      this.itemProperties = null;
      this.ocrProposalsFeature = null;
      this.chPanelFeature = null;
      if (this.presetBatchRetryCommands) {
        this.presetBatchRetryCommands.clear();
      }
      if (this.presetBatchRuns) this.presetBatchRuns.clear();
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
    BOOKS_NAVIGATION_COMMANDS,
    CONTEXT_SCHEMA,
    CorrectionsShell,
    CorrectionsWindowState,
    artifactSelection,
    correctionsRuntimePorts,
    installAutoBoot,
    navigationOnlyTarget,
    nextTrayTab,
    normalizeSelection,
    normalizeWorkbenchContext,
    selectionContext,
  };
});

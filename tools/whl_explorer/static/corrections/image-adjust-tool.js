(function installCorrectionsImageAdjustTool(root, factory) {
  const dependencies = typeof module === "object" && module.exports
    ? require("./image-editor-state")
    : root.LibraryToolCorrections;
  const api = factory(dependencies);
  if (typeof module === "object" && module.exports) module.exports = api;
  else Object.assign(root.LibraryToolCorrections ||= {}, api);
})(typeof globalThis !== "undefined" ? globalThis : this, function imageAdjustToolFactory(
  stateApi,
) {
  "use strict";

  const {
    MAX_OPERATIONS_PER_RECIPE,
    PROCESSING_OPERATION_PARAMETERS,
    PROCESSING_OPERATION_SCHEMA,
    PROCESSING_OPERATION_VERSION,
    TOOLS,
    isFormControlTarget,
    normalizeManualAdjustment,
    sourcePinsValid,
    visibleModal,
  } = stateApi;

  const IMAGE_ADJUST_PROFILE_KEY = "imageAdjust";
  const IMAGE_ADJUST_PROFILE_DEFAULT = Object.freeze({
    lastAppliedBrightness: 0,
  });
  const BRIGHTNESS_MIN = -100;
  const BRIGHTNESS_MAX = 100;
  const DEFAULT_CONTRAST = 100;
  const CONTRAST_MIN = 0;
  const CONTRAST_MAX = 100;
  const THRESHOLD_RULE =
    "round_half_up(127.5 - brightness_percent * 1.275), clamped_0_255";
  const BINARY_ALGORITHM = "grayscale-threshold-blend-v1";

  // Authoring catalog for the six engine processing operations. Defaults and
  // rule strings mirror librarytool/processing/operations.py exactly: the
  // transform command is content-addressed, so the engine only accepts a
  // document that byte-matches its own canonical serialization — including
  // the human-readable rule. tests/fixtures/processing_operations_canonical
  // .json (generated from the Python module) pins this parity.
  const PROCESSING_OPERATION_DEFAULTS = Object.freeze({
    "white-balance-v1": Object.freeze({
      mode: "gray_world", strength_percent: 100, temperature: 0, tint: 0,
    }),
    "channel-gain-v1": Object.freeze({
      red_percent: 100, green_percent: 100, blue_percent: 100,
    }),
    "gamma-v1": Object.freeze({ gamma_hundredths: 100 }),
    "contrast-v1": Object.freeze({ contrast_percent: 0 }),
    "unsharp-mask-v1": Object.freeze({
      radius_tenths: 10, amount_percent: 150, threshold: 3,
    }),
    "kernel-sharpen-v1": Object.freeze({ strength_percent: 50 }),
  });
  const PROCESSING_OPERATION_LABELS = Object.freeze({
    "white-balance-v1": "White balance",
    "channel-gain-v1": "Channel gain",
    "gamma-v1": "Gamma",
    "contrast-v1": "Contrast",
    "unsharp-mask-v1": "Unsharp mask",
    "kernel-sharpen-v1": "Kernel sharpen",
  });
  const PROCESSING_PARAMETER_LABELS = Object.freeze({
    radius_tenths: "Radius ×0.1",
    amount_percent: "Amount %",
    threshold: "Threshold",
    strength_percent: "Strength %",
    gamma_hundredths: "Gamma ×100",
    contrast_percent: "Contrast %",
    red_percent: "R %",
    green_percent: "G %",
    blue_percent: "B %",
    temperature: "Temp",
    tint: "Tint",
  });
  const PROCESSING_OPERATION_RULES = Object.freeze({
    "unsharp-mask-v1": () =>
      "pillow_unsharp_mask(radius=radius_tenths/10, " +
      "percent=amount_percent, threshold=threshold)",
    "kernel-sharpen-v1": () =>
      "3x3 kernel [[0,-1,0],[-1,5,-1],[0,-1,0]] blended with identity at " +
      "strength_percent/100",
    "gamma-v1": () =>
      "round_half_up(255 * (value/255) ** (100/gamma_hundredths)), " +
      "clamped_0_255",
    "contrast-v1": () =>
      "factor = 1 + contrast_percent/100 when positive else " +
      "1 + contrast_percent/100 toward 127.5; " +
      "round_half_up(127.5 + (value - 127.5) * factor), clamped_0_255",
    "channel-gain-v1": () =>
      "round_half_up(value * channel_percent/100), clamped_0_255",
    "white-balance-v1": (parameters) => parameters.mode === "manual"
      ? "red gain = 1 + strength_percent/100 * temperature/200; " +
        "blue gain = 1 - strength_percent/100 * temperature/200; " +
        "green gain = 1 + strength_percent/100 * tint/200; " +
        "round_half_up(value * gain), clamped_0_255"
      : "per-channel gain = 1 + strength_percent/100 * " +
        "(mean_of_all_channels/channel_mean - 1); " +
        "round_half_up(value * gain), clamped_0_255",
  });

  function clampOperationParameter(algorithm, name, value) {
    const bounds = Object.prototype.hasOwnProperty.call(
      PROCESSING_OPERATION_PARAMETERS, algorithm,
    ) ? PROCESSING_OPERATION_PARAMETERS[algorithm][name] : null;
    const fallback = PROCESSING_OPERATION_DEFAULTS[algorithm][name];
    if (!bounds) return fallback;
    const numeric = Number(value);
    if (!Number.isFinite(numeric)) return fallback;
    const [minimum, maximum] = bounds;
    return Math.max(minimum, Math.min(maximum, Math.round(numeric)));
  }

  function createProcessingOperation(algorithm, parameters = {}) {
    const defaults = Object.prototype.hasOwnProperty.call(
      PROCESSING_OPERATION_DEFAULTS, algorithm,
    ) ? PROCESSING_OPERATION_DEFAULTS[algorithm] : null;
    if (!defaults) {
      throw new TypeError(
        `unsupported processing algorithm: ${String(algorithm)}`);
    }
    const source = isPlainObject(parameters) ? parameters : {};
    const resolved = {};
    for (const name of Object.keys(defaults)) {
      if (name === "mode") {
        resolved.mode = source.mode === "manual" ? "manual" : "gray_world";
        continue;
      }
      resolved[name] = clampOperationParameter(
        algorithm, name, source[name] == null ? defaults[name] : source[name]);
    }
    if (resolved.mode === "gray_world") {
      // The engine rejects a gray_world document carrying temperature or
      // tint; zeroing on mode switch keeps the authored recipe canonical.
      if ("temperature" in resolved) resolved.temperature = 0;
      if ("tint" in resolved) resolved.tint = 0;
    }
    return {
      schema: PROCESSING_OPERATION_SCHEMA,
      version: PROCESSING_OPERATION_VERSION,
      algorithm,
      rule: PROCESSING_OPERATION_RULES[algorithm](resolved),
      ...resolved,
    };
  }
  const TERMINAL_OCR_STATES = new Set([
    "not_requested", "succeeded", "failed", "cancelled",
  ]);
  const TERMINAL_JOB_STATES = new Set([
    "cancelled", "failed", "done", "interrupted",
  ]);
  const COMMITTED_OUTPUT_KINDS = new Set([
    "corrected-display", "ocr-ready", "thumbnail", "transform-manifest",
  ]);
  const EXTRACTED_OUTPUT_KINDS = new Set([
    "extracted-figure", "ocr-ready", "transform-manifest",
  ]);
  // Standalone re-OCR targets a committed transform output; the raster view
  // advertises that lineage through extensions.correction_transform.
  const REOCR_SOURCE_KINDS = new Set(["corrected-display", "ocr-ready"]);
  const PORTABLE_IDENTIFIER_RE = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
  let toolSequence = 0;
  let reocrOperationCounter = 0;

  function defaultReocrOperationId() {
    const cryptoRef = typeof globalThis !== "undefined" && globalThis.crypto;
    if (cryptoRef && typeof cryptoRef.randomUUID === "function") {
      return `reocr-${cryptoRef.randomUUID()}`;
    }
    reocrOperationCounter += 1;
    return `reocr-${Date.now().toString(36)}-${Math.random()
      .toString(36).slice(2)}-${reocrOperationCounter.toString(36)}`;
  }

  function reocrEligibleResource(resource) {
    const summary = resource && (resource.summary || resource);
    const extensions = summary && summary.extensions;
    const pin = extensions && extensions.correction_transform;
    return Boolean(
      pin && typeof pin === "object" && !Array.isArray(pin) &&
      REOCR_SOURCE_KINDS.has(pin.output_kind) &&
      summary && typeof summary.itemId === "string" && summary.itemId &&
      typeof summary.id === "string" && summary.id &&
      typeof summary.revision === "string" && summary.revision,
    );
  }

  function isPlainObject(value) {
    if (!value || typeof value !== "object" || Array.isArray(value)) return false;
    const prototype = Object.getPrototypeOf(value);
    return prototype === Object.prototype || prototype === null;
  }

  function cloneJson(value) {
    return JSON.parse(JSON.stringify(value));
  }

  function clampBrightness(value) {
    const numeric = Number(value);
    if (!Number.isFinite(numeric)) return 0;
    return Math.max(
      BRIGHTNESS_MIN,
      Math.min(BRIGHTNESS_MAX, Math.round(numeric)),
    );
  }

  function validBrightness(value) {
    return Number.isInteger(value) &&
      value >= BRIGHTNESS_MIN && value <= BRIGHTNESS_MAX;
  }

  function clampContrast(value) {
    const numeric = Number(value);
    if (!Number.isFinite(numeric)) return DEFAULT_CONTRAST;
    return Math.max(
      CONTRAST_MIN,
      Math.min(CONTRAST_MAX, Math.round(numeric)),
    );
  }

  function normalizeImageAdjustProfile(value) {
    const source = isPlainObject(value) ? value : {};
    return {
      lastAppliedBrightness: validBrightness(source.lastAppliedBrightness)
        ? source.lastAppliedBrightness
        : IMAGE_ADJUST_PROFILE_DEFAULT.lastAppliedBrightness,
    };
  }

  function serializeImageAdjustProfile(value) {
    if (value && typeof value.serializeProfile === "function") {
      return normalizeImageAdjustProfile(value.serializeProfile());
    }
    return normalizeImageAdjustProfile(value);
  }

  function thresholdForBrightness(brightness) {
    if (!validBrightness(brightness)) {
      throw new TypeError("brightness must be an integer from -100 through 100");
    }
    return Math.max(
      0,
      Math.min(255, Math.floor(127.5 - brightness * 1.275 + 0.5)),
    );
  }

  function createManualBinaryAdjustment(brightness, contrast = DEFAULT_CONTRAST) {
    if (!validBrightness(brightness)) {
      throw new TypeError("brightness must be an integer from -100 through 100");
    }
    if (!Number.isInteger(contrast) || contrast < 0 || contrast > 100) {
      throw new TypeError("contrast must be an integer from 0 through 100");
    }
    return normalizeManualAdjustment({
      schema: "org.whl.raster.manual-binary-adjust",
      version: 1,
      algorithm: BINARY_ALGORITHM,
      contrast_percent: contrast,
      brightness_percent: brightness,
      threshold: thresholdForBrightness(brightness),
      threshold_rule: THRESHOLD_RULE,
      comparison: "grayscale_value > threshold",
    });
  }

  function opaqueChannelOnWhite(channel, alpha) {
    return Math.floor((channel * alpha + 255 * (255 - alpha) + 127) / 255);
  }

  function pillowGrayscale(red, green, blue) {
    // These are Pillow's fixed-point RGB -> L coefficients. Keeping this
    // explicit avoids browser/CSS color-filter differences at the threshold.
    return Math.floor(
      (19595 * red + 38470 * green + 7471 * blue + 32768) / 65536,
    );
  }

  function applyManualBinaryPreview(rgba, adjustment) {
    if (!rgba || typeof rgba.length !== "number" || rgba.length % 4 !== 0) {
      throw new TypeError("preview pixels must be an RGBA array");
    }
    const recipe = normalizeManualAdjustment(adjustment);
    if (!recipe) throw new TypeError("a manual binary adjustment is required");
    const output = new Uint8ClampedArray(rgba.length);
    const threshold = recipe.threshold;
    const contrast = recipe.contrast_percent;
    for (let index = 0; index < rgba.length; index += 4) {
      const alpha = Number(rgba[index + 3]);
      const red = opaqueChannelOnWhite(Number(rgba[index]), alpha);
      const green = opaqueChannelOnWhite(Number(rgba[index + 1]), alpha);
      const blue = opaqueChannelOnWhite(Number(rgba[index + 2]), alpha);
      const grayscale = pillowGrayscale(red, green, blue);
      const binary = grayscale > threshold ? 255 : 0;
      const value = Math.floor(
        ((100 - contrast) * grayscale + contrast * binary + 50) / 100,
      );
      output[index] = value;
      output[index + 1] = value;
      output[index + 2] = value;
      output[index + 3] = 255;
    }
    return output;
  }

  function previewDimensions(width, height, options = {}) {
    if (!Number.isFinite(width) || !Number.isFinite(height) ||
        width <= 0 || height <= 0) {
      throw new TypeError("preview source dimensions are unavailable");
    }
    const maxEdge = Number.isFinite(options.maxEdge)
      ? Math.max(16, Math.floor(options.maxEdge)) : 1600;
    const maxPixels = Number.isFinite(options.maxPixels)
      ? Math.max(256, Math.floor(options.maxPixels)) : 2_000_000;
    const scale = Math.min(
      1,
      maxEdge / Math.max(width, height),
      Math.sqrt(maxPixels / (width * height)),
    );
    return {
      width: Math.max(1, Math.floor(width * scale)),
      height: Math.max(1, Math.floor(height * scale)),
      scaled: scale < 1,
    };
  }

  function renderBinaryCanvasPreview({
    image,
    canvas,
    adjustment,
    maxEdge,
    maxPixels,
  }) {
    if (!image || !canvas || typeof canvas.getContext !== "function") {
      throw new TypeError("preview image and canvas are required");
    }
    const context = canvas.getContext("2d", { willReadFrequently: true });
    if (!context || typeof context.drawImage !== "function" ||
        typeof context.getImageData !== "function" ||
        typeof context.putImageData !== "function") {
      throw new TypeError("the browser cannot render an exact binary preview");
    }
    const dimensions = previewDimensions(
      Number(image.naturalWidth || image.width),
      Number(image.naturalHeight || image.height),
      { maxEdge, maxPixels },
    );
    canvas.width = dimensions.width;
    canvas.height = dimensions.height;
    context.clearRect(0, 0, dimensions.width, dimensions.height);
    context.drawImage(image, 0, 0, dimensions.width, dimensions.height);
    const pixels = context.getImageData(0, 0, dimensions.width, dimensions.height);
    const adjusted = applyManualBinaryPreview(pixels.data, adjustment);
    pixels.data.set(adjusted);
    context.putImageData(pixels, 0, 0);
    return {
      ...dimensions,
      adjustment: createManualBinaryAdjustment(
        adjustment.brightness_percent,
        adjustment.contrast_percent,
      ),
    };
  }

  function plainShortcut(event, key) {
    return Boolean(
      event &&
      String(event.key || "").toLowerCase() === key &&
      event.defaultPrevented !== true &&
      event.repeat !== true &&
      event.isComposing !== true &&
      event.altKey !== true &&
      event.ctrlKey !== true &&
      event.metaKey !== true &&
      event.shiftKey !== true
    );
  }

  function canEnterImageAdjust(event, state) {
    const context = event || {};
    return Boolean(
      plainShortcut(context, "a") &&
      context.canvasFocused === true &&
      context.canvasTarget === true &&
      context.modalOpen !== true &&
      context.rectangleEditing !== true &&
      context.formControl !== true &&
      !isFormControlTarget(context.target) &&
      state &&
      !state.gesture
    );
  }

  function canQueueImageAdjustShortcut(event, state, pins) {
    const context = event || {};
    const isSpace = context.key === " " || context.key === "Spacebar" ||
      context.code === "Space";
    return Boolean(
      isSpace &&
      context.defaultPrevented !== true &&
      context.repeat !== true &&
      context.isComposing !== true &&
      context.altKey !== true &&
      context.ctrlKey !== true &&
      context.metaKey !== true &&
      context.shiftKey !== true &&
      context.canvasFocused === true &&
      context.canvasTarget === true &&
      context.modalOpen !== true &&
      context.rectangleEditing !== true &&
      context.formControl !== true &&
      !isFormControlTarget(context.target) &&
      state &&
      state.tool === TOOLS.IMAGE_ADJUST &&
      !state.gesture &&
      state.validation && state.validation.valid &&
      sourcePinsValid(pins) &&
      !["submitting", "queued", "complete"].includes(
        state.submission && state.submission.status,
      )
    );
  }

  function canApplyWheel(event, state) {
    const context = event || {};
    return Boolean(
      context.defaultPrevented !== true &&
      context.altKey !== true &&
      context.ctrlKey !== true &&
      context.metaKey !== true &&
      context.canvasFocused === true &&
      context.canvasTarget === true &&
      context.modalOpen !== true &&
      context.rectangleEditing !== true &&
      context.formControl !== true &&
      !isFormControlTarget(context.target) &&
      state &&
      state.tool === TOOLS.IMAGE_ADJUST &&
      !state.gesture &&
      Number.isFinite(Number(context.deltaY)) &&
      Number(context.deltaY) !== 0
    );
  }

  function element(documentRef, tagName, className = "", text = null) {
    const node = documentRef.createElement(tagName);
    if (className) node.className = className;
    if (text != null) node.textContent = String(text);
    return node;
  }

  function setData(node, name, value) {
    if (node.dataset) node.dataset[name] = String(value);
    else node.setAttribute(
      `data-${name.replace(/[A-Z]/g, (letter) => `-${letter.toLowerCase()}`)}`,
      String(value),
    );
  }

  function removeNode(node) {
    if (!node) return;
    if (typeof node.remove === "function") node.remove();
    else if (node.parentNode && typeof node.parentNode.removeChild === "function") {
      node.parentNode.removeChild(node);
    }
  }

  function addListener(removers, target, type, listener, options) {
    if (!target || typeof target.addEventListener !== "function") return;
    target.addEventListener(type, listener, options);
    removers.push(() => target.removeEventListener(type, listener, options));
  }

  function findDescendant(root, predicate) {
    if (!root) return null;
    if (predicate(root)) return root;
    const children = root.children ? Array.from(root.children) : [];
    for (const child of children) {
      const result = findDescendant(child, predicate);
      if (result) return result;
    }
    return null;
  }

  function operationIdentifier(value) {
    return typeof value === "string" && value.trim()
      ? value.trim() : "";
  }

  function canonicalCommand(value) {
    if (!isPlainObject(value)) return null;
    const operationId = operationIdentifier(value.operation_id);
    if (!operationId || typeof value.rerun_ocr !== "boolean") return null;
    let adjustment = null;
    try {
      adjustment = normalizeManualAdjustment(value.adjustment);
    } catch (error) {
      return null;
    }
    return {
      operationId,
      adjustment,
      rerunOcr: value.rerun_ocr,
      command: value,
    };
  }

  function normalizedOcrOutcome(value) {
    if (!isPlainObject(value) || !TERMINAL_OCR_STATES.has(value.state)) return null;
    return cloneJson({
      state: value.state,
      source: value.source == null ? null : value.source,
      proposal_ref: typeof value.proposal_ref === "string" ? value.proposal_ref : "",
      failure: value.failure == null ? null : value.failure,
    });
  }

  function committedOperation(result, command = null) {
    if (!isPlainObject(result) || result.cancelled_before_commit === true ||
        !isPlainObject(result.image_commit)) return "";
    const outer = operationIdentifier(result.operation_id);
    const inner = operationIdentifier(result.image_commit.operation_id);
    const expectedKinds = command && isPlainObject(command.extraction)
      ? EXTRACTED_OUTPUT_KINDS : COMMITTED_OUTPUT_KINDS;
    if (!outer || !inner || outer !== inner ||
        !Array.isArray(result.image_commit.outputs) ||
        result.image_commit.outputs.length !== expectedKinds.size) {
      return "";
    }
    const kinds = new Set();
    const artifactIds = new Set();
    for (const output of result.image_commit.outputs) {
      const kind = output && typeof output.kind === "string"
        ? output.kind : "";
      const artifactId = output && typeof output.artifact_id === "string" &&
        PORTABLE_IDENTIFIER_RE.test(output.artifact_id)
        ? output.artifact_id : "";
      if (!expectedKinds.has(kind) || kinds.has(kind) ||
          !artifactId || artifactIds.has(artifactId)) return "";
      kinds.add(kind);
      artifactIds.add(artifactId);
    }
    if (kinds.size !== expectedKinds.size) return "";
    return outer;
  }

  function terminalJobState(result, imageCommitted) {
    const supplied = result && result.terminal_state;
    if (TERMINAL_JOB_STATES.has(supplied)) return supplied;
    if (result && result.cancelled_before_commit === true) return "cancelled";
    if (imageCommitted) return "done";
    if (result && isPlainObject(result.failure)) return "failed";
    if (result && Object.prototype.hasOwnProperty.call(result, "image_commit")) {
      return "failed";
    }
    return "";
  }

  class ImageAdjustTool {
    constructor(options = {}) {
      this.options = options;
      this.profile = normalizeImageAdjustProfile(options.profile);
      this.brightness = this.profile.lastAppliedBrightness;
      this.contrast = DEFAULT_CONTRAST;
      this.adjustmentEnabled = true;
      this.rerunOcr = options.rerunOcr === true;
      this.reocrCapability = options.reocrCapability === true;
      this.reocrBusy = false;
      this.reocrOperationIdFactory =
        typeof options.reocrOperationIdFactory === "function"
          ? options.reocrOperationIdFactory : defaultReocrOperationId;
      this.pending = new Map();
      this.mountRecord = null;
      this.profileListeners = new Set();
      this.ocrListeners = new Set();
      this.lastOcrOutcome = null;
      this.destroyed = false;
    }

    setReocrCapability(value) {
      this.reocrCapability = value === true;
      this.refreshMount(false);
      return this.reocrCapability;
    }

    restoreProfile(value) {
      this.profile = normalizeImageAdjustProfile(value);
      const record = this.mountRecord;
      const state = record && !record.disposed
        ? record.controller.getState() : null;
      const activeDraft = Boolean(state && state.tool === TOOLS.IMAGE_ADJUST);
      if (!activeDraft) {
        this.brightness = this.profile.lastAppliedBrightness;
        this.contrast = DEFAULT_CONTRAST;
        if (record && !record.disposed) this.refreshMount(false, state);
      }
      return this.serializeProfile();
    }

    serializeProfile() {
      return { ...this.profile };
    }

    getState() {
      const editorState = this.mountRecord &&
        this.mountRecord.controller.getState();
      return {
        active: Boolean(editorState && editorState.tool === TOOLS.IMAGE_ADJUST),
        brightness: this.brightness,
        adjustmentEnabled: this.adjustmentEnabled,
        contrast: this.contrast,
        rememberedBrightness: this.profile.lastAppliedBrightness,
        rerunOcr: this.rerunOcr,
        reocrCapability: this.reocrCapability,
        reocrBusy: this.reocrBusy,
        pendingOperationIds: Array.from(this.pending.keys()),
        lastOcrOutcome: this.lastOcrOutcome && cloneJson(this.lastOcrOutcome),
      };
    }

    subscribeProfile(listener) {
      if (typeof listener !== "function") throw new TypeError("listener is required");
      this.profileListeners.add(listener);
      return () => this.profileListeners.delete(listener);
    }

    subscribeOcrOutcome(listener) {
      if (typeof listener !== "function") throw new TypeError("listener is required");
      this.ocrListeners.add(listener);
      return () => this.ocrListeners.delete(listener);
    }

    setBrightness(value, detail = {}) {
      const brightness = clampBrightness(value);
      const enabled = detail.activate !== false;
      const changed = brightness !== this.brightness ||
        enabled && !this.adjustmentEnabled;
      this.brightness = brightness;
      if (enabled) this.adjustmentEnabled = true;
      if (changed) this.refreshMount(detail.announce !== false);
      return brightness;
    }

    setAdjustment(value, detail = {}) {
      const adjustment = normalizeManualAdjustment(value);
      if (adjustment === null) {
        const changed = this.adjustmentEnabled ||
          this.contrast !== DEFAULT_CONTRAST;
        this.adjustmentEnabled = false;
        this.contrast = DEFAULT_CONTRAST;
        if (changed) this.refreshMount(detail.announce !== false);
        return null;
      }
      const changed = !this.adjustmentEnabled ||
        adjustment.brightness_percent !== this.brightness ||
        adjustment.contrast_percent !== this.contrast;
      this.brightness = adjustment.brightness_percent;
      this.contrast = adjustment.contrast_percent;
      this.adjustmentEnabled = true;
      if (changed) this.refreshMount(detail.announce !== false);
      return createManualBinaryAdjustment(this.brightness, this.contrast);
    }

    setRerunOcr(value) {
      this.rerunOcr = value === true;
      this.refreshMount(false);
      return this.rerunOcr;
    }

    ownsTransform(context = {}) {
      const state = context.state || this.mountRecord &&
        this.mountRecord.controller.getState();
      return Boolean(state && state.tool === TOOLS.IMAGE_ADJUST);
    }

    getAdjustment(context = {}) {
      return this.adjustmentEnabled && this.ownsTransform(context)
        ? createManualBinaryAdjustment(this.brightness, this.contrast)
        : null;
    }

    getRerunOcr() {
      return this.rerunOcr;
    }

    canQueue(context = {}) {
      const state = context.state || this.mountRecord &&
        this.mountRecord.controller.getState();
      return Boolean(
        state &&
        state.tool === TOOLS.IMAGE_ADJUST &&
        !state.gesture &&
        state.validation && state.validation.valid &&
        !this.rectangleEditing(state, context.resource)
      );
    }

    rectangleEditing(state, resource) {
      return typeof this.options.isRectangleEditing === "function" &&
        this.options.isRectangleEditing({
          controller: this.mountRecord && this.mountRecord.controller,
          resource: resource || this.mountRecord && this.mountRecord.resource,
          state,
        }) === true;
    }

    modalOpen(documentRef) {
      return typeof this.options.isModalOpen === "function"
        ? this.options.isModalOpen(documentRef) === true
        : visibleModal(documentRef);
    }

    eventContext(event, controller, resource) {
      const documentRef = controller.surface && controller.surface.ownerDocument ||
        controller.canvas && controller.canvas.ownerDocument;
      const state = controller.getState();
      return {
        key: event.key,
        code: event.code,
        deltaY: event.deltaY,
        repeat: event.repeat,
        defaultPrevented: event.defaultPrevented,
        isComposing: event.isComposing,
        altKey: event.altKey,
        ctrlKey: event.ctrlKey,
        metaKey: event.metaKey,
        shiftKey: event.shiftKey,
        target: event.target,
        formControl: isFormControlTarget(event.target),
        canvasFocused: Boolean(documentRef &&
          documentRef.activeElement === controller.canvas),
        canvasTarget: event.target === controller.canvas,
        modalOpen: this.modalOpen(documentRef),
        rectangleEditing: this.rectangleEditing(state, resource),
      };
    }

    buildPresetPanel(documentRef, controller) {
      const factory = this.options.createPresetPanel;
      const port = this.options.presets;
      if (typeof factory !== "function" || !port) return null;
      try {
        return factory({
          presets: port,
          documentRef,
          getCurrentRecipe: () => ({
            adjustment: this.adjustmentEnabled
              ? createManualBinaryAdjustment(
                this.brightness, this.contrast) : null,
            operations: (() => {
              const state = controller.getState();
              return Array.isArray(state.operations) ? state.operations : [];
            })(),
          }),
          onApply: (preset) => {
            controller.dispatch({
              type: "SET_TOOL",
              tool: TOOLS.IMAGE_ADJUST,
            });
            if (preset.adjustment) {
              this.setAdjustment(preset.adjustment, {
                announce: false,
              });
            } else {
              this.setAdjustment(null, { announce: false });
            }
            controller.dispatch({
              type: "SET_PROCESSING_OPERATIONS",
              operations: preset.operations,
            });
            this.refreshMount(false);
            if (typeof this.options.onPresetApplied === "function") {
              this.options.onPresetApplied(preset, controller);
            }
          },
          onBatchApply: typeof this.options.onPresetBatchApply === "function"
            ? (preset) => this.options.onPresetBatchApply(preset, controller)
            : null,
        });
      } catch (error) {
        return null;
      }
    }

    mount(controller, resource) {
      if (this.destroyed) throw new Error("image adjust tool is destroyed");
      if (!controller || !controller.canvas || !controller.surface ||
          !controller.inspector || !controller.image ||
          typeof controller.getState !== "function" ||
          typeof controller.dispatch !== "function" ||
          typeof controller.requestQueue !== "function") {
        throw new TypeError("an image editor controller is required");
      }
      if (this.mountRecord) this.unmount(this.mountRecord);
      this.brightness = this.profile.lastAppliedBrightness;
      this.contrast = DEFAULT_CONTRAST;
      this.adjustmentEnabled = true;
      const documentRef = controller.surface.ownerDocument ||
        controller.canvas.ownerDocument;
      if (!documentRef || typeof documentRef.createElement !== "function") {
        throw new TypeError("the image editor document is required");
      }
      const removers = [];
      const instanceId = `image-adjust-${++toolSequence}`;
      const panel = element(documentRef, "fieldset", "image-adjust-panel");
      const legend = element(documentRef, "legend", "", "Image Adjust");
      const activeTool = element(
        documentRef, "p", "image-adjust-active-tool", "Image Adjust inactive",
      );
      activeTool.setAttribute("role", "status");
      activeTool.setAttribute("aria-live", "polite");

      // The wire field is named contrast_percent, but it is a binary blend
      // weight: 0 keeps the grayscale image, 100 is fully thresholded.
      // Label it for what it does rather than repeating the wire name; the
      // real linear-contrast dial is the contrast-v1 processing operation.
      const contrastRow = element(documentRef, "div", "image-adjust-control");
      const contrastId = `${instanceId}-contrast`;
      const contrastLabel = element(documentRef, "label", "", "Binarize");
      contrastLabel.htmlFor = contrastId;
      const contrastInput = element(documentRef, "input");
      contrastInput.id = contrastId;
      contrastInput.type = "number";
      contrastInput.min = String(CONTRAST_MIN);
      contrastInput.max = String(CONTRAST_MAX);
      contrastInput.step = "1";
      contrastInput.inputMode = "numeric";
      contrastInput.title =
        "Binary blend weight: 0 keeps the grayscale image, " +
        "100 is fully thresholded.";
      contrastInput.setAttribute(
        "aria-label",
        "Binarize percent: blend weight toward the binary threshold image",
      );
      contrastRow.append(contrastLabel, contrastInput);

      const brightnessRow = element(documentRef, "div", "image-adjust-control");
      const brightnessId = `${instanceId}-brightness`;
      const brightnessLabel = element(documentRef, "label", "", "Brightness");
      brightnessLabel.htmlFor = brightnessId;
      const brightnessInput = element(documentRef, "input");
      brightnessInput.id = brightnessId;
      brightnessInput.type = "number";
      brightnessInput.min = String(BRIGHTNESS_MIN);
      brightnessInput.max = String(BRIGHTNESS_MAX);
      brightnessInput.step = "1";
      brightnessInput.inputMode = "numeric";
      const brightnessHint = element(
        documentRef,
        "p",
        "image-adjust-brightness-hint",
        "Use this field or the wheel over the focused image. Range −100 through 100.",
      );
      brightnessHint.id = `${instanceId}-brightness-hint`;
      brightnessInput.setAttribute("aria-describedby", brightnessHint.id);
      brightnessRow.append(brightnessLabel, brightnessInput);

      const operationsBlock = element(
        documentRef, "div", "image-adjust-operations",
      );
      const operationsHead = element(
        documentRef, "div", "image-adjust-operations-head",
      );
      const operationsLabel = element(
        documentRef, "span", "image-adjust-operations-label", "Processing",
      );
      const operationsAddSelect = element(
        documentRef, "select", "image-adjust-operation-add-select",
      );
      operationsAddSelect.setAttribute(
        "aria-label", "Processing operation to add",
      );
      for (const algorithm of Object.keys(PROCESSING_OPERATION_DEFAULTS)) {
        const choice = element(
          documentRef, "option", "", PROCESSING_OPERATION_LABELS[algorithm],
        );
        choice.value = algorithm;
        choice.setAttribute("value", algorithm);
        operationsAddSelect.append(choice);
      }
      operationsAddSelect.value = Object.keys(PROCESSING_OPERATION_DEFAULTS)[0];
      const operationsAddButton = element(
        documentRef, "button", "image-adjust-operation-add", "Add",
      );
      operationsAddButton.type = "button";
      operationsAddButton.setAttribute(
        "aria-label", "Add the selected processing operation",
      );
      operationsHead.append(
        operationsLabel, operationsAddSelect, operationsAddButton,
      );
      const operationList = element(
        documentRef, "ol", "image-adjust-operation-list",
      );
      // These operations run in the queued engine transform, before the
      // binary adjustment. The exact binary preview cannot reproduce them
      // client-side, so say so instead of faking it.
      const operationsHint = element(
        documentRef, "p", "image-adjust-operations-hint",
        "Applied on queue, before the binary adjust — not shown in the preview.",
      );
      operationsHint.hidden = true;
      operationsBlock.append(operationsHead, operationList, operationsHint);

      const ocrRow = element(documentRef, "div", "image-adjust-ocr-control");
      const ocrId = `${instanceId}-rerun-ocr`;
      const ocrInput = element(documentRef, "input");
      ocrInput.id = ocrId;
      ocrInput.type = "checkbox";
      const ocrLabel = element(documentRef, "label", "", "Re-run OCR");
      ocrLabel.htmlFor = ocrId;
      ocrRow.append(ocrInput, ocrLabel);

      let reocrRow = null;
      let reocrButton = null;
      if (typeof this.options.requestReocr === "function") {
        reocrRow = element(documentRef, "div", "image-adjust-reocr-control");
        reocrButton = element(documentRef, "button", "", "Re-OCR");
        reocrButton.type = "button";
        setData(reocrButton, "imageReocr", "true");
        reocrButton.setAttribute(
          "aria-label", "Queue OCR of this corrected image");
        const reocrHint = element(
          documentRef, "span", "image-adjust-reocr-hint",
          "Proposes machine OCR from the committed rendition.",
        );
        reocrRow.append(reocrButton, reocrHint);
      }

      const thresholdStatus = element(
        documentRef, "p", "image-adjust-threshold",
      );
      const jobStatus = element(documentRef, "p", "image-adjust-job-status");
      jobStatus.setAttribute("role", "status");
      jobStatus.setAttribute("aria-live", "polite");
      panel.append(
        legend,
        activeTool,
        contrastRow,
        brightnessRow,
        brightnessHint,
        operationsBlock,
        ocrRow,
        ...(reocrRow ? [reocrRow] : []),
        thresholdStatus,
        jobStatus,
      );
      // Presets are optional: without a configured port the panel simply is not
      // built, so a host that has not wired the preset service still mounts.
      const presetPanel = this.buildPresetPanel(documentRef, controller);
      if (presetPanel) {
        panel.append(presetPanel.node);
        removers.push(() => presetPanel.destroy());
        Promise.resolve(presetPanel.refresh()).catch(() => {});
      }
      controller.inspector.append(panel);

      const previewCanvas = element(
        documentRef, "canvas", "image-adjust-preview-canvas",
      );
      previewCanvas.setAttribute("aria-hidden", "true");
      previewCanvas.hidden = true;
      const imageStage = controller.image.parentNode;
      if (imageStage && typeof imageStage.append === "function") {
        imageStage.append(previewCanvas);
      }

      const toolButton = findDescendant(controller.toolbar, (candidate) =>
        candidate && candidate.dataset &&
        candidate.dataset.imageTool === TOOLS.IMAGE_ADJUST);
      if (toolButton) toolButton.setAttribute("aria-keyshortcuts", "A");

      const record = {
        controller,
        resource,
        documentRef,
        panel,
        activeTool,
        contrastInput,
        brightnessInput,
        operationsAddSelect,
        operationsAddButton,
        operationList,
        operationsHint,
        renderedOperations: null,
        renderedOperationsActive: null,
        ocrInput,
        reocrRow,
        reocrButton,
        thresholdStatus,
        jobStatus,
        previewCanvas,
        canvasOriginalLabel: typeof controller.canvas.getAttribute === "function"
          ? controller.canvas.getAttribute("aria-label") : null,
        removers,
        previewGeneration: 0,
        disposed: false,
      };
      this.mountRecord = record;

      const handleKeyDown = (event) => {
        const state = controller.getState();
        const context = this.eventContext(event, controller, resource);
        if (canEnterImageAdjust(context, state)) {
          controller.dispatch({ type: "SET_TOOL", tool: TOOLS.IMAGE_ADJUST });
          if (typeof event.preventDefault === "function") event.preventDefault();
          if (typeof event.stopPropagation === "function") event.stopPropagation();
          return;
        }
        const pins = typeof controller.getPins === "function"
          ? controller.getPins() : null;
        if (canQueueImageAdjustShortcut(context, state, pins)) {
          if (typeof event.preventDefault === "function") event.preventDefault();
          void controller.requestQueue("shortcut");
        }
      };
      addListener(removers, controller.surface, "keydown", handleKeyDown);

      addListener(removers, controller.canvas, "wheel", (event) => {
        const state = controller.getState();
        if (!canApplyWheel(
          this.eventContext(event, controller, resource),
          state,
        )) return;
        const step = event.shiftKey === true ? 5 : 1;
        this.setBrightness(
          this.brightness + (Number(event.deltaY) < 0 ? step : -step),
        );
        if (typeof event.preventDefault === "function") event.preventDefault();
      }, { passive: false });

      addListener(removers, brightnessInput, "input", () => {
        const raw = String(brightnessInput.value == null
          ? "" : brightnessInput.value).trim();
        const numeric = raw === "" ? Number.NaN : Number(raw);
        if (!Number.isFinite(numeric)) {
          brightnessInput.setAttribute("aria-invalid", "true");
          return;
        }
        brightnessInput.removeAttribute("aria-invalid");
        this.setBrightness(numeric);
      });
      addListener(removers, brightnessInput, "change", () => {
        brightnessInput.removeAttribute("aria-invalid");
        this.setBrightness(brightnessInput.value);
        brightnessInput.value = String(this.brightness);
      });
      addListener(removers, contrastInput, "input", () => {
        const raw = String(contrastInput.value == null
          ? "" : contrastInput.value).trim();
        const numeric = raw === "" ? Number.NaN : Number(raw);
        if (!Number.isFinite(numeric)) {
          contrastInput.setAttribute("aria-invalid", "true");
          return;
        }
        contrastInput.removeAttribute("aria-invalid");
        this.setAdjustment(createManualBinaryAdjustment(
          this.brightness, clampContrast(numeric)));
      });
      addListener(removers, contrastInput, "change", () => {
        contrastInput.removeAttribute("aria-invalid");
        this.setAdjustment(createManualBinaryAdjustment(
          this.brightness, clampContrast(contrastInput.value)));
        contrastInput.value = String(this.contrast);
      });
      addListener(removers, operationsAddButton, "click", () => {
        const operations = this.stateOperations(record);
        if (operations.length >= MAX_OPERATIONS_PER_RECIPE) return;
        this.dispatchOperations(record, operations.concat([
          createProcessingOperation(operationsAddSelect.value),
        ]));
      });
      addListener(removers, ocrInput, "change", () => {
        this.setRerunOcr(ocrInput.checked === true);
      });
      if (reocrButton) {
        addListener(removers, reocrButton, "click", () => {
          void this.requestStandaloneReocr(record);
        });
      }
      addListener(removers, controller.image, "load", () => {
        this.schedulePreview(record);
      });

      this.refreshMount(false);
      return () => this.unmount(record);
    }

    unmount(record = this.mountRecord) {
      if (!record || record.disposed) return;
      record.disposed = true;
      record.previewGeneration += 1;
      record.pointerOverImage = false;
      for (const remove of record.removers.splice(0)) remove();
      removeNode(record.panel);
      removeNode(record.previewCanvas);
      if (record.canvasOriginalLabel == null) {
        if (typeof record.controller.canvas.removeAttribute === "function") {
          record.controller.canvas.removeAttribute("aria-label");
        }
      } else {
        record.controller.canvas.setAttribute(
          "aria-label",
          record.canvasOriginalLabel,
        );
      }
      if (this.mountRecord === record) this.mountRecord = null;
    }

    syncEditorState(state, resource) {
      if (!this.mountRecord) return;
      if (resource && resource !== this.mountRecord.resource) return;
      this.refreshMount(false, state);
    }

    refreshMount(announce = false, suppliedState = null) {
      const record = this.mountRecord;
      if (!record || record.disposed) return;
      const state = suppliedState || record.controller.getState();
      const active = state.tool === TOOLS.IMAGE_ADJUST;
      setData(record.panel, "active", active);
      record.activeTool.textContent = active
        ? "Active tool: Image Adjust"
        : "Image Adjust inactive";
      record.brightnessInput.disabled = !active;
      record.brightnessInput.value = String(this.brightness);
      record.contrastInput.disabled = !active;
      record.contrastInput.value = String(this.contrast);
      this.renderOperations(record, state, active);
      record.ocrInput.checked = this.rerunOcr;
      if (record.reocrRow) {
        const available = this.reocrCapability &&
          reocrEligibleResource(record.resource);
        record.reocrRow.hidden = !available;
        record.reocrButton.disabled = !available || this.reocrBusy;
      }
      const threshold = thresholdForBrightness(this.brightness);
      const operationCount = Array.isArray(state.operations)
        ? state.operations.length : 0;
      record.thresholdStatus.textContent = this.adjustmentEnabled
        ? `Binarize ${this.contrast}% · brightness ${this.brightness} ` +
          `· binary threshold ${threshold}`
        : `Manual binary adjustment off · ${operationCount} processing ` +
          `operation${operationCount === 1 ? "" : "s"}`;
      const previewVisible = active && this.adjustmentEnabled;
      record.previewCanvas.hidden = !previewVisible;
      record.previewCanvas.setAttribute("aria-hidden", String(!previewVisible));
      record.controller.canvas.setAttribute(
        "aria-label",
        active
          ? "Image Adjust canvas. Use the wheel to change brightness; press Space to queue the transform."
          : record.canvasOriginalLabel || "Image correction canvas",
      );
      if (announce && active) {
        record.jobStatus.textContent =
          `Brightness ${this.brightness}; binary threshold ${threshold}.`;
      }
      if (previewVisible) this.schedulePreview(record);
    }

    stateOperations(record = this.mountRecord) {
      const state = record && !record.disposed
        ? record.controller.getState() : null;
      return state && Array.isArray(state.operations)
        ? state.operations.slice() : [];
    }

    dispatchOperations(record, operations) {
      if (!record || record.disposed) return;
      record.controller.dispatch({
        type: "SET_PROCESSING_OPERATIONS",
        operations,
      });
      // Hosts refresh through onStateChange; the direct call keeps a bare
      // controller (tests, detached mounts) rendering the same truth.
      this.refreshMount(false);
    }

    moveOperation(record, index, delta) {
      const operations = this.stateOperations(record);
      const target = index + delta;
      if (index < 0 || index >= operations.length ||
          target < 0 || target >= operations.length) return;
      const [moved] = operations.splice(index, 1);
      operations.splice(target, 0, moved);
      this.dispatchOperations(record, operations);
    }

    removeOperation(record, index) {
      const operations = this.stateOperations(record);
      if (index < 0 || index >= operations.length) return;
      operations.splice(index, 1);
      this.dispatchOperations(record, operations);
    }

    setOperationParameter(record, index, name, value) {
      const operations = this.stateOperations(record);
      const current = operations[index];
      if (!current) return null;
      let next = value;
      if (name !== "mode") {
        const raw = String(value == null ? "" : value).trim();
        const numeric = raw === "" ? Number.NaN : Number(raw);
        // A cleared or unparseable field keeps the current value instead of
        // silently resetting the recipe to the algorithm default.
        next = Number.isFinite(numeric) ? numeric : current[name];
      }
      operations[index] = createProcessingOperation(current.algorithm, {
        ...current,
        [name]: next,
      });
      this.dispatchOperations(record, operations);
      return operations[index];
    }

    renderOperations(record, state, active) {
      const operations = Array.isArray(state.operations)
        ? state.operations : [];
      record.operationsAddSelect.disabled = !active;
      record.operationsAddButton.disabled = !active ||
        operations.length >= MAX_OPERATIONS_PER_RECIPE;
      const serialized = JSON.stringify(operations);
      // Rebuilding rows drops focus, so an unchanged pipeline (the common
      // refresh) only re-syncs the enabled flags on the existing rows.
      if (serialized === record.renderedOperations &&
          active === record.renderedOperationsActive) return;
      record.renderedOperations = serialized;
      record.renderedOperationsActive = active;
      record.operationsHint.hidden = operations.length === 0;
      record.operationList.replaceChildren();
      operations.forEach((operation, index) => {
        record.operationList.append(this.buildOperationRow(
          record, operation, index, operations.length, active));
      });
    }

    buildOperationRow(record, operation, index, total, active) {
      const documentRef = record.documentRef;
      const label = PROCESSING_OPERATION_LABELS[operation.algorithm] ||
        operation.algorithm;
      const row = element(documentRef, "li", "image-adjust-operation");
      setData(row, "operationAlgorithm", operation.algorithm);
      setData(row, "operationIndex", index);
      const head = element(documentRef, "div", "image-adjust-operation-head");
      const name = element(
        documentRef, "span", "image-adjust-operation-name",
        `${index + 1} · ${label}`,
      );
      const actions = element(
        documentRef, "span", "image-adjust-operation-actions",
      );
      const control = (className, text, ariaLabel, disabled, onClick) => {
        const button = element(documentRef, "button", className, text);
        button.type = "button";
        button.setAttribute("aria-label", ariaLabel);
        button.disabled = disabled;
        button.addEventListener("click", onClick);
        return button;
      };
      actions.append(
        control(
          "image-adjust-operation-up", "↑", `Move ${label} earlier`,
          !active || index === 0,
          () => this.moveOperation(record, index, -1),
        ),
        control(
          "image-adjust-operation-down", "↓", `Move ${label} later`,
          !active || index === total - 1,
          () => this.moveOperation(record, index, 1),
        ),
        control(
          "image-adjust-operation-remove", "✕", `Remove ${label}`,
          !active,
          () => this.removeOperation(record, index),
        ),
      );
      head.append(name, actions);
      const parameters = element(
        documentRef, "div", "image-adjust-operation-params",
      );
      const grayWorld = operation.algorithm === "white-balance-v1" &&
        operation.mode === "gray_world";
      for (const parameterName of Object.keys(
        PROCESSING_OPERATION_PARAMETERS[operation.algorithm],
      )) {
        const wrap = element(documentRef, "label", "image-adjust-operation-param");
        if (parameterName === "mode") {
          wrap.append(element(documentRef, "span", "", "Mode"));
          const mode = element(documentRef, "select");
          for (const [value, text] of [
            ["gray_world", "Gray world"], ["manual", "Manual"],
          ]) {
            const choice = element(documentRef, "option", "", text);
            choice.value = value;
            choice.setAttribute("value", value);
            mode.append(choice);
          }
          mode.value = operation.mode;
          mode.disabled = !active;
          mode.setAttribute("aria-label", `${label} mode`);
          mode.addEventListener("change", () => {
            this.setOperationParameter(record, index, "mode", mode.value);
          });
          wrap.append(mode);
          parameters.append(wrap);
          continue;
        }
        const bounds =
          PROCESSING_OPERATION_PARAMETERS[operation.algorithm][parameterName];
        wrap.append(element(
          documentRef, "span", "",
          PROCESSING_PARAMETER_LABELS[parameterName] || parameterName,
        ));
        const input = element(documentRef, "input");
        input.type = "number";
        input.min = String(bounds[0]);
        input.max = String(bounds[1]);
        input.step = "1";
        input.inputMode = "numeric";
        input.value = String(operation[parameterName]);
        input.disabled = !active || grayWorld &&
          (parameterName === "temperature" || parameterName === "tint");
        input.setAttribute(
          "aria-label",
          `${label} ${PROCESSING_PARAMETER_LABELS[parameterName] || parameterName}`,
        );
        input.addEventListener("change", () => {
          const replacement = this.setOperationParameter(
            record, index, parameterName, input.value);
          if (replacement) input.value = String(replacement[parameterName]);
        });
        wrap.append(input);
        parameters.append(wrap);
      }
      row.append(head, parameters);
      return row;
    }

    schedulePreview(record = this.mountRecord) {
      if (!record || record.disposed ||
          record.controller.getState().tool !== TOOLS.IMAGE_ADJUST ||
          !this.adjustmentEnabled) return;
      const generation = ++record.previewGeneration;
      const adjustment = createManualBinaryAdjustment(
        this.brightness, this.contrast);
      const render = typeof this.options.previewAdapter === "function"
        ? this.options.previewAdapter
        : renderBinaryCanvasPreview;
      const run = () => {
        if (record.disposed || generation !== record.previewGeneration) return;
        let rendered;
        try {
          rendered = render({
            image: record.controller.image,
            canvas: record.previewCanvas,
            adjustment,
            resource: record.resource,
            maxEdge: this.options.previewMaxEdge,
            maxPixels: this.options.previewMaxPixels,
          });
        } catch (error) {
          if (!record.disposed && generation === record.previewGeneration) {
            setData(record.panel, "preview", "unavailable");
            record.jobStatus.textContent =
              "Exact binary preview is unavailable for this image.";
          }
          return;
        }
        Promise.resolve(rendered).then(
          () => {
            if (record.disposed || generation !== record.previewGeneration) return;
            setData(record.panel, "preview", "ready");
          },
          () => {
            if (record.disposed || generation !== record.previewGeneration) return;
            setData(record.panel, "preview", "unavailable");
            record.jobStatus.textContent =
              "Exact binary preview is unavailable for this image.";
          },
        );
      };
      const windowRef = record.documentRef.defaultView;
      if (windowRef && typeof windowRef.requestAnimationFrame === "function") {
        windowRef.requestAnimationFrame(run);
      } else {
        run();
      }
    }

    async requestStandaloneReocr(record = this.mountRecord) {
      if (!record || record.disposed || this.reocrBusy ||
          typeof this.options.requestReocr !== "function" ||
          !this.reocrCapability ||
          !reocrEligibleResource(record.resource)) return null;
      const summary = record.resource &&
        (record.resource.summary || record.resource);
      const request = {
        operationId: this.reocrOperationIdFactory(),
        itemId: summary.itemId,
        artifactId: summary.id,
        expectedArtifactRevision: summary.revision,
      };
      this.reocrBusy = true;
      this.refreshMount(false);
      try {
        const receipt = await this.options.requestReocr(request, {
          resource: record.resource,
        });
        if (!record.disposed && this.mountRecord === record) {
          record.jobStatus.textContent = receipt && receipt.replayed === true
            ? "Re-OCR already queued for this rendition."
            : "Re-OCR queued.";
        }
        return receipt;
      } catch (error) {
        if (!record.disposed && this.mountRecord === record) {
          record.jobStatus.textContent = error && error.message ||
            "Re-OCR could not be queued.";
        }
        return null;
      } finally {
        this.reocrBusy = false;
        this.refreshMount(false);
      }
    }

    handleQueueAccepted(result, command, resource) {
      const normalized = canonicalCommand(command);
      if (!normalized) return false;
      this.pending.set(normalized.operationId, {
        ...normalized,
        resource,
        jobId: result && (result.job_id || result.jobId) || "",
      });
      const pending = this.pending.get(normalized.operationId);
      while (this.pending.size > 64) {
        this.pending.delete(this.pending.keys().next().value);
      }
      const record = this.mountRecord;
      if (record && !record.disposed && normalized.adjustment) {
        record.jobStatus.textContent = pending.jobId
          ? `Image adjustment queued as ${pending.jobId}.`
          : "Image adjustment queued.";
      }
      return true;
    }

    handleCommandError(error) {
      const record = this.mountRecord;
      if (record && !record.disposed) {
        record.jobStatus.textContent = error && error.message
          ? error.message
          : "Image adjustment could not be queued.";
      }
    }

    settleMountedEditor(
      result,
      command,
      pending,
      terminalState,
      imageCommitted,
      operationId,
    ) {
      const record = this.mountRecord;
      if (!record || record.disposed || !terminalState || !command ||
          !operationId || command.operationId !== operationId) return false;
      const state = record.controller.getState();
      const submission = state && state.submission;
      const submitted = canonicalCommand(submission && submission.command);
      if (!submission || submission.status !== "queued" || !submitted ||
          submitted.operationId !== command.operationId) return false;
      const resultJobId = operationIdentifier(
        result && (result.job_id || result.jobId),
      );
      const pendingJobId = operationIdentifier(pending && pending.jobId);
      const submittedJobId = operationIdentifier(submission.jobId);
      if ((resultJobId && pendingJobId && resultJobId !== pendingJobId) ||
          (resultJobId && submittedJobId && resultJobId !== submittedJobId) ||
          (pendingJobId && submittedJobId && pendingJobId !== submittedJobId)) {
        return false;
      }
      record.controller.dispatch({
        type: imageCommitted ? "QUEUE_COMPLETED" : "QUEUE_RESET",
      });
      return true;
    }

    observeTransformResult(result, suppliedCommand = null) {
      const operationId = operationIdentifier(
        result && (result.operation_id ||
          result.image_commit && result.image_commit.operation_id),
      );
      const pending = this.pending.get(operationId);
      const command = canonicalCommand(suppliedCommand) || pending || null;
      const committedId = committedOperation(
        result,
        command && command.command,
      );
      const pendingJobId = operationIdentifier(pending && pending.jobId);
      const resultJobId = operationIdentifier(
        result && (result.job_id || result.jobId),
      );
      const jobIdentityValid = !pendingJobId || !resultJobId ||
        pendingJobId === resultJobId;
      const imageCommitted = Boolean(committedId && jobIdentityValid);
      const invalidImageCommit = Boolean(
        result && isPlainObject(result.image_commit) && !imageCommitted,
      );
      const terminalState = terminalJobState(result, imageCommitted);
      let profileChanged = false;

      const extraction = Boolean(
        command && isPlainObject(command.command && command.command.extraction),
      );
      if (imageCommitted && !extraction && command &&
          command.operationId === committedId && command.adjustment) {
        const lastAppliedBrightness = command.adjustment.brightness_percent;
        if (lastAppliedBrightness !== this.profile.lastAppliedBrightness) {
          this.profile = { lastAppliedBrightness };
          profileChanged = true;
          const profile = this.serializeProfile();
          if (typeof this.options.onProfileChange === "function") {
            this.options.onProfileChange(profile, {
              operationId: committedId,
              reason: "transform-committed",
            });
          }
          for (const listener of this.profileListeners) {
            listener(profile, {
              operationId: committedId,
              reason: "transform-committed",
            });
          }
        }
      }

      const ocrOutcome = imageCommitted
        ? normalizedOcrOutcome(result && result.ocr_followup) : null;
      if (ocrOutcome) {
        this.lastOcrOutcome = ocrOutcome;
        const detail = { operationId, imageCommitted };
        if (typeof this.options.onOcrOutcome === "function") {
          this.options.onOcrOutcome(cloneJson(ocrOutcome), detail);
        }
        for (const listener of this.ocrListeners) {
          listener(cloneJson(ocrOutcome), detail);
        }
      }

      const editorSettled = this.settleMountedEditor(
        result, command, pending, terminalState, imageCommitted, operationId,
      );
      if (operationId && terminalState) this.pending.delete(operationId);
      const record = this.mountRecord;
      if (record && !record.disposed && operationId) {
        if (invalidImageCommit) {
          record.jobStatus.textContent =
            "Image adjustment output was rejected; source and saved brightness " +
            "are unchanged.";
        } else if (imageCommitted && extraction && ocrOutcome &&
                   ocrOutcome.state === "failed") {
          record.jobStatus.textContent =
            "Region extracted; OCR follow-up failed.";
        } else if (imageCommitted && extraction && ocrOutcome &&
                   ocrOutcome.state === "cancelled") {
          record.jobStatus.textContent =
            "Region extracted; OCR follow-up was cancelled.";
        } else if (imageCommitted && extraction) {
          record.jobStatus.textContent = "Region extracted.";
        } else if (imageCommitted && ocrOutcome && ocrOutcome.state === "failed") {
          record.jobStatus.textContent =
            "Image adjustment applied; OCR follow-up failed.";
        } else if (imageCommitted && ocrOutcome &&
                   ocrOutcome.state === "cancelled") {
          record.jobStatus.textContent =
            "Image adjustment applied; OCR follow-up was cancelled.";
        } else if (imageCommitted) {
          record.jobStatus.textContent = "Image adjustment applied.";
        } else if (result && result.cancelled_before_commit === true) {
          record.jobStatus.textContent =
            "Image adjustment cancelled; source and saved brightness are unchanged.";
        } else if (terminalState) {
          record.jobStatus.textContent =
            "Image adjustment failed; source and saved brightness are unchanged.";
        }
      }
      return {
        recognized: Boolean(operationId),
        operationId,
        imageCommitted,
        invalidImageCommit,
        jobIdentityValid,
        terminalState,
        editorSettled,
        profileChanged,
        profile: this.serializeProfile(),
        ocrOutcome,
      };
    }

    destroy() {
      if (this.destroyed) return;
      this.unmount();
      this.profileListeners.clear();
      this.ocrListeners.clear();
      this.pending.clear();
      this.destroyed = true;
    }
  }

  function createImageAdjustTool(options = {}) {
    return new ImageAdjustTool(options);
  }

  function composeImageAdjustRendererOptions(tool, baseOptions = {}) {
    if (!tool || typeof tool.mount !== "function" ||
        typeof tool.getAdjustment !== "function") {
      throw new TypeError("an Image Adjust tool is required");
    }
    const base = { ...baseOptions };
    return {
      ...base,
      canQueue(context) {
        return tool.canQueue(context) ||
          (typeof base.canQueue === "function" &&
            base.canQueue(context) === true);
      },
      getAdjustment(context) {
        const adjustment = tool.getAdjustment(context);
        if (adjustment) return adjustment;
        return typeof base.getAdjustment === "function"
          ? base.getAdjustment(context) : null;
      },
      getRerunOcr(context) {
        if (tool.ownsTransform(context)) return tool.getRerunOcr(context);
        return typeof base.getRerunOcr === "function"
          ? base.getRerunOcr(context) : false;
      },
      onMount(controller, resource) {
        let baseCleanup = null;
        if (typeof base.onMount === "function") {
          baseCleanup = base.onMount(controller, resource);
        }
        let toolCleanup;
        try {
          toolCleanup = tool.mount(controller, resource);
        } catch (error) {
          if (typeof baseCleanup === "function") baseCleanup();
          throw error;
        }
        return () => {
          if (typeof toolCleanup === "function") toolCleanup();
          if (typeof baseCleanup === "function") baseCleanup();
        };
      },
      onStateChange(state, resource) {
        tool.syncEditorState(state, resource);
        if (typeof base.onStateChange === "function") {
          base.onStateChange(state, resource);
        }
      },
      onQueueResult(result, command, resource) {
        tool.handleQueueAccepted(result, command, resource);
        let observation = null;
        if (isPlainObject(result) && (
          Object.prototype.hasOwnProperty.call(result, "image_commit") ||
          Object.prototype.hasOwnProperty.call(result, "cancelled_before_commit") ||
          Object.prototype.hasOwnProperty.call(result, "ocr_followup")
        )) {
          observation = tool.observeTransformResult(result, command);
        }
        if (typeof base.onQueueResult === "function") {
          base.onQueueResult(result, command, resource, observation);
        }
      },
      onCommandError(error, resource) {
        tool.handleCommandError(error, resource);
        if (typeof base.onCommandError === "function") {
          base.onCommandError(error, resource);
        }
      },
    };
  }

  return {
    BINARY_ALGORITHM,
    BRIGHTNESS_MAX,
    BRIGHTNESS_MIN,
    CONTRAST_MAX,
    CONTRAST_MIN,
    DEFAULT_CONTRAST,
    IMAGE_ADJUST_PROFILE_DEFAULT,
    IMAGE_ADJUST_PROFILE_KEY,
    ImageAdjustTool,
    PROCESSING_OPERATION_DEFAULTS,
    PROCESSING_OPERATION_LABELS,
    THRESHOLD_RULE,
    applyManualBinaryPreview,
    canApplyWheel,
    canEnterImageAdjust,
    canQueueImageAdjustShortcut,
    clampBrightness,
    clampContrast,
    clampOperationParameter,
    composeImageAdjustRendererOptions,
    createImageAdjustTool,
    createManualBinaryAdjustment,
    createProcessingOperation,
    normalizeImageAdjustProfile,
    previewDimensions,
    renderBinaryCanvasPreview,
    reocrEligibleResource,
    serializeImageAdjustProfile,
    thresholdForBrightness,
  };
});

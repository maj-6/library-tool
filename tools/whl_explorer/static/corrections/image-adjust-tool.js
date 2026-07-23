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
    TOOLS,
    isFormControlTarget,
    normalizeManualAdjustment,
    sourcePinsValid,
  } = stateApi;

  const IMAGE_ADJUST_PROFILE_KEY = "imageAdjust";
  const IMAGE_ADJUST_PROFILE_DEFAULT = Object.freeze({
    lastAppliedBrightness: 0,
  });
  const BRIGHTNESS_MIN = -100;
  const BRIGHTNESS_MAX = 100;
  const DEFAULT_CONTRAST = 100;
  const THRESHOLD_RULE =
    "round_half_up(127.5 - brightness_percent * 1.275), clamped_0_255";
  const BINARY_ALGORITHM = "grayscale-threshold-blend-v1";
  const TERMINAL_OCR_STATES = new Set([
    "not_requested", "succeeded", "failed", "cancelled",
  ]);
  let toolSequence = 0;

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

  function defaultModalQuery(documentRef) {
    if (!documentRef || typeof documentRef.querySelector !== "function") return false;
    return Boolean(documentRef.querySelector(
      "dialog[open], [role='dialog'][aria-modal='true']",
    ));
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

  function committedOperation(result) {
    if (!isPlainObject(result) || result.cancelled_before_commit === true ||
        !isPlainObject(result.image_commit)) return "";
    const outer = operationIdentifier(result.operation_id);
    const inner = operationIdentifier(result.image_commit.operation_id);
    if (!outer || !inner || outer !== inner ||
        !Array.isArray(result.image_commit.outputs)) return "";
    return outer;
  }

  class ImageAdjustTool {
    constructor(options = {}) {
      this.options = options;
      this.profile = normalizeImageAdjustProfile(options.profile);
      this.brightness = this.profile.lastAppliedBrightness;
      this.rerunOcr = options.rerunOcr === true;
      this.pending = new Map();
      this.mountRecord = null;
      this.profileListeners = new Set();
      this.ocrListeners = new Set();
      this.lastOcrOutcome = null;
      this.destroyed = false;
    }

    restoreProfile(value) {
      this.profile = normalizeImageAdjustProfile(value);
      if (!this.mountRecord) this.brightness = this.profile.lastAppliedBrightness;
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
        contrast: DEFAULT_CONTRAST,
        rememberedBrightness: this.profile.lastAppliedBrightness,
        rerunOcr: this.rerunOcr,
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
      if (brightness === this.brightness) return brightness;
      this.brightness = brightness;
      this.refreshMount(detail.announce !== false);
      return brightness;
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
      return this.ownsTransform(context)
        ? createManualBinaryAdjustment(this.brightness)
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
        : defaultModalQuery(documentRef);
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

      const contrastRow = element(documentRef, "div", "image-adjust-control");
      const contrastLabel = element(documentRef, "span", "", "Contrast");
      const contrastOutput = element(documentRef, "output", "", "100");
      contrastOutput.setAttribute("aria-label", "Contrast percent");
      contrastOutput.setAttribute("aria-live", "off");
      contrastRow.append(contrastLabel, contrastOutput);

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

      const ocrRow = element(documentRef, "div", "image-adjust-ocr-control");
      const ocrId = `${instanceId}-rerun-ocr`;
      const ocrInput = element(documentRef, "input");
      ocrInput.id = ocrId;
      ocrInput.type = "checkbox";
      const ocrLabel = element(documentRef, "label", "", "Re-run OCR");
      ocrLabel.htmlFor = ocrId;
      ocrRow.append(ocrInput, ocrLabel);

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
        ocrRow,
        thresholdStatus,
        jobStatus,
      );
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
        brightnessInput,
        ocrInput,
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
      addListener(removers, ocrInput, "change", () => {
        this.setRerunOcr(ocrInput.checked === true);
      });
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
      record.ocrInput.checked = this.rerunOcr;
      const threshold = thresholdForBrightness(this.brightness);
      record.thresholdStatus.textContent =
        `Contrast 100 · brightness ${this.brightness} · binary threshold ${threshold}`;
      record.previewCanvas.hidden = !active;
      record.previewCanvas.setAttribute("aria-hidden", String(!active));
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
      if (active) this.schedulePreview(record);
    }

    schedulePreview(record = this.mountRecord) {
      if (!record || record.disposed ||
          record.controller.getState().tool !== TOOLS.IMAGE_ADJUST) return;
      const generation = ++record.previewGeneration;
      const adjustment = createManualBinaryAdjustment(this.brightness);
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

    observeTransformResult(result, suppliedCommand = null) {
      const operationId = operationIdentifier(
        result && (result.operation_id ||
          result.image_commit && result.image_commit.operation_id),
      );
      const pending = this.pending.get(operationId);
      const command = canonicalCommand(suppliedCommand) || pending || null;
      const committedId = committedOperation(result);
      const imageCommitted = Boolean(committedId);
      let profileChanged = false;

      if (imageCommitted && command &&
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

      const ocrOutcome = normalizedOcrOutcome(result && result.ocr_followup);
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

      if (operationId && (imageCommitted ||
          result && result.cancelled_before_commit === true ||
          ocrOutcome)) {
        this.pending.delete(operationId);
      }
      const record = this.mountRecord;
      if (record && !record.disposed && operationId) {
        if (imageCommitted && ocrOutcome && ocrOutcome.state === "failed") {
          record.jobStatus.textContent =
            "Image adjustment applied; OCR follow-up failed.";
        } else if (imageCommitted) {
          record.jobStatus.textContent = "Image adjustment applied.";
        } else if (result && result.cancelled_before_commit === true) {
          record.jobStatus.textContent =
            "Image adjustment cancelled; source and saved brightness are unchanged.";
        }
      }
      return {
        recognized: Boolean(operationId),
        operationId,
        imageCommitted,
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
        if (isPlainObject(result) && (
          Object.prototype.hasOwnProperty.call(result, "image_commit") ||
          Object.prototype.hasOwnProperty.call(result, "cancelled_before_commit") ||
          Object.prototype.hasOwnProperty.call(result, "ocr_followup")
        )) {
          tool.observeTransformResult(result, command);
        }
        if (typeof base.onQueueResult === "function") {
          base.onQueueResult(result, command, resource);
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
    DEFAULT_CONTRAST,
    IMAGE_ADJUST_PROFILE_DEFAULT,
    IMAGE_ADJUST_PROFILE_KEY,
    ImageAdjustTool,
    THRESHOLD_RULE,
    applyManualBinaryPreview,
    canApplyWheel,
    canEnterImageAdjust,
    canQueueImageAdjustShortcut,
    clampBrightness,
    composeImageAdjustRendererOptions,
    createImageAdjustTool,
    createManualBinaryAdjustment,
    normalizeImageAdjustProfile,
    previewDimensions,
    renderBinaryCanvasPreview,
    serializeImageAdjustProfile,
    thresholdForBrightness,
  };
});

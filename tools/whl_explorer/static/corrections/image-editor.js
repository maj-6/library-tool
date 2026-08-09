(function installCorrectionsImageEditor(root, factory) {
  const dependencies = typeof module === "object" && module.exports
    ? require("./image-editor-state")
    : root.LibraryToolCorrections;
  const api = factory(dependencies);
  if (typeof module === "object" && module.exports) module.exports = api;
  else Object.assign(root.LibraryToolCorrections ||= {}, api);
})(typeof globalThis !== "undefined" ? globalThis : this, function imageEditorFactory(stateApi) {
  "use strict";

  const {
    POINT_LABELS,
    TOOLS,
    TRANSFORM_COMMAND_ID,
    canQueuePerspectiveShortcut,
    canQueueTransform,
    FREEHAND_MIN_STEP,
    POLYGON_CLOSE_DISTANCE,
    clientToNormalized,
    createImageEditorState,
    isFormControlTarget,
    nearestCornerIndex,
    reduceImageEditorState,
    resolveEscape,
    serializeCorrectionTransformCommand,
    sourcePinsValid,
    visibleModal,
  } = stateApi;

  const TOOL_DEFINITIONS = Object.freeze([
    Object.freeze({ id: TOOLS.SELECT, label: "Select", shortcut: "" }),
    Object.freeze({ id: TOOLS.PERSPECTIVE, label: "Perspective", shortcut: "" }),
    Object.freeze({ id: TOOLS.IMAGE_ADJUST, label: "Image Adjust", shortcut: "" }),
    Object.freeze({ id: TOOLS.POLYGON, label: "Mask", shortcut: "" }),
  ]);
  const CORNER_CODES = Object.freeze(["TL", "TR", "BR", "BL"]);
  let rendererSequence = 0;

  function isPlainObject(value) {
    if (!value || typeof value !== "object" || Array.isArray(value)) return false;
    const prototype = Object.getPrototypeOf(value);
    return prototype === Object.prototype || prototype === null;
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

  function setBooleanAttribute(node, name, value) {
    node.setAttribute(name, String(Boolean(value)));
  }

  function removeAttribute(node, name) {
    if (typeof node.removeAttribute === "function") node.removeAttribute(name);
  }

  function safeRasterUrl(resource) {
    const value = String(resource && (
      resource.url || resource.resource_url || resource.content_url
    ) || "").trim();
    if (!value || /[\u0000-\u001f]/.test(value) ||
        /^(?:javascript|file|filesystem):/i.test(value)) return "";
    if (/^data:/i.test(value) && !/^data:image\//i.test(value)) return "";
    return value;
  }

  function resourceLabel(resource) {
    const value = resource && (
      resource.label || resource.name || resource.id || resource.artifact_id
    );
    return String(value || "image artifact").slice(0, 240);
  }

  function correctionResourceContract(resource) {
    const correction = resource && isPlainObject(resource.correction)
      ? resource.correction : null;
    if (!correction) {
      return {
        pins: null,
        proposal: null,
      };
    }
    return {
      pins: {
        item_id: correction.item_id,
        artifact_id: correction.artifact_id,
        artifact_revision: correction.artifact_revision,
        source_revision: correction.source_revision,
        source_sha256: correction.source_sha256,
      },
      proposal: correction.proposal || null,
    };
  }

  function originalBackupContract(resource) {
    const resourceRef = resource && isPlainObject(resource.resourceRef)
      ? resource.resourceRef : null;
    if (!resourceRef || resourceRef.variant !== "display") return null;
    const extensions = resource && isPlainObject(resource.extensions)
      ? resource.extensions : null;
    const backup = extensions && isPlainObject(extensions.original_backup)
      ? extensions.original_backup : null;
    return backup && backup.available === true ? backup : null;
  }

  function serializeProcessingPresetCommand({ resource, preset, operationId } = {}) {
    if (!isPlainObject(preset) || !Array.isArray(preset.operations)) {
      throw new TypeError("a canonical processing preset is required");
    }
    const contract = correctionResourceContract(resource);
    const initial = createImageEditorState({
      proposal: contract.proposal,
      sourceRevision: contract.pins && contract.pins.source_revision,
      hasSelection: true,
    });
    return serializeCorrectionTransformCommand({
      pins: contract.pins,
      quad: initial.quad,
      adjustment: preset.adjustment,
      operations: preset.operations.length ? preset.operations : null,
      rerunOcr: false,
      operationId,
    });
  }

  function defaultOperationId(windowRef, sequence) {
    const cryptoRef = windowRef && windowRef.crypto;
    if (cryptoRef && typeof cryptoRef.randomUUID === "function") {
      return `correction:${cryptoRef.randomUUID()}`;
    }
    return `correction:${Date.now().toString(36)}:${sequence.toString(36)}`;
  }

  function queueStatusText(submission) {
    switch (submission.status) {
      case "submitting":
        return "Queueing perspective transformation\u2026";
      case "queued":
        return submission.jobId
          ? `Perspective transformation queued as ${submission.jobId}.`
          : "Perspective transformation queued.";
      case "retryable":
        return submission.error || "Queue result uncertain. Retry will reuse the same command.";
      case "failed":
        return submission.error || "Perspective transformation could not be queued.";
      case "complete":
        return "Perspective transformation completed.";
      default:
        return "";
    }
  }

  function proposalStatusText(quadSource) {
    if (quadSource.kind === "proposal") {
      const confidence = quadSource.confidence == null
        ? "" : ` \u00b7 ${Math.round(quadSource.confidence * 100)}%`;
      return `Auto proposal${confidence}`;
    }
    if (quadSource.kind === "fallback") return "Full-image fallback";
    return quadSource.basedOn === "proposal"
      ? "Edited auto proposal" : "Edited full-image fallback";
  }

  function addListener(removers, target, type, handler, options) {
    if (!target || typeof target.addEventListener !== "function") return;
    target.addEventListener(type, handler, options);
    removers.push(() => target.removeEventListener(type, handler, options));
  }

  function contextMethod(context, name, ...args) {
    if (context && typeof context[name] === "function") {
      return context[name](...args);
    }
    return undefined;
  }

  function drawPerspectiveOverlay(canvas, state, windowRef) {
    if (!canvas || typeof canvas.getContext !== "function" ||
        typeof canvas.getBoundingClientRect !== "function") return false;
    const context = canvas.getContext("2d");
    if (!context) return false;
    const rect = canvas.getBoundingClientRect();
    if (!Number.isFinite(rect.width) || !Number.isFinite(rect.height) ||
        rect.width <= 0 || rect.height <= 0) return false;
    const pixelRatio = Math.max(1, Math.min(4, Number(
      windowRef && windowRef.devicePixelRatio || 1,
    ) || 1));
    const width = Math.max(1, Math.round(rect.width * pixelRatio));
    const height = Math.max(1, Math.round(rect.height * pixelRatio));
    if (canvas.width !== width) canvas.width = width;
    if (canvas.height !== height) canvas.height = height;

    contextMethod(context, "setTransform", pixelRatio, 0, 0, pixelRatio, 0, 0);
    contextMethod(context, "clearRect", 0, 0, rect.width, rect.height);
    contextMethod(context, "save");

    const points = state.quad.map(([x, y]) => [x * rect.width, y * rect.height]);
    const invalid = !state.validation.valid;
    const basedOnFallback = state.quadSource.basedOn === "fallback";
    context.lineWidth = state.gesture ? 2.5 : 2;
    context.strokeStyle = invalid ? "#ff7770"
      : basedOnFallback ? "#f0b45e" : "#9ad77d";
    context.fillStyle = invalid ? "rgba(255, 83, 77, 0.12)"
      : basedOnFallback ? "rgba(240, 180, 94, 0.10)" : "rgba(154, 215, 125, 0.10)";
    contextMethod(
      context,
      "setLineDash",
      basedOnFallback && state.quadSource.kind !== "user-edited" ? [9, 6] : [],
    );
    contextMethod(context, "beginPath");
    contextMethod(context, "moveTo", points[0][0], points[0][1]);
    for (let index = 1; index < points.length; index += 1) {
      contextMethod(context, "lineTo", points[index][0], points[index][1]);
    }
    contextMethod(context, "closePath");
    contextMethod(context, "fill");
    contextMethod(context, "stroke");
    contextMethod(context, "setLineDash", []);

    context.font = "600 11px system-ui, sans-serif";
    context.textAlign = "center";
    context.textBaseline = "middle";
    for (let index = 0; index < points.length; index += 1) {
      const [x, y] = points[index];
      const selected = state.selectedCorner === index;
      const radius = selected ? 8 : 6;
      contextMethod(context, "beginPath");
      contextMethod(context, "arc", x, y, radius, 0, Math.PI * 2);
      context.fillStyle = invalid ? "#ff7770"
        : selected ? "#e8f7dc" : basedOnFallback ? "#f0b45e" : "#9ad77d";
      contextMethod(context, "fill");
      context.lineWidth = 2;
      context.strokeStyle = "#111611";
      contextMethod(context, "stroke");

      const labelX = Math.max(16, Math.min(rect.width - 16, x));
      const labelY = Math.max(15, Math.min(rect.height - 15, y + (
        index < 2 ? 18 : -18
      )));
      context.fillStyle = "#f5f7ef";
      contextMethod(context, "fillText", CORNER_CODES[index], labelX, labelY);
    }
    drawMaskOverlay(context, state, rect);
    contextMethod(context, "restore");
    return true;
  }

  function drawMaskOverlay(context, state, rect) {
    const draft = state.maskDraft;
    const committed = state.maskPolygon;
    const ring = draft || committed;
    if (!ring || !ring.length) return;
    const points = ring.map(([x, y]) => [x * rect.width, y * rect.height]);
    contextMethod(context, "save");
    context.lineWidth = 2;
    // Cyan reads as a different kind of object from the green/amber page
    // boundary, so a mask is never mistaken for a quad edit.
    context.strokeStyle = draft ? "#7fd6e8" : "#4fb6cc";
    context.fillStyle = "rgba(79, 182, 204, 0.14)";
    contextMethod(context, "setLineDash", draft ? [6, 4] : []);
    contextMethod(context, "beginPath");
    contextMethod(context, "moveTo", points[0][0], points[0][1]);
    for (let index = 1; index < points.length; index += 1) {
      contextMethod(context, "lineTo", points[index][0], points[index][1]);
    }
    if (!draft) contextMethod(context, "closePath");
    if (!draft || points.length > 2) contextMethod(context, "fill");
    contextMethod(context, "stroke");
    contextMethod(context, "setLineDash", []);
    if (draft && !state.maskFreehand) {
      for (const [x, y] of points) {
        contextMethod(context, "beginPath");
        contextMethod(context, "arc", x, y, 3.5, 0, Math.PI * 2);
        context.fillStyle = "#7fd6e8";
        contextMethod(context, "fill");
      }
    }
    contextMethod(context, "restore");
  }

  function createPerspectiveImageRenderer(options = {}) {
    const invokeCommand = typeof options.invokeCommand === "function"
      ? options.invokeCommand : null;
    const commandId = String(options.commandId || TRANSFORM_COMMAND_ID);
    const initialTool = Object.values(TOOLS).includes(options.initialTool)
      ? options.initialTool : TOOLS.SELECT;
    let operationSequence = 0;

    return function renderPerspectiveImage({
      container,
      documentRef,
      resource,
    }) {
      if (!container || !documentRef) {
        throw new TypeError("perspective image renderer requires a container and document");
      }
      if (typeof container.replaceChildren === "function") container.replaceChildren();
      const removers = [];
      let destroyed = false;
      let observer = null;
      let mountCleanup = null;
      const instanceId = `perspective-editor-${++rendererSequence}`;
      const url = safeRasterUrl(resource);

      if (!url) {
        const unavailable = element(documentRef, "section", "editor-unsupported");
        unavailable.append(
          element(documentRef, "h2", "", "Image unavailable"),
          element(
            documentRef,
            "p",
            "",
            "This artifact has no safe renderable image resource.",
          ),
        );
        container.append(unavailable);
        return () => {};
      }

      const contract = correctionResourceContract(resource);
      let state = createImageEditorState({
        proposal: contract.proposal,
        sourceRevision: contract.pins && contract.pins.source_revision,
        tool: initialTool,
        hasSelection: true,
      });
      const numericInvalid = new Set();
      let maskPointerId = null;
      let maskDragged = false;

      const surface = element(documentRef, "section", "perspective-editor");
      surface.tabIndex = 0;
      surface.setAttribute("role", "region");
      surface.setAttribute(
        "aria-label",
        `Perspective correction editor for ${resourceLabel(resource)}`,
      );

      const toolbar = element(documentRef, "header", "perspective-toolbar");
      const toolGroup = element(documentRef, "div", "perspective-tool-group");
      toolGroup.setAttribute("role", "toolbar");
      toolGroup.setAttribute("aria-label", "Image editor tools");
      const toolButtons = new Map();
      for (const definition of TOOL_DEFINITIONS) {
        const button = element(
          documentRef,
          "button",
          "perspective-tool-button",
          definition.label,
        );
        button.type = "button";
        setData(button, "imageTool", definition.id);
        setBooleanAttribute(button, "aria-pressed", definition.id === state.tool);
        if (definition.shortcut) button.setAttribute("aria-keyshortcuts", definition.shortcut);
        toolButtons.set(definition.id, button);
        toolGroup.append(button);
      }

      const historyGroup = element(documentRef, "div", "perspective-history-group");
      const undoButton = element(documentRef, "button", "", "Undo");
      undoButton.type = "button";
      undoButton.setAttribute("aria-keyshortcuts", "Control+Z");
      const redoButton = element(documentRef, "button", "", "Redo");
      redoButton.type = "button";
      redoButton.setAttribute("aria-keyshortcuts", "Control+Shift+Z");
      historyGroup.append(undoButton, redoButton);

      const originalBackup = originalBackupContract(resource);
      let originalActions = null;
      let viewOriginalButton = null;
      let restoreOriginalButton = null;
      if (originalBackup) {
        originalActions = element(
          documentRef,
          "div",
          "perspective-original-actions",
        );
        originalActions.setAttribute("role", "group");
        originalActions.setAttribute("aria-label", "Original capture backup");
        viewOriginalButton = element(
          documentRef,
          "button",
          "perspective-view-original",
          "View original",
        );
        viewOriginalButton.type = "button";
        setData(viewOriginalButton, "originalAction", "view");
        originalActions.append(viewOriginalButton);
        if (originalBackup.restore_available === true) {
          restoreOriginalButton = element(
            documentRef,
            "button",
            "perspective-restore-original",
            "Restore original",
          );
          restoreOriginalButton.type = "button";
          setData(restoreOriginalButton, "originalAction", "restore");
          originalActions.append(restoreOriginalButton);
        }
      }

      const queueButton = element(
        documentRef,
        "button",
        "perspective-queue-button",
        "Queue transform",
      );
      queueButton.type = "button";
      queueButton.setAttribute("aria-keyshortcuts", "Space");
      setData(queueButton, "commandId", commandId);
      const proposalBadge = element(documentRef, "span", "perspective-proposal-badge");
      setData(proposalBadge, "quadSource", state.quadSource.kind);
      toolbar.append(toolGroup, historyGroup);
      if (originalActions) toolbar.append(originalActions);
      toolbar.append(proposalBadge, queueButton);

      const editorLayout = element(documentRef, "div", "perspective-editor-layout");
      const viewport = element(documentRef, "div", "perspective-viewport");
      const imageStage = element(documentRef, "div", "perspective-image-stage");
      const image = element(documentRef, "img", "perspective-image");
      image.src = url;
      image.alt = resourceLabel(resource);
      image.decoding = "async";
      image.draggable = false;
      const canvas = element(documentRef, "canvas", "perspective-overlay-canvas");
      canvas.tabIndex = 0;
      setData(canvas, "classificationCanvas", "true");
      canvas.setAttribute("role", "img");
      canvas.setAttribute(
        "aria-label",
        "Four-corner perspective boundary. Click or drag the closest corner in Perspective mode.",
      );
      imageStage.append(image, canvas);
      viewport.append(imageStage);

      const inspector = element(documentRef, "aside", "perspective-corner-inspector");
      inspector.setAttribute("aria-label", "Perspective corner coordinates");
      const fieldset = element(documentRef, "fieldset", "perspective-corner-fieldset");
      const legend = element(documentRef, "legend", "", "Perspective corners");
      const coordinateHint = element(
        documentRef,
        "p",
        "perspective-coordinate-hint",
        "Coordinates are normalized to the EXIF-oriented image from 0 to 1.",
      );
      coordinateHint.id = `${instanceId}-coordinate-hint`;
      const validationStatus = element(documentRef, "p", "perspective-validation-status");
      validationStatus.id = `${instanceId}-validation`;
      validationStatus.setAttribute("role", "status");
      validationStatus.setAttribute("aria-live", "polite");
      const cornerControls = [];
      fieldset.append(legend, coordinateHint);
      for (let cornerIndex = 0; cornerIndex < POINT_LABELS.length; cornerIndex += 1) {
        const row = element(documentRef, "div", "perspective-corner-row");
        setData(row, "cornerIndex", cornerIndex);
        const selectButton = element(
          documentRef,
          "button",
          "perspective-corner-select",
          `${CORNER_CODES[cornerIndex]} \u00b7 ${POINT_LABELS[cornerIndex]}`,
        );
        selectButton.type = "button";
        selectButton.setAttribute(
          "aria-label",
          `Select ${POINT_LABELS[cornerIndex].toLowerCase()} corner`,
        );
        setBooleanAttribute(selectButton, "aria-pressed", false);
        const coordinates = element(documentRef, "div", "perspective-coordinate-pair");
        const inputs = [];
        for (const [axisIndex, axisName] of [["0", "X"], ["1", "Y"]]) {
          const control = element(documentRef, "div", "perspective-coordinate-control");
          const inputId = `${instanceId}-corner-${cornerIndex}-${axisName.toLowerCase()}`;
          const label = element(
            documentRef,
            "label",
            "",
            `${POINT_LABELS[cornerIndex]} ${axisName}`,
          );
          label.htmlFor = inputId;
          const input = element(documentRef, "input");
          input.id = inputId;
          input.type = "number";
          input.min = "0";
          input.max = "1";
          input.step = "0.001";
          input.inputMode = "decimal";
          input.setAttribute(
            "aria-describedby",
            `${coordinateHint.id} ${validationStatus.id}`,
          );
          setData(input, "cornerIndex", cornerIndex);
          setData(input, "axisIndex", axisIndex);
          control.append(label, input);
          coordinates.append(control);
          inputs.push(input);
        }
        row.append(selectButton, coordinates);
        fieldset.append(row);
        cornerControls.push({ row, selectButton, inputs });
      }
      fieldset.append(validationStatus);
      const queueStatus = element(documentRef, "p", "perspective-queue-status");
      queueStatus.setAttribute("role", "status");
      queueStatus.setAttribute("aria-live", "polite");
      const originalStatus = originalBackup
        ? element(documentRef, "p", "perspective-original-status") : null;
      if (originalStatus) {
        originalStatus.setAttribute("role", "status");
        originalStatus.setAttribute("aria-live", "polite");
      }
      const instruction = element(documentRef, "p", "perspective-tool-instruction");
      instruction.id = `${instanceId}-instruction`;
      canvas.setAttribute(
        "aria-describedby",
        `${instruction.id} ${validationStatus.id}`,
      );
      inspector.append(fieldset, instruction, queueStatus);
      if (originalStatus) inspector.append(originalStatus);
      editorLayout.append(viewport, inspector);
      surface.append(toolbar, editorLayout);
      container.append(surface);

      const windowRef = documentRef.defaultView || null;
      const modalOpen = () => typeof options.isModalOpen === "function"
        ? options.isModalOpen(documentRef) === true
        : visibleModal(documentRef);
      const hostHasSelection = () => typeof options.hasSelection === "function"
        ? options.hasSelection(resource) === true
        : state.selectionPresent;
      let originalBusy = "";
      let originalStatusText = "";
      let originalStatusError = false;
      let originalPreviewDialog = null;
      let originalPreviewImage = null;
      let originalPreviewUrl = "";
      let originalPreviewUrlApi = null;
      let restoreOperationId = "";

      function originalIdentity() {
        const pins = contract.pins || {};
        return {
          itemId: String(pins.item_id || resource.itemId || ""),
          artifactId: String(pins.artifact_id || resource.id || ""),
          revision: String(
            pins.artifact_revision ||
            resource.summary && resource.summary.revision || "",
          ),
        };
      }

      function originalApi() {
        const engine = options.engine || options.engineClient ||
          windowRef && windowRef.engineClient;
        return engine && engine.rasterArtifacts || null;
      }

      function objectUrlApi() {
        if (options.objectUrls) return options.objectUrls;
        if (windowRef && windowRef.URL) return windowRef.URL;
        return typeof URL !== "undefined" ? URL : null;
      }

      function releaseOriginalPreview() {
        if (originalPreviewImage) removeAttribute(originalPreviewImage, "src");
        if (originalPreviewUrl && originalPreviewUrlApi &&
            typeof originalPreviewUrlApi.revokeObjectURL === "function") {
          originalPreviewUrlApi.revokeObjectURL(originalPreviewUrl);
        }
        originalPreviewUrl = "";
        originalPreviewUrlApi = null;
      }

      function closeOriginalPreview() {
        if (originalPreviewDialog) {
          if (typeof originalPreviewDialog.close === "function" &&
              originalPreviewDialog.open === true) {
            try { originalPreviewDialog.close(); } catch (error) {}
          }
          removeAttribute(originalPreviewDialog, "open");
        }
        releaseOriginalPreview();
      }

      function ensureOriginalPreviewDialog() {
        if (originalPreviewDialog) return originalPreviewDialog;
        const dialog = element(
          documentRef,
          "dialog",
          "perspective-original-dialog",
        );
        dialog.setAttribute("aria-label", "Original capture backup");
        const heading = element(documentRef, "h2", "", "Original capture");
        const description = element(
          documentRef,
          "p",
          "perspective-original-dialog-note",
          "Verified original camera bytes from the backup store.",
        );
        const preview = element(
          documentRef,
          "img",
          "perspective-original-preview",
        );
        preview.alt = `Original capture for ${resourceLabel(resource)}`;
        const closeButton = element(
          documentRef,
          "button",
          "perspective-original-close",
          "Close",
        );
        closeButton.type = "button";
        dialog.append(heading, description, preview, closeButton);
        surface.append(dialog);
        addListener(removers, closeButton, "click", closeOriginalPreview);
        addListener(removers, dialog, "cancel", (event) => {
          if (typeof event.preventDefault === "function") event.preventDefault();
          closeOriginalPreview();
        });
        addListener(removers, dialog, "close", releaseOriginalPreview);
        originalPreviewDialog = dialog;
        originalPreviewImage = preview;
        return dialog;
      }

      function presentOriginal(resolved) {
        if (!resolved || !resolved.blob ||
            !Number.isFinite(Number(resolved.blob.size)) ||
            Number(resolved.blob.size) < 1) {
          throw new TypeError("The original backup response has no image bytes");
        }
        const api = objectUrlApi();
        if (!api || typeof api.createObjectURL !== "function" ||
            typeof api.revokeObjectURL !== "function") {
          throw new Error("This browser cannot open the original backup");
        }
        closeOriginalPreview();
        const url = api.createObjectURL(resolved.blob);
        if (typeof url !== "string" || !/^blob:[^\s]+$/.test(url)) {
          if (typeof url === "string" && url) api.revokeObjectURL(url);
          throw new Error("The browser returned an unsafe original image URL");
        }
        originalPreviewUrl = url;
        originalPreviewUrlApi = api;
        const dialog = ensureOriginalPreviewDialog();
        originalPreviewImage.src = url;
        if (typeof dialog.showModal === "function") {
          try { dialog.showModal(); } catch (error) {
            dialog.setAttribute("open", "");
          }
        } else {
          dialog.setAttribute("open", "");
        }
      }

      function reportOriginalError(error, fallback) {
        originalStatusText = error && error.message || fallback;
        originalStatusError = true;
        if (typeof options.onCommandError === "function") {
          options.onCommandError(error, resource);
        }
      }

      async function requestViewOriginal() {
        const identity = originalIdentity();
        const api = originalApi();
        if (destroyed || originalBusy || !originalBackup || !api ||
            typeof api.resolveOriginalBackup !== "function" ||
            !identity.itemId || !identity.artifactId || !identity.revision) {
          return null;
        }
        originalBusy = "view";
        originalStatusText = "Retrieving and verifying the original…";
        originalStatusError = false;
        update();
        try {
          const resolved = await api.resolveOriginalBackup({
            itemId: identity.itemId,
            artifactId: identity.artifactId,
            revision: identity.revision,
          });
          if (destroyed) return null;
          presentOriginal(resolved);
          originalStatusText = "Verified original opened from backup.";
          return resolved;
        } catch (error) {
          if (!destroyed) reportOriginalError(
            error,
            "The original backup could not be opened",
          );
          return null;
        } finally {
          originalBusy = "";
          update();
        }
      }

      async function confirmOriginalRestore(identity) {
        const message =
          "Restore the original camera image? This will replace the corrected " +
          "display while keeping its correction history.";
        if (typeof options.confirmRestoreOriginal === "function") {
          return (await options.confirmRestoreOriginal({
            message,
            resource,
            ...identity,
          })) === true;
        }
        return Boolean(windowRef && typeof windowRef.confirm === "function" &&
          windowRef.confirm(message));
      }

      async function requestRestoreOriginal() {
        const identity = originalIdentity();
        const api = originalApi();
        if (destroyed || originalBusy || !originalBackup ||
            originalBackup.restore_available !== true || !api ||
            typeof api.restoreOriginalBackup !== "function" ||
            !identity.itemId || !identity.artifactId || !identity.revision) {
          return null;
        }
        originalBusy = "confirm";
        update();
        let confirmed = false;
        try {
          confirmed = await confirmOriginalRestore(identity);
        } catch (error) {
          originalBusy = "";
          reportOriginalError(error, "The restore confirmation failed");
          update();
          return null;
        }
        if (!confirmed || destroyed) {
          originalBusy = "";
          update();
          return null;
        }
        if (!restoreOperationId) restoreOperationId = operationId();
        originalBusy = "restore";
        originalStatusText = "Restoring the original display…";
        originalStatusError = false;
        update();
        try {
          const receipt = await api.restoreOriginalBackup({
            itemId: identity.itemId,
            artifactId: identity.artifactId,
            expectedArtifactRevision: identity.revision,
            idempotencyKey: restoreOperationId,
          });
          restoreOperationId = "";
          if (destroyed) return receipt;
          closeOriginalPreview();
          originalStatusText = "Original display restored.";
          if (typeof options.refreshAfterOriginalRestore === "function") {
            try {
              await options.refreshAfterOriginalRestore({
                itemId: identity.itemId,
                artifactId: identity.artifactId,
                preserveSelection: true,
                receipt,
                resource,
              });
            } catch (refreshError) {
              reportOriginalError(
                refreshError,
                "The original was restored, but the artifact could not be refreshed",
              );
            }
          }
          return receipt;
        } catch (error) {
          if (!(error && (error.retryable === true || error.ambiguous === true))) {
            restoreOperationId = "";
          }
          if (!destroyed) reportOriginalError(
            error,
            "The original display could not be restored",
          );
          return null;
        } finally {
          originalBusy = "";
          update();
        }
      }

      function editorFocused(event) {
        const active = documentRef.activeElement;
        if (active && typeof surface.contains === "function") {
          return surface.contains(active);
        }
        return Boolean(event && event.target &&
          (event.target === surface ||
           typeof surface.contains !== "function" ||
           surface.contains(event.target)));
      }

      function draw() {
        return drawPerspectiveOverlay(canvas, state, windowRef);
      }

      function validationText() {
        if (numericInvalid.size) {
          return "Enter a finite numeric coordinate from 0 through 1.";
        }
        if (!state.validation.valid) return state.validation.message;
        return "Quadrilateral geometry is valid.";
      }

      function queueAllowed() {
        if (!state || state.gesture || state.maskDraft || !state.validation.valid ||
            !sourcePinsValid(contract.pins) ||
            ["submitting", "queued", "complete"].includes(
              state.submission && state.submission.status,
            )) return false;
        if (canQueueTransform(state, contract.pins)) return true;
        return typeof options.canQueue === "function" &&
          options.canQueue({ state, resource, pins: contract.pins }) === true;
      }

      function toolInstruction() {
        if (state.tool === TOOLS.PERSPECTIVE) {
          return "Perspective mode. Click or drag to move the closest corner. Press Space to queue.";
        }
        if (state.tool === TOOLS.IMAGE_ADJUST) {
          return "Image Adjust mode. Adjustment controls can be supplied by a registered tool extension.";
        }
        if (state.tool === TOOLS.POLYGON) {
          return "Mask mode. Click to place vertices or drag to trace; click the first vertex or press Enter to close.";
        }
        return "Select mode. Choose Perspective to edit the four-corner boundary.";
      }

      function update() {
        if (destroyed) return;
        setData(surface, "activeTool", state.tool);
        setData(surface, "quadSource", state.quadSource.kind);
        setData(surface, "quadBasedOn", state.quadSource.basedOn);
        setData(surface, "quadValid", state.validation.valid);
        for (const [toolId, button] of toolButtons) {
          setBooleanAttribute(button, "aria-pressed", toolId === state.tool);
        }
        proposalBadge.textContent = proposalStatusText(state.quadSource);
        setData(proposalBadge, "quadSource", state.quadSource.kind);
        setData(proposalBadge, "quadBasedOn", state.quadSource.basedOn);
        proposalBadge.title = state.quadSource.message || "";
        undoButton.disabled = Boolean(state.gesture) || !state.undoStack.length;
        redoButton.disabled = Boolean(state.gesture) || !state.redoStack.length;
        queueButton.disabled = !invokeCommand || numericInvalid.size > 0 ||
          !queueAllowed();
        queueButton.textContent = state.submission.status === "retryable"
          ? "Retry queue" : "Queue transform";
        if (viewOriginalButton) {
          const identity = originalIdentity();
          const api = originalApi();
          const pinned = Boolean(
            identity.itemId && identity.artifactId && identity.revision,
          );
          viewOriginalButton.disabled = Boolean(originalBusy) || !pinned ||
            !api || typeof api.resolveOriginalBackup !== "function";
          viewOriginalButton.textContent = originalBusy === "view"
            ? "Opening…" : "View original";
          setBooleanAttribute(
            viewOriginalButton,
            "aria-busy",
            originalBusy === "view",
          );
          if (restoreOriginalButton) {
            restoreOriginalButton.disabled = Boolean(originalBusy) || !pinned ||
              !api || typeof api.restoreOriginalBackup !== "function";
            restoreOriginalButton.textContent = originalBusy === "restore"
              ? "Restoring…" : "Restore original";
            setBooleanAttribute(
              restoreOriginalButton,
              "aria-busy",
              originalBusy === "restore",
            );
          }
          originalStatus.textContent = originalStatusText;
          setData(originalStatus, "error", originalStatusError);
        }
        validationStatus.textContent = validationText();
        setData(validationStatus, "validationCode", state.validation.code || "valid");
        instruction.textContent = toolInstruction();
        queueStatus.textContent = queueStatusText(state.submission);

        const affected = new Set(state.validation.cornerIndices || []);
        for (let cornerIndex = 0; cornerIndex < cornerControls.length; cornerIndex += 1) {
          const controls = cornerControls[cornerIndex];
          const selected = state.selectedCorner === cornerIndex;
          setBooleanAttribute(controls.selectButton, "aria-pressed", selected);
          setData(controls.row, "selected", selected);
          for (let axisIndex = 0; axisIndex < 2; axisIndex += 1) {
            const input = controls.inputs[axisIndex];
            const localInvalid = numericInvalid.has(input);
            if (documentRef.activeElement !== input || !localInvalid) {
              const nextValue = Number(state.quad[cornerIndex][axisIndex]).toFixed(4);
              if (input.value !== nextValue) input.value = nextValue;
            }
            const invalid = localInvalid || affected.has(cornerIndex);
            setBooleanAttribute(input, "aria-invalid", invalid);
            if (invalid) setData(input, "invalid", true);
            else if (input.dataset) delete input.dataset.invalid;
            else removeAttribute(input, "data-invalid");
          }
        }
        draw();
        if (typeof options.onStateChange === "function") {
          options.onStateChange(state, resource);
        }
      }

      function dispatch(action) {
        if (destroyed) return state;
        if (action && action.type === "SET_TOOL" &&
            action.tool !== TOOLS.POLYGON && maskPointerId !== null) {
          const pointerId = maskPointerId;
          maskPointerId = null;
          maskDragged = false;
          releasePointer({ pointerId });
        }
        state = reduceImageEditorState(state, action);
        update();
        return state;
      }

      function coordinateForEvent(event) {
        const rect = canvas.getBoundingClientRect();
        return clientToNormalized(event, rect, { clamp: true });
      }

      function pointerMatches(event) {
        return state.gesture &&
          state.gesture.kind === "pointer" &&
          (state.gesture.pointerId == null ||
           event.pointerId == null ||
           state.gesture.pointerId === event.pointerId);
      }

      function releasePointer(event) {
        if (event.pointerId == null ||
            typeof canvas.releasePointerCapture !== "function") return;
        try {
          if (typeof canvas.hasPointerCapture !== "function" ||
              canvas.hasPointerCapture(event.pointerId)) {
            canvas.releasePointerCapture(event.pointerId);
          }
        } catch (error) {
          // Capture may already have been released by the browser.
        }
      }

      function maskPointFor(event) {
        return clientToNormalized(event, canvas.getBoundingClientRect(), {
          clamp: true,
        });
      }

      function handleMaskPointerDown(event) {
        let point;
        try { point = maskPointFor(event); } catch (error) { return; }
        if (typeof canvas.focus === "function") {
          try { canvas.focus({ preventScroll: true }); } catch (error) { canvas.focus(); }
        }
        if (state.maskDraft && !state.maskFreehand) {
          // Clicking back onto the first vertex is how a click-placed ring is
          // closed; anywhere else just adds another vertex.
          const first = state.maskDraft[0];
          const closing = state.maskDraft.length >= 3 && Math.hypot(
            point[0] - first[0], point[1] - first[1],
          ) <= POLYGON_CLOSE_DISTANCE;
          dispatch(closing ? { type: "MASK_COMMIT" } : { type: "MASK_EXTEND", point });
        } else {
          // A drag traces freehand; a click places vertices. Which one this is
          // only becomes known on the first move, so the draft starts in
          // click mode and is promoted by handleMaskPointerMove.
          dispatch({ type: "MASK_BEGIN", point, freehand: false });
        }
        if (event.pointerId != null &&
            typeof canvas.setPointerCapture === "function") {
          try { canvas.setPointerCapture(event.pointerId); } catch (error) {}
        }
        maskPointerId = event.pointerId == null ? null : event.pointerId;
        maskDragged = false;
        if (typeof event.preventDefault === "function") event.preventDefault();
      }

      function handleMaskPointerMove(event) {
        if (!state.maskDraft || maskPointerId === null ||
            event.pointerId !== maskPointerId) return;
        let point;
        try { point = maskPointFor(event); } catch (error) { return; }
        if (!maskDragged) {
          const first = state.maskDraft[state.maskDraft.length - 1];
          if (Math.hypot(point[0] - first[0], point[1] - first[1]) < FREEHAND_MIN_STEP) {
            return;
          }
          maskDragged = true;
          dispatch({ type: "MASK_BEGIN", point: state.maskDraft[0], freehand: true });
        }
        dispatch({ type: "MASK_EXTEND", point });
        if (typeof event.preventDefault === "function") event.preventDefault();
      }

      function handleMaskPointerUp(event) {
        if (maskPointerId === null || event.pointerId !== maskPointerId) return;
        if (maskDragged) dispatch({ type: "MASK_COMMIT" });
        maskPointerId = null;
        maskDragged = false;
        releasePointer(event);
        if (typeof event.preventDefault === "function") event.preventDefault();
      }

      function handlePointerDown(event) {
        if (state.tool === TOOLS.POLYGON) {
          if (maskPointerId !== null ||
              event.button != null && event.button !== 0) return;
          handleMaskPointerDown(event);
          return;
        }
        if (state.tool !== TOOLS.PERSPECTIVE || state.gesture ||
            (event.button != null && event.button !== 0)) return;
        let cornerIndex;
        let point;
        try {
          const rect = canvas.getBoundingClientRect();
          cornerIndex = nearestCornerIndex(state.quad, rect, event);
          point = clientToNormalized(event, rect, { clamp: true });
        } catch (error) {
          return;
        }
        if (typeof canvas.focus === "function") {
          try { canvas.focus({ preventScroll: true }); } catch (error) { canvas.focus(); }
        }
        dispatch({
          type: "BEGIN_GESTURE",
          kind: "pointer",
          pointerId: event.pointerId,
          cornerIndex,
          point,
        });
        if (event.pointerId != null &&
            typeof canvas.setPointerCapture === "function") {
          try { canvas.setPointerCapture(event.pointerId); } catch (error) {}
        }
        if (typeof event.preventDefault === "function") event.preventDefault();
      }

      function handlePointerMove(event) {
        if (state.tool === TOOLS.POLYGON) {
          handleMaskPointerMove(event);
          return;
        }
        if (!pointerMatches(event)) return;
        let point;
        try { point = coordinateForEvent(event); } catch (error) { return; }
        dispatch({ type: "MOVE_CORNER", point });
        if (typeof event.preventDefault === "function") event.preventDefault();
      }

      function handlePointerUp(event) {
        if (state.tool === TOOLS.POLYGON) {
          handleMaskPointerUp(event);
          return;
        }
        if (!pointerMatches(event)) return;
        let point = null;
        try { point = coordinateForEvent(event); } catch (error) {}
        if (point) dispatch({ type: "MOVE_CORNER", point });
        dispatch({ type: "COMMIT_GESTURE" });
        releasePointer(event);
        if (typeof event.preventDefault === "function") event.preventDefault();
      }

      function handlePointerCancel(event) {
        if (maskPointerId !== null && event.pointerId === maskPointerId) {
          if (state.maskDraft) dispatch({ type: "MASK_CANCEL" });
          maskPointerId = null;
          maskDragged = false;
          releasePointer(event);
          if (typeof event.preventDefault === "function") event.preventDefault();
          return;
        }
        if (!pointerMatches(event)) return;
        dispatch({ type: "CANCEL_GESTURE" });
        releasePointer(event);
      }

      function operationId() {
        operationSequence += 1;
        if (typeof options.createOperationId === "function") {
          return options.createOperationId({
            resource,
            sequence: operationSequence,
          });
        }
        return defaultOperationId(windowRef, operationSequence);
      }

      async function requestQueue(trigger = "toolbar") {
        if (destroyed || !invokeCommand || numericInvalid.size ||
            !queueAllowed()) return null;
        let command;
        if (state.submission.status === "retryable" && state.submission.command) {
          command = state.submission.command;
        } else {
          const adjustment = typeof options.getAdjustment === "function"
            ? options.getAdjustment({ state, resource }) : null;
          const rerunOcr = typeof options.getRerunOcr === "function"
            ? options.getRerunOcr({ state, resource }) : false;
          try {
            command = serializeCorrectionTransformCommand({
              pins: contract.pins,
              quad: state.quad,
              adjustment,
              operations: state.operations.length ? state.operations : null,
              rerunOcr,
              operationId: operationId(),
              maskPolygon: state.maskPolygon,
            });
          } catch (error) {
            if (typeof options.onCommandError === "function") {
              options.onCommandError(error, resource);
            }
            return null;
          }
        }
        dispatch({ type: "QUEUE_STARTED", command });
        try {
          const result = await invokeCommand(commandId, { command, trigger, resource });
          if (destroyed) return result;
          dispatch({
            type: "QUEUE_ACCEPTED",
            jobId: result && (result.job_id || result.jobId),
          });
          if (typeof options.onQueueResult === "function") {
            options.onQueueResult(result, command, resource);
          }
          return result;
        } catch (error) {
          if (destroyed) return null;
          const retryable = typeof options.isRetryableError === "function"
            ? options.isRetryableError(error) === true
            : Boolean(error && (error.retryable === true || error.ambiguous === true));
          dispatch({
            type: retryable ? "QUEUE_RETRYABLE" : "QUEUE_FAILED",
            error: error && error.message || String(error || "Queue failed"),
          });
          if (typeof options.onCommandError === "function") {
            options.onCommandError(error, resource);
          }
          return null;
        }
      }

      function handleKeyDown(event) {
        // An in-progress ring owns Escape and Enter before anything else, so a
        // half-drawn mask is never left behind by the editor-wide handlers.
        if (state.maskDraft && (event.key === "Escape" || event.key === "Enter")) {
          if (modalOpen()) return;
          dispatch({
            type: event.key === "Enter" ? "MASK_COMMIT" : "MASK_CANCEL",
          });
          maskPointerId = null;
          maskDragged = false;
          if (typeof event.preventDefault === "function") event.preventDefault();
          if (typeof event.stopPropagation === "function") event.stopPropagation();
          return;
        }
        if (event.key === "Escape") {
          if (modalOpen()) return;
          const resolution = resolveEscape(state, hostHasSelection());
          if (!resolution) return;
          numericInvalid.clear();
          dispatch(resolution.action);
          if (resolution.clearHostSelection &&
              typeof options.clearSelection === "function") {
            options.clearSelection(resource);
          }
          if (typeof event.preventDefault === "function") event.preventDefault();
          if (typeof event.stopPropagation === "function") event.stopPropagation();
          return;
        }

        if (canQueuePerspectiveShortcut({
          key: event.key,
          code: event.code,
          repeat: event.repeat,
          defaultPrevented: event.defaultPrevented,
          isComposing: event.isComposing,
          altKey: event.altKey,
          ctrlKey: event.ctrlKey,
          metaKey: event.metaKey,
          shiftKey: event.shiftKey,
          target: event.target,
          editorFocused: editorFocused(event),
          modalOpen: modalOpen(),
          formControl: isFormControlTarget(event.target),
        }, state, contract.pins) && numericInvalid.size === 0) {
          if (typeof event.preventDefault === "function") event.preventDefault();
          void requestQueue("shortcut");
          return;
        }

        if (!isFormControlTarget(event.target) &&
            (event.ctrlKey === true || event.metaKey === true) &&
            String(event.key || "").toLowerCase() === "z") {
          if (event.shiftKey === true) dispatch({ type: "REDO" });
          else dispatch({ type: "UNDO" });
          if (typeof event.preventDefault === "function") event.preventDefault();
        }
      }

      for (const [toolId, button] of toolButtons) {
        addListener(removers, button, "click", () => {
          numericInvalid.clear();
          dispatch({ type: "SET_TOOL", tool: toolId });
          if (typeof options.onToolChange === "function") {
            options.onToolChange(toolId, resource);
          }
        });
      }
      addListener(removers, undoButton, "click", () => dispatch({ type: "UNDO" }));
      addListener(removers, redoButton, "click", () => dispatch({ type: "REDO" }));
      addListener(removers, queueButton, "click", () => { void requestQueue("toolbar"); });
      addListener(removers, viewOriginalButton, "click", () => {
        void requestViewOriginal();
      });
      addListener(removers, restoreOriginalButton, "click", () => {
        void requestRestoreOriginal();
      });
      addListener(removers, canvas, "pointerdown", handlePointerDown);
      addListener(removers, canvas, "pointermove", handlePointerMove);
      addListener(removers, canvas, "pointerup", handlePointerUp);
      addListener(removers, canvas, "pointercancel", handlePointerCancel);
      addListener(removers, canvas, "lostpointercapture", handlePointerCancel);
      addListener(removers, surface, "keydown", handleKeyDown);

      for (let cornerIndex = 0; cornerIndex < cornerControls.length; cornerIndex += 1) {
        const controls = cornerControls[cornerIndex];
        addListener(removers, controls.selectButton, "click", () => {
          dispatch({ type: "SELECT_CORNER", cornerIndex });
          if (typeof controls.inputs[0].focus === "function") controls.inputs[0].focus();
        });
        for (let axisIndex = 0; axisIndex < controls.inputs.length; axisIndex += 1) {
          const input = controls.inputs[axisIndex];
          addListener(removers, input, "focus", () => {
            if (!state.gesture) {
              dispatch({
                type: "BEGIN_GESTURE",
                kind: "numeric",
                cornerIndex,
              });
            }
          });
          addListener(removers, input, "input", () => {
            const raw = String(input.value == null ? "" : input.value).trim();
            const value = raw === "" ? Number.NaN : Number(raw);
            if (!Number.isFinite(value)) {
              numericInvalid.add(input);
              update();
              return;
            }
            numericInvalid.delete(input);
            if (!state.gesture) {
              dispatch({
                type: "BEGIN_GESTURE",
                kind: "numeric",
                cornerIndex,
              });
            }
            if (!state.gesture || state.gesture.cornerIndex !== cornerIndex) return;
            const point = [...state.quad[cornerIndex]];
            point[axisIndex] = value;
            dispatch({ type: "MOVE_CORNER", cornerIndex, point });
          });
          addListener(removers, input, "blur", () => {
            if (!state.gesture || state.gesture.kind !== "numeric" ||
                state.gesture.cornerIndex !== cornerIndex) return;
            const invalidForCorner = controls.inputs.some(
              (candidate) => numericInvalid.has(candidate));
            if (invalidForCorner) {
              for (const candidate of controls.inputs) numericInvalid.delete(candidate);
              dispatch({ type: "CANCEL_GESTURE" });
            } else {
              dispatch({ type: "COMMIT_GESTURE" });
            }
          });
          addListener(removers, input, "keydown", (event) => {
            if (event.key !== "Enter") return;
            if (state.gesture && state.gesture.kind === "numeric" &&
                state.gesture.cornerIndex === cornerIndex &&
                !controls.inputs.some((candidate) => numericInvalid.has(candidate))) {
              dispatch({ type: "COMMIT_GESTURE" });
              if (typeof event.preventDefault === "function") event.preventDefault();
            }
          });
        }
      }

      addListener(removers, image, "load", () => {
        setData(imageStage, "loaded", true);
        publishPaneBox();
        draw();
      });
      addListener(removers, image, "error", () => {
        setData(imageStage, "loadError", true);
        validationStatus.textContent = "The image could not be loaded.";
      });

      // The stage must shrink-wrap the image exactly — the quad canvas and
      // the region overlay both stretch across the stage and convert
      // normalized coordinates against that box, so an image bigger than its
      // stage misdraws every region AND records wrong quad/mask coordinates.
      // CSS alone cannot cap the image by the pane (a percentage cap inside
      // a shrink-wrapped parent is circular, and size containment collapses
      // the pane), so the pane's box is published as custom properties the
      // image's max-width/height calc() against.
      const publishPaneBox = () => {
        if (!viewport.style || typeof viewport.style.setProperty !== "function") {
          return;
        }
        const width = Number(viewport.clientWidth) || 0;
        const height = Number(viewport.clientHeight) || 0;
        if (width > 0) viewport.style.setProperty("--pane-w", `${width}px`);
        if (height > 0) viewport.style.setProperty("--pane-h", `${height}px`);
      };
      publishPaneBox();
      const ResizeObserverRef = options.ResizeObserver ||
        windowRef && windowRef.ResizeObserver;
      if (typeof ResizeObserverRef === "function") {
        observer = new ResizeObserverRef(() => {
          publishPaneBox();
          draw();
        });
        if (typeof observer.observe === "function") {
          observer.observe(imageStage);
          observer.observe(viewport);
        }
      } else if (windowRef) {
        addListener(removers, windowRef, "resize", () => {
          publishPaneBox();
          draw();
        });
      }

      const controller = {
        canvas,
        dispatch,
        getState: () => state,
        getPins: () => contract.pins && { ...contract.pins },
        image,
        inspector,
        requestQueue,
        requestRestoreOriginal,
        requestViewOriginal,
        resource,
        surface,
        syncCanvas: draw,
        toolbar,
        viewport,
      };
      if (typeof options.onMount === "function") {
        mountCleanup = options.onMount(controller, resource);
      }
      update();

      const dispose = () => {
        if (destroyed) return;
        destroyed = true;
        if (observer && typeof observer.disconnect === "function") observer.disconnect();
        observer = null;
        closeOriginalPreview();
        for (const remove of removers.splice(0)) remove();
        if (typeof mountCleanup === "function") mountCleanup();
        mountCleanup = null;
        if (typeof options.onStateChange === "function") {
          options.onStateChange(null, resource);
        }
      };
      dispose.controller = controller;
      return dispose;
    };
  }

  return {
    TOOL_DEFINITIONS,
    correctionResourceContract,
    createPerspectiveImageRenderer,
    drawPerspectiveOverlay,
    originalBackupContract,
    safeRasterUrl,
    serializeProcessingPresetCommand,
  };
});

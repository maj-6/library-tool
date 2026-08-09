(function installCorrectionsImageEditorState(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else Object.assign(root.LibraryToolCorrections ||= {}, api);
})(typeof globalThis !== "undefined" ? globalThis : this, function imageEditorStateFactory() {
  "use strict";

  const TOOLS = Object.freeze({
    SELECT: "select",
    PERSPECTIVE: "perspective",
    IMAGE_ADJUST: "image-adjust",
    POLYGON: "polygon",
  });
  // Freehand tracing emits a point per pointer sample. Points closer together
  // than this (in normalized units) are dropped as the stroke is drawn, which
  // keeps the committed polygon inside the engine's 512-point ceiling without
  // needing a second simplification pass.
  const FREEHAND_MIN_STEP = 0.004;
  // Clicking within this distance of the first vertex closes the ring, which is
  // the usual way a reviewer finishes a click-to-place polygon.
  const POLYGON_CLOSE_DISTANCE = 0.02;
  const TOOL_IDS = new Set(Object.values(TOOLS));
  const POINT_ORDER = Object.freeze([
    "top_left", "top_right", "bottom_right", "bottom_left",
  ]);
  const POINT_LABELS = Object.freeze([
    "Top left", "Top right", "Bottom right", "Bottom left",
  ]);
  const FULL_FRAME_QUAD = Object.freeze([
    Object.freeze([0, 0]),
    Object.freeze([1, 0]),
    Object.freeze([1, 1]),
    Object.freeze([0, 1]),
  ]);
  const PROPOSAL_SCHEMA = "org.whl.page-boundary-proposal";
  const PROPOSAL_VERSION = 1;
  const COORDINATE_SPACE = "exif_oriented_normalized";
  const TRANSFORM_COMMAND_SCHEMA = "org.whl.correction-transform-command";
  const TRANSFORM_COMMAND_VERSION = 1;
  const TRANSFORM_COMMAND_ID = "corrections.transform.queue";
  const GEOMETRY_EPSILON = 1e-12;
  const MIN_NORMALIZED_QUAD_AREA = 0.0001;
  const MIN_NORMALIZED_EDGE_LENGTH = 0.001;
  const MIN_NORMALIZED_POLYGON_AREA = 0.0001;
  const MIN_MASK_POLYGON_POINTS = 3;
  const MAX_MASK_POLYGON_POINTS = 512;
  const HISTORY_LIMIT = 100;
  const IDENTIFIER_RE = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
  const SHA256_RE = /^[0-9a-fA-F]{64}$/;
  const MANUAL_ADJUSTMENT_FIELDS = Object.freeze([
    "schema",
    "version",
    "algorithm",
    "contrast_percent",
    "brightness_percent",
    "threshold",
    "threshold_rule",
    "comparison",
  ]);

  function isPlainObject(value) {
    if (!value || typeof value !== "object" || Array.isArray(value)) return false;
    const prototype = Object.getPrototypeOf(value);
    return prototype === Object.prototype || prototype === null;
  }

  function cloneQuad(quad) {
    return quad.map((point) => [point[0], point[1]]);
  }

  function cloneSource(source) {
    return source ? { ...source } : source;
  }

  function sameQuad(first, second) {
    return Array.isArray(first) && Array.isArray(second) &&
      first.length === second.length &&
      first.every((point, index) =>
        point[0] === second[index][0] && point[1] === second[index][1]);
  }

  function pointValue(value) {
    if (Array.isArray(value)) return [value[0], value[1]];
    if (value && typeof value === "object") {
      const x = value.clientX != null ? value.clientX : value.x;
      const y = value.clientY != null ? value.clientY : value.y;
      return [x, y];
    }
    return [undefined, undefined];
  }

  function validationResult(code, message, cornerIndices = []) {
    const error = Object.freeze({
      code,
      message,
      cornerIndices: Object.freeze(cornerIndices.slice()),
    });
    return Object.freeze({
      valid: false,
      code,
      message,
      cornerIndices: error.cornerIndices,
      errors: Object.freeze([error]),
      quad: null,
    });
  }

  function validResult(quad) {
    return Object.freeze({
      valid: true,
      code: null,
      message: "",
      cornerIndices: Object.freeze([]),
      errors: Object.freeze([]),
      quad: cloneQuad(quad),
    });
  }

  function cross(a, b, c) {
    return (b[0] - a[0]) * (c[1] - a[1]) -
      (b[1] - a[1]) * (c[0] - a[0]);
  }

  function onSegment(a, b, point) {
    return Math.min(a[0], b[0]) - GEOMETRY_EPSILON <= point[0] &&
      point[0] <= Math.max(a[0], b[0]) + GEOMETRY_EPSILON &&
      Math.min(a[1], b[1]) - GEOMETRY_EPSILON <= point[1] &&
      point[1] <= Math.max(a[1], b[1]) + GEOMETRY_EPSILON;
  }

  function segmentsIntersect(a, b, c, d) {
    const turns = [
      cross(a, b, c),
      cross(a, b, d),
      cross(c, d, a),
      cross(c, d, b),
    ];
    if (turns[0] * turns[1] < -GEOMETRY_EPSILON &&
        turns[2] * turns[3] < -GEOMETRY_EPSILON) return true;
    return [
      [turns[0], a, b, c],
      [turns[1], a, b, d],
      [turns[2], c, d, a],
      [turns[3], c, d, b],
    ].some(([turn, start, end, point]) =>
      Math.abs(turn) <= GEOMETRY_EPSILON && onSegment(start, end, point));
  }

  function signedArea(points) {
    let area = 0;
    for (let index = 0; index < 4; index += 1) {
      const next = (index + 1) % 4;
      area += points[index][0] * points[next][1] -
        points[next][0] * points[index][1];
    }
    return 0.5 * area;
  }

  function polygonSignedArea(points) {
    let area = 0;
    for (let index = 0; index < points.length; index += 1) {
      const next = (index + 1) % points.length;
      area += points[index][0] * points[next][1] -
        points[next][0] * points[index][1];
    }
    return 0.5 * area;
  }

  // Mirrors validate_normalized_polygon in librarytool/processing/raster.py.
  // A mask is not a page boundary: any vertex count from three up is allowed,
  // concave shapes are expected, and winding is free — only simplicity, bounds
  // and a minimum area are enforced.
  function validateMaskPolygon(polygon, options = {}) {
    const minArea = options.minArea == null
      ? MIN_NORMALIZED_POLYGON_AREA : options.minArea;
    if (typeof minArea !== "number" || !Number.isFinite(minArea) || minArea <= 0) {
      throw new TypeError("minArea must be a positive finite number");
    }
    if (!Array.isArray(polygon) || polygon.length < MIN_MASK_POLYGON_POINTS) {
      return validationResult(
        "polygon-point-count",
        `Mask polygon must contain at least ${MIN_MASK_POLYGON_POINTS} points.`,
      );
    }
    if (polygon.length > MAX_MASK_POLYGON_POINTS) {
      return validationResult(
        "polygon-point-limit",
        `Mask polygon must contain at most ${MAX_MASK_POLYGON_POINTS} points.`,
      );
    }

    const points = [];
    for (let index = 0; index < polygon.length; index += 1) {
      const point = polygon[index];
      if (!Array.isArray(point) || point.length !== 2) {
        return validationResult(
          "polygon-point-arity",
          `Mask polygon point ${index + 1} must contain exactly X and Y.`,
          [index],
        );
      }
      const [x, y] = point;
      if (typeof x !== "number" || typeof y !== "number" ||
          !Number.isFinite(x) || !Number.isFinite(y)) {
        return validationResult(
          "polygon-coordinate-finite",
          `Mask polygon point ${index + 1} coordinates must be finite numbers.`,
          [index],
        );
      }
      if (x < 0 || x > 1 || y < 0 || y > 1) {
        return validationResult(
          "polygon-coordinate-bounds",
          `Mask polygon point ${index + 1} must be within normalized bounds from 0 to 1.`,
          [index],
        );
      }
      points.push([x, y]);
    }

    for (let index = 0; index < points.length; index += 1) {
      for (let other = index + 1; other < points.length; other += 1) {
        if (Math.hypot(
          points[index][0] - points[other][0],
          points[index][1] - points[other][1],
        ) <= GEOMETRY_EPSILON) {
          return validationResult(
            "polygon-duplicate-vertices",
            "Mask polygon vertices must be distinct.",
            [index, other],
          );
        }
      }
    }

    const count = points.length;
    for (let index = 0; index < count; index += 1) {
      const next = (index + 1) % count;
      for (let other = index + 1; other < count; other += 1) {
        const otherNext = (other + 1) % count;
        // Edges sharing a vertex always meet there; only disjoint pairs matter.
        if (other === next || otherNext === index) continue;
        if (segmentsIntersect(
          points[index], points[next], points[other], points[otherNext],
        )) {
          return validationResult(
            "polygon-self-intersection",
            "Mask polygon must not self-intersect.",
            [index, other],
          );
        }
      }
    }

    if (Math.abs(polygonSignedArea(points)) < minArea) {
      return validationResult(
        "polygon-area",
        "Mask polygon encloses too little area.",
      );
    }
    return Object.freeze({
      valid: true,
      code: "",
      message: "",
      cornerIndices: Object.freeze([]),
      errors: Object.freeze([]),
      polygon: points.map((point) => [point[0], point[1]]),
    });
  }

  function validatePerspectiveQuad(quad, options = {}) {
    const minArea = options.minArea == null
      ? MIN_NORMALIZED_QUAD_AREA : options.minArea;
    if (typeof minArea !== "number" || !Number.isFinite(minArea) || minArea <= 0) {
      throw new TypeError("minArea must be a positive finite number");
    }
    if (!Array.isArray(quad) || quad.length !== 4) {
      return validationResult(
        "point-count",
        "Quad must contain exactly four points in TL/TR/BR/BL order.",
      );
    }

    const points = [];
    for (let index = 0; index < 4; index += 1) {
      const point = quad[index];
      if (!Array.isArray(point) || point.length !== 2) {
        return validationResult(
          "point-arity",
          `${POINT_LABELS[index]} must contain exactly X and Y.`,
          [index],
        );
      }
      const [x, y] = point;
      if (typeof x !== "number" || typeof y !== "number") {
        return validationResult(
          "coordinate-type",
          `${POINT_LABELS[index]} coordinates must be numbers.`,
          [index],
        );
      }
      if (!Number.isFinite(x) || !Number.isFinite(y)) {
        return validationResult(
          "coordinate-finite",
          `${POINT_LABELS[index]} coordinates must be finite.`,
          [index],
        );
      }
      if (x < 0 || x > 1 || y < 0 || y > 1) {
        return validationResult(
          "coordinate-bounds",
          `${POINT_LABELS[index]} must be within normalized bounds from 0 to 1.`,
          [index],
        );
      }
      points.push([x, y]);
    }

    for (let index = 0; index < 4; index += 1) {
      for (let other = index + 1; other < 4; other += 1) {
        if (Math.hypot(
          points[index][0] - points[other][0],
          points[index][1] - points[other][1],
        ) <= GEOMETRY_EPSILON) {
          return validationResult(
            "duplicate-vertices",
            "Quad vertices must be distinct.",
            [index, other],
          );
        }
      }
    }

    if (segmentsIntersect(points[0], points[1], points[2], points[3]) ||
        segmentsIntersect(points[1], points[2], points[3], points[0])) {
      return validationResult(
        "self-intersection",
        "Quad edges must not cross.",
        [0, 1, 2, 3],
      );
    }

    const turns = points.map((_point, index) =>
      cross(points[index], points[(index + 1) % 4], points[(index + 2) % 4]));
    if (turns.some((turn) => Math.abs(turn) <= GEOMETRY_EPSILON) ||
        !(turns.every((turn) => turn > 0) || turns.every((turn) => turn < 0))) {
      return validationResult(
        "non-convex",
        "Quad must be strictly convex.",
        [0, 1, 2, 3],
      );
    }
    if (turns.some((turn) => turn < 0)) {
      return validationResult(
        "point-order",
        "Quad vertices must use TL/TR/BR/BL order.",
        [0, 1, 2, 3],
      );
    }

    const topMeanY = (points[0][1] + points[1][1]) / 2;
    const bottomMeanY = (points[3][1] + points[2][1]) / 2;
    const leftMeanX = (points[0][0] + points[3][0]) / 2;
    const rightMeanX = (points[1][0] + points[2][0]) / 2;
    if (topMeanY >= bottomMeanY || leftMeanX >= rightMeanX) {
      return validationResult(
        "point-labels",
        "Quad vertices must use top-left, top-right, bottom-right, bottom-left labels.",
        [0, 1, 2, 3],
      );
    }

    if (signedArea(points) < minArea) {
      return validationResult(
        "area-too-small",
        `Quad area is too small; normalized area must be at least ${minArea}.`,
        [0, 1, 2, 3],
      );
    }
    for (let index = 0; index < 4; index += 1) {
      const next = (index + 1) % 4;
      if (Math.hypot(
        points[index][0] - points[next][0],
        points[index][1] - points[next][1],
      ) < MIN_NORMALIZED_EDGE_LENGTH) {
        return validationResult(
          "edge-too-short",
          "Quad edge is too short.",
          [index, next],
        );
      }
    }
    return validResult(points);
  }

  function fallbackResolution(code, message) {
    return {
      quad: cloneQuad(FULL_FRAME_QUAD),
      quadSource: {
        kind: "fallback",
        basedOn: "fallback",
        reason: code,
        message,
        detector: null,
        detectorVersion: null,
        confidence: null,
      },
    };
  }

  function resolveInitialQuad({ proposal = null, sourceRevision = null } = {}) {
    if (!proposal) {
      return fallbackResolution("missing-proposal", "No page-boundary proposal is available.");
    }
    if (!isPlainObject(proposal)) {
      return fallbackResolution("invalid-proposal", "The page-boundary proposal is not an object.");
    }
    if (proposal.schema !== PROPOSAL_SCHEMA ||
        proposal.version !== PROPOSAL_VERSION ||
        !Number.isInteger(proposal.version)) {
      return fallbackResolution(
        "unsupported-proposal-schema",
        "The page-boundary proposal schema is unsupported.",
      );
    }
    if (proposal.coordinate_space !== COORDINATE_SPACE) {
      return fallbackResolution(
        "unsupported-coordinate-space",
        "The proposal does not use EXIF-oriented normalized coordinates.",
      );
    }
    if (!Array.isArray(proposal.point_order) ||
        proposal.point_order.length !== POINT_ORDER.length ||
        proposal.point_order.some((value, index) => value !== POINT_ORDER[index])) {
      return fallbackResolution(
        "unsupported-point-order",
        "The proposal does not use TL/TR/BR/BL point order.",
      );
    }
    if (typeof sourceRevision !== "string" || !sourceRevision) {
      return fallbackResolution(
        "source-revision-unavailable",
        "The selected image has no source revision for proposal pinning.",
      );
    }
    if (proposal.source_revision !== sourceRevision) {
      return fallbackResolution(
        "stale-proposal",
        "The page-boundary proposal belongs to another source revision.",
      );
    }
    if (typeof proposal.detector !== "string" || !proposal.detector.trim() ||
        typeof proposal.detector_version !== "string" ||
        !proposal.detector_version.trim() ||
        typeof proposal.confidence !== "number" ||
        !Number.isFinite(proposal.confidence) ||
        proposal.confidence < 0 || proposal.confidence > 1) {
      return fallbackResolution(
        "invalid-proposal-metadata",
        "The page-boundary proposal metadata is invalid.",
      );
    }
    const validation = validatePerspectiveQuad(proposal.quad);
    if (!validation.valid) {
      return fallbackResolution(
        `invalid-proposal-${validation.code}`,
        `The page-boundary proposal is invalid: ${validation.message}`,
      );
    }
    return {
      quad: cloneQuad(validation.quad),
      quadSource: {
        kind: "proposal",
        basedOn: "proposal",
        reason: null,
        message: "Auto-detected page boundary.",
        detector: proposal.detector,
        detectorVersion: proposal.detector_version,
        confidence: proposal.confidence,
      },
    };
  }

  function idleSubmission() {
    return {
      status: "idle",
      command: null,
      jobId: null,
      error: null,
    };
  }

  function createImageEditorState(options = {}) {
    const initial = resolveInitialQuad({
      proposal: options.proposal,
      sourceRevision: options.sourceRevision,
    });
    const tool = TOOL_IDS.has(options.tool) ? options.tool : TOOLS.SELECT;
    return {
      tool,
      quad: initial.quad,
      quadSource: initial.quadSource,
      selectedCorner: null,
      gesture: null,
      undoStack: [],
      redoStack: [],
      validation: validatePerspectiveQuad(initial.quad),
      submission: idleSubmission(),
      selectionPresent: options.hasSelection === true,
      // null means "no mask", which is different from an empty in-progress
      // ring. Only maskPolygon reaches the wire; maskDraft is never submitted.
      maskPolygon: null,
      maskDraft: null,
      maskFreehand: false,
      // Processing operations are editor-domain state rather than tool-local
      // state: a preset may carry operations without a manual adjustment, and
      // the exact recipe must survive until the transform is serialized.
      operations: options.operations == null ||
          Array.isArray(options.operations) && options.operations.length === 0
        ? [] : normalizeProcessingOperations(options.operations),
    };
  }


  function cloneMask(polygon) {
    return polygon ? polygon.map((point) => [point[0], point[1]]) : null;
  }


  function historyEntry(state) {
    return {
      quad: cloneQuad(state.quad),
      quadSource: cloneSource(state.quadSource),
      maskPolygon: cloneMask(state.maskPolygon),
    };
  }


  function restoreHistory(state, target) {
    const quad = cloneQuad(target.quad);
    return {
      quad,
      quadSource: cloneSource(target.quadSource),
      // Entries written before masking existed carry no maskPolygon; treating
      // that as "no mask" keeps an older undo stack usable.
      maskPolygon: cloneMask(target.maskPolygon || null),
      validation: validatePerspectiveQuad(quad),
    };
  }


  function sameMaskPolygon(actual, expected) {
    return Array.isArray(actual) && Array.isArray(expected) &&
      actual.length === expected.length && actual.every((point, index) =>
        Array.isArray(point) && Array.isArray(expected[index]) &&
        point.length === 2 && expected[index].length === 2 &&
        point[0] === expected[index][0] && point[1] === expected[index][1]);
  }


  function historyAfterMaskExtraction(entries, currentQuad) {
    const scrubbed = [];
    for (const entry of entries) {
      const withoutMask = { ...entry, maskPolygon: null };
      const prior = scrubbed[scrubbed.length - 1];
      if (prior && sameQuad(prior.quad, withoutMask.quad)) continue;
      scrubbed.push(withoutMask);
    }
    // Committing a mask records the otherwise-identical pre-mask state. Once
    // the mask is durable that entry is no longer an undo step.
    while (scrubbed.length &&
        sameQuad(scrubbed[scrubbed.length - 1].quad, currentQuad)) {
      scrubbed.pop();
    }
    return scrubbed;
  }


  function normalizedPoint(point) {
    if (!Array.isArray(point) || point.length !== 2 ||
        point.some((value) => typeof value !== "number" || !Number.isFinite(value))) {
      throw new TypeError("point must contain two finite numbers");
    }
    return [
      Math.max(0, Math.min(1, point[0])),
      Math.max(0, Math.min(1, point[1])),
    ];
  }

  function editedSource(source) {
    return {
      kind: "user-edited",
      basedOn: source && source.basedOn === "proposal" ? "proposal" : "fallback",
      reason: source && source.reason || null,
      message: source && source.basedOn === "proposal"
        ? "Edited auto-detected page boundary."
        : "Edited full-image fallback boundary.",
      detector: source && source.detector || null,
      detectorVersion: source && source.detectorVersion || null,
      confidence: source && source.confidence != null ? source.confidence : null,
    };
  }

  function replaceCorner(state, cornerIndex, point) {
    if (!Number.isInteger(cornerIndex) || cornerIndex < 0 || cornerIndex > 3) {
      throw new TypeError("cornerIndex must identify one of four corners");
    }
    if (!Array.isArray(point) || point.length !== 2 ||
        point.some((value) => typeof value !== "number" || !Number.isFinite(value))) {
      throw new TypeError("point must contain two finite numbers");
    }
    const quad = cloneQuad(state.quad);
    quad[cornerIndex] = [point[0], point[1]];
    return {
      ...state,
      quad,
      quadSource: editedSource(
        state.gesture ? state.gesture.beforeQuadSource : state.quadSource,
      ),
      selectedCorner: cornerIndex,
      validation: validatePerspectiveQuad(quad),
    };
  }

  function trimHistory(entries) {
    return entries.length <= HISTORY_LIMIT
      ? entries : entries.slice(entries.length - HISTORY_LIMIT);
  }

  function resetSubmissionAfterEdit(state) {
    return state.submission.status === "idle" ? state.submission : idleSubmission();
  }

  function cancelGesture(state) {
    if (!state.gesture) return state;
    const quad = cloneQuad(state.gesture.beforeQuad);
    return {
      ...state,
      quad,
      quadSource: cloneSource(state.gesture.beforeQuadSource),
      selectedCorner: state.gesture.beforeSelectedCorner,
      gesture: null,
      validation: validatePerspectiveQuad(quad),
    };
  }

  function reduceImageEditorState(state, action) {
    if (!state || !action || typeof action.type !== "string") {
      throw new TypeError("state and action are required");
    }
    switch (action.type) {
      case "SET_TOOL": {
        if (!TOOL_IDS.has(action.tool)) throw new TypeError("unknown image editor tool");
        const settled = state.gesture ? cancelGesture(state) : state;
        return {
          ...settled,
          tool: action.tool,
          maskDraft: action.tool === TOOLS.POLYGON ? settled.maskDraft : null,
          maskFreehand: action.tool === TOOLS.POLYGON
            ? settled.maskFreehand : false,
        };
      }
      case "SELECT_CORNER": {
        if (action.cornerIndex !== null &&
            (!Number.isInteger(action.cornerIndex) ||
             action.cornerIndex < 0 || action.cornerIndex > 3)) {
          throw new TypeError("cornerIndex must identify one of four corners or be null");
        }
        return { ...state, selectedCorner: action.cornerIndex };
      }
      case "SET_SELECTION_PRESENT":
        return { ...state, selectionPresent: action.present === true };
      case "CLEAR_SELECTION":
        return { ...state, selectedCorner: null, selectionPresent: false };
      case "BEGIN_GESTURE": {
        if (state.gesture) return state;
        if (!Number.isInteger(action.cornerIndex) ||
            action.cornerIndex < 0 || action.cornerIndex > 3) {
          throw new TypeError("cornerIndex must identify one of four corners");
        }
        let next = {
          ...state,
          selectedCorner: action.cornerIndex,
          gesture: {
            kind: action.kind === "numeric" ? "numeric" : "pointer",
            pointerId: action.pointerId == null ? null : action.pointerId,
            cornerIndex: action.cornerIndex,
            beforeQuad: cloneQuad(state.quad),
            beforeQuadSource: cloneSource(state.quadSource),
            beforeSelectedCorner: state.selectedCorner,
          },
        };
        if (action.point != null) {
          next = replaceCorner(next, action.cornerIndex, action.point);
        }
        return next;
      }
      case "MOVE_CORNER": {
        if (!state.gesture) return state;
        const cornerIndex = action.cornerIndex == null
          ? state.gesture.cornerIndex : action.cornerIndex;
        if (cornerIndex !== state.gesture.cornerIndex) {
          throw new TypeError("a gesture cannot change vertex identity");
        }
        return replaceCorner(state, cornerIndex, action.point);
      }
      case "COMMIT_GESTURE": {
        if (!state.gesture) return state;
        if (sameQuad(state.quad, state.gesture.beforeQuad)) {
          return { ...state, gesture: null };
        }
        const undoStack = trimHistory(state.undoStack.concat([{
          quad: cloneQuad(state.gesture.beforeQuad),
          quadSource: cloneSource(state.gesture.beforeQuadSource),
          maskPolygon: cloneMask(state.maskPolygon),
        }]));
        return {
          ...state,
          gesture: null,
          undoStack,
          redoStack: [],
          submission: resetSubmissionAfterEdit(state),
        };
      }
      case "CANCEL_GESTURE":
        return cancelGesture(state);
      case "UNDO": {
        if (state.gesture || state.maskDraft || !state.undoStack.length) return state;
        const target = state.undoStack[state.undoStack.length - 1];
        return {
          ...state,
          ...restoreHistory(state, target),
          undoStack: state.undoStack.slice(0, -1),
          redoStack: trimHistory(state.redoStack.concat([historyEntry(state)])),
          submission: resetSubmissionAfterEdit(state),
        };
      }
      case "REDO": {
        if (state.gesture || state.maskDraft || !state.redoStack.length) return state;
        const target = state.redoStack[state.redoStack.length - 1];
        return {
          ...state,
          ...restoreHistory(state, target),
          redoStack: state.redoStack.slice(0, -1),
          undoStack: trimHistory(state.undoStack.concat([historyEntry(state)])),
          submission: resetSubmissionAfterEdit(state),
        };
      }
      case "MASK_BEGIN": {
        if (state.tool !== TOOLS.POLYGON) return state;
        return {
          ...state,
          maskDraft: [normalizedPoint(action.point)],
          maskFreehand: action.freehand === true,
        };
      }
      case "MASK_EXTEND": {
        if (!state.maskDraft) return state;
        const point = normalizedPoint(action.point);
        const last = state.maskDraft[state.maskDraft.length - 1];
        if (state.maskFreehand &&
            Math.hypot(point[0] - last[0], point[1] - last[1]) < FREEHAND_MIN_STEP) {
          return state;
        }
        if (state.maskDraft.length >= MAX_MASK_POLYGON_POINTS) return state;
        return { ...state, maskDraft: state.maskDraft.concat([point]) };
      }
      case "MASK_COMMIT": {
        if (!state.maskDraft) return state;
        const validation = validateMaskPolygon(state.maskDraft);
        if (!validation.valid) {
          // A traced ring that does not close cleanly is discarded rather than
          // half-kept: a mask the engine would reject must never look applied.
          return { ...state, maskDraft: null, maskFreehand: false };
        }
        return {
          ...state,
          maskPolygon: validation.polygon,
          maskDraft: null,
          maskFreehand: false,
          undoStack: trimHistory(state.undoStack.concat([historyEntry(state)])),
          redoStack: [],
          submission: resetSubmissionAfterEdit(state),
        };
      }
      case "MASK_CANCEL":
        if (!state.maskDraft) return state;
        return { ...state, maskDraft: null, maskFreehand: false };
      case "MASK_CLEAR": {
        if (state.maskPolygon === null && !state.maskDraft) return state;
        const cleared = {
          ...state,
          maskDraft: null,
          maskFreehand: false,
          maskPolygon: null,
        };
        if (state.maskPolygon === null) return cleared;
        return {
          ...cleared,
          undoStack: trimHistory(state.undoStack.concat([historyEntry(state)])),
          redoStack: [],
          submission: resetSubmissionAfterEdit(state),
        };
      }
      case "MASK_EXTRACTION_COMMITTED": {
        if (state.maskPolygon === null && !state.maskDraft) return state;
        if (action.maskPolygon != null &&
            (state.maskDraft ||
             !sameMaskPolygon(state.maskPolygon, action.maskPolygon))) {
          // The user drew another mask while this extraction was in flight.
          // Its transient state belongs to a later intent and must survive.
          return state;
        }
        // The transform result has made this geometry durable as an
        // overlay-only source marker. Scrub it from every history snapshot,
        // collapse the now-redundant mask transitions, and retain each
        // distinct perspective step.
        return {
          ...state,
          maskPolygon: null,
          maskDraft: null,
          maskFreehand: false,
          undoStack: historyAfterMaskExtraction(state.undoStack, state.quad),
          redoStack: [],
        };
      }
      case "SET_PROCESSING_OPERATIONS": {
        const operations = action.operations == null ||
            Array.isArray(action.operations) && action.operations.length === 0
          ? [] : normalizeProcessingOperations(action.operations);
        return {
          ...state,
          operations,
          submission: resetSubmissionAfterEdit(state),
        };
      }
      case "QUEUE_STARTED":
        if (!isPlainObject(action.command)) {
          throw new TypeError("QUEUE_STARTED requires a serialized command");
        }
        return {
          ...state,
          submission: {
            status: "submitting",
            command: action.command,
            jobId: null,
            error: null,
          },
        };
      case "QUEUE_ACCEPTED":
        if (state.submission.status !== "submitting") return state;
        return {
          ...state,
          submission: {
            ...state.submission,
            status: "queued",
            jobId: action.jobId == null ? null : String(action.jobId),
          },
        };
      case "QUEUE_RETRYABLE":
        if (state.submission.status !== "submitting") return state;
        return {
          ...state,
          submission: {
            ...state.submission,
            status: "retryable",
            error: String(action.error || "The queue result is uncertain; retry safely."),
          },
        };
      case "QUEUE_FAILED":
        if (state.submission.status !== "submitting") return state;
        return {
          ...state,
          submission: {
            status: "failed",
            command: null,
            jobId: null,
            error: String(action.error || "The correction job could not be queued."),
          },
        };
      case "QUEUE_COMPLETED":
        if (state.submission.status !== "queued") return state;
        return {
          ...state,
          submission: {
            ...state.submission,
            status: "complete",
            error: null,
          },
        };
      case "QUEUE_RESET":
        return { ...state, submission: idleSubmission() };
      default:
        throw new TypeError(`unknown image editor action: ${action.type}`);
    }
  }

  function normalizeImageRect(rect) {
    if (!rect || typeof rect !== "object") throw new TypeError("image rectangle is required");
    const left = Number(rect.left);
    const top = Number(rect.top);
    const width = Number(rect.width);
    const height = Number(rect.height);
    if (![left, top, width, height].every(Number.isFinite) || width <= 0 || height <= 0) {
      throw new TypeError("image rectangle must have finite positive dimensions");
    }
    return { left, top, width, height };
  }

  function containedImageRect(containerRect, naturalWidth, naturalHeight, options = {}) {
    const container = normalizeImageRect(containerRect);
    if (typeof naturalWidth !== "number" || !Number.isFinite(naturalWidth) ||
        naturalWidth <= 0 ||
        typeof naturalHeight !== "number" || !Number.isFinite(naturalHeight) ||
        naturalHeight <= 0) {
      throw new TypeError("oriented image dimensions must be finite and positive");
    }
    const zoom = options.zoom == null ? 1 : Number(options.zoom);
    const panX = options.panX == null ? 0 : Number(options.panX);
    const panY = options.panY == null ? 0 : Number(options.panY);
    if (![zoom, panX, panY].every(Number.isFinite) || zoom <= 0) {
      throw new TypeError("zoom and pan values are invalid");
    }
    const scale = Math.min(
      container.width / naturalWidth,
      container.height / naturalHeight,
    ) * zoom;
    const width = naturalWidth * scale;
    const height = naturalHeight * scale;
    return {
      left: container.left + (container.width - width) / 2 + panX,
      top: container.top + (container.height - height) / 2 + panY,
      width,
      height,
    };
  }

  function normalizedToClient(point, imageRect) {
    const [x, y] = pointValue(point);
    if (![x, y].every((value) => typeof value === "number" && Number.isFinite(value))) {
      throw new TypeError("normalized point must contain two finite numbers");
    }
    const rect = normalizeImageRect(imageRect);
    return [rect.left + x * rect.width, rect.top + y * rect.height];
  }

  function clientToNormalized(point, imageRect, options = {}) {
    const [x, y] = pointValue(point);
    if (![x, y].every((value) => typeof value === "number" && Number.isFinite(value))) {
      throw new TypeError("client point must contain two finite numbers");
    }
    const rect = normalizeImageRect(imageRect);
    let normalizedX = (x - rect.left) / rect.width;
    let normalizedY = (y - rect.top) / rect.height;
    if (options.clamp === true) {
      normalizedX = Math.max(0, Math.min(1, normalizedX));
      normalizedY = Math.max(0, Math.min(1, normalizedY));
    }
    return [normalizedX, normalizedY];
  }

  function nearestCornerIndex(quad, imageRect, clientPoint) {
    if (!Array.isArray(quad) || quad.length !== 4) {
      throw new TypeError("quad must contain four points");
    }
    const [clientX, clientY] = pointValue(clientPoint);
    if (![clientX, clientY].every(
      (value) => typeof value === "number" && Number.isFinite(value))) {
      throw new TypeError("client point must contain two finite numbers");
    }
    let bestIndex = 0;
    let bestDistance = Number.POSITIVE_INFINITY;
    for (let index = 0; index < quad.length; index += 1) {
      const [screenX, screenY] = normalizedToClient(quad[index], imageRect);
      const distance = (screenX - clientX) ** 2 + (screenY - clientY) ** 2;
      // Iteration follows TL/TR/BR/BL. Strict comparison makes an exact tie
      // deterministic without silently changing vertex identity.
      if (distance < bestDistance) {
        bestDistance = distance;
        bestIndex = index;
      }
    }
    return bestIndex;
  }

  function portableIdentifier(value, fieldName) {
    if (typeof value !== "string" || !IDENTIFIER_RE.test(value)) {
      throw new TypeError(`${fieldName} must be a portable opaque identifier`);
    }
    return value;
  }

  function revisionToken(value, fieldName) {
    if (typeof value !== "string" || !value || value.length > 512 ||
        value !== value.trim() || /\s|"|\\/.test(value)) {
      throw new TypeError(`${fieldName} must be a revision token`);
    }
    return value;
  }

  function normalizeSourcePins(pins) {
    if (!isPlainObject(pins)) throw new TypeError("source pins are required");
    return {
      item_id: portableIdentifier(pins.item_id, "item_id"),
      artifact_id: portableIdentifier(pins.artifact_id, "artifact_id"),
      artifact_revision: revisionToken(pins.artifact_revision, "artifact_revision"),
      source_revision: revisionToken(pins.source_revision, "source_revision"),
      source_sha256: (() => {
        if (typeof pins.source_sha256 !== "string" ||
            !SHA256_RE.test(pins.source_sha256)) {
          throw new TypeError("source_sha256 must be a SHA-256 digest");
        }
        return pins.source_sha256.toLowerCase();
      })(),
    };
  }


  function normalizeExtraction(value) {
    if (!isPlainObject(value)) {
      throw new TypeError("extraction must be an object");
    }
    const fields = Object.keys(value).sort();
    const annotationFields = ["annotation_id", "annotation_revision", "label"];
    const maskFields = ["label", "source_kind"];
    const annotation = fields.length === annotationFields.length &&
      fields.every((field, index) => field === annotationFields[index]);
    const committedMask = fields.length === maskFields.length &&
      fields.every((field, index) => field === maskFields[index]) &&
      value.source_kind === "committed-mask";
    if (!annotation && !committedMask) {
      throw new TypeError("extraction fields must match one exact source schema");
    }
    if (typeof value.label !== "string" || !value.label.trim() ||
        value.label.length > 512 ||
        /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/.test(value.label)) {
      throw new TypeError("extraction label must be non-empty bounded text");
    }
    if (committedMask) {
      return { source_kind: "committed-mask", label: value.label };
    }
    return {
      annotation_id: portableIdentifier(
        value.annotation_id, "extraction.annotation_id"),
      annotation_revision: revisionToken(
        value.annotation_revision, "extraction.annotation_revision"),
      label: value.label,
    };
  }

  function sourcePinsValid(pins) {
    try {
      normalizeSourcePins(pins);
      return true;
    } catch (error) {
      return false;
    }
  }

  function expectedThreshold(brightness) {
    return Math.max(0, Math.min(255, Math.floor(
      127.5 - brightness * 1.275 + 0.5,
    )));
  }

  function normalizeManualAdjustment(adjustment) {
    if (adjustment == null) return null;
    if (!isPlainObject(adjustment) ||
        Object.keys(adjustment).length !== MANUAL_ADJUSTMENT_FIELDS.length ||
        MANUAL_ADJUSTMENT_FIELDS.some(
          (field) => !Object.prototype.hasOwnProperty.call(adjustment, field))) {
      throw new TypeError("manual adjustment fields must match its schema exactly");
    }
    const contrast = adjustment.contrast_percent;
    const brightness = adjustment.brightness_percent;
    const threshold = expectedThreshold(brightness);
    if (adjustment.schema !== "org.whl.raster.manual-binary-adjust" ||
        adjustment.version !== 1 || !Number.isInteger(adjustment.version) ||
        adjustment.algorithm !== "grayscale-threshold-blend-v1" ||
        !Number.isInteger(contrast) || contrast < 0 || contrast > 100 ||
        !Number.isInteger(brightness) || brightness < -100 || brightness > 100 ||
        adjustment.threshold !== threshold ||
        adjustment.threshold_rule !==
          "round_half_up(127.5 - brightness_percent * 1.275), clamped_0_255" ||
        adjustment.comparison !== "grayscale_value > threshold") {
      throw new TypeError("manual adjustment is not a canonical raster recipe");
    }
    return Object.fromEntries(
      MANUAL_ADJUSTMENT_FIELDS.map((field) => [field, adjustment[field]]));
  }

  // Mirrors librarytool/processing/operations.py. Parameters are integers on
  // purpose: a float would have to serialize identically in Python and
  // JavaScript canonical JSON, and the two disagree on exponent formatting.
  const PROCESSING_OPERATION_SCHEMA = "org.whl.raster.processing-operation";
  const PROCESSING_OPERATION_VERSION = 1;
  const MAX_OPERATIONS_PER_RECIPE = 16;
  const PROCESSING_OPERATION_PARAMETERS = Object.freeze({
    "unsharp-mask-v1": Object.freeze({
      radius_tenths: [1, 500],
      amount_percent: [0, 500],
      threshold: [0, 255],
    }),
    "kernel-sharpen-v1": Object.freeze({ strength_percent: [0, 100] }),
    "gamma-v1": Object.freeze({ gamma_hundredths: [10, 1000] }),
    "contrast-v1": Object.freeze({ contrast_percent: [-100, 100] }),
    "channel-gain-v1": Object.freeze({
      red_percent: [0, 400],
      green_percent: [0, 400],
      blue_percent: [0, 400],
    }),
    "white-balance-v1": Object.freeze({
      mode: null,
      strength_percent: [0, 100],
      temperature: [-100, 100],
      tint: [-100, 100],
    }),
  });
  const PROCESSING_ALGORITHMS = Object.freeze(
    Object.keys(PROCESSING_OPERATION_PARAMETERS).sort(),
  );

  function normalizeProcessingOperation(operation) {
    if (!isPlainObject(operation)) {
      throw new TypeError("a processing operation must be an object");
    }
    const bounds = Object.prototype.hasOwnProperty.call(
      PROCESSING_OPERATION_PARAMETERS, operation.algorithm,
    ) ? PROCESSING_OPERATION_PARAMETERS[operation.algorithm] : null;
    if (!bounds) {
      throw new TypeError(
        `unsupported processing algorithm: ${String(operation.algorithm)}`);
    }
    const names = Object.keys(bounds);
    const expected = ["schema", "version", "algorithm", "rule", ...names];
    if (Object.keys(operation).length !== expected.length ||
        expected.some((key) =>
          !Object.prototype.hasOwnProperty.call(operation, key))) {
      throw new TypeError(
        `processing operation fields must match ${operation.algorithm} exactly`);
    }
    if (operation.schema !== PROCESSING_OPERATION_SCHEMA ||
        operation.version !== PROCESSING_OPERATION_VERSION ||
        typeof operation.rule !== "string" || !operation.rule) {
      throw new TypeError("unsupported processing operation schema");
    }
    if (operation.algorithm === "white-balance-v1") {
      if (operation.mode !== "gray_world" && operation.mode !== "manual") {
        throw new TypeError("white balance mode must be gray_world or manual");
      }
      if (operation.mode === "gray_world" &&
          (operation.temperature !== 0 || operation.tint !== 0)) {
        throw new TypeError(
          "gray_world white balance does not take temperature or tint");
      }
    }
    for (const name of names) {
      if (name === "mode") continue;
      const [minimum, maximum] = bounds[name];
      const value = operation[name];
      if (!Number.isInteger(value) || value < minimum || value > maximum) {
        throw new TypeError(
          `${name} must be an integer from ${minimum} through ${maximum}`);
      }
    }
    return Object.fromEntries(expected.map((key) => [key, operation[key]]));
  }

  function normalizeProcessingOperations(operations) {
    if (!Array.isArray(operations)) {
      throw new TypeError("operations must be an array");
    }
    if (operations.length === 0) {
      throw new TypeError(
        "operations must contain at least one operation or be absent");
    }
    if (operations.length > MAX_OPERATIONS_PER_RECIPE) {
      throw new TypeError(
        `operations must contain at most ${MAX_OPERATIONS_PER_RECIPE} entries`);
    }
    return operations.map((value) => normalizeProcessingOperation(value));
  }

  function serializeCorrectionTransformCommand(options = {}) {
    const pins = normalizeSourcePins(options.pins);
    const validation = validatePerspectiveQuad(options.quad);
    if (!validation.valid) {
      const error = new TypeError(validation.message);
      error.code = validation.code;
      throw error;
    }
    if (typeof options.rerunOcr !== "boolean") {
      throw new TypeError("rerunOcr must be boolean");
    }
    const command = {
      schema: TRANSFORM_COMMAND_SCHEMA,
      version: TRANSFORM_COMMAND_VERSION,
      item_id: pins.item_id,
      artifact_id: pins.artifact_id,
      artifact_revision: pins.artifact_revision,
      source_revision: pins.source_revision,
      source_sha256: pins.source_sha256,
      quad: cloneQuad(validation.quad),
      adjustment: normalizeManualAdjustment(options.adjustment),
      rerun_ocr: options.rerunOcr,
      operation_id: portableIdentifier(options.operationId, "operation_id"),
    };
    // The engine fingerprints this exact document, so the key is emitted only
    // when a mask is actually carried — a mask-less command has to keep the
    // bytes it had before masking existed. See _COMMAND_OPTIONAL_FIELDS in
    // librarytool/engine/correction_transforms.py.
    if (options.maskPolygon != null) {
      const mask = validateMaskPolygon(options.maskPolygon);
      if (!mask.valid) {
        const error = new TypeError(mask.message);
        error.code = mask.code;
        throw error;
      }
      command.mask_polygon = mask.polygon;
    }
    if (options.operations != null) {
      command.operations = normalizeProcessingOperations(options.operations);
    }
    // Like mask_polygon and operations, extraction is additive-optional: an
    // ordinary correction must retain the exact canonical bytes and engine
    // fingerprint it had before region extraction became a transform profile.
    if (options.extraction != null) {
      command.extraction = normalizeExtraction(options.extraction);
      if (command.extraction.source_kind !== "committed-mask" &&
          command.mask_polygon) {
        throw new TypeError(
          "an annotation extraction must not carry a committed mask polygon");
      }
      if (command.extraction.source_kind === "committed-mask" &&
          !command.mask_polygon) {
        throw new TypeError(
          "a committed-mask extraction requires a committed mask polygon");
      }
    }
    return command;
  }

  function isFormControlTarget(target) {
    if (!target || typeof target !== "object") return false;
    if (typeof target.closest === "function") {
      try {
        if (target.closest(
          "input, textarea, select, button, a[href], [contenteditable]:not([contenteditable='false']), [role='textbox']",
        )) return true;
      } catch (error) {
        // Minimal DOM test doubles may implement only a subset of selectors.
      }
    }
    const tagName = String(target.tagName || target.nodeName || "").toLowerCase();
    if (["input", "textarea", "select", "button", "a"].includes(tagName)) return true;
    const contentEditable = target.isContentEditable === true ||
      target.contentEditable === "" || target.contentEditable === "true";
    return contentEditable;
  }

  function visibleModal(documentRef) {
    if (!documentRef) return false;
    const candidates = [];
    const seen = new Set();
    for (const selector of [
      "dialog",
      "[aria-modal='true']",
      "[data-corrections-modal='true']",
    ]) {
      let matches = [];
      try {
        if (typeof documentRef.querySelectorAll === "function") {
          matches = Array.from(documentRef.querySelectorAll(selector));
        } else if (typeof documentRef.querySelector === "function") {
          const match = documentRef.querySelector(selector);
          if (match) matches = [match];
        }
      } catch (error) {
        // Minimal DOM test doubles may implement only a subset of selectors.
      }
      for (const match of matches) {
        if (!seen.has(match)) {
          seen.add(match);
          candidates.push(match);
        }
      }
    }
    return candidates.some((candidate) => {
      let cursor = candidate;
      while (cursor) {
        if (cursor.hidden === true ||
            typeof cursor.getAttribute === "function" &&
            cursor.getAttribute("aria-hidden") === "true") return false;
        cursor = cursor.parentNode;
      }
      const tagName = String(
        candidate.tagName || candidate.nodeName || "",
      ).toUpperCase();
      const attribute = (name) => typeof candidate.getAttribute === "function"
        ? candidate.getAttribute(name) : null;
      if (tagName === "DIALOG" &&
          candidate.open !== true &&
          attribute("open") == null &&
          attribute("aria-modal") !== "true") return false;
      return true;
    });
  }

  function canQueueTransform(state, pins) {
    return Boolean(
      state &&
      state.tool === TOOLS.PERSPECTIVE &&
      !state.gesture &&
      state.validation && state.validation.valid &&
      sourcePinsValid(pins) &&
      !["submitting", "queued", "complete"].includes(
        state.submission && state.submission.status,
      )
    );
  }

  function canQueuePerspectiveShortcut(eventContext, state, pins) {
    const event = eventContext || {};
    const isSpace = event.key === " " || event.key === "Spacebar" ||
      event.code === "Space";
    if (!isSpace || event.defaultPrevented === true || event.repeat === true ||
        event.isComposing === true || event.altKey === true ||
        event.ctrlKey === true || event.metaKey === true || event.shiftKey === true ||
        event.editorFocused !== true || event.modalOpen === true ||
        event.formControl === true || isFormControlTarget(event.target)) {
      return false;
    }
    return canQueueTransform(state, pins);
  }

  function resolveEscape(state, hasSelection = null) {
    if (!state) return null;
    if (state.gesture) {
      return {
        handled: true,
        action: { type: "CANCEL_GESTURE" },
        clearHostSelection: false,
      };
    }
    if (state.tool !== TOOLS.SELECT) {
      return {
        handled: true,
        action: { type: "SET_TOOL", tool: TOOLS.SELECT },
        clearHostSelection: false,
      };
    }
    if (state.selectedCorner !== null ||
        (hasSelection == null ? state.selectionPresent : hasSelection)) {
      return {
        handled: true,
        action: { type: "CLEAR_SELECTION" },
        clearHostSelection: true,
      };
    }
    return null;
  }

  return {
    COORDINATE_SPACE,
    FULL_FRAME_QUAD,
    GEOMETRY_EPSILON,
    HISTORY_LIMIT,
    FREEHAND_MIN_STEP,
    MAX_MASK_POLYGON_POINTS,
    MAX_OPERATIONS_PER_RECIPE,
    POLYGON_CLOSE_DISTANCE,
    MIN_MASK_POLYGON_POINTS,
    PROCESSING_ALGORITHMS,
    PROCESSING_OPERATION_PARAMETERS,
    PROCESSING_OPERATION_SCHEMA,
    PROCESSING_OPERATION_VERSION,
    MIN_NORMALIZED_EDGE_LENGTH,
    MIN_NORMALIZED_QUAD_AREA,
    POINT_LABELS,
    POINT_ORDER,
    PROPOSAL_SCHEMA,
    PROPOSAL_VERSION,
    TOOLS,
    TRANSFORM_COMMAND_ID,
    TRANSFORM_COMMAND_SCHEMA,
    TRANSFORM_COMMAND_VERSION,
    canQueuePerspectiveShortcut,
    canQueueTransform,
    clientToNormalized,
    containedImageRect,
    createImageEditorState,
    isFormControlTarget,
    nearestCornerIndex,
    normalizeManualAdjustment,
    normalizeProcessingOperation,
    normalizeProcessingOperations,
    normalizeSourcePins,
    normalizedToClient,
    reduceImageEditorState,
    resolveEscape,
    resolveInitialQuad,
    serializeCorrectionTransformCommand,
    sourcePinsValid,
    validateMaskPolygon,
    validatePerspectiveQuad,
    visibleModal,
  };
});

(function installCorrectionsArtifactModel(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else Object.assign(root.LibraryToolCorrections ||= {}, api);
})(typeof globalThis !== "undefined" ? globalThis : this, function artifactModelFactory() {
  "use strict";

  const MAX_PAGE_ITEMS = 512;
  const MAX_LINKS = 128;
  const MAX_LINEAGE = 128;
  const MAX_ASSERTIONS = 32;
  const MAX_JSON_DEPTH = 5;
  const MAX_JSON_KEYS = 256;
  const MAX_JSON_ARRAY = 256;
  const MAX_JSON_STRING = 4096;
  const DEFAULT_ROW_HEIGHT = 28;
  const DEFAULT_OVERSCAN = 6;
  const PORTABLE_ID_RE = /^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,255}$/;

  const ARTIFACT_GROUPS = Object.freeze([
    Object.freeze({ id: "generated-metadata", label: "Generated metadata" }),
    Object.freeze({ id: "ocr-text", label: "OCR and extracted text" }),
    Object.freeze({ id: "layout-regions", label: "Layout and Mistral regions" }),
    Object.freeze({ id: "source-images", label: "Source image captures" }),
    Object.freeze({ id: "extracted-figures", label: "Extracted figures" }),
    Object.freeze({ id: "processed-images", label: "Processed and corrected images" }),
    Object.freeze({ id: "transforms", label: "Transforms and recipes" }),
    Object.freeze({ id: "generated-images", label: "Generated and reworked images" }),
    Object.freeze({ id: "unknown", label: "Other artifacts" }),
  ]);
  const GROUP_IDS = new Set(ARTIFACT_GROUPS.map((group) => group.id));

  const KIND_GROUPS = Object.freeze({
    "about": "generated-metadata",
    "analysis": "generated-metadata",
    "generated-metadata": "generated-metadata",
    "metadata": "generated-metadata",
    "structured-metadata": "generated-metadata",
    "summary": "generated-metadata",

    "full-text": "ocr-text",
    "ocr": "ocr-text",
    "ocr-text": "ocr-text",
    "text": "ocr-text",
    "text-layer": "ocr-text",
    "transcription": "ocr-text",
    "translation": "ocr-text",

    "annotation": "layout-regions",
    "layout-box": "layout-regions",
    "mistral-box": "layout-regions",
    "region": "layout-regions",
    "regions": "layout-regions",
    "spatial-annotation": "layout-regions",
    "spatial-annotations": "layout-regions",

    "capture": "source-images",
    "captured-image": "source-images",
    "page-image": "source-images",
    "scan": "source-images",
    "source-image": "source-images",

    "extracted-figure": "extracted-figures",
    "figure": "extracted-figures",
    "illustration": "extracted-figures",

    "corrected-image": "processed-images",
    "perspective-corrected": "processed-images",
    "processed-image": "processed-images",
    "processed-source": "processed-images",

    "correction-transform": "transforms",
    "recipe": "transforms",
    "transform": "transforms",
    "transform-job": "transforms",

    "generated-image": "generated-images",
    "reworked-figure": "generated-images",
    "reworked-image": "generated-images",
  });

  const OBJECT_PREFIX = Object.freeze({
    artifact: "artifact",
    "raster-artifact": "artifact",
    annotation: "annotation",
    "spatial-annotation": "annotation",
    transform: "transform",
    job: "job",
  });

  function isObject(value) {
    return !!value && typeof value === "object" && !Array.isArray(value);
  }

  function text(value, maximum = 512) {
    if (value == null) return "";
    const result = String(value).replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f]/g, "");
    return result.slice(0, maximum);
  }

  function portableId(value, name, required = false) {
    const result = text(value, 256);
    if (!result) {
      if (required) throw new TypeError(`${name} is required`);
      return "";
    }
    if (!PORTABLE_ID_RE.test(result)) throw new TypeError(`${name} is invalid`);
    return result;
  }

  function boundedJson(value, state = { keys: 0 }, depth = 0) {
    if (depth > MAX_JSON_DEPTH) throw new TypeError("artifact data is too deeply nested");
    if (value == null || typeof value === "boolean") return value;
    if (typeof value === "number") {
      if (!Number.isFinite(value)) throw new TypeError("artifact data contains a non-finite number");
      return value;
    }
    if (typeof value === "string") return text(value, MAX_JSON_STRING);
    if (Array.isArray(value)) {
      if (value.length > MAX_JSON_ARRAY) throw new TypeError("artifact data array is too large");
      return value.map((entry) => boundedJson(entry, state, depth + 1));
    }
    if (!isObject(value)) throw new TypeError("artifact data must be portable JSON");
    const keys = Object.keys(value).sort();
    state.keys += keys.length;
    if (state.keys > MAX_JSON_KEYS) throw new TypeError("artifact data has too many fields");
    const result = {};
    for (const key of keys) {
      if (!key || key.length > 128 ||
          ["__proto__", "constructor", "prototype"].includes(key)) {
        throw new TypeError("artifact data contains an invalid field");
      }
      result[key] = boundedJson(value[key], state, depth + 1);
    }
    return result;
  }

  function safeBoundedJson(value, fallback) {
    try {
      return boundedJson(value);
    } catch (error) {
      return fallback;
    }
  }

  function freezeJson(value) {
    if (Array.isArray(value)) {
      value.forEach(freezeJson);
      return Object.freeze(value);
    }
    if (isObject(value)) {
      Object.values(value).forEach(freezeJson);
      return Object.freeze(value);
    }
    return value;
  }

  function normalizeObjectType(raw, kind) {
    const supplied = text(raw.object_type || raw.objectType || raw.type, 64).toLowerCase();
    if (supplied) {
      if (supplied.includes("annotation") || supplied === "region") {
        return "spatial-annotation";
      }
      if (supplied.includes("raster") || supplied === "artifact") {
        return "raster-artifact";
      }
      if (supplied.includes("transform")) return "transform";
      if (supplied.includes("job")) return "job";
      return supplied;
    }
    if (["spatial-annotation", "spatial-annotations", "annotation",
      "layout-box", "mistral-box", "region", "regions"].includes(kind)) {
      return "spatial-annotation";
    }
    if (kind.includes("transform") || kind === "recipe") return "transform";
    return "artifact";
  }

  function identifierFromRaw(raw, objectType) {
    if (typeof raw.key === "string" && raw.key.includes(":")) {
      const parts = raw.key.split(":");
      return portableId(parts.slice(1).join(":"), "artifact id", true);
    }
    const key = isObject(raw.key) ? raw.key : {};
    const candidates = objectType === "spatial-annotation"
      ? [raw.annotation_id, raw.annotationId, key.annotation_id, key.annotationId, raw.id]
      : [raw.artifact_id, raw.artifactId, key.artifact_id, key.artifactId, raw.id,
        raw.transform_id, raw.transformId, key.transform_id];
    const found = candidates.find((value) => value != null && value !== "");
    return portableId(found, `${objectType} id`, true);
  }

  function itemIdFromRaw(raw) {
    const key = isObject(raw.key) ? raw.key : {};
    return portableId(raw.item_id || raw.itemId || key.item_id || key.itemId, "item id");
  }

  function canonicalKey(objectType, id) {
    const prefix = OBJECT_PREFIX[objectType] || "object";
    return `${prefix}:${id}`;
  }

  function classifyArtifactGroup(raw, kind, objectType, family) {
    const explicit = text(raw.group || raw.group_id || raw.groupId, 64).toLowerCase();
    if (GROUP_IDS.has(explicit)) return explicit;
    if (objectType === "spatial-annotation" || family === "regions") {
      return "layout-regions";
    }
    if (KIND_GROUPS[kind]) return KIND_GROUPS[kind];
    if (family === "image") return "generated-images";
    if (family === "text") return "ocr-text";
    if (family === "metadata") return "generated-metadata";
    return "unknown";
  }

  function inferFamily(raw, kind, objectType) {
    const explicit = text(raw.family, 24).toLowerCase();
    if (["image", "text", "metadata", "regions", "unknown"].includes(explicit)) {
      return explicit;
    }
    const mediaType = text(raw.media_type || raw.mediaType, 128).toLowerCase();
    if (objectType === "spatial-annotation") return "regions";
    if (mediaType.startsWith("image/")) return "image";
    if (mediaType.startsWith("text/")) return "text";
    if (mediaType === "application/json" || mediaType.endsWith("+json")) return "metadata";
    const group = KIND_GROUPS[kind];
    if (group === "layout-regions") return "regions";
    if (["source-images", "extracted-figures", "processed-images",
      "generated-images"].includes(group)) return "image";
    if (group === "ocr-text") return "text";
    if (group === "generated-metadata") return "metadata";
    return "unknown";
  }

  function normalizeResourceState(raw) {
    const supplied = text(raw.resource_state || raw.resourceState, 24).toLowerCase();
    if (["available", "missing", "unavailable"].includes(supplied)) return supplied;
    if (raw.available === false) return "unavailable";
    if (raw.missing === true) return "missing";
    return raw.resource || raw.resource_ref || raw.resourceRef ? "available" : "unavailable";
  }

  function normalizeFreshness(raw) {
    const supplied = text(raw.freshness, 24).toLowerCase();
    if (["current", "stale", "untracked"].includes(supplied)) return supplied;
    if (raw.stale === true) return "stale";
    if (raw.stale === false) return "current";
    return "untracked";
  }

  function normalizeResourceRef(value) {
    if (!isObject(value)) return null;
    const id = portableId(
      value.resource_id || value.resourceId || value.id,
      "resource id",
    );
    const revision = text(value.revision, 512);
    if (!id || !revision) return null;
    return Object.freeze({
      id,
      revision,
      variant: portableId(value.variant || "display", "resource variant", true),
    });
  }

  function normalizeSource(value) {
    if (!isObject(value)) return Object.freeze({});
    const result = {
      representationId: portableId(
        value.representation_id || value.representationId,
        "representation id",
      ),
      representationRevision: text(
        value.representation_revision || value.representationRevision ||
          value.source_revision || value.sourceRevision,
        512,
      ),
      canvasId: portableId(value.canvas_id || value.canvasId, "canvas id"),
      canvasRevision: text(value.canvas_revision || value.canvasRevision, 512),
    };
    return Object.freeze(result);
  }

  function assertionArray(value, allowedOrigins) {
    if (!Array.isArray(value)) return Object.freeze([]);
    return Object.freeze(value.slice(0, MAX_ASSERTIONS).flatMap((entry) => {
      if (!isObject(entry)) return [];
      const origin = text(entry.origin, 24).toLowerCase();
      if (!allowedOrigins.has(origin)) return [];
      const result = {
        origin,
        revision: text(entry.revision, 512),
        text: text(entry.text, 16384),
        language: text(entry.language, 64),
        category: text(entry.category, 64),
        role: text(entry.role, 64),
        confidence: typeof entry.confidence === "number" &&
          Number.isFinite(entry.confidence) ? entry.confidence : null,
        provenance: freezeJson(safeBoundedJson(entry.provenance, {})),
      };
      return [Object.freeze(result)];
    }));
  }

  function effectiveCaption(assertions, supplied) {
    if (isObject(supplied)) {
      const normalized = assertionArray([supplied],
        new Set(["manual", "machine", "imported", "inherited"]));
      if (normalized.length) return normalized[0];
    }
    for (const origin of ["manual", "imported", "inherited", "machine"]) {
      const value = assertions.find((assertion) => assertion.origin === origin);
      if (value) return value;
    }
    return null;
  }

  function normalizeLineage(value) {
    if (!Array.isArray(value)) return Object.freeze([]);
    const seen = new Set();
    const result = [];
    for (const entry of value.slice(0, MAX_LINEAGE)) {
      if (!isObject(entry)) continue;
      const artifactId = portableId(
        entry.artifact_id || entry.artifactId || entry.id,
        "lineage artifact id",
      );
      if (!artifactId || seen.has(artifactId)) continue;
      seen.add(artifactId);
      result.push(Object.freeze({
        artifactId,
        artifactRevision: text(
          entry.artifact_revision || entry.artifactRevision || entry.revision,
          512,
        ),
        relation: portableId(entry.relation || "derived_from", "lineage relation", true),
      }));
    }
    return Object.freeze(result);
  }

  function normalizeLinkedKeys(raw, objectType, lineage) {
    const values = [];
    if (Array.isArray(raw.linked_keys || raw.linkedKeys)) {
      values.push(...(raw.linked_keys || raw.linkedKeys));
    }
    if (Array.isArray(raw.linked_artifact_ids || raw.linkedArtifactIds)) {
      values.push(...(raw.linked_artifact_ids || raw.linkedArtifactIds)
        .map((id) => `artifact:${id}`));
    }
    if (raw.linked_artifact_id || raw.linkedArtifactId) {
      values.push(`artifact:${raw.linked_artifact_id || raw.linkedArtifactId}`);
    }
    if (raw.source_artifact_id || raw.sourceArtifactId) {
      values.push(`artifact:${raw.source_artifact_id || raw.sourceArtifactId}`);
    }
    values.push(...lineage.map((entry) => `artifact:${entry.artifactId}`));

    const result = [];
    const seen = new Set();
    for (const candidate of values.slice(0, MAX_LINKS)) {
      const rawKey = text(candidate, 520);
      const key = rawKey.includes(":")
        ? rawKey
        : canonicalKey(objectType, portableId(rawKey, "linked id", true));
      if (!/^[a-z][a-z0-9-]*:[A-Za-z0-9][A-Za-z0-9._:@+-]{0,255}$/.test(key) ||
          seen.has(key)) continue;
      seen.add(key);
      result.push(key);
    }
    return Object.freeze(result);
  }

  function decodeArtifactSummary(raw) {
    if (!isObject(raw)) throw new TypeError("artifact summary must be an object");
    const kind = text(raw.kind || raw.type || "unknown", 64).toLowerCase() || "unknown";
    const objectType = normalizeObjectType(raw, kind);
    const id = identifierFromRaw(raw, objectType);
    const key = canonicalKey(objectType, id);
    const family = inferFamily(raw, kind, objectType);
    const group = classifyArtifactGroup(raw, kind, objectType, family);
    const lineage = normalizeLineage(raw.lineage);
    const captionAssertions = assertionArray(
      raw.caption_assertions || raw.captionAssertions,
      new Set(["manual", "machine", "imported", "inherited"]),
    );
    const categoryAssignments = assertionArray(
      raw.category_assignments || raw.categoryAssignments,
      new Set(["manual", "inherited", "suggested"]),
    );
    const roleAssignments = assertionArray(
      raw.role_assignments || raw.roleAssignments,
      new Set(["manual", "machine", "imported"]),
    );
    const resourceRef = normalizeResourceRef(
      raw.resource_ref || raw.resourceRef || raw.resource,
    );
    const resourceState = normalizeResourceState(raw);
    const result = {
      key,
      objectType,
      group,
      family,
      itemId: itemIdFromRaw(raw),
      id,
      revision: text(raw.revision, 512),
      kind,
      label: text(raw.label || raw.name || id, 512),
      mediaType: text(raw.media_type || raw.mediaType, 128).toLowerCase(),
      resourceState: resourceState === "available" && !resourceRef && family === "image"
        ? "unavailable" : resourceState,
      freshness: normalizeFreshness(raw),
      generated: raw.generated === true ||
        ["generated-metadata", "generated-images"].includes(group) ||
        text(raw.provenance && raw.provenance.origin, 64).toLowerCase() === "generated",
      source: normalizeSource(raw.source || {
        representation_id: raw.source_representation_id || raw.sourceRepresentationId,
        representation_revision: raw.source_revision || raw.sourceRevision,
        canvas_id: raw.canvas_id || raw.canvasId,
        canvas_revision: raw.canvas_revision || raw.canvasRevision,
      }),
      lineage,
      linkedKeys: null,
      effectiveCategory: text(
        raw.effective_category || raw.effectiveCategory ||
          (categoryAssignments[0] && categoryAssignments[0].category),
        64,
      ),
      effectiveRole: text(
        raw.effective_role || raw.effectiveRole ||
          (roleAssignments[0] && roleAssignments[0].role),
        64,
      ),
      categoryAssignments,
      roleAssignments,
      captionAssertions,
      effectiveCaption: effectiveCaption(
        captionAssertions,
        raw.effective_caption || raw.effectiveCaption,
      ),
      provenance: freezeJson(safeBoundedJson(raw.provenance, {})),
      metadata: freezeJson(safeBoundedJson(raw.metadata, {})),
      resourceRef,
    };
    result.linkedKeys = normalizeLinkedKeys(raw, objectType, lineage)
      .filter((linkedKey) => linkedKey !== key);
    result.linkedKeys = Object.freeze(result.linkedKeys);
    return Object.freeze(result);
  }

  function decodeArtifactPage(value, expectedGroup = "") {
    if (!isObject(value)) throw new TypeError("artifact page must be an object");
    const rows = value.items || value.artifacts || value.annotations;
    if (!Array.isArray(rows)) throw new TypeError("artifact page requires an items array");
    if (rows.length > MAX_PAGE_ITEMS) throw new TypeError("artifact page is too large");
    const items = rows.map(decodeArtifactSummary);
    if (expectedGroup && items.some((item) => item.group !== expectedGroup)) {
      throw new TypeError("artifact page contains an item from another group");
    }
    const nextCursor = value.next_cursor != null ? value.next_cursor : value.nextCursor;
    return Object.freeze({
      revision: text(value.revision, 512),
      items: Object.freeze(items),
      nextCursor: nextCursor == null || nextCursor === "" ? null : text(nextCursor, 1024),
    });
  }

  function buildLinkIndex(items) {
    const result = new Map();
    const keys = new Set(items.map((item) => item.key));
    const add = (from, to) => {
      if (!result.has(from)) result.set(from, new Set());
      result.get(from).add(to);
    };
    for (const item of items) {
      if (!result.has(item.key)) result.set(item.key, new Set());
      for (const linked of item.linkedKeys) {
        add(item.key, linked);
        add(linked, item.key);
      }
    }
    for (const [key, linked] of result) {
      result.set(key, Object.freeze(Array.from(linked)
        .filter((candidate) => candidate !== key)
        .sort((first, second) => {
          const firstKnown = keys.has(first) ? 0 : 1;
          const secondKnown = keys.has(second) ? 0 : 1;
          return firstKnown - secondKnown || first.localeCompare(second);
        })));
    }
    return result;
  }

  function stateForGroup(groupStates, groupId) {
    if (groupStates instanceof Map) return groupStates.get(groupId) || {};
    return isObject(groupStates) ? groupStates[groupId] || {} : {};
  }

  function buildArtifactTreeRows(groupStates, expandedGroups) {
    const expanded = expandedGroups instanceof Set
      ? expandedGroups : new Set(expandedGroups || []);
    const rows = [];
    for (const group of ARTIFACT_GROUPS) {
      const state = stateForGroup(groupStates, group.id);
      const items = Array.isArray(state.items) ? state.items : [];
      rows.push(Object.freeze({
        key: `group:${group.id}`,
        type: "group",
        group: group.id,
        label: group.label,
        level: 1,
        expanded: expanded.has(group.id),
        count: Number.isSafeInteger(state.total) ? state.total : items.length,
        loading: state.loading === true,
        error: state.error || null,
      }));
      if (!expanded.has(group.id)) continue;
      for (const item of items) {
        rows.push(Object.freeze({
          key: item.key,
          type: "item",
          group: group.id,
          label: item.label,
          level: 2,
          item,
        }));
      }
      if (state.loading && items.length === 0) {
        rows.push(Object.freeze({
          key: `status:${group.id}:loading`,
          type: "status",
          group: group.id,
          label: "Loading artifacts…",
          level: 2,
          disabled: true,
        }));
      } else if (state.error && items.length === 0) {
        rows.push(Object.freeze({
          key: `status:${group.id}:error`,
          type: "status",
          group: group.id,
          label: text(state.error.message || state.error, 256),
          level: 2,
          disabled: true,
        }));
      } else if (state.loaded && items.length === 0) {
        rows.push(Object.freeze({
          key: `status:${group.id}:empty`,
          type: "status",
          group: group.id,
          label: "No artifacts in this group",
          level: 2,
          disabled: true,
        }));
      }
      if (!state.loading && state.nextCursor) {
        rows.push(Object.freeze({
          key: `more:${group.id}`,
          type: "more",
          group: group.id,
          label: "Load more",
          level: 2,
        }));
      }
    }
    return Object.freeze(rows);
  }

  function virtualArtifactWindow(rows, options = {}) {
    const rowHeight = Number.isFinite(options.rowHeight) && options.rowHeight >= 18
      ? options.rowHeight : DEFAULT_ROW_HEIGHT;
    const viewportHeight = Number.isFinite(options.viewportHeight) &&
      options.viewportHeight > 0 ? options.viewportHeight : rowHeight * 12;
    const scrollTop = Number.isFinite(options.scrollTop) && options.scrollTop > 0
      ? options.scrollTop : 0;
    const overscan = Number.isSafeInteger(options.overscan) && options.overscan >= 0
      ? options.overscan : DEFAULT_OVERSCAN;
    const visible = Math.max(1, Math.ceil(viewportHeight / rowHeight));
    let start = Math.max(0, Math.floor(scrollTop / rowHeight) - overscan);
    let end = Math.min(rows.length, start + visible + overscan * 2);
    const activeIndex = options.activeKey
      ? rows.findIndex((row) => row.key === options.activeKey) : -1;
    if (activeIndex >= 0 && (activeIndex < start || activeIndex >= end)) {
      start = Math.max(0, activeIndex - overscan);
      end = Math.min(rows.length, Math.max(start + visible + overscan * 2,
        activeIndex + 1));
      start = Math.max(0, Math.min(start, end - visible - overscan * 2));
    }
    return Object.freeze({
      start,
      end,
      rowHeight,
      totalHeight: rows.length * rowHeight,
      paddingTop: start * rowHeight,
      paddingBottom: Math.max(0, (rows.length - end) * rowHeight),
      rows: Object.freeze(rows.slice(start, end)),
    });
  }

  function mergeArtifactItems(current, incoming) {
    const values = new Map((current || []).map((item) => [item.key, item]));
    for (const item of incoming || []) values.set(item.key, item);
    return Object.freeze(Array.from(values.values()).sort((first, second) =>
      first.label.localeCompare(second.label) || first.key.localeCompare(second.key)));
  }

  return {
    ARTIFACT_GROUPS,
    DEFAULT_OVERSCAN,
    DEFAULT_ROW_HEIGHT,
    MAX_PAGE_ITEMS,
    boundedJson,
    buildArtifactTreeRows,
    buildLinkIndex,
    classifyArtifactGroup,
    decodeArtifactPage,
    decodeArtifactSummary,
    mergeArtifactItems,
    virtualArtifactWindow,
  };
});

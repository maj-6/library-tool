(function exposeWorkbenchLaunch(root, factory) {
  "use strict";

  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root && typeof root === "object") root.LibraryToolWorkbenchLaunch = api;
})(typeof globalThis === "object" ? globalThis : this, function buildWorkbenchLaunch() {
  "use strict";

  const CONTEXT_SCHEMA = "librarytool.workbench-context/1";
  const IDENTIFIER_RE = /^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,255}$/;
  const PROFILE_RE = /^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$/;
  const RESERVED_PROFILE_PARTS = new Set([
    ".",
    "..",
    "__proto__",
    "constructor",
    "prototype",
  ]);
  const OPTIONAL_IDENTIFIERS = [
    "item_id",
    "representation_id",
    "canvas_id",
    "artifact_id",
    "annotation_id",
  ];

  function identifier(value, name, required) {
    if (value == null || value === "") {
      if (required) throw new TypeError(`${name} is required`);
      return null;
    }
    if (typeof value !== "string" || !IDENTIFIER_RE.test(value)) {
      throw new TypeError(`${name} must be an opaque transport-safe identifier`);
    }
    return value;
  }

  function profileKey(value) {
    if (value == null || value === "") return null;
    if (typeof value !== "string" || !PROFILE_RE.test(value) ||
        value.split("/").some((part) =>
          !part || RESERVED_PROFILE_PARTS.has(part))) {
      throw new TypeError("uiProfileKey must be an opaque profile key");
    }
    return value;
  }

  function portableObject(value, name) {
    if (value == null) return null;
    if (!value || typeof value !== "object" || Array.isArray(value)) {
      throw new TypeError(`${name} must be a portable object`);
    }
    let encoded;
    try {
      encoded = JSON.stringify(value);
    } catch (error) {
      throw new TypeError(`${name} must be portable JSON`);
    }
    if (encoded === undefined) throw new TypeError(`${name} must be portable JSON`);
    const result = JSON.parse(encoded);
    if (!result || typeof result !== "object" || Array.isArray(result)) {
      throw new TypeError(`${name} must be a portable object`);
    }
    return result;
  }

  function createContext(options = {}) {
    if (!options || typeof options !== "object" || Array.isArray(options)) {
      throw new TypeError("workbench launch options must be an object");
    }
    const context = {
      schema: CONTEXT_SCHEMA,
      workbench_id: identifier(options.workbenchId, "workbenchId", true),
      workspace_id: identifier(options.workspaceId, "workspaceId", true),
    };
    for (const field of OPTIONAL_IDENTIFIERS) {
      const camelName = field.replace(/_([a-z])/g, (_match, letter) => letter.toUpperCase());
      const value = identifier(options[camelName], camelName, false);
      if (value !== null) context[field] = value;
    }
    if (options.resourceRevision != null) {
      const revision = options.resourceRevision;
      if (!((Number.isSafeInteger(revision) && revision >= 0) ||
            (typeof revision === "string" && IDENTIFIER_RE.test(revision)))) {
        throw new TypeError("resourceRevision must be a non-negative integer or revision token");
      }
      context.resource_revision = revision;
    }
    for (const [optionName, field] of [["viewHint", "view_hint"], ["origin", "origin"]]) {
      const value = portableObject(options[optionName], optionName);
      if (value !== null) context[field] = value;
    }
    const profile = profileKey(options.uiProfileKey);
    if (profile !== null) context.ui_profile_key = profile;
    return Object.freeze(context);
  }

  async function open(desktop, options = {}) {
    if (!desktop || !desktop.workbenches ||
        typeof desktop.workbenches.open !== "function") {
      const error = new Error("independent workbenches require the desktop application");
      error.code = "WORKBENCH_UNAVAILABLE";
      throw error;
    }
    const context = createContext(options);
    const result = await desktop.workbenches.open(context, {
      newWindow: options.newWindow === true,
    });
    if (!result || result.ok !== true) {
      const error = new Error(result && result.message
        ? result.message
        : "desktop rejected the workbench request");
      error.code = result && result.error
        ? String(result.error)
        : "WORKBENCH_OPEN_FAILED";
      throw error;
    }
    return result;
  }

  return Object.freeze({ CONTEXT_SCHEMA, createContext, open });
});

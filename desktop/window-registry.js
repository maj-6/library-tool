"use strict";

const crypto = require("crypto");
const fs = require("fs");
const path = require("path");


const WORKBENCH_CONTEXT_SCHEMA = "librarytool.workbench-context/1";
const WINDOW_STATE_SCHEMA = "librarytool.desktop-window-state/1";
const MAX_CONTEXT_BYTES = 16 * 1024;
const MAX_HINT_DEPTH = 5;
const MAX_HINT_KEYS = 128;
const ID_RE = /^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,255}$/;
const PROFILE_RE = /^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$/;
const CONTEXT_FIELDS = new Set([
  "schema",
  "workbench_id",
  "workspace_id",
  "item_id",
  "representation_id",
  "canvas_id",
  "artifact_id",
  "annotation_id",
  "resource_revision",
  "view_hint",
  "origin",
  "ui_profile_key",
]);
const OPTIONAL_ID_FIELDS = [
  "item_id",
  "representation_id",
  "canvas_id",
  "artifact_id",
  "annotation_id",
];


function isPlainObject(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}


function checkedIdentifier(value, name, required = false) {
  if (value == null || value === "") {
    if (required) throw new TypeError(`${name} is required`);
    return null;
  }
  if (typeof value !== "string" || !ID_RE.test(value)) {
    throw new TypeError(`${name} must be an opaque transport-safe identifier`);
  }
  return value;
}


function checkedProfileKey(value, fallback) {
  const candidate = value == null || value === "" ? fallback : value;
  const reserved = new Set([".", "..", "__proto__", "constructor", "prototype"]);
  if (typeof candidate !== "string" || !PROFILE_RE.test(candidate) ||
      candidate.split("/").some((part) => !part || reserved.has(part))) {
    throw new TypeError("ui_profile_key must be an opaque profile key");
  }
  return candidate;
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
    if (!key || key.length > 128 || ["__proto__", "constructor", "prototype"].includes(key)) {
      throw new TypeError("context hint contains an invalid field name");
    }
    const entry = value[key];
    if (entry === undefined || typeof entry === "function" || typeof entry === "symbol" ||
        typeof entry === "bigint") {
      throw new TypeError("context hint must be portable JSON");
    }
    result[key] = canonicalPortableValue(entry, state, depth + 1);
  }
  return result;
}


function normalizeWorkbenchContext(value, options = {}) {
  if (!isPlainObject(value)) throw new TypeError("workbench context must be an object");
  const unknown = Object.keys(value).filter((key) => !CONTEXT_FIELDS.has(key));
  if (unknown.length) throw new TypeError(`unknown workbench context field: ${unknown[0]}`);
  if (value.schema !== WORKBENCH_CONTEXT_SCHEMA) {
    throw new TypeError(`workbench context schema must be ${WORKBENCH_CONTEXT_SCHEMA}`);
  }

  const workbenchId = checkedIdentifier(value.workbench_id, "workbench_id", true);
  if (options.expectedWorkbenchId && workbenchId !== options.expectedWorkbenchId) {
    throw new TypeError("workbench context targets a different workbench");
  }
  const result = {
    schema: WORKBENCH_CONTEXT_SCHEMA,
    workbench_id: workbenchId,
    workspace_id: checkedIdentifier(value.workspace_id, "workspace_id", true),
  };
  for (const field of OPTIONAL_ID_FIELDS) {
    const normalized = checkedIdentifier(value[field], field, false);
    if (normalized !== null) result[field] = normalized;
  }

  if (value.resource_revision != null) {
    const revision = value.resource_revision;
    if (Number.isSafeInteger(revision) && revision >= 0) {
      result.resource_revision = revision;
    } else if (typeof revision === "string" && ID_RE.test(revision)) {
      result.resource_revision = revision;
    } else {
      throw new TypeError("resource_revision must be a non-negative integer or revision token");
    }
  }
  const hintState = { keys: 0 };
  for (const field of ["view_hint", "origin"]) {
    if (value[field] == null) continue;
    if (!isPlainObject(value[field])) throw new TypeError(`${field} must be an object`);
    result[field] = canonicalPortableValue(value[field], hintState);
  }
  result.ui_profile_key = checkedProfileKey(
    value.ui_profile_key,
    options.defaultUiProfileKey || `${workbenchId}/default`,
  );
  if (Buffer.byteLength(JSON.stringify(result), "utf8") > MAX_CONTEXT_BYTES) {
    throw new TypeError("workbench context is too large");
  }
  return result;
}


function workbenchReuseKey(context) {
  return JSON.stringify([
    context.workbench_id,
    context.workspace_id,
    context.item_id || null,
    context.representation_id || null,
  ]);
}


function numberOr(value, fallback) {
  return Number.isFinite(value) ? Math.round(value) : fallback;
}


function normalizedArea(display) {
  const raw = display && (display.workArea || display.bounds || display);
  if (!raw) return null;
  const width = numberOr(raw.width, 0);
  const height = numberOr(raw.height, 0);
  if (width < 1 || height < 1) return null;
  return {
    x: numberOr(raw.x, 0),
    y: numberOr(raw.y, 0),
    width,
    height,
  };
}


function intersectionArea(left, right) {
  const width = Math.max(0,
    Math.min(left.x + left.width, right.x + right.width) - Math.max(left.x, right.x));
  const height = Math.max(0,
    Math.min(left.y + left.height, right.y + right.height) - Math.max(left.y, right.y));
  return width * height;
}


function clamp(value, low, high) {
  return Math.min(Math.max(value, low), high);
}


function clampWindowBounds(savedBounds, defaultBounds, displays) {
  const defaults = defaultBounds || {};
  const minimumWidth = Math.max(1, numberOr(defaults.minWidth, 640));
  const minimumHeight = Math.max(1, numberOr(defaults.minHeight, 480));
  const wanted = {
    x: Number.isFinite(savedBounds && savedBounds.x) ? Math.round(savedBounds.x) : null,
    y: Number.isFinite(savedBounds && savedBounds.y) ? Math.round(savedBounds.y) : null,
    width: Math.max(minimumWidth,
      numberOr(savedBounds && savedBounds.width, numberOr(defaults.width, 1200))),
    height: Math.max(minimumHeight,
      numberOr(savedBounds && savedBounds.height, numberOr(defaults.height, 800))),
  };
  const areas = (Array.isArray(displays) ? displays : [])
    .map(normalizedArea).filter(Boolean);
  if (!areas.length) {
    return {
      x: wanted.x == null ? numberOr(defaults.x, 0) : wanted.x,
      y: wanted.y == null ? numberOr(defaults.y, 0) : wanted.y,
      width: wanted.width,
      height: wanted.height,
    };
  }

  let target = areas[0];
  let bestIntersection = 0;
  if (wanted.x != null && wanted.y != null) {
    for (const area of areas) {
      const overlap = intersectionArea({
        x: wanted.x, y: wanted.y, width: wanted.width, height: wanted.height,
      }, area);
      if (overlap > bestIntersection) {
        target = area;
        bestIntersection = overlap;
      }
    }
  }
  const width = Math.min(wanted.width, target.width);
  const height = Math.min(wanted.height, target.height);
  const centered = bestIntersection === 0 || wanted.x == null || wanted.y == null;
  const x = centered
    ? target.x + Math.floor((target.width - width) / 2)
    : clamp(wanted.x, target.x, target.x + target.width - width);
  const y = centered
    ? target.y + Math.floor((target.height - height) / 2)
    : clamp(wanted.y, target.y, target.y + target.height - height);
  return { x, y, width, height };
}


class JsonWindowStateStore {
  constructor(filePath) {
    this.filePath = filePath;
    this.document = { schema: WINDOW_STATE_SCHEMA, profiles: Object.create(null) };
    try {
      const parsed = JSON.parse(fs.readFileSync(filePath, "utf8"));
      if (parsed && parsed.schema === WINDOW_STATE_SCHEMA && isPlainObject(parsed.profiles)) {
        this.document.profiles = parsed.profiles;
      }
    } catch (error) {
      if (error && error.code !== "ENOENT") {
        console.warn("[window registry] could not read window state:", error.message);
      }
    }
  }

  get(uiProfileKey, workbenchId) {
    const profile = this.document.profiles[uiProfileKey];
    const value = profile && profile[workbenchId];
    return isPlainObject(value) ? JSON.parse(JSON.stringify(value)) : null;
  }

  set(uiProfileKey, workbenchId, value) {
    if (!isPlainObject(this.document.profiles[uiProfileKey])) {
      this.document.profiles[uiProfileKey] = Object.create(null);
    }
    this.document.profiles[uiProfileKey][workbenchId] = JSON.parse(JSON.stringify(value));
    try {
      fs.mkdirSync(path.dirname(this.filePath), { recursive: true });
      const temporary = `${this.filePath}.${process.pid}.tmp`;
      fs.writeFileSync(temporary, JSON.stringify(this.document, null, 2) + "\n", "utf8");
      fs.renameSync(temporary, this.filePath);
    } catch (error) {
      console.warn("[window registry] could not persist window state:", error.message);
    }
  }
}


function validWindow(win) {
  return !!win && typeof win.isDestroyed === "function" && !win.isDestroyed() &&
    !!win.webContents;
}


class WorkbenchWindowRegistry {
  constructor(options = {}) {
    if (!options.origin) throw new TypeError("registry origin is required");
    this.origin = new URL(options.origin).origin;
    this.definitions = options.definitions || {};
    this.stateStore = options.stateStore || { get: () => null, set: () => {} };
    this.getDisplays = options.getDisplays || (() => []);
    this.makeWindowId = options.makeWindowId || (() => `window-${crypto.randomUUID()}`);
    this.byWindowId = new Map();
    this.byWebContentsId = new Map();
  }

  registerManager(win, options = {}) {
    return this._register(win, {
      role: "manager",
      workbenchId: "manager",
      documentPath: options.documentPath || "/",
      context: null,
      reuseKey: null,
      uiProfileKey: null,
      persistBounds: false,
      restoredState: null,
    });
  }

  open(request, createWindow) {
    if (!isPlainObject(request)) throw new TypeError("workbench open request must be an object");
    if (request.newWindow != null && typeof request.newWindow !== "boolean") {
      throw new TypeError("newWindow must be a boolean");
    }
    const requestedId = request.context && request.context.workbench_id;
    const definition = Object.prototype.hasOwnProperty.call(
      this.definitions, requestedId) ? this.definitions[requestedId] : null;
    if (!definition) throw new TypeError("unknown workbench");
    const context = normalizeWorkbenchContext(request.context, {
      expectedWorkbenchId: requestedId,
      defaultUiProfileKey: definition.defaultUiProfileKey,
    });
    const reuseKey = workbenchReuseKey(context);
    if (!request.newWindow) {
      const existing = Array.from(this.byWindowId.values()).find((record) =>
        record.role === "workbench" && record.reuseKey === reuseKey &&
        validWindow(record.window));
      if (existing) {
        existing.context = context;
        existing.uiProfileKey = context.ui_profile_key;
        this.sendContext(existing);
        this._focus(existing.window);
        return { record: existing, reused: true };
      }
    }
    if (typeof createWindow !== "function") throw new TypeError("createWindow is required");

    const savedState = this.stateStore.get(context.ui_profile_key, requestedId) || {};
    const restoredState = {
      bounds: isPlainObject(savedState.bounds) ? savedState.bounds : null,
      maximized: savedState.maximized === true,
    };
    const bounds = clampWindowBounds(
      restoredState.bounds,
      {
        width: definition.width,
        height: definition.height,
        minWidth: definition.minWidth,
        minHeight: definition.minHeight,
      },
      this.getDisplays(),
    );
    const win = createWindow({ definition, context, bounds, restoredState });
    const record = this._register(win, {
      role: "workbench",
      workbenchId: requestedId,
      documentPath: definition.documentPath,
      context,
      reuseKey,
      uiProfileKey: context.ui_profile_key,
      persistBounds: true,
      restoredState,
    });
    return { record, reused: false };
  }

  _register(win, values) {
    if (!validWindow(win) || !Number.isInteger(win.webContents.id)) {
      throw new TypeError("registered window must have a live webContents identity");
    }
    if (this.byWebContentsId.has(win.webContents.id)) {
      throw new TypeError("webContents is already registered");
    }
    const documentPath = String(values.documentPath || "");
    if (!documentPath.startsWith("/") || documentPath.includes("?") ||
        documentPath.includes("#")) {
      throw new TypeError("registered document path is invalid");
    }
    const documentUrl = new URL(documentPath, this.origin + "/");
    if (documentUrl.origin !== this.origin) {
      throw new TypeError("registered document path escapes the application origin");
    }
    const record = {
      windowId: this.makeWindowId(),
      role: values.role,
      workbenchId: values.workbenchId,
      documentPath,
      documentUrl: documentUrl.href,
      context: values.context,
      reuseKey: values.reuseKey,
      uiProfileKey: values.uiProfileKey,
      persistBounds: values.persistBounds,
      restoredState: values.restoredState,
      window: win,
    };
    this.byWindowId.set(record.windowId, record);
    this.byWebContentsId.set(win.webContents.id, record);
    if (record.persistBounds && typeof win.on === "function") {
      win.on("close", () => this._saveBounds(record));
    }
    if (typeof win.on === "function") {
      win.on("closed", () => this.unregister(record));
    }
    return record;
  }

  _saveBounds(record) {
    if (!record.persistBounds || !validWindow(record.window)) return;
    try {
      const bounds = record.window.getNormalBounds();
      this.stateStore.set(record.uiProfileKey, record.workbenchId, {
        bounds,
        maximized: typeof record.window.isMaximized === "function" &&
          record.window.isMaximized(),
      });
    } catch (error) {
      console.warn("[window registry] could not capture window bounds:", error.message);
    }
  }

  unregister(record) {
    if (!record) return false;
    const current = this.byWindowId.get(record.windowId);
    if (current !== record) return false;
    this.byWindowId.delete(record.windowId);
    if (record.window && record.window.webContents) {
      this.byWebContentsId.delete(record.window.webContents.id);
    }
    return true;
  }

  recordForEvent(event, isTrustedDocumentUrl) {
    if (!event || !event.sender || !event.senderFrame) return null;
    const record = this.byWebContentsId.get(event.sender.id);
    if (!record || !validWindow(record.window) ||
        event.sender !== record.window.webContents ||
        event.senderFrame !== record.window.webContents.mainFrame ||
        typeof isTrustedDocumentUrl !== "function" ||
        !isTrustedDocumentUrl(event.senderFrame.url, this.origin, record.documentPath)) {
      return null;
    }
    return record;
  }

  recordForWebContents(webContents, isTrustedDocumentUrl) {
    if (!webContents || !Number.isInteger(webContents.id)) return null;
    const record = this.byWebContentsId.get(webContents.id);
    if (!record || !validWindow(record.window) ||
        webContents !== record.window.webContents ||
        typeof isTrustedDocumentUrl !== "function") return null;
    const frame = record.window.webContents.mainFrame;
    if (!frame || !isTrustedDocumentUrl(frame.url, this.origin, record.documentPath)) {
      return null;
    }
    return record;
  }

  trustForWebRequest(webContentsId, isTrustedDocumentUrl) {
    const record = this.byWebContentsId.get(webContentsId);
    if (!record || !validWindow(record.window) ||
        typeof isTrustedDocumentUrl !== "function") return null;
    const frame = record.window.webContents.mainFrame;
    if (!frame || !isTrustedDocumentUrl(frame.url, this.origin, record.documentPath)) {
      return null;
    }
    return {
      origin: this.origin,
      webContentsId: record.window.webContents.id,
      mainFrame: frame,
      documentPath: record.documentPath,
    };
  }

  contextForEvent(event, isTrustedDocumentUrl) {
    const record = this.recordForEvent(event, isTrustedDocumentUrl);
    return record && record.role === "workbench"
      ? JSON.parse(JSON.stringify(record.context)) : null;
  }

  sendContext(record) {
    if (!record || record.role !== "workbench" || !validWindow(record.window) ||
        typeof record.window.webContents.send !== "function") return false;
    record.window.webContents.send(
      "workbench:context",
      JSON.parse(JSON.stringify(record.context)),
    );
    return true;
  }

  _focus(win) {
    if (!validWindow(win)) return;
    if (typeof win.isMinimized === "function" && win.isMinimized() &&
        typeof win.restore === "function") win.restore();
    if (typeof win.show === "function") win.show();
    if (typeof win.focus === "function") win.focus();
  }

  revokeAll() {
    this.byWindowId.clear();
    this.byWebContentsId.clear();
  }
}


module.exports = {
  JsonWindowStateStore,
  WorkbenchWindowRegistry,
  WORKBENCH_CONTEXT_SCHEMA,
  WINDOW_STATE_SCHEMA,
  clampWindowBounds,
  normalizeWorkbenchContext,
  workbenchReuseKey,
};

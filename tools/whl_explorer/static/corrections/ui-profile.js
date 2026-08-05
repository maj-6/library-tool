(function installCorrectionsProfile(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else {
    Object.assign(root.LibraryToolCorrections ||= {}, api);
    api.installSharedAppearance({ windowRef: root, documentRef: root.document });
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function profileFactory() {
  "use strict";

  const PROFILE_SCHEMA = "librarytool.corrections-ui-profile/1";
  const TOOL_PROFILE_SCHEMA = "librarytool.corrections-ui-tool-profile/1";
  const PROFILE_KEY_RE = /^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$/;
  const TOOL_NAME_RE = /^[A-Za-z][A-Za-z0-9_-]{0,63}$/;
  const RESERVED_SEGMENTS = new Set([".", "..", "__proto__", "constructor", "prototype"]);
  const SHARED_SETTINGS_KEY = "whl_cad_settings_v1";
  const DEFAULT_THEME = "sage";
  const BUILTIN_THEMES = new Set([
    "sage", "ledger", "foolscap", "vellum", "linen", "porcelain", "slate",
  ]);
  const LEGACY_THEMES = Object.freeze({
    "": DEFAULT_THEME,
    platinum: "linen",
    quarto: "linen",
    pewter: "linen",
    folio: "linen",
    redmond: "linen",
    motif: "linen",
    scope: "linen",
    "terminal-amber": "vellum",
    "blueprint-linen": "linen",
    oxblood: "ledger",
    herbarium: "vellum",
    blueprint: "linen",
    modern: "linen",
    dark: "linen",
    stone: "linen",
    midnight: "linen",
    cde: "linen",
    xp2003: "linen",
    acad: "linen",
    workstation: "linen",
    mainframe: "vellum",
    graphite: "linen",
  });
  const appliedAppearanceProperties = new WeakMap();

  function plainObject(value) {
    return !!value && typeof value === "object" && !Array.isArray(value);
  }

  function normalizedThemeBase(value) {
    const requested = typeof value === "string" ? value : "";
    const migrated = Object.prototype.hasOwnProperty.call(LEGACY_THEMES, requested)
      ? LEGACY_THEMES[requested] : requested;
    return BUILTIN_THEMES.has(migrated) ? migrated : DEFAULT_THEME;
  }

  function normalizeSharedAppearance(value) {
    const settings = plainObject(value) ? value : {};
    const requested = typeof settings.theme === "string" ? settings.theme : "";
    const savedThemes = Array.isArray(settings.savedThemes) ? settings.savedThemes : [];
    const custom = savedThemes.find((theme) =>
      plainObject(theme) && typeof theme.id === "string" && theme.id === requested);
    const theme = normalizedThemeBase(custom ? custom.base : requested);
    const builtInOverrides = plainObject(settings.themeOverrides)
      ? settings.themeOverrides[theme] : null;
    const sourceOverrides = custom && plainObject(custom.overrides)
      ? custom.overrides : builtInOverrides;
    const overrides = {};
    if (plainObject(sourceOverrides)) {
      for (const [name, rawValue] of Object.entries(sourceOverrides)) {
        if (!/^--[a-z0-9-]{1,80}$/i.test(name) || typeof rawValue !== "string") continue;
        const cssValue = rawValue.trim();
        if (!cssValue || cssValue.length > 1024) continue;
        overrides[name] = cssValue;
      }
    }
    const rawScale = Number(settings.uiScale);
    const uiScale = Number.isFinite(rawScale) && rawScale >= 0.7 && rawScale <= 2
      ? Math.round(rawScale * 100) / 100 : 1;
    return { theme, overrides, uiScale };
  }

  function applySharedAppearance(documentRef, value) {
    const body = documentRef && documentRef.body;
    const rootElement = documentRef && documentRef.documentElement;
    if (!body || !rootElement) return null;
    const appearance = normalizeSharedAppearance(value);
    body.dataset.theme = appearance.theme;

    const previous = appliedAppearanceProperties.get(body) || [];
    if (body.style && typeof body.style.removeProperty === "function") {
      for (const name of previous) body.style.removeProperty(name);
    }
    const applied = [];
    if (body.style && typeof body.style.setProperty === "function") {
      for (const [name, cssValue] of Object.entries(appearance.overrides)) {
        body.style.setProperty(name, cssValue);
        applied.push(name);
      }
    }
    appliedAppearanceProperties.set(body, applied);

    if (rootElement.style) {
      rootElement.style.zoom = String(appearance.uiScale);
      if (typeof rootElement.style.setProperty === "function") {
        rootElement.style.setProperty("--ui-scale", String(appearance.uiScale));
      }
    }
    return appearance;
  }

  function cachedSharedSettings(windowRef) {
    try {
      const parsed = JSON.parse(windowRef.localStorage.getItem(SHARED_SETTINGS_KEY) || "{}");
      return plainObject(parsed) ? parsed : {};
    } catch (error) {
      return {};
    }
  }

  function installSharedAppearance(options = {}) {
    const windowRef = options.windowRef || null;
    const documentRef = options.documentRef || windowRef && windowRef.document || null;
    if (!windowRef || !documentRef) return () => {};

    let disposed = false;
    let generation = 0;
    const apply = (settings) => {
      if (disposed) return null;
      const body = documentRef.body;
      if (body && body.classList && typeof body.classList.toggle === "function") {
        body.classList.toggle("desktop", !!(windowRef.whlDesktop && windowRef.whlDesktop.isDesktop));
      }
      return applySharedAppearance(documentRef, settings);
    };
    const start = () => {
      apply(cachedSharedSettings(windowRef));
      const requestGeneration = generation;
      const fetchImpl = options.fetchImpl ||
        (typeof windowRef.fetch === "function" ? windowRef.fetch.bind(windowRef) : null);
      if (!fetchImpl) return;
      Promise.resolve(fetchImpl("/api/client_state", {
        method: "GET",
        headers: { Accept: "application/json" },
      })).then((response) => {
        if (!response || response.ok === false || typeof response.json !== "function") return null;
        return response.json();
      }).then((payload) => {
        if (disposed || requestGeneration !== generation || !plainObject(payload) ||
            !plainObject(payload.settings)) return;
        apply(payload.settings);
      }).catch(() => {
        // The local cache remains a complete offline appearance fallback.
      });
    };
    const onStorage = (event) => {
      if (event && event.key !== SHARED_SETTINGS_KEY) return;
      generation += 1;
      let settings = cachedSharedSettings(windowRef);
      if (event && typeof event.newValue === "string") {
        try {
          const parsed = JSON.parse(event.newValue);
          if (plainObject(parsed)) settings = parsed;
        } catch (error) {
          // A partially written cache is ignored in favor of the last readable value.
        }
      }
      apply(settings);
    };

    if (documentRef.body) start();
    else if (typeof documentRef.addEventListener === "function") {
      documentRef.addEventListener("DOMContentLoaded", start, { once: true });
    }
    if (typeof windowRef.addEventListener === "function") {
      windowRef.addEventListener("storage", onStorage);
    }
    return () => {
      disposed = true;
      if (typeof windowRef.removeEventListener === "function") {
        windowRef.removeEventListener("storage", onStorage);
      }
    };
  }

  function validateProfileKey(value) {
    if (typeof value !== "string" || !PROFILE_KEY_RE.test(value) ||
        value.split("/").some((part) => !part || RESERVED_SEGMENTS.has(part))) {
      throw new TypeError("ui_profile_key is invalid");
    }
    return value;
  }

  function clone(value) {
    return JSON.parse(JSON.stringify(value));
  }

  class CorrectionsProfileStore {
    constructor(options = {}) {
      this.storage = options.storage || null;
      this.namespace = String(options.namespace || "librarytool.corrections-ui-profile");
      this.normalizeLayout = typeof options.normalizeLayout === "function"
        ? options.normalizeLayout : (value) => value && typeof value === "object" ? value : {};
      this.normalizeEditors = typeof options.normalizeEditors === "function"
        ? options.normalizeEditors : (value) => value && typeof value === "object" ? value : {};
      this.normalizeTools = typeof options.normalizeTools === "function"
        ? options.normalizeTools : (value) => value && typeof value === "object" ? value : {};
    }

    key(profileKey) {
      return `${this.namespace}:${encodeURIComponent(validateProfileKey(profileKey))}`;
    }

    toolNames() {
      const normalized = this.normalizeTools({});
      if (!normalized || typeof normalized !== "object" ||
          Array.isArray(normalized)) return [];
      return Object.keys(normalized).filter((name) => TOOL_NAME_RE.test(name));
    }

    toolKey(profileKey, toolName) {
      if (typeof toolName !== "string" || !TOOL_NAME_RE.test(toolName) ||
          !this.toolNames().includes(toolName)) {
        throw new TypeError("ui profile tool name is invalid");
      }
      return `${this.key(profileKey)}:tool:${encodeURIComponent(toolName)}`;
    }

    normalizeToolValue(toolName, value) {
      const normalized = this.normalizeTools({ [toolName]: value });
      if (!normalized || typeof normalized !== "object" ||
          Array.isArray(normalized) ||
          !Object.prototype.hasOwnProperty.call(normalized, toolName)) {
        throw new TypeError("ui profile tool name is unsupported");
      }
      return clone(normalized[toolName]);
    }

    loadToolOverrides(profileKey, tools) {
      const merged = { ...tools };
      let found = false;
      if (!this.storage || typeof this.storage.getItem !== "function") {
        return { found, tools: merged };
      }
      for (const toolName of this.toolNames()) {
        try {
          const raw = this.storage.getItem(this.toolKey(profileKey, toolName));
          if (!raw) continue;
          const parsed = JSON.parse(raw);
          if (!parsed || parsed.schema !== TOOL_PROFILE_SCHEMA ||
              parsed.profile_key !== validateProfileKey(profileKey) ||
              parsed.tool_name !== toolName) continue;
          merged[toolName] = this.normalizeToolValue(toolName, parsed.value);
          found = true;
        } catch (error) {
          // One malformed or unavailable tool preference must not invalidate
          // the rest of the presentation profile.
        }
      }
      return { found, tools: merged };
    }

    normalize(profileKey, value) {
      const key = validateProfileKey(profileKey);
      const source = value && typeof value === "object" && !Array.isArray(value) ? value : {};
      return {
        schema: PROFILE_SCHEMA,
        profile_key: key,
        layout: clone(this.normalizeLayout(source.layout)),
        editors: clone(this.normalizeEditors(source.editors)),
        tools: clone(this.normalizeTools(source.tools)),
      };
    }

    load(profileKey) {
      const fallback = this.normalize(profileKey, {});
      let found = false;
      let document = fallback;
      if (!this.storage || typeof this.storage.getItem !== "function") {
        return { found: false, ...fallback };
      }
      try {
        const raw = this.storage.getItem(this.key(profileKey));
        if (raw) {
          const parsed = JSON.parse(raw);
          if (parsed && parsed.schema === PROFILE_SCHEMA &&
              parsed.profile_key === validateProfileKey(profileKey)) {
            document = this.normalize(profileKey, parsed);
            found = true;
          }
        }
      } catch (error) {
        document = fallback;
      }
      const overrides = this.loadToolOverrides(profileKey, document.tools);
      return {
        found: found || overrides.found,
        ...document,
        tools: overrides.tools,
      };
    }

    save(profileKey, value, options = {}) {
      const document = this.normalize(profileKey, value);
      if (options.writeTools !== false) {
        for (const [toolName, toolValue] of Object.entries(document.tools)) {
          if (this.toolNames().includes(toolName)) {
            this.saveTool(profileKey, toolName, toolValue);
          }
        }
      }
      if (!this.storage || typeof this.storage.setItem !== "function") return document;
      try {
        this.storage.setItem(this.key(profileKey), JSON.stringify(document));
      } catch (error) {
        // Private browsing, quotas, and disabled storage leave the in-memory
        // controller authoritative for this window. Domain state is unaffected.
      }
      return document;
    }

    saveTool(profileKey, toolName, value) {
      const normalized = this.normalizeToolValue(toolName, value);
      if (!this.storage || typeof this.storage.setItem !== "function") {
        return normalized;
      }
      const document = {
        schema: TOOL_PROFILE_SCHEMA,
        profile_key: validateProfileKey(profileKey),
        tool_name: toolName,
        value: normalized,
      };
      try {
        this.storage.setItem(
          this.toolKey(profileKey, toolName),
          JSON.stringify(document),
        );
      } catch (error) {
        // The main profile document remains a compatible fallback when a
        // private or quota-limited storage implementation rejects this write.
      }
      return normalized;
    }

    clear(profileKey) {
      validateProfileKey(profileKey);
      if (!this.storage || typeof this.storage.removeItem !== "function") return false;
      let cleared = true;
      for (const key of [
        this.key(profileKey),
        ...this.toolNames().map((toolName) =>
          this.toolKey(profileKey, toolName)),
      ]) {
        try {
          this.storage.removeItem(key);
        } catch (error) {
          cleared = false;
        }
      }
      return cleared;
    }

    matchesStorageEvent(profileKey, event) {
      if (!event) return false;
      const keys = new Set([
        this.key(profileKey),
        ...this.toolNames().map((toolName) =>
          this.toolKey(profileKey, toolName)),
      ]);
      if (!keys.has(event.key)) return false;
      return !event.storageArea || !this.storage ||
        event.storageArea === this.storage;
    }
  }

  return {
    applySharedAppearance,
    CorrectionsProfileStore,
    installSharedAppearance,
    normalizeSharedAppearance,
    PROFILE_SCHEMA,
    TOOL_PROFILE_SCHEMA,
    SHARED_SETTINGS_KEY,
    validateProfileKey,
  };
});

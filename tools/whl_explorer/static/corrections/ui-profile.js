(function installCorrectionsProfile(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else Object.assign(root.LibraryToolCorrections ||= {}, api);
})(typeof globalThis !== "undefined" ? globalThis : this, function profileFactory() {
  "use strict";

  const PROFILE_SCHEMA = "librarytool.corrections-ui-profile/1";
  const TOOL_PROFILE_SCHEMA = "librarytool.corrections-ui-tool-profile/1";
  const PROFILE_KEY_RE = /^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$/;
  const TOOL_NAME_RE = /^[A-Za-z][A-Za-z0-9_-]{0,63}$/;
  const RESERVED_SEGMENTS = new Set([".", "..", "__proto__", "constructor", "prototype"]);

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
    CorrectionsProfileStore,
    PROFILE_SCHEMA,
    TOOL_PROFILE_SCHEMA,
    validateProfileKey,
  };
});

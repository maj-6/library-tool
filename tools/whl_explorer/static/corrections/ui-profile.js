(function installCorrectionsProfile(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else Object.assign(root.LibraryToolCorrections ||= {}, api);
})(typeof globalThis !== "undefined" ? globalThis : this, function profileFactory() {
  "use strict";

  const PROFILE_SCHEMA = "librarytool.corrections-ui-profile/1";
  const PROFILE_KEY_RE = /^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$/;
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
      if (!this.storage || typeof this.storage.getItem !== "function") {
        return { found: false, ...fallback };
      }
      try {
        const raw = this.storage.getItem(this.key(profileKey));
        if (!raw) return { found: false, ...fallback };
        const parsed = JSON.parse(raw);
        if (!parsed || parsed.schema !== PROFILE_SCHEMA ||
            parsed.profile_key !== validateProfileKey(profileKey)) {
          return { found: false, ...fallback };
        }
        return { found: true, ...this.normalize(profileKey, parsed) };
      } catch (error) {
        return { found: false, ...fallback };
      }
    }

    save(profileKey, value) {
      const document = this.normalize(profileKey, value);
      if (!this.storage || typeof this.storage.setItem !== "function") return document;
      try {
        this.storage.setItem(this.key(profileKey), JSON.stringify(document));
      } catch (error) {
        // Private browsing, quotas, and disabled storage leave the in-memory
        // controller authoritative for this window. Domain state is unaffected.
      }
      return document;
    }

    clear(profileKey) {
      validateProfileKey(profileKey);
      if (!this.storage || typeof this.storage.removeItem !== "function") return false;
      try {
        this.storage.removeItem(this.key(profileKey));
        return true;
      } catch (error) {
        return false;
      }
    }
  }

  return {
    CorrectionsProfileStore,
    PROFILE_SCHEMA,
    validateProfileKey,
  };
});

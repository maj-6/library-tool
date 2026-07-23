(function installCorrectionsEditorRegistry(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else Object.assign(root.LibraryToolCorrections ||= {}, api);
})(typeof globalThis !== "undefined" ? globalThis : this, function editorRegistryFactory() {
  "use strict";

  const EDITOR_ID_RE = /^[a-z][a-z0-9-]{0,63}$/;
  const RESOURCE_FAMILIES = new Set([
    "image", "text", "metadata", "regions", "missing", "unknown",
  ]);

  function resourceFamily(resource) {
    if (!resource || resource.missing === true) return "missing";
    const kind = String(resource.kind || resource.type || "").trim().toLowerCase();
    const mediaType = String(resource.media_type || resource.mediaType || "")
      .trim().toLowerCase();
    if (kind === "spatial-annotations" || kind === "spatial-annotation" ||
        kind === "regions" || kind === "region-list" ||
        Array.isArray(resource.annotations) || Array.isArray(resource.regions)) {
      return "regions";
    }
    if (mediaType.startsWith("image/") ||
        /(?:^|[-_])(image|raster|figure|illustration)(?:$|[-_])/.test(`-${kind}-`)) {
      return "image";
    }
    if (mediaType === "application/json" || mediaType.endsWith("+json") ||
        ["metadata", "generated-metadata", "structured-metadata"].includes(kind)) {
      return "metadata";
    }
    if (mediaType.startsWith("text/") ||
        ["ocr", "ocr-text", "text", "full-text", "transcription"].includes(kind)) {
      return "text";
    }
    return "unknown";
  }

  function resourceLabel(resource) {
    if (!resource) return "No resource selected";
    const value = resource.label || resource.name || resource.id || resource.artifact_id;
    return String(value || "Unnamed resource").slice(0, 240);
  }

  function clearNode(node) {
    if (typeof node.replaceChildren === "function") node.replaceChildren();
    else while (node.firstChild) node.removeChild(node.firstChild);
  }

  function element(documentRef, name, className, text) {
    const node = documentRef.createElement(name);
    if (className) node.className = className;
    if (text != null) node.textContent = String(text);
    return node;
  }

  function rasterUrl(resource) {
    const value = String(resource && (resource.url || resource.resource_url ||
      resource.content_url) || "").trim();
    if (!value || /[\u0000-\u001f]/.test(value) ||
        /^(?:javascript|file|filesystem):/i.test(value)) return "";
    if (/^data:/i.test(value) && !/^data:image\//i.test(value)) return "";
    return value;
  }

  function renderMessage(container, documentRef, title, message, className) {
    clearNode(container);
    const wrapper = element(documentRef, "section", className);
    wrapper.append(
      element(documentRef, "h2", "", title),
      element(documentRef, "p", "", message),
    );
    container.append(wrapper);
  }

  function imageRenderer(withOverlay) {
    return ({ container, documentRef, resource }) => {
      clearNode(container);
      const surface = element(documentRef, "div", "image-editor-surface");
      const url = rasterUrl(resource);
      if (!url) {
        const unavailable = element(documentRef, "div", "editor-unsupported");
        unavailable.append(
          element(documentRef, "h2", "", "Image unavailable"),
          element(documentRef, "p", "", "This artifact has no renderable image resource."),
        );
        surface.append(unavailable);
      } else {
        const image = element(documentRef, "img");
        image.src = url;
        image.alt = resourceLabel(resource);
        image.decoding = "async";
        surface.append(image);
        if (withOverlay) {
          const overlay = element(documentRef, "div", "image-overlay-layer");
          overlay.setAttribute("aria-hidden", "true");
          surface.append(overlay);
        }
      }
      container.append(surface);
    };
  }

  function textRenderer({ container, documentRef, resource }) {
    clearNode(container);
    const surface = element(documentRef, "article", "text-editor-surface");
    const pre = element(documentRef, "pre");
    pre.textContent = String(resource.text || resource.content || "");
    if (!pre.textContent) pre.textContent = "No OCR text is available for this artifact.";
    surface.append(pre);
    container.append(surface);
  }

  function metadataRenderer({ container, documentRef, resource }) {
    clearNode(container);
    const surface = element(documentRef, "article", "metadata-editor-surface");
    const pre = element(documentRef, "pre");
    const value = resource.metadata != null ? resource.metadata : resource.content;
    try {
      pre.textContent = JSON.stringify(value == null ? {} : value, null, 2);
    } catch (error) {
      pre.textContent = "Metadata could not be displayed safely.";
    }
    surface.append(pre);
    container.append(surface);
  }

  function regionRenderer({ container, documentRef, resource }) {
    clearNode(container);
    const surface = element(documentRef, "section", "region-editor-surface");
    const list = element(documentRef, "ol");
    const rows = Array.isArray(resource.annotations) ? resource.annotations
      : Array.isArray(resource.regions) ? resource.regions : [];
    if (!rows.length) {
      list.append(element(documentRef, "li", "", "No regions are available."));
    } else {
      for (const row of rows) {
        const role = String(row && (row.role || row.kind) || "unlabeled");
        const label = String(row && (row.label || row.caption || row.text || row.id) || "region");
        list.append(element(documentRef, "li", "", `${role}: ${label}`));
      }
    }
    surface.append(list);
    container.append(surface);
  }

  class EditorRegistry {
    constructor(options = {}) {
      this.documentRef = options.documentRef || null;
      this.onSelectionChange = typeof options.onSelectionChange === "function"
        ? options.onSelectionChange : () => {};
      this.editors = new Map();
      this.choices = Object.create(null);
      this.resource = null;
      this.family = "missing";
      this.selectedEditorId = null;
      this.fallbackIds = { missing: null, unknown: null };
    }

    register(definition) {
      if (!definition || !EDITOR_ID_RE.test(String(definition.id || "")) ||
          typeof definition.label !== "string" || !definition.label.trim() ||
          typeof definition.render !== "function") {
        throw new TypeError("editor definition is invalid");
      }
      if (this.editors.has(definition.id)) throw new TypeError("editor id is already registered");
      const families = Array.isArray(definition.families)
        ? definition.families.slice() : [];
      if (!families.length || families.some((family) => !RESOURCE_FAMILIES.has(family))) {
        throw new TypeError("editor families are invalid");
      }
      const checked = Object.freeze({
        id: definition.id,
        label: definition.label.trim().slice(0, 80),
        families: Object.freeze(families),
        render: definition.render,
        accepts: typeof definition.accepts === "function" ? definition.accepts : null,
        fallback: definition.fallback === true,
      });
      this.editors.set(checked.id, checked);
      if (checked.fallback) {
        for (const family of checked.families) this.fallbackIds[family] = checked.id;
      }
      return this;
    }

    compatibleEditors(resource = this.resource) {
      const family = resourceFamily(resource);
      return Array.from(this.editors.values()).filter((editor) =>
        !editor.fallback && editor.families.includes(family) &&
        (!editor.accepts || editor.accepts(resource)));
    }

    setResource(resource) {
      this.resource = resource || null;
      this.family = resourceFamily(this.resource);
      const compatible = this.compatibleEditors();
      const preferred = this.choices[this.family];
      const selected = compatible.find((editor) => editor.id === preferred) || compatible[0];
      this.selectedEditorId = selected ? selected.id : this.fallbackIds[this.family] ||
        this.fallbackIds.unknown;
      return this.currentEditor();
    }

    selectEditor(editorId) {
      const selected = this.compatibleEditors().find((editor) => editor.id === editorId);
      if (!selected) return false;
      this.selectedEditorId = selected.id;
      this.choices[this.family] = selected.id;
      this.onSelectionChange({ family: this.family, editorId: selected.id });
      return true;
    }

    currentEditor() {
      return this.editors.get(this.selectedEditorId) || null;
    }

    validateChoices(value) {
      const restored = Object.create(null);
      if (value && typeof value === "object" && !Array.isArray(value)) {
        for (const family of RESOURCE_FAMILIES) {
          const editorId = value[family];
          const editor = this.editors.get(editorId);
          if (editor && !editor.fallback && editor.families.includes(family)) {
            restored[family] = editor.id;
          }
        }
      }
      return Object.fromEntries(Object.entries(restored));
    }

    restoreChoices(value) {
      const restored = this.validateChoices(value);
      this.choices = restored;
      if (this.resource !== null) this.setResource(this.resource);
      return this.serializeChoices();
    }

    resetChoices() {
      this.choices = Object.create(null);
      this.setResource(this.resource);
    }

    serializeChoices() {
      return Object.fromEntries(Object.entries(this.choices));
    }

    render(container) {
      if (!container) throw new TypeError("editor container is required");
      const editor = this.currentEditor();
      const documentRef = this.documentRef || container.ownerDocument;
      if (!editor || !documentRef) throw new TypeError("editor document is required");
      editor.render({
        container,
        documentRef,
        resource: this.resource,
        family: this.family,
      });
      return editor.id;
    }
  }

  function createDefaultEditorRegistry(options = {}) {
    const registry = new EditorRegistry(options);
    registry
      .register({
        id: "image-overlay",
        label: "Image + overlay",
        families: ["image"],
        render: imageRenderer(true),
      })
      .register({
        id: "image-plain",
        label: "Image only",
        families: ["image"],
        render: imageRenderer(false),
      })
      .register({
        id: "ocr-text",
        label: "OCR text",
        families: ["text"],
        render: textRenderer,
      })
      .register({
        id: "structured-metadata",
        label: "Structured metadata",
        families: ["metadata"],
        render: metadataRenderer,
      })
      .register({
        id: "region-list",
        label: "Region list",
        families: ["regions"],
        render: regionRenderer,
      })
      .register({
        id: "empty-resource",
        label: "No resource",
        families: ["missing"],
        fallback: true,
        render: ({ container, documentRef }) => renderMessage(
          container, documentRef, "Choose a resource",
          "Select a book and artifact to begin reviewing corrections.", "editor-empty"),
      })
      .register({
        id: "unsupported-resource",
        label: "Unsupported resource",
        families: ["unknown"],
        fallback: true,
        render: ({ container, documentRef, resource }) => renderMessage(
          container, documentRef, "No compatible editor",
          `This ${resourceLabel(resource)} resource type is not supported by this client.`,
          "editor-unsupported"),
      });
    registry.setResource(null);
    return registry;
  }

  return {
    EditorRegistry,
    createDefaultEditorRegistry,
    resourceFamily,
    resourceLabel,
  };
});

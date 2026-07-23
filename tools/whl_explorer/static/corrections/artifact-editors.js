(function installCorrectionsArtifactEditors(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else Object.assign(root.LibraryToolCorrections ||= {}, api);
})(typeof globalThis !== "undefined" ? globalThis : this, function artifactEditorsFactory() {
  "use strict";

  const EDITOR_IDS = Object.freeze({
    pagedText: "paged-ocr",
    pagedRegions: "paged-regions",
    regionOverlay: "region-overlay",
    generic: "generic-artifact",
  });

  function clearNode(node) {
    if (typeof node.replaceChildren === "function") node.replaceChildren();
    else while (node.firstChild) node.removeChild(node.firstChild);
  }

  function element(documentRef, name, className, value) {
    const node = documentRef.createElement(name);
    if (className) node.className = className;
    if (value != null) node.textContent = String(value);
    return node;
  }

  function boundedDisplayValue(value, depth = 0) {
    if (depth > 4) return "[nested value]";
    if (value == null || typeof value === "boolean" || typeof value === "number") {
      return value;
    }
    if (typeof value === "string") return value.slice(0, 4096);
    if (Array.isArray(value)) {
      return value.slice(0, 128).map((entry) => boundedDisplayValue(entry, depth + 1));
    }
    if (!value || typeof value !== "object") return String(value).slice(0, 4096);
    const result = {};
    for (const key of Object.keys(value).sort().slice(0, 128)) {
      if (["__proto__", "constructor", "prototype"].includes(key)) continue;
      result[key] = boundedDisplayValue(value[key], depth + 1);
    }
    return result;
  }

  function setBusy(button, busy, label) {
    button.disabled = !!busy;
    button.setAttribute("aria-busy", String(!!busy));
    if (label) button.textContent = label;
  }

  function pageButton(documentRef, resource, kind) {
    const button = element(documentRef, "button", "artifact-load-more", "Load more");
    button.type = "button";
    button.addEventListener("click", async () => {
      if (typeof resource.loadNext !== "function" || button.disabled) return;
      setBusy(button, true, "Loading…");
      try {
        await resource.loadNext();
      } catch (error) {
        button.textContent = "Try loading again";
        button.setAttribute("aria-label", `Retry loading more ${kind}`);
      } finally {
        if (button.textContent === "Loading…") setBusy(button, false, "Load more");
        else setBusy(button, false);
      }
    });
    return button;
  }

  function pagedTextRenderer({ container, documentRef, resource }) {
    clearNode(container);
    const surface = element(documentRef, "article", "text-editor-surface paged-text-surface");
    surface.setAttribute("aria-label", "OCR text");
    const pre = element(documentRef, "pre", "", resource && resource.text ||
      "No OCR text is available for this artifact.");
    surface.append(pre);
    if (resource && resource.nextCursor) {
      surface.append(pageButton(documentRef, resource, "OCR text"));
    }
    if (resource && resource.truncated) {
      const note = element(documentRef, "p", "artifact-page-note",
        "Only a bounded portion is loaded. Use Load more to continue.");
      surface.append(note);
    }
    container.append(surface);
  }

  function regionLabel(row) {
    if (!row || typeof row !== "object") return "Unlabeled region";
    const role = String(row.effectiveRole || row.effective_role || row.role || "unlabeled");
    const label = String(row.label || row.caption || row.text || row.id || "region");
    return `${role}: ${label}`.slice(0, 1024);
  }

  function pagedRegionRenderer({ container, documentRef, resource }) {
    clearNode(container);
    const surface = element(documentRef, "section",
      "region-editor-surface paged-region-surface");
    surface.setAttribute("aria-label", "Artifact regions");
    const list = element(documentRef, "ol");
    const rows = Array.isArray(resource && resource.regions)
      ? resource.regions.slice(0, 512) : [];
    if (!rows.length) {
      list.append(element(documentRef, "li", "artifact-empty-row",
        "No regions are available."));
    } else {
      for (const row of rows) {
        const item = element(documentRef, "li", "artifact-region-row", regionLabel(row));
        const id = row && (row.key || row.id || row.annotation_id);
        if (id) item.dataset.artifactKey = String(id);
        if (row && row.highlighted) item.dataset.linked = "true";
        list.append(item);
      }
    }
    surface.append(list);
    if (resource && resource.nextCursor) {
      surface.append(pageButton(documentRef, resource, "regions"));
    }
    container.append(surface);
  }

  function percent(value) {
    const number = Number(value);
    if (!Number.isFinite(number)) return null;
    return `${Math.max(0, Math.min(1, number)) * 100}%`;
  }

  function regionOverlayRenderer({ container, documentRef, resource }) {
    clearNode(container);
    const surface = element(documentRef, "section", "artifact-region-overlay");
    surface.setAttribute("aria-label", "Region overlay");
    const canvas = element(documentRef, "div", "artifact-region-overlay-canvas");
    const regions = Array.isArray(resource && resource.regions)
      ? resource.regions.slice(0, 512) : [];
    for (const row of regions) {
      const selector = row && (row.selector || row.polygon);
      const points = selector && Array.isArray(selector.points)
        ? selector.points.slice(0, 64) : [];
      const polygon = points.map((point) => {
        const x = percent(point && point.x);
        const y = percent(point && point.y);
        return x && y ? `${x} ${y}` : "";
      }).filter(Boolean);
      if (polygon.length < 3) continue;
      const marker = element(documentRef, "div", "artifact-region-polygon");
      marker.style.clipPath = `polygon(${polygon.join(",")})`;
      marker.title = regionLabel(row);
      marker.setAttribute("aria-hidden", "true");
      if (row.highlighted) marker.dataset.linked = "true";
      canvas.append(marker);
    }
    if (!regions.length) {
      canvas.append(element(documentRef, "p", "artifact-empty-row",
        "No region geometry is available."));
    }
    surface.append(canvas);
    const hint = element(documentRef, "p", "artifact-page-note",
      "Use the Region list editor for keyboard navigation.");
    surface.append(hint);
    container.append(surface);
  }

  function genericRenderer({ container, documentRef, resource }) {
    clearNode(container);
    const surface = element(documentRef, "article",
      "metadata-editor-surface generic-artifact-surface");
    surface.setAttribute("aria-label", "Generic artifact inspector");
    const title = element(documentRef, "h2", "", resource && resource.label ||
      "Unknown artifact");
    const pre = element(documentRef, "pre");
    try {
      const value = resource && (resource.detail || resource.metadata || resource.content ||
        resource.summary || resource);
      pre.textContent = JSON.stringify(boundedDisplayValue(value || {}), null, 2);
    } catch (error) {
      pre.textContent = "This artifact could not be displayed safely.";
    }
    surface.append(title, pre);
    container.append(surface);
  }

  function artifactEditorHint(resource) {
    if (!resource || typeof resource !== "object") return "";
    if (resource.editorHint && Object.values(EDITOR_IDS).includes(resource.editorHint)) {
      return resource.editorHint;
    }
    if (resource.paged === true && resource.family === "text") return EDITOR_IDS.pagedText;
    if (resource.paged === true && resource.family === "regions") {
      return EDITOR_IDS.pagedRegions;
    }
    if (resource.family === "unknown") return EDITOR_IDS.generic;
    return "";
  }

  function registerArtifactEditors(registry) {
    if (!registry || typeof registry.register !== "function") {
      throw new TypeError("an editor registry is required");
    }
    const has = (id) => registry.editors instanceof Map && registry.editors.has(id);
    const definitions = [
      {
        id: EDITOR_IDS.pagedText,
        label: "Paged OCR text",
        families: ["text"],
        accepts: (resource) => !!resource && resource.paged === true,
        render: pagedTextRenderer,
      },
      {
        id: EDITOR_IDS.regionOverlay,
        label: "Region overlay",
        families: ["regions"],
        accepts: (resource) => !!resource && Array.isArray(resource.regions),
        render: regionOverlayRenderer,
      },
      {
        id: EDITOR_IDS.pagedRegions,
        label: "Region object list",
        families: ["regions"],
        accepts: (resource) => !!resource && resource.paged === true,
        render: pagedRegionRenderer,
      },
      {
        id: EDITOR_IDS.generic,
        label: "Generic inspector",
        families: ["unknown"],
        render: genericRenderer,
      },
    ];
    for (const definition of definitions) {
      if (!has(definition.id)) registry.register(definition);
    }
    return registry;
  }

  return {
    ARTIFACT_EDITOR_IDS: EDITOR_IDS,
    artifactEditorHint,
    registerArtifactEditors,
  };
});

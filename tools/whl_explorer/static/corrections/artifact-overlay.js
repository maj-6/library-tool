(function installCorrectionsArtifactOverlay(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else Object.assign(root.LibraryToolCorrections ||= {}, api);
})(typeof globalThis !== "undefined" ? globalThis : this,
  function correctionsArtifactOverlayFactory() {
    "use strict";

    const ROLE_CODES = Object.freeze({
      marginalia: "MAR",
      manuscript: "MAR",
      handwritten: "MAR",
      figure: "ILL",
      illustration: "ILL",
      image: "ILL",
    });
    const CATEGORY_CODES = Object.freeze({
      title_page: "T",
      cover: "C",
      spine: "S",
      content_specimen: "E",
    });

    function text(value, maximum = 512) {
      return value == null ? "" : String(value)
        .replace(/[\u0000-\u001f\u007f]/g, "")
        .trim()
        .slice(0, maximum);
    }

    function finite(value, fallback = 0) {
      const number = Number(value);
      return Number.isFinite(number) ? number : fallback;
    }

    function positive(value, fallback = 1) {
      const number = Number(value);
      return Number.isFinite(number) && number > 0 ? number : fallback;
    }

    function clamp(value, minimum = 0, maximum = 1) {
      return Math.max(minimum, Math.min(maximum, finite(value)));
    }

    function nodeInside(root, node) {
      if (!root || !node) return false;
      if (root === node) return true;
      if (typeof root.contains === "function") return root.contains(node);
      let cursor = node;
      while (cursor) {
        if (cursor === root) return true;
        cursor = cursor.parentNode;
      }
      return false;
    }

    function exifOrientation(value) {
      const number = Number(value);
      return Number.isSafeInteger(number) && number >= 1 && number <= 8 ? number : 1;
    }

    function orientNormalizedPoint(point, orientation = 1) {
      const x = finite(Array.isArray(point) ? point[0] : point && point.x);
      const y = finite(Array.isArray(point) ? point[1] : point && point.y);
      switch (exifOrientation(orientation)) {
        case 2: return Object.freeze({ x: 1 - x, y });
        case 3: return Object.freeze({ x: 1 - x, y: 1 - y });
        case 4: return Object.freeze({ x, y: 1 - y });
        case 5: return Object.freeze({ x: y, y: x });
        case 6: return Object.freeze({ x: 1 - y, y: x });
        case 7: return Object.freeze({ x: 1 - y, y: 1 - x });
        case 8: return Object.freeze({ x: y, y: 1 - x });
        default: return Object.freeze({ x, y });
      }
    }

    function orientedDimensions(width, height, orientation = 1) {
      const normalized = exifOrientation(orientation);
      return normalized >= 5
        ? Object.freeze({ width: height, height: width })
        : Object.freeze({ width, height });
    }

    function normalizePoint(point, options = {}) {
      let x = finite(Array.isArray(point) ? point[0] : point && point.x, NaN);
      let y = finite(Array.isArray(point) ? point[1] : point && point.y, NaN);
      if (!Number.isFinite(x) || !Number.isFinite(y)) return null;
      const coordinateSpace = text(options.coordinateSpace, 64).toLowerCase();
      if (coordinateSpace.includes("pixel") ||
          Math.abs(x) > 1.000001 || Math.abs(y) > 1.000001) {
        x /= positive(options.sourceWidth);
        y /= positive(options.sourceHeight);
      }
      return Object.freeze({
        x: options.clamp === false ? x : clamp(x),
        y: options.clamp === false ? y : clamp(y),
      });
    }

    function selectorPoints(value, options = {}) {
      const selector = value && (value.selector || value.polygon) || value || {};
      const raw = Array.isArray(selector)
        ? selector : Array.isArray(selector.points) ? selector.points : [];
      const coordinateSpace = selector.coordinate_space ||
        selector.coordinateSpace || options.coordinateSpace;
      return Object.freeze(raw.slice(0, 128)
        .map((point) => normalizePoint(point, { ...options, coordinateSpace }))
        .filter(Boolean));
    }

    function createOverlayTransform(options = {}) {
      const sourceWidth = positive(options.sourceWidth || options.width);
      const sourceHeight = positive(options.sourceHeight || options.height);
      const orientation = exifOrientation(options.orientation);
      const oriented = orientedDimensions(sourceWidth, sourceHeight, orientation);
      const viewportWidth = positive(options.viewportWidth, oriented.width);
      const viewportHeight = positive(options.viewportHeight, oriented.height);
      const zoom = Math.max(0.01, Math.min(128, positive(options.zoom)));
      let width;
      let height;
      let left;
      let top;
      const rect = options.renderedRect;
      if (rect && Number.isFinite(Number(rect.width)) &&
          Number.isFinite(Number(rect.height))) {
        width = positive(rect.width) * zoom;
        height = positive(rect.height) * zoom;
        left = finite(rect.left) - (width - positive(rect.width)) / 2;
        top = finite(rect.top) - (height - positive(rect.height)) / 2;
      } else {
        const fit = text(options.fit || "contain", 16).toLowerCase();
        const fitScale = fit === "cover"
          ? Math.max(viewportWidth / oriented.width, viewportHeight / oriented.height)
          : fit === "none" ? 1
            : Math.min(viewportWidth / oriented.width, viewportHeight / oriented.height);
        width = oriented.width * fitScale * zoom;
        height = oriented.height * fitScale * zoom;
        left = (viewportWidth - width) / 2;
        top = (viewportHeight - height) / 2;
      }
      left += finite(options.panX);
      top += finite(options.panY);
      const project = (point) => {
        const orientedPoint = orientNormalizedPoint(point, orientation);
        return Object.freeze({
          x: left + orientedPoint.x * width,
          y: top + orientedPoint.y * height,
        });
      };
      const unproject = (point) => {
        const orientedPoint = {
          x: (finite(point && point.x) - left) / width,
          y: (finite(point && point.y) - top) / height,
        };
        const inverse = {
          1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 8, 7: 7, 8: 6,
        }[orientation];
        return orientNormalizedPoint(orientedPoint, inverse);
      };
      return Object.freeze({
        sourceWidth,
        sourceHeight,
        orientation,
        viewportWidth,
        viewportHeight,
        zoom,
        panX: finite(options.panX),
        panY: finite(options.panY),
        left,
        top,
        width,
        height,
        project,
        unproject,
      });
    }

    function polygonBounds(points) {
      if (!Array.isArray(points) || !points.length) return null;
      const xs = points.map((point) => finite(point.x));
      const ys = points.map((point) => finite(point.y));
      const left = Math.min(...xs);
      const top = Math.min(...ys);
      const right = Math.max(...xs);
      const bottom = Math.max(...ys);
      return Object.freeze({
        left,
        top,
        right,
        bottom,
        width: right - left,
        height: bottom - top,
      });
    }

    function polygonCentroid(points) {
      if (!Array.isArray(points) || !points.length) {
        return Object.freeze({ x: 0, y: 0 });
      }
      let signedArea = 0;
      let x = 0;
      let y = 0;
      for (let index = 0; index < points.length; index += 1) {
        const current = points[index];
        const next = points[(index + 1) % points.length];
        const cross = current.x * next.y - next.x * current.y;
        signedArea += cross;
        x += (current.x + next.x) * cross;
        y += (current.y + next.y) * cross;
      }
      if (Math.abs(signedArea) < 1e-9) {
        return Object.freeze({
          x: points.reduce((total, point) => total + point.x, 0) / points.length,
          y: points.reduce((total, point) => total + point.y, 0) / points.length,
        });
      }
      return Object.freeze({
        x: x / (3 * signedArea),
        y: y / (3 * signedArea),
      });
    }

    function projectPolygon(value, transformOrOptions = {}, pointOptions = {}) {
      const transform = typeof transformOrOptions.project === "function"
        ? transformOrOptions : createOverlayTransform(transformOrOptions);
      const points = selectorPoints(value, {
        sourceWidth: transform.sourceWidth,
        sourceHeight: transform.sourceHeight,
        ...pointOptions,
      }).map(transform.project);
      return Object.freeze(points);
    }

    function localClipPath(points, bounds = polygonBounds(points)) {
      if (!bounds || points.length < 3) return "";
      const width = Math.max(bounds.width, 1e-9);
      const height = Math.max(bounds.height, 1e-9);
      const values = points.map((point) => {
        const x = clamp((point.x - bounds.left) / width) * 100;
        const y = clamp((point.y - bounds.top) / height) * 100;
        return `${x.toFixed(4)}% ${y.toFixed(4)}%`;
      });
      return `polygon(${values.join(", ")})`;
    }

    function assertion(value, origin) {
      return Array.isArray(value)
        ? value.find((entry) => entry && entry.origin === origin) || null
        : null;
    }

    function artifactPresentationMetadata(value) {
      const provenance = value && value.provenance || {};
      const source = value && value.source || {};
      const roles = value && (value.roleAssignments || value.role_assignments) || [];
      const captions = value && (value.captionAssertions || value.caption_assertions) || [];
      const manualRole = assertion(roles, "manual");
      const machineRole = assertion(roles, "machine");
      const manualCaption = assertion(captions, "manual");
      const machineCaption = assertion(captions, "machine");
      const confidence = machineRole && machineRole.confidence != null
        ? machineRole.confidence
        : machineCaption && machineCaption.confidence != null
          ? machineCaption.confidence : null;
      return Object.freeze({
        provider: text(provenance.provider_id || provenance.providerId, 128),
        model: text(provenance.model, 256),
        confidence: Number.isFinite(Number(confidence)) ? Number(confidence) : null,
        sourceRevision: text(
          source.representationRevision || source.representation_revision ||
            source.canvasRevision || source.canvas_revision,
          512,
        ),
        machineRole: text(machineRole && machineRole.role, 64),
        machineCaption: text(machineCaption && machineCaption.text, 4096),
        humanRole: text(manualRole && manualRole.role, 64),
        humanCaption: text(manualCaption && manualCaption.text, 4096),
        freshness: text(value && value.freshness, 32),
      });
    }

    function effectiveRole(value) {
      const explicit = text(value && (value.effectiveRole || value.effective_role ||
        value.role), 64).toLowerCase();
      if (explicit) return explicit;
      const roles = value && (value.roleAssignments || value.role_assignments);
      for (const origin of ["manual", "imported", "machine"]) {
        const found = assertion(roles, origin);
        if (found && found.role) return text(found.role, 64).toLowerCase();
      }
      return "";
    }

    function effectiveCategory(value) {
      return text(value && (value.effectiveCategory || value.effective_category ||
        value.category), 64).toLowerCase();
    }

    function artifactCode(value) {
      const role = effectiveRole(value);
      const category = effectiveCategory(value);
      const supplied = text(value && (value.code || value.shortCode), 8).toUpperCase();
      if (ROLE_CODES[role]) return ROLE_CODES[role];
      if (CATEGORY_CODES[category]) return CATEGORY_CODES[category];
      if (/^[A-Z0-9]{1,4}$/.test(supplied)) return supplied;
      return role ? role.slice(0, 3).toUpperCase()
        : category ? category.slice(0, 3).toUpperCase() : "REG";
    }

    function overlayKey(value, index = 0) {
      const explicit = text(value && value.key, 520);
      if (explicit) return explicit;
      const annotationId = text(value &&
        (value.annotationId || value.annotation_id), 256);
      if (annotationId) return `annotation:${annotationId}`;
      const id = text(value && value.id, 256);
      const objectType = text(value &&
        (value.objectType || value.object_type || value.type), 64).toLowerCase();
      const kind = text(value && value.kind, 64).toLowerCase();
      const annotationLike = objectType.includes("annotation") ||
        objectType === "region" ||
        ["annotation", "region", "mistral-box", "spatial-annotation"]
          .includes(kind);
      if (id && annotationLike) return `annotation:${id}`;
      return id || `overlay:${index}`;
    }

    function normalizeOverlayRegion(value, index = 0, options = {}) {
      const points = selectorPoints(value, options);
      if (points.length < 3) return null;
      const code = artifactCode(value);
      const label = text(value && (value.label || value.name ||
        value.caption || value.text || value.id), 512) || `Region ${index + 1}`;
      return Object.freeze({
        key: overlayKey(value, index),
        label,
        code,
        role: effectiveRole(value),
        category: effectiveCategory(value),
        points,
        metadata: artifactPresentationMetadata(value),
        target: value,
      });
    }

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

    function px(value) {
      return `${finite(value).toFixed(3)}px`;
    }

    class ArtifactOverlay {
      constructor(options = {}) {
        if (!options.root || typeof options.root.replaceChildren !== "function") {
          throw new TypeError("an artifact overlay root is required");
        }
        this.root = options.root;
        this.documentRef = options.documentRef || this.root.ownerDocument;
        this.getViewport = typeof options.getViewport === "function"
          ? options.getViewport : () => ({
            width: Number(this.root.clientWidth) || 1,
            height: Number(this.root.clientHeight) || 1,
          });
        this.onSoftTarget = typeof options.onSoftTarget === "function"
          ? options.onSoftTarget : () => {};
        this.onFocusTarget = typeof options.onFocusTarget === "function"
          ? options.onFocusTarget : () => {};
        this.onActivate = typeof options.onActivate === "function"
          ? options.onActivate : () => {};
        this.ResizeObserver = options.ResizeObserver ||
          typeof ResizeObserver === "function" && ResizeObserver || null;
        this.rawRegions = Object.freeze([]);
        this.regionOptions = Object.freeze({});
        this.regions = Object.freeze([]);
        this.view = {
          sourceWidth: 1,
          sourceHeight: 1,
          orientation: 1,
          zoom: 1,
          panX: 0,
          panY: 0,
          fit: "contain",
        };
        this.hotKey = "";
        this.focusedKey = "";
        this.layer = null;
        this.observer = null;
        this.rendering = false;
        this.destroyed = false;
      }

      mount() {
        if (this.destroyed) return this;
        if (!this.layer) {
          this.layer = element(
            this.documentRef,
            "div",
            "corrections-artifact-overlay-layer",
          );
          this.layer.setAttribute("aria-label", "Artifact region overlays");
          this.root.append(this.layer);
        }
        if (this.ResizeObserver && !this.observer) {
          this.observer = new this.ResizeObserver(() => this.render());
          this.observer.observe(this.root);
        }
        this.render();
        return this;
      }

      setRegions(values, options = {}) {
        const rows = Array.isArray(values) ? values : [];
        if (options.sourceWidth || options.sourceHeight) {
          this.view = {
            ...this.view,
            sourceWidth: positive(options.sourceWidth || this.view.sourceWidth),
            sourceHeight: positive(options.sourceHeight || this.view.sourceHeight),
          };
        }
        this.rawRegions = Object.freeze(rows.slice(0, 512));
        this.regionOptions = Object.freeze({
          coordinateSpace: options.coordinateSpace,
        });
        this.normalizeRegions();
        this.render();
        return this.regions;
      }

      normalizeRegions() {
        this.regions = Object.freeze(this.rawRegions.map((value, index) =>
          normalizeOverlayRegion(value, index, {
            sourceWidth: this.view.sourceWidth,
            sourceHeight: this.view.sourceHeight,
            coordinateSpace: this.regionOptions.coordinateSpace,
          })).filter(Boolean));
        return this.regions;
      }

      setView(value = {}) {
        this.view = {
          ...this.view,
          ...value,
          sourceWidth: positive(value.sourceWidth || value.width ||
            this.view.sourceWidth),
          sourceHeight: positive(value.sourceHeight || value.height ||
            this.view.sourceHeight),
          orientation: exifOrientation(
            value.orientation == null ? this.view.orientation : value.orientation),
          zoom: Math.max(0.01, positive(
            value.zoom == null ? this.view.zoom : value.zoom)),
          panX: finite(value.panX == null ? this.view.panX : value.panX),
          panY: finite(value.panY == null ? this.view.panY : value.panY),
        };
        if (this.rawRegions.length) this.normalizeRegions();
        this.render();
        return Object.freeze({ ...this.view });
      }

      setHotTarget(key, options = {}) {
        this.hotKey = text(key, 520);
        this.updateTargetStates();
        if (options.emit !== false) {
          const region = this.regions.find((candidate) => candidate.key === this.hotKey);
          this.onSoftTarget(region ? region.target : null, {
            key: this.hotKey,
            element: this.marker(this.hotKey),
          });
        }
      }

      setFocusedTarget(key, options = {}) {
        this.focusedKey = text(key, 520);
        this.updateTargetStates();
        if (options.emit !== false) {
          const region = this.regions
            .find((candidate) => candidate.key === this.focusedKey);
          this.onFocusTarget(region ? region.target : null, {
            key: this.focusedKey,
            element: this.marker(this.focusedKey),
          });
        }
      }

      marker(key) {
        if (!this.layer || typeof this.layer.querySelectorAll !== "function") return null;
        return Array.from(this.layer.querySelectorAll("[data-overlay-key]"))
          .find((node) => node.dataset && node.dataset.overlayKey === key) || null;
      }

      updateTargetStates() {
        if (!this.layer || typeof this.layer.querySelectorAll !== "function") return;
        for (const node of this.layer.querySelectorAll("[data-overlay-key]")) {
          const key = node.dataset && node.dataset.overlayKey;
          if (key === this.hotKey) node.dataset.hot = "true";
          else delete node.dataset.hot;
          if (key === this.focusedKey) {
            node.dataset.focused = "true";
            node.setAttribute("aria-current", "true");
          } else {
            delete node.dataset.focused;
            node.removeAttribute("aria-current");
          }
        }
      }

      markerElement(region, transform) {
        const points = region.points.map(transform.project);
        const bounds = polygonBounds(points);
        if (!bounds) return null;
        const wrapper = element(
          this.documentRef,
          "div",
          "corrections-artifact-overlay-region",
        );
        wrapper.dataset.overlayKey = region.key;
        wrapper.style.left = px(bounds.left);
        wrapper.style.top = px(bounds.top);
        wrapper.style.width = px(Math.max(1, bounds.width));
        wrapper.style.height = px(Math.max(1, bounds.height));

        const marker = element(
          this.documentRef,
          "button",
          "corrections-artifact-overlay-shape",
        );
        marker.type = "button";
        marker.style.clipPath = localClipPath(points, bounds);
        marker.setAttribute("aria-label",
          `${region.code}, ${region.label}, artifact region`);
        marker.title = `${region.code} — ${region.label}`;
        marker.addEventListener("pointerenter", () =>
          this.setHotTarget(region.key));
        marker.addEventListener("pointerleave", () => {
          if (this.hotKey === region.key) this.setHotTarget("");
        });
        marker.addEventListener("focus", () =>
          this.setFocusedTarget(region.key));
        marker.addEventListener("blur", () => {
          if (!this.rendering && this.focusedKey === region.key) {
            this.setFocusedTarget("");
          }
        });
        marker.addEventListener("click", () =>
          this.onActivate(region.target, { key: region.key, element: wrapper }));

        const centroid = polygonCentroid(points);
        const badge = element(
          this.documentRef,
          "span",
          "corrections-artifact-overlay-code",
          region.code,
        );
        badge.setAttribute("aria-hidden", "true");
        badge.style.left = px(centroid.x - bounds.left);
        badge.style.top = px(centroid.y - bounds.top);
        wrapper.append(marker, badge);
        return wrapper;
      }

      reconcileTargets() {
        const keys = new Set(this.regions.map((region) => region.key));
        if (this.hotKey && !keys.has(this.hotKey)) this.setHotTarget("");
        if (this.focusedKey && !keys.has(this.focusedKey)) {
          this.setFocusedTarget("");
        }
      }

      render() {
        if (!this.layer || this.destroyed) return;
        this.reconcileTargets();
        const focusKey = this.focusedKey;
        const active = this.documentRef && this.documentRef.activeElement;
        const restoreFocus = Boolean(
          focusKey &&
          this.regions.some((region) => region.key === focusKey) &&
          nodeInside(this.marker(focusKey), active),
        );
        const viewport = this.getViewport() || {};
        const transform = createOverlayTransform({
          ...this.view,
          viewportWidth: positive(viewport.width || viewport.clientWidth),
          viewportHeight: positive(viewport.height || viewport.clientHeight),
        });
        this.rendering = true;
        try {
          clearNode(this.layer);
          for (const region of this.regions) {
            const marker = this.markerElement(region, transform);
            if (marker) this.layer.append(marker);
          }
        } finally {
          this.rendering = false;
        }
        this.updateTargetStates();
        if (restoreFocus) {
          const wrapper = this.marker(focusKey);
          const marker = wrapper &&
            typeof wrapper.querySelector === "function" &&
            wrapper.querySelector(".corrections-artifact-overlay-shape");
          if (marker && typeof marker.focus === "function") {
            try {
              marker.focus({ preventScroll: true });
            } catch (error) {
              marker.focus();
            }
          }
        }
      }

      destroy() {
        if (this.destroyed) return;
        this.destroyed = true;
        if (this.observer) this.observer.disconnect();
        this.observer = null;
        if (this.layer && this.layer.parentNode) {
          this.layer.parentNode.removeChild(this.layer);
        }
        this.layer = null;
        this.rawRegions = Object.freeze([]);
        this.regions = Object.freeze([]);
      }
    }

    function createArtifactOverlay(options) {
      return new ArtifactOverlay(options);
    }

    return {
      ARTIFACT_CATEGORY_CODES: CATEGORY_CODES,
      ARTIFACT_ROLE_CODES: ROLE_CODES,
      ArtifactOverlay,
      artifactCode,
      artifactPresentationMetadata,
      createArtifactOverlay,
      createOverlayTransform,
      exifOrientation,
      localClipPath,
      normalizeOverlayRegion,
      orientNormalizedPoint,
      orientedDimensions,
      polygonBounds,
      polygonCentroid,
      projectPolygon,
      selectorPoints,
    };
  });

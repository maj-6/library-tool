(function installCorrectionsLayout(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else Object.assign(root.LibraryToolCorrections ||= {}, api);
})(typeof globalThis !== "undefined" ? globalThis : this, function layoutFactory() {
  "use strict";

  const COMPACT_BREAKPOINT = 1040;
  const EDITOR_MIN_WIDTH = 360;
  const EDITOR_MIN_HEIGHT = 260;
  const HORIZONTAL_GUTTER_WIDTH = 7;
  const VERTICAL_GUTTER_HEIGHT = 7;
  const COLLAPSED_PROPERTIES_WIDTH = 42;
  const DEFAULT_LAYOUT = Object.freeze({
    navigatorWidth: 292,
    booksHeight: 300,
    propertiesWidth: 320,
    trayHeight: 220,
    collapsed: Object.freeze({
      books: false,
      artifacts: false,
      properties: false,
      tray: false,
    }),
    primaryMaximized: false,
  });
  const GUTTERS = Object.freeze({
    navigator: Object.freeze({
      property: "navigatorWidth", css: "--navigator-width", axis: "x",
      direction: 1, min: 220, max: 520, pane: null,
    }),
    properties: Object.freeze({
      property: "propertiesWidth", css: "--properties-width", axis: "x",
      direction: -1, min: 240, max: 560, pane: "properties",
    }),
    books: Object.freeze({
      property: "booksHeight", css: "--books-height", axis: "y",
      direction: 1, min: 120, max: 720, pane: "books",
    }),
    tray: Object.freeze({
      property: "trayHeight", css: "--tray-height", axis: "y",
      direction: -1, min: 120, max: 440, pane: "tray",
    }),
  });
  const COLLAPSIBLE_PANES = new Set(["books", "artifacts", "properties", "tray"]);

  function clamp(value, low, high) {
    return Math.min(Math.max(value, low), high);
  }

  function finiteInteger(value, fallback) {
    return Number.isFinite(value) ? Math.round(value) : fallback;
  }

  function normalizeLayoutState(value) {
    const source = value && typeof value === "object" && !Array.isArray(value) ? value : {};
    const collapsed = source.collapsed && typeof source.collapsed === "object" &&
      !Array.isArray(source.collapsed) ? source.collapsed : {};
    const result = {
      navigatorWidth: clamp(
        finiteInteger(source.navigatorWidth, DEFAULT_LAYOUT.navigatorWidth),
        GUTTERS.navigator.min, GUTTERS.navigator.max),
      booksHeight: clamp(
        finiteInteger(source.booksHeight, DEFAULT_LAYOUT.booksHeight),
        GUTTERS.books.min, GUTTERS.books.max),
      propertiesWidth: clamp(
        finiteInteger(source.propertiesWidth, DEFAULT_LAYOUT.propertiesWidth),
        GUTTERS.properties.min, GUTTERS.properties.max),
      trayHeight: clamp(
        finiteInteger(source.trayHeight, DEFAULT_LAYOUT.trayHeight),
        GUTTERS.tray.min, GUTTERS.tray.max),
      collapsed: {},
      primaryMaximized: source.primaryMaximized === true,
    };
    for (const pane of COLLAPSIBLE_PANES) result.collapsed[pane] = collapsed[pane] === true;
    return result;
  }

  function resizeLayoutState(value, gutterId, coordinateDelta, options = {}) {
    const gutter = GUTTERS[gutterId];
    if (!gutter) throw new TypeError("unknown layout gutter");
    const state = normalizeLayoutState(value);
    const low = Number.isFinite(options.min) ? options.min : gutter.min;
    const high = Number.isFinite(options.max) ? options.max : gutter.max;
    state[gutter.property] = clamp(
      state[gutter.property] + coordinateDelta * gutter.direction,
      low,
      high,
    );
    return state;
  }

  function fitHorizontalLayoutState(value, workspaceWidth) {
    const state = normalizeLayoutState(value);
    const width = finiteInteger(workspaceWidth, 0);
    if (width <= COMPACT_BREAKPOINT || state.primaryMaximized ||
        state.collapsed.properties) {
      return state;
    }
    const availableForSidePanes = width - EDITOR_MIN_WIDTH -
      HORIZONTAL_GUTTER_WIDTH * 2;
    const minimumSidePaneWidth = GUTTERS.navigator.min + GUTTERS.properties.min;
    if (availableForSidePanes <= minimumSidePaneWidth) {
      state.navigatorWidth = GUTTERS.navigator.min;
      state.propertiesWidth = GUTTERS.properties.min;
      return state;
    }
    const requestedSidePaneWidth = state.navigatorWidth + state.propertiesWidth;
    if (requestedSidePaneWidth <= availableForSidePanes) return state;

    const availableSlack = availableForSidePanes - minimumSidePaneWidth;
    const navigatorSlack = state.navigatorWidth - GUTTERS.navigator.min;
    const propertiesSlack = state.propertiesWidth - GUTTERS.properties.min;
    const requestedSlack = navigatorSlack + propertiesSlack;
    const fittedNavigatorSlack = requestedSlack > 0
      ? Math.round(availableSlack * navigatorSlack / requestedSlack) : 0;
    state.navigatorWidth = GUTTERS.navigator.min + fittedNavigatorSlack;
    state.propertiesWidth = GUTTERS.properties.min +
      (availableSlack - fittedNavigatorSlack);
    return state;
  }

  function fitVerticalLayoutState(value, workspaceHeight) {
    const state = normalizeLayoutState(value);
    const height = finiteInteger(workspaceHeight, 0);
    if (height <= 0 || state.primaryMaximized) return state;
    if (!state.collapsed.tray) {
      const trayHigh = Math.min(GUTTERS.tray.max, Math.max(
        GUTTERS.tray.min,
        height - EDITOR_MIN_HEIGHT - VERTICAL_GUTTER_HEIGHT,
      ));
      state.trayHeight = clamp(state.trayHeight, GUTTERS.tray.min, trayHigh);
    }
    if (!state.collapsed.books && !state.collapsed.artifacts) {
      const booksHigh = Math.min(GUTTERS.books.max, Math.max(
        GUTTERS.books.min,
        height - GUTTERS.books.min - VERTICAL_GUTTER_HEIGHT,
      ));
      state.booksHeight = clamp(state.booksHeight, GUTTERS.books.min, booksHigh);
    }
    return state;
  }

  function keyboardCoordinateDelta(gutterId, key, shiftKey = false) {
    const gutter = GUTTERS[gutterId];
    if (!gutter) return null;
    const step = shiftKey ? 48 : 16;
    if (gutter.axis === "x") {
      if (key === "ArrowLeft") return -step;
      if (key === "ArrowRight") return step;
    } else {
      if (key === "ArrowUp") return -step;
      if (key === "ArrowDown") return step;
    }
    return null;
  }

  function copyState(state) {
    return {
      navigatorWidth: state.navigatorWidth,
      booksHeight: state.booksHeight,
      propertiesWidth: state.propertiesWidth,
      trayHeight: state.trayHeight,
      collapsed: { ...state.collapsed },
      primaryMaximized: state.primaryMaximized,
    };
  }

  class LayoutController {
    constructor(options = {}) {
      if (!options.root || typeof options.root.querySelector !== "function") {
        throw new TypeError("layout root is required");
      }
      this.root = options.root;
      this.workspace = this.root.querySelector("[data-workspace-layout]");
      if (!this.workspace) throw new TypeError("workspace layout element is required");
      this.documentRef = options.documentRef || this.root.ownerDocument;
      this.windowRef = options.windowRef ||
        (this.documentRef && this.documentRef.defaultView) || null;
      this.onChange = typeof options.onChange === "function" ? options.onChange : () => {};
      this.state = normalizeLayoutState(options.initialState);
      this.compact = false;
      this.drawers = { navigator: false, properties: false };
      this.listeners = [];
      this.activePointerCleanup = null;
      this.mediaQuery = null;
      this.applyState();
      if (options.bind !== false) this.bind();
    }

    listen(target, type, handler, options) {
      if (!target || typeof target.addEventListener !== "function") return;
      target.addEventListener(type, handler, options);
      this.listeners.push(() => target.removeEventListener(type, handler, options));
    }

    bind() {
      for (const gutterElement of this.root.querySelectorAll("[data-layout-gutter]")) {
        const gutterId = gutterElement.dataset.layoutGutter;
        if (!GUTTERS[gutterId]) continue;
        this.listen(gutterElement, "pointerdown", (event) =>
          this.startPointerResize(gutterId, gutterElement, event));
        this.listen(gutterElement, "keydown", (event) =>
          this.handleGutterKey(gutterId, event));
        this.listen(gutterElement, "dblclick", () =>
          this.resetDimension(gutterId));
      }
      for (const button of this.root.querySelectorAll("[data-collapse-pane]")) {
        this.listen(button, "click", () => this.toggleCollapse(button.dataset.collapsePane));
      }
      const maximize = this.root.querySelector("[data-layout-action='maximize-primary']");
      this.listen(maximize, "click", () => this.togglePrimaryMaximized());
      for (const button of this.root.querySelectorAll("[data-drawer-toggle]")) {
        this.listen(button, "click", () => this.toggleDrawer(button.dataset.drawerToggle));
      }
      this.listen(this.root.querySelector("[data-close-drawers]"), "click", () =>
        this.closeDrawers());
      this.listen(this.root, "keydown", (event) => {
        if (event.key !== "Escape") return;
        if (this.state.primaryMaximized) {
          event.preventDefault();
          this.togglePrimaryMaximized(false);
        } else if (this.drawers.navigator || this.drawers.properties) {
          event.preventDefault();
          this.closeDrawers();
        }
      });
      if (this.windowRef && typeof this.windowRef.matchMedia === "function") {
        this.mediaQuery = this.windowRef.matchMedia(
          `(max-width: ${COMPACT_BREAKPOINT}px)`);
        const onMedia = (event) => this.setCompact(event.matches);
        this.setCompact(this.mediaQuery.matches);
        if (typeof this.mediaQuery.addEventListener === "function") {
          this.mediaQuery.addEventListener("change", onMedia);
          this.listeners.push(() => this.mediaQuery.removeEventListener("change", onMedia));
        } else if (typeof this.mediaQuery.addListener === "function") {
          this.mediaQuery.addListener(onMedia);
          this.listeners.push(() => this.mediaQuery.removeListener(onMedia));
        }
      }
      this.listen(this.windowRef, "resize", () => this.applyState());
    }

    limitsFor(gutterId) {
      const gutter = GUTTERS[gutterId];
      let high = gutter.max;
      const width = Number(this.workspace.clientWidth || 0);
      const height = Number(this.workspace.clientHeight || 0);
      if (gutterId === "navigator" && width > 0) {
        const propertiesWidth = this.state.collapsed.properties
          ? COLLAPSED_PROPERTIES_WIDTH : this.state.propertiesWidth;
        const gutterWidth = this.state.collapsed.properties
          ? HORIZONTAL_GUTTER_WIDTH : HORIZONTAL_GUTTER_WIDTH * 2;
        high = Math.min(high, Math.max(gutter.min,
          width - propertiesWidth - EDITOR_MIN_WIDTH - gutterWidth));
      } else if (gutterId === "properties" && width > 0) {
        high = Math.min(high, Math.max(gutter.min,
          width - this.state.navigatorWidth - EDITOR_MIN_WIDTH -
            HORIZONTAL_GUTTER_WIDTH * 2));
      } else if (gutterId === "books" && height > 0) {
        high = Math.min(high, Math.max(gutter.min, height - 120 - 7));
      } else if (gutterId === "tray" && height > 0) {
        high = Math.min(high, Math.max(gutter.min,
          height - EDITOR_MIN_HEIGHT - VERTICAL_GUTTER_HEIGHT));
      }
      return { min: gutter.min, max: high };
    }

    startPointerResize(gutterId, gutterElement, event) {
      if (event.button != null && event.button !== 0) return;
      if (this.activePointerCleanup) this.activePointerCleanup();
      const gutter = GUTTERS[gutterId];
      const startCoordinate = gutter.axis === "x" ? event.clientX : event.clientY;
      const startValue = this.state[gutter.property];
      if (!Number.isFinite(startCoordinate)) return;
      event.preventDefault();
      gutterElement.dataset.dragging = "true";
      if (typeof gutterElement.setPointerCapture === "function" && event.pointerId != null) {
        try { gutterElement.setPointerCapture(event.pointerId); } catch (error) { /* stale pointer */ }
      }
      const move = (moveEvent) => {
        const coordinate = gutter.axis === "x" ? moveEvent.clientX : moveEvent.clientY;
        if (!Number.isFinite(coordinate)) return;
        this.setDimension(gutterId,
          startValue + (coordinate - startCoordinate) * gutter.direction,
          "pointer-resize");
      };
      const cleanup = () => {
        delete gutterElement.dataset.dragging;
        this.documentRef.removeEventListener("pointermove", move);
        this.documentRef.removeEventListener("pointerup", cleanup);
        this.documentRef.removeEventListener("pointercancel", cleanup);
        if (this.activePointerCleanup === cleanup) this.activePointerCleanup = null;
      };
      this.activePointerCleanup = cleanup;
      this.documentRef.addEventListener("pointermove", move);
      this.documentRef.addEventListener("pointerup", cleanup);
      this.documentRef.addEventListener("pointercancel", cleanup);
    }

    handleGutterKey(gutterId, event) {
      const gutter = GUTTERS[gutterId];
      const limits = this.limitsFor(gutterId);
      let value = null;
      if (event.key === "Home") value = limits.min;
      else if (event.key === "End") value = limits.max;
      else {
        const delta = keyboardCoordinateDelta(gutterId, event.key, event.shiftKey);
        if (delta != null) value = this.state[gutter.property] + delta * gutter.direction;
      }
      if (value == null) return false;
      event.preventDefault();
      this.setDimension(gutterId, value, "keyboard-resize");
      return true;
    }

    setDimension(gutterId, value, reason = "resize") {
      const gutter = GUTTERS[gutterId];
      if (!gutter) throw new TypeError("unknown layout gutter");
      const limits = this.limitsFor(gutterId);
      this.state[gutter.property] = clamp(finiteInteger(value,
        this.state[gutter.property]), limits.min, limits.max);
      this.applyState();
      this.onChange(this.getState(), reason);
    }

    resetDimension(gutterId) {
      const gutter = GUTTERS[gutterId];
      this.setDimension(gutterId, DEFAULT_LAYOUT[gutter.property], "reset-dimension");
    }

    toggleCollapse(pane, force) {
      if (!COLLAPSIBLE_PANES.has(pane)) return false;
      this.state.collapsed[pane] = typeof force === "boolean"
        ? force : !this.state.collapsed[pane];
      this.applyState();
      this.onChange(this.getState(), "collapse-pane");
      return true;
    }

    togglePrimaryMaximized(force) {
      this.state.primaryMaximized = typeof force === "boolean"
        ? force : !this.state.primaryMaximized;
      this.closeDrawers();
      this.applyState();
      this.onChange(this.getState(), "maximize-primary");
      return this.state.primaryMaximized;
    }

    setCompact(value) {
      this.compact = value === true;
      if (!this.compact) this.drawers = { navigator: false, properties: false };
      this.applyState();
    }

    toggleDrawer(name, force) {
      if (!["navigator", "properties"].includes(name) || !this.compact ||
          this.state.primaryMaximized) return false;
      const open = typeof force === "boolean" ? force : !this.drawers[name];
      this.drawers = {
        navigator: name === "navigator" ? open : false,
        properties: name === "properties" ? open : false,
      };
      this.applyState();
      return open;
    }

    closeDrawers() {
      this.drawers = { navigator: false, properties: false };
      this.applyState();
    }

    replaceState(value, emit = false) {
      this.state = normalizeLayoutState(value);
      this.closeDrawers();
      this.applyState();
      if (emit) this.onChange(this.getState(), "replace-layout");
    }

    reset(emit = true) {
      this.state = normalizeLayoutState(DEFAULT_LAYOUT);
      this.closeDrawers();
      this.applyState();
      if (emit) this.onChange(this.getState(), "reset-layout");
    }

    applyState() {
      this.state = fitHorizontalLayoutState(
        this.state, Number(this.workspace.clientWidth || 0));
      this.state = fitVerticalLayoutState(
        this.state, Number(this.workspace.clientHeight || 0));
      const style = this.workspace.style;
      for (const gutter of Object.values(GUTTERS)) {
        style.setProperty(gutter.css, `${this.state[gutter.property]}px`);
      }
      for (const pane of COLLAPSIBLE_PANES) {
        this.workspace.dataset[`${pane}Collapsed`] = String(this.state.collapsed[pane]);
        const button = this.root.querySelector(`[data-collapse-pane='${pane}']`);
        if (button) {
          const expanded = !this.state.collapsed[pane];
          button.setAttribute("aria-expanded", String(expanded));
          button.setAttribute("aria-label", `${expanded ? "Collapse" : "Expand"} ${pane} panel`);
        }
      }
      this.workspace.dataset.primaryMaximized = String(this.state.primaryMaximized);
      this.workspace.dataset.compact = String(this.compact);
      this.root.dataset.compact = String(this.compact);
      this.workspace.dataset.navigatorOpen = String(this.drawers.navigator);
      this.workspace.dataset.propertiesOpen = String(this.drawers.properties);
      const maximize = this.root.querySelector("[data-layout-action='maximize-primary']");
      if (maximize) {
        maximize.setAttribute("aria-pressed", String(this.state.primaryMaximized));
        maximize.textContent = this.state.primaryMaximized ? "Restore panels" : "Maximize editor";
      }
      for (const button of this.root.querySelectorAll("[data-drawer-toggle]")) {
        button.setAttribute("aria-expanded", String(!!this.drawers[button.dataset.drawerToggle]));
        button.disabled = this.state.primaryMaximized;
      }
      for (const gutterElement of this.root.querySelectorAll("[data-layout-gutter]")) {
        const gutterId = gutterElement.dataset.layoutGutter;
        const gutter = GUTTERS[gutterId];
        if (!gutter) continue;
        const limits = this.limitsFor(gutterId);
        gutterElement.setAttribute("aria-valuemin", String(limits.min));
        gutterElement.setAttribute("aria-valuemax", String(limits.max));
        gutterElement.setAttribute("aria-valuenow", String(this.state[gutter.property]));
      }
    }

    getState() {
      return copyState(this.state);
    }

    destroy() {
      if (this.activePointerCleanup) this.activePointerCleanup();
      for (const remove of this.listeners.splice(0)) remove();
    }
  }

  return {
    COMPACT_BREAKPOINT,
    DEFAULT_LAYOUT,
    EDITOR_MIN_HEIGHT,
    EDITOR_MIN_WIDTH,
    GUTTERS,
    LayoutController,
    fitHorizontalLayoutState,
    fitVerticalLayoutState,
    keyboardCoordinateDelta,
    normalizeLayoutState,
    resizeLayoutState,
  };
});

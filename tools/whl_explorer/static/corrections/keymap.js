(function installCorrectionsKeymap(root, factory) {
  const dependencies = typeof module === "object" && module.exports
    ? require("./commands") : root.LibraryToolCorrections;
  const api = factory(dependencies);
  if (typeof module === "object" && module.exports) module.exports = api;
  else Object.assign(root.LibraryToolCorrections ||= {}, api);
})(typeof globalThis !== "undefined" ? globalThis : this,
  function correctionsKeymapFactory(commands) {
    "use strict";

    const TYPING_TAGS = new Set(["INPUT", "TEXTAREA", "SELECT"]);
    const TYPING_ROLES = new Set([
      "combobox", "searchbox", "spinbutton", "textbox",
    ]);

    function text(value, maximum = 512) {
      return value == null ? "" : String(value).trim().slice(0, maximum);
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

    function attribute(node, name) {
      return node && typeof node.getAttribute === "function"
        ? node.getAttribute(name) : null;
    }

    function typingTarget(node) {
      let cursor = node;
      while (cursor) {
        if (TYPING_TAGS.has(String(cursor.tagName || "").toUpperCase()) ||
            cursor.isContentEditable === true ||
            attribute(cursor, "contenteditable") === "true" ||
            TYPING_ROLES.has(String(attribute(cursor, "role") || "").toLowerCase()) ||
            attribute(cursor, "data-corrections-typing") === "true") {
          return true;
        }
        cursor = cursor.parentNode;
      }
      return false;
    }

    function dialogTarget(node) {
      let cursor = node;
      while (cursor) {
        const tag = String(cursor.tagName || "").toUpperCase();
        const role = String(attribute(cursor, "role") || "").toLowerCase();
        if (tag === "DIALOG" || role === "dialog" || role === "alertdialog" ||
            attribute(cursor, "aria-modal") === "true") return true;
        cursor = cursor.parentNode;
      }
      return false;
    }

    function nativeActivationTarget(node) {
      let cursor = node;
      while (cursor) {
        const tag = String(cursor.tagName || "").toUpperCase();
        const role = String(attribute(cursor, "role") || "").toLowerCase();
        if (tag === "BUTTON" || tag === "SUMMARY" ||
            (tag === "A" && attribute(cursor, "href")) ||
            ["button", "link", "menuitem"].includes(role)) return true;
        cursor = cursor.parentNode;
      }
      return false;
    }

    function visibleModal(root, documentRef) {
      const selectors = [
        "[aria-modal='true']",
        "[data-corrections-modal='true']",
        "dialog",
      ];
      for (const owner of [root, documentRef]) {
        if (!owner || typeof owner.querySelector !== "function") continue;
        for (const selector of selectors) {
          const foundValues = typeof owner.querySelectorAll === "function"
            ? Array.from(owner.querySelectorAll(selector))
            : [owner.querySelector(selector)].filter(Boolean);
          for (const found of foundValues) {
            if (found.hidden === true ||
                attribute(found, "aria-hidden") === "true") continue;
            if (String(found.tagName || "").toUpperCase() === "DIALOG" &&
                found.open !== true && attribute(found, "open") == null &&
                attribute(found, "aria-modal") !== "true") continue;
            return true;
          }
        }
      }
      return false;
    }

    function gestureOwnsKeyboard(owner, event) {
      const value = typeof owner === "function" ? owner(event) : owner;
      if (!value) return false;
      if (typeof value === "object") {
        if (typeof value.ownsKeyboard === "function") {
          return value.ownsKeyboard(event) !== false;
        }
        if (value.active === false || value.ownsKeyboard === false) return false;
      }
      return true;
    }

    function eligibleKeyEvent(event, options = {}) {
      if (!event || event.defaultPrevented || event.repeat || event.isComposing) return false;
      const root = options.scope;
      const documentRef = options.documentRef ||
        root && root.ownerDocument || null;
      const target = event.target || documentRef && documentRef.activeElement;
      if (!nodeInside(root, target)) return false;
      if (typingTarget(target) || dialogTarget(target) ||
          visibleModal(root, documentRef)) return false;
      if (documentRef && typeof documentRef.hasFocus === "function" &&
          documentRef.hasFocus() === false) return false;
      if (options.windowRef && event.view && event.view !== options.windowRef) return false;
      if (typeof options.isScopeActive === "function" &&
          options.isScopeActive(event) === false) return false;
      if (gestureOwnsKeyboard(
        typeof options.getGestureOwner === "function"
          ? options.getGestureOwner(event) : options.gestureOwner,
        event,
      )) return false;
      return true;
    }

    class ScopedCorrectionKeymap {
      constructor(options = {}) {
        if (!options.scope || typeof options.scope.addEventListener !== "function") {
          throw new TypeError("a Corrections keymap scope is required");
        }
        if (!options.registry ||
            typeof options.registry.commandForBinding !== "function") {
          throw new TypeError("a correction command registry is required");
        }
        this.scope = options.scope;
        this.registry = options.registry;
        this.documentRef = options.documentRef ||
          this.scope.ownerDocument || null;
        this.windowRef = options.windowRef ||
          this.documentRef && this.documentRef.defaultView || null;
        this.getContext = typeof options.getContext === "function"
          ? options.getContext : () => ({});
        this.getGestureOwner = typeof options.getGestureOwner === "function"
          ? options.getGestureOwner : () => null;
        this.isScopeActive = typeof options.isScopeActive === "function"
          ? options.isScopeActive : () => true;
        this.isEventEligible = typeof options.isEventEligible === "function"
          ? options.isEventEligible : null;
        this.onError = typeof options.onError === "function"
          ? options.onError : () => {};
        this.onInvoked = typeof options.onInvoked === "function"
          ? options.onInvoked : () => {};
        this.mounted = false;
        this.handleKeydown = this.handleKeydown.bind(this);
      }

      mount() {
        if (this.mounted) return this;
        this.scope.addEventListener("keydown", this.handleKeydown);
        this.mounted = true;
        return this;
      }

      destroy() {
        if (!this.mounted) return;
        this.scope.removeEventListener("keydown", this.handleKeydown);
        this.mounted = false;
      }

      handleKeydown(event) {
        if (!eligibleKeyEvent(event, {
          scope: this.scope,
          documentRef: this.documentRef,
          windowRef: this.windowRef,
          isScopeActive: this.isScopeActive,
          getGestureOwner: this.getGestureOwner,
        })) return false;
        const binding = commands.eventKeyBinding(event);
        const command = binding && this.registry.commandForBinding(binding);
        if (!command) return false;
        if (["enter", "space"].includes(binding) &&
            nativeActivationTarget(event.target)) return false;
        const context = this.getContext("shortcut", event, command);
        if (this.isEventEligible &&
            this.isEventEligible(event, command, context) !== true) return false;
        if (!this.registry.canInvoke(command.id, context)) return false;
        event.preventDefault();
        Promise.resolve(this.registry.invoke(command.id, context))
          .then((result) => this.onInvoked(result, command, event))
          .catch((error) => this.onError(error, command, event));
        return true;
      }
    }

    function commandTargetKey(target) {
      if (!target || typeof target !== "object") return "";
      return text(target.key || target.artifactId || target.artifact_id ||
        target.annotationId || target.annotation_id || target.id, 520);
    }

    function markTargetElement(element, state, value) {
      if (!element || !element.dataset) return;
      const name = state === "focused"
        ? "classificationFocused" : "classificationHot";
      if (value) element.dataset[name] = "true";
      else delete element.dataset[name];
    }

    class ClassificationController {
      constructor(options = {}) {
        if (!options.scope || typeof options.scope.addEventListener !== "function") {
          throw new TypeError("a Corrections controller scope is required");
        }
        this.scope = options.scope;
        this.documentRef = options.documentRef || this.scope.ownerDocument || null;
        this.windowRef = options.windowRef ||
          this.documentRef && this.documentRef.defaultView || null;
        this.selectionTarget = null;
        this.selectionElement = null;
        this.selectionFocused = true;
        this.hotTarget = null;
        this.hotElement = null;
        this.canvasOwner = null;
        this.scopeActive = true;
        this.stateListeners = new Set();
        this.resolveLinkedArtifact = typeof options.resolveLinkedArtifact === "function"
          ? options.resolveLinkedArtifact : null;
        this.refreshTarget = typeof options.refreshTarget === "function"
          ? options.refreshTarget : null;
        this.promoteTarget = typeof options.promoteSoftTarget === "function"
          ? options.promoteSoftTarget : null;
        this.onTarget = typeof options.onTarget === "function"
          ? options.onTarget : () => {};
        this.onError = typeof options.onError === "function"
          ? options.onError : () => {};
        this.registry = options.registry || new commands.CorrectionCommandRegistry({
          bindings: options.bindings,
        });
        const registered = commands.registerClassificationCommands(this.registry, {
          port: options.port || options.commands,
          history: options.history,
          operationIdFactory: options.operationIdFactory,
          onChanged: options.onChanged,
          onConflict: options.onConflict,
          onStatus: options.onStatus,
        });
        this.executor = registered.executor;
        this.keymap = options.keymap || new ScopedCorrectionKeymap({
          scope: this.scope,
          documentRef: this.documentRef,
          windowRef: this.windowRef,
          registry: this.registry,
          getContext: (source, event, command) =>
            this.commandContext(source, event, command),
          getGestureOwner: () => this.canvasOwner,
          isScopeActive: () => this.scopeActive,
          isEventEligible: options.isEventEligible,
          onError: this.onError,
          onInvoked: options.onInvoked,
        });
        this.destroyed = false;
      }

      mount() {
        if (this.destroyed) return this;
        this.keymap.mount();
        return this;
      }

      setSelectionTarget(target, options = {}) {
        markTargetElement(this.selectionElement, "focused", false);
        this.selectionTarget = target || null;
        this.selectionElement = options.element || null;
        this.selectionFocused = options.focused !== false;
        markTargetElement(
          this.selectionElement,
          "focused",
          !!this.selectionTarget && this.selectionFocused,
        );
        if (this.scope.dataset) {
          this.scope.dataset.classificationSelection =
            commandTargetKey(this.selectionTarget);
        }
        this.emitState("selection");
        return this.selectionTarget;
      }

      setSelectionFocus(value) {
        if (this.destroyed) return false;
        this.selectionFocused = value !== false;
        markTargetElement(
          this.selectionElement,
          "focused",
          !!this.selectionTarget && this.selectionFocused,
        );
        this.emitState("selection-focus");
        return this.selectionFocused;
      }

      setHotTarget(target, options = {}) {
        markTargetElement(this.hotElement, "hot", false);
        this.hotTarget = target || null;
        this.hotElement = options.element || null;
        markTargetElement(this.hotElement, "hot", !!this.hotTarget);
        if (this.scope.dataset) {
          this.scope.dataset.classificationHot = commandTargetKey(this.hotTarget);
        }
        this.emitState("hot-target");
        return this.hotTarget;
      }

      setCanvasOwner(owner) {
        this.canvasOwner = owner || null;
        this.emitState("canvas-owner");
        return this.canvasOwner;
      }

      setScopeActive(value) {
        this.scopeActive = value !== false;
        this.emitState("scope-active");
        return this.scopeActive;
      }

      stateSnapshot() {
        return Object.freeze({
          selectionTarget: this.selectionTarget,
          selectionFocused: this.selectionFocused,
          hotTarget: this.hotTarget,
          canvasOwner: this.canvasOwner,
          scopeActive: this.scopeActive,
        });
      }

      subscribe(listener) {
        if (typeof listener !== "function") throw new TypeError("listener is required");
        this.stateListeners.add(listener);
        listener(this.stateSnapshot(), Object.freeze({ type: "initial" }));
        return () => this.stateListeners.delete(listener);
      }

      emitState(type) {
        const snapshot = this.stateSnapshot();
        const change = Object.freeze({ type });
        for (const listener of [...this.stateListeners]) listener(snapshot, change);
      }

      async promoteSoftTarget(target, command) {
        let promoted = target;
        if (this.promoteTarget) {
          promoted = await this.promoteTarget(target, command);
          if (promoted === false || promoted == null) return promoted;
        }
        if (promoted === true) promoted = target;
        this.setSelectionTarget(promoted, {
          element: this.hotElement,
          focused: true,
        });
        if (this.selectionElement &&
            typeof this.selectionElement.focus === "function") {
          this.selectionElement.focus();
        }
        return promoted;
      }

      announceTarget(detail) {
        const live = typeof this.scope.querySelector === "function"
          ? this.scope.querySelector("[data-corrections-command-target]") : null;
        if (live) live.textContent = `${detail.command.shortLabel}: ${detail.name}`;
        if (this.scope.dataset) {
          this.scope.dataset.classificationCommandTarget =
            commandTargetKey(detail.target);
        }
        return this.onTarget(detail);
      }

      commandContext(source = "api", event = null, command = null) {
        return {
          focusedTarget: this.selectionFocused ? this.selectionTarget : null,
          selectionTarget: this.selectionTarget,
          softTarget: this.hotTarget,
          resolveLinkedArtifact: this.resolveLinkedArtifact,
          refreshTarget: this.refreshTarget,
          promoteSoftTarget: (target, selectedCommand) =>
            this.promoteSoftTarget(target, selectedCommand),
          announceTarget: (detail) => this.announceTarget(detail),
          source,
          event,
          command,
        };
      }

      invoke(commandId, options = {}) {
        return this.registry.invoke(
          commandId,
          { ...this.commandContext(options.source || "api"), ...options.context },
        );
      }

      bindControl(commandId, control, options = {}) {
        return commands.bindCommandControl(this.registry, commandId, control, {
          ...options,
          getContext: (source) => ({
            ...this.commandContext(source),
            ...(typeof options.getContext === "function"
              ? options.getContext(source) : null),
          }),
          onError: options.onError || this.onError,
        });
      }

      undoLast(options = {}) {
        return this.executor.undoLast({
          ...this.commandContext(options.source || "undo"),
          signal: options.signal,
        });
      }

      paletteEntries(target = null) {
        const context = target
          ? {
            ...this.commandContext("palette"),
            focusedTarget: target,
            selectionTarget: target,
            softTarget: null,
          }
          : this.commandContext("palette");
        return this.registry.list().map((command) => Object.freeze({
          id: command.id,
          label: command.label,
          code: command.code,
          binding: command.binding,
          bindingLabel: command.bindingLabel,
          available: this.registry.canInvoke(command.id, context),
          invoke: () => this.registry.invoke(command.id, context),
        }));
      }

      destroy() {
        if (this.destroyed) return;
        this.destroyed = true;
        this.keymap.destroy();
        markTargetElement(this.selectionElement, "focused", false);
        markTargetElement(this.hotElement, "hot", false);
        this.selectionTarget = null;
        this.selectionElement = null;
        this.selectionFocused = false;
        this.hotTarget = null;
        this.hotElement = null;
        this.stateListeners.clear();
      }
    }

    function createScopedCorrectionKeymap(options) {
      return new ScopedCorrectionKeymap(options);
    }

    function createClassificationController(options) {
      return new ClassificationController(options);
    }

    return {
      ClassificationController,
      ScopedCorrectionKeymap,
      createClassificationController,
      createScopedCorrectionKeymap,
      dialogTarget,
      eligibleKeyEvent,
      gestureOwnsKeyboard,
      nativeActivationTarget,
      nodeInside,
      typingTarget,
    };
  });

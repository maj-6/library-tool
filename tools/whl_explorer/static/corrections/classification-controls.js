(function installCorrectionsClassificationControls(root, factory) {
  const dependencies = typeof module === "object" && module.exports
    ? require("./commands") : root.LibraryToolCorrections;
  const api = factory(dependencies);
  if (typeof module === "object" && module.exports) module.exports = api;
  else Object.assign(root.LibraryToolCorrections ||= {}, api);
})(typeof globalThis !== "undefined" ? globalThis : this,
  function correctionsClassificationControlsFactory(commands) {
    "use strict";

    const CLASSIFICATION_IDS = Object.freeze(
      commands.DEFAULT_CLASSIFICATION_COMMANDS.map((command) => command.id),
    );
    const CLASSIFICATION_ID_SET = new Set(CLASSIFICATION_IDS);
    let presenterSequence = 0;

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

    function bindingValue(registry, commandId) {
      const binding = registry.bindingFor(commandId);
      return binding ? commands.displayKeyBinding(binding) : "Unassigned";
    }

    function conflictLabels(registry, error) {
      const ids = error && error.details && Array.isArray(error.details.commandIds)
        ? error.details.commandIds : [];
      return ids.map((id) => {
        const command = registry.get(id);
        return command ? command.label : id;
      });
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

    function removeNode(node) {
      if (!node) return;
      if (typeof node.remove === "function") node.remove();
      else if (node.parentNode && typeof node.parentNode.removeChild === "function") {
        node.parentNode.removeChild(node);
      }
    }

    function rememberNode(map, key, node) {
      const nodes = map.get(key) || [];
      nodes.push(node);
      map.set(key, nodes);
    }

    class ClassificationControls {
      constructor(options = {}) {
        if (!options.root || typeof options.root.replaceChildren !== "function") {
          throw new TypeError("a classification controls host is required");
        }
        if (!options.controller ||
            typeof options.controller.bindControl !== "function" ||
            typeof options.controller.invoke !== "function" ||
            !options.controller.registry) {
          throw new TypeError("a classification controller is required");
        }
        this.root = options.root;
        this.documentRef = options.documentRef || this.root.ownerDocument;
        this.controller = options.controller;
        this.registry = this.controller.registry;
        this.toolbarRoot = options.toolbarRoot || null;
        this.contextScope = options.contextScope || this.controller.scope || null;
        this.paletteTrigger = options.paletteTrigger || null;
        this.windowRef = options.windowRef ||
          this.documentRef && this.documentRef.defaultView || null;
        this.isContextMenuEvent =
          typeof options.isContextMenuEvent === "function"
            ? options.isContextMenuEvent : () => false;
        this.onError = typeof options.onError === "function"
          ? options.onError : () => {};
        this.onBindingsChanged = typeof options.onBindingsChanged === "function"
          ? options.onBindingsChanged : () => {};
        this.id = `classification-controls-${++presenterSequence}`;
        this.controlBindings = [];
        this.listeners = [];
        this.commandButtons = new Map();
        this.commandKeys = new Map();
        this.shortcutInputs = new Map();
        this.resetButtons = new Map();
        this.status = null;
        this.unsubscribeRegistry = null;
        this.unsubscribeController = null;
        this.contextMenu = null;
        this.contextMenuEntries = Object.freeze([]);
        this.contextMenuReturnFocus = null;
        this.palette = null;
        this.paletteList = null;
        this.paletteSearch = null;
        this.paletteEntries = Object.freeze([]);
        this.mounted = false;
        this.destroyed = false;
      }

      definitions() {
        return CLASSIFICATION_IDS.map((id) => this.registry.get(id)).filter(Boolean);
      }

      listen(target, type, listener) {
        target.addEventListener(type, listener);
        this.listeners.push(() => target.removeEventListener(type, listener));
      }

      mount() {
        if (this.mounted || this.destroyed) return this;
        this.mounted = true;
        clearNode(this.root);
        this.root.setAttribute("aria-label",
          this.root.getAttribute("aria-label") || "Classification");
        this.root.className = `${this.root.className || ""} classification-controls-host`
          .trim();

        const surface = element(
          this.documentRef,
          "section",
          "classification-controls",
        );
        surface.setAttribute("aria-labelledby", `${this.id}-title`);
        const title = element(
          this.documentRef,
          "h2",
          "classification-controls-title",
          "Classify",
        );
        title.id = `${this.id}-title`;
        const hint = element(
          this.documentRef,
          "p",
          "classification-controls-hint",
          "Choose a label for the focused image or region.",
        );

        const toolbar = element(
          this.documentRef,
          "div",
          "classification-command-toolbar",
        );
        toolbar.setAttribute("role", "toolbar");
        toolbar.setAttribute("aria-label", "Classification commands");
        for (const command of this.definitions()) {
          toolbar.append(this.commandButton(command));
        }

        const shortcutEditor = this.shortcutEditor();
        surface.append(title, hint, toolbar, shortcutEditor);
        this.root.append(surface);
        this.mountWorkspaceToolbar();
        this.mountContextMenu();
        this.mountPalette();
        this.unsubscribeRegistry = this.registry.subscribe((change) => {
          this.refresh();
          if (change.type === "remapped") {
            this.onBindingsChanged(this.bindingsSnapshot(), change);
          }
        });
        if (typeof this.controller.subscribe === "function") {
          this.unsubscribeController = this.controller.subscribe(() => this.refresh());
        }
        this.refresh();
        return this;
      }

      commandButton(command, options = {}) {
        const compact = options.compact === true;
        const button = element(
          this.documentRef,
          "button",
          `classification-command-button${
            compact ? " classification-command-button-compact" : ""}`,
        );
        button.type = "button";
        button.dataset.classificationCommand = command.id;
        button.dataset.commandButton = "true";
        const code = element(
          this.documentRef,
          "span",
          "classification-command-code",
          command.code,
        );
        code.setAttribute("aria-hidden", "true");
        const label = element(
          this.documentRef,
          "span",
          "classification-command-label",
          command.shortLabel,
        );
        if (compact) label.className += " sr-only";
        const key = element(
          this.documentRef,
          "kbd",
          "classification-command-key",
        );
        if (compact) key.className += " sr-only";
        key.setAttribute("aria-hidden", "true");
        button.append(code, label, key);
        rememberNode(this.commandButtons, command.id, button);
        rememberNode(this.commandKeys, command.id, key);
        this.controlBindings.push(this.controller.bindControl(
          command.id,
          button,
          { onError: (error) => this.handleCommandError(error, command) },
        ));
        return button;
      }

      mountWorkspaceToolbar() {
        if (!this.toolbarRoot ||
            typeof this.toolbarRoot.replaceChildren !== "function") return;
        clearNode(this.toolbarRoot);
        this.toolbarRoot.setAttribute("role", "toolbar");
        this.toolbarRoot.setAttribute(
          "aria-label",
          this.toolbarRoot.getAttribute("aria-label") || "Classification commands",
        );
        for (const command of this.definitions()) {
          this.toolbarRoot.append(this.commandButton(command, { compact: true }));
        }
      }

      surfaceEntries(target = null) {
        if (typeof this.controller.paletteEntries === "function") {
          return this.controller.paletteEntries(target);
        }
        const baseContext = typeof this.controller.commandContext === "function"
          ? this.controller.commandContext("command-surface") : {};
        const context = target
          ? {
            ...baseContext,
            focusedTarget: target,
            selectionTarget: target,
            softTarget: null,
          }
          : baseContext;
        return this.registry.list().map((command) => Object.freeze({
          id: command.id,
          label: command.label,
          code: command.code,
          binding: command.binding,
          bindingLabel: command.bindingLabel,
          available: this.registry.canInvoke(command.id, context),
          invoke: () => this.controller.invoke(command.id, {
            source: "command-surface",
            context,
          }),
        }));
      }

      surfaceCommandButton(entry, surface, close) {
        const button = element(
          this.documentRef,
          "button",
          `classification-surface-command classification-${surface}-command`,
        );
        button.type = "button";
        button.dataset.surfaceCommand = entry.id;
        if (surface === "context-menu") button.setAttribute("role", "menuitem");
        const code = element(
          this.documentRef,
          "span",
          "classification-surface-code",
          entry.code,
        );
        code.setAttribute("aria-hidden", "true");
        const label = element(
          this.documentRef,
          "span",
          "classification-surface-label",
          entry.label,
        );
        const key = element(
          this.documentRef,
          "kbd",
          "classification-surface-key",
          entry.bindingLabel,
        );
        key.setAttribute("aria-hidden", "true");
        button.append(code, label, key);
        button.disabled = entry.available !== true;
        button.setAttribute(
          "aria-label",
          `${entry.label}${entry.binding ? ` (${entry.bindingLabel})` : ""}`,
        );
        if (entry.binding) {
          button.setAttribute(
            "aria-keyshortcuts",
            commands.ariaKeyBinding(entry.binding),
          );
        }
        button.addEventListener("click", () => {
          if (button.disabled) return;
          let invocation;
          try {
            invocation = entry.invoke();
          } catch (error) {
            this.handleCommandError(error, this.registry.get(entry.id) || entry);
            return;
          }
          close();
          Promise.resolve(invocation).catch((error) =>
            this.handleCommandError(error, this.registry.get(entry.id) || entry));
        });
        return button;
      }

      mountContextMenu() {
        if (!this.contextScope ||
            typeof this.contextScope.addEventListener !== "function" ||
            typeof this.contextScope.append !== "function") return;
        const menu = element(
          this.documentRef,
          "div",
          "classification-context-menu",
        );
        menu.dataset.classificationContextMenu = "true";
        menu.setAttribute("role", "menu");
        menu.setAttribute("aria-label", "Classification commands");
        menu.hidden = true;
        this.contextScope.append(menu);
        this.contextMenu = menu;

        this.listen(this.contextScope, "contextmenu", (event) => {
          const contextTarget = this.isContextMenuEvent(event);
          if (!contextTarget) return;
          const entries = this.surfaceEntries(
            typeof contextTarget === "object" ? contextTarget : null,
          ).filter(
            (entry) => entry.available === true);
          if (!entries.length) return;
          event.preventDefault();
          this.openContextMenu(event, entries);
        });
        this.listen(this.contextScope, "pointerdown", (event) => {
          if (!this.contextMenu || this.contextMenu.hidden ||
              nodeInside(this.contextMenu, event.target)) return;
          this.closeContextMenu(false);
        });
        this.listen(menu, "keydown", (event) => this.handleContextMenuKeydown(event));
      }

      openContextMenu(event, entries) {
        if (!this.contextMenu) return;
        this.closePalette(false);
        this.contextMenuEntries = Object.freeze([...entries]);
        this.contextMenuReturnFocus = event && event.target || null;
        clearNode(this.contextMenu);
        for (const entry of this.contextMenuEntries) {
          this.contextMenu.append(this.surfaceCommandButton(
            entry,
            "context-menu",
            () => this.closeContextMenu(false),
          ));
        }
        this.contextMenu.hidden = false;
        const viewportWidth = Number(this.windowRef && this.windowRef.innerWidth) || 1280;
        const viewportHeight = Number(this.windowRef && this.windowRef.innerHeight) || 800;
        const menuWidth = Number(this.contextMenu.offsetWidth) || 248;
        const menuHeight = Number(this.contextMenu.offsetHeight) ||
          this.contextMenuEntries.length * 38;
        const rect = event && event.target &&
          typeof event.target.getBoundingClientRect === "function"
          ? event.target.getBoundingClientRect() : null;
        const requestedX = Number(event && event.clientX) ||
          Number(rect && rect.left) || 8;
        const requestedY = Number(event && event.clientY) ||
          Number(rect && rect.bottom) || 8;
        this.contextMenu.style.left = `${Math.max(
          8,
          Math.min(requestedX, Math.max(8, viewportWidth - menuWidth - 8)),
        )}px`;
        this.contextMenu.style.top = `${Math.max(
          8,
          Math.min(requestedY, Math.max(8, viewportHeight - menuHeight - 8)),
        )}px`;
        const first = this.contextMenu.querySelector(
          "[data-surface-command]",
        );
        if (first && typeof first.focus === "function") first.focus();
      }

      handleContextMenuKeydown(event) {
        if (!this.contextMenu || this.contextMenu.hidden) return;
        if (event.key === "Escape") {
          event.preventDefault();
          if (typeof event.stopPropagation === "function") event.stopPropagation();
          this.closeContextMenu(true);
          return;
        }
        const buttons = Array.from(this.contextMenu.querySelectorAll(
          "[data-surface-command]",
        )).filter((button) => !button.disabled);
        if (!buttons.length ||
            !["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
        const active = this.documentRef && this.documentRef.activeElement;
        const current = Math.max(0, buttons.indexOf(active));
        const index = event.key === "Home"
          ? 0
          : event.key === "End"
            ? buttons.length - 1
            : event.key === "ArrowUp"
              ? (current - 1 + buttons.length) % buttons.length
              : (current + 1) % buttons.length;
        event.preventDefault();
        buttons[index].focus();
      }

      closeContextMenu(restoreFocus = false) {
        if (!this.contextMenu) return;
        this.contextMenu.hidden = true;
        clearNode(this.contextMenu);
        this.contextMenuEntries = Object.freeze([]);
        const returnFocus = this.contextMenuReturnFocus;
        this.contextMenuReturnFocus = null;
        if (restoreFocus && returnFocus &&
            typeof returnFocus.focus === "function") returnFocus.focus();
      }

      mountPalette() {
        if (!this.contextScope ||
            typeof this.contextScope.append !== "function" ||
            !this.paletteTrigger) return;
        const palette = element(
          this.documentRef,
          "dialog",
          "classification-command-palette",
        );
        palette.id = `${this.id}-palette`;
        palette.dataset.classificationPalette = "true";
        palette.setAttribute("role", "dialog");
        palette.setAttribute("aria-modal", "true");
        palette.setAttribute("aria-labelledby", `${this.id}-palette-title`);
        palette.hidden = true;

        const surface = element(
          this.documentRef,
          "section",
          "classification-command-palette-surface",
        );
        const header = element(
          this.documentRef,
          "header",
          "classification-command-palette-header",
        );
        const title = element(
          this.documentRef,
          "h2",
          "",
          "Classification command palette",
        );
        title.id = `${this.id}-palette-title`;
        const close = element(
          this.documentRef,
          "button",
          "classification-command-palette-close",
          "Close",
        );
        close.type = "button";
        close.setAttribute("aria-label", "Close command palette");
        header.append(title, close);
        const searchLabel = element(
          this.documentRef,
          "label",
          "classification-command-palette-search-label",
          "Filter commands",
        );
        const search = element(
          this.documentRef,
          "input",
          "classification-command-palette-search",
        );
        search.type = "search";
        search.id = `${this.id}-palette-search`;
        searchLabel.htmlFor = search.id;
        const list = element(
          this.documentRef,
          "div",
          "classification-command-palette-list",
        );
        list.setAttribute("role", "group");
        list.setAttribute("aria-label", "Classification commands");
        surface.append(header, searchLabel, search, list);
        palette.append(surface);
        this.contextScope.append(palette);
        this.palette = palette;
        this.paletteList = list;
        this.paletteSearch = search;

        this.paletteTrigger.setAttribute("aria-haspopup", "dialog");
        this.paletteTrigger.setAttribute("aria-controls", palette.id);
        this.paletteTrigger.setAttribute("aria-expanded", "false");
        this.listen(this.paletteTrigger, "click", () => this.openPalette());
        this.listen(close, "click", () => this.closePalette(true));
        this.listen(search, "input", () => this.filterPalette(search.value));
        this.listen(palette, "keydown", (event) => {
          if (event.key !== "Escape") return;
          event.preventDefault();
          if (typeof event.stopPropagation === "function") event.stopPropagation();
          this.closePalette(true);
        });
        this.listen(palette, "pointerdown", (event) => {
          if (event.target === palette) this.closePalette(true);
        });
        this.listen(palette, "cancel", (event) => {
          event.preventDefault();
          this.closePalette(true);
        });
        this.listen(palette, "close", () => this.finishPaletteClose(false));
      }

      openPalette() {
        if (!this.palette) return;
        this.closeContextMenu(false);
        this.paletteEntries = Object.freeze([...this.surfaceEntries()]);
        clearNode(this.paletteList);
        for (const entry of this.paletteEntries) {
          const button = this.surfaceCommandButton(
            entry,
            "palette",
            () => this.closePalette(false),
          );
          button.dataset.paletteSearch = [
            entry.code,
            entry.label,
            entry.bindingLabel,
          ].join(" ").toLowerCase();
          this.paletteList.append(button);
        }
        this.paletteSearch.value = "";
        this.palette.hidden = false;
        this.paletteTrigger.setAttribute("aria-expanded", "true");
        if (typeof this.palette.showModal === "function" && this.palette.open !== true) {
          try {
            this.palette.showModal();
          } catch (error) {
            this.palette.setAttribute("open", "");
          }
        } else {
          this.palette.setAttribute("open", "");
        }
        if (typeof this.paletteSearch.focus === "function") {
          this.paletteSearch.focus();
        }
      }

      filterPalette(value) {
        if (!this.paletteList) return;
        const query = String(value || "").trim().toLowerCase();
        for (const button of this.paletteList.querySelectorAll(
          "[data-surface-command]",
        )) {
          button.hidden = Boolean(
            query && !String(button.dataset.paletteSearch || "").includes(query),
          );
        }
      }

      finishPaletteClose(restoreFocus) {
        if (!this.palette) return;
        this.palette.hidden = true;
        this.palette.removeAttribute("open");
        this.paletteTrigger.setAttribute("aria-expanded", "false");
        this.paletteEntries = Object.freeze([]);
        clearNode(this.paletteList);
        if (restoreFocus && typeof this.paletteTrigger.focus === "function") {
          this.paletteTrigger.focus();
        }
      }

      closePalette(restoreFocus = false) {
        if (!this.palette || this.palette.hidden) return;
        if (this.palette.open === true && typeof this.palette.close === "function") {
          try {
            this.palette.close();
          } catch (error) {
            this.finishPaletteClose(restoreFocus);
            return;
          }
          this.finishPaletteClose(restoreFocus);
          return;
        }
        this.finishPaletteClose(restoreFocus);
      }

      shortcutEditor() {
        const details = element(
          this.documentRef,
          "details",
          "classification-shortcut-editor",
        );
        details.dataset.shortcutEditor = "true";
        const summary = element(
          this.documentRef,
          "summary",
          "",
          "Keyboard shortcuts",
        );
        const instructions = element(
          this.documentRef,
          "p",
          "classification-shortcut-instructions",
          "Focus a shortcut field and press a key combination. Backspace clears it; Escape cancels.",
        );
        instructions.id = `${this.id}-shortcut-help`;
        const rows = element(
          this.documentRef,
          "div",
          "classification-shortcut-list",
        );
        for (const command of this.definitions()) {
          rows.append(this.shortcutRow(command, instructions.id));
        }
        const resetAll = element(
          this.documentRef,
          "button",
          "classification-reset-all",
          "Reset all shortcuts",
        );
        resetAll.type = "button";
        resetAll.dataset.resetAllShortcuts = "true";
        this.listen(resetAll, "click", () => this.resetAll());
        this.status = element(
          this.documentRef,
          "p",
          "classification-shortcut-status",
        );
        this.status.id = `${this.id}-shortcut-status`;
        this.status.setAttribute("role", "status");
        this.status.setAttribute("aria-live", "polite");
        this.status.setAttribute("aria-atomic", "true");
        details.append(summary, instructions, rows, resetAll, this.status);
        return details;
      }

      shortcutRow(command, helpId) {
        const row = element(
          this.documentRef,
          "div",
          "classification-shortcut-row",
        );
        row.dataset.shortcutCommand = command.id;
        const label = element(
          this.documentRef,
          "label",
          "classification-shortcut-label",
          command.label,
        );
        const input = element(
          this.documentRef,
          "input",
          "classification-shortcut-input",
        );
        input.id = `${this.id}-${command.id.replace(/[^a-z0-9]+/gi, "-")}`;
        input.type = "text";
        input.readOnly = true;
        input.setAttribute("readonly", "");
        input.setAttribute("autocomplete", "off");
        input.setAttribute("spellcheck", "false");
        input.setAttribute("aria-describedby",
          `${helpId} ${this.id}-shortcut-status`);
        input.dataset.shortcutInput = command.id;
        label.htmlFor = input.id;
        const reset = element(
          this.documentRef,
          "button",
          "classification-shortcut-reset",
          "Reset",
        );
        reset.type = "button";
        reset.dataset.resetShortcut = command.id;
        reset.setAttribute("aria-label", `Reset shortcut for ${command.label}`);
        this.listen(input, "focus", () => {
          this.setStatus(`Press a new shortcut for ${command.label}.`);
          if (typeof input.select === "function") input.select();
        });
        this.listen(input, "keydown", (event) =>
          this.captureShortcut(event, command));
        this.listen(reset, "click", () => this.resetCommand(command));
        row.append(label, input, reset);
        this.shortcutInputs.set(command.id, input);
        this.resetButtons.set(command.id, reset);
        return row;
      }

      stopCaptureEvent(event) {
        event.preventDefault();
        if (typeof event.stopPropagation === "function") event.stopPropagation();
      }

      captureShortcut(event, command) {
        if (!event || event.repeat || event.isComposing || event.key === "Tab") return;
        if (event.key === "Escape") {
          this.stopCaptureEvent(event);
          this.refresh();
          this.setStatus(`Shortcut change for ${command.label} cancelled.`);
          return;
        }
        if ((event.key === "Backspace" || event.key === "Delete") &&
            !event.ctrlKey && !event.altKey && !event.shiftKey && !event.metaKey) {
          this.stopCaptureEvent(event);
          this.applyBinding(command, "");
          return;
        }
        const binding = commands.eventKeyBinding(event);
        if (!binding) return;
        this.stopCaptureEvent(event);
        this.applyBinding(command, binding);
      }

      applyBinding(command, binding) {
        try {
          this.registry.remap(command.id, binding);
          this.setStatus(
            binding
              ? `${command.label} now uses ${commands.displayKeyBinding(binding)}.`
              : `${command.label} no longer has a shortcut.`,
          );
        } catch (error) {
          this.handleBindingError(error, command, binding);
        }
      }

      resetCommand(command) {
        try {
          this.registry.resetBinding(command.id);
          this.setStatus(
            `${command.label} reset to ${commands.displayKeyBinding(
              command.defaultBinding)}.`,
          );
        } catch (error) {
          this.handleBindingError(error, command, command.defaultBinding);
        }
      }

      resetAll() {
        const definitions = this.definitions();
        const externalConflicts = definitions.flatMap((command) =>
          this.registry.conflicts(command.defaultBinding, command.id)
            .filter((id) => !CLASSIFICATION_ID_SET.has(id))
            .map((id) => ({ command, conflictingId: id })));
        if (externalConflicts.length) {
          const conflict = externalConflicts[0];
          const occupied = this.registry.get(conflict.conflictingId);
          this.setStatus(
            `${commands.displayKeyBinding(conflict.command.defaultBinding)} is used by ${
              occupied ? occupied.label : conflict.conflictingId}; defaults were not changed.`,
            true,
          );
          return;
        }
        for (const command of definitions) this.registry.remap(command.id, "");
        for (const command of definitions) this.registry.resetBinding(command.id);
        this.setStatus("All classification shortcuts reset to their defaults.");
      }

      handleBindingError(error, command, binding) {
        if (error && error.code === "key_binding_conflict") {
          const occupied = conflictLabels(this.registry, error);
          this.setStatus(
            `${commands.displayKeyBinding(binding)} is already used by ${
              occupied.join(", ") || "another command"}. ${command.label} was not changed.`,
            true,
          );
        } else {
          this.setStatus(
            error && error.message || `The shortcut for ${command.label} could not be changed.`,
            true,
          );
        }
        this.onError(error, command);
        this.refresh();
      }

      handleCommandError(error, command) {
        this.setStatus(
          error && error.message || `${command.label} could not be applied.`,
          true,
        );
        this.onError(error, command);
      }

      setStatus(message, error = false) {
        if (!this.status) return;
        this.status.textContent = String(message || "");
        if (error) this.status.dataset.error = "true";
        else delete this.status.dataset.error;
      }

      bindingsSnapshot() {
        const result = {};
        for (const command of this.definitions()) {
          result[command.id] = this.registry.bindingFor(command.id);
        }
        return Object.freeze(result);
      }

      refresh() {
        for (const command of this.definitions()) {
          const binding = bindingValue(this.registry, command.id);
          const keys = this.commandKeys.get(command.id) || [];
          const input = this.shortcutInputs.get(command.id);
          for (const key of keys) key.textContent = binding;
          if (input) {
            input.value = binding;
            input.setAttribute("aria-label",
              `Shortcut for ${command.label}: ${binding}`);
          }
        }
        for (const binding of this.controlBindings) binding.refresh();
      }

      destroy() {
        if (this.destroyed) return;
        this.destroyed = true;
        if (this.unsubscribeRegistry) this.unsubscribeRegistry();
        if (this.unsubscribeController) this.unsubscribeController();
        for (const binding of this.controlBindings) binding.destroy();
        for (const remove of this.listeners.splice(0)) remove();
        this.closeContextMenu(false);
        this.closePalette(false);
        removeNode(this.contextMenu);
        removeNode(this.palette);
        this.contextMenu = null;
        this.palette = null;
        this.paletteList = null;
        this.paletteSearch = null;
        if (this.toolbarRoot) clearNode(this.toolbarRoot);
        if (this.paletteTrigger) {
          this.paletteTrigger.removeAttribute("aria-controls");
          this.paletteTrigger.removeAttribute("aria-expanded");
          this.paletteTrigger.removeAttribute("aria-haspopup");
        }
        this.controlBindings = [];
        this.commandButtons.clear();
        this.commandKeys.clear();
        this.shortcutInputs.clear();
        this.resetButtons.clear();
        clearNode(this.root);
      }
    }

    function createClassificationControls(options) {
      return new ClassificationControls(options);
    }

    return {
      CLASSIFICATION_CONTROL_COMMAND_IDS: CLASSIFICATION_IDS,
      ClassificationControls,
      createClassificationControls,
    };
  });

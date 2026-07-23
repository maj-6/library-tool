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

      commandButton(command) {
        const button = element(
          this.documentRef,
          "button",
          "classification-command-button",
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
        const key = element(
          this.documentRef,
          "kbd",
          "classification-command-key",
        );
        key.setAttribute("aria-hidden", "true");
        button.append(code, label, key);
        this.commandButtons.set(command.id, button);
        this.commandKeys.set(command.id, key);
        this.controlBindings.push(this.controller.bindControl(
          command.id,
          button,
          { onError: (error) => this.handleCommandError(error, command) },
        ));
        return button;
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
          const key = this.commandKeys.get(command.id);
          const input = this.shortcutInputs.get(command.id);
          if (key) key.textContent = binding;
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

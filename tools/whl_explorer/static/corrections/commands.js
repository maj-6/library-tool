(function installCorrectionsCommands(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else Object.assign(root.LibraryToolCorrections ||= {}, api);
})(typeof globalThis !== "undefined" ? globalThis : this,
  function correctionsCommandsFactory() {
    "use strict";

    const COMMAND_IDS = Object.freeze({
      titlePage: "corrections.category.title-page",
      cover: "corrections.category.cover",
      spine: "corrections.category.spine",
      contentSpecimen: "corrections.category.content-specimen",
      marginalia: "corrections.role.marginalia",
      illustration: "corrections.role.illustration",
      manuscript: "corrections.role.manuscript",
      stamp: "corrections.role.stamp",
      damage: "corrections.role.damage",
      extractRegion: "corrections.region.extract",
      archiveImage: "corrections.image.archive",
    });

    const TARGET_KINDS = Object.freeze({
      IMAGE: "image",
      ANNOTATION: "annotation",
    });

    const DEFAULT_COMMAND_DEFINITIONS = Object.freeze([
      Object.freeze({
        id: COMMAND_IDS.titlePage,
        label: "Mark as title page",
        shortLabel: "Title page",
        code: "T",
        defaultBinding: "t",
        targetKind: TARGET_KINDS.IMAGE,
        action: "category.assign",
        value: "title_page",
      }),
      Object.freeze({
        id: COMMAND_IDS.cover,
        label: "Mark as cover",
        shortLabel: "Cover",
        code: "C",
        defaultBinding: "c",
        targetKind: TARGET_KINDS.IMAGE,
        action: "category.assign",
        value: "cover",
      }),
      Object.freeze({
        id: COMMAND_IDS.spine,
        label: "Mark as spine",
        shortLabel: "Spine",
        code: "S",
        defaultBinding: "s",
        targetKind: TARGET_KINDS.IMAGE,
        action: "category.assign",
        value: "spine",
      }),
      Object.freeze({
        id: COMMAND_IDS.contentSpecimen,
        label: "Mark as content specimen",
        shortLabel: "Content specimen",
        code: "E",
        defaultBinding: "e",
        targetKind: TARGET_KINDS.IMAGE,
        action: "category.assign",
        value: "content_specimen",
      }),
      Object.freeze({
        id: COMMAND_IDS.marginalia,
        label: "Mark region as marginalia",
        shortLabel: "Marginalia",
        code: "MAR",
        defaultBinding: "m",
        targetKind: TARGET_KINDS.ANNOTATION,
        action: "role.assign",
        value: "marginalia",
      }),
      Object.freeze({
        id: COMMAND_IDS.illustration,
        label: "Mark region as illustration",
        shortLabel: "Illustration",
        code: "ILL",
        defaultBinding: "i",
        targetKind: TARGET_KINDS.ANNOTATION,
        action: "role.assign",
        value: "figure",
      }),
      // manuscript = hand-drawn or pencilled annotations, stamp = stamps,
      // bookplates, and ownership marks, damage = foxing, stains, and tears.
      Object.freeze({
        id: COMMAND_IDS.manuscript,
        label: "Mark region as manuscript",
        shortLabel: "Manuscript",
        code: "MS",
        defaultBinding: "n",
        targetKind: TARGET_KINDS.ANNOTATION,
        action: "role.assign",
        value: "manuscript",
      }),
      Object.freeze({
        id: COMMAND_IDS.stamp,
        label: "Mark region as stamp",
        shortLabel: "Stamp",
        code: "STP",
        defaultBinding: "p",
        targetKind: TARGET_KINDS.ANNOTATION,
        action: "role.assign",
        value: "stamp",
      }),
      Object.freeze({
        id: COMMAND_IDS.damage,
        label: "Mark region as damage",
        shortLabel: "Damage",
        code: "DMG",
        defaultBinding: "d",
        targetKind: TARGET_KINDS.ANNOTATION,
        action: "role.assign",
        value: "damage",
      }),
      // Queue a correction transform cropped to the region's bounding quad
      // (with re-OCR), which commits as a new artifact set — the region
      // becomes its own reviewable image without touching the source.
      Object.freeze({
        id: COMMAND_IDS.extractRegion,
        label: "Extract region as artifact",
        shortLabel: "Extract region",
        code: "EXT",
        defaultBinding: "x",
        targetKind: TARGET_KINDS.ANNOTATION,
        action: "region.extract",
        value: "",
        // A hovered region is the crop instruction. Focusing it through the
        // Artifacts tree would replace the capture editor before the transform
        // is even queued, so extraction deliberately keeps the soft target in
        // place while ordinary classification commands still promote theirs.
        preserveView: true,
      }),
      // Archive is a durable metadata assertion, not a delete: the image
      // leaves the capture's working set but stays stored and linked for
      // audit, and the same key restores it (undo also works).
      // The key is ``v`` (archi-v-e) rather than the obvious ``a``: the image
      // editor claims a bare ``a`` for its Image Adjust tool, and that
      // handler sits deeper in the tree and calls stopPropagation, so an
      // ``a`` binding never reaches the keymap while the editor canvas holds
      // focus — the archive would silently switch tools instead.
      Object.freeze({
        id: COMMAND_IDS.archiveImage,
        label: "Archive image (keep for audit)",
        shortLabel: "Archive image",
        code: "ARC",
        defaultBinding: "v",
        targetKind: TARGET_KINDS.IMAGE,
        action: "metadata.archive",
        value: "",
      }),
    ]);

    // Padding keeps a sliver of context around the region for the OCR pass;
    // the minimum extent keeps the quad above the engine's edge-length and
    // area floors even for degenerate hairline regions.
    const REGION_EXTRACT_PADDING = 0.02;
    const REGION_EXTRACT_MIN_EXTENT = 0.004;

    // Archiving asserts both names together, so restoring has to clear both;
    // clearing only ``archived`` leaves ``archived_at`` behind as a stale
    // orphan assertion claiming a restored image was archived.
    const ARCHIVE_ASSERTION_NAMES = Object.freeze(["archived", "archived_at"]);

    const PORTABLE_ID = /^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,255}$/;
    const MODIFIER_ORDER = Object.freeze(["ctrl", "alt", "shift", "meta"]);
    const MODIFIER_LABELS = Object.freeze({
      ctrl: "Ctrl",
      alt: "Alt",
      shift: "Shift",
      meta: "Meta",
    });

    class CorrectionCommandError extends Error {
      constructor(message, code = "correction_command_unavailable", details = null) {
        super(message);
        this.name = "CorrectionCommandError";
        this.code = code;
        if (details) this.details = details;
      }
    }

    class KeyBindingConflictError extends CorrectionCommandError {
      constructor(binding, commandIds) {
        super(
          `${displayKeyBinding(binding)} is already assigned to ${commandIds.join(", ")}`,
          "key_binding_conflict",
          Object.freeze({
            binding,
            commandIds: Object.freeze([...commandIds]),
          }),
        );
        this.name = "KeyBindingConflictError";
      }
    }

    function text(value, maximum = 512) {
      return value == null ? "" : String(value)
        .replace(/[\u0000-\u001f\u007f]/g, "")
        .trim()
        .slice(0, maximum);
    }

    function read(value, ...names) {
      if (!value || typeof value !== "object") return undefined;
      for (const name of names) {
        if (value[name] != null && value[name] !== "") return value[name];
      }
      return undefined;
    }

    function valueOf(value, ...names) {
      const candidate = read(value, ...names);
      return typeof candidate === "function" ? candidate.call(value) : candidate;
    }

    function identifier(value, name, required = true) {
      const normalized = text(value, 256);
      if (!normalized && !required) return "";
      if (!PORTABLE_ID.test(normalized)) {
        throw new CorrectionCommandError(
          `${name} is required for this correction`,
          "invalid_command_target",
          { field: name },
        );
      }
      return normalized;
    }

    function normalizeKeyName(value) {
      let key = text(value, 64).toLowerCase();
      const aliases = {
        control: "ctrl",
        cmd: "meta",
        command: "meta",
        option: "alt",
        esc: "escape",
        " ": "space",
        spacebar: "space",
      };
      key = aliases[key] || key;
      if (/^key[a-z]$/.test(key)) key = key.slice(3);
      if (/^digit[0-9]$/.test(key)) key = key.slice(5);
      return key;
    }

    function normalizeKeyBinding(value) {
      if (value == null || value === "") return "";
      const raw = String(value).split("+").map(normalizeKeyName).filter(Boolean);
      const modifiers = new Set(raw.filter((part) => MODIFIER_ORDER.includes(part)));
      const keys = raw.filter((part) => !MODIFIER_ORDER.includes(part));
      if (keys.length !== 1 || !/^(?:[a-z0-9]|backspace|escape|space|enter|f(?:[1-9]|1[0-2]))$/
        .test(keys[0])) {
        throw new CorrectionCommandError(
          "A shortcut must contain one portable key",
          "invalid_key_binding",
        );
      }
      return [
        ...MODIFIER_ORDER.filter((modifier) => modifiers.has(modifier)),
        keys[0],
      ].join("+");
    }

    function eventKeyBinding(event) {
      if (!event || event.isComposing) return "";
      const key = normalizeKeyName(event.key || event.code);
      if (!key || ["ctrl", "alt", "shift", "meta", "process", "dead"].includes(key)) {
        return "";
      }
      try {
        return normalizeKeyBinding([
          event.ctrlKey ? "ctrl" : "",
          event.altKey ? "alt" : "",
          event.shiftKey ? "shift" : "",
          event.metaKey ? "meta" : "",
          key,
        ].filter(Boolean).join("+"));
      } catch (error) {
        return "";
      }
    }

    function displayKeyBinding(value) {
      const normalized = normalizeKeyBinding(value);
      if (!normalized) return "Unassigned";
      return normalized.split("+").map((part) => MODIFIER_LABELS[part] ||
        (part.length === 1 ? part.toUpperCase() :
          part.slice(0, 1).toUpperCase() + part.slice(1))).join("+");
    }

    function ariaKeyBinding(value) {
      const normalized = normalizeKeyBinding(value);
      if (!normalized) return "";
      const labels = {
        ctrl: "Control", alt: "Alt", shift: "Shift", meta: "Meta",
        backspace: "Backspace", escape: "Escape", space: "Space", enter: "Enter",
      };
      return normalized.split("+").map((part) => labels[part] ||
        (part.length === 1 ? part.toUpperCase() : part)).join("+");
    }

    function normalizeDefinition(value) {
      if (!value || typeof value !== "object") {
        throw new TypeError("command definition must be an object");
      }
      const id = identifier(value.id, "command id");
      const label = text(value.label, 256);
      if (!label || typeof value.execute !== "function") {
        throw new TypeError("command definition requires a label and execute function");
      }
      return Object.freeze({
        ...value,
        id,
        label,
        shortLabel: text(value.shortLabel || label, 128),
        code: text(value.code, 16),
        targetKind: text(value.targetKind, 32),
        defaultBinding: normalizeKeyBinding(value.defaultBinding),
        execute: value.execute,
        available: typeof value.available === "function" ? value.available : () => true,
      });
    }

    class CorrectionCommandRegistry {
      constructor(options = {}) {
        this.commands = new Map();
        this.bindings = new Map();
        this.listeners = new Set();
        this.initialBindings = options.bindings && typeof options.bindings === "object"
          ? { ...options.bindings } : {};
      }

      register(definition) {
        const normalized = normalizeDefinition(definition);
        if (this.commands.has(normalized.id)) {
          throw new CorrectionCommandError(
            `Command ${normalized.id} is already registered`,
            "duplicate_command",
          );
        }
        const supplied = Object.hasOwn(this.initialBindings, normalized.id)
          ? this.initialBindings[normalized.id] : normalized.defaultBinding;
        const binding = normalizeKeyBinding(supplied);
        const conflicts = this.conflicts(binding);
        if (binding && conflicts.length) {
          throw new KeyBindingConflictError(binding, conflicts);
        }
        this.commands.set(normalized.id, normalized);
        this.bindings.set(normalized.id, binding);
        this.emit({ type: "registered", commandId: normalized.id });
        return normalized;
      }

      registerMany(definitions) {
        return definitions.map((definition) => this.register(definition));
      }

      unregister(commandId) {
        const id = String(commandId || "");
        const removed = this.commands.delete(id);
        this.bindings.delete(id);
        if (removed) this.emit({ type: "unregistered", commandId: id });
        return removed;
      }

      get(commandId) {
        return this.commands.get(String(commandId || "")) || null;
      }

      list(options = {}) {
        return Object.freeze(Array.from(this.commands.values())
          .filter((command) => !options.targetKind ||
            command.targetKind === options.targetKind)
          .map((command) => Object.freeze({
            ...command,
            binding: this.bindingFor(command.id),
            bindingLabel: displayKeyBinding(this.bindingFor(command.id)),
          })));
      }

      bindingFor(commandId) {
        return this.bindings.get(String(commandId || "")) || "";
      }

      conflicts(binding, excludingCommandId = "") {
        const normalized = normalizeKeyBinding(binding);
        if (!normalized) return Object.freeze([]);
        return Object.freeze(Array.from(this.bindings)
          .filter(([id, candidate]) => id !== excludingCommandId &&
            candidate === normalized)
          .map(([id]) => id));
      }

      remap(commandId, binding, options = {}) {
        const id = String(commandId || "");
        if (!this.commands.has(id)) {
          throw new CorrectionCommandError(`Unknown command ${id}`, "unknown_command");
        }
        const normalized = normalizeKeyBinding(binding);
        const conflicts = this.conflicts(normalized, id);
        if (conflicts.length && options.replaceConflicts !== true) {
          throw new KeyBindingConflictError(normalized, conflicts);
        }
        if (conflicts.length) {
          for (const conflict of conflicts) this.bindings.set(conflict, "");
        }
        this.bindings.set(id, normalized);
        this.emit({
          type: "remapped",
          commandId: id,
          binding: normalized,
          unbound: conflicts,
        });
        return normalized;
      }

      resetBinding(commandId, options = {}) {
        const command = this.get(commandId);
        if (!command) {
          throw new CorrectionCommandError(
            `Unknown command ${commandId}`,
            "unknown_command",
          );
        }
        return this.remap(command.id, command.defaultBinding, options);
      }

      commandForBinding(binding) {
        const normalized = normalizeKeyBinding(binding);
        if (!normalized) return null;
        const match = Array.from(this.bindings)
          .find(([, candidate]) => candidate === normalized);
        return match ? this.get(match[0]) : null;
      }

      canInvoke(commandId, context = {}) {
        const command = this.get(commandId);
        if (!command) return false;
        try {
          return command.available(context, command) !== false;
        } catch (error) {
          return false;
        }
      }

      invoke(commandId, context = {}) {
        const command = this.get(commandId);
        if (!command) {
          return Promise.reject(new CorrectionCommandError(
            `Unknown command ${commandId}`,
            "unknown_command",
          ));
        }
        if (!this.canInvoke(command.id, context)) {
          return Promise.reject(new CorrectionCommandError(
            `${command.label} is not available for the current target`,
            "command_unavailable",
          ));
        }
        return Promise.resolve(command.execute(context, command));
      }

      subscribe(listener) {
        if (typeof listener !== "function") throw new TypeError("listener is required");
        this.listeners.add(listener);
        return () => this.listeners.delete(listener);
      }

      emit(change) {
        const frozen = Object.freeze({ ...change });
        for (const listener of [...this.listeners]) listener(frozen);
      }
    }

    function targetObjectType(target) {
      return text(read(target, "objectType", "object_type", "type"), 64)
        .toLowerCase();
    }

    function targetKey(target) {
      return text(read(target, "key"), 520);
    }

    function imageTarget(target) {
      if (!target || typeof target !== "object") return false;
      const objectType = targetObjectType(target);
      if (objectType.includes("annotation") || targetKey(target).startsWith("annotation:")) {
        return false;
      }
      const family = text(read(target, "family"), 32).toLowerCase();
      const group = text(read(target, "group"), 64).toLowerCase();
      const kind = text(read(target, "kind"), 64).toLowerCase();
      return family === "image" || [
        "source-images", "extracted-figures", "processed-images", "generated-images",
      ].includes(group) || [
        "capture", "captured-image", "page-image", "scan", "source-image",
        "figure", "illustration", "extracted-figure", "corrected-image",
        "processed-image", "generated-image", "reworked-image",
      ].includes(kind);
    }

    function annotationTarget(target) {
      if (!target || typeof target !== "object") return false;
      const objectType = targetObjectType(target);
      const key = targetKey(target);
      return objectType.includes("annotation") || objectType === "region" ||
        key.startsWith("annotation:") ||
        ["annotation", "mistral-box", "region", "spatial-annotation"]
          .includes(text(read(target, "kind"), 64).toLowerCase());
    }

    function committedMaskTarget(target) {
      if (!target || typeof target !== "object") return false;
      return targetObjectType(target) === "committed-mask" ||
        text(read(target, "kind"), 64).toLowerCase() === "committed-mask";
    }

    function regionSelectorPoints(target) {
      const selector = read(target, "selector");
      const points = (selector && typeof selector === "object" &&
        (selector.points || selector.polygon)) ||
        read(target, "points", "polygon");
      if (!Array.isArray(points) || points.length < 3) return null;
      const result = [];
      for (const point of points) {
        const x = Number(Array.isArray(point)
          ? point[0] : point && (point.x !== undefined ? point.x : NaN));
        const y = Number(Array.isArray(point)
          ? point[1] : point && (point.y !== undefined ? point.y : NaN));
        if (!Number.isFinite(x) || !Number.isFinite(y)) return null;
        result.push([
          Math.min(1, Math.max(0, x)),
          Math.min(1, Math.max(0, y)),
        ]);
      }
      return result;
    }

    function regionExtractQuad(target) {
      const points = regionSelectorPoints(target);
      if (!points) return null;
      let minX = 1;
      let minY = 1;
      let maxX = 0;
      let maxY = 0;
      for (const [x, y] of points) {
        minX = Math.min(minX, x);
        minY = Math.min(minY, y);
        maxX = Math.max(maxX, x);
        maxY = Math.max(maxY, y);
      }
      minX = Math.max(0, minX - REGION_EXTRACT_PADDING);
      minY = Math.max(0, minY - REGION_EXTRACT_PADDING);
      maxX = Math.min(1, maxX + REGION_EXTRACT_PADDING);
      maxY = Math.min(1, maxY + REGION_EXTRACT_PADDING);
      if (maxX - minX < REGION_EXTRACT_MIN_EXTENT) {
        const center = (minX + maxX) / 2;
        minX = Math.max(0, Math.min(
          center - REGION_EXTRACT_MIN_EXTENT / 2,
          1 - REGION_EXTRACT_MIN_EXTENT));
        maxX = minX + REGION_EXTRACT_MIN_EXTENT;
      }
      if (maxY - minY < REGION_EXTRACT_MIN_EXTENT) {
        const center = (minY + maxY) / 2;
        minY = Math.max(0, Math.min(
          center - REGION_EXTRACT_MIN_EXTENT / 2,
          1 - REGION_EXTRACT_MIN_EXTENT));
        maxY = minY + REGION_EXTRACT_MIN_EXTENT;
      }
      return [
        [minX, minY],
        [maxX, minY],
        [maxX, maxY],
        [minX, maxY],
      ];
    }

    function commandRevision(target) {
      return text(read(
        target,
        "revision", "artifactRevision", "artifact_revision",
        "annotationRevision", "annotation_revision",
      ), 512);
    }

    // Only a target that carries metadata assertions can answer this. Lean
    // navigation targets — the Books capture rows, which are the normal way
    // into the archive toggle — carry none, so the host tops them up from the
    // artifacts feature before publishing them (see the shell's
    // ``classificationTargetMetadata``). Without that, an already-archived
    // image would look unarchived here and the documented "same key restores
    // it" toggle would only ever re-assert ``archived``.
    function targetArchived(target) {
      const assertions = read(target, "metadataAssertions", "metadata_assertions");
      if (!Array.isArray(assertions)) return false;
      return assertions.some((assertion) =>
        assertion && typeof assertion === "object" &&
        assertion.name === "archived" && Boolean(assertion.value));
    }

    function targetAccepted(target, command) {
      const revision = commandRevision(target);
      if (!revision || revision.startsWith("index:")) return false;
      if (command.action === "region.extract" && committedMaskTarget(target)) {
        return regionSelectorPoints(target) !== null;
      }
      return command.targetKind === TARGET_KINDS.IMAGE
        ? imageTarget(target) : annotationTarget(target);
    }

    function candidateValue(context, ...names) {
      for (const name of names) {
        const candidate = valueOf(context, name);
        if (candidate) return candidate;
      }
      return null;
    }

    function resolveClassificationTarget(context, command) {
      const focused = candidateValue(context, "focusedTarget");
      const selected = candidateValue(context, "selectionTarget", "selectedTarget");
      const soft = candidateValue(context, "softTarget", "hotTarget");
      const extraction = command.action === "region.extract"
        ? candidateValue(context, "extractionTarget") : null;
      // A hovered target outranks the selection so hover-hotkeys act on the
      // box under the pointer; kind acceptance below keeps incompatible
      // commands on the focused or selected target instead.
      const candidates = [
        ["soft", soft],
        ["extraction", extraction],
        ["focused", focused],
        ["selection", selected],
      ];
      const seen = new Set();
      for (const [source, target] of candidates) {
        if (!target || seen.has(target) || !targetAccepted(target, command)) continue;
        seen.add(target);
        return Object.freeze({ target, source });
      }
      return null;
    }

    function classificationTargetName(target, command = null) {
      const label = text(read(
        target,
        "commandTargetName", "ariaLabel", "label", "title", "name",
      ), 512);
      const rawId = read(
        target,
        "id", "artifactId", "artifact_id", "annotationId", "annotation_id",
      );
      const id = text(rawId || targetKey(target).split(":").slice(1).join(":"), 256);
      const kind = command && command.targetKind === TARGET_KINDS.ANNOTATION
        ? "region" : "image";
      return label ? `${label} (${kind})` : id ? `${kind} ${id}` : "";
    }

    function targetIdentifiers(target, targetKind) {
      const key = targetKey(target);
      const keyId = key.includes(":") ? key.split(":").slice(1).join(":") : "";
      const itemId = identifier(read(target, "itemId", "item_id"), "item id");
      const revision = commandRevision(target);
      if (!revision || revision.startsWith("index:")) {
        throw new CorrectionCommandError(
          "The selected target has no authoritative revision pin",
          "target_revision_required",
        );
      }
      const id = targetKind === TARGET_KINDS.ANNOTATION
        ? identifier(read(target, "annotationId", "annotation_id", "id") || keyId,
          "annotation id")
        : identifier(read(target, "artifactId", "artifact_id", "id") || keyId,
          "artifact id");
      return Object.freeze({ itemId, id, revision, key: key || `${targetKind}:${id}` });
    }

    function linkedArtifactKeys(target) {
      const values = [];
      const keys = read(target, "linkedKeys", "linked_keys");
      if (Array.isArray(keys)) values.push(...keys);
      const artifactIds = read(
        target,
        "linkedArtifactIds", "linked_artifact_ids",
      );
      if (Array.isArray(artifactIds)) values.push(...artifactIds);
      const artifactId = read(
        target,
        "linkedArtifactId", "linked_artifact_id",
      );
      if (artifactId != null) values.push(artifactId);

      const result = [];
      const seen = new Set();
      for (const value of values) {
        const raw = text(value, 520);
        const key = raw.startsWith("artifact:") ? raw : `artifact:${raw}`;
        const id = key.slice("artifact:".length);
        if (!PORTABLE_ID.test(id) || seen.has(key)) continue;
        seen.add(key);
        result.push(key);
      }
      return Object.freeze(result);
    }

    function roleSourceArtifactKeys(target) {
      const linked = linkedArtifactKeys(target);
      const extensions = read(target, "extensions");
      const extraction = extensions && typeof extensions === "object"
        ? extensions.correction_extraction : null;
      const artifactIds = extraction && Array.isArray(extraction.artifact_ids)
        ? extraction.artifact_ids : null;
      if (!artifactIds || artifactIds.some((value) =>
        typeof value !== "string" || !PORTABLE_ID.test(value))) {
        // Fail closed: malformed extraction metadata cannot suppress a real
        // source/foreign-link ambiguity.
        return linked;
      }
      const extracted = new Set(artifactIds.map((value) => `artifact:${value}`));
      return Object.freeze(linked.filter((key) => !extracted.has(key)));
    }

    function normalizeLinkedArtifact(value, itemId, expectedKey = "") {
      if (!value || typeof value !== "object") return null;
      const rawKey = targetKey(value);
      const keyId = rawKey.startsWith("artifact:")
        ? rawKey.slice("artifact:".length) : "";
      const id = identifier(
        read(value, "artifactId", "artifact_id", "id") || keyId,
        "linked artifact id",
      );
      if (expectedKey && expectedKey !== `artifact:${id}`) {
        throw new CorrectionCommandError(
          "The resolved linked artifact does not match the annotation link",
          "linked_target_mismatch",
        );
      }
      const revision = text(read(
        value,
        "revision", "artifactRevision", "artifact_revision",
      ), 512);
      if (!revision || revision.startsWith("index:")) {
        throw new CorrectionCommandError(
          "The linked artifact has no authoritative revision pin",
          "linked_artifact_revision_required",
        );
      }
      const linkedItemId = text(read(value, "itemId", "item_id"), 256);
      if (linkedItemId && linkedItemId !== itemId) {
        throw new CorrectionCommandError(
          "The annotation and linked artifact belong to different books",
          "linked_target_scope_mismatch",
        );
      }
      return Object.freeze({ id, revision });
    }

    function operationId(factory, prefix, command, target) {
      const supplied = factory(prefix, command, target);
      return identifier(supplied, "operation id");
    }

    let defaultOperationCounter = 0;
    function defaultOperationId(prefix = "classify") {
      const cryptoRef = typeof globalThis !== "undefined" && globalThis.crypto;
      if (cryptoRef && typeof cryptoRef.randomUUID === "function") {
        return `${prefix}-${cryptoRef.randomUUID()}`;
      }
      defaultOperationCounter += 1;
      return `${prefix}-${Date.now().toString(36)}-${Math.random()
        .toString(36).slice(2)}-${defaultOperationCounter.toString(36)}`;
    }

    function conflictError(error) {
      if (!error) return false;
      const code = String(error.code || "").toLowerCase();
      return Number(error.status) === 409 ||
        code.includes("conflict") || code.includes("stale");
    }

    function receiptFromResult(result) {
      return result && typeof result === "object" &&
        result.receipt && typeof result.receipt === "object"
        ? result.receipt : null;
    }

    function refreshTarget(kind, itemId, id, revision = "", hint = null) {
      const key = `${kind}:${id}`;
      const group = text(read(hint, "group"), 64).toLowerCase();
      const artifactKind = text(read(hint, "kind"), 64).toLowerCase();
      if (kind === "artifact") {
        return Object.freeze({
          key,
          objectType: "raster-artifact",
          family: "image",
          ...(group ? { group } : {}),
          ...(artifactKind ? { kind: artifactKind } : {}),
          itemId,
          id,
          artifactId: id,
          revision,
        });
      }
      return Object.freeze({
        key,
        objectType: "spatial-annotation",
        kind: "spatial-annotation",
        itemId,
        id,
        annotationId: id,
        revision,
      });
    }

    function refreshTargetsFromRevisions(values, itemId, hints = []) {
      if (!Array.isArray(values)) return Object.freeze([]);
      const targets = [];
      const seen = new Set();
      const hintsByKey = new Map((hints || []).map((value) =>
        [targetKey(value), value]));
      for (const value of values) {
        if (!value || typeof value !== "object") return Object.freeze([]);
        const kind = text(read(value, "kind"), 32).toLowerCase();
        const id = text(read(value, "target_id", "targetId"), 256);
        if (!["artifact", "annotation"].includes(kind) ||
            !PORTABLE_ID.test(id)) return Object.freeze([]);
        const key = `${kind}:${id}`;
        if (seen.has(key)) return Object.freeze([]);
        seen.add(key);
        targets.push(refreshTarget(
          kind,
          itemId,
          id,
          text(read(
            value,
            "after_revision", "afterRevision",
            "before_revision", "beforeRevision",
          ), 512),
          hintsByKey.get(key),
        ));
      }
      return Object.freeze(targets);
    }

    function uniqueRefreshTargets(values) {
      const result = [];
      const seen = new Set();
      for (const value of values || []) {
        const key = targetKey(value);
        if (!key || seen.has(key)) continue;
        seen.add(key);
        result.push(value);
      }
      return Object.freeze(result);
    }

    async function refreshClassificationTargets(context, targets, detail) {
      if (typeof context.refreshTarget !== "function") {
        return Object.freeze({
          attempted: false,
          errors: Object.freeze([]),
        });
      }
      const errors = [];
      for (const target of uniqueRefreshTargets(targets)) {
        try {
          await context.refreshTarget(target, Object.freeze({
            ...detail,
            refreshTarget: target,
          }));
        } catch (error) {
          errors.push(error);
        }
      }
      return Object.freeze({
        attempted: true,
        errors: Object.freeze(errors),
      });
    }

    function attachRefreshErrors(error, refresh) {
      if (!error || !refresh || !refresh.errors.length) return;
      try {
        error.refreshError = refresh.errors[0];
        error.refreshErrors = refresh.errors;
      } catch (ignored) {
        // A frozen transport error must still be rethrown as the conflict.
      }
    }

    async function invokePort(port, names, command, payload) {
      for (const name of names) {
        if (port && typeof port[name] === "function") return port[name](payload);
      }
      if (port && typeof port.invokeClassificationCommand === "function") {
        return port.invokeClassificationCommand(command.action, payload);
      }
      if (port && typeof port.invoke === "function") {
        return port.invoke(command.id, { command: payload, target: payload });
      }
      throw new CorrectionCommandError(
        "Classification commands are not available",
        "capability-unavailable",
      );
    }

    function portSupportsCommand(port, command) {
      if (!port || !command) return false;
      if (command.action === "region.extract") {
        // Extraction rides the transform queue, not the classification
        // endpoints, so the generic invoke fallbacks do not cover it.
        return typeof port.queueTransform === "function";
      }
      if (command.action === "metadata.archive") {
        return typeof port.assertArtifactMetadata === "function";
      }
      if (typeof port.invokeClassificationCommand === "function" ||
          typeof port.invoke === "function") return true;
      if (command.targetKind === TARGET_KINDS.IMAGE) {
        return typeof port.assignImageCategory === "function" ||
          typeof port.assignCategory === "function";
      }
      if (command.targetKind === TARGET_KINDS.ANNOTATION) {
        return typeof port.assignRegionRole === "function";
      }
      return false;
    }

    class ClassificationCommandExecutor {
      constructor(options = {}) {
        this.port = options.port || options.commands || null;
        this.history = options.history || null;
        this.operationIdFactory = typeof options.operationIdFactory === "function"
          ? options.operationIdFactory : defaultOperationId;
        this.onChanged = typeof options.onChanged === "function"
          ? options.onChanged : () => {};
        this.onConflict = typeof options.onConflict === "function"
          ? options.onConflict : () => {};
        this.onStatus = typeof options.onStatus === "function"
          ? options.onStatus : () => {};
        this.undoStack = [];
        this.busy = new Set();
      }

      available(context, command) {
        if (!portSupportsCommand(this.port, command)) return false;
        const resolved = resolveClassificationTarget(context, command);
        return !!resolved;
      }

      async resolveLinkedArtifact(target, context, identifiers, command) {
        const keys = roleSourceArtifactKeys(target);
        if (keys.length > 1) {
          throw new CorrectionCommandError(
            "This region has more than one linked image; select one in Properties",
            "linked_target_ambiguous",
            { linkedKeys: keys },
          );
        }
        let value = read(target, "linkedArtifact", "linked_artifact");
        if (!value && typeof context.resolveLinkedArtifact === "function") {
          value = await context.resolveLinkedArtifact(target, {
            command,
            linkedKey: keys[0] || "",
          });
        }
        if (!value && keys.length) {
          throw new CorrectionCommandError(
            "Load the linked extracted image before assigning this region",
            "linked_artifact_revision_required",
            { linkedKey: keys[0] },
          );
        }
        return value
          ? normalizeLinkedArtifact(value, identifiers.itemId, keys[0] || "")
          : null;
      }

      async targetBeforeMutation(resolved, context, command) {
        let target = resolved.target;
        let source = resolved.source;
        if (source === "soft" && command.preserveView !== true &&
            typeof context.promoteSoftTarget === "function") {
          const promoted = await context.promoteSoftTarget(target, command);
          if (promoted === false || promoted == null) {
            throw new CorrectionCommandError(
              "The hovered target could not be focused",
              "soft_target_not_promoted",
            );
          }
          if (promoted && typeof promoted === "object") target = promoted;
          source = "focused";
        }
        const name = classificationTargetName(target, command);
        if (!name) {
          throw new CorrectionCommandError(
            "The command target has no accessible name",
            "unnamed_command_target",
          );
        }
        if (typeof context.announceTarget === "function") {
          await context.announceTarget(Object.freeze({
            command,
            target,
            name,
            source,
          }));
        }
        return Object.freeze({ target, name, source });
      }

      async execute(context, command) {
        const initial = resolveClassificationTarget(context, command);
        if (!initial) {
          throw new CorrectionCommandError(
            `${command.label} needs a compatible image or region`,
            "command_target_required",
          );
        }
        const resolved = await this.targetBeforeMutation(initial, context, command);
        const identifiers = targetIdentifiers(resolved.target, command.targetKind);
        const busyKey = `${command.id}:${identifiers.key}`;
        if (this.busy.has(busyKey)) {
          throw new CorrectionCommandError(
            "That correction is already being applied",
            "command_busy",
          );
        }
        this.busy.add(busyKey);
        const signal = context.signal;
        const primaryKind = command.targetKind === TARGET_KINDS.ANNOTATION
          ? "annotation" : "artifact";
        let mutationTargets = Object.freeze([
          refreshTarget(
            primaryKind,
            identifiers.itemId,
            identifiers.id,
            identifiers.revision,
            resolved.target,
          ),
        ]);
        let archiveRestored = false;
        let result;
        try {
          if (command.action === "region.extract") {
            result = await this.executeRegionExtract(
              context, command, resolved, identifiers, signal);
          } else if (command.action === "metadata.archive") {
            archiveRestored = targetArchived(resolved.target);
            const payload = Object.freeze({
              itemId: identifiers.itemId,
              artifactId: identifiers.id,
              expectedArtifactRevision: identifiers.revision,
              ...(archiveRestored
                ? { assertions: {}, clearNames: [...ARCHIVE_ASSERTION_NAMES] }
                : {
                  assertions: {
                    archived: true,
                    archived_at: new Date().toISOString(),
                  },
                  clearNames: [],
                }),
              operationId: operationId(
                this.operationIdFactory, "archive", command, resolved.target),
              signal,
            });
            result = await invokePort(
              this.port,
              ["assertArtifactMetadata"],
              command,
              payload,
            );
          } else if (command.targetKind === TARGET_KINDS.IMAGE) {
            const payload = Object.freeze({
              itemId: identifiers.itemId,
              artifactId: identifiers.id,
              expectedArtifactRevision: identifiers.revision,
              category: command.value,
              operationId: operationId(
                this.operationIdFactory, "category", command, resolved.target),
              signal,
            });
            result = await invokePort(
              this.port,
              ["assignImageCategory", "assignCategory"],
              command,
              payload,
            );
          } else {
            const linkedKeys = roleSourceArtifactKeys(resolved.target);
            if (linkedKeys.length === 1) {
              mutationTargets = Object.freeze([
                ...mutationTargets,
                refreshTarget(
                  "artifact",
                  identifiers.itemId,
                  linkedKeys[0].slice("artifact:".length),
                ),
              ]);
            }
            const linked = await this.resolveLinkedArtifact(
              resolved.target, context, identifiers, command);
            const payload = {
              itemId: identifiers.itemId,
              annotationId: identifiers.id,
              expectedAnnotationRevision: identifiers.revision,
              role: command.value,
              operationId: operationId(
                this.operationIdFactory, "role", command, resolved.target),
              signal,
            };
            if (linked) {
              payload.linkedArtifactId = linked.id;
              payload.expectedLinkedArtifactRevision = linked.revision;
              mutationTargets = Object.freeze([
                ...mutationTargets.filter((target) =>
                  target.key !== `artifact:${linked.id}`),
                refreshTarget(
                  "artifact",
                  identifiers.itemId,
                  linked.id,
                  linked.revision,
                  linked,
                ),
              ]);
            }
            result = await invokePort(
              this.port,
              ["assignRegionRole"],
              command,
              Object.freeze(payload),
            );
          }
        } catch (error) {
          if (conflictError(error)) {
            const refresh = await refreshClassificationTargets(
              context,
              mutationTargets,
              { command, error, reason: "conflict" },
            );
            attachRefreshErrors(error, refresh);
            await this.onConflict(error, Object.freeze({
              command,
              target: resolved.target,
              targets: mutationTargets,
              name: resolved.name,
              refreshAttempted: refresh.attempted,
              refreshErrors: refresh.errors,
            }));
          }
          throw error;
        } finally {
          this.busy.delete(busyKey);
        }

        const receipt = receiptFromResult(result);
        const receiptTargets = refreshTargetsFromRevisions(
          receipt && receipt.targets,
          identifiers.itemId,
          mutationTargets,
        );
        const changedTargets = receiptTargets.length
          ? receiptTargets : mutationTargets;
        const refresh = await refreshClassificationTargets(
          context,
          changedTargets,
          { command, receipt, reason: "committed" },
        );
        if (receipt && receipt.inverse) {
          const undo = Object.freeze({
            commandId: command.id,
            label: command.label,
            targetName: resolved.name,
            itemId: identifiers.itemId,
            inverse: receipt.inverse,
            refreshTargets: changedTargets,
          });
          this.undoStack.push(undo);
          if (this.history && typeof this.history.push === "function") {
            this.history.push(undo);
          }
        }
        await this.onChanged(result, Object.freeze({
          command,
          target: resolved.target,
          targets: changedTargets,
          name: resolved.name,
          refreshAttempted: refresh.attempted,
          refreshErrors: refresh.errors,
        }));
        this.onStatus(
          command.action === "region.extract"
            ? `Region extraction queued for ${resolved.name}`
            : command.action === "metadata.archive"
              ? archiveRestored
                ? `${resolved.name} restored to the capture set`
                : `${resolved.name} archived — same key or undo restores it`
              : `${command.shortLabel} assigned to ${resolved.name}`,
          false,
        );
        return result;
      }

      async executeRegionExtract(context, command, resolved, identifiers, signal) {
        const quad = regionExtractQuad(resolved.target);
        if (!quad) {
          throw new CorrectionCommandError(
            "This region has no polygon to extract",
            "region_polygon_required",
          );
        }
        const contract = typeof context.transformContract === "function"
          ? await context.transformContract(resolved.target)
          : null;
        if (!contract) {
          throw new CorrectionCommandError(
            "Open the region's image in the editor before extracting",
            "transform_source_required",
          );
        }
        if (typeof context.serializeTransformCommand !== "function") {
          throw new CorrectionCommandError(
            "Region extraction is not available",
            "capability-unavailable",
          );
        }
        let wire;
        try {
          const committedMask = committedMaskTarget(resolved.target);
          wire = context.serializeTransformCommand({
            pins: contract,
            quad,
            adjustment: null,
            // The extracted crop exists to be read: always ask the engine
            // for a machine OCR proposal alongside the new artifact.
            rerunOcr: true,
            operationId: operationId(
              this.operationIdFactory, "extract", command, resolved.target),
            ...(committedMask ? { maskPolygon: regionSelectorPoints(resolved.target) } : {}),
            extraction: committedMask
              ? {
                  source_kind: "committed-mask",
                  label: text(read(
                    resolved.target, "label", "name", "caption", "title",
                  ), 512) || "Extracted user mask",
                }
              : {
                  annotation_id: identifiers.id,
                  annotation_revision: identifiers.revision,
                  label: text(read(
                    resolved.target, "label", "name", "caption", "title",
                  ), 512) || `Extracted region ${identifiers.id}`,
                },
          });
        } catch (error) {
          throw new CorrectionCommandError(
            error && error.message || "The extraction request is invalid",
            error && error.code || "region_extract_invalid",
          );
        }
        if (!wire) {
          throw new CorrectionCommandError(
            "Region extraction is not available",
            "capability-unavailable",
          );
        }
        return invokePort(this.port, ["queueTransform"], command, {
          command: wire,
          signal,
        });
      }

      async undoLast(context = {}) {
        const entry = this.undoStack[this.undoStack.length - 1];
        if (!entry) {
          throw new CorrectionCommandError(
            "There is no classification change to undo",
            "undo_unavailable",
          );
        }
        const expectedTargets = refreshTargetsFromRevisions(
          entry.inverse && entry.inverse.expected_targets,
          entry.itemId,
          entry.refreshTargets,
        );
        const fallbackTargets = expectedTargets.length
          ? expectedTargets : entry.refreshTargets || Object.freeze([]);
        const payload = Object.freeze({
          itemId: entry.itemId,
          inverse: entry.inverse,
          operationId: operationId(
            this.operationIdFactory,
            "undo",
            { id: `${entry.commandId}.undo` },
            entry,
          ),
          signal: context.signal,
        });
        try {
          const result = await invokePort(
            this.port,
            ["executeInverse"],
            { id: `${entry.commandId}.undo`, action: "inverse.execute" },
            payload,
          );
          const receipt = receiptFromResult(result);
          const receiptTargets = refreshTargetsFromRevisions(
            receipt && receipt.targets,
            entry.itemId,
            fallbackTargets,
          );
          const changedTargets = receiptTargets.length
            ? receiptTargets : fallbackTargets;
          const refresh = await refreshClassificationTargets(
            context,
            changedTargets,
            {
              command: Object.freeze({
                id: `${entry.commandId}.undo`,
                action: "inverse.execute",
              }),
              receipt,
              undo: entry,
              reason: "undo-committed",
            },
          );
          this.undoStack.pop();
          await this.onChanged(result, Object.freeze({
            command: Object.freeze({
              id: `${entry.commandId}.undo`,
              action: "inverse.execute",
            }),
            target: changedTargets[0] || null,
            targets: changedTargets,
            undo: entry,
            refreshAttempted: refresh.attempted,
            refreshErrors: refresh.errors,
          }));
          this.onStatus(`Undid ${entry.label.toLowerCase()}`, false);
          return result;
        } catch (error) {
          if (conflictError(error)) {
            const refresh = await refreshClassificationTargets(
              context,
              fallbackTargets,
              { error, undo: entry, reason: "undo-conflict" },
            );
            attachRefreshErrors(error, refresh);
            await this.onConflict(error, Object.freeze({
              undo: entry,
              targets: fallbackTargets,
              refreshAttempted: refresh.attempted,
              refreshErrors: refresh.errors,
            }));
          }
          throw error;
        }
      }
    }

    function registerClassificationCommands(registry, options = {}) {
      if (!registry || typeof registry.register !== "function") {
        throw new TypeError("a correction command registry is required");
      }
      const executor = options.executor instanceof ClassificationCommandExecutor
        ? options.executor : new ClassificationCommandExecutor(options);
      const commands = DEFAULT_COMMAND_DEFINITIONS.map((definition) =>
        registry.register({
          ...definition,
          available: (context, command) => executor.available(context, command),
          execute: (context, command) => executor.execute(context, command),
        }));
      return Object.freeze({ registry, executor, commands: Object.freeze(commands) });
    }

    function bindCommandControl(registry, commandId, control, options = {}) {
      if (!control || typeof control.addEventListener !== "function") {
        throw new TypeError("a command control is required");
      }
      const command = registry.get(commandId);
      if (!command) throw new CorrectionCommandError("Unknown command", "unknown_command");
      const getContext = typeof options.getContext === "function"
        ? options.getContext : () => ({});
      const onError = typeof options.onError === "function"
        ? options.onError : () => {};
      const refresh = () => {
        const binding = registry.bindingFor(command.id);
        control.dataset.commandId = command.id;
        control.setAttribute("aria-label",
          `${command.label}${binding ? ` (${displayKeyBinding(binding)})` : ""}`);
        if (binding) control.setAttribute("aria-keyshortcuts", ariaKeyBinding(binding));
        else control.removeAttribute("aria-keyshortcuts");
        control.disabled = !registry.canInvoke(command.id, getContext("control"));
      };
      const click = () => {
        Promise.resolve(registry.invoke(command.id, getContext("control")))
          .catch(onError)
          .finally(refresh);
      };
      control.addEventListener("click", click);
      const unsubscribe = registry.subscribe(refresh);
      refresh();
      return Object.freeze({
        refresh,
        invoke: () => registry.invoke(command.id, getContext("control")),
        destroy() {
          unsubscribe();
          control.removeEventListener("click", click);
        },
      });
    }

    return {
      CLASSIFICATION_COMMAND_IDS: COMMAND_IDS,
      CLASSIFICATION_TARGET_KINDS: TARGET_KINDS,
      DEFAULT_CLASSIFICATION_COMMANDS: DEFAULT_COMMAND_DEFINITIONS,
      CorrectionCommandError,
      KeyBindingConflictError,
      CorrectionCommandRegistry,
      ClassificationCommandExecutor,
      ariaKeyBinding,
      bindCommandControl,
      classificationTargetName,
      conflictError,
      displayKeyBinding,
      eventKeyBinding,
      imageTarget,
      annotationTarget,
      committedMaskTarget,
      linkedArtifactKeys,
      roleSourceArtifactKeys,
      normalizeKeyBinding,
      regionExtractQuad,
      registerClassificationCommands,
      resolveClassificationTarget,
    };
  });

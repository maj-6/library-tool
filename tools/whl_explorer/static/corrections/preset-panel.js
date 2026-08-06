(function installCorrectionsPresetPanel(root, factory) {
  const dependencies = typeof module === "object" && module.exports
    ? require("./image-editor-state")
    : root.LibraryToolCorrections;
  const api = factory(dependencies);
  if (typeof module === "object" && module.exports) module.exports = api;
  else Object.assign(root.LibraryToolCorrections ||= {}, api);
})(typeof globalThis !== "undefined" ? globalThis : this, function presetPanelFactory(
  stateApi,
) {
  "use strict";

  const { normalizeManualAdjustment, normalizeProcessingOperations } = stateApi;

  const PRESET_SCHEMA = "org.whl.processing-preset";
  const PRESET_VERSION = 1;
  const CATEGORY_LABELS = Object.freeze([
    Object.freeze({ id: "", label: "No category" }),
    Object.freeze({ id: "cover", label: "Cover" }),
    Object.freeze({ id: "title_page", label: "Title page" }),
    Object.freeze({ id: "spine", label: "Spine" }),
    Object.freeze({ id: "content_specimen", label: "Content" }),
    Object.freeze({ id: "other", label: "Other" }),
  ]);
  const CATEGORY_IDS = new Set(CATEGORY_LABELS.map((value) => value.id));
  let panelSequence = 0;

  function element(documentRef, tagName, className = "", text = null) {
    const node = documentRef.createElement(tagName);
    if (className) node.className = className;
    if (text != null) node.textContent = String(text);
    return node;
  }

  function option(documentRef, value, label) {
    const node = element(documentRef, "option", "", label);
    node.value = value;
    node.setAttribute("value", value);
    return node;
  }

  function presetIdFor(name, taken) {
    const base = String(name).toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "")
      .slice(0, 64) || "preset";
    let candidate = base;
    let counter = 2;
    while (taken.has(candidate)) {
      candidate = `${base}-${counter}`;
      counter += 1;
    }
    return candidate;
  }

  // A preset is only meaningful if it carries something to apply, which mirrors
  // the engine's own rule rather than letting the server reject an empty save.
  function buildPreset({ presetId, name, category, adjustment, operations }) {
    // An empty list is "no operations", not a malformed recipe: a tool whose
    // recipe is a bare manual adjustment hands one over on every save.
    const wanted = operations == null ||
      (Array.isArray(operations) && operations.length === 0) ? null : operations;
    const recipe = {
      schema: PRESET_SCHEMA,
      version: PRESET_VERSION,
      preset_id: presetId,
      name: String(name).trim(),
      category: CATEGORY_IDS.has(category) ? category : "",
      operations: wanted === null ? [] : normalizeProcessingOperations(wanted),
      adjustment: normalizeManualAdjustment(adjustment),
    };
    if (!recipe.name) throw new TypeError("a preset needs a name");
    if (!recipe.operations.length && recipe.adjustment === null) {
      throw new TypeError("a preset must carry an adjustment or an operation");
    }
    return recipe;
  }

  function createPresetPanel(options = {}) {
    const port = options.presets;
    if (!port || typeof port.list !== "function" ||
        typeof port.create !== "function" || typeof port.remove !== "function") {
      throw new TypeError("a processing preset port is required");
    }
    const documentRef = options.documentRef;
    if (!documentRef || typeof documentRef.createElement !== "function") {
      throw new TypeError("a document is required");
    }
    const onApply = typeof options.onApply === "function" ? options.onApply : null;
    const onBatchApply = typeof options.onBatchApply === "function"
      ? options.onBatchApply : null;
    const getCurrentRecipe = typeof options.getCurrentRecipe === "function"
      ? options.getCurrentRecipe : () => ({ adjustment: null, operations: [] });

    const instanceId = `preset-panel-${++panelSequence}`;
    let presets = [];
    let busy = false;
    let lastDeleted = null;
    let destroyed = false;
    const removers = [];

    const panel = element(documentRef, "fieldset", "preset-panel");
    panel.setAttribute("id", instanceId);
    panel.append(element(documentRef, "legend", "", "Presets"));

    const chooserRow = element(documentRef, "div", "preset-row");
    const chooser = element(documentRef, "select", "preset-chooser");
    chooser.setAttribute("aria-label", "Saved processing preset");
    const applyButton = element(documentRef, "button", "preset-apply", "Apply");
    applyButton.setAttribute("type", "button");
    const batchButton = element(documentRef, "button", "preset-batch", "All · —");
    batchButton.setAttribute("type", "button");
    const deleteButton = element(documentRef, "button", "preset-delete", "✕");
    deleteButton.setAttribute("type", "button");
    deleteButton.setAttribute("aria-label", "Delete preset");
    chooserRow.append(chooser, applyButton, batchButton, deleteButton);

    const saveRow = element(documentRef, "div", "preset-row");
    const nameInput = element(documentRef, "input", "preset-name");
    nameInput.setAttribute("type", "text");
    nameInput.setAttribute("maxlength", "120");
    nameInput.setAttribute("placeholder", "Save current as…");
    nameInput.setAttribute("aria-label", "New preset name");
    const categorySelect = element(documentRef, "select", "preset-category");
    categorySelect.setAttribute("aria-label", "Preset category");
    for (const entry of CATEGORY_LABELS) {
      categorySelect.append(option(documentRef, entry.id, entry.label));
    }
    categorySelect.value = "";
    const saveButton = element(documentRef, "button", "preset-save", "Save");
    saveButton.setAttribute("type", "button");
    saveRow.append(nameInput, categorySelect, saveButton);

    const status = element(documentRef, "p", "preset-status", "");
    status.setAttribute("role", "status");
    status.setAttribute("aria-live", "polite");
    const undoButton = element(documentRef, "button", "preset-undo", "Undo");
    undoButton.setAttribute("type", "button");
    undoButton.hidden = true;

    panel.append(chooserRow, saveRow, status, undoButton);

    function selected() {
      return presets.find((value) => value.preset_id === chooser.value) || null;
    }

    function setStatus(message, kind = "") {
      status.textContent = message;
      if (kind) status.setAttribute("data-preset-status", kind);
      else status.removeAttribute("data-preset-status");
    }

    function categoryLabel(id) {
      const entry = CATEGORY_LABELS.find((value) => value.id === id);
      return entry ? entry.label : id;
    }

    function render() {
      if (destroyed) return;
      const keep = chooser.value;
      chooser.replaceChildren();
      if (!presets.length) {
        chooser.append(option(documentRef, "", "No saved presets"));
        chooser.value = "";
      } else {
        for (const preset of presets) {
          const suffix = preset.category
            ? ` · ${categoryLabel(preset.category)}` : "";
          chooser.append(option(
            documentRef, preset.preset_id, `${preset.name}${suffix}`,
          ));
        }
        chooser.value = presets.some((value) => value.preset_id === keep)
          ? keep : presets[0].preset_id;
      }
      const current = selected();
      const disabled = busy || !current;
      chooser.disabled = disabled;
      applyButton.disabled = disabled;
      deleteButton.disabled = disabled;
      batchButton.disabled = disabled || !current.category || !onBatchApply;
      batchButton.textContent = current && current.category
        ? `All ${categoryLabel(current.category).toLowerCase()}`
        : "All · —";
      batchButton.setAttribute(
        "aria-label",
        current && current.category
          ? `Apply ${current.name} to every ${categoryLabel(current.category).toLowerCase()} in this book`
          : "Batch apply needs a preset with a category",
      );
      saveButton.disabled = busy;
      nameInput.disabled = busy;
      undoButton.hidden = lastDeleted === null;
    }

    async function withBusy(work, failure) {
      if (busy) return null;
      busy = true;
      render();
      try {
        return await work();
      } catch (error) {
        setStatus(`${failure}: ${error && error.message || error}`, "error");
        return null;
      } finally {
        busy = false;
        render();
      }
    }

    async function refresh() {
      return withBusy(async () => {
        const body = await port.list();
        presets = Array.isArray(body && body.presets) ? body.presets : [];
        return presets;
      }, "Presets could not be loaded");
    }

    addListener(chooser, "change", () => render());

    addListener(applyButton, "click", () => {
      const preset = selected();
      if (!preset || !onApply) return;
      onApply(preset);
      setStatus(`Applied ${preset.name}.`);
    });

    addListener(batchButton, "click", () => {
      const preset = selected();
      if (!preset || !preset.category || !onBatchApply) return;
      withBusy(async () => {
        const outcome = await onBatchApply(preset);
        const queued = outcome && Number(outcome.queued) || 0;
        const failed = outcome && Number(outcome.failed) || 0;
        // A partial batch is reported as a partial batch. Saying "done" when
        // some captures were skipped would hide work the reviewer still owes.
        setStatus(
          failed
            ? `${preset.name}: queued ${queued}, ${failed} failed.`
            : `${preset.name}: queued ${queued} ${categoryLabel(preset.category).toLowerCase()}.`,
          failed ? "error" : "",
        );
        return outcome;
      }, "Batch apply failed");
    });

    addListener(saveButton, "click", () => {
      let recipe;
      try {
        const current = getCurrentRecipe() || {};
        recipe = buildPreset({
          presetId: presetIdFor(
            nameInput.value,
            new Set(presets.map((value) => value.preset_id)),
          ),
          name: nameInput.value,
          category: categorySelect.value,
          adjustment: current.adjustment == null ? null : current.adjustment,
          operations: current.operations,
        });
      } catch (error) {
        setStatus(error.message, "error");
        return;
      }
      withBusy(async () => {
        await port.create({ preset: recipe });
        nameInput.value = "";
        await refreshInline();
        chooser.value = recipe.preset_id;
        setStatus(`Saved ${recipe.name}.`);
      }, "Preset could not be saved");
    });

    addListener(deleteButton, "click", () => {
      const preset = selected();
      if (!preset) return;
      withBusy(async () => {
        await port.remove({
          presetId: preset.preset_id,
          revision: preset.revision,
        });
        // Deletes are recoverable rather than confirmed: the removed recipe is
        // held so Undo can put it straight back.
        lastDeleted = preset;
        await refreshInline();
        setStatus(`Deleted ${preset.name}.`);
      }, "Preset could not be deleted");
    });

    addListener(undoButton, "click", () => {
      const preset = lastDeleted;
      if (!preset) return;
      withBusy(async () => {
        const { revision, ...recipe } = preset;
        await port.create({ preset: recipe });
        lastDeleted = null;
        await refreshInline();
        chooser.value = preset.preset_id;
        setStatus(`Restored ${preset.name}.`);
      }, "Preset could not be restored");
    });

    async function refreshInline() {
      const body = await port.list();
      presets = Array.isArray(body && body.presets) ? body.presets : [];
    }

    function addListener(target, type, handler) {
      if (!target || typeof target.addEventListener !== "function") return;
      target.addEventListener(type, handler);
      removers.push(() => target.removeEventListener(type, handler));
    }

    render();

    return Object.freeze({
      node: panel,
      refresh,
      getPresets: () => presets.slice(),
      destroy() {
        destroyed = true;
        for (const remove of removers.splice(0)) remove();
      },
    });
  }

  return {
    CATEGORY_LABELS,
    buildPreset,
    createPresetPanel,
    presetIdFor,
  };
});

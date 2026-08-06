const assert = require("node:assert/strict");
const test = require("node:test");

const {
  CATEGORY_LABELS,
  buildPreset,
  createPresetPanel,
  presetIdFor,
} = require("../tools/whl_explorer/static/corrections/preset-panel");
const {
  createManualBinaryAdjustment,
} = require("../tools/whl_explorer/static/corrections/image-adjust-tool");
const { FakeNode } = require("./fixtures/corrections_fake_dom");

// Properties the panel is allowed to touch even though FakeNode does not
// define them: the runtime and node:assert probe for these on any object.
const TOLERATED_MISSES = new Set([
  "then", "catch", "constructor", "toJSON", "nodeType", "inspect",
]);


// The handlers are async but the DOM events that start them are not, so a
// click only settles after the port's promise chain has drained.
async function settle(turns = 24) {
  for (let index = 0; index < turns; index += 1) await Promise.resolve();
}


// A document whose elements refuse any DOM API the stub does not implement, so
// the panel cannot quietly rely on jsdom-only surface.
function guardedDocument(overrides = {}) {
  const documentRef = {
    activeElement: null,
    unsupported: [],
    createElement(tagName) {
      const node = new FakeNode(tagName, documentRef);
      return new Proxy(node, {
        get(target, property, receiver) {
          if (typeof property === "string" && !(property in target) &&
              !TOLERATED_MISSES.has(property)) {
            documentRef.unsupported.push(`${target.tagName}.${property}`);
            throw new TypeError(`the DOM stub does not implement ${property}`);
          }
          return Reflect.get(target, property, receiver);
        },
      });
    },
    ...overrides,
  };
  return documentRef;
}


function adjustment(overrides = {}) {
  return { ...createManualBinaryAdjustment(24, 100), ...overrides };
}


function operation(overrides = {}) {
  return {
    schema: "org.whl.raster.processing-operation",
    version: 1,
    algorithm: "gamma-v1",
    rule: "output = 255 * (input / 255) ** (gamma_hundredths / 100)",
    gamma_hundredths: 120,
    ...overrides,
  };
}


function savedPreset(overrides = {}) {
  return {
    schema: "org.whl.processing-preset",
    version: 1,
    preset_id: "cover-pop",
    name: "Cover pop",
    category: "cover",
    operations: [],
    adjustment: adjustment(),
    revision: "rev-1",
    ...overrides,
  };
}


// An in-memory stand-in for the workspace preset service: async like the real
// port, and it remembers exactly what reached it.
function presetPort(overrides = {}) {
  const rows = [];
  const calls = [];
  const failures = {};
  let revisions = 0;
  return {
    rows,
    calls,
    failures,
    async list() {
      calls.push({ method: "list", payload: null });
      if (failures.list) throw failures.list;
      return { presets: rows.map((row) => ({ ...row })) };
    },
    async create(payload) {
      calls.push({ method: "create", payload });
      if (failures.create) throw failures.create;
      const preset = payload && payload.preset;
      if (rows.some((row) => row.preset_id === preset.preset_id)) {
        throw new Error(`preset already exists: ${preset.preset_id}`);
      }
      revisions += 1;
      rows.push({ ...preset, revision: `rev-new-${revisions}` });
      return { preset: { ...rows[rows.length - 1] } };
    },
    async update(payload) {
      calls.push({ method: "update", payload });
      if (failures.update) throw failures.update;
      const preset = payload && payload.preset;
      const index = rows.findIndex((row) => row.preset_id === preset.preset_id);
      if (index < 0) throw new Error(`no such preset: ${preset.preset_id}`);
      revisions += 1;
      rows[index] = { ...preset, revision: `rev-new-${revisions}` };
      return { preset: { ...rows[index] } };
    },
    async remove(payload) {
      calls.push({ method: "remove", payload });
      if (failures.remove) throw failures.remove;
      const index = rows.findIndex((row) => row.preset_id === payload.presetId);
      if (index < 0) throw new Error(`no such preset: ${payload.presetId}`);
      if (payload.revision != null && rows[index].revision !== payload.revision) {
        throw new Error("preset revision conflict");
      }
      rows.splice(index, 1);
      return { removed: true };
    },
    ...overrides,
  };
}


function mount(overrides = {}) {
  const settings = {
    port: presetPort(),
    documentRef: guardedDocument(),
    recipe: { adjustment: adjustment(), operations: [] },
    batchOutcome: { queued: 0, failed: 0 },
    ...overrides,
  };
  const applied = [];
  const batched = [];
  const panel = createPresetPanel({
    presets: settings.port,
    documentRef: settings.documentRef,
    getCurrentRecipe: () => settings.recipe,
    onApply: (preset) => applied.push(preset),
    onBatchApply: settings.batchOutcome === null ? null : async (preset) => {
      batched.push(preset);
      return settings.batchOutcome;
    },
  });
  const node = panel.node;
  return {
    settings,
    applied,
    batched,
    panel,
    node,
    port: settings.port,
    documentRef: settings.documentRef,
    chooser: node.querySelector(".preset-chooser"),
    applyButton: node.querySelector(".preset-apply"),
    batchButton: node.querySelector(".preset-batch"),
    deleteButton: node.querySelector(".preset-delete"),
    nameInput: node.querySelector(".preset-name"),
    categorySelect: node.querySelector(".preset-category"),
    saveButton: node.querySelector(".preset-save"),
    status: node.querySelector(".preset-status"),
    undoButton: node.querySelector(".preset-undo"),
  };
}


function select(harness, presetId) {
  harness.chooser.value = presetId;
  harness.chooser.emit("change");
}


test("an empty workspace shows a disabled chooser reading no saved presets", async () => {
  const harness = mount();

  await harness.panel.refresh();

  assert.equal(harness.chooser.textContent, "No saved presets");
  assert.equal(harness.chooser.value, "");
  assert.equal(harness.chooser.disabled, true);
  assert.equal(harness.applyButton.disabled, true);
  assert.equal(harness.batchButton.disabled, true);
  assert.equal(harness.deleteButton.disabled, true);
  assert.equal(harness.batchButton.textContent, "All · —");
  assert.equal(harness.undoButton.hidden, true);
  assert.deepEqual(harness.panel.getPresets(), []);
});


test("the category chooser offers every category label", () => {
  const harness = mount();

  assert.deepEqual(
    harness.categorySelect.children.map((child) => child.getAttribute("value")),
    CATEGORY_LABELS.map((entry) => entry.id),
  );
  assert.deepEqual(
    harness.categorySelect.children.map((child) => child.textContent),
    CATEGORY_LABELS.map((entry) => entry.label),
  );
  assert.equal(CATEGORY_LABELS[0].id, "");
  assert.equal(CATEGORY_LABELS[0].label, "No category");
  assert.equal(Object.isFrozen(CATEGORY_LABELS), true);
});


test("saving builds a canonical preset from the current recipe", async () => {
  const recipe = { adjustment: adjustment(), operations: [] };
  const harness = mount({ recipe });
  await harness.panel.refresh();

  harness.nameInput.value = "  Warm cover  ";
  harness.categorySelect.value = "cover";
  harness.saveButton.emit("click");
  await settle();

  const created = harness.port.calls.filter((call) => call.method === "create");
  assert.equal(created.length, 1);
  assert.deepEqual(created[0].payload.preset, {
    schema: "org.whl.processing-preset",
    version: 1,
    preset_id: "warm-cover",
    name: "Warm cover",
    category: "cover",
    operations: [],
    adjustment: recipe.adjustment,
  });
  assert.equal("revision" in created[0].payload.preset, false);
  assert.equal(harness.nameInput.value, "");
  assert.equal(harness.status.textContent, "Saved Warm cover.");
  assert.equal(harness.status.getAttribute("data-preset-status"), null);
  assert.equal(harness.chooser.value, "warm-cover");
});


test("saving a recipe with operations normalizes them and drops an unknown category", async () => {
  const harness = mount({
    recipe: { adjustment: null, operations: [operation()] },
  });
  await harness.panel.refresh();

  harness.nameInput.value = "Gamma bump";
  harness.categorySelect.value = "not-a-category";
  harness.saveButton.emit("click");
  await settle();

  const created = harness.port.calls.filter((call) => call.method === "create");
  assert.deepEqual(created[0].payload.preset.operations, [operation()]);
  assert.equal(created[0].payload.preset.adjustment, null);
  assert.equal(created[0].payload.preset.category, "");
});


test("presetIdFor slugifies a preset name", () => {
  const cases = [
    ["Warm cover", "warm-cover"],
    ["  Spine  boost 2  ", "spine-boost-2"],
    ["Title page · flatten!", "title-page-flatten"],
    ["!!!", "preset"],
    ["", "preset"],
    ["A".repeat(80), "a".repeat(64)],
  ];

  for (const [name, expected] of cases) {
    assert.equal(presetIdFor(name, new Set()), expected, name);
  }
});


test("presetIdFor de-duplicates against ids that are already taken", () => {
  const cases = [
    [[], "warm-cover"],
    [["warm-cover"], "warm-cover-2"],
    [["warm-cover", "warm-cover-2"], "warm-cover-3"],
    [["warm-cover-2"], "warm-cover"],
  ];

  for (const [taken, expected] of cases) {
    assert.equal(
      presetIdFor("Warm cover", new Set(taken)), expected, taken.join(","),
    );
  }
});


test("buildPreset rejects a blank name", () => {
  for (const name of ["", "   ", "\t\n"]) {
    assert.throws(() => buildPreset({
      presetId: "warm-cover",
      name,
      category: "cover",
      adjustment: adjustment(),
      operations: null,
    }), /a preset needs a name/, JSON.stringify(name));
  }
});


test("buildPreset rejects a preset that carries neither operations nor an adjustment", () => {
  for (const operations of [null, []]) {
    assert.throws(() => buildPreset({
      presetId: "empty",
      name: "Empty",
      category: "cover",
      adjustment: null,
      operations,
    }), /must carry an adjustment or an operation/, String(operations));
  }
});


test("applying calls back with the selected preset", async () => {
  const harness = mount();
  harness.port.rows.push(
    savedPreset(),
    savedPreset({ preset_id: "spine-lift", name: "Spine lift", category: "spine" }),
  );
  await harness.panel.refresh();

  select(harness, "spine-lift");
  harness.applyButton.emit("click");

  assert.equal(harness.applied.length, 1);
  assert.equal(harness.applied[0].preset_id, "spine-lift");
  assert.deepEqual(harness.applied[0], harness.port.rows[1]);
  assert.equal(harness.status.textContent, "Applied Spine lift.");
});


test("the batch button reflects the category of the selected preset", async () => {
  const harness = mount();
  harness.port.rows.push(
    savedPreset({ preset_id: "plain", name: "Plain", category: "" }),
    savedPreset({ preset_id: "cover-pop", name: "Cover pop", category: "cover" }),
    savedPreset({ preset_id: "title-pop", name: "Title pop", category: "title_page" }),
  );
  await harness.panel.refresh();

  const cases = [
    ["plain", "All · —", true],
    ["cover-pop", "All cover", false],
    ["title-pop", "All title page", false],
  ];
  for (const [presetId, label, disabled] of cases) {
    select(harness, presetId);
    assert.equal(harness.batchButton.textContent, label, presetId);
    assert.equal(harness.batchButton.disabled, disabled, presetId);
    assert.equal(harness.applyButton.disabled, false, presetId);
  }
});


test("a preset without a category cannot be batch applied", async () => {
  const harness = mount();
  harness.port.rows.push(savedPreset({ preset_id: "plain", name: "Plain", category: "" }));
  await harness.panel.refresh();

  harness.batchButton.emit("click");
  await settle();

  assert.deepEqual(harness.batched, []);
  assert.equal(harness.status.textContent, "");
});


test("batch apply is unavailable when the host wired no batch handler", async () => {
  const harness = mount({ batchOutcome: null });
  harness.port.rows.push(savedPreset());
  await harness.panel.refresh();

  assert.equal(harness.batchButton.disabled, true);
  assert.equal(harness.applyButton.disabled, false);
});


test("a batch apply reports the queued count in the status line", async () => {
  const harness = mount({ batchOutcome: { queued: 7, failed: 0 } });
  harness.port.rows.push(savedPreset());
  await harness.panel.refresh();

  harness.batchButton.emit("click");
  await settle();

  assert.equal(harness.batched.length, 1);
  assert.equal(harness.batched[0].preset_id, "cover-pop");
  assert.equal(harness.status.textContent, "Cover pop: queued 7 cover.");
  assert.equal(harness.status.getAttribute("data-preset-status"), null);
  assert.equal(harness.batchButton.disabled, false);
});


test("a batch apply that fails part of the book reports the failure count", async () => {
  const harness = mount({ batchOutcome: { queued: 5, failed: 2 } });
  harness.port.rows.push(savedPreset());
  await harness.panel.refresh();

  harness.batchButton.emit("click");
  await settle();

  assert.match(harness.status.textContent, /2 failed/);
  assert.match(harness.status.textContent, /queued 5/);
  assert.equal(harness.status.getAttribute("data-preset-status"), "error");
});


test("deleting a preset removes it from the port and reveals undo", async () => {
  const harness = mount();
  harness.port.rows.push(savedPreset());
  await harness.panel.refresh();

  harness.deleteButton.emit("click");
  await settle();

  const removed = harness.port.calls.filter((call) => call.method === "remove");
  assert.equal(removed.length, 1);
  assert.deepEqual(removed[0].payload, { presetId: "cover-pop", revision: "rev-1" });
  assert.deepEqual(harness.port.rows, []);
  assert.deepEqual(harness.panel.getPresets(), []);
  assert.equal(harness.status.textContent, "Deleted Cover pop.");
  assert.equal(harness.undoButton.hidden, false);
  assert.equal(harness.chooser.textContent, "No saved presets");
});


test("undo re-creates the deleted preset without its revision", async () => {
  const harness = mount();
  harness.port.rows.push(savedPreset());
  await harness.panel.refresh();
  harness.deleteButton.emit("click");
  await settle();

  harness.undoButton.emit("click");
  await settle();

  const created = harness.port.calls.filter((call) => call.method === "create");
  assert.equal(created.length, 1);
  assert.equal("revision" in created[0].payload.preset, false);
  assert.deepEqual(created[0].payload.preset, {
    schema: "org.whl.processing-preset",
    version: 1,
    preset_id: "cover-pop",
    name: "Cover pop",
    category: "cover",
    operations: [],
    adjustment: adjustment(),
  });
  assert.equal(harness.port.rows.length, 1);
  assert.equal(harness.port.rows[0].preset_id, "cover-pop");
  assert.equal(harness.status.textContent, "Restored Cover pop.");
  assert.equal(harness.undoButton.hidden, true);
  assert.equal(harness.chooser.value, "cover-pop");
});


test("a port rejection is reported and leaves the panel usable", async () => {
  const cases = [
    {
      label: "list",
      method: "list",
      message: /Presets could not be loaded/,
      async act(harness) {
        await harness.panel.refresh();
      },
    },
    {
      label: "create",
      method: "create",
      message: /Preset could not be saved/,
      async act(harness) {
        harness.nameInput.value = "Broken";
        harness.saveButton.emit("click");
      },
    },
    {
      label: "delete",
      method: "remove",
      message: /Preset could not be deleted/,
      async act(harness) {
        harness.deleteButton.emit("click");
      },
    },
  ];

  for (const entry of cases) {
    const harness = mount();
    harness.port.rows.push(savedPreset());
    await harness.panel.refresh();

    harness.port.failures[entry.method] = new Error("the port said no");
    await entry.act(harness);
    await settle();

    assert.equal(
      harness.status.getAttribute("data-preset-status"), "error", entry.label,
    );
    assert.match(harness.status.textContent, entry.message, entry.label);
    assert.match(harness.status.textContent, /the port said no/, entry.label);
    assert.equal(harness.saveButton.disabled, false, entry.label);
    assert.equal(harness.nameInput.disabled, false, entry.label);

    delete harness.port.failures[entry.method];
    harness.nameInput.value = "Recovered";
    harness.saveButton.emit("click");
    await settle();

    assert.equal(harness.status.textContent, "Saved Recovered.", entry.label);
    assert.equal(
      harness.status.getAttribute("data-preset-status"), null, entry.label,
    );
    assert.equal(
      harness.port.rows.some((row) => row.name === "Recovered"), true, entry.label,
    );
  }
});


test("an unsaveable recipe is reported without reaching the port", async () => {
  const harness = mount({ recipe: { adjustment: null, operations: [] } });
  await harness.panel.refresh();

  harness.nameInput.value = "Nothing to save";
  harness.saveButton.emit("click");
  await settle();

  assert.deepEqual(harness.port.calls.filter((call) => call.method === "create"), []);
  assert.match(harness.status.textContent, /must carry an adjustment or an operation/);
  assert.equal(harness.status.getAttribute("data-preset-status"), "error");
  assert.equal(harness.saveButton.disabled, false);
});


test("destroy detaches the panel listeners", async () => {
  const harness = mount();
  harness.port.rows.push(savedPreset());
  await harness.panel.refresh();
  const before = harness.port.calls.length;

  harness.panel.destroy();
  harness.nameInput.value = "After destroy";
  harness.saveButton.emit("click");
  harness.applyButton.emit("click");
  harness.batchButton.emit("click");
  harness.deleteButton.emit("click");
  harness.undoButton.emit("click");
  harness.chooser.emit("change");
  await settle();

  assert.equal(harness.port.calls.length, before);
  assert.deepEqual(harness.applied, []);
  assert.deepEqual(harness.batched, []);
  assert.deepEqual(harness.port.rows.length, 1);
});


test("the panel only uses dom apis the fake dom implements", async () => {
  const harness = mount({ batchOutcome: { queued: 3, failed: 1 } });
  harness.port.rows.push(savedPreset());
  await harness.panel.refresh();

  harness.nameInput.value = "Guarded";
  harness.categorySelect.value = "spine";
  harness.saveButton.emit("click");
  await settle();
  select(harness, "cover-pop");
  harness.applyButton.emit("click");
  harness.batchButton.emit("click");
  await settle();
  harness.deleteButton.emit("click");
  await settle();
  harness.undoButton.emit("click");
  await settle();
  harness.panel.destroy();

  assert.deepEqual(harness.documentRef.unsupported, []);
  assert.throws(
    () => harness.documentRef.createElement("div").classList,
    /does not implement classList/,
  );
});

"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const {
  CLASSIFICATION_COMMAND_IDS,
  CorrectionCommandRegistry,
  KeyBindingConflictError,
  bindCommandControl,
  registerClassificationCommands,
} = require("../tools/whl_explorer/static/corrections/commands");
const {
  createClassificationController,
} = require("../tools/whl_explorer/static/corrections/keymap");
const {
  FakeNode,
  fakeDocument,
} = require("./fixtures/corrections_fake_dom");


function image(overrides = {}) {
  return {
    key: "artifact:scan-1",
    objectType: "raster-artifact",
    family: "image",
    group: "source-images",
    itemId: "book-1",
    id: "scan-1",
    revision: "scan-r1",
    label: "Front capture",
    ...overrides,
  };
}


function annotation(overrides = {}) {
  return {
    key: "annotation:region-1",
    objectType: "spatial-annotation",
    family: "regions",
    group: "layout-regions",
    itemId: "book-1",
    id: "region-1",
    revision: "region-r1",
    label: "Handwritten note",
    linkedKeys: ["artifact:figure-1"],
    ...overrides,
  };
}


function revisionTarget(kind, id, before, after) {
  return {
    kind,
    target_id: id,
    before_revision: before,
    after_revision: after,
  };
}


function mutationResult(targets, inverseAction = "role.clear") {
  return {
    receipt: {
      item_id: "book-1",
      targets,
      inverse: {
        action: inverseAction,
        expected_targets: targets.map((target) => ({ ...target })),
        payload: {},
      },
    },
  };
}


function harness(options = {}) {
  const documentRef = fakeDocument();
  documentRef.hasFocus = () => true;
  const scope = new FakeNode("main", documentRef);
  const calls = [];
  let sequence = 0;
  const port = options.port || {
    async assignImageCategory(payload) {
      calls.push(["image", payload]);
      return {
        receipt: {
          inverse: {
            action: "category.clear",
            expected_targets: [],
            payload: {},
          },
        },
      };
    },
    async assignRegionRole(payload) {
      calls.push(["role", payload]);
      return {
        receipt: {
          inverse: {
            action: "role.clear",
            expected_targets: [],
            payload: {},
          },
        },
      };
    },
    async executeInverse(payload) {
      calls.push(["undo", payload]);
      return { receipt: { operation_id: payload.operationId } };
    },
  };
  const controller = createClassificationController({
    scope,
    documentRef,
    windowRef: options.windowRef,
    port,
    history: options.history,
    resolveLinkedArtifact: options.resolveLinkedArtifact,
    refreshTarget: options.refreshTarget,
    promoteSoftTarget: options.promoteSoftTarget,
    onTarget: options.onTarget,
    onChanged: options.onChanged,
    onConflict: options.onConflict,
    onError: options.onError || ((error) => {
      throw error;
    }),
    operationIdFactory: (prefix) => `op-${prefix}-${++sequence}`,
  });
  return { calls, controller, documentRef, port, scope };
}


function settled() {
  return new Promise((resolve) => setImmediate(resolve));
}


test("visible controls and shortcuts invoke the same registered image command", async () => {
  const changed = [];
  const { calls, controller, documentRef, scope } = harness({
    onChanged(_result, detail) {
      changed.push(detail.command.id);
    },
  });
  controller.setSelectionTarget(image());
  controller.mount();
  const button = new FakeNode("button", documentRef);
  scope.append(button);
  const binding = controller.bindControl(
    CLASSIFICATION_COMMAND_IDS.titlePage,
    button,
  );

  button.emit("click");
  await settled();
  const event = scope.emit("keydown", { key: "t", target: scope });
  await settled();

  assert.equal(event.defaultPrevented, true);
  assert.deepEqual(changed, [
    CLASSIFICATION_COMMAND_IDS.titlePage,
    CLASSIFICATION_COMMAND_IDS.titlePage,
  ]);
  assert.equal(calls.length, 2);
  for (const [, payload] of calls) {
    assert.equal(payload.itemId, "book-1");
    assert.equal(payload.artifactId, "scan-1");
    assert.equal(payload.expectedArtifactRevision, "scan-r1");
    assert.equal(payload.category, "title_page");
  }
  assert.equal(button.getAttribute("aria-keyshortcuts"), "T");
  assert.match(button.getAttribute("aria-label"), /Mark as title page \(T\)/);

  binding.destroy();
  controller.destroy();
});


test("navigation-only image hints cannot activate a category command", async () => {
  const { calls, controller, documentRef, scope } = harness({ onError() {} });
  controller.mount();
  const button = new FakeNode("button", documentRef);
  scope.append(button);
  const binding = controller.bindControl(
    CLASSIFICATION_COMMAND_IDS.titlePage,
    button,
  );

  controller.setSelectionTarget(image({ revision: "index:abc123" }));
  binding.refresh();
  assert.equal(button.disabled, true);
  button.emit("click");
  await settled();
  assert.equal(calls.length, 0);

  controller.setSelectionTarget(image({ revision: "scan-r9" }));
  binding.refresh();
  assert.equal(button.disabled, false);
  button.emit("click");
  await settled();
  assert.equal(calls.length, 1);
  assert.equal(calls[0][1].expectedArtifactRevision, "scan-r9");

  binding.destroy();
  controller.destroy();
});


test("classification commands stay unavailable without a mutation port", () => {
  const documentRef = fakeDocument();
  documentRef.hasFocus = () => true;
  const scope = new FakeNode("main", documentRef);
  const controller = createClassificationController({
    scope,
    documentRef,
    port: null,
  });
  controller.setSelectionTarget(image());
  controller.mount();

  assert.equal(
    controller.registry.canInvoke(CLASSIFICATION_COMMAND_IDS.titlePage),
    false,
  );
});


test("region classification is one CAS-pinned linked-artifact transaction and undoable", async () => {
  const order = [];
  const history = [];
  const { calls, controller, scope } = harness({
    history: { push: (entry) => history.push(entry) },
    resolveLinkedArtifact(target, request) {
      order.push(["resolve", target.key, request.linkedKey]);
      return {
        key: "artifact:figure-1",
        objectType: "raster-artifact",
        itemId: "book-1",
        id: "figure-1",
        revision: "figure-r4",
      };
    },
    onTarget(detail) {
      order.push(["target", detail.name, detail.command.id]);
    },
    onChanged(_result, detail) {
      order.push(["changed", detail.command.id]);
    },
  });
  controller.setSelectionTarget(annotation());
  controller.mount();

  scope.emit("keydown", { key: "m", target: scope });
  await settled();

  assert.equal(calls.length, 1, "the browser must not fan out linked writes");
  assert.equal(calls[0][0], "role");
  assert.deepEqual(calls[0][1], {
    itemId: "book-1",
    annotationId: "region-1",
    expectedAnnotationRevision: "region-r1",
    role: "marginalia",
    operationId: "op-role-1",
    signal: undefined,
    linkedArtifactId: "figure-1",
    expectedLinkedArtifactRevision: "figure-r4",
  });
  assert.deepEqual(order.map((entry) => entry[0]), [
    "target", "resolve", "changed",
  ]);
  assert.equal(history.length, 1);
  assert.equal(history[0].inverse.action, "role.clear");

  await controller.undoLast();
  assert.equal(calls.length, 2);
  assert.equal(calls[1][0], "undo");
  assert.equal(calls[1][1].itemId, "book-1");
  assert.equal(calls[1][1].inverse.action, "role.clear");
  assert.equal(calls[1][1].operationId, "op-undo-2");
});


test("linked role receipts refresh every changed target after apply and undo", async () => {
  const appliedTargets = [
    revisionTarget("annotation", "region-1", "region-r1", "region-r2"),
    revisionTarget("artifact", "figure-1", "figure-r4", "figure-r5"),
  ];
  const undoneTargets = [
    revisionTarget("annotation", "region-1", "region-r2", "region-r3"),
    revisionTarget("artifact", "figure-1", "figure-r5", "figure-r6"),
  ];
  const refreshes = [];
  let controller;
  const built = harness({
    port: {
      async assignRegionRole() {
        controller.setSelectionTarget(image({
          key: "artifact:unrelated",
          id: "unrelated",
          revision: "unrelated-r1",
        }));
        return mutationResult(appliedTargets);
      },
      async executeInverse() {
        return mutationResult(undoneTargets, "role.assign");
      },
    },
    resolveLinkedArtifact() {
      return {
        key: "artifact:figure-1",
        objectType: "raster-artifact",
        itemId: "book-1",
        id: "figure-1",
        revision: "figure-r4",
      };
    },
    async refreshTarget(target, detail) {
      refreshes.push({
        key: target.key,
        revision: target.revision,
        reason: detail.reason,
      });
    },
  });
  controller = built.controller;
  controller.setSelectionTarget(annotation());

  await controller.invoke(CLASSIFICATION_COMMAND_IDS.marginalia);

  assert.deepEqual(refreshes, [
    { key: "annotation:region-1", revision: "region-r2", reason: "committed" },
    { key: "artifact:figure-1", revision: "figure-r5", reason: "committed" },
  ]);
  assert.equal(
    controller.stateSnapshot().selectionTarget.key,
    "artifact:unrelated",
    "receipt refresh must not import the command's old target into selection state",
  );

  refreshes.length = 0;
  controller.setSelectionTarget(image({
    key: "artifact:other-selection",
    id: "other-selection",
    revision: "other-r1",
  }));
  await controller.undoLast();

  assert.deepEqual(refreshes, [
    {
      key: "annotation:region-1",
      revision: "region-r3",
      reason: "undo-committed",
    },
    {
      key: "artifact:figure-1",
      revision: "figure-r6",
      reason: "undo-committed",
    },
  ]);
  assert.equal(
    controller.stateSnapshot().selectionTarget.key,
    "artifact:other-selection",
    "undo refresh must use its receipt instead of the current selection",
  );
});


test("source image refresh targets retain their group across receipts and undo", async () => {
  const applied = [
    revisionTarget("artifact", "scan-1", "scan-r1", "scan-r2"),
  ];
  const undone = [
    revisionTarget("artifact", "scan-1", "scan-r2", "scan-r3"),
  ];
  const refreshes = [];
  const { controller } = harness({
    port: {
      async assignImageCategory() {
        return mutationResult(applied, "category.clear");
      },
      async executeInverse() {
        return mutationResult(undone, "category.assign");
      },
    },
    async refreshTarget(target, detail) {
      refreshes.push({
        group: target.group,
        key: target.key,
        reason: detail.reason,
        revision: target.revision,
      });
    },
  });
  controller.setSelectionTarget(image());

  await controller.invoke(CLASSIFICATION_COMMAND_IDS.cover);
  await controller.undoLast();

  assert.deepEqual(refreshes, [
    {
      group: "source-images",
      key: "artifact:scan-1",
      reason: "committed",
      revision: "scan-r2",
    },
    {
      group: "source-images",
      key: "artifact:scan-1",
      reason: "undo-committed",
      revision: "scan-r3",
    },
  ]);
});


test("linked role conflicts force both target refreshes even when one fails", async () => {
  const conflict = Object.assign(new Error("changed elsewhere"), {
    code: "annotation_revision_conflict",
    status: 409,
  });
  const refreshFailure = new Error("annotation reload unavailable");
  const refreshes = [];
  const conflicts = [];
  const history = [];
  const { controller } = harness({
    port: {
      async assignRegionRole() {
        throw conflict;
      },
    },
    history: { push: (entry) => history.push(entry) },
    resolveLinkedArtifact() {
      return {
        key: "artifact:figure-1",
        objectType: "raster-artifact",
        itemId: "book-1",
        id: "figure-1",
        revision: "figure-r4",
      };
    },
    async refreshTarget(target, detail) {
      refreshes.push([target.key, detail.reason]);
      if (target.key === "annotation:region-1") throw refreshFailure;
    },
    async onConflict(error, detail) {
      conflicts.push({ error, detail });
    },
  });
  controller.setSelectionTarget(annotation());

  await assert.rejects(
    controller.invoke(CLASSIFICATION_COMMAND_IDS.illustration),
    (error) => error === conflict,
  );

  assert.deepEqual(refreshes, [
    ["annotation:region-1", "conflict"],
    ["artifact:figure-1", "conflict"],
  ]);
  assert.deepEqual(conflict.refreshErrors, [refreshFailure]);
  assert.deepEqual(
    conflicts[0].detail.targets.map((target) => target.key),
    ["annotation:region-1", "artifact:figure-1"],
  );
  assert.equal(history.length, 0);
});


test("linked role undo conflicts refresh recorded targets, not current selection", async () => {
  const targets = [
    revisionTarget("annotation", "region-1", "region-r1", "region-r2"),
    revisionTarget("artifact", "figure-1", "figure-r4", "figure-r5"),
  ];
  const conflict = Object.assign(new Error("undo changed elsewhere"), {
    code: "artifact_revision_conflict",
    status: 409,
  });
  const refreshes = [];
  const { controller } = harness({
    port: {
      async assignRegionRole() {
        return mutationResult(targets);
      },
      async executeInverse() {
        throw conflict;
      },
    },
    resolveLinkedArtifact() {
      return {
        key: "artifact:figure-1",
        objectType: "raster-artifact",
        itemId: "book-1",
        id: "figure-1",
        revision: "figure-r4",
      };
    },
    async refreshTarget(target, detail) {
      refreshes.push([target.key, detail.reason]);
    },
  });
  controller.setSelectionTarget(annotation());
  await controller.invoke(CLASSIFICATION_COMMAND_IDS.marginalia);

  refreshes.length = 0;
  controller.setSelectionTarget(image({
    key: "artifact:unrelated",
    id: "unrelated",
    revision: "unrelated-r1",
  }));
  await assert.rejects(controller.undoLast(), (error) => error === conflict);

  assert.deepEqual(refreshes, [
    ["annotation:region-1", "undo-conflict"],
    ["artifact:figure-1", "undo-conflict"],
  ]);
  assert.equal(
    controller.stateSnapshot().selectionTarget.key,
    "artifact:unrelated",
  );
});


test("raw spatial annotation link ids resolve the linked artifact transaction", async () => {
  const requests = [];
  const { calls, controller } = harness({
    resolveLinkedArtifact(_target, request) {
      requests.push(request);
      return {
        key: "artifact:figure-1",
        objectType: "raster-artifact",
        itemId: "book-1",
        id: "figure-1",
        revision: "figure-r4",
      };
    },
  });
  controller.setSelectionTarget(annotation({
    linkedKeys: undefined,
    linked_artifact_ids: ["figure-1"],
  }));

  await controller.invoke(CLASSIFICATION_COMMAND_IDS.illustration);

  assert.equal(requests[0].linkedKey, "artifact:figure-1");
  assert.equal(calls[0][1].linkedArtifactId, "figure-1");
  assert.equal(calls[0][1].expectedLinkedArtifactRevision, "figure-r4");
});


test("a hovered compatible target wins over the selection and is promoted first", async () => {
  const order = [];
  const { calls, controller } = harness({
    async promoteSoftTarget(target) {
      order.push(["promote", target.key]);
      return target;
    },
    onTarget(detail) {
      order.push(["target", detail.target.key]);
    },
  });
  controller.setSelectionTarget(image({
    key: "artifact:focused",
    id: "focused",
    revision: "focused-r1",
  }));
  controller.setHotTarget(image({
    key: "artifact:hovered",
    id: "hovered",
    revision: "hovered-r1",
  }));

  await controller.invoke(CLASSIFICATION_COMMAND_IDS.cover);
  assert.equal(calls[0][1].artifactId, "hovered");
  assert.deepEqual(order, [
    ["promote", "artifact:hovered"],
    ["target", "artifact:hovered"],
  ]);

  controller.setSelectionTarget(image({
    key: "artifact:focused",
    id: "focused",
    revision: "focused-r1",
  }));
  controller.setHotTarget(annotation({ linkedKeys: [] }));
  await controller.invoke(CLASSIFICATION_COMMAND_IDS.spine);
  assert.equal(calls[1][1].artifactId, "focused");
  assert.deepEqual(order.slice(2), [["target", "artifact:focused"]],
    "kind acceptance keeps image commands off a hovered annotation");

  controller.setHotTarget(null);
  await controller.invoke(CLASSIFICATION_COMMAND_IDS.titlePage);
  assert.equal(calls[2][1].artifactId, "focused");
  assert.deepEqual(order.slice(3), [["target", "artifact:focused"]]);
});


test("manuscript, stamp, and damage assign their open-vocabulary region roles", async () => {
  const { calls, controller, scope } = harness();
  controller.setSelectionTarget(annotation({ linkedKeys: [] }));
  controller.mount();

  assert.deepEqual(
    [
      CLASSIFICATION_COMMAND_IDS.manuscript,
      CLASSIFICATION_COMMAND_IDS.stamp,
      CLASSIFICATION_COMMAND_IDS.damage,
    ].map((id) => {
      const command = controller.registry.get(id);
      return [command.label, command.code, controller.registry.bindingFor(id)];
    }),
    [
      ["Mark region as manuscript", "MS", "n"],
      ["Mark region as stamp", "STP", "p"],
      ["Mark region as damage", "DMG", "d"],
    ],
  );
  assert.equal(controller.registry.list().length, 9,
    "the new default bindings register without a KeyBindingConflictError");

  for (const key of ["n", "p", "d"]) {
    scope.emit("keydown", { key, target: scope });
    await settled();
  }

  assert.deepEqual(calls.map(([route, payload]) => [route, payload.role]), [
    ["role", "manuscript"],
    ["role", "stamp"],
    ["role", "damage"],
  ]);
  for (const [, payload] of calls) {
    assert.equal(payload.itemId, "book-1");
    assert.equal(payload.annotationId, "region-1");
    assert.equal(payload.expectedAnnotationRevision, "region-r1");
  }
});


test("conflicts refresh current state without retrying or recording a false undo", async () => {
  const conflict = Object.assign(new Error("changed elsewhere"), {
    code: "artifact_revision_conflict",
    status: 409,
  });
  let attempts = 0;
  let refreshes = 0;
  let conflicts = 0;
  const history = [];
  const { controller } = harness({
    port: {
      async assignImageCategory() {
        attempts += 1;
        throw conflict;
      },
    },
    history: { push: (entry) => history.push(entry) },
    async refreshTarget(target, detail) {
      assert.equal(target.group, "source-images");
      assert.equal(detail.reason, "conflict");
      refreshes += 1;
    },
    async onConflict(error) {
      assert.equal(error, conflict);
      conflicts += 1;
    },
  });
  controller.setSelectionTarget(image());

  await assert.rejects(
    controller.invoke(CLASSIFICATION_COMMAND_IDS.contentSpecimen),
    (error) => error === conflict,
  );

  assert.equal(attempts, 1);
  assert.equal(refreshes, 1);
  assert.equal(conflicts, 1);
  assert.equal(history.length, 0);
});


test("remapping detects conflicts and exposes deliberate conflict replacement", () => {
  const registry = new CorrectionCommandRegistry();
  registerClassificationCommands(registry, {
    port: {
      async assignImageCategory() {},
      async assignRegionRole() {},
    },
    operationIdFactory: () => "op-1",
  });

  assert.throws(
    () => registry.remap(CLASSIFICATION_COMMAND_IDS.titlePage, "C"),
    (error) => {
      assert.ok(error instanceof KeyBindingConflictError);
      assert.equal(error.code, "key_binding_conflict");
      assert.deepEqual(error.details.commandIds, [
        CLASSIFICATION_COMMAND_IDS.cover,
      ]);
      return true;
    },
  );

  registry.remap(CLASSIFICATION_COMMAND_IDS.titlePage, "C", {
    replaceConflicts: true,
  });
  assert.equal(registry.bindingFor(CLASSIFICATION_COMMAND_IDS.titlePage), "c");
  assert.equal(registry.bindingFor(CLASSIFICATION_COMMAND_IDS.cover), "");
  assert.equal(
    registry.commandForBinding("c").id,
    CLASSIFICATION_COMMAND_IDS.titlePage,
  );
});


test("linked artifact identity and revision are mandatory before role mutation", async () => {
  const { calls, controller } = harness();
  controller.setSelectionTarget(annotation());

  await assert.rejects(
    controller.invoke(CLASSIFICATION_COMMAND_IDS.illustration),
    (error) => error.code === "linked_artifact_revision_required",
  );
  assert.equal(calls.length, 0);
});


test("#235 modules install in dependency order through the browser namespace", () => {
  const context = vm.createContext({});
  const staticRoot = path.join(
    __dirname,
    "..",
    "tools",
    "whl_explorer",
    "static",
    "corrections",
  );
  for (const name of ["commands.js", "keymap.js", "artifact-overlay.js"]) {
    vm.runInContext(
      fs.readFileSync(path.join(staticRoot, name), "utf8"),
      context,
      { filename: name },
    );
  }
  const exported = context.LibraryToolCorrections;
  assert.equal(typeof exported.CorrectionCommandRegistry, "function");
  assert.equal(typeof exported.createClassificationController, "function");
  assert.equal(typeof exported.createArtifactOverlay, "function");
  assert.equal(
    exported.CLASSIFICATION_COMMAND_IDS.marginalia,
    "corrections.role.marginalia",
  );
});

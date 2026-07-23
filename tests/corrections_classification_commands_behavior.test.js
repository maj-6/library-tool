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


test("focused compatible target wins; a soft-only target is promoted before mutation", async () => {
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
  assert.equal(calls[0][1].artifactId, "focused");
  assert.deepEqual(order, [["target", "artifact:focused"]]);

  controller.setSelectionTarget(annotation({ linkedKeys: [] }));
  await controller.invoke(CLASSIFICATION_COMMAND_IDS.spine);
  assert.equal(calls[1][1].artifactId, "hovered");
  assert.deepEqual(order.slice(1), [
    ["promote", "artifact:hovered"],
    ["target", "artifact:hovered"],
  ]);
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
    async refreshTarget() {
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

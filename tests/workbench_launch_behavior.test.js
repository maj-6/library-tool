const assert = require("node:assert/strict");
const test = require("node:test");

const {
  CONTEXT_SCHEMA,
  createContext,
  open,
} = require("../tools/whl_explorer/static/workbench-launch");


test("createContext emits the versioned portable workbench address", () => {
  const context = createContext({
    workbenchId: "corrections",
    workspaceId: "local-library",
    itemId: "book-17",
    representationId: "scan-primary",
    resourceRevision: 4,
    viewHint: { panel: "books" },
    origin: { surface: "manager" },
    uiProfileKey: "corrections/default",
  });

  assert.deepEqual(context, {
    schema: CONTEXT_SCHEMA,
    workbench_id: "corrections",
    workspace_id: "local-library",
    item_id: "book-17",
    representation_id: "scan-primary",
    resource_revision: 4,
    view_hint: { panel: "books" },
    origin: { surface: "manager" },
    ui_profile_key: "corrections/default",
  });
  assert.equal(Object.isFrozen(context), true);
});


test("createContext omits empty optional addresses and rejects unsafe ids", () => {
  assert.deepEqual(createContext({
    workbenchId: "corrections",
    workspaceId: "local-library",
    itemId: null,
  }), {
    schema: CONTEXT_SCHEMA,
    workbench_id: "corrections",
    workspace_id: "local-library",
  });
  assert.throws(() => createContext({
    workbenchId: "corrections",
    workspaceId: "../library",
  }), /workspaceId/);
  assert.throws(() => createContext({
    workbenchId: "corrections",
    workspaceId: "local-library",
    uiProfileKey: "corrections/../other",
  }), /uiProfileKey/);
});


test("open delegates immutable context and duplicate-window intent to desktop", async () => {
  const calls = [];
  const desktop = {
    workbenches: {
      open: async (context, options) => {
        calls.push({ context, options });
        return { ok: true, reused: false };
      },
    },
  };

  const result = await open(desktop, {
    workbenchId: "corrections",
    workspaceId: "local-library",
    itemId: "book-17",
    newWindow: true,
  });

  assert.deepEqual(result, { ok: true, reused: false });
  assert.equal(calls.length, 1);
  assert.equal(calls[0].context.item_id, "book-17");
  assert.deepEqual(calls[0].options, { newWindow: true });
});


test("open defaults to reusable-window intent", async () => {
  const calls = [];
  const desktop = {
    workbenches: {
      open: async (context, options) => {
        calls.push({ context, options });
        return { ok: true, reused: true };
      },
    },
  };

  const result = await open(desktop, {
    workbenchId: "corrections",
    workspaceId: "local-library",
    itemId: "book-17",
  });

  assert.equal(result.reused, true);
  assert.deepEqual(calls[0].options, { newWindow: false });
});


test("open fails clearly outside the desktop workbench bridge", async () => {
  await assert.rejects(
    () => open(null, {
      workbenchId: "corrections",
      workspaceId: "local-library",
    }),
    (error) => error.code === "WORKBENCH_UNAVAILABLE",
  );
});


test("open turns a rejected desktop reply into a failed launch", async () => {
  const desktop = {
    workbenches: {
      open: async () => ({
        ok: false,
        error: "invalid_workbench_open_request",
        message: "bad context",
      }),
    },
  };

  await assert.rejects(
    () => open(desktop, {
      workbenchId: "corrections",
      workspaceId: "local-library",
    }),
    (error) => error.code === "invalid_workbench_open_request" &&
      error.message === "bad context",
  );
});

"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const {
  CORRECTIONS_ITEM_MUTATION_SCHEMA,
  CORRECTIONS_ITEM_SCHEMA,
  CorrectionsItemApi,
  MAX_METADATA_TEXT,
  createItemMetadataEditor,
  editableMetadata,
  isConflict,
  patchFromDraft,
} = require("../tools/whl_explorer/static/corrections/item-properties");
const {
  CorrectionsShell,
  CorrectionsWindowState,
} = require("../tools/whl_explorer/static/corrections/shell");
const {
  FakeNode,
  deferred,
  fakeDocument,
} = require("./fixtures/corrections_fake_dom");


function item(overrides = {}) {
  return {
    id: "capture-b9f1",
    kind: "capture",
    title: "Uncatalogued herbal",
    metadata: {
      authors: "Unknown",
      condition: "foxed",
      capture_id: "phone-roll-raw-id",
      local_pdf: "C:\\private\\capture.pdf",
    },
    record_revision: "mir-r1",
    ...overrides,
  };
}


function response(body, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    async json() { return body; },
  };
}


function loadEnvelope(value) {
  return {
    ok: true,
    schema: CORRECTIONS_ITEM_SCHEMA,
    item: value,
  };
}


function mutationEnvelope(value, replayed = false) {
  return {
    ok: true,
    schema: CORRECTIONS_ITEM_MUTATION_SCHEMA,
    item: value,
    replayed,
  };
}


function harness(options = {}) {
  const documentRef = fakeDocument();
  const root = new FakeNode("section", documentRef);
  const statuses = [];
  const changes = [];
  const editor = createItemMetadataEditor({
    root,
    documentRef,
    operationIdFactory: () => "metadata-op-1",
    onStatus: (...args) => statuses.push(args),
    onChanged: (...args) => changes.push(args),
    ...options,
  }).mount();
  return { changes, documentRef, editor, root, statuses };
}


test("Corrections template mounts item metadata before shell boot", () => {
  const template = fs.readFileSync(path.join(
    __dirname, "..", "tools", "whl_explorer", "templates", "corrections.html",
  ), "utf8");
  assert.match(template,
    /data-item-properties[^>]+aria-label="Selected item metadata"/);
  assert.match(template, /corrections\/item-properties\.js/);
  assert.ok(
    template.indexOf("corrections/item-properties.js") <
      template.indexOf("corrections/shell.js"),
  );
});


test("Corrections item transport uses canonical endpoint, CAS, and idempotency", async () => {
  const calls = [];
  const loaded = item();
  const saved = item({
    title: "Catalogued herbal",
    metadata: { authors: "A. Botanist" },
    record_revision: "mir-r2",
  });
  const api = new CorrectionsItemApi({
    fetchImpl: async (url, init) => {
      calls.push({ url, init });
      return init.method === "GET"
        ? response(loadEnvelope(loaded))
        : response(mutationEnvelope(saved, false));
    },
  });

  const detail = await api.loadItem({ itemId: "capture-b9f1" });
  const result = await api.updateItem({
    itemId: detail.id,
    expectedRevision: detail.revision,
    operationId: "metadata-op-1",
    patch: {
      title: "Catalogued herbal",
      metadata_set: { authors: "A. Botanist" },
      metadata_remove: ["condition"],
    },
  });

  assert.equal(detail.revision, "mir-r1");
  assert.equal(result.item.revision, "mir-r2");
  assert.equal("record_revision" in result.item, false,
    "the UI normalizes the server revision into one canonical field");
  assert.deepEqual(calls.map(({ url, init }) => ({
    url,
    method: init.method,
    headers: init.headers,
    body: init.body ? JSON.parse(init.body) : null,
    cache: init.cache,
    credentials: init.credentials,
  })), [
    {
      url: "/api/v1/corrections/items/capture-b9f1",
      method: "GET",
      headers: { Accept: "application/json" },
      body: null,
      cache: "no-store",
      credentials: "same-origin",
    },
    {
      url: "/api/v1/corrections/items/capture-b9f1",
      method: "PATCH",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
        "If-Record-Match": "\"mir-r1\"",
        "Idempotency-Key": "metadata-op-1",
      },
      body: {
        patch: {
          title: "Catalogued herbal",
          metadata_set: { authors: "A. Botanist" },
          metadata_remove: ["condition"],
        },
      },
      cache: "no-store",
      credentials: "same-origin",
    },
  ]);
});


test("flat HTTP errors retain safe codes without treating operation reuse as revision drift",
  async () => {
    const api = new CorrectionsItemApi({
      fetchImpl: async () => response({
        ok: false,
        error: "That operation id was already used for another command",
        code: "operation_id_conflict",
        details: { operation_id: "metadata-op-1" },
      }, 409),
    });

    await assert.rejects(api.loadItem({ itemId: "capture-b9f1" }), (error) => {
      assert.equal(error.status, 409);
      assert.equal(error.code, "operation_id_conflict");
      assert.match(error.message, /already used/);
      assert.deepEqual(error.details, { operation_id: "metadata-op-1" });
      assert.equal(isConflict(error), false);
      return true;
    });
    assert.equal(isConflict({ status: 409, code: "item_revision_conflict" }), true);
  });


test("captured entries and books show canonical identity without storage details", async () => {
  const raw = item({
    active_storage_id: "manual-row-raw-key",
    metadata: {
      authors: "Unknown",
      storage_id: "manual-row-raw-key",
      capture_id: "private-capture-key",
      notes: "Leaf fragment enclosed",
      extra: {
        illustration: {
          caption: "Aloe, supplied by the cataloguer",
        },
      },
    },
  });
  const api = {
    async loadItem() { return raw; },
    async updateItem() { throw new Error("not used"); },
  };
  const { editor, root } = harness({ api });

  await editor.setSelection(raw.id);

  assert.match(root.textContent, /Captured entry/);
  assert.match(root.textContent, /capture-b9f1/);
  assert.equal(root.querySelector("input").value, "Uncatalogued herbal");
  assert.doesNotMatch(root.textContent, /manual-row-raw-key|private-capture-key/);
  assert.doesNotMatch(root.querySelector("textarea").value,
    /storage_id|capture_id|manual-row-raw-key|private-capture-key/);
  assert.deepEqual(editableMetadata(raw.metadata), {
    authors: "Unknown",
    extra: {
      illustration: {
        caption: "Aloe, supplied by the cataloguer",
      },
    },
    notes: "Leaf fragment enclosed",
  });

  raw.kind = "book";
  raw.id = "book-canonical";
  raw.record_revision = "book-r1";
  await editor.setSelection(raw.id);
  assert.match(root.textContent, /Book record/);
  assert.match(root.textContent, /book-canonical/);
});


test("metadata editor computes set/remove patches and reports committed state", async () => {
  const calls = [];
  const initial = item({
    metadata: {
      authors: "Unknown",
      publisher: "Field Press",
      capture_id: "managed",
    },
  });
  const saved = item({
    title: "The Field Herbal",
    metadata: {
      authors: "A. Green",
      notes: "Verified from title page",
    },
    record_revision: "mir-r2",
  });
  const api = {
    async loadItem() { return initial; },
    async updateItem(payload) {
      calls.push(payload);
      return { item: saved, replayed: false };
    },
  };
  const { changes, editor, root, statuses } = harness({ api });
  await editor.setSelection(initial.id);
  editor.updateDraft({
    title: "The Field Herbal",
    metadataText: JSON.stringify({
      authors: "A. Green",
      notes: "Verified from title page",
    }, null, 2),
  });

  const result = await editor.save();

  assert.equal(result.replayed, false);
  assert.deepEqual({
    itemId: calls[0].itemId,
    expectedRevision: calls[0].expectedRevision,
    operationId: calls[0].operationId,
    patch: calls[0].patch,
  }, {
    itemId: "capture-b9f1",
    expectedRevision: "mir-r1",
    operationId: "metadata-op-1",
    patch: {
      title: "The Field Herbal",
      metadata_set: {
        authors: "A. Green",
        notes: "Verified from title page",
      },
      metadata_remove: ["publisher"],
    },
  });
  assert.equal(editor.item.revision, "mir-r2");
  assert.equal(editor.draft.dirty, false);
  assert.equal(root.dataset.state, "ready");
  assert.match(root.textContent, /Metadata saved/);
  assert.equal(changes.length, 1);
  assert.match(statuses.at(-1)[0], /Metadata saved/);
});


test("loading and saving states remain visible while requests are in flight", async () => {
  const loading = deferred();
  const saving = deferred();
  const { editor, root } = harness({
    api: {
      loadItem() { return loading.promise; },
      updateItem() { return saving.promise; },
    },
  });

  const selected = editor.setSelection("capture-b9f1");
  assert.equal(root.dataset.state, "loading");
  assert.match(root.textContent, /Loading item metadata/);
  loading.resolve(item());
  await selected;

  editor.updateDraft({ title: "Corrected title" });
  const saved = editor.save();
  assert.equal(root.dataset.state, "saving");
  assert.match(root.textContent, /Saving metadata/);
  assert.equal(root.querySelector("input").disabled, true);
  saving.resolve({
    item: item({ title: "Corrected title", record_revision: "mir-r2" }),
    replayed: false,
  });
  await saved;
  assert.equal(root.dataset.state, "ready");
});


test("replayed saves are visibly distinguished from new commits", async () => {
  const initial = item();
  const saved = item({
    metadata: { authors: "A. Green" },
    record_revision: "mir-r2",
  });
  const { editor, root } = harness({
    api: {
      async loadItem() { return initial; },
      async updateItem() { return { item: saved, replayed: true }; },
    },
  });
  await editor.setSelection(initial.id);
  editor.updateDraft({
    metadataText: JSON.stringify({ authors: "A. Green" }, null, 2),
  });

  await editor.save();

  assert.equal(root.dataset.state, "replayed");
  assert.match(root.textContent, /save replayed/i);
});


test("revision conflicts reload current data and preserve attempted edits", async () => {
  let loads = 0;
  const initial = item();
  const latest = item({
    title: "Changed on another workstation",
    metadata: {
      authors: "Concurrent editor",
      publisher: "Concurrent Press",
    },
    record_revision: "mir-r2",
  });
  const conflict = Object.assign(new Error("revision conflict"), {
    status: 409,
    code: "record_revision_conflict",
  });
  const { editor, root, statuses } = harness({
    api: {
      async loadItem() {
        loads += 1;
        return loads === 1 ? initial : latest;
      },
      async updateItem() { throw conflict; },
    },
  });
  await editor.setSelection(initial.id);
  editor.updateDraft({
    title: "My corrected title",
    metadataText: JSON.stringify({
      authors: "My attribution",
      condition: "stable",
    }, null, 2),
  });

  await editor.save();

  assert.equal(loads, 2);
  assert.equal(editor.item.revision, "mir-r2");
  assert.equal(editor.draft.title, "My corrected title");
  assert.match(editor.draft.metadataText, /My attribution/);
  assert.match(editor.draft.metadataText, /Concurrent Press/,
    "the three-way merge retains metadata added by the concurrent writer");
  assert.equal(editor.draft.baseRevision, "mir-r2");
  assert.equal(editor.draft.dirty, true);
  assert.equal(root.querySelector("input").value, "My corrected title");
  assert.match(root.textContent, /changed elsewhere/i);
  assert.match(root.textContent, /edits were merged/i);
  assert.match(root.textContent, /authors|title/i);
  assert.equal(root.dataset.state, "error");
  assert.equal(statuses.at(-1)[1], true);
});


test("conflict reload becomes clean when the latest revision already has the draft", async () => {
  let loads = 0;
  const initial = item({ metadata: { authors: "Unknown" } });
  const latest = item({
    metadata: { authors: "A. Green" },
    record_revision: "mir-r2",
  });
  const conflict = Object.assign(new Error("revision conflict"), {
    status: 409,
    code: "item_revision_conflict",
  });
  const { editor, root } = harness({
    api: {
      async loadItem() {
        loads += 1;
        return loads === 1 ? initial : latest;
      },
      async updateItem() { throw conflict; },
    },
  });
  await editor.setSelection(initial.id);
  editor.updateDraft({
    metadataText: JSON.stringify({ authors: "A. Green" }, null, 2),
  });

  await editor.save();

  assert.equal(editor.item.revision, "mir-r2");
  assert.equal(editor.draft.dirty, false);
  assert.match(root.textContent, /already contains your draft changes/i);
});


test("unsaved metadata drafts survive rerenders and item navigation", async () => {
  const items = {
    "capture-b9f1": item(),
    "book-two": item({
      id: "book-two",
      kind: "book",
      title: "Second book",
      metadata: { authors: "B. Moss" },
      record_revision: "book-r1",
    }),
  };
  const drafts = new Map();
  const { editor, root } = harness({
    draftStore: drafts,
    api: {
      async loadItem({ itemId }) { return items[itemId]; },
      async updateItem() { throw new Error("not used"); },
    },
  });
  await editor.setSelection("capture-b9f1");
  editor.updateDraft({
    title: "Draft title",
    metadataText: JSON.stringify({ notes: "Unsaved field note" }, null, 2),
  });

  editor.render();
  assert.equal(root.querySelector("input").value, "Draft title");
  assert.match(root.querySelector("textarea").value, /Unsaved field note/);

  await editor.setSelection("book-two");
  items["capture-b9f1"] = item({
    metadata: {
      authors: "Unknown",
      condition: "foxed",
      publisher: "Added concurrently",
    },
    record_revision: "mir-r2",
  });
  await editor.setSelection("capture-b9f1");
  assert.equal(root.querySelector("input").value, "Draft title");
  assert.match(root.querySelector("textarea").value, /Unsaved field note/);
  assert.match(root.querySelector("textarea").value, /Added concurrently/);
  assert.equal(editor.draft.baseRevision, "mir-r2");
  assert.equal(editor.draft.dirty, true);
});


test("large valid metadata does not block a title-only patch", async () => {
  const description = "x".repeat(70 * 1024);
  assert.ok(description.length > 64 * 1024);
  assert.ok(description.length < MAX_METADATA_TEXT);
  const initial = item({ metadata: { description } });
  let update = null;
  const { editor } = harness({
    api: {
      async loadItem() { return initial; },
      async updateItem(payload) {
        update = payload;
        return {
          item: item({
            title: "Title-only correction",
            metadata: { description },
            record_revision: "mir-r2",
          }),
          replayed: false,
        };
      },
    },
  });
  await editor.setSelection(initial.id);
  editor.updateDraft({ title: "Title-only correction" });

  await editor.save();

  assert.deepEqual(update.patch, {
    title: "Title-only correction",
    metadata_set: {},
    metadata_remove: [],
  });
});


test("transport rejects an oversized metadata patch before issuing a request", async () => {
  let fetches = 0;
  const api = new CorrectionsItemApi({
    fetchImpl: async () => {
      fetches += 1;
      throw new Error("must not fetch");
    },
  });
  await assert.rejects(api.updateItem({
    itemId: "capture-b9f1",
    expectedRevision: "mir-r1",
    operationId: "metadata-op-large",
    patch: {
      title: null,
      metadata_set: { notes: "x".repeat(70 * 1024) },
      metadata_remove: [],
    },
  }), /64 KiB request limit/);
  assert.equal(fetches, 0);
});


test("server-managed metadata cannot be introduced through the JSON editor", async () => {
  let updates = 0;
  const { editor, root } = harness({
    api: {
      async loadItem() { return item(); },
      async updateItem() {
        updates += 1;
        throw new Error("must not be called");
      },
    },
  });
  await editor.setSelection("capture-b9f1");
  editor.updateDraft({
    metadataText: JSON.stringify({
      authors: "A. Green",
      storage_id: "manual-raw-key",
    }),
  });

  assert.equal(await editor.save(), null);
  assert.equal(updates, 0);
  assert.match(root.textContent, /Server-managed metadata cannot be edited/);
  assert.doesNotMatch(root.textContent, /manual-raw-key/);
  assert.equal(root.dataset.state, "error");
});


test("shell forwards canonical book and capture selection to item metadata", async () => {
  const selected = [];
  const shell = Object.create(CorrectionsShell.prototype);
  Object.assign(shell, {
    state: new CorrectionsWindowState(),
    booksFeature: null,
    artifactsFeature: null,
    itemProperties: {
      async setSelection(itemId) { selected.push(itemId); },
    },
    root: { querySelector: () => null },
    selectionListeners: new Set(),
    updateContextLabels() {},
    setStatus() {},
  });

  shell.selectAddress({ itemId: "capture-b9f1" }, { source: "books" });
  shell.selectAddress({ itemId: "book-two" }, { source: "books" });
  await Promise.resolve();

  assert.deepEqual(selected, ["capture-b9f1", "book-two"]);
});


test("patch construction treats title and metadata changes independently", () => {
  const source = item({
    metadata: { authors: "Unknown", year: "1897" },
  });
  const patch = patchFromDraft({
    ...source,
    revision: source.record_revision,
  }, {
    title: source.title,
    metadataText: JSON.stringify({ authors: "Known" }),
  });
  assert.deepEqual(patch, {
    title: null,
    metadata_set: { authors: "Known" },
    metadata_remove: ["year"],
  });
});

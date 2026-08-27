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
  chProvisionalPlan,
  createItemMetadataEditor,
  editableMetadata,
  isConflict,
  itemFormFields,
  metadataFieldLabel,
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
  const advanced = root.querySelector("[data-item-metadata-advanced]");
  assert.match(advanced.value, /Aloe, supplied by the cataloguer/);
  assert.doesNotMatch(advanced.value,
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
  assert.match(root.textContent, /Authors, Condition, Title/,
    "conflicts are reported with human field names");
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
  assert.equal(root.querySelector('[data-item-field="notes"]').value,
    "Unsaved field note");
  assert.equal(root.querySelector('[data-item-field="publisher"]').value,
    "Added concurrently");
  assert.match(editor.draft.metadataText, /Added concurrently/);
  assert.equal(editor.draft.baseRevision, "mir-r2");
  assert.equal(editor.draft.dirty, true);
});


test("external refresh rebases an unsaved capture draft onto current metadata",
  async () => {
    let loads = 0;
    const initial = item({
      metadata: { authors: "Unknown", condition: "foxed" },
    });
    const latest = item({
      metadata: {
        authors: "Unknown",
        condition: "foxed",
        publisher: "Added in another window",
      },
      record_revision: "mir-r2",
    });
    const { editor } = harness({
      api: {
        async loadItem() {
          loads += 1;
          return loads === 1 ? initial : latest;
        },
        async updateItem() { throw new Error("not used"); },
      },
    });
    await editor.setSelection(initial.id);
    editor.updateDraft({
      title: "My unsaved title",
      metadataText: JSON.stringify({
        authors: "Unknown",
        condition: "stable",
      }, null, 2),
    });

    await editor.refresh();

    assert.equal(loads, 2);
    assert.equal(editor.item.revision, "mir-r2");
    assert.equal(editor.draft.baseRevision, "mir-r2");
    assert.equal(editor.draft.title, "My unsaved title");
    assert.match(editor.draft.metadataText, /Added in another window/);
    assert.match(editor.draft.metadataText, /"condition": "stable"/);
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


test("typed form renders the manual field set from capture storage metadata", async () => {
  const raw = item({
    metadata: {
      active_storage_kind: "manual",
      authors: "Unknown",
      condition: "foxed",
      year: "1897",
      notes: "Leaf fragment enclosed",
      extra: { shelf: "B4" },
    },
  });
  const { editor, root } = harness({
    api: {
      async loadItem() { return raw; },
      async updateItem() { throw new Error("not used"); },
    },
  });

  await editor.setSelection(raw.id);

  assert.deepEqual(
    root.querySelectorAll("[data-item-field]")
      .map((node) => node.getAttribute("data-item-field")),
    itemFormFields("manual").map((field) => field.key));
  assert.deepEqual(itemFormFields("manual").map((field) => field.key), [
    "subtitle", "authors", "publisher", "publisher_city", "year", "edition",
    "volume", "language", "pages", "marked_price", "scan_priority",
    "scan_verdict", "condition", "price", "illustrations", "categories",
    "notes",
  ]);
  assert.equal(root.querySelector('[data-item-field="authors"]').value, "Unknown");
  assert.equal(root.querySelector('[data-item-field="condition"]').value, "foxed");
  assert.equal(root.querySelector('[data-item-field="year"]').value, "1897");
  assert.equal(root.querySelector('[data-item-field="subtitle"]').value, "");
  assert.equal(root.querySelector('[data-item-field="notes"]').tagName, "TEXTAREA");
  assert.equal(root.querySelector('[data-item-field="scan_priority"]').tagName,
    "SELECT");
  assert.match(root.textContent, /manual storage/);
  const advanced = root.querySelector("[data-item-metadata-advanced]");
  assert.deepEqual(JSON.parse(advanced.value), { extra: { shelf: "B4" } });
  assert.match(root.textContent, /1 key/,
    "the collapsed Advanced section counts its residual keys");
});


test("build-storage items add description and rights to the typed form", async () => {
  const raw = item({
    id: "book-two",
    kind: "book",
    title: "Second book",
    record_revision: "book-r1",
    metadata: {
      active_storage_kind: "build",
      authors: "B. Moss",
      description: "A field guide.",
      rights: "public-domain",
    },
  });
  const { editor, root } = harness({
    api: {
      async loadItem() { return raw; },
      async updateItem() { throw new Error("not used"); },
    },
  });

  await editor.setSelection(raw.id);

  assert.deepEqual(
    root.querySelectorAll("[data-item-field]")
      .map((node) => node.getAttribute("data-item-field")),
    itemFormFields("build").map((field) => field.key));
  const description = root.querySelector('[data-item-field="description"]');
  assert.equal(description.tagName, "TEXTAREA");
  assert.equal(description.value, "A field guide.");
  assert.equal(root.querySelector('[data-item-field="rights"]').value,
    "public-domain");
  assert.match(root.textContent, /catalogue storage/);
});


test("field set falls back by item kind when the projection omits storage kind",
  async () => {
    const raw = item();
    const { editor, root } = harness({
      api: {
        async loadItem() { return raw; },
        async updateItem() { throw new Error("not used"); },
      },
    });

    await editor.setSelection(raw.id);
    assert.ok(root.querySelector('[data-item-field="condition"]'));
    assert.equal(root.querySelector('[data-item-field="rights"]'), null);

    raw.kind = "book";
    raw.id = "book-canonical";
    raw.record_revision = "book-r1";
    await editor.setSelection(raw.id);
    assert.ok(root.querySelector('[data-item-field="rights"]'));
  });


test("typed field edits produce the same patch intents as the JSON editor", async () => {
  const calls = [];
  const initial = item({
    metadata: { authors: "Unknown", publisher: "Field Press" },
  });
  const saved = item({
    metadata: { authors: "A. Green" },
    record_revision: "mir-r2",
  });
  const { editor, root } = harness({
    api: {
      async loadItem() { return initial; },
      async updateItem(payload) {
        calls.push(payload);
        return { item: saved, replayed: false };
      },
    },
  });
  await editor.setSelection(initial.id);

  const authors = root.querySelector('[data-item-field="authors"]');
  authors.value = "A. Green";
  authors.emit("input");
  const publisher = root.querySelector('[data-item-field="publisher"]');
  publisher.value = "";
  publisher.emit("input");

  await editor.save();

  assert.deepEqual(calls[0].patch, {
    title: null,
    metadata_set: { authors: "A. Green" },
    metadata_remove: ["publisher"],
  });
  assert.equal(editor.item.revision, "mir-r2");
  assert.equal(editor.draft.dirty, false);
});


test("copy curation controls trim transcription edges and keep exact priority",
  async () => {
    const calls = [];
    const initial = item({ metadata: {} });
    const saved = item({
      metadata: {
        marked_price: "£ 2/6",
        scan_priority: "High",
        scan_verdict: "Scan the annotated copy.",
      },
      record_revision: "mir-r2",
    });
    const { editor, root } = harness({
      api: {
        async loadItem() { return initial; },
        async updateItem(payload) {
          calls.push(payload);
          return { item: saved, replayed: false };
        },
      },
    });
    await editor.setSelection(initial.id);

    const marked = root.querySelector('[data-item-field="marked_price"]');
    marked.value = "  £ 2/6  ";
    marked.emit("input");
    const priority = root.querySelector('[data-item-field="scan_priority"]');
    priority.value = "High";
    priority.emit("change");
    const verdict = root.querySelector('[data-item-field="scan_verdict"]');
    verdict.value = "  Scan the annotated copy.  ";
    verdict.emit("input");

    await editor.save();

    assert.deepEqual(calls[0].patch.metadata_set, saved.metadata);
    assert.deepEqual(calls[0].patch.metadata_remove, []);
  });


test("advanced JSON edits merge residual keys with typed field values", async () => {
  const initial = item({
    metadata: { authors: "Unknown", extra: { flag: true } },
  });
  const { editor, root } = harness({
    api: {
      async loadItem() { return initial; },
      async updateItem() { throw new Error("not used"); },
    },
  });
  await editor.setSelection(initial.id);

  const advanced = root.querySelector("[data-item-metadata-advanced]");
  assert.deepEqual(JSON.parse(advanced.value), { extra: { flag: true } });
  advanced.value = JSON.stringify({
    extra: { flag: false },
    source_url: "https://example.test/scan",
  });
  advanced.emit("input");

  assert.deepEqual(JSON.parse(editor.draft.metadataText), {
    authors: "Unknown",
    extra: { flag: false },
    source_url: "https://example.test/scan",
  });

  editor.render();
  assert.equal(root.querySelector('[data-item-field="authors"]').value,
    "Unknown");
  assert.deepEqual(
    JSON.parse(root.querySelector("[data-item-metadata-advanced]").value),
    { extra: { flag: false }, source_url: "https://example.test/scan" });
});


test("advanced JSON edits keep untouched empty-string fields out of the removal set",
  async () => {
    const calls = [];
    const initial = item({
      metadata: { authors: "Unknown", condition: "", extra: { flag: true } },
    });
    const saved = item({
      metadata: { authors: "Unknown", condition: "", extra: { flag: false } },
      record_revision: "mir-r2",
    });
    const { editor, root } = harness({
      api: {
        async loadItem() { return initial; },
        async updateItem(payload) {
          calls.push(payload);
          return { item: saved, replayed: false };
        },
      },
    });
    await editor.setSelection(initial.id);
    assert.equal(root.querySelector('[data-item-field="condition"]').value, "");

    const advanced = root.querySelector("[data-item-metadata-advanced]");
    advanced.value = JSON.stringify({ extra: { flag: false } });
    advanced.emit("input");

    assert.deepEqual(JSON.parse(editor.draft.metadataText), {
      authors: "Unknown",
      condition: "",
      extra: { flag: false },
    }, "a stored empty string is data, not a pending removal");

    await editor.save();
    assert.deepEqual(calls[0].patch, {
      title: null,
      metadata_set: { extra: { flag: false } },
      metadata_remove: [],
    }, "no removal is emitted for a field the user never touched");
  });


test("advanced JSON writes form keys through to the visible inputs", async () => {
  const initial = item({
    metadata: { authors: "Unknown", extra: { flag: true } },
  });
  const { editor, root } = harness({
    api: {
      async loadItem() { return initial; },
      async updateItem() { throw new Error("not used"); },
    },
  });
  await editor.setSelection(initial.id);

  const advanced = root.querySelector("[data-item-metadata-advanced]");
  advanced.value = JSON.stringify({
    authors: "A. Green",
    condition: "foxed",
    extra: { flag: true },
  });
  advanced.emit("input");

  assert.equal(root.querySelector('[data-item-field="authors"]').value,
    "A. Green", "the input reflects the Advanced edit without a rerender");
  assert.equal(root.querySelector('[data-item-field="condition"]').value,
    "foxed");
  assert.deepEqual(JSON.parse(editor.draft.metadataText), {
    authors: "A. Green",
    condition: "foxed",
    extra: { flag: true },
  });

  advanced.value = JSON.stringify({ extra: { flag: false } });
  advanced.emit("input");
  assert.deepEqual(JSON.parse(editor.draft.metadataText), {
    authors: "A. Green",
    condition: "foxed",
    extra: { flag: false },
  }, "a second Advanced edit merges from the draft, not stale inputs");
});


test("invalid advanced JSON blocks saving without losing typed fields", async () => {
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
  editor.updateDraft({ title: "Corrected" });
  const advanced = root.querySelector("[data-item-metadata-advanced]");
  advanced.value = "{ not json";
  advanced.emit("input");

  assert.equal(root.querySelector("[data-item-metadata-save]").disabled, true);
  assert.equal(await editor.save(), null);
  assert.equal(updates, 0);
  assert.match(root.textContent, /Advanced \(JSON\) metadata before saving/);
  assert.equal(root.querySelector("[data-item-metadata-advanced]").value,
    "{ not json");
  assert.equal(root.querySelector('[data-item-field="authors"]').value,
    "Unknown");
  assert.deepEqual(JSON.parse(editor.draft.metadataText),
    { authors: "Unknown", condition: "foxed" });

  const restored = root.querySelector("[data-item-metadata-advanced]");
  restored.value = "{}";
  restored.emit("input");
  assert.equal(editor.advancedError, "");
  assert.equal(root.querySelector("[data-item-metadata-save]").disabled, false);
  assert.deepEqual(JSON.parse(editor.draft.metadataText),
    { authors: "Unknown", condition: "foxed" });
});


test("server field errors mark the matching typed input", async () => {
  const failure = Object.assign(
    new Error("server-managed Corrections item fields cannot be changed here"), {
      status: 422,
      code: "managed_item_fields_not_writable",
      details: { fields: ["condition", "extra.book_id"] },
    });
  const { editor, root } = harness({
    api: {
      async loadItem() { return item(); },
      async updateItem() { throw failure; },
    },
  });
  await editor.setSelection("capture-b9f1");
  const condition = root.querySelector('[data-item-field="condition"]');
  condition.value = "rebound";
  condition.emit("input");

  await editor.save();

  const marked = root.querySelector('[data-item-field="condition"]');
  assert.equal(marked.getAttribute("aria-invalid"), "true");
  assert.match(root.textContent, /Server-managed; not writable/);
  assert.equal(
    root.querySelector("[data-item-metadata-advanced-section]").open, true,
    "errors on keys without a form input open the Advanced section");
  assert.match(root.textContent, /extra: Server-managed; not writable/);

  marked.value = "foxed";
  marked.emit("input");
  assert.equal(editor.fieldErrors, null);
  assert.equal(marked.getAttribute("aria-invalid"), null);
});


// --- CH provisional blank-fill ---------------------------------------------

const CH_KEY = "6e805628-39";

function chCandidateState(overrides = {}, fieldOverrides = {}) {
  return {
    ok: true,
    item_id: "capture-b9f1",
    list_available: true,
    match: null,
    candidates: [{
      index: 4,
      key: CH_KEY,
      title: "The Field Herbal",
      author: "Green, A.",
      year: "1897",
      score: 0.914,
      fields: {
        title: "The Field Herbal",
        author: "A. Green",
        year: "1897",
        publisher: "Field Press",
        city: "London",
        pages: "204",
        condition: "good",
        categories: "Herbal",
        ...fieldOverrides,
      },
    }],
    rejected: null,
    ...overrides,
  };
}


function provisionalHarness(metadata, overrides = {}) {
  const raw = item({ metadata, ...overrides });
  const calls = [];
  const saved = item({
    metadata: { ...metadata, year: "1901" },
    record_revision: "mir-r2",
  });
  return {
    raw,
    calls,
    ...harness({
      api: {
        async loadItem() { return raw; },
        async updateItem(payload) {
          calls.push(payload);
          return { item: saved, replayed: false };
        },
      },
    }),
  };
}


test("CH candidates provisionally fill only empty fields, display-only", async () => {
  const { editor, root } = provisionalHarness({
    authors: "Unknown",
    condition: "foxed",
    extra: { pages: "204 p." },
  });
  await editor.setSelection("capture-b9f1");
  editor.setChState(chCandidateState());

  const year = root.querySelector('[data-item-field="year"]');
  assert.equal(year.getAttribute("data-item-provisional"), "ch");
  assert.equal(year.getAttribute("placeholder"), "1897");
  assert.equal(year.getAttribute("title"), `CH candidate: ${CH_KEY}`);
  assert.equal(year.value, "",
    "the provisional value never becomes the input value");
  const publisher = root.querySelector('[data-item-field="publisher"]');
  assert.equal(publisher.getAttribute("placeholder"), "Field Press");
  assert.equal(
    root.querySelector('[data-item-field="publisher_city"]')
      .getAttribute("placeholder"), "London");
  assert.equal(
    root.querySelector('[data-item-field="categories"]')
      .getAttribute("placeholder"), "Herbal");

  const authors = root.querySelector('[data-item-field="authors"]');
  assert.equal(authors.getAttribute("data-item-provisional"), null,
    "an occupied field never previews an agreement");
  assert.equal(authors.getAttribute("placeholder"), null);
  const condition = root.querySelector('[data-item-field="condition"]');
  assert.equal(condition.getAttribute("data-item-provisional"), null,
    "a disagreement is the CH panel's conflict, never a provisional value");
  const pages = root.querySelector('[data-item-field="pages"]');
  assert.equal(pages.getAttribute("data-item-provisional"), null,
    "a value held in extra under the phone's field name blocks blank-fill");
  const title = root.querySelector("[data-item-title]");
  assert.equal(title.getAttribute("data-item-provisional"), null,
    "a real title never shows a provisional value");
  assert.equal(
    root.querySelector("[data-item-provisional-legend]").hidden, false,
    "the legend appears alongside provisional fields");
});


test("an ingest placeholder title counts as blank for provisional fill", async () => {
  const { editor, root } = provisionalHarness({}, {
    title: "(untitled capture b9f1a2c3)",
  });
  await editor.setSelection("capture-b9f1");
  editor.setChState(chCandidateState());

  const title = root.querySelector("[data-item-title]");
  assert.equal(title.getAttribute("data-item-provisional"), "ch");
  assert.equal(title.getAttribute("title"), `CH candidate: ${CH_KEY}`);
  assert.equal(title.getAttribute("placeholder"), "The Field Herbal");
  assert.equal(title.value, "(untitled capture b9f1a2c3)",
    "the stored placeholder text itself is never rewritten");
});


test("a stamped match or another item's state clears every provisional value",
  async () => {
    const { editor, root } = provisionalHarness({});
    await editor.setSelection("capture-b9f1");
    editor.setChState(chCandidateState());
    assert.ok(root.querySelectorAll("[data-item-provisional]").length > 0);

    editor.setChState(chCandidateState({
      match: { resolution: "key", key: CH_KEY },
      candidates: [],
    }));
    assert.equal(root.querySelectorAll("[data-item-provisional]").length, 0,
      "a stamped match means the reconciliation already happened");
    assert.equal(
      root.querySelector("[data-item-provisional-legend]").hidden, true);
    for (const input of root.querySelectorAll("[data-item-field]")) {
      assert.equal(input.getAttribute("placeholder"), null);
      assert.equal(input.getAttribute("title"), null);
    }

    editor.setChState(chCandidateState());
    assert.ok(root.querySelectorAll("[data-item-provisional]").length > 0);
    editor.setChState(chCandidateState({ item_id: "some-other-item" }));
    assert.equal(root.querySelectorAll("[data-item-provisional]").length, 0,
      "a late body for another item never decorates this one");
  });


test("a user edit replaces the provisional value and only real input saves",
  async () => {
    const { calls, editor, root } = provisionalHarness({
      authors: "Unknown",
    });
    await editor.setSelection("capture-b9f1");
    editor.setChState(chCandidateState());
    const year = root.querySelector('[data-item-field="year"]');
    assert.equal(year.getAttribute("data-item-provisional"), "ch");

    year.value = "1901";
    year.emit("input");

    assert.equal(year.getAttribute("data-item-provisional"), null);
    assert.equal(year.getAttribute("placeholder"), null);
    assert.equal(year.getAttribute("title"), null);
    assert.equal(
      root.querySelector('[data-item-field="publisher"]')
        .getAttribute("data-item-provisional"), "ch",
      "untouched empty fields keep their provisional display");

    await editor.save();

    assert.deepEqual(calls[0].patch, {
      title: null,
      metadata_set: { year: "1901" },
      metadata_remove: [],
    }, "the patch carries the typed value and no provisional field");
  });


test("provisional values are never serialized into drafts or patches", async () => {
  const { calls, editor, root } = provisionalHarness({ authors: "Unknown" });
  await editor.setSelection("capture-b9f1");
  editor.setChState(chCandidateState());
  assert.equal(root.querySelector('[data-item-field="year"]')
    .getAttribute("placeholder"), "1897");

  editor.updateDraft({ title: "Corrected title" });
  const draftMetadata = JSON.parse(editor.draft.metadataText);
  for (const key of ["year", "publisher", "publisher_city", "categories"]) {
    assert.equal(Object.prototype.hasOwnProperty.call(draftMetadata, key),
      false, `${key} must not leak into the draft`);
  }

  await editor.save();

  assert.deepEqual(calls[0].patch, {
    title: "Corrected title",
    metadata_set: {},
    metadata_remove: [],
  }, "a save while provisional values display commits none of them");
});


test("the never-serialized assertion detects the value-writing mutant", async () => {
  // The plausible regression: provisional display writing candidate values
  // into input.value instead of the placeholder attribute. The invalid-JSON
  // Advanced fallback merges from input values, so that mutant leaks the
  // candidate into the draft — first prove the correct implementation does
  // not leak through that exact path, then apply the mutation by hand and
  // show the same assertion trips, so the guard above is not vacuous.
  const clean = provisionalHarness({ authors: "Unknown" });
  await clean.editor.setSelection("capture-b9f1");
  clean.editor.setChState(chCandidateState());
  clean.editor.updateDraft({ metadataText: "{ not json" });
  const cleanAdvanced = clean.root.querySelector(
    "[data-item-metadata-advanced]");
  cleanAdvanced.value = "{}";
  cleanAdvanced.emit("input");
  assert.equal(
    Object.prototype.hasOwnProperty.call(
      JSON.parse(clean.editor.draft.metadataText), "year"), false,
    "a placeholder survives the fallback merge without leaking");

  const mutant = provisionalHarness({ authors: "Unknown" });
  await mutant.editor.setSelection("capture-b9f1");
  mutant.editor.setChState(chCandidateState());
  const year = mutant.root.querySelector('[data-item-field="year"]');
  year.value = year.getAttribute("placeholder"); // the mutation
  mutant.editor.updateDraft({ metadataText: "{ not json" });
  const advanced = mutant.root.querySelector("[data-item-metadata-advanced]");
  advanced.value = "{}";
  advanced.emit("input");
  assert.equal(JSON.parse(mutant.editor.draft.metadataText).year, "1897",
    "the mutant leaks the candidate value, so the guard assertion catches it");
});


test("chProvisionalPlan mirrors the server merge plan's blank-fill rules", () => {
  const base = { id: "capture-b9f1", kind: "capture", storageKind: "manual" };
  const draft = {
    title: "",
    metadataText: JSON.stringify({ authors: "" }),
  };
  const plan = chProvisionalPlan(base, draft, chCandidateState());
  assert.equal(plan.key, CH_KEY);
  assert.equal(plan.title, "The Field Herbal");
  assert.deepEqual(plan.values, {
    authors: "A. Green",
    year: "1897",
    publisher: "Field Press",
    publisher_city: "London",
    pages: "204",
    condition: "good",
    categories: "Herbal",
  });
  assert.equal("edition" in plan.values, false,
    "a blank CH column never fills anything");

  const buildPlan = chProvisionalPlan(
    { ...base, kind: "book", storageKind: "build" }, draft,
    chCandidateState());
  assert.equal("condition" in buildPlan.values, false,
    "build storage has no columns for condition/illustrations/price");
  assert.equal(buildPlan.values.publisher, "Field Press");

  assert.equal(chProvisionalPlan(base, draft, chCandidateState({
    match: { resolution: "key", key: CH_KEY },
  })), null, "a stamped match never previews");
  assert.equal(chProvisionalPlan(base, draft,
    chCandidateState({ item_id: "other" })), null);
  assert.equal(chProvisionalPlan(base, draft,
    chCandidateState({ candidates: [] })), null);
  assert.equal(chProvisionalPlan(base,
    { title: "", metadataText: "{ nope" }, chCandidateState()), null,
  "an unparsable draft offers no safe view of which fields are empty");
});


test("conflict reporting names human field labels", () => {
  assert.equal(metadataFieldLabel("publisher_city"), "Publisher city");
  assert.equal(metadataFieldLabel("title"), "Title");
  assert.equal(metadataFieldLabel("rights"), "Rights");
  assert.equal(metadataFieldLabel("custom_key"), "custom_key");
});

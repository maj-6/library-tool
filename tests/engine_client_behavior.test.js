const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const clientPath = path.join(
  __dirname, "..", "tools", "whl_explorer", "static", "engine-client.js");
const appPath = path.join(
  __dirname, "..", "tools", "whl_explorer", "static", "app.js");
const templatePath = path.join(
  __dirname, "..", "tools", "whl_explorer", "templates", "index.html");
const clientSource = fs.readFileSync(clientPath, "utf8");
const { EngineClient, EngineClientError } = require(clientPath);

function response(status, body) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  };
}

function harness(body = { ok: true }) {
  const calls = [];
  const client = new EngineClient({
    transport: async (url, init) => {
      calls.push({ url, init });
      return response(200, body);
    },
  });
  return { client, calls };
}

test("EngineClient exposes the complete Replica compatibility surface", () => {
  const { client } = harness();
  assert.equal(typeof client.capabilities, "function");
  assert.equal(typeof client.ocr.layout, "function");
  assert.equal(typeof client.jobs.list, "function");
  assert.equal(typeof client.jobs.get, "function");
  assert.equal(typeof client.jobs.cancel, "function");
  assert.equal(typeof client.jobs.events, "function");
  assert.equal(typeof client.items.list, "function");
  assert.equal(typeof client.items.get, "function");
  assert.equal(typeof client.items.create, "function");
  assert.equal(typeof client.items.update, "function");
  assert.equal(typeof client.items.representations, "function");
  assert.equal(typeof client.items.attachRepresentation, "function");
  assert.equal(typeof client.items.replaceRepresentation, "function");
  assert.equal(typeof client.items.detachRepresentation, "function");
  assert.equal(typeof client.items.artifacts, "function");
  assert.equal(typeof client.items.readiness, "function");
  const jsonMethods = [
    client.replica.templates.list,
    client.pdf.info,
    client.replica.pages.get,
    client.replica.detection.start,
    client.pdf.words,
    client.replica.figures.rework,
    client.replica.proposals.decide,
    client.replica.pages.save,
    client.replica.pages.recompile,
    client.replica.templates.saveFromPage,
    client.replica.templates.apply,
    client.replica.templates.outliers,
    client.replica.styles.get,
    client.translations.list,
    client.translations.get,
    client.translations.replacePage,
    client.replica.styles.save,
    client.replica.instructions.get,
    client.replica.instructions.save,
    client.replica.styles.reset,
    client.replica.packages.open,
    client.replica.packages.import,
  ];
  const urlBuilders = [
    client.pdf.pageImageUrl,
    client.replica.pages.imageUrl,
    client.replica.figures.imageUrl,
    client.replica.packages.exportUrl,
    client.replica.printUrl,
  ];
  assert.equal(jsonMethods.length, 22);
  assert.ok(jsonMethods.every((method) => typeof method === "function"));
  assert.equal(urlBuilders.length, 5);
  assert.ok(urlBuilders.every((method) => typeof method === "function"));
});

test("EngineClient exposes versioned capability discovery", async () => {
  const { client, calls } = harness({
    ok: true, schema: "librarytool.capabilities/1",
  });
  const result = await client.capabilities();
  assert.equal(result.schema, "librarytool.capabilities/1");
  assert.equal(calls[0].url, "/api/v1/capabilities");
  assert.equal(calls[0].init.method, "GET");
});

test("shared OCR layout metadata also crosses the client boundary", async () => {
  const { client, calls } = harness({ ok: true, region_pages: {} });
  await client.ocr.layout({ bookId: "book / one" });
  assert.equal(calls[0].url, "/api/builds/book%20%2F%20one/ocr-layout");
  assert.equal(calls[0].init.method, "GET");
});

test("background jobs use the versioned engine transport", async () => {
  const { client, calls } = harness({ ok: true, jobs: [] });
  await client.jobs.list({
    state: ["running", "cancelling"], kind: "ocr", itemId: "book / one",
  });
  await client.jobs.get({ jobId: "job / one" });
  await client.jobs.cancel({ jobId: "job / one" });
  await client.jobs.events({ after: 12, limit: 50 });

  assert.equal(calls[0].url,
    "/api/v1/jobs?state=running%2Ccancelling&kind=ocr&item_id=book%20%2F%20one");
  assert.equal(calls[0].init.method, "GET");
  assert.equal(calls[1].url, "/api/v1/jobs/job%20%2F%20one");
  assert.equal(calls[1].init.method, "GET");
  assert.equal(calls[2].url, "/api/v1/jobs/job%20%2F%20one/cancel");
  assert.equal(calls[2].init.method, "POST");
  assert.equal(calls[3].url, "/api/v1/job-events?after=12&limit=50");
  assert.equal(calls[3].init.method, "GET");
});

test("item queries use versioned path-safe engine resources", async () => {
  const { client, calls } = harness({ ok: true, items: [] });
  await client.items.list({ includeBuildCompatibility: true });
  await client.items.get({ itemId: "book / one" });
  await client.items.representations({ itemId: "book / one" });
  await client.items.artifacts({ itemId: "book / one" });
  await client.items.readiness({ itemId: "book / one" });

  assert.equal(calls[0].url,
    "/api/v1/items?projection=build-workbench");
  assert.equal(calls[1].url, "/api/v1/items/book%20%2F%20one");
  assert.equal(calls[2].url,
    "/api/v1/items/book%20%2F%20one/representations");
  assert.equal(calls[3].url,
    "/api/v1/items/book%20%2F%20one/artifacts");
  assert.equal(calls[4].url,
    "/api/v1/items/book%20%2F%20one/readiness");
  assert.ok(calls.every(({ init }) => init.method === "GET"));
});

test("item commands use versioned idempotent JSON contracts", async () => {
  const { client, calls } = harness({ ok: true, item: { id: "book-1" } });
  const item = {
    kind: "book",
    title: "A Book",
    metadata: {},
    representations: [],
  };
  const patch = {
    title: "A Revised Book",
    metadata_set: { cataloguer: "Ada" },
    metadata_remove: [],
    representations: null,
  };

  await client.items.create({
    item,
    idempotencyKey: "item-create-1",
  });
  await client.items.update({
    itemId: "book / one!*",
    patch,
    recordRevision: "ir-current",
    idempotencyKey: "item-update-1",
  });

  assert.equal(calls[0].url, "/api/v1/items");
  assert.equal(calls[0].init.method, "POST");
  assert.deepEqual(calls[0].init.headers, {
    Accept: "application/json",
    "Idempotency-Key": "item-create-1",
    "Content-Type": "application/json",
  });
  assert.deepEqual(JSON.parse(calls[0].init.body), { item });

  assert.equal(calls[1].url, "/api/v1/items/book%20%2F%20one%21%2A");
  assert.equal(calls[1].init.method, "PATCH");
  assert.deepEqual(calls[1].init.headers, {
    Accept: "application/json",
    "Idempotency-Key": "item-update-1",
    "If-Record-Match": '"ir-current"',
    "Content-Type": "application/json",
  });
  assert.deepEqual(JSON.parse(calls[1].init.body), { patch });
});

test("item commands reject missing or unsafe preconditions locally", () => {
  const { client, calls } = harness();
  const item = {
    kind: "book", title: "A Book", metadata: {}, representations: [],
  };
  const patch = {
    title: "Revised", metadata_set: {}, metadata_remove: [],
    representations: null,
  };

  assert.throws(
    () => client.items.create({ item }),
    (error) => error instanceof TypeError && /idempotencyKey/.test(error.message),
  );
  assert.throws(
    () => client.items.create({ item, idempotencyKey: "unsafe/key" }),
    (error) => error instanceof TypeError && /idempotencyKey/.test(error.message),
  );
  assert.throws(
    () => client.items.update({
      itemId: "book-1", patch, recordRevision: "ir-current",
    }),
    (error) => error instanceof TypeError && /idempotencyKey/.test(error.message),
  );
  assert.throws(
    () => client.items.update({
      itemId: "book-1", patch, recordRevision: "ir-current",
      idempotencyKey: "unsafe/key",
    }),
    (error) => error instanceof TypeError && /idempotencyKey/.test(error.message),
  );
  assert.throws(
    () => client.items.update({
      itemId: "book-1", patch, idempotencyKey: "item-update-1",
    }),
    (error) => error instanceof TypeError && /recordRevision/.test(error.message),
  );
  assert.throws(
    () => client.items.update({
      itemId: "book-1", patch, recordRevision: 'W/"ir-current"',
      idempotencyKey: "item-update-1",
    }),
    (error) => error instanceof TypeError && /recordRevision/.test(error.message),
  );
  for (const recordRevision of ["*", "ir current", "ir/current", `ir-${"x".repeat(512)}`]) {
    assert.throws(
      () => client.items.update({
        itemId: "book-1", patch, recordRevision,
        idempotencyKey: "item-update-1",
      }),
      (error) => error instanceof TypeError && /recordRevision/.test(error.message),
    );
  }
  assert.equal(calls.length, 0);
});

test("representation commands use path-safe dual-CAS engine resources", async () => {
  const { client, calls } = harness({ ok: true, receipt: {} });
  const representation = {
    source_token: "C:\\Scans\\herbal.pdf",
    acquisition: "reference",
    expected_content_sha256: "",
    expected_size: null,
    role: "alternate",
    media_type: "application/pdf",
    label: "Alternate scan",
    metadata: { shelf: "A-2" },
  };

  await client.items.attachRepresentation({
    itemId: "book / one",
    representationId: "scan !*",
    representation,
    recordRevision: "item-r1",
    idempotencyKey: "attach-1",
  });
  await client.items.replaceRepresentation({
    itemId: "book / one",
    representationId: "scan !*",
    representation,
    recordRevision: "item-r2",
    representationRevision: "source-r1",
    idempotencyKey: "replace-1",
  });
  await client.items.detachRepresentation({
    itemId: "book / one",
    representationId: "scan !*",
    recordRevision: "item-r3",
    representationRevision: "source-r2",
    idempotencyKey: "detach-1",
  });

  const url = "/api/v1/items/book%20%2F%20one/representations/scan%20%21%2A";
  assert.deepEqual(calls.map((call) => [call.url, call.init.method]), [
    [url, "PUT"], [url, "PUT"], [url, "DELETE"],
  ]);
  assert.equal(calls[0].init.headers["If-Representation-Match"], undefined);
  assert.equal(calls[0].init.headers["If-Record-Match"], '"item-r1"');
  assert.equal(calls[1].init.headers["If-Representation-Match"],
    '"source-r1"');
  assert.deepEqual(JSON.parse(calls[0].init.body), { representation });
  assert.equal(calls[2].init.headers["If-Representation-Match"],
    '"source-r2"');
  assert.equal(calls[2].init.body, undefined);
});

test("representation commands reject ambiguous or missing preconditions locally", () => {
  const { client, calls } = harness();
  const base = {
    itemId: "book", representationId: "scan", representation: {},
    recordRevision: "item-r1", idempotencyKey: "representation-1",
  };
  assert.throws(
    () => client.items.attachRepresentation({
      ...base, representationRevision: "source-r1",
    }), /does not accept representationRevision/);
  assert.throws(
    () => client.items.replaceRepresentation(base),
    /representationRevision/);
  assert.throws(
    () => client.items.detachRepresentation({
      itemId: "book", representationId: "scan",
      recordRevision: "item-r1", idempotencyKey: "detach-1",
    }), /representationRevision/);
  assert.equal(calls.length, 0);
});

test("translations use versioned aggregate reads and dual-CAS page writes", async () => {
  const { client, calls } = harness({
    ok: true, translation: { id: "translation-1" },
  });
  await client.translations.list({ itemId: "book / one" });
  await client.translations.get({
    itemId: "book / one", translationId: "translation / one",
  });
  await client.translations.replacePage({
    itemId: "book / one", translationId: "translation / one",
    selector: "page:7", text: "Nueva.",
    documentRevision: "tr-current", sourceRevision: "ts-current",
  });

  assert.equal(calls[0].url,
    "/api/v1/items/book%20%2F%20one/translations");
  assert.equal(calls[0].init.method, "GET");
  assert.equal(calls[1].url,
    "/api/v1/items/book%20%2F%20one/translations/translation%20%2F%20one");
  assert.equal(calls[1].init.method, "GET");
  assert.equal(calls[2].url,
    "/api/v1/items/book%20%2F%20one/translations/translation%20%2F%20one" +
    "/pages/page%3A7");
  assert.equal(calls[2].init.method, "PUT");
  assert.equal(
    calls[2].init.headers["If-Document-Match"], '"tr-current"');
  assert.equal(calls[2].init.headers["If-Source-Match"], '"ts-current"');
  assert.deepEqual(JSON.parse(calls[2].init.body), {
    text: "Nueva.",
    expected_document_revision: "tr-current",
    expected_source_revision: "ts-current",
  });
});

test("translation page writes reject missing revision tokens locally", () => {
  const { client, calls } = harness();
  assert.throws(() => client.translations.replacePage({
    itemId: "book", translationId: "translation-1",
    selector: "page:1", text: "Nueva.", sourceRevision: "ts-current",
  }), /documentRevision/);
  assert.throws(() => client.translations.replacePage({
    itemId: "book", translationId: "translation-1",
    selector: "page:1", text: "Nueva.", documentRevision: "tr-current",
  }), /sourceRevision/);
  assert.equal(calls.length, 0);
});

test("EngineClient encodes Replica path components and query values", async () => {
  const { client, calls } = harness({ ok: true, found: false, revision: "rr-1" });
  await client.replica.pages.get({
    bookId: "book /alpha", sourceId: "scan !'()* /二", page: 7,
  });

  assert.equal(calls.length, 1);
  assert.equal(calls[0].url,
    "/api/builds/book%20%2Falpha/ocr-regions" +
    "?src=scan%20%21%27%28%29%2A%20%2F%E4%BA%8C&page=7");
  assert.equal(calls[0].init.method, "GET");
  assert.deepEqual(calls[0].init.headers, { Accept: "application/json" });
  assert.equal(calls[0].init.body, undefined);
});

test("page save owns JSON encoding and the If-Match contract", async () => {
  const { client, calls } = harness({ ok: true, revision: "rr-next" });
  const items = [{ rid: "region-1", role: "body" }];
  await client.replica.pages.save({
    bookId: "book-1", sourceId: "primary", page: 4, revision: "rr-old",
    record: {
      doc: "compiled.txt", dims: { w: 100, h: 200 }, ext: { keep: true },
      state: "verified", items,
    },
  });

  const { url, init } = calls[0];
  assert.equal(url, "/api/builds/book-1/ocr-regions");
  assert.equal(init.method, "PUT");
  assert.equal(init.headers["If-Match"], '"rr-old"');
  assert.equal(init.headers["Content-Type"], "application/json");
  assert.deepEqual(JSON.parse(init.body), {
    src: "primary", page: 4, doc: "compiled.txt",
    dims: { w: 100, h: 200 }, ext: { keep: true },
    state: "verified", items, expect_revision: "rr-old",
  });
});

test("Replica region detection starts a versioned page-scoped job", async () => {
  const { client, calls } = harness({
    ok: true,
    provider: "mistral",
    job: { id: "job-1", state: "running" },
  });
  const result = await client.replica.detection.start({
    bookId: "book / one", sourceId: "scan 2", page: 17,
    revision: "rr-current", provider: "automatic",
    idempotencyKey: "detect-command-1",
  });

  assert.equal(result.job.id, "job-1");
  assert.equal(calls[0].url,
    "/api/v1/items/book%20%2F%20one/replica/region-detection-jobs");
  assert.equal(calls[0].init.method, "POST");
  assert.equal(calls[0].init.headers["If-Match"], '"rr-current"');
  assert.deepEqual(JSON.parse(calls[0].init.body), {
    source_id: "scan 2", page: 17, provider: "automatic",
    expect_revision: "rr-current", idempotency_key: "detect-command-1",
  });
});

test("proposal decisions own both conditional revisions", async () => {
  const { client, calls } = harness({ ok: true });
  await client.replica.proposals.decide({
    bookId: "book", sourceId: "scan", page: 9, action: "apply",
    revision: "rr-current", proposalRevision: "rp-current",
  });

  const { init } = calls[0];
  assert.equal(init.method, "POST");
  assert.equal(init.headers["If-Match"], '"rr-current"');
  assert.equal(init.headers["If-Proposal-Match"], '"rp-current"');
  assert.deepEqual(JSON.parse(init.body), {
    src: "scan", page: 9, action: "apply",
    expect_revision: "rr-current",
    expect_proposal_revision: "rp-current",
  });
});

test("Replica import uses the versioned idempotent multipart contract", async () => {
  class FakeFormData {
    constructor() { this.entries = []; }
    append(name, value) { this.entries.push([name, value]); }
  }
  const calls = [];
  const client = new EngineClient({
    transport: async (url, init) => {
      calls.push({ url, init });
      return response(200, { ok: true });
    },
    formDataFactory: () => new FakeFormData(),
  });
  const file = { name: "edition.lib" };
  await client.replica.packages.import({
    bookId: "book one", sourceId: "scan & notes", file,
    overwrite: true, idempotencyKey: "import-command-1",
  });

  const { url, init } = calls[0];
  assert.equal(url,
    "/api/v1/items/book%20one/replica/lib-imports" +
    "?source_id=scan%20%26%20notes&overwrite=1");
  assert.equal(init.method, "POST");
  assert.deepEqual(init.body.entries, [["lib", file]]);
  assert.equal(init.headers["Idempotency-Key"], "import-command-1");
  assert.equal(init.headers["Content-Type"], undefined);
  assert.equal(init.headers.Accept, "application/json");
});

test("Replica open uses the composite new-item multipart contract", async () => {
  class FakeFormData {
    constructor() { this.entries = []; }
    append(name, value) { this.entries.push([name, value]); }
  }
  const calls = [];
  const client = new EngineClient({
    transport: async (url, init) => {
      calls.push({ url, init });
      return response(201, { ok: true, replayed: false });
    },
    formDataFactory: () => new FakeFormData(),
  });
  const file = { name: "edition.lib" };

  await client.replica.packages.open({
    file, idempotencyKey: "open-command-1",
  });

  const { url, init } = calls[0];
  assert.equal(url, "/api/v1/lib-opens");
  assert.equal(init.method, "POST");
  assert.deepEqual(init.body.entries, [["lib", file]]);
  assert.equal(init.headers["Idempotency-Key"], "open-command-1");
  assert.equal(init.headers["Content-Type"], undefined);
});

test("Replica package commands reject unsafe idempotency keys locally", () => {
  class FakeFormData {
    append() {}
  }
  let calls = 0;
  const client = new EngineClient({
    transport: async () => {
      calls += 1;
      return response(200, { ok: true });
    },
    formDataFactory: () => new FakeFormData(),
  });

  assert.throws(
    () => client.replica.packages.import({
      bookId: "book", sourceId: "primary", file: {},
    }),
    (error) => error instanceof TypeError && /idempotencyKey/.test(error.message),
  );
  assert.throws(
    () => client.replica.packages.import({
      bookId: "book", sourceId: "primary", file: {},
      idempotencyKey: "unsafe/key",
    }),
    (error) => error instanceof TypeError && /idempotencyKey/.test(error.message),
  );
  assert.throws(
    () => client.replica.packages.open({ file: {} }),
    (error) => error instanceof TypeError && /idempotencyKey/.test(error.message),
  );
  assert.equal(calls, 0);
});

test("EngineClientError preserves structured engine conflict details", async () => {
  const client = new EngineClient({
    transport: async () => response(409, {
      ok: false,
      error: "regions changed elsewhere",
      code: "revision-conflict",
      conflict: { expected: "rr-old", actual: "rr-new" },
    }),
  });

  await assert.rejects(
    client.replica.pages.get({ bookId: "book", sourceId: "primary", page: 1 }),
    (error) => {
      assert.ok(error instanceof EngineClientError);
      assert.equal(error.message, "regions changed elsewhere");
      assert.equal(error.status, 409);
      assert.equal(error.code, "revision-conflict");
      assert.deepEqual(error.conflict,
        { expected: "rr-old", actual: "rr-new" });
      assert.equal(error.retryable, false);
      assert.equal(error.method, "GET");
      assert.match(error.url, /ocr-regions/);
      return true;
    });
});

test("malformed and network responses are normalized as EngineClientError", async () => {
  const malformed = new EngineClient({
    transport: async () => ({
      ok: true, status: 200, json: async () => { throw new SyntaxError("bad"); },
    }),
  });
  await assert.rejects(
    malformed.pdf.info({ path: "book.pdf" }),
    (error) => error instanceof EngineClientError &&
      error.code === "invalid-response" && error.status === 200);

  const offline = new EngineClient({
    transport: async () => { throw new Error("offline"); },
  });
  await assert.rejects(
    offline.pdf.info({ path: "book.pdf" }),
    (error) => error instanceof EngineClientError &&
      error.code === "network-error" && error.retryable);
});

test("URL builders centralize all Replica resource routes", () => {
  const { client } = harness();
  const page = client.pdf.pageImageUrl({
    path: "source #1.pdf", page: 3, width: 1100,
  });
  assert.equal(page,
    "/api/pdf/pageimg?path=source%20%231.pdf&page=3&w=1100");
  assert.equal(client.replica.pages.imageUrl({
    pdfPath: "source #1.pdf", page: 3, width: 1100,
  }), page);
  assert.equal(client.replica.figures.imageUrl({
    bookId: "book / 1", name: "plate #1.png",
  }), "/api/builds/book%20%2F%201/ocr/images/plate%20%231.png");
  assert.equal(client.replica.packages.exportUrl({
    bookId: "book", sourceId: "scan two",
  }), "/api/builds/book/replica-export?src=scan%20two");
  assert.equal(client.replica.printUrl({
    bookId: "book", sourceId: "primary", layer: "français",
  }), "/api/builds/book/replica-print?src=primary&layer=fran%C3%A7ais");
  assert.equal(client.replica.printUrl({
    bookId: "book", sourceId: "primary", layer: "",
  }), "/api/builds/book/replica-print?src=primary");
});

test("classic browser script exposes one global engineClient instance", () => {
  class FakeFormData { append() {} }
  const sandbox = {
    fetch: async () => response(200, { ok: true }),
    FormData: FakeFormData,
  };
  sandbox.window = sandbox;
  vm.runInNewContext(clientSource, sandbox, { filename: "engine-client.js" });

  assert.equal(typeof sandbox.EngineClient, "function");
  assert.equal(typeof sandbox.EngineClientError, "function");
  assert.ok(sandbox.engineClient instanceof sandbox.EngineClient);
  const first = sandbox.engineClient;
  vm.runInNewContext(clientSource, sandbox, { filename: "engine-client.js" });
  assert.equal(sandbox.engineClient, first);
});

test("Replica workbench contains no direct transport or API route literals", () => {
  const app = fs.readFileSync(appPath, "utf8");
  const start = app.indexOf("// --- Replica workbench");
  const end = app.indexOf("function initOcrTab", start);
  assert.ok(start >= 0 && end > start, "Replica source boundaries are present");
  const replica = app.slice(start, end);
  assert.doesNotMatch(replica, /\bfetch\s*\(/);
  assert.doesNotMatch(replica, /["'`]\/api\//);

  const template = fs.readFileSync(templatePath, "utf8");
  const clientScript = template.indexOf("filename='engine-client.js'");
  const appScript = template.indexOf("filename='app.js'");
  assert.ok(clientScript >= 0 && clientScript < appScript,
    "engine-client.js loads before app.js");
});

test("Replica translation preview consumes aggregate summaries and selectors", () => {
  const app = fs.readFileSync(appPath, "utf8");
  const start = app.indexOf("async function rwLoadTranslations");
  const end = app.indexOf("let rwRenderSeq", start);
  assert.ok(start >= 0 && end > start);
  const preview = app.slice(start, end);
  assert.match(preview, /translations\.list\(\{\s*itemId:/);
  assert.match(preview, /t\.id\s*&&\s*t\.target_language/);
  assert.match(preview, /translations\.get\(\{[\s\S]*translationId:\s*layer/);
  assert.match(preview, /translationPage\.selector/);
  assert.match(preview, /sec\.get\(`page:\$\{page\}`\)/);
  assert.doesNotMatch(preview, /ocrPageSections\(r\.text/);

  const printStart = app.indexOf('el("rw-print").addEventListener');
  const printEnd = app.indexOf('el("rw-import-file").addEventListener', printStart);
  const print = app.slice(printStart, printEnd);
  assert.match(print, /transSummaries\.find/);
  assert.match(print, /summary\s*\?\s*summary\.target_language/);
});

test("the initial build load crosses the semantic item client boundary", () => {
  const app = fs.readFileSync(appPath, "utf8");
  const start = app.indexOf("async function loadBuilds()");
  const end = app.indexOf("function allBuildsSorted", start);
  assert.ok(start >= 0 && end > start);
  const loader = app.slice(start, end);
  assert.match(loader, /engineClient\.items\.list/);
  assert.match(loader, /includeBuildCompatibility:\s*true/);
  // Existing mutation routes remain transitional; only the collection GET
  // belongs to this slice.
  assert.doesNotMatch(loader, /fetch\s*\(\s*["']\/api\/builds["']/);
});

test("build projections retain item command eligibility", () => {
  const app = fs.readFileSync(appPath, "utf8");
  const start = app.indexOf("function engineBuildProjection");
  const end = app.indexOf("async function loadBuilds", start);
  assert.ok(start >= 0 && end > start);
  const context = vm.createContext({});
  vm.runInContext(`${app.slice(start, end)}
this.project = engineBuildProjection;`, context);
  const item = {
    id: "book", title: "Herbal", record_revision: "item-r1",
    compatibility: { build: { id: "legacy", title: "Old" } },
    representations: [],
    workbench_state: { available_commands: [
      "representation.attach", "representation.attach", "replica.open",
    ] },
  };

  const build = context.project(item);
  assert.deepEqual(Array.from(build._available_commands), [
    "representation.attach", "replica.open",
  ]);
  item.workbench_state.available_commands.push("representation.detach");
  assert.equal(build._available_commands.includes("representation.detach"), false);
});

test("late semantic item reads cannot replace a newer incarnation", async () => {
  const app = fs.readFileSync(appPath, "utf8");
  const refreshStart = app.indexOf("async function refreshBuildEngineRecord");
  const refreshEnd = app.indexOf("function representationOperationKey", refreshStart);
  const generationStart = app.indexOf("const buildEngineRecordGenerations");
  const generationEnd = app.indexOf("let secondaryRepresentationSequence", generationStart);
  assert.ok(refreshStart >= 0 && refreshEnd > refreshStart);
  assert.ok(generationStart >= 0 && generationEnd > generationStart);

  const pendingReads = [];
  const state = { builds: {
    book: { id: "book", _record_revision: "item-r1", marker: "initial" },
  } };
  const context = vm.createContext({
    state,
    engineClient: { items: { get: () => new Promise((resolve) => {
      pendingReads.push(resolve);
    }) } },
    engineBuildProjection: (item) => item.build,
  });
  vm.runInContext(`${app.slice(generationStart, generationEnd)}
${app.slice(refreshStart, refreshEnd)}
this.api = { refreshBuildEngineRecord, advanceBuildEngineRecordGeneration };`, context);

  const staleAfterReceipt = context.api.refreshBuildEngineRecord("book");
  context.api.advanceBuildEngineRecordGeneration("book");
  state.builds.book = {
    id: "book", _record_revision: "item-r2", marker: "receipt",
  };
  pendingReads.shift()({
    item: { build: {
      id: "book", _record_revision: "item-r1", marker: "stale-read",
    } },
  });
  await staleAfterReceipt;
  assert.equal(state.builds.book.marker, "receipt");

  const staleAfterDelete = context.api.refreshBuildEngineRecord("book");
  context.api.advanceBuildEngineRecordGeneration("book");
  delete state.builds.book;
  pendingReads.shift()({
    item: { build: {
      id: "book", _record_revision: "item-r2", marker: "deleted-read",
    } },
  });
  await staleAfterDelete;
  assert.equal("book" in state.builds, false);
});

test("interactive PDF attachment crosses the representation command boundary", () => {
  const app = fs.readFileSync(appPath, "utf8");
  const start = app.indexOf("async function refreshBuildEngineRecord");
  const end = app.indexOf("async function patchBuildRaw", start);
  assert.ok(start >= 0 && end > start);
  const attachment = app.slice(start, end);

  assert.match(attachment, /engineClient\.items\.get\(/);
  assert.match(attachment, /engineClient\.items\.attachRepresentation\(/);
  assert.match(attachment, /engineClient\.items\.replaceRepresentation\(/);
  assert.match(attachment, /engineClient\.items\.detachRepresentation\(/);
  assert.match(attachment, /pendingRepresentationMutations/);
  assert.match(attachment, /return receipt/);
  assert.match(attachment, /recordRevision:\s*transition\.receipt\.after_item_revision/);
  assert.match(attachment, /representationRevision:\s*current\s*&&\s*current\.revision/);
  assert.doesNotMatch(attachment, /\bfetch\s*\(/);
  assert.doesNotMatch(attachment, /["'`]\/api\/builds/);
  assert.doesNotMatch(attachment, /method:\s*["']HEAD["']/);

  const saveStart = app.indexOf("async function saveBuildFields");
  const saveEnd = app.indexOf("async function deleteBuild", saveStart);
  const save = app.slice(saveStart, saveEnd);
  assert.match(save, /if \(f === "pdf_file"\) continue/);

  const refreshStart = app.indexOf("async function refreshSourceTab");
  const refreshEnd = app.indexOf("async function syncBuildFolder", refreshStart);
  const refresh = app.slice(refreshStart, refreshEnd);
  assert.match(refresh,
    /const receipt = await setBuildRepresentation\([\s\S]*if \(receipt\)/);
  assert.match(refresh, /representationFailureMessage/);
});

function representationSnapshot(id, revision) {
  return {
    id,
    revision,
    role: id === "primary" ? "primary" : "alternate",
    media_type: "application/pdf",
    locator: `urn:test:${id}`,
    label: id === "primary" ? "Primary source" : "Alternate source",
    available: true,
    disposition: "referenced",
    content_sha256: "a".repeat(64),
    size: 42,
    metadata: { fixture: true },
  };
}

function representationResult(action, {
  itemId = "book", representationId = "primary",
  beforeItem = "item-r1", afterItem = "item-r2",
  before = null, after = representationSnapshot(representationId, "source-r1"),
} = {}) {
  return {
    ok: true,
    replayed: false,
    receipt: {
      action,
      operation_id: `${action}-operation`,
      command_sha256: "b".repeat(64),
      item_id: itemId,
      representation_id: representationId,
      before_item_revision: beforeItem,
      after_item_revision: afterItem,
      before,
      after,
    },
  };
}

function representationUiHarness({ build, attach, replace, detach, refresh } = {}) {
  const app = fs.readFileSync(appPath, "utf8");
  const start = app.indexOf("function representationOperationKey");
  const end = app.indexOf("async function patchBuildRaw", start);
  assert.ok(start >= 0 && end > start);
  const operations = [];
  const initialBuild = build || {
    id: "book", title: "Herbal", _record_revision: "item-r1",
    _representations: [], pdf_file: "", pdf_sources: [],
  };
  if (!Object.prototype.hasOwnProperty.call(initialBuild, "_available_commands")) {
    initialBuild._available_commands = [
      "representation.attach", "representation.replace", "representation.detach",
    ];
  }
  const state = { builds: { book: initialBuild } };
  const context = vm.createContext({
    state,
    crypto: { randomUUID: () => "12345678-1234-4234-8234-123456789abc" },
    engineClient: { items: {
      attachRepresentation: attach || (async () => {
        throw new Error("unexpected attach");
      }),
      replaceRepresentation: replace || (async () => {
        throw new Error("unexpected replace");
      }),
      detachRepresentation: detach || (async () => {
        throw new Error("unexpected detach");
      }),
    } },
    refreshBuildEngineRecord: refresh || (async (id) => state.builds[id]),
    pushOp: (label, undoFn, redoFn, revert, originTab) => {
      operations.push({ label, undoFn, redoFn, revert, originTab });
      return operations.length;
    },
  });
  vm.runInContext(`${app.slice(start, end)}
this.api = { setBuildRepresentation, pushRepresentationOp,
  secondaryRepresentationId, representationFailureMessage,
  representationCommandAvailable, updateRepresentationMutationControls,
  clearPendingRepresentationMutationsForItem,
  invalidateRepresentationItemIncarnation,
  generation: buildEngineRecordGeneration,
  pendingCount: () => pendingRepresentationMutations.size };`, context);
  return { api: context.api, context, state, operations };
}

test("representation controls follow advertised workbench commands", () => {
  const build = {
    id: "book", _record_revision: "item-r1", _representations: [],
    _available_commands: [], pdf_file: "", pdf_sources: [],
  };
  const h = representationUiHarness({ build });
  const elements = {
    "b-pdf_file": { value: "", disabled: false },
    "b-pdf-attach": { disabled: false },
    "b-pdf-browse": { disabled: false },
    "b-pdf2-add": { disabled: false },
    "b-src-msg": { textContent: "" },
    "b-pdf-sources": {
      querySelectorAll: () => [{ disabled: false }],
    },
  };
  h.context.el = (id) => elements[id] || null;

  h.api.updateRepresentationMutationControls(build);
  assert.equal(elements["b-pdf_file"].disabled, true);
  assert.equal(elements["b-pdf-attach"].disabled, true);
  assert.equal(elements["b-pdf-browse"].disabled, true);
  assert.equal(elements["b-pdf2-add"].disabled, true);
  assert.equal(elements["b-src-msg"].textContent, "Source tools unavailable");

  build._available_commands = ["representation.attach"];
  elements["b-src-msg"].textContent = "";
  h.api.updateRepresentationMutationControls(build);
  assert.equal(elements["b-pdf_file"].disabled, false);
  assert.equal(elements["b-pdf-attach"].disabled, true);
  assert.equal(elements["b-pdf-browse"].disabled, false);
  assert.equal(elements["b-pdf2-add"].disabled, false);
  assert.equal(elements["b-src-msg"].textContent, "");

  elements["b-pdf_file"].value = "C:/scans/herbal.pdf";
  h.api.updateRepresentationMutationControls(build);
  assert.equal(elements["b-pdf-attach"].disabled, false);
});

test("unknown representation capability state fails closed", async () => {
  let attachCalls = 0;
  const build = {
    id: "book", _record_revision: "item-r1", _representations: [],
    pdf_file: "", pdf_sources: [],
  };
  const h = representationUiHarness({
    build,
    attach: async () => { attachCalls += 1; },
  });
  delete build._available_commands;
  const elements = {
    "b-pdf_file": { value: "C:/scans/herbal.pdf", disabled: false },
    "b-pdf-attach": { disabled: false },
    "b-pdf-browse": { disabled: false },
    "b-pdf2-add": { disabled: false },
    "b-src-msg": { textContent: "" },
    "b-pdf-sources": { querySelectorAll: () => [] },
  };
  h.context.el = (id) => elements[id] || null;

  h.api.updateRepresentationMutationControls(build);
  assert.equal(elements["b-pdf_file"].disabled, true);
  assert.equal(elements["b-pdf-attach"].disabled, true);
  assert.equal(elements["b-pdf-browse"].disabled, true);
  assert.equal(elements["b-pdf2-add"].disabled, true);
  assert.equal(elements["b-src-msg"].textContent, "Source tools unavailable");

  const receipt = await h.api.setBuildRepresentation(
    "book", "primary", "C:/scans/herbal.pdf",
    { intent: "attach", recordRevision: "item-r1" });
  assert.equal(receipt, null);
  assert.equal(attachCalls, 0);
});

test("representation failures keep unavailable, invalid PDF, and CAS states distinct", async () => {
  async function failureMessage(error, build) {
    const h = representationUiHarness({
      build: build || {
        id: "book", _record_revision: "item-r1", _representations: [],
        _available_commands: ["representation.attach"],
        pdf_file: "", pdf_sources: [],
      },
      attach: async () => { throw error; },
      replace: async () => { throw error; },
    });
    const current = h.state.builds.book._representations[0] || null;
    await h.api.setBuildRepresentation(
      "book", "primary", "C:/scans/herbal.pdf", {
        intent: current ? "replace" : "attach",
        recordRevision: "item-r1",
        representationRevision: current && current.revision,
      });
    return h.api.representationFailureMessage("book", "primary");
  }

  assert.equal(await failureMessage(Object.assign(new Error("module absent"), {
    code: "representation_command_unavailable", status: 503, retryable: true,
  })), "Source tools unavailable");
  assert.equal(await failureMessage(Object.assign(new Error("not a PDF"), {
    code: "invalid_representation_source", status: 400,
  })), "Invalid or changed PDF");
  const current = representationSnapshot("primary", "source-r1");
  assert.equal(await failureMessage(Object.assign(new Error("stale"), {
    code: "representation_revision_conflict", status: 409,
  }), {
    id: "book", _record_revision: "item-r1", _representations: [current],
    _available_commands: ["representation.replace"],
    pdf_file: "C:/scans/old.pdf", pdf_sources: [],
  }), "Source changed elsewhere");
});

test("deleting an item incarnation drops uncertain representation commands", async () => {
  const error = Object.assign(new Error("response lost"), {
    code: "network-error", status: 0, retryable: true,
  });
  const h = representationUiHarness({ attach: async () => { throw error; } });
  await h.api.setBuildRepresentation(
    "book", "primary", "C:/scans/herbal.pdf",
    { intent: "attach", recordRevision: "item-r1" });
  assert.equal(h.api.pendingCount(), 1);

  h.api.invalidateRepresentationItemIncarnation("book");
  assert.equal(h.api.pendingCount(), 0);
  assert.equal(h.api.generation("book"), 1);

  const app = fs.readFileSync(appPath, "utf8");
  const restoreStart = app.indexOf("async function restoreDeletedBuildFromTrash");
  const deleteStart = app.indexOf("async function deleteBuild", restoreStart);
  const restore = app.slice(restoreStart, deleteStart);
  assert.match(restore, /invalidateRepresentationItemIncarnation/);
  assert.match(restore, /fetch\("\/api\/trash\/restore"/);
  assert.doesNotMatch(restore, /\/api\/builds\/restore/);
  const deletion = app.slice(deleteStart, app.indexOf("function exportBuilds", deleteStart));
  assert.match(deletion, /deleteBuildToTrash/);
});

test("build history restores trusted Trash records and advances recovery handles", async () => {
  const app = fs.readFileSync(appPath, "utf8");
  const start = app.indexOf("async function deleteBuildToTrash");
  const end = app.indexOf("function exportBuilds", start);
  assert.ok(start >= 0 && end > start);
  const state = { builds: {
    book: {
      id: "book", title: "Herbal", updated_at: "item-r1",
      _record_revision: "item-r1", _representations: [],
      _available_commands: ["representation.attach"],
      pdf_file: "", pdf_sources: [],
    },
  }, buildSel: "book" };
  const operations = [];
  const restoreIds = [];
  const invalidated = [];
  let deleteCount = 0;
  let missingHandle = false;
  const context = vm.createContext({
    state,
    fetch: async (url, init = {}) => {
      if (init.method === "DELETE") {
        deleteCount += 1;
        return response(200, missingHandle
          ? { ok: true }
          : { ok: true, trash_id: `trash-${deleteCount}` });
      }
      assert.equal(url, "/api/trash/restore");
      const body = JSON.parse(init.body);
      restoreIds.push(body.id);
      return response(200, { ok: true, replayed: false, build: {
        id: "book", title: "Herbal", updated_at: `item-restored-${deleteCount}`,
        pdf_file: "", pdf_sources: [],
      } });
    },
    invalidateRepresentationItemIncarnation: (id) => invalidated.push(id),
    refreshBuildEngineRecord: async (id) => {
      state.builds[id]._available_commands = ["representation.attach"];
      return state.builds[id];
    },
    pushOp: (label, undoFn, redoFn) => {
      operations.push({ label, undoFn, redoFn });
    },
    renderUpload: () => {},
    status: () => {},
    statusCrit: () => {},
  });
  vm.runInContext(`${app.slice(start, end)}
this.api = { deleteBuild, deleteBuildToTrash };`, context);

  await context.api.deleteBuild("workbench");
  assert.equal("book" in state.builds, false);
  assert.equal(operations.length, 1);

  await operations[0].undoFn();
  assert.equal(state.builds.book.id, "book");
  await operations[0].redoFn();
  assert.equal("book" in state.builds, false);
  await operations[0].undoFn();
  assert.deepEqual(restoreIds, ["trash-1", "trash-2"]);
  assert.equal(invalidated.length, 4,
    "delete, restore, delete, and restore transitions invalidate incarnations");

  state.builds.other = { id: "other" };
  missingHandle = true;
  await assert.rejects(context.api.deleteBuildToTrash("other"),
    /recovery handle/);
  assert.equal(state.builds.other.id, "other");
  assert.doesNotMatch(app.slice(start, end), /\/api\/builds\/restore/);
});

test("representation mutation retries and later recovery reuse one operation key", async () => {
  const keys = [];
  let attempts = 0;
  const confirmed = representationResult("attach");
  const h = representationUiHarness({
    attach: async (args) => {
      keys.push(args.idempotencyKey);
      attempts += 1;
      if (attempts <= 2) {
        const error = new Error("connection dropped");
        error.code = "network-error";
        error.status = 0;
        throw error;
      }
      return confirmed;
    },
    refresh: async () => { throw new Error("still offline"); },
  });

  const first = await h.api.setBuildRepresentation(
    "book", "primary", "C:/scans/herbal.pdf",
    { intent: "attach", recordRevision: "item-r1" });
  assert.equal(first, null);
  assert.equal(h.api.pendingCount(), 1);

  const recovered = await h.api.setBuildRepresentation(
    "book", "primary", "C:/scans/herbal.pdf",
    { intent: "replace", recordRevision: "item-newer" });
  assert.equal(recovered, confirmed.receipt);
  assert.equal(new Set(keys).size, 1,
    "the uncertain attach is replayed even if refreshed state suggests replace");
  assert.equal(h.api.pendingCount(), 0);
});

test("a receipt-confirmed mutation succeeds when its follow-up refresh fails", async () => {
  const calls = [];
  const confirmed = representationResult("attach");
  const h = representationUiHarness({
    attach: async (args) => {
      calls.push(args);
      if (calls.length === 1) {
        const error = new Error("response lost after send");
        error.code = "network-error";
        error.status = 0;
        throw error;
      }
      return confirmed;
    },
    refresh: async () => { throw new Error("refresh unavailable"); },
  });

  const receipt = await h.api.setBuildRepresentation(
    "book", "primary", "C:/scans/herbal.pdf",
    { intent: "attach", recordRevision: "item-r1" });

  assert.equal(receipt, confirmed.receipt);
  assert.equal(calls.length, 2);
  assert.equal(calls[0].idempotencyKey, calls[1].idempotencyKey);
  assert.equal(h.state.builds.book._record_revision, "item-r2");
  assert.equal(h.state.builds.book.pdf_file, "C:/scans/herbal.pdf");
  assert.equal(h.state.builds.book._representations[0].revision, "source-r1");
  assert.equal(h.api.generation("book"), 1);
});

test("representation history chains exact receipt revisions through undo and redo", async () => {
  const firstSource = representationSnapshot("primary", "source-r1");
  const redoSource = representationSnapshot("primary", "source-r2");
  const initial = representationResult("attach", { after: firstSource }).receipt;
  const detachCalls = [];
  const attachCalls = [];
  const h = representationUiHarness({
    build: {
      id: "book", title: "Herbal", _record_revision: "item-r2",
      _representations: [firstSource], pdf_file: "C:/scans/herbal.pdf",
      pdf_sources: [],
    },
    detach: async (args) => {
      detachCalls.push(args);
      if (detachCalls.length > 1) {
        const error = new Error("changed elsewhere");
        error.code = "representation_revision_conflict";
        error.status = 409;
        throw error;
      }
      return representationResult("detach", {
        beforeItem: "item-r2", afterItem: "item-r3",
        before: firstSource, after: null,
      });
    },
    attach: async (args) => {
      attachCalls.push(args);
      return representationResult("attach", {
        beforeItem: "item-r3", afterItem: "item-r4", after: redoSource,
      });
    },
    refresh: async () => { throw new Error("refresh unavailable"); },
  });

  h.api.pushRepresentationOp(
    "attach source", "book", "primary", initial,
    "", "C:/scans/herbal.pdf", "workbench");
  const operation = h.operations[0];
  await operation.undoFn();
  assert.equal(detachCalls[0].recordRevision, "item-r2");
  assert.equal(detachCalls[0].representationRevision, "source-r1");

  await operation.redoFn();
  assert.equal(attachCalls[0].recordRevision, "item-r3");
  assert.equal("representationRevision" in attachCalls[0], false);
  assert.equal(attachCalls[0].representation.expected_content_sha256,
    firstSource.content_sha256);

  h.state.builds.book._record_revision = "item-concurrent";
  h.state.builds.book._representations[0].revision = "source-concurrent";
  await assert.rejects(operation.undoFn, /changed elsewhere/i);
  assert.equal(detachCalls[1].recordRevision, "item-r4");
  assert.equal(detachCalls[1].representationRevision, "source-r2");
});

test("secondary IDs are portable and attach intent never degrades into replace", async () => {
  const collided = representationSnapshot("collision", "source-r1");
  let attachCalls = 0;
  let replaceCalls = 0;
  const h = representationUiHarness({
    build: {
      id: "book", _record_revision: "item-r1",
      _representations: [collided], pdf_sources: [
        { id: "collision", path: "C:/old.pdf" },
      ],
    },
    attach: async () => {
      attachCalls += 1;
      const error = new Error("already exists");
      error.code = "representation_already_exists";
      error.status = 409;
      throw error;
    },
    replace: async () => { replaceCalls += 1; },
  });

  const id = h.api.secondaryRepresentationId(h.state.builds.book);
  assert.match(id, /^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$/);
  assert.notEqual(id.toLowerCase(), "collision");
  const result = await h.api.setBuildRepresentation(
    "book", "collision", "C:/new.pdf",
    { intent: "attach", recordRevision: "item-r1" });
  assert.equal(result, null);
  assert.equal(attachCalls, 1);
  assert.equal(replaceCalls, 0);
});

test("metadata Save does not claim or persist a PDF source draft", async () => {
  const app = fs.readFileSync(appPath, "utf8");
  const start = app.indexOf("async function saveBuildFields");
  const end = app.indexOf("async function deleteBuildToTrash", start);
  assert.ok(start >= 0 && end > start);
  const elements = {
    "b-pdf_file": { value: "C:/scans/new.pdf" },
    "b-title": { value: "Herbal" },
    "b-ready": { classList: { contains: () => false } },
    "build-msg": { textContent: "" },
    "b-src-msg": { textContent: "" },
  };
  const state = { buildSel: "book", builds: { book: {
    id: "book", title: "Herbal", updated_at: "item-r1",
    pdf_file: "C:/scans/old.pdf", status: "draft",
  } } };
  const patches = [];
  const statuses = [];
  const context = vm.createContext({
    state,
    BUILD_FIELDS: ["title", "pdf_file"],
    el: (id) => elements[id] || null,
    activeHistoryTab: () => "workbench",
    buildIsDirty: () => context.metadataDirty,
    metadataDirty: false,
    catPickers: { "b-categories": { get: () => [] } },
    buildDescMd: { get: () => "Description" },
    currentBuild: () => state.builds[state.buildSel],
    patchBuild: async (id, fields) => {
      patches.push({ id, fields: { ...fields } });
      return true;
    },
    buildEditGeneration: 0,
    buildPatchConflict: false,
    descState: {},
    buildDirty: false,
    renderBuildEditor: () => {},
    status: (message) => statuses.push(message),
  });
  vm.runInContext(`${app.slice(start, end)}
this.saveBuildFields = saveBuildFields;`, context);

  assert.equal(await context.saveBuildFields(), false);
  assert.equal(patches.length, 0);
  assert.equal(elements["build-msg"].textContent, "");
  assert.equal(elements["b-src-msg"].textContent, "Not attached");

  context.metadataDirty = true;
  assert.equal(await context.saveBuildFields(), true);
  assert.equal("pdf_file" in patches[0].fields, false);
  assert.equal(elements["build-msg"].textContent, "Metadata saved");
  assert.equal(elements["b-src-msg"].textContent, "Not attached");
  assert.match(statuses[0], /^METADATA SAVED/);

  const listenerStart = app.indexOf('el("build-form").addEventListener("input"');
  const listenerEnd = app.indexOf('el("build-save").addEventListener', listenerStart);
  assert.match(app.slice(listenerStart, listenerEnd),
    /ev\.target\.id === "b-pdf_file"\) return/);
});

test("build creation strips legacy sources and attaches before history or selection", async () => {
  const app = fs.readFileSync(appPath, "utf8");
  const start = app.indexOf("async function createBuild(");
  const end = app.indexOf("function buildSeedFromSource", start);
  assert.ok(start >= 0 && end > start);
  const events = [];
  const messages = { "b-src-msg": { textContent: "" } };
  const state = { builds: {} };
  const created = {
    id: "new-book", title: "Seeded Herbal", updated_at: "item-r1",
    pdf_file: "", pdf_sources: [],
  };
  const context = vm.createContext({
    state,
    fetch: async (url, init) => {
      events.push({ type: "post", url, body: JSON.parse(init.body) });
      return { ok: true, json: async () => ({ ok: true, build: created }) };
    },
    setBuildRepresentation: async (itemId, sourceId, sourcePath, options) => {
      events.push({ type: "representation", itemId, sourceId, sourcePath,
        options: { ...options } });
      if (sourceId !== "primary") return null;
      const after = representationSnapshot("primary", "source-r1");
      state.builds[itemId].pdf_file = sourcePath;
      state.builds[itemId]._representations = [after];
      state.builds[itemId]._record_revision = "item-r2";
      return representationResult("attach", {
        itemId, beforeItem: "item-r1", afterItem: "item-r2", after,
      }).receipt;
    },
    invalidateRepresentationItemIncarnation: (itemId) => {
      events.push({ type: "incarnation", itemId });
    },
    refreshBuildEngineRecord: async (itemId) => {
      events.push({ type: "semantic", itemId });
      state.builds[itemId]._available_commands = [
        "representation.attach", "representation.replace", "representation.detach",
      ];
      return state.builds[itemId];
    },
    secondaryRepresentationId: () => "s1-secondary",
    representationFailureMessage: () => "Source update rejected",
    pushOp: () => {
      events.push({ type: "history", snapshot: {
        ...state.builds["new-book"],
      } });
    },
    selectBuild: (id) => events.push({ type: "select", id }),
    renderUpload: () => events.push({ type: "render" }),
    statusCrit: (message) => events.push({ type: "critical", message }),
    el: (id) => messages[id] || null,
  });
  vm.runInContext(`${app.slice(start, end)}
this.createBuild = createBuild;`, context);

  const result = await context.createBuild({
    title: "Seeded Herbal",
    pdf_file: "C:/scans/primary.pdf",
    pdf_sources: [{ id: "scan", path: "C:/scans/alternate.pdf" }],
  }, "seeded", "workbench");

  const post = events.find((event) => event.type === "post");
  assert.equal("pdf_file" in post.body.build, false);
  assert.equal("pdf_sources" in post.body.build, false);
  const mutationEvents = events.filter((event) =>
    event.type === "representation");
  assert.equal(mutationEvents.length, 2);
  assert.equal(mutationEvents[0].options.intent, "attach");
  assert.equal(mutationEvents[0].options.recordRevision, "item-r1");
  assert.equal(mutationEvents[1].options.recordRevision, "item-r2");
  assert.ok(events.findIndex((event) => event.type === "semantic") <
    events.findIndex((event) => event.type === "representation"));
  assert.ok(events.findIndex((event) => event.type === "representation") <
    events.findIndex((event) => event.type === "history"));
  assert.ok(events.findIndex((event) => event.type === "history") <
    events.findIndex((event) => event.type === "select"));
  assert.equal(result.pdf_file, "C:/scans/primary.pdf");
  assert.equal(messages["b-src-msg"].textContent,
    "Source update rejected");
  assert.ok(events.some((event) => event.type === "critical" &&
    /ATTACH FAILED/.test(event.message)));
  assert.equal(events.some((event) =>
    /ATTACHED|SOURCE ADDED/.test(event.message || "")), false);
});

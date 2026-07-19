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
  const jsonMethods = [
    client.replica.templates.list,
    client.pdf.info,
    client.replica.pages.get,
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
    client.replica.styles.save,
    client.replica.instructions.get,
    client.replica.instructions.save,
    client.replica.styles.reset,
    client.replica.packages.import,
  ];
  const urlBuilders = [
    client.pdf.pageImageUrl,
    client.replica.pages.imageUrl,
    client.replica.figures.imageUrl,
    client.replica.packages.exportUrl,
    client.replica.printUrl,
  ];
  assert.equal(jsonMethods.length, 19);
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

test("Replica import constructs multipart data without forcing Content-Type", async () => {
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
  });

  const { url, init } = calls[0];
  assert.equal(url,
    "/api/builds/book%20one/replica-import?src=scan%20%26%20notes");
  assert.equal(init.method, "POST");
  assert.deepEqual(init.body.entries, [["lib", file]]);
  assert.equal(init.headers["Content-Type"], undefined);
  assert.equal(init.headers.Accept, "application/json");
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

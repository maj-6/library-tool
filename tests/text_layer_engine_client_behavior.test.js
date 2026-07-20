const assert = require("node:assert/strict");
const path = require("node:path");
const test = require("node:test");

const clientPath = path.join(
  __dirname, "..", "tools", "whl_explorer", "static", "engine-client.js");
const { EngineClient, EngineClientError } = require(clientPath);

function response(status, body) {
  return { ok: status >= 200 && status < 300, status, json: async () => body };
}

function http(status, body) {
  return { _httpResponse: true, status, body };
}

function harness(...responses) {
  const calls = [];
  let index = 0;
  const client = new EngineClient({
    transport: async (url, init) => {
      calls.push({ url, init });
      const selected = responses.length
        ? responses[Math.min(index++, responses.length - 1)]
        : { ok: true };
      return selected && selected._httpResponse
        ? response(selected.status, selected.body)
        : response(200, selected);
    },
  });
  return { client, calls };
}

function provenance() {
  return {
    origin: "human",
    review_state: "reviewed",
    provider_id: "",
    model: "",
    recipe_revision: "",
    updated_at: "2026-07-19T19:00:00Z",
    metadata: { editor: "Ada" },
  };
}

function sourceView() {
  return {
    representation_id: "scan-main",
    pinned_revision: "source-current",
    current_revision: "source-current",
    available: true,
    status: "current",
  };
}

function unit() {
  return {
    selector: "canvas:a",
    order: 1,
    label: "Folio A",
    text: "Original",
    provenance: provenance(),
    content_revision: "tuc-current",
    unit_revision: "tur-current",
  };
}

function summary(itemId = "book:one", layerId = "layer:one") {
  return {
    item_id: itemId,
    layer_id: layerId,
    label: "Diplomatic transcription",
    kind: "transcription",
    language: "la",
    document_revision: "tld-current",
    content_revision: "tlc-current",
    view_revision: "tlv-current",
    source: sourceView(),
    unit_count: 1,
  };
}

function collection(itemId = "book:one") {
  return {
    ok: true,
    schema: "librarytool.text-layer-summaries/1",
    item_id: itemId,
    text_layers: [summary(itemId)],
    revision: "tlc-collection",
  };
}

function detail(itemId = "book:one", layerId = "layer:one") {
  return {
    ok: true,
    schema: "librarytool.text-layer/1",
    text_layer: {
      document: {
        item_id: itemId,
        layer_id: layerId,
        label: "Diplomatic transcription",
        kind: "transcription",
        language: "la",
        source: {
          representation_id: "scan-main",
          revision: "source-current",
        },
        preamble: "Shelf note",
        units: [unit()],
        document_revision: "tld-current",
        content_revision: "tlc-current",
      },
      source: sourceView(),
      view_revision: "tlv-current",
    },
  };
}

function mutationReceipt() {
  return {
    ok: true,
    schema: "librarytool.text-layer-mutation-receipt/1",
    replayed: false,
    receipt: {
      action: "replace-unit",
      operation_id: "replace-unit-one",
      item_id: "book:one",
      layer_id: "layer:one",
      source_revision: "source-current",
      before_document_revision: "tld-current",
      after_document_revision: "tld-next",
      before_content_revision: "tlc-current",
      after_content_revision: "tlc-next",
      units: [{
        selector: "canvas:a",
        before_unit_revision: "tur-current",
        after_unit_revision: "tur-next",
        before_content_revision: "tuc-current",
        after_content_revision: "tuc-next",
      }],
    },
  };
}

function copyJson(value) {
  return JSON.parse(JSON.stringify(value));
}

async function rejectsInvalidResponse(clientCall) {
  await assert.rejects(clientCall, (error) =>
    error instanceof EngineClientError && error.code === "invalid-response" &&
    error.retryable === true);
}

test("EngineClient exposes the revisioned text-layer surface", () => {
  const { client } = harness();
  assert.equal(typeof client.textLayers.list, "function");
  assert.equal(typeof client.textLayers.get, "function");
  assert.equal(typeof client.textLayers.replaceUnit, "function");
  assert.ok(Object.isFrozen(client.textLayers));
});

test("text-layer reads use portable path identities", async () => {
  const { client, calls } = harness(collection(), detail());
  await client.textLayers.list({ itemId: "book:one" });
  await client.textLayers.get({ itemId: "book:one", layerId: "layer:one" });

  assert.equal(calls[0].url, "/api/v1/items/book%3Aone/text-layers");
  assert.equal(calls[0].init.method, "GET");
  assert.equal(calls[1].url,
    "/api/v1/items/book%3Aone/text-layers/layer%3Aone");
  assert.equal(calls[1].init.method, "GET");
});

test("text-layer unit replacement owns the exact header and body contract",
  async () => {
    const { client, calls } = harness(mutationReceipt());
    const evidence = provenance();

    await client.textLayers.replaceUnit({
      itemId: "book:one",
      layerId: "layer:one",
      selector: "canvas:a",
      text: "Corrected",
      provenance: evidence,
      unitRevision: "tur-current",
      sourceRevision: "source-current",
      idempotencyKey: "replace-unit-one",
    });

    assert.equal(calls[0].url,
      "/api/v1/items/book%3Aone/text-layers/layer%3Aone/units/canvas%3Aa");
    assert.equal(calls[0].init.method, "PUT");
    assert.equal(calls[0].init.cache, "no-store");
    assert.equal(calls[0].init.headers["Idempotency-Key"],
      "replace-unit-one");
    assert.equal(calls[0].init.headers["If-Unit-Match"], '"tur-current"');
    assert.equal(calls[0].init.headers["If-Source-Match"],
      '"source-current"');
    assert.deepEqual(JSON.parse(calls[0].init.body), {
      replacement: { text: "Corrected", provenance: evidence },
    });
  });

test("text-layer commands reject unsafe paths and headers before transport",
  () => {
    const { client, calls } = harness();
    const valid = {
      itemId: "book-one",
      layerId: "layer-one",
      selector: "canvas-a",
      text: "Corrected",
      provenance: provenance(),
      unitRevision: "tur-current",
      sourceRevision: "source-current",
      idempotencyKey: "replace-unit-one",
    };
    const cases = [
      [{ ...valid, itemId: "book/one" }, /itemId/],
      [{ ...valid, layerId: "layer one" }, /layerId/],
      [{ ...valid, selector: "canvas/one" }, /selector/],
      [{ ...valid, idempotencyKey: "operation/one" }, /idempotencyKey/],
      [{ ...valid, unitRevision: "" }, /unitRevision/],
      [{ ...valid, unitRevision: 'W/"old"' }, /unitRevision/],
      [{ ...valid, sourceRevision: "source old" }, /sourceRevision/],
      [{ ...valid, provenance: null }, /provenance/],
      [{ ...valid, text: 42 }, /text/],
    ];

    for (const [args, expected] of cases) {
      assert.throws(() => client.textLayers.replaceUnit(args), expected);
    }
    assert.throws(() => client.textLayers.list({ itemId: "book/one" }),
      /itemId/);
    assert.throws(() => client.textLayers.get({
      itemId: "book-one", layerId: "layer/one",
    }), /layerId/);
    assert.equal(calls.length, 0);
  });

test("text-layer revision headers reject Python whitespace and control formats",
  () => {
    const { client, calls } = harness(mutationReceipt());
    const base = {
      itemId: "book:one",
      layerId: "layer:one",
      selector: "canvas:a",
      text: "Corrected",
      provenance: provenance(),
      unitRevision: "tur-current",
      sourceRevision: "source-current",
      idempotencyKey: "replace-unit-one",
    };
    for (const forbidden of ["\u00a0", "\u0085", "\u200b"]) {
      assert.throws(() => client.textLayers.replaceUnit({
        ...base, unitRevision: `tur${forbidden}current`,
      }), /unitRevision/);
      assert.throws(() => client.textLayers.replaceUnit({
        ...base, sourceRevision: `source${forbidden}current`,
      }), /sourceRevision/);
    }
    assert.equal(calls.length, 0);
  });

test("text-layer collection validation fails closed on malformed 2xx", async () => {
  const wrongItem = collection("other-item");
  const badSource = collection();
  badSource.text_layers[0].source.current_revision = "source\u200bcurrent";
  const leaked = collection();
  leaked.text_layers[0].source.command_sha256 = "private";
  const duplicate = collection();
  duplicate.text_layers.push(copyJson(duplicate.text_layers[0]));
  const cases = [
    http(201, collection()),
    { ...collection(), schema: "librarytool.text-layer-summaries/2" },
    wrongItem,
    { ...collection(), revision: "tlc\u00a0collection" },
    badSource,
    leaked,
    duplicate,
  ];
  for (const body of cases) {
    const { client } = harness(body);
    await rejectsInvalidResponse(
      () => client.textLayers.list({ itemId: "book:one" }));
  }
});

test("text-layer detail validation fails closed on malformed 2xx", async () => {
  const wrongLayer = detail();
  wrongLayer.text_layer.document.layer_id = "another-layer";
  const incoherentSource = detail();
  incoherentSource.text_layer.source.status = "stale";
  const invalidUnit = detail();
  invalidUnit.text_layer.document.units[0].unit_revision = "tur\u0085bad";
  const leaked = detail();
  leaked.text_layer.document.units[0].provenance.metadata.command_sha256 =
    "private";
  const cases = [
    http(206, detail()),
    { ...detail(), schema: "librarytool.text-layer/2" },
    wrongLayer,
    incoherentSource,
    invalidUnit,
    leaked,
  ];
  for (const body of cases) {
    const { client } = harness(body);
    await rejectsInvalidResponse(() => client.textLayers.get({
      itemId: "book:one", layerId: "layer:one",
    }));
  }
});

test("text-layer receipt validation binds the exact command and rejects leaks",
  async () => {
    const wrongOperation = mutationReceipt();
    wrongOperation.receipt.operation_id = "other-operation";
    const wrongSelector = mutationReceipt();
    wrongSelector.receipt.units[0].selector = "canvas:b";
    const wrongBefore = mutationReceipt();
    wrongBefore.receipt.units[0].before_unit_revision = "tur-other";
    const wrongContent = mutationReceipt();
    wrongContent.receipt.units[0].after_content_revision = "tuc-current";
    const leaked = mutationReceipt();
    leaked.receipt.units[0].command_sha256 = "private";
    const cases = [
      http(201, mutationReceipt()),
      { ...mutationReceipt(), replayed: "false" },
      wrongOperation,
      wrongSelector,
      wrongBefore,
      wrongContent,
      leaked,
    ];
    for (const body of cases) {
      const { client } = harness(body);
      await rejectsInvalidResponse(() => client.textLayers.replaceUnit({
        itemId: "book:one",
        layerId: "layer:one",
        selector: "canvas:a",
        text: "Corrected",
        provenance: provenance(),
        unitRevision: "tur-current",
        sourceRevision: "source-current",
        idempotencyKey: "replace-unit-one",
      }));
    }
  });

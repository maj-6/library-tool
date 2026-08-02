const assert = require("node:assert/strict");
const test = require("node:test");

const {
  OCR_PROPOSALS_READ_CAPABILITY,
  REOCR_QUEUE_CAPABILITY,
  createOcrProposalsPanel,
} = require("../tools/whl_explorer/static/corrections/ocr-proposals");
const {
  FakeNode,
  fakeDocument,
} = require("./fixtures/corrections_fake_dom");


function proposalSummary(letter, overrides = {}) {
  return {
    proposal_ref: `cop-${letter.repeat(40)}`,
    operation_id: `correction-reocr:${"c".repeat(48)}`,
    source: {
      kind: "ocr-ready",
      artifact_id: "result-ocr-ready-1",
      artifact_revision: "ocr-ready-r1",
      content_sha256: "b".repeat(64),
    },
    provider: { provider_id: "mistral", model: "mistral-ocr-latest" },
    publication_policy: "machine-proposal-only",
    content_sha256: "c".repeat(64),
    availability: "available",
    ...overrides,
  };
}


function proposalDetail(letter, recognition) {
  return {
    ...proposalSummary(letter),
    item_id: "book-1",
    recognition: recognition !== undefined ? recognition : {
      text: "Machine proposal text",
      regions: [{ role: "text", box: [0.1, 0.2, 0.3, 0.4] }],
      dims: { width: 1200, height: 1600 },
    },
  };
}


function applyReceipt(overrides = {}) {
  return {
    ok: true,
    schema: "librarytool.correction-ocr-proposal-apply-receipt/1",
    replayed: false,
    operation_id: "ocr-apply-1",
    proposal_ref: `cop-${"a".repeat(40)}`,
    applied: {
      item_id: "book-1",
      capture_id: "capture-1",
      asset_id: "asset-1",
      source_id: "primary",
      page: 3,
      doc: "compiled.txt",
      regions: "saved",
      words: "saved",
      text: "merged",
      ...(overrides.applied || {}),
    },
    ...(overrides.envelope || {}),
  };
}


function catalogPage(itemId, proposals, overrides = {}) {
  return {
    ok: true,
    schema: "librarytool.correction-ocr-proposals/1",
    item_id: itemId,
    snapshot_revision: `cops-${"e".repeat(64)}`,
    proposals,
    next_cursor: null,
    total: proposals.length,
    ...overrides,
  };
}


function panelHarness(portOverrides = {}, options = {}) {
  const documentRef = fakeDocument();
  const root = new FakeNode("section", documentRef);
  const statuses = [];
  const listeners = [];
  const calls = { apply: [], get: [], list: [] };
  const port = {
    async list(args) {
      calls.list.push(args);
      return catalogPage(args.itemId, [
        proposalSummary("a"), proposalSummary("b"),
      ]);
    },
    async get(args) {
      calls.get.push(args);
      return { ok: true, proposal: proposalDetail("a") };
    },
    async apply(args) {
      calls.apply.push(args);
      return applyReceipt();
    },
    subscribeResults(listener) {
      listeners.push(listener);
      return () => listeners.splice(listeners.indexOf(listener), 1);
    },
    ...portOverrides,
  };
  let operationCounter = 0;
  const panel = createOcrProposalsPanel({
    root,
    documentRef,
    port,
    operationIdFactory: () => `ocr-apply-${++operationCounter}`,
    onStatus: (message, error) => statuses.push({
      message, error: error === true,
    }),
    ...options,
  }).mount();
  return { calls, documentRef, listeners, panel, port, root, statuses };
}


function node(root, selector) {
  const found = root.querySelector(selector);
  assert.ok(found, `expected ${selector}`);
  return found;
}


test("the panel stays hidden and quiet until discovery proves the read capability", async () => {
  assert.equal(OCR_PROPOSALS_READ_CAPABILITY,
    "library.corrections.ocr-proposals.read");
  assert.equal(REOCR_QUEUE_CAPABILITY, "library.corrections.reocr.queue");
  const harness = panelHarness();
  assert.equal(harness.root.hidden, true);

  await harness.panel.setItem("book-1");
  assert.equal(harness.calls.list.length, 0,
    "no catalog reads before the capability is proven");

  await harness.panel.setCapabilities({ read: true, queue: true });
  assert.equal(harness.root.hidden, false);
  assert.equal(harness.calls.list.length, 1,
    "the pending item loads once reads are allowed");
  assert.equal(
    node(harness.root, "[data-ocr-proposals-count]").textContent, "2");
});


test("catalog reads pin one snapshot across pages and list compact rows", async () => {
  const pageOne = catalogPage("book-1", [proposalSummary("a")], {
    next_cursor: "copc-page-2",
    total: 2,
  });
  const pageTwo = catalogPage("book-1", [proposalSummary("b")], { total: 2 });
  let listCall = 0;
  const harnessCalls = [];
  const harness = panelHarness({
    async list(args) {
      listCall += 1;
      harnessCalls.push(args);
      return listCall === 1 ? pageOne : pageTwo;
    },
  });
  await harness.panel.setCapabilities({ read: true, queue: true });
  await harness.panel.setItem("book-1");

  assert.deepEqual(harnessCalls.map((call) => [
    call.cursor, call.snapshotRevision,
  ]), [
    [null, undefined],
    ["copc-page-2", pageOne.snapshot_revision],
  ]);
  const rows = harness.root.querySelectorAll("[data-proposal-ref]");
  assert.deepEqual(
    rows.map((row) => row.getAttribute("data-proposal-ref")),
    [`cop-${"a".repeat(40)}`, `cop-${"b".repeat(40)}`],
  );
  assert.equal(
    node(harness.root, "[data-ocr-proposal-detail]").hidden, true,
    "nothing is selected yet");
});


test("selecting a proposal renders recognition text and a layout summary", async () => {
  const harness = panelHarness();
  await harness.panel.setCapabilities({ read: true, queue: true });
  await harness.panel.setItem("book-1");

  const row = harness.root.querySelectorAll("[data-proposal-ref]")[0];
  row.emit("click");
  await new Promise((resolve) => setImmediate(resolve));

  assert.deepEqual(harness.calls.get, [{
    itemId: "book-1",
    proposalRef: `cop-${"a".repeat(40)}`,
  }]);
  assert.equal(node(harness.root, "[data-ocr-proposal-detail]").hidden, false);
  assert.equal(
    node(harness.root, "[data-ocr-proposal-provider]").textContent,
    "mistral · mistral-ocr-latest");
  assert.equal(
    node(harness.root, "[data-ocr-proposal-summary]").textContent,
    "1 region · 1200×1600");
  assert.equal(
    node(harness.root, "[data-ocr-proposal-text]").textContent,
    "Machine proposal text");
  assert.equal(node(harness.root, "[data-ocr-proposal-apply]").disabled, false);
});


test("provider-shaped payload gaps degrade to hints instead of errors", async () => {
  const harness = panelHarness({
    async get() {
      return {
        ok: true,
        proposal: proposalDetail("a", { blocks: ["provider-specific"] }),
      };
    },
  });
  await harness.panel.setCapabilities({ read: true, queue: true });
  await harness.panel.setItem("book-1");
  await harness.panel.select(`cop-${"a".repeat(40)}`);

  assert.equal(
    node(harness.root, "[data-ocr-proposal-summary]").textContent,
    "no region layout");
  const text = node(harness.root, "[data-ocr-proposal-text]");
  assert.equal(text.textContent, "No text payload in this proposal.");
  assert.equal(text.getAttribute("data-ocr-proposal-text-state"), "absent");
  assert.equal(node(harness.root, "[data-ocr-proposal-apply]").disabled, true);
  assert.equal(
    node(harness.root, "[data-ocr-proposal-apply-hint]").textContent,
    "This proposal has no region layout to apply.");
  assert.deepEqual(harness.statuses, []);
});


test("apply distinguishes merged, replayed, and proposed outcomes", async () => {
  const receipts = [
    applyReceipt(),
    applyReceipt({ envelope: { replayed: true } }),
    applyReceipt({ applied: { regions: "proposed", text: "proposed" } }),
  ];
  let applyCall = 0;
  const applyCalls = [];
  const harness = panelHarness({
    async apply(args) {
      applyCalls.push(args);
      applyCall += 1;
      return receipts[applyCall - 1];
    },
  });
  await harness.panel.setCapabilities({ read: true, queue: true });
  await harness.panel.setItem("book-1");
  await harness.panel.select(`cop-${"a".repeat(40)}`);
  const message = node(harness.root, "[data-ocr-proposals-message]");

  await harness.panel.apply();
  assert.equal(message.textContent, "OCR applied to page 3.");
  assert.equal(message.getAttribute("role"), "status");

  await harness.panel.apply();
  assert.equal(message.textContent, "OCR applied to page 3 (already applied).");

  await harness.panel.apply();
  assert.match(message.textContent, /^Saved as proposals — /);
  assert.equal(message.getAttribute("role"), "status");

  assert.deepEqual(applyCalls.map((call) => call.operationId),
    ["ocr-apply-1", "ocr-apply-2", "ocr-apply-3"],
    "every apply click mints a fresh idempotency key");
});


test("a capture without a build entry disables Apply with a hint, not a toast", async () => {
  const error = new Error("the capture has no imported build entry to write into");
  error.code = "ocr_apply_capture_entry_unavailable";
  error.status = 409;
  const harness = panelHarness({
    async apply() {
      throw error;
    },
  });
  await harness.panel.setCapabilities({ read: true, queue: true });
  await harness.panel.setItem("book-1");
  await harness.panel.select(`cop-${"a".repeat(40)}`);

  await harness.panel.apply();
  const apply = node(harness.root, "[data-ocr-proposal-apply]");
  assert.equal(apply.disabled, true);
  assert.equal(
    node(harness.root, "[data-ocr-proposal-apply-hint]").textContent,
    "No imported build entry — import the capture before applying OCR.");
  assert.deepEqual(harness.statuses, [], "no error toast for this state");

  await harness.panel.apply();
  assert.equal(harness.calls.apply.length, 0,
    "a blocked proposal does not retry the write");
});


test("apply failures and unavailable modules surface without breaking the panel", async () => {
  const failure = new Error("the OCR proposal does not exist");
  failure.code = "correction_ocr_proposal_not_found";
  const harness = panelHarness({
    async apply() {
      throw failure;
    },
  });
  await harness.panel.setCapabilities({ read: true, queue: true });
  await harness.panel.setItem("book-1");
  await harness.panel.select(`cop-${"a".repeat(40)}`);
  await harness.panel.apply();
  const message = node(harness.root, "[data-ocr-proposals-message]");
  assert.equal(message.textContent, "the OCR proposal does not exist");
  assert.equal(message.getAttribute("role"), "alert");

  const unavailable = new Error("the correction ocr proposal catalog module is unavailable");
  unavailable.code = "correction_ocr_proposal_catalog_module_unavailable";
  const offline = panelHarness({
    async list() {
      throw unavailable;
    },
  });
  await offline.panel.setCapabilities({ read: true, queue: true });
  await offline.panel.setItem("book-1");
  const offlineMessage = node(offline.root, "[data-ocr-proposals-message]");
  assert.equal(offlineMessage.textContent,
    "OCR proposal module is unavailable.");
  assert.equal(offlineMessage.getAttribute("role"), "status");
});


test("a changed catalog snapshot restarts the read once from scratch", async () => {
  const conflict = new Error("the OCR proposal catalog changed");
  conflict.code = "correction_ocr_proposal_catalog_changed";
  const listCalls = [];
  let attempt = 0;
  const harness = panelHarness({
    async list(args) {
      listCalls.push(args);
      attempt += 1;
      if (attempt === 2) throw conflict;
      if (attempt === 1) {
        return catalogPage("book-1", [proposalSummary("a")], {
          next_cursor: "copc-page-2",
          total: 2,
        });
      }
      return catalogPage("book-1", [
        proposalSummary("a"), proposalSummary("b"),
      ]);
    },
  });
  await harness.panel.setCapabilities({ read: true, queue: true });
  await harness.panel.setItem("book-1");

  assert.equal(listCalls.length, 3);
  assert.equal(listCalls[2].cursor, null, "the retry starts a fresh snapshot");
  assert.equal(
    node(harness.root, "[data-ocr-proposals-count]").textContent, "2");
});


test("terminal re-OCR results refresh the matching item and report outcomes", async () => {
  const harness = panelHarness();
  await harness.panel.setCapabilities({ read: true, queue: true });
  await harness.panel.setItem("book-1");
  assert.equal(harness.listeners.length, 1);
  const reads = harness.calls.list.length;

  harness.listeners[0]({
    job_id: "correction-ocr-1",
    operation_id: `correction-reocr:${"c".repeat(48)}`,
    terminal_state: "done",
    image_commit: null,
    ocr_followup: {
      state: "succeeded",
      source: { kind: "ocr-ready", artifact_id: "result-ocr-ready-1" },
      proposal_ref: `cop-${"a".repeat(40)}`,
      failure: null,
    },
    cancelled_before_commit: false,
    failure: null,
  }, { item_id: "book-1" });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(harness.calls.list.length, reads + 1);
  assert.deepEqual(harness.statuses, [{
    message: "Re-OCR complete — proposal ready",
    error: false,
  }]);

  harness.listeners[0]({
    job_id: "correction-ocr-2",
    operation_id: `correction-reocr:${"f".repeat(48)}`,
    terminal_state: "failed",
    image_commit: null,
    ocr_followup: null,
    cancelled_before_commit: false,
    failure: { code: "transform_job_unavailable", message: "job lost" },
  }, { item_id: "book-2" });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(harness.calls.list.length, reads + 1,
    "another item's job does not refresh this panel");
  assert.deepEqual(harness.statuses.at(-1), {
    message: "job lost",
    error: true,
  });

  harness.panel.destroy();
  assert.equal(harness.listeners.length, 0,
    "destroy releases the results subscription");
});

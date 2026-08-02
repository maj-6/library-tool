const assert = require("node:assert/strict");
const test = require("node:test");

const {
  createChPanel,
  defaultChOperationId,
} = require("../tools/whl_explorer/static/corrections/ch-panel");
const {
  FakeNode,
  deferred,
  fakeDocument,
} = require("./fixtures/corrections_fake_dom");


const ROW_KEY = "6e805628-39";
const OTHER_KEY = "8a7b43ca-28";


function chRow(overrides = {}) {
  return {
    index: 0,
    key: ROW_KEY,
    title: "A Modern Herbal Volume 1",
    author: "Grieve, Maud",
    year: "1931",
    fields: {
      title: "A Modern Herbal Volume 1",
      author: "Grieve, Maud",
      year: "1931",
      publisher: "Jonathan Cape",
      city: "London",
      pages: "888",
      condition: "good",
      illustrations: "line drawings",
      price: "12",
      categories: "Herbal",
    },
    ...overrides,
  };
}


function keyMatch(overrides = {}) {
  return {
    resolution: "key",
    key: ROW_KEY,
    title: "A Modern Herbal Volume 1",
    author: "Grieve, Maud",
    score: 0.92,
    adopted: ["author", "year"],
    conflicts: ["publisher"],
    row: chRow(),
    ...overrides,
  };
}


function statePayload(overrides = {}) {
  return {
    ok: true,
    item_id: "book-1",
    list_available: true,
    match: null,
    candidates: [],
    rejected: null,
    ...overrides,
  };
}


function candidate(overrides = {}) {
  return { ...chRow(), score: 0.914, ...overrides };
}


function response(status, body) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  };
}


function harness(handler, options = {}) {
  const documentRef = fakeDocument();
  const root = new FakeNode("section", documentRef);
  root.hidden = true;
  const calls = [];
  const changed = [];
  let operationCounter = 0;
  const panel = createChPanel({
    root,
    documentRef,
    fetchImpl: async (url, init) => {
      const call = {
        url,
        init,
        body: init && init.body ? JSON.parse(init.body) : null,
      };
      calls.push(call);
      return handler(call, calls.length);
    },
    operationIdFactory: options.operationIdFactory ||
      (() => `ch-op-${++operationCounter}`),
    onChanged: (body) => changed.push(body),
  }).mount();
  return { calls, changed, documentRef, panel, root };
}


function node(root, selector) {
  const found = root.querySelector(selector);
  assert.ok(found, `expected ${selector}`);
  return found;
}


function definitionEntries(fields) {
  const terms = fields.querySelectorAll("dt").map((dt) => dt.textContent);
  const values = fields.querySelectorAll("dd").map((dd) => dd.textContent);
  return terms.map((term, index) => [term, values[index]]);
}


test("a confirmed key match renders the record, markers, and a revoke action", async () => {
  const harnessed = harness(() =>
    response(200, statePayload({ match: keyMatch() })));

  await harnessed.panel.setItem("book-1");

  assert.deepEqual(harnessed.calls.map(({ url, init }) => [
    init.method, url, init.cache, init.credentials,
  ]), [[
    "GET", "/api/corrections/ch/state?item_id=book-1",
    "no-store", "same-origin",
  ]]);
  assert.equal(harnessed.root.hidden, false);
  const state = node(harnessed.root, "[data-ch-state]");
  assert.equal(state.getAttribute("data-ch-state"), "confirmed");
  assert.equal(
    node(harnessed.root, "[data-ch-state-text]").textContent, "Confirmed");
  const resolution = node(harnessed.root, "[data-ch-resolution]");
  assert.equal(resolution.hidden, false);
  assert.equal(resolution.textContent, "key");
  assert.equal(node(harnessed.root, "[data-ch-score]").textContent, "0.92");
  assert.equal(node(harnessed.root, "[data-ch-rematch-hint]").hidden, true);
  const fields = node(harnessed.root, "[data-ch-match-fields]");
  assert.equal(fields.hidden, false);
  assert.deepEqual(definitionEntries(fields), [
    ["Title", "A Modern Herbal Volume 1"],
    ["Author", "Grieve, Maud"],
    ["Year", "1931"],
    ["Publisher", "Jonathan Cape"],
    ["City", "London"],
    ["Pages", "888"],
    ["Condition", "good"],
    ["Illustrations", "line drawings"],
    ["Price", "12"],
    ["Categories", "Herbal"],
  ]);
  assert.equal(node(harnessed.root, "[data-ch-adopted]").textContent,
    "adopted: author, year");
  assert.equal(node(harnessed.root, "[data-ch-conflicts]").textContent,
    "scan kept: publisher");
  assert.equal(node(harnessed.root, "[data-ch-reject-match]").hidden, false);
  assert.equal(node(harnessed.root, "[data-ch-approve-match]").hidden, true);
  assert.equal(
    harnessed.root.querySelectorAll("[data-ch-candidate]").length, 0);
  assert.equal(node(harnessed.root, "[data-ch-rejected]").hidden, true);
});


test("a stale rematch shows the re-pin hint and re-approves the row's current key", async () => {
  const staleMatch = keyMatch({ resolution: "rematch", key: "deadbeef-39" });
  const harnessed = harness((call, index) => {
    if (index === 1) {
      return response(200, statePayload({ match: staleMatch }));
    }
    assert.equal(call.url, "/api/corrections/ch/approve");
    return response(200, {
      ok: true,
      replayed: false,
      adopted: ["author"],
      conflicts: [],
      match: keyMatch({ adopted: ["author"], conflicts: [] }),
    });
  });

  await harnessed.panel.setItem("book-1");
  assert.equal(node(harnessed.root, "[data-ch-state]")
    .getAttribute("data-ch-state"), "rematch");
  assert.equal(node(harnessed.root, "[data-ch-rematch-hint]").hidden, false);
  const approve = node(harnessed.root, "[data-ch-approve-match]");
  assert.equal(approve.hidden, false);

  approve.emit("click");
  await new Promise((resolve) => setImmediate(resolve));

  assert.deepEqual(harnessed.calls[1].body, {
    item_id: "book-1",
    key: ROW_KEY,
    operation_id: "ch-op-1",
  }, "re-approval uses the resolved row's key, not the stale stamp key");
  assert.equal(node(harnessed.root, "[data-ch-state]")
    .getAttribute("data-ch-state"), "confirmed");
  assert.equal(node(harnessed.root, "[data-ch-message]").textContent,
    "CH record linked.");
  assert.equal(harnessed.changed.length, 1);
});


test("candidates carry scores and approval reports adopted and kept-scan fields", async () => {
  let approveCall = 0;
  const harnessed = harness((call, index) => {
    if (index === 1) {
      return response(200, statePayload({
        candidates: [
          candidate(),
          candidate({ index: 2, key: OTHER_KEY, title: "Herbal Simples",
            author: "Fernie, W. T.", year: "1897", score: 0.62 }),
        ],
      }));
    }
    approveCall += 1;
    return response(200, {
      ok: true,
      replayed: approveCall > 1,
      adopted: ["author", "year", "publisher"],
      conflicts: ["title", "edition"],
      match: keyMatch({
        adopted: ["author", "year", "publisher"],
        conflicts: ["title", "edition"],
      }),
    });
  });

  await harnessed.panel.setItem("book-1");
  assert.equal(node(harnessed.root, "[data-ch-state]")
    .getAttribute("data-ch-state"), "proposed");
  assert.equal(node(harnessed.root, "[data-ch-state-text]").textContent,
    "Proposed — 2 candidates");
  const rows = harnessed.root.querySelectorAll("[data-ch-candidate]");
  assert.deepEqual(rows.map((row) =>
    row.getAttribute("data-ch-candidate")), [ROW_KEY, OTHER_KEY]);
  assert.match(rows[0].textContent, /0\.91/);
  assert.match(rows[1].textContent, /Fernie, W\. T\. · 1897 · 0\.62/);

  node(rows[0], "[data-ch-approve]").emit("click");
  await new Promise((resolve) => setImmediate(resolve));

  assert.deepEqual(harnessed.calls[1].body, {
    item_id: "book-1",
    key: ROW_KEY,
    operation_id: "ch-op-1",
  });
  assert.equal(node(harnessed.root, "[data-ch-state]")
    .getAttribute("data-ch-state"), "confirmed");
  assert.equal(node(harnessed.root, "[data-ch-message]").textContent,
    "Linked — the scan kept: title, edition.");
  assert.equal(node(harnessed.root, "[data-ch-conflicts]").textContent,
    "scan kept: title, edition");
  assert.equal(
    harnessed.root.querySelectorAll("[data-ch-candidate]").length, 0,
    "an approval clears the proposal list");
  assert.equal(harnessed.changed.length, 1);

  await harnessed.panel.approve(ROW_KEY);
  assert.equal(node(harnessed.root, "[data-ch-message]").textContent,
    "This CH link was already recorded.");
  assert.equal(harnessed.changed.length, 1,
    "a replayed decision does not re-run the merge refresh");
});


test("absent, unavailable, unresolved, and rejected states render quiet lines", async () => {
  const absent = harness(() => response(200, statePayload()));
  await absent.panel.setItem("book-1");
  assert.equal(node(absent.root, "[data-ch-state]")
    .getAttribute("data-ch-state"), "absent");
  assert.equal(node(absent.root, "[data-ch-state-text]").textContent,
    "No master-list match.");
  assert.equal(node(absent.root, "[data-ch-match-fields]").hidden, true);

  const unavailable = harness(() =>
    response(200, statePayload({ list_available: false })));
  await unavailable.panel.setItem("book-1");
  assert.equal(node(unavailable.root, "[data-ch-state]")
    .getAttribute("data-ch-state"), "unavailable");
  assert.equal(node(unavailable.root, "[data-ch-state-text]").textContent,
    "CH master list unavailable.");
  assert.equal(
    unavailable.root.querySelectorAll("[data-ch-approve]").length, 0);

  const unresolved = harness(() => response(200, statePayload({
    match: keyMatch({ resolution: "unresolved", row: null }),
    candidates: [candidate()],
  })));
  await unresolved.panel.setItem("book-1");
  assert.equal(node(unresolved.root, "[data-ch-state]")
    .getAttribute("data-ch-state"), "unresolved");
  const stampFields = node(unresolved.root, "[data-ch-match-fields]");
  assert.equal(stampFields.hidden, false);
  assert.deepEqual(definitionEntries(stampFields), [
    ["Title", "A Modern Herbal Volume 1"],
    ["Author", "Grieve, Maud"],
  ], "an unresolved approval still shows its stamped identity");
  assert.equal(node(unresolved.root, "[data-ch-match-actions]").hidden, true);
  assert.equal(
    unresolved.root.querySelectorAll("[data-ch-candidate]").length, 1,
    "candidates offer a way to approve something else");

  const rejected = harness(() => response(200, statePayload({
    rejected: { key: ROW_KEY, decided_at: "2026-08-02T12:00:00+00:00" },
  })));
  await rejected.panel.setItem("book-1");
  assert.equal(node(rejected.root, "[data-ch-state]")
    .getAttribute("data-ch-state"), "rejected");
  const rejectedLine = node(rejected.root, "[data-ch-rejected]");
  assert.equal(rejectedLine.hidden, false);
  assert.equal(node(rejected.root, "[data-ch-rejected-text]").textContent,
    `Rejected ${ROW_KEY} · 2026-08-02T12:00:00+00:00`);
  assert.ok(node(rejected.root, "[data-ch-unreject]"),
    "the rejected line offers Undo");
});


test("reject suppresses the candidate and rejecting the approved key revokes it", async () => {
  const decidedAt = "2026-08-02T12:34:56+00:00";
  const rejectedState = statePayload({
    rejected: { key: ROW_KEY, decided_at: decidedAt },
  });
  let rejectedYet = false;
  const proposal = harness((call, index) => {
    if (call.init.method === "GET") {
      return response(200, rejectedYet
        ? rejectedState
        : statePayload({ candidates: [candidate()] }));
    }
    assert.equal(call.url, "/api/corrections/ch/reject");
    assert.equal(index, 2);
    rejectedYet = true;
    return response(200, {
      ok: true,
      replayed: false,
      rejected: { key: ROW_KEY, decided_at: decidedAt },
    });
  });
  await proposal.panel.setItem("book-1");

  node(proposal.root, "[data-ch-reject]").emit("click");
  await new Promise((resolve) => setImmediate(resolve));

  assert.deepEqual(proposal.calls[1].body, {
    item_id: "book-1",
    key: ROW_KEY,
    operation_id: "ch-op-1",
  });
  assert.equal(proposal.calls.length, 3,
    "a recorded rejection reloads the canonical state");
  assert.equal(node(proposal.root, "[data-ch-state]")
    .getAttribute("data-ch-state"), "rejected");
  assert.equal(node(proposal.root, "[data-ch-message]").textContent,
    "CH record rejected.");
  assert.equal(node(proposal.root, "[data-ch-rejected]").hidden, false);
  assert.equal(proposal.changed.length, 1);

  let revoked = false;
  const approved = harness((call) => {
    if (call.init.method === "GET") {
      return response(200, revoked
        ? rejectedState
        : statePayload({ match: keyMatch() }));
    }
    revoked = true;
    return response(200, {
      ok: true,
      replayed: false,
      rejected: { key: ROW_KEY, decided_at: decidedAt },
    });
  });
  await approved.panel.setItem("book-1");
  assert.equal(node(approved.root, "[data-ch-state]")
    .getAttribute("data-ch-state"), "confirmed");

  node(approved.root, "[data-ch-reject-match]").emit("click");
  await new Promise((resolve) => setImmediate(resolve));

  assert.equal(node(approved.root, "[data-ch-state]")
    .getAttribute("data-ch-state"), "rejected");
  assert.equal(node(approved.root, "[data-ch-match-fields]").hidden, true,
    "revoking the approval removes the matched record");
});


test("non-capture and missing items hide the panel; other failures surface", async () => {
  const nonCapture = harness(() => response(422, {
    ok: false,
    error: "the Corrections item is not capture-backed",
    code: "not_capture_backed",
  }));
  await nonCapture.panel.setItem("book-1");
  assert.equal(nonCapture.root.hidden, true);
  assert.equal(node(nonCapture.root, "[data-ch-message]").textContent, "");

  const missing = harness(() => response(404, {
    ok: false,
    error: "no Corrections item has this id",
    code: "item_not_found",
  }));
  await missing.panel.setItem("book-1");
  assert.equal(missing.root.hidden, true);

  const failing = harness(() => response(500, {
    ok: false,
    error: "the engine session is unavailable",
    code: "engine_unavailable",
  }));
  await failing.panel.setItem("book-1");
  assert.equal(failing.root.hidden, false,
    "unexplained failures stay visible so the user can retry");
  const message = node(failing.root, "[data-ch-message]");
  assert.equal(message.textContent, "the engine session is unavailable");
  assert.equal(message.getAttribute("role"), "alert");

  await failing.panel.setItem(null);
  assert.equal(failing.root.hidden, true,
    "clearing the selection hides the panel");
});


test("a revision conflict re-fetches state and retries once with a fresh operation id", async () => {
  const conflictBody = {
    ok: false,
    error: "the item changed while the decision was being recorded",
    code: "item_revision_conflict",
  };
  const proposedState = statePayload({ candidates: [candidate()] });
  let approveAttempts = 0;
  const recovering = harness((call) => {
    if (call.init.method === "GET") return response(200, proposedState);
    approveAttempts += 1;
    if (approveAttempts === 1) return response(409, conflictBody);
    return response(200, {
      ok: true,
      replayed: false,
      adopted: ["author"],
      conflicts: [],
      match: keyMatch({ adopted: ["author"], conflicts: [] }),
    });
  });
  await recovering.panel.setItem("book-1");

  await recovering.panel.approve(ROW_KEY);

  const posts = recovering.calls.filter(
    (call) => call.init.method === "POST");
  assert.deepEqual(posts.map((call) => call.body.operation_id),
    ["ch-op-1", "ch-op-2"],
    "the retry never reuses the conflicted operation id");
  assert.equal(recovering.calls.filter(
    (call) => call.init.method === "GET").length, 2,
  "the conflict reloads state before retrying");
  assert.equal(node(recovering.root, "[data-ch-state]")
    .getAttribute("data-ch-state"), "confirmed");
  assert.equal(node(recovering.root, "[data-ch-message]").textContent,
    "CH record linked.");

  const stuck = harness((call) => call.init.method === "GET"
    ? response(200, proposedState)
    : response(409, conflictBody));
  await stuck.panel.setItem("book-1");
  await stuck.panel.approve(ROW_KEY);

  assert.equal(stuck.calls.filter(
    (call) => call.init.method === "POST").length, 2);
  const message = node(stuck.root, "[data-ch-message]");
  assert.equal(message.textContent,
    "The item changed elsewhere — the CH state was reloaded.");
  assert.equal(message.getAttribute("role"), "status",
    "a twice-conflicted decision informs quietly, not as an error");
  assert.deepEqual(stuck.changed, []);
});


test("a vanished master-list row surfaces the server's reason and reloads", async () => {
  let stateReads = 0;
  const harnessed = harness((call) => {
    if (call.init.method === "GET") {
      stateReads += 1;
      return response(200, statePayload({ candidates: [candidate()] }));
    }
    return response(409, {
      ok: false,
      error: "the key does not resolve to a current CH row",
      code: "ch_key_unknown",
    });
  });
  await harnessed.panel.setItem("book-1");

  await harnessed.panel.approve(ROW_KEY);

  assert.equal(stateReads, 2);
  const message = node(harnessed.root, "[data-ch-message]");
  assert.equal(message.textContent,
    "the key does not resolve to a current CH row");
  assert.equal(message.getAttribute("role"), "alert");
  assert.deepEqual(harnessed.changed, []);
});


test("default operation ids are portable, prefixed, and unique per decision", () => {
  const first = defaultChOperationId();
  const second = defaultChOperationId();
  for (const operationId of [first, second]) {
    assert.match(operationId, /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/);
    assert.match(operationId, /^ch-/);
  }
  assert.notEqual(first, second);

  const documentRef = fakeDocument();
  const root = new FakeNode("section", documentRef);
  const panel = createChPanel({
    root,
    documentRef,
    fetchImpl: async () => response(200, statePayload()),
  });
  assert.equal(panel.operationIdFactory, defaultChOperationId,
    "panels mint portable ids unless a factory is injected");
});


test("destroy clears the panel and drops in-flight results", async () => {
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  const harnessed = harness(async () => {
    await gate;
    return response(200, statePayload({ match: keyMatch() }));
  });
  const pending = harnessed.panel.setItem("book-1");

  harnessed.panel.destroy();
  release();
  await pending;

  assert.equal(harnessed.root.hidden, true);
  assert.equal(harnessed.root.children.length, 0);
});

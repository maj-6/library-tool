const assert = require("node:assert/strict");
const test = require("node:test");

const {
  captionDraftKey,
  createPropertiesInspector,
  effectiveCaption,
} = require("../tools/whl_explorer/static/corrections/properties");
const {
  FakeNode,
  fakeDocument,
} = require("./fixtures/corrections_fake_dom");


function artifact(overrides = {}) {
  return {
    key: { item_id: "book-1", artifact_id: "figure-1" },
    revision: "figure-r1",
    kind: "figure",
    label: "Medicinal plant plate",
    media_type: "image/png",
    resource_state: "available",
    resource: {
      resource_id: "figure-resource",
      revision: "resource-r1",
      variant: "display",
    },
    freshness: "current",
    source: {
      representation_id: "scan-1",
      representation_revision: "scan-r1",
      canvas_id: "page-4",
      canvas_revision: "page-r1",
    },
    caption_assertions: [
      {
        origin: "manual",
        revision: "caption-manual-r1",
        text: "Human caption",
        language: "en",
      },
      {
        origin: "machine",
        revision: "caption-machine-r1",
        text: "Machine caption",
        confidence: 0.87,
      },
    ],
    effective_caption: {
      origin: "manual",
      revision: "caption-manual-r1",
      text: "Human caption",
      language: "en",
    },
    role_assignments: [
      {
        origin: "manual",
        revision: "role-manual-r1",
        role: "figure",
      },
      {
        origin: "machine",
        revision: "role-machine-r1",
        role: "text",
        confidence: 0.73,
      },
    ],
    effective_role: "figure",
    provenance: {
      origin: "ocr",
      provider_id: "mistral",
      model: "pixtral-large",
    },
    ...overrides,
  };
}


function harness(options = {}) {
  const documentRef = fakeDocument();
  const root = new FakeNode("dl", documentRef);
  const statuses = [];
  let operation = 0;
  const inspector = createPropertiesInspector({
    root,
    documentRef,
    operationIdFactory: (action) => `${action.replace(".", "-")}-${++operation}`,
    onStatus: (...args) => statuses.push(args),
    ...options,
  }).mount();
  return { documentRef, inspector, root, statuses };
}


test("Properties visibly separates immutable machine facts from human assertions", () => {
  const { inspector, root } = harness();
  inspector.setSelection(artifact());

  const cards = root.querySelectorAll(".property-card");
  assert.equal(cards.length, 3);
  assert.match(cards[0].textContent, /Machine and source facts/);
  assert.match(cards[0].textContent, /Mistral/i);
  assert.match(cards[0].textContent, /Machine caption/);
  assert.match(cards[0].textContent, /Machine roletext/);
  assert.match(cards[0].textContent, /Machine role confidence0\.73/);
  assert.match(cards[0].textContent, /Human role overridefigure/);
  assert.match(cards[0].textContent, /Human caption overrideHuman caption/);
  assert.match(cards[1].textContent, /Human assertions/);
  assert.match(cards[1].textContent, /Manual caption/);
  assert.match(cards[1].textContent, /Human assertion at caption-manual-r1/);
  assert.match(cards[2].textContent, /Artifact data/);
  assert.equal(effectiveCaption(inspector.detail).text, "Human caption");
});


test("manual caption set uses exact artifact CAS pins and records an inverse", async () => {
  const calls = [];
  const history = [];
  const updated = artifact({
    revision: "figure-r2",
    caption_assertions: [
      {
        origin: "manual",
        revision: "caption-manual-r2",
        text: "Corrected caption",
        language: "la",
      },
      {
        origin: "machine",
        revision: "caption-machine-r1",
        text: "Machine caption",
      },
    ],
    effective_caption: {
      origin: "manual",
      revision: "caption-manual-r2",
      text: "Corrected caption",
      language: "la",
    },
  });
  const commands = {
    async setManualCaption(payload) {
      calls.push(["set", payload]);
      return {
        receipt: {
          inverse: {
            action: "caption.clear",
            expected_aggregate_revision: "aggregate-r2",
            expected_targets: [{
              kind: "artifact",
              target_id: "figure-1",
              before_revision: "figure-r1",
              after_revision: "figure-r2",
            }],
            payload: {},
          },
        },
        detail: updated,
      };
    },
    async clearManualCaption() { throw new Error("not used"); },
    async executeInverse(payload) {
      calls.push(["undo", payload]);
      return { detail: artifact({ revision: "figure-r3" }) };
    },
  };
  const { inspector } = harness({
    commands,
    history: { push: (entry) => history.push(entry) },
  });
  inspector.setSelection(artifact());
  inspector.saveDraft({ text: "Corrected caption", language: "la" });

  await inspector.setManualCaption();

  assert.equal(calls[0][0], "set");
  assert.deepEqual({
    itemId: calls[0][1].itemId,
    artifactId: calls[0][1].artifactId,
    expectedArtifactRevision: calls[0][1].expectedArtifactRevision,
    text: calls[0][1].text,
    operationId: calls[0][1].operationId,
    language: calls[0][1].language,
  }, {
    itemId: "book-1",
    artifactId: "figure-1",
    expectedArtifactRevision: "figure-r1",
    text: "Corrected caption",
    operationId: "caption-set-1",
    language: "la",
  });
  assert.ok("signal" in calls[0][1]);
  assert.equal(inspector.detail.revision, "figure-r2");
  assert.equal(history.length, 1);
  assert.equal(history[0].inverse.action, "caption.clear");

  await inspector.undoLast();
  assert.equal(calls[1][0], "undo");
  assert.equal(calls[1][1].itemId, "book-1");
  assert.equal(calls[1][1].inverse.action, "caption.clear");
  assert.equal(calls[1][1].operationId, "correction-undo-2");
});


test("clearing a manual caption reveals the retained machine assertion", async () => {
  const calls = [];
  const cleared = artifact({
    revision: "figure-r2",
    caption_assertions: [{
      origin: "machine",
      revision: "caption-machine-r1",
      text: "Machine caption",
      confidence: 0.87,
    }],
    effective_caption: {
      origin: "machine",
      revision: "caption-machine-r1",
      text: "Machine caption",
      confidence: 0.87,
    },
  });
  const { inspector, root } = harness({
    commands: {
      async setManualCaption() { throw new Error("not used"); },
      async clearManualCaption(payload) {
        calls.push(payload);
        return {
          receipt: {
            inverse: {
              action: "caption.set",
              expected_aggregate_revision: "aggregate-r2",
              expected_targets: [],
              payload: {
                assertion: {
                  text: "Human caption",
                  language: "en",
                },
              },
            },
          },
          detail: cleared,
        };
      },
      async executeInverse() { throw new Error("not used"); },
    },
  });
  inspector.setSelection(artifact());

  await inspector.clearManualCaption();

  assert.deepEqual({
    itemId: calls[0].itemId,
    artifactId: calls[0].artifactId,
    expectedArtifactRevision: calls[0].expectedArtifactRevision,
    operationId: calls[0].operationId,
  }, {
    itemId: "book-1",
    artifactId: "figure-1",
    expectedArtifactRevision: "figure-r1",
    operationId: "caption-clear-1",
  });
  assert.equal(effectiveCaption(inspector.detail).origin, "machine");
  assert.equal(effectiveCaption(inspector.detail).text, "Machine caption");
  assert.match(root.textContent, /machine caption is now effective/i);
  assert.equal(inspector.detail.captionAssertions.some(
    (assertion) => assertion.origin === "manual"), false);
});


test("CAS conflicts reload current detail while retaining the attempted draft", async () => {
  const drafts = new Map();
  let reloads = 0;
  const conflict = Object.assign(new Error("changed elsewhere"), {
    code: "artifact_revision_conflict",
    status: 409,
    details: {
      expected_revision: "figure-r1",
      current_revision: "figure-r2",
    },
  });
  const { inspector, root } = harness({
    draftStore: drafts,
    commands: {
      async setManualCaption() { throw conflict; },
      async clearManualCaption() { throw new Error("not used"); },
      async executeInverse() { throw new Error("not used"); },
    },
    async reloadDetail() {
      reloads += 1;
      return artifact({ revision: "figure-r2" });
    },
  });
  inspector.setSelection(artifact());
  inspector.saveDraft({ text: "My unsaved correction", language: "fr" });

  await inspector.setManualCaption();

  assert.equal(reloads, 1);
  assert.equal(inspector.detail.revision, "figure-r2");
  assert.equal(inspector.draft.text, "My unsaved correction");
  assert.equal(inspector.draft.language, "fr");
  assert.equal(drafts.get(captionDraftKey(inspector.detail)).text,
    "My unsaved correction");
  assert.match(root.textContent, /changed elsewhere/i);
  assert.match(root.textContent, /draft was kept/i);
});


test("a re-OCR machine update cannot displace an existing manual assertion", () => {
  const { inspector } = harness();
  inspector.setSelection(artifact());
  inspector.setSelection(artifact({
    revision: "figure-r2",
    caption_assertions: [
      {
        origin: "manual",
        revision: "caption-manual-r1",
        text: "Human caption",
      },
      {
        origin: "machine",
        revision: "caption-machine-r2",
        text: "New OCR caption",
        confidence: 0.96,
      },
    ],
    effective_caption: {
      origin: "manual",
      revision: "caption-manual-r1",
      text: "Human caption",
    },
  }));

  assert.equal(effectiveCaption(inspector.detail).origin, "manual");
  assert.equal(effectiveCaption(inspector.detail).text, "Human caption");
  assert.equal(inspector.detail.captionAssertions.find(
    (assertion) => assertion.origin === "machine").text, "New OCR caption");
});

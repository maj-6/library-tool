const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const root = path.join(__dirname, "..");
const app = fs.readFileSync(path.join(
  root, "tools", "whl_explorer", "static", "app.js"), "utf8");
const template = fs.readFileSync(path.join(
  root, "tools", "whl_explorer", "templates", "index.html"), "utf8");

function declaration(name) {
  const asyncMarker = `async function ${name}(`;
  const plainMarker = `function ${name}(`;
  const asyncStart = app.indexOf(asyncMarker);
  const start = asyncStart >= 0 ? asyncStart : app.indexOf(plainMarker);
  assert.ok(start >= 0, `${name} declaration is present`);
  const end = /^}\r?$/m.exec(app.slice(start));
  assert.ok(end, `${name} declaration has a closing brace`);
  return app.slice(start, start + end.index + end[0].length);
}

function element(overrides = {}) {
  const classes = new Set();
  return {
    hidden: true,
    textContent: "",
    classList: {
      add: (...names) => names.forEach((name) => classes.add(name)),
      remove: (...names) => names.forEach((name) => classes.delete(name)),
      contains: (name) => classes.has(name),
    },
    focus() {},
    ...overrides,
  };
}

function harness({ expectedHash = "a".repeat(64), responseHash = expectedHash } = {}) {
  const elements = {
    "scan-assessment-popover": element(),
    "scan-assessment-title": element(),
    "scan-assessment-verdict": element(),
    "scan-assessment-status": element(),
    "scan-assessment-body": element(),
    "scan-assessment-close": element(),
  };
  const requests = [];
  const sourceId = "manual/one?private=false";
  const trigger = {
    textContent: "High",
    dataset: {
      scanVerdict: "Worth reviewing",
      scanNamespace: "manual_entries",
      scanSourceId: sourceId,
      scanSourceSha256: expectedHash,
    },
    setAttribute(name, value) { this[name] = value; },
  };
  const reasoning = "<img src=x onerror=globalThis.compromised=true>\n**Markdown**";
  const context = vm.createContext({
    AbortController,
    encodeURIComponent,
    el: (id) => elements[id],
    hideTip() {},
    positionScanAssessmentPopover() {},
    fetch: async (...args) => {
      requests.push(args);
      return {
        ok: true,
        status: 200,
        json: async () => ({
          assessment: {
            text: reasoning,
            manifest: {
              namespace: "manual_entries",
              source_id: sourceId,
              provenance: { source_row_sha256: responseHash },
            },
          },
        }),
      };
    },
  });
  vm.runInContext(`
    let scanAssessmentTrigger = null;
    let scanAssessmentRequest = null;
    let scanAssessmentGeneration = 0;
    ${declaration("scanAssessmentResponseText")}
    ${declaration("scanAssessmentResponseManifest")}
    ${declaration("openScanAssessmentPopover")}
    this.open = openScanAssessmentPopover;
  `, context);
  return { context, elements, requests, trigger, reasoning };
}

test("reasoning remains inert text and uses an encoded exact source reference", async () => {
  const { context, elements, requests, trigger, reasoning } = harness();

  await context.open(trigger);

  assert.equal(requests.length, 1);
  assert.equal(requests[0][0],
    "/api/v1/scan-assessments/manual_entries/manual%2Fone%3Fprivate%3Dfalse");
  assert.equal(elements["scan-assessment-body"].textContent, reasoning);
  assert.equal(context.compromised, undefined);
  assert.equal(elements["scan-assessment-status"].textContent, "Full reasoning");
  assert.equal(trigger["aria-expanded"], "true");
});

test("source-hash mismatch fails closed without rendering stale reasoning", async () => {
  const { context, elements, trigger } = harness({
    expectedHash: "a".repeat(64),
    responseHash: "b".repeat(64),
  });

  await context.open(trigger);

  assert.equal(elements["scan-assessment-body"].textContent, "");
  assert.equal(
    elements["scan-assessment-popover"].classList.contains("is-error"),
    true,
  );
  assert.match(elements["scan-assessment-status"].textContent,
    /older or different source record/);
});

test("long reasoning is keyboard-scrollable", () => {
  assert.match(template,
    /<pre id="scan-assessment-body"[^>]*tabindex="0"[^>]*><\/pre>/);
});

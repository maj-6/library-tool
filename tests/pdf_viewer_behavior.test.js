const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");


const source = fs.readFileSync(
  path.join(__dirname, "..", "tools", "whl_explorer", "static", "app.js"),
  "utf8",
);
const start = source.indexOf("function pdfOcrMode(");
const end = source.indexOf("\nfunction createPdfViewer()", start);
assert.ok(start >= 0 && end > start, "pdf OCR preference helper is present");

const context = vm.createContext({});
vm.runInContext(
  `${source.slice(start, end)}\nthis.pdfOcrMode = pdfOcrMode;`,
  context,
);
const mode = (wanted, requested, hasText) =>
  JSON.parse(JSON.stringify(context.pdfOcrMode(wanted, requested, hasText)));

test("a textless PDF does not erase the default OCR comparison preference", () => {
  const textless = mode(true, null, false);
  assert.deepEqual(textless, { wanted: true, on: false });
  assert.deepEqual(mode(textless.wanted, null, true), { wanted: true, on: true });
});

test("an explicit PDF-only choice remains off when OCR later becomes available", () => {
  const disabled = mode(true, false, false);
  assert.deepEqual(disabled, { wanted: false, on: false });
  assert.deepEqual(mode(disabled.wanted, null, true), { wanted: false, on: false });
  assert.deepEqual(mode(disabled.wanted, true, true), { wanted: true, on: true });
});

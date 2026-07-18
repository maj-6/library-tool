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
  const start = app.indexOf(`function ${name}(`);
  assert.ok(start >= 0, `${name} declaration is present`);
  const end = /^}\r?$/m.exec(app.slice(start));
  assert.ok(end, `${name} declaration has a closing brace`);
  return app.slice(start, start + end.index + end[0].length);
}

const context = vm.createContext({
  bookTitleText: (book, fallback = "PDF") => {
    const title = String((book && book.title) || fallback || "");
    const volume = String((book && book.volume) || "").trim();
    return volume ? `Vol. ${volume} ${title}` : title;
  },
});
vm.runInContext([
  declaration("smartScanRefBody"),
  declaration("smartScanRefTitle"),
  declaration("smartScanRemaining"),
  declaration("smartScanPageSummary"),
  "this.api = { smartScanRefBody, smartScanRefTitle, smartScanRemaining, smartScanPageSummary };",
].join("\n"), context);

test("Smart Scan request bodies prefer the selected local PDF", () => {
  assert.deepEqual(
    JSON.parse(JSON.stringify(context.api.smartScanRefBody({
      target: "build:a", label: "A", pdf: "selected/a.pdf",
      url: "https://example.test/a.pdf",
    }))),
    { target: "build:a", label: "A", pdf: "selected/a.pdf" },
  );
  assert.deepEqual(
    JSON.parse(JSON.stringify(context.api.smartScanRefBody({
      target: "whl:2", url: "https://example.test/b.pdf",
    }))),
    { target: "whl:2", label: "", url: "https://example.test/b.pdf" },
  );
});

test("manual marker counts remaining documents and sorts marked pages", () => {
  assert.equal(context.api.smartScanRemaining(0, 3), "3 PDFs left to mark");
  assert.equal(context.api.smartScanRemaining(2, 3), "1 PDF left to mark");
  assert.equal(context.api.smartScanPageSummary(new Set()), "None marked");
  assert.equal(context.api.smartScanPageSummary(new Set([8, 2, 3])), "2, 3, 8");
});

test("manual marker and Smart Scan job titles carry volume metadata", () => {
  const ref = { target: "manual:a", label: "The Herbal", volume: "IV",
    pdf: "selected/a.pdf" };
  assert.equal(context.api.smartScanRefTitle(ref), "Vol. IV The Herbal");
  assert.deepEqual(JSON.parse(JSON.stringify(context.api.smartScanRefBody(ref))), {
    target: "manual:a", label: "The Herbal", volume: "IV", pdf: "selected/a.pdf",
  });
  assert.ok(declaration("procPdfForRow").includes("volume: bookVolumeValue(b)"));
  assert.ok(declaration("renderSmartScanMarker").includes("bookTitleHtml"));
});

test("manual marker is keyboard-driven and saves a compact PDF before run", () => {
  for (const id of [
    "smartscan-mark-overlay", "smartscan-mark-left", "smartscan-mark-image",
    "smartscan-mark-prev", "smartscan-mark-next", "smartscan-mark-toggle",
    "smartscan-mark-confirm", "set-smartscan-manual-pages",
    "set-smartscan-instructions", "proc-manual-pages",
  ]) {
    assert.match(template, new RegExp(`id=["']${id}["']`), `${id} exists`);
  }
  const keyHandler = declaration("onSmartScanMarkerKey");
  for (const key of ["ArrowLeft", "ArrowRight", 'ev.key === " "', 'toLowerCase() === "t"', "Enter"])
    assert.ok(keyHandler.includes(key), `${key} is handled`);
  const flow = app.slice(
    app.indexOf("async function confirmSmartScanMarker("),
    app.indexOf("// Smart Scan: one background job", app.indexOf("function markSmartScanPdf(")),
  );
  assert.ok(flow.includes('/api/process/smartscan/prepare'));
  assert.ok(flow.includes('/api/process/smartscan/select-pages'));
  const run = app.slice(
    app.indexOf("async function procRunSmartScan("),
    app.indexOf("function procPollSmartScan", app.indexOf("async function procRunSmartScan(")),
  );
  assert.ok(run.includes("markSmartScanPdfs(refs)"));
});

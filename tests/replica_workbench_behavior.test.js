// Pure-function behavior of the Replica workbench (app.js): the mechanical
// normalization proposal and the Apply page-range parser. Extracted from the
// source and evaluated directly — both are DOM-free by design.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const source = fs.readFileSync(path.join(
  __dirname, "..", "tools", "whl_explorer", "static", "app.js"), "utf8");

function fn(name) {
  const m = source.match(new RegExp("function " + name + "\\([\\s\\S]*?\\n}"));
  assert.ok(m, name + " is present in app.js");
  return eval("(" + m[0] + ")");   // eslint-disable-line no-eval
}

const rwNormalize = fn("rwNormalize");
const rwParsePages = fn("rwParsePages");
const rwDistribute = fn("rwDistribute");
const rwDetectionOutcome = fn("rwDetectionOutcome");

function functionSource(name) {
  const start = source.indexOf(`function ${name}(`);
  const asyncStart = source.indexOf(`async function ${name}(`);
  const at = start >= 0 ? start : asyncStart;
  assert.ok(at >= 0, name + " is present in app.js");
  const end = source.indexOf("\n}\n", at);
  assert.ok(end > at, name + " has a closing brace");
  return source.slice(at, end + 2);
}

test("rwDistribute splits by weight at paragraph bounds and survives edges", () => {
  // a body-less page (title, plate) used to crash the translation preview
  assert.deepEqual(rwDistribute("some text", []), []);
  assert.deepEqual(rwDistribute("", [3, 1]), ["", ""]);
  const out = rwDistribute("aaa\n\nbbb\n\nc", [2, 1]);
  assert.equal(out.length, 2);
  assert.equal(out.join("\n\n"), "aaa\n\nbbb\n\nc");   // nothing lost
  // Recompute the boundary after every advance: a cached first threshold
  // used to skip directly to the final region here.
  assert.deepEqual(rwDistribute("a\n\nb\n\nc", [1, 1, 1]), ["a", "b", "c"]);
  // one paragraph, many regions: everything lands somewhere, none undefined
  const one = rwDistribute("only", [1, 1, 1]);
  assert.equal(one.filter((s) => s === "only").length, 1);
  assert.ok(one.every((s) => typeof s === "string"));
});

test("rwNormalize resolves long s, ligatures, and hyphenation to a fixpoint", () => {
  // three-line hyphenation was the review's regression: a single global
  // pass consumed every second break ("mate-\nria" survived)
  assert.equal(rwNormalize("ma-\nte-\nria vnd Waſſer, ﬁne Oele"),
               "materia vnd Wasser, fine Oele");
  assert.equal(rwNormalize("Waﬅe ﬃ ﬄ ﬂy ﬀable"), "Waste ffi ffl fly ffable");
  assert.equal(rwNormalize(""), "");
  // the join wants a letter before the hyphen: a bare dash line survives
  assert.equal(rwNormalize("see —\nnote"), "see —\nnote");
});

test("rwParsePages parses ranges, dedupes, sorts, and caps at 500", () => {
  assert.deepEqual(rwParsePages("120"), [120]);
  assert.deepEqual(rwParsePages("118-120, 118, 5"), [5, 118, 119, 120]);
  assert.deepEqual(rwParsePages("120-118"), [118, 119, 120]);
  assert.deepEqual(rwParsePages(null), []);
  assert.deepEqual(rwParsePages("abc, 0, -3"), []);
  // the server rejects >500 pages outright; the parser must cap, not leak 501
  assert.equal(rwParsePages("1-600").length, 500);
});

test("Replica detection presents every terminal job outcome distinctly", () => {
  assert.deepEqual(rwDetectionOutcome({
    state: "done", outputs: [{ kind: "replica.region-proposal" }],
  }), {
    terminal: true, state: "done", error: false,
    message: "DETECT :: proposal ready",
  });
  assert.equal(rwDetectionOutcome({ state: "done", outputs: [] }).message,
    "DETECT :: regions updated");
  assert.equal(rwDetectionOutcome({ state: "cancelled" }).message,
    "DETECT :: cancelled");
  assert.equal(rwDetectionOutcome({ state: "interrupted" }).message,
    "DETECT :: interrupted — retry");
  assert.equal(rwDetectionOutcome({
    state: "failed", error: { message: "provider unavailable" },
  }).message, "DETECT :: failed — provider unavailable");
  assert.equal(rwDetectionOutcome({ state: "running" }).terminal, false);
});

test("Replica Detect observes its returned engine job instead of OCR page markers", () => {
  const start = functionSource("rwDetectPage");
  const watch = functionSource("rwWatchDetection");
  assert.match(start, /engineClient\.replica\.detection\.start/);
  assert.match(start, /rwWatchDetection/);
  assert.match(watch, /engineClient\.jobs\.get/);
  assert.doesNotMatch(start + watch, /ocrQueuePages|ocrState\.pageRunning|setInterval/);
});

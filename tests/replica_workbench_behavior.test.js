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

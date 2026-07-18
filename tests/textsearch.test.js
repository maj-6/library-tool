const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");


const root = path.join(__dirname, "..");
const websiteTextsearch = fs.readFileSync(
  path.join(root, "website", "assets", "textsearch.js"), "utf8");

function searchApi() {
  const context = vm.createContext({});
  const source = websiteTextsearch.replace(/^export function /gm, "function ");
  vm.runInContext(
    `${source}\nthis.api = { normalizeSearchText, findMatchRanges, searchPages };`,
    context,
  );
  return context.api;
}

// vm-context arrays are another realm's; flatten them before deepEqual
const plain = (value) => JSON.parse(JSON.stringify(value));

test("early-modern folding: long s, ligatures, ae/oe", () => {
  const { normalizeSearchText } = searchApi();
  assert.equal(normalizeSearchText("Phyſick"), "physick");
  assert.equal(normalizeSearchText("oﬃce ﬁre ﬂoure aﬀection"), "office fire floure affection");
  assert.equal(normalizeSearchText("beﬅ moﬆ"), "best most");
  assert.equal(normalizeSearchText("Cæſar Œconomy"), "caesar oeconomy");
});

test("diacritics fold to their base letters", () => {
  const { normalizeSearchText } = searchApi();
  assert.equal(normalizeSearchText("FLORA RÚSTICA"), "flora rustica");
  assert.equal(normalizeSearchText("azafrán de los prados"), "azafran de los prados");
});

test("line-break hyphenation joins the split word", () => {
  const { normalizeSearchText, searchPages } = searchApi();
  assert.equal(normalizeSearchText("phy-\nsick"), "physick");
  assert.equal(normalizeSearchText("phy- \r\n  sick garden"), "physick garden");

  const hits = searchPages([{ page: 4, body: "Of the vertues of phy-\nsick herbs." }], "physick");
  assert.equal(hits.length, 1);
  assert.equal(hits[0].page, 4);
  // the snippet is cut from the ORIGINAL text, hyphen and line break intact
  assert.equal(hits[0].snippet.slice(hits[0].matchStart, hits[0].matchEnd), "phy-\nsick");
});

test("'physic' matches 'physick' and 'phyſick'", () => {
  const { searchPages } = searchApi();
  const hits = searchPages([
    { page: 1, body: "The English Physick Garden." },
    { page: 2, body: "A treatise of Phyſick." },
  ], "physic");
  assert.equal(hits.length, 2);
  assert.deepEqual(plain(hits.map((h) => h.page)), [1, 2]);
  assert.equal(hits[0].snippet.slice(hits[0].matchStart, hits[0].matchEnd), "Physic");
  assert.equal(hits[1].snippet.slice(hits[1].matchStart, hits[1].matchEnd), "Phyſic");
});

test("a query spanning collapsed whitespace still matches", () => {
  const { findMatchRanges } = searchApi();
  const body = "the physick\n\n  garden of London";
  const ranges = findMatchRanges(body, "physick   garden");
  assert.equal(ranges.length, 1);
  assert.equal(body.slice(ranges[0][0], ranges[0][1]), "physick\n\n  garden");
});

test("match ranges map back to original offsets across foldings", () => {
  const { findMatchRanges } = searchApi();
  const body = "Phyſick, and more PHYSICK.";
  const ranges = findMatchRanges(body, "physick");
  assert.equal(ranges.length, 2);
  assert.equal(body.slice(ranges[0][0], ranges[0][1]), "Phyſick");
  assert.equal(body.slice(ranges[1][0], ranges[1][1]), "PHYSICK");
});

test("snippets carry bounded context cut on word boundaries", () => {
  const { searchPages } = searchApi();
  const body = `${"before ".repeat(30)}the physick garden of London ${"after ".repeat(30)}`.trim();
  const [hit] = searchPages([{ page: 7, body }], "physick");
  assert.equal(hit.snippet.slice(hit.matchStart, hit.matchEnd), "physick");
  assert.ok(hit.snippet.length <= 7 + 2 * 60);
  assert.ok(hit.cutStart && hit.cutEnd);
  const at = body.indexOf(hit.snippet);
  assert.ok(at > 0);
  assert.equal(body[at - 1], " ");                      // starts at a word start
  assert.equal(body[at + hit.snippet.length], " ");     // ends at a word end
});

test("hits stay page-ordered and cap at three per page with a more count", () => {
  const { searchPages } = searchApi();
  const hits = searchPages([
    { page: 1, body: "nothing to see" },
    { page: 2, body: "physick and physick and physick and physick and physick" },
    { page: 3, body: "one physick only" },
  ], "physick");
  assert.equal(hits.length, 4);
  assert.deepEqual(plain(hits.map((h) => h.page)), [2, 2, 2, 3]);
  assert.equal(hits[2].more, 2);       // two of page 2's five matches unreported
  assert.equal(hits[3].more, 0);
});

test("blank and sub-length queries return nothing", () => {
  const { searchPages, findMatchRanges } = searchApi();
  assert.deepEqual(plain(searchPages([{ page: 1, body: "text" }], "   ")), []);
  assert.deepEqual(plain(findMatchRanges("text", "")), []);
});

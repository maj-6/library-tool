const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(
  __dirname, "..", "tools", "whl_explorer", "static", "app.js"), "utf8");

function declaration(name) {
  const start = source.indexOf(`function ${name}(`);
  assert.ok(start >= 0, `${name} declaration is present`);
  const end = /^}\r?$/m.exec(source.slice(start));
  assert.ok(end, `${name} declaration has a closing brace`);
  return source.slice(start, start + end.index + end[0].length);
}

function harness(rows) {
  const state = { downloadedIds: new Set() };
  const context = vm.createContext({
    state,
    combinedRows: () => rows,
    getVerify: (row, sourceName) =>
      row.approvedSource === sourceName ? "approved" : "pending",
    getManualUrl: () => "",
    addedRankByRowId: () => new Map(rows.map((row, index) => [row.id, index])),
    buildGroupIdFor: (book) => book.volume ? `group:${book.volume}` : "",
  });
  vm.runInContext(`
const ARCHIVE_NAMES = { internet_archive: "Internet Archive", hathitrust: "HathiTrust" };
${declaration("bookVolumeValue")}
${declaration("capturedSourceMeta")}
${declaration("approvedSources")}
${declaration("buildSeedFromSource")}
this.api = { approvedSources, buildSeedFromSource };`, context);
  return context.api;
}

test("approved source rows retain normalized volume metadata through build seeding", () => {
  const api = harness([
    {
      id: "local", localPdf: "downloads/local.pdf",
      book: { title: "Local Flora", volume: "2", category_ids: [] },
    },
    {
      id: "remote", localPdf: "", approvedSource: "internet_archive",
      book: { title: "Remote Flora", volume_number: "IV", category_ids: [] },
      scans: { internet_archive: { available: true, best_match: {
        identifier: "remote-flora", title: "Remote scan",
        url: "https://archive.org/details/remote-flora",
      } } },
    },
  ]);

  const sources = api.approvedSources();
  assert.equal(sources.length, 2);
  assert.equal(sources[0].volume, "2");
  assert.equal(sources[0].volume_number, "");
  assert.equal(sources[1].volume, "IV", "legacy volume_number is normalized for consumers");
  assert.equal(sources[1].volume_number, "IV", "legacy metadata is also preserved");

  const seed = api.buildSeedFromSource(sources[1]);
  assert.equal(seed.volume, "IV");
  assert.equal(seed.group_id, "group:IV");
  assert.equal(seed.title, "Remote Flora");
});

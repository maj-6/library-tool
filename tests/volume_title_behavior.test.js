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

const context = vm.createContext({
  state: { builds: {} },
  setBaseTitle: (book) => String((book && book.title) || "").trim(),
  esc: (value) => String(value == null ? "" : value)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;"),
});
vm.runInContext(`${declaration("bookVolumeValue")}
${declaration("bookTitleText")}
${declaration("bookTitleHtml")}
${declaration("jobBookRecord")}
${declaration("jobBookTitleHtml")}
this.api = { bookTitleText, bookTitleHtml, jobBookTitleHtml };`, context);

test("volume metadata renders as an escaped tag before the unchanged title", () => {
  const book = { title: "A <Herbal>", volume: '2" onclick="bad' };
  const before = JSON.stringify(book);
  const html = context.api.bookTitleHtml(book);

  assert.match(html, /class="volume-title-tag">Vol\. 2&quot; onclick=&quot;bad<\/span>/);
  assert.match(html, /class="book-title-text">A &lt;Herbal&gt;<\/span>/);
  assert.ok(html.indexOf("volume-title-tag") < html.indexOf("book-title-text"));
  assert.equal(JSON.stringify(book), before, "formatting does not mutate stored metadata");
});

test("plain display text and legacy volume_number use the same prefix", () => {
  assert.equal(context.api.bookTitleText({ title: "Flora", volume_number: "IV" }),
    "Vol. IV Flora");
  assert.equal(context.api.bookTitleText({
    title: "Legacy Flora", volume: "", volume_number: "IV",
  }), "Vol. IV Legacy Flora", "a blank modern field does not mask legacy metadata");
  assert.doesNotMatch(context.api.bookTitleHtml({ title: "Flora" }),
    /volume-title-tag/);
});

test("job titles resolve the live build volume and retain queued fallback metadata", () => {
  context.state.builds.live = { title: "Current Herbal", volume: "II" };
  const live = context.api.jobBookTitleHtml({
    buildId: "live", book: "Stale Herbal", volume: "I",
  }, "live");
  assert.match(live, /volume-title-tag">Vol\. II<\/span>/);
  assert.match(live, /book-title-text">Current Herbal<\/span>/);

  const queued = context.api.jobBookTitleHtml({
    book: "Queued Herbal", volume: "IV",
  }, "missing");
  assert.match(queued, /volume-title-tag">Vol\. IV<\/span>/);
  assert.match(queued, /book-title-text">Queued Herbal<\/span>/);
});

test("hiding repeated volume titles keeps the required volume tag visible", () => {
  assert.match(source,
    /hideTitle\s*\n\s*\? `<span class="book-title-display"><span class="volume-title-tag">Vol\. \$\{esc\(bookVolumeValue\(b\)\)\}<\/span><\/span>`/);
});

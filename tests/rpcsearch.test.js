// The client half of the search_volume RPC contract (issue #139):
// rpcSnippetHtml turns ts_headline's «...» markers into <mark> AFTER escaping,
// and rpcHitsUsable is the reader's fallback decision -- anything it rejects
// (error, zero hits, malformed rows) drops to the client-side search path.
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
    `${source}\nthis.api = { rpcSnippetHtml, rpcHitsUsable };`,
    context,
  );
  return context.api;
}

test("marker pairs become <mark> around escaped text", () => {
  const { rpcSnippetHtml } = searchApi();
  assert.equal(rpcSnippetHtml("the «physick» garden"),
    "the <mark>physick</mark> garden");
  assert.equal(rpcSnippetHtml("of «physick» and «surgery» both"),
    "of <mark>physick</mark> and <mark>surgery</mark> both");
});

test("every segment is escaped -- outside, inside, and after the marks", () => {
  const { rpcSnippetHtml } = searchApi();
  assert.equal(rpcSnippetHtml(`<b>bold</b> «a & "b"» 'tail'`),
    "&lt;b&gt;bold&lt;/b&gt; <mark>a &amp; &quot;b&quot;</mark> &#39;tail&#39;");
  // a snippet that is nothing but hostile text still cannot smuggle HTML
  assert.equal(rpcSnippetHtml("«<script>alert(1)</script>»"),
    "<mark>&lt;script&gt;alert(1)&lt;/script&gt;</mark>");
});

test("unpaired and stray markers are stripped, never rendered", () => {
  const { rpcSnippetHtml } = searchApi();
  assert.equal(rpcSnippetHtml("broken « marker"), "broken  marker");
  assert.equal(rpcSnippetHtml("stray » here"), "stray  here");
  assert.equal(rpcSnippetHtml("«a» then « a tail"),
    "<mark>a</mark> then  a tail");
  // guillemets quoted in the page text itself cannot fake extra markers
  assert.equal(rpcSnippetHtml("««a»»"), "<mark>a</mark>");
});

test("empty and non-string snippets render to nothing", () => {
  const { rpcSnippetHtml } = searchApi();
  assert.equal(rpcSnippetHtml(""), "");
  assert.equal(rpcSnippetHtml(null), "");
  assert.equal(rpcSnippetHtml(undefined), "");
});

test("the fallback decision accepts only a non-empty well-formed hit list", () => {
  const { rpcHitsUsable } = searchApi();
  assert.equal(rpcHitsUsable(
    [{ page: 3, rank: 0.5, snippet: "a «hit»" }]), true);
  assert.equal(rpcHitsUsable(
    [{ page: 1, rank: 0.9, snippet: "x" }, { page: 2, rank: 0.1, snippet: "y" }]), true);
});

test("errors, zero hits, and malformed rows all fall back", () => {
  const { rpcHitsUsable } = searchApi();
  assert.equal(rpcHitsUsable(null), false);        // the reader passes null on RPC error
  assert.equal(rpcHitsUsable(undefined), false);
  assert.equal(rpcHitsUsable([]), false);          // zero hits: client search may still match
  assert.equal(rpcHitsUsable("no"), false);        // not even a list
  assert.equal(rpcHitsUsable([{ page: "3", snippet: "x" }]), false);   // page not an int
  assert.equal(rpcHitsUsable([{ page: 0, snippet: "x" }]), false);     // no page 0
  assert.equal(rpcHitsUsable([{ page: 2 }]), false);                   // snippet missing
  assert.equal(rpcHitsUsable([{ page: 2, snippet: "x" }, null]), false);
});

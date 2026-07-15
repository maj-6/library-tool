const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const appPath = path.join(__dirname, "..", "tools", "whl_explorer", "static", "app.js");
const source = fs.readFileSync(appPath, "utf8");
const start = source.indexOf("function publishSlug(");
const end = source.indexOf("function publishSafeUrl(", start);
assert.ok(start >= 0 && end > start, "Publish tree function block is present");

const state = {
  publishEntries: [],
  publishOpen: new Set(),
  publishClosed: new Set(),
  publishSel: null,
  settings: { publishGroup: "sets" },
};
const context = vm.createContext({
  state,
  setBaseTitle: (book) => String(book.title || "").trim(),
  esc: (value) => String(value == null ? "" : value)
    .replace(/&/g, "&amp;").replace(/"/g, "&quot;")
    .replace(/</g, "&lt;").replace(/>/g, "&gt;"),
  el: () => { throw new Error("DOM access is outside the tree-model test"); },
});
vm.runInContext(source.slice(start, end) + `
this.publishTreeApi = {
  publishEntities,
  publishPositiveVolume,
  publishTreeModel,
  publishNodeOpen,
  publishRevealSelected,
  publishTreeNodeHtml,
};`, context);
const api = context.publishTreeApi;

function reset(entries, group = "sets") {
  state.publishEntries = entries;
  state.settings.publishGroup = group;
  state.publishOpen.clear();
  state.publishClosed.clear();
  state.publishSel = null;
}

function modelFingerprint(nodes) {
  return nodes.map((node) => ({
    kind: node.kind,
    nodeId: node.nodeId,
    selectKey: node.selectKey,
    label: node.label,
    children: modelFingerprint(node.children || []),
  }));
}

function findNodes(nodes, predicate, out = []) {
  for (const node of nodes) {
    if (predicate(node)) out.push(node);
    findNodes(node.children || [], predicate, out);
  }
  return out;
}

test("explicit group IDs retain partial sets and duplicate titles stay separate", () => {
  const entries = [
    { slug: "duplicate-a", title: "Duplicate" },
    { slug: "duplicate-b", title: "Duplicate" },
    { slug: "work-10", title: "Work", group_id: "work", volume: "10" },
    { slug: "work-custom", title: "Work", group_id: "work", volume: "2e2" },
    { slug: "work-2", title: "Work", group_id: "work", volume: "2" },
    { slug: "partial-1", title: "Partial", group_id: "partial", volume: "1" },
  ];
  reset(entries);
  const entities = api.publishEntities();
  assert.deepEqual(
    Array.from(entities, (entity) => entity.key),
    ["book:duplicate-a", "book:duplicate-b", "set:partial", "set:work"],
  );
  assert.deepEqual(
    Array.from(entities.find((entity) => entity.key === "set:work").entries,
      (entry) => entry.slug),
    ["work-2", "work-10", "work-custom"],
  );
  assert.equal(entities.find((entity) => entity.key === "set:partial").entries.length, 1);
  assert.equal(api.publishPositiveVolume("2e2"), null);
});

test("rendered aliases share selection but keep independent expansion IDs", () => {
  reset([
    { slug: "work-1", title: "Work", group_id: "work", volume: "1", authors: "Alpha" },
    { slug: "work-2", title: "Work", group_id: "work", volume: "2", authors: "Beta" },
  ], "author");
  const model = api.publishTreeModel();
  const aliases = findNodes(model, (node) => node.selectKey === "set:work");
  assert.equal(aliases.length, 2);
  assert.equal(aliases[0].selectKey, aliases[1].selectKey);
  assert.notEqual(aliases[0].nodeId, aliases[1].nodeId);

  state.publishOpen.add(aliases[0].nodeId);
  assert.equal(api.publishNodeOpen(aliases[0], 1), true);
  assert.equal(api.publishNodeOpen(aliases[1], 1), false);
});

test("revealing a selected volume opens one complete category alias path", () => {
  reset([
    { slug: "work-1", title: "Work", group_id: "work", volume: "1",
      category_paths: [["Botany", "Herbals"], ["Medicine", "History"]] },
    { slug: "work-2", title: "Work", group_id: "work", volume: "2",
      category_paths: [["Botany", "Herbals"], ["Medicine", "History"]] },
  ], "category");
  const model = api.publishTreeModel();
  const setAliases = findNodes(model, (node) => node.selectKey === "set:work");
  assert.equal(setAliases.length, 2);

  const pathIds = Array.from(api.publishRevealSelected(model, "book:work-1"));
  assert.equal(pathIds.length, 4);
  for (const nodeId of pathIds.slice(0, -1)) assert.equal(state.publishOpen.has(nodeId), true);
  assert.equal(state.publishOpen.has(pathIds.at(-1)), false);
  assert.equal(setAliases.filter((node) => state.publishOpen.has(node.nodeId)).length, 1);
});

test("tree order is input-order invariant and unknown author is an internal sentinel", () => {
  const entries = [
    { slug: "blank", title: "Blank", authors: "" },
    { slug: "literal", title: "Literal", authors: "Unknown author" },
    { slug: "accent", title: "Álpha", authors: "Zulu" },
    { slug: "alpha", title: "alpha", authors: "Alpha" },
  ];
  reset(entries, "author");
  const forward = modelFingerprint(api.publishTreeModel());
  reset([...entries].reverse(), "author");
  const reverse = modelFingerprint(api.publishTreeModel());
  assert.deepEqual(reverse, forward);
  const unknownLabels = forward.filter((node) => node.label === "Unknown author");
  assert.equal(unknownLabels.length, 2);
  assert.notEqual(unknownLabels[0].nodeId, unknownLabels[1].nodeId);
});

test("organizational labels are buttons without ARIA treeitem/group markup", () => {
  reset([{ slug: "work", title: "Work", authors: "Author" }], "author");
  const html = api.publishTreeNodeHtml(api.publishTreeModel()[0], 0);
  assert.match(html, /<button class="publish-tree-label"[^>]+data-publish-toggle=/);
  assert.doesNotMatch(html, /role="(?:treeitem|group)"/);
});

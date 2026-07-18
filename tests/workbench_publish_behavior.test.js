const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(
  __dirname, "..", "tools", "whl_explorer", "static", "app.js"), "utf8");

function declaration(name) {
  const starts = [`async function ${name}(`, `function ${name}(`];
  const start = starts.map((marker) => source.indexOf(marker))
    .find((index) => index >= 0);
  assert.ok(start >= 0, `${name} declaration is present`);
  const end = /^}\r?$/m.exec(source.slice(start));
  assert.ok(end, `${name} declaration has a closing brace`);
  return source.slice(start, start + end.index + end[0].length);
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((ok, fail) => { resolve = ok; reject = fail; });
  return { promise, resolve, reject };
}

function elements() {
  return {
    "build-msg": { textContent: "" },
    "publish-msg": { textContent: "" },
    "b-rights": { selectedOptions: [{ textContent: "Public domain" }] },
    "b-ready": { classList: {
      active: false,
      toggle(_name, on) { this.active = !!on; },
      contains() { return this.active; },
    } },
    "b-verified-tag": { hidden: true },
  };
}

function uploadHarness(saveBuildFields, isDirty = () => true) {
  const els = elements();
  const calls = { confirms: 0, fetches: 0, polls: 0 };
  const state = {
    buildSel: "A",
    builds: {
      A: { id: "A", title: "Alpha", status: "ready", rights: "public-domain",
        pdf_file: "alpha.pdf" },
      B: { id: "B", title: "Beta", status: "ready", rights: "public-domain",
        pdf_file: "beta.pdf" },
    },
  };
  const context = vm.createContext({
    state,
    el: (id) => els[id],
    buildIsDirty: isDirty,
    saveBuildFields,
    confirmDialog: async () => { calls.confirms += 1; return true; },
    fetch: async () => {
      calls.fetches += 1;
      return { json: async () => ({ ok: true }) };
    },
    status: () => {}, statusErr: () => {}, statusCrit: () => {},
    pollPublish: () => { calls.polls += 1; },
  });
  vm.runInContext(`let _publishMsgBuildId = null;
${declaration("setPublishGuard")}
${declaration("syncPublishGuard")}
${declaration("uploadBuild")}
this.api = { uploadBuild, setPublishGuard, syncPublishGuard };`, context);
  return { api: context.api, calls, els, state };
}

test("a dirty publish never follows a selection switch to another book", async () => {
  const wait = deferred();
  const { api, calls, state } = uploadHarness(() => wait.promise);
  const publishing = api.uploadBuild();
  await Promise.resolve();
  state.buildSel = "B";
  wait.resolve(true);
  await publishing;
  assert.equal(calls.confirms, 0);
  assert.equal(calls.fetches, 0);
  assert.equal(calls.polls, 0);
});

test("a rejected dirty save is contained and reported beside Publish", async () => {
  const { api, calls, els } = uploadHarness(async () => {
    throw new Error("offline");
  });
  await assert.doesNotReject(api.uploadBuild());
  assert.equal(els["build-msg"].textContent, "Save failed");
  assert.equal(els["publish-msg"].textContent, "Save failed");
  assert.equal(calls.confirms, 0);
  assert.equal(calls.fetches, 0);
});

test("an edit made during the pre-publish save blocks the stale publish", async () => {
  const wait = deferred();
  let dirty = true;
  const { api, calls, els } = uploadHarness(() => wait.promise, () => dirty);
  const publishing = api.uploadBuild();
  await Promise.resolve();
  // The original save resolves, but a newer same-book edit remains dirty.
  dirty = true;
  wait.resolve(true);
  await publishing;
  assert.equal(els["publish-msg"].textContent, "Newer edits are not saved yet");
  assert.equal(calls.confirms, 0);
  assert.equal(calls.fetches, 0);
});

test("a completed current save can publish the captured book id", async () => {
  let dirty = true;
  const { api, calls } = uploadHarness(async () => {
    dirty = false;
    return true;
  }, () => dirty);
  await api.uploadBuild();
  assert.equal(calls.confirms, 1);
  assert.equal(calls.fetches, 1);
  assert.equal(calls.polls, 1);
});

test("publish guard text belongs only to the selected book", () => {
  const { api, els, state } = uploadHarness(async () => true);
  api.setPublishGuard("A", "Attach the PDF before publishing");
  assert.equal(els["publish-msg"].textContent, "Attach the PDF before publishing");
  state.buildSel = "B";
  api.syncPublishGuard("B");
  assert.equal(els["publish-msg"].textContent, "");
});

function patchHarness(fetchImpl, dirty = true) {
  const calls = { upload: 0, list: 0, workbench: 0 };
  const state = { buildSel: "B", builds: { A: { id: "A" }, B: { id: "B" } } };
  const context = vm.createContext({
    state,
    fetch: fetchImpl,
    buildIsDirty: () => dirty,
    renderUpload: () => { calls.upload += 1; },
    renderBuildsList: () => { calls.list += 1; },
    renderWorkbench: () => { calls.workbench += 1; },
    renderRemarks: () => {}, renderHome: () => {},
    encodeURIComponent,
  });
  vm.runInContext(`let buildPatchConflict = false;
${declaration("patchBuildRaw")}
this.api = { patchBuildRaw };`, context);
  return { api: context.api, calls, state };
}

test("network rejection returns false and a late A patch cannot repaint dirty B", async () => {
  const offline = patchHarness(async () => { throw new Error("offline"); });
  assert.equal(await offline.api.patchBuildRaw("A", { title: "A" }), false);

  const late = patchHarness(async () => ({
    ok: true,
    status: 200,
    json: async () => ({ ok: true, build: { id: "A", title: "saved" } }),
  }));
  assert.equal(await late.api.patchBuildRaw("A", { title: "saved" }), true);
  assert.equal(late.calls.upload, 0);
  assert.equal(late.calls.list, 1);
  assert.equal(late.calls.workbench, 1);
});

test("an A save resolving after selecting B leaves B dirty and untouched", async () => {
  const wait = deferred();
  const els = elements();
  els["b-title"] = { value: "Alpha" };
  els["build-msg"].textContent = "A editing";
  const state = {
    buildSel: "A",
    builds: {
      A: { id: "A", title: "Alpha", status: "draft", updated_at: "one" },
      B: { id: "B", title: "Beta", status: "draft", updated_at: "two" },
    },
  };
  const context = vm.createContext({
    state,
    BUILD_FIELDS: ["title"],
    el: (id) => els[id],
    catPickers: { "b-categories": { get: () => [] } },
    buildDescMd: { get: () => "description" },
    buildGroupIdFor: () => "",
    currentBuild: () => state.builds[state.buildSel],
    patchBuild: () => wait.promise,
    status: () => {},
    renderBuildEditor: () => {},
  });
  vm.runInContext(`let buildPatchConflict = false;
let buildDirty = true;
let buildEditGeneration = 4;
const descState = { id: "A", val: "old" };
${declaration("saveBuildFields")}
this.api = { saveBuildFields, dirty: () => buildDirty };`, context);

  const saving = context.api.saveBuildFields();
  state.buildSel = "B";
  els["build-msg"].textContent = "B has unsaved edits";
  wait.resolve(true);
  assert.equal(await saving, true);
  assert.equal(context.api.dirty(), true);
  assert.equal(els["build-msg"].textContent, "B has unsaved edits");
});

test("verification completion for A cannot repaint selected B", async () => {
  const wait = deferred();
  const els = elements();
  const state = {
    buildSel: "A",
    builds: { A: { id: "A", status: "draft" }, B: { id: "B", status: "draft" } },
  };
  const calls = { status: 0, list: 0, workbench: 0 };
  const context = vm.createContext({
    state,
    currentBuild: () => state.builds[state.buildSel],
    el: (id) => els[id],
    saveBuildFields: () => wait.promise,
    status: () => { calls.status += 1; },
    renderBuildsList: () => { calls.list += 1; },
    renderWorkbench: () => { calls.workbench += 1; },
  });
  vm.runInContext(`${declaration("setVerified")}
this.api = { setVerified };`, context);
  const saving = context.api.setVerified(true);
  state.buildSel = "B";
  wait.resolve(true);
  assert.equal(await saving, true);
  assert.deepEqual(calls, { status: 0, list: 0, workbench: 0 });
});

test("publish completion preserves a different book's dirty editor", async () => {
  const state = { buildSel: "B", anSel: "B", builds: {} };
  const ocrState = { book: "B" };
  const calls = { upload: 0, list: 0, workbench: 0, home: 0 };
  let callback;
  const context = vm.createContext({
    state,
    ocrState,
    clearInterval: () => {},
    setInterval: (fn) => { callback = fn; return 1; },
    fetch: async () => ({ json: async () => ({
      running: false, stage: "done", build: "A", slug: "alpha",
    }) }),
    loadBuilds: async () => {},
    buildIsDirty: () => true,
    renderUpload: () => { calls.upload += 1; },
    renderBuildsList: () => { calls.list += 1; },
    renderWorkbench: () => { calls.workbench += 1; },
    renderHome: () => { calls.home += 1; },
    status: () => {}, statusCrit: () => {},
  });
  vm.runInContext(`let _publishTimer = null;
${declaration("pollPublish")}
this.api = { pollPublish };`, context);
  context.api.pollPublish("A");
  await callback();
  assert.equal(state.buildSel, "B");
  assert.equal(state.anSel, "B");
  assert.equal(ocrState.book, "B");
  assert.deepEqual(calls, { upload: 0, list: 1, workbench: 1, home: 1 });
});

test("clean publish completion clears only its own selection aliases", async () => {
  const state = { buildSel: "A", anSel: "A", builds: {} };
  const ocrState = { book: "A" };
  let callback;
  let renders = 0;
  const context = vm.createContext({
    state,
    ocrState,
    clearInterval: () => {},
    setInterval: (fn) => { callback = fn; return 1; },
    fetch: async () => ({ json: async () => ({
      running: false, stage: "done", build: "A", slug: "alpha",
    }) }),
    loadBuilds: async () => {},
    buildIsDirty: () => false,
    renderUpload: () => { renders += 1; },
    renderBuildsList: () => {}, renderWorkbench: () => {}, renderHome: () => {},
    status: () => {}, statusCrit: () => {},
  });
  vm.runInContext(`let _publishTimer = null;
${declaration("pollPublish")}
this.api = { pollPublish };`, context);
  context.api.pollPublish("A");
  await callback();
  assert.equal(state.buildSel, null);
  assert.equal(state.anSel, null);
  assert.equal(ocrState.book, null);
  assert.equal(renders, 1);
});

test("Workbench categories and suggested rights advance edit generations", () => {
  assert.match(source, /makeCatPicker\("b-categories", markBuildDirty\)/);
  assert.match(source, /el\("b-rights"\)\.value = "public-domain";\s*markBuildDirty\(\)/);
});

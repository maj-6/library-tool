const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const {
  applySharedAppearance,
  installSharedAppearance,
  normalizeSharedAppearance,
  SHARED_SETTINGS_KEY,
} = require("../tools/whl_explorer/static/corrections/ui-profile");


class FakeStyle {
  constructor() {
    this.values = new Map();
    this.zoom = "";
  }
  setProperty(name, value) { this.values.set(name, String(value)); }
  removeProperty(name) { this.values.delete(name); }
}


function fakeDocument() {
  const classes = new Set();
  return {
    body: {
      classList: {
        toggle(name, active) {
          if (active) classes.add(name);
          else classes.delete(name);
        },
      },
      dataset: {},
      style: new FakeStyle(),
    },
    documentElement: { style: new FakeStyle() },
    classes,
  };
}


test("Corrections appearance follows built-in, legacy, and custom Library Tool themes", () => {
  assert.deepEqual(normalizeSharedAppearance({ theme: "slate", uiScale: 1.3 }), {
    theme: "slate",
    overrides: {},
    uiScale: 1.3,
  });
  assert.equal(normalizeSharedAppearance({ theme: "midnight" }).theme, "linen");
  assert.equal(normalizeSharedAppearance({ theme: "unknown" }).theme, "sage");

  const custom = normalizeSharedAppearance({
    theme: "custom-1",
    uiScale: 9,
    savedThemes: [{
      id: "custom-1",
      base: "vellum",
      overrides: {
        "--ui": "Tahoma, sans-serif",
        "--cyan": "#123456",
        color: "red",
        "--empty": " ",
      },
    }],
  });
  assert.equal(custom.theme, "vellum");
  assert.equal(custom.uiScale, 1);
  assert.deepEqual(custom.overrides, {
    "--ui": "Tahoma, sans-serif",
    "--cyan": "#123456",
  });
});


test("applying shared appearance replaces stale overrides and scales the whole window", () => {
  const documentRef = fakeDocument();
  applySharedAppearance(documentRef, {
    theme: "slate",
    uiScale: 1.4,
    themeOverrides: { slate: { "--ui": "Verdana", "--radius": "2px" } },
  });
  assert.equal(documentRef.body.dataset.theme, "slate");
  assert.equal(documentRef.body.style.values.get("--ui"), "Verdana");
  assert.equal(documentRef.documentElement.style.zoom, "1.4");
  assert.equal(documentRef.documentElement.style.values.get("--ui-scale"), "1.4");

  applySharedAppearance(documentRef, { theme: "porcelain", uiScale: 0.9 });
  assert.equal(documentRef.body.dataset.theme, "porcelain");
  assert.equal(documentRef.body.style.values.has("--ui"), false);
  assert.equal(documentRef.body.style.values.has("--radius"), false);
  assert.equal(documentRef.documentElement.style.zoom, "0.9");
});


test("appearance bridge uses the local cache immediately and ignores a stale server reply", async () => {
  const documentRef = fakeDocument();
  const listeners = new Map();
  let cached = JSON.stringify({ theme: "sage", uiScale: 1.1 });
  let resolveFetch;
  const windowRef = {
    document: documentRef,
    localStorage: { getItem: (key) => key === SHARED_SETTINGS_KEY ? cached : null },
    whlDesktop: { isDesktop: true },
    addEventListener: (name, listener) => listeners.set(name, listener),
    removeEventListener: (name, listener) => {
      if (listeners.get(name) === listener) listeners.delete(name);
    },
  };
  const dispose = installSharedAppearance({
    windowRef,
    documentRef,
    fetchImpl: () => new Promise((resolve) => { resolveFetch = resolve; }),
  });
  assert.equal(documentRef.body.dataset.theme, "sage");
  assert.equal(documentRef.documentElement.style.zoom, "1.1");
  assert.equal(documentRef.classes.has("desktop"), true);

  cached = JSON.stringify({ theme: "porcelain", uiScale: 0.9 });
  listeners.get("storage")({ key: SHARED_SETTINGS_KEY, newValue: cached });
  assert.equal(documentRef.body.dataset.theme, "porcelain");

  resolveFetch({
    ok: true,
    json: async () => ({ settings: { theme: "slate", uiScale: 1.5 } }),
  });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(documentRef.body.dataset.theme, "porcelain");
  assert.equal(documentRef.documentElement.style.zoom, "0.9");

  dispose();
  assert.equal(listeners.has("storage"), false);
});


test("Corrections document consumes shared chrome without loading the main application runtime", () => {
  const template = fs.readFileSync(path.join(
    __dirname, "..", "tools", "whl_explorer", "templates", "corrections.html"), "utf8");
  assert.match(template, /filename='style\.css'\) }}\?v={{ corrections_main_css_v }}/);
  assert.match(template, /filename='corrections\/corrections\.css'\) }}\?v={{ corrections_css_v }}/);
  assert.match(template, /<body data-theme="sage">/);
  assert.match(template, /id="titlebar"/);
  assert.match(template, /id="statusbar"/);
  assert.doesNotMatch(template, /filename='app\.js'/);
});

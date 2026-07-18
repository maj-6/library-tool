const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");


const appPath = path.join(
  __dirname, "..", "tools", "whl_explorer", "static", "app.js");
const source = fs.readFileSync(appPath, "utf8");
const desktopMain = fs.readFileSync(path.join(__dirname, "..", "desktop", "main.js"), "utf8");
const startupHtml = fs.readFileSync(path.join(__dirname, "..", "desktop", "startup.html"), "utf8");
const updaterHtml = fs.readFileSync(path.join(__dirname, "..", "desktop", "updater.html"), "utf8");

function block(startMarker, endMarker) {
  const start = source.indexOf(startMarker);
  const end = source.indexOf(endMarker, start);
  assert.ok(start >= 0 && end > start, `${startMarker} block is present`);
  return source.slice(start, end);
}

function declaration(name) {
  const markers = [`async function ${name}(`, `function ${name}(`];
  let start = -1;
  for (const marker of markers) {
    start = source.indexOf(marker);
    if (start >= 0) break;
  }
  assert.ok(start >= 0, `${name} declaration is present`);
  const open = source.indexOf("{", start);
  let depth = 0;
  for (let i = open; i < source.length; i += 1) {
    if (source[i] === "{") depth += 1;
    else if (source[i] === "}") {
      depth -= 1;
      if (depth === 0) return source.slice(start, i + 1);
    }
  }
  assert.fail(`${name} declaration has a closing brace`);
}

function plain(value) {
  return JSON.parse(JSON.stringify(value));
}

test("fixed popups convert visual cursor coordinates through root zoom", () => {
  const node = {
    style: {},
    getBoundingClientRect: () => ({ width: 180, height: 120 }),
  };
  const context = vm.createContext({
    state: { settings: { uiScale: 1.5 } },
    innerWidth: 800,
    innerHeight: 600,
  });
  vm.runInContext([
    declaration("fixedPopupMetrics"),
    declaration("positionFixedPopup"),
    "this.api = { fixedPopupMetrics, positionFixedPopup };",
  ].join("\n"), context);

  context.api.positionFixedPopup(node, 450, 300);
  assert.equal(parseFloat(node.style.left), 300);
  assert.equal(parseFloat(node.style.top), 200);

  context.api.positionFixedPopup(node, 790, 590);
  const visualLeft = parseFloat(node.style.left) * 1.5;
  const visualTop = parseFloat(node.style.top) * 1.5;
  assert.ok(visualLeft + 180 <= 800 - 12);
  assert.ok(visualTop + 120 <= 600 - 12);
});

test("settings partition keeps embedding and image-generation keys out of storage", () => {
  const context = vm.createContext({});
  vm.runInContext([
    block("const VIEW_STATE_KEYS", "function partitionSettings"),
    declaration("partitionSettings"),
    "this.api = { partitionSettings };",
  ].join("\n"), context);

  const result = plain(context.api.partitionSettings({
    theme: "sage",
    topTable: "whl",
    embedKey: "embedding-secret",
    imgGenKey: "image-secret",
  }));
  assert.deepEqual(result.prefs, { theme: "sage" });
  assert.deepEqual(result.view, { topTable: "whl" });
});

test("normalization migrates legacy settings and removes unsupported OCR choices", () => {
  const settings = {
    theme: "sage",
    checkedCols: { title: false },
    colVis: {},
    colWidths: {},
    themeOverrides: {},
    savedThemes: [],
    font: '"Consolas", monospace',
    fontUi: '"Segoe UI", sans-serif',
    fontMono2: '"Courier New", monospace',
    maxRows: 250,
    setsBackfilled: true,
    textAnalysisService: "configured",
    ocrAzureEndpoint: "https://azure.invalid",
    ocrAzureKey: "secret",
    ocrService: "openai",
    ocrKeyMap: { 1: "tesseract", 5: "openai" },
    workbenchPhase: "record",
  };
  const context = vm.createContext({ state: { settings } });
  vm.runInContext([
    'const WB_PHASES = ["record"];',
    'const DEFAULT_THEME = "sage";',
    "const UI_SCALE_MIN = 0.7, UI_SCALE_MAX = 2.0;",
    "function themeOverrideMap(id, create) {",
    '  const key = id || "sage";',
    "  const all = state.settings.themeOverrides;",
    "  return all[key] || (create ? (all[key] = {}) : {});",
    "}",
    declaration("normalizeSettings"),
    "this.api = { normalizeSettings };",
  ].join("\n"), context);

  assert.equal(context.api.normalizeSettings(), true);
  const got = plain(settings);
  assert.deepEqual(got.colVis.checked, { title: false });
  for (const key of ["checkedCols", "font", "fontUi", "fontMono2", "maxRows",
    "setsBackfilled", "textAnalysisService", "ocrAzureEndpoint", "ocrAzureKey"])
    assert.equal(Object.hasOwn(got, key), false, `${key} was removed`);
  assert.equal(got.themeOverrides.sage["--ui"], '"Segoe UI", sans-serif');
  assert.equal(got.themeOverrides.sage["--mono"], '"Consolas", monospace');
  assert.equal(got.themeOverrides.sage["--mono2"], '"Courier New", monospace');
  assert.equal(got.ocrService, "tesseract");
  assert.deepEqual(got.ocrKeyMap, { 1: "tesseract" });
});

test("reset replaces server preferences before clearing cache and reloading", async () => {
  const calls = [];
  const removed = [];
  let reloaded = false;
  const context = vm.createContext({
    DEFAULT_SETTINGS: {
      theme: "",
      topTable: "checked",
      embedKey: "",
      imgGenKey: "",
    },
    SETTINGS_KEY: "settings",
    VIEWSTATE_KEY: "view",
    localStorage: { removeItem: (key) => removed.push(key) },
    location: { reload: () => { reloaded = true; } },
    fetch: async (url, init) => {
      calls.push([url, init]);
      return { ok: true, status: 200 };
    },
  });
  vm.runInContext([
    block("const VIEW_STATE_KEYS", "function partitionSettings"),
    declaration("partitionSettings"),
    declaration("resetSettingsToDefaults"),
    "this.api = { resetSettingsToDefaults };",
  ].join("\n"), context);

  await context.api.resetSettingsToDefaults();
  assert.equal(calls.length, 1);
  assert.equal(calls[0][0], "/api/client_state");
  assert.equal(calls[0][1].method, "PUT");
  assert.deepEqual(JSON.parse(calls[0][1].body), { settings: { theme: "" } });
  assert.deepEqual(removed, ["settings", "view"]);
  assert.equal(reloaded, true);
});

test("both cursor and anchored shared menus use zoom-aware popup geometry", () => {
  const cursorMenu = block("function openProcMenu", "// --- general row context menu");
  const anchoredMenu = declaration("openPopup");
  assert.match(cursorMenu, /positionFixedPopup\(pop, x, y\)/);
  assert.match(anchoredMenu, /fixedPopupMetrics\(pop\)/);
  assert.match(anchoredMenu, /positionFixedPopup\(pop,/);
});

test("desktop pre-launch windows recognize every current theme and custom bases", () => {
  const themesStart = desktopMain.indexOf("const KNOWN_THEMES");
  const functionStart = desktopMain.indexOf("function prelaunchTheme(");
  const functionEnd = desktopMain.indexOf("function readActiveTheme(", functionStart);
  assert.ok(themesStart >= 0 && functionStart > themesStart && functionEnd > functionStart);
  const context = vm.createContext({});
  vm.runInContext([
    desktopMain.slice(themesStart, functionEnd),
    "this.prelaunchTheme = prelaunchTheme;",
  ].join("\n"), context);

  assert.equal(context.prelaunchTheme({theme:"porcelain"}), "porcelain");
  assert.equal(context.prelaunchTheme({theme:"slate"}), "slate");
  assert.equal(context.prelaunchTheme({
    theme:"custom-1",
    savedThemes:[{id:"custom-1", base:"slate"}],
  }), "slate");
  assert.equal(context.prelaunchTheme({theme:"retired"}), "sage");
  for (const html of [startupHtml, updaterHtml]) {
    assert.match(html, /data-theme="porcelain"/);
    assert.match(html, /data-theme="slate"/);
  }
});

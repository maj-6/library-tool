// The bundled-font list and the font-var sanitizer, exercised as real code.
//
// The pickers in Settings offer bundled faces (which ship with the app and so
// always render) before system ones (which render only if installed). These
// tests evaluate the generated BUNDLED_FONTS block and sanitizeOverrides()
// out of app.js, so a regression in either shows up as a behaviour failure
// rather than a string that happens to still be present.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const root = path.join(__dirname, "..");
const source = fs.readFileSync(
  path.join(root, "tools", "whl_explorer", "static", "app.js"), "utf8");
const manifest = JSON.parse(fs.readFileSync(
  path.join(root, "tools", "whl_explorer", "static", "fonts", "fonts.json"),
  "utf8"));

// The working tree is CRLF (core.autocrlf=true), so markers must never span a
// line break.
function region(startMarker, endMarker) {
  const start = source.indexOf(startMarker);
  assert.ok(start >= 0, `${startMarker} is present`);
  const end = source.indexOf(endMarker, start + startMarker.length);
  assert.ok(end > start, `${endMarker} follows it`);
  return source.slice(start, end);
}

// Values built inside a vm realm have foreign prototypes, so deepEqual on them
// fails as "same structure but not reference-equal". Strip them back to plain.
function plain(value) {
  return JSON.parse(JSON.stringify(value));
}

function declaration(name) {
  const start = source.indexOf(`function ${name}(`);
  assert.ok(start >= 0, `${name} is declared`);
  const open = source.indexOf("{", start);
  let depth = 0;
  for (let i = open; i < source.length; i += 1) {
    if (source[i] === "{") depth += 1;
    else if (source[i] === "}") {
      depth -= 1;
      if (depth === 0) return source.slice(start, i + 1);
    }
  }
  assert.fail(`${name} has a closing brace`);
}

// Both generated constants, evaluated out of the marked block.
function fontBlock() {
  const ctx = {};
  vm.createContext(ctx);
  vm.runInContext(
    region("const BUNDLED_FONTS = [", "// --- END BUNDLED FONTS")
      + "\n;globalThis.out = { BUNDLED_FONTS, TYPESET_FONTS };", ctx);
  return ctx.out;
}

function bundled() {
  return fontBlock().BUNDLED_FONTS;
}

function sanitizer() {
  const ctx = {
    THEME_TOKEN_VARS: new Set(["--ui", "--mono", "--mono2", "--cyan"]),
    THEME_FONT_VARS: new Set(["--ui", "--mono", "--mono2"]),
  };
  vm.createContext(ctx);
  vm.runInContext(
    declaration("sanitizeOverrides")
      + "\n;globalThis.sanitizeOverrides = sanitizeOverrides;", ctx);
  return ctx.sanitizeOverrides;
}

test("the generated list mirrors the manifest", () => {
  const fonts = bundled();
  assert.ok(Array.isArray(fonts) && fonts.length > 0);
  assert.deepEqual(
    plain(fonts).map((f) => f.id), manifest.fonts.map((f) => f.id));
  for (const f of fonts) {
    for (const key of ["id", "family", "kind", "stack"]) {
      assert.equal(typeof f[key], "string", `${f.id} has a ${key}`);
      assert.ok(f[key].length, `${f.id}.${key} is not empty`);
    }
    assert.ok(f.stack.includes(f.family), `${f.id} stack names its family`);
  }
});

test("every bundled stack survives a theme import", () => {
  const sanitize = sanitizer();
  for (const f of bundled()) {
    const out = sanitize({ "--ui": f.stack });
    assert.equal(out["--ui"], f.stack, `${f.id} is importable`);
  }
});

test("every system font choice also survives a theme import", () => {
  const sanitize = sanitizer();
  const list = region("const FONT_CHOICES = [", "];");
  const stacks = [...list.matchAll(/\[\s*'([^']*)'/g)].map((m) => m[1]);
  assert.ok(stacks.length > 5, "the system list is still populated");
  for (const stack of stacks) {
    assert.equal(sanitize({ "--mono": stack })["--mono"], stack);
  }
});

test("a hostile font value is dropped rather than applied inline", () => {
  const sanitize = sanitizer();
  for (const evil of [
    '"X", url(http://evil/beacon)',       // caught by the paren rule
    '"X"; background: red',                // caught by the semicolon rule
    "var(--ui)",
    "expression(alert(1))",
    "x".repeat(300),
    "@import url(http://evil)",
  ]) {
    assert.equal("--ui" in sanitize({ "--ui": evil }), false,
      `rejected: ${evil.slice(0, 40)}`);
  }
});

test("the font grammar does not constrain non-font tokens", () => {
  // --cyan is a colour token: the tightened grammar must not touch it
  const sanitize = sanitizer();
  assert.equal(sanitize({ "--cyan": "#00ffff" })["--cyan"], "#00ffff");
});

test("an unknown token is still refused", () => {
  const sanitize = sanitizer();
  assert.deepEqual(plain(sanitize({ "--not-a-token": "Arial" })), {});
});

// Run the replica suggestion builder against whatever font lists we hand it,
// so the reserved section can be exercised even while it is empty on disk.
function replicaSuggestions(lists) {
  const ctx = { ...lists };
  vm.createContext(ctx);
  vm.runInContext(
    region("const RW_FONT_SUGGESTIONS_SYSTEM = [", "function rwSetMode")
    + "\n;globalThis.out = rwFontSuggestions();", ctx);
  return plain(ctx.out);
}

test("the replica suggestions lead with bundled families and do not repeat", () => {
  const { BUNDLED_FONTS, TYPESET_FONTS } = fontBlock();
  const list = replicaSuggestions({ BUNDLED_FONTS, TYPESET_FONTS });
  const families = plain([...TYPESET_FONTS, ...BUNDLED_FONTS]).map((f) => f.family);
  assert.deepEqual(list.slice(0, families.length), families);
  assert.equal(new Set(list).size, list.length, "no duplicate suggestion");
  assert.ok(list.includes("EB Garamond"), "historical faces are kept");
});

test("a typesetting face leads the replica list, ahead of chrome faces", () => {
  const list = replicaSuggestions({
    TYPESET_FONTS: [{ id: "fell", family: "IM Fell English", kind: "serif",
      stack: '"IM Fell English", serif' }],
    BUNDLED_FONTS: [{ id: "rs", family: "Roboto Slab", kind: "serif",
      stack: '"Roboto Slab", serif' }],
  });
  assert.deepEqual(list.slice(0, 2), ["IM Fell English", "Roboto Slab"]);
  // it was already a historical suggestion; bundling must not duplicate it
  assert.equal(list.filter((n) => n === "IM Fell English").length, 1);
});

// Build a Settings font picker by running the real option-building code
// against a DOM stub, so the ORDER the user sees is what gets asserted --
// not the order the constants happen to be declared in.
function buildPicker() {
  const make = (tag) => ({
    tag, children: [], value: "", textContent: "", label: "", className: "",
    appendChild(c) { this.children.push(c); return c; },
  });
  const ctx = {
    document: { createElement: make },
    ...fontBlock(),
  };
  vm.createContext(ctx);
  vm.runInContext(region("const FONT_CHOICES = [", "];") + "];", ctx);
  vm.runInContext(
    region('const sel = document.createElement("select");', "const stored")
    + "\n;globalThis.sel = sel;", ctx);
  return ctx.sel;
}

test("the picker offers bundled faces above system ones", () => {
  const sel = buildPicker();
  const groups = sel.children.filter((c) => c.tag === "optgroup");
  assert.deepEqual(groups.map((g) => g.label), ["Bundled", "System fonts"],
    "bundled always render, system fonts only if installed");
  assert.equal(sel.children[0].textContent, "Theme default");
  assert.deepEqual(
    groups[0].children.map((o) => o.textContent),
    plain(fontBlock().BUNDLED_FONTS).map((f) => f.family));
  assert.ok(groups[1].children.length > 5, "the system list is still offered");
  // the "Default" sentinel of FONT_CHOICES must not leak in as a blank option
  assert.ok(groups[1].children.every((o) => o.value));
});

test("no typesetting face appears anywhere in a settings picker", () => {
  const sel = buildPicker();
  const offered = sel.children.flatMap(
    (c) => (c.tag === "optgroup" ? c.children : [c])).map((o) => o.textContent);
  for (const f of plain(fontBlock().TYPESET_FONTS)) {
    assert.ok(!offered.includes(f.family),
      `${f.family} is reserved for the Replica engine`);
  }
});

test("the settings picker cannot reach the typesetting section", () => {
  // structural, not a filter: the picker builder never names TYPESET_FONTS
  const picker = region('tok.t === "font"', "} else {");
  assert.ok(picker.includes("BUNDLED_FONTS"), "it offers the chrome faces");
  assert.ok(!picker.includes("TYPESET_FONTS"),
    "a typesetting face must never be offered as an interface font");
});

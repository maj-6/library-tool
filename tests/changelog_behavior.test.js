const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");


const root = path.join(__dirname, "..");
const websiteData = fs.readFileSync(
  path.join(root, "website", "assets", "data.js"), "utf8");
const websiteReleases = fs.readFileSync(
  path.join(root, "website", "assets", "releases.js"), "utf8");
const desktopApp = fs.readFileSync(
  path.join(root, "tools", "whl_explorer", "static", "app.js"), "utf8");

function block(source, startMarker, endMarker) {
  const start = source.indexOf(startMarker);
  const end = source.indexOf(endMarker, start);
  assert.ok(start >= 0 && end > start, `${startMarker} block is present`);
  return source.slice(start, end);
}

function websiteApi() {
  const context = vm.createContext({});
  const parser = block(
    websiteData,
    "const CHANGELOG_CATEGORIES",
    "/** Group a newest-first version list",
  ).replace("export function parseChangelog", "function parseChangelog");
  const renderer = block(websiteReleases, "const esc =", "const box =");
  vm.runInContext(`${parser}\n${renderer}\nthis.api = { parseChangelog, release };`, context);
  return context.api;
}

function desktopApi() {
  const context = vm.createContext({});
  const esc = block(desktopApp, "const esc =", "// The footer carries");
  const changelog = block(
    desktopApp,
    "const CHANGELOG_CATEGORIES",
    "// --- FIND syntax",
  );
  vm.runInContext(
    `${esc}\n${changelog}\nthis.api = { parseChangelog, changelogHTML };`,
    context,
  );
  return context.api;
}

const categorized = [
  "# Release notes",
  "",
  "## 1.2.0 — 2026-07-15",
  "### Additions",
  "- Added <unsafe> previews.",
  "### Other Changes",
  "- Changed navigation.",
  "### Bugfixes",
  "- Fixed saved settings.",
].join("\n");

const legacy = [
  "## 1.1.0 - 2026-07-14",
  "- First legacy note.",
  "<!--more-->",
  "- Second legacy note.",
].join("\n");

const plain = (value) => JSON.parse(JSON.stringify(value));

test("website and desktop parsers preserve categories and legacy notes", () => {
  const web = websiteApi();
  const desktop = desktopApi();

  const expectedCategories = [
    { name: "Additions", items: ["Added <unsafe> previews."] },
    { name: "Other Changes", items: ["Changed navigation."] },
    { name: "Bugfixes", items: ["Fixed saved settings."] },
  ];
  for (const api of [web, desktop]) {
    const parsed = plain(api.parseChangelog(categorized));
    assert.equal(parsed.length, 1);
    assert.equal(parsed[0].version, "1.2.0");
    assert.equal(parsed[0].date, "2026-07-15");
    assert.deepEqual(parsed[0].categories, expectedCategories);
    assert.deepEqual(parsed[0].items, expectedCategories.flatMap((c) => c.items));

    const old = plain(api.parseChangelog(legacy));
    assert.deepEqual(old[0].categories, [{
      name: "Other Changes",
      items: ["First legacy note.", "Second legacy note."],
    }]);
  }
});

test("website and desktop renderers show ordered, escaped category sections", () => {
  const web = websiteApi();
  const desktop = desktopApi();
  const version = web.parseChangelog(categorized)[0];
  const outputs = [web.release(version), desktop.changelogHTML([version])];

  for (const html of outputs) {
    assert.ok(html.includes('class="cl-category-name"'));
    assert.ok(html.includes("&lt;unsafe&gt;"));
    assert.ok(!html.includes("<unsafe>"));
    assert.ok(html.indexOf("Additions") < html.indexOf("Other Changes"));
    assert.ok(html.indexOf("Other Changes") < html.indexOf("Bugfixes"));
    assert.ok(!html.includes("<details"));
  }
});

test("website release notes render as a flat version list", () => {
  assert.ok(websiteReleases.includes('versions.map(release).join("")'));
  assert.ok(!websiteReleases.includes("groupByMajor"));
  assert.ok(!websiteReleases.includes("cl-major"));
});

test("website release notes keep independent desktop and Android feeds", () => {
  const releasesPage = fs.readFileSync(
    path.join(root, "website", "releases.html"), "utf8");

  assert.ok(websiteData.includes('desktop: "changelog.md"'));
  assert.ok(websiteData.includes('android: "android-changelog.md"'));
  assert.ok(websiteData.includes('fetchChangelog(platform = "desktop")'));
  assert.ok(releasesPage.includes('data-platform="desktop"'));
  assert.ok(releasesPage.includes('data-platform="android"'));
  assert.ok(websiteReleases.includes("fetchChangelog(selected)"));
});

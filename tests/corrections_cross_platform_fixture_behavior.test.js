const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const {
  createArtifactsFeature,
} = require("../tools/whl_explorer/static/corrections/artifacts");
const {
  FakeNode,
  fakeDocument,
} = require("./fixtures/corrections_fake_dom");


const fixturePath = path.join(
  __dirname,
  "..",
  "android",
  "BookCapture",
  "app",
  "src",
  "test",
  "resources",
  "corrections_release_fixture.json",
);
const fixture = JSON.parse(fs.readFileSync(fixturePath, "utf8"));


function nextTurn() {
  return new Promise((resolve) => setImmediate(resolve));
}


async function pressTreeKey(treeRoot, key) {
  const event = treeRoot.emit("keydown", { key, target: treeRoot });
  await nextTurn();
  await nextTurn();
  return event;
}


async function moveKeyboardTo(feature, treeRoot, key) {
  const rows = feature.navigableRows();
  const index = rows.findIndex((row) => row.key === key);
  assert.ok(index >= 0, `${key} must be keyboard reachable`);
  await pressTreeKey(treeRoot, "Home");
  for (let offset = 0; offset < index; offset += 1) {
    await pressTreeKey(treeRoot, "ArrowDown");
  }
  assert.equal(feature.activeKey, key);
}


test("shared release fixture is keyboard navigable into accessible Properties state",
  async () => {
    const documentRef = fakeDocument();
    const treeRoot = new FakeNode("div", documentRef);
    const propertiesRoot = new FakeNode("dl", documentRef);
    const corrections = fixture.corrections;
    const display = corrections.display_artifact;
    const marginalia = corrections.marginalia_region;
    const illustration = corrections.illustration_region;
    const expected = corrections.expected;
    assert.deepEqual(Object.keys(expected).sort(), [
      "caption",
      "illustration_role",
      "image_category",
      "marginalia_role",
    ]);
    const byKey = new Map([
      [`artifact:${display.key.artifact_id}`, display],
      [`annotation:${marginalia.key.annotation_id}`, marginalia],
      [`annotation:${illustration.key.annotation_id}`, illustration],
    ]);
    const feature = createArtifactsFeature({
      treeRoot,
      propertiesRoot,
      documentRef,
      initialExpandedGroups: ["layout-regions", "source-images"],
      catalog: {
        async list({ group }) {
          const items = group === "source-images" ? [display]
            : group === "layout-regions" ? [marginalia, illustration] : [];
          return { revision: `${group}-fixture-r1`, items, next_cursor: null };
        },
        async get({ key }) {
          return byKey.get(key);
        },
      },
      resources: {
        async resolveRaster() {
          return { url: "/fixture/capture-display.jpg" };
        },
        async listRegions() {
          return { items: [marginalia, illustration], next_cursor: null };
        },
      },
      commands: {
        async setManualCaption() { throw new Error("not invoked"); },
        async clearManualCaption() { throw new Error("not invoked"); },
        async executeInverse() { throw new Error("not invoked"); },
      },
    }).mount();

    await feature.setContext({ item_id: fixture.book_id });
    treeRoot.focus();
    assert.equal(documentRef.activeElement, treeRoot);
    assert.equal(treeRoot.getAttribute("role"), "tree");
    assert.equal(treeRoot.getAttribute("tabindex"), "0");
    assert.equal(treeRoot.getAttribute("aria-label"),
      "Artifacts for selected book");

    await moveKeyboardTo(feature, treeRoot, "group:source-images");
    const enterImage = await pressTreeKey(treeRoot, "ArrowRight");
    assert.equal(enterImage.defaultPrevented, true);
    assert.equal(feature.activeKey, `artifact:${display.key.artifact_id}`);
    const selectImage = await pressTreeKey(treeRoot, "Enter");
    assert.equal(selectImage.defaultPrevented, true);
    assert.equal(feature.selectedKey, `artifact:${display.key.artifact_id}`);
    assert.ok(treeRoot.getAttribute("aria-activedescendant"));
    assert.equal(
      treeRoot.querySelector('[aria-selected="true"]').dataset.artifactKey,
      `artifact:${display.key.artifact_id}`,
    );
    assert.match(propertiesRoot.textContent,
      new RegExp(`Effective category${expected.image_category}`));
    assert.match(propertiesRoot.textContent,
      new RegExp(`Human caption override${expected.caption}`));

    const caption = propertiesRoot.querySelector("textarea");
    const language = propertiesRoot.querySelector("input");
    const labels = propertiesRoot.querySelectorAll("label");
    assert.equal(caption.disabled, false);
    assert.equal(language.disabled, false);
    assert.equal(labels[0].textContent, "Manual caption");
    assert.equal(labels[0].htmlFor, caption.id);
    assert.equal(labels[1].textContent, "Language");
    assert.equal(labels[1].htmlFor, language.id);

    await moveKeyboardTo(feature, treeRoot, "group:layout-regions");
    const enterRegions = await pressTreeKey(treeRoot, "ArrowRight");
    assert.equal(enterRegions.defaultPrevented, true);
    assert.ok([
      `annotation:${marginalia.key.annotation_id}`,
      `annotation:${illustration.key.annotation_id}`,
    ].includes(feature.activeKey),
    "ArrowRight reaches the first keyboard-accessible region");

    await moveKeyboardTo(feature, treeRoot,
      `annotation:${marginalia.key.annotation_id}`);
    const selectMarginalia = await pressTreeKey(treeRoot, "Enter");
    assert.equal(selectMarginalia.defaultPrevented, true);
    assert.equal(feature.selectedKey,
      `annotation:${marginalia.key.annotation_id}`);
    assert.match(propertiesRoot.textContent,
      new RegExp(`Effective role${expected.marginalia_role}`));
    assert.match(propertiesRoot.textContent,
      new RegExp(`Human role override${expected.marginalia_role}`));

    await moveKeyboardTo(feature, treeRoot,
      `annotation:${illustration.key.annotation_id}`);
    const selectRegion = await pressTreeKey(treeRoot, " ");
    assert.equal(selectRegion.defaultPrevented, true);
    assert.equal(feature.selectedKey,
      `annotation:${illustration.key.annotation_id}`);
    assert.match(propertiesRoot.textContent,
      new RegExp(`Effective role${expected.illustration_role}`));
    assert.match(propertiesRoot.textContent,
      new RegExp(`Human role override${expected.illustration_role}`));
    assert.match(propertiesRoot.textContent, /ProviderMistral/i);

    feature.destroy();
  });

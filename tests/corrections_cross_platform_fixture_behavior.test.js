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


function keyEvent(key) {
  return {
    key,
    prevented: false,
    preventDefault() { this.prevented = true; },
  };
}


test("shared release fixture is keyboard navigable into accessible Properties state",
  async () => {
    const documentRef = fakeDocument();
    const treeRoot = new FakeNode("div", documentRef);
    const propertiesRoot = new FakeNode("dl", documentRef);
    const corrections = fixture.corrections;
    const display = corrections.display_artifact;
    const illustration = corrections.illustration_region;
    const expected = corrections.expected;
    const byKey = new Map([
      [`artifact:${display.key.artifact_id}`, display],
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
            : group === "layout-regions" ? [illustration] : [];
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
          return { items: [illustration], next_cursor: null };
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

    feature.activeKey = "group:source-images";
    feature.render();
    const enterImage = keyEvent("ArrowRight");
    await feature.handleKeydown(enterImage);
    assert.equal(enterImage.prevented, true);
    assert.equal(feature.activeKey, `artifact:${display.key.artifact_id}`);
    const selectImage = keyEvent("Enter");
    await feature.handleKeydown(selectImage);
    assert.equal(selectImage.prevented, true);
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

    feature.activeKey = "group:layout-regions";
    feature.render();
    const enterRegions = keyEvent("ArrowRight");
    await feature.handleKeydown(enterRegions);
    assert.equal(enterRegions.prevented, true);
    assert.equal(feature.activeKey,
      `annotation:${illustration.key.annotation_id}`);
    const selectRegion = keyEvent(" ");
    await feature.handleKeydown(selectRegion);
    assert.equal(selectRegion.prevented, true);
    assert.equal(feature.selectedKey,
      `annotation:${illustration.key.annotation_id}`);
    assert.match(propertiesRoot.textContent,
      new RegExp(`Effective role${expected.illustration_role}`));
    assert.match(propertiesRoot.textContent,
      new RegExp(`Human role override${expected.illustration_role}`));
    assert.match(propertiesRoot.textContent, /ProviderMistral/i);

    feature.destroy();
  });

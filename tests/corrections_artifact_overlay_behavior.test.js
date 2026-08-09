"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const {
  artifactCode,
  artifactPresentationMetadata,
  createArtifactOverlay,
  createOverlayTransform,
  normalizeArtifactOverlayProfile,
  normalizeOverlayRegion,
  orientNormalizedPoint,
  projectPolygon,
} = require("../tools/whl_explorer/static/corrections/artifact-overlay");
const {
  FakeNode,
} = require("./fixtures/corrections_fake_dom");


function closePoint(actual, expected, epsilon = 1e-8) {
  assert.ok(Math.abs(actual.x - expected.x) <= epsilon,
    `expected x ${expected.x}, got ${actual.x}`);
  assert.ok(Math.abs(actual.y - expected.y) <= epsilon,
    `expected y ${expected.y}, got ${actual.y}`);
}


function region(overrides = {}) {
  return {
    key: "annotation:region-1",
    id: "region-1",
    label: "Botanical plate",
    selector: {
      type: "polygon",
      coordinate_space: "canvas-normalized",
      points: [
        { x: 0.1, y: 0.2 },
        { x: 0.6, y: 0.2 },
        { x: 0.6, y: 0.7 },
        { x: 0.1, y: 0.7 },
      ],
    },
    effectiveRole: "figure",
    freshness: "current",
    roleAssignments: [{
      origin: "manual",
      role: "figure",
      revision: "role-human-r2",
    }, {
      origin: "machine",
      role: "body",
      revision: "role-machine-r1",
      confidence: 0.82,
    }],
    captionAssertions: [{
      origin: "manual",
      text: "Plate of alpine herbs",
      revision: "caption-human-r2",
    }, {
      origin: "machine",
      text: "Figure 4",
      revision: "caption-machine-r1",
      confidence: 0.91,
    }],
    provenance: { provider_id: "mistral", model: "pixtral-large" },
    source: { representationRevision: "scan-r4" },
    ...overrides,
  };
}


function focusAwareDocument() {
  let documentRef = null;
  class FocusAwareNode extends FakeNode {
    contains(node) {
      if (node === this) return true;
      return this.children.some((child) =>
        child && typeof child.contains === "function" && child.contains(node));
    }

    replaceChildren(...nodes) {
      const active = documentRef.activeElement;
      if (active && this.contains(active)) {
        active.emit("blur");
        if (documentRef.activeElement === active) documentRef.activeElement = null;
      }
      super.replaceChildren(...nodes);
    }
  }
  documentRef = {
    activeElement: null,
    createElement(name) {
      return new FocusAwareNode(name, documentRef);
    },
  };
  return documentRef;
}


test("EXIF orientation maps normalized coordinates into the displayed raster", () => {
  closePoint(orientNormalizedPoint({ x: 0, y: 0 }, 1), { x: 0, y: 0 });
  closePoint(orientNormalizedPoint({ x: 0, y: 0 }, 3), { x: 1, y: 1 });
  closePoint(orientNormalizedPoint({ x: 0, y: 0 }, 6), { x: 1, y: 0 });
  closePoint(orientNormalizedPoint({ x: 1, y: 0 }, 6), { x: 1, y: 1 });
  closePoint(orientNormalizedPoint({ x: 0, y: 1 }, 8), { x: 1, y: 1 });
});


test("overlay projection remains aligned through resize, zoom, pan, and orientation", () => {
  const first = createOverlayTransform({
    sourceWidth: 400,
    sourceHeight: 200,
    orientation: 6,
    viewportWidth: 200,
    viewportHeight: 400,
  });
  assert.equal(first.width, 200);
  assert.equal(first.height, 400);
  closePoint(first.project({ x: 0, y: 0 }), { x: 200, y: 0 });
  closePoint(first.project({ x: 1, y: 1 }), { x: 0, y: 400 });

  const resized = createOverlayTransform({
    sourceWidth: 400,
    sourceHeight: 200,
    orientation: 6,
    viewportWidth: 400,
    viewportHeight: 800,
  });
  closePoint(resized.project({ x: 0.25, y: 0.5 }), { x: 200, y: 200 });

  const manipulated = createOverlayTransform({
    sourceWidth: 400,
    sourceHeight: 200,
    orientation: 6,
    viewportWidth: 200,
    viewportHeight: 400,
    zoom: 2,
    panX: 10,
    panY: 20,
  });
  closePoint(manipulated.project({ x: 0.5, y: 0.5 }), { x: 110, y: 220 });
  closePoint(manipulated.unproject({ x: 110, y: 220 }), { x: 0.5, y: 0.5 });

  const projected = projectPolygon(region(), manipulated);
  assert.equal(projected.length, 4);
  closePoint(projected[0], { x: 230, y: -100 });
});


test("concise overlay codes and metadata retain machine evidence and human overrides", () => {
  const value = region();
  assert.equal(artifactCode(value), "ILL");
  assert.equal(artifactCode(region({
    effectiveRole: "marginalia",
  })), "MAR");
  assert.equal(artifactCode(region({
    effectiveRole: "manuscript",
  })), "MS");
  assert.equal(artifactCode(region({
    effectiveRole: "stamp",
  })), "STP");
  assert.equal(artifactCode(region({
    effectiveRole: "damage",
  })), "DMG");
  assert.equal(artifactCode(region({
    effectiveRole: "watermark",
  })), "WAT",
  "unknown roles keep the first-three-letters fallback badge");
  assert.equal(artifactCode({
    effectiveCategory: "content_specimen",
  }), "E");

  const metadata = artifactPresentationMetadata(value);
  assert.deepEqual(metadata, {
    provider: "mistral",
    model: "pixtral-large",
    confidence: 0.82,
    sourceRevision: "scan-r4",
    machineRole: "body",
    machineCaption: "Figure 4",
    humanRole: "figure",
    humanCaption: "Plate of alpine herbs",
    freshness: "current",
  });
  const normalized = normalizeOverlayRegion(value);
  assert.equal(normalized.code, "ILL");
  assert.equal(normalizeOverlayRegion({
    ...value,
    key: undefined,
    id: undefined,
    annotation_id: "region-from-provider",
  }).key, "annotation:region-from-provider");
  assert.equal(normalizeOverlayRegion({
    ...value,
    key: undefined,
    id: "region-from-kind",
    kind: "mistral-box",
  }).key, "annotation:region-from-kind");
  assert.equal(normalizeOverlayRegion({
    ...value,
    key: "annotation:explicit-region",
    annotation_id: "ignored-provider-id",
  }).key, "annotation:explicit-region");
  assert.equal(normalized.metadata.humanCaption, "Plate of alpine herbs");
  assert.equal(normalized.metadata.machineCaption, "Figure 4");

  const laterMachineProposal = region({
    revision: "region-r3",
    roleAssignments: [
      value.roleAssignments[0],
      {
        origin: "machine",
        role: "illustration",
        revision: "role-machine-r3",
        confidence: 0.96,
      },
    ],
    captionAssertions: [
      value.captionAssertions[0],
      {
        origin: "machine",
        text: "Plate IV",
        revision: "caption-machine-r3",
        confidence: 0.96,
      },
    ],
  });
  const refreshed = artifactPresentationMetadata(laterMachineProposal);
  assert.equal(refreshed.humanRole, "figure");
  assert.equal(refreshed.humanCaption, "Plate of alpine herbs");
  assert.equal(refreshed.machineCaption, "Plate IV");
});


test("overlay renderer exposes named focusable polygons and recomputes on resize", () => {
  const documentRef = focusAwareDocument();
  const root = new FakeNode("div", documentRef);
  root.clientWidth = 200;
  root.clientHeight = 100;
  let observerCallback = null;
  let observed = null;
  let disconnected = false;
  class ResizeObserverStub {
    constructor(callback) {
      observerCallback = callback;
    }

    observe(value) {
      observed = value;
    }

    disconnect() {
      disconnected = true;
    }
  }
  const soft = [];
  const focused = [];
  const activated = [];
  const overlay = createArtifactOverlay({
    root,
    documentRef,
    ResizeObserver: ResizeObserverStub,
    onSoftTarget: (target, detail) => soft.push([target && target.key, detail]),
    onFocusTarget: (target, detail) => focused.push([target && target.key, detail]),
    onActivate: (target) => activated.push(target.key),
  });
  overlay.setView({
    sourceWidth: 400,
    sourceHeight: 200,
    orientation: 1,
  });
  overlay.setRegions([region()]);
  overlay.mount();

  assert.equal(observed, root);
  const wrapper = root.querySelector("[data-overlay-key]");
  const marker = wrapper.querySelector(".corrections-artifact-overlay-shape");
  const badge = wrapper.querySelector(".corrections-artifact-overlay-code");
  assert.match(marker.getAttribute("aria-label"), /ILL, Botanical plate/);
  assert.equal(badge.textContent, "ILL");
  assert.equal(wrapper.style.left, "20.000px");
  assert.equal(wrapper.style.width, "100.000px");

  marker.emit("pointerenter");
  assert.equal(wrapper.dataset.hot, "true");
  assert.equal(soft.at(-1)[0], "annotation:region-1");
  marker.focus();
  assert.equal(wrapper.dataset.focused, "true");
  assert.equal(wrapper.getAttribute("aria-current"), "true");
  assert.equal(focused.at(-1)[0], "annotation:region-1");
  assert.equal(documentRef.activeElement, marker);
  marker.emit("click");
  assert.deepEqual(activated, ["annotation:region-1"]);

  root.clientWidth = 400;
  root.clientHeight = 200;
  observerCallback();
  const resized = root.querySelector("[data-overlay-key]");
  const resizedMarker = resized.querySelector(
    ".corrections-artifact-overlay-shape");
  assert.equal(resized.style.left, "40.000px");
  assert.equal(resized.style.width, "200.000px");
  assert.notEqual(resizedMarker, marker);
  assert.equal(documentRef.activeElement, resizedMarker,
    "ResizeObserver rerenders restore the surviving focused marker");
  assert.equal(overlay.focusedKey, "annotation:region-1");
  assert.equal(resized.dataset.focused, "true");

  const softCount = soft.length;
  const focusedCount = focused.length;
  overlay.setRegions([region({
    key: "annotation:region-2",
    id: "region-2",
    label: "Replacement plate",
  })]);
  assert.equal(overlay.hotKey, "");
  assert.equal(overlay.focusedKey, "");
  assert.equal(soft.length, softCount + 1);
  assert.equal(focused.length, focusedCount + 1);
  assert.equal(soft.at(-1)[0], null,
    "a removed hot region emits an explicit null target");
  assert.equal(focused.at(-1)[0], null,
    "a removed focused region emits an explicit null target");
  assert.equal(documentRef.activeElement, null);
  assert.equal(
    root.querySelector("[data-overlay-key]").dataset.overlayKey,
    "annotation:region-2",
  );

  overlay.destroy();
  assert.equal(disconnected, true);
  assert.equal(root.children.length, 0);
});


test("region labels toggle at the layer without changing markers or handlers", () => {
  const documentRef = focusAwareDocument();
  const root = new FakeNode("div", documentRef);
  root.clientWidth = 200;
  root.clientHeight = 100;
  const activated = [];
  const overlay = createArtifactOverlay({
    root,
    documentRef,
    regionLabels: false,
    onActivate: (target) => activated.push(target.key),
  });
  overlay.setView({ sourceWidth: 400, sourceHeight: 200 });
  overlay.setRegions([region()]);
  overlay.mount();

  const layer = root.querySelector(".corrections-artifact-overlay-layer");
  const marker = root.querySelector(".corrections-artifact-overlay-shape");
  const badge = root.querySelector(".corrections-artifact-overlay-code");
  assert.equal(layer.dataset.regionLabels, "off");
  assert.equal(badge.getAttribute("aria-hidden"), "true");
  assert.match(marker.getAttribute("aria-label"), /ILL, Botanical plate/);

  marker.emit("click");
  assert.deepEqual(activated, ["annotation:region-1"]);
  overlay.setRegionLabels(true);
  assert.equal(layer.dataset.regionLabels, "on");
  assert.equal(root.querySelector(".corrections-artifact-overlay-shape"), marker,
    "the toggle does not rebuild the interactive marker");
  assert.equal(root.querySelector(".corrections-artifact-overlay-code"), badge,
    "the CSS-hidden badge remains mounted and aria-hidden");
  assert.deepEqual(normalizeArtifactOverlayProfile({ regionLabels: false }), {
    regionLabels: false,
  });
  assert.deepEqual(normalizeArtifactOverlayProfile(null), { regionLabels: true });
});


test("durable extraction links render dashed preview targets across rerenders", () => {
  const documentRef = focusAwareDocument();
  const root = new FakeNode("div", documentRef);
  root.clientWidth = 200;
  root.clientHeight = 100;
  const activations = [];
  const linked = region({
    linkedKeys: ["artifact:capture-1", "artifact:crop-1"],
    extensions: {
      correction_extraction: { artifact_ids: ["crop-1"] },
    },
  });
  const overlay = createArtifactOverlay({
    root,
    documentRef,
    onActivate: (target, detail) => activations.push({ target, detail }),
  });
  overlay.setView({ sourceWidth: 400, sourceHeight: 200 });
  overlay.setRegions([linked]);
  overlay.mount();

  let wrapper = root.querySelector("[data-overlay-key]");
  let marker = wrapper.querySelector(".corrections-artifact-overlay-shape");
  assert.equal(wrapper.dataset.extraction, "extracted");
  assert.equal(wrapper.dataset.extractedArtifactKey, "artifact:crop-1");
  assert.match(marker.getAttribute("aria-label"), /extracted crop available/);
  marker.emit("click");
  assert.equal(activations[0].target, linked);
  assert.equal(activations[0].detail.preview, true);
  assert.equal(
    activations[0].detail.extractedArtifactKey,
    "artifact:crop-1",
  );

  overlay.setRegionLabels(false);
  overlay.setView({ zoom: 1.5, panX: 2, panY: 3 });
  wrapper = root.querySelector("[data-overlay-key]");
  assert.equal(wrapper.dataset.extraction, "extracted",
    "persistent link state survives label and geometry rerenders");

  const repeated = region({
    extensions: {
      correction_extraction: {
        artifact_ids: ["crop-1", "crop-3", "crop-2"],
      },
    },
  });
  overlay.setRegions([repeated]);
  wrapper = root.querySelector("[data-overlay-key]");
  marker = wrapper.querySelector(".corrections-artifact-overlay-shape");
  assert.equal(wrapper.dataset.extraction, "extracted",
    "multiple durable links remain a dashed source marker");
  assert.equal(wrapper.dataset.extractedArtifactKey, "artifact:crop-2",
    "preview follows the final projected extraction link without reordering it");
  marker.emit("click");
  assert.equal(activations.at(-1).detail.extractedArtifactKey, "artifact:crop-2");
  assert.equal(activations.at(-1).detail.preview, true);

  overlay.setRegions([region()]);
  wrapper = root.querySelector("[data-overlay-key]");
  marker = wrapper.querySelector(".corrections-artifact-overlay-shape");
  assert.equal(wrapper.dataset.extraction, undefined);
  marker.emit("click");
  assert.equal(activations.at(-1).detail.preview, false);
  overlay.destroy();
});


test("destroying the overlay releases a live hot target", () => {
  const documentRef = focusAwareDocument();
  const root = new FakeNode("div", documentRef);
  root.clientWidth = 200;
  root.clientHeight = 100;
  const soft = [];
  const overlay = createArtifactOverlay({
    root,
    documentRef,
    onSoftTarget: (target) => soft.push(target && target.key || null),
  });
  overlay.setView({ sourceWidth: 400, sourceHeight: 200, orientation: 1 });
  overlay.setRegions([region()]);
  overlay.mount();

  const marker = root.querySelector(".corrections-artifact-overlay-shape");
  marker.emit("pointerenter");
  assert.deepEqual(soft, ["annotation:region-1"]);

  overlay.destroy();
  assert.deepEqual(soft, ["annotation:region-1", null],
    "removal fires no pointerleave, so destroy must emit the null target");
  assert.equal(overlay.hotKey, "");
  assert.equal(root.children.length, 0);

  overlay.destroy();
  assert.deepEqual(soft, ["annotation:region-1", null],
    "a repeated destroy stays silent");
});


test("perspective and Image Adjust tools retain pointer ownership over classification overlays", () => {
  const source = fs.readFileSync(path.join(
    __dirname,
    "..",
    "tools",
    "whl_explorer",
    "static",
    "corrections",
    "classification.css",
  ), "utf8");
  assert.match(source,
    /\.perspective-editor\[data-active-tool="perspective"\][\s\S]*?\.corrections-artifact-overlay-shape/);
  assert.match(source,
    /\.perspective-editor\[data-active-tool="image-adjust"\][\s\S]*?\.corrections-artifact-overlay-shape/);
  assert.match(source,
    /data-active-tool="image-adjust"[\s\S]*?pointer-events:\s*none/);
  assert.match(source,
    /data-region-labels="off"[\s\S]*?\.corrections-artifact-overlay-code[\s\S]*?display:\s*none/);
});

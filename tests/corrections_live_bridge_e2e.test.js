const assert = require("node:assert/strict");
const path = require("node:path");
const test = require("node:test");

const { EngineClient } = require(path.join(
  __dirname, "..", "tools", "whl_explorer", "static", "engine-client.js"));
const {
  canQueueImageAdjustShortcut,
  createImageAdjustTool,
} = require(path.join(
  __dirname, "..", "tools", "whl_explorer", "static", "corrections",
  "image-adjust-tool.js"));
const {
  COORDINATE_SPACE,
  POINT_ORDER,
  PROPOSAL_SCHEMA,
  TOOLS,
  createImageEditorState,
  nearestCornerIndex,
  reduceImageEditorState,
  serializeCorrectionTransformCommand,
} = require(path.join(
  __dirname, "..", "tools", "whl_explorer", "static", "corrections",
  "image-editor-state.js"));


const baseUrl = process.env.WHL_CORRECTIONS_E2E_BASE_URL || "";
const controlUrl = process.env.WHL_CORRECTIONS_E2E_CONTROL_URL || "";
const itemId = process.env.WHL_CORRECTIONS_E2E_ITEM_ID || "";


function newClient() {
  return new EngineClient({ transport: fetch, baseUrl });
}


function operationRevision(receipt, kind, targetId) {
  const target = receipt.receipt.targets.find(
    (value) => value.kind === kind && value.target_id === targetId);
  assert.ok(target, `missing ${kind} receipt target ${targetId}`);
  return target.after_revision;
}


function annotationForRegion(collection, regionId) {
  const annotation = collection.annotations.find(
    (value) => value.extensions &&
      value.extensions.android_geometry &&
      value.extensions.android_geometry.region_id === regionId);
  assert.ok(annotation, `missing Mistral region ${regionId}`);
  return annotation;
}


function spaceEvent() {
  return {
    key: " ",
    code: "Space",
    repeat: false,
    canvasFocused: true,
    canvasTarget: true,
    modalOpen: false,
    rectangleEditing: false,
    target: { tagName: "CANVAS" },
  };
}


test("production EngineClient completes the representative Corrections flow", {
  skip: !baseUrl || !controlUrl || !itemId,
  timeout: 30_000,
}, async () => {
  let firstClient = newClient();
  const initialReview = await firstClient.corrections.getReview({ itemId });
  const marked = await firstClient.corrections.markAttention({
    itemId,
    expectedReviewRevision: initialReview.review.revision,
    reason: "Check Mistral regions and image treatment",
    comment: "Release fixture",
    idempotencyKey: "release-e2e-attention",
  });
  const markedRevision = operationRevision(marked, "review", itemId);

  const books = await firstClient.corrections.index({
    workspaceId: "local-library",
  });
  assert.equal(books.books.length, 1);
  assert.equal(books.books[0].id, itemId);
  assert.equal(books.attention.length, 1);
  assert.equal(books.attention[0].review.revision, markedRevision);

  const rasters = await firstClient.rasterArtifacts.list({
    itemId,
    representationId: "capture",
  });
  assert.equal(rasters.artifacts.length, 2);
  let display = rasters.artifacts.find(
    (value) => value.extensions &&
      value.extensions.capture_asset &&
      value.extensions.capture_asset.variant === "display");
  if (!display) {
    display = rasters.artifacts.find(
      (value) => value.resource && value.resource.variant === "display");
  }
  assert.ok(
    display,
    `capture display artifact was not projected: ${JSON.stringify(rasters)}`,
  );
  const displayId = display.key.artifact_id;

  const category = await firstClient.corrections.assignImageCategory({
    itemId,
    artifactId: displayId,
    expectedArtifactRevision: display.revision,
    category: "title_page",
    idempotencyKey: "release-e2e-image-category",
  });
  display = (await firstClient.rasterArtifacts.get({
    itemId,
    artifactId: displayId,
  })).artifact;
  assert.equal(
    display.revision,
    operationRevision(category, "artifact", displayId),
  );

  const caption = await firstClient.corrections.setManualCaption({
    itemId,
    artifactId: displayId,
    expectedArtifactRevision: display.revision,
    text: "Reviewed botanical title page",
    language: "en",
    idempotencyKey: "release-e2e-caption",
  });
  display = (await firstClient.rasterArtifacts.get({
    itemId,
    artifactId: displayId,
  })).artifact;
  assert.equal(
    display.revision,
    operationRevision(caption, "artifact", displayId),
  );

  let annotations = await firstClient.spatialAnnotations.list({ itemId });
  assert.equal(annotations.annotations.length, 2);
  const margin = annotationForRegion(annotations, "margin-1");
  const illustration = annotationForRegion(annotations, "illustration-1");
  const marginRole = await firstClient.corrections.assignRegionRole({
    itemId,
    annotationId: margin.key.annotation_id,
    expectedAnnotationRevision: margin.revision,
    role: "marginalia",
    linkedArtifactId: displayId,
    expectedLinkedArtifactRevision: display.revision,
    idempotencyKey: "release-e2e-role-mar",
  });
  display.revision = operationRevision(marginRole, "artifact", displayId);
  await firstClient.corrections.assignRegionRole({
    itemId,
    annotationId: illustration.key.annotation_id,
    expectedAnnotationRevision: illustration.revision,
    role: "figure",
    linkedArtifactId: displayId,
    expectedLinkedArtifactRevision: display.revision,
    idempotencyKey: "release-e2e-role-ill",
  });

  display = (await firstClient.rasterArtifacts.get({
    itemId,
    artifactId: displayId,
  })).artifact;
  annotations = await firstClient.spatialAnnotations.list({ itemId });
  assert.equal(
    annotationForRegion(annotations, "margin-1").effective_role,
    "marginalia",
  );
  assert.equal(
    annotationForRegion(annotations, "illustration-1").effective_role,
    "figure",
  );

  const regionPoints = annotationForRegion(
    annotations, "illustration-1").selector.points;
  const proposal = {
    schema: PROPOSAL_SCHEMA,
    version: 1,
    coordinate_space: COORDINATE_SPACE,
    point_order: [...POINT_ORDER],
    quad: regionPoints.map((point) => [point.x, point.y]),
    confidence: 0.94,
    detector: "mistral-layout",
    detector_version: "release-e2e",
    source_revision: display.resource.revision,
  };
  let editorState = createImageEditorState({
    proposal,
    sourceRevision: display.resource.revision,
    tool: TOOLS.PERSPECTIVE,
    hasSelection: true,
  });
  const imageRect = { left: 40, top: 25, width: 700, height: 900 };
  const nearest = nearestCornerIndex(editorState.quad, imageRect, [
    imageRect.left + regionPoints[0].x * imageRect.width + 3,
    imageRect.top + regionPoints[0].y * imageRect.height + 2,
  ]);
  editorState = reduceImageEditorState(editorState, {
    type: "MOVE_CORNER",
    cornerIndex: nearest,
    point: [
      Math.min(0.95, editorState.quad[nearest][0] + 0.01),
      Math.min(0.95, editorState.quad[nearest][1] + 0.01),
    ],
  });
  assert.equal(editorState.validation.valid, true);
  editorState = reduceImageEditorState(editorState, {
    type: "SET_TOOL",
    tool: TOOLS.IMAGE_ADJUST,
  });

  const imageAdjust = createImageAdjustTool({
    profile: { lastAppliedBrightness: -3 },
  });
  imageAdjust.setBrightness(19);
  imageAdjust.setRerunOcr(true);
  const pins = {
    item_id: itemId,
    artifact_id: displayId,
    artifact_revision: display.revision,
    source_revision: display.resource.revision,
    source_sha256: display.content_sha256,
  };
  assert.equal(
    canQueueImageAdjustShortcut(spaceEvent(), editorState, pins),
    true,
  );
  const command = serializeCorrectionTransformCommand({
    pins,
    quad: editorState.quad,
    adjustment: imageAdjust.getAdjustment({ state: editorState }),
    rerunOcr: imageAdjust.getRerunOcr(),
    operationId: "release-e2e-transform",
  });
  const queued = await firstClient.corrections.queueTransform({ command });
  const duplicate = await firstClient.corrections.queueTransform({ command });
  assert.equal(queued.replayed, false);
  assert.equal(duplicate.replayed, true);
  assert.equal(duplicate.job_id, queued.job_id);

  imageAdjust.destroy();
  firstClient = null;
  const reopened = newClient();
  const observedReview = await reopened.corrections.getReview({ itemId });
  const resolved = await reopened.corrections.resolveCorrections({
    itemId,
    expectedReviewRevision: observedReview.review.revision,
    comment: "Image and roles inspected after reopening",
    idempotencyKey: "release-e2e-resolve",
  });
  const reopenedReceipt = await reopened.corrections.reopenCorrections({
    itemId,
    expectedReviewRevision: operationRevision(resolved, "review", itemId),
    comment: "OCR proposal receives a final pass",
    idempotencyKey: "release-e2e-reopen",
  });
  assert.equal(
    operationRevision(reopenedReceipt, "review", itemId).length > 0,
    true,
  );

  const released = await fetch(controlUrl, { method: "POST" });
  assert.equal(released.ok, true);

  let job = null;
  for (let attempt = 0; attempt < 200; attempt += 1) {
    job = (await reopened.jobs.get({ jobId: queued.job_id })).job;
    if (job.state === "done" || job.state === "failed" ||
        job.state === "cancelled" || job.state === "interrupted") {
      break;
    }
    await new Promise((resolve) => setTimeout(resolve, 25));
  }
  assert.ok(job, "transform job was not observable");
  assert.equal(job.state, "done");
  assert.equal(job.error, null);
  assert.equal(job.progress.completed, job.progress.total);
  assert.ok(job.outputs.some((value) => value.kind === "corrected-display"));
  assert.ok(job.outputs.some((value) => value.kind === "ocr-ready"));
  assert.ok(job.outputs.some((value) => value.kind === "ocr-proposal"));

  const corrected = await reopened.rasterArtifacts.list({
    itemId,
    representationId: display.source.representation_id,
    canvasId: display.source.canvas_id,
    group: "processed-images",
  });
  assert.ok(
    corrected.artifacts.some((value) =>
      ["corrected-image", "perspective-corrected", "processed-image",
        "processed-source"].includes(value.kind)),
    "corrected rendition was not projected back through the production bridge: " +
      JSON.stringify(corrected),
  );
  const correctedDisplay = corrected.artifacts.find(
    (value) => value.kind === "corrected-image");
  assert.ok(correctedDisplay, "corrected display was not projected");
  assert.equal(
    correctedDisplay.source.representation_id,
    display.source.representation_id,
  );
  assert.equal(correctedDisplay.source.canvas_id, display.source.canvas_id);
  assert.equal(
    correctedDisplay.source.canvas_revision,
    correctedDisplay.revision,
  );
  assert.equal(correctedDisplay.freshness, "untracked");
  const mappedAnnotations = await reopened.spatialAnnotations.list({
    itemId,
    representationId: correctedDisplay.source.representation_id,
    canvasId: correctedDisplay.source.canvas_id,
    canvasRevision: correctedDisplay.source.canvas_revision,
  });
  assert.ok(mappedAnnotations.annotations.length >= 1);
  const mappedIllustration = mappedAnnotations.annotations.find(
    (value) => value.extensions &&
      value.extensions.correction_transform &&
      value.extensions.correction_transform.source_annotation_id ===
        illustration.key.annotation_id,
  );
  assert.ok(
    mappedIllustration,
    "mapped illustration annotation was not projected",
  );
  assert.equal(mappedIllustration.effective_role, "figure");
  assert.deepEqual(mappedIllustration.linked_artifact_ids, [
    correctedDisplay.key.artifact_id,
  ]);
  const preserved = (await reopened.rasterArtifacts.get({
    itemId,
    artifactId: displayId,
  })).artifact;
  assert.equal(preserved.effective_category, "title_page");
  assert.equal(
    preserved.effective_caption.text,
    "Reviewed botanical title page",
  );
  const preservedAnnotations = await reopened.spatialAnnotations.list({
    itemId,
  });
  assert.equal(
    annotationForRegion(preservedAnnotations, "margin-1").effective_role,
    "marginalia",
  );
  assert.equal(
    annotationForRegion(preservedAnnotations, "illustration-1").effective_role,
    "figure",
  );
  const finalReview = await reopened.corrections.getReview({ itemId });
  assert.equal(finalReview.review.state, "needs_attention");
  assert.equal(finalReview.review.history_count, 3);
  assert.equal(finalReview.review.history_tail.length, 3);
  const finalHistory = await reopened.corrections.listReviewHistory({
    itemId,
    reviewRevision: finalReview.review.revision,
    limit: 10,
  });
  assert.equal(finalHistory.events.length, 3);
  assert.equal(finalHistory.next_cursor, null);
});

(function installCorrectionsEngineAdapter(root, factory) {
  const dependencies = typeof module === "object" && module.exports
    ? require("./artifact-model")
    : root.LibraryToolCorrections;
  const api = factory(dependencies);
  if (typeof module === "object" && module.exports) module.exports = api;
  else Object.assign(root.LibraryToolCorrections ||= {}, api);
})(typeof globalThis !== "undefined" ? globalThis : this,
  function correctionsEngineAdapterFactory(deps) {
    "use strict";

    const RASTER_GROUPS = new Set([
      "source-images",
      "extracted-figures",
      "processed-images",
      "generated-images",
    ]);
    const DOCUMENT_GROUPS = new Set([
      "generated-metadata",
      "ocr-text",
      "transforms",
      "unknown",
    ]);
    const ACTIVE_TRANSFORM_JOB_STATES = new Set([
      "queued", "running", "cancelling",
    ]);
    const TERMINAL_TRANSFORM_JOB_STATES = new Set([
      "cancelled", "failed", "done", "interrupted",
    ]);
    const CORRECTION_IMAGE_OUTPUT_KINDS = Object.freeze([
      "corrected-display", "ocr-ready", "thumbnail", "transform-manifest",
    ]);
    const EXTRACTION_IMAGE_OUTPUT_KINDS = Object.freeze([
      "extracted-figure", "ocr-ready", "transform-manifest",
    ]);
    const CORRECTION_JOB_OUTPUT_KINDS =
      new Set([
        ...CORRECTION_IMAGE_OUTPUT_KINDS,
        ...EXTRACTION_IMAGE_OUTPUT_KINDS,
        "ocr-proposal",
      ]);

    function transformOutputKinds(command) {
      return isPlainObject(command && command.extraction)
        ? EXTRACTION_IMAGE_OUTPUT_KINDS : CORRECTION_IMAGE_OUTPUT_KINDS;
    }
    // Standalone re-OCR shares the "correction.ocr-followup" job kind with
    // the transform rider; the operation namespace is what tells the tracked
    // command apart, so the rider's whitelist and guards stay untouched.
    const CORRECTION_REOCR_OPERATION_PREFIX = "correction-reocr:";
    const CORRECTION_OCR_FOLLOWUP_JOB_KIND = "correction.ocr-followup";
    const PORTABLE_JOB_VALUE_RE = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;

    function isStandaloneReocrCommand(command) {
      return isPlainObject(command) &&
        typeof command.operation_id === "string" &&
        command.operation_id.startsWith(CORRECTION_REOCR_OPERATION_PREFIX);
    }

    function isPlainObject(value) {
      if (!value || typeof value !== "object" || Array.isArray(value)) return false;
      const prototype = Object.getPrototypeOf(value);
      return prototype === Object.prototype || prototype === null;
    }

    function hasExactKeys(value, keys) {
      if (!isPlainObject(value)) return false;
      const actual = Object.keys(value).sort();
      const expected = [...keys].sort();
      return actual.length === expected.length &&
        actual.every((key, index) => key === expected[index]);
    }

    function portableJobValue(value) {
      return typeof value === "string" && PORTABLE_JOB_VALUE_RE.test(value);
    }

    function artifactJobRevision(value) {
      return typeof value === "string" && value.length >= 1 &&
        value.length <= 512 && /^[\x21-\x7e]+$/.test(value) &&
        !/["\\]/.test(value);
    }

    function invalidTransformResult(message) {
      const error = new Error(message);
      error.code = "invalid-transform-job-result";
      error.retryable = false;
      return error;
    }

    function cloneJson(value) {
      return value == null ? value : JSON.parse(JSON.stringify(value));
    }

    function sameJsonValue(left, right) {
      if (left === right) return true;
      if (Array.isArray(left) || Array.isArray(right)) {
        return Array.isArray(left) && Array.isArray(right) &&
          left.length === right.length &&
          left.every((value, index) => sameJsonValue(value, right[index]));
      }
      if (!isPlainObject(left) || !isPlainObject(right)) return false;
      const keys = Object.keys(left);
      return keys.length === Object.keys(right).length &&
        keys.every(
          (key) => Object.hasOwn(right, key) &&
            sameJsonValue(left[key], right[key]),
        );
    }

    function capabilityError(capability) {
      const error = new Error(`${capability} is not available`);
      error.code = "capability-unavailable";
      error.capability = capability;
      return error;
    }

    function requireEngineClient(engineClient) {
      const raster = engineClient && engineClient.rasterArtifacts;
      const spatial = engineClient && engineClient.spatialAnnotations;
      if (!raster || typeof raster.list !== "function" ||
          typeof raster.get !== "function" ||
          typeof raster.resourceUrl !== "function" ||
          !spatial || typeof spatial.list !== "function" ||
          typeof spatial.get !== "function") {
        throw new TypeError(
          "Corrections engine ports require rasterArtifacts and spatialAnnotations APIs",
        );
      }
      return engineClient;
    }

    function contextValue(context, camel, snake) {
      if (!context || typeof context !== "object") return "";
      const value = context[camel] != null ? context[camel] : context[snake];
      return value == null ? "" : String(value);
    }

    function engineQuery(context, signal) {
      return {
        itemId: contextValue(context, "itemId", "item_id"),
        representationId: contextValue(
          context, "representationId", "representation_id") || undefined,
        canvasId: contextValue(context, "canvasId", "canvas_id") || undefined,
        signal,
      };
    }

    function decorateRasterArtifact(value) {
      const extensions = value && value.extensions || {};
      const correctionsUi = extensions && extensions.corrections_ui;
      const proposal = extensions.page_boundary_proposal ||
        correctionsUi && correctionsUi.page_boundary_proposal || null;
      const resource = value && value.resource;
      const correction = value && value.resource_state === "available" &&
          resource && typeof resource.revision === "string" &&
          /^[0-9a-f]{64}$/.test(value.content_sha256 || "")
        ? Object.freeze({
            item_id: value.key && value.key.item_id,
            artifact_id: value.key && value.key.artifact_id,
            artifact_revision: value.revision,
            source_revision: resource.revision,
            source_sha256: value.content_sha256,
            proposal,
          })
        : null;
      const decorated = {
        ...value,
        artifact_id: value && value.key && value.key.artifact_id,
        object_type: "raster-artifact",
        correction,
      };
      const summary = deps.decodeArtifactSummary(decorated);
      return Object.freeze({
        ...decorated,
        group: summary.group,
      });
    }

    function decorateSpatialAnnotation(value) {
      const decorated = {
        ...value,
        annotation_id: value && value.key && value.key.annotation_id,
        object_type: "spatial-annotation",
        kind: "spatial-annotation",
        group: "layout-regions",
      };
      deps.decodeArtifactSummary(decorated);
      return Object.freeze(decorated);
    }

    function decorateDocumentArtifact(value) {
      const resource = value && value.resource || {};
      const source = value && value.source || {};
      const decorated = {
        ...value,
        artifact_id: value && value.key && value.key.artifact_id,
        object_type: "document-artifact",
        media_type: resource.media_type || value && value.media_type || "",
        content_sha256: resource.content_sha256,
        resource_state: resource.state || "unavailable",
        resource: resource.resource
          ? { ...resource.resource, variant: "text" }
          : null,
        source: {
          representation_id: source.id || "",
          representation_revision: source.revision || "",
        },
        lineage: Array.isArray(value && value.lineage)
          ? value.lineage.map((entry) => ({
              artifact_id: entry && entry.key && entry.key.artifact_id,
              artifact_revision: entry && entry.artifact_revision,
              relation: entry && entry.relation,
            }))
          : [],
        metadata: {},
      };
      const summary = deps.decodeArtifactSummary(decorated);
      return Object.freeze({
        ...decorated,
        group: summary.group,
      });
    }

    function parseCatalogKey(key) {
      const value = String(key || "");
      if (value.startsWith("artifact:") && value.length > "artifact:".length) {
        return Object.freeze({
          objectType: "raster-artifact",
          id: value.slice("artifact:".length),
        });
      }
      if (value.startsWith("annotation:") &&
          value.length > "annotation:".length) {
        return Object.freeze({
          objectType: "spatial-annotation",
          id: value.slice("annotation:".length),
        });
      }
      if (value.startsWith("document:") &&
          value.length > "document:".length) {
        return Object.freeze({
          objectType: "document-artifact",
          id: value.slice("document:".length),
        });
      }
      throw new TypeError("artifact catalog key is invalid");
    }

    function pageResult(response, values, options = {}) {
      const result = {
        revision: response && response.revision || "",
        items: Object.freeze(values),
        nextCursor: response && response.next_cursor || null,
      };
      if (options.includeTotal && Number.isSafeInteger(response.total)) {
        result.total = response.total;
      }
      return Object.freeze(result);
    }

    function rasterOwnsAnnotationFrame(artifact) {
      const source = artifact && artifact.source || {};
      const artifactId = artifact && artifact.key &&
        artifact.key.artifact_id || "";
      if (!source.canvas_id || !source.canvas_revision) return false;
      const correctionsUi = artifact && artifact.extensions &&
        artifact.extensions.corrections_ui;
      const annotationFrame = correctionsUi &&
        correctionsUi.annotation_frame;
      if (annotationFrame === "canvas") return true;
      if (annotationFrame === "crop" || annotationFrame === "detached") {
        return false;
      }
      if (["page-image", "scan", "source-image"].includes(artifact.kind)) {
        return true;
      }
      return source.representation_id === "capture" &&
        artifactId.startsWith("capture:") &&
        artifactId.endsWith(":display");
    }

    // Validates the "correction.ocr-followup" job created for one standalone
    // re-OCR queue receipt: the pins are the committed OCR-ready rendition,
    // and the parent operation id carries the "correction-reocr:" namespace.
    function correctionReocrFollowupJob(value, command, expectedJobId = "") {
      if (!isPlainObject(command) ||
          !portableJobValue(command.item_id) ||
          !portableJobValue(command.artifact_id) ||
          !portableJobValue(command.operation_id) ||
          !artifactJobRevision(command.artifact_revision) ||
          typeof command.source_sha256 !== "string" ||
          !/^[0-9a-f]{64}$/.test(command.source_sha256)) {
        throw invalidTransformResult("the tracked correction command is invalid");
      }
      if (!hasExactKeys(value, [
        "id", "kind", "state", "subject", "progress", "cancellable",
        "revision", "created_at", "updated_at", "finished_at", "note", "error",
        "input_revisions", "outputs",
      ]) || !portableJobValue(value.id) ||
          (expectedJobId && value.id !== expectedJobId) ||
          value.kind !== CORRECTION_OCR_FOLLOWUP_JOB_KIND ||
          (!ACTIVE_TRANSFORM_JOB_STATES.has(value.state) &&
            !TERMINAL_TRANSFORM_JOB_STATES.has(value.state)) ||
          !hasExactKeys(value.subject, ["item_id", "source_id"]) ||
          value.subject.item_id !== command.item_id ||
          value.subject.source_id !== command.artifact_id ||
          !hasExactKeys(value.progress, [
            "completed", "total", "unit", "phase",
          ]) ||
          !Number.isSafeInteger(value.progress.completed) ||
          value.progress.completed < 0 ||
          !Number.isSafeInteger(value.progress.total) ||
          value.progress.total < value.progress.completed ||
          typeof value.progress.unit !== "string" ||
          typeof value.progress.phase !== "string" ||
          typeof value.cancellable !== "boolean" ||
          !Number.isSafeInteger(value.revision) || value.revision < 1 ||
          typeof value.created_at !== "string" ||
          typeof value.updated_at !== "string" ||
          typeof value.finished_at !== "string" ||
          typeof value.note !== "string" ||
          !isPlainObject(value.input_revisions) ||
          !Array.isArray(value.outputs)) {
        throw invalidTransformResult(
          "the correction OCR job does not match its queued command",
        );
      }
      const inputs = value.input_revisions;
      const inputKeys = Object.keys(inputs);
      const allowedInputKeys = new Set([
        "parent_operation_id", "artifact_id", "artifact_revision",
        "source_sha256", "publication_policy", "command_sha256", "provider",
      ]);
      if (!inputKeys.every((key) => allowedInputKeys.has(key)) ||
          inputs.parent_operation_id !== command.operation_id ||
          inputs.artifact_id !== command.artifact_id ||
          inputs.artifact_revision !== command.artifact_revision ||
          inputs.source_sha256 !== command.source_sha256 ||
          inputs.publication_policy !== "machine-proposal-only" ||
          (Object.hasOwn(inputs, "command_sha256") &&
            (typeof inputs.command_sha256 !== "string" ||
              !/^[0-9a-f]{64}$/.test(inputs.command_sha256))) ||
          // The engine pins provider identity once recognition starts, so a
          // running or done job may carry it; a pre-pin poll may not.
          (Object.hasOwn(inputs, "provider") &&
            (!hasExactKeys(inputs.provider, ["provider_id", "model"]) ||
              typeof inputs.provider.provider_id !== "string" ||
              !inputs.provider.provider_id ||
              typeof inputs.provider.model !== "string" ||
              !inputs.provider.model))) {
        throw invalidTransformResult(
          "the correction OCR job input revisions are invalid",
        );
      }
      if (value.error !== null) {
        const failureKeys = Object.keys(value.error || {}).sort().join(",");
        if (!isPlainObject(value.error) ||
            !["code,message,retryable", "code,details,message,retryable"]
              .includes(failureKeys) ||
            typeof value.error.code !== "string" ||
            typeof value.error.message !== "string" ||
            typeof value.error.retryable !== "boolean" ||
            (Object.hasOwn(value.error, "details") &&
              !isPlainObject(value.error.details))) {
          throw invalidTransformResult(
            "the correction OCR job failure is invalid",
          );
        }
      }
      const refs = new Set();
      for (const output of value.outputs) {
        if (!hasExactKeys(output, ["kind", "ref", "partial"]) ||
            output.kind !== "ocr-proposal" ||
            !portableJobValue(output.ref) ||
            typeof output.partial !== "boolean" ||
            refs.has(output.ref)) {
          throw invalidTransformResult(
            "the correction OCR job outputs are invalid",
          );
        }
        refs.add(output.ref);
      }
      return Object.freeze({
        ...value,
        subject: Object.freeze({ ...value.subject }),
        progress: Object.freeze({ ...value.progress }),
        error: value.error && Object.freeze(cloneJson(value.error)),
        input_revisions: Object.freeze(
          Object.fromEntries(
            Object.entries(inputs)
              .filter(([key]) => key !== "command_sha256")
              .map(([key, entry]) => [key, cloneJson(entry)]),
          ),
        ),
        outputs: Object.freeze(
          value.outputs.map((output) => Object.freeze({ ...output })),
        ),
      });
    }

    function correctionTransformJob(value, command, expectedJobId = "") {
      if (isStandaloneReocrCommand(command)) {
        return correctionReocrFollowupJob(value, command, expectedJobId);
      }
      if (!isPlainObject(command) ||
          !portableJobValue(command.item_id) ||
          !portableJobValue(command.artifact_id) ||
          !portableJobValue(command.operation_id) ||
          !artifactJobRevision(command.artifact_revision) ||
          !artifactJobRevision(command.source_revision) ||
          typeof command.source_sha256 !== "string" ||
          !/^[0-9a-f]{64}$/.test(command.source_sha256) ||
          typeof command.rerun_ocr !== "boolean") {
        throw invalidTransformResult("the tracked correction command is invalid");
      }
      if (!hasExactKeys(value, [
        "id", "kind", "state", "subject", "progress", "cancellable",
        "revision", "created_at", "updated_at", "finished_at", "note", "error",
        "input_revisions", "outputs",
      ]) || !portableJobValue(value.id) ||
          (expectedJobId && value.id !== expectedJobId) ||
          value.kind !== "correction.transform" ||
          (!ACTIVE_TRANSFORM_JOB_STATES.has(value.state) &&
            !TERMINAL_TRANSFORM_JOB_STATES.has(value.state)) ||
          !hasExactKeys(value.subject, ["item_id", "source_id"]) ||
          value.subject.item_id !== command.item_id ||
          value.subject.source_id !== command.artifact_id ||
          !hasExactKeys(value.progress, [
            "completed", "total", "unit", "phase",
          ]) ||
          !Number.isSafeInteger(value.progress.completed) ||
          value.progress.completed < 0 ||
          !Number.isSafeInteger(value.progress.total) ||
          value.progress.total < value.progress.completed ||
          typeof value.progress.unit !== "string" ||
          typeof value.progress.phase !== "string" ||
          typeof value.cancellable !== "boolean" ||
          !Number.isSafeInteger(value.revision) || value.revision < 1 ||
          typeof value.created_at !== "string" ||
          typeof value.updated_at !== "string" ||
          typeof value.finished_at !== "string" ||
          typeof value.note !== "string" ||
          !isPlainObject(value.input_revisions) ||
          !Array.isArray(value.outputs)) {
        throw invalidTransformResult(
          "the correction transform job does not match its queued command",
        );
      }
      const inputs = value.input_revisions;
      const inputKeys = Object.keys(inputs);
      const transformKeys = ["quad", "adjustment", "rerun_ocr"];
      for (const field of ["mask_polygon", "operations", "extraction"]) {
        if (Object.hasOwn(command, field)) transformKeys.push(field);
      }
      const allowedInputKeys = new Set([
        "artifact_id", "artifact_revision", "source_revision", "source_sha256",
        "operation_id", "command_sha256", "dependent_assertions", "transform",
      ]);
      if (!inputKeys.every((key) => allowedInputKeys.has(key)) ||
          inputs.artifact_id !== command.artifact_id ||
          inputs.artifact_revision !== command.artifact_revision ||
          inputs.source_revision !== command.source_revision ||
          inputs.source_sha256 !== command.source_sha256 ||
          inputs.operation_id !== command.operation_id ||
          !Object.hasOwn(inputs, "transform") ||
          !hasExactKeys(inputs.transform, transformKeys) ||
              !sameJsonValue(inputs.transform.quad, command.quad) ||
              !sameJsonValue(
                inputs.transform.adjustment,
                command.adjustment,
              ) ||
              inputs.transform.rerun_ocr !== command.rerun_ocr ||
          (Object.hasOwn(command, "mask_polygon") &&
            !sameJsonValue(
              inputs.transform.mask_polygon,
              command.mask_polygon,
            )) ||
          (Object.hasOwn(command, "operations") &&
            !sameJsonValue(
              inputs.transform.operations,
              command.operations,
            )) ||
          (Object.hasOwn(command, "extraction") &&
            !sameJsonValue(
              inputs.transform.extraction,
              command.extraction,
            )) ||
          (Object.hasOwn(inputs, "command_sha256") &&
            (typeof inputs.command_sha256 !== "string" ||
              !/^[0-9a-f]{64}$/.test(inputs.command_sha256)))) {
        throw invalidTransformResult(
          "the correction transform job input revisions are invalid",
        );
      }
      if (value.error !== null) {
        const failureKeys = Object.keys(value.error || {}).sort().join(",");
        if (!isPlainObject(value.error) ||
            !["code,message,retryable", "code,details,message,retryable"]
              .includes(failureKeys) ||
            typeof value.error.code !== "string" ||
            typeof value.error.message !== "string" ||
            typeof value.error.retryable !== "boolean" ||
            (Object.hasOwn(value.error, "details") &&
              !isPlainObject(value.error.details))) {
          throw invalidTransformResult(
            "the correction transform job failure is invalid",
          );
        }
      }
      const refs = new Set();
      for (const output of value.outputs) {
        if (!hasExactKeys(output, ["kind", "ref", "partial"]) ||
            !CORRECTION_JOB_OUTPUT_KINDS.has(output.kind) ||
            !portableJobValue(output.ref) ||
            typeof output.partial !== "boolean" ||
            refs.has(output.ref)) {
          throw invalidTransformResult(
            "the correction transform job outputs are invalid",
          );
        }
        refs.add(output.ref);
      }
      return Object.freeze({
        ...value,
        subject: Object.freeze({ ...value.subject }),
        progress: Object.freeze({ ...value.progress }),
        error: value.error && Object.freeze(cloneJson(value.error)),
        input_revisions: Object.freeze(
          Object.fromEntries(
            Object.entries(inputs)
              .filter(([key]) => key !== "command_sha256")
              .map(([key, entry]) => [key, cloneJson(entry)]),
          ),
        ),
        outputs: Object.freeze(
          value.outputs.map((output) => Object.freeze({ ...output })),
        ),
      });
    }

    function correctionTransformJobEnvelope(value, command, expectedJobId) {
      if (!hasExactKeys(value, ["ok", "job"]) || value.ok !== true) {
        throw invalidTransformResult(
          "the correction transform job response is invalid",
        );
      }
      return correctionTransformJob(value.job, command, expectedJobId);
    }

    // Standalone jobs never carry an image commit, so the transform rider's
    // "unexpected OCR proposal" guard does not apply: a durable proposal is
    // this flow's one legitimate output.
    function correctionReocrTerminalResult(job, command) {
      if (ACTIVE_TRANSFORM_JOB_STATES.has(job.state)) return null;
      if (!TERMINAL_TRANSFORM_JOB_STATES.has(job.state)) {
        throw invalidTransformResult(
          "the correction OCR job has an unknown terminal state",
        );
      }
      const proposals = job.outputs.filter(
        (output) => output.kind === "ocr-proposal",
      );
      if (proposals.length > 1) {
        throw invalidTransformResult(
          "the correction OCR job repeats an output kind",
        );
      }
      const ocrProposal = proposals[0] || null;
      if (ocrProposal && ocrProposal.partial === true) {
        throw invalidTransformResult(
          "a correction OCR proposal cannot be partial",
        );
      }
      if (job.state === "done" && !ocrProposal) {
        throw invalidTransformResult(
          "a completed correction re-OCR job has no proposal output",
        );
      }
      const ocrSource = {
        kind: "ocr-ready",
        artifact_id: command.artifact_id,
      };
      let ocrFollowup;
      if (ocrProposal) {
        ocrFollowup = {
          state: "succeeded",
          source: ocrSource,
          proposal_ref: ocrProposal.ref,
          failure: null,
        };
      } else if (job.state === "cancelled") {
        ocrFollowup = {
          state: "cancelled",
          source: ocrSource,
          proposal_ref: "",
          failure: null,
        };
      } else if (job.state === "interrupted") {
        ocrFollowup = {
          state: "failed",
          source: ocrSource,
          proposal_ref: "",
          failure: {
            code: "ocr_followup_interrupted",
            message: "OCR follow-up status is unknown after restart",
            retryable: false,
          },
        };
      } else {
        ocrFollowup = {
          state: "failed",
          source: ocrSource,
          proposal_ref: "",
          failure: cloneJson(job.error) || {
            code: "ocr_followup_failed",
            message: job.note || "OCR follow-up failed",
            retryable: true,
          },
        };
      }
      return Object.freeze({
        job_id: job.id,
        operation_id: command.operation_id,
        terminal_state: job.state,
        image_commit: null,
        ocr_followup: Object.freeze(ocrFollowup),
        cancelled_before_commit: false,
        failure: null,
      });
    }

    function correctionTransformTerminalResult(job, command) {
      if (isStandaloneReocrCommand(command)) {
        return correctionReocrTerminalResult(job, command);
      }
      if (ACTIVE_TRANSFORM_JOB_STATES.has(job.state)) return null;
      if (!TERMINAL_TRANSFORM_JOB_STATES.has(job.state)) {
        throw invalidTransformResult(
          "the correction transform job has an unknown terminal state",
        );
      }
      const byKind = new Map();
      for (const output of job.outputs) {
        if (byKind.has(output.kind)) {
          throw invalidTransformResult(
            "the correction transform job repeats an output kind",
          );
        }
        byKind.set(output.kind, output);
      }
      const imageOutputKinds = transformOutputKinds(command);
      const presentImageKinds = imageOutputKinds.filter(
        (kind) => byKind.has(kind),
      );
      const imageCommitted =
        presentImageKinds.length === imageOutputKinds.length;
      if (presentImageKinds.length > 0 && !imageCommitted) {
        throw invalidTransformResult(
          "the correction transform job has an incomplete image commit",
        );
      }
      if (job.state === "done" && !imageCommitted) {
        throw invalidTransformResult(
          "a completed correction transform has no image outputs",
        );
      }
      if (imageCommitted && imageOutputKinds.some(
        (kind) => byKind.get(kind).partial === true,
      )) {
        throw invalidTransformResult(
          "a correction image commit cannot contain partial outputs",
        );
      }
      const ocrProposal = byKind.get("ocr-proposal") || null;
      if (ocrProposal && ocrProposal.partial === true) {
        throw invalidTransformResult(
          "a correction OCR proposal cannot be partial",
        );
      }
      if (ocrProposal && (!imageCommitted || command.rerun_ocr !== true)) {
        throw invalidTransformResult(
          "the correction transform has an unexpected OCR proposal",
        );
      }

      const ocrSource = imageCommitted
        ? {
            kind: "ocr-ready",
            artifact_id: byKind.get("ocr-ready").ref,
          }
        : null;
      let ocrFollowup = {
        state: "not_requested",
        source: null,
        proposal_ref: "",
        failure: null,
      };
      if (imageCommitted && command.rerun_ocr === true) {
        const ocrFailureCode = job.error && String(job.error.code || "");
        const hasOcrFailure = /^ocr_(?:followup|outcome)_/.test(
          ocrFailureCode,
        );
        if (ocrFailureCode === "ocr_outcome_recording_failed" ||
            /ocr outcome recording failed/i.test(job.note)) {
          ocrFollowup = {
            state: "failed",
            source: ocrSource,
            proposal_ref: "",
            failure: cloneJson(job.error) || {
              code: "ocr_outcome_recording_failed",
              message: job.note,
              retryable: true,
            },
          };
        } else if (ocrProposal) {
          ocrFollowup = {
            state: "succeeded",
            source: ocrSource,
            proposal_ref: ocrProposal.ref,
            failure: null,
          };
        } else if (job.state === "interrupted") {
          ocrFollowup = {
            state: "failed",
            source: ocrSource,
            proposal_ref: "",
            failure: {
              code: "ocr_followup_interrupted",
              message: "OCR follow-up status is unknown after restart",
              retryable: false,
            },
          };
        } else if (ocrFailureCode === "ocr_followup_cancelled" ||
                   /ocr follow-up cancelled/i.test(job.note)) {
          ocrFollowup = {
            state: "cancelled",
            source: ocrSource,
            proposal_ref: "",
            failure: null,
          };
        } else if (hasOcrFailure ||
                   /ocr follow-up failed/i.test(job.note)) {
          ocrFollowup = {
            state: "failed",
            source: ocrSource,
            proposal_ref: "",
            failure: cloneJson(job.error) || {
              code: "ocr_followup_failed",
              message: job.note || "OCR follow-up failed",
              retryable: true,
            },
          };
        } else {
          throw invalidTransformResult(
            "the requested OCR follow-up has no observable outcome",
          );
        }
      }

      return Object.freeze({
        job_id: job.id,
        operation_id: command.operation_id,
        terminal_state: job.state,
        image_commit: imageCommitted
          ? Object.freeze({
              operation_id: command.operation_id,
              outputs: Object.freeze(
                imageOutputKinds.map((kind) => Object.freeze({
                  kind,
                  artifact_id: byKind.get(kind).ref,
                })),
              ),
            })
          : null,
        ocr_followup: Object.freeze(ocrFollowup),
        cancelled_before_commit: job.state === "cancelled" && !imageCommitted,
        failure: !imageCommitted && job.state !== "cancelled"
          ? cloneJson(job.error) || {
              code: `correction_transform_${job.state}`,
              message: job.note || `Correction transform ${job.state}`,
              retryable: job.state === "interrupted",
            }
          : null,
      });
    }

    function correctionTransformPollingPort(client, options = {}) {
      if (!client || !client.jobs || typeof client.jobs.get !== "function") {
        return null;
      }
      const schedule = typeof options.schedule === "function"
        ? options.schedule
        : (callback, delay) => setTimeout(callback, delay);
      const cancelSchedule = typeof options.cancelSchedule === "function"
        ? options.cancelSchedule
        : (token) => clearTimeout(token);
      const pollIntervalMs = Number.isFinite(options.pollIntervalMs)
        ? Math.max(25, Math.floor(options.pollIntervalMs)) : 400;
      const retryIntervalMs = Number.isFinite(options.retryIntervalMs)
        ? Math.max(pollIntervalMs, Math.floor(options.retryIntervalMs)) : 1200;
      const listeners = new Set();
      // Standalone re-OCR results stay off the transform stream so the image
      // editor never mistakes a proposal-only job for a failed image commit.
      const reocrListeners = new Set();
      const pending = new Map();
      let timer = null;
      let polling = false;

      function listenerCount() {
        return listeners.size + reocrListeners.size;
      }

      function emit(result, command) {
        const target = isStandaloneReocrCommand(command)
          ? reocrListeners : listeners;
        for (const listener of [...target]) {
          try {
            listener(result, command);
          } catch (error) {
            // One workbench observer must not prevent other windows from
            // converging on the same durable job result.
          }
        }
      }

      function monitorFailure(entry, error) {
        return Object.freeze({
          job_id: entry.jobId,
          operation_id: entry.command.operation_id,
          terminal_state: "failed",
          image_commit: null,
          ocr_followup: null,
          cancelled_before_commit: false,
          failure: {
            code: String(error && error.code || "transform_job_unavailable"),
            message: String(
              error && error.message ||
              "The correction transform result is unavailable",
            ),
            retryable: error && error.retryable === true,
          },
        });
      }

      function schedulePoll(delay = pollIntervalMs) {
        if (timer != null || polling || !listenerCount() || !pending.size) return;
        timer = schedule(() => {
          timer = null;
          void poll();
        }, delay);
      }

      async function poll() {
        if (polling || !listenerCount() || !pending.size) return;
        polling = true;
        let retry = false;
        try {
          for (const [jobId, entry] of [...pending]) {
            try {
              const response = await client.jobs.get({ jobId });
              const job = correctionTransformJobEnvelope(
                response, entry.command, jobId,
              );
              const terminal = correctionTransformTerminalResult(
                job, entry.command,
              );
              if (!terminal) continue;
              pending.delete(jobId);
              emit(terminal, entry.command);
            } catch (error) {
              const permanent = error && error.retryable === false ||
                error && error.status === 404;
              if (!permanent) {
                retry = true;
                continue;
              }
              pending.delete(jobId);
              emit(monitorFailure(entry, error), entry.command);
            }
          }
        } finally {
          polling = false;
        }
        if (pending.size && listenerCount()) {
          schedulePoll(retry ? retryIntervalMs : pollIntervalMs);
        }
      }

      function track(queueResult, command) {
        const jobId = queueResult && (
          queueResult.job_id || queueResult.jobId
        );
        if (!portableJobValue(jobId)) {
          throw invalidTransformResult(
            "the correction transform queue receipt has no valid job id",
          );
        }
        if (queueResult.job) {
          correctionTransformJob(queueResult.job, command, jobId);
        }
        pending.set(jobId, {
          jobId,
          command: cloneJson(command),
        });
        schedulePoll();
      }

      function subscribeSet(target, listener) {
        if (typeof listener !== "function") {
          throw new TypeError("transform result listener is required");
        }
        target.add(listener);
        schedulePoll(25);
        return () => {
          target.delete(listener);
          if (!listenerCount() && timer != null) {
            cancelSchedule(timer);
            timer = null;
          }
        };
      }

      function subscribeResults(listener) {
        return subscribeSet(listeners, listener);
      }

      function subscribeReocrResults(listener) {
        return subscribeSet(reocrListeners, listener);
      }

      return Object.freeze({
        subscribeResults,
        subscribeReocrResults,
        track,
      });
    }

    function correctionIndexPollingPort(client, options = {}) {
      const corrections = client && client.corrections;
      if (!corrections || typeof corrections.index !== "function") return null;
      const requestedInterval = Number(options.intervalMs);
      const intervalMs = Number.isFinite(requestedInterval)
        ? Math.max(250, Math.min(30_000, Math.round(requestedInterval)))
        : 2_000;
      const schedule = typeof options.schedule === "function"
        ? options.schedule
        : (callback, delay) => {
            const token = setTimeout(callback, delay);
            if (token && typeof token.unref === "function") token.unref();
            return token;
          };
      const cancelSchedule = typeof options.cancelSchedule === "function"
        ? options.cancelSchedule
        : (token) => clearTimeout(token);
      const lifecycle = options.lifecycle &&
          typeof options.lifecycle.addEventListener === "function"
        ? options.lifecycle : null;
      const subscriptions = new Set();
      let lifecycleListening = false;

      function lifecycleActive() {
        return !lifecycle || lifecycle.visibilityState !== "hidden";
      }

      function schedulePoll(subscription, delay = intervalMs) {
        if (subscription.closed || subscription.timer != null ||
            subscription.inflight || !lifecycleActive()) return;
        subscription.timer = schedule(() => {
          subscription.timer = null;
          void poll(subscription);
        }, delay);
      }

      function stopScheduled(subscription) {
        if (subscription.timer != null) {
          cancelSchedule(subscription.timer);
          subscription.timer = null;
        }
      }

      function suspend(subscription) {
        stopScheduled(subscription);
        if (subscription.inflight && subscription.inflight.controller) {
          subscription.inflight.controller.abort();
        }
      }

      // Reading the whole index to compare one revision string made the
      // sidecar rebuild a thousand-book projection every couple of seconds for
      // as long as the window stayed open, and everything the reviewer did
      // queued behind it. The probe reports the same revision without
      // projecting anything. Older sidecars do not serve it, so fall back
      // once and stay on the index for the life of the port.
      let probeSupported = typeof corrections.indexProbe === "function";

      async function readRevision(subscription, signal) {
        if (probeSupported) {
          try {
            return await corrections.indexProbe({
              workspaceId: subscription.workspaceId,
              signal,
            });
          } catch (error) {
            if (signal && signal.aborted) throw error;
            probeSupported = false;
          }
        }
        return corrections.index({
          workspaceId: subscription.workspaceId,
          signal,
        });
      }

      async function poll(subscription) {
        if (subscription.closed || subscription.inflight ||
            !lifecycleActive()) return;
        const controller = typeof AbortController === "function"
          ? new AbortController() : null;
        const inflight = { controller };
        subscription.inflight = inflight;
        try {
          const index = await readRevision(
            subscription,
            controller && controller.signal,
          );
          if (subscription.closed || subscription.inflight !== inflight ||
              !lifecycleActive() ||
              (controller && controller.signal.aborted) ||
              !index || typeof index.revision !== "string" ||
              !index.revision) return;
          const previous = subscription.revision;
          subscription.revision = index.revision;
          if (subscription.observed && previous === index.revision) return;
          subscription.observed = false;
          try {
            subscription.onChange(Object.freeze({
              schema: "librarytool.corrections-index-change/1",
              revision: index.revision,
            }));
          } catch (error) {
            // One renderer listener cannot stop future convergence checks.
          }
        } catch (error) {
          // Polling is best-effort. The next bounded pass retries unless the
          // request was suspended or the subscription was released.
        } finally {
          if (subscription.inflight === inflight) {
            subscription.inflight = null;
          }
          schedulePoll(subscription);
        }
      }

      function handleLifecycleChange() {
        for (const subscription of subscriptions) {
          if (lifecycleActive()) schedulePoll(subscription, 0);
          else suspend(subscription);
        }
      }

      function connectLifecycle() {
        if (!lifecycle || lifecycleListening) return;
        lifecycle.addEventListener("visibilitychange", handleLifecycleChange);
        lifecycleListening = true;
      }

      function disconnectLifecycle() {
        if (!lifecycle || !lifecycleListening) return;
        lifecycle.removeEventListener("visibilitychange", handleLifecycleChange);
        lifecycleListening = false;
      }

      function observe(workspaceId, revision) {
        if (!workspaceId || !revision) return;
        for (const subscription of subscriptions) {
          if (subscription.workspaceId === workspaceId) {
            subscription.revision = revision;
            subscription.observed = true;
          }
        }
      }

      function subscribe({
        workspaceId, afterRevision = null, onChange,
      } = {}) {
        if (typeof workspaceId !== "string" || !workspaceId) {
          throw new TypeError("workspaceId is required");
        }
        if (typeof onChange !== "function") {
          throw new TypeError("Corrections change listener is required");
        }
        const subscription = {
          workspaceId,
          revision: afterRevision || "",
          observed: Boolean(afterRevision),
          onChange,
          timer: null,
          inflight: null,
          closed: false,
        };
        subscriptions.add(subscription);
        connectLifecycle();
        schedulePoll(subscription);
        return () => {
          if (subscription.closed) return;
          subscription.closed = true;
          subscriptions.delete(subscription);
          suspend(subscription);
          if (!subscriptions.size) disconnectLifecycle();
        };
      }

      return Object.freeze({
        observe,
        subscribe,
      });
    }

    function correctionCommandPort(client, transformPolling = null) {
      const corrections = client && client.corrections &&
        typeof client.corrections === "object" ? client.corrections : {};
      const commands = {};
      if (typeof corrections.assignImageCategory === "function") {
        commands.assignImageCategory = ({
          operationId, ...payload
        } = {}) => corrections.assignImageCategory({
          ...payload,
          idempotencyKey: operationId,
        });
      }
      if (typeof corrections.clearImageCategory === "function") {
        commands.clearImageCategory = ({
          operationId, ...payload
        } = {}) => corrections.clearImageCategory({
          ...payload,
          idempotencyKey: operationId,
        });
      }
      if (typeof corrections.assignRegionRole === "function") {
        commands.assignRegionRole = ({
          operationId, ...payload
        } = {}) => corrections.assignRegionRole({
          ...payload,
          idempotencyKey: operationId,
        });
      }
      if (typeof corrections.clearRegionRole === "function") {
        commands.clearRegionRole = ({
          operationId, ...payload
        } = {}) => corrections.clearRegionRole({
          ...payload,
          idempotencyKey: operationId,
        });
      }
      if (typeof corrections.queueTransform === "function") {
        commands.queueTransform = async ({
          command, signal,
        } = {}) => {
          const result = await corrections.queueTransform({ command, signal });
          if (transformPolling) transformPolling.track(result, command);
          return result;
        };
      }
      if (typeof corrections.queueReocr === "function") {
        commands.queueReocr = async ({
          operationId, itemId, artifactId, expectedArtifactRevision, signal,
        } = {}) => {
          const receipt = await corrections.queueReocr({
            itemId,
            artifactId,
            expectedArtifactRevision,
            idempotencyKey: operationId,
            signal,
          });
          if (transformPolling) {
            // The receipt pins the OCR-ready rendition the job actually
            // reads; tracking follows that source, not the artifact the
            // user pointed at.
            const source = receipt.source || {};
            transformPolling.track(receipt, {
              item_id: itemId,
              artifact_id: source.artifact_id,
              artifact_revision: source.artifact_revision,
              source_sha256: source.content_sha256,
              operation_id: receipt.operation_id,
            });
          }
          return receipt;
        };
      }
      if (typeof corrections.setManualCaption === "function") {
        commands.setManualCaption = ({
          operationId, ...payload
        } = {}) => corrections.setManualCaption({
          ...payload,
          idempotencyKey: operationId,
        });
      }
      if (typeof corrections.clearManualCaption === "function") {
        commands.clearManualCaption = ({
          operationId, ...payload
        } = {}) => corrections.clearManualCaption({
          ...payload,
          idempotencyKey: operationId,
        });
      }
      if (typeof corrections.assertArtifactMetadata === "function") {
        commands.assertArtifactMetadata = ({
          operationId, ...payload
        } = {}) => corrections.assertArtifactMetadata({
          ...payload,
          idempotencyKey: operationId,
        });
      }
      if (typeof corrections.markAttention === "function") {
        commands.markAttention = ({
          operationId, actorId: _ignoredActorId, ...payload
        } = {}) => corrections.markAttention({
          ...payload,
          idempotencyKey: operationId,
        });
      }
      if (typeof corrections.resolveCorrections === "function") {
        commands.resolveCorrections = ({
          operationId, actorId: _ignoredActorId, ...payload
        } = {}) => corrections.resolveCorrections({
          ...payload,
          idempotencyKey: operationId,
        });
      }
      if (typeof corrections.reopenCorrections === "function") {
        commands.reopenCorrections = ({
          operationId, actorId: _ignoredActorId, ...payload
        } = {}) => corrections.reopenCorrections({
          ...payload,
          idempotencyKey: operationId,
        });
      }
      const raster = client && client.rasterArtifacts;
      if (raster && typeof raster.trashCaptureAsset === "function") {
        commands.trashCaptureAsset = ({
          operationId, ...payload
        } = {}) => raster.trashCaptureAsset({
          ...payload,
          idempotencyKey: operationId,
        });
      }
      return Object.keys(commands).length ? Object.freeze(commands) : null;
    }

    function correctionOcrProposalPort(client, transformPolling, commands) {
      const corrections = client && client.corrections;
      if (!corrections ||
          typeof corrections.listOcrProposals !== "function" ||
          typeof corrections.getOcrProposal !== "function") {
        return null;
      }
      const port = {
        list: ({ itemId, cursor, limit, snapshotRevision, signal } = {}) =>
          corrections.listOcrProposals({
            itemId, cursor, limit, snapshotRevision, signal,
          }),
        get: ({ itemId, proposalRef, signal } = {}) =>
          corrections.getOcrProposal({ itemId, proposalRef, signal }),
      };
      if (typeof corrections.applyOcrProposal === "function") {
        port.apply = ({ operationId, itemId, proposalRef, signal } = {}) =>
          corrections.applyOcrProposal({
            itemId,
            proposalRef,
            idempotencyKey: operationId,
            signal,
          });
      }
      if (commands && typeof commands.queueReocr === "function") {
        port.queueReocr = commands.queueReocr;
      }
      if (transformPolling &&
          typeof transformPolling.subscribeReocrResults === "function") {
        port.subscribeResults = transformPolling.subscribeReocrResults;
      }
      return Object.freeze(port);
    }

    function correctionReviewPort(client) {
      const corrections = client && client.corrections;
      if (!corrections ||
          typeof corrections.getReview !== "function" ||
          typeof corrections.listReviewHistory !== "function") {
        return null;
      }
      return Object.freeze({
        async get({ context, itemId, signal } = {}) {
          const id = itemId || contextValue(context, "itemId", "item_id");
          const response = await corrections.getReview({
            itemId: id,
            signal,
          });
          return Object.freeze({
            itemId: response.item_id,
            ...response.review,
          });
        },
        async listHistory({
          context, itemId, reviewRevision, cursor, limit, signal,
        } = {}) {
          const id = itemId || contextValue(context, "itemId", "item_id");
          const response = await corrections.listReviewHistory({
            itemId: id,
            reviewRevision,
            cursor,
            limit,
            signal,
          });
          return Object.freeze({
            itemId: response.item_id,
            revision: response.review_revision,
            state: response.review_state,
            events: Object.freeze(response.events.slice()),
            nextCursor: response.next_cursor,
            total: response.total,
          });
        },
      });
    }

    function correctionBooksPort(client, indexPolling = null) {
      const corrections = client && client.corrections;
      if (!corrections || typeof corrections.index !== "function" ||
          typeof corrections.getReview !== "function" ||
          typeof corrections.listReviewHistory !== "function") {
        return null;
      }
      let workspaceId = null;
      let requestedWorkspaceId = null;
      let workspaceRequestGeneration = 0;
      let workspaceEpoch = 0;
      // The tiered index routes (summary + details) replace the monolithic
      // /corrections/index read wherever the client serves them. The summary
      // is the only request that holds the workspace lease for a whole
      // projection pass, and it reads no capture manifests at all, so a load
      // stops starving concurrent per-item requests. An engine client without
      // the tiered calls keeps the monolithic path byte-for-byte unchanged.
      const tiered = typeof corrections.indexSummary === "function" &&
        typeof corrections.indexDetails === "function";
      const DETAIL_BATCH_LIMIT = 256;
      // The last whole index this port produced, so a review mutation can
      // converge through one single-item detail window instead of reloading
      // every book.
      let assembled = null;

      function invalidIndexAssembly(message) {
        const error = new Error(message);
        error.code = "invalid-corrections-index-assembly";
        return error;
      }

      async function assembleTieredIndex(
        nextWorkspaceId,
        signal,
        retry = true,
      ) {
        const summary = await corrections.indexSummary({
          workspaceId: nextWorkspaceId,
          signal,
        });
        const ids = summary.books.map((book) => book.id);
        const batches = [];
        for (let start = 0; start < ids.length; start += DETAIL_BATCH_LIMIT) {
          batches.push(ids.slice(start, start + DETAIL_BATCH_LIMIT));
        }
        const details = await Promise.all(batches.map((itemIds) =>
          corrections.indexDetails({
            workspaceId: nextWorkspaceId,
            itemIds,
            signal,
          })));
        const rows = new Map();
        let torn = false;
        for (const detail of details) {
          if (detail.missing.length) torn = true;
          for (const book of detail.books) rows.set(book.id, book);
        }
        torn = torn || ids.some((id) => !rows.has(id));
        if (torn) {
          // A book the summary listed but no detail window resolved was
          // deleted between the two reads. One clean retry re-lists; a second
          // tear is reported rather than papered over with a fabricated row.
          if (retry) {
            return assembleTieredIndex(nextWorkspaceId, signal, false);
          }
          throw invalidIndexAssembly(
            "The Corrections summary and its detail windows kept diverging");
        }
        // A detail row is exactly what the monolithic index would have
        // inlined, including its captures — "captures": [] is the row's
        // positive claim that the book has none. The review is still taken
        // from the summary: the attention list comes from the summary
        // snapshot and the index contract requires each book's review to
        // equal its attention entry, so a review that moved between the two
        // reads must not tear the assembled document. The next convergence
        // poll reloads the newer state.
        const books = summary.books.map((lean) => ({
          ...rows.get(lean.id),
          review: lean.review,
        }));
        return {
          schema: "librarytool.corrections-index/2",
          revision: summary.revision,
          books,
          attention: summary.attention,
        };
      }

      function loadWholeIndex(nextWorkspaceId, signal) {
        return tiered
          ? assembleTieredIndex(nextWorkspaceId, signal)
          : corrections.index({ workspaceId: nextWorkspaceId, signal });
      }

      function itemIdForTarget(target) {
        if (!target || target.kind !== "book" ||
            typeof target.item_id !== "string" || !target.item_id) {
          throw capabilityError("non-book correction reviews");
        }
        return target.item_id;
      }

      function invalidHistory(message) {
        const error = new Error(message);
        error.code = "invalid-corrections-review-history";
        return error;
      }

      function sameAuditEvent(left, right) {
        return [
          "operation_id", "action", "actor_id", "occurred_at",
          "before_state", "after_state", "reason", "comment",
        ].every((field) => left[field] === right[field]);
      }

      function sameReviewSummary(left, right) {
        return left.revision === right.revision &&
          left.state === right.state &&
          left.reason === right.reason &&
          left.history_count === right.history_count &&
          ((left.latest_event === null && right.latest_event === null) ||
            (left.latest_event !== null && right.latest_event !== null &&
              sameAuditEvent(left.latest_event, right.latest_event)));
      }

      async function assembleReview(target, signal, retry = true) {
        const itemId = itemIdForTarget(target);
        const response = await corrections.getReview({ itemId, signal });
        const summary = response.review;
        const events = [];
        const operationIds = new Set();
        const cursors = new Set();
        const maximumPages = Math.max(1, summary.history_count);
        let pageCount = 0;
        let cursor = null;
        try {
          do {
            pageCount += 1;
            if (pageCount > maximumPages) {
              throw invalidHistory(
                "Correction review history exceeded its page budget");
            }
            const page = await corrections.listReviewHistory({
              itemId,
              reviewRevision: summary.revision,
              cursor,
              limit: 100,
              signal,
            });
            if (page.review_revision !== summary.revision ||
                page.review_state !== summary.state ||
                page.total !== summary.history_count) {
              throw invalidHistory(
                "Correction review history changed while it was assembled");
            }
            const priorCount = events.length;
            for (const event of page.events) {
              if (operationIds.has(event.operation_id)) {
                throw invalidHistory(
                  "Correction review history contains duplicate operations");
              }
              const previous = events.at(-1);
              if (previous && previous.after_state !== event.before_state) {
                throw invalidHistory(
                  "Correction review history is not continuous across pages");
              }
              operationIds.add(event.operation_id);
              events.push(event);
            }
            if (events.length > summary.history_count) {
              throw invalidHistory(
                "Correction review history exceeded its declared total");
            }
            if (events.length === priorCount &&
                events.length < summary.history_count) {
              throw invalidHistory(
                "Correction review history paging made no progress");
            }
            const next = page.next_cursor;
            if (next !== null &&
                events.length >= summary.history_count) {
              throw invalidHistory(
                "Correction review history continued past its declared total");
            }
            if (next !== null) {
              if (cursors.has(next)) {
                throw invalidHistory(
                  "Correction review history repeated a paging cursor");
              }
              cursors.add(next);
            }
            cursor = next;
          } while (cursor !== null);
        } catch (error) {
          if (retry && error && error.code === "review_revision_conflict") {
            return assembleReview(target, signal, false);
          }
          throw error;
        }
        if (events.length !== summary.history_count ||
            (events.length > 0 &&
              events.at(-1).after_state !== summary.state)) {
          throw invalidHistory(
            "Correction review history is incomplete");
        }
        const expectedTail = summary.history_tail;
        const observedTail = expectedTail.length
          ? events.slice(-expectedTail.length)
          : [];
        if (observedTail.length !== expectedTail.length ||
            observedTail.some((event, index) =>
              !sameAuditEvent(event, expectedTail[index]))) {
          throw invalidHistory(
            "Correction review summary does not match its audit history");
        }
        return Object.freeze({
          schema: "librarytool.corrections-review/1",
          target: Object.freeze({ kind: "book", item_id: itemId }),
          review: Object.freeze({
            revision: summary.revision,
            state: summary.state,
            reason: summary.reason,
            history: Object.freeze(events.slice()),
          }),
        });
      }

      function invalidMutation(message) {
        const error = new Error(message);
        error.code = "invalid-corrections-review-result";
        return error;
      }

      function reviewConflict(message) {
        const error = new Error(message);
        error.code = "review_revision_conflict";
        error.status = 409;
        return error;
      }

      function currentWorkspace() {
        if (!workspaceId) {
          throw new Error("The Corrections workspace has not been opened");
        }
        return Object.freeze({
          id: workspaceId,
          epoch: workspaceEpoch,
        });
      }

      function workspaceIsCurrent(workspace) {
        return workspace &&
          workspace.id === workspaceId &&
          workspace.epoch === workspaceEpoch;
      }

      function receiptReviewRevision(mutation, itemId) {
        const targets = mutation && mutation.receipt &&
          mutation.receipt.targets;
        if (!Array.isArray(targets) || targets.length !== 1) {
          throw invalidMutation(
            "Engine review mutation returned an invalid receipt");
        }
        const target = targets[0];
        if (!target || target.kind !== "review" ||
            target.target_id !== itemId ||
            typeof target.after_revision !== "string" ||
            !target.after_revision) {
          throw invalidMutation(
            "Engine review mutation receipt did not identify its review");
        }
        return target.after_revision;
      }

      async function refreshMutationEntryFromDetails(
        itemId,
        expectedRevision,
        expectedState,
        workspace,
        signal,
      ) {
        const cached = assembled &&
          assembled.workspaceId === workspace.id &&
          assembled.epoch === workspace.epoch
          ? assembled.index : null;
        const cachedEntry = cached ? cached.attention.find((candidate) =>
          candidate.target && candidate.target.kind === "book" &&
          candidate.target.item_id === itemId) : null;
        const cachedBook = cached ? cached.books.find((candidate) =>
          candidate.id === itemId) : null;
        // Review transitions only move between non-clear states, so the
        // acted-on book and its attention entry are both in the last loaded
        // index. Without one (no index loaded through this port yet) the
        // whole-index fallback converges instead.
        if (!cached || !cachedEntry || !cachedBook) return null;
        const details = await corrections.indexDetails({
          workspaceId: workspace.id,
          itemIds: [itemId],
          signal,
        });
        if (!workspaceIsCurrent(workspace)) {
          throw reviewConflict(
            "The Corrections workspace changed before the mutation converged");
        }
        const book = details.books.find((candidate) =>
          candidate.id === itemId);
        if (!book) {
          throw invalidMutation(
            "Engine review mutation did not return its indexed book review");
        }
        if (book.review.revision !== expectedRevision ||
            book.review.state !== expectedState) {
          throw reviewConflict(
            "The review changed again before the mutation converged");
        }
        const entry = {
          key: cachedEntry.key,
          target: { kind: "book", item_id: itemId },
          review: book.review,
        };
        // The detail window's revision stands in for the index revision: it
        // moves with every change to the acted-on row and can never equal a
        // summary revision, so the convergence poll still reloads the whole
        // index once the server has settled.
        const index = {
          schema: cached.schema,
          revision: details.revision,
          books: cached.books.map((candidate) =>
            candidate.id === itemId ? book : candidate),
          attention: cached.attention.map((candidate) =>
            candidate === cachedEntry ? entry : candidate),
        };
        assembled = Object.freeze({
          workspaceId: workspace.id,
          epoch: workspace.epoch,
          index,
        });
        return Object.freeze({
          schema: "librarytool.corrections-review-result/1",
          index_revision: index.revision,
          entry,
          index,
        });
      }

      async function refreshMutationEntry(
        target,
        mutation,
        expectedState,
        workspace,
        signal,
      ) {
        if (!workspaceIsCurrent(workspace)) {
          throw reviewConflict(
            "The Corrections workspace changed before the mutation converged");
        }
        const itemId = itemIdForTarget(target);
        const expectedRevision = receiptReviewRevision(mutation, itemId);
        if (tiered) {
          const patched = await refreshMutationEntryFromDetails(
            itemId,
            expectedRevision,
            expectedState,
            workspace,
            signal,
          );
          if (patched) return patched;
        }
        const index = await loadWholeIndex(workspace.id, signal);
        if (!workspaceIsCurrent(workspace)) {
          throw reviewConflict(
            "The Corrections workspace changed before the mutation converged");
        }
        const book = index.books.find((candidate) =>
          candidate.id === itemId);
        const entry = index.attention.find((candidate) =>
          candidate.target && candidate.target.kind === "book" &&
          candidate.target.item_id === itemId);
        if (!book || !entry) {
          throw invalidMutation(
            "Engine review mutation did not return its indexed book review");
        }
        if (!sameReviewSummary(book.review, entry.review)) {
          throw invalidMutation(
            "Engine review mutation returned contradictory indexed reviews");
        }
        if (entry.review.revision !== expectedRevision ||
            entry.review.state !== expectedState) {
          throw reviewConflict(
            "The review changed again before the mutation converged");
        }
        assembled = Object.freeze({
          workspaceId: workspace.id,
          epoch: workspace.epoch,
          index,
        });
        return Object.freeze({
          schema: "librarytool.corrections-review-result/1",
          index_revision: index.revision,
          entry,
          index,
        });
      }

      const books = {
        trustedActor: true,
        async loadIndex({ workspaceId: nextWorkspaceId, signal } = {}) {
          const generation = ++workspaceRequestGeneration;
          if (requestedWorkspaceId !== nextWorkspaceId) {
            requestedWorkspaceId = nextWorkspaceId;
            workspaceEpoch += 1;
            workspaceId = null;
          }
          const index = await loadWholeIndex(nextWorkspaceId, signal);
          if (indexPolling && typeof indexPolling.observe === "function" &&
              !(signal && signal.aborted)) {
            indexPolling.observe(nextWorkspaceId, index && index.revision);
          }
          if (generation === workspaceRequestGeneration &&
              requestedWorkspaceId === nextWorkspaceId &&
              !(signal && signal.aborted)) {
            workspaceId = nextWorkspaceId;
            assembled = Object.freeze({
              workspaceId: nextWorkspaceId,
              epoch: workspaceEpoch,
              index,
            });
          }
          return index;
        },
        getReview({ target, signal } = {}) {
          return assembleReview(target, signal);
        },
      };
      if (indexPolling && typeof indexPolling.subscribe === "function") {
        books.subscribe = (options = {}) => indexPolling.subscribe(options);
      }
      if (typeof corrections.resolveCorrections === "function") {
        books.resolveReview = async ({
          target, expectedRevision, operationId, comment = "", signal,
        } = {}) => {
          const itemId = itemIdForTarget(target);
          const workspace = currentWorkspace();
          const mutation = await corrections.resolveCorrections({
            itemId,
            expectedReviewRevision: expectedRevision,
            idempotencyKey: operationId,
            comment,
            signal,
          });
          return refreshMutationEntry(
            target,
            mutation,
            "resolved",
            workspace,
            signal,
          );
        };
      }
      if (typeof corrections.reopenCorrections === "function") {
        books.reopenReview = async ({
          target, expectedRevision, operationId, comment = "", signal,
        } = {}) => {
          const itemId = itemIdForTarget(target);
          const workspace = currentWorkspace();
          const mutation = await corrections.reopenCorrections({
            itemId,
            expectedReviewRevision: expectedRevision,
            idempotencyKey: operationId,
            comment,
            signal,
          });
          return refreshMutationEntry(
            target,
            mutation,
            "needs_attention",
            workspace,
            signal,
          );
        };
      }
      return Object.freeze(books);
    }

    function createCorrectionsEnginePorts(engineClient, options = {}) {
      const client = requireEngineClient(engineClient);
      const documents = client.documentArtifacts;
      const documentsAvailable = !!documents &&
        typeof documents.list === "function" &&
        typeof documents.get === "function" &&
        typeof documents.readPage === "function";
      const portOptions = isPlainObject(options) ? options : {};
      const transformPolling = correctionTransformPollingPort(
        client,
        isPlainObject(portOptions.transformPolling)
          ? portOptions.transformPolling : {},
      );
      const indexPolling = correctionIndexPollingPort(
        client,
        isPlainObject(portOptions.indexPolling)
          ? portOptions.indexPolling : {},
      );
      const commands = correctionCommandPort(client, transformPolling);
      const reviews = correctionReviewPort(client);
      const books = correctionBooksPort(client, indexPolling);
      const processingPresets = client.processingPresets &&
          typeof client.processingPresets.list === "function" &&
          typeof client.processingPresets.create === "function" &&
          typeof client.processingPresets.update === "function" &&
          typeof client.processingPresets.remove === "function"
        ? client.processingPresets : null;
      const ocrProposals = correctionOcrProposalPort(
        client, transformPolling, commands);

      async function listRasterGroup({ context, group, cursor, limit, signal }) {
        if (!RASTER_GROUPS.has(group)) {
          return pageResult(null, []);
        }
        const query = engineQuery(context, signal);
        const response = await client.rasterArtifacts.list({
          ...query,
          group,
          cursor: cursor || null,
          limit,
        });
        const values = response.artifacts
          .map(decorateRasterArtifact)
          .filter((value) => value.group === group);
        return pageResult(response, values, { includeTotal: true });
      }

      async function listSpatial({ context, cursor, limit, signal }) {
        const response = await client.spatialAnnotations.list({
          ...engineQuery(context, signal),
          visibility: "tree",
          cursor: cursor || null,
          limit,
        });
        return pageResult(
          response,
          response.annotations.map(decorateSpatialAnnotation),
          { includeTotal: true },
        );
      }

      async function listDocumentGroup({
        context, group, cursor, limit, signal,
      }) {
        if (!DOCUMENT_GROUPS.has(group) || !documentsAvailable) {
          return pageResult(null, []);
        }
        const response = await documents.list({
          itemId: contextValue(context, "itemId", "item_id"),
          cursor: cursor || null,
          limit,
          signal,
        });
        const values = response.artifacts
          .map(decorateDocumentArtifact)
          .filter((value) => value.group === group);
        return Object.freeze({
          revision: response.snapshot_revision || "",
          items: Object.freeze(values),
          nextCursor: response.next_cursor || null,
        });
      }

      async function listRegions({
        context, representationId, canvasId, canvasRevision, cursor, limit,
        signal,
      }) {
        const query = engineQuery(context, signal);
        const response = await client.spatialAnnotations.list({
          ...query,
          representationId: representationId || query.representationId,
          canvasId: canvasId || query.canvasId,
          canvasRevision,
          visibility: "overlay",
          cursor: cursor || null,
          limit,
        });
        const expectedRepresentation = representationId ||
          query.representationId;
        const expectedCanvas = canvasId || query.canvasId;
        const values = response.annotations
          .map(decorateSpatialAnnotation)
          .filter((annotation) => {
            const source = annotation.source || {};
            return (!expectedRepresentation ||
                source.representation_id === expectedRepresentation) &&
              (!expectedCanvas || source.canvas_id === expectedCanvas) &&
              (!canvasRevision ||
                source.canvas_revision === canvasRevision);
          });
        return pageResult(
          response,
          values,
          { includeTotal: true },
        );
      }

      async function rasterDetail(context, artifactId, signal) {
        const query = engineQuery(context, signal);
        const response = await client.rasterArtifacts.get({
          itemId: query.itemId,
          artifactId,
          signal,
        });
        const artifact = decorateRasterArtifact(response.artifact);
        const source = artifact.source || {};
        const correctionsUi = artifact.extensions &&
          artifact.extensions.corrections_ui;
        return Object.freeze({
          ...artifact,
          extensions: Object.freeze({
            ...(artifact.extensions || {}),
            corrections_ui: Object.freeze({
              ...(correctionsUi && typeof correctionsUi === "object" &&
                !Array.isArray(correctionsUi) ? correctionsUi : {}),
              paged_regions: Boolean(
                source.canvas_id && rasterOwnsAnnotationFrame(artifact)),
            }),
          }),
        });
      }

      const artifacts = Object.freeze({
        catalog: Object.freeze({
          list(args = {}) {
            if (args.group === "layout-regions") return listSpatial(args);
            if (RASTER_GROUPS.has(args.group)) return listRasterGroup(args);
            return listDocumentGroup(args);
          },
          async get({ context, key, signal } = {}) {
            const parsed = parseCatalogKey(key);
            const itemId = contextValue(context, "itemId", "item_id");
            if (parsed.objectType === "raster-artifact") {
              return rasterDetail(context, parsed.id, signal);
            }
            if (parsed.objectType === "document-artifact") {
              if (!documentsAvailable) {
                throw capabilityError("document artifact detail");
              }
              const response = await documents.get({
                itemId,
                artifactId: parsed.id,
                signal,
              });
              return decorateDocumentArtifact(response.artifact);
            }
            const response = await client.spatialAnnotations.get({
              itemId,
              annotationId: parsed.id,
              signal,
            });
            return decorateSpatialAnnotation(response.annotation);
          },
        }),
        resources: Object.freeze({
          async resolveRaster({
            itemId, artifactId, resourceRef, signal,
          } = {}) {
            if (!resourceRef || !resourceRef.revision) {
              throw new TypeError("raster resource revision is required");
            }
            if (typeof client.rasterArtifacts.resolveResource === "function") {
              return client.rasterArtifacts.resolveResource({
                itemId,
                artifactId,
                revision: resourceRef.revision,
                signal,
              });
            }
            return Object.freeze({
              url: client.rasterArtifacts.resourceUrl({
                itemId,
                artifactId,
                revision: resourceRef.revision,
              }),
            });
          },
          readText(args = {}) {
            if (!documentsAvailable) {
              return Promise.reject(capabilityError("paged text reader"));
            }
            const resource = args.resourceRef || {};
            let offset = 0;
            if (args.cursor != null && args.cursor !== "") {
              if (typeof args.cursor !== "string" ||
                  !/^(?:0|[1-9][0-9]{0,15})$/.test(args.cursor)) {
                return Promise.reject(
                  new TypeError("document resource cursor is invalid"));
              }
              offset = Number(args.cursor);
              if (!Number.isSafeInteger(offset)) {
                return Promise.reject(
                  new TypeError("document resource cursor is invalid"));
              }
            }
            const maximum = Math.min(
              48 * 1024,
              Number.isSafeInteger(args.limit) ? args.limit : 48 * 1024,
            );
            return documents.readPage({
              itemId: args.itemId,
              artifactId: args.artifactId,
              artifactRevision: args.artifactRevision,
              resourceId: resource.id,
              resourceRevision: resource.revision,
              mode: "text",
              offset,
              maxBytes: maximum,
              signal: args.signal,
            }).then((page) => Object.freeze({
              text: page.data,
              nextCursor: page.next_offset == null
                ? null : String(page.next_offset),
            }));
          },
          listRegions,
        }),
        ...(commands ? { commands } : {}),
      });

      const invokeCommand = commands &&
          typeof commands.queueTransform === "function"
        ? (commandId, payload = {}) => {
            if (commandId !== "corrections.transform.queue") {
              return Promise.reject(capabilityError(commandId));
            }
            return commands.queueTransform(payload);
          }
        : null;
      return Object.freeze({
        artifacts,
        ...(books ? { books } : {}),
        ...(reviews ? { reviews } : {}),
        ...(processingPresets ? { processingPresets } : {}),
        ...(ocrProposals ? { ocrProposals } : {}),
        ...(invokeCommand ? { invokeCommand } : {}),
        ...(transformPolling ? {
          transforms: Object.freeze({
            subscribeResults: transformPolling.subscribeResults,
          }),
        } : {}),
      });
    }

    return {
      correctionTransformJob,
      correctionTransformTerminalResult,
      correctionTransformPollingPort,
      correctionIndexPollingPort,
      createCorrectionsEnginePorts,
      decorateRasterArtifact,
      decorateDocumentArtifact,
      decorateSpatialAnnotation,
      isStandaloneReocrCommand,
    };
  });

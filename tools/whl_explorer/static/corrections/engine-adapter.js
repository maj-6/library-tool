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
    const ACTIVE_TRANSFORM_JOB_STATES = new Set([
      "queued", "running", "cancelling",
    ]);
    const TERMINAL_TRANSFORM_JOB_STATES = new Set([
      "cancelled", "failed", "done", "interrupted",
    ]);
    const CORRECTION_IMAGE_OUTPUT_KINDS = Object.freeze([
      "corrected-display", "ocr-ready", "thumbnail", "transform-manifest",
    ]);
    const CORRECTION_JOB_OUTPUT_KINDS =
      new Set([...CORRECTION_IMAGE_OUTPUT_KINDS, "ocr-proposal"]);
    const PORTABLE_JOB_VALUE_RE = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;

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

    function correctionTransformJob(value, command, expectedJobId = "") {
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
      const allowedInputKeys = new Set([
        "artifact_id", "artifact_revision", "source_revision", "source_sha256",
        "operation_id", "command_sha256", "dependent_assertions",
      ]);
      if (!inputKeys.every((key) => allowedInputKeys.has(key)) ||
          inputs.artifact_id !== command.artifact_id ||
          inputs.artifact_revision !== command.artifact_revision ||
          inputs.source_revision !== command.source_revision ||
          inputs.source_sha256 !== command.source_sha256 ||
          inputs.operation_id !== command.operation_id ||
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

    function correctionTransformTerminalResult(job, command) {
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
      const presentImageKinds = CORRECTION_IMAGE_OUTPUT_KINDS.filter(
        (kind) => byKind.has(kind),
      );
      const imageCommitted =
        presentImageKinds.length === CORRECTION_IMAGE_OUTPUT_KINDS.length;
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
      if (imageCommitted && CORRECTION_IMAGE_OUTPUT_KINDS.some(
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
        if (/ocr outcome recording failed/i.test(job.note)) {
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
        } else if (/ocr follow-up cancelled/i.test(job.note) ||
                   job.state === "interrupted") {
          ocrFollowup = {
            state: "cancelled",
            source: ocrSource,
            proposal_ref: "",
            failure: null,
          };
        } else if (/ocr follow-up failed/i.test(job.note)) {
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
                CORRECTION_IMAGE_OUTPUT_KINDS.map((kind) => Object.freeze({
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
      const pending = new Map();
      let timer = null;
      let polling = false;

      function emit(result, command) {
        for (const listener of [...listeners]) {
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
        if (timer != null || polling || !listeners.size || !pending.size) return;
        timer = schedule(() => {
          timer = null;
          void poll();
        }, delay);
      }

      async function poll() {
        if (polling || !listeners.size || !pending.size) return;
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
        if (pending.size && listeners.size) {
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

      function subscribeResults(listener) {
        if (typeof listener !== "function") {
          throw new TypeError("transform result listener is required");
        }
        listeners.add(listener);
        schedulePoll(25);
        return () => {
          listeners.delete(listener);
          if (!listeners.size && timer != null) {
            cancelSchedule(timer);
            timer = null;
          }
        };
      }

      return Object.freeze({
        subscribeResults,
        track,
      });
    }

    function correctionCommandPort(client, transformPolling = null) {
      const corrections = client && client.corrections;
      if (!corrections || typeof corrections !== "object") return null;
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
      return Object.keys(commands).length ? Object.freeze(commands) : null;
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

    function correctionBooksPort(client) {
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
        const index = await corrections.index({
          workspaceId: workspace.id,
          signal,
        });
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
          const index = await corrections.index({
            workspaceId: nextWorkspaceId,
            signal,
          });
          if (generation === workspaceRequestGeneration &&
              requestedWorkspaceId === nextWorkspaceId &&
              !(signal && signal.aborted)) {
            workspaceId = nextWorkspaceId;
          }
          return index;
        },
        getReview({ target, signal } = {}) {
          return assembleReview(target, signal);
        },
      };
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
      const portOptions = isPlainObject(options) ? options : {};
      const transformPolling = correctionTransformPollingPort(
        client,
        isPlainObject(portOptions.transformPolling)
          ? portOptions.transformPolling : {},
      );
      const commands = correctionCommandPort(client, transformPolling);
      const reviews = correctionReviewPort(client);
      const books = correctionBooksPort(client);

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
          cursor: cursor || null,
          limit,
        });
        return pageResult(
          response,
          response.annotations.map(decorateSpatialAnnotation),
          { includeTotal: true },
        );
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
            return listRasterGroup(args);
          },
          async get({ context, key, signal } = {}) {
            const parsed = parseCatalogKey(key);
            const itemId = contextValue(context, "itemId", "item_id");
            if (parsed.objectType === "raster-artifact") {
              return rasterDetail(context, parsed.id, signal);
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
          resolveRaster({
            itemId, artifactId, resourceRef,
          } = {}) {
            if (!resourceRef || !resourceRef.revision) {
              throw new TypeError("raster resource revision is required");
            }
            return Object.freeze({
              url: client.rasterArtifacts.resourceUrl({
                itemId,
                artifactId,
                revision: resourceRef.revision,
              }),
            });
          },
          readText() {
            return Promise.reject(capabilityError("paged text reader"));
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
      createCorrectionsEnginePorts,
      decorateRasterArtifact,
      decorateSpatialAnnotation,
    };
  });

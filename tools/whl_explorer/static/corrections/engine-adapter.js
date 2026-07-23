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

    function correctionCommandPort(client) {
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
        commands.queueTransform = ({
          command, signal,
        } = {}) => corrections.queueTransform({ command, signal });
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

    function createCorrectionsEnginePorts(engineClient) {
      const client = requireEngineClient(engineClient);
      const commands = correctionCommandPort(client);
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
      });
    }

    return {
      createCorrectionsEnginePorts,
      decorateRasterArtifact,
      decorateSpatialAnnotation,
    };
  });

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
      const decorated = {
        ...value,
        artifact_id: value && value.key && value.key.artifact_id,
        object_type: "raster-artifact",
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
      return Object.keys(commands).length ? Object.freeze(commands) : null;
    }

    function createCorrectionsEnginePorts(engineClient) {
      const client = requireEngineClient(engineClient);
      const commands = correctionCommandPort(client);

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

      return Object.freeze({ artifacts });
    }

    return {
      createCorrectionsEnginePorts,
      decorateRasterArtifact,
      decorateSpatialAnnotation,
    };
  });

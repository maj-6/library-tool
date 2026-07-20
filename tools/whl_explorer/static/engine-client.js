/*
 * Browser transport boundary for the Library Tool engine.
 *
 * Workbenches call semantic methods on EngineClient; only this file knows the
 * current Flask route layout, JSON envelope, conditional-write headers, or
 * multipart encoding.  The client is intentionally stateless.  Draft state,
 * caching, request generations, and user-facing error messages belong to the
 * workbench controller.
 */
(function installEngineClient(root, factory) {
  const api = factory();

  if (typeof module === "object" && module.exports) module.exports = api;

  // A classic script is used for now because app.js still owns globals.  Keep
  // the constructor available for injected test/alternate transports and one
  // shared browser instance for the current UI.
  if (root && root.window === root) {
    root.EngineClient = api.EngineClient;
    root.EngineClientError = api.EngineClientError;
    if (!root.engineClient && typeof root.fetch === "function") {
      root.engineClient = new api.EngineClient({
        transport: root.fetch.bind(root),
        formDataFactory: () => new root.FormData(),
      });
    }
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function engineClientFactory() {
  "use strict";

  function encodePart(value) {
    // encodeURIComponent deliberately leaves !'()* alone.  Encoding them too
    // keeps path components and query values unambiguous across transports.
    return encodeURIComponent(String(value)).replace(/[!'()*]/g, (char) =>
      `%${char.charCodeAt(0).toString(16).toUpperCase()}`);
  }

  function quoteRevision(value, name) {
    const revision = String(value || "");
    if (!revision) throw new TypeError(`${name} is required`);
    if (/[\u0000-\u001f\u007f"\\]/.test(revision)) {
      throw new TypeError(`${name} is not a valid revision token`);
    }
    return `"${revision}"`;
  }

  function quoteRecordRevision(value, name) {
    const revision = String(value || "");
    if (!/^[A-Za-z0-9][A-Za-z0-9._:+-]{0,511}$/.test(revision)) {
      throw new TypeError(`${name} is not a valid record revision`);
    }
    return `"${revision}"`;
  }

  function operationKey(value, name) {
    const key = String(value || "");
    if (!/^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(key)) {
      throw new TypeError(`${name} is required and must be a portable identifier`);
    }
    return key;
  }

  const PORTABLE_IDENTIFIER = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;

  function isPortableIdentifier(value) {
    return typeof value === "string" && PORTABLE_IDENTIFIER.test(value);
  }

  function portableIdentifier(value, name) {
    if (!isPortableIdentifier(value)) {
      throw new TypeError(`${name} is required and must be a portable identifier`);
    }
    return value;
  }

  function isLifecycleRevision(value, optional = false) {
    if (typeof value !== "string") return false;
    if (!value) return optional;
    return value.length <= 512 && value === value.trim() &&
      !/[\u0000-\u0020\u007f"\\]/.test(value) &&
      !/[\ud800-\udfff]/.test(value);
  }

  function quoteLifecycleRevision(value, name) {
    if (!isLifecycleRevision(value)) {
      throw new TypeError(`${name} is not a valid strong revision token`);
    }
    return `"${value}"`;
  }

  const TEXT_LAYER_FORBIDDEN_REVISION =
    /["\\]|\p{White_Space}|\p{Cc}|\p{Cf}|\p{Cs}/u;

  function isTextLayerRevision(value, optional = false) {
    if (typeof value !== "string") return false;
    if (!value) return optional;
    return Array.from(value).length <= 512 &&
      !TEXT_LAYER_FORBIDDEN_REVISION.test(value);
  }

  function quoteTextLayerRevision(value, name) {
    if (!isTextLayerRevision(value)) {
      throw new TypeError(`${name} is not a valid text-layer revision`);
    }
    return `"${value}"`;
  }

  function isObject(value) {
    return !!value && typeof value === "object" && !Array.isArray(value);
  }

  function hasExactKeys(value, expected) {
    if (!isObject(value)) return false;
    const keys = Object.keys(value);
    return keys.length === expected.length &&
      expected.every((key) => Object.prototype.hasOwnProperty.call(value, key));
  }

  const PROVIDER_ID = /^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$/;
  const PROVIDER_SECRET_ID = /^[a-z0-9][a-z0-9._-]{0,62}(?::[a-z0-9][a-z0-9._-]{0,62})+$/;
  const PROVIDER_SEMVER = /^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$/;
  const PROVIDER_LANGUAGE = /^(?:\*|[a-z]{2,8}(?:-[a-z0-9]{1,8})*)$/;
  const PROVIDER_REASON_MESSAGES = Object.freeze({
    "command-not-installed": "The command implementation is not installed.",
    "disabled": "The provider is disabled.",
    "health-unknown": "Provider health could not be determined.",
    "network-unavailable": "Required network access is unavailable.",
    "no-selection": "No provider has been selected.",
    "not-configured": "Required provider configuration is missing.",
    "probe-failed": "Provider health could not be determined.",
    "provider-degraded": "The provider reports degraded service.",
    "provider-incompatible": "The selected provider is incompatible.",
    "provider-not-installed": "The selected provider is not installed.",
    "provider-unavailable": "The selected provider is unavailable.",
    "remote-unreachable": "The remote provider could not be reached.",
    "runtime-unavailable": "The provider runtime is unavailable.",
    "secret-status-unknown": "Required credential status is unavailable.",
    "secret-unavailable": "A required credential is not configured.",
  });
  const PROVIDER_CONFIGURED_UNAVAILABLE_REASONS = new Set([
    "disabled", "health-unknown", "network-unavailable", "probe-failed",
    "provider-unavailable", "remote-unreachable", "runtime-unavailable",
  ]);
  const PROVIDER_UNCONFIGURED_UNAVAILABLE_REASONS = new Set([
    "health-unknown", "not-configured", "probe-failed",
    "secret-status-unknown", "secret-unavailable",
  ]);

  function isProviderId(value, optional = false) {
    return typeof value === "string" && (optional && value === "" ||
      PROVIDER_ID.test(value));
  }

  function isProviderSecretId(value) {
    return typeof value === "string" && value.length <= 255 &&
      PROVIDER_SECRET_ID.test(value);
  }

  function isSortedUnique(values, token = (value) => value) {
    if (!Array.isArray(values)) return false;
    let previous = null;
    for (const value of values) {
      const current = token(value);
      if (typeof current !== "string" ||
          previous !== null && current <= previous) return false;
      previous = current;
    }
    return true;
  }

  function isProviderCapability(value) {
    return hasExactKeys(value, ["id", "version"]) &&
      isProviderId(value.id) && Number.isSafeInteger(value.version) &&
      value.version >= 1;
  }

  function providerCapabilityToken(value) {
    return isProviderCapability(value) ?
      `${value.id}@${String(value.version).padStart(16, "0")}` : "";
  }

  function isProviderReason(value, optional = false) {
    if (value === null) return optional;
    return hasExactKeys(value, ["code", "message"]) &&
      Object.prototype.hasOwnProperty.call(PROVIDER_REASON_MESSAGES, value.code) &&
      value.message === PROVIDER_REASON_MESSAGES[value.code];
  }

  function isPositiveLimit(value) {
    return value === null || Number.isSafeInteger(value) && value >= 1;
  }

  function isProviderLimits(value) {
    return hasExactKeys(value, [
      "max_input_bytes", "max_output_bytes", "max_batch_items",
      "max_context_tokens", "max_output_tokens",
    ]) && Object.values(value).every(isPositiveLimit);
  }

  function isProviderStringList(values, { nonempty = false,
    language = false } = {}) {
    if (!Array.isArray(values) || nonempty && values.length === 0 ||
        !isSortedUnique(values)) return false;
    const valid = language ? (value) => typeof value === "string" &&
      PROVIDER_LANGUAGE.test(value) : isProviderId;
    if (!values.every(valid)) return false;
    return !language || !values.includes("*") || values.length === 1;
  }

  function isProviderTraits(value) {
    if (!hasExactKeys(value, [
      "execution", "network", "modes", "input_media", "output_media",
      "input_languages", "output_languages", "limits",
    ]) || !["local", "remote"].includes(value.execution) ||
        !["offline", "required"].includes(value.network) ||
        value.execution === "remote" && value.network !== "required" ||
        !isProviderStringList(value.modes, { nonempty: true }) ||
        !value.modes.every((mode) => ["batch", "streaming"].includes(mode)) ||
        !isProviderStringList(value.input_media, { nonempty: true }) ||
        !isProviderStringList(value.output_media, { nonempty: true }) ||
        !isProviderStringList(value.input_languages, { language: true }) ||
        !isProviderStringList(value.output_languages, { language: true }) ||
        !isProviderLimits(value.limits)) return false;
    return true;
  }

  function isProviderRow(value) {
    if (!hasExactKeys(value, [
      "id", "version", "capabilities", "traits",
      "required_secret_status_ids", "secret_statuses", "configured",
      "health", "available",
    ]) || !isProviderId(value.id) || !PROVIDER_SEMVER.test(value.version) ||
        !isSortedUnique(value.capabilities, providerCapabilityToken) ||
        value.capabilities.length === 0 ||
        !value.capabilities.every(isProviderCapability) ||
        !isProviderTraits(value.traits) ||
        !Array.isArray(value.required_secret_status_ids) ||
        !isSortedUnique(value.required_secret_status_ids) ||
        !value.required_secret_status_ids.every(isProviderSecretId) ||
        !Array.isArray(value.secret_statuses) ||
        value.secret_statuses.length !== value.required_secret_status_ids.length ||
        typeof value.configured !== "boolean" ||
        typeof value.available !== "boolean" ||
        !hasExactKeys(value.health, ["state", "reason"]) ||
        !["healthy", "degraded", "unavailable"].includes(value.health.state) ||
        !isProviderReason(value.health.reason, value.health.state === "healthy")) {
      return false;
    }
    for (let index = 0; index < value.secret_statuses.length; index += 1) {
      const status = value.secret_statuses[index];
      if (!hasExactKeys(status, ["id", "configured"]) ||
          status.id !== value.required_secret_status_ids[index] ||
          status.configured !== null && typeof status.configured !== "boolean") {
        return false;
      }
    }
    if (value.health.state === "healthy" && value.health.reason !== null) return false;
    if (value.health.state !== "healthy" && value.health.reason === null) return false;
    if (!value.configured && value.health.state !== "unavailable") return false;
    const reasonCode = value.health.reason && value.health.reason.code;
    if (value.health.state === "degraded" &&
        reasonCode !== "provider-degraded") return false;
    if (value.health.state === "unavailable" &&
        !(value.configured ? PROVIDER_CONFIGURED_UNAVAILABLE_REASONS :
          PROVIDER_UNCONFIGURED_UNAVAILABLE_REASONS).has(reasonCode)) return false;
    const unknownSecret = value.secret_statuses.some((status) =>
      status.configured === null);
    const missingSecret = value.secret_statuses.some((status) =>
      status.configured === false);
    if (unknownSecret && reasonCode !== "secret-status-unknown") return false;
    if (!unknownSecret && missingSecret &&
        reasonCode !== "secret-unavailable") return false;
    if (!unknownSecret && !missingSecret &&
        ["secret-status-unknown", "secret-unavailable"].includes(reasonCode)) {
      return false;
    }
    const available = value.configured &&
      ["healthy", "degraded"].includes(value.health.state);
    if (value.available !== available) return false;
    if (value.secret_statuses.some((status) => status.configured !== true) &&
        value.configured) return false;
    return true;
  }

  function isProviderSelectionRow(value, providers, executableCommands) {
    if (!hasExactKeys(value, [
      "capability", "user_provider_id", "default_provider_id",
      "selected_provider_id", "source", "command_available", "reason",
    ]) || !isProviderCapability(value.capability) ||
        !isProviderId(value.user_provider_id, true) ||
        !isProviderId(value.default_provider_id, true) ||
        !isProviderId(value.selected_provider_id, true) ||
        !["user", "default", "none"].includes(value.source) ||
        typeof value.command_available !== "boolean" ||
        !isProviderReason(value.reason, value.command_available)) return false;
    const selected = value.user_provider_id || value.default_provider_id;
    const source = value.user_provider_id ? "user" :
      value.default_provider_id ? "default" : "none";
    if (value.selected_provider_id !== selected || value.source !== source) return false;
    const provider = providers.get(selected);
    const token = providerCapabilityToken(value.capability);
    const compatible = !!provider && provider.capabilities.some((item) =>
      providerCapabilityToken(item) === token);
    let expectedReason = null;
    if (!selected) expectedReason = "no-selection";
    else if (!provider) expectedReason = "provider-not-installed";
    else if (!compatible) expectedReason = "provider-incompatible";
    else if (!executableCommands.has(token)) {
      expectedReason = "command-not-installed";
    }
    else if (!provider.available) {
      expectedReason = provider.health.reason ?
        provider.health.reason.code : "provider-unavailable";
    }
    const expectedAvailable = expectedReason === null;
    return value.command_available === expectedAvailable &&
      (expectedAvailable ? value.reason === null :
        value.reason !== null && value.reason.code === expectedReason);
  }

  function isProviderDiscovery(value) {
    if (!hasExactKeys(value, [
      "ok", "schema", "providers", "selections", "executable_commands",
      "available_commands",
    ]) || value.ok !== true || value.schema !== "librarytool.providers/1" ||
        !Array.isArray(value.providers) ||
        !isSortedUnique(value.providers, (provider) =>
          isProviderRow(provider) ? provider.id : "") ||
        !value.providers.every(isProviderRow)) return false;
    const providers = new Map(value.providers.map((provider) => [provider.id, provider]));
    if (!Array.isArray(value.executable_commands) ||
        !isSortedUnique(value.executable_commands, providerCapabilityToken) ||
        !value.executable_commands.every(isProviderCapability)) return false;
    const executableCommands = new Set(
      value.executable_commands.map(providerCapabilityToken));
    if (!Array.isArray(value.selections) ||
        !isSortedUnique(value.selections, (selection) =>
          isObject(selection) ? providerCapabilityToken(selection.capability) : "") ||
        !value.selections.every((selection) =>
          isProviderSelectionRow(
            selection, providers, executableCommands))) return false;
    const declaredCapabilities = new Set();
    for (const provider of value.providers) {
      for (const capability of provider.capabilities) {
        declaredCapabilities.add(providerCapabilityToken(capability));
      }
    }
    const selectedCapabilities = value.selections.map((selection) =>
      providerCapabilityToken(selection.capability));
    if (declaredCapabilities.size > selectedCapabilities.length ||
        ![...declaredCapabilities].every((token) =>
          selectedCapabilities.includes(token))) return false;
    if (![...executableCommands].every((token) =>
      selectedCapabilities.includes(token))) return false;
    if (!Array.isArray(value.available_commands) ||
        !isSortedUnique(value.available_commands, providerCapabilityToken) ||
        !value.available_commands.every(isProviderCapability)) return false;
    const expected = value.selections.filter((selection) =>
      selection.command_available).map((selection) =>
      providerCapabilityToken(selection.capability));
    const actual = value.available_commands.map(providerCapabilityToken);
    return expected.length === actual.length &&
      expected.every((token, index) => token === actual[index]);
  }

  function containsCommandFingerprint(value, seen = new Set()) {
    if (!value || typeof value !== "object") return false;
    if (seen.has(value)) return true;
    seen.add(value);
    if (Object.prototype.hasOwnProperty.call(value, "command_sha256")) {
      return true;
    }
    return Object.keys(value).some((key) =>
      containsCommandFingerprint(value[key], seen));
  }

  function containsCredentialField(value, seen = new Set()) {
    if (!value || typeof value !== "object") return false;
    if (seen.has(value)) return true;
    seen.add(value);
    if (Object.prototype.hasOwnProperty.call(value, "credential")) return true;
    return Object.keys(value).some((key) =>
      containsCredentialField(value[key], seen));
  }

  function isSecretStatus(value, secretId = null) {
    return hasExactKeys(value, ["id", "configured", "masked_hint", "revision"]) &&
      isPortableIdentifier(value.id) &&
      (secretId === null || value.id === secretId) &&
      typeof value.configured === "boolean" &&
      typeof value.masked_hint === "string" &&
      value.masked_hint === (value.configured ? "••••" : "") &&
      isLifecycleRevision(value.revision);
  }

  function isSecretReceipt(value, action, expected) {
    return hasExactKeys(value, [
      "action", "operation_id", "secret_id", "before", "after",
    ]) && value.action === action &&
      value.operation_id === expected.operationId &&
      value.secret_id === expected.secretId &&
      isSecretStatus(value.before, expected.secretId) &&
      isSecretStatus(value.after, expected.secretId) &&
      value.before.revision === expected.revision &&
      value.after.revision !== value.before.revision &&
      value.after.configured === (action === "replace") &&
      !containsCredentialField(value);
  }

  function isTextLayerSourceView(value) {
    if (!hasExactKeys(value, [
      "representation_id", "pinned_revision", "current_revision",
      "available", "status",
    ]) || !isPortableIdentifier(value.representation_id) ||
        !isTextLayerRevision(value.pinned_revision) ||
        !isTextLayerRevision(value.current_revision, true) ||
        typeof value.available !== "boolean" ||
        value.available !== !!value.current_revision) return false;
    const status = !value.available ? "unavailable" :
      value.current_revision === value.pinned_revision ? "current" : "stale";
    return value.status === status;
  }

  function isTextLayerProvenance(value) {
    return hasExactKeys(value, [
      "origin", "review_state", "provider_id", "model", "recipe_revision",
      "updated_at", "metadata",
    ]) && ["unknown", "machine", "human", "import", "derived"].includes(
      value.origin) &&
      ["unreviewed", "reviewed", "approved", "rejected"].includes(
        value.review_state) &&
      (value.provider_id === "" || isPortableIdentifier(value.provider_id)) &&
      typeof value.model === "string" &&
      isTextLayerRevision(value.recipe_revision, true) &&
      typeof value.updated_at === "string" && isObject(value.metadata);
  }

  function isTextLayerUnit(value) {
    return hasExactKeys(value, [
      "selector", "order", "label", "text", "provenance",
      "content_revision", "unit_revision",
    ]) && isPortableIdentifier(value.selector) &&
      Number.isSafeInteger(value.order) && value.order >= 0 &&
      typeof value.label === "string" && typeof value.text === "string" &&
      isTextLayerProvenance(value.provenance) &&
      isTextLayerRevision(value.content_revision) &&
      isTextLayerRevision(value.unit_revision);
  }

  function isTextLayerSourcePin(value) {
    return hasExactKeys(value, ["representation_id", "revision"]) &&
      isPortableIdentifier(value.representation_id) &&
      isTextLayerRevision(value.revision);
  }

  function isTextLayerSummary(value, itemId) {
    return hasExactKeys(value, [
      "item_id", "layer_id", "label", "kind", "language",
      "document_revision", "content_revision", "view_revision", "source",
      "unit_count",
    ]) && value.item_id === itemId && isPortableIdentifier(value.item_id) &&
      isPortableIdentifier(value.layer_id) && typeof value.label === "string" &&
      isPortableIdentifier(value.kind) && typeof value.language === "string" &&
      isTextLayerRevision(value.document_revision) &&
      isTextLayerRevision(value.content_revision) &&
      isTextLayerRevision(value.view_revision) &&
      isTextLayerSourceView(value.source) &&
      Number.isSafeInteger(value.unit_count) && value.unit_count >= 0;
  }

  function isTextLayerDocument(value, itemId, layerId) {
    if (!hasExactKeys(value, [
      "item_id", "layer_id", "label", "kind", "language", "source",
      "preamble", "units", "document_revision", "content_revision",
    ]) || value.item_id !== itemId || value.layer_id !== layerId ||
        !isPortableIdentifier(value.item_id) ||
        !isPortableIdentifier(value.layer_id) ||
        typeof value.label !== "string" || !isPortableIdentifier(value.kind) ||
        typeof value.language !== "string" ||
        !isTextLayerSourcePin(value.source) ||
        typeof value.preamble !== "string" || !Array.isArray(value.units) ||
        !value.units.every(isTextLayerUnit) ||
        !isTextLayerRevision(value.document_revision) ||
        !isTextLayerRevision(value.content_revision)) return false;
    const selectors = new Set(value.units.map((unit) => unit.selector));
    const orders = new Set(value.units.map((unit) => unit.order));
    return selectors.size === value.units.length && orders.size === value.units.length;
  }

  function isTextLayerView(value, itemId, layerId) {
    return hasExactKeys(value, ["document", "source", "view_revision"]) &&
      isTextLayerDocument(value.document, itemId, layerId) &&
      isTextLayerSourceView(value.source) &&
      value.document.source.representation_id ===
        value.source.representation_id &&
      value.document.source.revision === value.source.pinned_revision &&
      isTextLayerRevision(value.view_revision);
  }

  const MAX_TEXT_LAYER_PAGE_UNITS = 256;
  const MAX_TEXT_LAYER_PAGE_NUMBER = 100000;
  const MAX_TEXT_LAYER_UNITS = 100000;

  function isTextLayerUnitPage(value, expected) {
    if (!hasExactKeys(value, [
      "item_id", "layer_id", "document_revision", "content_revision",
      "source_revision", "source", "page", "next_page", "limit",
      "unit_count", "units", "has_more", "page_revision",
    ]) || value.item_id !== expected.itemId ||
        value.layer_id !== expected.layerId ||
        value.document_revision !== expected.documentRevision ||
        value.source_revision !== expected.sourceRevision ||
        value.page !== expected.page || value.limit !== expected.limit ||
        !isPortableIdentifier(value.item_id) ||
        !isPortableIdentifier(value.layer_id) ||
        !isTextLayerRevision(value.document_revision) ||
        !isTextLayerRevision(value.content_revision) ||
        !isTextLayerRevision(value.source_revision) ||
        !isTextLayerSourceView(value.source) ||
        value.source.pinned_revision !== value.source_revision ||
        !Number.isSafeInteger(value.page) || value.page < 1 ||
        value.page > MAX_TEXT_LAYER_PAGE_NUMBER ||
        (value.next_page !== null &&
          (!Number.isSafeInteger(value.next_page) || value.next_page < 2 ||
            value.next_page > MAX_TEXT_LAYER_PAGE_NUMBER)) ||
        !Number.isSafeInteger(value.limit) || value.limit < 1 ||
        value.limit > MAX_TEXT_LAYER_PAGE_UNITS ||
        !Number.isSafeInteger(value.unit_count) || value.unit_count < 0 ||
        value.unit_count > MAX_TEXT_LAYER_UNITS ||
        !Array.isArray(value.units) || value.units.length > value.limit ||
        value.units.length > value.unit_count ||
        !value.units.every(isTextLayerUnit) ||
        typeof value.has_more !== "boolean" ||
        !isTextLayerRevision(value.page_revision)) return false;

    const start = (value.page - 1) * value.limit;
    if ((value.unit_count === 0 && value.page !== 1) ||
        (value.unit_count > 0 && start >= value.unit_count) ||
        value.units.length !== Math.min(
          value.limit, Math.max(0, value.unit_count - start))) return false;

    const selectors = new Set();
    const orders = new Set();
    let priorOrder = -1;
    for (const unit of value.units) {
      if (selectors.has(unit.selector) || orders.has(unit.order) ||
          unit.order <= priorOrder) return false;
      selectors.add(unit.selector);
      orders.add(unit.order);
      priorOrder = unit.order;
    }
    const hasMore = start + value.units.length < value.unit_count;
    return value.has_more === hasMore &&
      value.next_page === (hasMore ? value.page + 1 : null);
  }

  function isTextLayerUnitReceipt(value, selector, expectedUnitRevision) {
    return hasExactKeys(value, [
      "selector", "before_unit_revision", "after_unit_revision",
      "before_content_revision", "after_content_revision",
    ]) && value.selector === selector && isPortableIdentifier(value.selector) &&
      isTextLayerRevision(value.before_unit_revision) &&
      value.before_unit_revision === expectedUnitRevision &&
      isTextLayerRevision(value.after_unit_revision) &&
      value.before_unit_revision !== value.after_unit_revision &&
      isTextLayerRevision(value.before_content_revision) &&
      isTextLayerRevision(value.after_content_revision);
  }

  function isTextLayerReplaceReceipt(value, expected) {
    return hasExactKeys(value, [
      "action", "operation_id", "item_id", "layer_id", "source_revision",
      "before_document_revision", "after_document_revision",
      "before_content_revision", "after_content_revision", "units",
    ]) && value.action === "replace-unit" &&
      value.operation_id === expected.operationId &&
      value.item_id === expected.itemId && value.layer_id === expected.layerId &&
      value.source_revision === expected.sourceRevision &&
      isPortableIdentifier(value.operation_id) &&
      isPortableIdentifier(value.item_id) && isPortableIdentifier(value.layer_id) &&
      isTextLayerRevision(value.source_revision) &&
      isTextLayerRevision(value.before_document_revision) &&
      isTextLayerRevision(value.after_document_revision) &&
      value.before_document_revision !== value.after_document_revision &&
      isTextLayerRevision(value.before_content_revision) &&
      isTextLayerRevision(value.after_content_revision) &&
      Array.isArray(value.units) && value.units.length === 1 &&
      isTextLayerUnitReceipt(
        value.units[0], expected.selector, expected.unitRevision) &&
      (value.before_content_revision !== value.after_content_revision) ===
        (value.units[0].before_content_revision !==
          value.units[0].after_content_revision);
  }

  function isItemTombstone(value) {
    if (!isObject(value) ||
        !isPortableIdentifier(value.tombstone_id) ||
        !isLifecycleRevision(value.revision) ||
        !["deleted", "restored"].includes(value.state) ||
        !isPortableIdentifier(value.item_id) ||
        !isLifecycleRevision(value.deleted_item_revision) ||
        !isLifecycleRevision(value.managed_tree_revision) ||
        !isLifecycleRevision(value.restored_item_revision, true)) return false;
    if (value.state === "deleted") return value.restored_item_revision === "";
    return !!value.restored_item_revision &&
      value.restored_item_revision !== value.deleted_item_revision;
  }

  function hasUniqueTombstoneIdentities(tombstones) {
    const tombstoneIds = new Set();
    const activelyDeletedItems = new Set();
    for (const tombstone of tombstones) {
      const tombstoneId = tombstone.tombstone_id.toLowerCase();
      if (tombstoneIds.has(tombstoneId)) return false;
      tombstoneIds.add(tombstoneId);
      if (tombstone.state !== "deleted") continue;
      const itemId = tombstone.item_id.toLowerCase();
      if (activelyDeletedItems.has(itemId)) return false;
      activelyDeletedItems.add(itemId);
    }
    return true;
  }

  function isLifecycleReceipt(receipt, action, expected) {
    if (!isObject(receipt) || receipt.action !== action ||
        Object.prototype.hasOwnProperty.call(receipt, "command_sha256") ||
        !isPortableIdentifier(receipt.operation_id) ||
        receipt.operation_id !== expected.operationId ||
        !isPortableIdentifier(receipt.item_id) ||
        !isLifecycleRevision(receipt.deleted_item_revision) ||
        !isLifecycleRevision(receipt.restored_item_revision, true) ||
        !isLifecycleRevision(receipt.managed_tree_revision) ||
        !isLifecycleRevision(receipt.tombstone_before_revision, true) ||
        !isItemTombstone(receipt.tombstone) ||
        receipt.tombstone.item_id !== receipt.item_id ||
        receipt.tombstone.deleted_item_revision !== receipt.deleted_item_revision ||
        receipt.tombstone.managed_tree_revision !== receipt.managed_tree_revision) {
      return false;
    }
    if (action === "delete") {
      return receipt.item_id === expected.itemId &&
        receipt.deleted_item_revision === expected.recordRevision &&
        receipt.managed_tree_revision === expected.managedTreeRevision &&
        receipt.restored_item_revision === "" &&
        receipt.tombstone_before_revision === "" &&
        receipt.tombstone.state === "deleted";
    }
    return receipt.tombstone.tombstone_id === expected.tombstoneId &&
      receipt.tombstone_before_revision === expected.tombstoneRevision &&
      receipt.tombstone_before_revision !== receipt.tombstone.revision &&
      receipt.tombstone.state === "restored" &&
      !!receipt.restored_item_revision &&
      receipt.tombstone.restored_item_revision === receipt.restored_item_revision;
  }

  function fallbackCode(status) {
    if (status === 409) return "conflict";
    if (status === 428) return "precondition-required";
    if (status === 404) return "not-found";
    if (status === 401 || status === 403) return "forbidden";
    if (status === 429) return "rate-limited";
    if (status >= 500) return "engine-unavailable";
    return "request-failed";
  }

  class EngineClientError extends Error {
    constructor(message, options = {}) {
      super(message || "Engine request failed");
      this.name = "EngineClientError";
      this.status = Number(options.status) || 0;
      this.code = options.code || fallbackCode(this.status);
      this.details = options.details || null;
      this.conflict = options.conflict || null;
      this.retryable = options.retryable != null
        ? !!options.retryable
        : this.status === 0 || this.status === 429 || this.status >= 500;
      this.method = options.method || "";
      this.url = options.url || "";
      this.body = options.body == null ? null : options.body;
      if (options.cause !== undefined) this.cause = options.cause;
    }
  }

  class EngineClient {
    constructor(options = {}) {
      if (typeof options.transport !== "function") {
        throw new TypeError("EngineClient requires an injected transport");
      }
      this._transport = options.transport;
      this._baseUrl = String(options.baseUrl || "/api").replace(/\/+$/, "");
      this._formDataFactory = options.formDataFactory || (() => {
        if (typeof FormData !== "function") {
          throw new TypeError("No FormData implementation is available");
        }
        return new FormData();
      });

      const pageImageUrl = (args) => this._pageImageUrl(args);
      this.pdf = Object.freeze({
        info: (args) => this._pdfInfo(args),
        words: (args) => this._pdfWords(args),
        pageImageUrl,
      });
      this.translations = Object.freeze({
        list: (args) => this._translationList(args),
        get: (args) => this._translationGet(args),
        replacePage: (args) => this._translationReplacePage(args),
      });
      this.textLayers = Object.freeze({
        list: (args) => this._textLayerList(args),
        get: (args) => this._textLayerGet(args),
        pageUnits: (args) => this._textLayerPageUnits(args),
        replaceUnit: (args) => this._textLayerReplaceUnit(args),
      });
      this.ocr = Object.freeze({
        layout: (args) => this._ocrLayout(args),
      });
      this.jobs = Object.freeze({
        list: (args) => this._jobsList(args),
        get: (args) => this._jobGet(args),
        cancel: (args) => this._jobCancel(args),
        events: (args) => this._jobEvents(args),
      });
      this.items = Object.freeze({
        list: (args) => this._itemsList(args),
        get: (args) => this._itemGet(args),
        create: (args) => this._itemCreate(args),
        update: (args) => this._itemUpdate(args),
        seedCompatibility: (args) => this._itemSeedCompatibility(args),
        lifecycle: (args) => this._itemLifecycle(args),
        delete: (args) => this._itemDelete(args),
        representations: (args) => this._itemRepresentations(args),
        attachRepresentation: (args) => this._representationAttach(args),
        replaceRepresentation: (args) => this._representationReplace(args),
        detachRepresentation: (args) => this._representationDetach(args),
        artifacts: (args) => this._itemArtifacts(args),
        readiness: (args) => this._itemReadiness(args),
      });
      this.itemTombstones = Object.freeze({
        list: (args) => this._itemTombstonesList(args),
        get: (args) => this._itemTombstoneGet(args),
        restore: (args) => this._itemTombstoneRestore(args),
      });
      this.secrets = Object.freeze({
        list: (args) => this._secretsList(args),
        get: (args) => this._secretGet(args),
        replace: (args) => this._secretReplace(args),
        clear: (args) => this._secretClear(args),
      });
      this.capabilities = (args) => this._capabilities(args);
      this.providers = Object.freeze({
        discover: (args) => this._providersDiscover(args),
      });

      const pages = Object.freeze({
        get: (args) => this._replicaPageGet(args),
        save: (args) => this._replicaPageSave(args),
        recompile: (args) => this._replicaPageRecompile(args),
        // Semantic alias for the scan raster used by the page editor.  The
        // preview can use pdf.pageImageUrl directly.
        imageUrl: pageImageUrl,
      });
      const proposals = Object.freeze({
        decide: (args) => this._replicaProposalDecide(args),
      });
      const detection = Object.freeze({
        start: (args) => this._replicaDetectionStart(args),
      });
      const templates = Object.freeze({
        list: (args) => this._replicaTemplatesList(args),
        saveFromPage: (args) => this._replicaTemplateSaveFromPage(args),
        apply: (args) => this._replicaTemplateApply(args),
        outliers: (args) => this._replicaTemplateOutliers(args),
      });
      const styles = Object.freeze({
        get: (args) => this._replicaStylesGet(args),
        save: (args) => this._replicaStylesSave(args),
        reset: (args) => this._replicaStylesReset(args),
      });
      const instructions = Object.freeze({
        get: (args) => this._replicaInstructionsGet(args),
        save: (args) => this._replicaInstructionsSave(args),
      });
      const figures = Object.freeze({
        rework: (args) => this._replicaFigureRework(args),
        imageUrl: (args) => this._replicaFigureImageUrl(args),
      });
      const packages = Object.freeze({
        open: (args) => this._replicaPackageOpen(args),
        import: (args) => this._replicaPackageImport(args),
        exportUrl: (args) => this._replicaPackageExportUrl(args),
      });
      this.replica = Object.freeze({
        pages, proposals, detection, templates, styles, instructions, figures,
        packages,
        printUrl: (args) => this._replicaPrintUrl(args),
      });
    }

    _query(values) {
      const pairs = [];
      for (const [key, value] of Object.entries(values || {})) {
        if (value === undefined || value === null || value === "") continue;
        pairs.push(`${encodePart(key)}=${encodePart(value)}`);
      }
      return pairs.length ? `?${pairs.join("&")}` : "";
    }

    _url(path, query) {
      return `${this._baseUrl}${path}${this._query(query)}`;
    }

    _buildPath(bookId, suffix) {
      return `/builds/${encodePart(bookId)}/${suffix}`;
    }

    async _requestJson(method, path, options = {}) {
      const url = this._url(path, options.query);
      const headers = { Accept: "application/json", ...(options.headers || {}) };
      const init = { method, headers };
      if (options.signal) init.signal = options.signal;
      if (options.cache) init.cache = options.cache;
      if (options.multipart !== undefined) {
        init.body = options.multipart;
      } else if (options.body !== undefined) {
        headers["Content-Type"] = "application/json";
        init.body = JSON.stringify(options.body);
      }

      let response;
      try {
        response = await this._transport(url, init);
      } catch (cause) {
        if (cause instanceof EngineClientError) throw cause;
        const aborted = cause && cause.name === "AbortError";
        throw new EngineClientError(aborted ? "Engine request aborted" :
          (cause && cause.message || "Unable to reach the engine"), {
          status: 0, code: aborted ? "aborted" : "network-error",
          retryable: !aborted, method, url, cause,
        });
      }

      const status = Number(response && response.status) || 0;
      const responseOk = response && typeof response.ok === "boolean"
        ? response.ok : status >= 200 && status < 300;
      let body = null;
      try {
        if (!response || typeof response.json !== "function") {
          throw new TypeError("Transport response has no json() method");
        }
        body = await response.json();
      } catch (cause) {
        if (responseOk && options.allowEmpty) return { ok: true };
        throw new EngineClientError(responseOk
          ? "Engine returned an invalid JSON response"
          : `Engine request failed (${status || "no status"})`, {
          status, code: responseOk ? "invalid-response" : fallbackCode(status),
          retryable: status === 429 || status >= 500,
          method, url, body: null, cause,
        });
      }

      if (!responseOk || !body || body.ok === false) {
        const payload = body && typeof body === "object" ? body : {};
        throw new EngineClientError(payload.error || payload.message ||
          `Engine request failed (${status || "no status"})`, {
          status,
          code: payload.code || payload.error_code || fallbackCode(status),
          details: payload.details || payload.conflict || null,
          conflict: payload.conflict || null,
          retryable: payload.retryable,
          method, url, body,
        });
      }
      return options.includeStatus ? { body, status } : body;
    }

    _pdfInfo({ path, signal } = {}) {
      return this._requestJson("GET", "/pdf/info", {
        query: { path }, signal,
      });
    }

    _capabilities({ signal } = {}) {
      return this._requestJson("GET", "/v1/capabilities", { signal });
    }

    _providersDiscover({ signal } = {}) {
      const path = "/v1/providers";
      return this._requestJson("GET", path, {
        signal, cache: "no-cache", includeStatus: true,
      }).then(({ body, status }) => {
        if (status !== 200 || !isProviderDiscovery(body) ||
            containsCommandFingerprint(body)) {
          this._invalidResponse(
            "Engine returned invalid provider discovery",
            "GET", path, body, undefined, status);
        }
        return body;
      });
    }

    async _secretsList({ signal } = {}) {
      const path = "/v1/secrets";
      const { body, status } = await this._requestJson("GET", path, {
        signal, cache: "no-store", includeStatus: true,
      });
      const health = body && body.health;
      if (status !== 200 || !hasExactKeys(body, [
        "ok", "schema", "health", "secrets",
      ]) || body.ok !== true ||
          body.schema !== "librarytool.secret-status-list/1" ||
          !hasExactKeys(health, ["available", "state", "writable"]) ||
          typeof health.available !== "boolean" ||
          typeof health.state !== "string" ||
          typeof health.writable !== "boolean" ||
          !Array.isArray(body.secrets) ||
          !body.secrets.every((item) => isSecretStatus(item)) ||
          new Set(body.secrets.map((item) => item.id)).size !==
            body.secrets.length || containsCredentialField(body)) {
        this._invalidResponse(
          "Engine returned an invalid secret status list",
          "GET", path, null, undefined, status);
      }
      return body;
    }

    async _secretGet({ secretId, signal } = {}) {
      const id = portableIdentifier(secretId, "secretId");
      const path = `/v1/secrets/${encodePart(id)}`;
      const { body, status } = await this._requestJson("GET", path, {
        signal, cache: "no-store", includeStatus: true,
      });
      if (status !== 200 || !hasExactKeys(body, ["ok", "schema", "status"]) ||
          body.ok !== true || body.schema !== "librarytool.secret-status/1" ||
          !isSecretStatus(body.status, id) || containsCredentialField(body)) {
        this._invalidResponse(
          "Engine returned an invalid secret status",
          "GET", path, null, undefined, status);
      }
      return body;
    }

    async _secretReplace({ secretId, revision, credential,
      idempotencyKey, signal } = {}) {
      if (typeof credential !== "string" || !credential) {
        throw new TypeError("credential is required");
      }
      return this._secretMutation({
        action: "replace", secretId, revision, credential,
        idempotencyKey, signal,
      });
    }

    _secretClear({ secretId, revision, idempotencyKey, signal } = {}) {
      return this._secretMutation({
        action: "clear", secretId, revision, idempotencyKey, signal,
      });
    }

    async _secretMutation({ action, secretId, revision, credential,
      idempotencyKey, signal }) {
      const id = portableIdentifier(secretId, "secretId");
      const operationId = operationKey(idempotencyKey, "idempotencyKey");
      const path = `/v1/secrets/${encodePart(id)}`;
      const method = action === "replace" ? "PUT" : "DELETE";
      const { body, status } = await this._requestJson(method, path, {
        headers: {
          "Idempotency-Key": operationId,
          "If-Match": quoteLifecycleRevision(revision, "revision"),
        },
        body: action === "replace" ? { credential } : undefined,
        signal, cache: "no-store", includeStatus: true,
      });
      if (status !== 200 || !hasExactKeys(body, [
        "ok", "schema", "replayed", "receipt",
      ]) || body.ok !== true ||
          body.schema !== "librarytool.secret-mutation-receipt/1" ||
          typeof body.replayed !== "boolean" ||
          !isSecretReceipt(body.receipt, action, {
            operationId, secretId: id, revision,
          }) || containsCredentialField(body)) {
        this._invalidResponse(
          "Engine returned an invalid secret mutation receipt",
          method, path, null, undefined, status);
      }
      return body;
    }

    _itemsList({ includeBuildCompatibility = false, signal } = {}) {
      return this._requestJson("GET", "/v1/items", {
        query: {
          projection: includeBuildCompatibility ? "build-workbench" : undefined,
        },
        signal,
      });
    }

    _itemGet({ itemId, includeBuildCompatibility = false, signal } = {}) {
      return this._requestJson("GET", `/v1/items/${encodePart(itemId)}`, {
        query: {
          projection: includeBuildCompatibility ? "build-workbench" : undefined,
        },
        signal,
      });
    }

    _itemCreate({ item, idempotencyKey, signal } = {}) {
      return this._requestJson("POST", "/v1/items", {
        headers: {
          "Idempotency-Key": operationKey(idempotencyKey, "idempotencyKey"),
        },
        body: { item },
        signal,
      });
    }

    // Transitional storage-only acquisition fields are not part of the
    // portable ItemDraft. Keep their legacy route and CAS spelling contained
    // here so workbench code still depends on a semantic transport method.
    _itemSeedCompatibility({ itemId, compatibility, recordRevision,
      signal } = {}) {
      const revision = String(recordRevision || "");
      if (!/^[A-Za-z0-9][A-Za-z0-9._:+-]{0,511}$/.test(revision)) {
        throw new TypeError("recordRevision is not a valid record revision");
      }
      if (!isObject(compatibility) ||
          Object.keys(compatibility).some((key) =>
            !["extra", "images", "capture_id"].includes(key)) ||
          (Object.prototype.hasOwnProperty.call(compatibility, "extra") &&
            !isObject(compatibility.extra)) ||
          (Object.prototype.hasOwnProperty.call(compatibility, "images") &&
            (!Array.isArray(compatibility.images) ||
              compatibility.images.some((value) =>
                typeof value !== "string"))) ||
          (Object.prototype.hasOwnProperty.call(compatibility, "capture_id") &&
            typeof compatibility.capture_id !== "string")) {
        throw new TypeError("compatibility must contain only acquisition fields");
      }
      return this._requestJson(
        "PATCH", `/builds/${encodePart(itemId)}`, {
          body: { ...compatibility, expect_updated_at: revision },
          signal,
        });
    }

    _itemUpdate({ itemId, bookId, patch, recordRevision, idempotencyKey,
      signal } = {}) {
      const id = itemId != null ? itemId : bookId;
      return this._requestJson("PATCH", `/v1/items/${encodePart(id)}`, {
        headers: {
          "Idempotency-Key": operationKey(idempotencyKey, "idempotencyKey"),
          "If-Record-Match": quoteRecordRevision(
            recordRevision, "recordRevision"),
        },
        body: { patch },
        signal,
      });
    }

    _invalidResponse(message, method, path, body, query, status = 200) {
      throw new EngineClientError(message, {
        status,
        code: "invalid-response",
        retryable: true,
        method,
        url: this._url(path, query),
        body,
      });
    }

    _invalidLifecycleResponse(message, method, path, body, query, status = 200) {
      this._invalidResponse(message, method, path, body, query, status);
    }

    async _itemLifecycle({ itemId, signal } = {}) {
      const id = portableIdentifier(itemId, "itemId");
      const path = `/v1/items/${encodePart(id)}/lifecycle`;
      const { body, status } = await this._requestJson("GET", path, {
        signal, cache: "no-cache", includeStatus: true,
      });
      if (status !== 200 || !isObject(body) || body.ok !== true ||
          body.schema !== "librarytool.item-lifecycle-state/1" ||
          body.state !== "live" || body.item_id !== id ||
          !isLifecycleRevision(body.item_revision) ||
          !isLifecycleRevision(body.managed_tree_revision) ||
          !isLifecycleRevision(body.revision)) {
        this._invalidLifecycleResponse(
          "Engine returned an invalid item lifecycle state",
          "GET", path, body, undefined, status);
      }
      return body;
    }

    async _itemDelete({ itemId, recordRevision, managedTreeRevision,
      idempotencyKey, signal } = {}) {
      const id = portableIdentifier(itemId, "itemId");
      const operationId = portableIdentifier(
        idempotencyKey, "idempotencyKey");
      if (typeof recordRevision !== "string") {
        throw new TypeError("recordRevision is not a valid record revision");
      }
      const path = `/v1/items/${encodePart(id)}`;
      const { body, status } = await this._requestJson("DELETE", path, {
        headers: {
          "Idempotency-Key": operationId,
          "If-Record-Match": quoteRecordRevision(
            recordRevision, "recordRevision"),
          "If-Managed-Tree-Match": quoteLifecycleRevision(
            managedTreeRevision, "managedTreeRevision"),
        },
        signal,
        cache: "no-store",
        includeStatus: true,
      });
      if (status !== 200 || !isObject(body) || body.ok !== true ||
          body.schema !== "librarytool.item-lifecycle-receipt/1" ||
          Object.prototype.hasOwnProperty.call(body, "command_sha256") ||
          typeof body.replayed !== "boolean" ||
          !isLifecycleReceipt(body.receipt, "delete", {
            operationId,
            itemId: id,
            recordRevision,
            managedTreeRevision,
          })) {
        this._invalidLifecycleResponse(
          "Engine returned an invalid item deletion receipt",
          "DELETE", path, body, undefined, status);
      }
      return body;
    }

    async _itemTombstonesList({ state, signal } = {}) {
      const wantedState = state == null || state === "" ? "" : state;
      if (typeof wantedState !== "string" ||
          (wantedState && !["deleted", "restored"].includes(wantedState))) {
        throw new TypeError("state must be deleted or restored");
      }
      const path = "/v1/item-tombstones";
      const query = { state: wantedState || undefined };
      const { body, status } = await this._requestJson("GET", path, {
        query, signal, cache: "no-cache", includeStatus: true,
      });
      if (status !== 200 || !isObject(body) || body.ok !== true ||
          body.schema !== "librarytool.item-tombstone-list/1" ||
          !Array.isArray(body.tombstones) ||
          !body.tombstones.every(isItemTombstone) ||
          (wantedState && !body.tombstones.every(
            (tombstone) => tombstone.state === wantedState)) ||
          !hasUniqueTombstoneIdentities(body.tombstones)) {
        this._invalidLifecycleResponse(
          "Engine returned an invalid item tombstone list",
          "GET", path, body, query, status);
      }
      return body;
    }

    async _itemTombstoneGet({ tombstoneId, signal } = {}) {
      const id = portableIdentifier(tombstoneId, "tombstoneId");
      const path = `/v1/item-tombstones/${encodePart(id)}`;
      const { body, status } = await this._requestJson("GET", path, {
        signal, cache: "no-cache", includeStatus: true,
      });
      if (status !== 200 || !isObject(body) || body.ok !== true ||
          body.schema !== "librarytool.item-tombstone/1" ||
          !isItemTombstone(body.tombstone) ||
          body.tombstone.tombstone_id !== id) {
        this._invalidLifecycleResponse(
          "Engine returned an invalid item tombstone",
          "GET", path, body, undefined, status);
      }
      return body;
    }

    async _itemTombstoneRestore({ tombstoneId, tombstoneRevision,
      idempotencyKey, signal } = {}) {
      const id = portableIdentifier(tombstoneId, "tombstoneId");
      const operationId = portableIdentifier(
        idempotencyKey, "idempotencyKey");
      const path = `/v1/item-tombstones/${encodePart(id)}/restore`;
      const { body, status } = await this._requestJson("POST", path, {
        headers: {
          "Idempotency-Key": operationId,
          "If-Tombstone-Match": quoteLifecycleRevision(
            tombstoneRevision, "tombstoneRevision"),
        },
        signal,
        cache: "no-store",
        includeStatus: true,
      });
      const expectedStatus = body && body.replayed === true ? 200 : 201;
      if (status !== expectedStatus || !isObject(body) || body.ok !== true ||
          body.schema !== "librarytool.item-lifecycle-receipt/1" ||
          Object.prototype.hasOwnProperty.call(body, "command_sha256") ||
          typeof body.replayed !== "boolean" ||
          !isLifecycleReceipt(body.receipt, "restore", {
            operationId,
            tombstoneId: id,
            tombstoneRevision,
          })) {
        this._invalidLifecycleResponse(
          "Engine returned an invalid item restoration receipt",
          "POST", path, body, undefined, status);
      }
      return body;
    }

    _itemRepresentations({ itemId, signal } = {}) {
      return this._requestJson(
        "GET", `/v1/items/${encodePart(itemId)}/representations`, { signal });
    }

    _representationPut({ itemId, representationId, representation,
      recordRevision, representationRevision, idempotencyKey, signal } = {}) {
      const headers = {
        "Idempotency-Key": operationKey(idempotencyKey, "idempotencyKey"),
        "If-Record-Match": quoteRecordRevision(
          recordRevision, "recordRevision"),
      };
      if (representationRevision != null) {
        headers["If-Representation-Match"] = quoteRecordRevision(
          representationRevision, "representationRevision");
      }
      return this._requestJson(
        "PUT",
        `/v1/items/${encodePart(itemId)}/representations/` +
          encodePart(representationId),
        { headers, body: { representation }, signal });
    }

    _representationAttach(args = {}) {
      if (args.representationRevision != null) {
        throw new TypeError(
          "attachRepresentation does not accept representationRevision");
      }
      return this._representationPut(args);
    }

    _representationReplace(args = {}) {
      if (!args.representationRevision) {
        throw new TypeError("representationRevision is required");
      }
      return this._representationPut(args);
    }

    _representationDetach({ itemId, representationId, recordRevision,
      representationRevision, idempotencyKey, signal } = {}) {
      return this._requestJson(
        "DELETE",
        `/v1/items/${encodePart(itemId)}/representations/` +
          encodePart(representationId),
        {
          headers: {
            "Idempotency-Key": operationKey(idempotencyKey, "idempotencyKey"),
            "If-Record-Match": quoteRecordRevision(
              recordRevision, "recordRevision"),
            "If-Representation-Match": quoteRecordRevision(
              representationRevision, "representationRevision"),
          },
          signal,
        });
    }

    _itemArtifacts({ itemId, signal } = {}) {
      return this._requestJson(
        "GET", `/v1/items/${encodePart(itemId)}/artifacts`, { signal });
    }

    _itemReadiness({ itemId, signal } = {}) {
      return this._requestJson(
        "GET", `/v1/items/${encodePart(itemId)}/readiness`, { signal });
    }

    _jobsList({ state, kind, itemId, signal } = {}) {
      return this._requestJson("GET", "/v1/jobs", {
        query: {
          state: Array.isArray(state) ? state.join(",") : state,
          kind: Array.isArray(kind) ? kind.join(",") : kind,
          item_id: itemId,
        },
        signal,
      });
    }

    _jobGet({ jobId, signal } = {}) {
      return this._requestJson(
        "GET", `/v1/jobs/${encodePart(jobId)}`, { signal });
    }

    _jobCancel({ jobId, signal } = {}) {
      return this._requestJson(
        "POST", `/v1/jobs/${encodePart(jobId)}/cancel`, { signal });
    }

    _jobEvents({ after, limit, signal } = {}) {
      return this._requestJson("GET", "/v1/job-events", {
        query: { after, limit }, signal,
      });
    }

    _pdfWords({ path, page, bookId, signal } = {}) {
      return this._requestJson("GET", "/pdf/words", {
        query: { path, page, build_id: bookId }, signal,
      });
    }

    _pageImageUrl({ path, pdfPath, page, width } = {}) {
      return this._url("/pdf/pageimg", {
        path: path != null ? path : pdfPath, page, w: width,
      });
    }

    _ocrLayout({ bookId, signal } = {}) {
      return this._requestJson(
        "GET", this._buildPath(bookId, "ocr-layout"), { signal });
    }

    _replicaTemplatesList({ bookId, sourceId, signal } = {}) {
      return this._requestJson("GET", this._buildPath(bookId, "ocr-templates"), {
        query: { src: sourceId }, signal,
      });
    }

    _replicaPageGet({ bookId, sourceId, page, signal } = {}) {
      return this._requestJson("GET", this._buildPath(bookId, "ocr-regions"), {
        query: { src: sourceId, page }, signal,
      });
    }

    _replicaPageSave({ bookId, sourceId, page, revision, record, signal } = {}) {
      const value = record || {};
      return this._requestJson("PUT", this._buildPath(bookId, "ocr-regions"), {
        headers: { "If-Match": quoteRevision(revision, "revision") },
        body: {
          src: sourceId, page,
          doc: value.doc, dims: value.dims, ext: value.ext,
          state: value.state, items: value.items,
          expect_revision: revision,
        },
        signal,
      });
    }

    _replicaProposalDecide({ bookId, sourceId, page, action, revision,
      proposalRevision, signal } = {}) {
      return this._requestJson("POST",
        this._buildPath(bookId, "ocr-region-proposals"), {
          headers: {
            "If-Match": quoteRevision(revision, "revision"),
            "If-Proposal-Match": quoteRevision(
              proposalRevision, "proposalRevision"),
          },
          body: {
            src: sourceId, page, action,
            expect_revision: revision,
            expect_proposal_revision: proposalRevision,
          },
          signal,
        });
    }

    _replicaPageRecompile({ bookId, sourceId, layer, signal } = {}) {
      const body = { src: sourceId };
      if (layer === "norm") body.layer = "norm";
      return this._requestJson("POST",
        this._buildPath(bookId, "ocr-regions/recompile"), { body, signal });
    }

    _replicaTemplateSaveFromPage({ bookId, sourceId, name, page, signal } = {}) {
      return this._requestJson("PUT", this._buildPath(bookId, "ocr-templates"), {
        body: { src: sourceId, name, from_page: page }, signal,
      });
    }

    _replicaDetectionStart({ bookId, sourceId, page, revision,
      provider = "automatic", idempotencyKey, signal } = {}) {
      return this._requestJson("POST",
        `/v1/items/${encodePart(bookId)}/replica/region-detection-jobs`, {
          headers: { "If-Match": quoteRevision(revision, "revision") },
          body: {
            source_id: sourceId, page, provider,
            expect_revision: revision,
            idempotency_key: idempotencyKey,
          },
          signal,
        });
    }

    _replicaTemplateApply({ bookId, sourceId, name, pages, signal } = {}) {
      return this._requestJson("POST",
        this._buildPath(bookId, "ocr-templates/apply"), {
          body: { src: sourceId, name, pages }, signal,
        });
    }

    _replicaTemplateOutliers({ bookId, sourceId, name, signal } = {}) {
      return this._requestJson("POST",
        this._buildPath(bookId, "ocr-templates/outliers"), {
          body: { src: sourceId, name }, signal,
        });
    }

    _replicaFigureRework({ bookId, sourceId, figure, prompt, signal } = {}) {
      return this._requestJson("POST", this._buildPath(bookId, "rework-figure"), {
        body: { src: sourceId, figure, prompt }, signal,
      });
    }

    _replicaFigureImageUrl({ bookId, name } = {}) {
      return this._url(this._buildPath(bookId,
        `ocr/images/${encodePart(name)}`));
    }

    _replicaStylesGet({ bookId, signal } = {}) {
      return this._requestJson("GET", this._buildPath(bookId, "replica-style"), {
        signal,
      });
    }

    _replicaStylesSave({ bookId, styles, signal } = {}) {
      return this._requestJson("PUT", this._buildPath(bookId, "replica-style"), {
        body: { styles }, signal,
      });
    }

    _replicaStylesReset({ bookId, signal } = {}) {
      return this._requestJson("DELETE",
        this._buildPath(bookId, "replica-style"), {
          signal, allowEmpty: true,
        });
    }

    _replicaInstructionsGet({ bookId, signal } = {}) {
      return this._requestJson("GET",
        this._buildPath(bookId, "replica-instructions"), { signal });
    }

    _replicaInstructionsSave({ bookId, text, signal } = {}) {
      return this._requestJson("PUT",
        this._buildPath(bookId, "replica-instructions"), {
          body: { text }, signal,
        });
    }

    _translationList({ itemId, bookId, signal } = {}) {
      const id = itemId != null ? itemId : bookId;
      return this._requestJson("GET",
        `/v1/items/${encodePart(id)}/translations`, { signal });
    }

    _translationGet({ itemId, bookId, translationId, signal } = {}) {
      const id = itemId != null ? itemId : bookId;
      return this._requestJson("GET",
        `/v1/items/${encodePart(id)}/translations/${encodePart(translationId)}`,
        { signal });
    }

    _translationReplacePage({ itemId, bookId, translationId, selector, text,
      documentRevision, sourceRevision, signal } = {}) {
      const id = itemId != null ? itemId : bookId;
      return this._requestJson("PUT",
        `/v1/items/${encodePart(id)}/translations/${encodePart(translationId)}` +
        `/pages/${encodePart(selector)}`, {
          headers: {
            "If-Document-Match": quoteRevision(
              documentRevision, "documentRevision"),
            "If-Source-Match": quoteRevision(sourceRevision, "sourceRevision"),
          },
          body: {
            text,
            expected_document_revision: documentRevision,
            expected_source_revision: sourceRevision,
          },
          signal,
        });
    }

    _textLayerList({ itemId, signal } = {}) {
      const item = portableIdentifier(itemId, "itemId");
      const path = `/v1/items/${encodePart(item)}/text-layers`;
      return this._requestJson("GET", path, {
        signal, cache: "no-cache", includeStatus: true,
      }).then(({ body, status }) => {
        const valid = status === 200 && hasExactKeys(body, [
          "ok", "schema", "item_id", "text_layers", "revision",
        ]) && body.ok === true &&
          body.schema === "librarytool.text-layer-summaries/1" &&
          body.item_id === item && Array.isArray(body.text_layers) &&
          body.text_layers.every((value) => isTextLayerSummary(value, item)) &&
          new Set(body.text_layers.map((value) => value.layer_id)).size ===
            body.text_layers.length && isTextLayerRevision(body.revision) &&
          !containsCommandFingerprint(body);
        if (!valid) {
          this._invalidResponse(
            "Engine returned an invalid text-layer collection",
            "GET", path, body, undefined, status);
        }
        return body;
      });
    }

    _textLayerGet({ itemId, layerId, signal } = {}) {
      const item = portableIdentifier(itemId, "itemId");
      const layer = portableIdentifier(layerId, "layerId");
      const path =
        `/v1/items/${encodePart(item)}/text-layers/${encodePart(layer)}`;
      return this._requestJson("GET", path, {
        signal, cache: "no-cache", includeStatus: true,
      }).then(({ body, status }) => {
        if (status !== 200 || !hasExactKeys(body, [
          "ok", "schema", "text_layer",
        ]) || body.ok !== true ||
            body.schema !== "librarytool.text-layer/1" ||
            !isTextLayerView(body.text_layer, item, layer) ||
            containsCommandFingerprint(body)) {
          this._invalidResponse(
            "Engine returned an invalid text-layer detail",
            "GET", path, body, undefined, status);
        }
        return body;
      });
    }

    _textLayerPageUnits({ itemId, layerId, documentRevision, sourceRevision,
      page = 1, limit, signal } = {}) {
      const item = portableIdentifier(itemId, "itemId");
      const layer = portableIdentifier(layerId, "layerId");
      if (!Number.isSafeInteger(page) || page < 1 ||
          page > MAX_TEXT_LAYER_PAGE_NUMBER) {
        throw new TypeError(
          `page must be an integer from 1 to ${MAX_TEXT_LAYER_PAGE_NUMBER}`);
      }
      if (!Number.isSafeInteger(limit) || limit < 1 ||
          limit > MAX_TEXT_LAYER_PAGE_UNITS) {
        throw new TypeError(
          `limit must be an integer from 1 to ${MAX_TEXT_LAYER_PAGE_UNITS}`);
      }
      const document = quoteTextLayerRevision(
        documentRevision, "documentRevision");
      const source = quoteTextLayerRevision(sourceRevision, "sourceRevision");
      const path =
        `/v1/items/${encodePart(item)}/text-layers/${encodePart(layer)}/units`;
      return this._requestJson("GET", path, {
        headers: {
          "If-Document-Match": document,
          "If-Source-Match": source,
        },
        query: { page, limit },
        signal,
        cache: "no-cache",
        includeStatus: true,
      }).then(({ body, status }) => {
        if (status !== 200 || !hasExactKeys(body, [
          "ok", "schema", "page",
        ]) || body.ok !== true ||
            body.schema !== "librarytool.text-layer-unit-page/1" ||
            !isTextLayerUnitPage(body.page, {
              itemId: item,
              layerId: layer,
              documentRevision,
              sourceRevision,
              page,
              limit,
            }) || containsCommandFingerprint(body)) {
          this._invalidResponse(
            "Engine returned an invalid text-layer unit page",
            "GET", path, body, undefined, status);
        }
        return body;
      });
    }

    _textLayerReplaceUnit({ itemId, layerId, selector, text, provenance,
      unitRevision, sourceRevision, idempotencyKey, signal } = {}) {
      const item = portableIdentifier(itemId, "itemId");
      const layer = portableIdentifier(layerId, "layerId");
      const unit = portableIdentifier(selector, "selector");
      if (typeof text !== "string") throw new TypeError("text must be a string");
      if (!isTextLayerProvenance(provenance)) {
        throw new TypeError("provenance must be a complete object");
      }
      const operationId = operationKey(idempotencyKey, "idempotencyKey");
      const source = quoteTextLayerRevision(
        sourceRevision, "sourceRevision");
      const path =
        `/v1/items/${encodePart(item)}/text-layers/${encodePart(layer)}` +
        `/units/${encodePart(unit)}`;
      return this._requestJson("PUT", path, {
          headers: {
            "Idempotency-Key": operationId,
            "If-Unit-Match": quoteTextLayerRevision(
              unitRevision, "unitRevision"),
            "If-Source-Match": source,
          },
          body: { replacement: { text, provenance } },
          cache: "no-store",
          signal,
          includeStatus: true,
        }).then(({ body, status }) => {
        if (status !== 200 || !hasExactKeys(body, [
          "ok", "schema", "replayed", "receipt",
        ]) || body.ok !== true ||
            body.schema !== "librarytool.text-layer-mutation-receipt/1" ||
            typeof body.replayed !== "boolean" ||
            !isTextLayerReplaceReceipt(body.receipt, {
              operationId,
              itemId: item,
              layerId: layer,
              sourceRevision,
              selector: unit,
              unitRevision,
            }) || containsCommandFingerprint(body)) {
          this._invalidResponse(
            "Engine returned an invalid text-layer mutation receipt",
            "PUT", path, body, undefined, status);
        }
        return body;
      });
    }

    _replicaPackageImport({ bookId, sourceId, file, overwrite = false,
      idempotencyKey, signal } = {}) {
      const form = this._formDataFactory();
      form.append("lib", file);
      return this._requestJson("POST",
        `/v1/items/${encodePart(bookId)}/replica/lib-imports`, {
          headers: {
            "Idempotency-Key": operationKey(
              idempotencyKey, "idempotencyKey"),
          },
          query: {
            source_id: sourceId,
            overwrite: overwrite ? 1 : undefined,
          },
          multipart: form,
          signal,
        });
    }

    _replicaPackageOpen({ file, idempotencyKey, signal } = {}) {
      const form = this._formDataFactory();
      form.append("lib", file);
      return this._requestJson("POST", "/v1/lib-opens", {
        headers: {
          "Idempotency-Key": operationKey(
            idempotencyKey, "idempotencyKey"),
        },
        multipart: form,
        signal,
      });
    }

    _replicaPackageExportUrl({ bookId, sourceId } = {}) {
      return this._url(this._buildPath(bookId, "replica-export"), {
        src: sourceId,
      });
    }

    _replicaPrintUrl({ bookId, sourceId, layer } = {}) {
      return this._url(this._buildPath(bookId, "replica-print"), {
        src: sourceId, layer,
      });
    }
  }

  return { EngineClient, EngineClientError };
});

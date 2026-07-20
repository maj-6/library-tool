const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");


const root = path.join(__dirname, "..");
const mainSource = fs.readFileSync(path.join(root, "desktop", "main.js"), "utf8");
const preloadSource = fs.readFileSync(path.join(root, "desktop", "preload.js"), "utf8");
const startupPreloadSource = fs.readFileSync(
  path.join(root, "desktop", "startup-preload.js"), "utf8");
const updaterPreloadSource = fs.readFileSync(
  path.join(root, "desktop", "updater-preload.js"), "utf8");
const appSource = fs.readFileSync(
  path.join(root, "tools", "whl_explorer", "static", "app.js"), "utf8");
const serverSource = fs.readFileSync(
  path.join(root, "tools", "whl_explorer", "server.py"), "utf8");
const desktopPackage = JSON.parse(fs.readFileSync(
  path.join(root, "desktop", "package.json"), "utf8"));

function block(source, startMarker, endMarker) {
  const start = source.indexOf(startMarker);
  const end = source.indexOf(endMarker, start);
  assert.ok(start >= 0 && end > start, `${startMarker} block is present`);
  return source.slice(start, end);
}

function transportPolicy() {
  const context = vm.createContext({ URL, Buffer, crypto });
  const helpers = block(
    mainSource,
    "function createDesktopCapability",
    "// --- End testable desktop transport policy",
  );
  vm.runInContext(`
    const DESKTOP_CAPABILITY_HEADER = "X-WHL-Desktop-Capability";
    const DESKTOP_CAPABILITY_RE = /^[A-Za-z0-9_-]{43}$/;
    ${helpers}
    this.api = { createDesktopCapability, isTrustedAppDocumentUrl,
      isSidecarApiUrl, classifyAuthenticatedResource,
      shouldGrantTrustedAppPermission, shouldAuthorizeApiRequest, capabilityHeaders,
      createRequestChainTracker };
  `, context);
  return context.api;
}

function apiTransportHarness() {
  const handlers = {};
  const webRequest = {};
  for (const name of ["onBeforeRequest", "onBeforeRedirect", "onCompleted",
    "onErrorOccurred", "onBeforeSendHeaders"]) {
    webRequest[name] = (_filter, callback) => { handlers[name] = callback; };
  }
  const context = vm.createContext({ URL, Buffer, crypto, handlers, webRequest });
  const helpers = block(
    mainSource,
    "function createDesktopCapability",
    "// --- End testable desktop transport policy",
  );
  const install = block(
    mainSource,
    "function installApiCapabilityTransport",
    "function openExternalUrl",
  );
  vm.runInContext(`
    const DESKTOP_CAPABILITY_HEADER = "X-WHL-Desktop-Capability";
    const DESKTOP_CAPABILITY_RE = /^[A-Za-z0-9_-]{43}$/;
    ${helpers}
    let sidecarPort = 45678;
    let sidecarCapability = "${"S".repeat(43)}";
    const mainFrame = { url: "http://127.0.0.1:45678/" };
    const mainWebContents = { id: 9, mainFrame, session: { webRequest } };
    let mainWindow = { isDestroyed: () => false, webContents: mainWebContents };
    const authenticatedResourceLoads = new Map();
    function sidecarOrigin() { return "http://127.0.0.1:45678"; }
    ${install}
    installApiCapabilityTransport(mainWindow);
    this.api = { mainFrame, authenticatedResourceLoads };
  `, context);
  const before = (details) => {
    let result;
    handlers.onBeforeRequest(details, (value) => { result = value; });
    assert.equal(Object.keys(result).length, 0);
  };
  const send = (details) => {
    let result;
    handlers.onBeforeSendHeaders(details, (value) => { result = value; });
    return result.requestHeaders;
  };
  return { handlers, before, send, ...context.api };
}

class FakeEvents {
  constructor() { this.handlers = new Map(); }
  on(name, callback) {
    const values = this.handlers.get(name) || [];
    values.push({ callback, once: false });
    this.handlers.set(name, values);
  }
  once(name, callback) {
    const values = this.handlers.get(name) || [];
    values.push({ callback, once: true });
    this.handlers.set(name, values);
  }
  emit(name, ...args) {
    const values = this.handlers.get(name) || [];
    this.handlers.set(name, values.filter((value) => !value.once));
    for (const value of values) value.callback(...args);
  }
}

function resourceWindowHarness() {
  let nextId = 30;
  const created = [];
  class FakeBrowserWindow extends FakeEvents {
    constructor(options) {
      super();
      this.options = options;
      this.webContents = new FakeEvents();
      this.webContents.id = nextId++;
      this.webContents.mainFrame = { url: "" };
      this.webContents.session = {};
      this.webContents.setWindowOpenHandler = (handler) => { this.openHandler = handler; };
      this.webContents.isDestroyed = () => false;
      this.loadURL = (url) => {
        this.loadedUrl = url;
        this.webContents.mainFrame.url = url;
      };
      created.push(this);
    }
  }
  const context = vm.createContext({ URL, Buffer, crypto, FakeBrowserWindow, created });
  const helpers = block(
    mainSource,
    "function createDesktopCapability",
    "// --- End testable desktop transport policy",
  );
  const createResource = block(
    mainSource,
    "function createAuthenticatedResourceWindow",
    "function createWindow",
  );
  vm.runInContext(`
    const DESKTOP_CAPABILITY_HEADER = "X-WHL-Desktop-Capability";
    const DESKTOP_CAPABILITY_RE = /^[A-Za-z0-9_-]{43}$/;
    ${helpers}
    const BrowserWindow = FakeBrowserWindow;
    const mainWindow = { isDestroyed: () => false };
    const authenticatedResourceLoads = new Map();
    const resourceWindows = new Set();
    const isDev = false;
    function sidecarOrigin() { return "http://127.0.0.1:45678"; }
    function denyUnrequestedPermissions() {}
    function openExternalUrl() { return false; }
    ${createResource}
    this.api = { createAuthenticatedResourceWindow,
      authenticatedResourceLoads, resourceWindows };
  `, context);
  return { created, ...context.api };
}

function pdfHelpers(fetchImpl, urlApi = URL) {
  const context = vm.createContext({
    AbortController, Blob, Uint8Array, URL: urlApi, fetchImpl,
    window: {},
  });
  const helpers = block(
    appSource,
    "const DESKTOP_PDF_EMBED_MAX_BYTES",
    "function createPdfViewer",
  );
  vm.runInContext(`
    const fetch = fetchImpl;
    ${helpers}
    this.api = { pdfResponseLength, fetchBoundedPdfBlob, replaceObjectUrl,
      requestDesktopResource, openLocalResource };
  `, context);
  return context.api;
}

function responseHeaders(values = {}) {
  const normalized = Object.fromEntries(
    Object.entries(values).map(([key, value]) => [key.toLowerCase(), String(value)]));
  return { get: (name) => normalized[String(name).toLowerCase()] || null };
}

function responseStream(chunks) {
  let index = 0;
  const state = { canceled: false };
  const reader = {
    async read() {
      if (index >= chunks.length) return { done: true };
      return { done: false, value: Uint8Array.from(chunks[index++]) };
    },
    async cancel() { state.canceled = true; },
  };
  return { state, body: { getReader: () => reader } };
}

function pdfViewerHarness(fetchImpl, desktopApi) {
  const selectors = [
    ".pdf-frame", ".pdf-framewrap", ".pdf-note", ".pdf-path", ".pdf-size",
    ".pdf-open", ".pdf-ocr", ".pdf-ocrpane", ".pdf-pagesbtn", ".pdf-laybtn",
    ".pdf-pagesave", ".pdf-pagesbox",
  ];
  const makeNode = () => {
    const attributes = new Map();
    const listeners = {};
    const node = {
      hidden: false, textContent: "", innerHTML: "", dataset: {}, listeners,
      classList: { toggle() {}, add() {}, remove() {} },
      addEventListener: (name, handler) => { listeners[name] = handler; },
      removeAttribute: (name) => { attributes.delete(name); if (name === "src") node._src = ""; },
      getAttribute: (name) => attributes.get(name) || null,
      setAttribute: (name, value) => attributes.set(name, String(value)),
      querySelectorAll: () => [],
    };
    Object.defineProperty(node, "src", {
      get: () => node._src || "",
      set: (value) => { node._src = String(value); attributes.set("src", String(value)); },
    });
    Object.defineProperty(node, "href", {
      get: () => attributes.get("href") || "",
      set: (value) => attributes.set("href", String(value)),
    });
    return node;
  };
  const nodes = Object.fromEntries(selectors.map((selector) => [selector, makeNode()]));
  const rootNode = makeNode();
  rootNode.querySelector = (selector) => nodes[selector];
  const revoked = [];
  let objectId = 0;
  const urlApi = {
    createObjectURL: () => `blob:viewer-${++objectId}`,
    revokeObjectURL: (value) => revoked.push(value),
  };
  const context = vm.createContext({
    AbortController, Blob, Uint8Array, URL: urlApi, fetchImpl,
    document: { createElement: () => rootNode },
    window: { whlDesktop: desktopApi },
    state: { settings: { viewerLayout: false, confirmDiscard: true } },
    ICONS: new Proxy({}, { get: () => "" }),
  });
  const source = block(
    appSource,
    "const DESKTOP_PDF_EMBED_MAX_BYTES",
    "// --- WHL publication viewer window",
  );
  vm.runInContext(`
    const fetch = fetchImpl;
    function fmtBytes(value) { return String(value); }
    function pdfOcrMode(wanted, requested, hasText) {
      const nextWanted = requested == null ? !!wanted : !!requested;
      return { wanted: nextWanted, on: nextWanted && !!hasText };
    }
    ${source}
    this.viewer = createPdfViewer();
  `, context);
  return { viewer: context.viewer, nodes, revoked };
}

function iaViewerHarness(fetchImpl, boundedFetch) {
  const ids = ["ia-pages", "ia-frame", "ia-meta", "ia-downloads", "ia-title",
    "ia-external", "ia-overlay"];
  const nodes = Object.fromEntries(ids.map((id) => [id, {
    hidden: false, src: "", textContent: "", innerHTML: "", scrollTop: 0,
    onclick: null,
    querySelectorAll: () => [],
    querySelector: () => null,
  }]));
  const revoked = [];
  let objectId = 0;
  const urlApi = {
    createObjectURL: () => `blob:ia-${++objectId}`,
    revokeObjectURL: (value) => revoked.push(value),
  };
  const context = vm.createContext({
    AbortController, Blob, URL: urlApi, fetchImpl, boundedFetch, nodes,
    window: { open() {} },
    state: { settings: { autoIaDownload: false } },
  });
  const source = block(appSource, "const iaViewer", "function initWebView");
  vm.runInContext(`
    const fetch = fetchImpl;
    const el = (id) => nodes[id];
    const esc = (value) => String(value || "");
    const fmtSize = (value) => String(value || "");
    const enqueueAutoDl = () => {};
    const fetchBoundedPdfBlob = boundedFetch;
    function replaceObjectUrl(previous, blob) {
      if (previous) URL.revokeObjectURL(previous);
      return blob ? URL.createObjectURL(blob) : "";
    }
    ${source}
    this.api = { iaViewer, openIaViewer, closeIaViewer, showIaPreview };
  `, context);
  return { nodes, revoked, ...context.api };
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((onResolve, onReject) => {
    resolve = onResolve;
    reject = onReject;
  });
  return { promise, resolve, reject };
}

test("desktop capability is 256 random bits encoded without URL-unsafe bytes", () => {
  const api = transportPolicy();
  const capability = api.createDesktopCapability(() => Buffer.alloc(32, 0xab));
  assert.equal(capability.length, 43);
  assert.match(capability, /^[A-Za-z0-9_-]{43}$/);
  assert.equal(Buffer.from(capability, "base64url").length, 32);
});

test("only the exact trusted main frame receives API authorization", () => {
  const api = transportPolicy();
  const origin = "http://127.0.0.1:45678";
  const mainFrame = { url: origin + "/" };
  const details = {
    url: origin + "/api/client_state",
    method: "GET",
    webContentsId: 9,
    frame: mainFrame,
    resourceType: "xhr",
  };
  const trust = { origin, webContentsId: 9, mainFrame };

  assert.equal(api.shouldAuthorizeApiRequest(details, trust), true);
  assert.equal(api.shouldAuthorizeApiRequest(
    { ...details, frame: { url: origin + "/" } }, trust), false);
  assert.equal(api.shouldAuthorizeApiRequest(
    { ...details, webContentsId: 10 }, trust), false);
  const remoteFrame = { url: "https://example.com/" };
  assert.equal(api.shouldAuthorizeApiRequest(
    { ...details, frame: remoteFrame }, { ...trust, mainFrame: remoteFrame }), false);
  assert.equal(api.shouldAuthorizeApiRequest(
    { ...details, url: "http://127.0.0.1:9999/api/client_state" }, trust), false);
});

test("authenticated resource routes are an explicit GET-only allowlist", () => {
  const api = transportPolicy();
  const origin = "http://127.0.0.1:45678";
  const classified = (value) => api.classifyAuthenticatedResource(value, origin);

  assert.equal(classified("/api/pdf?path=books%2Fone.pdf").mode, "exact-pdf");
  assert.equal(classified("/api/pdf?path=books%2Fone.pdf&preview=1&pages=20").mode,
    "exact-pdf");
  assert.equal(classified("/api/pdf?url=https%3A%2F%2Fexample.test%2Fone.pdf&preview=1").mode,
    "exact-pdf");
  assert.equal(classified(
    "/api/pdf?url=https%3A%2F%2Fexample.test%2Fone.pdf&preview=1&pages=500").mode,
  "exact-pdf");
  assert.equal(classified("/api/builds/book/replica-print?src=primary&layer=fr").mode,
    "one-shot");
  assert.equal(classified("/api/builds/book/ocr/images/figure.png").mode, "one-shot");
  assert.equal(classified("/api/capture/image?path=captures%2Fone.jpg").mode, "one-shot");

  for (const value of [
    "/api/secrets",
    "/api/client_state",
    "/api/pdf",
    "/api/pdf?path=one.pdf&url=https%3A%2F%2Fexample.test%2Ftwo.pdf",
    "/api/pdf?path=one.pdf&extra=1",
    "/api/pdf?path=one.pdf&path=two.pdf",
    "/api/pdf?path=one.pdf&pages=20",
    "/api/pdf?path=one.pdf&preview=0&pages=20",
    "/api/pdf?path=one.pdf&preview=1&pages=0",
    "/api/pdf?path=one.pdf&preview=1&pages=020",
    "/api/pdf?path=one.pdf&preview=1&pages=501",
    "/api/pdf?path=one.pdf&preview=1&pages=20&pages=21",
    "/api/builds/book/replica-print?unsafe=1",
    "/api/builds/book/ocr/images/figure.png?download=1",
    "/api/capture/image",
    "https://example.test/api/pdf?path=one.pdf",
  ]) assert.equal(classified(value), null, value);
});

test("resource-open IPC accepts only the trusted application main frame", () => {
  const opened = [];
  const ipcHandlers = {};
  const context = vm.createContext({ URL, Buffer, crypto, opened, ipcHandlers });
  const helpers = block(
    mainSource,
    "function createDesktopCapability",
    "// --- End testable desktop transport policy",
  );
  const senderPolicy = block(
    mainSource,
    "function sidecarOrigin",
    "// --- .lib open flow",
  );
  const resourceIpc = block(
    mainSource,
    "// Opening an authenticated top-level resource is privileged.",
    "// a free loopback port",
  );
  vm.runInContext(`
    const DESKTOP_CAPABILITY_HEADER = "X-WHL-Desktop-Capability";
    const DESKTOP_CAPABILITY_RE = /^[A-Za-z0-9_-]{43}$/;
    ${helpers}
    let sidecarPort = 45678;
    const appFrame = { url: "http://127.0.0.1:45678/" };
    const appContents = { mainFrame: appFrame };
    let mainWindow = { isDestroyed: () => false, webContents: appContents };
    ${senderPolicy}
    const ipcMain = { on: (name, handler) => { ipcHandlers[name] = handler; } };
    function createAuthenticatedResourceWindow(url) { opened.push(url); }
    ${resourceIpc}
    this.api = { appFrame, appContents };
  `, context);
  const trusted = { sender: context.api.appContents, senderFrame: context.api.appFrame };
  ipcHandlers["resource:open"](trusted, "/api/pdf?path=book.pdf");
  assert.deepEqual(opened, ["/api/pdf?path=book.pdf"]);

  ipcHandlers["resource:open"]({ ...trusted, senderFrame: { url: trusted.senderFrame.url } },
    "/api/pdf?path=subframe.pdf");
  ipcHandlers["resource:open"]({ sender: { mainFrame: trusted.senderFrame },
    senderFrame: trusted.senderFrame }, "/api/pdf?path=other-window.pdf");
  trusted.senderFrame.url = "https://example.test/";
  ipcHandlers["resource:open"](trusted, "/api/pdf?path=remote.pdf");
  ipcHandlers["resource:open"]({ ...trusted, senderFrame: trusted.sender.mainFrame }, 42);
  assert.deepEqual(opened, ["/api/pdf?path=book.pdf"]);
});

test("resource windows retain exact PDF grants and clear every grant on close", () => {
  const harness = resourceWindowHarness();
  assert.equal(harness.createAuthenticatedResourceWindow(
    "/api/pdf?path=book.pdf&preview=1&pages=20"), true);
  const pdfWindow = harness.created[0];
  const pdfGrant = harness.authenticatedResourceLoads.get(pdfWindow.webContents.id);
  assert.equal(pdfGrant.mode, "exact-pdf");
  assert.equal(pdfGrant.url,
    "http://127.0.0.1:45678/api/pdf?path=book.pdf&preview=1&pages=20");
  pdfWindow.webContents.emit("did-finish-load");
  assert.equal(harness.authenticatedResourceLoads.has(pdfWindow.webContents.id), true);
  pdfWindow.emit("closed");
  assert.equal(harness.authenticatedResourceLoads.has(pdfWindow.webContents.id), false);
  assert.equal(harness.resourceWindows.has(pdfWindow), false);

  assert.equal(harness.createAuthenticatedResourceWindow(
    "/api/builds/book/replica-print?src=primary"), true);
  const printWindow = harness.created[1];
  assert.equal(harness.authenticatedResourceLoads.get(printWindow.webContents.id).mode,
    "one-shot");
  printWindow.webContents.emit("did-finish-load");
  assert.equal(harness.authenticatedResourceLoads.has(printWindow.webContents.id), false);
  assert.equal(harness.createAuthenticatedResourceWindow("/api/secrets"), false);
  assert.equal(harness.created.length, 2);
});

test("clipboard permission is limited to the exact trusted application main frame", () => {
  const api = transportPolicy();
  const origin = "http://127.0.0.1:45678";
  const webContents = { id: 9 };
  const trust = { origin, webContents };
  const details = { isMainFrame: true, requestingUrl: origin + "/" };
  assert.equal(api.shouldGrantTrustedAppPermission(
    "clipboard-read", webContents, details, trust), true);
  assert.equal(api.shouldGrantTrustedAppPermission(
    "clipboard-sanitized-write", webContents, details, trust), true);
  assert.equal(api.shouldGrantTrustedAppPermission(
    "geolocation", webContents, details, trust), false);
  assert.equal(api.shouldGrantTrustedAppPermission(
    "clipboard-read", { id: 9 }, details, trust), false);
  assert.equal(api.shouldGrantTrustedAppPermission(
    "clipboard-read", webContents, { ...details, isMainFrame: false }, trust), false);
  assert.equal(api.shouldGrantTrustedAppPermission(
    "clipboard-read", webContents,
    { ...details, requestingUrl: origin + "/api/pdf?path=book.pdf" }, trust), false);
  assert.equal(api.shouldGrantTrustedAppPermission(
    "clipboard-read", webContents,
    { ...details, requestingUrl: "https://example.test/" }, trust), false);
});

test("installed session handlers preserve trusted clipboard and deny everything else", () => {
  const session = {
    setPermissionCheckHandler(handler) { this.check = handler; },
    setPermissionRequestHandler(handler) { this.request = handler; },
  };
  const context = vm.createContext({ URL, Buffer, crypto, session });
  const helpers = block(
    mainSource,
    "function createDesktopCapability",
    "// --- End testable desktop transport policy",
  );
  const permissions = block(
    mainSource,
    "const hardenedSessions",
    "function denyRendererNavigation",
  );
  vm.runInContext(`
    const DESKTOP_CAPABILITY_HEADER = "X-WHL-Desktop-Capability";
    const DESKTOP_CAPABILITY_RE = /^[A-Za-z0-9_-]{43}$/;
    ${helpers}
    const mainFrame = { url: "http://127.0.0.1:45678/" };
    const webContents = { id: 9, mainFrame };
    let mainWindow = { isDestroyed: () => false, webContents };
    function sidecarOrigin() { return "http://127.0.0.1:45678"; }
    ${permissions}
    denyUnrequestedPermissions(session);
    this.api = { webContents };
  `, context);
  const details = {
    isMainFrame: true,
    requestingUrl: "http://127.0.0.1:45678/",
  };
  assert.equal(session.check(context.api.webContents, "clipboard-read",
    "http://127.0.0.1:45678", details), true);
  assert.equal(session.check(context.api.webContents, "notifications",
    "http://127.0.0.1:45678", details), false);
  assert.equal(session.check(context.api.webContents, "clipboard-read",
    "http://127.0.0.1:45678", { ...details, isMainFrame: false }), false);
  let granted;
  session.request(context.api.webContents, "clipboard-sanitized-write",
    (value) => { granted = value; }, details);
  assert.equal(granted, true);
  session.request({ id: 9 }, "clipboard-read", (value) => { granted = value; }, details);
  assert.equal(granted, false);
});

test("the pinned Electron contract exposes WebFrameMain request identity", () => {
  const version = desktopPackage.devDependencies.electron;
  assert.match(version, /^\^31\./);
  assert.match(mainSource, /details\.webContentsId/);
  assert.match(mainSource, /details\.frame !== trust\.mainFrame/);
});

test("request provenance rejects remote-to-sidecar and API redirect chains", () => {
  const api = transportPolicy();
  const origin = "http://127.0.0.1:45678";
  const frame = { url: origin + "/" };

  const remoteTracker = api.createRequestChainTracker();
  const remote = {
    id: 41, url: "https://images.example/cover.jpg", timestamp: 1,
    method: "GET",
    webContentsId: 9, frame, resourceType: "image",
  };
  assert.equal(remoteTracker.observe(remote), true);
  remoteTracker.redirect({ ...remote, timestamp: 2,
    redirectURL: origin + "/api/secrets" });
  const remoteRedirect = { ...remote, timestamp: 3, url: origin + "/api/secrets" };
  assert.equal(remoteTracker.observe(remoteRedirect), false);
  assert.equal(remoteTracker.hasAuthorizedOrigin(remoteRedirect, origin), false);

  const apiTracker = api.createRequestChainTracker();
  const initial = {
    id: 42, url: origin + "/api/client_state", timestamp: 10,
    method: "GET",
    webContentsId: 9, frame, resourceType: "xhr",
  };
  assert.equal(apiTracker.observe(initial), true);
  assert.equal(apiTracker.hasAuthorizedOrigin(initial, origin), true);
  apiTracker.redirect({ ...initial, timestamp: 11,
    redirectURL: origin + "/api/secrets" });
  const apiRedirect = { ...initial, timestamp: 12, url: origin + "/api/secrets" };
  apiTracker.observe(apiRedirect);
  assert.equal(apiTracker.hasAuthorizedOrigin(apiRedirect, origin), false);
});

test("installed webRequest callbacks enforce provenance and clean terminals", () => {
  const harness = apiTransportHarness();
  const origin = "http://127.0.0.1:45678";
  const base = (id, url, timestamp = id) => ({
    id, url, timestamp, method: "GET", webContentsId: 9,
    frame: harness.mainFrame, resourceType: "xhr", requestHeaders: {
      Accept: "application/json", "x-whl-desktop-capability": "forged",
    },
  });

  const direct = base(101, origin + "/api/client_state");
  harness.before(direct);
  assert.equal(harness.send(direct)["X-WHL-Desktop-Capability"], "S".repeat(43));

  const remote = base(102, "https://example.test/start", 10);
  harness.before(remote);
  assert.equal(harness.send(remote)["x-whl-desktop-capability"], undefined);
  harness.handlers.onBeforeRedirect({ ...remote, timestamp: 11,
    redirectURL: origin + "/api/secrets" });
  const remoteFinal = { ...remote, timestamp: 12, url: origin + "/api/secrets" };
  harness.before(remoteFinal);
  assert.equal(harness.send(remoteFinal)["X-WHL-Desktop-Capability"], undefined);

  const redirectedApi = base(103, origin + "/api/client_state", 20);
  harness.before(redirectedApi);
  harness.handlers.onBeforeRedirect({ ...redirectedApi, timestamp: 21,
    redirectURL: origin + "/api/secrets" });
  const apiFinal = { ...redirectedApi, timestamp: 22, url: origin + "/api/secrets" };
  harness.before(apiFinal);
  assert.equal(harness.send(apiFinal)["X-WHL-Desktop-Capability"], undefined);

  const completed = base(104, origin + "/api/client_state", 30);
  harness.before(completed);
  harness.handlers.onCompleted({ ...completed, timestamp: 31 });
  assert.equal(harness.send(completed)["X-WHL-Desktop-Capability"], undefined);
  const errored = base(105, origin + "/api/client_state", 40);
  harness.before(errored);
  harness.handlers.onErrorOccurred({ ...errored, timestamp: 41 });
  assert.equal(harness.send(errored)["X-WHL-Desktop-Capability"], undefined);
});

test("installed transport supports PDF plugin Range but taints redirects", () => {
  const harness = apiTransportHarness();
  const origin = "http://127.0.0.1:45678";
  const url = origin + "/api/pdf?path=book.pdf";
  const childMainFrame = { url };
  const pluginFrame = { url: "chrome-extension://pdf-viewer/index.html" };
  harness.authenticatedResourceLoads.set(22, {
    url, mainFrame: childMainFrame, mode: "exact-pdf",
  });
  const range = {
    id: 201, url, timestamp: 1, method: "GET", webContentsId: 22,
    frame: pluginFrame, resourceType: "other",
    requestHeaders: { Range: "bytes=0-65535" },
  };
  harness.before(range);
  assert.equal(harness.send(range)["X-WHL-Desktop-Capability"], "S".repeat(43));

  const redirected = { ...range, id: 202, timestamp: 10 };
  harness.before(redirected);
  harness.handlers.onBeforeRedirect({ ...redirected, timestamp: 11,
    redirectURL: url + "&changed=1" });
  const final = { ...redirected, timestamp: 12, url: url + "&changed=1" };
  harness.before(final);
  assert.equal(harness.send(final)["X-WHL-Desktop-Capability"], undefined);
});

test("request provenance permits exact API, range, and print image requests", () => {
  const api = transportPolicy();
  const origin = "http://127.0.0.1:45678";
  const appFrame = { url: origin + "/" };
  const tracker = api.createRequestChainTracker();
  const exact = {
    id: 51, url: origin + "/api/pdf?path=book.pdf", timestamp: 1,
    method: "GET",
    webContentsId: 9, frame: appFrame, resourceType: "xhr",
    requestHeaders: { Range: "bytes=0-65535" },
  };
  tracker.observe(exact);
  assert.equal(tracker.hasAuthorizedOrigin(exact, origin), true);
  assert.equal(api.shouldAuthorizeApiRequest(exact, {
    origin, webContentsId: 9, mainFrame: appFrame,
  }), true);

  const printUrl = origin + "/api/builds/book/replica-print?source=main";
  const printFrame = { url: printUrl };
  const image = {
    id: 52,
    url: origin + "/api/builds/book/ocr/images/figure.png",
    timestamp: 2,
    method: "GET",
    webContentsId: 12,
    frame: printFrame,
    resourceType: "image",
  };
  tracker.observe(image);
  assert.equal(tracker.hasAuthorizedOrigin(image, origin), true);
  assert.equal(api.shouldAuthorizeApiRequest(image, {
    origin, webContentsId: 12, mainFrame: printFrame, oneShotUrl: printUrl,
  }), true);
});

test("request provenance is bounded, cleans terminals, and resists id reuse", () => {
  const api = transportPolicy();
  const origin = "http://127.0.0.1:45678";
  const frame = { url: origin + "/" };
  const request = (id, url, timestamp) => ({
    id, url, timestamp, method: "GET", webContentsId: 9, frame, resourceType: "xhr",
  });
  const tracker = api.createRequestChainTracker(1);
  const first = request(1, origin + "/api/one", 1);
  const overCapacity = request(2, origin + "/api/two", 2);
  assert.equal(tracker.observe(first), true);
  assert.equal(tracker.observe(overCapacity), false);
  assert.equal(tracker.hasAuthorizedOrigin(overCapacity, origin), false);
  assert.equal(tracker.size(), 1);
  assert.equal(tracker.finish({ ...first, timestamp: 3 }), true);
  assert.equal(tracker.size(), 0);

  const reused = request(1, origin + "/api/new", 10);
  assert.equal(tracker.observe(reused), true);
  assert.equal(tracker.finish({ ...first, timestamp: 5 }), false);
  assert.equal(tracker.hasAuthorizedOrigin(reused, origin), true);
  assert.equal(tracker.finish({ ...reused, timestamp: 11 }), true);

  const reentrant = request(3, origin + "/api/reentrant", 20);
  tracker.observe(reentrant);
  assert.equal(tracker.observe({ ...reentrant, timestamp: 21 }), false);
  assert.equal(tracker.hasAuthorizedOrigin(reentrant, origin), false);
  assert.equal(tracker.finish({ ...reentrant, timestamp: 22 }), true);
});

test("one-shot resource grants authorize one exact top-level navigation only", () => {
  const api = transportPolicy();
  const origin = "http://127.0.0.1:45678";
  const mainFrame = { url: "" };
  const url = origin + "/api/replica/print?id=one";
  const trust = { origin, webContentsId: 12, mainFrame, oneShotUrl: url };
  const request = {
    url,
    method: "GET",
    webContentsId: 12,
    frame: mainFrame,
    resourceType: "mainFrame",
  };
  assert.equal(api.shouldAuthorizeApiRequest(request, trust), true);
  assert.equal(api.shouldAuthorizeApiRequest(
    { ...request, resourceType: "xhr" }, trust), false);
  assert.equal(api.shouldAuthorizeApiRequest(
    { ...request, url: origin + "/api/secrets" }, trust), false);

  const printUrl = origin + "/api/builds/book/replica-print?source=main";
  const printTrust = { ...trust, oneShotUrl: printUrl };
  mainFrame.url = printUrl;
  assert.equal(api.shouldAuthorizeApiRequest({
    ...request,
    url: origin + "/api/builds/book/ocr/images/figure.png",
    resourceType: "image",
  }, printTrust), true);
  assert.equal(api.shouldAuthorizeApiRequest({
    ...request,
    url: origin + "/api/builds/other/ocr/images/figure.png",
    resourceType: "image",
  }, printTrust), false);
  assert.equal(api.shouldAuthorizeApiRequest({
    ...request,
    url: origin + "/api/secrets",
    resourceType: "xhr",
  }, printTrust), false);
});

test("PDF resource grants permit exact GET and Range requests only", () => {
  const api = transportPolicy();
  const origin = "http://127.0.0.1:45678";
  const mainFrame = { url: origin + "/api/pdf?path=book.pdf" };
  const pluginFrame = { url: "chrome-extension://mhjfbmdgcfjbbpaeojofohoefgiehjai/index.html" };
  const url = origin + "/api/pdf?path=book.pdf";
  const trust = { origin, webContentsId: 18, mainFrame, exactResourceUrl: url };
  const request = {
    url, method: "GET", webContentsId: 18, frame: pluginFrame,
    resourceType: "other", requestHeaders: { Range: "bytes=0-65535" },
  };
  assert.equal(api.shouldAuthorizeApiRequest(request, trust), true);
  assert.equal(api.shouldAuthorizeApiRequest({ ...request, method: "POST" }, trust), false);
  assert.equal(api.shouldAuthorizeApiRequest(
    { ...request, url: url + "&other=1" }, trust), false);
  assert.equal(api.shouldAuthorizeApiRequest(
    { ...request, webContentsId: 19 }, trust), false);
});

test("untrusted requests lose spoofed capability headers", () => {
  const api = transportPolicy();
  const original = {
    Accept: "application/json",
    "x-whl-desktop-capability": "attacker-value",
  };
  const denied = api.capabilityHeaders(original, "trusted-value", false);
  assert.deepEqual(Object.keys(denied), ["Accept"]);
  assert.equal(original["x-whl-desktop-capability"], "attacker-value");
  const allowed = api.capabilityHeaders(original, "trusted-value", true);
  assert.equal(allowed["X-WHL-Desktop-Capability"], "trusted-value");
  assert.equal(allowed["x-whl-desktop-capability"], undefined);
});

test("bounded PDF fetch streams only through the cap and never calls blob()", async () => {
  const stream = responseStream([[1, 2], [3, 4]]);
  let blobCalls = 0;
  const calls = [];
  const fetchImpl = async (_url, options = {}) => {
    calls.push(options);
    if (options.method === "HEAD") {
      return { ok: true, status: 200, headers: responseHeaders({ "content-length": 4 }) };
    }
    return {
      ok: true, status: 200,
      headers: responseHeaders({ "content-type": "application/pdf" }),
      body: stream.body,
      blob: async () => { blobCalls++; return new Blob([]); },
    };
  };
  const api = pdfHelpers(fetchImpl);
  const controller = new AbortController();
  const result = await api.fetchBoundedPdfBlob("/api/pdf?path=small.pdf", {
    maxBytes: 4, signal: controller.signal,
  });
  assert.equal(result.bytes, 4);
  assert.equal(result.blob.size, 4);
  assert.equal(result.blob.type, "application/pdf");
  assert.equal(blobCalls, 0);
  assert.equal(calls.length, 2);
  assert.equal(calls[0].signal, controller.signal);
  assert.equal(calls[1].signal, controller.signal);
});

test("bounded PDF fetch fails closed on unknown, changed, or unstreamable bodies", async () => {
  let calls = 0;
  const missing = pdfHelpers(async () => {
    calls++;
    return { ok: true, status: 200, headers: responseHeaders() };
  });
  await assert.rejects(
    missing.fetchBoundedPdfBlob("/api/pdf?path=unknown.pdf", { maxBytes: 4 }),
    (error) => error.code === "PDF_EMBED_FALLBACK");
  assert.equal(calls, 1);

  const overflow = responseStream([[1, 2, 3], [4, 5, 6]]);
  let blobCalls = 0;
  const changed = pdfHelpers(async (_url, options = {}) => options.method === "HEAD"
    ? { ok: true, status: 200, headers: responseHeaders({ "content-length": 4 }) }
    : {
      ok: true, status: 200,
      headers: responseHeaders({ "content-length": 4 }), body: overflow.body,
      blob: async () => { blobCalls++; return new Blob([]); },
    });
  await assert.rejects(
    changed.fetchBoundedPdfBlob("/api/pdf?path=changed.pdf", { maxBytes: 4 }),
    (error) => error.code === "PDF_EMBED_FALLBACK" && error.bytes === 6);
  assert.equal(overflow.state.canceled, true);
  assert.equal(blobCalls, 0);

  let canceled = false;
  const unstreamable = pdfHelpers(async (_url, options = {}) => options.method === "HEAD"
    ? { ok: true, status: 200, headers: responseHeaders({ "content-length": 4 }) }
    : {
      ok: true, status: 200, headers: responseHeaders(),
      body: { cancel: async () => { canceled = true; } },
    });
  await assert.rejects(
    unstreamable.fetchBoundedPdfBlob("/api/pdf?path=no-stream.pdf", { maxBytes: 4 }),
    (error) => error.code === "PDF_EMBED_FALLBACK");
  assert.equal(canceled, true);
});

test("object URL replacement always revokes the superseded resource", () => {
  const revoked = [];
  let next = 0;
  const urlApi = {
    createObjectURL: () => `blob:test-${++next}`,
    revokeObjectURL: (value) => revoked.push(value),
  };
  const api = pdfHelpers(async () => {}, urlApi);
  const first = api.replaceObjectUrl("", new Blob([[1]]));
  const second = api.replaceObjectUrl(first, new Blob([[2]]));
  const cleared = api.replaceObjectUrl(second, null);
  assert.equal(first, "blob:test-1");
  assert.equal(second, "blob:test-2");
  assert.equal(cleared, "");
  assert.deepEqual(revoked, ["blob:test-1", "blob:test-2"]);
});

test("desktop PDF fallback link invokes the main-frame resource IPC", async () => {
  const opened = [];
  const fetchImpl = async () => ({
    ok: true, status: 200, headers: responseHeaders(),
  });
  const harness = pdfViewerHarness(fetchImpl, {
    isDesktop: true,
    openResource: (value) => opened.push(value),
  });
  const src = "/api/pdf?path=book.pdf&preview=1&pages=20";
  harness.viewer.show(src, "Book");
  await new Promise((resolve) => setImmediate(resolve));
  const open = harness.nodes[".pdf-open"];
  assert.equal(open.href, src);
  assert.equal(open.hidden, false);
  assert.equal(harness.nodes[".pdf-note"].textContent,
    "Open PDF in a separate window");
  let prevented = false;
  open.listeners.click({ preventDefault: () => { prevented = true; } });
  assert.equal(prevented, true);
  assert.deepEqual(opened, [src]);
});

test("embedded PDF open action streams the original route through IPC", async () => {
  const opened = [];
  const stream = responseStream([[1, 2, 3, 4]]);
  const fetchImpl = async (_url, options = {}) => options.method === "HEAD"
    ? { ok: true, status: 200, headers: responseHeaders({ "content-length": 4 }) }
    : {
      ok: true, status: 200, headers: responseHeaders({ "content-length": 4 }),
      body: stream.body,
    };
  const harness = pdfViewerHarness(fetchImpl, {
    isDesktop: true,
    openResource: (value) => opened.push(value),
  });
  const src = "/api/pdf?path=book.pdf&preview=1&pages=20";
  harness.viewer.show(src, "Book");
  await new Promise((resolve) => setImmediate(resolve));
  const frame = harness.nodes[".pdf-frame"];
  const open = harness.nodes[".pdf-open"];
  assert.match(frame.src, /^blob:viewer-1#/);
  assert.equal(open.href, src);
  let prevented = false;
  open.listeners.click({ preventDefault: () => { prevented = true; } });
  assert.equal(prevented, true);
  assert.deepEqual(opened, [src]);
});

test("PDF viewer aborts superseded and cleared authenticated fetches", async () => {
  const signals = [];
  const fetchImpl = (_url, options = {}) => new Promise((_resolve, reject) => {
    signals.push(options.signal);
    options.signal.addEventListener("abort", () => {
      const error = new Error("aborted");
      error.name = "AbortError";
      reject(error);
    }, { once: true });
  });
  const harness = pdfViewerHarness(fetchImpl, { isDesktop: true, openResource() {} });
  harness.viewer.show("/api/pdf?path=first.pdf", "First");
  assert.equal(signals.length, 1);
  harness.viewer.show("/api/pdf?path=second.pdf", "Second");
  assert.equal(signals[0].aborted, true);
  assert.equal(signals.length, 2);
  harness.viewer.clear();
  assert.equal(signals[1].aborted, true);
  await new Promise((resolve) => setImmediate(resolve));
});

test("closing the IA viewer invalidates and aborts pending metadata", async () => {
  const meta = deferred();
  let signal;
  let boundedCalls = 0;
  const harness = iaViewerHarness((_url, options = {}) => {
    signal = options.signal;
    return meta.promise;
  }, async () => { boundedCalls++; });
  const pending = harness.openIaViewer("first");
  assert.equal(signal.aborted, false);
  harness.closeIaViewer();
  assert.equal(signal.aborted, true);
  meta.resolve({ json: async () => ({ metadata: { title: "Stale" }, pdf: "stale.pdf" }) });
  await pending;
  assert.equal(harness.nodes["ia-overlay"].hidden, true);
  assert.equal(boundedCalls, 0);
  assert.equal(harness.nodes["ia-title"].textContent, "Internet Archive :: first");
});

test("IA supersession and close prevent stale blob creation and revoke owned URLs", async () => {
  const firstMeta = deferred();
  const bounded = deferred();
  const signals = [];
  const fetchImpl = (url, options = {}) => {
    signals.push(options.signal);
    if (url.includes("id=first")) return firstMeta.promise;
    if (url.includes("id=second")) {
      return Promise.resolve({ json: async () => ({
        metadata: { title: "Second" }, details: "https://archive.org/details/second",
        downloads: [], pdf: "https://archive.org/download/second/book.pdf",
      }) });
    }
    if (url.includes("/api/ia/preview/second")) {
      return Promise.resolve({ json: async () => ({ ok: false }) });
    }
    throw new Error(`unexpected fetch ${url}`);
  };
  const harness = iaViewerHarness(fetchImpl, () => bounded.promise);
  const first = harness.openIaViewer("first");
  harness.iaViewer.objectUrl = "blob:ia-superseded";
  const second = harness.openIaViewer("second");
  assert.equal(signals[0].aborted, true);
  assert.deepEqual(harness.revoked, ["blob:ia-superseded"]);
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(harness.nodes["ia-title"].textContent, "Second");
  harness.closeIaViewer();
  assert.equal(signals.at(-1).aborted, true);
  bounded.resolve({ blob: new Blob([[1, 2, 3]]), bytes: 3 });
  firstMeta.resolve({ json: async () => ({
    metadata: { title: "Stale" }, pdf: "https://example.test/stale.pdf",
  }) });
  await Promise.all([first, second]);
  assert.equal(harness.iaViewer.objectUrl, "");
  assert.deepEqual(harness.revoked, ["blob:ia-superseded"]);

  harness.iaViewer.objectUrl = "blob:ia-owned";
  harness.closeIaViewer();
  assert.equal(harness.iaViewer.objectUrl, "");
  assert.deepEqual(harness.revoked, ["blob:ia-superseded", "blob:ia-owned"]);
});

test("sidecar startup passes capability only in the child environment", () => {
  const functionSource = block(
    mainSource,
    "function sidecarCommand",
    "// --- Testable process/window lifecycle guards",
  );
  const fakeProcess = {
    env: { PARENT_ONLY: "kept" },
    platform: "win32",
    resourcesPath: "C:/resources",
  };
  const context = vm.createContext({
    app: { getVersion: () => "1.2.3" },
    isDev: false,
    path,
    process: fakeProcess,
  });
  vm.runInContext(`
    const DESKTOP_CAPABILITY_RE = /^[A-Za-z0-9_-]{43}$/;
    ${functionSource}
    this.sidecarCommand = sidecarCommand;
  `, context);
  const capability = "C".repeat(43);
  const command = context.sidecarCommand(45678, "C:/data", capability);

  assert.equal(command.opts.env.WHL_DESKTOP_CAPABILITY, capability);
  assert.equal(command.opts.env.WHL_DESKTOP_MODE, "packaged");
  assert.equal(command.opts.env.WHL_PORT, "45678");
  assert.equal(command.opts.windowsHide, true);
  assert.equal(command.args.includes(capability), false);
  assert.equal(command.cmd.includes(capability), false);
  assert.deepEqual(fakeProcess.env, { PARENT_ONLY: "kept" });
});

test("renderer and remote subframes never receive the capability", () => {
  assert.match(preloadSource, /if \(process\.isMainFrame\) contextBridge\.exposeInMainWorld/);
  assert.match(startupPreloadSource,
    /if \(process\.isMainFrame\) contextBridge\.exposeInMainWorld/);
  assert.match(updaterPreloadSource,
    /if \(process\.isMainFrame\) contextBridge\.exposeInMainWorld/);
  assert.doesNotMatch(preloadSource, /DESKTOP_CAPABILITY|WHL_DESKTOP_CAPABILITY/);
  assert.doesNotMatch(appSource, /WHL_DESKTOP_CAPABILITY|X-WHL-Desktop-Capability/);
  assert.doesNotMatch(mainSource, /loadURL\([^\n]*capability/i);
  assert.doesNotMatch(mainSource, /localStorage\.setItem|document\.cookie\s*=/);
  assert.match(mainSource, /details\.frame !== trust\.mainFrame/);
  assert.match(preloadSource, /openResource:[\s\S]+ipcRenderer\.send\("resource:open", url\)/);
  assert.match(mainSource, /ipcMain\.on\("resource:open"[\s\S]+isTrustedMainSender\(event\)/);
});

test("remote HTML is retired before automatic API authorization", () => {
  assert.doesNotMatch(appSource, /\/api\/webview\?/);
  assert.match(serverSource, /embedded_remote_content_disabled/);
  assert.match(serverSource, /return jsonify\([^\n]+\), 410/);
  assert.match(appSource, /authenticatedBlob/);
  assert.match(appSource, /frameObjectUrl = replaceObjectUrl\(frameObjectUrl, result\.blob\)/);
  assert.match(appSource, /iaViewer\.objectUrl = replaceObjectUrl\(iaViewer\.objectUrl, result\.blob\)/);
  assert.doesNotMatch(appSource, /response\.blob\(\)/);
});

test("desktop windows are sandboxed with default-deny navigation and permissions", () => {
  assert.match(mainSource, /sandbox: true/g);
  assert.match(mainSource, /nodeIntegration: false/g);
  assert.match(mainSource, /webviewTag: false/g);
  assert.match(mainSource, /setPermissionCheckHandler[\s\S]+shouldGrantTrustedAppPermission/);
  assert.match(mainSource, /setPermissionRequestHandler[\s\S]+shouldGrantTrustedAppPermission/);
  assert.match(mainSource, /will-frame-navigate/);
  assert.match(mainSource, /will-attach-webview/);
  assert.match(mainSource, /will-navigate/);
  assert.match(mainSource, /will-redirect/);
  assert.match(mainSource, /denyRendererNavigation\(startupWin\)/);
  assert.match(mainSource, /denyRendererNavigation\(updaterWin\)/);
  assert.match(mainSource, /path: "\/healthz"/);
  const openHandler = block(mainSource,
    "mainWindow.webContents.setWindowOpenHandler", "// The 'close' event");
  assert.match(openHandler, /isSidecarApiUrl\(url, sidecarOrigin\(\)\).*action: "deny"/s);
  assert.doesNotMatch(openHandler, /createAuthenticatedResourceWindow/);
  assert.doesNotMatch(openHandler, /action: "allow"|protocol === "blob:"/);
});

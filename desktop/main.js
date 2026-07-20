// Electron main process for Library Tool.
//
// The whole app is the existing Flask backend ("the sidecar") plus a thin
// Electron shell that (1) spawns the sidecar bound to loopback on a free port
// with a writable per-user data root, (2) waits for it to answer, and (3)
// loads it in a BrowserWindow. In dev the sidecar is the Python source; in a
// packaged build it is the PyInstaller-frozen exe shipped in resources/sidecar.
//
// The backend already supports everything this needs: WHL_PORT chooses the
// port, WHL_DATA_ROOT relocates all writable state, and when frozen
// (sys.frozen) libcommon reads shipped assets from the bundle and writes state
// to the per-user dir. So the shell stays tiny.

const { app, BrowserWindow, dialog, shell, Menu, ipcMain } = require("electron");
const { spawn } = require("child_process");
const path = require("path");
const http = require("http");
const net = require("net");
const fs = require("fs");
const crypto = require("crypto");

let sidecar = null;
let mainWindow = null;
let startupWin = null;        // immediate launch feedback while the sidecar/UI load
let startupClosing = false;
let updaterWin = null;        // frameless update splash, shown only while updating
let sidecarPort = null;
let sidecarCapability = null;
let mainReady = false;        // gates window-all-closed: don't quit mid-startup

const DESKTOP_CAPABILITY_HEADER = "X-WHL-Desktop-Capability";
const DESKTOP_CAPABILITY_RE = /^[A-Za-z0-9_-]{43}$/;
const authenticatedResourceLoads = new Map();
const resourceWindows = new Set();

const isDev = !app.isPackaged;

// --- Testable desktop transport policy ----------------------------------------
function createDesktopCapability(randomBytes = crypto.randomBytes) {
  const capability = randomBytes(32).toString("base64url");
  if (!DESKTOP_CAPABILITY_RE.test(capability)) {
    throw new Error("could not create desktop transport capability");
  }
  return capability;
}

function parseUrl(value) {
  try { return new URL(value); } catch (e) { return null; }
}

function isTrustedAppDocumentUrl(value, origin) {
  const url = parseUrl(value);
  return !!url && url.origin === origin && url.pathname === "/" &&
    !url.search && !url.username && !url.password;
}

function isSidecarApiUrl(value, origin) {
  const url = parseUrl(value);
  return !!url && url.origin === origin && url.pathname.startsWith("/api/") &&
    !url.username && !url.password;
}

function classifyAuthenticatedResource(value, origin) {
  let target;
  try { target = new URL(value, origin + "/"); } catch (e) { return null; }
  if (target.origin !== origin || target.username || target.password) return null;
  target.hash = "";
  const keys = Array.from(target.searchParams.keys());
  const hasOnly = (...allowed) => keys.every((key) => allowed.includes(key)) &&
    keys.every((key) => target.searchParams.getAll(key).length === 1);

  if (target.pathname === "/api/pdf") {
    if (!hasOnly("path", "url", "preview", "pages")) return null;
    const pathValue = target.searchParams.get("path");
    const urlValue = target.searchParams.get("url");
    if (!!pathValue === !!urlValue) return null;
    const preview = target.searchParams.get("preview");
    if (target.searchParams.has("preview") && preview !== "1") return null;
    if (target.searchParams.has("pages")) {
      const pages = target.searchParams.get("pages");
      if (preview !== "1" || !/^(?:[1-9]|[1-9][0-9]|[1-4][0-9]{2}|500)$/.test(pages)) {
        return null;
      }
    }
    return { url: target.href, mode: "exact-pdf" };
  }
  if (/^\/api\/builds\/[^/]+\/replica-print$/.test(target.pathname)) {
    if (!hasOnly("src", "layer")) return null;
    return { url: target.href, mode: "one-shot" };
  }
  if (/^\/api\/builds\/[^/]+\/ocr\/images\/[^/]+$/.test(target.pathname)) {
    if (keys.length) return null;
    return { url: target.href, mode: "one-shot" };
  }
  if (target.pathname === "/api/capture/image") {
    if (!hasOnly("path") || !target.searchParams.get("path")) return null;
    return { url: target.href, mode: "one-shot" };
  }
  return null;
}

const TRUSTED_APP_PERMISSIONS = new Set([
  "clipboard-read",
  "clipboard-sanitized-write",
]);

function shouldGrantTrustedAppPermission(permission, webContents, details, trust) {
  return !!trust && TRUSTED_APP_PERMISSIONS.has(permission) &&
    webContents === trust.webContents && !!details && details.isMainFrame === true &&
    isTrustedAppDocumentUrl(details.requestingUrl, trust.origin);
}

function shouldAuthorizeApiRequest(details, trust) {
  if (!details || !trust || !isSidecarApiUrl(details.url, trust.origin)) return false;
  if (details.webContentsId !== trust.webContentsId) return false;
  if (trust.exactResourceUrl) {
    // Chromium's PDF viewer performs follow-up Range requests from an internal
    // frame. The child webContents is the boundary: permit only GETs for the
    // byte-for-byte identical PDF URL and nothing else for its lifetime.
    return details.method === "GET" && details.url === trust.exactResourceUrl;
  }
  if (details.frame !== trust.mainFrame) return false;
  if (trust.oneShotUrl) {
    if (details.method !== "GET") return false;
    if (details.resourceType === "mainFrame") return details.url === trust.oneShotUrl;
    const grant = parseUrl(trust.oneShotUrl);
    const requested = parseUrl(details.url);
    const grantBuild = grant && grant.pathname.match(
      /^\/api\/builds\/([^/]+)\/replica-print$/);
    const requestedBuild = requested && requested.pathname.match(
      /^\/api\/builds\/([^/]+)\/ocr\/images\/[^/]+$/);
    return details.resourceType === "image" && details.frame.url === trust.oneShotUrl &&
      !!grantBuild && !!requestedBuild && grantBuild[1] === requestedBuild[1];
  }
  return isTrustedAppDocumentUrl(details.frame && details.frame.url, trust.origin);
}

function capabilityHeaders(requestHeaders, capability, authorize) {
  const headers = Object.assign({}, requestHeaders || {});
  for (const name of Object.keys(headers)) {
    if (name.toLowerCase() === DESKTOP_CAPABILITY_HEADER.toLowerCase()) delete headers[name];
  }
  if (authorize) headers[DESKTOP_CAPABILITY_HEADER] = capability;
  return headers;
}

function createRequestChainTracker(maxEntries = 4096) {
  if (!Number.isInteger(maxEntries) || maxEntries < 1) {
    throw new Error("request-chain capacity must be a positive integer");
  }
  const chains = new Map();
  const validId = (details) => details && Number.isInteger(details.id) && details.id >= 0;
  const timestamp = (details) => Number.isFinite(details.timestamp) ? details.timestamp : 0;

  return {
    observe(details) {
      if (!validId(details) || typeof details.url !== "string") return false;
      const existing = chains.get(details.id);
      if (existing) {
        // onBeforeRequest is called again for a redirect. Treat any duplicate
        // observation as tainted too: an unexpected re-entrant lifecycle must
        // fail closed, never be mistaken for a fresh authorized request.
        existing.redirected = true;
        existing.currentUrl = details.url;
        existing.lastTimestamp = timestamp(details);
        return false;
      }
      if (chains.size >= maxEntries) return false;
      chains.set(details.id, {
        initialUrl: details.url,
        currentUrl: details.url,
        webContentsId: details.webContentsId,
        frame: details.frame,
        redirected: false,
        lastTimestamp: timestamp(details),
      });
      return true;
    },

    redirect(details) {
      if (!validId(details)) return false;
      const chain = chains.get(details.id);
      if (!chain) return false;
      chain.redirected = true;
      chain.currentUrl = typeof details.redirectURL === "string"
        ? details.redirectURL : String(details.url || "");
      chain.lastTimestamp = timestamp(details);
      return true;
    },

    hasAuthorizedOrigin(details, origin) {
      if (!validId(details) || typeof details.url !== "string") return false;
      const chain = chains.get(details.id);
      return !!chain && !chain.redirected && chain.initialUrl === details.url &&
        chain.currentUrl === details.url &&
        chain.webContentsId === details.webContentsId && chain.frame === details.frame &&
        isSidecarApiUrl(chain.initialUrl, origin);
    },

    finish(details) {
      if (!validId(details)) return false;
      const chain = chains.get(details.id);
      if (!chain) return false;
      // A late completion from a reused request id must not erase its successor.
      if (timestamp(details) < chain.lastTimestamp) {
        return false;
      }
      chains.delete(details.id);
      return true;
    },

    size() { return chains.size; },
  };
}
// --- End testable desktop transport policy ------------------------------------

function sidecarOrigin() {
  return `http://127.0.0.1:${sidecarPort}`;
}

function isTrustedMainSender(event) {
  return !!mainWindow && !mainWindow.isDestroyed() && event &&
    event.sender === mainWindow.webContents &&
    event.senderFrame === mainWindow.webContents.mainFrame &&
    isTrustedAppDocumentUrl(event.senderFrame.url, sidecarOrigin());
}

// --- .lib open flow ------------------------------------------------------------
// A double-clicked .lib (the NSIS file association) arrives as an argv entry on
// first launch, through second-instance argv when the app is already running,
// or through open-file on macOS. The path is queued until the renderer says it
// has registered its handler ("lib:ready", sent from app.js init), then
// delivered over "lib:open" — the renderer owns the create-vs-import dialog.
let pendingLibPaths = [];    // a QUEUE: multi-select + Enter opens one per file
let libReadySender = null;   // the webContents that last signalled lib:ready

function libPathFromArgv(argv, cwd) {
  // Electron/Chromium switches ride the same array; a .lib path is the only
  // argument shape we accept, resolved against the caller's cwd (a shell
  // passes a relative path when launched from the file's own folder).
  for (let i = argv.length - 1; i >= 1; i--) {
    const a = argv[i];
    if (typeof a !== "string" || !/\.lib$/i.test(a) || a.startsWith("--")) continue;
    const p = path.resolve(cwd || process.cwd(), a);
    try { if (fs.statSync(p).isFile()) return p; } catch (e) { /* not a file */ }
  }
  return null;
}

function flushLibOpen() {
  if (!libReadySender || libReadySender.isDestroyed()) return;
  while (pendingLibPaths.length) {
    libReadySender.send("lib:open", pendingLibPaths.shift());
  }
}

function sendLibOpen(p) {
  if (!p) return;
  pendingLibPaths.push(p);
  flushLibOpen();
}

ipcMain.on("lib:ready", (event) => {
  if (!isTrustedMainSender(event)) return;
  libReadySender = event.sender;   // re-set on reload, so delivery stays live
  // a webContents survives navigation/reload, so isDestroyed() alone can't tell
  // that the listener's isolated world is gone. Drop the sender when it starts
  // loading, so a mid-reload flush keeps the path QUEUED for the next lib:ready
  // rather than sending into a page whose handler is not yet registered.
  event.sender.once("did-start-loading", () => {
    if (libReadySender === event.sender) libReadySender = null;
  });
  flushLibOpen();
});

// macOS delivers opened files as an event, not argv; register before ready.
app.on("open-file", (event, p) => {
  event.preventDefault();
  if (/\.lib$/i.test(p)) sendLibOpen(p);
});

// the file the user double-clicked to launch us, if any
{
  const p0 = libPathFromArgv(process.argv, process.cwd());
  if (p0) pendingLibPaths.push(p0);
}

// Only one packaged instance may run at a time. A second launch hands off to
// the first (focusing its window) and exits immediately. This is what makes an
// in-place update safe: NSIS cannot replace a running .exe, so a second
// instance open during an update is exactly the failure this avoids. Dev
// (electron .) is exempt, so a dev instance and the installed app can still run
// side by side.
const gotSingleInstanceLock = isDev || app.requestSingleInstanceLock();
if (!gotSingleInstanceLock) {
  app.quit();
} else {
  app.on("second-instance", (_event, argv, workingDirectory) => {
    // a second launch may BE a double-clicked .lib — hand its path over
    const p = libPathFromArgv(argv || [], workingDirectory);
    if (p) sendLibOpen(p);
    const win = mainWindow || startupWin || updaterWin;
    if (!win) return;
    if (win.isMinimized()) win.restore();
    win.focus();
  });
}

// custom title-bar controls (the window is frameless) driven from the renderer
ipcMain.on("win:minimize", (event) => {
  if (isTrustedMainSender(event)) mainWindow.minimize();
});
ipcMain.on("win:toggle-maximize", (event) => {
  if (!isTrustedMainSender(event)) return;
  if (!mainWindow) return;
  if (mainWindow.isMaximized()) mainWindow.unmaximize();
  else mainWindow.maximize();
});
ipcMain.on("win:close", (event) => {
  if (isTrustedMainSender(event)) mainWindow.close();
});

// Splash renderers signal only after their embedded icon and font have loaded,
// preventing a visible fallback-font or blank-icon frame.
ipcMain.on("startup:ready", (event) => {
  if (startupWin && !startupWin.isDestroyed() && startupWin.webContents === event.sender &&
      startupWin.webContents.mainFrame === event.senderFrame) {
    startupWin.show();
  }
});
ipcMain.on("updater:ready", (event) => {
  if (updaterWin && !updaterWin.isDestroyed() && updaterWin.webContents === event.sender &&
      updaterWin.webContents.mainFrame === event.senderFrame) {
    updaterWin.show();
    closeStartupWindow();
  }
});

// open a web link in the OS browser (the renderer routes external links here so
// they don't get trapped in the app). Re-validate the scheme: shell.openExternal
// will happily launch file:, smb:, mailto: handlers, so only http(s) passes.
ipcMain.on("win:open-external", (event, url) => {
  if (!isTrustedMainSender(event)) return;
  const target = typeof url === "string" ? parseUrl(url) : null;
  if (target && (target.protocol === "http:" || target.protocol === "https:") &&
      !target.username && !target.password && target.origin !== sidecarOrigin()) {
    shell.openExternal(target.href);
  }
});

// Opening an authenticated top-level resource is privileged. The preload is
// main-frame-only and this handler independently verifies the sender and a
// deliberately small GET-only route allowlist before minting a window grant.
ipcMain.on("resource:open", (event, url) => {
  if (!isTrustedMainSender(event) || typeof url !== "string") return;
  createAuthenticatedResourceWindow(url);
});

// a free loopback port so multiple installs / a running dev server never clash
function freePort() {
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.unref();
    srv.on("error", reject);
    srv.listen(0, "127.0.0.1", () => {
      const p = srv.address().port;
      srv.close(() => resolve(p));
    });
  });
}

// The sidecar's origin (http://127.0.0.1:<port>) keys the renderer's
// localStorage, HTTP cache, and V8 compiled-code cache. A fresh ephemeral port
// every launch silently discarded all three: the ~620 KB app.js re-parsed from
// cold and the localStorage fast-path started empty on every run. Persist the
// first port we pick and reuse it on later launches; if another process holds
// it we fall back to a new free one and persist that instead. Still
// loopback-only, and the server Host-guards against DNS rebinding.
//
// Dev and the installed app share userData (%APPDATA%\Library Tool) and dev is
// exempt from the single-instance lock, so they can run side by side — a
// SHARED port file would make them steal each other's port (churning the
// packaged origin and defeating this cache exactly on a dev machine). Each
// kind therefore persists its own file.
function portFilePath() {
  return path.join(app.getPath("userData"),
    isDev ? "sidecar-port-dev.json" : "sidecar-port.json");
}
function readPreferredPort() {
  try {
    const p = JSON.parse(fs.readFileSync(portFilePath(), "utf8")).port;
    return Number.isInteger(p) && p >= 1024 && p <= 65535 ? p : null;
  } catch (e) {
    return null;                            // first run / unreadable
  }
}
function writePreferredPort(port) {
  try { fs.writeFileSync(portFilePath(), JSON.stringify({ port })); } catch (e) {}
}
function portIsFree(port) {
  return new Promise((resolve) => {
    const srv = net.createServer();
    srv.unref();
    srv.on("error", () => resolve(false));
    srv.listen(port, "127.0.0.1", () => srv.close(() => resolve(true)));
  });
}
async function choosePort() {
  const preferred = readPreferredPort();
  if (preferred && await portIsFree(preferred)) return preferred;
  const p = await freePort();
  writePreferredPort(p);
  return p;
}

function sidecarCommand(port, dataRoot, capability) {
  if (!DESKTOP_CAPABILITY_RE.test(capability || "")) {
    throw new Error("desktop transport capability is required");
  }
  const env = Object.assign({}, process.env, {
    WHL_PORT: String(port),
    WHL_DATA_ROOT: dataRoot,
    WHL_APP_VERSION: app.getVersion(),   // so the UI shows the real shell version
    WHL_DESKTOP_MODE: isDev ? "development" : "packaged",
    WHL_DESKTOP_CAPABILITY: capability,
  });
  if (isDev) {
    // dev: run the Python source straight from the repo (../tools/...)
    const repo = path.resolve(__dirname, "..");
    return {
      cmd: process.env.WHL_PYTHON || (process.platform === "win32" ? "python" : "python3"),
      args: [path.join(repo, "tools", "whl_explorer", "server.py")],
      opts: { cwd: repo, env, windowsHide: true },
    };
  }
  // packaged: the frozen onedir sidecar lives in resources/sidecar/
  const exeName = process.platform === "win32"
    ? "whl-explorer-sidecar.exe" : "whl-explorer-sidecar";
  const exe = path.join(process.resourcesPath, "sidecar", exeName);
  return { cmd: exe, args: [], opts: { env, windowsHide: true } };
}

// --- Testable process/window lifecycle guards ---------------------------------
// Keep these helpers free of Electron globals so their race behavior can be
// exercised under plain Node in CI.
function createSingleFlightGate() {
  let active = false;
  return {
    enter() {
      if (active) return false;
      active = true;
      return true;
    },
    leave() { active = false; },
    isActive() { return active; },
  };
}

function superviseChildProcess(child, readiness, onUnexpectedEnd) {
  let ready = false;
  let ended = false;
  let rejectStartup;
  const startupFailure = new Promise((_resolve, reject) => { rejectStartup = reject; });

  const finish = (end) => {
    if (ended) return;
    ended = true;
    if (ready) {
      onUnexpectedEnd(end);
      return;
    }
    if (end.type === "error") {
      rejectStartup(new Error(`Could not launch the backend: ${end.error.message}`));
      return;
    }
    const reason = end.code !== null && end.code !== undefined
      ? `code ${end.code}`
      : `signal ${end.signal || "unknown"}`;
    rejectStartup(new Error(`The backend exited before it became ready (${reason}).`));
  };

  child.once("error", (error) => finish({ type: "error", error }));
  child.once("exit", (code, signal) => finish({ type: "exit", code, signal }));

  return Promise.race([Promise.resolve(readiness), startupFailure]).then((value) => {
    ready = true;
    return value;
  });
}
// --- End testable process/window lifecycle guards -----------------------------

// tiny loopback JSON helpers for the quit guard (no fetch dep in main)
function sidecarJson(method, apiPath, timeoutMs) {
  return new Promise((resolve, reject) => {
    if (!DESKTOP_CAPABILITY_RE.test(sidecarCapability || "")) {
      reject(new Error("desktop transport is unavailable"));
      return;
    }
    const req = http.request({
      host: "127.0.0.1", port: sidecarPort, path: apiPath, method,
      timeout: timeoutMs || 1500,
      headers: { [DESKTOP_CAPABILITY_HEADER]: sidecarCapability },
    }, (res) => {
      let body = "";
      res.on("data", (d) => { body += d; });
      res.on("end", () => {
        try { resolve(JSON.parse(body)); } catch (e) { reject(e); }
      });
    });
    req.on("error", reject);
    req.on("timeout", () => req.destroy(new Error("timed out")));
    req.end();
  });
}

// Closing the window used to kill active OCR/analysis/publish work silently.
// Ask the sidecar what is still running; if anything is, the user chooses:
// Wait (abort the quit), Cancel all and quit (cooperative cancel, bounded
// wait), or Quit anyway. An unreachable sidecar means a normal quit.
let closingThrough = false;
const closeConfirmGate = createSingleFlightGate();

async function confirmCloseWithJobs() {
  let active = null;
  try {
    active = await sidecarJson("GET", "/api/jobs/active", 1500);
  } catch (e) {
    active = null;                       // sidecar gone: nothing to protect
  }
  if (!active || !active.count) return true;
  const labels = (active.labels || []).slice(0, 8);
  const pick = dialog.showMessageBoxSync(mainWindow, {
    type: "warning",
    title: "Library Tool",
    message: `${active.count} task${active.count === 1 ? " is" : "s are"} still running`,
    detail: labels.join("\n") +
      "\n\nCancelling stops each task at its next safe point; pages and " +
      "stages already saved are kept.",
    buttons: ["Wait", "Cancel all and quit", "Quit anyway"],
    defaultId: 0,
    cancelId: 0,
    noLink: true,
  });
  if (pick === 0) return false;          // Wait: abort the quit
  if (pick === 1) {
    const cancellable = (active.jobs || []).filter((j) => j.cancellable && j.id);
    await Promise.all(cancellable.map((j) =>
      sidecarJson("POST", `/api/jobs/${encodeURIComponent(j.id)}/cancel`, 1500)
        .catch(() => {})));
    const deadline = Date.now() + 5000;  // bounded: quit happens regardless
    while (Date.now() < deadline) {
      try {
        const a = await sidecarJson("GET", "/api/jobs/active", 1000);
        if (!a.count) break;
      } catch (e) { break; }
      await new Promise((r) => setTimeout(r, 400));
    }
  }
  return true;                           // Quit anyway / after cancelling
}

// poll the sidecar's public readiness endpoint until it answers (or we give up)
function waitForServer(port, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  return new Promise((resolve, reject) => {
    const tick = () => {
      const req = http.get({ host: "127.0.0.1", port, path: "/healthz", timeout: 1500 }, (res) => {
        res.resume();
        if (res.statusCode === 200) resolve();
        else if (Date.now() > deadline) reject(new Error("backend readiness was rejected"));
        else setTimeout(tick, 300);
      });
      req.on("error", () => {
        if (Date.now() > deadline) reject(new Error("backend did not start in time"));
        else setTimeout(tick, 300);
      });
      req.on("timeout", () => req.destroy());
    };
    tick();
  });
}

async function startSidecar() {
  const dataRoot = app.getPath("userData");   // %APPDATA%\Library Tool
  fs.mkdirSync(dataRoot, { recursive: true });
  sidecarPort = await choosePort();
  sidecarCapability = createDesktopCapability();
  const { cmd, args, opts } = sidecarCommand(sidecarPort, dataRoot, sidecarCapability);
  sidecar = spawn(cmd, args, opts);
  // The update gate may commit to installing while we were awaiting the port:
  // before-quit has already run (nothing to kill then), so reap the child here.
  if (app.isQuitting) { try { sidecar.kill(); } catch (e) { /* already gone */ } }
  sidecar.stdout.on("data", (d) => process.stdout.write(`[sidecar] ${d}`));
  sidecar.stderr.on("data", (d) => process.stderr.write(`[sidecar] ${d}`));
  await superviseChildProcess(sidecar, waitForServer(sidecarPort, 45000), (end) => {
    if (app.isQuitting) return;
    const reason = end.type === "error"
      ? end.error.message
      : (end.code !== null && end.code !== undefined
        ? `code ${end.code}`
        : `signal ${end.signal || "unknown"}`);
    dialog.showErrorBox("Library Tool", `The backend exited unexpectedly (${reason}).`);
    app.quit();
  });
}

let startupStatus = "Preparing";
let cachedSplashAssets = null;

function readSplashAssets() {
  if (cachedSplashAssets) return cachedSplashAssets;
  const asset = (name, devPath, mimeType) => {
    const packaged = path.join(process.resourcesPath, "startup-assets", name);
    const assetPath = fs.existsSync(packaged) ? packaged : devPath;
    return `data:${mimeType};base64,${fs.readFileSync(assetPath).toString("base64")}`;
  };
  cachedSplashAssets = {
    icon: asset("icon.png", path.join(__dirname, "..", "icon.png"), "image/png"),
    font: asset("roboto-slab-var.woff2", path.join(
      __dirname, "..", "tools", "whl_explorer", "static", "fonts",
      "roboto-slab-var.woff2"), "font/woff2"),
  };
  return cachedSplashAssets;
}

function sendStartupStatus(message) {
  startupStatus = message;
  if (startupWin && !startupWin.isDestroyed()) {
    startupWin.webContents.send("startup:status", message);
  }
}

function createStartupWindow(theme) {
  startupWin = new BrowserWindow({
    width: 420, height: 148,
    resizable: false, movable: true, minimizable: false, maximizable: false,
    center: true, frame: false, roundedCorners: false, show: false, skipTaskbar: false,
    title: "Library Tool",
    backgroundColor: "#fbf7ee",
    webPreferences: {
      preload: path.join(__dirname, "startup-preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webSecurity: true,
      allowRunningInsecureContent: false,
      webviewTag: false,
      devTools: isDev,
    },
  });
  denyUnrequestedPermissions(startupWin.webContents.session);
  denyRendererNavigation(startupWin);
  startupWin.webContents.on("will-attach-webview", (event) => event.preventDefault());
  startupWin.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
  startupWin.webContents.on("did-finish-load", () => {
    if (!startupWin || startupWin.isDestroyed()) return;
    startupWin.webContents.send("startup:assets", readSplashAssets());
    startupWin.webContents.send("startup:theme", theme);
    startupWin.webContents.send("startup:version", app.getVersion());
    startupWin.webContents.send("startup:status", startupStatus);
  });
  // With no frame there is no close button, but Alt+F4 still exists. Keep the
  // splash alive until startup deliberately hands off to another window.
  startupWin.on("close", (event) => {
    if (!startupClosing && !app.isQuitting) event.preventDefault();
  });
  startupWin.on("closed", () => { startupWin = null; startupClosing = false; });
  startupWin.loadFile(path.join(__dirname, "startup.html"));
}

function closeStartupWindow() {
  if (startupWin && !startupWin.isDestroyed()) {
    startupClosing = true;
    startupWin.close();
  }
  startupWin = null;
}

const hardenedSessions = new WeakSet();

function denyUnrequestedPermissions(electronSession) {
  if (hardenedSessions.has(electronSession)) return;
  hardenedSessions.add(electronSession);
  const trust = () => mainWindow && !mainWindow.isDestroyed() ? {
    origin: sidecarOrigin(),
    webContents: mainWindow.webContents,
  } : null;
  electronSession.setPermissionCheckHandler((webContents, permission, _origin, details) =>
    shouldGrantTrustedAppPermission(permission, webContents, details, trust()));
  electronSession.setPermissionRequestHandler((webContents, permission, callback, details) => {
    callback(shouldGrantTrustedAppPermission(
      permission, webContents, details, trust()));
  });
}

function denyRendererNavigation(win) {
  win.webContents.on("will-navigate", (details) => details.preventDefault());
  win.webContents.on("will-redirect", (details) => details.preventDefault());
  win.webContents.on("will-frame-navigate", (details) => details.preventDefault());
}

function installApiCapabilityTransport(win) {
  const origin = sidecarOrigin();
  const electronSession = win.webContents.session;
  const allRequests = { urls: ["<all_urls>"] };
  const requestChains = createRequestChainTracker();
  electronSession.webRequest.onBeforeRequest(allRequests, (details, callback) => {
    requestChains.observe(details);
    callback({});
  });
  electronSession.webRequest.onBeforeRedirect(allRequests, (details) => {
    requestChains.redirect(details);
  });
  electronSession.webRequest.onCompleted(allRequests, (details) => {
    requestChains.finish(details);
  });
  electronSession.webRequest.onErrorOccurred(allRequests, (details) => {
    requestChains.finish(details);
  });
  electronSession.webRequest.onBeforeSendHeaders(
    // Match every request so a renderer-supplied spoof of our private header is
    // stripped even when its destination is not the sidecar.
    allRequests,
    (details, callback) => {
      const originalApiRequest = requestChains.hasAuthorizedOrigin(details, origin);
      let authorize = false;
      if (originalApiRequest && mainWindow && !mainWindow.isDestroyed()) {
        authorize = shouldAuthorizeApiRequest(details, {
          origin,
          webContentsId: mainWindow.webContents.id,
          mainFrame: mainWindow.webContents.mainFrame,
        });
      }
      if (!authorize && originalApiRequest) {
        const resource = authenticatedResourceLoads.get(details.webContentsId);
        if (resource) {
          authorize = shouldAuthorizeApiRequest(details, {
            origin,
            webContentsId: details.webContentsId,
            mainFrame: resource.mainFrame,
            oneShotUrl: resource.mode === "one-shot" ? resource.url : null,
            exactResourceUrl: resource.mode === "exact-pdf" ? resource.url : null,
          });
        }
      }
      callback({
        requestHeaders: capabilityHeaders(
          details.requestHeaders, sidecarCapability, authorize),
      });
    },
  );
}

function openExternalUrl(value) {
  const target = parseUrl(value);
  if (!target || !["http:", "https:"].includes(target.protocol) ||
      target.username || target.password || target.origin === sidecarOrigin()) return false;
  shell.openExternal(target.href);
  return true;
}

function createAuthenticatedResourceWindow(value) {
  if (!mainWindow || mainWindow.isDestroyed()) return false;
  const resource = classifyAuthenticatedResource(value, sidecarOrigin());
  if (!resource) return false;
  const networkUrl = resource.url;
  const grantMode = resource.mode;
  const child = new BrowserWindow({
    parent: mainWindow,
    width: 1000,
    height: 800,
    title: "Library Tool",
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webSecurity: true,
      allowRunningInsecureContent: false,
      webviewTag: false,
      devTools: isDev,
    },
  });
  resourceWindows.add(child);
  denyUnrequestedPermissions(child.webContents.session);
  authenticatedResourceLoads.set(child.webContents.id, {
    url: networkUrl,
    mainFrame: child.webContents.mainFrame,
    mode: grantMode,
  });
  const clearGrant = () => authenticatedResourceLoads.delete(child.webContents.id);
  if (grantMode === "one-shot") {
    child.webContents.once("did-finish-load", clearGrant);
    child.webContents.once("did-fail-load", clearGrant);
  }
  child.webContents.on("will-navigate", (event, url) => {
    if (url !== networkUrl) {
      event.preventDefault();
      openExternalUrl(url);
    }
  });
  child.webContents.on("will-redirect", (event, url) => {
    if (url !== networkUrl) {
      event.preventDefault();
      openExternalUrl(url);
    }
  });
  child.webContents.on("will-attach-webview", (event) => event.preventDefault());
  child.webContents.setWindowOpenHandler(({ url }) => {
    openExternalUrl(url);
    return { action: "deny" };
  });
  child.on("closed", () => {
    clearGrant();
    resourceWindows.delete(child);
  });
  child.loadURL(networkUrl);
  return true;
}

function createWindow() {
  sendStartupStatus("Opening library");
  mainWindow = new BrowserWindow({
    // These are the RESTORED (un-maximized) bounds. The app launches
    // maximized (below), and this is the reasonable window it drops back to
    // when the user hits the restore button — deliberately well short of a
    // full screen so "windowed" actually looks windowed.
    width: 1200, height: 800, minWidth: 900, minHeight: 600,
    show: false,            // shown once maximized, so there's no small-window flash
    title: "Library Tool",
    backgroundColor: "#1d1f21",
    frame: false,           // no OS chrome — the web UI's title bar is the frame
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webSecurity: true,
      allowRunningInsecureContent: false,
      webviewTag: false,
      devTools: isDev,
    },
  });
  denyUnrequestedPermissions(mainWindow.webContents.session);
  installApiCapabilityTransport(mainWindow);
  Menu.setApplicationMenu(null);   // the web UI has its own menu bar
  // keep the renderer's maximize/restore icon in sync with the real state
  mainWindow.on("maximize", () => mainWindow.webContents.send("win:maximized", true));
  mainWindow.on("unmaximize", () => mainWindow.webContents.send("win:maximized", false));
  // The icon is event-driven and the renderer registers its listener at load,
  // so a maximize() that fired before then would leave a stale icon. Re-send
  // the real state once the page is up (also covers reloads).
  mainWindow.webContents.on("did-finish-load", () => {
    if (mainWindow) mainWindow.webContents.send("win:maximized", mainWindow.isMaximized());
  });
  mainWindow.loadURL(`${sidecarOrigin()}/`);
  // Start maximized. Maximizing while the constructor bounds are set records
  // 1200×800 as the restore target, so the restore button returns there.
  mainWindow.once("ready-to-show", () => {
    if (!mainWindow) return;
    mainWindow.maximize();
    mainWindow.show();
    closeStartupWindow();
  });
  mainWindow.webContents.on("will-navigate", (event, url) => {
    if (!isTrustedAppDocumentUrl(url, sidecarOrigin())) {
      event.preventDefault();
      openExternalUrl(url);
    }
  });
  mainWindow.webContents.on("will-redirect", (event, url) => {
    if (!isTrustedAppDocumentUrl(url, sidecarOrigin())) {
      event.preventDefault();
      openExternalUrl(url);
    }
  });
  // No application credential is ever propagated into a subframe. Local PDF
  // frames use blob URLs created by an authenticated top-frame fetch.
  mainWindow.webContents.on("will-frame-navigate", (details) => {
    if (details.isMainFrame) return;
    const target = parseUrl(details.url);
    if (details.url === "about:blank" ||
        (target && target.protocol === "blob:" && target.origin === sidecarOrigin())) return;
    details.preventDefault();
  });
  mainWindow.webContents.on("will-attach-webview", (event) => event.preventDefault());
  // New-window callbacks do not identify the initiating WebFrameMain. They may
  // open ordinary external links, but can never mint a sidecar API grant; the
  // main-frame-only resource:open IPC above is the sole entry point for that.
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (isSidecarApiUrl(url, sidecarOrigin())) return { action: "deny" };
    if (openExternalUrl(url)) return { action: "deny" };
    return { action: "deny" };
  });
  // The 'close' event is synchronous, so hold it, ask the sidecar about
  // active jobs, and re-close once the user has decided.
  mainWindow.on("close", (event) => {
    if (closingThrough || app.isQuitting) return;
    event.preventDefault();
    if (!closeConfirmGate.enter()) return;
    confirmCloseWithJobs()
      .then((proceed) => {
        if (!proceed || !mainWindow || mainWindow.isDestroyed()) return;
        closingThrough = true;
        mainWindow.close();
      })
      .catch((err) => console.error("[quit guard]", err && err.message))
      .finally(() => closeConfirmGate.leave());
  });
  mainWindow.on("closed", () => {
    mainWindow = null;
    closingThrough = false;
    closeConfirmGate.leave();
  });
  mainReady = true;   // from here a window-all-closed is a real user quit
}

// The persisted UI theme, read straight off disk so the update splash matches
// it before the sidecar (which owns client_state) is even running. Mirrors the
// theme ids in tools/whl_explorer/static/app.js. Custom themes use their base
// chrome here because the compact pre-launch windows do not apply token-level
// overrides; anything else unknown falls back to sage.
const KNOWN_THEMES = new Set([
  "sage", "ledger", "foolscap", "vellum", "linen", "porcelain", "slate",
]);
function prelaunchTheme(settings) {
  const t = settings && settings.theme;
  if (KNOWN_THEMES.has(t)) return t;
  const saved = Array.isArray(settings && settings.savedThemes)
    ? settings.savedThemes : [];
  const custom = saved.find((item) => item && item.id === t);
  return custom && KNOWN_THEMES.has(custom.base) ? custom.base : "sage";
}
function readActiveTheme() {
  try {
    const p = path.join(app.getPath("userData"), "output", "client_state.json");
    const settings = JSON.parse(fs.readFileSync(p, "utf8"))?.settings || {};
    return prelaunchTheme(settings);
  } catch (e) {
    return "sage";                         // first run / unreadable -> default
  }
}

// Update prefs (Settings > Updates), read off client_state the same way the
// theme is — before the sidecar is up. Defaults to auto-update on.
function readUpdatePrefs() {
  try {
    const p = path.join(app.getPath("userData"), "output", "client_state.json");
    const s = JSON.parse(fs.readFileSync(p, "utf8"))?.settings || {};
    return {
      auto: s.autoUpdate !== false,
      prerelease: s.includePrereleaseUpdates === true,
    };
  } catch (e) {
    return { auto: true, prerelease: false };
  }
}

let updaterStatus = null;     // latest {phase, version} pushed to the splash

function sendUpdater(channel, payload) {
  if (updaterWin && !updaterWin.isDestroyed() && updaterWin.webContents) {
    updaterWin.webContents.send(channel, payload);
  }
}

// Set + push the splash status. Storing it means did-finish-load can REPLAY the
// current phase: on a cached/fast update, update-downloaded ("install") can fire
// before the renderer has registered its listeners, so a plain send is dropped —
// the replay ensures the splash lands on "Installing…", not a stale "Downloading".
function setUpdaterStatus(status) {
  updaterStatus = status;
  sendUpdater("updater:status", status);
}

function createUpdaterWindow(theme) {
  updaterWin = new BrowserWindow({
    width: 420, height: 148,
    resizable: false, movable: true, minimizable: false, maximizable: false,
    center: true, frame: false, roundedCorners: false, show: false, skipTaskbar: false,
    title: "Updating Library Tool",
    backgroundColor: "#fbf7ee",
    webPreferences: {
      preload: path.join(__dirname, "updater-preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webSecurity: true,
      allowRunningInsecureContent: false,
      webviewTag: false,
      devTools: isDev,
    },
  });
  denyUnrequestedPermissions(updaterWin.webContents.session);
  denyRendererNavigation(updaterWin);
  updaterWin.webContents.on("will-attach-webview", (event) => event.preventDefault());
  updaterWin.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
  // replay theme + the CURRENT status once the renderer has run its script
  // (listeners registered); show once it has painted — avoids a lost IPC and a
  // white flash.
  updaterWin.webContents.on("did-finish-load", () => {
    sendUpdater("updater:assets", readSplashAssets());
    sendUpdater("updater:theme", theme);
    sendUpdater("updater:version", app.getVersion());
    if (updaterStatus) sendUpdater("updater:status", updaterStatus);
  });
  updaterWin.on("closed", () => { updaterWin = null; });
  updaterWin.loadFile(path.join(__dirname, "updater.html"));
}

function closeUpdaterWindow() {
  if (updaterWin && !updaterWin.isDestroyed()) updaterWin.close();
  updaterWin = null;
  updaterStatus = null;
}

// Auto-update GATE: on startup (packaged only) check GitHub Releases once. If an
// update is waiting, show the frameless progress splash, download it, install
// it, and let NSIS relaunch the new version — the main app never opens on the
// old one. No update, offline, a slow check, or any error just falls through to
// a normal launch, so startup can never hang on the network. Resolves 'launch'
// (open the app now) or 'installing' (we are quitting into the installer).
function runUpdateGate() {
  return new Promise((resolve) => {
    if (isDev) return resolve("launch");   // dev runs from source; updater is inert
    let updater;
    try {
      updater = require("electron-updater").autoUpdater;
    } catch (e) {
      return resolve("launch");            // packaged without the dep: skip
    }

    // Settings > Updates: honour the user's auto-update choice.
    const prefs = readUpdatePrefs();
    if (!prefs.auto) return resolve("launch");    // user disabled auto-update

    let settled = false;
    let checkTimer = null;
    let stallTimer = null;
    const finish = (outcome) => {
      if (settled) return;
      settled = true;
      if (checkTimer) clearTimeout(checkTimer);
      if (stallTimer) clearTimeout(stallTimer);
      if (outcome === "launch") closeUpdaterWindow();
      resolve(outcome);
    };
    // a download that stalls with no bytes and no error must not strand startup
    const armStall = () => {
      if (stallTimer) clearTimeout(stallTimer);
      stallTimer = setTimeout(() => finish("launch"), 120000);
    };

    const theme = readActiveTheme();
    // A machine already running an alpha/beta/rc must keep following that
    // prerelease line automatically. The preference is only the stable-build
    // opt-in; using it as an unconditional override stranded alpha.5 because
    // its default false made electron-updater search only the stable channel.
    const installedPrerelease = app.getVersion().includes("-");
    updater.allowPrerelease = installedPrerelease || prefs.prerelease;
    updater.autoDownload = false;                 // probe first, then decide
    updater.autoInstallOnAppQuit = true;          // if we fall through mid-download, install on next quit

    // The progress listener MUST exist before the download starts (electron-updater
    // only wires onProgress when something is listening), so attach it up front.
    updater.on("download-progress", (p) => { armStall(); sendUpdater("updater:progress", p); });
    updater.on("update-not-available", () => finish("launch"));
    updater.on("error", (err) => {
      console.error("[updater]", err && err.message);
      finish("launch");                           // never block launch on an update error
    });
    updater.on("update-available", (info) => {
      if (settled) return;
      if (checkTimer) { clearTimeout(checkTimer); checkTimer = null; }  // committed: the download sets its own pace
      setUpdaterStatus({ phase: "download", version: info.version });
      createUpdaterWindow(theme);
      armStall();
      updater.downloadUpdate().catch((err) => {
        console.error("[updater] download", err && err.message);
        finish("launch");
      });
    });
    updater.on("update-downloaded", (info) => {
      if (settled) return;
      settled = true;                             // committed to installing; the splash rides until quit
      if (checkTimer) clearTimeout(checkTimer);
      if (stallTimer) clearTimeout(stallTimer);
      setUpdaterStatus({ phase: "install", version: info.version });
      app.isQuitting = true;
      // a short beat so "Installing…" paints before the app tears down.
      // quitAndInstall(isSilent=true, isForceRunAfter=true): /S runs the NSIS
      // installer with no wizard and no clicks, then --force-run relaunches the
      // new version. perMachine:false keeps it a per-user install, so no UAC.
      setTimeout(() => updater.quitAndInstall(true, true), 500);
      resolve("installing");
    });

    checkTimer = setTimeout(() => finish("launch"), 8000);   // slow/hung check -> just launch
    updater.checkForUpdates().catch((err) => {
      console.error("[updater] check", err && err.message);
      finish("launch");
    });
  });
}

app.whenReady().then(async () => {
  if (!gotSingleInstanceLock) return;   // a second instance — it is already quitting
  createStartupWindow(readActiveTheme());
  sendStartupStatus(isDev ? "Preparing local services" : "Checking for updates");
  // The two slowest startup steps — the GitHub update check (up to its 8s cap
  // on a bad network) and the PyInstaller sidecar cold start (multi-second) —
  // are independent, so run them CONCURRENTLY instead of serially. If the gate
  // resolves 'installing', the young sidecar is reaped (before-quit kills it,
  // and startSidecar reaps a spawn that lands after that), so NSIS never races
  // a running exe. The catch below keeps a sidecar failure during the check
  // from becoming an unhandled rejection; the launch branch still awaits the
  // same promise, so the existing failure dialog is preserved.
  const sidecarPromise = startSidecar();
  sidecarPromise.catch(() => { /* surfaced via the await below on launch */ });
  let outcome = "launch";
  try {
    outcome = await runUpdateGate();
  } catch (e) {
    outcome = "launch";
  }
  if (outcome === "installing") return;

  try {
    sendStartupStatus("Loading library service");
    await sidecarPromise;
  } catch (e) {
    closeStartupWindow();
    dialog.showErrorBox("Library Tool", "The local backend failed to start.\n" + e.message +
      (isDev ? "\n\n(Dev mode expects Python on PATH — set WHL_PYTHON to override.)" : ""));
    app.quit();
    return;
  }
  closeUpdaterWindow();     // no-op unless a failed download left the splash up
  createWindow();
  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

// Don't quit while the splash opens and closes during startup; only once the
// real window has existed does closing the last window mean the user is done.
app.on("window-all-closed", () => { if (mainReady) app.quit(); });
app.on("before-quit", () => {
  app.isQuitting = true;
  if (sidecar) { try { sidecar.kill(); } catch (e) { /* already gone */ } }
  authenticatedResourceLoads.clear();
  sidecarCapability = null;
});

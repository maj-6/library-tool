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

let sidecar = null;
let mainWindow = null;
let startupWin = null;        // immediate launch feedback while the sidecar/UI load
let startupClosing = false;
let updaterWin = null;        // frameless update splash, shown only while updating
let sidecarPort = null;
let mainReady = false;        // gates window-all-closed: don't quit mid-startup

const isDev = !app.isPackaged;

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
  app.on("second-instance", () => {
    const win = mainWindow || startupWin || updaterWin;
    if (!win) return;
    if (win.isMinimized()) win.restore();
    win.focus();
  });
}

// custom title-bar controls (the window is frameless) driven from the renderer
ipcMain.on("win:minimize", () => mainWindow && mainWindow.minimize());
ipcMain.on("win:toggle-maximize", () => {
  if (!mainWindow) return;
  if (mainWindow.isMaximized()) mainWindow.unmaximize();
  else mainWindow.maximize();
});
ipcMain.on("win:close", () => mainWindow && mainWindow.close());

// Splash renderers signal only after their embedded icon and font have loaded,
// preventing a visible fallback-font or blank-icon frame.
ipcMain.on("startup:ready", (event) => {
  if (startupWin && !startupWin.isDestroyed() && startupWin.webContents === event.sender) {
    startupWin.show();
  }
});
ipcMain.on("updater:ready", (event) => {
  if (updaterWin && !updaterWin.isDestroyed() && updaterWin.webContents === event.sender) {
    updaterWin.show();
    closeStartupWindow();
  }
});

// open a web link in the OS browser (the renderer routes external links here so
// they don't get trapped in the app). Re-validate the scheme: shell.openExternal
// will happily launch file:, smb:, mailto: handlers, so only http(s) passes.
ipcMain.on("win:open-external", (_e, url) => {
  if (typeof url === "string" && /^https?:\/\//i.test(url)) shell.openExternal(url);
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

function sidecarCommand(port, dataRoot) {
  const env = Object.assign({}, process.env, {
    WHL_PORT: String(port),
    WHL_DATA_ROOT: dataRoot,
    WHL_APP_VERSION: app.getVersion(),   // so the UI shows the real shell version
  });
  if (isDev) {
    // dev: run the Python source straight from the repo (../tools/...)
    const repo = path.resolve(__dirname, "..");
    return {
      cmd: process.env.WHL_PYTHON || (process.platform === "win32" ? "python" : "python3"),
      args: [path.join(repo, "tools", "whl_explorer", "server.py")],
      opts: { cwd: repo, env },
    };
  }
  // packaged: the frozen onedir sidecar lives in resources/sidecar/
  const exeName = process.platform === "win32"
    ? "whl-explorer-sidecar.exe" : "whl-explorer-sidecar";
  const exe = path.join(process.resourcesPath, "sidecar", exeName);
  return { cmd: exe, args: [], opts: { env } };
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
    const req = http.request({
      host: "127.0.0.1", port: sidecarPort, path: apiPath, method,
      timeout: timeoutMs || 1500,
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

// poll the sidecar's "/" until it answers (or we give up)
function waitForServer(port, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  return new Promise((resolve, reject) => {
    const tick = () => {
      const req = http.get({ host: "127.0.0.1", port, path: "/", timeout: 1500 }, (res) => {
        res.resume();
        resolve();
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
  sidecarPort = await freePort();
  const { cmd, args, opts } = sidecarCommand(sidecarPort, dataRoot);
  sidecar = spawn(cmd, args, opts);
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
    },
  });
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
    },
  });
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
  mainWindow.loadURL(`http://127.0.0.1:${sidecarPort}/`);
  // Start maximized. Maximizing while the constructor bounds are set records
  // 1200×800 as the restore target, so the restore button returns there.
  mainWindow.once("ready-to-show", () => {
    if (!mainWindow) return;
    mainWindow.maximize();
    mainWindow.show();
    closeStartupWindow();
  });
  // open target=_blank / external links in the system browser, not a new window
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:/i.test(url)) shell.openExternal(url);
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
// theme ids in tools/whl_explorer/static/app.js; anything unknown -> sage.
const KNOWN_THEMES = new Set([
  "sage", "ledger", "foolscap", "vellum", "linen",
]);
function readActiveTheme() {
  try {
    const p = path.join(app.getPath("userData"), "output", "client_state.json");
    const t = JSON.parse(fs.readFileSync(p, "utf8"))?.settings?.theme;
    return KNOWN_THEMES.has(t) ? t : "sage";
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
    },
  });
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
  // Update first: if one is installing, we quit into NSIS and never launch here.
  let outcome = "launch";
  try {
    outcome = await runUpdateGate();
  } catch (e) {
    outcome = "launch";
  }
  if (outcome === "installing") return;

  try {
    sendStartupStatus("Loading library service");
    await startSidecar();
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
});

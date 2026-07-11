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
let updaterWin = null;        // frameless update splash, shown only while updating
let sidecarPort = null;
let mainReady = false;        // gates window-all-closed: don't quit mid-startup

const isDev = !app.isPackaged;

// custom title-bar controls (the window is frameless) driven from the renderer
ipcMain.on("win:minimize", () => mainWindow && mainWindow.minimize());
ipcMain.on("win:toggle-maximize", () => {
  if (!mainWindow) return;
  if (mainWindow.isMaximized()) mainWindow.unmaximize();
  else mainWindow.maximize();
});
ipcMain.on("win:close", () => mainWindow && mainWindow.close());

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
  sidecar.on("error", (err) => {
    dialog.showErrorBox("Library Tool",
      "Could not launch the backend:\n" + err.message +
      (isDev ? "\n\n(Dev mode expects Python on PATH — set WHL_PYTHON to override.)" : ""));
  });
  sidecar.on("exit", (code) => {
    if (code && !app.isQuitting) {
      dialog.showErrorBox("Library Tool", `The backend exited unexpectedly (code ${code}).`);
      app.quit();
    }
  });
  await waitForServer(sidecarPort, 45000);
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1440, height: 920, minWidth: 900, minHeight: 600,
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
  mainWindow.loadURL(`http://127.0.0.1:${sidecarPort}/`);
  // open target=_blank / external links in the system browser, not a new window
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:/i.test(url)) shell.openExternal(url);
    return { action: "deny" };
  });
  mainWindow.on("closed", () => { mainWindow = null; });
  mainReady = true;   // from here a window-all-closed is a real user quit
}

// The persisted UI theme, read straight off disk so the update splash matches
// it before the sidecar (which owns client_state) is even running. Mirrors the
// theme ids in tools/whl_explorer/static/app.js; anything unknown -> sage.
const KNOWN_THEMES = new Set([
  "sage", "ledger", "foolscap", "vellum", "linen", "platinum",
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
    // frameless, so this height IS the content height; the splash box is ~136px
    // tall, so 140 fits it snugly (no centered blank space above the title)
    width: 460, height: 140,
    resizable: false, movable: false, minimizable: false, maximizable: false,
    center: true, frame: false, show: false, skipTaskbar: false,
    title: "Updating Library Tool",
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
    sendUpdater("updater:theme", theme);
    if (updaterStatus) sendUpdater("updater:status", updaterStatus);
  });
  updaterWin.once("ready-to-show", () => { if (updaterWin) updaterWin.show(); });
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
  // Update first: if one is installing, we quit into NSIS and never launch here.
  let outcome = "launch";
  try {
    outcome = await runUpdateGate();
  } catch (e) {
    outcome = "launch";
  }
  if (outcome === "installing") return;

  try {
    await startSidecar();
  } catch (e) {
    dialog.showErrorBox("Library Tool", "The local backend failed to start.\n" + e.message);
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

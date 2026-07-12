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
let updaterWin = null;        // frameless splash: covers startup, and update progress
let sidecarPort = null;
let mainReady = false;        // gates window-all-closed: don't quit mid-startup
let sidecarRestarts = 0;      // recovery: bounded auto-restart of a crashed backend
let lastSidecarStart = 0;     // when the sidecar last came up (to spot a crash loop)
let sidecarRestarting = false;
const MAX_SIDECAR_RESTARTS = 3;
const SIDECAR_STABLE_MS = 60000;   // ran this long -> a fresh crash isn't the loop

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
    if (!mainWindow) return;
    if (mainWindow.isMinimized()) mainWindow.restore();
    mainWindow.focus();
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

async function startSidecar(timeoutMs = 45000) {
  const dataRoot = app.getPath("userData");   // %APPDATA%\Library Tool
  fs.mkdirSync(dataRoot, { recursive: true });
  sidecarPort = await freePort();
  const { cmd, args, opts } = sidecarCommand(sidecarPort, dataRoot);
  sidecar = spawn(cmd, args, { ...opts, windowsHide: true });   // no console flash on Windows
  sidecar.stdout.on("data", (d) => process.stdout.write(`[sidecar] ${d}`));
  sidecar.stderr.on("data", (d) => process.stderr.write(`[sidecar] ${d}`));
  sidecar.on("error", (err) => {
    dialog.showErrorBox("Library Tool",
      "Could not launch the backend:\n" + err.message +
      (isDev ? "\n\n(Dev mode expects Python on PATH — set WHL_PYTHON to override.)" : ""));
  });
  sidecar.on("exit", (code) => {
    if (code && !app.isQuitting) handleSidecarCrash(code);
  });
  await waitForServer(sidecarPort, timeoutMs);
  lastSidecarStart = Date.now();
}

// The sidecar died while the app was running. Rather than kill the whole app on
// one transient backend crash, bring it back: respawn on a fresh port, wait for
// it, and reload the window there, with a splash over the gap. Bounded — a real
// crash loop (several crashes in quick succession) falls through to error+quit,
// but a lone hiccup after a long stable run resets the budget so it always gets
// a retry. During INITIAL startup (no window yet) we keep the old fail-fast.
function handleSidecarCrash(code) {
  if (!mainReady) {
    dialog.showErrorBox("Library Tool", `The backend exited unexpectedly (code ${code}).`);
    app.quit();
    return;
  }
  if (sidecarRestarting) return;                 // a restart is already in flight
  if (Date.now() - lastSidecarStart > SIDECAR_STABLE_MS) sidecarRestarts = 0;
  if (sidecarRestarts >= MAX_SIDECAR_RESTARTS) {
    dialog.showErrorBox("Library Tool",
      `The backend keeps exiting unexpectedly (code ${code}); giving up after ${sidecarRestarts} restarts.`);
    app.quit();
    return;
  }
  sidecarRestarts++;
  restartSidecar();
}

async function restartSidecar() {
  sidecarRestarting = true;
  setUpdaterStatus({ phase: "restarting" });
  createUpdaterWindow(readActiveTheme());        // a splash over the app while it comes back
  try {
    await startSidecar(20000);                   // shorter wait: a failed restart shouldn't hang
  } catch (e) {
    sidecarRestarting = false;
    if (app.isQuitting) return;
    closeUpdaterWindow();
    dialog.showErrorBox("Library Tool", "The backend could not be restarted.\n" + e.message);
    app.quit();
    return;
  }
  sidecarRestarting = false;
  if (!mainWindow) { closeUpdaterWindow(); return; }
  // reload at the NEW port; close the splash once the fresh page paints, with a
  // safety timeout so a stalled reload can never strand it.
  const done = () => closeUpdaterWindow();
  mainWindow.webContents.once("did-finish-load", done);
  setTimeout(done, 5000);
  mainWindow.loadURL(`http://127.0.0.1:${sidecarPort}/`);
}

function createWindow() {
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
      sandbox: true,          // preload uses only contextBridge/ipcRenderer/process.platform
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
    closeUpdaterWindow();   // hand off from the startup splash to the real window
  });
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
    title: "Library Tool",
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
      // Keep the splash up for the sidecar warm that follows a "launch"; reset
      // it to the neutral "starting" label in case an update download had begun.
      if (outcome === "launch") setUpdaterStatus({ phase: "starting" });
      resolve(outcome);
    };
    // a download that stalls with no bytes and no error must not strand startup
    const armStall = () => {
      if (stallTimer) clearTimeout(stallTimer);
      stallTimer = setTimeout(() => finish("launch"), 120000);
    };

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
  // A themed splash covers the WHOLE startup — the update check and the sidecar
  // warm-up — so a slow start never looks like a blank hang. The same window
  // doubles as the update-progress splash if a download begins, and closes when
  // the main window paints (or rides into the installer if we quit to update).
  // Shown in dev too, where the Python sidecar takes a few seconds to answer.
  setUpdaterStatus({ phase: "starting" });
  createUpdaterWindow(readActiveTheme());

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
    closeUpdaterWindow();
    dialog.showErrorBox("Library Tool", "The local backend failed to start.\n" + e.message);
    app.quit();
    return;
  }
  createWindow();          // the splash closes in the window's ready-to-show
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

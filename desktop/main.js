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
let sidecarPort = null;

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
}

// Auto-update: check GitHub Releases (maj-6/library-tool) once at startup,
// download in the background, and offer a restart when it is ready. Offline or
// rate-limited just means "no update today" — never a dialog.
function startUpdateCheck() {
  if (isDev) return;                       // dev runs from source
  let updater;
  try {
    updater = require("electron-updater").autoUpdater;
  } catch (e) {
    return;                                // packaged without the dep: skip
  }
  updater.autoDownload = true;
  updater.on("error", (err) => console.error("[updater]", err && err.message));
  updater.on("update-downloaded", (info) => {
    // "Later" is the default: this dialog pops at an unpredictable moment, and
    // a buffered Enter from mid-typing must not quit the app into an installer.
    dialog.showMessageBox(mainWindow, {
      type: "info",
      buttons: ["Restart now", "Later"],
      defaultId: 1,
      cancelId: 1,
      title: "Library Tool",
      message: `Library Tool ${info.version} is ready to install.`,
      detail: "It installs when the app restarts. Your catalogue and settings are untouched.",
    }).then(({ response }) => {
      if (response === 0) updater.quitAndInstall();
    });
  });
  updater.checkForUpdates().catch(() => { /* offline; next launch tries again */ });
}

app.whenReady().then(async () => {
  try {
    await startSidecar();
  } catch (e) {
    dialog.showErrorBox("Library Tool", "The local backend failed to start.\n" + e.message);
    app.quit();
    return;
  }
  createWindow();
  startUpdateCheck();
  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on("window-all-closed", () => app.quit());
app.on("before-quit", () => {
  app.isQuitting = true;
  if (sidecar) { try { sidecar.kill(); } catch (e) { /* already gone */ } }
});

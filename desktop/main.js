// Electron main process for Catalog Explorer.
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

const { app, BrowserWindow, dialog, shell, Menu } = require("electron");
const { spawn } = require("child_process");
const path = require("path");
const http = require("http");
const net = require("net");
const fs = require("fs");

let sidecar = null;
let mainWindow = null;
let sidecarPort = null;

const isDev = !app.isPackaged;

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
  const dataRoot = app.getPath("userData");   // %APPDATA%\Catalog Explorer
  fs.mkdirSync(dataRoot, { recursive: true });
  sidecarPort = await freePort();
  const { cmd, args, opts } = sidecarCommand(sidecarPort, dataRoot);
  sidecar = spawn(cmd, args, opts);
  sidecar.stdout.on("data", (d) => process.stdout.write(`[sidecar] ${d}`));
  sidecar.stderr.on("data", (d) => process.stderr.write(`[sidecar] ${d}`));
  sidecar.on("error", (err) => {
    dialog.showErrorBox("Catalog Explorer",
      "Could not launch the backend:\n" + err.message +
      (isDev ? "\n\n(Dev mode expects Python on PATH — set WHL_PYTHON to override.)" : ""));
  });
  sidecar.on("exit", (code) => {
    if (code && !app.isQuitting) {
      dialog.showErrorBox("Catalog Explorer", `The backend exited unexpectedly (code ${code}).`);
      app.quit();
    }
  });
  await waitForServer(sidecarPort, 45000);
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1440, height: 920, minWidth: 900, minHeight: 600,
    title: "Catalog Explorer",
    backgroundColor: "#1d1f21",
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  Menu.setApplicationMenu(null);   // the web UI has its own menu bar
  mainWindow.loadURL(`http://127.0.0.1:${sidecarPort}/`);
  // open target=_blank / external links in the system browser, not a new window
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:/i.test(url)) shell.openExternal(url);
    return { action: "deny" };
  });
  mainWindow.on("closed", () => { mainWindow = null; });
}

app.whenReady().then(async () => {
  try {
    await startSidecar();
  } catch (e) {
    dialog.showErrorBox("Catalog Explorer", "The local backend failed to start.\n" + e.message);
    app.quit();
    return;
  }
  createWindow();
  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on("window-all-closed", () => app.quit());
app.on("before-quit", () => {
  app.isQuitting = true;
  if (sidecar) { try { sidecar.kill(); } catch (e) { /* already gone */ } }
});

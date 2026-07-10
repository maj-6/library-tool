// Preload for the frameless update splash (updater.html). Mirrors preload.js's
// single contextBridge surface, but read-only: the splash only receives events
// from main (theme + status + download progress) and never sends anything back.
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("whlUpdater", {
  // main pushes the persisted theme name once, up front, so the splash can
  // paint itself in the same palette the app will open with.
  onTheme: (cb) => ipcRenderer.on("updater:theme", (_e, name) => cb(name)),
  // { phase: "download" | "install", version } — coarse state for the label.
  onStatus: (cb) => ipcRenderer.on("updater:status", (_e, s) => cb(s)),
  // electron-updater ProgressInfo: { percent, transferred, total, bytesPerSecond }.
  onProgress: (cb) => ipcRenderer.on("updater:progress", (_e, p) => cb(p)),
});

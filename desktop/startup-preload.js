// Narrow, context-isolated bridge for the startup splash. Asset files are read
// by the main process so this preload remains compatible with sandboxing.
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("whlStartup", {
  onAssets: (cb) => ipcRenderer.on("startup:assets", (_event, value) => cb(value)),
  onStatus: (cb) => ipcRenderer.on("startup:status", (_event, value) => cb(String(value))),
  onTheme: (cb) => ipcRenderer.on("startup:theme", (_event, value) => cb(String(value))),
  onVersion: (cb) => ipcRenderer.on("startup:version", (_event, value) => cb(String(value))),
  ready: () => ipcRenderer.send("startup:ready"),
});

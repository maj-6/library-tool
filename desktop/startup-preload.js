// Narrow, context-isolated bridge for the startup splash. The renderer can
// only receive display strings and has no Node or filesystem access.
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("whlStartup", {
  onStatus: (cb) => ipcRenderer.on("startup:status", (_event, value) => cb(String(value))),
  onTheme: (cb) => ipcRenderer.on("startup:theme", (_event, value) => cb(String(value))),
});

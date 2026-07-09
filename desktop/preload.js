// Minimal, context-isolated preload. The UI is the existing web app served by
// the local sidecar, so it needs almost nothing from Electron — we only expose
// a tiny, read-only marker so the page can tell it is running inside the
// desktop shell (e.g. to show a "download databases" affordance).
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("whlDesktop", {
  isDesktop: true,
  platform: process.platform,
  win: {
    minimize: () => ipcRenderer.send("win:minimize"),
    toggleMaximize: () => ipcRenderer.send("win:toggle-maximize"),
    close: () => ipcRenderer.send("win:close"),
    onMaximized: (cb) => ipcRenderer.on("win:maximized", (_e, v) => cb(!!v)),
  },
});

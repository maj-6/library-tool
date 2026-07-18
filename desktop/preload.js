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
  // Hand a web link to the OS browser. Only http(s) is forwarded; the main
  // process validates the scheme again before shell.openExternal.
  openExternal: (url) => {
    if (typeof url === "string" && /^https?:\/\//i.test(url)) {
      ipcRenderer.send("win:open-external", url);
    }
  },
  // A .lib opened through the OS (double-click / Open With / second launch).
  // The renderer registers its handler then signals ready; the main process
  // queues any path that arrived earlier and delivers it once signalled.
  lib: {
    onOpen: (cb) => ipcRenderer.on("lib:open", (_e, p) => cb(String(p || ""))),
    ready: () => ipcRenderer.send("lib:ready"),
  },
});

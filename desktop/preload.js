// Minimal, context-isolated preload. The UI is the existing web app served by
// the local sidecar, so it needs almost nothing from Electron — we only expose
// a tiny, read-only marker so the page can tell it is running inside the
// desktop shell (e.g. to show a "download databases" affordance).
const { contextBridge, ipcRenderer } = require("electron");

// Electron runs a preload for subframes too. Never expose privileged IPC to an
// iframe, even when a future document accidentally shares the app's origin.
if (process.isMainFrame) contextBridge.exposeInMainWorld("whlDesktop", {
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
  // Request a tightly allowlisted local PDF/image/print window. The main
  // process re-validates both this frame's identity and the exact route.
  openResource: (url) => {
    if (typeof url === "string" && url.length <= 8192) {
      ipcRenderer.send("resource:open", url);
    }
  },
  // Open an independent authenticated workbench. Context is a portable engine
  // address; the main process validates it and owns reuse/window identity.
  workbenches: {
    open: (context, options = {}) => ipcRenderer.invoke("workbench:open", {
      context,
      new_window: options && options.newWindow === true,
    }),
    currentContext: () => ipcRenderer.invoke("workbench:context:get"),
    onContext: (callback) => {
      if (typeof callback !== "function") return () => {};
      const listener = (_event, context) => callback(context);
      ipcRenderer.on("workbench:context", listener);
      return () => ipcRenderer.removeListener("workbench:context", listener);
    },
  },
  // A .lib opened through the OS (double-click / Open With / second launch).
  // The renderer registers its handler then signals ready; the main process
  // queues any path that arrived earlier and delivers it once signalled.
  lib: {
    onOpen: (cb) => ipcRenderer.on("lib:open", (_e, p) => cb(String(p || ""))),
    ready: () => ipcRenderer.send("lib:ready"),
  },
});

// Narrow, context-isolated bridge for the startup splash. The renderer can
// receive display strings and the two read-only packaged asset URLs; it has no
// general Node or filesystem access.
const { contextBridge, ipcRenderer } = require("electron");
const fs = require("fs");
const path = require("path");
const { pathToFileURL } = require("url");

function startupAsset(name, devPath) {
  const packaged = path.join(process.resourcesPath, "startup-assets", name);
  return pathToFileURL(fs.existsSync(packaged) ? packaged : devPath).href;
}

contextBridge.exposeInMainWorld("whlStartup", {
  assets: {
    icon: startupAsset("icon.png", path.join(__dirname, "..", "icon.png")),
    font: startupAsset("roboto-slab-var.woff2", path.join(
      __dirname, "..", "tools", "whl_explorer", "static", "fonts",
      "roboto-slab-var.woff2")),
  },
  onStatus: (cb) => ipcRenderer.on("startup:status", (_event, value) => cb(String(value))),
  onTheme: (cb) => ipcRenderer.on("startup:theme", (_event, value) => cb(String(value))),
});

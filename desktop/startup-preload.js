// Narrow, context-isolated bridge for the startup splash. The renderer can
// receive display strings and the two read-only packaged assets; it has no
// general Node or filesystem access.
const { contextBridge, ipcRenderer } = require("electron");
const fs = require("fs");
const path = require("path");

function startupAsset(name, devPath, mimeType) {
  const packaged = path.join(process.resourcesPath, "startup-assets", name);
  const assetPath = fs.existsSync(packaged) ? packaged : devPath;
  return `data:${mimeType};base64,${fs.readFileSync(assetPath).toString("base64")}`;
}

contextBridge.exposeInMainWorld("whlStartup", {
  assets: {
    icon: startupAsset("icon.png", path.join(__dirname, "..", "icon.png"), "image/png"),
    font: startupAsset("roboto-slab-var.woff2", path.join(
      __dirname, "..", "tools", "whl_explorer", "static", "fonts",
      "roboto-slab-var.woff2"), "font/woff2"),
  },
  onStatus: (cb) => ipcRenderer.on("startup:status", (_event, value) => cb(String(value))),
  onTheme: (cb) => ipcRenderer.on("startup:theme", (_event, value) => cb(String(value))),
});

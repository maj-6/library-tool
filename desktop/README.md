# Library Tool — desktop app

An Electron shell around the existing Flask backend ("the sidecar"). The shell
spawns the backend on a free loopback port with a per-user writable data root
and loads it in a window. Nothing about the web app changes — this is packaging.

```
Electron (main.js)
  └─ spawns sidecar  ──  dev:      python ../tools/whl_explorer/server.py
                          packaged: resources/sidecar/whl-explorer-sidecar.exe
     with  WHL_PORT=<free port>   WHL_DATA_ROOT=%APPDATA%\Library Tool
  └─ BrowserWindow → http://127.0.0.1:<port>/
```

The backend already supports this: `WHL_PORT` picks the port, `WHL_DATA_ROOT`
relocates all writable state, and when frozen (`sys.frozen`) `libcommon` reads
shipped assets from the bundle (`APP_ROOT`) and writes state to the per-user
dir (`DATA_ROOT`). See `../REQUIREMENTS.md`.

## Run in dev

```
cd desktop
npm install
npm start           # spawns the Python source; needs Python on PATH (or set WHL_PYTHON)
```

## Build the Windows installer (NSIS)

Prerequisites on the build machine: Python 3 with the app's requirements
installed, `pip install pyinstaller`, Node 18+. electron-builder downloads the
NSIS tooling itself. A *signed* build additionally needs a code-signing cert.

```
cd desktop
npm install
npm run build:sidecar     # PyInstaller → dist-sidecar/whl-explorer-sidecar/
npm run dist              # electron-builder → release/LibraryTool-Setup-<version>.exe
```

The installer is branded (the app icon + a Roboto Slab sidebar generated from
the repo's own assets — regenerate with the script noted in build/), assisted
(license-free, per-user, choose-your-directory), and creates desktop + start
menu shortcuts. Uninstall keeps the data root: the catalogue outlives the app.

## Releasing an update

The app checks GitHub Releases (`maj-6/library-tool`) once at startup,
downloads in the background, and offers a restart when ready. Publishing a
release is: bump `version` in package.json, build, then

```
gh release create v<version> release/LibraryTool-Setup-<version>.exe release/latest.yml --title v<version>
```

`latest.yml` is what electron-updater reads; without it the check is a no-op.
Offline machines simply skip the check.

### Downloading the databases (offline search)

The installer stays small on purpose: Tesseract, optional API keys, and the
multi-GB Open Library index are handled by the **in-app setup guide** on
first launch (re-openable from Help → Setup guide), which detects Tesseract,
takes the optional Mistral key, and downloads databases with progress from
the URLs in **Settings → Sync**. Cloud accounts need no setup at all — the
app ships knowing its own project URL and public anon key
(`tools/cloud_defaults.py`); only the owner's service key is ever entered by
hand. For unattended installs, pre-place the databases in the data root or
fetch them later from Settings.

### Databases (local vs cloud)

Search resolves **local-first, cloud-fallback**: if the Open Library index has
been downloaded into the data root it is used directly (offline); otherwise the
backend proxies the query to the configured **remote base URL**. Remote URLs
(the cloud API and the per-database download sources) are set in
**Settings → Sync** and can be downloaded/synced for offline use at any time.

### Notes / caveats

- Signing: `npm run dist` produces an **unsigned** installer unless you set
  electron-builder's `CSC_LINK`/`CSC_KEY_PASSWORD`. Unsigned installers trip
  SmartScreen.
- Tesseract: local OCR needs the Tesseract binary. It is not bundled by
  default; install it on the target machine (or add it to the spec's binaries).
- The multi-GB `ol_*.db` indexes are never bundled — they are downloaded on
  demand so the installer stays small.

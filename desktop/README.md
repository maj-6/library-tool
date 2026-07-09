# Catalog Explorer — desktop app

An Electron shell around the existing Flask backend ("the sidecar"). The shell
spawns the backend on a free loopback port with a per-user writable data root
and loads it in a window. Nothing about the web app changes — this is packaging.

```
Electron (main.js)
  └─ spawns sidecar  ──  dev:      python ../tools/whl_explorer/server.py
                          packaged: resources/sidecar/whl-explorer-sidecar.exe
     with  WHL_PORT=<free port>   WHL_DATA_ROOT=%APPDATA%\Catalog Explorer
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

## Build the Windows .msi installer

Prerequisites on the build machine: Python 3 with the app's requirements
installed, `pip install pyinstaller`, Node 18+, and — for the MSI target —
**.NET Framework 3.5** (WiX 3's dependency; electron-builder downloads the WiX
tools themselves). A *signed* build additionally needs a code-signing cert.

```
cd desktop
npm install
npm run build:sidecar     # PyInstaller → dist-sidecar/whl-explorer-sidecar/
npm run dist              # electron-builder → release/CatalogExplorer-<version>.msi
```

`npm run dist` bundles the frozen sidecar as `resources/sidecar/` and produces
an **`.msi`** (assisted UI, per-user by default; silent install with
`msiexec /i CatalogExplorer-<version>.msi /qn`).

### Downloading the databases (offline search)

electron-builder's MSI has no custom installer UI, so the *"download a local
copy of the databases"* option is presented by the **app on first launch**: if
no local Open Library index is found it offers to download one from the URLs in
**Settings → Sync** (multi-GB, so it never belongs inside the installer). For
unattended/enterprise installs, the databases can be pre-placed in the data
root or fetched later from Settings. (An NSIS `.exe` target with an in-installer
checkbox is also possible — ask if you want both.)

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

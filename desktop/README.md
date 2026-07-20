# Library Tool — desktop app

An Electron shell around the existing Flask backend ("the sidecar"). The shell
spawns the backend on a free loopback port with a per-user writable data root
and loads it in a window. Nothing about the web app changes — this is packaging.

```
Electron (main.js)
  └─ spawns sidecar  ──  dev:      python ../tools/whl_explorer/server.py
                          packaged: resources/sidecar/whl-explorer-sidecar.exe
     with  WHL_PORT=<free port>   WHL_DATA_ROOT=%APPDATA%\Library Tool
           WHL_APP_VERSION=<shell version — shown in the web UI>
  └─ BrowserWindow → http://127.0.0.1:<port>/
```

The backend already supports this: `WHL_PORT` picks the port, `WHL_DATA_ROOT`
relocates all writable state, and when frozen (`sys.frozen`) `libcommon` reads
shipped assets from the bundle (`APP_ROOT`) and writes state to the per-user
dir (`DATA_ROOT`). See `../REQUIREMENTS.md`.

The shell itself stays thin: a frameless main window (the web UI's title bar
is the frame, wired over IPC), themed startup/updater splash windows while
the sidecar and update check run, external links routed to the OS browser,
and a single-instance lock in packaged builds — a second launch focuses the
first. The lock is what makes in-place updates safe (NSIS cannot replace a
running exe); dev is exempt, so a dev instance and the installed app run
side by side.

## Run in dev

```
cd desktop
npm install
npm start           # spawns the Python source; needs Python on PATH (or set WHL_PYTHON)
```

## Build the Windows installer (NSIS)

Prerequisites on the build machine: Python 3 with the app's requirements
installed, `pip install pyinstaller`, Node 22.12+. Electron 43's npm package
requires that Node baseline; electron-builder downloads the NSIS tooling
itself. A *signed* build additionally needs a code-signing cert.

The Windows signing options live under `build.win.signtoolOptions`, as required
by electron-builder 26. Keep certificate material in the `CSC_LINK` and
`CSC_KEY_PASSWORD` environment variables; do not put it in package metadata.

```
cd desktop
npm install
npm run build:sidecar     # PyInstaller → dist-sidecar/whl-explorer-sidecar/
npm run dist              # electron-builder → release/LibraryTool-Setup-<version>.exe
```

The installer is branded (the app icon + the Roboto Slab sidebar bitmap,
both checked-in assets in build/ — there is no in-repo generator), assisted
(license-free, per-user, choose-your-directory), and creates desktop + start
menu shortcuts. Uninstall keeps the data root: the catalogue outlives the app.

## Releasing an update

Updates are a startup **gate**, not a background download: a packaged build
checks GitHub Releases (`maj-6/library-tool`) once before launch. If an
update is waiting, a small update splash shows the download, the NSIS
installer runs silently, and the app relaunches on the new version — the
main window never opens on the old one. No update, offline, a slow check, or
any updater error just falls through to a normal launch. **Settings →
Updates** can switch the check off or opt a stable install into prereleases;
an installed alpha/beta/rc always follows its own prerelease line.

Publishing goes through CI (`../.github/workflows/release.yml`; full
mechanics in `../docs/releasing.md`): bump `version` in package.json (and the
Android version when its APK rides along), commit the bumps, then tag
`v<version>` — or `v<version>-alpha.N` for a testing build, which is flagged
prerelease on GitHub so stable installs never auto-update to it — and push
the tag. The workflow builds the sidecar + installer, signs when the signing
secret is set, publishes the GitHub Release (exe + `.exe.blockmap` +
`latest.yml`, plus the APK when it rides along), and registers the row on the
website's Downloads page. The manual fallback is: build, then

```
gh release create v<version> release/LibraryTool-Setup-<version>.exe release/LibraryTool-Setup-<version>.exe.blockmap release/latest.yml --title v<version>
```

`latest.yml` and the `.blockmap` are what electron-updater reads; without
them the check is a no-op.
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

- Signing: CI signs when the `WIN_CSC_LINK_B64`/`WIN_CSC_KEY_PASSWORD`
  secrets are set; a local `npm run dist` is unsigned unless
  `CSC_LINK`/`CSC_KEY_PASSWORD` point at the cert. The current cert chains
  to a self-managed root CA, trusted only on machines that install it, so
  public downloads still trip SmartScreen — see `signing/README.md`.
- Tesseract: local OCR needs the Tesseract binary. It is not bundled by
  default; install it on the target machine (or add it to the spec's binaries).
- The multi-GB `ol_*.db` indexes are never bundled — they are downloaded on
  demand so the installer stays small.

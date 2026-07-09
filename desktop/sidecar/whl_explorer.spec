# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec that freezes the Library Tool Flask backend into a
# self-contained "sidecar" the Electron app spawns.
#
#   build (from desktop/):  npm run build:sidecar
#   output:                 dist-sidecar/whl-explorer-sidecar/whl-explorer-sidecar[.exe]
#
# When frozen, libcommon._app_root() returns sys._MEIPASS, so the shipped
# read-only assets below must land at the bundle root (ch_library.xlsx, the
# reference CSVs) or output/ (ch_library.json). Writable state (client_state,
# entry folders, the downloaded ol_*.db) goes to WHL_DATA_ROOT, which the
# Electron shell points at the per-user app-data dir — nothing writable is
# bundled. The multi-GB Open Library indexes are downloaded on demand, never
# shipped.
import os
from PyInstaller.utils.hooks import collect_submodules

# SPECPATH is the directory containing this spec (desktop/sidecar); the repo
# root is two levels up.
SPEC_DIR = os.path.abspath(SPECPATH)
REPO = os.path.abspath(os.path.join(SPEC_DIR, "..", ".."))
TOOLS = os.path.join(REPO, "tools")
APP = os.path.join(TOOLS, "whl_explorer")

# (source, dest-in-bundle). Flask's templates/static are pointed here by a
# frozen-aware app init in server.py; the rest mirror the APP_ROOT layout.
_candidates = [
    (os.path.join(APP, "templates"), "templates"),
    (os.path.join(APP, "static"), "static"),
    (os.path.join(REPO, "ch_library.xlsx"), "."),
    (os.path.join(REPO, "output", "ch_library.json"), "output"),
    (os.path.join(REPO, "copyright_renewals.csv"), "."),
    (os.path.join(REPO, "whl_catalog.csv"), "."),
]
datas = [(s, d) for (s, d) in _candidates if os.path.exists(s)]

hiddenimports = (
    collect_submodules("fitz")            # PyMuPDF
    + collect_submodules("pypdf")
    + ["flask", "jinja2", "openpyxl", "openpyxl.cell._writer", "sqlite3"]
)

a = Analysis(
    [os.path.join(APP, "server.py")],
    pathex=[TOOLS, APP],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "numpy.tests"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="whl-explorer-sidecar",
    console=True,      # keep a console so stdout/stderr reach the Electron logs
    debug=False, strip=False, upx=False,
)
coll = COLLECT(
    exe, a.binaries, a.datas,
    strip=False, upx=False,
    name="whl-explorer-sidecar",
)

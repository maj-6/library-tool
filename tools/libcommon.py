"""Shared helpers for the world-herb-library tools.

Repo paths, random id generation, and small JSON helpers used by the
explorer, the Open Library index builders, and the catalog checks.
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path

# Repo layout: this file lives at <root>/tools/libcommon.py
ROOT = Path(__file__).resolve().parent.parent

# --- app vs data roots -----------------------------------------------------
# Two roots, so the app can be packaged/relocated without pinning user data
# to the (possibly read-only) install location:
#   APP_ROOT  — read-only assets shipped WITH the app (the source
#               spreadsheet + the reference CSVs + the generated catalogue
#               JSON). When frozen this is the bundle dir (sys._MEIPASS).
#   DATA_ROOT — writable per-user state (the JSON document store, entry
#               folders, IA downloads + caches, and the downloaded search
#               indexes). When frozen this is a per-user app-data dir.
# In a normal dev checkout both resolve to the repo root, so the on-disk
# layout is exactly as before. WHL_DATA_ROOT overrides the data root
# explicitly (used by tests and by the packaged launcher).


def _app_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    return ROOT


def _data_root() -> Path:
    override = os.environ.get("WHL_DATA_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    if getattr(sys, "frozen", False):
        if sys.platform == "win32":
            base = Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
        elif sys.platform == "darwin":
            base = Path.home() / "Library" / "Application Support"
        else:
            base = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share"))
        return base / "whl-explorer"
    return ROOT


APP_ROOT = _app_root()
DATA_ROOT = _data_root()

# writable per-user state
OUTPUT_DIR = DATA_ROOT / "output"
MANUAL_ENTRIES_PATH = OUTPUT_DIR / "manual_entries.json"
# UI/session state lifted out of browser localStorage so it is
# port-independent and syncable (checked books, settings, attention marks).
CLIENT_STATE_PATH = OUTPUT_DIR / "client_state.json"

# read-only shipped assets
XLSX_PATH = APP_ROOT / "ch_library.xlsx"
CH_LIBRARY_JSON_PATH = APP_ROOT / "output" / "ch_library.json"

# Internet Archive PDF downloads + their cataloging metadata (writable).
IA_DOWNLOADS_DIR = DATA_ROOT / "downloads" / "ia"
IA_CATALOG_PATH = IA_DOWNLOADS_DIR / "catalog.json"

# Ordered fields for manually added books. local_pdf holds a locally
# attached scan (a verified source for books marked SCAN).
MANUAL_ENTRY_FIELDS = [
    "title",
    "subtitle",
    "author",
    "publisher",
    "city",
    "year",
    "edition",
    "volume",
    "language",
    "pages",
    "condition",
    "price",
    "illustrations",
    "categories",
    "notes",
    "local_pdf",
    "attention",
]


# --- ids -------------------------------------------------------------------

def gen_id(existing: set[str] | None = None) -> str:
    """Return a short random hex id not present in existing."""
    existing = existing or set()
    while True:
        candidate = uuid.uuid4().hex[:12]
        if candidate not in existing:
            existing.add(candidate)
            return candidate


# --- json ------------------------------------------------------------------

def load_json(path: Path, default):
    if Path(path).exists():
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    return default


def save_json(path: Path, data) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)

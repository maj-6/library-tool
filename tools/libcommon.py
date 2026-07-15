"""Shared helpers for the world-herb-library tools.

Repo paths, random id generation, and small JSON helpers used by the
explorer, the Open Library index builders, and the catalog checks.
"""
from __future__ import annotations

import json
import re
import os
import sys
import time
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

# Search databases (the Open Library indexes, the copyright-renewal CSV) are
# large and are often copied by hand from a flash drive, so they live somewhere
# easy to reach: a ~/.library-tool folder in the home directory. They resolve
# MOST-ACCESSIBLE-FIRST — that drop-in folder, then the app's data root, then the
# copy shipped with the app — so a local file is always used with no URL and no
# download. DB_DIR is where a download or a hand-drop is expected.
DB_DIR = Path.home() / ".library-tool"


def find_db(basename: str, data_rel: str = "") -> Path:
    """First existing copy of a database file across the drop-in folder, the data
    root (its root and, if given, data_rel like 'output/x.db'), and the app
    bundle. When none exists, returns the drop-in target DB_DIR/basename — where a
    download or a hand-drop should go."""
    candidates = [DB_DIR / basename]
    if data_rel:
        candidates.append(DATA_ROOT / data_rel)
    candidates += [DATA_ROOT / basename, APP_ROOT / basename]
    for p in candidates:
        if p.exists():
            return p
    return DB_DIR / basename

# read-only shipped assets
XLSX_PATH = APP_ROOT / "ch_library.xlsx"
CH_LIBRARY_JSON_PATH = APP_ROOT / "output" / "ch_library.json"

# Release notes, authored once in website/changelog.md and shared by the website
# and the desktop app (Help > View changelog). A frozen build bundles the file
# to the app root (see desktop/sidecar/whl_explorer.spec); a dev checkout reads
# it straight from website/.
CHANGELOG_PATH = (
    APP_ROOT / "changelog.md" if getattr(sys, "frozen", False)
    else ROOT / "website" / "changelog.md"
)

# Internet Archive PDF downloads + their cataloging metadata (writable).
IA_DOWNLOADS_DIR = DATA_ROOT / "downloads" / "ia"
IA_CATALOG_PATH = IA_DOWNLOADS_DIR / "catalog.json"

# The category taxonomy: a tree of {name, parent} nodes keyed by id. Replaces
# the deprecated comma-separated `categories` text fields; records point at
# nodes via `category_ids` lists. Synced across machines like the builds
# (tools/store_sync.py), published as resolved name paths (docs/
# library-analyze-design.md).
CATEGORIES_PATH = OUTPUT_DIR / "categories.json"

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
    "group_id",
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

def slugify(title: str, year=None, taken: set | None = None) -> str:
    """A stable, readable url key: "flora-rustica-1792".

    Deduplicated against `taken` when given, so two editions of the same book in
    the same year get -2, -3 rather than colliding on a unique index.
    """
    base = re.sub(r"[^a-z0-9]+", "-", f"{title} {year or ''}".lower()).strip("-")[:60]
    base = base or "volume"
    if taken is None:
        return base
    slug, n = base, 2
    while slug in taken:
        slug, n = f"{base}-{n}", n + 1
    taken.add(slug)
    return slug


def gen_id(existing: set[str] | None = None) -> str:
    """Return a short random hex id not present in existing."""
    existing = existing or set()
    while True:
        candidate = uuid.uuid4().hex[:12]
        if candidate not in existing:
            existing.add(candidate)
            return candidate


# --- category taxonomy -------------------------------------------------------
# Shared by the server (assignment, publishing) and tools/cloud_setup.py
# (seed/fixture), which resolve ids to paths without a running server.

def load_taxonomy() -> dict:
    """The taxonomy document: {"version": 1, "nodes": {id: {name, parent,
    created_at, updated_at}}}. Absent file = empty tree."""
    doc = load_json(CATEGORIES_PATH, None)
    if not isinstance(doc, dict) or not isinstance(doc.get("nodes"), dict):
        return {"version": 1, "nodes": {}}
    return doc


def category_path(nodes: dict, node_id: str) -> list[str]:
    """Root→leaf name path for one node. A dangling or cyclic parent chain
    yields what could be resolved rather than raising: assignments must not
    break because a node was deleted on another machine mid-sync."""
    path, seen = [], set()
    cur = node_id
    while cur and cur in nodes and cur not in seen:
        seen.add(cur)
        path.append(str(nodes[cur].get("name") or "").strip() or "?")
        cur = str(nodes[cur].get("parent") or "")
    return list(reversed(path))


def category_paths(nodes: dict, ids) -> list[list[str]]:
    """Resolved, de-duplicated, sorted paths for a record's category_ids."""
    out, seen = [], set()
    for cid in ids or []:
        p = category_path(nodes, str(cid))
        if not p:
            continue
        key = "\x1f".join(p)
        if key not in seen:
            seen.add(key)
            out.append(p)
    return sorted(out)


def categories_text(paths: list[list[str]]) -> str:
    """The flat rendering of paths — " › " within a path, ", " between —
    which is what the volumes.categories text column (and its fts index)
    carries after the overhaul."""
    return ", ".join(" › ".join(p) for p in paths)


# --- json ------------------------------------------------------------------

def load_json(path: Path, default):
    if Path(path).exists():
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    return default


def save_json(path: Path, data) -> None:
    """Atomic write (tmp + replace): a concurrent reader sees the old or the
    new file, never a torn half-write. Background threads (cloud sync, OCR)
    read these files while request handlers rewrite them.

    Windows can refuse the replace while another handle has the target open
    (sharing violation); those read windows are milliseconds, so retry briefly
    and fall back to an in-place write rather than dropping the data."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
    for attempt in range(5):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.05 * (attempt + 1))
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass

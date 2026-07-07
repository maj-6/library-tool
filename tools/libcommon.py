"""Shared helpers for the world-herb-library tools.

Repo paths, random id generation, and small JSON helpers used by the
explorer, the Open Library index builders, and the catalog checks.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path

# Repo layout: this file lives at <root>/tools/libcommon.py
ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = ROOT / "output"
XLSX_PATH = ROOT / "ch_library.xlsx"

CH_LIBRARY_JSON_PATH = OUTPUT_DIR / "ch_library.json"
MANUAL_ENTRIES_PATH = OUTPUT_DIR / "manual_entries.json"

# Internet Archive PDF downloads + their cataloging metadata.
IA_DOWNLOADS_DIR = ROOT / "downloads" / "ia"
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

"""Shared helpers for the world-herb-library tools.

Covers repo paths, random id generation, EXIF capture-time reading,
transcript parsing (time markers + Book/End book regions), best-effort
metadata extraction, and small JSON helpers.
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timedelta
from pathlib import Path

# Repo layout: this file lives at <root>/tools/libcommon.py
ROOT = Path(__file__).resolve().parent.parent
TRANSCRIPT_DIR = ROOT / "transcript"
PHOTO_DIR = ROOT / "photo"
BOOKS_DIR = ROOT / "books"
OUTPUT_DIR = ROOT / "output"
XLSX_PATH = ROOT / "ch_library.xlsx"

BOOKS_INDEX_PATH = OUTPUT_DIR / "books_index.json"
BOOKS_METADATA_PATH = OUTPUT_DIR / "books_metadata.json"
LIBRARY_DB_PATH = OUTPUT_DIR / "library_db.json"
CH_LIBRARY_JSON_PATH = OUTPUT_DIR / "ch_library.json"
MANUAL_ENTRIES_PATH = OUTPUT_DIR / "manual_entries.json"

# Internet Archive PDF downloads + their cataloging metadata.
IA_DOWNLOADS_DIR = ROOT / "downloads" / "ia"
IA_CATALOG_PATH = IA_DOWNLOADS_DIR / "catalog.json"

# Ordered metadata fields for list 2 / the review form.
METADATA_FIELDS = [
    "title",
    "subtitle",
    "author",
    "publisher",
    "published_date",
    "language",
    "edition",
    "page_count",
    "notes",
]

# Ordered fields for manually added books (no transcript/photos behind them).
MANUAL_ENTRY_FIELDS = [
    "title",
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


# --- exif ------------------------------------------------------------------

_EXIF_DATETIME_ORIGINAL = 36867
_EXIF_DATETIME = 306


def read_exif_datetime(path: Path) -> datetime | None:
    """Return the photo capture time from EXIF, or None."""
    from PIL import Image, ExifTags  # imported lazily so non-photo tools run without Pillow

    try:
        with Image.open(path) as img:
            exif = img.getexif()
            if not exif:
                return None
            ifd = exif.get_ifd(ExifTags.IFD.Exif)
            raw = ifd.get(_EXIF_DATETIME_ORIGINAL) or exif.get(_EXIF_DATETIME)
    except Exception:
        return None
    if not raw:
        return None
    raw = str(raw).strip()
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def load_photo_index() -> list[tuple[Path, datetime]]:
    """Return [(path, capture_time)] for all photos with EXIF time, sorted."""
    photos = []
    if not PHOTO_DIR.exists():
        return photos
    for p in sorted(PHOTO_DIR.iterdir()):
        if not p.is_file():
            continue
        dt = read_exif_datetime(p)
        if dt is not None:
            photos.append((p, dt))
    photos.sort(key=lambda t: t[1])
    return photos


# --- transcript filename ---------------------------------------------------

_FILENAME_TS_RE = re.compile(r"(\d{8})_(\d{6})")


def parse_recording_start(filename: str) -> datetime | None:
    """Extract the absolute recording start from a URecorder filename."""
    m = _FILENAME_TS_RE.search(filename)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
    except ValueError:
        return None


# --- transcript body parsing ----------------------------------------------

_MARKER_RE = re.compile(r"^\((\d+):(\d{2})\s*-\s*(\d+):(\d{2})\)$")


def _offset_to_seconds(minutes: int, seconds: int) -> int:
    return minutes * 60 + seconds


def seconds_to_offset(total: int) -> str:
    m, s = divmod(int(total), 60)
    return f"{m}:{s:02d}"


def parse_segments(text: str) -> list[dict]:
    """Split transcript text into timed segments.

    Each segment: {start, end (seconds), text}. Text is the joined non-marker
    lines that follow a marker up to the next marker.
    """
    segments: list[dict] = []
    current = None
    buffer: list[str] = []

    def flush():
        if current is not None:
            segments.append(
                {
                    "start": current[0],
                    "end": current[1],
                    "text": " ".join(b.strip() for b in buffer if b.strip()).strip(),
                }
            )

    for line in text.splitlines():
        stripped = line.strip()
        m = _MARKER_RE.match(stripped)
        if m:
            flush()
            current = (
                _offset_to_seconds(int(m.group(1)), int(m.group(2))),
                _offset_to_seconds(int(m.group(3)), int(m.group(4))),
            )
            buffer = []
        else:
            buffer.append(line)
    flush()
    return segments


# Tokens that begin / end a book region.
_END_BOOK_RE = re.compile(r"end\s+book", re.IGNORECASE)
_START_BOOK_RE = re.compile(r"\bbook\b", re.IGNORECASE)
# Keyword labels stripped when testing whether a region has real content.
_KEYWORDS_RE = re.compile(
    r"\b(end\s+book|book|end\s+note|note)\b", re.IGNORECASE
)


def _classify(segment_text: str) -> tuple[bool, bool]:
    """Return (has_start, has_end) for a segment.

    has_end is true if an 'end book' token is present. has_start is true if a
    'book' token remains after removing 'end book' occurrences.
    """
    has_end = bool(_END_BOOK_RE.search(segment_text))
    without_end = _END_BOOK_RE.sub(" ", segment_text)
    has_start = bool(_START_BOOK_RE.search(without_end))
    return has_start, has_end


def _has_content(text: str) -> bool:
    """True if text has alphanumerics once book/note keywords are removed."""
    cleaned = _KEYWORDS_RE.sub(" ", text)
    return bool(re.search(r"[A-Za-z0-9]", cleaned))


def find_book_regions(segments: list[dict]) -> list[dict]:
    """Group segments into book regions using a tolerant state machine.

    A region opens on a 'book' token and closes on 'end book'. A missing
    'end book' is closed by the next 'book' or end of input. Regions whose
    text carries no real content (e.g. repeated 'Book. End book.' fumbles)
    are dropped.
    """
    regions: list[dict] = []
    open_region: dict | None = None

    def close(region, end_sec):
        region["end"] = end_sec
        region["text"] = " ".join(
            s["text"] for s in region["segments"] if s["text"]
        ).strip()
        if _has_content(region["text"]):
            regions.append(region)

    def new_region(seg) -> dict:
        return {"start": seg["start"], "end": seg["end"], "segments": [seg]}

    for seg in segments:
        has_start, has_end = _classify(seg["text"])
        if open_region is None:
            if has_start:
                open_region = new_region(seg)
                if has_end:
                    close(open_region, seg["end"])
                    open_region = None
        else:
            if has_start and not has_end:
                # A new book began without an explicit end; close the prior one.
                close(open_region, seg["start"])
                open_region = new_region(seg)
            else:
                open_region["segments"].append(seg)
                if has_end:
                    close(open_region, seg["end"])
                    open_region = None
    if open_region is not None:
        close(open_region, open_region["segments"][-1]["end"])
    return regions


def region_lines(region: dict) -> str:
    """Render a region's segments as readable '(m:ss - m:ss) text' lines."""
    out = []
    for s in region["segments"]:
        marker = f"({seconds_to_offset(s['start'])} - {seconds_to_offset(s['end'])})"
        out.append(f"{marker} {s['text']}".rstrip())
    return "\n".join(out)


# --- best-effort metadata extraction ---------------------------------------

_FIELD_LABELS = re.compile(
    r"\b(author|by|published|publisher|page\s+count|paid|price|note|"
    r"alternate\s+title|alternative\s+title|volume|edition)\b",
    re.IGNORECASE,
)
_YEAR_RE = re.compile(r"\b(1[5-9]\d{2}|20\d{2})\b")
_PAGE_RE = re.compile(
    r"page\s+count,?\s*([0-9][0-9, ]*(?:\s*plus\s*\d+)?)", re.IGNORECASE
)
_EDITION_RE = re.compile(r"\b(\d+\s*(?:st|nd|rd|th))\s+edition\b", re.IGNORECASE)
_AUTHOR_RE = re.compile(
    r"(?:author,?\s*|by\s+)([A-Z][A-Za-z.\-]*(?:\s+[A-Z][A-Za-z.\-]*){0,3})"
)


def _clean_title(text: str) -> str:
    """Take the title as the text after the opening 'Book.' up to a label."""
    t = re.sub(r"^\s*(the\s+)?book[.,]?\s*", "", text, flags=re.IGNORECASE)
    label = _FIELD_LABELS.search(t)
    if label:
        t = t[: label.start()]
    # Stop at the first sentence break.
    t = re.split(r"[.\n]", t, maxsplit=1)[0]
    return t.strip(" ,.-")


def extract_metadata(region_text: str) -> dict:
    """Heuristic pre-fill of list 2 fields from a region's text."""
    meta = {f: "" for f in METADATA_FIELDS}
    text = region_text.strip()
    if not text:
        return meta

    meta["title"] = _clean_title(text)

    m = _AUTHOR_RE.search(text)
    if m:
        meta["author"] = m.group(1).strip(" ,.")

    m = _PAGE_RE.search(text)
    if m:
        meta["page_count"] = re.sub(r"\s+", " ", m.group(1)).strip(" ,").replace(
            "plus", "+"
        ).replace(" + ", "+")

    m = _EDITION_RE.search(text)
    if m:
        meta["edition"] = m.group(1).replace(" ", "")

    # published_date: first plausible year.
    m = _YEAR_RE.search(text)
    if m:
        meta["published_date"] = m.group(1)

    # publisher: text after 'Published,' minus a leading/trailing year.
    pm = re.search(r"publish(?:ed|er),?\s*([^.\n]+)", text, re.IGNORECASE)
    if pm:
        pub = pm.group(1).strip()
        pub = _YEAR_RE.sub("", pub).strip(" ,.")
        if pub and not pub.isdigit():
            meta["publisher"] = pub

    # notes: collect prices, explicit notes, and alternate titles.
    notes: list[str] = []
    for nm in re.finditer(
        r"(?:paid|price),?\s*([^.\n]+)", text, re.IGNORECASE
    ):
        notes.append("Paid " + nm.group(1).strip(" ,."))
    for nm in re.finditer(
        r"note[,.]?\s*(.+?)\s*end\s+note", text, re.IGNORECASE
    ):
        notes.append("Note: " + nm.group(1).strip(" ,."))
    for nm in re.finditer(
        r"alternat(?:e|ive)\s+title,?\s*([^.\n]+)", text, re.IGNORECASE
    ):
        notes.append("Alternate title: " + nm.group(1).strip(" ,."))
    meta["notes"] = "; ".join(dict.fromkeys(notes))
    return meta


# --- datetime helpers ------------------------------------------------------

def add_offset(start: datetime, seconds: int) -> datetime:
    return start + timedelta(seconds=seconds)

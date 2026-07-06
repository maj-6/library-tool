"""Build per-book folders and the two JSON lists from transcripts + photos.

For each dictated book region it creates books/<id>/ containing the region
transcript and the photos whose EXIF capture time falls inside the region's
absolute time window, copied in capture order as 1.jpg, 2.jpg, ...

Outputs:
  output/books_index.json    (list 1: folder index)
  output/books_metadata.json (list 2: per-book metadata)

Run with python3. Existing output requires --force to overwrite.
"""
from __future__ import annotations

import argparse
import shutil
from pathlib import Path

import libcommon as lib


def pick_transcripts() -> list[Path]:
    """Return one transcript per distinct recording start (drops duplicates)."""
    files = [p for p in sorted(lib.TRANSCRIPT_DIR.glob("*.txt")) if p.is_file()]
    chosen: dict = {}
    for path in files:
        start = lib.parse_recording_start(path.name)
        # Key on the parsed start; fall back to the name when unparseable.
        key = start.isoformat() if start else path.name
        existing = chosen.get(key)
        if existing is None:
            chosen[key] = path
            continue
        # Prefer the name without a " (n)" duplicate suffix.
        if "(" in existing.stem and "(" not in path.stem:
            chosen[key] = path
    return sorted(chosen.values(), key=lambda p: p.name)


def collect_regions(transcripts: list[Path]) -> list[dict]:
    """Parse every transcript into regions tagged with absolute time windows."""
    regions: list[dict] = []
    for path in transcripts:
        start = lib.parse_recording_start(path.name)
        text = path.read_text(encoding="utf-8", errors="replace")
        segments = lib.parse_segments(text)
        for region in lib.find_book_regions(segments):
            abs_start = lib.add_offset(start, region["start"]) if start else None
            abs_end = lib.add_offset(start, region["end"]) if start else None
            regions.append(
                {
                    "source": path.name,
                    "region": region,
                    "abs_start": abs_start,
                    "abs_end": abs_end,
                }
            )
    # Order by absolute start when known, otherwise by source then offset.
    regions.sort(
        key=lambda r: (
            r["abs_start"] is None,
            r["abs_start"] or r["source"],
            r["region"]["start"],
        )
    )
    return regions


def assign_photos(
    regions: list[dict],
    photos: list[tuple[Path, object]],
    pad: int = 0,
) -> dict[int, list]:
    """Map region index -> photo paths whose capture time is inside its window.

    The window is the book's [start, end] expanded by pad seconds on each side.
    Each photo is assigned to at most one region (the first chronological match).
    """
    assignments: dict[int, list] = {i: [] for i in range(len(regions))}
    used: set[Path] = set()
    for idx, item in enumerate(regions):
        a, b = item["abs_start"], item["abs_end"]
        if a is None or b is None:
            continue
        lo = lib.add_offset(a, -pad)
        hi = lib.add_offset(b, pad)
        for path, dt in photos:
            if path in used:
                continue
            if lo <= dt <= hi:
                assignments[idx].append(path)
                used.add(path)
    return assignments


def write_book(book_id: str, item: dict, photo_paths: list[Path]) -> int:
    """Create the book folder with transcript.txt and sequential jpgs."""
    folder = lib.BOOKS_DIR / book_id
    folder.mkdir(parents=True, exist_ok=True)
    region = item["region"]

    header = [
        f"source: {item['source']}",
        f"offset: {lib.seconds_to_offset(region['start'])} - {lib.seconds_to_offset(region['end'])}",
    ]
    if item["abs_start"] and item["abs_end"]:
        header.append(
            f"absolute: {item['abs_start'].isoformat()} - {item['abs_end'].isoformat()}"
        )
    body = "\n".join(header) + "\n\n" + lib.region_lines(region) + "\n"
    (folder / "transcript.txt").write_text(body, encoding="utf-8")

    for i, src in enumerate(photo_paths, start=1):
        shutil.copy2(src, folder / f"{i}.jpg")
    return len(photo_paths)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build book folders and JSON lists.")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Remove existing books/ and the two JSON lists, then rebuild.",
    )
    parser.add_argument(
        "--pad",
        type=int,
        default=0,
        help=(
            "Seconds of tolerance added to each side of a book's time window "
            "when matching photos. Default 0 (strict containment)."
        ),
    )
    args = parser.parse_args()

    lists_exist = lib.BOOKS_INDEX_PATH.exists() or lib.BOOKS_METADATA_PATH.exists()
    books_exist = lib.BOOKS_DIR.exists() and any(lib.BOOKS_DIR.iterdir())
    if (lists_exist or books_exist) and not args.force:
        parser.error(
            "Existing output found. Re-run with --force to regenerate "
            "(note: this assigns new random book ids)."
        )

    if lib.BOOKS_DIR.exists():
        shutil.rmtree(lib.BOOKS_DIR, ignore_errors=True)
    lib.BOOKS_DIR.mkdir(parents=True, exist_ok=True)

    transcripts = pick_transcripts()
    regions = collect_regions(transcripts)
    photos = lib.load_photo_index()
    assignments = assign_photos(regions, photos, pad=args.pad)

    index: list[dict] = []
    metadata: list[dict] = []
    ids: set[str] = set()
    assigned_total = 0

    for idx, item in enumerate(regions):
        book_id = lib.gen_id(ids)
        photo_paths = assignments[idx]
        count = write_book(book_id, item, photo_paths)
        assigned_total += count

        region = item["region"]
        index.append(
            {
                "id": book_id,
                "folder": f"books/{book_id}",
                "title_page_image": "1",
                "metadata_ref": book_id,
                "source_transcript": item["source"],
                "time_region": {
                    "start": item["abs_start"].isoformat() if item["abs_start"] else "",
                    "end": item["abs_end"].isoformat() if item["abs_end"] else "",
                    "start_offset": lib.seconds_to_offset(region["start"]),
                    "end_offset": lib.seconds_to_offset(region["end"]),
                },
                "image_count": count,
            }
        )
        metadata.append({"id": book_id, **lib.extract_metadata(region["text"])})

    lib.save_json(lib.BOOKS_INDEX_PATH, index)
    lib.save_json(lib.BOOKS_METADATA_PATH, metadata)

    print(f"transcripts used: {len(transcripts)}")
    print(f"pad seconds:      {args.pad}")
    print(f"books created:    {len(regions)}")
    print(f"photos total:     {len(photos)}")
    print(f"photos assigned:  {assigned_total}")
    print(f"photos unassigned:{len(photos) - assigned_total}")
    print(f"index -> {lib.BOOKS_INDEX_PATH}")
    print(f"metadata -> {lib.BOOKS_METADATA_PATH}")


if __name__ == "__main__":
    main()

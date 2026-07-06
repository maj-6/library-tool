"""Convert the legacy ch_library.xlsx catalogue to JSON.

Reads the single worksheet, maps the DBF-style truncated headers to readable
snake_case keys, ISO-formats dates, and writes output/ch_library.json as an
array of row objects.

Run with python3.
"""
from __future__ import annotations

import argparse
import re
from datetime import date, datetime

import openpyxl

import libcommon as lib

# Known truncated headers -> readable keys.
HEADER_MAP = {
    "AUTHORS": "authors",
    "PUBLICATION": "publication",
    "YEAR_OF_PU": "year_of_publication",
    "EDITION": "edition",
    "CONDITION": "condition",
    "PAGE_REFER": "page_reference",
    "CITY_PUBLI": "city_published",
    "PUBLISHER": "publisher",
    "KEY": "key",
    "KEY_2": "key_2",
    "KEY_3": "key_3",
    "ILLUSTRATI": "illustrations",
    "NOTES": "notes",
    "PRICE": "price",
    "DATE": "date",
}


def slugify(header: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(header).strip().lower()).strip("_")


def clean_value(value):
    if value is None:
        return ""
    if isinstance(value, (datetime, date)):
        return value.date().isoformat() if isinstance(value, datetime) else value.isoformat()
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description="Convert ch_library.xlsx to JSON.")
    parser.add_argument("--xlsx", default=str(lib.XLSX_PATH), help="Source .xlsx path.")
    args = parser.parse_args()

    wb = openpyxl.load_workbook(args.xlsx, read_only=True, data_only=True)
    ws = wb.worksheets[0]

    rows = ws.iter_rows(values_only=True)
    try:
        raw_headers = next(rows)
    except StopIteration:
        raw_headers = []
    keys = [HEADER_MAP.get(h, slugify(h)) if h is not None else f"col_{i}"
            for i, h in enumerate(raw_headers)]

    records: list[dict] = []
    for row in rows:
        if row is None or all(v is None for v in row):
            continue
        record = {keys[i]: clean_value(row[i]) for i in range(len(keys))}
        records.append(record)

    lib.save_json(lib.CH_LIBRARY_JSON_PATH, records)
    print(f"sheet: {ws.title}")
    print(f"columns: {keys}")
    print(f"rows converted: {len(records)}")
    print(f"output -> {lib.CH_LIBRARY_JSON_PATH}")


if __name__ == "__main__":
    main()

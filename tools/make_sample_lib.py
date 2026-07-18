"""Regenerate website/fixtures/sample.lib — the downloadable example archive
the API & file format page (website/api.html) links for tool authors and LLMs
to test against.

One page, two regions (body + a marginal gloss), one figure, a per-book
instructions note, and the generated INSTRUCTIONS.md/schema.json members —
small enough to read by eye, complete enough to exercise a reader. Sealed
through libformat.write_lib, so the fixture always matches what the app
itself would accept. Deterministic ids, so re-running only rewrites the file
when the format actually changed.

    python3 tools/make_sample_lib.py
"""
from __future__ import annotations

import base64
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import libformat  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "website" / "fixtures" / "sample.lib"

# a 1x1 opaque PNG — a real, decodable image without binary noise in the repo
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNs"
    "sPlfDwAFCAJS0eXhIgAAAABJRU5ErkJggg==")

INSTRUCTIONS_BOOK = (
    "This is a demonstration archive. Latin plant names stay untranslated; "
    "the marginal gloss is a later hand — keep it attributed and separate "
    "from the body text.")


def build() -> libformat.LibDocument:
    return libformat.LibDocument(
        format=(2, 0),
        book={
            "format_version": libformat.FORMAT_VERSION,
            "source": "primary",
            "created_at": "2026-07-17T00:00:00+00:00",
            "meta": {
                "title": "Specimen Herbal",
                "authors": "A. Botanist",
                "year": "1652",
                "language": "en",
            },
            "figures": {"p1-fig.png": {"page": 1, "x": 0.3, "y": 0.72,
                                       "w": 0.4, "h": 0.18}},
            "stylesheet": {"body": {"family": "EB Garamond", "size_em": 1.0,
                                    "align": "justify"},
                           "marginalia": {"family": "EB Garamond",
                                          "size_em": 0.78, "style": "italic"}},
        },
        pages=[libformat.LibPage(page=1, dims={"w": 1400, "h": 1798, "dpi": 200},
                                 state="verified", items=[
            {"rid": "s4mp1eb0", "role": "body", "order": 0,
             "box": {"x": 0.2, "y": 0.1, "w": 0.6, "h": 0.55},
             "text": "OF Sage. Chap. 1.\nSage is of a hot and drie "
                     "temperature, much vſed of the\nphyſitions of our time.",
             "norm": "Of Sage. Chapter 1.\nSage is of a hot and dry "
                     "temperament, much used by the\nphysicians of our time."},
            {"rid": "s4mp1egl", "role": "marginalia", "order": 1,
             "box": {"x": 0.03, "y": 0.18, "w": 0.13, "h": 0.08},
             "text": "Salvia offi-\ncinalis."},
            {"rid": "s4mp1efg", "role": "figure", "order": 2,
             "box": {"x": 0.3, "y": 0.72, "w": 0.4, "h": 0.18},
             "text": "![p1-fig.png](p1-fig.png)"},
        ])],
        translations={"ja": {"lang": "ja", "pages":
                             {"1": {"_page": "セージについて。第一章。"}}}},
        assets={"p1-fig.png": _PNG},
    )


def main() -> None:
    doc = build()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    libformat.write_lib(doc, OUT, generator="library-tool/sample",
                        book_id="b-5a3b1e00c0ffee00dec0de00feedf00d",
                        instructions_book=INSTRUCTIONS_BOOK)
    issues = libformat.validate(libformat.read_lib(OUT))
    for i in issues:
        print(f"{i.level}: {i.loc}: {i.msg}")
    if any(i.level == "error" for i in issues):
        raise SystemExit("the sample must validate clean")
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main()

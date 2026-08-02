from __future__ import annotations

import hashlib

from build_whl_index import canonical_source_sha256


def test_source_fingerprint_is_independent_of_line_endings() -> None:
    lf = b"Title,Authors\nHerbal,Ada\n"
    crlf = lf.replace(b"\n", b"\r\n")

    expected = hashlib.sha256(lf).hexdigest()
    assert canonical_source_sha256(lf) == expected
    assert canonical_source_sha256(crlf) == expected


def test_source_fingerprint_still_detects_catalogue_changes() -> None:
    original = b"Title,Authors\nHerbal,Ada\n"
    changed = b"Title,Authors\nHerbal,Grace\n"
    bare_cr = b"Title,Authors\nHerbal,Ad\ra\n"

    assert canonical_source_sha256(original) != canonical_source_sha256(changed)
    assert canonical_source_sha256(original) != canonical_source_sha256(bare_cr)

"""Validating adapters for the legacy ``--- page N ---`` text convention."""

from __future__ import annotations

import re
from typing import Any

from ..errors import ValidationError
from .contracts import CanvasText, TextCorpusSnapshot, TextSegment


_VALID_MARKER = re.compile(r"^--- page ([1-9][0-9]*) ---$", re.MULTILINE)
# Deliberately broad: a damaged marker such as ``--- page 2 --`` must not be
# mistaken for ordinary text and silently collapse a multi-page document into
# one canvas.
_MARKER_LIKE = re.compile(r"^---\s*page\b.*$", re.IGNORECASE | re.MULTILINE)


def _line_number(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _invalid_marker_issues(text: str) -> list[dict[str, Any]]:
    issues = []
    for marker in _MARKER_LIKE.finditer(text):
        if _VALID_MARKER.fullmatch(marker.group(0)) is None:
            issues.append(
                {
                    "code": "invalid_page_marker",
                    "line": _line_number(text, marker.start()),
                    "marker": marker.group(0),
                }
            )
    return issues


def parse_legacy_page_text(
    text: str,
    *,
    item_id: str,
    representation_id: str,
    layer_id: str,
    revision: str,
) -> TextCorpusSnapshot:
    """Convert marked text without silently discarding malformed content.

    An unmarked document is a valid one-canvas corpus.  Once marker syntax is
    present, malformed, duplicate, decreasing, or prefixed content is reported
    as structured adapter issues and no partial corpus is returned.
    """

    if not isinstance(text, str):
        raise ValidationError(
            "legacy text must be a string",
            code="invalid_legacy_text",
        )
    marker_like = list(_MARKER_LIKE.finditer(text))
    if not marker_like:
        return TextCorpusSnapshot(
            item_id=item_id,
            representation_id=representation_id,
            layer_id=layer_id,
            revision=revision,
            canvases=(
                CanvasText(
                    canvas_id="page:1",
                    order=0,
                    label="Page 1",
                    segments=(TextSegment("body", text),),
                ),
            ),
            metadata={"adapter": "legacy-page-markers", "marked": False},
        )

    issues = _invalid_marker_issues(text)
    markers = list(_VALID_MARKER.finditer(text))
    if markers and text[: markers[0].start()].strip():
        issues.append(
            {
                "code": "text_before_first_page_marker",
                "line": 1,
            }
        )
    page_numbers = [int(marker.group(1)) for marker in markers]
    seen: set[int] = set()
    previous = 0
    for marker, page in zip(markers, page_numbers):
        if page in seen:
            issues.append(
                {
                    "code": "duplicate_page_marker",
                    "line": _line_number(text, marker.start()),
                    "page": page,
                }
            )
        elif page < previous:
            issues.append(
                {
                    "code": "decreasing_page_marker",
                    "line": _line_number(text, marker.start()),
                    "page": page,
                    "previous_page": previous,
                }
            )
        seen.add(page)
        previous = page
    if issues or len(markers) != len(marker_like):
        raise ValidationError(
            "legacy page markers are malformed",
            code="invalid_legacy_page_markers",
            details={"issues": issues},
        )

    canvases = []
    for order, marker in enumerate(markers):
        start = marker.end()
        if text.startswith("\r\n", start):
            start += 2
        elif text.startswith("\n", start):
            start += 1
        end = markers[order + 1].start() if order + 1 < len(markers) else len(text)
        page = page_numbers[order]
        canvases.append(
            CanvasText(
                canvas_id=f"page:{page}",
                order=order,
                label=f"Page {page}",
                segments=(TextSegment("body", text[start:end]),),
                metadata={"page_number": page},
            )
        )
    return TextCorpusSnapshot(
        item_id=item_id,
        representation_id=representation_id,
        layer_id=layer_id,
        revision=revision,
        canvases=tuple(canvases),
        metadata={"adapter": "legacy-page-markers", "marked": True},
    )


__all__ = ["parse_legacy_page_text"]

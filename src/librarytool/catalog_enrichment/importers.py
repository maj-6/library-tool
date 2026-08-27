"""Read-only importers for the legacy Library Tool catalog sources.

Only the two catalog sources are in scope here: ``manual_entries.json`` and
``ch_library.json``.  Checked/review state is intentionally not imported as a
third catalog.  Every source object is copied into :class:`SourceRecord` as an
immutable snapshot; this module never opens a legacy path for writing.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
import uuid
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from itertools import chain
from pathlib import Path
from typing import Any

IMPORT_PROJECTION_VERSION = 3
_RECORD_NAMESPACE = uuid.UUID("a86614ce-e234-47bf-a973-b48656872de8")


class LegacySourceError(RuntimeError):
    """A legacy source is absent or does not have the expected JSON shape."""


@dataclass(frozen=True, slots=True)
class LegacySourcePaths:
    """Resolved, read-only Library Tool paths."""

    root: Path
    output_dir: Path
    manual_entries: Path
    ch_library: Path
    captures_dir: Path | None


@dataclass(frozen=True)
class SourceRecord:
    """One read-only row projected from a Library Tool source.

    This small value contract lives with the projection so selective review
    imports do not depend on the separate enrichment database implementation.
    """

    namespace: str
    source_id: str
    data: Mapping[str, Any]
    title: str = ""
    subtitle: str = ""
    authors: tuple[str, ...] = ()
    publisher: str = ""
    publication_year: int | None = None
    edition_statement: str = ""
    volume_statement: str = ""
    summary: str = ""
    extent_statement: str = ""
    page_count: int | None = None
    categories: tuple[str, ...] = ()
    identifiers: tuple[Mapping[str, Any], ...] = ()

    @property
    def source_key(self) -> str:
        return f"{self.namespace}:{self.source_id}"

    @property
    def record_id(self) -> str:
        return str(uuid.uuid5(_RECORD_NAMESPACE, self.source_key))


_ISBN_SPAN_RE = re.compile(
    r"(?<![0-9A-Za-z])(?:[0-9Xx](?:[\s.\-\u2010-\u2015]*[0-9Xx]){9,12})(?![0-9A-Za-z])"
)
_ISBN_LABEL_RE = re.compile(r"(?<![A-Za-z])ISBN(?:\s*-?\s*1[03])?\b", re.I)
_SBN_LABEL_RE = re.compile(
    r"(?<![A-Za-z])SBN\s*[:#]?\s*"
    r"([0-9](?:[\s.\-\u2010-\u2015]*[0-9Xx]){8})(?![0-9A-Za-z])",
    re.I,
)
_LCCN_LABEL_RE = re.compile(
    r"(?:\bLCCN\b|(?:Library\s+of\s+Congress|\bLC\b)\s+"
    r"(?:(?:Catalog(?:ue)?(?:ing)?\s+)?(?:Card\s+)?)?(?:Control\s+)?"
    r"(?:Number|No\.?)\b)",
    re.I,
)
_CIP_HEADING_RE = re.compile(
    r"(?:catalog(?:ing)?\s+)?in\s+publication\s+data|cataloging-in-publication",
    re.I,
)
_CIP_MARK_RE = re.compile(r"\bCIP\b", re.I)
_LCCN_HYPHEN_RE = re.compile(
    r"(?<![0-9A-Za-z])([A-Za-z]{0,3}\s*-?\s*(?:\d{4}|\d{2})"
    r"\s*[-:]\s*\d{1,6})(?!\d)"
)
_LCCN_COMPACT_RE = re.compile(r"(?<![0-9A-Za-z])([A-Za-z]{0,3}\d{8,10})(?![0-9A-Za-z])")
_SIMPLE_EXTENT_RE = re.compile(r"^\s*(\d{1,7})\s*(?:p(?:p)?\.?|pages?)?\s*$", re.I)
_YEAR_RE = re.compile(r"(?<!\d)([12]\d{3})(?!\d)")
_SPACE_RE = re.compile(r"\s+")
_ROLE_PARENS_RE = re.compile(r"^[ \t]*(?:\(([^)]{1,80})\)|([^;,/\n]{1,40}))")
_EMPTY_VALUES = {"", "none", "null", "n/a", "na", "not specified", "unknown"}


def _candidate_output_dir(root: Path) -> Path | None:
    root = root.expanduser().resolve()
    direct = root
    nested = root / "output"
    if (direct / "manual_entries.json").is_file() or (
        direct / "ch_library.json"
    ).is_file():
        return direct
    if (nested / "manual_entries.json").is_file() or (
        nested / "ch_library.json"
    ).is_file():
        return nested
    return None


def discover_source_root(repo_root: str | Path | None = None) -> Path:
    """Find the live Library Tool root, falling back to the repository.

    On Windows the live ``%APPDATA%/Library Tool`` copy is preferred because
    it contains the current manual catalog.  A supplied ``repo_root`` is only
    a fallback, making tests and non-Windows use deterministic.
    """

    candidates: list[Path] = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        candidates.append(Path(appdata) / "Library Tool")
    if repo_root is not None:
        candidates.append(Path(repo_root))
    else:
        candidates.extend((Path.cwd(), Path(__file__).resolve().parents[3]))

    seen: set[Path] = set()
    resolved_candidates: list[Path] = []
    for candidate in candidates:
        resolved = candidate.expanduser().resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        resolved_candidates.append(resolved)
    for require_complete in (True, False):
        for resolved in resolved_candidates:
            output_dir = _candidate_output_dir(resolved)
            if output_dir is None:
                continue
            complete = (output_dir / "manual_entries.json").is_file() and (
                output_dir / "ch_library.json"
            ).is_file()
            if complete or not require_complete:
                return resolved
    looked = ", ".join(str(path) for path in seen)
    raise LegacySourceError(
        f"could not find Library Tool catalog sources; checked: {looked}"
    )


def resolve_source_paths(source_root: str | Path | None = None) -> LegacySourcePaths:
    """Resolve catalog and capture paths without creating or changing them."""

    root = (
        discover_source_root()
        if source_root is None
        else Path(source_root).expanduser().resolve()
    )
    output_dir = _candidate_output_dir(root)
    if output_dir is None:
        raise LegacySourceError(
            f"no manual_entries.json or ch_library.json under {root}"
        )

    capture_candidates = (
        root / "captures",
        output_dir.parent / "captures",
        output_dir / "captures",
    )
    captures_dir = next((path for path in capture_candidates if path.is_dir()), None)
    return LegacySourcePaths(
        root=root,
        output_dir=output_dir,
        manual_entries=output_dir / "manual_entries.json",
        ch_library=output_dir / "ch_library.json",
        captures_dir=captures_dir,
    )


def _read_json(path: Path) -> Any:
    if not path.is_file():
        raise LegacySourceError(f"legacy source does not exist: {path}")
    last_error: Exception | None = None
    for _attempt in range(3):
        try:
            before = path.stat()
            payload = path.read_bytes()
            after = path.stat()
            signature_before = (before.st_size, before.st_mtime_ns, before.st_ino)
            signature_after = (after.st_size, after.st_mtime_ns, after.st_ino)
            if signature_before != signature_after or len(payload) != after.st_size:
                continue
            return json.loads(payload.decode("utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            last_error = exc
            try:
                changed = path.stat().st_mtime_ns != before.st_mtime_ns
            except (OSError, UnboundLocalError):
                changed = False
            if changed:
                continue
            break
    if last_error is not None:
        raise LegacySourceError(f"could not read legacy source {path}: {last_error}") from last_error
    raise LegacySourceError(f"legacy source changed repeatedly while being read: {path}")


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return _SPACE_RE.sub(" ", value).strip()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return ""


def _extent_statement(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (int, float)):
        return str(value)
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
    except (TypeError, ValueError):
        return str(value).strip()


def parse_page_count(value: Any) -> int | None:
    """Return a count only for a single, unambiguous extent."""

    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if 1 <= value <= 1_000_000 else None
    if isinstance(value, float):
        return int(value) if value.is_integer() and 1 <= value <= 1_000_000 else None
    if not isinstance(value, str):
        return None
    match = _SIMPLE_EXTENT_RE.fullmatch(value)
    if not match:
        return None
    count = int(match.group(1))
    return count if 1 <= count <= 1_000_000 else None


def _publication_year(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if 1 <= value <= 9999 else None
    text = str(value).strip()
    if text.isdigit():
        year = int(text)
        return year if 1 <= year <= 9999 else None
    match = _YEAR_RE.search(text)
    return int(match.group(1)) if match else None


def _unique(values: Iterable[str]) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = _SPACE_RE.sub(" ", str(value)).strip(" \t\r\n,;")
        folded = cleaned.casefold()
        if cleaned and folded not in seen:
            seen.add(folded)
            result.append(cleaned)
    return tuple(result)


def _split_people(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return _unique(re.split(r"\s*(?:;|\||\r?\n)\s*", value))
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return _unique(_text(item) for item in value)
    return ()


def _split_categories(*values: Any) -> tuple[str, ...]:
    parts: list[str] = []
    for value in values:
        if isinstance(value, str):
            parts.extend(re.split(r"\s*(?:,|;|\||\r?\n)\s*", value))
        elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
            parts.extend(_text(item) for item in value)
    return _unique(parts)


def _flatten_explicit_value(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        stripped = value.strip()
        if stripped[:1] in "[{" and stripped[-1:] in "]}":
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                pass
            else:
                yield from _flatten_explicit_value(parsed)
                return
        yield value
    elif isinstance(value, Mapping):
        for nested in value.values():
            yield from _flatten_explicit_value(nested)
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        for nested in value:
            yield from _flatten_explicit_value(nested)
    elif value is not None:
        yield str(value)


def _isbn_characters(value: Any) -> str:
    return re.sub(r"[^0-9Xx]", "", str(value)).upper()


def is_valid_isbn10(value: object) -> bool:
    """Return whether *value* has a valid ISBN-10 checksum."""

    isbn = _isbn_characters(value)
    if not re.fullmatch(r"[0-9]{9}[0-9X]", isbn):
        return False
    total = sum(
        (10 - index) * (10 if character == "X" else int(character))
        for index, character in enumerate(isbn)
    )
    return total % 11 == 0


def is_valid_isbn13(value: object) -> bool:
    """Return whether *value* has a valid ISBN-13 checksum."""

    isbn = _isbn_characters(value)
    if not re.fullmatch(r"97[89][0-9]{10}", isbn):
        return False
    total = sum(
        int(character) * (1 if index % 2 == 0 else 3)
        for index, character in enumerate(isbn[:12])
    )
    check = (10 - total % 10) % 10
    return check == int(isbn[-1])


_LCCN_NORMALIZE_LABEL_RE = re.compile(
    r"^(?:(?:library\s+of\s+congress\s+)?(?:catalog(?:ue)?\s+card|control|card)?"
    r"\s*(?:number|no\.?)?|lccn)\s*[:#]?\s*",
    re.I,
)


def normalize_lccn(value: object) -> str | None:
    """Normalize an LCCN to the compact form used by ``lccn.loc.gov``."""

    if value is None:
        return None
    text = unicodedata.normalize("NFKC", str(value)).strip().casefold()
    text = _LCCN_NORMALIZE_LABEL_RE.sub("", text)
    text = text.split("/", 1)[0].strip()
    text = re.sub(r"\s+", "", text)
    match = re.fullmatch(r"([a-z]{0,3})(\d{2}|\d{4})-(\d{1,6})", text)
    if match:
        prefix, year, serial = match.groups()
        return f"{prefix}{year}{serial.zfill(6)}"
    compact = text.replace("-", "")
    match = re.fullmatch(r"([a-z]{0,3})(\d{8}|\d{10})", compact)
    if not match:
        return None
    return f"{match.group(1)}{match.group(2)}"


def _canonical_role(value: str) -> str:
    role = _SPACE_RE.sub(" ", value.replace("_", " ")).strip(" .:;,-").casefold()
    aliases = {
        "case": "hardcover",
        "casebound": "hardcover",
        "cloth": "hardcover",
        "clothbound": "hardcover",
        "hardback": "hardcover",
        "hardbound": "hardcover",
        "hb": "hardcover",
        "paper": "paperback",
        "paperbound": "paperback",
        "pbk": "paperback",
        "pkb": "paperback",
        "softbound": "paperback",
        "softcover": "paperback",
        "electronic": "ebook",
        "e-book": "ebook",
        "audio": "audiobook",
    }
    if role in aliases:
        return aliases[role]
    for token, canonical in aliases.items():
        if re.search(rf"\b{re.escape(token)}\b", role):
            return canonical
    return role[:80]


def _role_from_key(key: str) -> str:
    folded = key.casefold().strip("_")
    folded = re.sub(r"(?:^isbn_|_isbn$|^isbn$)", "", folded)
    return _canonical_role(folded) if folded else ""


def _role_after(text: str, end: int) -> str:
    match = _ROLE_PARENS_RE.match(text[end:])
    if not match:
        return ""
    raw_role = match.group(1) or match.group(2) or ""
    role = _canonical_role(raw_role)
    if role in {"hardcover", "paperback", "ebook", "audiobook"}:
        return role
    if re.search(r"\b(?:vol(?:ume)?s?|set)\b", raw_role, re.I):
        return role
    return ""


def _provenance(path: str, role: str = "", qualifier: str = "") -> str:
    annotations = []
    if role:
        annotations.append(f"role={role}")
    if qualifier:
        annotations.append(qualifier)
    return f"{path} [{'; '.join(annotations)}]" if annotations else path


def _identifier(
    scheme: str,
    value: str,
    normalized: str,
    *,
    valid: bool,
    confidence: str,
    provenance: str,
    role: str = "",
) -> dict[str, Any]:
    return {
        "scheme": scheme,
        "value": value.strip(),
        "normalized_value": normalized.upper(),
        "valid": valid,
        "confidence": confidence,
        "provenance": [provenance],
        "roles": [role] if role else [],
    }


def _derive_sbn(raw: str, *, path: str, confidence: str) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for match in _SBN_LABEL_RE.finditer(raw):
        sbn = _isbn_characters(match.group(1))
        if len(sbn) != 9:
            continue
        isbn = f"0{sbn}"
        valid = is_valid_isbn10(isbn)
        found.append(
            _identifier(
                "SBN",
                match.group(1),
                sbn,
                valid=valid,
                confidence=confidence,
                provenance=_provenance(path, qualifier="explicit-sbn-label"),
            )
        )
        found.append(
            _identifier(
                "ISBN-10",
                f"0-{match.group(1)}",
                isbn,
                valid=valid,
                confidence="derived/from-explicit-sbn",
                provenance=_provenance(path, qualifier="derived-from-explicit-sbn"),
            )
        )
    return found


def _isbn_evidence(value: Any, *, path: str, confidence: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    leaf = path.rsplit(".", 1)[-1]
    key_role = _role_from_key(leaf) if "isbn" in leaf.casefold() else ""
    for text in _flatten_explicit_value(value):
        result.extend(_derive_sbn(text, path=path, confidence=confidence))
        candidates = [
            (match.group(0), match.end()) for match in _ISBN_SPAN_RE.finditer(text)
        ]
        if not candidates:
            # A few captured ISBNs use slash separators (for example
            # ``0-941524/27/2``).  Accept that only when the complete field,
            # apart from an optional ISBN label, consists of ISBN characters
            # and separators.  This deliberately excludes imprint suffixes
            # such as ``/R·096``.
            candidate_text = _ISBN_LABEL_RE.sub("", text, count=1).strip(" :#")
            if re.fullmatch(r"[0-9Xx\s.\-/\u2010-\u2015]+", candidate_text):
                compact = _isbn_characters(candidate_text)
                if 6 <= len(compact) <= 17:
                    candidates = [(candidate_text, len(text))]
        for raw, end in candidates:
            normalized = _isbn_characters(raw)
            role = key_role or _role_after(text, end)
            if len(normalized) == 10:
                scheme = "ISBN-10"
                valid = is_valid_isbn10(normalized)
            elif len(normalized) == 13:
                scheme = "ISBN-13"
                valid = is_valid_isbn13(normalized)
            else:
                scheme = "ISBN-CANDIDATE"
                valid = False
            result.append(
                _identifier(
                    scheme,
                    raw,
                    normalized,
                    valid=valid,
                    confidence=confidence,
                    provenance=_provenance(path, role),
                    role=role,
                )
            )
    return result


def _normalize_explicit_lccn(value: str) -> str | None:
    normalized = normalize_lccn(value)
    if normalized:
        return normalized
    text = str(value).strip().casefold().lstrip("#")
    match = re.fullmatch(
        r"([a-z]{0,3})\s*-?\s*(\d{2}|\d{4})\s*[-:\s]\s*(\d{1,6})",
        text,
    )
    if not match:
        return None
    prefix, year, serial = match.groups()
    return f"{prefix}{year}{serial.zfill(6)}"


def _lccn_strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        stripped = value.strip()
        if stripped[:1] in "[{" and stripped[-1:] in "]}":
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                pass
            else:
                if isinstance(parsed, Mapping) and "lccn" in parsed:
                    yield from _flatten_explicit_value(parsed["lccn"])
                return
        yield value
    elif isinstance(value, Mapping):
        if "lccn" in value:
            yield from _flatten_explicit_value(value["lccn"])
    else:
        yield from _flatten_explicit_value(value)


def _lccn_evidence(value: Any, *, path: str, confidence: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for text in _lccn_strings(value):
        candidates = [match.group(1) for match in _LCCN_HYPHEN_RE.finditer(text)]
        candidates.extend(match.group(1) for match in _LCCN_COMPACT_RE.finditer(text))
        if not candidates and text.strip().casefold() not in _EMPTY_VALUES:
            candidates = [text.strip()]
        for raw in candidates:
            normalized = _normalize_explicit_lccn(raw)
            fallback = re.sub(r"[^0-9A-Za-z]", "", raw).upper()
            if not normalized and not fallback:
                continue
            result.append(
                _identifier(
                    "LCCN",
                    raw,
                    normalized or fallback,
                    valid=normalized is not None,
                    confidence=confidence,
                    provenance=path,
                )
            )
    return result


def _merge_identifiers(
    values: Iterable[Mapping[str, Any]],
) -> tuple[Mapping[str, Any], ...]:
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    confidence_rank = {
        "exact/captured": 3,
        "inferred/known-catalog-correction": 3,
        "ocr/labeled": 2,
        "inferred/corroborated-lccn": 1,
        "derived/from-explicit-sbn": 0,
    }
    for value in values:
        key = (str(value["scheme"]).upper(), str(value["normalized_value"]).upper())
        existing = merged.get(key)
        if existing is None:
            merged[key] = {
                **value,
                "provenance": list(value.get("provenance") or ()),
                "roles": list(value.get("roles") or ()),
            }
            continue
        existing["valid"] = bool(existing.get("valid")) or bool(value.get("valid"))
        if confidence_rank.get(str(value.get("confidence")), -1) > confidence_rank.get(
            str(existing.get("confidence")), -1
        ):
            existing["confidence"] = value["confidence"]
            existing["value"] = value["value"]
        existing["provenance"] = list(
            _unique([*existing["provenance"], *(value.get("provenance") or ())])
        )
        existing["roles"] = list(
            _unique([*existing["roles"], *(value.get("roles") or ())])
        )
    return tuple(merged.values())


def _is_lccn_key(key: str) -> bool:
    folded = key.casefold().strip()
    if folded in {"lccn", "lc_catalog_card_no"}:
        return True
    if not folded.startswith("library_of_congress"):
        return False
    if any(term in folded for term in ("classification", "call_number", "subject")):
        return False
    return any(term in folded for term in ("card", "control", "number", "_no", "_data"))


def _embedded_ocr_lines(extra: Mapping[str, Any]) -> list[tuple[str, str]]:
    lines: list[tuple[str, str]] = []
    assets_root = extra.get("_capture_photo_assets")
    if not isinstance(assets_root, Mapping):
        return lines
    assets = assets_root.get("assets")
    if not isinstance(assets, Sequence) or isinstance(assets, (str, bytes, bytearray)):
        return lines
    for asset_index, asset in enumerate(assets):
        if not isinstance(asset, Mapping):
            continue
        geometry = asset.get("geometry")
        if not isinstance(geometry, Sequence) or isinstance(
            geometry, (str, bytes, bytearray)
        ):
            continue
        for geometry_index, item in enumerate(geometry):
            if not isinstance(item, Mapping):
                continue
            regions = item.get("regions")
            if not isinstance(regions, Sequence) or isinstance(
                regions, (str, bytes, bytearray)
            ):
                continue
            for region_index, region in enumerate(regions):
                if not isinstance(region, Mapping) or not isinstance(
                    region.get("text"), str
                ):
                    continue
                path = (
                    "extra._capture_photo_assets.assets"
                    f"[{asset_index}].geometry[{geometry_index}].regions[{region_index}].text"
                )
                for line_index, text in enumerate(region["text"].splitlines()):
                    if text.strip():
                        lines.append((f"{path}:line[{line_index}]", text.strip()))
    return lines


def _capture_ocr_evidence(
    captures_dir: Path | None,
    capture_id: str,
) -> tuple[list[tuple[str, str]], Mapping[str, Any] | None]:
    if captures_dir is None or not capture_id:
        return [], None
    path = captures_dir / capture_id / "ocr.txt"
    if not path.is_file():
        return [], None
    try:
        payload = path.read_bytes()
    except OSError:
        return [], None
    text = payload.decode("utf-8-sig", errors="replace")
    lines = [
        (f"captures/{capture_id}/ocr.txt:line[{index}]", line.strip())
        for index, line in enumerate(text.splitlines())
        if line.strip()
    ]
    snapshot = {
        "path": f"captures/{capture_id}/ocr.txt",
        "sha256": hashlib.sha256(payload).hexdigest(),
        "byte_length": len(payload),
    }
    return lines, snapshot


def _ocr_identifiers(lines: Sequence[tuple[str, str]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for index, (path, line) in enumerate(lines):
        short_window = "\n".join(text for _, text in lines[index : index + 3])
        if _ISBN_LABEL_RE.search(line):
            result.extend(
                _isbn_evidence(short_window, path=path, confidence="ocr/labeled")
            )
        if _SBN_LABEL_RE.search(line):
            result.extend(
                _derive_sbn(short_window, path=path, confidence="ocr/labeled")
            )
        lccn_label = _LCCN_LABEL_RE.search(line)
        if lccn_label:
            # Parse only the value governed by this label.  Including a nearby
            # ISBN line can misread its suffix (for example ``...-15-6``) as a
            # catalog-card number.
            lccn_text = line[lccn_label.end() :].strip(" :#")
            isbn_label = _ISBN_LABEL_RE.search(lccn_text)
            if isbn_label:
                lccn_text = lccn_text[: isbn_label.start()].strip()
            if not lccn_text and index + 1 < len(lines):
                next_line = lines[index + 1][1]
                if not (
                    _ISBN_LABEL_RE.search(next_line)
                    or _SBN_LABEL_RE.search(next_line)
                ):
                    lccn_text = next_line
            if lccn_text:
                result.extend(
                    _lccn_evidence(lccn_text, path=path, confidence="ocr/labeled")
                )
        if _CIP_HEADING_RE.search(line):
            cip_window = lines[index : index + 16]
            combined = "\n".join(text for _, text in cip_window)
            if not _CIP_MARK_RE.search(combined):
                continue
            for candidate_index, (candidate_path, candidate_line) in enumerate(
                cip_window
            ):
                if candidate_index == 0 or _ISBN_LABEL_RE.search(candidate_line):
                    continue
                matches = [
                    match.group(1) for match in _LCCN_HYPHEN_RE.finditer(candidate_line)
                ]
                for raw in matches:
                    result.extend(
                        _lccn_evidence(
                            raw,
                            path=_provenance(candidate_path, qualifier="CIP-context"),
                            confidence="ocr/labeled",
                        )
                    )
    return result


def _manual_identifiers(
    source_id: str,
    record: Mapping[str, Any],
    external_ocr_lines: Sequence[tuple[str, str]] = (),
) -> tuple[Mapping[str, Any], ...]:
    evidence: list[dict[str, Any]] = []
    possible_mislabeled_lccns: list[tuple[str, str]] = []
    extra = record.get("extra")
    extra = extra if isinstance(extra, Mapping) else {}

    # Only direct fields are considered.  bibliographic_identifiers is an
    # online, work-level candidate set and must never become captured evidence.
    for container_name, container in (("", record), ("extra.", extra)):
        for raw_key, value in container.items():
            key = str(raw_key)
            folded = key.casefold()
            if folded == "bibliographic_identifiers":
                continue
            path = f"{container_name}{key}"
            if "isbn" in folded:
                texts = list(_flatten_explicit_value(value))
                if (
                    len(texts) == 1
                    and re.fullmatch(r"\s*\d{2}\s*-\s*\d{4,6}\s*", texts[0])
                    and not _ISBN_LABEL_RE.search(texts[0])
                ):
                    possible_mislabeled_lccns.append((path, texts[0]))
                else:
                    evidence.extend(
                        _isbn_evidence(value, path=path, confidence="exact/captured")
                    )
            elif _is_lccn_key(folded):
                evidence.extend(
                    _lccn_evidence(value, path=path, confidence="exact/captured")
                )

    lines = _embedded_ocr_lines(extra)
    lines.extend(external_ocr_lines)
    ocr_evidence = _ocr_identifiers(lines)
    evidence.extend(ocr_evidence)
    ocr_lccns = {
        str(item["normalized_value"])
        for item in ocr_evidence
        if item["scheme"] == "LCCN" and item["valid"]
    }
    for path, raw in possible_mislabeled_lccns:
        normalized_lccn = _normalize_explicit_lccn(raw)
        known_correction = (
            source_id == "a6ba0d6716eb"
            and raw.strip() == "90-49652"
            and _text(record.get("title")).casefold() == "the mushroom trail guide"
        )
        if normalized_lccn in ocr_lccns or known_correction:
            evidence.extend(
                _lccn_evidence(
                    raw,
                    path=_provenance(
                        path, qualifier="reclassified-from-mislabeled-isbn"
                    ),
                    confidence=(
                        "inferred/known-catalog-correction"
                        if known_correction
                        else "inferred/corroborated-lccn"
                    ),
                )
            )
            continue
        evidence.append(
            _identifier(
                "ISBN-CANDIDATE",
                raw,
                _isbn_characters(raw),
                valid=False,
                confidence="exact/captured",
                provenance=_provenance(path, qualifier="invalid-isbn-length"),
            )
        )
    return _merge_identifiers(evidence)


def _manual_summary(record: Mapping[str, Any], extra: Mapping[str, Any]) -> str:
    for container in (record, extra):
        for key in ("summary", "abstract"):
            value = _text(container.get(key))
            if value:
                return value
    return ""


def iter_manual_records(
    path: str | Path,
    *,
    captures_dir: str | Path | None = None,
) -> Iterator[SourceRecord]:
    """Yield records from the manual-entry mapping in stable key order."""

    source_path = Path(path).expanduser().resolve()
    data = _read_json(source_path)
    if not isinstance(data, Mapping):
        raise LegacySourceError(f"manual source must be a JSON object: {source_path}")
    capture_path = Path(captures_dir).expanduser().resolve() if captures_dir else None
    for raw_source_id, raw_record in data.items():
        if not isinstance(raw_record, Mapping):
            raise LegacySourceError(f"manual entry {raw_source_id!r} is not an object")
        source_id = str(raw_source_id)
        record = dict(raw_record)
        extra = record.get("extra")
        extra = extra if isinstance(extra, Mapping) else {}
        capture_id = _text(record.get("capture_id"))
        external_ocr_lines, external_ocr_snapshot = _capture_ocr_evidence(
            capture_path,
            capture_id,
        )
        source_data = dict(record)
        source_evidence: dict[str, Any] = {
            "import_projection_version": IMPORT_PROJECTION_VERSION,
        }
        if external_ocr_snapshot is not None:
            # External OCR is a source dependency just like an in-record OCR
            # region.  Hashing this small manifest into SourceRecord.data makes
            # corrections/removal visible to the store's re-projection logic
            # without copying the whole OCR transcript into the database.
            source_evidence["capture_ocr"] = external_ocr_snapshot
        source_data["_catalog_enrichment_source_evidence"] = source_evidence
        extent = record.get("pages")
        yield SourceRecord(
            namespace="manual_entries",
            source_id=source_id,
            data=source_data,
            title=_text(record.get("title")),
            subtitle=_text(record.get("subtitle")),
            authors=_split_people(record.get("authors", record.get("author"))),
            publisher=_text(record.get("publisher")),
            publication_year=_publication_year(record.get("year")),
            edition_statement=_text(record.get("edition")),
            volume_statement=_text(record.get("volume")),
            summary=_manual_summary(record, extra),
            extent_statement=_extent_statement(extent),
            page_count=parse_page_count(extent),
            categories=_split_categories(
                record.get("categories"),
                record.get("key"),
                record.get("key_2"),
                record.get("key_3"),
                extra.get("categories"),
                extra.get("key"),
                extra.get("key_2"),
                extra.get("key_3"),
            ),
            identifiers=_manual_identifiers(source_id, record, external_ocr_lines),
        )


def iter_ch_records(path: str | Path) -> Iterator[SourceRecord]:
    """Yield CH master-list rows using their zero-based position as identity."""

    source_path = Path(path).expanduser().resolve()
    data = _read_json(source_path)
    if not isinstance(data, list):
        raise LegacySourceError(f"CH source must be a JSON array: {source_path}")
    for index, raw_record in enumerate(data):
        if not isinstance(raw_record, Mapping):
            raise LegacySourceError(f"CH row {index} is not an object")
        record = dict(raw_record)
        source_data = {
            **record,
            "_catalog_enrichment_source_evidence": {
                "import_projection_version": IMPORT_PROJECTION_VERSION,
            },
        }
        extent = record.get("page_reference")
        title = _text(record.get("publication")).replace("_", " ")
        title = _SPACE_RE.sub(" ", title).strip()
        yield SourceRecord(
            namespace="ch_library",
            source_id=str(index),
            data=source_data,
            title=title,
            authors=_split_people(record.get("authors")),
            publisher=_text(record.get("publisher")),
            publication_year=_publication_year(record.get("year_of_publication")),
            edition_statement=_text(record.get("edition")),
            volume_statement=_text(record.get("volume")),
            summary="",
            extent_statement=_extent_statement(extent),
            page_count=parse_page_count(extent),
            categories=_split_categories(
                record.get("key"), record.get("key_2"), record.get("key_3")
            ),
        )


def iter_library_tool_records(
    source_root: str | Path | None = None,
) -> Iterator[SourceRecord]:
    """Yield the canonical manual and CH catalogs, and no review-state rows."""

    paths = resolve_source_paths(source_root)
    yield from iter_manual_records(
        paths.manual_entries, captures_dir=paths.captures_dir
    )
    yield from iter_ch_records(paths.ch_library)


def import_library_tool(
    store: Any,
    source_root: str | Path | None = None,
) -> dict[str, int]:
    """Import both catalogs into a separate initialized enrichment store."""

    paths = resolve_source_paths(source_root)
    if store.path in {paths.manual_entries, paths.ch_library}:
        raise LegacySourceError(
            "the enrichment database path must be separate from Library Tool sources"
        )
    store.initialize()
    records = chain(
        iter_manual_records(
            paths.manual_entries,
            captures_dir=paths.captures_dir,
        ),
        iter_ch_records(paths.ch_library),
    )
    return store.import_source_records(records)


__all__ = [
    "IMPORT_PROJECTION_VERSION",
    "LegacySourceError",
    "LegacySourcePaths",
    "SourceRecord",
    "discover_source_root",
    "import_library_tool",
    "iter_ch_records",
    "iter_library_tool_records",
    "iter_manual_records",
    "parse_page_count",
    "resolve_source_paths",
]

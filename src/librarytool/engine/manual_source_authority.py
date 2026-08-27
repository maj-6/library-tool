"""Resolve a legacy manual source to its active, canonical holding authority.

The legacy manual row remains the enrichment/source identity after a capture is
promoted.  Corrections, however, writes copy curation to the active build and
uses the capture association (or one unambiguous historical identity) as the
canonical item identity.  This module keeps those three identities distinct
and performs the in-memory, fail-closed claim resolution shared by adapters.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .capture_archives import CaptureArchiveAssociation, capture_book_id
from .errors import EngineError


_BOOK_ID_RE = re.compile(r"^b-[0-9a-f]{32}$", re.ASCII)
_BUILD_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$", re.ASCII)
_PORTABLE_ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,255}$", re.ASCII)
_DEVICE_NAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{index}" for index in range(1, 10)}
    | {f"lpt{index}" for index in range(1, 10)}
)

AssociationReader = Callable[[str], CaptureArchiveAssociation | None]
BuildIdentityReader = Callable[[str], Mapping[str, Any] | None]


class ManualSourceAuthorityError(ValueError):
    """A captured source has ambiguous or invalid active authority."""

    def __init__(self, message: str, *, code: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


@dataclass(frozen=True, slots=True)
class ManualSourceAuthority:
    """The source identity and independently resolved active write target."""

    source_id: str
    source_row: Mapping[str, Any]
    storage_kind: str
    storage_id: str
    storage_row: Mapping[str, Any]
    capture_id: str
    canonical_item_id: str
    association_state: str
    association_book_id: str


def _error(message: str, *, code: str, **details: Any) -> ManualSourceAuthorityError:
    return ManualSourceAuthorityError(message, code=code, **details)


def _build_storage_id(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not _BUILD_ID_RE.fullmatch(value)
        or value.endswith(".")
        or value.split(".", 1)[0].casefold() in _DEVICE_NAMES
    ):
        raise _error(
            "a promoted build has an unsafe storage identity",
            code="invalid_manual_source_authority",
            field="storage_id",
        )
    return value


def _capture_identity(value: Any) -> str:
    if value in (None, ""):
        return ""
    if not isinstance(value, str):
        raise _error(
            "the selected manual source has an invalid capture identity",
            code="invalid_manual_source_authority",
            field="capture_id",
        )
    try:
        capture_book_id(value)
    except (EngineError, TypeError, ValueError) as exc:
        raise _error(
            "the selected manual source has an invalid capture identity",
            code="invalid_manual_source_authority",
            field="capture_id",
        ) from exc
    return value


def _identity_claims(raw: Mapping[str, Any]) -> tuple[tuple[str, Any], ...]:
    claims: list[tuple[str, Any]] = [
        ("capture_book_id", raw.get("capture_book_id")),
        ("book_id", raw.get("book_id")),
        ("lib_book_id", raw.get("lib_book_id")),
    ]
    extra = raw.get("extra")
    if isinstance(extra, Mapping):
        claims.extend(
            (
                ("extra.book_id", extra.get("book_id")),
                ("extra.lib_book_id", extra.get("lib_book_id")),
            )
        )
    return tuple(claims)


def _legacy_resolution_claims(
    raw: Mapping[str, Any],
) -> tuple[tuple[str, Any], ...]:
    return tuple(
        (field, value)
        for field, value in _identity_claims(raw)
        if field != "capture_book_id"
    )


def _valid_book_claim(field: str, value: Any) -> str:
    if not isinstance(value, str) or not _BOOK_ID_RE.fullmatch(value):
        raise _error(
            "a capture has an invalid stable book identity claim",
            code="invalid_manual_source_authority",
            field=field,
        )
    return value


def _historical_build_book_id(
    build_id: str,
    identity_document: Mapping[str, Any] | None,
) -> str:
    persisted = (
        identity_document.get("book_id")
        if isinstance(identity_document, Mapping)
        else None
    )
    if isinstance(persisted, str) and _BOOK_ID_RE.fullmatch(persisted):
        return persisted
    return (
        "b-"
        + uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"https://librarytool.local/items/{build_id}",
        ).hex
    )


def _build_identity_claims(
    build_id: str,
    build: Mapping[str, Any],
    identity_document: Mapping[str, Any] | None,
) -> tuple[tuple[str, str], ...]:
    if identity_document is not None and not isinstance(identity_document, Mapping):
        raise _error(
            "a promoted build identity document is invalid",
            code="invalid_manual_source_authority",
            build_id=build_id,
        )
    linked = build.get("capture_book_id")
    if linked not in (None, ""):
        claims = [
            (
                f"build:{build_id}:capture_book_id",
                _valid_book_claim("capture_book_id", linked),
            )
        ]
        persisted = (
            identity_document.get("book_id")
            if isinstance(identity_document, Mapping)
            else None
        )
        if persisted not in (None, ""):
            claims.append(
                (
                    f"build:{build_id}:lib-id",
                    _valid_book_claim("lib-id.book_id", persisted),
                )
            )
        return tuple(claims)
    return (
        (
            f"build:{build_id}",
            _historical_build_book_id(build_id, identity_document),
        ),
    )


def _standalone_alias(raw: Mapping[str, Any]) -> str:
    candidates: list[str] = []
    containers = [raw]
    extra = raw.get("extra")
    if isinstance(extra, Mapping):
        containers.append(extra)
    for container in containers:
        for field in (
            "canonical_item_id",
            "canonical_book_id",
            "capture_book_id",
            "book_id",
            "lib_book_id",
        ):
            value = container.get(field)
            if value in (None, ""):
                continue
            if not isinstance(value, str) or not _PORTABLE_ALIAS_RE.fullmatch(value):
                raise _error(
                    "a manual source has an invalid canonical alias",
                    code="invalid_manual_source_authority",
                    field=field,
                )
            candidates.append(value)
    distinct = tuple(dict.fromkeys(candidates))
    if len(distinct) > 1:
        raise _error(
            "a manual source has conflicting canonical aliases",
            code="manual_source_identity_conflict",
        )
    return distinct[0] if distinct else ""


def resolve_manual_source_authority(
    manual_entries: Mapping[str, Any],
    builds: Mapping[str, Any],
    source_id: str,
    *,
    association_for: AssociationReader,
    build_identity_for: BuildIdentityReader,
) -> ManualSourceAuthority:
    """Resolve one exact manual source using Corrections-compatible claims.

    Promotion changes only the active storage row. The manual ``source_id``
    remains the import/source-hash identity, while ``canonical_item_id`` comes
    from the capture association or one unambiguous historical lib identity.
    """

    if not isinstance(manual_entries, Mapping) or not isinstance(builds, Mapping):
        raise TypeError("manual_entries and builds must be mappings")
    if not isinstance(source_id, str) or not source_id:
        raise TypeError("source_id must be a non-empty string")
    if not callable(association_for) or not callable(build_identity_for):
        raise TypeError("authority readers must be callable")
    source = manual_entries.get(source_id)
    if not isinstance(source, Mapping):
        raise _error(
            "the selected manual source does not exist",
            code="manual_source_missing",
        )
    capture_id = _capture_identity(source.get("capture_id"))
    if not capture_id:
        return ManualSourceAuthority(
            source_id=source_id,
            source_row=source,
            storage_kind="manual",
            storage_id=source_id,
            storage_row=source,
            capture_id="",
            canonical_item_id=_standalone_alias(source),
            association_state="not_applicable",
            association_book_id="",
        )

    manual_claims = [
        key
        for key, row in manual_entries.items()
        if isinstance(row, Mapping) and row.get("capture_id") == capture_id
    ]
    build_claims = [
        (key, row)
        for key, row in builds.items()
        if isinstance(row, Mapping) and row.get("capture_id") == capture_id
    ]
    if len(manual_claims) != 1 or len(build_claims) > 1:
        raise _error(
            "a capture identity has duplicate active storage claims",
            code="duplicate_manual_source_authority_claim",
            capture_id=capture_id,
            manual_claims=len(manual_claims),
            build_claims=len(build_claims),
        )

    build_id = ""
    build: Mapping[str, Any] | None = None
    discovered: tuple[tuple[str, str], ...] = ()
    if build_claims:
        raw_build_id, build = build_claims[0]
        build_id = _build_storage_id(raw_build_id)
        identity_document = build_identity_for(build_id)
        discovered = _build_identity_claims(build_id, build, identity_document)

    try:
        association = association_for(capture_id)
    except ManualSourceAuthorityError:
        raise
    except Exception as exc:
        raise _error(
            "the capture association could not be inspected",
            code="manual_source_authority_unavailable",
            capture_id=capture_id,
            cause_type=type(exc).__name__,
        ) from exc
    if association is not None:
        if association.capture_id != capture_id:
            raise _error(
                "the capture association belongs to another capture",
                code="manual_source_identity_conflict",
                capture_id=capture_id,
            )
        canonical_id = _valid_book_claim("association.book_id", association.book_id)
        association_state = association.state.value
        association_book_id = canonical_id
    else:
        candidates = list(_legacy_resolution_claims(source)) + list(discovered)
        resolved: dict[str, list[str]] = {}
        for field, raw_value in candidates:
            if raw_value in (None, ""):
                continue
            value = _valid_book_claim(field, raw_value)
            resolved.setdefault(value, []).append(field)
        if len(resolved) > 1:
            raise _error(
                "a capture has conflicting stable book identities",
                code="manual_source_identity_conflict",
                capture_id=capture_id,
            )
        canonical_id = next(iter(resolved), capture_book_id(capture_id))
        association_state = "missing"
        association_book_id = ""

    claimed_rows = [("manual", source_id, source)]
    if build is not None:
        claimed_rows.append(("build", build_id, build))
    for storage_kind, storage_id, row in claimed_rows:
        for field, raw_value in _identity_claims(row):
            if raw_value in (None, ""):
                continue
            if _valid_book_claim(field, raw_value) != canonical_id:
                raise _error(
                    "a captured source carries conflicting book identities",
                    code="manual_source_identity_conflict",
                    capture_id=capture_id,
                    storage_kind=storage_kind,
                    storage_id=storage_id if storage_kind == "build" else "manual",
                    field=field,
                )
    for field, value in discovered:
        if value != canonical_id:
            raise _error(
                "a promoted build carries conflicting historical identities",
                code="manual_source_identity_conflict",
                capture_id=capture_id,
                storage_kind="build",
                storage_id=build_id,
                field=field,
            )

    return ManualSourceAuthority(
        source_id=source_id,
        source_row=source,
        storage_kind="build" if build is not None else "manual",
        storage_id=build_id if build is not None else source_id,
        storage_row=build if build is not None else source,
        capture_id=capture_id,
        canonical_item_id=canonical_id,
        association_state=association_state,
        association_book_id=association_book_id,
    )


__all__ = [
    "ManualSourceAuthority",
    "ManualSourceAuthorityError",
    "resolve_manual_source_authority",
]

"""Manual-entry row codec for framework-neutral item services.

The desktop's ``manual_entries.json`` store predates the engine catalogue.
Rows combine editable bibliographic fields with capture provenance, local
paths, scan-search results, and other server-managed state. This codec keeps
that storage shape out of the engine while providing one deterministic
full-row revision shared by read projections and future command adapters.

Unlike the legacy row's ``updated_at`` field, :meth:`record_revision` covers
every persisted value. Older imports sometimes lack a timestamp and some
historical writers did not advance it, so trusting that field alone would make
conditional edits accept a stale snapshot.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from typing import Any, TypeAlias

from ...engine.item_commands import ItemDraft, ItemRecordSnapshot


ManualRevisionAdvancer: TypeAlias = Callable[[str], str]

_CAPTURE_ID_RE = re.compile(
    r"^[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9_-])?$"
)
_DEVICE_NAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{index}" for index in range(1, 10)}
    | {f"lpt{index}" for index in range(1, 10)}
)
_REVISION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+-]{0,511}$")

_LEGACY_EDITABLE_STRING_FIELDS = (
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
    "attention",
)
_LEGACY_TO_CANONICAL = {
    "author": "authors",
    "city": "publisher_city",
}
_CANONICAL_EDITABLE_STRING_FIELDS = tuple(
    _LEGACY_TO_CANONICAL.get(field, field)
    for field in _LEGACY_EDITABLE_STRING_FIELDS
)
_CANONICAL_TO_LEGACY = {
    canonical: legacy
    for legacy, canonical in _LEGACY_TO_CANONICAL.items()
}
_OPTIONAL_EDITABLE_FIELDS = frozenset({"category_ids", "extra"})
_LOCAL_OR_SERVER_MANAGED_FIELDS = frozenset(
    {
        "capture_id",
        "capture_transport",
        "checks",
        "created_at",
        "edited",
        "id",
        "images",
        "local_pdf",
        "manual_urls",
        "scans",
        "title",
        "updated_at",
        "verify",
    }
)
_ALL_EDITABLE_FIELDS = frozenset(_CANONICAL_EDITABLE_STRING_FIELDS) | (
    _OPTIONAL_EDITABLE_FIELDS
)


def _json_clone(value: Any, *, label: str) -> Any:
    """Detach strict JSON data while rejecting cycles and non-finite numbers."""

    def plain(current: Any, active: set[int]) -> Any:
        if current is None or isinstance(current, (str, bool, int, float)):
            return current
        if isinstance(current, Mapping):
            identity = id(current)
            if identity in active:
                raise ValueError("cyclic mapping")
            active.add(identity)
            try:
                result = {}
                for key, item in current.items():
                    if not isinstance(key, str):
                        raise TypeError("JSON object keys must be strings")
                    result[key] = plain(item, active)
                return result
            finally:
                active.remove(identity)
        if isinstance(current, (list, tuple)):
            identity = id(current)
            if identity in active:
                raise ValueError("cyclic sequence")
            active.add(identity)
            try:
                return [plain(item, active) for item in current]
            finally:
                active.remove(identity)
        raise TypeError(f"{type(current).__name__} is not JSON data")

    try:
        encoded = json.dumps(
            plain(value, set()),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return json.loads(encoded)
    except (RecursionError, TypeError, ValueError, UnicodeError) as exc:
        raise ValueError(f"{label} is not strict JSON data") from exc


class ManualEntryItemCodec:
    """Translate one legacy manual row to and from catalogue item DTOs."""

    editable_fields = _ALL_EDITABLE_FIELDS
    server_managed_fields = _LOCAL_OR_SERVER_MANAGED_FIELDS

    def __init__(self, *, advance_revision: ManualRevisionAdvancer) -> None:
        if not callable(advance_revision):
            raise TypeError("advance_revision must be callable")
        self._advance_revision = advance_revision

    @staticmethod
    def valid_record_revision(value: object) -> bool:
        return isinstance(value, str) and bool(_REVISION_RE.fullmatch(value))

    @classmethod
    def record_revision(
        cls,
        entry_id: str,
        raw: Mapping[str, Any],
    ) -> str:
        """Return a digest bound to the storage key and complete raw row."""

        if not isinstance(entry_id, str) or not entry_id:
            raise ValueError("manual entry id must be a non-empty string")
        if not isinstance(raw, Mapping):
            raise TypeError("a manual entry record must be an object")
        canonical = _json_clone(
            {"entry_id": entry_id, "record": raw},
            label="manual entry record",
        )
        try:
            encoded = json.dumps(
                canonical,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        except UnicodeError as exc:
            raise ValueError(
                "manual entry record contains invalid Unicode"
            ) from exc
        return "mir-" + hashlib.sha256(encoded).hexdigest()

    @classmethod
    def validate_record(
        cls,
        entry_id: str,
        raw: Mapping[str, Any],
    ) -> None:
        """Validate fields consumed by the neutral projection and command seam."""

        cls.record_revision(entry_id, raw)
        embedded_id = raw.get("id")
        if embedded_id is not None and embedded_id != entry_id:
            raise ValueError("the embedded manual entry id conflicts with its key")
        if "title" in raw and not isinstance(raw["title"], str):
            raise TypeError("manual entry title must be a string")
        for field in _LEGACY_EDITABLE_STRING_FIELDS:
            if field in raw and not isinstance(raw[field], str):
                raise TypeError(f"manual entry {field} must be a string")
        for field in ("created_at", "updated_at", "local_pdf"):
            if field in raw and not isinstance(raw[field], str):
                raise TypeError(f"manual entry {field} must be a string")

        capture_id = raw.get("capture_id", "")
        if capture_id not in (None, "") and (
            not isinstance(capture_id, str)
            or not _CAPTURE_ID_RE.fullmatch(capture_id)
            or capture_id.split(".", 1)[0] in _DEVICE_NAMES
        ):
            raise ValueError("manual entry capture_id is invalid")
        if "images" in raw and (
            not isinstance(raw["images"], (list, tuple))
            or any(not isinstance(value, str) for value in raw["images"])
        ):
            raise TypeError("manual entry images must be an array of strings")
        if "category_ids" in raw and (
            not isinstance(raw["category_ids"], (list, tuple))
            or any(not isinstance(value, str) for value in raw["category_ids"])
            or len(raw["category_ids"]) != len(set(raw["category_ids"]))
        ):
            raise TypeError(
                "manual entry category_ids must be unique strings"
            )
        if "extra" in raw and not isinstance(raw["extra"], Mapping):
            raise TypeError("manual entry extra must be an object")

    @classmethod
    def metadata(cls, raw: Mapping[str, Any]) -> dict[str, Any]:
        """Return path-free editable metadata for an item/read projection."""

        result: dict[str, Any] = {
            _LEGACY_TO_CANONICAL.get(field, field): raw[field]
            for field in _LEGACY_EDITABLE_STRING_FIELDS
            if field in raw
        }
        if "category_ids" in raw:
            result["category_ids"] = list(raw["category_ids"])
        if "extra" in raw:
            result["extra"] = _json_clone(
                raw["extra"],
                label="manual entry extra",
            )
        images = raw.get("images")
        if isinstance(images, (list, tuple)):
            result["image_count"] = len(images)
        transport = raw.get("capture_transport")
        if isinstance(transport, str) and transport:
            result["capture_transport"] = transport
        return result

    def decode(
        self,
        entry_id: str,
        raw: Mapping[str, Any],
    ) -> ItemRecordSnapshot:
        """Decode one row without exposing local paths or search results."""

        if not isinstance(raw, Mapping):
            raise TypeError("a manual entry record must be an object")
        self.validate_record(entry_id, raw)
        capture_id = raw.get("capture_id")
        return ItemRecordSnapshot(
            item_id=entry_id,
            revision=self.record_revision(entry_id, raw),
            kind="capture" if capture_id else "book",
            title=raw.get("title", ""),
            metadata={
                key: value
                for key, value in self.metadata(raw).items()
                if key not in {"image_count", "capture_transport"}
            },
            representations=(),
        )

    def encode(
        self,
        entry_id: str,
        draft: ItemDraft,
        previous: Mapping[str, Any] | None,
    ) -> Mapping[str, Any]:
        """Encode bibliographic edits while preserving capture/local state."""

        if not isinstance(draft, ItemDraft):
            raise TypeError("the item draft is invalid")
        if draft.representations:
            raise ValueError("manual entries do not accept representations")
        unknown = sorted(set(draft.metadata) - self.editable_fields)
        if unknown:
            raise ValueError(
                "manual entry metadata contains unsupported fields: "
                + ", ".join(unknown)
            )
        if draft.title != draft.title.strip():
            raise ValueError("manual entry title must not have outer whitespace")
        for field in _CANONICAL_EDITABLE_STRING_FIELDS:
            value = draft.metadata.get(field, "")
            if not isinstance(value, str):
                raise TypeError(f"manual entry {field} must be a string")
            if value != value.strip():
                raise ValueError(
                    f"manual entry {field} must not have outer whitespace"
                )
        category_ids = draft.metadata.get("category_ids")
        if category_ids is not None and (
            not isinstance(category_ids, (list, tuple))
            or any(not isinstance(value, str) for value in category_ids)
            or len(category_ids) != len(set(category_ids))
        ):
            raise TypeError("manual entry category_ids must be unique strings")
        extra = draft.metadata.get("extra")
        if extra is not None and not isinstance(extra, Mapping):
            raise TypeError("manual entry extra must be an object")

        if previous is None:
            if draft.kind != "book":
                raise ValueError(
                    "new manual entries require the book item kind"
                )
            now = self._advance_revision("")
            result: dict[str, Any] = {
                "id": entry_id,
                "title": draft.title,
                "created_at": now,
                "updated_at": now,
                "local_pdf": "",
                "images": [],
            }
        else:
            if not isinstance(previous, Mapping):
                raise TypeError("the previous manual entry must be an object")
            self.validate_record(entry_id, previous)
            expected_kind = "capture" if previous.get("capture_id") else "book"
            if draft.kind != expected_kind:
                raise ValueError("manual entry kind cannot be changed")
            result = _json_clone(previous, label="previous manual entry")
            result["id"] = entry_id
            result["title"] = draft.title
            result["updated_at"] = self._advance_revision(
                str(previous.get("updated_at") or "")
            )

        for field in _CANONICAL_EDITABLE_STRING_FIELDS:
            legacy_field = _CANONICAL_TO_LEGACY.get(field, field)
            if field not in draft.metadata:
                result.pop(legacy_field, None)
            else:
                result[legacy_field] = draft.metadata[field]
        for field in _OPTIONAL_EDITABLE_FIELDS:
            if field not in draft.metadata:
                result.pop(field, None)
            elif field == "category_ids":
                result[field] = list(draft.metadata[field])
            else:
                result[field] = _json_clone(
                    draft.metadata[field],
                    label="manual entry extra",
                )
        self.validate_record(entry_id, result)
        return result


__all__ = [
    "ManualEntryItemCodec",
    "ManualRevisionAdvancer",
]

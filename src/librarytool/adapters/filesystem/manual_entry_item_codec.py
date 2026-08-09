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
_OPTIONAL_EDITABLE_FIELDS = frozenset(
    {"category_ids", "digitization_candidate", "extra"}
)
_WINDOWS_PATH_RE = re.compile(r"(?:^|[\s\"'(])[A-Za-z]:[\\/]")
_ACRONYM_BOUNDARY_RE = re.compile(r"([A-Z]+)([A-Z][a-z])")
_CAMEL_BOUNDARY_RE = re.compile(r"([a-z0-9])([A-Z])")
_KEY_SEPARATOR_RE = re.compile(r"[^A-Za-z0-9]+")
_PRIVATE_EXTRA_LOCATOR_KEYS = frozenset(
    {
        "absolute_path",
        "asset_ref",
        "capture_file",
        "display_ref",
        "file",
        "file_name",
        "filename",
        "filepath",
        "local_path",
        "locator",
        "path",
        "raw_ref",
        "reference",
        "resource_ref",
        "storage_key",
        "storage_locator",
        "storage_path",
        "uri",
        "url",
        "workspace_path",
    }
)
_PRIVATE_EXTRA_LOCATOR_SUFFIXES = frozenset(
    {
        "file",
        "filename",
        "filepath",
        "locator",
        "path",
        "ref",
        "reference",
        "uri",
        "url",
    }
)
# These values describe storage authority or the immutable phone-ingest
# envelope. They are useful to the capture/archive adapters, but are not
# user-authored bibliographic metadata.
_SERVER_OWNED_EXTRA_KEYS = frozenset(
    {
        "_capture_notes",
        "_capture_photo_assets",
        "active_storage_id",
        "book_id",
        "canonical_book_id",
        "canonical_item_id",
        "capture_id",
        "lib_book_id",
        "scan_collection",
        "scan_collection_id",
        "scan_from",
        "storage_id",
    }
)
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
_DROP = object()
_MISSING = object()


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


def _normalized_extra_key(key: str) -> str:
    separated = _ACRONYM_BOUNDARY_RE.sub(r"\1_\2", key)
    separated = _CAMEL_BOUNDARY_RE.sub(r"\1_\2", separated)
    return _KEY_SEPARATOR_RE.sub("_", separated).strip("_").casefold()


def _private_extra_key(key: str) -> bool:
    normalized = _normalized_extra_key(key)
    if key.startswith("_") or normalized in _SERVER_OWNED_EXTRA_KEYS:
        return True
    if normalized in _PRIVATE_EXTRA_LOCATOR_KEYS:
        return True
    return normalized.rsplit("_", 1)[-1] in (
        _PRIVATE_EXTRA_LOCATOR_SUFFIXES
    )


def _looks_like_local_locator(value: str) -> bool:
    stripped = value.strip()
    normalized = stripped.replace("\\", "/")
    lowered = normalized.casefold()
    return bool(
        _WINDOWS_PATH_RE.search(stripped)
        or stripped.startswith(("\\\\", "//", "file:"))
        or "file://" in lowered
        or normalized.startswith("/")
        or normalized.startswith("./")
        or normalized.startswith("../")
        or "/../" in normalized
        or (
            not lowered.startswith(("http://", "https://"))
            and (
                "\\" in stripped
                or (
                    "/" in normalized
                    and (
                        normalized.count("/") > 1
                        or bool(
                            re.search(
                                r"\.[A-Za-z0-9]{1,12}$",
                                normalized,
                            )
                        )
                    )
                )
                or bool(
                    re.fullmatch(
                        (
                            r"[^\s/\\]+\."
                            r"(?:bmp|gif|jpe?g|json|pdf|png|tiff?|txt|webp)"
                        ),
                        stripped,
                        re.IGNORECASE,
                    )
                )
            )
        )
    )


def _public_extra_projection(value: Any) -> Any:
    """Return the user-visible portion of strict capture metadata."""

    if isinstance(value, Mapping):
        projected: dict[str, Any] = {}
        for key, item in value.items():
            if _private_extra_key(key):
                continue
            clean = _public_extra_projection(item)
            if clean is not _DROP:
                projected[key] = clean
        return projected
    if isinstance(value, list):
        projected_items = []
        for item in value:
            clean = _public_extra_projection(item)
            if clean is not _DROP:
                projected_items.append(clean)
        return projected_items
    if isinstance(value, str) and _looks_like_local_locator(value):
        return _DROP
    return value


def _contains_protected_extra(value: Any) -> bool:
    if isinstance(value, Mapping):
        return any(
            _private_extra_key(key) or _contains_protected_extra(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_contains_protected_extra(item) for item in value)
    return isinstance(value, str) and _looks_like_local_locator(value)


def _protected_extra_fragment(value: Any) -> Any:
    """Extract data that must survive removal of the public metadata."""

    if isinstance(value, Mapping):
        protected: dict[str, Any] = {}
        for key, item in value.items():
            if _private_extra_key(key):
                protected[key] = _json_clone(
                    item,
                    label="protected manual entry extra",
                )
                continue
            if _contains_protected_extra(item):
                protected[key] = _protected_extra_fragment(item)
        return protected
    if isinstance(value, list):
        # A list has no durable member key. Keep its complete shape instead of
        # risking that a hidden locator is rebound to a different generated
        # artifact after public members are removed or reordered.
        return _json_clone(value, label="protected manual entry extra")
    if isinstance(value, str) and _looks_like_local_locator(value):
        return value
    return _MISSING


def _merge_protected_extra(previous: Any, edited: Any) -> Any:
    """Merge an editable projection without losing adapter-private values."""

    if not _contains_protected_extra(previous):
        return _json_clone(edited, label="manual entry extra")

    if isinstance(previous, Mapping):
        if not isinstance(edited, Mapping):
            raise ValueError(
                "manual entry extra cannot replace server-managed metadata"
            )
        merged = _json_clone(edited, label="manual entry extra")
        for key, prior in previous.items():
            if _private_extra_key(key):
                if key in edited:
                    raise ValueError(
                        "manual entry extra cannot change "
                        "server-managed metadata"
                    )
                merged[key] = _json_clone(
                    prior,
                    label="protected manual entry extra",
                )
                continue
            if not _contains_protected_extra(prior):
                continue
            if key not in edited:
                merged[key] = _protected_extra_fragment(prior)
                continue
            merged[key] = _merge_protected_extra(prior, edited[key])
        return merged

    if isinstance(previous, list):
        if not isinstance(edited, list):
            raise ValueError(
                "manual entry extra cannot replace server-managed metadata"
            )
        visible = _public_extra_projection(previous)
        if not isinstance(visible, list) or len(edited) != len(visible):
            raise ValueError(
                "manual entry extra cannot resize a list containing "
                "server-managed metadata"
            )
        merged_items = []
        edited_index = 0
        for prior in previous:
            projected = _public_extra_projection(prior)
            if projected is _DROP:
                merged_items.append(
                    _json_clone(
                        prior,
                        label="protected manual entry extra",
                    )
                )
                continue
            candidate = edited[edited_index]
            edited_index += 1
            merged_items.append(
                _merge_protected_extra(prior, candidate)
                if _contains_protected_extra(prior)
                else _json_clone(candidate, label="manual entry extra")
            )
        return merged_items

    raise ValueError(
        "manual entry extra cannot change server-managed metadata"
    )


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

    @staticmethod
    def public_extra(value: Mapping[str, Any]) -> dict[str, Any]:
        """Return a detached, path-free editable ``extra`` projection."""

        if not isinstance(value, Mapping):
            raise TypeError("manual entry extra must be an object")
        detached = _json_clone(value, label="manual entry extra")
        projected = _public_extra_projection(detached)
        assert isinstance(projected, dict)
        return projected

    @staticmethod
    def merge_public_extra(
        previous: Mapping[str, Any],
        edited: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Merge public edits while preserving nested server-owned values."""

        if not isinstance(previous, Mapping) or not isinstance(edited, Mapping):
            raise TypeError("manual entry extra must be an object")
        detached = _json_clone(edited, label="manual entry extra")
        if _public_extra_projection(detached) != detached:
            raise ValueError(
                "manual entry extra contains server-managed metadata "
                "or a private locator"
            )
        merged = _merge_protected_extra(previous, detached)
        assert isinstance(merged, dict)
        return merged

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
        if "digitization_candidate" in raw and not isinstance(
            raw["digitization_candidate"], bool
        ):
            raise TypeError(
                "manual entry digitization_candidate must be a boolean"
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
        if "digitization_candidate" in raw:
            result["digitization_candidate"] = raw[
                "digitization_candidate"
            ]
        if "extra" in raw:
            result["extra"] = cls.public_extra(raw["extra"])
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
        has_digitization_candidate = (
            "digitization_candidate" in draft.metadata
        )
        digitization_candidate = draft.metadata.get(
            "digitization_candidate"
        )
        if has_digitization_candidate and not isinstance(
            digitization_candidate, bool
        ):
            raise TypeError(
                "manual entry digitization_candidate must be a boolean"
            )
        has_extra = "extra" in draft.metadata
        extra = draft.metadata.get("extra")
        detached_extra: Mapping[str, Any] | None = None
        if has_extra and not isinstance(extra, Mapping):
            raise TypeError("manual entry extra must be an object")
        if has_extra:
            detached_extra = _json_clone(
                extra,
                label="manual entry extra",
            )
            if _public_extra_projection(detached_extra) != detached_extra:
                raise ValueError(
                    "manual entry extra contains server-managed metadata "
                    "or a private locator"
                )

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
        if "category_ids" not in draft.metadata:
            result.pop("category_ids", None)
        else:
            result["category_ids"] = list(draft.metadata["category_ids"])
        if "digitization_candidate" not in draft.metadata:
            result.pop("digitization_candidate", None)
        else:
            result["digitization_candidate"] = digitization_candidate

        previous_extra = (
            previous.get("extra")
            if isinstance(previous, Mapping)
            and isinstance(previous.get("extra"), Mapping)
            else {}
        )
        if "extra" not in draft.metadata:
            protected = _protected_extra_fragment(previous_extra)
            if protected is _MISSING or protected == {}:
                result.pop("extra", None)
            else:
                result["extra"] = protected
        else:
            if detached_extra is None:  # pragma: no cover - guarded above
                raise TypeError("manual entry extra must be an object")
            result["extra"] = _merge_protected_extra(
                previous_extra,
                detached_extra,
            )
        self.validate_record(entry_id, result)
        return result


__all__ = [
    "ManualEntryItemCodec",
    "ManualRevisionAdvancer",
]

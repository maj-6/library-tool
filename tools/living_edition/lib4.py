#!/usr/bin/env python3
"""Safe reader, validator, and deterministic sealer for ``.lib4`` packages.

``lib/4`` is the Living Edition profile in Library Tool's ``.lib`` lineage.
It carries immutable, revision-pinned editorial evidence while permitting
large source assets to be embedded, remotely addressed, or both.  The module
uses only the Python standard library; bundled JSON Schemas support generic
clients while these checks enforce cross-record semantics and ZIP safety.
"""

from __future__ import annotations

import copy
import hashlib
import io
import json
import math
import os
import re
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO, Iterable, Mapping
from urllib.parse import urlsplit


FORMAT = "lib/4"
PROFILE = "living-edition"
PROFILE_VERSION = "1.0"
MEDIA_TYPE = "application/vnd.world-herb-library.lib4+zip"
MANIFEST_SCHEMA_ID = "https://worldherblibrary.org/schemas/lib4-manifest-1.0.json"
LAYER_SCHEMA_ID = "https://worldherblibrary.org/schemas/lib4-layer-1.0.json"
CATALOG_SCHEMA_ID = "https://worldherblibrary.org/schemas/lib4-catalog-1.0.json"

SCHEMA_FILES = (
    "lib4-manifest.schema.json",
    "lib4-layer.schema.json",
    "lib4-catalog.schema.json",
    "lib4-generation-receipt.schema.json",
    "lib4-retrieval-record.schema.json",
)

MAX_ARCHIVE_BYTES = 4 * 1024 * 1024 * 1024
MAX_INFLATED_BYTES = 8 * 1024 * 1024 * 1024
MAX_MEMBER_BYTES = 512 * 1024 * 1024
MAX_JSON_BYTES = 64 * 1024 * 1024
MAX_MEMBERS = 50_000
MAX_COMPRESSION_RATIO = 500
MAX_NESTING = 128
MAX_EXT_BYTES = 128 * 1024

PORTABLE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
PORTABLE_REVISION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+-]{0,127}$")
PORTABLE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MEDIA_TYPE_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,126}/"
    r"[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,126}$"
)
LANGUAGE_RE = re.compile(r"^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*$")

CORE_LAYER_KINDS = frozenset({
    "region", "transcription", "translation", "entity", "knowledge",
    "commentary", "notes", "reprocessing", "classification", "retrieval",
})
CORE_RESOURCE_ROLES = frozenset({
    "source", "canvas-image", "layer", "authority-snapshot", "catalog",
    "generation-receipt", "retrieval-records", "embedding-index", "asset",
})
CORE_STRUCTURE_TYPES = frozenset({
    "collection", "work", "volume", "part", "gathering", "folio", "page",
    "issue", "article", "chapter", "section", "entry", "plate", "figure",
    "table", "supplement", "track", "segment",
})
CORE_MATERIAL_TYPES = frozenset({
    "manuscript", "early-print", "modern-book", "journal-article", "periodical",
    "reference-work", "illustrated-work", "map", "ephemera", "audio",
    "video", "born-digital", "digital-document", "mixed-material", "unknown",
})
CORE_SELECTOR_TYPES = frozenset({"box", "polygon", "time-range", "text-quote"})
CORE_RELATION_PREDICATES = frozenset({
    "marginalia-of", "continues-at", "caption-of", "interlinear-gloss-of",
    "translation-of", "transcribed-from", "derived-from", "mentions",
})
CORE_CAPABILITIES = frozenset({
    "external-assets", "alternate-locations", "polygon-regions",
    "time-selectors", "parallel-layers", "partial-processing",
    "guided-reprocessing", "entity-links", "retrieval-projection",
    "embedding-reference", "release-pins", "custom-registries",
})
LIFECYCLE_STATES = frozenset({"planned", "running", "partial", "failed", "complete"})
EDITORIAL_STATUSES = frozenset({
    "machine-draft", "human-draft", "under-review", "approved", "frozen", "superseded",
})
ACTOR_TYPES = frozenset({"human", "software", "model", "service", "import"})
REVIEW_DECISIONS = frozenset({"approve", "reject", "request-changes", "abstain"})
RELEASE_STATES = frozenset({"draft", "candidate", "published", "withdrawn"})

_GENERATED_MEMBERS = frozenset({
    "manifest.json",
    "INSTRUCTIONS.md",
    "checksums.sha256",
    *(f"schemas/{name}" for name in SCHEMA_FILES),
})
_ALLOWED_PREFIXES = (
    "assets/", "authority-snapshots/", "extensions/", "layers/", "metadata/",
    "retrieval/", "sources/",
)
_FORBIDDEN_MEMBER_SUFFIXES = (".sqlite", ".sqlite3", ".db", ".db3")
_FIXED_ZIP_TIME = (1980, 1, 1, 0, 0, 0)
_PRIVATE_KEYS = {
    "api_key", "apikey", "authorization", "cookie", "credential", "file_path",
    "local_path", "password", "path", "presigned_url", "private_key", "secret",
    "session", "signature", "token",
}


class Lib4Error(Exception):
    """Raised when a package cannot be read, validated, or sealed safely."""


@dataclass(frozen=True, slots=True)
class Issue:
    level: str
    location: str
    message: str


@dataclass(slots=True)
class Lib4Document:
    manifest: dict[str, Any]
    members: dict[str, bytes]

    def resource_bytes(self, resource_id: str) -> bytes | None:
        """Return embedded bytes for a resource, or ``None`` for remote-only data."""

        for resource in self.manifest.get("resources", []):
            if not isinstance(resource, dict) or resource.get("id") != resource_id:
                continue
            for location in sorted(
                (item for item in resource.get("locations", []) if isinstance(item, dict)),
                key=lambda item: item.get("priority", 0),
            ):
                if location.get("type") == "embedded":
                    return self.members.get(location.get("member"))
        return None


def _issue(issues: list[Issue], location: str, message: str, level: str = "error") -> None:
    issues.append(Issue(level, location, message))


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _canonical_json(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, separators=(",", ": "))
        + "\n"
    ).encode("utf-8")


def _strict_json(payload: bytes, location: str) -> Any:
    if len(payload) > MAX_JSON_BYTES:
        raise Lib4Error(f"{location}: JSON exceeds {MAX_JSON_BYTES} bytes")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise Lib4Error(f"{location}: JSON is not UTF-8") from exc

    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite number {value}")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate key {key!r}")
            result[key] = value
        return result

    try:
        value = json.loads(text, object_pairs_hook=unique_object, parse_constant=reject_constant)
    except (json.JSONDecodeError, ValueError) as exc:
        raise Lib4Error(f"{location}: invalid JSON: {exc}") from exc
    if _nesting_exceeds(value):
        raise Lib4Error(f"{location}: JSON nesting exceeds {MAX_NESTING}")
    return value


def _nesting_exceeds(value: Any) -> bool:
    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack:
        current, depth = stack.pop()
        if depth > MAX_NESTING:
            return True
        if isinstance(current, dict):
            stack.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            stack.extend((item, depth + 1) for item in current)
    return False


def _is_id(value: Any) -> bool:
    return isinstance(value, str) and bool(PORTABLE_ID_RE.fullmatch(value))


def _is_revision(value: Any) -> bool:
    return isinstance(value, str) and bool(PORTABLE_REVISION_RE.fullmatch(value))


def _is_timestamp(value: Any) -> bool:
    if not isinstance(value, str) or not value.endswith(("Z", "+00:00")):
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _is_namespaced_id(value: Any) -> bool:
    return _is_id(value) and (str(value).startswith("x-") or ":" in str(value))


def _safe_member(name: Any) -> bool:
    if not isinstance(name, str) or not name or len(name) > 512:
        return False
    if "\\" in name or name.startswith("/") or name.endswith("/"):
        return False
    if any(ord(char) > 127 or ord(char) < 32 for char in name):
        return False
    parts = name.split("/")
    return not any(
        part in {"", ".", ".."} or not PORTABLE_SEGMENT_RE.fullmatch(part)
        for part in parts
    )


def _allowed_member(name: str) -> bool:
    lowered = name.casefold()
    if lowered.endswith(_FORBIDDEN_MEMBER_SUFFIXES):
        return False
    return name in _GENERATED_MEMBERS or name.startswith(_ALLOWED_PREFIXES)


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    return ((info.external_attr >> 16) & 0o170000) == 0o120000


def _check_keys(
    issues: list[Issue],
    value: Any,
    location: str,
    *,
    required: Iterable[str] = (),
    allowed: Iterable[str] | None = None,
) -> bool:
    if not isinstance(value, dict):
        _issue(issues, location, "must be an object")
        return False
    for key in required:
        if key not in value:
            _issue(issues, f"{location}.{key}", "is required")
    if allowed is not None:
        allowed_set = set(allowed)
        for key in value:
            if key not in allowed_set:
                _issue(issues, f"{location}.{key}", "unknown field; custom data belongs in ext")
    return True


def _check_ext(issues: list[Issue], value: Any, location: str) -> None:
    if not isinstance(value, dict):
        _issue(issues, location, "must be an object")
        return
    try:
        size = len(_canonical_json(value))
    except (TypeError, ValueError):
        _issue(issues, location, "must contain finite JSON values only")
        return
    if size > MAX_EXT_BYTES:
        _issue(issues, location, f"exceeds the {MAX_EXT_BYTES}-byte extension limit")
    stack: list[tuple[Any, str]] = [(value, location)]
    while stack:
        current, loc = stack.pop()
        if isinstance(current, dict):
            for key, item in current.items():
                if str(key).casefold() in _PRIVATE_KEYS:
                    _issue(issues, f"{loc}.{key}", "credentials and local/private locators are forbidden")
                stack.append((item, f"{loc}.{key}"))
        elif isinstance(current, list):
            stack.extend((item, f"{loc}[{index}]") for index, item in enumerate(current))
        elif isinstance(current, float) and not math.isfinite(current):
            _issue(issues, loc, "non-finite numbers are forbidden")


def _check_actor(issues: list[Issue], actor: Any, location: str) -> None:
    fields = ("type", "id", "label", "ext")
    if not _check_keys(
        issues, actor, location, required=fields,
        allowed=(*fields, "version", "uri"),
    ):
        return
    if actor.get("type") not in ACTOR_TYPES:
        _issue(issues, f"{location}.type", f"must be one of {sorted(ACTOR_TYPES)}")
    if not _is_id(actor.get("id")):
        _issue(issues, f"{location}.id", "must be a portable stable identifier")
    if not isinstance(actor.get("label"), str) or not actor["label"].strip():
        _issue(issues, f"{location}.label", "must be non-empty text")
    if "uri" in actor and actor["uri"] is not None:
        _check_public_uri(issues, actor["uri"], f"{location}.uri", schemes={"http", "https", "urn"})
    _check_ext(issues, actor.get("ext", {}), f"{location}.ext")


def _check_public_uri(
    issues: list[Issue], value: Any, location: str, *, schemes: set[str], stable: bool = False,
) -> None:
    if not isinstance(value, str) or len(value) > 4096 or "\\" in value or any(ord(c) < 32 for c in value):
        _issue(issues, location, "must be a bounded URI")
        return
    parsed = urlsplit(value)
    if parsed.scheme.casefold() not in schemes:
        _issue(issues, location, f"scheme must be one of {sorted(schemes)}")
        return
    if parsed.scheme.casefold() != "urn" and not parsed.netloc:
        _issue(issues, location, "must include an authority/bucket")
    if parsed.username or parsed.password:
        _issue(issues, location, "URI user information is forbidden")
    if stable and (parsed.query or parsed.fragment):
        _issue(issues, location, "stable resource locations may not contain query strings or fragments")


def _check_lib4_uri(issues: list[Issue], value: Any, location: str, package_id: str) -> None:
    prefix = f"lib4://package/{package_id}"
    if not isinstance(value, str) or not value.startswith(prefix) or "\\" in value or " " in value:
        _issue(issues, location, f"must be a stable URI beneath {prefix}")


def _registry_sets(issues: list[Issue], registries: Any) -> dict[str, set[str]]:
    names = (
        "layer_kinds", "resource_roles", "structure_types", "material_types",
        "selector_types", "relation_predicates", "capabilities",
    )
    result = {name: set() for name in names}
    if not _check_keys(issues, registries, "manifest.registries", required=names, allowed=names):
        return result
    core = {
        "layer_kinds": CORE_LAYER_KINDS,
        "resource_roles": CORE_RESOURCE_ROLES,
        "structure_types": CORE_STRUCTURE_TYPES,
        "material_types": CORE_MATERIAL_TYPES,
        "selector_types": CORE_SELECTOR_TYPES,
        "relation_predicates": CORE_RELATION_PREDICATES,
        "capabilities": CORE_CAPABILITIES,
    }
    for name in names:
        entries = registries.get(name)
        if not isinstance(entries, list):
            _issue(issues, f"manifest.registries.{name}", "must be an array")
            continue
        for index, entry in enumerate(entries):
            loc = f"manifest.registries.{name}[{index}]"
            required = ("id", "label", "description", "schema_uri", "renderer_hint", "ext")
            if not _check_keys(issues, entry, loc, required=required, allowed=required):
                continue
            entry_id = entry.get("id")
            if not _is_namespaced_id(entry_id):
                _issue(issues, f"{loc}.id", "extension identifiers must be namespaced with ':' or x-")
            elif entry_id in core[name]:
                _issue(issues, f"{loc}.id", "may not redefine a core identifier")
            elif entry_id in result[name]:
                _issue(issues, f"{loc}.id", "duplicates another registry entry")
            else:
                result[name].add(entry_id)
            schema_uri = entry.get("schema_uri")
            if schema_uri is not None:
                _check_public_uri(issues, schema_uri, f"{loc}.schema_uri", schemes={"https", "urn"})
            _check_ext(issues, entry.get("ext", {}), f"{loc}.ext")
    return result


def _check_lifecycle(issues: list[Issue], value: Any, location: str) -> None:
    required = ("state", "completeness", "updated_at", "message", "retryable", "ext")
    if not _check_keys(issues, value, location, required=required, allowed=required):
        return
    state = value.get("state")
    completeness = value.get("completeness")
    if state not in LIFECYCLE_STATES:
        _issue(issues, f"{location}.state", f"must be one of {sorted(LIFECYCLE_STATES)}")
    if (
        not isinstance(completeness, (int, float))
        or isinstance(completeness, bool)
        or not math.isfinite(float(completeness))
        or not 0 <= float(completeness) <= 1
    ):
        _issue(issues, f"{location}.completeness", "must be a finite number from 0 through 1")
    elif state == "complete" and completeness != 1:
        _issue(issues, f"{location}.completeness", "a complete layer must have completeness 1")
    elif state == "planned" and completeness != 0:
        _issue(issues, f"{location}.completeness", "a planned layer must have completeness 0")
    elif state in {"running", "partial"} and completeness >= 1:
        _issue(issues, f"{location}.completeness", f"a {state} layer must be less than 1")
    if not _is_timestamp(value.get("updated_at")):
        _issue(issues, f"{location}.updated_at", "must be an ISO 8601 UTC timestamp")
    if value.get("message") is not None and not isinstance(value.get("message"), str):
        _issue(issues, f"{location}.message", "must be text or null")
    if not isinstance(value.get("retryable"), bool):
        _issue(issues, f"{location}.retryable", "must be boolean")
    _check_ext(issues, value.get("ext", {}), f"{location}.ext")


def _check_catalog(
    issues: list[Issue], catalog: Any,
) -> tuple[str | None, set[str], set[str], set[str]]:
    """Apply the small catalog subset needed for package links.

    Full statement/projection semantics live in the bundled catalog schema and
    generation guide.  Keeping this validator independent of JSON Schema is a
    deliberate no-dependency choice.
    """

    if not isinstance(catalog, dict):
        _issue(issues, "manifest.catalog", "must be an inline lib4-catalog object")
        return None, set(), set(), set()
    if catalog.get("profile") != "lib4-catalog/1.0":
        _issue(issues, "manifest.catalog.profile", "must be lib4-catalog/1.0")
    record = catalog.get("record")
    revision: str | None = None
    if not isinstance(record, dict):
        _issue(issues, "manifest.catalog.record", "must be an object")
    else:
        if not _is_id(record.get("id")):
            _issue(issues, "manifest.catalog.record.id", "must be a portable stable identifier")
        if not _is_revision(record.get("revision")):
            _issue(issues, "manifest.catalog.record.revision", "must be a portable revision")
        else:
            revision = record["revision"]
        if record.get("status") not in {
            "machine-draft", "human-draft", "under-review", "approved",
            "published", "superseded", "tombstoned",
        }:
            _issue(issues, "manifest.catalog.record.status", "is not a catalog record status")
        completion = record.get("completion")
        if not isinstance(completion, dict):
            _issue(issues, "manifest.catalog.record.completion", "must explicitly describe record completeness")
        elif completion.get("state") not in LIFECYCLE_STATES:
            _issue(issues, "manifest.catalog.record.completion.state", "must be a core lifecycle state")
    entity_ids: set[str] = set()
    entities = catalog.get("entities")
    if not isinstance(entities, list):
        _issue(issues, "manifest.catalog.entities", "must be an array")
    else:
        for index, entity in enumerate(entities):
            entity_id = entity.get("id") if isinstance(entity, dict) else None
            if not _is_id(entity_id) or entity_id in entity_ids:
                _issue(issues, f"manifest.catalog.entities[{index}].id", "must be a unique portable identifier")
            else:
                entity_ids.add(entity_id)
    primary = catalog.get("primary_entity_id")
    if primary not in entity_ids:
        _issue(issues, "manifest.catalog.primary_entity_id", "must reference a catalog entity")
    if "ext" in catalog:
        _check_ext(issues, catalog.get("ext"), "manifest.catalog.ext")
    policy_sets: list[set[str]] = []
    for field in ("rights_policies", "access_policies"):
        policy_ids: set[str] = set()
        policies = catalog.get(field)
        if not isinstance(policies, list):
            _issue(issues, f"manifest.catalog.{field}", "must be an array")
        else:
            for index, policy in enumerate(policies):
                policy_id = policy.get("id") if isinstance(policy, dict) else None
                if not _is_id(policy_id) or policy_id in policy_ids:
                    _issue(issues, f"manifest.catalog.{field}[{index}].id", "must be a unique portable identifier")
                else:
                    policy_ids.add(policy_id)
        policy_sets.append(policy_ids)
    return revision, entity_ids, policy_sets[0], policy_sets[1]


def _resource_locations(
    issues: list[Issue], resource: Mapping[str, Any], location: str,
) -> list[str]:
    locations = resource.get("locations")
    embedded: list[str] = []
    if not isinstance(locations, list) or not locations:
        _issue(issues, f"{location}.locations", "must be a non-empty array")
        return embedded
    priorities: set[int] = set()
    seen: set[tuple[str, str]] = set()
    for index, item in enumerate(locations):
        loc = f"{location}.locations[{index}]"
        if not isinstance(item, dict):
            _issue(issues, loc, "must be an object")
            continue
        location_type = item.get("type")
        priority = item.get("priority")
        if not isinstance(priority, int) or isinstance(priority, bool) or priority < 0:
            _issue(issues, f"{loc}.priority", "must be a non-negative integer")
        elif priority in priorities:
            _issue(issues, f"{loc}.priority", "must be unique within a resource")
        else:
            priorities.add(priority)
        if location_type == "embedded":
            allowed = ("type", "member", "priority", "ext")
            _check_keys(issues, item, loc, required=allowed, allowed=allowed)
            member = item.get("member")
            if not _safe_member(member) or not _allowed_member(str(member)) or member in _GENERATED_MEMBERS:
                _issue(issues, f"{loc}.member", "must be a portable, allowed data member")
            elif ("embedded", member) in seen:
                _issue(issues, f"{loc}.member", "duplicates another location")
            else:
                embedded.append(member)
                seen.add(("embedded", member))
        elif location_type in {"http", "s3", "iiif"}:
            required = ("type", "uri", "priority", "ext")
            allowed = (*required, "service")
            _check_keys(issues, item, loc, required=required, allowed=allowed)
            uri = item.get("uri")
            schemes = {"s3"} if location_type == "s3" else {"https", "http"}
            _check_public_uri(issues, uri, f"{loc}.uri", schemes=schemes, stable=True)
            if isinstance(uri, str) and (location_type, uri) in seen:
                _issue(issues, f"{loc}.uri", "duplicates another location")
            elif isinstance(uri, str):
                seen.add((location_type, uri))
            if item.get("service") is not None:
                _check_public_uri(
                    issues, item["service"], f"{loc}.service",
                    schemes={"https", "http"}, stable=True,
                )
        else:
            _issue(issues, f"{loc}.type", "must be embedded, http, s3, or iiif")
        _check_ext(issues, item.get("ext", {}), f"{loc}.ext")
    return embedded


def _check_resource(
    issues: list[Issue], resource: Any, location: str, custom_roles: set[str],
    members: Mapping[str, bytes] | None,
) -> tuple[str | None, list[str]]:
    required = (
        "id", "role", "media_type", "byte_length", "integrity", "locations",
        "availability", "retrieval_policy", "rights_policy_id", "access_policy_id",
        "provenance", "ext",
    )
    if not _check_keys(issues, resource, location, required=required, allowed=required):
        return None, []
    resource_id = resource.get("id")
    if not _is_id(resource_id):
        _issue(issues, f"{location}.id", "must be a portable stable identifier")
        resource_id = None
    role = resource.get("role")
    if role not in CORE_RESOURCE_ROLES | custom_roles:
        _issue(issues, f"{location}.role", "must be a core or declared resource role")
    if not isinstance(resource.get("media_type"), str) or not MEDIA_TYPE_RE.fullmatch(resource["media_type"]):
        _issue(issues, f"{location}.media_type", "must be a media type without parameters")
    length = resource.get("byte_length")
    if length is not None and (
        not isinstance(length, int) or isinstance(length, bool) or not 0 <= length <= MAX_MEMBER_BYTES
    ):
        _issue(issues, f"{location}.byte_length", f"must be null or 0..{MAX_MEMBER_BYTES}")
    integrity = resource.get("integrity")
    digest: str | None = None
    integrity_state = integrity.get("state") if isinstance(integrity, dict) else None
    integrity_fields = ("state", "algorithm", "digest", "reason")
    if _check_keys(
        issues, integrity, f"{location}.integrity",
        required=integrity_fields, allowed=integrity_fields,
    ):
        state = integrity.get("state")
        if state == "verified":
            if integrity.get("algorithm") != "sha256":
                _issue(issues, f"{location}.integrity.algorithm", "verified integrity must use sha256")
            if not isinstance(integrity.get("digest"), str) or not SHA256_RE.fullmatch(integrity["digest"]):
                _issue(issues, f"{location}.integrity.digest", "must be a lowercase SHA-256 digest")
            else:
                digest = integrity["digest"]
            if integrity.get("reason") is not None:
                _issue(issues, f"{location}.integrity.reason", "must be null when verified")
        elif state in {"unverified", "unavailable"}:
            if integrity.get("algorithm") is not None or integrity.get("digest") is not None:
                _issue(issues, f"{location}.integrity", "unverified integrity cannot claim an algorithm or digest")
            if not isinstance(integrity.get("reason"), str) or not integrity["reason"].strip():
                _issue(issues, f"{location}.integrity.reason", "must explain why integrity is not verified")
        else:
            _issue(issues, f"{location}.integrity.state", "must be verified, unverified, or unavailable")
    embedded = _resource_locations(issues, resource, location)
    if members is not None and embedded and (
        length is None or digest is None or integrity_state != "verified"
    ):
        _issue(issues, location, "embedded resources require byte_length and SHA-256 integrity")
    if len(embedded) > 1:
        _issue(issues, f"{location}.locations", "one logical resource may have at most one embedded location")
    availability = resource.get("availability")
    availability_fields = ("state", "cache", "offline", "ext")
    if _check_keys(
        issues, availability, f"{location}.availability",
        required=availability_fields, allowed=(*availability_fields, "checked_at"),
    ):
        if availability.get("state") not in {"available", "anticipated", "restricted", "unavailable", "unknown"}:
            _issue(issues, f"{location}.availability.state", "invalid availability state")
        if availability.get("cache") not in {"allow", "require", "forbid"}:
            _issue(issues, f"{location}.availability.cache", "must be allow, require, or forbid")
        if availability.get("offline") not in {"embedded", "use-alternate", "metadata-only", "unavailable"}:
            _issue(issues, f"{location}.availability.offline", "invalid offline behavior")
        if availability.get("offline") == "embedded" and not embedded:
            _issue(issues, f"{location}.availability.offline", "requires an embedded location")
        if "checked_at" in availability and not _is_timestamp(availability.get("checked_at")):
            _issue(issues, f"{location}.availability.checked_at", "must be an ISO 8601 UTC timestamp")
        _check_ext(issues, availability.get("ext", {}), f"{location}.availability.ext")
    retrieval_policy = resource.get("retrieval_policy")
    retrieval_fields = ("mode", "authentication", "max_bytes", "ext")
    if _check_keys(
        issues, retrieval_policy, f"{location}.retrieval_policy",
        required=retrieval_fields, allowed=retrieval_fields,
    ):
        if retrieval_policy.get("mode") not in {
            "manual", "on-demand", "prefetch-allowed", "embedded-only", "forbidden",
        }:
            _issue(issues, f"{location}.retrieval_policy.mode", "invalid retrieval mode")
        if retrieval_policy.get("authentication") not in {
            "none", "application-managed", "user-mediated", "unavailable",
        }:
            _issue(issues, f"{location}.retrieval_policy.authentication", "invalid authentication mode")
        max_bytes = retrieval_policy.get("max_bytes")
        if max_bytes is not None and (
            not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes < 0
        ):
            _issue(issues, f"{location}.retrieval_policy.max_bytes", "must be null or a non-negative integer")
        _check_ext(issues, retrieval_policy.get("ext", {}), f"{location}.retrieval_policy.ext")
    provenance = resource.get("provenance")
    fields = ("actor", "at", "method", "sources", "ext")
    if _check_keys(issues, provenance, f"{location}.provenance", required=fields, allowed=fields):
        _check_actor(issues, provenance.get("actor"), f"{location}.provenance.actor")
        if not _is_timestamp(provenance.get("at")):
            _issue(issues, f"{location}.provenance.at", "must be an ISO 8601 UTC timestamp")
        if not isinstance(provenance.get("method"), str) or not provenance["method"].strip():
            _issue(issues, f"{location}.provenance.method", "must be non-empty text")
        if not isinstance(provenance.get("sources"), list):
            _issue(issues, f"{location}.provenance.sources", "must be an array")
        _check_ext(issues, provenance.get("ext", {}), f"{location}.provenance.ext")
    _check_ext(issues, resource.get("ext", {}), f"{location}.ext")
    if members is not None:
        for member in embedded:
            payload = members.get(member)
            if payload is None:
                _issue(issues, f"{location}.locations", f"embedded member {member} is absent")
            else:
                if length != len(payload):
                    _issue(issues, f"{location}.byte_length", "does not match embedded bytes")
                if digest != _sha256(payload):
                    _issue(issues, f"{location}.integrity.digest", "does not match embedded bytes")
    return resource_id, embedded


def _check_selector(
    issues: list[Issue], selector: Any, location: str, canvas_revision: str,
    custom_selector_types: set[str], duration_ms: int | None,
) -> None:
    if not isinstance(selector, dict):
        _issue(issues, location, "must be an object")
        return
    selector_type = selector.get("type")
    if selector.get("canvas_revision") != canvas_revision:
        _issue(issues, f"{location}.canvas_revision", "must pin the referenced canvas revision")
    if selector_type == "box":
        required = ("type", "coordinate_space", "canvas_revision", "x", "y", "width", "height")
        _check_keys(issues, selector, location, required=required, allowed=required)
        if selector.get("coordinate_space") != "canvas-normalized":
            _issue(issues, f"{location}.coordinate_space", "must be canvas-normalized")
        values = [selector.get(name) for name in ("x", "y", "width", "height")]
        if any(not isinstance(value, (int, float)) or isinstance(value, bool) for value in values):
            _issue(issues, location, "box coordinates must be numeric")
        else:
            x, y, width, height = map(float, values)
            if x < 0 or y < 0 or width <= 0 or height <= 0 or x + width > 1.0000001 or y + height > 1.0000001:
                _issue(issues, location, "box must have positive area inside normalized bounds")
    elif selector_type == "polygon":
        required = ("type", "coordinate_space", "canvas_revision", "points")
        _check_keys(issues, selector, location, required=required, allowed=required)
        if selector.get("coordinate_space") != "canvas-normalized":
            _issue(issues, f"{location}.coordinate_space", "must be canvas-normalized")
        points = selector.get("points")
        if not isinstance(points, list) or not 3 <= len(points) <= 256:
            _issue(issues, f"{location}.points", "must contain 3 to 256 points")
        else:
            parsed: list[tuple[float, float]] = []
            for index, point in enumerate(points):
                if not isinstance(point, dict) or set(point) != {"x", "y"}:
                    _issue(issues, f"{location}.points[{index}]", "must contain exactly x and y")
                    continue
                x, y = point.get("x"), point.get("y")
                if any(not isinstance(value, (int, float)) or isinstance(value, bool) for value in (x, y)):
                    _issue(issues, f"{location}.points[{index}]", "coordinates must be numeric")
                    continue
                if not 0 <= float(x) <= 1 or not 0 <= float(y) <= 1:
                    _issue(issues, f"{location}.points[{index}]", "coordinates must be in 0..1")
                parsed.append((float(x), float(y)))
            if len(set(parsed)) < 3:
                _issue(issues, f"{location}.points", "must contain three distinct points")
            elif len(parsed) == len(points):
                area = abs(sum(
                    parsed[index][0] * parsed[(index + 1) % len(parsed)][1]
                    - parsed[(index + 1) % len(parsed)][0] * parsed[index][1]
                    for index in range(len(parsed))
                )) / 2
                if area <= 1e-10:
                    _issue(issues, f"{location}.points", "polygon must have non-zero area")
    elif selector_type == "time-range":
        required = ("type", "coordinate_space", "canvas_revision", "start_ms", "end_ms")
        _check_keys(issues, selector, location, required=required, allowed=required)
        start, end = selector.get("start_ms"), selector.get("end_ms")
        if selector.get("coordinate_space") != "media-time":
            _issue(issues, f"{location}.coordinate_space", "must be media-time")
        if not isinstance(start, int) or isinstance(start, bool) or start < 0:
            _issue(issues, f"{location}.start_ms", "must be a non-negative integer")
        if not isinstance(end, int) or isinstance(end, bool) or end <= 0:
            _issue(issues, f"{location}.end_ms", "must be a positive integer")
        if isinstance(start, int) and isinstance(end, int) and end <= start:
            _issue(issues, location, "time range end must follow start")
        if duration_ms is not None and isinstance(end, int) and end > duration_ms:
            _issue(issues, f"{location}.end_ms", "exceeds canvas duration")
    elif selector_type in custom_selector_types:
        required = ("type", "coordinate_space", "canvas_revision", "data", "fallback")
        _check_keys(issues, selector, location, required=required, allowed=required)
        if not isinstance(selector.get("data"), dict):
            _issue(issues, f"{location}.data", "must be an object")
        fallback = selector.get("fallback")
        if not isinstance(fallback, dict) or fallback.get("type") not in {"box", "polygon", "time-range"}:
            _issue(issues, f"{location}.fallback", "custom selectors require a core fallback")
        else:
            _check_selector(issues, fallback, f"{location}.fallback", canvas_revision, set(), duration_ms)
    else:
        _issue(issues, f"{location}.type", "must be a core or declared selector type")


def _walk_data_semantics(
    issues: list[Issue], value: Any, location: str, *, package_id: str,
    canvases: Mapping[str, Mapping[str, Any]], custom_selector_types: set[str],
) -> None:
    stack: list[tuple[Any, str]] = [(value, location)]
    while stack:
        current, loc = stack.pop()
        if isinstance(current, list):
            stack.extend((item, f"{loc}[{index}]") for index, item in enumerate(current))
            continue
        if not isinstance(current, dict):
            continue
        if "canvas_id" in current:
            canvas_id, revision = current.get("canvas_id"), current.get("canvas_revision")
            canvas = canvases.get(canvas_id) if isinstance(canvas_id, str) else None
            if canvas is None:
                _issue(issues, f"{loc}.canvas_id", "references a missing canvas")
            elif revision != canvas.get("revision"):
                _issue(issues, f"{loc}.canvas_revision", "does not pin the canvas revision")
            if current.get("selector") is not None and canvas is not None:
                _check_selector(
                    issues, current["selector"], f"{loc}.selector", str(canvas["revision"]),
                    custom_selector_types, canvas.get("duration_ms"),
                )
        if current.get("type") == "text-quote":
            required = ("type", "layer_id", "revision", "item_id", "start", "end", "exact", "prefix", "suffix", "status")
            _check_keys(issues, current, loc, required=required, allowed=required)
            start, end, exact = current.get("start"), current.get("end"), current.get("exact")
            if not isinstance(start, int) or not isinstance(end, int) or start < 0 or end < start:
                _issue(issues, loc, "text offsets must be ordered non-negative integers")
            elif isinstance(exact, str) and end - start != len(exact):
                _issue(issues, f"{loc}.exact", "length must equal end - start")
            if current.get("status") not in {"current", "stale", "ambiguous"}:
                _issue(issues, f"{loc}.status", "must be current, stale, or ambiguous")
        for key, item in current.items():
            child_loc = f"{loc}.{key}"
            if key.endswith("_uri") and item is not None:
                if isinstance(item, str) and item.startswith("lib4://"):
                    _check_lib4_uri(issues, item, child_loc, package_id)
            elif key.endswith("_uris") and isinstance(item, list):
                for index, uri in enumerate(item):
                    if isinstance(uri, str) and uri.startswith("lib4://"):
                        _check_lib4_uri(issues, uri, f"{child_loc}[{index}]", package_id)
            stack.append((item, child_loc))


def validate_layer(
    layer: Any,
    manifest: Mapping[str, Any] | None = None,
    descriptor: Mapping[str, Any] | None = None,
) -> list[Issue]:
    """Validate one immutable layer envelope and its stable anchors."""

    issues: list[Issue] = []
    required = (
        "$schema", "id", "revision", "kind", "label", "language", "lifecycle",
        "editorial_status", "coverage", "provenance", "dependencies", "history",
        "reviews", "errors", "data", "ext",
    )
    if not _check_keys(issues, layer, "layer", required=required, allowed=required):
        return issues
    if layer.get("$schema") != LAYER_SCHEMA_ID:
        _issue(issues, "layer.$schema", f"must be {LAYER_SCHEMA_ID}")
    if not _is_id(layer.get("id")):
        _issue(issues, "layer.id", "must be a portable stable identifier")
    if not _is_revision(layer.get("revision")):
        _issue(issues, "layer.revision", "must be a portable revision")
    registry_sets = _registry_sets([], (manifest or {}).get("registries", {
        "layer_kinds": [], "resource_roles": [], "structure_types": [], "material_types": [],
        "selector_types": [], "relation_predicates": [], "capabilities": [],
    }))
    if layer.get("kind") not in CORE_LAYER_KINDS | registry_sets["layer_kinds"]:
        _issue(issues, "layer.kind", "must be a core or declared layer kind")
    if layer.get("editorial_status") not in EDITORIAL_STATUSES:
        _issue(issues, "layer.editorial_status", f"must be one of {sorted(EDITORIAL_STATUSES)}")
    language = layer.get("language")
    if language is not None and (not isinstance(language, str) or not LANGUAGE_RE.fullmatch(language)):
        _issue(issues, "layer.language", "must be null or a BCP 47-like language tag")
    _check_lifecycle(issues, layer.get("lifecycle"), "layer.lifecycle")
    if descriptor is not None:
        for field in ("id", "revision", "kind", "label", "language", "lifecycle", "editorial_status"):
            if layer.get(field) != descriptor.get(field):
                _issue(issues, f"layer.{field}", "does not match the manifest descriptor")

    coverage = layer.get("coverage")
    coverage_fields = ("scope", "target_uris", "completed_items", "total_items", "omissions", "ext")
    if _check_keys(issues, coverage, "layer.coverage", required=coverage_fields, allowed=coverage_fields):
        if coverage.get("scope") not in {"all", "selection", "unknown"}:
            _issue(issues, "layer.coverage.scope", "must be all, selection, or unknown")
        for field in ("completed_items", "total_items"):
            value = coverage.get(field)
            if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 0):
                _issue(issues, f"layer.coverage.{field}", "must be null or a non-negative integer")
        completed, total = coverage.get("completed_items"), coverage.get("total_items")
        if isinstance(completed, int) and isinstance(total, int) and completed > total:
            _issue(issues, "layer.coverage.completed_items", "may not exceed total_items")
        if not isinstance(coverage.get("target_uris"), list) or not isinstance(coverage.get("omissions"), list):
            _issue(issues, "layer.coverage", "target_uris and omissions must be arrays")
        _check_ext(issues, coverage.get("ext", {}), "layer.coverage.ext")

    provenance = layer.get("provenance")
    provenance_fields = ("actor", "generated_at", "method", "run_id", "parameters", "sources", "ext")
    if _check_keys(
        issues, provenance, "layer.provenance", required=provenance_fields, allowed=provenance_fields,
    ):
        _check_actor(issues, provenance.get("actor"), "layer.provenance.actor")
        if not _is_timestamp(provenance.get("generated_at")):
            _issue(issues, "layer.provenance.generated_at", "must be an ISO 8601 UTC timestamp")
        if not _is_id(provenance.get("run_id")):
            _issue(issues, "layer.provenance.run_id", "must be a stable run identifier")
        if not isinstance(provenance.get("parameters"), dict):
            _issue(issues, "layer.provenance.parameters", "must be an object")
        if not isinstance(provenance.get("sources"), list):
            _issue(issues, "layer.provenance.sources", "must be an array")
        _check_ext(issues, provenance.get("ext", {}), "layer.provenance.ext")

    manifest_layer_keys = {
        (item.get("id"), item.get("revision"))
        for item in (manifest or {}).get("layers", []) if isinstance(item, dict)
    }
    dependencies = layer.get("dependencies")
    if not isinstance(dependencies, list):
        _issue(issues, "layer.dependencies", "must be an array")
    else:
        seen: set[tuple[Any, Any, Any]] = set()
        for index, dependency in enumerate(dependencies):
            loc = f"layer.dependencies[{index}]"
            fields = ("layer_id", "revision", "relation")
            if not _check_keys(issues, dependency, loc, required=fields, allowed=fields):
                continue
            key = (dependency.get("layer_id"), dependency.get("revision"))
            triple = (*key, dependency.get("relation"))
            if not _is_id(key[0]) or not _is_revision(key[1]) or not _is_id(triple[2]):
                _issue(issues, loc, "must contain portable pinned identifiers")
            if triple in seen:
                _issue(issues, loc, "duplicates another dependency")
            seen.add(triple)
            if manifest is not None and key not in manifest_layer_keys:
                _issue(issues, loc, "references a missing layer revision")
            if key == (layer.get("id"), layer.get("revision")):
                _issue(issues, loc, "a layer may not depend on itself")

    history = layer.get("history")
    if not isinstance(history, list) or not history:
        _issue(issues, "layer.history", "must contain at least a creation event")
    else:
        history_ids: set[str] = set()
        previous_time: str | None = None
        for index, event in enumerate(history):
            loc = f"layer.history[{index}]"
            fields = ("id", "action", "actor", "at", "message", "base_revision", "ext")
            if not _check_keys(issues, event, loc, required=fields, allowed=fields):
                continue
            event_id = event.get("id")
            if not _is_id(event_id) or event_id in history_ids:
                _issue(issues, f"{loc}.id", "must be a unique portable identifier")
            else:
                history_ids.add(event_id)
            _check_actor(issues, event.get("actor"), f"{loc}.actor")
            at = event.get("at")
            if not _is_timestamp(at):
                _issue(issues, f"{loc}.at", "must be an ISO 8601 UTC timestamp")
            elif previous_time is not None and at < previous_time:
                _issue(issues, f"{loc}.at", "history must be chronological")
            else:
                previous_time = at
            _check_ext(issues, event.get("ext", {}), f"{loc}.ext")

    reviews = layer.get("reviews")
    human_approval = False
    if not isinstance(reviews, list):
        _issue(issues, "layer.reviews", "must be an array")
    else:
        review_ids: set[str] = set()
        for index, review in enumerate(reviews):
            loc = f"layer.reviews[{index}]"
            fields = ("id", "decision", "reviewer", "at", "rationale", "applies_to_revision", "ext")
            if not _check_keys(issues, review, loc, required=fields, allowed=fields):
                continue
            review_id = review.get("id")
            if not _is_id(review_id) or review_id in review_ids:
                _issue(issues, f"{loc}.id", "must be a unique portable identifier")
            else:
                review_ids.add(review_id)
            if review.get("decision") not in REVIEW_DECISIONS:
                _issue(issues, f"{loc}.decision", "is not a core review decision")
            _check_actor(issues, review.get("reviewer"), f"{loc}.reviewer")
            actor_type = review.get("reviewer", {}).get("type") if isinstance(review.get("reviewer"), dict) else None
            if review.get("decision") in {"approve", "reject"} and actor_type != "human":
                _issue(issues, f"{loc}.reviewer.type", "only a human may approve or reject scholarly content")
            if review.get("decision") == "approve" and actor_type == "human" and review.get("applies_to_revision") == layer.get("revision"):
                human_approval = True
            if review.get("applies_to_revision") != layer.get("revision"):
                _issue(issues, f"{loc}.applies_to_revision", "must pin this exact layer revision")
            if not _is_timestamp(review.get("at")):
                _issue(issues, f"{loc}.at", "must be an ISO 8601 UTC timestamp")
            _check_ext(issues, review.get("ext", {}), f"{loc}.ext")
    if layer.get("editorial_status") in {"approved", "frozen"}:
        if layer.get("lifecycle", {}).get("state") != "complete":
            _issue(issues, "layer.editorial_status", "approved or frozen layers must be complete")
        if not human_approval:
            _issue(issues, "layer.editorial_status", "approved or frozen layers require exact human approval")

    errors = layer.get("errors")
    if not isinstance(errors, list):
        _issue(issues, "layer.errors", "must be an array")
    elif layer.get("lifecycle", {}).get("state") == "failed" and not errors:
        _issue(issues, "layer.errors", "a failed layer must record at least one processing error")
    if layer.get("lifecycle", {}).get("state") == "complete" and errors:
        _issue(issues, "layer.errors", "a complete layer cannot retain unresolved processing errors")

    data = layer.get("data")
    if not isinstance(data, dict):
        _issue(issues, "layer.data", "must be an object")
    elif manifest is not None:
        canvases = {
            item.get("id"): item for item in manifest.get("canvases", [])
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
        _walk_data_semantics(
            issues, data, "layer.data", package_id=str(manifest.get("package_id", "")),
            canvases=canvases, custom_selector_types=registry_sets["selector_types"],
        )
    _check_ext(issues, layer.get("ext", {}), "layer.ext")
    return issues


def validate_manifest(
    manifest: Any, members: Mapping[str, bytes] | None = None,
) -> list[Issue]:
    """Validate the manifest, cross-record references, and available bytes."""

    issues: list[Issue] = []
    required = (
        "$schema", "format", "profile", "profile_version", "package_id",
        "package_revision", "created_at", "generator", "catalog", "source_material",
        "structures", "canvases", "resources", "layers", "releases", "registries",
        "capabilities", "ext",
    )
    allowed = (*required, "catalog_resource_id", "retrieval")
    if not _check_keys(issues, manifest, "manifest", required=required, allowed=allowed):
        return issues
    if manifest.get("$schema") != MANIFEST_SCHEMA_ID:
        _issue(issues, "manifest.$schema", f"must be {MANIFEST_SCHEMA_ID}")
    if manifest.get("format") != FORMAT:
        _issue(issues, "manifest.format", f"must be {FORMAT}")
    if manifest.get("profile") != PROFILE:
        _issue(issues, "manifest.profile", f"must be {PROFILE}")
    if manifest.get("profile_version") != PROFILE_VERSION:
        _issue(issues, "manifest.profile_version", f"must be {PROFILE_VERSION}")
    package_id = manifest.get("package_id")
    if not _is_id(package_id):
        _issue(issues, "manifest.package_id", "must be a portable stable identifier")
        package_id = "invalid"
    if not _is_revision(manifest.get("package_revision")):
        _issue(issues, "manifest.package_revision", "must be a portable revision")
    if not _is_timestamp(manifest.get("created_at")):
        _issue(issues, "manifest.created_at", "must be an ISO 8601 UTC timestamp")
    _check_actor(issues, manifest.get("generator"), "manifest.generator")
    catalog_revision, _, rights_policy_ids, access_policy_ids = _check_catalog(
        issues, manifest.get("catalog"),
    )

    registry_sets = _registry_sets(issues, manifest.get("registries"))

    resources = manifest.get("resources")
    resource_by_id: dict[str, dict[str, Any]] = {}
    embedded_by_member: dict[str, str] = {}
    if not isinstance(resources, list):
        _issue(issues, "manifest.resources", "must be an array")
    else:
        for index, resource in enumerate(resources):
            loc = f"manifest.resources[{index}]"
            resource_id, embedded = _check_resource(
                issues, resource, loc, registry_sets["resource_roles"], members,
            )
            if resource_id is not None:
                if resource_id in resource_by_id:
                    _issue(issues, f"{loc}.id", "duplicates another resource")
                else:
                    resource_by_id[resource_id] = resource
            for member in embedded:
                if member in embedded_by_member:
                    _issue(issues, f"{loc}.locations", f"member is already owned by {embedded_by_member[member]}")
                elif resource_id is not None:
                    embedded_by_member[member] = resource_id
            if isinstance(resource, dict):
                rights_id = resource.get("rights_policy_id")
                access_id = resource.get("access_policy_id")
                if rights_id is not None and rights_id not in rights_policy_ids:
                    _issue(issues, f"{loc}.rights_policy_id", "must reference an inline catalog rights policy")
                if access_id is not None and access_id not in access_policy_ids:
                    _issue(issues, f"{loc}.access_policy_id", "must reference an inline catalog access policy")

    source_material = manifest.get("source_material")
    source_ids: set[str] = set()
    if not isinstance(source_material, list):
        _issue(issues, "manifest.source_material", "must be an array")
    else:
        for index, source in enumerate(source_material):
            loc = f"manifest.source_material[{index}]"
            fields = ("id", "material_type", "label", "media_type", "identifiers", "resource_ids", "rights_policy_id", "ext")
            if not _check_keys(issues, source, loc, required=fields, allowed=fields):
                continue
            source_id = source.get("id")
            if not _is_id(source_id) or source_id in source_ids:
                _issue(issues, f"{loc}.id", "must be a unique portable identifier")
            else:
                source_ids.add(source_id)
            if source.get("material_type") not in CORE_MATERIAL_TYPES | registry_sets["material_types"]:
                _issue(issues, f"{loc}.material_type", "must be a core or declared material type")
            rights_id = source.get("rights_policy_id")
            if rights_id is not None and rights_id not in rights_policy_ids:
                _issue(issues, f"{loc}.rights_policy_id", "must reference an inline catalog rights policy")
            resource_ids = source.get("resource_ids")
            if not isinstance(resource_ids, list):
                _issue(issues, f"{loc}.resource_ids", "must be an array")
            else:
                for resource_id in resource_ids:
                    if resource_id not in resource_by_id:
                        _issue(issues, f"{loc}.resource_ids", f"references missing resource {resource_id!r}")
            _check_ext(issues, source.get("ext", {}), f"{loc}.ext")

    structures = manifest.get("structures")
    structure_by_id: dict[str, dict[str, Any]] = {}
    if not isinstance(structures, list):
        _issue(issues, "manifest.structures", "must be an array")
    else:
        sibling_orders: set[tuple[Any, Any]] = set()
        for index, structure in enumerate(structures):
            loc = f"manifest.structures[{index}]"
            fields = ("id", "revision", "type", "label", "parent_id", "order", "target_uris", "metadata", "ext")
            if not _check_keys(issues, structure, loc, required=fields, allowed=fields):
                continue
            structure_id = structure.get("id")
            if not _is_id(structure_id) or structure_id in structure_by_id:
                _issue(issues, f"{loc}.id", "must be a unique portable identifier")
            else:
                structure_by_id[structure_id] = structure
            if not _is_revision(structure.get("revision")):
                _issue(issues, f"{loc}.revision", "must be a portable revision")
            if structure.get("type") not in CORE_STRUCTURE_TYPES | registry_sets["structure_types"]:
                _issue(issues, f"{loc}.type", "must be a core or declared structure type")
            parent_id, order = structure.get("parent_id"), structure.get("order")
            if parent_id is not None and not _is_id(parent_id):
                _issue(issues, f"{loc}.parent_id", "must be null or a portable identifier")
            if not isinstance(order, int) or isinstance(order, bool) or order < 0:
                _issue(issues, f"{loc}.order", "must be a non-negative integer")
            elif (parent_id, order) in sibling_orders:
                _issue(issues, f"{loc}.order", "duplicates a sibling order")
            else:
                sibling_orders.add((parent_id, order))
            target_uris = structure.get("target_uris")
            if not isinstance(target_uris, list):
                _issue(issues, f"{loc}.target_uris", "must be an array")
            else:
                for uri_index, uri in enumerate(target_uris):
                    _check_lib4_uri(issues, uri, f"{loc}.target_uris[{uri_index}]", package_id)
            if not isinstance(structure.get("metadata"), dict):
                _issue(issues, f"{loc}.metadata", "must be an object")
            _check_ext(issues, structure.get("ext", {}), f"{loc}.ext")
        for structure_id, structure in structure_by_id.items():
            parent = structure.get("parent_id")
            if parent is not None and parent not in structure_by_id:
                _issue(issues, f"manifest.structures[{structure_id}].parent_id", "references a missing structure")
            seen: set[str] = set()
            cursor: str | None = structure_id
            while cursor is not None and cursor in structure_by_id:
                if cursor in seen:
                    _issue(issues, f"manifest.structures[{structure_id}]", "hierarchy contains a cycle")
                    break
                seen.add(cursor)
                cursor = structure_by_id[cursor].get("parent_id")

    canvases = manifest.get("canvases")
    canvas_by_id: dict[str, dict[str, Any]] = {}
    sequences: set[int] = set()
    if not isinstance(canvases, list):
        _issue(issues, "manifest.canvases", "must be an array")
    else:
        for index, canvas in enumerate(canvases):
            loc = f"manifest.canvases[{index}]"
            fields = ("id", "revision", "sequence", "label", "structure_ids", "image_resource_id", "dimensions", "duration_ms", "ext")
            if not _check_keys(issues, canvas, loc, required=fields, allowed=fields):
                continue
            canvas_id = canvas.get("id")
            if not _is_id(canvas_id) or canvas_id in canvas_by_id:
                _issue(issues, f"{loc}.id", "must be a unique portable identifier")
            else:
                canvas_by_id[canvas_id] = canvas
            if not _is_revision(canvas.get("revision")):
                _issue(issues, f"{loc}.revision", "must be a portable revision")
            sequence = canvas.get("sequence")
            if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
                _issue(issues, f"{loc}.sequence", "must be a positive integer")
            elif sequence in sequences:
                _issue(issues, f"{loc}.sequence", "duplicates another canvas sequence")
            else:
                sequences.add(sequence)
            structure_ids = canvas.get("structure_ids")
            if not isinstance(structure_ids, list):
                _issue(issues, f"{loc}.structure_ids", "must be an array")
            else:
                for structure_id in structure_ids:
                    if structure_id not in structure_by_id:
                        _issue(issues, f"{loc}.structure_ids", f"references missing structure {structure_id!r}")
            image_resource_id = canvas.get("image_resource_id")
            if image_resource_id is not None:
                resource = resource_by_id.get(image_resource_id)
                if resource is None:
                    _issue(issues, f"{loc}.image_resource_id", "references a missing resource")
                elif resource.get("role") != "canvas-image":
                    _issue(issues, f"{loc}.image_resource_id", "must reference a canvas-image resource")
            dimensions = canvas.get("dimensions")
            if dimensions is not None and (
                not isinstance(dimensions, dict)
                or set(dimensions) != {"width", "height"}
                or any(not isinstance(dimensions.get(key), int) or dimensions[key] < 1 for key in ("width", "height"))
            ):
                _issue(issues, f"{loc}.dimensions", "must be null or positive integer width and height")
            duration = canvas.get("duration_ms")
            if duration is not None and (not isinstance(duration, int) or isinstance(duration, bool) or duration < 1):
                _issue(issues, f"{loc}.duration_ms", "must be null or a positive integer")
            _check_ext(issues, canvas.get("ext", {}), f"{loc}.ext")
        if sequences and sequences != set(range(1, len(sequences) + 1)):
            _issue(issues, "manifest.canvases", "sequence values must form an unbroken 1-based range")

    descriptors = manifest.get("layers")
    descriptor_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    current_layer_ids: set[str] = set()
    if not isinstance(descriptors, list):
        _issue(issues, "manifest.layers", "must be an array")
    else:
        for index, descriptor in enumerate(descriptors):
            loc = f"manifest.layers[{index}]"
            fields = (
                "id", "revision", "kind", "label", "variant", "language", "current",
                "lifecycle", "editorial_status", "content_resource_id", "supersedes", "ext",
            )
            if not _check_keys(issues, descriptor, loc, required=fields, allowed=fields):
                continue
            layer_id, revision = descriptor.get("id"), descriptor.get("revision")
            if not _is_id(layer_id):
                _issue(issues, f"{loc}.id", "must be a portable stable identifier")
            if not _is_revision(revision):
                _issue(issues, f"{loc}.revision", "must be a portable revision")
            key = (layer_id, revision)
            if key in descriptor_by_key:
                _issue(issues, loc, "duplicates another layer id/revision")
            elif isinstance(layer_id, str) and isinstance(revision, str):
                descriptor_by_key[key] = descriptor
            if descriptor.get("kind") not in CORE_LAYER_KINDS | registry_sets["layer_kinds"]:
                _issue(issues, f"{loc}.kind", "must be a core or declared layer kind")
            if descriptor.get("editorial_status") not in EDITORIAL_STATUSES:
                _issue(issues, f"{loc}.editorial_status", "is not a core editorial status")
            _check_lifecycle(issues, descriptor.get("lifecycle"), f"{loc}.lifecycle")
            if descriptor.get("current") is True:
                if layer_id in current_layer_ids:
                    _issue(issues, f"{loc}.current", "only one revision per layer id may be current")
                current_layer_ids.add(layer_id)
            elif descriptor.get("current") is not False:
                _issue(issues, f"{loc}.current", "must be boolean")
            resource_id = descriptor.get("content_resource_id")
            state = descriptor.get("lifecycle", {}).get("state") if isinstance(descriptor.get("lifecycle"), dict) else None
            if state in {"partial", "complete"} and resource_id is None:
                _issue(issues, f"{loc}.content_resource_id", f"a {state} layer requires content")
            if resource_id is not None:
                resource = resource_by_id.get(resource_id)
                if resource is None:
                    _issue(issues, f"{loc}.content_resource_id", "references a missing resource")
                elif resource.get("role") != "layer" or resource.get("media_type") != "application/json":
                    _issue(issues, f"{loc}.content_resource_id", "must reference an application/json layer resource")
            supersedes = descriptor.get("supersedes")
            if supersedes is not None and not isinstance(supersedes, dict):
                _issue(issues, f"{loc}.supersedes", "must be null or a pinned layer revision")
            _check_ext(issues, descriptor.get("ext", {}), f"{loc}.ext")

    # Validate embedded layer bodies after every descriptor key is known.
    for key, descriptor in descriptor_by_key.items():
        resource = resource_by_id.get(descriptor.get("content_resource_id"))
        if not isinstance(resource, dict):
            continue
        embedded = [
            location.get("member") for location in resource.get("locations", [])
            if isinstance(location, dict) and location.get("type") == "embedded"
        ]
        if not embedded:
            _issue(
                issues, f"manifest.layers[{key}]", "external-only layer content cannot be semantically validated",
                level="warning",
            )
            continue
        if members is None or embedded[0] not in members:
            continue
        try:
            layer = _strict_json(members[embedded[0]], embedded[0])
        except Lib4Error as exc:
            _issue(issues, f"archive:{embedded[0]}", str(exc))
            continue
        for item in validate_layer(layer, manifest, descriptor):
            issues.append(Issue(item.level, f"{embedded[0]}:{item.location}", item.message))

    releases = manifest.get("releases")
    release_by_id: dict[str, dict[str, Any]] = {}
    if not isinstance(releases, list):
        _issue(issues, "manifest.releases", "must be an array")
    else:
        for index, release in enumerate(releases):
            loc = f"manifest.releases[{index}]"
            fields = (
                "id", "revision", "state", "created_at", "created_by", "previous_release_id",
                "layer_pins", "resource_ids", "catalog_revision", "citation", "policy", "ext",
            )
            if not _check_keys(issues, release, loc, required=fields, allowed=fields):
                continue
            release_id = release.get("id")
            if not _is_id(release_id) or release_id in release_by_id:
                _issue(issues, f"{loc}.id", "must be a unique portable identifier")
            else:
                release_by_id[release_id] = release
            if not _is_revision(release.get("revision")):
                _issue(issues, f"{loc}.revision", "must be a portable revision")
            if release.get("state") not in RELEASE_STATES:
                _issue(issues, f"{loc}.state", f"must be one of {sorted(RELEASE_STATES)}")
            if not _is_timestamp(release.get("created_at")):
                _issue(issues, f"{loc}.created_at", "must be an ISO 8601 UTC timestamp")
            _check_actor(issues, release.get("created_by"), f"{loc}.created_by")
            layer_pins = release.get("layer_pins")
            if not isinstance(layer_pins, list):
                _issue(issues, f"{loc}.layer_pins", "must be an array")
            else:
                for pin_index, pin in enumerate(layer_pins):
                    pin_loc = f"{loc}.layer_pins[{pin_index}]"
                    if not isinstance(pin, dict) or set(pin) != {"layer_id", "revision"}:
                        _issue(issues, pin_loc, "must contain exactly layer_id and revision")
                        continue
                    descriptor = descriptor_by_key.get((pin.get("layer_id"), pin.get("revision")))
                    if descriptor is None:
                        _issue(issues, pin_loc, "references a missing layer revision")
                    elif release.get("state") == "published":
                        if descriptor.get("lifecycle", {}).get("state") != "complete":
                            _issue(issues, pin_loc, "published releases may pin only complete layers")
                        if descriptor.get("editorial_status") not in {"approved", "frozen"}:
                            _issue(issues, pin_loc, "published releases may pin only approved or frozen layers")
            resource_ids = release.get("resource_ids")
            if not isinstance(resource_ids, list):
                _issue(issues, f"{loc}.resource_ids", "must be an array")
            else:
                for resource_id in resource_ids:
                    if resource_id not in resource_by_id:
                        _issue(issues, f"{loc}.resource_ids", f"references missing resource {resource_id!r}")
            if release.get("catalog_revision") != catalog_revision:
                _issue(issues, f"{loc}.catalog_revision", "must pin the inline catalog record revision")
            policy = release.get("policy")
            if not isinstance(policy, dict) or set(policy) != {"approved_layers_only", "include_machine_drafts", "ext"}:
                _issue(issues, f"{loc}.policy", "must declare approved_layers_only, include_machine_drafts, and ext")
            elif release.get("state") == "published" and (
                policy.get("approved_layers_only") is not True or policy.get("include_machine_drafts") is not False
            ):
                _issue(issues, f"{loc}.policy", "published releases require approved-only content and exclude machine drafts")
            _check_ext(issues, release.get("ext", {}), f"{loc}.ext")
        for release_id, release in release_by_id.items():
            previous = release.get("previous_release_id")
            if previous is not None and previous not in release_by_id:
                _issue(issues, f"manifest.releases[{release_id}].previous_release_id", "references a missing release")

    catalog_resource_id = manifest.get("catalog_resource_id")
    if catalog_resource_id is not None:
        resource = resource_by_id.get(catalog_resource_id)
        if resource is None:
            _issue(issues, "manifest.catalog_resource_id", "references a missing resource")
        elif resource.get("role") != "catalog":
            _issue(issues, "manifest.catalog_resource_id", "must reference a catalog resource")

    retrieval = manifest.get("retrieval")
    if retrieval is not None:
        retrieval_fields = ("document_id", "chunk_sets", "citation_uri_template", "ext")
        if _check_keys(issues, retrieval, "manifest.retrieval", required=retrieval_fields, allowed=retrieval_fields):
            if not _is_id(retrieval.get("document_id")):
                _issue(issues, "manifest.retrieval.document_id", "must be a portable identifier")
            chunk_sets = retrieval.get("chunk_sets")
            if not isinstance(chunk_sets, list):
                _issue(issues, "manifest.retrieval.chunk_sets", "must be an array")
            else:
                chunk_ids: set[str] = set()
                for index, chunk_set in enumerate(chunk_sets):
                    loc = f"manifest.retrieval.chunk_sets[{index}]"
                    fields = (
                        "id", "layer_id", "revision", "record_schema", "records_resource_id",
                        "content_sha256", "embedding_resource_id", "index_hint", "ext",
                    )
                    if not _check_keys(issues, chunk_set, loc, required=fields, allowed=fields):
                        continue
                    chunk_id = chunk_set.get("id")
                    if not _is_id(chunk_id) or chunk_id in chunk_ids:
                        _issue(issues, f"{loc}.id", "must be a unique portable identifier")
                    else:
                        chunk_ids.add(chunk_id)
                    descriptor = descriptor_by_key.get((chunk_set.get("layer_id"), chunk_set.get("revision")))
                    if descriptor is None or descriptor.get("kind") != "retrieval":
                        _issue(issues, loc, "must pin a retrieval layer revision")
                    records_id = chunk_set.get("records_resource_id")
                    records_resource = resource_by_id.get(records_id)
                    if records_resource is None or records_resource.get("role") != "retrieval-records":
                        _issue(issues, f"{loc}.records_resource_id", "must reference a retrieval-records resource")
                    if not isinstance(chunk_set.get("content_sha256"), str) or not SHA256_RE.fullmatch(chunk_set["content_sha256"]):
                        _issue(issues, f"{loc}.content_sha256", "must be a lowercase SHA-256 digest")
                    elif isinstance(records_resource, dict):
                        integrity = records_resource.get("integrity")
                        if (
                            isinstance(integrity, dict)
                            and integrity.get("state") == "verified"
                            and integrity.get("digest") != chunk_set["content_sha256"]
                        ):
                            _issue(issues, f"{loc}.content_sha256", "does not match the retrieval-records resource digest")
                    embedding_id = chunk_set.get("embedding_resource_id")
                    if embedding_id is not None:
                        resource = resource_by_id.get(embedding_id)
                        if resource is None or resource.get("role") != "embedding-index":
                            _issue(issues, f"{loc}.embedding_resource_id", "must reference an embedding-index resource")
                    _check_ext(issues, chunk_set.get("ext", {}), f"{loc}.ext")
            template = retrieval.get("citation_uri_template")
            if not isinstance(template, str) or "{chunk_id}" not in template:
                _issue(issues, "manifest.retrieval.citation_uri_template", "must contain {chunk_id}")
            _check_ext(issues, retrieval.get("ext", {}), "manifest.retrieval.ext")

    capabilities = manifest.get("capabilities")
    if not isinstance(capabilities, list) or any(not _is_id(value) for value in capabilities):
        _issue(issues, "manifest.capabilities", "must be an array of portable identifiers")
    elif len(set(capabilities)) != len(capabilities):
        _issue(issues, "manifest.capabilities", "must not contain duplicates")
    elif any(value not in CORE_CAPABILITIES | registry_sets["capabilities"] for value in capabilities):
        _issue(issues, "manifest.capabilities", "each value must be core or declared")
    _check_ext(issues, manifest.get("ext", {}), "manifest.ext")

    if members is not None:
        actual_data_members = set(members) - _GENERATED_MEMBERS
        expected = set(embedded_by_member)
        for member in sorted(actual_data_members - expected):
            _issue(issues, f"archive:{member}", "embedded data member is not declared by a resource location")
        for member in sorted(expected - actual_data_members):
            _issue(issues, f"manifest.resources[{member}]", "declared embedded member is absent")
    return issues


def _parse_checksums(payload: bytes) -> dict[str, str]:
    try:
        text = payload.decode("ascii")
    except UnicodeDecodeError as exc:
        raise Lib4Error("checksums.sha256 must be ASCII") from exc
    checksums: dict[str, str] = {}
    pattern = re.compile(r"^([0-9a-f]{64})  (.+)$")
    for line_number, line in enumerate(text.splitlines(), 1):
        match = pattern.fullmatch(line)
        if not match:
            raise Lib4Error(f"checksums.sha256 line {line_number} is malformed")
        digest, member = match.groups()
        if not _safe_member(member) or member == "checksums.sha256":
            raise Lib4Error(f"checksums.sha256 line {line_number} names an invalid member")
        if member in checksums:
            raise Lib4Error(f"checksums.sha256 repeats {member}")
        checksums[member] = digest
    return checksums


def _read_zip(
    path_or_bytes: str | os.PathLike[str] | bytes | bytearray | BinaryIO,
) -> tuple[dict[str, bytes], list[Issue]]:
    issues: list[Issue] = []
    close_stream = False
    if isinstance(path_or_bytes, (bytes, bytearray)):
        stream: BinaryIO = io.BytesIO(bytes(path_or_bytes))
    elif hasattr(path_or_bytes, "read"):
        stream = path_or_bytes  # type: ignore[assignment]
    else:
        path = Path(path_or_bytes)
        try:
            size = path.stat().st_size
        except OSError as exc:
            return {}, [Issue("error", "archive", f"cannot stat archive: {exc}")]
        if size > MAX_ARCHIVE_BYTES:
            return {}, [Issue("error", "archive", f"exceeds {MAX_ARCHIVE_BYTES} bytes")]
        stream = path.open("rb")
        close_stream = True
    try:
        try:
            archive = zipfile.ZipFile(stream, "r")
        except (OSError, zipfile.BadZipFile) as exc:
            return {}, [Issue("error", "archive", f"not a readable ZIP archive: {exc}")]
        with archive:
            infos = archive.infolist()
            if len(infos) > MAX_MEMBERS:
                return {}, [Issue("error", "archive", f"contains more than {MAX_MEMBERS} members")]
            names: set[str] = set()
            inflated = 0
            for info in infos:
                loc = f"archive:{info.filename}"
                if not _safe_member(info.filename) or not _allowed_member(info.filename):
                    _issue(issues, loc, "unsafe or unrecognized member path")
                if info.filename in names:
                    _issue(issues, loc, "duplicate member")
                names.add(info.filename)
                if info.is_dir() or _is_symlink(info):
                    _issue(issues, loc, "directories and symbolic links are forbidden")
                if info.flag_bits & 0x1:
                    _issue(issues, loc, "encrypted members are forbidden")
                if info.file_size > MAX_MEMBER_BYTES:
                    _issue(issues, loc, f"member exceeds {MAX_MEMBER_BYTES} bytes")
                inflated += info.file_size
                if inflated > MAX_INFLATED_BYTES:
                    _issue(issues, "archive", f"inflated data exceeds {MAX_INFLATED_BYTES} bytes")
                    break
                if info.compress_size and info.file_size / info.compress_size > MAX_COMPRESSION_RATIO:
                    _issue(issues, loc, f"compression ratio exceeds {MAX_COMPRESSION_RATIO}:1")
            if issues:
                return {}, issues
            members: dict[str, bytes] = {}
            for info in infos:
                with archive.open(info, "r") as handle:
                    payload = handle.read(info.file_size + 1)
                if len(payload) != info.file_size:
                    _issue(issues, f"archive:{info.filename}", "decompressed size does not match ZIP metadata")
                    return {}, issues
                members[info.filename] = payload
            return members, issues
    finally:
        if close_stream:
            stream.close()


def validate_archive(
    path_or_bytes: str | os.PathLike[str] | bytes | bytearray | BinaryIO,
) -> list[Issue]:
    """Validate ZIP safety, integrity, schemas, graph references, and layers."""

    members, issues = _read_zip(path_or_bytes)
    if issues:
        return issues
    for member in sorted(_GENERATED_MEMBERS - set(members)):
        _issue(issues, "archive", f"missing required member {member}")
    if issues:
        return issues
    try:
        checksums = _parse_checksums(members["checksums.sha256"])
    except Lib4Error as exc:
        return [Issue("error", "checksums.sha256", str(exc))]
    expected_members = set(members) - {"checksums.sha256"}
    for member in sorted(expected_members - set(checksums)):
        _issue(issues, f"archive:{member}", "is missing from checksums.sha256")
    for member in sorted(set(checksums) - expected_members):
        _issue(issues, f"checksums.sha256:{member}", "names a missing member")
    for member in sorted(expected_members & set(checksums)):
        if _sha256(members[member]) != checksums[member]:
            _issue(issues, f"archive:{member}", "checksum mismatch")
    try:
        manifest = _strict_json(members["manifest.json"], "manifest.json")
    except Lib4Error as exc:
        _issue(issues, "manifest.json", str(exc))
        return issues
    if not isinstance(manifest, dict):
        _issue(issues, "manifest.json", "root must be an object")
        return issues
    issues.extend(validate_manifest(manifest, members))
    for name in SCHEMA_FILES:
        member = f"schemas/{name}"
        try:
            schema = _strict_json(members[member], member)
            if not isinstance(schema, dict) or schema.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
                _issue(issues, f"archive:{member}", "must be a JSON Schema draft 2020-12 object")
        except Lib4Error as exc:
            _issue(issues, f"archive:{member}", str(exc))
    return issues


def read_archive(
    path_or_bytes: str | os.PathLike[str] | bytes | bytearray | BinaryIO,
) -> Lib4Document:
    """Return a validated package. Legacy formats receive a migration hint."""

    issues = validate_archive(path_or_bytes)
    errors = [issue for issue in issues if issue.level == "error"]
    if errors:
        detected = detect_format(path_or_bytes)
        hint = ""
        if detected == "whled/0.1":
            hint = "; legacy .whled requires the documented explicit migration to lib/4"
        elif detected and detected.startswith("lib/") and detected != FORMAT:
            hint = f"; {detected} requires the Library Tool major-version importer"
        summary = "; ".join(f"{item.location}: {item.message}" for item in errors[:8])
        raise Lib4Error(summary + hint)
    members, read_issues = _read_zip(path_or_bytes)
    if read_issues:
        raise Lib4Error(read_issues[0].message)
    manifest = _strict_json(members["manifest.json"], "manifest.json")
    return Lib4Document(manifest=manifest, members=members)


def detect_format(
    path_or_bytes: str | os.PathLike[str] | bytes | bytearray | BinaryIO,
) -> str | None:
    """Read only the format marker of a ZIP-based package when safely possible."""

    close_stream = False
    if isinstance(path_or_bytes, (bytes, bytearray)):
        stream: BinaryIO = io.BytesIO(bytes(path_or_bytes))
    elif hasattr(path_or_bytes, "read"):
        stream = path_or_bytes  # type: ignore[assignment]
        try:
            stream.seek(0)
        except (AttributeError, OSError):
            return None
    else:
        try:
            stream = Path(path_or_bytes).open("rb")
        except OSError:
            return None
        close_stream = True
    try:
        try:
            with zipfile.ZipFile(stream, "r") as archive:
                matches = [info for info in archive.infolist() if info.filename == "manifest.json"]
                if len(matches) != 1 or matches[0].file_size > MAX_JSON_BYTES or matches[0].flag_bits & 0x1:
                    return None
                payload = archive.read(matches[0])
        except (OSError, KeyError, zipfile.BadZipFile, RuntimeError):
            return None
        try:
            manifest = _strict_json(payload, "manifest.json")
        except Lib4Error:
            return None
        value = manifest.get("format") if isinstance(manifest, dict) else None
        return value if isinstance(value, str) else None
    finally:
        if close_stream:
            stream.close()


def _schema_payload(filename: str) -> bytes:
    path = Path(__file__).resolve().parents[2] / "schemas" / filename
    try:
        return _canonical_json(_strict_json(path.read_bytes(), str(path)))
    except OSError as exc:
        raise Lib4Error(f"cannot load bundled schema {path}: {exc}") from exc


def render_instructions(manifest: Mapping[str, Any]) -> bytes:
    catalog = manifest.get("catalog", {})
    package_id = str(manifest.get("package_id", ""))
    record = catalog.get("record", {}) if isinstance(catalog, dict) else {}
    text = f"""# Instructions for this `.lib4` Living Edition

Package `{package_id}` revision `{manifest.get('package_revision', '')}` follows
`{FORMAT}` profile `{PROFILE}/{PROFILE_VERSION}`. Catalog record
`{record.get('id', '')}` is at revision `{record.get('revision', '')}`.

## Read order

1. Validate every checksum before using any member.
2. Read `manifest.json`, then the schemas under `schemas/`.
3. Resolve structure, canvas, resource, and layer references by stable ID and
   exact revision. Never use array position or filename as scholarly identity.
4. Select only a declared resource location. Embedded locations are offline
   evidence; ordered HTTP, S3, and IIIF locations are stable references. Never
   add credentials, signed URLs, request headers, local paths, or secrets.
5. Treat `releases[]` as immutable projections. "Latest" is not a citation.

## Safe generation and augmentation

* Preserve source evidence. A changed page image, media file, structure, layer,
  or catalog record receives a new revision; retain the old revision when it is
  cited. Do not overwrite human-approved content.
* Work may be `planned`, `running`, `partial`, or `failed`. Record honest
  coverage, omissions, processing errors, service/model identity, version,
  parameters, inputs, and run ID. A later engine adds a new revision and pins
  the earlier output as provenance; it does not pretend the initial pass was
  complete.
* Keep regions, diplomatic transcription, normalized text, translations,
  entity mentions, commentary, knowledge, retrieval projections, and guided
  reprocessing in separate layers. Only a named human may approve or reject a
  scholarly revision. Published releases pin complete approved/frozen layers.
* Core geometry is normalized 0..1 and pins a canvas revision. Time ranges use
  integer milliseconds. Text quotes pin layer/revision/item plus offsets,
  exact text, context, and staleness state. Custom selectors require a core
  fallback and a namespaced registry declaration.
* Material structure is not assumed to be pages or a codex. Use the generic
  hierarchy for folios/gatherings, volumes/issues/articles, chapters/figures,
  maps, entries, tracks, segments, or declared extension types. A non-visual
  edition may have no canvases.
* The inline catalog is the self-contained discovery record. Preserve
  assertions, uncertainty, provenance, rights, and access state; use explicit
  unknowns rather than guesses. A richer catalog resource may supplement but
  never replace it.
* Retrieval chunks are derived, revisioned records. Each one retains exact
  source anchors, entity references, content digest, rights/access filters, and
  a stable citation URI. Embeddings and indexes are replaceable derivatives;
  record model/version and never make a vector the sole route to evidence.
* Preserve unknown declared extensions byte-for-byte. New kinds, roles,
  selectors, relations, structure/material types, and capabilities must use a
  namespaced ID and registry entry. Do not execute code from an archive.

## Seal

Every archive member except `checksums.sha256` is checksummed. Every embedded
data member is owned by exactly one `resources[].locations[]` entry. Rebuild
with `tools/living_edition/build_lib4.py`; do not patch ZIP members or checksums
in place. The reference validator rejects traversal paths, links, encryption,
credential-shaped extension data, unstable remote URLs, undeclared files,
duplicate JSON keys, non-finite numbers, broken revision pins, invalid release
contents, and archive bombs.
"""
    return text.encode("utf-8")


def _coerce_payload(value: bytes | bytearray | str | os.PathLike[str]) -> bytes:
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    return Path(value).read_bytes()


def _zip_info(name: str, *, compressed: bool) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, _FIXED_ZIP_TIME)
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    info.compress_type = zipfile.ZIP_DEFLATED if compressed else zipfile.ZIP_STORED
    info.flag_bits = 0x800
    return info


def _embedded_inventory(manifest: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for resource in manifest.get("resources", []):
        if not isinstance(resource, dict):
            continue
        for location in resource.get("locations", []):
            if isinstance(location, dict) and location.get("type") == "embedded" and isinstance(location.get("member"), str):
                member = location["member"]
                if member in result:
                    raise Lib4Error(f"embedded member {member!r} is declared by multiple resources")
                result[member] = resource
    return result


def seal_archive(
    manifest: Mapping[str, Any],
    payloads: Mapping[str, bytes | bytearray | str | os.PathLike[str]],
    destination: str | os.PathLike[str] | BinaryIO,
) -> dict[str, Any]:
    """Seal a deterministic package from an explicit embedded-member inventory."""

    final_manifest = copy.deepcopy(dict(manifest))
    inventory = _embedded_inventory(final_manifest)
    if set(inventory) != set(payloads):
        missing = sorted(set(inventory) - set(payloads))
        extra = sorted(set(payloads) - set(inventory))
        raise Lib4Error(f"payload inventory mismatch; missing={missing}, undeclared={extra}")
    embedded_payloads: dict[str, bytes] = {}
    for member, resource in inventory.items():
        if not _safe_member(member) or not _allowed_member(member) or member in _GENERATED_MEMBERS:
            raise Lib4Error(f"unsafe or reserved data member {member!r}")
        try:
            payload = _coerce_payload(payloads[member])
        except OSError as exc:
            raise Lib4Error(f"cannot read payload for {member}: {exc}") from exc
        if len(payload) > MAX_MEMBER_BYTES:
            raise Lib4Error(f"{member} exceeds {MAX_MEMBER_BYTES} bytes")
        resource["byte_length"] = len(payload)
        resource["integrity"] = {
            "state": "verified", "algorithm": "sha256", "digest": _sha256(payload), "reason": None,
        }
        embedded_payloads[member] = payload
    final_manifest["resources"] = sorted(
        final_manifest.get("resources", []), key=lambda item: item.get("id", "") if isinstance(item, dict) else "",
    )

    generated: dict[str, bytes] = {
        "manifest.json": _canonical_json(final_manifest),
        "INSTRUCTIONS.md": render_instructions(final_manifest),
        **{f"schemas/{name}": _schema_payload(name) for name in SCHEMA_FILES},
        **embedded_payloads,
    }
    validation_members = {**generated, "checksums.sha256": b""}
    issues = validate_manifest(final_manifest, validation_members)
    errors = [issue for issue in issues if issue.level == "error"]
    if errors:
        summary = "; ".join(f"{item.location}: {item.message}" for item in errors[:16])
        raise Lib4Error(f"refusing to seal invalid package: {summary}")
    generated["checksums.sha256"] = "".join(
        f"{_sha256(payload)}  {member}\n" for member, payload in sorted(generated.items())
    ).encode("ascii")

    close_target = False
    temporary_path: Path | None = None
    final_path: Path | None = None
    if hasattr(destination, "write"):
        target = destination  # type: ignore[assignment]
    else:
        final_path = Path(destination)
        final_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{final_path.name}.", suffix=".tmp", dir=final_path.parent,
        )
        os.close(fd)
        temporary_path = Path(temp_name)
        target = temporary_path.open("w+b")
        close_target = True
    try:
        with zipfile.ZipFile(target, "w", allowZip64=True) as archive:
            for member in sorted(generated):
                payload = generated[member]
                resource = inventory.get(member)
                media_type = resource.get("media_type") if resource else (
                    "application/json" if member.endswith(".json") else "text/plain"
                )
                compressed = media_type.startswith("text/") or media_type in {
                    "application/json", "application/xml", "application/ld+json",
                }
                archive.writestr(_zip_info(member, compressed=compressed), payload, compresslevel=9)
        if close_target:
            target.close()
            close_target = False
        if temporary_path is not None and final_path is not None:
            os.replace(temporary_path, final_path)
            temporary_path = None
    except Exception:
        if close_target:
            target.close()
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except OSError:
                pass
        raise
    return final_manifest


def load_manifest(path: str | os.PathLike[str]) -> dict[str, Any]:
    value = _strict_json(Path(path).read_bytes(), str(path))
    if not isinstance(value, dict):
        raise Lib4Error("manifest root must be an object")
    return value


__all__ = [
    "CATALOG_SCHEMA_ID",
    "FORMAT",
    "Issue",
    "LAYER_SCHEMA_ID",
    "Lib4Document",
    "Lib4Error",
    "MANIFEST_SCHEMA_ID",
    "MEDIA_TYPE",
    "PROFILE",
    "PROFILE_VERSION",
    "detect_format",
    "load_manifest",
    "read_archive",
    "render_instructions",
    "seal_archive",
    "validate_archive",
    "validate_layer",
    "validate_manifest",
]

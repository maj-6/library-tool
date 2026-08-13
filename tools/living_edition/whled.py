#!/usr/bin/env python3
"""Reader, validator, and deterministic sealer for ``.whled`` packages.

``whled/0.1`` is a living-edition profile, not a revision of Library Tool's
``.lib`` interchange format.  It keeps immutable page evidence beside
revision-pinned editorial layers while leaving the mutable authority database
outside the archive.  Only a JSON authority *snapshot* may be embedded.

The implementation deliberately uses only the Python standard library.  JSON
Schema files travel in every package for generic clients; the checks below are
the normative semantic and archive-safety checks used by the prototype.
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


FORMAT = "whled/0.1"
MANIFEST_SCHEMA_ID = "https://worldherblibrary.org/schemas/whled-manifest-0.1.json"
LAYER_SCHEMA_ID = "https://worldherblibrary.org/schemas/whled-layer-0.1.json"

MAX_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024
MAX_INFLATED_BYTES = 4 * 1024 * 1024 * 1024
MAX_MEMBER_BYTES = 256 * 1024 * 1024
MAX_JSON_BYTES = 32 * 1024 * 1024
MAX_MEMBERS = 20_000
MAX_COMPRESSION_RATIO = 500
MAX_NESTING = 128

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
    "region",
    "transcription",
    "translation",
    "entity",
    "knowledge",
    "commentary",
    "notes",
    "reprocessing",
})
CORE_SELECTOR_TYPES = frozenset({"box", "polygon"})
CORE_RESOURCE_ROLES = frozenset({"canvas-image", "layer", "authority-snapshot", "asset"})
CORE_REGION_RELATIONS = frozenset({
    "marginalia-of", "continues-at", "caption-of", "interlinear-gloss-of",
})
CORE_CAPABILITIES = frozenset({
    "polygon-regions", "parallel-layers", "entity-snapshot",
    "guided-reprocessing", "named-reading-flows", "region-relations",
})
LAYER_STATUSES = frozenset({
    "machine-draft",
    "human-draft",
    "under-review",
    "approved",
    "frozen",
    "superseded",
})
EDITION_STATUSES = frozenset({"working", "review", "published", "frozen"})
REVIEW_DECISIONS = frozenset({"approve", "reject", "request-changes", "abstain"})
ACTOR_TYPES = frozenset({"human", "software", "model", "import"})
CONFIDENCE_TERMS = frozenset({"certain", "likely", "possible", "disputed", "unresolved"})

_REQUIRED_MEMBERS = frozenset({
    "manifest.json",
    "INSTRUCTIONS.md",
    "checksums.sha256",
    "schemas/whled-manifest.schema.json",
    "schemas/whled-layer.schema.json",
})
_DATABASE_SUFFIXES = (".sqlite", ".sqlite3", ".db", ".db3")
_FIXED_ZIP_TIME = (1980, 1, 1, 0, 0, 0)


class WhledError(Exception):
    """Raised when a package cannot be read or sealed safely."""


@dataclass(frozen=True, slots=True)
class Issue:
    level: str
    location: str
    message: str


@dataclass(slots=True)
class WhledDocument:
    manifest: dict[str, Any]
    members: dict[str, bytes]


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
        raise WhledError(f"{location}: JSON exceeds {MAX_JSON_BYTES} bytes")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WhledError(f"{location}: JSON is not UTF-8") from exc

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
        value = json.loads(
            text,
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except (json.JSONDecodeError, ValueError) as exc:
        raise WhledError(f"{location}: invalid JSON: {exc}") from exc
    if _nesting_exceeds(value):
        raise WhledError(f"{location}: JSON nesting exceeds {MAX_NESTING}")
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


def _is_public_uri(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(("https://", "http://", "urn:"))


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
    if any(part in {"", ".", ".."} or not PORTABLE_SEGMENT_RE.fullmatch(part) for part in parts):
        return False
    return True


def _is_database_member(name: str) -> bool:
    lowered = name.casefold()
    return lowered.endswith(_DATABASE_SUFFIXES) or "plant-authority.sqlite" in lowered


def _allowed_member(name: str) -> bool:
    if name in _REQUIRED_MEMBERS:
        return True
    if _is_database_member(name):
        return False
    return name.startswith(("canvases/", "layers/", "authority-snapshots/", "assets/"))


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
        _issue(issues, location, "must contain JSON values only")
        return
    if size > 64 * 1024:
        _issue(issues, location, "exceeds the 64 KiB extension limit")
    private_keys = {"path", "file_path", "local_path", "api_key", "token", "secret", "password"}
    stack: list[tuple[Any, str]] = [(value, location)]
    while stack:
        current, loc = stack.pop()
        if isinstance(current, dict):
            for key, item in current.items():
                if str(key).casefold() in private_keys:
                    _issue(issues, f"{loc}.{key}", "private locators and credentials are forbidden")
                stack.append((item, f"{loc}.{key}"))
        elif isinstance(current, list):
            stack.extend((item, f"{loc}[{index}]") for index, item in enumerate(current))
        elif isinstance(current, float) and not math.isfinite(current):
            _issue(issues, loc, "non-finite numbers are forbidden")


def _check_actor(issues: list[Issue], actor: Any, location: str) -> None:
    if not _check_keys(
        issues,
        actor,
        location,
        required=("type", "id", "label"),
        allowed=("type", "id", "label", "version", "uri", "ext"),
    ):
        return
    if actor.get("type") not in ACTOR_TYPES:
        _issue(issues, f"{location}.type", f"must be one of {sorted(ACTOR_TYPES)}")
    if not _is_id(actor.get("id")):
        _issue(issues, f"{location}.id", "must be a portable stable identifier")
    if not isinstance(actor.get("label"), str) or not actor["label"].strip():
        _issue(issues, f"{location}.label", "must be non-empty text")
    if "uri" in actor and not _is_public_uri(actor["uri"]):
        _issue(issues, f"{location}.uri", "must be an http(s) or urn URI")
    _check_ext(issues, actor.get("ext", {}), f"{location}.ext")


def _check_selector(
    issues: list[Issue],
    selector: Any,
    location: str,
    *,
    canvas_revision: str | None,
    custom_types: set[str] | None = None,
    allow_custom: bool = True,
) -> None:
    if not isinstance(selector, dict):
        _issue(issues, location, "must be an object")
        return
    selector_type = selector.get("type")
    common = {"type", "coordinate_space", "canvas_revision"}
    is_custom = allow_custom and selector_type in (custom_types or set())
    allowed = common | (
        {"x", "y", "width", "height"} if selector_type == "box"
        else {"points"} if selector_type == "polygon"
        else {"data", "fallback"} if is_custom
        else set()
    )
    _check_keys(
        issues,
        selector,
        location,
        required=("type", "coordinate_space", "canvas_revision"),
        allowed=allowed,
    )
    if selector.get("coordinate_space") != "canvas-normalized":
        _issue(issues, f"{location}.coordinate_space", "must be canvas-normalized")
    if not _is_revision(selector.get("canvas_revision")):
        _issue(issues, f"{location}.canvas_revision", "must be a portable revision")
    elif canvas_revision and selector["canvas_revision"] != canvas_revision:
        _issue(issues, f"{location}.canvas_revision", "does not pin the referenced canvas revision")

    if selector_type == "box":
        for field in ("x", "y", "width", "height"):
            if field not in selector:
                _issue(issues, f"{location}.{field}", "is required for a box")
            elif not isinstance(selector[field], (int, float)) or isinstance(selector[field], bool):
                _issue(issues, f"{location}.{field}", "must be a number")
        if all(isinstance(selector.get(field), (int, float)) for field in ("x", "y", "width", "height")):
            x, y, width, height = (float(selector[field]) for field in ("x", "y", "width", "height"))
            if x < 0 or y < 0 or width <= 0 or height <= 0 or x + width > 1.0000001 or y + height > 1.0000001:
                _issue(issues, location, "box must have positive size and remain within normalized canvas bounds")
    elif selector_type == "polygon":
        points = selector.get("points")
        if not isinstance(points, list) or not 3 <= len(points) <= 256:
            _issue(issues, f"{location}.points", "must contain 3 to 256 points")
            return
        parsed: list[tuple[float, float]] = []
        for index, point in enumerate(points):
            ploc = f"{location}.points[{index}]"
            if not isinstance(point, dict) or set(point) != {"x", "y"}:
                _issue(issues, ploc, "must contain exactly numeric x and y")
                continue
            if any(not isinstance(point.get(field), (int, float)) or isinstance(point.get(field), bool) for field in ("x", "y")):
                _issue(issues, ploc, "coordinates must be numbers")
                continue
            x, y = float(point["x"]), float(point["y"])
            if not 0 <= x <= 1 or not 0 <= y <= 1:
                _issue(issues, ploc, "coordinates must be in the range 0..1")
            parsed.append((x, y))
        if len(set(parsed)) < 3:
            _issue(issues, f"{location}.points", "must contain at least three distinct points")
        elif len(parsed) == len(points):
            area = abs(sum(
                parsed[i][0] * parsed[(i + 1) % len(parsed)][1]
                - parsed[(i + 1) % len(parsed)][0] * parsed[i][1]
                for i in range(len(parsed))
            )) / 2
            if area <= 1e-10:
                _issue(issues, f"{location}.points", "polygon must have non-zero area")
    elif is_custom:
        if not _is_namespaced_id(selector_type):
            _issue(issues, f"{location}.type", "custom selector types must be namespaced")
        if not isinstance(selector.get("data"), dict):
            _issue(issues, f"{location}.data", "custom selector data must be an object")
        fallback = selector.get("fallback")
        if not isinstance(fallback, dict) or fallback.get("type") not in CORE_SELECTOR_TYPES:
            _issue(issues, f"{location}.fallback", "custom selectors require a core box/polygon fallback")
        else:
            _check_selector(
                issues,
                fallback,
                f"{location}.fallback",
                canvas_revision=canvas_revision,
                custom_types=set(),
                allow_custom=False,
            )
    else:
        _issue(issues, f"{location}.type", "must be box, polygon, or a declared custom selector type")


def _registry_sets(issues: list[Issue], registries: Any, location: str = "manifest.registries") -> dict[str, set[str]]:
    names = (
        "layer_kinds", "selector_types", "resource_roles",
        "region_relation_predicates", "capabilities",
    )
    result = {name: set() for name in names}
    if not _check_keys(issues, registries, location, required=names, allowed=names):
        return result
    core_sets = {
        "layer_kinds": CORE_LAYER_KINDS,
        "selector_types": CORE_SELECTOR_TYPES,
        "resource_roles": CORE_RESOURCE_ROLES,
        "region_relation_predicates": CORE_REGION_RELATIONS,
        "capabilities": CORE_CAPABILITIES,
    }
    for name in names:
        entries = registries.get(name)
        if not isinstance(entries, list):
            _issue(issues, f"{location}.{name}", "must be an array")
            continue
        for index, entry in enumerate(entries):
            loc = f"{location}.{name}[{index}]"
            if not _check_keys(
                issues,
                entry,
                loc,
                required=("id", "label", "description", "schema_uri", "renderer_hint", "ext"),
                allowed=("id", "label", "description", "schema_uri", "renderer_hint", "ext"),
            ):
                continue
            entry_id = entry.get("id")
            if not _is_namespaced_id(entry_id):
                _issue(issues, f"{loc}.id", "extension identifiers must be namespaced with ':' or x-")
            elif entry_id in core_sets[name]:
                _issue(issues, f"{loc}.id", "may not redefine a core identifier")
            elif entry_id in result[name]:
                _issue(issues, f"{loc}.id", "duplicates another registry entry")
            else:
                result[name].add(entry_id)
            if not isinstance(entry.get("label"), str) or not entry["label"].strip():
                _issue(issues, f"{loc}.label", "must be non-empty text")
            if entry.get("schema_uri") is not None and not _is_public_uri(entry.get("schema_uri")):
                _issue(issues, f"{loc}.schema_uri", "must be null or a public schema URI")
            if not _is_id(entry.get("renderer_hint")):
                _issue(issues, f"{loc}.renderer_hint", "must be a portable renderer hint")
            _check_ext(issues, entry.get("ext", {}), f"{loc}.ext")
    return result


def _check_ref(issues: list[Issue], value: Any, location: str, *, region: bool = False) -> None:
    required = ("layer_id", "revision", "region_id") if region else ("layer_id", "revision", "item_id")
    if not _check_keys(issues, value, location, required=required, allowed=required):
        return
    if not _is_id(value.get("layer_id")):
        _issue(issues, f"{location}.layer_id", "must be a portable identifier")
    if not _is_revision(value.get("revision")):
        _issue(issues, f"{location}.revision", "must be a portable revision")
    tail = "region_id" if region else "item_id"
    if not _is_id(value.get(tail)):
        _issue(issues, f"{location}.{tail}", "must be a portable identifier")


def _check_target_uri(issues: list[Issue], value: Any, location: str) -> None:
    allowed = (
        "whl://book/",
        "whl-entity://name/",
        "whl-entity://concept/",
        "whl-entity://referent/",
        "whl-entity://assertion/",
        "whl-entity://evidence/",
        "whl-entity://review/",
    )
    if not isinstance(value, str) or not value.startswith(allowed) or "\\" in value or " " in value:
        _issue(issues, location, "must be a stable whl:// or whl-entity:// target URI")


def validate_manifest(manifest: Any, members: Mapping[str, bytes] | None = None) -> list[Issue]:
    """Validate manifest structure and, when supplied, resource bytes."""

    issues: list[Issue] = []
    required = (
        "$schema", "format", "package_id", "created_at", "generator", "edition",
        "catalog", "source", "canvases", "region_types", "layers", "resources",
        "authority_snapshots", "external_authorities", "registries", "capabilities", "ext",
    )
    allowed = required
    if not _check_keys(issues, manifest, "manifest", required=required, allowed=allowed):
        return issues
    if manifest.get("$schema") != MANIFEST_SCHEMA_ID:
        _issue(issues, "manifest.$schema", f"must be {MANIFEST_SCHEMA_ID}")
    if manifest.get("format") != FORMAT:
        _issue(issues, "manifest.format", f"must be {FORMAT}")
    if not _is_id(manifest.get("package_id")):
        _issue(issues, "manifest.package_id", "must be a portable stable identifier")
    if not _is_timestamp(manifest.get("created_at")):
        _issue(issues, "manifest.created_at", "must be an ISO 8601 UTC timestamp")
    if not isinstance(manifest.get("generator"), str) or not manifest["generator"].strip():
        _issue(issues, "manifest.generator", "must be non-empty text")

    edition = manifest.get("edition")
    if _check_keys(
        issues, edition, "manifest.edition",
        required=("id", "revision", "status", "label", "steward", "previous_revision", "ext"),
        allowed=("id", "revision", "status", "label", "steward", "previous_revision", "release", "citation", "ext"),
    ):
        if not _is_id(edition.get("id")):
            _issue(issues, "manifest.edition.id", "must be a portable identifier")
        if not _is_revision(edition.get("revision")):
            _issue(issues, "manifest.edition.revision", "must be a portable revision")
        if edition.get("status") not in EDITION_STATUSES:
            _issue(issues, "manifest.edition.status", f"must be one of {sorted(EDITION_STATUSES)}")
        if edition.get("previous_revision") is not None and not _is_revision(edition.get("previous_revision")):
            _issue(issues, "manifest.edition.previous_revision", "must be null or a portable revision")
        if not isinstance(edition.get("steward"), dict):
            _issue(issues, "manifest.edition.steward", "must identify the edition steward")
        else:
            _check_actor(issues, edition["steward"], "manifest.edition.steward")
        _check_ext(issues, edition.get("ext", {}), "manifest.edition.ext")

    catalog = manifest.get("catalog")
    if _check_keys(
        issues, catalog, "manifest.catalog",
        required=("record_id", "title", "material_type", "repository", "call_number", "dates", "languages", "identifiers", "source_url", "rights", "ext"),
        allowed=("record_id", "title", "alternative_titles", "material_type", "contributors", "repository", "call_number", "dates", "languages", "extent", "description", "subjects", "identifiers", "source_url", "rights", "license", "ext"),
    ):
        for key in ("record_id", "title", "material_type", "repository", "call_number", "rights"):
            if not isinstance(catalog.get(key), str) or not catalog[key].strip():
                _issue(issues, f"manifest.catalog.{key}", "must be non-empty text")
        if not _is_public_uri(catalog.get("source_url")):
            _issue(issues, "manifest.catalog.source_url", "must be a public http(s) or urn URI")
        for key in ("dates", "languages", "identifiers"):
            if not isinstance(catalog.get(key), list):
                _issue(issues, f"manifest.catalog.{key}", "must be an array")
        _check_ext(issues, catalog.get("ext", {}), "manifest.catalog.ext")

    source = manifest.get("source")
    if _check_keys(
        issues, source, "manifest.source",
        required=("filename", "media_type", "sha256", "page_count", "excluded_pages", "extraction", "ext"),
        allowed=("filename", "media_type", "sha256", "page_count", "excluded_pages", "source_url", "extraction", "ext"),
    ):
        if not isinstance(source.get("filename"), str) or not source["filename"].strip():
            _issue(issues, "manifest.source.filename", "must be a display filename")
        if not isinstance(source.get("sha256"), str) or not SHA256_RE.fullmatch(source["sha256"]):
            _issue(issues, "manifest.source.sha256", "must be a lowercase SHA-256 digest")
        if not isinstance(source.get("page_count"), int) or source["page_count"] < 1:
            _issue(issues, "manifest.source.page_count", "must be a positive integer")
        if "source_url" in source and not _is_public_uri(source["source_url"]):
            _issue(issues, "manifest.source.source_url", "must be a public URI; local paths are forbidden")
        _check_ext(issues, source.get("ext", {}), "manifest.source.ext")

    registry_sets = _registry_sets(issues, manifest.get("registries"))

    resources = manifest.get("resources")
    resource_by_member: dict[str, dict[str, Any]] = {}
    if not isinstance(resources, list):
        _issue(issues, "manifest.resources", "must be an array")
    else:
        for index, resource in enumerate(resources):
            loc = f"manifest.resources[{index}]"
            if not _check_keys(
                issues, resource, loc,
                required=("member", "media_type", "role", "sha256", "bytes"),
                allowed=("member", "media_type", "role", "sha256", "bytes", "ext"),
            ):
                continue
            member = resource.get("member")
            if not _safe_member(member) or not _allowed_member(member):
                _issue(issues, f"{loc}.member", "must be an allowed portable resource member")
            elif member in resource_by_member:
                _issue(issues, f"{loc}.member", "duplicates another resource member")
            else:
                resource_by_member[member] = resource
            if isinstance(member, str) and _is_database_member(member):
                _issue(issues, f"{loc}.member", "mutable SQLite authority databases must remain external")
            if not isinstance(resource.get("media_type"), str) or not MEDIA_TYPE_RE.fullmatch(resource["media_type"]):
                _issue(issues, f"{loc}.media_type", "must be a media type without parameters")
            if not _is_id(resource.get("role")):
                _issue(issues, f"{loc}.role", "must be a portable resource role")
            elif resource.get("role") not in CORE_RESOURCE_ROLES | registry_sets["resource_roles"]:
                _issue(issues, f"{loc}.role", "must be a core or declared resource role")
            if not isinstance(resource.get("sha256"), str) or not SHA256_RE.fullmatch(resource["sha256"]):
                _issue(issues, f"{loc}.sha256", "must be a lowercase SHA-256 digest")
            if not isinstance(resource.get("bytes"), int) or not 0 <= resource["bytes"] <= MAX_MEMBER_BYTES:
                _issue(issues, f"{loc}.bytes", f"must be between 0 and {MAX_MEMBER_BYTES}")
            _check_ext(issues, resource.get("ext", {}), f"{loc}.ext")
            if members is not None and isinstance(member, str):
                payload = members.get(member)
                if payload is None:
                    _issue(issues, f"{loc}.member", "declared resource is absent from the archive")
                else:
                    if resource.get("bytes") != len(payload):
                        _issue(issues, f"{loc}.bytes", "does not match the resource byte length")
                    if resource.get("sha256") != _sha256(payload):
                        _issue(issues, f"{loc}.sha256", "does not match the resource bytes")

    canvases = manifest.get("canvases")
    canvas_by_id: dict[str, dict[str, Any]] = {}
    sequences: set[int] = set()
    if not isinstance(canvases, list) or not canvases:
        _issue(issues, "manifest.canvases", "must be a non-empty array")
    else:
        for index, canvas in enumerate(canvases):
            loc = f"manifest.canvases[{index}]"
            if not _check_keys(
                issues, canvas, loc,
                required=("id", "revision", "sequence", "label", "source_page", "image_member", "dimensions", "ext"),
                allowed=("id", "revision", "sequence", "label", "source_page", "source_label", "image_member", "dimensions", "ext"),
            ):
                continue
            canvas_id = canvas.get("id")
            if not _is_id(canvas_id):
                _issue(issues, f"{loc}.id", "must be a portable identifier")
            elif canvas_id in canvas_by_id:
                _issue(issues, f"{loc}.id", "duplicates another canvas")
            else:
                canvas_by_id[canvas_id] = canvas
            if not _is_revision(canvas.get("revision")):
                _issue(issues, f"{loc}.revision", "must be a portable revision")
            sequence = canvas.get("sequence")
            if not isinstance(sequence, int) or sequence < 1:
                _issue(issues, f"{loc}.sequence", "must be a positive integer")
            elif sequence in sequences:
                _issue(issues, f"{loc}.sequence", "duplicates another canvas sequence")
            else:
                sequences.add(sequence)
            member = canvas.get("image_member")
            resource = resource_by_member.get(member)
            if resource is None:
                _issue(issues, f"{loc}.image_member", "must reference a declared resource")
            elif resource.get("role") != "canvas-image":
                _issue(issues, f"{loc}.image_member", "must reference a canvas-image resource")
            dimensions = canvas.get("dimensions")
            if not isinstance(dimensions, dict) or set(dimensions) != {"width", "height"}:
                _issue(issues, f"{loc}.dimensions", "must contain exactly width and height")
            elif any(not isinstance(dimensions.get(field), int) or dimensions[field] < 1 for field in ("width", "height")):
                _issue(issues, f"{loc}.dimensions", "width and height must be positive integers")
            _check_ext(issues, canvas.get("ext", {}), f"{loc}.ext")
        if sequences and sequences != set(range(1, len(sequences) + 1)):
            _issue(issues, "manifest.canvases", "sequence values must form an unbroken 1-based range")

    region_types = manifest.get("region_types")
    region_type_by_id: dict[str, dict[str, Any]] = {}
    if not isinstance(region_types, list) or not region_types:
        _issue(issues, "manifest.region_types", "must declare at least one hierarchical region type")
    else:
        for index, region_type in enumerate(region_types):
            loc = f"manifest.region_types[{index}]"
            if not _check_keys(
                issues, region_type, loc,
                required=("id", "label", "facet", "parent_id", "description", "custom", "ext"),
                allowed=("id", "label", "facet", "parent_id", "description", "custom", "color", "ext"),
            ):
                continue
            type_id = region_type.get("id")
            if not _is_id(type_id):
                _issue(issues, f"{loc}.id", "must be a portable identifier")
            elif type_id in region_type_by_id:
                _issue(issues, f"{loc}.id", "duplicates another region type")
            else:
                region_type_by_id[type_id] = region_type
            if not _is_id(region_type.get("facet")):
                _issue(issues, f"{loc}.facet", "must be a portable facet identifier")
            if region_type.get("parent_id") is not None and not _is_id(region_type.get("parent_id")):
                _issue(issues, f"{loc}.parent_id", "must be null or a portable identifier")
            if not isinstance(region_type.get("custom"), bool):
                _issue(issues, f"{loc}.custom", "must be boolean")
            _check_ext(issues, region_type.get("ext", {}), f"{loc}.ext")
        for type_id, record in region_type_by_id.items():
            parent = record.get("parent_id")
            if parent is not None and parent not in region_type_by_id:
                _issue(issues, f"manifest.region_types[{type_id}].parent_id", "references a missing region type")
            elif parent is not None and region_type_by_id[parent].get("facet") != record.get("facet"):
                _issue(issues, f"manifest.region_types[{type_id}].parent_id", "parent must belong to the same facet")
            seen: set[str] = set()
            cursor: str | None = type_id
            while cursor is not None and cursor in region_type_by_id:
                if cursor in seen:
                    _issue(issues, f"manifest.region_types[{type_id}]", "region type hierarchy contains a cycle")
                    break
                seen.add(cursor)
                cursor = region_type_by_id[cursor].get("parent_id")

    layer_descriptors = manifest.get("layers")
    descriptor_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    current_ids: set[str] = set()
    if not isinstance(layer_descriptors, list):
        _issue(issues, "manifest.layers", "must be an array")
    else:
        for index, layer in enumerate(layer_descriptors):
            loc = f"manifest.layers[{index}]"
            if not _check_keys(
                issues, layer, loc,
                required=("id", "revision", "kind", "label", "member", "current", "variant", "ext"),
                allowed=("id", "revision", "kind", "label", "member", "current", "variant", "language", "supersedes", "ext"),
            ):
                continue
            layer_id, revision = layer.get("id"), layer.get("revision")
            if not _is_id(layer_id):
                _issue(issues, f"{loc}.id", "must be a portable identifier")
            if not _is_revision(revision):
                _issue(issues, f"{loc}.revision", "must be a portable revision")
            key = (layer_id, revision)
            if key in descriptor_by_key:
                _issue(issues, loc, "duplicates another layer id/revision")
            elif isinstance(layer_id, str) and isinstance(revision, str):
                descriptor_by_key[key] = layer
            kind = layer.get("kind")
            if kind not in CORE_LAYER_KINDS | registry_sets["layer_kinds"]:
                _issue(issues, f"{loc}.kind", "must be a core or declared layer kind")
            if layer.get("current") is True:
                if layer_id in current_ids:
                    _issue(issues, f"{loc}.current", "only one revision per layer id may be current")
                current_ids.add(layer_id)
            elif layer.get("current") is not False:
                _issue(issues, f"{loc}.current", "must be boolean")
            member = layer.get("member")
            resource = resource_by_member.get(member)
            if resource is None:
                _issue(issues, f"{loc}.member", "must reference a declared resource")
            elif resource.get("role") != "layer":
                _issue(issues, f"{loc}.member", "must reference a layer resource")
            if "language" in layer and not LANGUAGE_RE.fullmatch(str(layer["language"])):
                _issue(issues, f"{loc}.language", "must be a BCP 47-like language tag")
            _check_ext(issues, layer.get("ext", {}), f"{loc}.ext")

    snapshots = manifest.get("authority_snapshots")
    snapshot_ids: set[str] = set()
    if not isinstance(snapshots, list):
        _issue(issues, "manifest.authority_snapshots", "must be an array")
    else:
        for index, snapshot in enumerate(snapshots):
            loc = f"manifest.authority_snapshots[{index}]"
            if not _check_keys(
                issues, snapshot, loc,
                required=("id", "database_id", "release", "member", "created_at", "ext"),
                allowed=("id", "database_id", "release", "member", "created_at", "ext"),
            ):
                continue
            snapshot_id = snapshot.get("id")
            if not _is_id(snapshot_id) or snapshot_id in snapshot_ids:
                _issue(issues, f"{loc}.id", "must be a unique portable identifier")
            else:
                snapshot_ids.add(snapshot_id)
            if not _is_timestamp(snapshot.get("created_at")):
                _issue(issues, f"{loc}.created_at", "must be an ISO 8601 UTC timestamp")
            resource = resource_by_member.get(snapshot.get("member"))
            if resource is None:
                _issue(issues, f"{loc}.member", "must reference a declared snapshot resource")
            elif resource.get("role") != "authority-snapshot" or resource.get("media_type") != "application/json":
                _issue(issues, f"{loc}.member", "must reference an application/json authority-snapshot resource")
            _check_ext(issues, snapshot.get("ext", {}), f"{loc}.ext")

    authorities = manifest.get("external_authorities")
    if not isinstance(authorities, list):
        _issue(issues, "manifest.external_authorities", "must be an array")
    else:
        for index, authority in enumerate(authorities):
            loc = f"manifest.external_authorities[{index}]"
            if not _check_keys(
                issues, authority, loc,
                required=("id", "kind", "database_id", "snapshot_id", "resolver_template", "ext"),
                allowed=("id", "kind", "database_id", "snapshot_id", "resolver_template", "ext"),
            ):
                continue
            for field in ("id", "kind", "database_id"):
                if not _is_id(authority.get(field)):
                    _issue(issues, f"{loc}.{field}", "must be a portable identifier")
            snapshot_id = authority.get("snapshot_id")
            if snapshot_id is not None and snapshot_id not in snapshot_ids:
                _issue(issues, f"{loc}.snapshot_id", "must be null or a declared authority snapshot id")
            template = authority.get("resolver_template")
            if not isinstance(template, str) or "{id}" not in template or not template.startswith("https://"):
                _issue(issues, f"{loc}.resolver_template", "must be an HTTPS template containing {id}")
            _check_ext(issues, authority.get("ext", {}), f"{loc}.ext")

    capabilities = manifest.get("capabilities")
    if not isinstance(capabilities, list) or any(not _is_id(item) for item in capabilities) or len(set(capabilities)) != len(capabilities):
        _issue(issues, "manifest.capabilities", "must contain unique portable identifiers")
    elif any(item not in CORE_CAPABILITIES | registry_sets["capabilities"] for item in capabilities):
        _issue(issues, "manifest.capabilities", "each capability must be core or declared in the registry")
    _check_ext(issues, manifest.get("ext", {}), "manifest.ext")

    if members is not None:
        expected = set(resource_by_member)
        actual_resources = set(members) - _REQUIRED_MEMBERS
        for member in sorted(actual_resources - expected):
            _issue(issues, f"archive:{member}", "resource is not declared in manifest.resources")
        for member in sorted(expected - actual_resources):
            _issue(issues, f"manifest.resources[{member}]", "declared resource is absent")

    return issues


def _canvas_revisions(manifest: Mapping[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for canvas in manifest.get("canvases", []):
        if isinstance(canvas, dict) and isinstance(canvas.get("id"), str):
            result[canvas["id"]] = canvas.get("revision")
    return result


def _layer_keys(manifest: Mapping[str, Any]) -> set[tuple[str, str]]:
    return {
        (layer.get("id"), layer.get("revision"))
        for layer in manifest.get("layers", [])
        if isinstance(layer, dict)
    }


def validate_layer(
    layer: Any,
    manifest: Mapping[str, Any] | None = None,
    descriptor: Mapping[str, Any] | None = None,
) -> list[Issue]:
    """Validate one normalized editorial layer."""

    issues: list[Issue] = []
    required = (
        "$schema", "id", "revision", "kind", "label", "status", "language",
        "provenance", "dependencies", "history", "reviews", "data", "ext",
    )
    if not _check_keys(issues, layer, "layer", required=required, allowed=required):
        return issues
    if layer.get("$schema") != LAYER_SCHEMA_ID:
        _issue(issues, "layer.$schema", f"must be {LAYER_SCHEMA_ID}")
    if not _is_id(layer.get("id")):
        _issue(issues, "layer.id", "must be a portable identifier")
    if not _is_revision(layer.get("revision")):
        _issue(issues, "layer.revision", "must be a portable revision")
    kind = layer.get("kind")
    registry_sets = _registry_sets([], (manifest or {}).get("registries", {
        "layer_kinds": [], "selector_types": [], "resource_roles": [],
        "region_relation_predicates": [], "capabilities": [],
    }))
    if kind not in CORE_LAYER_KINDS | registry_sets["layer_kinds"]:
        _issue(issues, "layer.kind", "must be a core or declared layer kind")
    if layer.get("status") not in LAYER_STATUSES:
        _issue(issues, "layer.status", f"must be one of {sorted(LAYER_STATUSES)}")
    language = layer.get("language")
    if language is not None and (not isinstance(language, str) or not LANGUAGE_RE.fullmatch(language)):
        _issue(issues, "layer.language", "must be null or a BCP 47-like language tag")
    if descriptor is not None:
        for field in ("id", "revision", "kind", "label"):
            if layer.get(field) != descriptor.get(field):
                _issue(issues, f"layer.{field}", "does not match the manifest layer descriptor")
        if layer.get("language") != descriptor.get("language"):
            _issue(issues, "layer.language", "does not match the manifest layer descriptor")

    provenance = layer.get("provenance")
    if _check_keys(
        issues, provenance, "layer.provenance",
        required=("actor", "generated_at", "method", "parameters", "sources", "ext"),
        allowed=("actor", "generated_at", "method", "parameters", "sources", "ext"),
    ):
        _check_actor(issues, provenance.get("actor"), "layer.provenance.actor")
        if not _is_timestamp(provenance.get("generated_at")):
            _issue(issues, "layer.provenance.generated_at", "must be an ISO 8601 UTC timestamp")
        if not isinstance(provenance.get("parameters"), dict):
            _issue(issues, "layer.provenance.parameters", "must be an object")
        if not isinstance(provenance.get("sources"), list):
            _issue(issues, "layer.provenance.sources", "must be an array")
        _check_ext(issues, provenance.get("ext", {}), "layer.provenance.ext")

    manifest_keys = _layer_keys(manifest or {})
    dependencies = layer.get("dependencies")
    if not isinstance(dependencies, list):
        _issue(issues, "layer.dependencies", "must be an array")
    else:
        seen_dependencies: set[tuple[str, str, str]] = set()
        for index, dependency in enumerate(dependencies):
            loc = f"layer.dependencies[{index}]"
            if not _check_keys(
                issues, dependency, loc,
                required=("layer_id", "revision", "relation"),
                allowed=("layer_id", "revision", "relation"),
            ):
                continue
            key = (dependency.get("layer_id"), dependency.get("revision"))
            triple = (*key, dependency.get("relation"))
            if not all(_is_id(value) for value in (dependency.get("layer_id"), dependency.get("relation"))) or not _is_revision(dependency.get("revision")):
                _issue(issues, loc, "must contain portable layer_id, revision, and relation values")
            if triple in seen_dependencies:
                _issue(issues, loc, "duplicates another dependency")
            seen_dependencies.add(triple)
            if manifest is not None and key not in manifest_keys:
                _issue(issues, loc, "references a missing layer revision")
            if key == (layer.get("id"), layer.get("revision")):
                _issue(issues, loc, "a layer may not depend on itself")

    history = layer.get("history")
    history_ids: set[str] = set()
    if not isinstance(history, list) or not history:
        _issue(issues, "layer.history", "must contain at least the creation event")
    else:
        previous_time: str | None = None
        for index, event in enumerate(history):
            loc = f"layer.history[{index}]"
            if not _check_keys(
                issues, event, loc,
                required=("id", "action", "actor", "at", "message", "base_revision", "ext"),
                allowed=("id", "action", "actor", "at", "message", "base_revision", "ext"),
            ):
                continue
            event_id = event.get("id")
            if not _is_id(event_id) or event_id in history_ids:
                _issue(issues, f"{loc}.id", "must be a unique portable identifier")
            else:
                history_ids.add(event_id)
            _check_actor(issues, event.get("actor"), f"{loc}.actor")
            if not _is_timestamp(event.get("at")):
                _issue(issues, f"{loc}.at", "must be an ISO 8601 UTC timestamp")
            elif previous_time is not None and event["at"] < previous_time:
                _issue(issues, f"{loc}.at", "history must be chronological")
            else:
                previous_time = event.get("at")
            if event.get("base_revision") is not None and not _is_revision(event.get("base_revision")):
                _issue(issues, f"{loc}.base_revision", "must be null or a portable revision")
            _check_ext(issues, event.get("ext", {}), f"{loc}.ext")

    reviews = layer.get("reviews")
    approved_by_human = False
    if not isinstance(reviews, list):
        _issue(issues, "layer.reviews", "must be an array")
    else:
        review_ids: set[str] = set()
        for index, review in enumerate(reviews):
            loc = f"layer.reviews[{index}]"
            if not _check_keys(
                issues, review, loc,
                required=("id", "decision", "reviewer", "at", "rationale", "applies_to_revision", "ext"),
                allowed=("id", "decision", "reviewer", "at", "rationale", "applies_to_revision", "ext"),
            ):
                continue
            review_id = review.get("id")
            if not _is_id(review_id) or review_id in review_ids:
                _issue(issues, f"{loc}.id", "must be a unique portable identifier")
            else:
                review_ids.add(review_id)
            if review.get("decision") not in REVIEW_DECISIONS:
                _issue(issues, f"{loc}.decision", f"must be one of {sorted(REVIEW_DECISIONS)}")
            _check_actor(issues, review.get("reviewer"), f"{loc}.reviewer")
            if review.get("decision") in {"approve", "reject"} and isinstance(review.get("reviewer"), dict) and review["reviewer"].get("type") != "human":
                _issue(issues, f"{loc}.reviewer.type", "only a human may approve or reject scholarly content")
            if review.get("applies_to_revision") != layer.get("revision"):
                _issue(issues, f"{loc}.applies_to_revision", "must pin this exact layer revision")
            if not _is_timestamp(review.get("at")):
                _issue(issues, f"{loc}.at", "must be an ISO 8601 UTC timestamp")
            if review.get("decision") == "approve" and isinstance(review.get("reviewer"), dict) and review["reviewer"].get("type") == "human":
                approved_by_human = True
            _check_ext(issues, review.get("ext", {}), f"{loc}.ext")
    if layer.get("status") in {"approved", "frozen"} and not approved_by_human:
        _issue(issues, "layer.status", "approved or frozen content requires a human approval of this revision")

    data = layer.get("data")
    if not isinstance(data, dict):
        _issue(issues, "layer.data", "must be an object")
    else:
        canvas_revisions = _canvas_revisions(manifest or {})
        if kind == "region":
            _validate_region_data(
                issues, data, canvas_revisions, manifest or {},
                registry_sets["selector_types"],
                registry_sets["region_relation_predicates"],
            )
        elif kind in {"transcription", "translation"}:
            _validate_text_data(issues, data, canvas_revisions, kind)
        elif kind == "entity":
            _validate_entity_data(issues, data, canvas_revisions, registry_sets["selector_types"])
        elif kind in {"knowledge", "commentary"}:
            _validate_entry_data(issues, data)
        elif kind == "notes":
            _validate_note_data(issues, data)
        elif kind == "reprocessing":
            _validate_reprocessing_data(issues, data)
    _check_ext(issues, layer.get("ext", {}), "layer.ext")
    return issues


def _check_canvas(
    issues: list[Issue],
    record: Mapping[str, Any],
    location: str,
    canvas_revisions: Mapping[str, str],
) -> str | None:
    canvas_id = record.get("canvas_id")
    revision = record.get("canvas_revision")
    if not _is_id(canvas_id):
        _issue(issues, f"{location}.canvas_id", "must be a portable identifier")
        return None
    if not _is_revision(revision):
        _issue(issues, f"{location}.canvas_revision", "must be a portable revision")
    expected = canvas_revisions.get(canvas_id)
    if canvas_revisions and expected is None:
        _issue(issues, f"{location}.canvas_id", "references a missing canvas")
    elif expected is not None and revision != expected:
        _issue(issues, f"{location}.canvas_revision", "does not pin the canvas revision")
    return expected


def _validate_region_data(
    issues: list[Issue],
    data: Mapping[str, Any],
    canvas_revisions: Mapping[str, str],
    manifest: Mapping[str, Any],
    custom_selector_types: set[str],
    custom_relation_predicates: set[str],
) -> None:
    if set(data) - {"regions", "reading_flows", "relations"}:
        _issue(issues, "layer.data", "region data may contain only regions, reading_flows, and relations")
    regions = data.get("regions")
    if not isinstance(regions, list):
        _issue(issues, "layer.data.regions", "must be an array")
        return
    type_ids = {record.get("id") for record in manifest.get("region_types", []) if isinstance(record, dict)}
    by_id: dict[str, Mapping[str, Any]] = {}
    for index, region in enumerate(regions):
        loc = f"layer.data.regions[{index}]"
        if not _check_keys(
            issues, region, loc,
            required=("id", "canvas_id", "canvas_revision", "selector", "type_ids", "parent_region_id", "order", "label", "confidence", "ext"),
            allowed=("id", "canvas_id", "canvas_revision", "selector", "type_ids", "parent_region_id", "order", "label", "confidence", "ext"),
        ):
            continue
        region_id = region.get("id")
        if not _is_id(region_id) or region_id in by_id:
            _issue(issues, f"{loc}.id", "must be a unique portable identifier")
        else:
            by_id[region_id] = region
        canvas_revision = _check_canvas(issues, region, loc, canvas_revisions)
        _check_selector(
            issues, region.get("selector"), f"{loc}.selector",
            canvas_revision=canvas_revision,
            custom_types=custom_selector_types,
        )
        assigned = region.get("type_ids")
        if not isinstance(assigned, list) or not assigned or len(set(assigned)) != len(assigned):
            _issue(issues, f"{loc}.type_ids", "must contain unique declared region type ids")
        else:
            for assigned_id in assigned:
                if assigned_id not in type_ids:
                    _issue(issues, f"{loc}.type_ids", f"references undeclared region type {assigned_id!r}")
        parent = region.get("parent_region_id")
        if parent is not None and not _is_id(parent):
            _issue(issues, f"{loc}.parent_region_id", "must be null or a portable identifier")
        if not isinstance(region.get("order"), int) or region["order"] < 0:
            _issue(issues, f"{loc}.order", "must be a non-negative integer")
        confidence = region.get("confidence")
        if confidence is not None and (not isinstance(confidence, (int, float)) or isinstance(confidence, bool) or not 0 <= confidence <= 1):
            _issue(issues, f"{loc}.confidence", "must be null or a number from 0 to 1")
        _check_ext(issues, region.get("ext", {}), f"{loc}.ext")
    for region_id, region in by_id.items():
        parent_id = region.get("parent_region_id")
        if parent_id is not None:
            parent = by_id.get(parent_id)
            if parent is None:
                _issue(issues, f"layer.data.regions[{region_id}].parent_region_id", "references a missing region")
            elif parent.get("canvas_id") != region.get("canvas_id"):
                _issue(issues, f"layer.data.regions[{region_id}].parent_region_id", "parent must be on the same canvas")
        seen: set[str] = set()
        cursor: str | None = region_id
        while cursor is not None and cursor in by_id:
            if cursor in seen:
                _issue(issues, f"layer.data.regions[{region_id}]", "region parent hierarchy contains a cycle")
                break
            seen.add(cursor)
            cursor = by_id[cursor].get("parent_region_id")

    reading_flows = data.get("reading_flows")
    if not isinstance(reading_flows, list):
        _issue(issues, "layer.data.reading_flows", "must be an array")
    else:
        flow_ids: set[str] = set()
        for index, flow in enumerate(reading_flows):
            loc = f"layer.data.reading_flows[{index}]"
            if not _check_keys(
                issues, flow, loc,
                required=("id", "label", "direction", "ordered_region_ids", "ext"),
                allowed=("id", "label", "direction", "ordered_region_ids", "ext"),
            ):
                continue
            flow_id = flow.get("id")
            if not _is_id(flow_id) or flow_id in flow_ids:
                _issue(issues, f"{loc}.id", "must be a unique portable identifier")
            else:
                flow_ids.add(flow_id)
            if flow.get("direction") not in {"ltr", "rtl", "ttb", "btt", "mixed"}:
                _issue(issues, f"{loc}.direction", "must be ltr, rtl, ttb, btt, or mixed")
            ordered = flow.get("ordered_region_ids")
            if not isinstance(ordered, list) or len(set(ordered)) != len(ordered):
                _issue(issues, f"{loc}.ordered_region_ids", "must contain unique region ids")
            else:
                for region_id in ordered:
                    if region_id not in by_id:
                        _issue(issues, f"{loc}.ordered_region_ids", f"references missing region {region_id!r}")
            _check_ext(issues, flow.get("ext", {}), f"{loc}.ext")

    relations = data.get("relations")
    if not isinstance(relations, list):
        _issue(issues, "layer.data.relations", "must be an array")
    else:
        relation_ids: set[str] = set()
        for index, relation in enumerate(relations):
            loc = f"layer.data.relations[{index}]"
            if not _check_keys(
                issues, relation, loc,
                required=("id", "subject_region_id", "predicate", "object_region_id", "confidence", "ext"),
                allowed=("id", "subject_region_id", "predicate", "object_region_id", "confidence", "ext"),
            ):
                continue
            relation_id = relation.get("id")
            if not _is_id(relation_id) or relation_id in relation_ids:
                _issue(issues, f"{loc}.id", "must be a unique portable identifier")
            else:
                relation_ids.add(relation_id)
            subject, object_id = relation.get("subject_region_id"), relation.get("object_region_id")
            if subject not in by_id or object_id not in by_id:
                _issue(issues, loc, "subject and object must reference regions in this revision")
            elif subject == object_id:
                _issue(issues, loc, "a region relation may not point to itself")
            if not _is_id(relation.get("predicate")):
                _issue(issues, f"{loc}.predicate", "must be a portable relation identifier")
            elif relation.get("predicate") not in CORE_REGION_RELATIONS | custom_relation_predicates:
                _issue(issues, f"{loc}.predicate", "must be a core or declared region relation predicate")
            if relation.get("confidence") not in CONFIDENCE_TERMS:
                _issue(issues, f"{loc}.confidence", f"must be one of {sorted(CONFIDENCE_TERMS)}")
            _check_ext(issues, relation.get("ext", {}), f"{loc}.ext")


def _validate_text_data(
    issues: list[Issue],
    data: Mapping[str, Any],
    canvas_revisions: Mapping[str, str],
    kind: str,
) -> None:
    if set(data) - {"passages"}:
        _issue(issues, "layer.data", f"{kind} data may contain only passages")
    passages = data.get("passages")
    if not isinstance(passages, list):
        _issue(issues, "layer.data.passages", "must be an array")
        return
    seen: set[str] = set()
    for index, passage in enumerate(passages):
        loc = f"layer.data.passages[{index}]"
        if not _check_keys(
            issues, passage, loc,
            required=("id", "canvas_id", "canvas_revision", "region_ref", "order", "text", "alignment_refs", "uncertainties", "confidence", "ext"),
            allowed=("id", "canvas_id", "canvas_revision", "region_ref", "order", "text", "alignment_refs", "uncertainties", "confidence", "ext"),
        ):
            continue
        passage_id = passage.get("id")
        if not _is_id(passage_id) or passage_id in seen:
            _issue(issues, f"{loc}.id", "must be a unique portable identifier")
        else:
            seen.add(passage_id)
        _check_canvas(issues, passage, loc, canvas_revisions)
        _check_ref(issues, passage.get("region_ref"), f"{loc}.region_ref", region=True)
        if not isinstance(passage.get("text"), str):
            _issue(issues, f"{loc}.text", "must be text")
        if not isinstance(passage.get("order"), int) or passage["order"] < 0:
            _issue(issues, f"{loc}.order", "must be a non-negative integer")
        alignment_refs = passage.get("alignment_refs")
        if not isinstance(alignment_refs, list):
            _issue(issues, f"{loc}.alignment_refs", "must be an array")
        else:
            for ref_index, ref in enumerate(alignment_refs):
                _check_ref(issues, ref, f"{loc}.alignment_refs[{ref_index}]")
        uncertainties = passage.get("uncertainties")
        if not isinstance(uncertainties, list):
            _issue(issues, f"{loc}.uncertainties", "must be an array")
        else:
            for uncertainty_index, uncertainty in enumerate(uncertainties):
                uloc = f"{loc}.uncertainties[{uncertainty_index}]"
                if not isinstance(uncertainty, dict) or set(uncertainty) - {"start", "end", "state", "note"}:
                    _issue(issues, uloc, "must contain start, end, state, and optional note")
                    continue
                start, end = uncertainty.get("start"), uncertainty.get("end")
                if not isinstance(start, int) or not isinstance(end, int) or start < 0 or end < start or (isinstance(passage.get("text"), str) and end > len(passage["text"])):
                    _issue(issues, uloc, "character range must lie within passage text")
                if uncertainty.get("state") not in {"illegible", "conjectural", "disputed", "supplied", "deleted"}:
                    _issue(issues, f"{uloc}.state", "is not a controlled uncertainty state")
        confidence = passage.get("confidence")
        if confidence is not None and (not isinstance(confidence, (int, float)) or isinstance(confidence, bool) or not 0 <= confidence <= 1):
            _issue(issues, f"{loc}.confidence", "must be null or a number from 0 to 1")
        _check_ext(issues, passage.get("ext", {}), f"{loc}.ext")


def _validate_entity_data(
    issues: list[Issue],
    data: Mapping[str, Any],
    canvas_revisions: Mapping[str, str],
    custom_selector_types: set[str],
) -> None:
    if set(data) - {"mentions"}:
        _issue(issues, "layer.data", "entity data may contain only mentions")
    mentions = data.get("mentions")
    if not isinstance(mentions, list):
        _issue(issues, "layer.data.mentions", "must be an array")
        return
    seen: set[str] = set()
    for index, mention in enumerate(mentions):
        loc = f"layer.data.mentions[{index}]"
        if not _check_keys(
            issues, mention, loc,
            required=("id", "canvas_id", "canvas_revision", "region_ref", "selector", "text_anchor", "authority_refs", "review_state", "ext"),
            allowed=("id", "canvas_id", "canvas_revision", "region_ref", "selector", "text_anchor", "authority_refs", "review_state", "ext"),
        ):
            continue
        mention_id = mention.get("id")
        if not _is_id(mention_id) or mention_id in seen:
            _issue(issues, f"{loc}.id", "must be a unique portable identifier")
        else:
            seen.add(mention_id)
        canvas_revision = _check_canvas(issues, mention, loc, canvas_revisions)
        _check_ref(issues, mention.get("region_ref"), f"{loc}.region_ref", region=True)
        if mention.get("selector") is not None:
            _check_selector(
                issues, mention["selector"], f"{loc}.selector",
                canvas_revision=canvas_revision,
                custom_types=custom_selector_types,
            )
        anchor = mention.get("text_anchor")
        if _check_keys(
            issues, anchor, f"{loc}.text_anchor",
            required=("layer_id", "revision", "passage_id", "start", "end", "exact", "prefix", "suffix", "status"),
            allowed=("layer_id", "revision", "passage_id", "start", "end", "exact", "prefix", "suffix", "status"),
        ):
            if not _is_id(anchor.get("layer_id")) or not _is_revision(anchor.get("revision")) or not _is_id(anchor.get("passage_id")):
                _issue(issues, f"{loc}.text_anchor", "must pin a passage in an exact text-layer revision")
            start, end, exact = anchor.get("start"), anchor.get("end"), anchor.get("exact")
            if not isinstance(start, int) or not isinstance(end, int) or start < 0 or end < start:
                _issue(issues, f"{loc}.text_anchor", "must contain a valid character range")
            elif isinstance(exact, str) and len(exact) != end - start:
                _issue(issues, f"{loc}.text_anchor.exact", "length must equal end minus start")
            if anchor.get("status") not in {"current", "stale", "repaired", "ambiguous"}:
                _issue(issues, f"{loc}.text_anchor.status", "is not a controlled anchor state")
            for field in ("exact", "prefix", "suffix"):
                if not isinstance(anchor.get(field), str):
                    _issue(issues, f"{loc}.text_anchor.{field}", "must be text")
        authority_refs = mention.get("authority_refs")
        if not isinstance(authority_refs, list):
            _issue(issues, f"{loc}.authority_refs", "must be an array")
        else:
            for ref_index, ref in enumerate(authority_refs):
                rloc = f"{loc}.authority_refs[{ref_index}]"
                if not _check_keys(
                    issues, ref, rloc,
                    required=("database_id", "snapshot_id", "node_type", "node_id", "role", "assertion_ids"),
                    allowed=("database_id", "snapshot_id", "node_type", "node_id", "role", "assertion_ids"),
                ):
                    continue
                if any(not _is_id(ref.get(field)) for field in ("database_id", "snapshot_id", "node_id", "role")):
                    _issue(issues, rloc, "database, snapshot, node, and role identifiers must be portable")
                if ref.get("node_type") not in {"name", "concept", "referent", "assertion", "evidence", "review"}:
                    _issue(issues, f"{rloc}.node_type", "is not an authority node type")
                if not isinstance(ref.get("assertion_ids"), list) or any(not _is_id(value) for value in ref.get("assertion_ids", [])):
                    _issue(issues, f"{rloc}.assertion_ids", "must contain portable identifiers")
        if mention.get("review_state") not in {"proposed", "accepted", "rejected", "disputed", "unresolved"}:
            _issue(issues, f"{loc}.review_state", "is not a controlled review state")
        _check_ext(issues, mention.get("ext", {}), f"{loc}.ext")


def _validate_entry_data(issues: list[Issue], data: Mapping[str, Any]) -> None:
    if set(data) - {"entries"}:
        _issue(issues, "layer.data", "knowledge/commentary data may contain only entries")
    entries = data.get("entries")
    if not isinstance(entries, list):
        _issue(issues, "layer.data.entries", "must be an array")
        return
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        loc = f"layer.data.entries[{index}]"
        if not _check_keys(
            issues, entry, loc,
            required=("id", "target_uri", "title", "text", "citations", "ext"),
            allowed=("id", "target_uri", "title", "text", "citations", "ext"),
        ):
            continue
        if not _is_id(entry.get("id")) or entry.get("id") in seen:
            _issue(issues, f"{loc}.id", "must be a unique portable identifier")
        else:
            seen.add(entry["id"])
        _check_target_uri(issues, entry.get("target_uri"), f"{loc}.target_uri")
        if not isinstance(entry.get("text"), str):
            _issue(issues, f"{loc}.text", "must be text")
        if not isinstance(entry.get("citations"), list):
            _issue(issues, f"{loc}.citations", "must be an array")
        _check_ext(issues, entry.get("ext", {}), f"{loc}.ext")


def _validate_note_data(issues: list[Issue], data: Mapping[str, Any]) -> None:
    if set(data) - {"notes"}:
        _issue(issues, "layer.data", "notes data may contain only notes")
    notes = data.get("notes")
    if not isinstance(notes, list):
        _issue(issues, "layer.data.notes", "must be an array")
        return
    seen: set[str] = set()
    for index, note in enumerate(notes):
        loc = f"layer.data.notes[{index}]"
        if not _check_keys(
            issues, note, loc,
            required=("id", "target_uri", "text", "actor", "created_at", "tags", "visibility", "ext"),
            allowed=("id", "target_uri", "text", "actor", "created_at", "tags", "visibility", "ext"),
        ):
            continue
        if not _is_id(note.get("id")) or note.get("id") in seen:
            _issue(issues, f"{loc}.id", "must be a unique portable identifier")
        else:
            seen.add(note["id"])
        _check_target_uri(issues, note.get("target_uri"), f"{loc}.target_uri")
        _check_actor(issues, note.get("actor"), f"{loc}.actor")
        if not _is_timestamp(note.get("created_at")):
            _issue(issues, f"{loc}.created_at", "must be an ISO 8601 UTC timestamp")
        if note.get("visibility") not in {"private", "project", "public"}:
            _issue(issues, f"{loc}.visibility", "must be private, project, or public")
        if not isinstance(note.get("tags"), list) or any(not _is_id(tag) for tag in note.get("tags", [])):
            _issue(issues, f"{loc}.tags", "must contain portable identifiers")
        _check_ext(issues, note.get("ext", {}), f"{loc}.ext")


def _validate_reprocessing_data(issues: list[Issue], data: Mapping[str, Any]) -> None:
    if set(data) - {"directives"}:
        _issue(issues, "layer.data", "reprocessing data may contain only directives")
    directives = data.get("directives")
    if not isinstance(directives, list):
        _issue(issues, "layer.data.directives", "must be an array")
        return
    seen: set[str] = set()
    for index, directive in enumerate(directives):
        loc = f"layer.data.directives[{index}]"
        if not _check_keys(
            issues, directive, loc,
            required=("id", "target_uri", "instruction", "reason", "requested_outputs", "engine_constraints", "priority", "status", "created_by", "created_at", "resolution", "ext"),
            allowed=("id", "target_uri", "instruction", "reason", "requested_outputs", "engine_constraints", "priority", "status", "created_by", "created_at", "resolution", "ext"),
        ):
            continue
        if not _is_id(directive.get("id")) or directive.get("id") in seen:
            _issue(issues, f"{loc}.id", "must be a unique portable identifier")
        else:
            seen.add(directive["id"])
        _check_target_uri(issues, directive.get("target_uri"), f"{loc}.target_uri")
        if not isinstance(directive.get("instruction"), str) or not directive["instruction"].strip():
            _issue(issues, f"{loc}.instruction", "must be non-empty text")
        if not isinstance(directive.get("requested_outputs"), list) or any(not _is_id(item) for item in directive.get("requested_outputs", [])):
            _issue(issues, f"{loc}.requested_outputs", "must contain portable output identifiers")
        if not isinstance(directive.get("engine_constraints"), dict):
            _issue(issues, f"{loc}.engine_constraints", "must be an object")
        if directive.get("priority") not in {"low", "normal", "high", "urgent"}:
            _issue(issues, f"{loc}.priority", "is not a controlled priority")
        if directive.get("status") not in {"open", "queued", "running", "resolved", "cancelled"}:
            _issue(issues, f"{loc}.status", "is not a controlled directive state")
        _check_actor(issues, directive.get("created_by"), f"{loc}.created_by")
        if not _is_timestamp(directive.get("created_at")):
            _issue(issues, f"{loc}.created_at", "must be an ISO 8601 UTC timestamp")
        if directive.get("status") == "resolved" and not isinstance(directive.get("resolution"), dict):
            _issue(issues, f"{loc}.resolution", "resolved directives must describe their resolution")
        _check_ext(issues, directive.get("ext", {}), f"{loc}.ext")


def _semantic_archive_issues(manifest: Mapping[str, Any], members: Mapping[str, bytes]) -> list[Issue]:
    issues: list[Issue] = []
    snapshot_nodes: dict[str, dict[str, set[str]]] = {}
    for snapshot_descriptor in manifest.get("authority_snapshots", []):
        if not isinstance(snapshot_descriptor, dict):
            continue
        snapshot_id = snapshot_descriptor.get("id")
        member = snapshot_descriptor.get("member")
        if not isinstance(snapshot_id, str) or not isinstance(member, str) or member not in members:
            continue
        try:
            snapshot = _strict_json(members[member], member)
        except WhledError as exc:
            _issue(issues, f"archive:{member}", str(exc))
            continue
        if not isinstance(snapshot, dict):
            _issue(issues, f"archive:{member}", "authority snapshot root must be an object")
            continue
        if snapshot.get("schema") != "world-herb-library/plant-authority-snapshot/0.1":
            _issue(issues, f"archive:{member}.schema", "is not a supported plant authority snapshot")
        if snapshot.get("database_id") != snapshot_descriptor.get("database_id"):
            _issue(issues, f"archive:{member}.database_id", "does not match the manifest snapshot descriptor")
        if snapshot.get("release") != snapshot_descriptor.get("release"):
            _issue(issues, f"archive:{member}.release", "does not match the manifest snapshot descriptor")
        if snapshot.get("created_at") != snapshot_descriptor.get("created_at"):
            _issue(issues, f"archive:{member}.created_at", "does not match the manifest snapshot descriptor")
        records = snapshot.get("records")
        if not isinstance(records, dict):
            _issue(issues, f"archive:{member}.records", "must be an object")
            continue
        mapping = {
            "name": "names",
            "concept": "concepts",
            "referent": "referents",
            "assertion": "assertions",
            "evidence": "evidence",
            "review": "reviews",
        }
        snapshot_nodes[snapshot_id] = {}
        for node_type, record_key in mapping.items():
            values = records.get(record_key)
            if not isinstance(values, list):
                _issue(issues, f"archive:{member}.records.{record_key}", "must be an array")
                snapshot_nodes[snapshot_id][node_type] = set()
            else:
                snapshot_nodes[snapshot_id][node_type] = {
                    record.get("id") for record in values if isinstance(record, dict) and _is_id(record.get("id"))
                }
        content = {"records": records, "entities": snapshot.get("entities")}
        try:
            content_digest = _sha256(json.dumps(
                content, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8"))
        except (TypeError, ValueError):
            content_digest = ""
        if snapshot.get("content_sha256") != content_digest:
            _issue(issues, f"archive:{member}.content_sha256", "does not match snapshot records and entities")

    layers: dict[tuple[str, str], dict[str, Any]] = {}
    descriptors: dict[tuple[str, str], Mapping[str, Any]] = {}
    for descriptor in manifest.get("layers", []):
        if not isinstance(descriptor, dict):
            continue
        key = (descriptor.get("id"), descriptor.get("revision"))
        descriptors[key] = descriptor
        member = descriptor.get("member")
        if not isinstance(member, str) or member not in members:
            continue
        try:
            layer = _strict_json(members[member], member)
        except WhledError as exc:
            _issue(issues, f"archive:{member}", str(exc))
            continue
        layer_issues = validate_layer(layer, manifest, descriptor)
        issues.extend(Issue(item.level, f"{member}:{item.location}", item.message) for item in layer_issues)
        if isinstance(layer, dict):
            layers[key] = layer

    region_index: set[tuple[str, str, str]] = set()
    passage_index: dict[tuple[str, str, str], str] = {}
    for (layer_id, revision), layer in layers.items():
        data = layer.get("data", {})
        if layer.get("kind") == "region":
            for region in data.get("regions", []):
                if isinstance(region, dict):
                    region_index.add((layer_id, revision, region.get("id")))
        elif layer.get("kind") in {"transcription", "translation"}:
            for passage in data.get("passages", []):
                if isinstance(passage, dict):
                    passage_index[(layer_id, revision, passage.get("id"))] = passage.get("text", "")

    def check_region_ref(ref: Any, location: str) -> None:
        if isinstance(ref, dict):
            key = (ref.get("layer_id"), ref.get("revision"), ref.get("region_id"))
            if key not in region_index:
                _issue(issues, location, "references a missing region in a pinned layer revision")

    for key, layer in layers.items():
        kind = layer.get("kind")
        data = layer.get("data", {})
        if kind in {"transcription", "translation"}:
            for index, passage in enumerate(data.get("passages", [])):
                if isinstance(passage, dict):
                    check_region_ref(passage.get("region_ref"), f"layer {key}.passages[{index}].region_ref")
        elif kind == "entity":
            for index, mention in enumerate(data.get("mentions", [])):
                if not isinstance(mention, dict):
                    continue
                check_region_ref(mention.get("region_ref"), f"layer {key}.mentions[{index}].region_ref")
                anchor = mention.get("text_anchor")
                if isinstance(anchor, dict):
                    passage_key = (anchor.get("layer_id"), anchor.get("revision"), anchor.get("passage_id"))
                    text = passage_index.get(passage_key)
                    if text is None:
                        _issue(issues, f"layer {key}.mentions[{index}].text_anchor", "references a missing passage")
                    else:
                        start, end = anchor.get("start"), anchor.get("end")
                        if isinstance(start, int) and isinstance(end, int) and 0 <= start <= end <= len(text):
                            if text[start:end] != anchor.get("exact"):
                                _issue(issues, f"layer {key}.mentions[{index}].text_anchor.exact", "does not match the pinned passage revision")

    snapshot_ids = {
        snapshot.get("id")
        for snapshot in manifest.get("authority_snapshots", [])
        if isinstance(snapshot, dict)
    }
    database_ids = {
        authority.get("database_id")
        for authority in manifest.get("external_authorities", [])
        if isinstance(authority, dict)
    }
    for key, layer in layers.items():
        if layer.get("kind") != "entity":
            continue
        for index, mention in enumerate(layer.get("data", {}).get("mentions", [])):
            if not isinstance(mention, dict):
                continue
            for ref_index, ref in enumerate(mention.get("authority_refs", [])):
                if not isinstance(ref, dict):
                    continue
                loc = f"layer {key}.mentions[{index}].authority_refs[{ref_index}]"
                if ref.get("snapshot_id") not in snapshot_ids:
                    _issue(issues, loc, "references a snapshot not sealed into this package")
                if ref.get("database_id") not in database_ids:
                    _issue(issues, loc, "references an undeclared external authority database")
                snapshot_index = snapshot_nodes.get(ref.get("snapshot_id"), {})
                node_ids = snapshot_index.get(ref.get("node_type"), set())
                if ref.get("node_id") not in node_ids:
                    _issue(issues, loc, "references a node absent from the cited authority snapshot")
                assertion_ids = snapshot_index.get("assertion", set())
                for assertion_id in ref.get("assertion_ids", []):
                    if assertion_id not in assertion_ids:
                        _issue(issues, loc, f"references missing authority assertion {assertion_id!r}")
    return issues


def _parse_checksums(payload: bytes) -> dict[str, str]:
    try:
        text = payload.decode("ascii")
    except UnicodeDecodeError as exc:
        raise WhledError("checksums.sha256 must be ASCII") from exc
    checksums: dict[str, str] = {}
    for number, line in enumerate(text.splitlines(), start=1):
        if not line:
            continue
        match = re.fullmatch(r"([0-9a-f]{64})  (.+)", line)
        if not match:
            raise WhledError(f"checksums.sha256:{number}: invalid checksum line")
        digest, member = match.groups()
        if member == "checksums.sha256" or not _safe_member(member) or member in checksums:
            raise WhledError(f"checksums.sha256:{number}: invalid or duplicate member")
        checksums[member] = digest
    return checksums


def _read_zip(path_or_bytes: str | os.PathLike[str] | bytes | bytearray | BinaryIO) -> tuple[dict[str, bytes], list[Issue]]:
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
            return {}, [Issue("error", "archive", f"archive exceeds {MAX_ARCHIVE_BYTES} bytes")]
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
                _issue(issues, "archive", f"contains more than {MAX_MEMBERS} members")
                return {}, issues
            seen: set[str] = set()
            seen_casefold: set[str] = set()
            total = 0
            for info in infos:
                name = info.filename
                if info.is_dir():
                    _issue(issues, f"archive:{name}", "directory entries are forbidden")
                    continue
                if not _safe_member(name) or not _allowed_member(name):
                    _issue(issues, f"archive:{name}", "unsafe or unsupported member name")
                if _is_database_member(name):
                    _issue(issues, f"archive:{name}", "mutable authority databases must never be embedded")
                if name in seen or name.casefold() in seen_casefold:
                    _issue(issues, f"archive:{name}", "duplicate or case-colliding member")
                seen.add(name)
                seen_casefold.add(name.casefold())
                if info.flag_bits & 0x1:
                    _issue(issues, f"archive:{name}", "encrypted members are forbidden")
                if _is_symlink(info):
                    _issue(issues, f"archive:{name}", "symbolic-link members are forbidden")
                if info.file_size > MAX_MEMBER_BYTES:
                    _issue(issues, f"archive:{name}", f"member exceeds {MAX_MEMBER_BYTES} bytes")
                total += info.file_size
                if info.file_size > 1_000_000 and info.compress_size > 0 and info.file_size / info.compress_size > MAX_COMPRESSION_RATIO:
                    _issue(issues, f"archive:{name}", "suspicious compression ratio")
            if total > MAX_INFLATED_BYTES:
                _issue(issues, "archive", f"inflated content exceeds {MAX_INFLATED_BYTES} bytes")
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


def validate_archive(path_or_bytes: str | os.PathLike[str] | bytes | bytearray | BinaryIO) -> list[Issue]:
    """Validate ZIP safety, checksums, schemas, manifest, layers, and links."""

    members, issues = _read_zip(path_or_bytes)
    if issues:
        return issues
    for required in sorted(_REQUIRED_MEMBERS - set(members)):
        _issue(issues, "archive", f"missing required member {required}")
    if issues:
        return issues
    try:
        checksums = _parse_checksums(members["checksums.sha256"])
    except WhledError as exc:
        return [Issue("error", "checksums.sha256", str(exc))]
    expected_members = set(members) - {"checksums.sha256"}
    if set(checksums) != expected_members:
        for member in sorted(expected_members - set(checksums)):
            _issue(issues, f"archive:{member}", "is missing from checksums.sha256")
        for member in sorted(set(checksums) - expected_members):
            _issue(issues, f"checksums.sha256:{member}", "names a missing archive member")
    for member in sorted(expected_members & set(checksums)):
        if _sha256(members[member]) != checksums[member]:
            _issue(issues, f"archive:{member}", "checksum mismatch")
    try:
        manifest = _strict_json(members["manifest.json"], "manifest.json")
    except WhledError as exc:
        _issue(issues, "manifest.json", str(exc))
        return issues
    if not isinstance(manifest, dict):
        _issue(issues, "manifest.json", "root must be an object")
        return issues
    issues.extend(validate_manifest(manifest, members))
    issues.extend(_semantic_archive_issues(manifest, members))
    for member in ("schemas/whled-manifest.schema.json", "schemas/whled-layer.schema.json"):
        try:
            schema = _strict_json(members[member], member)
            if not isinstance(schema, dict) or schema.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
                _issue(issues, f"archive:{member}", "must be a JSON Schema draft 2020-12 object")
        except WhledError as exc:
            _issue(issues, f"archive:{member}", str(exc))
    return issues


def read_archive(path_or_bytes: str | os.PathLike[str] | bytes | bytearray | BinaryIO) -> WhledDocument:
    """Read a valid package or raise :class:`WhledError`."""

    issues = validate_archive(path_or_bytes)
    errors = [issue for issue in issues if issue.level == "error"]
    if errors:
        summary = "; ".join(f"{item.location}: {item.message}" for item in errors[:8])
        raise WhledError(summary)
    members, read_issues = _read_zip(path_or_bytes)
    if read_issues:
        raise WhledError(read_issues[0].message)
    manifest = _strict_json(members["manifest.json"], "manifest.json")
    return WhledDocument(manifest=manifest, members=members)


def _schema_payload(filename: str) -> bytes:
    path = Path(__file__).resolve().parents[2] / "schemas" / filename
    try:
        return _canonical_json(_strict_json(path.read_bytes(), str(path)))
    except OSError as exc:
        raise WhledError(f"cannot load bundled schema {path}: {exc}") from exc


def render_instructions(manifest: Mapping[str, Any]) -> bytes:
    title = str(manifest.get("catalog", {}).get("title", "Untitled living edition"))
    edition = manifest.get("edition", {})
    text = f"""# Instructions for this `.whled` living edition

This is **{title}**, edition `{edition.get('id', '')}` at revision
`{edition.get('revision', '')}`. It follows `{FORMAT}`. The package is a sealed,
checksummed ZIP projection for reading, comparison, exchange, and review. It is
not the mutable workbench database and it is not a `.lib` file.

## Invariants

* Treat `canvases/` as immutable evidence. A changed image receives a new
  canvas revision; it never silently replaces the bytes cited by a layer.
* A layer identity names a continuing editorial strand. Its revision names an
  immutable state. Preserve older layer members and revision-pinned
  dependencies so comparisons and citations remain reproducible.
* Keep machine drafts, human readings, translations, entity mentions,
  commentary, notes, and reprocessing instructions in separate layers. Never
  overwrite a transcription with a modernization or interpretation.
* Regions use normalized canvas coordinates. Boxes and polygons pin the exact
  canvas revision. Region classifications may include multiple facets, such as
  `layout:marginalia` and `hand:hand-b`; definitions and parentage live in the
  manifest's `region_types` hierarchy.
* New layer, selector, resource, relation, and capability identifiers must be
  namespaced and declared in `manifest.registries`. Preserve unknown declared
  extensions. A custom selector must retain its core box/polygon fallback.
* Notes target stable `whl://` addresses. Entity links target stable
  `whl-entity://` addresses. Do not use list positions, filenames, or character
  offsets alone as identity.
* Entity mentions retain three anchors: page region, character offsets in a
  pinned transcription revision, and exact text with surrounding context.
  When they disagree, mark the anchor stale or ambiguous; never guess.
* Only a named human can approve scholarly content. New processing adds a new
  machine layer/revision and never overwrites an approved human layer.
* The plant authority's mutable SQLite database is external and MUST NOT be
  placed in this ZIP. `authority-snapshots/*.json` are immutable, versioned
  exports only. Preserve competing and rejected assertions.

## Integrity and safe editing

Every member except `checksums.sha256` is listed in that file. Every data
resource is also declared with its media type, byte length, and digest in
`manifest.json`. Rebuild with the reference sealer after an edit; do not patch
checksums by hand. Member paths are portable ASCII paths and may not contain
absolute paths, `..`, links, encryption, credentials, or local filesystem
locators.

Generic structural schemas are under `schemas/`. The normative semantics,
including dependency, selector, approval, and cross-layer anchor checks, are
implemented by `tools/living_edition/whled.py` and documented in
`docs/living-edition-format.md`.
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


def seal_archive(
    manifest: Mapping[str, Any],
    payloads: Mapping[str, bytes | bytearray | str | os.PathLike[str]],
    destination: str | os.PathLike[str] | BinaryIO,
) -> dict[str, Any]:
    """Seal a deterministic archive and return its finalized manifest.

    ``manifest.resources`` supplies media types and roles. Its ``sha256`` and
    ``bytes`` fields may be placeholders; the sealer always derives them from
    ``payloads``. No undeclared payload is accepted.
    """

    final_manifest = copy.deepcopy(dict(manifest))
    resources = final_manifest.get("resources")
    if not isinstance(resources, list):
        raise WhledError("manifest.resources must be an array before sealing")
    declared = {
        resource.get("member")
        for resource in resources
        if isinstance(resource, dict) and isinstance(resource.get("member"), str)
    }
    if declared != set(payloads):
        missing = sorted(declared - set(payloads))
        extra = sorted(set(payloads) - declared)
        raise WhledError(f"payload inventory mismatch; missing={missing}, undeclared={extra}")
    member_payloads: dict[str, bytes] = {}
    for resource in resources:
        if not isinstance(resource, dict) or not isinstance(resource.get("member"), str):
            continue
        member = resource["member"]
        if not _safe_member(member) or not _allowed_member(member) or _is_database_member(member):
            raise WhledError(f"unsafe or forbidden resource member {member!r}")
        try:
            payload = _coerce_payload(payloads[member])
        except OSError as exc:
            raise WhledError(f"cannot read payload for {member}: {exc}") from exc
        if len(payload) > MAX_MEMBER_BYTES:
            raise WhledError(f"{member} exceeds {MAX_MEMBER_BYTES} bytes")
        resource["bytes"] = len(payload)
        resource["sha256"] = _sha256(payload)
        resource.setdefault("ext", {})
        member_payloads[member] = payload
    final_manifest["resources"] = sorted(resources, key=lambda item: item.get("member", ""))

    generated: dict[str, bytes] = {
        "manifest.json": _canonical_json(final_manifest),
        "INSTRUCTIONS.md": render_instructions(final_manifest),
        "schemas/whled-manifest.schema.json": _schema_payload("whled-manifest.schema.json"),
        "schemas/whled-layer.schema.json": _schema_payload("whled-layer.schema.json"),
        **member_payloads,
    }
    issues = validate_manifest(final_manifest, {**generated, "checksums.sha256": b""})
    issues.extend(_semantic_archive_issues(final_manifest, generated))
    errors = [issue for issue in issues if issue.level == "error"]
    if errors:
        summary = "; ".join(f"{item.location}: {item.message}" for item in errors[:12])
        raise WhledError(f"refusing to seal invalid package: {summary}")
    checksum_payload = "".join(
        f"{_sha256(payload)}  {member}\n"
        for member, payload in sorted(generated.items())
    ).encode("ascii")
    generated["checksums.sha256"] = checksum_payload

    close_target = False
    temporary_path: Path | None = None
    final_path: Path | None = None
    if hasattr(destination, "write"):
        target = destination  # type: ignore[assignment]
    else:
        final_path = Path(destination)
        final_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=f".{final_path.name}.", suffix=".tmp", dir=final_path.parent)
        os.close(fd)
        temporary_path = Path(temp_name)
        target = temporary_path.open("w+b")
        close_target = True
    try:
        with zipfile.ZipFile(target, "w", allowZip64=True) as archive:
            for member in sorted(generated):
                payload = generated[member]
                media_type = next(
                    (resource.get("media_type") for resource in resources if resource.get("member") == member),
                    "application/json" if member.endswith(".json") else "text/plain",
                )
                compressed = media_type.startswith("text/") or media_type in {"application/json", "application/xml"}
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
    """Load a strict UTF-8 JSON manifest for the CLI."""

    value = _strict_json(Path(path).read_bytes(), str(path))
    if not isinstance(value, dict):
        raise WhledError("manifest root must be an object")
    return value


__all__ = [
    "FORMAT",
    "Issue",
    "WhledDocument",
    "WhledError",
    "load_manifest",
    "read_archive",
    "render_instructions",
    "seal_archive",
    "validate_archive",
    "validate_layer",
    "validate_manifest",
]

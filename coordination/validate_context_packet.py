#!/usr/bin/env python3
"""Fail-closed validation for Studio context profiles, packets, and receipts."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence


MANIFEST_MAX_BYTES = 65_536
AUDIENCES = ("implementer", "integrator", "reviewer")
SOURCE_DEFINITIONS = (
    (
        "handoff-protocol",
        "docs/living-edition-concurrent-session-handoff.md",
        "text/markdown; charset=utf-8",
    ),
    (
        "production-spec",
        "docs/living-edition-production-build-spec.md",
        "text/markdown; charset=utf-8",
    ),
)
EXPANSION_IDS = (
    "contract-detail",
    "fixture-evidence",
    "security-rights-recovery",
    "quality-scale-a11y",
    "integration-scenarios",
    "legacy-migration",
)
PROFILE_MATRIX = {
    "B00": (
        "studio-adoption-v1.1.1",
        ("not-applicable", "contract tag does not exist"),
        ("not-applicable", "fixture tag does not exist"),
        49_152,
        73_728,
    ),
    "C00": (
        "studio-bootstrap-v1.0.0",
        ("producer", "C00 produces the contract tag"),
        ("not-applicable", "fixture tag does not exist"),
        135_168,
        163_840,
    ),
    "T01": (
        "studio-contracts-v1.0.0",
        ("required", None),
        ("producer", "T01 produces the fixture tag"),
        98_304,
        131_072,
    ),
    "E10": (
        "studio-fixtures-v1.0.0",
        ("required", None),
        ("required", None),
        180_224,
        229_376,
    ),
    "D20": (
        "studio-fixtures-v1.0.0",
        ("required", None),
        ("required", None),
        98_304,
        131_072,
    ),
    "U20": (
        "studio-fixtures-v1.0.0",
        ("required", None),
        ("required", None),
        65_536,
        98_304,
    ),
    "E11-E21": (
        "studio-engine-foundation-v1.0.0",
        ("required", None),
        ("required", None),
        131_072,
        163_840,
    ),
    "U21-U27": (
        "studio-renderer-foundation-v1.0.0",
        ("required", None),
        ("required", None),
        65_536,
        98_304,
    ),
    "I30": (
        "studio-composition-input-v1.0.0",
        ("required", None),
        ("required", None),
        114_688,
        163_840,
    ),
}


class Invalid(ValueError):
    """A deterministic validation failure."""


def fail(message: str) -> None:
    raise Invalid(message)


def _object_no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            fail(f"duplicate JSON object key {key!r}")
        result[key] = value
    return result


def _parse_int(token: str) -> int:
    value = int(token)
    try:
        binary64 = float(value)
    except OverflowError:
        fail(f"integer is outside the finite IEEE-754 binary64 range: {token}")
    if not math.isfinite(binary64) or int(binary64) != value:
        fail(f"integer is not exactly expressible as IEEE-754 binary64: {token}")
    return value


def _parse_float(token: str) -> float:
    value = float(token)
    if not math.isfinite(value):
        fail(f"non-finite JSON number: {token}")
    return value


def _parse_constant(token: str) -> None:
    fail(f"non-finite JSON value: {token}")


def _reject_surrogates(value: Any, where: str = "$") -> None:
    if isinstance(value, str):
        if any(0xD800 <= ord(char) <= 0xDFFF for char in value):
            fail(f"Unicode surrogate is not valid I-JSON at {where}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_surrogates(item, f"{where}[{index}]")
    elif isinstance(value, dict):
        for key, item in value.items():
            _reject_surrogates(key, f"{where}.<key>")
            _reject_surrogates(item, f"{where}.{key}")


def load_json_bytes(data: bytes, label: str) -> Any:
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        fail(f"{label}: not strict UTF-8 ({exc})")
    try:
        value = json.loads(
            text,
            object_pairs_hook=_object_no_duplicates,
            parse_int=_parse_int,
            parse_float=_parse_float,
            parse_constant=_parse_constant,
        )
    except Invalid:
        raise
    except (json.JSONDecodeError, ValueError) as exc:
        fail(f"{label}: invalid JSON ({exc})")
    _reject_surrogates(value)
    return value


def load_json_file(path: Path, label: str) -> tuple[Any, bytes]:
    try:
        data = path.read_bytes()
    except OSError as exc:
        fail(f"{label}: cannot read {path} ({exc})")
    return load_json_bytes(data, label), data


NODE_JCS = r"""
'use strict';
const fs = require('fs');
const root = JSON.parse(fs.readFileSync(0, 'utf8'));
function canonical(value) {
  if (value === null || typeof value === 'boolean' ||
      typeof value === 'number' || typeof value === 'string') {
    if (typeof value === 'number' && !Number.isFinite(value)) {
      throw new Error('non-finite number');
    }
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) {
    return '[' + value.map(canonical).join(',') + ']';
  }
  const keys = Object.keys(value).sort();
  return '{' + keys.map(k => JSON.stringify(k) + ':' + canonical(value[k])).join(',') + '}';
}
process.stdout.write(canonical(root));
"""


def _check_jcs_value(value: Any, where: str = "$") -> None:
    if value is None or isinstance(value, (bool, str)):
        return
    if isinstance(value, int):
        try:
            binary64 = float(value)
        except OverflowError:
            fail(f"JCS integer is outside finite IEEE-754 binary64 at {where}")
        if not math.isfinite(binary64) or int(binary64) != value:
            fail(f"JCS integer is not exactly expressible as binary64 at {where}")
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            fail(f"non-finite JCS number at {where}")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _check_jcs_value(item, f"{where}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                fail(f"non-string JCS object key at {where}")
            _check_jcs_value(item, f"{where}.{key}")
        return
    fail(f"unsupported JCS value at {where}: {type(value).__name__}")


def jcs(value: Any) -> bytes:
    """RFC 8785 using ECMAScript serialization and UTF-16 property sorting."""
    _reject_surrogates(value)
    _check_jcs_value(value)
    transport = json.dumps(
        value, ensure_ascii=True, allow_nan=False, separators=(",", ":")
    ).encode("ascii")
    try:
        process = subprocess.run(
            ["node", "-e", NODE_JCS],
            input=transport,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except FileNotFoundError:
        fail("RFC 8785 canonicalization requires local Node.js")
    if process.returncode != 0:
        detail = process.stderr.decode("utf-8", "replace").strip()
        fail(f"RFC 8785 canonicalization failed: {detail}")
    return process.stdout


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_path(parts: Iterable[Any]) -> str:
    result = "$"
    for part in parts:
        if isinstance(part, int):
            result += f"[{part}]"
        else:
            result += f".{part}"
    return result


def validate_schema_instance(
    schema: dict[str, Any], instance: Any, label: str, definition: str | None = None
) -> None:
    try:
        from jsonschema import Draft202012Validator, FormatChecker
        from jsonschema.exceptions import SchemaError
    except ImportError:
        fail("Draft 2020-12 validation requires the Python jsonschema package")
    if schema.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
        fail("schema does not declare JSON Schema Draft 2020-12")
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        fail(f"context schema is invalid: {exc.message}")
    target: dict[str, Any]
    if definition is None:
        target = schema
    else:
        if definition not in schema.get("$defs", {}):
            fail(f"context schema has no $defs/{definition}")
        target = {
            "$schema": schema["$schema"],
            "$defs": schema["$defs"],
            "$ref": f"#/$defs/{definition}",
        }
    errors = sorted(
        Draft202012Validator(target, format_checker=FormatChecker()).iter_errors(instance),
        key=lambda error: _json_path(error.absolute_path),
    )
    if errors:
        error = errors[0]
        fail(f"{label}: schema violation at {_json_path(error.absolute_path)}: {error.message}")


def _expanded_profile_id(profile_id: str) -> list[str]:
    match = re.fullmatch(r"([A-Z])(\d+)-([A-Z])(\d+)", profile_id)
    if not match:
        return [profile_id]
    left_letter, left_number, right_letter, right_number = match.groups()
    if left_letter != right_letter or len(left_number) != len(right_number):
        fail(f"profile ID has a non-expandable range: {profile_id}")
    start, end = int(left_number), int(right_number)
    if start > end:
        fail(f"profile ID has a descending range: {profile_id}")
    return [f"{left_letter}{number:0{len(left_number)}d}" for number in range(start, end + 1)]


def _all_manifest_templates(manifest: dict[str, Any]) -> Iterable[tuple[str, dict[str, Any]]]:
    for template in manifest["universal_templates"]:
        yield "universal_templates", template
    for audience in AUDIENCES:
        for template in manifest["audience_templates"][audience]:
            yield f"audience_templates.{audience}", template
    for profile in manifest["profiles"]:
        for template in profile["templates"]:
            yield f"profiles.{profile['profile_id']}.templates", template
        for package in profile["package_templates"]:
            for template in package["templates"]:
                yield f"package_templates.{package['work_package']}", template


@dataclass(frozen=True)
class Catalog:
    manifest: dict[str, Any]
    profiles: dict[str, dict[str, Any]]
    sources: dict[str, dict[str, Any]]
    expansions: tuple[str, ...]


def validate_profile_manifest(
    schema: dict[str, Any], manifest: dict[str, Any]
) -> Catalog:
    validate_schema_instance(schema, manifest, "profile manifest", "profileManifest")
    expected_profile_ids = tuple(
        schema["$defs"]["profile"]["properties"]["profile_id"]["enum"]
    )
    profile_ids = tuple(profile["profile_id"] for profile in manifest["profiles"])
    if profile_ids != expected_profile_ids:
        fail(
            "profile IDs/order differ from the schema enum: "
            f"expected {list(expected_profile_ids)}, got {list(profile_ids)}"
        )

    source_tuples = tuple(
        (source["source_id"], source["path"], source["media_type"])
        for source in manifest["template_sources"]
    )
    if source_tuples != SOURCE_DEFINITIONS:
        fail("template source IDs, paths, media types, or order are not the exact protocol set")
    source_ids = [item[0] for item in source_tuples]
    if len(source_ids) != len(set(source_ids)):
        fail("duplicate template source_id")
    sources = {
        source["source_id"]: source for source in manifest["template_sources"]
    }

    expansion_ids = tuple(
        expansion["expansion_id"] for expansion in manifest["expansion_catalog"]
    )
    if expansion_ids != EXPANSION_IDS:
        fail("expansion IDs/order are not the exact protocol expansion catalog")

    all_packages: list[str] = []
    all_context_ids: dict[str, str] = {}
    used_sources: set[str] = set()
    for profile in manifest["profiles"]:
        profile_id = profile["profile_id"]
        actual_matrix = (
            profile["required_base"],
            (
                profile["contract_pin"]["expectation"],
                profile["contract_pin"]["not_applicable_reason"],
            ),
            (
                profile["fixture_pin"]["expectation"],
                profile["fixture_pin"]["not_applicable_reason"],
            ),
            profile["default_utf8_bytes"],
            profile["hard_max_utf8_bytes"],
        )
        if actual_matrix != PROFILE_MATRIX[profile_id]:
            fail(f"profile {profile_id} base/phase/budget matrix differs from the frozen protocol")
        expected_packages = _expanded_profile_id(profile_id)
        if profile["work_packages"] != expected_packages:
            fail(
                f"profile {profile_id} work_packages must be exactly {expected_packages}"
            )
        package_template_ids = [
            package["work_package"] for package in profile["package_templates"]
        ]
        if package_template_ids != expected_packages:
            fail(
                f"profile {profile_id} package_templates must exactly match work_packages in order"
            )
        all_packages.extend(profile["work_packages"])
        if profile["default_utf8_bytes"] > profile["hard_max_utf8_bytes"]:
            fail(f"profile {profile_id} default exceeds hard maximum")
        allowed = profile["allowed_expansion_ids"]
        if allowed != [item for item in EXPANSION_IDS if item in allowed]:
            fail(f"profile {profile_id} allowed expansions are not in catalog order")
        route_ids = [
            route["expansion_id"]
            for route in profile["required_initial_expansion_routes"]
        ]
        if len(route_ids) != len(set(route_ids)):
            fail(f"profile {profile_id} repeats a required initial expansion route")
        if route_ids != [item for item in allowed if item in route_ids]:
            fail(f"profile {profile_id} required initial routes are not in allowed order")
        for route in profile["required_initial_expansion_routes"]:
            phase = route["phase_pin"]
            expected_expansion = (
                "contract-detail" if phase == "contract" else "fixture-evidence"
            )
            if route["expansion_id"] != expected_expansion:
                fail(f"profile {profile_id} has mismatched {phase} expansion route")
            if profile[f"{phase}_pin"]["expectation"] != "required":
                fail(f"profile {profile_id} routes a non-required {phase} pin")
        for phase, expansion in (
            ("contract", "contract-detail"),
            ("fixture", "fixture-evidence"),
        ):
            present = any(
                route["phase_pin"] == phase
                for route in profile["required_initial_expansion_routes"]
            )
            if present != (
                profile[f"{phase}_pin"]["expectation"] == "required"
            ):
                fail(f"profile {profile_id} {phase} expectation/route mismatch")

    expected_packages = [
        package
        for profile_id in expected_profile_ids
        for package in _expanded_profile_id(profile_id)
    ]
    if all_packages != expected_packages or len(all_packages) != len(set(all_packages)):
        fail("work-package coverage is not exact and unique")

    for scope, template in _all_manifest_templates(manifest):
        context_id = template["context_id"]
        if context_id in all_context_ids:
            fail(
                f"duplicate context_id {context_id!r} in {scope}; first in {all_context_ids[context_id]}"
            )
        all_context_ids[context_id] = scope
        source_id = template["source_id"]
        if source_id not in sources:
            fail(f"unknown source_id {source_id!r} in {scope}.{context_id}")
        used_sources.add(source_id)
        expected_capsule = (
            "universal"
            if scope.startswith("universal_templates")
            or scope.startswith("audience_templates")
            else "package"
        )
        if template["capsule"] != expected_capsule:
            fail(f"{scope}.{context_id} has wrong capsule")
        if template["selector"]["kind"] == "whole-git-blob/1":
            fail(f"core template {context_id} may not ingest a whole normative document")
    if used_sources != set(sources):
        fail("template source definitions must each be used")
    if set().union(
        *(set(profile["allowed_expansion_ids"]) for profile in manifest["profiles"])
    ) != set(EXPANSION_IDS):
        fail("expansion catalog contains an expansion unused by every profile")
    return Catalog(
        manifest=manifest,
        profiles={profile["profile_id"]: profile for profile in manifest["profiles"]},
        sources=sources,
        expansions=expansion_ids,
    )


@dataclass(frozen=True)
class RawLine:
    start: int
    body_end: int
    end: int


def _raw_lines(blob: bytes) -> list[RawLine]:
    lines: list[RawLine] = []
    start = 0
    while start < len(blob):
        cr = blob.find(b"\r", start)
        lf = blob.find(b"\n", start)
        positions = [position for position in (cr, lf) if position >= 0]
        if not positions:
            lines.append(RawLine(start, len(blob), len(blob)))
            break
        body_end = min(positions)
        if blob[body_end : body_end + 2] == b"\r\n":
            end = body_end + 2
        else:
            end = body_end + 1
        lines.append(RawLine(start, body_end, end))
        start = end
    return lines


def _decode_utf8(blob: bytes, label: str) -> str:
    try:
        return blob.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        fail(f"{label}: source is not strict UTF-8 ({exc})")


def _line_body(blob: bytes, line: RawLine) -> str:
    return blob[line.start : line.body_end].decode("utf-8", errors="strict")


def _opening_fence(body: str) -> tuple[str, int] | None:
    match = re.match(r"^ {0,3}(`{3,}|~{3,})", body)
    if not match:
        return None
    run = match.group(1)
    return run[0], len(run)


def _closing_fence(body: str, char: str, count: int) -> bool:
    return re.fullmatch(rf" {{0,3}}{re.escape(char)}{{{count},}}[ \t]*", body) is not None


def _atx_heading(body: str) -> tuple[int, str] | None:
    match = re.match(r"^ {0,3}(#{1,6})(.*)$", body)
    if not match:
        return None
    rest = match.group(2)
    if rest and rest[0] not in " \t":
        return None
    text = rest.strip(" \t")
    closing = re.fullmatch(r"(.*?)[ \t]+#+", text)
    if closing:
        text = closing.group(1).rstrip(" \t")
    return len(match.group(1)), text


def _headings(blob: bytes) -> list[tuple[int, int, int, str]]:
    result: list[tuple[int, int, int, str]] = []
    fence: tuple[str, int] | None = None
    for line in _raw_lines(blob):
        body = _line_body(blob, line)
        if fence is not None:
            if _closing_fence(body, *fence):
                fence = None
            continue
        opening = _opening_fence(body)
        if opening is not None:
            fence = opening
            continue
        heading = _atx_heading(body)
        if heading is not None:
            result.append((line.start, line.end, heading[0], heading[1]))
    return result


def select_heading(blob: bytes, selector: dict[str, Any]) -> bytes:
    wanted = (
        selector["heading_level"],
        selector["heading_text"],
    )
    occurrence = selector["occurrence"]
    matches = [
        index
        for index, heading in enumerate(_headings(blob))
        if (heading[2], heading[3]) == wanted
    ]
    if len(matches) < occurrence:
        fail(
            f"heading selector resolved {len(matches)} occurrence(s), needs {occurrence}: "
            f"level {wanted[0]} {wanted[1]!r}"
        )
    headings = _headings(blob)
    selected_index = matches[occurrence - 1]
    selected = headings[selected_index]
    end = len(blob)
    for later in headings[selected_index + 1 :]:
        if later[2] <= selected[2]:
            end = later[0]
            break
    return blob[selected[0] : end]


def _outside_fence_flags(blob: bytes, lines: Sequence[RawLine]) -> list[bool]:
    flags: list[bool] = []
    fence: tuple[str, int] | None = None
    for line in lines:
        body = _line_body(blob, line)
        if fence is not None:
            flags.append(False)
            if _closing_fence(body, *fence):
                fence = None
            continue
        opening = _opening_fence(body)
        if opening is not None:
            flags.append(False)
            fence = opening
        else:
            flags.append(True)
    return flags


def _pipe_cells(body: str) -> list[str] | None:
    if not body.startswith("|") or not body.endswith("|"):
        return None
    return [cell.strip(" \t") for cell in body[1:-1].split("|")]


def select_table_row(blob: bytes, selector: dict[str, Any]) -> bytes:
    anchor = dict(selector["containing_heading"])
    anchor["kind"] = "markdown-heading-section/1"
    section = select_heading(blob, anchor)
    lines = _raw_lines(section)
    flags = _outside_fence_flags(section, lines)
    wanted_columns = selector["table_columns"]
    candidates: list[int] = []
    for index in range(len(lines) - 1):
        if not (flags[index] and flags[index + 1]):
            continue
        header = _pipe_cells(_line_body(section, lines[index]))
        delimiter = _pipe_cells(_line_body(section, lines[index + 1]))
        if header != wanted_columns or delimiter is None:
            continue
        if len(delimiter) != len(header):
            continue
        if not all(re.fullmatch(r":?-{3,}:?", cell) for cell in delimiter):
            continue
        candidates.append(index)
    occurrence = selector["table_occurrence"]
    if len(candidates) < occurrence:
        fail(
            f"table selector resolved {len(candidates)} matching table(s), needs {occurrence}"
        )
    header_index = candidates[occurrence - 1]
    key_index = wanted_columns.index(selector["key_column"])
    matches: list[int] = []
    index = header_index + 2
    while index < len(lines) and flags[index]:
        body = _line_body(section, lines[index])
        cells = _pipe_cells(body)
        if cells is None:
            break
        if len(cells) != len(wanted_columns):
            fail("selected table contains an embedded/escaped pipe or wrong-width row")
        if cells[key_index] == selector["key_value"]:
            matches.append(index)
        index += 1
    if len(matches) != 1:
        fail(f"table row key resolved {len(matches)} rows; exactly one is required")
    row = lines[matches[0]]
    header = lines[header_index]
    delimiter = lines[header_index + 1]
    return (
        section[header.start : header.end]
        + section[delimiter.start : delimiter.end]
        + section[row.start : row.end]
    )


def json_pointer(value: Any, pointer: str, label: str) -> Any:
    if pointer == "":
        return value
    current = value
    for raw_token in pointer[1:].split("/"):
        token = raw_token.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict):
            if token not in current:
                fail(f"{label}: object key {token!r} does not exist")
            current = current[token]
        elif isinstance(current, list):
            if re.fullmatch(r"0|[1-9][0-9]*", token) is None:
                fail(f"{label}: invalid RFC 6901 array index {token!r}")
            index = int(token)
            if index >= len(current):
                fail(f"{label}: array index {index} is out of bounds")
            current = current[index]
        else:
            fail(f"{label}: cannot traverse scalar at token {token!r}")
    return current


def select_json(blob: bytes, selector: dict[str, Any], label: str) -> bytes:
    document = load_json_bytes(blob, label)
    selected = json_pointer(document, selector["pointer"], f"{label} pointer")
    for pointer, expected in selector.get("expected_identity", {}).items():
        actual = json_pointer(selected, pointer, f"{label} expected_identity {pointer!r}")
        if jcs(actual) != jcs(expected):
            fail(f"{label}: expected_identity mismatch at {pointer!r}")
    return jcs(selected)


def resolve_selector(
    blob: bytes, selector: dict[str, Any], media_type: str, label: str
) -> bytes:
    _decode_utf8(blob, label)
    kind = selector["kind"]
    if kind == "whole-git-blob/1":
        return blob
    if kind == "markdown-heading-section/1":
        return select_heading(blob, selector)
    if kind == "markdown-table-row/1":
        return select_table_row(blob, selector)
    if kind == "rfc6901-json-pointer/1":
        return select_json(blob, selector, label)
    fail(f"{label}: unsupported selector kind {kind!r}")


class GitRepo:
    def __init__(self, path: Path):
        self.path = path.resolve()
        result = self._run(["rev-parse", "--is-inside-work-tree"], check=False)
        if result.returncode != 0 or result.stdout.strip() != b"true":
            fail(f"not a Git worktree: {self.path}")
        self._commit_cache: set[str] = set()
        self._blob_cache: dict[tuple[str, str], tuple[str, bytes]] = {}
        self._remote_tag_cache: dict[tuple[str, str], tuple[str, str]] = {}

    def _run(self, arguments: Sequence[str], check: bool = True) -> subprocess.CompletedProcess[bytes]:
        process = subprocess.run(
            ["git", "-C", str(self.path), *arguments],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if check and process.returncode != 0:
            detail = process.stderr.decode("utf-8", "replace").strip()
            fail(f"git {' '.join(arguments)} failed: {detail}")
        return process

    def verify_commit(self, commit: str, label: str) -> None:
        if commit in self._commit_cache:
            return
        process = self._run(["cat-file", "-t", commit])
        if process.stdout.strip() != b"commit":
            fail(f"{label}: {commit} is not a commit object")
        self._commit_cache.add(commit)

    def raw_blob(self, commit: str, path: str, expected_oid: str, label: str) -> bytes:
        oid, data = self._read_blob_with_oid(commit, path, label)
        if oid != expected_oid:
            fail(f"{label}: Git blob OID mismatch (expected {expected_oid}, got {oid})")
        return data

    def _read_blob_with_oid(self, commit: str, path: str, label: str) -> tuple[str, bytes]:
        self.verify_commit(commit, label)
        key = (commit, path)
        if key not in self._blob_cache:
            spec = f"{commit}:{path}"
            oid = self._run(["rev-parse", "--verify", spec]).stdout.decode("ascii").strip()
            kind = self._run(["cat-file", "-t", oid]).stdout.strip()
            if kind != b"blob":
                fail(f"{label}: {spec} is not a blob")
            data = self._run(["cat-file", "blob", spec]).stdout
            self._blob_cache[key] = (oid, data)
        oid, data = self._blob_cache[key]
        return oid, data

    def read_blob(self, commit: str, path: str, label: str) -> bytes:
        """Read an unpinned profile source from one explicit immutable commit."""
        return self._read_blob_with_oid(commit, path, label)[1]

    def git_blob_pin(self, pin: dict[str, Any], label: str) -> bytes:
        data = self.raw_blob(pin["commit"], pin["path"], pin["git_blob_oid"], label)
        actual = sha256(data)
        if actual != pin["sha256"]:
            fail(f"{label}: whole-blob SHA-256 mismatch (expected {pin['sha256']}, got {actual})")
        return data

    def packet_pin(self, pin: dict[str, Any], label: str) -> tuple[dict[str, Any], bytes]:
        data = self.raw_blob(pin["commit"], pin["path"], pin["git_blob_oid"], label)
        value = load_json_bytes(data, label)
        digest = sha256(jcs(value))
        if digest != pin["sha256"]:
            fail(f"{label}: RFC 8785 JCS SHA-256 mismatch")
        if value.get("packet_id") != pin["packet_id"] or value.get("revision") != pin["revision"]:
            fail(f"{label}: packet_id/revision do not match pinned packet bytes")
        return value, data

    def verify_tag_tuple(
        self, pin: dict[str, Any], label: str, remote_url: str | None = None
    ) -> None:
        if pin["tag_object"] != pin["remote_tag_object"]:
            fail(f"{label}: local and remote annotated tag objects differ")
        ref = f"refs/tags/{pin['tag']}"
        actual = self._run(["rev-parse", "--verify", ref]).stdout.decode("ascii").strip()
        if actual != pin["tag_object"]:
            fail(f"{label}: annotated tag object mismatch")
        if self._run(["cat-file", "-t", actual]).stdout.strip() != b"tag":
            fail(f"{label}: tag is not annotated")
        commit = self._run(["rev-parse", "--verify", f"{actual}^{{commit}}"])
        if commit.stdout.decode("ascii").strip() != pin["commit"]:
            fail(f"{label}: peeled commit mismatch")
        tree = self._run(["rev-parse", "--verify", f"{pin['commit']}^{{tree}}"])
        if tree.stdout.decode("ascii").strip() != pin["tree"]:
            fail(f"{label}: tree mismatch")
        if remote_url is not None:
            remote_tag, remote_commit = self.remote_tag(remote_url, pin["tag"], label)
            if remote_tag != pin["remote_tag_object"]:
                fail(f"{label}: live remote annotated tag object mismatch")
            if remote_commit != pin["commit"]:
                fail(f"{label}: live remote peeled commit mismatch")

    def remote_tag(self, remote_url: str, tag: str, label: str) -> tuple[str, str]:
        key = (remote_url, tag)
        if key in self._remote_tag_cache:
            return self._remote_tag_cache[key]
        direct_ref = f"refs/tags/{tag}"
        peeled_ref = f"{direct_ref}^{{}}"
        process = self._run(
            ["ls-remote", "--tags", remote_url, direct_ref, peeled_ref], check=False
        )
        if process.returncode != 0:
            detail = process.stderr.decode("utf-8", "replace").strip()
            fail(f"{label}: live remote tag query failed: {detail}")
        found: dict[str, str] = {}
        for raw_line in process.stdout.decode("ascii", "strict").splitlines():
            fields = raw_line.split("\t")
            if len(fields) != 2 or fields[1] in found:
                fail(f"{label}: ambiguous live remote tag response")
            found[fields[1]] = fields[0]
        if set(found) != {direct_ref, peeled_ref}:
            fail(f"{label}: live remote annotated tag or peeled ref is missing")
        self._remote_tag_cache[key] = (found[direct_ref], found[peeled_ref])
        return self._remote_tag_cache[key]

    def verify_remote_coordination_ref(
        self, remote_url: str, ref: str, ledger_commit: str
    ) -> str:
        process = self._run(["ls-remote", remote_url, ref], check=False)
        if process.returncode != 0:
            detail = process.stderr.decode("utf-8", "replace").strip()
            fail(f"coordination ref: live remote query failed: {detail}")
        lines = process.stdout.decode("ascii", "strict").splitlines()
        if len(lines) != 1:
            fail("coordination ref: live remote ref is missing or ambiguous")
        fields = lines[0].split("\t")
        if len(fields) != 2 or fields[1] != ref or re.fullmatch(r"[0-9a-f]{40}", fields[0]) is None:
            fail("coordination ref: malformed live remote response")
        remote_head = fields[0]
        self.verify_commit(remote_head, "coordination remote head")
        ancestor = self._run(
            ["merge-base", "--is-ancestor", ledger_commit, remote_head], check=False
        )
        if ancestor.returncode != 0:
            fail("coordination ledger commit is not an ancestor of the live remote head")
        return remote_head


def _core_templates(
    catalog: Catalog, profile: dict[str, Any], audience: str, work_package: str
) -> list[dict[str, Any]]:
    package = next(
        item for item in profile["package_templates"] if item["work_package"] == work_package
    )
    return [
        *catalog.manifest["universal_templates"],
        *catalog.manifest["audience_templates"][audience],
        *profile["templates"],
        *package["templates"],
    ]


def run_profiles(
    schema: dict[str, Any],
    catalog: Catalog,
    source_root: Path,
    source_commit: str,
) -> list[str]:
    repo = GitRepo(source_root)
    repo.verify_commit(source_commit, "profile source commit")
    source_blobs: dict[str, bytes] = {}
    for source_id, source in catalog.sources.items():
        blob = repo.read_blob(source_commit, source["path"], f"template source {source_id}")
        _decode_utf8(blob, source["path"])
        source_blobs[source_id] = blob
    lines: list[str] = []
    for profile in catalog.manifest["profiles"]:
        for work_package in profile["work_packages"]:
            total = 0
            for template in _core_templates(catalog, profile, "implementer", work_package):
                source = catalog.sources[template["source_id"]]
                selected = resolve_selector(
                    source_blobs[template["source_id"]],
                    template["selector"],
                    source["media_type"],
                    f"{profile['profile_id']}/{work_package}/{template['context_id']}",
                )
                total += len(selected)
            default = profile["default_utf8_bytes"]
            hard = profile["hard_max_utf8_bytes"]
            lines.append(
                f"{profile['profile_id']}/{work_package} core_selected={total} "
                f"default={default} hard={hard}"
            )
            if total > default:
                fail(
                    f"{profile['profile_id']}/{work_package} implementer core is {total} bytes, "
                    f"over default {default}"
                )
    return lines


def _pin_equal(actual: dict[str, Any], expected: dict[str, Any], label: str) -> None:
    if actual != expected:
        fail(f"{label}: source pin differs from the authoritative protocol pin")


def _phase_status(expectation: str) -> str:
    return "required" if expectation == "required" else "not-applicable"


def _load_and_verify_phase(
    repo: GitRepo,
    pin: dict[str, Any],
    expectation: str,
    label: str,
    remote_url: str,
    not_applicable_reason: str | None,
) -> dict[str, Any] | None:
    expected_status = _phase_status(expectation)
    if pin["status"] != expected_status:
        fail(f"{label}: status {pin['status']!r} differs from profile expectation {expectation!r}")
    if pin["status"] == "not-applicable":
        if not_applicable_reason is None or pin["reason"] != not_applicable_reason:
            fail(f"{label}: reason differs from the frozen phase matrix")
        return None
    repo.verify_tag_tuple(pin, label, remote_url)
    if pin["lock"]["commit"] != pin["commit"]:
        fail(f"{label}: lock containing commit differs from peeled phase commit")
    data = repo.git_blob_pin(pin["lock"], f"{label} lock")
    lock = load_json_bytes(data, f"{label} lock")
    if not isinstance(lock, dict):
        fail(f"{label}: lock must be a JSON object")
    return lock


def _require_record_array(ledger: dict[str, Any], key: str) -> list[dict[str, Any]]:
    value = ledger.get(key)
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        fail(f"coordination ledger has no supported top-level {key!r} record array")
    return value


def _one_record(
    records: list[dict[str, Any]], key: str, value: Any, label: str
) -> dict[str, Any]:
    matches = [record for record in records if record.get(key) == value]
    if len(matches) != 1:
        fail(f"coordination ledger must contain exactly one {label} matching {key}={value!r}")
    return matches[0]


def _ledger_value(record: dict[str, Any], key: str, expected: Any, label: str) -> None:
    if key not in record:
        fail(f"coordination ledger session omits required {label} field {key!r}")
    if record[key] != expected:
        fail(f"coordination ledger session {label} field {key!r} differs from packet")


def _validate_ledger_phase(
    session: dict[str, Any], field: str, packet_pin: dict[str, Any]
) -> None:
    ledger_pin = session.get(field)
    if not isinstance(ledger_pin, dict):
        fail(f"coordination ledger session omits {field}")
    if ledger_pin.get("status") != packet_pin["status"]:
        fail(f"coordination ledger {field} status differs from packet")
    if packet_pin["status"] == "not-applicable":
        if ledger_pin.get("reason") != packet_pin["reason"]:
            fail(f"coordination ledger {field} reason differs from packet")
        return
    expected = {
        "tag": packet_pin["tag"],
        "tag_object": packet_pin["tag_object"],
        "remote_tag_object": packet_pin["remote_tag_object"],
        "commit": packet_pin["commit"],
        "tree": packet_pin["tree"],
        "verification_status": packet_pin["verification_status"],
        "lock_sha256": packet_pin["lock"]["sha256"],
    }
    aliases = {
        "remote_tag_object": ("remote_tag_object", "remote_tag"),
        "verification_status": ("verification_status",),
        "lock_sha256": ("lock_sha256",),
    }
    for key, value in expected.items():
        candidates = aliases.get(key, (key,))
        present = [candidate for candidate in candidates if candidate in ledger_pin]
        if present and any(ledger_pin[candidate] != value for candidate in present):
            fail(f"coordination ledger {field} field {present[0]!r} differs from packet")
        if key in ("tag", "tag_object", "lock_sha256") and not present:
            fail(f"coordination ledger {field} omits required field {key!r}")


def _ledger_session_access_mode(
    session: dict[str, Any], label: str
) -> dict[str, Any]:
    validation = session.get("validation")
    if not isinstance(validation, list):
        fail(f"{label} validation must be an array")
    matches = [
        entry
        for entry in validation
        if isinstance(entry, dict)
        and entry.get("schema") == "studio-context-access-mode/1"
    ]
    if len(matches) != 1:
        fail(f"{label} must contain exactly one access-mode validation entry")
    entry = matches[0]
    required = {
        "schema",
        "session_id",
        "work_package",
        "assignment_receipt_id",
        "lease_id",
        "access_mode",
    }
    if set(entry) != required:
        fail(f"{label} access-mode validation has the wrong closed shape")
    for field in ("session_id", "work_package", "lease_id"):
        if entry[field] != session.get(field):
            fail(f"{label} access-mode validation differs on {field!r}")
    if entry["access_mode"] not in ("write-lease", "read-only-review"):
        fail(f"{label} access-mode validation has an unsupported mode")
    if not isinstance(entry["assignment_receipt_id"], str) or not entry[
        "assignment_receipt_id"
    ]:
        fail(f"{label} access-mode validation has no assignment receipt ID")
    return entry


def _validate_ledger_access_mode(
    session: dict[str, Any], assignment: dict[str, Any]
) -> None:
    entry = _ledger_session_access_mode(session, "coordination ledger session")
    expected = {
        "schema": "studio-context-access-mode/1",
        "session_id": assignment["session_id"],
        "work_package": assignment["work_package"],
        "assignment_receipt_id": assignment["assignment_receipt_id"],
        "lease_id": assignment["lease_id"],
        "access_mode": assignment["access_mode"],
    }
    if entry != expected:
        fail("coordination ledger access-mode validation differs from packet assignment")


def _validate_reviewer_ledger_overlap(
    sessions: list[dict[str, Any]], packet: dict[str, Any]
) -> None:
    if packet["audience"] != "reviewer":
        return
    assignment = packet["assignment"]
    review = packet["review_of"]
    implementation = _one_record(
        sessions,
        "session_id",
        review["implementation_session_id"],
        "reviewed implementation session",
    )
    if implementation.get("lease_id") != review["implementation_lease_id"]:
        fail("reviewed implementation ledger lease differs from review_of")
    if implementation.get("lease_entries") != review["lease"]:
        fail("reviewed implementation ledger scope differs from review_of")
    if (implementation.get("status"), implementation.get("lease_state")) != (
        "review",
        "active",
    ):
        fail("reviewed implementation session/lease must be review+active")
    implementation_mode = _ledger_session_access_mode(
        implementation, "reviewed implementation session"
    )
    if implementation_mode["access_mode"] != "write-lease":
        fail("reviewed implementation ledger session is not write-lease")
    reviewer_scope = assignment["lease_entries"]
    if any(
        not _lease_entry_is_contained(entry, review["lease"])
        for entry in reviewer_scope
    ):
        fail("reviewer ledger scope is not contained by the reviewed write lease")
    for other in sessions:
        if other.get("session_id") in {
            assignment["session_id"],
            review["implementation_session_id"],
        }:
            continue
        if other.get("lease_state") != "active":
            continue
        mode = _ledger_session_access_mode(
            other, f"active session {other.get('session_id')!r}"
        )
        if mode["access_mode"] != "write-lease":
            continue
        other_entries = other.get("lease_entries")
        if not isinstance(other_entries, list) or not other_entries:
            fail("active write-lease session has no lease entries")
        if _lease_sets_overlap(reviewer_scope, other_entries):
            fail("reviewer observation scope overlaps an unrelated active write lease")


def validate_ledger_binding(
    ledger: dict[str, Any], packet: dict[str, Any], *, require_active: bool = False
) -> None:
    coordination = packet["pins"]["coordination"]
    assignment = packet["assignment"]
    if ledger.get("coordination_ref") != coordination["ref"]:
        fail("coordination ledger coordination_ref differs from packet pin")
    if "authoritative_remote_url" in ledger and ledger["authoritative_remote_url"] != assignment["authoritative_remote_url"]:
        fail("coordination ledger authoritative remote differs from packet")
    receipts = _require_record_array(ledger, "receipts")
    protection = _one_record(
        receipts,
        "receipt_id",
        coordination["protection_receipt_id"],
        "protection receipt",
    )
    if protection.get("kind") != "coordination-ref-protection":
        fail("pinned protection receipt has the wrong kind")

    sessions = _require_record_array(ledger, "sessions")
    session = _one_record(
        sessions, "session_id", assignment["session_id"], "session"
    )
    for key in (
        "session_id",
        "work_package",
        "accountable_owner",
        "lease_id",
        "branch",
        "worktree",
        "authoritative_remote_url",
        "lease_entries",
        "reserved_ids",
    ):
        _ledger_value(session, key, assignment[key], "assignment")
    _ledger_value(session, "coordination_ref", coordination["ref"], "coordination")
    if "assignment_receipt_id" in session:
        _ledger_value(
            session,
            "assignment_receipt_id",
            assignment["assignment_receipt_id"],
            "assignment",
        )
    base = packet["pins"]["base"]
    for key, expected in (
        ("base_tag", base["tag"]),
        ("base_tag_object", base["tag_object"]),
        ("remote_base_tag_object", base["remote_tag_object"]),
        ("base_commit", base["commit"]),
        ("base_tree", base["tree"]),
        ("baseline_verification_status", base["verification_status"]),
    ):
        _ledger_value(session, key, expected, "base")
    _validate_ledger_phase(session, "contract_pin", packet["pins"]["contract"])
    _validate_ledger_phase(session, "fixture_pin", packet["pins"]["fixture"])
    _validate_ledger_access_mode(session, assignment)
    _validate_reviewer_ledger_overlap(sessions, packet)
    state = (session.get("status"), session.get("lease_state"))
    allowed_states = {("active", "active")} if require_active else {
        ("ready", "issued"),
        ("active", "active"),
    }
    if state not in allowed_states:
        expected = "active+active" if require_active else "ready+issued or active+active"
        fail(f"coordination ledger session/lease state must be {expected}")


def _lock_members(lock: dict[str, Any], phase: str) -> dict[str, dict[str, Any]]:
    key = "contracts" if phase == "contract" else "fixtures"
    records = lock.get(key)
    if not isinstance(records, list):
        fail(
            f"unsupported-{phase}-lock: expected top-level {key!r} array for source membership"
        )
    members: dict[str, dict[str, Any]] = {}
    path_keys = ("source_path", "path", "relative_source_path")
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            fail(f"unsupported-{phase}-lock: {key}[{index}] is not an object")
        present = [name for name in path_keys if isinstance(record.get(name), str)]
        if len(present) != 1 or not isinstance(record.get("sha256"), str):
            fail(
                f"unsupported-{phase}-lock: {key}[{index}] lacks one path field and sha256"
            )
        path = record[present[0]]
        if path in members:
            fail(f"{phase} lock repeats source path {path!r}")
        members[path] = record
    return members


def _route_entries(
    schema: dict[str, Any], lock: dict[str, Any], pointer: str, label: str
) -> list[dict[str, Any]]:
    routes = lock.get("context_routes")
    validate_schema_instance(schema, routes, f"{label} context_routes", "contextRoutes")
    value = json_pointer(lock, pointer, f"{label} route")
    if not isinstance(value, list):
        fail(f"{label}: route pointer must resolve to an array")
    seen: set[str] = set()
    for entry in value:
        validate_schema_instance(schema, entry, f"{label} route entry", "contextRouteEntry")
        if entry["context_id"] in seen:
            fail(f"{label}: route repeats context_id {entry['context_id']!r}")
        seen.add(entry["context_id"])
    return value


def _expected_route_templates(
    schema: dict[str, Any],
    profile: dict[str, Any],
    work_package: str,
    locks: dict[str, dict[str, Any] | None],
) -> list[tuple[str, str, dict[str, Any], dict[str, Any]]]:
    result: list[tuple[str, str, dict[str, Any], dict[str, Any]]] = []
    for route in profile["required_initial_expansion_routes"]:
        phase = route["phase_pin"]
        lock = locks[phase]
        if lock is None:
            fail(f"required expansion {route['expansion_id']} has no required {phase} pin")
        pointer = route["route_pointer_template"].replace("{work_package}", work_package)
        members = _lock_members(lock, phase)
        for entry in _route_entries(schema, lock, pointer, f"{phase}/{work_package}"):
            member = members.get(entry["source_path"])
            if member is None:
                fail(
                    f"{phase}/{work_package}: routed source {entry['source_path']!r} is not a lock member"
                )
            result.append((route["expansion_id"], phase, entry, member))
    return result


def _validate_reviewed_pin_metadata(
    packet: dict[str, Any], pin: dict[str, Any], label: str
) -> None:
    if packet["packet_id"] != pin["packet_id"] or packet["revision"] != pin["revision"]:
        fail(f"{label}: reviewed pin metadata mismatch")


def _validate_packet_pin_path(pin: dict[str, Any], label: str) -> None:
    expected = f"coordination/context-packets/{pin['packet_id']}.json"
    if pin["path"] != expected:
        fail(f"{label}: packet path must be exactly {expected!r}")


def _validate_revision_chain_link(
    current: dict[str, Any], previous: dict[str, Any], previous_pin: dict[str, Any]
) -> None:
    if current["packet_id"] != previous["packet_id"]:
        fail("successor packet_id differs from previous revision")
    if current["revision"] != previous["revision"] + 1:
        fail("successor revision is not exactly previous revision + 1")
    if current["previous_revision"] != previous_pin:
        fail("successor previous_revision does not equal the resolved prior pin")
    for field in ("audience", "assignment", "profile", "pins", "review_of"):
        if current[field] != previous[field]:
            fail(f"successor changes immutable field {field!r}")
    old_context = previous["required_context"]
    if current["required_context"][: len(old_context)] != old_context:
        fail("successor required_context is not an exact append-only extension")
    old_ids = previous["activated_expansion_ids"]
    if current["activated_expansion_ids"][: len(old_ids)] != old_ids:
        fail("successor activated_expansion_ids is not an exact append-only extension")
    if len(current["required_context"]) == len(old_context):
        fail("successor revision adds no context entry")


def _resolve_packet_pin(
    repo: GitRepo, schema: dict[str, Any], pin: dict[str, Any], label: str
) -> dict[str, Any]:
    value, _ = repo.packet_pin(pin, label)
    validate_schema_instance(schema, value, label)
    return value


def _path_is_leased(path: str, lease_entries: list[dict[str, Any]]) -> bool:
    for lease in lease_entries:
        lease_path = lease["path"]
        if lease["kind"] == "exact-file" and path == lease_path:
            return True
        if lease["kind"] == "subtree" and (
            path == lease_path or path.startswith(f"{lease_path}/")
        ):
            return True
    return False


def _lease_entry_is_contained(
    candidate: dict[str, Any], owner_entries: list[dict[str, Any]]
) -> bool:
    candidate_path = candidate["path"]
    for owner in owner_entries:
        owner_path = owner["path"]
        if owner["kind"] == "exact-file":
            if candidate["kind"] == "exact-file" and candidate_path == owner_path:
                return True
            continue
        if candidate_path == owner_path or candidate_path.startswith(f"{owner_path}/"):
            return True
    return False


def _normalized_lease_entry(
    entry: dict[str, Any], label: str
) -> tuple[str, str]:
    if not isinstance(entry, dict) or set(entry) != {"kind", "path"}:
        fail(f"{label} has the wrong closed lease-entry shape")
    kind = entry["kind"]
    path = entry["path"]
    if kind not in ("exact-file", "subtree") or not isinstance(path, str):
        fail(f"{label} has an unsupported lease kind or path")
    if re.fullmatch(
        r"[A-Za-z0-9._@+\-]+(?:/[A-Za-z0-9._@+\-]+)*", path
    ) is None:
        fail(f"{label} has a noncanonical repository path")
    if any(segment in (".", "..") or segment.endswith(".") for segment in path.split("/")):
        fail(f"{label} has a forbidden repository path segment")
    return kind, path.lower()


def _lease_entries_overlap(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_kind, left_path = _normalized_lease_entry(left, "left lease entry")
    right_kind, right_path = _normalized_lease_entry(right, "right lease entry")
    if left_kind == "exact-file" and right_kind == "exact-file":
        return left_path == right_path
    if left_kind == "exact-file":
        return left_path == right_path or left_path.startswith(f"{right_path}/")
    if right_kind == "exact-file":
        return right_path == left_path or right_path.startswith(f"{left_path}/")
    return (
        left_path == right_path
        or left_path.startswith(f"{right_path}/")
        or right_path.startswith(f"{left_path}/")
    )


def _lease_sets_overlap(
    left: list[dict[str, Any]], right: list[dict[str, Any]]
) -> bool:
    return any(
        _lease_entries_overlap(left_entry, right_entry)
        for left_entry in left
        for right_entry in right
    )


def _changed_paths(
    repo: GitRepo, base_commit: str, handoff_head: str
) -> list[str]:
    repo.verify_commit(base_commit, "review base commit")
    repo.verify_commit(handoff_head, "review handoff head")
    ancestor = repo._run(
        ["merge-base", "--is-ancestor", base_commit, handoff_head], check=False
    )
    if ancestor.returncode != 0:
        fail("review handoff head does not descend from the pinned base")
    merges = repo._run(
        ["rev-list", "--merges", f"{base_commit}..{handoff_head}"], check=True
    )
    if merges.stdout.strip():
        fail("review handoff history contains a merge commit")
    raw = repo._run(
        ["diff", "--name-only", "-z", f"{base_commit}...{handoff_head}", "--"],
        check=True,
    ).stdout
    if not raw:
        return []
    fields = raw.split(b"\0")
    if fields[-1] != b"":
        fail("Git returned malformed NUL-delimited changed paths")
    try:
        return [field.decode("utf-8", errors="strict") for field in fields[:-1]]
    except UnicodeDecodeError as exc:
        fail(f"changed Git path is not strict UTF-8 ({exc})")


def _commit_list(
    repo: GitRepo, arguments: list[str], label: str
) -> list[str]:
    raw = repo._run(arguments, check=True).stdout
    try:
        lines = raw.decode("ascii", errors="strict").splitlines()
    except UnicodeDecodeError as exc:
        fail(f"{label}: Git returned a non-ASCII object ID ({exc})")
    if any(re.fullmatch(r"[0-9a-f]{40}", commit) is None for commit in lines):
        fail(f"{label}: Git returned a malformed commit list")
    return lines


def _validate_checks(
    checks: list[dict[str, Any]], commands: list[str], label: str
) -> bool:
    check_ids: set[str] = set()
    all_passed = True
    for check in checks:
        check_id = check["check_id"]
        if check_id in check_ids:
            fail(f"{label} repeats check_id {check_id!r}")
        check_ids.add(check_id)
        passed = check["exit_code"] == 0
        if passed != (check["result"] == "passed"):
            fail(f"{label} check {check_id!r} result contradicts exit_code")
        all_passed = all_passed and passed
    if [check["command"] for check in checks] != commands:
        fail(f"{label} check commands differ from assignment acceptance commands")
    return all_passed


def _outer_receipt(
    ledger: dict[str, Any], receipt_id: str, kind: str, label: str
) -> dict[str, Any]:
    record = _one_record(
        _require_record_array(ledger, "receipts"),
        "receipt_id",
        receipt_id,
        label,
    )
    if record.get("kind") != kind:
        fail(f"{label} has wrong kind")
    payload = record.get("payload")
    if not isinstance(payload, dict):
        fail(f"{label} has no object payload")
    return payload


def _validate_return_payload(
    repo: GitRepo,
    schema: dict[str, Any],
    payload: dict[str, Any],
    implementation_packet: dict[str, Any],
    label: str,
) -> None:
    validate_schema_instance(
        schema, payload, f"{label} payload", "contextPacketReturnReceipt"
    )
    assignment = implementation_packet["assignment"]
    expected = {
        "session_id": assignment["session_id"],
        "work_package": assignment["work_package"],
        "lease_id": assignment["lease_id"],
        "assignment_receipt_id": assignment["assignment_receipt_id"],
        "access_mode": "write-lease",
        "base_commit": implementation_packet["pins"]["base"]["commit"],
        "lease_entries": assignment["lease_entries"],
        "acceptance_commands": assignment["acceptance_commands"],
    }
    for field, value in expected.items():
        if payload[field] != value:
            fail(f"{label} field {field!r} differs from implementation packet")
    if implementation_packet["audience"] == "reviewer":
        fail(f"{label} cannot refer to a reviewer packet")
    if assignment.get("access_mode") != "write-lease":
        fail(f"{label} implementation assignment is not write-lease")
    _validate_packet_pin_path(payload["packet"], f"{label} packet")
    resolved = _resolve_packet_pin(repo, schema, payload["packet"], f"{label} packet")
    if jcs(resolved) != jcs(implementation_packet):
        fail(f"{label} resolves to a different implementation packet")

    actual_paths = _changed_paths(
        repo, payload["base_commit"], payload["handoff_head"]
    )
    if payload["changed_paths"] != actual_paths:
        fail(f"{label} changed_paths differ from the exact Git diff")
    if any(not _path_is_leased(path, payload["lease_entries"]) for path in actual_paths):
        fail(f"{label} contains a changed path outside the implementation lease")
    ordered_commits = _commit_list(
        repo,
        ["rev-list", "--reverse", f"{payload['base_commit']}..{payload['handoff_head']}"],
        f"{label} ordered commits",
    )
    if payload["ordered_commits"] != ordered_commits:
        fail(f"{label} ordered_commits differ from Git history")
    merge_commits = _commit_list(
        repo,
        ["rev-list", "--merges", f"{payload['base_commit']}..{payload['handoff_head']}"],
        f"{label} merge commits",
    )
    if payload["merge_commits"] != merge_commits:
        fail(f"{label} merge_commits differ from Git history")
    _validate_checks(payload["checks"], payload["acceptance_commands"], label)


def _resolve_return_receipt(
    repo: GitRepo,
    schema: dict[str, Any],
    ledger: dict[str, Any],
    receipt_id: str,
    implementation_packet: dict[str, Any],
    label: str,
) -> dict[str, Any]:
    payload = _outer_receipt(ledger, receipt_id, "context-packet-return", label)
    _validate_return_payload(repo, schema, payload, implementation_packet, label)
    return payload


def _validate_reviewer_binding(
    repo: GitRepo,
    schema: dict[str, Any],
    reviewer_packet: dict[str, Any],
    implementation_packet: dict[str, Any],
    ledger: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    review = reviewer_packet["review_of"]
    reviewer_assignment = reviewer_packet["assignment"]
    implementation_assignment = implementation_packet["assignment"]
    if implementation_packet["audience"] == "reviewer":
        fail("review_of implementation packet cannot itself be a reviewer packet")
    if implementation_assignment.get("access_mode") != "write-lease":
        fail("reviewed implementation packet must use write-lease access")
    if reviewer_assignment.get("access_mode") != "read-only-review":
        fail("reviewer packet must use read-only-review access")
    for field in ("session_id", "lease_id", "branch", "worktree"):
        if reviewer_assignment[field] == implementation_assignment[field]:
            fail(f"reviewer and implementation assignments share {field!r}")
    expected_fields = {
        "implementation_session_id": implementation_assignment["session_id"],
        "work_package": implementation_assignment["work_package"],
        "implementation_lease_id": implementation_assignment["lease_id"],
        "base_commit": implementation_packet["pins"]["base"]["commit"],
        "lease": implementation_assignment["lease_entries"],
        "acceptance_commands": implementation_assignment["acceptance_commands"],
    }
    for field, expected in expected_fields.items():
        if review.get(field) != expected:
            fail(f"review_of field {field!r} differs from implementation evidence")
    if reviewer_assignment["work_package"] != implementation_assignment["work_package"]:
        fail("reviewer and implementation packet work packages differ")
    if any(
        not _lease_entry_is_contained(entry, implementation_assignment["lease_entries"])
        for entry in reviewer_assignment["lease_entries"]
    ):
        fail("reviewer observation scope is not contained by the implementation lease")
    actual_paths = _changed_paths(
        repo, review["base_commit"], review["handoff_head"]
    )
    if review["changed_paths"] != actual_paths:
        fail("review_of changed_paths differ from the exact Git diff")
    if any(not _path_is_leased(path, review["lease"]) for path in actual_paths):
        fail("review_of changed path is outside the implementation lease")
    if any(
        not _path_is_leased(path, reviewer_assignment["lease_entries"])
        for path in actual_paths
    ):
        fail("review_of changed path is outside the reviewer observation scope")

    handoff_payload = _outer_receipt(
        ledger,
        review["handoff_receipt_id"],
        "context-packet-handoff",
        "review_of handoff receipt",
    )
    validate_schema_instance(
        schema,
        handoff_payload,
        "review_of handoff receipt payload",
        "contextPacketHandoffReceipt",
    )
    validate_receipt(schema, handoff_payload, repo)
    handoff_expected = {
        "session_id": implementation_assignment["session_id"],
        "work_package": implementation_assignment["work_package"],
        "lease_id": implementation_assignment["lease_id"],
        "assignment_receipt_id": implementation_assignment["assignment_receipt_id"],
        "packet": review["implementation_packet"],
        "handoff_head": review["handoff_head"],
        "return_receipt_id": review["return_receipt_id"],
    }
    for field, expected in handoff_expected.items():
        if handoff_payload[field] != expected:
            fail(f"handoff receipt field {field!r} differs from review_of evidence")
    return_payload = _resolve_return_receipt(
        repo,
        schema,
        ledger,
        review["return_receipt_id"],
        implementation_packet,
        "review_of return receipt",
    )
    historical_ledger = load_json_bytes(
        repo.git_blob_pin(
            handoff_payload["activation_ledger"],
            "review_of historical activation ledger",
        ),
        "review_of historical activation ledger",
    )
    if not isinstance(historical_ledger, dict):
        fail("review_of historical activation ledger root must be an object")
    historical_return = _resolve_return_receipt(
        repo,
        schema,
        historical_ledger,
        review["return_receipt_id"],
        implementation_packet,
        "review_of historical return receipt",
    )
    if return_payload != historical_return:
        fail("reviewer and historical ledgers contain different return receipts")
    if return_payload["packet"] != review["implementation_packet"]:
        fail("return receipt and review_of pin different implementation packets")
    if return_payload["handoff_head"] != review["handoff_head"]:
        fail("return receipt and review_of pin different handoff heads")
    if return_payload["changed_paths"] != review["changed_paths"]:
        fail("return receipt and review_of pin different changed paths")
    return handoff_payload, return_payload


def validate_packet(
    schema: dict[str, Any],
    schema_bytes: bytes,
    catalog: Catalog,
    profiles_bytes: bytes,
    packet: dict[str, Any],
    repo: GitRepo,
    external_sha256: str | None,
) -> tuple[str, int, list[dict[str, Any]], str]:
    validate_schema_instance(schema, packet, "packet")
    profile_id = packet["profile"]["profile_id"]
    profile = catalog.profiles[profile_id]
    work_package = packet["assignment"]["work_package"]
    expected_access_mode = (
        "read-only-review" if packet["audience"] == "reviewer" else "write-lease"
    )
    if packet["assignment"].get("access_mode") != expected_access_mode:
        fail(
            f"{packet['audience']} packet assignment must use access_mode "
            f"{expected_access_mode!r}"
        )
    if work_package not in profile["work_packages"]:
        fail(f"work package {work_package!r} is outside profile {profile_id}")

    manifest_blob = repo.git_blob_pin(packet["profile"]["manifest"], "profile manifest pin")
    pinned_manifest = load_json_bytes(manifest_blob, "pinned profile manifest")
    if pinned_manifest != catalog.manifest or manifest_blob != profiles_bytes:
        fail("--profiles bytes differ from the pinned profile manifest")
    manifest_digest = sha256(jcs(catalog.manifest))
    if packet["profile"]["manifest_jcs_sha256"] != manifest_digest:
        fail("profile manifest JCS SHA-256 mismatch")

    context_schema_pin = packet["pins"]["context_schema"]
    pinned_schema_bytes = repo.git_blob_pin(context_schema_pin["artifact"], "context schema pin")
    pinned_schema = load_json_bytes(pinned_schema_bytes, "pinned context schema")
    if pinned_schema != schema or pinned_schema_bytes != schema_bytes:
        fail("--schema bytes differ from the pinned context schema")

    coordination = packet["pins"]["coordination"]
    ledger_pin = {
        "path": coordination["ledger_path"],
        "commit": coordination["ledger_commit"],
        "git_blob_oid": coordination["ledger_git_blob_oid"],
        "digest_domain": coordination["digest_domain"],
        "sha256": coordination["sha256"],
    }
    ledger_bytes = repo.git_blob_pin(ledger_pin, "coordination ledger")
    ledger = load_json_bytes(ledger_bytes, "coordination ledger")
    if not isinstance(ledger, dict):
        fail("coordination ledger root must be an object")
    validate_ledger_binding(ledger, packet)
    remote_url = packet["assignment"]["authoritative_remote_url"]
    remote_coordination_head = repo.verify_remote_coordination_ref(
        remote_url, coordination["ref"], coordination["ledger_commit"]
    )

    base = packet["pins"]["base"]
    if base["tag"] != profile["required_base"]:
        fail("base tag differs from selected profile required_base")
    repo.verify_tag_tuple(base, "base pin", remote_url)

    protocol = packet["pins"]["protocol"]
    repo.verify_tag_tuple(protocol, "protocol pin", remote_url)
    if protocol["handoff"]["commit"] != protocol["commit"] or protocol["specification"]["commit"] != protocol["commit"]:
        fail("protocol document commits differ from the protocol tag commit")
    handoff_blob = repo.git_blob_pin(protocol["handoff"], "handoff protocol")
    specification_blob = repo.git_blob_pin(protocol["specification"], "production specification")
    _decode_utf8(handoff_blob, "handoff protocol")
    _decode_utf8(specification_blob, "production specification")

    locks = {
        "contract": _load_and_verify_phase(
            repo,
            packet["pins"]["contract"],
            profile["contract_pin"]["expectation"],
            "contract pin",
            remote_url,
            profile["contract_pin"]["not_applicable_reason"],
        ),
        "fixture": _load_and_verify_phase(
            repo,
            packet["pins"]["fixture"],
            profile["fixture_pin"]["expectation"],
            "fixture pin",
            remote_url,
            profile["fixture_pin"]["not_applicable_reason"],
        ),
    }

    previous: dict[str, Any] | None = None
    if packet["revision"] == 1:
        if packet["previous_revision"] is not None:
            fail("revision 1 must not pin a previous revision")
    else:
        previous_pin = packet["previous_revision"]
        _validate_packet_pin_path(previous_pin, "previous packet revision")
        previous = _resolve_packet_pin(repo, schema, previous_pin, "previous packet revision")
        _validate_revision_chain_link(packet, previous, previous_pin)
    if packet["review_of"] is not None:
        implementation_pin = packet["review_of"]["implementation_packet"]
        _validate_packet_pin_path(
            implementation_pin,
            "reviewed implementation packet",
        )
        implementation_packet = _resolve_packet_pin(
            repo, schema, implementation_pin, "reviewed implementation packet"
        )
        _validate_reviewer_binding(
            repo, schema, packet, implementation_packet, ledger
        )

    core = _core_templates(catalog, profile, packet["audience"], work_package)
    if len(packet["required_context"]) < len(core):
        fail("required_context omits core entries")
    source_protocol_pins = {
        "handoff-protocol": protocol["handoff"],
        "production-spec": protocol["specification"],
    }
    seen_ids: set[str] = set()
    for index, template in enumerate(core):
        entry = packet["required_context"][index]
        source = catalog.sources[template["source_id"]]
        for field in ("context_id", "capsule", "purpose", "selector"):
            if entry[field] != template[field]:
                fail(f"core context entry {index} field {field!r} differs from profile template")
        if entry["expansion_id"] is not None:
            fail(f"core context entry {entry['context_id']} must have null expansion_id")
        if entry["media_type"] != source["media_type"] or entry["source"]["path"] != source["path"]:
            fail(f"core context entry {entry['context_id']} has wrong source identity")
        _pin_equal(entry["source"], source_protocol_pins[template["source_id"]], entry["context_id"])

    required_routes = _expected_route_templates(schema, profile, work_package, locks)
    route_start = len(core)
    if len(packet["required_context"]) < route_start + len(required_routes):
        fail("required_context omits required initial expansion route entries")
    for offset, (expansion_id, phase, template, member) in enumerate(required_routes):
        entry = packet["required_context"][route_start + offset]
        expected = {
            "context_id": template["context_id"],
            "capsule": "expansion",
            "expansion_id": expansion_id,
            "purpose": template["purpose"],
            "media_type": template["media_type"],
            "selector": template["selector"],
        }
        for field, value in expected.items():
            if entry[field] != value:
                fail(f"required route entry {offset} field {field!r} differs from pinned lock route")
        if entry["source"]["path"] != template["source_path"]:
            fail("required route entry source path differs from pinned lock route")
        phase_pin = packet["pins"][phase]
        if entry["source"]["commit"] != phase_pin["commit"]:
            fail(f"{entry['context_id']}: source commit differs from {phase} peeled commit")
        if entry["source"]["sha256"] != member["sha256"]:
            fail(f"{entry['context_id']}: source digest differs from {phase} lock member")
        if isinstance(member.get("git_blob_oid"), str) and entry["source"]["git_blob_oid"] != member["git_blob_oid"]:
            fail(f"{entry['context_id']}: source blob OID differs from {phase} lock member")

    activated = packet["activated_expansion_ids"]
    allowed = profile["allowed_expansion_ids"]
    if any(item not in allowed for item in activated):
        fail("packet activates an expansion outside the profile")
    if activated != [item for item in allowed if item in activated]:
        fail("activated expansion IDs are not in profile order")
    required_ids = [route["expansion_id"] for route in profile["required_initial_expansion_routes"]]
    if activated[: len(required_ids)] != required_ids:
        fail("packet does not activate required initial expansion IDs in order")
    if packet["revision"] == 1:
        if activated != required_ids:
            fail("revision 1 may activate only required initial expansions")
        if len(packet["required_context"]) != len(core) + len(required_routes):
            fail("revision 1 context must be exactly core plus required routed entries")
    else:
        assert previous is not None
        old_ids = previous["activated_expansion_ids"]
        new_ids = activated[len(old_ids) :]
        new_entries = packet["required_context"][len(previous["required_context"]) :]
        for expansion_id in new_ids:
            if not any(entry["expansion_id"] == expansion_id for entry in new_entries):
                fail(
                    f"newly activated expansion {expansion_id!r} has no newly appended entry"
                )

    expansion_entries = packet["required_context"][len(core) :]
    expansion_counts = {expansion_id: 0 for expansion_id in activated}
    for entry in expansion_entries:
        if entry["capsule"] != "expansion" or entry["expansion_id"] not in activated:
            fail("all post-core entries must name an activated expansion")
        expansion_counts[entry["expansion_id"]] += 1
    if any(count == 0 for count in expansion_counts.values()):
        fail("each activated expansion must have at least one context entry")

    phase_members: dict[str, dict[str, dict[str, Any]]] = {}
    for phase in ("contract", "fixture"):
        if locks[phase] is not None:
            phase_members[phase] = _lock_members(locks[phase], phase)

    selected_total = 0
    emitted: list[dict[str, Any]] = []
    for index, entry in enumerate(packet["required_context"]):
        context_id = entry["context_id"]
        if context_id in seen_ids:
            fail(f"duplicate packet context_id {context_id!r}")
        seen_ids.add(context_id)
        if entry["repository_url"] != packet["assignment"]["authoritative_remote_url"]:
            fail(f"{context_id}: repository URL differs from assignment remote")
        expansion_phase = {
            "contract-detail": "contract",
            "fixture-evidence": "fixture",
        }.get(entry["expansion_id"])
        if expansion_phase is not None:
            phase_pin = packet["pins"][expansion_phase]
            if phase_pin["status"] != "required":
                fail(
                    f"{context_id}: {entry['expansion_id']} requires a pinned {expansion_phase} phase"
                )
            if entry["source"]["commit"] != phase_pin["commit"]:
                fail(
                    f"{context_id}: source commit differs from {expansion_phase} peeled commit"
                )
            member = phase_members[expansion_phase].get(entry["source"]["path"])
            if member is None:
                fail(
                    f"{context_id}: source path is not present in the pinned {expansion_phase} lock"
                )
            if entry["source"]["sha256"] != member["sha256"]:
                fail(
                    f"{context_id}: source SHA-256 differs from the pinned {expansion_phase} lock"
                )
            if (
                isinstance(member.get("git_blob_oid"), str)
                and entry["source"]["git_blob_oid"] != member["git_blob_oid"]
            ):
                fail(
                    f"{context_id}: source blob OID differs from the pinned {expansion_phase} lock"
                )
        blob = repo.git_blob_pin(entry["source"], f"context {context_id}")
        selected = resolve_selector(blob, entry["selector"], entry["media_type"], f"context {context_id}")
        if len(selected) != entry["selection"]["selected_utf8_bytes"]:
            fail(f"{context_id}: selected UTF-8 byte length mismatch")
        if sha256(selected) != entry["selection"]["sha256"]:
            fail(f"{context_id}: selected SHA-256 mismatch")
        selected_total += len(selected)
        emitted.append(
            {
                "context_id": context_id,
                "purpose": entry["purpose"],
                "media_type": entry["media_type"],
                "selected_utf8_bytes": len(selected),
                "text": selected.decode("utf-8", errors="strict"),
            }
        )

    budget = packet["budget"]
    if budget["default_utf8_bytes"] != profile["default_utf8_bytes"]:
        fail("packet default budget differs from profile")
    if budget["hard_max_utf8_bytes"] != profile["hard_max_utf8_bytes"]:
        fail("packet hard maximum differs from profile")
    if budget["selected_utf8_bytes"] != selected_total:
        fail("packet selected budget differs from measured selected bytes")
    if not (selected_total <= budget["authorized_utf8_bytes"] <= profile["hard_max_utf8_bytes"]):
        fail("budget relation selected <= authorized <= hard maximum is false")
    if packet["revision"] == 1 and (
        selected_total > profile["default_utf8_bytes"]
        or budget["authorized_utf8_bytes"] != profile["default_utf8_bytes"]
    ):
        fail("revision 1 must fit the default and authorize exactly the default budget")
    above_default = max(selected_total, budget["authorized_utf8_bytes"]) > profile["default_utf8_bytes"]
    if above_default != (budget["above_default_reason"] is not None):
        fail("above_default_reason presence does not match above-default authorization/use")

    canonical = jcs(packet)
    if len(canonical) != budget["manifest_jcs_bytes"]:
        fail("manifest_jcs_bytes differs from measured packet JCS bytes")
    if len(canonical) > budget["manifest_hard_max_bytes"] or len(canonical) > MANIFEST_MAX_BYTES:
        fail("packet JCS form exceeds manifest hard maximum")
    digest = sha256(canonical)
    if external_sha256 is not None and digest != external_sha256:
        fail(f"external packet JCS SHA-256 mismatch (expected {external_sha256}, got {digest})")
    return digest, selected_total, emitted, remote_coordination_head


def _same_pin(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return left == right


def _validate_chain(
    repo: GitRepo, schema: dict[str, Any], chain: list[dict[str, Any]], label: str
) -> list[dict[str, Any]]:
    packets: list[dict[str, Any]] = []
    packet_id: str | None = None
    for index, pin in enumerate(chain):
        _validate_packet_pin_path(pin, f"{label}[{index}]")
        packet = _resolve_packet_pin(repo, schema, pin, f"{label}[{index}]")
        if pin["revision"] != index + 1:
            fail(f"{label}: revisions must be consecutive from 1")
        if packet_id is None:
            packet_id = pin["packet_id"]
        elif pin["packet_id"] != packet_id:
            fail(f"{label}: packet IDs differ")
        if index == 0:
            if packet["previous_revision"] is not None:
                fail(f"{label}: revision 1 has a previous_revision")
        else:
            _validate_revision_chain_link(packet, packets[-1], chain[index - 1])
        packets.append(packet)
    return packets


def _packet_ledger(
    repo: GitRepo, packet: dict[str, Any], label: str
) -> dict[str, Any]:
    coordination = packet["pins"]["coordination"]
    pin = {
        "path": coordination["ledger_path"],
        "commit": coordination["ledger_commit"],
        "git_blob_oid": coordination["ledger_git_blob_oid"],
        "digest_domain": coordination["digest_domain"],
        "sha256": coordination["sha256"],
    }
    ledger = load_json_bytes(repo.git_blob_pin(pin, label), label)
    if not isinstance(ledger, dict):
        fail(f"{label}: ledger root is not an object")
    return ledger


def _packet_ledger_receipts(
    repo: GitRepo, packet: dict[str, Any], label: str
) -> list[dict[str, Any]]:
    return _require_record_array(_packet_ledger(repo, packet, label), "receipts")


def _require_ledger_receipt(
    receipts: list[dict[str, Any]], receipt_id: str, kind: str, label: str
) -> None:
    record = _one_record(receipts, "receipt_id", receipt_id, label)
    if record.get("kind") != kind:
        fail(f"{label}: receipt kind differs from {kind!r}")


def _resolve_activation_receipt(
    repo: GitRepo,
    schema: dict[str, Any],
    activation_ledger_pin: dict[str, Any],
    activation_receipt_id: str,
    label: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    ledger_bytes = repo.git_blob_pin(activation_ledger_pin, f"{label} ledger")
    ledger = load_json_bytes(ledger_bytes, f"{label} ledger")
    if not isinstance(ledger, dict):
        fail(f"{label} ledger root must be an object")
    payload = _outer_receipt(
        ledger,
        activation_receipt_id,
        "context-packet-activation",
        label,
    )
    validate_schema_instance(
        schema, payload, f"{label} payload", "contextPacketActivationReceipt"
    )
    validate_receipt(schema, payload, repo)
    return payload, ledger


def validate_receipt(
    schema: dict[str, Any], receipt: dict[str, Any], repo: GitRepo
) -> str:
    validate_schema_instance(schema, receipt, "receipt", "contextPacketReceiptPayload")
    kind = receipt["schema"]
    if kind == "studio-context-packet-return/1":
        _validate_packet_pin_path(receipt["packet"], "return receipt packet")
        implementation_packet = _resolve_packet_pin(
            repo, schema, receipt["packet"], "return receipt packet"
        )
        _validate_return_payload(
            repo, schema, receipt, implementation_packet, "return receipt"
        )
    elif kind in ("studio-context-packet-activation/1", "studio-context-packet-handoff/1"):
        packets = _validate_chain(repo, schema, receipt["packet_revision_chain"], "packet_revision_chain")
        if not _same_pin(receipt["packet_revision_chain"][-1], receipt["packet"]):
            fail("receipt packet is not the head of packet_revision_chain")
        packet = packets[-1]
        assignment = packet["assignment"]
        for receipt_field, packet_field in (
            ("session_id", "session_id"),
            ("work_package", "work_package"),
            ("lease_id", "lease_id"),
            ("assignment_receipt_id", "assignment_receipt_id"),
        ):
            if receipt[receipt_field] != assignment[packet_field]:
                fail(f"receipt {receipt_field} differs from packet assignment")
        if receipt["profile"] != packet["profile"] or receipt["budget"] != packet["budget"]:
            fail("receipt profile/budget differs from packet")
        if receipt["activated_expansion_ids"] != packet["activated_expansion_ids"]:
            fail("receipt activated expansions differ from packet")
        if kind == "studio-context-packet-activation/1" and receipt["handoff_head"] is not None:
            fail("activation receipt must omit a handoff head")
        if kind == "studio-context-packet-handoff/1":
            repo.verify_commit(receipt["handoff_head"], "handoff receipt head")
            activation_ledger = receipt["activation_ledger"]
            assignment_ledger_commit = packet["pins"]["coordination"][
                "ledger_commit"
            ]
            activation_ancestry = repo._run(
                [
                    "merge-base",
                    "--is-ancestor",
                    assignment_ledger_commit,
                    activation_ledger["commit"],
                ],
                check=False,
            )
            if activation_ancestry.returncode != 0:
                fail("handoff activation ledger does not descend from assignment ledger")
            remote_head = repo.verify_remote_coordination_ref(
                assignment["authoritative_remote_url"],
                packet["pins"]["coordination"]["ref"],
                activation_ledger["commit"],
            )
            activation_payload, cumulative_ledger = _resolve_activation_receipt(
                repo,
                schema,
                activation_ledger,
                receipt["activation_receipt_id"],
                "handoff activation receipt",
            )
            for field in (
                "session_id",
                "work_package",
                "lease_id",
                "assignment_receipt_id",
                "packet",
                "profile",
                "budget",
                "activated_expansion_ids",
                "packet_revision_chain",
            ):
                if activation_payload[field] != receipt[field]:
                    fail(
                        f"handoff field {field!r} differs from historical activation receipt"
                    )
            return_payload = _resolve_return_receipt(
                repo,
                schema,
                cumulative_ledger,
                receipt["return_receipt_id"],
                packet,
                "handoff return receipt",
            )
            if return_payload["packet"] != receipt["packet"]:
                fail("handoff and return receipts pin different implementation packets")
            if return_payload["handoff_head"] != receipt["handoff_head"]:
                fail("handoff and return receipts pin different handoff heads")
            final_remote_head = repo.verify_remote_coordination_ref(
                assignment["authoritative_remote_url"],
                packet["pins"]["coordination"]["ref"],
                activation_ledger["commit"],
            )
            if final_remote_head != remote_head:
                fail("protected coordination ref changed during handoff validation")
    else:
        reviewer_packets = _validate_chain(repo, schema, receipt["reviewer_revision_chain"], "reviewer_revision_chain")
        implementation_packets = _validate_chain(
            repo, schema, receipt["implementation_revision_chain"], "implementation_revision_chain"
        )
        if receipt["reviewer_revision_chain"][-1] != receipt["reviewer_packet"]:
            fail("reviewer_packet is not reviewer_revision_chain head")
        if receipt["implementation_revision_chain"][-1] != receipt["implementation_packet"]:
            fail("implementation_packet is not implementation_revision_chain head")
        reviewer = reviewer_packets[-1]
        implementation = implementation_packets[-1]
        if reviewer["audience"] != "reviewer" or implementation["audience"] == "reviewer":
            fail("review receipt chain audiences are invalid")
        if reviewer["review_of"] is None:
            fail("reviewer packet lacks review_of")
        if reviewer["review_of"]["implementation_packet"] != receipt["implementation_packet"]:
            fail("reviewer packet and receipt pin different implementation packets")
        if reviewer["review_of"]["handoff_head"] != receipt["handoff_head"]:
            fail("reviewer packet and receipt pin different handoff heads")
        if reviewer["review_of"]["return_receipt_id"] != receipt["return_receipt_id"]:
            fail("reviewer packet and receipt pin different return receipts")
        if reviewer["review_of"].get("handoff_receipt_id") != receipt["handoff_receipt_id"]:
            fail("reviewer packet and receipt pin different handoff receipts")
        if receipt["work_package"] != implementation["assignment"]["work_package"]:
            fail("review receipt work package differs from implementation packet")
        if receipt["work_package"] != reviewer["assignment"]["work_package"]:
            fail("review receipt work package differs from reviewer packet")
        if receipt["implementation_session_id"] != implementation["assignment"]["session_id"]:
            fail("review receipt implementation session differs from packet")
        if receipt["reviewer_session_id"] != reviewer["assignment"]["session_id"]:
            fail("review receipt reviewer session differs from packet")
        if receipt["lease_id"] != reviewer["assignment"]["lease_id"]:
            fail("review receipt lease differs from reviewer packet")
        if reviewer["assignment"].get("access_mode") != "read-only-review":
            fail("reviewer packet assignment is not read-only-review")
        if implementation["assignment"].get("access_mode") != "write-lease":
            fail("implementation packet assignment is not write-lease")
        reviewer_ledger = _packet_ledger(
            repo, reviewer, "reviewer packet coordination ledger"
        )
        validate_ledger_binding(reviewer_ledger, reviewer)
        live_head = repo.verify_remote_coordination_ref(
            reviewer["assignment"]["authoritative_remote_url"],
            reviewer["pins"]["coordination"]["ref"],
            reviewer["pins"]["coordination"]["ledger_commit"],
        )
        live_ledger = load_json_bytes(
            repo.read_blob(
                live_head,
                reviewer["pins"]["coordination"]["ledger_path"],
                "live reviewer coordination ledger",
            ),
            "live reviewer coordination ledger",
        )
        if not isinstance(live_ledger, dict):
            fail("live reviewer coordination ledger root must be an object")
        validate_ledger_binding(live_ledger, reviewer, require_active=True)
        handoff_payload, return_payload = _validate_reviewer_binding(
            repo, schema, reviewer, implementation, reviewer_ledger
        )
        if handoff_payload["packet"] != receipt["implementation_packet"]:
            fail("handoff receipt and review receipt pin different implementation packets")
        if return_payload["packet"] != receipt["implementation_packet"]:
            fail("return receipt and review receipt pin different implementation packets")
        if return_payload["handoff_head"] != receipt["handoff_head"]:
            fail("return receipt and review receipt pin different handoff heads")
        repo.verify_commit(receipt["handoff_head"], "review receipt handoff head")
        all_passed = _validate_checks(
            receipt["checks"],
            reviewer["review_of"]["acceptance_commands"],
            "review receipt",
        )
        if all_passed != (receipt["outcome"] == "accepted"):
            fail("review outcome contradicts check results")
        final_live_head = repo.verify_remote_coordination_ref(
            reviewer["assignment"]["authoritative_remote_url"],
            reviewer["pins"]["coordination"]["ref"],
            reviewer["pins"]["coordination"]["ledger_commit"],
        )
        if final_live_head != live_head:
            fail("protected coordination ref changed during review validation")
    return sha256(jcs(receipt))


def validate_activation_for_emission(
    schema: dict[str, Any],
    packet: dict[str, Any],
    packet_digest: str,
    repo: GitRepo,
    activation_ledger_commit: str,
    activation_ledger_sha256: str,
    activation_receipt_id: str,
    expected_live_head: str,
) -> None:
    repo.verify_commit(activation_ledger_commit, "activation ledger commit")
    pinned_ledger_commit = packet["pins"]["coordination"]["ledger_commit"]
    ancestry = repo._run(
        ["merge-base", "--is-ancestor", pinned_ledger_commit, activation_ledger_commit],
        check=False,
    )
    if ancestry.returncode != 0:
        fail("packet assignment ledger is not an ancestor of the activation ledger")
    ledger_path = packet["pins"]["coordination"]["ledger_path"]
    ledger_bytes = repo.read_blob(
        activation_ledger_commit, ledger_path, "activation ledger"
    )
    if sha256(ledger_bytes) != activation_ledger_sha256:
        fail("activation ledger raw SHA-256 mismatch")
    ledger = load_json_bytes(ledger_bytes, "activation ledger")
    if not isinstance(ledger, dict):
        fail("activation ledger root must be an object")
    validate_ledger_binding(ledger, packet, require_active=True)
    coordination_ref = packet["pins"]["coordination"]["ref"]
    observed_live_head = repo.verify_remote_coordination_ref(
        packet["assignment"]["authoritative_remote_url"],
        coordination_ref,
        activation_ledger_commit,
    )
    if observed_live_head != expected_live_head:
        fail("protected coordination ref changed during packet validation")
    live_ledger_bytes = repo.read_blob(
        observed_live_head, ledger_path, "live coordination ledger"
    )
    live_ledger = load_json_bytes(live_ledger_bytes, "live coordination ledger")
    if not isinstance(live_ledger, dict):
        fail("live coordination ledger root must be an object")
    validate_ledger_binding(live_ledger, packet, require_active=True)
    outer = _one_record(
        _require_record_array(ledger, "receipts"),
        "receipt_id",
        activation_receipt_id,
        "activation receipt",
    )
    if outer.get("kind") != "context-packet-activation":
        fail("activation receipt outer record has the wrong kind")
    payload = outer.get("payload")
    if not isinstance(payload, dict):
        fail("activation receipt outer record has no object payload")
    validate_schema_instance(
        schema, payload, "activation receipt payload", "contextPacketActivationReceipt"
    )
    validate_receipt(schema, payload, repo)
    if payload["packet"]["sha256"] != packet_digest:
        fail("activation receipt packet digest differs from the validated packet")
    activated_packet, _ = repo.packet_pin(
        payload["packet"], "activation receipt packet"
    )
    if jcs(activated_packet) != jcs(packet):
        fail("activation receipt resolves to packet bytes different from --packet")
    final_live_head = repo.verify_remote_coordination_ref(
        packet["assignment"]["authoritative_remote_url"],
        coordination_ref,
        activation_ledger_commit,
    )
    if final_live_head != expected_live_head:
        fail("protected coordination ref changed during activation validation")


def _hex_sha256(value: str) -> str:
    if re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise argparse.ArgumentTypeError("must be 64 lowercase hexadecimal characters")
    return value


def _hex40(value: str) -> str:
    if re.fullmatch(r"[0-9a-f]{40}", value) is None:
        raise argparse.ArgumentTypeError("must be 40 lowercase hexadecimal characters")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate closed Studio context profiles, packets, and receipts."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    profiles = subparsers.add_parser(
        "profiles", help="validate the profile catalog and measure implementer cores"
    )
    profiles.add_argument("--schema", required=True, type=Path, help="context packet schema")
    profiles.add_argument("--profiles", required=True, type=Path, help="profile manifest")
    profiles.add_argument("--source-root", required=True, type=Path, help="Git worktree containing source documents")
    profiles.add_argument(
        "--source-commit",
        required=True,
        type=_hex40,
        help="immutable commit used to read every template source through Git",
    )

    packet = subparsers.add_parser("packet", help="validate one context packet")
    packet.add_argument("--schema", required=True, type=Path, help="context packet schema")
    packet.add_argument("--profiles", required=True, type=Path, help="profile manifest")
    packet.add_argument("--packet", required=True, type=Path, help="packet JSON")
    packet.add_argument("--repo", required=True, type=Path, help="Git worktree used for object resolution")
    packet.add_argument("--external-sha256", type=_hex_sha256, help="expected RFC 8785 packet digest")
    packet.add_argument(
        "--emit-context",
        metavar="PATH_OR_-",
        help="after success, emit deterministic selected-context JSON to a path or stdout (-)",
    )
    packet.add_argument(
        "--activation-ledger-commit",
        type=_hex40,
        help="coordination commit containing the activation receipt (required for emission)",
    )
    packet.add_argument(
        "--activation-ledger-sha256",
        type=_hex_sha256,
        help="raw activation-ledger blob SHA-256 (required for emission)",
    )
    packet.add_argument(
        "--activation-receipt-id",
        help="outer activation receipt ID (required for emission)",
    )

    receipt = subparsers.add_parser("receipt", help="validate a context-packet receipt payload")
    receipt.add_argument("--schema", required=True, type=Path, help="context packet schema")
    receipt.add_argument("--receipt", required=True, type=Path, help="receipt payload JSON")
    receipt.add_argument("--repo", required=True, type=Path, help="Git worktree used for object resolution")
    return parser


def _write_emitted(path: str, entries: list[dict[str, Any]]) -> None:
    output = jcs({"entries": entries}) + b"\n"
    if path == "-":
        sys.stdout.buffer.write(output)
        sys.stdout.buffer.flush()
        return
    destination = Path(path)
    try:
        destination.write_bytes(output)
    except OSError as exc:
        fail(f"cannot write selected context to {destination}: {exc}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        schema, schema_bytes = load_json_file(args.schema, "context schema")
        if not isinstance(schema, dict):
            fail("context schema root must be an object")
        if args.command == "profiles":
            manifest, _ = load_json_file(args.profiles, "profile manifest")
            catalog = validate_profile_manifest(schema, manifest)
            for line in run_profiles(
                schema, catalog, args.source_root, args.source_commit
            ):
                print(line)
            print("context-profiles-valid")
            return 0
        if args.command == "packet":
            activation_arguments = (
                args.activation_ledger_commit,
                args.activation_ledger_sha256,
                args.activation_receipt_id,
            )
            if args.emit_context and (
                args.external_sha256 is None
                or any(value is None for value in activation_arguments)
            ):
                fail(
                    "--emit-context requires --external-sha256, --activation-ledger-commit, "
                    "--activation-ledger-sha256, and --activation-receipt-id"
                )
            if not args.emit_context and any(
                value is not None for value in activation_arguments
            ):
                fail("activation-ledger arguments are valid only with --emit-context")
            manifest, profiles_bytes = load_json_file(args.profiles, "profile manifest")
            catalog = validate_profile_manifest(schema, manifest)
            packet, _ = load_json_file(args.packet, "packet")
            repo = GitRepo(args.repo)
            digest, selected, entries, live_head = validate_packet(
                schema,
                schema_bytes,
                catalog,
                profiles_bytes,
                packet,
                repo,
                args.external_sha256,
            )
            if args.emit_context:
                validate_activation_for_emission(
                    schema,
                    packet,
                    digest,
                    repo,
                    args.activation_ledger_commit,
                    args.activation_ledger_sha256,
                    args.activation_receipt_id,
                    live_head,
                )
                _write_emitted(args.emit_context, entries)
                if args.emit_context == "-":
                    print(
                        f"context-packet-valid sha256={digest} selected_utf8_bytes={selected}",
                        file=sys.stderr,
                    )
                else:
                    print(f"context-packet-valid sha256={digest} selected_utf8_bytes={selected}")
            else:
                print(f"context-packet-valid sha256={digest} selected_utf8_bytes={selected}")
            return 0
        receipt, _ = load_json_file(args.receipt, "receipt")
        digest = validate_receipt(schema, receipt, GitRepo(args.repo))
        print(f"context-packet-receipt-valid sha256={digest}")
        return 0
    except Invalid as exc:
        prefix = {
            "profiles": "context-profiles-invalid",
            "packet": "context-packet-invalid",
            "receipt": "context-packet-receipt-invalid",
        }.get(getattr(args, "command", None), "context-validation-invalid")
        print(f"{prefix}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

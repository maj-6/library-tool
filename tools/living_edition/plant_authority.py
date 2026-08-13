#!/usr/bin/env python3
"""Create, validate, query, and snapshot the external plant authority POC.

The SQLite database is a mutable workbench store and deliberately lives under
``data/plant-authority-poc/``, never inside a ``.whled`` package. The exporter
produces a deterministic, checksummed JSON projection suitable for sealing as
an immutable authority snapshot.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping


SNAPSHOT_SCHEMA = "world-herb-library/plant-authority-snapshot/0.1"
DEFAULT_ROOT = Path(__file__).resolve().parents[2] / "data" / "plant-authority-poc"
DEFAULT_SCHEMA = DEFAULT_ROOT / "schema.sql"
DEFAULT_SEED = DEFAULT_ROOT / "seed.json"
REQUIRED_META = frozenset({
    "database_id",
    "schema_version",
    "release",
    "snapshot_created_at",
    "title",
    "license",
    "coverage_note",
})

TABLE_COLUMNS: dict[str, tuple[str, ...]] = {
    "agent": ("id", "kind", "display_name", "version", "uri", "note"),
    "authority_source": ("id", "label", "base_uri", "license", "scope_note"),
    "witness": ("id", "catalog_uri", "edition_uri", "title", "repository", "call_number", "external_record_id", "source_url"),
    "name_form": ("id", "literal", "normalized_key", "language", "script", "transliteration", "period_label", "period_start", "period_end", "geographic_scope", "normalization_profile", "source_note", "status", "created_by", "created_at"),
    "concept": ("id", "label", "kind", "tradition", "period_label", "period_start", "period_end", "geographic_scope", "scope_note", "status", "created_by", "created_at"),
    "referent": ("id", "authority_source_id", "authority_identifier", "cached_label", "cached_at", "authority_uri", "authority_snapshot", "status", "note", "created_by", "created_at"),
    "mention": ("id", "witness_id", "canvas_uri", "region_uri", "region_revision", "selector_json", "name_form_id", "reading_state", "created_by", "created_at", "note"),
    "mention_anchor": ("id", "mention_id", "transcription_layer_uri", "transcription_revision", "passage_id", "char_start", "char_end", "exact", "prefix", "suffix", "status", "supersedes_id", "created_by", "created_at"),
    "assertion": ("id", "subject_node_id", "predicate", "object_node_id", "created_by", "created_at", "confidence", "state", "method", "rationale", "supersedes_id"),
    "evidence": ("id", "assertion_id", "kind", "quote", "page_uri", "region_uri", "selector_json", "citation_uri", "citation_label", "reasoning", "created_by", "created_at"),
    "review": ("id", "assertion_id", "reviewer_id", "decision", "rationale", "created_at"),
}
NODE_KINDS = {
    "name_form": "name",
    "concept": "concept",
    "referent": "referent",
    "mention": "mention",
    "assertion": "assertion",
    "evidence": "evidence",
    "review": "review",
}
SEED_ORDER = tuple(TABLE_COLUMNS)
SEED_KEYS = {
    "agent": "agents",
    "authority_source": "authority_sources",
    "witness": "witnesses",
    "name_form": "name_forms",
    "concept": "concepts",
    "referent": "referents",
    "mention": "mentions",
    "mention_anchor": "mention_anchors",
    "assertion": "assertions",
    "evidence": "evidence",
    "review": "reviews",
}


class AuthorityError(Exception):
    """Raised for invalid or unsafe authority operations."""


@dataclass(frozen=True, slots=True)
class Issue:
    level: str
    location: str
    message: str


def _canonical_json(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, separators=(",", ": "))
        + "\n"
    ).encode("utf-8")


def _content_digest(value: Any) -> str:
    compact = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(compact).hexdigest()


def normalize_key(literal: str) -> str:
    """A conservative POC lookup key; original spelling always remains stored."""

    value = unicodedata.normalize("NFKC", literal).casefold().replace("ſ", "s")
    return "".join(character for character in value if character.isalnum())


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AuthorityError(f"cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise AuthorityError(f"{path} must contain a JSON object")
    return value


def _connect(path: Path, *, readonly: bool = False) -> sqlite3.Connection:
    if readonly and not path.is_file():
        raise AuthorityError(f"database does not exist: {path}")
    if readonly:
        uri = path.resolve().as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
    else:
        connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _insert_record(connection: sqlite3.Connection, table: str, record: Mapping[str, Any]) -> None:
    allowed = TABLE_COLUMNS[table]
    unknown = set(record) - set(allowed)
    if unknown:
        raise AuthorityError(f"seed.{table}: unknown fields {sorted(unknown)}")
    columns = [column for column in allowed if column in record]
    if not columns:
        raise AuthorityError(f"seed.{table}: empty record")
    values = []
    for column in columns:
        value = record[column]
        if column.endswith("_json") and value is not None and not isinstance(value, str):
            value = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        values.append(value)
    placeholders = ", ".join("?" for _ in columns)
    connection.execute(
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})",
        values,
    )


def initialize_database(
    destination: str | os.PathLike[str],
    *,
    schema_path: str | os.PathLike[str] = DEFAULT_SCHEMA,
    seed_path: str | os.PathLike[str] = DEFAULT_SEED,
) -> Path:
    """Create a new authority DB atomically; an existing target is untouched."""

    destination_path = Path(destination).resolve()
    if destination_path.exists():
        raise AuthorityError(f"refusing to replace existing database: {destination_path}")
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    schema = Path(schema_path).read_text(encoding="utf-8")
    seed = _load_json(Path(seed_path))
    metadata = seed.get("metadata")
    if not isinstance(metadata, dict) or REQUIRED_META - set(metadata):
        raise AuthorityError(f"seed.metadata is missing {sorted(REQUIRED_META - set(metadata or {}))}")

    fd, temp_name = tempfile.mkstemp(prefix=f".{destination_path.name}.", suffix=".tmp", dir=destination_path.parent)
    os.close(fd)
    temporary = Path(temp_name)
    try:
        connection = _connect(temporary)
        try:
            connection.executescript(schema)
            with connection:
                for key, value in sorted(metadata.items()):
                    connection.execute(
                        "INSERT INTO authority_meta (key, value) VALUES (?, ?)",
                        (key, str(value)),
                    )
                for table in SEED_ORDER:
                    seed_key = SEED_KEYS[table]
                    records = seed.get(seed_key, [])
                    if not isinstance(records, list):
                        raise AuthorityError(f"seed.{seed_key} must be an array")
                    for record in records:
                        if not isinstance(record, dict):
                            raise AuthorityError(f"seed.{seed_key} entries must be objects")
                        node_kind = NODE_KINDS.get(table)
                        if node_kind:
                            connection.execute(
                                "INSERT INTO node (id, kind, created_at) VALUES (?, ?, ?)",
                                (record.get("id"), node_kind, record.get("created_at")),
                            )
                        _insert_record(connection, table, record)
            issues = validate_connection(connection)
            errors = [issue for issue in issues if issue.level == "error"]
            if errors:
                raise AuthorityError("; ".join(f"{item.location}: {item.message}" for item in errors))
            connection.execute("PRAGMA optimize")
        finally:
            connection.close()
        os.replace(temporary, destination_path)
    except Exception:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise
    return destination_path


def _rows(connection: sqlite3.Connection, query: str, parameters: Iterable[Any] = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(query, tuple(parameters)).fetchall()]


def validate_connection(connection: sqlite3.Connection) -> list[Issue]:
    issues: list[Issue] = []
    integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        issues.append(Issue("error", "database", f"integrity check failed: {integrity}"))
    for row in connection.execute("PRAGMA foreign_key_check"):
        issues.append(Issue("error", f"{row[0]}:{row[1]}", f"foreign-key failure at {row[2]}"))
    metadata = dict(connection.execute("SELECT key, value FROM authority_meta"))
    for key in sorted(REQUIRED_META - set(metadata)):
        issues.append(Issue("error", f"authority_meta.{key}", "is required"))
    if connection.execute("PRAGMA user_version").fetchone()[0] != 1:
        issues.append(Issue("error", "database.user_version", "must be 1"))

    child_union = " UNION ALL ".join(
        f"SELECT id, '{kind}' AS kind FROM {table}"
        for table, kind in NODE_KINDS.items()
    )
    orphaned = _rows(
        connection,
        f"SELECT n.id, n.kind FROM node n LEFT JOIN ({child_union}) c ON c.id=n.id AND c.kind=n.kind WHERE c.id IS NULL",
    )
    for row in orphaned:
        issues.append(Issue("error", f"node.{row['id']}", "has no matching typed record"))

    machine_promotions = _rows(
        connection,
        """
        SELECT a.id FROM assertion a
        JOIN agent g ON g.id = a.created_by
        WHERE a.state <> 'proposed' AND g.kind <> 'human'
        """,
    )
    for row in machine_promotions:
        issues.append(Issue("error", f"assertion.{row['id']}", "machine/import actor authored a non-proposed assertion"))
    nonhuman_reviews = _rows(
        connection,
        """
        SELECT r.id FROM review r
        JOIN agent g ON g.id = r.reviewer_id
        WHERE g.kind <> 'human'
        """,
    )
    for row in nonhuman_reviews:
        issues.append(Issue("error", f"review.{row['id']}", "reviewer must be human"))

    nameless = _rows(
        connection,
        """
        SELECT c.id FROM concept c
        WHERE c.kind='plant' AND NOT EXISTS (
            SELECT 1 FROM assertion a
            WHERE a.object_node_id=c.id AND a.predicate='historical-name-for'
        )
        """,
    )
    for row in nameless:
        issues.append(Issue("warning", f"concept.{row['id']}", "plant concept has no asserted written name"))
    unsupported_name_forms = _rows(
        connection,
        """
        SELECT n.id FROM name_form n
        WHERE NOT EXISTS (
            SELECT 1 FROM assertion a
            WHERE a.subject_node_id=n.id
              AND a.predicate IN ('historical-name-for','modern-name-for')
        )
        """,
    )
    for row in unsupported_name_forms:
        issues.append(Issue("warning", f"name_form.{row['id']}", "has no concept or referent assertion"))
    return issues


def validate_database(path: str | os.PathLike[str]) -> list[Issue]:
    connection = _connect(Path(path), readonly=True)
    try:
        return validate_connection(connection)
    except sqlite3.DatabaseError as exc:
        return [Issue("error", "database", str(exc))]
    finally:
        connection.close()


def _json_columns(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for record in records:
        for key in tuple(record):
            if key.endswith("_json"):
                raw = record.pop(key)
                record[key.removesuffix("_json")] = json.loads(raw) if raw is not None else None
    return records


def _with_uri(records: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    for record in records:
        record["uri"] = f"whl-entity://{kind}/{record['id']}"
    return records


def build_snapshot(connection: sqlite3.Connection, *, created_at: str | None = None) -> dict[str, Any]:
    metadata = dict(connection.execute("SELECT key, value FROM authority_meta ORDER BY key"))
    timestamp = created_at or metadata["snapshot_created_at"]
    records: dict[str, list[dict[str, Any]]] = {
        "agents": _rows(connection, "SELECT * FROM agent ORDER BY id"),
        "authority_sources": _rows(connection, "SELECT * FROM authority_source ORDER BY id"),
        "witnesses": _rows(connection, "SELECT * FROM witness ORDER BY id"),
        "names": _with_uri(_rows(connection, "SELECT * FROM name_form ORDER BY id"), "name"),
        "concepts": _with_uri(_rows(connection, "SELECT * FROM concept ORDER BY id"), "concept"),
        "referents": _with_uri(_rows(connection, "SELECT * FROM referent ORDER BY id"), "referent"),
        "mentions": _json_columns(_rows(connection, "SELECT * FROM mention ORDER BY id")),
        "mention_anchors": _rows(connection, "SELECT * FROM mention_anchor ORDER BY created_at, id"),
        "assertions": _with_uri(_rows(connection, "SELECT * FROM assertion_effective_state ORDER BY id"), "assertion"),
        "evidence": _with_uri(_json_columns(_rows(connection, "SELECT * FROM evidence ORDER BY id")), "evidence"),
        "reviews": _with_uri(_rows(connection, "SELECT * FROM review ORDER BY created_at, id"), "review"),
    }
    for mention in records["mentions"]:
        mention["uri"] = f"{mention['region_uri']}/mention/{mention['id']}"

    names_by_id = {record["id"]: record for record in records["names"]}
    referents_by_id = {record["id"]: record for record in records["referents"]}
    assertions = records["assertions"]
    entities: list[dict[str, Any]] = []
    for concept in records["concepts"]:
        written_names = []
        modern_referents = []
        for assertion in assertions:
            if assertion["object_node_id"] == concept["id"] and assertion["predicate"] == "historical-name-for":
                name = names_by_id.get(assertion["subject_node_id"])
                if name:
                    written_names.append({
                        "name_id": name["id"],
                        "literal": name["literal"],
                        "language": name["language"],
                        "script": name["script"],
                        "period_label": name["period_label"],
                        "assertion_id": assertion["id"],
                        "confidence": assertion["confidence"],
                        "state": assertion["effective_state"],
                    })
            if assertion["subject_node_id"] == concept["id"] and assertion["predicate"] == "identified-as":
                referent = referents_by_id.get(assertion["object_node_id"])
                if referent:
                    modern_referents.append({
                        "referent_id": referent["id"],
                        "label": referent["cached_label"],
                        "status": referent["status"],
                        "assertion_id": assertion["id"],
                        "confidence": assertion["confidence"],
                        "state": assertion["effective_state"],
                    })
        entities.append({
            "id": concept["id"],
            "uri": concept["uri"],
            "label": concept["label"],
            "kind": concept["kind"],
            "scope": {
                "tradition": concept["tradition"],
                "period": concept["period_label"],
                "region": concept["geographic_scope"],
                "note": concept["scope_note"],
            },
            "written_names": sorted(written_names, key=lambda item: (item["language"], item["literal"], item["name_id"])),
            "modern_referents": sorted(modern_referents, key=lambda item: item["referent_id"]),
        })

    content = {"records": records, "entities": entities}
    return {
        "schema": SNAPSHOT_SCHEMA,
        "database_id": metadata["database_id"],
        "schema_version": metadata["schema_version"],
        "release": metadata["release"],
        "created_at": timestamp,
        "title": metadata["title"],
        "license": metadata["license"],
        "coverage_note": metadata["coverage_note"],
        "content_sha256": _content_digest(content),
        **content,
    }


def export_snapshot(
    database: str | os.PathLike[str],
    destination: str | os.PathLike[str],
    *,
    created_at: str | None = None,
) -> dict[str, Any]:
    issues = validate_database(database)
    errors = [issue for issue in issues if issue.level == "error"]
    if errors:
        raise AuthorityError("; ".join(f"{item.location}: {item.message}" for item in errors))
    connection = _connect(Path(database), readonly=True)
    try:
        snapshot = build_snapshot(connection, created_at=created_at)
    finally:
        connection.close()
    destination_path = Path(destination)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    destination_path.write_bytes(_canonical_json(snapshot))
    return snapshot


def validate_snapshot(snapshot: Any) -> list[Issue]:
    issues: list[Issue] = []
    if not isinstance(snapshot, dict):
        return [Issue("error", "snapshot", "must be an object")]
    for field in ("schema", "database_id", "schema_version", "release", "created_at", "title", "license", "coverage_note", "content_sha256", "records", "entities"):
        if field not in snapshot:
            issues.append(Issue("error", f"snapshot.{field}", "is required"))
    if snapshot.get("schema") != SNAPSHOT_SCHEMA:
        issues.append(Issue("error", "snapshot.schema", f"must be {SNAPSHOT_SCHEMA}"))
    content = {"records": snapshot.get("records"), "entities": snapshot.get("entities")}
    if snapshot.get("content_sha256") != _content_digest(content):
        issues.append(Issue("error", "snapshot.content_sha256", "does not match records and entities"))
    records = snapshot.get("records")
    if isinstance(records, dict):
        ids: dict[str, set[str]] = {}
        for key in ("names", "concepts", "referents", "mentions", "assertions", "evidence", "reviews"):
            value = records.get(key)
            if not isinstance(value, list):
                issues.append(Issue("error", f"snapshot.records.{key}", "must be an array"))
                ids[key] = set()
            else:
                ids[key] = {record.get("id") for record in value if isinstance(record, dict)}
        for assertion in records.get("assertions", []):
            if not isinstance(assertion, dict):
                continue
            subject = assertion.get("subject_node_id")
            object_id = assertion.get("object_node_id")
            all_nodes = set().union(ids["names"], ids["concepts"], ids["referents"], ids["mentions"], ids["assertions"], ids["evidence"], ids["reviews"])
            if subject not in all_nodes or object_id not in all_nodes:
                issues.append(Issue("error", f"snapshot.assertion.{assertion.get('id')}", "has a missing endpoint"))
    return issues


def lookup(database: str | os.PathLike[str], query: str) -> list[dict[str, Any]]:
    key = normalize_key(query)
    connection = _connect(Path(database), readonly=True)
    try:
        return _rows(
            connection,
            """
            SELECT
                n.id AS name_id, n.literal, n.language, n.script,
                c.id AS concept_id, c.label AS concept_label,
                c.tradition, c.period_label, c.geographic_scope,
                a.id AS assertion_id, a.confidence, a.effective_state
            FROM name_form n
            LEFT JOIN assertion_effective_state a
              ON a.subject_node_id=n.id AND a.predicate='historical-name-for'
            LEFT JOIN concept c ON c.id=a.object_node_id
            WHERE n.normalized_key=? OR lower(n.literal)=lower(?)
            ORDER BY n.literal, c.label, a.id
            """,
            (key, query),
        )
    finally:
        connection.close()


def _print_issues(issues: list[Issue], as_json: bool) -> None:
    if as_json:
        print(json.dumps([
            {"level": issue.level, "location": issue.location, "message": issue.message}
            for issue in issues
        ], ensure_ascii=False, indent=2))
    elif not issues:
        print("valid")
    else:
        for issue in issues:
            print(f"{issue.level}: {issue.location}: {issue.message}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    init = subparsers.add_parser("init", help="create a new SQLite authority database")
    init.add_argument("database", type=Path)
    init.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA)
    init.add_argument("--seed", type=Path, default=DEFAULT_SEED)

    validate = subparsers.add_parser("validate", help="run integrity and scholarship guards")
    validate.add_argument("database", type=Path)
    validate.add_argument("--json", action="store_true")

    export = subparsers.add_parser("export", help="write an immutable JSON snapshot")
    export.add_argument("database", type=Path)
    export.add_argument("output", type=Path)
    export.add_argument("--created-at")

    search = subparsers.add_parser("lookup", help="exact/normalized written-name lookup")
    search.add_argument("database", type=Path)
    search.add_argument("query")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "init":
            path = initialize_database(args.database, schema_path=args.schema, seed_path=args.seed)
            print(f"created {path}")
            return 0
        if args.command == "validate":
            issues = validate_database(args.database)
            _print_issues(issues, args.json)
            return 1 if any(issue.level == "error" for issue in issues) else 0
        if args.command == "export":
            snapshot = export_snapshot(args.database, args.output, created_at=args.created_at)
            print(f"exported {args.output} ({len(snapshot['entities'])} concept entities)")
            return 0
        if args.command == "lookup":
            print(json.dumps(lookup(args.database, args.query), ensure_ascii=False, indent=2))
            return 0
    except (AuthorityError, OSError, sqlite3.DatabaseError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

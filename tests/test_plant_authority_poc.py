"""External authority schema, auditability, and snapshot boundaries."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "living_edition"))
import plant_authority  # noqa: E402


SCHEMA = ROOT / "data" / "plant-authority-poc" / "schema.sql"
SEED = ROOT / "data" / "plant-authority-poc" / "seed.json"


def _database(tmp_path: Path) -> Path:
    return plant_authority.initialize_database(
        tmp_path / "plant-authority.sqlite3",
        schema_path=SCHEMA,
        seed_path=SEED,
    )


def test_seed_database_validates_and_reconciles_written_names(tmp_path):
    database = _database(tmp_path)
    assert plant_authority.validate_database(database) == []
    result = plant_authority.lookup(database, "gencyane")
    assert result == [{
        "name_id": "name-gencyane",
        "literal": "gencyane",
        "language": "enm",
        "script": "Latn",
        "concept_id": "concept-gentian-western",
        "concept_label": "Gentian as a Western bitter-root simple",
        "tradition": "Western herbal",
        "period_label": "classical through early modern",
        "geographic_scope": "Western Europe",
        "assertion_id": "assert-name-gencyane",
        "confidence": "likely",
        "effective_state": "proposed",
    }]


def test_snapshot_is_deterministic_checksummed_and_lists_written_forms(tmp_path):
    database = _database(tmp_path)
    first_path = tmp_path / "first.json"
    second_path = tmp_path / "second.json"
    first = plant_authority.export_snapshot(database, first_path)
    second = plant_authority.export_snapshot(database, second_path)
    assert first_path.read_bytes() == second_path.read_bytes()
    assert plant_authority.validate_snapshot(first) == []
    assert first["content_sha256"] == second["content_sha256"]

    entities = {record["id"]: record for record in first["entities"]}
    betony_names = {record["literal"] for record in entities["concept-betony-medieval-western"]["written_names"]}
    assert {"betayne", "betonie", "betony", "betonica", "wood betony", "bishop's wort", "bishopswort"} <= betony_names
    longdan_names = {record["literal"] for record in entities["concept-longdan-chinese"]["written_names"]}
    assert longdan_names == {"龍膽", "龙胆", "lóngdǎn"}
    assert entities["concept-longdan-chinese"]["modern_referents"][0]["status"] == "unresolved"


def test_machine_cannot_author_accepted_assertion(tmp_path):
    database = _database(tmp_path)
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        connection.execute("INSERT INTO node (id, kind, created_at) VALUES ('assert-illegal', 'assertion', '2026-08-12T01:00:00Z')")
        with pytest.raises(sqlite3.IntegrityError, match="only a human"):
            connection.execute(
                """
                INSERT INTO assertion (
                    id, subject_node_id, predicate, object_node_id,
                    created_by, created_at, confidence, state, method,
                    rationale, supersedes_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "assert-illegal", "name-gencyane", "historical-name-for",
                    "concept-gentian-western", "agent-poc-seed",
                    "2026-08-12T01:00:00Z", "certain", "accepted", "test",
                    "A model may not promote this.", None,
                ),
            )
    finally:
        connection.close()


def test_assertions_reviews_evidence_and_anchor_repairs_are_append_only(tmp_path):
    database = _database(tmp_path)
    connection = sqlite3.connect(database)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="assertions are append-only"):
            connection.execute("UPDATE assertion SET rationale='mutated' WHERE id='assert-name-gencyane'")
        with pytest.raises(sqlite3.IntegrityError, match="evidence is never deleted"):
            connection.execute("DELETE FROM evidence WHERE id='evidence-gentian-identification'")
        with pytest.raises(sqlite3.IntegrityError, match="reviews are append-only"):
            connection.execute("UPDATE review SET rationale='mutated' WHERE id='review-gentian-placeholder-abstention'")
    finally:
        connection.close()


def test_assertion_predicates_enforce_endpoint_kinds(tmp_path):
    database = _database(tmp_path)
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        connection.execute("INSERT INTO node (id, kind, created_at) VALUES ('assert-wrong-shape', 'assertion', '2026-08-12T01:00:00Z')")
        with pytest.raises(sqlite3.IntegrityError, match="identified-as requires concept -> referent"):
            connection.execute(
                """
                INSERT INTO assertion (
                    id, subject_node_id, predicate, object_node_id,
                    created_by, created_at, confidence, state, method,
                    rationale, supersedes_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "assert-wrong-shape", "name-gencyane", "identified-as",
                    "referent-gentiana-lutea", "agent-poc-seed",
                    "2026-08-12T01:00:00Z", "possible", "proposed", "test",
                    "Wrong endpoint kind.", None,
                ),
            )
    finally:
        connection.close()


def test_snapshot_tampering_is_detected(tmp_path):
    database = _database(tmp_path)
    snapshot = plant_authority.export_snapshot(database, tmp_path / "snapshot.json")
    tampered = json.loads(json.dumps(snapshot))
    tampered["entities"][0]["label"] = "Changed without resealing"
    issues = plant_authority.validate_snapshot(tampered)
    assert any(issue.location == "snapshot.content_sha256" for issue in issues)

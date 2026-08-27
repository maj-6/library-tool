from __future__ import annotations

from copy import deepcopy

import server
import supabase_sync


def test_books_mirror_upsert_preserves_database_generated_identity(monkeypatch):
    rows = [
        {
            "key": "ch_library:42",
            "data": {"title": "A New Herbal"},
            "updated_at": "2026-07-19T12:00:00+00:00",
        }
    ]
    original = deepcopy(rows)
    calls = []

    def rest(cfg, method, path, payload, *, prefer):
        calls.append((cfg, method, path, deepcopy(payload), prefer))

    monkeypatch.setattr(supabase_sync, "_rest", rest)

    assert supabase_sync.push_books({"url": "test"}, rows) == 1
    assert rows == original
    assert calls == [
        (
            {"url": "test"},
            "POST",
            "books?on_conflict=key",
            original,
            "resolution=merge-duplicates,return=minimal",
        )
    ]
    assert "id" not in calls[0][3][0]


def test_phone_books_projection_keeps_textual_priority_but_omits_copy_prose(
        monkeypatch, tmp_path):
    client_state = tmp_path / "client_state.json"
    manual_entries = tmp_path / "manual_entries.json"
    monkeypatch.setattr(server.lib, "CLIENT_STATE_PATH", client_state)
    monkeypatch.setattr(server.lib, "MANUAL_ENTRIES_PATH", manual_entries)
    server.lib.save_json(client_state, {
        "checked": [["ch_library:0", {
            "book": {
                "title": "Checked Herbal",
                "marked_price": "7/6",
                "scan_priority": "High",
                "scan_verdict": "Private verdict.",
                "source_sha256": "a" * 64,
                "annotation_revision": "cha-" + "b" * 64,
            },
        }]],
    })
    server.lib.save_json(manual_entries, {
        "manual-one": {
            "id": "manual-one",
            "title": "Manual Herbal",
            "marked_price": "£1/10/-",
            "scan_priority": "Low",
            "scan_verdict": "Private verdict.",
        },
    })

    rows = {row["key"]: row["data"] for row in server._books_mirror_rows()}

    assert rows["ch_library:0"]["scan_priority"] == "High"
    assert rows["manual:manual-one"]["scan_priority"] == "Low"
    for data in rows.values():
        assert "marked_price" not in data
        assert "scan_verdict" not in data
        assert "source_sha256" not in data
        assert "annotation_revision" not in data

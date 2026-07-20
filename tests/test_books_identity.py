from __future__ import annotations

from copy import deepcopy

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

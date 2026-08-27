from __future__ import annotations


def test_manual_entry_only_gets_edited_marker_from_user_edit(client):
    created = client.post("/api/manual", json={"title": "New Book"}).get_json()["entry"]
    assert "edited" not in created

    preserved = client.patch(
        f"/api/manual/{created['id']}",
        json={"local_pdf": "scan.pdf", "_preserve": True},
    ).get_json()["entry"]
    assert "edited" not in preserved

    edited = client.patch(
        f"/api/manual/{created['id']}",
        json={"author": "Ada Author", "_edited": True},
    ).get_json()["entry"]
    assert edited["edited"] is True


def test_manual_delete_restore_repeats_without_source_hash_drift(client):
    import server

    snapshot = client.post(
        "/api/manual",
        json={
            "title": "Stable source identity",
            "marked_price": "7/6",
            "scan_priority": "High",
            "scan_verdict": "Preserve this assessment binding.",
        },
    ).get_json()["entry"]
    entry_id = snapshot["id"]
    source_sha256 = snapshot["source_sha256"]
    record_revision = snapshot["record_revision"]

    for _attempt in range(2):
        deleted = client.delete(f"/api/manual/{entry_id}")
        assert deleted.status_code == 200, deleted.get_json()
        restored = client.post(
            "/api/manual/restore",
            json={"entry": snapshot},
        )
        assert restored.status_code == 200, restored.get_json()
        snapshot = restored.get_json()["entry"]
        assert snapshot["source_sha256"] == source_sha256
        assert snapshot["record_revision"] == record_revision
        stored = server.lib.load_json(
            server.lib.MANUAL_ENTRIES_PATH,
            {},
        )[entry_id]
        assert "record_revision" not in stored
        assert "source_sha256" not in stored

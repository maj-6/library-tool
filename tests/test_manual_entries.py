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

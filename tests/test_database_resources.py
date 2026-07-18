from __future__ import annotations

import os
import sqlite3
from datetime import datetime


def test_database_status_includes_resource_metadata(client, monkeypatch, tmp_path):
    import server

    editions = tmp_path / "ol_search.db"
    with sqlite3.connect(editions) as con:
        con.execute("CREATE TABLE ed(id INTEGER PRIMARY KEY, title TEXT)")
        con.executemany("INSERT INTO ed(id, title) VALUES(?, ?)", [
            (1, "A Modern Herbal"),
            (2, "Culpeper's Complete Herbal"),
        ])

    catalog = tmp_path / "whl_catalog.csv"
    catalog.write_text(
        'Title,Authors,"Year Published"\n'
        '"Herbs, Vol. I","A. Author",1901\n'
        '"A title with\na wrapped line","B. Author",1910\n',
        encoding="utf-8",
    )
    stamp = 1_700_000_000
    os.utime(editions, (stamp, stamp))
    os.utime(catalog, (stamp, stamp))

    paths = {
        "output/ol_search.db": editions,
        "whl_catalog.csv": catalog,
    }
    monkeypatch.setattr(server, "_db_local", lambda rel: paths.get(rel))
    monkeypatch.setattr(server, "_db_urls", lambda: {
        "ol_search": "https://downloads.example/ol_search.db",
    })
    server._db_count_cache.clear()

    response = client.get("/api/db/status")
    assert response.status_code == 200
    targets = response.get_json()["targets"]

    search = targets["ol_search"]
    assert search["present"] is True
    assert search["loaded"] is True
    assert search["format"] == "SQLite 3 with FTS5"
    assert search["entries"] == 2
    assert search["entry_unit"] == "editions"
    assert search["size"] == editions.stat().st_size
    assert search["origin"].startswith("Open Library")
    assert search["description"]
    assert search["resolved_path"] == str(editions)
    assert search["location"] == "Local file"
    assert search["url"] == "https://downloads.example/ol_search.db"
    assert datetime.fromisoformat(search["updated_at"]).utcoffset() is not None

    whl = targets["whl_catalog"]
    assert whl["entries"] == 2  # quoted embedded newline is one CSV record
    assert whl["entry_unit"] == "catalog entries"
    assert whl["format"] == "UTF-8 CSV"

    missing = targets["ol_works"]
    assert missing["present"] is False
    assert missing["loaded"] is False
    assert missing["entries"] is None
    assert missing["updated_at"] == ""
    assert missing["resolved_path"] == ""
    assert missing["description"]

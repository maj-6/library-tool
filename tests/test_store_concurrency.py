"""Serialized JSON-store writes: concurrent read-modify-writes must never
drop each other's records (issue #100). Exercises the route-vs-background
races the per-store locks close, the build editor's optimistic concurrency,
and save_json's collision-safe temp names."""
from __future__ import annotations

import json
import threading

import libcommon as lib
import server


def _create(client, title: str) -> dict:
    return client.post("/api/builds",
                       json={"build": {"title": title}}).get_json()["build"]


def test_patch_survives_concurrent_analyze_summary(client):
    """A background writer hammering one build while the route PATCHes
    another: both changes must land (the lost-update the lock prevents)."""
    a = _create(client, "Background target")
    b = _create(client, "Editor target")

    stop = threading.Event()

    def hammer():
        while not stop.is_set():
            server._save_analyze_summary(a["id"], "background summary")

    t = threading.Thread(target=hammer)
    t.start()
    try:
        for i in range(40):
            r = client.patch(f"/api/builds/{b['id']}", json={"notes": f"edit {i}"})
            assert r.status_code == 200
    finally:
        stop.set()
        t.join()
    server._save_analyze_summary(a["id"], "background summary")   # at least once

    builds = lib.load_json(server.BUILDS_PATH, {})
    assert builds[b["id"]]["notes"] == "edit 39"
    assert builds[a["id"]]["description"] == "background summary"


def test_concurrent_correction_adds_all_retained():
    """Two writers appending corrections in parallel: every add survives."""
    n = 25
    failures: list[str] = []

    def add_rows(tag: str):
        c = server.app.test_client()
        for i in range(n):
            r = c.post("/api/whl_catalog", json={"add": {"title": f"{tag}-{i}"}})
            if r.status_code != 200:
                failures.append(f"{tag}-{i}: {r.status_code}")

    threads = [threading.Thread(target=add_rows, args=(tag,))
               for tag in ("corr-one", "corr-two")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not failures
    corr = lib.load_json(server.WHL_CORRECTIONS_PATH, {})
    titles = {r.get("title") for r in corr.get("added") or []}
    expected = {f"corr-one-{i}" for i in range(n)} | {f"corr-two-{i}" for i in range(n)}
    assert expected <= titles


def test_build_update_stale_expectation_conflicts(client):
    b = _create(client, "Optimistic")
    original_token = b["updated_at"]
    first = client.patch(f"/api/builds/{b['id']}", json={
        "notes": "first", "expect_updated_at": original_token})
    current = first.get_json()["build"]
    assert current["updated_at"] != original_token

    # This used to pass whenever both writes landed in the same wall-clock
    # second because the token was rounded to seconds.
    same_second_stale = client.patch(f"/api/builds/{b['id']}", json={
        "notes": "lost update", "expect_updated_at": original_token})
    assert same_second_stale.status_code == 409
    assert same_second_stale.get_json()["build"]["notes"] == "first"

    stale = client.patch(f"/api/builds/{b['id']}", json={
        "notes": "second", "expect_updated_at": "2000-01-01T00:00:00+00:00"})
    assert stale.status_code == 409
    body = stale.get_json()
    assert body["ok"] is False
    assert body["build"]["notes"] == "first"     # the current record comes back

    fresh = client.patch(f"/api/builds/{b['id']}", json={
        "notes": "third", "expect_updated_at": current["updated_at"]})
    assert fresh.status_code == 200
    assert fresh.get_json()["build"]["notes"] == "third"

    # clients that don't send the expectation keep the old semantics
    plain = client.patch(f"/api/builds/{b['id']}", json={"notes": "fourth"})
    assert plain.status_code == 200


def test_background_build_apply_always_bumps_editor_revision(client):
    b = _create(client, "Background revision")
    old = b["updated_at"]

    token = server._builds_apply(b["id"], {"title_pages": "1,3"})
    assert token and token != old
    current = lib.load_json(server.BUILDS_PATH, {})[b["id"]]
    assert current["updated_at"] == token

    stale = client.patch(f"/api/builds/{b['id']}", json={
        "notes": "stale editor", "expect_updated_at": old})
    assert stale.status_code == 409
    assert stale.get_json()["build"]["title_pages"] == "1,3"


def test_folder_sync_returns_revision_from_legacy_preview_rename(
        client, data_root, monkeypatch):
    b = _create(client, "Legacy preview")
    entry = server._entry_dir(b["id"])
    entry.mkdir(parents=True, exist_ok=True)
    legacy = entry / "preview.pdf"
    legacy.write_bytes(b"%PDF-legacy")
    rel = legacy.resolve().relative_to(data_root.resolve()).as_posix()
    before = server._builds_apply(b["id"], {"pdf_file": rel})

    generated = data_root / "generated-preview.pdf"
    generated.write_bytes(b"%PDF-primary")
    monkeypatch.setattr(server, "_preview_pdf", lambda _src, _pages: generated)
    monkeypatch.setattr(
        server, "_pdf_extract_text", lambda _src: (1, 1, "page text", 1))

    response = client.post(
        f"/api/builds/{b['id']}/folder", json={"keep_original": True})
    assert response.status_code == 200
    returned = response.get_json()["build"]
    saved = lib.load_json(server.BUILDS_PATH, {})[b["id"]]

    assert returned["pdf_file"].endswith(f"/{b['id']}/primary.pdf")
    assert returned["updated_at"] == saved["updated_at"]
    assert returned["updated_at"] != before
    assert not legacy.exists()


def test_save_json_concurrent_same_path_never_corrupts(data_root):
    path = data_root / "output" / "hammer.json"
    rounds = 200
    errors: list[Exception] = []

    def writer(tag: str):
        try:
            for i in range(rounds):
                lib.save_json(path, {"tag": tag, "i": i})
        except Exception as exc:  # a shared temp name raises FileNotFoundError
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(t,)) for t in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    doc = json.loads(path.read_text(encoding="utf-8"))   # parseable = not torn
    assert doc["i"] == rounds - 1                        # last write won
    assert not list(path.parent.glob("hammer.json.tmp*"))

"""Legacy entry writers serialize with aggregate item lifecycle deletion."""

from __future__ import annotations

import threading

import pytest

import server


def _seed_item(bid: str, *, status: str = "ready") -> dict:
    builds = server.lib.load_json(server.BUILDS_PATH, {})
    record = {
        "id": bid,
        "title": "Lifecycle guard",
        "status": status,
        "updated_at": "record-1",
    }
    builds[bid] = record
    server.lib.save_json(server.BUILDS_PATH, builds)
    return record


@pytest.mark.parametrize(
    ("method", "suffix", "payload"),
    (
        ("POST", "folder", {}),
        ("POST", "ocr", {"name": "manual.txt", "text": "late"}),
        ("PUT", "ocr-templates", {
            "src": "primary", "name": "recto", "from_page": 1,
        }),
        ("DELETE", "ocr-templates", {
            "src": "primary", "name": "recto",
        }),
        ("POST", "ocr-templates/apply", {
            "src": "primary", "name": "recto", "pages": [1],
        }),
        ("PUT", "replica-style", {"styles": {"body": {
            "family": "serif", "size_em": 1, "leading": 1.4,
        }}}),
        ("DELETE", "replica-style", None),
        ("PUT", "replica-instructions", {"text": "late"}),
        ("PUT", "about", {"text": "late"}),
        ("PUT", "annotations", {"remove": "note-1"}),
        ("PATCH", "passages", {"exclude": ["passage-1"]}),
        ("PUT", "eval", {"add": {"text": "late", "kind": "fact"}}),
    ),
)
def test_guarded_legacy_writes_do_not_recreate_a_missing_item_tree(
        client, method, suffix, payload):
    bid = "gone-live-write"
    builds = server.lib.load_json(server.BUILDS_PATH, {})
    builds.pop(bid, None)
    server.lib.save_json(server.BUILDS_PATH, builds)
    entry = server._entry_dir(bid)
    assert not entry.exists()

    response = client.open(
        f"/api/builds/{bid}/{suffix}", method=method, json=payload,
    )

    assert response.status_code == 404
    assert not entry.exists()


def test_pdf_text_missing_save_target_keeps_get_read_only(client, data_root):
    """The legacy GET keeps its response but cannot mint an orphan entry."""

    import fitz

    bid = "gone-pdf-save"
    builds = server.lib.load_json(server.BUILDS_PATH, {})
    builds.pop(bid, None)
    server.lib.save_json(server.BUILDS_PATH, builds)
    pdf = data_root / "guard-source.pdf"
    doc = fitz.open()
    for page_number in range(2):
        page = doc.new_page()
        page.insert_text((50, 50), f"page {page_number + 1}")
    doc.save(str(pdf))
    doc.close()

    response = client.get(
        "/api/pdf/text",
        query_string={"path": str(pdf), "pages": 0, "save_build": bid},
    )

    assert response.status_code == 200
    assert response.get_json()["ok"] is True
    assert "saved" not in response.get_json()
    assert not server._entry_dir(bid).exists()


def test_lifecycle_delete_wins_before_a_waiting_legacy_write(
        client, monkeypatch):
    """A writer queued behind deletion revalidates and cannot resurrect."""

    bid = "delete-wins-write"
    _seed_item(bid)
    entry = server._entry_dir(bid)
    entry.mkdir(parents=True, exist_ok=True)
    (entry / "existing.txt").write_text("managed", encoding="utf-8")
    state = client.get(f"/api/v1/items/{bid}/lifecycle").get_json()
    headers = {
        "Idempotency-Key": "delete-wins-write-operation",
        "If-Record-Match": f'"{state["item_revision"]}"',
        "If-Managed-Tree-Match": f'"{state["managed_tree_revision"]}"',
    }

    publish_entered = threading.Event()
    allow_delete = threading.Event()
    writer_started = threading.Event()
    writer_done = threading.Event()
    results: dict[str, object] = {}
    errors: list[BaseException] = []
    write_set = server._ensure_engine_session().write_set
    original_hook = write_set._publish_hook

    def pause_delete(_index, _target):
        publish_entered.set()
        assert allow_delete.wait(timeout=5)

    monkeypatch.setattr(write_set, "_publish_hook", pause_delete)

    def delete_item():
        try:
            with server.app.test_client() as threaded_client:
                results["delete"] = threaded_client.delete(
                    f"/api/v1/items/{bid}", headers=headers,
                )
        except BaseException as exc:  # surfaced on the main test thread
            errors.append(exc)

    def write_item():
        writer_started.set()
        try:
            with server.app.test_client() as threaded_client:
                results["write"] = threaded_client.post(
                    f"/api/builds/{bid}/ocr",
                    json={"name": "late.txt", "text": "must not return"},
                )
        except BaseException as exc:  # surfaced on the main test thread
            errors.append(exc)
        finally:
            writer_done.set()

    delete_thread = threading.Thread(target=delete_item)
    writer_thread = threading.Thread(target=write_item)
    try:
        delete_thread.start()
        assert publish_entered.wait(timeout=5)
        writer_thread.start()
        assert writer_started.wait(timeout=5)
        assert not writer_done.wait(timeout=0.1)
        allow_delete.set()
        delete_thread.join(timeout=5)
        writer_thread.join(timeout=5)
    finally:
        allow_delete.set()
        if delete_thread.ident is not None:
            delete_thread.join(timeout=5)
        if writer_thread.ident is not None:
            writer_thread.join(timeout=5)
        monkeypatch.setattr(write_set, "_publish_hook", original_hook)

    assert not delete_thread.is_alive() and not writer_thread.is_alive()
    assert errors == []
    assert results["delete"].status_code == 200
    assert results["write"].status_code == 404
    assert bid not in server.lib.load_json(server.BUILDS_PATH, {})
    assert not entry.exists()


def test_figure_rework_revalidates_after_remote_generation(
        client, monkeypatch):
    """Deletion may proceed during the model call; output is then discarded."""

    bid = "delete-during-rework"
    _seed_item(bid)
    ocr = server._entry_dir(bid) / "ocr"
    image = ocr / "images" / "p1-fig.png"
    image.parent.mkdir(parents=True, exist_ok=True)
    image.write_bytes(b"original-image")
    server.lib.save_json(ocr / "layout.json", {"images": {
        image.name: {
            "page": 1, "x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4,
            "src_key": "primary",
        },
    }})

    model_entered = threading.Event()
    allow_model = threading.Event()
    results: dict[str, object] = {}
    errors: list[BaseException] = []

    monkeypatch.setattr(server, "_img_gen_cfg", lambda: {
        "provider": "openai", "model": "fake",
    })
    monkeypatch.setattr(server, "_secret_is_configured",
                        lambda key: key == "imgGenKey")

    def fake_generate(_cfg, _raw, _mime, _prompt):
        model_entered.set()
        assert allow_model.wait(timeout=5)
        return b"generated-image"

    monkeypatch.setattr(server, "_img_gen", fake_generate)

    def rework():
        try:
            with server.app.test_client() as threaded_client:
                results["rework"] = threaded_client.post(
                    f"/api/builds/{bid}/rework-figure",
                    json={"figure": image.name},
                )
        except BaseException as exc:  # surfaced on the main test thread
            errors.append(exc)

    thread = threading.Thread(target=rework)
    thread.start()
    try:
        assert model_entered.wait(timeout=5)
        deleted = client.delete(f"/api/builds/{bid}")
        assert deleted.status_code == 200, deleted.get_json()
        allow_model.set()
        thread.join(timeout=5)
    finally:
        allow_model.set()
        thread.join(timeout=5)

    assert not thread.is_alive()
    assert errors == []
    assert results["rework"].status_code == 404
    assert not server._entry_dir(bid).exists()

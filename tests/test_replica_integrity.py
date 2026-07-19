"""Integrity contracts for Replica's canonical page store.

These tests deliberately sit above the layout heuristics.  They pin the rules
that make the working store safe to put several future UIs in front of:
stable identities, conditional replacement, protected human pages,
side-effect-free export, and import filtering at the page boundary.
"""
from __future__ import annotations

import io
import json
import zipfile

import libformat
import pytest
import server


BOOK_ID = "b-" + "a" * 32


def _seed_build(bid: str, **extra) -> None:
    builds = server.lib.load_json(server.BUILDS_PATH, {})
    builds[bid] = {"id": bid, "title": "Integrity " + bid, **extra}
    server.lib.save_json(server.BUILDS_PATH, builds)


def _item(text: str = "text", *, rid: str | None = None,
          ext: dict | None = None) -> dict:
    item = {
        "role": "body", "order": 0,
        "box": {"x": 0.2, "y": 0.1, "w": 0.6, "h": 0.7},
        "text": text,
    }
    if rid is not None:
        item["rid"] = rid
    if ext is not None:
        item["ext"] = ext
    return item


def _get(client, bid: str, page: int, src: str = "primary"):
    return client.get(
        f"/api/builds/{bid}/ocr-regions?src={src}&page={page}")


def _put(client, bid: str, page: int, *, items: list,
         revision: str | None = None, **extra):
    if revision is None:
        revision = _get(client, bid, page).get_json()["revision"]
    payload = {"page": page, "items": items, "expect_revision": revision,
               **extra}
    return client.put(f"/api/builds/{bid}/ocr-regions", json=payload)


def _archive(*, pages: dict[int, dict], translations: dict | None = None,
             book_id: str = BOOK_ID) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("book.json", json.dumps({
            "format_version": "2.0", "book_id": book_id,
            "source": "primary", "pages": sorted(pages),
        }))
        for page, record in pages.items():
            z.writestr(f"pages/{page}.json", json.dumps({
                "page": page, **record,
            }))
        for lang, member in (translations or {}).items():
            z.writestr(f"translations/{lang}.json", json.dumps(member))
    return buf.getvalue()


def _tree_snapshot(root) -> tuple[tuple[str, ...], dict[str, bytes]]:
    if not root.exists():
        return (), {}
    dirs = tuple(sorted(
        p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_dir()))
    files = {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in root.rglob("*") if p.is_file()
    }
    return dirs, files


def test_region_get_returns_revision_and_matching_etag(client):
    bid = "integrity-etag"
    _seed_build(bid)

    absent = _get(client, bid, 1)
    body = absent.get_json()
    assert body["ok"] and body["found"] is False
    assert body["revision"].startswith("rr-")
    assert absent.headers["ETag"] == f'"{body["revision"]}"'

    saved = _put(client, bid, 1, items=[_item()])
    assert saved.status_code == 200
    record = saved.get_json()
    assert record["found"] is True
    assert record["revision"] != body["revision"]
    assert saved.headers["ETag"] == f'"{record["revision"]}"'

    loaded = _get(client, bid, 1)
    assert loaded.get_json() == {k: v for k, v in record.items()
                                 if k != "count"}
    assert loaded.headers["ETag"] == saved.headers["ETag"]


def test_region_put_requires_revision_and_rejects_stale_revision(client):
    bid = "integrity-cas"
    _seed_build(bid)
    url = f"/api/builds/{bid}/ocr-regions"

    missing = client.put(url, json={"page": 1, "items": [_item("missing")]})
    assert missing.status_code == 428
    assert missing.get_json()["conflict"] == "region_revision_required"
    assert _get(client, bid, 1).get_json()["found"] is False

    initial = _get(client, bid, 1).get_json()["revision"]
    first = _put(client, bid, 1, revision=initial,
                 items=[_item("first", rid="first-region")])
    assert first.status_code == 200
    current = first.get_json()

    stale = _put(client, bid, 1, revision=initial,
                 items=[_item("lost update", rid="stale-region")])
    assert stale.status_code == 409
    conflict = stale.get_json()
    assert conflict["conflict"] == "stale_region_revision"
    assert conflict["revision"] == current["revision"]
    assert conflict["items"][0]["text"] == "first"
    assert stale.headers["ETag"] == f'"{current["revision"]}"'

    # A stale empty replacement is just as destructive and must not delete.
    stale_delete = _put(client, bid, 1, revision=initial, items=[])
    assert stale_delete.status_code == 409
    assert _get(client, bid, 1).get_json()["items"][0]["text"] == "first"


def test_region_put_returns_canonical_rids_and_round_trips_page_ext(client):
    bid = "integrity-canonical"
    _seed_build(bid)
    page_ext = {"org.example.page": {"hand": "A"}}
    region_ext = {"org.example.region": {"certainty": 0.8}}

    saved = _put(client, bid, 3, items=[_item(ext=region_ext)],
                 ext=page_ext, doc="compiled.txt",
                 dims={"w": 1400, "h": 1800, "dpi": 200})
    assert saved.status_code == 200
    first = saved.get_json()
    rid = first["items"][0]["rid"]
    assert len(rid) == 32 and set(rid) <= set("0123456789abcdef")
    assert first["ext"] == page_ext
    assert first["items"][0]["ext"] == region_ext

    # If-Match is the HTTP spelling of the same precondition. Omitting page
    # ext means preserve it for compatibility with older clients.
    changed = dict(first["items"][0])
    changed["text"] = "corrected"
    response = client.put(
        f"/api/builds/{bid}/ocr-regions",
        json={"page": 3, "doc": first["doc"], "dims": first["dims"],
              "items": [changed]},
        headers={"If-Match": f'"{first["revision"]}"'})
    assert response.status_code == 200
    second = response.get_json()
    assert second["items"][0]["rid"] == rid
    assert second["items"][0]["ext"] == region_ext
    assert second["ext"] == page_ext
    assert second["revision"] != first["revision"]


def test_live_region_put_rejects_duplicate_rids_before_writing(client):
    bid = "integrity-live-duplicates"
    _seed_build(bid)
    duplicate = "same-region"

    within = _put(client, bid, 1, items=[
        _item("a", rid=duplicate),
        {**_item("b", rid=duplicate),
         "box": {"x": 0.2, "y": 0.82, "w": 0.6, "h": 0.1}, "order": 1},
    ])
    assert within.status_code == 400
    assert within.get_json()["duplicate_rids"] == [duplicate]
    assert _get(client, bid, 1).get_json()["found"] is False

    assert _put(client, bid, 1,
                items=[_item("page one", rid=duplicate)]).status_code == 200
    across = _put(client, bid, 2,
                  items=[_item("page two", rid=duplicate)])
    assert across.status_code == 409
    assert across.get_json()["duplicate_rids"] == [duplicate]
    assert _get(client, bid, 2).get_json()["found"] is False


def test_duplicate_lib_rids_are_validation_errors_and_import_is_atomic(client):
    bid = "integrity-lib-duplicates"
    _seed_build(bid)
    duplicate = "duplicate-across-pages"
    raw = _archive(pages={
        1: {"items": [_item("one", rid=duplicate)]},
        2: {"items": [_item("two", rid=duplicate)]},
    })

    lint = client.post(
        "/api/lib/validate",
        data={"lib": (io.BytesIO(raw), "duplicate.lib")},
        content_type="multipart/form-data")
    assert lint.status_code == 200
    report = lint.get_json()
    assert report["ok"] is False
    assert any("duplicate rid" in issue["msg"] for issue in report["errors"])

    before = _tree_snapshot(server._entry_dir(bid))
    imported = client.post(
        f"/api/builds/{bid}/replica-import",
        data={"lib": (io.BytesIO(raw), "duplicate.lib")},
        content_type="multipart/form-data")
    assert imported.status_code == 400
    assert "duplicate region identity" in imported.get_json()["error"]
    assert _tree_snapshot(server._entry_dir(bid)) == before


def test_protected_reocr_stores_proposal_without_replacing_regions(client):
    bid = "integrity-reocr-proposal"
    _seed_build(bid)
    saved = _put(
        client, bid, 5, state="verified",
        ext={"org.example.review": {"editor": "curator"}},
        items=[_item("human reading", rid="human-region",
                     ext={"org.example": {"note": "keep"}})])
    assert saved.status_code == 200
    before = saved.get_json()

    action = server._ocr_save_page_regions(
        bid, "primary", 5,
        [_item("new machine reading")],
        {"w": 1600, "h": 2000, "dpi": 200},
        doc="compiled.txt", protect_existing=True, provider="mistral")
    assert action == "proposed"

    after = _get(client, bid, 5).get_json()
    assert after["state"] == "verified"
    assert after["doc"] == before["doc"]
    assert after["dims"] == before["dims"]
    assert after["items"] == before["items"]
    assert after["ext"] == before["ext"]
    assert after["stale"]["provider"] == "mistral"
    assert after["proposal"]["provider"] == "mistral"
    assert after["proposal"]["reason"] == "protected-page"
    assert after["proposal"]["items"][0]["text"] == "new machine reading"
    proposed_rid = after["proposal"]["items"][0]["rid"]
    assert len(proposed_rid) == 32
    assert after["proposal"]["base_revision"] == after["revision"]

    layout = client.get(f"/api/builds/{bid}/ocr-layout").get_json()
    assert layout["region_proposal_pages"] == {"primary": [5]}
    assert layout["region_stale_pages"] == {"primary": [5]}


def test_import_filters_protected_pages_and_their_translations(client):
    bid = "integrity-import-filter"
    _seed_build(bid)
    local = _put(client, bid, 1,
                 items=[_item("local human page", rid="local-page-one")])
    assert local.status_code == 200
    local_before = local.get_json()

    raw = _archive(
        pages={
            1: {"state": "verified",
                "items": [_item("foreign replacement", rid="foreign-one")]},
            2: {"state": "verified",
                "items": [_item("foreign new page", rid="foreign-two")]},
        },
        translations={"en": {
            "lang": "en", "pages": {
                "1": {"_page": "translation of protected page"},
                "2": {"_page": "translation of imported page"},
            },
        }})
    imported = client.post(
        f"/api/builds/{bid}/replica-import?overwrite=1",
        data={"lib": (io.BytesIO(raw), "incoming.lib")},
        content_type="multipart/form-data")
    assert imported.status_code == 200
    receipt = imported.get_json()
    assert receipt["pages_applied"] == [2]
    assert receipt["pages_protected"] == [1]
    assert receipt["translations_added"] == ["en"]
    assert any("verified state imported as advisory" in w["msg"]
               for w in receipt["warnings"])

    page1 = _get(client, bid, 1).get_json()
    assert page1["revision"] == local_before["revision"]
    assert page1["items"][0]["text"] == "local human page"
    page2 = _get(client, bid, 2).get_json()
    assert page2["state"] == ""
    assert page2["items"][0]["text"] == "foreign new page"
    stored = server.lib.load_json(
        server._entry_dir(bid) / "ocr" / "layout.json", {})[
            "regions"]["primary"]["2"]
    assert stored["imported_state"] == "verified"

    translated = server._an_pages(server._read_entry_text(
        bid, "translations/en.txt"))
    assert translated == {2: "translation of imported page"}


def test_export_is_pure_and_legacy_ids_are_stable(client):
    bid = "integrity-pure-export"
    _seed_build(bid)
    entry = server._entry_dir(bid)
    layout = entry / "ocr" / "layout.json"
    server.lib.save_json(layout, {"regions": {"primary": {"4": {
        "doc": "compiled.txt", "dims": {}, "origin": "internal",
        "items": [{
            "id": "r0", "role": "body", "src_type": "machine", "order": 0,
            "box": {"x": 0.2, "y": 0.1, "w": 0.6, "h": 0.7},
            "text": "legacy without rid",
        }],
    }}}})
    before = _tree_snapshot(entry)
    assert not server._lib_id_path(bid).exists()

    first = client.get(f"/api/builds/{bid}/replica-export")
    second = client.get(f"/api/builds/{bid}/replica-export")
    assert first.status_code == second.status_code == 200
    assert _tree_snapshot(entry) == before
    assert not server._lib_id_path(bid).exists()

    z1 = zipfile.ZipFile(io.BytesIO(first.data))
    z2 = zipfile.ZipFile(io.BytesIO(second.data))
    rid1 = json.loads(z1.read("pages/4.json"))["items"][0]["rid"]
    rid2 = json.loads(z2.read("pages/4.json"))["items"][0]["rid"]
    assert rid1 == rid2
    assert rid1.startswith("legacy-") and len(rid1) == len("legacy-") + 32
    assert json.loads(z1.read("book.json"))["book_id"] == \
        json.loads(z2.read("book.json"))["book_id"]
    assert "rid" not in server.lib.load_json(
        layout, {})["regions"]["primary"]["4"]["items"][0]


def test_proposal_action_requires_both_revisions_and_dismisses_safely(client):
    bid = "integrity-proposal-dismiss"
    _seed_build(bid)
    canonical = _put(
        client, bid, 6, state="verified", doc="compiled.txt",
        dims={"w": 1200, "h": 1800, "dpi": 300},
        ext={"org.example.review": {"editor": "A"}},
        items=[_item("curated text", rid="curated-region")])
    assert canonical.status_code == 200
    canonical_before = canonical.get_json()
    assert server._ocr_save_page_regions(
        bid, "primary", 6, [_item("machine replacement")],
        {"w": 1600, "h": 2000, "dpi": 300}, doc="compiled.txt",
        protect_existing=True, provider="test-ocr",
        proposed_text="machine transcription") == "proposed"

    offered = _get(client, bid, 6).get_json()
    proposal_revision = offered["proposal"]["revision"]
    canonical_revision = offered["revision"]
    url = f"/api/builds/{bid}/ocr-region-proposals"
    base = {"page": 6, "action": "dismiss"}

    missing_canonical = client.post(url, json={
        **base, "expect_proposal_revision": proposal_revision,
    })
    assert missing_canonical.status_code == 428
    assert missing_canonical.get_json()["conflict"] == \
        "proposal_revision_required"
    missing_proposal = client.post(url, json={
        **base, "expect_revision": canonical_revision,
    })
    assert missing_proposal.status_code == 428
    assert missing_proposal.get_json()["conflict"] == \
        "proposal_revision_required"

    for canonical_token, proposal_token in (
            ("rr-stale", proposal_revision),
            (canonical_revision, "rp-stale")):
        stale = client.post(url, json={
            **base,
            "expect_revision": canonical_token,
            "expect_proposal_revision": proposal_token,
        })
        assert stale.status_code == 409
        conflict = stale.get_json()
        assert conflict["conflict"] == "stale_proposal_revision"
        assert conflict["proposal"]["revision"] == proposal_revision
        assert conflict["revision"] == canonical_revision

    dismissed = client.post(url, json={
        **base,
        "expect_revision": canonical_revision,
        "expect_proposal_revision": proposal_revision,
    })
    assert dismissed.status_code == 200
    body = dismissed.get_json()
    assert body["action"] == "dismissed" and body["compiled"] is True
    assert "proposal" not in body and body["stale"] == {}
    for field in ("doc", "dims", "state", "ext", "items"):
        assert body[field] == canonical_before[field]

    loaded = _get(client, bid, 6).get_json()
    assert "proposal" not in loaded and loaded["stale"] == {}
    meta = server.lib.load_json(
        server._entry_dir(bid) / "ocr" / "layout.json", {})
    assert "region_proposals" not in meta


def test_proposal_apply_accepts_canonical_record_and_compiles_proposed_text(
        client):
    bid = "integrity-proposal-apply"
    _seed_build(bid)
    assert _put(
        client, bid, 7, state="verified", doc="compiled.txt",
        ext={"org.example.page": {"shelfmark": "MS 7"}},
        items=[_item("old curated text", rid="old-curated")]
    ).status_code == 200
    proposed_text = "provider transcription\nwith its own line ordering"
    assert server._ocr_save_page_regions(
        bid, "primary", 7,
        [_item("region-composed text", rid="proposed-region")],
        {"w": 2000, "h": 3000, "dpi": 400}, doc="compiled.txt",
        protect_existing=True, provider="test-ocr",
        proposed_text=proposed_text) == "proposed"
    offered = _get(client, bid, 7).get_json()

    applied = client.post(
        f"/api/builds/{bid}/ocr-region-proposals",
        json={
            "page": 7, "action": "apply",
            "expect_revision": offered["revision"],
            "expect_proposal_revision": offered["proposal"]["revision"],
        })
    assert applied.status_code == 200
    body = applied.get_json()
    assert body["action"] == "applied" and body["compiled"] is True
    assert body["dims"] == {"w": 2000, "h": 3000, "dpi": 400}
    assert body["state"] == ""
    assert body["ext"] == {"org.example.page": {"shelfmark": "MS 7"}}
    assert body["items"][0]["rid"] == "proposed-region"
    assert body["items"][0]["text"] == "region-composed text"
    assert body["items"][0]["src_type"] == "human"
    assert "proposal" not in body and "compile_pending" not in body

    meta = server.lib.load_json(
        server._entry_dir(bid) / "ocr" / "layout.json", {})
    record = meta["regions"]["primary"]["7"]
    assert record["origin"] == "human"
    assert "region_proposals" not in meta
    assert "region_compile_pending" not in meta
    compiled = (server._entry_dir(bid) / "ocr" / "compiled.txt").read_text(
        encoding="utf-8")
    assert server._an_pages(compiled)[7] == proposed_text
    assert "region-composed text" not in compiled


@pytest.mark.parametrize("text_only", [False, True],
                         ids=["regions", "text-only-empty-regions"])
def test_failed_proposal_compile_is_pending_and_recompile_recovers(
        client, monkeypatch, text_only):
    bid = f"integrity-pending-{'text' if text_only else 'regions'}"
    page = 8 if text_only else 9
    _seed_build(bid)
    assert _put(
        client, bid, page, state="verified", doc="compiled.txt",
        items=[_item("old canonical", rid=f"old-{page}")]
    ).status_code == 200
    proposed_text = ("text-only provider output" if text_only else
                     "provider output awaiting compile")
    if text_only:
        action = server._ocr_drop_page_regions_for_doc(
            bid, "primary", page, "compiled.txt", provider="test-ocr",
            proposed_text=proposed_text)
    else:
        action = server._ocr_save_page_regions(
            bid, "primary", page,
            [_item("accepted region text", rid=f"new-{page}")],
            {"w": 1200, "h": 1800}, doc="compiled.txt",
            protect_existing=True, provider="test-ocr",
            proposed_text=proposed_text)
    assert action == "proposed"
    offered = _get(client, bid, page).get_json()

    real_merge = server._ocr_merge_page

    def fail_merge(*_args, **_kwargs):
        raise RuntimeError("injected compile failure")

    monkeypatch.setattr(server, "_ocr_merge_page", fail_merge)
    applied = client.post(
        f"/api/builds/{bid}/ocr-region-proposals",
        json={
            "page": page, "action": "apply",
            "expect_revision": offered["revision"],
            "expect_proposal_revision": offered["proposal"]["revision"],
        })
    assert applied.status_code == 202
    body = applied.get_json()
    assert body["action"] == "applied" and body["compiled"] is False
    assert "injected compile failure" in body["warning"]
    assert body["compile_pending"]["text"] == proposed_text
    assert "proposal" not in body
    assert body["found"] is (not text_only)

    pending = _get(client, bid, page).get_json()
    assert pending["compile_pending"]["text"] == proposed_text
    meta_path = server._entry_dir(bid) / "ocr" / "layout.json"
    meta = server.lib.load_json(meta_path, {})
    assert meta["region_compile_pending"]["primary"][str(page)][
        "text"] == proposed_text
    assert "region_proposals" not in meta
    if text_only:
        assert str(page) not in ((meta.get("regions") or {}).get(
            "primary") or {})
    else:
        assert meta["regions"]["primary"][str(page)]["origin"] == "human"

    monkeypatch.setattr(server, "_ocr_merge_page", real_merge)
    recovered = client.post(
        f"/api/builds/{bid}/ocr-regions/recompile",
        json={"src": "primary", "page": page})
    assert recovered.status_code == 200
    assert recovered.get_json() == {
        "ok": True, "pages": 1, "docs": ["compiled.txt"],
    }
    after = _get(client, bid, page).get_json()
    assert "compile_pending" not in after
    meta = server.lib.load_json(meta_path, {})
    assert "region_compile_pending" not in meta
    compiled = (server._entry_dir(bid) / "ocr" / "compiled.txt").read_text(
        encoding="utf-8")
    assert server._an_pages(compiled)[page] == proposed_text


def test_layout_family_route_returns_pure_capability_proposal(client):
    bid = "integrity-layout-family-proposal"
    _seed_build(bid)
    repeated = [
        {**_item("running head"), "role": "header", "order": 0,
         "box": {"x": 0.2, "y": 0.04, "w": 0.6, "h": 0.04}},
        {**_item("body"), "order": 1,
         "box": {"x": 0.16, "y": 0.14, "w": 0.68, "h": 0.7}},
        {**_item("7"), "role": "page-number", "order": 2,
         "box": {"x": 0.78, "y": 0.93, "w": 0.05, "h": 0.025}},
    ]
    for page in (1, 3):
        assert _put(
            client, bid, page, items=repeated,
            dims={"w": 1200, "h": 1800, "dpi": 300}).status_code == 200

    entry = server._entry_dir(bid)
    before = _tree_snapshot(entry)
    url = f"/api/builds/{bid}/ocr-layout-families/propose"
    payload = {"src": "primary", "similarity_threshold": 0.8}
    first = client.post(url, json=payload)
    second = client.post(url, json=payload)
    assert first.status_code == second.status_code == 200
    assert first.get_json() == second.get_json()
    assert _tree_snapshot(entry) == before

    body = first.get_json()
    assert body["ok"] is True
    assert body["capability"] == "replica.layout-families.propose@1"
    proposal = body["proposal"]
    assert proposal["status"] == "proposal"
    assert proposal["canonical"] is False
    assert proposal["page_count"] == 2
    assert proposal["input_revision"].startswith("lfi-")
    assert len(proposal["families"]) == 1
    assert proposal["families"][0]["member_pages"] == [1, 3]

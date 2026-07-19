"""The `.lib` format's `lib/2` revision (docs/lib-format.md): the standalone
libformat module (sanitizers, read/write, the linter) and the server's lib/2
export/import + POST /api/lib/validate.

These sit alongside the lib/1 round-trip suite in test_layout_regions.py; the
overlap is deliberate — lib/1 files must keep importing forever, so the older
tests pin the upgrade path while these pin what lib/2 adds.
"""
from __future__ import annotations

import io
import json
import zipfile

import libformat


TEST_BOOK_ID = "b-" + "1" * 32


def _put(client, bid, body):
    payload = dict(body)
    src = str(payload.get("src") or "primary")
    page = payload.get("page", 0)
    loaded = client.get(
        f"/api/builds/{bid}/ocr-regions?src={src}&page={page}").get_json()
    payload.setdefault("expect_revision", loaded["revision"])
    return client.put(
        f"/api/builds/{bid}/ocr-regions", json=payload).get_json()


def _seed_build(bid, **extra):
    import libcommon as lib
    import server
    builds = lib.load_json(server.BUILDS_PATH, {})
    builds[bid] = {"id": bid, "title": "T " + bid, **extra}
    lib.save_json(server.BUILDS_PATH, builds)


def _lib1(book_extra=None, pages=None, figures=None):
    """A minimal lib/1 archive in memory, for the upgrade path + fixtures."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        book = {"format": "lib/1", "source": "primary",
                "figures": figures or {}}
        book.update(book_extra or {})
        z.writestr("book.json", json.dumps(book))
        for n, items in (pages or {1: [
            {"role": "body", "order": 0,
             "box": {"x": 0.2, "y": 0.1, "w": 0.6, "h": 0.7},
             "text": "text"}]}).items():
            z.writestr(f"pages/{n}.json", json.dumps({"page": n, "items": items}))
    buf.seek(0)
    return buf


# --- the sanitizers, at the function level -----------------------------------

def test_sanitize_page_items_parity_and_rid():
    # the fields the old server sanitizer guaranteed: kebab role (bad -> body),
    # boxes clamped into the page, degenerate boxes dropped, text/norm capped,
    # re-idd and re-ordered by `order`. lib/2 adds a rid to every kept region.
    items = libformat.sanitize_page_items([
        {"role": "marginalia", "order": 5,
         "box": {"x": 0.05, "y": 0.3, "w": 0.1, "h": 0.06}, "text": "note"},
        {"role": "Body<script>", "order": 1,          # bad role -> body
         "box": {"x": 0.9, "y": 0.9, "w": 0.5, "h": 0.5}, "text": "clamp",
         "norm": "N"},
        {"role": "body", "order": 2,                   # zero-area -> dropped
         "box": {"x": 0.2, "y": 0.2, "w": 0, "h": 0.4}, "text": "gone"},
    ])
    assert [(i["role"], i["order"], i["id"], i["src_type"]) for i in items] == [
        ("body", 0, "r0", "human"), ("marginalia", 1, "r1", "human")]
    b = items[0]["box"]
    assert b["x"] + b["w"] <= 1.0 and b["y"] + b["h"] <= 1.0
    assert items[0]["norm"] == "N"
    # Every newly minted identity carries the full 128 bits of UUID entropy;
    # the dropped region minted none.
    assert all(libformat.RID_RE.match(i["rid"]) for i in items)
    assert all(len(i["rid"]) == 32 and
               set(i["rid"]) <= set("0123456789abcdef") for i in items)
    assert len({i["rid"] for i in items}) == 2

    # text over the cap truncates
    long = libformat.sanitize_page_items([
        {"role": "body", "order": 0,
         "box": {"x": 0, "y": 0, "w": 1, "h": 1}, "text": "x" * 30000}])
    assert len(long[0]["text"]) == 20000


def test_sanitize_preserves_incoming_rid():
    items = libformat.sanitize_page_items([
        {"role": "body", "order": 0, "rid": "keep_ME-1",
         "box": {"x": 0, "y": 0, "w": 0.5, "h": 0.5}, "text": "t"},
        {"role": "body", "order": 1, "rid": "../evil",   # unsafe -> minted
         "box": {"x": 0, "y": 0.6, "w": 0.5, "h": 0.3}, "text": "t"}])
    assert items[0]["rid"] == "keep_ME-1"
    assert items[1]["rid"] != "../evil" and libformat.RID_RE.match(items[1]["rid"])


def test_sanitize_warn_collects_coercions():
    seen = []
    libformat.sanitize_page_items([
        {"role": "NOPE!", "order": 0,
         "box": {"x": 0, "y": 0, "w": 0.5, "h": 0.5}, "text": "x" * 30000},
        {"role": "body", "order": 1,
         "box": {"x": 0, "y": 0, "w": 0, "h": 0}, "text": "y"},
    ], warn=lambda loc, msg: seen.append(msg))
    joined = " | ".join(seen)
    assert "coerced to 'body'" in joined
    assert "truncated" in joined
    assert "no area" in joined


def test_sanitize_ext_caps_and_round_trips():
    seen = []
    assert libformat.sanitize_ext({"a": {"b": 1}}) == {"a": {"b": 1}}
    assert libformat.sanitize_ext(None) == {}
    assert libformat.sanitize_ext(["nope"], warn=lambda loc, m: seen.append(m)) == {}
    big = {"blob": "x" * (libformat.MAX_EXT + 10)}
    assert libformat.sanitize_ext(big, warn=lambda loc, m: seen.append(m)) == {}
    assert any("exceeds" in m for m in seen) and any("not an object" in m
                                                     for m in seen)
    # non-finite numbers can't ride into a member no strict parser reads
    assert libformat.sanitize_ext({"n": float("inf")}) == {}


def test_parse_format():
    assert libformat.parse_format({"format": "lib/1"}) == (1, 0)
    assert libformat.parse_format({"format_version": "2.0"}) == (2, 0)
    assert libformat.parse_format({"format_version": "2.7"}) == (2, 7)
    assert libformat.parse_format({"format_version": "2"}) is None
    assert libformat.parse_format({}) is None


# --- lib/2 export ------------------------------------------------------------

def test_export_is_lib2_with_self_description(client, data_root):
    bid = "e2b012340001"
    _seed_build(bid, published_slug="herbal-two")
    _put(client, bid, {"page": 7, "doc": "compiled.txt", "state": "verified",
                       "items": [
        {"role": "body", "order": 0,
         "box": {"x": 0.2, "y": 0.1, "w": 0.6, "h": 0.7},
         "text": "Oliues", "norm": "Olives"}]})
    z = zipfile.ZipFile(io.BytesIO(
        client.get(f"/api/builds/{bid}/replica-export").data))
    names = set(z.namelist())
    assert {"book.json", "pages/7.json", "INSTRUCTIONS.md",
            "schema.json"} <= names

    book = json.loads(z.read("book.json"))
    assert book["format_version"] == "2.0"
    assert book["book_id"].startswith("b-")
    assert book["generator"].startswith("library-tool/")
    assert book["capabilities"] and "rid" in book["capabilities"]
    # the vocabulary travels as data, furniture flag derived from layout_roles
    assert book["roles"]["marginalia"]["furniture"] is True
    assert book["roles"]["body"]["furniture"] is False
    assert book["instructions"]["general_ref"] == "INSTRUCTIONS.md"

    page = json.loads(z.read("pages/7.json"))
    assert libformat.RID_RE.match(page["items"][0]["rid"])

    # INSTRUCTIONS.md renders the role table from the live vocabulary; the
    # schema is real JSON with both document shapes
    md = z.read("INSTRUCTIONS.md").decode("utf-8")
    assert "| role | furniture | meaning |" in md and "`marginalia`" in md
    schema = json.loads(z.read("schema.json"))
    assert schema["$schema"].endswith("2020-12/schema")
    assert "book" in schema["$defs"] and "page" in schema["$defs"]


def test_book_id_and_rid_are_stable_across_reexport(client, data_root):
    bid = "e2b012340002"
    _seed_build(bid)
    _put(client, bid, {"page": 3, "items": [
        {"role": "body", "order": 0,
         "box": {"x": 0.2, "y": 0.1, "w": 0.6, "h": 0.7}, "text": "t"}]})
    z1 = zipfile.ZipFile(io.BytesIO(
        client.get(f"/api/builds/{bid}/replica-export").data))
    z2 = zipfile.ZipFile(io.BytesIO(
        client.get(f"/api/builds/{bid}/replica-export").data))
    assert (json.loads(z1.read("book.json"))["book_id"] ==
            json.loads(z2.read("book.json"))["book_id"])
    assert (json.loads(z1.read("pages/3.json"))["items"][0]["rid"] ==
            json.loads(z2.read("pages/3.json"))["items"][0]["rid"])


# --- rid + ext round-trip through import -------------------------------------

def test_rid_and_ext_round_trip_export_import_export(client, data_root):
    import libcommon as lib
    import server
    src_bid, dst_bid = "e2b012340003", "e2b012340004"
    _seed_build(src_bid)
    _seed_build(dst_bid)
    _put(client, src_bid, {"page": 5, "items": [
        {"role": "body", "order": 0,
         "box": {"x": 0.2, "y": 0.1, "w": 0.6, "h": 0.7}, "text": "t",
         "ext": {"vendor": {"tag": "A"}}}]})
    # a manifest-level ext, persisted in its sidecar, must round-trip too
    lib.save_json(server._lib_manifest_ext_path(src_bid), {"m": {"k": 1}})

    exported = client.get(f"/api/builds/{src_bid}/replica-export").data
    src_rid = json.loads(zipfile.ZipFile(io.BytesIO(exported)).read(
        "pages/5.json"))["items"][0]["rid"]

    r = client.post(f"/api/builds/{dst_bid}/replica-import",
                    data={"lib": (io.BytesIO(exported), "b.lib")},
                    content_type="multipart/form-data").get_json()
    assert r["ok"] and r["pages_applied"] == [5]

    got = client.get(f"/api/builds/{dst_bid}/ocr-regions?page=5").get_json()
    assert got["items"][0]["rid"] == src_rid            # rid preserved
    assert got["items"][0]["ext"] == {"vendor": {"tag": "A"}}
    assert server._lib_manifest_ext(dst_bid) == {"m": {"k": 1}}

    # re-export the destination: rid + both ext levels survive a full lap
    z = zipfile.ZipFile(io.BytesIO(
        client.get(f"/api/builds/{dst_bid}/replica-export").data))
    assert json.loads(z.read("pages/5.json"))["items"][0]["rid"] == src_rid
    assert json.loads(z.read("book.json"))["ext"] == {"m": {"k": 1}}


def test_translations_member_round_trips(client, data_root):
    import server
    bid, bid2 = "e2b012340005", "e2b012340006"
    _seed_build(bid)
    _seed_build(bid2)
    _put(client, bid, {"page": 2, "items": [
        {"role": "body", "order": 0,
         "box": {"x": 0.2, "y": 0.1, "w": 0.6, "h": 0.7}, "text": "t"}]})
    tdir = server._entry_dir(bid) / "translations"
    tdir.mkdir(parents=True, exist_ok=True)
    (tdir / "ja.txt").write_text("--- page 2 ---\nオリーブ\n",
                                 encoding="utf-8")

    exported = client.get(f"/api/builds/{bid}/replica-export").data
    member = json.loads(zipfile.ZipFile(io.BytesIO(exported)).read(
        "translations/ja.json"))
    assert member["lang"] == "ja" and "_page" in member["pages"]["2"]

    r = client.post(f"/api/builds/{bid2}/replica-import",
                    data={"lib": (io.BytesIO(exported), "b.lib")},
                    content_type="multipart/form-data").get_json()
    assert r["translations_added"] == ["ja"]
    got = server._read_entry_text(bid2, "translations/ja.txt")
    assert got.startswith("--- page 2 ---") and "オリーブ" in got


# --- lib/1 upgrade path ------------------------------------------------------

def test_lib1_file_still_imports_and_upgrades(client, data_root):
    bid = "e2b012340007"
    _seed_build(bid)
    r = client.post(f"/api/builds/{bid}/replica-import",
                    data={"lib": (_lib1(), "old.lib")},
                    content_type="multipart/form-data").get_json()
    assert r["ok"] and r["pages_applied"] == [1]
    assert r["format_version"] == "1.0"          # read as 1.0, upgraded
    # rids are minted for the upgraded regions
    got = client.get(f"/api/builds/{bid}/ocr-regions?page=1").get_json()
    assert libformat.RID_RE.match(got["items"][0]["rid"])


def test_v1_replica_import_returns_full_replayable_engine_receipt(
        client, data_root):
    bid = "e2b012340007-v1"
    _seed_build(bid)
    archive = _lib1().getvalue()
    url = (
        f"/api/v1/items/{bid}/replica/lib-imports?source_id=primary"
    )
    headers = {"Idempotency-Key": "import-command-1"}

    first = client.post(
        url,
        headers=headers,
        data={"lib": (io.BytesIO(archive), "old.lib")},
        content_type="multipart/form-data",
    )
    assert first.status_code == 200
    body = first.get_json()
    assert body["schema"] == "librarytool.lib-import-receipt/1"
    receipt = body["receipt"]
    assert receipt["operation_id"] == "import-command-1"
    assert receipt["item_id"] == bid
    assert receipt["source_id"] == "primary"
    assert receipt["pages_applied"] == [1]
    assert receipt["compiled_pages"] == [1]
    assert isinstance(receipt["figures_added"], list)
    assert receipt["stylesheet_disposition"] == "none"

    replay = client.post(
        url,
        headers=headers,
        data={"lib": (io.BytesIO(archive), "renamed.lib")},
        content_type="multipart/form-data",
    )
    assert replay.status_code == 200
    assert replay.get_json() == body

    conflict = client.post(
        url + "&overwrite=1",
        headers=headers,
        data={"lib": (io.BytesIO(archive), "old.lib")},
        content_type="multipart/form-data",
    )
    assert conflict.status_code == 409
    assert conflict.get_json()["code"] == "operation_id_conflict"


def test_v1_replica_import_requires_explicit_transport_preconditions(
        client, data_root):
    bid = "e2b012340007-preconditions"
    _seed_build(bid)
    archive = _lib1().getvalue()
    base = f"/api/v1/items/{bid}/replica/lib-imports"

    missing_key = client.post(
        base + "?source_id=primary",
        data={"lib": (io.BytesIO(archive), "old.lib")},
        content_type="multipart/form-data",
    )
    assert missing_key.status_code == 428
    assert missing_key.get_json()["code"] == "idempotency_key_required"

    missing_source = client.post(
        base,
        headers={"Idempotency-Key": "missing-source"},
        data={"lib": (io.BytesIO(archive), "old.lib")},
        content_type="multipart/form-data",
    )
    assert missing_source.status_code == 400
    assert missing_source.get_json()["code"] == "source_id_required"

    invalid_overwrite = client.post(
        base + "?source_id=primary&overwrite=yes",
        headers={"Idempotency-Key": "invalid-overwrite"},
        data={"lib": (io.BytesIO(archive), "old.lib")},
        content_type="multipart/form-data",
    )
    assert invalid_overwrite.status_code == 400
    assert invalid_overwrite.get_json()["code"] == "invalid_overwrite"


def test_import_rejects_newer_major(client, data_root):
    bid = "e2b012340008"
    _seed_build(bid)
    r = client.post(f"/api/builds/{bid}/replica-import",
                    data={"lib": (_lib1({"format": None,
                                         "format_version": "9.0"}), "future.lib")},
                    content_type="multipart/form-data")
    assert r.status_code == 400 and "newer" in r.get_json()["error"]


# --- honest receipt: warnings ------------------------------------------------

def test_receipt_warns_on_coerced_role_and_skipped_figure(client, data_root):
    import libcommon as lib
    import server
    bid = "e2b012340009"
    _seed_build(bid)
    # a figure the destination already owns -> the import must skip it, loudly
    img_dir = server._entry_dir(bid) / "ocr" / "images"
    img_dir.mkdir(parents=True, exist_ok=True)
    (img_dir / "fig.jpeg").write_bytes(b"\xff\xd8mine")
    mp = server._entry_dir(bid) / "ocr" / "layout.json"
    meta = lib.load_json(mp, {})
    meta.setdefault("images", {})["fig.jpeg"] = {"page": 1, "src_key": "primary"}
    lib.save_json(mp, meta)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("book.json", json.dumps({
            "format_version": "2.0",
            "book_id": TEST_BOOK_ID,
            "figures": {"fig.jpeg": {"page": 1}}}))
        z.writestr("pages/1.json", json.dumps({"page": 1, "items": [
            {"role": "INVALID ROLE", "order": 0,
             "box": {"x": 0.1, "y": 0.1, "w": 0.5, "h": 0.5}, "text": "x"}]}))
        z.writestr("assets/img/fig.jpeg", b"\xff\xd8incoming")
    buf.seek(0)
    r = client.post(f"/api/builds/{bid}/replica-import",
                    data={"lib": (buf, "b.lib")},
                    content_type="multipart/form-data").get_json()
    assert r["ok"] and r["figures_added"] == 0
    msgs = " || ".join(w["msg"] for w in r["warnings"])
    assert "coerced to 'body'" in msgs
    assert "figure skipped" in msgs
    # the destination's original figure is untouched
    assert (img_dir / "fig.jpeg").read_bytes() == b"\xff\xd8mine"


# --- §2.6 the rework-of overwrite rule ---------------------------------------

def _fig_lib(rework_of=None, body=b"\x89PNGnew"):
    buf = io.BytesIO()
    fig = {"page": 1}
    if rework_of:
        fig["rework_of"] = rework_of
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("book.json", json.dumps({
            "format_version": "2.0", "book_id": TEST_BOOK_ID,
            "figures": {"fig.png": fig}}))
        z.writestr("pages/1.json", json.dumps({"page": 1, "items": [
            {"role": "body", "order": 0,
             "box": {"x": 0.1, "y": 0.1, "w": 0.5, "h": 0.5}, "text": "t"}]}))
        z.writestr("assets/img/fig.png", body)
    buf.seek(0)
    return buf


def test_rework_of_overwrite_rule(client, data_root):
    import libcommon as lib
    import server
    bid = "e2b012340010"
    _seed_build(bid)
    img_dir = server._entry_dir(bid) / "ocr" / "images"
    img_dir.mkdir(parents=True, exist_ok=True)
    (img_dir / "fig.png").write_bytes(b"\x89PNGoriginal")
    mp = server._entry_dir(bid) / "ocr" / "layout.json"
    meta = lib.load_json(mp, {})
    meta.setdefault("images", {})["fig.png"] = {"page": 1, "src_key": "primary"}
    lib.save_json(mp, meta)

    # overwrite=1 but no rework_of: an accidental collision still skips
    r = client.post(f"/api/builds/{bid}/replica-import?overwrite=1",
                    data={"lib": (_fig_lib(), "b.lib")},
                    content_type="multipart/form-data").get_json()
    assert r["figures_added"] == 0
    assert any("no rework_of" in w["msg"] for w in r["warnings"])
    assert (img_dir / "fig.png").read_bytes() == b"\x89PNGoriginal"

    # overwrite=1 AND rework_of names the original: deliberate rework wins
    r = client.post(f"/api/builds/{bid}/replica-import?overwrite=1",
                    data={"lib": (_fig_lib(rework_of="fig.png"), "b.lib")},
                    content_type="multipart/form-data").get_json()
    assert r["figures_added"] == 1
    assert (img_dir / "fig.png").read_bytes() == b"\x89PNGnew"
    assert lib.load_json(mp, {})["images"]["fig.png"]["rework_of"] == "fig.png"


# --- POST /api/lib/validate --------------------------------------------------

def test_validate_endpoint_happy_and_errors(client, data_root):
    bid = "e2b012340011"
    _seed_build(bid)
    _put(client, bid, {"page": 4, "items": [
        {"role": "body", "order": 0,
         "box": {"x": 0.2, "y": 0.1, "w": 0.6, "h": 0.7}, "text": "t"}]})
    good = client.get(f"/api/builds/{bid}/replica-export").data

    v = client.post("/api/lib/validate",
                    data={"lib": (io.BytesIO(good), "b.lib")},
                    content_type="multipart/form-data").get_json()
    assert v["ok"] and v["format_version"] == "2.0" and v["pages"] == 1
    assert v["errors"] == []

    # a readable-but-too-new file: 200, ok False, an error issue
    v = client.post("/api/lib/validate",
                    data={"lib": (_lib1({"format": None,
                                         "format_version": "9.0"}), "f.lib")},
                    content_type="multipart/form-data").get_json()
    assert v["ok"] is False and v["errors"]
    assert any("newer reader" in e["msg"] for e in v["errors"])

    # not an archive at all: a clean 400
    r = client.post("/api/lib/validate",
                    data={"lib": (io.BytesIO(b"not a zip"), "x.lib")},
                    content_type="multipart/form-data")
    assert r.status_code == 400

    assert client.post("/api/lib/validate").status_code == 400   # no file


# --- the standalone Python API: read_lib / write_lib / validate --------------

def test_python_api_read_write_validate(client, data_root, tmp_path):
    bid = "e2b012340012"
    _seed_build(bid)
    _put(client, bid, {"page": 9, "items": [
        {"role": "body", "order": 0,
         "box": {"x": 0.2, "y": 0.1, "w": 0.6, "h": 0.7},
         "text": "diplomatic", "norm": ""}]})
    exported = client.get(f"/api/builds/{bid}/replica-export").data

    doc = libformat.read_lib(exported)
    assert doc.format == (2, 0) and doc.book_id.startswith("b-")
    assert len(doc.pages) == 1 and doc.pages[0].page == 9
    assert libformat.validate(doc) == [] or all(
        i.level == "warning" for i in libformat.validate(doc))

    # edit a norm layer and seal it back; the change survives a re-read
    doc.pages[0].items[0]["norm"] = "modern reading"
    out = tmp_path / "edited.lib"
    libformat.write_lib(doc, out, generator="ext-tool/1")
    again = libformat.read_lib(out)
    assert again.format == (2, 0)
    assert again.pages[0].items[0]["norm"] == "modern reading"
    assert "INSTRUCTIONS.md" in again.members

    # a garbage buffer is a LibError, not a crash
    try:
        libformat.read_lib(b"not a zip")
        assert False, "expected LibError"
    except libformat.LibError:
        pass


# --- the per-book instructions field -----------------------------------------

def test_replica_instructions_roundtrip_and_export(client, data_root):
    import server
    bid = "e2b012340013"
    _seed_build(bid)
    assert client.get("/api/builds/nope/replica-instructions").status_code == 404
    r = client.get(f"/api/builds/{bid}/replica-instructions").get_json()
    assert r["ok"] and r["text"] == ""

    note = "Latin names stay untranslated."
    r = client.put(f"/api/builds/{bid}/replica-instructions",
                   json={"text": note}).get_json()
    assert r["ok"] and r["chars"] == len(note)
    assert client.get(
        f"/api/builds/{bid}/replica-instructions").get_json()["text"] == note

    # the export embeds it: the manifest key AND the generated contract
    _put(client, bid, {"page": 1, "items": [
        {"role": "body", "order": 0,
         "box": {"x": 0.2, "y": 0.1, "w": 0.6, "h": 0.7}, "text": "t"}]})
    z = zipfile.ZipFile(io.BytesIO(
        client.get(f"/api/builds/{bid}/replica-export").data))
    assert json.loads(z.read("book.json"))["instructions"]["book"] == note
    assert note in z.read("INSTRUCTIONS.md").decode("utf-8")

    # blank clears: the sidecar goes away rather than storing whitespace
    r = client.put(f"/api/builds/{bid}/replica-instructions",
                   json={"text": "   "}).get_json()
    assert r["ok"] and r["chars"] == 0
    assert client.get(
        f"/api/builds/{bid}/replica-instructions").get_json()["text"] == ""
    assert not (server._entry_dir(bid) / "ocr" / "lib-instructions.md").exists()


# --- POST /api/lib/open: the desktop "double-clicked a .lib" flow ------------

def _fixture_lib(tmp_path, title="Fixture Herbal"):
    """A small valid lib/2 file on disk, sealed through the Python API."""
    doc = libformat.LibDocument(
        format=(2, 0),
        book={"format_version": "2.0", "source": "primary",
              "meta": {"title": title, "authors": "A. Author", "year": "1700"},
              "figures": {"fig.png": {"page": 1}}},
        pages=[libformat.LibPage(page=1, items=[
            {"role": "body", "order": 0,
             "box": {"x": 0.2, "y": 0.1, "w": 0.6, "h": 0.7},
             "text": "fixture text"}])],
        assets={"fig.png": b"\x89PNGfix"})
    p = tmp_path / "fixture.lib"
    libformat.write_lib(doc, p)
    return p


def test_lib_open_creates_book_and_imports(client, data_root, tmp_path):
    import libcommon as lib
    import server
    p = _fixture_lib(tmp_path)
    r = client.post("/api/lib/open", json={"path": str(p)}).get_json()
    assert r["ok"] and r["build_id"]
    rec = r["receipt"]
    assert rec["pages_applied"] == [1] and rec["figures_added"] == 1
    # the manifest's meta seeded the new entry through the normal create path
    b = lib.load_json(server.BUILDS_PATH, {})[r["build_id"]]
    assert b["title"] == "Fixture Herbal" and b["year"] == "1700"
    assert b["status"] == "draft"
    got = client.get(
        f"/api/builds/{r['build_id']}/ocr-regions?page=1").get_json()
    assert got["found"] and got["items"][0]["text"] == "fixture text"


def test_lib_open_refuses_bad_paths(client, data_root, tmp_path):
    # missing / relative / nonexistent / wrong suffix all refuse cleanly
    assert client.post("/api/lib/open", json={}).status_code == 400
    assert client.post("/api/lib/open",
                       json={"path": "relative.lib"}).status_code == 400
    assert client.post("/api/lib/open",
                       json={"path": str(tmp_path / "nope.lib")}).status_code == 400
    txt = tmp_path / "notes.txt"
    txt.write_text("not an archive", encoding="utf-8")
    r = client.post("/api/lib/open", json={"path": str(txt)})
    assert r.status_code == 400 and "not a .lib" in r.get_json()["error"]
    # right suffix, wrong bytes
    junk = tmp_path / "junk.lib"
    junk.write_bytes(b"not a zip")
    assert client.post("/api/lib/open",
                       json={"path": str(junk)}).status_code == 400


def test_lib_open_respects_size_cap(client, data_root, tmp_path, monkeypatch):
    p = _fixture_lib(tmp_path)
    monkeypatch.setattr(libformat, "MAX_BYTES", 16)
    r = client.post("/api/lib/open", json={"path": str(p)})
    assert r.status_code == 400 and "large" in r.get_json()["error"]


def test_lib_open_failed_import_leaves_no_shell_build(client, data_root,
                                                      tmp_path):
    import libcommon as lib
    import server
    # a well-formed archive with no usable pages: the import refuses, and the
    # build minted for it must be rolled back rather than stranded empty
    buf = tmp_path / "empty.lib"
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("book.json", json.dumps({
            "format_version": "2.0", "book_id": TEST_BOOK_ID,
            "meta": {"title": "Shell"}}))
    before = set(lib.load_json(server.BUILDS_PATH, {}))
    r = client.post("/api/lib/open", json={"path": str(buf)})
    assert r.status_code == 400
    assert "no usable pages" in r.get_json()["error"]
    assert set(lib.load_json(server.BUILDS_PATH, {})) == before


def test_lib_open_ignores_operational_meta_fields(client, data_root, tmp_path):
    import libcommon as lib
    import server
    # a foreign .lib whose manifest meta smuggles operational fields must NOT
    # pre-set them on the new build: rights stays undecided (the publication
    # gate), status draft, no foreign pdf source — only the bibliographic tuple
    # the exporter writes seeds the build. A bogus rights would also have failed
    # the open before the filter; here a VALID-looking one must still not stick.
    doc = libformat.LibDocument(
        format=(2, 0),
        book={"format_version": "2.0", "source": "primary",
              "meta": {"title": "Crafted", "authors": "X. Author",
                       "rights": "public-domain", "status": "ready",
                       "pdf_sources": [{"id": "x",
                                        "path": str(tmp_path / "secret.pdf")}],
                       "ocr_verified": True}},
        pages=[libformat.LibPage(page=1, items=[
            {"role": "body", "order": 0,
             "box": {"x": 0.2, "y": 0.1, "w": 0.6, "h": 0.7}, "text": "t"}])])
    p = tmp_path / "crafted.lib"
    libformat.write_lib(doc, p)
    r = client.post("/api/lib/open", json={"path": str(p)}).get_json()
    assert r["ok"]
    b = lib.load_json(server.BUILDS_PATH, {})[r["build_id"]]
    assert b["title"] == "Crafted" and b["authors"] == "X. Author"
    assert b["rights"] == ""            # the rights gate stays undecided
    assert b["status"] == "draft"
    assert b["pdf_sources"] == []


# --- IDX-verified review fixes -----------------------------------------------

def test_read_lib_assets_share_the_inflation_budget(monkeypatch):
    # a small deflate-bomb archive must not inflate GBs: assets draw down the
    # SAME running budget as pages, so once it is spent the rest are skipped
    # (and recorded), not read into memory. Shrink the budget so a few small
    # highly-compressible members exercise the cap without a huge fixture.
    monkeypatch.setattr(libformat, "MAX_INFLATED", 4096)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("book.json", json.dumps({"format_version": "2.0"}))
        for i in range(8):
            z.writestr(f"assets/img/z{i}.png", b"\0" * 2000)   # deflates tiny
    buf.seek(0)
    doc = libformat.read_lib(buf.getvalue())
    # total inflated footprint is bounded by the budget, not by member count
    assert sum(len(v) for v in doc.assets.values()) <= 4096
    assert len(doc.assets) < 8
    assert any("size cap" in reason for _, reason in doc.skipped)
    # validate names every skipped member so CI/import stay in lockstep
    msgs = [i.msg for i in libformat.validate(doc)]
    assert any("member skipped on read" in m for m in msgs)


def test_rework_of_must_name_the_colliding_member(client, data_root):
    import libcommon as lib
    import server
    bid = "e2b012340020"
    _seed_build(bid)
    img_dir = server._entry_dir(bid) / "ocr" / "images"
    img_dir.mkdir(parents=True, exist_ok=True)
    (img_dir / "fig.png").write_bytes(b"\x89PNGoriginal")
    mp = server._entry_dir(bid) / "ocr" / "layout.json"
    meta = lib.load_json(mp, {})
    meta.setdefault("images", {})["fig.png"] = {"page": 1, "src_key": "primary"}
    lib.save_json(mp, meta)

    # §2.6: overwrite=1 but rework_of names a DIFFERENT member is an accidental
    # collision — it still skips; only rework_of == the colliding name replaces
    r = client.post(f"/api/builds/{bid}/replica-import?overwrite=1",
                    data={"lib": (_fig_lib(rework_of="unrelated.png"), "b.lib")},
                    content_type="multipart/form-data").get_json()
    assert r["figures_added"] == 0
    assert any("no rework_of" in w["msg"] for w in r["warnings"])
    assert (img_dir / "fig.png").read_bytes() == b"\x89PNGoriginal"


def test_regions_put_preserves_incoming_rid_and_ext(client, data_root):
    # the Replica workbench now sends rid + ext with every saved region, so a
    # nudge-and-save must not re-mint rids or erase imported region ext: the PUT
    # sanitizer preserves a valid incoming rid and re-stores ext
    bid = "e2b012340021"
    _seed_build(bid)
    r = _put(client, bid, {"page": 6, "items": [
        {"rid": "keep_ME-1", "ext": {"vendor": {"tag": "A"}},
         "role": "body", "order": 0,
         "box": {"x": 0.2, "y": 0.1, "w": 0.6, "h": 0.7}, "text": "t"}]})
    assert r["ok"]
    got = client.get(f"/api/builds/{bid}/ocr-regions?page=6").get_json()
    assert got["items"][0]["rid"] == "keep_ME-1"
    assert got["items"][0]["ext"] == {"vendor": {"tag": "A"}}


def test_export_derives_stable_rids_without_mutating_legacy_regions(
        client, data_root):
    import libcommon as lib
    import server
    bid = "e2b012340022"
    _seed_build(bid)
    # A region record predating rids, written straight to the sidecar (NOT
    # through the rid-minting PUT). Export is a read: it derives a deterministic
    # compatibility RID for the archive without stamping the working store.
    mp = server._entry_dir(bid) / "ocr" / "layout.json"
    mp.parent.mkdir(parents=True, exist_ok=True)
    lib.save_json(mp, {"regions": {"primary": {"4": {
        "doc": "compiled.txt", "dims": {},
        "items": [{"id": "r0", "role": "body", "order": 0,
                   "box": {"x": 0.2, "y": 0.1, "w": 0.6, "h": 0.7},
                   "text": "t"}]}}}})
    assert "rid" not in lib.load_json(
        mp, {})["regions"]["primary"]["4"]["items"][0]
    before = mp.read_bytes()
    id_path = server._lib_id_path(bid)
    assert not id_path.exists()

    z1 = zipfile.ZipFile(io.BytesIO(
        client.get(f"/api/builds/{bid}/replica-export").data))
    rid1 = json.loads(z1.read("pages/4.json"))["items"][0]["rid"]
    assert rid1.startswith("legacy-") and len(rid1) == len("legacy-") + 32
    assert mp.read_bytes() == before
    assert "rid" not in lib.load_json(
        mp, {})["regions"]["primary"]["4"]["items"][0]
    assert not id_path.exists()

    z2 = zipfile.ZipFile(io.BytesIO(
        client.get(f"/api/builds/{bid}/replica-export").data))
    rid2 = json.loads(z2.read("pages/4.json"))["items"][0]["rid"]
    assert rid1 == rid2
    assert json.loads(z1.read("book.json"))["book_id"] == \
        json.loads(z2.read("book.json"))["book_id"]
    assert mp.read_bytes() == before and not id_path.exists()

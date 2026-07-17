"""The typed-region substrate (Phase 1 of docs/facsimile-workbench-plan.md):
layout_roles classification, the Mistral runner's region/text composition,
the regions sidecar (save/drop/renumber/endpoint), and translation source
hashes.

The P120 fixture is a real Mistral OCR-4 `include_blocks` response for page
120 of the 1605 "Haven of Health" scan (blackletter body, roman margin
notes) — the Phase 0 validation page. Coordinates are exact; contents are
trimmed. It pins the load-bearing empirical facts: marginalia arrive typed
plain `text` (never `aside_text`) and must be separated geometrically.
"""
from __future__ import annotations

import json

import layout_roles


P120_DIMS = {"dpi": 200, "height": 1798, "width": 1400}

# (type, x0, y0, x1, y1, content)
P120_BLOCKS = [
    ("header", 493, 149, 554, 178, "102"),
    ("header", 684, 133, 1079, 178, "The Hauen of Health."),
    ("text", 329, 203, 488, 295, "How to keepe\nBarberies all\nthe yeare."),
    ("text", 488, 194, 1230, 340, "and picke the leaues cleane from them, "
     "and put them\nin a potte of earth."),
    ("text", 660, 374, 1065, 418, "Of Oliues. Chap. 115."),
    ("text", 329, 774, 454, 871, "Lib 6. Simp.\nLib. 3. Diof.\ncap. 31."),
    ("text", 329, 921, 488, 1130, "A good me-\ndicine for\nthe cholicke\n"
     "and stone."),
    ("text", 329, 1245, 488, 1337, "Sacke & Salet\noile to pro-\ncure a vomit."),
    ("text", 488, 441, 1235, 1353, "O Liues if they be ripe are temperatly "
     "hot, they which\nbe greene are cold and drie."),
    ("text", 645, 1380, 1064, 1425, "Of Orenges. Chap. 116."),
    ("text", 490, 1422, 1235, 1535, "O Menges are not wholly of one "
     "temperature, for the\nrinde is hot in the first degree."),
]


def _blocks(rows):
    return [{"type": t, "top_left_x": x0, "top_left_y": y0,
             "bottom_right_x": x1, "bottom_right_y": y1, "content": c}
            for t, x0, y0, x1, y1, c in rows]


def _p120_regions():
    return layout_roles.regions_from_blocks(_blocks(P120_BLOCKS), P120_DIMS)


# --- classification on the real 1605 page -----------------------------------

def test_p120_margin_notes_become_marginalia():
    roles = [r["role"] for r in _p120_regions()]
    assert roles == ["header", "header", "marginalia", "body", "body",
                     "marginalia", "marginalia", "marginalia", "body",
                     "body", "body"]


def test_p120_boxes_are_normalized_page_fractions():
    r = _p120_regions()[2]  # "How to keepe" margin note
    assert abs(r["box"]["x"] - 329 / 1400) < 1e-4
    assert abs(r["box"]["w"] - (488 - 329) / 1400) < 1e-4
    assert 0 < r["box"]["y"] < r["box"]["y"] + r["box"]["h"] < 1


def test_p120_compose_excludes_furniture_keeps_flow():
    text = layout_roles.compose_text(_p120_regions())
    assert "How to keepe" not in text          # margin note lifted out
    assert "Hauen of Health" not in text       # running title lifted out
    assert "102" not in text                   # page number lifted out
    assert "Of Oliues. Chap. 115." in text     # chapter head stays in flow
    assert text.index("picke the leaues") < text.index("Of Oliues")


# --- the specific early-print roles ------------------------------------------

def _classify(rows):
    regions = layout_roles.regions_from_blocks(_blocks(rows), P120_DIMS)
    return {r["text"]: r["role"] for r in regions}


BODY = ("text", 420, 300, 1260, 1470, "the main text column of the page, "
        "wide and tall, anchoring the band")


def test_catchword_signature_and_page_number():
    roles = _classify([
        BODY,
        ("text", 1100, 1560, 1240, 1600, "Peares,"),   # bottom right, lone token
        ("text", 630, 1620, 700, 1660, "B2"),          # bottom center, sig code
        ("text", 1250, 80, 1330, 130, "102"),          # top corner numeral
    ])
    assert roles["Peares,"] == "catch-word"
    assert roles["B2"] == "signature-mark"
    assert roles["102"] == "page-number"


def test_roman_letter_ambiguity_defaults_to_page_number():
    # "C." at the bottom is indistinguishable from a folio number without
    # book-level context — page-number is the documented default; a digit
    # ("C2") disambiguates to signature-mark.
    roles = _classify([
        BODY,
        ("text", 630, 1620, 690, 1660, "C."),
        ("text", 900, 1620, 960, 1660, "C2"),
    ])
    assert roles["C."] == "page-number"
    assert roles["C2"] == "signature-mark"


def test_sparse_page_wide_margin_note_loses_nothing():
    # The band anchors on the centre-spanning cluster, not the widest block:
    # a margin note wider than the only body line must not eject that line
    # from the flow. The note erring toward body is the safe direction —
    # noise kept beats text lost.
    rows = [
        ("text", 28, 500, 280, 680, "A wide margin gloss"),   # w 0.18, edge
        ("text", 588, 700, 700, 740, "FINIS."),               # w 0.08, centre
    ]
    roles = _classify(rows)
    assert roles["FINIS."] == "body"
    text = layout_roles.compose_text(
        layout_roles.regions_from_blocks(_blocks(rows), P120_DIMS))
    assert "FINIS." in text


def test_classification_is_input_order_independent():
    base = layout_roles.regions_from_blocks(_blocks(P120_BLOCKS), P120_DIMS)
    expect = {r["text"]: r["role"] for r in base}
    for rows in (list(reversed(P120_BLOCKS)),
                 P120_BLOCKS[5:] + P120_BLOCKS[:5]):
        got = {r["text"]: r["role"]
               for r in layout_roles.regions_from_blocks(_blocks(rows), P120_DIMS)}
        assert got == expect


def test_two_column_page_stays_body():
    # An index's second column is band-wide — the width guard must keep it
    # body, not demote it to marginalia (Phase 0: the p305 "TABLE" page).
    roles = _classify([
        ("text", 150, 300, 580, 1500, "left column entries"),
        ("text", 620, 300, 1050, 1500, "right column entries"),
    ])
    assert set(roles.values()) == {"body"}


def test_aside_text_maps_directly_when_mistral_provides_it():
    roles = _classify([BODY, ("aside_text", 100, 500, 300, 600, "gloss")])
    assert roles["gloss"] == "marginalia"


def test_unknown_block_type_degrades_to_body():
    roles = _classify([("celestial_diagram", 400, 400, 1000, 900, "wat")])
    assert roles["wat"] == "body"


def test_coverage_guard():
    regions = _p120_regions()
    md = "\n\n".join(c for *_x, c in P120_BLOCKS)
    assert layout_roles.coverage(regions, md) > 0.95
    assert layout_roles.coverage(regions[:2], md) < 0.2   # blocks lost text


# --- the Mistral runner composes clean text ----------------------------------

def _fake_pages(monkeypatch, pages):
    import server
    monkeypatch.setattr(server.capture, "mistral_ocr_pages",
                        lambda *a, **k: pages)
    return server


def test_ocr_mistral_returns_regions_and_clean_text(monkeypatch):
    server = _fake_pages(monkeypatch, [{
        "markdown": "\n\n".join(c for *_x, c in P120_BLOCKS),
        "dimensions": P120_DIMS,
        "blocks": _blocks(P120_BLOCKS),
        "images": [],
    }])
    out = server._ocr_mistral(b"png", {"mistral_key": "k"})
    assert "How to keepe" not in out["text"]
    assert "Of Oliues. Chap. 115." in out["text"]
    assert len(out["regions"]) == len(P120_BLOCKS)
    assert out["dims"] == {"w": 1400, "h": 1798, "dpi": 200}
    assert "words" not in out    # still box-silent: word sidecar untouched


def test_ocr_mistral_falls_back_to_markdown_on_poor_coverage(monkeypatch):
    server = _fake_pages(monkeypatch, [{
        "markdown": "a page of text the blocks completely failed to carry, "
                    "long enough that two words are clearly not 70 percent",
        "dimensions": P120_DIMS,
        "blocks": _blocks([("text", 10, 10, 60, 40, "two words")]),
        "images": [],
    }])
    out = server._ocr_mistral(b"png", {"mistral_key": "k"})
    assert out["text"].startswith("a page of text")
    assert out["regions"]      # regions still saved for the workbench


# --- regions sidecar: save / drop / renumber / endpoint -----------------------

BID = "cafe12345678"


def _layout_path(server):
    return server._entry_dir(BID) / "ocr" / "layout.json"


def test_regions_sidecar_roundtrip_and_renumber(data_root):
    import server
    items = [{"id": "r0", "role": "body", "order": 0,
              "box": {"x": 0.1, "y": 0.1, "w": 0.5, "h": 0.5}, "text": "hi"}]
    server._ocr_save_page_regions(BID, "primary", 3, items,
                                  {"w": 1400, "h": 1798, "dpi": 200},
                                  doc="compiled.txt")
    meta = json.loads(_layout_path(server).read_text(encoding="utf-8"))
    rec = meta["regions"]["primary"]["3"]
    assert rec["doc"] == "compiled.txt" and rec["items"][0]["text"] == "hi"

    # page 2 deleted -> page 3 becomes page 2
    server._renumber_layout_words(BID, "primary", [2])
    meta = json.loads(_layout_path(server).read_text(encoding="utf-8"))
    assert "2" in meta["regions"]["primary"]
    assert "3" not in meta["regions"]["primary"]

    # an empty save drops the page and the emptied maps
    server._ocr_save_page_regions(BID, "primary", 2, [], None)
    meta = json.loads(_layout_path(server).read_text(encoding="utf-8"))
    assert "regions" not in meta


def test_ocr_regions_endpoint(client, data_root):
    import libcommon as lib
    import server
    builds = lib.load_json(server.BUILDS_PATH, {})
    builds[BID] = {"id": BID, "title": "T"}
    lib.save_json(server.BUILDS_PATH, builds)
    items = [{"id": "r0", "role": "marginalia", "order": 0,
              "box": {"x": 0.05, "y": 0.3, "w": 0.1, "h": 0.06},
              "text": "Lib 6. Simp."}]
    server._ocr_save_page_regions(BID, "primary", 7, items, None, doc="c.txt")

    r = client.get(f"/api/builds/{BID}/ocr-regions?page=7").get_json()
    assert r["found"] and r["doc"] == "c.txt"
    assert r["items"][0]["role"] == "marginalia"
    assert not client.get(f"/api/builds/{BID}/ocr-regions?page=8").get_json()["found"]

    layout = client.get(f"/api/builds/{BID}/ocr-layout").get_json()
    assert layout["region_pages"] == {"primary": [7]}


def test_figure_refs_rewritten_in_regions_too(data_root):
    import server
    regions = [{"id": "r0", "role": "figure", "order": 0,
                "box": {"x": 0.3, "y": 0.8, "w": 0.4, "h": 0.1},
                "text": "![img-0.jpeg](img-0.jpeg)"}]
    text = server._ocr_save_page_images(
        BID, 5, [{"id": "img-0.jpeg", "data": b"\xff", "bbox": None}],
        "![img-0.jpeg](img-0.jpeg)", regions=regions)
    assert text == "![img-0.jpeg](p5-img-0.jpeg)"
    assert regions[0]["text"] == "![img-0.jpeg](p5-img-0.jpeg)"


def test_region_record_dropped_when_its_doc_is_reocred(data_root):
    import server
    items = [{"id": "r0", "role": "body", "order": 0,
              "box": {"x": 0.1, "y": 0.1, "w": 0.5, "h": 0.5}, "text": "old"}]
    server._ocr_save_page_regions(BID, "primary", 9, items, None,
                                  doc="compiled.txt")
    # a re-OCR into a DIFFERENT target leaves the record alone…
    server._ocr_drop_page_regions_for_doc(BID, "primary", 9, "tess.txt")
    meta = json.loads(_layout_path(server).read_text(encoding="utf-8"))
    assert "9" in meta["regions"]["primary"]
    # …but rewriting the SAME doc's page supersedes the record's text
    server._ocr_drop_page_regions_for_doc(BID, "primary", 9, "compiled.txt")
    meta = json.loads(_layout_path(server).read_text(encoding="utf-8"))
    assert "9" not in (meta.get("regions", {}).get("primary") or {})


def _put(client, bid, body):
    return client.put(f"/api/builds/{bid}/ocr-regions", json=body).get_json()


def test_regions_put_sanitizes_and_saves(client, data_root):
    import libcommon as lib
    import server
    bid = "beef12345678"
    builds = lib.load_json(server.BUILDS_PATH, {})
    builds[bid] = {"id": bid, "title": "T"}
    lib.save_json(server.BUILDS_PATH, builds)

    r = _put(client, bid, {"page": 2, "doc": "compiled.txt", "items": [
        # arrives out of order; order values win
        {"role": "marginalia", "order": 5,
         "box": {"x": 0.05, "y": 0.3, "w": 0.1, "h": 0.06}, "text": "note"},
        {"role": "Body<script>", "order": 1,          # bad role -> body
         "box": {"x": 0.9, "y": 0.9, "w": 0.5, "h": 0.5}, "text": "clamped"},
        {"role": "body", "order": 2,
         "box": {"x": 0.2, "y": 0.2, "w": 0, "h": 0.4}, "text": "dropped"},
    ]})
    assert r["ok"] and r["count"] == 2
    got = client.get(f"/api/builds/{bid}/ocr-regions?page=2").get_json()
    assert got["found"] and got["doc"] == "compiled.txt"
    roles = [(i["role"], i["order"], i["src_type"]) for i in got["items"]]
    assert roles == [("body", 0, "human"), ("marginalia", 1, "human")]
    b0 = got["items"][0]["box"]
    assert b0["x"] + b0["w"] <= 1.0 and b0["y"] + b0["h"] <= 1.0  # clamped

    # an empty save drops the record
    r = _put(client, bid, {"page": 2, "items": []})
    assert r["ok"] and r["count"] == 0
    assert not client.get(f"/api/builds/{bid}/ocr-regions?page=2").get_json()["found"]

    assert _put(client, bid, {"page": 0, "items": []})["ok"] is False
    assert _put(client, bid, {"page": 1, "src": "nope", "items": []})["ok"] is False


def test_regions_put_survives_hostile_values(client, data_root):
    import libcommon as lib
    import server
    bid = "abad12345678"
    builds = lib.load_json(server.BUILDS_PATH, {})
    builds[bid] = {"id": bid, "title": "T"}
    lib.save_json(server.BUILDS_PATH, builds)

    # json.loads accepts the non-standard Infinity literal; int(inf) raises
    # OverflowError, not ValueError — both spots must answer 400/degrade
    r = client.put(f"/api/builds/{bid}/ocr-regions",
                   data='{"page": Infinity, "items": []}',
                   content_type="application/json")
    assert r.status_code == 400 and r.get_json()["ok"] is False

    r = client.put(
        f"/api/builds/{bid}/ocr-regions",
        data='{"page": 4, "dims": {"w": Infinity}, "items": '
             '[{"role": "body", "order": "x", '
             '"box": {"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2}, "text": "a"},'
             ' {"role": "body", "order": 1, '
             '"box": {"x": 0.1, "y": 0.4, "w": 0.2, "h": 0.2}, "text": "b"}]}',
        content_type="application/json")
    # mixed str/int order must not 500 the sort; Infinity dims degrade to none
    assert r.status_code == 200 and r.get_json()["count"] == 2
    got = client.get(f"/api/builds/{bid}/ocr-regions?page=4").get_json()
    assert got["found"] and got["dims"] == {}

    # a present-but-garbage recompile page refuses instead of silently
    # widening to every page
    r = client.post(f"/api/builds/{bid}/ocr-regions/recompile",
                    json={"page": 0})
    assert r.status_code == 400
    r = client.post(f"/api/builds/{bid}/ocr-regions/recompile",
                    data='{"page": "abc"}', content_type="application/json")
    assert r.status_code == 400
    # targeted recompile touches exactly the asked page
    r = client.post(f"/api/builds/{bid}/ocr-regions/recompile",
                    json={"page": 4}).get_json()
    assert r["ok"] and r["pages"] == 1


def test_regions_recompile_writes_body_only_text(client, data_root):
    import libcommon as lib
    import server
    bid = "feed12345678"
    builds = lib.load_json(server.BUILDS_PATH, {})
    builds[bid] = {"id": bid, "title": "T"}
    lib.save_json(server.BUILDS_PATH, builds)

    _put(client, bid, {"page": 3, "doc": "compiled.txt", "items": [
        {"role": "header", "order": 0,
         "box": {"x": 0.4, "y": 0.05, "w": 0.2, "h": 0.03}, "text": "RUNNING HEAD"},
        {"role": "body", "order": 1,
         "box": {"x": 0.2, "y": 0.1, "w": 0.6, "h": 0.6}, "text": "the body text"},
        {"role": "marginalia", "order": 2,
         "box": {"x": 0.02, "y": 0.2, "w": 0.1, "h": 0.1}, "text": "a gloss"},
    ]})
    r = client.post(f"/api/builds/{bid}/ocr-regions/recompile",
                    json={}).get_json()
    assert r["ok"] and r["pages"] == 1 and r["docs"] == ["compiled.txt"]
    text = (server._entry_dir(bid) / "ocr" / "compiled.txt").read_text(encoding="utf-8")
    assert "--- page 3 ---" in text
    assert "the body text" in text
    assert "a gloss" not in text and "RUNNING HEAD" not in text


# --- templates, layers, review states -------------------------------------------

def test_clip_words_to_box_and_iou():
    words = [
        {"t": "How", "l": 1, "x": 0.05, "y": 0.30, "w": 0.03, "h": 0.01},
        {"t": "to", "l": 1, "x": 0.09, "y": 0.30, "w": 0.02, "h": 0.01},
        {"t": "keepe", "l": 2, "x": 0.05, "y": 0.32, "w": 0.04, "h": 0.01},
        {"t": "body", "l": 3, "x": 0.50, "y": 0.30, "w": 0.04, "h": 0.01},  # outside
    ]
    box = {"x": 0.04, "y": 0.29, "w": 0.10, "h": 0.06}
    assert layout_roles.clip_words_to_box(words, box) == "How to\nkeepe"
    assert layout_roles.box_iou(box, box) == 1.0
    assert layout_roles.box_iou(box, {"x": 0.5, "y": 0.5, "w": 0.1, "h": 0.1}) == 0.0
    tpl = [{"box": box}]
    assert layout_roles.template_score(tpl, [{"box": dict(box)}]) == 1.0
    assert layout_roles.template_score(tpl, []) == 0.0


def test_compose_text_norm_layer_falls_back():
    regions = [
        {"role": "body", "order": 0, "text": "Waſſer", "norm": "Wasser"},
        {"role": "body", "order": 1, "text": "unchanged"},
        {"role": "marginalia", "order": 2, "text": "gloſſe", "norm": "glosse"},
    ]
    assert layout_roles.compose_text(regions) == "Waſſer\n\nunchanged"
    assert layout_roles.compose_text(regions, layer="norm") == "Wasser\n\nunchanged"


def test_templates_apply_and_outliers(client, data_root):
    import libcommon as lib
    import server
    bid = "cafe87654321"
    builds = lib.load_json(server.BUILDS_PATH, {})
    builds[bid] = {"id": bid, "title": "T"}
    lib.save_json(server.BUILDS_PATH, builds)

    # an exemplar page: body + margin note, saved and verified
    _put(client, bid, {"page": 10, "doc": "compiled.txt", "state": "verified",
                       "items": [
        {"role": "body", "order": 0,
         "box": {"x": 0.3, "y": 0.1, "w": 0.55, "h": 0.7}, "text": "body ten",
         "norm": "body ten (norm)"},
        {"role": "marginalia", "order": 1,
         "box": {"x": 0.05, "y": 0.28, "w": 0.12, "h": 0.08}, "text": "gloss"},
    ]})
    got = client.get(f"/api/builds/{bid}/ocr-regions?page=10").get_json()
    assert got["state"] == "verified" and got["items"][0]["norm"]
    layout = client.get(f"/api/builds/{bid}/ocr-layout").get_json()
    assert layout["region_states"] == {"primary": {"10": "verified"}}

    # snapshot it as a template (no text), then stamp pages 11-12; page 11
    # has stored word boxes inside the margin box, so its text pre-fills
    r = client.put(f"/api/builds/{bid}/ocr-templates",
                   json={"name": "recto", "from_page": 10}).get_json()
    assert r["ok"] and r["items"] == 2
    assert client.get(f"/api/builds/{bid}/ocr-templates").get_json()[
        "templates"] == [{"name": "recto", "items": 2, "from_page": 10}]

    server._ocr_save_page_words(bid, "primary", 11, [
        {"t": "Lib", "l": 0, "x": 0.06, "y": 0.30, "w": 0.03, "h": 0.01},
        {"t": "6.", "l": 0, "x": 0.10, "y": 0.30, "w": 0.02, "h": 0.01},
    ], doc="tess.txt")
    r = client.post(f"/api/builds/{bid}/ocr-templates/apply",
                    json={"name": "recto", "pages": [10, 11, 12]}).get_json()
    assert r["applied"] == [11, 12] and r["skipped"] == [10]
    assert r["clipped"] == [11]
    p11 = client.get(f"/api/builds/{bid}/ocr-regions?page=11").get_json()
    texts = {i["role"]: i["text"] for i in p11["items"]}
    assert texts["marginalia"] == "Lib 6." and texts["body"] == ""
    assert p11["items"][0]["src_type"] == "template"
    assert p11["state"] == ""            # a stamped page is not verified

    # knock page 12's regions off the grid -> outlier
    _put(client, bid, {"page": 12, "doc": "compiled.txt", "items": [
        {"role": "figure", "order": 0,
         "box": {"x": 0.1, "y": 0.55, "w": 0.8, "h": 0.4}, "text": ""}]})
    r = client.post(f"/api/builds/{bid}/ocr-templates/outliers",
                    json={"name": "recto"}).get_json()
    assert r["ok"] and r["outliers"] == [12]
    assert r["scores"]["10"] == 1.0 and r["scores"]["11"] == 1.0

    # normalized layer recompiles into its own target
    r = client.post(f"/api/builds/{bid}/ocr-regions/recompile",
                    json={"layer": "normalized", "page": 10}).get_json()
    assert r["ok"] and r["docs"] == ["normalized.txt"]
    text = (server._entry_dir(bid) / "ocr" / "normalized.txt").read_text(
        encoding="utf-8")
    assert "body ten (norm)" in text and "gloss" not in text

    # unknown template and bad name refuse
    assert client.post(f"/api/builds/{bid}/ocr-templates/apply",
                       json={"name": "nope", "pages": [1]}).get_json()["ok"] is False
    assert client.put(f"/api/builds/{bid}/ocr-templates",
                      json={"name": "../x", "from_page": 10}).status_code == 400


def test_replica_export_lib(client, data_root):
    import io
    import zipfile
    import libcommon as lib
    import server
    bid = "11b012345678"
    builds = lib.load_json(server.BUILDS_PATH, {})
    builds[bid] = {"id": bid, "title": "Species Plantarum", "year": "1753",
                   "published_slug": "species-plantarum"}
    lib.save_json(server.BUILDS_PATH, builds)

    # nothing to export yet
    assert client.get(f"/api/builds/{bid}/replica-export").status_code == 400

    _put(client, bid, {"page": 3, "doc": "compiled.txt", "state": "verified",
                       "items": [
        {"role": "body", "order": 0,
         "box": {"x": 0.2, "y": 0.1, "w": 0.6, "h": 0.7},
         "text": "CANNA foliis ovatis", "norm": "Canna foliis ovatis"},
        {"role": "marginalia", "order": 1,
         "box": {"x": 0.03, "y": 0.3, "w": 0.12, "h": 0.06},
         "text": "Habitat in Indiis."},
    ]})
    client.put(f"/api/builds/{bid}/ocr-templates",
               json={"name": "recto", "from_page": 3})
    # a figure crop for this source
    img_dir = server._entry_dir(bid) / "ocr" / "images"
    img_dir.mkdir(parents=True, exist_ok=True)
    (img_dir / "p3-fig.jpeg").write_bytes(b"\xff\xd8jpegish")
    meta_path = server._entry_dir(bid) / "ocr" / "layout.json"
    meta = lib.load_json(meta_path, {})
    meta.setdefault("images", {})["p3-fig.jpeg"] = {
        "x": 0.3, "y": 0.8, "w": 0.4, "h": 0.1, "page": 3, "src_key": "primary"}
    lib.save_json(meta_path, meta)

    r = client.get(f"/api/builds/{bid}/replica-export")
    assert r.status_code == 200
    assert r.mimetype == "application/zip"
    assert "species-plantarum.lib" in r.headers.get("Content-Disposition", "")
    z = zipfile.ZipFile(io.BytesIO(r.data))
    names = set(z.namelist())
    assert {"book.json", "pages/3.json", "assets/img/p3-fig.jpeg"} <= names
    book = json.loads(z.read("book.json"))
    assert book["format"] == "lib/1"
    assert book["meta"]["title"] == "Species Plantarum"
    assert book["pages"] == [3]
    assert "recto" in book["templates"]
    assert book["figures"]["p3-fig.jpeg"]["page"] == 3
    assert book["stylesheet"]["marginalia"]["style"] == "italic"
    page = json.loads(z.read("pages/3.json"))
    assert page["state"] == "verified"
    roles = {i["role"]: i for i in page["items"]}
    assert roles["marginalia"]["text"] == "Habitat in Indiis."
    assert roles["body"]["norm"] == "Canna foliis ovatis"


def test_replica_style_roundtrip_and_export(client, data_root):
    import io
    import zipfile
    import libcommon as lib
    import server
    bid = "57e112345678"
    builds = lib.load_json(server.BUILDS_PATH, {})
    builds[bid] = {"id": bid, "title": "T"}
    lib.save_json(server.BUILDS_PATH, builds)

    r = client.get(f"/api/builds/{bid}/replica-style").get_json()
    assert r["ok"] and r["custom"] is False
    assert r["styles"]["body"]["family"] == "EB Garamond"

    r = client.put(f"/api/builds/{bid}/replica-style", json={"styles": {
        "body": {"family": "IM Fell English", "size_em": 1.1,
                 "align": "justify"},
        "marginalia": {"family": "IM Fell English", "size_em": 99,  # out of range
                       "style": "italic"},
        "bad role!!": {"family": "X"},                              # dropped
    }}).get_json()
    assert r["ok"] and r["count"] == 2

    r = client.get(f"/api/builds/{bid}/replica-style").get_json()
    assert r["custom"] is True
    assert r["styles"]["body"]["family"] == "IM Fell English"
    assert "size_em" not in r["styles"]["marginalia"]
    assert "bad role!!" not in r["styles"]

    # the export carries the stored sheet, not the seed
    _put(client, bid, {"page": 1, "items": [
        {"role": "body", "order": 0,
         "box": {"x": 0.2, "y": 0.1, "w": 0.6, "h": 0.7}, "text": "t"}]})
    z = zipfile.ZipFile(io.BytesIO(
        client.get(f"/api/builds/{bid}/replica-export").data))
    book = json.loads(z.read("book.json"))
    assert book["stylesheet"]["body"]["family"] == "IM Fell English"

    client.delete(f"/api/builds/{bid}/replica-style")
    r = client.get(f"/api/builds/{bid}/replica-style").get_json()
    assert r["custom"] is False


def test_norm_recompile_target_is_per_source(client, data_root):
    import libcommon as lib
    import server
    bid = "b00212345678"
    builds = lib.load_json(server.BUILDS_PATH, {})
    builds[bid] = {"id": bid, "title": "T",
                   "pdf_sources": [{"id": "scan2", "path": "x.pdf"}]}
    lib.save_json(server.BUILDS_PATH, builds)
    for src in ("primary", "scan2"):
        _put(client, bid, {"src": src, "page": 1, "doc": "compiled.txt",
                           "items": [{"role": "body", "order": 0,
                                      "box": {"x": 0.1, "y": 0.1,
                                              "w": 0.6, "h": 0.6},
                                      "text": f"dipl {src}",
                                      "norm": f"norm {src}"}]})
    r1 = client.post(f"/api/builds/{bid}/ocr-regions/recompile",
                     json={"layer": "norm"}).get_json()
    r2 = client.post(f"/api/builds/{bid}/ocr-regions/recompile",
                     json={"src": "scan2", "layer": "norm"}).get_json()
    # two scans must not interleave into one modern-edition file
    assert r1["docs"] == ["normalized.txt"]
    assert r2["docs"] == ["normalized-scan2.txt"]
    d = server._entry_dir(bid) / "ocr"
    assert "norm primary" in (d / "normalized.txt").read_text(encoding="utf-8")
    assert "norm scan2" in (d / "normalized-scan2.txt").read_text(encoding="utf-8")
    # the secondary's file maps back to its scan like every per-source doc
    assert server._ocr_sources(bid).get("normalized-scan2.txt") == "scan2"

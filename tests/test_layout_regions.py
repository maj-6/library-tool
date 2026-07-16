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

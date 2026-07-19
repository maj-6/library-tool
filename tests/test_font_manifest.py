"""The font manifest, its generated derivatives, and the files on disk agree.

static/fonts.css, the BUNDLED_FONTS block in static/app.js, and
static/fonts/README.md are GENERATED from static/fonts/fonts.json by
tools/fontman.py and committed, because the PyInstaller-frozen sidecar serves
static/ from a read-only _MEIPASS dir and cannot generate them at run time.
These assertions fail if someone hand-edits a derived file or forgets to
re-run `python tools/fontman.py generate`.
"""
from __future__ import annotations

import re
from pathlib import Path

import fontman

ROOT = Path(__file__).parents[1]
STATIC = ROOT / "tools" / "whl_explorer" / "static"
APP = (STATIC / "app.js").read_text(encoding="utf-8")
STYLE = (STATIC / "style.css").read_text(encoding="utf-8")
FONTS_CSS = (STATIC / "fonts.css").read_text(encoding="utf-8")
TEMPLATE = (ROOT / "tools" / "whl_explorer" / "templates" /
            "index.html").read_text(encoding="utf-8")
SERVER = (ROOT / "tools" / "whl_explorer" / "server.py").read_text(encoding="utf-8")
MAN = fontman.load()
ALL = [f for _, f in fontman.entries(MAN)]

# the theme-editor's font control: the block that builds a Settings picker
PICKER = APP[APP.index('tok.t === "font"'):APP.index("} else {", APP.index('tok.t === "font"'))]


def test_the_manifest_is_internally_consistent_and_matches_the_files_on_disk():
    # covers missing/duplicate ids, unknown kinds, absent files, and a size or
    # sha256 that drifted from the bytes actually committed
    assert fontman.problems(MAN) == []


def test_at_least_one_face_is_bundled_so_the_pickers_are_never_empty():
    assert MAN["fonts"], "static/fonts/fonts.json bundles no chrome faces"


def test_the_manifest_reserves_a_typesetting_section():
    assert isinstance(MAN["typeset"], list)


def test_typesetting_faces_are_never_offered_as_interface_fonts():
    # the reserve is structural: the Settings picker reads BUNDLED_FONTS, and
    # must not learn about TYPESET_FONTS by any route
    assert "BUNDLED_FONTS" in PICKER
    assert "TYPESET_FONTS" not in PICKER
    families = {f["family"] for f in MAN["typeset"]}
    assert families.isdisjoint({f["family"] for f in MAN["fonts"]})


def test_only_the_replica_engine_reads_the_typesetting_section():
    # every use must sit inside an rw* function -- the replica engine's prefix
    readers = []
    for m in re.finditer(r"TYPESET_FONTS", APP):
        start = APP.rfind("\n", 0, m.start()) + 1
        end = APP.find("\n", m.end())
        line = APP[start:end if end >= 0 else len(APP)]
        if line.lstrip().startswith("//") or "const TYPESET_FONTS" in line:
            continue
        fn = re.findall(r"(?m)^function (\w+)", APP[:m.start()])
        readers.append(fn[-1] if fn else "(top level)")
    assert readers, "nothing consumes the typesetting section"
    assert all(n.startswith("rw") for n in readers), readers


def test_no_generated_font_file_is_stale():
    assert fontman.stale(MAN) == [], (
        "run: python tools/fontman.py generate")


def test_generating_twice_produces_identical_output():
    assert fontman.render_css(MAN) == fontman.render_css(MAN)
    assert fontman.render_js(MAN) == fontman.render_js(MAN)
    assert fontman.render_readme(MAN) == fontman.render_readme(MAN)


def test_every_bundled_family_is_declared_by_a_font_face_rule():
    declared = set(re.findall(r'font-family:\s*"([^"]+)"', FONTS_CSS))
    assert {f["family"] for f in ALL} == declared


def test_style_css_declares_no_face_of_its_own():
    # a hand-written @font-face would be a face the pickers never list; the
    # bare words appear in the pointer comment, so match the rule syntax
    assert not re.search(r"@font-face\s*\{", STYLE)
    assert "fonts.css" in STYLE


def test_the_font_sheet_declares_faces_only_and_no_custom_properties():
    # --ui/--mono live in style.css and are overridden inline by applyFont();
    # a var declared here would fight that and lose in confusing ways
    assert "--" not in FONTS_CSS.split("*/", 1)[1]


def test_the_page_links_the_generated_font_sheet_with_a_cache_token():
    assert "filename='fonts.css'" in TEMPLATE
    assert "?v={{ fonts_v }}" in TEMPLATE
    assert 'fonts_v=_asset_v("fonts.css")' in SERVER


def test_the_font_pickers_offer_both_groups():
    # the ORDER the user sees is asserted by building the real picker in
    # tests/font_manifest_behavior.test.js; source order proves nothing here
    assert "const BUNDLED_FONTS = [" in APP
    assert 'bg.label = "Bundled"' in APP
    assert 'sg.label = "System fonts"' in APP
    # the system list survives as its own group rather than being merged away
    assert "const FONT_CHOICES = [" in APP


def test_the_generated_block_precedes_every_reader_of_it():
    # BUNDLED_FONTS/TYPESET_FONTS are const: a reader above them would hit the
    # temporal dead zone at load, not fall back to undefined
    for const in ("BUNDLED_FONTS", "TYPESET_FONTS"):
        declared = APP.index(f"const {const} = [") + len("const ")
        assert declared == min(
            m.start() for m in re.finditer(rf"\b{const}\b", APP)
            if "//" not in APP[APP.rfind("\n", 0, m.start()) + 1:m.start()])


def test_the_app_js_block_matches_the_manifest_entry_for_entry():
    body = fontman.current_js_body(APP)
    for f in ALL:
        assert f'family: "{f["family"]}"' in body
        assert f'id: "{f["id"]}"' in body


def test_bundled_stacks_survive_the_theme_import_sanitizer():
    # the sanitizer's font grammar, lifted from app.js so the two cannot drift
    m = re.search(r'THEME_FONT_VARS\.has\(k\) && !/\^(\[[^\]]+\]\+)\$/\.test\(val\)',
                  APP)
    assert m, "app.js no longer holds font vars to a family-list grammar"
    grammar = re.compile("^" + m.group(1).replace('\\"', '"') + "$")
    for f in ALL:
        assert grammar.match(f["stack"]), (
            f'{f["id"]}: stack would be stripped on theme import')
        assert not set("();") & set(f["stack"])


def test_the_replica_typeface_suggestions_lead_with_typesetting_faces():
    assert "RW_FONT_SUGGESTIONS_SYSTEM" in APP
    assert "[...TYPESET_FONTS, ...BUNDLED_FONTS]" in APP


def test_every_bundled_font_records_its_licence_and_source():
    readme = (STATIC / "fonts" / "README.md").read_text(encoding="utf-8")
    for f in ALL:
        assert f["license"].strip()
        assert f["source"].strip()
        assert f["family"] in readme, "attribution is a licence obligation"


def test_no_bundled_face_is_fetched_from_the_network():
    # the whole point of bundling: the app never reaches out for chrome assets
    assert "http://" not in FONTS_CSS
    assert "https://" not in FONTS_CSS
    assert "@import" not in FONTS_CSS
    for f in ALL:
        assert not re.match(r"^[a-z]+://", f["file"], re.I)

#!/usr/bin/env python3
"""The bundled-font manager: one manifest, three generated files.

    python3 tools/fontman.py list                 show the bundled faces
    python3 tools/fontman.py add --file f.woff2 --family "Inter" --kind sans \
        --license "SIL Open Font License 1.1" --source "Google Fonts, v4.1"
    python3 tools/fontman.py rm <id>              drop a face
    python3 tools/fontman.py generate             rewrite the derived files
    python3 tools/fontman.py verify               fail if anything is stale

WHY A MANIFEST.  The font pickers in Settings used to offer a hardcoded list of
system faces (Segoe UI, Consolas, Fira Code...), so what the user saw depended
on what happened to be installed -- a picked font could silently render as
something else, or as the fallback. The manifest is the list of faces that SHIP
with the app, so a bundled pick always renders. static/fonts/fonts.json is the
single source of truth; everything else is generated from it.

TWO SECTIONS.  "fonts" are chrome faces, offered in the Settings font pickers.
"typeset" is reserved for the Replica engine: faces chosen to set a facsimile
page, which have no business dressing the application chrome. The split is
structural rather than a flag, and it survives into the generated JS as two
separate constants -- the Settings picker reads BUNDLED_FONTS and so cannot
offer a typesetting face even by mistake. Both sections get @font-face rules;
only the section decides who may pick from them. Add with --typeset.

WHY GENERATED-AND-COMMITTED, NOT GENERATED AT RUNTIME.  The sidecar is
PyInstaller-frozen and serves static/ out of the read-only _MEIPASS extraction
dir (see server.py's frozen-aware app init). Anything written at runtime works
in a checkout and then quietly does nothing in the packaged app. So generation
happens here, at author time, and the outputs are committed:

    static/fonts.css            the @font-face blocks
    static/app.js               the BUNDLED_FONTS block, between its markers
    static/fonts/README.md      the licence/attribution list

tests/test_font_manifest.py asserts all three still match the manifest, so a
forgotten `generate` reddens CI rather than shipping a face the CSS never
declares.

STDLIB ONLY, DELIBERATELY.  No fonttools, no subsetting, no conversion: `add`
copies a font file you already built. The frozen sidecar installs no extras
(release.yml installs requirements.txt + pyinstaller and nothing else), and a
font dependency that exists only in a dev extra is exactly the kind of import
that passes locally and fails in the bundle. Convert to .woff2 with an external
tool, then hand the file to `add`.

NOT COVERED HERE.  The Electron splash and updater windows live outside the
Flask origin and cannot read /static/, so they carry their own copy of Roboto
Slab via desktop/package.json's extraResources -> startup-assets/. Adding a
face to the manifest does NOT reach those windows; see docs/fonts.md.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
from pathlib import Path

STATIC = Path(__file__).resolve().parent / "whl_explorer" / "static"
FONTS_DIR = STATIC / "fonts"
MANIFEST = FONTS_DIR / "fonts.json"
CSS_OUT = STATIC / "fonts.css"
APP_JS = STATIC / "app.js"
README_OUT = FONTS_DIR / "README.md"

# The generated block in app.js is replaced between these two exact lines, so
# the generator can never eat surrounding code.
JS_BEGIN = "// --- BUNDLED FONTS (GENERATED from static/fonts/fonts.json) ----------------"
JS_END = "// --- END BUNDLED FONTS -----------------------------------------------------"

GENERATED_BY = "tools/fontman.py from static/fonts/fonts.json"

# Manifest section -> (generated JS constant, what it is for). "fonts" dresses
# the app chrome; "typeset" is reserved for the Replica engine's facsimile
# pages and is deliberately absent from every Settings picker.
SECTIONS = {
    "fonts": ("BUNDLED_FONTS", "chrome faces, offered in the Settings pickers"),
    "typeset": ("TYPESET_FONTS", "typesetting faces, Replica engine only"),
}

# Roboto Slab is referenced by four hardcoded rules in style.css (the home-page
# wordmark and the Publish preview) and by the splash/updater windows, none of
# which consult the manifest. Removing it breaks them silently.
PINNED_IDS = {"roboto-slab"}

KINDS = {"sans": "sans-serif", "serif": "serif", "mono": "monospace",
         "display": "sans-serif"}

FORMATS = {".woff2": "woff2", ".woff": "woff", ".ttf": "truetype",
           ".otf": "opentype"}

# Mirrors the font-var grammar in app.js's sanitizeOverrides(): a stack is
# written into an inline body style, so it stays a plain family list. Keeping
# the two in step is asserted by tests/test_font_manifest.py.
STACK_RE = re.compile(r"^[A-Za-z0-9 ,._'\"-]+$")

REQUIRED = ("id", "family", "kind", "stack", "file", "format", "weight",
            "style", "display", "bytes", "sha256", "source", "license")


# --- manifest ---------------------------------------------------------------

def load() -> dict:
    try:
        data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"no manifest at {MANIFEST}")
    except json.JSONDecodeError as e:
        raise SystemExit(f"{MANIFEST} is not valid JSON: {e}")
    for section in SECTIONS:
        data.setdefault(section, [])
        if not isinstance(data[section], list):
            raise SystemExit(f"{MANIFEST}: '{section}' must be a list")
    return data


def entries(man: dict):
    """Every face, paired with the section it belongs to."""
    for section in SECTIONS:
        for font in man[section]:
            yield section, font


def save(man: dict) -> None:
    MANIFEST.write_text(json.dumps(man, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s or "font"


# --- generators (pure: text in, text out, so the tests can re-run them) ------

def render_css(man: dict) -> str:
    out = [f"/* GENERATED by {GENERATED_BY} -- DO NOT EDIT.",
           "   Regenerate: python tools/fontman.py generate",
           "   Faces only; this sheet must never declare a custom property"
           " (--ui/--mono",
           "   live in style.css and are overridden inline by applyFont()). */",
           ""]
    for section, label in (("fonts", "Chrome faces."),
                           ("typeset", "Typesetting faces (Replica engine).")):
        if not man[section]:
            continue
        out += [f"/* {label} */", ""]
        for f in man[section]:
            out += ["@font-face {",
                    f'  font-family: "{f["family"]}";',
                    f'  src: url("fonts/{f["file"]}") format("{f["format"]}");',
                    f'  font-weight: {f["weight"]};',
                    f'  font-style: {f["style"]};',
                    f'  font-display: {f["display"]};',
                    "}",
                    ""]
    return "\n".join(out).rstrip("\n") + "\n"


def render_js(man: dict) -> str:
    """The body between the app.js markers (no markers, no trailing newline)."""
    out = ["// Faces that ship with the app, so a pick here always renders.",
           "// Regenerate: python tools/fontman.py generate"]
    for section, (const, purpose) in SECTIONS.items():
        out.append(f"// {const}: {purpose}.")
        out.append(f"const {const} = [")
        for f in man[section]:
            out.append("  {{ id: {}, family: {}, kind: {},".format(
                json.dumps(f["id"]), json.dumps(f["family"]),
                json.dumps(f["kind"])))
            out.append(f"    stack: {json.dumps(f['stack'])} }},")
        out.append("];")
    return "\n".join(out)


def render_readme(man: dict) -> str:
    out = [f"<!-- GENERATED by {GENERATED_BY} -- DO NOT EDIT.",
           "     Regenerate: python tools/fontman.py generate -->",
           "",
           "# Bundled fonts",
           "",
           "Faces shipped with Library Tool. They are bundled locally so the",
           "desktop app never reaches out for chrome assets, and so the font",
           "pickers in Settings do not depend on what is installed on the",
           "machine. See `docs/fonts.md`.",
           ""]
    for section, heading, blurb in (
        ("fonts", "Chrome",
         "Offered in the Settings font pickers."),
        ("typeset", "Typesetting",
         "Reserved for the Replica engine's facsimile pages; deliberately "
         "not offered as interface fonts."),
    ):
        if not man[section]:
            continue
        out += [f"## {heading}", "", blurb, ""]
        for f in man[section]:
            out.append(f"- `{f['file']}` -- {f['family']} ({f['kind']}, "
                       f"weight {f['weight']}, {f['style']}), "
                       f"{f['bytes']:,} bytes.")
            out.append(f"  Source: {f['source']}.")
            lic = f"  Licensed under the {f['license']}"
            url = (f.get("license_url") or "").strip()
            out.append(f"{lic} ({url})." if url else f"{lic}.")
        out.append("")
    return "\n".join(out).rstrip("\n") + "\n"


def splice_js(text: str, body: str) -> str:
    """Replace the marked region of app.js, matching markers as whole lines."""
    lines = text.split("\n")
    try:
        i = lines.index(JS_BEGIN)
        j = lines.index(JS_END)
    except ValueError:
        raise SystemExit(
            f"{APP_JS}: missing the BUNDLED_FONTS markers. Expected these two "
            f"lines:\n{JS_BEGIN}\n{JS_END}")
    if j <= i:
        raise SystemExit(f"{APP_JS}: BUNDLED_FONTS end marker precedes begin")
    return "\n".join(lines[:i + 1] + body.split("\n") + lines[j:])


def current_js_body(text: str) -> str:
    lines = text.split("\n")
    try:
        i = lines.index(JS_BEGIN)
        j = lines.index(JS_END)
    except ValueError:
        raise SystemExit(f"{APP_JS}: missing the BUNDLED_FONTS markers")
    return "\n".join(lines[i + 1:j])


# --- validation -------------------------------------------------------------

def problems(man: dict) -> list[str]:
    """Everything wrong with the manifest and its derived files, as messages."""
    bad: list[str] = []
    seen_ids: set[str] = set()
    seen_families: set[str] = set()
    # ids and families must be unique ACROSS sections: they share one CSS font
    # namespace, and a family in both would make "is this a chrome face?"
    # ambiguous for the pickers.
    for n, (section, f) in enumerate(entries(man)):
        where = f.get("id") or f"{section}#{n}"
        for key in REQUIRED:
            if not str(f.get(key, "")).strip():
                bad.append(f"{where}: missing '{key}'")
        if not f.get("id"):
            continue
        if f["id"] in seen_ids:
            bad.append(f"{f['id']}: duplicate id")
        seen_ids.add(f["id"])
        fam = f.get("family", "")
        if fam in seen_families:
            bad.append(f"{f['id']}: duplicate family {fam!r}")
        seen_families.add(fam)
        if f.get("kind") not in KINDS:
            bad.append(f"{f['id']}: kind {f.get('kind')!r} not one of "
                       f"{sorted(KINDS)}")
        stack = f.get("stack", "")
        if stack and not STACK_RE.match(stack):
            bad.append(f"{f['id']}: stack {stack!r} would be rejected by the "
                       f"theme-import sanitizer in app.js")
        if stack and fam and fam not in stack:
            bad.append(f"{f['id']}: stack does not name the family {fam!r}")
        name = str(f.get("file", ""))
        if name != Path(name).name or name in (".", ".."):
            # keeps rm's unlink and the generated url() inside static/fonts/
            bad.append(f"{f['id']}: file {name!r} must be a bare filename")
            continue
        path = FONTS_DIR / name
        if not path.is_file():
            bad.append(f"{f['id']}: {path.name} is not in static/fonts/")
            continue
        size = path.stat().st_size
        if size != f.get("bytes"):
            bad.append(f"{f['id']}: {path.name} is {size} bytes, manifest "
                       f"says {f.get('bytes')}")
        digest = sha256(path)
        if digest != f.get("sha256"):
            bad.append(f"{f['id']}: {path.name} sha256 {digest[:12]}... does "
                       f"not match the manifest")
    return bad


def stale(man: dict) -> list[str]:
    """Derived files that `generate` would change."""
    out = []
    if not CSS_OUT.is_file() or CSS_OUT.read_text(encoding="utf-8") != render_css(man):
        out.append("static/fonts.css")
    if not README_OUT.is_file() or README_OUT.read_text(encoding="utf-8") != render_readme(man):
        out.append("static/fonts/README.md")
    if current_js_body(APP_JS.read_text(encoding="utf-8")) != render_js(man):
        out.append("static/app.js (BUNDLED_FONTS block)")
    return out


# --- commands ---------------------------------------------------------------

def cmd_list(args) -> None:
    man = load()
    if not any(man[s] for s in SECTIONS):
        print("no bundled fonts")
        return
    for section, (const, purpose) in SECTIONS.items():
        print(f"[{section}] {purpose} -> {const}")
        if not man[section]:
            print("    (none)")
        for f in man[section]:
            print(f"    {f['id']:<18} {f['family']:<24} {f['kind']:<8} "
                  f"{f['bytes']:>8,}B  {f['file']}")
            print(f"    {'':<18} {f['license']}")


def cmd_generate(args) -> None:
    man = load()
    bad = problems(man)
    if bad:
        for m in bad:
            print(f"error: {m}", file=sys.stderr)
        raise SystemExit(1)
    CSS_OUT.write_text(render_css(man), encoding="utf-8")
    README_OUT.write_text(render_readme(man), encoding="utf-8")
    APP_JS.write_text(splice_js(APP_JS.read_text(encoding="utf-8"),
                                render_js(man)), encoding="utf-8")
    print(f"generated {CSS_OUT.name}, {README_OUT.name}, and the font block "
          f"in {APP_JS.name} "
          f"({len(man['fonts'])} chrome, {len(man['typeset'])} typesetting)")


def cmd_verify(args) -> None:
    man = load()
    n = sum(len(man[s]) for s in SECTIONS)
    bad = problems(man)
    out_of_date = [] if bad else stale(man)
    for m in bad:
        print(f"error: {m}", file=sys.stderr)
    for m in out_of_date:
        print(f"error: {m} is stale -- run: python tools/fontman.py generate",
              file=sys.stderr)
    if bad or out_of_date:
        raise SystemExit(1)
    print(f"ok: {n} face(s), derived files match the manifest")


def cmd_add(args) -> None:
    src = Path(args.file).expanduser()
    if re.match(r"^[a-z]+://", str(args.file), re.I):
        raise SystemExit("add takes a local file path, not a URL: bundled "
                         "faces are vendored deliberately so the app never "
                         "fetches chrome assets")
    if not src.is_file():
        raise SystemExit(f"no such file: {src}")
    fmt = FORMATS.get(src.suffix.lower())
    if not fmt:
        raise SystemExit(f"unsupported font format {src.suffix!r}; "
                         f"expected one of {sorted(FORMATS)}")
    if fmt != "woff2":
        print(f"warning: {src.suffix} is several times larger than .woff2 and "
              f"ships in every installer; convert it first unless you have a "
              f"reason not to", file=sys.stderr)

    man = load()
    section = "typeset" if args.typeset else "fonts"
    font_id = args.id or slugify(args.family)
    if any(f["id"] == font_id for _, f in entries(man)):
        raise SystemExit(f"id {font_id!r} is already bundled (use --id, or "
                         f"`rm {font_id}` first)")
    if any(f["family"] == args.family for _, f in entries(man)):
        raise SystemExit(f"family {args.family!r} is already bundled")

    stack = args.stack or f'"{args.family}", {KINDS[args.kind]}'
    if not STACK_RE.match(stack):
        raise SystemExit(f"stack {stack!r} contains characters the theme-import "
                         f"sanitizer strips; allowed: letters, digits, spaces, "
                         f"quotes, commas, dots, underscores, hyphens")

    dest = FONTS_DIR / (args.filename or src.name)
    if dest.exists():
        # Font binaries are referenced from CSS with no ?v= token, so a
        # replaced file can serve up to a day stale from the browser cache.
        # Filenames are therefore immutable: a new build gets a new name.
        if sha256(dest) != sha256(src):
            raise SystemExit(
                f"{dest.name} already exists with different bytes. Font "
                f"filenames are cached for a day with no cache-busting token, "
                f"so give the new build its own name (--filename).")
    else:
        shutil.copy2(src, dest)

    man[section].append({
        "id": font_id,
        "family": args.family,
        "stack": stack,
        "kind": args.kind,
        "file": dest.name,
        "format": fmt,
        "weight": args.weight,
        "style": args.style,
        "display": "swap",
        "bytes": dest.stat().st_size,
        "sha256": sha256(dest),
        "source": args.source,
        "license": args.license,
        "license_url": args.license_url or "",
    })
    save(man)
    cmd_generate(args)
    print(f"added {args.family} as {font_id} [{section}]")


def cmd_rm(args) -> None:
    man = load()
    found = [(s, f) for s, f in entries(man) if f["id"] == args.id]
    if not found:
        raise SystemExit(f"no bundled font with id {args.id!r}")
    if args.id in PINNED_IDS and not args.force:
        raise SystemExit(
            f"{args.id} is referenced by name in style.css and by the Electron "
            f"splash/updater windows, none of which read the manifest. "
            f"Removing it breaks them silently. Pass --force if you have "
            f"already fixed those call sites.")
    section, gone = found[0]
    man[section] = [f for f in man[section] if f["id"] != args.id]
    if not any(f["file"] == gone["file"] for _, f in entries(man)):
        (FONTS_DIR / gone["file"]).unlink(missing_ok=True)
    save(man)
    cmd_generate(args)
    print(f"removed {args.id} [{section}]")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="show the bundled faces").set_defaults(fn=cmd_list)
    sub.add_parser("generate", help="rewrite the derived files").set_defaults(fn=cmd_generate)
    sub.add_parser("verify", help="fail if the manifest or derived files are stale").set_defaults(fn=cmd_verify)

    a = sub.add_parser("add", help="bundle a font file")
    a.add_argument("--file", required=True, help="local .woff2 (or .ttf/.otf/.woff) to copy in")
    a.add_argument("--family", required=True, help='CSS family name, e.g. "Inter"')
    a.add_argument("--kind", required=True, choices=sorted(KINDS))
    a.add_argument("--typeset", action="store_true",
                   help="reserve for the Replica engine: bundled and given an "
                        "@font-face, but never offered as an interface font")
    a.add_argument("--license", required=True,
                   help='e.g. "SIL Open Font License 1.1" -- attribution is a licence obligation')
    a.add_argument("--source", required=True,
                   help='where it came from, e.g. "Google Fonts (fonts.gstatic.com), v4.1, latin subset"')
    a.add_argument("--license-url", default="", help="link to the licence/upstream")
    a.add_argument("--stack", help='full CSS stack (default: "<family>", <generic for kind>)')
    a.add_argument("--id", help="manifest id (default: slug of --family)")
    a.add_argument("--filename", help="name to store it under (default: the source filename)")
    a.add_argument("--weight", default="400", help='400, or a variable range like "100 900"')
    a.add_argument("--style", default="normal", choices=["normal", "italic"])
    a.set_defaults(fn=cmd_add)

    r = sub.add_parser("rm", help="drop a bundled face")
    r.add_argument("id")
    r.add_argument("--force", action="store_true", help="remove even a face other code names directly")
    r.set_defaults(fn=cmd_rm)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()

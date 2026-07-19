# Bundled fonts

Library Tool ships the faces it offers. The font pickers in Settings list
**bundled** faces first — those are present on every machine that installs the
app, so a pick renders the same everywhere — and installed **system** faces
second, as a convenience that may or may not resolve.

Before this, the pickers offered a hardcoded list of system faces (Segoe UI,
Consolas, Fira Code…). On a machine without them a chosen font silently
rendered as something else, and there was no way to tell from inside the app.

## The pieces

| Path | Role |
| --- | --- |
| `tools/whl_explorer/static/fonts/fonts.json` | **The manifest — the only file you edit.** |
| `tools/whl_explorer/static/fonts/*.woff2` | The font binaries. |
| `tools/fontman.py` | The CLI that edits the manifest and regenerates everything else. |
| `tools/whl_explorer/static/fonts.css` | *Generated.* The `@font-face` rules. |
| `static/app.js` → the font block | *Generated.* `BUNDLED_FONTS` and `TYPESET_FONTS`. |
| `tools/whl_explorer/static/fonts/README.md` | *Generated.* Licence attribution. |

## Two sections

The manifest has two lists, and which one a face is in decides who may pick it:

| Section | Constant | Who offers it |
| --- | --- | --- |
| `fonts` | `BUNDLED_FONTS` | The Settings font pickers — interface, data/table, tag/marker. |
| `typeset` | `TYPESET_FONTS` | **The Replica engine only.** Never offered as an interface font. |

`typeset` is reserved for faces chosen to set a facsimile page — historical
book faces that belong on a reconstructed page and have no business dressing
the application chrome.

The reserve is **structural, not a filter**. The two lists generate two
separate JS constants, and the Settings picker only ever reads
`BUNDLED_FONTS` — so a typesetting face cannot leak into the chrome even
through a bug, because that code never sees it. Tests assert both that the
picker never names `TYPESET_FONTS` and that nothing outside the Replica
engine's `rw*` functions reads it.

Both sections get `@font-face` rules, so both actually render; the section
only decides who may choose them. Ids and family names must be unique across
both — they share one CSS font namespace.

The three generated files are **committed, never produced at run time**. The
sidecar is PyInstaller-frozen and serves `static/` out of a read-only `_MEIPASS`
extraction directory, so anything written at run time works in a checkout and
then quietly does nothing in the packaged app.

`tests/test_font_manifest.py` and `tests/font_manifest_behavior.test.js` assert
the generated files still match the manifest, so a forgotten `generate` fails
CI instead of shipping a face the CSS never declares.

## Adding a face

Get a `.woff2` — subset it and convert it with whatever tool you like; the
manager deliberately does no conversion (see *Design notes*). Then:

```bash
python tools/fontman.py add \
    --file ~/Downloads/inter-var.woff2 \
    --family "Inter" \
    --kind sans \
    --weight "100 900" \
    --license "SIL Open Font License 1.1" \
    --license-url "https://github.com/rsms/inter" \
    --source "Google Fonts (fonts.gstatic.com), Inter variable v4.1, latin subset"
```

That copies the file into `static/fonts/`, records its size and SHA-256, and
regenerates all three derived files. Restart the server and *Inter* is in the
**Bundled** group of every font picker.

`--kind` is one of `sans`, `serif`, `mono`, `display`. It sets the generic
fallback in the default stack (`"Inter", sans-serif`) and is stored so the
pickers can filter by role later; today they list every face for every slot,
which matches the previous behaviour.

Pass `--typeset` to put a face in the reserved typesetting section instead —
bundled and given an `@font-face`, offered at the top of the Replica engine's
typeface list, and absent from every Settings picker:

```bash
python tools/fontman.py add --typeset \
    --file ~/Downloads/ebgaramond-var.woff2 \
    --family "EB Garamond" --kind serif --weight "400 800" \
    --license "SIL Open Font License 1.1" \
    --source "Google Fonts (fonts.gstatic.com), EB Garamond variable, latin subset"
```

`EB Garamond`, `IM Fell English`, `Junicode` and the other historical names in
the Replica typeface list are currently *system* suggestions: they render only
if the reader happens to have them installed. Bundling them via `--typeset` is
what makes them dependable.

Other commands:

```bash
python tools/fontman.py list        # what is bundled
python tools/fontman.py rm inter    # drop a face and its file
python tools/fontman.py generate    # rebuild the derived files
python tools/fontman.py verify      # exit non-zero if anything is stale
```

Only ever bundle a face whose licence permits redistribution — the SIL Open
Font License is the usual choice. `--license` and `--source` are required, and
they end up in the generated `README.md`, which is how the project meets its
attribution obligation.

## Design notes

**Filenames are immutable.** Font binaries are referenced from `fonts.css` with
no `?v=` cache-busting token, so `/static/*` serves them with `max-age=86400`.
Replacing a file in place can therefore serve up to a day stale from the
browser cache. `add` refuses to overwrite an existing filename with different
bytes; give a new build its own name via `--filename`.

**No font tooling as a dependency.** `fontman.py` is stdlib-only and copies a
font file you already built. `release.yml` installs `requirements.txt` plus
PyInstaller and no extras, so a font library declared in a dev extra would be
absent from the bundle — exactly the class of import that passes locally and
fails when frozen.

**Nothing is fetched at run time.** No `@import`, no `fonts.googleapis.com`, no
remote `src:`. `add` takes a local path and rejects a URL. This is the same
promise the website makes, and a test enforces it.

**`fonts.css` declares faces only.** The `--ui` / `--mono` / `--mono2` custom
properties live in `style.css`, and `applyFont()` overrides them as inline
styles on `<body>`. A custom property declared in `fonts.css` would join that
fight and lose confusingly, so a test forbids it.

## What the manifest does *not* reach

**The Electron splash and updater windows.** `desktop/startup.html` and
`desktop/updater.html` render before the Flask sidecar is up, so they cannot
load `/static/`. They get their own copy of Roboto Slab through
`desktop/package.json` → `extraResources` → `startup-assets/`, which
`desktop/main.js` reads as a base64 data URI and installs with the `FontFace`
API. Three places there name exactly one font. Adding a face to the manifest
does not change the splash; changing the splash font means editing those files
too.

**The website.** `website/assets/fonts/` is a separate, parallel copy with its
own `@font-face` blocks in `site.css` and `library.css`. The desktop bundle is
latin-only; the website also carries a latin-ext subset. Unifying the two is
not done.

**Four rules in `style.css`** name `"Roboto Slab"` directly — the home-page
wordmark and the Publish preview, which is a deliberately theme-independent
print surface. `fontman.py rm roboto-slab` refuses without `--force` for that
reason.

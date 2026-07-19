"""The `.lib` book format — one implementation of sanitize/read/write/validate.

A `.lib` is a ZIP archive that carries a book from a Library Tool archive in a
form an *external* tool — including an AI assistant with no prior knowledge of
Library Tool — can understand, edit, and return without breaking it (see
docs/lib-format.md). This module is that format's single source of truth:

  - the sanitizers (`sanitize_page_items`/`sanitize_dims`/`sanitize_styles`/
    `sanitize_figure`) the server's export/import routes call, so the app and
    any external program scrub identically — no drift;
  - `read_lib`/`write_lib`/`validate`, the standalone Python API a tool author
    (or CI) uses to round-trip and lint a `.lib` with no Flask in sight;
  - the self-description a `lib/2` file ships: `INSTRUCTIONS.md` generated from
    the live role vocabulary and `schema.json`, so the artifact teaches its
    reader.

Depends only on the standard library + `layout_roles` (the role vocabulary),
so it is safe for external scripts and pip-installable later via the existing
pyproject.
"""
from __future__ import annotations

import io
import json
import os
import re
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import layout_roles

# --- the format's constants ------------------------------------------------

# format_version is "MAJOR.MINOR": MINOR is additive (new optional keys only),
# a higher MAJOR breaks and an importer must reject it. lib/1 files upgrade to
# 1.0 on read. See docs/lib-format.md §2.3.
FORMAT_VERSION = "2.0"
SUPPORTED_MAJOR = 2

# What this writer's files declare they contain — a reader can feature-detect
# without sniffing the members.
CAPABILITIES = ["norm-layer", "templates", "figures", "translations",
                "ext", "rid"]

# Size caps — a `.lib` is somebody else's file. Names/values match the numbers
# the import route enforced as lib/1 so behaviour is unchanged.
MAX_BYTES = 250 * 1024 * 1024            # whole archive
MAX_FIGURE = 15 * 1024 * 1024            # one image member, decompressed
MAX_PAGES = 2000
MAX_JSON = 10 * 1024 * 1024              # one JSON member, decompressed
MAX_INFLATED = 300 * 1024 * 1024         # total page-JSON budget
MAX_EXT = 64 * 1024                       # one `ext` object, serialized
MAX_ITEMS = 800                           # regions per page

ROLE_RE = re.compile(r"^[a-z][a-z-]{0,23}$")
HEX_RE = re.compile(r"^#[0-9a-fA-F]{3,8}$")
# a region's stable id: permissive enough to keep whatever a third-party tool
# assigned, tight enough that it can never carry a path or markup
RID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_TPL_RE = re.compile(r"^[\w\- ]{1,24}$")
# the negative lookahead rejects dot-only names ("."/".."): matched as a bare
# member name they resolve to a directory and a write on them raises mid-import
_FIG_RE = re.compile(r"^(?!\.+$)[\w.\-]{1,120}$")

# The role vocabulary AS DATA. The `furniture` flag is the load-bearing
# semantic — layout_roles.compose_text lifts furniture out of the body flow —
# so it is DERIVED from layout_roles rather than duplicated; the notes are the
# only prose authored here. Every role the pipeline can produce (MISTRAL_ROLES
# values + the geometric roles classify() assigns) appears exactly once.
_ROLE_NOTES = {
    "body": "main text flow",
    "title": "a chapter or section heading, set in the body column",
    "caption": "the caption belonging to a figure or table",
    "table": "tabular matter",
    "figure": ("an illustration; its text IS the ![id](id) placeholder that "
               "keeps the figure's place in the reading order"),
    "footnote": "a note set small at the foot of the text block",
    "drop-capital": ("the large opening initial of a paragraph; it joins the "
                     "next region's text, never stands alone"),
    "header": "the running head in the top margin",
    "footer": "the running foot in the bottom margin",
    "marginalia": "a margin note",
    "page-number": "the folio or page numeral in a margin",
    "catch-word": ("the catchword at the foot that cues the next page's first "
                   "word"),
    "signature-mark": "the compositor's gathering signature at the foot",
}

SECONDARY_ROLES = set(layout_roles.SECONDARY_ROLES)

ROLE_VOCAB = {
    role: {"furniture": role in SECONDARY_ROLES, "note": note}
    for role, note in _ROLE_NOTES.items()
}


class LibError(Exception):
    """A `.lib` could not be read as an archive of this format."""


@dataclass
class Issue:
    """One finding from the lint pass. `level` is "error" (the file is not a
    valid `.lib` this reader accepts) or "warning" (accepted, but something was
    coerced or dropped); `loc` names where; `msg` says what."""
    level: str
    loc: str
    msg: str

    def as_dict(self) -> dict:
        return {"level": self.level, "loc": self.loc, "msg": self.msg}


# --- ids -------------------------------------------------------------------

def new_rid() -> str:
    """A globally credible random region id (128 bits, UUID hex)."""
    return uuid.uuid4().hex


def clean_rid(raw) -> str:
    """The incoming rid if it is safe to preserve verbatim, else "" (mint one).
    Region identity must survive a round trip through a third-party tool, so the
    charset is permissive — but never a path or markup."""
    r = str(raw or "")
    # fullmatch, not match: `$` alone would accept a trailing newline lib/1's
    # re.fullmatch rejected, and schema.json's anchored pattern would then fail
    return r if RID_RE.fullmatch(r) else ""


def ensure_rids(items: list) -> list:
    """Return `items` with every region carrying a rid — preserved where valid,
    minted where absent. Non-destructive to all other fields (unlike a full
    sanitize, which rewrites src_type/order): used at export to guarantee a
    stable id on every region, even ones saved before rids existed."""
    out = []
    used: set[str] = set()
    for it in items or []:
        if not isinstance(it, dict):
            continue
        rec = dict(it)
        rid = clean_rid(rec.get("rid"))
        if not rid or rid in used:
            rid = new_rid()
            while rid in used:
                rid = new_rid()
            rec["rid"] = rid
        used.add(rid)
        out.append(rec)
    return out


# --- sanitizers (shared by the server routes and the Python API) -----------

def sanitize_ext(raw, loc: str = "ext", warn=None) -> dict:
    """The `ext` namespace: the sanctioned home for third-party/AI data, at the
    manifest, page, or region level. Preserved VERBATIM (round-tripped through
    JSON so nothing non-serializable survives) and size-capped. A dropped `ext`
    is named through `warn` — the whole point of `ext` is that it isn't the
    thing that silently vanishes."""
    if raw in (None, {}):
        return {}
    if not isinstance(raw, dict):
        if warn:
            warn(loc, "ext ignored: not an object")
        return {}
    try:
        # allow_nan=False so a NaN/Infinity smuggled in can't ride into a
        # member no strict JSON parser will read back
        blob = json.dumps(raw, ensure_ascii=False, allow_nan=False)
    except (ValueError, TypeError):
        if warn:
            warn(loc, "ext dropped: not JSON-serializable")
        return {}
    if len(blob.encode("utf-8")) > MAX_EXT:
        if warn:
            warn(loc, f"ext dropped: exceeds {MAX_EXT} bytes")
        return {}
    return json.loads(blob)


def sanitize_page_items(raw: list, src_type: str = "human",
                        warn=None, loc: str = "pages") -> list:
    """One page's region items scrubbed for storage: roles kebab-case, boxes
    clamped into the page, text layers capped, everything re-ordered and
    re-idd. A `rid` (stable region identity) is PRESERVED when the item carries
    a valid one and minted otherwise — order stays `order`, identity stays
    `rid`. Shared by the workbench PUT and the .lib import/read paths: anything
    that writes items the sidecar will trust comes through here. When `warn` is
    given every coercion and drop is named (the import receipt / linter); when
    it is None the scrub is silent (the live PUT, whose contract is unchanged)."""
    def order_of(it):
        o = it.get("order")
        return float(o) if isinstance(o, (int, float)) \
            and not isinstance(o, bool) else 0.0

    items = []
    used_rids: set[str] = set()
    for idx, it in enumerate(
            sorted((x for x in raw if isinstance(x, dict)), key=order_of)):
        here = f"{loc}[{idx}]"
        box = it.get("box") or {}
        try:
            x = min(1.0, max(0.0, float(box.get("x") or 0)))
            y = min(1.0, max(0.0, float(box.get("y") or 0)))
            w = min(1.0 - x, max(0.0, float(box.get("w") or 0)))
            h = min(1.0 - y, max(0.0, float(box.get("h") or 0)))
        except (TypeError, ValueError):
            if warn:
                warn(here, "region dropped: box is not numeric")
            continue
        if w < 0.001 or h < 0.001:
            if warn:
                warn(here, "region dropped: box has no area")
            continue
        role_in = str(it.get("role") or "body").lower()
        if ROLE_RE.match(role_in):
            role = role_in
            if warn and role not in ROLE_VOCAB:
                warn(here, f"role {role!r} is not in the vocabulary "
                           "(kept, but external tools may not render it)")
        else:
            role = "body"
            if warn:
                warn(here, f"role {role_in!r} coerced to 'body': "
                           "not a valid role name")
        text = str(it.get("text") or "")
        if warn and len(text) > 20000:
            warn(here, "text truncated to 20000 chars")
        rid = clean_rid(it.get("rid"))
        if not rid or rid in used_rids:
            if rid and warn:
                warn(here, f"duplicate rid {rid!r} replaced with a new id")
            rid = new_rid()
            while rid in used_rids:
                rid = new_rid()
        used_rids.add(rid)
        rec = {"id": f"r{len(items)}", "rid": rid, "role": role,
               "src_type": src_type, "order": len(items),
               "box": {"x": round(x, 5), "y": round(y, 5),
                       "w": round(w, 5), "h": round(h, 5)},
               "text": text[:20000]}
        # the normalized reading layer (long-s resolved, dehyphenated…),
        # stored only when it exists — compose_text falls back per region
        norm = str(it.get("norm") or "")
        if warn and len(norm) > 20000:
            warn(here, "norm truncated to 20000 chars")
        norm = norm[:20000]
        if norm:
            rec["norm"] = norm
        ext = sanitize_ext(it.get("ext"), f"{here}.ext", warn)
        if ext:
            rec["ext"] = ext
        items.append(rec)
    return items


def sanitize_dims(dims):
    if not isinstance(dims, dict):
        return None
    try:
        return {k: int(dims.get(k) or 0) for k in ("w", "h", "dpi")}
    except (TypeError, ValueError, OverflowError):
        return None


def sanitize_styles(raw: dict) -> dict:
    """A role->style mapping scrubbed for storage. Shared by the style-board
    PUT and the .lib import — a .lib is somebody else's file."""
    styles = {}
    for role, st in raw.items():
        role = str(role).lower()
        if not ROLE_RE.match(role) or not isinstance(st, dict):
            continue
        out = {}
        family = str(st.get("family") or "").strip()[:60]
        if family:
            out["family"] = family
        for k, lo, hi in (("size_em", 0.3, 4.0), ("leading", 0.8, 3.0)):
            try:
                v = float(st.get(k))
            except (TypeError, ValueError, OverflowError):
                continue
            if lo <= v <= hi:
                out[k] = round(v, 2)
        if st.get("style") in ("italic", "normal"):
            out["style"] = st["style"]
        if st.get("variant") in ("small-caps", "normal"):
            out["variant"] = st["variant"]
        if st.get("align") in ("left", "right", "center", "justify"):
            out["align"] = st["align"]
        for k in ("color", "bg"):
            v = str(st.get(k) or "")
            if HEX_RE.match(v):
                out[k] = v
        if out:
            styles[role] = out
    return styles


def sanitize_figure(fig, src_key: str, warn=None, loc: str = "figures") -> dict:
    """One figure inventory entry scrubbed for storage under `src_key`. The
    bbox values ride into layout.json, which /ocr-layout serializes — a NaN or
    a nested object here would break every layout fetch. `rework_of` (the
    deliberate-rework pointer, §2.6) survives when it names a plausible member."""
    out = {"src_key": src_key}
    if not isinstance(fig, dict):
        return out
    try:
        pg = int(fig.get("page"))
        if 1 <= pg <= 99999:
            out["page"] = pg
    except (TypeError, ValueError, OverflowError):
        pass
    for k in ("x", "y", "w", "h"):
        try:
            v = float(fig.get(k))
        except (TypeError, ValueError):
            continue
        if v == v and 0.0 <= v <= 1.0:          # finite, in the page
            out[k] = round(v, 5)
    ro = str(fig.get("rework_of") or "")
    if _FIG_RE.fullmatch(ro):
        out["rework_of"] = ro
    elif ro and warn:
        warn(loc, f"rework_of {ro!r} ignored: not a valid member name")
    ext = sanitize_ext(fig.get("ext"), f"{loc}.ext", warn)
    if ext:
        out["ext"] = ext
    return out


# --- version detection -----------------------------------------------------

def parse_format(book) -> tuple[int, int] | None:
    """(major, minor) for a manifest, or None when the version is missing or
    malformed. A lib/1 file (the bare `"format": "lib/1"` marker) reads as
    (1, 0) — it upgrades on import."""
    if not isinstance(book, dict):
        return None
    fv = book.get("format_version")
    if isinstance(fv, str):
        m = re.fullmatch(r"(\d+)\.(\d+)", fv.strip())
        return (int(m.group(1)), int(m.group(2))) if m else None
    legacy = book.get("format")
    if isinstance(legacy, str):
        m = re.fullmatch(r"lib/(\d+)", legacy.strip())
        if m:
            return (int(m.group(1)), 0)
    return None


# --- the document model + read/write ---------------------------------------

@dataclass
class LibPage:
    page: int
    doc: str = "compiled.txt"
    dims: dict = field(default_factory=dict)
    state: str = ""
    items: list = field(default_factory=list)
    ext: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)     # the member as parsed (linting)


@dataclass
class LibDocument:
    """An open `.lib` in memory. `book` is the raw manifest as parsed (so
    `validate` can lint it); the convenience accessors read through it. `pages`
    keep their items as dicts — mutate `page.items[i]["norm"] = "…"` and write
    it back with `write_lib`."""
    format: tuple[int, int] | None = None
    book: dict = field(default_factory=dict)
    pages: list = field(default_factory=list)             # list[LibPage]
    translations: dict = field(default_factory=dict)      # lang -> parsed member
    assets: dict = field(default_factory=dict)            # name -> bytes
    members: list = field(default_factory=list)           # every member name
    skipped: list = field(default_factory=list)           # (member, reason) pairs

    @property
    def format_version(self) -> str:
        return "%d.%d" % self.format if self.format else ""

    @property
    def book_id(self) -> str:
        return str(self.book.get("book_id") or "")

    @property
    def meta(self) -> dict:
        m = self.book.get("meta")
        return m if isinstance(m, dict) else {}


_PAGE_MEMBER = re.compile(r"pages/(\d{1,5})\.json")
_ASSET_MEMBER = re.compile(r"assets/img/((?!\.+$)[\w.\-]{1,120})")
_TRANS_MEMBER = re.compile(r"translations/([a-z]{2,3}(?:-[A-Za-z0-9]{2,8})*)\.json")
_KNOWN_MEMBERS = {"book.json", "INSTRUCTIONS.md", "schema.json"}


def read_lib(path_or_bytes) -> LibDocument:
    """Open a `.lib` (a filesystem path, or the raw bytes) into a LibDocument.
    Owns the zip layout and every size/zip-slip/deflate-bomb defence: member
    names are matched against known-safe patterns and never trusted as paths,
    and each member is checked at its DECLARED decompressed size before it is
    read, so a small archive cannot inflate the reader into an OOM. Content is
    kept close to raw so `validate` can report what a sanitize pass would coerce
    — call `validate(doc)` next, or `write_lib` to re-seal."""
    if isinstance(path_or_bytes, (bytes, bytearray)):
        raw = bytes(path_or_bytes)
    else:
        raw = Path(path_or_bytes).read_bytes()
    if len(raw) > MAX_BYTES:
        raise LibError("archive too large")
    try:
        z = zipfile.ZipFile(io.BytesIO(raw))
        info = z.getinfo("book.json")
    except (zipfile.BadZipFile, KeyError) as exc:
        raise LibError("not a .lib archive") from exc
    if info.file_size > MAX_JSON:
        raise LibError("book.json too large")
    try:
        book = json.loads(z.read("book.json"))
    except ValueError as exc:
        raise LibError("book.json is not valid JSON") from exc
    if not isinstance(book, dict):
        raise LibError("book.json is not an object")

    doc = LibDocument(format=parse_format(book), book=book,
                      members=list(z.namelist()))
    # pages, translations, and assets all draw down one running budget so a
    # small deflate-bomb archive can't inflate GBs of members into memory —
    # every silent drop is recorded on doc.skipped so validate() can name it
    budget = MAX_INFLATED
    for name in z.namelist():
        pm = _PAGE_MEMBER.fullmatch(name)
        if pm:
            n = int(pm.group(1))
            if not 1 <= n <= 99999:
                doc.skipped.append((name, "page number out of range"))
                continue
            if len(doc.pages) >= MAX_PAGES:
                doc.skipped.append((name, "beyond the page cap"))
                continue
            declared = z.getinfo(name).file_size
            if declared > MAX_JSON or declared > budget:
                doc.skipped.append((name, "exceeds the size cap"))
                continue
            budget -= declared
            try:
                rec = json.loads(z.read(name))
            except ValueError:
                doc.skipped.append((name, "not valid JSON"))
                continue
            if not isinstance(rec, dict):
                doc.skipped.append((name, "not an object"))
                continue
            doc.pages.append(LibPage(
                page=n,
                doc=str(rec.get("doc") or "compiled.txt"),
                dims=rec.get("dims") if isinstance(rec.get("dims"), dict) else {},
                state=str(rec.get("state") or ""),
                items=rec.get("items") if isinstance(rec.get("items"), list)
                else [],
                ext=rec.get("ext") if isinstance(rec.get("ext"), dict) else {},
                raw=rec))
            continue
        tm = _TRANS_MEMBER.fullmatch(name)
        if tm:
            declared = z.getinfo(name).file_size
            if declared > MAX_JSON or declared > budget:
                doc.skipped.append((name, "exceeds the size cap"))
                continue
            budget -= declared
            try:
                td = json.loads(z.read(name))
            except ValueError:
                doc.skipped.append((name, "not valid JSON"))
                continue
            if isinstance(td, dict):
                doc.translations[tm.group(1).lower()] = td
            else:
                doc.skipped.append((name, "not an object"))
            continue
        am = _ASSET_MEMBER.fullmatch(name)
        if am:
            declared = z.getinfo(name).file_size
            if declared <= MAX_FIGURE and declared <= budget:
                budget -= declared
                doc.assets[am.group(1)] = z.read(name)
            else:
                doc.skipped.append((name, "exceeds the size cap"))
    doc.pages.sort(key=lambda p: p.page)
    return doc


def _book_manifest(doc: LibDocument, *, book_id: str, generator: str,
                   instructions_book: str) -> dict:
    """Seal a LibDocument's manifest into the lib/2 book.json shape."""
    book = doc.book
    figures = {}
    src_key = str(book.get("source") or "primary")
    raw_figs = book.get("figures") if isinstance(book.get("figures"),
                                                 dict) else {}
    for name, fig in raw_figs.items():
        if _FIG_RE.fullmatch(str(name)):
            figures[str(name)] = sanitize_figure(fig, src_key)
    styles = sanitize_styles(book["stylesheet"]) \
        if isinstance(book.get("stylesheet"), dict) else {}
    templates = book.get("templates") if isinstance(book.get("templates"),
                                                    dict) else {}
    return {
        "format_version": FORMAT_VERSION,
        "generator": generator,
        "book_id": book_id,
        "created_at": str(book.get("created_at") or ""),
        "source": src_key,
        "meta": book.get("meta") if isinstance(book.get("meta"), dict) else {},
        "capabilities": list(CAPABILITIES),
        "roles": ROLE_VOCAB,
        "instructions": {"general_ref": "INSTRUCTIONS.md",
                         "book": instructions_book},
        "stylesheet": styles,
        "templates": templates,
        "figures": figures,
        "pages": sorted(p.page for p in doc.pages),
        "ext": sanitize_ext(book.get("ext"), "ext"),
    }


def write_lib(doc: LibDocument, path, *, generator: str = "library-tool/dev",
              book_id: str = "", instructions_book: str = "") -> None:
    """Seal a LibDocument to `path` as a lib/2 archive: sanitized book.json,
    one pages/N.json per page, INSTRUCTIONS.md + schema.json, translations, and
    the referenced image assets. A `.lib` that round-trips through here cannot
    come out a shape the app rejects. Raises LibError for a document whose MAJOR
    this build cannot write."""
    if doc.format and doc.format[0] > SUPPORTED_MAJOR:
        raise LibError(f"cannot write format {doc.format[0]}.{doc.format[1]}")

    # A rid identifies one logical region in the whole book, not merely within
    # a page.  The page sanitizer can safely repair a duplicate *on the same
    # page*, but silently changing one side of a cross-page collision would
    # make references to that region ambiguous.  Refuse before opening the
    # destination so callers never receive a partially written archive.
    seen_rids: dict[str, tuple[int, int]] = {}
    for page_index, page in enumerate(doc.pages):
        for item in page.items:
            if not isinstance(item, dict):
                continue
            rid = clean_rid(item.get("rid"))
            previous = seen_rids.get(rid) if rid else None
            if previous is not None and previous[0] != page_index:
                raise LibError(
                    f"duplicate rid {rid!r} on pages "
                    f"{previous[1]} and {page.page}")
            if rid and previous is None:
                seen_rids[rid] = (page_index, page.page)

    # Sanitize once for this seal operation.  On success the canonical items
    # (including any newly minted rids) are written back to the in-memory
    # document, so sealing the same object again preserves region identity.
    sealed_pages = [
        (page, sanitize_page_items(page.items, src_type="import")[:MAX_ITEMS])
        for page in sorted(doc.pages, key=lambda page: page.page)
    ]
    bid = book_id or doc.book_id or ("b-" + uuid.uuid4().hex)
    manifest = _book_manifest(doc, book_id=bid, generator=generator,
                              instructions_book=instructions_book)
    destination = Path(path)
    temporary = destination.with_name(
        destination.name + f".tmp-{os.getpid()}-{uuid.uuid4().hex}")
    try:
        # Seal beside the destination and publish with one replace. A late
        # serialization/ZIP failure therefore preserves an existing archive.
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("book.json", json.dumps(
                manifest, indent=1, ensure_ascii=False, allow_nan=False))
            z.writestr("INSTRUCTIONS.md",
                       render_instructions(manifest["meta"],
                                           per_book=instructions_book))
            z.writestr("schema.json", json.dumps(SCHEMA, indent=1))
            for p, items in sealed_pages:
                # Items were capped before opening the destination so a sealed
                # page cannot come out a shape schema/import then truncates.
                body = {"page": p.page, "doc": p.doc, "dims": p.dims or {},
                        "state": "verified" if p.state == "verified" else "",
                        "items": items}
                ext = sanitize_ext(p.ext, f"pages/{p.page}.json.ext")
                if ext:
                    body["ext"] = ext
                z.writestr(f"pages/{p.page}.json", json.dumps(
                    body, indent=1, ensure_ascii=False, allow_nan=False))
            for lang, td in doc.translations.items():
                if RID_RE.fullmatch(lang) and isinstance(td, dict):
                    z.writestr(f"translations/{lang}.json", json.dumps(
                        td, ensure_ascii=False, allow_nan=False))
            for name in manifest["figures"]:
                blob = doc.assets.get(name)
                if isinstance(blob, (bytes, bytearray)):
                    z.writestr(f"assets/img/{name}", bytes(blob))
        os.replace(temporary, destination)
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass

    for page, items in sealed_pages:
        page.items = items


# --- the linter ------------------------------------------------------------

def validate(doc: LibDocument) -> list:
    """Run the sanitize/lint pass over an open document and return the findings
    without mutating anything — the Python twin of POST /api/lib/validate.
    Errors mean the file is not a `.lib` this reader accepts (bad/newer
    version); warnings mean it was accepted but something was coerced or
    dropped. External tools and CI check a `.lib` here before shipping it."""
    issues: list = []

    def add(level, loc, msg):
        issues.append(Issue(level, loc, msg))

    def warn(loc, msg):
        add("warning", loc, msg)

    if doc.format is None:
        add("error", "book.json", "missing or malformed format_version "
                                  "(expected \"MAJOR.MINOR\" or \"lib/1\")")
        return issues
    if doc.format[0] > SUPPORTED_MAJOR:
        add("error", "book.json",
            f"format {doc.format[0]}.{doc.format[1]} needs a newer reader "
            f"(this one knows major {SUPPORTED_MAJOR})")
        return issues
    if doc.format[0] >= 2 and not doc.book_id:
        warn("book.json", "no book_id — a stable id will be minted on import")

    sanitize_ext(doc.book.get("ext"), "book.json/ext", warn)

    # members read_lib dropped are invisible in doc.pages/translations — name
    # each so validate stays in lockstep with what the import receipt reports
    for name, reason in doc.skipped:
        warn(name, f"member skipped on read: {reason}")

    # a stylesheet the import discards wholesale (>40 roles) must not validate
    # clean; sanitize_styles has no per-role warn hook, so lint the count here
    raw_styles = doc.book.get("stylesheet")
    if isinstance(raw_styles, dict) and len(raw_styles) > 40:
        warn("book.json/stylesheet", "stylesheet dropped: more than 40 roles")

    seen_rids: dict = {}
    for p in doc.pages:
        loc = f"pages/{p.page}.json"
        if not p.items:
            warn(loc, "page has no usable regions")
        if len([x for x in p.items if isinstance(x, dict)]) > MAX_ITEMS:
            warn(loc, f"page has more than {MAX_ITEMS} regions; "
                      "the surplus is dropped on import")
        sanitize_page_items(p.items, warn=warn, loc=loc)
        sanitize_ext(p.ext, f"{loc}/ext", warn)
        for it in p.items:
            if not isinstance(it, dict):
                continue
            rid = clean_rid(it.get("rid"))
            if rid and rid in seen_rids:
                add("error", loc,
                    f"duplicate rid {rid!r} (also on {seen_rids[rid]})")
            elif rid:
                seen_rids[rid] = loc

    # figures: an entry with no asset is a broken reference; an asset with no
    # entry is a member that will be skipped on import
    raw_figs = doc.book.get("figures") if isinstance(
        doc.book.get("figures"), dict) else {}
    fig_names = {str(n) for n in raw_figs}
    asset_names = set(doc.assets)
    for name in sorted(fig_names):
        if not _FIG_RE.fullmatch(name):
            warn("figures", f"figure {name!r} skipped: not a valid member name")
        elif name not in asset_names:
            warn("figures", f"figure {name!r} has no assets/img/ member")
        sanitize_figure(raw_figs.get(name), doc.book.get("source") or "primary",
                        warn=warn, loc=f"figures/{name}")
    for name in sorted(asset_names - fig_names):
        warn("assets/img", f"{name!r} has no figure entry (skipped on import)")

    # any member outside the known shapes round-trips only through `ext`
    for name in doc.members:
        if (name in _KNOWN_MEMBERS or _PAGE_MEMBER.fullmatch(name)
                or _ASSET_MEMBER.fullmatch(name) or _TRANS_MEMBER.fullmatch(name)
                or name.endswith("/")):
            continue
        warn(name, "member ignored: not part of the .lib layout")
    return issues


# --- self-description: INSTRUCTIONS.md + schema.json ------------------------

def _role_table() -> str:
    rows = ["| role | furniture | meaning |", "| --- | --- | --- |"]
    for role, spec in ROLE_VOCAB.items():
        rows.append(f"| `{role}` | {'yes' if spec['furniture'] else 'no'} | "
                    f"{spec['note']} |")
    return "\n".join(rows)


def render_instructions(meta: dict, per_book: str = "") -> str:
    """Generate INSTRUCTIONS.md — the LLM contract a `.lib` ships. Covers what
    the file is, the data model (with the role table rendered from the live
    vocabulary), the editing invariants (docs/lib-format.md §2.1), the per-book
    note, and a worked translate/colorize example."""
    title = str(meta.get("title") or "this book")
    return f"""# {title} — a Library Tool `.lib` book file

## What this file is

This is a **book from a Library Tool archive**, packaged as a ZIP archive with
a `.lib` extension. You can unzip it, edit its members, and re-zip it, and the
Library Tool app will import your changes — *provided you follow the rules
below*. The members:

- `book.json` — the manifest: format version, bibliographic metadata, the role
  stylesheet, layout templates, the figure inventory, and the page list.
- `pages/<N>.json` — one file per page, where `<N>` is the page number. Holds
  the page's regions.
- `assets/img/<name>` — the figure crops that figure regions reference.
- `translations/<bcp47>.json` — page-aligned translated text (optional).
- `INSTRUCTIONS.md` (this file) and `schema.json` — self-description.

## The data model

A page is a list of **regions**. A region is a typed box of text:

```json
{{ "rid": "k3f9a2", "role": "body", "order": 0,
  "box": {{ "x": 0.2, "y": 0.1, "w": 0.6, "h": 0.7 }},
  "text": "diplomatic transcription…", "norm": "modern reading…" }}
```

- **`box`** is the region's rectangle as **0..1 fractions of the page** (x, y
  = top-left corner; w, h = size). Not pixels.
- **`text`** is the *diplomatic* transcription — faithful to the scan (long-s,
  original spelling, line breaks). **`norm`** is the optional *modern-edition*
  reading. They are two layers of the same region; keep both.
- **`rid`** is the region's stable identity. **`order`** is its reading order.
- A **figure** region's `text` is a `![id](id)` placeholder that holds the
  figure's place in the flow — leave it as the placeholder.

### Roles

Every region has a **role** from this fixed vocabulary. *Furniture* roles
(running heads, margin notes, catchwords…) are excluded from the compiled body
text; the rest are content.

{_role_table()}

## Editing rules (the invariants)

1. **Never renumber or rename `pages/<N>.json`.** The page number is the key.
2. **Never invent roles.** Use only the vocabulary above. Custom or tool-
   specific data goes in an **`ext`** object (allowed at the manifest, page,
   and region level, round-tripped verbatim) — never in a new role or a new
   top-level key.
3. **Translations and modernized text go in `norm`** (or a
   `translations/<lang>.json` member) — **never overwrite `text`**.
4. **Reworked or colorized images:** write a **new** file under `assets/img/`
   and add a figure entry with **`rework_of: "<original>"`**. Never replace the
   original file.
5. **Do not touch** `format_version`, `book_id`, region `rid`s, or the
   provenance fields (`src_type`, `rework_of`).

## Per-book instructions

{per_book.strip() or "_(none provided)_"}

## Worked example — "translate into Japanese and colorize the illustrations"

1. Read this file, `schema.json`, and `book.json`'s `instructions.book`.
2. For the translation, write `translations/ja.json`:
   `{{ "lang": "ja", "pages": {{ "7": {{ "_page": "…翻訳…" }} }} }}` — keyed by
   page. Leave every region's `text` and `norm` untouched.
3. For each figure, render a colorized `assets/img/<name>-color.png`, and add a
   figure entry to `book.json` with `rework_of: "<name>"`.
4. Re-zip and import. The receipt should report the pages, translations, and
   figures added — with **zero warnings**. Nothing broke; provenance records
   exactly what you did.
"""


# schema.json — a JSON Schema (draft 2020-12) covering book.json and
# pages/<N>.json, so a tool can validate mechanically. $defs hold each member's
# shape; x-lib-members maps a member glob to the def that governs it.
SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://library-tool/lib/schema.json",
    "title": "Library Tool .lib archive",
    "description": "Shapes for the members of a .lib book archive (lib/2).",
    "x-lib-members": {
        "book.json": "#/$defs/book",
        "pages/<N>.json": "#/$defs/page",
        "translations/<bcp47>.json": "#/$defs/translation",
    },
    "$defs": {
        "box": {
            "type": "object",
            "required": ["x", "y", "w", "h"],
            "properties": {k: {"type": "number", "minimum": 0, "maximum": 1}
                           for k in ("x", "y", "w", "h")},
        },
        "region": {
            "type": "object",
            "required": ["role", "order", "box", "text"],
            "properties": {
                "id": {"type": "string"},
                "rid": {"type": "string", "pattern": RID_RE.pattern},
                "role": {"type": "string", "enum": sorted(ROLE_VOCAB)},
                "src_type": {"type": "string"},
                "order": {"type": "number"},
                "box": {"$ref": "#/$defs/box"},
                "text": {"type": "string", "maxLength": 20000},
                "norm": {"type": "string", "maxLength": 20000},
                "ext": {"type": "object"},
            },
        },
        "page": {
            "type": "object",
            "required": ["page", "items"],
            "properties": {
                "page": {"type": "integer", "minimum": 1},
                "doc": {"type": "string"},
                "dims": {"type": "object"},
                "state": {"type": "string", "enum": ["", "verified"]},
                "items": {"type": "array",
                          "items": {"$ref": "#/$defs/region"},
                          "maxItems": MAX_ITEMS},
                "ext": {"type": "object"},
            },
        },
        "figure": {
            "type": "object",
            "properties": {
                "page": {"type": "integer"},
                "x": {"type": "number"}, "y": {"type": "number"},
                "w": {"type": "number"}, "h": {"type": "number"},
                "rework_of": {"type": "string", "pattern": _FIG_RE.pattern},
                "ext": {"type": "object"},
            },
        },
        "book": {
            "type": "object",
            "required": ["format_version", "pages"],
            "properties": {
                "format_version": {"type": "string",
                                   "pattern": r"^\d+\.\d+$"},
                "generator": {"type": "string"},
                "book_id": {"type": "string"},
                "created_at": {"type": "string"},
                "source": {"type": "string"},
                "meta": {"type": "object"},
                "capabilities": {"type": "array",
                                 "items": {"type": "string"}},
                "roles": {"type": "object"},
                "instructions": {
                    "type": "object",
                    "properties": {"general_ref": {"type": "string"},
                                   "book": {"type": "string"}},
                },
                "stylesheet": {"type": "object"},
                "templates": {"type": "object"},
                "figures": {"type": "object",
                            "additionalProperties": {"$ref": "#/$defs/figure"}},
                "pages": {"type": "array", "items": {"type": "integer"}},
                "ext": {"type": "object"},
            },
        },
        "translation": {
            "type": "object",
            "required": ["lang", "pages"],
            "properties": {
                "lang": {"type": "string"},
                "pages": {
                    "type": "object",
                    "additionalProperties": {
                        "type": "object",
                        "additionalProperties": {"type": "string"},
                    },
                },
            },
        },
    },
}

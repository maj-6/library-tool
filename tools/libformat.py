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
  - the self-description `lib/2` and capture-aware `lib/3` files ship:
    `INSTRUCTIONS.md` generated from the live vocabulary and `schema.json`, so
    the artifact teaches its reader.

Depends only on the standard library + `layout_roles` (the role vocabulary),
so it is safe for external scripts and pip-installable later via the existing
pyproject.
"""
from __future__ import annotations

import io
import hashlib
import json
import math
import os
import re
import stat
import uuid
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

import layout_roles

# --- the format's constants ------------------------------------------------

# format_version is "MAJOR.MINOR": MINOR is additive (new optional keys only),
# a higher MAJOR breaks and an importer must reject it. lib/1 files upgrade to
# 1.0 on read. See docs/lib-format.md §2.3.
# Replica's existing page exporter deliberately remains lib/2 until it can
# project the canonical capture aggregates.  ``write_lib`` selects lib/3 when
# the document itself is lib/3; keeping this alias at 2.0 preserves every
# existing server/export call site's semantics.
FORMAT_VERSION = "2.0"
CAPTURE_FORMAT_VERSION = "3.0"
SUPPORTED_MAJOR = 3

# What this writer's files declare they contain — a reader can feature-detect
# without sniffing the members.
CAPABILITIES = ["norm-layer", "templates", "figures", "translations",
                "ext", "rid"]
CAPTURE_CAPABILITIES = [
    *CAPABILITIES,
    "representations",
    "artifacts",
    "artifact-assertions",
    "spatial-selectors",
    "revisioned-lineage",
]

# Size caps — a `.lib` is somebody else's file. Names/values match the numbers
# the import route enforced as lib/1 so behaviour is unchanged.
MAX_BYTES = 250 * 1024 * 1024            # whole archive
MAX_FIGURE = 15 * 1024 * 1024            # one image member, decompressed
MAX_PAGES = 2000
MAX_JSON = 10 * 1024 * 1024              # one JSON member, decompressed
MAX_INFLATED = 300 * 1024 * 1024         # total page-JSON budget
MAX_EXT = 64 * 1024                       # one `ext` object, serialized
MAX_ITEMS = 800                           # regions per page
MAX_MEMBERS = 10_000
MAX_REPRESENTATIONS = 5_000
MAX_ARTIFACTS = 10_000
MAX_RESOURCE = 100 * 1024 * 1024          # one lib/3 declared resource

ROLE_RE = re.compile(r"^[a-z][a-z-]{0,23}$")
HEX_RE = re.compile(r"^#[0-9a-fA-F]{3,8}$")
# a region's stable id: permissive enough to keep whatever a third-party tool
# assigned, tight enough that it can never carry a path or markup
RID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_TPL_RE = re.compile(r"^[\w\- ]{1,24}$")
# the negative lookahead rejects dot-only names ("."/".."): matched as a bare
# member name they resolve to a directory and a write on them raises mid-import
_FIG_RE = re.compile(r"^(?!\.+$)[\w.\-]{1,120}$")
PORTABLE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
REVISION_RE = re.compile(r"^[^\s\"\\]{1,512}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MEDIA_TYPE_RE = re.compile(
    r"^[a-z0-9][a-z0-9!#$&^_.+-]{0,126}/"
    r"[a-z0-9][a-z0-9!#$&^_.+-]{0,126}$",
    re.IGNORECASE,
)
LANGUAGE_RE = re.compile(r"^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*$")
PORTABLE_MEMBER_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
BOOK_ID_RE = re.compile(r"^b-[0-9a-f]{32}$")

IMAGE_CATEGORIES = frozenset(
    {"title_page", "cover", "spine", "content_specimen", "other"}
)
ASSIGNMENT_ORIGINS = frozenset({"manual", "inherited", "suggested"})
CAPTION_ORIGINS = frozenset({"manual", "machine", "inherited", "imported"})
ROLE_ASSIGNMENT_ORIGINS = frozenset({"manual", "machine", "imported"})
REVIEW_EXPORT_MODES = frozenset({"all-durable", "active-only", "none"})
PRIMARY_ARTIFACT_KINDS = (
    "generated-metadata",
    "ocr-text",
    "spatial-annotation",
    "raster-image",
)
WORKFLOW_ARTIFACT_KINDS = (
    "transform-recipe",
    "correction-review",
)
_REPRESENTATION_REQUIRED_FIELDS = frozenset({
    "id",
    "revision",
    "role",
    "media_type",
    "member",
    "content_sha256",
    "lineage",
    "ext",
})
_ARTIFACT_REQUIRED_FIELDS = frozenset({
    "id",
    "revision",
    "kind",
    "media_type",
    "member",
    "content_sha256",
    "source",
    "provenance",
    "category_assignments",
    "caption_assertions",
    "role_assignments",
    "relationships",
    "ext",
})

_PRIVATE_LOCATOR_KEYS = frozenset({
    "absolute_path",
    "asset_ref",
    "file",
    "file_name",
    "filename",
    "filepath",
    "href",
    "local_path",
    "locator",
    "path",
    "resource_grant",
    "resource_id",
    "resource_ref",
    "source_token",
    "storage_key",
    "storage_locator",
    "storage_path",
    "uri",
    "url",
})
_PRIVATE_LOCATOR_SUFFIXES = frozenset(
    {"file", "filename", "filepath", "locator", "path", "uri", "url"}
)
_ACRONYM_BOUNDARY_RE = re.compile(r"([A-Z]+)([A-Z][a-z])")
_CAMEL_BOUNDARY_RE = re.compile(r"([a-z0-9])([A-Z])")
_KEY_SEPARATOR_RE = re.compile(r"[^A-Za-z0-9]+")

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
    """A `.lib` could not be read as an archive of this format.

    ``code`` and ``details`` make hostile-input failures honest to callers
    without coupling this standard-library module to Flask or engine errors.
    """

    def __init__(self, message: str, *, code: str = "invalid_lib",
                 details: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})


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


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    value: dict = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON object key {key!r}")
        value[key] = item
    return value


def _strict_json(payload: bytes):
    """Decode strict UTF-8 JSON (no duplicate keys or non-finite numbers)."""
    try:
        return json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON number {value}")
            ),
        )
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ValueError("invalid strict JSON") from exc


def _json_clone(value, *, loc: str = "value"):
    """Return detached strict JSON or raise a format-level error."""
    try:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return _strict_json(payload)
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise LibError(
            f"{loc} is not strict JSON",
            code="invalid_lib3_graph",
            details={"location": loc},
        ) from exc


def _normalized_key(key: str) -> str:
    separated = _ACRONYM_BOUNDARY_RE.sub(r"\1_\2", key)
    separated = _CAMEL_BOUNDARY_RE.sub(r"\1_\2", separated)
    return _KEY_SEPARATOR_RE.sub("_", separated).strip("_").casefold()


def _is_private_locator_key(key: str) -> bool:
    normalized = _normalized_key(key)
    return (
        normalized in _PRIVATE_LOCATOR_KEYS
        or normalized.rsplit("_", 1)[-1] in _PRIVATE_LOCATOR_SUFFIXES
    )


def _portable_ext_problem(value, *, loc: str, depth: int = 0,
                          budget: list[int] | None = None) -> tuple[str, str] | None:
    """Return the first unsafe lib/3 extension value, if any.

    Unlike lib/2's permissive, size-capped ``ext``, lib/3 graph extensions may
    never smuggle private resource locators.  Archive members are the only
    portable resource address in the sealed format.
    """
    if budget is None:
        budget = [0]
    budget[0] += 1
    if budget[0] > 512:
        return loc, "extension data has too many values"
    if depth > 12:
        return loc, "extension data is nested too deeply"
    if value is None or isinstance(value, (str, bool)):
        if isinstance(value, str) and len(value) > 8192:
            return loc, "extension string is too long"
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        if abs(value) > (1 << 53) - 1:
            return loc, "extension integer is not portable"
        return None
    if isinstance(value, float):
        if not math.isfinite(value):
            return loc, "extension number is not finite"
        return None
    if isinstance(value, list):
        for index, item in enumerate(value):
            problem = _portable_ext_problem(
                item, loc=f"{loc}[{index}]", depth=depth + 1, budget=budget
            )
            if problem:
                return problem
        return None
    if isinstance(value, dict):
        for key, item in value.items():
            if (
                not isinstance(key, str)
                or not key
                or key != key.strip()
                or len(key) > 128
            ):
                return loc, "extension keys must be bounded, trimmed strings"
            if _is_private_locator_key(key):
                return f"{loc}.{key}", "private resource locators are forbidden"
            problem = _portable_ext_problem(
                item, loc=f"{loc}.{key}", depth=depth + 1, budget=budget
            )
            if problem:
                return problem
        return None
    return loc, "extension data is not JSON"


def _portable_ext(value, *, loc: str) -> dict:
    if value == {}:
        return {}
    if not isinstance(value, dict):
        raise LibError(
            f"{loc} must be an object",
            code="invalid_lib3_extension",
            details={"location": loc},
        )
    detached = _json_clone(value, loc=loc)
    encoded = json.dumps(
        detached, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode("utf-8")
    problem = _portable_ext_problem(detached, loc=loc)
    if len(encoded) > MAX_EXT:
        problem = (loc, f"extension data exceeds {MAX_EXT} bytes")
    if problem:
        location, message = problem
        raise LibError(
            f"{location}: {message}",
            code="unsafe_lib3_extension",
            details={"location": location, "reason": message},
        )
    return detached


def _safe_archive_name(name: object, *, allow_directory: bool = False) -> bool:
    if not isinstance(name, str) or not name or len(name) > 1024:
        return False
    if "\\" in name or "\x00" in name or name.startswith("/"):
        return False
    if any(ord(character) < 32 or ord(character) == 127 for character in name):
        return False
    if re.match(r"^[A-Za-z]:", name):
        return False
    candidate = name[:-1] if allow_directory and name.endswith("/") else name
    if not candidate:
        return False
    parts = candidate.split("/")
    return all(part not in ("", ".", "..") for part in parts)


def _safe_declared_member(name: object, root: str) -> bool:
    if not _safe_archive_name(name):
        return False
    parts = str(name).split("/")
    return (
        len(parts) >= 2
        and parts[0] == root
        and all(PORTABLE_MEMBER_SEGMENT_RE.fullmatch(part) for part in parts[1:])
    )


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    mode = (int(info.external_attr) >> 16) & 0xFFFF
    return bool(mode) and stat.S_ISLNK(mode)


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
    except (ValueError, TypeError, RecursionError):
        if warn:
            warn(loc, "ext dropped: not JSON-serializable")
        return {}
    if len(blob.encode("utf-8")) > MAX_EXT:
        if warn:
            warn(loc, f"ext dropped: exceeds {MAX_EXT} bytes")
        return {}
    try:
        return json.loads(blob)
    except (ValueError, RecursionError):
        if warn:
            warn(loc, "ext dropped: not JSON-serializable")
        return {}


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
class LibRepresentation:
    """One first-class lib/3 media representation.

    The public fields intentionally mirror the archive schema rather than a
    filesystem adapter.  ``member`` is the only resource address a sealed
    archive may expose.
    """

    representation_id: str
    revision: str
    role: str
    media_type: str
    member: str
    content_sha256: str
    dimensions: dict = field(default_factory=dict)
    lineage: list = field(default_factory=list)
    ext: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, value: Mapping) -> "LibRepresentation":
        return cls(
            # Preserve the supplied JSON types until the graph validator has
            # inspected them.  Coercing ``123`` to ``"123"`` (or replacing a
            # malformed collection with an empty one) would turn hostile or
            # broken input into a different, apparently valid record.
            representation_id=value.get("id", ""),
            revision=value.get("revision", ""),
            role=value.get("role", ""),
            media_type=value.get("media_type", ""),
            member=value.get("member", ""),
            content_sha256=value.get("content_sha256", ""),
            dimensions=value.get("dimensions", {}),
            lineage=value.get("lineage"),
            ext=value.get("ext"),
            raw=dict(value),
        )

    def as_dict(self) -> dict:
        value = {
            "id": self.representation_id,
            "revision": self.revision,
            "role": self.role,
            "media_type": self.media_type,
            "member": self.member,
            "content_sha256": self.content_sha256,
            "lineage": _json_clone(self.lineage, loc="representation.lineage"),
            "ext": _portable_ext(self.ext, loc="representation.ext"),
        }
        if "dimensions" in self.raw or self.dimensions != {}:
            value["dimensions"] = _json_clone(
                self.dimensions, loc="representation.dimensions"
            )
        return value


@dataclass
class LibArtifact:
    """One first-class lib/3 generated, extracted, or reviewed artifact."""

    artifact_id: str
    revision: str
    kind: str
    media_type: str
    member: str
    content_sha256: str
    source: dict
    dimensions: dict = field(default_factory=dict)
    provenance: dict = field(default_factory=dict)
    category_assignments: list = field(default_factory=list)
    caption_assertions: list = field(default_factory=list)
    role_assignments: list = field(default_factory=list)
    selector: dict = field(default_factory=dict)
    relationships: list = field(default_factory=list)
    ext: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, value: Mapping) -> "LibArtifact":
        return cls(
            artifact_id=value.get("id", ""),
            revision=value.get("revision", ""),
            kind=value.get("kind", ""),
            media_type=value.get("media_type", ""),
            member=value.get("member", ""),
            content_sha256=value.get("content_sha256", ""),
            source=value.get("source"),
            dimensions=value.get("dimensions", {}),
            provenance=value.get("provenance"),
            category_assignments=value.get("category_assignments"),
            caption_assertions=value.get("caption_assertions"),
            role_assignments=value.get("role_assignments"),
            selector=value.get("selector", {}),
            relationships=value.get("relationships"),
            ext=value.get("ext"),
            raw=dict(value),
        )

    def as_dict(self) -> dict:
        value = {
            "id": self.artifact_id,
            "revision": self.revision,
            "kind": self.kind,
            "media_type": self.media_type,
            "member": self.member,
            "content_sha256": self.content_sha256,
            "source": _json_clone(self.source, loc="artifact.source"),
            "provenance": _json_clone(
                self.provenance, loc="artifact.provenance"
            ),
            "category_assignments": _json_clone(
                self.category_assignments,
                loc="artifact.category_assignments",
            ),
            "caption_assertions": _json_clone(
                self.caption_assertions,
                loc="artifact.caption_assertions",
            ),
            "role_assignments": _json_clone(
                self.role_assignments,
                loc="artifact.role_assignments",
            ),
            "relationships": _json_clone(
                self.relationships, loc="artifact.relationships"
            ),
            "ext": _portable_ext(self.ext, loc="artifact.ext"),
        }
        if "dimensions" in self.raw or self.dimensions != {}:
            value["dimensions"] = _json_clone(
                self.dimensions, loc="artifact.dimensions"
            )
        if "selector" in self.raw or self.selector != {}:
            value["selector"] = _json_clone(
                self.selector, loc="artifact.selector"
            )
        return value


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
    representations: list = field(default_factory=list)   # LibRepresentation
    artifacts: list = field(default_factory=list)         # LibArtifact
    resources: dict = field(default_factory=dict)          # member -> bytes
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


def _representations_from_book(book: Mapping) -> list[LibRepresentation]:
    values = book.get("representations")
    if not isinstance(values, list):
        return []
    return [
        LibRepresentation.from_dict(value)
        for value in values
        if isinstance(value, Mapping)
    ]


def _artifacts_from_book(book: Mapping) -> list[LibArtifact]:
    values = book.get("artifacts")
    if not isinstance(values, list):
        return []
    return [
        LibArtifact.from_dict(value)
        for value in values
        if isinstance(value, Mapping)
    ]


def _representation_record(value) -> dict:
    allowed = {
        "id",
        "revision",
        "role",
        "media_type",
        "member",
        "content_sha256",
        "dimensions",
        "lineage",
        "ext",
    }
    if isinstance(value, LibRepresentation):
        source = value.raw
        if source and any(not isinstance(key, str) for key in source):
            raise LibError(
                "representation field names must be strings",
                code="invalid_lib3_graph",
                details={"location": "book.json/representations"},
            )
        if source and set(source) - allowed:
            raise LibError(
                "representation has unknown fields; move extension data to ext",
                code="invalid_lib3_graph",
                details={
                    "location": "book.json/representations",
                    "fields": sorted(set(source) - allowed),
                },
            )
        record = value.as_dict()
        problem = _private_locator_problem(
            record, loc="book.json/representations"
        )
        if problem:
            location, reason = problem
            raise LibError(
                f"{location}: {reason}",
                code="unsafe_lib3_graph",
                details={"location": location, "reason": reason},
            )
        return record
    if isinstance(value, Mapping):
        record = dict(value)
        if any(not isinstance(key, str) for key in record):
            raise LibError(
                "representation field names must be strings",
                code="invalid_lib3_graph",
                details={"location": "book.json/representations"},
            )
        if set(record) - allowed:
            raise LibError(
                "representation has unknown fields; move extension data to ext",
                code="invalid_lib3_graph",
                details={
                    "location": "book.json/representations",
                    "fields": sorted(set(record) - allowed),
                },
            )
        return _json_clone(record, loc="book.json/representations")
    raise LibError(
        "representations must contain LibRepresentation values or objects",
        code="invalid_lib3_graph",
        details={"location": "book.json/representations"},
    )


def _artifact_record(value) -> dict:
    allowed = {
        "id",
        "revision",
        "kind",
        "media_type",
        "member",
        "content_sha256",
        "source",
        "dimensions",
        "provenance",
        "category_assignments",
        "caption_assertions",
        "role_assignments",
        "selector",
        "relationships",
        "ext",
    }
    if isinstance(value, LibArtifact):
        source = value.raw
        if source and any(not isinstance(key, str) for key in source):
            raise LibError(
                "artifact field names must be strings",
                code="invalid_lib3_graph",
                details={"location": "book.json/artifacts"},
            )
        if source and set(source) - allowed:
            raise LibError(
                "artifact has unknown fields; move extension data to ext",
                code="invalid_lib3_graph",
                details={
                    "location": "book.json/artifacts",
                    "fields": sorted(set(source) - allowed),
                },
            )
        record = value.as_dict()
        problem = _private_locator_problem(
            record, loc="book.json/artifacts"
        )
        if problem:
            location, reason = problem
            raise LibError(
                f"{location}: {reason}",
                code="unsafe_lib3_graph",
                details={"location": location, "reason": reason},
            )
        return record
    if isinstance(value, Mapping):
        record = dict(value)
        if any(not isinstance(key, str) for key in record):
            raise LibError(
                "artifact field names must be strings",
                code="invalid_lib3_graph",
                details={"location": "book.json/artifacts"},
            )
        if set(record) - allowed:
            raise LibError(
                "artifact has unknown fields; move extension data to ext",
                code="invalid_lib3_graph",
                details={
                    "location": "book.json/artifacts",
                    "fields": sorted(set(record) - allowed),
                },
            )
        return _json_clone(record, loc="book.json/artifacts")
    raise LibError(
        "artifacts must contain LibArtifact values or objects",
        code="invalid_lib3_graph",
        details={"location": "book.json/artifacts"},
    )


def _private_locator_problem(
    value,
    *,
    loc: str,
    depth: int = 0,
) -> tuple[str, str] | None:
    if depth > 32:
        return loc, "graph data is nested too deeply"
    if isinstance(value, dict):
        for key, item in value.items():
            here = f"{loc}/{key}"
            if (
                isinstance(key, str)
                and key != "member"
                and _is_private_locator_key(key)
            ):
                return here, "private resource locators are forbidden"
            problem = _private_locator_problem(
                item,
                loc=here,
                depth=depth + 1,
            )
            if problem:
                return problem
    elif isinstance(value, list):
        for index, item in enumerate(value):
            problem = _private_locator_problem(
                item,
                loc=f"{loc}[{index}]",
                depth=depth + 1,
            )
            if problem:
                return problem
    return None


def _graph_security_problem(book: Mapping) -> tuple[str, str] | None:
    """Return the first fail-closed lib/3 graph security problem."""
    book_ext = book.get("ext", {})
    if not isinstance(book_ext, dict):
        return "book.json/ext", "must be an object"
    problem = _portable_ext_problem(book_ext, loc="book.json/ext")
    if problem:
        return problem
    for collection in ("representations", "artifacts"):
        values = book.get(collection)
        if values is None:
            continue
        if not isinstance(values, list):
            return f"book.json/{collection}", "must be an array"
        for index, record in enumerate(values):
            loc = f"book.json/{collection}[{index}]"
            if not isinstance(record, dict):
                return loc, "must be an object"
            problem = _private_locator_problem(record, loc=loc)
            if problem:
                return problem
            member = record.get("member")
            valid_member = (
                _safe_declared_member(member, "representations")
                if collection == "representations"
                else (
                    _safe_declared_member(member, "artifacts")
                    or _safe_declared_member(member, "representations")
                )
            )
            if not valid_member:
                roots = (
                    "representations/"
                    if collection == "representations"
                    else "artifacts/ or a shared representations/"
                )
                return (
                    f"{loc}/member",
                    f"must be a portable member below {roots}",
                )
            for extension_loc, extension in (
                (f"{loc}/ext", record.get("ext", {})),
                (
                    f"{loc}/provenance/ext",
                    (
                        record.get("provenance", {}).get("ext", {})
                        if isinstance(record.get("provenance"), dict)
                        else {}
                    ),
                ),
            ):
                problem = _portable_ext_problem(extension, loc=extension_loc)
                if problem:
                    return problem
            for assertions_key in (
                "category_assignments",
                "caption_assertions",
                "role_assignments",
            ):
                assertions = record.get(assertions_key, [])
                if not isinstance(assertions, list):
                    continue
                for assertion_index, assertion in enumerate(assertions):
                    if not isinstance(assertion, dict):
                        continue
                    assertion_loc = (
                        f"{loc}/{assertions_key}[{assertion_index}]"
                    )
                    for key in assertion:
                        if isinstance(key, str) and _is_private_locator_key(key):
                            return (
                                f"{assertion_loc}/{key}",
                                "private resource locators are forbidden",
                            )
                    problem = _portable_ext_problem(
                        assertion.get("ext", {}),
                        loc=f"{assertion_loc}/ext",
                    )
                    if problem:
                        return problem
                    provenance = assertion.get("provenance")
                    if isinstance(provenance, dict):
                        problem = _portable_ext_problem(
                            provenance.get("ext", {}),
                            loc=f"{assertion_loc}/provenance/ext",
                        )
                        if problem:
                            return problem
    return None


def read_lib(path_or_bytes) -> LibDocument:
    """Open a `.lib` path or byte string with bounded, fail-closed ZIP IO."""
    if isinstance(path_or_bytes, (bytes, bytearray)):
        raw = bytes(path_or_bytes)
    else:
        raw = Path(path_or_bytes).read_bytes()
    if len(raw) > MAX_BYTES:
        raise LibError(
            "archive too large",
            code="lib_archive_too_large",
            details={"maximum_bytes": MAX_BYTES},
        )
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as zipped:
            infos = zipped.infolist()
            if len(infos) > MAX_MEMBERS:
                raise LibError(
                    "archive contains too many members",
                    code="lib_member_limit_exceeded",
                    details={"maximum_members": MAX_MEMBERS},
                )
            by_name: dict[str, zipfile.ZipInfo] = {}
            for info in infos:
                name = info.filename
                if name in by_name:
                    raise LibError(
                        f"duplicate archive member {name!r}",
                        code="duplicate_lib_member",
                        details={"member": name},
                    )
                if not _safe_archive_name(name, allow_directory=info.is_dir()):
                    raise LibError(
                        f"unsafe archive member {name!r}",
                        code="unsafe_lib_member",
                        details={"member": name},
                    )
                if _is_symlink(info):
                    raise LibError(
                        f"symbolic-link archive member {name!r} is forbidden",
                        code="unsafe_lib_member",
                        details={"member": name},
                    )
                if info.flag_bits & 0x1:
                    raise LibError(
                        "encrypted .lib members are not supported",
                        code="encrypted_lib_member",
                        details={"member": name},
                    )
                by_name[name] = info

            info = by_name.get("book.json")
            if info is None:
                raise LibError("not a .lib archive", code="invalid_lib_archive")
            if info.file_size > MAX_JSON:
                raise LibError(
                    "book.json too large",
                    code="lib_book_too_large",
                    details={"maximum_bytes": MAX_JSON},
                )
            try:
                book = _strict_json(zipped.read(info))
            except ValueError as exc:
                raise LibError(
                    "book.json is not valid strict JSON",
                    code="invalid_lib_manifest",
                ) from exc
            if not isinstance(book, dict):
                raise LibError(
                    "book.json is not an object",
                    code="invalid_lib_manifest",
                )

            fmt = parse_format(book)
            major = fmt[0] if fmt else 0
            doc = LibDocument(
                format=fmt,
                book=book,
                members=[value.filename for value in infos],
                representations=_representations_from_book(book),
                artifacts=_artifacts_from_book(book),
            )

            declared_resources: set[str] = set()
            resource_groups: dict[
                str,
                dict[str, list[tuple[str, dict]]],
            ] = {}
            if major == 3:
                problem = _graph_security_problem(book)
                if problem:
                    location, reason = problem
                    raise LibError(
                        f"{location}: {reason}",
                        code="unsafe_lib3_graph",
                        details={"location": location, "reason": reason},
                    )
                aliases: dict[str, str] = {}
                for name in by_name:
                    alias = name.casefold()
                    previous = aliases.get(alias)
                    if previous is not None and previous != name:
                        raise LibError(
                            "archive member names alias on case-insensitive systems",
                            code="lib_member_alias",
                            details={"members": [previous, name]},
                        )
                    aliases[alias] = name
                sharing_issues = _lib3_resource_sharing_issues(book)
                if sharing_issues:
                    first = sharing_issues[0]
                    raise LibError(
                        f"{first.loc}: {first.msg}",
                        code="invalid_lib3_resource_sharing",
                        details={
                            "issues": [
                                value.as_dict() for value in sharing_issues
                            ],
                        },
                    )
                graph_issues = _lib3_graph_issues(
                    book,
                    {},
                    page_numbers=[
                        int(match.group(1))
                        for name in by_name
                        if (match := _PAGE_MEMBER.fullmatch(name)) is not None
                    ],
                    check_resources=False,
                )
                if graph_issues:
                    first = graph_issues[0]
                    raise LibError(
                        f"{first.loc}: {first.msg}",
                        code="invalid_lib3_graph",
                        details={
                            "issues": [
                                value.as_dict() for value in graph_issues
                            ],
                        },
                    )
                inflated = sum(
                    int(value.file_size)
                    for value in infos
                    if not value.is_dir()
                )
                if inflated > MAX_INFLATED:
                    raise LibError(
                        "the .lib expands beyond the size cap",
                        code="lib_inflated_limit_exceeded",
                        details={
                            "inflated_bytes": inflated,
                            "maximum_bytes": MAX_INFLATED,
                        },
                    )
                resource_groups = _lib3_resource_groups(book)
                declared_resources = set(resource_groups)
                for member in declared_resources:
                    if member not in by_name:
                        raise LibError(
                            f"declared resource member {member!r} is missing",
                            code="missing_lib3_resource_member",
                            details={"member": member},
                        )
                for name, member_info in by_name.items():
                    if member_info.is_dir():
                        continue
                    if (
                        name in _KNOWN_MEMBERS
                        or _PAGE_MEMBER.fullmatch(name)
                        or _ASSET_MEMBER.fullmatch(name)
                        or _TRANS_MEMBER.fullmatch(name)
                        or name in declared_resources
                    ):
                        continue
                    raise LibError(
                        f"undeclared lib/3 member {name!r}",
                        code="undeclared_lib3_member",
                        details={"member": name},
                    )

            budget = MAX_INFLATED
            for name, member_info in by_name.items():
                if member_info.is_dir():
                    continue
                if name in declared_resources:
                    declared = int(member_info.file_size)
                    if declared > MAX_RESOURCE:
                        raise LibError(
                            f"resource member {name!r} exceeds the size cap",
                            code="lib3_resource_too_large",
                            details={
                                "member": name,
                                "maximum_bytes": MAX_RESOURCE,
                            },
                        )
                    content = zipped.read(member_info)
                    actual = hashlib.sha256(content).hexdigest()
                    mismatches = [
                        {
                            "location": f"{loc}/content_sha256",
                            "expected": record.get("content_sha256"),
                        }
                        for loc, record in [
                            *resource_groups[name]["representations"],
                            *resource_groups[name]["artifacts"],
                        ]
                        if record.get("content_sha256") != actual
                    ]
                    if mismatches:
                        raise LibError(
                            f"resource checksum mismatch for {name!r}",
                            code="lib3_checksum_mismatch",
                            details={
                                "member": name,
                                "declarations": mismatches,
                                "actual": actual,
                            },
                        )
                    doc.resources[name] = content
                    continue

                page_match = _PAGE_MEMBER.fullmatch(name)
                if page_match:
                    page_number = int(page_match.group(1))
                    if not 1 <= page_number <= 99999:
                        doc.skipped.append((name, "page number out of range"))
                        continue
                    if len(doc.pages) >= MAX_PAGES:
                        doc.skipped.append((name, "beyond the page cap"))
                        continue
                    declared = int(member_info.file_size)
                    if declared > MAX_JSON or declared > budget:
                        doc.skipped.append((name, "exceeds the size cap"))
                        continue
                    budget -= declared
                    try:
                        payload = zipped.read(member_info)
                        record = (
                            _strict_json(payload)
                            if major >= 3
                            else json.loads(payload)
                        )
                    except (UnicodeError, ValueError):
                        doc.skipped.append((name, "not valid JSON"))
                        continue
                    if not isinstance(record, dict):
                        doc.skipped.append((name, "not an object"))
                        continue
                    doc.pages.append(LibPage(
                        page=page_number,
                        doc=str(record.get("doc") or "compiled.txt"),
                        dims=(
                            record.get("dims")
                            if isinstance(record.get("dims"), dict)
                            else {}
                        ),
                        state=str(record.get("state") or ""),
                        items=(
                            record.get("items")
                            if isinstance(record.get("items"), list)
                            else []
                        ),
                        ext=(
                            record.get("ext")
                            if isinstance(record.get("ext"), dict)
                            else {}
                        ),
                        raw=record,
                    ))
                    continue

                translation_match = _TRANS_MEMBER.fullmatch(name)
                if translation_match:
                    declared = int(member_info.file_size)
                    if declared > MAX_JSON or declared > budget:
                        doc.skipped.append((name, "exceeds the size cap"))
                        continue
                    budget -= declared
                    try:
                        payload = zipped.read(member_info)
                        translation = (
                            _strict_json(payload)
                            if major >= 3
                            else json.loads(payload)
                        )
                    except (UnicodeError, ValueError):
                        doc.skipped.append((name, "not valid JSON"))
                        continue
                    if isinstance(translation, dict):
                        doc.translations[
                            translation_match.group(1).lower()
                        ] = translation
                    else:
                        doc.skipped.append((name, "not an object"))
                    continue

                asset_match = _ASSET_MEMBER.fullmatch(name)
                if asset_match:
                    declared = int(member_info.file_size)
                    if declared <= MAX_FIGURE and declared <= budget:
                        budget -= declared
                        doc.assets[asset_match.group(1)] = zipped.read(member_info)
                    else:
                        doc.skipped.append((name, "exceeds the size cap"))
    except LibError:
        raise
    except (
        zipfile.BadZipFile,
        RuntimeError,
        OSError,
        EOFError,
        NotImplementedError,
    ) as exc:
        raise LibError("not a .lib archive", code="invalid_lib_archive") from exc
    doc.pages.sort(key=lambda page: page.page)
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


def _capture_manifest(doc: LibDocument, *, book_id: str, generator: str,
                      instructions_book: str) -> dict:
    """Seal a document into the portable lib/3 capture graph manifest."""
    allowed_book_fields = {
        "format_version",
        "generator",
        "book_id",
        "created_at",
        "source",
        "meta",
        "capabilities",
        "roles",
        "instructions",
        "stylesheet",
        "templates",
        "figures",
        "pages",
        "representations",
        "artifacts",
        "review_policy",
        "ext",
    }
    extra_book_fields = set(doc.book) - allowed_book_fields
    if extra_book_fields:
        raise LibError(
            "lib/3 manifest has unknown fields; move extension data to ext",
            code="invalid_lib3_graph",
            details={
                "location": "book.json",
                "fields": sorted(extra_book_fields),
            },
        )
    manifest = _book_manifest(
        doc,
        book_id=book_id,
        generator=generator,
        instructions_book=instructions_book,
    )
    manifest["format_version"] = CAPTURE_FORMAT_VERSION
    manifest["capabilities"] = list(CAPTURE_CAPABILITIES)
    manifest["ext"] = _portable_ext(
        doc.book.get("ext", {}), loc="book.json/ext"
    )
    # The typed graph is the mutable in-memory source of truth. Falling back
    # to stale raw manifest arrays would resurrect records a caller removed
    # and would make validate() disagree with the bytes write_lib() seals.
    representation_values = doc.representations
    artifact_values = doc.artifacts
    if not isinstance(representation_values, list):
        raise LibError(
            "book.json/representations must be an array",
            code="invalid_lib3_graph",
            details={"location": "book.json/representations"},
        )
    if not isinstance(artifact_values, list):
        raise LibError(
            "book.json/artifacts must be an array",
            code="invalid_lib3_graph",
            details={"location": "book.json/artifacts"},
        )
    manifest["representations"] = [
        _representation_record(value) for value in representation_values
    ]
    manifest["artifacts"] = [
        _artifact_record(value) for value in artifact_values
    ]
    policy = doc.book.get("review_policy")
    if policy is None:
        policy = {"mode": "all-durable"}
    manifest["review_policy"] = _json_clone(
        policy, loc="book.json/review_policy"
    )
    return manifest


def _graph_issue(issues: list[Issue], loc: str, message: str) -> None:
    issues.append(Issue("error", loc, message))


def _lib3_resource_groups(
    book: Mapping,
) -> dict[str, dict[str, list[tuple[str, dict]]]]:
    """Index graph declarations without conflating logical and physical data.

    One representation owns one physical member. Raster-image artifacts may
    project assertions over those same bytes, so callers need all declarations
    rather than a lossy ``member -> record`` map.
    """
    groups: dict[str, dict[str, list[tuple[str, dict]]]] = {}
    for collection in ("representations", "artifacts"):
        records = book.get(collection)
        if not isinstance(records, list):
            continue
        for index, record in enumerate(records):
            if not isinstance(record, dict):
                continue
            member = record.get("member")
            if not isinstance(member, str):
                continue
            group = groups.setdefault(
                member,
                {"representations": [], "artifacts": []},
            )
            group[collection].append(
                (f"book.json/{collection}[{index}]", record)
            )
    return groups


def _lib3_resource_sharing_issues(book: Mapping) -> list[Issue]:
    """Validate the sole lib/3 exception to unique member declarations.

    A representation member may also be named by any number of raster-image
    artifacts when every alias pins that exact representation revision and
    describes identical bytes. All other same-set or cross-set reuse is
    ambiguous and therefore invalid.
    """
    issues: list[Issue] = []
    groups = _lib3_resource_groups(book)
    aliases: dict[str, tuple[str, str]] = {}
    for member, group in groups.items():
        declarations = [
            *group["representations"],
            *group["artifacts"],
        ]
        first_loc = declarations[0][0] if declarations else member
        alias = member.casefold()
        previous = aliases.get(alias)
        if previous is not None and previous[0] != member:
            _graph_issue(
                issues,
                f"{first_loc}/member",
                (
                    f"member aliases {previous[0]!r} on "
                    "case-insensitive filesystems"
                ),
            )
        else:
            aliases[alias] = (member, first_loc)

        representations = group["representations"]
        artifacts = group["artifacts"]
        if len(representations) > 1:
            for loc, _record in representations[1:]:
                _graph_issue(
                    issues,
                    f"{loc}/member",
                    "representation member is declared more than once",
                )
        if not representations:
            if len(artifacts) > 1:
                for loc, _record in artifacts[1:]:
                    _graph_issue(
                        issues,
                        f"{loc}/member",
                        (
                            "artifact-only member is declared more than once; "
                            "only representation-owned bytes may be shared"
                        ),
                    )
            if _safe_declared_member(member, "representations"):
                for loc, _record in artifacts:
                    _graph_issue(
                        issues,
                        f"{loc}/member",
                        (
                            "artifact references a representations/ member "
                            "with no representation owner"
                        ),
                    )
            continue
        if len(representations) != 1 or not artifacts:
            continue

        representation_loc, representation = representations[0]
        if not _safe_declared_member(member, "representations"):
            for loc, _record in artifacts:
                _graph_issue(
                    issues,
                    f"{loc}/member",
                    (
                        "a shared resource must be owned below "
                        "representations/"
                    ),
                )
            continue
        for loc, artifact in artifacts:
            if artifact.get("kind") != "raster-image":
                _graph_issue(
                    issues,
                    f"{loc}/kind",
                    "shared resource artifact must have kind raster-image",
                )
            source = artifact.get("source")
            if not isinstance(source, dict):
                _graph_issue(
                    issues,
                    f"{loc}/source",
                    (
                        "shared resource artifact source must pin its "
                        "representation owner"
                    ),
                )
            else:
                if source.get("representation_id") != representation.get("id"):
                    _graph_issue(
                        issues,
                        f"{loc}/source/representation_id",
                        (
                            "shared resource source must pin the owning "
                            f"representation at {representation_loc}"
                        ),
                    )
                if source.get("representation_revision") != representation.get(
                    "revision"
                ):
                    _graph_issue(
                        issues,
                        f"{loc}/source/representation_revision",
                        (
                            "shared resource source must pin the owning "
                            "representation revision"
                        ),
                    )
            for field_name in (
                "media_type",
                "content_sha256",
                "dimensions",
            ):
                if artifact.get(field_name) != representation.get(field_name):
                    label = (
                        "checksum"
                        if field_name == "content_sha256"
                        else field_name
                    )
                    _graph_issue(
                        issues,
                        f"{loc}/{field_name}",
                        (
                            f"shared resource {label} must match the owning "
                            "representation"
                        ),
                    )
    return issues


def _valid_id(value) -> bool:
    return isinstance(value, str) and bool(PORTABLE_ID_RE.fullmatch(value))


def _valid_revision(value) -> bool:
    return isinstance(value, str) and bool(REVISION_RE.fullmatch(value))


def _check_ext(issues: list[Issue], value, loc: str) -> None:
    if not isinstance(value, dict):
        _graph_issue(issues, loc, "ext must be an object")
        return
    if not value:
        return
    try:
        encoded = json.dumps(
            value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        _graph_issue(issues, loc, "ext is not strict JSON")
        return
    if len(encoded) > MAX_EXT:
        _graph_issue(issues, loc, f"ext exceeds {MAX_EXT} bytes")
        return
    problem = _portable_ext_problem(value, loc=loc)
    if problem:
        location, message = problem
        _graph_issue(issues, location, message)


def _check_dimensions(issues: list[Issue], value, loc: str) -> None:
    if not isinstance(value, dict):
        _graph_issue(issues, loc, "dimensions must be an object")
        return
    if set(value) != {"width", "height", "orientation"}:
        _graph_issue(
            issues,
            loc,
            "dimensions fields must be width, height, and orientation",
        )
        return
    for field_name in ("width", "height"):
        number = value.get(field_name)
        if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
            _graph_issue(
                issues, f"{loc}/{field_name}", "must be a positive integer"
            )
    orientation = value.get("orientation")
    if (
        isinstance(orientation, bool)
        or not isinstance(orientation, int)
        or orientation not in range(1, 9)
    ):
        _graph_issue(
            issues,
            f"{loc}/orientation",
            "must be an EXIF orientation from 1 through 8",
        )


def _check_provenance(issues: list[Issue], value, loc: str, *,
                      required: bool = True) -> None:
    if not isinstance(value, dict):
        if required:
            _graph_issue(issues, loc, "provenance must be an object")
        return
    allowed = {
        "origin",
        "provider_id",
        "model",
        "recipe_revision",
        "operation_id",
        "generated_at",
        "ext",
    }
    extra = set(value) - allowed
    if extra:
        _graph_issue(
            issues, loc, f"unknown provenance fields: {sorted(extra)}"
        )
    if not _valid_id(value.get("origin")):
        _graph_issue(
            issues, f"{loc}/origin", "origin must be a portable identifier"
        )
    provider = value.get("provider_id", "")
    if provider and not _valid_id(provider):
        _graph_issue(
            issues,
            f"{loc}/provider_id",
            "provider_id must be a portable identifier",
        )
    operation = value.get("operation_id", "")
    if operation and not _valid_id(operation):
        _graph_issue(
            issues,
            f"{loc}/operation_id",
            "operation_id must be a portable identifier",
        )
    recipe = value.get("recipe_revision", "")
    if recipe and not _valid_revision(recipe):
        _graph_issue(
            issues, f"{loc}/recipe_revision", "recipe_revision is invalid"
        )
    for field_name, maximum in (
        ("model", 256),
        ("generated_at", 128),
    ):
        supplied = value.get(field_name, "")
        if not isinstance(supplied, str) or len(supplied) > maximum:
            _graph_issue(
                issues,
                f"{loc}/{field_name}",
                f"{field_name} must be a bounded string",
            )
    _check_ext(issues, value.get("ext", {}), f"{loc}/ext")


def _check_source(issues: list[Issue], value, loc: str) -> None:
    if not isinstance(value, dict):
        _graph_issue(issues, loc, "source must be an object")
        return
    allowed = {
        "representation_id",
        "representation_revision",
        "canvas_id",
        "canvas_revision",
    }
    if set(value) - allowed:
        _graph_issue(
            issues, loc, f"unknown source fields: {sorted(set(value) - allowed)}"
        )
    if not _valid_id(value.get("representation_id")):
        _graph_issue(
            issues,
            f"{loc}/representation_id",
            "representation_id must be a portable identifier",
        )
    if not _valid_revision(value.get("representation_revision")):
        _graph_issue(
            issues,
            f"{loc}/representation_revision",
            "representation_revision is invalid",
        )
    canvas_id = value.get("canvas_id", "")
    canvas_revision = value.get("canvas_revision", "")
    if bool(canvas_id) != bool(canvas_revision):
        _graph_issue(
            issues,
            loc,
            "canvas_id and canvas_revision must be supplied together",
        )
    if canvas_id and not _valid_id(canvas_id):
        _graph_issue(
            issues, f"{loc}/canvas_id", "canvas_id is not portable"
        )
    if canvas_revision and not _valid_revision(canvas_revision):
        _graph_issue(
            issues, f"{loc}/canvas_revision", "canvas_revision is invalid"
        )


def _check_selector(issues: list[Issue], value, loc: str, source: dict) -> None:
    if not isinstance(value, dict):
        _graph_issue(issues, loc, "selector must be an object")
        return
    if set(value) != {
        "type",
        "coordinate_space",
        "coordinate_space_revision",
        "points",
    }:
        _graph_issue(
            issues,
            loc,
            "selector fields must be type, coordinate_space, "
            "coordinate_space_revision, and points",
        )
        return
    if value.get("type") != "polygon":
        _graph_issue(issues, f"{loc}/type", "only polygon selectors are valid")
    if not _valid_id(value.get("coordinate_space")):
        _graph_issue(
            issues,
            f"{loc}/coordinate_space",
            "coordinate_space must be a portable identifier",
        )
    coordinate_revision = value.get("coordinate_space_revision")
    if not _valid_revision(coordinate_revision):
        _graph_issue(
            issues,
            f"{loc}/coordinate_space_revision",
            "coordinate_space_revision is invalid",
        )
    expected_revision = (
        source.get("canvas_revision")
        or source.get("representation_revision")
        if isinstance(source, dict)
        else ""
    )
    if expected_revision and coordinate_revision != expected_revision:
        _graph_issue(
            issues,
            f"{loc}/coordinate_space_revision",
            "selector does not pin the source coordinate revision",
        )
    points = value.get("points")
    if not isinstance(points, list) or not 3 <= len(points) <= 64:
        _graph_issue(issues, f"{loc}/points", "polygon needs 3 through 64 points")
        return
    coordinates: list[tuple[float, float]] = []
    for index, point in enumerate(points):
        point_loc = f"{loc}/points[{index}]"
        if not isinstance(point, dict) or set(point) != {"x", "y"}:
            _graph_issue(
                issues, point_loc, "point fields must be x and y"
            )
            continue
        pair: list[float] = []
        for axis in ("x", "y"):
            number = point.get(axis)
            if (
                isinstance(number, bool)
                or not isinstance(number, (int, float))
                or not math.isfinite(float(number))
                or not 0 <= float(number) <= 1
            ):
                _graph_issue(
                    issues,
                    f"{point_loc}/{axis}",
                    "coordinate must be a finite number from zero through one",
                )
                pair = []
                break
            pair.append(float(number))
        if pair:
            coordinates.append((pair[0], pair[1]))
    if len(coordinates) == len(points):
        if len(set(coordinates)) != len(coordinates):
            _graph_issue(issues, f"{loc}/points", "polygon points must be unique")
        area_twice = sum(
            (point[0] * coordinates[(index + 1) % len(coordinates)][1])
            - (
                coordinates[(index + 1) % len(coordinates)][0]
                * point[1]
            )
            for index, point in enumerate(coordinates)
        )
        if math.isclose(area_twice, 0.0, abs_tol=1e-12):
            _graph_issue(issues, f"{loc}/points", "polygon must enclose an area")


def _check_assertions(issues: list[Issue], record: dict, loc: str) -> None:
    categories = record.get("category_assignments", [])
    if not isinstance(categories, list) or len(categories) > 3:
        _graph_issue(
            issues,
            f"{loc}/category_assignments",
            "category_assignments must be an array of at most three values",
        )
        categories = []
    origins: set[str] = set()
    for index, assignment in enumerate(categories):
        here = f"{loc}/category_assignments[{index}]"
        if not isinstance(assignment, dict):
            _graph_issue(issues, here, "category assignment must be an object")
            continue
        allowed = {
            "category",
            "origin",
            "revision",
            "inherited_from_artifact_id",
            "confidence",
            "provenance",
            "ext",
        }
        if set(assignment) - allowed:
            _graph_issue(
                issues,
                here,
                f"unknown category fields: {sorted(set(assignment) - allowed)}",
            )
        if assignment.get("category") not in IMAGE_CATEGORIES:
            _graph_issue(
                issues, f"{here}/category", "category is not canonical"
            )
        origin = assignment.get("origin")
        if origin not in ASSIGNMENT_ORIGINS:
            _graph_issue(
                issues, f"{here}/origin", "category origin is invalid"
            )
        elif origin in origins:
            _graph_issue(
                issues, f"{here}/origin", "category origin is duplicated"
            )
        else:
            origins.add(origin)
        if not _valid_revision(assignment.get("revision")):
            _graph_issue(
                issues, f"{here}/revision", "category revision is invalid"
            )
        inherited = assignment.get("inherited_from_artifact_id", "")
        if origin == "inherited" and not _valid_id(inherited):
            _graph_issue(
                issues,
                f"{here}/inherited_from_artifact_id",
                "inherited category must name an artifact",
            )
        elif origin != "inherited" and inherited:
            _graph_issue(
                issues,
                f"{here}/inherited_from_artifact_id",
                "only inherited categories may name an inherited artifact",
            )
        confidence = assignment.get("confidence")
        if confidence is not None and (
            isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not math.isfinite(float(confidence))
            or not 0 <= float(confidence) <= 1
        ):
            _graph_issue(
                issues, f"{here}/confidence", "confidence must be zero through one"
            )
        if "provenance" in assignment:
            _check_provenance(
                issues, assignment["provenance"], f"{here}/provenance"
            )
        _check_ext(issues, assignment.get("ext", {}), f"{here}/ext")

    captions = record.get("caption_assertions", [])
    if not isinstance(captions, list) or len(captions) > 32:
        _graph_issue(
            issues,
            f"{loc}/caption_assertions",
            "caption_assertions must be an array of at most 32 values",
        )
        captions = []
    origins = set()
    for index, assertion in enumerate(captions):
        here = f"{loc}/caption_assertions[{index}]"
        if not isinstance(assertion, dict):
            _graph_issue(issues, here, "caption assertion must be an object")
            continue
        allowed = {
            "text",
            "origin",
            "revision",
            "language",
            "source_annotation_id",
            "confidence",
            "provenance",
            "ext",
        }
        if set(assertion) - allowed:
            _graph_issue(
                issues,
                here,
                f"unknown caption fields: {sorted(set(assertion) - allowed)}",
            )
        text = assertion.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > 16_384:
            _graph_issue(
                issues, f"{here}/text", "caption text must be non-empty and bounded"
            )
        origin = assertion.get("origin")
        if origin not in CAPTION_ORIGINS:
            _graph_issue(issues, f"{here}/origin", "caption origin is invalid")
        elif origin in origins:
            _graph_issue(issues, f"{here}/origin", "caption origin is duplicated")
        else:
            origins.add(origin)
        if not _valid_revision(assertion.get("revision")):
            _graph_issue(
                issues, f"{here}/revision", "caption revision is invalid"
            )
        language = assertion.get("language", "")
        if language and (
            not isinstance(language, str) or not LANGUAGE_RE.fullmatch(language)
        ):
            _graph_issue(
                issues, f"{here}/language", "caption language tag is invalid"
            )
        annotation = assertion.get("source_annotation_id", "")
        if annotation and not _valid_id(annotation):
            _graph_issue(
                issues,
                f"{here}/source_annotation_id",
                "source_annotation_id is not portable",
            )
        confidence = assertion.get("confidence")
        if confidence is not None and (
            isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not math.isfinite(float(confidence))
            or not 0 <= float(confidence) <= 1
        ):
            _graph_issue(
                issues, f"{here}/confidence", "confidence must be zero through one"
            )
        if "provenance" in assertion:
            _check_provenance(
                issues, assertion["provenance"], f"{here}/provenance"
            )
        _check_ext(issues, assertion.get("ext", {}), f"{here}/ext")

    roles = record.get("role_assignments", [])
    if not isinstance(roles, list) or len(roles) > 3:
        _graph_issue(
            issues,
            f"{loc}/role_assignments",
            "role_assignments must be an array of at most three values",
        )
        roles = []
    origins = set()
    for index, assignment in enumerate(roles):
        here = f"{loc}/role_assignments[{index}]"
        if not isinstance(assignment, dict):
            _graph_issue(issues, here, "role assignment must be an object")
            continue
        allowed = {
            "role",
            "origin",
            "revision",
            "confidence",
            "provenance",
            "ext",
        }
        if set(assignment) - allowed:
            _graph_issue(
                issues,
                here,
                f"unknown role fields: {sorted(set(assignment) - allowed)}",
            )
        if not isinstance(assignment.get("role"), str) or not ROLE_RE.fullmatch(
            assignment["role"]
        ):
            _graph_issue(issues, f"{here}/role", "spatial role is invalid")
        origin = assignment.get("origin")
        if origin not in ROLE_ASSIGNMENT_ORIGINS:
            _graph_issue(issues, f"{here}/origin", "role origin is invalid")
        elif origin in origins:
            _graph_issue(issues, f"{here}/origin", "role origin is duplicated")
        else:
            origins.add(origin)
        if not _valid_revision(assignment.get("revision")):
            _graph_issue(issues, f"{here}/revision", "role revision is invalid")
        confidence = assignment.get("confidence")
        if confidence is not None and (
            isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not math.isfinite(float(confidence))
            or not 0 <= float(confidence) <= 1
        ):
            _graph_issue(
                issues, f"{here}/confidence", "confidence must be zero through one"
            )
        if "provenance" in assignment:
            _check_provenance(
                issues, assignment["provenance"], f"{here}/provenance"
            )
        _check_ext(issues, assignment.get("ext", {}), f"{here}/ext")


def _lib3_graph_issues(
    book: Mapping,
    resources: Mapping[str, bytes],
    *,
    page_numbers=None,
    check_resources: bool = True,
) -> list[Issue]:
    issues: list[Issue] = []
    allowed_book_fields = {
        "format_version",
        "generator",
        "book_id",
        "created_at",
        "source",
        "meta",
        "capabilities",
        "roles",
        "instructions",
        "stylesheet",
        "templates",
        "figures",
        "pages",
        "representations",
        "artifacts",
        "review_policy",
        "ext",
    }
    extra_book_fields = set(book) - allowed_book_fields
    if extra_book_fields:
        _graph_issue(
            issues,
            "book.json",
            f"unknown manifest fields: {sorted(extra_book_fields)}",
        )
    if not BOOK_ID_RE.fullmatch(str(book.get("book_id") or "")):
        _graph_issue(
            issues, "book.json/book_id", "lib/3 requires a stable b-<uuidhex> id"
        )
    representations = book.get("representations")
    artifacts = book.get("artifacts")
    if not isinstance(representations, list):
        _graph_issue(
            issues, "book.json/representations", "representations must be an array"
        )
        representations = []
    if not isinstance(artifacts, list):
        _graph_issue(issues, "book.json/artifacts", "artifacts must be an array")
        artifacts = []
    pages = book.get("pages")
    declared_pages: list[int] = []
    if not isinstance(pages, list):
        _graph_issue(issues, "book.json/pages", "pages must be an array")
        pages = []
    elif len(pages) > MAX_PAGES:
        _graph_issue(
            issues,
            "book.json/pages",
            f"more than {MAX_PAGES} pages",
        )
    declared_page_set: set[int] = set()
    for index, page_number in enumerate(pages):
        loc = f"book.json/pages[{index}]"
        if isinstance(page_number, bool) or not isinstance(page_number, int):
            _graph_issue(issues, loc, "page number must be an integer")
            continue
        if not 1 <= page_number <= 99999:
            _graph_issue(issues, loc, "page number must be between 1 and 99999")
            continue
        if page_number in declared_page_set:
            _graph_issue(issues, loc, "page number is duplicated")
            continue
        declared_page_set.add(page_number)
        declared_pages.append(page_number)
    if page_numbers is not None:
        physical_page_set: set[int] = set()
        for page_number in page_numbers:
            if isinstance(page_number, bool) or not isinstance(page_number, int):
                _graph_issue(
                    issues,
                    "pages",
                    "archive page member number must be an integer",
                )
                continue
            if not 1 <= page_number <= 99999:
                _graph_issue(
                    issues,
                    f"pages/{page_number}.json",
                    "archive page member number must be between 1 and 99999",
                )
                continue
            if page_number in physical_page_set:
                _graph_issue(
                    issues,
                    f"pages/{page_number}.json",
                    "multiple archive members resolve to the same page number",
                )
                continue
            physical_page_set.add(page_number)
        for page_number in sorted(declared_page_set - physical_page_set):
            _graph_issue(
                issues,
                f"pages/{page_number}.json",
                "page is declared in book.json but its member is missing",
            )
        for page_number in sorted(physical_page_set - declared_page_set):
            _graph_issue(
                issues,
                f"pages/{page_number}.json",
                "page member is not declared in book.json/pages",
            )
    if len(representations) > MAX_REPRESENTATIONS:
        _graph_issue(
            issues,
            "book.json/representations",
            f"more than {MAX_REPRESENTATIONS} representations",
        )
    if len(artifacts) > MAX_ARTIFACTS:
        _graph_issue(
            issues,
            "book.json/artifacts",
            f"more than {MAX_ARTIFACTS} artifacts",
        )
    if not representations and not declared_pages:
        _graph_issue(
            issues,
            "book.json",
            "lib/3 needs at least one representation or one legacy page",
        )

    representation_by_id: dict[str, dict] = {}
    pending_rep_lineage: list[tuple[str, dict]] = []
    for index, record in enumerate(representations):
        loc = f"book.json/representations[{index}]"
        if not isinstance(record, dict):
            _graph_issue(issues, loc, "representation must be an object")
            continue
        allowed = {
            "id",
            "revision",
            "role",
            "media_type",
            "member",
            "content_sha256",
            "dimensions",
            "lineage",
            "ext",
        }
        extra = set(record) - allowed
        if extra:
            _graph_issue(
                issues, loc, f"unknown representation fields: {sorted(extra)}"
            )
        for field_name in sorted(
            _REPRESENTATION_REQUIRED_FIELDS - set(record)
        ):
            _graph_issue(
                issues,
                f"{loc}/{field_name}",
                f"{field_name} is required",
            )
        identity = record.get("id")
        if not _valid_id(identity):
            _graph_issue(
                issues, f"{loc}/id", "id must be a portable opaque identifier"
            )
        elif identity.casefold() in representation_by_id:
            _graph_issue(
                issues, f"{loc}/id", "representation identity is duplicated"
            )
        else:
            representation_by_id[identity.casefold()] = record
        if not _valid_revision(record.get("revision")):
            _graph_issue(issues, f"{loc}/revision", "revision is invalid")
        if not _valid_id(record.get("role")):
            _graph_issue(
                issues, f"{loc}/role", "role must be a portable identifier"
            )
        media_type = record.get("media_type")
        if not isinstance(media_type, str) or not MEDIA_TYPE_RE.fullmatch(media_type):
            _graph_issue(issues, f"{loc}/media_type", "media_type is invalid")
        member = record.get("member")
        if not _safe_declared_member(member, "representations"):
            _graph_issue(
                issues,
                f"{loc}/member",
                "member must be below representations/ with portable segments",
            )
        digest = record.get("content_sha256")
        if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
            _graph_issue(
                issues,
                f"{loc}/content_sha256",
                "content_sha256 must be a lowercase SHA-256 digest",
            )
        raster_media = (
            isinstance(media_type, str)
            and media_type.casefold().startswith("image/")
        )
        if "dimensions" in record or raster_media:
            _check_dimensions(
                issues,
                record.get("dimensions"),
                f"{loc}/dimensions",
            )
        lineage = record.get("lineage", [])
        if not isinstance(lineage, list) or len(lineage) > 64:
            _graph_issue(
                issues, f"{loc}/lineage", "lineage must have at most 64 entries"
            )
        else:
            for lineage_index, link in enumerate(lineage):
                link_loc = f"{loc}/lineage[{lineage_index}]"
                if not isinstance(link, dict) or set(link) != {
                    "representation_id",
                    "representation_revision",
                    "relation",
                }:
                    _graph_issue(
                        issues,
                        link_loc,
                        "representation lineage fields are invalid",
                    )
                    continue
                if not _valid_id(link.get("representation_id")):
                    _graph_issue(
                        issues,
                        f"{link_loc}/representation_id",
                        "lineage representation_id is invalid",
                    )
                if not _valid_revision(link.get("representation_revision")):
                    _graph_issue(
                        issues,
                        f"{link_loc}/representation_revision",
                        "lineage representation_revision is invalid",
                    )
                if not _valid_id(link.get("relation")):
                    _graph_issue(
                        issues,
                        f"{link_loc}/relation",
                        "lineage relation is invalid",
                    )
                pending_rep_lineage.append((link_loc, link))
        _check_ext(issues, record.get("ext", {}), f"{loc}/ext")

    for loc, link in pending_rep_lineage:
        target = representation_by_id.get(
            str(link.get("representation_id") or "").casefold()
        )
        if target is None:
            _graph_issue(
                issues, f"{loc}/representation_id", "lineage target is missing"
            )
        elif target.get("id") != link.get("representation_id"):
            _graph_issue(
                issues,
                f"{loc}/representation_id",
                "lineage target identity must match exactly",
            )
        elif target.get("revision") != link.get("representation_revision"):
            _graph_issue(
                issues,
                f"{loc}/representation_revision",
                "lineage target revision does not match",
            )

    artifact_by_id: dict[str, dict] = {}
    pending_relationships: list[tuple[str, dict, str]] = []
    for index, record in enumerate(artifacts):
        loc = f"book.json/artifacts[{index}]"
        if not isinstance(record, dict):
            _graph_issue(issues, loc, "artifact must be an object")
            continue
        allowed = {
            "id",
            "revision",
            "kind",
            "media_type",
            "member",
            "content_sha256",
            "source",
            "dimensions",
            "provenance",
            "category_assignments",
            "caption_assertions",
            "role_assignments",
            "selector",
            "relationships",
            "ext",
        }
        extra = set(record) - allowed
        if extra:
            _graph_issue(issues, loc, f"unknown artifact fields: {sorted(extra)}")
        for field_name in sorted(_ARTIFACT_REQUIRED_FIELDS - set(record)):
            _graph_issue(
                issues,
                f"{loc}/{field_name}",
                f"{field_name} is required",
            )
        identity = record.get("id")
        if not _valid_id(identity):
            _graph_issue(
                issues, f"{loc}/id", "id must be a portable opaque identifier"
            )
        elif identity.casefold() in artifact_by_id:
            _graph_issue(issues, f"{loc}/id", "artifact identity is duplicated")
        else:
            artifact_by_id[identity.casefold()] = record
        if not _valid_revision(record.get("revision")):
            _graph_issue(issues, f"{loc}/revision", "revision is invalid")
        if not _valid_id(record.get("kind")):
            _graph_issue(
                issues, f"{loc}/kind", "kind must be a portable identifier"
            )
        media_type = record.get("media_type")
        if not isinstance(media_type, str) or not MEDIA_TYPE_RE.fullmatch(media_type):
            _graph_issue(issues, f"{loc}/media_type", "media_type is invalid")
        member = record.get("member")
        if not (
            _safe_declared_member(member, "artifacts")
            or _safe_declared_member(member, "representations")
        ):
            _graph_issue(
                issues,
                f"{loc}/member",
                (
                    "member must be below artifacts/ or be a validated shared "
                    "representations/ member"
                ),
            )
        digest = record.get("content_sha256")
        if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
            _graph_issue(
                issues,
                f"{loc}/content_sha256",
                "content_sha256 must be a lowercase SHA-256 digest",
            )
        source = record.get("source")
        _check_source(issues, source, f"{loc}/source")
        if isinstance(source, dict):
            source_record = representation_by_id.get(
                str(source.get("representation_id") or "").casefold()
            )
            if source_record is None:
                _graph_issue(
                    issues,
                    f"{loc}/source/representation_id",
                    "source representation is missing",
                )
            elif source_record.get("id") != source.get("representation_id"):
                _graph_issue(
                    issues,
                    f"{loc}/source/representation_id",
                    "source representation identity must match exactly",
                )
            elif source_record.get("revision") != source.get(
                "representation_revision"
            ):
                _graph_issue(
                    issues,
                    f"{loc}/source/representation_revision",
                    "source representation revision does not match",
                )
        raster_media = (
            isinstance(media_type, str)
            and media_type.casefold().startswith("image/")
        )
        if "dimensions" in record or raster_media:
            _check_dimensions(
                issues,
                record.get("dimensions"),
                f"{loc}/dimensions",
            )
        _check_provenance(
            issues, record.get("provenance"), f"{loc}/provenance"
        )
        _check_assertions(issues, record, loc)
        if "selector" in record:
            _check_selector(
                issues,
                record.get("selector"),
                f"{loc}/selector",
                source,
            )
        elif record.get("kind") == "spatial-annotation":
            _graph_issue(
                issues,
                f"{loc}/selector",
                "spatial-annotation artifacts require a selector",
            )
        relationships = record.get("relationships", [])
        if not isinstance(relationships, list) or len(relationships) > 64:
            _graph_issue(
                issues,
                f"{loc}/relationships",
                "relationships must have at most 64 entries",
            )
        else:
            seen_links: set[tuple[str, str]] = set()
            for relationship_index, link in enumerate(relationships):
                link_loc = f"{loc}/relationships[{relationship_index}]"
                if not isinstance(link, dict) or set(link) != {
                    "artifact_id",
                    "artifact_revision",
                    "relation",
                }:
                    _graph_issue(
                        issues, link_loc, "artifact relationship fields are invalid"
                    )
                    continue
                if not _valid_id(link.get("artifact_id")):
                    _graph_issue(
                        issues,
                        f"{link_loc}/artifact_id",
                        "relationship artifact_id is invalid",
                    )
                if not _valid_revision(link.get("artifact_revision")):
                    _graph_issue(
                        issues,
                        f"{link_loc}/artifact_revision",
                        "relationship artifact_revision is invalid",
                    )
                if not _valid_id(link.get("relation")):
                    _graph_issue(
                        issues,
                        f"{link_loc}/relation",
                        "relationship relation is invalid",
                    )
                key = (str(link.get("relation")), str(link.get("artifact_id")))
                if key in seen_links:
                    _graph_issue(
                        issues, link_loc, "artifact relationship is duplicated"
                    )
                seen_links.add(key)
                if link.get("artifact_id") == identity:
                    _graph_issue(
                        issues, f"{link_loc}/artifact_id", "self-links are invalid"
                    )
                pending_relationships.append((link_loc, link, str(identity)))
        _check_ext(issues, record.get("ext", {}), f"{loc}/ext")

    for loc, link, _owner in pending_relationships:
        target = artifact_by_id.get(str(link.get("artifact_id") or "").casefold())
        if target is None:
            _graph_issue(
                issues, f"{loc}/artifact_id", "relationship target is missing"
            )
        elif target.get("id") != link.get("artifact_id"):
            _graph_issue(
                issues,
                f"{loc}/artifact_id",
                "relationship target identity must match exactly",
            )
        elif target.get("revision") != link.get("artifact_revision"):
            _graph_issue(
                issues,
                f"{loc}/artifact_revision",
                "relationship target revision does not match",
            )
    for index, record in enumerate(artifacts):
        if not isinstance(record, dict):
            continue
        for assignment_index, assignment in enumerate(
            record.get("category_assignments", [])
            if isinstance(record.get("category_assignments"), list)
            else []
        ):
            if not isinstance(assignment, dict) or assignment.get(
                "origin"
            ) != "inherited":
                continue
            inherited_id = assignment.get("inherited_from_artifact_id")
            target = artifact_by_id.get(str(inherited_id or "").casefold())
            loc = (
                f"book.json/artifacts[{index}]/category_assignments"
                f"[{assignment_index}]/inherited_from_artifact_id"
            )
            if target is None:
                _graph_issue(issues, loc, "inherited category source is missing")
            elif target.get("id") != inherited_id:
                _graph_issue(
                    issues,
                    loc,
                    "inherited category source identity must match exactly",
                )

    issues.extend(_lib3_resource_sharing_issues(book))
    declared_groups = _lib3_resource_groups(book)
    declared_members = set(declared_groups)
    if check_resources:
        for member in sorted(declared_members):
            content = resources.get(member)
            if not isinstance(content, bytes):
                _graph_issue(
                    issues,
                    member,
                    "declared member bytes are missing from the document",
                )
                continue
            actual = hashlib.sha256(content).hexdigest()
            declarations = [
                *declared_groups[member]["representations"],
                *declared_groups[member]["artifacts"],
            ]
            for loc, owner in declarations:
                expected = owner.get("content_sha256")
                if expected != actual:
                    _graph_issue(
                        issues,
                        f"{loc}/content_sha256",
                        (
                            "content checksum mismatch "
                            f"(expected {expected}, got {actual})"
                        ),
                    )
            if len(content) > MAX_RESOURCE:
                _graph_issue(
                    issues, member, f"member exceeds {MAX_RESOURCE} bytes"
                )
        undeclared = set(resources) - declared_members
        for member in sorted(undeclared):
            _graph_issue(
                issues,
                member,
                "resource bytes have no graph declaration",
            )
        total_resource_bytes = sum(
            len(value)
            for value in resources.values()
            if isinstance(value, bytes)
        )
        if total_resource_bytes > MAX_INFLATED:
            _graph_issue(
                issues,
                "resources",
                (
                    "declared resources exceed the "
                    f"{MAX_INFLATED}-byte inflation budget"
                ),
            )

    policy = book.get("review_policy")
    if not isinstance(policy, dict) or set(policy) != {"mode"}:
        _graph_issue(
            issues,
            "book.json/review_policy",
            "review_policy must contain exactly one mode field",
        )
    elif policy.get("mode") not in REVIEW_EXPORT_MODES:
        _graph_issue(
            issues,
            "book.json/review_policy/mode",
            "review export mode is invalid",
        )
    elif policy.get("mode") == "none" and any(
        isinstance(value, dict) and value.get("kind") == "correction-review"
        for value in artifacts
    ):
        _graph_issue(
            issues,
            "book.json/review_policy",
            "review artifacts conflict with the none export policy",
        )
    _check_ext(issues, book.get("ext", {}), "book.json/ext")
    return issues


def _preflight_sealed_archive(
    path: Path,
    infos: tuple[zipfile.ZipInfo, ...],
) -> None:
    """Refuse to publish an archive this reader cannot consume intact."""
    archive_bytes = path.stat().st_size
    if archive_bytes > MAX_BYTES:
        raise LibError(
            "archive too large",
            code="lib_archive_too_large",
            details={
                "archive_bytes": archive_bytes,
                "maximum_bytes": MAX_BYTES,
            },
        )
    if len(infos) > MAX_MEMBERS:
        raise LibError(
            "archive contains too many members",
            code="lib_member_limit_exceeded",
            details={
                "member_count": len(infos),
                "maximum_members": MAX_MEMBERS,
            },
        )

    names: set[str] = set()
    aliases: dict[str, str] = {}
    inflated = 0
    for info in infos:
        name = info.filename
        if name in names:
            raise LibError(
                f"duplicate archive member {name!r}",
                code="duplicate_lib_member",
                details={"member": name},
            )
        names.add(name)
        alias = name.casefold()
        previous = aliases.get(alias)
        if previous is not None and previous != name:
            raise LibError(
                "archive member names alias on case-insensitive systems",
                code="lib_member_alias",
                details={"members": [previous, name]},
            )
        aliases[alias] = name
        if info.is_dir():
            continue
        inflated += int(info.file_size)
        structured_json = (
            name in {"book.json", "schema.json"}
            or bool(_PAGE_MEMBER.fullmatch(name))
            or bool(_TRANS_MEMBER.fullmatch(name))
        )
        if structured_json and info.file_size > MAX_JSON:
            code = (
                "lib_book_too_large"
                if name == "book.json"
                else "lib_json_too_large"
            )
            raise LibError(
                f"{name} too large",
                code=code,
                details={
                    "member": name,
                    "member_bytes": int(info.file_size),
                    "maximum_bytes": MAX_JSON,
                },
            )
    if inflated > MAX_INFLATED:
        raise LibError(
            "the .lib expands beyond the size cap",
            code="lib_inflated_limit_exceeded",
            details={
                "inflated_bytes": inflated,
                "maximum_bytes": MAX_INFLATED,
            },
        )


def _write_lib(doc: LibDocument, path, *, generator: str = "library-tool/dev",
               book_id: str = "", instructions_book: str = "") -> None:
    """Seal a document as lib/2 or capture-aware lib/3.

    lib/1 documents retain their established upgrade-to-lib/2 behavior.
    A document explicitly marked major 3 is sealed with its complete
    representation/artifact graph and byte-for-byte checksummed resources.
    """
    if doc.format and doc.format[0] > SUPPORTED_MAJOR:
        raise LibError(f"cannot write format {doc.format[0]}.{doc.format[1]}")
    if doc.format and doc.format[0] == 3 and doc.format[1] > 0:
        raise LibError(
            f"cannot re-seal additive format "
            f"{doc.format[0]}.{doc.format[1]} as 3.0",
            code="newer_lib_minor_write_unsupported",
            details={"format_version": doc.format_version},
        )
    capture_aware = bool(doc.format and doc.format[0] >= 3)
    if not capture_aware and (
        doc.representations
        or doc.artifacts
        or doc.resources
        or doc.book.get("representations")
        or doc.book.get("artifacts")
    ):
        raise LibError(
            "capture graphs require format 3.0",
            code="lib3_format_required",
        )

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
    effective_instructions = instructions_book
    if capture_aware and not effective_instructions:
        existing_instructions = doc.book.get("instructions")
        if isinstance(existing_instructions, dict):
            effective_instructions = str(
                existing_instructions.get("book") or ""
            )
    manifest = (
        _capture_manifest(
            doc,
            book_id=bid,
            generator=generator,
            instructions_book=effective_instructions,
        )
        if capture_aware
        else _book_manifest(
            doc,
            book_id=bid,
            generator=generator,
            instructions_book=instructions_book,
        )
    )
    if capture_aware:
        graph_issues = _lib3_graph_issues(
            manifest,
            doc.resources,
            page_numbers=[page.page for page in doc.pages],
        )
        if graph_issues:
            first = graph_issues[0]
            raise LibError(
                f"{first.loc}: {first.msg}",
                code="invalid_lib3_graph",
                details={
                    "issues": [value.as_dict() for value in graph_issues],
                },
            )
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
                                           per_book=effective_instructions,
                                           format_version=(
                                               CAPTURE_FORMAT_VERSION
                                               if capture_aware
                                               else FORMAT_VERSION
                                           )))
            z.writestr(
                "schema.json",
                json.dumps(SCHEMA_V3 if capture_aware else SCHEMA, indent=1),
            )
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
            if capture_aware:
                for member in sorted(doc.resources):
                    z.writestr(member, doc.resources[member])
            sealed_infos = tuple(z.infolist())
        _preflight_sealed_archive(temporary, sealed_infos)
        os.replace(temporary, destination)
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass

    for page, items in sealed_pages:
        page.items = items
    if capture_aware:
        doc.format = (3, 0)
        doc.book = manifest
        doc.representations = _representations_from_book(manifest)
        doc.artifacts = _artifacts_from_book(manifest)


def write_lib(doc: LibDocument, path, *, generator: str = "library-tool/dev",
              book_id: str = "", instructions_book: str = "") -> None:
    """Seal ``doc`` while converting excessive JSON nesting to a typed error."""
    try:
        _write_lib(
            doc,
            path,
            generator=generator,
            book_id=book_id,
            instructions_book=instructions_book,
        )
    except RecursionError as exc:
        raise LibError(
            "document nesting is too deep to serialize",
            code="invalid_lib_document",
            details={"reason": "json_nesting_too_deep"},
        ) from exc


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
    if doc.format[0] == 3:
        graph_book = dict(doc.book)
        typed_page_numbers = [page.page for page in doc.pages]
        graph_book.setdefault("pages", typed_page_numbers)
        try:
            graph_book["representations"] = [
                _representation_record(value)
                for value in doc.representations
            ]
            graph_book["artifacts"] = [
                _artifact_record(value) for value in doc.artifacts
            ]
        except LibError as exc:
            add(
                "error",
                str(exc.details.get("location") or "book.json"),
                str(exc),
            )
        else:
            issues.extend(_lib3_graph_issues(
                graph_book,
                doc.resources,
                page_numbers=typed_page_numbers,
            ))

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

    declared_graph_members = (
        {
            (
                value.member
                if isinstance(value, (LibRepresentation, LibArtifact))
                else value.get("member")
            )
            for value in [*doc.representations, *doc.artifacts]
            if (
                isinstance(value, (LibRepresentation, LibArtifact))
                or isinstance(value, Mapping)
            )
        }
        if doc.format[0] == 3
        else set()
    )
    # any member outside the known shapes round-trips only through `ext`
    for name in doc.members:
        if (name in _KNOWN_MEMBERS or _PAGE_MEMBER.fullmatch(name)
                or _ASSET_MEMBER.fullmatch(name) or _TRANS_MEMBER.fullmatch(name)
                or name in declared_graph_members
                or name.endswith("/")):
            continue
        if doc.format[0] == 3:
            add("error", name, "undeclared member is not part of lib/3")
        else:
            warn(name, "member ignored: not part of the .lib layout")
    return issues


# --- self-description: INSTRUCTIONS.md + schema.json ------------------------

def _role_table() -> str:
    rows = ["| role | furniture | meaning |", "| --- | --- | --- |"]
    for role, spec in ROLE_VOCAB.items():
        rows.append(f"| `{role}` | {'yes' if spec['furniture'] else 'no'} | "
                    f"{spec['note']} |")
    return "\n".join(rows)


def _render_capture_instructions(meta: dict, per_book: str) -> str:
    title = str(meta.get("title") or "this book")
    return f"""# {title} — a Library Tool `.lib/3` book file

## What this file is

This ZIP archive is a sealed, portable projection of one Library Tool book.
It may be capture-only: conventional `pages/<N>.json` members are optional.
Read `schema.json` before editing.

- `book.json` contains bibliographic metadata plus the revisioned
  `representations[]` and `artifacts[]` graph.
- `representations/<...>` contains immutable captured/source media.
- `artifacts/<...>` contains generated metadata, OCR text, Mistral/OCR spatial
  annotations, extracted/generated images, transform recipes, or durable
  correction-review exports.
- `pages/<N>.json`, `assets/img/<name>`, and translations retain the lib/2
  page/region model when a book has Replica pages.
- `INSTRUCTIONS.md` and `schema.json` are the human and mechanical contracts.

## Capture graph

Every representation has a stable `id` and `revision`, role, media type,
archive `member`, SHA-256 checksum, raster dimensions/orientation when
applicable, and revision-pinned lineage.

Every artifact has its own stable identity and checksum; it pins the source
representation and revision. An artifact may carry normalized polygon
`selector` geometry, revision-pinned `relationships`, provenance, extension
data, and category, spatial-role, or caption assertions. The four primary
artifact classes are generated metadata, OCR text, spatial annotations
(including Mistral boxes), and raster images (captured, processed, extracted,
or generated). Transform recipes and durable attention/review state use the
same graph scaffolding.

A `raster-image` artifact may refer to its source representation's existing
`representations/<...>` member instead of duplicating captured bytes. Its
source identity/revision, media type, checksum, and dimensions must exactly
match that representation. The ZIP still contains the shared member once;
category and caption assertions remain owned by the artifact.

`manual` assertions are human overrides. They remain separate from machine or
imported assertions, and must survive reruns. `MAR` and `ILL` are UI aliases;
the stored canonical spatial roles are `marginalia` and `figure`.

## Editing rules

1. Never replace an original representation member. A crop, perspective
   correction, binary adjustment, extraction, or generated image is a new
   representation/artifact with a new member and revision-pinned lineage.
2. Preserve every `id`, `revision`, source revision, selector, relationship,
   provenance record, and human assertion. If content changes, create a new
   member/revision and update its `content_sha256`.
3. Never replace or delete a `manual` category, role, or caption assertion
   when adding machine output. A manual caption supplements retained machine
   evidence; clearing one reveals rather than deletes that evidence.
4. Never put local paths, private URLs, storage keys, credentials, resource
   grants, or other locators in the graph or `ext`. Archive `member` names are
   the only portable resource address.
5. Custom data belongs in bounded `ext` objects. Do not invent top-level
   fields. Preserve unknown declared extension data.
6. Active jobs, credentials, UI layouts, keymaps, window state, and remembered
   adjustment brightness are not portable and must not enter this archive.
7. Keep `format_version`, `book_id`, region `rid`s, graph identities, and
   provenance intact. Never renumber or rename legacy `pages/<N>.json`.
8. Keep diplomatic OCR in `text`; put modernized readings in `norm` and
   translations in `translations/<lang>.json`.

### Legacy spatial roles

{_role_table()}

## Review export policy

`book.json.review_policy.mode` is `all-durable`, `active-only`, or `none`.
Resolved review history may be exported only when the declared policy allows
it. Active job runtime state is never exported.

## Per-book instructions

{per_book.strip() or "_(none provided)_"}

## Before returning the archive

Recompute the SHA-256 digest for every new or changed declared member, validate
`book.json` against `schema.json`, and run `libformat.validate()`. A successful
round trip reports no errors and preserves originals, revision pins,
provenance, extensions, and human overrides.
"""


def render_instructions(meta: dict, per_book: str = "",
                        format_version: str = FORMAT_VERSION) -> str:
    """Generate INSTRUCTIONS.md — the LLM contract a `.lib` ships. Covers what
    the file is, the data model (with the role table rendered from the live
    vocabulary), the editing invariants (docs/lib-format.md §2.1), the per-book
    note, and a worked translate/colorize example."""
    title = str(meta.get("title") or "this book")
    parsed = parse_format({"format_version": format_version})
    if parsed and parsed[0] >= 3:
        return _render_capture_instructions(meta, per_book)
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


# lib/3 has a separate schema rather than mutating ``SCHEMA``: the legacy
# server exporter imports SCHEMA directly and must continue emitting the exact
# lib/2 contract until it can project canonical capture aggregates.
SCHEMA_V3 = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://library-tool/lib/3/schema.json",
    "title": "Library Tool capture-aware .lib/3 archive",
    "description": (
        "Shapes for capture-aware book, representation, artifact, and legacy "
        "page members."
    ),
    "x-lib-members": {
        "book.json": "#/$defs/bookV3",
        "pages/<N>.json": "#/$defs/page",
        "translations/<bcp47>.json": "#/$defs/translation",
    },
    "x-lib-resource-members": {
        "representations/<...>": (
            "binary media owned by one representation, optionally shared by "
            "exactly source-pinned raster-image artifact records, and pinned "
            "by SHA-256"
        ),
        "artifacts/<...>": (
            "artifact content declared by one artifact and pinned by SHA-256"
        ),
    },
    "$defs": {
        **SCHEMA["$defs"],
        "portableId": {
            "type": "string",
            "pattern": PORTABLE_ID_RE.pattern,
        },
        "revision": {
            "type": "string",
            "pattern": REVISION_RE.pattern,
        },
        "optionalPortableId": {
            "anyOf": [
                {"const": ""},
                {"$ref": "#/$defs/portableId"},
            ],
        },
        "optionalRevision": {
            "anyOf": [
                {"const": ""},
                {"$ref": "#/$defs/revision"},
            ],
        },
        "sha256": {
            "type": "string",
            "pattern": SHA256_RE.pattern,
        },
        "ext": {
            "type": "object",
            "description": (
                "Bounded portable extension data. Private paths, URLs, "
                "storage locators, and resource references are forbidden."
            ),
        },
        "dimensionsV3": {
            "type": "object",
            "required": ["width", "height", "orientation"],
            "properties": {
                "width": {"type": "integer", "minimum": 1},
                "height": {"type": "integer", "minimum": 1},
                "orientation": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 8,
                },
            },
            "additionalProperties": False,
        },
        "provenance": {
            "type": "object",
            "required": ["origin"],
            "properties": {
                "origin": {"$ref": "#/$defs/portableId"},
                "provider_id": {"$ref": "#/$defs/optionalPortableId"},
                "model": {"type": "string", "maxLength": 256},
                "recipe_revision": {"$ref": "#/$defs/optionalRevision"},
                "operation_id": {"$ref": "#/$defs/optionalPortableId"},
                "generated_at": {"type": "string", "maxLength": 128},
                "ext": {"$ref": "#/$defs/ext"},
            },
            "additionalProperties": False,
        },
        "representationLineage": {
            "type": "object",
            "required": [
                "representation_id",
                "representation_revision",
                "relation",
            ],
            "properties": {
                "representation_id": {"$ref": "#/$defs/portableId"},
                "representation_revision": {"$ref": "#/$defs/revision"},
                "relation": {"$ref": "#/$defs/portableId"},
            },
            "additionalProperties": False,
        },
        "representation": {
            "type": "object",
            "required": [
                "id",
                "revision",
                "role",
                "media_type",
                "member",
                "content_sha256",
                "lineage",
                "ext",
            ],
            "properties": {
                "id": {"$ref": "#/$defs/portableId"},
                "revision": {"$ref": "#/$defs/revision"},
                "role": {"$ref": "#/$defs/portableId"},
                "media_type": {
                    "type": "string",
                    "pattern": MEDIA_TYPE_RE.pattern,
                },
                "member": {
                    "type": "string",
                    "pattern": (
                        r"^representations/"
                        r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}"
                        r"(?:/[A-Za-z0-9][A-Za-z0-9._-]{0,127})*$"
                    ),
                },
                "content_sha256": {"$ref": "#/$defs/sha256"},
                "dimensions": {"$ref": "#/$defs/dimensionsV3"},
                "lineage": {
                    "type": "array",
                    "items": {"$ref": "#/$defs/representationLineage"},
                    "maxItems": 64,
                },
                "ext": {"$ref": "#/$defs/ext"},
            },
            "additionalProperties": False,
        },
        "artifactSource": {
            "type": "object",
            "required": [
                "representation_id",
                "representation_revision",
            ],
            "properties": {
                "representation_id": {"$ref": "#/$defs/portableId"},
                "representation_revision": {"$ref": "#/$defs/revision"},
                "canvas_id": {"$ref": "#/$defs/portableId"},
                "canvas_revision": {"$ref": "#/$defs/revision"},
            },
            "dependentRequired": {
                "canvas_id": ["canvas_revision"],
                "canvas_revision": ["canvas_id"],
            },
            "additionalProperties": False,
        },
        "point": {
            "type": "object",
            "required": ["x", "y"],
            "properties": {
                "x": {"type": "number", "minimum": 0, "maximum": 1},
                "y": {"type": "number", "minimum": 0, "maximum": 1},
            },
            "additionalProperties": False,
        },
        "selector": {
            "type": "object",
            "required": [
                "type",
                "coordinate_space",
                "coordinate_space_revision",
                "points",
            ],
            "properties": {
                "type": {"const": "polygon"},
                "coordinate_space": {"$ref": "#/$defs/portableId"},
                "coordinate_space_revision": {"$ref": "#/$defs/revision"},
                "points": {
                    "type": "array",
                    "items": {"$ref": "#/$defs/point"},
                    "minItems": 3,
                    "maxItems": 64,
                },
            },
            "additionalProperties": False,
        },
        "artifactRelationship": {
            "type": "object",
            "required": ["artifact_id", "artifact_revision", "relation"],
            "properties": {
                "artifact_id": {"$ref": "#/$defs/portableId"},
                "artifact_revision": {"$ref": "#/$defs/revision"},
                "relation": {"$ref": "#/$defs/portableId"},
            },
            "additionalProperties": False,
        },
        "categoryAssignment": {
            "type": "object",
            "required": ["category", "origin", "revision"],
            "properties": {
                "category": {"enum": sorted(IMAGE_CATEGORIES)},
                "origin": {"enum": sorted(ASSIGNMENT_ORIGINS)},
                "revision": {"$ref": "#/$defs/revision"},
                "inherited_from_artifact_id": {
                    "$ref": "#/$defs/portableId",
                },
                "confidence": {
                    "type": ["number", "null"],
                    "minimum": 0,
                    "maximum": 1,
                },
                "provenance": {"$ref": "#/$defs/provenance"},
                "ext": {"$ref": "#/$defs/ext"},
            },
            "additionalProperties": False,
        },
        "captionAssertion": {
            "type": "object",
            "required": ["text", "origin", "revision"],
            "properties": {
                "text": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 16_384,
                },
                "origin": {"enum": sorted(CAPTION_ORIGINS)},
                "revision": {"$ref": "#/$defs/revision"},
                "language": {
                    "type": "string",
                    "pattern": LANGUAGE_RE.pattern,
                },
                "source_annotation_id": {"$ref": "#/$defs/portableId"},
                "confidence": {
                    "type": ["number", "null"],
                    "minimum": 0,
                    "maximum": 1,
                },
                "provenance": {"$ref": "#/$defs/provenance"},
                "ext": {"$ref": "#/$defs/ext"},
            },
            "additionalProperties": False,
        },
        "roleAssignment": {
            "type": "object",
            "required": ["role", "origin", "revision"],
            "properties": {
                "role": {"type": "string", "pattern": ROLE_RE.pattern},
                "origin": {"enum": sorted(ROLE_ASSIGNMENT_ORIGINS)},
                "revision": {"$ref": "#/$defs/revision"},
                "confidence": {
                    "type": ["number", "null"],
                    "minimum": 0,
                    "maximum": 1,
                },
                "provenance": {"$ref": "#/$defs/provenance"},
                "ext": {"$ref": "#/$defs/ext"},
            },
            "additionalProperties": False,
        },
        "artifact": {
            "type": "object",
            "required": [
                "id",
                "revision",
                "kind",
                "media_type",
                "member",
                "content_sha256",
                "source",
                "provenance",
                "category_assignments",
                "caption_assertions",
                "role_assignments",
                "relationships",
                "ext",
            ],
            "properties": {
                "id": {"$ref": "#/$defs/portableId"},
                "revision": {"$ref": "#/$defs/revision"},
                "kind": {"$ref": "#/$defs/portableId"},
                "media_type": {
                    "type": "string",
                    "pattern": MEDIA_TYPE_RE.pattern,
                },
                "member": {
                    "type": "string",
                    "description": (
                        "A unique artifacts/ member, or the exact "
                        "representations/ member owned by the source-pinned "
                        "representation when this is a byte-identical "
                        "raster-image assertion projection."
                    ),
                    "pattern": (
                        r"^(?:artifacts|representations)/"
                        r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}"
                        r"(?:/[A-Za-z0-9][A-Za-z0-9._-]{0,127})*$"
                    ),
                },
                "content_sha256": {"$ref": "#/$defs/sha256"},
                "source": {"$ref": "#/$defs/artifactSource"},
                "dimensions": {"$ref": "#/$defs/dimensionsV3"},
                "provenance": {"$ref": "#/$defs/provenance"},
                "category_assignments": {
                    "type": "array",
                    "items": {"$ref": "#/$defs/categoryAssignment"},
                    "maxItems": 3,
                },
                "caption_assertions": {
                    "type": "array",
                    "items": {"$ref": "#/$defs/captionAssertion"},
                    "maxItems": 32,
                },
                "role_assignments": {
                    "type": "array",
                    "items": {"$ref": "#/$defs/roleAssignment"},
                    "maxItems": 3,
                },
                "selector": {"$ref": "#/$defs/selector"},
                "relationships": {
                    "type": "array",
                    "items": {"$ref": "#/$defs/artifactRelationship"},
                    "maxItems": 64,
                },
                "ext": {"$ref": "#/$defs/ext"},
            },
            "additionalProperties": False,
        },
        "reviewPolicy": {
            "type": "object",
            "required": ["mode"],
            "properties": {
                "mode": {"enum": sorted(REVIEW_EXPORT_MODES)},
            },
            "additionalProperties": False,
        },
        "bookV3": {
            "type": "object",
            "required": [
                "format_version",
                "book_id",
                "pages",
                "representations",
                "artifacts",
                "review_policy",
            ],
            "properties": {
                **SCHEMA["$defs"]["book"]["properties"],
                "format_version": {"const": CAPTURE_FORMAT_VERSION},
                "book_id": {
                    "type": "string",
                    "pattern": BOOK_ID_RE.pattern,
                },
                "pages": {
                    "type": "array",
                    "items": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 99999,
                    },
                    "maxItems": MAX_PAGES,
                    "uniqueItems": True,
                },
                "representations": {
                    "type": "array",
                    "items": {"$ref": "#/$defs/representation"},
                    "maxItems": MAX_REPRESENTATIONS,
                },
                "artifacts": {
                    "type": "array",
                    "items": {"$ref": "#/$defs/artifact"},
                    "maxItems": MAX_ARTIFACTS,
                },
                "review_policy": {"$ref": "#/$defs/reviewPolicy"},
            },
            "anyOf": [
                {
                    "properties": {
                        "pages": {"type": "array", "minItems": 1},
                    },
                },
                {
                    "properties": {
                        "representations": {
                            "type": "array",
                            "minItems": 1,
                        },
                    },
                },
            ],
            "additionalProperties": False,
        },
    },
}

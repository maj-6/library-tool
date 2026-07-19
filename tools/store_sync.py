"""Two-way cloud sync for the desktop's working stores — the files that left git.

Commit 87a9bf2 removed whl_builds.json, downloads/ia/catalog.json,
whl_corrections.json and output/entries/ from version control on the promise
that they would sync through the cloud instead; this module is that channel.

The three JSON stores sync record-by-record against their Supabase tables
(builds / ia_catalog / corrections — see docs/cloud/migrations/), merged by
last-write-wins on updated_at. A local SHADOW LEDGER (output/cloud_shadow.json,
what the cloud looked like after the last sync) is what tells "deleted here"
apart from "added there", so no UI endpoint needs delete hooks. Deletes
propagate as tombstones — the cloud row keeps its data, only `deleted` flips —
and are arbitrated by timestamp like any other write, with EDITS BEATING
DELETES on conflict.

The safety rules follow the checked-books precedent (never let an emptier side
clobber a fuller one):
  - before a sync pass changes a local store file, the current file is
    snapshotted into output/backups/;
  - a pass that finds most of the previously-synced records missing locally
    (a wiped or replaced DATA_ROOT) refuses to tombstone anything and pulls
    the records back instead;
  - nothing is ever hard-deleted in the cloud, and entry FILES are never
    deleted on either side.

The entry folders (output/entries/ — OCR text, layout, previews) are file
blobs, so like the corpus they mirror to the R2 bucket, under the entries/
prefix. Unlike the corpus these files are edited in place, so comparison is
content MD5 against the object's ETag, and when both sides differ the file's
mtime against the object's upload time picks the direction.

CLI (dry-run by default, like corpus_sync):
    python3 tools/store_sync.py status        what a sync would do

Mutation runs through Library Tool's composed host, which supplies the item
lifecycle guard shared with deletion and restore.  The standalone CLI remains
available for read-only planning but refuses ``sync --run`` because it cannot
safely acquire that host-owned policy boundary.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import sys
import threading
from collections.abc import Callable
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
from pathlib import Path
from typing import ContextManager

sys.path.insert(0, str(Path(__file__).resolve().parent))
import libcommon as lib  # noqa: E402
import r2_store as r2  # noqa: E402
import supabase_sync as sbase  # noqa: E402

SHADOW_PATH = lib.OUTPUT_DIR / "cloud_shadow.json"
BACKUP_KEEP = 20
_shadow_lock = threading.Lock()

# In-process the server passes its own per-store locks into sync_stores, so
# sync and route writes serialize on the same lock. This fallback only covers
# the standalone CLI, where this module is the sole (single-threaded) writer.
_corrections_lock = threading.Lock()


# --- timestamps and hashing --------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_ts(ts) -> datetime:
    """Lenient ISO-8601 → aware datetime; anything unparseable sorts oldest.
    PostgREST and our own stamps format the same instant differently
    (fractional seconds, Z vs +00:00), so equality checks must parse."""
    s = str(ts or "").strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return datetime.fromtimestamp(0, timezone.utc)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _hash(record: dict) -> str:
    """Content identity of one record, independent of key order."""
    blob = json.dumps(record, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"))
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


# --- store specs -------------------------------------------------------------------
# decompose: local file document -> ({key: record}, ids_assigned)
# recompose: (old document, final records) -> new document
# scrub:     record -> what gets hashed and pushed (machine-local fields out)
# adopt:     (old local record | None, pulled record) -> new local record
# local_ts:  record -> the record's own edit stamp, "" when it has none

def _builds_decompose(doc):
    return ({str(k): v for k, v in (doc or {}).items() if isinstance(v, dict)},
            False)


def _ia_scrub(rec: dict) -> dict:
    # `preview` is a derived per-machine cache path (regenerated on demand);
    # `saved_as` is written with the OS separator, and a posix relpath is the
    # portable spelling every consumer already resolves.
    out = {k: v for k, v in rec.items() if k != "preview"}
    if out.get("saved_as"):
        out["saved_as"] = str(out["saved_as"]).replace("\\", "/")
    return out


def _ia_adopt(old, incoming):
    out = dict(incoming)
    if old and old.get("preview"):
        out["preview"] = old["preview"]
    return out


def _corr_decompose(doc):
    doc = doc if isinstance(doc, dict) else {}
    records: dict[str, dict] = {}
    for sidx, fields in (doc.get("edits") or {}).items():
        if isinstance(fields, dict) and fields:
            records[f"edit:{sidx}"] = dict(fields)
    assigned = False
    added = [r for r in (doc.get("added") or []) if isinstance(r, dict)]
    taken = {str(r.get("id")) for r in added if r.get("id")}
    for row in added:
        if not row.get("id"):        # rows predating sync get a stable identity
            row["id"] = lib.gen_id(taken)
            assigned = True
        records[f"add:{row['id']}"] = dict(row)
    return records, assigned


def _corr_recompose(old_doc, records):
    old_doc = old_doc if isinstance(old_doc, dict) else {}
    edits = {k.split(":", 1)[1]: dict(v) for k, v in records.items()
             if k.startswith("edit:")}
    adds = {}
    for k, v in records.items():
        if k.startswith("add:"):
            rid = k.split(":", 1)[1]
            adds[rid] = dict(v, id=rid)   # the key names the row's identity
    # keep the local ordering (the UI addresses added rows by position);
    # rows new from the cloud go on the end, deterministically
    added, seen = [], set()
    for row in (old_doc.get("added") or []):
        rid = str((row or {}).get("id") or "")
        if rid in adds and rid not in seen:
            added.append(adds[rid])
            seen.add(rid)
    for rid in sorted(set(adds) - seen):
        added.append(adds[rid])
    return {"added": added, "edits": edits}


STORES: dict[str, dict] = {
    "builds": {
        "table": "builds", "pk": "id",
        "path": lambda: lib.OUTPUT_DIR / "whl_builds.json",
        "default": {},
        "decompose": _builds_decompose,
        "recompose": lambda old, records: dict(records),
        "scrub": lambda rec: rec,
        "adopt": lambda old, incoming: incoming,
        "local_ts": lambda rec: str(rec.get("updated_at")
                                    or rec.get("created_at") or ""),
    },
    "ia_catalog": {
        "table": "ia_catalog", "pk": "identifier",
        "path": lambda: lib.IA_CATALOG_PATH,
        "default": {},
        "decompose": _builds_decompose,      # same shape: {key: record}
        "recompose": lambda old, records: dict(records),
        "scrub": _ia_scrub,
        "adopt": _ia_adopt,
        # downloaded_at never changes on edit, so an edited record falls back
        # to sync-time stamping in _effective_ts
        "local_ts": lambda rec: str(rec.get("downloaded_at") or ""),
    },
    "corrections": {
        "table": "corrections", "pk": "key",
        "path": lambda: lib.OUTPUT_DIR / "whl_corrections.json",
        "default": {"added": [], "edits": {}},
        "decompose": _corr_decompose,
        "recompose": _corr_recompose,
        "scrub": lambda rec: rec,
        "adopt": lambda old, incoming: incoming,
        "local_ts": lambda rec: "",
    },
    # the category taxonomy: {"version": 1, "nodes": {id: node}} — one row
    # per node, so a rename here and a re-parent there merge cleanly
    "taxonomy": {
        "table": "taxonomy", "pk": "id",
        "path": lambda: lib.CATEGORIES_PATH,
        "default": {"version": 1, "nodes": {}},
        "decompose": lambda doc: _builds_decompose((doc or {}).get("nodes")),
        "recompose": lambda old, records: {"version": 1, "nodes": dict(records)},
        "scrub": lambda rec: rec,
        "adopt": lambda old, incoming: incoming,
        "local_ts": lambda rec: str(rec.get("updated_at")
                                    or rec.get("created_at") or ""),
    },
}


# --- the merge ---------------------------------------------------------------------

def _effective_ts(rec: dict, spec: dict, shadow_entry: dict | None, now: str) -> str:
    """When a record changed, the timestamp that represents the change: its
    own edit stamp when the store keeps one and it moved, else sync time."""
    ts = spec["local_ts"](rec)
    shadow_ts = (shadow_entry or {}).get("ts") or ""
    if ts and _parse_ts(ts) != _parse_ts(shadow_ts):
        return ts
    return now


def merge(local: dict[str, dict], cloud: dict[str, dict],
          shadow: dict[str, dict], now: str, spec: dict) -> dict:
    """Pure per-key three-way merge. `local` holds SCRUBBED records; `cloud`
    holds {"data", "updated_at", "deleted"} rows; `shadow` holds
    {"h", "ts", "dead"} entries from the last sync.

    Returns a plan: push/tombstone (rows for the cloud), pull (adopt locally),
    delete_local [(key, cloud_ts)], refresh (shadow-only corrections),
    shadow_drop, in_sync, guard."""
    plan = {"push": [], "tombstone": [], "pull": {}, "delete_local": [],
            "refresh": {}, "shadow_drop": [], "in_sync": 0, "guard": ""}

    # The wipe guard: when most of what we know we synced is suddenly gone
    # locally, the file was lost, not edited — restore it, never propagate it.
    shadow_live = {k for k, e in shadow.items() if not (e or {}).get("dead")}
    missing = {k for k in shadow_live if k not in local}
    guarded = (len(shadow_live) >= 3
               and len(missing) >= max(3, math.ceil(0.8 * len(shadow_live))))
    if guarded:
        plan["guard"] = (
            f"{len(missing)} of {len(shadow_live)} previously-synced records "
            f"are missing locally — treating it as a wipe: nothing is "
            f"tombstoned, the records are pulled back instead")

    for k in sorted(set(local) | set(cloud) | set(shadow)):
        L, C, S = local.get(k), cloud.get(k), shadow.get(k)

        if L is None and C is None:                  # only the shadow remembers it
            plan["shadow_drop"].append(k)
            continue

        if L is not None:
            lh = _hash(L)
            l_changed = S is None or S.get("h") != lh
            lts = (_effective_ts(L, spec, S, now) if l_changed
                   else (S or {}).get("ts") or now)

        if C is None:                                # cloud has no row at all
            plan["push"].append({"key": k, "data": L, "ts": lts})
            continue

        cts = str(C.get("updated_at") or "")
        c_changed = S is None or _parse_ts(cts) != _parse_ts(S.get("ts"))

        if L is None:                                # locally absent
            if C.get("deleted"):
                plan["shadow_drop"].append(k)        # gone on both sides
            elif S is None or S.get("dead"):
                plan["pull"][k] = C                  # new from the cloud
            elif guarded:
                plan["pull"][k] = C                  # wipe guard: restore
            elif c_changed:
                plan["pull"][k] = C                  # delete vs edit: edit wins
            else:
                plan["tombstone"].append({"key": k, "data": C.get("data") or {},
                                          "ts": now})
            continue

        if C.get("deleted"):                         # tombstone vs local record
            if l_changed and _parse_ts(lts) > _parse_ts(cts):
                plan["push"].append({"key": k, "data": L, "ts": lts})
            else:
                plan["delete_local"].append((k, cts))
            continue

        if _hash(C.get("data") or {}) == lh:         # identical content
            plan["in_sync"] += 1
            if S is None or S.get("h") != lh or _parse_ts(S.get("ts")) != _parse_ts(cts):
                plan["refresh"][k] = {"h": lh, "ts": cts, "dead": False}
            continue

        if l_changed and not c_changed:
            plan["push"].append({"key": k, "data": L, "ts": lts})
        elif c_changed and not l_changed:
            plan["pull"][k] = C
        else:
            # both moved since the last sync (or the ledger is stale):
            # last write wins, and the local file wins ties
            if _parse_ts(lts) >= _parse_ts(cts):
                plan["push"].append({"key": k, "data": L, "ts": lts})
            else:
                plan["pull"][k] = C
    return plan


# --- shadow + backups --------------------------------------------------------------

def _load_shadow() -> dict:
    doc = lib.load_json(SHADOW_PATH, {})
    return doc if isinstance(doc, dict) else {}


def _update_shadow(store: str, updates: dict[str, dict | None]) -> None:
    """Merge one store's shadow entries (None deletes) into the shared file.
    Reload-modify-save under a lock: two stores must not lose each other's
    bookkeeping."""
    with _shadow_lock:
        doc = _load_shadow()
        section = doc.setdefault(store, {})
        for k, entry in updates.items():
            if entry is None:
                section.pop(k, None)
            else:
                section[k] = entry
        lib.save_json(SHADOW_PATH, doc)


def _backup(path: Path) -> None:
    """Snapshot a store file before sync rewrites it — every overwrite and
    delete a merge applies stays reversible (the client_state precedent)."""
    if not path.exists():
        return
    bdir = lib.OUTPUT_DIR / "backups"
    bdir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%f")
    shutil.copy2(path, bdir / f"{path.name}.presync.{ts}.json")
    for old in sorted(bdir.glob(f"{path.name}.presync.*.json"))[:-BACKUP_KEEP]:
        try:
            old.unlink()
        except OSError:
            pass


# --- executing one store -----------------------------------------------------------

_ITEM_CATALOGUE_STORE = "builds"
ItemPolicy = Callable[[str], bool]
ItemPolicyGuard = Callable[[], ContextManager[ItemPolicy]]


def _checked_item_policy(
        allow_item: ItemPolicy | None,
) -> ItemPolicy | None:
    if allow_item is not None and not callable(allow_item):
        raise TypeError("allow_item must be callable")
    return allow_item


def _checked_item_policy_guard(
        allow_item: ItemPolicy | None,
        item_policy_guard: ItemPolicyGuard | None,
) -> tuple[ItemPolicy | None, ItemPolicyGuard | None]:
    allow_item = _checked_item_policy(allow_item)
    if item_policy_guard is not None and not callable(item_policy_guard):
        raise TypeError("item_policy_guard must be callable")
    if allow_item is not None and item_policy_guard is not None:
        raise TypeError(
            "allow_item and item_policy_guard are mutually exclusive"
        )
    return allow_item, item_policy_guard


@contextmanager
def _item_policy_scope(
        allow_item: ItemPolicy | None,
        item_policy_guard: ItemPolicyGuard | None,
):
    if item_policy_guard is None:
        yield allow_item
        return
    context = item_policy_guard()
    if not hasattr(context, "__enter__") or not hasattr(context, "__exit__"):
        raise TypeError("item_policy_guard must return a context manager")
    with context as policy:
        if not callable(policy):
            raise TypeError(
                "item_policy_guard must yield a callable item policy"
            )
        yield policy


def _policy_allows_item(
        policy: ItemPolicy, item_id: str,
) -> bool:
    """Evaluate a lifecycle policy without weakening its exact identity.

    Store keys are already normalized to strings by the existing transport and
    decomposition paths.  Deliberately do not strip or case-fold here: engine
    item identities are exact, and silently accepting an alias could bypass a
    durable tombstone.
    """
    decision = policy(item_id)
    if not isinstance(decision, bool):
        raise TypeError("allow_item must return a boolean")
    return decision


def sync_store(
        cfg: dict,
        name: str,
        lock=None,
        dry: bool = False,
        allow_item: ItemPolicy | None = None,
        item_policy_guard: ItemPolicyGuard | None = None,
) -> dict:
    """One store's full pass: fetch, merge, apply locally, push.

    Local reads and writes happen under ``lock`` (the server passes its own);
    network calls happen outside it.  For the item catalogue (``builds``), an
    optional ``allow_item`` policy filters exact local, cloud, and shadow keys
    before the merge and is evaluated again before either local or cloud
    publication.  Invalid policy values fail closed before the affected write.

    A plain callback narrows, but cannot eliminate, the final TOCTOU interval.
    ``item_policy_guard`` closes it by yielding a lock-safe policy while the
    caller's lifecycle/workspace gate stays held across each local or cloud
    publication phase. It is mutually exclusive with ``allow_item``.
    """
    allow_item, item_policy_guard = _checked_item_policy_guard(
        allow_item, item_policy_guard
    )
    if (
        name == _ITEM_CATALOGUE_STORE
        and item_policy_guard is not None
        and lock is not None
    ):
        raise ValueError(
            "the guarded item policy owns the catalogue lock; "
            "a second builds lock is not allowed"
        )
    item_policy = allow_item if name == _ITEM_CATALOGUE_STORE else None
    policy_guard = (
        item_policy_guard if name == _ITEM_CATALOGUE_STORE else None
    )

    spec = STORES[name]
    rows = sbase.list_store_rows(cfg, spec["table"], spec["pk"])
    raw_cloud: dict[str, dict] = {}
    for r in rows:
        k = str(r.get(spec["pk"]) or "")
        if k:
            raw_cloud[k] = {
                "data": (
                    r.get("data") if isinstance(r.get("data"), dict) else {}
                ),
                "updated_at": str(r.get("updated_at") or ""),
                "deleted": bool(r.get("deleted")),
            }

    path = spec["path"]()
    with _item_policy_scope(item_policy, policy_guard) as scoped_policy:
        planning_allowed: dict[str, bool] = {}

        def allowed_for_plan(item_id: str) -> bool:
            assert scoped_policy is not None
            if item_id not in planning_allowed:
                planning_allowed[item_id] = _policy_allows_item(
                    scoped_policy, item_id,
                )
            return planning_allowed[item_id]

        cloud = {
            k: row for k, row in raw_cloud.items()
            if scoped_policy is None or allowed_for_plan(k)
        }
        with (lock or nullcontext()):
            doc = lib.load_json(path, spec["default"])
            records, ids_assigned = spec["decompose"](doc)
            scrubbed = {
                k: spec["scrub"](rec) for k, rec in records.items()
            }
            shadow = _load_shadow().get(name, {})
            if scoped_policy is not None:
                scrubbed = {
                    k: rec for k, rec in scrubbed.items()
                    if allowed_for_plan(k)
                }
                shadow = {
                    k: entry for k, entry in shadow.items()
                    if allowed_for_plan(k)
                }
            plan = merge(scrubbed, cloud, shadow, _now(), spec)

            if dry:
                return {
                    "pushed": len(plan["push"]),
                    "pulled": len(plan["pull"]),
                    "tombstoned": len(plan["tombstone"]),
                    "deleted": len(plan["delete_local"]),
                    "in_sync": plan["in_sync"],
                    "guard": plan["guard"],
                    "dry": True,
                }

            if scoped_policy is not None:
                plan["pull"] = {
                    k: row for k, row in plan["pull"].items()
                    if _policy_allows_item(scoped_policy, k)
                }
                plan["delete_local"] = [
                    (k, cts) for k, cts in plan["delete_local"]
                    if _policy_allows_item(scoped_policy, k)
                ]
                plan["refresh"] = {
                    k: entry for k, entry in plan["refresh"].items()
                    if _policy_allows_item(scoped_policy, k)
                }
                plan["shadow_drop"] = [
                    k for k in plan["shadow_drop"]
                    if _policy_allows_item(scoped_policy, k)
                ]

            updates: dict[str, dict | None] = {}
            if plan["pull"] or plan["delete_local"] or ids_assigned:
                for k, row in plan["pull"].items():
                    incoming = row.get("data") or {}
                    records[k] = spec["adopt"](records.get(k), incoming)
                    updates[k] = {
                        "h": _hash(incoming),
                        "ts": row.get("updated_at") or "",
                        "dead": False,
                    }
                for k, cts in plan["delete_local"]:
                    records.pop(k, None)
                    updates[k] = {"h": None, "ts": cts, "dead": True}
                if plan["pull"] or plan["delete_local"]:
                    _backup(path)
                lib.save_json(path, spec["recompose"](doc, records))
            for k, entry in plan["refresh"].items():
                updates[k] = entry
            for k in plan["shadow_drop"]:
                updates[k] = None
            if updates:
                _update_shadow(name, updates)

    with _item_policy_scope(item_policy, policy_guard) as scoped_policy:
        if scoped_policy is not None:
            plan["push"] = [
                value for value in plan["push"]
                if _policy_allows_item(scoped_policy, value["key"])
            ]
            plan["tombstone"] = [
                value for value in plan["tombstone"]
                if _policy_allows_item(scoped_policy, value["key"])
            ]

        outgoing = (
            [
                {
                    spec["pk"]: value["key"],
                    "data": value["data"],
                    "updated_at": value["ts"],
                    "deleted": False,
                }
                for value in plan["push"]
            ]
            + [
                {
                    spec["pk"]: value["key"],
                    "data": value["data"],
                    "updated_at": value["ts"],
                    "deleted": True,
                }
                for value in plan["tombstone"]
            ]
        )
        if outgoing:
            sbase.upsert_store_rows(
                cfg, spec["table"], spec["pk"], outgoing
            )
            _update_shadow(name, {
                **{
                    value["key"]: {
                        "h": _hash(value["data"]),
                        "ts": value["ts"],
                        "dead": False,
                    }
                    for value in plan["push"]
                },
                **{
                    value["key"]: {
                        "h": None,
                        "ts": value["ts"],
                        "dead": True,
                    }
                    for value in plan["tombstone"]
                },
            })

    return {"pushed": len(plan["push"]), "pulled": len(plan["pull"]),
            "tombstoned": len(plan["tombstone"]),
            "deleted": len(plan["delete_local"]),
            "in_sync": plan["in_sync"], "guard": plan["guard"]}


def sync_stores(
        cfg: dict,
        locks: dict | None = None,
        dry: bool = False,
        allow_item: ItemPolicy | None = None,
        item_policy_guard: ItemPolicyGuard | None = None,
) -> dict:
    """Sync every JSON store; one store's failure never stops the others.

    The item policy options apply only to the ``builds`` catalogue because IA
    source, correction, and taxonomy keys are not item identities. Use the
    guarded form for the atomic lifecycle guarantee described by
    :func:`sync_store`.
    """
    allow_item, item_policy_guard = _checked_item_policy_guard(
        allow_item, item_policy_guard
    )
    locks = dict(locks or {})
    if item_policy_guard is not None and locks.get("builds") is not None:
        raise ValueError(
            "the guarded item policy owns the catalogue lock; "
            "a second builds lock is not allowed"
        )
    locks.setdefault("corrections", _corrections_lock)
    out = {}
    for name in STORES:
        try:
            out[name] = sync_store(
                cfg,
                name,
                lock=locks.get(name),
                dry=dry,
                allow_item=(allow_item
                            if name == _ITEM_CATALOGUE_STORE else None),
                item_policy_guard=(
                    item_policy_guard
                    if name == _ITEM_CATALOGUE_STORE else None
                ),
            )
        except Exception as exc:
            out[name] = {"error": f"{type(exc).__name__}: {exc}"}
    return out


# --- entry files (output/entries/ <-> R2 entries/) ----------------------------------

ENTRIES_PREFIX = "entries/"

_CONTENT_TYPES = {
    ".txt": "text/plain", ".json": "application/json",
    ".pdf": "application/pdf", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".png": "image/png", ".webp": "image/webp",
}


def entries_dir() -> Path:
    return lib.OUTPUT_DIR / "entries"


def content_type_for(rel: str) -> str:
    return _CONTENT_TYPES.get(Path(rel).suffix.lower(), "application/octet-stream")


def _safe_rel(rel: str) -> bool:
    """A bucket key must resolve inside the entries dir when pulled."""
    if "\\" in rel or ":" in rel:
        return False
    return all(p not in ("", ".", "..") for p in rel.split("/"))


def _md5(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def local_entry_files(base: Path | None = None) -> dict[str, dict]:
    """Every entry file on disk, {rel posix: {size, mtime}}. In-flight
    temporaries (atomic-write .tmp*, download .part) are not entry data."""
    base = base or entries_dir()
    out: dict[str, dict] = {}
    if base.is_dir():
        for p in sorted(base.rglob("*")):
            if p.is_file() and ".tmp" not in p.name and not p.name.endswith(".part"):
                st = p.stat()
                out[p.relative_to(base).as_posix()] = {"size": st.st_size,
                                                       "mtime": st.st_mtime}
    return out


def entries_plan(local: dict[str, dict], remote: dict[str, dict],
                 md5s: dict[str, str]) -> dict:
    """Pure: push what only exists here, pull what only exists there, and for
    files on both sides let the MD5-vs-ETag verdict decide; when content
    really differs, the newer side (file mtime vs object upload time) wins.
    Nothing is ever deleted."""
    push, pull, same = [], [], []
    for rel in sorted(set(local) | set(remote)):
        loc, rem = local.get(rel), remote.get(rel)
        if rem is None:
            push.append(rel)
            continue
        if loc is None:
            pull.append(rel)
            continue
        etag, h = str(rem.get("etag") or ""), md5s.get(rel, "")
        if h and etag and "-" not in etag:           # single-PUT etag == md5
            if h == etag:
                same.append(rel)
                continue
        elif loc["size"] == rem["size"]:             # multipart: size is all we have
            same.append(rel)
            continue
        if loc["mtime"] >= _parse_ts(rem.get("modified")).timestamp():
            push.append(rel)                         # ties: the local file wins
        else:
            pull.append(rel)
    return {"push": push, "pull": pull, "same": same}


def _entry_item_id(rel: str) -> str:
    """Return the exact item identity encoded by an entry-relative path."""
    return str(rel).split("/", 1)[0]


def sync_entry_files(
        r2cfg: dict,
        dry: bool = False,
        allow_item: ItemPolicy | None = None,
        item_policy_guard: ItemPolicyGuard | None = None,
) -> dict:
    """Mirror output/entries/ against the bucket's entries/ prefix.

    ``allow_item`` is an optional lifecycle policy over the exact first path
    component (the item id). ``item_policy_guard`` is its atomic alternative:
    it yields a policy while the lifecycle isolation scope is held. Disallowed
    items are absent from planning, and the guarded policy stays held across
    each remote or local transfer so deletion cannot enter after the check.

    These seams are injected instead of reading a tombstone store here: the
    lifecycle adapter owns durable state and lock ordering.
    """
    base = entries_dir()
    allow_item, item_policy_guard = _checked_item_policy_guard(
        allow_item, item_policy_guard
    )
    report_suppressed = (
        allow_item is not None or item_policy_guard is not None
    )
    suppressed: set[str] = set()
    planning_allowed: dict[str, bool] = {}

    local_inventory = local_entry_files(base)
    remote_inventory = {}
    for key, meta in r2.list_objects_meta(
        r2cfg, prefix=ENTRIES_PREFIX
    ).items():
        rel = key[len(ENTRIES_PREFIX):]
        if not rel or not _safe_rel(rel):
            continue
        remote_inventory[rel] = meta

    with _item_policy_scope(
        allow_item, item_policy_guard
    ) as planning_policy:
        def allowed_for_plan(rel: str) -> bool:
            if planning_policy is None:
                return True
            item_id = _entry_item_id(rel)
            if item_id not in planning_allowed:
                planning_allowed[item_id] = _policy_allows_item(
                    planning_policy, item_id
                )
            return planning_allowed[item_id]

        local = {}
        for rel, meta in local_inventory.items():
            if allowed_for_plan(rel):
                local[rel] = meta
            else:
                suppressed.add(rel)
        remote = {}
        for rel, meta in remote_inventory.items():
            if allowed_for_plan(rel):
                remote[rel] = meta
            else:
                suppressed.add(rel)
    md5s = {rel: _md5(base / rel) for rel in set(local) & set(remote)}
    plan = entries_plan(local, remote, md5s)
    if dry:
        result = {"pushed": len(plan["push"]), "pulled": len(plan["pull"]),
                  "in_sync": len(plan["same"]), "dry": True}
        if report_suppressed:
            result["suppressed"] = len(suppressed)
        return result

    pushed = 0
    for rel in plan["push"]:
        with _item_policy_scope(
            allow_item, item_policy_guard
        ) as transfer_policy:
            if transfer_policy is not None and not _policy_allows_item(
                transfer_policy, _entry_item_id(rel)
            ):
                suppressed.add(rel)
                continue
            r2.put_file(
                r2cfg,
                ENTRIES_PREFIX + rel,
                base / rel,
                content_type=content_type_for(rel),
            )
            pushed += 1

    pulled = 0
    for rel in plan["pull"]:
        # Check before even inspecting or creating a destination/backup path.
        # A lifecycle policy change must leave no local publication footprint.
        with _item_policy_scope(
            allow_item, item_policy_guard
        ) as transfer_policy:
            if transfer_policy is not None and not _policy_allows_item(
                transfer_policy, _entry_item_id(rel)
            ):
                suppressed.add(rel)
                continue
            dest = base / rel
            if dest.exists():            # overwriting OCR work: keep one copy
                bak = lib.OUTPUT_DIR / "backups" / "entries" / rel
                bak.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(dest, bak)
            r2.get_file(r2cfg, ENTRIES_PREFIX + rel, dest)
            pulled += 1
    result = {"pushed": pushed, "pulled": pulled,
              "in_sync": len(plan["same"])}
    if report_suppressed:
        result["suppressed"] = len(suppressed)
    return result


# --- CLI ---------------------------------------------------------------------------

def _cli_cfg() -> dict:
    import cloud_defaults
    url = os.environ.get("SUPABASE_URL", "")
    key = os.environ.get("SUPABASE_KEY", "")
    if not (url and key):
        s = lib.load_json(lib.CLIENT_STATE_PATH, {}).get("settings", {})
        url = url or str(s.get("supabaseUrl") or "")
        key = key or str(s.get("supabaseKey") or "")
    url = url or cloud_defaults.SUPABASE_URL   # the key is a secret; the URL isn't
    if not key:
        raise SystemExit("No Supabase service key. Set it in Settings > Sync, or "
                         "export SUPABASE_URL and SUPABASE_KEY.")
    return {"url": url.rstrip("/"), "key": key}


def _cli_r2cfg() -> dict:
    s = lib.load_json(lib.CLIENT_STATE_PATH, {}).get("settings", {})
    return {"account": str(s.get("r2Account") or "").strip(),
            "bucket": str(s.get("r2Bucket") or "").strip(),
            "key_id": str(s.get("r2KeyId") or "").strip(),
            "secret": str(s.get("r2Secret") or "").strip(),
            "public_base": str(s.get("r2PublicBase") or "").strip()}


def _print_result(results: dict, entries: dict | None) -> None:
    for name, res in results.items():
        if res.get("error"):
            print(f"{name:<12} ERROR  {res['error']}")
            continue
        line = (f"{name:<12} {res['pushed']} push, {res['pulled']} pull, "
                f"{res['tombstoned']} tombstone, {res['deleted']} delete, "
                f"{res['in_sync']} in sync")
        print(line)
        if res.get("guard"):
            print(f"{'':<12} ! {res['guard']}")
    if entries is None:
        print(f"{'entries':<12} skipped (R2 not configured)")
    elif entries.get("error"):
        print(f"{'entries':<12} ERROR  {entries['error']}")
    else:
        print(f"{'entries':<12} {entries['pushed']} push, {entries['pulled']} pull, "
              f"{entries['in_sync']} in sync")


def _run(dry: bool) -> None:
    if not dry:
        raise SystemExit(
            "Mutating standalone sync is disabled because it cannot acquire "
            "Library Tool's item-lifecycle guard. Run Sync from Library Tool; "
            "the standalone `status` command remains available."
        )
    cfg = _cli_cfg()
    results = sync_stores(cfg, dry=dry)
    r2cfg = _cli_r2cfg()
    entries = None
    if r2.configured(r2cfg):
        try:
            entries = sync_entry_files(r2cfg, dry=dry)
        except Exception as exc:
            entries = {"error": f"{type(exc).__name__}: {exc}"}
    _print_result(results, entries)
    if dry:
        print("\ndry run — apply changes from Library Tool's Sync control")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(
        description="Two-way cloud sync for builds / IA catalog / corrections "
                    "/ entry files")
    sub = ap.add_subparsers(required=True)
    s = sub.add_parser("status", help="what a sync would do")
    s.set_defaults(fn=lambda a: _run(dry=True))
    c = sub.add_parser("sync", help="merge with the cloud")
    c.add_argument("--run", action="store_true",
                   help="request mutation (requires the Library Tool host)")
    c.set_defaults(fn=lambda a: _run(dry=not a.run))
    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()

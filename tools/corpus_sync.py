"""Sync the private book corpus with R2 — the corpus lives outside git.

The capture photography (photo/) and the per-book scan images
(books/<id>/*.jpg) are personal binaries that used to be version-controlled:
~273 MB in every clone, useless to diff, and unpublishable. They are now
gitignored; this tool is their sync/backup channel instead, mirroring them
under the corpus/ prefix of the same R2 bucket the publish flow uses. The
OCR transcripts (books/<id>/*.txt) stay in git — small, diffable text.

Comparison is by name + byte size, which is enough for camera output that is
never edited in place; a re-shot photo gets a new filename. Like
cloud_setup.py, mutating commands are dry-run by default: `status` shows the
plan, `push --run` / `pull --run` execute it.

Set R2_ACCOUNT_ID, R2_BUCKET, R2_ACCESS_KEY_ID, and R2_SECRET_ACCESS_KEY in
the environment. R2_PUBLIC_BASE_URL is additionally required for upload runs;
read-only comparison and downloads do not need it. Standalone sync never reads
desktop UI state.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cli_credentials  # noqa: E402
import libcommon as lib  # noqa: E402
import r2_store as r2  # noqa: E402

PREFIX = "corpus/"

# What belongs to the corpus, relative to the repo root. photo/ is whole
# sessions of capture photography; books/ mixes images (corpus) with OCR
# transcripts (tracked in git, NOT synced here).
_PHOTO_DIR = "photo"
_BOOKS_DIR = "books"
_BOOK_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png"}

_CONTENT_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
}


def content_type_for(rel: str) -> str:
    return _CONTENT_TYPES.get(Path(rel).suffix.lower(), "application/octet-stream")


def local_files(root: Path | None = None) -> dict[str, int]:
    """The corpus on disk, as {repo-relative-posix-path: size}."""
    root = root or lib.ROOT
    out: dict[str, int] = {}
    photo = root / _PHOTO_DIR
    if photo.is_dir():
        for p in sorted(photo.rglob("*")):
            if p.is_file():
                out[p.relative_to(root).as_posix()] = p.stat().st_size
    books = root / _BOOKS_DIR
    if books.is_dir():
        for p in sorted(books.rglob("*")):
            if p.is_file() and p.suffix.lower() in _BOOK_IMAGE_SUFFIXES:
                out[p.relative_to(root).as_posix()] = p.stat().st_size
    return out


def remote_files(cfg: dict) -> dict[str, int]:
    """The corpus in the bucket, keyed like local_files (prefix stripped)."""
    return {key[len(PREFIX):]: size
            for key, size in r2.list_objects(cfg, prefix=PREFIX).items()
            if key.startswith(PREFIX) and key != PREFIX}


def plan(local: dict[str, int], remote: dict[str, int]) -> dict:
    """What a sync would do. Pure: push what is local-only or size-differs,
    pull what is remote-only; never delete on either side."""
    push = sorted(rel for rel, size in local.items() if remote.get(rel) != size)
    pull = sorted(rel for rel in remote if rel not in local)
    same = sorted(rel for rel, size in local.items() if remote.get(rel) == size)
    return {"push": push, "pull": pull, "same": same}


def _cfg(*, require_public_base: bool = False) -> dict:
    return cli_credentials.r2_config(
        require_public_base=require_public_base
    )


def _mb(n: int) -> str:
    return f"{n / 1e6:,.1f} MB"


def _plan_now(cfg: dict | None = None) -> tuple[dict, dict[str, int], dict[str, int]]:
    local = local_files()
    remote = remote_files(cfg or _cfg())
    return plan(local, remote), local, remote


def cmd_status(args) -> None:
    p, local, remote = _plan_now()
    print(f"local corpus:  {len(local)} files, {_mb(sum(local.values()))}")
    print(f"remote corpus: {len(remote)} files, {_mb(sum(remote.values()))}")
    print(f"in sync: {len(p['same'])}   to push: {len(p['push'])} "
          f"({_mb(sum(local[r] for r in p['push']))})   to pull: {len(p['pull'])}")
    for rel in p["push"][:10]:
        print(f"  push {rel}")
    if len(p["push"]) > 10:
        print(f"  ... and {len(p['push']) - 10} more")
    for rel in p["pull"][:10]:
        print(f"  pull {rel}")
    if len(p["pull"]) > 10:
        print(f"  ... and {len(p['pull']) - 10} more")


def cmd_push(args) -> None:
    cfg = _cfg(require_public_base=bool(args.run))
    p, local, _ = _plan_now(cfg)
    if not p["push"]:
        print("nothing to push — remote corpus is current")
        return
    total = sum(local[rel] for rel in p["push"])
    print(f"pushing {len(p['push'])} files, {_mb(total)}")
    if not args.run:
        for rel in p["push"]:
            print(f"  would push {rel} ({_mb(local[rel])})")
        print("dry run — pass --run to upload")
        return
    done = 0
    for i, rel in enumerate(p["push"], 1):
        r2.put_file(cfg, PREFIX + rel, lib.ROOT / rel,
                    content_type=content_type_for(rel))
        done += local[rel]
        print(f"[{i}/{len(p['push'])}] {rel} ({_mb(done)} of {_mb(total)})",
              flush=True)
    print("push complete")


def cmd_pull(args) -> None:
    p, _, remote = _plan_now()
    if not p["pull"]:
        print("nothing to pull — local corpus is current")
        return
    total = sum(remote[rel] for rel in p["pull"])
    print(f"pulling {len(p['pull'])} files, {_mb(total)}")
    if not args.run:
        for rel in p["pull"]:
            print(f"  would pull {rel} ({_mb(remote[rel])})")
        print("dry run — pass --run to download")
        return
    cfg = _cfg()
    for i, rel in enumerate(p["pull"], 1):
        r2.get_file(cfg, PREFIX + rel, lib.ROOT / rel)
        print(f"[{i}/{len(p['pull'])}] {rel}", flush=True)
    print("pull complete")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(
        description="Sync the private book corpus (photo/, books/ images) with R2")
    sub = ap.add_subparsers(required=True)
    s = sub.add_parser("status", help="compare local corpus against the bucket")
    s.set_defaults(fn=cmd_status)
    for name, fn, verb in (("push", cmd_push, "upload"),
                           ("pull", cmd_pull, "download")):
        c = sub.add_parser(name, help=f"{verb} what the other side is missing")
        c.add_argument("--run", action="store_true",
                       help=f"actually {verb} (default: dry run)")
        c.set_defaults(fn=fn)
    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()

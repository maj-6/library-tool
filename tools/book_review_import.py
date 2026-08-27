#!/usr/bin/env python3
"""Validate, dry-run, or explicitly commit a selective book-review import.

Dry-run is the default.  Commit requires a separate flag, confirmation token,
mutable data root, explicit selection, and a conflict-free CAS-pinned plan.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from librarytool.adapters.filesystem.book_review_import import (  # noqa: E402
    FilesystemBookReviewImportAdapter,
)
from librarytool.adapters.filesystem.recoverable_write_set import (  # noqa: E402
    RecoverableWriteSet,
)
from librarytool.catalog_enrichment.importers import (  # noqa: E402
    LegacySourceError,
    resolve_source_paths,
)
from librarytool.engine.book_review_import import (  # noqa: E402
    BOOK_REVIEW_COMMIT_CONFIRMATION,
    BookReviewImportError,
    CurrentSourceIndex,
    ReviewSelection,
    build_review_import_plan,
    commit_review_import_plan,
    load_destination_snapshot,
    load_reviewed_export,
)


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--export", type=Path, required=True)
    value.add_argument(
        "--review-root",
        type=Path,
        required=True,
        help="root beneath which every reasoning member must resolve",
    )
    value.add_argument(
        "--source-root",
        type=Path,
        help="legacy common source root; optional when both exact paths are supplied",
    )
    value.add_argument(
        "--manual-entries-path",
        type=Path,
        help="exact mutable manual_entries.json source path",
    )
    value.add_argument(
        "--ch-library-path",
        type=Path,
        help="exact read-only shipped ch_library.json source path",
    )
    value.add_argument(
        "--captures-dir",
        type=Path,
        help="capture evidence directory used by the manual SourceRecord projection",
    )
    value.add_argument(
        "--destination-snapshot",
        type=Path,
        help="read-only metadata/artifact/CAS state from the Desktop adapter",
    )
    value.add_argument(
        "--create-destination-snapshot",
        type=Path,
        help="create a new selected-only destination snapshot and exit",
    )
    value.add_argument(
        "--review-id",
        action="append",
        default=[],
        help="explicit staging review UUID; repeat to select more",
    )
    value.add_argument(
        "--source-ref",
        action="append",
        default=[],
        help="explicit manual_entries:<id> or ch_library:<index>; repeatable",
    )
    value.add_argument("--output", type=Path, help="write the plan JSON here")
    value.add_argument(
        "--commit",
        action="store_true",
        help="explicitly commit only the selected, conflict-free plan",
    )
    value.add_argument(
        "--confirm",
        default="",
        help=f"--commit requires exactly {BOOK_REVIEW_COMMIT_CONFIRMATION}",
    )
    value.add_argument(
        "--data-root",
        type=Path,
        help="mutable Library Tool root; required for snapshot creation or commit",
    )
    value.add_argument(
        "--backup",
        type=Path,
        help="new persistent pre-import backup ZIP; required for a writing commit",
    )
    return value


def _resolved_source_paths(
    args: argparse.Namespace,
) -> tuple[Path, Path, Path | None]:
    base = None
    if args.source_root is not None:
        try:
            base = resolve_source_paths(args.source_root)
        except LegacySourceError as exc:
            raise BookReviewImportError(
                "--source-root does not resolve Library Tool source files",
                code="current_source_unavailable",
            ) from exc
    manual = args.manual_entries_path or (base.manual_entries if base else None)
    checked = args.ch_library_path or (base.ch_library if base else None)
    captures = args.captures_dir or (base.captures_dir if base else None)
    if manual is None or checked is None:
        raise BookReviewImportError(
            "provide --source-root or both exact source file paths",
            code="current_source_paths_required",
        )
    try:
        manual_path = Path(manual).expanduser().resolve(strict=True)
        checked_path = Path(checked).expanduser().resolve(strict=True)
        captures_path = (
            Path(captures).expanduser().resolve(strict=True)
            if captures is not None
            else None
        )
    except OSError as exc:
        raise BookReviewImportError(
            "an exact Library Tool source path is unavailable",
            code="current_source_unavailable",
        ) from exc
    if (
        not manual_path.is_file()
        or not checked_path.is_file()
        or (captures_path is not None and not captures_path.is_dir())
    ):
        raise BookReviewImportError(
            "Library Tool source paths have the wrong type",
            code="current_source_unavailable",
        )
    return manual_path, checked_path, captures_path


def _selected_source_refs(bundle, selection: ReviewSelection):
    by_id = {record.record_id: record for record in bundle.records}
    by_ref = {
        link.ref: record for record in bundle.records for link in record.source_links
    }
    missing_ids = sorted(selection.review_record_ids - set(by_id))
    missing_refs = sorted(ref.key for ref in selection.source_refs if ref not in by_ref)
    if missing_ids or missing_refs:
        raise BookReviewImportError(
            "one or more explicit selectors do not exist in the reviewed export",
            code="selection_not_found",
            details={
                "review_record_ids": missing_ids,
                "source_refs": missing_refs,
            },
        )
    refs = set(selection.source_refs)
    for record_id in selection.review_record_ids:
        refs.update(link.ref for link in by_id[record_id].source_links)
    return tuple(sorted(refs))


def _data_root(value: Path | None) -> Path:
    if value is None:
        raise BookReviewImportError(
            "an explicit --data-root is required",
            code="book_review_commit_data_root_required",
        )
    try:
        root = value.expanduser().resolve(strict=True)
    except OSError as exc:
        raise BookReviewImportError(
            "--data-root must already exist",
            code="book_review_commit_data_root_required",
        ) from exc
    if not root.is_dir():
        raise BookReviewImportError(
            "--data-root must be an existing directory",
            code="book_review_commit_data_root_required",
        )
    return root


def _require_manual_path_matches_data_root(
    refs,
    manual_path: Path,
    data_root: Path,
) -> None:
    if (
        any(ref.namespace == "manual_entries" for ref in refs)
        and manual_path != (data_root / "output" / "manual_entries.json").resolve()
    ):
        raise BookReviewImportError(
            "committed manual source must be data-root/output/manual_entries.json",
            code="book_review_commit_manual_path_mismatch",
        )


def _write_new_json(path: Path, value: object) -> tuple[int, str]:
    payload = (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    configured = path.expanduser()
    target: Path | None = None
    descriptor = -1
    created = False
    try:
        parent = configured.parent.resolve(strict=True)
        target = parent / configured.name
        descriptor = os.open(
            target,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
            0o600,
        )
        created = True
    except FileExistsError as exc:
        raise BookReviewImportError(
            "destination snapshot already exists",
            code="destination_snapshot_exists",
        ) from exc
    except OSError as exc:
        raise BookReviewImportError(
            "destination snapshot cannot be created",
            code="destination_snapshot_unavailable",
        ) from exc
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        if created and target is not None:
            try:
                os.unlink(target)
            except OSError:
                pass
        raise BookReviewImportError(
            "destination snapshot cannot be persisted",
            code="destination_snapshot_unavailable",
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return len(payload), hashlib.sha256(payload).hexdigest()


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        selection = ReviewSelection.explicit(
            review_record_ids=args.review_id,
            source_refs=args.source_ref,
        )
        bundle = load_reviewed_export(args.export, args.review_root)
        manual_path, ch_path, captures_path = _resolved_source_paths(args)
        current_sources = CurrentSourceIndex.from_paths(
            manual_path,
            ch_path,
            captures_path,
        )
        selected_refs = _selected_source_refs(bundle, selection)
        if args.create_destination_snapshot is not None:
            if args.destination_snapshot is not None or args.commit:
                raise BookReviewImportError(
                    "snapshot creation cannot also load a snapshot or commit",
                    code="invalid_destination_snapshot_mode",
                )
            if args.backup is not None:
                raise BookReviewImportError(
                    "--backup is valid only with --commit",
                    code="invalid_book_review_backup_mode",
                )
            data_root = _data_root(args.data_root)
            _require_manual_path_matches_data_root(
                selected_refs,
                manual_path,
                data_root,
            )
            adapter = FilesystemBookReviewImportAdapter(
                RecoverableWriteSet(data_root),
                ch_library_path=ch_path,
            )
            snapshot = adapter.snapshot(selected_refs)
            byte_size, snapshot_sha256 = _write_new_json(
                args.create_destination_snapshot,
                snapshot,
            )
            result = {
                "ok": True,
                "schema": "librarytool.book-review-destination-snapshot-created/1",
                "selected_source_refs": len(selected_refs),
                "byte_size": byte_size,
                "sha256": snapshot_sha256,
            }
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0
        if args.destination_snapshot is None:
            raise BookReviewImportError(
                "provide --destination-snapshot or --create-destination-snapshot",
                code="destination_snapshot_required",
            )
        if args.backup is not None and not args.commit:
            raise BookReviewImportError(
                "--backup is valid only with --commit",
                code="invalid_book_review_backup_mode",
            )
        destination = load_destination_snapshot(args.destination_snapshot)
        plan = build_review_import_plan(
            bundle,
            selection=selection,
            current_sources=current_sources,
            destination=destination,
        )
        result: object = plan.as_dict()
        if args.commit:
            if args.confirm != BOOK_REVIEW_COMMIT_CONFIRMATION:
                raise BookReviewImportError(
                    "--commit requires its exact confirmation token",
                    code="book_review_commit_confirmation_required",
                )
            # Refuse conflicts before constructing the write coordinator,
            # whose initialization creates its private transaction directory.
            commit_requests = plan.atomic_requests()
            data_root = _data_root(args.data_root)
            _require_manual_path_matches_data_root(
                tuple(request.unit.source_ref for request in commit_requests),
                manual_path,
                data_root,
            )
            adapter = FilesystemBookReviewImportAdapter(
                RecoverableWriteSet(data_root),
                ch_library_path=ch_path,
            )
            backup = None
            if commit_requests:
                if args.backup is None:
                    raise BookReviewImportError(
                        "a writing commit requires an explicit persistent --backup ZIP",
                        code="book_review_preimport_backup_required",
                    )
                backup = adapter.create_preimport_backup(
                    commit_requests,
                    args.backup,
                )
            committed = commit_review_import_plan(
                plan,
                adapter,
                confirmation=args.confirm,
            )
            result = {
                "plan": plan.as_dict(),
                "backup": backup,
                "commit": committed.as_dict(),
            }
        payload = (
            json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        )
        if args.output:
            output = args.output.expanduser().resolve()
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(payload, encoding="utf-8", newline="\n")
        else:
            print(payload, end="")
        return 0 if plan.ready else 3
    except BookReviewImportError as exc:
        result: dict[str, object] = {
            "ok": False,
            "code": exc.code,
            "error": str(exc),
        }
        if exc.details:
            result["details"] = exc.details
        print(
            json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

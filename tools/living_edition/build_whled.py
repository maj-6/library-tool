#!/usr/bin/env python3
"""Build, validate, or inspect a ``whled/0.1`` package.

Build input is a JSON manifest plus a resource root. Every
``manifest.resources[].member`` is read at that same relative path beneath the
root. The command never discovers files implicitly, which prevents scratch
files, credentials, or the external authority SQLite database from leaking
into an edition.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import whled


def _resource_payloads(manifest: dict, root: Path) -> dict[str, Path]:
    resolved_root = root.resolve()
    payloads: dict[str, Path] = {}
    for index, resource in enumerate(manifest.get("resources", [])):
        if not isinstance(resource, dict) or not isinstance(resource.get("member"), str):
            raise whled.WhledError(f"manifest.resources[{index}].member must be text")
        member = resource["member"]
        candidate = (resolved_root / Path(*member.split("/"))).resolve()
        try:
            candidate.relative_to(resolved_root)
        except ValueError as exc:
            raise whled.WhledError(f"resource escapes the resource root: {member}") from exc
        if not candidate.is_file():
            raise whled.WhledError(f"resource does not exist: {candidate}")
        payloads[member] = candidate
    return payloads


def _print_issues(issues: list[whled.Issue], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps([
            {"level": issue.level, "location": issue.location, "message": issue.message}
            for issue in issues
        ], ensure_ascii=False, indent=2))
        return
    if not issues:
        print("valid")
    for issue in issues:
        print(f"{issue.level}: {issue.location}: {issue.message}")


def _build(args: argparse.Namespace) -> int:
    manifest = whled.load_manifest(args.manifest)
    output = args.output.resolve()
    if output.exists() and not args.force:
        raise whled.WhledError(f"output already exists (pass --force to replace): {output}")
    if output.suffix.casefold() != ".whled":
        raise whled.WhledError("output filename must use the .whled extension")
    root = (args.resource_root or args.manifest.parent).resolve()
    payloads = _resource_payloads(manifest, root)
    finalized = whled.seal_archive(manifest, payloads, output)
    issues = whled.validate_archive(output)
    _print_issues(issues, as_json=False)
    if any(issue.level == "error" for issue in issues):
        return 1
    print(
        f"sealed {output} ({output.stat().st_size} bytes, "
        f"{len(finalized['canvases'])} canvases, {len(finalized['layers'])} layer revisions)"
    )
    return 0


def _validate(args: argparse.Namespace) -> int:
    issues = whled.validate_archive(args.archive)
    _print_issues(issues, as_json=args.json)
    return 1 if any(issue.level == "error" for issue in issues) else 0


def _inspect(args: argparse.Namespace) -> int:
    document = whled.read_archive(args.archive)
    manifest = document.manifest
    summary = {
        "format": manifest["format"],
        "package_id": manifest["package_id"],
        "title": manifest["catalog"]["title"],
        "edition": manifest["edition"],
        "canvas_count": len(manifest["canvases"]),
        "layer_revisions": [
            {
                "id": layer["id"],
                "revision": layer["revision"],
                "kind": layer["kind"],
                "variant": layer["variant"],
                "current": layer["current"],
            }
            for layer in manifest["layers"]
        ],
        "authority_snapshots": manifest["authority_snapshots"],
        "external_authorities": manifest["external_authorities"],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    build = subparsers.add_parser("build", help="seal a manifest and declared resources")
    build.add_argument("manifest", type=Path)
    build.add_argument("output", type=Path)
    build.add_argument("--resource-root", type=Path)
    build.add_argument("--force", action="store_true")
    build.set_defaults(handler=_build)

    validate = subparsers.add_parser("validate", help="validate without changing an archive")
    validate.add_argument("archive", type=Path)
    validate.add_argument("--json", action="store_true", help="emit a machine-readable issue array")
    validate.set_defaults(handler=_validate)

    inspect = subparsers.add_parser("inspect", help="print package and layer inventory")
    inspect.add_argument("archive", type=Path)
    inspect.set_defaults(handler=_inspect)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return args.handler(args)
    except (OSError, whled.WhledError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

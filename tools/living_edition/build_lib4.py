#!/usr/bin/env python3
"""Build, validate, or inspect a ``lib/4`` Living Edition package.

Build reads only the embedded members explicitly declared in
``resources[].locations[]``. Remote HTTP, S3, and IIIF locations are recorded
but never fetched. This prevents accidental inclusion of scratch files,
credentials, mutable authority databases, or unrelated source material.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import lib4


def _resource_payloads(manifest: dict, root: Path) -> dict[str, Path]:
    resolved_root = root.resolve()
    payloads: dict[str, Path] = {}
    for resource_index, resource in enumerate(manifest.get("resources", [])):
        if not isinstance(resource, dict):
            raise lib4.Lib4Error(f"manifest.resources[{resource_index}] must be an object")
        for location_index, location in enumerate(resource.get("locations", [])):
            if not isinstance(location, dict) or location.get("type") != "embedded":
                continue
            member = location.get("member")
            if not isinstance(member, str):
                raise lib4.Lib4Error(
                    f"manifest.resources[{resource_index}].locations[{location_index}].member must be text"
                )
            candidate = (resolved_root / Path(*member.split("/"))).resolve()
            try:
                candidate.relative_to(resolved_root)
            except ValueError as exc:
                raise lib4.Lib4Error(f"resource escapes the resource root: {member}") from exc
            if not candidate.is_file():
                raise lib4.Lib4Error(f"embedded resource does not exist: {candidate}")
            payloads[member] = candidate
    return payloads


def _print_issues(issues: list[lib4.Issue], *, as_json: bool) -> None:
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
    manifest = lib4.load_manifest(args.manifest)
    output = args.output.resolve()
    if output.exists() and not args.force:
        raise lib4.Lib4Error(f"output already exists (pass --force to replace): {output}")
    if output.suffix.casefold() != ".lib4":
        raise lib4.Lib4Error("output filename must use the .lib4 extension")
    root = (args.resource_root or args.manifest.parent).resolve()
    payloads = _resource_payloads(manifest, root)
    finalized = lib4.seal_archive(manifest, payloads, output)
    issues = lib4.validate_archive(output)
    _print_issues(issues, as_json=False)
    if any(issue.level == "error" for issue in issues):
        return 1
    embedded = sum(
        1 for resource in finalized["resources"] for location in resource["locations"]
        if location["type"] == "embedded"
    )
    print(
        f"sealed {output} ({output.stat().st_size} bytes, {len(finalized['canvases'])} canvases, "
        f"{len(finalized['layers'])} layer revisions, {embedded} embedded resources)"
    )
    return 0


def _validate(args: argparse.Namespace) -> int:
    issues = lib4.validate_archive(args.archive)
    _print_issues(issues, as_json=args.json)
    return 1 if any(issue.level == "error" for issue in issues) else 0


def _inspect(args: argparse.Namespace) -> int:
    document = lib4.read_archive(args.archive)
    manifest = document.manifest
    external = sum(
        1 for resource in manifest["resources"]
        if any(location["type"] != "embedded" for location in resource["locations"])
    )
    summary = {
        "format": manifest["format"],
        "profile": f"{manifest['profile']}/{manifest['profile_version']}",
        "media_type": lib4.MEDIA_TYPE,
        "package_id": manifest["package_id"],
        "package_revision": manifest["package_revision"],
        "catalog_record": manifest["catalog"]["record"],
        "source_material": len(manifest["source_material"]),
        "structures": len(manifest["structures"]),
        "canvases": len(manifest["canvases"]),
        "resources": len(manifest["resources"]),
        "resources_with_external_locations": external,
        "layers": [
            {
                "id": item["id"],
                "revision": item["revision"],
                "kind": item["kind"],
                "lifecycle": item["lifecycle"]["state"],
                "editorial_status": item["editorial_status"],
                "current": item["current"],
            }
            for item in manifest["layers"]
        ],
        "releases": manifest["releases"],
        "retrieval": manifest.get("retrieval"),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    build = subparsers.add_parser("build", help="seal declared embedded resources")
    build.add_argument("manifest", type=Path)
    build.add_argument("output", type=Path)
    build.add_argument("--resource-root", type=Path)
    build.add_argument("--force", action="store_true")
    build.set_defaults(handler=_build)

    validate = subparsers.add_parser("validate", help="validate without changing an archive")
    validate.add_argument("archive", type=Path)
    validate.add_argument("--json", action="store_true", help="emit a machine-readable issue array")
    validate.set_defaults(handler=_validate)

    inspect = subparsers.add_parser("inspect", help="print catalog, resource, layer, and release inventory")
    inspect.add_argument("archive", type=Path)
    inspect.set_defaults(handler=_inspect)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return args.handler(args)
    except (OSError, lib4.Lib4Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

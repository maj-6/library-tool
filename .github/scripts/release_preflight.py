#!/usr/bin/env python3
"""Fail-closed release classification and Android version scoping.

The workflow keeps this logic in a tested Python module instead of spreading
slightly different shell globs and Gradle parsing across release steps.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
from typing import Any, NamedTuple, Sequence


PUBLIC_VERSION_RE = re.compile(
    r"^(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"
    r"(?:-(?P<channel>alpha|beta|rc)\.(?:0|[1-9]\d*))?$"
)
ANDROID_APK_RE = re.compile(r"^BookCapture-[^/\\]+\.apk$")
VERSION_CODE_RE = re.compile(r"^\s*versionCode\s*=\s*(\d+)\s*(?://.*)?$", re.MULTILINE)
VERSION_NAME_RE = re.compile(
    r'^\s*versionName\s*=\s*"([^"\r\n]+)"\s*(?://.*)?$', re.MULTILINE
)
SAFE_ANDROID_VERSION_NAME_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._+-]*$")


class PreflightError(RuntimeError):
    """A release invariant could not be established safely."""


class AndroidVersion(NamedTuple):
    code: int
    name: str


class AndroidScope(NamedTuple):
    release: bool
    current: AndroidVersion
    baseline_tag: str | None


class CurrentRelease(NamedTuple):
    draft: bool
    asset_names: tuple[str, ...]


def classify_public_version(version: str) -> str:
    """Return the one public channel represented by an exact supported version."""

    match = PUBLIC_VERSION_RE.fullmatch(version)
    if match is None:
        raise PreflightError(
            f"Unsupported public version {version!r}. Use X.Y.Z or "
            "X.Y.Z-(alpha|beta|rc).N; workflow-dispatch artifacts are the "
            "only debug builds."
        )
    return match.group("channel") or "stable"


def parse_android_version(text: str, source: str) -> AndroidVersion:
    """Parse exactly one literal versionCode and versionName assignment."""

    codes = VERSION_CODE_RE.findall(text)
    names = VERSION_NAME_RE.findall(text)
    if len(codes) != 1 or len(names) != 1:
        raise PreflightError(
            f"{source} must contain exactly one literal versionCode and one "
            "literal versionName assignment; found "
            f"versionCode={len(codes)}, versionName={len(names)}."
        )
    code = int(codes[0])
    if code < 1:
        raise PreflightError(f"{source} versionCode must be a positive integer.")
    name = names[0]
    if SAFE_ANDROID_VERSION_NAME_RE.fullmatch(name) is None:
        raise PreflightError(
            f"{source} versionName {name!r} is unsafe for a release asset name."
        )
    return AndroidVersion(code=code, name=name)


def _parse_published_at(raw: Any, tag: str) -> datetime:
    if not isinstance(raw, str) or not raw:
        raise PreflightError(
            f"Published release {tag!r} has no published_at timestamp."
        )
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PreflightError(
            f"Published release {tag!r} has an invalid published_at timestamp."
        ) from exc
    if parsed.tzinfo is None:
        raise PreflightError(
            f"Published release {tag!r} has a timezone-free published_at timestamp."
        )
    return parsed


def flatten_release_pages(payload: Any) -> list[dict[str, Any]]:
    """Validate gh api --paginate --slurp output and flatten its pages."""

    if not isinstance(payload, list):
        raise PreflightError("GitHub releases response must be a JSON array of pages.")
    if not payload:
        return []
    if all(isinstance(page, list) for page in payload):
        releases = [release for page in payload for release in page]
    elif all(isinstance(release, dict) for release in payload):
        # Also accept one unwrapped page for local use and fixture readability.
        releases = list(payload)
    else:
        raise PreflightError("GitHub releases response mixes invalid page shapes.")
    if not all(isinstance(release, dict) for release in releases):
        raise PreflightError("GitHub releases response contains a non-object release.")
    return releases


def newest_android_release(
    payload: Any, *, exclude_tag: str | None = None
) -> str | None:
    """Find the newest prior published release containing a real Android APK."""

    candidates: list[tuple[datetime, str]] = []
    for release in flatten_release_pages(payload):
        draft = release.get("draft")
        tag = release.get("tag_name")
        assets = release.get("assets")
        if not isinstance(draft, bool):
            raise PreflightError("GitHub release has a missing or invalid draft field.")
        if not isinstance(tag, str) or not tag:
            raise PreflightError("GitHub release has a missing or invalid tag_name.")
        if not isinstance(assets, list):
            raise PreflightError(f"GitHub release {tag!r} has an invalid assets field.")
        if draft or tag == exclude_tag:
            continue

        published_at = _parse_published_at(release.get("published_at"), tag)
        has_real_apk = False
        for asset in assets:
            if not isinstance(asset, dict):
                raise PreflightError(f"GitHub release {tag!r} has a non-object asset.")
            name = asset.get("name")
            if not isinstance(name, str):
                raise PreflightError(
                    f"GitHub release {tag!r} has an asset without a name."
                )
            if not ANDROID_APK_RE.fullmatch(name):
                continue
            if "DONOTPUBLISH" in name.upper():
                continue
            state = asset.get("state")
            size = asset.get("size")
            if (
                not isinstance(state, str)
                or not isinstance(size, int)
                or isinstance(size, bool)
            ):
                raise PreflightError(
                    f"GitHub release {tag!r} has malformed metadata for {name!r}."
                )
            if state == "uploaded" and isinstance(size, int) and size > 0:
                has_real_apk = True
        if has_real_apk:
            candidates.append((published_at, tag))

    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


def find_current_release(payload: Any, tag: str) -> CurrentRelease | None:
    """Return the exact current release, including drafts, from list output."""

    matches = [
        release
        for release in flatten_release_pages(payload)
        if release.get("tag_name") == tag
    ]
    if len(matches) > 1:
        raise PreflightError(f"GitHub returned duplicate releases for tag {tag!r}.")
    if not matches:
        return None
    release = matches[0]
    draft = release.get("draft")
    assets = release.get("assets")
    if not isinstance(draft, bool) or not isinstance(assets, list):
        raise PreflightError(f"GitHub release {tag!r} has malformed state or assets.")
    names: list[str] = []
    for asset in assets:
        if not isinstance(asset, dict) or not isinstance(asset.get("name"), str):
            raise PreflightError(f"GitHub release {tag!r} has an asset without a name.")
        name = asset["name"]
        if "\n" in name or "\r" in name:
            raise PreflightError(f"GitHub release {tag!r} has an unsafe asset name.")
        state = asset.get("state")
        size = asset.get("size")
        if (
            not isinstance(state, str)
            or not isinstance(size, int)
            or isinstance(size, bool)
        ):
            raise PreflightError(
                f"GitHub release {tag!r} has malformed metadata for {name!r}."
            )
        if state == "uploaded" and size > 0:
            names.append(name)
    return CurrentRelease(draft=draft, asset_names=tuple(names))


def validate_version_progression(
    current: AndroidVersion, previous: AndroidVersion, baseline_tag: str
) -> bool:
    """Return whether Android should ship, rejecting ambiguous identities."""

    if current == previous:
        return False
    if current.code <= previous.code:
        raise PreflightError(
            f"Android versionCode must increase beyond {previous.code} from "
            f"{baseline_tag}; current value is {current.code}. Both versionCode "
            "and versionName must change for an Android release."
        )
    if current.name == previous.name:
        raise PreflightError(
            f"Android versionCode changed from {previous.code} to {current.code}, "
            f"but versionName is still {current.name!r}. Both values must change."
        )
    return True


def _validated_repo_path(raw_path: str) -> str:
    path = PurePosixPath(raw_path.replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise PreflightError("Android version file must be a repository-relative path.")
    return path.as_posix()


def read_file_at_tag(repository: Path, tag: str, relative_path: str) -> str:
    """Read a tracked file from a release tag without accepting option injection."""

    if tag.startswith("android-v"):
        version = tag.removeprefix("android-v")
    elif tag.startswith("v"):
        version = tag.removeprefix("v")
    else:
        raise PreflightError(f"Published Android release has an unsafe tag {tag!r}.")
    try:
        classify_public_version(version)
    except PreflightError as exc:
        raise PreflightError(
            f"Published Android release has an unsupported tag {tag!r}; "
            "refusing to construct a git revision from it."
        ) from exc
    path = _validated_repo_path(relative_path)
    spec = f"refs/tags/{tag}:{path}"
    try:
        result = subprocess.run(
            ["git", "show", spec],
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise PreflightError(
            f"Could not read {path} from published Android baseline {tag}. "
            "Refusing to guess the release scope."
        ) from exc
    return result.stdout


def determine_android_scope(
    *,
    releases_payload: Any,
    repository: Path,
    version_file: str,
    exclude_tag: str | None,
) -> AndroidScope:
    path = _validated_repo_path(version_file)
    current_path = repository / Path(*PurePosixPath(path).parts)
    try:
        current_text = current_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PreflightError(
            f"Could not read current Android version file {path}."
        ) from exc
    current = parse_android_version(current_text, path)
    baseline_tag = newest_android_release(releases_payload, exclude_tag=exclude_tag)
    if baseline_tag is None:
        return AndroidScope(True, current, None)
    previous_text = read_file_at_tag(repository, baseline_tag, path)
    previous = parse_android_version(previous_text, f"{baseline_tag}:{path}")
    return AndroidScope(
        validate_version_progression(current, previous, baseline_tag),
        current,
        baseline_tag,
    )


def append_github_outputs(path: Path, values: dict[str, str]) -> None:
    try:
        with path.open("a", encoding="utf-8", newline="\n") as output:
            for key, value in values.items():
                if "\n" in key or "\n" in value:
                    raise PreflightError(
                        "GitHub output values must be single-line strings."
                    )
                output.write(f"{key}={value}\n")
    except OSError as exc:
        raise PreflightError(f"Could not write GitHub outputs to {path}.") from exc


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PreflightError(
            f"Could not parse GitHub releases response at {path}."
        ) from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    classify = commands.add_parser("classify-version")
    classify.add_argument("--version", required=True)
    classify.add_argument("--github-output", type=Path, required=True)

    scope = commands.add_parser("android-scope")
    scope.add_argument("--releases-json", type=Path, required=True)
    scope.add_argument("--repository", type=Path, default=Path.cwd())
    scope.add_argument(
        "--version-file",
        default="android/BookCapture/app/build.gradle.kts",
    )
    scope.add_argument("--exclude-tag")
    scope.add_argument("--github-output", type=Path, required=True)

    inspect = commands.add_parser("inspect-release")
    inspect.add_argument("--releases-json", type=Path, required=True)
    inspect.add_argument("--tag", required=True)
    inspect.add_argument("--assets-output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "classify-version":
            channel = classify_public_version(args.version)
            append_github_outputs(
                args.github_output,
                {"release_channel": channel},
            )
            print(f"Public release channel: {channel}")
            return 0

        if args.command == "inspect-release":
            release = find_current_release(_load_json(args.releases_json), args.tag)
            asset_names = () if release is None else release.asset_names
            try:
                args.assets_output.write_text(
                    "".join(f"{name}\n" for name in asset_names), encoding="utf-8"
                )
            except OSError as exc:
                raise PreflightError(
                    f"Could not write existing release assets to {args.assets_output}."
                ) from exc
            if release is None:
                print("absent")
            else:
                print("draft" if release.draft else "published")
            return 0

        scope = determine_android_scope(
            releases_payload=_load_json(args.releases_json),
            repository=args.repository.resolve(),
            version_file=args.version_file,
            exclude_tag=args.exclude_tag,
        )
        append_github_outputs(
            args.github_output,
            {
                "android_release": str(scope.release).lower(),
                "android_version": scope.current.name,
                "android_baseline_tag": scope.baseline_tag or "",
            },
        )
        if scope.baseline_tag is None:
            print("No prior published BookCapture APK exists; including Android.")
        elif scope.release:
            print(
                f"Android version advanced beyond {scope.baseline_tag}; "
                "including Android."
            )
        else:
            print(
                f"Android version is unchanged from the APK on {scope.baseline_tag}; "
                "this is deliberately a desktop-only build."
            )
        return 0
    except PreflightError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

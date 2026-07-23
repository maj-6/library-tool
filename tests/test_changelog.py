from __future__ import annotations

import re
import tomllib
from pathlib import Path


ROOT = Path(__file__).parents[1]
DESKTOP_CHANGELOG = (ROOT / "website" / "changelog.md").read_text(encoding="utf-8")
ANDROID_CHANGELOG = (ROOT / "website" / "android-changelog.md").read_text(encoding="utf-8")
PACKAGED_ANDROID_CHANGELOG = (
    ROOT
    / "android"
    / "BookCapture"
    / "app"
    / "src"
    / "release"
    / "res"
    / "raw"
    / "android_changelog.md"
).read_text(encoding="utf-8")
CATEGORY_ORDER = ("Additions", "Other Changes", "Bugfixes")


def validate_changelog(changelog: str) -> list[str]:
    versions: list[str] = []
    categories: dict[str, list[str]] = {}
    bullet_counts: dict[tuple[str, str], int] = {}
    current_version = ""
    current_category = ""

    for raw in changelog.splitlines():
        if match := re.match(r"^##\s+(.+?)\s+—\s+\d{4}-\d{2}-\d{2}$", raw):
            current_version = match.group(1)
            current_category = ""
            versions.append(current_version)
            categories[current_version] = []
        elif match := re.match(r"^###\s+(.+)$", raw):
            assert current_version
            current_category = match.group(1)
            assert current_category in CATEGORY_ORDER
            assert current_category not in categories[current_version]
            categories[current_version].append(current_category)
        elif raw.startswith("- "):
            assert current_version
            assert current_category
            bullet_counts[(current_version, current_category)] = (
                bullet_counts.get((current_version, current_category), 0) + 1
            )

    assert versions
    assert len(versions) == len(set(versions))
    for version in versions:
        names = categories[version]
        assert names
        assert names == sorted(names, key=CATEGORY_ORDER.index)
        assert all(bullet_counts.get((version, name), 0) for name in names)

    assert "<!--more-->" not in changelog
    return versions


def test_changelogs_use_public_release_categories_in_a_fixed_order():
    desktop_versions = validate_changelog(DESKTOP_CHANGELOG)
    android_versions = validate_changelog(ANDROID_CHANGELOG)

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    # Desktop may stage one clearly-labelled unreleased section while Android
    # publishes independently.  The packaged desktop version must still be
    # the first concrete release immediately after that section.
    if desktop_versions[0] == "Next prerelease":
        assert desktop_versions.count("Next prerelease") == 1
        assert desktop_versions[1] == project["project"]["version"]
    else:
        assert desktop_versions[0] == project["project"]["version"]

    android_build = (ROOT / "android" / "BookCapture" / "app" / "build.gradle.kts").read_text(
        encoding="utf-8"
    )
    android_version = re.search(r'versionName\s*=\s*"([^"]+)"', android_build)
    assert android_version
    assert android_versions[0] == android_version.group(1)


def test_changelog_avoids_internal_release_note_terms():
    for term in (
        "credential-aware",
        "DNS-rebinding",
        "Host headers",
        "local-only secrets store",
        "service-role",
        "Supabase",
        "CPRS",
    ):
        assert term not in DESKTOP_CHANGELOG
        assert term not in ANDROID_CHANGELOG


def test_android_release_packages_the_public_android_changelog_verbatim():
    assert PACKAGED_ANDROID_CHANGELOG == ANDROID_CHANGELOG

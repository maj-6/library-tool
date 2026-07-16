from __future__ import annotations

import re
import tomllib
from pathlib import Path


ROOT = Path(__file__).parents[1]
CHANGELOG = (ROOT / "website" / "changelog.md").read_text(encoding="utf-8")
CATEGORY_ORDER = ("Additions", "Other Changes", "Bugfixes")


def test_changelog_uses_public_release_categories_in_a_fixed_order():
    versions: list[str] = []
    categories: dict[str, list[str]] = {}
    bullet_counts: dict[tuple[str, str], int] = {}
    current_version = ""
    current_category = ""

    for raw in CHANGELOG.splitlines():
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

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert versions[0] == project["project"]["version"]
    assert "<!--more-->" not in CHANGELOG


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
        assert term not in CHANGELOG

import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".github" / "scripts" / "release_preflight.py"
SPEC = importlib.util.spec_from_file_location("release_preflight", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
preflight = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(preflight)


def _asset(name="BookCapture-0.5.1.apk", *, state="uploaded", size=1234):
    return {"name": name, "state": state, "size": size}


def _release(
    tag,
    published_at,
    *,
    assets=None,
    draft=False,
):
    return {
        "tag_name": tag,
        "published_at": published_at,
        "draft": draft,
        "assets": [] if assets is None else assets,
    }


@pytest.mark.parametrize(
    ("version", "channel"),
    [
        ("0.5.1", "stable"),
        ("0.5.1-alpha.0", "alpha"),
        ("12.34.56-beta.2", "beta"),
        ("1.0.0-rc.10", "rc"),
    ],
)
def test_exact_public_version_classifier_accepts_supported_versions(version, channel):
    assert preflight.classify_public_version(version) == channel


@pytest.mark.parametrize(
    "version",
    [
        "0.5",
        "v0.5.1",
        "01.5.1",
        "0.5.1-alpha",
        "0.5.1-alpha.01",
        "0.5.1-debug",
        "0.5.1-debug-alpha.1",
        "0.5.1-alpha.1-debug",
        "0.5.1+build.1",
        " 0.5.1 ",
    ],
)
def test_exact_public_version_classifier_rejects_ambiguous_versions(version):
    with pytest.raises(preflight.PreflightError, match="Unsupported public version"):
        preflight.classify_public_version(version)


def test_android_version_parser_requires_one_literal_assignment_each():
    parsed = preflight.parse_android_version(
        'android {\n  versionCode = 26 // monotonic\n  versionName = "0.5.2"\n}\n',
        "build.gradle.kts",
    )
    assert parsed == preflight.AndroidVersion(26, "0.5.2")

    for malformed in [
        'versionName = "0.5.2"',
        "versionCode = 26",
        'versionCode = computeCode()\nversionName = "0.5.2"',
        'versionCode = 26\nversionCode = 27\nversionName = "0.5.2"',
        'versionCode = 0\nversionName = "0.5.2"',
        'versionCode = 26\nversionName = "0.5.2#public"',
        'versionCode = 26\nversionName = "../../escape"',
    ]:
        with pytest.raises(preflight.PreflightError):
            preflight.parse_android_version(malformed, "build.gradle.kts")


def test_android_baseline_is_newest_published_release_with_real_apk():
    payload = [
        [
            _release(
                "v0.5.5",
                "2026-07-18T00:00:00Z",
                assets=[_asset("BookCapture-0.5.5.apk")],
                draft=True,
            ),
            _release(
                "v0.5.4",
                "2026-07-17T00:00:00Z",
                assets=[_asset("BookCapture-0.5.4-debug-DONOTPUBLISH.apk")],
            ),
            _release(
                "v0.5.3",
                "2026-07-16T00:00:00Z",
                assets=[_asset("BookCapture-0.5.3.apk", size=0)],
            ),
        ],
        [
            _release(
                "v0.5.2",
                "2026-07-15T00:00:00Z",
                assets=[_asset("BookCapture-0.5.2.apk")],
            ),
            _release(
                "v0.5.1",
                "2026-07-14T00:00:00Z",
                assets=[_asset("BookCapture-0.5.1.apk")],
            ),
        ],
    ]

    assert preflight.newest_android_release(payload) == "v0.5.2"


def test_current_tag_is_excluded_from_android_baseline_on_rerun():
    payload = [
        [
            _release(
                "v0.5.2",
                "2026-07-15T00:00:00Z",
                assets=[_asset("BookCapture-0.5.2.apk")],
            ),
            _release(
                "v0.5.1",
                "2026-07-14T00:00:00Z",
                assets=[_asset("BookCapture-0.5.1.apk")],
            ),
        ]
    ]

    assert preflight.newest_android_release(payload, exclude_tag="v0.5.2") == "v0.5.1"


def test_current_release_inspection_includes_draft_assets_for_recovery():
    payload = [
        [
            _release(
                "v0.5.2",
                None,
                draft=True,
                assets=[
                    _asset("LibraryTool-Setup-0.5.2.exe"),
                    _asset("BookCapture-0.5.2.apk"),
                    _asset("BookCapture-incomplete.apk", size=0),
                ],
            ),
            _release("v0.5.1", "2026-07-14T00:00:00Z"),
        ]
    ]

    assert preflight.find_current_release(payload, "v0.5.2") == (
        True,
        ("LibraryTool-Setup-0.5.2.exe", "BookCapture-0.5.2.apk"),
    )
    assert preflight.find_current_release(payload, "v9.9.9") is None


def test_duplicate_current_release_records_fail_closed():
    release = _release("v0.5.2", "2026-07-15T00:00:00Z")

    with pytest.raises(preflight.PreflightError, match="duplicate releases"):
        preflight.find_current_release([[release, release]], "v0.5.2")


def test_current_release_asset_metadata_fails_closed():
    release = _release(
        "v0.5.2",
        "2026-07-15T00:00:00Z",
        assets=[{"name": "BookCapture-0.5.2.apk"}],
    )

    with pytest.raises(preflight.PreflightError, match="malformed metadata"):
        preflight.find_current_release([[release]], "v0.5.2")


@pytest.mark.parametrize(
    "payload",
    [
        {},
        [["not a release"]],
        [[{"tag_name": "v1.0.0", "draft": False, "assets": []}]],
        [
            [
                {
                    "tag_name": "v1.0.0",
                    "draft": False,
                    "published_at": "not-a-date",
                    "assets": [],
                }
            ]
        ],
        [
            [
                {
                    "tag_name": "v1.0.0",
                    "draft": False,
                    "published_at": "2026-07-14T00:00:00Z",
                    "assets": [None],
                }
            ]
        ],
    ],
)
def test_malformed_github_release_responses_fail_closed(payload):
    with pytest.raises(preflight.PreflightError):
        preflight.newest_android_release(payload)


def test_android_version_progression_skips_only_an_identical_identity():
    previous = preflight.AndroidVersion(25, "0.5.1")

    assert not preflight.validate_version_progression(previous, previous, "v0.5.1")
    assert preflight.validate_version_progression(
        preflight.AndroidVersion(26, "0.5.2"), previous, "v0.5.1"
    )


@pytest.mark.parametrize(
    "current",
    [
        preflight.AndroidVersion(25, "0.5.2"),  # name only
        preflight.AndroidVersion(26, "0.5.1"),  # code only
        preflight.AndroidVersion(24, "0.5.2"),  # decreasing code
    ],
)
def test_android_version_progression_rejects_partial_or_nonmonotonic_bumps(current):
    with pytest.raises(preflight.PreflightError, match="Both"):
        preflight.validate_version_progression(
            current, preflight.AndroidVersion(25, "0.5.1"), "v0.5.1"
        )


def test_scope_ignores_newer_partial_release_and_uses_last_shipped_apk(
    tmp_path, monkeypatch
):
    version_file = Path("android/BookCapture/app/build.gradle.kts")
    current_file = tmp_path / version_file
    current_file.parent.mkdir(parents=True)
    current_file.write_text(
        'versionCode = 26\nversionName = "0.5.2"\n', encoding="utf-8"
    )
    payload = [
        [
            _release("v0.5.2-alpha.1", "2026-07-16T00:00:00Z", assets=[]),
            _release(
                "v0.5.1",
                "2026-07-15T00:00:00Z",
                assets=[_asset("BookCapture-0.5.1.apk")],
            ),
        ]
    ]
    seen = []

    def read_at_tag(repository, tag, relative_path):
        seen.append((repository, tag, relative_path))
        return 'versionCode = 25\nversionName = "0.5.1"\n'

    monkeypatch.setattr(preflight, "read_file_at_tag", read_at_tag)

    scope = preflight.determine_android_scope(
        releases_payload=payload,
        repository=tmp_path,
        version_file=version_file.as_posix(),
        exclude_tag="v0.5.2",
    )

    assert scope == preflight.AndroidScope(
        True, preflight.AndroidVersion(26, "0.5.2"), "v0.5.1"
    )
    assert seen == [(tmp_path, "v0.5.1", version_file.as_posix())]


def test_scope_treats_no_published_apk_as_first_android_release(tmp_path):
    version_file = Path("android/BookCapture/app/build.gradle.kts")
    current_file = tmp_path / version_file
    current_file.parent.mkdir(parents=True)
    current_file.write_text(
        'versionCode = 1\nversionName = "0.1.0"\n', encoding="utf-8"
    )

    scope = preflight.determine_android_scope(
        releases_payload=[[]],
        repository=tmp_path,
        version_file=version_file.as_posix(),
        exclude_tag="v0.1.0",
    )

    assert scope.release
    assert scope.baseline_tag is None


@pytest.mark.parametrize(
    "tag",
    [
        "release-0.5.1",
        "v0.5.1:refs/heads/main",
        "v0.5.1/other",
        "v0.5.1-alpha.1-debug",
        "v0.5.1 debug",
    ],
)
def test_git_baseline_reader_rejects_tags_outside_public_tag_grammar(tmp_path, tag):
    with pytest.raises(preflight.PreflightError, match="unsafe tag|unsupported tag"):
        preflight.read_file_at_tag(tmp_path, tag, "android/build.gradle.kts")

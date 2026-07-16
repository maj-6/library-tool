"""Rights backfill must converge public artifacts before publishing a label."""
from __future__ import annotations

import backfill_rights as backfill
import pytest


@pytest.mark.parametrize(
    ("rights", "status"),
    (("searchable-only", "Search only"), ("no-public-text", "Restricted")),
)
def test_restricted_backfill_prunes_text_and_manifest_before_status(
        monkeypatch, rights, status):
    calls = []
    original_assets = {
        "about": True,
        "pages": 12,
        "translations": {"es": 12},
        "notes": 3,
        "thumbnail": True,
    }

    def rest(cfg, method, path, payload=None, prefer=""):
        calls.append((method, path, payload, prefer))
        if method == "GET" and path.startswith("volumes?"):
            return [{"slug": "work", "assets": original_assets}]
        if method == "GET":
            return []
        if method == "PATCH":
            assert payload == {
                "copyright_status": status,
                "assets": {"about": True, "thumbnail": True},
            }
            return [{"slug": "work"}]
        return None

    monkeypatch.setattr(backfill.sb, "_rest", rest)
    backfill.apply_rights({"url": "u", "key": "k"}, "work", rights)

    deleted = [path for method, path, _payload, _prefer in calls
               if method == "DELETE"]
    assert deleted == ["volume_pages?slug=eq.work", "volume_notes?slug=eq.work"]
    patch_at = next(i for i, call in enumerate(calls) if call[0] == "PATCH")
    assert all(i < patch_at for i, call in enumerate(calls)
               if call[0] in ("DELETE", "GET"))
    assert not any("volume_texts" in call[1] for call in calls)


def test_permitting_backfill_does_not_touch_public_artifacts(monkeypatch):
    calls = []

    def rest(cfg, method, path, payload=None, prefer=""):
        calls.append((method, path, payload, prefer))
        return [{"slug": "work"}] if method == "PATCH" else []

    monkeypatch.setattr(backfill.sb, "_rest", rest)
    backfill.apply_rights({"url": "u", "key": "k"}, "work", "public-domain")

    assert calls == [(
        "PATCH",
        "volumes?slug=eq.work",
        {"copyright_status": "Public domain"},
        "return=representation",
    )]


def test_restricted_backfill_never_patches_status_when_text_remains(monkeypatch):
    calls = []

    def rest(cfg, method, path, payload=None, prefer=""):
        calls.append((method, path, payload, prefer))
        if method == "GET" and path.startswith("volumes?"):
            return [{"slug": "work", "assets": {"pages": 1}}]
        if method == "GET" and path.startswith("volume_pages?"):
            return [{"slug": "work"}]
        if method == "GET":
            return []
        return None

    monkeypatch.setattr(backfill.sb, "_rest", rest)

    with pytest.raises(backfill.sb.SyncError, match="still contains public text"):
        backfill.apply_rights(
            {"url": "u", "key": "k"}, "work", "no-public-text",
        )
    assert not any(call[0] == "PATCH" for call in calls)


def test_backfill_treats_a_missing_volume_as_failure(monkeypatch):
    monkeypatch.setattr(
        backfill.sb, "_rest",
        lambda cfg, method, path, payload=None, prefer="": []
        if method == "PATCH" else None,
    )
    with pytest.raises(backfill.sb.SyncError, match="matched no unique volume"):
        backfill.apply_rights(
            {"url": "u", "key": "k"}, "missing", "public-domain",
        )


def test_main_exits_nonzero_and_does_not_report_failed_row_as_ok(
        monkeypatch, capsys):
    monkeypatch.setattr(backfill.lib, "load_json", lambda *a, **k: {
        "b1": {"status": "uploaded", "published_slug": "work",
               "rights": "no-public-text", "title": "Work"},
    })
    monkeypatch.setattr(backfill, "config", lambda: {"url": "u", "key": "k"})
    monkeypatch.setattr(
        backfill, "apply_rights",
        lambda *a, **k: (_ for _ in ()).throw(backfill.sb.SyncError("boom")),
    )
    monkeypatch.setattr(backfill.sys, "argv", ["backfill_rights.py", "--apply"])

    with pytest.raises(SystemExit, match="1 rights backfill"):
        backfill.main()
    out = capsys.readouterr().out
    assert "work: FAILED" in out
    assert "work: ok" not in out

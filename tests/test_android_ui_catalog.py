from __future__ import annotations

import base64
import json
from pathlib import Path

import android_ui_catalog as catalog
import pytest


def write_source(path: Path, *, revision=2, strings=None, icons=None) -> Path:
    path.write_text(json.dumps({
        "schema": 1,
        "revision": revision,
        "strings": strings or {"home_new_scan": "New scan"},
        "icons": icons or {},
    }), encoding="utf-8")
    return path


def test_build_wire_catalog_hashes_png_and_keeps_paths_out(tmp_path):
    png = catalog.PNG_MAGIC + b"small-test-payload"
    (tmp_path / "mark.png").write_bytes(png)
    source = write_source(
        tmp_path / "catalog.json",
        icons={"app_menu_button": {"path": "mark.png", "mime": "image/png"}},
    )

    revision, wire = catalog.build_wire_catalog(source)

    assert revision == 2
    icon = wire["icons"]["app_menu_button"]
    assert "path" not in icon
    assert base64.b64decode(icon["data"]) == png
    assert len(icon["sha256"]) == 64


@pytest.mark.parametrize("bad_key", ["Bad", "has.dot", "../escape", "a-b"])
def test_build_wire_catalog_rejects_non_resource_keys(tmp_path, bad_key):
    source = write_source(tmp_path / "catalog.json", strings={bad_key: "x"})
    with pytest.raises(catalog.CatalogError, match="invalid string key"):
        catalog.build_wire_catalog(source)


def test_lower_camel_view_ids_are_valid_remote_icon_keys(tmp_path):
    png = catalog.PNG_MAGIC + b"menu"
    (tmp_path / "menu.png").write_bytes(png)
    source = write_source(
        tmp_path / "catalog.json",
        icons={"appMenu": "menu.png"},
    )
    _, wire = catalog.build_wire_catalog(source)
    assert "appMenu" in wire["icons"]


def test_build_wire_catalog_rejects_non_png_icon(tmp_path):
    (tmp_path / "icon.svg").write_text("<svg/>", encoding="utf-8")
    source = write_source(
        tmp_path / "catalog.json",
        icons={"app_menu_button": "icon.svg"},
    )
    with pytest.raises(catalog.CatalogError, match="not a PNG"):
        catalog.build_wire_catalog(source)


def test_publish_uses_user_jwt_and_attributes_patch(monkeypatch):
    calls = []

    def rest(cfg, token, method, path, payload=None, prefer=""):
        calls.append((token, method, path, payload, prefer))
        if method == "GET":
            return [{"revision": 1}]
        return [{"revision": payload["revision"]}]

    monkeypatch.setattr(catalog.sauth, "rest", rest)
    row = catalog.publish(
        {"url": "https://example.test", "key": "public"},
        {"access_token": "user-jwt", "user_id": "user-id"},
        2,
        {"schema": 1, "strings": {}, "icons": {}},
    )

    assert row["revision"] == 2
    assert [c[1] for c in calls] == ["GET", "PATCH"]
    assert all(c[0] == "user-jwt" for c in calls)
    assert calls[1][3]["updated_by"] == "user-id"
    assert calls[1][4] == "return=representation"


def test_publish_refuses_stale_revision_before_writing(monkeypatch):
    methods = []

    def rest(_cfg, _token, method, _path, payload=None, prefer=""):
        methods.append(method)
        return [{"revision": 8}]

    monkeypatch.setattr(catalog.sauth, "rest", rest)
    with pytest.raises(catalog.CatalogError, match="not newer"):
        catalog.publish(
            {"url": "https://example.test", "key": "public"},
            {"access_token": "user-jwt", "user_id": "user-id"},
            8,
            {"schema": 1, "strings": {}, "icons": {}},
        )
    assert methods == ["GET"]


def test_check_action_needs_no_cloud_credentials(tmp_path, capsys):
    source = write_source(tmp_path / "catalog.json")
    assert catalog.main(["check", str(source)]) == 0
    assert "revision 2" in capsys.readouterr().out


def test_shipped_catalog_seeds_a_real_replaceable_app_icon():
    revision, wire = catalog.build_wire_catalog(catalog.DEFAULT_SOURCE)

    assert revision >= 3
    icon = wire["icons"]["appMenu"]
    assert base64.b64decode(icon["data"]).startswith(catalog.PNG_MAGIC)
    assert len(icon["sha256"]) == 64

"""Characterization tests for tools/libcommon.py.

Pins the CURRENT behavior of the shared helpers — slugify, gen_id, and the
JSON load/save pair — exactly as observed, so refactors (packaging, the
DATA_ROOT split) can't silently change semantics the explorer relies on.
Suspected bugs are pinned as-is with a comment; nothing here asserts what
the code *should* do, only what it does.

conftest.py points WHL_DATA_ROOT at a throwaway directory before any tools
module is imported, so importing libcommon below is safe and every path
constant resolves under the test root.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

import libcommon as lib


# --- module constants --------------------------------------------------------

def test_data_root_is_the_isolated_test_root(data_root):
    assert lib.DATA_ROOT == data_root
    # All writable-state constants hang off the isolated root, never the repo.
    assert lib.OUTPUT_DIR == data_root / "output"
    assert lib.MANUAL_ENTRIES_PATH == data_root / "output" / "manual_entries.json"
    assert lib.CLIENT_STATE_PATH == data_root / "output" / "client_state.json"
    assert lib.IA_DOWNLOADS_DIR == data_root / "downloads" / "ia"
    assert lib.IA_CATALOG_PATH == data_root / "downloads" / "ia" / "catalog.json"


def test_import_has_no_disk_side_effects(tmp_path):
    # Importing libcommon defines Path constants but creates nothing. The
    # session DATA_ROOT is shared with suites that do write, so the property
    # is only observable against a virgin root — hence the subprocess.
    env = dict(os.environ,
               WHL_DATA_ROOT=str(tmp_path),
               PYTHONPATH=str(lib.ROOT / "tools"))
    subprocess.run([sys.executable, "-c", "import libcommon"],
                   check=True, env=env)
    assert list(tmp_path.iterdir()) == []


def test_manual_entry_fields_exact_order():
    # Order is significant ("Ordered fields" per the module) — pin it exactly.
    assert lib.MANUAL_ENTRY_FIELDS == [
        "title",
        "subtitle",
        "author",
        "publisher",
        "city",
        "year",
        "edition",
        "volume",
        "language",
        "pages",
        "condition",
        "price",
        "illustrations",
        "categories",
        "notes",
        "local_pdf",
        "attention",
    ]


# --- slugify ------------------------------------------------------------------

@pytest.mark.parametrize(
    ("title", "year", "expected"),
    [
        ("Flora Rustica", 1792, "flora-rustica-1792"),
        (
            "The Herball, or Generall Historie of Plantes!",
            1597,
            "the-herball-or-generall-historie-of-plantes-1597",
        ),
        # Non-ASCII letters are NOT transliterated — the regex is ASCII-only,
        # so accented characters collapse into hyphens.
        ("Café Botánica", 1900, "caf-bot-nica-1900"),
        ("Kräuterbuch", "1543", "kr-uterbuch-1543"),
        ("", None, "volume"),
        ("---", None, "volume"),
        # `year or ''` — year=0 is falsy and dropped like None.
        ("Flora", 0, "flora"),
        ("Title", None, "title"),
        # Length cap is 60; " 2024" fell off the end of the slice.
        ("A" * 100, 2024, "a" * 60),
        # strip("-") runs BEFORE the [:60] slice, so a truncated slug can end
        # with a hyphen — current behavior, pinned as-is.
        ("x" * 59 + " tail", None, "x" * 59 + "-"),
    ],
)
def test_slugify_goldens(title, year, expected):
    assert lib.slugify(title, year) == expected


def test_slugify_without_taken_is_stateless():
    # No hidden state accumulates across taken-less calls: the second call
    # returns the identical slug rather than a -2 suffix.
    assert lib.slugify("Flora Rustica", 1792) == lib.slugify("Flora Rustica", 1792)


def test_slugify_dedup_suffixes_and_mutates_taken():
    taken: set[str] = set()
    assert lib.slugify("Flora Rustica", 1792, taken) == "flora-rustica-1792"
    assert lib.slugify("Flora Rustica", 1792, taken) == "flora-rustica-1792-2"
    assert lib.slugify("Flora Rustica", 1792, taken) == "flora-rustica-1792-3"
    assert taken == {
        "flora-rustica-1792",
        "flora-rustica-1792-2",
        "flora-rustica-1792-3",
    }


def test_slugify_dedup_applies_to_the_volume_fallback():
    taken = {"volume"}
    assert lib.slugify("", None, taken) == "volume-2"
    assert "volume-2" in taken


def test_slugify_dedup_suffix_is_not_recapped_at_60():
    # The "-2" is appended AFTER the [:60] slice, so deduped slugs can exceed
    # the cap — current behavior, pinned as-is.
    base = "x" * 60
    taken = {base}
    slug = lib.slugify("x" * 70, None, taken)
    assert slug == base + "-2"
    assert len(slug) == 62


# --- gen_id -------------------------------------------------------------------

def test_gen_id_shape():
    candidate = lib.gen_id()
    assert len(candidate) == 12
    assert set(candidate) <= set("0123456789abcdef")


def test_gen_id_empty_set_is_not_mutated():
    # Line 116 `existing = existing or set()`: an EMPTY set is falsy and gets
    # replaced by a fresh set, so the caller's set is NOT updated. Suspected
    # latent bug — pinned as-is.
    existing: set[str] = set()
    candidate = lib.gen_id(existing)
    assert candidate not in existing
    assert existing == set()


def test_gen_id_nonempty_set_is_mutated():
    existing = {"deadbeef0000"}
    candidate = lib.gen_id(existing)
    assert candidate in existing
    assert len(existing) == 2


def test_gen_id_skips_collisions(monkeypatch):
    hexes = iter(["a" * 32, "b" * 32])
    monkeypatch.setattr(
        lib.uuid, "uuid4", lambda: SimpleNamespace(hex=next(hexes))
    )
    assert lib.gen_id({"a" * 12}) == "b" * 12


# --- load_json ------------------------------------------------------------------

def test_load_json_missing_file_returns_default_object(tmp_path):
    default = {"marker": 1}
    result = lib.load_json(tmp_path / "nope.json", default)
    # The SAME object comes back, not a copy.
    assert result is default


def test_load_json_missing_parent_dir_also_returns_default(tmp_path):
    assert lib.load_json(tmp_path / "no" / "such" / "dir" / "f.json", 7) == 7


def test_load_json_accepts_str_paths_and_utf8(tmp_path):
    target = tmp_path / "data.json"
    target.write_text(json.dumps({"k": "münz"}, ensure_ascii=False), encoding="utf-8")
    assert lib.load_json(str(target), None) == {"k": "münz"}


def test_load_json_corrupt_file_raises(tmp_path):
    # Finding C21: load_json has no try/except around json.load, so a corrupt
    # file raises JSONDecodeError instead of returning the default. Pinned
    # as-is — do not "fix" here.
    target = tmp_path / "corrupt.json"
    target.write_text("{not json", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        lib.load_json(target, {"default": True})


# --- save_json ------------------------------------------------------------------

def test_save_json_round_trip_creates_deep_parents(tmp_path):
    target = tmp_path / "deep" / "nested" / "out.json"
    data = {"a": [1, 2], "u": "héllo"}
    lib.save_json(target, data)
    assert lib.load_json(target, None) == data


def test_save_json_exact_bytes_and_no_tmp_left(tmp_path):
    target = tmp_path / "out.json"
    lib.save_json(target, {"a": [1, 2], "u": "héllo"})
    # indent=2, ensure_ascii=False (raw "héllo"), no trailing newline.
    assert target.read_text(encoding="utf-8") == (
        '{\n  "a": [\n    1,\n    2\n  ],\n  "u": "héllo"\n}'
    )
    # The .tmp<pid> sidecar was consumed by os.replace.
    assert list(tmp_path.glob("*.tmp*")) == []


def test_save_json_overwrites_existing_file(tmp_path):
    target = tmp_path / "out.json"
    lib.save_json(target, {"v": 1})
    lib.save_json(target, {"v": 2})
    assert lib.load_json(target, None) == {"v": 2}


def test_save_json_retries_replace_then_succeeds(tmp_path, monkeypatch):
    # os.replace fails twice with PermissionError (Windows sharing violation),
    # then works: no data loss, tmp cleaned up, correct content.
    target = tmp_path / "out.json"
    lib.save_json(target, {"old": True})

    real_replace = os.replace
    calls = {"n": 0}

    def flaky_replace(src, dst):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise PermissionError("sharing violation")
        return real_replace(src, dst)

    sleeps: list[float] = []
    # libcommon does `import os` / `import time`, so patching the shared
    # module objects reaches its retry loop.
    monkeypatch.setattr(os, "replace", flaky_replace)
    monkeypatch.setattr(time, "sleep", sleeps.append)

    lib.save_json(target, {"new": True})

    assert calls["n"] == 3
    assert sleeps == [0.05, 0.10]  # 0.05 * (attempt + 1) backoff
    assert json.loads(target.read_text(encoding="utf-8")) == {"new": True}
    assert list(tmp_path.glob("*.tmp*")) == []


def test_save_json_falls_back_to_inplace_write_when_replace_never_succeeds(
    tmp_path, monkeypatch
):
    # The documented non-atomic degradation: after 5 failed os.replace
    # attempts, save_json rewrites the target in place rather than dropping
    # the data. It returns None and never raises for PermissionError.
    target = tmp_path / "out.json"
    lib.save_json(target, {"old": True})

    calls = {"n": 0}

    def always_fail(src, dst):
        calls["n"] += 1
        raise PermissionError("sharing violation")

    sleeps: list[float] = []
    monkeypatch.setattr(os, "replace", always_fail)
    monkeypatch.setattr(time, "sleep", sleeps.append)

    assert lib.save_json(target, {"fallback": True}) is None

    assert calls["n"] == 5  # exactly 5 attempts
    assert sleeps == pytest.approx([0.05, 0.10, 0.15, 0.20, 0.25])
    # New data landed via the plain open(path, "w") fallback...
    assert json.loads(target.read_text(encoding="utf-8")) == {"fallback": True}
    # ...and the tmp sidecar was unlinked in the finally block.
    assert list(tmp_path.glob("*.tmp*")) == []

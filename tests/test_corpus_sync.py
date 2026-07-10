"""Characterization tests for tools/corpus_sync.py's pure logic.

The corpus (photo/, books/ images) left git and syncs through R2 instead;
these pin the planning half — what gets scanned locally and what a sync
would do — without any network. The upload/download halves are r2_store
calls exercised operationally, not here.
"""
from __future__ import annotations

import corpus_sync as cs


# --- local_files: what counts as corpus ---------------------------------------

def _touch(path, size=1):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)


def test_local_files_scans_photo_and_book_images_only(tmp_path):
    _touch(tmp_path / "photo" / "IMG_1.jpg", 3)
    _touch(tmp_path / "photo" / "nested" / "IMG_2.JPG", 4)  # any file under photo/
    _touch(tmp_path / "photo" / "note.txt", 1)              # photo/ takes everything
    _touch(tmp_path / "books" / "abc123" / "1.jpg", 5)
    _touch(tmp_path / "books" / "abc123" / "cover.PNG", 6)  # suffix case-insensitive
    _touch(tmp_path / "books" / "abc123" / "transcript.txt", 7)  # tracked in git, NOT corpus
    _touch(tmp_path / "whl_catalog.csv", 8)                 # outside corpus dirs
    assert cs.local_files(tmp_path) == {
        "photo/IMG_1.jpg": 3,
        "photo/nested/IMG_2.JPG": 4,
        "photo/note.txt": 1,
        "books/abc123/1.jpg": 5,
        "books/abc123/cover.PNG": 6,
    }


def test_local_files_missing_dirs_is_empty(tmp_path):
    assert cs.local_files(tmp_path) == {}


def test_local_files_uses_posix_relpaths(tmp_path):
    _touch(tmp_path / "books" / "id" / "1.jpg")
    (rel,) = cs.local_files(tmp_path)
    assert "\\" not in rel and rel == "books/id/1.jpg"


# --- plan ----------------------------------------------------------------------

def test_plan_pushes_local_only_and_size_mismatch():
    p = cs.plan(local={"a.jpg": 10, "b.jpg": 20, "c.jpg": 30},
                remote={"b.jpg": 20, "c.jpg": 31})
    assert p == {"push": ["a.jpg", "c.jpg"], "pull": [], "same": ["b.jpg"]}


def test_plan_pulls_remote_only_and_never_deletes():
    p = cs.plan(local={"a.jpg": 10}, remote={"a.jpg": 10, "z.jpg": 5})
    # z.jpg is pulled, not deleted; a size match is "same".
    assert p == {"push": [], "pull": ["z.jpg"], "same": ["a.jpg"]}


def test_plan_empty_both_sides():
    assert cs.plan({}, {}) == {"push": [], "pull": [], "same": []}


def test_plan_output_is_sorted():
    p = cs.plan(local={"b": 1, "a": 1}, remote={"d": 1, "c": 1})
    assert p["push"] == ["a", "b"] and p["pull"] == ["c", "d"]


# --- key layout and content types ----------------------------------------------

def test_remote_prefix_is_corpus():
    assert cs.PREFIX == "corpus/"


def test_content_types():
    assert cs.content_type_for("photo/IMG.jpg") == "image/jpeg"
    assert cs.content_type_for("books/id/cover.PNG") == "image/png"
    assert cs.content_type_for("photo/note.txt") == "application/octet-stream"

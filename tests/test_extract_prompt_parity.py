"""The extraction prompt has two runners; they must stay one prompt.

`Pipeline.kt` claims its copy is "Verbatim from tools/capture_pipeline.py", but
nothing enforced that and the two silently diverged: the Kotlin copy gained
title-case and honorific rules while the Python copy still asked for "the main
title, in its original capitalization". Phone-extracted and desktop-extracted
captures therefore landed in the same catalogue under different rules.

These tests compare the RUNTIME string values, not the source text, because the
Python literal uses a backslash line continuation that never reaches the model.
"""
from __future__ import annotations

import re
from pathlib import Path

import capture_pipeline

_PIPELINE_KT = (
    Path(__file__).resolve().parents[1]
    / "android" / "BookCapture" / "app" / "src" / "main" / "java"
    / "org" / "whl" / "bookcapture" / "Pipeline.kt"
)


def _kotlin_extract_prompt() -> str:
    source = _PIPELINE_KT.read_text(encoding="utf-8")
    match = re.search(r'EXTRACT_PROMPT = """(.*?)"""', source, re.S)
    assert match, "Pipeline.kt no longer declares EXTRACT_PROMPT as a raw string"
    return match.group(1)


def test_extract_prompt_is_byte_identical_across_both_runners() -> None:
    assert capture_pipeline._EXTRACT_PROMPT == _kotlin_extract_prompt()


def test_extract_prompt_documents_every_photo_role() -> None:
    """The role tags Entries.ocrText() emits must all be explained to the model.

    A tag the prompt never defines is worse than no tag: the model sees an
    unfamiliar header and weights it arbitrarily.
    """
    prompt = capture_pipeline._EXTRACT_PROMPT
    for role in ("title_page", "cover", "spine", "other"):
        assert f"(role: {role})" in prompt, f"prompt does not document role {role!r}"


def test_extract_prompt_keeps_the_year_and_abstention_guards() -> None:
    """Two specific live defects are fixed only by prompt text, so pin them.

    A bookseller's "6/52" became a publication year, and a capture whose OCR held
    nothing but image placeholders still produced a full confident record.
    """
    prompt = capture_pipeline._EXTRACT_PROMPT
    assert "6/52" in prompt
    assert "NEVER take \"year\" from a role: other block." in prompt
    assert "image placeholders" in prompt

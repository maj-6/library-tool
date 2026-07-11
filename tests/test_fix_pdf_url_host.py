"""The pure host-rewrite / planning logic behind the R2 CORS repoint tool."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import fix_pdf_url_host as fix  # noqa: E402


def test_rewrite_swaps_host_keeps_key():
    assert fix.rewrite_host(
        "https://pub-abc.r2.dev/volumes/x.pdf", "https://files.d.org"
    ) == "https://files.d.org/volumes/x.pdf"


def test_rewrite_trims_trailing_slash_and_preserves_encoding():
    assert fix.rewrite_host(
        "https://pub-abc.r2.dev/volumes/a%20b.pdf", "https://files.d.org/"
    ) == "https://files.d.org/volumes/a%20b.pdf"


def test_plan_moves_only_stale_rows():
    rows = [
        {"slug": "a", "pdf_url": "https://pub-abc.r2.dev/volumes/a.pdf"},
        {"slug": "b", "pdf_url": ""},            # metadata-only, no scan
        {"slug": "c", "pdf_url": None},
        {"slug": "d", "pdf_url": "https://files.d.org/volumes/d.pdf"},  # already there
    ]
    assert [s for s, _o, _n in fix.plan(rows, "https://files.d.org", "")] == ["a"]


def test_plan_from_filter_restricts_to_one_host():
    rows = [{"slug": "a", "pdf_url": "https://pub-abc.r2.dev/volumes/a.pdf"}]
    assert fix.plan(rows, "https://files.d.org", "other.host") == []
    assert len(fix.plan(rows, "https://files.d.org", "pub-abc.r2.dev")) == 1

"""Framework-neutral recurring-layout proposals for the Replica engine."""
from __future__ import annotations

import copy
import json

import replica_service


def _region(role, x, y, w, h, text=""):
    return {
        "role": role,
        "box": {"x": x, "y": y, "w": w, "h": h},
        "text": text,
    }


def _recurring_page(side, jitter=0.0):
    """Mirrored outer matter makes recto and verso genuinely distinct."""
    if side == "recto":
        items = [
            _region("header", 0.14 + jitter, 0.05, 0.56, 0.04),
            _region("body", 0.13 + jitter, 0.15, 0.61, 0.70),
            _region("marginalia", 0.78 + jitter, 0.28, 0.13, 0.25),
            _region("page-number", 0.84 + jitter, 0.93, 0.04, 0.025),
        ]
    else:
        items = [
            _region("header", 0.30 + jitter, 0.05, 0.56, 0.04),
            _region("body", 0.26 + jitter, 0.15, 0.61, 0.70),
            _region("marginalia", 0.08 + jitter, 0.28, 0.13, 0.25),
            _region("page-number", 0.12 + jitter, 0.93, 0.04, 0.025),
        ]
    return {
        "doc": "compiled.txt",
        "dims": {"w": 1000, "h": 1500, "dpi": 300},
        "items": items,
    }


def _book_pages():
    return {
        1: _recurring_page("recto"),
        2: _recurring_page("verso"),
        3: _recurring_page("recto", 0.004),
        4: _recurring_page("verso", -0.004),
        5: {
            "doc": "compiled.txt",
            "dims": {"w": 1000, "h": 1500, "dpi": 300},
            "items": [
                _region("title", 0.10, 0.15, 0.80, 0.10),
                _region("figure", 0.20, 0.35, 0.60, 0.40),
                _region("caption", 0.30, 0.80, 0.40, 0.04),
            ],
        },
    }


def test_proposes_noisy_recto_verso_families_and_outlier():
    proposal = replica_service.propose_layout_families(_book_pages())

    assert proposal["status"] == "proposal"
    assert proposal["canonical"] is False
    assert proposal["input_revision"].startswith("lfi-")
    assert [family["member_pages"] for family in proposal["families"]] == [
        [1, 3], [2, 4]
    ]
    assert [family["representative_page"] for family in proposal["families"]] == [1, 2]
    assert len({family["family_id"] for family in proposal["families"]}) == 2

    for family in proposal["families"]:
        assert family["family_id"].startswith("layout-")
        assert family["confidence"] > 0.8
        assert "recurring-layout" in family["reasons"]
        assert len(family["members"]) == 2
        for member in family["members"]:
            assert member["similarity"] > 0.9
            assert member["confidence"] > 0.8
            assert member["reasons"]
            assert member["source_revision"].startswith("lpr-")

    assert len(proposal["exceptions"]) == 1
    exception = proposal["exceptions"][0]
    assert exception["page"] == 5
    assert exception["reasons"] == ["singleton-layout"]
    assert exception["nearest_family_id"] in {
        family["family_id"] for family in proposal["families"]
    }
    assert exception["confidence"] < 0.5
    json.dumps(proposal)  # provider-neutral output is ordinary JSON data


def test_proposal_is_deterministic_and_does_not_mutate_input():
    pages = _book_pages()
    untouched = copy.deepcopy(pages)

    first = replica_service.propose_layout_families(pages)
    second = replica_service.propose_layout_families(
        dict(reversed(list(copy.deepcopy(pages).items())))
    )

    assert pages == untouched
    assert first == second
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_empty_and_single_page_are_reviewable_proposals_not_assignments():
    empty = replica_service.propose_layout_families({})
    assert empty["page_count"] == 0
    assert empty["families"] == []
    assert empty["exceptions"] == []

    single = replica_service.propose_layout_families({9: _recurring_page("recto")})
    assert single["page_count"] == 1
    assert single["families"] == []
    assert single["exceptions"][0]["page"] == 9
    assert single["exceptions"][0]["reasons"] == ["singleton-layout"]
    assert single["exceptions"][0]["similarity"] == 0.0

    no_regions = replica_service.propose_layout_families({10: {"items": []}})
    assert no_regions["exceptions"][0]["reasons"] == ["no-usable-regions"]


def test_average_linkage_scaling_is_quadratic_not_recomputed_cross_products():
    """Operation counts catch the former cubic merge loop without a stopwatch."""
    page_count = 300
    pages = list(range(1, page_count + 1))
    pair_metrics = {
        (left, right): {"score": 0.95}
        for left_index, left in enumerate(pages)
        for right in pages[left_index + 1:]
    }
    stats = {}

    clusters = replica_service._average_link_clusters(
        pages, pair_metrics, 0.78, stats
    )

    assert clusters == [tuple(pages)]
    assert stats["initial_pairs"] == page_count * (page_count - 1) // 2
    assert stats["linkage_updates"] == (page_count - 1) * (page_count - 2) // 2
    assert stats["heap_pushes"] == (page_count - 1) ** 2
    assert stats["heap_pops"] <= stats["heap_pushes"]

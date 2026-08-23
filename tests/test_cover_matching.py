from __future__ import annotations

import json

import pytest
from PIL import Image, ImageDraw, ImageEnhance

import cover_matching as covers


def _herbal_cover() -> Image.Image:
    image = Image.new("RGB", (360, 520), (34, 88, 57))
    draw = ImageDraw.Draw(image)
    draw.rectangle((32, 28, 328, 492), outline=(235, 210, 121), width=12)
    draw.rectangle((62, 72, 298, 198), fill=(219, 188, 92))
    draw.rectangle((82, 94, 278, 110), fill=(45, 63, 48))
    draw.rectangle((104, 126, 256, 140), fill=(45, 63, 48))
    draw.ellipse((112, 238, 248, 374), fill=(151, 43, 38), outline=(242, 218, 136), width=8)
    draw.line((180, 222, 180, 410), fill=(231, 220, 160), width=7)
    for offset in (0, 38, 76):
        draw.polygon(
            ((180, 258 + offset), (128, 230 + offset), (144, 282 + offset)),
            fill=(91, 155, 78),
        )
        draw.polygon(
            ((180, 278 + offset), (232, 248 + offset), (216, 300 + offset)),
            fill=(112, 176, 91),
        )
    draw.rectangle((96, 440, 264, 452), fill=(230, 208, 129))
    return image


def _different_cover() -> Image.Image:
    image = Image.new("RGB", (360, 520), (72, 35, 108))
    draw = ImageDraw.Draw(image)
    draw.rectangle((20, 20, 340, 500), outline=(235, 105, 36), width=10)
    for x in range(44, 330, 52):
        draw.rectangle((x, 52, x + 22, 462), fill=(36, 164, 184))
    draw.polygon(((50, 390), (180, 90), (310, 390)), fill=(245, 133, 45))
    draw.ellipse((125, 330, 235, 440), fill=(43, 33, 84))
    return image


def _changed_exposure(image: Image.Image, brightness: float, contrast: float) -> Image.Image:
    exposed = ImageEnhance.Brightness(image).enhance(brightness)
    result = ImageEnhance.Contrast(exposed).enhance(contrast)
    exposed.close()
    return result


def _gamma_exposure(image: Image.Image, gamma: float) -> Image.Image:
    lookup = [round(255 * ((value / 255) ** gamma)) for value in range(256)] * 3
    return image.point(lookup)


def test_signature_is_deterministic_bounded_and_contains_no_pixels():
    image = _herbal_cover()
    first = covers.build_visual_signature(image)
    second = covers.build_visual_signature(image)

    assert first == second
    assert image.getpixel((0, 0)) == (34, 88, 57)  # caller-owned image remains open
    assert set(first) == {
        "version",
        "algorithm",
        "aspect_milli",
        "hue_hist",
        "chroma_hist",
        "chroma_grid",
        "tone_grid",
        "edge_grid",
        "gradient_hist",
        "dhash",
    }
    assert len(first["hue_hist"]) == 12
    assert len(first["chroma_hist"]) == 16
    assert len(first["chroma_grid"]) == 144
    assert len(first["tone_grid"]) == len(first["edge_grid"]) == 48
    assert len(first["gradient_hist"]) == 8
    assert sum(first["hue_hist"]) == sum(first["chroma_hist"]) == 255
    encoded = covers.serialize_visual_signature(first)
    assert len(encoded.encode()) < covers.SIGNATURE_MAX_JSON_BYTES
    assert covers.parse_visual_signature(encoded) == first
    assert all(word not in encoded for word in ("data:image", "base64", "thumbnail", "path"))


@pytest.mark.parametrize("brightness,contrast", [(0.48, 1.15), (1.55, 0.88)])
def test_visual_similarity_survives_large_exposure_changes(brightness, contrast):
    original = _herbal_cover()
    exposed = _changed_exposure(original, brightness, contrast)

    compared = covers.compare_visual_signatures(
        covers.build_visual_signature(original),
        covers.build_visual_signature(exposed),
    )

    assert compared["color"] >= 0.80
    assert compared["structure"] >= 0.80
    assert compared["gradient"] >= 0.72
    assert compared["visual"] >= 0.80
    assert all(0.0 <= score <= 1.0 for score in compared.values())


@pytest.mark.parametrize("gamma", [0.55, 1.8])
def test_visual_similarity_survives_nonlinear_camera_exposure(gamma):
    original = _herbal_cover()
    exposed = _gamma_exposure(original, gamma)

    compared = covers.compare_visual_signatures(
        covers.build_visual_signature(original),
        covers.build_visual_signature(exposed),
    )

    assert compared["color"] >= 0.80
    assert compared["structure"] >= 0.90
    assert compared["visual"] >= 0.88


def test_structurally_and_chromatically_different_cover_does_not_corroborate():
    compared = covers.compare_visual_signatures(
        covers.build_visual_signature(_herbal_cover()),
        covers.build_visual_signature(_different_cover()),
    )

    assert compared["color"] < 0.72
    assert compared["structure"] < 0.78
    assert compared["visual"] < 0.72
    assert all(0.0 <= score <= 1.0 for score in compared.values())


def test_ranker_uses_visual_evidence_to_break_an_identical_title_tie():
    original = _herbal_cover()
    dark = _changed_exposure(original, 0.5, 1.1)
    ranked = covers.rank_cover_matches(
        query_ocr_text="THE PRACTICAL HERBAL by A. Green",
        query_image=dark,
        candidates=[
            {
                "capture_id": "wrong",
                "title": "The Practical Herbal",
                "cover_image": _different_cover(),
            },
            {
                "capture_id": "right",
                "title": "The Practical Herbal",
                "cover_image": original,
            },
        ],
    )

    assert [item["candidate_capture_id"] for item in ranked] == ["right", "wrong"]
    assert ranked[0]["match_confidence"] >= 0.82
    assert ranked[0]["match_confidence"] - ranked[1]["match_confidence"] >= 0.10
    assert ranked[1]["match_confidence"] <= 0.72
    assert ranked[1]["match_evidence"]["band"] == "review"
    assert set(ranked[0]["match_evidence"]["components"]) == {
        "text",
        "color",
        "structure",
        "gradient",
        "aspect",
        "visual",
    }
    assert ranked[0]["match_evidence"]["band"] == "likely"
    assert any("color" in reason.lower() for reason in ranked[0]["match_evidence"]["reasons"])


def test_visual_only_queue_row_is_supported_but_capped_for_review():
    signature = covers.build_visual_signature(_herbal_cover())
    confidence, evidence = covers.score_cover_match(
        query_signature=signature,
        candidate_signature=signature,
    )

    assert confidence == 0.88
    assert evidence["components"]["text"] is None
    assert evidence["band"] == "likely"
    assert "visual-only" in evidence["reasons"][0]


def test_candidate_without_cover_remains_available_as_capped_text_only_result():
    ranked = covers.rank_cover_matches(
        query_ocr_text="THE PRACTICAL HERBAL A. GREEN",
        query_image=_herbal_cover(),
        candidates=[{"capture_id": "legacy", "title": "The Practical Herbal"}],
    )

    assert ranked[0]["candidate_capture_id"] == "legacy"
    assert ranked[0]["match_confidence"] == 0.70
    assert ranked[0]["match_evidence"]["band"] == "review"
    assert set(ranked[0]["match_evidence"]["components"]) == {"text"}


def test_inventory_author_and_year_disambiguate_identical_titles():
    query = "THE PRACTICAL HERBAL by A BOTANIST published 1812"
    correct = covers.text_match_score(
        query,
        "The Practical Herbal",
        candidate_author="A. Botanist",
        candidate_year="1812",
    )
    wrong = covers.text_match_score(
        query,
        "The Practical Herbal",
        candidate_author="B. Writer",
        candidate_year="1901",
    )

    assert correct == 1.0
    assert wrong == 0.82
    confidence, evidence = covers.score_cover_match(
        query_ocr_text=query,
        candidate_title="The Practical Herbal",
        candidate_author="A. Botanist",
        candidate_year="1812",
    )
    assert confidence == 0.70
    assert evidence["text_evidence"] == {
        "title": 1.0,
        "candidate_ocr": None,
        "author": 1.0,
        "year": 1.0,
    }


def test_session_ranker_combines_title_ocr_and_cover_views_but_not_title_page_visual():
    cover = _herbal_cover()
    cover_signature = covers.build_visual_signature(cover)
    dark_signature = covers.build_visual_signature(_changed_exposure(cover, 0.46, 1.2))
    unrelated_signature = covers.build_visual_signature(_different_cover())
    rows = [
        {
            "session_id": "session-7",
            "photo_role": "cover",
            "ocr_text": "PRACTICAL",
            "visual_signature": dark_signature,
        },
        {
            "session_id": "session-7",
            "photo_role": "cover",
            "ocr_text": "",
            "visual_signature": cover_signature,
        },
        {
            "session_id": "session-7",
            "photo_role": "title_page",
            "ocr_text": "THE PRACTICAL HERBAL A. GREEN",
            # This must not depress visual evidence: title pages differ from covers.
            "visual_signature": unrelated_signature,
        },
    ]
    ranked = covers.rank_cover_session_matches(
        rows=rows,
        candidates=[
            {
                "capture_id": "other",
                "title": "The Practical Herbal",
                "visual_signature": unrelated_signature,
            },
            {
                "capture_id": "book-7",
                "title": "The Practical Herbal",
                "visual_signatures": [cover_signature],
            },
        ],
    )

    proposal = ranked[0]
    assert proposal["session_id"] == "session-7"
    assert proposal["candidate_capture_id"] == "book-7"
    assert proposal["match_confidence"] >= 0.82
    assert proposal["match_evidence"]["session"] == {
        "row_count": 3,
        "ocr_observation_count": 2,
        "cover_signature_count": 2,
        "candidate_signature_count": 1,
        "visual_comparison_count": 2,
    }


def test_session_accepts_empty_ocr_when_cover_signature_is_valid():
    signature = covers.build_visual_signature(_herbal_cover())
    ranked = covers.rank_cover_session_matches(
        rows=[
            {
                "session_id": "visual-only-session",
                "photo_role": "cover",
                "ocr_text": "",
                "visual_signature": signature,
            }
        ],
        candidates=[
            {
                "capture_id": "visual-candidate",
                "title": "",
                "visual_signature": signature,
            }
        ],
    )

    assert ranked[0]["match_confidence"] == 0.88
    assert ranked[0]["match_evidence"]["components"]["text"] is None


def test_session_tie_is_capped_and_exposes_bounded_ambiguity_even_at_limit_one():
    signature = covers.build_visual_signature(_herbal_cover())
    ranked = covers.rank_cover_session_matches(
        rows=[
            {
                "session_id": "ambiguous-session",
                "photo_role": "cover",
                "ocr_text": "THE PRACTICAL HERBAL A BOTANIST 1812",
                "visual_signature": signature,
            }
        ],
        candidates=[
            {
                "capture_id": "candidate-b",
                "title": "The Practical Herbal",
                "author": "A. Botanist",
                "year": "1812",
                "visual_signature": signature,
            },
            {
                "capture_id": "candidate-a",
                "title": "The Practical Herbal",
                "author": "A. Botanist",
                "year": "1812",
                "visual_signature": signature,
            },
        ],
        limit=1,
    )

    top = ranked[0]
    assert top["candidate_capture_id"] == "candidate-a"
    assert top["match_confidence"] == covers.AMBIGUOUS_CONFIDENCE_CAP
    assert top["match_evidence"]["band"] == "review"
    assert top["match_evidence"]["ambiguity"] == {
        "ambiguous": True,
        "margin": 0.0,
        "threshold": covers.AMBIGUITY_MARGIN_THRESHOLD,
        "uncapped_top_confidence": 1.0,
        "runner_up_candidate_id": "candidate-b",
        "runner_up_confidence": 1.0,
        "close_candidate_count": 2,
    }
    assert len(json.dumps(top["match_evidence"]["ambiguity"]).encode()) < 512
    assert len(top["match_evidence"]["reasons"]) <= 4
    assert "ambiguity margin" in top["match_evidence"]["reasons"][-1]


def test_signature_parser_rejects_wrong_shape_version_and_oversized_unknown_data():
    signature = covers.build_visual_signature(_herbal_cover())
    wrong_shape = dict(signature, tone_grid=signature["tone_grid"][:-1])
    wrong_version = dict(signature, version=2)
    oversized = dict(signature, raw_photo="x" * covers.SIGNATURE_MAX_JSON_BYTES)

    with pytest.raises(covers.CoverSignatureError, match="exactly 48"):
        covers.parse_visual_signature(wrong_shape)
    with pytest.raises(covers.CoverSignatureError, match="unsupported cover signature version"):
        covers.parse_visual_signature(wrong_version)
    with pytest.raises(covers.CoverSignatureError, match="serialized-size limit"):
        covers.parse_visual_signature(json.dumps(oversized))


def test_pillow_decompression_bomb_is_normalized(monkeypatch):
    def bomb(_source):
        raise covers.Image.DecompressionBombError("hostile dimensions")

    monkeypatch.setattr(covers.Image, "open", bomb)
    with pytest.raises(covers.CoverSignatureError, match="pixel safety limit"):
        covers.build_visual_signature(b"not-an-image")


def test_session_rows_must_share_an_identity():
    rows = [
        {"session_id": "one", "photo_role": "cover", "ocr_text": "Herbal"},
        {"session_id": "two", "photo_role": "title_page", "ocr_text": "Herbal"},
    ]
    with pytest.raises(ValueError, match="share one"):
        covers.rank_cover_session_matches(
            rows=rows,
            candidates=[{"capture_id": "candidate", "title": "Herbal"}],
        )

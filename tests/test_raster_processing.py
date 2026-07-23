from __future__ import annotations

import hashlib
import io
import json
import math
import random

import pytest
from PIL import Image

import librarytool.processing.raster as raster_module
from librarytool.processing.raster import (
    EXIF_ORIENTED_NORMALIZED,
    ManualBinaryAdjustRecipe,
    PageBoundaryProposal,
    RasterInputError,
    RasterLimits,
    apply_manual_binary_adjust,
    apply_perspective_transform,
    page_boundary_proposal_from_pixel_quad,
    validate_normalized_quad,
)


FULL_FRAME = ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0))


def _png(image: Image.Image) -> bytes:
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=False, compress_level=9)
    return output.getvalue()


def _gradient(width: int = 256, height: int = 8) -> Image.Image:
    row = list(range(256))
    if width != 256:
        row = [round(index * 255 / (width - 1)) for index in range(width)]
    return Image.frombytes("L", (width, height), bytes(row * height))


def _map_homography(matrix: tuple[float, ...], point: tuple[float, float]) -> tuple[float, float]:
    x, y = point
    denominator = matrix[6] * x + matrix[7] * y + matrix[8]
    return (
        (matrix[0] * x + matrix[1] * y + matrix[2]) / denominator,
        (matrix[3] * x + matrix[4] * y + matrix[5]) / denominator,
    )


def _invert_homography(matrix: tuple[float, ...]) -> tuple[float, ...]:
    a, b, c, d, e, f, g, h, i = matrix
    determinant = a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)
    return tuple(
        value / determinant
        for value in (
            e * i - f * h,
            c * h - b * i,
            b * f - c * e,
            f * g - d * i,
            a * i - c * g,
            c * d - a * f,
            d * h - e * g,
            b * g - a * h,
            a * e - b * d,
        )
    )


def test_quad_validation_preserves_order_and_accepts_normalized_bounds() -> None:
    quad = ((0.08, 0.12), (0.91, 0.08), (0.86, 0.94), (0.12, 0.89))

    assert validate_normalized_quad(quad) == quad


@pytest.mark.parametrize(
    ("quad", "message"),
    [
        (((0, 0), (1, 0), (1, 1)), "exactly four"),
        (((0, 0), (1, 0), (1, 1), (math.nan, 1)), "finite"),
        (((0, 0), (1.01, 0), (1, 1), (0, 1)), "bounds"),
        (((0, 0), (1, 1), (1, 0), (0, 1)), "self-intersect"),
        (((0, 0), (1, 0), (0.4, 0.2), (0, 1)), "convex"),
        (((0, 1), (1, 1), (1, 0), (0, 0)), "TL/TR/BR/BL"),
        (((0.1, 0.1), (0.1004, 0.1), (0.1004, 0.1004), (0.1, 0.1004)), "too small"),
    ],
)
def test_quad_validation_rejects_unsafe_geometry(
    quad: tuple[tuple[float, float], ...],
    message: str,
) -> None:
    with pytest.raises(RasterInputError, match=message):
        validate_normalized_quad(quad)


def test_page_boundary_proposal_is_revision_pinned_and_json_safe() -> None:
    proposal = PageBoundaryProposal(
        quad=((0.08, 0.12), (0.91, 0.08), (0.86, 0.94), (0.12, 0.89)),
        confidence=0.875,
        detector="contour",
        detector_version="2.1.0",
        source_revision="raster-revision-17",
    )

    payload = proposal.as_dict()
    assert payload["coordinate_space"] == EXIF_ORIENTED_NORMALIZED
    assert payload["point_order"] == ["top_left", "top_right", "bottom_right", "bottom_left"]
    assert payload["source_revision"] == "raster-revision-17"
    assert proposal.points == proposal.quad
    json.dumps(payload, allow_nan=False)


@pytest.mark.parametrize("confidence", [-0.01, 1.01, math.inf, math.nan])
def test_page_boundary_proposal_rejects_invalid_confidence(confidence: float) -> None:
    with pytest.raises(ValueError, match="confidence"):
        PageBoundaryProposal(
            quad=FULL_FRAME,
            confidence=confidence,
            detector="test",
            detector_version="1",
            source_revision="revision",
        )


def test_pixel_quad_proposal_conversion_round_trips_randomized_valid_geometry() -> None:
    rng = random.Random(229)
    for index in range(100):
        width = rng.randint(32, 6000)
        height = rng.randint(32, 6000)
        left = rng.uniform(0.08, 0.18)
        right = rng.uniform(0.82, 0.92)
        top = rng.uniform(0.02, 0.18)
        bottom = rng.uniform(0.82, 0.98)
        skew_x = rng.uniform(-0.03, 0.03)
        skew_y = rng.uniform(-0.03, 0.03)
        normalized = (
            (left, top),
            (right, top + skew_y),
            (right + skew_x, bottom),
            (left + skew_x, bottom - skew_y),
        )
        pixels = tuple(
            (x * (width - 1), y * (height - 1))
            for x, y in normalized
        )

        proposal = page_boundary_proposal_from_pixel_quad(
            pixels,
            source_width=width,
            source_height=height,
            confidence=index / 100,
            detector="property-test",
            detector_version="1",
            source_revision=f"source:{index}",
        )

        for actual, expected in zip(proposal.quad, normalized, strict=True):
            assert actual == pytest.approx(expected, abs=1e-12)
        assert proposal.confidence == index / 100
        assert proposal.as_dict() == proposal.as_dict()


@pytest.mark.parametrize(
    ("quad", "width", "height", "message"),
    [
        (FULL_FRAME, 1, 100, "source_width"),
        (FULL_FRAME, 100, 1, "source_height"),
        (((0, 0), (100, 0), (99, 99), (0, 99)), 100, 100, "pixel bounds"),
        (((0, 0), (99, False), (99, 99), (0, 99)), 100, 100, "numbers"),
    ],
)
def test_pixel_quad_proposal_conversion_rejects_invalid_pixel_contracts(
    quad: tuple[tuple[float, float], ...],
    width: int,
    height: int,
    message: str,
) -> None:
    with pytest.raises((RasterInputError, ValueError), match=message):
        page_boundary_proposal_from_pixel_quad(
            quad,
            source_width=width,
            source_height=height,
            confidence=0.5,
            detector="test",
            detector_version="1",
            source_revision="source",
        )


def test_full_frame_transform_is_lossless_sized_deterministic_and_pure() -> None:
    image = Image.new("RGB", (64, 48))
    pixels = image.load()
    for y in range(image.height):
        for x in range(image.width):
            pixels[x, y] = (x * 4, y * 5, (x + y) * 2)
    mutable_source = bytearray(_png(image))
    source_before = bytes(mutable_source)

    first = apply_perspective_transform(
        mutable_source,
        FULL_FRAME,
        source_revision="capture:7",
    )
    second = apply_perspective_transform(
        mutable_source,
        FULL_FRAME,
        source_revision="capture:7",
    )

    assert bytes(mutable_source) == source_before
    assert (first.source_width, first.source_height) == (64, 48)
    assert (first.output_width, first.output_height) == (64, 48)
    assert first.output_png == second.output_png
    assert first.manifest_bytes == second.manifest_bytes
    assert first.source_sha256 == hashlib.sha256(source_before).hexdigest()
    assert first.output_sha256 == hashlib.sha256(first.output_png).hexdigest()
    assert first.manifest_sha256 == hashlib.sha256(first.manifest_bytes).hexdigest()
    assert first.transform_manifest["source_revision"] == "capture:7"
    assert first.transform_manifest["output_dimensions"] == [64, 48]
    with Image.open(io.BytesIO(first.output_png)) as output:
        assert output.convert("RGB").tobytes() == image.tobytes()


def test_explicit_trapezoid_has_deterministic_dimensions_and_normalized_homography() -> None:
    image = Image.new("RGB", (160, 120))
    pixels = image.load()
    for y in range(image.height):
        for x in range(image.width):
            pixels[x, y] = (
                round(x * 255 / (image.width - 1)),
                round(y * 255 / (image.height - 1)),
                0,
            )
    source = _png(image)
    quad = ((0.15, 0.10), (0.85, 0.18), (0.90, 0.90), (0.10, 0.82))

    result = apply_perspective_transform(source, quad)

    assert (result.output_width, result.output_height) == (121, 87)
    destinations = ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0))
    for source_point, expected in zip(quad, destinations, strict=True):
        assert _map_homography(result.source_to_output_homography, source_point) == pytest.approx(
            expected, abs=1e-10
        )
    manifest = result.transform_manifest
    assert tuple(manifest["source_to_output_homography"]) == (
        result.source_to_output_homography
    )
    assert manifest["quad"] == [list(point) for point in quad]
    assert manifest["mapping_convention"].startswith("row-major 3x3")
    assert manifest["output"]["media_type"] == "image/png"
    assert manifest["processor"]["name"] == "librarytool.processing.raster"
    assert manifest["processor"]["algorithm_version"] == "1.0.0"
    assert manifest["processor"]["pillow_version"]
    output_x, output_y = result.output_width // 2, result.output_height // 2
    output_normalized = (
        output_x / (result.output_width - 1),
        output_y / (result.output_height - 1),
    )
    source_normalized = _map_homography(
        _invert_homography(result.source_to_output_homography),
        output_normalized,
    )
    with Image.open(io.BytesIO(result.output_png)) as output:
        actual = output.convert("RGB").getpixel((output_x, output_y))
    assert actual[0] == pytest.approx(source_normalized[0] * 255, abs=2)
    assert actual[1] == pytest.approx(source_normalized[1] * 255, abs=2)

    replay_manifest = json.loads(result.manifest_bytes)
    replay = apply_perspective_transform(
        source,
        replay_manifest["quad"],
        source_revision=replay_manifest["source_revision"],
    )
    assert replay.output_png == result.output_png
    assert replay.manifest_bytes == result.manifest_bytes
    inverse = _invert_homography(result.source_to_output_homography)
    for expected, destination in zip(quad, destinations, strict=True):
        assert _map_homography(inverse, destination) == pytest.approx(expected, abs=1e-10)


def test_output_dimensions_are_bounded_without_changing_source() -> None:
    source = _png(Image.new("RGB", (200, 100), (12, 34, 56)))

    result = apply_perspective_transform(
        source,
        FULL_FRAME,
        limits=RasterLimits(max_output_edge_px=50, max_output_megapixels=1),
    )

    assert (result.output_width, result.output_height) == (50, 25)
    assert result.transform_manifest["output_was_bounded"] is True
    assert hashlib.sha256(source).hexdigest() == result.source_sha256


def test_destination_smaller_than_two_decoded_pixels_is_rejected() -> None:
    source = _png(Image.new("RGB", (32, 32), (12, 34, 56)))
    subpixel_width_quad = ((0.10, 0.10), (0.11, 0.10), (0.11, 0.12), (0.10, 0.12))

    with pytest.raises(RasterInputError, match="span below 1 pixel"):
        apply_perspective_transform(source, subpixel_width_quad)


def test_unrounded_subpixel_span_is_rejected_before_half_up_rounding() -> None:
    source = _png(Image.new("RGB", (100, 100), (12, 34, 56)))
    reviewer_reproduction = (
        (0.10, 0.10),
        (0.106, 0.10),
        (0.106, 0.12),
        (0.10, 0.12),
    )

    with pytest.raises(RasterInputError, match="span below 1 pixel"):
        apply_perspective_transform(source, reviewer_reproduction)


def test_mutable_source_size_is_checked_before_copying() -> None:
    class CopyGuard(bytearray):
        def __bytes__(self) -> bytes:
            raise AssertionError("oversized mutable input was copied")

    source = CopyGuard(b"12345")
    with pytest.raises(RasterInputError, match="byte limit"):
        apply_perspective_transform(
            source,
            FULL_FRAME,
            limits=RasterLimits(max_source_bytes=4),
        )


def test_raw_dimensions_are_bounded_before_exif_transpose_or_pixel_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _png(Image.new("RGB", (32, 16), (12, 34, 56)))

    def unexpected_transpose(_image: Image.Image) -> Image.Image:
        raise AssertionError("oversized source reached EXIF transpose")

    monkeypatch.setattr(raster_module.ImageOps, "exif_transpose", unexpected_transpose)
    with pytest.raises(RasterInputError, match="source edge"):
        apply_perspective_transform(
            source,
            FULL_FRAME,
            limits=RasterLimits(max_source_edge_px=31),
        )


def test_quad_coordinates_are_relative_to_the_exif_oriented_source() -> None:
    image = Image.new("RGB", (80, 40), (180, 140, 90))
    exif = Image.Exif()
    exif[274] = 6
    encoded = io.BytesIO()
    image.save(encoded, format="JPEG", quality=95, exif=exif)

    result = apply_perspective_transform(encoded.getvalue(), FULL_FRAME)

    assert (result.source_width, result.source_height) == (40, 80)
    assert (result.output_width, result.output_height) == (40, 80)
    assert result.transform_manifest["raw_source_dimensions"] == [80, 40]
    assert result.transform_manifest["source_dimensions"] == [40, 80]
    assert result.transform_manifest["exif_orientation"] == 6


def test_manual_contrast_100_produces_only_black_and_white_pixels() -> None:
    source = _png(_gradient())
    recipe = ManualBinaryAdjustRecipe(contrast=100, brightness=0)

    result = apply_perspective_transform(source, FULL_FRAME, adjustment=recipe)
    with Image.open(io.BytesIO(result.output_png)) as output:
        values = set(output.convert("L").tobytes())

    assert values == {0, 255}
    assert result.transform_manifest["adjustment"]["schema"] == (
        "org.whl.raster.manual-binary-adjust"
    )
    assert result.transform_manifest["adjustment"]["threshold"] == 128


def test_brightness_lowers_a_clamped_threshold_and_monotonically_lightens() -> None:
    image = _gradient()
    recipes = [
        ManualBinaryAdjustRecipe(brightness=-100),
        ManualBinaryAdjustRecipe(brightness=-50),
        ManualBinaryAdjustRecipe(brightness=0),
        ManualBinaryAdjustRecipe(brightness=50),
        ManualBinaryAdjustRecipe(brightness=100),
    ]

    thresholds = [recipe.threshold for recipe in recipes]
    white_counts = [
        apply_manual_binary_adjust(image, recipe).tobytes().count(255)
        for recipe in recipes
    ]

    assert thresholds == [255, 191, 128, 64, 0]
    assert thresholds == sorted(thresholds, reverse=True)
    assert white_counts == sorted(white_counts)


def test_manual_adjust_composites_transparent_black_on_white_like_transform() -> None:
    image = Image.new("RGBA", (2, 2), (0, 0, 0, 0))
    image.putpixel((1, 1), (0, 0, 0, 255))
    recipe = ManualBinaryAdjustRecipe()

    direct = apply_manual_binary_adjust(image, recipe)
    transformed = apply_perspective_transform(_png(image), FULL_FRAME, adjustment=recipe)
    with Image.open(io.BytesIO(transformed.output_png)) as output:
        transform_pixels = output.convert("L").tobytes()

    assert direct.getpixel((0, 0)) == 255
    assert direct.getpixel((1, 1)) == 0
    assert transform_pixels == direct.tobytes()


def test_partial_manual_contrast_is_not_the_existing_color_normalization_contract() -> None:
    image = _gradient(width=32, height=1)
    untouched = apply_manual_binary_adjust(
        image,
        ManualBinaryAdjustRecipe(contrast=0, brightness=75),
    )
    binary = apply_manual_binary_adjust(
        image,
        ManualBinaryAdjustRecipe(contrast=100, brightness=75),
    )

    assert len(set(untouched.tobytes())) > 2
    assert set(binary.tobytes()) <= {0, 255}


def test_invalid_source_is_an_input_error() -> None:
    with pytest.raises(RasterInputError, match="cannot be decoded"):
        apply_perspective_transform(b"not an image", FULL_FRAME)

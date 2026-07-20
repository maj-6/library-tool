from __future__ import annotations

import hashlib
import importlib
import io
import json
import sys
from pathlib import Path

import pytest


cv2 = pytest.importorskip("cv2", reason="image-processor tests require OpenCV")
np = pytest.importorskip("numpy", reason="image-processor tests require NumPy")
pytest.importorskip("PIL", reason="image-processor tests require Pillow")
Image = importlib.import_module("PIL.Image")


SERVICE_ROOT = Path(__file__).resolve().parents[1]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

pipeline = importlib.import_module("whl_image_processor.pipeline")
PermanentImageInputError = pipeline.PermanentImageInputError
ProcessingOptions = pipeline.ProcessingOptions
process_image = pipeline.process_image


def _jpeg(rgb: np.ndarray, *, orientation: int | None = None) -> bytes:
    output = io.BytesIO()
    image = Image.fromarray(rgb.astype(np.uint8), mode="RGB")
    kwargs: dict[str, object] = {
        "format": "JPEG",
        "quality": 96,
        "subsampling": 0,
        "optimize": False,
    }
    if orientation is not None:
        exif = Image.Exif()
        exif[274] = orientation
        kwargs["exif"] = exif
    image.save(output, **kwargs)
    return output.getvalue()


def _synthetic_page(*, low_contrast: bool = False) -> bytes:
    page_height, page_width = 360, 250
    if low_contrast:
        paper = np.array([205, 195, 164], dtype=np.uint8)
        ink = (157, 149, 132)
    else:
        paper = np.array([236, 220, 177], dtype=np.uint8)
        ink = (45, 44, 38)
    page = np.empty((page_height, page_width, 3), dtype=np.uint8)
    page[:] = paper
    cv2.rectangle(page, (2, 2), (page_width - 3, page_height - 3), ink, 2)
    cv2.putText(page, "HERBARIUM", (34, 55), cv2.FONT_HERSHEY_SIMPLEX, 0.72, ink, 2)
    cv2.putText(page, "A. Botanist", (48, 88), cv2.FONT_HERSHEY_SIMPLEX, 0.52, ink, 1)
    for y, length in zip(range(125, 320, 24), (174, 192, 158, 186, 169, 194, 148, 181)):
        cv2.line(page, (30, y), (30 + length, y), ink, 3)

    canvas_height, canvas_width = 480, 520
    canvas = np.empty((canvas_height, canvas_width, 3), dtype=np.uint8)
    canvas[:] = (28, 37, 31)
    source = np.array(
        [[0, 0], [page_width - 1, 0], [page_width - 1, page_height - 1], [0, page_height - 1]],
        dtype=np.float32,
    )
    destination = np.array([[123, 45], [397, 80], [421, 432], [76, 397]], dtype=np.float32)
    transform = cv2.getPerspectiveTransform(source, destination)
    warped_page = cv2.warpPerspective(page, transform, (canvas_width, canvas_height))
    mask = cv2.warpPerspective(
        np.full((page_height, page_width), 255, dtype=np.uint8),
        transform,
        (canvas_width, canvas_height),
    )
    canvas[mask > 0] = warped_page[mask > 0]
    return _jpeg(canvas)


def _options(
    *,
    operations: tuple[str, ...] = (
        "page_dewarp",
        "detected_margin_crop",
        "contrast_normalization",
    ),
    role: str = "title_page",
    dewarp: int = 55,
    margin: int = 2,
    contrast: int = 70,
    retention: int = 25,
    backend: str = "off",
) -> ProcessingOptions:
    return ProcessingOptions(
        operations=operations,
        role=role,
        dewarp_strength_percent=dewarp,
        margin_padding_percent=margin,
        contrast_strength_percent=contrast,
        paper_tone_retention_percent=retention,
        curvature_backend=backend,
    )


def _decode_jpeg(data: bytes) -> np.ndarray:
    with Image.open(io.BytesIO(data)) as image:
        return np.asarray(image.convert("RGB"))


def test_invalid_and_truncated_bytes_are_permanent_input_failures() -> None:
    with pytest.raises(PermanentImageInputError, match="signature"):
        process_image(b"this is not an image", _options())
    with pytest.raises(PermanentImageInputError, match="corrupt|decoded"):
        process_image(b"\xff\xd8\xff\xe0" + b"truncated", _options())


def test_perspective_page_is_rectified_and_transform_is_normalized() -> None:
    source = _synthetic_page()
    result = process_image(source, _options())

    assert result.source_sha256 == hashlib.sha256(source).hexdigest()
    assert result.source_width == 520
    assert result.source_height == 480
    assert result.output_height > result.output_width
    assert 0.55 < result.output_width / result.output_height < 0.90
    assert result.source_to_display_homography is not None
    assert len(result.source_to_display_homography) == 9
    assert result.source_to_display_homography[-1] == pytest.approx(1.0)
    assert result.applied_operations == (
        "page_dewarp",
        "detected_margin_crop",
        "contrast_normalization",
    )
    assert result.transform_manifest["kind"] == "projective"
    assert result.transform_manifest["re_ocr_required"] is False
    assert result.quality["page_boundary_detected"] is True
    assert max(result.thumbnail_width, result.thumbnail_height) <= 512


def test_older_padding_retains_more_context_than_modern_padding() -> None:
    source = _synthetic_page()
    operations = ("page_dewarp", "detected_margin_crop")
    modern = process_image(
        source,
        _options(operations=operations, margin=2, contrast=0, retention=25),
    )
    early = process_image(
        source,
        _options(operations=operations, dewarp=85, margin=8, contrast=0, retention=90),
    )

    modern_area = modern.output_width * modern.output_height
    early_area = early.output_width * early.output_height
    assert early_area > modern_area * 1.08


def test_modern_contrast_is_stronger_while_old_paper_colour_is_preserved() -> None:
    source = _synthetic_page(low_contrast=True)
    operations = ("contrast_normalization",)
    modern = process_image(
        source,
        _options(operations=operations, contrast=70, retention=25),
    )
    early = process_image(
        source,
        _options(operations=operations, contrast=35, retention=90),
    )

    modern_rgb = _decode_jpeg(modern.display_jpeg)
    early_rgb = _decode_jpeg(early.display_jpeg)
    modern_gray = cv2.cvtColor(modern_rgb, cv2.COLOR_RGB2GRAY)
    early_gray = cv2.cvtColor(early_rgb, cv2.COLOR_RGB2GRAY)
    # Measure the page rather than the dark table, whose large global step would
    # hide the intended within-page text/paper contrast difference.
    modern_page = modern_gray[100:380, 130:390]
    early_page = early_gray[100:380, 130:390]
    assert float(modern_page.std()) > float(early_page.std()) * 1.10
    # Display remains colour, and the conservative preset holds on to more warm paper tone.
    assert not np.array_equal(modern_rgb[..., 0], modern_rgb[..., 1])
    early_warmth = float(early_rgb[..., 0].mean() - early_rgb[..., 2].mean())
    modern_warmth = float(modern_rgb[..., 0].mean() - modern_rgb[..., 2].mean())
    assert early_warmth > modern_warmth


def test_derivatives_and_hashes_are_deterministic() -> None:
    source = _synthetic_page()
    options = _options()
    first = process_image(source, options)
    second = process_image(source, options)

    assert first.display_jpeg == second.display_jpeg
    assert first.ocr_jpeg == second.ocr_jpeg
    assert first.thumbnail_jpeg == second.thumbnail_jpeg
    assert first.output_hashes == second.output_hashes
    assert first.transform_manifest == second.transform_manifest
    assert first.display_sha256 == hashlib.sha256(first.display_jpeg).hexdigest()
    assert first.ocr_sha256 == hashlib.sha256(first.ocr_jpeg).hexdigest()
    assert first.thumbnail_sha256 == hashlib.sha256(first.thumbnail_jpeg).hexdigest()


def test_exif_transpose_defines_the_source_coordinate_plane() -> None:
    rgb = np.empty((90, 160, 3), dtype=np.uint8)
    rgb[:] = (224, 211, 178)
    source = _jpeg(rgb, orientation=6)
    result = process_image(
        source,
        _options(operations=("contrast_normalization",), contrast=20),
    )

    assert (result.source_width, result.source_height) == (90, 160)
    assert result.transform_manifest["source_coordinate_space"] == "exif_oriented_normalized"
    assert result.transform_manifest["stages"][0]["orientation"] == 6


def test_confident_spine_crop_is_landscape() -> None:
    rgb = np.empty((500, 300, 3), dtype=np.uint8)
    rgb[:] = (32, 39, 34)
    rectangle = ((150, 250), (72, 410), 14)
    polygon = cv2.boxPoints(rectangle).astype(np.int32)
    cv2.fillConvexPoly(rgb, polygon, (209, 184, 136))
    cv2.polylines(rgb, [polygon], True, (62, 54, 42), 3)
    source = _jpeg(rgb)
    result = process_image(
        source,
        _options(
            operations=("contrast_normalization", "spine_crop"),
            role="spine",
            contrast=50,
            retention=75,
        ),
    )

    assert result.output_width >= result.output_height
    assert result.applied_operations == ("contrast_normalization", "spine_crop")
    spine_stage = next(
        stage for stage in result.transform_manifest["stages"] if stage["name"] == "spine_crop"
    )
    assert 0.0 <= spine_stage["confidence"] <= 1.0


def test_low_confidence_spine_is_preserved_instead_of_speculatively_cropped() -> None:
    rgb = np.full((500, 300, 3), (32, 39, 34), dtype=np.uint8)
    source = _jpeg(rgb)
    result = process_image(
        source,
        _options(
            operations=("contrast_normalization", "spine_crop"),
            role="spine",
            contrast=50,
            retention=75,
        ),
    )

    assert (result.output_width, result.output_height) == (300, 500)
    assert result.applied_operations == ("contrast_normalization",)
    assert "spine_crop.low_confidence" in result.skipped_operations
    spine_stage = next(
        stage for stage in result.transform_manifest["stages"] if stage["name"] == "spine_crop"
    )
    assert spine_stage["skipped"] is True
    assert spine_stage["method"] == "no_reliable_candidate"


def test_nonlinear_adapter_removes_homography_and_marks_reocr(monkeypatch: pytest.MonkeyPatch) -> None:
    source = _synthetic_page()
    calls: list[int] = []

    def fake_adapter(rgb: np.ndarray, strength: int) -> pipeline._NonlinearResult:
        calls.append(strength)
        rows = columns = 17
        coordinates = [
            [x / (columns - 1), y / (rows - 1)]
            for y in range(rows)
            for x in range(columns)
        ]
        return pipeline._NonlinearResult(
            rgb=np.roll(rgb, shift=1, axis=1),
            manifest={
                "schema": "org.whl.image-processor.cubic-page-remap",
                "version": 1,
                "engine": {"name": "page-dewarp", "version": "0.3.4-test"},
                "cubic_parameters": [0.08, -0.05],
                "mapping_convention": "test inverse cubic remap",
                "inverse_mapping_grid": {
                    "columns": columns,
                    "rows": rows,
                    "direction": "display_normalized_to_projective_input_normalized",
                    "interpolation": "bilinear",
                    "coordinates_row_major": coordinates,
                },
                "re_ocr_required": True,
            },
        )

    monkeypatch.setattr(pipeline, "_get_page_dewarp_adapter", lambda: fake_adapter)
    result = process_image(
        source,
        _options(
            operations=("page_dewarp", "detected_margin_crop"),
            dewarp=70,
            contrast=0,
            backend="page-dewarp",
        ),
    )

    assert calls == [70]
    assert result.source_to_display_homography is None
    assert result.transform_manifest["kind"] == "nonlinear"
    assert result.transform_manifest["re_ocr_required"] is True
    nonlinear = result.transform_manifest["nonlinear"]
    assert nonlinear["engine"]["version"] == "0.3.4-test"
    assert nonlinear["cubic_parameters"] == [0.08, -0.05]
    assert nonlinear["display_to_source_mapping_grid"]["direction"].endswith(
        "source_normalized"
    )


def test_nonfinite_nonlinear_mapping_falls_back_to_strict_projective_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _synthetic_page()

    def invalid_adapter(rgb: np.ndarray, _strength: int) -> pipeline._NonlinearResult:
        coordinates = [[0.0, 0.0] for _ in range(17 * 17)]
        coordinates[20] = [float("nan"), 0.5]
        return pipeline._NonlinearResult(
            rgb=np.roll(rgb, shift=1, axis=1),
            manifest={
                "schema": "org.whl.image-processor.cubic-page-remap",
                "version": 1,
                "engine": {"name": "invalid-test", "version": "1"},
                "inverse_mapping_grid": {
                    "columns": 17,
                    "rows": 17,
                    "direction": "display_normalized_to_projective_input_normalized",
                    "interpolation": "bilinear",
                    "coordinates_row_major": coordinates,
                },
            },
        )

    monkeypatch.setattr(pipeline, "_get_page_dewarp_adapter", lambda: invalid_adapter)
    result = process_image(source, _options(backend="page-dewarp", contrast=0))

    assert result.transform_manifest["kind"] == "projective"
    assert result.source_to_display_homography is not None
    assert any(
        item.startswith("page_dewarp.nonlinear_invalid_mapping")
        for item in result.skipped_operations
    )
    json.dumps(result.transform_manifest, allow_nan=False)

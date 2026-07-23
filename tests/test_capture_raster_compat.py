from __future__ import annotations

import hashlib
import io
from pathlib import Path
from typing import Any

import pytest

import capture_pipeline
import librarytool.processing.capture_compat as capture_compat
from librarytool.processing import (
    CAPTURE_DETECTOR,
    CAPTURE_DETECTOR_VERSION,
    EXIF_ORIENTED_NORMALIZED,
    apply_capture_perspective_compat,
    find_capture_page_quad,
    propose_capture_page_boundary,
)


def test_capture_pipeline_public_api_is_a_thin_shared_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ordered = object()
    detected = object()
    corrected = b"corrected"
    calls: list[tuple[object, ...]] = []

    def order(points: object) -> object:
        calls.append(("order", points))
        return ordered

    def find(source: bytes) -> object:
        calls.append(("find", source))
        return detected

    def correct(source: bytes, quality: int = 92) -> bytes:
        calls.append(("correct", source, quality))
        return corrected

    monkeypatch.setattr(capture_pipeline._raster_processing, "order_capture_quad", order)
    monkeypatch.setattr(capture_pipeline._raster_processing, "find_capture_page_quad", find)
    monkeypatch.setattr(
        capture_pipeline._raster_processing,
        "apply_capture_perspective_compat",
        correct,
    )

    points = ((1, 2), (3, 4), (5, 6), (7, 8))
    source = b"source"
    assert capture_pipeline._order_quad(points) is ordered
    assert capture_pipeline.find_page_quad(source) is detected
    assert capture_pipeline.perspective_correct(source, quality=73) == corrected
    assert calls == [
        ("order", points),
        ("find", source),
        ("correct", source, 73),
    ]


def test_capture_pipeline_contains_no_independent_perspective_pixel_path() -> None:
    source = Path(capture_pipeline.__file__).read_text(encoding="utf-8")

    assert "getPerspectiveTransform" not in source
    assert "warpPerspective" not in source
    assert "findContours" not in source


def test_missing_optional_runtime_keeps_legacy_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(capture_compat, "_opencv_runtime", lambda: None)
    source = b"not-decoded-without-the-optional-runtime"

    assert find_capture_page_quad(source) is None
    assert propose_capture_page_boundary(source) is None
    assert apply_capture_perspective_compat(source) is source


def _opencv_runtime() -> tuple[Any, Any]:
    cv2 = pytest.importorskip("cv2", reason="capture compatibility tests require OpenCV")
    np = pytest.importorskip("numpy", reason="capture compatibility tests require NumPy")
    return cv2, np


def _synthetic_page(width: int, height: int) -> bytes:
    cv2, np = _opencv_runtime()
    image = np.full((height, width, 3), (28, 37, 31), dtype=np.uint8)
    polygon = np.array(
        [
            [round(width * 0.23), round(height * 0.09)],
            [round(width * 0.77), round(height * 0.16)],
            [round(width * 0.81), round(height * 0.90)],
            [round(width * 0.15), round(height * 0.83)],
        ],
        dtype=np.int32,
    )
    cv2.fillConvexPoly(image, polygon, (236, 220, 177))
    cv2.polylines(image, [polygon], True, (45, 44, 38), max(2, width // 260))
    cv2.putText(
        image,
        "HERBARIUM",
        (round(width * 0.30), round(height * 0.30)),
        cv2.FONT_HERSHEY_SIMPLEX,
        max(0.7, width / 740),
        (45, 44, 38),
        max(2, width // 260),
    )
    encoded, output = cv2.imencode(
        ".jpg",
        image,
        [cv2.IMWRITE_JPEG_QUALITY, 96],
    )
    assert encoded
    return output.tobytes()


def _reference_order_quad(points: object) -> Any:
    _, np = _opencv_runtime()
    array = np.asarray(points, dtype="float32").reshape(4, 2)
    sums = array.sum(axis=1)
    differences = np.diff(array, axis=1).ravel()
    return np.array(
        [
            array[sums.argmin()],
            array[differences.argmin()],
            array[sums.argmax()],
            array[differences.argmax()],
        ],
        dtype="float32",
    )


def _reference_find_page_quad(image_bytes: bytes) -> Any | None:
    cv2, np = _opencv_runtime()
    encoded = np.frombuffer(image_bytes, dtype=np.uint8)
    image = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
    if image is None:
        return None
    height, width = image.shape[:2]
    scale = 1000.0 / max(height, width)
    work = (
        cv2.resize(image, (int(width * scale), int(height * scale)))
        if scale < 1
        else image.copy()
    )
    work_height, work_width = work.shape[:2]
    gray = cv2.cvtColor(work, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (5, 5), 0)
    edges = cv2.Canny(gray, 50, 150)
    edges = cv2.dilate(edges, np.ones((3, 3), np.uint8), iterations=2)
    contours, _ = cv2.findContours(
        edges,
        cv2.RETR_EXTERNAL,
        cv2.CHAIN_APPROX_SIMPLE,
    )
    best = None
    best_area = 0.0
    for contour in sorted(contours, key=cv2.contourArea, reverse=True)[:10]:
        perimeter = cv2.arcLength(contour, True)
        approximation = cv2.approxPolyDP(contour, 0.02 * perimeter, True)
        if len(approximation) != 4 or not cv2.isContourConvex(approximation):
            continue
        area = cv2.contourArea(approximation)
        if area > best_area:
            best, best_area = approximation, area
    if best is None or best_area < 0.25 * work_height * work_width:
        return None
    quad = _reference_order_quad(best.reshape(4, 2))
    mask = np.zeros((work_height, work_width), np.uint8)
    cv2.fillPoly(mask, [quad.astype(np.int32)], 255)
    unblurred_gray = cv2.cvtColor(work, cv2.COLOR_BGR2GRAY)
    inside = cv2.mean(unblurred_gray, mask=mask)[0]
    outside_mask = cv2.bitwise_not(mask)
    if cv2.countNonZero(outside_mask) > 0.02 * work_height * work_width:
        outside = cv2.mean(unblurred_gray, mask=outside_mask)[0]
        if inside - outside < 25:
            return None
    if scale < 1:
        quad = quad / scale
    return quad


def _reference_perspective_correct(
    image_bytes: bytes,
    quality: int = 92,
) -> bytes:
    cv2, np = _opencv_runtime()
    quad = _reference_find_page_quad(image_bytes)
    if quad is None:
        return image_bytes
    image = cv2.imdecode(
        np.frombuffer(image_bytes, dtype=np.uint8),
        cv2.IMREAD_COLOR,
    )
    top_left, top_right, bottom_right, bottom_left = quad
    width = int(
        max(
            np.linalg.norm(bottom_right - bottom_left),
            np.linalg.norm(top_right - top_left),
        )
    )
    height = int(
        max(
            np.linalg.norm(top_right - bottom_right),
            np.linalg.norm(top_left - bottom_left),
        )
    )
    if width < 200 or height < 200:
        return image_bytes
    destination = np.array(
        [
            [0, 0],
            [width - 1, 0],
            [width - 1, height - 1],
            [0, height - 1],
        ],
        dtype="float32",
    )
    matrix = cv2.getPerspectiveTransform(quad, destination)
    warped = cv2.warpPerspective(image, matrix, (width, height))
    encoded, output = cv2.imencode(
        ".jpg",
        warped,
        [cv2.IMWRITE_JPEG_QUALITY, quality],
    )
    return output.tobytes() if encoded else image_bytes


@pytest.mark.parametrize(("width", "height"), [(520, 480), (1300, 1100)])
def test_detector_matches_the_preconsolidation_pixel_contract(
    width: int,
    height: int,
) -> None:
    _, np = _opencv_runtime()
    source = _synthetic_page(width, height)

    expected = _reference_find_page_quad(source)
    actual = find_capture_page_quad(source)

    assert expected is not None
    assert actual is not None
    assert actual.dtype == np.dtype("float32")
    assert np.array_equal(actual, expected)


@pytest.mark.parametrize("quality", [73, 92])
def test_perspective_jpeg_bytes_match_the_preconsolidation_contract(
    quality: int,
) -> None:
    source = _synthetic_page(520, 480)

    assert apply_capture_perspective_compat(source, quality) == (
        _reference_perspective_correct(source, quality)
    )
    assert capture_pipeline.perspective_correct(source, quality) == (
        _reference_perspective_correct(source, quality)
    )


def test_proposal_reuses_the_legacy_detection_in_exif_oriented_coordinates() -> None:
    cv2, np = _opencv_runtime()
    source = _synthetic_page(520, 480)
    pixel_quad = find_capture_page_quad(source)
    decoded = cv2.imdecode(np.frombuffer(source, dtype=np.uint8), cv2.IMREAD_COLOR)
    height, width = decoded.shape[:2]

    proposal = propose_capture_page_boundary(
        source,
        source_revision="capture:asset-7:revision-3",
    )

    assert proposal is not None
    assert proposal.source_revision == "capture:asset-7:revision-3"
    assert proposal.coordinate_space == EXIF_ORIENTED_NORMALIZED
    assert proposal.detector == CAPTURE_DETECTOR
    assert proposal.detector_version == CAPTURE_DETECTOR_VERSION
    assert 0.25 <= proposal.confidence <= 1.0
    expected = tuple(
        (
            float(point[0]) / float(width - 1),
            float(point[1]) / float(height - 1),
        )
        for point in pixel_quad
    )
    assert proposal.quad == expected
    assert propose_capture_page_boundary(source).source_revision == (
        f"sha256:{hashlib.sha256(source).hexdigest()}"
    )


def test_proposal_coordinates_follow_the_legacy_exif_oriented_decode() -> None:
    cv2, np = _opencv_runtime()
    Image = pytest.importorskip("PIL.Image")
    source = _synthetic_page(520, 480)
    with Image.open(io.BytesIO(source)) as image:
        exif = Image.Exif()
        exif[274] = 6
        encoded = io.BytesIO()
        image.save(encoded, format="JPEG", quality=96, exif=exif)
    oriented_source = encoded.getvalue()
    decoded = cv2.imdecode(
        np.frombuffer(oriented_source, dtype=np.uint8),
        cv2.IMREAD_COLOR,
    )
    pixel_quad = find_capture_page_quad(oriented_source)

    proposal = propose_capture_page_boundary(
        oriented_source,
        source_revision="capture:oriented:2",
    )

    assert decoded.shape[:2] == (520, 480)
    assert pixel_quad is not None
    assert proposal is not None
    expected = tuple(
        (
            float(point[0]) / float(decoded.shape[1] - 1),
            float(point[1]) / float(decoded.shape[0] - 1),
        )
        for point in pixel_quad
    )
    assert proposal.quad == expected

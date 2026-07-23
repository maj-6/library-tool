"""Compatibility raster operations for the legacy desktop capture pipeline.

The desktop ingest path predates the correction raster contract.  Its OpenCV
detector, output sizing, interpolation, and JPEG encoding are observable
behavior, so this module centralizes that behavior without silently changing
existing captures.  New consumers can use :func:`propose_capture_page_boundary`
to obtain the same detection as a revision-pinned :class:`PageBoundaryProposal`.

OpenCV and NumPy remain optional and are imported only when these compatibility
operations run.  A missing runtime therefore retains the legacy "no proposal /
return the original bytes" fallback.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from .raster import PageBoundaryProposal, RasterInputError


CAPTURE_DETECTOR = "librarytool.capture-pipeline.contour"
CAPTURE_DETECTOR_VERSION = "1.0.0"


@dataclass(frozen=True, slots=True)
class _CaptureDetection:
    quad: Any
    width: int
    height: int
    area_fraction: float


def _opencv_runtime() -> tuple[Any, Any] | None:
    try:
        import cv2
        import numpy as np
    except ImportError:
        return None
    return cv2, np


def order_capture_quad(points: object) -> Any:
    """Return four pixel points in legacy TL/TR/BR/BL order.

    The NumPy return type and float32 precision are intentionally preserved for
    callers of ``capture_pipeline._order_quad``.
    """

    import numpy as np

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


def _detect_capture_page(image_bytes: bytes) -> _CaptureDetection | None:
    runtime = _opencv_runtime()
    if runtime is None:
        return None
    cv2, np = runtime
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

    quad = order_capture_quad(best.reshape(4, 2))
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
    return _CaptureDetection(
        quad=quad,
        width=int(width),
        height=int(height),
        area_fraction=float(best_area) / float(work_height * work_width),
    )


def find_capture_page_quad(image_bytes: bytes) -> Any | None:
    """Return the legacy full-resolution float32 pixel quad, or ``None``."""

    detection = _detect_capture_page(image_bytes)
    return None if detection is None else detection.quad


def propose_capture_page_boundary(
    image_bytes: bytes,
    *,
    source_revision: str | None = None,
) -> PageBoundaryProposal | None:
    """Return the legacy detector result in the shared normalized contract."""

    detection = _detect_capture_page(image_bytes)
    if detection is None or detection.width < 2 or detection.height < 2:
        return None
    revision = (
        source_revision
        if source_revision is not None
        else f"sha256:{hashlib.sha256(image_bytes).hexdigest()}"
    )
    normalized_quad = tuple(
        (
            float(point[0]) / float(detection.width - 1),
            float(point[1]) / float(detection.height - 1),
        )
        for point in detection.quad
    )
    try:
        return PageBoundaryProposal(
            quad=normalized_quad,
            confidence=round(max(0.0, min(1.0, detection.area_fraction)), 6),
            detector=CAPTURE_DETECTOR,
            detector_version=CAPTURE_DETECTOR_VERSION,
            source_revision=revision,
        )
    except RasterInputError:
        # The historical pixel API remains available for a detector result
        # that cannot satisfy the stricter reusable proposal contract.
        return None


def apply_capture_perspective_compat(
    image_bytes: bytes,
    quality: int = 92,
) -> bytes:
    """Apply the legacy desktop perspective path without changing its bytes."""

    detection = _detect_capture_page(image_bytes)
    if detection is None:
        return image_bytes
    runtime = _opencv_runtime()
    if runtime is None:  # Defensive: the detector already proved it available.
        return image_bytes
    cv2, np = runtime
    image = cv2.imdecode(
        np.frombuffer(image_bytes, dtype=np.uint8),
        cv2.IMREAD_COLOR,
    )
    if image is None:
        return image_bytes
    top_left, top_right, bottom_right, bottom_left = detection.quad
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
    matrix = cv2.getPerspectiveTransform(detection.quad, destination)
    warped = cv2.warpPerspective(image, matrix, (width, height))
    encoded, output = cv2.imencode(
        ".jpg",
        warped,
        [cv2.IMWRITE_JPEG_QUALITY, quality],
    )
    return output.tobytes() if encoded else image_bytes

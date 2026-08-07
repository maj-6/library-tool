"""Remap phone-capture OCR geometry onto desktop-derived display renditions.

A BookCapture phone records Mistral OCR regions as polygons normalized against
its own display rendition, which is byte- or pixel-identical to the camera
original (recipe ``camera-original``).  The desktop import derives its own
display JPEG — ``standardize(perspective_correct(original))`` — so those
polygons stop describing the stored display pixels whenever the legacy page
warp fires.  This module carries the geometry across that derivation:

* When no warp was applied the derivative is an EXIF-uprighted, aspect
  preserving resize (or plain re-encode) of the original, so normalized
  upright coordinates transfer unchanged.
* When the warp was applied the polygon is scaled into upright pixel
  space, pushed through the exact perspective homography the compat kernel
  used, and re-normalized against the warp output.

One orientation fact governs all of this: ``cv2.imdecode`` applies the EXIF
orientation itself unless ``IMREAD_IGNORE_ORIENTATION`` is passed (verified
empirically on OpenCV 5.0), so the detector's quad and the warp already
operate on upright pixels — the same frame ``ImageOps.exif_transpose``
produces for the Pillow half of the pipeline, and the same frame the phone
normalizes its regions against.  There is therefore no orientation change
to undo, and :func:`upright_to_raw_point` must NOT be applied to these
coordinates; it is retained only for callers that genuinely hold
as-stored-grid coordinates.

Everything here is pure deterministic math over immutable inputs — no I/O,
no OpenCV requirement — so an import path, a backfill tool, and tests share
one contract.
"""

from __future__ import annotations

import io
import math
from typing import Any, Mapping, Sequence

from PIL import Image, UnidentifiedImageError

#: Marker recipe for geometry records the desktop remapped onto its own
#: display derivative.  Consumers use the ``display_sha256`` pin, not this
#: string, for validity; the marker exists for provenance and debugging.
DESKTOP_REMAP_RECIPE = "desktop-geometry-remap-v1"

_EPSILON = 1e-9
_MIN_REGION_EXTENT = 1e-4  # normalized; below this a clamped region is noise


def jpeg_exif_orientation(image_bytes: bytes) -> int:
    """The EXIF orientation tag (1..8) of *image_bytes*, defaulting to 1."""

    try:
        with Image.open(io.BytesIO(image_bytes)) as image:
            value = image.getexif().get(0x0112)
    except (OSError, UnidentifiedImageError, ValueError):
        return 1
    return value if isinstance(value, int) and 1 <= value <= 8 else 1


def upright_to_raw_point(
    x: float,
    y: float,
    orientation: int,
) -> tuple[float, float]:
    """Map a normalized EXIF-upright point back to normalized raw space.

    ``orientation`` is the EXIF tag of the raw bytes.  Raw space is the pixel
    grid as stored (what ``cv2.imdecode`` sees); upright space is what
    ``PIL.ImageOps.exif_transpose`` (or any EXIF-aware viewer) renders.
    """

    if orientation == 2:
        return 1.0 - x, y
    if orientation == 3:
        return 1.0 - x, 1.0 - y
    if orientation == 4:
        return x, 1.0 - y
    if orientation == 5:
        return y, x
    if orientation == 6:
        return y, 1.0 - x
    if orientation == 7:
        return 1.0 - y, 1.0 - x
    if orientation == 8:
        return 1.0 - y, x
    return x, y


def capture_warp_destination_size(quad: Sequence[Sequence[float]]) -> tuple[int, int]:
    """The output size the legacy capture warp derives from an ordered quad.

    Mirrors ``apply_capture_pixel_perspective_compat`` exactly: edge-length
    maxima truncated with ``int()``.  The quad must already be in TL/TR/BR/BL
    order.
    """

    (tlx, tly), (trx, try_), (brx, bry), (blx, bly) = (
        (float(p[0]), float(p[1])) for p in quad
    )
    width = int(
        max(
            math.hypot(brx - blx, bry - bly),
            math.hypot(trx - tlx, try_ - tly),
        )
    )
    height = int(
        max(
            math.hypot(trx - brx, try_ - bry),
            math.hypot(tlx - blx, tly - bly),
        )
    )
    return width, height


def perspective_matrix(
    source: Sequence[Sequence[float]],
    destination: Sequence[Sequence[float]],
) -> tuple[tuple[float, ...], ...]:
    """The 3x3 homography taking each source point to its destination point.

    Equivalent to ``cv2.getPerspectiveTransform`` for four point pairs.
    Raises ``ValueError`` for degenerate configurations.
    """

    if len(source) != 4 or len(destination) != 4:
        raise ValueError("a perspective transform needs exactly four point pairs")
    # Solve the standard 8x8 system for [h11..h32], h33 = 1.
    rows: list[list[float]] = []
    rhs: list[float] = []
    for (sx, sy), (dx, dy) in zip(
        ((float(p[0]), float(p[1])) for p in source),
        ((float(p[0]), float(p[1])) for p in destination),
    ):
        rows.append([sx, sy, 1.0, 0.0, 0.0, 0.0, -sx * dx, -sy * dx])
        rhs.append(dx)
        rows.append([0.0, 0.0, 0.0, sx, sy, 1.0, -sx * dy, -sy * dy])
        rhs.append(dy)
    coefficients = _solve_linear_system(rows, rhs)
    h = list(coefficients) + [1.0]
    return (
        (h[0], h[1], h[2]),
        (h[3], h[4], h[5]),
        (h[6], h[7], h[8]),
    )


def _solve_linear_system(rows: list[list[float]], rhs: list[float]) -> list[float]:
    size = len(rows)
    augmented = [rows[i] + [rhs[i]] for i in range(size)]
    for column in range(size):
        pivot = max(range(column, size), key=lambda r: abs(augmented[r][column]))
        if abs(augmented[pivot][column]) < _EPSILON:
            raise ValueError("degenerate perspective point configuration")
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        pivot_row = augmented[column]
        for row in range(size):
            if row == column:
                continue
            factor = augmented[row][column] / pivot_row[column]
            if factor == 0.0:
                continue
            augmented[row] = [
                value - factor * pivot_value
                for value, pivot_value in zip(augmented[row], pivot_row)
            ]
    return [augmented[i][size] / augmented[i][i] for i in range(size)]


def apply_matrix(
    matrix: Sequence[Sequence[float]],
    x: float,
    y: float,
) -> tuple[float, float]:
    """Apply a 3x3 homography to a point."""

    denominator = matrix[2][0] * x + matrix[2][1] * y + matrix[2][2]
    if abs(denominator) < _EPSILON:
        raise ValueError("point maps to infinity under the homography")
    return (
        (matrix[0][0] * x + matrix[0][1] * y + matrix[0][2]) / denominator,
        (matrix[1][0] * x + matrix[1][1] * y + matrix[1][2]) / denominator,
    )


def remap_phone_regions(
    regions: Sequence[Mapping[str, Any]],
    *,
    source_size: tuple[int, int],
    quad: Sequence[Sequence[float]] | None,
    warp_size: tuple[int, int] | None,
) -> tuple[list[dict[str, Any]], int]:
    """Remap phone display-normalized regions into the derivative's space.

    ``source_size`` is the EXIF-upright pixel size of the camera original —
    the frame both the phone's normalized coordinates and the detector's
    quad already live in.  ``quad`` is the ordered TL/TR/BR/BL pixel quad
    the legacy warp applied, or ``None`` when the derivative kept the
    original geometry; ``warp_size`` is that warp's output size.  Returns
    the surviving remapped regions plus the count dropped because the warp
    cropped them away.

    No orientation correction happens here, deliberately.  Every stage of
    the desktop derivation observes the EXIF-upright frame: ``cv2.imdecode``
    applies the orientation itself, and ``ImageOps.exif_transpose`` puts
    Pillow in the same frame.  Applying :func:`upright_to_raw_point` here
    would rotate coordinates into a grid nothing downstream ever uses.
    """

    if quad is None:
        return [dict(region) for region in regions], 0
    if warp_size is None:
        raise ValueError("warp_size is required when a quad was applied")
    source_width, source_height = source_size
    warp_width, warp_height = warp_size
    if min(source_width, source_height, warp_width, warp_height) < 1:
        raise ValueError("raster sizes must be positive")
    destination = (
        (0.0, 0.0),
        (float(warp_width - 1), 0.0),
        (float(warp_width - 1), float(warp_height - 1)),
        (0.0, float(warp_height - 1)),
    )
    matrix = perspective_matrix(quad, destination)
    remapped: list[dict[str, Any]] = []
    dropped = 0
    for region in regions:
        polygon = region.get("polygon")
        if not isinstance(polygon, Sequence) or isinstance(polygon, (str, bytes)):
            dropped += 1
            continue
        points: list[list[float]] = []
        try:
            for point in polygon:
                pixel_x, pixel_y = apply_matrix(
                    matrix,
                    float(point[0]) * source_width,
                    float(point[1]) * source_height,
                )
                points.append(
                    [
                        min(1.0, max(0.0, pixel_x / warp_width)),
                        min(1.0, max(0.0, pixel_y / warp_height)),
                    ]
                )
        except (TypeError, ValueError, IndexError):
            dropped += 1
            continue
        if len(points) < 3 or _degenerate(points):
            dropped += 1
            continue
        survivor = dict(region)
        survivor["polygon"] = points
        remapped.append(survivor)
    return remapped, dropped


def _degenerate(points: Sequence[Sequence[float]]) -> bool:
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return (
        max(xs) - min(xs) < _MIN_REGION_EXTENT
        or max(ys) - min(ys) < _MIN_REGION_EXTENT
    )


def desktop_display_geometry_record(
    phone_geometry: Mapping[str, Any],
    *,
    regions: Sequence[Mapping[str, Any]],
    display_width: int,
    display_height: int,
    display_sha256: str,
) -> dict[str, Any]:
    """A geometry record describing the desktop display derivative.

    Copies the phone record's identity and engine provenance, replaces the
    coordinate frame with the desktop derivative's, and pins validity to the
    derivative's content hash so projection can accept it regardless of how
    the phone's own display rendition evolves.
    """

    if display_width < 1 or display_height < 1:
        raise ValueError("display dimensions must be positive")
    if not display_sha256:
        raise ValueError("display_sha256 is required")
    record = {
        key: phone_geometry[key]
        for key in (
            "asset_id",
            "coordinate_space",
            "engine",
            "engine_version",
            "model",
            "source_revision",
            "display_revision",
            "source_sha256",
        )
        if key in phone_geometry
    }
    record.update(
        {
            "width": int(display_width),
            "height": int(display_height),
            "orientation": 0,
            "display_sha256": str(display_sha256),
            "remap_recipe": DESKTOP_REMAP_RECIPE,
            "regions": [dict(region) for region in regions],
        }
    )
    return record

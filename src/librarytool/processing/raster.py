"""Deterministic raster primitives shared by correction front ends.

The functions in this module are deliberately transport- and provider-neutral.
They accept immutable image bytes, return a new lossless derivative, and do no
filesystem or network I/O.  Coordinates are normalized against the
EXIF-oriented source image so a proposal, editor, and background worker can
share one geometry contract.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import numbers
import warnings
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from PIL import Image, ImageOps, UnidentifiedImageError, __version__ as PILLOW_VERSION


EXIF_ORIENTED_NORMALIZED = "exif_oriented_normalized"
POINT_ORDER = ("top_left", "top_right", "bottom_right", "bottom_left")
KERNEL_ALGORITHM_VERSION = "1.0.0"
MIN_NORMALIZED_QUAD_AREA = 0.0001
MIN_NORMALIZED_EDGE_LENGTH = 0.001
_GEOMETRY_EPSILON = 1e-12

NormalizedPoint = tuple[float, float]
NormalizedQuad = tuple[
    NormalizedPoint,
    NormalizedPoint,
    NormalizedPoint,
    NormalizedPoint,
]
Matrix3 = tuple[
    tuple[float, float, float],
    tuple[float, float, float],
    tuple[float, float, float],
]


class RasterInputError(ValueError):
    """The source raster or requested transform is invalid."""


def _is_number(value: object) -> bool:
    return isinstance(value, numbers.Real) and not isinstance(value, bool)


def _cross(a: NormalizedPoint, b: NormalizedPoint, c: NormalizedPoint) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _on_segment(
    a: NormalizedPoint,
    b: NormalizedPoint,
    point: NormalizedPoint,
) -> bool:
    return (
        min(a[0], b[0]) - _GEOMETRY_EPSILON
        <= point[0]
        <= max(a[0], b[0]) + _GEOMETRY_EPSILON
        and min(a[1], b[1]) - _GEOMETRY_EPSILON
        <= point[1]
        <= max(a[1], b[1]) + _GEOMETRY_EPSILON
    )


def _segments_intersect(
    a: NormalizedPoint,
    b: NormalizedPoint,
    c: NormalizedPoint,
    d: NormalizedPoint,
) -> bool:
    turns = (_cross(a, b, c), _cross(a, b, d), _cross(c, d, a), _cross(c, d, b))
    if turns[0] * turns[1] < -_GEOMETRY_EPSILON and turns[2] * turns[3] < -_GEOMETRY_EPSILON:
        return True
    return any(
        abs(turn) <= _GEOMETRY_EPSILON and _on_segment(start, end, point)
        for turn, start, end, point in (
            (turns[0], a, b, c),
            (turns[1], a, b, d),
            (turns[2], c, d, a),
            (turns[3], c, d, b),
        )
    )


def _signed_area(points: Sequence[NormalizedPoint]) -> float:
    return 0.5 * sum(
        points[index][0] * points[(index + 1) % 4][1]
        - points[(index + 1) % 4][0] * points[index][1]
        for index in range(4)
    )


def validate_normalized_quad(
    quad: Iterable[Iterable[float]],
    *,
    min_area: float = MIN_NORMALIZED_QUAD_AREA,
) -> NormalizedQuad:
    """Validate and normalize an ordered TL/TR/BR/BL quadrilateral.

    Image coordinates have their origin at the top left, so TL/TR/BR/BL is a
    positive (visual-clockwise) winding.  The function never silently reorders
    vertices: vertex identity is part of the editor/job contract.
    """

    if not _is_number(min_area) or not math.isfinite(float(min_area)) or min_area <= 0:
        raise ValueError("min_area must be a positive finite number")
    try:
        raw_points = tuple(tuple(point) for point in quad)
    except TypeError as exc:
        raise RasterInputError("quad must contain four coordinate pairs") from exc
    if len(raw_points) != 4:
        raise RasterInputError("quad must contain exactly four points in TL/TR/BR/BL order")

    points: list[NormalizedPoint] = []
    for index, point in enumerate(raw_points):
        if len(point) != 2:
            raise RasterInputError(f"{POINT_ORDER[index]} must contain exactly x and y")
        x, y = point
        if not _is_number(x) or not _is_number(y):
            raise RasterInputError(f"{POINT_ORDER[index]} coordinates must be numbers")
        x_float, y_float = float(x), float(y)
        if not math.isfinite(x_float) or not math.isfinite(y_float):
            raise RasterInputError(f"{POINT_ORDER[index]} coordinates must be finite")
        if not 0.0 <= x_float <= 1.0 or not 0.0 <= y_float <= 1.0:
            raise RasterInputError(f"{POINT_ORDER[index]} must be within normalized bounds [0, 1]")
        points.append((x_float, y_float))

    for index, point in enumerate(points):
        for other in points[index + 1 :]:
            if math.dist(point, other) <= _GEOMETRY_EPSILON:
                raise RasterInputError("quad vertices must be distinct")

    if _segments_intersect(points[0], points[1], points[2], points[3]) or _segments_intersect(
        points[1], points[2], points[3], points[0]
    ):
        raise RasterInputError("quad must not self-intersect")

    turns = tuple(_cross(points[index], points[(index + 1) % 4], points[(index + 2) % 4]) for index in range(4))
    if any(abs(turn) <= _GEOMETRY_EPSILON for turn in turns) or not (
        all(turn > 0 for turn in turns) or all(turn < 0 for turn in turns)
    ):
        raise RasterInputError("quad must be strictly convex")
    if any(turn < 0 for turn in turns):
        raise RasterInputError("quad vertices must use TL/TR/BR/BL order")

    top_mean_y = (points[0][1] + points[1][1]) / 2.0
    bottom_mean_y = (points[3][1] + points[2][1]) / 2.0
    left_mean_x = (points[0][0] + points[3][0]) / 2.0
    right_mean_x = (points[1][0] + points[2][0]) / 2.0
    if top_mean_y >= bottom_mean_y or left_mean_x >= right_mean_x:
        raise RasterInputError("quad vertices must use TL/TR/BR/BL labels")

    area = _signed_area(points)
    if area < float(min_area):
        raise RasterInputError(
            f"quad area is too small; normalized area must be at least {float(min_area):g}"
        )
    if any(
        math.dist(points[index], points[(index + 1) % 4]) < MIN_NORMALIZED_EDGE_LENGTH
        for index in range(4)
    ):
        raise RasterInputError("quad edge is too short")
    return (points[0], points[1], points[2], points[3])


@dataclass(frozen=True, slots=True)
class PageBoundaryProposal:
    """A detector proposal pinned to one exact source revision."""

    quad: NormalizedQuad
    confidence: float
    detector: str
    detector_version: str
    source_revision: str
    coordinate_space: str = EXIF_ORIENTED_NORMALIZED

    def __post_init__(self) -> None:
        object.__setattr__(self, "quad", validate_normalized_quad(self.quad))
        if not _is_number(self.confidence) or not math.isfinite(float(self.confidence)):
            raise ValueError("confidence must be a finite number from 0 through 1")
        if not 0.0 <= float(self.confidence) <= 1.0:
            raise ValueError("confidence must be from 0 through 1")
        object.__setattr__(self, "confidence", float(self.confidence))
        for field_name in ("detector", "detector_version", "source_revision"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be a non-empty string")
        if self.coordinate_space != EXIF_ORIENTED_NORMALIZED:
            raise ValueError(
                f"coordinate_space must be {EXIF_ORIENTED_NORMALIZED!r}"
            )

    @property
    def points(self) -> NormalizedQuad:
        """Alias used by consumers that describe the quadrilateral as points."""

        return self.quad

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "org.whl.page-boundary-proposal",
            "version": 1,
            "coordinate_space": self.coordinate_space,
            "point_order": list(POINT_ORDER),
            "quad": [[x, y] for x, y in self.quad],
            "confidence": self.confidence,
            "detector": self.detector,
            "detector_version": self.detector_version,
            "source_revision": self.source_revision,
        }


def page_boundary_proposal_from_pixel_quad(
    quad: Iterable[Iterable[float]],
    *,
    source_width: int,
    source_height: int,
    confidence: float,
    detector: str,
    detector_version: str,
    source_revision: str,
) -> PageBoundaryProposal:
    """Publish a detector's pixel quad in the shared normalized contract.

    Pixel coordinates use inclusive image endpoints, so ``width - 1`` and
    ``height - 1`` map exactly to normalized coordinate ``1``.  Detectors keep
    ownership of their pixel-space ordering and confidence calculation; this
    adapter centralizes the coordinate conversion and contract validation.
    """

    for name, value in (
        ("source_width", source_width),
        ("source_height", source_height),
    ):
        if not isinstance(value, int) or isinstance(value, bool) or value < 2:
            raise ValueError(f"{name} must be an integer of at least 2")
    try:
        pixel_points = tuple(tuple(point) for point in quad)
    except TypeError as exc:
        raise RasterInputError("quad must contain four coordinate pairs") from exc
    if len(pixel_points) != 4:
        raise RasterInputError(
            "quad must contain exactly four points in TL/TR/BR/BL order"
        )
    normalized_points: list[NormalizedPoint] = []
    for index, point in enumerate(pixel_points):
        if len(point) != 2:
            raise RasterInputError(
                f"{POINT_ORDER[index]} must contain exactly x and y"
            )
        x, y = point
        if not _is_number(x) or not _is_number(y):
            raise RasterInputError(
                f"{POINT_ORDER[index]} pixel coordinates must be numbers"
            )
        x_float, y_float = float(x), float(y)
        if not math.isfinite(x_float) or not math.isfinite(y_float):
            raise RasterInputError(
                f"{POINT_ORDER[index]} pixel coordinates must be finite"
            )
        if not 0.0 <= x_float <= source_width - 1:
            raise RasterInputError(
                f"{POINT_ORDER[index]} x must be within source pixel bounds"
            )
        if not 0.0 <= y_float <= source_height - 1:
            raise RasterInputError(
                f"{POINT_ORDER[index]} y must be within source pixel bounds"
            )
        normalized_points.append(
            (
                x_float / float(source_width - 1),
                y_float / float(source_height - 1),
            )
        )
    return PageBoundaryProposal(
        quad=(
            normalized_points[0],
            normalized_points[1],
            normalized_points[2],
            normalized_points[3],
        ),
        confidence=confidence,
        detector=detector,
        detector_version=detector_version,
        source_revision=source_revision,
    )


def _bounded_integer(value: object, *, name: str, minimum: int, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer from {minimum} through {maximum}")
    return value


@dataclass(frozen=True, slots=True)
class ManualBinaryAdjustRecipe:
    """Manual grayscale/binary adjustment, distinct from color normalization.

    ``contrast=100`` is a true one-bit-looking rendition whose pixels are only
    0 or 255.  Positive brightness lowers the threshold, so the number of white
    pixels can only increase for a fixed input.
    """

    contrast: int = 100
    brightness: int = 0

    def __post_init__(self) -> None:
        _bounded_integer(self.contrast, name="contrast", minimum=0, maximum=100)
        _bounded_integer(self.brightness, name="brightness", minimum=-100, maximum=100)

    @property
    def contrast_percent(self) -> int:
        return self.contrast

    @property
    def brightness_percent(self) -> int:
        return self.brightness

    @property
    def threshold(self) -> int:
        # Half-up rounding makes the zero point explicit and avoids Python's
        # banker rounding becoming part of the wire contract.
        raw_threshold = 127.5 - self.brightness * 1.275
        rounded = int(math.floor(raw_threshold + 0.5))
        return max(0, min(255, rounded))

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "org.whl.raster.manual-binary-adjust",
            "version": 1,
            "algorithm": "grayscale-threshold-blend-v1",
            "contrast_percent": self.contrast,
            "brightness_percent": self.brightness,
            "threshold": self.threshold,
            "threshold_rule": "round_half_up(127.5 - brightness_percent * 1.275), clamped_0_255",
            "comparison": "grayscale_value > threshold",
        }


@dataclass(frozen=True, slots=True)
class RasterLimits:
    """Decode and derivative bounds for an interactive correction job."""

    max_source_bytes: int = 96 * 1024 * 1024
    max_source_edge_px: int = 12_000
    max_source_megapixels: int = 80
    max_output_edge_px: int = 6_000
    max_output_megapixels: int = 40

    def __post_init__(self) -> None:
        for name in (
            "max_source_bytes",
            "max_source_edge_px",
            "max_source_megapixels",
            "max_output_edge_px",
            "max_output_megapixels",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if self.max_output_edge_px < 2:
            raise ValueError("max_output_edge_px must be at least 2")


@dataclass(frozen=True, slots=True)
class PixelPerspectiveResult:
    """Compatibility result for an in-memory OpenCV projective transform.

    The cloud processor predates the lossless correction recipe and has an
    observable OpenCV/JPEG pixel contract.  Keeping this lower-level result in
    the shared kernel lets that processor delegate its perspective operation
    without routing old captures through the newer Pillow/PNG rendition.
    """

    pixels: Any
    source_to_output_pixel_homography: Any
    output_width: int
    output_height: int


def apply_capture_pixel_perspective_compat(
    image_bytes: bytes,
    quad: object,
    *,
    quality: int = 92,
) -> bytes:
    """Apply the historical desktop capture warp without changing its bytes.

    The desktop recipe predates the bounded cloud and lossless correction
    recipes. Its maximum-edge sizing, linear interpolation, black border, and
    JPEG encoder settings are observable compatibility behavior. Keeping that
    recipe here makes the live capture entry point a wrapper over the shared
    raster kernel while retaining byte-for-byte output.
    """

    import cv2
    import numpy as np

    image = cv2.imdecode(
        np.frombuffer(image_bytes, dtype=np.uint8),
        cv2.IMREAD_COLOR,
    )
    if image is None:
        return image_bytes
    ordered = np.asarray(quad, dtype=np.float32).reshape(4, 2)
    top_left, top_right, bottom_right, bottom_left = ordered
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
        dtype=np.float32,
    )
    matrix = cv2.getPerspectiveTransform(ordered, destination)
    warped = cv2.warpPerspective(image, matrix, (width, height))
    encoded, output = cv2.imencode(
        ".jpg",
        warped,
        [cv2.IMWRITE_JPEG_QUALITY, quality],
    )
    return output.tobytes() if encoded else image_bytes


def order_pixel_quad(points: object) -> Any:
    """Return float32 pixel points in TL/TR/BR/BL order.

    This preserves the cloud processor's established sum/difference ordering
    and NumPy return type.  It is intentionally separate from
    :func:`validate_normalized_quad`, whose callers already know vertex
    identity and must never have their points silently reordered.
    """

    import numpy as np

    array = np.asarray(points, dtype=np.float32).reshape(4, 2)
    ordered = np.empty((4, 2), dtype=np.float32)
    sums = array.sum(axis=1)
    differences = np.diff(array, axis=1).reshape(-1)
    ordered[0] = array[int(np.argmin(sums))]
    ordered[2] = array[int(np.argmax(sums))]
    ordered[1] = array[int(np.argmin(differences))]
    ordered[3] = array[int(np.argmax(differences))]
    if len({(float(x), float(y)) for x, y in ordered}) != 4:
        raise ValueError("Degenerate four-point boundary")
    return ordered


def _bounded_pixel_destination_size(
    quad: object,
    *,
    max_edge_px: int,
    max_megapixels: int,
    min_edge_px: int,
) -> tuple[int, int]:
    import numpy as np

    top_left, top_right, bottom_right, bottom_left = order_pixel_quad(quad)
    widths = (
        float(np.linalg.norm(top_right - top_left)),
        float(np.linalg.norm(bottom_right - bottom_left)),
    )
    heights = (
        float(np.linalg.norm(bottom_left - top_left)),
        float(np.linalg.norm(bottom_right - top_right)),
    )
    # Python's round and this exact operation order are part of the existing
    # cloud rendition contract.
    width = max(min_edge_px, int(round(sum(widths) / 2.0)))
    height = max(min_edge_px, int(round(sum(heights) / 2.0)))
    scale = min(
        1.0,
        max_edge_px / max(width, height),
        math.sqrt(max_megapixels * 1_000_000 / max(width * height, 1)),
    )
    return (
        max(min_edge_px, int(round(width * scale))),
        max(min_edge_px, int(round(height * scale))),
    )


def apply_pixel_perspective_transform(
    pixels: Any,
    quad: object,
    *,
    max_edge_px: int,
    max_megapixels: int,
    min_edge_px: int = 32,
) -> PixelPerspectiveResult:
    """Rectify an RGB pixel array with the cloud-compatible OpenCV recipe.

    Unlike :func:`apply_perspective_transform`, this compatibility primitive
    does not decode, encode, or reinterpret the source.  The caller retains
    ownership of its immutable-byte policy and derivative encoders.
    """

    import cv2
    import numpy as np

    for name, value in (
        ("max_edge_px", max_edge_px),
        ("max_megapixels", max_megapixels),
        ("min_edge_px", min_edge_px),
    ):
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ValueError(f"{name} must be a positive integer")
    output_width, output_height = _bounded_pixel_destination_size(
        quad,
        max_edge_px=max_edge_px,
        max_megapixels=max_megapixels,
        min_edge_px=min_edge_px,
    )
    destination = np.array(
        [
            [0, 0],
            [output_width - 1, 0],
            [output_width - 1, output_height - 1],
            [0, output_height - 1],
        ],
        dtype=np.float32,
    )
    matrix = cv2.getPerspectiveTransform(order_pixel_quad(quad), destination)
    warped = cv2.warpPerspective(
        pixels,
        matrix,
        (output_width, output_height),
        flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_REPLICATE,
    )
    return PixelPerspectiveResult(
        pixels=warped,
        source_to_output_pixel_homography=matrix.astype(np.float64),
        output_width=output_width,
        output_height=output_height,
    )


def normalize_pixel_homography(
    pixel_matrix: Any,
    *,
    source_width: int,
    source_height: int,
    output_width: int,
    output_height: int,
) -> tuple[float, ...]:
    """Publish a pixel homography in normalized source/output coordinates."""

    import numpy as np

    source_scale = np.diag(
        [max(source_width - 1, 1), max(source_height - 1, 1), 1.0]
    )
    output_scale_inverse = np.diag(
        [
            1.0 / max(output_width - 1, 1),
            1.0 / max(output_height - 1, 1),
            1.0,
        ]
    )
    matrix = (
        output_scale_inverse
        @ np.asarray(pixel_matrix, dtype=np.float64)
        @ source_scale
    )
    if abs(float(matrix[2, 2])) > 1e-12:
        matrix /= matrix[2, 2]
    return tuple(round(float(value), 12) for value in matrix.reshape(-1))


@dataclass(frozen=True, slots=True)
class PerspectiveTransformResult:
    """Lossless derivative and reproducible transform evidence."""

    output_png: bytes
    source_width: int
    source_height: int
    output_width: int
    output_height: int
    source_revision: str
    source_sha256: str
    output_sha256: str
    source_to_output_homography: tuple[float, ...]
    _manifest_json: bytes

    @property
    def output_bytes(self) -> bytes:
        return self.output_png

    @property
    def output_media_type(self) -> str:
        return "image/png"

    @property
    def transform_manifest(self) -> dict[str, Any]:
        # A fresh object keeps callers from mutating the evidence whose hash is
        # carried by this immutable result.
        return json.loads(self._manifest_json.decode("utf-8"))

    @property
    def manifest_bytes(self) -> bytes:
        return self._manifest_json

    @property
    def manifest_sha256(self) -> str:
        return hashlib.sha256(self._manifest_json).hexdigest()

    @property
    def output_hashes(self) -> dict[str, str]:
        return {"source": self.source_sha256, "output": self.output_sha256}


def apply_manual_binary_adjust(
    image: Image.Image,
    recipe: ManualBinaryAdjustRecipe,
) -> Image.Image:
    """Return a new grayscale image using the manual adjustment contract."""

    if not isinstance(image, Image.Image):
        raise TypeError("image must be a Pillow Image")
    if not isinstance(recipe, ManualBinaryAdjustRecipe):
        raise TypeError("recipe must be a ManualBinaryAdjustRecipe")
    # Match source decoding exactly: invisible RGB values under alpha must not
    # become visible black ink in a preview or correction derivative.
    if image.mode in {"RGBA", "LA"} or (
        image.mode == "P" and "transparency" in image.info
    ):
        rgba = image.convert("RGBA")
        background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        opaque = Image.alpha_composite(background, rgba).convert("RGB")
    else:
        opaque = image.convert("RGB")
    grayscale = opaque.convert("L")
    threshold = recipe.threshold
    contrast = recipe.contrast
    lookup: list[int] = []
    for value in range(256):
        binary_value = 255 if value > threshold else 0
        # Integer half-up blend is deterministic. At 100 this simplifies to
        # the exact binary value without any resampling or encoder ambiguity.
        lookup.append(((100 - contrast) * value + contrast * binary_value + 50) // 100)
    return grayscale.point(lookup, mode="L")


def _round_half_up(value: float) -> int:
    return int(math.floor(value + 0.5))


def _pixel_point(point: NormalizedPoint, width: int, height: int) -> tuple[float, float]:
    return point[0] * (width - 1), point[1] * (height - 1)


def _destination_size(
    quad: NormalizedQuad,
    source_width: int,
    source_height: int,
    limits: RasterLimits,
) -> tuple[int, int, bool]:
    tl, tr, br, bl = tuple(_pixel_point(point, source_width, source_height) for point in quad)
    width_spans = (math.dist(tl, tr), math.dist(bl, br))
    height_spans = (math.dist(tl, bl), math.dist(tr, br))
    if min(*width_spans, *height_spans) < 1.0:
        raise RasterInputError(
            "quad edge has a decoded pixel-space span below 1 pixel"
        )
    mean_width_span = sum(width_spans) / 2.0
    mean_height_span = sum(height_spans) / 2.0
    if mean_width_span < 1.0 or mean_height_span < 1.0:
        raise RasterInputError(
            "quad produces a decoded pixel-space span below 1 pixel"
        )
    width = _round_half_up(mean_width_span) + 1
    height = _round_half_up(mean_height_span) + 1
    if width < 2 or height < 2:
        raise RasterInputError(
            "quad produces a destination smaller than 2 pixels in width or height"
        )
    maximum_pixels = limits.max_output_megapixels * 1_000_000
    scale = min(
        1.0,
        limits.max_output_edge_px / max(width, height),
        math.sqrt(maximum_pixels / (width * height)),
    )
    if scale >= 1.0:
        return width, height, False
    # Flooring under a bound avoids a one-pixel rounding overshoot.
    output_width = int(math.floor(width * scale))
    output_height = int(math.floor(height * scale))
    if output_width < 2 or output_height < 2:
        raise RasterInputError(
            "configured output limits reduce the destination below 2 pixels"
        )
    return output_width, output_height, True


def _solve_linear_system(rows: Sequence[Sequence[float]], values: Sequence[float]) -> tuple[float, ...]:
    size = len(values)
    augmented = [list(map(float, rows[index])) + [float(values[index])] for index in range(size)]
    for column in range(size):
        pivot = max(range(column, size), key=lambda row: abs(augmented[row][column]))
        if abs(augmented[pivot][column]) <= _GEOMETRY_EPSILON:
            raise RasterInputError("quad does not define an invertible perspective transform")
        if pivot != column:
            augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        pivot_value = augmented[column][column]
        augmented[column] = [value / pivot_value for value in augmented[column]]
        for row in range(size):
            if row == column:
                continue
            factor = augmented[row][column]
            if abs(factor) <= _GEOMETRY_EPSILON:
                continue
            augmented[row] = [
                current - factor * pivot_current
                for current, pivot_current in zip(augmented[row], augmented[column], strict=True)
            ]
    return tuple(augmented[index][-1] for index in range(size))


def _homography(
    source: Sequence[tuple[float, float]],
    destination: Sequence[tuple[float, float]],
) -> Matrix3:
    rows: list[list[float]] = []
    values: list[float] = []
    for (x, y), (u, v) in zip(source, destination, strict=True):
        rows.append([x, y, 1.0, 0.0, 0.0, 0.0, -u * x, -u * y])
        values.append(u)
        rows.append([0.0, 0.0, 0.0, x, y, 1.0, -v * x, -v * y])
        values.append(v)
    coefficients = _solve_linear_system(rows, values)
    return (
        (coefficients[0], coefficients[1], coefficients[2]),
        (coefficients[3], coefficients[4], coefficients[5]),
        (coefficients[6], coefficients[7], 1.0),
    )


def _matrix_multiply(left: Matrix3, right: Matrix3) -> Matrix3:
    return tuple(
        tuple(sum(left[row][index] * right[index][column] for index in range(3)) for column in range(3))
        for row in range(3)
    )  # type: ignore[return-value]


def _matrix_inverse(matrix: Matrix3) -> Matrix3:
    a, b, c = matrix[0]
    d, e, f = matrix[1]
    g, h, i = matrix[2]
    determinant = a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)
    if abs(determinant) <= _GEOMETRY_EPSILON:
        raise RasterInputError("quad does not define an invertible perspective transform")
    inverse_determinant = 1.0 / determinant
    return (
        (
            (e * i - f * h) * inverse_determinant,
            (c * h - b * i) * inverse_determinant,
            (b * f - c * e) * inverse_determinant,
        ),
        (
            (f * g - d * i) * inverse_determinant,
            (a * i - c * g) * inverse_determinant,
            (c * d - a * f) * inverse_determinant,
        ),
        (
            (d * h - e * g) * inverse_determinant,
            (b * g - a * h) * inverse_determinant,
            (a * e - b * d) * inverse_determinant,
        ),
    )


def _normalize_matrix(matrix: Matrix3) -> Matrix3:
    scale = matrix[2][2]
    if abs(scale) <= _GEOMETRY_EPSILON:
        raise RasterInputError("perspective homography cannot be normalized")
    return tuple(tuple(value / scale for value in row) for row in matrix)  # type: ignore[return-value]


def _stable_float(value: float) -> float:
    rounded = round(value, 12)
    return 0.0 if abs(rounded) < 5e-13 else rounded


def _canonical_matrix(matrix: Matrix3) -> Matrix3:
    """Freeze the matrix precision before either publication or rendering."""

    return tuple(
        tuple(_stable_float(value) for value in row) for row in matrix
    )  # type: ignore[return-value]


def _validate_source_dimensions(width: int, height: int, limits: RasterLimits) -> None:
    if width < 2 or height < 2:
        raise RasterInputError("source dimensions must be at least 2 by 2 pixels")
    if max(width, height) > limits.max_source_edge_px:
        raise RasterInputError("source edge exceeds the configured pixel limit")
    if width * height > limits.max_source_megapixels * 1_000_000:
        raise RasterInputError("source exceeds the configured megapixel limit")


def _decode_source(source: bytes, limits: RasterLimits) -> tuple[Image.Image, int, tuple[int, int], str]:
    if not source:
        raise RasterInputError("source image is empty")
    if len(source) > limits.max_source_bytes:
        raise RasterInputError("compressed source exceeds the configured byte limit")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(source)) as opened:
                if int(getattr(opened, "n_frames", 1)) != 1:
                    raise RasterInputError("multi-frame images are not supported")
                raw_dimensions = tuple(map(int, opened.size))
                orientation = int(opened.getexif().get(274, 1) or 1)
                if orientation not in range(1, 9):
                    orientation = 1
                raw_width, raw_height = raw_dimensions
                _validate_source_dimensions(raw_width, raw_height, limits)
                expected_oriented_dimensions = (
                    (raw_height, raw_width)
                    if orientation in {5, 6, 7, 8}
                    else (raw_width, raw_height)
                )
                _validate_source_dimensions(*expected_oriented_dimensions, limits)
                oriented = ImageOps.exif_transpose(opened)
                width, height = map(int, oriented.size)
                if (width, height) != expected_oriented_dimensions:
                    raise RasterInputError("EXIF orientation produced unexpected source dimensions")
                if oriented.mode in {"RGBA", "LA"} or (
                    oriented.mode == "P" and "transparency" in oriented.info
                ):
                    rgba = oriented.convert("RGBA")
                    background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
                    rgb = Image.alpha_composite(background, rgba).convert("RGB")
                else:
                    rgb = oriented.convert("RGB")
                rgb.load()
                format_name = str(opened.format or "unknown").upper()
    except RasterInputError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise RasterInputError("source triggered Pillow's decompression-bomb guard") from exc
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
        raise RasterInputError(f"source image cannot be decoded: {exc}") from exc
    return rgb, orientation, raw_dimensions, format_name


def _encode_png(image: Image.Image) -> bytes:
    clean = image.copy()
    clean.info.clear()
    output = io.BytesIO()
    clean.save(output, format="PNG", optimize=False, compress_level=9)
    return output.getvalue()


def _canonical_json(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def apply_perspective_transform(
    source_bytes: bytes | bytearray | memoryview,
    quad: Iterable[Iterable[float]],
    *,
    source_revision: str | None = None,
    adjustment: ManualBinaryAdjustRecipe | None = None,
    limits: RasterLimits | None = None,
) -> PerspectiveTransformResult:
    """Rectify an explicit normalized quad into a new lossless PNG.

    Output dimensions are the half-up-rounded mean lengths of each pair of
    opposite source edges, plus one pixel for inclusive endpoints, then scaled
    down (never up) to ``limits``.  The source buffer is copied before decode
    and is never written or returned as a mutable object.
    """

    if not isinstance(source_bytes, (bytes, bytearray, memoryview)):
        raise TypeError("source_bytes must be bytes-like")
    validated_quad = validate_normalized_quad(quad)
    if adjustment is not None and not isinstance(adjustment, ManualBinaryAdjustRecipe):
        raise TypeError("adjustment must be a ManualBinaryAdjustRecipe or None")
    selected_limits = limits or RasterLimits()
    if not isinstance(selected_limits, RasterLimits):
        raise TypeError("limits must be RasterLimits or None")
    source_length = source_bytes.nbytes if isinstance(source_bytes, memoryview) else len(source_bytes)
    if source_length > selected_limits.max_source_bytes:
        raise RasterInputError("compressed source exceeds the configured byte limit")
    if source_length == 0:
        raise RasterInputError("source image is empty")
    source = bytes(source_bytes)

    source_sha256 = hashlib.sha256(source).hexdigest()
    revision = source_revision if source_revision is not None else f"sha256:{source_sha256}"
    if not isinstance(revision, str) or not revision.strip():
        raise ValueError("source_revision must be a non-empty string")

    image, orientation, raw_dimensions, format_name = _decode_source(source, selected_limits)
    source_width, source_height = map(int, image.size)
    output_width, output_height, was_bounded = _destination_size(
        validated_quad,
        source_width,
        source_height,
        selected_limits,
    )

    destination_normalized = ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0))
    normalized_forward = _canonical_matrix(
        _normalize_matrix(_homography(validated_quad, destination_normalized))
    )
    normalized_inverse = _matrix_inverse(normalized_forward)
    source_scale: Matrix3 = (
        (float(source_width - 1), 0.0, 0.0),
        (0.0, float(source_height - 1), 0.0),
        (0.0, 0.0, 1.0),
    )
    output_unscale: Matrix3 = (
        (1.0 / float(output_width - 1), 0.0, 0.0),
        (0.0, 1.0 / float(output_height - 1), 0.0),
        (0.0, 0.0, 1.0),
    )
    output_pixel_to_source_pixel = _normalize_matrix(
        _matrix_multiply(source_scale, _matrix_multiply(normalized_inverse, output_unscale))
    )
    inverse_coefficients = (
        output_pixel_to_source_pixel[0][0],
        output_pixel_to_source_pixel[0][1],
        output_pixel_to_source_pixel[0][2],
        output_pixel_to_source_pixel[1][0],
        output_pixel_to_source_pixel[1][1],
        output_pixel_to_source_pixel[1][2],
        output_pixel_to_source_pixel[2][0],
        output_pixel_to_source_pixel[2][1],
    )
    transformed = image.transform(
        (output_width, output_height),
        Image.Transform.PERSPECTIVE,
        inverse_coefficients,
        resample=Image.Resampling.BICUBIC,
        fillcolor=(255, 255, 255),
    )
    if adjustment is not None:
        transformed = apply_manual_binary_adjust(transformed, adjustment)

    output_png = _encode_png(transformed)
    output_sha256 = hashlib.sha256(output_png).hexdigest()
    stable_homography = tuple(value for row in normalized_forward for value in row)
    manifest: dict[str, Any] = {
        "schema": "org.whl.raster.perspective-transform",
        "version": 1,
        "kind": "projective",
        "processor": {
            "name": "librarytool.processing.raster",
            "algorithm_version": KERNEL_ALGORITHM_VERSION,
            "pillow_version": PILLOW_VERSION,
        },
        "source_revision": revision,
        "source_sha256": source_sha256,
        "source_format": format_name,
        "source_coordinate_space": EXIF_ORIENTED_NORMALIZED,
        "output_coordinate_space": "normalized",
        "point_order": list(POINT_ORDER),
        "quad": [[x, y] for x, y in validated_quad],
        "raw_source_dimensions": list(raw_dimensions),
        "source_dimensions": [source_width, source_height],
        "exif_orientation": orientation,
        "output_dimensions": [output_width, output_height],
        "output_dimension_rule": "round_half_up(mean_opposite_edge_lengths) + 1",
        "output_was_bounded": was_bounded,
        "source_to_output_homography": list(stable_homography),
        "homography_precision": "decimal_12_canonicalized_before_render",
        "mapping_convention": (
            "row-major 3x3 maps normalized EXIF-oriented source coordinates "
            "to normalized output coordinates"
        ),
        "resampling": "pillow_bicubic",
        "adjustment": None if adjustment is None else adjustment.as_dict(),
        "output": {
            "media_type": "image/png",
            "sha256": output_sha256,
            "bytes": len(output_png),
        },
        "limits": {
            "max_output_edge_px": selected_limits.max_output_edge_px,
            "max_output_megapixels": selected_limits.max_output_megapixels,
        },
    }
    manifest_json = _canonical_json(manifest)
    return PerspectiveTransformResult(
        output_png=output_png,
        source_width=source_width,
        source_height=source_height,
        output_width=output_width,
        output_height=output_height,
        source_revision=revision,
        source_sha256=source_sha256,
        output_sha256=output_sha256,
        source_to_output_homography=stable_homography,
        _manifest_json=manifest_json,
    )

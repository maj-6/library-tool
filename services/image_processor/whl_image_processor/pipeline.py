"""Deterministic, bounded image cleanup for photographed catalogue material.

The immutable upload is never rewritten.  This module decodes it into a new
pixel buffer, applies the requested recipe, and emits three derivative JPEGs.
All coordinates in the public transform contract refer to the EXIF-oriented
source image; raw stored bytes are identified separately by ``source_sha256``.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.metadata
import io
import math
import tempfile
import warnings
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

from librarytool.processing import raster as _raster_processing


class PermanentImageInputError(ValueError):
    """The source or recipe is invalid and retrying cannot repair it."""


class RetryableImageProcessingError(RuntimeError):
    """The processor could not finish because of a transient/runtime failure."""


@dataclass(frozen=True, slots=True)
class ProcessingOptions:
    operations: tuple[str, ...]
    role: str
    dewarp_strength_percent: int
    margin_padding_percent: int
    contrast_strength_percent: int
    paper_tone_retention_percent: int
    max_megapixels: int = 40
    max_edge_px: int = 6000
    curvature_backend: str = "page-dewarp"

    def __post_init__(self) -> None:
        if len(set(self.operations)) != len(self.operations):
            raise ValueError("operations must not contain duplicates")
        if self.role not in {"title_page", "cover", "spine", "other"}:
            raise ValueError(f"unsupported photo role: {self.role!r}")
        for name in (
            "dewarp_strength_percent",
            "margin_padding_percent",
            "contrast_strength_percent",
            "paper_tone_retention_percent",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 100:
                raise ValueError(f"{name} must be an integer from 0 through 100")
        if not isinstance(self.max_megapixels, int) or self.max_megapixels < 1:
            raise ValueError("max_megapixels must be a positive integer")
        if not isinstance(self.max_edge_px, int) or self.max_edge_px < 256:
            raise ValueError("max_edge_px must be an integer of at least 256")
        if self.curvature_backend not in {"page-dewarp", "off"}:
            raise ValueError("curvature_backend must be 'page-dewarp' or 'off'")


@dataclass(frozen=True, slots=True)
class ProcessedImage:
    display_jpeg: bytes
    ocr_jpeg: bytes
    thumbnail_jpeg: bytes
    source_width: int
    source_height: int
    output_width: int
    output_height: int
    thumbnail_width: int
    thumbnail_height: int
    source_sha256: str
    display_sha256: str
    ocr_sha256: str
    thumbnail_sha256: str
    applied_operations: tuple[str, ...]
    skipped_operations: tuple[str, ...]
    source_to_display_homography: tuple[float, ...] | None
    transform_manifest: dict[str, Any]
    quality: dict[str, Any]

    @property
    def output_hashes(self) -> dict[str, str]:
        return {
            "display": self.display_sha256,
            "ocr": self.ocr_sha256,
            "thumbnail": self.thumbnail_sha256,
        }


@dataclass(slots=True)
class _DecodedImage:
    rgb: Any
    raw_width: int
    raw_height: int
    orientation: int
    format_name: str


@dataclass(slots=True)
class _Boundary:
    quad: Any | None
    confidence: float
    detected: bool
    method: str


@dataclass(slots=True)
class _NonlinearResult:
    rgb: Any
    manifest: dict[str, Any]


_KNOWN_OPERATIONS = {
    "page_dewarp",
    "detected_margin_crop",
    "contrast_normalization",
    "spine_crop",
}
_MAX_COMPRESSED_BYTES = 96 * 1024 * 1024
_JPEG_MAGIC = b"\xff\xd8\xff"
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_TIFF_MAGICS = (b"II*\x00", b"MM\x00*")


@lru_cache(maxsize=1)
def _runtime() -> tuple[Any, Any, Any, Any]:
    try:
        import cv2
        import numpy as np
        from PIL import Image, ImageOps
    except ImportError as exc:  # a bad container may succeed on a later deployment
        raise RetryableImageProcessingError(
            f"Required image runtime is unavailable: {exc.name or type(exc).__name__}"
        ) from exc
    return cv2, np, Image, ImageOps


def _magic_format(data: bytes) -> str | None:
    if data.startswith(_JPEG_MAGIC):
        return "JPEG"
    if data.startswith(_PNG_MAGIC):
        return "PNG"
    if data.startswith(_TIFF_MAGICS):
        return "TIFF"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "WEBP"
    return None


def _decode_source(data: bytes, options: ProcessingOptions) -> _DecodedImage:
    _, np, Image, ImageOps = _runtime()
    expected_format = _magic_format(data)
    if expected_format is None:
        raise PermanentImageInputError("Unsupported or missing image file signature")
    if not data:
        raise PermanentImageInputError("Source image is empty")
    if len(data) > _MAX_COMPRESSED_BYTES:
        raise PermanentImageInputError("Compressed source exceeds the 96 MiB safety limit")

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as probe:
                actual_format = str(probe.format or "").upper()
                if actual_format != expected_format:
                    raise PermanentImageInputError(
                        f"Image signature says {expected_format}, decoder says {actual_format or 'unknown'}"
                    )
                raw_width, raw_height = map(int, probe.size)
                if raw_width < 32 or raw_height < 32:
                    raise PermanentImageInputError("Source image dimensions are too small")
                if raw_width > options.max_edge_px or raw_height > options.max_edge_px:
                    raise PermanentImageInputError(
                        f"Source edge exceeds configured {options.max_edge_px}px limit"
                    )
                pixels = raw_width * raw_height
                if pixels > options.max_megapixels * 1_000_000:
                    raise PermanentImageInputError(
                        f"Source exceeds configured {options.max_megapixels} megapixel limit"
                    )
                if int(getattr(probe, "n_frames", 1)) != 1:
                    raise PermanentImageInputError("Multi-frame images are not accepted")
                orientation = int(probe.getexif().get(274, 1) or 1)
                if orientation not in range(1, 9):
                    orientation = 1
                probe.verify()

            with Image.open(io.BytesIO(data)) as image:
                image = ImageOps.exif_transpose(image)
                if image.mode in {"RGBA", "LA"} or (
                    image.mode == "P" and "transparency" in image.info
                ):
                    rgba = image.convert("RGBA")
                    background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
                    image = Image.alpha_composite(background, rgba).convert("RGB")
                else:
                    image = image.convert("RGB")
                image.load()
                rgb = np.asarray(image, dtype=np.uint8).copy()
    except PermanentImageInputError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise PermanentImageInputError("Image triggered Pillow's decompression-bomb guard") from exc
    except (OSError, SyntaxError, ValueError) as exc:
        raise PermanentImageInputError(f"Image is corrupt or cannot be decoded: {exc}") from exc
    except MemoryError as exc:
        raise RetryableImageProcessingError("Insufficient memory while decoding image") from exc

    return _DecodedImage(
        rgb=rgb,
        raw_width=raw_width,
        raw_height=raw_height,
        orientation=orientation,
        format_name=expected_format,
    )


def _order_quad(points: Any) -> Any:
    return _raster_processing.order_pixel_quad(points)


def _quad_score(quad: Any, contour_area: float, width: int, height: int) -> float:
    cv2, np, _, _ = _runtime()
    ordered = _order_quad(quad)
    quad_area = abs(float(cv2.contourArea(ordered)))
    if quad_area <= 1:
        return -1.0
    area_fraction = quad_area / float(width * height)
    if not 0.10 <= area_fraction <= 0.995:
        return -1.0
    rectangularity = min(1.0, max(0.0, contour_area / quad_area))
    centre = ordered.mean(axis=0)
    centre_distance = math.hypot(
        (float(centre[0]) - width / 2) / max(width, 1),
        (float(centre[1]) - height / 2) / max(height, 1),
    )
    centre_score = max(0.0, 1.0 - centre_distance)
    edge_margin = min(
        float(np.min(ordered[:, 0])),
        float(np.min(ordered[:, 1])),
        float(width - 1 - np.max(ordered[:, 0])),
        float(height - 1 - np.max(ordered[:, 1])),
    )
    border_score = 0.7 if edge_margin <= 1 else 1.0
    return area_fraction * (0.55 + 0.25 * rectangularity + 0.20 * centre_score) * border_score


def _detect_page_boundary(rgb: Any) -> _Boundary:
    cv2, np, _, _ = _runtime()
    height, width = rgb.shape[:2]
    scale = min(1.0, 1400.0 / max(height, width))
    if scale < 1.0:
        work = cv2.resize(
            rgb,
            (max(32, int(round(width * scale))), max(32, int(round(height * scale)))),
            interpolation=cv2.INTER_AREA,
        )
    else:
        work = rgb
    wh, ww = work.shape[:2]
    gray = cv2.cvtColor(work, cv2.COLOR_RGB2GRAY)
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    median = float(np.median(blurred))
    lower = int(max(20, 0.55 * median))
    upper = int(min(245, max(lower + 30, 1.35 * median)))
    edge_mask = cv2.Canny(blurred, lower, upper)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (7, 7))
    edge_mask = cv2.morphologyEx(edge_mask, cv2.MORPH_CLOSE, kernel, iterations=2)

    _, light_mask = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    light_mask = cv2.morphologyEx(light_mask, cv2.MORPH_CLOSE, kernel, iterations=2)

    best_quad = None
    best_score = -1.0
    best_method = "none"
    for method, mask in (("edge_contour", edge_mask), ("luminance_contour", light_mask)):
        contours, _ = cv2.findContours(mask, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        contours = sorted(contours, key=cv2.contourArea, reverse=True)[:24]
        for contour in contours:
            contour_area = float(cv2.contourArea(contour))
            if contour_area < wh * ww * 0.08:
                continue
            perimeter = float(cv2.arcLength(contour, True))
            if perimeter <= 0:
                continue
            candidates: list[Any] = []
            for epsilon in (0.012, 0.02, 0.032, 0.05):
                approximation = cv2.approxPolyDP(contour, epsilon * perimeter, True)
                if len(approximation) == 4 and cv2.isContourConvex(approximation):
                    candidates.append(approximation.reshape(4, 2))
                    break
            if not candidates:
                rect = cv2.minAreaRect(contour)
                rectangularity = contour_area / max(float(rect[1][0] * rect[1][1]), 1.0)
                if rectangularity >= 0.72:
                    candidates.append(cv2.boxPoints(rect))
            for candidate in candidates:
                try:
                    score = _quad_score(candidate, contour_area, ww, wh)
                    ordered = _order_quad(candidate)
                except ValueError:
                    continue
                if score > best_score:
                    best_score = score
                    best_quad = ordered
                    best_method = method

    if best_quad is None:
        return _Boundary(None, 0.0, False, "full_frame_fallback")
    best_quad = (best_quad / scale).astype(np.float32)
    area_fraction = abs(float(cv2.contourArea(best_quad))) / float(width * height)
    confidence = max(0.05, min(0.99, 0.25 + 0.85 * area_fraction))
    return _Boundary(best_quad, round(confidence, 4), True, best_method)


def _expand_quad(quad: Any, padding_percent: int, width: int, height: int) -> Any:
    _, np, _, _ = _runtime()
    ordered = _order_quad(quad)
    centre = ordered.mean(axis=0)
    # Padding is per side: 8% retains visibly more surrounding material than 2%.
    scale = 1.0 + 2.0 * padding_percent / 100.0
    expanded = centre + (ordered - centre) * scale
    expanded[:, 0] = np.clip(expanded[:, 0], 0, width - 1)
    expanded[:, 1] = np.clip(expanded[:, 1], 0, height - 1)
    return expanded.astype(np.float32)


def _projective_warp(rgb: Any, quad: Any, options: ProcessingOptions) -> tuple[Any, Any]:
    # Resolve the service runtime first so its established retryable error is
    # retained when OpenCV/NumPy are absent.
    _runtime()
    result = _raster_processing.apply_pixel_perspective_transform(
        rgb,
        quad,
        max_edge_px=options.max_edge_px,
        max_megapixels=options.max_megapixels,
    )
    return result.pixels, result.source_to_output_pixel_homography


def _limit_output(rgb: Any, options: ProcessingOptions) -> tuple[Any, Any | None]:
    cv2, np, _, _ = _runtime()
    height, width = rgb.shape[:2]
    scale = min(
        1.0,
        options.max_edge_px / max(width, height),
        math.sqrt(options.max_megapixels * 1_000_000 / max(width * height, 1)),
    )
    if scale >= 1.0:
        return rgb, None
    output_width = max(32, int(round(width * scale)))
    output_height = max(32, int(round(height * scale)))
    resized = cv2.resize(rgb, (output_width, output_height), interpolation=cv2.INTER_AREA)
    resize_matrix = np.diag(
        [
            (output_width - 1) / max(width - 1, 1),
            (output_height - 1) / max(height - 1, 1),
            1.0,
        ]
    )
    return resized, resize_matrix


def _spine_candidate(rgb: Any) -> tuple[Any | None, float]:
    cv2, np, _, _ = _runtime()
    height, width = rgb.shape[:2]
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    edges = cv2.Canny(blurred, 45, 140)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (11, 7))
    merged = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, kernel, iterations=3)
    contours, _ = cv2.findContours(merged, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    best = None
    best_score = 0.0
    for contour in sorted(contours, key=cv2.contourArea, reverse=True)[:30]:
        area = float(cv2.contourArea(contour))
        rect = cv2.minAreaRect(contour)
        rw, rh = map(float, rect[1])
        if min(rw, rh) < 8:
            continue
        long_edge, short_edge = max(rw, rh), min(rw, rh)
        elongation = long_edge / short_edge
        area_fraction = rw * rh / max(width * height, 1)
        rectangularity = area / max(rw * rh, 1.0)
        if elongation < 2.2 or not 0.035 <= area_fraction <= 0.98:
            continue
        score = (
            min(1.0, (elongation - 2.0) / 6.0) * 0.45
            + min(1.0, rectangularity) * 0.30
            + min(1.0, area_fraction / 0.35) * 0.25
        )
        if score > best_score:
            best = cv2.boxPoints(rect).astype(np.float32)
            best_score = score
    return best, round(min(0.99, best_score), 4)


def _rotate_clockwise(rgb: Any, source_to_current: Any) -> tuple[Any, Any]:
    cv2, np, _, _ = _runtime()
    height, _ = rgb.shape[:2]
    rotation = np.array([[0.0, -1.0, height - 1], [1.0, 0.0, 0.0], [0.0, 0.0, 1.0]])
    return cv2.rotate(rgb, cv2.ROTATE_90_CLOCKWISE), rotation @ source_to_current


def _crop_spine(rgb: Any, options: ProcessingOptions) -> tuple[Any, Any, dict[str, Any]]:
    _, np, _, _ = _runtime()
    height, width = rgb.shape[:2]
    candidate, confidence = _spine_candidate(rgb)
    if candidate is not None and confidence >= 0.50:
        padded = _expand_quad(candidate, min(options.margin_padding_percent, 8), width, height)
        cropped, matrix = _projective_warp(rgb, padded, options)
        skipped = False
    else:
        # A guessed centre strip can silently discard a real spine that is off
        # centre or photographed diagonally. Preserve the complete source when
        # geometry is uncertain and expose the skip in the result contract.
        cropped = rgb.copy()
        matrix = np.eye(3, dtype=np.float64)
        skipped = True
    if not skipped and cropped.shape[0] > cropped.shape[1]:
        cropped, matrix = _rotate_clockwise(cropped, matrix)
    return cropped, matrix, {
        "name": "spine_crop",
        "method": "rotated_min_area_rect" if not skipped else "no_reliable_candidate",
        "confidence": confidence,
        "skipped": skipped,
        "long_edge_orientation": "source" if skipped else "horizontal",
    }


def _normalised_homography(
    pixel_matrix: Any,
    source_width: int,
    source_height: int,
    output_width: int,
    output_height: int,
) -> tuple[float, ...]:
    _runtime()
    return _raster_processing.normalize_pixel_homography(
        pixel_matrix,
        source_width=source_width,
        source_height=source_height,
        output_width=output_width,
        output_height=output_height,
    )


def _normalise_colour(rgb: Any, strength_percent: int, retention_percent: int) -> Any:
    cv2, np, _, _ = _runtime()
    if strength_percent <= 0:
        return rgb.copy()
    strength = strength_percent / 100.0
    retention = retention_percent / 100.0
    lab = cv2.cvtColor(rgb, cv2.COLOR_RGB2LAB)
    lightness, chroma_a, chroma_b = cv2.split(lab)

    minimum_edge = min(lightness.shape[:2])
    kernel = max(15, min(151, int(round(minimum_edge / 7))))
    if kernel % 2 == 0:
        kernel += 1
    illumination = cv2.GaussianBlur(lightness, (kernel, kernel), 0)
    local_reference = max(1.0, float(np.median(lightness)))
    normalised = cv2.divide(
        lightness, np.maximum(illumination, 1), scale=min(230.0, local_reference)
    )
    clahe = cv2.createCLAHE(clipLimit=1.4 + 2.8 * strength, tileGridSize=(8, 8))
    global_preserving = clahe.apply(lightness)
    local_cleanup = clahe.apply(normalised)
    enhanced = cv2.addWeighted(global_preserving, 0.78, local_cleanup, 0.22, 0)
    low, high = np.percentile(enhanced, (1.0, 99.0))
    if high - low >= 8:
        stretched = np.clip(
            (enhanced.astype(np.float32) - float(low)) * (255.0 / float(high - low)),
            0,
            255,
        ).astype(np.uint8)
        enhanced = cv2.addWeighted(enhanced, 1.0 - 0.35 * strength, stretched, 0.35 * strength, 0)
    luminance_mix = strength * (1.0 - 0.38 * retention)
    final_l = cv2.addWeighted(lightness, 1.0 - luminance_mix, enhanced, luminance_mix, 0)

    # Old paper keeps its cream/foxed character. Modern material gets mild
    # chroma-neutralisation without ever converting the display derivative to gray.
    neutralise = 0.18 * strength * (1.0 - retention)
    final_a = np.clip(128.0 + (chroma_a.astype(np.float32) - 128.0) * (1.0 - neutralise), 0, 255)
    final_b = np.clip(128.0 + (chroma_b.astype(np.float32) - 128.0) * (1.0 - neutralise), 0, 255)
    merged = cv2.merge((final_l, final_a.astype(np.uint8), final_b.astype(np.uint8)))
    return cv2.cvtColor(merged, cv2.COLOR_LAB2RGB)


def _ocr_derivative(rgb: Any) -> Any:
    cv2, np, _, _ = _runtime()
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    minimum_edge = min(gray.shape[:2])
    kernel = max(21, min(161, int(round(minimum_edge / 6))))
    if kernel % 2 == 0:
        kernel += 1
    illumination = cv2.GaussianBlur(gray, (kernel, kernel), 0)
    flattened = cv2.divide(gray, np.maximum(illumination, 1), scale=215)
    enhanced = cv2.createCLAHE(clipLimit=3.2, tileGridSize=(8, 8)).apply(flattened)
    blurred = cv2.GaussianBlur(enhanced, (0, 0), 1.1)
    sharpened = cv2.addWeighted(enhanced, 1.55, blurred, -0.55, 0)
    return sharpened


def _thumbnail(rgb: Any) -> Any:
    cv2, _, _, _ = _runtime()
    height, width = rgb.shape[:2]
    scale = min(1.0, 510.0 / max(width, height))
    inner_width = max(1, int(round(width * scale)))
    inner_height = max(1, int(round(height * scale)))
    if (inner_width, inner_height) != (width, height):
        inner = cv2.resize(rgb, (inner_width, inner_height), interpolation=cv2.INTER_AREA)
    else:
        inner = rgb.copy()
    return cv2.copyMakeBorder(
        inner,
        1,
        1,
        1,
        1,
        cv2.BORDER_CONSTANT,
        value=(58, 58, 58),
    )


def _encode_jpeg(array: Any, *, grayscale: bool = False, quality: int = 92) -> bytes:
    _, np, Image, _ = _runtime()
    try:
        mode = "L" if grayscale else "RGB"
        image = Image.fromarray(np.asarray(array, dtype=np.uint8), mode=mode)
        output = io.BytesIO()
        save_options: dict[str, Any] = {
            "format": "JPEG",
            "quality": quality,
            "optimize": False,
            "progressive": False,
        }
        if not grayscale:
            save_options["subsampling"] = 0
        image.save(output, **save_options)
        return output.getvalue()
    except (OSError, ValueError, MemoryError) as exc:
        raise RetryableImageProcessingError("Could not encode image derivative") from exc


def _quality_metrics(rgb: Any, *, page_confidence: float, spine_confidence: float) -> dict[str, Any]:
    cv2, np, _, _ = _runtime()
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    focus = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    mean = float(gray.mean())
    standard_deviation = float(gray.std())
    return {
        "page_boundary_confidence": round(page_confidence, 4),
        "spine_confidence": round(spine_confidence, 4),
        "focus_variance": round(focus, 3),
        "brightness_mean": round(mean, 3),
        "contrast_standard_deviation": round(standard_deviation, 3),
        "shadow_clip_fraction": round(float(np.mean(gray <= 3)), 6),
        "highlight_clip_fraction": round(float(np.mean(gray >= 252)), 6),
    }


def _mapping_grid(map_x: Any, map_y: Any, input_width: int, input_height: int) -> dict[str, Any]:
    _, np, _, _ = _runtime()
    rows = 17
    columns = 17
    y_indices = np.linspace(0, map_x.shape[0] - 1, rows).round().astype(int)
    x_indices = np.linspace(0, map_x.shape[1] - 1, columns).round().astype(int)
    coordinates: list[list[float]] = []
    for y in y_indices:
        for x in x_indices:
            coordinates.append(
                [
                    round(float(map_x[y, x]) / max(input_width - 1, 1), 8),
                    round(float(map_y[y, x]) / max(input_height - 1, 1), 8),
                ]
            )
    return {
        "columns": columns,
        "rows": rows,
        "direction": "display_normalized_to_projective_input_normalized",
        "interpolation": "bilinear",
        "coordinates_row_major": coordinates,
    }


class _PageDewarp034Adapter:
    """Small direct-API adapter around page-dewarp's cubic sheet model."""

    def __init__(self) -> None:
        from page_dewarp.dewarp import round_nearest_multiple
        from page_dewarp.image import WarpedImage
        from page_dewarp.normalisation import norm2pix
        from page_dewarp.options import Config
        from page_dewarp.projection import project_xy

        self.version = importlib.metadata.version("page-dewarp")
        self.Config = Config
        self.WarpedImage = WarpedImage
        self.norm2pix = norm2pix
        self.project_xy = project_xy
        self.round_nearest_multiple = round_nearest_multiple

    def __call__(self, rgb: Any, strength_percent: int) -> _NonlinearResult | None:
        cv2, np, Image, _ = _runtime()
        input_height, input_width = rgb.shape[:2]
        with tempfile.TemporaryDirectory(prefix="whl-page-dewarp-") as temp_dir:
            source_path = Path(temp_dir) / "source.png"
            Image.fromarray(rgb, mode="RGB").save(source_path, format="PNG")
            config = self.Config(
                OUTPUT_DIR=temp_dir,
                OUTPUT_FORMAT="png",
                OUTPUT_JSON=0,
                NO_BINARY=1,
                DEBUG_LEVEL=0,
                DEBUG_DEST="file",
                USE_BATCH="off",
                DEVICE="cpu",
                PAGE_MARGIN_X=max(4, min(50, input_width // 30)),
                PAGE_MARGIN_Y=max(4, min(20, input_height // 40)),
            )
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                warped = self.WarpedImage(source_path, config=config)
            if not warped.written or warped.params is None or warped.page_dims is None:
                return None

            output_height = 0.5 * warped.page_dims[1] * config.OUTPUT_ZOOM * input_height
            output_height = self.round_nearest_multiple(output_height, config.REMAP_DECIMATE)
            output_width = self.round_nearest_multiple(
                output_height * warped.page_dims[0] / warped.page_dims[1],
                config.REMAP_DECIMATE,
            )
            output_height = int(output_height)
            output_width = int(output_width)
            small_height, small_width = np.floor_divide(
                [output_height, output_width], config.REMAP_DECIMATE
            )
            page_x_range = np.linspace(0, warped.page_dims[0], int(small_width))
            page_y_range = np.linspace(0, warped.page_dims[1], int(small_height))
            page_x, page_y = np.meshgrid(page_x_range, page_y_range)
            page_points = np.column_stack((page_x.reshape(-1), page_y.reshape(-1))).astype(
                np.float32
            )
            image_points = self.project_xy(page_points, warped.params)
            image_points = self.norm2pix(rgb.shape, image_points, False)
            map_x = image_points[:, 0, 0].reshape(page_x.shape)
            map_y = image_points[:, 0, 1].reshape(page_y.shape)
            map_x = cv2.resize(map_x, (output_width, output_height), interpolation=cv2.INTER_CUBIC)
            map_y = cv2.resize(map_y, (output_width, output_height), interpolation=cv2.INTER_CUBIC)
            if not np.isfinite(map_x).all() or not np.isfinite(map_y).all():
                return None

            identity_x, identity_y = np.meshgrid(
                np.linspace(0, input_width - 1, output_width),
                np.linspace(0, input_height - 1, output_height),
            )
            strength = strength_percent / 100.0
            map_x = (identity_x * (1.0 - strength) + map_x * strength).astype(np.float32)
            map_y = (identity_y * (1.0 - strength) + map_y * strength).astype(np.float32)
            if not np.isfinite(warped.params).all() or not np.isfinite(warped.page_dims).all():
                return None
            output = cv2.remap(
                rgb,
                map_x,
                map_y,
                interpolation=cv2.INTER_CUBIC,
                borderMode=cv2.BORDER_REPLICATE,
            )
            rvec = config.RVEC_IDX
            tvec = config.TVEC_IDX
            cubic = config.CUBIC_IDX
            manifest = {
                "schema": "org.whl.image-processor.cubic-page-remap",
                "version": 1,
                "engine": {"name": "page-dewarp", "version": self.version},
                "model": "cubic_sheet",
                "strength_percent": strength_percent,
                "rotation_vector": [float(v) for v in warped.params[rvec[0] : rvec[1]]],
                "translation_vector": [float(v) for v in warped.params[tvec[0] : tvec[1]]],
                "cubic_parameters": [float(v) for v in warped.params[cubic[0] : cubic[1]]],
                "page_dimensions": [float(v) for v in warped.page_dims],
                "mapping_convention": (
                    "inverse remap: each display pixel samples the projective input; "
                    "the coarse grid uses normalized display-to-input coordinates"
                ),
                "inverse_mapping_grid": _mapping_grid(
                    map_x, map_y, input_width=input_width, input_height=input_height
                ),
                "re_ocr_required": True,
            }
            return _NonlinearResult(output, manifest)


@lru_cache(maxsize=1)
def _get_page_dewarp_adapter() -> Callable[[Any, int], _NonlinearResult | None] | None:
    try:
        return _PageDewarp034Adapter()
    except (ImportError, importlib.metadata.PackageNotFoundError):
        return None


def _grid_to_source_coordinates(grid: dict[str, Any], projective_h: tuple[float, ...]) -> dict[str, Any]:
    _, np, _, _ = _runtime()
    matrix = np.asarray(projective_h, dtype=np.float64).reshape(3, 3)
    if not np.isfinite(matrix).all():
        raise ValueError("Projective transform contains a non-finite value")
    inverse = np.linalg.inv(matrix)
    source_coordinates: list[list[float]] = []
    for x, y in grid["coordinates_row_major"]:
        if not math.isfinite(float(x)) or not math.isfinite(float(y)):
            raise ValueError("Nonlinear mapping grid contains a non-finite value")
        point = inverse @ np.array([float(x), float(y), 1.0])
        if abs(float(point[2])) < 1e-12:
            raise ValueError("Nonlinear mapping crosses the projective horizon")
        source_x = float(point[0] / point[2])
        source_y = float(point[1] / point[2])
        if not math.isfinite(source_x) or not math.isfinite(source_y):
            raise ValueError("Mapped source coordinate is non-finite")
        source_coordinates.append([round(source_x, 8), round(source_y, 8)])
    return {
        "columns": grid["columns"],
        "rows": grid["rows"],
        "direction": "display_normalized_to_exif_oriented_source_normalized",
        "interpolation": grid["interpolation"],
        "coordinates_row_major": source_coordinates,
    }


def _operation_result_order(
    requested: tuple[str, ...], applied: set[str], skipped_extra: list[str]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    applied_ordered = tuple(operation for operation in requested if operation in applied)
    skipped = [operation for operation in requested if operation not in applied]
    skipped.extend(skipped_extra)
    return applied_ordered, tuple(skipped)


def process_image(data: bytes, options: ProcessingOptions) -> ProcessedImage:
    """Process immutable image bytes into display, OCR, and thumbnail derivatives."""

    if not isinstance(data, bytes):
        raise PermanentImageInputError("Source image must be supplied as immutable bytes")
    source_sha256 = hashlib.sha256(data).hexdigest()
    unknown = [operation for operation in options.operations if operation not in _KNOWN_OPERATIONS]
    if unknown:
        raise PermanentImageInputError(f"Unsupported processing operation: {unknown[0]}")

    try:
        cv2, np, _, _ = _runtime()
        decoded = _decode_source(data, options)
        rgb = decoded.rgb
        source_height, source_width = rgb.shape[:2]
        source_to_current = np.eye(3, dtype=np.float64)
        applied: set[str] = set()
        skipped_extra: list[str] = []
        stages: list[dict[str, Any]] = [
            {
                "name": "decode_and_exif_transpose",
                "input_format": decoded.format_name,
                "raw_dimensions": [decoded.raw_width, decoded.raw_height],
                "orientation": decoded.orientation,
                "output_dimensions": [source_width, source_height],
            }
        ]
        page_confidence = 0.0
        spine_confidence = 0.0
        nonlinear_manifest: dict[str, Any] | None = None

        if "spine_crop" in options.operations:
            if options.role != "spine":
                skipped_extra.append("spine_crop.role_mismatch")
            else:
                rgb, spine_matrix, spine_stage = _crop_spine(rgb, options)
                source_to_current = spine_matrix @ source_to_current
                spine_confidence = float(spine_stage["confidence"])
                stages.append(spine_stage)
                if spine_stage["skipped"]:
                    skipped_extra.append("spine_crop.low_confidence")
                else:
                    applied.add("spine_crop")
        elif options.role in {"title_page", "cover"} and {
            "page_dewarp",
            "detected_margin_crop",
        }.intersection(options.operations):
            boundary = _detect_page_boundary(rgb)
            page_confidence = boundary.confidence
            page_quad = boundary.quad
            stages.append(
                {
                    "name": "page_boundary_detection",
                    "method": boundary.method,
                    "detected": boundary.detected,
                    "confidence": boundary.confidence,
                }
            )
            if page_quad is None:
                page_quad = np.array(
                    [
                        [0, 0],
                        [source_width - 1, 0],
                        [source_width - 1, source_height - 1],
                        [0, source_height - 1],
                    ],
                    dtype=np.float32,
                )
            padding = (
                options.margin_padding_percent
                if boundary.detected and "detected_margin_crop" in options.operations
                else 0
            )
            padded_quad = _expand_quad(page_quad, padding, source_width, source_height)
            rgb, page_matrix = _projective_warp(rgb, padded_quad, options)
            source_to_current = page_matrix @ source_to_current
            stages.append(
                {
                    "name": "projective_page_rectification",
                    "boundary_fallback": not boundary.detected,
                    "margin_padding_percent": padding,
                    "output_dimensions": [int(rgb.shape[1]), int(rgb.shape[0])],
                }
            )
            if "page_dewarp" in options.operations:
                applied.add("page_dewarp")
            if "detected_margin_crop" in options.operations:
                if boundary.detected:
                    applied.add("detected_margin_crop")
                else:
                    skipped_extra.append("detected_margin_crop.no_boundary")

            projective_h = _normalised_homography(
                source_to_current,
                source_width,
                source_height,
                int(rgb.shape[1]),
                int(rgb.shape[0]),
            )
            if "page_dewarp" in options.operations and options.dewarp_strength_percent > 0:
                if options.curvature_backend == "off":
                    skipped_extra.append("page_dewarp.nonlinear_disabled")
                else:
                    adapter = _get_page_dewarp_adapter()
                    if adapter is None:
                        skipped_extra.append("page_dewarp.nonlinear_unavailable")
                    else:
                        try:
                            nonlinear = adapter(rgb, options.dewarp_strength_percent)
                        except Exception as exc:  # optional backend must projectively fall back
                            nonlinear = None
                            skipped_extra.append(
                                f"page_dewarp.nonlinear_failed:{type(exc).__name__}"
                            )
                        if nonlinear is None:
                            if not any(
                                item.startswith("page_dewarp.nonlinear_failed")
                                for item in skipped_extra
                            ):
                                skipped_extra.append("page_dewarp.nonlinear_no_text_spans")
                        else:
                            candidate_manifest = nonlinear.manifest
                            inverse_grid = candidate_manifest.get("inverse_mapping_grid")
                            try:
                                if not isinstance(inverse_grid, dict):
                                    raise ValueError("Nonlinear result has no inverse mapping grid")
                                candidate_manifest["display_to_source_mapping_grid"] = (
                                    _grid_to_source_coordinates(inverse_grid, projective_h)
                                )
                            except (KeyError, TypeError, ValueError, np.linalg.LinAlgError) as exc:
                                skipped_extra.append(
                                    f"page_dewarp.nonlinear_invalid_mapping:{type(exc).__name__}"
                                )
                            else:
                                rgb = nonlinear.rgb
                                nonlinear_manifest = candidate_manifest
                                nonlinear_manifest["projective_pretransform"] = list(projective_h)
                                stages.append(
                                    {
                                        "name": "nonlinear_cubic_page_dewarp",
                                        "engine": nonlinear_manifest.get("engine"),
                                        "strength_percent": options.dewarp_strength_percent,
                                        "output_dimensions": [
                                            int(rgb.shape[1]),
                                            int(rgb.shape[0]),
                                        ],
                                    }
                                )
        elif "detected_margin_crop" in options.operations or "page_dewarp" in options.operations:
            skipped_extra.append("page_geometry.role_not_supported")

        previous_width, previous_height = int(rgb.shape[1]), int(rgb.shape[0])
        rgb, output_resize_matrix = _limit_output(rgb, options)
        if output_resize_matrix is not None:
            if nonlinear_manifest is None:
                source_to_current = output_resize_matrix @ source_to_current
            stages.append(
                {
                    "name": "bounded_output_resize",
                    "input_dimensions": [previous_width, previous_height],
                    "output_dimensions": [int(rgb.shape[1]), int(rgb.shape[0])],
                }
            )

        if "contrast_normalization" in options.operations:
            if options.contrast_strength_percent <= 0:
                skipped_extra.append("contrast_normalization.zero_strength")
            else:
                rgb = _normalise_colour(
                    rgb,
                    options.contrast_strength_percent,
                    options.paper_tone_retention_percent,
                )
                stages.append(
                    {
                        "name": "colour_preserving_contrast_normalization",
                        "strength_percent": options.contrast_strength_percent,
                        "paper_tone_retention_percent": options.paper_tone_retention_percent,
                    }
                )
                applied.add("contrast_normalization")

        output_height, output_width = map(int, rgb.shape[:2])
        if output_width < 1 or output_height < 1:
            raise RetryableImageProcessingError("Processing produced an empty image")
        projective_h = _normalised_homography(
            source_to_current,
            source_width,
            source_height,
            output_width,
            output_height,
        )
        source_to_display = None if nonlinear_manifest is not None else projective_h

        ocr = _ocr_derivative(rgb)
        thumbnail = _thumbnail(rgb)
        display_jpeg = _encode_jpeg(rgb, quality=92)
        ocr_jpeg = _encode_jpeg(ocr, grayscale=True, quality=94)
        thumbnail_jpeg = _encode_jpeg(thumbnail, quality=88)
        applied_ordered, skipped_ordered = _operation_result_order(
            options.operations, applied, skipped_extra
        )
        quality = _quality_metrics(
            rgb, page_confidence=page_confidence, spine_confidence=spine_confidence
        )
        quality["nonlinear_curvature_applied"] = nonlinear_manifest is not None
        quality["page_boundary_detected"] = page_confidence > 0
        quality["spine_crop_skipped"] = any(
            stage.get("name") == "spine_crop" and stage.get("skipped") for stage in stages
        )

        transform_manifest: dict[str, Any] = {
            "schema": "org.whl.image-processor.transform",
            "version": 1,
            "kind": "nonlinear" if nonlinear_manifest is not None else "projective",
            "source_coordinate_space": "exif_oriented_normalized",
            "display_coordinate_space": "normalized",
            "source_dimensions": [source_width, source_height],
            "output_dimensions": [output_width, output_height],
            "source_to_display_homography": (
                None if source_to_display is None else list(source_to_display)
            ),
            "mapping_convention": (
                "row-major 3x3 maps normalized EXIF-oriented source coordinates "
                "to normalized display coordinates"
                if source_to_display is not None
                else "nonlinear inverse grid maps display coordinates back to source coordinates"
            ),
            "re_ocr_required": nonlinear_manifest is not None,
            "stages": stages,
        }
        if nonlinear_manifest is not None:
            transform_manifest["nonlinear"] = nonlinear_manifest

        return ProcessedImage(
            display_jpeg=display_jpeg,
            ocr_jpeg=ocr_jpeg,
            thumbnail_jpeg=thumbnail_jpeg,
            source_width=source_width,
            source_height=source_height,
            output_width=output_width,
            output_height=output_height,
            thumbnail_width=int(thumbnail.shape[1]),
            thumbnail_height=int(thumbnail.shape[0]),
            source_sha256=source_sha256,
            display_sha256=hashlib.sha256(display_jpeg).hexdigest(),
            ocr_sha256=hashlib.sha256(ocr_jpeg).hexdigest(),
            thumbnail_sha256=hashlib.sha256(thumbnail_jpeg).hexdigest(),
            applied_operations=applied_ordered,
            skipped_operations=skipped_ordered,
            source_to_display_homography=source_to_display,
            transform_manifest=transform_manifest,
            quality=quality,
        )
    except (PermanentImageInputError, RetryableImageProcessingError):
        raise
    except MemoryError as exc:
        raise RetryableImageProcessingError("Insufficient memory during image processing") from exc
    except cv2.error as exc:
        raise RetryableImageProcessingError("OpenCV could not complete image processing") from exc
    except Exception as exc:
        raise RetryableImageProcessingError(
            f"Unexpected image processing failure: {type(exc).__name__}"
        ) from exc

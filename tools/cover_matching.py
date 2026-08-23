"""Compact, explainable matching for photographed book covers.

The physical-scan capture flow cannot retain a cover photograph merely to
match it later.  This module turns the photograph into a small, versioned
signature and ranks existing catalogue captures from that signature plus OCR
text.  The visual evidence deliberately uses several independent signals:

* hue and RGB chromaticity, which are substantially invariant to exposure;
* contrast-normalized tone and perceptual-difference features;
* edge-density and gradient-orientation features; and
* aspect ratio as a low-weight sanity check.

Signatures contain aggregate measurements only.  They contain no encoded
image, thumbnail, path, or original dimensions and have a strict serialized
size limit suitable for a PostgreSQL ``jsonb`` column or Kotlin JSON object.
"""
from __future__ import annotations

import colorsys
import json
import math
import re
import unicodedata
import warnings
from collections.abc import Iterable, Mapping, Sequence
from difflib import SequenceMatcher
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps, UnidentifiedImageError


SIGNATURE_VERSION = 1
SIGNATURE_ALGORITHM = "whl-cover-v1"
SIGNATURE_MAX_JSON_BYTES = 4096
MATCH_EVIDENCE_VERSION = 1
MAX_SOURCE_BYTES = 32 * 1024 * 1024
MAX_SOURCE_PIXELS = 40_000_000
MAX_RANK_CANDIDATES = 10_000
MAX_CANDIDATE_ID_CHARS = 200
MAX_CANDIDATE_TITLE_CHARS = 4_000
MAX_CANDIDATE_AUTHOR_CHARS = 4_000
MAX_CANDIDATE_YEAR_CHARS = 200
MAX_CANDIDATE_OCR_CHARS = 32_000
MAX_SESSION_ROWS = 100
AMBIGUITY_MARGIN_THRESHOLD = 0.05
AMBIGUOUS_CONFIDENCE_CAP = 0.79

_WIDTH = 48
_HEIGHT = 64
_GRID_COLUMNS = 6
_GRID_ROWS = 8
_GRID_SIZE = _GRID_COLUMNS * _GRID_ROWS
_HUE_BINS = 12
_CHROMA_BINS_PER_AXIS = 4
_CHROMA_BINS = _CHROMA_BINS_PER_AXIS**2
_GRADIENT_BINS = 8
_DHASH_BITS = 64
_TOKEN_RE = re.compile(r"[^a-z0-9]+")

_REQUIRED_ARRAY_LENGTHS = {
    "hue_hist": _HUE_BINS,
    "chroma_hist": _CHROMA_BINS,
    "chroma_grid": _GRID_SIZE * 3,
    "tone_grid": _GRID_SIZE,
    "edge_grid": _GRID_SIZE,
    "gradient_hist": _GRADIENT_BINS,
}
_SIGNATURE_KEYS = {
    "version",
    "algorithm",
    "aspect_milli",
    *_REQUIRED_ARRAY_LENGTHS,
    "dhash",
}


class CoverSignatureError(ValueError):
    """Raised when a cover image or signature cannot be safely processed."""


def _bounded_int(value: object, *, minimum: int, maximum: int, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CoverSignatureError(f"{label} must be an integer")
    if not minimum <= value <= maximum:
        raise CoverSignatureError(f"{label} must be between {minimum} and {maximum}")
    return value


def _open_rgb(image: Image.Image | bytes | bytearray | memoryview | str | Path) -> Image.Image:
    source: Image.Image | None = None
    upright: Image.Image | None = None
    try:
        if isinstance(image, Image.Image):
            source = image.copy()
        elif isinstance(image, (bytes, bytearray, memoryview)):
            encoded = bytes(image)
            if len(encoded) > MAX_SOURCE_BYTES:
                raise CoverSignatureError("cover image exceeds the encoded-size limit")
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                source = Image.open(BytesIO(encoded))
        elif isinstance(image, (str, Path)):
            path = Path(image)
            if path.stat().st_size > MAX_SOURCE_BYTES:
                raise CoverSignatureError("cover image exceeds the encoded-size limit")
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                source = Image.open(path)
        else:
            raise CoverSignatureError("cover image must be a Pillow image, bytes, or path")

        if source.width <= 0 or source.height <= 0:
            raise CoverSignatureError("cover image has invalid dimensions")
        if source.width * source.height > MAX_SOURCE_PIXELS:
            raise CoverSignatureError("cover image exceeds the pixel limit")
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            upright = ImageOps.exif_transpose(source)
        rgb = upright.convert("RGB")
        return rgb
    except CoverSignatureError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise CoverSignatureError("cover image exceeds the pixel safety limit") from exc
    except (OSError, ValueError, UnidentifiedImageError) as exc:
        raise CoverSignatureError("cover image could not be decoded") from exc
    finally:
        if upright is not None and upright is not source:
            upright.close()
        if source is not None:
            source.close()


def _fit(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    return ImageOps.fit(
        image,
        size,
        method=Image.Resampling.LANCZOS,
        centering=(0.5, 0.5),
    )


def _image_data(image: Image.Image) -> list[Any]:
    # Pillow 12.1 renamed getdata() but the project still supports Pillow 10.
    flattened = getattr(image, "get_flattened_data", None)
    return list(flattened() if flattened is not None else image.getdata())


def _quantize_distribution(values: Sequence[float], total: int = 255) -> list[int]:
    summed = float(sum(values))
    if summed <= 0:
        return [0] * len(values)
    exact = [max(0.0, float(value)) * total / summed for value in values]
    output = [int(value) for value in exact]
    remainder = total - sum(output)
    order = sorted(range(len(values)), key=lambda i: (exact[i] - output[i], -i), reverse=True)
    for index in order[:remainder]:
        output[index] += 1
    return output


def _percentile(values: Sequence[int | float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(float(value) for value in values)
    position = max(0.0, min(1.0, fraction)) * (len(ordered) - 1)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _normalized_luminance(pixels: Sequence[tuple[int, int, int]]) -> list[int]:
    luminance = [round(0.299 * red + 0.587 * green + 0.114 * blue) for red, green, blue in pixels]
    low = _percentile(luminance, 0.03)
    high = _percentile(luminance, 0.97)
    spread = high - low
    if spread < 8.0:
        return [128] * len(luminance)
    return [round(255 * max(0.0, min(1.0, (value - low) / spread))) for value in luminance]


def _grid_means(values: Sequence[float], *, width: int = _WIDTH, height: int = _HEIGHT) -> list[float]:
    cells: list[float] = []
    cell_width = width // _GRID_COLUMNS
    cell_height = height // _GRID_ROWS
    for grid_y in range(_GRID_ROWS):
        for grid_x in range(_GRID_COLUMNS):
            samples = []
            for y in range(grid_y * cell_height, (grid_y + 1) * cell_height):
                offset = y * width
                samples.extend(values[offset + grid_x * cell_width:offset + (grid_x + 1) * cell_width])
            cells.append(sum(samples) / len(samples) if samples else 0.0)
    return cells


def _color_features(pixels: Sequence[tuple[int, int, int]]) -> tuple[list[int], list[int], list[int]]:
    hue = [0.0] * _HUE_BINS
    chroma = [0.0] * _CHROMA_BINS
    cell_values: list[list[tuple[float, float, float]]] = [[] for _ in range(_GRID_SIZE)]
    cell_width = _WIDTH // _GRID_COLUMNS
    cell_height = _HEIGHT // _GRID_ROWS

    for index, (red, green, blue) in enumerate(pixels):
        red_unit, green_unit, blue_unit = red / 255.0, green / 255.0, blue / 255.0
        hue_value, saturation, _ = colorsys.rgb_to_hsv(red_unit, green_unit, blue_unit)
        if saturation >= 0.06:
            hue[min(_HUE_BINS - 1, int(hue_value * _HUE_BINS))] += saturation

        channel_sum = red + green + blue
        if channel_sum >= 24:
            red_chroma = red / channel_sum
            green_chroma = green / channel_sum
            # Most RGB chromaticities fall in [0, .8].  Reserving the final
            # bin for highly saturated values gives useful resolution near
            # neutral colors without allowing brightness into the feature.
            red_bin = min(_CHROMA_BINS_PER_AXIS - 1, int(red_chroma * 5.0))
            green_bin = min(_CHROMA_BINS_PER_AXIS - 1, int(green_chroma * 5.0))
            chroma[green_bin * _CHROMA_BINS_PER_AXIS + red_bin] += 1.0
            x = index % _WIDTH
            y = index // _WIDTH
            cell = (y // cell_height) * _GRID_COLUMNS + (x // cell_width)
            cell_values[cell].append((red_chroma, green_chroma, saturation))

    spatial: list[int] = []
    for samples in cell_values:
        if not samples:
            spatial.extend((85, 85, 0))
            continue
        count = len(samples)
        spatial.extend(
            (
                round(255 * sum(sample[0] for sample in samples) / count),
                round(255 * sum(sample[1] for sample in samples) / count),
                round(255 * sum(sample[2] for sample in samples) / count),
            )
        )
    return _quantize_distribution(hue), _quantize_distribution(chroma), spatial


def _structure_features(luminance: Sequence[int]) -> tuple[list[int], list[int], list[int], str]:
    tone_grid = [round(value) for value in _grid_means(luminance)]
    magnitudes = [0.0] * (_WIDTH * _HEIGHT)
    directions = [0.0] * _GRADIENT_BINS
    for y in range(1, _HEIGHT - 1):
        for x in range(1, _WIDTH - 1):
            index = y * _WIDTH + x
            dx = luminance[index + 1] - luminance[index - 1]
            dy = luminance[index + _WIDTH] - luminance[index - _WIDTH]
            magnitude = math.hypot(dx, dy)
            magnitudes[index] = magnitude
            if magnitude >= 4.0:
                angle = math.atan2(dy, dx) % math.pi
                direction = min(_GRADIENT_BINS - 1, int(angle * _GRADIENT_BINS / math.pi))
                directions[direction] += magnitude

    nonzero = [magnitude for magnitude in magnitudes if magnitude > 0.0]
    scale = max(12.0, _percentile(nonzero, 0.90))
    edge_grid = [round(255 * min(1.0, value / scale)) for value in _grid_means(magnitudes)]

    # Difference hash is computed from the already exposure-normalized tone.
    tone_image = Image.new("L", (_WIDTH, _HEIGHT))
    tone_image.putdata(luminance)
    small = tone_image.resize((9, 8), Image.Resampling.BILINEAR)
    small_values = _image_data(small)
    bits = 0
    for row in range(8):
        for column in range(8):
            bits <<= 1
            bits |= int(small_values[row * 9 + column] < small_values[row * 9 + column + 1])
    tone_image.close()
    small.close()
    return tone_grid, edge_grid, _quantize_distribution(directions), f"{bits:016x}"


def build_visual_signature(
    image: Image.Image | bytes | bytearray | memoryview | str | Path,
) -> dict[str, Any]:
    """Return a deterministic, non-reversible aggregate signature for ``image``.

    The output is directly JSON serializable and always satisfies
    :data:`SIGNATURE_MAX_JSON_BYTES`.  The input image is closed internally
    only when this function opened or copied it; a caller-owned Pillow image
    remains usable.
    """
    rgb = _open_rgb(image)
    try:
        aspect_milli = round(1000 * rgb.width / rgb.height)
        sample = _fit(rgb, (_WIDTH, _HEIGHT))
        try:
            pixels = _image_data(sample)
        finally:
            sample.close()
    finally:
        rgb.close()

    hue_hist, chroma_hist, chroma_grid = _color_features(pixels)
    luminance = _normalized_luminance(pixels)
    tone_grid, edge_grid, gradient_hist, dhash = _structure_features(luminance)
    signature: dict[str, Any] = {
        "version": SIGNATURE_VERSION,
        "algorithm": SIGNATURE_ALGORITHM,
        "aspect_milli": min(4000, max(250, aspect_milli)),
        "hue_hist": hue_hist,
        "chroma_hist": chroma_hist,
        "chroma_grid": chroma_grid,
        "tone_grid": tone_grid,
        "edge_grid": edge_grid,
        "gradient_hist": gradient_hist,
        "dhash": dhash,
    }
    # Parsing also provides a single, strict validation path for signatures
    # produced in Python, Kotlin, or read back from jsonb.
    return parse_visual_signature(signature)


def serialize_visual_signature(signature: Mapping[str, object] | str) -> str:
    """Validate and serialize a signature using a canonical compact encoding."""
    parsed = parse_visual_signature(signature)
    encoded = json.dumps(parsed, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    if len(encoded.encode("utf-8")) > SIGNATURE_MAX_JSON_BYTES:
        raise CoverSignatureError("cover signature exceeds the serialized-size limit")
    return encoded


def parse_visual_signature(signature: Mapping[str, object] | str) -> dict[str, Any]:
    """Parse a Kotlin/PostgreSQL-friendly JSON object and enforce all bounds."""
    if isinstance(signature, str):
        if len(signature.encode("utf-8")) > SIGNATURE_MAX_JSON_BYTES:
            raise CoverSignatureError("cover signature exceeds the serialized-size limit")
        try:
            raw = json.loads(signature)
        except json.JSONDecodeError as exc:
            raise CoverSignatureError("cover signature is not valid JSON") from exc
    elif isinstance(signature, Mapping):
        raw = dict(signature)
    else:
        raise CoverSignatureError("cover signature must be a JSON object or string")
    if not isinstance(raw, dict):
        raise CoverSignatureError("cover signature must be a JSON object")
    try:
        raw_size = len(
            json.dumps(raw, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("utf-8")
        )
    except (TypeError, ValueError) as exc:
        raise CoverSignatureError("cover signature contains a non-JSON value") from exc
    if raw_size > SIGNATURE_MAX_JSON_BYTES:
        raise CoverSignatureError("cover signature exceeds the serialized-size limit")
    unknown = set(raw) - _SIGNATURE_KEYS
    if unknown:
        raise CoverSignatureError(f"cover signature contains unknown fields: {', '.join(sorted(unknown))}")
    if raw.get("version") != SIGNATURE_VERSION:
        raise CoverSignatureError(f"unsupported cover signature version: {raw.get('version')!r}")
    if raw.get("algorithm") != SIGNATURE_ALGORITHM:
        raise CoverSignatureError("unsupported cover signature algorithm")

    parsed: dict[str, Any] = {
        "version": SIGNATURE_VERSION,
        "algorithm": SIGNATURE_ALGORITHM,
        "aspect_milli": _bounded_int(
            raw.get("aspect_milli"), minimum=250, maximum=4000, label="aspect_milli"
        ),
    }
    for name, length in _REQUIRED_ARRAY_LENGTHS.items():
        values = raw.get(name)
        if not isinstance(values, list) or len(values) != length:
            raise CoverSignatureError(f"{name} must contain exactly {length} values")
        parsed[name] = [
            _bounded_int(value, minimum=0, maximum=255, label=f"{name}[{index}]")
            for index, value in enumerate(values)
        ]
        if name in {"hue_hist", "chroma_hist", "gradient_hist"} and sum(parsed[name]) not in {
            0,
            255,
        }:
            raise CoverSignatureError(f"{name} must be empty or normalized to 255")
    dhash = raw.get("dhash")
    if not isinstance(dhash, str) or not re.fullmatch(r"[0-9a-f]{16}", dhash):
        raise CoverSignatureError("dhash must be 16 lowercase hexadecimal characters")
    parsed["dhash"] = dhash

    encoded = json.dumps(parsed, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    if len(encoded.encode("utf-8")) > SIGNATURE_MAX_JSON_BYTES:
        raise CoverSignatureError("cover signature exceeds the serialized-size limit")
    return parsed


def _distribution_similarity(left: Sequence[int], right: Sequence[int], *, circular: bool = False) -> float:
    def smooth(values: Sequence[int], offset: int = 0) -> list[float]:
        count = len(values)
        rotated = [values[(index + offset) % count] for index in range(count)]
        if not circular:
            return [float(value) for value in rotated]
        return [
            0.2 * rotated[(index - 1) % count]
            + 0.6 * rotated[index]
            + 0.2 * rotated[(index + 1) % count]
            for index in range(count)
        ]

    left_values = smooth(left)
    left_sum = sum(left_values)
    right_sum = sum(right)
    if left_sum <= 0 and right_sum <= 0:
        return 1.0
    if left_sum <= 0 or right_sum <= 0:
        return 0.0
    offsets = (-1, 0, 1) if circular else (0,)
    best = 0.0
    for offset in offsets:
        right_values = smooth(right, offset)
        right_total = sum(right_values) or 1.0
        coefficient = sum(
            math.sqrt((a / left_sum) * (b / right_total))
            for a, b in zip(left_values, right_values, strict=True)
        )
        best = max(best, coefficient)
    return min(1.0, max(0.0, best))


def _cosine_similarity(left: Sequence[int], right: Sequence[int]) -> float:
    numerator = sum(float(a) * float(b) for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(float(value) ** 2 for value in left))
    right_norm = math.sqrt(sum(float(value) ** 2 for value in right))
    if left_norm == 0.0 and right_norm == 0.0:
        return 1.0
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return min(1.0, max(0.0, numerator / (left_norm * right_norm)))


def _grid_similarity(left: Sequence[int], right: Sequence[int]) -> float:
    cosine = _cosine_similarity(left, right)
    absolute = 1.0 - sum(abs(a - b) for a, b in zip(left, right, strict=True)) / (255 * len(left))
    return min(1.0, max(0.0, 0.7 * cosine + 0.3 * absolute))


def _tone_similarity(left: Sequence[int], right: Sequence[int]) -> float:
    left_mean = sum(left) / len(left)
    right_mean = sum(right) / len(right)
    centered_left = [value - left_mean for value in left]
    centered_right = [value - right_mean for value in right]
    # Direct covariance gives the useful [-1, 1] structural correlation.
    covariance = sum(a * b for a, b in zip(centered_left, centered_right, strict=True))
    variance = math.sqrt(
        sum(value**2 for value in centered_left) * sum(value**2 for value in centered_right)
    )
    correlation = covariance / variance if variance else (1.0 if left == right else 0.0)
    correlation_score = (max(-1.0, min(1.0, correlation)) + 1.0) / 2.0
    absolute = 1.0 - sum(abs(a - b) for a, b in zip(left, right, strict=True)) / (255 * len(left))
    return min(1.0, max(0.0, 0.7 * correlation_score + 0.3 * absolute))


def _chroma_grid_similarity(left: Sequence[int], right: Sequence[int]) -> float:
    distances = []
    maximum = math.sqrt(255**2 + 255**2 + (0.5 * 255) ** 2)
    for index in range(0, len(left), 3):
        red_distance = left[index] - right[index]
        green_distance = left[index + 1] - right[index + 1]
        saturation_distance = 0.5 * (left[index + 2] - right[index + 2])
        distances.append(math.sqrt(red_distance**2 + green_distance**2 + saturation_distance**2))
    return min(1.0, max(0.0, 1.0 - sum(distances) / (maximum * len(distances))))


def _dhash_similarity(left: str, right: str) -> float:
    distance = (int(left, 16) ^ int(right, 16)).bit_count()
    return 1.0 - distance / _DHASH_BITS


def compare_visual_signatures(
    query: Mapping[str, object] | str,
    candidate: Mapping[str, object] | str,
) -> dict[str, float]:
    """Compare two signatures and return exposure-tolerant component scores."""
    left = parse_visual_signature(query)
    right = parse_visual_signature(candidate)
    hue = _distribution_similarity(left["hue_hist"], right["hue_hist"], circular=True)
    chroma = _distribution_similarity(left["chroma_hist"], right["chroma_hist"])
    chroma_spatial = _chroma_grid_similarity(left["chroma_grid"], right["chroma_grid"])
    color = 0.40 * hue + 0.32 * chroma + 0.28 * chroma_spatial

    tone = _tone_similarity(left["tone_grid"], right["tone_grid"])
    perceptual = _dhash_similarity(left["dhash"], right["dhash"])
    edge = _grid_similarity(left["edge_grid"], right["edge_grid"])
    structure = 0.55 * tone + 0.30 * perceptual + 0.15 * edge
    orientation = _distribution_similarity(left["gradient_hist"], right["gradient_hist"], circular=True)
    gradient = 0.55 * orientation + 0.45 * edge

    aspect_ratio = min(left["aspect_milli"], right["aspect_milli"]) / max(
        left["aspect_milli"], right["aspect_milli"]
    )
    visual = 0.34 * color + 0.40 * structure + 0.22 * gradient + 0.04 * aspect_ratio
    return {
        "color": round(min(1.0, max(0.0, color)), 4),
        "structure": round(min(1.0, max(0.0, structure)), 4),
        "gradient": round(min(1.0, max(0.0, gradient)), 4),
        "aspect": round(min(1.0, max(0.0, aspect_ratio)), 4),
        "visual": round(min(1.0, max(0.0, visual)), 4),
    }


def _normalize_text(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode("ascii")
    return " ".join(_TOKEN_RE.sub(" ", text.lower()).split())


def _title_evidence(query_text: str, candidate_title: str) -> float | None:
    query_tokens = _normalize_text(query_text).split()
    title_tokens = _normalize_text(candidate_title).split()
    if not query_tokens or not title_tokens:
        return None
    title_set = set(title_tokens)
    query_set = set(query_tokens)
    coverage = len(title_set & query_set) / len(title_set)
    target_length = len(title_tokens)
    windows: list[list[str]] = []
    for length in range(max(1, target_length - 2), target_length + 3):
        windows.extend(query_tokens[start:start + length] for start in range(max(1, len(query_tokens) - length + 1)))
    ordered = max(
        (SequenceMatcher(None, " ".join(title_tokens), " ".join(window)).ratio() for window in windows),
        default=0.0,
    )
    score = 0.62 * coverage + 0.38 * ordered
    if len(title_tokens) == 1 and coverage < 1.0:
        score *= 0.65
    return min(1.0, max(0.0, score))


def _ocr_evidence(query_text: str, candidate_text: str) -> float | None:
    query_tokens = _normalize_text(query_text).split()
    candidate_tokens = _normalize_text(candidate_text).split()
    if not query_tokens or not candidate_tokens:
        return None
    query_set, candidate_set = set(query_tokens), set(candidate_tokens)
    intersection = len(query_set & candidate_set)
    overlap = intersection / max(1, min(len(query_set), len(candidate_set)))
    union = intersection / max(1, len(query_set | candidate_set))
    return min(1.0, max(0.0, 0.72 * overlap + 0.28 * union))


def _author_evidence(query_text: str, candidate_author: str) -> float | None:
    query_tokens = set(_normalize_text(query_text).split())
    author_tokens = set(_normalize_text(candidate_author).split())
    if not query_tokens or not author_tokens:
        return None
    return len(query_tokens & author_tokens) / len(author_tokens)


def _year_evidence(query_text: str, candidate_year: str) -> float | None:
    year_pattern = r"(?<!\d)(?:1\d{3}|20\d{2}|21\d{2})(?!\d)"
    candidate_years = set(re.findall(year_pattern, _normalize_text(candidate_year)))
    query_years = set(re.findall(year_pattern, _normalize_text(query_text)))
    if not candidate_years or not query_years:
        return None
    return 1.0 if candidate_years & query_years else 0.0


def _text_match_evidence(
    query_ocr_text: str,
    candidate_title: str = "",
    candidate_ocr_text: str = "",
    *,
    candidate_author: str = "",
    candidate_year: str = "",
) -> tuple[float | None, dict[str, float | None]]:
    title = _title_evidence(query_ocr_text, candidate_title)
    ocr = _ocr_evidence(query_ocr_text, candidate_ocr_text)
    author = _author_evidence(query_ocr_text, candidate_author)
    year = _year_evidence(query_ocr_text, candidate_year)
    details = {
        "title": round(title, 4) if title is not None else None,
        "candidate_ocr": round(ocr, 4) if ocr is not None else None,
        "author": round(author, 4) if author is not None else None,
        "year": round(year, 4) if year is not None else None,
    }
    if title is None:
        primary = ocr
    elif ocr is None:
        primary = title
    else:
        primary = 0.75 * title + 0.25 * ocr
    if primary is None:
        return None, details

    # Author and year are corroborators rather than prerequisites: covers may
    # omit either.  Only metadata actually present on the candidate reserves
    # weight, and a query that does not expose it receives a neutral 0.5.
    author_weight = 0.12 if _normalize_text(candidate_author) else 0.0
    year_weight = 0.06 if re.search(
        r"(?<!\d)(?:1\d{3}|20\d{2}|21\d{2})(?!\d)",
        _normalize_text(candidate_year),
    ) else 0.0
    author_score = 0.5 if author is None else author
    year_score = 0.5 if year is None else year
    combined = (
        (1.0 - author_weight - year_weight) * primary
        + author_weight * author_score
        + year_weight * year_score
    )
    return round(min(1.0, max(0.0, combined)), 4), details


def text_match_score(
    query_ocr_text: str,
    candidate_title: str = "",
    candidate_ocr_text: str = "",
    *,
    candidate_author: str = "",
    candidate_year: str = "",
) -> float | None:
    """Return OCR/bibliographic agreement, or ``None`` without text evidence."""
    score, _details = _text_match_evidence(
        query_ocr_text,
        candidate_title,
        candidate_ocr_text,
        candidate_author=candidate_author,
        candidate_year=candidate_year,
    )
    return score


def _reason_list(*, text: float | None, components: Mapping[str, float]) -> list[str]:
    reasons = []
    if text is None:
        reasons.append("No OCR/title evidence was available; confidence is visual-only.")
    elif text >= 0.82:
        reasons.append("OCR/title evidence strongly agrees.")
    elif text >= 0.58:
        reasons.append("OCR/title evidence partially agrees and needs review.")
    else:
        reasons.append("OCR/title evidence is weak or conflicts.")

    color = components["color"]
    if color >= 0.78:
        reasons.append("Exposure-normalized color evidence corroborates the cover.")
    elif color < 0.48:
        reasons.append("Color/chromaticity differs from the candidate cover.")
    else:
        reasons.append("Color/chromaticity provides only partial corroboration.")
    structure = components["structure"]
    gradient = components["gradient"]
    if min(structure, gradient) >= 0.72:
        reasons.append("Perceptual structure and edge features corroborate the cover.")
    elif min(structure, gradient) < 0.48:
        reasons.append("Structural or edge features conflict with the candidate cover.")
    else:
        reasons.append("Structural and edge evidence is mixed.")
    return reasons[:4]


def _combined_match_evidence(
    text: float | None,
    visual_components: Mapping[str, float] | None,
    *,
    text_details: Mapping[str, float | None] | None = None,
) -> tuple[float, dict[str, Any]]:
    if text is None and visual_components is None:
        raise CoverSignatureError("matching requires text or visual evidence")
    if visual_components is None:
        confidence = min(0.70, 0.72 * float(text))
        components: dict[str, float | None] = {"text": round(float(text), 4)}
        reasons = ["Visual corroboration was unavailable; confidence is capped for review."]
    else:
        visual = visual_components["visual"]
        components = {
            "text": round(text, 4) if text is not None else None,
            **{name: round(float(value), 4) for name, value in visual_components.items()},
        }
        if text is None:
            confidence = 0.88 * visual
        else:
            confidence = 0.56 * text + 0.44 * visual - 0.10 * abs(text - visual)
            visual_corroboration = min(
                visual_components["color"],
                visual_components["structure"],
            )
            if text >= 0.78 and (visual < 0.35 or visual_corroboration < 0.40):
                confidence = min(confidence, 0.60)
            elif text >= 0.78 and (visual < 0.55 or visual_corroboration < 0.65):
                confidence = min(confidence, 0.72)
        reasons = _reason_list(text=text, components=visual_components)

    confidence = round(min(1.0, max(0.0, confidence)), 4)
    band = "likely" if confidence >= 0.82 else "review" if confidence >= 0.58 else "unlikely"
    evidence = {
        "version": MATCH_EVIDENCE_VERSION,
        "components": components,
        "band": band,
        "reasons": reasons,
    }
    if text_details is not None:
        evidence["text_evidence"] = dict(text_details)
    return confidence, evidence


def score_cover_match(
    *,
    query_ocr_text: str = "",
    query_signature: Mapping[str, object] | str | None = None,
    candidate_title: str = "",
    candidate_author: str = "",
    candidate_year: str = "",
    candidate_ocr_text: str = "",
    candidate_signature: Mapping[str, object] | str | None = None,
) -> tuple[float, dict[str, Any]]:
    """Score one candidate and return ``(confidence, explainable evidence)``.

    A high OCR score cannot by itself produce a high-confidence match when
    visual evidence conflicts.  Conversely, a visual-only cover can be queued
    for review but is capped below automatic-approval confidence.
    """
    text, text_details = _text_match_evidence(
        query_ocr_text,
        candidate_title,
        candidate_ocr_text,
        candidate_author=candidate_author,
        candidate_year=candidate_year,
    )
    visual_components: dict[str, float] | None = None
    if query_signature is not None or candidate_signature is not None:
        if query_signature is None or candidate_signature is None:
            raise CoverSignatureError("both query and candidate signatures are required")
        visual_components = compare_visual_signatures(query_signature, candidate_signature)
    return _combined_match_evidence(
        text,
        visual_components,
        text_details=text_details,
    )


def rank_cover_matches(
    *,
    candidates: Iterable[Mapping[str, object]],
    query_ocr_text: str = "",
    query_signature: Mapping[str, object] | str | None = None,
    query_image: Image.Image | bytes | bytearray | memoryview | str | Path | None = None,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Rank catalogue/capture candidates without returning any source images.

    Each candidate must have ``capture_id`` (``id`` is accepted as a
    compatibility alias), may have ``title`` and ``ocr_text``, and supplies
    visual evidence as either ``visual_signature`` or ``cover_image``.  The
    latter is signed in memory and is never included in the result.
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError("limit must be an integer between 1 and 100")
    if query_signature is not None and query_image is not None:
        raise CoverSignatureError("provide query_signature or query_image, not both")
    if query_image is not None:
        query_signature = build_visual_signature(query_image)

    ranked = []
    for candidate_index, raw in enumerate(candidates):
        if candidate_index >= MAX_RANK_CANDIDATES:
            raise ValueError(f"cover matching is limited to {MAX_RANK_CANDIDATES} candidates")
        candidate = dict(raw)
        capture_id = str(candidate.get("capture_id") or candidate.get("id") or "").strip()
        if not capture_id or len(capture_id) > MAX_CANDIDATE_ID_CHARS:
            raise ValueError("each cover candidate requires capture_id")
        title = str(candidate.get("title") or "")
        author = str(candidate.get("author") or "")
        year = str(candidate.get("year") or "")
        candidate_ocr = str(candidate.get("ocr_text") or "")
        if (
            len(title) > MAX_CANDIDATE_TITLE_CHARS
            or len(author) > MAX_CANDIDATE_AUTHOR_CHARS
            or len(year) > MAX_CANDIDATE_YEAR_CHARS
            or len(candidate_ocr) > MAX_CANDIDATE_OCR_CHARS
        ):
            raise ValueError("cover candidate metadata exceeds its size limit")
        signature = candidate.get("visual_signature")
        if signature is None and candidate.get("cover_image") is not None:
            signature = build_visual_signature(candidate["cover_image"])
        paired_query = query_signature if query_signature is not None and signature is not None else None
        paired_candidate = signature if paired_query is not None else None
        text = text_match_score(
            query_ocr_text,
            title,
            candidate_ocr,
            candidate_author=author,
            candidate_year=year,
        )
        if text is None and paired_query is None:
            continue
        confidence, evidence = score_cover_match(
            query_ocr_text=query_ocr_text,
            query_signature=paired_query,
            candidate_title=title,
            candidate_author=author,
            candidate_year=year,
            candidate_ocr_text=candidate_ocr,
            candidate_signature=paired_candidate,
        )
        ranked.append(
            {
                "candidate_capture_id": capture_id,
                "title": title,
                "author": author,
                "year": year,
                "match_confidence": confidence,
                "match_evidence": evidence,
            }
        )

    ranked.sort(key=lambda item: (-item["match_confidence"], item["candidate_capture_id"]))
    for rank, item in enumerate(ranked[:limit], 1):
        item["rank"] = rank
    return ranked[:limit]


def _candidate_signatures(candidate: Mapping[str, object]) -> list[dict[str, Any]]:
    signatures: list[dict[str, Any]] = []
    raw_many = candidate.get("visual_signatures")
    if raw_many is not None:
        if isinstance(raw_many, (str, bytes, bytearray, Mapping)) or not isinstance(raw_many, Iterable):
            raise CoverSignatureError("visual_signatures must be an array")
        signatures.extend(parse_visual_signature(value) for value in raw_many)
    raw_one = candidate.get("visual_signature")
    if raw_one is not None:
        signatures.append(parse_visual_signature(raw_one))

    image_many = candidate.get("cover_images")
    if image_many is not None:
        if isinstance(image_many, (str, bytes, bytearray, Mapping)) or not isinstance(image_many, Iterable):
            raise CoverSignatureError("cover_images must be an array")
        signatures.extend(build_visual_signature(value) for value in image_many)
    image_one = candidate.get("cover_image")
    if image_one is not None:
        signatures.append(build_visual_signature(image_one))
    return signatures


def _aggregate_visual_comparisons(
    queries: Sequence[Mapping[str, object] | str],
    candidates: Sequence[Mapping[str, object] | str],
) -> tuple[dict[str, float] | None, int]:
    if not queries or not candidates:
        return None, 0
    # Every captured cover gets its best candidate-cover view.  Averaging the
    # best three means an alternate exposure/view can help without allowing a
    # single coincidental photo to erase contradictory evidence indefinitely.
    per_query = []
    comparisons = 0
    for query in queries:
        possible = []
        for candidate in candidates:
            possible.append(compare_visual_signatures(query, candidate))
            comparisons += 1
        per_query.append(max(possible, key=lambda item: item["visual"]))
    selected = sorted(per_query, key=lambda item: item["visual"], reverse=True)[:3]
    names = ("color", "structure", "gradient", "aspect", "visual")
    aggregate = {
        name: round(sum(item[name] for item in selected) / len(selected), 4)
        for name in names
    }
    return aggregate, comparisons


def _apply_session_ambiguity(ranked: list[dict[str, Any]]) -> None:
    """Annotate the top result and cap every candidate in a near-tie."""
    if not ranked:
        return
    top = ranked[0]
    uncapped_top = float(top["match_confidence"])
    runner = ranked[1] if len(ranked) > 1 else None
    runner_confidence = float(runner["match_confidence"]) if runner is not None else None
    margin = (
        round(max(0.0, uncapped_top - runner_confidence), 4)
        if runner_confidence is not None
        else None
    )
    ambiguous = margin is not None and margin <= AMBIGUITY_MARGIN_THRESHOLD
    close_candidates = [
        item
        for item in ranked
        if round(uncapped_top - float(item["match_confidence"]), 4)
        <= AMBIGUITY_MARGIN_THRESHOLD
    ]
    top["match_evidence"]["ambiguity"] = {
        "ambiguous": ambiguous,
        "margin": margin,
        "threshold": AMBIGUITY_MARGIN_THRESHOLD,
        "uncapped_top_confidence": round(uncapped_top, 4),
        "runner_up_candidate_id": (
            runner["candidate_capture_id"] if runner is not None else None
        ),
        "runner_up_confidence": (
            round(runner_confidence, 4) if runner_confidence is not None else None
        ),
        "close_candidate_count": len(close_candidates),
    }
    if not ambiguous:
        return

    for item in close_candidates:
        item["match_confidence"] = round(
            min(float(item["match_confidence"]), AMBIGUOUS_CONFIDENCE_CAP),
            4,
        )
        item["match_evidence"]["band"] = (
            "review" if item["match_confidence"] >= 0.58 else "unlikely"
        )
    reasons = top["match_evidence"].setdefault("reasons", [])
    ambiguity_reason = "Top candidates are within the ambiguity margin; manual review is required."
    if ambiguity_reason not in reasons:
        if len(reasons) >= 4:
            reasons[-1] = ambiguity_reason
        else:
            reasons.append(ambiguity_reason)


def rank_cover_session_matches(
    *,
    rows: Iterable[Mapping[str, object]],
    candidates: Iterable[Mapping[str, object]],
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Rank one deferred-review capture session as a single proposal list.

    OCR is combined from both ``cover`` and ``title_page`` rows.  Visual
    evidence is aggregated only from ``cover`` rows, because a title page is
    not expected to resemble a candidate cover.  All rows must share one
    non-empty ``session_id``.  Candidate records accept the same fields as
    :func:`rank_cover_matches`, plus plural ``visual_signatures`` or
    ``cover_images`` for editions with several stored cover views.
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError("limit must be an integer between 1 and 100")
    session_rows = [dict(row) for row in rows]
    if not session_rows:
        raise ValueError("a cover matching session requires at least one row")
    if len(session_rows) > MAX_SESSION_ROWS:
        raise ValueError(f"a cover matching session is limited to {MAX_SESSION_ROWS} rows")
    session_ids = {str(row.get("session_id") or "").strip() for row in session_rows}
    if (
        "" in session_ids
        or len(session_ids) != 1
        or any(len(value) > MAX_CANDIDATE_ID_CHARS for value in session_ids)
    ):
        raise ValueError("all cover matching rows must share one non-empty session_id")
    session_id = next(iter(session_ids))

    ocr_observations = []
    cover_signatures = []
    for row in session_rows:
        role = str(row.get("photo_role") or "").strip()
        if role not in {"cover", "title_page"}:
            raise ValueError("photo_role must be cover or title_page")
        ocr = str(row.get("ocr_text") or "").strip()
        if ocr and ocr not in ocr_observations:
            ocr_observations.append(ocr)
        signature = row.get("visual_signature")
        if role == "cover" and signature is not None:
            cover_signatures.append(parse_visual_signature(signature))
    combined_ocr = "\n".join(ocr_observations)
    if not combined_ocr and not cover_signatures:
        raise CoverSignatureError("session has neither OCR nor a cover visual signature")

    ranked = []
    for candidate_index, raw in enumerate(candidates):
        if candidate_index >= MAX_RANK_CANDIDATES:
            raise ValueError(f"cover matching is limited to {MAX_RANK_CANDIDATES} candidates")
        candidate = dict(raw)
        capture_id = str(candidate.get("capture_id") or candidate.get("id") or "").strip()
        if not capture_id or len(capture_id) > MAX_CANDIDATE_ID_CHARS:
            raise ValueError("each cover candidate requires capture_id")
        title = str(candidate.get("title") or "")
        author = str(candidate.get("author") or "")
        year = str(candidate.get("year") or "")
        candidate_ocr = str(candidate.get("ocr_text") or "")
        if (
            len(title) > MAX_CANDIDATE_TITLE_CHARS
            or len(author) > MAX_CANDIDATE_AUTHOR_CHARS
            or len(year) > MAX_CANDIDATE_YEAR_CHARS
            or len(candidate_ocr) > MAX_CANDIDATE_OCR_CHARS
        ):
            raise ValueError("cover candidate metadata exceeds its size limit")
        text, text_details = _text_match_evidence(
            combined_ocr,
            title,
            candidate_ocr,
            candidate_author=author,
            candidate_year=year,
        )
        candidate_signatures = _candidate_signatures(candidate)
        visual, comparison_count = _aggregate_visual_comparisons(
            cover_signatures,
            candidate_signatures,
        )
        if text is None and visual is None:
            continue
        confidence, evidence = _combined_match_evidence(
            text,
            visual,
            text_details=text_details,
        )
        evidence["session"] = {
            "row_count": len(session_rows),
            "ocr_observation_count": len(ocr_observations),
            "cover_signature_count": len(cover_signatures),
            "candidate_signature_count": len(candidate_signatures),
            "visual_comparison_count": comparison_count,
        }
        ranked.append(
            {
                "session_id": session_id,
                "candidate_capture_id": capture_id,
                "title": title,
                "author": author,
                "year": year,
                "match_confidence": confidence,
                "match_evidence": evidence,
            }
        )

    ranked.sort(key=lambda item: (-item["match_confidence"], item["candidate_capture_id"]))
    _apply_session_ambiguity(ranked)
    for rank, item in enumerate(ranked[:limit], 1):
        item["rank"] = rank
    return ranked[:limit]

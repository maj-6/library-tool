"""Parameterized processing operations for correction recipes.

These extend the adjustment vocabulary beyond the single
``grayscale-threshold-blend-v1`` recipe in :mod:`librarytool.processing.raster`.
Each operation is an immutable, self-describing recipe that renders a new image
from an input image and does no I/O.

Two properties are load-bearing and shape every choice here:

*Determinism.*  An operation's serialized form travels inside a correction
transform command, and that command is content-addressed — the same recipe must
always produce the same bytes.  Per-pixel operations are therefore expressed as
integer lookup tables built with explicit half-up rounding rather than
floating-point pixel math, which is how the existing manual binary adjustment
already works.

*Cross-language wire safety.*  Every wire parameter is an integer.  A float
would have to survive Python and JavaScript canonical JSON identically, and the
two disagree on exponent formatting (``1e-07`` versus ``1e-7``), so sub-unit
precision is carried in fixed units — tenths for a blur radius, hundredths for
gamma — and converted at render time.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from PIL import Image, ImageFilter

PROCESSING_OPERATION_SCHEMA = "org.whl.raster.processing-operation"
PROCESSING_OPERATION_VERSION = 1
MAX_OPERATIONS_PER_RECIPE = 16


class ProcessingOperationError(ValueError):
    """The requested processing operation or its parameters are invalid."""


def _bounded_integer(value: object, *, name: str, minimum: int, maximum: int) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not minimum <= value <= maximum
    ):
        raise ProcessingOperationError(
            f"{name} must be an integer from {minimum} through {maximum}"
        )
    return value


def _round_half_up(value: float) -> int:
    return int(math.floor(value + 0.5))


def _clamp_byte(value: int) -> int:
    return max(0, min(255, value))


def _rgb(image: Image.Image) -> Image.Image:
    """Flatten to RGB the same way the correction kernel decodes a source.

    Invisible colour under an alpha channel must not become visible ink, so a
    transparent input is composited onto white rather than simply dropped.
    """

    if image.mode in {"RGBA", "LA"} or (
        image.mode == "P" and "transparency" in image.info
    ):
        rgba = image.convert("RGBA")
        background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        return Image.alpha_composite(background, rgba).convert("RGB")
    return image.convert("RGB")


def _apply_channel_lookups(
    image: Image.Image,
    red: Sequence[int],
    green: Sequence[int],
    blue: Sequence[int],
) -> Image.Image:
    """Apply one 256-entry table per channel.

    A grayscale input stays grayscale when all three tables agree, so a
    monochrome scan is not silently widened to RGB by a neutral operation.
    """

    if image.mode == "L" and list(red) == list(green) == list(blue):
        return image.point(list(red), mode="L")
    return _rgb(image).point(list(red) + list(green) + list(blue))


@dataclass(frozen=True, slots=True)
class ProcessingOperation:
    """Base class for a self-describing, deterministic image operation."""

    algorithm: str = ""

    def parameters(self) -> dict[str, Any]:  # pragma: no cover - interface
        raise NotImplementedError

    @property
    def rule(self) -> str:  # pragma: no cover - interface
        raise NotImplementedError

    def apply(self, image: Image.Image) -> Image.Image:  # pragma: no cover
        raise NotImplementedError

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": PROCESSING_OPERATION_SCHEMA,
            "version": PROCESSING_OPERATION_VERSION,
            "algorithm": self.algorithm,
            "rule": self.rule,
            **self.parameters(),
        }


@dataclass(frozen=True, slots=True)
class UnsharpMaskOperation(ProcessingOperation):
    """Classic unsharp mask: blur, difference, add the difference back.

    ``threshold`` suppresses sharpening where the local difference is small,
    which keeps paper grain and JPEG noise from being amplified into speckle.
    """

    radius_tenths: int = 10
    amount_percent: int = 150
    threshold: int = 3
    algorithm: str = "unsharp-mask-v1"

    def __post_init__(self) -> None:
        _bounded_integer(self.radius_tenths, name="radius_tenths", minimum=1, maximum=500)
        _bounded_integer(
            self.amount_percent, name="amount_percent", minimum=0, maximum=500
        )
        _bounded_integer(self.threshold, name="threshold", minimum=0, maximum=255)

    @property
    def radius(self) -> float:
        return self.radius_tenths / 10.0

    def parameters(self) -> dict[str, Any]:
        return {
            "radius_tenths": self.radius_tenths,
            "amount_percent": self.amount_percent,
            "threshold": self.threshold,
        }

    @property
    def rule(self) -> str:
        return (
            "pillow_unsharp_mask(radius=radius_tenths/10, "
            "percent=amount_percent, threshold=threshold)"
        )

    def apply(self, image: Image.Image) -> Image.Image:
        source = image if image.mode == "L" else _rgb(image)
        return source.filter(
            ImageFilter.UnsharpMask(
                radius=self.radius,
                percent=self.amount_percent,
                threshold=self.threshold,
            )
        )


@dataclass(frozen=True, slots=True)
class KernelSharpenOperation(ProcessingOperation):
    """A fixed 3x3 Laplacian sharpen blended against the identity.

    Unlike an unsharp mask this has no radius: it only ever touches immediate
    neighbours, which makes it the cheaper choice for text that is already
    close to in focus.
    """

    strength_percent: int = 50
    algorithm: str = "kernel-sharpen-v1"

    def __post_init__(self) -> None:
        _bounded_integer(
            self.strength_percent, name="strength_percent", minimum=0, maximum=100
        )

    def parameters(self) -> dict[str, Any]:
        return {"strength_percent": self.strength_percent}

    @property
    def rule(self) -> str:
        return (
            "3x3 kernel [[0,-1,0],[-1,5,-1],[0,-1,0]] blended with identity at "
            "strength_percent/100"
        )

    def apply(self, image: Image.Image) -> Image.Image:
        if self.strength_percent == 0:
            return image.copy()
        source = image if image.mode == "L" else _rgb(image)
        strength = self.strength_percent / 100.0
        # Blend the sharpening kernel toward the identity so the strength dial
        # is continuous instead of switching between two fixed looks.
        kernel = [
            0.0, -strength, 0.0,
            -strength, 1.0 + 4.0 * strength, -strength,
            0.0, -strength, 0.0,
        ]
        return source.filter(ImageFilter.Kernel((3, 3), kernel, scale=1.0, offset=0))


@dataclass(frozen=True, slots=True)
class GammaOperation(ProcessingOperation):
    """Non-linear tone transfer: ``output = 255 * (input/255) ** (1/gamma)``.

    Gamma above 1 lifts the midtones without clipping the white point, which is
    usually what a faint pencil annotation or a thin ink needs.
    """

    gamma_hundredths: int = 100
    algorithm: str = "gamma-v1"

    def __post_init__(self) -> None:
        _bounded_integer(
            self.gamma_hundredths, name="gamma_hundredths", minimum=10, maximum=1000
        )

    @property
    def gamma(self) -> float:
        return self.gamma_hundredths / 100.0

    def parameters(self) -> dict[str, Any]:
        return {"gamma_hundredths": self.gamma_hundredths}

    @property
    def rule(self) -> str:
        return (
            "round_half_up(255 * (value/255) ** (100/gamma_hundredths)), "
            "clamped_0_255"
        )

    def lookup(self) -> list[int]:
        exponent = 1.0 / self.gamma
        return [
            _clamp_byte(_round_half_up(255.0 * (value / 255.0) ** exponent))
            for value in range(256)
        ]

    def apply(self, image: Image.Image) -> Image.Image:
        table = self.lookup()
        return _apply_channel_lookups(image, table, table, table)


@dataclass(frozen=True, slots=True)
class ContrastOperation(ProcessingOperation):
    """Linear contrast around mid-grey, and the inverse toward flat grey."""

    contrast_percent: int = 0
    algorithm: str = "contrast-v1"

    def __post_init__(self) -> None:
        _bounded_integer(
            self.contrast_percent, name="contrast_percent", minimum=-100, maximum=100
        )

    def parameters(self) -> dict[str, Any]:
        return {"contrast_percent": self.contrast_percent}

    @property
    def rule(self) -> str:
        return (
            "factor = 1 + contrast_percent/100 when positive else "
            "1 + contrast_percent/100 toward 127.5; "
            "round_half_up(127.5 + (value - 127.5) * factor), clamped_0_255"
        )

    def lookup(self) -> list[int]:
        # A positive dial doubles slope at +100; a negative dial collapses to
        # flat grey at -100. Both ends stay finite and monotonic.
        factor = 1.0 + self.contrast_percent / 100.0
        return [
            _clamp_byte(_round_half_up(127.5 + (value - 127.5) * factor))
            for value in range(256)
        ]

    def apply(self, image: Image.Image) -> Image.Image:
        table = self.lookup()
        return _apply_channel_lookups(image, table, table, table)


@dataclass(frozen=True, slots=True)
class ChannelGainOperation(ProcessingOperation):
    """Independent per-channel gain, for correcting a colour cast by hand."""

    red_percent: int = 100
    green_percent: int = 100
    blue_percent: int = 100
    algorithm: str = "channel-gain-v1"

    def __post_init__(self) -> None:
        for name in ("red_percent", "green_percent", "blue_percent"):
            _bounded_integer(
                getattr(self, name), name=name, minimum=0, maximum=400
            )

    def parameters(self) -> dict[str, Any]:
        return {
            "red_percent": self.red_percent,
            "green_percent": self.green_percent,
            "blue_percent": self.blue_percent,
        }

    @property
    def rule(self) -> str:
        return "round_half_up(value * channel_percent/100), clamped_0_255"

    def apply(self, image: Image.Image) -> Image.Image:
        def table(percent: int) -> list[int]:
            gain = percent / 100.0
            return [_clamp_byte(_round_half_up(value * gain)) for value in range(256)]

        return _apply_channel_lookups(
            image,
            table(self.red_percent),
            table(self.green_percent),
            table(self.blue_percent),
        )


@dataclass(frozen=True, slots=True)
class WhiteBalanceOperation(ProcessingOperation):
    """Neutralize a colour cast, automatically or by hand.

    ``gray_world`` measures the image: it assumes the average of a scene is
    neutral and scales each channel toward the overall mean.  That is the right
    default for a photographed page under mixed lighting, but it misfires on a
    genuinely single-hued page, so ``manual`` exposes the same correction as
    temperature and tint dials the reviewer controls directly.
    """

    mode: str = "gray_world"
    strength_percent: int = 100
    temperature: int = 0
    tint: int = 0
    algorithm: str = "white-balance-v1"

    def __post_init__(self) -> None:
        if self.mode not in {"gray_world", "manual"}:
            raise ProcessingOperationError(
                "white balance mode must be gray_world or manual"
            )
        _bounded_integer(
            self.strength_percent, name="strength_percent", minimum=0, maximum=100
        )
        _bounded_integer(self.temperature, name="temperature", minimum=-100, maximum=100)
        _bounded_integer(self.tint, name="tint", minimum=-100, maximum=100)
        if self.mode == "gray_world" and (self.temperature or self.tint):
            raise ProcessingOperationError(
                "gray_world white balance does not take temperature or tint"
            )

    def parameters(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "strength_percent": self.strength_percent,
            "temperature": self.temperature,
            "tint": self.tint,
        }

    @property
    def rule(self) -> str:
        if self.mode == "gray_world":
            return (
                "per-channel gain = 1 + strength_percent/100 * "
                "(mean_of_all_channels/channel_mean - 1); "
                "round_half_up(value * gain), clamped_0_255"
            )
        return (
            "red gain = 1 + strength_percent/100 * temperature/200; "
            "blue gain = 1 - strength_percent/100 * temperature/200; "
            "green gain = 1 + strength_percent/100 * tint/200; "
            "round_half_up(value * gain), clamped_0_255"
        )

    def _gains(self, image: Image.Image) -> tuple[float, float, float]:
        strength = self.strength_percent / 100.0
        if self.mode == "manual":
            warm = strength * self.temperature / 200.0
            green = strength * self.tint / 200.0
            return 1.0 + warm, 1.0 + green, 1.0 - warm
        rgb = _rgb(image)
        channel_means = [band.tobytes() for band in rgb.split()]
        means = [
            (sum(band) / len(band)) if band else 0.0 for band in channel_means
        ]
        overall = sum(means) / 3.0
        if overall <= 0.0:
            return 1.0, 1.0, 1.0
        gains = []
        for mean in means:
            # A channel with no signal cannot be scaled into one; leaving it
            # alone is safer than dividing by (near) zero.
            target = 1.0 if mean <= 0.0 else overall / mean
            gains.append(1.0 + strength * (target - 1.0))
        return gains[0], gains[1], gains[2]

    def apply(self, image: Image.Image) -> Image.Image:
        if self.strength_percent == 0:
            return image.copy()
        red_gain, green_gain, blue_gain = self._gains(image)

        def table(gain: float) -> list[int]:
            return [_clamp_byte(_round_half_up(value * gain)) for value in range(256)]

        # White balance is inherently a colour operation, so a grayscale input
        # is widened rather than silently ignored.
        return _rgb(image).point(
            table(red_gain) + table(green_gain) + table(blue_gain)
        )


_OPERATION_TYPES: dict[str, type[ProcessingOperation]] = {
    "unsharp-mask-v1": UnsharpMaskOperation,
    "kernel-sharpen-v1": KernelSharpenOperation,
    "gamma-v1": GammaOperation,
    "contrast-v1": ContrastOperation,
    "channel-gain-v1": ChannelGainOperation,
    "white-balance-v1": WhiteBalanceOperation,
}
PROCESSING_ALGORITHMS = tuple(sorted(_OPERATION_TYPES))


def operation_from_dict(payload: Mapping[str, Any]) -> ProcessingOperation:
    """Rebuild one operation from its canonical wire form.

    Validation is exact in both directions: unknown keys are rejected, and the
    document must already equal the recipe's own canonical serialization.  That
    second check is what stops two spellings of the same operation from
    producing two different command fingerprints.
    """

    if not isinstance(payload, Mapping):
        raise ProcessingOperationError("a processing operation must be an object")
    version = payload.get("version")
    if (
        payload.get("schema") != PROCESSING_OPERATION_SCHEMA
        or not isinstance(version, int)
        or isinstance(version, bool)
        or version != PROCESSING_OPERATION_VERSION
    ):
        raise ProcessingOperationError("unsupported processing operation schema")
    algorithm = payload.get("algorithm")
    operation_type = _OPERATION_TYPES.get(algorithm) if isinstance(algorithm, str) else None
    if operation_type is None:
        raise ProcessingOperationError(
            f"unsupported processing algorithm: {algorithm!r}"
        )
    fixed = {"schema", "version", "algorithm", "rule"}
    parameter_names = frozenset(operation_type().parameters())
    unknown = sorted(set(payload) - fixed - parameter_names)
    missing = sorted(parameter_names - set(payload))
    if unknown or missing:
        raise ProcessingOperationError(
            f"processing operation fields must match {algorithm} exactly"
        )
    try:
        operation = operation_type(
            **{name: payload[name] for name in parameter_names}
        )
    except TypeError as exc:
        raise ProcessingOperationError(
            f"processing operation parameters are invalid for {algorithm}"
        ) from exc
    canonical = operation.as_dict()
    if any(
        type(payload[name]) is not type(canonical[name])
        or payload[name] != canonical[name]
        for name in canonical
    ):
        raise ProcessingOperationError(
            f"processing operation is not a canonical {algorithm} recipe"
        )
    return operation


def operations_from_list(
    payload: Sequence[Mapping[str, Any]],
) -> tuple[ProcessingOperation, ...]:
    if isinstance(payload, (str, bytes)) or not isinstance(payload, Sequence):
        raise ProcessingOperationError("operations must be a sequence")
    if not payload:
        raise ProcessingOperationError(
            "operations must contain at least one operation or be absent"
        )
    if len(payload) > MAX_OPERATIONS_PER_RECIPE:
        raise ProcessingOperationError(
            f"operations must contain at most {MAX_OPERATIONS_PER_RECIPE} entries"
        )
    return tuple(operation_from_dict(value) for value in payload)


def apply_operations(
    image: Image.Image,
    operations: Sequence[ProcessingOperation],
) -> Image.Image:
    """Run an ordered pipeline, returning a new image.

    Order is significant and is the caller's: white balance before a contrast
    stretch is a different picture from the reverse, so the list is never
    reordered or deduplicated.
    """

    if not isinstance(image, Image.Image):
        raise TypeError("image must be a Pillow Image")
    result = image
    for index, operation in enumerate(operations):
        if not isinstance(operation, ProcessingOperation):
            raise TypeError(
                f"operations[{index}] must be a ProcessingOperation"
            )
        result = operation.apply(result)
    return result


__all__ = [
    "MAX_OPERATIONS_PER_RECIPE",
    "PROCESSING_ALGORITHMS",
    "PROCESSING_OPERATION_SCHEMA",
    "PROCESSING_OPERATION_VERSION",
    "ChannelGainOperation",
    "ContrastOperation",
    "GammaOperation",
    "KernelSharpenOperation",
    "ProcessingOperation",
    "ProcessingOperationError",
    "UnsharpMaskOperation",
    "WhiteBalanceOperation",
    "apply_operations",
    "operation_from_dict",
    "operations_from_list",
]

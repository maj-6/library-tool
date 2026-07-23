"""Provider-neutral raster processing contracts and operations."""

from .raster import (
    EXIF_ORIENTED_NORMALIZED,
    POINT_ORDER,
    ManualBinaryAdjustRecipe,
    PageBoundaryProposal,
    PerspectiveTransformResult,
    RasterInputError,
    RasterLimits,
    apply_manual_binary_adjust,
    apply_perspective_transform,
    validate_normalized_quad,
)

__all__ = [
    "EXIF_ORIENTED_NORMALIZED",
    "POINT_ORDER",
    "ManualBinaryAdjustRecipe",
    "PageBoundaryProposal",
    "PerspectiveTransformResult",
    "RasterInputError",
    "RasterLimits",
    "apply_manual_binary_adjust",
    "apply_perspective_transform",
    "validate_normalized_quad",
]

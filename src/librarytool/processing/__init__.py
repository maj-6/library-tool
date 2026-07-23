"""Provider-neutral raster processing contracts and operations."""

from .capture_compat import (
    CAPTURE_DETECTOR,
    CAPTURE_DETECTOR_VERSION,
    apply_capture_perspective_compat,
    find_capture_page_quad,
    order_capture_quad,
    propose_capture_page_boundary,
)
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
    "CAPTURE_DETECTOR",
    "CAPTURE_DETECTOR_VERSION",
    "EXIF_ORIENTED_NORMALIZED",
    "POINT_ORDER",
    "ManualBinaryAdjustRecipe",
    "PageBoundaryProposal",
    "PerspectiveTransformResult",
    "RasterInputError",
    "RasterLimits",
    "apply_capture_perspective_compat",
    "apply_manual_binary_adjust",
    "apply_perspective_transform",
    "find_capture_page_quad",
    "order_capture_quad",
    "propose_capture_page_boundary",
    "validate_normalized_quad",
]

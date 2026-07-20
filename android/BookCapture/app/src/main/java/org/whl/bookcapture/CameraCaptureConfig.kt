package org.whl.bookcapture

/** Camera presets are kept independent of preferences and CameraX builders so
 * their speed/detail trade-offs remain explicit and unit-testable. */
internal enum class CameraCaptureProfile {
    LOW_RESOLUTION,
    FAST,
    MORE_DETAIL,
}

internal enum class CameraResolutionFallback {
    CLOSEST_LOWER_THEN_HIGHER,
}

/** The capture screen is portrait-locked so controls never jump around. This
 * value changes CameraX's target rotation and the page-margin guide instead. */
internal enum class CameraCaptureOrientation(val storedValue: String) {
    PORTRAIT("portrait"),
    LANDSCAPE("landscape"),
}

internal data class CameraCaptureConfig(
    val targetWidth: Int,
    val targetHeight: Int,
    val jpegQuality: Int,
    val zslEligible: Boolean,
    val resolutionFallback: CameraResolutionFallback,
)

internal fun cameraCaptureProfile(storedValue: String?): CameraCaptureProfile =
    when (storedValue?.trim()) {
        "low" -> CameraCaptureProfile.LOW_RESOLUTION
        "detail" -> CameraCaptureProfile.MORE_DETAIL
        else -> CameraCaptureProfile.FAST
    }

internal fun cameraCaptureOrientation(storedValue: String?): CameraCaptureOrientation =
    when (storedValue?.trim()) {
        CameraCaptureOrientation.LANDSCAPE.storedValue -> CameraCaptureOrientation.LANDSCAPE
        else -> CameraCaptureOrientation.PORTRAIT
    }

/** Camera ranges differ substantially between devices. Keeping all persisted
 * controls inside the range reported by the active camera avoids sending an
 * invalid CameraControl request after moving preferences between devices. */
internal fun normalizedZoomRatio(requested: Float, minimum: Float, maximum: Float): Float {
    val safeMinimum = minimum.takeIf { it.isFinite() && it > 0f } ?: 1f
    val safeMaximum = maximum.takeIf { it.isFinite() && it >= safeMinimum } ?: safeMinimum
    val safeRequested = requested.takeIf { it.isFinite() && it > 0f } ?: 1f
    return safeRequested.coerceIn(safeMinimum, safeMaximum)
}

internal fun normalizedExposureIndex(requested: Int, minimum: Int, maximum: Int): Int =
    if (minimum <= maximum) requested.coerceIn(minimum, maximum) else 0

internal fun zoomRatioFromProgress(
    progress: Int,
    maximumProgress: Int,
    minimumRatio: Float,
    maximumRatio: Float,
): Float {
    val minimum = normalizedZoomRatio(minimumRatio, minimumRatio, maximumRatio)
    val maximum = normalizedZoomRatio(maximumRatio, minimum, maximumRatio)
    if (maximumProgress <= 0 || maximum <= minimum) return minimum
    val fraction = progress.coerceIn(0, maximumProgress).toFloat() / maximumProgress
    return minimum + (maximum - minimum) * fraction
}

internal fun zoomProgressFromRatio(
    ratio: Float,
    maximumProgress: Int,
    minimumRatio: Float,
    maximumRatio: Float,
): Int {
    if (maximumProgress <= 0 || maximumRatio <= minimumRatio) return 0
    val normalized = normalizedZoomRatio(ratio, minimumRatio, maximumRatio)
    return (((normalized - minimumRatio) / (maximumRatio - minimumRatio)) * maximumProgress)
        .toInt()
        .coerceIn(0, maximumProgress)
}

internal fun cameraCaptureConfig(profile: CameraCaptureProfile): CameraCaptureConfig =
    when (profile) {
        CameraCaptureProfile.LOW_RESOLUTION -> CameraCaptureConfig(
            targetWidth = 1280,
            targetHeight = 960,
            jpegQuality = 78,
            zslEligible = true,
            resolutionFallback = CameraResolutionFallback.CLOSEST_LOWER_THEN_HIGHER,
        )
        CameraCaptureProfile.FAST -> CameraCaptureConfig(
            targetWidth = 1600,
            targetHeight = 1200,
            jpegQuality = 80,
            zslEligible = true,
            resolutionFallback = CameraResolutionFallback.CLOSEST_LOWER_THEN_HIGHER,
        )
        CameraCaptureProfile.MORE_DETAIL -> CameraCaptureConfig(
            targetWidth = 2048,
            targetHeight = 1536,
            jpegQuality = 85,
            zslEligible = false,
            resolutionFallback = CameraResolutionFallback.CLOSEST_LOWER_THEN_HIGHER,
        )
    }

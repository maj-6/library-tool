package org.whl.bookcapture

/** Camera presets are kept independent of preferences and CameraX builders so
 * their speed/detail trade-offs remain explicit and unit-testable. */
internal enum class CameraCaptureProfile {
    FAST,
    MORE_DETAIL,
}

internal enum class CameraResolutionFallback {
    CLOSEST_LOWER_THEN_HIGHER,
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
        "detail" -> CameraCaptureProfile.MORE_DETAIL
        else -> CameraCaptureProfile.FAST
    }

internal fun cameraCaptureConfig(profile: CameraCaptureProfile): CameraCaptureConfig =
    when (profile) {
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

package org.whl.bookcapture

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class CameraCaptureConfigTest {

    @Test
    fun storedProfileValuesDefaultSafelyToFast() {
        assertEquals(CameraCaptureProfile.LOW_RESOLUTION, cameraCaptureProfile("low"))
        assertEquals(CameraCaptureProfile.MORE_DETAIL, cameraCaptureProfile("detail"))
        assertEquals(CameraCaptureProfile.FAST, cameraCaptureProfile("fast"))
        assertEquals(CameraCaptureProfile.FAST, cameraCaptureProfile("unknown"))
        assertEquals(CameraCaptureProfile.FAST, cameraCaptureProfile(null))
    }

    @Test
    fun fastMapsToSensorOrientedLowResolutionWithLowerFirstFallback() {
        val config = cameraCaptureConfig(CameraCaptureProfile.FAST)

        assertEquals(1600, config.targetWidth)
        assertEquals(1200, config.targetHeight)
        assertEquals(80, config.jpegQuality)
        assertTrue(config.zslEligible)
        assertEquals(
            CameraResolutionFallback.CLOSEST_LOWER_THEN_HIGHER,
            config.resolutionFallback,
        )
    }

    @Test
    fun moreDetailMapsToLargerNonZslCapture() {
        val config = cameraCaptureConfig(CameraCaptureProfile.MORE_DETAIL)

        assertEquals(2048, config.targetWidth)
        assertEquals(1536, config.targetHeight)
        assertEquals(85, config.jpegQuality)
        assertFalse(config.zslEligible)
        assertEquals(
            CameraResolutionFallback.CLOSEST_LOWER_THEN_HIGHER,
            config.resolutionFallback,
        )
    }

    @Test
    fun lowResolutionProfileRequestsADeviceSafeLowerCaptureSize() {
        val config = cameraCaptureConfig(CameraCaptureProfile.LOW_RESOLUTION)

        assertEquals(1280, config.targetWidth)
        assertEquals(960, config.targetHeight)
        assertEquals(78, config.jpegQuality)
        assertTrue(config.zslEligible)
        assertEquals(
            CameraResolutionFallback.CLOSEST_LOWER_THEN_HIGHER,
            config.resolutionFallback,
        )
    }

    @Test
    fun orientationDefaultsToPortraitAndAcceptsLandscape() {
        assertEquals(CameraCaptureOrientation.PORTRAIT, cameraCaptureOrientation(null))
        assertEquals(CameraCaptureOrientation.PORTRAIT, cameraCaptureOrientation("unknown"))
        assertEquals(CameraCaptureOrientation.PORTRAIT, cameraCaptureOrientation("portrait"))
        assertEquals(CameraCaptureOrientation.LANDSCAPE, cameraCaptureOrientation("landscape"))
    }

    @Test
    fun cameraRequestsAreClampedToActiveLensCapabilities() {
        assertEquals(1f, normalizedZoomRatio(Float.NaN, 1f, 8f))
        assertEquals(1f, normalizedZoomRatio(0.5f, 1f, 8f))
        assertEquals(8f, normalizedZoomRatio(12f, 1f, 8f))
        assertEquals(-2, normalizedExposureIndex(-8, -2, 3))
        assertEquals(3, normalizedExposureIndex(8, -2, 3))
        assertEquals(0, normalizedExposureIndex(2, 3, -3))
    }

    @Test
    fun zoomSeekBarMappingRoundTripsAcrossReportedRange() {
        assertEquals(1f, zoomRatioFromProgress(0, 1000, 1f, 5f))
        assertEquals(3f, zoomRatioFromProgress(500, 1000, 1f, 5f))
        assertEquals(5f, zoomRatioFromProgress(1000, 1000, 1f, 5f))
        assertEquals(500, zoomProgressFromRatio(3f, 1000, 1f, 5f))
        assertEquals(0, zoomProgressFromRatio(Float.NaN, 1000, 1f, 5f))
    }
}

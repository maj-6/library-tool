package org.whl.bookcapture

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class CameraCaptureConfigTest {

    @Test
    fun storedProfileValuesDefaultSafelyToFast() {
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
}

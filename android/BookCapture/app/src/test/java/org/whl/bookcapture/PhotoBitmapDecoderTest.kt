package org.whl.bookcapture

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class PhotoBitmapDecoderTest {

    @Test
    fun landscapeCaptureIsSampledAndBoundedByWidth() {
        assertEquals(2, bitmapDecodeSampleSize(1600, 1200, 640, 640))
        assertEquals(
            DecodedBitmapSize(width = 640, height = 480),
            boundedBitmapSize(800, 600, 640, 640),
        )
    }

    @Test
    fun portraitCaptureIsSampledAndBoundedByHeight() {
        assertEquals(2, bitmapDecodeSampleSize(1200, 1600, 640, 640))
        assertEquals(
            DecodedBitmapSize(width = 480, height = 640),
            boundedBitmapSize(600, 800, 640, 640),
        )
    }

    @Test
    fun longEdgeIsSampledEvenWhenShortEdgeIsAlreadyBelowLimit() {
        assertEquals(4, bitmapDecodeSampleSize(4000, 500, 640, 640))
        assertEquals(
            DecodedBitmapSize(width = 640, height = 80),
            boundedBitmapSize(1000, 125, 640, 640),
        )
    }

    @Test
    fun imagesWithinBoundsAreNotUpscaled() {
        assertEquals(1, bitmapDecodeSampleSize(320, 240, 640, 640))
        assertEquals(
            DecodedBitmapSize(width = 320, height = 240),
            boundedBitmapSize(320, 240, 640, 640),
        )
    }

    @Test
    fun quarterTurnExifOrientationsSwapEncodedAxes() {
        assertTrue(exifOrientationSwapsAxes(5))
        assertTrue(exifOrientationSwapsAxes(6))
        assertTrue(exifOrientationSwapsAxes(7))
        assertTrue(exifOrientationSwapsAxes(8))
        assertFalse(exifOrientationSwapsAxes(1))
        assertFalse(exifOrientationSwapsAxes(2))
        assertFalse(exifOrientationSwapsAxes(3))
        assertFalse(exifOrientationSwapsAxes(4))
    }

    @Test
    fun asymmetricOrientedBoundsMapToEncodedAxes() {
        // A 90-degree EXIF rotation means a 384x512 displayed limit must be
        // applied as 512x384 before rotating the decoded pixels.
        assertEquals(4, bitmapDecodeSampleSize(1200, 1600, 512, 384))
        assertEquals(
            DecodedBitmapSize(width = 288, height = 384),
            boundedBitmapSize(300, 400, 512, 384),
        )
    }
}

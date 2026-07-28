package org.whl.bookcapture

import org.junit.Assert.assertEquals
import org.junit.Test

class CaptureElapsedTimeTest {
    @Test
    fun elapsedTimeUsesMinutesUntilTheFirstHour() {
        assertEquals("00:00", formatCaptureElapsed(0))
        assertEquals("00:59", formatCaptureElapsed(59_999))
        assertEquals("01:00", formatCaptureElapsed(60_000))
        assertEquals("59:59", formatCaptureElapsed(3_599_999))
    }

    @Test
    fun elapsedTimeAddsHoursForLongCaptures() {
        assertEquals("1:00:00", formatCaptureElapsed(3_600_000))
        assertEquals("12:34:56", formatCaptureElapsed(45_296_000))
    }

    @Test
    fun elapsedTimeDoesNotGoNegativeIfTheWallClockMovesBack() {
        assertEquals("00:00", formatCaptureElapsed(-1))
    }
}

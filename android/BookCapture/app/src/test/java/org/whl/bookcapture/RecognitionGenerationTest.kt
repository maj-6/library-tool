package org.whl.bookcapture

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class RecognitionGenerationTest {
    @Test
    fun onlyCurrentActiveGenerationAcceptsRecognitionCallbacks() {
        assertTrue(recognitionCallbackIsCurrent(4, 4, paused = false, stopped = false))
        assertFalse(recognitionCallbackIsCurrent(3, 4, paused = false, stopped = false))
        assertFalse(recognitionCallbackIsCurrent(4, 4, paused = true, stopped = false))
        assertFalse(recognitionCallbackIsCurrent(4, 4, paused = false, stopped = true))
    }
}
